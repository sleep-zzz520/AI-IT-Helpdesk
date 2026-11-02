#!/usr/bin/env python3
"""Conservative backup retention. Preview by default; mutations require --apply.

Writers must share .retention.lock (flock) or run while retention is stopped.
A successful knowledge restore does not certify the full application SQL dump.
"""
import argparse
import contextlib
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import uuid

PREFIX = r"pre-sync-\d{8}T\d{6}Z"
SUFFIXES = (".sql", "-knowledge.sql", "-rag.tar.gz", "-manifest.json",
            "-restart-check.json", "-restore-check.json")
NAME = re.compile(r"(" + PREFIX + r")(" + "|".join(re.escape(s) for s in SUFFIXES) + r")$")
TICKET = re.compile(PREFIX + r"-[a-f0-9]{32}$")
DAY = 86400


def regular(path):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ValueError(f"Not a private regular file: {path.name}")
    return info


@contextlib.contextmanager
def open_regular(path, mode='rb'):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        os.close(fd)
        raise ValueError(f'Not a private regular file: {path.name}')
    with os.fdopen(fd, mode) as handle:
        yield handle


def read_json(path):
    with open_regular(path, 'r') as handle:
        return json.load(handle)


def digest(path):
    with open_regular(path) as handle:
        checksum = hashlib.sha256()
        for block in iter(lambda: handle.read(1024 * 1024), b''):
            checksum.update(block)
        return checksum.hexdigest()


def directory(path):
    path = Path(os.path.abspath(path))
    for part in (path, *path.parents):
        if part.is_symlink():
            raise ValueError('Symlink directory refused')
    if not path.is_dir():
        raise ValueError('Directory does not exist')
    return path


def latest(root):
    with open_regular(root / 'latest-kb-backup', 'r') as handle:
        value = handle.read().strip()
    if not re.fullmatch(PREFIX, value):
        raise ValueError('Invalid latest-kb-backup; refusing retention')
    return value


def verified(root, prefix):
    try:
        for suffix in SUFFIXES:
            if regular(root / (prefix + suffix)).st_size == 0:
                return False
        verified_at = regular(root / (prefix + '-restore-check.json')).st_mtime_ns
        if any(regular(root / (prefix + suffix)).st_mtime_ns > verified_at
               for suffix in SUFFIXES if suffix != '-restore-check.json'):
            return False
        manifest = read_json(root / (prefix + '-manifest.json'))
        restart = read_json(root / (prefix + '-restart-check.json'))
        restore = read_json(root / (prefix + '-restore-check.json'))
        baseline = manifest['baseline']
        return (isinstance(baseline, dict) and bool(baseline.get('active_generation'))
                and bool(baseline.get('content_metadata_sha256'))
                and restart.get('passed') is True
                and restart.get('old_generation_survived_restart') is True
                and restart.get('active_generation') == baseline['active_generation']
                and restore.get('passed') is True
                and restore.get('temporary_files_removed') is True
                and restore.get('denied_documents_not_resurrected') is True
                and restore.get('baseline') == baseline
                and restore.get('hybrid', {}).get('all_parent_contexts_verified') is True)
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return False


