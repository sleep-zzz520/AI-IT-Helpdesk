"""意图识别节点测试：真实调用 GLM 验证分类效果。

用法（在 backend 目录下）：
    cd backend && python -m scripts.test_intent
"""
from app.agents.graph import graph


def run(user_msg: str) -> None:
    out = graph.invoke({
        "messages": [{"role": "user", "content": user_msg}],
        "trace": [],
    })
    print(f"输入  : {user_msg}")
    print(f"意图  : {out.get('intent')}")
    print(f"Trace : {out.get('trace')}")
    print("-" * 50)


if __name__ == "__main__":
    run("VPN连不上，报错Error 800，急着参加10点晨会")
    run("我的邮箱密码忘了怎么办")
    run("我的密码过期了，怎么办")
