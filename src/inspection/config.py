"""集中加载 .env 配置。所有凭据只通过 Settings 获取，禁止写入清单或代码。

P0 改进：
- 新增 environment 字段：production 下 API_TOKEN 必填，防止 fail-open。
- 新增 default_inventory_path 消除硬编码。
- CredentialProfile 改为 dataclass（P3-21），类型安全的凭据访问。
- 新增 db_pool_size / db_max_overflow 控制连接池。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_ENV_FILES = (_PROJECT_ROOT / ".env",)


@dataclass
class CredentialProfile:
    """{username, password, enable} 三元组。P3-21：从 dict 改为 dataclass，类型安全。"""
    username: str = ""
    password: str = field(default="", repr=False)
    enable: str = field(default="", repr=False)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=_ENV_FILES, env_file_encoding="utf-8", extra="ignore")

    base_dir: Path = Field(default=_PROJECT_ROOT)

    # P0-2: 部署环境标识，production 下 API_TOKEN 必填。
    environment: Literal["development", "production"] = "production"

    # 默认凭据
    cred_default_username: str = Field(default="")
    cred_default_password: str = Field(default="", repr=False)
    cred_default_enable: str = Field(default="", repr=False)

    # 备份专用凭据（可选）
    cred_backup_username: str = ""
    cred_backup_password: str = Field(default="", repr=False)

    # 存储
    db_url: str = "sqlite:///./inspection.db"
    # P2-12: 连接池配置（SQLite 不需要大池，但为 PostgreSQL 预留）。
    db_pool_size: int = 5
    db_max_overflow: int = 10
    result_dir: Path = Field(default_factory=lambda: _PROJECT_ROOT / "results")
    log_dir: Path = Field(default_factory=lambda: _PROJECT_ROOT / "logs")

    # P3-16: 统一默认 CSV 路径，消除代码中 6 处硬编码。
    default_inventory_path: str = "inventory/devices.csv"

    # 运行参数
    default_concurrency: int = 20
    default_cmd_timeout: int = 60
    # P2: 单设备整体执行超时（秒），0 表示不限制。
    device_run_timeout: int = 0

    # 日志
    log_level: str = "INFO"
    log_format: Literal["text", "json"] = "text"

    # SSH 兼容性
    ssh_allow_legacy_rsa: bool = False
    ssh_debug: bool = False

    # API
    api_token: str = Field(default="", repr=False)
    api_host: str = "0.0.0.0"
    api_port: int = 8080

    def credential(self, profile: str = "default") -> CredentialProfile:
        if profile == "default":
            return CredentialProfile(
                username=self.cred_default_username,
                password=self.cred_default_password,
                enable=self.cred_default_enable,
            )
        if profile == "backup":
            return CredentialProfile(
                username=self.cred_backup_username or self.cred_default_username,
                password=self.cred_backup_password or self.cred_default_password,
                enable=self.cred_default_enable,
            )
        raise KeyError(f"未知凭据 profile: {profile}")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    settings = Settings()
    base_dir = settings.base_dir
    if not base_dir.is_absolute():
        base_dir = _PROJECT_ROOT / base_dir
    settings.base_dir = base_dir
    if not settings.result_dir.is_absolute():
        settings.result_dir = base_dir / settings.result_dir
    if not settings.log_dir.is_absolute():
        settings.log_dir = base_dir / settings.log_dir
    return settings
