from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

import httpx
from pydantic import EmailStr, TypeAdapter
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.models import PhoneAccountBinding, PhoneNumber
from app.services.mail import extract_code


PHONE_CODE_RE = re.compile(r"\b(\d{6})\b")
MASK_CHARS = "*xX#?•"
MISSING_SMS_URL = "接码链接不存在"
CDK_SMS_URL_PREFIX = "cdk:"
email_adapter = TypeAdapter(EmailStr)


@dataclass(frozen=True)
class BoundPhone:
    phone_number: str
    sms_url: str
    phone_id: int | None = None
    sms_cdk: str | None = None
    sms_recharge_url: str | None = None


@dataclass(frozen=True)
class ParsedPhoneImportRow:
    phone_key: str
    phone_number: str
    sms_url: str
    sms_cdk: str | None = None
    sms_recharge_url: str | None = None


class OAuthPhoneResolutionError(RuntimeError):
    pass


@dataclass
class SmsFetchResult:
    status: str
    code: str | None = None
    error: str | None = None
    provider_status: str | None = None
    snapshot_key: str | None = None
    has_timestamp: bool = False
    timestamp_text: str | None = None


def has_working_sms_url(value: str) -> bool:
    text = str(value or "").strip()
    parsed = urlparse(text)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def is_manual_phone_source(value: str) -> bool:
    text = str(value or "").strip()
    return text.lower().startswith(CDK_SMS_URL_PREFIX)


def describe_phone_source(value: str) -> str:
    text = str(value or "").strip()
    if is_manual_phone_source(text):
        return text[len(CDK_SMS_URL_PREFIX) :].strip() or text
    return text


def normalize_sms_cdk(value: str | None) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    if is_manual_phone_source(text):
        text = text[len(CDK_SMS_URL_PREFIX) :].strip()
    return text or None


def validate_sms_cdk(value: str) -> str:
    text = normalize_sms_cdk(value)
    if not text or not re.fullmatch(r"[A-Za-z0-9_-]{6,}", text):
        raise ValueError("SMS CDK is invalid.")
    return text


def is_masked_phone_number(value: str) -> bool:
    text = str(value or "")
    return any(marker in text for marker in MASK_CHARS)


def phone_matches_hint(phone_number: str, hint: str) -> bool:
    prefix, suffix, visible = _phone_hint_parts(hint)
    if len(visible) < 4:
        return False
    for digits in _phone_digit_variants(phone_number):
        if prefix and not digits.startswith(prefix):
            continue
        if suffix and not digits.endswith(suffix):
            continue
        if not prefix and not suffix:
            continue
        return True
    return False


async def ensure_bound_phone_for_oauth(db: AsyncSession, email: str, page_phone_hint: str | None) -> BoundPhone | None:
    normalized = str(email or "").strip().lower()
    if not normalized:
        return None

    bound_row = await _bound_phone_row_by_email(db, normalized)
    if bound_row is not None and (not page_phone_hint or phone_matches_hint(bound_row.phone_number, page_phone_hint)):
        return _bound_phone_from_row(bound_row)

    matched_row = await find_phone_by_hint(db, page_phone_hint) if page_phone_hint else None
    if matched_row is not None:
        await bind_phone_to_account(db, matched_row.id, normalized)
        return _bound_phone_from_row(matched_row)

    if page_phone_hint:
        placeholder = await get_or_create_phone_placeholder(db, page_phone_hint)
        await bind_phone_to_account(db, placeholder.id, normalized)
        return _bound_phone_from_row(placeholder)

    if bound_row is not None and page_phone_hint is None:
        return _bound_phone_from_row(bound_row)

    return None


