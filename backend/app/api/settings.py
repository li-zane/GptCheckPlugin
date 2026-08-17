from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import require_admin
from app.models import AccountSnapshot, ApiAccount
from app.schemas import (
    AppSettingsOut,
    AppSettingsUpdate,
    MessageResponse,
    SiteLogoUpdateResult,
    ManagementSiteScanResult,
)
from app.services.monitor import get_monitor_service
from app.services.notification_dispatcher import get_notification_dispatcher
from app.services.discord_commands import get_discord_command_service
from app.services.notification_transports import NotificationTransportError
from app.services.account_liveness import get_account_liveness_limiter
from app.services.refresh import get_refresh_service
from app.services.runtime_config import RuntimeConfigServiceError, get_runtime_config_service
from app.services.upstream_rate_sync import get_upstream_rate_sync_service
from app.services.usage_refresh import get_usage_refresh_service

router = APIRouter()


async def _available_test_models(db: AsyncSession) -> list[dict[str, str]]:
    oauth_lists = list(
        (await db.execute(select(AccountSnapshot.available_models))).scalars().all()
    )
    api_key_lists = list(
        (await db.execute(select(ApiAccount.available_models))).scalars().all()
    )
    models: dict[str, str] = {}
    for raw_list in (*oauth_lists, *api_key_lists):
        if not isinstance(raw_list, list):
            continue
        for item in raw_list:
            if not isinstance(item, dict):
                continue
            model_id = str(item.get("id") or "").strip()[:160]
            if not model_id:
                continue
            display_name = str(item.get("display_name") or model_id).strip()[:200]
            models.setdefault(model_id, display_name or model_id)
    return [
        {"id": model_id, "display_name": models[model_id]}
        for model_id in sorted(models, key=str.casefold)
    ]


