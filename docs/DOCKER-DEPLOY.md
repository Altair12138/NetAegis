# NetAegis Docker 部署指南

## 概览

NetAegis 使用 Docker 容器化部署，包含以下核心组件：
- **FastAPI API 服务** — 提供 REST API 接口
- **SQLite 数据库** — 存储任务、设备运行记录、调度信息
- **数据卷** — 持久化数据库、巡检结果、日志、配置备份

---

## 一、前置条件

| 条件 | 说明 |
|------|------|
| Docker | >= 20.10 |
| Docker Compose | >= v2 (即 `docker compose` 命令) |
| 网络可达 | 容器需能 SSH 到被管理网络设备 |
| 磁盘空间 | >= 2GB (镜像约 500MB，数据视规模增长) |

---

## 二、快速部署（3 步启动）

```bash
# 1. 准备配置
cp .env.example .env
# 编辑 .env，填写设备凭据和 API Token

# 2. 构建镜像并启动
docker compose up -d --build

# 3. 验证服务
curl http://localhost:8080/health
```

---

## 三、部署方式

### 部署文件清单

| 文件/目录 | 是否必需 | 说明 |
|-----------|----------|------|
| `Dockerfile` | ✅ | 镜像构建定义 |
| `docker-compose.yml` | ✅ | 容器编排配置 |
| `.dockerignore` | ✅ | 构建上下文排除规则 |
| `pyproject.toml` | ✅ | Python 依赖声明 |
| `src/` | ✅ | 项目源码 |
| `inventory/` | ✅ | 设备清单 |
| `.env.example` | 推荐 | 环境配置模板 |
| `.env` | ⚠️ **不在代码中** | 含敏感凭据，在服务器上单独创建 |
| `inspection.db` | ❌ | 运行时在 Docker 卷中自动创建 |
| `results/` `logs/` `backups/` | ❌ | 运行时在 Docker 卷中自动创建 |
| `.git/` | ❌ | 版本控制元数据 |
| `tests/` | ❌ | 测试代码 |
| `.venv/` | ❌ | 本地虚拟环境 |

### 方式一：源码构建部署（推荐 — 开发/试运行阶段）

将源码拷贝到服务器，在服务器上构建镜像。改代码后只需增量同步 + 重新构建，灵活高效。

#### 首次部署

```bash
# ---- 本地机器 ----

# 打包部署文件（排除不需要的目录）
tar czf netaegis-deploy.tar.gz \
  --exclude='.git' \
  --exclude='.env' \
  --exclude='inspection.db' \
  --exclude='results' \
  --exclude='logs' \
  --exclude='backups' \
  --exclude='tests' \
  --exclude='.venv' \
  --exclude='__pycache__' \
  .

# 传输到服务器
scp netaegis-deploy.tar.gz user@server:/opt/

# ---- 服务器 ----

# 解压
mkdir -p /opt/netaegis
tar xzf /opt/netaegis-deploy.tar.gz -C /opt/netaegis
cd /opt/netaegis

# 创建环境配置（从模板复制并填写真实凭据）
cp .env.example .env
vim .env   # 必须修改: CRED_DEFAULT_USERNAME/PASSWORD, API_TOKEN

# 构建镜像并启动
docker compose up -d --build

# 验证服务
curl http://localhost:8080/health
```

#### 代码更新后重新部署

```bash
# ---- 本地机器 ----

# 增量同步变更文件（rsync 只传差异部分，速度更快）
rsync -avz --delete \
  --exclude='.git' \
  --exclude='.env' \
  --exclude='*.db' \
  --exclude='results' \
  --exclude='logs' \
  --exclude='backups' \
  --exclude='.venv' \
  --exclude='__pycache__' \
  ./ user@server:/opt/netaegis/

# ---- 服务器 ----

# 重新构建并启动（Docker 缓存层加速，只重建变更部分）
ssh user@server "cd /opt/netaegis && docker compose up -d --build"

# 验证
ssh user@server "curl -fs http://localhost:8080/health"
```

> 💡 使用 `rsync` 而非 `scp`，只传输变更的文件，大幅加快更新速度。
> 首次部署用 `tar`，后续更新用 `rsync`，两者配合使用。

### 方式二：镜像分发部署（正式生产/多节点）

本地构建镜像，推送到 Container Registry，服务器直接拉取运行。
服务器上不需要源码和 Dockerfile，只需 `docker-compose.yml` + `.env` 两个文件。

