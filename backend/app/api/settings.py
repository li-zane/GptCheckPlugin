from fastapi import APIRouter, Depends

from app.core.security import require_admin
from app.schemas import AppSettingsOut, AppSettingsUpdate, Sub2ApiPortScanResult
from app.services.monitor import get_monitor_service
from app.services.refresh import get_refresh_service
from app.services.runtime_config import get_runtime_config_service
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
    settings = await get_runtime_config_service().update_public_settings(payload.model_dump(exclude_unset=True))
    get_monitor_service().wake()
    get_upstream_rate_sync_service().wake()
    get_usage_refresh_service().wake()
    await get_refresh_service().wake_concurrency()
    return AppSettingsOut(**settings)


@router.post("/scan-sub2api", response_model=Sub2ApiPortScanResult)
async def scan_sub2api(_: dict = Depends(require_admin)) -> Sub2ApiPortScanResult:
    result = await get_runtime_config_service().scan_sub2api_ports(apply=True)
    get_monitor_service().wake()
    get_upstream_rate_sync_service().wake()
    return Sub2ApiPortScanResult(**result)
