from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
from io import BytesIO
import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from time import monotonic
from typing import Any
from urllib.parse import urlparse

import httpx
from PIL import Image, UnidentifiedImageError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.crypto import decrypt_text, encrypt_text, redact
from app.core.database import AsyncSessionLocal
from app.models import AppSetting, utcnow
from app.core.subscription_types import normalize_usage_limit_ranges
from app.core.sub2api_urls import (
    is_loopback_sub2api_url,
    normalize_sub2api_base_url,
    replace_sub2api_port,
)
from app.services.upstream_rate_logs import delete_expired_upstream_rate_change_logs
from app.services.change_logs import delete_expired_change_logs
from app.services.upstream_usage_history import prune_upstream_usage_history


KEY_SUB2API_BASE_URL = "sub2api_base_url"
KEY_SUB2API_BASE_URL_SOURCE = "sub2api_base_url_source"
KEY_SUB2API_X_API_KEY = "sub2api_x_api_key"
KEY_SUB2API_AUTO_RECOVER_STATE = "sub2api_auto_recover_state"
KEY_OAUTH_ACCOUNT_SYNC_ENABLED = "oauth_account_sync_enabled"
KEY_MONITOR_INTERVAL_SECONDS = "monitor_interval_seconds"
KEY_USAGE_REFRESH_ENABLED = "usage_refresh_enabled"
KEY_USAGE_REFRESH_INTERVAL_SECONDS = "usage_refresh_interval_seconds"
KEY_USAGE_REFRESH_MAX_CONCURRENCY = "usage_refresh_max_concurrency"
KEY_API_KEY_ACCOUNT_SYNC_ENABLED = "api_key_account_sync_enabled"
KEY_API_KEY_ACCOUNT_SYNC_INTERVAL_SECONDS = "api_key_account_sync_interval_seconds"
KEY_UPSTREAM_SYNC_ENABLED = "upstream_sync_enabled"
KEY_UPSTREAM_SYNC_INTERVAL_SECONDS = "upstream_sync_interval_seconds"
KEY_UPSTREAM_SYNC_MAX_CONCURRENCY = "upstream_sync_max_concurrency"
KEY_UPSTREAM_RATE_SYNC_ENABLED = "upstream_rate_sync_enabled"
KEY_UPSTREAM_PRIORITY_SYNC_ENABLED = "upstream_priority_sync_enabled"
KEY_MANUAL_UPSTREAM_SYNC_RATE_ENABLED = "manual_upstream_sync_rate_enabled"
KEY_MANUAL_UPSTREAM_SYNC_PRIORITY_ENABLED = "manual_upstream_sync_priority_enabled"
KEY_MANUAL_UPSTREAM_SYNC_UPSTREAM_HEALTH_ENABLED = (
    "manual_upstream_sync_upstream_health_enabled"
)
KEY_MANUAL_UPSTREAM_SYNC_CHANNEL_MONITORS_ENABLED = (
    "manual_upstream_sync_channel_monitors_enabled"
)
KEY_MANUAL_UPSTREAM_SYNC_ACCOUNT_AVAILABILITY_ENABLED = (
    "manual_upstream_sync_account_availability_enabled"
)
KEY_MANUAL_UPSTREAM_SYNC_BALANCE_GUARD_ENABLED = (
    "manual_upstream_sync_balance_guard_enabled"
)
KEY_MANUAL_UPSTREAM_SYNC_RATE_PAUSE_ENABLED = (
    "manual_upstream_sync_rate_pause_enabled"
)
KEY_API_KEY_AUTO_DISABLE_ON_UPSTREAM_UNAVAILABLE = (
    "api_key_auto_disable_on_upstream_unavailable"
)
KEY_API_KEY_AUTO_PAUSE_ON_CHANNEL_MONITOR_UNAVAILABLE_ENABLED = (
    "api_key_auto_pause_on_channel_monitor_unavailable_enabled"
)
KEY_API_KEY_AVAILABILITY_ALL_TESTS_MUST_SUCCEED = (
    "api_key_availability_all_tests_must_succeed"
)
KEY_CHANNEL_MONITOR_AUTO_PROBE_ENABLED = "channel_monitor_auto_probe_enabled"
KEY_ACCOUNT_MODEL_WHITELIST_SYNC_ENABLED = "account_model_whitelist_sync_enabled"
KEY_ACCOUNT_MODEL_WHITELIST_SYNC_INTERVAL_SECONDS = "account_model_whitelist_sync_interval_seconds"
KEY_ACCOUNT_MODEL_WHITELIST_SYNC_EACH_TIME = "account_model_whitelist_sync_each_time"
KEY_CHANNEL_MONITOR_UNAVAILABLE_CONSECUTIVE_THRESHOLD = (
    "channel_monitor_unavailable_consecutive_threshold"
)
KEY_CHANNEL_MONITOR_RECOVERY_CONSECUTIVE_THRESHOLD = (
    "channel_monitor_recovery_consecutive_threshold"
)
KEY_CHANNEL_MONITOR_FALLBACK_WITHOUT_MONITOR_ENABLED = (
    "channel_monitor_fallback_without_monitor_enabled"
)
KEY_CHANNEL_MONITOR_FALLBACK_TEST_MODELS = "channel_monitor_fallback_test_models"
KEY_CHANNEL_MONITOR_FALLBACK_TEST_MODEL = "channel_monitor_fallback_test_model"
KEY_CHANNEL_MONITOR_FALLBACK_TEST_ATTEMPTS = "channel_monitor_fallback_test_attempts"
KEY_CHANNEL_MONITOR_RECOVERY_TEST_ATTEMPTS = "channel_monitor_recovery_test_attempts"
KEY_CHANNEL_MONITOR_TEST_ATTEMPT_INTERVAL_SECONDS = (
    "channel_monitor_test_attempt_interval_seconds"
)
KEY_API_KEY_AUTO_PAUSE_ON_NEGATIVE_BALANCE_ENABLED = (
    "api_key_auto_pause_on_negative_balance_enabled"
)
KEY_UPSTREAM_NEGATIVE_BALANCE_BASIS = "upstream_negative_balance_basis"
KEY_UPSTREAM_BALANCE_PAUSE_THRESHOLD = "upstream_balance_pause_threshold"
KEY_SHOW_STALE_NEGATIVE_BALANCE_ALERT = "show_stale_negative_balance_alert"
KEY_PRIORITY_ASSIGN_DISABLED_API_KEY_ACCOUNTS = (
    "priority_assign_disabled_api_key_accounts"
)
KEY_PRIORITY_SHARE_SAME_COMPOSITE_MULTIPLIER = (
    "priority_share_same_composite_multiplier"
)
KEY_DISCORD_BOT_NOTIFICATIONS_ENABLED = "discord_bot_notifications_enabled"
KEY_DISCORD_BOT_TOKEN = "discord_bot_token"
KEY_DISCORD_BOT_CHANNEL_ID = "discord_bot_channel_id"
KEY_NOTIFY_OAUTH_ACCOUNT_DISABLED = "notify_oauth_account_disabled"
KEY_NOTIFY_ACCOUNT_ENABLED = "notify_account_enabled"
KEY_NOTIFY_API_KEY_RATE_CHANGED = "notify_api_key_rate_changed"
KEY_NOTIFY_UPSTREAM_GROUP_CHANGED = "notify_upstream_group_changed"
KEY_NOTIFY_UPSTREAM_BALANCE_LOW = "notify_upstream_balance_low"
KEY_NOTIFY_UPSTREAM_TOKEN_INVALID = "notify_upstream_token_invalid"
KEY_UPSTREAM_RATE_LOG_RETENTION_DAYS = "upstream_rate_log_retention_days"
KEY_UPSTREAM_USAGE_DATA_RETENTION_DAYS = "upstream_usage_data_retention_days"
KEY_CHANGE_LOG_PAGE_SIZE = "change_log_page_size"
KEY_CHANGE_LOG_PAGE_SIZE_OPTIONS = "change_log_page_size_options"
KEY_USAGE_LIMIT_SAMPLE_FIVE_HOUR_THRESHOLD_PERCENT = "usage_limit_sample_five_hour_threshold_percent"
KEY_USAGE_LIMIT_SAMPLE_SEVEN_DAY_THRESHOLD_PERCENT = "usage_limit_sample_seven_day_threshold_percent"
KEY_USAGE_LIMIT_DEFAULT_RANGES = "usage_limit_default_ranges"
KEY_RECOVERY_ENABLED = "recovery_enabled"
KEY_OAUTH_LOGIN_MODE = "oauth_login_mode"
KEY_OAUTH_STOP_ON_PHONE_VERIFICATION = "oauth_stop_on_phone_verification"
KEY_REFRESH_MAX_CONCURRENCY = "refresh_max_concurrency"
KEY_PROTOCOL_REFRESH_MAX_CONCURRENCY = "protocol_refresh_max_concurrency"
KEY_BROWSER_REFRESH_MAX_CONCURRENCY = "browser_refresh_max_concurrency"
KEY_BROWSER_MIN_AVAILABLE_MEMORY_MB = "browser_min_available_memory_mb"
KEY_SUBSCRIPTION_REFRESH_BATCH_SIZE = "subscription_refresh_batch_size"
KEY_SUBSCRIPTION_REFRESH_MAX_CONCURRENCY = "subscription_refresh_max_concurrency"
KEY_ACCOUNT_LIVENESS_MAX_CONCURRENCY = "account_liveness_max_concurrency"
KEY_LAST_SCAN_AT = "sub2api_last_scan_at"
KEY_LAST_SCAN_STATUS = "sub2api_last_scan_status"
KEY_LAST_SCAN_MESSAGE = "sub2api_last_scan_message"
KEY_DISPLAY_TIMEZONE = "display_timezone"
KEY_SITE_NAME = "site_name"
KEY_SITE_LOGO_DATA = "site_logo_data"
KEY_SITE_LOGO_MIME = "site_logo_mime"
KEY_SITE_LOGO_UPDATED_AT = "site_logo_updated_at"
KEY_AUTOMATION_PAUSED = "automation_paused"

MAX_CONFIGURED_PROBE_RESPONSE_BYTES = 512 * 1024
COMMON_SUB2API_PORTS = (8080, 18080, 18090)
MAX_SITE_LOGO_BYTES = 1024 * 1024
MAX_SITE_LOGO_DIMENSION = 2048
MAX_SITE_LOGO_PIXELS = MAX_SITE_LOGO_DIMENSION * MAX_SITE_LOGO_DIMENSION


class RuntimeConfigServiceError(RuntimeError):
    def __init__(self, message: str, *, status_code: int = 422) -> None:
        super().__init__(message)
        self.public_message = message
        self.status_code = status_code


class _ProbeResponseRejected(RuntimeError):
    pass


@dataclass(frozen=True)
class EffectiveSub2ApiConfig:
    base_url: str
    auth_token: str
    auth_header: str
    auth_scheme: str
    accounts_path: str
    access_token_path: str
    auto_clear_error: bool
    auto_recover_state: bool


@dataclass(frozen=True)
class ProbeHit:
    base_url: str
    port: int
    status: str
    message: str


