"""Phase 4 转译降维测试：合成截图 → GLM-4V 转译 → 入库 → 检索闭环。

注意：截图是【合成数据】（Pillow 画的模拟 VPN 报错窗口），
按 AGENTS.md 原则明确标注，不伪装成真实用户数据。

验证点：
1. 转译单测：GLM-4V 从截图中提取错误码 800 + 生成摘要
2. 入库闭环：图片 → loader 转译 → sync 入库（临时 kb 目录 + 独立台账）
3. 检索命中：自然语言 query 命中转译文档的 chunk（截图变知识资产）
4. 幂等 + 缓存：第二次 sync 全跳过，转译命中缓存不重复调 LLM
"""
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw

from app.db import SessionLocal
from app.models import KbDocument
from app.rag.loader import scan_docs
from app.rag.retriever import retrieve
from app.rag.store import ChromaStore
from app.rag.sync import run_sync
from app.rag.transcribe import transcribe_image

TMP_KB = Path(tempfile.mkdtemp(prefix="kb_transcribe_"))
TEST_KB_NAME = "transcribe_test"  # 独立台账名：避免误删真实知识库（见 sync.py 注释）
# 独立向量库：sync/retrieve 默认连生产库（.rag/kb_chroma），测试必须注入隔离存储
# （这是踩坑点：临时 kb_root 只隔离了源文档和台账，向量库不传 store 会混进生产库）
TEST_STORE = ChromaStore(persist_dir=str(TMP_KB / "chroma"))


def reset_ledger() -> None:
    """清掉本测试 kb_name 的旧台账（MySQL 持久化：上次运行留下的记录
    会让本次 sync 判"未变→跳过"，测试必须可重复运行）。"""
    with SessionLocal() as db:
        db.query(KbDocument).filter_by(kb_name=TEST_KB_NAME).delete()
        db.commit()


def make_screenshot(path: Path) -> None:
    """合成一张 VPN 报错截图（错误码 800 + 证书过期）。"""
    img = Image.new("RGB", (640, 300), (40, 44, 54))          # 深色桌面背景
    d = ImageDraw.Draw(img)
    d.rectangle([60, 40, 580, 260], fill=(24, 28, 36), outline=(120, 128, 140))  # 弹窗
    d.text((90, 70), "VPN Client  v4.2.1", fill=(200, 210, 225))
    d.text((90, 110), "Error 800 - Connection failed", fill=(255, 90, 90))
    d.text((90, 140), "Certificate is expired (2026-08-01)", fill=(225, 230, 240))
    d.text((90, 170), "错误代码: 800", fill=(225, 230, 240))
    d.rectangle([420, 210, 540, 240], fill=(70, 90, 140))      # 按钮
    d.text((440, 216), "确定", fill=(255, 255, 255))
    img.save(path, format="PNG")


def main() -> None:
    reset_ledger()  # 清旧台账，保证测试可重复运行（幂等验证除外）

    # ---- 0) 准备：临时知识库放一张合成截图 ----
    kb_root = TMP_KB / "vpn"
    kb_root.mkdir(parents=True)
    shot_path = kb_root / "screenshot-error-800.png"
    make_screenshot(shot_path)
    print(f"[准备] 合成截图: {shot_path}")

    # ---- 1) 转译单测 ----
    trans = transcribe_image(shot_path, media_ref=shot_path.name)
    assert trans is not None, "转译失败（GLM-4V 无输出）"
    print(f"[转译] 错误码: {trans.error_code!r}  (期望含 800)")
    print(f"[转译] 场景: {trans.scene}")
    print(f"[转译] 界面: {trans.ui_state}")
    print(f"[转译] 摘要: {trans.summary[:60]}...")
    assert "800" in trans.error_code, f"错误码提取失败: {trans.error_code!r}"
    assert trans.summary, "摘要为空"

    # ---- 2) loader 可见该图片文档 ----
    loaded = scan_docs(TMP_KB)
    assert len(loaded.docs) == 1, f"应加载 1 篇图片文档，实际 {len(loaded.docs)}"
    doc = loaded.docs[0]
    print(f"[loader] 文档: {doc.rel_path}")
    print(f"[loader] media_type={doc.meta.get('media_type')} media_ref={doc.meta.get('media_ref')}")
    assert doc.meta.get("media_type") == "image"
    assert doc.meta.get("scenario") == "vpn"  # 目录名推断

    # ---- 3) 入库（临时源目录 + 独立台账 + 独立向量库） ----
    report1 = run_sync(kb_root=TMP_KB, store=TEST_STORE, kb_name=TEST_KB_NAME)
    print(f"[sync#1] {report1.summary}")
    assert len(report1.added) == 1, f"应新增 1 篇，实际: {report1.summary}"

    # ---- 4) 检索命中：自然语言 query 命中转译块 ----
    hits = retrieve("VPN 连接失败 错误码 800 证书过期", scenario="vpn", top_k=3,
                    store=TEST_STORE)
    top = hits[0] if hits else None
    print(f"[检索] 命中 {len(hits)} 条，Top1: {top.id if top else None}")
    assert top is not None, "检索无命中"
    assert top.metadata.get("media_type") == "image", f"Top1 应是图片转译块: {top.id}"
    print(f"[检索] Top1 文本: {top.text[:80]}...")

    # ---- 5) 幂等 + 缓存：第二次 sync 全跳过 ----
    report2 = run_sync(kb_root=TMP_KB, store=TEST_STORE, kb_name=TEST_KB_NAME)
    print(f"[sync#2] {report2.summary}")
    assert len(report2.skipped) == 1 and not report2.added, "第二次应全跳过（幂等）"
    trans2 = transcribe_image(shot_path)
    print(f"[缓存] 第二次转译 from_cache={trans2.from_cache}")
    assert trans2.from_cache, "应命中转译缓存（图片未变）"

    print("\n=== Phase 4 转译降维闭环：全部通过 ===")


if __name__ == "__main__":
    main()
