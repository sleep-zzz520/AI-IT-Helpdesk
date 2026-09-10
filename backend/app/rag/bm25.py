"""BM25 稀疏检索（jieba 分词 + rank_bm25 倒排索引）。

为什么 BM25 与向量检索并存（混合检索，教程 chapter4）：
- 向量检索：语义泛化（"VPN连不上" ↔ "拨号失败"），但专业术语/错误码
  这类"精确 token"容易被稀释（"Error 800" 的 800 在 1024 维向量里占比极小）
- BM25：词法精确匹配（"800"、"720"、"certutil" 必须原词命中），
  可解释、零训练、错误码和专有名词的命中率远高于向量
- 运维场景两个都要：错误码精确匹配 + 口语语义泛化 → RRF 融合

索引管理（双引擎一致性）：
- BM25 索引是**内存结构**（从 Chroma 全量加载文本构建倒排）
- 正常检索时对比 doc_count：新增/删除导致数量变化就自动重建；
  文档修改/软删除即使数量不变，也由 sync 写入边界主动淘汰缓存
  （不能只依赖数量，否则 BM25 会继续返回旧正文/旧 metadata）
- 10 万 chunk 构建秒级（分词 + 倒排），重建成本可忽略
"""
import jieba
from rank_bm25 import BM25Okapi

from app.rag.store import VectorStore

# 运维术语自定义词典：保证"证书续期/拨号/错误码"等不被误切
_TERMS = [
    "证书续期", "证书过期", "错误码", "拨号连接", "拨号失败", "客户端配置",
    "路由表", "防火墙", "VPN", "续期", "重排", "知识库", "重新拨号",
]
for _t in _TERMS:
    jieba.add_word(_t)


def tokenize(text: str) -> list[str]:
    """jieba 分词（BM25 的词法单元）。"""
    return [w for w in jieba.cut(text) if w.strip()]


class Bm25Index:
    """内存 BM25 索引：数量变化自动失效，sync 变更时显式淘汰。"""

    def __init__(self, store: VectorStore):
        self._store = store
        self._ids: list[str] = []
        self._texts: list[str] = []
        self._metas: dict[str, dict] = {}          # id → metadata（过滤条件用）
        self._bm25: BM25Okapi | None = None
        self._built_at_count = -1

    def _ensure_built(self) -> None:
        """懒加载 + 失效重建：库的 chunk 数变了就重建索引。"""
        count = self._store.count()
        if count == 0:
            self._bm25 = None      # 空库：无索引可建（避免除零）
            self._built_at_count = 0
            return
        if self._bm25 is not None and count == self._built_at_count:
            return
        # 全量加载（含 metadata，供过滤）：万级秒级，10 万级 ~秒级
        all_meta = self._store.get_all_meta()
        self._ids = list(all_meta.keys())
        self._texts = [v["text"] for v in all_meta.values()]
        self._metas = {cid: v["metadata"] for cid, v in all_meta.items()}
        self._bm25 = BM25Okapi([tokenize(t) for t in self._texts])
        self._built_at_count = count

    def search(self, query: str, top_k: int) -> list[tuple[str, str, float, dict]]:
        """检索 top_k 条，返回 [(chunk_id, text, score, metadata)]。"""
        self._ensure_built()
        if not self._bm25 or not self._ids:
            return []
        scores = self._bm25.get_scores(tokenize(query))
        ranked = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)
        return [(self._ids[i], self._texts[i], float(scores[i]), self._metas.get(self._ids[i], {}))
                for i in ranked[:top_k] if scores[i] > 0]
