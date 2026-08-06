"""Agent 状态定义：所有节点共用的数据容器（LangGraph State）。

状态就是一个「流水账本」：每个节点读自己需要的字段，
返回自己负责更新的字段，LangGraph 负责合并。

注意 trace 用了 reducer（Annotated[list, add]）：
并行节点（intent∥extract）各自只返回【新增】的 trace 记录，
由 reducer 自动累积，避免并行写入互相覆盖。
"""
from operator import add
from typing import Annotated, TypedDict


class HelpdeskState(TypedDict):
    # 对话历史（[{role: user/assistant, content: str}]）
    messages: list[dict]
    # 意图分类结果：vpn / password / email / software / other（会话级，判定一次后复用）
    intent: str
    # 诉求类型：troubleshoot（故障→执行）/ consult（咨询→问答）/ other（寒暄）
    # 与 intent 一样会话级复用（graph 路由消费）
    request_type: str
    # 多问题检测：消息里精确命中 ≥2 个场景关键词时非空（如 "vpn和密码都连不上"）
    # 路由优先消费：引导用户逐个描述（不转人工，见 nodes/multi.py）
    multi_scenarios: list[str]
    # 还缺哪些信息（多轮追问用）
    missing_info: list[str]
    # 已收集的上下文
    device: str        # 设备型号（如 Windows 11）
    error_code: str    # 错误码（如 800）
    username: str      # 用户名/账号（密码重置场景）
    # 会话身份：来自系统上下文（登录态/工单系统），不是问出来的
    user_id: str
    # 运行时模型链（前端"速度/准确"切换，API 层注入，不落库；None = 默认能力链）
    model_chain: list[str]
    # 异常兜底：safe 包装器捕获的节点异常（非空时路由优先转人工）
    error: str
    # 查证结果：证书状态（mock 或真实 API）
    cert_status: dict
    # 知识库匹配结果
    kb_match: dict
    # 风险决策：auto / human
    risk_level: str
    # Tool 执行结果
    tool_result: dict
    # 全链路 Trace 记录：reducer 累积，节点只返回新增记录
    trace: Annotated[list[dict], add]
    # 工单号
    ticket_id: str