```bash
# ---- 本地机器 ----

# 构建镜像
docker build -t your-registry.com/netaegis:latest .

# 推送到私有 Registry
docker push your-registry.com/netaegis:latest

# 传输 docker-compose.yml 到服务器
scp docker-compose.yml user@server:/opt/netaegis/

# ---- 服务器 ----

cd /opt/netaegis

# 修改 docker-compose.yml 中的 image 为 registry 地址
#   image: your-registry.com/netaegis:latest
# 并删除 build: 配置块

# 创建 .env
cp .env.example .env
vim .env

# 拉取镜像并启动
docker compose up -d
```

### 两种方式对比

| 对比项 | 方式一（源码构建） | 方式二（镜像分发） |
|--------|-------------------|-------------------|
| 服务器需要源码 | ✅ 是 | ❌ 否 |
| 改代码后更新 | `rsync` → `docker compose up -d --build` | 本地 `build` + `push` → 服务器 `pull` → 重启 |
| 调试排查 | 服务器有源码，可直接 `docker exec` 查看 | 只有镜像，排查不便 |
| 前期成本 | 无需额外基础设施 | 需要私有 Registry 或 Docker Hub |
| 适用阶段 | **开发/试运行**（推荐当前使用） | 正式生产/多节点部署 |

---

## 四、详细操作步骤

### 步骤 1: 准备环境配置文件

```bash
# 复制模板
cp .env.example .env

# 编辑配置（必须修改以下项）
vim .env
```

**必须修改的配置项：**

| 配置项 | 说明 | 示例 |
|--------|------|------|
| `CRED_DEFAULT_USERNAME` | 网络设备 SSH 用户名 | `netdevops` |
| `CRED_DEFAULT_PASSWORD` | 网络设备 SSH 密码 | `YourPassword!` |
| `API_TOKEN` | API 认证 Token (Base64) | `echo -n 'your-secret' \| base64` |

**Docker 部署中的特殊配置项：**

| 配置项 | 推荐值 | 说明 |
|--------|--------|------|
| `DB_URL` | `sqlite:///./data/inspection.db` | 使用 `/app/data/` 子目录，便于卷挂载 |
| `RESULT_DIR` | `/app/results` | 容器内路径，与 docker-compose 卷对应 |
| `LOG_DIR` | `/app/logs` | 容器内路径，与 docker-compose 卷对应 |
| `ENVIRONMENT` | `production` | 生产模式下 API_TOKEN 必填 |

> ⚠️ Docker 部署中 `DB_URL`、`RESULT_DIR`、`LOG_DIR` 的值已在 docker-compose.yml 的 `environment` 中覆盖，
> **无需在 .env 中手动修改**，保持 .env.example 默认值即可。

### 步骤 2: 准备设备清单

设备清单文件位于 `inventory/devices.csv`，格式如下：

```csv
name,mgmt_ip,vendor,device_type,username,password,enable
SW-Core-01,10.1.1.1,h3c,switch,,,,
FW-Edge-01,10.1.1.2,huawei,firewall,,,,
```

- `username` / `password` / `enable` 列留空时，使用 `.env` 中的默认凭据
- 如果需要从宿主机挂载自定义清单，docker-compose.yml 已配置 `./inventory:/app/inventory:ro`

### 步骤 3: 构建 Docker 镜像

```bash
# 构建镜像
docker compose build

# 查看构建的镜像
docker images | grep netaegis
```

**镜像构建细节：**
- 多阶段构建，最终镜像基于 `python:3.10-slim`
- 使用 `uv` 安装依赖（比 pip 快 10-100 倍）
- 运行时包含 `openssh-client`（用于 SSH 连接）
- 非 root 用户 (`appuser`) 运行服务
- 镜像大小约 300-500 MB

### 步骤 4: 启动服务

```bash
# 前台启动（方便查看日志调试）
docker compose up

# 后台启动（生产推荐）
docker compose up -d
```

### 步骤 5: 验证服务状态

```bash
# 检查容器状态
docker compose ps

# 健康检查
curl http://localhost:8080/health

# 预期返回：
# {
#   "status": "ok",
#   "db": {"ok": true},
#   "scheduler": {"ok": true, "jobs": 0},
#   "version": "0.2.0"
# }

# 查看 API 文档（浏览器打开）
# http://localhost:8080/docs
```

### 步骤 6: 调用 API 接口

