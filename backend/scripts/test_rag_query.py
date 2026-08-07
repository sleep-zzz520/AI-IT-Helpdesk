"""rag_query 节点验证：实体约束校验（纯函数）+ 单跳/多跳问答（真实 LLM）。

用法（backend 目录）：python -m scripts.test_rag_query
"""
from app.agents.nodes.rag_query import (
    MAX_HOPS,
    _shares_evidence_terms,
    answer_question,
)


def test_terms_check() -> None:
    """next_query 实体约束：必须引用 evidence 里的词（防无意义乱跳）。"""
    evidence = [{"id": "e1", "text": "临时密码首次登录强制修改，重置后原密码立即失效", "source": "x"}]
    # 引用证据实体的检索词 → 通过
    assert _shares_evidence_terms("临时密码 首次登录", evidence)
    # 凭空造词（与证据无关）→ 拒绝
    assert not _shares_evidence_terms("服务器 防火墙 端口", evidence)
    # 全停用词 → 拒绝
    assert not _shares_evidence_terms("怎么 如何 什么", evidence)
    print("✅ 实体约束校验通过")


def test_multihop_loop() -> None:
    """确定性验证多跳循环：mock judge 判不足 → 第 2 跳、next_query 生效、证据合并。

    为什么用 mock：真实文档质量高、检索泛化强，judge 往往第一跳就判足够
    （"不够才跳"），多跳路径很难靠真实 LLM 自然触发——但循环逻辑必须被验证。
    这是工程原则：**不确定的行为用确定性测试兜底**。
    """
    import unittest.mock as mock

    from app.agents.nodes import rag_query as m

    judge_calls = {"n": 0}
    retrieve_calls = {"n": 0}

    def fake_judge(query, evidence):
        judge_calls["n"] += 1
        if judge_calls["n"] == 1:
            # 第一跳：判不足，next_query 引用证据实体（config-guide 内容里的词）
            return {"enough": False, "answerable": False, "executable": False,
                    "missing_info": ["密码重置后的注意事项"], "next_query": "密码 客户端"}
        # 第二跳：证据补齐了
        return {"enough": True, "answerable": True, "executable": False,
                "missing_info": [], "next_query": ""}

    def fake_retrieve(q, scenario=None, top_k=5):
        retrieve_calls["n"] += 1
        # 第 1 跳命中 email 文档，第 2 跳命中 password 文档（不同 id → 证据合并）
        doc = "email/config-guide" if retrieve_calls["n"] == 1 else "password/reset-sop"
        # 文本里包含 next_query 校验需要的实体词（"客户端"）——模拟真实证据内容
        h = mock.MagicMock()
        h.id, h.text, h.score, h.parent_text = doc, f"{doc} 内容 客户端 配置", 0.9, None
        h.metadata = {"source_url": f"docs/knowledge/{doc}.md"}
        return [h]

    with mock.patch.object(m, "judge_evidence", side_effect=fake_judge), \
         mock.patch.object(m, "retrieve", side_effect=fake_retrieve), \
         mock.patch.object(m, "generate_answer", return_value="合成回答"):
        r = m.answer_question("重置密码后邮箱报错怎么配", "email")

    assert len(r["hops"]) == 2, f"应执行 2 跳，实际 {len(r['hops'])}"
    assert r["hops"][0]["query"] == "重置密码后邮箱报错怎么配"
    assert r["hops"][1]["query"] == "密码 客户端"          # next_query 生效
    assert len(r["evidence"]) == 2                          # 跨跳证据合并去重
    assert r["answer"] == "合成回答"
    print("✅ 多跳循环验证通过：2 跳、next_query 生效、证据合并去重")


if __name__ == "__main__":
    test_terms_check()
    test_multihop_loop()
    print(f"\n=== 问答验证（MAX_HOPS={MAX_HOPS}）===\n")
    cases = [
        ("VPN 客户端怎么配置", "vpn"),                      # 期望：单跳命中
        # 真实场景里 intent 判为 email → 第 1 跳按场景过滤只命中邮箱文档，
        # judge 缺密码重置证据 → next_query 补检 password（跨场景多跳）
        ("重置密码后邮箱客户端提示密码错误，怎么配置", "email"),
    ]
    for q, sc in cases:
        print(f"--- 问题：{q} ---")
        r = answer_question(q, sc)
        print(f"跳数: {len(r['hops'])}")
        for h in r["hops"]:
            judge = h["judge"]
            print(f"  第{h['hop']}跳 query={h['query'][:40]!r} hits={len(h['hits'])} "
                  f"enough={judge.get('enough')} answerable={judge.get('answerable')} "
                  f"next_query={judge.get('next_query', '')[:30]!r}")
        print(f"证据来源: {sorted({e['source'].split('/')[-1] for e in r['evidence']})}")
        print(f"回答: {r['answer'][:80]}...\n")
