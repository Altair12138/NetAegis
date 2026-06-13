"""命令行入口：uv run inspect run --inventory inventory/devices.csv

P1 改进：传递 log_level / log_format 配置；使用 default_inventory_path。
"""

from __future__ import annotations

import typer
from sqlalchemy import select

from .config import get_settings
from .db import DeviceRunRow, session
from .inventory import CSVInventorySource
from .logging_setup import configure
from .models import DeviceRunStatus, JobCreate, JobType
from .runner import run_job

app = typer.Typer(help="Network inspection platform CLI")


@app.command()
def run(
    inventory: str = typer.Option(None, help="CSV 路径（默认使用 .env 中 DEFAULT_INVENTORY_PATH）"),
    concurrency: int = typer.Option(20),
    type: JobType = typer.Option(JobType.inspect, "--type", "-t"),
    vendor: str = typer.Option(None, help="按 vendor 过滤设备"),
    device_type: str = typer.Option(None, help="按设备类型过滤设备"),
    model: str = typer.Option(None, help="按 model 过滤设备（例如 S9825）"),
    keys: str = typer.Option(None, "--keys", "-k",
                             help="逗号分隔的命令 key，仅跑这些命令，例：lldp,route"),
    tags: str = typer.Option(None, "--tags",
                             help="逗号分隔的 tag，命中任一即入选，例：topology 或 routing,health"),
):
    """触发巡检 / 备份。不指定 --keys/--tags 则跑全量。"""
    settings = get_settings()
    configure(settings.log_dir, settings.ssh_debug,
              log_level=settings.log_level, log_format=settings.log_format)
    inventory_path = inventory or settings.default_inventory_path
    src = CSVInventorySource(inventory_path)
    devices = list(src.fetch(vendor=vendor, device_type=device_type, model=model))
    if not devices:
        typer.echo("No matching devices")
        raise typer.Exit(1)
    job_id = run_job(
        JobCreate(
            type=type,
            concurrency=concurrency,
            inventory_path=inventory_path,
            command_keys=[k.strip() for k in keys.split(",")] if keys else None,
            command_tags=[t.strip() for t in tags.split(",")] if tags else None,
        ),
        devices,
    )
    with session() as s:
        rows = s.execute(
            select(DeviceRunRow.device_name, DeviceRunRow.status, DeviceRunRow.error)
            .where(DeviceRunRow.job_id == job_id)
        ).all()

    success = [n for n, st, _ in rows if st in {DeviceRunStatus.success.value, DeviceRunStatus.name_mismatch.value}]
    failed = [(n, err) for n, st, err in rows if st == DeviceRunStatus.failed.value]
    skipped = [n for n, st, _ in rows if st == DeviceRunStatus.skipped.value]

    typer.echo(f"Job {job_id} done, result dir: {settings.result_dir}/{job_id}")
    typer.echo(f"Success: {len(success)} devices")
    if success:
        typer.echo("Success list: " + ", ".join(success))
    typer.echo(f"Failed: {len(failed)} devices")
    for idx, (name, err) in enumerate(failed):
        if not err:
            typer.echo(f"  - {name}")
            continue
        lines = str(err).splitlines()
        typer.echo(f"  - {name}: {lines[0]}")
        for extra in lines[1:]:
            if extra.strip() == "":
                typer.echo("    ")
            else:
                typer.echo(f"    {extra}")
        if idx != len(failed) - 1:
            typer.echo("")
    if skipped:
        typer.echo(f"Skipped: {len(skipped)} devices: " + ", ".join(skipped))


@app.command("commands")
def list_commands(vendor_type: str = typer.Argument(None, help="e.g. h3c_switch; omit to list all")):
    """列出某 vendor_device_type 支持的命令 key 与 tag，便于 --keys / --tags 取值。"""
    from .commands.loader import all_tags, catalog
    cat = catalog()
    if vendor_type:
        if vendor_type not in cat:
            typer.echo(f"Unknown vendor_type: {vendor_type}, options: {list(cat)}")
            raise typer.Exit(1)
        for c in cat[vendor_type]["commands"]:
            typer.echo(f"{c['key']:<14}  tags={c['tags']:<24}  cmd={c['cmd']}")
    else:
        for vt, spec in cat.items():
            typer.echo(f"\n[{vt}]")
            for c in spec["commands"]:
                typer.echo(f"  {c['key']:<14}  tags={c['tags']}")
        typer.echo(f"\nAll available tags: {all_tags()}")


if __name__ == "__main__":
    app()
