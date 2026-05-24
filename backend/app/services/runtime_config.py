from __future__ import annotations

import asyncio
import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.parse import urlparse, urlunparse

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.crypto import decrypt_text, encrypt_text, redact
from app.core.database import AsyncSessionLocal
from app.models import AppSetting, utcnow


KEY_SUB2API_BASE_URL = "sub2api_base_url"
KEY_SUB2API_BASE_URL_SOURCE = "sub2api_base_url_source"
KEY_SUB2API_X_API_KEY = "sub2api_x_api_key"
KEY_MONITOR_INTERVAL_SECONDS = "monitor_interval_seconds"
KEY_USAGE_REFRESH_ENABLED = "usage_refresh_enabled"
KEY_USAGE_REFRESH_INTERVAL_SECONDS = "usage_refresh_interval_seconds"
KEY_REFRESH_MAX_CONCURRENCY = "refresh_max_concurrency"
KEY_LAST_SCAN_AT = "sub2api_last_scan_at"
KEY_LAST_SCAN_STATUS = "sub2api_last_scan_status"
KEY_LAST_SCAN_MESSAGE = "sub2api_last_scan_message"
KEY_DISPLAY_TIMEZONE = "display_timezone"
KEY_SITE_NAME = "site_name"
SUB2API_API_PREFIX = "/api/v1"


@dataclass(frozen=True)
class EffectiveSub2ApiConfig:
    base_url: str
    auth_token: str
    auth_header: str
    auth_scheme: str
    accounts_path: str
    access_token_path: str
    auto_clear_error: bool
    auto_recover_state: bool


@dataclass(frozen=True)
class ProbeHit:
    base_url: str
    port: int
    status: str
    message: str


