import asyncio
import hashlib
import html
import imaplib
import json
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from email import message_from_bytes
from email.header import decode_header, make_header
from email.utils import parseaddr, parsedate_to_datetime
from typing import Any, ClassVar, Protocol

import httpx

from app.core.config import Settings, get_settings
from app.core.crypto import decrypt_text
from app.models import MailboxCredential


CODE_RE = re.compile(r"\b(\d{6})\b")
IMAP_UID_RE = re.compile(rb"\bUID\s+(\d+)\b")
FOLDER_MAP = {
    "inbox": "inbox",
    "junk": "junkemail",
    "INBOX": "inbox",
    "Junk": "junkemail",
}
OUTLOOK_REST_BASE = "https://outlook.office.com/api/v2.0"


@dataclass
class MailFetchResult:
    status: str
    code: str | None = None
    error: str | None = None
    provider_status: str | None = None
    new_refresh_token: str | None = None


@dataclass
class MailMessage:
    id: str
    folder: str
    subject: str | None
    sender_name: str | None
    sender_address: str | None
    body_preview: str | None
    received_at: datetime | None
    text: str | None = None
    code: str | None = None


@dataclass
class MailMessagesResult:
    status: str
    messages: list[MailMessage]
    error: str | None = None
    provider_status: str | None = None
    new_refresh_token: str | None = None


@dataclass
class _TokenResult:
    status: str
    access_token: str | None = None
    new_refresh_token: str | None = None
    expires_at: float | None = None
    error: str | None = None
    provider_status: str | None = None


@dataclass
class _CachedToken:
    access_token: str
    new_refresh_token: str
    expires_at: float


class MailAdapter(Protocol):
    async def fetch_code(self, credential: MailboxCredential, after: datetime) -> MailFetchResult:
        ...

    async def fetch_messages(self, credential: MailboxCredential, folder: str, limit: int) -> MailMessagesResult:
        ...


def extract_code(*texts: str | None) -> str | None:
    for text in texts:
        if not text:
            continue
        match = CODE_RE.search(text)
        if match:
            return match.group(1)
    return None


