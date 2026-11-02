"""Offline, synthetic backups only: retention never touches production files."""
import importlib.util
import json
import os
from pathlib import Path
import sys
from datetime import datetime, timezone

import pytest

SPEC = importlib.util.spec_from_file_location('backup_retention', Path(__file__).resolve().parents[2] / 'scripts/backup_retention.py')
r = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(r)
NOW = datetime(2026, 11, 2, tzinfo=timezone.utc).timestamp()
OLD = 'pre-sync-20260901T000000Z'
FALLBACK = 'pre-sync-20260902T000000Z'
LATEST = 'pre-sync-20261101T000000Z'


def group(root, prefix):
    baseline = {'active_generation': 'generation-a', 'content_metadata_sha256': 'a' * 64}
    payloads = {'-manifest.json': {'baseline': baseline},
                '-restart-check.json': {'passed': True, 'old_generation_survived_restart': True, 'active_generation': 'generation-a'},
                '-restore-check.json': {'passed': True, 'temporary_files_removed': True,
                                        'denied_documents_not_resurrected': True, 'baseline': baseline,
                                        'hybrid': {'all_parent_contexts_verified': True}}}
    for suffix in r.SUFFIXES:
        path = root / (prefix + suffix)
        path.write_text(json.dumps(payloads[suffix]) if suffix in payloads else 'synthetic data')
        os.utime(path, (NOW - 60 * r.DAY, NOW - 60 * r.DAY))


@pytest.fixture
def backups(tmp_path):
    for prefix in (OLD, FALLBACK, LATEST):
        group(tmp_path, prefix)
    (tmp_path / 'latest-kb-backup').write_text(LATEST + '\n')
    return tmp_path


def test_protect_latest_window_and_fallback(backups):
    result = r.plan(backups, now=NOW)
    assert result['quarantine'] == [OLD]
    assert result['protected'][LATEST] == 'latest pointer'
    assert result['protected'][FALLBACK] == 'older verified fallback'
    os.utime(backups / (OLD + '.sql'), (NOW, NOW))
    assert not r.plan(backups, now=NOW)['quarantine']


@pytest.mark.parametrize('kind', ['missing', 'failed', 'mismatch', 'symlink', 'hardlink'])
def test_unverified_protected(backups, kind):
    target = backups / (OLD + '-restore-check.json')
    if kind == 'missing':
        target.unlink()
    elif kind == 'failed':
        target.write_text('{"passed": false}')
    elif kind == 'mismatch':
        payload = json.loads(target.read_text())
        payload['baseline']['active_generation'] = 'other'
        target.write_text(json.dumps(payload))
    elif kind == 'symlink':
        target.unlink()
        target.symlink_to(backups / (FALLBACK + '-restore-check.json'))
    else:
        target.unlink()
        os.link(backups / (FALLBACK + '-restore-check.json'), target)
    os.utime(target, (NOW - 60 * r.DAY, NOW - 60 * r.DAY)) if target.exists() else None
    assert OLD not in r.plan(backups, now=NOW)['quarantine']


def test_preview_is_readonly(backups, capsys):
    before = sorted((p.name, p.read_bytes(), p.stat().st_mtime_ns) for p in backups.iterdir())
    r.main(['--backup-dir', str(backups)])
    assert sorted((p.name, p.read_bytes(), p.stat().st_mtime_ns) for p in backups.iterdir()) == before
    assert not json.loads(capsys.readouterr().out)['applied']


def test_quarantine_restore_and_collision(backups):
    ticket = r.quarantine(backups, OLD, NOW)
    assert not (backups / (OLD + '.sql')).exists()
    assert len(r.restore(backups, ticket)['files']) == 6
    (backups / (OLD + '.sql')).write_text('new backup')
    with pytest.raises(ValueError, match='already exists'):
        r.restore(backups, ticket, True)
    (backups / (OLD + '.sql')).unlink()
    r.restore(backups, ticket, True)
    assert (backups / (OLD + '.sql')).read_text() == 'synthetic data'
    assert r.restore(backups, ticket, True)['files'] == []


