"""Offline, journaled mark/sweep for single-host Git generations.

ponytail: full JSON collection exports suit the bounded Markdown source, not a
large distributed index. SQL audit rows stay; unknown artifacts fail closed.
Collection deletion uses Chroma's API; exact owned orphan segments are then
quarantined after read-only schema/reference validation, never internal SQL writes.
"""
import argparse
import hashlib
import json
import math
import os
import re
import shutil
import uuid
from contextlib import suppress
from datetime import timedelta
from pathlib import Path

import chromadb
from chromadb.errors import NotFoundError

from app import db as db_module
from app.config import settings
from app.models import KbSource, KbSyncJob
from app.rag import generations as g
from app.rag import segment_retention as segments
from app.rag.git_source import parser_profile, read_export
from app.rag.maintenance import service_guard
from app.rag.stage_cache import CACHE_VERSION, _digest
from app.rag.sync import publication_lock

DAYS = 7
HEX = re.compile(r'[a-f0-9]{64}')
TICKET = re.compile(r'[a-f0-9]{32}')
TERMINAL = {'published', 'superseded', 'failed'}
PENDING = {'queued', 'running', 'prepared'}
MAX_ARCHIVE_BYTES = 512 * 1024 * 1024


def root():
    path = Path(settings.KB_SYNC_STATE_DIR).absolute()
    _regular_tree(path)
    return path


def _regular_tree(path):
    for node in [path, *path.parents]:
        if node.is_symlink():
            raise ValueError('Symbolic links are forbidden in retention paths')


def _path(relative):
    value = Path(relative)
    if value.is_absolute() or '..' in value.parts or str(value) != relative:
        raise ValueError('Invalid retention relative path')
    result = root() / value
    _regular_tree(result)
    return result


def _read(path):
    _regular_tree(path)
    if not path.is_file() or path.stat().st_size > MAX_ARCHIVE_BYTES:
        raise ValueError('Missing or oversized retention artifact')
    return json.loads(path.read_text())


def _sync_dir(path):
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _sha(path):
    _regular_tree(path)
    h = hashlib.sha256()
    with path.open('rb') as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def _stop_chroma():
    from chromadb.api.shared_system_client import SharedSystemClient
    # Offline guard excludes backend readers; no client may retain the moved HNSW files.
    for system in tuple(SharedSystemClient._identifier_to_system.values()):
        system.stop()
    SharedSystemClient.clear_system_cache()
    from app.rag import retriever
    retriever._BM25_CACHE.clear()
    retriever._existing_cache.update(dir=None, ts=0.0, names=set())


def _segment_dir(ticket):
    return _path(f'quarantine/{ticket}/segments')


def _client():
    return chromadb.PersistentClient(path=settings.KB_VECTOR_DIR)


def _rows():
    with db_module.SessionLocal() as db:
        sources = db.query(KbSource).all()
        jobs = db.query(KbSyncJob).all()
        for row in [*sources, *jobs]:
            db.expunge(row)
    return sources, jobs


def _pins(sources, jobs, now):
    pinned = {key for s in sources for key in (s.active_job, s.previous_job) if key}
    desired = {(s.id, s.desired_seq) for s in sources}
    known = {j.id for j in jobs}
    if pinned - known:
        raise ValueError('A source pointer references a missing SQL job')
    reasons = {}
    for job in jobs:
        why = []
        if job.id in pinned:
            why.append('active_or_rollback')
        if (job.source_id, job.seq) in desired:
            why.append('latest_source_intent')
        if job.status in PENDING:
            why.append('pending_task')
        elif job.status not in TERMINAL | {'quarantined', 'collected'}:
            why.append('unknown_status')
        if (job.updated_at is None or job.created_at is None
                or max(job.updated_at, job.created_at) > now - timedelta(days=DAYS)):
            why.append('safety_window')
        if why:
            reasons[job.id] = why
    return reasons


def _snapshot_path(job):
    expected = _path(f'snapshots/{job.snapshot_hash}.json')
    if not HEX.fullmatch(job.snapshot_hash) or Path(job.snapshot_path).absolute() != expected:
        raise ValueError('SQL snapshot path is outside the canonical snapshot directory')
    return expected


def _files(directory, pattern):
    base = _path(directory)
    if not base.exists():
        return []
    files = []
    for path in base.rglob('*'):
        _regular_tree(path)
        if path.is_file() and re.fullmatch(pattern, str(path.relative_to(base))):
            files.append(path)
    return files


