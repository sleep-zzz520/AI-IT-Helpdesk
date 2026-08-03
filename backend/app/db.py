"""数据库连接（SQLAlchemy 2.0 风格）。

engine: 连接池（管理到 MySQL 的连接）
SessionLocal: 会话工厂（每个请求用它开一个会话）
Base: ORM 模型基类（models.py 里的表都继承它）
"""
from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from app.config import settings

engine = create_engine(settings.mysql_url, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


class Base(DeclarativeBase):
    """所有 ORM 模型的基类。"""


def init_db() -> None:
    """首次建表（开发期用；生产迁移用 Alembic，P2 再做）。"""
    import app.models  # noqa: F401 确保模型已注册
    Base.metadata.create_all(engine)
