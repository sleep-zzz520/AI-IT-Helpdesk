"""模拟监控服务也应识别 Idempotency-Key，覆盖真实 HTTP 契约的下游一端。"""
from fastapi.testclient import TestClient


def test_monitor_reuses_response_for_same_idempotency_key(tmp_path, monkeypatch):
    import mock_monitor.server as monitor

    monkeypatch.setattr(monitor, "DB_PATH", tmp_path / "monitor.db")
    with TestClient(monitor.app) as client:
        headers = {"Idempotency-Key": "monitor-op-1"}
        first = client.post("/api/v1/cert/zhangsan/renew", json={"days": 180}, headers=headers)
        second = client.post("/api/v1/cert/zhangsan/renew", json={"days": 180}, headers=headers)
        conflict = client.post("/api/v1/cert/lisi/renew", json={"days": 180}, headers=headers)

    assert first.status_code == second.status_code == 200
    assert first.json() == second.json()
    assert conflict.status_code == 409