def _open_tickets():
    base = _path('quarantine')
    if not base.exists():
        return []
    tickets = []
    for path in base.iterdir():
        _regular_tree(path)
        if path.is_dir() and TICKET.fullmatch(path.name):
            if not (path / 'journal.json').exists():
                continue  # interrupted export, no mutation began; preserve the orphan for inspection
            journal = _read(path / 'journal.json')
            if journal['phase'] not in {'quarantined', 'restored', 'purged'}:
                tickets.append(path.name)
    return tickets


def plan(*, now=None):
    """Call under publication_lock. No cache writes, embedding calls or row changes."""
    now = now or g._now()
    unfinished = _open_tickets()
    if unfinished:
        raise ValueError(f'Finish interrupted retention ticket with --resume: {unfinished}')
    sources, jobs = _rows()
    reasons = _pins(sources, jobs, now)
    candidates = [j for j in jobs if j.id not in reasons and j.status in TERMINAL]
    for job in candidates:
        if not TICKET.fullmatch(job.id) or job.collection != f'kb_gen_{job.id}':
            raise ValueError('Noncanonical generation identity')
        _snapshot_path(job)
    selected = {j.id for j in candidates}
    retained = [j for j in jobs if j.id not in selected and j.status not in {'quarantined', 'collected'}]
    referenced = {_snapshot_path(j) for j in retained}
    def old(p):
        from datetime import UTC
        return p.stat().st_mtime <= now.replace(tzinfo=UTC).timestamp() - DAYS * 86400
    # Every source participates in the root set, even if not configured in this process.
    snapshots = [p for p in _files('snapshots', r'[a-f0-9]{64}\.json') if p not in referenced and old(p)]
    caches = []
    pending = any(j.status in PENDING or j.status not in TERMINAL | {'quarantined', 'collected'} for j in jobs)
    historic_profile = any(j.profile != g.pipeline_profile() for j in retained)
    incomplete_manifest = any(j.status in {'failed', 'superseded'} and not j.manifest_json for j in retained)
    pending = pending or historic_profile or incomplete_manifest  # Ambiguous historical dependencies: preserve all caches.
    if not pending:
        parsed, revisions, inputs = set(), set(), set()
        legacy = json.loads(Path(g.__file__).with_name('legacy_document_ids.json').read_text())
        profile = parser_profile(legacy)
        for job in retained:
            snapshot = read_export(_snapshot_path(job))  # authoritative bytes; cache_dir=None
            if snapshot.snapshot_hash != job.snapshot_hash or snapshot.commit != job.commit_sha:
                raise ValueError('Retained snapshot differs from SQL target')
            source_hash = g._hash(job.source_id)
            for raw_hash in snapshot.raw_hashes.values():
                key = _digest({'version': CACHE_VERSION, 'stage': 'parsed',
                               'identity': {'raw_hash': raw_hash, 'profile': profile}})
                parsed.add(f'stages/{source_hash}/parsed/{key}.json')
            revisions.update((doc_id, item['revision']) for doc_id, item in g._manifest(job).items())
            try:
                store = _client().get_collection(job.collection)
            except NotFoundError:
                if job.status == 'failed':
                    continue
                raise
            got = store.get(include=['documents', 'metadatas'])
            for text, meta in zip(got['documents'], got['metadatas'], strict=True):
                if meta.get('chunk_type') == 'child':
                    inputs.add(hashlib.sha256(g.embedder._clean_text(text).encode()).hexdigest())
        for path in _files('stages', r'[a-f0-9]{64}/(parsed|chunks)/[a-f0-9]{64}\.json'):
            relative = str(path.relative_to(root()))
            live = relative in parsed
            if path.parent.name == 'chunks':
                value = _read(path)
                # Damage is ambiguous; protect it instead of deleting based on guessed identity.
                payload = value.get('payload')
                if not isinstance(payload, list) or not payload:
                    continue
                try:
                    live = any((c['metadata']['doc_id'], c['metadata']['doc_revision']) in revisions for c in payload)
                except (KeyError, TypeError):
                    continue
            if not live and old(path):
                caches.append(path)
        for path in _files('embeddings', r'[a-f0-9]{64}/[a-f0-9]{64}\.json'):
            if path.stem not in inputs and old(path):
                caches.append(path)
    files = sorted({*snapshots, *caches})
    segment_bytes = 0
    for job in candidates:
        try:
            col = _client().get_collection(job.collection)
        except NotFoundError:
            if job.status != 'failed':
                raise ValueError('A completed generation has lost its collection') from None
            continue
        captured = segments.capture(settings.KB_VECTOR_DIR, col.id)
        segment_bytes += sum(f['bytes'] for seg in captured['segments'] for f in (seg['files'] or []))
    return {'version': 1, 'scope': 'offline single-host Git knowledge retention',
            'reference_fence': _reference_fence(sources, jobs, selected),
            'safety_days': DAYS, 'protected_jobs': reasons, 'cache_sweep_deferred': pending, 'historical_profile_protected': historic_profile,
            'incomplete_manifest_protected': incomplete_manifest,
            'generations': [{'id': j.id, 'collection': j.collection, 'status': j.status,
                             'identity': _identity(j)} for j in candidates],
            'files': [{'path': str(p.relative_to(root())), 'sha256': _sha(p), 'bytes': p.stat().st_size} for p in files],
            'file_bytes': sum(p.stat().st_size for p in files), 'physical_segment_bytes': segment_bytes,
            'unknown_collections_preserved': sorted(c.name for c in _client().list_collections()
                                                   if c.name not in {j.collection for j in jobs})}


