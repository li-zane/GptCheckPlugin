import logging
from time import perf_counter
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import require_admin
from app.schemas import (
    MessageResponse,
    UpstreamChannelDiscoverAllRequest,
    UpstreamChannelDiscoverAllOut,
    UpstreamChannelOut,
    UpstreamChannelUpdate,
    UpstreamOverviewOut,
)
from app.services.upstream_accounts import UpstreamAccountServiceError
from app.services.upstream_channels import UpstreamChannelService, get_upstream_channel_service
from app.services.events import elapsed_ms, record_event
from app.services.runtime_config import get_runtime_config_service


router = APIRouter()
logger = logging.getLogger(__name__)
ChannelId = Annotated[int, Path(ge=1, le=9_007_199_254_740_991)]


def _http_error(exc: UpstreamAccountServiceError) -> HTTPException:
    return HTTPException(status_code=exc.status_code, detail=exc.public_message)


@router.get("", response_model=UpstreamOverviewOut)
async def upstream_channel_overview(
    _: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    service: UpstreamChannelService = Depends(get_upstream_channel_service),
) -> UpstreamOverviewOut:
    try:
        return await service.overview(db)
    except UpstreamAccountServiceError as exc:
        raise _http_error(exc) from None


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
        max_concurrency = await runtime.get_upstream_sync_max_concurrency()
        probe_globally_enabled = await runtime.get_upstream_sync_enabled()
        if not probe_globally_enabled:
            inventory_started_at = perf_counter()
            overview = await service.overview(db)
            inventory_duration_ms = elapsed_ms(inventory_started_at)
            occupied_channels = [
                channel for channel in overview.channels if channel.account_count > 0
            ]
            result = UpstreamChannelDiscoverAllOut(
                total=len(occupied_channels),
                succeeded=0,
                failed=0,
                cached=0,
                skipped=len(occupied_channels),
                force=True,
                cache_max_age_seconds=None,
                probe_globally_enabled=False,
                inventory_duration_ms=inventory_duration_ms,
                probe_duration_ms=0,
                priority_duration_ms=0,
                channels=overview.channels,
                overview=overview,
            )
        else:
            result = (
                await service.discover_all(
                    db,
                    legacy_bindings=legacy_bindings,
                    max_concurrency=max_concurrency,
                    require_management_credentials=True,
                    force=True,
                )
                if legacy_bindings is not None
                else await service.discover_all(
                    db,
                    max_concurrency=max_concurrency,
                    require_management_credentials=True,
                    force=True,
                )
            )
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
        return await service.discover_channel(db, channel_id)
    except UpstreamAccountServiceError as exc:
        raise _http_error(exc) from None