class RuntimeConfigService:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    async def get_sub2api_config(self) -> EffectiveSub2ApiConfig:
        values = await self._load_values()
        runtime_key = decrypt_text(values.get(KEY_SUB2API_X_API_KEY))
        if runtime_key:
            auth_token = runtime_key
            auth_header = "x-api-key"
            auth_scheme = ""
        else:
            auth_token = self.settings.sub2api_auth_token
            auth_header = self.settings.sub2api_auth_header
            auth_scheme = self.settings.sub2api_auth_scheme

        return EffectiveSub2ApiConfig(
            base_url=_normalize_base_url(values.get(KEY_SUB2API_BASE_URL) or self.settings.sub2api_base_url),
            auth_token=auth_token,
            auth_header=auth_header,
            auth_scheme=auth_scheme,
            accounts_path=self.settings.sub2api_accounts_path,
            access_token_path=self.settings.sub2api_access_token_path,
            auto_clear_error=self.settings.sub2api_auto_clear_error,
            auto_recover_state=self.settings.sub2api_auto_recover_state,
        )

    async def get_monitor_interval_seconds(self) -> int:
        values = await self._load_values()
        return _int_or_default(values.get(KEY_MONITOR_INTERVAL_SECONDS), self.settings.monitor_interval_seconds)

    async def get_usage_refresh_enabled(self) -> bool:
        values = await self._load_values()
        return _bool_or_default(values.get(KEY_USAGE_REFRESH_ENABLED), self.settings.usage_refresh_enabled)

    async def get_usage_refresh_interval_seconds(self) -> int:
        values = await self._load_values()
        return _int_or_default(
            values.get(KEY_USAGE_REFRESH_INTERVAL_SECONDS),
            self.settings.usage_refresh_interval_seconds,
        )

    async def get_refresh_max_concurrency(self) -> int:
        values = await self._load_values()
        return _bounded_int_or_default(
            values.get(KEY_REFRESH_MAX_CONCURRENCY),
            self.settings.refresh_max_concurrency,
            1,
            50,
        )

    async def get_public_settings(self) -> dict:
        values = await self._load_values()
        base_url = _normalize_base_url(values.get(KEY_SUB2API_BASE_URL) or self.settings.sub2api_base_url)
        runtime_key = decrypt_text(values.get(KEY_SUB2API_X_API_KEY))
        env_x_api_key = self._env_x_api_key()
        effective_x_api_key = runtime_key or env_x_api_key
        return {
            "sub2api_base_url": base_url,
            "sub2api_port": _port_from_url(base_url),
            "sub2api_base_url_source": values.get(KEY_SUB2API_BASE_URL_SOURCE) or "env",
            "sub2api_x_api_key_set": bool(effective_x_api_key),
            "sub2api_x_api_key_hint": redact(effective_x_api_key) if effective_x_api_key else None,
            "monitor_interval_seconds": _int_or_default(
                values.get(KEY_MONITOR_INTERVAL_SECONDS),
                self.settings.monitor_interval_seconds,
            ),
            "usage_refresh_enabled": _bool_or_default(
                values.get(KEY_USAGE_REFRESH_ENABLED),
                self.settings.usage_refresh_enabled,
            ),
            "usage_refresh_interval_seconds": _int_or_default(
                values.get(KEY_USAGE_REFRESH_INTERVAL_SECONDS),
                self.settings.usage_refresh_interval_seconds,
            ),
            "refresh_max_concurrency": _bounded_int_or_default(
                values.get(KEY_REFRESH_MAX_CONCURRENCY),
                self.settings.refresh_max_concurrency,
                1,
                50,
            ),
            "last_scan_at": _datetime_or_none(values.get(KEY_LAST_SCAN_AT)),
            "last_scan_status": values.get(KEY_LAST_SCAN_STATUS),
            "last_scan_message": values.get(KEY_LAST_SCAN_MESSAGE),
            "display_timezone": _clean_timezone(values.get(KEY_DISPLAY_TIMEZONE) or self.settings.display_timezone),
            "site_name": _clean_site_name(values.get(KEY_SITE_NAME) or self.settings.app_name, self.settings.app_name),
        }

    async def update_public_settings(self, payload: dict) -> dict:
        current_values = await self._load_values()
        current_base_url = _normalize_base_url(
            current_values.get(KEY_SUB2API_BASE_URL) or self.settings.sub2api_base_url
        )

        async with AsyncSessionLocal() as db:
            if payload.get("sub2api_base_url") is not None or payload.get("sub2api_port") is not None:
                next_base_url = _normalize_base_url(payload.get("sub2api_base_url") or current_base_url)
                if payload.get("sub2api_port") is not None:
                    next_base_url = _replace_port(next_base_url, int(payload["sub2api_port"]))
                await self._put(db, KEY_SUB2API_BASE_URL, next_base_url)
                await self._put(db, KEY_SUB2API_BASE_URL_SOURCE, "manual")

            if payload.get("monitor_interval_seconds") is not None:
                await self._put(db, KEY_MONITOR_INTERVAL_SECONDS, str(int(payload["monitor_interval_seconds"])))

            if payload.get("usage_refresh_enabled") is not None:
                await self._put(db, KEY_USAGE_REFRESH_ENABLED, "true" if payload["usage_refresh_enabled"] else "false")

            if payload.get("usage_refresh_interval_seconds") is not None:
                await self._put(
                    db,
                    KEY_USAGE_REFRESH_INTERVAL_SECONDS,
                    str(int(payload["usage_refresh_interval_seconds"])),
                )

            if payload.get("refresh_max_concurrency") is not None:
                await self._put(db, KEY_REFRESH_MAX_CONCURRENCY, str(int(payload["refresh_max_concurrency"])))

            if payload.get("display_timezone") is not None:
                await self._put(db, KEY_DISPLAY_TIMEZONE, _clean_timezone(str(payload["display_timezone"])))

            if payload.get("site_name") is not None:
                await self._put(db, KEY_SITE_NAME, _clean_site_name(str(payload["site_name"]), self.settings.app_name))

            if payload.get("clear_sub2api_x_api_key"):
                await self._delete(db, KEY_SUB2API_X_API_KEY)
            else:
                raw_key = payload.get("sub2api_x_api_key")
                if isinstance(raw_key, str) and raw_key.strip():
                    await self._put(db, KEY_SUB2API_X_API_KEY, encrypt_text(raw_key.strip()))

            await db.commit()

        return await self.get_public_settings()

    async def scan_sub2api_ports(self, apply: bool = False) -> dict:
        config = await self.get_sub2api_config()
        checked_ports = self._candidate_ports(config.base_url)
        hit = await self._find_sub2api(checked_ports, config)

        if hit:
            status = "found"
            message = hit.message
            base_url = hit.base_url
            port = hit.port
        else:
            status = "not_found"
            message = "未在候选端口发现 sub2api，请手动设置端口或调整 SUB2API_SCAN_PORTS。"
            base_url = None
            port = None

        async with AsyncSessionLocal() as db:
            if hit and apply:
                await self._put(db, KEY_SUB2API_BASE_URL, hit.base_url)
                await self._put(db, KEY_SUB2API_BASE_URL_SOURCE, "auto")
            await self._put(db, KEY_LAST_SCAN_AT, utcnow().isoformat())
            await self._put(db, KEY_LAST_SCAN_STATUS, status)
            await self._put(db, KEY_LAST_SCAN_MESSAGE, message)
            await db.commit()

        return {
            "found": bool(hit),
            "base_url": base_url,
            "port": port,
            "status": status,
            "message": message,
            "checked_ports": checked_ports,
            "applied": bool(hit and apply),
        }

    async def auto_detect_sub2api(self) -> None:
        values = await self._load_values()
        if values.get(KEY_SUB2API_BASE_URL_SOURCE) == "manual":
            return
        base_url = _normalize_base_url(values.get(KEY_SUB2API_BASE_URL) or self.settings.sub2api_base_url)
        if not _is_local_url(base_url):
            return
        await self.scan_sub2api_ports(apply=True)

    def _candidate_ports(self, current_base_url: str) -> list[int]:
        ports: list[int] = []
        current_port = _port_from_url(current_base_url)
        if current_port:
            ports.append(current_port)
        ports.extend(self.settings.sub2api_scan_ports)
        ports.extend(_local_listening_ports())
        return list(dict.fromkeys(port for port in ports if 0 < int(port) <= 65535))

    async def _find_sub2api(
        self,
        ports: list[int],
        config: EffectiveSub2ApiConfig,
    ) -> ProbeHit | None:
        headers = _headers_for_probe(config)
        timeout = httpx.Timeout(self.settings.sub2api_scan_timeout_seconds)
        async with httpx.AsyncClient(
            timeout=timeout,
            headers=headers,
            follow_redirects=False,
            trust_env=False,
        ) as client:
            tasks = [asyncio.create_task(self._probe_port(client, port)) for port in ports]
            try:
                for finished in asyncio.as_completed(tasks):
                    hit = await finished
                    if hit is not None:
                        for task in tasks:
                            if not task.done():
                                task.cancel()
                        return hit
            finally:
                await asyncio.gather(*tasks, return_exceptions=True)
        return None

    async def _probe_port(self, client: httpx.AsyncClient, port: int) -> ProbeHit | None:
        root = f"http://127.0.0.1:{port}"
        base_url = f"{root}/api/v1"
        accounts_url = f"{base_url}/admin/accounts"
        try:
            response = await client.get(accounts_url)
            if response.status_code in {401, 403} and _looks_like_sub2api_auth_response(response):
                return ProbeHit(
                    base_url=base_url,
                    port=port,
                    status=str(response.status_code),
                    message=f"已检测到 {base_url}（/admin/accounts 返回 {response.status_code}）。",
                )
            if response.status_code == 200 and _looks_like_sub2api_accounts_response(response):
                return ProbeHit(
                    base_url=base_url,
                    port=port,
                    status=str(response.status_code),
                    message=f"已检测到 {base_url}（/admin/accounts 返回账号 JSON）。",
                )
        except httpx.HTTPError:
            pass

        try:
            response = await client.get(f"{root}/openapi.json")
            if response.status_code == 200 and _looks_like_sub2api(response.text):
                return ProbeHit(
                    base_url=base_url,
                    port=port,
                    status="openapi",
                    message=f"已检测到 {base_url}（OpenAPI 匹配 sub2api）。",
                )
        except httpx.HTTPError:
            pass

        return None

    async def _load_values(self) -> dict[str, str | None]:
        async with AsyncSessionLocal() as db:
            result = await db.execute(select(AppSetting))
            return {item.key: item.value for item in result.scalars().all()}

    async def _put(self, db: AsyncSession, key: str, value: str | None) -> None:
        setting = await db.get(AppSetting, key)
        if setting is None:
            setting = AppSetting(key=key)
            db.add(setting)
        setting.value = value

    async def _delete(self, db: AsyncSession, key: str) -> None:
        setting = await db.get(AppSetting, key)
        if setting is not None:
            await db.delete(setting)

    def _env_x_api_key(self) -> str:
        header = self.settings.sub2api_auth_header.strip().lower()
        if header in {"x-api-key", "x-api", "x-api_key"}:
            return self.settings.sub2api_auth_token.strip()
        return ""


