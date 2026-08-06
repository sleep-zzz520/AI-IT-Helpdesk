"""咨询问答节点：Agentic 多跳检索 + 运维域 judge + 基于证据的回答生成。

为什么咨询要单独的节点（Phase 3 核心）：
- 故障路径（kb→risk→execute）的目的是"执行"，咨询诉求不该触发任何执行动作
- rag_query 是纯只读问答：检索知识库 → 判断证据够不够 → 生成回答
- 多跳（MultiHop-RAG 思路）：单跳检索不到完整答案时，judge 按证据实体
  规划下一跳检索词，直到证据充分或达到跳数上限——LangGraph 同一框架下
  用【节点内循环】实现（每跳记独立 trace，可观测）

安全边界：本节点不调用任何执行类 Tool（TOOL_REGISTRY 不碰）。
"""
import jieba

from app.agents.state import HelpdeskState
from app.llm import chat, chat_json
from app.rag.retriever import retrieve
from app.rag.transcribe import transcribe_data_url

MAX_HOPS = 2              # 最多跳数（每跳 = 一次检索 + 一次 judge）
HOP_TOP_K = 5             # 每跳召回数
EVIDENCE_MAX_CHARS = 800  # 单条证据进入 LLM 上下文的最大字符数（防超上下文）

# next_query 校验的无效词（运维问答泛词——不算"引用了证据实体"）
_STOP_TERMS = {"怎么", "如何", "什么", "可以", "需要", "帮助", "一下", "为什么",
               "处理", "解决", "问题", "配置", "设置", "这个", "那个"}


def _enrich_query_with_image(text: str, image_data_url: str | None) -> str:
    """截图转译 → 拼进检索 query（Phase 4 查询侧：截图变知识资产）。

    拼接格式是双通道的（混合检索两路各取所需）：
    - summary（自然语言摘要）→ 向量路（语义召回）
    - 错误码 + 关键文字（精确词）→ BM25 路（词法命中，历史截图块的错误码
      "800" 与用户截图转译出的 "800" 词面一致才能命中）
    转译失败/无图 → 返回原文（截图是增强不是依赖，检索必须始终可用）。
    """
    if not image_data_url:
        return text
    trans = transcribe_data_url(image_data_url, media_ref="user-screenshot")
    if trans is None:
        return text
    parts = [text]
    if trans.summary:
        parts.append(f"截图转译：{trans.summary}")
    if trans.error_code:
        parts.append(f"错误码：{trans.error_code}")
    if trans.key_texts:
        parts.append(f"关键文字：{' '.join(trans.key_texts)}")
    return " ".join(parts)


def _hit_to_evidence(hit) -> dict:
    """检索命中 → 证据条目（子块优先，无子块用父块；截断保长度）。

    父子块策略（见 rag/retriever）：Hit.text 是命中的小子块，Hit.parent_text
    是所在章节全文——答案生成需要完整章节，所以优先父块。
    """
    text = hit.parent_text or hit.text
    return {
        "id": hit.id,
        "text": text[:EVIDENCE_MAX_CHARS],
        "source": hit.metadata.get("source_url", ""),
    }


def _evidence_texts(evidence: list[dict]) -> str:
    """证据列表 → LLM 上下文（编号引用，judge 与回答生成共用同一格式）。"""
    return "\n\n".join(f"[证据{i + 1}] {e['text']}" for i, e in enumerate(evidence))


def _shares_evidence_terms(next_query: str, evidence: list[dict]) -> bool:
    """next_query 实体约束校验：分词后至少一个有效词出现在证据文本里。

    为什么这么校验：judge 是免费模型，可能输出与证据无关的检索词
    （"服务器 防火墙"凭空造词）——那会导致多跳检索越跳越偏。
    要求检索词与证据有实体交集 = 下一跳是对当前证据的"补查"而非"另起炉灶"。
    有效词 = 长度≥2 且不在运维问答泛词集合。
    """
    tokens = [t for t in jieba.lcut(next_query) if len(t) >= 2 and t not in _STOP_TERMS]
    if not tokens:
        return False
    blob = "".join(e["text"] for e in evidence)
    return any(t in blob for t in tokens)


