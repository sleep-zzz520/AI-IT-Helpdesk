"""④知识库匹配节点：规则护栏优先，RAG 检索兜底（场景无关）。

输入：intent（场景）+ error_code + cert_expired。
输出：kb_match（命中方案 / 未命中），供⑤风险分级和⑥执行用。

两级匹配（企业级最佳实践：确定性优先，检索兜底）：
1. 规则护栏（knowledge_base.KB 精确表）：完全匹配的已知组合 → 100% 确定，
   零 LLM 调用、零延迟、可预测。只保留"精确且风险明确"的高危护栏组合。
2. RAG 检索（rag.retriever）：规则未命中时，按 scenario + 状态构造检索式，
   从知识文档库召回最相关方案。场景无关——检索器只认 metadata 过滤
   （scenario/status/valid_to），不认识任何场景专属字段（error_code 是
   检索词而不是硬编码字段），加新场景 = 加文档，节点代码零改动。

Trace 增强：记录检索来源（命中 chunk / score / source_url / 父块摘要）——
"Agent 从知识库检索到哪个方案、凭什么信它"全程可审计（Trace 面板直接展示）。
"""
from app.agents.state import HelpdeskState
from app.rag.retriever import retrieve
from app.tools.knowledge_base import match_solution

# 检索兜底的召回数
RAG_TOP_K = 3


def _build_query(scenario: str, error_code: str, cert_expired: bool) -> str:
    """把用户情况拼成检索式（语义检索对口语描述更友好）。

    例：vpn + 800 + 过期 → "VPN 证书过期 Error 800 续期"
        vpn + 720       → "VPN Error 720 拨号连接 重建"
    """
    parts = [scenario]
    if error_code:
        parts.append(f"Error {error_code}")
    if scenario == "vpn":
        parts.append("证书过期" if cert_expired else "证书正常")
    return " ".join(parts)


def _to_kb_match(hit) -> dict:
    """检索命中 → kb_match（结构兼容 risk/execute 节点）。

    risk 默认 high（安全优先：来源不明的方案宁可转人工，
    与 risk.py 的 DEFAULT_RISK 一致；我们的文档 frontmatter 都显式标了 risk）。
    """
    return {
        "matched": True,
        "solution": hit.text,                          # 命中章节文本（含处理步骤）
        "action": hit.metadata.get("action", ""),      # 驱动执行（来自文档 frontmatter）
        "risk": hit.metadata.get("risk", "high"),
        "source": hit.metadata.get("source_url", ""),
        "score": hit.score,
    }


def kb_node(state: HelpdeskState) -> dict:
    scenario = state.get("intent", "")
    error_code = state.get("error_code", "")
    cert_expired = bool(state.get("cert_status", {}).get("expired"))

    # ① 规则护栏：精确命中直接走（100% 确定，零成本）
    rule = match_solution(scenario, error_code, cert_expired)
    if rule["matched"]:
        return {
            "kb_match": rule,
            "trace": [{
                "node": "kb",
                "result": {"mode": "rule", "solution": rule["solution"],
                           "action": rule["action"], "risk": rule["risk"]},
            }],
        }

    # ② RAG 兜底：规则未命中 → 检索知识文档库
    try:
        query = _build_query(scenario, error_code, cert_expired)
        hits = retrieve(query, scenario=scenario, top_k=RAG_TOP_K)
    except Exception as e:
        return {
            "kb_match": {"matched": False, "reason": f"知识库检索失败: {e}"},
            "trace": [{"node": "kb", "result": {"mode": "rag", "error": str(e)}}],
        }

    if not hits:
        return {
            "kb_match": {"matched": False,
                         "reason": f"知识库未检索到 [{scenario}] 相关方案"},
            "trace": [{"node": "kb", "result": {"mode": "rag", "query": query, "hits": []}}],
        }

    best = hits[0]
    kb_match = _to_kb_match(best)
    return {
        "kb_match": kb_match,
        "trace": [{
            "node": "kb",
            "result": {
                "mode": "rag",
                "query": query,
                "solution": kb_match["solution"],
                "action": kb_match["action"],
                "risk": kb_match["risk"],
                # 检索来源（Trace 可视化：Agent 凭什么信这个方案）
                "hits": [{"source": h.metadata.get("source_url"),
                          "score": h.score,
                          "preview": h.text[:60]} for h in hits],
            },
        }],
    }
