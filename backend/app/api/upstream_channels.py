from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import require_admin
from app.schemas import (
    UpstreamChannelDiscoverAllOut,
    UpstreamChannelOut,
    UpstreamChannelUpdate,
    UpstreamOverviewOut,
)
from app.services.upstream_accounts import UpstreamAccountServiceError
from app.services.upstream_channels import UpstreamChannelService, get_upstream_channel_service


router = APIRouter()
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


@router.post("/discover-all", response_model=UpstreamChannelDiscoverAllOut)
async def discover_all_upstream_channels(
    _: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    service: UpstreamChannelService = Depends(get_upstream_channel_service),
) -> UpstreamChannelDiscoverAllOut:
    try:
        return await service.discover_all(db)
    except UpstreamAccountServiceError as exc:
        raise _http_error(exc) from None


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
