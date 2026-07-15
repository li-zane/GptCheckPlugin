from __future__ import annotations

import asyncio
import io
import json
from dataclasses import dataclass
from typing import Any
from urllib import error as urllib_error
from urllib import parse as urllib_parse
from urllib import request as urllib_request

from app.core.config import Settings, get_settings


ACCOUNT_CHECK_PATH = "/backend-api/accounts/check/v4-2023-04-27"


class _NoRedirectHandler(urllib_request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _build_no_redirect_opener():
    return urllib_request.build_opener(_NoRedirectHandler())


class OpenAiTokenRefreshError(RuntimeError):
    pass


class OpenAiRefreshTokenInvalid(OpenAiTokenRefreshError):
    pass


class OpenAiAccessTokenInvalid(OpenAiTokenRefreshError):
    pass


@dataclass(frozen=True)
class OpenAiProfile:
    payload: dict[str, Any]
    selected_account: dict[str, Any]
    session: dict[str, Any]
    deactive: bool = False


class OpenAiTokenService:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    async def refresh_token(
        self,
        *,
        refresh_token: str,
        client_id: str | None = None,
    ) -> dict[str, Any]:
        token = str(refresh_token or "").strip()
        if not token:
            raise OpenAiRefreshTokenInvalid("empty refresh token")

        resolved_client_id = str(client_id or self.settings.openai_oauth_client_id or "").strip()
        if not resolved_client_id:
            raise OpenAiTokenRefreshError("missing OpenAI OAuth client_id")

        def run_request() -> dict[str, Any]:
            payload = urllib_parse.urlencode(
                {
                    "grant_type": "refresh_token",
                    "refresh_token": token,
                    "client_id": resolved_client_id,
                    "scope": "openid profile email",
                }
            ).encode("utf-8")
            request = urllib_request.Request(
                self.settings.openai_oauth_token_url,
                data=payload,
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    "User-Agent": self.settings.openai_oauth_user_agent,
                },
                method="POST",
            )
            with urllib_request.build_opener().open(request, timeout=30) as response:
                body = response.read().decode(response.headers.get_content_charset() or "utf-8")
                parsed = json.loads(body)
                if not isinstance(parsed, dict):
                    raise OpenAiTokenRefreshError("OpenAI token refresh response was not a JSON object.")
                return parsed

        try:
            payload = await asyncio.to_thread(run_request)
        except urllib_error.HTTPError as exc:
            detail = exc.reason
            error_code = None
            try:
                body = exc.read().decode("utf-8")
                parsed = json.loads(body)
                if isinstance(parsed, dict):
                    nested_error = parsed.get("error") if isinstance(parsed.get("error"), dict) else {}
                    error_code = str(parsed.get("code") or nested_error.get("code") or parsed.get("error") or "").strip()
                    detail = (
                        parsed.get("error_description")
                        or nested_error.get("message")
                        or parsed.get("error")
                        or body
                        or detail
                    )
            except Exception:
                pass
            if error_code == "refresh_token_reused":
                raise OpenAiRefreshTokenInvalid(
                    "OpenAI refresh token 已失效（refresh_token_reused），请改用 OAuth 重新获取 refresh_token。"
                ) from exc
            raise OpenAiTokenRefreshError(f"OpenAI refresh token 刷新失败: {detail}") from exc
        except urllib_error.URLError as exc:
            raise OpenAiTokenRefreshError(f"OpenAI Token 服务不可用: {exc.reason}") from exc
        except TimeoutError as exc:
            raise OpenAiTokenRefreshError("OpenAI Token 服务请求超时") from exc

        access_token = str(payload.get("access_token") or "").strip()
        if not access_token:
            raise OpenAiTokenRefreshError("OpenAI token response did not include access_token.")
        if not str(payload.get("refresh_token") or "").strip():
            raise OpenAiTokenRefreshError("OpenAI token response did not include refresh_token.")
        return payload

    async def fetch_profile(self, access_token: str) -> OpenAiProfile:
        token = str(access_token or "").strip()
        if not token:
            raise OpenAiAccessTokenInvalid("empty access token")

        async def run_curl_cffi_request() -> dict[str, Any] | None:
            try:
                from curl_cffi import requests
            except ImportError:
                return None

            async with requests.AsyncSession(
                impersonate="chrome136",
                timeout=20,
                trust_env=False,
                allow_redirects=False,
            ) as session:
                response = await session.get(
                    f"{self.settings.chatgpt_base_url.rstrip('/')}{ACCOUNT_CHECK_PATH}",
                    headers=_account_check_headers(token, self.settings.openai_oauth_user_agent),
                )
            if response.status_code >= 400:
                body = str(getattr(response, "text", "") or "")
                raise urllib_error.HTTPError(
                    f"{self.settings.chatgpt_base_url.rstrip('/')}{ACCOUNT_CHECK_PATH}",
                    int(response.status_code),
                    getattr(response, "reason", "") or body[:200],
                    getattr(response, "headers", None),
                    io.BytesIO(body.encode("utf-8")),
                )
            parsed = response.json()
            return parsed if isinstance(parsed, dict) else {}

        def run_request() -> dict[str, Any]:
            request = urllib_request.Request(
                f"{self.settings.chatgpt_base_url.rstrip('/')}{ACCOUNT_CHECK_PATH}",
                headers=_account_check_headers(token, self.settings.openai_oauth_user_agent),
                method="GET",
            )
            with _build_no_redirect_opener().open(request, timeout=20) as response:
                body = response.read().decode(response.headers.get_content_charset() or "utf-8")
                parsed = json.loads(body)
                if not isinstance(parsed, dict):
                    raise OpenAiTokenRefreshError("OpenAI 账号信息返回不是 JSON 对象。")
                return parsed

        try:
            payload = await run_curl_cffi_request()
            if payload is None:
                payload = await asyncio.to_thread(run_request)
        except urllib_error.HTTPError as exc:
            detail = exc.reason
            status_code = int(getattr(exc, "code", 0) or 0)
            try:
                body = exc.read().decode("utf-8")
                parsed = json.loads(body)
                if isinstance(parsed, dict):
                    detail = str(parsed.get("detail") or parsed.get("message") or parsed.get("error") or body or detail)
            except Exception:
                pass
            if status_code == 401:
                raise OpenAiAccessTokenInvalid(f"OpenAI 账号信息获取失败: {detail}") from exc
            raise OpenAiTokenRefreshError(f"OpenAI 账号信息获取失败: {detail}") from exc
        except urllib_error.URLError as exc:
            raise OpenAiTokenRefreshError(f"OpenAI 账号信息服务不可用: {exc.reason}") from exc
        except TimeoutError as exc:
            raise OpenAiTokenRefreshError("OpenAI 账号信息请求超时") from exc
        except json.JSONDecodeError as exc:
            raise OpenAiTokenRefreshError("OpenAI 账号信息返回不是有效 JSON。") from exc

        selected_account = _selected_account(payload)
        session = _session_from_payload(payload)
        return OpenAiProfile(
            payload=payload,
            selected_account=selected_account,
            session=session,
            deactive=_looks_deactive(payload),
        )


