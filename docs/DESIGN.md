# 自动化巡检平台 — 设计文档

> 版本：v0.1（一期 + 二期方向）
> 维护：网络运维平台组
> 适用范围：防火墙（华为、华三）、交换机（锐捷、华三）

---

## 1. 目标与范围

### 1.1 一期目标

- 通过设备清单（CSV / CMDB API）批量登录设备执行预设巡检命令。
- 登录后核对设备实际命名与清单是否一致，结果落盘为 `<设备名>_<管理IP>.log`。
- 提供操作日志（执行级 + 设备级）。
- 支持并发执行、**设备级暂停 / 恢复**。
- 通过 FastAPI 暴露接口，供前端触发巡检、查询状态、暂停恢复、下载结果。
- 凭据通过 `uv` 管理的 `.env` 注入，禁止明文落盘。

### 1.2 二期目标

- 屏蔽厂商差异，将原始命令输出解析为统一 JSON Schema（基于 ntc-templates / TextFSM，必要时补充自定义模板）。
- 支持配置备份、差异比对、定时巡检、报表导出。

---

## 2. 技术选型

| 能力 | 选型 | 说明 |
| --- | --- | --- |
| 编排 / 并发 | **Nornir 3.x** | 原生多线程、Inventory 抽象、任务可组合 |
| 设备连接 | **Netmiko**（via `nornir-netmiko`） | 覆盖华为/华三/锐捷主流型号 |
| 解析（二期） | **ntc-templates + TextFSM**，配合 `genie`（可选）兜底 | 锐捷、华三部分命令需自定义模板 |
| API | **FastAPI** + **uvicorn** | 异步、自动 OpenAPI、便于前端集成 |
| 数据模型 | **pydantic v2** | Inventory、Job、Result 模型校验 |
| 状态持久化 | **SQLite + SQLAlchemy 2.x** | 任务/设备状态、并支持后续切换 PostgreSQL |
| 日志 | **loguru** | 结构化、按 job 分文件 |
| 配置 / 凭据 | **python-dotenv** + `.env` | 与 `uv` 工作流一致 |
| 包管理 | **uv** | `uv sync` / `uv run` |

> 备选项：如果未来设备覆盖到 Cisco、Juniper、Arista，建议引入 **Scrapli + Scrapli-Community** 提升性能与稳定性，与 Nornir 通过 `nornir-scrapli` 集成；解析层可在 ntc-templates 之上叠加 **TTP** 处理私有格式。

---

## 3. 系统架构

```
┌────────────────────────────────────────────────────────────────────┐
│                              前端 / 调度方                          │
└──────────────┬───────────────────────────────────────────┬─────────┘
               │ HTTP (FastAPI)                            │
┌──────────────▼────────────┐                  ┌──────────▼──────────┐
│        API Layer          │                  │     CLI Entry       │
│  routes / schemas / auth  │                  │   inspect / backup  │
└──────────────┬────────────┘                  └──────────┬──────────┘
               │                                          │
               ▼                                          ▼
        ┌──────────────────────────────────────────────────────┐
        │                       Runner                         │
        │  Nornir 编排 · 并发控制 · 设备级暂停/恢复 · 进度上报    │
        └──────────────┬──────────────────────────┬────────────┘
                       │                          │
       ┌───────────────▼───────────┐  ┌───────────▼────────────┐
       │  Inventory Source         │  │   Task Library          │
       │  (CSV / CMDB API)         │  │  inspect / backup /     │
       │  + 命名校验                │  │  verify_name            │
       └───────────────────────────┘  └───────────┬────────────┘
                                                  │
                                  ┌───────────────▼────────────┐
                                  │ Command Repo (yaml)         │
                                  │ vendor × device_type        │
                                  └───────────────┬────────────┘
                                                  │
                            ┌─────────────────────▼──────────────────┐
                            │  Netmiko 驱动 · 与设备 SSH 交互          │
                            └─────────────────────┬──────────────────┘
                                                  │
                                  ┌───────────────▼────────────┐
                                  │  Parser (二期)              │
                                  │  ntc-templates → JSON       │
                                  └───────────────┬────────────┘
                                                  │
                                  ┌───────────────▼────────────┐
                                  │  存储层                      │
                                  │  SQLite（状态） + 文件（日志/结果） │
                                  └────────────────────────────┘
```

