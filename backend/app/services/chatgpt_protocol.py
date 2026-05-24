from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.parse import unquote, urljoin

from app.core.config import Settings, get_settings
from app.services.browser import FetchCode
from app.services.sub2api import looks_deactive_text


AUTH_ACCOUNTS_BASE = "https://auth.openai.com/api/accounts"
NEXTAUTH_PROVIDER = "openai"


@dataclass
class ProtocolRefreshOutcome:
    status: str
    access_token: str | None = None
    session: dict[str, Any] | None = None
    error: str | None = None


class ChatGptProtocolRefresher:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    async def refresh_access_token(self, email: str, fetch_code: FetchCode) -> ProtocolRefreshOutcome:
        try:
            from curl_cffi import requests
        except ImportError:
            return ProtocolRefreshOutcome(status="failed", error="curl_cffi is not installed.")

        base_url = self.settings.chatgpt_base_url.rstrip("/")
        try:
            async with requests.AsyncSession(impersonate="chrome136", timeout=45) as session:
                login_response = await session.get(f"{base_url}/auth/login", allow_redirects=True)
                if login_response.status_code >= 400:
                    return self._http_error("ChatGPT login page", login_response)

                csrf_token = self._csrf_token(session)
                if not csrf_token:
                    return ProtocolRefreshOutcome(status="failed", error="ChatGPT login did not set a CSRF token.")

                signin_response = await session.post(
                    f"{base_url}/api/auth/signin/{NEXTAUTH_PROVIDER}",
                    data={
                        "csrfToken": csrf_token,
                        "callbackUrl": f"{base_url}/",
                        "json": "true",
                    },
                    headers={
                        "Accept": "application/json",
                        "Content-Type": "application/x-www-form-urlencoded",
                    },
                    allow_redirects=False,
                )
                if signin_response.status_code >= 400:
                    return self._http_error("ChatGPT sign-in start", signin_response)
                signin_payload = self._json_object(signin_response)
                auth_url = self._string(signin_payload.get("url"))
                if not auth_url:
                    return ProtocolRefreshOutcome(status="failed", error="ChatGPT sign-in start did not return an auth URL.")

                auth_response = await session.get(auth_url, headers={"Accept": "application/json"}, allow_redirects=False)
                if auth_response.status_code >= 400:
                    return self._http_error("OpenAI auth start", auth_response)
                auth_payload = self._json_object(auth_response)
                continue_url = self._continue_url(auth_payload) or self._location(auth_response)
                if not continue_url:
                    return ProtocolRefreshOutcome(status="failed", error="OpenAI auth start did not return a continuation URL.")

                await session.get(continue_url, allow_redirects=True)

                requested_at = datetime.now().astimezone()
                email_response = await session.post(
                    f"{AUTH_ACCOUNTS_BASE}/authorize/continue",
                    headers={
                        "Accept": "application/json",
                        "Content-Type": "application/json",
                        "Origin": "https://auth.openai.com",
                        "Referer": "https://auth.openai.com/log-in",
                        "OpenAI-Sentinel-Token": '{"e":"q2n8w7x5z1"}',
                    },
                    json={"username": {"kind": "email", "value": email}},
                )
                if email_response.status_code >= 400:
                    return self._http_error("OpenAI email submission", email_response)
                email_payload = self._json_object(email_response)
                page_type = self._page_type(email_payload)
                if page_type == "login_password":
                    return ProtocolRefreshOutcome(
                        status="failed",
                        error="Password login is required; protocol refresh supports email verification code login only.",
                    )
                if page_type != "email_otp_verification":
                    return ProtocolRefreshOutcome(
                        status="failed",
                        error=f"OpenAI email submission reached unsupported auth page: {page_type or 'unknown'}.",
                    )

                code = await fetch_code(requested_at)
                if not code:
                    return ProtocolRefreshOutcome(status="failed", error="Verification code was not found before timeout.")

                otp_response = await session.post(
                    f"{AUTH_ACCOUNTS_BASE}/email-otp/validate",
                    headers={
                        "Accept": "application/json",
                        "Content-Type": "application/json",
                        "Origin": "https://auth.openai.com",
                        "Referer": "https://auth.openai.com/email-verification",
                    },
                    json={"code": code},
                )
                if otp_response.status_code >= 400:
                    if looks_deactive_text(otp_response.text[:4000]):
                        return ProtocolRefreshOutcome(
                            status="deactive",
                            error="account_deactive was returned during protocol OTP validation.",
                        )
                    return ProtocolRefreshOutcome(
                        status="failed",
                        error=f"OpenAI OTP validation failed with HTTP {otp_response.status_code}.",
                    )

                otp_payload = self._json_object(otp_response)
                page_type = self._page_type(otp_payload)
                if page_type == "login_password":
                    return ProtocolRefreshOutcome(
                        status="failed",
                        error="Password login is required after OTP validation.",
                    )
                if page_type not in {"external_url", "token_response"}:
                    if looks_deactive_text(otp_payload):
                        return ProtocolRefreshOutcome(
                            status="deactive",
                            error="account_deactive was returned after protocol OTP validation.",
                        )
                    return ProtocolRefreshOutcome(
                        status="failed",
                        error=f"OpenAI OTP validation reached unsupported auth page: {page_type or 'unknown'}.",
                    )

                next_url = self._continue_url(otp_payload) or self._payload_url(otp_payload)
                if next_url:
                    await session.get(next_url, allow_redirects=True)

                session_response = await session.get(
                    self.settings.chatgpt_session_url,
                    headers={"Accept": "application/json"},
                    allow_redirects=False,
                )
                if session_response.status_code >= 400:
                    return self._http_error("ChatGPT session endpoint", session_response)
                if looks_deactive_text(session_response.text[:4000]):
                    return ProtocolRefreshOutcome(
                        status="deactive",
                        error="account_deactive was returned by protocol session endpoint.",
                    )
                session_payload = self._json_object(session_response)
                access_token = self._access_token(session_payload)
                if not access_token:
                    return ProtocolRefreshOutcome(status="failed", error="Session endpoint did not include accessToken.")
                return ProtocolRefreshOutcome(status="ok", access_token=access_token, session=session_payload)
        except Exception as exc:
            if looks_deactive_text(str(exc)):
                return ProtocolRefreshOutcome(status="deactive", error=f"Protocol refresh reported account_deactivated: {exc}")
            return ProtocolRefreshOutcome(status="failed", error=f"Protocol refresh failed: {exc}")

    def _csrf_token(self, session: Any) -> str | None:
        cookies = getattr(getattr(session, "cookies", None), "jar", [])
        for cookie in cookies:
            if getattr(cookie, "name", "") == "__Host-next-auth.csrf-token":
                value = unquote(str(getattr(cookie, "value", "")))
                token = value.split("|", 1)[0].strip()
                return token or None
        return None

    def _http_error(self, label: str, response: Any) -> ProtocolRefreshOutcome:
        text = str(getattr(response, "text", ""))[:4000]
        if looks_deactive_text(text):
            return ProtocolRefreshOutcome(status="deactive", error=f"account_deactive was returned by {label}.")
        return ProtocolRefreshOutcome(status="failed", error=f"{label} failed with HTTP {response.status_code}.")

    def _json_object(self, response: Any) -> dict[str, Any]:
        try:
            payload = response.json()
        except Exception:
            return {}
        return payload if isinstance(payload, dict) else {}

    def _continue_url(self, payload: dict[str, Any]) -> str | None:
        value = payload.get("continue_url")
        if isinstance(value, str) and value.strip():
            return value.strip()
        return None

    def _payload_url(self, payload: dict[str, Any]) -> str | None:
        page = payload.get("page")
        page_payload = page.get("payload") if isinstance(page, dict) else None
        if not isinstance(page_payload, dict):
            return None
        for key in ("url", "continue_url"):
            value = page_payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return None

    def _page_type(self, payload: dict[str, Any]) -> str | None:
        page = payload.get("page")
        if isinstance(page, dict):
            return self._string(page.get("type"))
        return self._string(payload.get("type"))

    def _location(self, response: Any) -> str | None:
        headers = getattr(response, "headers", {})
        value = headers.get("location") if hasattr(headers, "get") else None
        if not isinstance(value, str) or not value.strip():
            return None
        return urljoin(str(getattr(response, "url", "")), value.strip())

    def _access_token(self, session: dict[str, Any]) -> str | None:
        token = session.get("accessToken") or session.get("access_token")
        if isinstance(token, str):
            token = token.strip()
            return token or None
        return None

    def _string(self, value: Any) -> str | None:
        if not isinstance(value, str):
            return None
        value = value.strip()
        return value or None
