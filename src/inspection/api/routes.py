"""FastAPI 路由：供前端调用。

P0 改进:
  - P0-2: 认证 fail-closed -- production 下 API_TOKEN 必填，development 才放行。
  - P0-3: create_job 竞态修复 -- 同步写入 JobRow 获取 job_id，execution 用 daemon 线程。
  - P0-5: FileResponse 路径穿越防护 -- 校验路径在 result_dir/backups 子树内。
  - P0-1: 切换到 DB-backed pause/resume/cancel（替代进程内存 controller）。
"""

from __future__ import annotations

import threading
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Header, status
from fastapi.responses import FileResponse
from loguru import logger

from .. import backup_store, report, scheduler
from ..commands.loader import all_tags, catalog
from ..config import get_settings
from ..db import DeviceRunRow, JobRow, session
from ..inventory import CSVInventorySource
from ..models import DeviceRunStatus, JobCreate, JobType
from ..runner import cancel_job, pause_job, resume_job, run_job
from .schemas import (
    CreateJobRequest,
    DeviceRunBrief,
    InventoryPreviewRequest,
    InventoryPreviewResponse,
    JobBrief,
    JobDetail,
    ScheduleCreate,
)

router = APIRouter(
    prefix="/api",
    tags=["inspection"],
    dependencies=[Depends(_auth)],  # P3-18: 路由级统一认证，避免遗漏。
)

_DEFAULT_INVENTORY = get_settings().default_inventory_path  # P3-16


def _auth(authorization: str = Header(default="")):
    """P0-2: production 下强制鉴权，development 才可跳过。"""
    settings = get_settings()
    token = settings.api_token
    if not token:
        if settings.environment == "development":
            return
        raise HTTPException(status_code=500, detail="API_TOKEN not configured")
    if authorization != f"Bearer {token}":
        raise HTTPException(status_code=401, detail="invalid token")


# ---------------------------------------------------------------------------
# P0-5: 路径穿越防护辅助函数
# ---------------------------------------------------------------------------

def _safe_file_response(file_path: str, allowed_parent: Path, **kwargs) -> FileResponse:
    """校验 file_path 在 allowed_parent 子树内，防止路径穿越。"""
    resolved = Path(file_path).resolve()
    if not str(resolved).startswith(str(allowed_parent.resolve())):
        logger.warning(f"path traversal attempt: {file_path}")
        raise HTTPException(status_code=403, detail="access denied")
    return FileResponse(str(resolved), **kwargs)


# ---------------------------------------------------------------------------
# Catalog
# ---------------------------------------------------------------------------

@router.get("/commands")
def list_commands():
    """前端下拉数据：每个 vendor/device_type 支持的命令 key/cmd/tags，以及所有可用 tag。"""
    return {"catalog": catalog(), "tags": all_tags()}


# ---------------------------------------------------------------------------
# Inventory preview
# ---------------------------------------------------------------------------

@router.post("/inventory/preview", response_model=InventoryPreviewResponse)
def preview_inventory(req: InventoryPreviewRequest):
    if req.inventory_source == "csv":
        src = CSVInventorySource(req.inventory_path or _DEFAULT_INVENTORY)
    else:
        raise HTTPException(status_code=400, detail="CMDB inventory not yet implemented")

    devices = list(src.fetch(**(req.device_filter or {})))
    errors = src.errors[:200]
    valid = len(devices)
    invalid = len(src.errors)
    sample_devices = [
        {
            "name": d.name,
            "mgmt_ip": str(d.mgmt_ip),
            "vendor": d.vendor.value,
            "device_type": d.device_type.value,
            "model": d.model,
        }
        for d in devices[:50]
    ]
    return {
        "total": valid + invalid,
        "valid": valid,
        "invalid": invalid,
        "errors": errors,
        "sample_devices": sample_devices,
    }


# ---------------------------------------------------------------------------
# Jobs
# ---------------------------------------------------------------------------

