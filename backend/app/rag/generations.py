"""受控Git来源：持久任务、不可变整代索引、向量复用和条件发布。

ponytail: 首版同机、单共享IT知识来源，最多512篇。每代复制完整索引，
以存储换读写隔离；仅离线保护式GC，不承诺多主机或文档级租户授权。
"""
import hashlib
import inspect
import json
import logging
import math
import os
import threading
import time
import traceback
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import or_, update
from sqlalchemy.exc import IntegrityError, OperationalError

from app import db as db_module
from app.config import settings
from app.models import KbSource, KbSyncJob
from app.rag import embedder
from app.rag.bm25 import Bm25Index
from app.rag.chunks import Chunk, KnowledgeDoc
from app.rag.splitter import split_markdown
from app.rag.store import ChromaStore
from app.rag.sync import publication_lock

logger = logging.getLogger(__name__)
LEASE_SECONDS = 120
MAX_ATTEMPTS = 3


def _now():
    return datetime.now(UTC).replace(tzinfo=None)


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _hash(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("w", encoding="utf-8") as handle:
            handle.write(_json(value))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        temporary.unlink(missing_ok=True)


def embedding_profile():
    """嵌入空间只绑定实际清洗输入及模型，解析器/重试代码升级不使向量失效。"""
    return _hash({"model": embedder.EMBED_MODEL, "dimensions": embedder.EMBED_DIM,
                  "endpoint": settings.GLM_BASE_URL, "max_chars": embedder.EMBED_MAX_CHARS,
                  "cleaner": hashlib.sha256(inspect.getsource(embedder._clean_text).encode()).hexdigest(),
                  "epoch": settings.KB_PIPELINE_EPOCH, "source": settings.KB_SOURCE_ID})


def chunk_profile():
    return _hash({"splitter": hashlib.sha256(Path(__file__).with_name("splitter.py").read_bytes()).hexdigest(),
                  "chunker": "bounded-h2-v1-parent-zero", "max_chars": embedder.EMBED_MAX_CHARS,
                  "builder": hashlib.sha256(inspect.getsource(_build_chunks).encode()).hexdigest(),
                  "source_url_prefix": settings.KB_GIT_SUBDIR})


def pipeline_profile():
    from app.rag.git_source import parser_profile
    # 绑定实际嵌入空间及清洗/切分代码；供应商静默换权重需人工提高epoch。
    return _hash({
        "embedding": embedding_profile(), "chunking": chunk_profile(),
        "parser": parser_profile(json.loads(Path(__file__).with_name("legacy_document_ids.json").read_text())),
        "source": settings.KB_SOURCE_ID, "subdir": settings.KB_GIT_SUBDIR,
    })


def configured_snapshot(stats=None):
    from app.rag.git_source import read_export, read_snapshot
    if settings.KB_GIT_EXPORT:
        return read_export(Path(settings.KB_GIT_EXPORT), cache_dir=stage_cache_dir(), stats=stats)
    return read_snapshot(Path(settings.KB_GIT_REPO), settings.KB_GIT_REF, settings.KB_GIT_SUBDIR,
                         cache_dir=stage_cache_dir(), stats=stats)


def stage_cache_dir():
    # 来源隔离；缓存可丢弃，固定快照字节仍是权威。
    return Path(settings.KB_SYNC_STATE_DIR) / "stages" / _hash(settings.KB_SOURCE_ID)


def job_out(job):
    report = json.loads(job.report_json or "{}")
    return {"id": job.id, "status": job.status, "source_commit": job.commit_sha,
            "collection": job.collection, "attempts": job.attempts, "error": job.error,
            "error_kind": job.error_kind,
            "next_attempt_at": job.next_attempt_at.replace(tzinfo=UTC).isoformat() if job.next_attempt_at else None,
            "failures": json.loads(job.failures_json or "[]"),
            "report": report, "created_at": job.created_at.isoformat() if job.created_at else None}


def get_job(job_id):
    with db_module.SessionLocal() as db:
        job = db.get(KbSyncJob, job_id)
        if job is None or job.source_id != settings.KB_SOURCE_ID:
            raise ValueError("同步任务不存在")
        return job_out(job)


def state():
    with db_module.SessionLocal() as db:
        source = db.get(KbSource, settings.KB_SOURCE_ID)
        active = db.get(KbSyncJob, source.active_job) if source and source.active_job else None
        latest = (db.query(KbSyncJob).filter_by(source_id=settings.KB_SOURCE_ID)
                  .order_by(KbSyncJob.seq.desc()).first())
        documents = _manifest(active) if active else {}
        denied = set(json.loads(source.denied_json)) if source else set()
        docs = []
        for doc_id, item in documents.items():
            valid_to = item["meta"].get("valid_to")
            try:
                days = (datetime.strptime(str(valid_to), "%Y-%m-%d").date() - _now().date()).days
            except ValueError:
                days = None
            docs.append({"id": doc_id, "path": item["path"],
                         "status": "removed" if doc_id in denied else item["status"],
                         "chunk_count": len(item["ids"]), "valid_to": valid_to,
                         "expiring_days": days, "changelog": f"Git {active.commit_sha[:12]}",
                         "updated_at": active.updated_at.isoformat()})
        docs.sort(key=lambda item: item["path"])
        ready = bool(latest and source and latest.status == "prepared" and latest.seq == source.desired_seq
                     and latest.profile == pipeline_profile())
        latest_out = job_out(latest) if latest else None
        if latest_out and latest.status == "prepared" and not ready:
            latest_out["status"] = "superseded"
        return {"mode": "git", "active_generation": active.id if active else None,
                "source_commit": active.commit_sha if active else None,
                "active_collection": active.collection if active else settings.active_collection,
                "candidate_collection": latest.collection if latest else None,
                "active_chunks": sum(d["chunk_count"] for d in docs if d["status"] == "active"),
                "candidate_chunks": sum(len(d["ids"]) for d in _manifest(latest).values()) if ready else 0,
                "candidate_ready": ready, "previous_generation": source.previous_job if source else None,
                "doc_count": sum(d["status"] == "active" for d in docs), "total_docs": len(docs),
                "removed_docs": sum(d["status"] == "removed" for d in docs),
                "last_sync": active.updated_at.isoformat() if active else None,
                "latest_job": latest_out, "extra_collections": {}, "docs": docs}


def enqueue(snapshot=None):
    # 快照落盘到SQL引用建立之间也不能与离线回收交错。
    from app.rag.maintenance import service_guard
    with service_guard():
        return _enqueue_locked(snapshot)


def _enqueue_locked(snapshot=None):
    discovery = {}
    snapshot = snapshot or configured_snapshot(stats=discovery)
    for attempt in range(3):
        try:
            return _enqueue_once(snapshot, discovery)
        except (IntegrityError, OperationalError) as exc:
            # 首次来源并发建立、MySQL行/间隙锁死锁可安全重试整个短事务。
            code = exc.orig.args[0] if exc.orig.args else None
            if code not in {1062, 1205, 1213} or attempt == 2:
                raise
            time.sleep(0.05 * (2 ** attempt))
    raise RuntimeError("来源入队重试耗尽")


def _enqueue_once(snapshot=None, discovery=None):
    from app.rag.git_source import write_export
    snapshot = snapshot or configured_snapshot()
    if not snapshot.docs:
        raise ValueError("空来源禁止发布；整库清空需单独管理流程")
    profile = pipeline_profile()
    snapshot_path = Path(settings.KB_SYNC_STATE_DIR) / "snapshots" / f"{snapshot.snapshot_hash}.json"
    # 先落可恢复字节，SQL只引用已经持久的产物。未入SQL的孤立快照不影响在岗。
    write_export(snapshot, snapshot_path, cache_dir=stage_cache_dir())
    with db_module.SessionLocal.begin() as db:
        source = db.query(KbSource).filter_by(id=settings.KB_SOURCE_ID).with_for_update().one_or_none()
        if source is None:
            source = KbSource(id=settings.KB_SOURCE_ID, desired_seq=0,
                              denied_json="[]", known_json="{}")
            db.add(source)
            db.flush()
        latest = (db.query(KbSyncJob).filter_by(source_id=source.id, seq=source.desired_seq).one_or_none())
        if latest and latest.snapshot_hash == snapshot.snapshot_hash and latest.profile == profile \
                and latest.status in {"queued", "running", "prepared", "published"}:
            return job_out(latest)
        source.desired_seq += 1
        known = json.loads(source.known_json)
        if not known:
            legacy = json.loads(Path(__file__).with_name("legacy_document_ids.json").read_text())
            # 迁移映射同样纳入第一次完整对账，防止首次同步前就删除的旧文档漏拦截。
            for path, doc_id in legacy.items():
                known.setdefault(doc_id, [path])
        active_ids = set()
        for doc in snapshot.docs:
            known.setdefault(doc.doc_id, [])
            if doc.rel_path not in known[doc.doc_id]:
                known[doc.doc_id].append(doc.rel_path)
            if doc.meta.get("status", "active") == "active":
                active_ids.add(doc.doc_id)
        denied = set(json.loads(source.denied_json))
        for doc_id in set(known) - active_ids:
            denied.add(doc_id)
            # 迁移前旧库按路径摘要标识，首次切代之前也必须拦住确认删除。
            denied.update(hashlib.sha256(p.encode()).hexdigest()[:16] for p in known[doc_id])
        source.known_json, source.denied_json = _json(known), _json(sorted(denied))
        job_id = uuid.uuid4().hex
        job = KbSyncJob(id=job_id, source_id=source.id, seq=source.desired_seq,
                       commit_sha=snapshot.commit, snapshot_hash=snapshot.snapshot_hash,
                       profile=profile, status="queued", fence=0, attempts=0,
                       collection=f"kb_gen_{job_id}", snapshot_path=str(snapshot_path),
                       report_json=_json({"discovery": discovery or {}}))
        db.add(job)
        db.flush()
        return job_out(job)


def _manifest(job):
    value = json.loads(job.manifest_json or "{}")
    if not value or value.get("digest") != _hash(value.get("documents")):
        raise RuntimeError("版本清单摘要校验失败")
    return value["documents"]


def _lease(job_id, fence):
    with db_module.SessionLocal.begin() as db:
        result = db.execute(update(KbSyncJob).where(
            KbSyncJob.id == job_id, KbSyncJob.fence == fence,
            KbSyncJob.status == "running", KbSyncJob.lease_until > _now(),
        ).values(lease_until=_now() + timedelta(seconds=LEASE_SECONDS)))
        if result.rowcount != 1:
            raise RuntimeError("同步任务租约已失效")


def _claim(job_id):
    with db_module.SessionLocal.begin() as db:
        source = db.query(KbSource).filter_by(id=settings.KB_SOURCE_ID).with_for_update().one()
        job = db.query(KbSyncJob).filter_by(id=job_id).with_for_update().one()
        if job.seq != source.desired_seq and job.status in {"queued", "running"}:
            job.status = "superseded"
            return None
        if job.status != "queued" and not (
            job.status == "running" and job.lease_until and job.lease_until <= _now()
        ):
            return None
        if job.status == "queued" and job.next_attempt_at and job.next_attempt_at > _now():
            return None
        if job.attempts >= MAX_ATTEMPTS:
            job.status, job.error = "failed", "任务恢复/重试次数达到上限，请重新同步"
            job.error_kind, job.next_attempt_at, job.lease_until = "retry_exhausted", None, None
            return None
        if job.status == "running":
            history = json.loads(job.failures_json or "[]")
            history.append({"attempt": job.attempts, "at": _now().replace(tzinfo=UTC).isoformat(),
                            "kind": "worker_lease_expired", "stage": "unknown",
                            "retryable": True, "summary": "上次工作进程租约到期，重新领取持久任务"})
            job.failures_json = _json(history[-MAX_ATTEMPTS:])
            db.flush()
        previous_fence = job.fence
        claimed = db.execute(update(KbSyncJob).where(
            KbSyncJob.id == job.id, KbSyncJob.fence == previous_fence,
            or_((KbSyncJob.status == "queued") & or_(KbSyncJob.next_attempt_at.is_(None), KbSyncJob.next_attempt_at <= _now()),
                (KbSyncJob.status == "running") & (KbSyncJob.lease_until <= _now())),
        ).values(status="running", attempts=job.attempts + 1, fence=previous_fence + 1,
                 lease_until=_now() + timedelta(seconds=LEASE_SECONDS), error=None,
                 error_kind=None, next_attempt_at=None))
        if claimed.rowcount != 1:
            return None
        db.refresh(job)
        db.expunge(job)
        return job


def _chunks(doc, revision, source_id, stats=None):
    from app.rag.stage_cache import cached_chunks
    return cached_chunks(doc, revision, source_id, chunk_profile(),
                         lambda: _build_chunks(doc, revision, source_id),
                         cache_dir=stage_cache_dir(), stats=stats)


def _build_chunks(doc, revision, source_id):
    metadata = dict(doc.meta)
    metadata["source_url"] = f"{settings.KB_GIT_SUBDIR.strip('/')}/{doc.rel_path}"
    namespaced = KnowledgeDoc(doc.rel_path, f"{_hash(source_id)[:12]}:{doc.doc_id}:{revision}",
                              metadata, doc.content)
    split = split_markdown(namespaced)
    chunks = [split.parent]
    # 子块不得依赖embedder截断：长章节显式拆小，正文与否定/数字均保留。
    for child in split.children:
        for start in range(0, len(child.text), embedder.EMBED_MAX_CHARS):
            text = child.text[start:start + embedder.EMBED_MAX_CHARS]
            chunks.append(Chunk(f"{child.id}:{start}", text, dict(child.metadata)))
    for chunk in chunks:
        chunk.metadata.update(doc_id=doc.doc_id, source_id=source_id, doc_revision=revision)
    return chunks


def _valid_vector(vector):
    return (isinstance(vector, list) and len(vector) == embedder.EMBED_DIM
            and all(isinstance(v, (int, float)) and math.isfinite(v) for v in vector))


def _vectors(chunks, profile, report):
    vectors = {}
    misses = {}
    cache_root = Path(settings.KB_SYNC_STATE_DIR) / "embeddings" / profile
    for chunk in chunks:
        if chunk.metadata["chunk_type"] == "parent":
            vectors[chunk.id] = [0.0] * embedder.EMBED_DIM
            continue
        cleaned = embedder._clean_text(chunk.text)
        key = hashlib.sha256(cleaned.encode()).hexdigest()
        path = cache_root / f"{key}.json"
        try:
            cached = json.loads(path.read_text())
            vector = cached["vector"]
            valid = (cached["input_hash"] == key and cached["profile"] == profile
                     and cached["vector_hash"] == _hash(vector) and _valid_vector(vector))
        except (OSError, ValueError, KeyError, TypeError):
            valid = False
        if valid:
            vectors[chunk.id] = vector
            report["reused_vectors"] += 1
        else:
            misses.setdefault(key, {"text": cleaned, "ids": []})["ids"].append(chunk.id)
    if misses:
        items = list(misses.items())
        for start in range(0, len(items), embedder.BATCH_SIZE):
            batch = items[start:start + embedder.BATCH_SIZE]
            # 每个成功批次立刻缓存，后续批次失败不会丢掉已完成计算。
            # 任务调度层统一重试，每次任务尝试每个API批次最多发起一次调用。
            with embedder.single_attempt(stats=report):
                values = embedder.embed_texts([item["text"] for _, item in batch])
            if len(values) != len(batch) or not all(_valid_vector(v) for v in values):
                raise RuntimeError("嵌入响应数量/维度/有限值校验失败")
            report["embedding_inputs"] += len(batch)
            for (key, item), vector in zip(batch, values, strict=True):
                _atomic_json(cache_root / f"{key}.json", {"input_hash": key, "profile": profile,
                             "vector_hash": _hash(vector), "vector": vector})
                for cid in item["ids"]:
                    vectors[cid] = vector
    return [vectors[chunk.id] for chunk in chunks]


def _record_failure(job, exc, stage, report):
    from app.rag.retry_policy import classify, retry_delay
    failure = classify(exc, stage)
    with db_module.SessionLocal.begin() as db:
        source = db.query(KbSource).filter_by(id=job.source_id).with_for_update().one()
        current = db.query(KbSyncJob).filter_by(id=job.id).with_for_update().one()
        if (current.fence != job.fence or current.status != "running"
                or current.lease_until is None or current.lease_until <= _now()):
            return
        eligible = failure.retryable and current.attempts < MAX_ATTEMPTS
        current.status = ("superseded" if source.desired_seq != job.seq else
                          "queued" if eligible else "failed")
        delay = retry_delay(failure, current.attempts) if current.status == "queued" else None
        current.next_attempt_at = _now() + timedelta(seconds=delay) if delay is not None else None
        current.lease_until = None
        current.error_kind = failure.kind
        current.error = failure.summary
        if failure.retryable and not eligible:
            current.error += "；自动重试预算已耗尽"
        history = json.loads(current.failures_json or "[]")
        history.append({"attempt": current.attempts, "at": _now().replace(tzinfo=UTC).isoformat(),
                        "kind": failure.kind, "stage": stage, "status_code": failure.status_code,
                        "retryable": failure.retryable, "scheduled_delay_seconds": delay,
                        "embedding_requests_attempted": report.get("embedding_requests_attempted", 0),
                        "embedding_inputs_attempted": report.get("embedding_inputs_attempted", 0),
                        "embedding_inputs_completed": report.get("embedding_inputs", 0),
                        "stages": dict(report.get("stages", {})),
                        "summary": failure.summary})
        current.failures_json = _json(history[-MAX_ATTEMPTS:])
        current.report_json = _json(report)
    frames = [f"{Path(frame.filename).name}:{frame.lineno}:{frame.name}"
              for frame in traceback.extract_tb(exc.__traceback__)]
    # 供应商原文可能包含请求正文/凭据；只记录安全类别与不含局部变量的代码位置。
    logger.error("同步失败 job=%s stage=%s kind=%s attempt=%s frames=%s",
                 job.id, stage, failure.kind, job.attempts, frames)


def process_job(job_id):
    """一次有效构建，同机锁+SQL租约。掉电后running到期可重新领取。"""
    with publication_lock():
        job = _claim(job_id)
        if job is None:
            return get_job(job_id)
        stop = threading.Event()

        def renew():
            while not stop.wait(15):
                try:
                    _lease(job.id, job.fence)
                except Exception:
                    logger.exception("同步任务心跳失败 %s", job.id)
                    return

        heartbeat = threading.Thread(target=renew, daemon=True)
        heartbeat.start()
        started = time.perf_counter()
        report = {"added": [], "updated": [], "deleted": [], "skipped_count": 0,
                  "attempt": job.attempts,
                  "embedding_inputs": 0, "reused_vectors": 0,
                  "discovery": json.loads(job.report_json or "{}").get("discovery", {}), "stages": {}}
        stage = "snapshot"
        try:
            from app.rag.git_source import read_export
            snapshot = read_export(Path(job.snapshot_path), cache_dir=stage_cache_dir(), stats=report["stages"])
            if snapshot.snapshot_hash != job.snapshot_hash or snapshot.commit != job.commit_sha:
                raise RuntimeError("恢复快照与持久任务目标不一致")
            stage = "profile"
            if pipeline_profile() != job.profile:
                raise RuntimeError("流水线配置已变化，请重新同步")
            stage = "index_read"
            with db_module.SessionLocal() as db:
                source = db.get(KbSource, job.source_id)
                previous = db.get(KbSyncJob, source.active_job) if source.active_job else None
                old_manifest = _manifest(previous) if previous else {}
                previous_store = (ChromaStore(settings.KB_VECTOR_DIR, previous.collection, must_exist=True)
                                  if previous else None)
            target = ChromaStore(settings.KB_VECTOR_DIR, job.collection)
            target.set_build_ready(False)
            documents, expected_ids = {}, set()
            for doc in snapshot.docs:
                _lease(job.id, job.fence)
                # 解析器升级但输出不变时，复用切分/向量；job仍记录完整流水线profile。
                revision = _hash({"body": doc.content, "meta": doc.meta,
                                  "chunking": chunk_profile(), "embedding": embedding_profile()})
                stage = "chunking"
                chunks = _chunks(doc, revision, job.source_id, stats=report["stages"])
                ids = [c.id for c in chunks]
                old = old_manifest.get(doc.doc_id)
                if old and old["revision"] == revision and previous_store:
                    vectors_by_id = previous_store.get_vectors(ids)
                    previous_chunks = previous_store.get_chunks(ids)
                    if (set(vectors_by_id) == set(ids) and all(_valid_vector(v) for v in vectors_by_id.values())
                            and all(previous_chunks.get(c.id) == {"text": c.text, "metadata": c.metadata} for c in chunks)):
                        vectors = [vectors_by_id[cid] for cid in ids]
                        report["reused_vectors"] += len(chunks) - 1
                    else:
                        stage = "embedding"
                        vectors = _vectors(chunks, embedding_profile(), report)
                    report["skipped_count"] += 1
                else:
                    stage = "embedding"
                    vectors = _vectors(chunks, embedding_profile(), report)
                    report["updated" if old else "added"].append(doc.rel_path)
                stage = "index_write"
                target.upsert_chunks(chunks, vectors)
                got = target.get_chunks(ids)
                if any(got.get(c.id) != {"text": c.text, "metadata": c.metadata} for c in chunks):
                    raise RuntimeError("候选块正文/版本/元数据写后核对失败")
                documents[doc.doc_id] = {"path": doc.rel_path, "revision": revision, "ids": ids,
                                          "status": doc.meta.get("status", "active"), "meta": doc.meta,
                                          "raw_hash": snapshot.raw_hashes[doc.doc_id]}
                expected_ids.update(ids)
            stage = "validation"
            report["deleted"] = [v["path"] for key, v in old_manifest.items() if key not in documents]
            # 重试可能留下过期局部块：清理仅限从未对外发布的本任务候选。
            unexpected = set(target.get_all()) - expected_ids
            target.delete_ids(sorted(unexpected))
            if set(target.get_all()) != expected_ids:
                raise RuntimeError("候选全量块清单核对失败")
            stored_vectors = target.get_vectors(sorted(expected_ids))
            if set(stored_vectors) != expected_ids or not all(_valid_vector(v) for v in stored_vectors.values()):
                raise RuntimeError("候选向量完整性核对失败")
            Bm25Index(target)._ensure_built()
            _lease(job.id, job.fence)
            target.set_build_ready(True)
            report["elapsed_ms"] = round((time.perf_counter() - started) * 1000, 2)
            stage = "finalize"
            with db_module.SessionLocal.begin() as db:
                source = db.query(KbSource).filter_by(id=job.source_id).with_for_update().one()
                status = "prepared" if source.desired_seq == job.seq else "superseded"
                changed = db.execute(update(KbSyncJob).where(
                    KbSyncJob.id == job.id, KbSyncJob.status == "running", KbSyncJob.fence == job.fence,
                    KbSyncJob.lease_until > _now(),
                ).values(status=status, manifest_json=_json({"documents": documents, "digest": _hash(documents)}),
                         report_json=_json(report), lease_until=None))
                if changed.rowcount != 1:
                    raise RuntimeError("完成阶段租约校验失败")
        except Exception as exc:
            report["elapsed_ms"] = round((time.perf_counter() - started) * 1000, 2)
            try:
                _record_failure(job, exc, stage, report)
            except Exception as persist_error:
                # DB故障时无法持久退避，保留running等待租约恢复；避免异常链泄露上游正文。
                logger.error("同步失败状态写入失败 job=%s type=%s；等待租约恢复",
                             job.id, type(persist_error).__name__)
        finally:
            stop.set()
            heartbeat.join(timeout=2)
        return get_job(job.id)


def _verify_generation(job):
    from app.rag.git_source import read_export
    documents = _manifest(job)
    store = ChromaStore(settings.KB_VECTOR_DIR, job.collection, must_exist=True)
    if not store.is_build_ready():
        raise RuntimeError("索引没有完整构建标记")
    snapshot = read_export(Path(job.snapshot_path), cache_dir=stage_cache_dir())
    if snapshot.snapshot_hash != job.snapshot_hash or snapshot.commit != job.commit_sha:
        raise RuntimeError("版本来源快照不一致")
    expected = {}
    for doc in snapshot.docs:
        item = documents.get(doc.doc_id)
        if item is None:
            raise RuntimeError("版本文档清单不完整")
        for chunk in _chunks(doc, item["revision"], job.source_id):
            expected[chunk.id] = {"text": chunk.text, "metadata": chunk.metadata}
    if set(expected) != {cid for item in documents.values() for cid in item["ids"]} or store.get_all_meta() != expected:
        raise RuntimeError("待发布索引正文、元数据或精确块清单校验失败")
    vectors = store.get_vectors(list(expected))
    if set(vectors) != set(expected) or not all(_valid_vector(v) for v in vectors.values()):
        raise RuntimeError("待发布向量不完整")
    Bm25Index(store)._ensure_built()
    return documents


def publish(job_id):
    with publication_lock(), db_module.SessionLocal.begin() as db:
        source = db.query(KbSource).filter_by(id=settings.KB_SOURCE_ID).with_for_update().one()
        job = db.query(KbSyncJob).filter_by(id=job_id, source_id=source.id).with_for_update().one()
        if source.active_job == job.id:
            return job_out(job)
        if job.status != "prepared" or source.desired_seq != job.seq or job.profile != pipeline_profile():
            raise ValueError("任务尚未准备完毕或已过期，禁止发布")
        documents = _verify_generation(job)
        source.previous_job, source.active_job = source.active_job, job.id
        # 恢复必须等对应新正文完整发布，单纯在来源中重新出现不能解除删除。
        restored = {key for key, d in documents.items() if d["status"] == "active"}
        denied = set(json.loads(source.denied_json)) - restored
        known = json.loads(source.known_json)
        for key in restored:
            denied.difference_update(hashlib.sha256(p.encode()).hexdigest()[:16] for p in known[key])
        source.denied_json = _json(sorted(denied))
        job.status = "published"
        return job_out(job)


def rollback():
    with publication_lock(), db_module.SessionLocal.begin() as db:
        source = db.query(KbSource).filter_by(id=settings.KB_SOURCE_ID).with_for_update().one()
        if not source.previous_job:
            raise ValueError("没有可回滚版本")
        previous = db.get(KbSyncJob, source.previous_job)
        _verify_generation(previous)
        source.active_job, source.previous_job = source.previous_job, source.active_job
        source.desired_seq += 1  # 回滚也是新的发布意图，阻止仍在构建的旧任务随后覆盖。
        return job_out(previous)


def denied_ids():
    with db_module.SessionLocal() as db:
        source = db.get(KbSource, settings.KB_SOURCE_ID)
        return set(json.loads(source.denied_json)) if source else set()


class GenerationStore(ChromaStore):
    """单次查询固定一代，独立删除限制每次读都重新检查。"""
    def upsert_chunks(self, chunks, vectors):
        raise RuntimeError("查询版本不可修改，请创建新同步任务")

    def set_build_ready(self, ready):
        raise RuntimeError("查询版本不可修改就绪标记")

    def delete_ids(self, ids):
        raise RuntimeError("查询版本不可删除")

    def delete_by_doc_id(self, doc_id):
        raise RuntimeError("查询版本不可删除")

    def clear(self):
        raise RuntimeError("查询版本不可清空")

    def query(self, vector, top_k, where=None):
        denied = denied_ids()
        if denied:
            exclusion = {"doc_id": {"$nin": sorted(denied)}}
            where = {"$and": [where, exclusion]} if where else exclusion
        return super().query(vector, top_k, where)

    def get_all_meta(self):
        denied = denied_ids()
        return {key: item for key, item in super().get_all_meta().items()
                if item["metadata"].get("doc_id") not in denied}

    def get_chunks(self, ids):
        denied = denied_ids()
        return {key: item for key, item in super().get_chunks(ids).items()
                if item["metadata"].get("doc_id") not in denied}

    def get_by_ids(self, ids):
        return {key: item["text"] for key, item in self.get_chunks(ids).items()}

    def count(self):
        return len(self.get_all_meta())


def resolve_store(collection_name=None):
    with db_module.SessionLocal() as db:
        source = db.get(KbSource, settings.KB_SOURCE_ID)
        active = db.get(KbSyncJob, source.active_job) if source and source.active_job else None
        name = collection_name or (active.collection if active else settings.active_collection)
        if name.startswith("kb_gen_"):
            job = db.query(KbSyncJob).filter_by(collection=name, source_id=settings.KB_SOURCE_ID).one_or_none()
            if not job or job.status not in {"prepared", "published", "superseded"}:
                raise RuntimeError("索引版本不可查询")
            manifest = _manifest(job)
            store = GenerationStore(settings.KB_VECTOR_DIR, name, must_exist=True)
            expected_count = sum(len(item["ids"]) for item in manifest.values())
            if not store.is_build_ready() or store._col.count() != expected_count:
                raise RuntimeError("索引版本未完整构建")
            return store
        # 首次迁移前旧库继续服务；删除限制同样作用于旧路径ID。
        if collection_name is None:
            return GenerationStore(settings.KB_VECTOR_DIR, name, must_exist=True)
        return ChromaStore(settings.KB_VECTOR_DIR, name)


def filter_hits(hits):
    denied = denied_ids()
    return [hit for hit in hits if hit.metadata.get("doc_id") not in denied]


def run_worker(stop):
    while not stop.wait(2):
        try:
            with db_module.SessionLocal() as db:
                job = (db.query(KbSyncJob).filter(KbSyncJob.source_id == settings.KB_SOURCE_ID,
                    or_((KbSyncJob.status == "queued") & or_(KbSyncJob.next_attempt_at.is_(None), KbSyncJob.next_attempt_at <= _now()),
                        (KbSyncJob.status == "running") & (KbSyncJob.lease_until <= _now())))
                    .order_by(KbSyncJob.seq.desc()).first())
                job_id = job.id if job else None
            if job_id:
                process_job(job_id)
        except Exception:
            logger.exception("同步worker本轮失败，将继续读取持久任务")


def main():
    import argparse
    parser = argparse.ArgumentParser(description="从固定Git来源构建并发布增量版本")
    parser.add_argument("--publish", action="store_true")
    parser.add_argument("--timeout", type=int, default=900)
    args = parser.parse_args()
    db_module.init_db()
    job = enqueue()
    deadline = time.monotonic() + args.timeout
    while job["status"] in {"queued", "running"} and time.monotonic() < deadline:
        # 构建由服务中的持久worker执行；CLI仅等待，timeout不会被模型请求阻塞。
        time.sleep(2)
        job = get_job(job["id"])
    if args.publish and job["status"] == "prepared":
        job = publish(job["id"])
    print(_json(job))
    if job["status"] not in ({"published"} if args.publish else {"prepared", "published"}):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
