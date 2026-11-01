"""RAG 问答核心逻辑回归测试（离线，mock 检索和 judge）。

这里把原来 ``scripts/test_rag_query.py`` 中最重要的确定性检查纳入
pytest：实体约束、多跳规划、证据跨跳合并。真实模型的回答质量仍由
qa_eval 负责，避免把两种测试职责混在一起。
"""
from unittest import mock

from app.agents.nodes.rag_query import _shares_evidence_terms, answer_question


def test_next_query_must_share_evidence_terms():
    evidence = [{
        "id": "e1",
        "text": "临时密码首次登录强制修改，重置后原密码立即失效",
        "source": "x",
    }]

    assert _shares_evidence_terms("临时密码 首次登录", evidence)
    assert not _shares_evidence_terms("服务器 防火墙 端口", evidence)
    assert not _shares_evidence_terms("怎么 如何 什么", evidence)


def test_multihop_loop_uses_valid_next_query_and_merges_evidence():
    """第一跳不足时才允许第二跳，且下一跳必须引用当前证据实体。"""
    from app.agents.nodes import rag_query as module

    judge_calls = {"count": 0}
    retrieve_calls = {"count": 0}

    def fake_judge(_query, _evidence):
        judge_calls["count"] += 1
        if judge_calls["count"] == 1:
            return {
                "enough": False,
                "answerable": False,
                "executable": False,
                "missing_info": ["密码重置后的注意事项"],
                "next_query": "密码 客户端",
            }
        return {
            "enough": True,
            "answerable": True,
            "executable": False,
            "missing_info": [],
            "next_query": "",
        }

    def fake_retrieve(_query, scenario=None, top_k=5):
        retrieve_calls["count"] += 1
        doc = (
            "email/config-guide"
            if retrieve_calls["count"] == 1
            else "password/reset-sop"
        )
        hit = mock.MagicMock()
        hit.id = doc
        hit.text = f"{doc} 内容 客户端 配置"
        hit.parent_text = None
        hit.score = 0.9
        hit.metadata = {"source_url": f"docs/knowledge/{doc}.md"}
        return [hit]

    with (
        mock.patch.object(module, "judge_evidence", side_effect=fake_judge),
        mock.patch.object(module, "retrieve", side_effect=fake_retrieve),
        mock.patch.object(module, "generate_answer", return_value="合成回答"),
    ):
        result = answer_question("重置密码后邮箱报错怎么配", "email")

    assert len(result["hops"]) == 2
    assert result["hops"][0]["query"] == "重置密码后邮箱报错怎么配"
    assert result["hops"][1]["query"] == "密码 客户端"
    assert len(result["evidence"]) == 2
    assert result["answer"] == "合成回答"


def test_answer_question_keeps_one_parent_context_for_same_source():
    """检索可返回同文档两个子段，但 LLM 证据不能重复塞同一父文档。"""
    from types import SimpleNamespace

    from app.agents.nodes import rag_query as module

    same_source_hits = [
        SimpleNamespace(
            id="vpn:1", text="症状子段", parent_text="完整 VPN 文档",
            score=0.9, metadata={"source_url": "docs/knowledge/vpn/a.md"},
            routes={"vector"},
        ),
        SimpleNamespace(
            id="vpn:2", text="操作子段", parent_text="完整 VPN 文档",
            score=0.8, metadata={"source_url": "docs/knowledge/vpn/a.md"},
            routes={"bm25"},
        ),
        SimpleNamespace(
            id="mail:1", text="邮箱子段", parent_text="完整邮箱文档",
            score=0.7, metadata={"source_url": "docs/knowledge/email/b.md"},
            routes={"vector"},
        ),
    ]

    with (
        mock.patch.object(module, "retrieve", return_value=same_source_hits),
        mock.patch.object(module, "judge_evidence", return_value={
            "enough": True, "answerable": True, "next_query": "",
            "executable": False, "missing_info": [],
        }),
        mock.patch.object(module, "generate_answer", return_value="合成回答"),
    ):
        result = module.answer_question("VPN 怎么处理", "vpn")

    assert [item["source"] for item in result["evidence"]] == [
        "docs/knowledge/vpn/a.md", "docs/knowledge/email/b.md",
    ]


def test_answer_question_can_pin_an_existing_store_for_offline_evaluation():
    """离线评测可读候选库，不修改 active 指针或线上默认检索。"""
    from types import SimpleNamespace

    from app.agents.nodes import rag_query as module

    pinned_store = object()
    hit = SimpleNamespace(
        id="vpn:1", text="证据子段", parent_text="完整 VPN 文档", score=0.9,
        metadata={"source_url": "docs/knowledge/vpn/a.md"}, routes={"vector"},
    )

    with (
        mock.patch.object(module, "retrieve", return_value=[hit]) as mocked_retrieve,
        mock.patch.object(module, "judge_evidence", return_value={
            "enough": True, "answerable": True, "next_query": "",
            "executable": False, "missing_info": [],
        }),
        mock.patch.object(module, "generate_answer", return_value="合成回答"),
    ):
        result = module.answer_question("VPN 怎么处理", "vpn", retrieval_store=pinned_store)

    assert result["answer"] == "合成回答"
    assert mocked_retrieve.call_args.kwargs["store"] is pinned_store
