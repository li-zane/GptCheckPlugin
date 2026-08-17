import asyncio
import logging
from datetime import date, datetime, timedelta, timezone
from time import perf_counter
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crypto import decrypt_text
from app.core.database import get_db
from app.core.security import require_admin
from app.models import Upstream
from app.schemas import (
    MessageResponse,
    UpstreamDiscoverAllRequest,
    UpstreamDiscoverAllOut,
    UpstreamCredentialsOut,
    UpstreamMonitorsOut,
    UpstreamOut,
    UpstreamUpdate,
    UpstreamOverviewOut,
    UpstreamUsageHistoryOut,
)
from app.services.upstream_accounts import ApiAccountServiceError
from app.services.upstream_channels import (
    UpstreamService,
    UpstreamDiscoveryOptions,
    get_upstream_service,
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
UpstreamId = Annotated[
    str,
    Path(pattern=r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"),
]
MANUAL_UPSTREAM_SYNC_TIMEOUT_SECONDS = 300.0


def _manual_discovery_options(settings: dict[str, object]) -> UpstreamDiscoveryOptions:
    return UpstreamDiscoveryOptions(
        sync_rates=bool(settings.get("manual_upstream_sync_rate_enabled", True)),
        sync_priorities=bool(settings.get("manual_upstream_sync_priority_enabled", True)),
        evaluate_upstream_health=bool(
            settings.get("manual_upstream_sync_upstream_health_enabled", True)
        ),
        refresh_upstream_monitors=bool(
            settings.get("manual_upstream_sync_upstream_monitors_enabled", True)
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


def _http_error(exc: ApiAccountServiceError) -> HTTPException:
    return HTTPException(status_code=exc.status_code, detail=exc.public_message)


async def _load_upstream(
    db: AsyncSession,
    upstream_id: str,
    *,
    include_deleted: bool = False,
) -> Upstream:
    statement = select(Upstream).where(
        Upstream.id == upstream_id.lower()
    )
    if not include_deleted:
        statement = statement.where(Upstream.deleted_at.is_(None))
    result = await db.execute(statement)
    upstream = result.scalar_one_or_none()
    if upstream is None:
        raise HTTPException(status_code=404, detail="Upstream not found.")
    return upstream


@router.get("", response_model=UpstreamOverviewOut)
async def upstream_channel_overview(
    _: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    service: UpstreamService = Depends(get_upstream_service),
) -> UpstreamOverviewOut:
    try:
        return await service.overview(db, sync_inventory=False)
    except ApiAccountServiceError as exc:
        raise _http_error(exc) from None


@router.get("/{upstream_id}/credentials", response_model=UpstreamCredentialsOut)
async def upstream_channel_credentials(
    upstream_id: UpstreamId,
    response: Response,
    _: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> UpstreamCredentialsOut:
    upstream = await _load_upstream(db, upstream_id)
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return UpstreamCredentialsOut(
        access_token=decrypt_text(upstream.encrypted_access_token),
        refresh_token=decrypt_text(upstream.encrypted_refresh_token),
        login_username=decrypt_text(upstream.encrypted_login_username),
        login_password=decrypt_text(upstream.encrypted_login_password),
    )


@router.get("/{upstream_id}/usage-history", response_model=UpstreamUsageHistoryOut)
async def upstream_channel_usage_history(
    upstream_id: UpstreamId,
    start_date: Annotated[date | None, Query()] = None,
    end_date: Annotated[date | None, Query()] = None,
    management_account_id: Annotated[
        int | None,
        Query(ge=1, le=9_007_199_254_740_991),
    ] = None,
    time_zone: Annotated[str | None, Query(max_length=80)] = None,
    _: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    service: UpstreamService = Depends(get_upstream_service),
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
    upstream = await _load_upstream(db, upstream_id, include_deleted=True)
    try:
        history = await usage_history(
            db,
            upstream=upstream,
            start_date=effective_start,
            end_date=effective_end,
            management_account_id=management_account_id,
            time_zone=normalized_time_zone,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    return UpstreamUsageHistoryOut(**history)


@router.post("/sync-inventory", response_model=UpstreamOverviewOut)
async def sync_upstream_channel_inventory(
    _: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    service: UpstreamService = Depends(get_upstream_service),
) -> UpstreamOverviewOut:
    started_at = perf_counter()
    try:
        result = await service.overview(db)
    except ApiAccountServiceError as exc:
        raise _http_error(exc) from None

    upstream_count = sum(item.account_count > 0 for item in result.upstreams)
    account_count = sum(item.account_count for item in result.upstreams) + len(
        result.unassigned_accounts
    )
    try:
        await record_event(
            db,
            "manual_api_key_inventory_sync",
            (
                f"Synchronized {account_count} API key account(s) across "
                f"{upstream_count} upstream(s)."
            ),
            details={
                "reason": "manual",
                "accounts": account_count,
                "upstreams": upstream_count,
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


@router.post("/discover-all", response_model=UpstreamDiscoverAllOut)
async def discover_all_upstream_channels(
    payload: UpstreamDiscoverAllRequest | None = None,
    _: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    service: UpstreamService = Depends(get_upstream_service),
) -> UpstreamDiscoverAllOut:
    started_at = perf_counter()
    try:
        legacy_bindings = None
        if payload is not None and payload.confirm_legacy_bindings:
            legacy_bindings = {
                item.management_account_id: item.expected_identity_fingerprint
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
        if payload is not None and payload.skip_upstream_ids:
            rows = (
                await db.execute(
                    select(Upstream.id).where(
                        Upstream.id.in_(payload.skip_upstream_ids),
                        Upstream.deleted_at.is_(None),
                    )
                )
            ).scalars().all()
            discover_kwargs["skip_upstream_ids"] = set(rows)
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
    except ApiAccountServiceError as exc:
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
        if payload is not None and payload.skip_upstream_ids:
            details["skip_upstream_ids"] = payload.skip_upstream_ids
        if manual_settings_available and options is not None:
            details["manual_tasks"] = {
                "rates": options.sync_rates,
                "priorities": options.sync_priorities,
                "upstream_health": options.evaluate_upstream_health,
                "upstream_monitors": options.refresh_upstream_monitors,
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
                f"Synchronized {result.total} upstream(s); "
                f"{result.succeeded} probed successfully, {result.cached} reused cached state, "
                f"{result.failed} failed, and {result.skipped} skipped."
            ),
            details=details,
        )
    except Exception:
        await db.rollback()
        logger.warning("Could not persist the manual upstream sync event.", exc_info=True)
    return result


@router.put("/{upstream_id}", response_model=UpstreamOut)
async def update_upstream_channel(
    upstream_id: UpstreamId,
    payload: UpstreamUpdate,
    _: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    service: UpstreamService = Depends(get_upstream_service),
) -> UpstreamOut:
    try:
        upstream = await _load_upstream(db, upstream_id)
        return await service.update_channel(db, upstream.id, payload)
    except ApiAccountServiceError as exc:
        raise _http_error(exc) from None


@router.delete("/{upstream_id}", response_model=MessageResponse)
async def delete_upstream_channel(
    upstream_id: UpstreamId,
    _: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    service: UpstreamService = Depends(get_upstream_service),
) -> MessageResponse:
    try:
        upstream = await _load_upstream(db, upstream_id)
        await service.delete_channel(db, upstream.id)
    except ApiAccountServiceError as exc:
        raise _http_error(exc) from None
    return MessageResponse(message="空上游已归档。")


@router.post("/{upstream_id}/discover", response_model=UpstreamOut)
async def discover_upstream_channel(
    upstream_id: UpstreamId,
    _: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    service: UpstreamService = Depends(get_upstream_service),
) -> UpstreamOut:
    try:
        upstream = await _load_upstream(db, upstream_id)
        settings = await get_runtime_config_service().get_public_settings()
        return await service.discover_channel(
            db,
            upstream.id,
            options=_manual_discovery_options(settings),
        )
    except ApiAccountServiceError as exc:
        raise _http_error(exc) from None


@router.post(
    "/{upstream_id}/upstream-monitors/refresh",
    response_model=UpstreamMonitorsOut,
)
async def refresh_upstream_monitors(
    upstream_id: UpstreamId,
    _: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    service: UpstreamService = Depends(get_upstream_service),
) -> UpstreamMonitorsOut:
    try:
        upstream = await _load_upstream(db, upstream_id)
        return await service.refresh_upstream_monitors(db, upstream.id)
    except ApiAccountServiceError as exc:
        raise _http_error(exc) from None
