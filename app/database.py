"""SQLite 持久化层（SQLAlchemy 2.x 同步引擎）。"""

from __future__ import annotations

import os
from collections.abc import Generator

from sqlalchemy import String, create_engine, inspect, text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

DB_PATH = os.getenv("SCENIC_DB_PATH", "/data/scenic.db")

# 报表生命周期状态：在线（参与在线求解与查数）/ 归档（留档备查，不参与查数）。
STATUS_ONLINE = "online"
STATUS_ARCHIVED = "archived"
VALID_STATUSES = (STATUS_ONLINE, STATUS_ARCHIVED)


class Base(DeclarativeBase):
    pass


class ReportRow(Base):
    __tablename__ = "reports"

    report_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    start_date: Mapped[str] = mapped_column(String(10), nullable=False)  # ISO YYYY-MM-DD
    end_date: Mapped[str] = mapped_column(String(10), nullable=False)
    total: Mapped[int] = mapped_column(nullable=False)
    version: Mapped[int] = mapped_column(default=1, nullable=False)
    # 旧库（v1.0）无此列，init_db 会自动 ALTER 补列，存量报表默认在线。
    status: Mapped[str] = mapped_column(
        String(16), default=STATUS_ONLINE, nullable=False, server_default=STATUS_ONLINE
    )


def _connect_args() -> dict:
    # /data 在容器内由 volume 提供；本地自测可落到临时文件。
    parent = os.path.dirname(DB_PATH)
    if parent:
        try:
            os.makedirs(parent, exist_ok=True)
        except OSError:
            raise RuntimeError(
                f"无法创建数据库目录 {parent}；请通过环境变量 SCENIC_DB_PATH "
                "指定可写路径（容器内默认 /data/scenic.db 由卷挂载提供）"
            )
    return {"check_same_thread": False}


engine = create_engine(
    f"sqlite:///{DB_PATH}",
    connect_args=_connect_args(),
    future=True,
)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False, future=True)


def init_db() -> None:
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        # 兼容旧版 SQLite 库：缺 status 列时在线 DDL 补上，存量报表视为在线。
        columns = {col["name"] for col in inspect(conn).get_columns("reports")}
        if "status" not in columns:
            conn.execute(
                text(
                    f"ALTER TABLE reports ADD COLUMN status VARCHAR(16) "
                    f"NOT NULL DEFAULT '{STATUS_ONLINE}'"
                )
            )
        # WAL 让读请求与写请求互不阻塞。
        conn.execute(text("PRAGMA journal_mode=WAL"))
        conn.execute(text("PRAGMA foreign_keys=ON"))


def get_session() -> Generator:
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
