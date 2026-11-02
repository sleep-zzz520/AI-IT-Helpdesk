"""Isolated, version-selected SQLite/Chroma sync benchmark child process.

No application imports occur until paths and offline environment are configured.
The parent owns datasets, repeated runs, timeouts and archival of source versions.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import math
import os
import platform
import re
import resource
import sys
import time
import traceback
import uuid
from datetime import date, datetime
from pathlib import Path


def logical_bytes(root):
    return sum(p.stat().st_size for p in root.rglob('*') if p.is_file())


def peak_rss_bytes():
    value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return int(value if sys.platform == 'darwin' else value * 1024)


def expected_documents(files):
    # Independent of the implementation parser and splitter under measurement.
    import yaml
    result = {}
    for path, text in files.items():
        lines = text.splitlines(keepends=True)
        assert lines and lines[0].strip() == '---', path
        end = next(i for i in range(1, len(lines)) if lines[i].strip() == '---')
        meta = yaml.safe_load(''.join(lines[1:end]))
        identity = str(uuid.UUID(str(meta['document_id'])))
        assert identity not in result, identity
        result[identity] = {'meta': meta, 'path': path, 'body': ''.join(lines[end + 1:]).lstrip('\r\n')}
    return result


def deterministic_vector(text):
    raw = hashlib.shake_256(text.encode()).digest(2048)
    return [(int.from_bytes(raw[i:i + 2], 'big') + 1) / 65536 for i in range(0, 2048, 2)]


def scalar_metadata(meta):
    result = {key: value.isoformat() if isinstance(value, (date, datetime)) else value for key, value in meta.items()}
    result.pop('source_url', None)  # source path is assigned by the ingestion pipeline
    if 'document_id' in result:
        result['document_id'] = str(uuid.UUID(str(result['document_id'])))
    for key in ('tags', 'error_codes'):
        if isinstance(result.get(key), (list, tuple)):
            result[key] = ','.join(str(v) for v in result[key])
    for key in ('valid_to', 'valid_from'):
        value = result.get(key)
        if value is not None:
            digits = str(value)[:10].replace('-', '')
            if isinstance(value, (int, float)):
                result[key] = int(value)
            elif len(str(value)) >= 10 and digits.isdigit():
                result[key] = int(digits)
            else:
                result.pop(key, None)
    return {k: v for k, v in result.items() if isinstance(v, (str, int, float, bool))}


def verify(db, g, settings, ChromaStore, expected, job_id):
    from app.models import KbSource, KbSyncJob
    with db.SessionLocal() as session:
        source = session.get(KbSource, settings.KB_SOURCE_ID)
        assert source.active_job == job_id
        job = session.get(KbSyncJob, job_id)
        assert job.status == 'published'
        manifest = json.loads(job.manifest_json)
        documents = manifest['documents']
        canonical = json.dumps(documents, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
        assert manifest['digest'] == hashlib.sha256(canonical.encode()).hexdigest()
        assert set(documents) == set(expected)
        store = ChromaStore(settings.KB_VECTOR_DIR, job.collection, must_exist=True)
    raw = store._col.get(include=['documents', 'metadatas', 'embeddings'])
    ids = raw['ids']
    assert len(ids) == len(set(ids))
    assert set(ids) == {cid for doc in documents.values() for cid in doc['ids']}
    rows = {cid: (text, meta, vector) for cid, text, meta, vector in
            zip(ids, raw['documents'], raw['metadatas'], raw['embeddings'], strict=True)}
    from app.rag import embedder
    parents = {}
    child_texts = {identity: [] for identity in expected}
    children = 0
    covered_lines = 0
    for cid, (text, meta, vector) in rows.items():
        identity = meta['doc_id']
        assert identity in expected
        for key, value in scalar_metadata(expected[identity]['meta']).items():
            assert meta.get(key) == value, (identity, key, meta.get(key), value)
        assert meta['source_id'] == settings.KB_SOURCE_ID
        assert meta['source_url'] == f"{settings.KB_GIT_SUBDIR}/{expected[identity]['path']}"
        assert len(vector) == 1024 and all(math.isfinite(float(v)) for v in vector)
        assert cid in documents[identity]['ids']
        assert documents[identity]['path'] == expected[identity]['path']
        if meta['chunk_type'] == 'parent':
            assert identity not in parents
            assert text == expected[identity]['body']
            assert all(float(v) == 0 for v in vector), ('parent vector', cid)
            parents[identity] = cid
        else:
            assert meta['chunk_type'] == 'child'
            children += 1
            assert text and text in expected[identity]['body']
            child_texts[identity].append((int(meta.get('seq', 0)), int(cid.rsplit(':', 1)[-1]), text))
            wanted = deterministic_vector(embedder._clean_text(text))
            norm = math.sqrt(sum(v * v for v in wanted))
            actual_norm = math.sqrt(sum(float(v) ** 2 for v in vector))
            assert actual_norm > 0
            assert all(abs(float(a) / actual_norm - b / norm) < 2e-6
                       for a, b in zip(vector, wanted, strict=True)), ('child vector', cid)
    assert set(parents) == set(expected)
    for _text, meta, _ in rows.values():
        if meta['chunk_type'] == 'child':
            assert meta['parent_id'] == parents[meta['doc_id']]
    characters = 0
    for identity, document in expected.items():
        combined = ''.join(text for _, _, text in sorted(child_texts[identity]))
        body = document['body']
        headings = list(re.finditer(r'^##\s+[^\n]+$', body, re.MULTILINE))
        if headings:
            introduction = body[:headings[0].start()].strip()
            sections = ([introduction] if len(introduction) >= 60 else [])
            sections += [body[h.start():headings[i + 1].start() if i + 1 < len(headings) else len(body)].strip()
                         for i, h in enumerate(headings)]
            expected_text = ''.join(sections)
        else:
            expected_text = body
        assert combined == expected_text, ('ordered child coverage differs', identity)
        characters += len(expected_text)
        for line in document['body'].splitlines():
            line = line.strip()
            if line and not line.startswith('#'):
                assert line in combined, ('source line missing from children', identity, line[:80])
                covered_lines += 1
    return {'vectors_verified': len(rows), 'nonheading_source_lines_present': covered_lines,
            'ordered_source_characters_verified': characters, 'passed': True,
            'documents': len(parents), 'children': children, 'blocks': len(rows)}


def run(args, report):
    backend = args.backend_root.resolve(strict=True)
    root = args.work_dir.resolve()
    root.mkdir(parents=True, exist_ok=True)
    payload = json.loads(args.input.read_text())
    resume = payload.get('resume_from')
    if any(root.iterdir()) and not resume:
        raise ValueError('work-dir must be empty unless resume_from is provided')
    if resume and (Path(resume).resolve() == root or not (root / 'jobs.sqlite3').is_file()):
        raise ValueError('resume requires an offline copied database in a distinct work-dir')
    os.environ.update(ZHIPU_API_KEY='offline-benchmark-no-network', MONITOR_MODE='mock',
                      ANONYMIZED_TELEMETRY='False',
                      KB_ROOT=str(root / 'knowledge'), KB_VECTOR_DIR=str(root / 'vectors'),
                      KB_SYNC_STATE_DIR=str(root / 'state'), KB_GIT_EXPORT=str(root / 'input-export.json'),
                      KB_GIT_REPO=str(root / 'repo'), KB_SYNC_MODE='git', KB_SOURCE_ID='bench-source')
    sys.path.insert(0, str(backend))
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app import db
    from app.config import settings
    from app.rag import embedder, git_source
    from app.rag import generations as g
    from app.rag.store import ChromaStore
    assert Path(db.__file__).resolve().is_relative_to(backend)
    db.engine.dispose()
    db.engine = create_engine(f"sqlite:///{root / 'jobs.sqlite3'}", connect_args={'check_same_thread': False})
    db.SessionLocal = sessionmaker(bind=db.engine, autoflush=False, expire_on_commit=False)
    for name in ('KB_ROOT', 'KB_VECTOR_DIR', 'KB_SYNC_STATE_DIR', 'KB_GIT_EXPORT', 'KB_GIT_REPO'):
        setattr(settings, name, os.environ[name])
    settings.KB_ACTIVE_FILE = root / 'active.txt'
    settings.KB_SYNC_MODE = 'git'
    settings.KB_SOURCE_ID = 'bench-source'
    settings.KB_GIT_SUBDIR = 'docs/knowledge'
    settings.KB_EXTRA_COLLECTIONS = ''
    settings.KB_PIPELINE_EPOCH = 'isolated-benchmark-v1'
    embedder.EMBED_DIM = 1024
    db.Base.metadata.create_all(db.engine)
    from app.models import KbSource, KbSyncJob
    if resume:
        old_root = Path(resume).resolve()
        with db.SessionLocal.begin() as session:
            for row in session.query(KbSyncJob).all():
                row.snapshot_path = str(root / Path(row.snapshot_path).relative_to(old_root))
    with db.SessionLocal() as session:
        source = session.get(KbSource, settings.KB_SOURCE_ID)
        report['preexisting_generation'] = source.active_job if source else None
    report['preexisting_cache_bytes'] = logical_bytes(root / 'state')

    class ForbiddenClient:
        def __getattr__(self, name):
            raise RuntimeError('Network embedding API forbidden in isolated benchmark')
    embedder._client = ForbiddenClient()
    counts = {}

    def embedding(texts):
        started = time.perf_counter()
        counts['embedding_inputs'] += len(texts)
        counts['embedding_utf8_bytes'] += sum(len(t.encode()) for t in texts)
        vectors = []
        for text in texts:
            vectors.append(deterministic_vector(text))
        counts['embedding_wall_ms'] += (time.perf_counter() - started) * 1000
        return vectors
    profile_mode = getattr(args, 'profile_mode', 'native')
    profile_originals = {name: getattr(g, name) for name in
                         ('pipeline_profile', 'embedding_profile', 'chunk_profile') if hasattr(g, name)}
    # Counter spies below do not replace _clean_text/_build_chunks or disk source.
    # Compare native values before/after spies rather than skipping real hashing.
    profiles = {name: original() for name, original in profile_originals.items()}
    profile_stats = {}
    if profile_mode == 'precomputed':
        for name, value in profiles.items():
            setattr(g, name, lambda value=value: value)
    else:
        def measured_profile(name, original):
            def measured(*a, **kw):
                metric = profile_stats.setdefault(name, {'calls': 0, 'wall_ms': 0.0})
                metric['calls'] += 1
                started = time.perf_counter()
                try:
                    return original(*a, **kw)
                finally:
                    metric['wall_ms'] += (time.perf_counter() - started) * 1000
            return measured
        for name, original in profile_originals.items():
            setattr(g, name, measured_profile(name, original))
    embedder.embed_texts = embedding
    parse_name = '_parse_content' if hasattr(git_source, '_parse_content') else '_parse'
    parse_original = getattr(git_source, parse_name)
    split_original = g.split_markdown
    upsert_original = ChromaStore.upsert_chunks

    def parse(*a, **kw):
        counts['parsed_documents'] += 1
        started = time.perf_counter()
        try:
            return parse_original(*a, **kw)
        finally:
            counts['parse_wall_ms'] += (time.perf_counter() - started) * 1000

    def split(*a, **kw):
        counts['chunked_documents'] += 1
        started = time.perf_counter()
        try:
            return split_original(*a, **kw)
        finally:
            counts['split_wall_ms'] += (time.perf_counter() - started) * 1000

    def upsert(self, chunks, vectors):
        counts['index_upsert_calls'] += 1
        counts['index_upsert_blocks'] += len(chunks)
        started = time.perf_counter()
        try:
            return upsert_original(self, chunks, vectors)
        finally:
            counts['upsert_wall_ms'] += (time.perf_counter() - started) * 1000

    # Preserve the historical precomputed protocol only when explicitly selected.
    if profile_mode == 'precomputed' and hasattr(git_source, 'parser_profile'):
        parser_original = git_source.parser_profile
        identities = {}
        def parser_profile(ids):
            key = json.dumps(ids, sort_keys=True)
            if key not in identities:
                setattr(git_source, parse_name, parse_original)
                try:
                    identities[key] = parser_original(ids)
                finally:
                    setattr(git_source, parse_name, parse)
            return identities[key]
        git_source.parser_profile = parser_profile
    report.update(seed=payload.get('seed'), repeat=payload.get('repeat'), version_label=payload.get('version_label'),
                  profile_mode=profile_mode,
                  profile_scope=('native source fingerprint computation included in timed lifecycle' if profile_mode == 'native' else
                                 'precomputed fingerprints; excludes native profile recomputation overhead; historical protocol'),
                  profile_timing_scope='inclusive nested calls; do not sum wall_ms across profile functions',
                  backend_root=str(backend), embedding='deterministic SHAKE256 1024-dimensional; no network',
                  rss_scope='process cumulative peak, not phase delta',
                  environment={'python': platform.python_version(), 'platform': platform.platform(),
                               'libraries': {name: importlib.metadata.version(name) for name in ('chromadb', 'sqlalchemy', 'openai', 'PyYAML')}}, phases=[])
    try:
        for phase in payload['phases']:
            if phase.get('profile_change'):
                raise ValueError('profile_change is not supported; use unchanged native profiles')
            # Input preparation is outside timers and has no stage-cache arguments.
            setattr(git_source, parse_name, parse_original)
            g.split_markdown = split_original
            snapshot = git_source.read_snapshot_from_files(phase['commit'], {p: t.encode() for p, t in phase['files'].items()})
            git_source.write_export(snapshot, Path(settings.KB_GIT_EXPORT))
            expected = expected_documents(phase['files'])
            counts.clear()
            counts.update({k: 0 for k in ('parsed_documents', 'chunked_documents', 'embedding_inputs', 'embedding_utf8_bytes', 'index_upsert_calls', 'index_upsert_blocks', 'parse_wall_ms', 'split_wall_ms', 'embedding_wall_ms', 'upsert_wall_ms')})
            setattr(git_source, parse_name, parse)
            g.split_markdown = split
            ChromaStore.upsert_chunks = upsert
            if profile_mode == 'native':
                assert {name: original() for name, original in profile_originals.items()} == profiles, 'instrumentation changed native profile identity'
                # Identity checks and input preparation are outside timed statistics.
                profile_stats.clear()
            item = {'name': phase['name'], 'commit': phase['commit']}
            report['phases'].append(item)
            start, cpu = time.perf_counter(), time.process_time()
            tick = start
            job = g.enqueue()
            item['enqueue_ms'] = (time.perf_counter() - tick) * 1000
            tick = time.perf_counter()
            job = g.process_job(job['id'])
            item['process_ms'] = (time.perf_counter() - tick) * 1000
            assert job['status'] == 'prepared', job
            tick = time.perf_counter()
            g.publish(job['id'])
            item['publish_ms'] = (time.perf_counter() - tick) * 1000
            item.update(wall_ms=(time.perf_counter() - start) * 1000,
                        cpu_ms=(time.process_time() - cpu) * 1000, counts=dict(counts),
                        job_report=job.get('report'), peak_rss_bytes=peak_rss_bytes())
            item['profile_metrics'] = {name: dict(metric) for name, metric in profile_stats.items()}
            item['profile_identity_spies_preserved'] = profile_mode == 'native'
            item['oracle'] = verify(db, g, settings, ChromaStore, expected, job['id'])
            item['logical_storage_bytes'] = logical_bytes(root)
    finally:
        db.engine.dispose()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('backend-root', 'input', 'output', 'work-dir'):
        parser.add_argument('--' + name, required=True, type=Path)
    parser.add_argument('--profile-mode', choices=('native', 'precomputed'), default='native')
    args = parser.parse_args()
    report = {'ok': False}
    code = 0
    try:
        run(args, report)
        report['ok'] = True
    except BaseException as exc:
        report['error'] = {'type': type(exc).__name__, 'message': str(exc), 'traceback': traceback.format_exc()}
        code = 1
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    return code


if __name__ == '__main__':
    raise SystemExit(main())