```bash
# 设置 Token（来自 .env 中的 API_TOKEN）
TOKEN="你的API_TOKEN"

# 查看可用巡检命令
curl -s -H "Authorization: Bearer $TOKEN" \
  http://localhost:8080/api/commands | python3 -m json.tool

# 预览设备清单
curl -s -H "Authorization: Bearer $TOKEN" \
  -X POST http://localhost:8080/api/inventory/preview \
  -H "Content-Type: application/json" \
  -d '{"source": "csv", "path": "inventory/devices.csv"}' | python3 -m json.tool

# 创建巡检任务
curl -s -H "Authorization: Bearer $TOKEN" \
  -X POST http://localhost:8080/api/jobs \
  -H "Content-Type: application/json" \
  -d '{
    "type": "inspect",
    "inventory_source": "csv",
    "inventory_path": "inventory/devices.csv",
    "concurrency": 10
  }' | python3 -m json.tool

# 创建配置备份任务
curl -s -H "Authorization: Bearer $TOKEN" \
  -X POST http://localhost:8080/api/jobs \
  -H "Content-Type: application/json" \
  -d '{
    "type": "backup",
    "inventory_source": "csv",
    "inventory_path": "inventory/devices.csv",
    "concurrency": 5
  }' | python3 -m json.tool

# 创建定时调度（每天 08:00 巡检）
curl -s -H "Authorization: Bearer $TOKEN" \
  -X POST http://localhost:8080/api/schedules \
  -H "Content-Type: application/json" \
  -d '{
    "name": "daily-inspect",
    "trigger_type": "cron",
    "trigger_args": {"hour": 8, "minute": 0},
    "job_type": "inspect",
    "inventory_source": "csv",
    "inventory_path": "inventory/devices.csv",
    "concurrency": 10
  }' | python3 -m json.tool
```

---

## 五、数据持久化与卷管理

docker-compose.yml 使用命名卷持久化数据：

| 卷名 | 容器路径 | 说明 |
|------|----------|------|
| `netaegis-data` | `/app/data` | SQLite 数据库 |
| `netaegis-results` | `/app/results` | 巡检结果（日志 + JSON） |
| `netaegis-logs` | `/app/logs` | 应用运行日志 |
| `netaegis-backups` | `/app/backups` | 设备配置备份 |

**卷操作命令：**

```bash
# 查看卷列表
docker volume ls | grep netaegis

# 查看卷详情
docker volume inspect netaegis-data

# 备份数据库（卷内文件 → 宿主机）
docker run --rm -v netaegis-data:/data -v $(pwd):/backup \
  alpine tar czf /backup/netaegis-db-$(date +%Y%m%d).tar.gz -C /data .

# 恢复数据库
docker run --rm -v netaegis-data:/data -v $(pwd):/backup \
  alpine tar xzf /backup/netaegis-db-20260622.tar.gz -C /data
```

> 💡 **如果希望数据直接存储在宿主机指定目录**，可修改 docker-compose.yml 中的卷定义为绑定挂载：
> ```yaml
> volumes:
>   - /data/netaegis/db:/app/data
>   - /data/netaegis/results:/app/results
>   - /data/netaegis/logs:/app/logs
>   - /data/netaegis/backups:/app/backups
> ```

---

## 六、网络配置

### 场景 1: 默认 Bridge 网络（推荐）

容器使用自定义 bridge 网络 `netaegis-net`，通过 Docker NAT 访问外部设备。适用于设备可通过 IP 路由到达的场景。

### 场景 2: Host 网络模式

如果设备与 Docker 宿主机在同一二层网络，或有特殊路由需求，可改用 host 网络：

```yaml
# docker-compose.yml 中修改
services:
  netaegis-api:
    network_mode: host
    # 删除 ports 映射（host 模式下不需要）
    # 删除 networks 配置
```

### 场景 3: 指定网络接口

如果需要容器使用特定宿主机网卡访问设备：

```yaml
services:
  netaegis-api:
    networks:
      - device-net

networks:
  device-net:
    name: device-net
    driver: bridge
    driver_opts:
      # 指定宿主机网络接口
      parent: eth1
    ipam:
      config:
        - subnet: 10.1.0.0/24
```

---

## 七、生产环境加固

### 1. 限制 API 访问来源

```yaml
# docker-compose.yml 中增加
ports:
  - "127.0.0.1:8080:8080"  # 仅本机访问
```

配合 Nginx 反向代理：

```nginx
server {
    listen 80;
    server_name netaegis.example.com;

    location / {
        proxy_pass http://127.0.0.1:8080;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

### 2. 启用 HTTPS

建议在 Nginx 层使用 Let's Encrypt 或自签证书：

```bash
# 使用 certbot 自动签发
certbot --nginx -d netaegis.example.com
```

### 3. 日志收集

将日志格式切换为 JSON，便于接入 ELK / Loki 等日志系统：

```bash
# .env 中设置
LOG_FORMAT=json
```

### 4. 资源限制调整

根据巡检规模调整资源：

| 设备规模 | CPU | 内存 | 并发数 |
|----------|-----|------|--------|
| ≤ 50 台 | 1 核 | 512MB | 10 |
| 50-200 台 | 2 核 | 1GB | 20 |
| 200-1000 台 | 4 核 | 2GB | 40 |

修改 docker-compose.yml 中的 `deploy.resources`。

---

## 八、运维操作

### 日常命令速查

```bash
# 启动 / 停止 / 重启
docker compose up -d
docker compose down
docker compose restart