def _headers_for_probe(config: EffectiveSub2ApiConfig) -> dict[str, str]:
    headers = {"Accept": "application/json"}
    token = config.auth_token.strip()
    if token:
        scheme = config.auth_scheme.strip()
        value = f"{scheme} {token}".strip() if scheme else token
        headers[config.auth_header] = value
    return headers


def _normalize_base_url(value: str) -> str:
    return f"{_normalize_instance_url(value)}{SUB2API_API_PREFIX}"


def _normalize_instance_url(value: str) -> str:
    text = value.strip().rstrip("/")
    if not text:
        text = "http://localhost:8080"
    if "://" not in text:
        text = f"http://{text}"
    parsed = urlparse(text)
    path = _strip_api_prefix(parsed.path)
    return urlunparse((parsed.scheme or "http", parsed.netloc, path, "", "", "")).rstrip("/")


def _strip_api_prefix(path: str) -> str:
    clean_path = (path or "").strip().rstrip("/")
    if not clean_path or clean_path == "/":
        return ""
    if clean_path.lower() == SUB2API_API_PREFIX:
        return ""
    if clean_path.lower().endswith(SUB2API_API_PREFIX):
        return clean_path[: -len(SUB2API_API_PREFIX)].rstrip("/")
    return clean_path


def _replace_port(base_url: str, port: int) -> str:
    parsed = urlparse(_normalize_instance_url(base_url))
    host = parsed.hostname or "localhost"
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    netloc = f"{host}:{port}"
    instance_url = urlunparse((parsed.scheme or "http", netloc, parsed.path.rstrip("/"), "", "", "")).rstrip("/")
    return f"{instance_url}{SUB2API_API_PREFIX}"


