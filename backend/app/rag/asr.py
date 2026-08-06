"""语音转写适配层（GLM-ASR，付费接口 ~0.02 元/分钟，Phase 4 音频/视频支持）。

设计要点：
1. 可插拔（ASR_MODE 配置）：
   - real（默认）：openai SDK 调智谱 /audio/transcriptions（模型 glm-asr-2512，
     实测 wav 支持、m4a 不支持；模型名 glm-4.7-asr 不存在）
   - mock：返回占位转写文本——离线开发/测试用，不烧钱
2. 格式统一：所有音频先经 PyAV 转码成 PCM wav 再调 ASR——
   m4a/aac/ogg 等容器由 PyAV 解码（自带 ffmpeg 库，零系统依赖），
   ASR 永远只见到 wav，格式支持与智谱 API 解耦
3. 失败返回空串不抛异常：sync 引擎有失败自愈（台账 hash 不更新，下次重试）
"""
from pathlib import Path

import av
from openai import OpenAI

from app.config import settings

ASR_MODEL = "glm-asr-2512"        # 实测确认的模型名（glm-4.7-asr 不存在）
_WAV_SAMPLE_RATE = 16000          # 转写用 16k 单声道（ASR 标准输入，体积小）

_client = OpenAI(api_key=settings.ZHIPU_API_KEY, base_url=settings.GLM_BASE_URL)


def to_wav(input_path: Path, out_path: Path, max_seconds: float | None = None) -> bool:
    """任意音频/视频文件 → 16k 单声道 PCM wav（PyAV 统一转码）。

    PyAV 解码输入的音视频流，重采样后编码 wav。任何容器格式都能进来，
    失败返回 False（不抛异常，调用方跳过该媒体）。
    max_seconds：解码时长截断（长视频只保留前 N 秒，ASR 计时收费的成本护栏）。
    """
    try:
        with av.open(str(input_path)) as container:
            stream = next((s for s in container.streams.audio if s.codec_context is not None),
                          container.streams.audio[0] if container.streams.audio else None)
            if stream is None:
                return False
            resampler = av.AudioResampler(
                format="s16", layout="mono", rate=_WAV_SAMPLE_RATE)
            with av.open(str(out_path), mode="w") as out:
                out_stream = out.add_stream("pcm_s16le", rate=_WAV_SAMPLE_RATE)
                out_stream.layout = "mono"
                for frame in container.decode(stream):
                    if max_seconds and frame.time is not None and frame.time >= max_seconds:
                        break  # 时长截断：达到上限即停止解码
                    for resampled in resampler.resample(frame):
                        for packet in out_stream.encode(resampled):
                            out.mux(packet)
                for packet in out_stream.encode(None):  # 冲刷编码器尾部
                    out.mux(packet)
        return out_path.exists() and out_path.stat().st_size > 44  # 至少有个 wav 头
    except Exception:  # noqa: BLE001 —— 解码失败不拖垮调用方
        return False


def transcribe_audio(audio_path: Path) -> str:
    """音频文件 → 转写文本；失败返回空串（绝不抛异常，sync 会重试）。

    ASR_MODE=mock 时返回占位文本（离线测试/演示，不调用付费接口）。
    """
    if settings.ASR_MODE != "real":
        return f"【模拟转写】{audio_path.name}：VPN 连接失败，错误代码 800，证书已过期。"
    try:
        # 统一转 wav（PyAV 解码任意容器 → PCM wav），再调 ASR
        import tempfile
        wav_path = Path(tempfile.mktemp(suffix=".wav"))
        try:
            if not to_wav(audio_path, wav_path):
                return ""
            with wav_path.open("rb") as f:
                resp = _client.audio.transcriptions.create(model=ASR_MODEL, file=f)
            return (resp.text or "").strip()
        finally:
            wav_path.unlink(missing_ok=True)
    except Exception:  # noqa: BLE001 —— 转写失败不拖垮流程
        return ""


def transcribe_media_text(media_path: Path) -> str:
    """媒体文件 → 可检索文本（音频直接转写；视频经 loader 抽音轨后调用本函数）。"""
    return transcribe_audio(media_path)
