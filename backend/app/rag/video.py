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
    """覆盖全片均匀抽样（约每 3 秒一个名额，上限 max_frames）。

    帧名包含真实时间戳，便于回到视频原位；超过预算时仍覆盖最后一帧。
    """
    max_frames = settings.VIDEO_MAX_FRAMES if max_frames is None else max_frames
    if max_frames <= 0:
        return []
    frames: list[ImageRef] = []
    try:
        # ponytail: 两遍解码换取可变帧率/缺失时长元数据下的全片覆盖，内存仅存 N 帧。
        # 大视频后续可用时间戳 seek 优化，不能再退化为仅取开头。
        with av.open(str(video_path)) as container:
            stream = container.streams.video[0]
            total = sum(1 for _ in container.decode(stream))
            fps = float(stream.average_rate or 30)
        if not total:
            return []
        count = min(max_frames, max(1, int((total - 1) / (fps * 3)) + 1))
        indices = ({0} if count == 1 else
                   {round(i * (total - 1) / (count - 1)) for i in range(count)})
        with av.open(str(video_path)) as container:
            stream = container.streams.video[0]
            for i, frame in enumerate(container.decode(stream)):
                if i not in indices:
                    continue
                buf = io.BytesIO()
                frame.to_image().save(buf, format="PNG")
                timestamp = float(frame.time) if frame.time is not None else i / fps
                frames.append(ImageRef(
                    name=f"frame-{len(frames) + 1}-at-{timestamp:.3f}s.png",
                    data=buf.getvalue(), page=0,
                ))
    except Exception:
        return []  # 部分解码失败不能冒充完整视频
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
    except Exception:
        return ""
    finally:
        wav_path.unlink(missing_ok=True)