def _port_from_url(value: str) -> int | None:
    parsed = urlparse(value)
    try:
        if parsed.port:
            return parsed.port
    except ValueError:
        return None
    if parsed.scheme == "http":
        return 80
    if parsed.scheme == "https":
        return 443
    return None


def _int_or_default(value: str | None, default: int) -> int:
    try:
        return int(value) if value is not None else default
    except (TypeError, ValueError):
        return default


def _bounded_int_or_default(value: str | None, default: int, minimum: int, maximum: int) -> int:
    result = _int_or_default(value, default)
    if result < minimum:
        return minimum
    if result > maximum:
        return maximum
    return result


def _bool_or_default(value: str | None, default: bool) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _datetime_or_none(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _is_local_url(value: str) -> bool:
    host = (urlparse(value).hostname or "").lower()
    return host in {"localhost", "127.0.0.1", "::1"}


def _looks_like_sub2api(text: str) -> bool:
    lowered = text.lower()
    if "at guardian" in lowered:
        return False
    return "sub2api" in lowered and ("admin/accounts" in lowered or "accounts" in lowered)


def _clean_timezone(value: str | None) -> str:
    text = (value or "").strip()
    if text and re.fullmatch(r"[A-Za-z0-9_+./:-]{1,80}", text):
        return text
    return "Asia/Shanghai"


def _clean_site_name(value: str | None, default: str) -> str:
    text = re.sub(r"\s+", " ", (value or "").strip())
    text = "".join(char for char in text if char.isprintable())
    if not text:
        return default
    return text[:80]


def _looks_like_sub2api_accounts_response(response: httpx.Response) -> bool:
    content_type = response.headers.get("content-type", "").lower()
    if "json" not in content_type:
        return False
    try:
        payload = response.json()
    except ValueError:
        return False
    return _looks_like_sub2api_accounts_payload(payload)


def _looks_like_sub2api_auth_response(response: httpx.Response) -> bool:
    text = response.text.lower()
    if "authorization required" in text or "unauthorized" in text:
        return True
    try:
        payload = response.json()
    except ValueError:
        return False
    if not isinstance(payload, dict) or not payload:
        return False
    code = str(payload.get("code") or payload.get("error") or "").lower()
    message = str(payload.get("message") or payload.get("detail") or "").lower()
    return "unauthorized" in code or "authorization" in message or "api" in message


def _looks_like_sub2api_accounts_payload(payload: Any) -> bool:
    if isinstance(payload, list):
        return True
    if not isinstance(payload, dict):
        return False
    if _contains_account_list(payload):
        return True
    data = payload.get("data")
    if isinstance(data, list):
        return True
    if isinstance(data, dict) and _contains_account_list(data):
        return True
    keys = {str(key).lower() for key in payload.keys()}
    return {"code", "message", "data"} <= keys and data is None


def _contains_account_list(payload: dict[str, Any]) -> bool:
    for key in ("items", "records", "accounts", "list"):
        if isinstance(payload.get(key), list):
            return True
    return False


def _local_listening_ports() -> list[int]:
    commands = (
        [["netstat", "-ano", "-p", "tcp"]]
        if sys.platform.startswith("win")
        else [["ss", "-ltn"], ["netstat", "-ltn"]]
    )
    for command in commands:
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                encoding="utf-8",
                errors="ignore",
                text=True,
                timeout=1.5,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        ports = _parse_listening_ports(completed.stdout)
        if ports:
            return ports
    return []


def _parse_listening_ports(output: str) -> list[int]:
    ports: list[int] = []
    for raw_line in output.splitlines():
        line = raw_line.strip()
        lowered = line.lower()
        if "listen" not in lowered:
            continue
        parts = line.split()
        local_address = _pick_local_address(parts)
        if not local_address:
            continue
        port = _port_from_socket_address(local_address)
        if port:
            ports.append(port)
    return list(dict.fromkeys(ports))


def _pick_local_address(parts: list[str]) -> str | None:
    if not parts:
        return None
    if parts[0].lower().startswith("tcp") and len(parts) >= 2:
        if len(parts) >= 4 and parts[1].isdigit() and parts[2].isdigit():
            return parts[3]
        return parts[1]
    if parts[0].lower() == "listen" and len(parts) >= 4:
        return parts[3]
    return None


def _port_from_socket_address(value: str) -> int | None:
    match = re.search(r":(\d+)$", value)
    if not match:
        match = re.search(r"\]:(\d+)$", value)
    if not match:
        return None
    try:
        return int(match.group(1))
    except ValueError:
        return None


_runtime_config_service: RuntimeConfigService | None = None


def get_runtime_config_service() -> RuntimeConfigService:
    global _runtime_config_service
    if _runtime_config_service is None:
        _runtime_config_service = RuntimeConfigService()
    return _runtime_config_service
