"""统一日志配置（ROADMAP P3.1 日志系统）。

背景：早期各模块用 print() 打诊断日志（llm failover / rerank 降级 / API 异常），
无级别、无文件落盘、生产排障不可查。统一后：

1. 运行期诊断日志 → logging（有级别 + 结构化 + 写文件 + 轮转）
2. 控制台实时可见（开发期）+ 文件按天轮转（生产排障）
3. 级别可配（LOG_LEVEL）：DEBUG 看全量、INFO 默认、生产可调 WARNING 减噪

格式（结构化，方便 grep/采集）：
    2026-08-07 10:00:00.123 | INFO  | app.llm         | 模型 glm-4.7-flash 拉黑（429）

用法：
    from app.logging_config import setup_logging
    setup_logging()   # 应用启动时调用一次（main.py lifespan / CLI 入口）

注意：
- 幂等：重复调用会先清空 root handlers 再重建，避免重复输出
- CLI 报告（如 sync 的知识库同步结果 print）保留 print——那是给操作者看的
  "用户界面"，不属于诊断日志
"""
import logging
import logging.config
from pathlib import Path

from app.config import settings

# 结构化格式：时间 | 级别 | 模块名 | 消息
_LOGGING_CONFIG = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "structured": {
            "format": "%(asctime)s | %(levelname)-7s | %(name)-28s | %(message)s",
            "datefmt": "%Y-%m-%d %H:%M:%S",
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "level": settings.LOG_LEVEL,
            "formatter": "structured",
        },
        "file": {
            "class": "logging.handlers.RotatingFileHandler",
            "level": settings.LOG_LEVEL,
            "formatter": "structured",
            # RotatingFileHandler 按大小轮转：单文件满 LOG_MAX_BYTES 切一个 .1，
            # 保留 LOG_BACKUPS 个历史文件
            "filename": str(Path(settings.LOG_DIR) / "app.log"),
            "maxBytes": settings.LOG_MAX_BYTES,
            "backupCount": settings.LOG_BACKUPS,
            "encoding": "utf-8",
        },
    },
    "loggers": {
        # 我们的应用模块统一走 root 配置；第三方库（openai/httpx 等）默认 WARNING
        # 避免其 DEBUG/INFO 刷屏污染
        "": {"handlers": ["console", "file"], "level": settings.LOG_LEVEL},
        "openai": {"handlers": ["console", "file"], "level": "WARNING", "propagate": False},
        "httpx": {"handlers": ["console", "file"], "level": "WARNING", "propagate": False},
        "uvicorn": {"handlers": ["console", "file"], "level": "WARNING", "propagate": False},
        "chromadb": {"handlers": ["console", "file"], "level": "WARNING", "propagate": False},
        "langgraph": {"handlers": ["console", "file"], "level": "WARNING", "propagate": False},
    },
}


def setup_logging() -> None:
    """初始化统一日志（幂等，应用启动调用一次）。

    先确保日志目录存在（文件 handler 需要），再 dictConfig 应用配置。
    """
    Path(settings.LOG_DIR).mkdir(parents=True, exist_ok=True)
    logging.config.dictConfig(_LOGGING_CONFIG)
    logging.getLogger(__name__).info(
        "日志系统已初始化: level=%s dir=%s", settings.LOG_LEVEL, settings.LOG_DIR
    )
