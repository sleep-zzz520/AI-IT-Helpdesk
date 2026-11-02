"""Real MySQL/Chroma retention and SIGKILL drill, isolated tables/temp directory.

Uses deterministic 1024-D vectors; no model quality or production latency claim.
"""
import argparse
import hashlib
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import UTC, timedelta
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app import db
from app.config import BASE_DIR, settings
from app.models import KbSource, KbSyncJob
from app.rag import embedder
from app.rag import generations as g
from app.rag import retention as r
from app.rag.git_source import read_snapshot, read_snapshot_from_files
from app.rag.maintenance import service_guard
from app.rag.retriever import RetrievalStats, retrieve
from app.rag.sync import publication_lock


def deterministic(texts):
    return [[(v + 1) / 256 for v in hashlib.shake_256(t.encode()).digest(1024)] for t in texts]


def _version():
    with db.engine.connect() as connection:
        return connection.execute(text('SELECT VERSION()')).scalar()


def configure(root, schema):
    original = db.engine
    db.engine = create_engine(original.url, pool_pre_ping=True)
    # Use separate randomized tables: the ordinary app identity cannot CREATE DATABASE.
    suffix = schema.removeprefix('helpdesk_retention_')
    KbSource.__table__.name = 'kb_retention_sources_' + suffix
    KbSyncJob.__table__.name = 'kb_retention_jobs_' + suffix
    db.SessionLocal = sessionmaker(bind=db.engine, autoflush=False, expire_on_commit=False)
    original.dispose()
    settings.KB_SOURCE_ID = 'retention-evaluation'
    settings.KB_SYNC_MODE = 'git'
    settings.KB_VECTOR_DIR = str(root / 'vectors')
    settings.KB_SYNC_STATE_DIR = str(root / 'state')
    settings.KB_GIT_EXPORT = ''
    settings.KB_PIPELINE_EPOCH = 'retention-evaluation-1024d'
    settings.RERANK_ENABLED = False
    settings.VECTOR_MIN_SCORE = 0.0
    embedder.EMBED_DIM = 1024
    embedder.embed_texts = deterministic


def footprint(root):
    return {name: {'files': len(files), 'bytes': sum(p.stat().st_size for p in files),
                   'hnsw_bytes': sum(p.stat().st_size for p in files if p.suffix in {'.bin', '.pickle'}),
                   'sqlite_bytes': sum(p.stat().st_size for p in files if p.name == 'chroma.sqlite3')}
            for name in ('state', 'vectors')
            for files in [[p for p in (root / name).rglob('*') if p.is_file()]]}


def signature(collection):
    col = r._client().get_collection(collection)
    data = col.get(include=['documents', 'metadatas', 'embeddings'])
    rows = {cid: [t, m, list(map(float, v))] for cid, t, m, v in zip(
        data['ids'], data['documents'], data['metadatas'], data['embeddings'], strict=True)}
    return {'content_metadata_config_sha256': g._hash({'rows': {k: v[:2] for k,v in rows.items()},
                                                         'metadata': col.metadata, 'hnsw': col.configuration['hnsw']}),
            'vectors': {k: v[2] for k,v in rows.items()}}


def inspection():
    stats = RetrievalStats()
    hits = retrieve('VPN 证书过期 certutil', scenario='vpn', top_k=5,
                    query_vector=deterministic(['VPN 证书过期 certutil'])[0], stats=stats)
    assert hits and stats.vector_recall and stats.bm25_recall
    assert all(h.parent_text for h in hits)
    return {'active': g.state()['active_generation'], 'documents': g.state()['doc_count'],
            'vector_recall': stats.vector_recall, 'bm25_recall': stats.bm25_recall,
            'parents_verified': True, 'signature': signature(g.state()['active_collection'])}