# 查看实时日志
docker compose logs -f netaegis-api

# 查看最近 100 行日志
docker compose logs --tail 100 netaegis-api

# 进入容器调试
docker compose exec netaegis-api bash

# 重新构建并启动（代码更新后）
docker compose up -d --build

# 清理旧镜像
docker image prune -f
```

### 数据库迁移/升级

```bash
# 升级版本后，数据库表会通过 SQLAlchemy create_all 自动创建
# 如需重置数据库（⚠️ 清空所有数据）:
docker compose down
docker volume rm netaegis-data
docker compose up -d
```

### 监控告警

利用内置 `/health` 端点进行存活监控：

```bash
# cron 定期检查（每 5 分钟）
*/5 * * * * curl -fs http://localhost:8080/health || echo "NetAegis DOWN" | mail -s "Alert" admin@example.com
```

---

## 九、故障排查

| 问题 | 检查方法 | 解决方案 |
|------|----------|----------|
| 容器启动失败 | `docker compose logs netaegis-api` | 检查 .env 配置，特别是 API_TOKEN |
| SSH 连接超时 | `docker compose exec netaegis-api bash` → `ssh user@device_ip` | 检查网络可达性，必要时使用 host 网络模式 |
| 旧设备 RSA 密钥被拒 | 设置 `SSH_ALLOW_LEGACY_RSA=true` | 或升级设备 SSH 密钥 |
| 数据库锁定 | 查看日志是否有 "database is locked" | SQLite WAL 模式已启用，并发写入有限制；大规模部署建议迁移 PostgreSQL |
| 磁盘空间不足 | `docker system df` | 清理旧日志：`docker compose exec netaegis-api find /app/logs -mtime +30 -delete` |
| 权限拒绝 | `docker compose logs netaegis-api` | 检查 Bearer Token 是否正确 |

---

## 十、架构示意

```
┌───────────────────────────────────────────────────┐
│                   Docker Host                      │
│                                                    │
│  ┌─────────────────────────────────────────────┐  │
│  │         netaegis-api Container               │  │
│  │                                              │  │
│  │  ┌──────────┐  ┌─────────┐  ┌────────────┐ │  │
│  │  │ FastAPI   │  │APScheduler│ │ Nornir/   │ │  │
│  │  │ +Uvicorn  │  │ Scheduler │ │ Netmiko    │ │  │
│  │  └────┬─────┘  └────┬─────┘  └─────┬──────┘ │  │
│  │       │              │              │        │  │
│  │  ┌────┴─────┐  ┌────┴─────┐        │        │  │
│  │  │ SQLite   │  │ APS Job  │    SSH  │        │  │
│  │  │ (WAL)    │  │ Store    │        │        │  │
│  │  └──────────┘  └──────────┘        │        │  │
│  └─────────────────────────────────────┼────────┘  │
│                                        │           │
│  Named Volumes:                        │           │
│  ├── netaegis-data    (/app/data)      │           │
│  ├── netaegis-results (/app/results)   │           │
│  ├── netaegis-logs    (/app/logs)      │           │
│  └── netaegis-backups (/app/backups)   │           │
│                                        │           │
│  Port: 8080 ←───────────────────       │           │
│                                        ▼           │
│                              Network Devices        │
│                            (10.x.x.x:22 SSH)       │
└───────────────────────────────────────────────────┘
```

---

## 十一、升级流程

```bash
# 1. 本地增量同步代码
rsync -avz --delete \
  --exclude='.git' --exclude='.env' --exclude='*.db' \
  --exclude='results' --exclude='logs' --exclude='backups' \
  --exclude='.venv' --exclude='__pycache__' \
  ./ user@server:/opt/netaegis/

# 2. 备份数据库（在服务器上执行）
ssh user@server
docker run --rm -v netaegis-data:/data -v $(pwd):/backup \
  alpine tar czf /backup/netaegis-db-backup-$(date +%Y%m%d).tar.gz -C /data .

# 3. 重新构建并启动
cd /opt/netaegis && docker compose up -d --build

# 4. 验证
curl http://localhost:8080/health
```
