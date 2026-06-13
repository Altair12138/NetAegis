# NetAegis 自动化巡检平台 — 设计文档 v2.0

> **版本**: v0.2 (一期 + 二期)
> **维护**: 网络运维平台组
> **适用范围**: 防火墙（华为、华三）、交换机（锐捷、华三）
> **上一版本**: docs/DESIGN.md (v0.1)
> **更新日期**: 2026-06-08

---

## 变更摘要 (v0.1 → v0.2)

本次升级聚焦于 **安全性修复**、**跨进程 Job 控制**、**性能优化** 和 **代码质量改进**，共涉及 14 个源文件和 2 个新增测试文件，按优先级分为 P0/P1/P2/P3 四级共 23 项改进。

---

## 1. 架构演进

### 1.1 架构对比

```
v0.1:                              v0.2:
┌──────────┐                       ┌──────────┐
│  CLI/API │                       │  CLI/API │
└────┬─────┘                       └────┬─────┘
     │                                  │
     ▼                                  ▼
┌──────────┐                       ┌──────────┐
│  Runner  │◄── JobController      │  Runner  │◄── job_control 表 (SQLite)
│  (进程内存) │     pause/cancel      │  (DB-backed)│   跨进程 pause/cancel
└────┬─────┘                       └────┬─────┘
     │                                  │
     ▼                                  ▼
┌──────────┐                       ┌──────────┐
│  Nornir  │── 全量构建+filter     │  Nornir  │── 单设备构建
│  (O(N²)) │                       │  (O(N))  │
└──────────┘                       └──────────┘
```

核心变化：JobController 从进程内存迁移到 SQLite `job_control` 表，Nornir inventory 从全量+filter 改为单设备构建。

---

## 2. P0 — 安全性与数据正确性修复

### 2.1 跨进程 Job 控制 (P0-1)

**问题**: v0.1 中 `JobController` 使用 `threading.Event` 存储在进程内存中。多 worker 部署时，API 请求的 pause/cancel 可能落在不同进程，导致静默失败。进程重启后所有控制状态丢失。

**修改文件**:

- `db.py` — 新增 `JobControlRow` 表

```python
class JobControlRow(Base):
    __tablename__ = "job_control"
    job_id: Mapped[str] = mapped_column(String, primary_key=True)
    paused: Mapped[bool] = mapped_column(Boolean, default=False)
    canceled: Mapped[bool] = mapped_column(Boolean, default=False)
```

- `runner.py` — 移除 `JobController` 类（~87 行），新增 DB-backed 控制函数：

| 函数 | 作用 |
|------|------|
| `_set_control(job_id, paused, canceled)` | 写入/更新控制记录 |
| `_check_paused(job_id)` | 检查是否暂停（每设备执行前轮询，间隔 1s） |
| `_check_canceled(job_id)` | 检查是否取消 |
| `_delete_control(job_id)` | Job 完成后清理 |
| `pause_job / resume_job / cancel_job` | 供 routes.py 使用的公共接口 |

- `routes.py` — `from ..runner import controller` 改为 `from ..runner import cancel_job, pause_job, resume_job`

**设计要点**: 暂停检查使用轮询（1 秒间隔），而非阻塞 Event。这样每条设备执行间隙都会查询 DB，支持跨进程控制。

### 2.2 认证 Fail-Closed (P0-2)

**问题**: v0.1 中 `API_TOKEN` 为空时全放行。生产环境遗漏 `.env` 文件将导致 API 完全暴露。

**修改文件**:

- `config.py` — 新增字段

```python
environment: Literal["development", "production"] = "production"
```

- `routes.py` — `_auth()` 函数重构

```
production + API_TOKEN 为空 → 500 "API_TOKEN not configured"
development + API_TOKEN 为空 → 放行
请求 Token 不匹配 → 401 "invalid token"
```

**安全原则**: 默认行为从 fail-open 改为 fail-closed。

### 2.3 创建 Job 竞态条件修复 (P0-3)

**问题**: v0.1 中用 `daemon=True` 线程 + `t.join(timeout=2.0)` 等待 JobRow 写入，超时后按 `created_at DESC` 查询最新 row，并发时可能返回别人的 job_id。

**修改文件**:

- `routes.py` — `create_job()` 改为：

```
1. 同步生成 job_id = uuid4().hex[:12]
2. 同步写入 JobRow + DeviceRunRow（微秒级）
3. daemon 线程执行设备巡检
4. 按 job_id 精确查询返回 JobBrief
```

- `runner.py` — `run_job()` 新增 `_job_id` 参数

