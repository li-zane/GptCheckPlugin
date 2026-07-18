from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator, model_validator

from app.core.subscription_types import (
    MAX_SUBSCRIPTION_TYPES,
    MAX_USAGE_LIMIT_VALUE,
    SUBSCRIPTION_TYPE_UNKNOWN,
    normalize_subscription_type,
)
from app.core.sub2api_urls import normalize_sub2api_base_url
from app.core.upstream_urls import canonicalize_upstream_url


JS_SAFE_INTEGER_MAX = 9_007_199_254_740_991


class LoginRequest(BaseModel):
    admin_key: str = Field(min_length=1)


class MessageResponse(BaseModel):
    message: str


class AccountSnapshotOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    email: str
    sub2api_account_id: str | None
    platform: str | None
    account_type: str | None
    status: str | None
    schedulable: bool | None
    usage_estimate_enabled: bool
    mailbox_bound: bool
    deactive: bool
    refreshing: bool
    last_error: str | None
    last_seen_at: datetime
    updated_at: datetime


class AccountOut(BaseModel):
    id: int
    email: str
    account_name: str
    sub2api_account_id: str | None
    sub2api_imported_at: str | None = None
    platform: str | None
    account_type: str | None
    status: str | None
    schedulable: bool | None
    usage_estimate_enabled: bool
    mailbox_bound: bool
    deactive: bool
    refreshing: bool
    auto_refresh_locked: bool = False
    last_error: str | None
    sub2api_error_code: int | None = None
    sub2api_error_message: str | None = None
    last_seen_at: datetime
    updated_at: datetime
    is_duplicate: bool = False
    duplicate_group_size: int = 1
    duplicate_rank: int = 0
    duplicate_primary: bool = True
    duplicate_primary_account_id: str | None = None
    remote_error: bool = False
    can_delete_remote: bool = False
    delete_unlockable: bool = False
    delete_unlocked: bool = False
    rate_limited: bool = False
    rate_limited_windows: list[str] = Field(default_factory=list)
    subscription_starts_at: str | None = None
    subscription_expires_at: str | None = None
    subscription_renews_at: str | None = None
    subscription_cancels_at: str | None = None
    subscription_billing_period: str | None = None
    subscription_plan: str | None = None
    subscription_type: str = SUBSCRIPTION_TYPE_UNKNOWN
    subscription_label: str = "Unknown"
    has_active_subscription: bool | None = None
    phone_number: str | None = None
    phone_sms_url: str | None = None
    phone_sms_cdk: str | None = None
    phone_sms_recharge_url: str | None = None


class UsageGroupRef(BaseModel):
    id: str
    name: str


class UsageWindowEstimate(BaseModel):
    used_percent: float | None
    spent: float | None
    raw_spent: float | None
    baseline_spent: float | None
    estimate_spent: float | None
    estimate_basis: str | None
    spend_source: str | None
    estimated_limit: float | None
    remaining: float | None
    remaining_percent: float | None
    reset_at: str | None
    remaining_seconds: int | None
    requests: int | None
    tokens: int | None
    estimable: bool
    rate_limited: bool = False
    source: str
    window_kind: str = "unknown"
    window_minutes: int | None = None
    window_label: str | None = None


class UsageWindowAggregate(BaseModel):
    spent: float
    estimated_limit: float | None
    remaining: float | None
    remaining_percent: float | None
    used_percent: float | None
    account_count: int
    enabled_account_count: int
    estimable_accounts: int


class UsageTokenWindowOut(BaseModel):
    window_key: str
    window_reset_key: str
    window_start_at: str | None
    reset_at: str | None
    spent: float
    tokens: int
    estimated_limit: float | None = None
    first_observed_at: datetime
    last_observed_at: datetime


class UsageTokenHistoryOut(BaseModel):
    total_spent: float
    total_tokens: int
    total_estimated_limit: float
    window_count: int
    windows: list[UsageTokenWindowOut]


