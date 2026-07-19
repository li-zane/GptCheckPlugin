from __future__ import annotations

import base64
import calendar
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from app.core.config import Settings, get_settings
from app.core.subscription_types import normalize_subscription_type
from app.services.sub2api import looks_deactive_text


ACCOUNT_CHECK_PATH = "/backend-api/accounts/check/v4-2023-04-27"
SUBSCRIPTIONS_PATH = "/backend-api/subscriptions"


class ChatGptAccountCheckError(RuntimeError):
    pass


class ChatGptAccessTokenInvalid(ChatGptAccountCheckError):
    pass


@dataclass(frozen=True)
class ChatGptAccountStatus:
    payload: dict[str, Any]
    session: dict[str, Any]
    plan_type: str
    deactive: bool = False


class ChatGptAccountStatusChecker:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    async def check(self, access_token: str) -> ChatGptAccountStatus:
        token = access_token.strip()
        if not token:
            raise ChatGptAccessTokenInvalid("empty access token")

        account_error: ChatGptAccountCheckError | None = None
        try:
            status = await self._check_with_curl_cffi(token)
            if status is None:
                status = await self._check_with_httpx(token)
        except ChatGptAccountCheckError as exc:
            status = None
            account_error = exc

        if status is not None and _has_full_subscription_details(status.session):
            return status

        subscription_session: dict[str, Any] | None = None
        account_id = _chatgpt_account_id_from_token(token)
        if account_id:
            try:
                subscription_session = await self._subscription_with_curl_cffi(token, account_id)
            except ChatGptAccountCheckError:
                subscription_session = None

        if subscription_session is not None:
            return _merge_status_subscription(status, subscription_session)
        if status is not None:
            return ChatGptAccountStatus(
                payload=status.payload,
                session=complete_subscription_metadata(status.session),
                plan_type=status.plan_type,
                deactive=status.deactive,
            )
        if account_error is not None:
            raise account_error
        raise ChatGptAccountCheckError("ChatGPT account and subscription checks returned no data")

    async def _check_with_curl_cffi(self, token: str) -> ChatGptAccountStatus | None:
        try:
            from curl_cffi import requests
        except ImportError:
            return None

        url = f"{self.settings.chatgpt_base_url.rstrip('/')}{ACCOUNT_CHECK_PATH}"
        async with requests.AsyncSession(impersonate="chrome136", timeout=20) as client:
            response = await client.get(url, headers=_account_check_headers(token), allow_redirects=False)
        return _status_from_response(int(response.status_code), str(response.text))

    async def _check_with_httpx(self, token: str) -> ChatGptAccountStatus:
        url = f"{self.settings.chatgpt_base_url.rstrip('/')}{ACCOUNT_CHECK_PATH}"
        async with httpx.AsyncClient(
            timeout=20.0,
            headers=_account_check_headers(token, include_user_agent=True),
            trust_env=False,
        ) as client:
            response = await client.get(url, follow_redirects=False)
        return _status_from_response(response.status_code, response.text)

    async def _subscription_with_curl_cffi(self, token: str, account_id: str) -> dict[str, Any] | None:
        try:
            from curl_cffi import requests
        except ImportError:
            return await self._subscription_with_httpx(token, account_id)

        url = f"{self.settings.chatgpt_base_url.rstrip('/')}{SUBSCRIPTIONS_PATH}"
        async with requests.AsyncSession(impersonate="chrome136", timeout=20) as client:
            response = await client.get(
                url,
                params={"account_id": account_id},
                headers=_account_check_headers(token),
                allow_redirects=False,
            )
        return _subscription_from_response(int(response.status_code), str(response.text))

    async def _subscription_with_httpx(self, token: str, account_id: str) -> dict[str, Any] | None:
        url = f"{self.settings.chatgpt_base_url.rstrip('/')}{SUBSCRIPTIONS_PATH}"
        async with httpx.AsyncClient(
            timeout=20.0,
            headers=_account_check_headers(token, include_user_agent=True),
            trust_env=False,
        ) as client:
            response = await client.get(url, params={"account_id": account_id}, follow_redirects=False)
        return _subscription_from_response(response.status_code, response.text)

    async def check_with_urllib(self, access_token: str) -> ChatGptAccountStatus:
        return await self.check(access_token)


def _account_check_headers(token: str, include_user_agent: bool = False) -> dict[str, str]:
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Origin": "https://chatgpt.com",
        "Referer": "https://chatgpt.com/",
        "OAI-Language": "en-US",
        "Sec-Fetch-Site": "same-origin",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Dest": "empty",
    }
    if include_user_agent:
        headers["User-Agent"] = (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/136.0.0.0 Safari/537.36"
        )
    return headers