---

## 4. 目录结构

```
inspection-platform/
├── pyproject.toml
├── .env.example
├── README.md
├── docs/DESIGN.md
├── inventory/
│   ├── devices.csv             # 一期数据源
│   └── groups.yaml             # 厂商默认参数（platform/timeout 等）
├── src/inspection/
│   ├── config.py               # Settings (.env 注入)
│   ├── models.py               # Device / Job / TaskResult
│   ├── db.py                   # SQLite + SQLAlchemy
│   ├── logging_setup.py
│   ├── naming.py               # 设备名规则解析与校验
│   ├── inventory/
│   │   ├── base.py             # InventorySource 抽象
│   │   ├── csv_source.py
│   │   └── cmdb_source.py      # 二期：CMDB API
│   ├── commands/
│   │   ├── loader.py           # vendor+type → 命令列表
│   │   ├── huawei_firewall.yaml
│   │   ├── h3c_firewall.yaml
│   │   ├── ruijie_switch.yaml
│   │   └── h3c_switch.yaml
│   ├── tasks/
│   │   ├── verify_name.py
│   │   ├── inspect.py
│   │   └── backup.py
│   ├── runner.py               # Nornir 编排 + 并发 + 暂停
│   ├── parser.py               # 二期 ntc-templates 封装
│   └── api/
│       ├── main.py
│       ├── routes.py
│       └── schemas.py
├── results/                    # 巡检输出，按 job_id 分目录
├── logs/                       # 运行日志
└── tests/
```

---

## 5. 数据模型

### 5.1 Device（清单字段）

| 字段 | 类型 | 必填 | 示例 |
| --- | --- | --- | --- |
| name | str | Y | `SH-MH-401-C11U3-H3CS9825-G0-A04008` |
| mgmt_ip | IPv4Address | Y | `10.10.1.5` |
| device_type | enum(`firewall` / `switch`) | Y | switch |
| vendor | enum(`huawei` / `h3c` / `ruijie`) | Y | h3c |
| model | str | N | `S9825`（可由命名解析） |
| credential_profile | str | N | 默认 `default`，用于多套账号 |
| port | int | N | 默认 22 |

> CSV 列名与上表一致；CMDB 适配层负责字段映射。

### 5.2 Naming（命名校验）

正则：

```
^(?P<city>[A-Z]+)-(?P<area>[A-Z0-9]+)-(?P<room>\d+)-(?P<rack>[A-Z0-9]+)-(?P<vendor_model>[A-Z0-9]+)-(?P<role>[A-Z0-9]+)-(?P<suffix>.+)$
```

要点：
- 末段 `suffix` 允许出现多个连字符，例如 `IntCPU-11212`。
- `vendor_model` 用于反查厂商型号（如 `H3CS9825` → vendor=h3c, model=S9825），用于 `vendor` 字段缺失时的兜底。
- 校验阶段比对：清单 `name` ↔ 设备实际 `display this | include sysname / hostname` 输出。
- 不一致时不阻断巡检，但记录到 `verify_name` 结果与日志，最终结果中加 `name_mismatch: true` 标记。

### 5.3 Job（运行实例）

```python
class JobStatus(str, Enum):
    pending = "pending"
    running = "running"
    paused = "paused"          # 设备级暂停：未开始的设备阻塞
    completed = "completed"
    failed = "failed"
    canceled = "canceled"

class DeviceRunStatus(str, Enum):
    queued = "queued"
    running = "running"
    success = "success"
    name_mismatch = "name_mismatch"
    failed = "failed"
    skipped = "skipped"        # 暂停时未开始的设备
```

`Job` 记录：`id`、`type`（inspect/backup）、`created_at`、`status`、`concurrency`、`device_count`、`progress`、`paused_at`、`result_dir`。
`DeviceRun` 记录每台设备的状态、开始/结束时间、日志路径、`name_mismatch`。

