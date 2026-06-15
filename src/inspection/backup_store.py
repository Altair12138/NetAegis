"""配置备份的版本化存储 + 差异比对 + 保留策略。

布局
----
    backups/
        <device_name>/
            20260530T120000Z_<sha256_8>.cfg       # 华三/华为
            20260601T080000Z_<sha256_8>.text      # 锐捷
            latest.cfg / latest.text             # 拷贝指向最新版本
            <ts>_<sha256_8>.cfg.diff              # 与上一版有差异时生成的 diff 文件

SHA256 去重 + 保留策略
----------------------
保存前算整文件 SHA256；若与该设备最新一版相同则跳过新建文件，仅更新 last_seen_at。
每设备最多保留最近 5 次备份，超出时自动删除旧文件及数据库记录。

配置文件扩展名：
- 华三 (h3c) / 华为 (huawei) → .cfg
- 锐捷 (ruijie) → .text
"""

from __future__ import annotations

import hashlib
import re
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
    vendor: Mapped[str] = mapped_column(String, default="")  # 用于确定文件扩展名


def _backups_root() -> Path:
    root = Path(get_settings().result_dir).parent / "backups"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _ensure_tables() -> None:
    Base.metadata.create_all(engine())
    # 迁移：将旧 md5 列重命名为 sha256（如果还存在）
    with engine().connect() as conn:
        # 检查旧列是否存在
        cols = [row[1] for row in conn.exec_driver_sql(
            "PRAGMA table_info('backups')"
        ).fetchall()]
        if "md5" in cols and "sha256" not in cols:
            conn.exec_driver_sql("ALTER TABLE backups RENAME COLUMN md5 TO sha256")
            conn.commit()


def _ext_for(vendor: str) -> str:
    """华三/华为 → .cfg，锐捷 → .text。"""
    if vendor in ("h3c", "huawei"):
        return ".cfg"
    if vendor == "ruijie":
        return ".text"
    return ".cfg"  # 默认


# diff 噪声过滤：时间戳替换 + 按 vendor 跳过配置 header
_TIMESTAMP_RE = re.compile(
    r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:[+-]\d{2}:\d{2})?"
)

# 各 vendor 配置正文的起始标记（只对比标记之后的内容）
_HEADER_START_MARKERS: dict[str, re.Pattern] = {
    "huawei": re.compile(r"^!Software Version", re.MULTILINE),
    "h3c":    re.compile(r"^\s*version\s+\d", re.MULTILINE),
    "ruijie": re.compile(r"^!\s*[Ss]oftware [Vv]ersion", re.MULTILINE),
}


def _prepare_for_diff(text: str, vendor: str = "") -> str:
    """规范化配置文本：跳过 header 前导内容 + 替换时间戳为占位符。"""
    # 1) 按 vendor 跳过 header 前导行（如 Huawei 的时间戳、备注等）
    if vendor and vendor in _HEADER_START_MARKERS:
        m = _HEADER_START_MARKERS[vendor].search(text)
        if m:
            text = text[m.start():]
    # 2) 替换时间戳
    text = _TIMESTAMP_RE.sub("<TS>", text)
    return text


