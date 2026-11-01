"""Bounded, strict Markdown snapshots read from immutable Git objects or exports.

The original bytes are authoritative. Parsing never consults the working tree,
loads images, expands LFS, or fetches external resources. Empty snapshots are
valid inputs; publication policy belongs to the caller.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
import math
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path, PurePosixPath
from uuid import UUID

import yaml

from app.rag.chunks import KnowledgeDoc

MAX_DOCUMENTS = 512
MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_TOTAL_BYTES = 16 * 1024 * 1024
MAX_EXPORT_BYTES = 24 * 1024 * 1024
GIT_TIMEOUT_SECONDS = 30
_OID = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_IMAGE = re.compile(r"!\s*\[|<\s*(?:img|picture|svg|object|iframe)\b", re.I)


class GitSourceError(ValueError):
    """Source could not be read completely and validated; never infer deletion."""


@dataclass(frozen=True)
class GitSnapshot:
    commit: str
    files: dict[str, bytes]
    docs: list[KnowledgeDoc]
    raw_hashes: dict[str, str]
    snapshot_hash: str

    @property
    def snapshot_id(self) -> str:
        return self.snapshot_hash


class _StrictLoader(yaml.SafeLoader):
    def compose_node(self, parent, index):
        if self.check_event(yaml.AliasEvent):
            raise GitSourceError("YAML aliases are not supported")
        return super().compose_node(parent, index)

    def construct_mapping(self, node, deep=False):
        result = {}
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            if not isinstance(key, str) or key in result:
                raise GitSourceError("YAML keys must be unique strings")
            result[key] = self.construct_object(value_node, deep=deep)
        return result


def _path(value: str) -> str:
    if (not isinstance(value, str) or not value or len(value) > 4096 or value.startswith(("/", "-"))
            or "\\" in value or any(ord(c) < 32 or ord(c) == 127 for c in value)
            or any(part in {"", ".", ".."} for part in value.split("/"))
            or PurePosixPath(value).is_absolute()):
        raise GitSourceError(f"Unsafe source path: {value!r}")
    return value


def _metadata(meta: dict) -> dict:
    result = {}
    for key, value in meta.items():
        if not isinstance(key, str):
            raise GitSourceError("Metadata keys must be strings")
        if isinstance(value, (date, datetime)):
            value = value.isoformat()
        if isinstance(value, list):
            if not all(isinstance(item, str) for item in value):
                raise GitSourceError(f"Metadata {key} must contain only strings")
        elif not isinstance(value, (str, bool, int, float)) or (
            isinstance(value, float) and not math.isfinite(value)
        ):
            raise GitSourceError(f"Unsupported metadata value: {key}")
        result[key] = value
    return result


def _parse(path: str, raw: bytes, legacy_ids: dict[str, str]) -> KnowledgeDoc:
    if PurePosixPath(path).suffix.lower() != ".md":
        raise GitSourceError(f"Unsupported file format: {path}")
    if raw.startswith(b"version https://git-lfs.github.com/spec/v1"):
        raise GitSourceError(f"Git LFS pointer is unsupported: {path}")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise GitSourceError(f"Markdown must be UTF-8: {path}") from exc
    if "\x00" in text:
        raise GitSourceError(f"Binary Markdown is unsupported: {path}")
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        raise GitSourceError(f"Missing frontmatter: {path}")
    end = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
    if end is None:
        raise GitSourceError(f"Unterminated frontmatter: {path}")
    try:
        meta = yaml.load("".join(lines[1:end]), Loader=_StrictLoader)
    except (yaml.YAMLError, RecursionError, TypeError, ValueError) as exc:
        raise GitSourceError(f"Invalid frontmatter: {path}: {exc}") from exc
    if not isinstance(meta, dict):
        raise GitSourceError(f"Frontmatter must be a mapping: {path}")
    meta = _metadata(meta)
    if not isinstance(meta.get("scenario"), str) or not meta["scenario"].strip():
        raise GitSourceError(f"Missing nonempty scenario: {path}")
    identity = meta.get("document_id", legacy_ids.get(path))
    try:
        identity = str(UUID(identity))
    except (ValueError, TypeError, AttributeError) as exc:
        raise GitSourceError(f"Missing or invalid document_id: {path}") from exc
    body = "".join(lines[end + 1:]).lstrip("\r\n")
    if _IMAGE.search(body):
        raise GitSourceError(f"Embedded images or external objects are unsupported: {path}")
    meta.update(document_id=identity, source_url=path)
    return KnowledgeDoc(rel_path=path, doc_id=identity, meta=meta, content=body)


def read_snapshot_from_files(commit: str, files: dict[str, bytes]) -> GitSnapshot:
    """Validate retained bytes without touching Git (also used during recovery)."""
    if not isinstance(commit, str) or not _OID.fullmatch(commit):
        raise GitSourceError("A full Git commit SHA is required")
    if not isinstance(files, dict) or len(files) > MAX_DOCUMENTS:
        raise GitSourceError("Too many documents or invalid file manifest")
    try:
        legacy_ids = json.loads(Path(__file__).with_name("legacy_document_ids.json").read_text())
    except (OSError, ValueError) as exc:
        raise GitSourceError("Cannot read explicit legacy ID migration map") from exc
    for path in files:
        _path(path)
    docs, retained, raw_hashes = [], {}, {}
    total = 0
    for path, raw in sorted(files.items()):
        _path(path)
        if not isinstance(raw, bytes) or len(raw) > MAX_FILE_BYTES:
            raise GitSourceError(f"Invalid or oversized file: {path}")
        total += len(raw)
        if total > MAX_TOTAL_BYTES:
            raise GitSourceError("Snapshot byte limit exceeded")
        doc = _parse(path, raw, legacy_ids)
        if doc.doc_id in raw_hashes:
            raise GitSourceError(f"Duplicate document_id: {doc.doc_id}")
        docs.append(doc)
        retained[path] = raw
        raw_hashes[doc.doc_id] = hashlib.sha256(raw).hexdigest()
    manifest = {"commit": commit, "files": {
        path: hashlib.sha256(raw).hexdigest() for path, raw in retained.items()
    }}
    snapshot_hash = hashlib.sha256(json.dumps(
        manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode()).hexdigest()
    return GitSnapshot(commit, retained, docs, raw_hashes, snapshot_hash)


def _git(repo: Path, *args: str) -> bytes:
    try:
        return subprocess.run(
            ["git", "--no-pager", "--literal-pathspecs", "-C", str(repo), *args],
            check=True, capture_output=True,
            timeout=GIT_TIMEOUT_SECONDS,
        ).stdout
    except (OSError, subprocess.SubprocessError) as exc:
        raise GitSourceError(f"Git snapshot command failed: {args[0]}") from exc


def read_snapshot(repo: Path, ref: str = "HEAD", subdir: str = "docs/knowledge") -> GitSnapshot:
    """Resolve ref once, enumerate once, then read only the pinned blob objects."""
    _path(subdir)
    if not isinstance(ref, str) or not ref or ref.startswith("-") or any(
        ord(char) < 32 for char in ref
    ):
        raise GitSourceError("Unsafe Git ref")
    try:
        commit = _git(repo, "rev-parse", "--verify", "--end-of-options", f"{ref}^{{commit}}").decode("ascii").strip()
    except UnicodeDecodeError as exc:
        raise GitSourceError("Invalid Git commit response") from exc
    if not _OID.fullmatch(commit):
        raise GitSourceError("Git did not return a full commit SHA")
    tree = _git(repo, "ls-tree", "-rz", "-l", commit, "--", subdir)
    if tree and not tree.endswith(b"\0"):
        raise GitSourceError("Incomplete Git tree enumeration")
    entries = tree[:-1].split(b"\0") if tree else []
    if len(entries) > MAX_DOCUMENTS:
        raise GitSourceError("Too many documents")
    files, total = {}, 0
    for entry in entries:
        try:
            header, name = entry.split(b"\t", 1)
            mode, kind, oid, size = header.split()
            full_path = name.decode("utf-8")
            _path(full_path)
            if not full_path.startswith(subdir + "/"):
                raise GitSourceError("Source directory is not a regular Git tree")
            path = _path(full_path[len(subdir) + 1:])
            if mode not in {b"100644", b"100755"} or kind != b"blob":
                raise GitSourceError(f"Symlink/submodule/unsupported Git entry: {path}")
            if not _OID.fullmatch(oid.decode("ascii")):
                raise GitSourceError("Invalid Git blob OID")
            size = int(size)
            if size < 0 or size > MAX_FILE_BYTES:
                raise GitSourceError(f"Oversized blob: {path}")
            total += size
            if total > MAX_TOTAL_BYTES or path in files:
                raise GitSourceError("Duplicate path or snapshot byte limit exceeded")
            raw = _git(repo, "cat-file", "blob", oid.decode("ascii"))
            if len(raw) != size:
                raise GitSourceError(f"Incomplete Git blob read: {path}")
            files[path] = raw
        except (UnicodeError, ValueError) as exc:
            if isinstance(exc, GitSourceError):
                raise
            raise GitSourceError("Malformed Git tree enumeration") from exc
    return read_snapshot_from_files(commit, files)


def _unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise GitSourceError(f"Duplicate JSON key: {key}")
        result[key] = value
    return result


def write_export(snapshot: GitSnapshot, path: Path) -> None:
    """Atomically persist portable source bytes for workers without .git access."""
    verified = read_snapshot_from_files(snapshot.commit, snapshot.files)
    if verified.snapshot_hash != snapshot.snapshot_hash:
        raise GitSourceError("Snapshot was mutated after validation")
    payload = {"version": 1, "commit": verified.commit,
               "snapshot_hash": verified.snapshot_hash, "files": {
                   name: {"sha256": hashlib.sha256(raw).hexdigest(),
                          "bytes": base64.b64encode(raw).decode("ascii")}
                   for name, raw in verified.files.items()
               }}
    serialized = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    if len(serialized.encode("utf-8")) > MAX_EXPORT_BYTES:
        raise GitSourceError("Export byte limit exceeded")
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=f".{path.name}.", delete=False) as handle:
            temp = Path(handle.name)
            handle.write(serialized)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if temp is not None:
            temp.unlink(missing_ok=True)


def read_export(path: Path) -> GitSnapshot:
    """Read a bounded export, verify every checksum, and reparse authoritative bytes."""
    try:
        with Path(path).open("rb") as handle:
            raw = handle.read(MAX_EXPORT_BYTES + 1)
        if len(raw) > MAX_EXPORT_BYTES:
            raise GitSourceError("Export byte limit exceeded")
        payload = json.loads(raw, object_pairs_hook=_unique_pairs)
        if (not isinstance(payload, dict) or set(payload) != {"version", "commit", "snapshot_hash", "files"}
                or type(payload["version"]) is not int or payload["version"] != 1
                or not isinstance(payload["files"], dict)
                or len(payload["files"]) > MAX_DOCUMENTS):
            raise GitSourceError("Invalid export manifest")
        files = {}
        for name, entry in payload["files"].items():
            _path(name)
            if not isinstance(entry, dict) or set(entry) != {"sha256", "bytes"}:
                raise GitSourceError("Invalid export file entry")
            if not isinstance(entry["bytes"], str) or len(entry["bytes"]) > (MAX_FILE_BYTES + 2) // 3 * 4:
                raise GitSourceError("Oversized or invalid export file")
            content = base64.b64decode(entry["bytes"], validate=True)
            if hashlib.sha256(content).hexdigest() != entry["sha256"]:
                raise GitSourceError(f"Export checksum mismatch: {name}")
            files[name] = content
        snapshot = read_snapshot_from_files(payload["commit"], files)
        if snapshot.snapshot_hash != payload["snapshot_hash"]:
            raise GitSourceError("Export snapshot checksum mismatch")
        return snapshot
    except (OSError, UnicodeError, ValueError, TypeError, KeyError, binascii.Error, RecursionError) as exc:
        if isinstance(exc, GitSourceError):
            raise
        raise GitSourceError("Cannot read valid source export") from exc
