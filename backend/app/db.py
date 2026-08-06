"""数据库连接（SQLAlchemy 2.0 风格）。

engine: 连接池（管理到 MySQL 的连接）
SessionLocal: 会话工厂（每个请求用它开一个会话）
Base: ORM 模型基类（models.py 里的表都继承它）
"""
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from app.config import settings

engine = create_engine(settings.mysql_url, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


class Base(DeclarativeBase):
    """所有 ORM 模型的基类。"""


def init_db() -> None:
    """建表 + 轻量迁移（开发期用；生产迁移用 Alembic，P2 再做）。

    create_all 只会建【缺失的表】，不会给已存在的表加新列，
    所以新增列要单独做增量迁移（见 _ensure_columns）。
    """
    import app.models  # noqa: F401 确保模型已注册
    Base.metadata.create_all(engine)
    _ensure_columns()
    _seed()


# 新增列清单：表名 → [列名, DDL 类型]。缺列就 ALTER 补上（老库平滑升级）。
_EXTRA_COLUMNS = {
    "messages": [
        ("elapsed_ms", "INT NULL"),                          # agent 回复总耗时（ms）
        ("feedback", "VARCHAR(8) NULL"),                     # 用户反馈 up/down（反馈闭环）
    ],
    "traces": [("elapsed_ms", "INT NULL")],     # 节点执行耗时（ms）
    "kb_documents": [
        ("kb_name", "VARCHAR(32) NOT NULL DEFAULT 'default'"),  # 台账隔离
        ("valid_to", "VARCHAR(16) NULL"),                      # 文档有效期（过期预警）
    ],
    "conversations": [("tenant_id", "INT NULL")],  # 多租户隔离（老会话可空=未归租户）
}


def _ensure_columns() -> None:
    """给已存在的表补新增列（幂等：列已存在就跳过）。"""
    inspector = inspect(engine)
    with engine.begin() as conn:
        for table, columns in _EXTRA_COLUMNS.items():
            existing = {c["name"] for c in inspector.get_columns(table)}
            for col, ddl in columns:
                if col not in existing:
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}"))
    _ensure_kb_constraints()


def _ensure_kb_constraints() -> None:
    """kb_documents 唯一约束迁移：path 全局唯一 → (kb_name, path) 复合唯一。

    背景：早期版本 path 是全局唯一，多知识库实例（测试/多库）会冲突。
    MySQL 不支持 CREATE INDEX IF NOT EXISTS，先查 information_schema 再建。
    """
    with engine.begin() as conn:
        def _has_index(name: str) -> bool:
            return conn.execute(text(
                "SELECT COUNT(*) FROM information_schema.statistics "
                "WHERE table_schema=DATABASE() AND table_name='kb_documents' "
                "AND index_name=:name"
            ), {"name": name}).scalar()

        if _has_index("path"):
            conn.execute(text("ALTER TABLE kb_documents DROP INDEX `path`"))
        if _has_index("doc_id"):
            # doc_id = 路径哈希，同路径不同 kb_name 会重复——不需要全局唯一
            conn.execute(text("ALTER TABLE kb_documents DROP INDEX `doc_id`"))
        if not _has_index("uq_kb_name_path"):
            conn.execute(text(
                "CREATE UNIQUE INDEX uq_kb_name_path ON kb_documents (kb_name, path)"))


# ===== 种子数据（幂等：只在空表时灌入，重复启动不重复插入）=====
# 演示账号（生产环境应移除并走注册流程）：
# - admin / Admin@2025   管理员（租户 acme，可看审计/管理知识库）
# - zhangsan / Zhangsan@2025  普通用户（租户 acme）
# - lisi / Lisi@2025     普通用户（租户 globex，与 zhangsan 不同租户 → 数据隔离演示）
_SEED_TENANTS = [
    {"code": "acme", "name": "Acme 集团"},
    {"code": "globex", "name": "Globex 科技"},
]
_SEED_USERS = [
    {"username": "admin", "password": "Admin@2025", "display_name": "系统管理员",
     "role": "admin", "tenant": "acme"},
    {"username": "zhangsan", "password": "Zhangsan@2025", "display_name": "张三",
     "role": "user", "tenant": "acme"},
    {"username": "lisi", "password": "Lisi@2025", "display_name": "李四",
     "role": "user", "tenant": "globex"},
]


def _seed() -> None:
    """首次启动灌入演示租户和用户（幂等：已有数据则跳过）。

    教学点：种子数据是"开发体验"，不是"安全实现"——
    演示密码写死在代码里没问题，生产环境必须有注册流程 + 强密码策略。
    """
    from app.models import Tenant, User  # 局部导入避免循环引用
    from app.security import hash_password  # 同上：db→security 延迟导入
    with SessionLocal() as db:
        if db.query(Tenant).count() > 0:
            return  # 已初始化过，跳过（幂等）
        tenant_map: dict[str, int] = {}
        for t in _SEED_TENANTS:
            row = Tenant(code=t["code"], name=t["name"])
            db.add(row)
            db.flush()  # 先拿到自增 id
            tenant_map[t["code"]] = row.id
        for u in _SEED_USERS:
            db.add(User(
                username=u["username"],
                password_hash=hash_password(u["password"]),
                display_name=u["display_name"],
                role=u["role"],
                tenant_id=tenant_map[u["tenant"]],
                active=1,
            ))
        db.commit()
