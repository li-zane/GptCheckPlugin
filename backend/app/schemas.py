from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field


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


class UsageGroupRef(BaseModel):
    id: str
    name: str


class UsageWindowEstimate(BaseModel):
    used_percent: float | None
    spent: float | None
    spend_source: str | None
    estimated_limit: float | None
    remaining: float | None
    remaining_percent: float | None
    reset_at: str | None
    remaining_seconds: int | None
    requests: int | None
    tokens: int | None
    estimable: bool
    source: str


class UsageWindowAggregate(BaseModel):
    spent: float
    estimated_limit: float | None
    remaining: float | None
    remaining_percent: float | None
    used_percent: float | None
    account_count: int
    enabled_account_count: int
    estimable_accounts: int


class AccountUsageEstimateOut(BaseModel):
    email: str
    sub2api_account_id: str | None
    platform: str | None
    account_type: str | None
    status: str | None
    schedulable: bool | None
    deactive: bool
    usage_estimate_enabled: bool
    rate_multiplier: float
    groups: list[UsageGroupRef]
    usage_error: str | None
    five_hour: UsageWindowEstimate
    seven_day: UsageWindowEstimate


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


class MailboxImportRequest(BaseModel):
    content: str = Field(min_length=1)
    default_provider: Literal["auto", "outlook", "hotmail", "custom", "manual"] = "auto"


class MailboxImportResult(MessageResponse):
    imported: int
    skipped: int
    invalid_lines: list[int]


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


class DashboardSummary(BaseModel):
    total_accounts: int
    error_accounts: int
    deactive_accounts: int
    refreshing_accounts: int
    mailbox_count: int
    recent_success: int
    recent_failed: int


class ManualRefreshRequest(BaseModel):
    email: EmailStr


class UsageEstimatePreferenceUpdate(BaseModel):
    enabled: bool


class Sub2ApiSyncResult(BaseModel):
    total_seen: int
    error_seen: int
    queued: int


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


class DeactivatedCleanupResult(MessageResponse):
    deleted_accounts: int
    deleted_mailboxes: int
    deleted_sub2api_accounts: int
    failed_sub2api_accounts: list[str]


class AppSettingsOut(BaseModel):
    sub2api_base_url: str
    sub2api_port: int | None
    sub2api_base_url_source: str
    sub2api_x_api_key_set: bool
    sub2api_x_api_key_hint: str | None
    monitor_interval_seconds: int
    usage_refresh_enabled: bool
    usage_refresh_interval_seconds: int
    refresh_max_concurrency: int
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
    monitor_interval_seconds: int | None = Field(default=None, ge=30, le=86_400)
    usage_refresh_enabled: bool | None = None
    usage_refresh_interval_seconds: int | None = Field(default=None, ge=60, le=86_400)
    refresh_max_concurrency: int | None = Field(default=None, ge=1, le=50)
    display_timezone: str | None = Field(default=None, min_length=1, max_length=80)
    site_name: str | None = Field(default=None, min_length=1, max_length=80)


class Sub2ApiPortScanResult(BaseModel):
    found: bool
    base_url: str | None
    port: int | None
    status: str
    message: str
    checked_ports: list[int]
    applied: bool
