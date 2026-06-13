"""P3-23: backup_store tests (save / list / diff with SHA dedup)."""

from __future__ import annotations

import uuid

import pytest

from inspection.backup_store import diff, list_for, save


def test_save_and_dedup():
    device = f"TEST-dedup-{uuid.uuid4().hex[:8]}"
    config = "hostname test-switch\ninterface GigabitEthernet0/0/1\n ip address 10.0.0.1/24\n"

    result1 = save(device, config)
    assert result1["created"] is True
    assert result1["deduped"] is False
    assert result1["sha256"]

    result2 = save(device, config)
    assert result2["deduped"] is True
    assert result2["sha256"] == result1["sha256"]


def test_save_and_list():
    device = f"TEST-list-{uuid.uuid4().hex[:8]}"
    config = "hostname test-list\n"

    save(device, config)
    rows = list_for(device, limit=10)
    assert len(rows) >= 1
    assert rows[0].device_name == device


def test_diff_no_backups():
    result = diff(f"NONEXISTENT-{uuid.uuid4().hex[:8]}")
    assert result["changed"] is False
    assert "reason" in result


def test_save_empty_raises():
    with pytest.raises(ValueError, match="empty config text"):
        save("TEST-EMPTY", "")


def test_diff_two_versions():
    device = f"TEST-diff2-{uuid.uuid4().hex[:8]}"
    save(device, "hostname v1\n")
    save(device, "hostname v2\n")

    result = diff(device)
    assert result["changed"] is True
    assert result["added_lines"] > 0 or result["removed_lines"] > 0


def test_list_limit():
    device = f"TEST-limit-{uuid.uuid4().hex[:8]}"
    for i in range(5):
        save(device, f"hostname v{i}\n")
    rows = list_for(device, limit=3)
    assert len(rows) <= 3
