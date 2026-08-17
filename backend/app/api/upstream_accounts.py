from datetime import date, datetime, time, timedelta, timezone
from typing import Annotated, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, HTTPException, Path, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import require_admin
from app.models import Upstream
from app.schemas import (
    MessageResponse,
    AccountSchedulingChangeLogOut,
    AccountSchedulingChangePageOut,
    ChangeLogMarkReadRequest,
    ChangeLogUnreadCountsOut,
    PriorityIntervalAssignment,
    PriorityIntervalCreate,
    PriorityIntervalOut,
    PriorityIntervalUpdate,
    PriorityRebalanceOut,
    PriorityTieMoveRequest,
    ApiAccountEnabledUpdate,
    ApiAccountAvailabilityTestOut,
    ApiAccountConnectionTestOut,
    ApiAccountOut,
    ApiAccountUpdate,
    UpstreamApplyRequest,
    ApiAccountDiscoverAllOut,
    UpstreamIdentityRequest,
    UpstreamRateChangeLogOut,
    UpstreamChangeEventOut,
    UpstreamChangePageOut,
    ApiAccountRemoteDeleteRequest,
)
from app.services.runtime_config import get_runtime_config_service
from app.services.change_logs import (
    change_log_unread_counts,
    list_account_scheduling_changes,
    list_upstream_channel_changes,
    mark_account_scheduling_changes_read,
    mark_upstream_changes_read,
)
from app.services.upstream_accounts import (
    ApiAccountService,
    ApiAccountServiceError,
    get_api_account_service,
)
from app.services.upstream_rate_logs import list_upstream_rate_change_logs
from app.services.upstream_channels import (
    UpstreamService,
    get_upstream_service,
)
from app.services.upstream_priorities import (
    UpstreamPriorityService,
    get_upstream_priority_service,
)


router = APIRouter()
AccountId = Annotated[int, Path(ge=1, le=9_007_199_254_740_991)]
IntervalId = Annotated[int, Path(ge=1, le=9_007_199_254_740_991)]


def _http_error(exc: ApiAccountServiceError) -> HTTPException:
    return HTTPException(status_code=exc.status_code, detail=exc.public_message)


def _rate_log_date_bounds(
    start_date: date | None,
    end_date: date | None,
    time_zone: str,
) -> tuple[datetime | None, datetime | None]:
    if start_date is not None and end_date is not None and start_date > end_date:
        raise HTTPException(status_code=422, detail="The start date must not be after the end date.")
    try:
        zone = ZoneInfo(time_zone)
    except (ZoneInfoNotFoundError, ValueError):
        raise HTTPException(status_code=422, detail="The display time zone is invalid.") from None

    start_at = (
        datetime.combine(start_date, time.min, tzinfo=zone).astimezone(timezone.utc)
        if start_date is not None
        else None
    )
    if end_date is None:
        end_at = None
    else:
        try:
            next_date = end_date + timedelta(days=1)
        except OverflowError:
            raise HTTPException(status_code=422, detail="The end date is outside the supported range.") from None
        end_at = datetime.combine(next_date, time.min, tzinfo=zone).astimezone(timezone.utc)
    return start_at, end_at


@router.get("", response_model=list[ApiAccountOut])
async def list_upstream_accounts(
    _: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    service: ApiAccountService = Depends(get_api_account_service),
) -> list[ApiAccountOut]:
    try:
        return await service.list_accounts(db, use_cache=True)
    except ApiAccountServiceError as exc:
        raise _http_error(exc) from None


@router.post("/discover-all", response_model=ApiAccountDiscoverAllOut)
async def discover_all_upstream_accounts(
    _: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    service: ApiAccountService = Depends(get_api_account_service),
) -> ApiAccountDiscoverAllOut:
    try:
        return await service.discover_all(db)
    except ApiAccountServiceError as exc:
        raise _http_error(exc) from None