class RuntimeConfigService:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    async def get_sub2api_config(self) -> EffectiveSub2ApiConfig:
        values = await self._load_values()
        runtime_key = decrypt_text(values.get(KEY_SUB2API_X_API_KEY))
        if KEY_SUB2API_X_API_KEY in values:
            auth_token = runtime_key or ""
            auth_header = "x-api-key"
            auth_scheme = ""
        else:
            auth_token = self.settings.sub2api_auth_token
            auth_header = self.settings.sub2api_auth_header
            auth_scheme = self.settings.sub2api_auth_scheme

        return EffectiveSub2ApiConfig(
            base_url=_normalize_base_url(values.get(KEY_SUB2API_BASE_URL) or self.settings.sub2api_base_url),
            auth_token=auth_token,
            auth_header=auth_header,
            auth_scheme=auth_scheme,
            accounts_path=self.settings.sub2api_accounts_path,
            access_token_path=self.settings.sub2api_access_token_path,
            auto_clear_error=self.settings.sub2api_auto_clear_error,
            auto_recover_state=_bool_or_default(
                values.get(KEY_SUB2API_AUTO_RECOVER_STATE),
                self.settings.sub2api_auto_recover_state,
            ),
        )

    async def get_monitor_interval_seconds(self) -> int:
        values = await self._load_values()
        return _int_or_default(values.get(KEY_MONITOR_INTERVAL_SECONDS), self.settings.monitor_interval_seconds)

    async def get_oauth_account_sync_enabled(self) -> bool:
        values = await self._load_values()
        return _bool_or_default(
            values.get(KEY_OAUTH_ACCOUNT_SYNC_ENABLED),
            self.settings.oauth_account_sync_enabled,
        )

    async def get_automation_paused(self) -> bool:
        values = await self._load_values()
        return _bool_or_default(values.get(KEY_AUTOMATION_PAUSED), self.settings.automation_paused)

    async def get_recovery_enabled(self) -> bool:
        values = await self._load_values()
        return _bool_or_default(values.get(KEY_RECOVERY_ENABLED), self.settings.recovery_enabled)

    async def get_oauth_login_mode(self) -> str:
        values = await self._load_values()
        return _oauth_login_mode_or_default(
            values.get(KEY_OAUTH_LOGIN_MODE),
            getattr(self.settings, "oauth_login_mode", "protocol"),
        )

    async def get_oauth_stop_on_phone_verification(self) -> bool:
        values = await self._load_values()
        return _bool_or_default(
            values.get(KEY_OAUTH_STOP_ON_PHONE_VERIFICATION),
            bool(getattr(self.settings, "oauth_stop_on_phone_verification", False)),
        )

    async def get_usage_refresh_enabled(self) -> bool:
        values = await self._load_values()
        return _bool_or_default(values.get(KEY_USAGE_REFRESH_ENABLED), self.settings.usage_refresh_enabled)

    async def get_usage_refresh_interval_seconds(self) -> int:
        values = await self._load_values()
        return _int_or_default(
            values.get(KEY_USAGE_REFRESH_INTERVAL_SECONDS),
            self.settings.usage_refresh_interval_seconds,
        )

    async def get_usage_refresh_max_concurrency(self) -> int:
        values = await self._load_values()
        return _bounded_int_or_default(
            values.get(KEY_USAGE_REFRESH_MAX_CONCURRENCY),
            self.settings.usage_refresh_max_concurrency,
            0,
            100,
        )

    async def get_api_key_account_sync_enabled(self) -> bool:
        values = await self._load_values()
        return _bool_or_default(
            values.get(KEY_API_KEY_ACCOUNT_SYNC_ENABLED),
            self.settings.api_key_account_sync_enabled,
        )

    async def get_api_key_account_sync_interval_seconds(self) -> int:
        values = await self._load_values()
        return _bounded_int_or_default(
            values.get(KEY_API_KEY_ACCOUNT_SYNC_INTERVAL_SECONDS),
            self.settings.api_key_account_sync_interval_seconds,
            30,
            86_400,
        )

    async def get_upstream_sync_enabled(self) -> bool:
        values = await self._load_values()
        configured_default = self.settings.upstream_sync_enabled
        if configured_default is None:
            configured_default = _bool_or_default(
                values.get(KEY_UPSTREAM_RATE_SYNC_ENABLED),
                self.settings.upstream_rate_sync_enabled,
            )
        return _bool_or_default(values.get(KEY_UPSTREAM_SYNC_ENABLED), configured_default)

    async def get_upstream_sync_interval_seconds(self) -> int:
        values = await self._load_values()
        return _bounded_int_or_default(
            values.get(KEY_UPSTREAM_SYNC_INTERVAL_SECONDS),
            self.settings.upstream_sync_interval_seconds,
            60,
            86_400,
        )

    async def get_upstream_sync_max_concurrency(self) -> int:
        values = await self._load_values()
        return _bounded_int_or_default(
            values.get(KEY_UPSTREAM_SYNC_MAX_CONCURRENCY),
            self.settings.upstream_sync_max_concurrency,
            0,
            50,
        )

    async def get_upstream_rate_sync_enabled(self) -> bool:
        values = await self._load_values()
        return _bool_or_default(
            values.get(KEY_UPSTREAM_RATE_SYNC_ENABLED),
            self.settings.upstream_rate_sync_enabled,
        )

    async def get_upstream_priority_sync_enabled(self) -> bool:
        values = await self._load_values()
        return _bool_or_default(
            values.get(KEY_UPSTREAM_PRIORITY_SYNC_ENABLED),
            self.settings.upstream_priority_sync_enabled,
        )

    async def get_api_key_auto_disable_on_upstream_unavailable(self) -> bool:
        values = await self._load_values()
        return _bool_or_default(
            values.get(KEY_API_KEY_AUTO_DISABLE_ON_UPSTREAM_UNAVAILABLE),
            self.settings.api_key_auto_disable_on_upstream_unavailable,
        )

    async def get_api_key_auto_pause_on_channel_monitor_unavailable_enabled(self) -> bool:
        values = await self._load_values()
        return _bool_or_default(
            values.get(KEY_API_KEY_AUTO_PAUSE_ON_CHANNEL_MONITOR_UNAVAILABLE_ENABLED),
            bool(
                getattr(
                    self.settings,
                    "api_key_auto_pause_on_channel_monitor_unavailable_enabled",
                    False,
                )
            ),
        )

    async def get_api_key_availability_all_tests_must_succeed(self) -> bool:
        values = await self._load_values()
        return _bool_or_default(
            values.get(KEY_API_KEY_AVAILABILITY_ALL_TESTS_MUST_SUCCEED),
            bool(
                getattr(
                    self.settings,
                    "api_key_availability_all_tests_must_succeed",
                    False,
                )
            ),
        )

    async def get_channel_monitor_auto_probe_enabled(self) -> bool:
        values = await self._load_values()
        return _bool_or_default(
            values.get(KEY_CHANNEL_MONITOR_AUTO_PROBE_ENABLED),
            bool(getattr(self.settings, "channel_monitor_auto_probe_enabled", True)),
        )

    async def get_account_model_whitelist_sync_enabled(self) -> bool:
        values = await self._load_values()
        legacy_value = _bool_or_default(
            values.get(KEY_ACCOUNT_MODEL_WHITELIST_SYNC_EACH_TIME),
            bool(getattr(self.settings, "account_model_whitelist_sync_each_time", False)),
        )
        configured_default = bool(
            getattr(self.settings, "account_model_whitelist_sync_enabled", False)
        )
        if KEY_ACCOUNT_MODEL_WHITELIST_SYNC_ENABLED not in values:
            configured_default = legacy_value
        return _bool_or_default(
            values.get(KEY_ACCOUNT_MODEL_WHITELIST_SYNC_ENABLED),
            configured_default,
        )

    async def get_account_model_whitelist_sync_interval_seconds(self) -> int:
        values = await self._load_values()
        return _bounded_int_or_default(
            values.get(KEY_ACCOUNT_MODEL_WHITELIST_SYNC_INTERVAL_SECONDS),
            int(getattr(self.settings, "account_model_whitelist_sync_interval_seconds", 3600)),
            60,
            86_400,
        )

    async def get_account_model_whitelist_sync_each_time(self) -> bool:
        values = await self._load_values()
        return _bool_or_default(
            values.get(KEY_ACCOUNT_MODEL_WHITELIST_SYNC_EACH_TIME),
            bool(getattr(self.settings, "account_model_whitelist_sync_each_time", False)),
        )

    async def get_channel_monitor_unavailable_consecutive_threshold(self) -> int:
        values = await self._load_values()
        return _bounded_int_or_default(
            values.get(KEY_CHANNEL_MONITOR_UNAVAILABLE_CONSECUTIVE_THRESHOLD),
            int(
                getattr(
                    self.settings,
                    "channel_monitor_unavailable_consecutive_threshold",
                    2,
                )
            ),
            1,
            100,
        )

    async def get_channel_monitor_recovery_consecutive_threshold(self) -> int:
        values = await self._load_values()
        return _bounded_int_or_default(
            values.get(KEY_CHANNEL_MONITOR_RECOVERY_CONSECUTIVE_THRESHOLD),
            int(
                getattr(
                    self.settings,
                    "channel_monitor_recovery_consecutive_threshold",
                    2,
                )
            ),
            1,
            100,
        )

    async def get_channel_monitor_fallback_test_model(self) -> str:
        models = await self.get_channel_monitor_fallback_test_models()
        return models[0] if models else ""

    async def get_channel_monitor_fallback_without_monitor_enabled(self) -> bool:
        values = await self._load_values()
        return _bool_or_default(
            values.get(KEY_CHANNEL_MONITOR_FALLBACK_WITHOUT_MONITOR_ENABLED),
            bool(
                getattr(
                    self.settings,
                    "channel_monitor_fallback_without_monitor_enabled",
                    False,
                )
            ),
        )

    async def get_channel_monitor_fallback_test_models(self) -> list[str]:
        values = await self._load_values()
        stored = values.get(KEY_CHANNEL_MONITOR_FALLBACK_TEST_MODELS)
        configured = getattr(self.settings, "channel_monitor_fallback_test_models", [])
        models = _normalize_model_chain(stored if stored is not None else configured)
        if models:
            return models
        legacy = (
            values.get(KEY_CHANNEL_MONITOR_FALLBACK_TEST_MODEL)
            or getattr(self.settings, "channel_monitor_fallback_test_model", "")
            or ""
        )
        return _normalize_model_chain(legacy)

    async def get_channel_monitor_fallback_test_attempts(self) -> int:
        values = await self._load_values()
        return _bounded_int_or_default(
            values.get(KEY_CHANNEL_MONITOR_FALLBACK_TEST_ATTEMPTS),
            int(getattr(self.settings, "channel_monitor_fallback_test_attempts", 1)),
            1,
            5,
        )

    async def get_channel_monitor_recovery_test_attempts(self) -> int:
        values = await self._load_values()
        return _bounded_int_or_default(
            values.get(KEY_CHANNEL_MONITOR_RECOVERY_TEST_ATTEMPTS),
            int(getattr(self.settings, "channel_monitor_recovery_test_attempts", 1)),
            1,
            5,
        )

    async def get_channel_monitor_test_attempt_interval_seconds(self) -> int:
        values = await self._load_values()
        return _bounded_int_or_default(
            values.get(KEY_CHANNEL_MONITOR_TEST_ATTEMPT_INTERVAL_SECONDS),
            int(
                getattr(
                    self.settings,
                    "channel_monitor_test_attempt_interval_seconds",
                    0,
                )
            ),
            0,
            300,
        )

    async def get_api_key_auto_pause_on_negative_balance_enabled(self) -> bool:
        values = await self._load_values()
        return _bool_or_default(
            values.get(KEY_API_KEY_AUTO_PAUSE_ON_NEGATIVE_BALANCE_ENABLED),
            bool(getattr(self.settings, "api_key_auto_pause_on_negative_balance_enabled", False)),
        )

    async def get_upstream_negative_balance_basis(self) -> str:
        values = await self._load_values()
        value = str(
            values.get(KEY_UPSTREAM_NEGATIVE_BALANCE_BASIS)
            or getattr(self.settings, "upstream_negative_balance_basis", "wallet")
        ).strip()
        return value if value in {"wallet", "recharge_adjusted"} else "wallet"

    async def get_upstream_balance_pause_threshold(self) -> float:
        values = await self._load_values()
        return _bounded_float_or_default(
            values.get(KEY_UPSTREAM_BALANCE_PAUSE_THRESHOLD),
            float(getattr(self.settings, "upstream_balance_pause_threshold", 0.0)),
            -1_000_000_000.0,
            1_000_000_000.0,
        )

    async def get_show_stale_negative_balance_alert(self) -> bool:
        values = await self._load_values()
        return _bool_or_default(
            values.get(KEY_SHOW_STALE_NEGATIVE_BALANCE_ALERT),
            bool(getattr(self.settings, "show_stale_negative_balance_alert", True)),
        )

    async def get_priority_assign_disabled_api_key_accounts(self) -> bool:
        values = await self._load_values()
        return _bool_or_default(
            values.get(KEY_PRIORITY_ASSIGN_DISABLED_API_KEY_ACCOUNTS),
            bool(getattr(self.settings, "priority_assign_disabled_api_key_accounts", False)),
        )

    async def get_priority_share_same_composite_multiplier(self) -> bool:
        values = await self._load_values()
        return _bool_or_default(
            values.get(KEY_PRIORITY_SHARE_SAME_COMPOSITE_MULTIPLIER),
            bool(
                getattr(
                    self.settings,
                    "priority_share_same_composite_multiplier",
                    False,
                )
            ),
        )

    async def get_notification_config(self) -> dict[str, Any]:
        values = await self._load_values()
        if KEY_DISCORD_BOT_TOKEN in values:
            token = decrypt_text(values.get(KEY_DISCORD_BOT_TOKEN)) or ""
        else:
            token = str(getattr(self.settings, "discord_bot_token", "") or "").strip()
        account_disabled_enabled = _bool_or_default(
            values.get(KEY_NOTIFY_OAUTH_ACCOUNT_DISABLED),
            bool(getattr(self.settings, "notify_oauth_account_disabled", False)),
        )
        return {
            "enabled": _bool_or_default(
                values.get(KEY_DISCORD_BOT_NOTIFICATIONS_ENABLED),
                bool(getattr(self.settings, "discord_bot_notifications_enabled", False)),
            ),
            "oauth_account_disabled_enabled": account_disabled_enabled,
            "account_disabled_enabled": account_disabled_enabled,
            "account_enabled_enabled": _bool_or_default(
                values.get(KEY_NOTIFY_ACCOUNT_ENABLED),
                bool(getattr(self.settings, "notify_account_enabled", False)),
            ),
            "api_key_rate_changed_enabled": _bool_or_default(
                values.get(KEY_NOTIFY_API_KEY_RATE_CHANGED),
                bool(getattr(self.settings, "notify_api_key_rate_changed", False)),
            ),
            "upstream_group_changed_enabled": _bool_or_default(
                values.get(KEY_NOTIFY_UPSTREAM_GROUP_CHANGED),
                _bool_or_default(
                    values.get(KEY_NOTIFY_API_KEY_RATE_CHANGED),
                    bool(getattr(self.settings, "notify_upstream_group_changed", False)),
                ),
            ),
            "upstream_balance_low_enabled": _bool_or_default(
                values.get(KEY_NOTIFY_UPSTREAM_BALANCE_LOW),
                bool(getattr(self.settings, "notify_upstream_balance_low", False)),
            ),
            "upstream_channel_token_invalid_enabled": _bool_or_default(
                values.get(KEY_NOTIFY_UPSTREAM_TOKEN_INVALID),
                bool(getattr(self.settings, "notify_upstream_token_invalid", False)),
            ),
            "discord_bot_token": token,
            "discord_channel_id": (
                values.get(KEY_DISCORD_BOT_CHANNEL_ID)
                if KEY_DISCORD_BOT_CHANNEL_ID in values
                else getattr(self.settings, "discord_bot_channel_id", "")
            ) or "",
        }

    async def get_site_logo(self) -> tuple[bytes, str] | None:
        values = await self._load_values()
        encoded = values.get(KEY_SITE_LOGO_DATA)
        mime = values.get(KEY_SITE_LOGO_MIME)
        if not encoded or mime not in {"image/png", "image/jpeg", "image/webp"}:
            return None
        try:
            raw = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error):
            return None
        if not _valid_logo_bytes(raw, mime):
            return None
        return raw, mime

    async def set_site_logo(self, raw: bytes, mime: str) -> dict[str, Any]:
        normalized_mime = mime.split(";", 1)[0].strip().lower()
        if normalized_mime not in {"image/png", "image/jpeg", "image/webp"}:
            raise RuntimeConfigServiceError(
                "The site logo must be a PNG, JPEG, or WebP image."
            )
        raw = _normalize_logo_bytes(raw, normalized_mime)
        updated_at = utcnow().isoformat()
        async with AsyncSessionLocal() as db:
            await self._put(db, KEY_SITE_LOGO_DATA, base64.b64encode(raw).decode("ascii"))
            await self._put(db, KEY_SITE_LOGO_MIME, normalized_mime)
            await self._put(db, KEY_SITE_LOGO_UPDATED_AT, updated_at)
            await db.commit()
        return await self.get_public_settings()

    async def clear_site_logo(self) -> dict[str, Any]:
        async with AsyncSessionLocal() as db:
            await self._put(db, KEY_SITE_LOGO_DATA, None)
            await self._put(db, KEY_SITE_LOGO_MIME, None)
            await self._put(db, KEY_SITE_LOGO_UPDATED_AT, None)
            await db.commit()
        return await self.get_public_settings()

    async def get_upstream_rate_log_retention_days(self) -> int:
        values = await self._load_values()
        return _bounded_int_or_default(
            values.get(KEY_UPSTREAM_RATE_LOG_RETENTION_DAYS),
            self.settings.upstream_rate_log_retention_days,
            1,
            3650,
        )

    async def get_upstream_usage_data_retention_days(self) -> int:
        values = await self._load_values()
        return _bounded_int_or_default(
            values.get(KEY_UPSTREAM_USAGE_DATA_RETENTION_DAYS),
            int(getattr(self.settings, "upstream_usage_data_retention_days", 90)),
            1,
            3650,
        )

    async def get_change_log_page_size(self) -> int:
        values = await self._load_values()
        options = _change_log_page_size_options_or_default(
            values.get(KEY_CHANGE_LOG_PAGE_SIZE_OPTIONS),
            getattr(self.settings, "change_log_page_size_options", None),
        )
        return _change_log_page_size_or_default(
            values.get(KEY_CHANGE_LOG_PAGE_SIZE),
            options,
            int(getattr(self.settings, "change_log_page_size", 50)),
        )

    async def get_change_log_page_size_options(self) -> list[int]:
        values = await self._load_values()
        return _change_log_page_size_options_or_default(
            values.get(KEY_CHANGE_LOG_PAGE_SIZE_OPTIONS),
            getattr(self.settings, "change_log_page_size_options", None),
        )

    async def get_usage_limit_sample_thresholds(self) -> dict[str, float]:
        values = await self._load_values()
        return {
            "five_hour": _threshold_or_settings_default(
                values.get(KEY_USAGE_LIMIT_SAMPLE_FIVE_HOUR_THRESHOLD_PERCENT),
                self.settings.usage_limit_sample_five_hour_threshold_percent,
            ),
            "seven_day": _threshold_or_settings_default(
                values.get(KEY_USAGE_LIMIT_SAMPLE_SEVEN_DAY_THRESHOLD_PERCENT),
                self.settings.usage_limit_sample_seven_day_threshold_percent,
            ),
        }

    async def get_usage_limit_default_ranges(self) -> dict[str, dict[str, dict[str, float]]]:
        values = await self._load_values()
        stored = values.get(KEY_USAGE_LIMIT_DEFAULT_RANGES)
        return normalize_usage_limit_ranges(stored or self.settings.usage_limit_default_ranges_json)

    async def get_refresh_max_concurrency(self) -> int:
        return await self.get_protocol_refresh_max_concurrency()

    async def get_protocol_refresh_max_concurrency(self) -> int:
        values = await self._load_values()
        return _bounded_int_or_default(
            values.get(KEY_PROTOCOL_REFRESH_MAX_CONCURRENCY) or values.get(KEY_REFRESH_MAX_CONCURRENCY),
            self._default_protocol_refresh_max_concurrency(),
            0,
            50,
        )

    async def get_browser_refresh_max_concurrency(self) -> int:
        values = await self._load_values()
        return _bounded_int_or_default(
            values.get(KEY_BROWSER_REFRESH_MAX_CONCURRENCY),
            self.settings.browser_refresh_max_concurrency,
            0,
            50,
        )

    async def get_browser_min_available_memory_mb(self) -> int:
        values = await self._load_values()
        return _bounded_int_or_default(
            values.get(KEY_BROWSER_MIN_AVAILABLE_MEMORY_MB),
            self.settings.browser_min_available_memory_mb,
            0,
            1_048_576,
        )

    async def get_subscription_refresh_batch_size(self) -> int:
        values = await self._load_values()
        return _bounded_int_or_default(
            values.get(KEY_SUBSCRIPTION_REFRESH_BATCH_SIZE),
            self.settings.subscription_refresh_batch_size,
            1,
            100,
        )

    async def get_subscription_refresh_max_concurrency(self) -> int:
        values = await self._load_values()
        return _bounded_int_or_default(
            values.get(KEY_SUBSCRIPTION_REFRESH_MAX_CONCURRENCY),
            self.settings.subscription_refresh_max_concurrency,
            0,
            20,
        )

    async def get_account_liveness_max_concurrency(self) -> int:
        values = await self._load_values()
        return _bounded_int_or_default(
            values.get(KEY_ACCOUNT_LIVENESS_MAX_CONCURRENCY),
            self.settings.account_liveness_max_concurrency,
            0,
            50,
        )

    async def get_public_settings(self) -> dict:
        values = await self._load_values()
        base_url = _normalize_base_url(values.get(KEY_SUB2API_BASE_URL) or self.settings.sub2api_base_url)
        runtime_key = decrypt_text(values.get(KEY_SUB2API_X_API_KEY))
        env_x_api_key = self._env_x_api_key()
        effective_x_api_key = runtime_key if KEY_SUB2API_X_API_KEY in values else env_x_api_key
        notification = await self.get_notification_config()
        logo_data = values.get(KEY_SITE_LOGO_DATA)
        logo_version = hashlib.sha256(logo_data.encode("ascii")).hexdigest()[:12] if logo_data else None
        change_log_page_size_options = _change_log_page_size_options_or_default(
            values.get(KEY_CHANGE_LOG_PAGE_SIZE_OPTIONS),
            getattr(self.settings, "change_log_page_size_options", None),
        )
        change_log_page_size = _change_log_page_size_or_default(
            values.get(KEY_CHANGE_LOG_PAGE_SIZE),
            change_log_page_size_options,
            int(getattr(self.settings, "change_log_page_size", 50)),
        )
        return {
            "sub2api_base_url": base_url,
            "sub2api_port": _port_from_url(base_url),
            "sub2api_base_url_source": values.get(KEY_SUB2API_BASE_URL_SOURCE) or "env",
            "sub2api_x_api_key_set": bool(effective_x_api_key),
            "sub2api_x_api_key_hint": redact(effective_x_api_key) if effective_x_api_key else None,
            "sub2api_auto_recover_state": _bool_or_default(
                values.get(KEY_SUB2API_AUTO_RECOVER_STATE),
                self.settings.sub2api_auto_recover_state,
            ),
            "automation_paused": _bool_or_default(
                values.get(KEY_AUTOMATION_PAUSED),
                self.settings.automation_paused,
            ),
            "oauth_account_sync_enabled": _bool_or_default(
                values.get(KEY_OAUTH_ACCOUNT_SYNC_ENABLED),
                self.settings.oauth_account_sync_enabled,
            ),
            "recovery_enabled": _bool_or_default(
                values.get(KEY_RECOVERY_ENABLED),
                self.settings.recovery_enabled,
            ),
            "oauth_login_mode": _oauth_login_mode_or_default(
                values.get(KEY_OAUTH_LOGIN_MODE),
                getattr(self.settings, "oauth_login_mode", "protocol"),
            ),
            "oauth_stop_on_phone_verification": _bool_or_default(
                values.get(KEY_OAUTH_STOP_ON_PHONE_VERIFICATION),
                bool(getattr(self.settings, "oauth_stop_on_phone_verification", False)),
            ),
            "monitor_interval_seconds": _int_or_default(
                values.get(KEY_MONITOR_INTERVAL_SECONDS),
                self.settings.monitor_interval_seconds,
            ),
            "usage_refresh_enabled": _bool_or_default(
                values.get(KEY_USAGE_REFRESH_ENABLED),
                self.settings.usage_refresh_enabled,
            ),
            "usage_refresh_interval_seconds": _int_or_default(
                values.get(KEY_USAGE_REFRESH_INTERVAL_SECONDS),
                self.settings.usage_refresh_interval_seconds,
            ),
            "usage_refresh_max_concurrency": _bounded_int_or_default(
                values.get(KEY_USAGE_REFRESH_MAX_CONCURRENCY),
                self.settings.usage_refresh_max_concurrency,
                0,
                100,
            ),
            "api_key_account_sync_enabled": _bool_or_default(
                values.get(KEY_API_KEY_ACCOUNT_SYNC_ENABLED),
                self.settings.api_key_account_sync_enabled,
            ),
            "api_key_account_sync_interval_seconds": _bounded_int_or_default(
                values.get(KEY_API_KEY_ACCOUNT_SYNC_INTERVAL_SECONDS),
                self.settings.api_key_account_sync_interval_seconds,
                30,
                86_400,
            ),
            "upstream_sync_enabled": _bool_or_default(
                values.get(KEY_UPSTREAM_SYNC_ENABLED),
                self.settings.upstream_sync_enabled
                if self.settings.upstream_sync_enabled is not None
                else _bool_or_default(
                    values.get(KEY_UPSTREAM_RATE_SYNC_ENABLED),
                    self.settings.upstream_rate_sync_enabled,
                ),
            ),
            "upstream_sync_interval_seconds": _bounded_int_or_default(
                values.get(KEY_UPSTREAM_SYNC_INTERVAL_SECONDS),
                self.settings.upstream_sync_interval_seconds,
                60,
                86_400,
            ),
            "upstream_sync_max_concurrency": _bounded_int_or_default(
                values.get(KEY_UPSTREAM_SYNC_MAX_CONCURRENCY),
                self.settings.upstream_sync_max_concurrency,
                0,
                50,
            ),
            "upstream_rate_sync_enabled": _bool_or_default(
                values.get(KEY_UPSTREAM_RATE_SYNC_ENABLED),
                self.settings.upstream_rate_sync_enabled,
            ),
            "upstream_priority_sync_enabled": _bool_or_default(
                values.get(KEY_UPSTREAM_PRIORITY_SYNC_ENABLED),
                self.settings.upstream_priority_sync_enabled,
            ),
            "manual_upstream_sync_rate_enabled": _bool_or_default(
                values.get(KEY_MANUAL_UPSTREAM_SYNC_RATE_ENABLED),
                bool(getattr(self.settings, "manual_upstream_sync_rate_enabled", True)),
            ),
            "manual_upstream_sync_priority_enabled": _bool_or_default(
                values.get(KEY_MANUAL_UPSTREAM_SYNC_PRIORITY_ENABLED),
                bool(getattr(self.settings, "manual_upstream_sync_priority_enabled", True)),
            ),
            "manual_upstream_sync_upstream_health_enabled": _bool_or_default(
                values.get(KEY_MANUAL_UPSTREAM_SYNC_UPSTREAM_HEALTH_ENABLED),
                bool(
                    getattr(
                        self.settings,
                        "manual_upstream_sync_upstream_health_enabled",
                        True,
                    )
                ),
            ),
            "manual_upstream_sync_channel_monitors_enabled": _bool_or_default(
                values.get(KEY_MANUAL_UPSTREAM_SYNC_CHANNEL_MONITORS_ENABLED),
                bool(
                    getattr(
                        self.settings,
                        "manual_upstream_sync_channel_monitors_enabled",
                        True,
                    )
                ),
            ),
            "manual_upstream_sync_account_availability_enabled": _bool_or_default(
                values.get(KEY_MANUAL_UPSTREAM_SYNC_ACCOUNT_AVAILABILITY_ENABLED),
                bool(
                    getattr(
                        self.settings,
                        "manual_upstream_sync_account_availability_enabled",
                        False,
                    )
                ),
            ),
            "manual_upstream_sync_balance_guard_enabled": _bool_or_default(
                values.get(KEY_MANUAL_UPSTREAM_SYNC_BALANCE_GUARD_ENABLED),
                bool(
                    getattr(
                        self.settings,
                        "manual_upstream_sync_balance_guard_enabled",
                        True,
                    )
                ),
            ),
            "manual_upstream_sync_rate_pause_enabled": _bool_or_default(
                values.get(KEY_MANUAL_UPSTREAM_SYNC_RATE_PAUSE_ENABLED),
                bool(
                    getattr(
                        self.settings,
                        "manual_upstream_sync_rate_pause_enabled",
                        True,
                    )
                ),
            ),
            "api_key_auto_disable_on_upstream_unavailable": _bool_or_default(
                values.get(KEY_API_KEY_AUTO_DISABLE_ON_UPSTREAM_UNAVAILABLE),
                self.settings.api_key_auto_disable_on_upstream_unavailable,
            ),
            "api_key_auto_pause_on_channel_monitor_unavailable_enabled": _bool_or_default(
                values.get(KEY_API_KEY_AUTO_PAUSE_ON_CHANNEL_MONITOR_UNAVAILABLE_ENABLED),
                bool(
                    getattr(
                        self.settings,
                        "api_key_auto_pause_on_channel_monitor_unavailable_enabled",
                        False,
                    )
                ),
            ),
            "api_key_availability_all_tests_must_succeed": _bool_or_default(
                values.get(KEY_API_KEY_AVAILABILITY_ALL_TESTS_MUST_SUCCEED),
                bool(
                    getattr(
                        self.settings,
                        "api_key_availability_all_tests_must_succeed",
                        False,
                    )
                ),
            ),
            "channel_monitor_auto_probe_enabled": _bool_or_default(
                values.get(KEY_CHANNEL_MONITOR_AUTO_PROBE_ENABLED),
                bool(getattr(self.settings, "channel_monitor_auto_probe_enabled", True)),
            ),
            "account_model_whitelist_sync_enabled": _bool_or_default(
                values.get(KEY_ACCOUNT_MODEL_WHITELIST_SYNC_ENABLED),
                (
                    _bool_or_default(
                        values.get(KEY_ACCOUNT_MODEL_WHITELIST_SYNC_EACH_TIME),
                        bool(getattr(self.settings, "account_model_whitelist_sync_each_time", False)),
                    )
                    if KEY_ACCOUNT_MODEL_WHITELIST_SYNC_ENABLED not in values
                    else bool(getattr(self.settings, "account_model_whitelist_sync_enabled", False))
                ),
            ),
            "account_model_whitelist_sync_interval_seconds": _bounded_int_or_default(
                values.get(KEY_ACCOUNT_MODEL_WHITELIST_SYNC_INTERVAL_SECONDS),
                int(getattr(self.settings, "account_model_whitelist_sync_interval_seconds", 3600)),
                60,
                86_400,
            ),
            "account_model_whitelist_sync_each_time": _bool_or_default(
                values.get(KEY_ACCOUNT_MODEL_WHITELIST_SYNC_EACH_TIME),
                bool(
                    getattr(
                        self.settings,
                        "account_model_whitelist_sync_each_time",
                        False,
                    )
                ),
            ),
            "channel_monitor_unavailable_consecutive_threshold": _bounded_int_or_default(
                values.get(KEY_CHANNEL_MONITOR_UNAVAILABLE_CONSECUTIVE_THRESHOLD),
                int(
                    getattr(
                        self.settings,
                        "channel_monitor_unavailable_consecutive_threshold",
                        2,
                    )
                ),
                1,
                100,
            ),
            "channel_monitor_recovery_consecutive_threshold": _bounded_int_or_default(
                values.get(KEY_CHANNEL_MONITOR_RECOVERY_CONSECUTIVE_THRESHOLD),
                int(
                    getattr(
                        self.settings,
                        "channel_monitor_recovery_consecutive_threshold",
                        2,
                    )
                ),
                1,
                100,
            ),
            "channel_monitor_fallback_without_monitor_enabled": _bool_or_default(
                values.get(KEY_CHANNEL_MONITOR_FALLBACK_WITHOUT_MONITOR_ENABLED),
                bool(
                    getattr(
                        self.settings,
                        "channel_monitor_fallback_without_monitor_enabled",
                        False,
                    )
                ),
            ),
            "channel_monitor_fallback_test_models": _normalize_model_chain(
                values.get(KEY_CHANNEL_MONITOR_FALLBACK_TEST_MODELS)
                if values.get(KEY_CHANNEL_MONITOR_FALLBACK_TEST_MODELS) is not None
                else getattr(self.settings, "channel_monitor_fallback_test_models", [])
            ) or _normalize_model_chain(
                values.get(KEY_CHANNEL_MONITOR_FALLBACK_TEST_MODEL)
                or getattr(self.settings, "channel_monitor_fallback_test_model", "")
                or ""
            ),
            "channel_monitor_fallback_test_model": str(
                values.get(KEY_CHANNEL_MONITOR_FALLBACK_TEST_MODEL)
                or getattr(self.settings, "channel_monitor_fallback_test_model", "")
                or ""
            ).strip()[:160],
            "channel_monitor_fallback_test_attempts": _bounded_int_or_default(
                values.get(KEY_CHANNEL_MONITOR_FALLBACK_TEST_ATTEMPTS),
                int(getattr(self.settings, "channel_monitor_fallback_test_attempts", 1)),
                1,
                5,
            ),
            "channel_monitor_recovery_test_attempts": _bounded_int_or_default(
                values.get(KEY_CHANNEL_MONITOR_RECOVERY_TEST_ATTEMPTS),
                int(getattr(self.settings, "channel_monitor_recovery_test_attempts", 1)),
                1,
                5,
            ),
            "channel_monitor_test_attempt_interval_seconds": _bounded_int_or_default(
                values.get(KEY_CHANNEL_MONITOR_TEST_ATTEMPT_INTERVAL_SECONDS),
                int(
                    getattr(
                        self.settings,
                        "channel_monitor_test_attempt_interval_seconds",
                        0,
                    )
                ),
                0,
                300,
            ),
            "api_key_auto_pause_on_negative_balance_enabled": _bool_or_default(
                values.get(KEY_API_KEY_AUTO_PAUSE_ON_NEGATIVE_BALANCE_ENABLED),
                bool(getattr(self.settings, "api_key_auto_pause_on_negative_balance_enabled", False)),
            ),
            "upstream_negative_balance_basis": (
                values.get(KEY_UPSTREAM_NEGATIVE_BALANCE_BASIS)
                if values.get(KEY_UPSTREAM_NEGATIVE_BALANCE_BASIS) in {"wallet", "recharge_adjusted"}
                else getattr(self.settings, "upstream_negative_balance_basis", "wallet")
            ),
            "upstream_balance_pause_threshold": _bounded_float_or_default(
                values.get(KEY_UPSTREAM_BALANCE_PAUSE_THRESHOLD),
                float(getattr(self.settings, "upstream_balance_pause_threshold", 0.0)),
                -1_000_000_000.0,
                1_000_000_000.0,
            ),
            "show_stale_negative_balance_alert": _bool_or_default(
                values.get(KEY_SHOW_STALE_NEGATIVE_BALANCE_ALERT),
                bool(getattr(self.settings, "show_stale_negative_balance_alert", True)),
            ),
            "priority_assign_disabled_api_key_accounts": _bool_or_default(
                values.get(KEY_PRIORITY_ASSIGN_DISABLED_API_KEY_ACCOUNTS),
                bool(getattr(self.settings, "priority_assign_disabled_api_key_accounts", False)),
            ),
            "priority_share_same_composite_multiplier": _bool_or_default(
                values.get(KEY_PRIORITY_SHARE_SAME_COMPOSITE_MULTIPLIER),
                bool(
                    getattr(
                        self.settings,
                        "priority_share_same_composite_multiplier",
                        False,
                    )
                ),
            ),
            "discord_bot_notifications_enabled": notification["enabled"],
            "discord_bot_token_set": bool(notification["discord_bot_token"]),
            "discord_bot_token_hint": (
                redact(notification["discord_bot_token"])
                if notification["discord_bot_token"]
                else None
            ),
            "discord_bot_channel_id": notification["discord_channel_id"],
            "notify_oauth_account_disabled": notification["oauth_account_disabled_enabled"],
            "notify_account_enabled": notification["account_enabled_enabled"],
            "notify_api_key_rate_changed": notification["api_key_rate_changed_enabled"],
            "notify_upstream_group_changed": notification["upstream_group_changed_enabled"],
            "notify_upstream_balance_low": notification["upstream_balance_low_enabled"],
            "notify_upstream_token_invalid": notification[
                "upstream_channel_token_invalid_enabled"
            ],
            "upstream_rate_log_retention_days": _bounded_int_or_default(
                values.get(KEY_UPSTREAM_RATE_LOG_RETENTION_DAYS),
                self.settings.upstream_rate_log_retention_days,
                1,
                3650,
            ),
            "upstream_usage_data_retention_days": _bounded_int_or_default(
                values.get(KEY_UPSTREAM_USAGE_DATA_RETENTION_DAYS),
                int(getattr(self.settings, "upstream_usage_data_retention_days", 90)),
                1,
                3650,
            ),
            "change_log_page_size": change_log_page_size,
            "change_log_page_size_options": change_log_page_size_options,
            "usage_limit_sample_five_hour_threshold_percent": _threshold_or_settings_default(
                values.get(KEY_USAGE_LIMIT_SAMPLE_FIVE_HOUR_THRESHOLD_PERCENT),
                self.settings.usage_limit_sample_five_hour_threshold_percent,
            ),
            "usage_limit_sample_seven_day_threshold_percent": _threshold_or_settings_default(
                values.get(KEY_USAGE_LIMIT_SAMPLE_SEVEN_DAY_THRESHOLD_PERCENT),
                self.settings.usage_limit_sample_seven_day_threshold_percent,
            ),
            "usage_limit_default_ranges": normalize_usage_limit_ranges(
                values.get(KEY_USAGE_LIMIT_DEFAULT_RANGES) or self.settings.usage_limit_default_ranges_json
            ),
            "refresh_max_concurrency": _bounded_int_or_default(
                values.get(KEY_PROTOCOL_REFRESH_MAX_CONCURRENCY) or values.get(KEY_REFRESH_MAX_CONCURRENCY),
                self._default_protocol_refresh_max_concurrency(),
                0,
                50,
            ),
            "protocol_refresh_max_concurrency": _bounded_int_or_default(
                values.get(KEY_PROTOCOL_REFRESH_MAX_CONCURRENCY) or values.get(KEY_REFRESH_MAX_CONCURRENCY),
                self._default_protocol_refresh_max_concurrency(),
                0,
                50,
            ),
            "browser_refresh_max_concurrency": _bounded_int_or_default(
                values.get(KEY_BROWSER_REFRESH_MAX_CONCURRENCY),
                self.settings.browser_refresh_max_concurrency,
                0,
                50,
            ),
            "browser_min_available_memory_mb": _bounded_int_or_default(
                values.get(KEY_BROWSER_MIN_AVAILABLE_MEMORY_MB),
                self.settings.browser_min_available_memory_mb,
                0,
                1_048_576,
            ),
            "subscription_refresh_batch_size": _bounded_int_or_default(
                values.get(KEY_SUBSCRIPTION_REFRESH_BATCH_SIZE),
                self.settings.subscription_refresh_batch_size,
                1,
                100,
            ),
            "subscription_refresh_max_concurrency": _bounded_int_or_default(
                values.get(KEY_SUBSCRIPTION_REFRESH_MAX_CONCURRENCY),
                self.settings.subscription_refresh_max_concurrency,
                0,
                20,
            ),
            "account_liveness_max_concurrency": _bounded_int_or_default(
                values.get(KEY_ACCOUNT_LIVENESS_MAX_CONCURRENCY),
                self.settings.account_liveness_max_concurrency,
                0,
                50,
            ),
            "last_scan_at": _datetime_or_none(values.get(KEY_LAST_SCAN_AT)),
            "last_scan_status": values.get(KEY_LAST_SCAN_STATUS),
            "last_scan_message": values.get(KEY_LAST_SCAN_MESSAGE),
            "display_timezone": _clean_timezone(values.get(KEY_DISPLAY_TIMEZONE) or self.settings.display_timezone),
            "site_name": _clean_site_name(values.get(KEY_SITE_NAME) or self.settings.app_name, self.settings.app_name),
            "site_logo_url": f"/api/settings/logo?v={logo_version}" if logo_version else "/logo.png",
            "site_logo_custom": bool(logo_version),
            "site_logo_updated_at": _datetime_or_none(
                values.get(KEY_SITE_LOGO_UPDATED_AT)
            ),
        }

    async def update_public_settings(self, payload: dict) -> dict:
        if payload.get("clear_discord_bot_token") and str(
            payload.get("discord_bot_token") or ""
        ).strip():
            raise RuntimeConfigServiceError(
                "A Discord bot token cannot be set and cleared in the same request."
            )
        if payload.get("clear_site_logo") and payload.get("site_logo_data_url"):
            raise RuntimeConfigServiceError(
                "A site logo cannot be set and cleared in the same request."
            )
        current_values = await self._load_values()
        requested_page_size_options = payload.get("change_log_page_size_options")
        if requested_page_size_options is not None:
            normalized_page_size_options = _parse_change_log_page_size_options(
                requested_page_size_options
            )
            if not normalized_page_size_options:
                raise RuntimeConfigServiceError(
                    "Change log page size options must contain 1 to 20 unique integers from 1 to 200."
                )
        else:
            normalized_page_size_options = _change_log_page_size_options_or_default(
                current_values.get(KEY_CHANGE_LOG_PAGE_SIZE_OPTIONS),
                getattr(self.settings, "change_log_page_size_options", None),
            )
        requested_page_size = payload.get("change_log_page_size")
        if requested_page_size is not None:
            normalized_page_size = int(requested_page_size)
        else:
            normalized_page_size = _change_log_page_size_or_default(
                current_values.get(KEY_CHANGE_LOG_PAGE_SIZE),
                normalized_page_size_options,
                int(getattr(self.settings, "change_log_page_size", 50)),
            )
        if normalized_page_size not in normalized_page_size_options:
            raise RuntimeConfigServiceError(
                "The default change log page size must be included in its selectable options."
            )
        usage_history_time_zone = _clean_timezone(
            str(
                payload.get("display_timezone")
                or current_values.get(KEY_DISPLAY_TIMEZONE)
                or self.settings.display_timezone
            )
        )
        current_base_url = _normalize_base_url(
            current_values.get(KEY_SUB2API_BASE_URL) or self.settings.sub2api_base_url
        )
        next_base_url = current_base_url
        if payload.get("sub2api_base_url") is not None or payload.get("sub2api_port") is not None:
            next_base_url = _normalize_base_url(payload.get("sub2api_base_url") or current_base_url)
            if payload.get("sub2api_port") is not None:
                next_base_url = _replace_port(next_base_url, int(payload["sub2api_port"]))

        current_runtime_key = decrypt_text(current_values.get(KEY_SUB2API_X_API_KEY))
        current_auth_token = (
            current_runtime_key or ""
            if KEY_SUB2API_X_API_KEY in current_values
            else self.settings.sub2api_auth_token.strip()
        )
        replacing_key = bool(
            isinstance(payload.get("sub2api_x_api_key"), str)
            and payload["sub2api_x_api_key"].strip()
        )
        clearing_key = bool(payload.get("clear_sub2api_x_api_key"))
        if (
            _sub2api_origin(next_base_url) != _sub2api_origin(current_base_url)
            and current_auth_token
            and not replacing_key
            and not clearing_key
            and not payload.get("confirm_sub2api_credential_rebind")
        ):
            raise RuntimeConfigServiceError(
                "Changing the sub2api origin while retaining its credential requires explicit confirmation.",
                status_code=409,
            )
        x_api_key_for_file = self._x_api_key_for_file(payload, current_values)
        explicit_runtime_key_change = clearing_key or replacing_key

        async with AsyncSessionLocal() as db:
            if payload.get("sub2api_base_url") is not None or payload.get("sub2api_port") is not None:
                await self._put(db, KEY_SUB2API_BASE_URL, next_base_url)
                await self._put(db, KEY_SUB2API_BASE_URL_SOURCE, "manual")

            if payload.get("monitor_interval_seconds") is not None:
                await self._put(db, KEY_MONITOR_INTERVAL_SECONDS, str(int(payload["monitor_interval_seconds"])))

            if payload.get("oauth_account_sync_enabled") is not None:
                await self._put(
                    db,
                    KEY_OAUTH_ACCOUNT_SYNC_ENABLED,
                    "true" if payload["oauth_account_sync_enabled"] else "false",
                )

            if payload.get("automation_paused") is not None:
                await self._put(db, KEY_AUTOMATION_PAUSED, "true" if payload["automation_paused"] else "false")

            if payload.get("recovery_enabled") is not None:
                await self._put(db, KEY_RECOVERY_ENABLED, "true" if payload["recovery_enabled"] else "false")

            if payload.get("oauth_login_mode") is not None:
                requested_mode = str(payload["oauth_login_mode"]).strip().lower()
                if requested_mode not in {"protocol", "browser"}:
                    raise RuntimeConfigServiceError("OAuth login mode must be protocol or browser.")
                await self._put(db, KEY_OAUTH_LOGIN_MODE, requested_mode)

            if payload.get("oauth_stop_on_phone_verification") is not None:
                await self._put(
                    db,
                    KEY_OAUTH_STOP_ON_PHONE_VERIFICATION,
                    "true" if payload["oauth_stop_on_phone_verification"] else "false",
                )

            if payload.get("usage_refresh_enabled") is not None:
                await self._put(db, KEY_USAGE_REFRESH_ENABLED, "true" if payload["usage_refresh_enabled"] else "false")

            if payload.get("usage_refresh_interval_seconds") is not None:
                await self._put(
                    db,
                    KEY_USAGE_REFRESH_INTERVAL_SECONDS,
                    str(int(payload["usage_refresh_interval_seconds"])),
                )

            if payload.get("usage_refresh_max_concurrency") is not None:
                await self._put(
                    db,
                    KEY_USAGE_REFRESH_MAX_CONCURRENCY,
                    str(int(payload["usage_refresh_max_concurrency"])),
                )

            if payload.get("api_key_account_sync_enabled") is not None:
                await self._put(
                    db,
                    KEY_API_KEY_ACCOUNT_SYNC_ENABLED,
                    "true" if payload["api_key_account_sync_enabled"] else "false",
                )

            if payload.get("api_key_account_sync_interval_seconds") is not None:
                await self._put(
                    db,
                    KEY_API_KEY_ACCOUNT_SYNC_INTERVAL_SECONDS,
                    str(int(payload["api_key_account_sync_interval_seconds"])),
                )

            if payload.get("upstream_sync_enabled") is not None:
                await self._put(
                    db,
                    KEY_UPSTREAM_SYNC_ENABLED,
                    "true" if payload["upstream_sync_enabled"] else "false",
                )

            if payload.get("upstream_sync_interval_seconds") is not None:
                await self._put(
                    db,
                    KEY_UPSTREAM_SYNC_INTERVAL_SECONDS,
                    str(int(payload["upstream_sync_interval_seconds"])),
                )

            if payload.get("upstream_sync_max_concurrency") is not None:
                await self._put(
                    db,
                    KEY_UPSTREAM_SYNC_MAX_CONCURRENCY,
                    str(int(payload["upstream_sync_max_concurrency"])),
                )

            if payload.get("upstream_rate_sync_enabled") is not None:
                await self._put(
                    db,
                    KEY_UPSTREAM_RATE_SYNC_ENABLED,
                    "true" if payload["upstream_rate_sync_enabled"] else "false",
                )

            if payload.get("upstream_priority_sync_enabled") is not None:
                await self._put(
                    db,
                    KEY_UPSTREAM_PRIORITY_SYNC_ENABLED,
                    "true" if payload["upstream_priority_sync_enabled"] else "false",
                )

            if payload.get("api_key_auto_disable_on_upstream_unavailable") is not None:
                await self._put(
                    db,
                    KEY_API_KEY_AUTO_DISABLE_ON_UPSTREAM_UNAVAILABLE,
                    "true"
                    if payload["api_key_auto_disable_on_upstream_unavailable"]
                    else "false",
                )

            bool_setting_keys = {
                "manual_upstream_sync_rate_enabled": KEY_MANUAL_UPSTREAM_SYNC_RATE_ENABLED,
                "manual_upstream_sync_priority_enabled": KEY_MANUAL_UPSTREAM_SYNC_PRIORITY_ENABLED,
                "manual_upstream_sync_upstream_health_enabled": KEY_MANUAL_UPSTREAM_SYNC_UPSTREAM_HEALTH_ENABLED,
                "manual_upstream_sync_channel_monitors_enabled": KEY_MANUAL_UPSTREAM_SYNC_CHANNEL_MONITORS_ENABLED,
                "manual_upstream_sync_account_availability_enabled": KEY_MANUAL_UPSTREAM_SYNC_ACCOUNT_AVAILABILITY_ENABLED,
                "manual_upstream_sync_balance_guard_enabled": KEY_MANUAL_UPSTREAM_SYNC_BALANCE_GUARD_ENABLED,
                "manual_upstream_sync_rate_pause_enabled": KEY_MANUAL_UPSTREAM_SYNC_RATE_PAUSE_ENABLED,
                "api_key_auto_pause_on_channel_monitor_unavailable_enabled": KEY_API_KEY_AUTO_PAUSE_ON_CHANNEL_MONITOR_UNAVAILABLE_ENABLED,
                "api_key_availability_all_tests_must_succeed": KEY_API_KEY_AVAILABILITY_ALL_TESTS_MUST_SUCCEED,
                "channel_monitor_auto_probe_enabled": KEY_CHANNEL_MONITOR_AUTO_PROBE_ENABLED,
                "account_model_whitelist_sync_enabled": KEY_ACCOUNT_MODEL_WHITELIST_SYNC_ENABLED,
                "account_model_whitelist_sync_each_time": KEY_ACCOUNT_MODEL_WHITELIST_SYNC_EACH_TIME,
                "channel_monitor_fallback_without_monitor_enabled": KEY_CHANNEL_MONITOR_FALLBACK_WITHOUT_MONITOR_ENABLED,
                "api_key_auto_pause_on_negative_balance_enabled": KEY_API_KEY_AUTO_PAUSE_ON_NEGATIVE_BALANCE_ENABLED,
                "show_stale_negative_balance_alert": KEY_SHOW_STALE_NEGATIVE_BALANCE_ALERT,
                "priority_assign_disabled_api_key_accounts": KEY_PRIORITY_ASSIGN_DISABLED_API_KEY_ACCOUNTS,
                "priority_share_same_composite_multiplier": KEY_PRIORITY_SHARE_SAME_COMPOSITE_MULTIPLIER,
                "discord_bot_notifications_enabled": KEY_DISCORD_BOT_NOTIFICATIONS_ENABLED,
                "notify_oauth_account_disabled": KEY_NOTIFY_OAUTH_ACCOUNT_DISABLED,
                "notify_account_enabled": KEY_NOTIFY_ACCOUNT_ENABLED,
                "notify_api_key_rate_changed": KEY_NOTIFY_API_KEY_RATE_CHANGED,
                "notify_upstream_group_changed": KEY_NOTIFY_UPSTREAM_GROUP_CHANGED,
                "notify_upstream_balance_low": KEY_NOTIFY_UPSTREAM_BALANCE_LOW,
                "notify_upstream_token_invalid": KEY_NOTIFY_UPSTREAM_TOKEN_INVALID,
            }
            for payload_key, setting_key in bool_setting_keys.items():
                if payload.get(payload_key) is not None:
                    await self._put(
                        db,
                        setting_key,
                        "true" if payload[payload_key] else "false",
                    )

            int_setting_keys = {
                "account_model_whitelist_sync_interval_seconds": KEY_ACCOUNT_MODEL_WHITELIST_SYNC_INTERVAL_SECONDS,
                "channel_monitor_fallback_test_attempts": KEY_CHANNEL_MONITOR_FALLBACK_TEST_ATTEMPTS,
                "channel_monitor_recovery_test_attempts": KEY_CHANNEL_MONITOR_RECOVERY_TEST_ATTEMPTS,
                "channel_monitor_test_attempt_interval_seconds": KEY_CHANNEL_MONITOR_TEST_ATTEMPT_INTERVAL_SECONDS,
            }
            for payload_key, setting_key in int_setting_keys.items():
                if payload.get(payload_key) is not None:
                    await self._put(db, setting_key, str(int(payload[payload_key])))

            if payload.get("channel_monitor_fallback_test_model") is not None:
                await self._put(
                    db,
                    KEY_CHANNEL_MONITOR_FALLBACK_TEST_MODEL,
                    str(payload["channel_monitor_fallback_test_model"]).strip()[:160],
                )

            if payload.get("channel_monitor_fallback_test_models") is not None:
                models = _normalize_model_chain(payload["channel_monitor_fallback_test_models"])
                await self._put(
                    db,
                    KEY_CHANNEL_MONITOR_FALLBACK_TEST_MODELS,
                    json.dumps(models, ensure_ascii=False),
                )

            if payload.get("upstream_negative_balance_basis") is not None:
                basis = str(payload["upstream_negative_balance_basis"])
                if basis not in {"wallet", "recharge_adjusted"}:
                    raise RuntimeConfigServiceError("The upstream balance basis is invalid.")
                await self._put(db, KEY_UPSTREAM_NEGATIVE_BALANCE_BASIS, basis)

            if payload.get("upstream_balance_pause_threshold") is not None:
                await self._put(
                    db,
                    KEY_UPSTREAM_BALANCE_PAUSE_THRESHOLD,
                    _format_number(float(payload["upstream_balance_pause_threshold"])),
                )

            if payload.get("discord_bot_channel_id") is not None:
                await self._put(
                    db,
                    KEY_DISCORD_BOT_CHANNEL_ID,
                    str(payload["discord_bot_channel_id"]).strip(),
                )

            if payload.get("clear_discord_bot_token"):
                await self._put(db, KEY_DISCORD_BOT_TOKEN, "")
            else:
                raw_token = payload.get("discord_bot_token")
                if isinstance(raw_token, str) and raw_token.strip():
                    await self._put(db, KEY_DISCORD_BOT_TOKEN, encrypt_text(raw_token.strip()))

            if payload.get("upstream_rate_log_retention_days") is not None:
                retention_days = int(payload["upstream_rate_log_retention_days"])
                await self._put(
                    db,
                    KEY_UPSTREAM_RATE_LOG_RETENTION_DAYS,
                    str(retention_days),
                )
                await delete_expired_upstream_rate_change_logs(
                    db,
                    retention_days=retention_days,
                )
                await delete_expired_change_logs(
                    db,
                    retention_days=retention_days,
                )

            if payload.get("upstream_usage_data_retention_days") is not None:
                retention_days = int(payload["upstream_usage_data_retention_days"])
                await self._put(
                    db,
                    KEY_UPSTREAM_USAGE_DATA_RETENTION_DAYS,
                    str(retention_days),
                )
                await prune_upstream_usage_history(
                    db,
                    retention_days=retention_days,
                    time_zone=usage_history_time_zone,
                )

            if payload.get("change_log_page_size") is not None:
                await self._put(
                    db,
                    KEY_CHANGE_LOG_PAGE_SIZE,
                    str(normalized_page_size),
                )

            if payload.get("change_log_page_size_options") is not None:
                await self._put(
                    db,
                    KEY_CHANGE_LOG_PAGE_SIZE_OPTIONS,
                    json.dumps(normalized_page_size_options, separators=(",", ":")),
                )

            if payload.get("usage_limit_sample_five_hour_threshold_percent") is not None:
                await self._put(
                    db,
                    KEY_USAGE_LIMIT_SAMPLE_FIVE_HOUR_THRESHOLD_PERCENT,
                    _format_number(float(payload["usage_limit_sample_five_hour_threshold_percent"])),
                )

            if payload.get("usage_limit_sample_seven_day_threshold_percent") is not None:
                await self._put(
                    db,
                    KEY_USAGE_LIMIT_SAMPLE_SEVEN_DAY_THRESHOLD_PERCENT,
                    _format_number(float(payload["usage_limit_sample_seven_day_threshold_percent"])),
                )

            if payload.get("usage_limit_default_ranges") is not None:
                normalized_ranges = normalize_usage_limit_ranges(payload["usage_limit_default_ranges"])
                await self._put(
                    db,
                    KEY_USAGE_LIMIT_DEFAULT_RANGES,
                    json.dumps(normalized_ranges, ensure_ascii=True, separators=(",", ":"), sort_keys=True),
                )

            if payload.get("sub2api_auto_recover_state") is not None:
                await self._put(
                    db,
                    KEY_SUB2API_AUTO_RECOVER_STATE,
                    "true" if payload["sub2api_auto_recover_state"] else "false",
                )

            protocol_concurrency = payload.get("protocol_refresh_max_concurrency")
            if protocol_concurrency is None:
                protocol_concurrency = payload.get("refresh_max_concurrency")
            if protocol_concurrency is not None:
                value = str(int(protocol_concurrency))
                await self._put(db, KEY_PROTOCOL_REFRESH_MAX_CONCURRENCY, value)
                await self._put(db, KEY_REFRESH_MAX_CONCURRENCY, value)

            if payload.get("browser_refresh_max_concurrency") is not None:
                await self._put(
                    db,
                    KEY_BROWSER_REFRESH_MAX_CONCURRENCY,
                    str(int(payload["browser_refresh_max_concurrency"])),
                )

            if payload.get("browser_min_available_memory_mb") is not None:
                await self._put(
                    db,
                    KEY_BROWSER_MIN_AVAILABLE_MEMORY_MB,
                    str(int(payload["browser_min_available_memory_mb"])),
                )

            if payload.get("subscription_refresh_batch_size") is not None:
                await self._put(
                    db,
                    KEY_SUBSCRIPTION_REFRESH_BATCH_SIZE,
                    str(int(payload["subscription_refresh_batch_size"])),
                )

            if payload.get("subscription_refresh_max_concurrency") is not None:
                await self._put(
                    db,
                    KEY_SUBSCRIPTION_REFRESH_MAX_CONCURRENCY,
                    str(int(payload["subscription_refresh_max_concurrency"])),
                )

            if payload.get("account_liveness_max_concurrency") is not None:
                await self._put(
                    db,
                    KEY_ACCOUNT_LIVENESS_MAX_CONCURRENCY,
                    str(int(payload["account_liveness_max_concurrency"])),
                )

            if payload.get("display_timezone") is not None:
                await self._put(db, KEY_DISPLAY_TIMEZONE, _clean_timezone(str(payload["display_timezone"])))

            if payload.get("site_name") is not None:
                await self._put(db, KEY_SITE_NAME, _clean_site_name(str(payload["site_name"]), self.settings.app_name))

            if payload.get("clear_site_logo"):
                await self._put(db, KEY_SITE_LOGO_DATA, None)
                await self._put(db, KEY_SITE_LOGO_MIME, None)
                await self._put(db, KEY_SITE_LOGO_UPDATED_AT, None)
            elif payload.get("site_logo_data_url") is not None:
                raw, mime = _parse_logo_data_url(str(payload["site_logo_data_url"]))
                await self._put(db, KEY_SITE_LOGO_DATA, base64.b64encode(raw).decode("ascii"))
                await self._put(db, KEY_SITE_LOGO_MIME, mime)
                await self._put(db, KEY_SITE_LOGO_UPDATED_AT, utcnow().isoformat())

            if payload.get("clear_sub2api_x_api_key"):
                # An explicit empty marker prevents the in-memory environment
                # credential from becoming active again before restart.
                await self._put(db, KEY_SUB2API_X_API_KEY, "")
            else:
                raw_key = payload.get("sub2api_x_api_key")
                if isinstance(raw_key, str) and raw_key.strip():
                    await self._put(db, KEY_SUB2API_X_API_KEY, encrypt_text(raw_key.strip()))

            await db.commit()

        settings = await self.get_public_settings()
        self._persist_settings_file(
            settings,
            x_api_key_for_file,
            remove_legacy_x_api_key=explicit_runtime_key_change and bool(self._env_x_api_key()),
        )
        return settings

    async def scan_sub2api_ports(self, apply: bool = False) -> dict:
        config = await self.get_sub2api_config()
        checked_ports = self._candidate_ports(config.base_url)
        configured_hit = await self._probe_configured_sub2api(config)
        hit = configured_hit or await self._find_sub2api(checked_ports, config)
        credential_rebind_blocked = bool(
            hit
            and apply
            and str(getattr(config, "auth_token", "")).strip()
            and _sub2api_origin(hit.base_url) != _sub2api_origin(config.base_url)
        )
        should_apply = bool(hit and apply and not credential_rebind_blocked)

        if hit:
            status = "found"
            message = hit.message
            if credential_rebind_blocked:
                message = (
                    f"{message} 检测结果未自动应用：切换来源并保留现有凭据需要在设置中明确确认。"
                )
            base_url = hit.base_url
            port = hit.port
        else:
            status = "not_found"
            message = (
                "当前配置地址和受限本地候选端口均未发现 sub2api；"
                "请手动设置地址/端口，或调整 SUB2API_SCAN_PORTS。"
            )
            base_url = None
            port = None

        async with AsyncSessionLocal() as db:
            if should_apply:
                await self._put(db, KEY_SUB2API_BASE_URL, hit.base_url)
                if configured_hit is None:
                    await self._put(db, KEY_SUB2API_BASE_URL_SOURCE, "auto")
            await self._put(db, KEY_LAST_SCAN_AT, utcnow().isoformat())
            await self._put(db, KEY_LAST_SCAN_STATUS, status)
            await self._put(db, KEY_LAST_SCAN_MESSAGE, message)
            await db.commit()

        result = {
            "found": bool(hit),
            "base_url": base_url,
            "port": port,
            "status": status,
            "message": message,
            "checked_ports": checked_ports,
            "applied": should_apply,
        }
        if should_apply:
            current_values = await self._load_values()
            self._persist_settings_file(
                await self.get_public_settings(),
                self._x_api_key_for_file({}, current_values),
            )
        return result

    async def _probe_configured_sub2api(
        self,
        config: EffectiveSub2ApiConfig,
    ) -> ProbeHit | None:
        timeout = httpx.Timeout(self._probe_operation_timeout())
        accounts_url = f"{config.base_url.rstrip('/')}{config.accounts_path}"
        try:
            async with httpx.AsyncClient(
                timeout=timeout,
                headers={**_headers_for_probe(config), "Accept-Encoding": "identity"},
                follow_redirects=False,
                trust_env=False,
            ) as client:
                response = await self._bounded_probe_get(
                    client,
                    accounts_url,
                    params={"page": 1, "page_size": 1},
                )
        except (httpx.HTTPError, TimeoutError, _ProbeResponseRejected):
            return None

        port = _port_from_url(config.base_url)
        if port is None:
            return None
        if response.status_code == 200 and _looks_like_sub2api_accounts_response(response):
            return ProbeHit(
                base_url=config.base_url,
                port=port,
                status="200",
                message=f"已验证当前 sub2api 地址 {config.base_url}。",
            )
        if response.status_code in {401, 403} and _looks_like_sub2api_auth_response(response):
            return ProbeHit(
                base_url=config.base_url,
                port=port,
                status=str(response.status_code),
                message=f"已识别当前 sub2api 地址 {config.base_url}，但凭据未通过验证。",
            )
        return None

    async def auto_detect_sub2api(self) -> None:
        values = await self._load_values()
        if values.get(KEY_SUB2API_BASE_URL_SOURCE) == "manual":
            return
        base_url = _normalize_base_url(values.get(KEY_SUB2API_BASE_URL) or self.settings.sub2api_base_url)
        if not _is_local_url(base_url):
            return
        await self.scan_sub2api_ports(apply=True)

    def _candidate_ports(self, current_base_url: str) -> list[int]:
        ports: list[int] = []
        current_port = _port_from_url(current_base_url)
        if current_port:
            ports.append(current_port)
        ports.extend(self.settings.sub2api_scan_ports)
        ports.extend(COMMON_SUB2API_PORTS)
        return list(dict.fromkeys(port for port in ports if 0 < int(port) <= 65535))

    async def _find_sub2api(
        self,
        ports: list[int],
        config: EffectiveSub2ApiConfig,
    ) -> ProbeHit | None:
        timeout = httpx.Timeout(self._probe_operation_timeout())
        async with httpx.AsyncClient(
            timeout=timeout,
            # Discovery must not broadcast the administrator token to every
            # process that happens to listen on loopback.
            headers={"Accept": "application/json", "Accept-Encoding": "identity"},
            follow_redirects=False,
            trust_env=False,
        ) as client:
            tasks = [asyncio.create_task(self._probe_port(client, port)) for port in ports]
            try:
                for finished in asyncio.as_completed(tasks):
                    hit = await finished
                    if hit is not None:
                        for task in tasks:
                            if not task.done():
                                task.cancel()
                        return hit
            finally:
                await asyncio.gather(*tasks, return_exceptions=True)
        return None

    async def _probe_port(self, client: httpx.AsyncClient, port: int) -> ProbeHit | None:
        root = f"http://127.0.0.1:{port}"
        base_url = f"{root}/api/v1"
        accounts_url = f"{base_url}/admin/accounts"
        try:
            response = await self._bounded_probe_get(client, accounts_url)
            if response.status_code in {401, 403} and _looks_like_sub2api_auth_response(response):
                return ProbeHit(
                    base_url=base_url,
                    port=port,
                    status=str(response.status_code),
                    message=f"已检测到 {base_url}（/admin/accounts 返回 {response.status_code}）。",
                )
            if response.status_code == 200 and _looks_like_sub2api_accounts_response(response):
                return ProbeHit(
                    base_url=base_url,
                    port=port,
                    status=str(response.status_code),
                    message=f"已检测到 {base_url}（/admin/accounts 返回账号 JSON）。",
                )
        except (httpx.HTTPError, TimeoutError, _ProbeResponseRejected):
            pass

        try:
            response = await self._bounded_probe_get(client, f"{root}/openapi.json")
            if response.status_code == 200 and _looks_like_sub2api(response.text):
                return ProbeHit(
                    base_url=base_url,
                    port=port,
                    status="openapi",
                    message=f"已检测到 {base_url}（OpenAPI 匹配 sub2api）。",
                )
        except (httpx.HTTPError, TimeoutError, _ProbeResponseRejected):
            pass

        return None

    async def _bounded_probe_get(
        self,
        client: httpx.AsyncClient,
        url: str,
        *,
        params: dict[str, Any] | None = None,
    ) -> httpx.Response:
        total_timeout = self._probe_total_timeout()
        deadline = monotonic() + total_timeout
        async with asyncio.timeout(total_timeout):
            async with client.stream("GET", url, params=params) as response:
                body = await _read_bounded_probe_body(response, deadline=deadline)
                return httpx.Response(
                    response.status_code,
                    headers=response.headers,
                    content=body,
                    request=response.request,
                )

    def _probe_operation_timeout(self) -> float:
        return _bounded_float_or_default(
            self.settings.sub2api_scan_timeout_seconds,
            0.8,
            0.1,
            10.0,
        )

    def _probe_total_timeout(self) -> float:
        return min(15.0, max(1.0, self._probe_operation_timeout() * 3.0))

    async def _load_values(self) -> dict[str, str | None]:
        async with AsyncSessionLocal() as db:
            result = await db.execute(select(AppSetting))
            return {item.key: item.value for item in result.scalars().all()}

    def _x_api_key_for_file(self, payload: dict, current_values: dict[str, str | None]) -> str | None:
        if payload.get("clear_sub2api_x_api_key"):
            return ""
        raw_key = payload.get("sub2api_x_api_key")
        if isinstance(raw_key, str) and raw_key.strip():
            return raw_key.strip()
        if KEY_SUB2API_X_API_KEY in current_values:
            return decrypt_text(current_values.get(KEY_SUB2API_X_API_KEY)) or ""
        return decrypt_text(current_values.get(KEY_SUB2API_X_API_KEY)) or self._env_x_api_key() or None

    def _persist_settings_file(
        self,
        settings: dict[str, Any],
        x_api_key: str | None,
        *,
        remove_legacy_x_api_key: bool = False,
    ) -> None:
        if self.settings.app_env == "test":
            return
        path = self.settings.project_root / ".env"
        values: dict[str, str | None] = {
            "APP_NAME": str(settings["site_name"]),
            "SUB2API_BASE_URL": str(settings["sub2api_base_url"]),
            "SUB2API_AUTO_RECOVER_STATE": _env_bool(bool(settings["sub2api_auto_recover_state"])),
            "AUTOMATION_PAUSED": _env_bool(bool(settings["automation_paused"])),
            "OAUTH_ACCOUNT_SYNC_ENABLED": _env_bool(bool(settings["oauth_account_sync_enabled"])),
            "RECOVERY_ENABLED": _env_bool(bool(settings["recovery_enabled"])),
            "OAUTH_LOGIN_MODE": str(settings.get("oauth_login_mode", "protocol")),
            "OAUTH_STOP_ON_PHONE_VERIFICATION": _env_bool(
                bool(settings.get("oauth_stop_on_phone_verification", False))
            ),
            "MONITOR_INTERVAL_SECONDS": str(int(settings["monitor_interval_seconds"])),
            "USAGE_REFRESH_ENABLED": _env_bool(bool(settings["usage_refresh_enabled"])),
            "USAGE_REFRESH_INTERVAL_SECONDS": str(int(settings["usage_refresh_interval_seconds"])),
            "USAGE_REFRESH_MAX_CONCURRENCY": str(int(settings["usage_refresh_max_concurrency"])),
            "API_KEY_ACCOUNT_SYNC_ENABLED": _env_bool(bool(settings["api_key_account_sync_enabled"])),
            "API_KEY_ACCOUNT_SYNC_INTERVAL_SECONDS": str(
                int(settings["api_key_account_sync_interval_seconds"])
            ),
            "UPSTREAM_SYNC_ENABLED": _env_bool(bool(settings["upstream_sync_enabled"])),
            "UPSTREAM_SYNC_INTERVAL_SECONDS": str(int(settings["upstream_sync_interval_seconds"])),
            "UPSTREAM_SYNC_MAX_CONCURRENCY": str(int(settings["upstream_sync_max_concurrency"])),
            "UPSTREAM_RATE_SYNC_ENABLED": _env_bool(bool(settings["upstream_rate_sync_enabled"])),
            "UPSTREAM_PRIORITY_SYNC_ENABLED": _env_bool(
                bool(settings["upstream_priority_sync_enabled"])
            ),
            "MANUAL_UPSTREAM_SYNC_RATE_ENABLED": _env_bool(
                bool(settings.get("manual_upstream_sync_rate_enabled", True))
            ),
            "MANUAL_UPSTREAM_SYNC_PRIORITY_ENABLED": _env_bool(
                bool(settings.get("manual_upstream_sync_priority_enabled", True))
            ),
            "MANUAL_UPSTREAM_SYNC_UPSTREAM_HEALTH_ENABLED": _env_bool(
                bool(settings.get("manual_upstream_sync_upstream_health_enabled", True))
            ),
            "MANUAL_UPSTREAM_SYNC_CHANNEL_MONITORS_ENABLED": _env_bool(
                bool(settings.get("manual_upstream_sync_channel_monitors_enabled", True))
            ),
            "MANUAL_UPSTREAM_SYNC_ACCOUNT_AVAILABILITY_ENABLED": _env_bool(
                bool(settings.get("manual_upstream_sync_account_availability_enabled", False))
            ),
            "MANUAL_UPSTREAM_SYNC_BALANCE_GUARD_ENABLED": _env_bool(
                bool(settings.get("manual_upstream_sync_balance_guard_enabled", True))
            ),
            "MANUAL_UPSTREAM_SYNC_RATE_PAUSE_ENABLED": _env_bool(
                bool(settings.get("manual_upstream_sync_rate_pause_enabled", True))
            ),
            "API_KEY_AUTO_DISABLE_ON_UPSTREAM_UNAVAILABLE": _env_bool(
                bool(settings["api_key_auto_disable_on_upstream_unavailable"])
            ),
            "API_KEY_AUTO_PAUSE_ON_CHANNEL_MONITOR_UNAVAILABLE_ENABLED": _env_bool(
                bool(settings["api_key_auto_pause_on_channel_monitor_unavailable_enabled"])
            ),
            "API_KEY_AVAILABILITY_ALL_TESTS_MUST_SUCCEED": _env_bool(
                bool(settings.get("api_key_availability_all_tests_must_succeed", False))
            ),
            "CHANNEL_MONITOR_AUTO_PROBE_ENABLED": _env_bool(
                bool(settings.get("channel_monitor_auto_probe_enabled", True))
            ),
            "ACCOUNT_MODEL_WHITELIST_SYNC_ENABLED": _env_bool(
                bool(settings.get("account_model_whitelist_sync_enabled", False))
            ),
            "ACCOUNT_MODEL_WHITELIST_SYNC_INTERVAL_SECONDS": str(
                int(settings.get("account_model_whitelist_sync_interval_seconds", 3600))
            ),
            "ACCOUNT_MODEL_WHITELIST_SYNC_EACH_TIME": _env_bool(
                bool(settings.get("account_model_whitelist_sync_each_time", False))
            ),
            # Legacy confirmation thresholds are read for migration only. The
            # current policy uses per-round pause and recovery test counts.
            "CHANNEL_MONITOR_UNAVAILABLE_CONSECUTIVE_THRESHOLD": None,
            "CHANNEL_MONITOR_RECOVERY_CONSECUTIVE_THRESHOLD": None,
            "CHANNEL_MONITOR_FALLBACK_WITHOUT_MONITOR_ENABLED": _env_bool(
                bool(settings.get("channel_monitor_fallback_without_monitor_enabled", False))
            ),
            "CHANNEL_MONITOR_FALLBACK_TEST_MODELS": json.dumps(
                settings.get("channel_monitor_fallback_test_models", []),
                ensure_ascii=False,
            ),
            "CHANNEL_MONITOR_FALLBACK_TEST_MODEL": str(
                settings.get("channel_monitor_fallback_test_model", "")
            ),
            "CHANNEL_MONITOR_FALLBACK_TEST_ATTEMPTS": str(
                int(settings.get("channel_monitor_fallback_test_attempts", 1))
            ),
            "CHANNEL_MONITOR_RECOVERY_TEST_ATTEMPTS": str(
                int(settings.get("channel_monitor_recovery_test_attempts", 1))
            ),
            "CHANNEL_MONITOR_TEST_ATTEMPT_INTERVAL_SECONDS": str(
                int(settings.get("channel_monitor_test_attempt_interval_seconds", 0))
            ),
            "API_KEY_AUTO_PAUSE_ON_NEGATIVE_BALANCE_ENABLED": _env_bool(
                bool(settings.get("api_key_auto_pause_on_negative_balance_enabled", False))
            ),
            "UPSTREAM_NEGATIVE_BALANCE_BASIS": str(settings.get("upstream_negative_balance_basis", "wallet")),
            "UPSTREAM_BALANCE_PAUSE_THRESHOLD": _format_number(
                float(settings.get("upstream_balance_pause_threshold", 0.0))
            ),
            "SHOW_STALE_NEGATIVE_BALANCE_ALERT": _env_bool(
                bool(settings.get("show_stale_negative_balance_alert", True))
            ),
            "PRIORITY_ASSIGN_DISABLED_API_KEY_ACCOUNTS": _env_bool(
                bool(settings.get("priority_assign_disabled_api_key_accounts", False))
            ),
            "PRIORITY_SHARE_SAME_COMPOSITE_MULTIPLIER": _env_bool(
                bool(settings.get("priority_share_same_composite_multiplier", False))
            ),
            "DISCORD_BOT_NOTIFICATIONS_ENABLED": _env_bool(
                bool(settings.get("discord_bot_notifications_enabled", False))
            ),
            "DISCORD_BOT_CHANNEL_ID": str(settings.get("discord_bot_channel_id", "")),
            "NOTIFY_OAUTH_ACCOUNT_DISABLED": _env_bool(
                bool(settings.get("notify_oauth_account_disabled", False))
            ),
            "NOTIFY_ACCOUNT_ENABLED": _env_bool(
                bool(settings.get("notify_account_enabled", False))
            ),
            "NOTIFY_API_KEY_RATE_CHANGED": _env_bool(
                bool(settings.get("notify_api_key_rate_changed", False))
            ),
            "NOTIFY_UPSTREAM_GROUP_CHANGED": _env_bool(
                bool(settings.get("notify_upstream_group_changed", False))
            ),
            "NOTIFY_UPSTREAM_BALANCE_LOW": _env_bool(
                bool(settings.get("notify_upstream_balance_low", False))
            ),
            "NOTIFY_UPSTREAM_TOKEN_INVALID": _env_bool(
                bool(settings.get("notify_upstream_token_invalid", False))
            ),
            "UPSTREAM_RATE_LOG_RETENTION_DAYS": str(int(settings["upstream_rate_log_retention_days"])),
            "UPSTREAM_USAGE_DATA_RETENTION_DAYS": str(
                int(settings.get("upstream_usage_data_retention_days", 90))
            ),
            "CHANGE_LOG_PAGE_SIZE": str(int(settings.get("change_log_page_size", 50))),
            "CHANGE_LOG_PAGE_SIZE_OPTIONS": json.dumps(
                settings.get("change_log_page_size_options", [20, 50, 100, 200]),
                separators=(",", ":"),
            ),
            "USAGE_LIMIT_SAMPLE_FIVE_HOUR_THRESHOLD_PERCENT": _format_number(
                float(settings["usage_limit_sample_five_hour_threshold_percent"])
            ),
            "USAGE_LIMIT_SAMPLE_SEVEN_DAY_THRESHOLD_PERCENT": _format_number(
                float(settings["usage_limit_sample_seven_day_threshold_percent"])
            ),
            "USAGE_LIMIT_DEFAULT_RANGES_JSON": json.dumps(
                settings["usage_limit_default_ranges"],
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
            ),
            "REFRESH_MAX_CONCURRENCY": str(int(settings["protocol_refresh_max_concurrency"])),
            "PROTOCOL_REFRESH_MAX_CONCURRENCY": str(int(settings["protocol_refresh_max_concurrency"])),
            "BROWSER_REFRESH_MAX_CONCURRENCY": str(int(settings["browser_refresh_max_concurrency"])),
            "BROWSER_MIN_AVAILABLE_MEMORY_MB": str(int(settings["browser_min_available_memory_mb"])),
            "SUBSCRIPTION_REFRESH_BATCH_SIZE": str(int(settings["subscription_refresh_batch_size"])),
            "SUBSCRIPTION_REFRESH_MAX_CONCURRENCY": str(int(settings["subscription_refresh_max_concurrency"])),
            "ACCOUNT_LIVENESS_MAX_CONCURRENCY": str(
                int(settings["account_liveness_max_concurrency"])
            ),
            "DISPLAY_TIMEZONE": str(settings["display_timezone"]),
        }
        if remove_legacy_x_api_key or x_api_key == "":
            values.update(
                {
                    "SUB2API_AUTH_TOKEN": None,
                    "SUB2API_AUTH_HEADER": None,
                    "SUB2API_AUTH_SCHEME": None,
                }
            )
        _write_env_file(path, values)

    async def _put(self, db: AsyncSession, key: str, value: str | None) -> None:
        setting = await db.get(AppSetting, key)
        if setting is None:
            setting = AppSetting(key=key)
            db.add(setting)
        setting.value = value

    def _env_x_api_key(self) -> str:
        header = self.settings.sub2api_auth_header.strip().lower()
        if header in {"x-api-key", "x-api", "x-api_key"}:
            return self.settings.sub2api_auth_token.strip()
        return ""

    def _default_protocol_refresh_max_concurrency(self) -> int:
        if self.settings.protocol_refresh_max_concurrency is not None:
            return self.settings.protocol_refresh_max_concurrency
        return self.settings.refresh_max_concurrency