class AccountUsageEstimateOut(BaseModel):
    email: str
    account_name: str
    sub2api_account_id: str | None
    platform: str | None
    account_type: str | None
    subscription_plan: str | None = None
    subscription_type: str = SUBSCRIPTION_TYPE_UNKNOWN
    subscription_label: str = "Unknown"
    subscription_billing_period: str | None = None
    has_active_subscription: bool | None = None
    status: str | None
    schedulable: bool | None
    deactive: bool
    error: bool = False
    rate_limited: bool = False
    rate_limited_windows: list[str] = Field(default_factory=list)
    usage_estimate_enabled: bool
    rate_multiplier: float
    groups: list[UsageGroupRef]
    usage_error: str | None
    five_hour: UsageWindowEstimate
    seven_day: UsageWindowEstimate
    seven_day_token_history: UsageTokenHistoryOut


class GroupUsageEstimateOut(BaseModel):
    group_id: str
    group_name: str
    account_count: int
    five_hour: UsageWindowAggregate
    seven_day: UsageWindowAggregate


class OverallUsageEstimateOut(BaseModel):
    account_count: int
    five_hour: UsageWindowAggregate
    seven_day: UsageWindowAggregate


class UsageEstimateOut(BaseModel):
    updated_at: datetime
    refreshed_usage: bool
    formula: dict[str, str]
    overall: OverallUsageEstimateOut
    groups: list[GroupUsageEstimateOut]
    accounts: list[AccountUsageEstimateOut]


class UsageLimitCalibrationOut(BaseModel):
    source: str
    sample_count: int
    lower: float
    upper: float
    mean: float | None
    sigma: float | None
    default_lower: float
    default_upper: float


class UsageLimitSampleOut(BaseModel):
    id: int
    account_key: str
    email: str | None
    sub2api_account_id: str | None
    plan_cohort: str
    subscription_type: str
    subscription_label: str
    reset_key: str
    reset_at: str | None
    observed_limit: float
    raw_spent: float
    used_percent: float
    created_at: datetime
    updated_at: datetime


class UsageLimitWindowSamplesOut(BaseModel):
    window_key: str
    label: str
    plan_cohort: str
    plan_label: str
    subscription_type: str
    subscription_label: str
    calibration: UsageLimitCalibrationOut
    samples: list[UsageLimitSampleOut]


class UsageLimitSamplesOut(BaseModel):
    updated_at: datetime
    target_sample_count: int
    full_percent_threshold: float
    five_hour_threshold_percent: float
    seven_day_threshold_percent: float
    windows: list[UsageLimitWindowSamplesOut]


class UsageLimitRangeSettings(BaseModel):
    lower: float = Field(ge=0, le=MAX_USAGE_LIMIT_VALUE)
    upper: float = Field(ge=0, le=MAX_USAGE_LIMIT_VALUE)

    @model_validator(mode="after")
    def validate_order(self) -> "UsageLimitRangeSettings":
        if self.upper < self.lower:
            raise ValueError("upper must be greater than or equal to lower")
        return self


class UsageLimitPlanRanges(BaseModel):
    five_hour: UsageLimitRangeSettings
    seven_day: UsageLimitRangeSettings
    monthly: UsageLimitRangeSettings


class MailboxImportRequest(BaseModel):
    content: str = Field(min_length=1)
    default_provider: Literal["auto", "outlook", "hotmail", "gmail", "custom", "manual"] = "auto"


class MailboxImportResult(MessageResponse):
    imported: int
    skipped: int
    invalid_lines: list[int]


class PhoneImportRequest(BaseModel):
    content: str = Field(min_length=1)


class PhoneImportResult(MessageResponse):
    imported: int
    updated: int
    skipped: int
    invalid_lines: list[int]


class PhoneBindingUpdate(BaseModel):
    account_emails: list[EmailStr] = Field(default_factory=list, max_length=3)


class PhoneNumberOut(BaseModel):
    id: int
    phone_number: str
    sms_url: str
    sms_cdk: str | None = None
    sms_recharge_url: str | None = None
    account_emails: list[str] = Field(default_factory=list)
    bindings_count: int = 0
    sms_status: str | None = None
    sms_error: str | None = None
    sms_checked_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class MailboxCredentialOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    gpt_email: str
    mailbox_email: str
    provider: str
    disabled: bool
    last_error: str | None
    last_success_at: datetime | None
    created_at: datetime
    updated_at: datetime


