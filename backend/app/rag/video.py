"""视频处理：PyAV（ffmpeg Python 绑定）抽帧 + 音轨提取（Phase 4 视频支持）。

为什么用 PyAV 而不是系统 ffmpeg CLI：
- PyAV 是纯 Python wheel，自带 ffmpeg 库——零系统依赖，开发/Docker 一致
- 能力覆盖：视频解码抽帧 + 音频解码重采样，统一一套 API
- 避免 subprocess 调外部二进制（版本漂移、Docker 里没装就崩）

成本护栏（付费接口时代的关键设计）：
- 抽帧上限 VIDEO_MAX_FRAMES：每帧一次 GLM-4V 转译（免费但限流），
  长视频按间隔均匀抽样，最多 N 帧
- 音轨时长上限 VIDEO_MAX_AUDIO_SECONDS：ASR 计时收费，知识片段前 N 秒足够
"""
import io
import tempfile
from pathlib import Path

import av

from app.config import settings
from app.rag.parsers import ImageRef


def extract_frames(video_path: Path, max_frames: int | None = None) -> list[ImageRef]:
    """等间隔抽帧（每 3 秒一帧，上限 max_frames，默认配置值）。

    抽帧间隔固定 3 秒而非按视频长度换算：知识视频通常十几秒~几分钟，
    3 秒一帧能覆盖画面变化；超长视频由 max_frames 兜底截断。
    返回 ImageRef 列表（loader 统一走图片转译链路，media_ref 带 #frame-N 定位）。
    """
    max_frames = max_frames or settings.VIDEO_MAX_FRAMES
    frames: list[ImageRef] = []
    try:
        with av.open(str(video_path)) as container:
            stream = container.streams.video[0]
            fps = float(stream.average_rate or 30)
            step = max(1, int(fps * 3))  # 3 秒 = 3*fps 帧取 1
            for i, frame in enumerate(container.decode(stream)):
                if len(frames) >= max_frames:
                    break
                if i % step != 0:
                    continue
                buf = io.BytesIO()
                frame.to_image().save(buf, format="PNG")
                frames.append(ImageRef(
                    name=f"frame-{len(frames) + 1}.png",
                    data=buf.getvalue(),
                    page=0,  # 视频帧统一放文档末尾（帧间无章节语义）
                ))
    except Exception:  # noqa: BLE001 —— 解码失败返回已抽帧（部分可用）
        return frames
    return frames


def extract_audio_text(video_path: Path) -> str:
    """视频音轨 → 转写文本（截取前 VIDEO_MAX_AUDIO_SECONDS 秒，ASR 计时收费）。

    to_wav(max_seconds=...) 控制"解码多少秒"（时长截断），
    asr.transcribe_audio 负责转写——职责分离，音频文件走同一函数（不限时长）。
    """
    from app.rag.asr import to_wav, transcribe_audio

    wav_path = Path(tempfile.mktemp(suffix="_video.wav"))
    try:
        if not to_wav(video_path, wav_path,
                      max_seconds=settings.VIDEO_MAX_AUDIO_SECONDS):
            return ""
        return transcribe_audio(wav_path)
    except Exception:  # noqa: BLE001 —— 转写失败返回空（sync 重试兜底）
        return ""
    finally:
        wav_path.unlink(missing_ok=True)
