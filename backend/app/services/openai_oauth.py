from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable
from urllib.parse import parse_qs, urlencode, urljoin, urlparse

import httpx

from app.core.config import Settings, get_settings
from app.services.browser import FetchCode
from app.services.camoufox_browser import launch_browser_context
from app.services.sub2api import looks_deactive_text


FetchPhoneCode = FetchCode
ResolvePhoneVerification = Callable[[str | None, bool], Awaitable["PhoneVerificationContext | None"]]


AUTH_ACCOUNTS_BASE = "https://auth.openai.com/api/accounts"
ADD_PHONE_ERROR = "当前账号触发 OpenAI 手机号验证；无接码流程下自动 OAuth 终止。"
OAUTH_PROTOCOL_EDGE_BLOCK_REASON_CODE = "oauth_protocol_edge_verification_blocked"
OAUTH_PROTOCOL_EDGE_BLOCK_GUIDANCE = (
    "Switch OAuth login mode to headless browser in Settings and retry."
)


class PhoneVerificationError(RuntimeError):
    pass


@dataclass(frozen=True)
class OAuthContext:
    state: str
    code_verifier: str
    redirect_uri: str
    client_id: str
    auth_url: str


@dataclass
class OpenAiOAuthOutcome:
    status: str
    access_token: str | None = None
    refresh_token: str | None = None
    id_token: str | None = None
    session: dict[str, Any] | None = None
    error: str | None = None
    phone_number_hint: str | None = None
    reason_code: str | None = None


@dataclass(frozen=True)
class PhoneVerificationContext:
    phone_number: str
    sms_url: str


