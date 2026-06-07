from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import delete, desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.database import get_db
from app.core.security import require_admin
from app.models import PhoneAccountBinding, PhoneNumber
from app.schemas import MessageResponse, PhoneBindingUpdate, PhoneImportRequest, PhoneImportResult, PhoneNumberOut
from app.services.phone_numbers import (
    build_phone_export_content,
    find_reconcilable_placeholder,
    normalize_account_emails,
    parse_phone_import,
    refresh_phone_sms_status,
)


router = APIRouter()


@router.get("", response_model=list[PhoneNumberOut])
async def list_phones(
    _: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> list[PhoneNumberOut]:
    phones = await _load_phones(db)
    account_map = await _phone_account_map(db)
    return [
        PhoneNumberOut(
            id=phone.id,
            phone_number=phone.phone_number,
            sms_url=phone.sms_url,
            sms_cdk=phone.sms_cdk,
            sms_recharge_url=phone.sms_recharge_url,
            account_emails=account_map.get(phone.id, []),
            bindings_count=len(account_map.get(phone.id, [])),
            sms_status=phone.sms_status,
            sms_error=phone.sms_error,
            sms_checked_at=phone.sms_checked_at,
            created_at=phone.created_at,
            updated_at=phone.updated_at,
        )
        for phone in phones
    ]


@router.get("/export", response_model=MessageResponse)
async def export_phones(
    _: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> MessageResponse:
    phones = await _load_phones(db)
    account_map = await _phone_account_map(db)
    return MessageResponse(message=build_phone_export_content(phones, account_map))


@router.post("/import", response_model=PhoneImportResult)
async def import_phones(
    payload: PhoneImportRequest,
    _: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> PhoneImportResult:
    parsed, invalid_lines = parse_phone_import(payload.content)
    if not parsed:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No valid phone rows were found.")

    phone_keys = [phone_key for phone_key, _, _, _, _ in parsed]
    existing_result = await db.execute(select(PhoneNumber).where(PhoneNumber.phone_key.in_(phone_keys)))
    existing_by_key = {item.phone_key: item for item in existing_result.scalars().all()}
    seen_import_keys: set[str] = set()
    imported = 0
    updated = 0
    duplicate_skipped = 0

    for phone_key, phone_number, sms_url, sms_cdk, sms_recharge_url in parsed:
        if phone_key in seen_import_keys:
            duplicate_skipped += 1
            continue
        seen_import_keys.add(phone_key)
        existing = existing_by_key.get(phone_key)
        if existing is None:
            placeholder = await find_reconcilable_placeholder(db, phone_number)
            if placeholder is not None:
                changed = False
                if placeholder.phone_key != phone_key:
                    placeholder.phone_key = phone_key
                    changed = True
                if placeholder.phone_number != phone_number:
                    placeholder.phone_number = phone_number
                    changed = True
                if placeholder.sms_url != sms_url:
                    placeholder.sms_url = sms_url
                    changed = True
                if placeholder.sms_cdk != sms_cdk:
                    placeholder.sms_cdk = sms_cdk
                    changed = True
                if placeholder.sms_recharge_url != sms_recharge_url:
                    placeholder.sms_recharge_url = sms_recharge_url
                    changed = True
                if changed:
                    updated += 1
                existing_by_key[phone_key] = placeholder
                continue
            db.add(
                PhoneNumber(
                    phone_key=phone_key,
                    phone_number=phone_number,
                    sms_url=sms_url,
                    sms_cdk=sms_cdk,
                    sms_recharge_url=sms_recharge_url,
                )
            )
            imported += 1
            continue
        changed = False
        if existing.phone_number != phone_number:
            existing.phone_number = phone_number
            changed = True
        if existing.sms_url != sms_url:
            existing.sms_url = sms_url
            changed = True
        if existing.sms_cdk != sms_cdk:
            existing.sms_cdk = sms_cdk
            changed = True
        if existing.sms_recharge_url != sms_recharge_url:
            existing.sms_recharge_url = sms_recharge_url
            changed = True
        if changed:
            updated += 1

    await db.commit()
    skipped = len(invalid_lines) + duplicate_skipped
    message = f"Imported {imported} phone number(s)."
    if updated:
        message = f"{message} Updated {updated} existing phone number(s)."
    if duplicate_skipped:
        message = f"{message} Skipped {duplicate_skipped} duplicate import row(s)."
    if invalid_lines:
        lines = ", ".join(str(line) for line in invalid_lines[:10])
        suffix = "..." if len(invalid_lines) > 10 else ""
        message = f"{message} Skipped {len(invalid_lines)} invalid line(s): {lines}{suffix}."
    return PhoneImportResult(
        message=message,
        imported=imported,
        updated=updated,
        skipped=skipped,
        invalid_lines=invalid_lines,
    )


@router.post("/status-refresh", response_model=MessageResponse)
async def refresh_phone_statuses(
    _: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> MessageResponse:
    phones = await _load_phones(db)
    settings = get_settings()
    checked = 0
    for phone in phones:
        await refresh_phone_sms_status(db, phone, settings)
        checked += 1
    await db.commit()
    return MessageResponse(message=f"Checked {checked} phone SMS link(s).")


@router.put("/{phone_id}/bindings", response_model=MessageResponse)
async def update_phone_bindings(
    phone_id: int,
    payload: PhoneBindingUpdate,
    _: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> MessageResponse:
    phone = await db.get(PhoneNumber, phone_id)
    if phone is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Phone number not found.")
    account_emails = normalize_account_emails([str(value) for value in payload.account_emails])
    if len(account_emails) > 3:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="A phone number can be bound to at most 3 accounts.")

    await db.execute(delete(PhoneAccountBinding).where(PhoneAccountBinding.account_email.in_(account_emails)))
    await db.execute(delete(PhoneAccountBinding).where(PhoneAccountBinding.phone_id == phone_id))
    for email in account_emails:
        db.add(PhoneAccountBinding(phone_id=phone_id, account_email=email))
    await db.commit()
    return MessageResponse(message=f"Updated {len(account_emails)} phone binding(s) for {phone.phone_number}.")


@router.delete("/{phone_id}", response_model=MessageResponse)
async def delete_phone(
    phone_id: int,
    _: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> MessageResponse:
    phone = await db.get(PhoneNumber, phone_id)
    if phone is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Phone number not found.")
    await db.delete(phone)
    await db.commit()
    return MessageResponse(message="Phone number deleted.")


async def _load_phones(db: AsyncSession) -> list[PhoneNumber]:
    result = await db.execute(select(PhoneNumber).order_by(desc(PhoneNumber.updated_at), desc(PhoneNumber.id)))
    return list(result.scalars().all())


async def _phone_account_map(db: AsyncSession) -> dict[int, list[str]]:
    result = await db.execute(select(PhoneAccountBinding.phone_id, PhoneAccountBinding.account_email))
    account_map: dict[int, list[str]] = {}
    for phone_id, account_email in result.all():
        account_map.setdefault(int(phone_id), []).append(str(account_email))
    for phone_id in account_map:
        account_map[phone_id].sort()
    return account_map
