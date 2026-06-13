"""CSV 数据源。"""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Iterator

from loguru import logger
from pydantic import ValidationError

from ..config import get_settings
from ..models import Device
from ..naming import parse as parse_name
from .base import InventorySource


class CSVInventorySource(InventorySource):
    def __init__(self, path: str | Path) -> None:
        p = Path(path)
        if not p.is_absolute():
            settings = get_settings()
            # P3: 复用 config._PROJECT_ROOT 进行路径解析，避免 parents[3] 硬编码。
            p = settings.base_dir / p
        self.path = p
        self.errors: list[dict] = []

    def fetch(self, **filters) -> Iterator[Device]:
        if not self.path.exists():
            raise FileNotFoundError(self.path)
        self.errors = []
        with self.path.open(newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for line_no, row in enumerate(reader, start=2):
                row = {k.strip(): (v or "").strip() for k, v in row.items()}
                # 命名兜底解析：CSV 缺 vendor/model 时尝试从 name 推断
                parsed = parse_name(row.get("name", ""))
                if parsed:
                    row.setdefault("vendor", parsed.vendor or "")
                    if not row.get("model"):
                        row["model"] = parsed.model or ""
                try:
                    dev = Device(**row)
                except ValidationError as e:
                    self.errors.append({"line": line_no, "row": row, "error": str(e)})
                    logger.warning(f"CSV 行解析失败 line={line_no} row={row!r} err={e}")
                    continue
                if _match(dev, filters):
                    yield dev


def _match(dev: Device, filters: dict) -> bool:
    for k, v in (filters or {}).items():
        if v in (None, ""):
            continue
        val = getattr(dev, k, None)
        # enum 与字符串都支持
        val = getattr(val, "value", val)
        if str(val) != str(v):
            return False
    return True
