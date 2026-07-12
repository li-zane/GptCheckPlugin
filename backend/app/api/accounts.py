import calendar
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import delete, desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import require_admin
from app.models import (
    AccountExceptionRecord,
    AccountSnapshot,
    AppEvent,
    AppSetting,
    MailboxCredential,
    PhoneAccountBinding,
    PhoneNumber,
    RefreshJob,
    UsageLimitSample,
    utcnow,
)
from app.schemas import (
    AccountExceptionRecordOut,
    AccountDeleteUnlockUpdate,
    AccountOut,
    AccountRefreshUnlockUpdate,
    DeactivatedCleanupResult,
    EventOut,
    ManualRefreshRequest,
    MessageResponse,
    RefreshJobOut,
    SelectedAccountDeleteRequest,
    SubscriptionRefreshFailureOut,
    SubscriptionRefreshResult,
    Sub2ApiSyncResult,
    UsageEstimateOut,
    UsageLimitCalibrationOut,
    UsageLimitSampleOut,
    UsageLimitSamplesOut,
    UsageLimitWindowSamplesOut,
    UsageEstimatePreferenceUpdate,
    UsageRefreshFailureOut,
    UsageRefreshResult,
)
from app.services.account_exceptions import (
    backfill_account_exception_records,
    clear_account_exception,
    prune_account_exception_records_to_current_accounts,
)
from app.services.events import record_event
from app.services.monitor import get_monitor_service
from app.services.refresh import get_refresh_service
from app.services.runtime_config import get_runtime_config_service
from app.services.sub2api import Sub2ApiClient, Sub2ApiRequestError, looks_deactive_text
from app.services.subscription_refresh import refresh_subscriptions
from app.services.usage_estimate import (
    LIMIT_SAMPLE_FULL_PERCENT,
    LIMIT_SAMPLE_TARGET,
    SAMPLE_WINDOWS,
    _default_limit_bounds,
    _default_sample_plan_cohorts,
    _limit_calibration,
    _plan_cohort_label,
    _plan_cohort_sort_key,
    _normalize_plan_cohort,
    _usage_limit_sample_allowed,
    account_rate_limited_windows,
    build_usage_estimate,
)
from app.services.usage_refresh import get_usage_refresh_service

router = APIRouter()
_ACCOUNT_DELETE_UNLOCK_PREFIX = "account_delete_unlock."

_SUBSCRIPTION_KEYS = (
    "subscription_starts_at",
    "subscription_expires_at",
    "subscription_renews_at",
    "subscription_cancels_at",
    "subscription_billing_period",
    "subscription_plan",
    "has_active_subscription",
)


def _deletable_duplicate_account_ids(
    accounts: list[dict[str, Any]],
    sub2api: Sub2ApiClient,
    metadata: dict[int, dict[str, Any]] | None = None,
) -> set[str]:
    metadata = metadata if metadata is not None else sub2api.account_duplicate_metadata(accounts)
    account_ids: set[str] = set()

    for index, account in enumerate(accounts):
        item = metadata.get(index)
        if not item or not item.get("is_duplicate") or item.get("duplicate_primary"):
            continue

        account_id = sub2api.account_id(account)
        primary_id = item.get("duplicate_primary_account_id")
        if not account_id or (primary_id is not None and account_id == str(primary_id)):
            continue

        account_ids.add(account_id)

    return account_ids


def _snapshot_has_local_deactive(snapshot: AccountSnapshot | None) -> bool:
    if snapshot is None:
        return False
    return bool(snapshot.deactive or looks_deactive_text(snapshot.last_error))


def _effective_deactive(
    sub2api: Sub2ApiClient,
    account: dict[str, Any],
    snapshot: AccountSnapshot | None,
) -> bool:
    remote_deactive = sub2api.is_deactive_account(account)
    remote_healthy = sub2api.account_looks_healthy(account)
    return remote_deactive or (_snapshot_has_local_deactive(snapshot) and not remote_healthy)


def _manual_error_delete_unlockable(
    *,
    remote_error: bool,
    effective_deactive: bool,
    mailbox_bound: bool,
    is_duplicate: bool,
) -> bool:
    return remote_error and not effective_deactive and not is_duplicate


def _account_delete_unlock_key(sub2api_account_id: str) -> str:
    return f"{_ACCOUNT_DELETE_UNLOCK_PREFIX}{sub2api_account_id}"


async def _load_delete_unlocked_ids(db: AsyncSession) -> set[str]:
    result = await db.execute(select(AppSetting.key).where(AppSetting.key.like(f"{_ACCOUNT_DELETE_UNLOCK_PREFIX}%")))
    return {
        key[len(_ACCOUNT_DELETE_UNLOCK_PREFIX) :]
        for key in result.scalars().all()
        if isinstance(key, str) and key.startswith(_ACCOUNT_DELETE_UNLOCK_PREFIX)
    }


async def _set_delete_unlocked(db: AsyncSession, sub2api_account_id: str, unlocked: bool) -> None:
    setting_key = _account_delete_unlock_key(sub2api_account_id)
    setting = await db.get(AppSetting, setting_key)
    if unlocked:
        if setting is None:
            setting = AppSetting(key=setting_key)
            db.add(setting)
        setting.value = "true"
        return
    if setting is not None:
        await db.delete(setting)


