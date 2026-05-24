from __future__ import annotations

import asyncio
import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

import httpx

from app.core.config import Settings, get_settings
from app.services.sub2api import looks_deactive_text


ACCOUNT_CHECK_PATH = "/backend-api/accounts/check/v4-2023-04-27"


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

        headers = {
            "Authorization": f"Bearer {token}",
            "Origin": "https://chatgpt.com",
            "Referer": "https://chatgpt.com/",
            "Accept": "application/json",
            "User-Agent": "codex_cli_rs/0.104.0",
        }
        url = f"{self.settings.chatgpt_base_url.rstrip('/')}{ACCOUNT_CHECK_PATH}"
        async with httpx.AsyncClient(timeout=20.0, headers=headers) as client:
            response = await client.get(url)

        text = response.text[:4000]
        if response.status_code in {401, 403}:
            if looks_deactive_text(text):
                payload = _safe_json_object(response)
                session = _session_from_payload(payload)
                return ChatGptAccountStatus(payload=payload, session=session, plan_type="", deactive=True)
            raise ChatGptAccessTokenInvalid(f"ChatGPT account check rejected access token with HTTP {response.status_code}")
        if response.status_code >= 400:
            raise ChatGptAccountCheckError(f"ChatGPT account check failed with HTTP {response.status_code}")

        payload = _safe_json_object(response)
        session = _session_from_payload(payload)
        return ChatGptAccountStatus(
            payload=payload,
            session=session,
            plan_type=str(session.get("plan_type") or "").strip().lower(),
            deactive=looks_deactive_text(payload),
        )

    async def check_with_urllib(self, access_token: str) -> ChatGptAccountStatus:
        return await asyncio.to_thread(self._check_with_urllib_sync, access_token)

    def _check_with_urllib_sync(self, access_token: str) -> ChatGptAccountStatus:
        token = access_token.strip()
        if not token:
            raise ChatGptAccessTokenInvalid("empty access token")

        request = urllib.request.Request(
            f"{self.settings.chatgpt_base_url.rstrip('/')}{ACCOUNT_CHECK_PATH}",
            headers={
                "Authorization": f"Bearer {token}",
                "Origin": "https://chatgpt.com",
                "Referer": "https://chatgpt.com/",
                "Accept": "application/json",
                "User-Agent": "codex_cli_rs/0.104.0",
            },
            method="GET",
        )
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                charset = response.headers.get_content_charset() or "utf-8"
                text = response.read().decode(charset, errors="replace")
        except urllib.error.HTTPError as exc:
            text = exc.read().decode("utf-8", errors="replace")[:4000]
            if exc.code in {401, 403}:
                if looks_deactive_text(text):
                    payload = _json_object_from_text(text)
                    session = _session_from_payload(payload)
                    return ChatGptAccountStatus(payload=payload, session=session, plan_type="", deactive=True)
                raise ChatGptAccessTokenInvalid(
                    f"ChatGPT account check rejected access token with HTTP {exc.code}"
                ) from exc
            raise ChatGptAccountCheckError(f"ChatGPT account check failed with HTTP {exc.code}") from exc

        payload = _json_object_from_text(text)
        session = _session_from_payload(payload)
        return ChatGptAccountStatus(
            payload=payload,
            session=session,
            plan_type=str(session.get("plan_type") or "").strip().lower(),
            deactive=looks_deactive_text(payload),
        )


def _safe_json_object(response: httpx.Response) -> dict[str, Any]:
    try:
        payload = response.json()
    except ValueError as exc:
        raise ChatGptAccountCheckError("ChatGPT account check did not return JSON") from exc
    if not isinstance(payload, dict):
        raise ChatGptAccountCheckError("ChatGPT account check response was not a JSON object")
    return payload


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
    plan_type = _extract_plan_type(selected, account, entitlement)

    session: dict[str, Any] = {
        "account": account,
        "entitlement": entitlement,
        "plan_type": plan_type,
    }
    return session


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
