"""API 输入输出 schema（与 models.Job 区分，避免序列化耦合）。

P3-16: 使用 get_settings().default_inventory_path 消除硬编码。
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from ..config import get_settings

_DEFAULT_INVENTORY = get_settings().default_inventory_path


class CreateJobRequest(BaseModel):
    type: Literal["inspect", "backup"] = "inspect"
    inventory_source: Literal["csv", "cmdb"] = "csv"
    inventory_path: str | None = Field(default_factory=lambda: _DEFAULT_INVENTORY)
    device_filter: dict | None = None
    concurrency: int = Field(default=20, ge=1, le=200)
    credential_profile: str = "default"
    command_keys: list[str] | None = None
    command_tags: list[str] | None = None
    enable_parse: bool = False
    auto_backup: bool = True
    device_save: bool | None = None


class ScheduleCreate(BaseModel):
    id: str
    trigger_type: Literal["cron", "interval", "date"]
    trigger_args: dict
    inventory_path: str = Field(default_factory=lambda: _DEFAULT_INVENTORY)
    job_type: Literal["inspect", "backup"] = "inspect"
    concurrency: int = 20
    credential_profile: str = "default"
    command_keys: list[str] | None = None
    command_tags: list[str] | None = None
    device_filter: dict | None = None


class InventoryPreviewRequest(BaseModel):
    inventory_source: Literal["csv", "cmdb"] = "csv"
    inventory_path: str | None = Field(default_factory=lambda: _DEFAULT_INVENTORY)
    device_filter: dict | None = None


class InventoryPreviewResponse(BaseModel):
    total: int
    valid: int
    invalid: int
    errors: list[dict]
    sample_devices: list[dict]


class JobBrief(BaseModel):
    id: str
    type: str
    status: str
    concurrency: int
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    paused_at: datetime | None
    result_dir: str


class DeviceRunBrief(BaseModel):
    device_name: str
    mgmt_ip: str
    status: str
    started_at: datetime | None
    finished_at: datetime | None
    log_path: str | None
    json_path: str | None
    name_mismatch: bool
    error: str | None
    save_result: dict | None = None


class JobDetail(JobBrief):
    progress: dict[str, int]
    devices: list[DeviceRunBrief]
