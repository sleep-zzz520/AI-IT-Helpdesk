"""④知识库匹配节点：查方案表，决定"怎么处理"。

输入：intent（按场景过滤方案）+ error_code + cert_expired。
输出：kb_match（命中方案 / 未命中），供⑤风险分级和⑥执行用。
"""
from app.agents.state import HelpdeskState
from app.tools.knowledge_base import match_solution


def kb_node(state: HelpdeskState) -> dict:
    scenario = state.get("intent", "")
    error_code = state.get("error_code", "")
    cert_expired = bool(state.get("cert_status", {}).get("expired"))
    result = match_solution(scenario, error_code, cert_expired)
    return {
        "kb_match": result,
        "trace": state["trace"] + [{
            "node": "kb",
            "result": {"scenario": scenario, "error_code": error_code, "cert_expired": cert_expired, "kb_match": result},
        }],
    }