```python
def run_job(create: JobCreate, devices: Iterable[Device], *, _job_id: str = "") -> str:
    job_id = _job_id or uuid.uuid4().hex[:12]
    # 若 _job_id 已传入（routes.py 已写 JobRow），跳过重复写入
    if not _job_id:
        # ... 写入 JobRow + DeviceRunRow
```

### 2.4 异常静默吞修复 (P0-4)

**问题**: v0.1 中 `for _ in as_completed(futures): pass` 不调用 `f.result()`，若 `_run_one` 抛未捕获异常则被静默丢弃。

**修改文件**:

- `runner.py` — 修复为:

```python
for f in as_completed(futures):
    try:
        f.result()
    except Exception:
        pass  # _run_one 内部已捕获并记录
```

### 2.5 文件服务路径穿越防护 (P0-5)

**问题**: `get_log`/`get_result`/`download_backup` 直接从 DB 取路径传递给 `FileResponse`，无路径校验。

**修改文件**:

- `routes.py` — 新增 `_safe_file_response()`:

```python
def _safe_file_response(file_path: str, allowed_parent: Path, **kwargs) -> FileResponse:
    resolved = Path(file_path).resolve()
    if not str(resolved).startswith(str(allowed_parent.resolve())):
        logger.warning(f"path traversal attempt: {file_path}")
        raise HTTPException(status_code=403, detail="access denied")
    return FileResponse(str(resolved), **kwargs)
```

所有返回文件内容的端点均使用此函数，`allowed_parent` 分别为 `settings.result_dir` 或 `backups/` 根目录。

---

## 3. P1 — 架构改善与可观测性

### 3.1 数据库引擎线程安全与 WAL 模式 (P2-12)

**问题**: v0.1 中 `engine()` 使用无锁全局变量，多线程可能创建多个 engine。SQLite 默认 journal 模式写入性能差。

**修改文件**:

- `db.py` — 完整重构

```python
_engine = None
_lock = threading.Lock()
SessionFactory: sessionmaker | None = None

def engine():
    global _engine, SessionFactory
    if _engine is None:
        with _lock:              # 双重检查锁
            if _engine is None:
                _engine = create_engine(
                    settings.db_url,
                    future=True,
                    pool_size=settings.db_pool_size,    # 默认 5
                    max_overflow=settings.db_max_overflow,  # 默认 10
                    pool_pre_ping=True,
                )
                Base.metadata.create_all(_engine)
                # 启用 WAL 模式
                with _engine.connect() as conn:
                    conn.exec_driver_sql("PRAGMA journal_mode=WAL")
                    conn.commit()
                SessionFactory = sessionmaker(bind=_engine, future=True)
    return _engine
```

- `config.py` — 新增

```python
db_pool_size: int = 5
db_max_overflow: int = 10
```

### 3.2 结构化日志与可配置级别 (P1-7)

**问题**: v0.1 日志格式为固定文本，无法对接 ELK/Loki 等日志聚合系统。日志级别硬编码。

**修改文件**:

- `config.py` — 新增

```python
log_level: str = "INFO"
log_format: Literal["text", "json"] = "text"
```

- `logging_setup.py` — 重构

```python
def configure(log_dir: Path, ssh_debug: bool = False,
              log_level: str = "INFO", log_format: Literal["text", "json"] = "text") -> None:
    serialize = (log_format == "json")
    logger.add(sys.stderr, format=_format if not serialize else None,
               serialize=serialize, level=log_level)
    # ... file handler 同样支持 JSON
```

同时修复了 SSH debug 异常时吞错误消息的问题（现在打印 `str(e)`）。

- `cli.py` / `api/main.py` — 调用处传递新参数

```python
configure(settings.log_dir, settings.ssh_debug,
          log_level=settings.log_level, log_format=settings.log_format)
```

### 3.3 /health 端点增强 (P1-8)

**问题**: v0.1 的 `/health` 始终返回 `{"status": "ok"}`，不检查 DB 连通性或调度器状态。

**修改文件**:

- `scheduler.py` — 新增公共函数

```python
def is_running() -> bool:
    with _lock:
        return _scheduler is not None and _scheduler.running

def job_count() -> int:
    with _lock:
        if _scheduler is None: return 0
        return len(_scheduler.get_jobs())
```

- `api/main.py` — `/health` 增强

