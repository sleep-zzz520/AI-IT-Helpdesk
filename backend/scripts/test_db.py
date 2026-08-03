"""持久化测试：保存 → 加载往返一致（纯数据库，不调 GLM）。

验证：
1. MySQL 能连、建表成功
2. create_conversation / save_turn / load_state 三函数正确
3. 多轮追加：第二轮只新增消息和 Trace，不重复
"""
from app.db import SessionLocal, init_db
from app.services.session_service import create_conversation, load_state, save_turn

if __name__ == "__main__":
    print("==> 连接 MySQL 并建表...")
    init_db()
    print("    建表完成")

    with SessionLocal() as db:
        # 第一轮：用户描述 + 意图判定
        state1 = {
            "messages": [{"role": "user", "content": "VPN连不上，报错800"}],
            "intent": "vpn",
            "trace": [{"node": "intent", "result": {"intent": "vpn"}}],
        }
        conv = create_conversation(db, "zhangsan")
        save_turn(db, conv, state1)

        # 第二轮：带上历史 + 追问 + 用户回答
        # 注意：trace 必须是【追加式】的（state1.trace + 新增），和真实 graph 输出一致
        state2 = {
            "messages": [
                {"role": "user", "content": "VPN连不上，报错800"},
                {"role": "assistant", "content": "请问您的设备型号？"},
                {"role": "user", "content": "Windows 11"},
            ],
            "intent": "vpn",  # 会话级：复用第一轮判定
            "trace": state1["trace"] + [
                {"node": "intent", "result": {"reused": "vpn"}},
                {"node": "extract", "result": {"device": "Windows 11"}},
                {"node": "check", "result": {"missing": []}},
            ],
        }
        save_turn(db, conv, state2)

        # 第三轮：收尾（工单关闭）
        state3 = {
            "messages": state2["messages"] + [
                {"role": "assistant", "content": "✅ 已自动续期证书。"},
            ],
            "intent": "vpn",
            "ticket_id": "TKT-TEST-001",
            "trace": state2["trace"] + [
                {"node": "close", "result": {"ticket_id": "TKT-TEST-001", "status": "resolved"}},
            ],
        }
        save_turn(db, conv, state3)

        # 加载回来验证
        loaded = load_state(db, conv.id)

        assert loaded["intent"] == "vpn", loaded
        assert loaded["ticket_id"] == "TKT-TEST-001"
        assert len(loaded["messages"]) == 4, f"消息数={len(loaded['messages'])}"
        assert len(loaded["trace"]) == 5, f"trace数={len(loaded['trace'])}"
        assert loaded["messages"][-1]["role"] == "assistant"
        assert loaded["trace"][-1]["node"] == "close"

        print("==> 往返一致 ✅")
        print("  消息数 :", len(loaded["messages"]))
        print("  Trace 数:", len(loaded["trace"]))
        print("  intent :", loaded["intent"], "| ticket:", loaded["ticket_id"])
        print("  会话 ID:", conv.id, "| 状态:", conv.status)
