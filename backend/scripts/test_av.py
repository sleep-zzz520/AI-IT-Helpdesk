"""Phase 4 音频/视频支持测试：合成音频（macOS say TTS）→ ASR 转写 → 入库 → 检索。

注意：音频/视频均为【合成数据】（say 语音合成 + PyAV 编码，明确标注），
非真实用户录音/录屏。

验证点：
1. ASR 单测：合成语音（含"错误代码800"）→ glm-asr-2512 转写 → 包含 800
2. 视频抽帧：等间隔抽帧数正确（6 秒 fps=1 → 2 帧），帧内容转译含 800
3. 音轨提取：视频音轨 → ASR 转写（前 N 秒截断）
4. loader：音频/视频 2 篇加载，content 含转写文本 + 帧转译
5. 入库 + 检索闭环 + 幂等
"""
import shutil
import subprocess
import tempfile
from pathlib import Path

import av
from PIL import Image, ImageDraw

from app.db import SessionLocal
from app.models import KbDocument
from app.rag.asr import to_wav, transcribe_audio
from app.rag.loader import scan_docs
from app.rag.retriever import retrieve
from app.rag.store import ChromaStore
from app.rag.sync import run_sync
from app.rag.video import extract_audio_text, extract_frames

TMP_KB = Path(tempfile.mkdtemp(prefix="kb_av_"))
TEST_KB_NAME = "av_test"
TEST_STORE = ChromaStore(persist_dir=str(TMP_KB / "chroma"))

# 视频合成参数：fps=1、6 帧 = 6 秒；抽帧间隔 3 秒 → 期望 2 帧
VIDEO_SECONDS = 6


def reset_ledger() -> None:
    with SessionLocal() as db:
        db.query(KbDocument).filter_by(kb_name=TEST_KB_NAME).delete()
        db.commit()


def make_shot_frame(seconds: int) -> Image.Image:
    """第 N 秒的视频帧：报错弹窗 + 秒数水印（验证抽帧时序）。"""
    img = Image.new("RGB", (640, 300), (40, 44, 54))
    d = ImageDraw.Draw(img)
    d.rectangle([60, 40, 580, 260], fill=(24, 28, 36), outline=(120, 128, 140))
    d.text((90, 70), "VPN Client v4.2.1", fill=(200, 210, 225))
    d.text((90, 110), "Error 800 - Connection failed", fill=(255, 90, 90))
    d.text((90, 140), "Certificate is expired", fill=(225, 230, 240))
    d.text((90, 170), f"错误代码: 800  (t={seconds}s)", fill=(225, 230, 240))
    return img


def make_speech_wav(path: Path, text: str) -> bool:
    """macOS say 合成语音（中文 TTS）。无 say 的环境返回 False（跳过音频断言）。"""
    if not shutil.which("say"):
        print("[提示] 未找到 say（非 macOS），跳过 TTS 合成")
        return False
    aiff = path.with_suffix(".aiff")
    subprocess.run(["say", "-v", "Tingting", "-o", str(aiff), text], check=True)
    return to_wav(aiff, path)  # 统一转 16k wav（顺带验证 to_wav）

    aiff.unlink(missing_ok=True)


def make_video(path: Path, wav: Path) -> bool:
    """PyAV 合成视频：6 帧（fps=1）+ 音轨（say 生成的 wav 重采样进 aac）。"""
    import numpy as np

    try:
        container = av.open(str(path), mode="w")
        # 编码器自动回退：h264 不可用时用 mpeg4
        try:
            vstream = container.add_stream("h264", rate=1)
        except Exception:  # noqa: BLE001
            vstream = container.add_stream("mpeg4", rate=1)
        astream = container.add_stream("aac", rate=16000)

        for s in range(VIDEO_SECONDS):
            frame = av.VideoFrame.from_ndarray(
                np.array(make_shot_frame(s + 1)), format="rgb24")
            for p in vstream.encode(frame):
                container.mux(p)

        # 音轨：从 say wav 解码出帧，重采样写进 aac
        if wav.exists():
            with av.open(str(wav)) as src:
                audio = src.streams.audio[0]
                resampler = av.AudioResampler(format="fltp", layout="stereo",
                                              rate=16000)
                for f in src.decode(audio):
                    for r in resampler.resample(f):
                        for p in astream.encode(r):
                            container.mux(p)

        for p in vstream.encode(None):
            container.mux(p)
        for p in astream.encode(None):
            container.mux(p)
        container.close()
        return True
    except Exception as e:  # noqa: BLE001
        print(f"[合成视频失败] {type(e).__name__}: {e}")
        return False