def _headers_for_probe(config: EffectiveSub2ApiConfig) -> dict[str, str]:
    headers = {"Accept": "application/json"}
    token = config.auth_token.strip()
    if token:
        scheme = config.auth_scheme.strip()
        value = f"{scheme} {token}".strip() if scheme else token
        headers[config.auth_header] = value
    return headers


def _normalize_base_url(value: str) -> str:
    return normalize_sub2api_base_url(value)


def _sub2api_origin(value: str) -> tuple[str, str]:
    parsed = urlparse(_normalize_base_url(value))
    return parsed.scheme.casefold(), parsed.netloc.casefold()


def _replace_port(base_url: str, port: int) -> str:
    return replace_sub2api_port(base_url, port)


def _port_from_url(value: str) -> int | None:
    parsed = urlparse(value)
    try:
        if parsed.port:
            return parsed.port
    except ValueError:
        return None
    if parsed.scheme == "http":
        return 80
    if parsed.scheme == "https":
        return 443
    return None


def _int_or_default(value: str | None, default: int) -> int:
    try:
        return int(value) if value is not None else default
    except (TypeError, ValueError):
        return default


def _bounded_int_or_default(value: str | None, default: int, minimum: int, maximum: int) -> int:
    result = _int_or_default(value, default)
    if result < minimum:
        return minimum
    if result > maximum:
        return maximum
    return result