class OpenAiOAuthRefresher:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    async def refresh_with_protocol(self, email: str, fetch_code: FetchCode) -> OpenAiOAuthOutcome:
        try:
            from curl_cffi import requests
        except ImportError:
            return OpenAiOAuthOutcome(status="failed", error="curl_cffi is not installed.")

        context = self._new_context()
        try:
            async with requests.AsyncSession(impersonate="chrome136", timeout=60) as session:
                code_result = await self._run_protocol_oauth(session, context, email, fetch_code)
                if code_result.status != "ok" or not code_result.access_token:
                    return code_result
                return await self._exchange_code(context, code_result.access_token, email)
        except Exception as exc:
            if looks_deactive_text(str(exc)):
                return OpenAiOAuthOutcome(status="deactive", error=f"OpenAI OAuth reported account_deactivated: {exc}")
            if _is_add_phone_text(str(exc)):
                return OpenAiOAuthOutcome(status="add_phone", error=ADD_PHONE_ERROR)
            if _is_protocol_edge_verification_text(str(exc)):
                return self._protocol_edge_blocked_outcome(
                    "OpenAI OAuth protocol flow",
                    _edge_status_from_text(str(exc)),
                )
            return OpenAiOAuthOutcome(status="failed", error=f"OpenAI OAuth protocol flow failed: {exc}")

    async def refresh_with_browser(
        self,
        email: str,
        fetch_code: FetchCode,
        phone: PhoneVerificationContext | None = None,
        fetch_phone_code: FetchPhoneCode | None = None,
        resolve_phone: ResolvePhoneVerification | None = None,
        *,
        stop_on_phone_verification: bool = False,
    ) -> OpenAiOAuthOutcome:
        try:
            from playwright.async_api import TimeoutError as PlaywrightTimeoutError
            from playwright.async_api import async_playwright
        except ImportError:
            return OpenAiOAuthOutcome(status="failed", error="Playwright is not installed.")

        context = self._new_context()
        async with async_playwright() as p:
            handle = await launch_browser_context(p, email, self.settings)
            page = await handle.context.new_page()
            page.set_default_timeout(self.settings.playwright_timeout_ms)
            observed_callback_urls: list[str] = []

            def remember_url(value: Any) -> None:
                text = str(value or "")
                try:
                    if self._code_from_url(text, context.state, context.redirect_uri):
                        observed_callback_urls.append(text)
                except RuntimeError:
                    observed_callback_urls.append(text)

            page.on("framenavigated", lambda frame: remember_url(getattr(frame, "url", "")))
            page.on("request", lambda request: remember_url(getattr(request, "url", "")))
            try:
                await page.goto(context.auth_url, wait_until="domcontentloaded", timeout=60_000)
                await self._wait_for_cloudflare_to_clear(page)
                requested_at: datetime | None = None
                phone_requested_at: datetime | None = None
                email_submit_attempts = 0

                deadline = asyncio.get_running_loop().time() + max(self.settings.verification_code_timeout_seconds + 90, 240)
                while asyncio.get_running_loop().time() < deadline:
                    code = self._code_from_url(page.url, context.state, context.redirect_uri)
                    if not code and observed_callback_urls:
                        code = self._code_from_url(
                            observed_callback_urls[-1], context.state, context.redirect_uri
                        )
                    if code:
                        return await self._exchange_code(context, code, email)

                    body_text = await self._page_body_text(page)
                    if looks_deactive_text(body_text):
                        return OpenAiOAuthOutcome(status="deactive", error="account_deactive was detected during OpenAI OAuth.")
                    if _is_cloudflare_text(body_text):
                        await self._wait_for_cloudflare_to_clear(page)
                        continue
                    if _is_phone_verification_rate_limited_text(body_text):
                        snapshot_path = await self._capture_phone_verification_snapshot(page, email)
                        return OpenAiOAuthOutcome(
                            status="add_phone",
                            error=_append_snapshot_path(
                                "当前账号触发 OpenAI 手机号验证频控：Too many phone verification requests, please try again later.",
                                snapshot_path,
                            ),
                        )
                    if _is_mfa_app_challenge_text(body_text) or "/mfa-challenge/" in page.url:
                        snapshot_path = await self._capture_phone_verification_snapshot(page, email)
                        return OpenAiOAuthOutcome(
                            status="failed",
                            error=_append_snapshot_path(
                                "当前账号触发 OpenAI OTP 应用验证：需要 authenticator app / TOTP 一次性口令。",
                                snapshot_path,
                            ),
                        )

                    if (
                        _is_add_phone_text(body_text)
                        or "/add-phone" in page.url
                        or _is_phone_verification_text(body_text)
                        or (phone is not None and _phone_text_matches_bound_phone(body_text, phone.phone_number))
                    ):
                        page_phone_hint = await self._extract_phone_number_hint(page, body_text)
                        if stop_on_phone_verification:
                            snapshot_path = await self._capture_phone_verification_snapshot(page, email)
                            return OpenAiOAuthOutcome(
                                status="add_phone",
                                error=_append_snapshot_path(ADD_PHONE_ERROR, snapshot_path),
                                phone_number_hint=page_phone_hint,
                            )
                        if resolve_phone is not None and _is_phone_number_limit_text(body_text):
                            resolved_phone = await resolve_phone(page_phone_hint or (phone.phone_number if phone else None), True)
                            if resolved_phone is not None:
                                phone = resolved_phone
                        if resolve_phone is not None:
                            resolved_phone = await resolve_phone(page_phone_hint, False)
                            if resolved_phone is not None:
                                phone = resolved_phone
                        handled, next_requested_at = await self._handle_phone_step(
                            page,
                            body_text,
                            phone,
                            fetch_phone_code,
                            phone_requested_at,
                        )
                        if handled == "completed":
                            phone_requested_at = next_requested_at or phone_requested_at
                            await page.wait_for_timeout(2500)
                            continue
                        if handled == "blocked":
                            snapshot_path = await self._capture_phone_verification_snapshot(page, email)
                            phone_text = f" 已绑定手机号 {phone.phone_number}。" if phone else ""
                            return OpenAiOAuthOutcome(
                                status="add_phone",
                                error=_append_snapshot_path(f"{ADD_PHONE_ERROR}{phone_text}", snapshot_path),
                                phone_number_hint=page_phone_hint,
                            )

                    if await self._email_input_visible(page):
                        if email_submit_attempts >= 3:
                            return OpenAiOAuthOutcome(status="failed", error="OpenAI OAuth email submission did not advance.")
                        requested_at = datetime.now().astimezone()
                        email_submit_attempts += 1
                        await self._submit_email(page, email)
                        await page.wait_for_timeout(2500)
                        continue

                    if await self._choose_email_code_flow(page):
                        await page.wait_for_timeout(2500)
                        continue

                    if await self._choose_existing_account(page, email):
                        await page.wait_for_timeout(2500)
                        continue

                    if await self._password_input_visible(page):
                        return OpenAiOAuthOutcome(
                            status="failed",
                            error="Password login is required; OAuth RT acquisition supports email verification code login only.",
                        )

                    if await self._code_input_visible(page):
                        if requested_at is None:
                            requested_at = datetime.now().astimezone()
                        verification_code = await fetch_code(requested_at)
                        if not verification_code:
                            return OpenAiOAuthOutcome(status="failed", error="Verification code was not found before timeout.")
                        await self._fill_code(page, verification_code)
                        await page.wait_for_timeout(3000)
                        continue

                    if await self._choose_workspace(page):
                        await page.wait_for_timeout(2000)
                        continue
                    if await self._approve_consent(page):
                        await page.wait_for_timeout(2000)
                        continue

                    try:
                        await page.wait_for_load_state("networkidle", timeout=3000)
                    except PlaywrightTimeoutError:
                        pass
                    await page.wait_for_timeout(1000)

                return OpenAiOAuthOutcome(
                    status="failed",
                    error=f"Timed out waiting for OpenAI OAuth callback. {await self._safe_page_state(page)}",
                )
            except PhoneVerificationError as exc:
                snapshot_path = await self._capture_phone_verification_snapshot(page, email)
                return OpenAiOAuthOutcome(status="add_phone", error=_append_snapshot_path(str(exc), snapshot_path))
            except Exception as exc:
                if _is_add_phone_text(str(exc)):
                    return OpenAiOAuthOutcome(status="add_phone", error=ADD_PHONE_ERROR)
                if _is_phone_verification_error_text(str(exc)):
                    snapshot_path = await self._capture_phone_verification_snapshot(page, email)
                    return OpenAiOAuthOutcome(status="add_phone", error=_append_snapshot_path(str(exc), snapshot_path))
                if looks_deactive_text(str(exc)):
                    return OpenAiOAuthOutcome(status="deactive", error=f"OpenAI OAuth browser flow reported account_deactivated: {exc}")
                return OpenAiOAuthOutcome(status="failed", error=f"OpenAI OAuth browser flow failed: {exc}")
            finally:
                await handle.close()

    def _new_context(self) -> OAuthContext:
        code_verifier = _pkce_code_verifier()
        code_challenge = _pkce_code_challenge(code_verifier)
        state = secrets.token_urlsafe(32)
        redirect_uri = self.settings.openai_oauth_redirect_uri
        client_id = self.settings.openai_oauth_client_id
        params = {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "scope": self.settings.openai_oauth_scopes,
            "state": state,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
            "id_token_add_organizations": "true",
            "codex_cli_simplified_flow": "true",
        }
        auth_url = f"{self.settings.openai_oauth_authorize_url}?{urlencode(params)}"
        return OAuthContext(
            state=state,
            code_verifier=code_verifier,
            redirect_uri=redirect_uri,
            client_id=client_id,
            auth_url=auth_url,
        )

    async def _run_protocol_oauth(
        self,
        session: Any,
        context: OAuthContext,
        email: str,
        fetch_code: FetchCode,
    ) -> OpenAiOAuthOutcome:
        auth_response = await session.get(context.auth_url, headers={"Accept": "application/json"}, allow_redirects=False)
        if auth_response.status_code >= 400:
            return self._http_error("OpenAI OAuth authorization start", auth_response)
        if _is_protocol_edge_verification_response(auth_response):
            return self._protocol_edge_blocked_outcome(
                "OpenAI OAuth authorization start",
                _response_status_code(auth_response),
            )
        start_url = self._continue_url(self._json_object(auth_response)) or self._location(auth_response)
        if start_url:
            code = await self._follow_protocol_url(session, start_url, context)
            if code.status != "failed" or code.access_token:
                return code

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
            allow_redirects=False,
        )
        if email_response.status_code >= 400:
            return self._http_error("OpenAI OAuth email submission", email_response)
        if _is_protocol_edge_verification_response(email_response):
            return self._protocol_edge_blocked_outcome(
                "OpenAI OAuth email submission",
                _response_status_code(email_response),
            )
        email_location = self._location(email_response)
        if email_location:
            return await self._follow_protocol_url(session, email_location, context)
        email_payload = self._json_object(email_response)
        page_type = self._page_type(email_payload)
        if page_type == "login_password":
            return OpenAiOAuthOutcome(
                status="failed",
                error="Password login is required; OAuth RT acquisition supports email verification code login only.",
            )
        if page_type and page_type != "email_otp_verification":
            next_url = self._continue_url(email_payload) or self._payload_url(email_payload)
            if next_url:
                return await self._follow_protocol_url(session, next_url, context)
            if _is_add_phone_text(json.dumps(email_payload, ensure_ascii=False)):
                return OpenAiOAuthOutcome(status="add_phone", error=ADD_PHONE_ERROR)
            return OpenAiOAuthOutcome(status="failed", error=f"OpenAI OAuth reached unsupported auth page: {page_type}.")

        code = await fetch_code(requested_at)
        if not code:
            return OpenAiOAuthOutcome(status="failed", error="Verification code was not found before timeout.")

        otp_response = await session.post(
            f"{AUTH_ACCOUNTS_BASE}/email-otp/validate",
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "Origin": "https://auth.openai.com",
                "Referer": "https://auth.openai.com/email-verification",
            },
            json={"code": code},
            allow_redirects=False,
        )
        if otp_response.status_code >= 400:
            return self._http_error("OpenAI OAuth OTP validation", otp_response)
        if _is_protocol_edge_verification_response(otp_response):
            return self._protocol_edge_blocked_outcome(
                "OpenAI OAuth OTP validation",
                _response_status_code(otp_response),
            )

        otp_payload = self._json_object(otp_response)
        if _is_add_phone_text(json.dumps(otp_payload, ensure_ascii=False)):
            return OpenAiOAuthOutcome(status="add_phone", error=ADD_PHONE_ERROR)
        page_type = self._page_type(otp_payload)
        if page_type == "login_password":
            return OpenAiOAuthOutcome(status="failed", error="Password login is required after OTP validation.")
        next_url = self._continue_url(otp_payload) or self._payload_url(otp_payload) or self._location(otp_response)
        if next_url:
            return await self._follow_protocol_url(session, next_url, context)
        return OpenAiOAuthOutcome(
            status="failed",
            error=f"OpenAI OAuth OTP validation reached unsupported auth page: {page_type or 'unknown'}.",
        )

    async def _follow_protocol_url(self, session: Any, url: str, context: OAuthContext) -> OpenAiOAuthOutcome:
        current_url = urljoin(context.auth_url, url)
        for _ in range(12):
            callback_code = self._code_from_url(
                current_url, context.state, context.redirect_uri
            )
            if callback_code:
                return OpenAiOAuthOutcome(status="ok", access_token=callback_code)
            if _is_add_phone_text(current_url):
                return OpenAiOAuthOutcome(status="add_phone", error=ADD_PHONE_ERROR)
            if not _is_allowed_protocol_continuation(current_url, context):
                return OpenAiOAuthOutcome(
                    status="failed",
                    error="OpenAI OAuth continuation changed to an untrusted origin.",
                )

            response = await session.get(current_url, allow_redirects=False)
            callback_code = self._code_from_response(
                response, context.state, context.redirect_uri
            )
            if callback_code:
                return OpenAiOAuthOutcome(status="ok", access_token=callback_code)
            text = str(getattr(response, "text", ""))[:4000]
            if _is_add_phone_text(text) or _is_add_phone_text(self._location(response)):
                return OpenAiOAuthOutcome(status="add_phone", error=ADD_PHONE_ERROR)
            if looks_deactive_text(text):
                return OpenAiOAuthOutcome(status="deactive", error="account_deactive was returned during OpenAI OAuth.")
            if _is_protocol_edge_verification_response(response):
                return self._protocol_edge_blocked_outcome(
                    "OpenAI OAuth continuation",
                    _response_status_code(response),
                )
            if response.status_code >= 400:
                return self._http_error("OpenAI OAuth continuation", response)

            payload = self._json_object(response)
            next_url = self._continue_url(payload) or self._payload_url(payload) or self._location(response)
            if not next_url:
                page_type = self._page_type(payload)
                return OpenAiOAuthOutcome(
                    status="failed",
                    error=f"OpenAI OAuth continuation reached unsupported page: {page_type or 'unknown'}.",
                )
            current_url = urljoin(str(getattr(response, "url", current_url)), next_url)
        return OpenAiOAuthOutcome(status="failed", error="OpenAI OAuth continuation exceeded redirect limit.")

    async def _exchange_code(self, context: OAuthContext, code: str, email: str) -> OpenAiOAuthOutcome:
        try:
            async with httpx.AsyncClient(timeout=30.0, trust_env=False) as client:
                response = await client.post(
                    self.settings.openai_oauth_token_url,
                    data={
                        "grant_type": "authorization_code",
                        "client_id": context.client_id,
                        "code": code,
                        "redirect_uri": context.redirect_uri,
                        "code_verifier": context.code_verifier,
                    },
                    headers={
                        "Accept": "application/json",
                        "Content-Type": "application/x-www-form-urlencoded",
                        "User-Agent": self.settings.openai_oauth_user_agent,
                    },
                )
        except httpx.HTTPError as exc:
            return OpenAiOAuthOutcome(status="failed", error=f"OpenAI OAuth token request failed: {exc}")

        if _is_protocol_edge_verification_response(response):
            return self._protocol_edge_blocked_outcome(
                "OpenAI OAuth token exchange",
                _response_status_code(response),
            )
        if response.status_code >= 400:
            return OpenAiOAuthOutcome(
                status="failed",
                error=f"OpenAI OAuth token exchange failed with HTTP {response.status_code}: {_oauth_error(response.text)}",
            )
        try:
            payload = response.json()
        except ValueError:
            return OpenAiOAuthOutcome(status="failed", error="OpenAI OAuth token endpoint did not return JSON.")
        if not isinstance(payload, dict):
            return OpenAiOAuthOutcome(status="failed", error="OpenAI OAuth token response was not a JSON object.")

        access_token = _string(payload.get("access_token"))
        refresh_token = _string(payload.get("refresh_token"))
        id_token = _string(payload.get("id_token"))
        if not access_token:
            return OpenAiOAuthOutcome(status="failed", error="OpenAI OAuth token response did not include access_token.")
        if not refresh_token:
            return OpenAiOAuthOutcome(status="failed", error="OpenAI OAuth token response did not include refresh_token.")

        id_claims = _decode_jwt_payload(id_token)
        token_email = _string(id_claims.get("email"))
        if token_email and token_email.lower() != email.lower():
            return OpenAiOAuthOutcome(status="failed", error=f"OAuth login email mismatch: {token_email}.")

        session_payload = self._token_session_payload(payload, context.client_id, token_email or email)
        return OpenAiOAuthOutcome(
            status="ok",
            access_token=access_token,
            refresh_token=refresh_token,
            id_token=id_token,
            session=session_payload,
        )

    def _token_session_payload(self, payload: dict[str, Any], client_id: str, email: str) -> dict[str, Any]:
        access_token = _string(payload.get("access_token"))
        refresh_token = _string(payload.get("refresh_token"))
        id_token = _string(payload.get("id_token"))
        expires_at = _expires_at(payload.get("expires_in"))
        tokens = {
            "access_token": access_token,
            "refresh_token": refresh_token,
            "id_token": id_token,
            "client_id": client_id,
        }
        if expires_at:
            tokens["expires_at"] = expires_at
        return {
            "access_token": access_token,
            "refresh_token": refresh_token,
            "id_token": id_token,
            "client_id": client_id,
            "expires_at": expires_at,
            "email": email,
            "tokens": tokens,
        }

    def _http_error(self, label: str, response: Any) -> OpenAiOAuthOutcome:
        text = str(getattr(response, "text", ""))[:4000]
        if _is_add_phone_text(text):
            return OpenAiOAuthOutcome(status="add_phone", error=ADD_PHONE_ERROR)
        if looks_deactive_text(text):
            return OpenAiOAuthOutcome(status="deactive", error=f"account_deactive was returned by {label}.")
        if _is_protocol_edge_verification_response(response):
            return self._protocol_edge_blocked_outcome(label, _response_status_code(response))
        return OpenAiOAuthOutcome(status="failed", error=f"{label} failed with HTTP {response.status_code}.")

    def _protocol_edge_blocked_outcome(self, label: str, status_code: int | None) -> OpenAiOAuthOutcome:
        status_text = f"HTTP {status_code}, " if status_code is not None else ""
        return OpenAiOAuthOutcome(
            status="failed",
            error=(
                f"{OAUTH_PROTOCOL_EDGE_BLOCK_REASON_CODE}: OpenAI OAuth protocol login was blocked by "
                f"edge verification ({status_text}Cloudflare/Sentinel) during {label}. "
                f"{OAUTH_PROTOCOL_EDGE_BLOCK_GUIDANCE}"
            ),
            reason_code=OAUTH_PROTOCOL_EDGE_BLOCK_REASON_CODE,
        )

    def _json_object(self, response: Any) -> dict[str, Any]:
        try:
            payload = response.json()
        except Exception:
            return {}
        return payload if isinstance(payload, dict) else {}

    def _continue_url(self, payload: dict[str, Any]) -> str | None:
        return _string(payload.get("continue_url"))

    def _payload_url(self, payload: dict[str, Any]) -> str | None:
        page = payload.get("page")
        page_payload = page.get("payload") if isinstance(page, dict) else None
        if not isinstance(page_payload, dict):
            return None
        for key in ("url", "continue_url"):
            value = _string(page_payload.get(key))
            if value:
                return value
        return None

    def _page_type(self, payload: dict[str, Any]) -> str | None:
        page = payload.get("page")
        if isinstance(page, dict):
            return _string(page.get("type"))
        return _string(payload.get("type"))

    def _location(self, response: Any) -> str | None:
        headers = getattr(response, "headers", {})
        value = headers.get("location") if hasattr(headers, "get") else None
        if not isinstance(value, str) or not value.strip():
            return None
        return urljoin(str(getattr(response, "url", "")), value.strip())

    def _code_from_response(
        self,
        response: Any,
        state: str,
        redirect_uri: str,
    ) -> str | None:
        location = self._location(response)
        if location:
            code = self._code_from_url(location, state, redirect_uri)
            if code:
                return code
        return self._code_from_url(str(getattr(response, "url", "")), state, redirect_uri)

    def _code_from_url(
        self,
        url: str | None,
        state: str,
        redirect_uri: str,
    ) -> str | None:
        if not url:
            return None
        parsed = urlparse(str(url))
        if not _same_oauth_redirect_target(parsed, urlparse(redirect_uri)):
            return None
        query = parse_qs(parsed.query, keep_blank_values=True)
        if any(len(query.get(key, [])) > 1 for key in ("state", "code", "error", "error_description")):
            return None
        returned_state = (query.get("state") or [""])[0]
        if returned_state != state:
            return None
        error = (query.get("error_description") or query.get("error") or [""])[0]
        if error:
            raise RuntimeError(f"OpenAI OAuth callback returned error: {error}")
        code = (query.get("code") or [""])[0]
        return code.strip() or None

    async def _page_body_text(self, page: Any) -> str:
        try:
            return await page.locator("body").inner_text(timeout=3000)
        except Exception:
            return ""

    async def _safe_page_state(self, page: Any) -> str:
        url = str(getattr(page, "url", ""))
        parsed = urlparse(url)
        path = parsed.path or "/"
        text = await self._page_body_text(page)
        text = _sanitize_page_text(text)
        return f"current_url={parsed.netloc}{path}; body={text or '-'}"

    async def _capture_phone_verification_snapshot(self, page: Any, email: str) -> str | None:
        output_dir = self.settings.project_root / "output" / "oauth-phone-blocks"
        output_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        safe_email = re.sub(r"[^a-z0-9]+", "_", email.lower()).strip("_") or "account"
        prefix = output_dir / f"{safe_email}-{timestamp}"
        screenshot_path = prefix.with_suffix(".png")
        text_path = prefix.with_suffix(".txt")

        try:
            await page.screenshot(path=str(screenshot_path), full_page=True)
        except Exception:
            screenshot_path = None

        try:
            url = str(getattr(page, "url", ""))
            body_text = _sanitize_page_text(await self._page_body_text(page))
            safe_state = await self._safe_page_state(page)
            text_path.write_text(
                f"url={url}\nstate={safe_state}\n\nbody:\n{body_text}\n",
                encoding="utf-8",
            )
        except Exception:
            text_path = None

        if screenshot_path is not None:
            try:
                return str(screenshot_path.relative_to(self.settings.project_root))
            except ValueError:
                return str(screenshot_path)
        if text_path is not None:
            try:
                return str(text_path.relative_to(self.settings.project_root))
            except ValueError:
                return str(text_path)
        return None

    async def _email_input_visible(self, page: Any) -> bool:
        return await self._first_visible(
            page,
            [
                'input#email',
                'input[name="email"]',
                'input[name="username"]',
                'input[type="email"]',
                'input[autocomplete*="email"]',
                'input[autocomplete*="username"]',
                'input[placeholder*="Email" i]',
            ],
            timeout_ms=1000,
        ) is not None

    async def _password_input_visible(self, page: Any) -> bool:
        return await self._first_visible(page, ['input[type="password"]'], timeout_ms=1000) is not None

    async def _choose_email_code_flow(self, page: Any) -> bool:
        return await self._click_text(page, ["one-time code", "email code", "log in with a one-time code", "send code"])

    async def _code_input_visible(self, page: Any) -> bool:
        return await self._first_visible(
            page,
            self._otp_input_selectors(include_generic_text=False),
            timeout_ms=1000,
        ) is not None

    async def _submit_email(self, page: Any, email: str) -> None:
        locator = await self._first_visible(
            page,
            [
                'input#email',
                'input[name="email"]',
                'input[name="username"]',
                'input[type="email"]',
                'input[autocomplete*="email"]',
                'input[autocomplete*="username"]',
                'input[placeholder*="Email" i]',
            ],
            timeout_ms=15_000,
        )
        if locator is None:
            raise RuntimeError("Could not find OpenAI OAuth email input.")
        await locator.fill(email)
        await self._click_continue(page)

    async def _fill_code(self, page: Any, code: str, include_generic_text: bool = True) -> None:
        inputs = page.locator(
            ", ".join(self._otp_input_selectors(include_generic_text=include_generic_text))
        )
        count = await inputs.count()
        visible_inputs = []
        for index in range(count):
            candidate = inputs.nth(index)
            try:
                if await candidate.is_visible():
                    visible_inputs.append(candidate)
            except Exception:
                continue
        if not visible_inputs and include_generic_text:
            for locator in (
                page.get_by_label(re_compile("code")),
                page.get_by_placeholder(re_compile("code")),
                page.get_by_role("textbox", name=re_compile("code")),
            ):
                try:
                    locator_count = await locator.count()
                except Exception:
                    continue
                for index in range(locator_count):
                    candidate = locator.nth(index)
                    try:
                        if await candidate.is_visible():
                            visible_inputs.append(candidate)
                    except Exception:
                        continue
        if len(visible_inputs) > 1 and len(code) <= len(visible_inputs):
            for candidate, character in zip(visible_inputs[-len(code) :], code, strict=False):
                await candidate.fill(character)
        elif visible_inputs:
            try:
                await visible_inputs[0].fill(code)
            except Exception:
                await visible_inputs[0].click()
                await page.keyboard.type(code)
        else:
            if include_generic_text:
                try:
                    await page.locator("body").click()
                    await page.keyboard.type(code)
                except Exception as exc:
                    raise RuntimeError("Could not find OpenAI OAuth verification-code input.") from exc
            else:
                raise RuntimeError("Could not find OpenAI OAuth verification-code input.")
        await self._click_continue(page)

    async def _choose_workspace(self, page: Any) -> bool:
        body = await self._page_body_text(page)
        if not _is_workspace_text(body):
            return False
        radios = page.locator('input[type="radio"][name*="workspace"], input[type="radio"][name="workspace_id"]')
        count = await radios.count()
        if count:
            try:
                await radios.nth(0).check()
            except Exception:
                await radios.nth(0).click(force=True)
            await self._click_continue(page)
            return True
        return await self._click_text(page, ["continue", "next"])

    async def _choose_existing_account(self, page: Any, email: str) -> bool:
        body = await self._page_body_text(page)
        if not _is_choose_account_text(body):
            return False
        if await self._click_text(page, [email, email.split("@", 1)[0], "select account", "continue"]):
            return True
        elements = page.locator("button, [role='button'], a")
        count = await elements.count()
        for index in range(count):
            candidate = elements.nth(index)
            try:
                if not await candidate.is_visible():
                    continue
                text = await self._clickable_text(candidate)
                if any(marker in text for marker in ("log in to another account", "create account", "google", "apple", "microsoft")):
                    continue
                await candidate.click()
                return True
            except Exception:
                continue
        return False

    async def _approve_consent(self, page: Any) -> bool:
        return await self._click_text(page, ["authorize", "allow", "continue", "approve", "accept", "yes"])

    async def _handle_phone_step(
        self,
        page: Any,
        body_text: str,
        phone: PhoneVerificationContext | None,
        fetch_phone_code: FetchPhoneCode | None,
        requested_at: datetime | None,
    ) -> tuple[str, datetime | None]:
        if phone is None or fetch_phone_code is None:
            return "blocked", requested_at

        if await self._phone_code_input_visible(page, body_text):
            active_requested_at = requested_at or datetime.now().astimezone()
            code = await fetch_phone_code(active_requested_at)
            if not code:
                return "blocked", active_requested_at
            await self._fill_phone_code(page, code)
            return "completed", active_requested_at

        submitted = False
        add_phone_page = _is_add_phone_text(body_text) or "/add-phone" in page.url
        phone_input_visible = await self._phone_number_input_visible(page)
        if not _is_masked_phone_value(phone.phone_number) and phone_input_visible:
            await self._submit_phone_number(page, phone.phone_number)
            submitted = True

        delivery_selected = False
        if not add_phone_page or submitted or not phone_input_visible:
            delivery_selected = await self._choose_phone_delivery_method(page)
        if submitted or delivery_selected:
            return "completed", datetime.now().astimezone()

        if _is_phone_verification_text(body_text) or add_phone_page:
            await self._click_continue(page)
            return "completed", datetime.now().astimezone()
        return "blocked", requested_at

    async def _extract_phone_number_hint(self, page: Any, body_text: str) -> str | None:
        input_value_hint = await self._phone_number_input_value(page)
        if input_value_hint:
            return input_value_hint
        patterns = [
            r"(?:to|sent to|sending to|texted to|whatsapp to)\s*[:：]?\s*(\+?[\d\s().\-•*xX#?]{5,})",
            r"(?:发送到|短信发送到|验证码发送到|将发送到)\s*[:：]?\s*(\+?[\d\s().\-•*xX#?]{5,})",
            r"(\+?\d[\d\s().\-]*[*•xX#?][\d\s().\-•*xX#?]{3,})",
        ]
        for pattern in patterns:
            match = re.search(pattern, body_text, re.I)
            if not match:
                continue
            candidate = match.group(1).strip(" .:;,)」】]\n\t")
            if _looks_phone_hint(candidate):
                return candidate
        return None

    async def _phone_number_input_value(self, page: Any) -> str | None:
        values: list[str] = []
        for candidate in await self._visible_phone_inputs(page, timeout_ms=300):
            for getter in (lambda item=candidate: item.input_value(), lambda item=candidate: item.get_attribute("value")):
                try:
                    value = await getter()
                except Exception:
                    continue
                text = str(value or "").strip()
                if text:
                    values.append(text)
                    break
        if not values:
            return None

        cleaned_values = [re.sub(r"\s+", "", value) for value in values if value.strip()]
        for candidate in cleaned_values:
            if _looks_phone_hint(candidate):
                return candidate
        combined = "".join(cleaned_values)
        if _looks_phone_hint(combined):
            return combined
        return None

    async def _phone_number_input_visible(self, page: Any) -> bool:
        return await self._first_visible(
            page,
            [
                'input[autocomplete="tel"]',
                'input[inputmode="tel"]',
                'input[type="tel"]',
                'input[aria-label*="phone" i]',
                'input[aria-describedby*="phone" i]',
                'input[name*="phone" i]',
                'input[id*="phone" i]',
                'input[placeholder*="phone" i]',
            ],
            timeout_ms=1000,
        ) is not None

    async def _submit_phone_number(self, page: Any, phone_number: str) -> None:
        inputs = await self._visible_phone_inputs(page, timeout_ms=3000)
        if not inputs:
            raise RuntimeError("Could not find OpenAI OAuth phone-number input.")

        country_code, local_number = _split_phone_number(phone_number)
        if country_code and len(inputs) >= 2:
            try:
                await inputs[0].fill(country_code)
            except Exception:
                pass
            await inputs[-1].fill(local_number)
        else:
            await inputs[-1].fill(local_number)
        await self._click_continue(page)

    async def _visible_phone_inputs(self, page: Any, timeout_ms: int) -> list[Any]:
        selectors = [
            'input[autocomplete="tel"]',
            'input[inputmode="tel"]',
            'input[type="tel"]',
            'input[aria-label*="phone" i]',
            'input[aria-describedby*="phone" i]',
            'input[name*="phone" i]',
            'input[id*="phone" i]',
            'input[placeholder*="phone" i]',
        ]
        deadline = asyncio.get_running_loop().time() + timeout_ms / 1000
        while asyncio.get_running_loop().time() < deadline:
            visible_inputs: list[Any] = []
            for selector in selectors:
                locator = page.locator(selector)
                count = await locator.count()
                for index in range(count):
                    candidate = locator.nth(index)
                    try:
                        if await candidate.is_visible(timeout=200):
                            visible_inputs.append(candidate)
                    except Exception:
                        continue
            if visible_inputs:
                return visible_inputs
            await asyncio.sleep(0.1)
        return []

    async def _choose_phone_delivery_method(self, page: Any) -> bool:
        if await self._click_text(page, ["text message", "text", "sms"]):
            return True
        if await self._click_text(page, ["whatsapp"]):
            return True
        if await self._click_text(page, ["send code", "send", "continue", "verify"]):
            return True
        return False

    async def _phone_code_input_visible(self, page: Any, body_text: str | None = None) -> bool:
        prompt_text = body_text or await self._page_body_text(page)
        if _is_phone_code_prompt_text(prompt_text):
            return True
        selectors = self._otp_input_selectors(include_generic_text=False)
        return await self._first_visible(
            page,
            selectors,
            timeout_ms=1000,
        ) is not None

    async def _fill_phone_code(self, page: Any, code: str) -> None:
        await self._fill_code(page, code, include_generic_text=True)

    def _otp_input_selectors(self, include_generic_text: bool) -> list[str]:
        selectors = [
            'input[autocomplete="one-time-code"]',
            'input[name*="code" i]',
            'input[id*="code" i]',
            'input[placeholder*="code" i]',
            'input[aria-label*="code" i]',
            'input[aria-describedby*="code" i]',
        ]
        if include_generic_text:
            selectors.extend(
                [
                    'input[inputmode="numeric"]',
                    'input[type="tel"]',
                    'input[type="text"]',
                    'input:not([type])',
                    'textarea',
                    '[role="textbox"]',
                    '[contenteditable="true"]',
                ]
            )
        return selectors

    async def _click_resend_phone_code(self, page: Any) -> bool:
        return await self._click_text(page, ["resend text message", "resend code", "resend sms", "resend"])

    async def _click_continue(self, page: Any) -> None:
        if await self._click_text(page, ["continue", "next", "log in", "sign in", "verify", "submit", "allow", "authorize"]):
            return
        await page.keyboard.press("Enter")

    async def _click_text(self, page: Any, labels: list[str]) -> bool:
        pattern = "|".join(labels)
        for role in ("button", "link"):
            locator = page.get_by_role(role, name=re_compile(pattern))
            count = await locator.count()
            for index in range(count):
                candidate = locator.nth(index)
                try:
                    if await candidate.is_visible():
                        text = await self._clickable_text(candidate)
                        if any(provider in text for provider in ("google", "apple", "microsoft")):
                            continue
                        await candidate.click()
                        return True
                except Exception:
                    continue
        elements = page.locator("button, [role='button'], a")
        count = await elements.count()
        for index in range(count):
            candidate = elements.nth(index)
            try:
                if not await candidate.is_visible():
                    continue
                text = await self._clickable_text(candidate)
                if any(provider in text for provider in ("google", "apple", "microsoft")):
                    continue
                if any(label in text for label in labels):
                    await candidate.click()
                    return True
            except Exception:
                continue
        return False

    async def _clickable_text(self, locator: Any) -> str:
        parts: list[str] = []
        for getter in (locator.inner_text, lambda: locator.get_attribute("aria-label"), lambda: locator.get_attribute("title")):
            try:
                value = await getter()
            except Exception:
                continue
            if isinstance(value, str) and value.strip():
                parts.append(value.strip())
        return " ".join(parts).lower()

    async def _wait_for_cloudflare_to_clear(self, page: Any, timeout_ms: int = 45_000) -> None:
        deadline = asyncio.get_running_loop().time() + timeout_ms / 1000
        while asyncio.get_running_loop().time() < deadline:
            body_text = await self._page_body_text(page)
            if not _is_cloudflare_text(body_text):
                return
            await page.wait_for_timeout(1500)
        raise RuntimeError(f"OpenAI OAuth Cloudflare challenge did not clear. {await self._safe_page_state(page)}")

    async def _first_visible(self, page: Any, selectors: list[str], timeout_ms: int) -> Any | None:
        deadline = asyncio.get_running_loop().time() + timeout_ms / 1000
        while asyncio.get_running_loop().time() < deadline:
            for selector in selectors:
                locator = page.locator(selector)
                try:
                    count = await locator.count()
                except Exception:
                    continue
                for index in range(count):
                    candidate = locator.nth(index)
                    try:
                        if await candidate.is_visible(timeout=250):
                            return candidate
                    except Exception:
                        continue
            await asyncio.sleep(0.1)
        return None


