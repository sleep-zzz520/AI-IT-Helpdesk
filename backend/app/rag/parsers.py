"""多格式文档解析：pdf / docx / pptx / xlsx → 统一 ParsedDoc（Phase 4 格式扩展）。

设计思想（延续"转译降维"）：
- 所有格式最终都变成【sections（标题+正文）+ images（图片清单）】的统一结构
- sections → 伪 Markdown（复用 splitter 的 ## 结构切分）
- images（docx/pptx 内嵌图、扫描型 PDF 页面）→ GLM-4V 转译（复用 transcribe.py）
- 解析是确定性的（无 LLM 调用），只有图片转译会调模型（且有内容哈希缓存）

技术选型：
- pdfplumber：抽文本质量最好（表格/版面），文本型 PDF
- PyMuPDF(fitz)：渲染 PDF 页面成 PNG（纯 Python wheel，无 poppler 系统依赖），扫描型 PDF
- python-docx / python-pptx / openpyxl：各自格式的官方风格解析库
- docx/pptx 内嵌图片直接解 zip 读 media/ 目录：docx/pptx 本质是 zip 包，
  绕开库的私有 API，用 zipfile 标准库最稳
"""
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

import fitz  # PyMuPDF
import pdfplumber
import pptx
from docx import Document as DocxDocument
from docx.table import Table as DocxTable
from docx.text.paragraph import Paragraph as DocxParagraph
from openpyxl import load_workbook
from pptx.enum.shapes import MSO_SHAPE_TYPE

# 扫描型 PDF 分诊阈值：平均每页非空字符数低于此值 → 判定为扫描件（图片页）
# （文本型 PDF 每页至少几十上百字符；纯扫描页 pdfplumber 抽不出字）
_SCANNED_MIN_CHARS_PER_PAGE = 30


@dataclass
class ImageRef:
    """解析出的内嵌图片（docx/pptx 附件图 / 扫描 PDF 页面）。"""
    name: str            # 文件名（写进章节标题，如 "page-3.png"）
    data: bytes          # 图片二进制（PNG/JPEG）
    page: int = 0        # 原位锚点：所属页码（1-based；0 = 无锚点，组装时放文档末尾）


@dataclass
class ParsedDoc:
    """所有格式解析器的统一出口（loader 消费，生成伪 Markdown）。"""
    sections: list[tuple[str, str]] = field(default_factory=list)  # (章节标题, 正文)
    images: list[ImageRef] = field(default_factory=list)
    scanned: bool = False    # 扫描型 PDF 标记（loader 据此决定图片怎么转译）
    page_count: int = 0


# ===== 通用：内嵌图片提取（docx/pptx 都是 zip 包） =====

def _extract_zip_media(path: Path, media_prefix: str) -> list[ImageRef]:
    """从 zip 包（docx）的 media/ 目录提取全部图片。

    docx 的图片在 word/media/（zip 包内部路径）。
    返回按文件名排序的图片清单（无页码锚点 → 组装时放文档末尾；
    pptx 不走这里：shape 级提取才能标注页码锚点，见 parse_pptx）。
    """
    results: list[ImageRef] = []
    try:
        with zipfile.ZipFile(path) as zf:
            for name in sorted(zf.namelist()):
                if not name.startswith(media_prefix):
                    continue
                if name.lower().endswith((".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp")):
                    results.append(ImageRef(name=Path(name).name, data=zf.read(name)))
    except (zipfile.BadZipFile, KeyError, OSError):
        return []  # 包损坏：图片丢失不影响主文档入库
    return results


# ===== PDF =====

def parse_pdf(path: Path) -> ParsedDoc:
    """解析 PDF：分诊文本型/扫描型。

    文本型：每页正文一个 section（保留页边界，检索命中可定位页码）。
    扫描型：scanned=True 且 images 空——页面渲染由 render_pdf_pages 负责
    （把每页变成 ImageRef，统一走图片转译链路）。
    """
    doc = ParsedDoc()
    try:
        with pdfplumber.open(path) as pdf:
            doc.page_count = len(pdf.pages)
            page_texts: list[str] = []
            for page in pdf.pages:
                text = page.extract_text() or ""
                page_texts.append(text.strip())

        total_chars = sum(len(t) for t in page_texts)
        if doc.page_count == 0:
            return doc
        # 分诊：平均每页字符数低于阈值 → 扫描型（文字是图片，抽不出来）
        if total_chars / doc.page_count < _SCANNED_MIN_CHARS_PER_PAGE:
            doc.scanned = True
            return doc
        doc.sections = [(f"第 {i + 1} 页", t) for i, t in enumerate(page_texts) if t]
    except Exception:  # noqa: BLE001 —— PDF 损坏返回空 ParsedDoc（loader 跳过并报告）
        return doc
    return doc


def render_pdf_pages(path: Path) -> list[ImageRef]:
    """扫描型 PDF → 每页渲染成 PNG 图片（PyMuPDF，dpi=150 兼顾清晰/体积）。

    渲染结果与解析解耦：loader 拿到 ImageRef 后统一走 transcribe_image，
    缓存键 = 页面图片内容 hash（PDF 没改 → 页面图没变 → 命中缓存不重复调模型）。
    """
    pages: list[ImageRef] = []
    try:
        with fitz.open(path) as pdf:
            for i, page in enumerate(pdf, start=1):
                pix = page.get_pixmap(dpi=150)
                pages.append(ImageRef(name=f"page-{i}.png", data=pix.tobytes("png")))
    except Exception:  # noqa: BLE001
        return []
    return pages


# ===== DOCX =====