```python
@app.get("/health")
def health():
    scheduler_ok = sched_mod.is_running()
    scheduler_jobs = sched_mod.job_count()
    db_ok = False
    try:
        with session() as s: s.execute(text("SELECT 1"))
        db_ok = True
    except Exception as e:
        db_error = str(e)

    overall = "ok" if (db_ok and scheduler_ok) else "degraded"
    return {
        "status": overall,
        "db": {"ok": db_ok, "error": db_error} if db_error else {"ok": True},
        "scheduler": {"ok": scheduler_ok, "jobs": scheduler_jobs},
        "version": "0.2.0",
    }
```

---

## 4. P2 — 性能与健壮性优化

### 4.1 Nornir O(N²) 过滤优化 (P2-10)

**问题**: v0.1 中构建包含全部 N 台设备的 Nornir 实例，再每设备 `nr.filter(name=...)` 遍历 N 个 host → O(N²)。

**修改文件**:

- `runner.py` — 移除 `_build_nornir(devices, ...)` 全量构建，新增 `_build_nornir_for_device(device, ...)` 单设备构建

```python
def _build_nornir_for_device(device: Device, credential_profile: str) -> Nornir:
    """P2-10: 为单台设备构建 Nornir 实例，避免 O(N^2) filter。"""
    # 只包含当前 device 的 hosts dict
    hosts = {device.name: {...}}
    return _dict_inventory_nornir(hosts, groups, 1)
```

`_run_one` 中直接调用 `_build_nornir_for_device(device, ...)` 替代原来的 `nr.filter(name=device.name)`。

### 4.2 大 Job 摘要内存优化 (P2-14)

**问题**: v0.1 中 `_write_summary` 将全量设备数据加载到内存字典和列表。

**修改文件**:

- `runner.py` — `_write_summary()` 改为 SQL GROUP BY 聚合

```python
# 计数用 SQL 聚合
counts = s.execute(
    select(DeviceRunRow.status, func.count())
    .where(DeviceRunRow.job_id == job_id)
    .group_by(DeviceRunRow.status)
).all()

# 成功列表限制 100 条
success_rows = s.execute(
    select(...).where(...).limit(100)
).all()
```

### 4.3 APScheduler 不阻塞 (P2-11)

**问题**: v0.1 中 `_scheduled_inspect` 同步执行，可能阻塞 APScheduler worker 数小时。

**修改文件**:

- `scheduler.py` — `_scheduled_inspect()` 改为 daemon 线程

```python
def _scheduled_inspect(...):
    import threading
    def _run():
        # ... 实际执行逻辑
    threading.Thread(target=_run, daemon=True).start()
```

### 4.4 未知设备类型友好错误 (P2-15)

**问题**: v0.1 中 `_platform_for()` 使用裸 dict 索引，未知组合引发无上下文 KeyError。

**修改文件**:

- `runner.py` — 改为 `.get()` + 自定义 ValueError

```python
_PLATFORM_MAP = {
    ("huawei", "firewall"): "huawei",
    ("h3c",    "firewall"): "hp_comware",
    ("h3c",    "switch"):   "hp_comware",
    ("ruijie", "switch"):   "ruijie_os",
}

def _platform_for(d: Device) -> str:
    key = (d.vendor.value, d.device_type.value)
    plat = _PLATFORM_MAP.get(key)
    if plat is None:
        raise ValueError(
            f"Unsupported vendor/device_type: vendor={d.vendor.value}"
            f" device_type={d.device_type.value} (device={d.name})"
            f". Supported: {list(_PLATFORM_MAP.keys())}"
        )
    return plat
```

### 4.5 _update_device / _finalize_job 缺失行告警

**修改文件**:

- `runner.py` — 原来 silently return，现在：

```python
def _update_device(...):
    row = s.get(DeviceRunRow, (job_id, device_name))
    if not row:
        logger.warning(f"DeviceRunRow missing: job={job_id} device={device_name}")
        return
```

---

## 5. P3 — 代码质量与可维护性

### 5.1 消除硬编码默认路径 (P3-16)

**修改文件**:

- `config.py` — 新增统一默认值

```python
default_inventory_path: str = "inventory/devices.csv"
```

- `api/schemas.py` — 3 处硬编码改为 `Field(default_factory=lambda: get_settings().default_inventory_path)`
- `cli.py` — `inventory` 参数默认值改为 `None`，使用 `settings.default_inventory_path` 兜底
- `routes.py` — 使用 `_DEFAULT_INVENTORY` 常量（从 settings 读取）

### 5.2 消除重复正则 (P3-17)

**问题**: `parser.py` 和 `tasks/verify_name.py` 各自定义了 hostname 提取正则。

**修改文件**:

- `naming.py` — 统一定义