def plan(root, online_days=7, now=None):
    now = now or datetime.now(timezone.utc).timestamp()
    protected_latest = latest(root)
    groups, unknown = {}, []
    for entry in root.iterdir():
        match = NAME.fullmatch(entry.name)
        if match:
            groups.setdefault(match[1], set()).add(match[2])
        elif entry.name not in ('latest-kb-backup', '.retention.lock', '.quarantine'):
            unknown.append(entry.name)
    candidates, protected = [], {}
    for prefix, suffixes in groups.items():
        try:
            age = now - datetime.strptime(prefix[9:], '%Y%m%dT%H%M%SZ').replace(tzinfo=timezone.utc).timestamp()
            # A newly copied old backup must also pass the online safety window.
            newest_write = max((root / (prefix + s)).lstat().st_mtime for s in suffixes)
        except (ValueError, OSError):
            protected[prefix] = 'invalid timestamp or unreadable files'
            continue
        if prefix == protected_latest:
            protected[prefix] = 'latest pointer'
        elif age < online_days * DAY or now - newest_write < online_days * DAY:
            protected[prefix] = 'online safety window'
        elif suffixes != set(SUFFIXES) or not verified(root, prefix):
            protected[prefix] = 'incomplete or not successfully restore-verified'
        else:
            candidates.append(prefix)
    # ponytail: keep one old verified fallback irrespective of backup frequency.
    # Larger installations can replace this with an explicit restore-point policy.
    candidates.sort()
    earlier = [prefix for prefix in candidates if prefix < protected_latest]
    if earlier:
        fallback = earlier[-1]
        candidates.remove(fallback)
        protected[fallback] = 'older verified fallback'
    else:
        for prefix in candidates:
            protected[prefix] = 'no older verified fallback'
        candidates = []
    return {'quarantine': candidates, 'protected': protected, 'unknown_protected': sorted(unknown)}


