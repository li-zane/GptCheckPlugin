import base64
import copy
import json
import re
from datetime import datetime, timezone
from typing import Any

import httpx

from app.core.config import Settings, get_settings
from app.services.runtime_config import EffectiveSub2ApiConfig, get_runtime_config_service


EMAIL_RE = re.compile(r"[A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,}", re.I)
DEACTIVE_MARKERS = (
    "account_deactive",
    "account_deactivated",
    "account deactive",
    "account deactivated",
    "account has been deactivated",
    "account is deactivated",
    "openai account has been deactivated",
    "账号已停用",
    "账户已停用",
    "账号被停用",
    "账户被停用",
)
ACCOUNT_EMAIL_PATHS = (
    ("credentials", "email"),
    ("credentials", "account_email"),
    ("credentials", "account"),
    ("credentials", "username"),
    ("credentials", "user", "email"),
    ("credentials", "user"),
    ("credentials", "login"),
    ("extra", "email"),
    ("extra", "account_email"),
    ("extra", "account"),
    ("extra", "username"),
    ("extra", "user", "email"),
    ("extra", "user"),
    ("extra", "login"),
    ("profile", "email"),
    ("email",),
    ("account_email",),
    ("username",),
    ("user", "email"),
    ("user",),
    ("login",),
)


class Sub2ApiRequestError(RuntimeError):
    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


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


def extract_email(value: Any) -> str | None:
    if isinstance(value, str):
        match = EMAIL_RE.search(value.strip())
        if match:
            return match.group(0).lower()
    return None