def _iter_docx_blocks(doc):
    """按文档顺序遍历段落和表格（python-docx 没有内置 API，这是官方标准配方）。

    为什么按顺序：Word 文档里正文和表格交替出现，顺序错乱会导致
    表格前后文语义断裂（表格内容被甩到文档末尾）。
    """
    from docx.oxml.ns import qn
    body = doc.element.body
    for child in body.iterchildren():
        if child.tag == qn("w:p"):
            yield DocxParagraph(child, doc)
        elif child.tag == qn("w:tbl"):
            yield DocxTable(child, doc)


def _table_to_markdown(table) -> str:
    """Word 表格 → Markdown 表格（| 分隔 + 分隔行）。"""
    rows = [[(cell.text or "").strip() for cell in row.cells] for row in table.rows]
    if not rows:
        return ""
    lines = ["| " + " | ".join(rows[0]) + " |"]
    lines.append("| " + " | ".join(["---"] * len(rows[0])) + " |")
    for row in rows[1:]:
        lines.append("| " + " | ".join(row) + " |")
    return "\n".join(lines)


def parse_docx(path: Path) -> ParsedDoc:
    """解析 Word 文档：标题层级映射 + 正文 + 表格 + 内嵌图片。

    标题映射：Word 的 Heading 1 → ##（章节，splitter 切分锚点），
    Heading 2/3 → ###/####（留在父块内不切，避免过度碎片化）。
    """
    doc = ParsedDoc()
    try:
        d = DocxDocument(str(path))
        section_title, section_body = "", []
        images: list[ImageRef] = []

        for block in _iter_docx_blocks(d):
            if isinstance(block, DocxTable):
                section_body.append(_table_to_markdown(block))
                continue
            text = block.text.strip()
            if not text:
                continue
            style = (block.style.name or "") if block.style else ""
            if style.startswith("Heading"):
                # 段落收尾：上一个章节完整时提交
                if section_title and section_body:
                    doc.sections.append((section_title, "\n".join(section_body)))
                section_title = text
                section_body = []
            elif style.startswith("Title"):
                if section_title and section_body:
                    doc.sections.append((section_title, "\n".join(section_body)))
                section_title = text  # Title 样式也当章节标题
                section_body = []
            else:
                section_body.append(text)
        if section_title and section_body:
            doc.sections.append((section_title, "\n".join(section_body)))
        if not doc.sections and not images:
            # 整篇无标题：退化为单章节（正文全文，splitter 会整篇作单子块）
            body = [b.text.strip() for b in _iter_docx_blocks(d)
                    if isinstance(b, DocxParagraph) and b.text.strip()]
            if body:
                doc.sections = [("文档正文", "\n".join(body))]

        doc.images = _extract_zip_media(path, "word/media/")
    except Exception:  # noqa: BLE001
        return doc
    return doc


# ===== PPTX =====

def parse_pptx(path: Path) -> ParsedDoc:
    """解析 PPT：每页一个 section（页标题 + 页内全部文本）。

    pptx 的文字藏在 shape 里（文本框/表格/组合），逐一展开收集。
    """
    doc = ParsedDoc()
    try:
        prs = pptx.Presentation(str(path))
        for i, slide in enumerate(prs.slides, start=1):
            title = (slide.shapes.title.text or "").strip() if slide.shapes.title else ""
            parts: list[str] = []
            for shape in slide.shapes:
                if shape == slide.shapes.title:
                    continue
                if shape.shape_type == MSO_SHAPE_TYPE.PICTURE:
                    # 图片标注页码锚点（原位转译：图插回所属页的 section 内）
                    doc.images.append(ImageRef(
                        name=f"page-{i}-{shape.name}.png",
                        data=shape.image.blob,
                        page=i,
                    ))
                    continue
                if getattr(shape, "has_text_frame", False) and shape.text_frame.text.strip():
                    parts.append(shape.text_frame.text.strip())
                if getattr(shape, "has_table", False):
                    rows = [[(c.text or "").strip() for c in row.cells]
                            for row in shape.table.rows]
                    if rows:
                        lines = ["| " + " | ".join(rows[0]) + " |",
                                 "| " + " | ".join(["---"] * len(rows[0])) + " |"]
                        lines += ["| " + " | ".join(r) + " |" for r in rows[1:]]
                        parts.append("\n".join(lines))
            heading = title or f"第 {i} 页"
            body = "\n".join(parts)
            if body:
                doc.sections.append((heading, body))
        doc.page_count = len(prs.slides)
    except Exception:  # noqa: BLE001
        return doc
    return doc


# ===== XLSX =====

def parse_xlsx(path: Path) -> ParsedDoc:
    """解析 Excel：每个 sheet 一个 section，转 Markdown 表。

    read_only=True：流式读取不把整表载入内存（大表友好）。
    空行/空列跳过：避免空白单元格把表撑成巨块。
    """
    doc = ParsedDoc()
    try:
        wb = load_workbook(path, read_only=True, data_only=True)
        for ws in wb.worksheets:
            rows: list[list[str]] = []
            for row in ws.iter_rows(values_only=True):
                vals = ["" if v is None else str(v).strip() for v in row]
                if any(vals):  # 整行全空跳过
                    rows.append(vals)
            if not rows:
                continue
            # 对齐列宽（短行补空）：Markdown 表要求每行列数一致
            width = max(len(r) for r in rows)
            rows = [r + [""] * (width - len(r)) for r in rows]
            lines = ["| " + " | ".join(rows[0]) + " |",
                     "| " + " | ".join(["---"] * width) + " |"]
            lines += ["| " + " | ".join(r) + " |" for r in rows[1:]]
            doc.sections.append((ws.title, "\n".join(lines)))
        wb.close()
        doc.page_count = len(doc.sections)
    except Exception:  # noqa: BLE001
        return doc
    return doc