class OutlookGraphAdapter:
    _strategy_cache: ClassVar[dict[str, str]] = {}
    _token_cache: ClassVar[dict[str, _CachedToken]] = {}
    _imap_host_cooldown: ClassVar[dict[str, float]] = {}

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    async def fetch_code(self, credential: MailboxCredential, after: datetime) -> MailFetchResult:
        new_refresh_token: str | None = None
        last_error: str | None = None
        for folder in ("inbox", "junk"):
            result = await self.fetch_messages(credential, folder, 10)
            if result.new_refresh_token:
                new_refresh_token = result.new_refresh_token
            if result.status != "ok":
                last_error = result.error
                if folder == "inbox":
                    return MailFetchResult(
                        status="failed",
                        error=result.error,
                        provider_status=result.provider_status,
                        new_refresh_token=new_refresh_token,
                    )
                continue

            messages = sorted(
                result.messages,
                key=lambda item: item.received_at or datetime.min.replace(tzinfo=timezone.utc),
                reverse=True,
            )
            for message in messages:
                if message.received_at and message.received_at < after:
                    continue
                code = message.code or extract_code(message.subject, message.body_preview, message.text)
                if code:
                    return MailFetchResult(status="ok", code=code, new_refresh_token=new_refresh_token)

        if last_error:
            return MailFetchResult(
                status="not_found",
                error=f"No fresh verification code message was found. Last mailbox error: {last_error}",
                new_refresh_token=new_refresh_token,
            )
        return MailFetchResult(
            status="not_found",
            error="No fresh verification code message was found.",
            new_refresh_token=new_refresh_token,
        )

    async def fetch_messages(self, credential: MailboxCredential, folder: str, limit: int) -> MailMessagesResult:
        try:
            return await asyncio.wait_for(
                self._fetch_messages_inner(credential, folder, limit),
                timeout=self.settings.mail_read_timeout_seconds,
            )
        except asyncio.TimeoutError:
            return MailMessagesResult(
                status="failed",
                messages=[],
                error=f"Mailbox read timed out after {self.settings.mail_read_timeout_seconds} seconds.",
            )

    async def _fetch_messages_inner(self, credential: MailboxCredential, folder: str, limit: int) -> MailMessagesResult:
        client_id = (decrypt_text(credential.encrypted_client_id) or "").strip()
        refresh_token = (decrypt_text(credential.encrypted_refresh_token) or "").strip()
        if not client_id or not refresh_token:
            return MailMessagesResult(status="failed", messages=[], error="Missing client_id or refresh_token.")

        base_cache_key = self._base_cache_key(credential, client_id)
        errors: list[str] = []

        for strategy in self._strategy_order(base_cache_key):
            if strategy in {"graph_imap", "o2_wl_imap"} and _has_imap_network_failure(errors):
                continue
            result = await self._fetch_with_strategy(credential, folder, limit, client_id, refresh_token, base_cache_key, strategy)
            if result.status == "ok":
                self._strategy_cache[base_cache_key] = strategy
                return result
            if result.error:
                errors.append(result.error)
            if strategy == "o2_imap" and _has_imap_network_failure(result.error):
                break

        return MailMessagesResult(
            status="failed",
            messages=[],
            error="Unable to read mailbox. " + " | ".join(errors[-4:]),
        )

    def _strategy_order(self, base_cache_key: str) -> list[str]:
        default_order = ["outlook_rest", "graph", "o2_imap", "graph_imap", "o2_wl_imap"]
        preferred = self._strategy_cache.get(base_cache_key)
        if preferred not in default_order:
            return default_order
        return [preferred, *[strategy for strategy in default_order if strategy != preferred]]

    async def _fetch_with_strategy(
        self,
        credential: MailboxCredential,
        folder: str,
        limit: int,
        client_id: str,
        refresh_token: str,
        base_cache_key: str,
        strategy: str,
    ) -> MailMessagesResult:
        if strategy == "outlook_rest":
            token_result = await self._cached_access_token(
                base_cache_key=base_cache_key,
                refresh_token=refresh_token,
                strategy=strategy,
                client_id=client_id,
                token_url=self.settings.graph_token_url,
                scope=None,
                label="Outlook REST",
            )
            if token_result.status != "ok" or not token_result.access_token:
                return MailMessagesResult(
                    status="failed",
                    messages=[],
                    error=token_result.error or "Outlook REST token refresh failed.",
                    provider_status=token_result.provider_status,
                )
            rest_result = await self._fetch_outlook_rest_messages(token_result.access_token, folder, limit)
            rest_result.new_refresh_token = token_result.new_refresh_token
            if rest_result.status != "ok":
                self._drop_cached_access_token(base_cache_key, refresh_token, strategy)
            return rest_result

        if strategy == "graph":
            token_result = await self._cached_access_token(
                base_cache_key=base_cache_key,
                refresh_token=refresh_token,
                strategy=strategy,
                client_id=client_id,
                token_url=self.settings.graph_token_url,
                scope=self.settings.graph_scope,
                label="Graph",
            )
            if token_result.status != "ok" or not token_result.access_token:
                return MailMessagesResult(
                    status="failed",
                    messages=[],
                    error=token_result.error or "Graph token refresh failed.",
                    provider_status=token_result.provider_status,
                )
            graph_result = await self._fetch_graph_messages(token_result.access_token, folder, limit)
            graph_result.new_refresh_token = token_result.new_refresh_token
            if graph_result.status != "ok":
                self._drop_cached_access_token(base_cache_key, refresh_token, strategy)
            return graph_result

        imap_specs = {
            "o2_imap": (self.settings.live_oauth_token_url, None, "O2/IMAP"),
            "graph_imap": (self.settings.graph_token_url, self.settings.outlook_imap_scope, "Graph/IMAP"),
            "o2_wl_imap": (self.settings.live_oauth_token_url, self.settings.live_oauth_scope, "O2/wl.imap"),
        }
        spec = imap_specs.get(strategy)
        if not spec:
            return MailMessagesResult(
                status="failed",
                messages=[],
                error=f"Unknown mailbox read strategy: {strategy}.",
            )
        token_url, scope, label = spec
        token_result = await self._cached_access_token(
            base_cache_key=base_cache_key,
            refresh_token=refresh_token,
            strategy=strategy,
            client_id=client_id,
            token_url=token_url,
            scope=scope,
            label=label,
        )
        if token_result.status != "ok" or not token_result.access_token:
            return MailMessagesResult(
                status="failed",
                messages=[],
                error=token_result.error or f"{label} token refresh failed.",
                provider_status=token_result.provider_status,
            )
        imap_result = await self._fetch_imap_messages(credential, token_result.access_token, folder, limit)
        imap_result.new_refresh_token = token_result.new_refresh_token
        if imap_result.status != "ok" and not _has_imap_network_failure(imap_result.error):
            self._drop_cached_access_token(base_cache_key, refresh_token, strategy)
        return imap_result

    async def _cached_access_token(
        self,
        *,
        base_cache_key: str,
        refresh_token: str,
        strategy: str,
        client_id: str,
        token_url: str,
        scope: str | None,
        label: str,
    ) -> _TokenResult:
        cache_key = self._token_cache_key(base_cache_key, refresh_token, strategy)
        cached = self._token_cache.get(cache_key)
        if cached and cached.expires_at > time.monotonic():
            return _TokenResult(
                status="ok",
                access_token=cached.access_token,
                new_refresh_token=cached.new_refresh_token,
                expires_at=cached.expires_at,
            )

        token_result = await self._refresh_access_token(
            client_id=client_id,
            refresh_token=refresh_token,
            token_url=token_url,
            scope=scope,
            label=label,
        )
        if token_result.status == "ok" and token_result.access_token:
            expires_at = token_result.expires_at or (time.monotonic() + 300)
            self._token_cache[cache_key] = _CachedToken(
                access_token=token_result.access_token,
                new_refresh_token=token_result.new_refresh_token or refresh_token,
                expires_at=expires_at,
            )
        return token_result

    def _drop_cached_access_token(self, base_cache_key: str, refresh_token: str, strategy: str) -> None:
        self._token_cache.pop(self._token_cache_key(base_cache_key, refresh_token, strategy), None)

    def _base_cache_key(self, credential: MailboxCredential, client_id: str) -> str:
        digest = hashlib.sha256(client_id.encode("utf-8")).hexdigest()[:16]
        return f"{credential.id}:{credential.mailbox_email.lower()}:{digest}"

    def _token_cache_key(self, base_cache_key: str, refresh_token: str, strategy: str) -> str:
        digest = hashlib.sha256(refresh_token.encode("utf-8")).hexdigest()[:16]
        return f"{base_cache_key}:{strategy}:{digest}"

    async def _refresh_access_token(
        self,
        *,
        client_id: str,
        refresh_token: str,
        token_url: str,
        scope: str | None,
        label: str,
    ) -> "_TokenResult":
        data = {
            "client_id": client_id,
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        }
        if scope:
            data["scope"] = scope
        try:
            async with httpx.AsyncClient(timeout=self.settings.mail_token_timeout_seconds) as client:
                response = await client.post(
                    token_url,
                    data=data,
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                )
        except httpx.HTTPError as exc:
            return _TokenResult(status="failed", error=f"{label} token request failed: {exc}")

        if response.status_code >= 400:
            return _TokenResult(
                status="failed",
                error=f"{label} access_token refresh failed: HTTP {response.status_code}: {_microsoft_error(response.text)}",
                provider_status=response.text[:500],
            )

        payload = response.json()
        access_token = payload.get("access_token")
        if not access_token:
            return _TokenResult(status="failed", error=f"{label} token response did not include access_token.")
        try:
            expires_in = int(payload.get("expires_in") or 3600)
        except (TypeError, ValueError):
            expires_in = 3600

        return _TokenResult(
            status="ok",
            access_token=str(access_token),
            new_refresh_token=payload.get("refresh_token") or refresh_token,
            expires_at=time.monotonic() + max(expires_in - 60, 60),
        )

    async def _fetch_graph_messages(self, access_token: str, folder: str, limit: int) -> MailMessagesResult:
        graph_folder = FOLDER_MAP.get(folder, "inbox")
        limit = max(1, min(int(limit), 100))
        select_fields = [
            "id",
            "internetMessageId",
            "subject",
            "receivedDateTime",
            "bodyPreview",
            "body",
            "from",
            "sender",
            "toRecipients",
            "ccRecipients",
            "internetMessageHeaders",
        ]
        url: str | None = f"https://graph.microsoft.com/v1.0/me/mailFolders/{graph_folder}/messages"
        params: dict[str, str] | None = {
            "$top": str(min(limit, 100)),
            "$orderby": "receivedDateTime DESC",
            "$select": ",".join(select_fields),
        }
        messages: list[MailMessage] = []

        try:
            async with httpx.AsyncClient(timeout=self.settings.mail_token_timeout_seconds) as client:
                while url and len(messages) < limit:
                    response = await client.get(
                        url,
                        params=params,
                        headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"},
                    )
                    if response.status_code >= 400:
                        return MailMessagesResult(
                            status="failed",
                            messages=[],
                            error=f"Graph mail read failed: HTTP {response.status_code}: {_microsoft_error(response.text)}",
                            provider_status=response.text[:500],
                        )

                    payload = response.json()
                    for item in payload.get("value") or []:
                        if isinstance(item, dict):
                            messages.append(_normalize_graph_message(item, "junk" if folder == "junk" else "inbox"))
                            if len(messages) >= limit:
                                break
                    url = payload.get("@odata.nextLink")
                    params = None
        except httpx.HTTPError as exc:
            return MailMessagesResult(status="failed", messages=[], error=f"Graph mail request failed: {exc}")

        return MailMessagesResult(status="ok", messages=messages)

    async def _fetch_outlook_rest_messages(self, access_token: str, folder: str, limit: int) -> MailMessagesResult:
        rest_folder = FOLDER_MAP.get(folder, "inbox")
        limit = max(1, min(int(limit), 100))
        url = f"{OUTLOOK_REST_BASE}/me/mailfolders/{rest_folder}/messages"
        params = {
            "$top": str(limit),
            "$orderby": "ReceivedDateTime DESC",
            "$select": "Id,Subject,BodyPreview,ReceivedDateTime,From,ToRecipients,Body",
        }

        try:
            async with httpx.AsyncClient(timeout=self.settings.mail_token_timeout_seconds) as client:
                response = await client.get(
                    url,
                    params=params,
                    headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"},
                )
        except httpx.HTTPError as exc:
            return MailMessagesResult(status="failed", messages=[], error=f"Outlook REST mail request failed: {exc}")

        if response.status_code >= 400:
            return MailMessagesResult(
                status="failed",
                messages=[],
                error=f"Outlook REST mail read failed: HTTP {response.status_code}: {_microsoft_error(response.text)}",
                provider_status=response.text[:500],
            )

        payload = response.json()
        items = payload.get("value") if isinstance(payload, dict) else []
        messages = [
            _normalize_outlook_rest_message(item, "junk" if folder == "junk" else "inbox")
            for item in (items if isinstance(items, list) else [])
            if isinstance(item, dict)
        ]
        return MailMessagesResult(status="ok", messages=messages[:limit])

    async def _fetch_imap_messages(self, credential: MailboxCredential, access_token: str, folder: str, limit: int) -> MailMessagesResult:
        return await asyncio.to_thread(self._fetch_imap_messages_sync, credential, access_token, folder, limit)

    def _fetch_imap_messages_sync(self, credential: MailboxCredential, access_token: str, folder: str, limit: int) -> MailMessagesResult:
        errors: list[str] = []
        hosts = self._available_imap_hosts()
        if not hosts:
            return MailMessagesResult(
                status="failed",
                messages=[],
                error="IMAP hosts are cooling down after recent connection timeouts.",
            )

        for host in hosts:
            client: imaplib.IMAP4_SSL | None = None
            try:
                client = imaplib.IMAP4_SSL(host, self.settings.outlook_imap_port, timeout=self.settings.outlook_imap_timeout_seconds)
                auth_string = f"user={credential.mailbox_email}\x01auth=Bearer {access_token}\x01\x01"
                client.authenticate("XOAUTH2", lambda _: auth_string.encode("utf-8"))
                messages = self._read_imap_messages(client, folder, limit)
                self._imap_host_cooldown.pop(host, None)
                return MailMessagesResult(status="ok", messages=messages)
            except imaplib.IMAP4.error as exc:
                errors.append(f"{host}: IMAP OAuth failed: {exc}")
            except OSError as exc:
                errors.append(f"{host}: IMAP connection failed: {exc}")
                if _is_imap_cooldown_error(exc):
                    self._imap_host_cooldown[host] = time.monotonic() + self.settings.outlook_imap_failure_cooldown_seconds
            finally:
                if client is not None:
                    try:
                        client.logout()
                    except imaplib.IMAP4.error:
                        pass
        return MailMessagesResult(status="failed", messages=[], error="; ".join(errors))

    def _read_imap_messages(self, client: imaplib.IMAP4_SSL, folder: str, limit: int) -> list[MailMessage]:
        selected = False
        for candidate in self._imap_folder_candidates(folder):
            status, _ = client.select(candidate, readonly=True)
            if status == "OK":
                selected = True
                break
        if not selected:
            raise imaplib.IMAP4.error(f"IMAP folder not found for {folder}.")

        status, data = client.uid("SEARCH", None, "ALL")
        if status != "OK" or not data or not data[0]:
            return []

        latest_uids = list(reversed(data[0].split()[-max(1, min(limit, 20)) :]))
        if not latest_uids:
            return []

        fetch_spec = (
            "(BODY.PEEK[HEADER.FIELDS "
            "(FROM SUBJECT DATE MESSAGE-ID CONTENT-TYPE CONTENT-TRANSFER-ENCODING MIME-VERSION)] "
            "BODY.PEEK[TEXT]<0.4096>)"
        )
        uid_set = b",".join(latest_uids).decode("ascii", errors="ignore")
        fetch_status, fetch_data = client.uid("FETCH", uid_set, fetch_spec)
        if fetch_status != "OK":
            return []

        grouped_parts = _group_imap_fetch_parts(fetch_data)
        messages: list[MailMessage] = []
        for uid in latest_uids:
            uid_text = uid.decode("ascii", errors="ignore")
            chunks = grouped_parts.get(uid_text)
            if chunks:
                messages.append(_imap_chunks_to_mail_message(chunks, uid_text, folder))
        return messages

    def _imap_folder_candidates(self, folder: str) -> list[str]:
        if folder == "junk":
            return ['"Junk"', '"Junk Email"', '"Junk E-mail"', '"JunkEmail"']
        return ["INBOX"]

    def _imap_hosts(self) -> list[str]:
        hosts = list(self.settings.outlook_imap_hosts or [])
        if self.settings.outlook_imap_host and self.settings.outlook_imap_host not in hosts:
            hosts.append(self.settings.outlook_imap_host)
        return hosts or ["outlook.office365.com"]

    def _available_imap_hosts(self) -> list[str]:
        now = time.monotonic()
        return [host for host in self._imap_hosts() if self._imap_host_cooldown.get(host, 0) <= now]


