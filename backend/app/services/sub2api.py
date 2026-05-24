import copy
import re
from typing import Any

import httpx

from app.core.config import Settings, get_settings


EMAIL_RE = re.compile(r"[A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,}", re.I)


def _deep_get(data: Any, path: str) -> Any:
    current = data
    for part in path.split("."):
        if isinstance(current, dict):
            current = current.get(part)
        else:
            return None
    return current


def _deep_set(data: dict[str, Any], path: str, value: Any) -> None:
    current = data
    parts = path.split(".")
    for part in parts[:-1]:
        next_value = current.get(part)
        if not isinstance(next_value, dict):
            next_value = {}
            current[part] = next_value
        current = next_value
    current[parts[-1]] = value


def find_email(value: Any) -> str | None:
    if isinstance(value, dict):
        priority_keys = ("email", "account", "username", "user", "login")
        for key in priority_keys:
            candidate = value.get(key)
            if isinstance(candidate, str):
                match = EMAIL_RE.search(candidate)
                if match:
                    return match.group(0).lower()
        for child in value.values():
            found = find_email(child)
            if found:
                return found
    elif isinstance(value, list):
        for child in value:
            found = find_email(child)
            if found:
                return found
    elif isinstance(value, str):
        match = EMAIL_RE.search(value)
        if match:
            return match.group(0).lower()
    return None


def sanitize_payload(value: Any) -> Any:
    if isinstance(value, dict):
        sanitized: dict[str, Any] = {}
        for key, child in value.items():
            lowered = str(key).lower()
            if any(marker in lowered for marker in ("token", "secret", "password", "cookie", "authorization")):
                sanitized[key] = "***redacted***" if child else child
            else:
                sanitized[key] = sanitize_payload(child)
        return sanitized
    if isinstance(value, list):
        return [sanitize_payload(child) for child in value]
    return value


class Sub2ApiClient:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        token = self.settings.sub2api_auth_token.strip()
        if token:
            scheme = self.settings.sub2api_auth_scheme.strip()
            value = f"{scheme} {token}".strip() if scheme else token
            headers[self.settings.sub2api_auth_header] = value
        return headers

    def _url(self, path: str) -> str:
        return f"{self.settings.sub2api_base_url}{path}"

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        async with httpx.AsyncClient(timeout=30.0, headers=self._headers()) as client:
            response = await client.request(method, self._url(path), **kwargs)
            response.raise_for_status()
            if not response.content:
                return None
            return response.json()

    def _unwrap(self, payload: Any) -> Any:
        if isinstance(payload, dict):
            if isinstance(payload.get("data"), list):
                return payload["data"]
            if isinstance(payload.get("data"), dict):
                data = payload["data"]
                for key in ("items", "records", "accounts", "list"):
                    if isinstance(data.get(key), list):
                        return data[key]
                return data
            for key in ("items", "records", "accounts", "list"):
                if isinstance(payload.get(key), list):
                    return payload[key]
        return payload

    async def list_accounts(self) -> list[dict[str, Any]]:
        payload = await self._request("GET", self.settings.sub2api_accounts_path)
        accounts = self._unwrap(payload)
        if isinstance(accounts, list):
            return [item for item in accounts if isinstance(item, dict)]
        return []

    async def update_access_token(self, account: dict[str, Any], access_token: str) -> None:
        account_id = self.account_id(account)
        if account_id is None:
            raise ValueError("Cannot update sub2api account without id.")

        token_path = self.settings.sub2api_access_token_path
        payload: dict[str, Any]
        if token_path.startswith("credentials."):
            credentials = copy.deepcopy(account.get("credentials") or {})
            _deep_set({"credentials": credentials}, token_path, access_token)
            payload = {"credentials": credentials}
        else:
            payload = {}
            _deep_set(payload, token_path, access_token)

        await self._request("PUT", f"{self.settings.sub2api_accounts_path}/{account_id}", json=payload)

        if self.settings.sub2api_auto_clear_error:
            await self._try_post(f"{self.settings.sub2api_accounts_path}/{account_id}/clear-error")
        if self.settings.sub2api_auto_recover_state:
            await self._try_post(f"{self.settings.sub2api_accounts_path}/{account_id}/recover-state")

    async def _try_post(self, path: str) -> None:
        try:
            await self._request("POST", path)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code not in {404, 405}:
                raise

    def account_id(self, account: dict[str, Any]) -> str | None:
        for key in ("id", "account_id", "accountId"):
            value = account.get(key)
            if value is not None:
                return str(value)
        return None

    def account_email(self, account: dict[str, Any]) -> str | None:
        return find_email(account)

    def account_status(self, account: dict[str, Any]) -> str | None:
        for key in ("status", "state", "account_status"):
            value = account.get(key)
            if value is not None:
                return str(value)
        return None

    def account_platform(self, account: dict[str, Any]) -> str | None:
        for key in ("platform", "provider", "service"):
            value = account.get(key)
            if value is not None:
                return str(value)
        return None

    def account_type(self, account: dict[str, Any]) -> str | None:
        for key in ("type", "account_type", "auth_type"):
            value = account.get(key)
            if value is not None:
                return str(value)
        return None

    def account_schedulable(self, account: dict[str, Any]) -> bool | None:
        value = account.get("schedulable")
        if isinstance(value, bool):
            return value
        return None

    def is_gpt_account(self, account: dict[str, Any]) -> bool:
        text = " ".join(
            str(item).lower()
            for item in [
                self.account_platform(account),
                self.account_type(account),
                _deep_get(account, "credentials.provider"),
                _deep_get(account, "credentials.platform"),
            ]
            if item is not None
        )
        if any(marker in text for marker in ("openai", "chatgpt", "gpt")):
            return True
        credentials = account.get("credentials")
        return isinstance(credentials, dict) and any(
            key in credentials for key in ("access_token", "accessToken", "refresh_token", "refreshToken")
        )

    def is_error_account(self, account: dict[str, Any]) -> bool:
        status = (self.account_status(account) or "").lower()
        if any(marker in status for marker in ("error", "fail", "invalid", "expired", "disabled")):
            return True
        schedulable = self.account_schedulable(account)
        return schedulable is False and "deactive" not in status

    def is_deactive_account(self, account: dict[str, Any]) -> bool:
        raw_text = str(account).lower()
        return "account_deactive" in raw_text or "deactive" in (self.account_status(account) or "").lower()