---

## 6. 命令库

按 `vendor × device_type` 加载 YAML：

```yaml
# h3c_switch.yaml
sysname_cmd: "display current-configuration | include sysname"
commands:
  - key: version
    cmd: "display version"
  - key: config
    cmd: "display current-configuration"
  - key: interface
    cmd: "display interface brief"
  - key: route
    cmd: "display ip routing-table"
  - key: arp
    cmd: "display arp"
  - key: lldp
    cmd: "display lldp neighbor-information"
  - key: bgp_summary
    cmd: "display bgp peer"
    optional: true            # 没启用 BGP 不报错
```

常见命令（节选）：

| 类别 | 华为防火墙 | 华三防火墙 | 锐捷交换机 | 华三交换机 |
| --- | --- | --- | --- | --- |
| 版本 | display version | display version | show version | display version |
| 配置 | display current-configuration | display current-configuration | show running-config | display current-configuration |
| 路由 | display ip routing-table | display ip routing-table | show ip route | display ip routing-table |
| ARP | display arp | display arp | show arp | display arp |
| 接口 | display interface brief | display interface brief | show interface status | display interface brief |
| LLDP | display lldp neighbor brief | display lldp neighbor-information | show lldp neighbors | display lldp neighbor-information |
| BGP | display bgp peer | display bgp peer | show bgp summary | display bgp peer |
| 序列号 | display device manufacture-info | display device manufacture-info | show version slots | display device manufacture-info |
| 主机名 | display current-configuration \| include sysname | 同左 | show running-config \| include hostname | 同华三 |

> 完整命令清单维护在 `src/inspection/commands/*.yaml`，便于平台同事直接 PR 而无需改代码。

### 6.1 命令子集过滤（部分巡检）

每次巡检不必跑全量。`JobCreate` 提供两个互补字段：

| 字段 | 语义 | 用例 |
| --- | --- | --- |
| `command_keys` | 精确按命令 key 选 | 只看 LLDP：`["lldp"]`；只看路由：`["route"]` |
| `command_tags` | 按主题 tag 选（命中任一） | 只看路由相关：`["routing"]`；只看拓扑：`["topology"]` |

两者可以同时给，按**并集**计算；都为空则跑全量。`__sysname__` 探测永远会跑（设备名核对必跑，且耗时极小）。

可用 tag 约定：`basic / health / interface / l2 / l3 / routing / topology / security / ha / stack / bgp / ospf / config / mlag / rocev2 / pfc / ecn / dcb / qos`。新增 vendor 时统一沿用，方便跨厂商按主题筛选。

> `mlag` 覆盖 M-LAG / S-MLAG 高可用相关命令（同时打 `ha` tag，`--tags ha` 可一并拉取）；
> `rocev2` 是 RDMA 无损网络相关命令的伞 tag，子项 `pfc / ecn / dcb / qos` 用于更细粒度筛选，例如 `--tags pfc,ecn` 只看流控与拥塞标记。

调用形式：

```bash
# CLI：只巡检 lldp 和 route
uv run inspect run --keys lldp,route

# CLI：按主题选（路由 + 健康）
uv run inspect run --tags routing,health

# CLI：查看某类型设备的可选 key/tag
uv run inspect commands h3c_switch
```

```http
POST /api/jobs
{
  "type": "inspect",
  "inventory_path": "inventory/devices.csv",
  "command_keys": ["lldp"],
  "concurrency": 20
}

GET /api/commands         # 返回 {catalog: {h3c_switch: {commands:[...]}, ...}, tags: [...]}
```

前端可调用 `GET /api/commands` 拿到每个 vendor_device_type 的命令清单和全量 tag，渲染成「按命令」/「按主题」两个多选下拉。

### 6.2 使用方式（CLI / API）

#### CLI 触发

