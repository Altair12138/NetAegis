"""FastAPI 路由：供前端调用。"""

from __future__ import annotations

import threading
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Header, status
from fastapi.responses import FileResponse

from .. import backup_store, report, scheduler
from ..commands.loader import all_tags, catalog
from ..config import get_settings
from ..db import DeviceRunRow, JobRow, session
from ..inventory import CSVInventorySource
from ..models import JobCreate, JobType
from ..runner import controller, run_job
from .schemas import (
    CreateJobRequest,
    DeviceRunBrief,
    InventoryPreviewRequest,
    InventoryPreviewResponse,
    JobBrief,
    JobDetail,
    ScheduleCreate,
)

router = APIRouter(prefix="/api", tags=["inspection"])


def _auth(authorization: str = Header(default="")):
    token = get_settings().api_token
    if not token:
        return  # 未配置则放行（开发模式）
    if authorization != f"Bearer {token}":
        raise HTTPException(status_code=401, detail="invalid token")


@router.get("/commands", dependencies=[Depends(_auth)])
def list_commands():
    """前端下拉数据：每个 vendor/device_type 支持的命令 key/cmd/tags，以及所有可用 tag。"""
    return {"catalog": catalog(), "tags": all_tags()}


@router.post("/inventory/preview", response_model=InventoryPreviewResponse, dependencies=[Depends(_auth)])
def preview_inventory(req: InventoryPreviewRequest):
    if req.inventory_source == "csv":
        src = CSVInventorySource(req.inventory_path or "inventory/devices.csv")
    else:
        raise HTTPException(400, "CMDB inventory 尚未实现")

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


@router.post("/jobs", response_model=JobBrief, dependencies=[Depends(_auth)])
def create_job(req: CreateJobRequest, bg: BackgroundTasks) -> JobBrief:
    if req.inventory_source == "csv":
        src = CSVInventorySource(req.inventory_path or "inventory/devices.csv")
    else:
        raise HTTPException(400, "CMDB inventory 尚未实现")

    devices = list(src.fetch(**(req.device_filter or {})))
    if not devices:
        raise HTTPException(400, "没有匹配的设备")

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
    )

    # 在后台线程中跑，避免阻塞 HTTP worker
    holder: dict[str, str] = {}

    def _go():
        jid = run_job(create, devices)
        holder["id"] = jid

    t = threading.Thread(target=_go, name="job-launcher", daemon=True)
    t.start()
    # 等待 Job 落库（runner 内部第一步就写 JobRow）
    t.join(timeout=2.0)

    # 找最新 Job
    with session() as s:
        row = s.query(JobRow).order_by(JobRow.created_at.desc()).first()
        if not row:
            raise HTTPException(500, "Job 创建失败")
        return _to_brief(row)


@router.get("/jobs", response_model=list[JobBrief], dependencies=[Depends(_auth)])
def list_jobs(status_filter: str | None = None, limit: int = 50):
    with session() as s:
        q = s.query(JobRow)
        if status_filter:
            q = q.filter(JobRow.status == status_filter)
        rows = q.order_by(JobRow.created_at.desc()).limit(limit).all()
        return [_to_brief(r) for r in rows]


@router.get("/jobs/{job_id}", response_model=JobDetail, dependencies=[Depends(_auth)])
def get_job(job_id: str):
    with session() as s:
        row = s.get(JobRow, job_id)
        if not row:
            raise HTTPException(404)
        devices = s.query(DeviceRunRow).filter(DeviceRunRow.job_id == job_id).all()
        brief = _to_brief(row)
        counts = {"queued": 0, "running": 0, "success": 0, "name_mismatch": 0,
                  "failed": 0, "skipped": 0, "total": len(devices)}
        for d in devices:
            counts[d.status] = counts.get(d.status, 0) + 1
        return JobDetail(
            **brief.model_dump(),
            progress=counts,
            devices=[_to_device_brief(d) for d in devices],
        )


@router.post("/jobs/{job_id}/pause", dependencies=[Depends(_auth)])
def pause(job_id: str):
    if not controller.pause(job_id):
        raise HTTPException(404, "job not running in this process")
    return {"ok": True}