def _url_origin(value: str) -> tuple[str, str, int] | None:
    parsed = urlparse(value)
    scheme = parsed.scheme.casefold()
    hostname = (parsed.hostname or "").casefold()
    if scheme not in {"http", "https"} or not hostname:
        return None
    if parsed.username is not None or parsed.password is not None:
        return None
    try:
        port = parsed.port or (443 if scheme == "https" else 80)
    except ValueError:
        return None
    return scheme, hostname, port


def _same_oauth_redirect_target(actual: Any, expected: Any) -> bool:
    actual_origin = _url_origin(actual.geturl())
    expected_origin = _url_origin(expected.geturl())
    if actual_origin is None or actual_origin != expected_origin:
        return False
    actual_path = actual.path or "/"
    expected_path = expected.path or "/"
    return (
        actual_path == expected_path
        and actual.params == expected.params
        and not actual.fragment
        and not expected.fragment
    )


def _is_allowed_protocol_continuation(url: str, context: OAuthContext) -> bool:
    candidate_origin = _url_origin(url)
    if candidate_origin is None:
        return False
    allowed_origins = {
        origin
        for origin in (_url_origin(context.auth_url), _url_origin(AUTH_ACCOUNTS_BASE))
        if origin is not None
    }
    return candidate_origin in allowed_origins


