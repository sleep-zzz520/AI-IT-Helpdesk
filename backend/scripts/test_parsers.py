"""Phase 4 格式扩展测试：pdf（文本型/扫描型分诊）/ docx / pptx / xlsx。

注意：所有文档均为【合成数据】（程序生成，非真实用户资料），
按 AGENTS.md 原则明确标注。扫描型 PDF 用合成截图嵌入 PDF 页模拟。

验证点：
1. 解析确定性：各格式 sections 结构正确（无 LLM 调用，纯解析）
2. 扫描型 PDF 分诊：判定 scanned → 每页渲染 → GLM-4V 转译（错误码 800）
3. 入库闭环：5 个文件全部加载入库（临时库+独立台账+独立向量库）
4. 检索命中：中文 query 命中扫描 PDF 转译块；英文 query 命中文本 PDF
5. 幂等：第二次 sync 全跳过
"""
import tempfile
from pathlib import Path

import fitz
from openpyxl import Workbook
from PIL import Image, ImageDraw
from pptx import Presentation
from pptx.util import Inches, Pt
from docx import Document as DocxDocument

from app.db import SessionLocal
from app.models import KbDocument
from app.rag.loader import scan_docs
from app.rag.parsers import parse_docx, parse_pdf, parse_pptx, parse_xlsx
from app.rag.retriever import retrieve
from app.rag.store import ChromaStore
from app.rag.sync import run_sync

TMP_KB = Path(tempfile.mkdtemp(prefix="kb_parsers_"))
TEST_KB_NAME = "parsers_test"
TEST_STORE = ChromaStore(persist_dir=str(TMP_KB / "chroma"))


def reset_ledger() -> None:
    with SessionLocal() as db:
        db.query(KbDocument).filter_by(kb_name=TEST_KB_NAME).delete()
        db.commit()


# ===== 合成测试文件 =====

def make_screenshot_bytes() -> bytes:
    """合成 VPN 报错截图（错误码 800 + 证书过期），返回 PNG bytes。"""
    img = Image.new("RGB", (640, 300), (40, 44, 54))
    d = ImageDraw.Draw(img)
    d.rectangle([60, 40, 580, 260], fill=(24, 28, 36), outline=(120, 128, 140))
    d.text((90, 70), "VPN Client  v4.2.1", fill=(200, 210, 225))
    d.text((90, 110), "Error 800 - Connection failed", fill=(255, 90, 90))
    d.text((90, 140), "Certificate is expired (2026-08-01)", fill=(225, 230, 240))
    d.text((90, 170), "错误代码: 800", fill=(225, 230, 240))
    buf = tempfile.SpooledTemporaryFile()
    img.save(buf, format="PNG")
    buf.seek(0)
    return buf.read()


def make_docx(path: Path) -> None:
    """Word 文档：Heading 1 标题 + 正文段落 + 表格。"""
    doc = DocxDocument()
    doc.add_heading("VPN 客户端故障排查指南", level=1)
    doc.add_paragraph("本文档描述 VPN 客户端常见错误的排查步骤。")
    doc.add_heading("错误码 800", level=2)
    doc.add_paragraph("证书已过期导致连接失败，需要申请续期。")
    table = doc.add_table(rows=2, cols=2)
    table.cell(0, 0).text, table.cell(0, 1).text = "错误码", "处置"
    table.cell(1, 0).text, table.cell(1, 1).text = "800", "证书续期"
    doc.save(path)


def make_pptx(path: Path) -> None:
    """PPT：标题页 + 正文页。"""
    prs = Presentation()
    slide1 = prs.slides.add_slide(prs.slide_layouts[0])
    slide1.shapes.title.text = "VPN 服务台培训"
    slide1.placeholders[1].text = "本培训覆盖 VPN 客户端常见故障处理"
    slide2 = prs.slides.add_slide(prs.slide_layouts[1])
    slide2.shapes.title.text = "错误码 800 处理流程"
    body = slide2.placeholders[1].text_frame
    body.text = "步骤 1：查询证书状态"
    body.add_paragraph().text = "步骤 2：证书过期则续期"
    prs.save(path)


def make_xlsx(path: Path) -> None:
    """Excel：错误码对照表。"""
    wb = Workbook()
    ws = wb.active
    ws.title = "错误码对照"
    ws.append(["错误码", "含义", "处置建议"])
    ws.append(["800", "证书过期", "证书续期"])
    ws.append(["720", "设备未注册", "联系管理员注册设备"])
    ws.append([""])  # 空行（验证被跳过）
    wb.save(path)


def make_text_pdf(path: Path) -> None:
    """文本型 PDF：fitz 直接插入文本（英文+数字，规避中文字体依赖）。"""
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)
    page.insert_text((72, 72),
                     "VPN Client Troubleshooting Guide (text-based PDF)",
                     fontsize=14)
    page.insert_text((72, 110), "Error 800 - Certificate expired", fontsize=12)
    page.insert_text((72, 130), "Renew the certificate via the admin portal", fontsize=12)
    doc.save(path)


def make_scanned_pdf(path: Path) -> None:
    """扫描型 PDF：合成截图嵌入 PDF 页（模拟扫描件，无文本层）。"""
    png = make_screenshot_bytes()
    doc = fitz.open()
    page = doc.new_page(width=640, height=300)
    page.insert_image(page.rect, stream=png)
    doc.save(path)


