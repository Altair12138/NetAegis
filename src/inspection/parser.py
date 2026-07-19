"""二期：将原始命令输出解析为结构化 JSON。

策略
----
1. 优先 ntc-templates（TextFSM），通过 (platform, command) 查模板；
2. 模板缺失或解析失败，回落到内置正则兜底（兜底覆盖常用 6 类：
   version / hostname / interface_brief / lldp / arp / 表格式通用解析）；
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

try:
    from ..naming import HOSTNAME_RE
except ImportError:
    from inspection.naming import HOSTNAME_RE

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
    if "arp" in c:
        return _parse_arp(raw)
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
    """提取 (local_intf, neighbor_device, neighbor_port) 的近似结构。

    支持两种输出格式：
    1. 键值对格式（H3C/Huawei）：Local Interface: X / System Name: Y
    2. 表格式（Ruijie/Cisco）：列对齐的 System Name / Local Intf / Port ID
    """
    neighbors: list[dict] = []

    # 先尝试键值对格式（H3C/Huawei 的 key: value 风格）
    # 关键区别：键值对行一定包含冒号 ":"，而表头行（如锐捷的
    # "System Name   Local Intf   Port ID ..."）不含冒号。
    cur: dict = {}
    is_kv = False
    for line in raw.splitlines():
        s = line.strip()
        if not s:
            if cur:
                neighbors.append(cur); cur = {}
            continue
        if ":" not in s:
            continue  # 无冒号 → 不是键值对，跳过
        low = s.lower()
        if low.startswith(("local interface", "local intf", "interface index")):
            if cur:
                neighbors.append(cur); cur = {}
            cur["local_intf"] = s.split(":", 1)[-1].strip()
            is_kv = True
        elif low.startswith(("system name", "system name:", "device id")):
            cur["neighbor_device"] = s.split(":", 1)[-1].strip()
            is_kv = True
        elif low.startswith(("port id", "neighbor port", "remote port")):
            cur["neighbor_port"] = s.split(":", 1)[-1].strip()
            is_kv = True

    if cur:
        neighbors.append(cur)

    if is_kv:
        return neighbors

    # 键值对无结果，尝试表格式解析
    # 典型锐捷输出：
    #   System Name                 Local Intf          Port ID                          Capability   Aging-time
    #   SH-MHZS-M504-G28U47-H3CS5570  GigabitEthernet1/0/46  ...                          B, R         1min 32sec
    return _parse_table(
        raw,
        header_patterns=["system name", "local intf", "port id"],
        column_names=["neighbor_device", "local_intf", "neighbor_port"],
    )


def _parse_arp(raw: str) -> list[dict]:
    """解析 ARP 表（跨厂商通用：show arp / display arp）。

    典型列头：IP Address / MAC Address / Type / Age / Interface / Port
    H3C 增加列：SubVlan / SubVni / Location / Gid
    """
    return _parse_table(
        raw,
        header_patterns=["ip address", "mac address"],
        column_names=["ip_address", "mac_address", "type", "age", "interface", "port"],
    )


def _parse_table(
    raw: str,
    header_patterns: list[str],
    column_names: list[str],
) -> list[dict]:
    """通用表格式解析器：以 header_patterns 为锚点定位列头，按列切分数据行。

    原理：表头中同一列头内的多个词（如 "IP Address"、"System Name"）
    之间只有小空隙（1-2 空格），而不同列头之间有大空隙（3+ 空格），
    据此用正则 ``re.split(r' {3,}', ...)`` 切出列头段，得到每个列的
    起始位置，再按此位置切分数据行。

    参数
    ----
    header_patterns : 表头行中必须出现的关键词（小写），用于定位表头行和列。
    column_names : 与 header_patterns 一一对应的输出字段名。

    返回
    ----
    list[dict]，每个 dict 包含 column_names 中对应的字段值。
    """
    rows: list[dict] = []
    lines = raw.splitlines()

    # 1) 找到表头行
    header_line_idx = -1
    header_line = ""
    for i, line in enumerate(lines):
        low = line.lower()
        if all(p in low for p in header_patterns):
            header_line_idx = i
            header_line = line
            break

    if header_line_idx < 0:
        return rows

    # 2) 识别表头中的列头段
    #    同一列头内的词之间只有 1-2 空格，不同列头之间 3+ 空格。
    header_segments = re.split(r' {3,}', header_line.strip())

    # 3) 每个列头段在表头行中的起始位置
    col_starts: list[int] = []
    pos = 0
    for seg in header_segments:
        first_word = seg.split()[0]
        idx = header_line.find(first_word, pos)
        if idx >= 0:
            col_starts.append(idx)
            pos = idx + len(seg)
        else:
            pos += len(seg) + 3

    # 4) 将 header_patterns 映射到列头段
    low_header = header_line.lower()
    mapped: list[tuple[int, str]] = []
    for pattern, col_name in zip(header_patterns, column_names):
        pat_pos = low_header.find(pattern)
        if pat_pos < 0:
            continue
        # 找到 pattern 所属的列头段（其 col_start <= pat_pos）
        best = None
        for cs in col_starts:
            if cs <= pat_pos:
                best = cs
        if best is not None:
            mapped.append((best, col_name))

    if not mapped:
        return rows

    mapped.sort(key=lambda x: x[0])
    mapped_starts = [m[0] for m in mapped]
    mapped_names = [m[1] for m in mapped]

    # 5) 逐行解析数据行
    for line in lines[header_line_idx + 1:]:
        stripped = line.rstrip()
        if not stripped.strip():
            continue
        if stripped.strip().startswith("-") and set(stripped.strip()) <= {"-", " ", ":"}:
            continue
        low = stripped.lower()
        if low.startswith(("total", "summary", "*")):
            continue

        entry: dict = {}
        for idx, (start, name) in enumerate(zip(mapped_starts, mapped_names)):
            # 列结束位置 = 下一个列头段的起始位置（无论是否映射）
            end = len(stripped)
            for cs in col_starts:
                if cs > start:
                    end = cs
                    break
            val = stripped[start:end].strip() if start < len(stripped) else ""
            entry[name] = val
        if entry:
            # 续行检测：只有第一列有值、其余列全空 → 拼接到上一行的第一列
            # 典型场景：锐捷 LLDP 设备名过长被终端换行
            first_col = mapped_names[0]
            if (
                rows
                and entry.get(first_col)
                and not any(v for k, v in entry.items() if k != first_col and v)
            ):
                rows[-1][first_col] = rows[-1].get(first_col, "") + entry[first_col]
            else:
                rows.append(entry)

    return rows