class MailMessageOut(BaseModel):
    id: str
    folder: Literal["inbox", "junk"]
    subject: str | None
    sender_name: str | None
    sender_address: str | None
    body_preview: str | None
    code: str | None = None
    received_at: datetime | None


class RefreshJobOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    email: str
    sub2api_account_id: str | None
    status: str
    reason: str | None
    access_token_tail: str | None
    memory_peak_rss_bytes: int | None
    started_at: datetime | None
    finished_at: datetime | None
    created_at: datetime


class EventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    kind: str
    email: str | None
    message: str
    details: dict[str, Any] | None
    created_at: datetime


class AccountExceptionRecordOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    email: str | None
    sub2api_account_id: str | None
    source: str
    status: str
    message: str
    details: dict[str, Any] | None
    created_at: datetime
    updated_at: datetime


class DashboardSummary(BaseModel):
    total_accounts: int
    error_accounts: int
    paused_accounts: int = 0
    deactive_accounts: int
    refreshing_accounts: int
    mailbox_count: int
    recent_success: int
    recent_failed: int


class ManualRefreshRequest(BaseModel):
    email: EmailStr


class UsageEstimatePreferenceUpdate(BaseModel):
    enabled: bool


class AccountDeleteUnlockUpdate(BaseModel):
    unlocked: bool


class AccountRefreshUnlockUpdate(BaseModel):
    unlocked: bool


class SelectedAccountDeleteItem(BaseModel):
    sub2api_account_id: str | None = Field(default=None, max_length=120)
    snapshot_id: int | None = Field(default=None, ge=1)


class SelectedAccountDeleteRequest(BaseModel):
    accounts: list[SelectedAccountDeleteItem] = Field(default_factory=list, max_length=500)


class AccountLivenessSelection(BaseModel):
    account_ids: list[str] = Field(min_length=1, max_length=200)

    @field_validator("account_ids")
    @classmethod
    def validate_account_ids(cls, value: list[str]) -> list[str]:
        normalized: list[str] = []
        seen: set[str] = set()
        for raw_account_id in value:
            account_id = str(raw_account_id or "").strip()
            if not account_id.isdigit() or not 0 < int(account_id) <= JS_SAFE_INTEGER_MAX:
                raise ValueError("account_ids must contain positive numeric sub2api account ids")
            if account_id not in seen:
                normalized.append(account_id)
                seen.add(account_id)
        if not normalized:
            raise ValueError("at least one sub2api account id is required")
        return normalized


class AccountLivenessModelsRequest(AccountLivenessSelection):
    pass


class AccountLivenessTestRequest(AccountLivenessSelection):
    model_id: str = Field(min_length=1, max_length=200)

    @field_validator("model_id")
    @classmethod
    def validate_model_id(cls, value: str) -> str:
        model_id = value.strip()
        if not model_id or any(ord(character) < 32 for character in model_id):
            raise ValueError("model_id must be a non-empty printable value")
        return model_id


class AccountLivenessModelOut(BaseModel):
    id: str
    display_name: str


class AccountLivenessModelsOut(BaseModel):
    source_account_id: str
    models: list[AccountLivenessModelOut]


class AccountLivenessTestItemOut(BaseModel):
    account_id: str
    email: str | None = None
    account_name: str | None = None
    success: bool
    error: str | None = None
    duration_ms: int


class AccountLivenessTestResult(MessageResponse):
    model_id: str
    total: int
    succeeded: int
    failed: int
    results: list[AccountLivenessTestItemOut]


class Sub2ApiSyncResult(MessageResponse):
    total_seen: int
    error_seen: int
    queued: int
    duplicate_accounts_ignored: int = 0
    deleted_accounts: int = 0
    deleted_mailboxes: int = 0
    usage_total: int = 0
    usage_refreshed: int = 0
    usage_skipped: int = 0
    usage_failed: int = 0
    usage_pending: int = 0


class UsageRefreshFailureOut(BaseModel):
    email: str | None
    account_id: str | None
    error: str