def looks_deactive_text(value: Any) -> bool:
    if value is None:
        return False
    if not isinstance(value, str):
        try:
            value = json.dumps(value, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            value = str(value)
    text = value.lower()
    return any(marker in text for marker in DEACTIVE_MARKERS)


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


SENSITIVE_CREDENTIAL_KEYS = {
    "access_token",
    "refresh_token",
    "id_token",
    "api_key",
    "session_key",
    "cookie",
    "aws_secret_access_key",
    "aws_session_token",
    "service_account_json",
    "service_account",
    "private_key",
}
REDACTED_VALUES = {"***redacted***", "[redacted]", "[REDACTED]", "***", "********"}
SUBSCRIPTION_CREDENTIAL_KEYS = ("plan_type", "subscription_expires_at")


def _path_get(data: Any, path: tuple[str, ...]) -> Any:
    current = data
    for part in path:
        if isinstance(current, dict):
            current = current.get(part)
        elif isinstance(current, list) and part.isdigit():
            index = int(part)
            if index >= len(current):
                return None
            current = current[index]
        else:
            return None
    return current


def _first_value(data: Any, *paths: tuple[str, ...]) -> Any:
    for path in paths:
        value = _path_get(data, path)
        if value is not None:
            return value
    return None


def _first_string(data: Any, *paths: tuple[str, ...]) -> str | None:
    value = _first_value(data, *paths)
    if isinstance(value, str):
        text = value.strip()
        return text or None
    return None


def _set_if_present(target: dict[str, Any], key: str, value: Any) -> None:
    if isinstance(value, str):
        value = value.strip()
    if value not in (None, ""):
        target[key] = value


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


def _unix_to_rfc3339(value: int | float) -> str:
    return datetime.fromtimestamp(float(value), timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _coerce_rfc3339(value: Any) -> str | None:
    epoch = _time_to_epoch(value)
    if epoch is not None:
        return _unix_to_rfc3339(epoch)
    if isinstance(value, str):
        text = value.strip()
        if text:
            return text
    return None


def _time_to_epoch(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        if number > 10_000_000_000:
            number = number / 1000
        return int(number)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if re.fullmatch(r"\d+(\.\d+)?", text):
            return _time_to_epoch(float(text))
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return int(parsed.timestamp())
    return None


def _credential_values_equal(key: str, current: Any, candidate: Any) -> bool:
    if key in {"expires_at", "subscription_expires_at"}:
        current_epoch = _time_to_epoch(current)
        candidate_epoch = _time_to_epoch(candidate)
        if current_epoch is not None and candidate_epoch is not None:
            return current_epoch == candidate_epoch
    return current == candidate


def _looks_redacted(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    text = value.strip()
    return text in REDACTED_VALUES or (len(text) >= 6 and set(text) <= {"*"})


def _default_organization_id(openai_auth: dict[str, Any]) -> str | None:
    poid = openai_auth.get("poid")
    if isinstance(poid, str) and poid.strip():
        return poid.strip()
    organizations = openai_auth.get("organizations")
    if not isinstance(organizations, list):
        return None
    first_id: str | None = None
    for organization in organizations:
        if not isinstance(organization, dict):
            continue
        org_id = organization.get("id")
        if not isinstance(org_id, str) or not org_id.strip():
            continue
        if first_id is None:
            first_id = org_id.strip()
        if organization.get("is_default") is True:
            return org_id.strip()
    return first_id


class Sub2ApiClient:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    def _headers(self, config: EffectiveSub2ApiConfig) -> dict[str, str]:
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        token = config.auth_token.strip()
        if token:
            scheme = config.auth_scheme.strip()
            value = f"{scheme} {token}".strip() if scheme else token
            headers[config.auth_header] = value
        return headers

    def _url(self, config: EffectiveSub2ApiConfig, path: str) -> str:
        return f"{config.base_url}{path}"

    async def _request(
        self,
        method: str,
        path: str,
        config: EffectiveSub2ApiConfig | None = None,
        **kwargs: Any,
    ) -> Any:
        active_config = config or await get_runtime_config_service().get_sub2api_config()
        async with httpx.AsyncClient(timeout=30.0, headers=self._headers(active_config), trust_env=False) as client:
            url = self._url(active_config, path)
            response = await client.request(method, url, **kwargs)
            try:
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                detail = exc.response.text[:500] if exc.response is not None else ""
                status_code = exc.response.status_code if exc.response is not None else None
                raise Sub2ApiRequestError(
                    f"sub2api request failed: HTTP {status_code} for {method} {url}. {detail}",
                    status_code=status_code,
                ) from exc
            if not response.content:
                return None
            try:
                return response.json()
            except json.JSONDecodeError as exc:
                preview = response.text[:500]
                raise Sub2ApiRequestError(f"sub2api returned non-JSON response for {method} {url}: {preview}") from exc

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
        config = await get_runtime_config_service().get_sub2api_config()
        payload = await self._request("GET", config.accounts_path, config=config)
        accounts = self._unwrap(payload)
        if isinstance(accounts, list):
            return [item for item in accounts if isinstance(item, dict)]
        return []

    async def list_groups(self) -> list[dict[str, Any]]:
        config = await get_runtime_config_service().get_sub2api_config()
        try:
            payload = await self._request("GET", "/admin/groups/all", config=config)
        except Sub2ApiRequestError as exc:
            if exc.status_code not in {404, 405}:
                raise
            payload = await self._request("GET", "/admin/groups", config=config)
        groups = self._unwrap(payload)
        if isinstance(groups, list):
            return [item for item in groups if isinstance(item, dict)]
        return []

    async def get_account_usage(self, account: dict[str, Any] | str, force: bool = True) -> dict[str, Any]:
        account_id = account if isinstance(account, str) else self.account_id(account)
        if not account_id:
            raise ValueError("Cannot read sub2api account usage without id.")

        config = await get_runtime_config_service().get_sub2api_config()
        params = {"source": "active"}
        if force:
            params["force"] = "true"
        payload = await self._request(
            "GET",
            f"{config.accounts_path}/{account_id}/usage",
            config=config,
            params=params,
        )
        usage = self._unwrap(payload)
        return usage if isinstance(usage, dict) else {}

    async def update_access_token(self, account: dict[str, Any], access_token: str) -> None:
        account_id = self.account_id(account)
        if account_id is None:
            raise ValueError("Cannot update sub2api account without id.")

        config = await get_runtime_config_service().get_sub2api_config()
        token_path = config.access_token_path
        payload: dict[str, Any]
        if token_path == "credentials.access_token":
            await self._update_credentials_patch(account, {"access_token": access_token}, config)
            await self._after_account_credentials_update(account_id, config)
            return
        if token_path.startswith("credentials."):
            credentials = copy.deepcopy(account.get("credentials") or {})
            _deep_set({"credentials": credentials}, token_path, access_token)
            payload = {"credentials": credentials}
        else:
            payload = {}
            _deep_set(payload, token_path, access_token)

        await self._request("PUT", f"{config.accounts_path}/{account_id}", config=config, json=payload)
        await self._after_account_credentials_update(account_id, config)

    async def update_credentials_from_session(
        self,
        account: dict[str, Any],
        session: dict[str, Any] | None,
        access_token: str,
    ) -> list[str]:
        account_id = self.account_id(account)
        if account_id is None:
            raise ValueError("Cannot update sub2api account without id.")

        credentials = self.credentials_from_session(session, access_token)
        if not credentials.get("access_token"):
            raise ValueError("Session endpoint did not include a usable access token.")

        changes = self.changed_credentials(account, credentials)
        config = await get_runtime_config_service().get_sub2api_config()
        if changes:
            await self._update_credentials_patch(account, changes, config)
            self._merge_account_credentials(account, changes)
        await self._after_account_credentials_update(account_id, config)
        return sorted(changes)

    async def reassert_subscription_state_from_session(
        self,
        account: dict[str, Any],
        session: dict[str, Any] | None,
        access_token: str,
    ) -> list[str]:
        account_id = self.account_id(account)
        if account_id is None:
            raise ValueError("Cannot update sub2api account without id.")

        credentials = self.subscription_credentials_from_session(session, access_token)
        if not credentials:
            return []

        config = await get_runtime_config_service().get_sub2api_config()
        await self._update_credentials_patch(account, credentials, config)
        self._merge_account_credentials(account, credentials)
        return sorted(credentials)

    async def refresh_account_usage(self, account: dict[str, Any] | str) -> bool:
        account_id = account if isinstance(account, str) else self.account_id(account)
        if not account_id:
            raise ValueError("Cannot refresh sub2api account usage without id.")

        config = await get_runtime_config_service().get_sub2api_config()
        try:
            await self._request(
                "GET",
                f"{config.accounts_path}/{account_id}/usage",
                config=config,
                params={"source": "active", "force": "true"},
            )
            return True
        except Sub2ApiRequestError as exc:
            if exc.status_code in {404, 405}:
                return False
            raise

    async def check_openai_account_status(self, account: dict[str, Any] | str) -> dict[str, Any] | None:
        account_id = account if isinstance(account, str) else self.account_id(account)
        if not account_id:
            raise ValueError("Cannot check sub2api account status without id.")

        config = await get_runtime_config_service().get_sub2api_config()
        try:
            payload = await self._request(
                "POST",
                f"{config.accounts_path}/{account_id}/check-status",
                config=config,
            )
        except Sub2ApiRequestError as exc:
            if exc.status_code in {404, 405}:
                return None
            raise

        status = self._unwrap(payload)
        return status if isinstance(status, dict) else None

    async def refresh_account_credentials(self, account: dict[str, Any] | str) -> dict[str, Any] | None:
        account_id = account if isinstance(account, str) else self.account_id(account)
        if not account_id:
            raise ValueError("Cannot refresh sub2api account credentials without id.")

        config = await get_runtime_config_service().get_sub2api_config()
        try:
            payload = await self._request(
                "POST",
                f"{config.accounts_path}/{account_id}/refresh",
                config=config,
            )
        except Sub2ApiRequestError as exc:
            if exc.status_code in {404, 405}:
                return None
            raise

        refreshed = self._unwrap(payload)
        if isinstance(refreshed, dict):
            if isinstance(account, dict):
                account.update(refreshed)
            return refreshed
        return {}

    def account_has_refresh_token(self, account: dict[str, Any]) -> bool:
        credentials_status = account.get("credentials_status")
        if isinstance(credentials_status, dict):
            for key in ("has_refresh_token", "has_refreshToken", "has_rt"):
                if credentials_status.get(key) is True:
                    return True

        credentials = account.get("credentials")
        if not isinstance(credentials, dict):
            return False
        for key in ("refresh_token", "refreshToken", "rt"):
            value = credentials.get(key)
            if isinstance(value, str) and value.strip() and not _looks_redacted(value):
                return True
        return False

    async def test_account_for_deactivation(self, account: dict[str, Any] | str) -> bool | None:
        account_id = account if isinstance(account, str) else self.account_id(account)
        if not account_id:
            raise ValueError("Cannot test sub2api account without id.")

        config = await get_runtime_config_service().get_sub2api_config()
        url = self._url(config, f"{config.accounts_path}/{account_id}/test")
        async with httpx.AsyncClient(timeout=30.0, headers=self._headers(config), trust_env=False) as client:
            try:
                async with client.stream("POST", url, json={"prompt": "hi"}) as response:
                    text = ""
                    async for chunk in response.aiter_text():
                        text += chunk
                        if looks_deactive_text(text):
                            return True
                        if len(text) >= 20_000:
                            break
                    if response.status_code in {404, 405}:
                        return None
                    if response.status_code >= 400 and looks_deactive_text(text):
                        return True
                    return False
            except httpx.HTTPStatusError as exc:
                if exc.response is not None and exc.response.status_code in {404, 405}:
                    return None
                raise

    async def delete_account(self, account: dict[str, Any] | str) -> bool:
        account_id = account if isinstance(account, str) else self.account_id(account)
        if not account_id:
            raise ValueError("Cannot delete sub2api account without id.")

        config = await get_runtime_config_service().get_sub2api_config()
        try:
            await self._request("DELETE", f"{config.accounts_path}/{account_id}", config=config)
            return True
        except Sub2ApiRequestError as exc:
            if exc.status_code == 404:
                return False
            raise

    def credentials_from_session(self, session: dict[str, Any] | None, fallback_access_token: str) -> dict[str, Any]:
        data: dict[str, Any] = session if isinstance(session, dict) else {}
        access_token = _first_string(
            data,
            ("accessToken",),
            ("access_token",),
            ("tokens", "accessToken"),
            ("tokens", "access_token"),
            ("token",),
        ) or fallback_access_token
        id_token = _first_string(data, ("idToken",), ("id_token",), ("tokens", "idToken"), ("tokens", "id_token"))
        refresh_token = _first_string(
            data,
            ("refreshToken",),
            ("refresh_token",),
            ("tokens", "refreshToken"),
            ("tokens", "refresh_token"),
        )

        access_claims = _decode_jwt_payload(access_token)
        id_claims = _decode_jwt_payload(id_token)
        openai_auth = access_claims.get("https://api.openai.com/auth")
        if not isinstance(openai_auth, dict):
            openai_auth = id_claims.get("https://api.openai.com/auth")
        if not isinstance(openai_auth, dict):
            openai_auth = {}

        credentials: dict[str, Any] = {"access_token": access_token}
        _set_if_present(credentials, "refresh_token", refresh_token)
        _set_if_present(credentials, "id_token", id_token)
        _set_if_present(credentials, "client_id", _first_string(data, ("client_id",), ("clientId",), ("tokens", "client_id")))

        expires_value = _first_value(
            data,
            ("tokens", "expires_at"),
            ("tokens", "expiresAt"),
            ("expires_at",),
            ("expiresAt",),
        )
        expires_at = _coerce_rfc3339(expires_value)
        if expires_at is None and access_claims.get("exp") is not None:
            expires_at = _coerce_rfc3339(access_claims.get("exp"))
        _set_if_present(credentials, "expires_at", expires_at)

        email = (
            _first_string(data, ("email",), ("user", "email"))
            or str(access_claims.get("email") or id_claims.get("email") or "").strip()
        )
        _set_if_present(credentials, "email", email)

        _set_if_present(
            credentials,
            "chatgpt_account_id",
            _first_string(
                data,
                ("chatgpt_account_id",),
                ("chatgptAccountId",),
                ("account_id",),
                ("accountId",),
                ("account", "id"),
                ("account", "account_id"),
                ("account", "chatgpt_account_id"),
            )
            or openai_auth.get("chatgpt_account_id"),
        )
        _set_if_present(
            credentials,
            "chatgpt_user_id",
            _first_string(
                data,
                ("chatgpt_user_id",),
                ("chatgptUserId",),
                ("user_id",),
                ("userId",),
                ("user", "id"),
            )
            or openai_auth.get("chatgpt_user_id")
            or openai_auth.get("user_id")
            or access_claims.get("sub"),
        )
        _set_if_present(
            credentials,
            "organization_id",
            _first_string(
                data,
                ("organization_id",),
                ("organizationId",),
                ("org_id",),
                ("orgId",),
                ("account", "organization_id"),
                ("account", "organizationId"),
            )
            or _default_organization_id(openai_auth),
        )
        _set_if_present(credentials, "plan_type", self.session_plan_type(data, openai_auth))
        _set_if_present(
            credentials,
            "subscription_expires_at",
            _first_string(
                data,
                ("account", "subscriptionExpiresAt"),
                ("account", "subscription_expires_at"),
                ("subscriptionExpiresAt",),
                ("subscription_expires_at",),
                ("account", "entitlement", "expires_at"),
                ("entitlement", "expires_at"),
                ("entitlement", "expiresAt"),
            ),
        )
        return credentials

    def session_plan_type(self, session: dict[str, Any], openai_auth: dict[str, Any] | None = None) -> str | None:
        plan_type = _first_string(
            session,
            ("account", "planType"),
            ("account", "plan_type"),
            ("planType",),
            ("plan_type",),
            ("entitlement", "subscription_plan"),
            ("entitlement", "subscriptionPlan"),
        )
        if plan_type:
            return plan_type

        claim_plan_type = (openai_auth or {}).get("chatgpt_plan_type")
        if isinstance(claim_plan_type, str):
            text = claim_plan_type.strip()
            return text or None
        return None

    def subscription_credentials_from_session(
        self,
        session: dict[str, Any] | None,
        access_token: str,
    ) -> dict[str, Any]:
        credentials = self.credentials_from_session(session, access_token)
        return {
            key: credentials[key]
            for key in SUBSCRIPTION_CREDENTIAL_KEYS
            if credentials.get(key) not in (None, "")
        }

    def changed_credentials(self, account: dict[str, Any], credentials: dict[str, Any]) -> dict[str, Any]:
        current = account.get("credentials")
        if not isinstance(current, dict):
            current = {}
        changes: dict[str, Any] = {}
        for key, value in credentials.items():
            if value in (None, ""):
                continue
            current_value = current.get(key)
            if key not in current:
                changes[key] = value
                continue
            if key in SENSITIVE_CREDENTIAL_KEYS and _looks_redacted(current_value):
                changes[key] = value
                continue
            if not _credential_values_equal(key, current_value, value):
                changes[key] = value
        return changes

    def _merge_account_credentials(self, account: dict[str, Any], credentials: dict[str, Any]) -> None:
        current = account.get("credentials")
        if not isinstance(current, dict):
            current = {}
            account["credentials"] = current
        current.update(credentials)

    async def _update_credentials_patch(
        self,
        account: dict[str, Any],
        credentials: dict[str, Any],
        config: EffectiveSub2ApiConfig,
    ) -> None:
        account_id = self.account_id(account)
        if account_id is None:
            raise ValueError("Cannot update sub2api account without id.")

        numeric_id = self._numeric_account_id(account_id)
        if numeric_id is not None:
            try:
                await self._request(
                    "POST",
                    f"{config.accounts_path}/bulk-update",
                    config=config,
                    json={"account_ids": [numeric_id], "credentials": credentials},
                )
                return
            except Sub2ApiRequestError as exc:
                if exc.status_code not in {404, 405}:
                    raise

        merged_credentials = copy.deepcopy(account.get("credentials") or {})
        merged_credentials.update(credentials)
        await self._request(
            "PUT",
            f"{config.accounts_path}/{account_id}",
            config=config,
            json={"credentials": merged_credentials},
        )

    async def _after_account_credentials_update(self, account_id: str, config: EffectiveSub2ApiConfig) -> None:
        if config.auto_clear_error:
            await self._try_post(f"{config.accounts_path}/{account_id}/clear-error", config)
        if config.auto_recover_state:
            await self._try_post(f"{config.accounts_path}/{account_id}/recover-state", config)
            await self._try_post(f"{config.accounts_path}/{account_id}/schedulable", config, json={"schedulable": True})
            await self._try_delete(f"{config.accounts_path}/{account_id}/temp-unschedulable", config)

    async def _try_post(self, path: str, config: EffectiveSub2ApiConfig, **kwargs: Any) -> None:
        try:
            await self._request("POST", path, config=config, **kwargs)
        except Sub2ApiRequestError as exc:
            if exc.status_code not in {404, 405}:
                raise

    async def _try_delete(self, path: str, config: EffectiveSub2ApiConfig, **kwargs: Any) -> None:
        try:
            await self._request("DELETE", path, config=config, **kwargs)
        except Sub2ApiRequestError as exc:
            if exc.status_code not in {404, 405}:
                raise

    def _numeric_account_id(self, account_id: str) -> int | None:
        try:
            return int(account_id)
        except (TypeError, ValueError):
            return None

    def account_id(self, account: dict[str, Any]) -> str | None:
        for key in ("id", "account_id", "accountId"):
            value = account.get(key)
            if value is not None:
                return str(value)
        return None

    def account_email(self, account: dict[str, Any]) -> str | None:
        for path in ACCOUNT_EMAIL_PATHS:
            email = extract_email(_path_get(account, path))
            if email:
                return email
        return None

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

    def account_access_token(self, account: dict[str, Any]) -> str | None:
        credentials = account.get("credentials")
        if not isinstance(credentials, dict):
            return None
        token = credentials.get("access_token") or credentials.get("accessToken")
        if not isinstance(token, str):
            return None
        token = token.strip()
        if not token or _looks_redacted(token):
            return None
        return token

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
        for key in ("deactive", "deactivated", "account_deactive"):
            if account.get(key) is True:
                return True

        status = (self.account_status(account) or "").strip().lower()
        if "deactive" in status or "deactivated" in status:
            return True

        schedulable = self.account_schedulable(account)
        if ("error" in status or "fail" in status or schedulable is False) and looks_deactive_text(
            account.get("error_message")
        ):
            return True

        explicit_code = _first_string(
            account,
            ("error_code",),
            ("errorCode",),
            ("last_error_code",),
            ("lastErrorCode",),
            ("error", "code"),
        )
        if explicit_code and explicit_code.strip().lower() in {"account_deactive", "account_deactivated"}:
            return True

        return False
