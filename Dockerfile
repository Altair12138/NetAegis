# ===========================================================================
# NetAegis API 服务 — 多阶段构建 Dockerfile
# ===========================================================================
# 构建:  docker build -t netaegis:latest .
# 运行:  docker compose up -d
# ===========================================================================

# ---------- 阶段 1: 构建 ----------
FROM python:3.10-slim AS builder

# 安装 uv（比 pip 快 10-100x）
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /build

# 先复制依赖声明和源码（hatchling 构建 wheel 需要源码）
COPY pyproject.toml ./
COPY src/inspection/ src/inspection/

# 用 uv 创建 venv 并安装依赖
RUN uv venv /opt/venv && \
    uv pip install --python /opt/venv/bin/python .

# ---------- 阶段 2: 运行 ----------
FROM python:3.10-slim AS runtime

LABEL maintainer="NetAegis Team"
LABEL description="Network Inspection & Backup Platform (Nornir + Netmiko + FastAPI)"

# 安装运行时系统依赖（SSH 客户端、时区数据）
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        openssh-client \
        tzdata \
        curl && \
    rm -rf /var/lib/apt/lists/*

# 从 builder 阶段复制 venv
COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
ENV VIRTUAL_ENV=/opt/venv

# 创建非 root 用户运行服务
RUN groupadd -r appuser && useradd -r -g appuser -d /app -s /sbin/nologin appuser

WORKDIR /app

# 复制项目源码
COPY src/ src/
COPY inventory/ inventory/
COPY pyproject.toml ./

# 创建数据目录并设置权限
RUN mkdir -p /app/data /app/results /app/logs /app/backups && \
    chown -R appuser:appuser /app

# 切换到非 root 用户
USER appuser

# 环境变量默认值（可通过 docker-compose / .env 覆盖）
ENV ENVIRONMENT=production \
    DB_URL=sqlite:///./data/inspection.db \
    RESULT_DIR=/app/results \
    LOG_DIR=/app/logs \
    API_HOST=0.0.0.0 \
    API_PORT=8080 \
    LOG_LEVEL=INFO \
    TZ=Asia/Shanghai

# 健康检查
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -fs http://localhost:${API_PORT}/health || exit 1

# 暴露端口
EXPOSE 8080

# 启动命令
CMD ["uvicorn", "inspection.api.main:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1"]
