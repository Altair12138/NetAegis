"""从设备实际配置中抽取 hostname / sysname，与期望名比对。

P3-17: 复用 naming.HOSTNAME_RE 避免重复定义。
"""

from __future__ import annotations

from ..naming import HOSTNAME_RE


def extract_hostname(raw: str) -> str | None:
    if not raw:
        return None
    m = HOSTNAME_RE.search(raw)
    return m.group(1) if m else None
