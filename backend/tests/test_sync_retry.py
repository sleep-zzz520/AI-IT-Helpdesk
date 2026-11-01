"""分类、任务退避和SDK请求预算；真实SQL/Chroma，替身只在外部HTTP边界。"""
import errno
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest
from openai import APIConnectionError, APIStatusError, APITimeoutError

from app import db as db_module
from app.models import KbSyncJob
from app.rag import embedder
from app.rag import generations as g
from app.rag.retry_policy import BASE_DELAY, MAX_RETRY_DELAY, classify, retry_delay
from tests.test_generations import env as _generation_env
from tests.test_generations import snapshot

REAL_EMBED_TEXTS = embedder.embed_texts


@pytest.fixture
def env(request):
    return request.getfixturevalue("_generation_env")


def status_error(status, *, code=None, headers=None, host="open.bigmodel.cn"):
    request = httpx.Request("POST", f"https://{host}/api/embeddings")
    response = httpx.Response(status, request=request, headers=headers or {})
    return APIStatusError("private-secret-body", response=response, body={"code": code})


@pytest.mark.parametrize(("status", "retryable"), [
    (400, False), (401, False), (403, False), (404, False), (422, False),
    (408, True), (409, True), (429, True), (500, True), (503, True),
])
def test_http_status_classification_omits_private_provider_text(status, retryable):
    failure = classify(status_error(status))
    assert failure.retryable is retryable
    assert failure.status_code == status
    assert "private-secret-body" not in failure.summary


@pytest.mark.parametrize("code", ["1113", "1308", "1309", "1310", "1311", "1313", "1314", "1315", "1316", "1321"])
def test_bigmodel_429_quota_or_plan_is_not_short_lived_rate_limit(code):
    failure = classify(status_error(429, code=code))
    assert failure.kind == "upstream_quota" and not failure.retryable


@pytest.mark.parametrize("code", ["1302", "1305"])
def test_bigmodel_temporary_rate_limit_can_retry(code):
    assert classify(status_error(429, code=code)).retryable


def test_provider_code_mapping_is_not_applied_to_another_vendor():
    assert classify(status_error(429, code="1113", host="example.invalid")).retryable


@pytest.mark.parametrize("error", [
    APITimeoutError(httpx.Request("POST", "https://example.invalid")),
    APIConnectionError(request=httpx.Request("POST", "https://example.invalid")),
    httpx.ReadTimeout("timeout"), OSError(errno.EIO, "disk transient"),
])
def test_known_transient_error_types_can_retry(error):
    assert classify(error).retryable


@pytest.mark.parametrize("error", [PermissionError("secret path"), FileNotFoundError("missing"),
                                       OSError(errno.ENOSPC, "full"), ValueError("invalid"), RuntimeError("unknown")])
def test_permanent_or_unknown_errors_fail_closed(error):
    assert not classify(error).retryable


def test_retry_after_and_explicit_provider_no_retry():
    failure = classify(status_error(429, headers={"Retry-After": "8"}))
    assert failure.retry_after == 8 and retry_delay(failure, 1) == 8
    assert not classify(status_error(503, headers={"x-should-retry": "false"})).retryable
    assert not classify(status_error(429, headers={"Retry-After": "10000"})).retryable
    for value in ("bad", "nan", "inf"):
        assert classify(status_error(429, headers={"Retry-After": value})).retry_after is None
    dated = classify(status_error(429, headers={"Retry-After": "Wed, 01 Jan 2000 00:00:00 GMT"}))
    assert dated.retry_after == 0


def test_backoff_has_jitter_and_is_bounded():
    failure = classify(status_error(503))
    for attempt in range(1, 12):
        base = min(MAX_RETRY_DELAY, BASE_DELAY * 2 ** (attempt - 1))
        delay = retry_delay(failure, attempt)
        assert base <= delay <= min(MAX_RETRY_DELAY, base * 1.25)


def due_now(job_id):
    with db_module.SessionLocal.begin() as db:
        db.get(KbSyncJob, job_id).next_attempt_at = g._now() - timedelta(seconds=1)