def judge_evidence(query: str, evidence: list[dict]) -> dict:
    """运维域 judge：证据能否回答用户问题 + 方案能否执行；不足时给 next_query。

    与通用 RAG judge 的区别：判断标准是运维域的业务语义——
    1. enough/answerable：检索到的知识块是否包含用户问题的答案（不追求"证据无限全"）
    2. executable：方案是否需要转人工执行（涉及账号/系统/安全 = 需人工）
    3. next_query 必须引用证据中的实体（防乱跳，prompt 硬性要求）
    """
    prompt = f"""你是 IT 运维知识库的"证据充分性裁判"。用户提问和检索到的知识块如下。

【用户问题】{query}

【知识块】
{_evidence_texts(evidence)}

请按运维域标准判断（不是通用"证据够不够"标准）：
1. enough：知识块是否足以回答用户的问题（已检索到答案所在知识块 = true）
2. answerable：知识块能否直接回答用户的问题
3. executable：方案是否需要转人工执行（涉及账号/系统/安全等需人工操作 = true；纯自助配置说明 = false）
4. missing_info：还缺什么信息（不缺就空数组）
5. next_query：若不足，给出更精确的检索词。硬性要求：必须改写自知识块中出现的
   实体/概念（如知识块提到"临时密码、首次登录修改"，检索词必须包含这类词），不得凭空造词。

只输出 JSON：{{"enough": bool, "answerable": bool, "executable": bool,
"missing_info": [str], "next_query": str}}"""
    return chat_json([
        {"role": "system", "content": prompt},
        {"role": "user", "content": "请判断。"},
    ])


def generate_answer(query: str, evidence: list[dict], judge: dict) -> str:
    """基于证据生成回答（faithful 约束：只依据证据，不足则明说）。"""
    prompt = f"""你是 IT 运维服务台的知识问答助手。基于以下知识库内容回答用户问题。

【用户问题】{query}

【知识库内容】
{_evidence_texts(evidence)}

要求：
- 只依据知识库内容回答，不得编造
- 操作步骤清晰编号；涉及人工执行（账号/系统/安全操作）要明确提示"需联系人工处理"
- 知识不足以完整回答时，明确说明已解答部分与缺失部分
- 回答简洁（300 字内），结尾提示"如需进一步排查，请描述故障现象，我可转人工协助"
"""
    return chat([
        {"role": "system", "content": prompt},
        {"role": "user", "content": query},
    ], temperature=0.2)


