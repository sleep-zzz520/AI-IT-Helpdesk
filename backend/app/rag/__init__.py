"""RAG 知识库子系统：文档加载 → 切分 → 嵌入 → 向量存储 → 同步 → 检索。

模块职责划分：
- chunks.py    数据结构（KnowledgeDoc / Chunk）
- loader.py   源文档扫描 + frontmatter 元数据解析
- splitter.py  Markdown 结构分块（父子块：小块检索、大块生成）
- embedder.py 智谱 Embedding-3 嵌入（批处理 + 重试）
- store.py    向量库抽象（ChromaDB 实现，工厂可切 Milvus）
- sync.py     reconcile 引擎（增/改/删/跳过 + 台账 + 幂等）
- retriever.py 统一检索入口（kb 节点调用，Phase 2 加混合检索）
"""
