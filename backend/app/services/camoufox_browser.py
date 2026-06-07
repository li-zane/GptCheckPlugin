from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Any

from app.core.config import Settings, get_settings


@dataclass
class BrowserContextHandle:
    context: Any
    browser: Any | None = None
    engine: str = "playwright"

    async def close(self) -> None:
        await self.context.close()
        if self.browser is not None:
            await self.browser.close()


def browser_profile_dir(email: str, settings: Settings | None = None) -> str:
    active_settings = settings or get_settings()
    slug = re.sub(r"[^a-z0-9._-]+", "_", email.lower()).strip("._-") or "default"
    path = active_settings.project_root / "data" / "browser-profiles" / slug
    path.mkdir(parents=True, exist_ok=True)
    return str(path)


async def launch_browser_context(playwright: Any, email: str, settings: Settings | None = None) -> BrowserContextHandle:
    active_settings = settings or get_settings()
    try:
        from camoufox.addons import DefaultAddons
        from camoufox.pkgman import launch_path
        from camoufox.utils import launch_options

        camoufox_options = launch_options(
            executable_path=launch_path(),
            headless=active_settings.playwright_headless,
            # Keep DISPLAY/XAUTHORITY and other runtime env so headed Camoufox can run under xvfb.
            env=dict(os.environ),
            exclude_addons=[DefaultAddons.UBO],
            os="windows",
            locale="en-US",
            humanize=1.8,
            enable_cache=False,
            block_webrtc=True,
            block_webgl=False,
        )
        if active_settings.playwright_slow_mo_ms > 0:
            camoufox_options["slow_mo"] = active_settings.playwright_slow_mo_ms
        context = await playwright.firefox.launch_persistent_context(
            browser_profile_dir(email, active_settings),
            **camoufox_options,
        )
        return BrowserContextHandle(context=context, engine="camoufox")
    except Exception:
        browser = await playwright.chromium.launch(
            headless=active_settings.playwright_headless,
            slow_mo=active_settings.playwright_slow_mo_ms,
        )
        context = await browser.new_context(locale="en-US")
        return BrowserContextHandle(context=context, browser=browser, engine="chromium")