def stage(args):
    root = args.root
    configure(root, args.schema)
    evidence = root / 'evidence.json'
    if args.stage == 'setup':
        for table in (KbSource.__table__, KbSyncJob.__table__):
            table.create(db.engine)
        original = read_snapshot(BASE_DIR)
        versions = []
        for i in range(6):
            files = dict(original.files)
            files['vpn/cert-renewal.md'] += f'\n## 清理验收\n\n隔离测试版本 retention-marker-{i}。\n'.encode()
            job = g.enqueue(read_snapshot_from_files(f'{i+1:040x}', files))
            job = g.process_job(job['id'])
            assert job['status'] == 'prepared', job
            g.publish(job['id'])
            versions.append(job)
        old = g._now() - timedelta(days=15)
        with db.SessionLocal.begin() as session:
            session.query(KbSyncJob).update({'created_at': old, 'updated_at': old})
        for p in (root / 'state').rglob('*'):
            if p.is_file():
                os.utime(p, (old.replace(tzinfo=UTC).timestamp(),) * 2)
        baseline = inspection()
        with service_guard(exclusive=True), publication_lock():
            planned = r.plan()
        assert len(planned['generations']) == 4
        data = {'scope': 'isolated real MySQL tables/Chroma; 16 Git Markdown documents and synthetic revisions',
                'mysql_version': _version(),
                'embedding': 'deterministic 1024-D; no external requests', 'baseline': baseline,
                'generations_before': 6, 'candidates': 4, 'storage_before': footprint(root),
                'selected_files': len(planned['files']), 'selected_file_bytes': planned['file_bytes'],
                'signatures': {j['collection']: signature(j['collection']) for j in versions}}
        g._atomic_json(evidence, data)
    elif args.stage == 'kill':
        client_type = type(r._client())
        original_delete = client_type.delete_collection
        def crash(client, name):
            original_delete(client, name)
            os.kill(os.getpid(), signal.SIGKILL)
        client_type.delete_collection = crash
        with service_guard(exclusive=True), publication_lock():
            r.apply()
        raise AssertionError('SIGKILL injection not reached')
    else:
        data = json.loads(evidence.read_text())
        # After interruption only offline maintenance may proceed; normal read pins survived.
        assert inspection() == data['baseline']
        ticket = next((root / 'state' / 'quarantine').iterdir()).name
        def forbid(*_):
            raise AssertionError('Retention must not call embedding service')
        embedder.embed_texts = forbid
        with service_guard(exclusive=True), publication_lock():
            journal = r.resume(ticket)
            assert journal['phase'] == 'quarantined'
            data['generations_after_quarantine'] = len(r._client().list_collections())
            assert data['generations_after_quarantine'] == 2
            r.restore(ticket)
            max_delta = 0.0
            for name, original in data['signatures'].items():
                actual = signature(name)
                assert actual['content_metadata_config_sha256'] == original['content_metadata_config_sha256']
                assert actual['vectors'].keys() == original['vectors'].keys()
                for cid, vector in original['vectors'].items():
                    for a, b in zip(vector, actual['vectors'][cid], strict=True):
                        max_delta = max(max_delta, abs(a-b))
                        assert abs(a-b) <= 1e-7 + 1e-6 * abs(a)
            data['restored_vector_max_abs_delta'] = max_delta
            old = g._now() - timedelta(days=15)
            for p in (root / 'state').rglob('*'):
                if p.is_file() and 'quarantine' not in p.parts:
                    os.utime(p, (old.replace(tzinfo=UTC).timestamp(),) * 2)
            journal = r.apply()
            # Advance only the drill clock to exercise the seven-day expiry policy.
            data['purge_ticket'] = journal['ticket']
        data.update(sigkill_cleanup_recovered=True, restore_content_metadata_config_exact=True, zero_new_embeddings=True)
        assert inspection() == data['baseline']
        g._atomic_json(evidence, data)
    db.engine.dispose()


def finish(root, schema):
    configure(root, schema)
    data = json.loads((root / 'evidence.json').read_text())
    now = g._now() + timedelta(days=8)
    g._now = lambda: now
    with service_guard(exclusive=True), publication_lock():
        purged = r.purge_expired(execute=True)
        assert purged['purged'] == [data['purge_ticket']]
        assert inspection() == data['baseline']
    assert g.rollback()['id'] != data['baseline']['active']
    inspection()
    data.update(expired_purge_passed=True, active_and_rollback_hybrid_passed=True,
                generations_after_purge=len(r._client().list_collections()), storage_after_purge=footprint(root),
                remaining_quarantine_payload_bytes=sum(p.stat().st_size for p in (root / 'state' / 'quarantine').rglob('*')
                                                       if p.is_file() and p.name != 'journal.json'))
    assert data['remaining_quarantine_payload_bytes'] == 0
    return data


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=Path('tests/quality_reports/retention_real.json'))
    parser.add_argument('--stage', choices=['setup', 'kill', 'recover'])
    parser.add_argument('--root', type=Path)
    parser.add_argument('--schema')
    args = parser.parse_args()
    if args.stage:
        if not args.schema or not args.schema.startswith('helpdesk_retention_') or len(args.schema) != 51:
            raise ValueError('Invalid isolated schema')
        stage(args)
        return
    schema = 'helpdesk_retention_' + uuid.uuid4().hex
    admin = db.engine
    start = time.perf_counter()
    try:
        with tempfile.TemporaryDirectory(prefix='retention-eval-') as directory:
            root = Path(directory).resolve()
            for phase in ('setup', 'kill', 'recover'):
                result = subprocess.run([sys.executable, __file__, '--stage', phase, '--root', str(root), '--schema', schema],
                                        timeout=120, capture_output=True, text=True)
                if phase == 'kill':
                    assert result.returncode == -signal.SIGKILL, result.stderr[-1500:]
                else:
                    assert result.returncode == 0, result.stderr[-2500:]
            data = finish(root, schema)
            data['elapsed_ms'] = round((time.perf_counter() - start) * 1000, 2)
            from chromadb.api.shared_system_client import SharedSystemClient
            for system in tuple(SharedSystemClient._identifier_to_system.values()):
                system.stop()
            SharedSystemClient.clear_system_cache()
            db.engine.dispose()
    finally:
        with admin.begin() as conn:
            suffix = schema.removeprefix('helpdesk_retention_')
            for prefix in ('kb_retention_jobs_', 'kb_retention_sources_'):
                conn.execute(text(f'DROP TABLE IF EXISTS `{prefix}{suffix}`'))
        admin.dispose()
    data['isolated_tables_and_directory_removed'] = True
    data['baseline']['signature'] = data['baseline']['signature']['content_metadata_config_sha256']
    data['signatures'] = {k: v['content_metadata_config_sha256'] for k,v in data['signatures'].items()}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(data, ensure_ascii=False, indent=2))
    print(json.dumps({k: v for k, v in data.items() if k != 'signatures'}, ensure_ascii=False))


if __name__ == '__main__':
    main()
