"""二期：将原始命令输出解析为结构化 JSON。

策略
----
1. 优先 ntc-templates（TextFSM），通过 (platform, command) 查模板；
2. 模板缺失或解析失败，回落到内置正则兜底（兜底覆盖常用 4 类：
   version / hostname / interface_brief / lldp）；
3. 仍无法解析则返回 {"raw_kept": true}，保留原文不丢数据。

P3-22: ParsedResult 联合类型，替换裸 Any。
"""

from __future__ import annotations

import re
from typing import Any, TypedDict, Union


class VersionInfo(TypedDict, total=False):
    version: str
    uptime: str


class HostnameInfo(TypedDict):
    hostname: str


class InterfaceBriefRow(TypedDict):
    name: str
    admin_or_oper_1: str
    admin_or_oper_2: str
    raw: str


class LLDPNeighbor(TypedDict, total=False):
    local_intf: str
    neighbor_device: str
    neighbor_port: str


class RawKept(TypedDict):
    raw_kept: bool


# P3-22: 类型化的解析结果。
ParsedResult = Union[
    VersionInfo,
    HostnameInfo,
    list[InterfaceBriefRow],
    list[LLDPNeighbor],
    RawKept,
    list[dict[str, Any]],  # ntc-templates 返回格式不可控，保持宽松
]

from __future__ import annotations

import re
from typing import Any

try:
    from ntc_templates.parse import parse_output  # type: ignore
except Exception:  # pragma: no cover - ntc-templates 未安装时降级
    parse_output = None


# (vendor, device_type) → ntc-templates platform
_PLATFORM_MAP = {
    ("huawei", "firewall"): "huawei_vrp",
    ("h3c",    "firewall"): "hp_comware",
    ("h3c",    "switch"):   "hp_comware",
    ("ruijie", "switch"):   "ruijie_os",
}

# 命令归一化：同一类别在不同厂商写法差异较大，统一映射到 ntc-templates 习惯的写法。
# key = (platform, command_lower) → ntc-templates command
_COMMAND_ALIAS = {
    # 华三/华为常见
    ("hp_comware", "display version"): "display version",
    ("hp_comware", "display interface brief"): "display interface brief",
    ("hp_comware", "display ip routing-table"): "display ip routing-table",
    ("hp_comware", "display lldp neighbor-information"): "display lldp neighbor-information",
    ("hp_comware", "display arp"): "display arp",
    ("hp_comware", "display mac-address"): "display mac-address",
    # 华为
    ("huawei_vrp", "display version"): "display version",
    ("huawei_vrp", "display interface brief"): "display interface brief",
    ("huawei_vrp", "display ip routing-table"): "display ip routing-table",
    ("huawei_vrp", "display lldp neighbor brief"): "display lldp neighbor brief",
    ("huawei_vrp", "display arp"): "display arp",
    # 锐捷
    ("ruijie_os", "show version"): "show version",
    ("ruijie_os", "show interface status"): "show interfaces status",
    ("ruijie_os", "show ip route"): "show ip route",
    ("ruijie_os", "show lldp neighbors"): "show lldp neighbors",
    ("ruijie_os", "show arp"): "show arp",
}


def parse(vendor: str, device_type: str, command: str, raw: str) -> ParsedResult | None:
    """主入口。返回结构化结果或 None（拿不到时调用方自行降级）。"""
    if not raw:
        return None
    platform = _PLATFORM_MAP.get((vendor, device_type))
    if not platform:
        return None

    cmd_norm = _COMMAND_ALIAS.get((platform, command.strip().lower()), command)

    # 1) ntc-templates
    if parse_output is not None:
        try:
            data = parse_output(platform=platform, command=cmd_norm, data=raw)
            if data:
                return data
        except Exception:  # noqa: BLE001 - 模板缺失或解析异常都向兜底退
            pass

    # 2) 内置兜底
    fallback = _fallback(command, raw)
    if fallback is not None:
        return fallback

    return {"raw_kept": True}


# ---------------------------------------------------------------------------
# 内置兜底解析（仅覆盖最常用、跨厂商通用的几个）
# ---------------------------------------------------------------------------

from ..naming import HOSTNAME_RE

# For backward compatibility, keep as alias.
_RE_HOSTNAME = HOSTNAME_RE
_RE_VERSION_VRP = re.compile(r"VRP\s+\(R\)\s+software,\s+Version\s+([\w\.\-]+)", re.I)
_RE_VERSION_COMWARE = re.compile(r"Comware\s+Software,\s+Version\s+([\w\.\-]+)", re.I)
_RE_VERSION_RGOS = re.compile(r"(?:RGOS|RG-NOS)\s+(?:Software\s+,?\s+)?Version\s+([\w\.\(\)\-]+)", re.I)
_RE_UPTIME = re.compile(r"uptime\s+is\s+(.+)", re.I)


def _fallback(command: str, raw: str) -> Any | None:
    c = command.strip().lower()
    if "version" in c:
        return _parse_version(raw)
    if "sysname" in c or "hostname" in c:
        m = _RE_HOSTNAME.search(raw)
        return {"hostname": m.group(1)} if m else None
    if "interface brief" in c or "interface status" in c:
        return _parse_intf_brief(raw)
    if "lldp" in c:
        return _parse_lldp(raw)
    return None


def _parse_version(raw: str) -> dict:
    out: dict = {}
    for re_ in (_RE_VERSION_VRP, _RE_VERSION_COMWARE, _RE_VERSION_RGOS):
        m = re_.search(raw)
        if m:
            out["version"] = m.group(1)
            break
    m = _RE_UPTIME.search(raw)
    if m:
        out["uptime"] = m.group(1).strip().rstrip(".")
    return out or {"raw_kept": True}


def _parse_intf_brief(raw: str) -> list[dict]:
    """提取 (name, admin, oper, desc?) 的近似结构，宽松匹配多厂商表头。"""
    rows: list[dict] = []
    lines = raw.splitlines()
    # 跳过表头：找到第一行类似 "Interface ... " 之后开始
    started = False
    for line in lines:
        s = line.strip()
        if not s:
            continue
        if not started:
            low = s.lower()
            if low.startswith(("interface", "port")) and "name" not in low.split()[0]:
                started = True
                continue
        else:
            parts = s.split()
            if len(parts) >= 3:
                rows.append({
                    "name": parts[0],
                    "admin_or_oper_1": parts[1],
                    "admin_or_oper_2": parts[2],
                    "raw": s,
                })
    return rows


def _parse_lldp(raw: str) -> list[dict]:
    """提取 (local_intf, neighbor_device, neighbor_port) 的近似结构。"""
    neighbors: list[dict] = []
    cur: dict = {}
    for line in raw.splitlines():
        s = line.strip()
        if not s:
            if cur:
                neighbors.append(cur); cur = {}
            continue
        low = s.lower()
        if low.startswith(("local interface", "local intf", "interface index")):
            if cur:
                neighbors.append(cur); cur = {}
            cur["local_intf"] = s.split(":", 1)[-1].strip()
        elif low.startswith(("system name", "system name:", "device id")):
            cur["neighbor_device"] = s.split(":", 1)[-1].strip()
        elif low.startswith(("port id", "neighbor port", "remote port")):
            cur["neighbor_port"] = s.split(":", 1)[-1].strip()
    if cur:
        neighbors.append(cur)
    return neighbors