```bash
# 巡检（默认）
uv run inspect run --inventory inventory/devices.csv --concurrency 20

# 备份配置
uv run inspect run --type backup --inventory inventory/devices.csv

# 只对部分设备（按 vendor / device_type 过滤）
uv run inspect run --type inspect --vendor h3c --device-type switch

# 只跑部分命令
uv run inspect run --keys lldp,route
uv run inspect run --tags routing,health

# 查看某类型设备可选命令 key / tag
uv run inspect commands h3c_switch
```

#### API 下发

```http
POST /api/jobs
{
  "type": "inspect",
  "inventory_path": "inventory/devices.csv",
  "concurrency": 20
}
```

```http
POST /api/jobs
{
  "type": "backup",
  "inventory_path": "inventory/devices.csv",
  "device_filter": {"vendor": "h3c", "device_type": "switch"}
}
```

```http
POST /api/jobs
{
  "type": "inspect",
  "inventory_path": "inventory/devices.csv",
  "command_keys": ["lldp", "route"]
}
```

```http
POST /api/jobs
{
  "type": "inspect",
  "inventory_path": "inventory/devices.csv",
  "command_tags": ["routing", "health"]
}
```

```http
GET /api/commands
```

#### 任务控制与查询

```http
GET /api/jobs
GET /api/jobs/{id}
POST /api/jobs/{id}/pause
POST /api/jobs/{id}/resume
POST /api/jobs/{id}/cancel
GET /api/jobs/{id}/devices
GET /api/jobs/{id}/devices/{name}/log
```

#### 鉴权（API Token）

- 在项目根目录 `.env` 设置 `API_TOKEN` 后，所有 `/api/*` 需带 `Authorization` 头。
- 未设置 `API_TOKEN` 时默认放行（开发模式）。

```bash
curl -X POST localhost:8080/api/jobs \
  -H 'Authorization: Bearer your_token_here' \
  -H 'Content-Type: application/json' \
  -d '{"type":"inspect","inventory_path":"inventory/devices.csv","concurrency":20}'
```

---

## 7. Runner：并发、暂停、恢复

### 7.1 并发模型

- Nornir `runners.threaded`，`num_workers` 由 Job 创建参数决定，默认 20。
- 单设备内串行执行命令，命令间错误隔离（一条命令超时不影响其它命令，结果中记录 `error`）。

### 7.2 设备级暂停语义

- 维护一个进程内 `JobController`：`{job_id: {pause_event, cancel_event, status}}`。
- 任务进入设备前 `pause_event.wait()`，已在跑的设备**跑完所有命令**才让出。
- 暂停时：未开始的设备状态为 `queued`（持久化），收到 `resume` 后继续；`cancel` 则将剩余设备标记 `canceled`。
- 进程重启恢复：根据 SQLite 中 `DeviceRun.status=queued` 的设备重建队列继续跑（一期可选，二期完善）。

### 7.3 错误处理

- 连接失败：重试 2 次（指数退避），最终失败写 `failed`，日志保留异常栈。
- 命令超时：单条命令超时（默认 60s），其它命令照常。
- 设备名不一致：写 `name_mismatch=true`，巡检继续完成。

### 7.4 输出布局

```
results/
  <job_id>/
    <device_name>_<mgmt_ip>.log     # 原始：所有命令的原文，按 "===== <cmd> =====" 分隔
    <device_name>_<mgmt_ip>.json    # 处理后：结构化（device/name_check/commands[].raw/parsed/error）
                                    #   一期 parsed=None；二期由 parser.py 填充
    summary.json                    # 设备级状态摘要（可选，由汇总任务生成）
logs/
  platform.log                      # 全局日志（loguru rotation）
  <job_id>.log                      # 单 Job 全过程日志
```

> 原始 `.log` 与处理后 `.json` 始终同目录、同前缀、并列存在，便于人工 vs 程序两种消费方式同时可用。
> JSON 中通过 `raw_log` 字段反向指向同目录的 `.log` 文件名。

---

## 8. 二期解析层