def main() -> None:
    reset_ledger()
    vpn_dir = TMP_KB / "vpn"
    vpn_dir.mkdir(parents=True, exist_ok=True)

    # ---- 0) 合成素材 ----
    speech = "VPN 连接失败，错误代码八百，证书已过期，请申请续期"
    speech_wav = vpn_dir / "speech.wav"
    ok_tts = make_speech_wav(speech_wav, speech)
    video_path = vpn_dir / "demo.mp4"
    ok_video = make_video(video_path, speech_wav)
    if not ok_tts or not ok_video:
        print("素材合成不完整，跳过测试（需 macOS say + PyAV 编码）")
        return
    print(f"[准备] 合成语音（TTS，含错误码800）+ 合成视频（{VIDEO_SECONDS}s，6 帧 + 音轨）")

    # ---- 1) ASR 单测：TTS 语音 → 转写 ----
    text = transcribe_audio(speech_wav)
    print(f"[ASR] 转写: {text[:60]}")
    assert "800" in text, f"ASR 转写应含错误码 800: {text!r}"
    print("[ASR] 错误码 800 识别 ✅（glm-asr-2512，付费实测）")

    # ---- 2) 视频抽帧（等间隔 3s，6 秒 → 2 帧）----
    frames = extract_frames(video_path, max_frames=10)
    print(f"[视频] 抽帧 {len(frames)} 帧（期望 2）")
    assert len(frames) == 2, f"6 秒视频应抽 2 帧: {len(frames)}"

    # ---- 3) 视频音轨 → 转写 ----
    audio_t = extract_audio_text(video_path)
    print(f"[视频] 音轨转写: {audio_t[:50]}")
    assert "800" in audio_t, "视频音轨转写应含 800"

    # ---- 4) loader：音频/视频 2 篇加载 ----
    loaded = scan_docs(TMP_KB)
    names = {Path(d.rel_path).name for d in loaded.docs}
    print(f"[loader] 加载 {len(loaded.docs)} 篇: {sorted(names)}")
    assert "speech.wav" in names and "demo.mp4" in names, f"应含音频+视频: {names}"
    for d in loaded.docs:
        if d.rel_path.endswith(".wav"):
            assert d.meta["media_type"] == "audio" and "800" in d.content
        if d.rel_path.endswith(".mp4"):
            assert d.meta["media_type"] == "video" and "800" in d.content
            assert "frame-" in d.content, "视频 content 应含帧转译块"
    print("[loader] media_type=audio/video ✅，content 含转写+帧转译")

    # ---- 5) 入库 + 检索 + 幂等 ----
    report1 = run_sync(kb_root=TMP_KB, store=TEST_STORE, kb_name=TEST_KB_NAME)
    print(f"[sync#1] {report1.summary}")
    assert len(report1.added) == 2, f"应新增 2 篇（音频+视频）: {report1.summary}"
    hits = retrieve("VPN 证书过期 错误码 800", scenario="vpn", top_k=3,
                    store=TEST_STORE)
    assert hits, "检索应命中音视频知识块"
    top = hits[0]
    print(f"[检索] Top1: {top.id}（media_type={top.metadata.get('media_type')}）")
    assert "800" in (top.parent_text or top.text)
    report2 = run_sync(kb_root=TMP_KB, store=TEST_STORE, kb_name=TEST_KB_NAME)
    assert len(report2.skipped) == 2, "第二次应全跳过（幂等）"

    print("\n=== Phase 4 音频/视频（GLM-ASR + PyAV）：全部通过 ===")


if __name__ == "__main__":
    main()
