"""Retention integration tests use actual Chroma, SQL and filesystem artifacts."""
import json
import os
from datetime import UTC, timedelta
from pathlib import Path

import pytest

from app import db as db_module
from app.config import settings
from app.models import KbSource, KbSyncJob
from app.rag import generations as g
from app.rag import retention as r
from app.rag.bm25 import Bm25Index
from app.rag.maintenance import service_guard
from tests.test_generations import A, env, mock_llm, snapshot  # noqa: F401


def age_all():
    old = g._now() - timedelta(days=15)
    with db_module.SessionLocal.begin() as db:
        db.query(KbSyncJob).update({'updated_at': old, 'created_at': old})
    timestamp = old.replace(tzinfo=UTC).timestamp()
    for path in Path(settings.KB_SYNC_STATE_DIR).rglob('*'):
        if path.is_file():
            os.utime(path, (timestamp, timestamp))


def versions(env):
    one = env.publish(snapshot(1))
    two = env.publish(snapshot(2, a='VPN second certificate evidence'))
    three = env.publish(snapshot(3, a='VPN third certificate evidence'))
    age_all()
    return one, two, three


def test_plan_apply_restore_preserves_roots_and_denial(env):
    one, two, three = versions(env)
    with db_module.SessionLocal.begin() as db:
        db.get(KbSource, settings.KB_SOURCE_ID).denied_json = json.dumps([A])
    before = r._client().get_collection(one['collection']).get(include=['documents', 'metadatas', 'embeddings'])
    calls = list(env.calls)
    files = {p: p.read_bytes() for p in Path(settings.KB_SYNC_STATE_DIR).rglob('*') if p.is_file()}
    plan = r.plan()
    assert [j['id'] for j in plan['generations']] == [one['id']]
    assert {two['id'], three['id']} <= plan['protected_jobs'].keys()
    assert files == {p: p.read_bytes() for p in files}
    journal = r.apply()
    assert journal['phase'] == 'quarantined'
    assert g.get_job(one['id'])['status'] == 'quarantined'
    assert one['collection'] not in {c.name for c in r._client().list_collections()}
    assert env.active() == three['id']
    with pytest.raises(RuntimeError, match='不可查询'):
        g.resolve_store(one['collection'])
    with pytest.raises(ValueError, match='grace'):
        r.purge(journal['ticket'])
    assert r.restore(journal['ticket'])['phase'] == 'restored'
    col = r._client().get_collection(one['collection'])
    after = col.get(include=['documents', 'metadatas', 'embeddings'])
    assert after['ids'] == before['ids']
    assert after['documents'] == before['documents']
    assert after['metadatas'] == before['metadatas']
    assert after['embeddings'].tolist() == before['embeddings'].tolist()
    assert col.configuration['hnsw']['space'] == 'cosine'
    assert env.calls == calls
    assert g.denied_ids() == {A} and env.active() == three['id']
    assert all(item['metadata']['doc_id'] != A for item in g.resolve_store(one['collection']).get_all_meta().values())
    assert Bm25Index(g.resolve_store())._ensure_built() is None
    assert g.resolve_store().query([1, 1, 1], 5)
    assert g.rollback()['id'] == two['id']


@pytest.mark.parametrize('status', ['queued', 'running', 'prepared', 'mystery'])
def test_pending_and_unknown_protect_collection_snapshot_and_all_caches(env, status):
    one, _, _ = versions(env)
    with db_module.SessionLocal.begin() as db:
        db.get(KbSyncJob, one['id']).status = status
    plan = r.plan()
    assert not plan['generations']
    assert not plan['files']
    assert plan['cache_sweep_deferred'] is True


def test_shared_snapshot_and_other_source_pointer_are_roots(env):
    one = env.publish(snapshot(1))
    env.patch.setattr(settings, 'KB_PIPELINE_EPOCH', 'test-v2')
    two = env.publish(snapshot(1))
    three = env.publish(snapshot(3))
    age_all()
    plan = r.plan()
    assert one['id'] in {j['id'] for j in plan['generations']}
    with db_module.SessionLocal() as db:
        old_path = db.get(KbSyncJob, one['id']).snapshot_path
        assert old_path == db.get(KbSyncJob, two['id']).snapshot_path
    assert str(Path(old_path).relative_to(r.root())) not in {i['path'] for i in plan['files']}
    with db_module.SessionLocal.begin() as db:
        db.add(KbSource(id='another-source', active_job=one['id'], desired_seq=1))
    assert not r.plan()['generations']
    assert env.active() == three['id']


