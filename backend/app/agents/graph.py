"""LangGraph 状态图编排：把节点串成流程。

流程：intent → extract → check ──缺信息──▶ ask（等用户回答）
                                └─信息齐──▶ verify（③查证）
verify 之后按你的树状设计三分支：
  ├─ 查询失败 ──▶ finalize（回复失败，转人工）──▶ END
  ├─ 成功+过期 ──▶ ④知识库匹配（下一轮开发，先 END 占位）
  └─ 成功+未过期 ──▶ finalize（回复证书正常）──▶ END
"""
from langgraph.graph import END, StateGraph

from app.agents.nodes.ask import ask_node
from app.agents.nodes.check import check_node
from app.agents.nodes.extract import extract_node
from app.agents.nodes.finalize import finalize_node
from app.agents.nodes.intent import intent_node
from app.agents.nodes.verify import verify_node
from app.agents.state import HelpdeskState


def should_ask_or_proceed(state: HelpdeskState) -> str:
    """条件边①：check 之后——缺信息就追问，齐了就进查证。"""
    if state.get("missing_info"):
        return "ask"
    return "verify"


def route_after_verify(state: HelpdeskState) -> str:
    """条件边②：verify 之后——按你的树状设计分三路。"""
    cs = state.get("cert_status", {})
    if cs.get("status") == "error":
        return "fail"       # 查询失败 → 终止
    if cs.get("expired"):
        return "expired"    # 已过期 → 进④（下一轮接知识库匹配）
    return "valid"          # 未过期 → 终止


def build_graph():
    g = StateGraph(HelpdeskState)

    g.add_node("intent", intent_node)
    g.add_node("extract", extract_node)
    g.add_node("check", check_node)
    g.add_node("ask", ask_node)
    g.add_node("verify", verify_node)
    g.add_node("finalize", finalize_node)

    g.set_entry_point("intent")
    g.add_edge("intent", "extract")
    g.add_edge("extract", "check")
    g.add_conditional_edges("check", should_ask_or_proceed, {
        "ask": "ask",
        "verify": "verify",
    })
    g.add_conditional_edges("verify", route_after_verify, {
        "fail": "finalize",
        "expired": END,   # ④知识库匹配（下一轮替换）
        "valid": "finalize",
    })
    g.add_edge("finalize", END)

    return g.compile()


graph = build_graph()