class CustomHttpMailAdapter:
    async def fetch_code(self, credential: MailboxCredential, after: datetime) -> MailFetchResult:
        if not credential.custom_fetch_url:
            return MailFetchResult(status="failed", error="Missing custom_fetch_url.")
        payload = {
            "gpt_email": credential.gpt_email,
            "mailbox_email": credential.mailbox_email,
            "after": after.isoformat(),
        }
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(credential.custom_fetch_url, json=payload)
        except httpx.HTTPError as exc:
            return MailFetchResult(status="failed", error=f"Custom mail endpoint failed: {exc}")
        if response.status_code >= 400:
            return MailFetchResult(status="failed", error="Custom mail endpoint returned an error.", provider_status=response.text[:500])
        data: dict[str, Any] = response.json()
        code = data.get("code")
        if code:
            return MailFetchResult(status="ok", code=str(code))
        return MailFetchResult(status=str(data.get("status") or "not_found"), error=data.get("error"))

    async def fetch_messages(self, credential: MailboxCredential, folder: str, limit: int) -> MailMessagesResult:
        if folder != "inbox":
            return MailMessagesResult(status="ok", messages=[])
        if not credential.custom_fetch_url:
            return MailMessagesResult(status="failed", messages=[], error="Missing custom_fetch_url.")
        payload = {
            "action": "list_messages",
            "folder": "inbox",
            "limit": max(1, min(limit, 50)),
            "gpt_email": credential.gpt_email,
            "mailbox_email": credential.mailbox_email,
        }
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(credential.custom_fetch_url, json=payload)
        except httpx.HTTPError as exc:
            return MailMessagesResult(status="failed", messages=[], error=f"Custom mail endpoint failed: {exc}")
        if response.status_code >= 400:
            return MailMessagesResult(
                status="failed",
                messages=[],
                error="Custom mail endpoint returned an error.",
                provider_status=response.text[:500],
            )
        data: dict[str, Any] = response.json()
        raw_messages = data.get("messages") if isinstance(data.get("messages"), list) else []
        messages = [_custom_message_to_mail_message(message, "inbox") for message in raw_messages[:limit]]
        return MailMessagesResult(status=str(data.get("status") or "ok"), messages=messages, error=data.get("error"))