def _parse_change_log_page_size_options(value: object) -> list[int]:
    raw = value
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (TypeError, ValueError):
            return []
    if not isinstance(raw, (list, tuple)) or not 1 <= len(raw) <= 20:
        return []
    normalized: list[int] = []
    for item in raw:
        if isinstance(item, bool) or not isinstance(item, int) or item < 1 or item > 200:
            return []
        normalized.append(item)
    return sorted(set(normalized))


def _change_log_page_size_options_or_default(
    value: object,
    default: object = None,
) -> list[int]:
    return (
        _parse_change_log_page_size_options(value)
        or _parse_change_log_page_size_options(default)
        or [20, 50, 100, 200]
    )


def _change_log_page_size_or_default(
    value: str | None,
    options: list[int],
    default: int = 50,
) -> int:
    result = _int_or_default(value, default)
    return result if result in options else (default if default in options else options[0])


def _float_or_default(value: str | None, default: float) -> float:
    try:
        return float(value) if value is not None else float(default)
    except (TypeError, ValueError):
        return float(default)


def _bounded_float_or_default(value: str | None, default: float, minimum: float, maximum: float) -> float:
    result = _float_or_default(value, default)
    if result < minimum:
        return minimum
    if result > maximum:
        return maximum
    return result


def _positive_bounded_float_or_default(value: str | None, default: float, maximum: float) -> float:
    result = _float_or_default(value, default)
    if not result > 0:
        return float(default)
    if result > maximum:
        return maximum
    return result