def _pkce_code_verifier() -> str:
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("ascii").rstrip("=")


def _pkce_code_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def _decode_jwt_payload(token: str | None) -> dict[str, Any]:
    if not token:
        return {}
    parts = token.strip().split(".")
    if len(parts) < 2:
        return {}
    segment = parts[1]
    padded = segment + ("=" * (-len(segment) % 4))
    try:
        raw = base64.urlsafe_b64decode(padded.encode("ascii"))
        payload = json.loads(raw)
    except (ValueError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _expires_at(expires_in: Any) -> str | None:
    try:
        seconds = int(expires_in)
    except (TypeError, ValueError):
        return None
    if seconds <= 0:
        return None
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _string(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value or None


def _oauth_error(text: str) -> str:
    try:
        payload = json.loads(text)
    except ValueError:
        return text[:300]
    if not isinstance(payload, dict):
        return text[:300]
    error = payload.get("error")
    if isinstance(error, dict):
        return str(error.get("message") or error.get("code") or text[:300])
    return str(payload.get("error_description") or payload.get("message") or payload.get("error") or text[:300])[:300]


def _is_add_phone_text(value: Any) -> bool:
    text = str(value or "").lower()
    return "/add-phone" in text or "add phone" in text or "phone number required" in text


def _is_phone_verification_text(value: Any) -> bool:
    text = str(value or "").lower()
    markers = (
        "phone verification",
        "verify your phone",
        "verify your phone number",
        "enter your phone number",
        "text or whatsapp",
        "text message",
        "whatsapp",
        "send a code",
        "send code",
        "send a one-time code",
        "send a verification code",
        "phone number required",
        "phone number",
        "手机验证码",
        "手机号验证码",
        "手机号码",
        "手机号",
        "发送验证码",
        "发送到",
        "短信",
        "whatsapp 发送",
    )
    return any(marker in text for marker in markers)


def _is_phone_verification_rate_limited_text(value: Any) -> bool:
    text = str(value or "").lower()
    return (
        "too many phone verification requests" in text
        or "rate_limit_exceeded" in text and "phone" in text
        or "please try again later" in text and ("phone verification" in text or "verify your phone" in text or "check your phone" in text)
    )


def _is_phone_number_limit_text(value: Any) -> bool:
    text = str(value or "").lower()
    return (
        "already linked to the maximum number of accounts" in text
        or "maximum number of accounts" in text and "phone number" in text
        or "已关联到可绑定账号上限" in text
        or "绑定了过多账号" in text
    )


def _split_phone_number(value: str) -> tuple[str | None, str]:
    raw = str(value or "").strip()
    digits = re.sub(r"\D", "", raw)
    if not digits:
        return None, raw
    if raw.startswith("+"):
        for code in _COMMON_COUNTRY_CODES:
            if digits.startswith(code):
                national = digits[len(code) :]
                if 4 <= len(national) <= 14:
                    return f"+{code}", national
        if len(digits) > 10:
            for code_len in (3, 2, 1):
                if len(digits) <= code_len:
                    continue
                national = digits[code_len:]
                if 4 <= len(national) <= 14:
                    return f"+{digits[:code_len]}", national
    if len(digits) == 13 and digits.startswith("86"):
        return "+86", digits[-11:]
    if not raw.startswith("+") and len(digits) == 11 and digits.startswith("1"):
        return "+86", digits
    return None, raw


_COMMON_COUNTRY_CODES = (
    "234",
    "95",
    "94",
    "93",
    "92",
    "91",
    "90",
    "86",
    "84",
    "82",
    "81",
    "66",
    "65",
    "63",
    "62",
    "61",
    "60",
    "49",
    "44",
    "39",
    "34",
    "33",
    "27",
    "20",
    "7",
    "1",
)


def _phone_text_matches_bound_phone(body_text: str, phone_number: str) -> bool:
    digits = re.sub(r"\D", "", str(phone_number or ""))
    if len(digits) < 4:
        return False
    tail4 = digits[-4:]
    tail3 = digits[-3:]
    text = str(body_text or "")
    normalized = re.sub(r"\s+", "", text)
    return tail4 in normalized or tail3 in normalized


def _looks_phone_hint(value: str) -> bool:
    text = str(value or "").strip()
    visible_digits = re.sub(r"\D", "", text)
    if len(visible_digits) < 4:
        return False
    if any(marker in text for marker in ("*", "•", "x", "X", "#", "?")):
        return True
    return len(visible_digits) >= 7


def _is_masked_phone_value(value: str) -> bool:
    text = str(value or "")
    return any(marker in text for marker in ("*", "•", "x", "X", "#", "?"))


_PROTOCOL_EDGE_TEXT_MARKERS = (
    "cf-chl-",
    "cf-mitigated",
    "challenge-platform",
    "cloudflare",
    "openai-sentinel",
    "performing security verification",
    "sentinel challenge",
    "turnstile",
    "verify you are human",
    "verify you are not a bot",
)


def _response_status_code(response: Any) -> int | None:
    try:
        return int(getattr(response, "status_code", None))
    except (TypeError, ValueError):
        return None


def _edge_status_from_text(value: str) -> int | None:
    match = re.search(r"\b(?:HTTP(?:\s+status)?\s*)?(403|409)\b", value, re.I)
    return int(match.group(1)) if match else None


def _is_protocol_edge_verification_text(value: str) -> bool:
    text = str(value or "").casefold()
    if any(marker in text for marker in _PROTOCOL_EDGE_TEXT_MARKERS):
        return True
    return _edge_status_from_text(text) in {403, 409}


def _is_protocol_edge_verification_response(response: Any) -> bool:
    if _response_status_code(response) in {403, 409}:
        return True
    if _is_protocol_edge_verification_text(str(getattr(response, "text", ""))[:16_000]):
        return True
    headers = getattr(response, "headers", None)
    if not hasattr(headers, "items"):
        return False
    for key, value in headers.items():
        normalized_key = str(key).casefold()
        normalized_value = str(value).casefold()
        if normalized_key == "cf-mitigated" and "challenge" in normalized_value:
            return True
        if normalized_key.startswith("x-openai-sentinel") and any(
            marker in normalized_value
            for marker in ("blocked", "challenge", "failed", "invalid", "required")
        ):
            return True
    return False


def is_protocol_edge_verification_blocked(value: str | None) -> bool:
    return OAUTH_PROTOCOL_EDGE_BLOCK_REASON_CODE in str(value or "")


def _is_cloudflare_text(value: str) -> bool:
    text = value.lower()
    return "performing security verification" in text or "verify you are not a bot" in text or "cloudflare" in text


def _is_phone_verification_error_text(value: str) -> bool:
    text = str(value or "")
    lowered = text.lower()
    return (
        "接码链接已过期" in text
        or "接码链接" in text and "不可用" in text
        or "sms url has expired" in lowered
        or "手机号库中找到" in text
        or "可绑定账号上限" in text
        or "停止自动 oauth" in lowered
        or "manual verification" in lowered
    )


def _is_mfa_app_challenge_text(value: str) -> bool:
    text = str(value or "").lower()
    return (
        "verify your identity" in text
        and "one-time password application" in text
        or "authenticator app" in text
        or "totp" in text
    )


def _is_phone_code_prompt_text(value: str) -> bool:
    text = str(value or "").lower()
    phone_context_markers = (
        "check your phone",
        "text message",
        "resend text message",
        "sms",
        "phone-verification",
        "手机号",
        "短信",
        "发送到",
    )
    code_markers = (
        "enter the verification code",
        "verification code we just sent",
        "we just sent to",
        "we sent a code",
        "code was sent",
        "sent you a code",
        "enter code",
        "短信验证码",
        "输入验证码",
        "我们刚刚发送",
    )
    return any(marker in text for marker in phone_context_markers) and any(marker in text for marker in code_markers)


def _is_workspace_text(value: str) -> bool:
    text = value.lower()
    return "choose a workspace" in text or "select a workspace" in text or ("workspace" in text and "personal account" in text)


def _is_choose_account_text(value: str) -> bool:
    text = value.lower()
    return "choose an account" in text or "select account" in text or "continue with" in text and "another account" in text


def _sanitize_page_text(value: str) -> str:
    text = re.sub(r"[A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,}", "***email***", str(value or ""), flags=re.I)
    text = re.sub(r"\b\d{6}\b", "***code***", text)
    return re.sub(r"\s+", " ", text).strip()[:4000]


def _append_snapshot_path(message: str, snapshot_path: str | None) -> str:
    if not snapshot_path:
        return message
    return f"{message} 阻塞页快照：{snapshot_path}。"


def re_compile(pattern: str):
    return re.compile(pattern, re.I)
