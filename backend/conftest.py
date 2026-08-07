"""pytest 全局配置（测试工程化 ROADMAP P3.1）。

核心目标：**回归测试不依赖真实 GLM 调用与 MySQL**（优化文档五.5 承诺落地）。
策略：
1. 在 import app.* 之前用环境变量锁死外部依赖：
   - MONITOR_MODE=mock / ASR_MODE=mock：监控/语音走内置 mock，零网络
   - 隔离 KB 目录：KB_ROOT / KB_VECTOR_DIR 指向临时目录，不污染真实知识库
   - 占位 ZHIPU_API_KEY：让 OpenAI client 通过空 key 校验（无真实调用）
2. mock LLM：patch 5 个 LLM 调用点（intent/extract/rag_query/transcribe/ocr），
   返回确定性 JSON —— 不再依赖免费模型的稳定性
3. 内存 DB：用 SQLite :memory: 替换 MySQL（app.db.engine/SessionLocal），
   持久化测试离线可跑

注意：模块导入顺序敏感。env 设置必须放在【模块顶层】而非 fixture
（pytest 收集时即生效，先于任何 app.* import）。
"""
import os
import tempfile

# ===== ① 在 import app.* 之前设环境变量 =====
os.environ.setdefault("MONITOR_MODE", "mock")       # 监控查询/执行：内置 mock
os.environ.setdefault("ASR_MODE", "mock")           # 音频转写：占位，不调付费 ASR
os.environ.setdefault("ZHIPU_API_KEY", "ci-placeholder")  # 让 OpenAI client 过空 key 校验
os.environ.setdefault("RERANK_ENABLED", "false")    # 不开付费 rerank

# 隔离 KB：每个测试进程用独立临时知识库（不污染真实 docs/knowledge）
_TMP_KB_ROOT = tempfile.mkdtemp(prefix="pytest_kb_")
os.environ.setdefault("KB_ROOT", _TMP_KB_ROOT)
os.environ.setdefault("KB_VECTOR_DIR", os.path.join(_TMP_KB_ROOT, "chroma"))

import pytest  # noqa: E402


# ===== ② 全部 LLM 调用点（import app.* 后 patch 这些模块名）=====
@pytest.fixture(autouse=True)
def mock_llm(monkeypatch):
    """autouse：每个测试自动 mock 全部 LLM 调用，回归零真实调用。

    返回可定制 dict：
    - overrides["intent"]：intent 节点响应（None → 用内置路由）
    - overrides["extract"]：extract 节点响应
    - overrides["judge"]：rag_query judge 响应
    - overrides["chat"]：rag_query 回答生成文本
    - overrides["chat_with_image"]：OCR/转译响应
    测试想定制某个节点，改对应键即可（如 mock_llm["intent"]={...}）。
    """
    overrides = {
        "intent": None, "extract": None, "judge": None,
        "chat": "（mock 回答）这是基于知识库的确定性答案。",
        "chat_with_image": None,
    }

    def _route(system_prompt: str) -> dict:
        """按 system prompt 关键词给确定性响应（未被 overrides 覆盖时）。"""
        if "意图分类器" in system_prompt:
            return {"intent": "vpn", "request_type": "troubleshoot", "reason": "mock"}
        if "信息抽取器" in system_prompt:
            return {"device": "Windows 11", "error_code": "800", "username": ""}
        return {}

    def chat_json(messages, **_):
        sys = next((m.get("content", "") for m in messages
                    if m.get("role") == "system"), "")
        if "裁判" in sys and overrides["judge"] is not None:
            return overrides["judge"]
        if "意图分类器" in sys:
            return overrides["intent"] if overrides["intent"] is not None else _route(sys)
        if "信息抽取器" in sys:
            return overrides["extract"] if overrides["extract"] is not None else _route(sys)
        return _route(sys)

    def chat(messages, **_):
        return overrides["chat"]

    def chat_with_image(*_, **__):
        return overrides["chat_with_image"]

    import app.agents.nodes.intent
    import app.agents.nodes.extract
    import app.agents.nodes.rag_query
    import app.rag.transcribe
    import app.tools.ocr

    monkeypatch.setattr(app.agents.nodes.intent, "chat_json", chat_json)
    monkeypatch.setattr(app.agents.nodes.extract, "chat_json", chat_json)
    monkeypatch.setattr(app.agents.nodes.rag_query, "chat_json", chat_json)
    monkeypatch.setattr(app.agents.nodes.rag_query, "chat", chat)
    monkeypatch.setattr(app.rag.transcribe, "chat_with_image", chat_with_image)
    monkeypatch.setattr(app.tools.ocr, "chat_with_image", chat_with_image)

    # 重置监控 mock 状态：续期工具会改 MOCK_USERS（expired→False），
    # 跨测试共享这个可变字典会互相污染（先跑的续期让后跑的查证"过期"失效）。
    # 每个测试前恢复初始状态，保证用例确定性。
    import app.tools.monitor_api as monitor_api
    monitor_api.MOCK_USERS.update({
        "zhangsan": {"cert_valid_until": "2026-07-30", "expired": True},
        "lisi": {"cert_valid_until": "2027-01-15", "expired": False},
        "error_user": {"cert_valid_until": "2026-07-30", "expired": True},
    })

    return overrides


# ===== ③ 内存 DB：SQLite 替换 MySQL =====
@pytest.fixture()
def db_session():
    """内存 SQLite 会话：持久化测试离线跑，不碰 MySQL。

    替换 app.db 模块级 engine / SessionLocal，并建全量表。
    用 Base.metadata.create_all（不用 init_db：init_db 里有 MySQL 特有
    ALTER TABLE，SQLite 会报错）。
    """
    import sqlalchemy
    import app.db as db_mod
    import app.models  # noqa: F401  确保模型已注册

    engine = sqlalchemy.create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=sqlalchemy.pool.StaticPool,
    )
    db_mod.engine = engine
    db_mod.SessionLocal = sqlalchemy.orm.sessionmaker(
        bind=engine, autoflush=False, expire_on_commit=False)
    db_mod.Base.metadata.create_all(engine)

    db = db_mod.SessionLocal()
    yield db
    db.close()
    engine.dispose()