def _status_from_response(status_code: int, text: str) -> ChatGptAccountStatus:
    preview = text[:4000]
    if status_code in {401, 403}:
        if looks_deactive_text(preview):
            payload = _json_object_from_text(preview)
            session = _session_from_payload(payload)
            return ChatGptAccountStatus(payload=payload, session=session, plan_type="", deactive=True)
        if _looks_html(preview):
            raise ChatGptAccountCheckError(
                f"ChatGPT account check was blocked with HTTP {status_code}; browser TLS impersonation is required."
            )
        raise ChatGptAccessTokenInvalid(f"ChatGPT account check rejected access token with HTTP {status_code}")
    if status_code >= 400:
        raise ChatGptAccountCheckError(f"ChatGPT account check failed with HTTP {status_code}")

    payload = _json_object_from_text(text)
    session = _session_from_payload(payload)
    return ChatGptAccountStatus(
        payload=payload,
        session=session,
        plan_type=str(session.get("plan_type") or "").strip().lower(),
        deactive=looks_deactive_text(payload),
    )


def _looks_html(text: str) -> bool:
    stripped = text.lstrip().lower()
    return stripped.startswith("<html") or "<!doctype html" in stripped[:100]


def _json_object_from_text(text: str) -> dict[str, Any]:
    try:
        payload = json.loads(text)
    except ValueError as exc:
        raise ChatGptAccountCheckError("ChatGPT account check did not return JSON") from exc
    if not isinstance(payload, dict):
        raise ChatGptAccountCheckError("ChatGPT account check response was not a JSON object")
    return payload


def _session_from_payload(payload: dict[str, Any]) -> dict[str, Any]:
    selected = _selected_account(payload)
    account = selected.get("account") if isinstance(selected.get("account"), dict) else selected
    entitlement = selected.get("entitlement") if isinstance(selected.get("entitlement"), dict) else {}
    last_active_subscription = (
        selected.get("last_active_subscription") if isinstance(selected.get("last_active_subscription"), dict) else {}
    )
    plan_type = _extract_plan_type(selected, account, entitlement)

    session: dict[str, Any] = {
        "account": account,
        "entitlement": entitlement,
        "last_active_subscription": last_active_subscription,
        "plan_type": plan_type,
    }
    _set_if_present(session, "subscription_expires_at", _first_string(entitlement, "expires_at", "expiresAt"))
    _set_if_present(session, "subscription_renews_at", _first_string(entitlement, "renews_at", "renewsAt"))
    _set_if_present(session, "subscription_cancels_at", _first_string(entitlement, "cancels_at", "cancelsAt"))
    _set_if_present(session, "subscription_billing_period", _first_string(entitlement, "billing_period", "billingPeriod"))
    _set_if_present(session, "subscription_plan", _first_string(entitlement, "subscription_plan", "subscriptionPlan"))
    _set_if_present(
        session,
        "subscription_starts_at",
        _first_string(entitlement, "starts_at", "startsAt")
        or _subscription_starts_at(
            str(session.get("subscription_renews_at") or session.get("subscription_cancels_at") or ""),
            str(session.get("subscription_billing_period") or ""),
        ),
    )
    if isinstance(entitlement, dict) and isinstance(entitlement.get("has_active_subscription"), bool):
        session["has_active_subscription"] = entitlement["has_active_subscription"]
    return session


def _has_full_subscription_details(session: dict[str, Any]) -> bool:
    return bool(
        session.get("subscription_starts_at")
        and session.get("subscription_expires_at")
        and session.get("subscription_billing_period")
        and isinstance(session.get("has_active_subscription"), bool)
    )


def _merge_status_subscription(
    status: ChatGptAccountStatus | None,
    subscription_session: dict[str, Any],
) -> ChatGptAccountStatus:
    merged = dict(subscription_session)
    if status is not None:
        for key, value in status.session.items():
            if value not in (None, ""):
                merged[key] = value
    merged = complete_subscription_metadata(merged)
    plan_type = str(
        (status.plan_type if status is not None else "")
        or merged.get("plan_type")
        or merged.get("subscription_plan")
        or ""
    ).strip().lower()
    return ChatGptAccountStatus(
        payload=status.payload if status is not None else {},
        session=merged,
        plan_type=plan_type,
        deactive=status.deactive if status is not None else False,
    )


def _subscription_from_response(status_code: int, text: str) -> dict[str, Any] | None:
    preview = text[:4000]
    if status_code == 404:
        return None
    if status_code in {401, 403}:
        if _looks_html(preview):
            raise ChatGptAccountCheckError(
                f"ChatGPT subscription check was blocked with HTTP {status_code}; browser TLS impersonation is required."
            )
        raise ChatGptAccessTokenInvalid(f"ChatGPT subscription check rejected access token with HTTP {status_code}")
    if status_code >= 400:
        raise ChatGptAccountCheckError(f"ChatGPT subscription check failed with HTTP {status_code}")

    payload = _json_object_from_text(text)
    plan_type = _first_string(payload, "plan_type", "planType")
    active_until = _first_string(payload, "active_until", "activeUntil")
    will_renew = payload.get("will_renew")
    session: dict[str, Any] = {}
    _set_if_present(session, "plan_type", plan_type)
    _set_if_present(session, "subscription_plan", plan_type)
    _set_if_present(session, "subscription_expires_at", active_until)
    if active_until and will_renew is True:
        session["subscription_renews_at"] = active_until
    elif active_until and will_renew is False:
        session["subscription_cancels_at"] = active_until
    if active_until:
        parsed = _parse_datetime(active_until)
        if parsed is not None:
            session["has_active_subscription"] = parsed > datetime.now(timezone.utc)
    elif payload.get("id"):
        session["has_active_subscription"] = True
    return complete_subscription_metadata(session) if session else None


