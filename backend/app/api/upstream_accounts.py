from datetime import date, datetime, time, timedelta, timezone
from typing import Annotated
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, HTTPException, Path, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import require_admin
from app.schemas import (
    MessageResponse,
    UpstreamAccountEnabledUpdate,
    UpstreamAccountOut,
    UpstreamAccountUpdate,
    UpstreamApplyRequest,
    UpstreamDiscoverAllOut,
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


router = APIRouter()
AccountId = Annotated[int, Path(ge=1, le=9_007_199_254_740_991)]


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


@router.put("/{sub2api_account_id}", response_model=UpstreamAccountOut)
async def upsert_upstream_account(
    sub2api_account_id: AccountId,
    payload: UpstreamAccountUpdate,
    _: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    service: UpstreamAccountService = Depends(get_upstream_account_service),
) -> UpstreamAccountOut:
    try:
        account = await service.upsert_account(db, sub2api_account_id, payload)
        should_reconcile = bool(
            account.channel_id
            and payload.model_fields_set
            & {"channel_id", "api_key", "manual_group_multiplier"}
        )
        if should_reconcile and account.channel_id is not None:
            try:
                channel = await get_upstream_channel_service().discover_channel(
                    db,
                    account.channel_id,
                )
            except UpstreamAccountServiceError:
                return account
            return next(
                (
                    item
                    for item in channel.accounts
                    if item.sub2api_account_id == sub2api_account_id
                ),
                account,
            )
        return account
    except UpstreamAccountServiceError as exc:
        raise _http_error(exc) from None


@router.delete("/{sub2api_account_id}", response_model=MessageResponse)
async def delete_upstream_account(
    sub2api_account_id: AccountId,
    _: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    service: UpstreamAccountService = Depends(get_upstream_account_service),
) -> MessageResponse:
    removed = await service.delete_account(db, sub2api_account_id)
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
        return await service.set_account_enabled(db, sub2api_account_id, payload.enabled)
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
        await service.delete_remote_account(db, sub2api_account_id)
    except UpstreamAccountServiceError as exc:
        raise _http_error(exc) from None
    return MessageResponse(message=f"Deleted sub2api API key account #{sub2api_account_id}.")


@router.post("/{sub2api_account_id}/discover", response_model=UpstreamAccountOut)
async def discover_upstream_account(
    sub2api_account_id: AccountId,
    _: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    service: UpstreamAccountService = Depends(get_upstream_account_service),
) -> UpstreamAccountOut:
    try:
        return await service.discover_account(db, sub2api_account_id)
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
        )
    except UpstreamAccountServiceError as exc:
        raise _http_error(exc) from None
