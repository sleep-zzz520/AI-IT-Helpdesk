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


# 新增列清单：表名 → [列名, DDL 类型]。缺列就 ALTER 补上（老库平滑升级）。
_EXTRA_COLUMNS = {
    "messages": [("elapsed_ms", "INT NULL")],   # agent 回复总耗时（ms）
    "traces": [("elapsed_ms", "INT NULL")],     # 节点执行耗时（ms）
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
