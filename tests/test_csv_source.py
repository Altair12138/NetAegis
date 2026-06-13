"""P3-23: CSVInventorySource tests."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from inspection.inventory.csv_source import CSVInventorySource


@pytest.fixture
def sample_csv() -> Path:
    content = (
        "name,mgmt_ip,device_type,vendor,model,credential_profile\n"
        "SH-MH-401-C11U3-H3CS9825-G0-A04008,192.168.1.1,switch,h3c,S9825,default\n"
        "SH-MH-601-A04U4-H3CM9K-Int-FW-15001,10.0.0.1,firewall,h3c,M9K,\n"
        "BAD-DEVICE,1.2.3.4,switch,,,\n"
        "BAD-IP,not-an-ip,switch,h3c,,\n"
    )
    with tempfile.NamedTemporaryFile(mode="w", suffix=".csv", delete=False, encoding="utf-8") as f:
        f.write(content)
        return Path(f.name)


def test_fetch_all(sample_csv: Path):
    src = CSVInventorySource(sample_csv)
    devices = list(src.fetch())
    assert len(devices) == 2
    assert devices[0].name == "SH-MH-401-C11U3-H3CS9825-G0-A04008"
    assert str(devices[0].mgmt_ip) == "192.168.1.1"
    assert devices[0].vendor.value == "h3c"


def test_filter_vendor(sample_csv: Path):
    src = CSVInventorySource(sample_csv)
    devices = list(src.fetch(vendor="h3c"))
    assert len(devices) == 2


def test_filter_device_type(sample_csv: Path):
    src = CSVInventorySource(sample_csv)
    devices = list(src.fetch(device_type="firewall"))
    assert len(devices) == 1
    assert devices[0].device_type.value == "firewall"


def test_parse_errors(sample_csv: Path):
    src = CSVInventorySource(sample_csv)
    list(src.fetch())
    assert len(src.errors) == 2


def test_nonexistent_file():
    src = CSVInventorySource("/nonexistent/path.csv")
    with pytest.raises(FileNotFoundError):
        list(src.fetch())


def test_name_fallback(sample_csv: Path):
    src = CSVInventorySource(sample_csv)
    devices = list(src.fetch())
    assert all(d.vendor.value for d in devices)
