from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.sqlite import JSON
from sqlalchemy.orm import Mapped, mapped_column

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


class UpstreamChannel(Base):
    __tablename__ = "upstream_channels"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    display_name: Mapped[str] = mapped_column(String(200), nullable=False)
    canonical_base_url: Mapped[str] = mapped_column(String(500), unique=True, index=True, nullable=False)
    management_base_url: Mapped[str | None] = mapped_column(String(500))
    upstream_type: Mapped[str] = mapped_column(String(32), default="auto", nullable=False)
    resolved_upstream_type: Mapped[str | None] = mapped_column(String(32))
    encrypted_access_token: Mapped[str | None] = mapped_column(Text)
    encrypted_refresh_token: Mapped[str | None] = mapped_column(Text)
    upstream_user_id: Mapped[str | None] = mapped_column(String(128))
    manual_recharge_multiplier: Mapped[float | None] = mapped_column(Float)
    discovered_recharge_multiplier: Mapped[float | None] = mapped_column(Float)
    effective_recharge_multiplier: Mapped[float | None] = mapped_column(Float)
    recharge_multiplier_source: Mapped[str | None] = mapped_column(String(128))
    recharge_multiplier_status: Mapped[str | None] = mapped_column(String(32))
    group_options: Mapped[list[dict] | None] = mapped_column(JSON)
    balance_remaining: Mapped[float | None] = mapped_column(Float)
    balance_total: Mapped[float | None] = mapped_column(Float)
    balance_used: Mapped[float | None] = mapped_column(Float)
    balance_unit: Mapped[str | None] = mapped_column(String(32))
    balance_status: Mapped[str | None] = mapped_column(String(32))
    balance_message: Mapped[str | None] = mapped_column(Text)
    balance_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    last_discovered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class UpstreamAccountConfig(Base):
    __tablename__ = "upstream_account_configs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    sub2api_account_id: Mapped[int] = mapped_column(Integer, unique=True, index=True, nullable=False)
    remote_identity_fingerprint: Mapped[str | None] = mapped_column(String(64))
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
    remote_name: Mapped[str | None] = mapped_column(String(200))
    remote_platform: Mapped[str | None] = mapped_column(String(64))
    remote_account_type: Mapped[str | None] = mapped_column(String(32))
    base_url: Mapped[str | None] = mapped_column(String(500))
    encrypted_api_key: Mapped[str | None] = mapped_column(Text)
    encrypted_access_token: Mapped[str | None] = mapped_column(Text)
    upstream_type: Mapped[str] = mapped_column(String(32), default="auto", nullable=False)
    resolved_upstream_type: Mapped[str | None] = mapped_column(String(32))
    upstream_user_id: Mapped[str | None] = mapped_column(String(128))
    selected_group_id: Mapped[str | None] = mapped_column(String(128))
    selected_group_name: Mapped[str | None] = mapped_column(String(200))
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
    balance_message: Mapped[str | None] = mapped_column(Text)
    balance_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    last_discovered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class UpstreamRateChangeLog(Base):
    __tablename__ = "upstream_rate_change_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    sub2api_account_id: Mapped[int] = mapped_column(Integer, index=True, nullable=False)
    account_name: Mapped[str | None] = mapped_column(String(200))
    channel_id: Mapped[int | None] = mapped_column(Integer, index=True)
    channel_name: Mapped[str | None] = mapped_column(String(200))
    group_id: Mapped[str | None] = mapped_column(String(128))
    group_name: Mapped[str | None] = mapped_column(String(200))
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
    reason: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    safe_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