class ManualMailAdapter:
    async def fetch_code(self, credential: MailboxCredential, after: datetime) -> MailFetchResult:
        return MailFetchResult(status="not_found", error="Manual provider cannot fetch codes automatically.")

    async def fetch_messages(self, credential: MailboxCredential, folder: str, limit: int) -> MailMessagesResult:
        return MailMessagesResult(status="ok", messages=[])


class MailAdapterRegistry:
    def __init__(self) -> None:
        outlook = OutlookGraphAdapter()
        self.adapters: dict[str, MailAdapter] = {
            "outlook": outlook,
            "hotmail": outlook,
            "custom": CustomHttpMailAdapter(),
            "manual": ManualMailAdapter(),
        }

    async def fetch_code(self, credential: MailboxCredential, after: datetime) -> MailFetchResult:
        adapter = self.adapters.get(credential.provider.lower(), self.adapters["manual"])
        return await adapter.fetch_code(credential, after)

    async def fetch_messages(self, credential: MailboxCredential, folder: str, limit: int) -> MailMessagesResult:
        adapter = self.adapters.get(credential.provider.lower(), self.adapters["manual"])
        return await adapter.fetch_messages(credential, folder, limit)


def _normalize_graph_message(item: dict[str, Any], folder: str) -> MailMessage:
    sender_name = None
    sender_address = None
    sender = item.get("from")
    if isinstance(sender, dict):
        email_address = sender.get("emailAddress")
        if isinstance(email_address, dict):
            sender_name = str(email_address.get("name") or "").strip() or None
            sender_address = str(email_address.get("address") or "").strip() or None

    body = item.get("body") if isinstance(item.get("body"), dict) else {}
    body_content = str(body.get("content") or "")
    body_type = str(body.get("contentType") or "Text").lower()
    text_content = _strip_html(body_content) if body_type == "html" else body_content

    subject = str(item.get("subject") or "").strip() or "(No subject)"
    snippet = str(item.get("bodyPreview") or "").strip() or None
    code = extract_code(subject, snippet, text_content)

    return MailMessage(
        id=str(item.get("id") or item.get("internetMessageId") or "").strip(),
        folder=folder,
        subject=subject,
        sender_name=sender_name,
        sender_address=sender_address,
        body_preview=snippet or _truncate(text_content),
        received_at=_parse_graph_time(str(item.get("receivedDateTime") or "").strip() or None),
        text=text_content or None,
        code=code,
    )


