"""VPN 受控执行服务测试：入口变化不应改变安全和幂等边界。"""
from app.models import AuditLog, ExecutionOperation
from app.services import vpn_execution_service as service
from app.services.vpn_execution_service import ExecutionRequest


def _request(**overrides) -> ExecutionRequest:
    values = {
        "actor_id": "zhangsan",
        "tenant_id": 1,
        "target_user_id": "zhangsan",
        "risk_decision": "auto",
        "source": "web_agent",
        "operation_id": "vpn-renew-op-1",
    }
    values.update(overrides)
    return ExecutionRequest(**values)


def test_same_operation_id_executes_monitor_once_and_reuses_stored_result(
    db_session, api_users, monkeypatch
):
    """重试读取第一次结果，不应第二次续期。"""
    calls = []

    def fake_renew(username: str, operation_id: str):
        calls.append((username, operation_id))
        return {"status": "ok", "message": "续期成功"}

    monkeypatch.setattr(service, "renew_certificate", fake_renew)
    request = _request()

    first = service.renew_vpn_certificate(request)
    second = service.renew_vpn_certificate(request)

    assert first == second == {
        "status": "ok",
        "message": "续期成功",
        "operation_id": "vpn-renew-op-1",
    }
    assert calls == [("zhangsan", "vpn-renew-op-1")]

    db_session.expire_all()
    operation = db_session.query(ExecutionOperation).one()
    assert (operation.status, operation.actor_id, operation.target_user_id) == (
        "succeeded", "zhangsan", "zhangsan"
    )
    audit = db_session.query(AuditLog).filter_by(action="execute_vpn_renew_certificate").one()
    assert audit.tenant_id == api_users["zhangsan"].tenant_id
    assert audit.detail["operation_id"] == "vpn-renew-op-1"


def test_non_auto_risk_is_denied_before_any_monitor_call(db_session, api_users, monkeypatch):
    """即使绕过 LangGraph 直接调服务，高风险也不能自动续期。"""
    monkeypatch.setattr(service, "renew_certificate", lambda *_: (_ for _ in ()).throw(AssertionError()))

    result = service.renew_vpn_certificate(_request(risk_decision="human"))

    assert result["status"] == "error"
    assert "风险策略" in result["reason"]
    audit = db_session.query(AuditLog).filter_by(action="deny_vpn_renew_certificate").one()
    assert audit.detail["reason"] == "当前风险策略不允许自动续期"


def test_actor_cannot_renew_another_users_certificate(db_session, api_users, monkeypatch):
    """同租户内也按自助服务原则限制为“只能操作自己”。"""
    monkeypatch.setattr(service, "renew_certificate", lambda *_: (_ for _ in ()).throw(AssertionError()))

    result = service.renew_vpn_certificate(_request(target_user_id="admin"))

    assert result["status"] == "error"
    assert "当前登录用户" in result["reason"]


def test_reusing_an_operation_id_from_another_tenant_is_denied(
    db_session, api_users, monkeypatch
):
    """操作编号不能成为跨租户读取先前结果的钥匙。"""
    monkeypatch.setattr(service, "renew_certificate", lambda *_: {"status": "ok", "message": "续期成功"})
    service.renew_vpn_certificate(_request())

    result = service.renew_vpn_certificate(_request(
        actor_id="lisi",
        tenant_id=api_users["lisi"].tenant_id,
        target_user_id="lisi",
        source="mcp",
    ))

    assert result == {"status": "error", "reason": "操作编号不属于当前主体或目标"}