class UsageRefreshResult(MessageResponse):
    total: int
    refreshed: int
    skipped: int
    failed: int
    failures: list[UsageRefreshFailureOut]


class SubscriptionRefreshFailureOut(BaseModel):
    email: str | None
    account_id: str | None
    error: str


class SubscriptionRefreshResult(MessageResponse):
    total: int
    refreshed: int
    skipped: int
    no_subscription_fields: int
    protocol_attempts: int = 0
    failed: int
    failures: list[SubscriptionRefreshFailureOut]


class DeactivatedCleanupResult(MessageResponse):
    deleted_accounts: int
    deleted_mailboxes: int
    deleted_sub2api_accounts: int
    deleted_no_email_sub2api_accounts: int = 0
    failed_sub2api_accounts: list[str]


class AppSettingsOut(BaseModel):
    sub2api_base_url: str
    sub2api_port: int | None
    sub2api_base_url_source: str
    sub2api_x_api_key_set: bool
    sub2api_x_api_key_hint: str | None
    sub2api_auto_recover_state: bool
    automation_paused: bool
    oauth_account_sync_enabled: bool
    recovery_enabled: bool
    monitor_interval_seconds: int
    usage_refresh_enabled: bool
    usage_refresh_interval_seconds: int
    usage_refresh_max_concurrency: int
    api_key_account_sync_enabled: bool
    api_key_account_sync_interval_seconds: int
    upstream_sync_enabled: bool
    upstream_sync_interval_seconds: int
    upstream_sync_max_concurrency: int
    upstream_rate_sync_enabled: bool
    upstream_priority_sync_enabled: bool
    api_key_auto_disable_on_upstream_unavailable: bool
    upstream_rate_log_retention_days: int
    usage_limit_sample_five_hour_threshold_percent: float
    usage_limit_sample_seven_day_threshold_percent: float
    usage_limit_default_ranges: dict[str, UsageLimitPlanRanges]
    refresh_max_concurrency: int
    protocol_refresh_max_concurrency: int
    browser_refresh_max_concurrency: int
    browser_min_available_memory_mb: int
    subscription_refresh_batch_size: int
    subscription_refresh_max_concurrency: int
    account_liveness_max_concurrency: int
    last_scan_at: datetime | None
    last_scan_status: str | None
    last_scan_message: str | None
    display_timezone: str
    site_name: str