def _journal(ticket):
    if not TICKET.fullmatch(ticket):
        raise ValueError('Invalid retention ticket')
    path = _path(f'quarantine/{ticket}/journal.json')
    journal = _read(path)
    if journal.get('ticket') != ticket or journal.get('version') != 1:
        raise ValueError('Invalid retention journal')
    for item in journal['files']:
        # Journal can only name recognized, disposable cache/snapshot locations.
        if not re.fullmatch(r'(snapshots/[a-f0-9]{64}|embeddings/[a-f0-9]{64}/[a-f0-9]{64}|stages/[a-f0-9]{64}/(parsed|chunks)/[a-f0-9]{64})\.json', item['path']):
            raise ValueError('Invalid journal artifact path')
        _path(item['path'])
    for item in journal['generations']:
        if item.get('segment_record') is not None:
            segments._record(item['segment_record'])
        if not TICKET.fullmatch(item['id']) or item['collection'] != f"kb_gen_{item['id']}" or item['status'] not in TERMINAL:
            raise ValueError('Invalid journal generation')
    return path, journal


def _save(path, journal, phase):
    journal['phase'] = phase
    g._atomic_json(path, journal)


def _reference_fence(sources, jobs, selected):
    return g._hash({'sources': sorted((s.id, s.active_job, s.previous_job, s.desired_seq) for s in sources),
                    'jobs': sorted((j.id, j.status, _identity(j)) for j in jobs if j.id not in selected)})


def _identity(job):
    return {key: getattr(job, key) for key in ('source_id', 'collection', 'snapshot_hash', 'profile', 'commit_sha', 'snapshot_path', 'seq')}


def _archive_path(ticket, item):
    return _path(f"quarantine/{ticket}/{item['id']}.json")


def _assert_unpinned(journal, *, allowed_statuses=None, check_fence=False, restore_only=False):
    sources, jobs = _rows()
    protected = _pins(sources, jobs, g._now())
    selected = {j['id'] for j in journal['generations']}
    if check_fence and _reference_fence(sources, jobs, selected) != journal['reference_fence']:
        raise ValueError('References changed after interruption; restore ticket and create a fresh plan')
    by_id = {j.id: j for j in jobs}
    for item in journal['generations']:
        if item['id'] not in by_id or _identity(by_id[item['id']]) != item['identity']:
            raise ValueError('SQL generation identity differs from journal')
        allowed = allowed_statuses or {item['status'], 'quarantined'}
        if by_id[item['id']].status not in allowed:
            raise ValueError('SQL generation state conflicts with retention operation')
    if selected & protected.keys():
        raise ValueError('Retention target is now protected')
    if restore_only:
        return  # Adding identical recovery bytes cannot invalidate a new task's references.
    # Never move/delete a snapshot or cache which a newly queued task may need.
    if any(j.status in PENDING for j in jobs) and journal['files']:
        raise ValueError('Pending tasks prevent resuming file cleanup')
    retained_paths = {_snapshot_path(j) for j in jobs if j.id not in selected and j.status not in {'quarantined', 'collected'}}
    if any(_path(i['path']) in retained_paths for i in journal['files']):
        raise ValueError('A retained task now references a selected snapshot')


