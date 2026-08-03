from datetime import date, datetime, timezone

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.sqlite import JSON
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class AccountSnapshot(Base):
    __tablename__ = "account_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str] = mapped_column(String(320), index=True, unique=True)
    sub2api_account_id: Mapped[str | None] = mapped_column(String(64), index=True)
    platform: Mapped[str | None] = mapped_column(String(64), index=True)
    account_type: Mapped[str | None] = mapped_column(String(64), index=True)
    status: Mapped[str | None] = mapped_column(String(64), index=True)
    schedulable: Mapped[bool | None] = mapped_column(Boolean)
    usage_estimate_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    deactive: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    refreshing: Mapped[bool] = mapped_column(Boolean, default=False)
    auto_refresh_locked: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    subscription_starts_at: Mapped[str | None] = mapped_column(String(128))
    subscription_expires_at: Mapped[str | None] = mapped_column(String(128))
    subscription_renews_at: Mapped[str | None] = mapped_column(String(128))
    subscription_cancels_at: Mapped[str | None] = mapped_column(String(128))
    subscription_billing_period: Mapped[str | None] = mapped_column(String(64))
    subscription_plan: Mapped[str | None] = mapped_column(String(128))
    has_active_subscription: Mapped[bool | None] = mapped_column(Boolean)
    subscription_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    available_models: Mapped[list[dict] | None] = mapped_column(JSON)
    available_models_status: Mapped[str] = mapped_column(
        String(32), default="not_checked", server_default="not_checked", nullable=False
    )
    available_models_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    phone_note_sync_marker: Mapped[str | None] = mapped_column(String(64))
    encrypted_openai_refresh_token: Mapped[str | None] = mapped_column(Text)
    encrypted_openai_access_token: Mapped[str | None] = mapped_column(Text)
    encrypted_openai_id_token: Mapped[str | None] = mapped_column(Text)
    encrypted_openai_client_id: Mapped[str | None] = mapped_column(Text)
    openai_token_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    raw: Mapped[dict | None] = mapped_column(JSON)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class AccountEditPreset(Base):
    __tablename__ = "account_edit_presets"
    __table_args__ = (UniqueConstraint("platform", "name", name="uq_account_edit_preset_platform_name"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    platform: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    configuration: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class MailboxCredential(Base):
    __tablename__ = "mailbox_credentials"
    __table_args__ = (UniqueConstraint("gpt_email", name="uq_mailbox_gpt_email"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    gpt_email: Mapped[str] = mapped_column(String(320), index=True)
    mailbox_email: Mapped[str] = mapped_column(String(320), index=True)
    provider: Mapped[str] = mapped_column(String(32), default="outlook", index=True)
    encrypted_password: Mapped[str | None] = mapped_column(Text)
    encrypted_client_id: Mapped[str | None] = mapped_column(Text)
    encrypted_refresh_token: Mapped[str | None] = mapped_column(Text)
    encrypted_access_token: Mapped[str | None] = mapped_column(Text)
    custom_fetch_url: Mapped[str | None] = mapped_column(Text)
    proxy_url: Mapped[str | None] = mapped_column(Text)
    disabled: Mapped[bool] = mapped_column(Boolean, default=False)
    last_error: Mapped[str | None] = mapped_column(Text)
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class PhoneNumber(Base):
    __tablename__ = "phone_numbers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    phone_key: Mapped[str] = mapped_column(String(32), index=True, unique=True)
    phone_number: Mapped[str] = mapped_column(String(32))
    sms_url: Mapped[str] = mapped_column(Text)
    sms_cdk: Mapped[str | None] = mapped_column(String(256))
    sms_recharge_url: Mapped[str | None] = mapped_column(Text)
    sms_status: Mapped[str | None] = mapped_column(String(32), index=True)
    sms_error: Mapped[str | None] = mapped_column(Text)
    sms_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class PhoneAccountBinding(Base):
    __tablename__ = "phone_account_bindings"
    __table_args__ = (
        UniqueConstraint("phone_id", "account_email", name="uq_phone_binding_phone_email"),
        UniqueConstraint("account_email", name="uq_phone_binding_account_email"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    phone_id: Mapped[int] = mapped_column(ForeignKey("phone_numbers.id", ondelete="CASCADE"), index=True)
    account_email: Mapped[str] = mapped_column(String(320), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class RefreshJob(Base):
    __tablename__ = "refresh_jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str] = mapped_column(String(320), index=True)
    sub2api_account_id: Mapped[str | None] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(32), default="queued", index=True)
    reason: Mapped[str | None] = mapped_column(Text)
    access_token_tail: Mapped[str | None] = mapped_column(String(16))
    memory_peak_rss_bytes: Mapped[int | None] = mapped_column(Integer)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class AppEvent(Base):
    __tablename__ = "app_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[str] = mapped_column(String(64), index=True)
    email: Mapped[str | None] = mapped_column(String(320), index=True)
    message: Mapped[str] = mapped_column(Text)
    details: Mapped[dict | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class AccountExceptionRecord(Base):
    __tablename__ = "account_exception_records"
    __table_args__ = (UniqueConstraint("fingerprint", name="uq_account_exception_records_fingerprint"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    fingerprint: Mapped[str] = mapped_column(String(384), index=True)
    email: Mapped[str | None] = mapped_column(String(320), index=True)
    sub2api_account_id: Mapped[str | None] = mapped_column(String(64), index=True)
    source: Mapped[str] = mapped_column(String(32), index=True)
    status: Mapped[str] = mapped_column(String(32), index=True)
    message: Mapped[str] = mapped_column(Text)
    details: Mapped[dict | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, index=True)


class UsageWindowState(Base):
    __tablename__ = "usage_window_states"
    __table_args__ = (UniqueConstraint("account_key", "window_key", name="uq_usage_window_state_account_window"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_key: Mapped[str] = mapped_column(String(384), index=True)
    email: Mapped[str | None] = mapped_column(String(320), index=True)
    sub2api_account_id: Mapped[str | None] = mapped_column(String(64), index=True)
    window_key: Mapped[str] = mapped_column(String(32), index=True)
    baseline_spent: Mapped[float | None] = mapped_column(Float)
    estimate_uses_spent_delta: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    last_raw_spent: Mapped[float | None] = mapped_column(Float)
    last_used_percent: Mapped[float | None] = mapped_column(Float)
    last_reset_at: Mapped[str | None] = mapped_column(String(128))
    last_remaining_seconds: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class UsageLimitSample(Base):
    __tablename__ = "usage_limit_samples"
    __table_args__ = (UniqueConstraint("account_key", "window_key", "reset_key", name="uq_usage_limit_sample_account_window_reset"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_key: Mapped[str] = mapped_column(String(384), index=True)
    email: Mapped[str | None] = mapped_column(String(320), index=True)
    sub2api_account_id: Mapped[str | None] = mapped_column(String(64), index=True)
    plan_cohort: Mapped[str] = mapped_column(String(32), default="unknown", nullable=False, index=True)
    window_key: Mapped[str] = mapped_column(String(32), index=True)
    reset_key: Mapped[str] = mapped_column(String(256), index=True)
    reset_at: Mapped[str | None] = mapped_column(String(128))
    observed_limit: Mapped[float] = mapped_column(Float, index=True)
    raw_spent: Mapped[float] = mapped_column(Float)
    used_percent: Mapped[float] = mapped_column(Float)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class UsageTokenWindow(Base):
    __tablename__ = "usage_token_windows"
    __table_args__ = (UniqueConstraint("account_key", "window_key", "window_reset_key", name="uq_usage_token_window_account_window_reset"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_key: Mapped[str] = mapped_column(String(384), index=True)
    email: Mapped[str | None] = mapped_column(String(320), index=True)
    sub2api_account_id: Mapped[str | None] = mapped_column(String(64), index=True)
    window_key: Mapped[str] = mapped_column(String(32), index=True)
    window_reset_key: Mapped[str] = mapped_column(String(256), index=True)
    window_start_at: Mapped[str | None] = mapped_column(String(128))
    reset_at: Mapped[str | None] = mapped_column(String(128))
    spent: Mapped[float] = mapped_column(Float, default=0, nullable=False)
    tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    first_observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class AppSetting(Base):
    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class UsageEstimateCache(Base):
    __tablename__ = "usage_estimate_cache"

    key: Mapped[str] = mapped_column(String(32), primary_key=True, default="latest")
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class UpstreamChannel(Base):
    __tablename__ = "upstream_channels"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    display_name: Mapped[str] = mapped_column(String(200), nullable=False)
    canonical_base_url: Mapped[str] = mapped_column(String(500), unique=True, index=True, nullable=False)
    management_base_url: Mapped[str | None] = mapped_column(String(500))
    upstream_type: Mapped[str] = mapped_column(String(32), default="auto", nullable=False)
    probe_enabled: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        server_default="1",
        nullable=False,
    )
    resolved_upstream_type: Mapped[str | None] = mapped_column(String(32))
    encrypted_access_token: Mapped[str | None] = mapped_column(Text)
    encrypted_refresh_token: Mapped[str | None] = mapped_column(Text)
    upstream_user_id: Mapped[str | None] = mapped_column(String(128))
    manual_recharge_multiplier: Mapped[float | None] = mapped_column(Float)
    discovered_recharge_multiplier: Mapped[float | None] = mapped_column(Float)
    effective_recharge_multiplier: Mapped[float | None] = mapped_column(Float)
    last_known_recharge_multiplier: Mapped[float | None] = mapped_column(Float)
    recharge_multiplier_source: Mapped[str | None] = mapped_column(String(128))
    recharge_multiplier_status: Mapped[str | None] = mapped_column(String(32))
    group_options: Mapped[list[dict] | None] = mapped_column(JSON)
    pending_group_options: Mapped[list[dict] | None] = mapped_column(JSON)
    pending_group_removal_count: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )
    pending_group_removal_checked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    balance_remaining: Mapped[float | None] = mapped_column(Float)
    balance_total: Mapped[float | None] = mapped_column(Float)
    balance_used: Mapped[float | None] = mapped_column(Float)
    balance_unit: Mapped[str | None] = mapped_column(String(32))
    balance_status: Mapped[str | None] = mapped_column(String(32))
    balance_source: Mapped[str | None] = mapped_column(String(64))
    balance_message: Mapped[str | None] = mapped_column(Text)
    balance_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    today_balance_used: Mapped[float | None] = mapped_column(Float)
    today_balance_unit: Mapped[str | None] = mapped_column(String(32))
    today_balance_status: Mapped[str | None] = mapped_column(String(32))
    today_balance_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    yesterday_balance_used: Mapped[float | None] = mapped_column(Float)
    yesterday_balance_unit: Mapped[str | None] = mapped_column(String(32))
    yesterday_balance_status: Mapped[str | None] = mapped_column(String(32))
    yesterday_balance_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    balance_guard_state: Mapped[str] = mapped_column(
        String(32), default="not_checked", server_default="not_checked", nullable=False
    )
    balance_guard_basis: Mapped[str | None] = mapped_column(String(32))
    balance_guard_value: Mapped[float | None] = mapped_column(Float)
    balance_guard_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    balance_guard_episode_id: Mapped[str | None] = mapped_column(String(64))
    balance_guard_paused_count: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )
    channel_monitors: Mapped[list[dict] | None] = mapped_column(JSON)
    channel_monitor_test_models: Mapped[dict | None] = mapped_column(JSON)
    channel_monitor_count: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )
    channel_monitor_status: Mapped[str] = mapped_column(
        String(32), default="not_checked", server_default="not_checked", nullable=False
    )
    channel_monitor_message: Mapped[str | None] = mapped_column(Text)
    channel_monitor_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    channel_monitor_guard_state: Mapped[str] = mapped_column(
        String(32), default="not_checked", server_default="not_checked", nullable=False
    )
    channel_monitor_unavailable_count: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )
    channel_monitor_recovery_count: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )
    channel_monitor_guard_checked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    last_error: Mapped[str | None] = mapped_column(Text)
    last_discovered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class UpstreamChannelDailyUsage(Base):
    """A durable per-day upstream balance and linked account revenue snapshot."""

    __tablename__ = "upstream_channel_daily_usages"
    __table_args__ = (
        UniqueConstraint(
            "channel_identity",
            "usage_date",
            name="uq_upstream_channel_daily_usage_identity_date",
        ),
        Index("ix_upstream_channel_daily_usage_identity_date", "channel_identity", "usage_date"),
        Index("ix_upstream_channel_daily_usage_channel_date", "channel_id", "usage_date"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    channel_id: Mapped[int] = mapped_column(Integer, index=True, nullable=False)
    # Channel database IDs can be reused by SQLite after deletion. The
    # canonical URL identifies the upstream that actually produced this row.
    channel_identity: Mapped[str] = mapped_column(String(500), index=True, nullable=False)
    channel_name: Mapped[str | None] = mapped_column(String(200))
    usage_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    balance_used: Mapped[float | None] = mapped_column(Float)
    balance_used_adjusted: Mapped[float | None] = mapped_column(Float)
    balance_unit: Mapped[str | None] = mapped_column(String(32))
    recharge_multiplier: Mapped[float | None] = mapped_column(Float)
    upstream_api_key_usage: Mapped[float | None] = mapped_column(Float)
    sub2api_actual_cost: Mapped[float | None] = mapped_column(Float)
    income: Mapped[float | None] = mapped_column(Float)
    income_recharge_multiplier: Mapped[float | None] = mapped_column(Float)
    income_unit: Mapped[str | None] = mapped_column(String(32))
    finalized: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="0", nullable=False, index=True
    )
    observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finalized_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class UpstreamAccountDailyUsage(Base):
    """Per API key usage/revenue retained for filtering channel history."""

    __tablename__ = "upstream_account_daily_usages"
    __table_args__ = (
        UniqueConstraint(
            "sub2api_account_id",
            "usage_date",
            name="uq_upstream_account_daily_usage_account_date",
        ),
        Index(
            "ix_upstream_account_daily_usage_identity_date",
            "channel_identity",
            "usage_date",
        ),
        Index(
            "ix_upstream_account_daily_usage_account_date",
            "sub2api_account_id",
            "usage_date",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    channel_id: Mapped[int] = mapped_column(Integer, index=True, nullable=False)
    channel_identity: Mapped[str] = mapped_column(String(500), index=True, nullable=False)
    sub2api_account_id: Mapped[int] = mapped_column(BigInteger, index=True, nullable=False)
    upstream_api_key_record_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    account_name: Mapped[str | None] = mapped_column(String(200))
    remote_identity_fingerprint: Mapped[str | None] = mapped_column(String(64))
    usage_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    upstream_usage: Mapped[float | None] = mapped_column(Float)
    upstream_usage_unit: Mapped[str | None] = mapped_column(String(32))
    upstream_usage_source: Mapped[str | None] = mapped_column(String(64))
    sub2api_actual_cost: Mapped[float | None] = mapped_column(Float)
    local_recharge_multiplier: Mapped[float | None] = mapped_column(Float)
    income: Mapped[float | None] = mapped_column(Float)
    income_unit: Mapped[str | None] = mapped_column(String(32))
    finalized: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="0", nullable=False, index=True
    )
    observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class UpstreamChannelUsageTotal(Base):
    """Lifetime aggregates which deliberately survive daily-detail pruning."""

    __tablename__ = "upstream_channel_usage_totals"

    channel_identity: Mapped[str] = mapped_column(String(500), primary_key=True)
    channel_id: Mapped[int] = mapped_column(Integer, index=True, nullable=False)
    channel_name: Mapped[str | None] = mapped_column(String(200))
    total_balance_used: Mapped[float] = mapped_column(Float, default=0, nullable=False)
    total_balance_used_adjusted: Mapped[float] = mapped_column(
        Float, default=0, nullable=False
    )
    total_upstream_api_key_usage: Mapped[float] = mapped_column(
        Float, default=0, nullable=False
    )
    total_sub2api_actual_cost: Mapped[float] = mapped_column(Float, default=0, nullable=False)
    total_income: Mapped[float] = mapped_column(Float, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class UpstreamPriorityInterval(Base):
    __tablename__ = "upstream_priority_intervals"
    __table_args__ = (
        CheckConstraint("start_priority >= 0", name="ck_upstream_priority_interval_start"),
        CheckConstraint("end_priority > start_priority", name="ck_upstream_priority_interval_end"),
        CheckConstraint("step >= 1", name="ck_upstream_priority_interval_step"),
        CheckConstraint(
            "allocation_strategy IN ('cost_optimized', 'fixed_step')",
            name="ck_upstream_priority_interval_allocation_strategy",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True, index=True, nullable=False)
    start_priority: Mapped[int] = mapped_column(Integer, nullable=False)
    end_priority: Mapped[int] = mapped_column(Integer, nullable=False)
    step: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    allocation_strategy: Mapped[str] = mapped_column(
        String(32), default="cost_optimized", server_default="cost_optimized", nullable=False
    )
    rate_pause_enabled: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="0", nullable=False
    )
    rate_absolute_threshold: Mapped[float] = mapped_column(
        Float, default=1.0, server_default="1", nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class UpstreamAccountConfig(Base):
    __tablename__ = "upstream_account_configs"
    __table_args__ = (
        Index(
            "uq_upstream_account_configs_channel_record_id",
            "channel_id",
            "upstream_api_key_record_id",
            unique=True,
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    sub2api_account_id: Mapped[int] = mapped_column(
        BigInteger,
        unique=True,
        index=True,
        nullable=False,
    )
    remote_identity_fingerprint: Mapped[str | None] = mapped_column(String(64))
    upstream_api_key_record_id: Mapped[int | None] = mapped_column(BigInteger)
    upstream_identity_rebind_required: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        server_default="0",
        nullable=False,
    )
    api_key_origin_rebind_required: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        nullable=False,
    )
    channel_id: Mapped[int | None] = mapped_column(
        ForeignKey("upstream_channels.id", ondelete="SET NULL"),
        index=True,
    )
    channel_auto_assign_disabled: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        nullable=False,
    )
    priority_interval_id: Mapped[int | None] = mapped_column(
        ForeignKey("upstream_priority_intervals.id", ondelete="SET NULL"),
        index=True,
    )
    desired_priority: Mapped[int | None] = mapped_column(Integer)
    priority_sync_status: Mapped[str] = mapped_column(
        String(32),
        default="unassigned",
        server_default="unassigned",
        nullable=False,
    )
    priority_sync_error: Mapped[str | None] = mapped_column(Text)
    last_priority_applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    priority_tiebreak_order: Mapped[int | None] = mapped_column(Integer)
    priority_tiebreak_multiplier: Mapped[float | None] = mapped_column(Float)
    priority_assignment_when_disabled: Mapped[bool | None] = mapped_column(Boolean)
    rate_pause_policy: Mapped[str] = mapped_column(
        String(16), default="inherit", server_default="inherit", nullable=False
    )
    rate_absolute_threshold: Mapped[float | None] = mapped_column(Float)
    remote_name: Mapped[str | None] = mapped_column(String(200))
    remote_platform: Mapped[str | None] = mapped_column(String(64))
    remote_account_type: Mapped[str | None] = mapped_column(String(32))
    remote_status: Mapped[str | None] = mapped_column(String(64))
    remote_schedulable: Mapped[bool | None] = mapped_column(Boolean)
    remote_priority: Mapped[int | None] = mapped_column(Integer)
    remote_snapshot: Mapped[dict | None] = mapped_column(JSON)
    remote_snapshot_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    remote_present: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default="1", nullable=False, index=True
    )
    remote_missing_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    base_url: Mapped[str | None] = mapped_column(String(500))
    encrypted_api_key: Mapped[str | None] = mapped_column(Text)
    encrypted_access_token: Mapped[str | None] = mapped_column(Text)
    upstream_type: Mapped[str] = mapped_column(String(32), default="auto", nullable=False)
    resolved_upstream_type: Mapped[str | None] = mapped_column(String(32))
    upstream_user_id: Mapped[str | None] = mapped_column(String(128))
    selected_group_id: Mapped[str | None] = mapped_column(String(128))
    selected_group_name: Mapped[str | None] = mapped_column(String(200))
    upstream_key_status: Mapped[str] = mapped_column(
        String(32),
        default="not_checked",
        server_default="not_checked",
        nullable=False,
    )
    upstream_group_status: Mapped[str] = mapped_column(
        String(32),
        default="not_checked",
        server_default="not_checked",
        nullable=False,
    )
    upstream_health_invalid_count: Mapped[int] = mapped_column(
        Integer,
        default=0,
        server_default="0",
        nullable=False,
    )
    upstream_key_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    upstream_group_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    availability_check_mode: Mapped[str] = mapped_column(
        String(32), default="channel_monitor", server_default="channel_monitor", nullable=False
    )
    availability_monitor_id: Mapped[int | None] = mapped_column(Integer)
    availability_test_model: Mapped[str | None] = mapped_column(String(160))
    available_models: Mapped[list[dict] | None] = mapped_column(JSON)
    available_models_status: Mapped[str] = mapped_column(
        String(32), default="not_checked", server_default="not_checked", nullable=False
    )
    available_models_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    availability_status: Mapped[str] = mapped_column(
        String(32), default="not_checked", server_default="not_checked", nullable=False
    )
    availability_unavailable_count: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )
    availability_recovery_count: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )
    availability_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    availability_source: Mapped[str | None] = mapped_column(String(32))
    availability_message: Mapped[str | None] = mapped_column(Text)
    auto_disabled_reason: Mapped[str | None] = mapped_column(String(64))
    last_auto_disabled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    balance_guard_restore_eligible: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="0", nullable=False
    )
    balance_guard_channel_id: Mapped[int | None] = mapped_column(Integer)
    balance_guard_paused_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    balance_guard_operation: Mapped[str | None] = mapped_column(String(32))
    auto_pause_episode_id: Mapped[str | None] = mapped_column(String(64))
    pause_owned_by_plugin: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="0", nullable=False
    )
    auto_pause_channel_id: Mapped[int | None] = mapped_column(Integer)
    auto_paused_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    pause_operation: Mapped[str | None] = mapped_column(String(32))
    manual_group_multiplier: Mapped[float | None] = mapped_column(Float)
    manual_recharge_multiplier: Mapped[float | None] = mapped_column(Float)
    group_options: Mapped[list[dict] | None] = mapped_column(JSON)
    discovered_group_multiplier: Mapped[float | None] = mapped_column(Float)
    effective_group_multiplier: Mapped[float | None] = mapped_column(Float)
    group_multiplier_source: Mapped[str | None] = mapped_column(String(128))
    group_multiplier_status: Mapped[str | None] = mapped_column(String(32))
    discovered_recharge_multiplier: Mapped[float | None] = mapped_column(Float)
    effective_recharge_multiplier: Mapped[float | None] = mapped_column(Float)
    recharge_multiplier_source: Mapped[str | None] = mapped_column(String(128))
    recharge_multiplier_status: Mapped[str | None] = mapped_column(String(32))
    local_recharge_multiplier: Mapped[float | None] = mapped_column(Float)
    local_recharge_source: Mapped[str | None] = mapped_column(String(32))
    local_recharge_status: Mapped[str | None] = mapped_column(String(32))
    target_rate: Mapped[float | None] = mapped_column(Float)
    current_rate: Mapped[float | None] = mapped_column(Float)
    balance_remaining: Mapped[float | None] = mapped_column(Float)
    balance_total: Mapped[float | None] = mapped_column(Float)
    balance_used: Mapped[float | None] = mapped_column(Float)
    balance_unit: Mapped[str | None] = mapped_column(String(32))
    balance_status: Mapped[str | None] = mapped_column(String(32))
    balance_source: Mapped[str | None] = mapped_column(String(64))
    balance_message: Mapped[str | None] = mapped_column(Text)
    balance_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    upstream_usage_amount: Mapped[float | None] = mapped_column(Float)
    upstream_usage_unit: Mapped[str | None] = mapped_column(String(32))
    upstream_usage_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    today_upstream_usage_amount: Mapped[float | None] = mapped_column(Float)
    today_upstream_usage_unit: Mapped[str | None] = mapped_column(String(32))
    today_upstream_usage_status: Mapped[str] = mapped_column(
        String(32), default="not_checked", server_default="not_checked", nullable=False
    )
    today_upstream_usage_source: Mapped[str | None] = mapped_column(String(64))
    today_upstream_usage_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    last_discovered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    pause_holds: Mapped[list["UpstreamAccountPauseHold"]] = relationship(
        back_populates="account_config",
        cascade="all, delete-orphan",
        lazy="selectin",
        order_by="UpstreamAccountPauseHold.id",
    )


class UpstreamAccountPauseHold(Base):
    __tablename__ = "upstream_account_pause_holds"
    __table_args__ = (
        UniqueConstraint(
            "account_config_id",
            "reason",
            name="uq_upstream_account_pause_hold_reason",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_config_id: Mapped[int] = mapped_column(
        ForeignKey("upstream_account_configs.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    reason: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    active: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default="1", index=True, nullable=False
    )
    scope_channel_id: Mapped[int | None] = mapped_column(Integer, index=True)
    triggered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    recovery_mode: Mapped[str] = mapped_column(String(64), nullable=False)
    evidence_json: Mapped[dict | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )
    account_config: Mapped["UpstreamAccountConfig"] = relationship(
        back_populates="pause_holds"
    )


class UpstreamRateChangeLog(Base):
    __tablename__ = "upstream_rate_change_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    sub2api_account_id: Mapped[int] = mapped_column(Integer, index=True, nullable=False)
    account_name: Mapped[str | None] = mapped_column(String(200))
    channel_id: Mapped[int | None] = mapped_column(Integer, index=True)
    channel_name: Mapped[str | None] = mapped_column(String(200))
    group_id: Mapped[str | None] = mapped_column(String(128))
    group_name: Mapped[str | None] = mapped_column(String(200))
    old_group_id: Mapped[str | None] = mapped_column(String(128))
    new_group_id: Mapped[str | None] = mapped_column(String(128))
    old_group_name: Mapped[str | None] = mapped_column(String(200))
    new_group_name: Mapped[str | None] = mapped_column(String(200))
    old_group_multiplier: Mapped[float | None] = mapped_column(Float)
    new_group_multiplier: Mapped[float | None] = mapped_column(Float)
    old_upstream_multiplier: Mapped[float | None] = mapped_column(Float)
    new_upstream_multiplier: Mapped[float | None] = mapped_column(Float)
    old_upstream_recharge_multiplier: Mapped[float | None] = mapped_column(Float)
    new_upstream_recharge_multiplier: Mapped[float | None] = mapped_column(Float)
    upstream_recharge_multiplier: Mapped[float | None] = mapped_column(Float)
    local_recharge_multiplier: Mapped[float | None] = mapped_column(Float)
    old_target_rate: Mapped[float | None] = mapped_column(Float)
    new_target_rate: Mapped[float | None] = mapped_column(Float)
    old_current_rate: Mapped[float | None] = mapped_column(Float)
    new_current_rate: Mapped[float | None] = mapped_column(Float)
    old_upstream_key_status: Mapped[str | None] = mapped_column(String(32))
    new_upstream_key_status: Mapped[str | None] = mapped_column(String(32))
    old_upstream_group_status: Mapped[str | None] = mapped_column(String(32))
    new_upstream_group_status: Mapped[str | None] = mapped_column(String(32))
    old_remote_schedulable: Mapped[bool | None] = mapped_column(Boolean)
    new_remote_schedulable: Mapped[bool | None] = mapped_column(Boolean)
    reason: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    safe_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class UpstreamAccountDataArchive(Base):
    """Last trustworthy API Key data retained before an identity reset."""

    __tablename__ = "upstream_account_data_archives"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    sub2api_account_id: Mapped[int] = mapped_column(Integer, index=True, nullable=False)
    remote_identity_fingerprint: Mapped[str | None] = mapped_column(String(64))
    account_name: Mapped[str | None] = mapped_column(String(200))
    channel_id: Mapped[int | None] = mapped_column(Integer, index=True)
    channel_name: Mapped[str | None] = mapped_column(String(200))
    reason: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    snapshot: Mapped[dict] = mapped_column(JSON, nullable=False)
    observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )


class UpstreamChannelChangeEvent(Base):
    __tablename__ = "upstream_channel_change_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    channel_id: Mapped[int | None] = mapped_column(Integer, index=True)
    channel_name: Mapped[str | None] = mapped_column(String(200))
    event_type: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    group_id: Mapped[str | None] = mapped_column(String(128), index=True)
    group_name: Mapped[str | None] = mapped_column(String(200))
    old_value: Mapped[float | None] = mapped_column(Float)
    new_value: Mapped[float | None] = mapped_column(Float)
    old_status: Mapped[str | None] = mapped_column(String(32))
    new_status: Mapped[str | None] = mapped_column(String(32))
    details: Mapped[dict | None] = mapped_column(JSON)
    legacy_imported: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="0", index=True, nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )


class AccountSchedulingChangeLog(Base):
    __tablename__ = "account_scheduling_change_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    sub2api_account_id: Mapped[int] = mapped_column(Integer, index=True, nullable=False)
    account_name: Mapped[str | None] = mapped_column(String(200))
    channel_id: Mapped[int | None] = mapped_column(Integer, index=True)
    channel_name: Mapped[str | None] = mapped_column(String(200))
    event_type: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    reason: Mapped[str | None] = mapped_column(String(64), index=True)
    active_reasons: Mapped[list[str] | None] = mapped_column(JSON)
    evidence: Mapped[dict | None] = mapped_column(JSON)
    old_schedulable: Mapped[bool | None] = mapped_column(Boolean)
    new_schedulable: Mapped[bool | None] = mapped_column(Boolean)
    status: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    safe_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )


class NotificationOutbox(Base):
    __tablename__ = "notification_outbox"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    event_type: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    dedupe_key: Mapped[str] = mapped_column(String(256), unique=True, index=True, nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    details: Mapped[dict | None] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(
        String(32), default="pending", server_default="pending", index=True, nullable=False
    )
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    last_error: Mapped[str | None] = mapped_column(Text)
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    claim_token: Mapped[str | None] = mapped_column(String(64), index=True)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    claim_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
