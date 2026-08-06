"""集中读取 .env 配置：所有密钥/连接信息只在这里出现一次。"""
import os
from pathlib import Path
from urllib.parse import quote_plus

from dotenv import find_dotenv, load_dotenv

# 自动向上查找项目根目录的 .env（不管从哪个目录启动）
load_dotenv(find_dotenv(usecwd=True))

# 项目根：backend/app/config.py → 上三级（app → backend → 根）。
# 路径类配置（KB_ROOT / KB_VECTOR_DIR）用绝对路径推导，
# 避免"从 backend/ 启动和从项目根启动"行为不一致（踩过 cwd 依赖的坑）。
BASE_DIR = Path(__file__).resolve().parent.parent.parent


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
    # 速度优先链（前端可切换）：glm-4-flash 最快但能力最弱，放最前；
    # 4.7 可用时仍能兜底（游标机制：成功后记住位置，会稳定在最快的模型上）
    GLM_MODELS_FAST: list[str] = [
        m.strip() for m in os.getenv(
            "GLM_MODELS_FAST", "glm-4-flash,glm-4.7-flash,glm-4.5-flash"
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

    # ===== RAG 知识库 =====
    # 源文档目录（唯一事实来源，sync 引擎扫描它）——绝对路径，与 cwd 无关
    KB_ROOT: str = os.getenv("KB_ROOT", str(BASE_DIR / "docs/knowledge"))
    # ChromaDB 持久化目录（Docker 挂卷落盘；切 Milvus 时此项失效）
    KB_VECTOR_DIR: str = os.getenv("KB_VECTOR_DIR", str(BASE_DIR / ".rag" / "kb_chroma"))
    KB_COLLECTION: str = os.getenv("KB_COLLECTION", "kb_docs")  # Chroma 要求 ≥3 字符

    # ===== 混合检索（Phase 2）=====
    # RRF 融合参数：score = Σ weight / (rrf_k + rank)；k 越大越平滑
    RRF_K: float = float(os.getenv("RRF_K", "60"))
    # 双路召回数（BM25 / 向量各召回多少条再融合）
    RRF_RECALL_N: int = int(os.getenv("RRF_RECALL_N", "10"))
    # 智谱 Rerank 二次精排开关（付费模型，按需开启）
    RERANK_ENABLED: bool = os.getenv("RERANK_ENABLED", "false").lower() == "true"
    # 精排后保留条数（召回 10 条精排取 5）
    RERANK_TOP_N: int = int(os.getenv("RERANK_TOP_N", "5"))

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
