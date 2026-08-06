"""知识文档加载：扫描 docs/knowledge/ 下的 Markdown，解析 frontmatter 元数据。

frontmatter 格式（YAML，`---` 包裹）：
    ---
    scenario: vpn
    error_codes: ["800", "720"]
    risk: low
    action: vpn.renew_certificate
    version: "1.0"
    status: active
    valid_to: "2099-12-31"
    tags: [vpn, 证书]
    ---
    # 正文标题
    ## 章节

这套元数据会被 sync 复制进每个 chunk 的 metadata：
检索命中后，action/risk 直接驱动现有执行流程（风险分级、工具注册表不变）。
"""
import hashlib
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from app.rag.chunks import KnowledgeDoc


def doc_id_of(rel_path: str) -> str:
    """稳定 doc_id：相对路径哈希前 16 位（见 chunks.py 的设计说明）。"""
    return hashlib.sha256(rel_path.encode("utf-8")).hexdigest()[:16]


@dataclass
class LoadResult:
    """一次扫描的结果（供 sync 报告 / 调试）。"""
    docs: list[KnowledgeDoc] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)   # 非 .md 或 frontmatter 损坏的文件


def _parse_frontmatter(text: str) -> tuple[dict | None, str]:
    """解析 `---` 包裹的 YAML frontmatter。

    返回 (meta, body)；无 frontmatter 返回 (None, 原文)。
    """
    if not text.startswith("---"):
        return None, text
    parts = text.split("---", 2)
    if len(parts) < 3:
        return None, text
    try:
        meta = yaml.safe_load(parts[1]) or {}
        if not isinstance(meta, dict):
            return None, text
        return meta, parts[2].lstrip("\n")
    except yaml.YAMLError:
        return None, text  # frontmatter 损坏按无 meta 处理（sync 会跳过并报告）


def load_doc(path: Path, kb_root: Path) -> KnowledgeDoc | None:
    """加载单篇文档；frontmatter 缺失/损坏返回 None（sync 跳过并报告）。"""
    rel = path.resolve().relative_to(kb_root.resolve()).as_posix()
    text = path.read_text(encoding="utf-8")
    meta, body = _parse_frontmatter(text)
    if meta is None or "scenario" not in meta:
        return None
    return KnowledgeDoc(rel_path=rel, doc_id=doc_id_of(rel), meta=meta, content=body)


def scan_docs(kb_root: Path) -> LoadResult:
    """递归扫描 kb_root 下所有 .md 文档。"""
    result = LoadResult()
    for path in sorted(kb_root.rglob("*.md")):
        doc = load_doc(path, kb_root)
        if doc is None:
            result.skipped.append(path.as_posix())
        else:
            result.docs.append(doc)
    return result
