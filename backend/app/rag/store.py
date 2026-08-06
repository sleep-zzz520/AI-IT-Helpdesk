"""向量库抽象层：VectorStore 协议 + ChromaDB 实现。

为什么抽象：规模演进到百万级时切 Milvus（生产级），切换只改工厂函数
create_store()，检索/同步/切分代码零改动。ChromaDB 官方定位
"原型到中等规模生产"——万级~十万级 chunk 单机毫秒级，本项目够用。

Chroma 实现要点：
- PersistentClient：本地文件持久化（Docker 挂卷即可落盘）
- metadata 值只支持 str/int/float/bool（list 已在 splitter 转成逗号字符串）
- where 过滤是 AND 语义：{"scenario": "vpn", "status": "active"}
- query 返回的是"距离"（越小越相似），对外统一转成 score（越大越相关）
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

import chromadb

from app.config import settings
from app.rag.chunks import Chunk


@dataclass
class Hit:
    """一次检索命中的结果（对外统一结构）。"""
    id: str
    text: str
    metadata: dict = field(default_factory=dict)
    score: float = 0.0          # 0~1，越大越相关（由距离转换）
    parent_text: str = ""       # 若命中子块，附带父块全文（生成用）


class VectorStore(ABC):
    """向量存取协议：sync 写入、retriever 查询都只认这 5 个方法。"""

    @abstractmethod
    def upsert_chunks(self, chunks: list[Chunk], vectors: list[list[float]]) -> None:
        """批量写入/覆盖 chunks（同 id 重复写入 = 覆盖，幂等）。"""

    @abstractmethod
    def delete_by_doc_id(self, doc_id: str) -> int:
        """删除一篇文档的全部 chunk（定点修正/删除用）。返回删除条数。"""

    @abstractmethod
    def get_doc_ids(self, doc_id: str) -> list[str]:
        """按 doc_id 取全部 chunk id（目标库存在性判断/幂等补写用）。"""

    @abstractmethod
    def query(self, vector: list[float], top_k: int,
              where: dict | None = None) -> list[Hit]:
        """按向量检索 top_k 条；where 是 metadata 过滤（AND 语义）。"""

    @abstractmethod
    def get_by_ids(self, ids: list[str]) -> dict[str, str]:
        """按 id 批量取文本（retriever 取父块全文用）。"""

    @abstractmethod
    def get_all(self) -> dict[str, str]:
        """全量取 id→文本（BM25 索引构建用；万级~十万级内存可承受）。"""

    @abstractmethod
    def get_all_meta(self) -> dict[str, dict]:
        """全量取 id→{text, metadata}（BM25 索引 + 过滤条件构建用）。"""

    @abstractmethod
    def count(self) -> int:
        """库内 chunk 总数（压测/健康检查用）。"""

    @abstractmethod
    def clear(self) -> None:
        """清空全库（重建/测试用）。"""


class ChromaStore(VectorStore):
    """ChromaDB 实现：本地文件持久化 + HNSW 索引。"""

    def __init__(self, persist_dir: str, collection_name: str = "kb_docs"):
        self._client = chromadb.PersistentClient(path=persist_dir)
        self._col = self._client.get_or_create_collection(
            name=collection_name,
            metadata={"hnsw:space": "cosine"},   # 余弦相似度空间（检索默认）
        )

    def upsert_chunks(self, chunks: list[Chunk], vectors: list[list[float]]) -> None:
        if not chunks:
            return
        self._col.upsert(
            ids=[c.id for c in chunks],
            documents=[c.text for c in chunks],
            metadatas=[c.metadata for c in chunks],
            embeddings=vectors,
        )

    def delete_by_doc_id(self, doc_id: str) -> int:
        ids = self.get_doc_ids(doc_id)
        if ids:
            self._col.delete(ids=ids)
        return len(ids)

    def get_doc_ids(self, doc_id: str) -> list[str]:
        # where 只支持等于匹配：按 doc_id 查（幂等补写/删除共用）
        got = self._col.get(where={"doc_id": doc_id})
        return got.get("ids") or []

    def query(self, vector: list[float], top_k: int,
              where: dict | None = None) -> list[Hit]:
        res = self._col.query(
            query_embeddings=[vector],
            n_results=top_k,
            where=where,
        )
        hits: list[Hit] = []
        ids = res.get("ids", [[]])[0]
        for i, cid in enumerate(ids):
            # 距离 → 相关度：cosine 距离 ∈ [0,2]，score = 1 - distance
            distance = (res.get("distances", [[]])[0] or [0])[i]
            metas = res.get("metadatas", [[]])[0]
            docs = res.get("documents", [[]])[0]
            meta = (metas[i] if metas else {}) or {}
            hits.append(Hit(
                id=cid,
                text=docs[i] if docs else "",
                metadata=meta,
                score=round(1.0 - distance, 4),
            ))
        return hits

    def get_by_ids(self, ids: list[str]) -> dict[str, str]:
        if not ids:
            return {}
        got = self._col.get(ids=ids)
        id_list = got.get("ids") or []
        doc_list = got.get("documents") or []
        return dict(zip(id_list, doc_list))

    def get_all(self) -> dict[str, str]:
        got = self._col.get()
        id_list = got.get("ids") or []
        doc_list = got.get("documents") or []
        return dict(zip(id_list, doc_list))

    def get_all_meta(self) -> dict[str, dict]:
        got = self._col.get()
        id_list = got.get("ids") or []
        doc_list = got.get("documents") or []
        meta_list = got.get("metadatas") or []
        return {
            cid: {"text": doc_list[i] if doc_list else "",
                  "metadata": (meta_list[i] if meta_list else {}) or {}}
            for i, cid in enumerate(id_list)
        }

    def count(self) -> int:
        return self._col.count()

    def clear(self) -> None:
        self._client.delete_collection(self._col.name)
        self._col = self._client.get_or_create_collection(
            name=self._col.name, metadata={"hnsw:space": "cosine"})


def create_store(collection_name: str | None = None) -> VectorStore:
    """工厂：按配置返回实现（未来 MILVUS_ENABLED=true 时换 MilvusStore）。

    sync / retriever 只 import 这个工厂，不直接依赖 ChromaStore。

    collection_name：
    - 不传 → 当前 active collection（检索/查询默认打"在岗"库）
    - 传候选 collection（蓝绿切换）→ sync 写入目标，线上零感知
    """
    return ChromaStore(persist_dir=settings.KB_VECTOR_DIR,
                       collection_name=collection_name or settings.active_collection)
