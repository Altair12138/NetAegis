"""SQLAlchemy 持久化层：Job / DeviceRun / JobControl。

P0 改进：
- 新增 JobControlRow 表格，将 job 暂停/取消状态持久化到 DB，
  解决多 worker 部署时 pause/cancel 跨进程失效的问题。
- engine() 改为线程安全的 sessionmaker 模式，消除竞态条件。
"""

from __future__ import annotations

import threading
from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, Integer, String, create_engine
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    Session,
    mapped_column,
    sessionmaker,
)

from .config import get_settings


class Base(DeclarativeBase):
    pass


class JobRow(Base):
    __tablename__ = "jobs"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    type: Mapped[str] = mapped_column(String)
    status: Mapped[str] = mapped_column(String, index=True)
    concurrency: Mapped[int] = mapped_column()
    created_at: Mapped[datetime] = mapped_column(DateTime)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    paused_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    result_dir: Mapped[str] = mapped_column(String)
    extra: Mapped[dict] = mapped_column(JSON, default=dict)


class DeviceRunRow(Base):
    __tablename__ = "device_runs"
    job_id: Mapped[str] = mapped_column(String, primary_key=True)
    device_name: Mapped[str] = mapped_column(String, primary_key=True)
    mgmt_ip: Mapped[str] = mapped_column(String)
    status: Mapped[str] = mapped_column(String, index=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    log_path: Mapped[str | None] = mapped_column(String, nullable=True)
    json_path: Mapped[str | None] = mapped_column(String, nullable=True)
    name_mismatch: Mapped[bool] = mapped_column(default=False)
    error: Mapped[str | None] = mapped_column(String, nullable=True)


# P0-1: DB-backed job control table for cross-process pause/cancel.
class JobControlRow(Base):
    __tablename__ = "job_control"
    job_id: Mapped[str] = mapped_column(String, primary_key=True)
    paused: Mapped[bool] = mapped_column(Boolean, default=False)
    canceled: Mapped[bool] = mapped_column(Boolean, default=False)


_engine = None
_lock = threading.Lock()
SessionFactory: sessionmaker | None = None


def engine():
    """线程安全的 engine 创建（P0 修复：加锁消除竞态）。"""
    global _engine, SessionFactory
    if _engine is None:
        with _lock:
            if _engine is None:
                settings = get_settings()
                _engine = create_engine(
                    settings.db_url,
                    future=True,
                    pool_size=settings.db_pool_size,
                    max_overflow=settings.db_max_overflow,
                    pool_pre_ping=True,
                )
                Base.metadata.create_all(_engine)
                # P2-12: 启用 WAL 模式提升并发写入性能。
                with _engine.connect() as conn:
                    conn.exec_driver_sql("PRAGMA journal_mode=WAL")
                    conn.commit()
                SessionFactory = sessionmaker(bind=_engine, future=True)
    return _engine


def session() -> Session:
    """创建新的 Session（优先使用 sessionmaker）。"""
    if SessionFactory is not None:
        return SessionFactory()
    return Session(engine(), future=True)
