"""设备命名解析与一致性校验。

约定：城市-区域-房间号-机架位U位-厂商型号-角色-编号
其中末段（编号）允许多个连字符，例如：
    SH-MH-401-C11U3-H3CS9825-G0-A04008
    SH-MH-601-C02U43-H3CS6850-C0-IntCPU-11212

P3-17: 统一 HOSTNAME_RE 供 parser.py / verify_name.py 复用。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_PATTERN = re.compile(
    r"^(?P<city>[A-Z]+)-"
    r"(?P<area>[A-Z0-9]+)-"
    r"(?P<room>\d+)-"
    r"(?P<rack>[A-Z]\d+U\d+)-"
    r"(?P<vendor_model>[A-Z0-9]+)-"
    r"(?P<role>[A-Z0-9]+)-"
    r"(?P<suffix>.+)$"
)

# P3-17: 统一 hostname 提取正则，避免 parser.py 和 verify_name.py 重复定义。
HOSTNAME_RE = re.compile(r"(?:sysname|hostname)\s+(\S+)", re.IGNORECASE)

_VENDOR_PREFIX = {
    "HW": "huawei",
    "H3C": "h3c",
    "RJ": "ruijie",
}


@dataclass
class ParsedName:
    city: str
    area: str
    room: str
    rack: str
    vendor_model: str
    role: str
    suffix: str

    @property
    def vendor(self) -> str | None:
        for prefix, vendor in _VENDOR_PREFIX.items():
            if self.vendor_model.startswith(prefix):
                return vendor
        return None

    @property
    def model(self) -> str | None:
        for prefix in _VENDOR_PREFIX:
            if self.vendor_model.startswith(prefix):
                return self.vendor_model[len(prefix):]
        return None


def parse(name: str) -> ParsedName | None:
    m = _PATTERN.match(name.strip())
    if not m:
        return None
    return ParsedName(**m.groupdict())


def is_same_hostname(expected: str, actual: str) -> bool:
    """Netmiko 取到的 hostname 经常被截断或大小写不一致，做温和比较。"""
    if not actual:
        return False
    a = expected.strip().lower()
    b = actual.strip().lower()
    return a == b or a.startswith(b) or b.startswith(a)