class AppSettingsUpdate(BaseModel):
    sub2api_base_url: str | None = Field(default=None, min_length=1, max_length=500)
    sub2api_port: int | None = Field(default=None, ge=1, le=65535)
    sub2api_x_api_key: str | None = Field(default=None, max_length=500)
    clear_sub2api_x_api_key: bool = False
    confirm_sub2api_credential_rebind: bool = False
    sub2api_auto_recover_state: bool | None = None
    automation_paused: bool | None = None
    oauth_account_sync_enabled: bool | None = None
    recovery_enabled: bool | None = None
    monitor_interval_seconds: int | None = Field(default=None, ge=30, le=86_400)
    usage_refresh_enabled: bool | None = None
    usage_refresh_interval_seconds: int | None = Field(default=None, ge=60, le=86_400)
    usage_refresh_max_concurrency: int | None = Field(default=None, ge=0, le=100)
    api_key_account_sync_enabled: bool | None = None
    api_key_account_sync_interval_seconds: int | None = Field(default=None, ge=30, le=86_400)
    upstream_sync_enabled: bool | None = None
    upstream_sync_interval_seconds: int | None = Field(default=None, ge=60, le=86_400)
    upstream_sync_max_concurrency: int | None = Field(default=None, ge=0, le=50)
    upstream_rate_sync_enabled: bool | None = None
    upstream_priority_sync_enabled: bool | None = None
    api_key_auto_disable_on_upstream_unavailable: bool | None = None
    upstream_rate_log_retention_days: int | None = Field(default=None, ge=1, le=3650)
    usage_limit_sample_five_hour_threshold_percent: float | None = Field(default=None, ge=0, le=100)
    usage_limit_sample_seven_day_threshold_percent: float | None = Field(default=None, ge=0, le=100)
    usage_limit_default_ranges: dict[str, UsageLimitPlanRanges] | None = None
    refresh_max_concurrency: int | None = Field(default=None, ge=0, le=50)
    protocol_refresh_max_concurrency: int | None = Field(default=None, ge=0, le=50)
    browser_refresh_max_concurrency: int | None = Field(default=None, ge=0, le=50)
    browser_min_available_memory_mb: int | None = Field(default=None, ge=0, le=1_048_576)
    subscription_refresh_batch_size: int | None = Field(default=None, ge=1, le=100)
    subscription_refresh_max_concurrency: int | None = Field(default=None, ge=0, le=20)
    account_liveness_max_concurrency: int | None = Field(default=None, ge=0, le=50)
    display_timezone: str | None = Field(default=None, min_length=1, max_length=80)
    site_name: str | None = Field(default=None, min_length=1, max_length=80)

    @field_validator("sub2api_base_url")
    @classmethod
    def validate_sub2api_base_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return normalize_sub2api_base_url(value)

    @field_validator("usage_limit_default_ranges")
    @classmethod
    def validate_usage_limit_default_range_keys(
        cls,
        value: dict[str, UsageLimitPlanRanges] | None,
    ) -> dict[str, UsageLimitPlanRanges] | None:
        if value is None:
            return None
        if len(value) > MAX_SUBSCRIPTION_TYPES:
            raise ValueError(f"at most {MAX_SUBSCRIPTION_TYPES} subscription types are allowed")
        normalized_keys: set[str] = set()
        for key in value:
            normalized = normalize_subscription_type(key)
            if normalized == SUBSCRIPTION_TYPE_UNKNOWN and key.strip().lower() != SUBSCRIPTION_TYPE_UNKNOWN:
                raise ValueError(f"invalid subscription type: {key}")
            if normalized in normalized_keys:
                raise ValueError(f"duplicate normalized subscription type: {normalized}")
            normalized_keys.add(normalized)
        return value


class Sub2ApiPortScanResult(BaseModel):
    found: bool
    base_url: str | None
    port: int | None
    status: str
    message: str
    checked_ports: list[int]
    applied: bool


class UpstreamGroupOptionOut(BaseModel):
    id: str = Field(min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=200)
    multiplier: float = Field(gt=0, le=1000, allow_inf_nan=False)


class PriorityIntervalCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    start_priority: int = Field(ge=0, le=JS_SAFE_INTEGER_MAX - 1)
    end_priority: int = Field(ge=1, le=JS_SAFE_INTEGER_MAX)
    step: int = Field(default=1, ge=1, le=JS_SAFE_INTEGER_MAX)

    @field_validator("name", mode="before")
    @classmethod
    def strip_name(cls, value: Any) -> Any:
        return value.strip() if isinstance(value, str) else value

    @model_validator(mode="after")
    def validate_bounds(self) -> "PriorityIntervalCreate":
        if self.end_priority <= self.start_priority:
            raise ValueError("end_priority must be greater than start_priority")
        return self


class PriorityIntervalUpdate(PriorityIntervalCreate):
    pass


class PriorityIntervalAssignment(BaseModel):
    expected_identity_fingerprint: str = Field(
        min_length=64,
        max_length=64,
        pattern=r"^[0-9a-f]{64}$",
    )
    priority_interval_id: int | None = Field(
        default=None,
        ge=1,
        le=JS_SAFE_INTEGER_MAX,
    )
    confirm_identity_rebind: bool = False


class PriorityIntervalOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int = Field(ge=1, le=JS_SAFE_INTEGER_MAX)
    name: str
    start_priority: int = Field(ge=0, le=JS_SAFE_INTEGER_MAX - 1)
    end_priority: int = Field(ge=1, le=JS_SAFE_INTEGER_MAX)
    step: int = Field(ge=1, le=JS_SAFE_INTEGER_MAX)
    account_count: int = Field(default=0, ge=0)
    effective_step: int = Field(default=1, ge=1, le=JS_SAFE_INTEGER_MAX)
    created_at: datetime | None = None
    updated_at: datetime | None = None


class PriorityRebalanceOut(BaseModel):
    considered: int = Field(ge=0)
    updated: int = Field(ge=0)
    unchanged: int = Field(ge=0)
    failed: int = Field(ge=0)