def test_interrupted_move_can_restore(backups, monkeypatch):
    original = r.os.rename
    calls = []
    def fail_second(source, target):
        calls.append(source)
        if len(calls) == 2:
            raise OSError('simulated disk error')
        original(source, target)
    monkeypatch.setattr(r.os, 'rename', fail_second)
    with pytest.raises(OSError):
        r.quarantine(backups, OLD, NOW)
    monkeypatch.setattr(r.os, 'rename', original)
    ticket = next((backups / '.quarantine').iterdir()).name
    assert r.load_ticket(backups, ticket)[1]['state'] == 'moving'
    with pytest.raises(ValueError, match='Incomplete'):
        r.purge(backups, ticket, now=NOW + 8 * r.DAY, apply=True)
    r.restore(backups, ticket, True)
    assert all((backups / (OLD + s)).exists() for s in r.SUFFIXES)


def test_purge_explicit_grace_and_tamper(backups):
    ticket = r.quarantine(backups, OLD, NOW)
    with pytest.raises(ValueError, match='grace'):
        r.purge(backups, ticket, now=NOW + r.DAY, apply=True)
    r.purge(backups, ticket, now=NOW + 8 * r.DAY)
    assert (backups / '.quarantine' / ticket).exists()
    r.purge(backups, ticket, now=NOW + 8 * r.DAY, apply=True)
    assert r.load_ticket(backups, ticket)[1]['state'] == 'purged'


@pytest.mark.parametrize('attack', ['../outside', '/tmp/backup', OLD + '-../../../outside'])
def test_path_traversal(backups, attack):
    with pytest.raises(ValueError, match='ticket'):
        r.restore(backups, attack, True)


def test_journal_and_symlink_attacks(backups, tmp_path):
    ticket = r.quarantine(backups, OLD, NOW)
    journal_path = backups / '.quarantine' / ticket / 'journal.json'
    journal = json.loads(journal_path.read_text())
    journal['files']['../../outside'] = 'a' * 64
    journal_path.write_text(json.dumps(journal))
    with pytest.raises(ValueError, match='filenames'):
        r.restore(backups, ticket, True)
    link = backups / 'linked'
    link.symlink_to(backups, target_is_directory=True)
    with pytest.raises(ValueError, match='Symlink'):
        r.directory(link)


def test_lock_exclusion(backups):
    with r.lock(backups):
        with pytest.raises(BlockingIOError):
            with r.lock(backups):
                pass


def test_invalid_latest_fails_closed(backups):
    (backups / 'latest-kb-backup').write_text('../outside')
    with pytest.raises(ValueError):
        r.plan(backups, now=NOW)


def test_apply_recalculates(backups, monkeypatch, capsys):
    (backups / (OLD + '-restore-check.json')).unlink()
    r.main(['--backup-dir', str(backups), '--apply'])
    assert json.loads(capsys.readouterr().out)['tickets'] == []


def test_unknown_and_incomplete_files_survive(backups):
    unknown = backups / 'pre-sync-20260901T000000Z-extra.sql'
    unknown.write_text('unknown format')
    incomplete = 'pre-sync-20260801T000000Z'
    (backups / (incomplete + '.sql')).write_text('partial')
    result = r.plan(backups, now=NOW)
    assert unknown.name in result['unknown_protected']
    assert incomplete in result['protected']
    ticket = r.quarantine(backups, OLD, NOW)
    assert unknown.read_text() == 'unknown format'
    assert (backups / (incomplete + '.sql')).exists()


def test_changed_quarantine_refuses_restore_and_purge(backups):
    ticket = r.quarantine(backups, OLD, NOW)
    (backups / '.quarantine' / ticket / (OLD + '.sql')).write_text('corrupted')
    with pytest.raises(ValueError, match='hash mismatch'):
        r.restore(backups, ticket, True)
    with pytest.raises(ValueError, match='hash mismatch'):
        r.purge(backups, ticket, now=NOW + 8 * r.DAY, apply=True)