@router.post("/jobs/{job_id}/resume", dependencies=[Depends(_auth)])
def resume(job_id: str):
    if not controller.resume(job_id):
        raise HTTPException(404)
    return {"ok": True}


@router.post("/jobs/{job_id}/cancel", dependencies=[Depends(_auth)])
def cancel(job_id: str):
    if not controller.cancel(job_id):
        raise HTTPException(404)
    return {"ok": True}


@router.get("/jobs/{job_id}/devices/{device_name}/log", dependencies=[Depends(_auth)])
def get_log(job_id: str, device_name: str):
    """下载原始命令输出 .log"""
    with session() as s:
        row = s.get(DeviceRunRow, (job_id, device_name))
        if not row or not row.log_path or not Path(row.log_path).exists():
            raise HTTPException(404, "log not found")
        return FileResponse(row.log_path, media_type="text/plain", filename=Path(row.log_path).name)


@router.get("/jobs/{job_id}/devices/{device_name}/result", dependencies=[Depends(_auth)])
def get_result(job_id: str, device_name: str):
    """下载处理后的结构化 .json（一期 parsed=None；二期填充解析结果）"""
    with session() as s:
        row = s.get(DeviceRunRow, (job_id, device_name))
        if not row or not row.json_path or not Path(row.json_path).exists():
            raise HTTPException(404, "result json not found")
        return FileResponse(row.json_path, media_type="application/json", filename=Path(row.json_path).name)


# ---------------------------------------------------------------------------
# 二期：备份 / 差异
# ---------------------------------------------------------------------------

@router.get("/devices/{device_name}/backups", dependencies=[Depends(_auth)])
def list_backups(device_name: str, limit: int = 50):
    rows = backup_store.list_for(device_name, limit=limit)
    return [
        {"id": r.id, "sha256": r.sha256, "path": r.path, "size": r.size,
         "created_at": r.created_at.isoformat(),
         "last_seen_at": r.last_seen_at.isoformat(),
         "job_id": r.job_id}
        for r in rows
    ]


@router.get("/devices/{device_name}/backups/{backup_id}", dependencies=[Depends(_auth)])
def download_backup(device_name: str, backup_id: int):
    row = backup_store.get(backup_id)
    if not row or row.device_name != device_name or not Path(row.path).exists():
        raise HTTPException(404)
    return FileResponse(row.path, media_type="text/plain", filename=Path(row.path).name)


@router.get("/devices/{device_name}/diff", dependencies=[Depends(_auth)])
def get_diff(device_name: str, a: int | None = None, b: int | None = None):
    return backup_store.diff(device_name, a_id=a, b_id=b)


# ---------------------------------------------------------------------------
# 二期：定时巡检
# ---------------------------------------------------------------------------

@router.post("/schedules", dependencies=[Depends(_auth)])
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
        raise HTTPException(400, str(e))


@router.get("/schedules", dependencies=[Depends(_auth)])
def get_schedules():
    return scheduler.list_schedules()


@router.delete("/schedules/{schedule_id}", dependencies=[Depends(_auth)])
def delete_schedule(schedule_id: str):
    if not scheduler.remove_schedule(schedule_id):
        raise HTTPException(404)
    return {"ok": True}


@router.post("/schedules/{schedule_id}/pause", dependencies=[Depends(_auth)])
def pause_schedule(schedule_id: str):
    if not scheduler.pause_schedule(schedule_id):
        raise HTTPException(404)
    return {"ok": True}


@router.post("/schedules/{schedule_id}/resume", dependencies=[Depends(_auth)])
def resume_schedule(schedule_id: str):
    if not scheduler.resume_schedule(schedule_id):
        raise HTTPException(404)
    return {"ok": True}


# ---------------------------------------------------------------------------
# 二期：Excel 报表
# ---------------------------------------------------------------------------

@router.get("/jobs/{job_id}/report.xlsx", dependencies=[Depends(_auth)])
def export_report(job_id: str):
    try:
        path = report.generate(job_id)
    except KeyError:
        raise HTTPException(404, "job not found")
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
