"""Agent 状态定义：所有节点共用的数据容器（LangGraph State）。

状态就是一个「流水账本」：每个节点读自己需要的字段，
返回自己负责更新的字段，LangGraph 负责合并。
"""
from typing import TypedDict


class HelpdeskState(TypedDict):
    # 对话历史（[{role: user/assistant, content: str}]）
    messages: list[dict]
    # 意图分类结果：vpn / other
    intent: str
    # 还缺哪些信息（多轮追问用）
    missing_info: list[str]
    # 已收集的上下文
    device: str        # 设备型号（如 Windows 11）
    error_code: str    # 错误码（如 800）
    username: str      # 用户名/账号（密码重置场景）
    # 会话身份：来自系统上下文（登录态/工单系统），不是问出来的
    user_id: str
    # 异常兜底：safe 包装器捕获的节点异常（非空时路由优先转人工）
    error: str
    # 查证结果：证书状态（mock 或真实 API）
    cert_status: dict
    # 知识库匹配结果
    kb_match: dict
    # 风险等级：low / high
    risk_level: str
    # Tool 执行结果
    tool_result: dict
    # 全链路 Trace 记录（每个节点追加一条）
    trace: list[dict]
    # 工单号
    ticket_id: str
