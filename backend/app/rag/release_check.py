"""发布验收：验证权威指针、双路索引、父块与固定Git来源。失败退出非零。"""
import json

from app.rag.generations import configured_snapshot, state
from app.rag.retriever import RetrievalStats, retrieve


def main():
    current = state()
    snapshot = configured_snapshot()
    assert current["active_generation"], "没有已发布代"
    assert current["source_commit"] == snapshot.commit, "已发布来源与本次部署不符"
    assert current["doc_count"] == len([d for d in snapshot.docs if d.meta.get("status", "active") == "active"])
    stats = RetrievalStats()
    hits = retrieve("VPN 证书过期 Error 800 如何续期", scenario="vpn", stats=stats, joint=False)
    assert hits and stats.vector_recall > 0 and stats.bm25_recall > 0, "双路召回验收失败"
    assert all(hit.parent_text for hit in hits), "父块回填验收失败"
    assert all(hit.source_collection == current["active_collection"] for hit in hits), "检索版本不一致"
    print(json.dumps({"status": "ok", "generation": current["active_generation"],
                      "commit": current["source_commit"], "docs": current["doc_count"],
                      "vector_recall": stats.vector_recall, "bm25_recall": stats.bm25_recall}, ensure_ascii=False))


if __name__ == "__main__":
    main()