- 入口：`parser.parse(vendor, device_type, command_key, raw_output) -> dict | list[dict]`。
- 优先使用 ntc-templates（platform 命名映射：`hp_comware`、`huawei_vrp`、`ruijie_os`）。
- 缺失模板时回落到自研 TextFSM / TTP 模板，统一存放 `src/inspection/parser_templates/`。
- 输出 Schema（节选）：

```jsonc
{
  "device": {"name": "...", "mgmt_ip": "...", "vendor": "h3c", "model": "S9825"},
  "collected_at": "2026-05-30T12:00:00+08:00",
  "data": {
    "version": {"os": "Comware", "version": "7.1.075", "uptime_seconds": 1234567},
    "interfaces": [{"name": "GE1/0/1", "admin": "up", "oper": "up", "desc": "..."}],
    "routes":     [{"prefix": "10.0.0.0/24", "protocol": "ospf", "next_hop": "..."}],
    "arp":        [...],
    "lldp":       [...],
    "bgp_peers":  [...]
  },
  "warnings": ["name_mismatch", "cmd_timeout:display bgp peer"]
}
```

---

## 9. FastAPI 接口

| Method | Path | 说明 |
| --- | --- | --- |
| POST | `/api/jobs` | 创建巡检/备份任务（body: type、inventory_source、device_filter、concurrency） |
| GET | `/api/jobs` | 任务列表（分页 + 状态过滤） |
| GET | `/api/jobs/{id}` | 任务详情 + 进度 |
| POST | `/api/jobs/{id}/pause` | 设备级暂停 |
| POST | `/api/jobs/{id}/resume` | 恢复 |
| POST | `/api/jobs/{id}/cancel` | 取消 |
| GET | `/api/jobs/{id}/devices` | 设备级状态列表 |
| GET | `/api/jobs/{id}/devices/{name}/log` | 下载单设备原始日志 |
| GET | `/api/jobs/{id}/devices/{name}/result` | 二期：结构化 JSON 结果 |
| POST | `/api/inventory/preview` | 上传 CSV / 触发 CMDB 同步预览 |

认证留出 `Depends(get_current_user)` 钩子，初期可使用 API Token，与公司 SSO 对接后切换。

---

## 10. 凭据与安全

- `.env` 字段：
  ```
  CRED_DEFAULT_USERNAME=netops
  CRED_DEFAULT_PASSWORD=...
  CRED_DEFAULT_ENABLE=...
  CRED_BACKUP_USERNAME=backup
  CRED_BACKUP_PASSWORD=...
  DB_URL=sqlite:///./inspection.db
  RESULT_DIR=./results
  LOG_DIR=./logs
  API_TOKEN=...                # 简易鉴权，生产改 OIDC
  ```
- `Settings` 通过 `pydantic-settings` 加载，禁止 `print`/`log` 输出密码字段（loguru 配置脱敏过滤器）。
- 日志中出现的密码字段统一替换为 `***`。
- 与 CMDB 集成后，可考虑改为 Vault / KMS 动态获取，credential_profile 字段已为此预留。

---

## 11. 后续路线

| 阶段 | 内容 |
| --- | --- |
| v0.1 一期 | CSV inventory、巡检/备份、设备名核对、设备级暂停、API、日志落盘 |
| v0.2 | CMDB 接入、定时任务（APScheduler）、断点续跑 |
| v0.2 二期 | ntc-templates 解析、配置备份版本化+差异、APScheduler 定时、Excel 报表 |
| v0.3 | 设备组/标签、断点续跑、解析模板自动补全、邮件/IM 通知 |
| v0.4 | 多租户、RBAC、SSO、Web UI、PostgreSQL/对象存储后端 |

---

## 12. 二期实现细节（v0.2 已落地）

### 12.1 解析层（parser.py）

- 入口 `parse(vendor, device_type, command, raw) -> Any | None`，在 `JobCreate.enable_parse=True` 时由 inspect 任务调用，结果写入 `commands[].parsed`。
- 三级降级：① ntc-templates（按 `_PLATFORM_MAP` + `_COMMAND_ALIAS` 查模板）→ ② 内置正则兜底（version / hostname / interface_brief / lldp）→ ③ `{"raw_kept": true}` 不丢数据。
- 输出 JSON 里 `parsed` 与 `raw` 并存，前端可二选一展示，便于人工核对。