def _normalize_outlook_rest_message(item: dict[str, Any], folder: str) -> MailMessage:
    sender_name = None
    sender_address = None
    sender = item.get("From")
    if isinstance(sender, dict):
        email_address = sender.get("EmailAddress")
        if isinstance(email_address, dict):
            sender_name = str(email_address.get("Name") or "").strip() or None
            sender_address = str(email_address.get("Address") or "").strip() or None

    recipients = []
    for recipient in item.get("ToRecipients") if isinstance(item.get("ToRecipients"), list) else []:
        if not isinstance(recipient, dict):
            continue
        email_address = recipient.get("EmailAddress")
        if not isinstance(email_address, dict):
            continue
        address = str(email_address.get("Address") or "").strip()
        if address:
            recipients.append(address)

    body = item.get("Body") if isinstance(item.get("Body"), dict) else {}
    body_content = str(body.get("Content") or "")
    body_type = str(body.get("ContentType") or "Text").lower()
    text_content = _strip_html(body_content) if body_type == "html" else body_content

    subject = str(item.get("Subject") or "").strip() or "(No subject)"
    snippet = str(item.get("BodyPreview") or "").strip() or None
    code = extract_code(subject, snippet, text_content)

    return MailMessage(
        id=str(item.get("Id") or "").strip(),
        folder=folder,
        subject=subject,
        sender_name=sender_name,
        sender_address=sender_address,
        body_preview=snippet or _truncate(text_content),
        received_at=_parse_graph_time(str(item.get("ReceivedDateTime") or "").strip() or None),
        text=text_content or None,
        code=code,
    )


