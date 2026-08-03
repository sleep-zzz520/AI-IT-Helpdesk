"""LangGraph 状态图编排：把节点串成流程。

流程：intent → extract → check ──缺信息──▶ ask（等用户回答）
                                └─信息齐──▶ 结束（后续接③查证）

这是图第一次出现【分支】：check 节点后由 should_ask 函数决定走哪条边。
"""
from langgraph.graph import END, StateGraph

from app.agents.nodes.ask import ask_node
from app.agents.nodes.check import check_node
from app.agents.nodes.extract import extract_node
from app.agents.nodes.intent import intent_node
from app.agents.state import HelpdeskState


def should_ask_or_proceed(state: HelpdeskState) -> str:
    """条件边函数：根据 state 决定下一站。返回目标节点名。"""
    if state.get("missing_info"):
        return "ask"          # 还缺信息 → 追问
    return "done"             # 信息齐了 → 结束（后续换成 ③查证）


def build_graph():
    g = StateGraph(HelpdeskState)

    # ① 意图识别（入口）
    g.add_node("intent", intent_node)
    # ② 信息补全三兄弟
    g.add_node("extract", extract_node)
    g.add_node("check", check_node)
    g.add_node("ask", ask_node)

    g.set_entry_point("intent")
    g.add_edge("intent", "extract")
    g.add_edge("extract", "check")
    # 条件边：check 之后的路由
    g.add_conditional_edges("check", should_ask_or_proceed, {
        "ask": "ask",
        "done": END,
    })

    return g.compile()


graph = build_graph()
