"""loguru 配置：脱敏 + Job 维度文件分发。"""

from __future__ import annotations

import re
import sys
from pathlib import Path

from loguru import logger

_SENSITIVE = re.compile(r"(?i)(password|secret|enable|token)['\"\s:=]+([^\s'\"]+)")


def _mask(record_msg: str) -> str:
    return _SENSITIVE.sub(lambda m: f"{m.group(1)}=***", record_msg)


def _format(record):
    record["message"] = _mask(record["message"])
    return "{time:YYYY-MM-DD HH:mm:ss} | {level: <7} | {extra[job_id]:<12} | {message}\n"


def configure(log_dir: Path, ssh_debug: bool = False) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    logger.remove()
    logger.configure(extra={"job_id": "-"})
    logger.add(sys.stderr, format=_format, level="INFO")
    logger.add(
        log_dir / "platform.log",
        format=_format,
        level="DEBUG",
        rotation="20 MB",
        retention="14 days",
        enqueue=True,
    )
    if ssh_debug:
        try:
            import paramiko

            paramiko.util.log_to_file(log_dir / "paramiko.log", level="DEBUG")
        except Exception:
            logger.warning("SSH_DEBUG enabled but paramiko log setup failed")


def job_logger(job_id: str, log_dir: Path):
    """每个 Job 单独一份明细日志。"""
    log_dir.mkdir(parents=True, exist_ok=True)
    handler_id = logger.add(
        log_dir / f"{job_id}.log",
        format=_format,
        level="DEBUG",
        enqueue=True,
        filter=lambda rec: rec["extra"].get("job_id") == job_id,
    )
    return logger.bind(job_id=job_id), handler_id