def _custom_message_to_mail_message(message: Any, folder: str) -> MailMessage:
    if not isinstance(message, dict):
        return MailMessage(
            id=str(hash(str(message))),
            folder=folder,
            subject=None,
            sender_name=None,
            sender_address=None,
            body_preview=str(message),
            received_at=None,
            text=str(message),
        )
    sender = message.get("sender") or message.get("from") or {}
    if isinstance(sender, str):
        sender_name = None
        sender_address = sender
    elif isinstance(sender, dict):
        sender_name = sender.get("name")
        sender_address = sender.get("address") or sender.get("email")
    else:
        sender_name = None
        sender_address = None
    body_text = message.get("text") or message.get("body_preview") or message.get("bodyPreview") or message.get("preview")
    return MailMessage(
        id=str(message.get("id") or message.get("message_id") or hash(str(message))),
        folder=folder,
        subject=message.get("subject"),
        sender_name=sender_name,
        sender_address=sender_address,
        body_preview=message.get("body_preview") or message.get("bodyPreview") or message.get("preview") or _truncate(str(body_text or "")),
        received_at=_parse_graph_time(message.get("received_at") or message.get("receivedDateTime")),
        text=str(body_text) if body_text else None,
        code=message.get("code") or extract_code(str(message.get("subject") or ""), str(body_text or "")),
    )


