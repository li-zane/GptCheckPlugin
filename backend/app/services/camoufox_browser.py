from __future__ import annotations

import hashlib
import os
import re
import tempfile
import time
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
    normalized_email = email.strip().lower()
    owner_digest = hashlib.sha256(normalized_email.encode("utf-8")).hexdigest()
    readable = re.sub(r"[^a-z0-9._-]+", "_", normalized_email).strip("._-")[:48] or "account"
    slug = f"{readable}-{owner_digest[:16]}"
    path = active_settings.project_root / "data" / "browser-profiles" / slug
    path.mkdir(parents=True, exist_ok=True)
    _ensure_profile_owner(path / ".account-owner", owner_digest)
    return str(path)


def _ensure_profile_owner(owner_marker: Any, owner_digest: str) -> None:
    for attempt in range(4):
        try:
            recorded_owner = owner_marker.read_text(encoding="ascii").strip()
        except FileNotFoundError:
            recorded_owner = None
        except (OSError, UnicodeError) as exc:
            raise RuntimeError("Browser profile ownership marker is unreadable.") from exc

        if recorded_owner == owner_digest:
            return
        if recorded_owner:
            raise RuntimeError("Browser profile ownership does not match the requested account.")
        if recorded_owner == "":
            if attempt == 0:
                time.sleep(0.05)
                continue
            try:
                owner_marker.unlink()
            except FileNotFoundError:
                pass
            except OSError as exc:
                raise RuntimeError("Browser profile ownership marker cannot be recovered.") from exc

        descriptor, temporary_name = tempfile.mkstemp(
            dir=owner_marker.parent,
            prefix=".account-owner.",
        )
        try:
            with os.fdopen(descriptor, "w", encoding="ascii") as handle:
                handle.write(f"{owner_digest}\n")
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.link(temporary_name, owner_marker)
            except FileExistsError:
                continue
            return
        finally:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass
    raise RuntimeError("Browser profile ownership marker could not be established.")


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