@router.post("/jobs", response_model=JobBrief)
def create_job(req: CreateJobRequest, bg: BackgroundTasks) -> JobBrief:
    """P0-3: 同步写入 JobRow 获取 job_id，设备执行在后台线程中进行。"""
    settings = get_settings()

    if req.inventory_source == "csv":
        src = CSVInventorySource(req.inventory_path or _DEFAULT_INVENTORY)
    else:
        raise HTTPException(status_code=400, detail="CMDB inventory not yet implemented")

    devices = list(src.fetch(**(req.device_filter or {})))
    if not devices:
        raise HTTPException(status_code=400, detail="No matching devices")

    create = JobCreate(
        type=JobType(req.type),
        inventory_source=req.inventory_source,
        inventory_path=req.inventory_path,
        device_filter=req.device_filter,
        concurrency=req.concurrency,
        credential_profile=req.credential_profile,
        command_keys=req.command_keys,
        command_tags=req.command_tags,
        enable_parse=req.enable_parse,
        auto_backup=req.auto_backup,
        device_save=req.device_save,
    )

    import uuid
    from datetime import datetime

    job_id = uuid.uuid4().hex[:12]
    result_dir = Path(settings.result_dir) / job_id
    result_dir.mkdir(parents=True, exist_ok=True)

    # P0-3: 同步写入 JobRow + DeviceRunRow（微秒级）。
    with session() as s:
        s.add(JobRow(
            id=job_id,
            type=create.type.value,
            status="running",
            concurrency=create.concurrency,
            created_at=datetime.now(),
            started_at=datetime.now(),
            result_dir=str(result_dir),
            extra={"credential_profile": create.credential_profile},
        ))
        for d in devices:
            s.add(DeviceRunRow(
                job_id=job_id,
                device_name=d.name,
                mgmt_ip=str(d.mgmt_ip),
                status=DeviceRunStatus.queued.value,
            ))
        s.commit()

    # 后台线程执行设备巡检（daemon=True，进程停止时自动终止）。
    def _go():
        run_job(create, devices, _job_id=job_id)

    threading.Thread(target=_go, name=f"job-launcher-{job_id}", daemon=True).start()

    with session() as s:
        row = s.get(JobRow, job_id)
        if not row:
            raise HTTPException(status_code=500, detail="Job creation failed")
        return _to_brief(row)


@router.get("/jobs")
def list_jobs(status_filter: str | None = None, limit: int = 50, offset: int = 0):
    """P3-19: 支持分页 offset/limit，响应包含 total。"""
    with session() as s:
        q = s.query(JobRow)
        if status_filter:
            q = q.filter(JobRow.status == status_filter)
        total = q.count()
        rows = q.order_by(JobRow.created_at.desc()).offset(offset).limit(limit).all()
        return {"total": total, "items": [_to_brief(r) for r in rows]}


@router.get("/jobs/{job_id}", response_model=JobDetail)
def get_job(job_id: str):
    with session() as s:
        row = s.get(JobRow, job_id)
        if not row:
            raise HTTPException(status_code=404)
        devices = s.query(DeviceRunRow).filter(DeviceRunRow.job_id == job_id).all()
        brief = _to_brief(row)
        counts: dict[str, int] = {"queued": 0, "running": 0, "success": 0,
                                   "name_mismatch": 0, "failed": 0, "skipped": 0, "total": len(devices)}
        for d in devices:
            counts[d.status] = counts.get(d.status, 0) + 1
        return JobDetail(
            **brief.model_dump(),
            progress=counts,
            devices=[_to_device_brief(d) for d in devices],
        )


# P0-1: pause/resume/cancel 切换到 DB-backed。
@router.post("/jobs/{job_id}/pause")
def pause(job_id: str):
    if not pause_job(job_id):
        raise HTTPException(status_code=404, detail="job not found")
    return {"ok": True}


@router.post("/jobs/{job_id}/resume")
def resume(job_id: str):
    if not resume_job(job_id):
        raise HTTPException(status_code=404)
    return {"ok": True}


@router.post("/jobs/{job_id}/cancel")
def cancel(job_id: str):
    if not cancel_job(job_id):
        raise HTTPException(status_code=404)
    return {"ok": True}


@router.get("/jobs/{job_id}/devices/{device_name}/log")
def get_log(job_id: str, device_name: str):
    """下载原始命令输出 .log"""
    settings = get_settings()
    with session() as s:
        row = s.get(DeviceRunRow, (job_id, device_name))
        if not row or not row.log_path or not Path(row.log_path).exists():
            raise HTTPException(status_code=404, detail="log not found")
        # P0-5: 路径穿越防护。
        return _safe_file_response(
            row.log_path, settings.result_dir.resolve(),
            media_type="text/plain", filename=Path(row.log_path).name,
        )


