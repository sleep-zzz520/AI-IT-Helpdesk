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

    # ===== 日志 =====
    # 生产排障可查：统一 logging（级别可配 + 按天轮转文件 + 结构化格式）
    LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")          # DEBUG/INFO/WARNING/ERROR
    LOG_DIR: str = os.getenv("LOG_DIR", str(BASE_DIR / "logs"))  # 日志目录（容器挂卷持久化）
    LOG_MAX_BYTES: int = int(os.getenv("LOG_MAX_BYTES", "10485760"))  # 单文件 10MB 轮转
    LOG_BACKUPS: int = int(os.getenv("LOG_BACKUPS", "5"))           # 保留 5 个轮转文件

    # ===== RAG 知识库 =====
    # 源文档目录（唯一事实来源，sync 引擎扫描它）——绝对路径，与 cwd 无关
    KB_ROOT: str = os.getenv("KB_ROOT", str(BASE_DIR / "docs/knowledge"))
    # ChromaDB 持久化目录（Docker 挂卷落盘；切 Milvus 时此项失效）
    KB_VECTOR_DIR: str = os.getenv("KB_VECTOR_DIR", str(BASE_DIR / ".rag" / "kb_chroma"))
    KB_COLLECTION: str = os.getenv("KB_COLLECTION", "kb_docs")  # Chroma 要求 ≥3 字符

    # ===== 蓝绿切换（Phase 5）=====
    # 两套 Chroma collection 交替承载索引，任何时刻只有一套"在岗"（active）：
    # - 蓝 = kb_docs（老库名）：存量数据天然是蓝，零迁移平滑进入蓝绿体系
    # - 绿 = kb_docs_candidate：候选库（sync 全量写入的目标）
    # sync 永远写"当前非 active"的那套 → 线上检索零中断；
    # 切换 = 改落盘指针（秒级生效 + 重启保持）；旧库未被覆盖前随时可回滚
    KB_COLLECTION_BLUE: str = os.getenv("KB_COLLECTION_BLUE", "kb_docs")
    KB_COLLECTION_GREEN: str = os.getenv("KB_COLLECTION_GREEN", "kb_docs_candidate")
    # active 指针落盘文件（内容 = collection 名；不存在 → 默认蓝，兼容老部署）
    KB_ACTIVE_FILE: Path = BASE_DIR / ".rag" / "kb_active.txt"

    # ===== 混合检索（Phase 2）=====
    # RRF 融合参数：score = Σ weight / (rrf_k + rank)；k 越大越平滑
    RRF_K: float = float(os.getenv("RRF_K", "60"))
    # 双路召回数（BM25 / 向量各召回多少条再融合）
    RRF_RECALL_N: int = int(os.getenv("RRF_RECALL_N", "10"))
    # 智谱 Rerank 二次精排开关（付费模型，按需开启）
    RERANK_ENABLED: bool = os.getenv("RERANK_ENABLED", "false").lower() == "true"
    # 精排后保留条数（召回 10 条精排取 5）
    RERANK_TOP_N: int = int(os.getenv("RERANK_TOP_N", "5"))

    # ===== 联合检索（多库，Phase 5）=====
    # 逗号分隔的独立库名（如 kb_docs_scale）：对话问答跨库联合检索。
    # 默认空 = 仅检索在岗库（与旧版行为一致，opt-in 开启）。
    # 蓝绿库/在岗库自动排除（防 RRF 同 id 双重加分）。
    KB_EXTRA_COLLECTIONS: str = os.getenv("KB_EXTRA_COLLECTIONS", "")
    # 向量路召回的最低相关度（cosine 相似度）：
    # 0.5 以下的"弱相关"命中不进证据池（实测：0.43 的弱命中让模型
    # 基于无关文档瞎编回答——宁可诚实"暂无"，不可错答）
    VECTOR_MIN_SCORE: float = float(os.getenv("VECTOR_MIN_SCORE", "0.5"))

    # ===== 安全（多租户/权限体系）=====
    # Token 签名密钥：生产必须改成随机强密钥（openssl rand -hex 32），
    # 默认值是开发用——泄露=token 可被伪造（但密码哈希仍安全，不影响已存账号）
    SECRET_KEY: str = os.getenv("SECRET_KEY", "dev-secret-change-me-in-production")

    # ===== 监控 API 对接（P2：替代 mock）=====
    # mock=读内存字典（离线测试，零依赖）；real=HTTP 调用监控服务
    MONITOR_MODE: str = os.getenv("MONITOR_MODE", "mock")
    # 监控服务地址（real 模式）：换真实系统只改这一项
    MONITOR_BASE_URL: str = os.getenv("MONITOR_BASE_URL", "http://127.0.0.1:9100")
    # 认证 token（真实系统用；本地模拟服务不需要）
    MONITOR_API_TOKEN: str = os.getenv("MONITOR_API_TOKEN", "")
    # HTTP 超时（秒）——真实网络必须设，否则 Agent 卡死
    MONITOR_TIMEOUT: float = float(os.getenv("MONITOR_TIMEOUT", "5.0"))
    # 失败重试次数（5xx/超时才重试，4xx 不重试）
    MONITOR_MAX_RETRIES: int = int(os.getenv("MONITOR_MAX_RETRIES", "2"))

    # ===== 音频/视频（Phase 4，GLM-ASR 付费接口）=====
    # real=真实调用（~0.02 元/分钟）；mock=占位转写（离线测试不烧钱）
    ASR_MODE: str = os.getenv("ASR_MODE", "real")
    # 视频抽帧上限（每帧一次 GLM-4V 转译，免费但限流：长视频不能无限抽）
    VIDEO_MAX_FRAMES: int = int(os.getenv("VIDEO_MAX_FRAMES", "10"))
    # 视频音轨转写时长上限（秒）：ASR 计时收费，知识片段前 N 秒足够
    VIDEO_MAX_AUDIO_SECONDS: int = int(os.getenv("VIDEO_MAX_AUDIO_SECONDS", "60"))

    @property
    def active_collection(self) -> str:
        """当前在岗的 collection（动态读落盘指针，切换后立即生效）。

        为什么每次读文件而不是缓存：蓝绿切换就是改这个文件，缓存会让
        切换在重启前不生效（"秒级切换"卖点就没了）。文件读取开销纳秒级。
        """
        try:
            v = self.KB_ACTIVE_FILE.read_text().strip()
            if v in (self.KB_COLLECTION_BLUE, self.KB_COLLECTION_GREEN):
                return v
        except OSError:
            pass  # 指针文件不存在（首次部署/老库）→ 蓝
        return self.KB_COLLECTION_BLUE

    @property
    def candidate_collection(self) -> str:
        """当前候选 collection（sync 写入目标；与 active 交替复用）。"""
        return (self.KB_COLLECTION_GREEN
                if self.active_collection == self.KB_COLLECTION_BLUE
                else self.KB_COLLECTION_BLUE)

    def switch_active(self) -> str:
        """切换 active 指针到候选库（回滚 = 再调用一次：两库交替）。

        调用前需校验候选库非空（切到空库 = 全站检索瘫痪，见 kb API）。
        """
        self.KB_ACTIVE_FILE.parent.mkdir(parents=True, exist_ok=True)
        self.KB_ACTIVE_FILE.write_text(self.candidate_collection, encoding="utf-8")
        return self.active_collection

    @property
    def extra_collections(self) -> list[str]:
        """解析后的独立库名单：去空白、去重、排除蓝绿与在岗库。

        为什么排除蓝绿/在岗：候选库可能是进行中的全量同步（半新半旧），
        且与在岗库 chunk id 完全相同——联合检索会把同一批 chunk 双重加分。
        """
        excluded = {self.KB_COLLECTION_BLUE, self.KB_COLLECTION_GREEN,
                    self.active_collection}
        return list(dict.fromkeys(
            n.strip() for n in self.KB_EXTRA_COLLECTIONS.split(",")
            if n.strip() and n.strip() not in excluded))

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