```python
HOSTNAME_RE = re.compile(r"(?:sysname|hostname)\s+(\S+)", re.IGNORECASE)
```

- `parser.py` — `from ..naming import HOSTNAME_RE`
- `tasks/verify_name.py` — 移除本地定义，改为 `from ..naming import HOSTNAME_RE`

### 5.3 路由级统一认证 (P3-18)

**修改文件**:

- `routes.py` — 从每路由手动加 `dependencies=[Depends(_auth)]` 改为 Router 级别统一声明

```python
router = APIRouter(
    prefix="/api",
    tags=["inspection"],
    dependencies=[Depends(_auth)],  # 所有路由自动继承
)
```

所有路由装饰器中移除 `dependencies=[Depends(_auth)]`。

### 5.4 API 分页 (P3-19)

**修改文件**:

- `routes.py` — `list_jobs` 和 `list_backups` 增加 `offset` 参数

```python
@router.get("/jobs")
def list_jobs(status_filter: str | None = None, limit: int = 50, offset: int = 0):
    total = q.count()
    rows = q.order_by(JobRow.created_at.desc()).offset(offset).limit(limit).all()
    return {"total": total, "items": [_to_brief(r) for r in rows]}
```

### 5.5 统一错误响应格式 (P3-20)

**修改文件**:

- `api/main.py` — 注册异常处理器

```python
@app.exception_handler(404)
async def not_found_handler(request: Request, exc: Exception):
    return JSONResponse(
        status_code=404,
        content={"error": {"code": "NOT_FOUND", "message": "Resource not found"}},
    )
```

### 5.6 CredentialProfile 改为 dataclass (P3-21)

**修改文件**:

- `config.py` — 从 `class CredentialProfile(dict)` 改为

```python
@dataclass
class CredentialProfile:
    username: str = ""
    password: str = field(default="", repr=False)
    enable: str = field(default="", repr=False)
```

类型安全的属性访问：`cred.username` 替代 `cred["username"]`。

### 5.7 Parser 返回类型化 (P3-22)

**修改文件**:

- `parser.py` — 新增 TypedDict 联合类型

```python
class VersionInfo(TypedDict, total=False):
    version: str
    uptime: str

class HostnameInfo(TypedDict):
    hostname: str

class InterfaceBriefRow(TypedDict):
    name: str
    admin_or_oper_1: str
    admin_or_oper_2: str
    raw: str

class LLDPNeighbor(TypedDict, total=False):
    local_intf: str
    neighbor_device: str
    neighbor_port: str

class RawKept(TypedDict):
    raw_kept: bool

ParsedResult = Union[
    VersionInfo,
    HostnameInfo,
    list[InterfaceBriefRow],
    list[LLDPNeighbor],
    RawKept,
    list[dict[str, Any]],  # ntc-templates fallback
]
```

`parse()` 签名更新为 `def parse(...) -> ParsedResult | None`。

### 5.8 冗余 import 清理与路径解析复用

- `csv_source.py` — 路径解析复用 `config.py` 的 `_PROJECT_ROOT`，移除 `Path(__file__).resolve().parents[3]` 硬编码
- `report.py` — 两处 `except Exception` 改为记录 `logger.warning()`
- `cli.py` — 使用 `settings.default_inventory_path` 作为默认，消除硬编码

---

## 6. 配置项变更汇总

### 6.1 .env 新增字段

| 字段 | 默认值 | 说明 |
|------|--------|------|
| `ENVIRONMENT` | `production` | `development` 时允许跳过鉴权 |
| `LOG_LEVEL` | `INFO` | 日志级别 (DEBUG/INFO/WARNING/ERROR) |
| `LOG_FORMAT` | `text` | `json` 时输出结构化日志 |
| `DB_POOL_SIZE` | `5` | 数据库连接池大小 |
| `DB_MAX_OVERFLOW` | `10` | 连接池最大溢出数 |
| `DEFAULT_INVENTORY_PATH` | `inventory/devices.csv` | 默认设备清单路径 |
| `DEVICE_RUN_TIMEOUT` | `0` | 单设备执行超时秒数（0=不限制） |

---

## 7. 数据库 Schema 变更

### 7.1 新增表: `job_control`

```sql
CREATE TABLE job_control (
    job_id   TEXT PRIMARY KEY,
    paused   BOOLEAN DEFAULT 0,
    canceled BOOLEAN DEFAULT 0
);
```

### 7.2 SQLite 优化

- 启用 WAL 模式：`PRAGMA journal_mode=WAL`
- 引擎初始化：`pool_pre_ping=True`、`pool_size=5`、`max_overflow=10`