def test_recent_generation_and_old_historical_profile_caches_protected(env):
    one, _, _ = versions(env)
    with db_module.SessionLocal.begin() as db:
        db.get(KbSyncJob, one['id']).created_at = g._now()
    assert not r.plan()['generations']
    env.patch.setattr(settings, 'KB_PIPELINE_EPOCH', 'unknown-old-profile')
    assert r.plan()['historical_profile_protected'] is True
    assert not [f for f in r.plan()['files'] if f['path'].startswith(('stages/', 'embeddings/'))]


@pytest.mark.parametrize('failure_at', ['delete_collection', 'rename'])
def test_interrupted_cleanup_resumes_from_durable_journal(env, monkeypatch, failure_at):
    one, _, _ = versions(env)
    if failure_at == 'delete_collection':
        client_type = type(r._client())
        original = client_type.delete_collection
        monkeypatch.setattr(client_type, 'delete_collection', lambda *_: (_ for _ in ()).throw(OSError('injected')))
    else:
        original = Path.rename
        monkeypatch.setattr(Path, 'rename', lambda *_: (_ for _ in ()).throw(OSError('injected')))
    with pytest.raises(OSError, match='injected'):
        r.apply()
    ticket = next((r.root() / 'quarantine').iterdir()).name
    assert g.get_job(one['id'])['status'] == 'quarantined'
    with pytest.raises(ValueError, match='resume'):
        r.plan()
    if failure_at == 'delete_collection':
        monkeypatch.setattr(client_type, 'delete_collection', original)
    else:
        monkeypatch.setattr(Path, 'rename', original)
    assert r.resume(ticket)['phase'] == 'quarantined'
    assert r.restore(ticket)['phase'] == 'restored'


def test_archive_corruption_and_sql_identity_change_refuse_restore(env):
    one, _, _ = versions(env)
    journal = r.apply()
    archive = r._archive_path(journal['ticket'], journal['generations'][0])
    original = archive.read_bytes()
    archive.write_text('{}')
    with pytest.raises(ValueError, match='checksum'):
        r.restore(journal['ticket'])
    archive.write_bytes(original)
    with db_module.SessionLocal.begin() as db:
        db.get(KbSyncJob, one['id']).profile = 'changed'
    with pytest.raises(ValueError, match='identity'):
        r.restore(journal['ticket'])


def test_purge_after_grace_keeps_active_and_rollback_queryable(env, monkeypatch):
    one, two, three = versions(env)
    journal = r.apply()
    now = g._now() + timedelta(days=8)
    monkeypatch.setattr(g, '_now', lambda: now)
    assert r.purge(journal['ticket'])['phase'] == 'purged'
    assert g.get_job(one['id'])['status'] == 'collected'
    assert env.active() == three['id']
    assert g.resolve_store().query([1, 1, 1], 4)
    assert g.rollback()['id'] == two['id']


def test_symlink_and_traversal_refused(env, tmp_path):
    versions(env)
    with pytest.raises(ValueError):
        r.restore('../outside')
    target = tmp_path / 'untouched'
    target.write_text('secret')
    (r.root() / 'snapshots' / ('f' * 64 + '.json')).symlink_to(target)
    with pytest.raises(ValueError, match='Symbolic'):
        r.apply()
    assert target.read_text() == 'secret'


def test_lifetime_guard_refuses_gc_and_service_during_maintenance(env):
    with service_guard():
        with pytest.raises(RuntimeError, match='停止'):
            with service_guard(exclusive=True):
                pass
    with service_guard(exclusive=True):
        with pytest.raises(RuntimeError):
            with service_guard():
                pass


def test_partial_restore_copy_is_atomic_and_resumable(env, monkeypatch):
    versions(env)
    journal = r.apply()
    original = r.shutil.copyfile

    def partial(src, dst):
        Path(dst).write_bytes(b'partial')
        raise OSError('partial copy')

    monkeypatch.setattr(r.shutil, 'copyfile', partial)
    with pytest.raises(OSError, match='partial copy'):
        r.restore(journal['ticket'])
    for item in journal['files']:
        assert not r._path(item['path']).exists()
    monkeypatch.setattr(r.shutil, 'copyfile', original)
    assert r.resume(journal['ticket'])['phase'] == 'restored'


def test_purge_state_conflict_does_not_delete_archives(env, monkeypatch):
    one, _, _ = versions(env)
    journal = r.apply()
    archive = r._archive_path(journal['ticket'], journal['generations'][0])
    with db_module.SessionLocal.begin() as db:
        db.get(KbSyncJob, one['id']).status = 'failed'
    now = g._now() + timedelta(days=8)
    monkeypatch.setattr(g, '_now', lambda: now)
    with pytest.raises(ValueError, match='state conflicts'):
        r.purge(journal['ticket'])
    assert archive.is_file()


