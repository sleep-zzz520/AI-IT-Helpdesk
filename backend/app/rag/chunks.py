"""RAG 数据结构：KnowledgeDoc（源文档）+ Chunk（切分单元）。

设计要点（为什么 doc_id 用路径哈希）：
- 用文件名 → 改名/移动后 ID 变，旧 chunks 成孤儿（参考文档避坑①）
- 用内容哈希 → 编辑后 ID 变，无法"按 doc_id 删除旧 chunks"，定点修正失效
- 用相对路径哈希 → 路径稳定则 ID 稳定，编辑内容不影响 ID（可定点修正）；
  改名 = 旧路径消失（触发删除）+ 新路径出现（触发新增），语义正确。
  且幂等可复现：同路径永远同 doc_id。
"""
from dataclasses import dataclass, field


@dataclass
class KnowledgeDoc:
    """一篇源文档（docs/knowledge/**/*.md）。"""
    rel_path: str            # 相对 kb_root 的路径（文档的唯一身份）
    doc_id: str              # sha256(rel_path)[:16]（稳定，不随内容变化）
    meta: dict               # frontmatter 元数据（scenario/risk/action/...）
    content: str             # 正文（不含 frontmatter）


@dataclass
class Chunk:
    """向量库里的一个检索单元。"""

    id: str                  # f"{doc_id}:parent" 或 f"{doc_id}:{seq}"
    text: str                # 块文本
    metadata: dict = field(default_factory=dict)
    # metadata 关键字段（sync 时统一注入）：
    # doc_id / doc_hash / chunk_type(parent|child) / parent_id / scenario /
    # error_codes(str) / risk / action / version / status / valid_to /
    # source_url / tags(str) / media_type / seq
    # media_type（Phase 4 多模态）：text（.md 默认）/ image / audio / video / pdf
    # media_ref：原始媒体文件相对路径——回答阶段可路由给 GLM-4V 二次看图
    # （联合嵌入升级项 bge-visualized-m3/ColPali 也靠 media_ref 找到原图）