def _account_check_headers(token: str, user_agent: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Origin": "https://chatgpt.com",
        "Referer": "https://chatgpt.com/",
        "OAI-Language": "en-US",
        "Sec-Fetch-Site": "same-origin",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Dest": "empty",
        "User-Agent": user_agent,
    }


def _looks_deactive(value: Any) -> bool:
    text = str(value or "").lower()
    return any(
        marker in text
        for marker in (
            "account_deactive",
            "account_deactivated",
            "account has been deactivated",
            "account is deactivated",
        )
    )


def _selected_account(payload: dict[str, Any]) -> dict[str, Any]:
    accounts = payload.get("accounts")
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


def _session_from_payload(payload: dict[str, Any]) -> dict[str, Any]:
    selected = _selected_account(payload)
    account = selected.get("account") if isinstance(selected.get("account"), dict) else selected
    entitlement = selected.get("entitlement") if isinstance(selected.get("entitlement"), dict) else {}
    last_active_subscription = (
        selected.get("last_active_subscription") if isinstance(selected.get("last_active_subscription"), dict) else {}
    )
    session: dict[str, Any] = {
        "account": account,
        "entitlement": entitlement,
        "last_active_subscription": last_active_subscription,
        "plan_type": str(
            (account if isinstance(account, dict) else {}).get("plan_type")
            or (account if isinstance(account, dict) else {}).get("planType")
            or entitlement.get("subscription_plan")
            or entitlement.get("subscriptionPlan")
            or selected.get("plan_type")
            or selected.get("planType")
            or ""
        ).strip().lower(),
    }
    for key, values in {
        "subscription_expires_at": ("expires_at", "expiresAt"),
        "subscription_renews_at": ("renews_at", "renewsAt"),
        "subscription_cancels_at": ("cancels_at", "cancelsAt"),
        "subscription_billing_period": ("billing_period", "billingPeriod"),
        "subscription_plan": ("subscription_plan", "subscriptionPlan"),
        "subscription_starts_at": ("starts_at", "startsAt"),
    }.items():
        for item_key in values:
            value = entitlement.get(item_key) if isinstance(entitlement, dict) else None
            if isinstance(value, str) and value.strip():
                session[key] = value.strip()
                break
    if isinstance(entitlement, dict) and isinstance(entitlement.get("has_active_subscription"), bool):
        session["has_active_subscription"] = entitlement["has_active_subscription"]
    return session
