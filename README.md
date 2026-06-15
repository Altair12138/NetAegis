# 自动化巡检平台 (inspection-platform)

基于 **Nornir + Netmiko + FastAPI** 的网络设备自动化巡检 / 配置备份平台，详见 [`docs/DESIGN.md`](docs/DESIGN.md)。

## 快速开始

```bash
# 1. 安装依赖（推荐 uv）
uv venv
uv sync

# 2. 配置环境
cp .env.example .env
# 编辑 .env 填入凭据

# 3. 编辑设备清单
vim inventory/devices.csv

# 4. CLI 触发巡检/备份
uv run inspect run --inventory inventory/devices.csv --concurrency 20
uv run inspect run --type backup --inventory inventory/devices.csv

# 备份任务关闭设备端保存
uv run inspect run --type backup --inventory inventory/devices.csv --no-save

# 5. 启动 API
uv run uvicorn inspection.api.main:app --reload --port 8080

# 6. API 下发（示例）
curl -X POST localhost:8080/api/jobs \
	-H 'Content-Type: application/json' \
	-d '{"type":"inspect","inventory_path":"inventory/devices.csv","concurrency":20}'

# 备份任务默认保存，巡检不保存；可显式控制
curl -X POST localhost:8080/api/jobs \
	-H 'Content-Type: application/json' \
	-d '{"type":"inspect","inventory_path":"inventory/devices.csv","device_save":true}'
```

## 目录结构

```
docs/DESIGN.md              架构与详细设计
inventory/devices.csv       一期数据源（CSV）
inventory/groups.yaml       Nornir 厂商默认参数
src/inspection/             核心代码
results/                    巡检结果（按 job_id 分目录）
logs/                       运行日志
```

## API 速览

| Method | Path | 用途 |
| --- | --- | --- |
| POST | `/api/jobs` | 创建巡检/备份任务 |
| GET | `/api/jobs/{id}` | 任务详情 + 进度 |
| POST | `/api/jobs/{id}/pause` | 设备级暂停 |
| POST | `/api/jobs/{id}/resume` | 恢复 |
| GET | `/api/jobs/{id}/devices/{name}/log` | 下载原始日志 |
| GET | `/api/jobs/{id}/devices/{name}/result` | 结构化结果（含 save_result） |
| GET | `/api/devices/{name}/backups` | 配置备份历史 |
| GET | `/api/devices/{name}/diff` | 配置差异对比 |

完整接口见 OpenAPI（启动后访问 `/docs`）。

## 鉴权（API Token）

在项目根目录 `.env` 中设置：

```
API_TOKEN=your_token_here
```

请求时带上：

```bash
curl -X POST localhost:8080/api/jobs \
	-H 'Authorization: Bearer your_token_here' \
	-H 'Content-Type: application/json' \
	-d '{"type":"inspect","inventory_path":"inventory/devices.csv","concurrency":20,"device_save":true}'
```

未设置 `API_TOKEN` 时会放行所有请求（开发模式）。

## 注意

- 凭据通过项目根目录 `.env` 注入，禁止写入 CSV 或代码。
- 一期支持四类设备：华为防火墙、华三防火墙、锐捷交换机、华三交换机；新增厂商扩展 `src/inspection/commands/*.yaml` 即可。
- 二期解析层（`parser.py`）输出统一 JSON Schema，详见设计文档第 8 节。
