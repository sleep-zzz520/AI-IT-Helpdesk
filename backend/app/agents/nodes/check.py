"""完整性检查节点：对着清单打勾（纯代码，不调 AI）。

输入：intent（决定用哪张清单）+ 已收集字段。
输出：missing_info（还缺哪些字段的列表）。
"""
from app.agents.requirements import REQUIRED_FIELDS
from app.agents.state import HelpdeskState


def check_node(state: HelpdeskState) -> dict:
    required = REQUIRED_FIELDS.get(state["intent"], [])
    missing = [field for field in required if not state.get(field)]
    return {
        "missing_info": missing,
        "trace": state["trace"] + [{"node": "check", "result": {"missing": missing}}],
    }
