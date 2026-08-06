"""合成测试集（Eval 数据）。

设计原则（AGENTS.md 避坑指南：不伪造数据，用合成数据 + Eval 结果说话）：
- 正例变体：测试模型的泛化能力（同一个问题多种说法）
- 干扰项：测试不误判（打印机/蓝屏等不该走 VPN 流程）
- 三维覆盖：故障（troubleshoot）vs 咨询（consult）vs 寒暄（other）——Phase 3 分流的基础
"""
# 意图识别用例：(用户消息, 期望意图, 期望诉求类型)
INTENT_CASES = [
    # ===== VPN 故障（报障 → 执行流程）=====
    ("VPN连不上，报错Error 800", "vpn", "troubleshoot"),
    ("VPN拨号失败，代码720", "vpn", "troubleshoot"),
    ("无法建立VPN连接", "vpn", "troubleshoot"),
    ("证书过期导致连不上VPN", "vpn", "troubleshoot"),
    ("公司VPN登录不上去", "vpn", "troubleshoot"),
    # ===== VPN 咨询（问知识 → 问答路径）=====
    ("VPN客户端怎么配置", "vpn", "consult"),
    ("VPN证书续期的流程是什么", "vpn", "consult"),
    # ===== 密码 =====
    ("我的密码忘了怎么办", "password", "consult"),
    ("密码过期了，需要重置", "password", "troubleshoot"),
    # ===== 邮箱 =====
    ("邮箱收不到邮件，IMAP怎么配", "email", "consult"),
    ("邮件发不出去，报错553", "email", "troubleshoot"),
    # ===== 软件 =====
    ("办公软件怎么安装", "software", "consult"),
    ("软件激活提示许可证无效", "software", "troubleshoot"),
    # ===== 干扰项（场景不支持/寒暄 → 不触发任何流程）=====
    # 注意：打印机坏了是"故障诉求"，但场景不支持（other）——诉求类型与场景是两个正交维度
    ("打印机没有反应", "other", "troubleshoot"),
    ("电脑蓝屏了", "other", "troubleshoot"),
    ("你好", "other", "other"),
]