def test_latest_pointer_can_protect_quarantined_backup(backups):
    ticket = r.quarantine(backups, OLD, NOW)
    (backups / 'latest-kb-backup').write_text(OLD)
    with pytest.raises(ValueError, match='Latest pointer'):
        r.purge(backups, ticket, now=NOW + 8 * r.DAY, apply=True)


def test_payload_modified_after_restore_evidence_is_unverified(backups):
    path = backups / (OLD + '.sql')
    # Even after the online window, a changed payload needs another restore check.
    os.utime(path, (NOW - 30 * r.DAY, NOW - 30 * r.DAY))
    assert OLD not in r.plan(backups, now=NOW)['quarantine']


def test_batch_purge_protects_unknown_recent_and_restored(backups):
    expired = r.quarantine(backups, OLD, NOW - 8 * r.DAY)
    recent_prefix = 'pre-sync-20260802T000000Z'
    restored_prefix = 'pre-sync-20260803T000000Z'
    for prefix in (recent_prefix, restored_prefix):
        group(backups, prefix)
    recent = r.quarantine(backups, recent_prefix, NOW)
    restored = r.quarantine(backups, restored_prefix, NOW - 8 * r.DAY)
    r.restore(backups, restored, True)
    (backups / '.quarantine' / 'unknown').mkdir()
    preview = r.purge_expired(backups, now=NOW)
    assert [item['purge'] for item in preview['purge']] == [expired]
    assert {recent, restored, 'unknown'} <= preview['protected'].keys()
    assert r.load_ticket(backups, expired)[1]['state'] == 'complete'
    result = r.purge_expired(backups, now=NOW, apply=True)
    assert len(result['purge']) == 1
    assert (backups / (restored_prefix + '.sql')).exists()
    assert r.load_ticket(backups, expired)[1]['state'] == 'purged'
    assert expired in r.purge_expired(backups, now=NOW, apply=True)['protected']


def test_purge_resumes_after_unlink_failure(backups, monkeypatch):
    ticket = r.quarantine(backups, OLD, NOW - 8 * r.DAY)
    original = Path.unlink
    calls = []
    def fail_second(path, *args, **kwargs):
        if path.name in {OLD + suffix for suffix in r.SUFFIXES}:
            calls.append(path.name)
            if len(calls) == 2:
                raise OSError('injected unlink failure')
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'unlink', fail_second)
    with pytest.raises(OSError):
        r.purge(backups, ticket, now=NOW, apply=True)
    assert r.load_ticket(backups, ticket)[1]['state'] == 'purging'
    with pytest.raises(ValueError, match='purge has started'):
        r.restore(backups, ticket, True)
    monkeypatch.setattr(Path, 'unlink', original)
    assert len(r.purge(backups, ticket, now=NOW, apply=True)['files']) == 5
    assert r.load_ticket(backups, ticket)[1]['state'] == 'purged'
    assert all((backups / (FALLBACK + suffix)).exists() for suffix in r.SUFFIXES)


def test_batch_protects_partial_moving_corrupt_and_unknown_files(backups):
    ticket = r.quarantine(backups, OLD, NOW - 8 * r.DAY)
    path = backups / '.quarantine' / ticket / 'journal.json'
    payload = json.loads(path.read_text())
    payload['state'] = 'moving'
    path.write_text(json.dumps(payload))
    assert ticket in r.purge_expired(backups, now=NOW, apply=True)['protected']
    payload['state'] = 'complete'
    path.write_text(json.dumps(payload))
    unknown = path.parent / 'unknown.data'
    unknown.write_text('protect me')
    assert ticket in r.purge_expired(backups, now=NOW, apply=True)['protected']
    unknown.unlink()
    (path.parent / (OLD + '.sql')).write_text('corrupt')
    assert ticket in r.purge_expired(backups, now=NOW, apply=True)['protected']