async def require_oauth_phone_match(db: AsyncSession, email: str, page_phone_hint: str | None) -> BoundPhone:
    normalized = str(email or "").strip().lower()
    hint = _clean_phone_hint(page_phone_hint)
    if not normalized:
        raise OAuthPhoneResolutionError("OAuth 账号邮箱为空，无法匹配手机号。")
    if not hint:
        raise OAuthPhoneResolutionError("OpenAI 前端未显示可识别手机号，已停止自动 OAuth。")

    bound_row = await _bound_phone_row_by_email(db, normalized)
    if bound_row is not None and phone_matches_hint(bound_row.phone_number, hint):
        return _bound_phone_from_row(bound_row)

    matched_row = await find_phone_by_hint(db, hint)
    if matched_row is None:
        matched_row = await get_or_create_phone_placeholder(db, hint)

    await bind_phone_to_account(db, matched_row.id, normalized)
    await db.flush()
    return _bound_phone_from_row(matched_row)


async def find_phone_by_hint(db: AsyncSession, hint: str | None) -> PhoneNumber | None:
    if not hint:
        return None
    result = await db.execute(select(PhoneNumber).order_by(PhoneNumber.updated_at.desc(), PhoneNumber.id.desc()))
    rows = list(result.scalars().all())
    matches = [row for row in rows if phone_matches_hint(row.phone_number, hint)]
    if not matches:
        return None

    active_matches = [row for row in matches if has_working_sms_url(row.sms_url)]
    if len(active_matches) == 1:
        return active_matches[0]
    if len(matches) == 1:
        return matches[0]

    best_score = max(_phone_match_score(row.phone_number, hint) for row in matches)
    best_matches = [row for row in matches if _phone_match_score(row.phone_number, hint) == best_score]
    if len(best_matches) == 1:
        return best_matches[0]
    return None


async def find_reconcilable_placeholder(db: AsyncSession, phone_number: str) -> PhoneNumber | None:
    result = await db.execute(select(PhoneNumber).order_by(PhoneNumber.updated_at.desc(), PhoneNumber.id.desc()))
    placeholders = [
        row
        for row in result.scalars().all()
        if (is_masked_phone_number(row.phone_number) or not has_working_sms_url(row.sms_url))
        and phone_matches_hint(phone_number, row.phone_number)
    ]
    if len(placeholders) == 1:
        return placeholders[0]
    return None


async def bind_phone_to_account(db: AsyncSession, phone_id: int, email: str) -> None:
    normalized = str(email or "").strip().lower()
    if not normalized:
        return
    await db.execute(delete(PhoneAccountBinding).where(PhoneAccountBinding.account_email == normalized))
    await db.flush()
    db.add(PhoneAccountBinding(phone_id=phone_id, account_email=normalized))


async def get_or_create_phone_placeholder(db: AsyncSession, page_phone_hint: str) -> PhoneNumber:
    display = _clean_phone_hint(page_phone_hint)
    if not display:
        raise ValueError("Phone hint is empty.")
    if is_masked_phone_number(display):
        phone_key = _masked_phone_key(display)
    else:
        try:
            phone_key, display = normalize_phone_number(display)
        except ValueError:
            phone_key = _masked_phone_key(display)
    existing = await db.scalar(select(PhoneNumber).where(PhoneNumber.phone_key == phone_key))
    if existing is not None:
        if existing.phone_number != display:
            existing.phone_number = display
        if has_working_sms_url(existing.sms_url):
            return existing
        existing.sms_url = MISSING_SMS_URL
        return existing

    placeholder = PhoneNumber(phone_key=phone_key, phone_number=display, sms_url=MISSING_SMS_URL)
    db.add(placeholder)
    await db.flush()
    return placeholder


def normalize_phone_number(value: str) -> tuple[str, str]:
    text = str(value or "").strip()
    if not text:
        raise ValueError("Phone number is empty.")
    has_plus = text.startswith("+")
    digits = re.sub(r"\D", "", text)
    if len(digits) < 6:
        raise ValueError("Phone number must contain at least 6 digits.")
    display = f"+{digits}" if has_plus else digits
    return digits, display


def validate_sms_url(value: str) -> str:
    text = str(value or "").strip()
    matched_url = re.search(r"https?://\S+", text, re.I)
    if matched_url:
        text = matched_url.group(0).rstrip(",，;；)）]\"'")
    parsed = urlparse(text)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("SMS URL must be a valid http/https URL.")
    return text