def apply():
    """Fresh plan is recomputed; callers must hold exclusive service + publication guards."""
    journal = plan()
    if not journal['generations'] and not journal['files']:
        return {**journal, 'phase': 'nothing_to_collect'}
    ticket = uuid.uuid4().hex
    # Prepare all exports before any source/SQL mutation. A failed preparation is harmless.
    for item in journal['generations']:
        try:
            collection = _client().get_collection(item['collection'])
        except NotFoundError:
            if item['status'] != 'failed':
                raise ValueError('A completed generation has lost its collection') from None
            item['segment_record'] = None
            item['archive'] = None  # failed jobs may never have written a collection
            continue
        item['collection_uuid'] = str(collection.id)
        data = collection.get(include=['documents', 'metadatas', 'embeddings'])
        value = {'collection': item['collection'], 'metadata': collection.metadata,
                 'configuration': {'hnsw': collection.configuration['hnsw']},
                 'ids': data['ids'], 'documents': data['documents'], 'metadatas': data['metadatas'],
                 'embeddings': [list(map(float, v)) for v in data['embeddings']]}
        raw = g._json(value).encode()
        if len(raw) > MAX_ARCHIVE_BYTES:
            raise ValueError('Generation archive exceeds offline export budget')
        archive = _archive_path(ticket, item)
        g._atomic_json(archive, value)
        item['archive'] = _sha(archive)
    _stop_chroma()
    for item in journal['generations']:
        if item['archive']:
            item['segment_record'] = segments.capture(settings.KB_VECTOR_DIR, item['collection_uuid'])
    journal.update(ticket=ticket, created_at=g._now().isoformat(), phase='prepared')
    path = _path(f'quarantine/{ticket}/journal.json')
    g._atomic_json(path, journal)
    return _finish(path, journal)


def _finish(path, journal):
    _assert_unpinned(journal, check_fence=True)
    # Check all recovery bytes before retiring a row/collection.
    for item in journal['generations']:
        if item['archive'] and _sha(_archive_path(journal['ticket'], item)) != item['archive']:
            raise ValueError('Generation archive checksum mismatch')
    for item in journal['files']:
        src = _path(item['path'])
        dst = _path(f"quarantine/{journal['ticket']}/files/{item['path']}")
        if _sha(dst if dst.exists() else src) != item['sha256']:
            raise ValueError('Selected artifact changed; cleanup refused')
        if src.exists() and _sha(src) != item['sha256']:
            raise ValueError('Live artifact changed after interruption')
    _save(path, journal, 'moving')
    with db_module.SessionLocal.begin() as db:
        for item in journal['generations']:
            job = db.get(KbSyncJob, item['id'])
            if job is None or job.status not in {item['status'], 'quarantined'}:
                raise ValueError('SQL generation changed during retention')
            # Preserve updated_at for safety-window and resumability, not a new sync event.
            db.query(KbSyncJob).filter_by(id=job.id).update({'status': 'quarantined', 'updated_at': job.updated_at})
    for item in journal['generations']:
        with suppress(NotFoundError):
            _client().delete_collection(item['collection'])
    _stop_chroma()
    for item in journal['generations']:
        if item.get('segment_record'):
            segments.move(settings.KB_VECTOR_DIR, _segment_dir(journal['ticket']), item['segment_record'])
    for item in journal['files']:
        src = _path(item['path'])
        dst = _path(f"quarantine/{journal['ticket']}/files/{item['path']}")
        if src.exists():
            dst.parent.mkdir(parents=True, exist_ok=True)
            src.rename(dst)
            _sync_dir(src.parent)
            _sync_dir(dst.parent)
    journal['quarantined_at'] = g._now().isoformat()
    _save(path, journal, 'quarantined')
    return journal


def _same_rows(expected, actual, *, subset=False):
    if (not subset and set(expected) != set(actual)) or set(actual) - set(expected):
        return False
    # Chroma cosine upsert may renormalize float32 values during a round trip.
    # Text/metadata remain exact; dimension/nonfinite values and material drift fail.
    for cid, row in actual.items():
        original = expected[cid]
        if row[:2] != original[:2] or len(row[2]) != len(original[2]):
            return False
        if not all(math.isfinite(a) and math.isclose(a, b, rel_tol=1e-6, abs_tol=1e-7)
                   for a, b in zip(row[2], original[2], strict=True)):
            return False
    return True


