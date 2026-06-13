"""从设备实际配置中抽取 hostname / sysname，与期望名比对。"""

from __future__ import annotations

import re

_HOSTNAME_RE = re.compile(r"(?:sysname|hostname)\s+(\S+)", re.IGNORECASE)


def extract_hostname(raw: str) -> str | None:
    if not raw:
        return None
    m = _HOSTNAME_RE.search(raw)
    return m.group(1) if m else None
