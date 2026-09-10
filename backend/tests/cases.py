"""合成测试集（Eval 数据）。

设计原则（AGENTS.md 避坑指南：不伪造数据，用合成数据 + Eval 结果说话）：
- 正例变体：测试模型的泛化能力（同一个问题多种说法）
- 干扰项：测试不误判（打印机/蓝屏等不该走 VPN 流程）
- 三维覆盖：故障（troubleshoot）vs 咨询（consult）vs 寒暄（other）——Phase 3 分流的基础

测试集方法论（2026-08-12 扩容，回应"指标过于完美"的审阅意见）：
- 好测试集 = 系统最容易错的地方，而不是最标准的地方
- 扩容后 42 条：标准说法（基线）+ 拼写错误（bpn→vpn，验证规则兜底）
  + 口语/网络用语（挂了/掉线）+ emoji/繁体/中英混写 + 边界模糊用例
  + 硬负例（WiFi/打印机/蓝屏，测不误判）+ 多问题（测 multi 引导）+ 纯寒暄
- 期望值必须可辩护：intent 依据场景注册表关键词精确/语义命中；
  request_type 依据"有无明确故障现象"；multi 依据 ≥2 个场景关键词精确命中

用例格式：(消息, 期望 intent, 期望 request_type, 期望 multi_scenarios 或 None)
eval.py 兼容旧 3 元组（multi 期望默认 None）。
"""
# 意图识别用例：(用户消息, 期望意图, 期望诉求类型, [期望多问题场景])
INTENT_CASES = [
    # ================= 基线（16 条，保留）：标准说法 =================
    # VPN 故障（报障 → 执行流程）
    ("VPN连不上，报错Error 800", "vpn", "troubleshoot", None),
    ("VPN拨号失败，代码720", "vpn", "troubleshoot", None),
    ("无法建立VPN连接", "vpn", "troubleshoot", None),
    ("证书过期导致连不上VPN", "vpn", "troubleshoot", None),
    ("公司VPN登录不上去", "vpn", "troubleshoot", None),
    # VPN 咨询（问知识 → 问答路径）
    ("VPN客户端怎么配置", "vpn", "consult", None),
    ("VPN证书续期的流程是什么", "vpn", "consult", None),
    # 密码
    ("我的密码忘了怎么办", "password", "consult", None),
    ("密码过期了，需要重置", "password", "troubleshoot", None),
    # 邮箱
    ("邮箱收不到邮件，IMAP怎么配", "email", "consult", None),
    ("邮件发不出去，报错553", "email", "troubleshoot", None),
    # 软件
    ("办公软件怎么安装", "software", "consult", None),
    ("软件激活提示许可证无效", "software", "troubleshoot", None),
    # 干扰项（场景不支持/寒暄 → 不触发任何流程）
    ("打印机没有反应", "other", "troubleshoot", None),
    ("电脑蓝屏了", "other", "troubleshoot", None),
    ("你好", "other", "other", None),

    # ================= 拼写错误（3 条）：测规则层纠错兜底 =================
    # bpn≈vpn（替换）、vpvn≈vpn（插入）：编辑距离 1，_fuzzy_fix_intent 应修正
    # 8O0（O 与 0 混淆）：纯数字词不做模糊（规则层设计），只能靠 LLM 语义兜底
    ("bpn连不上，怎么解决", "vpn", "troubleshoot", None),
    ("vpvn拨号失败代码720", "vpn", "troubleshoot", None),
    ("vpn连不上报错8O0", "vpn", "troubleshoot", None),

    # ================= 口语/网络用语（6 条）：测泛化，不是背模板 =================
    ("vpn上不去了，怎么搞", "vpn", "troubleshoot", None),
    ("我的vpn是不是挂了", "vpn", "troubleshoot", None),
    ("vpn一直掉线，烦死了", "vpn", "troubleshoot", None),
    ("公司vpn连不上，一整天了", "vpn", "troubleshoot", None),
    ("咋连vpn啊", "vpn", "consult", None),
    ("vpn怎么弄", "vpn", "consult", None),

    # ================= emoji/繁体/中英混写（4 条）：测输入噪声鲁棒性 =================
    ("VPN连不上😭😭", "vpn", "troubleshoot", None),
    ("VPN連不上", "vpn", "troubleshoot", None),          # 繁体"連"
    ("vpn连接失败 error 800", "vpn", "troubleshoot", None),
    ("VPN连接不稳定，总断", "vpn", "troubleshoot", None),

    # ================= 边界模糊（3 条）：真实场景的灰色地带 =================
    # 2026-08-12 修订期望："很慢/太卡"按系统哲学"宁多问不漏报障"
    # （intent.py：非法 request_type 归一化默认 troubleshoot）→ 期望 troubleshoot
    ("vpn能连上但是很慢", "vpn", "troubleshoot", None),
    ("VPN证书快过期了，能续期吗", "vpn", "consult", None),
    # 密码重置后 vpn 连不上：词面命中 password+vpn 两个场景 → 期望触发 multi 引导
    ("密码重置后vpn连不上", "vpn", "troubleshoot", ["password", "vpn"]),

    # ================= 干扰/硬负例（5 条）：场景不支持 ≠ 故障，不得误入业务流 =================
    ("WiFi连不上", "other", "troubleshoot", None),        # 网络故障但非支持场景
    ("怎么安装打印机驱动", "other", "consult", None),
    ("电脑太卡了怎么办", "other", "troubleshoot", None),  # 同"很慢"：性能问题按故障处理
    ("帮我查一下明天的天气", "other", "consult", None),    # 无关话题，非纯寒暄 → 降级咨询
    ("在吗？", "other", "other", None),                    # 纯寒暄

    # ================= 多问题（3 条）：测 multi 节点引导，不误转人工 =================
    ("vpn连不上，密码也忘了", "vpn", "troubleshoot", ["password", "vpn"]),
    ("邮箱收不到，软件也装不上", "email", "troubleshoot", ["email", "software"]),
    ("电脑蓝屏，vpn还连不上", "vpn", "troubleshoot", None),  # 蓝屏非场景词 → 仅 vpn 命中
]
