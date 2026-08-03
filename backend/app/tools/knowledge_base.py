"""mock 企业知识库：条件组合 → 解决方案 的匹配表。

MVP 用【确定性规则】匹配（纯代码，快、免费、可预测）。
P2 升级为 RAG 向量检索（从企业文档库检索），但数据结构保持兼容。

每行一条方案：
- scenario:    所属场景（按场景过滤，不同场景的方案表可共存）
- match:       命中条件（error_code 匹配 / cert_expired 匹配）
- solution:    给用户看的方案描述
- action:      要执行的 Tool 操作名
- risk:        方案固有风险（low / medium / high）——⑤风险分级会用到
"""
KB = [
    {
        "scenario": "vpn",
        "match": {"error_code": "800", "cert_expired": True},
        "solution": "证书已过期，执行自动续期（certutil -renew + 重新拨号）",
        "action": "vpn.renew_certificate",
        "risk": "low",
    },
    {
        "scenario": "vpn",
        "match": {"error_code": "800", "cert_expired": False},
        "solution": "证书正常，检查 VPN 客户端配置是否被篡改",
        "action": "vpn.check_client_config",
        "risk": "low",
    },
    {
        "scenario": "vpn",
        "match": {"error_code": "720", "cert_expired": True},
        "solution": "证书过期且拨号失败，重建 VPN 拨号连接并续期",
        "action": "vpn.rebuild_connection",
        "risk": "medium",
    },
]


def match_solution(scenario: str, error_code: str, cert_expired: bool) -> dict:
    """按场景 + 条件匹配方案。返回：
    - 命中: {"matched": True, "solution": str, "action": str, "risk": str}
    - 未命中: {"matched": False, "reason": str}
    """
    for entry in KB:
        if entry["scenario"] != scenario:
            continue
        m = entry["match"]
        if m["error_code"] == error_code and m["cert_expired"] == cert_expired:
            return {
                "matched": True,
                "solution": entry["solution"],
                "action": entry["action"],
                "risk": entry["risk"],
            }
    return {"matched": False, "reason": f"知识库未收录 [{scenario}] error_code={error_code} + 证书状态={cert_expired} 的组合"}