def _threshold_or_settings_default(value: str | None, settings_default: float) -> float:
    if value is None:
        return _bounded_float_or_default(None, settings_default, 0.0, 100.0)
    return _bounded_float_or_default(value, 0.0, 0.0, 100.0)


def _format_number(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else str(value)


def _bool_or_default(value: str | None, default: bool) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _oauth_login_mode_or_default(value: Any, default: Any = "protocol") -> str:
    candidate = str(value or "").strip().lower()
    if candidate in {"protocol", "browser"}:
        return candidate
    fallback = str(default or "protocol").strip().lower()
    return fallback if fallback in {"protocol", "browser"} else "protocol"


def _normalize_model_chain(value: Any) -> list[str]:
    raw_items: list[Any]
    if isinstance(value, list):
        raw_items = value
    elif isinstance(value, tuple):
        raw_items = list(value)
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        try:
            parsed = json.loads(text)
        except (TypeError, ValueError):
            parsed = None
        if isinstance(parsed, list):
            raw_items = parsed
        else:
            raw_items = re.split(r"\s*(?:->|→|,|\r?\n)\s*", text)
    else:
        return []
    models: list[str] = []
    seen: set[str] = set()
    for item in raw_items:
        model = str(item or "").strip()[:160]
        if model and model not in seen:
            seen.add(model)
            models.append(model)
        if len(models) >= 10:
            break
    return models


def _env_bool(value: bool) -> str:
    return "true" if value else "false"


def _write_env_file(path: Path, values: dict[str, str | None]) -> None:
    existing = ""
    try:
        existing = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        pass
    except OSError as exc:
        raise RuntimeError(f"Could not read runtime config file {path}.") from exc

    next_text = _merge_env_text(existing, values)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_name(f"{path.name}.tmp")
        tmp_path.write_text(next_text, encoding="utf-8")
        try:
            tmp_path.chmod(0o600)
        except OSError:
            pass
        tmp_path.replace(path)
    except OSError as exc:
        raise RuntimeError(f"Could not write runtime config file {path}.") from exc


def _merge_env_text(existing: str, values: dict[str, str | None]) -> str:
    remaining = dict(values)
    lines: list[str] = []
    for line in existing.splitlines():
        key = _env_line_key(line)
        if key is None or key not in remaining:
            lines.append(line)
            continue
        value = remaining.pop(key)
        if value is not None:
            lines.append(f"{key}={_format_env_value(value)}")

    if remaining:
        if lines and lines[-1].strip():
            lines.append("")
        lines.append("# Runtime settings saved from the admin panel")
        for key, value in remaining.items():
            if value is not None:
                lines.append(f"{key}={_format_env_value(value)}")

    return "\n".join(lines).rstrip() + "\n"


def _env_line_key(line: str) -> str | None:
    stripped = line.strip()
    if not stripped or stripped.startswith("#") or "=" not in stripped:
        return None
    key = stripped.split("=", 1)[0].strip()
    if key.startswith("export "):
        key = key[7:].strip()
    return key if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key) else None


