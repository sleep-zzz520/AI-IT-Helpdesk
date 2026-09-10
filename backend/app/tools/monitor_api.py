"""监控 API（Tool）：查询账号证书状态 + 续期证书。

【架构演进】——从 mock 字典到真实 HTTP 对接

原来（P0）：用内存字典 MOCK_USERS 模拟，check_cert_status 直接读字典。
现在（P2）：通过真实 HTTP 调用本地监控服务（mock_monitor/server.py），
           走真实网络栈、超时、重试、错误归一化。

【为什么这么改】
1. 真实企业监控系统接触不到，但 Agent 必须证明"能对接真实 HTTP 系统"。
   本地起一个 FastAPI 监控服务，接口契约完全模拟企业系统（GET/POST + JSON）。
2. 换真实系统时：只改 MONITOR_BASE_URL，本文件零改动（契约稳定）。
3. 节点代码（verify.py / execute 节点）零改动——它们依赖的是函数签名，不是实现。
   这正是 P0 "mock 先行，契约与实现分离"设计决策的红利。

【双模式开关】MONITOR_MODE=mock|real
- mock：读内存字典（离线测试，零依赖，P0 行为不变）
- real：HTTP 调用监控服务（演示真实对接能力）

返回格式（契约，两种模式完全一致）：
- 成功: {"status": "ok", "expired": bool, "cert_valid_until": str}
- 失败: {"status": "error", "reason": str}
"""
import logging

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

# ===== mock 数据（仅 MONITOR_MODE=mock 时使用，保持 P0 行为不变）=====
MOCK_USERS = {
    "zhangsan": {"cert_valid_until": "2026-07-30", "expired": True},   # 已过期
    "lisi":     {"cert_valid_until": "2027-01-15", "expired": False},  # 正常
    "error_user": {"cert_valid_until": "2026-07-30", "expired": True},  # 已过期 + 续期必失败
}

# ===== 配置（real 模式）=====
# 从 settings 读，env 缺失时给安全默认值
MONITOR_MODE: str = getattr(settings, "MONITOR_MODE", "mock")
MONITOR_BASE_URL: str = getattr(settings, "MONITOR_BASE_URL", "http://127.0.0.1:9100")
# 真实系统会有认证 token；这里预留字段，换真实系统时从 settings 注入
MONITOR_API_TOKEN: str = getattr(settings, "MONITOR_API_TOKEN", "")
# HTTP 超时（秒）——真实网络必须设超时，否则 Agent 会卡死
MONITOR_TIMEOUT: float = getattr(settings, "MONITOR_TIMEOUT", 5.0)
# 失败重试次数（网络抖动很常见，重试是基本盘）
MONITOR_MAX_RETRIES: int = getattr(settings, "MONITOR_MAX_RETRIES", 2)


# ===== mock 实现（P0 行为，保留作离线测试兜底）=====
def _mock_check_cert(username: str) -> dict:
    info = MOCK_USERS.get(username)
    if info is None:
        return {"status": "error", "reason": f"账号 {username} 不存在或无查询权限"}
    return {"status": "ok", **info}


def _mock_renew_cert(username: str) -> dict:
    if username == "error_user":
        return {"status": "error", "reason": "证书服务当前不可达，续期失败"}
    MOCK_USERS[username]["cert_valid_until"] = "2027-01-15"
    MOCK_USERS[username]["expired"] = False
    return {"status": "ok", "message": f"已为用户 {username} 自动续期证书，有效期至 2027-01-15"}


# ===== real 实现（HTTP 调用，核心新增）=====
def _http_headers(operation_id: str | None = None) -> dict:
    """构造请求头。真实系统会用 Bearer token / API key 认证。"""
    h = {"Content-Type": "application/json"}
    if MONITOR_API_TOKEN:
        h["Authorization"] = f"Bearer {MONITOR_API_TOKEN}"
    if operation_id:
        h["Idempotency-Key"] = operation_id
    return h


