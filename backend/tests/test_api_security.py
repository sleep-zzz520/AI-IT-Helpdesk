"""HTTP 层安全边界回归测试（离线，内存 SQLite）。

这些测试补上原来 pytest 套件的关键缺口：
- 登录态和 token 防伪造
- 普通用户/管理员权限边界
- 创建会话时不信任前端 user_id
- 跨租户不能读取会话或提交反馈
- 反馈闭环的基本契约

它们不测试模型能力；模型能力由 Eval 测试集负责。这里测试的是
"身份是谁、能访问什么、越权时返回什么"，这是上线安全边界。
"""
from app.models import Conversation, Message


def _login(client, username: str, password: str) -> dict:
    response = client.post(
        "/api/auth/login",
        json={"username": username, "password": password},
    )
    assert response.status_code == 200, response.text
    return response.json()


def _auth(client, username: str, password: str) -> dict:
    return {"Authorization": f"Bearer {_login(client, username, password)['token']}"}


def test_me_requires_login(api_client, api_users):
    response = api_client.get("/api/auth/me")

    assert response.status_code == 401
    assert response.json()["detail"] == "未登录，请先登录"


def test_login_and_me_roundtrip(api_client, api_users):
    login = _login(api_client, "zhangsan", "Zhangsan@Test123")

    assert login["user"]["username"] == "zhangsan"
    assert login["user"]["role"] == "user"
    assert login["user"]["tenant_id"] == api_users["zhangsan"].tenant_id

    response = api_client.get(
        "/api/auth/me",
        headers={"Authorization": f"Bearer {login['token']}"},
    )
    assert response.status_code == 200
    assert response.json()["username"] == "zhangsan"


def test_invalid_credentials_use_same_401_contract(api_client, api_users):
    wrong_password = api_client.post(
        "/api/auth/login",
        json={"username": "zhangsan", "password": "wrong"},
    )
    unknown_user = api_client.post(
        "/api/auth/login",
        json={"username": "nobody", "password": "wrong"},
    )

    assert wrong_password.status_code == unknown_user.status_code == 401
    assert wrong_password.json()["detail"] == unknown_user.json()["detail"]


def test_tampered_token_is_rejected(api_client, api_users):
    token = _login(api_client, "zhangsan", "Zhangsan@Test123")["token"]
    replacement = "0" if token[-1] != "0" else "1"

    response = api_client.get(
        "/api/auth/me",
        headers={"Authorization": f"Bearer {token[:-1]}{replacement}"},
    )

    assert response.status_code == 401
    assert response.json()["detail"] == "登录已过期或 token 无效"


def test_disabled_user_cannot_login(api_client, api_users, db_session):
    api_users["zhangsan"].active = 0
    db_session.commit()

    response = api_client.post(
        "/api/auth/login",
        json={"username": "zhangsan", "password": "Zhangsan@Test123"},
    )

    assert response.status_code == 401
    assert response.json()["detail"] == "账号已被禁用"


def test_normal_user_cannot_read_admin_audit(api_client, api_users):
    response = api_client.get(
        "/api/audit/logs",
        headers=_auth(api_client, "zhangsan", "Zhangsan@Test123"),
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "需要管理员权限"


def test_admin_can_read_only_own_tenant_audit(api_client, api_users):
    # 登录会先写入 login 审计，确保管理员接口有可验证的数据。
    response = api_client.get(
        "/api/audit/logs",
        headers=_auth(api_client, "admin", "Admin@Test123"),
    )

    assert response.status_code == 200
    assert response.json()["items"]
    assert all(item["user_id"] in {"admin", "zhangsan", "anonymous"}
               for item in response.json()["items"])


def test_create_conversation_ignores_frontend_user_id(
    api_client, api_users, db_session
):
    response = api_client.post(
        "/api/conversations",
        headers=_auth(api_client, "zhangsan", "Zhangsan@Test123"),
        json={"user_id": "lisi"},
    )

    assert response.status_code == 200
    conversation_id = response.json()["id"]
    conversation = db_session.get(Conversation, conversation_id)
    assert conversation.user_id == "zhangsan"
    assert conversation.tenant_id == api_users["zhangsan"].tenant_id


def test_cross_tenant_cannot_read_or_message_conversation(
    api_client, api_users, db_session
):
    conversation = Conversation(
        user_id="zhangsan", tenant_id=api_users["zhangsan"].tenant_id
    )
    db_session.add(conversation)
    db_session.commit()

    headers = _auth(api_client, "lisi", "Lisi@Test123")
    get_response = api_client.get(
        f"/api/conversations/{conversation.id}", headers=headers
    )
    message_response = api_client.post(
        f"/api/conversations/{conversation.id}/messages",
        headers=headers,
        json={"content": "越权访问"},
    )

    assert get_response.status_code == 404
    assert message_response.status_code == 404


def test_feedback_is_tenant_scoped_and_only_for_agent_reply(
    api_client, api_users, db_session
):
    conversation = Conversation(
        user_id="zhangsan", tenant_id=api_users["zhangsan"].tenant_id
    )
    conversation.messages = [
        Message(role="user", content="问题"),
        Message(role="assistant", content="回答"),
    ]
    db_session.add(conversation)
    db_session.commit()
    user_message_id = conversation.messages[0].id
    assistant_message_id = conversation.messages[1].id

    zhang_headers = _auth(api_client, "zhangsan", "Zhangsan@Test123")
    up = api_client.post(
        f"/api/messages/{assistant_message_id}/feedback",
        headers=zhang_headers,
        json={"feedback": "up"},
    )
    cancel = api_client.post(
        f"/api/messages/{assistant_message_id}/feedback",
        headers=zhang_headers,
        json={"feedback": None},
    )
    user_message = api_client.post(
        f"/api/messages/{user_message_id}/feedback",
        headers=zhang_headers,
        json={"feedback": "down"},
    )
    cross_tenant = api_client.post(
        f"/api/messages/{assistant_message_id}/feedback",
        headers=_auth(api_client, "lisi", "Lisi@Test123"),
        json={"feedback": "down"},
    )

    assert up.status_code == 200 and up.json()["feedback"] == "up"
    assert cancel.status_code == 200 and cancel.json()["feedback"] is None
    assert user_message.status_code == 400
    assert cross_tenant.status_code == 404