---

## 8. 文件变更清单

| 文件 | P0 | P1 | P2 | P3 | 说明 |
|------|:--:|:--:|:--:|:--:|------|
| `src/inspection/config.py` | ✅ | | ✅ | ✅ | environment、log_level/log_format、db_pool、dataclass |
| `src/inspection/db.py` | ✅ | | ✅ | | JobControlRow、WAL、sessionmaker |
| `src/inspection/runner.py` | ✅ | | ✅ | | DB-backed control、O(N)→O(1)、as_completed fix |
| `src/inspection/api/routes.py` | ✅ | | | ✅ | auth fail-closed、竞态修复、路径防护、分页、router级认证 |
| `src/inspection/api/main.py` | | ✅ | | ✅ | /health增强、exception handler |
| `src/inspection/api/schemas.py` | | | | ✅ | default_inventory_path |
| `src/inspection/logging_setup.py` | | ✅ | | | JSON 日志、可配置级别 |
| `src/inspection/cli.py` | | ✅ | | ✅ | log params、default_path |
| `src/inspection/scheduler.py` | | | ✅ | | 非阻塞、is_running/job_count |
| `src/inspection/naming.py` | | | | ✅ | HOSTNAME_RE 统一 |
| `src/inspection/parser.py` | | | | ✅ | ParsedResult 类型化 |
| `src/inspection/tasks/verify_name.py` | | | | ✅ | 复用 HOSTNAME_RE |
| `src/inspection/report.py` | | | | ✅ | logger.warning |
| `src/inspection/inventory/csv_source.py` | | | | ✅ | 路径解析复用 |
| `tests/test_csv_source.py` | | | | ✅ | 新增 6 tests |
| `tests/test_backup_store.py` | | | | ✅ | 新增 6 tests |
| `pyproject.toml` | | | | ✅ | 新增 pytest 依赖 |

---

## 9. 测试

### 9.1 测试结果

```
tests/test_naming.py::test_parse_simple                  PASSED
tests/test_naming.py::test_parse_multi_dash_suffix       PASSED
tests/test_naming.py::test_hostname_compare_truncated    PASSED
tests/test_csv_source.py::test_fetch_all                 PASSED
tests/test_csv_source.py::test_filter_vendor             PASSED
tests/test_csv_source.py::test_filter_device_type        PASSED
tests/test_csv_source.py::test_parse_errors              PASSED
tests/test_csv_source.py::test_nonexistent_file          PASSED
tests/test_csv_source.py::test_name_fallback             PASSED
tests/test_backup_store.py::test_save_and_dedup          PASSED
tests/test_backup_store.py::test_save_and_list           PASSED
tests/test_backup_store.py::test_diff_no_backups         PASSED
tests/test_backup_store.py::test_save_empty_raises       PASSED
tests/test_backup_store.py::test_diff_two_versions       PASSED
tests/test_backup_store.py::test_list_limit                         PASSEDпохат

============================== 15 passed ==============================
```

### 9.2 新增测试说明

| 测试文件 | 覆盖模块 | 用例数 | 覆盖场景 |
|---------|---------|--------|---------|
| `test_csv_source.py` | `inventory/csv_source.py` | 6 | CSV 解析、vendor/type 过滤、无效行错误收集、文件不存在、命名兜底 |
| `test_backup_store.py` | `backup_store.py` | 6 | SHA 去重、列表查询、空配置异常、diff 对比、limit 限制 |

---

## 10. 已知局限

| 局限 | 说明 | 缓解措施 |
|------|------|----------|
| 单进程调度器 | APScheduler 仅在单 worker 内有效 | 文档说明，多实例应改用外部调度器 |
| pause 轮询间隔 1s | 暂停命令最多延迟 1 秒响应 | 网络设备 SSH 耗时远大于此 |
| legacy_kex 仍为全局修改 | 华为设备 KEX 兼容性仍影响整个进程 Paramiko | 少量场景接受，未来可改进 |
| SQLite 写入瓶颈 | 高并发大量写入仍有锁争用 | WAL 模式已大幅缓解，大数据量建议 PostgreSQL |

---

## 11. 后续路线 (v0.3+)

| 版本 | 内容 |
|------|------|
| v0.3 | CMDB API 数据源、断点续跑、邮件/IM 通知 |
| v0.4 | 多租户、RBAC、SSO、Web UI、PostgreSQL 后端 |
| v0.5 | 分布式任务队列 (Celery/RQ)、Prometheus 指标、Grafana 仪表板 |
