"""完整性检查节点：对着清单打勾（纯代码，不调 AI）。

输入：intent（从场景注册表取该场景的信息清单）+ 已收集字段。
输出：missing_info（还缺哪些字段的列表）。
"""
from app.agents.scenarios import SCENARIOS
from app.agents.state import HelpdeskState


def check_node(state: HelpdeskState) -> dict:
    required = SCENARIOS.get(state["intent"], {}).get("required_fields", [])
    missing = [field for field in required if not state.get(field)]
    return {
        "missing_info": missing,
        "trace": state["trace"] + [{"node": "check", "result": {"missing": missing}}],
    }