def _subscription_metadata(account: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(account, dict):
        return {key: None for key in _SUBSCRIPTION_KEYS}

    starts_at = _first_datetime_string(
        account,
        ("credentials", "subscription_starts_at"),
        ("credentials", "subscriptionStartsAt"),
        ("credentials", "subscription_started_at"),
        ("credentials", "subscriptionStartedAt"),
        ("subscription_starts_at",),
        ("subscriptionStartsAt",),
        ("subscription_started_at",),
        ("subscriptionStartedAt",),
        ("entitlement", "starts_at"),
        ("entitlement", "startsAt"),
        ("account", "entitlement", "starts_at"),
        ("account", "entitlement", "startsAt"),
        ("last_active_subscription", "starts_at"),
        ("last_active_subscription", "startsAt"),
        ("last_active_subscription", "started_at"),
        ("last_active_subscription", "startedAt"),
        ("last_active_subscription", "current_period_start"),
        ("last_active_subscription", "currentPeriodStart"),
        ("last_active_subscription", "period_start"),
        ("last_active_subscription", "periodStart"),
        ("last_active_subscription", "start_date"),
        ("last_active_subscription", "startDate"),
        ("extra", "subscription_starts_at"),
    )
    expires_at = _first_datetime_string(
        account,
        ("credentials", "subscription_expires_at"),
        ("credentials", "subscriptionExpiresAt"),
        ("subscription_expires_at",),
        ("subscriptionExpiresAt",),
        ("entitlement", "expires_at"),
        ("entitlement", "expiresAt"),
        ("account", "entitlement", "expires_at"),
        ("account", "entitlement", "expiresAt"),
        ("last_active_subscription", "expires_at"),
        ("last_active_subscription", "expiresAt"),
        ("last_active_subscription", "current_period_end"),
        ("last_active_subscription", "currentPeriodEnd"),
        ("last_active_subscription", "period_end"),
        ("last_active_subscription", "periodEnd"),
        ("last_active_subscription", "end_date"),
        ("last_active_subscription", "endDate"),
        ("extra", "subscription_expires_at"),
    )
    renews_at = _first_datetime_string(
        account,
        ("credentials", "subscription_renews_at"),
        ("credentials", "subscriptionRenewsAt"),
        ("subscription_renews_at",),
        ("subscriptionRenewsAt",),
        ("entitlement", "renews_at"),
        ("entitlement", "renewsAt"),
        ("account", "entitlement", "renews_at"),
        ("account", "entitlement", "renewsAt"),
        ("last_active_subscription", "renews_at"),
        ("last_active_subscription", "renewsAt"),
        ("last_active_subscription", "renew_at"),
        ("last_active_subscription", "renewAt"),
        ("extra", "subscription_renews_at"),
    )
    cancels_at = _first_datetime_string(
        account,
        ("credentials", "subscription_cancels_at"),
        ("credentials", "subscriptionCancelsAt"),
        ("subscription_cancels_at",),
        ("subscriptionCancelsAt",),
        ("entitlement", "cancels_at"),
        ("entitlement", "cancelsAt"),
        ("account", "entitlement", "cancels_at"),
        ("account", "entitlement", "cancelsAt"),
        ("last_active_subscription", "cancels_at"),
        ("last_active_subscription", "cancelsAt"),
        ("last_active_subscription", "canceled_at"),
        ("last_active_subscription", "canceledAt"),
        ("last_active_subscription", "cancel_at"),
        ("last_active_subscription", "cancelAt"),
        ("extra", "subscription_cancels_at"),
    )
    billing_period = _first_string(
        account,
        ("credentials", "subscription_billing_period"),
        ("credentials", "subscriptionBillingPeriod"),
        ("subscription_billing_period",),
        ("subscriptionBillingPeriod",),
        ("entitlement", "billing_period"),
        ("entitlement", "billingPeriod"),
        ("account", "entitlement", "billing_period"),
        ("account", "entitlement", "billingPeriod"),
        ("last_active_subscription", "billing_period"),
        ("last_active_subscription", "billingPeriod"),
        ("last_active_subscription", "interval"),
        ("last_active_subscription", "plan_interval"),
        ("last_active_subscription", "planInterval"),
        ("extra", "subscription_billing_period"),
    )
    if not starts_at and billing_period:
        starts_at = _subscription_starts_at(renews_at or cancels_at or expires_at, billing_period)
    if not expires_at and starts_at and billing_period:
        expires_at = _subscription_expires_at(starts_at, billing_period)

    active_subscription = _first_bool(
        account,
        ("credentials", "has_active_subscription"),
        ("credentials", "hasActiveSubscription"),
        ("has_active_subscription",),
        ("hasActiveSubscription",),
        ("entitlement", "has_active_subscription"),
        ("entitlement", "hasActiveSubscription"),
        ("account", "entitlement", "has_active_subscription"),
        ("account", "entitlement", "hasActiveSubscription"),
        ("last_active_subscription", "has_active_subscription"),
        ("last_active_subscription", "hasActiveSubscription"),
    )
    if active_subscription is None:
        active_subscription = _subscription_status_is_active(
            _first_string(account, ("last_active_subscription", "status"), ("subscription_status",), ("subscriptionStatus",))
        )
    if active_subscription is None and expires_at:
        active_subscription = _datetime_is_future(expires_at)

    return {
        "subscription_starts_at": starts_at,
        "subscription_expires_at": expires_at,
        "subscription_renews_at": renews_at,
        "subscription_cancels_at": cancels_at,
        "subscription_billing_period": billing_period,
        "subscription_plan": _first_string(
            account,
            ("credentials", "plan_type"),
            ("credentials", "planType"),
            ("plan_type",),
            ("planType",),
            ("credentials", "subscription_plan"),
            ("credentials", "subscriptionPlan"),
            ("subscription_plan",),
            ("subscriptionPlan",),
            ("account", "planType"),
            ("account", "plan_type"),
            ("entitlement", "subscription_plan"),
            ("entitlement", "subscriptionPlan"),
            ("account", "entitlement", "subscription_plan"),
            ("account", "entitlement", "subscriptionPlan"),
            ("last_active_subscription", "subscription_plan"),
            ("last_active_subscription", "subscriptionPlan"),
            ("last_active_subscription", "plan_type"),
            ("last_active_subscription", "planType"),
            ("last_active_subscription", "plan"),
            ("last_active_subscription", "name"),
        ),
        "has_active_subscription": active_subscription,
    }


def _merge_subscription_metadata(primary: dict[str, Any], fallback: dict[str, Any]) -> dict[str, Any]:
    return {key: primary.get(key) if primary.get(key) is not None else fallback.get(key) for key in _SUBSCRIPTION_KEYS}


def _subscription_type_metadata(subscription_metadata: dict[str, Any]) -> dict[str, str]:
    subscription_type = _normalize_plan_cohort(subscription_metadata.get("subscription_plan"))
    return {
        "subscription_type": subscription_type,
        "subscription_label": _plan_cohort_label(subscription_type),
    }


def _snapshot_subscription_metadata(snapshot: AccountSnapshot | None) -> dict[str, Any]:
    if snapshot is None:
        return {key: None for key in _SUBSCRIPTION_KEYS}
    return {
        "subscription_starts_at": snapshot.subscription_starts_at,
        "subscription_expires_at": snapshot.subscription_expires_at,
        "subscription_renews_at": snapshot.subscription_renews_at,
        "subscription_cancels_at": snapshot.subscription_cancels_at,
        "subscription_billing_period": snapshot.subscription_billing_period,
        "subscription_plan": snapshot.subscription_plan,
        "has_active_subscription": snapshot.has_active_subscription,
    }


def _sub2api_imported_at(account: dict[str, Any] | None, snapshot: AccountSnapshot | None = None) -> str | None:
    paths = (
        ("created_at",),
        ("createdAt",),
        ("created",),
        ("created_time",),
        ("createdTime",),
        ("create_time",),
        ("createTime",),
        ("imported_at",),
        ("importedAt",),
        ("added_at",),
        ("addedAt",),
        ("inserted_at",),
        ("insertedAt",),
        ("metadata", "created_at"),
        ("metadata", "createdAt"),
        ("account", "created_at"),
        ("account", "createdAt"),
    )
    return _first_datetime_string(account, *paths) or _first_datetime_string(snapshot.raw if snapshot else None, *paths)


def _path_get(data: Any, path: tuple[str, ...]) -> Any:
    current = data
    for part in path:
        if not isinstance(current, dict):
            return None
        current = current.get(part)
    return current


def _first_string(data: Any, *paths: tuple[str, ...]) -> str | None:
    for path in paths:
        text = _string_or_none(_path_get(data, path))
        if text is not None:
            return text
    return None


def _first_datetime_string(data: Any, *paths: tuple[str, ...]) -> str | None:
    for path in paths:
        text = _datetime_string_or_none(_path_get(data, path))
        if text is not None:
            return text
    return None


def _first_bool(data: Any, *paths: tuple[str, ...]) -> bool | None:
    for path in paths:
        value = _path_get(data, path)
        parsed = _bool_or_none(value)
        if parsed is not None:
            return parsed
    return None


def _string_or_none(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _bool_or_none(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        text = value.strip().lower()
        if text in {"1", "true", "yes", "active"}:
            return True
        if text in {"0", "false", "no", "inactive"}:
            return False
    return None


def _subscription_status_is_active(value: str | None) -> bool | None:
    if not value:
        return None
    text = value.strip().lower()
    if text in {"active", "trialing", "paid", "valid"}:
        return True
    if text in {"inactive", "canceled", "cancelled", "expired", "past_due", "unpaid"}:
        return False
    return None


def _datetime_string_or_none(value: Any) -> str | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        if number > 10_000_000_000:
            number = number / 1000
        return _format_subscription_datetime(datetime.fromtimestamp(number, timezone.utc))
    text = str(value).strip()
    if not text:
        return None
    parsed = _parse_subscription_datetime(text)
    return _format_subscription_datetime(parsed) if parsed else text


def _parse_subscription_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    text = value.strip()
    if not text:
        return None
    if re.fullmatch(r"\d+(?:\.\d+)?", text):
        return _parse_subscription_datetime(_datetime_string_or_none(float(text)))
    if re.fullmatch(r"\d{4}-\d{1,2}-\d{1,2}", text):
        text = f"{text}T00:00:00"
    else:
        text = text.replace(" ", "T", 1)
    text = text.replace("Z", "+00:00")
    match = re.search(r"([+-]\d{2})(\d{2})$", text)
    if match:
        text = f"{text[:-5]}{match.group(1)}:{match.group(2)}"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _format_subscription_datetime(value: datetime) -> str:
    value = value.replace(microsecond=0)
    if value.tzinfo is not None:
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    return value.isoformat()


def _datetime_is_future(value: str) -> bool | None:
    parsed = _parse_subscription_datetime(value)
    if parsed is None:
        return None
    if parsed.tzinfo is not None:
        return parsed.astimezone(timezone.utc) > datetime.now(timezone.utc)
    return parsed > datetime.utcnow()


def _shift_months(value: datetime, months: int) -> datetime:
    month_index = value.month - 1 + months
    year = value.year + month_index // 12
    month = month_index % 12 + 1
    day = min(value.day, calendar.monthrange(year, month)[1])
    return value.replace(year=year, month=month, day=day)


def _subscription_starts_at(end_at: str | None, billing_period: str | None) -> str | None:
    end = _parse_subscription_datetime(end_at)
    if end is None or not billing_period:
        return None
    period = billing_period.strip().lower()
    if period in {"monthly", "month"}:
        return _format_subscription_datetime(_shift_months(end, -1))
    if period in {"yearly", "annual", "annually", "year"}:
        return _format_subscription_datetime(_shift_months(end, -12))
    if period in {"weekly", "week"}:
        return _format_subscription_datetime(end - timedelta(days=7))
    return None


def _subscription_expires_at(start_at: str | None, billing_period: str | None) -> str | None:
    start = _parse_subscription_datetime(start_at)
    if start is None or not billing_period:
        return None
    period = billing_period.strip().lower()
    if period in {"monthly", "month"}:
        return _format_subscription_datetime(_shift_months(start, 1))
    if period in {"yearly", "annual", "annually", "year"}:
        return _format_subscription_datetime(_shift_months(start, 12))
    if period in {"weekly", "week"}:
        return _format_subscription_datetime(start + timedelta(days=7))
    return None


@router.get("", response_model=list[AccountOut])
async def list_accounts(
    _: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> list[AccountOut]:
    result = await db.execute(select(AccountSnapshot).order_by(desc(AccountSnapshot.updated_at)))
    snapshots = list(result.scalars().all())
    snapshots_by_email = {snapshot.email.lower(): snapshot for snapshot in snapshots}
    mailbox_result = await db.execute(
        select(MailboxCredential.gpt_email).where(MailboxCredential.disabled.is_(False))
    )
    bound_emails = {email.lower() for email in mailbox_result.scalars().all()}
    phone_result = await db.execute(
        select(
            PhoneAccountBinding.account_email,
            PhoneNumber.phone_number,
            PhoneNumber.sms_url,
            PhoneNumber.sms_cdk,
            PhoneNumber.sms_recharge_url,
        )
        .join(PhoneNumber, PhoneNumber.id == PhoneAccountBinding.phone_id)
    )
    phone_by_email = {
        str(account_email).lower(): {
            "phone_number": str(phone_number),
            "phone_sms_url": str(sms_url),
            "phone_sms_cdk": str(sms_cdk) if sms_cdk not in (None, "") else None,
            "phone_sms_recharge_url": str(sms_recharge_url) if sms_recharge_url not in (None, "") else None,
        }
        for account_email, phone_number, sms_url, sms_cdk, sms_recharge_url in phone_result.all()
    }

    sub2api = Sub2ApiClient()
    try:
        remote_accounts = [account for account in await sub2api.list_accounts() if sub2api.is_gpt_account(account)]
    except Sub2ApiRequestError:
        remote_accounts = []

    metadata = sub2api.account_duplicate_metadata(remote_accounts)
    duplicate_cleanup_ids = _deletable_duplicate_account_ids(remote_accounts, sub2api, metadata)
    delete_unlocked_ids = await _load_delete_unlocked_ids(db)
    sample_thresholds = await get_runtime_config_service().get_usage_limit_sample_thresholds()
    rows: list[AccountOut] = []
    seen_remote_emails: set[str] = set()
    now = utcnow()
    for index, account in enumerate(remote_accounts):
        email = sub2api.account_email(account)
        if not email:
            continue
        normalized = email.lower()
        seen_remote_emails.add(normalized)
        snapshot = snapshots_by_email.get(normalized)
        item = metadata.get(index) or {}
        is_duplicate = bool(item.get("is_duplicate", False))
        duplicate_primary = bool(item.get("duplicate_primary", True))
        duplicate_group_size = int(item.get("duplicate_group_size", 1))
        duplicate_rank = int(item.get("duplicate_rank", 0))
        account_id = sub2api.account_id(account)
        rate_limited_windows = account_rate_limited_windows(account, sample_thresholds=sample_thresholds)
        remote_healthy = sub2api.account_looks_healthy(account)
        effective_deactive = _effective_deactive(sub2api, account, snapshot)
        remote_deactive = sub2api.is_deactive_account(account)
        remote_error = sub2api.is_error_account(account) or effective_deactive
        mailbox_bound = normalized in bound_emails
        phone_data = phone_by_email.get(normalized) or {}
        delete_unlockable = _manual_error_delete_unlockable(
            remote_error=remote_error,
            effective_deactive=effective_deactive,
            mailbox_bound=mailbox_bound,
            is_duplicate=is_duplicate,
        )
        delete_unlocked = bool(account_id and account_id in delete_unlocked_ids)
        last_error = None if remote_healthy else snapshot.last_error if snapshot else None
        subscription_metadata = _merge_subscription_metadata(
            _snapshot_subscription_metadata(snapshot),
            _merge_subscription_metadata(_subscription_metadata(account), _subscription_metadata(snapshot.raw if snapshot else None)),
        )
        rows.append(
            AccountOut(
                id=snapshot.id if snapshot else 0,
                email=normalized,
                sub2api_account_id=account_id,
                sub2api_imported_at=_sub2api_imported_at(account, snapshot),
                platform=sub2api.account_platform(account) or (snapshot.platform if snapshot else None),
                account_type=sub2api.account_type(account) or (snapshot.account_type if snapshot else None),
                status=sub2api.account_status(account) or (snapshot.status if snapshot else None),
                schedulable=sub2api.account_schedulable(account),
                usage_estimate_enabled=snapshot.usage_estimate_enabled if snapshot else not sub2api.is_deactive_account(account),
                mailbox_bound=mailbox_bound,
                deactive=effective_deactive,
                refreshing=snapshot.refreshing if snapshot else False,
                auto_refresh_locked=snapshot.auto_refresh_locked if snapshot else False,
                last_error=last_error,
                last_seen_at=snapshot.last_seen_at if snapshot else now,
                updated_at=snapshot.updated_at if snapshot else now,
                is_duplicate=is_duplicate,
                duplicate_group_size=duplicate_group_size,
                duplicate_rank=duplicate_rank,
                duplicate_primary=duplicate_primary,
                duplicate_primary_account_id=item.get("duplicate_primary_account_id"),
                remote_error=remote_error,
                can_delete_remote=effective_deactive
                or (account_id in duplicate_cleanup_ids if account_id else False),
                delete_unlockable=delete_unlockable,
                delete_unlocked=delete_unlocked,
                rate_limited=bool(rate_limited_windows),
                rate_limited_windows=rate_limited_windows,
                phone_number=phone_data.get("phone_number"),
                phone_sms_url=phone_data.get("phone_sms_url"),
                phone_sms_cdk=phone_data.get("phone_sms_cdk"),
                phone_sms_recharge_url=phone_data.get("phone_sms_recharge_url"),
                **_subscription_type_metadata(subscription_metadata),
                **subscription_metadata,
            )
        )

    for snapshot in snapshots:
        normalized = snapshot.email.lower()
        if normalized in seen_remote_emails:
            continue
        rate_limited_windows = account_rate_limited_windows(snapshot.raw or {}, sample_thresholds=sample_thresholds)
        account_id = snapshot.sub2api_account_id
        mailbox_bound = normalized in bound_emails
        phone_data = phone_by_email.get(normalized) or {}
        remote_error = snapshot.deactive or bool(snapshot.last_error) or sub2api.is_error_account(
            {
                "status": snapshot.status,
                "schedulable": snapshot.schedulable,
            }
        )
        delete_unlockable = _manual_error_delete_unlockable(
            remote_error=remote_error,
            effective_deactive=snapshot.deactive,
            mailbox_bound=mailbox_bound,
            is_duplicate=False,
        )
        delete_unlocked = bool(account_id and account_id in delete_unlocked_ids)
        subscription_metadata = _merge_subscription_metadata(
            _snapshot_subscription_metadata(snapshot), _subscription_metadata(snapshot.raw)
        )
        rows.append(
            AccountOut(
                id=snapshot.id,
                email=normalized,
                sub2api_account_id=snapshot.sub2api_account_id,
                sub2api_imported_at=_sub2api_imported_at(None, snapshot),
                platform=snapshot.platform,
                account_type=snapshot.account_type,
                status=snapshot.status,
                schedulable=snapshot.schedulable,
                usage_estimate_enabled=snapshot.usage_estimate_enabled,
                mailbox_bound=mailbox_bound,
                deactive=snapshot.deactive,
                refreshing=snapshot.refreshing,
                auto_refresh_locked=snapshot.auto_refresh_locked,
                last_error=snapshot.last_error,
                last_seen_at=snapshot.last_seen_at,
                updated_at=snapshot.updated_at,
                remote_error=remote_error,
                can_delete_remote=bool(account_id and snapshot.deactive),
                delete_unlockable=delete_unlockable,
                delete_unlocked=delete_unlocked,
                rate_limited=bool(rate_limited_windows),
                rate_limited_windows=rate_limited_windows,
                phone_number=phone_data.get("phone_number"),
                phone_sms_url=phone_data.get("phone_sms_url"),
                phone_sms_cdk=phone_data.get("phone_sms_cdk"),
                phone_sms_recharge_url=phone_data.get("phone_sms_recharge_url"),
                **_subscription_type_metadata(subscription_metadata),
                **subscription_metadata,
            )
        )

    return sorted(rows, key=lambda item: (item.email, item.duplicate_rank, item.sub2api_account_id or ""))


@router.get("/usage-estimate", response_model=UsageEstimateOut)
async def usage_estimate(
    refresh: bool = Query(default=True),
    _: dict = Depends(require_admin),
) -> UsageEstimateOut:
    try:
        cached_usage = None if refresh else get_usage_refresh_service().latest_usage_snapshot()
        return await build_usage_estimate(refresh=refresh, usage_by_account_id=cached_usage)
    except Sub2ApiRequestError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc


@router.get("/usage-limit-samples", response_model=UsageLimitSamplesOut)
async def usage_limit_samples(
    _: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> UsageLimitSamplesOut:
    runtime_config = get_runtime_config_service()
    sample_thresholds = await runtime_config.get_usage_limit_sample_thresholds()
    default_ranges = await runtime_config.get_usage_limit_default_ranges()
    five_hour_threshold = sample_thresholds.get("five_hour") or LIMIT_SAMPLE_FULL_PERCENT
    seven_day_threshold = sample_thresholds.get("seven_day") or LIMIT_SAMPLE_FULL_PERCENT
    windows: list[UsageLimitWindowSamplesOut] = []
    for window_key, metadata in SAMPLE_WINDOWS.items():
        result = await db.execute(
            select(UsageLimitSample)
            .where(UsageLimitSample.window_key == window_key)
            .order_by(UsageLimitSample.plan_cohort, UsageLimitSample.observed_limit, UsageLimitSample.updated_at)
        )
        rows = list(result.scalars().all())
        rows_by_cohort: dict[str, list[UsageLimitSample]] = {}
        for row in rows:
            if not _usage_limit_sample_allowed(
                row.window_key, row.plan_cohort, row.observed_limit, default_ranges
            ):
                continue
            cohort = _normalize_plan_cohort(row.plan_cohort)
            rows_by_cohort.setdefault(cohort, []).append(row)

        cohort_names = set(_default_sample_plan_cohorts(window_key, default_ranges))
        cohort_names.update(rows_by_cohort)
        for cohort in sorted(cohort_names, key=_plan_cohort_sort_key):
            cohort_rows = rows_by_cohort.get(cohort, [])
            calibration = _limit_calibration(
                window_key, [row.observed_limit for row in cohort_rows], cohort, default_ranges
            )
            default_lower, default_upper = _default_limit_bounds(window_key, cohort, default_ranges)
            windows.append(
                UsageLimitWindowSamplesOut(
                    window_key=window_key,
                    label=metadata["label"],
                    plan_cohort=cohort,
                    plan_label=_plan_cohort_label(cohort),
                    subscription_type=cohort,
                    subscription_label=_plan_cohort_label(cohort),
                    calibration=UsageLimitCalibrationOut(
                        source=str(calibration["source"]),
                        sample_count=int(calibration["sample_count"]),
                        lower=float(calibration["lower"]),
                        upper=float(calibration["upper"]),
                        mean=calibration.get("mean"),
                        sigma=calibration.get("sigma"),
                        default_lower=default_lower,
                        default_upper=default_upper,
                    ),
                    samples=[
                        UsageLimitSampleOut(
                            id=row.id,
                            account_key=row.account_key,
                            email=row.email,
                            sub2api_account_id=row.sub2api_account_id,
                            plan_cohort=_normalize_plan_cohort(row.plan_cohort),
                            subscription_type=_normalize_plan_cohort(row.plan_cohort),
                            subscription_label=_plan_cohort_label(row.plan_cohort),
                            reset_key=row.reset_key,
                            reset_at=row.reset_at,
                            observed_limit=row.observed_limit,
                            raw_spent=row.raw_spent,
                            used_percent=row.used_percent,
                            created_at=row.created_at,
                            updated_at=row.updated_at,
                        )
                        for row in cohort_rows
                    ],
                )
            )
    return UsageLimitSamplesOut(
        updated_at=utcnow(),
        target_sample_count=LIMIT_SAMPLE_TARGET,
        full_percent_threshold=LIMIT_SAMPLE_FULL_PERCENT,
        five_hour_threshold_percent=five_hour_threshold,
        seven_day_threshold_percent=seven_day_threshold,
        windows=windows,
    )


@router.patch("/{account_id}/usage-estimate", response_model=MessageResponse)
async def update_usage_estimate_preference(
    account_id: int,
    payload: UsageEstimatePreferenceUpdate,
    _: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> MessageResponse:
    snapshot = await db.get(AccountSnapshot, account_id)
    if snapshot is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Account snapshot not found.")
    snapshot.usage_estimate_enabled = payload.enabled
    await db.commit()
    state = "enabled" if payload.enabled else "disabled"
    return MessageResponse(message=f"Usage estimate {state} for {snapshot.email}.")


@router.post("/sync", response_model=Sub2ApiSyncResult)
async def sync_accounts(
    _: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> Sub2ApiSyncResult:
    runtime_config = get_runtime_config_service()
    try:
        result = await get_monitor_service().sync_once(reason="manual")
        subscription_protocol_limit = await runtime_config.get_subscription_refresh_batch_size()
        subscription_max_concurrency = await runtime_config.get_subscription_refresh_max_concurrency()
        subscription_result = await refresh_subscriptions(
            protocol_limit=subscription_protocol_limit,
            max_concurrency=subscription_max_concurrency,
        )
        usage_result = await get_usage_refresh_service().refresh_all(reason="sync")
    except Sub2ApiRequestError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc
    sync_message = result.message
    result.message = f"{sync_message} {subscription_result['message']} {usage_result.message}"
    await record_event(
        db,
        "manual_sync",
        sync_message,
        details={
            "reason": "manual",
            "total_seen": result.total_seen,
            "error_seen": result.error_seen,
            "queued": result.queued,
            "duplicate_accounts_ignored": result.duplicate_accounts_ignored,
            "deleted_accounts": result.deleted_accounts,
            "deleted_mailboxes": result.deleted_mailboxes,
            "subscription_total": subscription_result["total"],
            "subscription_refreshed": subscription_result["refreshed"],
            "subscription_skipped": subscription_result["skipped"],
            "subscription_no_subscription_fields": subscription_result["no_subscription_fields"],
            "subscription_protocol_attempts": subscription_result.get("protocol_attempts", 0),
            "subscription_protocol_limit": subscription_protocol_limit,
            "subscription_max_concurrency": subscription_max_concurrency,
            "subscription_failed": subscription_result["failed"],
            "usage_total": usage_result.total,
            "usage_refreshed": usage_result.refreshed,
            "usage_skipped": usage_result.skipped,
            "usage_failed": usage_result.failed,
            "usage_max_concurrency": usage_result.max_concurrency,
        },
    )
    return result


@router.delete("/remote/{sub2api_account_id}", response_model=MessageResponse)
async def delete_remote_account(
    sub2api_account_id: str,
    _: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> MessageResponse:
    sub2api = Sub2ApiClient()
    try:
        accounts = [account for account in await sub2api.list_accounts() if sub2api.is_gpt_account(account)]
    except Sub2ApiRequestError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc

    target_index = None
    target_account = None
    for index, account in enumerate(accounts):
        if sub2api.account_id(account) == sub2api_account_id:
            target_index = index
            target_account = account
            break
    if target_account is None or target_index is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="sub2api account not found.")

    metadata = sub2api.account_duplicate_metadata(accounts)
    duplicate_cleanup_ids = _deletable_duplicate_account_ids(accounts, sub2api, metadata)
    item = metadata.get(target_index) or {}
    is_duplicate = bool(item.get("is_duplicate", False))
    duplicate_primary = bool(item.get("duplicate_primary", True))
    duplicate_group_size = int(item.get("duplicate_group_size", 1))
    duplicate_rank = int(item.get("duplicate_rank", 0))
    account_id = sub2api.account_id(target_account)
    is_remote_deactive = sub2api.is_deactive_account(target_account)
    is_remote_error = sub2api.is_error_account(target_account) or is_remote_deactive
    can_delete_duplicate_cleanup = account_id in duplicate_cleanup_ids if account_id else False
    email = sub2api.account_email(target_account)
    snapshot = await db.scalar(select(AccountSnapshot).where(AccountSnapshot.email == email.lower())) if email else None
    is_local_deactive = _snapshot_has_local_deactive(snapshot)
    mailbox_bound = False
    if email:
        mailbox_bound = bool(
            await db.scalar(
                select(MailboxCredential.id).where(
                    MailboxCredential.gpt_email == email.lower(),
                    MailboxCredential.disabled.is_(False),
                )
            )
        )
    delete_unlockable = _manual_error_delete_unlockable(
        remote_error=is_remote_error,
        effective_deactive=is_remote_deactive or is_local_deactive,
        mailbox_bound=mailbox_bound,
        is_duplicate=is_duplicate,
    )
    can_delete_manual_error = delete_unlockable
    if not is_remote_deactive and not is_local_deactive and not can_delete_duplicate_cleanup and not can_delete_manual_error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Only deactivated accounts, duplicate cleanup candidates, or protected error accounts can be deleted here.",
        )

    await record_event(
        db,
        "sub2api_account_delete_requested",
        "Requested deletion of one sub2api account.",
        email.lower() if email else None,
        {
            "sub2api_account_id": sub2api_account_id,
            "is_duplicate": is_duplicate,
            "duplicate_primary": duplicate_primary,
            "remote_error": is_remote_error,
            "delete_mode": "session_unlock" if delete_unlockable else "direct",
        },
    )

    try:
        deleted = await sub2api.delete_account(sub2api_account_id)
    except Sub2ApiRequestError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc
    if not deleted:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="sub2api account not found.")

    await _set_delete_unlocked(db, sub2api_account_id, False)

    if email:
        remaining_same_email = [
            account
            for account in accounts
            if sub2api.account_id(account) != sub2api_account_id and sub2api.account_email(account) == email
        ]
        if snapshot and remaining_same_email:
            deduped, _ = sub2api.dedupe_accounts_by_email(remaining_same_email)
            replacement = deduped[0] if deduped else remaining_same_email[0]
            snapshot.sub2api_account_id = sub2api.account_id(replacement)
            snapshot.platform = sub2api.account_platform(replacement)
            snapshot.account_type = sub2api.account_type(replacement)
            snapshot.status = sub2api.account_status(replacement)
            snapshot.schedulable = sub2api.account_schedulable(replacement)
            snapshot.deactive = _effective_deactive(sub2api, replacement, snapshot)
            snapshot.raw = None
        elif snapshot:
            await db.delete(snapshot)

    normalized_email = email.lower() if email else None
    for source in ("sync", "refresh", "usage_refresh", "subscription_refresh"):
        await clear_account_exception(
            db,
            source=source,
            email=normalized_email,
            sub2api_account_id=sub2api_account_id,
            commit=False,
        )

    await record_event(
        db,
        "sub2api_account_deleted",
        "Deleted one sub2api account from the account table.",
        email.lower() if email else None,
        {
            "sub2api_account_id": sub2api_account_id,
            "is_duplicate": is_duplicate,
            "duplicate_primary": duplicate_primary,
            "remote_error": is_remote_error,
            "delete_mode": "session_unlock" if delete_unlockable else "direct",
        },
    )
    return MessageResponse(message=f"Deleted sub2api account {sub2api_account_id}.")


@router.put("/remote/{sub2api_account_id}/delete-lock", response_model=MessageResponse)
async def update_remote_account_delete_lock(
    sub2api_account_id: str,
    payload: AccountDeleteUnlockUpdate,
    _: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> MessageResponse:
    sub2api = Sub2ApiClient()
    try:
        accounts = [account for account in await sub2api.list_accounts() if sub2api.is_gpt_account(account)]
    except Sub2ApiRequestError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc

    target_index = None
    target_account = None
    for index, account in enumerate(accounts):
        if sub2api.account_id(account) == sub2api_account_id:
            target_index = index
            target_account = account
            break
    if target_account is None or target_index is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="sub2api account not found.")

    metadata = sub2api.account_duplicate_metadata(accounts)
    item = metadata.get(target_index) or {}
    is_duplicate = bool(item.get("is_duplicate", False))
    email = sub2api.account_email(target_account)
    snapshot = await db.scalar(select(AccountSnapshot).where(AccountSnapshot.email == email.lower())) if email else None
    mailbox_bound = False
    if email:
        mailbox_bound = bool(
            await db.scalar(
                select(MailboxCredential.id).where(
                    MailboxCredential.gpt_email == email.lower(),
                    MailboxCredential.disabled.is_(False),
                )
            )
        )
    effective_deactive = _effective_deactive(sub2api, target_account, snapshot)
    delete_unlockable = _manual_error_delete_unlockable(
        remote_error=sub2api.is_error_account(target_account) or effective_deactive,
        effective_deactive=effective_deactive,
        mailbox_bound=mailbox_bound,
        is_duplicate=is_duplicate,
    )
    if payload.unlocked and not delete_unlockable:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="This account does not support manual delete unlock.")

    await _set_delete_unlocked(db, sub2api_account_id, payload.unlocked)
    message = "Manual delete unlock enabled for this account." if payload.unlocked else "Delete lock restored for this account."
    await record_event(
        db,
        "account_delete_lock_updated",
        message,
        email.lower() if email else None,
        {"sub2api_account_id": sub2api_account_id, "unlocked": payload.unlocked},
    )
    return MessageResponse(message=message)


@router.put("/{account_id}/refresh-lock", response_model=MessageResponse)
async def update_account_refresh_lock(
    account_id: int,
    payload: AccountRefreshUnlockUpdate,
    _: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> MessageResponse:
    snapshot = await db.get(AccountSnapshot, account_id)
    if snapshot is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Account snapshot not found.")

    snapshot.auto_refresh_locked = not payload.unlocked
    await db.commit()
    message = "Automatic refresh unlocked for this account." if payload.unlocked else "Automatic refresh locked for this account."
    await record_event(
        db,
        "account_refresh_lock_updated",
        message,
        snapshot.email.lower(),
        {"account_id": account_id, "unlocked": payload.unlocked},
    )
    return MessageResponse(message=message)


@router.post("/usage-refresh", response_model=UsageRefreshResult)
async def refresh_usage_windows(
    _: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> UsageRefreshResult:
    try:
        result = await get_usage_refresh_service().refresh_all(reason="manual")
    except Sub2ApiRequestError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc
    message = result.message
    await record_event(
        db,
        "usage_statistics_refresh",
        message,
        details={
            "usage_total": result.total,
            "usage_refreshed": result.refreshed,
            "usage_skipped": result.skipped,
            "usage_failed": result.failed,
            "usage_max_concurrency": result.max_concurrency,
        },
    )
    return UsageRefreshResult(
        message=message,
        total=result.total,
        refreshed=result.refreshed,
        skipped=result.skipped,
        failed=result.failed,
        failures=[
            UsageRefreshFailureOut(email=failure.email, account_id=failure.account_id, error=failure.error)
            for failure in result.failures
        ],
    )


@router.post("/subscription-refresh", response_model=SubscriptionRefreshResult)
async def refresh_account_subscriptions(
    protocol_limit: int | None = Query(default=None, ge=1, le=100),
    max_concurrency: int | None = Query(default=None, ge=1, le=20),
    _: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> SubscriptionRefreshResult:
    runtime_config = get_runtime_config_service()
    resolved_protocol_limit = protocol_limit or await runtime_config.get_subscription_refresh_batch_size()
    resolved_max_concurrency = max_concurrency or await runtime_config.get_subscription_refresh_max_concurrency()
    try:
        result = await refresh_subscriptions(
            protocol_limit=resolved_protocol_limit,
            max_concurrency=resolved_max_concurrency,
        )
    except Sub2ApiRequestError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc
    await record_event(
        db,
        "subscription_refresh",
        result["message"],
        details={
            "total": result["total"],
            "refreshed": result["refreshed"],
            "skipped": result["skipped"],
            "no_subscription_fields": result["no_subscription_fields"],
            "protocol_attempts": result.get("protocol_attempts", 0),
            "protocol_limit": resolved_protocol_limit,
            "max_concurrency": resolved_max_concurrency,
            "failed": result["failed"],
        },
    )
    return SubscriptionRefreshResult(
        message=result["message"],
        total=result["total"],
        refreshed=result["refreshed"],
        skipped=result["skipped"],
        no_subscription_fields=result["no_subscription_fields"],
        protocol_attempts=result.get("protocol_attempts", 0),
        failed=result["failed"],
        failures=[
            SubscriptionRefreshFailureOut(
                email=failure.get("email"),
                account_id=failure.get("account_id"),
                error=str(failure.get("error") or ""),
            )
            for failure in result["failures"]
            if isinstance(failure, dict)
        ],
    )


@router.post("/refresh", response_model=RefreshJobOut)
async def refresh_account(
    payload: ManualRefreshRequest,
    _: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> RefreshJobOut:
    if not await get_runtime_config_service().get_recovery_enabled():
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Recovery is disabled in settings.")
    try:
        job_id = await get_refresh_service().enqueue_by_email(str(payload.email), reason="manual")
    except Sub2ApiRequestError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc
    if job_id is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Could not find this account in sub2api or it is already refreshing.",
        )
    job = await db.get(RefreshJob, job_id)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Refresh job was not created.")
    return job


@router.delete("/deactivated", response_model=DeactivatedCleanupResult)
async def delete_deactivated_accounts(
    _: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> DeactivatedCleanupResult:
    sub2api = Sub2ApiClient()
    try:
        remote_accounts = [account for account in await sub2api.list_accounts() if sub2api.is_gpt_account(account)]
    except Sub2ApiRequestError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc

    snapshot_result = await db.execute(select(AccountSnapshot))
    snapshots = list(snapshot_result.scalars().all())
    snapshots_by_email = {snapshot.email.lower(): snapshot for snapshot in snapshots}
    local_deactive = [snapshot for snapshot in snapshots if _snapshot_has_local_deactive(snapshot)]
    local_deactive_emails = {snapshot.email.lower() for snapshot in local_deactive}
    metadata = sub2api.account_duplicate_metadata(remote_accounts)
    duplicate_cleanup_ids = _deletable_duplicate_account_ids(remote_accounts, sub2api, metadata)
    remote_cleanup = [
        account
        for account in remote_accounts
        if sub2api.is_deactive_account(account)
        or ((sub2api.account_email(account) or "").lower() in local_deactive_emails)
        or (sub2api.account_id(account) in duplicate_cleanup_ids)
    ]

    if not local_deactive and not remote_cleanup:
        return DeactivatedCleanupResult(
            message="No deactivated or duplicate accounts to delete.",
            deleted_accounts=0,
            deleted_mailboxes=0,
            deleted_sub2api_accounts=0,
            deleted_no_email_sub2api_accounts=0,
            failed_sub2api_accounts=[],
        )

    failed_remote: list[str] = []
    deleted_remote_ids: set[str] = set()
    affected_emails: set[str] = {snapshot.email.lower() for snapshot in local_deactive}
    deleted_remote = 0
    deleted_no_email_remote = 0

    for account in remote_cleanup:
        account_id = sub2api.account_id(account)
        email = sub2api.account_email(account)
        if email:
            affected_emails.add(email.lower())
        if not account_id:
            continue
        try:
            if await sub2api.delete_account(account_id):
                deleted_remote += 1
                deleted_remote_ids.add(account_id)
                if not email:
                    deleted_no_email_remote += 1
        except Sub2ApiRequestError as exc:
            failed_remote.append(f"{email or account_id}: {exc}")

    deleted_mailboxes = 0
    deleted_accounts = 0
    mailbox_delete_emails: list[str] = []
    for email in sorted(affected_emails):
        snapshot = snapshots_by_email.get(email)
        remaining = [
            account
            for account in remote_accounts
            if sub2api.account_email(account) == email and (sub2api.account_id(account) or "") not in deleted_remote_ids
        ]
        if remaining:
            if snapshot:
                deduped, _ = sub2api.dedupe_accounts_by_email(remaining)
                replacement = deduped[0] if deduped else remaining[0]
                snapshot.sub2api_account_id = sub2api.account_id(replacement)
                snapshot.platform = sub2api.account_platform(replacement)
                snapshot.account_type = sub2api.account_type(replacement)
                snapshot.status = sub2api.account_status(replacement)
                snapshot.schedulable = sub2api.account_schedulable(replacement)
                snapshot.deactive = _effective_deactive(sub2api, replacement, snapshot)
                snapshot.raw = None
            continue
        if snapshot:
            await db.delete(snapshot)
            deleted_accounts += 1
            mailbox_delete_emails.append(email)

    if mailbox_delete_emails:
        mailbox_result = await db.execute(delete(MailboxCredential).where(MailboxCredential.gpt_email.in_(mailbox_delete_emails)))
        deleted_mailboxes = mailbox_result.rowcount or 0

    for source in ("sync", "refresh", "usage_refresh", "subscription_refresh"):
        for email in affected_emails:
            await clear_account_exception(db, source=source, email=email, commit=False)
        for account_id in deleted_remote_ids:
            await clear_account_exception(db, source=source, sub2api_account_id=account_id, commit=False)

    await record_event(
        db,
        "deactivated_accounts_deleted",
        f"Deleted {deleted_accounts} deactivated/duplicate account(s), {deleted_mailboxes} mailbox credential(s), and {deleted_remote} sub2api account(s).",
        details={
            "deleted_accounts": deleted_accounts,
            "deleted_mailboxes": deleted_mailboxes,
            "deleted_sub2api_accounts": deleted_remote,
            "deleted_no_email_sub2api_accounts": deleted_no_email_remote,
            "failed_sub2api_accounts": failed_remote,
        },
    )
    remote_summary = f"and {deleted_remote} sub2api account(s)"
    if deleted_no_email_remote:
        remote_summary += f" ({deleted_no_email_remote} without email)"
    message = (
        f"Deleted {deleted_accounts} deactivated/duplicate account(s), {deleted_mailboxes} mailbox credential(s), "
        f"{remote_summary}."
    )
    if failed_remote:
        message += f" {len(failed_remote)} sub2api account(s) could not be deleted."
    return DeactivatedCleanupResult(
        message=message,
        deleted_accounts=deleted_accounts,
        deleted_mailboxes=deleted_mailboxes,
        deleted_sub2api_accounts=deleted_remote,
        deleted_no_email_sub2api_accounts=deleted_no_email_remote,
        failed_sub2api_accounts=failed_remote,
    )


@router.post("/delete-selected", response_model=DeactivatedCleanupResult)
async def delete_selected_accounts(
    payload: SelectedAccountDeleteRequest,
    _: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> DeactivatedCleanupResult:
    selected_remote_ids = {
        str(item.sub2api_account_id).strip()
        for item in payload.accounts
        if item.sub2api_account_id and str(item.sub2api_account_id).strip()
    }
    selected_snapshot_ids = {int(item.snapshot_id) for item in payload.accounts if item.snapshot_id}
    if not selected_remote_ids and not selected_snapshot_ids:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No accounts selected.")

    sub2api = Sub2ApiClient()
    try:
        remote_accounts = [account for account in await sub2api.list_accounts() if sub2api.is_gpt_account(account)]
    except Sub2ApiRequestError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc

    remote_by_id = {
        account_id: account
        for account in remote_accounts
        if (account_id := sub2api.account_id(account))
    }
    snapshot_result = await db.execute(select(AccountSnapshot))
    snapshots = list(snapshot_result.scalars().all())
    snapshots_by_email = {snapshot.email.lower(): snapshot for snapshot in snapshots}
    selected_snapshots = [snapshot for snapshot in snapshots if snapshot.id in selected_snapshot_ids]
    selected_snapshot_account_ids = {
        str(snapshot.sub2api_account_id).strip()
        for snapshot in selected_snapshots
        if snapshot.sub2api_account_id and str(snapshot.sub2api_account_id).strip()
    }

    failed_remote: list[str] = []
    deleted_remote_ids: set[str] = set()
    affected_emails = {snapshot.email.lower() for snapshot in selected_snapshots}
    deleted_remote = 0
    deleted_no_email_remote = 0

    for account_id in sorted(selected_remote_ids):
        account = remote_by_id.get(account_id)
        if account is None:
            if account_id not in selected_snapshot_account_ids:
                failed_remote.append(f"{account_id}: not found")
            continue

        email = sub2api.account_email(account)
        if email:
            affected_emails.add(email.lower())
        try:
            if await sub2api.delete_account(account_id):
                deleted_remote += 1
                deleted_remote_ids.add(account_id)
                if not email:
                    deleted_no_email_remote += 1
            elif account_id not in selected_snapshot_account_ids:
                failed_remote.append(f"{email or account_id}: not found")
        except Sub2ApiRequestError as exc:
            failed_remote.append(f"{email or account_id}: {exc}")

    deleted_mailboxes = 0
    deleted_accounts = 0
    mailbox_delete_emails: list[str] = []
    for email in sorted(affected_emails):
        snapshot = snapshots_by_email.get(email)
        remaining = [
            account
            for account in remote_accounts
            if sub2api.account_email(account) == email and (sub2api.account_id(account) or "") not in deleted_remote_ids
        ]
        if remaining:
            if snapshot:
                deduped, _ = sub2api.dedupe_accounts_by_email(remaining)
                replacement = deduped[0] if deduped else remaining[0]
                snapshot.sub2api_account_id = sub2api.account_id(replacement)
                snapshot.platform = sub2api.account_platform(replacement)
                snapshot.account_type = sub2api.account_type(replacement)
                snapshot.status = sub2api.account_status(replacement)
                snapshot.schedulable = sub2api.account_schedulable(replacement)
                snapshot.deactive = _effective_deactive(sub2api, replacement, snapshot)
                snapshot.raw = None
            continue
        if snapshot:
            await db.delete(snapshot)
            deleted_accounts += 1
            mailbox_delete_emails.append(email)

    if mailbox_delete_emails:
        mailbox_result = await db.execute(delete(MailboxCredential).where(MailboxCredential.gpt_email.in_(mailbox_delete_emails)))
        deleted_mailboxes = mailbox_result.rowcount or 0

    for source in ("sync", "refresh", "usage_refresh", "subscription_refresh"):
        for email in affected_emails:
            await clear_account_exception(db, source=source, email=email, commit=False)
        for account_id in deleted_remote_ids:
            await clear_account_exception(db, source=source, sub2api_account_id=account_id, commit=False)

    await record_event(
        db,
        "selected_accounts_deleted",
        f"Deleted {deleted_accounts} selected account(s), {deleted_mailboxes} mailbox credential(s), and {deleted_remote} sub2api account(s).",
        details={
            "selected_count": len(payload.accounts),
            "selected_sub2api_account_ids": sorted(selected_remote_ids),
            "selected_snapshot_ids": sorted(selected_snapshot_ids),
            "deleted_accounts": deleted_accounts,
            "deleted_mailboxes": deleted_mailboxes,
            "deleted_sub2api_accounts": deleted_remote,
            "deleted_no_email_sub2api_accounts": deleted_no_email_remote,
            "failed_sub2api_accounts": failed_remote,
        },
    )
    message = (
        f"Deleted {deleted_accounts} selected account(s), {deleted_mailboxes} mailbox credential(s), "
        f"and {deleted_remote} sub2api account(s)."
    )
    if failed_remote:
        message += f" {len(failed_remote)} sub2api account(s) could not be deleted."
    return DeactivatedCleanupResult(
        message=message,
        deleted_accounts=deleted_accounts,
        deleted_mailboxes=deleted_mailboxes,
        deleted_sub2api_accounts=deleted_remote,
        deleted_no_email_sub2api_accounts=deleted_no_email_remote,
        failed_sub2api_accounts=failed_remote,
    )


@router.get("/jobs", response_model=list[RefreshJobOut])
async def list_jobs(
    _: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> list[RefreshJobOut]:
    result = await db.execute(select(RefreshJob).order_by(desc(RefreshJob.created_at)).limit(100))
    return list(result.scalars().all())


@router.get("/events", response_model=list[EventOut])
async def list_events(
    _: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> list[EventOut]:
    result = await db.execute(select(AppEvent).order_by(desc(AppEvent.created_at)).limit(100))
    return list(result.scalars().all())


@router.get("/exception-records", response_model=list[AccountExceptionRecordOut])
async def list_exception_records(
    _: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> list[AccountExceptionRecordOut]:
    await backfill_account_exception_records(db)
    await prune_account_exception_records_to_current_accounts(db)
    result = await db.execute(
        select(AccountExceptionRecord)
        .where(AccountExceptionRecord.source == "sync")
        .order_by(desc(AccountExceptionRecord.updated_at))
        .limit(100)
    )
    return list(result.scalars().all())


@router.delete("/exception-records/{record_id}", response_model=MessageResponse)
async def delete_exception_record(
    record_id: int,
    _: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> MessageResponse:
    record = await db.get(AccountExceptionRecord, record_id)
    if record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Exception record not found.")
    await db.delete(record)
    await db.commit()
    return MessageResponse(message="异常账号记录已删除")


@router.delete("/history", response_model=MessageResponse)
async def clear_history(
    _: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> MessageResponse:
    await db.execute(delete(RefreshJob))
    await db.execute(delete(AppEvent))
    await db.commit()
    return MessageResponse(message="历史已清空")
