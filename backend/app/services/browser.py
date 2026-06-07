import asyncio
import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from app.core.config import Settings, get_settings
from app.services.camoufox_browser import launch_browser_context
from app.services.sub2api import looks_deactive_text


FetchCode = Callable[[datetime], Awaitable[str | None]]


@dataclass
class BrowserRefreshOutcome:
    status: str
    access_token: str | None = None
    session: dict[str, Any] | None = None
    error: str | None = None


class ChatGptBrowserRefresher:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    async def refresh_access_token(self, email: str, fetch_code: FetchCode) -> BrowserRefreshOutcome:
        try:
            from playwright.async_api import TimeoutError as PlaywrightTimeoutError
            from playwright.async_api import async_playwright
        except ImportError:
            return BrowserRefreshOutcome(status="failed", error="Playwright is not installed.")

        async with async_playwright() as p:
            handle = await launch_browser_context(p, email, self.settings)
            page = await handle.context.new_page()
            page.set_default_timeout(self.settings.playwright_timeout_ms)
            try:
                await self._open_login_form(page)
                requested_at = await self._submit_email(page, email)

                if await self._has_deactive_marker(page):
                    return BrowserRefreshOutcome(status="deactive", error="account_deactive was detected during login.")

                if await self._password_required(page):
                    return BrowserRefreshOutcome(
                        status="failed",
                        error="Password login is required; this plugin currently supports email verification code login.",
                    )

                if not await self._has_code_input(page, timeout_ms=10_000):
                    return BrowserRefreshOutcome(status="failed", error="Verification code input did not appear after email submit.")

                code = await fetch_code(requested_at)
                if not code:
                    return BrowserRefreshOutcome(status="failed", error="Verification code was not found before timeout.")

                await self._fill_code(page, code)
                await self._click_continue(page)
                await page.wait_for_timeout(2500)

                if await self._has_deactive_marker(page):
                    return BrowserRefreshOutcome(status="deactive", error="account_deactive was detected after code validation.")

                try:
                    await page.wait_for_load_state("networkidle", timeout=20_000)
                except PlaywrightTimeoutError:
                    pass

                await page.goto(self.settings.chatgpt_session_url, wait_until="domcontentloaded")
                body = await page.locator("body").inner_text(timeout=20_000)
                if looks_deactive_text(body):
                    return BrowserRefreshOutcome(status="deactive", error="account_deactive was returned by session endpoint.")

                session = self._extract_session_payload(body)
                token = self._extract_access_token(body, session)
                if not token:
                    return BrowserRefreshOutcome(status="failed", error="Session endpoint did not include accessToken.")
                return BrowserRefreshOutcome(status="ok", access_token=token, session=session)
            except Exception as exc:
                if looks_deactive_text(str(exc)):
                    return BrowserRefreshOutcome(status="deactive", error=f"Browser refresh reported account_deactivated: {exc}")
                return BrowserRefreshOutcome(status="failed", error=f"Browser refresh failed: {exc}")
            finally:
                await handle.close()

    async def _open_login_form(self, page) -> None:
        login_url = f"{self.settings.chatgpt_base_url.rstrip('/')}/auth/login"
        await page.goto(login_url, wait_until="domcontentloaded")
        await self._wait_for_cloudflare_to_clear(page)
        if await self._has_email_input(page, timeout_ms=12_000):
            return

        await page.goto(self.settings.chatgpt_base_url, wait_until="domcontentloaded")
        await self._wait_for_cloudflare_to_clear(page)
        await self._click_login(page)
        await self._first_visible(self._email_input_candidates(page), "email input", timeout_ms=20_000)

    async def _click_login(self, page) -> None:
        candidates = [
            page.locator('[data-testid="login-button"]').last,
            page.get_by_role("link", name=re.compile(r"log in|login|sign in", re.I)).last,
            page.get_by_role("button", name=re.compile(r"log in|login|sign in", re.I)).last,
            page.locator("button").filter(has_text=re.compile(r"log in|login|sign in", re.I)).last,
        ]
        await self._click_first_visible(candidates, "login button")

    async def _fill_email(self, page, email: str) -> None:
        locator = await self._first_visible(self._email_input_candidates(page), "email input")
        await locator.fill(email)

    async def _submit_email(self, page, email: str) -> datetime:
        requested_at = datetime.now().astimezone()
        for attempt in range(3):
            await self._fill_email(page, email)
            requested_at = datetime.now().astimezone()
            await self._click_continue(page)
            await page.wait_for_timeout(2500)
            if await self._has_code_input(page, timeout_ms=1500):
                return requested_at
            if await self._password_required(page) or await self._has_deactive_marker(page):
                return requested_at
            if not await self._has_email_input(page, timeout_ms=1500):
                return requested_at
            if attempt < 2:
                await page.wait_for_timeout(1000)
        return requested_at

    async def _fill_code(self, page, code: str) -> None:
        locator = await self._first_visible(self._code_input_candidates(page), "verification code input")
        input_count = await page.locator("input").count()
        if input_count >= len(code):
            visible_inputs = []
            for index in range(input_count):
                candidate = page.locator("input").nth(index)
                if await candidate.is_visible():
                    visible_inputs.append(candidate)
            if len(visible_inputs) >= len(code) and all(len(ch) == 1 for ch in code):
                for candidate, character in zip(visible_inputs[-len(code) :], code, strict=False):
                    await candidate.fill(character)
                return
        await locator.fill(code)

    async def _click_continue(self, page) -> None:
        candidates = [
            page.locator('button[type="submit"]').filter(
                has_text=re.compile(r"continue|next|log in|sign in", re.I)
            ).first,
            page.get_by_role("button", name=re.compile(r"continue|next|log in|sign in", re.I)).first,
            page.locator('button[name="intent"][value="validate"]').first,
            page.locator('button[type="submit"]').first,
        ]
        await self._click_first_visible(candidates, "continue button")

    async def _password_required(self, page) -> bool:
        inputs = page.locator('input[type="password"]')
        count = await inputs.count()
        for index in range(count):
            if await inputs.nth(index).is_visible():
                return True
        return False

    async def _has_deactive_marker(self, page) -> bool:
        try:
            text = await page.locator("body").inner_text(timeout=3000)
        except Exception:
            return False
        return looks_deactive_text(text)

    async def _wait_for_cloudflare_to_clear(self, page, timeout_ms: int = 45_000) -> None:
        deadline = asyncio.get_running_loop().time() + timeout_ms / 1000
        while asyncio.get_running_loop().time() < deadline:
            try:
                text = await page.locator("body").inner_text(timeout=3000)
            except Exception:
                text = ""
            lowered = text.lower()
            if "performing security verification" not in lowered and "verify you are not a bot" not in lowered and "cloudflare" not in lowered:
                return
            await page.wait_for_timeout(1500)
        raise RuntimeError("Cloudflare challenge did not clear before ChatGPT login.")

    async def _has_email_input(self, page, timeout_ms: int) -> bool:
        try:
            await self._first_visible(self._email_input_candidates(page), "email input", timeout_ms=timeout_ms)
            return True
        except RuntimeError:
            return False

    def _email_input_candidates(self, page) -> list[Any]:
        return [
            page.locator("input#email").first,
            page.locator('input[name="email"]').first,
            page.locator('input[name="username"]').first,
            page.locator('input[autocomplete*="email"]').first,
            page.locator('input[autocomplete*="username"]').first,
            page.locator('input[type="email"]').first,
            page.locator('input[placeholder*="Email" i]').first,
            page.locator('input[id*="email" i]').first,
        ]

    async def _has_code_input(self, page, timeout_ms: int) -> bool:
        try:
            await self._first_visible(self._code_input_candidates(page), "verification code input", timeout_ms=timeout_ms)
            return True
        except RuntimeError:
            return False

    def _code_input_candidates(self, page) -> list[Any]:
        return [
            page.locator('input[autocomplete="one-time-code"]').first,
            page.locator('input[name="code"]').first,
            page.locator('input[placeholder*="Code" i]').first,
            page.locator('input[aria-label*="code" i]').first,
            page.locator('input[type="tel"]').first,
            page.locator('input[type="text"]').first,
        ]

    async def _first_visible(self, candidates, description: str, timeout_ms: int | None = None):
        timeout = timeout_ms if timeout_ms is not None else self.settings.playwright_timeout_ms
        deadline = asyncio.get_running_loop().time() + timeout / 1000
        while asyncio.get_running_loop().time() < deadline:
            for locator in candidates:
                try:
                    if await locator.is_visible(timeout=1000):
                        return locator
                except Exception:
                    continue
            await asyncio.sleep(0.25)
        raise RuntimeError(f"Could not find visible {description}.")

    async def _click_first_visible(self, candidates, description: str) -> None:
        locator = await self._first_visible(candidates, description)
        await locator.click()

    def _extract_session_payload(self, body: str) -> dict[str, Any] | None:
        try:
            data = json.loads(body.strip())
        except json.JSONDecodeError:
            return None
        return data if isinstance(data, dict) else None

    def _extract_access_token(self, body: str, session: dict[str, Any] | None = None) -> str | None:
        data = session
        text = body.strip()
        if data is None:
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError:
                parsed = None
            data = parsed if isinstance(parsed, dict) else None
        if data is None:
            match = re.search(r'"accessToken"\s*:\s*"([^"]+)"', text)
            return match.group(1) if match else None
        token = data.get("accessToken") or data.get("access_token")
        return str(token) if token else None
