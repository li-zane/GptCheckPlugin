import base64
import calendar
import copy
import json
import math
import re
from datetime import datetime, timedelta, timezone
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
ACCOUNT_NAME_PATHS = (
    ("name",),
    ("account_name",),
    ("accountName",),
    ("profile", "name"),
    ("extra", "name"),
    ("credentials", "name"),
)
MAX_SUB2API_RESPONSE_BYTES = 1024 * 1024
MAX_SUB2API_ERROR_PREVIEW_BYTES = 500
MAX_SUB2API_ACCOUNT_PAGES = 100
MAX_SUB2API_ACCOUNTS = 10_000


class Sub2ApiRequestError(RuntimeError):
    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


async def _read_bounded_response(
    response: httpx.Response,
    *,
    limit: int,
) -> tuple[bytes, bool]:
    chunks: list[bytes] = []
    size = 0
    async for chunk in response.aiter_bytes():
        remaining = limit - size
        if remaining <= 0:
            return b"".join(chunks), True
        if len(chunk) > remaining:
            chunks.append(chunk[:remaining])
            return b"".join(chunks), True
        chunks.append(chunk)
        size += len(chunk)
    return b"".join(chunks), False


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


def _positive_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, float) and not value.is_integer():
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _find_nested_field(value: Any, field: str) -> tuple[bool, Any]:
    if not isinstance(value, dict):
        return False, None
    if field in value:
        return True, value[field]
    for key in ("data", "settings", "payment", "payment_settings", "config"):
        child = value.get(key)
        if isinstance(child, dict):
            found, result = _find_nested_field(child, field)
            if found:
                return True, result
    return False, None