def restore(ticket):
    path, journal = _journal(ticket)
    if journal['phase'] == 'restored':
        return _discard_restored(journal)
    if journal['phase'] not in {'quarantined', 'moving', 'prepared', 'restoring'}:
        raise ValueError('Ticket is not restorable')
    _assert_unpinned(journal, restore_only=True)
    for item in journal['generations']:
        if item['archive'] and _sha(_archive_path(ticket, item)) != item['archive']:
            raise ValueError('Generation archive checksum mismatch')
    for item in journal['files']:
        src = _path(f"quarantine/{ticket}/files/{item['path']}")
        dst = _path(item['path'])
        if _sha(src if src.exists() else dst) != item['sha256']:
            raise ValueError('Quarantine artifact checksum mismatch')
        if dst.exists() and _sha(dst) != item['sha256']:
            raise ValueError('Restore would overwrite a changed live artifact')
    _save(path, journal, 'restoring')
    for item in journal['files']:
        src = _path(f"quarantine/{ticket}/files/{item['path']}")
        dst = _path(item['path'])
        if not dst.exists():
            dst.parent.mkdir(parents=True, exist_ok=True)
            temporary = dst.with_name(f'.{dst.name}.restore-{ticket}')
            _regular_tree(temporary)
            shutil.copyfile(src, temporary)
            with temporary.open('rb') as handle:
                os.fsync(handle.fileno())
            if _sha(temporary) != item['sha256']:
                raise ValueError('Restore copy checksum mismatch')
            temporary.replace(dst)
            _sync_dir(dst.parent)
    for item in journal['generations']:
        if item['archive']:
            value = _read(_archive_path(ticket, item))
            try:
                col = _client().get_collection(item['collection'])
                existing = col.get(include=['documents', 'metadatas', 'embeddings'])
                expected = {cid: (text, meta, vec) for cid, text, meta, vec in zip(
                    value['ids'], value['documents'], value['metadatas'], value['embeddings'], strict=True)}
                actual = {cid: (text, meta, list(map(float, vec))) for cid, text, meta, vec in zip(
                    existing['ids'], existing['documents'], existing['metadatas'], existing['embeddings'], strict=True)}
                if (col.metadata != value['metadata'] or {'hnsw': col.configuration['hnsw']} != value['configuration']
                        or not _same_rows(expected, actual, subset=journal['phase'] == 'restoring')):
                    raise ValueError('Restore would overwrite a different collection')
            except NotFoundError:
                col = _client().create_collection(item['collection'], metadata=value['metadata'],
                                                  configuration=value['configuration'], embedding_function=None)
            batch_size = min(_client().get_max_batch_size(), 1000)
            for start in range(0, len(value['ids']), batch_size):
                end = start + batch_size
                col.upsert(ids=value['ids'][start:end], documents=value['documents'][start:end],
                           metadatas=value['metadatas'][start:end], embeddings=value['embeddings'][start:end])
            got = col.get(include=['documents', 'metadatas', 'embeddings'])
            expected = {cid: (text, meta, vec) for cid, text, meta, vec in zip(
                value['ids'], value['documents'], value['metadatas'], value['embeddings'], strict=True)}
            actual = {cid: (text, meta, list(map(float, vec))) for cid, text, meta, vec in zip(
                got['ids'], got['documents'], got['metadatas'], got['embeddings'], strict=True)}
            if not _same_rows(expected, actual):
                raise ValueError('Restored collection differs from archive')
    with db_module.SessionLocal.begin() as db:
        for item in journal['generations']:
            job = db.get(KbSyncJob, item['id'])
            if job is None or job.status not in {'quarantined', item['status']}:
                raise ValueError('Restore SQL state conflict')
            db.query(KbSyncJob).filter_by(id=job.id).update({'status': item['status'], 'updated_at': job.updated_at})
    _save(path, journal, 'restored')
    _stop_chroma()
    for item in journal['generations']:
        if item.get('segment_record') and not segments.collection_present(settings.KB_VECTOR_DIR, item['segment_record']):
            segments.move(settings.KB_VECTOR_DIR, _segment_dir(ticket), item['segment_record'])
    return _discard_restored(journal)  # never modify active/previous/desired_seq/deny


def _discard_restored(journal):
    # Successful restore has verified and durably published all copies. Keep only audit.
    for item in journal['generations']:
        if item.get('segment_record'):
            segments.purge(_segment_dir(journal['ticket']), item['segment_record'], resume=True)
        _archive_path(journal['ticket'], item).unlink(missing_ok=True)
    for item in journal['files']:
        _path(f"quarantine/{journal['ticket']}/files/{item['path']}").unlink(missing_ok=True)
    return journal