class UpstreamAccountUpdate(BaseModel):
    expected_identity_fingerprint: str = Field(
        min_length=64,
        max_length=64,
        pattern=r"^[0-9a-f]{64}$",
    )
    channel_id: int | None = Field(default=None, ge=1, le=JS_SAFE_INTEGER_MAX)
    remote_name: str | None = Field(default=None, max_length=100)
    base_url: str | None = Field(default=None, max_length=500)
    upstream_type: Literal["auto", "newapi", "sub2api"] = "auto"
    upstream_user_id: str | None = Field(default=None, max_length=128)
    selected_group_id: str | None = Field(default=None, max_length=128)
    selected_group_name: str | None = Field(default=None, max_length=200)
    api_key: str | None = None
    access_token: str | None = None
    clear_access_token: bool = False
    confirm_credential_rebind: bool = False
    confirm_identity_rebind: bool = False
    manual_group_multiplier: float | None = Field(default=None, gt=0, le=1000, allow_inf_nan=False)
    manual_recharge_multiplier: float | None = Field(default=None, gt=0, le=1000, allow_inf_nan=False)

    @field_validator("remote_name", mode="before")
    @classmethod
    def strip_remote_name(cls, value: Any) -> Any:
        if isinstance(value, str):
            value = value.strip()
            if not value:
                raise ValueError("remote_name must not be blank")
        return value

    @field_validator(
        "base_url",
        "upstream_user_id",
        "selected_group_id",
        "selected_group_name",
        "api_key",
        "access_token",
        mode="before",
    )
    @classmethod
    def strip_optional_text(cls, value: Any) -> Any:
        if isinstance(value, str):
            return value.strip() or None
        return value

    @field_validator("base_url")
    @classmethod
    def validate_base_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        try:
            return canonicalize_upstream_url(value)
        except ValueError as exc:
            raise ValueError(str(exc)) from exc


class UpstreamChannelUpdate(BaseModel):
    display_name: str | None = Field(default=None, max_length=200)
    base_url: str | None = Field(default=None, max_length=500)
    management_base_url: str | None = Field(default=None, max_length=500)
    upstream_type: Literal["auto", "newapi", "sub2api"] = "auto"
    probe_enabled: bool | None = None
    upstream_user_id: str | None = Field(default=None, max_length=128)
    access_token: str | None = None
    clear_access_token: bool = False
    refresh_token: str | None = None
    clear_refresh_token: bool = False
    confirm_credential_rebind: bool = False
    manual_recharge_multiplier: float | None = Field(default=None, gt=0, le=1000, allow_inf_nan=False)

    @field_validator(
        "display_name",
        "base_url",
        "management_base_url",
        "upstream_user_id",
        "access_token",
        "refresh_token",
        mode="before",
    )
    @classmethod
    def strip_optional_text(cls, value: Any) -> Any:
        if isinstance(value, str):
            return value.strip() or None
        return value

    @field_validator("base_url", "management_base_url")
    @classmethod
    def validate_base_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        try:
            return canonicalize_upstream_url(value)
        except ValueError as exc:
            raise ValueError(str(exc)) from exc


class UpstreamLegacyIdentityBinding(BaseModel):
    sub2api_account_id: int = Field(ge=1, le=JS_SAFE_INTEGER_MAX)
    expected_identity_fingerprint: str = Field(
        min_length=64,
        max_length=64,
        pattern=r"^[0-9a-f]{64}$",
    )


class UpstreamChannelDiscoverAllRequest(BaseModel):
    confirm_legacy_bindings: bool = False
    account_bindings: list[UpstreamLegacyIdentityBinding] = Field(
        default_factory=list,
        max_length=10_000,
    )

    @field_validator("account_bindings")
    @classmethod
    def validate_unique_account_bindings(
        cls,
        value: list[UpstreamLegacyIdentityBinding],
    ) -> list[UpstreamLegacyIdentityBinding]:
        account_ids = [item.sub2api_account_id for item in value]
        if len(account_ids) != len(set(account_ids)):
            raise ValueError("account_bindings must contain unique sub2api account ids")
        return value