@router.get("/upstream-change-logs", response_model=list[UpstreamRateChangeLogOut])
async def list_upstream_rate_logs(
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
    before_id: Annotated[int | None, Query(ge=1)] = None,
    start_date: Annotated[date | None, Query()] = None,
    end_date: Annotated[date | None, Query()] = None,
    time_zone: Annotated[str, Query(min_length=1, max_length=80)] = "UTC",
    _: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> list[UpstreamRateChangeLogOut]:
    start_at, end_at = _rate_log_date_bounds(start_date, end_date, time_zone)
    retention_days = await get_runtime_config_service().get_upstream_rate_log_retention_days()
    logs = await list_upstream_rate_change_logs(
        db,
        retention_days=retention_days,
        limit=limit,
        before_id=before_id,
        start_at=start_at,
        end_at=end_at,
    )
    return [UpstreamRateChangeLogOut.model_validate(item) for item in logs]


@router.get("/upstream-change-events", response_model=UpstreamChangePageOut)
async def list_channel_change_events(
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
    page: Annotated[int, Query(ge=1)] = 1,
    before_id: Annotated[int | None, Query(ge=1)] = None,
    start_date: Annotated[date | None, Query()] = None,
    end_date: Annotated[date | None, Query()] = None,
    time_zone: Annotated[str, Query(min_length=1, max_length=80)] = "UTC",
    category: Annotated[
        Literal["all", "upstream", "account_rate"],
        Query(),
    ] = "all",
    _: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> UpstreamChangePageOut:
    start_at, end_at = _rate_log_date_bounds(start_date, end_date, time_zone)
    retention_days = await get_runtime_config_service().get_upstream_rate_log_retention_days()
    rows, last_read_id, unread_count, total_count = await list_upstream_channel_changes(
        db,
        retention_days=retention_days,
        limit=limit,
        page=page,
        before_id=before_id,
        start_at=start_at,
        end_at=end_at,
        category=category,
    )
    items = []
    for row in rows:
        item = UpstreamChangeEventOut.model_validate(row)
        item.unread = not row.legacy_imported and row.id > last_read_id
        items.append(item)
    return UpstreamChangePageOut(
        items=items,
        unread_count=unread_count,
        last_read_id=last_read_id,
        total_count=total_count,
        page=page,
        page_size=limit,
    )


@router.get("/scheduling-change-events", response_model=AccountSchedulingChangePageOut)
async def list_scheduling_change_events(
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
    page: Annotated[int, Query(ge=1)] = 1,
    before_id: Annotated[int | None, Query(ge=1)] = None,
    start_date: Annotated[date | None, Query()] = None,
    end_date: Annotated[date | None, Query()] = None,
    time_zone: Annotated[str, Query(min_length=1, max_length=80)] = "UTC",
    _: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> AccountSchedulingChangePageOut:
    start_at, end_at = _rate_log_date_bounds(start_date, end_date, time_zone)
    retention_days = await get_runtime_config_service().get_upstream_rate_log_retention_days()
    rows, last_read_id, unread_count, total_count = await list_account_scheduling_changes(
        db,
        retention_days=retention_days,
        limit=limit,
        page=page,
        before_id=before_id,
        start_at=start_at,
        end_at=end_at,
    )
    items = []
    for row in rows:
        item = AccountSchedulingChangeLogOut.model_validate(row)
        item.active_reasons = list(row.active_reasons or [])
        item.unread = row.id > last_read_id
        items.append(item)
    return AccountSchedulingChangePageOut(
        items=items,
        unread_count=unread_count,
        last_read_id=last_read_id,
        total_count=total_count,
        page=page,
        page_size=limit,
    )


@router.get("/change-log-unread-counts", response_model=ChangeLogUnreadCountsOut)
async def get_change_log_unread_counts(
    _: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> ChangeLogUnreadCountsOut:
    upstream, account_rate, scheduling = await change_log_unread_counts(
        db,
    )
    return ChangeLogUnreadCountsOut(
        upstream_changes=upstream,
        account_rate_changes=account_rate,
        account_scheduling_changes=scheduling,
    )


@router.post("/upstream-change-events/mark-read", response_model=MessageResponse)
async def mark_channel_change_events_read(
    payload: ChangeLogMarkReadRequest,
    category: Annotated[
        Literal["all", "upstream", "account_rate"],
        Query(),
    ] = "all",
    _: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> MessageResponse:
    await mark_upstream_changes_read(db, payload.through_id, category=category)
    return MessageResponse(message="Upstream changes marked as read.")


@router.post("/scheduling-change-events/mark-read", response_model=MessageResponse)
async def mark_scheduling_change_events_read(
    payload: ChangeLogMarkReadRequest,
    _: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> MessageResponse:
    await mark_account_scheduling_changes_read(db, payload.through_id)
    return MessageResponse(message="Account scheduling changes marked as read.")


@router.get("/priority-intervals", response_model=list[PriorityIntervalOut])
async def list_priority_intervals(
    _: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    service: UpstreamPriorityService = Depends(get_upstream_priority_service),
) -> list[PriorityIntervalOut]:
    return await service.list_intervals(db)


@router.post("/priority-intervals", response_model=PriorityIntervalOut, status_code=201)
async def create_priority_interval(
    payload: PriorityIntervalCreate,
    _: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    service: UpstreamPriorityService = Depends(get_upstream_priority_service),
) -> PriorityIntervalOut:
    try:
        return await service.create_interval(db, payload)
    except ApiAccountServiceError as exc:
        raise _http_error(exc) from None


@router.post("/priority-intervals/rebalance", response_model=PriorityRebalanceOut)
async def rebalance_priority_intervals(
    _: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    service: UpstreamPriorityService = Depends(get_upstream_priority_service),
) -> PriorityRebalanceOut:
    return await service.rebalance(db)


@router.put("/priority-intervals/{interval_id}", response_model=PriorityIntervalOut)
async def update_priority_interval(
    interval_id: IntervalId,
    payload: PriorityIntervalUpdate,
    _: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    service: UpstreamPriorityService = Depends(get_upstream_priority_service),
) -> PriorityIntervalOut:
    try:
        return await service.update_interval(db, interval_id, payload)
    except ApiAccountServiceError as exc:
        raise _http_error(exc) from None


@router.delete("/priority-intervals/{interval_id}", response_model=MessageResponse)
async def delete_priority_interval(
    interval_id: IntervalId,
    _: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    service: UpstreamPriorityService = Depends(get_upstream_priority_service),
) -> MessageResponse:
    if not await service.delete_interval(db, interval_id):
        raise HTTPException(status_code=404, detail="The priority interval was not found.")
    return MessageResponse(
        message="Deleted the priority interval without changing remote account priorities."
    )


@router.put("/{management_account_id}", response_model=ApiAccountOut)
async def upsert_upstream_account(
    management_account_id: AccountId,
    payload: ApiAccountUpdate,
    _: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    service: ApiAccountService = Depends(get_api_account_service),
) -> ApiAccountOut:
    try:
        if "upstream_id" in payload.model_fields_set and payload.upstream_id is not None:
            result = await db.execute(
                select(Upstream.id).where(
                    Upstream.id == payload.upstream_id.lower(),
                    Upstream.deleted_at.is_(None),
                )
            )
            internal_upstream_id = result.scalar_one_or_none()
            if internal_upstream_id is None:
                raise HTTPException(status_code=404, detail="Upstream not found.")
            payload = payload.model_copy(update={"upstream_id": internal_upstream_id})
        rate_reconcile_fields = {
            "upstream_id",
            "api_key",
            "upstream_group_multiplier_override",
        }
        rate_reconcile_requested = bool(
            payload.model_fields_set & rate_reconcile_fields
        )
        priority_reconcile_requested = bool(
            payload.model_fields_set
            & {"priority_assignment_when_disabled", "remote_name"}
        )
        availability_reconcile_requested = bool(
            payload.model_fields_set
            & {
                "availability_check_mode",
                "availability_monitor_id",
                "availability_test_model",
            }
        )
        reconcile_requested = rate_reconcile_requested or priority_reconcile_requested
        account = await service.upsert_account(
            db,
            management_account_id,
            payload,
            defer_priority_rebalance=reconcile_requested,
        )
        should_reconcile_in_background = bool(
            account.upstream_id
            and (rate_reconcile_requested or availability_reconcile_requested)
        )
        if should_reconcile_in_background and account.upstream_id is not None:
            get_upstream_service().queue_discover_channel(account.upstream_id)
            return account
        if reconcile_requested and isinstance(service, ApiAccountService):
            await service._rebalance_priorities_best_effort(db)
            refreshed = await service.list_accounts(db)
            return next(
                (
                    item
                    for item in refreshed
                    if item.management_account_id == management_account_id
                ),
                account,
            )
        return account
    except ApiAccountServiceError as exc:
        raise _http_error(exc) from None


@router.put("/{management_account_id}/priority-interval", response_model=ApiAccountOut)
async def assign_priority_interval(
    management_account_id: AccountId,
    payload: PriorityIntervalAssignment,
    _: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    service: UpstreamPriorityService = Depends(get_upstream_priority_service),
) -> ApiAccountOut:
    try:
        return await service.assign_interval(db, management_account_id, payload)
    except ApiAccountServiceError as exc:
        raise _http_error(exc) from None


@router.put("/{management_account_id}/priority-order", response_model=PriorityRebalanceOut)
async def move_equal_multiplier_priority(
    management_account_id: AccountId,
    payload: PriorityTieMoveRequest,
    _: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    service: UpstreamPriorityService = Depends(get_upstream_priority_service),
) -> PriorityRebalanceOut:
    try:
        return await service.move_equal_multiplier_priority(
            db,
            management_account_id,
            payload,
        )
    except ApiAccountServiceError as exc:
        raise _http_error(exc) from None


@router.delete("/{management_account_id}", response_model=MessageResponse)
async def delete_upstream_account(
    management_account_id: AccountId,
    payload: UpstreamIdentityRequest,
    _: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    service: ApiAccountService = Depends(get_api_account_service),
) -> MessageResponse:
    try:
        removed = await service.delete_account(
            db,
            management_account_id,
            payload.expected_identity_fingerprint,
        )
    except ApiAccountServiceError as exc:
        raise _http_error(exc) from None
    if removed:
        return MessageResponse(message=f"Removed local upstream configuration for account #{management_account_id}.")
    return MessageResponse(message=f"Account #{management_account_id} was not locally managed.")


@router.patch("/{management_account_id}/enabled", response_model=ApiAccountOut)
async def set_upstream_account_enabled(
    management_account_id: AccountId,
    payload: ApiAccountEnabledUpdate,
    _: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    service: ApiAccountService = Depends(get_api_account_service),
) -> ApiAccountOut:
    try:
        return await service.set_account_enabled(
            db,
            management_account_id,
            payload.enabled,
            payload.expected_identity_fingerprint,
        )
    except ApiAccountServiceError as exc:
        raise _http_error(exc) from None


@router.post(
    "/{management_account_id}/connection-test",
    response_model=ApiAccountConnectionTestOut,
)
async def force_upstream_account_connection_test(
    management_account_id: AccountId,
    payload: UpstreamIdentityRequest,
    _: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    service: UpstreamService = Depends(get_upstream_service),
) -> ApiAccountConnectionTestOut:
    try:
        return await service.test_account_connection(
            db,
            management_account_id,
            payload.expected_identity_fingerprint,
        )
    except ApiAccountServiceError as exc:
        raise _http_error(exc) from None


@router.post(
    "/{management_account_id}/availability-test",
    response_model=ApiAccountAvailabilityTestOut,
)
async def test_upstream_account_availability(
    management_account_id: AccountId,
    payload: UpstreamIdentityRequest,
    _: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    service: UpstreamService = Depends(get_upstream_service),
) -> ApiAccountAvailabilityTestOut:
    try:
        return await service.test_account_availability(
            db,
            management_account_id,
            payload.expected_identity_fingerprint,
        )
    except ApiAccountServiceError as exc:
        raise _http_error(exc) from None


@router.delete("/{management_account_id}/remote", response_model=MessageResponse)
async def delete_remote_upstream_account(
    management_account_id: AccountId,
    payload: ApiAccountRemoteDeleteRequest,
    _: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    service: ApiAccountService = Depends(get_api_account_service),
) -> MessageResponse:
    if payload.confirmed_account_id != management_account_id:
        raise HTTPException(status_code=409, detail="The account deletion confirmation is stale.")
    try:
        await service.delete_remote_account(
            db,
            management_account_id,
            payload.expected_identity_fingerprint,
        )
    except ApiAccountServiceError as exc:
        raise _http_error(exc) from None
    return MessageResponse(message=f"Deleted management-site API account #{management_account_id}.")


@router.post("/{management_account_id}/discover", response_model=ApiAccountOut)
async def discover_upstream_account(
    management_account_id: AccountId,
    payload: UpstreamIdentityRequest,
    _: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    service: ApiAccountService = Depends(get_api_account_service),
) -> ApiAccountOut:
    try:
        return await service.discover_account(
            db,
            management_account_id,
            payload.expected_identity_fingerprint,
        )
    except ApiAccountServiceError as exc:
        raise _http_error(exc) from None


@router.post("/{management_account_id}/apply", response_model=ApiAccountOut)
async def apply_upstream_account_rate(
    management_account_id: AccountId,
    payload: UpstreamApplyRequest,
    _: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    service: ApiAccountService = Depends(get_api_account_service),
) -> ApiAccountOut:
    try:
        return await service.apply_account(
            db,
            management_account_id,
            payload.confirmed_expected_management_billing_multiplier,
            payload.expected_identity_fingerprint,
        )
    except ApiAccountServiceError as exc:
        raise _http_error(exc) from None
