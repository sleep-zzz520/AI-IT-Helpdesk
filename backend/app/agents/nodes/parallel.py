"""并行调度节点：首轮同时执行「意图识别」与「信息抽取」。

为什么并行：
- intent（分类）与 extract（抽取）互不依赖，可以同时发出两个 LLM 请求
- 耗时从 sum(14s) 降到 max(7s)，直接减半
- 不合并节点：职责、Trace、Eval 全部保持原样（风险远低于"合并成一个节点"）

实现要点：
- ThreadPoolExecutor 线程池并发两个节点函数（openai SDK 线程安全）
- trace 各自只返回新增记录，合并后由 state 的 reducer（Annotated[list, add]）累积
"""
from concurrent.futures import ThreadPoolExecutor

from app.agents.nodes.extract import extract_node
from app.agents.nodes.intent import intent_node
from app.agents.state import HelpdeskState


def parallel_round_node(state: HelpdeskState) -> dict:
    """并发执行 intent + extract，等两者都完成再合并返回。"""
    with ThreadPoolExecutor(max_workers=2) as ex:
        f_intent = ex.submit(intent_node, state)
        f_extract = ex.submit(extract_node, state)
        u_intent = f_intent.result()
        u_extract = f_extract.result()

    merged = {**u_intent, **u_extract}
    # trace 合并：各自只含新增记录，拼起来交给 reducer 累积
    merged["trace"] = u_intent.get("trace", []) + u_extract.get("trace", [])
    return merged
