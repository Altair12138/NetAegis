"""Inventory 抽象接口：未来 CMDB 适配只需实现 fetch()."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable

from ..models import Device


class InventorySource(ABC):
    @abstractmethod
    def fetch(self, **filters) -> Iterable[Device]:
        ...
