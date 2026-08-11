import asyncio
import logging
from datetime import date, datetime, timedelta, timezone
from time import perf_counter
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import require_admin
from app.models import UpstreamChannel
from app.schemas import (
    MessageResponse,
    UpstreamChannelDiscoverAllRequest,
    UpstreamChannelDiscoverAllOut,
    UpstreamChannelMonitorsOut,
    UpstreamChannelOut,
    UpstreamChannelUpdate,
    UpstreamOverviewOut,
    UpstreamUsageHistoryOut,
)
from app.services.upstream_accounts import UpstreamAccountServiceError
from app.services.upstream_channels import (
    UpstreamChannelService,
    UpstreamDiscoveryOptions,
    get_upstream_channel_service,
)
from app.services.events import elapsed_ms, record_event
from app.services.runtime_config import get_runtime_config_service
from app.services.upstream_usage_history import (
    normalize_history_time_zone,
    usage_day,
    usage_history,
)


router = APIRouter()
logger = logging.getLogger(__name__)
ChannelId = Annotated[int, Path(ge=1, le=9_007_199_254_740_991)]
MANUAL_UPSTREAM_SYNC_TIMEOUT_SECONDS = 300.0


def _manual_discovery_options(settings: dict[str, object]) -> UpstreamDiscoveryOptions:
    return UpstreamDiscoveryOptions(
        sync_rates=bool(settings.get("manual_upstream_sync_rate_enabled", True)),
        sync_priorities=bool(settings.get("manual_upstream_sync_priority_enabled", True)),
        evaluate_upstream_health=bool(
            settings.get("manual_upstream_sync_upstream_health_enabled", True)
        ),
        refresh_channel_monitors=bool(
            settings.get("manual_upstream_sync_channel_monitors_enabled", True)
        ),
        evaluate_account_availability=bool(
            settings.get("manual_upstream_sync_account_availability_enabled", False)
        ),
        evaluate_balance_guard=bool(
            settings.get("manual_upstream_sync_balance_guard_enabled", True)
        ),
        evaluate_rate_pause=bool(
            settings.get("manual_upstream_sync_rate_pause_enabled", True)
        ),
    )


def _http_error(exc: UpstreamAccountServiceError) -> HTTPException:
    return HTTPException(status_code=exc.status_code, detail=exc.public_message)


@router.get("", response_model=UpstreamOverviewOut)
async def upstream_channel_overview(
    _: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    service: UpstreamChannelService = Depends(get_upstream_channel_service),
) -> UpstreamOverviewOut:
    try:
        return await service.overview(db, sync_inventory=False)
    except UpstreamAccountServiceError as exc:
        raise _http_error(exc) from None


