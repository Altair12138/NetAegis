"""定时巡检 / 备份调度（APScheduler）。

- SQLAlchemyJobStore 持久化到 .env 中 DB_URL（默认 SQLite），重启不丢任务。
- 进程内 BackgroundScheduler，与 FastAPI 同进程；多实例部署时改 Celery/外部调度器。
- 暴露三类触发：cron / interval / date。
"""

from __future__ import annotations

import threading
from typing import Any, Literal

from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
from apscheduler.triggers.interval import IntervalTrigger
from loguru import logger

from .config import get_settings
from .inventory import CSVInventorySource
from .models import JobCreate, JobType
from .runner import run_job

_scheduler: BackgroundScheduler | None = None
_lock = threading.Lock()


def get_scheduler() -> BackgroundScheduler:
    global _scheduler
    with _lock:
        if _scheduler is None:
            settings = get_settings()
            _scheduler = BackgroundScheduler(
                jobstores={"default": SQLAlchemyJobStore(url=settings.db_url)},
                job_defaults={"coalesce": True, "max_instances": 1, "misfire_grace_time": 600},
                timezone="Asia/Shanghai",
            )
            _scheduler.start()
            logger.info("APScheduler started")
    return _scheduler


def shutdown() -> None:
    global _scheduler
    with _lock:
        if _scheduler is not None:
            _scheduler.shutdown(wait=False)
            _scheduler = None


def is_running() -> bool:
    """检查调度器是否在运行（供 health check 使用）。"""
    with _lock:
        return _scheduler is not None and _scheduler.running


def job_count() -> int:
    """返回当前调度的 job 数量。"""
    with _lock:
        if _scheduler is None:
            return 0
        return len(_scheduler.get_jobs())


# ---------------------------------------------------------------------------
# 任务函数：被 APScheduler 反序列化调用，因此参数必须是基础类型
# ---------------------------------------------------------------------------

def _scheduled_inspect(
    inventory_path: str,
    job_type: str = "inspect",
    concurrency: int = 20,
    credential_profile: str = "default",
    command_keys: list[str] | None = None,
    command_tags: list[str] | None = None,
    device_filter: dict | None = None,
) -> None:
    """P2-11: 在后台线程中执行，避免阻塞 APScheduler worker。"""
    import threading

    def _run():
        devices = list(CSVInventorySource(inventory_path).fetch(**(device_filter or {})))
        if not devices:
            logger.warning(f"scheduled job: no devices matched ({inventory_path}, {device_filter})")
            return
        run_job(
            JobCreate(
                type=JobType(job_type),
                inventory_path=inventory_path,
                concurrency=concurrency,
                credential_profile=credential_profile,
                command_keys=command_keys,
                command_tags=command_tags,
                device_filter=device_filter,
            ),
            devices,
        )

    threading.Thread(target=_run, daemon=True).start()


# ---------------------------------------------------------------------------
# 对外 API
# ---------------------------------------------------------------------------

def add_schedule(
    schedule_id: str,
    trigger_type: Literal["cron", "interval", "date"],
    trigger_args: dict,
    inventory_path: str,
    job_type: str = "inspect",
    concurrency: int = 20,
    credential_profile: str = "default",
    command_keys: list[str] | None = None,
    command_tags: list[str] | None = None,
    device_filter: dict | None = None,
) -> dict:
    sched = get_scheduler()
    if trigger_type == "cron":
        trigger = CronTrigger(**trigger_args)        # 例 {"hour": 2, "minute": 0}
    elif trigger_type == "interval":
        trigger = IntervalTrigger(**trigger_args)    # 例 {"hours": 6}
    elif trigger_type == "date":
        trigger = DateTrigger(**trigger_args)        # 例 {"run_date": "2026-06-01 03:00:00"}
    else:
        raise ValueError(f"unknown trigger_type: {trigger_type}")

    job = sched.add_job(
        func=_scheduled_inspect,
        trigger=trigger,
        id=schedule_id,
        replace_existing=True,
        kwargs={
            "inventory_path": inventory_path,
            "job_type": job_type,
            "concurrency": concurrency,
            "credential_profile": credential_profile,
            "command_keys": command_keys,
            "command_tags": command_tags,
            "device_filter": device_filter,
        },
    )
    return _job_to_dict(job)


def list_schedules() -> list[dict]:
    return [_job_to_dict(j) for j in get_scheduler().get_jobs()]


def remove_schedule(schedule_id: str) -> bool:
    try:
        get_scheduler().remove_job(schedule_id)
        return True
    except Exception:  # noqa: BLE001
        return False


def pause_schedule(schedule_id: str) -> bool:
    try:
        get_scheduler().pause_job(schedule_id); return True
    except Exception:  # noqa: BLE001
        return False


def resume_schedule(schedule_id: str) -> bool:
    try:
        get_scheduler().resume_job(schedule_id); return True
    except Exception:  # noqa: BLE001
        return False


def _job_to_dict(j: Any) -> dict:
    return {
        "id": j.id,
        "next_run_time": j.next_run_time.isoformat() if j.next_run_time else None,
        "trigger": str(j.trigger),
        "kwargs": j.kwargs,
    }
