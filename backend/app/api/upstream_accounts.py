from datetime import date, datetime, time, timedelta, timezone
from typing import Annotated
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, HTTPException, Path, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import require_admin
from app.schemas import (
    MessageResponse,
    PriorityIntervalAssignment,
    PriorityIntervalCreate,
    PriorityIntervalOut,
    PriorityIntervalUpdate,
    PriorityRebalanceOut,
    UpstreamAccountEnabledUpdate,
    UpstreamAccountOut,
    UpstreamAccountUpdate,
    UpstreamApplyRequest,
    UpstreamDiscoverAllOut,
    UpstreamIdentityRequest,
    UpstreamRateChangeLogOut,
    UpstreamRemoteDeleteRequest,
)
from app.services.runtime_config import get_runtime_config_service
from app.services.upstream_accounts import (
    UpstreamAccountService,
    UpstreamAccountServiceError,
    get_upstream_account_service,
)
from app.services.upstream_rate_logs import list_upstream_rate_change_logs
from app.services.upstream_channels import get_upstream_channel_service
from app.services.upstream_priorities import (
    UpstreamPriorityService,
    get_upstream_priority_service,
)


router = APIRouter()
AccountId = Annotated[int, Path(ge=1, le=9_007_199_254_740_991)]
IntervalId = Annotated[int, Path(ge=1, le=9_007_199_254_740_991)]


def _http_error(exc: UpstreamAccountServiceError) -> HTTPException:
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


@router.get("", response_model=list[UpstreamAccountOut])
async def list_upstream_accounts(
    _: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    service: UpstreamAccountService = Depends(get_upstream_account_service),
) -> list[UpstreamAccountOut]:
    try:
        return await service.list_accounts(db)
    except UpstreamAccountServiceError as exc:
        raise _http_error(exc) from None


@router.post("/discover-all", response_model=UpstreamDiscoverAllOut)
async def discover_all_upstream_accounts(
    _: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    service: UpstreamAccountService = Depends(get_upstream_account_service),
) -> UpstreamDiscoverAllOut:
    try:
        return await service.discover_all(db)
    except UpstreamAccountServiceError as exc:
        raise _http_error(exc) from None


@router.get("/upstream-change-logs", response_model=list[UpstreamRateChangeLogOut])
@router.get("/rate-change-logs", response_model=list[UpstreamRateChangeLogOut])
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
    except UpstreamAccountServiceError as exc:
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
    except UpstreamAccountServiceError as exc:
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


@router.put("/{sub2api_account_id}", response_model=UpstreamAccountOut)
async def upsert_upstream_account(
    sub2api_account_id: AccountId,
    payload: UpstreamAccountUpdate,
    _: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    service: UpstreamAccountService = Depends(get_upstream_account_service),
) -> UpstreamAccountOut:
    try:
        reconcile_fields = {"channel_id", "api_key", "manual_group_multiplier"}
        reconcile_requested = bool(payload.model_fields_set & reconcile_fields)
        account = await service.upsert_account(
            db,
            sub2api_account_id,
            payload,
            defer_priority_rebalance=reconcile_requested,
        )
        should_reconcile = bool(
            account.channel_id
            and reconcile_requested
        )
        if should_reconcile and account.channel_id is not None:
            try:
                channel = await get_upstream_channel_service().discover_channel(
                    db,
                    account.channel_id,
                )
            except UpstreamAccountServiceError:
                if isinstance(service, UpstreamAccountService):
                    await service._rebalance_priorities_best_effort(db)
                    refreshed = await service.list_accounts(db)
                    return next(
                        (
                            item
                            for item in refreshed
                            if item.sub2api_account_id == sub2api_account_id
                        ),
                        account,
                    )
                return account
            return next(
                (
                    item
                    for item in channel.accounts
                    if item.sub2api_account_id == sub2api_account_id
                ),
                account,
            )
        if reconcile_requested and isinstance(service, UpstreamAccountService):
            await service._rebalance_priorities_best_effort(db)
            refreshed = await service.list_accounts(db)
            return next(
                (
                    item
                    for item in refreshed
                    if item.sub2api_account_id == sub2api_account_id
                ),
                account,
            )
        return account
    except UpstreamAccountServiceError as exc:
        raise _http_error(exc) from None


