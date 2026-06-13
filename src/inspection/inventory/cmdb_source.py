"""CMDB API 数据源占位实现（二期填充真实 endpoint 与认证）。"""

from __future__ import annotations

from typing import Iterator

from ..models import Device
from .base import InventorySource


class CMDBInventorySource(InventorySource):
    def __init__(self, base_url: str, token: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token

    def fetch(self, **filters) -> Iterator[Device]:  # pragma: no cover - 占位
        raise NotImplementedError(
            "CMDB 适配尚未实现。请填充 HTTP 调用、字段映射后再启用。"
        )
