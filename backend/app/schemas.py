from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator, model_validator

from app.core.subscription_types import (
    MAX_SUBSCRIPTION_TYPES,
    MAX_USAGE_LIMIT_VALUE,
    SUBSCRIPTION_TYPE_UNKNOWN,
    normalize_subscription_type,
)


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


class Sub2ApiSyncResult(MessageResponse):
    total_seen: int
    error_seen: int
    queued: int
    duplicate_accounts_ignored: int = 0
    deleted_accounts: int = 0
    deleted_mailboxes: int = 0


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
    recovery_enabled: bool
    monitor_interval_seconds: int
    usage_refresh_enabled: bool
    usage_refresh_interval_seconds: int
    usage_refresh_max_concurrency: int
    usage_limit_sample_five_hour_threshold_percent: float
    usage_limit_sample_seven_day_threshold_percent: float
    usage_limit_default_ranges: dict[str, UsageLimitPlanRanges]
    refresh_max_concurrency: int
    protocol_refresh_max_concurrency: int
    browser_refresh_max_concurrency: int
    browser_min_available_memory_mb: int
    subscription_refresh_batch_size: int
    subscription_refresh_max_concurrency: int
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
    sub2api_auto_recover_state: bool | None = None
    automation_paused: bool | None = None
    recovery_enabled: bool | None = None
    monitor_interval_seconds: int | None = Field(default=None, ge=30, le=86_400)
    usage_refresh_enabled: bool | None = None
    usage_refresh_interval_seconds: int | None = Field(default=None, ge=60, le=86_400)
    usage_refresh_max_concurrency: int | None = Field(default=None, ge=1, le=20)
    usage_limit_sample_five_hour_threshold_percent: float | None = Field(default=None, ge=0, le=100)
    usage_limit_sample_seven_day_threshold_percent: float | None = Field(default=None, ge=0, le=100)
    usage_limit_default_ranges: dict[str, UsageLimitPlanRanges] | None = None
    refresh_max_concurrency: int | None = Field(default=None, ge=1, le=50)
    protocol_refresh_max_concurrency: int | None = Field(default=None, ge=1, le=50)
    browser_refresh_max_concurrency: int | None = Field(default=None, ge=1, le=50)
    browser_min_available_memory_mb: int | None = Field(default=None, ge=0, le=1_048_576)
    subscription_refresh_batch_size: int | None = Field(default=None, ge=1, le=100)
    subscription_refresh_max_concurrency: int | None = Field(default=None, ge=1, le=20)
    display_timezone: str | None = Field(default=None, min_length=1, max_length=80)
    site_name: str | None = Field(default=None, min_length=1, max_length=80)

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