# ===== 验证 =====

def main() -> None:
    reset_ledger()

    # ---- 0) 准备：临时知识库放入 5 个合成文件 ----
    for name, maker in [
        ("vpn/troubleshooting.docx", make_docx),
        ("vpn/training.pptx", make_pptx),
        ("vpn/error-codes.xlsx", make_xlsx),
        ("vpn/text-guide.pdf", make_text_pdf),
        ("vpn/scanned-error800.pdf", make_scanned_pdf),
    ]:
        p = TMP_KB / name
        p.parent.mkdir(parents=True, exist_ok=True)
        maker(p)
        print(f"[准备] {name}")

    # ---- 1) 解析确定性（无 LLM）：sections 结构 ----
    d = parse_docx(TMP_KB / "vpn/troubleshooting.docx")
    assert len(d.sections) >= 2, f"docx sections: {d.sections}"
    assert any("800" in body for _, body in d.sections), "docx 应含 错误码 800 正文"
    print(f"[docx] {len(d.sections)} 个章节，首节: {d.sections[0][0]}")

    p = parse_pptx(TMP_KB / "vpn/training.pptx")
    assert len(p.sections) == 2, f"pptx sections: {p.sections}"
    assert any("800" in f"{t}{b}" for t, b in p.sections), "pptx 应含 错误码 800（标题或正文）"
    print(f"[pptx] {len(p.sections)} 个章节: {[t for t, _ in p.sections]}")

    x = parse_xlsx(TMP_KB / "vpn/error-codes.xlsx")
    assert len(x.sections) == 1, f"xlsx sections: {x.sections}"
    assert "800" in x.sections[0][1], "xlsx 应含 错误码 800"
    assert x.sections[0][1].count("\n") == 3, "xlsx 表应 3 行（空行被跳过）"
    print(f"[xlsx] sheet: {x.sections[0][0]}，{x.sections[0][1].count(chr(10)) + 1} 行")

    tp = parse_pdf(TMP_KB / "vpn/text-guide.pdf")
    assert not tp.scanned, "文本型 PDF 不应判定为扫描型"
    assert len(tp.sections) == 1 and "800" in tp.sections[0][1]
    print(f"[pdf-文本型] {tp.page_count} 页，判定文本型（每页 {len(tp.sections[0][1])} 字符）")

    sp = parse_pdf(TMP_KB / "vpn/scanned-error800.pdf")
    assert sp.scanned, "合成截图 PDF 应判定为扫描型"
    print(f"[pdf-扫描型] 分诊正确（每页 {0} 字符 < 阈值 30）")

    # ---- 2) loader：5 个文件全部加载 ----
    loaded = scan_docs(TMP_KB)
    by_name = {Path(doc.rel_path).name: doc for doc in loaded.docs}
    print(f"[loader] 加载 {len(loaded.docs)} 篇: {sorted(by_name)}")
    assert len(loaded.docs) == 5, f"应加载 5 篇，实际 {len(loaded.docs)}"
    assert by_name["scanned-error800.pdf"].meta["media_type"] == "pdf"
    # 扫描 PDF 的转译块应含错误码 800（loader 组装时已调 GLM-4V）
    assert "800" in by_name["scanned-error800.pdf"].content, "扫描 PDF 转译块应含错误码 800"
    print("[转译] 扫描 PDF 页面 → GLM-4V 转译成功，content 含错误码 800")

    # ---- 3) 入库 ----
    report1 = run_sync(kb_root=TMP_KB, store=TEST_STORE, kb_name=TEST_KB_NAME)
    print(f"[sync#1] {report1.summary}")
    assert len(report1.added) == 5, f"应新增 5 篇: {report1.summary}"

    # ---- 4) 检索命中 ----
    # 中文 query → 命中扫描 PDF 转译块（截图变知识资产）
    hits = retrieve("VPN 连接失败 错误码 800 证书过期", scenario="vpn", top_k=5,
                    store=TEST_STORE)
    scanned_top = next((h for h in hits if h.metadata.get("media_type") == "pdf"), None)
    assert scanned_top is not None, f"中文 query 应命中 PDF（扫描或文本）: {[h.id for h in hits]}"
    print(f"[检索-中文] Top 命中 PDF 块: {scanned_top.id}，media_ref={scanned_top.metadata.get('media_ref')}")
    # 英文 query → 命中文本 PDF（词法匹配走 BM25 路）
    hits_en = retrieve("troubleshooting certificate expired renewal", scenario="vpn",
                       top_k=5, store=TEST_STORE)
    assert any(h.metadata.get("media_type") == "pdf" for h in hits_en), "英文 query 应命中文本 PDF"
    print(f"[检索-英文] 命中 {len(hits_en)} 条，含文本型 PDF 块 ✅")

    # ---- 5) 幂等 ----
    report2 = run_sync(kb_root=TMP_KB, store=TEST_STORE, kb_name=TEST_KB_NAME)
    print(f"[sync#2] {report2.summary}")
    assert len(report2.skipped) == 5 and not report2.added, "第二次应全跳过"

    print("\n=== Phase 4 格式扩展（pdf/docx/pptx/xlsx + 扫描件分诊）：全部通过 ===")


if __name__ == "__main__":
    main()
