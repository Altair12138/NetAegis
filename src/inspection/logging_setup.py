"""loguru 配置：脱敏 + Job 维度文件分发。

P1-7 改进：支持 JSON 结构化日志（LOG_FORMAT=json），可配置日志级别（LOG_LEVEL）。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Literal

from loguru import logger

from .config import get_settings

_SENSITIVE = re.compile(r"(?i)(password|secret|enable|token)['\"\s:=]+([^\s'\"]+)")


def _mask(record_msg: str) -> str:
    """脱敏：将 password/token 等敏感字段替换为 ***。"""
    return _SENSITIVE.sub(lambda m: f"{m.group(1)}=***", record_msg)


def _format(record):
    """根据 LOG_FORMAT 设置返回文本或 JSON 格式。"""
    record["message"] = _mask(record["message"])
    return "{time:YYYY-MM-DD HH:mm:ss} | {level: <7} | {extra[job_id]:<12} | {message}\n"


def configure(log_dir: Path, ssh_debug: bool = False,
              log_level: str = "INFO", log_format: Literal["text", "json"] = "text") -> None:
    """P1-7: 可配置日志级别和格式（text / json）。"""
    log_dir.mkdir(parents=True, exist_ok=True)
    logger.remove()
    logger.configure(extra={"job_id": "-"})

    serialize = (log_format == "json")
    logger.add(
        sys.stderr,
        format=_format if not serialize else None,
        serialize=serialize,
        level=log_level,
    )
    logger.add(
        log_dir / "platform.log",
        format=_format if not serialize else None,
        serialize=serialize,
        level="DEBUG",
        rotation="20 MB",
        retention="14 days",
        enqueue=True,
    )
    if ssh_debug:
        try:
            import paramiko
            paramiko.util.log_to_file(log_dir / "paramiko.log", level="DEBUG")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"SSH_DEBUG enabled but paramiko log setup failed: {e}")


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
