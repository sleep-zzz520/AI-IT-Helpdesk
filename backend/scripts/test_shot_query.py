"""Phase 4 查询侧测试：报障截图 → 转译增强 → 命中历史同类截图知识块。

注意：截图是【合成数据】（Pillow 画，明确标注非真实数据）。

验证点：
1. 缓存共享：同一张图，文件路径转译与 data_url 转译共享缓存（image_hash 一致）
2. query 增强：_enrich_query_with_image 拼入 摘要/错误码/关键文字（双通道）
3. 检索对比：用户纯文本（无错误码）查不到历史截图块；截图转译增强后命中
   ——「截图变知识资产」的直接证据
4. 节点级：answer_question 检索用增强词，judge/生成用原始问题（hops 可观测）
"""
import base64
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw

from app.db import SessionLocal
from app.models import KbDocument
from app.rag.retriever import retrieve
from app.rag.store import ChromaStore
from app.rag.sync import run_sync
from app.rag.transcribe import transcribe_data_url, transcribe_image

TMP_KB = Path(tempfile.mkdtemp(prefix="kb_shotquery_"))
TEST_KB_NAME = "shot_query_test"
TEST_STORE = ChromaStore(persist_dir=str(TMP_KB / "chroma"))


def reset_ledger() -> None:
    with SessionLocal() as db:
        db.query(KbDocument).filter_by(kb_name=TEST_KB_NAME).delete()
        db.commit()


def make_shot(path: Path, title: str = "VPN Client v4.2.1",
              code: str = "800") -> None:
    """合成报错截图（可调标题/错误码，模拟"历史截图 vs 用户新截图"）。"""
    img = Image.new("RGB", (640, 300), (40, 44, 54))
    d = ImageDraw.Draw(img)
    d.rectangle([60, 40, 580, 260], fill=(24, 28, 36), outline=(120, 128, 140))
    d.text((90, 70), title, fill=(200, 210, 225))
    d.text((90, 110), f"Error {code} - Connection failed", fill=(255, 90, 90))
    d.text((90, 140), "Certificate is expired", fill=(225, 230, 240))
    d.text((90, 170), f"错误代码: {code}", fill=(225, 230, 240))
    img.save(path, format="PNG")


def main() -> None:
    reset_ledger()

    # ---- 0) 准备：历史截图知识块（800）+ 干扰文档（无 800，让对比有区分度） ----
    vpn_dir = TMP_KB / "vpn"
    vpn_dir.mkdir(parents=True, exist_ok=True)
    make_shot(vpn_dir / "history-800.png", title="VPN Client v3.9.0")
    # 干扰文档：同类故障但不同错误码（720/553）——纯文本 query 下排名与 800 块竞争，
    # 增强后错误码 800 词面命中（BM25 路）确定性胜出 → 对比才有区分度
    (vpn_dir / "error-720.md").write_text(
        "---\nscenario: vpn\nstatus: active\nvalid_to: \"2099-12-31\"\n---\n"
        "\n# 错误码 720 排查\n\n## 设备未注册\n\n"
        "VPN 客户端报错 720，说明设备未注册，需联系管理员添加设备。", encoding="utf-8")
    (vpn_dir / "error-553.md").write_text(
        "---\nscenario: vpn\nstatus: active\nvalid_to: \"2099-12-31\"\n---\n"
        "\n# 错误码 553 排查\n\n## 证书吊销\n\n"
        "VPN 客户端报错 553，说明证书已被吊销，需要重新申请证书。", encoding="utf-8")
    report = run_sync(kb_root=TMP_KB, store=TEST_STORE, kb_name=TEST_KB_NAME)
    assert len(report.added) == 3, f"3 篇应入库: {report.summary}"
    print("[准备] 历史截图块 + 2 篇干扰文档已入库")

    # 用户新截图：同一错误码但窗口布局不同（v4.2.1）→ data URL（前端消息协议）
    user_shot = vpn_dir / "user-new.png"
    make_shot(user_shot, title="VPN Client v4.2.1")
    data_url = "data:image/png;base64," + base64.b64encode(user_shot.read_bytes()).decode()

    # ---- 1) 缓存共享：文件路径 vs data_url（同一内容 → 同一缓存） ----
    t_file = transcribe_image(user_shot, media_ref="vpn/user-new.png")
    t_url = transcribe_data_url(data_url, media_ref="user-screenshot")
    assert t_file is not None and t_url is not None
    assert t_file.image_hash == t_url.image_hash, "同图两入口应共享缓存键"
    assert "800" in t_file.error_code, f"转译错误码: {t_file.error_code!r}"
    assert t_url.from_cache, "data_url 第二次转译应命中文件入口的缓存"
    print(f"[缓存] 文件/data_url 共享缓存（hash={t_file.image_hash}），第二次 from_cache=True")

    # ---- 2) query 增强（双通道：摘要→向量路，错误码/关键文字→BM25 路） ----
    from app.agents.nodes.rag_query import _enrich_query_with_image
    plain = "我的电脑出问题了"
    enriched = _enrich_query_with_image(plain, data_url)
    assert enriched != plain and "错误码" in enriched and "800" in enriched
    assert "截图转译" in enriched, f"增强 query 应含转译摘要: {enriched[:80]}"
    print(f"[增强] query: {plain} → {enriched[:60]}...")

    # ---- 3) 检索对比：历史截图块（media_type=image）排名是否因增强而提升 ----
    def _rank_of_image_block(hits: list) -> int | None:
        """历史截图知识块的排名（1-based）；未命中返回 None。"""
        for i, h in enumerate(hits, start=1):
            if h.metadata.get("media_type") == "image":
                return i
        return None

    hits_before = retrieve(plain, scenario="vpn", top_k=5, store=TEST_STORE)
    hits_after = retrieve(enriched, scenario="vpn", top_k=5, store=TEST_STORE)
    rank_before, rank_after = _rank_of_image_block(hits_before), _rank_of_image_block(hits_after)
    print(f"[检索] 历史截图块排名：纯文本={rank_before} → 转译增强={rank_after}")
    assert rank_after is not None, "增强 query 必须命中历史截图知识块（截图变知识资产）"
    # 纯文本（无数字 token）下 720/553/800 三块故障文档语义相近，排名竞争（可能是 1 或 2）；
    # 增强后错误码 800 词面命中（BM25 路）→ 确定性 top1
    assert rank_after == 1, f"增强后截图块应确定性 top1（BM25 精确命中）: {rank_after}"
    assert rank_before is None or rank_before > 1, \
        f"纯文本下 800 块不应稳居 top1（需截图转译救场）: {rank_before}"

    # ---- 4) 节点级：search_query 与 question 分离 ----
    from app.agents.nodes.rag_query import answer_question
    result = answer_question(plain, scenario="vpn", search_query=enriched)
    assert result["hops"], "应有检索跳数记录"
    first_hop_query = result["hops"][0]["query"]
    assert first_hop_query == enriched, f"第 1 跳检索词应为增强词: {first_hop_query[:40]}"
    assert result["evidence"], "应检索到证据"
    print(f"[节点] 检索词=增强词（hops 可观测），回答基于 {len(result['evidence'])} 条证据")

    print("\n=== Phase 4 查询侧（截图转译拼进 query）：全部通过 ===")


if __name__ == "__main__":
    main()
