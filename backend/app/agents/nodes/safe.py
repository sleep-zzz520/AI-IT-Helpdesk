"""节点安全包装器：LLM/工具节点异常 → 兜底转人工，不让请求 500。

原理：给节点函数套一层 try/except，任何异常写入 state["error"]，
路由层检测到 error 就转人工。**用户永远不该看到 500，应该看到"已转人工"。**
"""
from typing import Callable


def safe(fn: Callable) -> Callable:
    """包装节点：异常 → 返回 {"error": ..., "trace": [...]}。"""
    def wrapper(state):
        try:
            return fn(state)
        except Exception as e:  # noqa: BLE001 —— 兜底必须捕获一切
            return {
                "error": f"{type(e).__name__}: {e}",
                "trace": state.get("trace", []) + [{
                    "node": "error",
                    "result": {"error": str(e)},
                }],
            }
    return wrapper
