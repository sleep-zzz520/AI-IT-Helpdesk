"""并行调度节点：首轮同时执行「意图识别」与「信息抽取」。

为什么并行：
- intent（分类）与 extract（抽取）互不依赖，可以同时发出两个 LLM 请求
- 耗时从 sum(14s) 降到 max(7s)，直接减半
- 不合并节点：职责、Trace、Eval 全部保持原样（风险远低于"合并成一个节点"）

实现要点：
- ThreadPoolExecutor 线程池并发两个节点函数（openai SDK 线程安全）
- trace 各自只返回新增记录，合并后由 state 的 reducer（Annotated[list, add]）累积
"""
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from app.agents.nodes.extract import extract_node
from app.agents.nodes.intent import intent_node
from app.agents.state import HelpdeskState


def parallel_round_node(state: HelpdeskState) -> dict:
    """并发执行 intent + extract，等两者都完成再合并返回。

    计时细节：并行下整体耗时 = max(两个子任务)，无法用「总耗时」代表每个子任务，
    所以用 as_completed 记录每个 future 各自完成的时刻，分别写入 trace。
    （外层 API 层会给顶层节点补计时，这里用 setdefault 语义保留更精确的子任务耗时）
    """
    started = time.perf_counter()
    updates, finished_at = {}, {}
    with ThreadPoolExecutor(max_workers=2) as ex:
        futures = {
            ex.submit(intent_node, state): "intent",
            ex.submit(extract_node, state): "extract",
        }
        for f in as_completed(futures):
            name = futures[f]
            updates[name] = f.result()
            finished_at[name] = round((time.perf_counter() - started) * 1000)

    u_intent, u_extract = updates["intent"], updates["extract"]
    # 各自 trace 记录写入各自的耗时（这里覆盖写；外层 API 层的 setdefault
    # 只补缺失值、不会覆盖这里写入的精确耗时，所以最终保留的是子任务真实耗时）
    for u, name in ((u_intent, "intent"), (u_extract, "extract")):
        for t in u.get("trace", []):
            t["elapsed_ms"] = finished_at[name]

    merged = {**u_intent, **u_extract}
    # trace 合并：各自只含新增记录，拼起来交给 reducer 累积
    merged["trace"] = u_intent.get("trace", []) + u_extract.get("trace", [])
    return merged
