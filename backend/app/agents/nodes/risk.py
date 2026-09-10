"""⑤风险分级节点：记录共享执行策略的决定。"""
from app.agents.state import HelpdeskState
from app.services.execution_policy import decide_execution


def risk_node(state: HelpdeskState) -> dict:
    kb = state.get("kb_match", {})
    decision, reason = decide_execution(kb)

    return {
        "risk_level": decision,  # auto / human
        "trace": [{
            "node": "risk",
            "result": {"decision": decision, "reason": reason},
        }],
    }
