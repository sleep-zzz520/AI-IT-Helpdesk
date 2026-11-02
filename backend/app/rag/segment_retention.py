"""Narrow physical HNSW cleanup for the audited Chroma 1.5.9 layout.

Call only offline under the maintenance/publication guards. Persist capture's
record in the durable parent journal BEFORE deleting the collection. After API
deletion close every Chroma system/handle before move; never edit Chroma SQL.
ponytail: version/schema/file allowlists deliberately fail closed on upgrades.
"""
import hashlib
import os
import sqlite3
import stat
from pathlib import Path
from uuid import UUID

import chromadb

VERSION = '1.5.9'
HNSW = 'urn:chroma:segment/vector/hnsw-local-persisted'
METADATA = 'urn:chroma:segment/metadata/sqlite'
FILES = {'header.bin', 'data_level0.bin', 'length.bin', 'link_lists.bin', 'index_metadata.pickle'}


def _uuid(value):
    if not isinstance(value, str) or str(UUID(value)) != value:
        raise ValueError('Noncanonical Chroma UUID')
    return value


def _safe(path):
    path = Path(path).absolute()
    for part in [path, *path.parents]:
        if part.is_symlink():
            raise ValueError('Segment paths cannot contain symlinks')
    return path


def _fsync(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _database(persistdir):
    if chromadb.__version__ != VERSION:
        raise ValueError('Unreviewed Chroma segment layout version')
    path = _safe(_safe(persistdir) / 'chroma.sqlite3')
    if not path.is_file():
        raise ValueError('Missing Chroma database')
    connection = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)
    try:
        columns = connection.execute('PRAGMA table_info(segments)').fetchall()
        if [(r[1], r[2], r[5]) for r in columns] != [
            ('id', 'TEXT', 1), ('type', 'TEXT', 0), ('scope', 'TEXT', 0), ('collection', 'TEXT', 0)
        ]:
            raise ValueError('Unreviewed Chroma segments schema')
        if not {'id', 'name'} <= {r[1] for r in connection.execute('PRAGMA table_info(collections)')}:
            raise ValueError('Unreviewed Chroma collections schema')
        # A single SQLite read transaction binds validation and reference reads.
        connection.execute('BEGIN')
        return connection
    except BaseException:
        connection.close()
        raise


def _inventory(directory):
    directory = _safe(directory)
    if not directory.is_dir():
        raise ValueError('Segment is not a directory')
    result = []
    for path in sorted(directory.iterdir()):
        mode = path.lstat()
        if path.name not in FILES or not stat.S_ISREG(mode.st_mode) or mode.st_nlink != 1:
            raise ValueError('Unknown, linked or nested HNSW artifact')
        h = hashlib.sha256()
        with path.open('rb') as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b''):
                h.update(block)
        result.append({'name': path.name, 'bytes': mode.st_size, 'sha256': h.hexdigest()})
    return result


def _record(record):
    if (record.get('version') != 1 or record.get('chroma_version') != VERSION
            or not isinstance(record.get('segments'), list)):
        raise ValueError('Invalid segment retention record')
    _uuid(record['collection_id'])
    seen = set()
    for segment in record['segments']:
        key = _uuid(segment['id'])
        if key in seen or segment.get('type') != HNSW:
            raise ValueError('Invalid segment identity')
        seen.add(key)
        if segment['files'] is None:
            continue
        if not isinstance(segment['files'], list):
            raise ValueError('Invalid segment files')
        names = set()
        for item in segment['files']:
            name = item['name']
            if (name not in FILES or name in names or type(item['bytes']) is not int or item['bytes'] < 0
                    or not isinstance(item['sha256'], str) or len(item['sha256']) != 64
                    or any(c not in '0123456789abcdef' for c in item['sha256'])):
                raise ValueError('Invalid segment file identity')
            names.add(name)