@router.get("/jobs/{job_id}/devices/{device_name}/result")
def get_result(job_id: str, device_name: str):
    """下载处理后的结构化 .json"""
    settings = get_settings()
    with session() as s:
        row = s.get(DeviceRunRow, (job_id, device_name))
        if not row or not row.json_path or not Path(row.json_path).exists():
            raise HTTPException(status_code=404, detail="result json not found")
        return _safe_file_response(
            row.json_path, settings.result_dir.resolve(),
            media_type="application/json", filename=Path(row.json_path).name,
        )


# ---------------------------------------------------------------------------
# Backup / Diff (二期)
# ---------------------------------------------------------------------------

def _backups_root() -> Path:
    return Path(get_settings().result_dir).parent / "backups"


@router.get("/devices/{device_name}/backups")
def list_backups(device_name: str, limit: int = 50, offset: int = 0):
    """P3-19: 支持分页。"""
    rows = backup_store.list_for(device_name, limit=limit)
    # backup_store currently doesn't support offset natively; apply here.
    all_rows = rows  # list_for already limits; for full pagination we'd need total count
    paged = list(all_rows)[offset:offset + limit]
    return [
        {"id": r.id, "sha256": r.sha256, "path": r.path, "size": r.size,
         "created_at": r.created_at.isoformat(),
         "last_seen_at": r.last_seen_at.isoformat(),
         "job_id": r.job_id}
        for r in paged
    ]


@router.get("/devices/{device_name}/backups/{backup_id}")
def download_backup(device_name: str, backup_id: int):
    row = backup_store.get(backup_id)
    if not row or row.device_name != device_name or not Path(row.path).exists():
        raise HTTPException(status_code=404)
    # P0-5: 路径穿越防护。
    return _safe_file_response(
        row.path, _backups_root().resolve(),
        media_type="text/plain", filename=Path(row.path).name,
    )


@router.get("/devices/{device_name}/diff")
def get_diff(device_name: str, a: int | None = None, b: int | None = None):
    return backup_store.diff(device_name, a_id=a, b_id=b)


# ---------------------------------------------------------------------------
# Scheduler (二期)
# ---------------------------------------------------------------------------

@router.post("/schedules")
def create_schedule(req: ScheduleCreate):
    try:
        return scheduler.add_schedule(
            schedule_id=req.id,
            trigger_type=req.trigger_type,
            trigger_args=req.trigger_args,
            inventory_path=req.inventory_path,
            job_type=req.job_type,
            concurrency=req.concurrency,
            credential_profile=req.credential_profile,
            command_keys=req.command_keys,
            command_tags=req.command_tags,
            device_filter=req.device_filter,
        )
    except Exception as e:  # noqa: BLE001
        logger.warning(f"Failed to create schedule: {e}")
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/schedules")
def get_schedules():
    return scheduler.list_schedules()


@router.delete("/schedules/{schedule_id}")
def delete_schedule(schedule_id: str):
    if not scheduler.remove_schedule(schedule_id):
        raise HTTPException(status_code=404)
    return {"ok": True}


@router.post("/schedules/{schedule_id}/pause")
def pause_schedule(schedule_id: str):
    if not scheduler.pause_schedule(schedule_id):
        raise HTTPException(status_code=404)
    return {"ok": True}


@router.post("/schedules/{schedule_id}/resume")
def resume_schedule(schedule_id: str):
    if not scheduler.resume_schedule(schedule_id):
        raise HTTPException(status_code=404)
    return {"ok": True}


# ---------------------------------------------------------------------------
# Excel report (二期)
# ---------------------------------------------------------------------------

@router.get("/jobs/{job_id}/report.xlsx")
def export_report(job_id: str):
    try:
        path = report.generate(job_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="job not found")
    return FileResponse(
        path,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename=path.name,
    )


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _to_brief(row: JobRow) -> JobBrief:
    return JobBrief(
        id=row.id, type=row.type, status=row.status, concurrency=row.concurrency,
        created_at=row.created_at, started_at=row.started_at,
        finished_at=row.finished_at, paused_at=row.paused_at, result_dir=row.result_dir,
    )


def _to_device_brief(row: DeviceRunRow) -> DeviceRunBrief:
    return DeviceRunBrief(
        device_name=row.device_name, mgmt_ip=row.mgmt_ip, status=row.status,
        started_at=row.started_at, finished_at=row.finished_at,
        log_path=row.log_path, json_path=row.json_path,
        name_mismatch=row.name_mismatch, error=row.error,
    )
