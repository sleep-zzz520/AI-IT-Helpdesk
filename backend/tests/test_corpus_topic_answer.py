"""语料话题全链路回归：撤销高分/BM25绕过证据裁判的不安全旧契约。"""
from app.agents.graph import graph
from app.agents.nodes.rag_query import _EMPTY_ANSWER
from app.rag.store import Hit

MOCK_CHAT_ANSWER = "（mock 回答）这是基于知识库的确定性答案。"


def _mk_hit(score: float, routes: set[str] | None = None,
            text: str = "英特尔宣布终止与某公司合作，并公布新处理器计划。") -> Hit:
    return Hit(id=f"d-{score}", text=text, parent_text=text,
               metadata={"source_url": "scale/347.md"}, score=score,
               routes=routes or {"vector"})


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


def test_strong_evidence_refuses_when_judge_down(monkeypatch, mock_llm):
    """judge 挂时高分不代表答案充分，必须拒答。"""
    _build_flow(monkeypatch, mock_llm, _mk_hit(0.62))
    nodes, reply = _invoke(mock_llm, "英特尔")
    assert "rag_query" in nodes, f"应走问答路径: {nodes}"
    assert reply == _EMPTY_ANSWER, f"judge 不可用时应拒答: {reply}"


def test_weak_evidence_conservative_when_judge_down(monkeypatch, mock_llm):
    """judge 挂 + 弱证据（0.45）→ 保守"暂无"（170 场景不回归）。"""
    _build_flow(monkeypatch, mock_llm, _mk_hit(0.45))
    _, reply = _invoke(mock_llm, "年终奖")
    assert reply == _EMPTY_ANSWER, f"弱证据应保守暂无: {reply}"


def test_bm25_route_refuses_when_judge_down(monkeypatch, mock_llm):
    """BM25 词面命中不能证明证据能回答原问题，裁判故障时拒答。"""
    _build_flow(monkeypatch, mock_llm,
                _mk_hit(0.50, routes={"bm25"}, text="错误码 800：证书过期，续期步骤"))
    _, reply = _invoke(mock_llm, "打印机连接故障")
    assert reply == _EMPTY_ANSWER, f"BM25 命中不得绕过 judge: {reply}"


def test_generate_down_falls_back_honest(monkeypatch, mock_llm):
    """合法裁判批准后 generate 故障，仍应诚实拒答。"""
    m = _build_flow(monkeypatch, mock_llm, _mk_hit(0.62), judge_exc=False,
                    judge_ok={"enough": True, "answerable": True,
                              "executable": False, "missing_info": [], "next_query": ""})
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