def test_expiry_batch_and_partial_purge_resume(env, monkeypatch):
    versions(env)
    journal = r.apply()
    assert not r.purge_expired()['eligible']
    now = g._now() + timedelta(days=8)
    monkeypatch.setattr(g, '_now', lambda: now)
    assert r.purge_expired()['eligible'] == [journal['ticket']]
    original = Path.unlink
    calls = 0

    def interrupted(path, *args, **kwargs):
        nonlocal calls
        if 'quarantine' in path.parts and path.name != 'journal.json':
            calls += 1
            if calls == 2:
                raise OSError('purge interrupted')
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, 'unlink', interrupted)
    with pytest.raises(OSError, match='purge interrupted'):
        r.purge(journal['ticket'])
    monkeypatch.setattr(Path, 'unlink', original)
    assert r.resume(journal['ticket'])['phase'] == 'purged'
    assert not r.purge_expired()['eligible']


def test_new_published_reference_fences_old_interrupted_sweep(env, monkeypatch):
    versions(env)
    original = Path.rename
    monkeypatch.setattr(Path, 'rename', lambda *_: (_ for _ in ()).throw(OSError('injected')))
    with pytest.raises(OSError):
        r.apply()
    monkeypatch.setattr(Path, 'rename', original)
    ticket = next((r.root() / 'quarantine').iterdir()).name
    env.publish(snapshot(4, a='VPN newer independent evidence'))
    with pytest.raises(ValueError, match='References changed'):
        r.resume(ticket)
    assert r.restore(ticket)['phase'] == 'restored'


def test_lifetime_guard_blocks_another_process(env):
    import subprocess
    import sys
    code = 'from app.rag.maintenance import service_guard\nwith service_guard(exclusive=True): print("acquired")'
    with service_guard():
        result = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True,
                                env={**os.environ, 'KB_VECTOR_DIR': settings.KB_VECTOR_DIR}, timeout=10)
    assert result.returncode != 0 and 'acquired' not in result.stdout
    assert '停止所有后端服务' in result.stderr


def test_vector_round_trip_tolerance_is_bounded_and_text_is_exact():
    expected = {'a': ('body', {'doc_id': A}, [0.5, 0.75])}
    assert r._same_rows(expected, {'a': ('body', {'doc_id': A}, [0.50000005, 0.75])})
    assert not r._same_rows(expected, {'a': ('body', {'doc_id': A}, [0.5001, 0.75])})
    assert not r._same_rows(expected, {'a': ('changed', {'doc_id': A}, [0.5, 0.75])})
    assert not r._same_rows(expected, {'a': ('body', {'doc_id': A}, [float('nan'), 0.75])})


def test_restore_prepared_ticket_preserves_not_yet_deleted_collection(env, monkeypatch):
    one, _, _ = versions(env)
    original = r._finish
    monkeypatch.setattr(r, '_finish', lambda *_: (_ for _ in ()).throw(OSError('before retirement')))
    with pytest.raises(OSError):
        r.apply()
    monkeypatch.setattr(r, '_finish', original)
    ticket = next((r.root() / 'quarantine').iterdir()).name
    assert r.restore(ticket)['phase'] == 'restored'
    assert g.get_job(one['id'])['status'] == 'published'
    assert g.resolve_store(one['collection']).query([1, 1, 1], 3)


@pytest.mark.parametrize('status', ['failed', 'superseded'])
def test_unfinished_retained_task_protects_cache_without_blocking_generation_plan(env, status):
    one, _, _ = versions(env)
    failed = g.enqueue(snapshot(5, a='a new failing input'))
    with db_module.SessionLocal.begin() as db:
        db.get(KbSyncJob, failed['id']).status = status
    plan = r.plan()
    assert one['id'] in {item['id'] for item in plan['generations']}
    assert plan['cache_sweep_deferred'] and plan['incomplete_manifest_protected']
    assert not [item for item in plan['files'] if item['path'].startswith(('stages/', 'embeddings/'))]


def test_referenced_shared_snapshot_can_be_restored_after_interrupted_sweep(env, monkeypatch):
    versions(env)
    original = Path.rename
    monkeypatch.setattr(Path, 'rename', lambda *_: (_ for _ in ()).throw(OSError('injected')))
    with pytest.raises(OSError):
        r.apply()
    monkeypatch.setattr(Path, 'rename', original)
    ticket = next((r.root() / 'quarantine').iterdir()).name
    # Enqueue recreates the exact same authoritative snapshot path for a new published job.
    new = env.publish(snapshot(1))
    with pytest.raises(ValueError, match='References changed'):
        r.resume(ticket)
    assert r.restore(ticket)['phase'] == 'restored'
    assert env.active() == new['id']
    assert r.plan() is not None