@router.get("/{channel_id}/usage-history", response_model=UpstreamUsageHistoryOut)
async def upstream_channel_usage_history(
    channel_id: ChannelId,
    start_date: Annotated[date | None, Query()] = None,
    end_date: Annotated[date | None, Query()] = None,
    api_key_account_id: Annotated[
        int | None,
        Query(ge=1, le=9_007_199_254_740_991),
    ] = None,
    time_zone: Annotated[str | None, Query(max_length=80)] = None,
    _: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    service: UpstreamChannelService = Depends(get_upstream_channel_service),
) -> UpstreamUsageHistoryOut:
    try:
        normalized_time_zone = normalize_history_time_zone(time_zone)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    today = usage_day(datetime.now(timezone.utc), normalized_time_zone)
    effective_end = end_date or today
    effective_start = start_date or effective_end - timedelta(days=29)
    if effective_end < effective_start:
        raise HTTPException(
            status_code=422,
            detail="The end date must not be earlier than the start date.",
        )
    channel = await db.get(UpstreamChannel, channel_id)
    if channel is None:
        raise HTTPException(status_code=404, detail="Upstream channel not found.")
    try:
        history = await usage_history(
            db,
            channel=channel,
            start_date=effective_start,
            end_date=effective_end,
            api_key_account_id=api_key_account_id,
            time_zone=normalized_time_zone,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    return UpstreamUsageHistoryOut(**history)


@router.post("/sync-inventory", response_model=UpstreamOverviewOut)
async def sync_upstream_channel_inventory(
    _: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    service: UpstreamChannelService = Depends(get_upstream_channel_service),
) -> UpstreamOverviewOut:
    started_at = perf_counter()
    try:
        result = await service.overview(db)
    except UpstreamAccountServiceError as exc:
        raise _http_error(exc) from None

    channel_count = sum(item.account_count > 0 for item in result.channels)
    account_count = sum(item.account_count for item in result.channels) + len(
        result.unassigned_accounts
    )
    try:
        await record_event(
            db,
            "manual_api_key_inventory_sync",
            (
                f"Synchronized {account_count} API key account(s) across "
                f"{channel_count} upstream channel(s)."
            ),
            details={
                "reason": "manual",
                "accounts": account_count,
                "channels": channel_count,
                "unassigned_accounts": len(result.unassigned_accounts),
                "duration_ms": elapsed_ms(started_at),
            },
        )
    except Exception:
        await db.rollback()
        logger.warning(
            "Could not persist the manual API key inventory sync event.",
            exc_info=True,
        )
    return result


@router.post("/discover-all", response_model=UpstreamChannelDiscoverAllOut)
async def discover_all_upstream_channels(
    payload: UpstreamChannelDiscoverAllRequest | None = None,
    _: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    service: UpstreamChannelService = Depends(get_upstream_channel_service),
) -> UpstreamChannelDiscoverAllOut:
    started_at = perf_counter()
    try:
        legacy_bindings = None
        if payload is not None and payload.confirm_legacy_bindings:
            legacy_bindings = {
                item.sub2api_account_id: item.expected_identity_fingerprint
                for item in payload.account_bindings
            }
        runtime = get_runtime_config_service()
        settings_getter = getattr(runtime, "get_public_settings", None)
        manual_settings_available = callable(settings_getter)
        runtime_settings = await settings_getter() if manual_settings_available else {}
        if not isinstance(runtime_settings, dict):
            runtime_settings = {}
        max_concurrency = await runtime.get_upstream_sync_max_concurrency()
        options = _manual_discovery_options(runtime_settings) if manual_settings_available else None
        discover_kwargs: dict[str, object] = {
            "max_concurrency": max_concurrency,
            "require_management_credentials": True,
            "force": True,
        }
        if legacy_bindings is not None:
            discover_kwargs["legacy_bindings"] = legacy_bindings
        if payload is not None and payload.skip_channel_ids:
            discover_kwargs["skip_channel_ids"] = set(payload.skip_channel_ids)
        if options is not None:
            discover_kwargs["options"] = options
        result = await asyncio.wait_for(
            service.discover_all(db, **discover_kwargs),
            timeout=MANUAL_UPSTREAM_SYNC_TIMEOUT_SECONDS,
        )
    except TimeoutError:
        raise HTTPException(
            status_code=504,
            detail="Manual API key account synchronization timed out.",
        ) from None
    except UpstreamAccountServiceError as exc:
        raise _http_error(exc) from None
    try:
        details = {
            "reason": "manual",
            "total": result.total,
            "succeeded": result.succeeded,
            "failed": result.failed,
            "cached": result.cached,
            "skipped": result.skipped,
            "force": result.force,
            "cache_max_age_seconds": result.cache_max_age_seconds,
            "probe_globally_enabled": result.probe_globally_enabled,
            "duration_ms": elapsed_ms(started_at),
        }
        if payload is not None and payload.skip_channel_ids:
            details["skip_channel_ids"] = payload.skip_channel_ids
        if manual_settings_available and options is not None:
            details["manual_tasks"] = {
                "rates": options.sync_rates,
                "priorities": options.sync_priorities,
                "upstream_health": options.evaluate_upstream_health,
                "channel_monitors": options.refresh_channel_monitors,
                "account_availability": options.evaluate_account_availability,
                "balance_guard": options.evaluate_balance_guard,
                "rate_pause": options.evaluate_rate_pause,
            }
        for field_name in (
            "inventory_duration_ms",
            "probe_duration_ms",
            "priority_duration_ms",
        ):
            value = getattr(result, field_name)
            if value is not None:
                details[field_name] = value
        await record_event(
            db,
            "manual_upstream_sync",
            (
                f"Synchronized {result.total} API key channel(s); "
                f"{result.succeeded} probed successfully, {result.cached} reused cached state, "
                f"{result.failed} failed, and {result.skipped} skipped."
            ),
            details=details,
        )
    except Exception:
        await db.rollback()
        logger.warning("Could not persist the manual upstream sync event.", exc_info=True)
    return result


@router.put("/{channel_id}", response_model=UpstreamChannelOut)
async def update_upstream_channel(
    channel_id: ChannelId,
    payload: UpstreamChannelUpdate,
    _: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    service: UpstreamChannelService = Depends(get_upstream_channel_service),
) -> UpstreamChannelOut:
    try:
        return await service.update_channel(db, channel_id, payload)
    except UpstreamAccountServiceError as exc:
        raise _http_error(exc) from None


@router.delete("/{channel_id}", response_model=MessageResponse)
async def delete_upstream_channel(
    channel_id: ChannelId,
    _: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    service: UpstreamChannelService = Depends(get_upstream_channel_service),
) -> MessageResponse:
    try:
        await service.delete_channel(db, channel_id)
    except UpstreamAccountServiceError as exc:
        raise _http_error(exc) from None
    return MessageResponse(message="空渠道已删除。")


@router.post("/{channel_id}/discover", response_model=UpstreamChannelOut)
async def discover_upstream_channel(
    channel_id: ChannelId,
    _: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    service: UpstreamChannelService = Depends(get_upstream_channel_service),
) -> UpstreamChannelOut:
    try:
        settings = await get_runtime_config_service().get_public_settings()
        return await service.discover_channel(
            db,
            channel_id,
            options=_manual_discovery_options(settings),
        )
    except UpstreamAccountServiceError as exc:
        raise _http_error(exc) from None


@router.post(
    "/{channel_id}/channel-monitors/refresh",
    response_model=UpstreamChannelMonitorsOut,
)
async def refresh_upstream_channel_monitors(
    channel_id: ChannelId,
    _: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    service: UpstreamChannelService = Depends(get_upstream_channel_service),
) -> UpstreamChannelMonitorsOut:
    try:
        return await service.refresh_channel_monitors(db, channel_id)
    except UpstreamAccountServiceError as exc:
        raise _http_error(exc) from None
