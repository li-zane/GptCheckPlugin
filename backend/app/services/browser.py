import asyncio
import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime

from app.core.config import Settings, get_settings


FetchCode = Callable[[datetime], Awaitable[str | None]]


@dataclass
class BrowserRefreshOutcome:
    status: str
    access_token: str | None = None
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
            browser = await p.chromium.launch(
                headless=self.settings.playwright_headless,
                slow_mo=self.settings.playwright_slow_mo_ms,
            )
            context = await browser.new_context(locale="zh-CN")
            page = await context.new_page()
            page.set_default_timeout(self.settings.playwright_timeout_ms)
            try:
                await page.goto(self.settings.chatgpt_base_url, wait_until="domcontentloaded")
                await self._click_login(page)
                await self._fill_email(page, email)
                requested_at = datetime.now().astimezone()
                await self._click_continue(page)
                await page.wait_for_timeout(1500)

                if await self._has_deactive_marker(page):
                    return BrowserRefreshOutcome(status="deactive", error="account_deactive was detected during login.")

                if await self._password_required(page):
                    return BrowserRefreshOutcome(
                        status="failed",
                        error="Password login is required; this plugin currently supports email verification code login.",
                    )

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
                if "account_deactive" in body.lower():
                    return BrowserRefreshOutcome(status="deactive", error="account_deactive was returned by session endpoint.")

                token = self._extract_access_token(body)
                if not token:
                    return BrowserRefreshOutcome(status="failed", error="Session endpoint did not include accessToken.")
                return BrowserRefreshOutcome(status="ok", access_token=token)
            except Exception as exc:
                return BrowserRefreshOutcome(status="failed", error=f"Browser refresh failed: {exc}")
            finally:
                await context.close()
                await browser.close()

    async def _click_login(self, page) -> None:
        candidates = [
            page.locator('[data-testid="login-button"]').first,
            page.get_by_role("button", name=re.compile(r"登录|log in|login", re.I)).first,
            page.locator("button").filter(has_text=re.compile(r"登录|log in|login", re.I)).first,
        ]
        await self._click_first_visible(candidates, "login button")

    async def _fill_email(self, page, email: str) -> None:
        candidates = [
            page.locator("input#email").first,
            page.locator('input[name="email"]').first,
            page.locator('input[autocomplete*="email"]').first,
            page.locator('input[type="email"]').first,
        ]
        locator = await self._first_visible(candidates, "email input")
        await locator.fill(email)

    async def _fill_code(self, page, code: str) -> None:
        candidates = [
            page.locator('input[autocomplete="one-time-code"]').first,
            page.locator('input[name="code"]').first,
            page.locator('input[aria-label*="验证码"]').first,
            page.locator('input[aria-label*="code" i]').first,
            page.locator('input[type="tel"]').first,
            page.locator('input[type="text"]').first,
        ]
        locator = await self._first_visible(candidates, "verification code input")
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
            page.locator('button[type="submit"]').filter(has_text=re.compile(r"继续|continue", re.I)).first,
            page.get_by_role("button", name=re.compile(r"继续|continue", re.I)).first,
            page.locator('button[name="intent"][value="validate"]').first,
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
            text = (await page.locator("body").inner_text(timeout=3000)).lower()
        except Exception:
            return False
        return "account_deactive" in text or "account deactive" in text or "account deactivated" in text

    async def _first_visible(self, candidates, description: str):
        deadline = asyncio.get_running_loop().time() + self.settings.playwright_timeout_ms / 1000
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

    def _extract_access_token(self, body: str) -> str | None:
        text = body.strip()
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            match = re.search(r'"accessToken"\s*:\s*"([^"]+)"', text)
            return match.group(1) if match else None
        token = data.get("accessToken") or data.get("access_token")
        return str(token) if token else None