def capture(persistdir, collection_uuid):
    """Read exact ownership before API deletion; return JSON-safe journal data."""
    collection_uuid = _uuid(str(collection_uuid))
    base = _safe(persistdir)
    db = _database(base)
    try:
        if db.execute('SELECT id FROM collections WHERE id=?', (collection_uuid,)).fetchone() is None:
            raise ValueError('Collection must exist when capturing segment ownership')
        rows = db.execute('SELECT id,type,scope FROM segments WHERE collection=?', (collection_uuid,)).fetchall()
        segments = []
        for key, kind, scope in rows:
            _uuid(key)
            if (kind, scope) == (METADATA, 'METADATA'):
                continue
            if (kind, scope) != (HNSW, 'VECTOR'):
                raise ValueError('Unknown collection segment type')
            directory = _safe(base / key)
            segments.append({'id': key, 'type': kind,
                             'files': _inventory(directory) if directory.exists() else None})
        return {'version': 1, 'chroma_version': VERSION, 'collection_id': collection_uuid,
                'segments': sorted(segments, key=lambda s: s['id'])}
    finally:
        db.close()


def collection_present(persistdir, record):
    _record(record)
    db = _database(persistdir)
    try:
        return db.execute('SELECT id FROM collections WHERE id=?', (record['collection_id'],)).fetchone() is not None
    finally:
        db.close()


def move(persistdir, quarantine_dir, record):
    """After API delete + closing Chroma handles, atomically quarantine exact dirs.

    quarantine_dir is the ticket's segments/ directory. Both missing means the
    API already removed the directory. A repeated move validates the destination.
    """
    _record(record)
    base, target = _safe(persistdir), _safe(quarantine_dir)
    db = _database(base)
    try:
        references = {row[0] for row in db.execute('SELECT id FROM segments')}
        if any(s['id'] in references for s in record['segments']):
            raise ValueError('Segment is still referenced in Chroma SQL')
        if db.execute('SELECT id FROM collections WHERE id=?', (record['collection_id'],)).fetchone():
            raise ValueError('Collection was not deleted through the Chroma API')
        actions = []
        for segment in record['segments']:
            src, dst = _safe(base / segment['id']), _safe(target / segment['id'])
            if src.exists() and dst.exists():
                raise ValueError('Both live and quarantined segment directories exist')
            existing = src if src.exists() else dst
            if existing.exists() and (segment['files'] is None or _inventory(existing) != segment['files']):
                raise ValueError('HNSW files changed since ownership capture')
            actions.append((src, dst))
        target.mkdir(parents=True, exist_ok=True)
        _fsync(target.parent)  # persist segments/ entry before removing live directory
        if target.stat().st_dev != base.stat().st_dev:
            raise ValueError('Segment quarantine must be on the same filesystem')
        for src, dst in actions:
            if src.exists():
                src.rename(dst)
                _fsync(base)
                _fsync(target)
        return record
    finally:
        db.close()


def validate_quarantine(quarantine_dir, record, *, resume=False):
    return _purge_actions(quarantine_dir, record, resume=resume)


def _purge_actions(quarantine_dir, record, *, resume=False):
    """Delete only captured files after validating all surviving bytes.

    Parent journal must durably enter purging before this call. Set resume=True
    only for an interrupted purge: then an exact subset of captured files is OK.
    JSON generation exports remain the restore authority, not these raw files.
    """
    _record(record)
    target = _safe(quarantine_dir)
    actions = []
    for segment in record['segments']:
        directory = _safe(target / segment['id'])
        if not directory.exists():
            continue  # API removed it, or an earlier purge finished this directory
        actual = _inventory(directory)
        expected = segment['files']
        if expected is None or (actual != expected and not (resume and all(i in expected for i in actual))):
            raise ValueError('Quarantined HNSW files changed')
        actions.append((directory, actual))
    return actions


def purge(quarantine_dir, record, *, resume=False):
    actions = _purge_actions(quarantine_dir, record, resume=resume)
    target = _safe(quarantine_dir)
    for directory, files in actions:
        for item in files:
            (directory / item['name']).unlink()
        _fsync(directory)
        directory.rmdir()
        _fsync(target)
    return record
