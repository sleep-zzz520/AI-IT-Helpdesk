"""集中读取 .env 配置：所有密钥/连接信息只在这里出现一次。"""
import os
from urllib.parse import quote_plus

from dotenv import find_dotenv, load_dotenv

# 自动向上查找项目根目录的 .env（不管从哪个目录启动）
load_dotenv(find_dotenv(usecwd=True))


class Settings:
    # ===== GLM（智谱）=====
    ZHIPU_API_KEY: str = os.getenv("ZHIPU_API_KEY", "")
    GLM_BASE_URL: str = os.getenv("GLM_BASE_URL", "https://open.bigmodel.cn/api/paas/v4")
    # 模型链：按优先级排列，限流/不可用时自动切下一个（故障转移）
    GLM_MODELS: list[str] = [
        m.strip() for m in os.getenv(
            "GLM_MODELS", "glm-4.7-flash,glm-4.6-flash,glm-4.5-flash,glm-4-flash"
        ).split(",") if m.strip()
    ]
    # 视觉模型链（OCR 用）
    GLM_VISION_MODELS: list[str] = [
        m.strip() for m in os.getenv("GLM_VISION_MODELS", "glm-4v-flash").split(",") if m.strip()
    ]

    # ===== MySQL =====
    MYSQL_HOST: str = os.getenv("MYSQL_HOST", "127.0.0.1")
    MYSQL_PORT: int = int(os.getenv("MYSQL_PORT", "3306"))
    MYSQL_USER: str = os.getenv("MYSQL_USER", "helpdesk")
    MYSQL_PASSWORD: str = os.getenv("MYSQL_PASSWORD", "")
    MYSQL_DB: str = os.getenv("MYSQL_DB", "it_helpdesk")

    @property
    def mysql_url(self) -> str:
        """SQLAlchemy 连接串（后续建表/CRUD 都用它）。

        注意：密码里的特殊字符（@ : / 等）必须 URL 编码，
        否则 URL 解析会把 @ 后面当成主机名（经典坑：'2025@127.0.0.1'）。
        """
        return (
            f"mysql+pymysql://{self.MYSQL_USER}:{quote_plus(self.MYSQL_PASSWORD)}"
            f"@{self.MYSQL_HOST}:{self.MYSQL_PORT}/{self.MYSQL_DB}?charset=utf8mb4"
        )


settings = Settings()
