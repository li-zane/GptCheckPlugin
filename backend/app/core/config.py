from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.core.sub2api_urls import normalize_sub2api_base_url


PROJECT_ROOT = Path(__file__).resolve().parents[3]
MIN_PRODUCTION_SECRET_LENGTH = 32


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
    mail_manager_route_config_path: str = ""

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
    automation_paused: bool = False
    oauth_account_sync_enabled: bool = True
    recovery_enabled: bool = False
    monitor_interval_seconds: int = 300
    monitor_page_size: int = 100
    usage_refresh_enabled: bool = False
    usage_refresh_interval_seconds: int = 3600
    usage_refresh_max_concurrency: int = 20
    api_key_account_sync_enabled: bool = True
    api_key_account_sync_interval_seconds: int = 300
    upstream_sync_enabled: bool | None = None
    upstream_sync_interval_seconds: int = 900
    upstream_sync_max_concurrency: int = 10
    upstream_rate_sync_enabled: bool = False
    upstream_priority_sync_enabled: bool = True
    manual_upstream_sync_rate_enabled: bool = True
    manual_upstream_sync_priority_enabled: bool = True
    manual_upstream_sync_upstream_health_enabled: bool = True
    manual_upstream_sync_channel_monitors_enabled: bool = True
    manual_upstream_sync_account_availability_enabled: bool = False
    manual_upstream_sync_balance_guard_enabled: bool = True
    manual_upstream_sync_rate_pause_enabled: bool = True
    api_key_auto_disable_on_upstream_unavailable: bool = False
    api_key_auto_pause_on_channel_monitor_unavailable_enabled: bool = False
    channel_monitor_auto_probe_enabled: bool = True
    account_model_whitelist_sync_enabled: bool = False
    account_model_whitelist_sync_interval_seconds: int = 3600
    account_model_whitelist_sync_each_time: bool = False
    channel_monitor_unavailable_consecutive_threshold: int = Field(default=2, ge=1, le=100)
    channel_monitor_recovery_consecutive_threshold: int = Field(default=2, ge=1, le=100)
    channel_monitor_fallback_without_monitor_enabled: bool = False
    channel_monitor_fallback_test_models: list[str] = Field(default_factory=list)
    channel_monitor_fallback_test_model: str = ""
    channel_monitor_fallback_test_attempts: int = Field(default=1, ge=1, le=5)
    channel_monitor_recovery_test_attempts: int = Field(default=1, ge=1, le=5)
    channel_monitor_test_attempt_interval_seconds: int = Field(default=0, ge=0, le=300)
    api_key_auto_pause_on_negative_balance_enabled: bool = False
    upstream_negative_balance_basis: Literal["wallet", "recharge_adjusted"] = "wallet"
    upstream_balance_pause_threshold: float = Field(
        default=0.0,
        ge=-1_000_000_000,
        le=1_000_000_000,
        allow_inf_nan=False,
    )
    show_stale_negative_balance_alert: bool = True
    priority_assign_disabled_api_key_accounts: bool = False
    priority_share_same_composite_multiplier: bool = False
    upstream_rate_log_retention_days: int = Field(default=90, ge=1, le=3650)
    upstream_usage_data_retention_days: int = Field(default=90, ge=1, le=3650)
    discord_bot_notifications_enabled: bool = False
    discord_bot_token: str = ""
    discord_bot_channel_id: str = ""
    notify_oauth_account_disabled: bool = False
    notify_account_enabled: bool = False
    notify_api_key_rate_changed: bool = False
    notify_upstream_group_changed: bool = False
    notify_upstream_balance_low: bool = False
    notify_upstream_token_invalid: bool = False
    usage_limit_sample_five_hour_threshold_percent: float = 0.0
    usage_limit_sample_seven_day_threshold_percent: float = 0.0
    usage_limit_default_ranges_json: str = ""
    refresh_max_concurrency: int = 2
    protocol_refresh_max_concurrency: int | None = None
    browser_refresh_max_concurrency: int = 1
    browser_min_available_memory_mb: int = 500
    subscription_refresh_batch_size: int = 3
    subscription_refresh_max_concurrency: int = 3
    account_liveness_max_concurrency: int = 3

    playwright_headless: bool = True
    playwright_slow_mo_ms: int = 0
    playwright_timeout_ms: int = 90_000
    chatgpt_base_url: str = "https://chatgpt.com"
    chatgpt_session_url: str = "https://chatgpt.com/api/auth/session"
    openai_oauth_authorize_url: str = "https://auth.openai.com/oauth/authorize"
    openai_oauth_token_url: str = "https://auth.openai.com/oauth/token"
    openai_oauth_client_id: str = "app_EMoamEEZ73f0CkXaXp7hrann"
    openai_oauth_redirect_uri: str = "http://localhost:1455/auth/callback"
    openai_oauth_scopes: str = "openid profile email offline_access"
    openai_oauth_user_agent: str = "codex_cli_rs/0.104.0"
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
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    @field_validator("sub2api_base_url")
    @classmethod
    def strip_trailing_slash(cls, value: str) -> str:
        return normalize_sub2api_base_url(value)

    @field_validator("sub2api_accounts_path")
    @classmethod
    def ensure_leading_slash(cls, value: str) -> str:
        if not value.startswith("/"):
            return f"/{value}"
        return value

    @model_validator(mode="after")
    def validate_production_security(self) -> "Settings":
        if self.app_env != "production":
            return self

        secret_fields = {
            "APP_ADMIN_KEY": self.app_admin_key,
            "APP_SESSION_SECRET": self.app_session_secret,
            "APP_ENCRYPTION_KEY": self.app_encryption_key,
        }
        for env_name, value in secret_fields.items():
            if (
                _is_placeholder_secret(value)
                or len(value.strip()) < MIN_PRODUCTION_SECRET_LENGTH
            ):
                raise ValueError(
                    f"{env_name} must be a non-default secret of at least "
                    f"{MIN_PRODUCTION_SECRET_LENGTH} characters in production"
                )
        if not self.cookie_secure:
            raise ValueError("COOKIE_SECURE must be true in production")
        return self

    @property
    def project_root(self) -> Path:
        return PROJECT_ROOT

    @property
    def session_cookie_name(self) -> str:
        return "sub2api_at_guardian_session"


@lru_cache
def get_settings() -> Settings:
    return Settings()


def _is_placeholder_secret(value: str) -> bool:
    normalized = value.strip().casefold()
    return not normalized or normalized in {
        "change-me-now",
        "change-me-session-secret",
        "change-me-encryption-key",
        "replace-with-a-long-random-admin-key",
        "replace-with-a-long-random-session-secret",
        "replace-with-a-long-random-encryption-key",
    }
