"""RAG 检索质量评估：Recall@k / MRR（纯向量 vs 混合检索对比）。

测试集：运维域 10 条合成用例（"问题 → 期望命中的文档 source_url"），
来源 docs/knowledge/ 的 6 篇知识文档。用例设计对齐 Eval 方法论：
正例变体（测泛化）× 7 + 不同场景 × 3，每条用例的期望文档是"知识库中最相关的答案"。

指标：
- Recall@k：top-k 内命中期望文档的比例（k=1/3/5）
- MRR：首个命中位置的倒数均值（衡量排序质量）

输出：tests/rag_report.json（与意图识别 report.json 并列，README 可贴）。
"""
import json
from pathlib import Path

from app.rag.embedder import embed_text
from app.rag.retriever import _build_where, retrieve
from app.rag.store import create_store

# 运维域检索测试集：query → 期望命中的文档 source_url
CASES = [
    {"query": "VPN 证书过期了 Error 800 怎么续期", "expected": "docs/knowledge/vpn/cert-renewal.md"},
    {"query": "VPN 拨号失败报错 720 连不上", "expected": "docs/knowledge/vpn/error-codes.md"},
    {"query": "证书正常但 VPN 连不上 怀疑客户端配置坏了", "expected": "docs/knowledge/vpn/client-config.md"},
    {"query": "我忘记密码了 账号被锁定怎么办", "expected": "docs/knowledge/password/reset-sop.md"},
    {"query": "邮箱收不到邮件 IMAP 怎么配置", "expected": "docs/knowledge/email/config-guide.md"},
    {"query": "安装办公软件提示许可证无效 激活失败", "expected": "docs/knowledge/software/install-guide.md"},
    {"query": "Error 809 可能是防火墙拦截了 VPN", "expected": "docs/knowledge/vpn/error-codes.md"},
    {"query": "密码过期了 需要重置密码", "expected": "docs/knowledge/password/reset-sop.md"},
    {"query": "发不出邮件 SMTP 报错 553", "expected": "docs/knowledge/email/config-guide.md"},
    {"query": "证书状态正常 VPN 还是连不上 路由表异常", "expected": "docs/knowledge/vpn/client-config.md"},
]

REPORT_PATH = Path(__file__).resolve().parent / "rag_report.json"


def _recall_mrr(hits: list, expected: str) -> tuple[bool, float]:
    """一条查询：是否命中 + MRR 贡献（命中位置倒数，未命中 0）。"""
    for i, h in enumerate(hits):
        if h.metadata.get("source_url") == expected:
            return True, 1.0 / (i + 1)
    return False, 0.0


def _evaluate(name: str, search_fn) -> dict:
    """跑一组检索，返回 Recall@1/3/5 + MRR。search_fn(query) -> list[Hit]。"""
    hits_at = {k: 0 for k in (1, 3, 5)}
    mrr_sum = 0.0
    for case in CASES:
        hits = search_fn(case["query"])[:5]
        hit, mrr = _recall_mrr(hits, case["expected"])
        for k in hits_at:
            if hit and any(h.metadata.get("source_url") == case["expected"]
                           for h in hits[:k]):
                hits_at[k] += 1
        mrr_sum += mrr
    n = len(CASES)
    return {
        "name": name,
        "cases": n,
        "recall@1": round(hits_at[1] / n, 3),
        "recall@3": round(hits_at[3] / n, 3),
        "recall@5": round(hits_at[5] / n, 3),
        "mrr": round(mrr_sum / n, 3),
    }


def main() -> None:
    import argparse
    import tempfile

    from app.config import settings
    from app.rag.store import ChromaStore
    from app.rag.sync import run_sync
    from pathlib import Path

    parser = argparse.ArgumentParser()
    parser.add_argument("--tmp-kb", action="store_true",
                        help="用独立临时库（不与生产 Chroma 并发冲突，后台嵌入时用）")
    args = parser.parse_args()

    if args.tmp_kb:
        # 独立临时库：从 docs/knowledge 重建（33 chunks，几十秒）
        tmp = Path(tempfile.mkdtemp(prefix="kb_eval_"))
        store = ChromaStore(persist_dir=str(tmp / "chroma"), collection_name="kb_eval")
        report = run_sync(Path(settings.KB_ROOT), store, kb_name="eval_tmp")
        print(f"临时库构建: {report.summary}")
    else:
        store = create_store()

    print(f"库内 chunk 总数: {store.count()}")

    # ---- 纯向量（对照）----
    def vector_only(q: str):
        return store.query(embed_text(q), top_k=5, where=_build_where(None))

    # ---- 混合检索（BM25 + 向量 + RRF）----
    def hybrid(q: str):
        return retrieve(q, top_k=5, store=store)

    results = [
        _evaluate("vector_only", vector_only),
        _evaluate("hybrid", hybrid),
    ]

    print("\n=== RAG 检索评估（10 条运维用例）===")
    for r in results:
        print(f"[{r['name']}] Recall@1={r['recall@1']} "
              f"Recall@3={r['recall@3']} Recall@5={r['recall@5']} MRR={r['mrr']}")

    REPORT_PATH.write_text(json.dumps({
        "task": "rag_retrieval",
        "cases": len(CASES),
        "results": results,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n报告落盘: {REPORT_PATH}")


if __name__ == "__main__":
    main()
