"""Markdown 结构分块 + 父子块策略。

为什么父子块（参考 All-in-RAG 教程 chapter8 实战验证）：
- 子块（## 章节）做检索 → 精确命中"错误码 800 的处置"这个具体段落
  （小 query 在整篇文档里占比小，直接拿整篇做向量检索会排名靠后甚至召回不到）
- 父块（整篇文档）做生成 → 回答时上下文完整
  （只给子块会丢了"风险说明""注意事项"，AI 会答得不完整）
- 这就是"小块检索、大块生成"：检索的精确性与生成的完整性兼得

子块粒度用二级标题（##）：三级（###）太碎会切断处理步骤，
一级（#）太粗退化成整篇。无 ## 的短文档整篇作为单个子块。
"""
import re
from dataclasses import dataclass

from app.rag.chunks import Chunk, KnowledgeDoc

# 匹配 Markdown 标题行（# 开头，后面跟空格再跟文字；## 就是 2 个 #）
_H2_RE = re.compile(r"^##\s+(.+)$")

# 引言阈值：一级标题到第一个二级标题之间的引言，够长才单独成块
# （太短的引言没检索价值，并入第一个子块更合理）
_INTRO_MIN_CHARS = 60


@dataclass
class SplitResult:
    """一篇文档的切分结果。"""
    doc_id: str
    parent: Chunk | None       # 父块（全文，生成用）
    children: list[Chunk]      # 子块（检索用）


def _to_int_date(value) -> int | None:
    """'2099-12-31' → 20991231（Chroma 的 $gte 只支持数值，日期必须转 int）。"""
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str) and len(value) >= 10:
        digits = value[:10].replace("-", "")
        return int(digits) if digits.isdigit() else None
    return None


def _make_meta(doc: KnowledgeDoc, **extra) -> dict:
    """把文档 frontmatter 复制成 chunk metadata（+ 运行时字段）。

    Chroma 的 metadata 值只支持 str/int/float/bool：
    - error_codes / tags 这类 list 要转成逗号分隔字符串（后续检索按包含匹配）
    - valid_to / valid_from 日期转 int（YYYYMMDD）——Chroma 的 $gte 只认数值
    """
    meta = dict(doc.meta)
    # list → 逗号分隔字符串（Chroma 不支持 list 值）
    for key in ("error_codes", "tags"):
        val = meta.get(key)
        if isinstance(val, (list, tuple)):
            meta[key] = ",".join(str(x) for x in val)
    # 日期 → int（YYYYMMDD），供 retriever 的 valid_to 过滤；转换失败删 key
    for key in ("valid_to", "valid_from"):
        val = meta.get(key)
        if val is not None:
            converted = _to_int_date(val)
            if converted is not None:
                meta[key] = converted
            else:
                meta.pop(key, None)
    meta.update(extra)
    return meta


def split_markdown(doc: KnowledgeDoc) -> SplitResult:
    """按 ## 标题切子块；父块 = 整篇正文。"""
    lines = doc.content.splitlines()

    # 找所有二级标题的位置
    h2_positions = [(i, m.group(1).strip()) for i, line in enumerate(lines)
                    if (m := _H2_RE.match(line))]

    parent_id = f"{doc.doc_id}:parent"
    parent = Chunk(
        id=parent_id,
        text=doc.content,
        metadata=_make_meta(doc, doc_id=doc.doc_id, doc_hash="",  # doc_hash 由 sync 注入
                            chunk_type="parent", parent_id="", seq=0),
    )

    children: list[Chunk] = []
    if not h2_positions:
        # 无二级标题：整篇作单子块（父块本身已含全文）
        children.append(Chunk(
            id=f"{doc.doc_id}:1",
            text=doc.content,
            metadata=_make_meta(doc, doc_id=doc.doc_id, doc_hash="",
                                chunk_type="child", parent_id=parent_id, seq=1),
        ))
        return SplitResult(doc.doc_id, parent, children)

    # 引言（# 标题后、第一个 ## 前）
    intro = "\n".join(lines[:h2_positions[0][0]]).strip()
    seq = 0
    if len(intro) >= _INTRO_MIN_CHARS:
        seq += 1
        children.append(Chunk(
            id=f"{doc.doc_id}:{seq}",
            text=intro,
            metadata=_make_meta(doc, doc_id=doc.doc_id, doc_hash="",
                                chunk_type="child", parent_id=parent_id, seq=seq),
        ))

    # 每个 ## 章节一个子块
    for idx, (start, _) in enumerate(h2_positions):
        end = h2_positions[idx + 1][0] if idx + 1 < len(h2_positions) else len(lines)
        section = "\n".join(lines[start:end]).strip()
        if not section:
            continue
        seq += 1
        children.append(Chunk(
            id=f"{doc.doc_id}:{seq}",
            text=section,
            metadata=_make_meta(doc, doc_id=doc.doc_id, doc_hash="",
                                chunk_type="child", parent_id=parent_id, seq=seq),
        ))

    return SplitResult(doc.doc_id, parent, children)


def split_all(docs: list[KnowledgeDoc]) -> list[SplitResult]:
    """批量切分（sync 引擎入口）。"""
    return [split_markdown(doc) for doc in docs]