def validate_sms_source(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError("SMS source is empty.")
    if re.fullmatch(r"[A-Za-z0-9_-]{6,}", text):
        return f"{CDK_SMS_URL_PREFIX}{text}"
    return validate_sms_url(text)


def parse_phone_import(content: str) -> tuple[list[tuple[str, str, str, str | None, str | None]], list[int]]:
    items: list[tuple[str, str, str, str | None, str | None]] = []
    invalid_lines: list[int] = []
    for line_number, raw_line in enumerate(content.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        parsed = _parse_phone_line(line)
        if parsed is None:
            invalid_lines.append(line_number)
            continue
        items.append((parsed.phone_key, parsed.phone_number, parsed.sms_url, parsed.sms_cdk, parsed.sms_recharge_url))
    return items, invalid_lines


async def get_bound_phone_by_email(db: AsyncSession, email: str) -> BoundPhone | None:
    normalized = str(email or "").strip().lower()
    if not normalized:
        return None
    row = await _bound_phone_row_by_email(db, normalized)
    if row is None:
        return None
    return _bound_phone_from_row(row)


async def fetch_sms_code(phone: BoundPhone, settings: Settings | None = None) -> SmsFetchResult:
    if phone.sms_cdk or is_manual_phone_source(phone.sms_url):
        source = phone.sms_cdk or describe_phone_source(phone.sms_url)
        return SmsFetchResult(status="manual", error=f"Phone source requires manual verification: {source}.")
    if not has_working_sms_url(phone.sms_url):
        return SmsFetchResult(status="not_found", error="SMS URL is unavailable.")
    active_settings = settings or get_settings()
    try:
        async with httpx.AsyncClient(timeout=active_settings.mail_read_timeout_seconds, trust_env=False) as client:
            response = await client.get(phone.sms_url)
    except httpx.HTTPError as exc:
        return SmsFetchResult(status="failed", error=f"SMS endpoint failed: {exc}")

    body = response.text
    try:
        payload = response.json()
    except ValueError:
        payload = None
    if response.status_code >= 400:
        return SmsFetchResult(
            status="failed",
            error=f"SMS endpoint returned HTTP {response.status_code}.",
            provider_status=body[:500],
        )

    code = _extract_sms_code_from_response(body) or _extract_sms_code_from_payload(payload)
    provider_status = body[:500]
    if payload is not None and len(provider_status) < 40:
        provider_status = str(payload)[:500]
    timestamp = _extract_sms_timestamp(payload)
    snapshot_key = _sms_snapshot_key(body, payload, code, timestamp)
    if code:
        return SmsFetchResult(
            status="ok",
            code=code,
            provider_status=provider_status,
            snapshot_key=snapshot_key,
            has_timestamp=bool(timestamp),
            timestamp_text=timestamp,
        )
    endpoint_error = _sms_endpoint_error(provider_status)
    if endpoint_error:
        return SmsFetchResult(
            status="failed",
            error=endpoint_error,
            provider_status=provider_status,
            snapshot_key=snapshot_key,
            has_timestamp=bool(timestamp),
            timestamp_text=timestamp,
        )
    return SmsFetchResult(
        status="not_found",
        error="No fresh SMS verification code was found.",
        provider_status=provider_status,
        snapshot_key=snapshot_key,
        has_timestamp=bool(timestamp),
        timestamp_text=timestamp,
    )


def is_sms_fetch_result_usable(result: SmsFetchResult) -> bool:
    return result.status in {"ok", "not_found"} and not _sms_endpoint_error(result.provider_status)


async def record_phone_sms_fetch_status(db: AsyncSession, phone_id: int | None, result: SmsFetchResult) -> None:
    if phone_id is None:
        return
    phone = await db.get(PhoneNumber, phone_id)
    if phone is None:
        return
    phone.sms_status = result.status
    phone.sms_error = result.error
    phone.sms_checked_at = datetime.now(timezone.utc)


async def refresh_phone_sms_status(db: AsyncSession, phone: PhoneNumber, settings: Settings | None = None) -> SmsFetchResult:
    result = await fetch_sms_code(
        _bound_phone_from_row(phone),
        settings,
    )
    phone.sms_status = result.status
    phone.sms_error = result.error
    phone.sms_checked_at = datetime.now(timezone.utc)
    return result


async def select_best_phone_for_oauth(
    db: AsyncSession,
    email: str,
    settings: Settings | None = None,
    page_phone_hint: str | None = None,
    exclude_phone_ids: set[int] | None = None,
) -> BoundPhone | None:
    normalized = str(email or "").strip().lower()
    if not normalized:
        return None
    excluded = exclude_phone_ids or set()

    result = await db.execute(select(PhoneNumber).order_by(PhoneNumber.updated_at.desc(), PhoneNumber.id.desc()))
    rows = list(result.scalars().all())
    binding_result = await db.execute(select(PhoneAccountBinding.phone_id, PhoneAccountBinding.account_email))
    bindings: dict[int, list[str]] = {}
    for phone_id, account_email in binding_result.all():
        bindings.setdefault(int(phone_id), []).append(str(account_email).lower())

    bound_row = next((row for row in rows if normalized in bindings.get(int(row.id), [])), None)
    matched_row = None
    if page_phone_hint:
        matches = [row for row in rows if phone_matches_hint(row.phone_number, page_phone_hint)]
        if matches:
            active_matches = [row for row in matches if has_working_sms_url(row.sms_url)]
            matched_pool = active_matches or matches
            matched_pool.sort(key=lambda row: (len(bindings.get(int(row.id), [])), -int(row.id)))
            matched_row = matched_pool[0]

    candidate_rows: list[PhoneNumber] = []
    for row in (bound_row, matched_row):
        if row is not None and row.id not in {item.id for item in candidate_rows}:
            candidate_rows.append(row)

    remaining = [row for row in rows if row.id not in {item.id for item in candidate_rows}]
    remaining.sort(key=lambda row: (len(bindings.get(int(row.id), [])), -(row.updated_at.timestamp() if row.updated_at else 0), -int(row.id)))
    candidate_rows.extend(remaining)

    active_settings = settings or get_settings()
    for row in candidate_rows:
        row_id = int(row.id)
        if row_id in excluded:
            continue
        row_bindings = bindings.get(row_id, [])
        if len(row_bindings) >= 3 and normalized not in row_bindings:
            continue
        if not has_working_sms_url(row.sms_url):
            row.sms_status = "not_found"
            row.sms_error = "SMS URL is unavailable."
            row.sms_checked_at = datetime.now(timezone.utc)
            continue
        result = await refresh_phone_sms_status(db, row, active_settings)
        if not is_sms_fetch_result_usable(result):
            continue
        if normalized not in row_bindings:
            await bind_phone_to_account(db, row_id, normalized)
            await db.flush()
        return _bound_phone_from_row(row)
    return None


def normalize_account_emails(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        email = str(email_adapter.validate_python(value)).lower()
        if email in seen:
            continue
        seen.add(email)
        result.append(email)
    return result


def build_phone_export_content(phones: list[PhoneNumber], account_map: dict[int, list[str]]) -> str:
    lines: list[str] = []
    for phone in phones:
        if phone.sms_cdk:
            parts = [phone.phone_number, phone.sms_cdk]
            if phone.sms_recharge_url:
                parts.append(phone.sms_recharge_url)
            lines.append("----".join(parts))
            continue
        lines.append(f"{phone.phone_number}----{describe_phone_source(phone.sms_url)}")
    return "\n".join(lines)


def _parse_phone_line(line: str) -> ParsedPhoneImportRow | None:
    separator = next((item for item in ("----", "|", ",") if item in line), None)
    if separator is None:
        return None
    parts = [part.strip() for part in line.split(separator)]
    if len(parts) < 2:
        return None
    try:
        phone_key, phone_number = normalize_phone_number(parts[0])
        if len(parts) >= 3:
            sms_cdk = validate_sms_cdk(parts[1])
            sms_recharge_url = validate_sms_url(parts[2])
            return ParsedPhoneImportRow(
                phone_key=phone_key,
                phone_number=phone_number,
                sms_url=MISSING_SMS_URL,
                sms_cdk=sms_cdk,
                sms_recharge_url=sms_recharge_url,
            )
        sms_url = validate_sms_source(parts[1])
    except ValueError:
        return None
    sms_cdk = normalize_sms_cdk(sms_url) if is_manual_phone_source(sms_url) else None
    return ParsedPhoneImportRow(
        phone_key=phone_key,
        phone_number=phone_number,
        sms_url=MISSING_SMS_URL if sms_cdk else sms_url,
        sms_cdk=sms_cdk,
        sms_recharge_url=None,
    )


def _bound_phone_or_error(row: PhoneNumber) -> BoundPhone:
    sms_source = str(row.sms_url or "").strip()
    phone_number = str(row.phone_number)
    if row.sms_cdk or is_manual_phone_source(sms_source):
        source = row.sms_cdk or describe_phone_source(sms_source)
        raise OAuthPhoneResolutionError(
            f"OpenAI 前端手机号 {phone_number} 对应的是 CDK/手动接码源 {source}，已停止自动 OAuth，请手动完成验证。"
        )
    if not has_working_sms_url(sms_source):
        raise OAuthPhoneResolutionError(
            f"OpenAI 前端手机号 {phone_number} 在库中存在，但接码链接不存在或不可用，已停止自动 OAuth。"
        )
    return _bound_phone_from_row(row)


def extract_phone_row_from_note(note_text: str) -> ParsedPhoneImportRow | None:
    parsed, _ = parse_phone_import(note_text)
    if len(parsed) == 1:
        phone_key, phone_number, sms_url, sms_cdk, sms_recharge_url = parsed[0]
        return ParsedPhoneImportRow(
            phone_key=phone_key,
            phone_number=phone_number,
            sms_url=sms_url,
            sms_cdk=sms_cdk,
            sms_recharge_url=sms_recharge_url,
        )

    phone_match = re.search(r"(\+?\d[\d\s().\-]{5,}\d)", note_text)
    if not phone_match:
        return None
    try:
        phone_key, phone_number = normalize_phone_number(phone_match.group(1))
    except ValueError:
        return None
    cdk_match = re.search(r"(?i)(?:cdk|code|接码(?:码)?|兑换码)\s*[:：=]?\s*([A-Za-z0-9_-]{6,})", note_text)
    url_match = re.search(r"https?://\S+", note_text)
    if cdk_match:
        return ParsedPhoneImportRow(
            phone_key=phone_key,
            phone_number=phone_number,
            sms_url=MISSING_SMS_URL,
            sms_cdk=validate_sms_cdk(cdk_match.group(1)),
            sms_recharge_url=validate_sms_url(url_match.group(0)) if url_match else None,
        )
    if url_match:
        return ParsedPhoneImportRow(
            phone_key=phone_key,
            phone_number=phone_number,
            sms_url=validate_sms_url(url_match.group(0)),
            sms_cdk=None,
            sms_recharge_url=None,
        )
    return None


def note_marker(value: str) -> str:
    text = str(value or "").strip()
    return hashlib.sha1(text.encode("utf-8", errors="ignore")).hexdigest()[:20] if text else ""


def _bound_phone_from_row(row: PhoneNumber) -> BoundPhone:
    return BoundPhone(
        phone_id=int(row.id),
        phone_number=str(row.phone_number),
        sms_url=str(row.sms_url or "").strip(),
        sms_cdk=normalize_sms_cdk(row.sms_cdk),
        sms_recharge_url=str(row.sms_recharge_url or "").strip() or None,
    )


def _extract_sms_code_from_response(text: str) -> str | None:
    return extract_code(text)


def _extract_sms_code_from_payload(payload: Any) -> str | None:
    if isinstance(payload, dict):
        for key in ("code", "verification_code", "otp", "captcha", "message", "msg", "sms", "content", "data"):
            code = _extract_sms_code_from_payload(payload.get(key))
            if code:
                return code
        return None
    if isinstance(payload, list):
        for item in payload:
            code = _extract_sms_code_from_payload(item)
            if code:
                return code
        return None
    if isinstance(payload, str):
        return extract_code(payload)
    if payload is None:
        return None
    return extract_code(str(payload))


def _sms_endpoint_error(value: str | None) -> str | None:
    text = str(value or "").strip()
    lowered = text.lower()
    if not text:
        return None
    if "已过期" in text or "expired" in lowered:
        return "SMS URL has expired."
    if "不存在" in text or "not found" in lowered:
        return "SMS URL is no longer valid."
    if "失效" in text or "invalid" in lowered and "code" not in lowered:
        return "SMS URL is invalid."
    return None


def _sms_snapshot_key(body: str, payload: Any, code: str | None, timestamp: str | None) -> str | None:
    if timestamp and code:
        return f"{code}|{timestamp}"
    if timestamp:
        return timestamp
    if code:
        body_fingerprint = hashlib.sha1(body[:500].encode("utf-8", errors="ignore")).hexdigest()[:12]
        return f"{code}|{body_fingerprint}"
    if payload is not None:
        try:
            encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
        except (TypeError, ValueError):
            encoded = str(payload)
        return hashlib.sha1(encoded[:1000].encode("utf-8", errors="ignore")).hexdigest()[:20]
    text = body.strip()
    if not text:
        return None
    return hashlib.sha1(text[:1000].encode("utf-8", errors="ignore")).hexdigest()[:20]


def _extract_sms_timestamp(payload: Any) -> str | None:
    if isinstance(payload, dict):
        for key in ("sms_time", "received_at", "created_at", "updated_at", "time", "timestamp", "date"):
            value = payload.get(key)
            if value not in (None, ""):
                return str(value).strip() or None
        for value in payload.values():
            timestamp = _extract_sms_timestamp(value)
            if timestamp:
                return timestamp
        return None
    if isinstance(payload, list):
        for item in payload:
            timestamp = _extract_sms_timestamp(item)
            if timestamp:
                return timestamp
        return None
    return None


async def _bound_phone_row_by_email(db: AsyncSession, email: str) -> PhoneNumber | None:
    result = await db.execute(
        select(PhoneNumber)
        .join(PhoneAccountBinding, PhoneAccountBinding.phone_id == PhoneNumber.id)
        .where(PhoneAccountBinding.account_email == email)
        .limit(1)
    )
    return result.scalar_one_or_none()


def _phone_match_score(phone_number: str, hint: str) -> int:
    prefix, suffix, visible = _phone_hint_parts(hint)
    if len(visible) < 4:
        return 0
    actual_digits = max(_phone_digit_variants(phone_number), key=len, default="")
    if not actual_digits:
        return 0
    exact_bonus = 1000 if re.sub(r"\D", "", hint) and not is_masked_phone_number(hint) and actual_digits == re.sub(r"\D", "", hint) else 0
    return exact_bonus + (len(prefix) * 100) + len(suffix)


def _phone_hint_parts(value: str | None) -> tuple[str, str, str]:
    text = _clean_phone_hint(value)
    if not text:
        return "", "", ""
    mask_indexes = [index for index, char in enumerate(text) if char in MASK_CHARS]
    if not mask_indexes:
        digits = re.sub(r"\D", "", text)
        return digits, digits, digits
    prefix = re.sub(r"\D", "", text[: mask_indexes[0]])
    suffix = re.sub(r"\D", "", text[mask_indexes[-1] + 1 :])
    return prefix, suffix, prefix + suffix


def _phone_digit_variants(value: str) -> set[str]:
    digits = re.sub(r"\D", "", str(value or ""))
    if not digits:
        return set()
    variants = {digits}
    if len(digits) == 13 and digits.startswith("86"):
        variants.add(digits[2:])
    if len(digits) == 11 and digits.startswith("1"):
        variants.add(f"86{digits}")
    return variants


def _clean_phone_hint(value: str | None) -> str:
    text = str(value or "").strip()
    text = re.sub(r"\s+", "", text)
    return text[:32]


def _masked_phone_key(value: str) -> str:
    digest = hashlib.sha1(value.encode("utf-8")).hexdigest()[:25]
    return f"masked-{digest}"
