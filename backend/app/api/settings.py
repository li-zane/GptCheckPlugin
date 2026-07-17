from fastapi import APIRouter, Depends, HTTPException

from app.core.security import require_admin
from app.schemas import AppSettingsOut, AppSettingsUpdate, Sub2ApiPortScanResult
from app.services.monitor import get_monitor_service
from app.services.account_liveness import get_account_liveness_limiter
from app.services.refresh import get_refresh_service
from app.services.runtime_config import RuntimeConfigServiceError, get_runtime_config_service
from app.services.upstream_rate_sync import get_upstream_rate_sync_service
from app.services.usage_refresh import get_usage_refresh_service

router = APIRouter()


@router.get("", response_model=AppSettingsOut)
async def get_settings(_: dict = Depends(require_admin)) -> AppSettingsOut:
    return AppSettingsOut(**await get_runtime_config_service().get_public_settings())


@router.put("", response_model=AppSettingsOut)
async def update_settings(
    payload: AppSettingsUpdate,
    _: dict = Depends(require_admin),
) -> AppSettingsOut:
    changes = payload.model_dump(exclude_unset=True)
    runtime_config = get_runtime_config_service()
    try:
        previous_settings = await runtime_config.get_public_settings()
        settings = await runtime_config.update_public_settings(changes)
    except RuntimeConfigServiceError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.public_message) from None
    changed_fields = {
        key
        for key in previous_settings.keys() | settings.keys()
        if previous_settings.get(key) != settings.get(key)
    }
    credential_changed = bool(
        str(changes.get("sub2api_x_api_key") or "").strip()
        or (
            changes.get("clear_sub2api_x_api_key")
            and previous_settings.get("sub2api_x_api_key_set")
        )
    )
    connection_changed = credential_changed or "sub2api_base_url" in changed_fields
    automation_pause_changed = "automation_paused" in changed_fields
    inventory_changed = bool(changed_fields & {
        "api_key_account_sync_enabled",
        "api_key_account_sync_interval_seconds",
    })
    upstream_changed = bool(changed_fields & {
        "upstream_sync_enabled",
        "upstream_sync_interval_seconds",
        "upstream_sync_max_concurrency",
        "upstream_rate_sync_enabled",
        "upstream_priority_sync_enabled",
        "api_key_auto_disable_on_upstream_unavailable",
        "upstream_rate_log_retention_days",
    })

    if connection_changed or automation_pause_changed or changed_fields & {
        "oauth_account_sync_enabled",
        "monitor_interval_seconds",
        "recovery_enabled",
    }:
        get_monitor_service().wake()

    if connection_changed or automation_pause_changed or inventory_changed or upstream_changed:
        upstream_scheduler = get_upstream_rate_sync_service()
        if connection_changed or automation_pause_changed:
            upstream_scheduler.wake()
        else:
            if inventory_changed:
                upstream_scheduler.wake_inventory()
            if upstream_changed:
                upstream_scheduler.wake_upstream()

    if connection_changed or automation_pause_changed or changed_fields & {
        "usage_refresh_enabled",
        "usage_refresh_interval_seconds",
        "usage_refresh_max_concurrency",
    }:
        get_usage_refresh_service().wake()
    if changed_fields & {
        "refresh_max_concurrency",
        "protocol_refresh_max_concurrency",
        "browser_refresh_max_concurrency",
    }:
        await get_refresh_service().wake_concurrency()
    if "account_liveness_max_concurrency" in changed_fields:
        await get_account_liveness_limiter().wake()
    return AppSettingsOut(**settings)


@router.post("/scan-sub2api", response_model=Sub2ApiPortScanResult)
async def scan_sub2api(_: dict = Depends(require_admin)) -> Sub2ApiPortScanResult:
    result = await get_runtime_config_service().scan_sub2api_ports(apply=True)
    get_monitor_service().wake()
    get_upstream_rate_sync_service().wake()
    return Sub2ApiPortScanResult(**result)
