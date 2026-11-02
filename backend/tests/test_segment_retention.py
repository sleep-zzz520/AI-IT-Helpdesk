"""Real temporary Chroma stores; no production paths or internal SQL writes."""
from pathlib import Path
import sqlite3

import chromadb
from chromadb.api.client import SharedSystemClient
import pytest

from app.rag import segment_retention as s


def close(client):
    client._system.stop()
    SharedSystemClient.clear_system_cache()


def build(tmp_path):
    base = tmp_path / 'vectors'
    client = chromadb.PersistentClient(path=str(base))
    col = client.create_collection('retention_test')
    col.add(ids=['a'], documents=['vpn certificate'], embeddings=[[1., 0., 0.]])
    record = s.capture(base, str(col.id))
    assert len(record['segments']) == 1
    assert record['segments'][0]['files']
    return base, client, col, record, tmp_path / 'ticket' / 'segments'


def bytes_at(path):
    return sum(p.stat().st_size for p in path.rglob('*') if p.is_file()) if path.exists() else 0


def test_real_delete_move_purge_reduces_live_hnsw_bytes(tmp_path):
    base, client, col, record, quarantine = build(tmp_path)
    segment = base / record['segments'][0]['id']
    before = bytes_at(segment)
    assert before > 0
    client.delete_collection(col.name)
    close(client)
    s.move(base, quarantine, record)
    assert bytes_at(segment) == 0
    moved = bytes_at(quarantine)
    assert moved in (0, before)  # API can already physically remove it on some platforms
    s.move(base, quarantine, record)
    s.purge(quarantine, record)
    assert bytes_at(quarantine) == 0
    s.purge(quarantine, record, resume=True)


def test_referenced_segment_refused(tmp_path):
    base, client, col, record, quarantine = build(tmp_path)
    try:
        with pytest.raises(ValueError, match='referenced'):
            s.move(base, quarantine, record)
        assert (base / record['segments'][0]['id']).is_dir()
    finally:
        close(client)


@pytest.mark.parametrize('kind', ['unknown', 'nested', 'symlink', 'hardlink'])
def test_capture_refuses_unreviewed_directory_entries(tmp_path, kind):
    base, client, col, record, quarantine = build(tmp_path)
    directory = base / record['segments'][0]['id']
    try:
        if kind == 'unknown':
            (directory / 'surprise.bin').write_bytes(b'x')
        elif kind == 'nested':
            (directory / 'nested').mkdir()
        elif kind == 'symlink':
            (directory / 'index_metadata.pickle').symlink_to(directory / 'header.bin')
        else:
            import os
            os.link(directory / 'header.bin', tmp_path / 'hardlink')
        with pytest.raises(ValueError, match='artifact'):
            s.capture(base, str(col.id))
    finally:
        close(client)


def test_move_checks_all_bytes_before_rename_and_preserves_unknown_orphan(tmp_path):
    base, client, col, record, quarantine = build(tmp_path)
    orphan = base / '00000000-0000-0000-0000-000000000001'
    orphan.mkdir()
    (orphan / 'important').write_bytes(b'keep')
    client.delete_collection(col.name)
    close(client)
    directory = base / record['segments'][0]['id']
    if not directory.exists():
        pytest.skip('API already removed physical segment on this platform')
    (directory / 'header.bin').write_bytes(b'changed')
    with pytest.raises(ValueError, match='changed'):
        s.move(base, quarantine, record)
    assert directory.exists()
    assert (orphan / 'important').read_bytes() == b'keep'


def test_partial_purge_requires_explicit_resume(tmp_path):
    base, client, col, record, quarantine = build(tmp_path)
    client.delete_collection(col.name)
    close(client)
    s.move(base, quarantine, record)
    directory = quarantine / record['segments'][0]['id']
    if not directory.exists():
        pytest.skip('API already removed physical segment on this platform')
    (directory / 'header.bin').unlink()
    with pytest.raises(ValueError, match='changed'):
        s.purge(quarantine, record)
    s.purge(quarantine, record, resume=True)
    assert not directory.exists()


def test_schema_and_version_fail_closed(tmp_path, monkeypatch):
    base, client, col, record, quarantine = build(tmp_path)
    close(client)
    monkeypatch.setattr(s.chromadb, '__version__', '1.6.0')
    with pytest.raises(ValueError, match='version'):
        s.capture(base, record['collection_id'])


def test_readonly_database_and_missing_dirs_are_safe(tmp_path):
    base, client, col, record, quarantine = build(tmp_path)
    db = s._database(base)
    with pytest.raises(sqlite3.OperationalError, match='readonly'):
        db.execute('DELETE FROM segments')
    db.close()
    client.delete_collection(col.name)
    close(client)
    # Explicitly simulate an API which already removes physical HNSW files.
    import shutil
    directory = base / record['segments'][0]['id']
    if directory.exists():
        shutil.rmtree(directory)
    s.move(base, quarantine, record)
    s.purge(quarantine, record)