### 12.2 配置备份 + 差异（backup_store.py）

- 目录：`backups/<device>/<YYYYMMDDTHHMMSSZ>_<sha8>.cfg`，每台设备额外维护 `latest.cfg` 方便直接 `cat`。
- SHA256 去重：若与该设备最新一版完全一致，不写新文件、只刷新 `last_seen_at`，避免长期巡检产生海量"零变化"备份。
- 触发方式：
  - `JobType.backup` 任务（仅跑 `backup_cmd`）；
  - 任意 `inspect` 任务只要抓到 `config` key 且 `auto_backup=True`（默认开），就会自动入库；
- 差异：`backup_store.diff(device, a_id=None, b_id=None)` 生成 unified diff。默认比"最近两版"，可显式指定任意两个备份 ID。
- API：
  - `GET /api/devices/{name}/backups` 列表
  - `GET /api/devices/{name}/backups/{id}` 下载某一版
  - `GET /api/devices/{name}/diff?a=&b=` 取 diff（不传 a/b 即比最近两版）

### 12.3 定时巡检（scheduler.py）

- APScheduler `BackgroundScheduler` + `SQLAlchemyJobStore`（沿用 `DB_URL`），重启不丢任务。
- 与 FastAPI 同进程，`lifespan` 启动/关闭。
- 三种触发：
  - `cron`：`{"hour": 2, "minute": 0, "day_of_week": "mon-fri"}` —— 每个工作日 02:00
  - `interval`：`{"hours": 6}` —— 每 6 小时
  - `date`：`{"run_date": "2026-06-01 03:00:00"}` —— 一次性
- API：
  - `POST /api/schedules` 新建/替换
  - `GET /api/schedules` 列表（含 next_run_time）
  - `DELETE /api/schedules/{id}` 删除
  - `POST /api/schedules/{id}/pause` / `resume` 暂停恢复
- 调度的 Job 参数（inventory / 命令子集 / 凭据 profile / device_filter 等）随 JobStore 一起持久化。
- 局限：单进程内有效；多实例部署需切 Celery 或外部调度器。

### 12.4 Excel 报表（report.py）

- `report.generate(job_id)` 输出 `results/<job_id>/report_<job_id>.xlsx`。
- 四个 sheet：
  - **Overview**：Job 元信息 + 状态计数 + 总耗时
  - **Devices**：每台设备一行（含 log/JSON 路径），失败标红、命名不一致标黄
  - **Errors**：从同目录 JSON 反向拉每条命令的 error，定位到 (设备, 命令)
  - **NameMismatch**：清单名 vs 实际 hostname 对照
- API：`GET /api/jobs/{job_id}/report.xlsx` 直接下载。

### 12.5 调用示例

```bash
# 启用解析、自动备份的巡检
curl -X POST localhost:8080/api/jobs \
  -H 'Authorization: Bearer xxx' -H 'Content-Type: application/json' \
  -d '{"type":"inspect","inventory_path":"inventory/devices.csv",
       "enable_parse":true,"auto_backup":true,"concurrency":30}'

# 看某设备的备份历史
curl localhost:8080/api/devices/SH-MH-401-C11U3-H3CS9825-G0-A04008/backups

# 比较最近两版
curl localhost:8080/api/devices/SH-MH-401-C11U3-H3CS9825-G0-A04008/diff

# 每个工作日凌晨 2 点跑全量巡检
curl -X POST localhost:8080/api/schedules \
  -H 'Content-Type: application/json' \
  -d '{"id":"daily-inspect","trigger_type":"cron",
       "trigger_args":{"hour":2,"minute":0,"day_of_week":"mon-fri"},
       "inventory_path":"inventory/devices.csv","concurrency":30}'

# 下载某次巡检的 Excel 报表
curl -OJ localhost:8080/api/jobs/<job_id>/report.xlsx
```
