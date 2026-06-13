"""配置备份任务：复用 inspect_device，但只跑 backup_cmd。"""

from __future__ import annotations

from pathlib import Path

from nornir.core.task import Result, Task

from ..models import Device, JobType
from .inspect import inspect_device


def backup_device(task: Task, device: Device, result_dir: Path, cmd_timeout: int) -> Result:
    return inspect_device(
        task=task,
        device=device,
        job_type=JobType.backup,
        result_dir=result_dir,
        cmd_timeout=cmd_timeout,
    )