def test_transient_error_schedules_persisted_retry_then_recovers(env):
    job = g.enqueue(snapshot())
    calls = Mock(side_effect=[status_error(429, code="1302", headers={"Retry-After": "6"}), env.embed(["alpha"]), env.embed(["beta"])])
    env.patch.setattr(embedder, "embed_texts", calls)
    started = g._now()
    first = g.process_job(job["id"])
    assert first["status"] == "queued" and first["attempts"] == 1
    assert first["error_kind"] == "upstream_rate_limit"
    assert first["failures"][0]["scheduled_delay_seconds"] == 6
    with db_module.SessionLocal() as db:
        assert db.get(KbSyncJob, job["id"]).next_attempt_at >= started + timedelta(seconds=6)
    g.process_job(job["id"])
    assert calls.call_count == 1, "backoff到期前不应重新调用供应商"
    due_now(job["id"])
    result = g.process_job(job["id"])
    assert result["status"] == "prepared" and result["attempts"] == 2
    assert len(result["failures"]) == 1
    assert result["report"]["attempt"] == 2
    assert result["failures"][0]["stages"]["chunked_documents"] == 1
    assert result["report"]["stages"]["chunked_documents"] == 1
    assert result["error"] is None and result["next_attempt_at"] is None
    g.publish(job["id"])


def test_retry_budget_is_shared_with_sdk_and_never_multiplies(env):
    # 走真正embed_texts/_embed_batch；只在SDK transport边界持续返回503。
    env.patch.setattr(embedder, "embed_texts", REAL_EMBED_TEXTS)
    create = Mock(side_effect=status_error(503))
    env.patch.setattr(embedder, "_client", SimpleNamespace(embeddings=SimpleNamespace(create=create)))
    job = g.enqueue(snapshot(a="alpha", b=None))
    for attempt in range(1, 4):
        due_now(job["id"])
        result = g.process_job(job["id"])
        assert result["attempts"] == attempt and create.call_count == attempt
        assert result["report"]["embedding_requests_attempted"] == 1
    assert result["status"] == "failed" and len(result["failures"]) == 3
    assert "预算已耗尽" in result["error"]
    g.process_job(job["id"])
    assert create.call_count == 3


@pytest.mark.parametrize(("status", "code"), [(401, "1000"), (400, "1210"), (429, "1113")])
def test_permanent_provider_failure_is_one_attempt_and_no_sensitive_error_text(env, status, code):
    job = g.enqueue(snapshot())
    call = Mock(side_effect=status_error(status, code=code))
    env.patch.setattr(embedder, "embed_texts", call)
    result = g.process_job(job["id"])
    assert result["status"] == "failed" and result["attempts"] == call.call_count == 1
    assert result["next_attempt_at"] is None
    assert "private-secret-body" not in str(result)


def test_failed_later_batch_reuses_confirmed_first_batch_on_retry(env):
    env.patch.setattr(embedder, "BATCH_SIZE", 1)
    body = "# VPN\n## First\nfirst unique input\n## Second\nsecond unique input"
    job = g.enqueue(snapshot(a=body, b=None))
    inputs = []
    def faulty(texts):
        inputs.extend(texts)
        if len(inputs) == 2:
            raise status_error(503)
        return env.embed(texts)
    env.patch.setattr(embedder, "embed_texts", faulty)
    first = g.process_job(job["id"])
    assert first["status"] == "queued" and first["report"]["embedding_inputs"] == 1
    due_now(job["id"])
    result = g.process_job(job["id"])
    assert result["status"] == "prepared"
    assert len(inputs) == 3 and inputs[1] == inputs[2] and inputs.count(inputs[0]) == 1


def test_new_intent_supersedes_waiting_retry(env):
    first = g.enqueue(snapshot())
    env.patch.setattr(embedder, "embed_texts", Mock(side_effect=status_error(503)))
    assert g.process_job(first["id"])["status"] == "queued"
    second = g.enqueue(snapshot(2, a="new alpha"))
    assert second["id"] != first["id"]
    assert g.process_job(first["id"])["status"] == "superseded"


def test_failure_state_database_write_error_retains_lease_recovery_without_leaking_provider_body(env, caplog):
    job = g.enqueue(snapshot())
    record = g._record_failure
    env.patch.setattr(embedder, "embed_texts", Mock(side_effect=status_error(503)))
    env.patch.setattr(g, "_record_failure", Mock(side_effect=RuntimeError("private-database-detail")))
    result = g.process_job(job["id"])
    assert result["status"] == "running"
    assert "private-secret-body" not in caplog.text and "private-database-detail" not in caplog.text
    env.patch.setattr(g, "_record_failure", record)
    env.patch.setattr(embedder, "embed_texts", env.embed)
    with db_module.SessionLocal.begin() as db:
        db.get(KbSyncJob, job["id"]).lease_until = g._now() - timedelta(seconds=1)
    recovered = g.process_job(job["id"])
    assert recovered["status"] == "prepared" and recovered["attempts"] == 2
    assert recovered["failures"][0]["kind"] == "worker_lease_expired"
