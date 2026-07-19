"""核心数据模型。"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from ipaddress import IPv4Address
from typing import Literal

from pydantic import BaseModel, Field, IPvAnyAddress


class DeviceType(str, Enum):
    firewall = "firewall"
    switch = "switch"


class Vendor(str, Enum):
    huawei = "huawei"
    h3c = "h3c"
    ruijie = "ruijie"


class Device(BaseModel):
    name: str
    mgmt_ip: IPvAnyAddress
    device_type: DeviceType
    vendor: Vendor
    model: str | None = None
    credential_profile: str = "default"
    port: int = 22

    @property
    def group(self) -> str:
        # 与 inventory/groups.yaml 对齐
        return f"{self.vendor.value}_{self.device_type.value}"


class JobType(str, Enum):
    inspect = "inspect"
    backup = "backup"


class JobStatus(str, Enum):
    pending = "pending"
    running = "running"
    paused = "paused"
    completed = "completed"
    failed = "failed"
    canceled = "canceled"


class DeviceRunStatus(str, Enum):
    queued = "queued"
    running = "running"
    success = "success"
    name_mismatch = "name_mismatch"
    failed = "failed"
    skipped = "skipped"


class JobCreate(BaseModel):
    type: JobType = JobType.inspect
    inventory_source: Literal["csv", "cmdb"] = "csv"
    inventory_path: str | None = None        # csv 文件路径，或 cmdb 查询参数
    device_filter: dict | None = None        # 例如 {"vendor": "h3c"}
    concurrency: int = Field(default=20, ge=1, le=200)
    credential_profile: str = "default"
    # 命令子集过滤：两者可同时给，按"并集"选；都为空则跑全部
    command_keys: list[str] | None = None       # 例: ["lldp", "route"]
    command_tags: list[str] | None = None       # 例: ["routing"] 或 ["topology"]
    enable_parse: bool = True                   # 二期：开启 ntc-templates 解析，结果填到 commands[].parsed
    auto_backup: bool = True                    # 抓到 'config' key 时自动入库到 backup_store
    device_save: bool | None = None               # 采集后在设备端执行 save；None=按 job_type 自动（backup→True，inspect→False）


class DeviceRun(BaseModel):
    device_name: str
    mgmt_ip: str
    status: DeviceRunStatus = DeviceRunStatus.queued
    started_at: datetime | None = None
    finished_at: datetime | None = None
    log_path: str | None = None
    name_mismatch: bool = False
    error: str | None = None


class Job(BaseModel):
    id: str
    type: JobType
    status: JobStatus
    concurrency: int
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    paused_at: datetime | None = None
    result_dir: str
    device_runs: list[DeviceRun] = []

    @property
    def progress(self) -> dict[str, int]:
        counts = {s.value: 0 for s in DeviceRunStatus}
        for d in self.device_runs:
            counts[d.status.value] += 1
        counts["total"] = len(self.device_runs)
        return counts
