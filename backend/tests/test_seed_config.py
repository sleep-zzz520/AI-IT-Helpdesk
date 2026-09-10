"""首次部署演示账号的环境变量配置测试。"""

from app.db import _seed_users


def test_seed_users_allow_production_password_overrides(monkeypatch):
    monkeypatch.setenv("SEED_ADMIN_PASSWORD", "admin-from-environment")
    monkeypatch.setenv("SEED_ZHANGSAN_PASSWORD", "zhangsan-from-environment")
    monkeypatch.setenv("SEED_LISI_PASSWORD", "lisi-from-environment")

    passwords = {user["username"]: user["password"] for user in _seed_users()}

    assert passwords == {
        "admin": "admin-from-environment",
        "zhangsan": "zhangsan-from-environment",
        "lisi": "lisi-from-environment",
    }
