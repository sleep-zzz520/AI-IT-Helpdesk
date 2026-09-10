"""真实监控 HTTP 适配层契约测试（离线，不启动监控服务）。"""
import httpx


def _response(status_code: int, *, json_data=None, text: str = "") -> httpx.Response:
    """构造带 request 上下文的 httpx 响应，支持被测代码调用 raise_for_status。"""
    request = httpx.Request("GET", "http://monitor.test")
    return httpx.Response(
        status_code,
        json=json_data,
        text=text if json_data is None else None,
        request=request,
    )


def test_real_check_success_preserves_contract(monkeypatch):
    import app.tools.monitor_api as monitor

    calls = []

    def fake_get(url, **kwargs):
        calls.append((url, kwargs))
        return _response(
            200,
            json_data={"expired": True, "cert_valid_until": "2026-07-30"},
        )

    monkeypatch.setattr(monitor, "MONITOR_MODE", "real")
    monkeypatch.setattr(monitor.httpx, "get", fake_get)

    result = monitor.check_cert_status("zhangsan")

    assert result == {
        "status": "ok",
        "expired": True,
        "cert_valid_until": "2026-07-30",
    }
    assert calls[0][0].endswith("/api/v1/cert/zhangsan")
    assert calls[0][1]["timeout"] == monitor.MONITOR_TIMEOUT


def test_real_check_404_does_not_retry(monkeypatch):
    import app.tools.monitor_api as monitor

    calls = []

    def fake_get(_url, **_kwargs):
        calls.append(1)
        return _response(404)

    monkeypatch.setattr(monitor, "MONITOR_MODE", "real")
    monkeypatch.setattr(monitor.httpx, "get", fake_get)

    result = monitor.check_cert_status("nobody")

    assert len(calls) == 1
    assert result == {
        "status": "error",
        "reason": "账号 nobody 不存在或无查询权限",
    }


def test_real_check_503_retries_then_succeeds(monkeypatch):
    import app.tools.monitor_api as monitor

    responses = [
        _response(503, text="temporary"),
        _response(200, json_data={"expired": False, "cert_valid_until": "2027-01-15"}),
    ]

    monkeypatch.setattr(monitor, "MONITOR_MODE", "real")
    monkeypatch.setattr(monitor, "MONITOR_MAX_RETRIES", 2)
    monkeypatch.setattr(monitor.httpx, "get", lambda *_a, **_k: responses.pop(0))

    result = monitor.check_cert_status("lisi")

    assert result["status"] == "ok"
    assert result["expired"] is False
    assert len(responses) == 0


def test_real_check_timeout_retries_and_normalizes_error(monkeypatch):
    import app.tools.monitor_api as monitor

    calls = []

    def timeout(_url, **_kwargs):
        calls.append(1)
        raise httpx.TimeoutException("timed out")

    monkeypatch.setattr(monitor, "MONITOR_MODE", "real")
    monkeypatch.setattr(monitor, "MONITOR_MAX_RETRIES", 2)
    monkeypatch.setattr(monitor.httpx, "get", timeout)

    result = monitor.check_cert_status("zhangsan")

    assert len(calls) == 3  # 首次 + 2 次重试
    assert result["status"] == "error"
    assert "监控服务超时" in result["reason"]


def test_real_renew_success_returns_agent_tool_contract(monkeypatch):
    import app.tools.monitor_api as monitor

    calls = []

    def fake_post(url, **kwargs):
        calls.append((url, kwargs))
        assert url.endswith("/api/v1/cert/zhangsan/renew")
        assert kwargs["json"] == {"days": 180}
        return _response(
            200,
            json_data={"message": "证书续期成功", "new_valid_until": "2027-01-15"},
        )

    monkeypatch.setattr(monitor, "MONITOR_MODE", "real")
    monkeypatch.setattr(monitor.httpx, "post", fake_post)

    result = monitor.renew_certificate("zhangsan", operation_id="renew-op-1")

    assert result == {
        "status": "ok",
        "message": "证书续期成功，有效期至 2027-01-15",
    }
    assert calls[0][1]["headers"]["Idempotency-Key"] == "renew-op-1"


def test_real_renew_404_does_not_retry(monkeypatch):
    import app.tools.monitor_api as monitor

    calls = []

    def fake_post(_url, **_kwargs):
        calls.append(1)
        return _response(404)

    monkeypatch.setattr(monitor, "MONITOR_MODE", "real")
    monkeypatch.setattr(monitor.httpx, "post", fake_post)

    result = monitor.renew_certificate("nobody")

    assert len(calls) == 1
    assert result == {"status": "error", "reason": "账号 nobody 不存在"}
