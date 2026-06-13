"""配置备份的版本化存储 + 差异比对。

布局
----
    backups/
        <device_name>/
            20260530T120000Z_<sha8>.cfg
            20260601T080000Z_<sha8>.cfg
            latest.cfg            # 软链/拷贝，指向最新版本，便于运维直接 cat

SHA 去重
--------
保存前算整文件 SHA256；若与该设备最新一版相同则跳过新建文件，仅更新 SQLite
last_seen_at，避免长期巡检产生大量"零变化"备份。
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from difflib import unified_diff
from pathlib import Path

from sqlalchemy import DateTime, Integer, String, select
from sqlalchemy.orm import Mapped, mapped_column

from .config import get_settings
from .db import Base, engine, session


class BackupRow(Base):
    __tablename__ = "backups"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    device_name: Mapped[str] = mapped_column(String, index=True)
    sha256: Mapped[str] = mapped_column(String, index=True)
    path: Mapped[str] = mapped_column(String)
    size: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime)
    job_id: Mapped[str] = mapped_column(String, default="")


def _backups_root() -> Path:
    root = Path(get_settings().result_dir).parent / "backups"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _ensure_tables() -> None:
    Base.metadata.create_all(engine())


def save(device_name: str, config_text: str, job_id: str = "") -> dict:
    """保存一份配置文本。若与最新一版 SHA 相同则不新建文件、仅刷新 last_seen_at。

    返回：{id, sha256, path, created, deduped}
    """
    _ensure_tables()
    if not config_text:
        raise ValueError("empty config text")

    sha = hashlib.sha256(config_text.encode("utf-8", "ignore")).hexdigest()
    now = datetime.now()

    with session() as s:
        latest = s.execute(
            select(BackupRow).where(BackupRow.device_name == device_name)
            .order_by(BackupRow.created_at.desc())
        ).scalars().first()

        if latest and latest.sha256 == sha:
            latest.last_seen_at = now
            s.commit()
            return {"id": latest.id, "sha256": sha, "path": latest.path,
                    "created": False, "deduped": True}

        dev_dir = _backups_root() / device_name
        dev_dir.mkdir(parents=True, exist_ok=True)
        ts = now.strftime("%Y%m%dT%H%M%SZ")
        path = dev_dir / f"{ts}_{sha[:8]}.cfg"
        path.write_text(config_text, encoding="utf-8")

        # latest.cfg 拷贝（避免软链跨平台问题）
        (dev_dir / "latest.cfg").write_text(config_text, encoding="utf-8")

        row = BackupRow(
            device_name=device_name, sha256=sha, path=str(path),
            size=len(config_text), created_at=now, last_seen_at=now, job_id=job_id,
        )
        s.add(row); s.commit()
        return {"id": row.id, "sha256": sha, "path": str(path),
                "created": True, "deduped": False}


def list_for(device_name: str, limit: int = 50) -> list[BackupRow]:
    with session() as s:
        return list(s.execute(
            select(BackupRow).where(BackupRow.device_name == device_name)
            .order_by(BackupRow.created_at.desc()).limit(limit)
        ).scalars())


def get(backup_id: int) -> BackupRow | None:
    with session() as s:
        return s.get(BackupRow, backup_id)


def diff(device_name: str, a_id: int | None = None, b_id: int | None = None) -> dict:
    """生成 unified diff。

    - 不传 a/b：取最近两版（a=次新，b=最新）。
    - 仅传 b：a 自动取 b 的前一版。
    """
    with session() as s:
        rows = list(s.execute(
            select(BackupRow).where(BackupRow.device_name == device_name)
            .order_by(BackupRow.created_at.desc()).limit(20)
        ).scalars())
    if len(rows) < 2 and not (a_id and b_id):
        return {"changed": False, "reason": "fewer than 2 backups", "device": device_name}

    by_id = {r.id: r for r in rows}
    if a_id and b_id:
        a = by_id.get(a_id) or get(a_id)
        b = by_id.get(b_id) or get(b_id)
    elif b_id:
        b = by_id.get(b_id) or get(b_id)
        # 找 b 之前最近一版
        a = next((r for r in rows if r.created_at < b.created_at), None)
    else:
        b, a = rows[0], rows[1]

    if not a or not b:
        return {"changed": False, "reason": "backup not found"}

    a_text = Path(a.path).read_text(encoding="utf-8").splitlines()
    b_text = Path(b.path).read_text(encoding="utf-8").splitlines()

    diff_lines = list(unified_diff(
        a_text, b_text,
        fromfile=f"{device_name}@{a.created_at.isoformat()}",
        tofile=f"{device_name}@{b.created_at.isoformat()}",
        lineterm="",
    ))
    added = sum(1 for l in diff_lines if l.startswith("+") and not l.startswith("+++"))
    removed = sum(1 for l in diff_lines if l.startswith("-") and not l.startswith("---"))
    return {
        "device": device_name,
        "from": {"id": a.id, "created_at": a.created_at.isoformat(), "sha": a.sha256[:8]},
        "to":   {"id": b.id, "created_at": b.created_at.isoformat(), "sha": b.sha256[:8]},
        "added_lines": added, "removed_lines": removed,
        "changed": bool(diff_lines),
        "diff": "\n".join(diff_lines),
    }
