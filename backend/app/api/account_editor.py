from __future__ import annotations

from dataclasses import replace
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Path, Query, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import require_admin
from app.models import AccountEditPreset, utcnow
from app.schemas import (
    AccountEditCurrent,
    AccountEditPresetApply,
    AccountEditPresetCreate,
    AccountEditPresetOut,
    AccountEditPresetUpdate,
    AccountEditResourceOption,
    AccountEditResult,
    AccountEditUpdate,
    AccountEditorOut,
    AccountLivenessModelOut,
    MessageResponse,
)
from app.services.account_editor import (
    AccountEditorResources,
    account_configuration_from_remote,
    account_extra_patch_from_configuration,
    account_identity_fingerprint,
    assert_account_identity,
    load_account_editor_resources,
    resource_id,
    validate_account_edit_configuration,
)
from app.services.events import record_event
from app.services.sub2api import Sub2ApiClient, Sub2ApiRequestError


router = APIRouter()


def _sub2api_http_error(exc: Sub2ApiRequestError) -> HTTPException:
    if exc.status_code == 404:
        status_code = status.HTTP_404_NOT_FOUND
    elif exc.status_code == 409:
        status_code = status.HTTP_409_CONFLICT
    else:
        status_code = status.HTTP_502_BAD_GATEWAY
    return HTTPException(status_code=status_code, detail=str(exc))


async def _load_resources(account_id: int) -> tuple[Sub2ApiClient, AccountEditorResources]:
    sub2api = Sub2ApiClient()
    try:
        resources = await load_account_editor_resources(sub2api, account_id)
    except Sub2ApiRequestError as exc:
        raise _sub2api_http_error(exc) from exc
    return sub2api, resources


async def _list_presets(db: AsyncSession, platform: str | None = None) -> list[AccountEditPreset]:
    statement = select(AccountEditPreset)
    if platform:
        statement = statement.where(AccountEditPreset.platform == platform)
    result = await db.execute(statement.order_by(AccountEditPreset.name, AccountEditPreset.id))
    return list(result.scalars().all())


def _resource_name(item: dict[str, Any], fallback: str) -> str:
    value = str(item.get("name") or item.get("display_name") or "").strip()
    return value or fallback


def _group_option(item: dict[str, Any]) -> AccountEditResourceOption | None:
    item_id = resource_id(item)
    if item_id is None:
        return None
    multiplier = item.get("rate_multiplier", item.get("multiplier"))
    detail = f"倍率 {multiplier:g}" if isinstance(multiplier, (int, float)) and not isinstance(multiplier, bool) else None
    return AccountEditResourceOption(
        id=item_id,
        name=_resource_name(item, f"分组 #{item_id}"),
        status=str(item.get("status") or "").strip() or None,
        detail=detail,
    )


def _proxy_option(item: dict[str, Any]) -> AccountEditResourceOption | None:
    item_id = resource_id(item)
    if item_id is None:
        return None
    protocol = str(item.get("protocol") or "").strip()
    host = str(item.get("host") or "").strip()
    port = item.get("port")
    address = f"{protocol}://{host}:{port}" if protocol and host and isinstance(port, int) else None
    return AccountEditResourceOption(
        id=item_id,
        name=_resource_name(item, f"代理 #{item_id}"),
        status=str(item.get("status") or "").strip() or None,
        detail=address,
    )


async def _editor_out(
    db: AsyncSession,
    sub2api: Sub2ApiClient,
    resources: AccountEditorResources,
) -> AccountEditorOut:
    account = resources.account
    account_id = sub2api.account_id(account)
    platform = sub2api.account_platform(account)
    account_type = sub2api.account_type(account)
    account_name = sub2api.account_name(account)
    if not account_id or not platform or not account_type or not account_name:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="sub2api account response is missing editable identity fields.",
        )
    configuration = account_configuration_from_remote(sub2api, account)
    groups = [option for item in resources.groups if (option := _group_option(item)) is not None]
    proxies = [option for item in resources.proxies if (option := _proxy_option(item)) is not None]
    model_candidates = sorted(
        (
            AccountLivenessModelOut(
                id=str(item.get("id") or "").strip(),
                display_name=str(item.get("display_name") or item.get("id") or "").strip(),
            )
            for item in resources.model_candidates
            if str(item.get("id") or "").strip()
        ),
        key=lambda item: item.id.casefold(),
    )
    presets = await _list_presets(db, platform)
    return AccountEditorOut(
        account=AccountEditCurrent(
            account_id=account_id,
            name=account_name,
            platform=platform,
            account_type=account_type,
            identity_fingerprint=account_identity_fingerprint(sub2api, account),
            **configuration.model_dump(),
        ),
        groups=sorted(groups, key=lambda item: (item.name.casefold(), item.id)),
        proxies=sorted(proxies, key=lambda item: (item.name.casefold(), item.id)),
        model_candidates=model_candidates,
        model_candidates_complete=resources.model_candidates_complete,
        presets=[AccountEditPresetOut.model_validate(preset) for preset in presets],
        resources_checked_at=resources.checked_at,
    )