def _format_env_value(value: str) -> str:
    if value == "":
        return ""
    if re.fullmatch(r"[A-Za-z0-9_./:+,@%-]+", value):
        return value
    return json.dumps(value, ensure_ascii=False)


def _datetime_or_none(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _is_local_url(value: str) -> bool:
    return is_loopback_sub2api_url(value)


def _looks_like_sub2api(text: str) -> bool:
    lowered = text.lower()
    if "at guardian" in lowered:
        return False
    return "sub2api" in lowered and ("admin/accounts" in lowered or "accounts" in lowered)


def _clean_timezone(value: str | None) -> str:
    text = (value or "").strip()
    if text and re.fullmatch(r"[A-Za-z0-9_+./:-]{1,80}", text):
        return text
    return "Asia/Shanghai"


def _clean_site_name(value: str | None, default: str) -> str:
    text = re.sub(r"\s+", " ", (value or "").strip())
    text = "".join(char for char in text if char.isprintable())
    if not text:
        return default
    return text[:80]


def _parse_logo_data_url(value: str) -> tuple[bytes, str]:
    match = re.fullmatch(
        r"data:(image/(?:png|jpeg|webp));base64,([A-Za-z0-9+/=\r\n]+)",
        value.strip(),
    )
    if not match:
        raise RuntimeConfigServiceError("The site logo must be a PNG, JPEG, or WebP image.")
    mime, encoded = match.groups()
    try:
        raw = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise RuntimeConfigServiceError("The site logo data is invalid.") from exc
    return _normalize_logo_bytes(raw, mime), mime


def _valid_logo_bytes(raw: bytes, mime: str) -> bool:
    try:
        image = _decode_logo(raw, mime)
    except RuntimeConfigServiceError:
        return False
    image.close()
    return True


def _decode_logo(raw: bytes, mime: str) -> Image.Image:
    if not raw or len(raw) > MAX_SITE_LOGO_BYTES:
        raise RuntimeConfigServiceError("The site logo must not exceed 1 MB.")
    expected_format = {
        "image/png": "PNG",
        "image/jpeg": "JPEG",
        "image/webp": "WEBP",
    }.get(mime)
    if expected_format is None:
        raise RuntimeConfigServiceError("The site logo must be a PNG, JPEG, or WebP image.")
    try:
        with Image.open(BytesIO(raw)) as source:
            if source.format != expected_format:
                raise RuntimeConfigServiceError("The site logo content does not match its media type.")
            width, height = source.size
            if (
                width < 1
                or height < 1
                or width > MAX_SITE_LOGO_DIMENSION
                or height > MAX_SITE_LOGO_DIMENSION
                or width * height > MAX_SITE_LOGO_PIXELS
            ):
                raise RuntimeConfigServiceError(
                    f"The site logo must be no larger than {MAX_SITE_LOGO_DIMENSION} x "
                    f"{MAX_SITE_LOGO_DIMENSION} pixels."
                )
            if bool(getattr(source, "is_animated", False)) or int(getattr(source, "n_frames", 1)) != 1:
                raise RuntimeConfigServiceError("Animated site logos are not supported.")
            source.load()
            return source.copy()
    except RuntimeConfigServiceError:
        raise
    except (Image.DecompressionBombError, UnidentifiedImageError, OSError, ValueError) as exc:
        raise RuntimeConfigServiceError("The site logo is not a valid decodable image.") from exc


def _normalize_logo_bytes(raw: bytes, mime: str) -> bytes:
    image = _decode_logo(raw, mime)
    try:
        has_alpha = "A" in image.getbands() or "transparency" in image.info
        if mime == "image/jpeg":
            normalized = image.convert("RGB")
            save_options: dict[str, Any] = {
                "format": "JPEG",
                "quality": 88,
                "optimize": True,
                "progressive": True,
            }
        elif mime == "image/png":
            normalized = image.convert("RGBA" if has_alpha else "RGB")
            save_options = {"format": "PNG", "optimize": True, "compress_level": 9}
        else:
            normalized = image.convert("RGBA" if has_alpha else "RGB")
            save_options = {"format": "WEBP", "quality": 88, "method": 4}
        try:
            output = BytesIO()
            normalized.save(output, **save_options)
            encoded = output.getvalue()
        finally:
            normalized.close()
    except (OSError, ValueError) as exc:
        raise RuntimeConfigServiceError("The site logo could not be normalized.") from exc
    finally:
        image.close()
    if not encoded or len(encoded) > MAX_SITE_LOGO_BYTES:
        raise RuntimeConfigServiceError("The normalized site logo must not exceed 1 MB.")
    return encoded


def _looks_like_sub2api_accounts_response(response: httpx.Response) -> bool:
    content_type = response.headers.get("content-type", "").lower()
    if "json" not in content_type:
        return False
    try:
        payload = response.json()
    except ValueError:
        return False
    return _looks_like_sub2api_accounts_payload(payload)


async def _read_bounded_probe_body(
    response: httpx.Response,
    *,
    deadline: float,
) -> bytes:
    raw_content_length = response.headers.get("content-length")
    if raw_content_length:
        try:
            content_length = int(raw_content_length)
        except ValueError as exc:
            raise _ProbeResponseRejected("Invalid probe response length.") from exc
        if content_length < 0 or content_length > MAX_CONFIGURED_PROBE_RESPONSE_BYTES:
            raise _ProbeResponseRejected("Probe response exceeded the byte limit.")

    # MockTransport and custom transports may return a response whose bounded
    # in-memory body is already materialized before the stream context opens.
    if response.is_stream_consumed:
        if monotonic() >= deadline:
            raise _ProbeResponseRejected("Probe response exceeded the total deadline.")
        body = response.content
        if len(body) > MAX_CONFIGURED_PROBE_RESPONSE_BYTES:
            raise _ProbeResponseRejected("Probe response exceeded the byte limit.")
        return body

    chunks: list[bytes] = []
    total = 0
    async for chunk in response.aiter_raw():
        if monotonic() >= deadline:
            raise _ProbeResponseRejected("Probe response exceeded the total deadline.")
        total += len(chunk)
        if total > MAX_CONFIGURED_PROBE_RESPONSE_BYTES:
            raise _ProbeResponseRejected("Probe response exceeded the byte limit.")
        chunks.append(chunk)
    if monotonic() >= deadline:
        raise _ProbeResponseRejected("Probe response exceeded the total deadline.")
    return b"".join(chunks)


def _looks_like_sub2api_auth_response(response: httpx.Response) -> bool:
    text = response.text.lower()
    if "authorization required" in text or "unauthorized" in text:
        return True
    try:
        payload = response.json()
    except ValueError:
        return False
    if not isinstance(payload, dict) or not payload:
        return False
    code = str(payload.get("code") or payload.get("error") or "").lower()
    message = str(payload.get("message") or payload.get("detail") or "").lower()
    return "unauthorized" in code or "authorization" in message or "api" in message


def _looks_like_sub2api_accounts_payload(payload: Any) -> bool:
    if isinstance(payload, list):
        return True
    if not isinstance(payload, dict):
        return False
    if _contains_account_list(payload):
        return True
    data = payload.get("data")
    if isinstance(data, list):
        return True
    if isinstance(data, dict) and _contains_account_list(data):
        return True
    keys = {str(key).lower() for key in payload.keys()}
    return {"code", "message", "data"} <= keys and data is None


def _contains_account_list(payload: dict[str, Any]) -> bool:
    for key in ("items", "records", "accounts", "list"):
        if isinstance(payload.get(key), list):
            return True
    return False


_runtime_config_service: RuntimeConfigService | None = None


def get_runtime_config_service() -> RuntimeConfigService:
    global _runtime_config_service
    if _runtime_config_service is None:
        _runtime_config_service = RuntimeConfigService()
    return _runtime_config_service
