"""单设备巡检任务。在 Nornir runner 中作为 Task 调用。

输出布局（每台设备两份文件，同目录并列）：

    results/<job_id>/
        <name>_<ip>.log      # 原始：所有命令的原文输出，按 ===== <cmd> ===== 分块
        <name>_<ip>.json     # 处理后：结构化（设备元信息 + 每条命令的 raw / parsed / error）
                              # 一期 parsed=None；二期由 parser.py 填充
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from loguru import logger
from netmiko import NetmikoAuthenticationException, NetmikoTimeoutException
from nornir.core.exceptions import NornirSubTaskError
from nornir.core.task import Result, Task
from nornir_netmiko.tasks import netmiko_send_command
from paramiko.ssh_exception import SSHException

from ..commands.loader import for_device
from ..models import Device, JobType
from ..naming import is_same_hostname
from .. import backup_store
from .verify_name import extract_hostname

try:
    from ..parser import parse as _parse_structured
except Exception:  # noqa: BLE001 - parser 依赖 ntc-templates，未装时降级
    _parse_structured = None


def inspect_device(
    task: Task,
    device: Device,
    job_type: JobType,
    result_dir: Path,
    cmd_timeout: int,
    enable_parse: bool = False,
    command_keys: list[str] | None = None,
    command_tags: list[str] | None = None,
    auto_backup: bool = True,
    job_id: str = "",
) -> Result:
    spec = for_device(device, job_type, keys=command_keys, tags=command_tags)
    base = result_dir / f"{device.name}_{device.mgmt_ip}"
    log_path = base.parent / f"{base.name}.log"
    json_path = base.parent / f"{base.name}.json"
    log_path.parent.mkdir(parents=True, exist_ok=True)

    raw_sections: list[str] = []
    commands_payload: list[dict[str, Any]] = []
    errors: dict[str, str] = {}
    log = logger.bind(device=device.name, ip=str(device.mgmt_ip))

    # 0) 连接预热（失败则直接返回）
    try:
        task.host.get_connection("netmiko", task.nornir.config)
    except Exception as e:  # noqa: BLE001
        code, detail = _classify_connection_error(e)
        errors["__connect__"] = f"{code}: {detail}"
        log.error(f"连接失败: {code}: {detail}")
        raw_sections.append(_section("__connect__", f"<<ERROR>> {code}: {detail}"))
        _write_outputs(
            log_path,
            json_path,
            raw_sections,
            commands_payload,
            device,
            job_type,
            actual_hostname=None,
            name_mismatch=False,
            errors=errors,
            backup_info=None,
        )
        return Result(
            host=task.host,
            result={
                "device": device.name,
                "ip": str(device.mgmt_ip),
                "log_path": str(log_path),
                "json_path": str(json_path),
                "name_mismatch": False,
                "errors": errors,
                "finished_at": datetime.now().isoformat(),
            },
            failed=True,
        )

    # 1) 设备名核对
    name_mismatch = False
    actual_hostname: str | None = None
    try:
        r = task.run(
            task=netmiko_send_command,
            command_string=spec["sysname_cmd"],
            read_timeout=cmd_timeout,
        )
        if r.failed:
            msg = _subtask_error(r)
            if _is_connection_error(r):
                code, detail = _classify_connection_error(r.exception)
                errors["__connect__"] = f"{code}: {detail}"
                log.error(f"连接失败: {code}: {detail}")
            else:
                errors["__sysname__"] = msg
                log.error(f"sysname 抓取失败: {msg}")
            raw_sections.append(_section(f"__sysname__: {spec['sysname_cmd']}", f"<<ERROR>> {msg}"))
        else:
            actual_hostname = extract_hostname(r.result)
            if actual_hostname and not is_same_hostname(device.name, actual_hostname):
                name_mismatch = True
                log.warning(f"hostname 不一致 expected={device.name} actual={actual_hostname}")
            raw_sections.append(_section(f"__sysname__: {spec['sysname_cmd']}", r.result))
    except NornirSubTaskError as e:
        msg, is_connect, code = _subtask_error_from_exception(e)
        if is_connect:
            errors["__connect__"] = f"{code}: {msg}"
            log.error(f"连接失败: {code}: {msg}")
        else:
            errors["__sysname__"] = msg
            log.error(f"sysname 抓取失败: {msg}")
        raw_sections.append(_section(f"__sysname__: {spec['sysname_cmd']}", f"<<ERROR>> {msg}"))

    if "__connect__" in errors:
        _write_outputs(log_path, json_path, raw_sections, commands_payload, device, job_type,
                       actual_hostname, name_mismatch, errors, backup_info=None)
        return Result(
            host=task.host,
            result={
                "device": device.name,
                "ip": str(device.mgmt_ip),
                "log_path": str(log_path),
                "json_path": str(json_path),
                "name_mismatch": name_mismatch,
                "errors": errors,
                "finished_at": datetime.now().isoformat(),
            },
            failed=True,
        )

    # 2) 业务命令
    for item in spec["commands"]:
        key, cmd = item["key"], item["cmd"]
        entry: dict[str, Any] = {"key": key, "cmd": cmd, "raw": None, "parsed": None, "error": None}
        try:
            r = task.run(
                task=netmiko_send_command,
                command_string=cmd,
                read_timeout=cmd_timeout,
            )
            if r.failed:
                msg = _subtask_error(r)
                entry["error"] = msg
                if _is_connection_error(r):
                    code, detail = _classify_connection_error(r.exception)
                    errors["__connect__"] = f"{code}: {detail}"
                    log.error(f"连接失败: {code}: {detail}")
                elif item.get("optional"):
                    log.info(f"可选命令失败 {cmd}: {msg}")
                else:
                    errors[key] = msg
                    log.error(f"命令失败 {cmd}: {msg}")
                raw_sections.append(_section(f"{key}: {cmd}", f"<<ERROR>> {msg}"))
            else:
                entry["raw"] = r.result
                raw_sections.append(_section(f"{key}: {cmd}", r.result))

                if enable_parse and _parse_structured is not None:
                    try:
                        entry["parsed"] = _parse_structured(
                            device.vendor.value, device.device_type.value, cmd, r.result,
                        )
                    except Exception as pe:  # noqa: BLE001
                        entry["parsed"] = None
                        entry["error"] = f"parse_failed: {pe}"
        except NornirSubTaskError as e:
            msg, is_connect, code = _subtask_error_from_exception(e)
            entry["error"] = msg
            if is_connect:
                errors["__connect__"] = f"{code}: {msg}"
                log.error(f"连接失败: {code}: {msg}")
            elif item.get("optional"):
                log.info(f"可选命令失败 {cmd}: {msg}")
            else:
                errors[key] = msg
                log.error(f"命令失败 {cmd}: {msg}")
            raw_sections.append(_section(f"{key}: {cmd}", f"<<ERROR>> {msg}"))
        commands_payload.append(entry)
        if "__connect__" in errors:
            break

    # 自动配置备份：抓到 'config' key 时入库
    backup_info: dict | None = None
    if auto_backup and "__connect__" not in errors:
        config_entry = next((c for c in commands_payload if c["key"] == "config" and c.get("raw")), None)
        if config_entry:
            try:
                backup_info = backup_store.save(device.name, config_entry["raw"], job_id=job_id)
                log.info(f"backup saved: sha={backup_info['sha256'][:8]} "
                         f"deduped={backup_info['deduped']}")
            except Exception as e:  # noqa: BLE001
                log.error(f"backup save failed: {e}")

    _write_outputs(log_path, json_path, raw_sections, commands_payload, device, job_type,
                   actual_hostname, name_mismatch, errors, backup_info)

    return Result(
        host=task.host,
        result={
            "device": device.name,
            "ip": str(device.mgmt_ip),
            "log_path": str(log_path),
            "json_path": str(json_path),
            "name_mismatch": name_mismatch,
            "errors": errors,
            "finished_at": datetime.now().isoformat(),
        },
        failed=bool(errors),
    )


def _section(title: str, body: str) -> str:
    return f"===== {title} =====\n{body}\n"


def _write_outputs(
    log_path: Path,
    json_path: Path,
    raw_sections: list[str],
    commands_payload: list[dict[str, Any]],
    device: Device,
    job_type: JobType,
    actual_hostname: str | None,
    name_mismatch: bool,
    errors: dict[str, str],
    backup_info: dict | None,
) -> None:
    log_path.write_text("\n".join(raw_sections), encoding="utf-8")
    structured = {
        "device": {
            "name": device.name,
            "mgmt_ip": str(device.mgmt_ip),
            "vendor": device.vendor.value,
            "device_type": device.device_type.value,
            "model": device.model,
        },
        "job_type": job_type.value,
        "collected_at": datetime.now().isoformat(timespec="seconds"),
        "name_check": {
            "expected": device.name,
            "actual": actual_hostname,
            "mismatch": name_mismatch,
        },
        "raw_log": log_path.name,
        "commands": commands_payload,
        "errors": errors,
        "warnings": (["name_mismatch"] if name_mismatch else []),
        "backup": backup_info,
    }
    json_path.write_text(json.dumps(structured, ensure_ascii=False, indent=2), encoding="utf-8")


def _subtask_error(result: Result) -> str:
    if result.exception is not None:
        return f"{type(result.exception).__name__}: {result.exception}"
    tb = _traceback_text(result)
    if tb:
        return _tail_trace_line(tb)
    if result.result:
        return str(result.result)
    return "unknown error"


def _subtask_error_from_exception(exc: NornirSubTaskError) -> tuple[str, bool, str]:
    result = getattr(exc, "result", None)
    if isinstance(result, Result):
        if _is_connection_error(result):
            code, detail = _classify_connection_error(result.exception)
            return detail, True, code
        tb = _traceback_text(result)
        if tb:
            code, detail = _classify_connection_error_from_traceback(tb)
            if code != "connect_failed":
                return detail, True, code
            return _tail_trace_line(tb), False, ""
        return _subtask_error(result), False, ""
    return str(exc), False, ""


def _is_connection_error(result: Result) -> bool:
    exc = result.exception
    if exc is None:
        tb = _traceback_text(result)
        return _traceback_has_connect_error(tb)
    return isinstance(exc, (NetmikoTimeoutException, NetmikoAuthenticationException, SSHException, OSError, TimeoutError))


def _classify_connection_error(exc: Exception | None) -> tuple[str, str]:
    if exc is None:
        return "connect_failed", "unknown error"
    if isinstance(exc, NetmikoAuthenticationException):
        return "auth_failed", str(exc)
    if isinstance(exc, (NetmikoTimeoutException, TimeoutError)):
        return "connect_timeout", str(exc)
    if isinstance(exc, SSHException):
        return "ssh_error", str(exc)
    if isinstance(exc, OSError):
        return "connect_error", str(exc)
    return "connect_failed", str(exc)


def _traceback_text(result: Result) -> str:
    tb = getattr(result, "traceback", None)
    return str(tb) if tb else ""


def _tail_trace_line(tb: str) -> str:
    lines = [line.strip() for line in tb.splitlines() if line.strip()]
    return lines[-1] if lines else "unknown error"


def _traceback_has_connect_error(tb: str) -> bool:
    if not tb:
        return False
    return any(
        token in tb
        for token in (
            "NetmikoTimeoutException",
            "NetmikoAuthenticationException",
            "Authentication failed",
            "SSHException",
            "TimeoutError",
        )
    )


def _classify_connection_error_from_traceback(tb: str) -> tuple[str, str]:
    if "NetmikoAuthenticationException" in tb or "Authentication failed" in tb:
        return "auth_failed", _tail_trace_line(tb)
    if "NetmikoTimeoutException" in tb or "TimeoutError" in tb:
        return "connect_timeout", _tail_trace_line(tb)
    if "SSHException" in tb:
        return "ssh_error", _tail_trace_line(tb)
    return "connect_failed", _tail_trace_line(tb)