class UpstreamApplyRequest(BaseModel):
    confirmed_target_rate: float = Field(gt=0, le=1000, allow_inf_nan=False)
    expected_identity_fingerprint: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")


class UpstreamAccountEnabledUpdate(BaseModel):
    enabled: bool
    expected_identity_fingerprint: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")


class UpstreamRemoteDeleteRequest(BaseModel):
    confirmed_account_id: int = Field(ge=1, le=JS_SAFE_INTEGER_MAX)
    expected_identity_fingerprint: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")


class UpstreamIdentityRequest(BaseModel):
    expected_identity_fingerprint: str = Field(
        min_length=64,
        max_length=64,
        pattern=r"^[0-9a-f]{64}$",
    )


class UpstreamRateChangeLogOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int = Field(ge=1, le=JS_SAFE_INTEGER_MAX)
    sub2api_account_id: int = Field(ge=1, le=JS_SAFE_INTEGER_MAX)
    account_name: str | None = None
    channel_id: int | None = Field(default=None, ge=1, le=JS_SAFE_INTEGER_MAX)
    channel_name: str | None = None
    group_id: str | None = None
    group_name: str | None = None
    old_group_multiplier: float | None = None
    new_group_multiplier: float | None = None
    old_upstream_multiplier: float | None = None
    new_upstream_multiplier: float | None = None
    old_upstream_recharge_multiplier: float | None = None
    new_upstream_recharge_multiplier: float | None = None
    upstream_recharge_multiplier: float | None = None
    local_recharge_multiplier: float | None = None
    old_target_rate: float | None = None
    new_target_rate: float | None = None
    old_current_rate: float | None = None
    new_current_rate: float | None = None
    old_upstream_key_status: str | None = None
    new_upstream_key_status: str | None = None
    old_upstream_group_status: str | None = None
    new_upstream_group_status: str | None = None
    old_remote_schedulable: bool | None = None
    new_remote_schedulable: bool | None = None
    reason: str
    status: str
    safe_error: str | None = None
    created_at: datetime