def save(device_name: str, config_text: str, vendor: str = "", job_id: str = "") -> dict:
    """保存一份配置文本。SHA256 去重；同 SHA256 仅刷新 last_seen_at。
    每设备保留最近 5 次备份，超出删除旧文件+记录。
    若与上一版内容不同，生成 .diff 文件。

    返回：{id, sha256, path, diff_path, created, deduped, changed}
    """
    _ensure_tables()
    if not config_text:
        raise ValueError("empty config text")

    # 基于规范化文本计算 SHA256，过滤时间戳/header 噪声，确保去重不受时间戳影响
    normalized = _prepare_for_diff(config_text, vendor)
    sha256 = hashlib.sha256(normalized.encode("utf-8", "ignore")).hexdigest()
    now = datetime.now()
    ext = _ext_for(vendor)

    with session() as s:
        latest = s.execute(
            select(BackupRow).where(BackupRow.device_name == device_name)
            .order_by(BackupRow.created_at.desc())
        ).scalars().first()

        # SHA256 去重：与最新一版相同且文件存在则仅刷新时间戳
        if latest and latest.sha256 == sha256:
            if Path(latest.path).exists():
                latest.last_seen_at = now
                s.commit()
                return {"id": latest.id, "sha256": sha256, "path": latest.path,
                        "created": False, "deduped": True, "changed": False,
                        "diff_path": None}
            # 文件已丢失，重新创建

        dev_dir = _backups_root() / device_name
        dev_dir.mkdir(parents=True, exist_ok=True)
        ts = now.strftime("%Y%m%dT%H%M%SZ")
        path = dev_dir / f"{ts}_{sha256[:8]}{ext}"
        path.write_text(config_text, encoding="utf-8")

        # latest 拷贝
        latest_path = dev_dir / f"latest{ext}"
        latest_path.write_text(config_text, encoding="utf-8")

        # --- 对比上一版，生成 diff ---
        diff_path = None
        changed = False
        if latest is not None:
            try:
                prev_text = _prepare_for_diff(
                    Path(latest.path).read_text(encoding="utf-8"), vendor
                ).splitlines()
                curr_text = _prepare_for_diff(config_text, vendor).splitlines()
                diff_lines = list(unified_diff(
                    prev_text, curr_text,
                    fromfile=f"{device_name}@v{latest.id}",
                    tofile=f"{device_name}@{ts}",
                    lineterm="",
                ))
                if diff_lines:
                    changed = True
                    diff_file = dev_dir / f"{ts}_{sha256[:8]}{ext}.diff"
                    diff_file.write_text("\n".join(diff_lines), encoding="utf-8")
                    diff_path = str(diff_file)
            except Exception:
                pass  # diff 失败不影响保存

        row = BackupRow(
            device_name=device_name, sha256=sha256, path=str(path),
            size=len(config_text), created_at=now, last_seen_at=now,
            job_id=job_id, vendor=vendor,
        )
        s.add(row); s.commit()

        # --- 保留策略：仅保留最近 5 次 ---
        all_backups = s.execute(
            select(BackupRow).where(BackupRow.device_name == device_name)
            .order_by(BackupRow.created_at.desc())
        ).scalars().all()
        if len(all_backups) > 5:
            for stale in all_backups[5:]:
                stale_path = Path(stale.path)
                if stale_path.exists():
                    stale_path.unlink()
                s.delete(stale)
            s.commit()

        return {"id": row.id, "sha256": sha256, "path": str(path),
                "created": True, "deduped": False, "changed": changed,
                "diff_path": diff_path}


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
        a = next((r for r in rows if r.created_at < b.created_at), None)
    else:
        b, a = rows[0], rows[1]

    if not a or not b:
        return {"changed": False, "reason": "backup not found"}

    vendor = a.vendor or b.vendor or ""
    a_text = _prepare_for_diff(
        Path(a.path).read_text(encoding="utf-8"), vendor
    ).splitlines()
    b_text = _prepare_for_diff(
        Path(b.path).read_text(encoding="utf-8"), vendor
    ).splitlines()

    diff_lines = list(unified_diff(
        a_text, b_text,
        fromfile=f"{device_name}@v{a.id}",
        tofile=f"{device_name}@v{b.id}",
        lineterm="",
    ))
    added = sum(1 for l in diff_lines if l.startswith("+") and not l.startswith("+++"))
    removed = sum(1 for l in diff_lines if l.startswith("-") and not l.startswith("---"))
    return {
        "device": device_name,
        "from": {"id": a.id, "created_at": a.created_at.isoformat(), "sha256": a.sha256[:8]},
        "to":   {"id": b.id, "created_at": b.created_at.isoformat(), "sha256": b.sha256[:8]},
        "added_lines": added, "removed_lines": removed,
        "changed": bool(diff_lines),
        "diff": "\n".join(diff_lines),
    }