def _imap_message_to_mail_message(raw: bytes, fallback_id: str, folder: str) -> MailMessage:
    message = message_from_bytes(raw)
    sender_name, sender_address = parseaddr(_decode_header_value(message.get("From")))
    subject = _decode_header_value(message.get("Subject"))
    body_text = _message_preview(message)
    return MailMessage(
        id=message.get("Message-ID") or fallback_id,
        folder="junk" if folder == "junk" else "inbox",
        subject=subject,
        sender_name=sender_name or None,
        sender_address=sender_address or None,
        body_preview=_truncate(body_text),
        received_at=_parse_email_time(message.get("Date")),
        text=body_text,
        code=extract_code(subject, body_text),
    )


def _group_imap_fetch_parts(fetch_data: list[Any]) -> dict[str, list[bytes]]:
    grouped: dict[str, list[bytes]] = {}
    current_uid: str | None = None
    current_chunks: list[bytes] = []

    for part in fetch_data or []:
        if isinstance(part, tuple) and isinstance(part[1], bytes):
            metadata = part[0] if isinstance(part[0], bytes) else b""
            uid_match = IMAP_UID_RE.search(metadata)
            if uid_match:
                if current_uid and current_chunks:
                    grouped.setdefault(current_uid, []).extend(current_chunks)
                current_uid = uid_match.group(1).decode("ascii", errors="ignore")
                current_chunks = []
            if current_uid:
                current_chunks.append(part[1])
        elif isinstance(part, bytes) and part.strip().endswith(b")"):
            if current_uid and current_chunks:
                grouped.setdefault(current_uid, []).extend(current_chunks)
            current_uid = None
            current_chunks = []

    if current_uid and current_chunks:
        grouped.setdefault(current_uid, []).extend(current_chunks)
    return grouped