def answer_question(question: str, scenario: str | None = None,
                    search_query: str | None = None) -> dict:
    """核心问答逻辑（节点与 RAGAS 评估共用）：多跳检索 → judge → 生成。

    为什么 question 与 search_query 分离（Phase 4 截图转译增强）：
    - question：用户的原始问题——judge 判断证据充分性、answer 生成都用它
    - search_query：实际检索用的 query——默认等于 question；
      用户带截图时，转译结果拼进 search_query 增强检索（截图→知识资产），
      但 judge/answer 保持干净的自然语言（增强是给"找"用的，不是给"答"用的）

    返回 {answer, evidence, hops}：
    - evidence: 跨跳去重后的证据列表 [{id, text, source}]
    - hops: 每跳记录 [{hop, query, hits, judge}]（Trace 面板数据源）

    循环终止条件（任一满足即停）：
    1. judge 判定 enough 且 answerable
    2. 达到 MAX_HOPS
    3. next_query 为空或不满足实体约束（judge 质量不合格 → 不冒险多跳）
    """
    evidence: list[dict] = []
    seen_ids: set[str] = set()
    hops: list[dict] = []
    current_query = search_query or question

    for hop in range(1, MAX_HOPS + 1):
        # 第 1 跳按场景过滤（缩小范围提精度）；后续跳放宽 scenario=None
        # （跨场景补检——"重置密码后邮箱报错"需要 password+email 两份证据）
        hop_scenario = scenario if hop == 1 else None
        try:
            hits = retrieve(current_query, scenario=hop_scenario, top_k=HOP_TOP_K)
        except Exception as e:  # noqa: BLE001 检索失败不 500，兜底
            hops.append({"hop": hop, "query": current_query, "hits": [],
                         "judge": {"error": f"检索失败: {e}"}})
            break
        # 性能优化（踩坑）：0 命中直接短路，不调 judge/generate——
        # 没证据就没答案，让 LLM 硬编只会幻觉；还白烧 2 次 LLM 调用
        # （实测 0 命中场景 judge 判断 not enough → 给 next_query →
        #  空证据不满足实体约束 → 终止，最终 generate 对着空证据编）
        if not hits:
            hops.append({"hop": hop, "query": current_query, "hits": [],
                         "judge": {"enough": False, "answerable": False,
                                   "reason": "检索 0 命中，跳过 LLM 判断"}})
            break
        for h in hits:
            if h.id not in seen_ids:
                seen_ids.add(h.id)
                evidence.append(_hit_to_evidence(h))

        try:
            judge = judge_evidence(question, evidence)  # judge 用原始问题（非增强检索词）
        except Exception as e:  # noqa: BLE001 judge 失败 → 退化为单跳直接回答
            hops.append({"hop": hop, "query": current_query,
                         "hits": [{"source": h.metadata.get("source_url", ""),
                                   "score": h.score, "preview": h.text[:60]}
                                  for h in hits],
                         "judge": {"error": f"judge 失败: {e}"}})
            break
        hops.append({
            "hop": hop,
            "query": current_query,
            "hits": [{"source": h.metadata.get("source_url", ""), "score": h.score,
                      "preview": h.text[:60]} for h in hits],
            "judge": judge,
        })

        if judge.get("enough") and judge.get("answerable"):
            break
        nq = (judge.get("next_query") or "").strip()
        if hop >= MAX_HOPS or not nq or not _shares_evidence_terms(nq, evidence):
            break
        current_query = nq

    # 0 命中/无证据：固定兜底话术（不调 generate 对着空证据编——幻觉 + 烧钱 + 慢）
    if not evidence:
        answer = ("知识库中暂无相关内容，无法回答您的具体问题。\n\n"
                  "如需进一步排查，请描述故障现象，我可转人工协助。")
        return {"answer": answer, "evidence": evidence, "hops": hops}

    answer = generate_answer(question, evidence, hops[-1].get("judge", {}))
    return {"answer": answer, "evidence": evidence, "hops": hops}


def rag_query_node(state: HelpdeskState) -> dict:
    """咨询问答节点：读最新用户消息（文本 + 可选截图）→ 检索问答 → 回复 + Trace。

    Trace 结构（前端"依据来源"展示）：
    {node: rag_query, result: {query, image_enriched, hops: [...], answer, sources}}
    """
    query = state["messages"][-1]["content"]
    image = state["messages"][-1].get("image")
    # 截图转译增强检索（截图→知识资产）；judge/answer 仍用原始 query
    enriched = _enrich_query_with_image(query, image) if image else query
    try:
        result = answer_question(query, state.get("intent"),
                                 search_query=enriched if enriched != query else None)
    except Exception as e:  # noqa: BLE001 —— 生成回答也失败：友好兜底
        reply = f"⚠️ 知识库查询失败（{type(e).__name__}），请稍后重试，或转人工客服处理。"
        return {
            "messages": state["messages"] + [{"role": "assistant", "content": reply}],
            "trace": [{"node": "rag_query", "result": {"query": query,
                                                       "image_enriched": enriched != query,
                                                       "error": str(e)}}],
        }
    return {
        "messages": state["messages"] + [{"role": "assistant", "content": result["answer"]}],
        "trace": [{
            "node": "rag_query",
            "result": {
                "query": query,
                "image_enriched": enriched != query,
                "hops": result["hops"],
                "answer": result["answer"],
                "sources": list(dict.fromkeys(
                    e["source"] for e in result["evidence"] if e["source"]
                )),
            },
        }],
    }
