"""mock 监控 API（Tool）：查询账号证书状态。

【这是模拟实现】：真实环境里这里会是 HTTP 调用企业监控系统。
mock 的价值：接口签名和返回格式与真实 API 完全一致，
流程先跑通，以后换真实现只改这一个文件，节点代码不动。
"""
# 模拟账号数据库：key=用户名，value=证书信息
MOCK_USERS = {
    "zhangsan": {"cert_valid_until": "2026-07-30", "expired": True},   # 已过期
    "lisi":     {"cert_valid_until": "2027-01-15", "expired": False},  # 正常
    "error_user": {"cert_valid_until": "2026-07-30", "expired": True},  # 已过期 + 执行续期必失败
}


def check_cert_status(username: str) -> dict:
    """查证书状态。返回两种结果：
    - 成功: {"status": "ok", "expired": bool, "cert_valid_until": str}
    - 失败: {"status": "error", "reason": str}
    """
    info = MOCK_USERS.get(username)
    if info is None:
        return {"status": "error", "reason": f"账号 {username} 不存在或无查询权限"}
    return {"status": "ok", **info}


# 查询工具注册表（只读工具）：场景配置 verify_tool 按名字调用
# 注意：必须放在函数定义之后（引用的是函数对象）
QUERY_REGISTRY = {
    "monitor.check_cert": check_cert_status,
}