def save(path, value):
    temporary = path.with_name(path.name + '.tmp')
    if temporary.exists() or temporary.is_symlink():
        regular(temporary)
        temporary.unlink()  # A prior interrupted atomic journal write; main journal is authoritative.
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as handle:
        json.dump(value, handle, sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    sync_dir(path.parent)


def sync_dir(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


@contextlib.contextmanager
def lock(root):
    fd = os.open(root / '.retention.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode) or os.fstat(fd).st_nlink != 1:
            raise ValueError('Unsafe lock file')
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        os.close(fd)


def quarantine(root, prefix, now):
    q = root / '.quarantine'
    if not q.exists():
        q.mkdir(mode=0o700)
    directory(q)
    if q.stat().st_dev != root.stat().st_dev:
        raise ValueError('Quarantine must be on the same filesystem')
    ticket = q / (prefix + '-' + uuid.uuid4().hex)
    ticket.mkdir(mode=0o700)
    files = {prefix + suffix: digest(root / (prefix + suffix)) for suffix in SUFFIXES}
    journal = {'version': 1, 'prefix': prefix, 'created_at': now, 'state': 'moving', 'files': files}
    # Persist intent before any rename: interrupted moves can always be restored.
    save(ticket / 'journal.json', journal)
    sync_dir(q)
    for name, expected in files.items():
        if digest(root / name) != expected:
            raise ValueError('Backup changed while quarantining')
        os.rename(root / name, ticket / name)
        sync_dir(root)
        sync_dir(ticket)
    journal['state'] = 'complete'
    save(ticket / 'journal.json', journal)
    return ticket.name


def load_ticket(root, ticket_id):
    if not TICKET.fullmatch(ticket_id):
        raise ValueError('Invalid quarantine ticket')
    ticket = directory(directory(root / '.quarantine') / ticket_id)
    journal = read_json(ticket / 'journal.json')
    prefix = journal.get('prefix', '')
    if not re.fullmatch(PREFIX, prefix) or not ticket_id.startswith(prefix + '-'):
        raise ValueError('Invalid journal prefix')
    if journal.get('version') != 1 or journal.get('state') not in ('moving', 'complete', 'restoring', 'restored', 'purging', 'purged'):
        raise ValueError('Invalid journal state')
    if set(journal.get('files', {})) != {prefix + s for s in SUFFIXES}:
        raise ValueError('Invalid journal filenames')
    if any(not re.fullmatch(r'[a-f0-9]{64}', h) for h in journal['files'].values()):
        raise ValueError('Invalid journal hashes')
    allowed = set(journal['files']) | {'journal.json', 'journal.json.tmp'}
    if any(p.name not in allowed for p in ticket.iterdir()):
        raise ValueError('Unexpected quarantine files')
    return ticket, journal


def restore(root, ticket_id, apply=False):
    ticket, journal = load_ticket(root, ticket_id)
    if journal['state'] in ('purging', 'purged'):
        raise ValueError('Physical purge has started; restore is unavailable')
    moves = []
    for name, expected in journal['files'].items():
        source, target = ticket / name, root / name
        if source.exists() or source.is_symlink():
            if target.exists() or target.is_symlink():
                raise ValueError('Restore destination already exists')
            if digest(source) != expected:
                raise ValueError('Quarantined file hash mismatch')
            moves.append(name)
        elif digest(target) != expected:
            raise ValueError('Missing or changed original file')
    if apply:
        journal['state'] = 'restoring'
        save(ticket / 'journal.json', journal)
        for name in moves:
            os.rename(ticket / name, root / name)
            sync_dir(root)
            sync_dir(ticket)
        journal['state'] = 'restored'
        save(ticket / 'journal.json', journal)
        # Retain journal for audit; repeated restore is idempotent.
    return {'restore': ticket_id, 'files': moves}


def purge(root, ticket_id, grace_days=7, now=None, apply=False):
    now = now or datetime.now(timezone.utc).timestamp()
    ticket, journal = load_ticket(root, ticket_id)
    if journal['state'] not in ('complete', 'purging'):
        raise ValueError('Incomplete, restored or already purged quarantine')
    if now - journal['created_at'] < grace_days * DAY:
        raise ValueError('Quarantine grace period has not elapsed')
    if latest(root) == journal['prefix']:
        raise ValueError('Latest pointer protects quarantined backup')
    # A restored group or unrelated files at original paths must never be purged.
    if any((root / name).exists() or (root / name).is_symlink() for name in journal['files']):
        raise ValueError('Original backup paths are occupied; protect quarantine')
    online = plan(root, 0, now)
    if not any(reason == 'older verified fallback' for reason in online['protected'].values()):
        raise ValueError('No older verified online fallback')
    remaining = []
    for name, expected in journal['files'].items():
        path = ticket / name
        if not path.exists() and not path.is_symlink() and journal['state'] == 'purging':
            continue  # The persisted purge intent permits resuming interrupted unlinks.
        if digest(path) != expected:
            raise ValueError('Quarantined file hash mismatch')
        remaining.append(name)
    if apply:
        journal['state'] = 'purging'
        save(ticket / 'journal.json', journal)
        for name in remaining:
            (ticket / name).unlink()
            sync_dir(ticket)
        journal['state'] = 'purged'
        journal['purged_at'] = now
        save(ticket / 'journal.json', journal)
    return {'purge': ticket_id, 'files': remaining}


def purge_expired(root, grace_days=7, now=None, apply=False):
    quarantine_dir = root / '.quarantine'
    if not quarantine_dir.exists() and not quarantine_dir.is_symlink():
        return {'purge': [], 'protected': {}}
    directory(quarantine_dir)
    result = {'purge': [], 'protected': {}}
    for entry in sorted(quarantine_dir.iterdir()):
        try:
            # Re-read protection and evidence for each ticket, including retries.
            item = purge(root, entry.name, grace_days, now, apply)
            result['purge'].append(item)
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
            result['protected'][entry.name] = str(exc)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--backup-dir', type=Path, default=Path('backups'))
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--online-days', type=int, default=7)
    parser.add_argument('--grace-days', type=int, default=7)
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument('--restore', metavar='TICKET')
    actions.add_argument('--purge', metavar='TICKET')
    actions.add_argument('--purge-expired', action='store_true')
    args = parser.parse_args(argv)
    if args.online_days < 7 or args.grace_days < 7:
        parser.error('Safety windows must be at least 7 days')
    root = directory(args.backup_dir)
    with lock(root) if args.apply else contextlib.nullcontext():
        if args.restore:
            result = restore(root, args.restore, args.apply)
        elif args.purge_expired:
            result = purge_expired(root, args.grace_days, apply=args.apply)
        elif args.purge:
            result = purge(root, args.purge, args.grace_days, apply=args.apply)
        else:
            result = plan(root, args.online_days)
            if args.apply:
                result['tickets'] = [quarantine(root, prefix, datetime.now(timezone.utc).timestamp())
                                     for prefix in result['quarantine']]
        print(json.dumps({'applied': args.apply, **result}, ensure_ascii=False, sort_keys=True))


if __name__ == '__main__':
    main()
