"""同步失败分类：只有已知瞬时错误自动重试，不从异常文案猜测。"""
import errno
import math
import random
import ssl
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

import httpx
from openai import APIConnectionError, APIStatusError, APITimeoutError
from sqlalchemy.exc import OperationalError
from sqlalchemy.exc import TimeoutError as DatabaseTimeoutError

MAX_RETRY_DELAY = 300.0
BASE_DELAY = 2.0


@dataclass(frozen=True)
class Failure:
    kind: str
    retryable: bool
    summary: str
    status_code: int | None = None
    retry_after: float | None = None


def _retry_after(headers):
    try:
        if headers.get("retry-after-ms"):
            seconds = float(headers["retry-after-ms"]) / 1000
        elif headers.get("retry-after"):
            value = headers["retry-after"]
            try:
                seconds = float(value)
            except ValueError:
                when = parsedate_to_datetime(value)
                if when.tzinfo is None:
                    when = when.replace(tzinfo=UTC)
                seconds = (when - datetime.now(UTC)).total_seconds()
        else:
            return None
        return max(0.0, seconds) if math.isfinite(seconds) else None
    except (TypeError, ValueError, OverflowError):
        return None


def classify(exc: Exception, stage: str = "unknown") -> Failure:
    """沿显式cause链查SDK/IO类型；摘要不保存供应商正文、URL或密钥。"""
    chain, seen = [], set()
    current = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        chain.append(current)
        current = current.__cause__
    for error in reversed(chain):
        if isinstance(error, ssl.SSLCertVerificationError):
            return Failure("upstream_tls_configuration", False, "模型服务TLS证书验证失败，需修正证书或连接配置")
        if isinstance(error, (APITimeoutError, httpx.TimeoutException, TimeoutError)):
            if isinstance(error, TimeoutError) and stage in {"snapshot", "index_read", "index_write", "validation"}:
                return Failure("storage_transient", True, "存储读写超时，等待有限重试")
            return Failure("upstream_timeout", True, "上游请求超时，等待有限重试")
        if isinstance(error, (APIConnectionError, httpx.NetworkError, ConnectionError)):
            return Failure("upstream_connection", True, "上游连接失败，等待有限重试")
        if isinstance(error, (APIStatusError, httpx.HTTPStatusError)):
            status = error.status_code if isinstance(error, APIStatusError) else error.response.status_code
            headers = error.response.headers
            if error.request.url.host == "bigmodel.cn" or error.request.url.host.endswith(".bigmodel.cn"):
                code = str(getattr(error, "code", ""))
                # 智谱将欠费/套餐过期/长周期配额也编码为429，不能当短时限流循环重试。
                if code in {"1113", "1308", "1309", "1310", "1311", "1313", "1314", "1315",
                            "1316", "1317", "1318", "1319", "1320", "1321"}:
                    return Failure("upstream_quota", False, "供应商额度、套餐或访问配置需人工处理", status)
            delay = _retry_after(headers)
            transient = status in {408, 409, 429} or 500 <= status < 600
            if headers.get("x-should-retry", "").lower() == "false":
                transient = False
            if delay is not None and delay > MAX_RETRY_DELAY:
                return Failure("provider_long_wait", False, "上游要求等待超过自动重试窗口，请稍后重新提交", status, delay)
            kind = ("upstream_rate_limit" if status == 429 else
                    "upstream_server" if status >= 500 else
                    "upstream_auth" if status in {401, 403} else "upstream_request")
            return Failure(kind, transient, f"上游返回HTTP {status}，" + ("等待有限重试" if transient else "需修正请求或配置"), status, delay)
        if isinstance(error, DatabaseTimeoutError):
            return Failure("database_busy", True, "数据库连接繁忙，等待有限重试")
        if isinstance(error, OperationalError):
            code = error.orig.args[0] if error.orig.args else None
            retryable = code in {1040, 1205, 1213, 2006, 2013} or "locked" in str(error.orig).lower()
            return Failure("database_transient" if retryable else "database_configuration", retryable,
                           "数据库暂时不可用" if retryable else "数据库结构或配置需检查")
        if isinstance(error, PermissionError):
            return Failure("storage_permission", False, "文件权限不足，需人工修正")
        if isinstance(error, FileNotFoundError):
            return Failure("artifact_missing", False, "持久快照或索引产物缺失，需人工检查")
        if isinstance(error, OSError):
            if error.errno == errno.ENOSPC:
                return Failure("storage_full", False, "存储空间不足，需人工处理")
            retryable = error.errno in {errno.EAGAIN, errno.EINTR, errno.EIO, errno.ETIMEDOUT, errno.ECONNRESET}
            return Failure("storage_transient" if retryable else "storage_error", retryable, "存储读写失败")
        if isinstance(error, ValueError):
            return Failure("source_validation", False, "来源或数据校验失败，需修正文档或快照")
    if stage == "profile":
        return Failure("pipeline_changed", False, "流水线配置已变化，请重新同步")
    if stage in {"snapshot", "validation"}:
        return Failure("integrity_error", False, "来源或索引完整性校验失败，禁止发布")
    return Failure("internal_error", False, f"同步阶段异常（{type(exc).__name__}），需检查服务日志")


def retry_delay(failure: Failure, attempt: int) -> float:
    base = min(MAX_RETRY_DELAY, BASE_DELAY * (2 ** max(0, attempt - 1)))
    jittered = min(MAX_RETRY_DELAY, base + random.uniform(0, base * 0.25))
    return min(MAX_RETRY_DELAY, max(jittered, failure.retry_after or 0.0))