def purge(ticket):
    path, journal = _journal(ticket)
    if journal['phase'] not in {'quarantined', 'purging'}:
        raise ValueError('Only quarantined tickets can be purged')
    from datetime import datetime
    if datetime.fromisoformat(journal['quarantined_at']) > g._now() - timedelta(days=DAYS):
        raise ValueError('Quarantine grace period has not elapsed')
    _assert_unpinned(journal, allowed_statuses={'quarantined', 'collected'})
    # Validate every existing recovery byte before starting an irreversible batch.
    for item in journal['generations']:
        archive = _archive_path(ticket, item)
        if item['archive'] and not archive.exists() and journal['phase'] != 'purging':
            raise ValueError('Missing generation recovery archive')
        if archive.exists() and _sha(archive) != item['archive']:
            raise ValueError('Generation archive checksum mismatch')
    for item in journal['files']:
        archived = _path(f"quarantine/{ticket}/files/{item['path']}")
        if not archived.exists() and journal['phase'] != 'purging':
            raise ValueError('Missing quarantine recovery artifact')
        if archived.exists() and _sha(archived) != item['sha256']:
            raise ValueError('Quarantine artifact checksum mismatch')
    for item in journal['generations']:
        if item.get('segment_record'):
            segments.validate_quarantine(_segment_dir(ticket), item['segment_record'], resume=journal['phase'] == 'purging')
    was_purging = journal['phase'] == 'purging'
    _save(path, journal, 'purging')
    for item in journal['generations']:
        if item.get('segment_record'):
            segments.purge(_segment_dir(ticket), item['segment_record'], resume=was_purging)
        _archive_path(ticket, item).unlink(missing_ok=True)
    for item in journal['files']:
        _path(f"quarantine/{ticket}/files/{item['path']}").unlink(missing_ok=True)
    with db_module.SessionLocal.begin() as db:
        for item in journal['generations']:
            job = db.get(KbSyncJob, item['id'])
            if job is None or job.status not in {'quarantined', 'collected'}:
                raise ValueError('Purge SQL state conflict')
            db.query(KbSyncJob).filter_by(id=job.id).update({'status': 'collected', 'updated_at': job.updated_at})
    _sync_dir(path.parent)
    _save(path, journal, 'purged')
    return journal


def resume(ticket):
    path, journal = _journal(ticket)
    if journal['phase'] in {'prepared', 'moving'}:
        return _finish(path, journal)
    if journal['phase'] == 'restoring':
        return restore(ticket)
    if journal['phase'] == 'purging':
        return purge(ticket)
    raise ValueError('Ticket has no interrupted operation')


def purge_expired(*, execute=False):
    results = {'eligible': [], 'protected': {}, 'purged': []}
    base = _path('quarantine')
    if not base.exists():
        return results
    from datetime import datetime
    for entry in sorted(base.iterdir()):
        _regular_tree(entry)
        if not entry.is_dir() or not TICKET.fullmatch(entry.name):
            continue
        try:
            _, journal = _journal(entry.name)
            if journal['phase'] not in {'quarantined', 'purging'}:
                continue
            if datetime.fromisoformat(journal['quarantined_at']) > g._now() - timedelta(days=DAYS):
                results['protected'][entry.name] = 'quarantine_grace_period'
                continue
            _assert_unpinned(journal, allowed_statuses={'quarantined', 'collected'})
            results['eligible'].append(entry.name)
            if execute:
                purge(entry.name)
                results['purged'].append(entry.name)
        except (ValueError, OSError, KeyError, TypeError):
            results['protected'][entry.name] = 'invalid_or_referenced_ticket'
    return results


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description='Git知识离线保护式清理；默认仅预览，至少保留7天')
    action = parser.add_mutually_exclusive_group()
    parser.add_argument('--apply', action='store_true', help='执行隔离或到期清除；默认只预览')
    action.add_argument('--purge-expired', action='store_true')
    action.add_argument('--restore')
    action.add_argument('--purge')
    action.add_argument('--resume')
    args = parser.parse_args()
    if (args.restore or args.purge or args.resume) and not args.apply:
        parser.error('--restore/--purge/--resume require explicit --apply')
    mutate = args.apply
    # Preview may run online; exclusive mutation requires backend lifetime locks released.
    with service_guard(exclusive=bool(mutate)), publication_lock():
        if args.purge_expired:
            result = purge_expired(execute=args.apply)
        elif args.restore:
            result = restore(args.restore)
        elif args.purge:
            result = purge(args.purge)
        elif args.resume:
            result = resume(args.resume)
        elif args.apply:
            result = apply()
        else:
            result = plan()
    print(g._json(result))


if __name__ == '__main__':
    main()