def _imap_chunks_to_mail_message(chunks: list[bytes], fallback_id: str, folder: str) -> MailMessage:
    header = chunks[0] if chunks else b""
    body = b"\r\n".join(chunks[1:])
    raw = header.rstrip() + b"\r\n\r\n" + body if body else header
    return _imap_message_to_mail_message(raw, fallback_id, folder)


def _is_imap_cooldown_error(exc: OSError) -> bool:
    value = str(exc).lower()
    return "timed out" in value or "unexpected_eof" in value or "eof occurred" in value


def _has_imap_network_failure(errors: list[str] | str | None) -> bool:
    if not errors:
        return False
    value = " ".join(errors) if isinstance(errors, list) else str(errors)
    lowered = value.lower()
    if "imap hosts are cooling down" in lowered:
        return True
    return "imap connection failed" in lowered and ("timed out" in lowered or "unexpected_eof" in lowered or "eof occurred" in lowered)


def _decode_header_value(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return str(make_header(decode_header(value)))
    except (LookupError, ValueError):
        return value


def _message_preview(message: Any) -> str | None:
    parts = message.walk() if message.is_multipart() else [message]
    html_fallback = None
    for part in parts:
        if part.get_content_maintype() == "multipart":
            continue
        if (part.get("Content-Disposition") or "").lower().startswith("attachment"):
            continue
        payload = part.get_payload(decode=True)
        if not payload:
            continue
        charset = part.get_content_charset() or "utf-8"
        try:
            text = payload.decode(charset, errors="replace")
        except LookupError:
            text = payload.decode("utf-8", errors="replace")
        if part.get_content_type() == "text/plain":
            return _truncate(text)
        if part.get_content_type() == "text/html" and html_fallback is None:
            html_fallback = _truncate(_strip_html(text))
    return html_fallback


def _strip_html(value: str | None) -> str:
    if not value:
        return ""
    text = re.sub(r"<style[\s\S]*?</style>", " ", value, flags=re.I)
    text = re.sub(r"<script[\s\S]*?</script>", " ", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _truncate(value: str | None, limit: int = 500) -> str | None:
    if not value:
        return None
    text = re.sub(r"\s+", " ", value).strip()
    return text[:limit] if text else None


def _parse_graph_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        normalized = value.replace("Z", "+00:00")
        parsed = datetime.fromisoformat(normalized)
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed
    except ValueError:
        return None


def _parse_email_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def _microsoft_error(value: str) -> str:
    try:
        payload = json.loads(value)
    except json.JSONDecodeError:
        return value
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict):
            return str(error.get("message") or error)
        return str(payload.get("error_description") or payload.get("error") or payload)
    return str(payload)