def _chatgpt_account_id_from_token(token: str) -> str | None:
    parts = token.strip().split(".")
    if len(parts) < 2:
        return None
    segment = parts[1] + ("=" * (-len(parts[1]) % 4))
    try:
        payload = json.loads(base64.urlsafe_b64decode(segment.encode("ascii")))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    auth = payload.get("https://api.openai.com/auth")
    if not isinstance(auth, dict):
        auth = {}
    for value in (
        auth.get("chatgpt_account_id"),
        auth.get("organization_id"),
        auth.get("poid"),
        payload.get("organization_id"),
    ):
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def complete_subscription_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    """Fill fields that are implied by an explicit plan and billing boundary."""

    completed = dict(metadata)
    plan_type = completed.get("plan_type") or completed.get("subscription_plan")
    if not completed.get("subscription_plan") and completed.get("plan_type"):
        completed["subscription_plan"] = completed["plan_type"]

    expires_at = str(completed.get("subscription_expires_at") or "").strip()
    billing_period = str(completed.get("subscription_billing_period") or "").strip()
    if not billing_period and expires_at and normalize_subscription_type(plan_type) == "plus":
        billing_period = "monthly"
        completed["subscription_billing_period"] = billing_period

    if not completed.get("subscription_starts_at") and billing_period:
        end_at = str(
            completed.get("subscription_renews_at")
            or completed.get("subscription_cancels_at")
            or expires_at
            or ""
        )
        starts_at = _subscription_starts_at(end_at, billing_period)
        if starts_at:
            completed["subscription_starts_at"] = starts_at

    if not expires_at and completed.get("subscription_starts_at") and billing_period:
        inferred_expires_at = _subscription_expires_at(
            str(completed["subscription_starts_at"]),
            billing_period,
        )
        if inferred_expires_at:
            completed["subscription_expires_at"] = inferred_expires_at
            expires_at = inferred_expires_at

    if not isinstance(completed.get("has_active_subscription"), bool) and expires_at:
        parsed_expires_at = _parse_datetime(expires_at)
        if parsed_expires_at is not None:
            completed["has_active_subscription"] = parsed_expires_at > datetime.now(timezone.utc)

    return completed


def _set_if_present(target: dict[str, Any], key: str, value: Any) -> None:
    if isinstance(value, str):
        value = value.strip()
    if value not in (None, ""):
        target[key] = value


def _first_string(data: Any, *keys: str) -> str | None:
    if not isinstance(data, dict):
        return None
    for key in keys:
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _parse_datetime(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _format_datetime(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _shift_months(value: datetime, months: int) -> datetime:
    month_index = value.month - 1 + months
    year = value.year + month_index // 12
    month = month_index % 12 + 1
    day = min(value.day, calendar.monthrange(year, month)[1])
    return value.replace(year=year, month=month, day=day)


def _subscription_starts_at(end_at: str, billing_period: str) -> str | None:
    end = _parse_datetime(end_at)
    if end is None or not billing_period:
        return None
    period = billing_period.strip().lower()
    if period in {"monthly", "month"}:
        return _format_datetime(_shift_months(end, -1))
    if period in {"yearly", "annual", "annually", "year"}:
        return _format_datetime(_shift_months(end, -12))
    if period in {"weekly", "week"}:
        return _format_datetime(end - timedelta(days=7))
    return None


def _subscription_expires_at(start_at: str, billing_period: str) -> str | None:
    start = _parse_datetime(start_at)
    if start is None or not billing_period:
        return None
    period = billing_period.strip().lower()
    if period in {"monthly", "month"}:
        return _format_datetime(_shift_months(start, 1))
    if period in {"yearly", "annual", "annually", "year"}:
        return _format_datetime(_shift_months(start, 12))
    if period in {"weekly", "week"}:
        return _format_datetime(start + timedelta(days=7))
    return None


def _selected_account(payload: dict[str, Any]) -> dict[str, Any]:
    accounts: Any = payload.get("accounts")
    response_payload = payload.get("response")
    if not isinstance(accounts, (list, dict)) and isinstance(response_payload, dict):
        accounts = response_payload.get("accounts")

    if isinstance(accounts, list) and accounts:
        first = accounts[0]
        return first if isinstance(first, dict) else {}
    if isinstance(accounts, dict) and accounts:
        first = next(iter(accounts.values()))
        return first if isinstance(first, dict) else {}
    return {}


def _extract_plan_type(selected: dict[str, Any], account: Any, entitlement: Any) -> str:
    account_map = account if isinstance(account, dict) else {}
    entitlement_map = entitlement if isinstance(entitlement, dict) else {}
    return str(
        account_map.get("plan_type")
        or account_map.get("planType")
        or entitlement_map.get("subscription_plan")
        or entitlement_map.get("subscriptionPlan")
        or selected.get("plan_type")
        or selected.get("planType")
        or ""
    ).strip().lower()
