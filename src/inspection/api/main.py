"""FastAPI 应用入口。

启动：
    uv run uvicorn inspection.api.main:app --reload --port 8080

P1-8 改进：/health 端点增强，检查 DB 连通性和调度器状态。
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text

from .. import scheduler as sched_mod
from ..config import get_settings
from ..db import session
from ..logging_setup import configure
from .routes import router

settings = get_settings()
configure(settings.log_dir, settings.ssh_debug,
          log_level=settings.log_level, log_format=settings.log_format)


@asynccontextmanager
async def lifespan(app: FastAPI):
    sched_mod.get_scheduler()
    yield
    sched_mod.shutdown()


app = FastAPI(
    title="Network Inspection Platform",
    version="0.2.0",
    description="基于 Nornir + Netmiko 的网络设备巡检 / 配置备份 / 定时调度 / 报表",
    lifespan=lifespan,
)

app.include_router(router)

# P3-20: 统一错误响应格式。
@app.exception_handler(404)
async def not_found_handler(request: Request, exc: Exception):
    return JSONResponse(
        status_code=404,
        content={"error": {"code": "NOT_FOUND", "message": "Resource not found"}},
    )


@app.get("/health")
def health():
    """P1-8: 增强健康检查，包含 DB 和调度器状态。"""
    scheduler_ok = sched_mod.is_running()
    scheduler_jobs = sched_mod.job_count()
    db_ok = False
    db_error = None
    try:
        with session() as s:
            s.execute(text("SELECT 1"))
        db_ok = True
    except Exception as e:
        db_error = str(e)

    overall = "ok" if (db_ok and scheduler_ok) else "degraded"
    resp: dict = {
        "status": overall,
        "db": {"ok": db_ok},
        "scheduler": {"ok": scheduler_ok, "jobs": scheduler_jobs},
        "version": "0.2.0",
    }
    if db_error:
        resp["db"]["error"] = db_error
    return resp
