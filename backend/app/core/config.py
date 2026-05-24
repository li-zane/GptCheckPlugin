from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "sub2api AT 刷新机"
    app_env: Literal["development", "production", "test"] = "development"
    app_admin_key: str = Field(default="change-me-now")
    app_session_secret: str = Field(default="change-me-session-secret")
    app_encryption_key: str = Field(default="change-me-encryption-key")
    app_session_ttl_seconds: int = 60 * 60 * 12
    cookie_secure: bool = False
    cors_origins: list[str] = Field(default_factory=lambda: ["http://localhost:5173"])
    display_timezone: str = "Asia/Shanghai"

    database_url: str = "sqlite+aiosqlite:///./data/sub2api_at_guardian.db"

    sub2api_base_url: str = "http://localhost:8080/api/v1"
    sub2api_auth_token: str = ""
    sub2api_auth_header: str = "Authorization"
    sub2api_auth_scheme: str = "Bearer"
    sub2api_accounts_path: str = "/admin/accounts"
    sub2api_access_token_path: str = "credentials.access_token"
    sub2api_auto_clear_error: bool = True
    sub2api_auto_recover_state: bool = True
    sub2api_scan_ports: list[int] = Field(
        default_factory=lambda: [
            8080,
            8081,
            8000,
            8001,
            3000,
            3001,
            5000,
            5001,
            7860,
            9000,
            9001,
            18080,
            18081,
        ]
    )
    sub2api_scan_timeout_seconds: float = 0.8

    monitor_enabled: bool = True
    monitor_interval_seconds: int = 300
    monitor_page_size: int = 100
    usage_refresh_enabled: bool = False
    usage_refresh_interval_seconds: int = 3600
    refresh_max_concurrency: int = 1

    playwright_headless: bool = True
    playwright_slow_mo_ms: int = 0
    playwright_timeout_ms: int = 90_000
    chatgpt_base_url: str = "https://chatgpt.com"
    chatgpt_session_url: str = "https://chatgpt.com/api/auth/session"
    verification_code_timeout_seconds: int = 180
    verification_code_poll_seconds: int = 6
    verification_code_lookup_grace_seconds: int = 900

    graph_token_url: str = "https://login.microsoftonline.com/common/oauth2/v2.0/token"
    graph_consumer_token_url: str = "https://login.microsoftonline.com/consumers/oauth2/v2.0/token"
    graph_messages_url: str = "https://graph.microsoft.com/v1.0/me/mailFolders/inbox/messages"
    graph_scope: str = "https://graph.microsoft.com/Mail.Read offline_access"
    outlook_imap_scope: str = "https://outlook.office.com/IMAP.AccessAsUser.All offline_access"
    outlook_imap_host: str = "outlook.office365.com"
    outlook_imap_hosts: list[str] = Field(default_factory=lambda: ["outlook.office365.com", "imap-mail.outlook.com"])
    outlook_imap_port: int = 993
    outlook_imap_timeout_seconds: int = 6
    outlook_imap_failure_cooldown_seconds: int = 30
    mail_token_timeout_seconds: int = 10
    mail_read_timeout_seconds: int = 45
    live_oauth_token_url: str = "https://login.live.com/oauth20_token.srf"
    live_oauth_scope: str = "wl.offline_access wl.imap"
    external_mail_api_base: str = "https://www.appleemail.top"
    external_mail_timeout_seconds: int = 30

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    @field_validator("sub2api_base_url")
    @classmethod
    def strip_trailing_slash(cls, value: str) -> str:
        return value.rstrip("/")

    @field_validator("sub2api_accounts_path")
    @classmethod
    def ensure_leading_slash(cls, value: str) -> str:
        if not value.startswith("/"):
            return f"/{value}"
        return value

    @property
    def project_root(self) -> Path:
        return Path(__file__).resolve().parents[3]

    @property
    def session_cookie_name(self) -> str:
        return "sub2api_at_guardian_session"


@lru_cache
def get_settings() -> Settings:
    return Settings()
