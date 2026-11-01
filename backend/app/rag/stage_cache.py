"""Optional, disposable parse/chunk artifacts; source bytes remain authoritative.

No application settings, database, model clients or generation imports belong here.
A missing, damaged or unwritable cache only causes a local stage recomputation.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path
from typing import Any

from app.rag.chunks import Chunk, KnowledgeDoc

MAX_CACHE_BYTES = 64 * 1024 * 1024
CACHE_VERSION = 1


def _json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                      allow_nan=False).encode("utf-8")


def _digest(value: Any) -> str:
    return hashlib.sha256(_json(value)).hexdigest()


def _pairs(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("Duplicate cache key")
        value[key] = item
    return value


def _bump(stats, key):
    if stats is not None:
        stats[key] = stats.get(key, 0) + 1


def _write(path: Path, value: dict) -> None:
    raw = _json(value)
    if len(raw) > MAX_CACHE_BYTES:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".cache-", delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _cached(stage, identity, builder, validate, *, cache_dir, stats, metric):
    key = _digest({"version": CACHE_VERSION, "stage": stage, "identity": identity})
    path = Path(cache_dir) / stage / f"{key}.json" if cache_dir is not None else None
    if path is not None:
        try:
            with path.open("rb") as handle:
                raw = handle.read(MAX_CACHE_BYTES + 1)
            if len(raw) > MAX_CACHE_BYTES:
                raise ValueError("Oversized stage cache")
            value = json.loads(raw, object_pairs_hook=_pairs)
            if (not isinstance(value, dict) or set(value) != {"key", "digest", "payload"}
                    or value["key"] != key or value["digest"] != _digest(value["payload"])):
                raise ValueError("Invalid stage cache envelope")
            validate(value["payload"])
        except (OSError, ValueError, TypeError, KeyError, RecursionError, OverflowError):
            pass
        else:
            _bump(stats, f"{metric}_hits")
            return value["payload"]
    _bump(stats, f"{metric}_misses")
    payload = builder()
    validate(payload)
    _bump(stats, "parsed_documents" if metric == "parse" else "chunked_documents")
    if path is not None:
        # Cache persistence must not turn a validated source into a failed sync.
        with suppress(OSError):
            _write(path, {"key": key, "digest": _digest(payload), "payload": payload})
    return payload


def cached_parse(raw_hash: str, profile: str, builder: Callable[[], dict],
                 validate: Callable[[dict], None], *, cache_dir: Path | None = None,
                 stats: dict | None = None) -> dict:
    """Cache path-independent parsed fields; callers must bind current path/UUID."""
    return _cached("parsed", {"raw_hash": raw_hash, "profile": profile}, builder, validate,
                   cache_dir=cache_dir, stats=stats, metric="parse")


def _scalar_metadata(metadata):
    if not isinstance(metadata, dict) or not metadata:
        return False
    return all(isinstance(key, str) and isinstance(value, (str, bool, int, float))
               and (not isinstance(value, float) or math.isfinite(value))
               for key, value in metadata.items())


def cached_chunks(doc: KnowledgeDoc, revision: str, source_id: str, profile: str,
                  builder: Callable[[], list[Chunk]], *, cache_dir: Path | None = None,
                  stats: dict | None = None) -> list[Chunk]:
    """Return complete parent/children, bound to source, body, metadata and splitter.

    ``profile`` must fingerprint every splitter implementation/configuration input.
    The builder performs the real split and injects source_id/doc_revision metadata.
    """
    identity = {"path": doc.rel_path, "doc_id": doc.doc_id, "meta": doc.meta,
                "content": doc.content, "revision": revision, "source_id": source_id,
                "profile": profile}

    def validate(payload):
        if not isinstance(payload, list) or not payload or len(payload) > 100_000:
            raise ValueError("Invalid chunk list")
        ids = set()
        parent = payload[0]
        if not isinstance(parent, dict) or not isinstance(parent.get("metadata"), dict):
            raise ValueError("Invalid parent chunk")
        parent_id = parent.get("id")
        for index, chunk in enumerate(payload):
            if (not isinstance(chunk, dict) or set(chunk) != {"id", "text", "metadata"}
                    or not isinstance(chunk["id"], str) or not chunk["id"] or chunk["id"] in ids
                    or not isinstance(chunk["text"], str) or not _scalar_metadata(chunk["metadata"])):
                raise ValueError("Invalid chunk fields")
            meta = chunk["metadata"]
            if (meta.get("doc_id") != doc.doc_id or meta.get("source_id") != source_id
                    or meta.get("doc_revision") != revision
                    or meta.get("chunk_type") != ("parent" if index == 0 else "child")
                    or meta.get("parent_id") != ("" if index == 0 else parent_id)):
                raise ValueError("Chunk source/parent identity mismatch")
            ids.add(chunk["id"])
        if parent["text"] != doc.content:
            raise ValueError("Cached parent differs from authoritative document")

    def build():
        return [{"id": chunk.id, "text": chunk.text, "metadata": chunk.metadata}
                for chunk in builder()]

    payload = _cached("chunks", identity, build, validate,
                      cache_dir=cache_dir, stats=stats, metric="chunk")
    return [Chunk(chunk["id"], chunk["text"], dict(chunk["metadata"])) for chunk in payload]
