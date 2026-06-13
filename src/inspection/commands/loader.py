"""按 vendor + device_type 加载命令清单，支持按 key / tag 子集过滤。

命令清单 YAML 形态（节选）：

    commands:
      - {key: route, cmd: "display ip routing-table", tags: [routing, l3]}
      - {key: lldp,  cmd: "display lldp neighbor-information", tags: [topology]}
      - {key: bgp_summary, cmd: "...", tags: [routing, bgp], optional: true}

过滤逻辑（取并集）：
- 指定 `keys`  → 按命令 key 精确选；
- 指定 `tags`  → 命令至少匹配一个 tag 即入选；
- 两者都不指定 → 全量。
- 始终保留 `__sysname__` 探测（设备名核对必跑）。
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Iterable

import yaml

from ..models import Device, DeviceType, JobType, Vendor

_DIR = Path(__file__).parent

_FILE_MAP = {
    (Vendor.huawei, DeviceType.firewall): "huawei_firewall.yaml",
    (Vendor.h3c,    DeviceType.firewall): "h3c_firewall.yaml",
    (Vendor.h3c,    DeviceType.switch):   "h3c_switch.yaml",
    (Vendor.ruijie, DeviceType.switch):   "ruijie_switch.yaml",
}


@lru_cache(maxsize=None)
def _load(filename: str) -> dict:
    return yaml.safe_load((_DIR / filename).read_text(encoding="utf-8"))


def for_device(
    device: Device,
    job_type: JobType = JobType.inspect,
    keys: Iterable[str] | None = None,
    tags: Iterable[str] | None = None,
) -> dict:
    """返回 {sysname_cmd, commands:[{key, cmd, tags?, optional?}], backup_cmd}."""
    fname = _FILE_MAP.get((device.vendor, device.device_type))
    if not fname:
        raise KeyError(f"未支持的 vendor/device_type: {device.vendor}/{device.device_type}")
    spec = _load(fname)

    if job_type is JobType.backup:
        return {
            "sysname_cmd": spec["sysname_cmd"],
            "commands": [{"key": "config", "cmd": spec["backup_cmd"], "tags": ["config"]}],
        }

    cmds = spec["commands"]
    key_set = set(keys) if keys else None
    tag_set = set(tags) if tags else None

    if key_set or tag_set:
        filtered = []
        for c in cmds:
            c_tags = set(c.get("tags") or [])
            if key_set and c["key"] in key_set:
                filtered.append(c); continue
            if tag_set and (c_tags & tag_set):
                filtered.append(c); continue
        if not filtered:
            raise ValueError(
                f"过滤后命令为空 device={device.name} keys={keys} tags={tags} "
                f"（可用 keys: {[c['key'] for c in cmds]}）"
            )
        cmds = filtered

    return {
        "sysname_cmd": spec["sysname_cmd"],
        "commands": cmds,
        "backup_cmd": spec.get("backup_cmd"),
    }


def catalog() -> dict[str, dict]:
    """供前端下拉用：返回每个 vendor/device_type 支持的命令清单。"""
    out: dict[str, dict] = {}
    for (vendor, dtype), fname in _FILE_MAP.items():
        spec = _load(fname)
        out[f"{vendor.value}_{dtype.value}"] = {
            "commands": [
                {"key": c["key"], "cmd": c["cmd"], "tags": c.get("tags") or [],
                 "optional": bool(c.get("optional"))}
                for c in spec["commands"]
            ],
        }
    return out


def all_tags() -> list[str]:
    tags: set[str] = set()
    for fname in _FILE_MAP.values():
        for c in _load(fname)["commands"]:
            tags.update(c.get("tags") or [])
    return sorted(tags)
