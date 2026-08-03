"""LangGraph 状态图编排：把节点串成流程。

当前版本：只接了意图识别节点（先跑通最小闭环），
后续按 ROADMAP 逐步加：追问 → 查证 → 知识库 → 风险分级 → Tool → 收尾。
"""
from langgraph.graph import END, StateGraph

from app.agents.nodes.intent import intent_node
from app.agents.state import HelpdeskState


def build_graph():
    g = StateGraph(HelpdeskState)

    # ① 意图识别：流程入口
    g.add_node("intent", intent_node)
    g.set_entry_point("intent")

    # 第一版先直接结束，后续这里接「信息补全」节点
    g.add_edge("intent", END)

    return g.compile()


graph = build_graph()
