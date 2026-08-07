"""语料话题全链路回归测试（会话 179 系统性修复）。

背景：会话 170→175→179 连踩三个词（年终奖/比亚迪/英特尔），每次只修一个
表象。系统性根因——链路对免费模型稳定性假设过强：judge 单点失败（限流/
非法 JSON 是常态），整条问答直接瘫痪。

本测试把用户"随便换一个词"变成自动化：mock 整条链路的最坏环境
（LLM 误判 other/other + judge 抛异常模拟限流），断言按证据强度正确分流：
- 强证据（最高分 ≥ 0.55 或含 BM25 路标）→ judge 缺席也基于证据生成
- 弱证据 → 保守"暂无"（会话 170 弱命中瞎编场景不回归）
- 纯寒暄 → greeting（会话 175 修复不回归）
- judge 正常 → 原路径不回归
"""
from app.agents.graph import graph
from app.agents.nodes.rag_query import _EMPTY_ANSWER, _evidence_strong
from app.rag.store import Hit

MOCK_CHAT_ANSWER = "（mock 回答）这是基于知识库的确定性答案。"


def _mk_hit(score: float, routes: set[str] | None = None,
            text: str = "英特尔宣布终止与某公司合作，并公布新处理器计划。") -> Hit:
    return Hit(id=f"d-{score}", text=text, parent_text=text,
               metadata={"source_url": "scale/347.md"}, score=score,
               routes=routes or {"vector"})


# ===== 规则层：证据强度判定 =====
def test_evidence_strong_by_score():
    """最高分 ≥ 0.55 → 强证据（179 的 0.62 英特尔新闻即此）。"""
    hops = [{"hits": [{"score": 0.45}, {"score": 0.62}]}]
    assert _evidence_strong(hops) is True


def test_evidence_weak_by_score():
    """全部分数 < 0.55 且无 BM25 路标 → 弱证据。"""
    hops = [{"hits": [{"score": 0.43}, {"score": 0.50, "routes": ["vector"]}]}]
    assert _evidence_strong(hops) is False


def test_evidence_strong_by_bm25_route():
    """低分但带 BM25 路标（错误码 800 词面命中）→ 强证据。"""
    hops = [{"hits": [{"score": 0.45, "routes": ["vector"]},
                      {"score": 0.48, "routes": ["bm25"]}]}]
    assert _evidence_strong(hops) is True


def test_evidence_strong_no_hits():
    """无命中记录 → 弱。"""
    assert _evidence_strong([]) is False
    assert _evidence_strong([{"hits": []}]) is False


# ===== 全链路：最坏环境（LLM 误判 + judge 挂） =====
def _build_flow(monkeypatch, mock_llm, hit: Hit, judge_exc=True, judge_ok=None):
    """搭最坏环境：intent 误判 other/other + judge 异常（或可控）+ 固定命中。"""
    import app.agents.nodes.rag_query as m

    mock_llm["intent"] = {"intent": "other", "request_type": "other", "reason": "mock"}
    mock_llm["chat"] = MOCK_CHAT_ANSWER

    def fake_retrieve(query, scenario=None, top_k=5):
        return [hit]

    monkeypatch.setattr(m, "retrieve", fake_retrieve)
    if judge_exc:
        def boom(*_a, **_k):
            raise RuntimeError("模型未返回合法 JSON: ")
        monkeypatch.setattr(m, "judge_evidence", boom)
    elif judge_ok is not None:
        monkeypatch.setattr(m, "judge_evidence", lambda *a, **k: judge_ok)
    return m


def _invoke(mock_llm, msg: str) -> tuple[list[str], str]:
    out = graph.invoke({
        "messages": [{"role": "user", "content": msg}],
        "user_id": "zhangsan",
        "trace": [],
    })
    nodes = [t["node"] for t in out.get("trace", [])]
    reply = out["messages"][-1]["content"]
    return nodes, reply


def test_strong_evidence_answers_even_when_judge_down(monkeypatch, mock_llm):
    """judge 挂 + 强证据（0.62）→ 仍基于证据生成回答（179 修复核心）。"""
    _build_flow(monkeypatch, mock_llm, _mk_hit(0.62))
    nodes, reply = _invoke(mock_llm, "英特尔")
    assert "rag_query" in nodes, f"应走问答路径: {nodes}"
    assert reply == MOCK_CHAT_ANSWER, f"强证据应生成回答: {reply}"
    assert "暂无相关内容" not in reply, "强证据不应被误杀成暂无"


def test_weak_evidence_conservative_when_judge_down(monkeypatch, mock_llm):
    """judge 挂 + 弱证据（0.45）→ 保守"暂无"（170 场景不回归）。"""
    _build_flow(monkeypatch, mock_llm, _mk_hit(0.45))
    _, reply = _invoke(mock_llm, "年终奖")
    assert reply == _EMPTY_ANSWER, f"弱证据应保守暂无: {reply}"


def test_bm25_route_answers_when_judge_down(monkeypatch, mock_llm):
    """judge 挂 + BM25 词法命中（低分）→ 确定性相关，仍回答（错误码场景）。

    消息不能用场景关键词（"vpn报错800"会触发规则层拼写纠错 → 走执行链路，
    那是设计行为）；用中性词验证纯 judge 降级路径。
    """
    _build_flow(monkeypatch, mock_llm,
                _mk_hit(0.50, routes={"bm25"}, text="错误码 800：证书过期，续期步骤"))
    _, reply = _invoke(mock_llm, "打印机连接故障")
    assert reply == MOCK_CHAT_ANSWER, f"BM25 命中应生成回答: {reply}"


def test_generate_down_falls_back_honest(monkeypatch, mock_llm):
    """judge 挂 + 强证据但 generate 也挂（模型全挂）→ 诚实"暂无"而非报错。"""
    m = _build_flow(monkeypatch, mock_llm, _mk_hit(0.62))
    monkeypatch.setattr(m, "generate_answer", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("限流")))
    _, reply = _invoke(mock_llm, "英特尔")
    assert reply == _EMPTY_ANSWER, "模型全挂时应诚实暂无"


def test_judge_normal_path_unchanged(monkeypatch, mock_llm):
    """judge 正常（enough+answerable）→ 原路径不回归。"""
    _build_flow(monkeypatch, mock_llm, _mk_hit(0.9), judge_exc=False,
                judge_ok={"enough": True, "answerable": True,
                          "executable": False, "missing_info": [], "next_query": ""})
    _, reply = _invoke(mock_llm, "英特尔")
    assert reply == MOCK_CHAT_ANSWER


def test_pure_greeting_still_greeting(mock_llm):
    """纯寒暄 → greeting（175 修复不回归）。"""
    mock_llm["intent"] = {"intent": "other", "request_type": "other", "reason": "mock"}
    nodes, reply = _invoke(mock_llm, "你好")
    assert "greeting" in nodes, f"纯寒暄应走 greeting: {nodes}"
    assert "智能助手" in reply
