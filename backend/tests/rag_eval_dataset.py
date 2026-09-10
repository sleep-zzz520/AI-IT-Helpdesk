"""带 Gold 标注的 RAG 测评集。

一条样本同时描述问题、标准相关 Chunk、参考答案、是否可回答、问题类型与难度。
检索评测只使用 ``RETRIEVAL_CASES`` 的 60 条可回答样本；端到端评测从中抽取
代表性样本，再加多跳、信息不全、无答案和陈旧前提样本，分别观察检索、生成、
澄清与拒答。这样不会把“无答案”错误计入 Recall@k 的分母。
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, replace
from pathlib import Path


@dataclass(frozen=True)
class GoldChunk:
    """稳定的 Gold Chunk 标注：源文档 + 二级标题，而不是脆弱的序号。"""

    source_url: str
    section: str


@dataclass(frozen=True)
class RagEvalCase:
    """一条可同时服务检索、生成和端到端评测的标注样本。"""

    case_id: str
    query: str
    gold_chunks: tuple[GoldChunk, ...]
    gold_answer: str
    answerable: bool
    question_type: str
    difficulty: str
    scenario: str | None = None
    expected_behavior: str = "answer"  # answer / clarify / refuse
    gold_answer_terms: tuple[str, ...] = ()
    expected_min_hops: int = 1


VPN_CERT = "docs/knowledge/vpn/cert-renewal.md"
VPN_ERROR = "docs/knowledge/vpn/error-codes.md"
VPN_CONFIG = "docs/knowledge/vpn/client-config.md"
VPN_INSTALL = "docs/knowledge/vpn/vpn-install-guide.md"
PASSWORD_RESET = "docs/knowledge/password/reset-sop.md"
PASSWORD_LOCK = "docs/knowledge/password/account-lock.md"
PASSWORD_POLICY = "docs/knowledge/password/password-policy.md"
EMAIL_CONFIG = "docs/knowledge/email/config-guide.md"
EMAIL_TROUBLE = "docs/knowledge/email/troubleshoot.md"
EMAIL_QUOTA = "docs/knowledge/email/mailbox-quota.md"
SOFTWARE_INSTALL = "docs/knowledge/software/install-guide.md"
SOFTWARE_LICENSE = "docs/knowledge/software/license-activation.md"
SOFTWARE_UPDATE = "docs/knowledge/software/software-update.md"
EXPENSE = "docs/knowledge/finance/expense-reimbursement.md"
ONBOARDING = "docs/knowledge/hr/onboarding-guide.md"
VACATION = "docs/knowledge/general/vacation-policy.md"


# 每个 Gold Chunk 配一条简洁的参考事实。由样本构造函数组合成 Gold Answer，
# 使所有样本都不是只有“正确文档”而没有“正确回答”。
_CHUNK_ANSWERS = {
    (VPN_CERT, "症状与判断"): "Error 800 且监控显示证书过期时，应按证书续期流程处理。",
    (VPN_CERT, "处理步骤"): "先在监控平台确认状态，证书过期后执行续期，再重新拨号验证。",
    (VPN_CERT, "操作命令"): "续期命令是 certutil -renew，执行后需要重新建立 VPN 连接。",
    (VPN_ERROR, "Error 800"): "Error 800 先查证书，过期则续期；证书正常再检查客户端配置。",
    (VPN_ERROR, "Error 720"): "Error 720 通常是拨号连接组件损坏，应删除旧连接后重新创建并拨号。",
    (VPN_ERROR, "Error 721"): "Error 721 多为网关不可达或 DNS 解析失败，应检查网络连通性和 DNS。",
    (VPN_ERROR, "Error 809"): "Error 809 多由防火墙、安全软件或网络环境变化导致，应检查放行规则后重试。",
    (VPN_ERROR, "Error 812"): "Error 812 表示连接被策略阻止，应核对账号、设备登记和旧会话。",
    (VPN_CONFIG, "适用场景"): "证书正常但 VPN 仍报 Error 800 时，应优先排查客户端配置。",
    (VPN_CONFIG, "常见配置问题"): "重点检查系统代理、公司网段路由表和损坏的客户端配置文件。",
    (VPN_CONFIG, "排查顺序"): "排查顺序是确认协议书正常、检查代理、检查路由表，再重置客户端配置。",
    (VPN_INSTALL, "安装前准备"): "VPN 客户端只能从公司软件中心获取，安装需要本地管理员权限。",
    (VPN_INSTALL, "安装步骤"): "安装后导入 vpn.acme.com.cn 配置，使用域账号绑定，并重启电脑。",
    (VPN_INSTALL, "常见问题"): "提示找不到设备通常是杀毒软件拦截驱动，可临时关闭后重装。",
    (PASSWORD_RESET, "症状与判断"): "忘记密码、密码过期或无法自助重置都应先收集域账号信息。",
    (PASSWORD_RESET, "处理步骤"): "先人工核验身份，再重置为临时密码；账号锁定时先确认风险后处理。",
    (PASSWORD_RESET, "注意事项"): "临时密码首次登录必须修改，重置后原密码立即失效并应退出其他设备。",
    (PASSWORD_LOCK, "症状与判断"): "账号锁定与普通密码错误不同，可能来自多次失败、暴力破解或异地登录风控。",
    (PASSWORD_LOCK, "处理步骤"): "先查登录日志；异常 IP 或高频尝试时应冻结账号并通知安全团队，不能直接解锁。",
    (PASSWORD_POLICY, "密码复杂度要求"): "密码至少 8 位，建议 12 位，需满足三类字符且每 90 天修改一次，不得复用最近 5 次。",
    (PASSWORD_POLICY, "账号锁定规则"): "连续输错 5 次会锁定 30 分钟，连续 10 次需管理员手动解锁。",
    (EMAIL_CONFIG, "症状与判断"): "邮箱问题先区分收信、发信还是都异常，再判断 IMAP、SMTP 或账号问题。",
    (EMAIL_CONFIG, "服务器参数"): "IMAP 服务器为 imap.company.com、端口 993、SSL；SMTP 为 smtp.company.com、端口 465、SSL。",
    (EMAIL_CONFIG, "处理步骤"): "按客户端和协议填写参数，分别检查 IMAP 或 SMTP 认证，最后互发测试邮件。",
    (EMAIL_CONFIG, "注意事项"): "企业邮箱应使用客户端授权码，换设备后建议吊销不再使用的设备授权。",
    (EMAIL_TROUBLE, "症状与判断"): "收不到邮件先查 IMAP，发不出邮件先查 SMTP；553/554 与发件认证或反垃圾有关。",
    (EMAIL_TROUBLE, "处理步骤"): "先区分收发方向，再核对服务器、密码和客户端授权；仍失败时带报错和截图转人工。",
    (EMAIL_TROUBLE, "注意事项"): "密码重置后必须更新所有邮箱客户端的密码，否则客户端会继续使用旧密码失败。",
    (EMAIL_QUOTA, "容量配额"): "个人邮箱默认 10GB，超过 90% 会预警；超配额后无法接收新邮件。",
    (EMAIL_QUOTA, "清理与归档"): "优先归档三年以上邮件，大附件存网盘后删除附件，归档邮件可保留 7 年。",
    (EMAIL_QUOTA, "常见问题"): "邮箱已满时还要检查已发送、草稿和订阅邮件；归档搜索最长可能延迟 24 小时。",
    (SOFTWARE_INSTALL, "症状与判断"): "安装软件前需确认软件名称、操作系统和授权类型，家庭版与企业版可能不匹配。",
    (SOFTWARE_INSTALL, "处理步骤"): "只从企业软件分发平台安装；KMS 激活失败先检查系统时间、网络和设备是否入域。",
    (SOFTWARE_LICENSE, "症状与判断"): "许可证无效可能是密钥与版本不匹配，激活次数超限可能是旧设备未释放授权。",
    (SOFTWARE_LICENSE, "处理步骤"): "先核对密钥类型和版本，再检查激活服务器网络；旧设备占用需联系管理员释放或迁移授权。",
    (SOFTWARE_UPDATE, "更新规则"): "软件中心更新自动安装，可推迟最多 14 天；超过期限会强制执行并提前提醒。",
    (SOFTWARE_UPDATE, "常见问题"): "更新后打不开先重启，仍失败可回滚版本；0x80070643 多与空间不足或安装源损坏有关。",
    (EXPENSE, "报销范围"): "住宿标准通常为 400 元每晚、一线城市 500 元；餐饮补贴 120 元每天。",
    (EXPENSE, "报销步骤"): "出差结束 5 个工作日内在 OA 提交报销单并上传合规发票，主管和财务审核后打款。",
    (EXPENSE, "注意事项"): "电子发票必须提供 PDF 原件，税号错误会被打回；超过 5000 元需 CFO 二次审批。",
    (ONBOARDING, "入职流程"): "入职当天携身份证和学历证明到 3 号楼 2 层办理手续，并领取临时工牌和门禁卡。",
    (ONBOARDING, "常见问题"): "门禁失效联系行政分机 8001；工牌丢失到 HR 前台登记补办。",
    (VACATION, "年假额度"): "入职满 1 年有 5 天年假，未休完最多结转 5 天至次年 3 月底。",
    (VACATION, "请假规则"): "年假需提前 3 个工作日申请；病假通常需要二级甲等以上医院证明。",
    (VACATION, "常见问题"): "离职未休年假按日工资 200% 补偿，调休有效期为 6 个月。",
}


def _gold_chunks(source_url: str, sections: tuple[str, ...]) -> tuple[GoldChunk, ...]:
    return tuple(GoldChunk(source_url, section) for section in sections)


def _gold_answer(chunks: tuple[GoldChunk, ...]) -> str:
    try:
        return " ".join(_CHUNK_ANSWERS[(chunk.source_url, chunk.section)] for chunk in chunks)
    except KeyError as exc:  # 数据集维护错误必须在 import 时暴露，不能静默降级。
        raise ValueError(f"缺少 Gold Answer：{exc.args[0]}") from exc


def _answerable(
    case_id: str,
    query: str,
    source_url: str,
    sections: tuple[str, ...],
    question_type: str,
    difficulty: str,
    *,
    scenario: str | None = None,
    gold_answer_terms: tuple[str, ...] = (),
) -> RagEvalCase:
    answer_chunks = _gold_chunks(source_url, sections)
    # 当前链路是“小块检索、父块生成”，并且 RRF 会按文档去重。因此单文档
    # 问题只标一个最能定位问题的子 Chunk；命中后 parent_text 会补全后续步骤。
    # Gold Answer 仍可覆盖同一父文档中的多个事实，避免拿“检索一小段”误测生成。
    retrieval_chunks = answer_chunks[:1]
    return RagEvalCase(
        case_id=case_id,
        query=query,
        gold_chunks=retrieval_chunks,
        gold_answer=_gold_answer(answer_chunks),
        answerable=True,
        question_type=question_type,
        difficulty=difficulty,
        scenario=scenario,
        gold_answer_terms=gold_answer_terms,
    )


# 60 条检索/排序样本。它们全部有标准 Chunk 和参考答案，既能测检索，也可被
# 端到端集按 ID 复用。无答案、澄清和多跳样本在下方单列，不污染 Recall@k。
RETRIEVAL_CASES = (
    _answerable("r01", "VPN 证书过期了 Error 800 怎么续期", VPN_CERT, ("症状与判断", "处理步骤", "操作命令"), "direct", "medium", scenario="vpn"),
    _answerable("r02", "VPN 拨号失败报错 720 连不上", VPN_ERROR, ("Error 720",), "direct", "medium", scenario="vpn"),
    _answerable("r03", "证书正常但 VPN 连不上 怀疑客户端配置坏了", VPN_CONFIG, ("适用场景", "常见配置问题"), "direct", "medium", scenario="vpn"),
    _answerable("r04", "忘记密码了 怎么重置密码", PASSWORD_RESET, ("症状与判断", "处理步骤"), "direct", "medium", scenario="password"),
    _answerable("r05", "邮箱收不到邮件 IMAP 怎么配置", EMAIL_CONFIG, ("服务器参数", "处理步骤"), "direct", "medium", scenario="email"),
    _answerable("r06", "安装办公软件提示许可证无效 激活失败", SOFTWARE_LICENSE, ("症状与判断", "处理步骤"), "direct", "medium", scenario="software"),
    _answerable("r07", "Error 809 可能是防火墙拦截了 VPN", VPN_ERROR, ("Error 809",), "exact_term", "medium", scenario="vpn"),
    _answerable("r08", "密码过期了 需要重置密码", PASSWORD_RESET, ("症状与判断", "处理步骤"), "direct", "medium", scenario="password"),
    _answerable("r09", "发不出邮件 SMTP 报错 553", EMAIL_CONFIG, ("症状与判断", "处理步骤"), "exact_term", "medium", scenario="email"),
    _answerable("r10", "证书状态正常 VPN 还是连不上 路由表异常", VPN_CONFIG, ("常见配置问题", "排查顺序"), "distractor", "hard", scenario="vpn"),
    _answerable("r11", "VPN Error 812 连接被阻止 怎么办", VPN_ERROR, ("Error 812",), "exact_term", "medium", scenario="vpn"),
    _answerable("r12", "certutil -renew 命令怎么用", VPN_CERT, ("操作命令",), "exact_term", "easy", scenario="vpn"),
    _answerable("r13", "vpn 720 报错", VPN_ERROR, ("Error 720",), "exact_term", "easy", scenario="vpn"),
    _answerable("r14", "Office 企业批量许可和零售密钥不匹配", SOFTWARE_LICENSE, ("症状与判断", "处理步骤"), "exact_term", "medium", scenario="software"),
    _answerable("r15", "VPN 客户端软件在哪里下载安装", VPN_INSTALL, ("安装前准备", "安装步骤"), "distractor", "medium", scenario="vpn"),
    _answerable("r16", "密码安全策略是什么 复杂度要求", PASSWORD_POLICY, ("密码复杂度要求",), "direct", "easy", scenario="password"),
    _answerable("r17", "邮箱满了收不到新邮件怎么办", EMAIL_QUOTA, ("容量配额", "清理与归档"), "distractor", "hard", scenario="email"),
    _answerable("r18", "办公软件更新是自动的吗 能推迟吗", SOFTWARE_UPDATE, ("更新规则",), "direct", "medium", scenario="software"),
    _answerable("r19", "Outlook 客户端一直提示密码错误 怎么重新配置", EMAIL_CONFIG, ("处理步骤",), "distractor", "hard", scenario="email"),
    _answerable("r20", "提示证书过期 报错 Error 800 怎么续期", VPN_CERT, ("症状与判断", "处理步骤"), "distractor", "hard", scenario="vpn"),
    _answerable("r21", "在哪里查看我的 VPN 证书是否已经失效", VPN_CERT, ("症状与判断",), "paraphrase", "easy", scenario="vpn"),
    _answerable("r22", "VPN 证书剩不到 30 天了，需要提前怎么处理", VPN_CERT, ("症状与判断",), "paraphrase", "medium", scenario="vpn"),
    _answerable("r23", "公司 VPN 客户端应该从什么渠道安装", VPN_INSTALL, ("安装前准备",), "direct", "easy", scenario="vpn"),
    _answerable("r24", "装 VPN 时提示找不到设备，杀毒软件可能拦截了驱动", VPN_INSTALL, ("常见问题",), "paraphrase", "medium", scenario="vpn"),
    _answerable("r25", "公司密码为什么九十天就要求修改，旧密码还能继续用吗", PASSWORD_POLICY, ("密码复杂度要求",), "paraphrase", "medium", scenario="password"),
    _answerable("r26", "企业账号的密码至少要多长，复杂度有什么要求", PASSWORD_POLICY, ("密码复杂度要求",), "direct", "easy", scenario="password"),
    _answerable("r27", "账号因异常 IP 的多次登录失败被锁了，能直接解锁吗", PASSWORD_LOCK, ("症状与判断", "处理步骤"), "distractor", "hard", scenario="password"),
    _answerable("r28", "我换了手机号，无法自助找回公司账号密码", PASSWORD_RESET, ("症状与判断",), "paraphrase", "medium", scenario="password"),
    _answerable("r29", "新电脑上的 Outlook 要怎样填写企业邮箱的收件服务器", EMAIL_CONFIG, ("服务器参数",), "direct", "easy", scenario="email"),
    _answerable("r30", "邮箱收信失败，客户端是不是要改成授权码登录", EMAIL_CONFIG, ("注意事项", "处理步骤"), "paraphrase", "medium", scenario="email"),
    _answerable("r31", "密码重置后邮箱客户端一直连不上，该先检查什么", EMAIL_TROUBLE, ("注意事项",), "cross_context", "hard", scenario="email"),
    _answerable("r32", "邮箱快满了，系统的容量预警是什么时候发", EMAIL_QUOTA, ("容量配额",), "direct", "easy", scenario="email"),
    _answerable("r33", "邮箱容量超限后不能收新邮件，应该怎样归档清理", EMAIL_QUOTA, ("清理与归档",), "direct", "medium", scenario="email"),
    _answerable("r34", "办公软件更新可以延后多久，超过期限会怎样", SOFTWARE_UPDATE, ("更新规则",), "direct", "medium", scenario="software"),
    _answerable("r35", "软件更新失败后报错，如何处理临时目录和安装源", SOFTWARE_UPDATE, ("常见问题",), "paraphrase", "medium", scenario="software"),
    _answerable("r36", "Office 的 KMS 激活失败，系统时间会有影响吗", SOFTWARE_INSTALL, ("处理步骤",), "direct", "medium", scenario="software"),
    _answerable("r37", "新电脑不在企业授权名单里，怎样完成办公软件激活", SOFTWARE_LICENSE, ("处理步骤",), "direct", "medium", scenario="software"),
    _answerable("r38", "出差结束后几天内要提交报销，酒店费用标准是多少", EXPENSE, ("报销范围", "报销步骤"), "direct", "medium", scenario="other"),
    _answerable("r39", "新员工入职当天要带哪些材料，去哪个楼层办理", ONBOARDING, ("入职流程",), "direct", "easy", scenario="other"),
    _answerable("r40", "年假需要提前几天申请，没休完能否结转", VACATION, ("年假额度", "请假规则"), "direct", "medium", scenario="other"),
    _answerable("r41", "VPN Client vpn.acme.com.cn", VPN_INSTALL, ("安装步骤",), "exact_term", "easy", scenario="vpn"),
    _answerable("r42", "route print 缺少公司网段", VPN_CONFIG, ("常见配置问题",), "exact_term", "medium", scenario="vpn"),
    _answerable("r43", "VPN Error 721 DNS 网关不可达", VPN_ERROR, ("Error 721",), "exact_term", "medium", scenario="vpn"),
    _answerable("r44", "imap.company.com 993 SSL", EMAIL_CONFIG, ("服务器参数",), "exact_term", "easy", scenario="email"),
    _answerable("r45", "SMTP 465 SSL 发信配置", EMAIL_CONFIG, ("服务器参数",), "exact_term", "easy", scenario="email"),
    _answerable("r46", "办公软件更新 0x80070643", SOFTWARE_UPDATE, ("常见问题",), "exact_term", "medium", scenario="software"),
    _answerable("r47", "报销 5000 元 CFO 二次审批", EXPENSE, ("注意事项",), "exact_term", "medium", scenario="other"),
    _answerable("r48", "门禁失效 分机 8001", ONBOARDING, ("常见问题",), "exact_term", "easy", scenario="other"),
    _answerable("r49", "VPN 已安装但证书正常仍报 Error 800，代理设置怎么检查", VPN_CONFIG, ("适用场景", "常见配置问题"), "distractor", "hard", scenario="vpn"),
    _answerable("r50", "VPN 报 800 且监控显示证书已经过期，下一步该做什么", VPN_CERT, ("症状与判断", "处理步骤"), "distractor", "hard", scenario="vpn"),
    _answerable("r51", "密码连续输错 5 次后锁定 30 分钟，会自动恢复吗", PASSWORD_POLICY, ("账号锁定规则",), "distractor", "hard", scenario="password"),
    _answerable("r52", "账号锁定时发现异常来源 IP，应该冻结还是马上解锁", PASSWORD_LOCK, ("处理步骤",), "distractor", "hard", scenario="password"),
    _answerable("r53", "IMAP 参数已填对仍收不到邮件，客户端授权可能被拦截", EMAIL_TROUBLE, ("处理步骤",), "distractor", "hard", scenario="email"),
    _answerable("r54", "邮箱能收信但 60MB 附件发不出去，应排查什么", EMAIL_TROUBLE, ("症状与判断", "处理步骤"), "distractor", "hard", scenario="email"),
    _answerable("r55", "邮箱显示已满，想清理已发送邮件和大附件释放容量", EMAIL_QUOTA, ("清理与归档", "常见问题"), "distractor", "hard", scenario="email"),
    _answerable("r56", "Office 更新后打不开，是否能回退上一个版本", SOFTWARE_UPDATE, ("常见问题",), "distractor", "hard", scenario="software"),
    _answerable("r57", "新电脑预装家庭版 Office，怎样换成公司的企业授权版本", SOFTWARE_INSTALL, ("症状与判断", "处理步骤"), "distractor", "hard", scenario="software"),
    _answerable("r58", "软件无法连接激活服务器，怀疑代理或防火墙拦截", SOFTWARE_LICENSE, ("症状与判断", "处理步骤"), "distractor", "hard", scenario="software"),
    _answerable("r59", "差旅发票税号写错被退回，重新提交时要注意什么", EXPENSE, ("注意事项",), "distractor", "hard", scenario="other"),
    _answerable("r60", "离职时没休完的年假，补偿按什么标准计算", VACATION, ("常见问题",), "distractor", "hard", scenario="other"),
)


# 这十条不进入单跳 Recall@k：其中多跳需要多个证据，无答案/信息不全也没有
# “正确文档”。它们用于端到端检验生成、澄清和拒答，防止分数只来自送分题。
EDGE_CASES = (
    RagEvalCase(
        "e01", "重置密码后邮箱客户端提示密码错误，怎么恢复收信", (
            GoldChunk(EMAIL_TROUBLE, "注意事项"), GoldChunk(PASSWORD_RESET, "注意事项")
        ), "密码重置后原密码立即失效，需要在邮箱客户端更新为新密码；仍无法收信时再检查客户端配置与授权。",
        True, "multi_hop", "hard", "email", "answer", ("原密码", "更新", "客户端"), 2,
    ),
    RagEvalCase(
        "e02", "邮箱账号被锁定且登录日志有异常 IP，应该立即解锁吗", (
            GoldChunk(PASSWORD_LOCK, "处理步骤"),
        ), "异常 IP 或高频失败可能是攻击，不能立即解锁；应先查登录日志、冻结账号并通知安全团队。",
        True, "multi_hop", "hard", "email", "answer", ("登录日志", "冻结", "安全团队"), 2,
    ),
    RagEvalCase(
        "e03", "VPN 连不上", (), "需要补充错误码、证书状态、设备和网络环境，才能继续判断。",
        False, "incomplete", "hard", "vpn", "clarify", ("错误码", "证书", "设备"), 1,
    ),
    RagEvalCase(
        "e04", "邮箱坏了怎么办", (), "需要补充是收不到、发不出还是都异常，以及客户端和报错信息。",
        False, "incomplete", "hard", "email", "clarify", ("收不到", "发不出", "客户端"), 1,
    ),
    RagEvalCase(
        "e05", "办公软件激活失败", (), "需要补充软件名称、操作系统、授权类型和具体报错，才能定位原因。",
        False, "incomplete", "hard", "software", "clarify", ("软件", "操作系统", "报错"), 1,
    ),
    RagEvalCase(
        "e06", "公司打印机卡纸怎么处理", (), "知识库中暂无相关内容，需转人工协助。",
        False, "no_answer", "hard", None, "refuse", ("知识库中暂无", "转人工"), 1,
    ),
    RagEvalCase(
        "e07", "会议室空调不制冷怎么修", (), "知识库中暂无相关内容，需转人工协助。",
        False, "no_answer", "hard", None, "refuse", ("知识库中暂无", "转人工"), 1,
    ),
    RagEvalCase(
        "e08", "Java 编译时报空指针异常怎么排查", (), "知识库中暂无相关内容，需转人工协助。",
        False, "no_answer", "hard", None, "refuse", ("知识库中暂无", "转人工"), 1,
    ),
    RagEvalCase(
        "e09", "听说办公软件更新能推迟 30 天，这个规则现在还有效吗", (
            GoldChunk(SOFTWARE_UPDATE, "更新规则"),
        ), "当前规则只允许推迟最多 14 天；超过期限会强制执行，并在此前持续提醒。",
        True, "stale_knowledge_conflict", "hard", "software", "answer", ("14 天", "强制"), 1,
    ),
    RagEvalCase(
        "e10", "有人说公司密码 60 天改一次并且可以重复使用旧密码，这还是现行要求吗", (
            GoldChunk(PASSWORD_POLICY, "密码复杂度要求"),
        ), "当前密码每 90 天必须修改，并且不得复用最近 5 次使用过的密码。",
        True, "stale_knowledge_conflict", "hard", "password", "answer", ("90 天", "最近 5 次"), 1,
    ),
)


_E2E_BASE_IDS = {
    "r01", "r02", "r04", "r05", "r06", "r09", "r16", "r17", "r18", "r38", "r39", "r40",
}
_E2E_GOLD_TERMS = {
    "r01": ("certutil", "重新拨号"),
    "r02": ("删除旧连接", "重新创建"),
    "r04": ("身份", "临时密码"),
    "r05": ("imap.company.com", "993"),
    "r06": ("密钥", "授权"),
    "r09": ("SMTP", "认证"),
    "r16": ("8", "90", "最近 5 次"),
    "r17": ("10GB", "归档"),
    "r18": ("14 天", "强制"),
    "r38": ("5 个工作日", "400"),
    "r39": ("身份证", "3 号楼"),
    "r40": ("3 个工作日", "5 天"),
}
E2E_CASES = tuple(
    replace(case, gold_answer_terms=_E2E_GOLD_TERMS[case.case_id])
    for case in RETRIEVAL_CASES if case.case_id in _E2E_BASE_IDS
) + EDGE_CASES
ALL_CASES = RETRIEVAL_CASES + EDGE_CASES


def validate_cases(cases: tuple[RagEvalCase, ...]) -> None:
    """在真正调用模型前验证人工标注本身，避免脏数据伪造高低分。"""
    ids = [case.case_id for case in cases]
    if len(ids) != len(set(ids)):
        raise ValueError("RAG 测评集存在重复 case_id")
    for case in cases:
        if not all((case.query.strip(), case.gold_answer.strip(), case.question_type, case.difficulty)):
            raise ValueError(f"{case.case_id} 缺少必填标注")
        if case.answerable != bool(case.gold_chunks):
            raise ValueError(f"{case.case_id} 的可回答标记与 Gold Chunk 不一致")
        if case.expected_behavior not in {"answer", "clarify", "refuse"}:
            raise ValueError(f"{case.case_id} 的期望行为非法")
        if case.expected_min_hops < 1:
            raise ValueError(f"{case.case_id} 的跳数下限非法")


def resolve_gold_chunk_ids(
    cases: tuple[RagEvalCase, ...], kb_root: Path,
) -> dict[str, frozenset[str]]:
    """将“文档 + 标题”的人工标注解析为当前切分策略下的真实 Chunk ID。

    Chunk 序号会随文档标题增删而改变；数据集保存语义稳定的标题，评测开始时
    再由当前 ``split_markdown`` 解析 ID，既能真正按 Chunk 评分，又能及时发现
    知识文档改动后 Gold 标注失效。
    """
    from app.rag.loader import load_doc
    from app.rag.splitter import split_markdown

    resolved: dict[str, frozenset[str]] = {}
    split_cache: dict[str, object] = {}
    prefix = "docs/knowledge/"
    for case in cases:
        ids: set[str] = set()
        for gold in case.gold_chunks:
            rel_path = gold.source_url.removeprefix(prefix)
            if gold.source_url == rel_path:
                raise ValueError(f"{case.case_id} 的 source_url 不在知识库根目录下")
            if gold.source_url not in split_cache:
                doc = load_doc(kb_root / rel_path, kb_root)
                if doc is None:
                    raise ValueError(f"{case.case_id} 的 Gold 文档不存在：{gold.source_url}")
                split_cache[gold.source_url] = split_markdown(doc)
            split = split_cache[gold.source_url]
            matches = [chunk.id for chunk in split.children if chunk.text.startswith(f"## {gold.section}")]
            if len(matches) != 1:
                raise ValueError(
                    f"{case.case_id} 的 Gold Chunk 无法唯一解析：{gold.source_url}#{gold.section}"
                )
            ids.update(matches)
        resolved[case.case_id] = frozenset(ids)
    return resolved


validate_cases(ALL_CASES)
EXPECTED_RETRIEVAL_CASES = 60
assert len(RETRIEVAL_CASES) == EXPECTED_RETRIEVAL_CASES
assert len(E2E_CASES) == 22


def dataset_summary(cases: tuple[RagEvalCase, ...]) -> dict[str, dict[str, int]]:
    """报告中展示类型和难度构成，避免只报一个总分。"""
    return {
        "question_types": dict(sorted(Counter(case.question_type for case in cases).items())),
        "difficulties": dict(sorted(Counter(case.difficulty for case in cases).items())),
        "behaviors": dict(sorted(Counter(case.expected_behavior for case in cases).items())),
    }