def _http_get_cert(username: str) -> dict:
    """真实 HTTP 查询证书状态，带超时 + 重试 + 错误归一化。

    【关键工程点】
    1. 超时：httpx.get 不设 timeout 会无限等，Agent 卡死。必须显式设。
    2. 重试：网络偶发抖动（503/超时）重试几次往往就好。但 4xx 不重试（语义错误，重试无用）。
    3. 错误归一化：HTTP 层各种异常（超时/连接失败/4xx/5xx）统一转成契约里的 error 格式，
       节点代码不用关心底层是哪种 HTTP 错误，只认 {"status":"error","reason":...}。
       这是"契约稳定"的核心——把不稳定的外部依赖收敛成稳定的内部接口。
    """
    url = f"{MONITOR_BASE_URL}/api/v1/cert/{username}"
    last_exc = None
    for attempt in range(1, MONITOR_MAX_RETRIES + 2):  # 1 次正常 + N 次重试
        try:
            resp = httpx.get(url, headers=_http_headers(), timeout=MONITOR_TIMEOUT)
            if resp.status_code == 404:
                return {"status": "error", "reason": f"账号 {username} 不存在或无查询权限"}
            if resp.status_code >= 500:
                # 5xx 可重试（服务端临时故障）
                last_exc = RuntimeError(f"监控服务返回 {resp.status_code}: {resp.text}")
                logger.warning("monitor 5xx (attempt %d/%d): %s",
                               attempt, MONITOR_MAX_RETRIES + 1, last_exc)
                continue
            resp.raise_for_status()
            data = resp.json()
            # 监控服务返回 {status, expired, cert_valid_until}，直接透传
            return {"status": "ok", "expired": data["expired"],
                    "cert_valid_until": data["cert_valid_until"]}
        except httpx.TimeoutException:
            last_exc = RuntimeError(f"监控服务超时（{MONITOR_TIMEOUT}s）")
            logger.warning("monitor timeout (attempt %d/%d)", attempt, MONITOR_MAX_RETRIES + 1)
        except httpx.ConnectError:
            last_exc = RuntimeError(f"监控服务不可达: {MONITOR_BASE_URL}")
            logger.warning("monitor connect error (attempt %d/%d)", attempt, MONITOR_MAX_RETRIES + 1)
        except Exception as e:
            # 非预期错误不重试（重试可能掩盖 bug）
            return {"status": "error", "reason": f"监控查询异常: {e}"}

    return {"status": "error", "reason": str(last_exc) if last_exc else "监控查询失败"}


def _http_renew_cert(username: str, operation_id: str | None = None) -> dict:
    """真实 HTTP 续期证书。"""
    url = f"{MONITOR_BASE_URL}/api/v1/cert/{username}/renew"
    last_exc = None
    for _attempt in range(1, MONITOR_MAX_RETRIES + 2):
        try:
            resp = httpx.post(url, headers=_http_headers(operation_id),
                              json={"days": 180}, timeout=MONITOR_TIMEOUT)
            if resp.status_code == 404:
                return {"status": "error", "reason": f"账号 {username} 不存在"}
            if resp.status_code >= 500:
                # 续期是写操作，5xx 重试要谨慎——但这里 error_user 的 503 是业务失败不是抖动。
                # 简单起见统一重试，达到上限后返回失败，Agent 侧兜底转人工。
                last_exc = RuntimeError(f"证书服务返回 {resp.status_code}: {resp.text}")
                continue
            resp.raise_for_status()
            data = resp.json()
            return {"status": "ok",
                    "message": f"{data['message']}，有效期至 {data['new_valid_until']}"}
        except httpx.TimeoutException:
            last_exc = RuntimeError(f"证书服务超时（{MONITOR_TIMEOUT}s）")
        except httpx.ConnectError:
            last_exc = RuntimeError(f"证书服务不可达: {MONITOR_BASE_URL}")
        except Exception as e:
            return {"status": "error", "reason": f"证书续期异常: {e}"}

    return {"status": "error", "reason": str(last_exc) if last_exc else "证书续期失败"}


# ===== 对外接口（契约稳定：节点代码只认这两个函数）=====
def check_cert_status(username: str) -> dict:
    """查证书状态。mock/real 自动切换，返回格式一致。"""
    if MONITOR_MODE == "real":
        return _http_get_cert(username)
    return _mock_check_cert(username)


def renew_certificate(username: str, operation_id: str | None = None) -> dict:
    """续期证书。供执行类工具注册表调用。

    原来续期逻辑写在 actions.py 里直接改 MOCK_USERS 字典，
    现在收敛到这里（real 模式走 HTTP POST）。
    operation_id 会作为 Idempotency-Key 继续传给支持该契约的下游系统。
    """
    if MONITOR_MODE == "real":
        return _http_renew_cert(username, operation_id)
    return _mock_renew_cert(username)


# 查询工具注册表（只读工具）：场景配置 verify_tool 按名字调用
# 注意：必须放在函数定义之后（引用的是函数对象）
QUERY_REGISTRY = {
    "monitor.check_cert": check_cert_status,
}
