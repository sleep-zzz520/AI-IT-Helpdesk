"""会话持久化测试（内存 SQLite，不依赖 MySQL）。

用 conftest 的 db_session fixture 把 app.db.engine/SessionLocal 换成
SQLite :memory:，验证 create_conversation → save_turn → load_state 往返一致。
替代原 scripts/test_db.py 的 MySQL 依赖版本。
"""
from app.services.session_service import (create_conversation, load_state,
                                          save_turn)


def test_save_load_roundtrip(db_session):
    """保存 → 加载 往返一致：消息、Trace、业务字段、工单号。"""
    conv = create_conversation(db_session, "zhangsan", tenant_id=1)

    state1 = {
        "messages": [{"role": "user", "content": "VPN连不上，报错800"}],
        "intent": "vpn",
        "request_type": "troubleshoot",
        "trace": [{"node": "intent", "result": {"intent": "vpn"}}],
    }
    save_turn(db_session, conv, state1)

    state2 = {
        "messages": state1["messages"] + [
            {"role": "assistant", "content": "✅ 已自动续期证书。"},
        ],
        "intent": "vpn",
        "request_type": "troubleshoot",
        "ticket_id": "TKT-TEST-001",
        "trace": state1["trace"] + [
            {"node": "close", "result": {"ticket_id": "TKT-TEST-001", "status": "resolved"}},
        ],
    }
    save_turn(db_session, conv, state2)

    loaded = load_state(db_session, conv.id)
    assert loaded["intent"] == "vpn"
    assert loaded["request_type"] == "troubleshoot"
    assert loaded["ticket_id"] == "TKT-TEST-001"
    assert len(loaded["messages"]) == 2, f"消息数={len(loaded['messages'])}"
    assert loaded["messages"][-1]["role"] == "assistant"
    assert len(loaded["trace"]) == 2, f"trace数={len(loaded['trace'])}"
    assert loaded["trace"][-1]["node"] == "close"


def test_append_only_traces(db_session):
    """多轮追加：第二轮只新增消息和 Trace，不重复旧数据。"""
    conv = create_conversation(db_session, "zhangsan")

    state1 = {
        "messages": [{"role": "user", "content": "VPN连不上"}],
        "intent": "vpn",
        "trace": [{"node": "intent", "result": {"intent": "vpn"}}],
    }
    save_turn(db_session, conv, state1)

    # 第二轮：消息+1，trace+1
    state2 = {
        "messages": state1["messages"] + [{"role": "user", "content": "Windows 11"}],
        "intent": "vpn",
        "trace": state1["trace"] + [
            {"node": "extract", "result": {"device": "Windows 11"}},
        ],
    }
    save_turn(db_session, conv, state2)

    loaded = load_state(db_session, conv.id)
    assert len(loaded["messages"]) == 2
    assert len(loaded["trace"]) == 2
    assert [t["node"] for t in loaded["trace"]] == ["intent", "extract"]
