from __future__ import annotations

from dataclasses import dataclass

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import EmailStr, TypeAdapter
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crypto import encrypt_text
from app.core.database import get_db
from app.core.security import require_admin
from app.models import MailboxCredential, utcnow
from app.schemas import MailboxCredentialOut, MailboxImportRequest, MailboxImportResult, MailMessageOut, MessageResponse
from app.services.mail import MailAdapterRegistry

router = APIRouter()

email_adapter = TypeAdapter(EmailStr)
PROVIDERS = {"outlook", "hotmail", "custom", "manual"}
OUTLOOK_DOMAINS = {
    "outlook.com",
    "outlook.com.cn",
    "live.com",
    "live.cn",
    "msn.com",
    "passport.com",
    "windowslive.com",
}


@dataclass
class ParsedMailbox:
    gpt_email: str
    mailbox_email: str
    password: str | None
    client_id: str | None
    refresh_token: str | None
    provider: str
    custom_fetch_url: str | None = None
    access_token: str | None = None


@router.get("", response_model=list[MailboxCredentialOut])
async def list_mailboxes(
    _: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> list[MailboxCredentialOut]:
    result = await db.execute(select(MailboxCredential).order_by(desc(MailboxCredential.updated_at)))
    return list(result.scalars().all())


@router.post("/import", response_model=MailboxImportResult)
async def import_mailboxes(
    payload: MailboxImportRequest,
    _: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> MailboxImportResult:
    parsed, invalid_lines = _parse_import_with_report(payload.content, payload.default_provider)
    if not parsed:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No valid mailbox rows were found.")

    imported = 0
    for item in parsed:
        existing = await db.scalar(select(MailboxCredential).where(MailboxCredential.gpt_email == item.gpt_email))
        if existing is None:
            existing = MailboxCredential(gpt_email=item.gpt_email, mailbox_email=item.mailbox_email)
            db.add(existing)
        existing.mailbox_email = item.mailbox_email
        existing.provider = item.provider
        existing.encrypted_password = encrypt_text(item.password)
        existing.encrypted_client_id = encrypt_text(item.client_id)
        existing.encrypted_refresh_token = encrypt_text(item.refresh_token)
        existing.encrypted_access_token = encrypt_text(item.access_token)
        existing.custom_fetch_url = item.custom_fetch_url
        existing.disabled = False
        existing.last_error = None
        imported += 1
    await db.commit()
    skipped = len(invalid_lines)
    message = f"Imported {imported} mailbox credential(s)."
    if skipped:
        lines = ", ".join(str(line) for line in invalid_lines[:10])
        suffix = "..." if skipped > 10 else ""
        message = f"{message} Skipped {skipped} invalid line(s): {lines}{suffix}."
    return MailboxImportResult(message=message, imported=imported, skipped=skipped, invalid_lines=invalid_lines)


@router.get("/{credential_id}/messages", response_model=list[MailMessageOut])
async def list_mailbox_messages(
    credential_id: int,
    folder: str = Query(default="inbox", pattern="^(inbox|junk)$"),
    limit: int = Query(default=20, ge=1, le=50),
    _: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> list[MailMessageOut]:
    credential = await db.get(MailboxCredential, credential_id)
    if credential is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Mailbox credential not found.")
    if credential.provider not in {"outlook", "hotmail"} and folder == "junk":
        return []

    db.expunge(credential)
    result = await MailAdapterRegistry().fetch_messages(credential, folder, limit)
    if result.status == "failed":
        stored = await db.get(MailboxCredential, credential_id)
        if stored is not None:
            stored.last_error = result.error or "Failed to read mailbox."
            await db.commit()
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=result.error or "Failed to read mailbox.")
    stored = await db.get(MailboxCredential, credential_id)
    if stored is not None:
        stored.last_error = None
        stored.last_success_at = utcnow()
        if result.new_refresh_token:
            stored.encrypted_refresh_token = encrypt_text(result.new_refresh_token)
        if result.new_access_token:
            stored.encrypted_access_token = encrypt_text(result.new_access_token)
        await db.commit()
    return [
        MailMessageOut(
            id=message.id,
            folder="junk" if message.folder == "junk" else "inbox",
            subject=message.subject,
            sender_name=message.sender_name,
            sender_address=message.sender_address,
            body_preview=message.body_preview,
            received_at=message.received_at,
        )
        for message in result.messages
    ]


@router.delete("/{credential_id}", response_model=MessageResponse)
async def delete_mailbox(
    credential_id: int,
    _: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> MessageResponse:
    credential = await db.get(MailboxCredential, credential_id)
    if credential is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Mailbox credential not found.")
    await db.delete(credential)
    await db.commit()
    return MessageResponse(message="Mailbox credential deleted.")


def _parse_import(content: str, default_provider: str) -> list[ParsedMailbox]:
    parsed, _ = _parse_import_with_report(content, default_provider)
    return parsed


def _parse_import_with_report(content: str, default_provider: str) -> tuple[list[ParsedMailbox], list[int]]:
    items: list[ParsedMailbox] = []
    invalid_lines: list[int] = []
    for line_number, raw_line in enumerate(content.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        parts = _split_line(line)
        parsed = _parse_parts(parts, default_provider)
        if parsed:
            items.append(parsed)
        else:
            invalid_lines.append(line_number)
    return items, invalid_lines


def _split_line(line: str) -> list[str]:
    if "----" in line:
        return [part.strip() for part in line.split("----")]
    if "|" in line:
        return [part.strip() for part in line.split("|")]
    return [part.strip() for part in line.split(",")]


def _parse_parts(parts: list[str], default_provider: str) -> ParsedMailbox | None:
    try:
        core, explicit_provider, custom_url = _split_provider_tail(parts)
        if len(core) == 4:
            mailbox = _email(core[0])
            provider = _resolve_provider(mailbox, default_provider, explicit_provider)
            if not provider or (provider == "custom" and not custom_url):
                return None
            return ParsedMailbox(mailbox, mailbox, core[1], core[2], core[3], provider, custom_url)
        if len(core) == 5:
            gpt_email = _email(core[0])
            mailbox_email = _email(core[1])
            provider = _resolve_provider(mailbox_email, default_provider, explicit_provider)
            if not provider or (provider == "custom" and not custom_url):
                return None
            return ParsedMailbox(gpt_email, mailbox_email, core[2], core[3], core[4], provider, custom_url)
        if len(core) == 6:
            maybe_mailbox_email = _maybe_email(core[1])
            if maybe_mailbox_email:
                gpt_email = _email(core[0])
                mailbox = maybe_mailbox_email
                password = core[2]
                client_id = core[3]
                refresh_token = core[4]
                access_token = core[5]
            else:
                gpt_email = _email(core[0])
                mailbox = gpt_email
                password = core[2]
                client_id = core[3]
                refresh_token = core[4]
                access_token = core[5]
            provider = _resolve_provider(mailbox, default_provider, explicit_provider)
            if not provider or (provider == "custom" and not custom_url):
                return None
            return ParsedMailbox(gpt_email, mailbox, password, client_id, refresh_token, provider, custom_url, access_token)
    except ValueError:
        return None
    return None


def _split_provider_tail(parts: list[str]) -> tuple[list[str], str | None, str | None]:
    if len(parts) >= 6 and _is_provider(parts[-2]):
        return parts[:-2], parts[-2].lower(), parts[-1] or None
    if len(parts) >= 5 and _is_provider(parts[-1]):
        return parts[:-1], parts[-1].lower(), None
    return parts, None, None


def _is_provider(value: str) -> bool:
    return value.lower() in PROVIDERS


def _resolve_provider(mailbox_email: str, default_provider: str, explicit_provider: str | None) -> str | None:
    if explicit_provider:
        return explicit_provider
    default = default_provider.lower()
    if default != "auto":
        return default if default in PROVIDERS else None
    return _detect_provider(mailbox_email)


def _detect_provider(mailbox_email: str) -> str | None:
    domain = mailbox_email.rsplit("@", 1)[-1].lower()
    if domain.startswith("hotmail."):
        return "hotmail"
    if domain in OUTLOOK_DOMAINS:
        return "outlook"
    return None


def _email(value: str) -> str:
    return str(email_adapter.validate_python(value)).lower()


def _maybe_email(value: str) -> str | None:
    try:
        return _email(value)
    except ValueError:
        return None