@router.get("/editor/{sub2api_account_id}", response_model=AccountEditorOut)
async def get_account_editor(
    sub2api_account_id: int = Path(ge=1),
    _: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> AccountEditorOut:
    sub2api, resources = await _load_resources(sub2api_account_id)
    return await _editor_out(db, sub2api, resources)


@router.put("/editor/{sub2api_account_id}", response_model=AccountEditResult)
async def update_account_editor(
    payload: AccountEditUpdate,
    sub2api_account_id: int = Path(ge=1),
    _: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> AccountEditResult:
    sub2api, resources = await _load_resources(sub2api_account_id)
    try:
        assert_account_identity(sub2api, resources.account, payload.expected_identity_fingerprint)
        validate_account_edit_configuration(payload, resources)
        updated = await sub2api.update_account_configuration(
            sub2api_account_id,
            name=payload.name,
            concurrency=payload.concurrency,
            priority=payload.priority,
            rate_multiplier=payload.rate_multiplier,
            status=payload.status,
            schedulable=payload.schedulable,
            proxy_id=payload.proxy_id,
            group_ids=payload.group_ids,
            model_whitelist=payload.model_whitelist,
            extra_patch=account_extra_patch_from_configuration(payload, resources.account),
            validate_current=lambda current: assert_account_identity(
                sub2api,
                current,
                payload.expected_identity_fingerprint,
            ),
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except Sub2ApiRequestError as exc:
        raise _sub2api_http_error(exc) from exc

    await record_event(
        db,
        "account_configuration_updated",
        f"已更新 sub2api 账号 {payload.name} 的调度配置。",
        details={
            "sub2api_account_id": sub2api_account_id,
            "concurrency": payload.concurrency,
            "priority": payload.priority,
            "proxy_id": payload.proxy_id,
            "group_ids": payload.group_ids,
            "model_whitelist_count": len(payload.model_whitelist),
        },
    )
    editor = await _editor_out(db, sub2api, replace(resources, account=updated, checked_at=utcnow()))
    return AccountEditResult(message="账号配置已写入 sub2api 并完成回读校验。", editor=editor)


@router.get("/edit-presets", response_model=list[AccountEditPresetOut])
async def list_account_edit_presets(
    platform: str | None = Query(default=None, min_length=1, max_length=64),
    _: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> list[AccountEditPresetOut]:
    normalized_platform = platform.strip().lower() if platform else None
    return [AccountEditPresetOut.model_validate(item) for item in await _list_presets(db, normalized_platform)]


@router.post("/edit-presets", response_model=AccountEditPresetOut, status_code=status.HTTP_201_CREATED)
async def create_account_edit_preset(
    payload: AccountEditPresetCreate,
    _: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> AccountEditPresetOut:
    preset = AccountEditPreset(
        name=payload.name,
        platform=payload.platform.lower(),
        configuration=payload.configuration.model_dump(mode="json"),
    )
    db.add(preset)
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="同平台已存在同名模板。") from exc
    await db.refresh(preset)
    return AccountEditPresetOut.model_validate(preset)


@router.put("/edit-presets/{preset_id}", response_model=AccountEditPresetOut)
async def update_account_edit_preset(
    payload: AccountEditPresetUpdate,
    preset_id: int = Path(ge=1),
    _: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> AccountEditPresetOut:
    preset = await db.get(AccountEditPreset, preset_id)
    if preset is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="模板不存在。")
    preset.name = payload.name
    preset.configuration = payload.configuration.model_dump(mode="json")
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="同平台已存在同名模板。") from exc
    await db.refresh(preset)
    return AccountEditPresetOut.model_validate(preset)


@router.delete("/edit-presets/{preset_id}", response_model=MessageResponse)
async def delete_account_edit_preset(
    preset_id: int = Path(ge=1),
    _: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> MessageResponse:
    preset = await db.get(AccountEditPreset, preset_id)
    if preset is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="模板不存在。")
    await db.delete(preset)
    await db.commit()
    return MessageResponse(message="模板已删除。")


@router.post(
    "/edit-presets/{preset_id}/apply/{sub2api_account_id}",
    response_model=AccountEditResult,
)
async def apply_account_edit_preset(
    payload: AccountEditPresetApply,
    preset_id: int = Path(ge=1),
    sub2api_account_id: int = Path(ge=1),
    _: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> AccountEditResult:
    preset = await db.get(AccountEditPreset, preset_id)
    if preset is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="模板不存在。")
    preset_out = AccountEditPresetOut.model_validate(preset)
    sub2api, resources = await _load_resources(sub2api_account_id)
    platform = sub2api.account_platform(resources.account)
    if platform != preset.platform:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="模板平台与当前账号不匹配。")
    configuration = preset_out.configuration
    account_type = (sub2api.account_type(resources.account) or "").strip().lower()
    if configuration.account_type_scope and configuration.account_type_scope != account_type:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="模板账号类型与当前账号不匹配。")
    try:
        assert_account_identity(sub2api, resources.account, payload.expected_identity_fingerprint)
        validate_account_edit_configuration(configuration, resources, preset_name=preset.name)
        updated = await sub2api.update_account_configuration(
            sub2api_account_id,
            name=sub2api.account_name(resources.account) or f"account-{sub2api_account_id}",
            concurrency=configuration.concurrency,
            priority=configuration.priority,
            rate_multiplier=configuration.rate_multiplier,
            status=configuration.status,
            schedulable=configuration.schedulable,
            proxy_id=configuration.proxy_id,
            group_ids=configuration.group_ids,
            model_whitelist=configuration.model_whitelist,
            extra_patch=account_extra_patch_from_configuration(configuration, resources.account),
            validate_current=lambda current: assert_account_identity(
                sub2api,
                current,
                payload.expected_identity_fingerprint,
            ),
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except Sub2ApiRequestError as exc:
        raise _sub2api_http_error(exc) from exc

    await record_event(
        db,
        "account_edit_preset_applied",
        f"已将模板 {preset.name} 应用到 sub2api 账号 #{sub2api_account_id}。",
        details={"preset_id": preset.id, "sub2api_account_id": sub2api_account_id},
    )
    editor = await _editor_out(db, sub2api, replace(resources, account=updated, checked_at=utcnow()))
    return AccountEditResult(message=f"模板“{preset.name}”已通过有效性检查并应用。", editor=editor)
