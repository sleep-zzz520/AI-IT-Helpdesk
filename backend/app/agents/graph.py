"""LangGraph 状态图编排：把节点串成流程。

首轮并行：parallel（intent ∥ extract）——两个 LLM 调用并发发出，耗时 sum→max。
parallel 后路由：异常→handoff；非 active 场景→finalize；active 场景→check。
check ──缺信息──▶ ask（等用户回答）
      └─信息齐──▶ verify（③查证）
verify 之后按你的树状设计三分支：
  ├─ 查询失败 ──▶ finalize（回复失败，转人工）──▶ END
  ├─ 成功+过期 ──▶ ④知识库匹配 → risk → execute → close
  └─ 成功+未过期 ──▶ finalize（回复证书正常）──▶ END
"""
from langgraph.graph import END, StateGraph

from app.agents.nodes.ask import ask_node
from app.agents.nodes.check import check_node
from app.agents.nodes.close import close_node
from app.agents.nodes.execute import execute_node
from app.agents.nodes.finalize import finalize_node
from app.agents.nodes.handoff import handoff_node
from app.agents.nodes.kb import kb_node
from app.agents.nodes.parallel import parallel_round_node
from app.agents.nodes.rag_query import rag_query_node
from app.agents.nodes.risk import risk_node
from app.agents.nodes.safe import safe
from app.agents.nodes.verify import verify_node
from app.agents.scenarios import SCENARIOS, validate_scenarios
from app.agents.state import HelpdeskState


def should_ask_or_proceed(state: HelpdeskState) -> str:
    """条件边①：check 之后——缺信息就追问，齐了就进查证。"""
    if state.get("missing_info"):
        return "ask"
    return "verify"


def route_after_parallel(state: HelpdeskState) -> str:
    """条件边①·前置：并行节点之后——系统异常/咨询/场景状态三级路由。

    优先级：异常 > request_type（咨询走问答）> 场景状态（active 走执行）。
    consult 与场景状态无关：password/email/software 是 coming 也能问答
    （文档在知识库即答），troubleshoot 才看场景状态。
    """
    if state.get("error"):
        return "handoff"  # LLM 彻底失败：兜底转人工（不 500）
    if state.get("request_type") == "consult":
        return "rag_query"  # 咨询诉求：知识问答路径（纯只读，不触发执行）
    sc = SCENARIOS.get(state.get("intent"))
    if sc and sc.get("status") == "active":
        return "check"
    return "finalize"  # other / coming 场景：收尾（转人工话术）


def route_after_verify(state: HelpdeskState) -> str:
    """条件边②：verify 之后——按你的树状设计分三路。"""
    cs = state.get("cert_status", {})
    if cs.get("status") == "error":
        return "fail"       # 查询失败 → 终止
    if cs.get("expired"):
        return "expired"    # 已过期 → 进④
    return "valid"          # 未过期 → 终止


def route_after_risk(state: HelpdeskState) -> str:
    """条件边③：risk 之后——自动执行 or 转人工。"""
    return "execute" if state.get("risk_level") == "auto" else "handoff"


def route_after_execute(state: HelpdeskState) -> str:
    """条件边④：execute 之后——成功进⑦收尾，失败兜底转人工。"""
    return "success" if state.get("tool_result", {}).get("status") == "ok" else "handoff"


def build_graph():
    validate_scenarios()  # 启动校验：场景配置完整性（防遗漏）
    g = StateGraph(HelpdeskState)

    g.add_node("parallel", safe(parallel_round_node))  # 并行调度 + 异常兜底
    g.add_node("check", check_node)
    g.add_node("ask", ask_node)
    g.add_node("verify", verify_node)
    g.add_node("kb", kb_node)
    g.add_node("risk", risk_node)
    g.add_node("execute", execute_node)
    g.add_node("close", close_node)
    g.add_node("handoff", handoff_node)
    g.add_node("finalize", finalize_node)
    g.add_node("rag_query", safe(rag_query_node))  # 咨询问答：多跳检索 + 证据生成

    g.set_entry_point("parallel")
    # parallel 后路由：异常→handoff；咨询→rag_query；active 场景→check；其他→收尾
    g.add_conditional_edges("parallel", route_after_parallel, {
        "handoff": "handoff",
        "check": "check",
        "finalize": "finalize",
        "rag_query": "rag_query",
    })
    g.add_conditional_edges("check", should_ask_or_proceed, {
        "ask": "ask",
        "verify": "verify",
    })
    g.add_conditional_edges("verify", route_after_verify, {
        "fail": "finalize",
        "expired": "kb",   # ④知识库匹配
        "valid": "finalize",
    })
    g.add_edge("kb", "risk")
    g.add_conditional_edges("risk", route_after_risk, {
        "execute": "execute",   # ⑥执行 Tool
        "handoff": "handoff",
    })
    g.add_conditional_edges("execute", route_after_execute, {
        "success": "close",   # ⑦收尾
        "handoff": "handoff",
    })
    g.add_edge("close", END)
    g.add_edge("handoff", END)
    g.add_edge("finalize", END)
    g.add_edge("rag_query", END)  # 咨询问答结束：纯回答，不产生工单动作

    return g.compile()


graph = build_graph()
