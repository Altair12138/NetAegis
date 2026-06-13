"""FastAPI 应用入口。

启动：
    uv run uvicorn inspection.api.main:app --reload --port 8080
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI

from .. import scheduler as sched_mod
from ..config import get_settings
from ..logging_setup import configure
from .routes import router

settings = get_settings()
configure(settings.log_dir, settings.ssh_debug)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 启动 APScheduler（懒加载，但 lifespan 早一步触发，避免首个调度请求阻塞）
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


@app.get("/health")
def health():
    return {"status": "ok"}
