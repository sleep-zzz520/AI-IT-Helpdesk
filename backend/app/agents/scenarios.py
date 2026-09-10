"""场景注册表：每个场景的完整配置（数据驱动架构核心）。

核心思想：流程骨架（7 步）是通用的，场景差异全部是【数据】。
加场景 = 在这里加一个字典 + 注册对应工具，图代码零改动。

status:
- active:  流程完整可用（有 verify_tool + KB 方案 + replies）
- coming:  未上线（有 reply_unsupported 话术即可，自动转人工，绝不套错话术）
"""

SCENARIOS = {
    "vpn": {
        "name": "VPN 连接故障",
        "status": "active",
        # 信息补全清单（②check 节点用）
        "required_fields": ["device", "error_code"],
        # 意图判定关键词（①intent 节点动态生成 prompt 用）
        "keywords": ["VPN", "拨号失败", "无法建立连接", "证书", "800", "720"],
        # 查证工具（③verify 节点按名字从 QUERY_REGISTRY 调用）
        "verify_tool": "monitor.check_cert",
        # 收尾话术模板（⑦finalize 节点用；{...} 是 format 占位符）
        "replies": {
            "error": "⚠️ 查询失败：{reason}。请稍后重试，或转人工客服处理。",
            "valid": "✅ 您的证书状态正常（有效期至 {date}）。VPN 连不上可能另有原因，建议检查网络，或联系人工排查。",
        },
    },
    "password": {
        "name": "密码问题",
        "status": "coming",
        "required_fields": ["username"],
        # 注意：不要加"过期"——"证书过期"会同时命中 vpn(证书)+password(过期)
        # 导致拼写纠错/多问题检测被判歧义（踩过："证书过期导致连不上VPN" 判 other）
        "keywords": ["密码", "忘记", "重置"],
        "reply_unsupported": "密码自助服务正在开发中，您的问题已记录并转人工处理，请留意后续通知。",
    },
    "email": {
        "name": "邮箱问题",
        "status": "coming",
        "required_fields": [],
        "keywords": ["邮箱", "邮件", "IMAP", "SMTP", "收不到", "发不出", "553", "554"],
        "reply_unsupported": "邮箱自助服务正在开发中，您的问题已记录并转人工处理，请留意后续通知。",
    },
    "software": {
        "name": "软件安装问题",
        "status": "coming",
        "required_fields": [],
        # 2026-08-12 修：去掉泛词"安装/装不上"（Eval 真实化发现——"怎么安装
        # 打印机驱动"被"安装"拉进软件流）。泛词会误命中硬件/驱动类问题；
        # "办公软件怎么安装"仍靠"软件"命中，"软件装不上"同理。
        "keywords": ["软件", "激活", "许可证", "Office"],
        "reply_unsupported": "软件安装自助服务正在开发中，您的问题已记录并转人工处理，请留意后续通知。",
    },
}


def validate_scenarios() -> None:
    """启动校验：active 场景必须配置完整（防遗漏——漏配直接报错，不静默出错）。"""
    for key, sc in SCENARIOS.items():
        if sc.get("status") == "active":
            for field in ("name", "required_fields", "verify_tool", "replies"):
                if field not in sc:
                    raise ValueError(f"场景 [{key}] 缺少必要配置: {field}")
        elif "name" not in sc:
            raise ValueError(f"场景 [{key}] 缺少 name 配置")
