"""从 CRUD-RAG 80000_docs 语料切出规模压测用的 md 文档（分批）。

为什么分批嵌入（用户决策 + sync 增量设计天然支持）：
- 系统未完成时全量嵌入 8 万条 ≈ 24 元 + 数小时，一旦嵌入逻辑/元数据有问题
  要重来，浪费成本
- 先切 N 篇（默认 1000）验证检索性能/功能，后续用 --offset 续切剩余部分，
  再跑 sync 增量补全（幂等：只嵌新增，不重嵌）

用法：
    python -m scripts.prepare_scale_corpus --limit 1000            # 首批 1000 篇
    python -m scripts.prepare_scale_corpus --offset 1000 --limit 1000  # 续切下一批
输出：
    数据集/scale_corpus/part-{id:06d}.md   （frontmatter: scenario=scale）
"""
import argparse
import re
import shutil
from pathlib import Path

# 每条新闻以「日期时间，正文：」开头（首条带可选方括号 [ ]，后续条无方括号）
NEWS_RE = re.compile(
    r"(?:\n|^)\s*\[?\s*(\d{4}-\d{2}-\d{2}[^，\n]*?)\s*\]?\s*，正文："
)

# scripts/ → backend → 项目根
_ROOT = Path(__file__).resolve().parent.parent.parent
SOURCE_DIR = _ROOT / "数据集" / "CRUD_RAG-main" / "data" / "80000_docs"
OUT_DIR = _ROOT / "数据集" / "scale_corpus"


def extract_news(path: Path) -> list[tuple[str, str]]:
    """解析一个分片文件 → [(日期, 正文)]。"""
    text = path.read_text(encoding="utf-8", errors="ignore")
    matches = list(NEWS_RE.finditer(text))
    items: list[tuple[str, str]] = []
    for i, m in enumerate(matches):
        date = m.group(1).strip()
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        body = text[start:end].strip()
        if body:
            items.append((date, body))
    return items


def write_doc(out_path: Path, idx: int, date: str, body: str) -> None:
    """一篇新闻 → 带 frontmatter 的 md（scenario=scale，与运维知识库隔离）。"""
    frontmatter = f"""---
scenario: scale
doc_title: 语料 {idx}
error_codes: []
risk: low
action: ""
version: "1.0"
status: active
valid_to: "2099-12-31"
source_url: scale-corpus/{idx}
---

# {date} 新闻

{body}
"""
    out_path.write_text(frontmatter, encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="切分 80000_docs 语料为 scale 压测文档")
    parser.add_argument("--limit", type=int, default=1000, help="本次切分条数（默认 1000）")
    parser.add_argument("--offset", type=int, default=0, help="起始偏移（增量续切用）")
    parser.add_argument("--force", action="store_true", help="重建输出目录")
    args = parser.parse_args()

    if args.offset == 0 and args.force:
        shutil.rmtree(OUT_DIR, ignore_errors=True)
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    # 收集全部新闻（顺序遍历分片）
    all_news: list[tuple[str, str]] = []
    for part in sorted(SOURCE_DIR.iterdir()):
        if part.is_file() and not part.name.startswith("."):
            all_news.extend(extract_news(part))

    print(f"语料总量: {len(all_news)} 条，本次切 {args.offset}~{args.offset + args.limit}")

    written = 0
    for idx in range(args.offset, min(args.offset + args.limit, len(all_news))):
        date, body = all_news[idx]
        out = OUT_DIR / f"part-{idx:06d}.md"
        if out.exists():  # 已存在跳过（幂等，重复运行不覆盖）
            continue
        write_doc(out, idx, date, body)
        written += 1

    print(f"生成 {written} 篇新文档（已存在跳过），目录: {OUT_DIR}")


if __name__ == "__main__":
    main()