class UpstreamAccountOut(BaseModel):
    sub2api_account_id: int = Field(ge=1, le=JS_SAFE_INTEGER_MAX)
    identity_fingerprint: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    identity_binding_status: Literal["unmanaged", "unbound", "bound", "mismatch"] = "unmanaged"
    identity_rebind_required: bool = False
    api_key_origin_rebind_required: bool = False
    channel_id: int | None = Field(default=None, ge=1, le=JS_SAFE_INTEGER_MAX)
    channel_name: str | None = None
    remote_name: str
    remote_platform: str | None = None
    remote_account_type: str | None = None
    remote_status: str | None = None
    remote_schedulable: bool | None = None
    priority: int | None = Field(default=None, ge=0, le=JS_SAFE_INTEGER_MAX)
    desired_priority: int | None = Field(default=None, ge=0, le=JS_SAFE_INTEGER_MAX)
    priority_interval_id: int | None = Field(default=None, ge=1, le=JS_SAFE_INTEGER_MAX)
    priority_interval_name: str | None = None
    priority_sync_status: str = "unassigned"
    priority_sync_error: str | None = None
    composite_multiplier: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    managed: bool
    base_url: str | None = None
    upstream_type: Literal["auto", "newapi", "sub2api"] = "auto"
    resolved_upstream_type: Literal["newapi", "sub2api"] | None = None
    upstream_user_id: str | None = None
    selected_group_id: str | None = None
    selected_group_name: str | None = None
    upstream_key_status: str = "not_checked"
    upstream_group_status: str = "not_checked"
    upstream_health_invalid_count: int = Field(default=0, ge=0, le=2)
    upstream_key_checked_at: datetime | None = None
    upstream_group_checked_at: datetime | None = None
    auto_disabled_reason: str | None = None
    last_auto_disabled_at: datetime | None = None
    api_key_set: bool = False
    api_key_hint: str | None = None
    access_token_set: bool = False
    manual_group_multiplier: float | None = None
    manual_recharge_multiplier: float | None = None
    group_options: list[UpstreamGroupOptionOut] = Field(default_factory=list)
    discovered_group_multiplier: float | None = None
    effective_group_multiplier: float | None = None
    group_multiplier_source: str | None = None
    group_multiplier_status: str | None = None
    discovered_recharge_multiplier: float | None = None
    effective_recharge_multiplier: float | None = None
    recharge_multiplier_source: str | None = None
    recharge_multiplier_status: str | None = None
    local_recharge_multiplier: float | None = None
    local_recharge_source: str | None = None
    local_recharge_status: str | None = None
    current_rate: float | None = None
    target_rate: float | None = None
    would_change: bool | None = None
    balance_remaining: float | None = None
    balance_total: float | None = None
    balance_used: float | None = None
    balance_unit: str | None = None
    balance_status: str | None = None
    balance_message: str | None = None
    balance_checked_at: datetime | None = None
    upstream_usage_amount: float | None = None
    upstream_usage_unit: str | None = None
    upstream_usage_checked_at: datetime | None = None
    last_error: str | None = None
    last_discovered_at: datetime | None = None
    last_applied_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class UpstreamChannelOut(BaseModel):
    id: int = Field(ge=1, le=JS_SAFE_INTEGER_MAX)
    display_name: str
    canonical_base_url: str
    base_url: str
    management_base_url: str | None = None
    upstream_type: Literal["auto", "newapi", "sub2api"] = "auto"
    probe_enabled: bool = True
    resolved_upstream_type: Literal["newapi", "sub2api"] | None = None
    access_token_set: bool = False
    refresh_token_set: bool = False
    upstream_user_id: str | None = None
    manual_recharge_multiplier: float | None = None
    discovered_recharge_multiplier: float | None = None
    effective_recharge_multiplier: float | None = None
    recharge_multiplier_source: str | None = None
    recharge_multiplier_status: str | None = None
    group_options: list[UpstreamGroupOptionOut] = Field(default_factory=list)
    balance_remaining: float | None = None
    balance_total: float | None = None
    balance_used: float | None = None
    balance_unit: str | None = None
    balance_status: str | None = None
    balance_message: str | None = None
    balance_checked_at: datetime | None = None
    today_balance_used: float | None = None
    today_balance_unit: str | None = None
    today_balance_status: str | None = None
    today_balance_checked_at: datetime | None = None
    yesterday_balance_used: float | None = None
    yesterday_balance_unit: str | None = None
    yesterday_balance_status: str | None = None
    yesterday_balance_checked_at: datetime | None = None
    last_error: str | None = None
    last_discovered_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    account_count: int = Field(default=0, ge=0)
    accounts: list["UpstreamAccountOut"] = Field(default_factory=list)


class UpstreamOverviewOut(BaseModel):
    channels: list[UpstreamChannelOut] = Field(default_factory=list)
    unassigned_accounts: list[UpstreamAccountOut] = Field(default_factory=list)
    priority_intervals: list[PriorityIntervalOut] = Field(default_factory=list)
    local_recharge_multiplier: float | None = None
    local_recharge_source: str | None = None
    local_recharge_status: str | None = None


class UpstreamChannelDiscoverAllOut(BaseModel):
    total: int = Field(ge=0)
    succeeded: int = Field(ge=0)
    failed: int = Field(ge=0)
    cached: int = Field(default=0, ge=0)
    skipped: int = Field(default=0, ge=0)
    force: bool = True
    cache_max_age_seconds: int | None = Field(default=None, ge=0)
    probe_globally_enabled: bool = True
    duration_ms: int | None = Field(default=None, ge=0)
    inventory_duration_ms: int | None = Field(default=None, ge=0)
    probe_duration_ms: int | None = Field(default=None, ge=0)
    priority_duration_ms: int | None = Field(default=None, ge=0)
    channels: list[UpstreamChannelOut] = Field(default_factory=list)
    overview: UpstreamOverviewOut | None = None


class UpstreamDiscoverAllOut(BaseModel):
    total: int = Field(ge=0)
    succeeded: int = Field(ge=0)
    failed: int = Field(ge=0)
    accounts: list[UpstreamAccountOut] = Field(default_factory=list)