def _bounded_number(value: Any, *, minimum: float = 0, maximum: float = 1000) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(parsed) or parsed < minimum or parsed > maximum:
        return None
    return parsed


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
            if lowered == "rt" or any(marker in lowered for marker in ("token", "secret", "password", "cookie", "authorization")):
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
    "rt",
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
SUBSCRIPTION_CREDENTIAL_KEYS = (
    "plan_type",
    "subscription_starts_at",
    "subscription_expires_at",
    "subscription_renews_at",
    "subscription_cancels_at",
    "subscription_billing_period",
    "subscription_plan",
    "has_active_subscription",
)
HEALTHY_ACCOUNT_STATUSES = {"active", "ok", "ready", "normal", "recovered", "healthy", "valid"}


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


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _format_datetime(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _shift_months(value: datetime, months: int) -> datetime:
    month_index = value.month - 1 + months
    year = value.year + month_index // 12
    month = month_index % 12 + 1
    day = min(value.day, calendar.monthrange(year, month)[1])
    return value.replace(year=year, month=month, day=day)


def _subscription_starts_at(end_at: str | None, billing_period: str | None) -> str | None:
    end = _parse_datetime(end_at)
    if end is None or not billing_period:
        return None
    period = billing_period.strip().lower()
    if period in {"monthly", "month"}:
        return _format_datetime(_shift_months(end, -1))
    if period in {"yearly", "annual", "annually", "year"}:
        return _format_datetime(_shift_months(end, -12))
    if period in {"weekly", "week"}:
        return _format_datetime(end - timedelta(days=7))
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
    if key in {"expires_at", "subscription_expires_at", "subscription_renews_at", "subscription_cancels_at"}:
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
    def __init__(
        self,
        settings: Settings | None = None,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.transport = transport

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
        async with httpx.AsyncClient(
            timeout=30.0,
            headers=self._headers(active_config),
            trust_env=False,
            transport=self.transport,
        ) as client:
            url = self._url(active_config, path)
            async with client.stream(method, url, **kwargs) as response:
                status_code = response.status_code
                if status_code < 200 or status_code >= 300:
                    body, _truncated = await _read_bounded_response(
                        response,
                        limit=MAX_SUB2API_ERROR_PREVIEW_BYTES,
                    )
                    detail = body.decode("utf-8", errors="replace")
                    raise Sub2ApiRequestError(
                        f"sub2api request failed: HTTP {status_code} for {method} {url}. {detail}",
                        status_code=status_code,
                    )
                body, truncated = await _read_bounded_response(
                    response,
                    limit=MAX_SUB2API_RESPONSE_BYTES,
                )
            if truncated:
                raise Sub2ApiRequestError(
                    f"sub2api response exceeded {MAX_SUB2API_RESPONSE_BYTES} bytes for {method} {url}."
                )
            if not body:
                return None
            try:
                return json.loads(body)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                preview = body[:MAX_SUB2API_ERROR_PREVIEW_BYTES].decode("utf-8", errors="replace")
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
        payload = await self._request("GET", config.accounts_path, config=config, params={"page": 1, "page_size": 100})
        accounts = self._unwrap(payload)
        if not isinstance(accounts, list):
            return []

        result = [item for item in accounts if isinstance(item, dict)]
        if len(result) > MAX_SUB2API_ACCOUNTS:
            raise Sub2ApiRequestError("sub2api returned too many accounts.")
        data = payload.get("data") if isinstance(payload, dict) else None
        pages = _positive_int(data.get("pages") if isinstance(data, dict) else None)
        if pages is not None and pages > MAX_SUB2API_ACCOUNT_PAGES:
            raise Sub2ApiRequestError("sub2api account pagination exceeds the safety limit.")
        if pages is not None and pages > 1:
            for page in range(2, pages + 1):
                page_payload = await self._request(
                    "GET",
                    config.accounts_path,
                    config=config,
                    params={"page": page, "page_size": 100},
                )
                page_accounts = self._unwrap(page_payload)
                if isinstance(page_accounts, list):
                    result.extend(item for item in page_accounts if isinstance(item, dict))
                    if len(result) > MAX_SUB2API_ACCOUNTS:
                        raise Sub2ApiRequestError("sub2api returned too many accounts.")
        return result

    async def list_api_key_accounts(self) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for account in await self.list_accounts():
            account_type = (self.account_type(account) or "").strip().lower()
            normalized = account_type.replace("-", "_").replace(" ", "_")
            if normalized in {"apikey", "api_key"}:
                result.append(account)
        return result

    async def export_api_key_secrets(self, account_ids: list[int]) -> dict[int, str]:
        """Read API keys from the protected local admin export without caching them."""

        normalized_ids: list[int] = []
        for raw_id in account_ids:
            account_id = _positive_int(raw_id)
            if account_id is None or account_id in normalized_ids:
                continue
            normalized_ids.append(account_id)
        if not normalized_ids or len(normalized_ids) > 200:
            return {}

        config = await get_runtime_config_service().get_sub2api_config()
        try:
            payload = await self._request(
                "GET",
                f"{config.accounts_path}/data",
                config=config,
                params={
                    "ids": ",".join(str(account_id) for account_id in normalized_ids),
                    "include_proxies": "false",
                },
            )
        except Sub2ApiRequestError:
            return {}
        exported = self._unwrap(payload)
        if not isinstance(exported, list):
            return {}

        requested = set(normalized_ids)

        def exported_key(item: Any) -> str | None:
            if not isinstance(item, dict):
                return None
            credentials = item.get("credentials")
            if not isinstance(credentials, dict):
                return None
            raw_key = _first_value(
                credentials,
                ("api_key",),
                ("apiKey",),
                ("apikey",),
            )
            if not isinstance(raw_key, str):
                return None
            api_key = raw_key.strip()
            if (
                not api_key
                or len(api_key) > 8192
                or _looks_redacted(api_key)
                or any(ord(char) < 32 or ord(char) == 127 for char in api_key)
            ):
                return None
            return api_key

        response_ids = [
            _positive_int(
                _first_value(
                    item,
                    ("id",),
                    ("account_id",),
                    ("accountId",),
                )
            )
            if isinstance(item, dict)
            else None
            for item in exported
        ]
        result: dict[int, str] = {}
        if any(account_id is not None for account_id in response_ids):
            duplicate_ids: set[int] = set()
            for account_id, item in zip(response_ids, exported, strict=True):
                if account_id is None or account_id not in requested:
                    continue
                if account_id in result:
                    duplicate_ids.add(account_id)
                    continue
                api_key = exported_key(item)
                if api_key:
                    result[account_id] = api_key
            for account_id in duplicate_ids:
                result.pop(account_id, None)
            return result

        # A credential must never be associated by response position. Older
        # exports that omit stable account ids are intentionally unsupported.
        return {}

    async def get_account_balance(self, account: dict[str, Any] | str | int) -> dict[str, Any]:
        raw_account_id = account if isinstance(account, (str, int)) else self.account_id(account)
        account_id = _positive_int(raw_account_id)
        if account_id is None:
            raise ValueError("A positive numeric sub2api account id is required.")
        config = await get_runtime_config_service().get_sub2api_config()
        payload = await self._request(
            "GET",
            f"{config.accounts_path}/{account_id}/balance",
            config=config,
        )
        balance = self._unwrap(payload)
        return balance if isinstance(balance, dict) else {}

    async def get_payment_balance_recharge_multiplier_info(self) -> tuple[float, bool]:
        config = await get_runtime_config_service().get_sub2api_config()
        payload = await self._request("GET", "/admin/settings", config=config)
        found, raw_value = _find_nested_field(payload, "payment_balance_recharge_multiplier")
        if not found:
            return 1.0, False
        value = _bounded_number(raw_value, minimum=0.000000001, maximum=1000)
        if value is None:
            raise Sub2ApiRequestError("sub2api returned an invalid payment balance recharge multiplier.")
        return value, True

    async def get_payment_balance_recharge_multiplier(self) -> float:
        value, _field_present = await self.get_payment_balance_recharge_multiplier_info()
        return value

    async def get_account_current_rate_multiplier(self, account_id: str | int) -> float:
        numeric_id = _positive_int(account_id)
        if numeric_id is None:
            raise ValueError("A positive numeric sub2api account id is required.")
        account = await self.get_account(str(numeric_id))
        if account is None:
            raise Sub2ApiRequestError("sub2api account was not found.", status_code=404)
        value = _bounded_number(account.get("rate_multiplier"), minimum=0, maximum=1000)
        if value is None:
            raise Sub2ApiRequestError("sub2api account returned an invalid rate multiplier.")
        return value

    async def update_account_rate_multiplier(self, account_id: str | int, rate_multiplier: float) -> None:
        numeric_id = _positive_int(account_id)
        if numeric_id is None:
            raise ValueError("A positive numeric sub2api account id is required.")
        parsed_rate = _bounded_number(rate_multiplier, minimum=0, maximum=1000)
        if parsed_rate is None:
            raise ValueError("rate_multiplier must be between 0 and 1000.")
        config = await get_runtime_config_service().get_sub2api_config()
        await self._request(
            "POST",
            f"{config.accounts_path}/bulk-update",
            config=config,
            json={"account_ids": [numeric_id], "rate_multiplier": parsed_rate},
        )

    async def set_account_schedulable(self, account_id: str | int, schedulable: bool) -> None:
        numeric_id = _positive_int(account_id)
        if numeric_id is None:
            raise ValueError("A positive numeric sub2api account id is required.")
        if not isinstance(schedulable, bool):
            raise ValueError("schedulable must be a boolean.")
        config = await get_runtime_config_service().get_sub2api_config()
        await self._request(
            "POST",
            f"{config.accounts_path}/{numeric_id}/schedulable",
            config=config,
            json={"schedulable": schedulable},
        )

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
            await self._after_account_credentials_update(account, account_id, config)
            return
        if token_path.startswith("credentials."):
            credentials = copy.deepcopy(account.get("credentials") or {})
            _deep_set({"credentials": credentials}, token_path, access_token)
            payload = {"credentials": credentials}
        else:
            payload = {}
            _deep_set(payload, token_path, access_token)

        await self._request("PUT", f"{config.accounts_path}/{account_id}", config=config, json=payload)
        await self._after_account_credentials_update(account, account_id, config)

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
        self._add_refresh_token_aliases(account, credentials)
        if not credentials.get("access_token"):
            raise ValueError("Session endpoint did not include a usable access token.")

        changes = self.changed_credentials(account, credentials)
        config = await get_runtime_config_service().get_sub2api_config()
        if changes:
            await self._update_credentials_patch(account, changes, config)
            self._merge_account_credentials(account, changes)
        if changes or self.account_requires_state_recovery(account):
            await self._after_account_credentials_update(account, account_id, config)
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
        return await self.refresh_account_usage_data(account) is not None

    async def refresh_account_usage_data(self, account: dict[str, Any] | str) -> dict[str, Any] | None:
        account_id = account if isinstance(account, str) else self.account_id(account)
        if not account_id:
            raise ValueError("Cannot refresh sub2api account usage without id.")

        config = await get_runtime_config_service().get_sub2api_config()
        try:
            payload = await self._request(
                "GET",
                f"{config.accounts_path}/{account_id}/usage",
                config=config,
                params={"source": "active", "force": "true"},
            )
        except Sub2ApiRequestError as exc:
            if exc.status_code in {404, 405}:
                return None
            raise
        usage = self._unwrap(payload)
        return usage if isinstance(usage, dict) else {}

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
                await self._after_account_credentials_update(account, account_id, config)
            return refreshed
        return {}

    async def get_account(self, account: dict[str, Any] | str) -> dict[str, Any] | None:
        account_id = account if isinstance(account, str) else self.account_id(account)
        email = None if isinstance(account, str) else self.account_email(account)
        normalized_email = email.lower() if isinstance(email, str) else None
        for item in await self.list_accounts():
            item_id = self.account_id(item)
            if account_id and item_id == str(account_id):
                return item
            item_email = self.account_email(item)
            if normalized_email and item_email and item_email.lower() == normalized_email:
                return item
        return None

    async def create_account(
        self,
        *,
        email: str,
        access_token: str,
        session: dict[str, Any] | None = None,
        refresh_token: str | None = None,
        id_token: str | None = None,
        phone_number: str | None = None,
        phone_sms_url: str | None = None,
        notes: str | None = None,
    ) -> dict[str, Any]:
        normalized_email = str(email or "").strip().lower()
        if not normalized_email:
            raise ValueError("Cannot create sub2api account without email.")
        token = str(access_token or "").strip()
        if not token:
            raise ValueError("Cannot create sub2api account without access_token.")

        session_payload = copy.deepcopy(session) if isinstance(session, dict) else {}
        tokens = session_payload.get("tokens") if isinstance(session_payload.get("tokens"), dict) else {}
        if refresh_token:
            session_payload["refresh_token"] = refresh_token
            tokens["refresh_token"] = refresh_token
        if id_token:
            session_payload["id_token"] = id_token
            tokens["id_token"] = id_token
        session_payload["access_token"] = token
        tokens["access_token"] = token
        session_payload["tokens"] = tokens
        session_payload.setdefault("email", normalized_email)

        credentials = self.credentials_from_session(session_payload, token)
        credentials.update(self.subscription_credentials_from_session(session_payload, token))
        credentials["email"] = normalized_email

        extra: dict[str, Any] = {"email": normalized_email}
        mail_console: dict[str, Any] = {}
        if phone_number:
            mail_console["phone_verification_phone"] = phone_number
        if phone_sms_url:
            mail_console["phone_verification_url"] = phone_sms_url
        if notes:
            mail_console["note"] = notes
        if mail_console:
            extra["mail_console"] = mail_console

        payload = {
            "name": normalized_email,
            "platform": "openai",
            "type": "oauth",
            "credentials": credentials,
            "extra": extra,
            "concurrency": 2,
            "priority": 1,
            "rate_multiplier": 1,
            "auto_pause_on_expired": True,
        }

        config = await get_runtime_config_service().get_sub2api_config()
        created = await self._request("POST", config.accounts_path, config=config, json=payload)
        unwrapped = self._unwrap(created)
        return unwrapped if isinstance(unwrapped, dict) else payload

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
            ("rt",),
            ("tokens", "refreshToken"),
            ("tokens", "refresh_token"),
            ("tokens", "rt"),
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
        subscription_expires_at = _coerce_rfc3339(
            _first_value(
                data,
                ("subscription_expires_at",),
                ("subscriptionExpiresAt",),
                ("account", "subscriptionExpiresAt"),
                ("account", "subscription_expires_at"),
                ("account", "entitlement", "expires_at"),
                ("entitlement", "expires_at"),
                ("entitlement", "expiresAt"),
            )
        )
        subscription_renews_at = _coerce_rfc3339(
            _first_value(
                data,
                ("subscription_renews_at",),
                ("subscriptionRenewsAt",),
                ("account", "entitlement", "renews_at"),
                ("entitlement", "renews_at"),
                ("entitlement", "renewsAt"),
            )
        )
        subscription_cancels_at = _coerce_rfc3339(
            _first_value(
                data,
                ("subscription_cancels_at",),
                ("subscriptionCancelsAt",),
                ("account", "entitlement", "cancels_at"),
                ("entitlement", "cancels_at"),
                ("entitlement", "cancelsAt"),
            )
        )
        subscription_billing_period = _first_string(
            data,
            ("subscription_billing_period",),
            ("subscriptionBillingPeriod",),
            ("account", "entitlement", "billing_period"),
            ("entitlement", "billing_period"),
            ("entitlement", "billingPeriod"),
        )
        _set_if_present(
            credentials,
            "subscription_starts_at",
            _coerce_rfc3339(
                _first_value(
                    data,
                    ("subscription_starts_at",),
                    ("subscriptionStartsAt",),
                    ("subscription_started_at",),
                    ("subscriptionStartedAt",),
                    ("account", "entitlement", "starts_at"),
                    ("entitlement", "starts_at"),
                    ("entitlement", "startsAt"),
                )
            )
            or _subscription_starts_at(subscription_renews_at or subscription_cancels_at, subscription_billing_period),
        )
        _set_if_present(
            credentials,
            "subscription_expires_at",
            subscription_expires_at,
        )
        _set_if_present(
            credentials,
            "subscription_renews_at",
            subscription_renews_at,
        )
        _set_if_present(
            credentials,
            "subscription_cancels_at",
            subscription_cancels_at,
        )
        _set_if_present(
            credentials,
            "subscription_billing_period",
            subscription_billing_period,
        )
        _set_if_present(
            credentials,
            "subscription_plan",
            _first_string(
                data,
                ("subscription_plan",),
                ("subscriptionPlan",),
                ("account", "entitlement", "subscription_plan"),
                ("entitlement", "subscription_plan"),
                ("entitlement", "subscriptionPlan"),
            ),
        )
        _set_if_present(
            credentials,
            "has_active_subscription",
            _first_value(
                data,
                ("has_active_subscription",),
                ("hasActiveSubscription",),
                ("account", "entitlement", "has_active_subscription"),
                ("entitlement", "has_active_subscription"),
                ("entitlement", "hasActiveSubscription"),
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

    def _add_refresh_token_aliases(self, account: dict[str, Any], credentials: dict[str, Any]) -> None:
        refresh_token = credentials.get("refresh_token")
        if not isinstance(refresh_token, str) or not refresh_token.strip():
            return
        current = account.get("credentials")
        if not isinstance(current, dict):
            current = {}
        credentials_status = account.get("credentials_status")
        has_rt = isinstance(credentials_status, dict) and credentials_status.get("has_rt") is True
        has_refresh_token_status = (
            isinstance(credentials_status, dict)
            and credentials_status.get("has_refresh_token") is True
            and "refresh_token" not in current
            and "refreshToken" not in current
        )
        if "rt" in current or has_rt or has_refresh_token_status:
            credentials["rt"] = refresh_token

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

    async def _after_account_credentials_update(
        self,
        account: dict[str, Any],
        account_id: str,
        config: EffectiveSub2ApiConfig,
    ) -> None:
        should_recover_state = self.account_requires_state_recovery(account)
        if should_recover_state:
            await self._recover_account_state(account_id, config)

    async def recover_account_state(self, account: dict[str, Any] | str) -> None:
        account_id = account if isinstance(account, str) else self.account_id(account)
        if not account_id:
            raise ValueError("Cannot recover sub2api account state without id.")
        config = await get_runtime_config_service().get_sub2api_config()
        await self._recover_account_state(account_id, config)

    async def clear_rate_limit_state(self, account: dict[str, Any] | str) -> None:
        account_id = account if isinstance(account, str) else self.account_id(account)
        if not account_id:
            raise ValueError("Cannot clear sub2api rate limit state without id.")
        config = await get_runtime_config_service().get_sub2api_config()
        await self._request(
            "PUT",
            f"{config.accounts_path}/{account_id}",
            config=config,
            json={
                "rate_limited_at": None,
                "rate_limit_reset_at": None,
                "temp_unschedulable_until": None,
                "temp_unschedulable_reason": "",
            },
        )

    async def _recover_account_state(self, account_id: str, config: EffectiveSub2ApiConfig) -> None:
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

    def dedupe_accounts_by_email(
        self,
        accounts: list[dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        metadata = self.account_duplicate_metadata(accounts)
        duplicates: list[dict[str, Any]] = []
        keep_indexes: set[int] = set()
        for index, account in enumerate(accounts):
            item = metadata.get(index)
            if item is None or item["duplicate_primary"]:
                keep_indexes.add(index)
            if item is not None and item["is_duplicate"] and not item["duplicate_primary"]:
                duplicates.append(
                    {
                        "email": item["email"],
                        "kept_account_id": item["duplicate_primary_account_id"],
                        "ignored_account_id": self.account_id(account),
                        "ignored_status": self.account_status(account),
                        "ignored_schedulable": self.account_schedulable(account),
                        "ignored_error": self.is_error_account(account),
                        "ignored_deactive": self.is_deactive_account(account),
                    }
                )

        deduped = [account for index, account in enumerate(accounts) if index in keep_indexes]
        return deduped, duplicates

    def account_duplicate_metadata(self, accounts: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
        grouped: dict[str, list[tuple[int, dict[str, Any]]]] = {}
        for index, account in enumerate(accounts):
            email = self.account_email(account)
            if email:
                grouped.setdefault(email.lower(), []).append((index, account))

        metadata: dict[int, dict[str, Any]] = {}
        for email, entries in grouped.items():
            ordered = sorted(entries, key=lambda item: self._dedupe_account_sort_key(item[1], item[0]))
            primary_index, primary_account = ordered[0]
            primary_id = self.account_id(primary_account)
            group_size = len(ordered)
            for rank, (index, account) in enumerate(ordered):
                metadata[index] = {
                    "email": email,
                    "is_duplicate": group_size > 1,
                    "duplicate_group_size": group_size,
                    "duplicate_rank": rank,
                    "duplicate_primary": index == primary_index,
                    "duplicate_primary_account_id": primary_id,
                }
        return metadata

    def _dedupe_account_sort_key(self, account: dict[str, Any], original_index: int) -> tuple[int, int, int, int, int, int]:
        status = (self.account_status(account) or "").strip().lower()
        schedulable = self.account_schedulable(account)
        schedulable_rank = 0 if schedulable is True else 2 if schedulable is False else 1
        credential_rank = 0 if self.account_has_refresh_token(account) else 1 if self.account_access_token(account) else 2
        return (
            1 if self.is_deactive_account(account) else 0,
            1 if self.is_error_account(account) else 0,
            schedulable_rank,
            0 if status in HEALTHY_ACCOUNT_STATUSES else 1,
            credential_rank,
            original_index,
        )

    def account_email(self, account: dict[str, Any]) -> str | None:
        for path in ACCOUNT_EMAIL_PATHS:
            email = extract_email(_path_get(account, path))
            if email:
                return email
        return None

    def account_name(self, account: dict[str, Any]) -> str | None:
        for path in ACCOUNT_NAME_PATHS:
            value = _path_get(account, path)
            if isinstance(value, str) and value.strip():
                return value.strip()
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

    def account_has_api_key(self, account: dict[str, Any]) -> bool:
        credentials_status = account.get("credentials_status")
        if isinstance(credentials_status, dict) and any(
            credentials_status.get(key) is True for key in ("has_api_key", "hasApiKey")
        ):
            return True
        credentials = account.get("credentials")
        if not isinstance(credentials, dict):
            return False
        return any(
            isinstance(credentials.get(key), str) and bool(credentials[key].strip())
            for key in ("api_key", "apiKey", "apikey")
        )

    def is_oauth_account(self, account: dict[str, Any]) -> bool:
        account_type = (self.account_type(account) or "").strip().lower()
        normalized_type = account_type.replace("-", "_").replace(" ", "_")
        if normalized_type:
            if "oauth" in normalized_type:
                return True
            if "api_key" in normalized_type or "apikey" in normalized_type:
                return False

        credentials_status = account.get("credentials_status")
        if isinstance(credentials_status, dict):
            if any(credentials_status.get(key) is True for key in ("has_refresh_token", "has_refreshToken", "has_rt")):
                return True
            if any(credentials_status.get(key) is True for key in ("has_api_key", "hasApiKey")):
                return False

        credentials = account.get("credentials")
        if not isinstance(credentials, dict):
            return False
        if any(key in credentials for key in ("refresh_token", "refreshToken", "rt", "id_token", "idToken")):
            return True
        if any(key in credentials for key in ("api_key", "apiKey", "apikey")):
            return False
        return False

    def account_schedulable(self, account: dict[str, Any]) -> bool | None:
        value = account.get("schedulable")
        if isinstance(value, bool):
            return value
        return None

    def account_error_message(self, account: dict[str, Any]) -> str | None:
        for value in (
            account.get("error_message"),
            account.get("errorMessage"),
            _deep_get(account, "error.message"),
        ):
            if value is None:
                continue
            text = str(value).strip()
            if text:
                return text
        return None

    def account_phone_note_text(self, account: dict[str, Any]) -> str:
        phone_number = _first_string(
            account,
            ("extra", "mail_console", "phone_verification_phone"),
            ("extra", "phone_verification_phone"),
            ("phone_verification_phone",),
            ("phone_number",),
            ("extra", "phone_number"),
        )
        phone_url = _first_string(
            account,
            ("extra", "mail_console", "phone_verification_url"),
            ("extra", "phone_verification_url"),
            ("phone_verification_url",),
            ("sms_url",),
            ("extra", "sms_url"),
        )
        phone_cdk = _first_string(
            account,
            ("extra", "mail_console", "phone_verification_cdk"),
            ("extra", "phone_verification_cdk"),
            ("phone_verification_cdk",),
            ("sms_cdk",),
            ("extra", "sms_cdk"),
        )
        if phone_number and phone_cdk and phone_url:
            return f"{phone_number}----{phone_cdk}----{phone_url}"
        if phone_number and phone_cdk:
            return f"{phone_number}----{phone_cdk}"
        if phone_number and phone_url:
            return f"{phone_number}----{phone_url}"

        notes: list[str] = []
        for value in (
            account.get("notes"),
            account.get("note"),
            account.get("remark"),
            account.get("remarks"),
            account.get("description"),
            _path_get(account, ("extra", "note")),
            _path_get(account, ("extra", "notes")),
            _path_get(account, ("extra", "remark")),
            _path_get(account, ("extra", "remarks")),
            _path_get(account, ("extra", "mail_console", "note")),
        ):
            if value is None:
                continue
            text = str(value).strip()
            if text and text not in notes:
                notes.append(text)
        return "\n".join(notes)

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
            key in credentials for key in ("access_token", "accessToken", "refresh_token", "refreshToken", "rt")
        )

    def is_error_account(self, account: dict[str, Any]) -> bool:
        status = (self.account_status(account) or "").strip().lower()
        if any(marker in status for marker in ("error", "fail", "invalid", "expired", "disabled")):
            return True
        schedulable = self.account_schedulable(account)
        if schedulable is not False or "deactive" in status:
            return False
        if status in HEALTHY_ACCOUNT_STATUSES and not self.account_error_message(account):
            return False
        return True

    def account_looks_healthy(self, account: dict[str, Any]) -> bool:
        if self.is_error_account(account) or self.is_deactive_account(account):
            return False
        status = (self.account_status(account) or "").strip().lower()
        schedulable = self.account_schedulable(account)
        return schedulable is True or status in HEALTHY_ACCOUNT_STATUSES

    def account_requires_state_recovery(self, account: dict[str, Any]) -> bool:
        return not self.account_looks_healthy(account)

    def account_has_stale_rate_limit_state(self, account: dict[str, Any]) -> bool:
        return any(
            _first_string(account, path) is not None
            for path in (
                ("rate_limited_at",),
                ("rate_limit_reset_at",),
                ("temp_unschedulable_until",),
                ("temp_unschedulable_reason",),
                ("extra", "rate_limited_at"),
                ("extra", "rate_limit_reset_at"),
            )
        )

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