@router.get("/logo", include_in_schema=False)
async def get_site_logo(request: Request) -> Response:
    logo = await get_runtime_config_service().get_site_logo()
    if logo is None:
        raise HTTPException(status_code=404, detail="A custom site logo is not configured.")
    content, media_type = logo
    return Response(
        content=content,
        media_type=media_type,
        headers={
            "Cache-Control": (
                "public, max-age=31536000, immutable"
                if request.query_params.get("v")
                else "public, no-cache"
            ),
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.put("/logo", response_model=SiteLogoUpdateResult)
async def update_site_logo(
    request: Request,
    _: dict = Depends(require_admin),
) -> SiteLogoUpdateResult:
    content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > 1024 * 1024:
            raise HTTPException(status_code=413, detail="The site logo must not exceed 1 MB.")
        chunks.append(chunk)
    try:
        settings = await get_runtime_config_service().set_site_logo(
            b"".join(chunks),
            content_type,
        )
    except RuntimeConfigServiceError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.public_message) from None
    return SiteLogoUpdateResult(
        site_logo_url=settings.get("site_logo_url"),
        site_logo_updated_at=settings.get("site_logo_updated_at"),
        message="Site logo updated.",
    )


@router.delete("/logo", response_model=SiteLogoUpdateResult)
async def reset_site_logo(
    _: dict = Depends(require_admin),
) -> SiteLogoUpdateResult:
    settings = await get_runtime_config_service().clear_site_logo()
    return SiteLogoUpdateResult(
        site_logo_url=settings.get("site_logo_url"),
        site_logo_updated_at=settings.get("site_logo_updated_at"),
        message="Site logo reset.",
    )


@router.get("", response_model=AppSettingsOut)
async def get_settings(
    _: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> AppSettingsOut:
    settings = await get_runtime_config_service().get_public_settings()
    settings["available_test_models"] = await _available_test_models(db)
    return AppSettingsOut(**settings)


@router.put("", response_model=AppSettingsOut)
async def update_settings(
    payload: AppSettingsUpdate,
    _: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
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
        str(changes.get("management_site_x_api_key") or "").strip()
        or (
            changes.get("clear_management_site_x_api_key")
            and previous_settings.get("management_site_x_api_key_set")
        )
    )
    connection_changed = credential_changed or "management_site_base_url" in changed_fields
    automation_pause_changed = "automation_paused" in changed_fields
    inventory_changed = bool(changed_fields & {
        "api_key_account_sync_enabled",
        "api_key_account_sync_interval_seconds",
    })
    model_whitelist_changed = bool(
        changed_fields
        & {
            "account_model_whitelist_sync_enabled",
            "account_model_whitelist_sync_interval_seconds",
            "account_model_whitelist_sync_each_time",
        }
    )
    priority_changed = bool(changed_fields & {
        "upstream_priority_sync_enabled",
        "priority_assign_disabled_api_key_accounts",
        "priority_share_same_upstream_actual_multiplier",
    })
    upstream_changed = bool(changed_fields & {
        "upstream_sync_enabled",
        "upstream_sync_interval_seconds",
        "upstream_sync_max_concurrency",
        "upstream_rate_sync_enabled",
        "manual_upstream_sync_rate_enabled",
        "manual_upstream_sync_priority_enabled",
        "manual_upstream_sync_upstream_health_enabled",
        "manual_upstream_monitor_sync_enabled",
        "manual_upstream_sync_account_availability_enabled",
        "manual_upstream_sync_balance_guard_enabled",
        "manual_upstream_sync_rate_pause_enabled",
        "api_key_auto_disable_on_upstream_unavailable",
        "api_account_auto_pause_on_upstream_monitor_unavailable_enabled",
        "api_key_availability_all_tests_must_succeed",
        "upstream_monitor_auto_probe_enabled",
        "upstream_monitor_fallback_without_monitor_enabled",
        "upstream_monitor_fallback_test_models",
        "upstream_monitor_fallback_test_model",
        "upstream_monitor_fallback_test_attempts",
        "upstream_monitor_recovery_test_attempts",
        "upstream_monitor_test_attempt_interval_seconds",
        "api_key_auto_pause_on_negative_balance_enabled",
        "upstream_negative_balance_basis",
        "upstream_balance_pause_threshold",
        "upstream_rate_log_retention_days",
    })

    if connection_changed or automation_pause_changed or changed_fields & {
        "oauth_account_sync_enabled",
        "monitor_interval_seconds",
        "recovery_enabled",
    }:
        get_monitor_service().wake()

    if (
        connection_changed
        or automation_pause_changed
        or inventory_changed
        or upstream_changed
        or priority_changed
    ):
        upstream_scheduler = get_upstream_rate_sync_service()
        if connection_changed or automation_pause_changed:
            upstream_scheduler.wake()
        else:
            if inventory_changed:
                upstream_scheduler.wake_inventory()
            if upstream_changed:
                upstream_scheduler.wake_upstream()
            if priority_changed:
                upstream_scheduler.wake_priority()
            if model_whitelist_changed:
                upstream_scheduler.wake_model_whitelist()
    elif model_whitelist_changed:
        get_upstream_rate_sync_service().wake_model_whitelist()

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
    if (
        bool(str(changes.get("discord_bot_token") or "").strip())
        or bool(changes.get("clear_discord_bot_token"))
        or bool(changed_fields & {
        "discord_bot_notifications_enabled",
        "discord_bot_token_set",
        "discord_bot_channel_id",
        "notify_oauth_account_disabled",
        "notify_account_enabled",
        "notify_api_key_rate_changed",
        "notify_upstream_group_changed",
        "notify_upstream_balance_low",
        "notify_upstream_token_invalid",
        })
    ):
        get_notification_dispatcher().wake()
        get_discord_command_service().wake()
    settings["available_test_models"] = await _available_test_models(db)
    return AppSettingsOut(**settings)


@router.post("/scan-management-site", response_model=ManagementSiteScanResult)
async def scan_management_site(_: dict = Depends(require_admin)) -> ManagementSiteScanResult:
    result = await get_runtime_config_service().scan_management_site(apply=True)
    get_monitor_service().wake()
    get_upstream_rate_sync_service().wake()
    return ManagementSiteScanResult(**result)


@router.post("/notifications/test", response_model=MessageResponse)
async def send_notification_test(_: dict = Depends(require_admin)) -> MessageResponse:
    try:
        await get_notification_dispatcher().send_test_notification()
    except NotificationTransportError as exc:
        messages = {
            "notifications_disabled": "请先启用 Discord Bot 通知。",
            "discord_not_configured": "请先保存有效的 Bot Token 和频道 ID。",
            "discord_unauthorized": "Discord Bot Token 无效或已失效。",
            "discord_rate_limited": "Discord 当前触发频率限制，请稍后再试。",
            "discord_timeout": "Discord 请求超时。",
            "discord_network_error": "无法连接 Discord。",
            "discord_channel_not_found": "Discord 频道不存在，请重新复制目标服务器频道的频道 ID。",
            "discord_missing_access": "Bot 尚未安装到该频道所在服务器，或无权查看该频道。请使用服务器安装并检查频道权限。",
            "discord_missing_permissions": "Bot 缺少发送消息权限。请授予查看频道、发送消息和嵌入链接权限。",
            "discord_request_rejected": "Discord 拒绝了消息请求。",
        }
        status_code = 400 if exc.code in {
            "notifications_disabled",
            "discord_not_configured",
            "discord_unauthorized",
            "discord_channel_not_found",
            "discord_missing_access",
            "discord_missing_permissions",
            "discord_request_rejected",
        } else 502
        raise HTTPException(
            status_code=status_code,
            detail=messages.get(exc.code, "Discord 测试通知发送失败。"),
        ) from None
    return MessageResponse(message="测试通知已发送。")