@router.put("/{sub2api_account_id}/priority-interval", response_model=UpstreamAccountOut)
async def assign_priority_interval(
    sub2api_account_id: AccountId,
    payload: PriorityIntervalAssignment,
    _: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    service: UpstreamPriorityService = Depends(get_upstream_priority_service),
) -> UpstreamAccountOut:
    try:
        return await service.assign_interval(db, sub2api_account_id, payload)
    except UpstreamAccountServiceError as exc:
        raise _http_error(exc) from None


@router.delete("/{sub2api_account_id}", response_model=MessageResponse)
async def delete_upstream_account(
    sub2api_account_id: AccountId,
    payload: UpstreamIdentityRequest,
    _: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    service: UpstreamAccountService = Depends(get_upstream_account_service),
) -> MessageResponse:
    try:
        removed = await service.delete_account(
            db,
            sub2api_account_id,
            payload.expected_identity_fingerprint,
        )
    except UpstreamAccountServiceError as exc:
        raise _http_error(exc) from None
    if removed:
        return MessageResponse(message=f"Removed local upstream configuration for account #{sub2api_account_id}.")
    return MessageResponse(message=f"Account #{sub2api_account_id} was not locally managed.")


@router.patch("/{sub2api_account_id}/enabled", response_model=UpstreamAccountOut)
async def set_upstream_account_enabled(
    sub2api_account_id: AccountId,
    payload: UpstreamAccountEnabledUpdate,
    _: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    service: UpstreamAccountService = Depends(get_upstream_account_service),
) -> UpstreamAccountOut:
    try:
        return await service.set_account_enabled(
            db,
            sub2api_account_id,
            payload.enabled,
            payload.expected_identity_fingerprint,
        )
    except UpstreamAccountServiceError as exc:
        raise _http_error(exc) from None


@router.delete("/{sub2api_account_id}/remote", response_model=MessageResponse)
async def delete_remote_upstream_account(
    sub2api_account_id: AccountId,
    payload: UpstreamRemoteDeleteRequest,
    _: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    service: UpstreamAccountService = Depends(get_upstream_account_service),
) -> MessageResponse:
    if payload.confirmed_account_id != sub2api_account_id:
        raise HTTPException(status_code=409, detail="The account deletion confirmation is stale.")
    try:
        await service.delete_remote_account(
            db,
            sub2api_account_id,
            payload.expected_identity_fingerprint,
        )
    except UpstreamAccountServiceError as exc:
        raise _http_error(exc) from None
    return MessageResponse(message=f"Deleted sub2api API key account #{sub2api_account_id}.")


@router.post("/{sub2api_account_id}/discover", response_model=UpstreamAccountOut)
async def discover_upstream_account(
    sub2api_account_id: AccountId,
    payload: UpstreamIdentityRequest,
    _: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    service: UpstreamAccountService = Depends(get_upstream_account_service),
) -> UpstreamAccountOut:
    try:
        return await service.discover_account(
            db,
            sub2api_account_id,
            payload.expected_identity_fingerprint,
        )
    except UpstreamAccountServiceError as exc:
        raise _http_error(exc) from None


@router.post("/{sub2api_account_id}/apply", response_model=UpstreamAccountOut)
async def apply_upstream_account_rate(
    sub2api_account_id: AccountId,
    payload: UpstreamApplyRequest,
    _: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    service: UpstreamAccountService = Depends(get_upstream_account_service),
) -> UpstreamAccountOut:
    try:
        return await service.apply_account(
            db,
            sub2api_account_id,
            payload.confirmed_target_rate,
            payload.expected_identity_fingerprint,
        )
    except UpstreamAccountServiceError as exc:
        raise _http_error(exc) from None
