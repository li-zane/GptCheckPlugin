import asyncio
import hashlib
import html
import imaplib
import json
import logging
import re
import socket
import time
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timezone, tzinfo
from email import message_from_bytes
from email.header import decode_header, make_header
from email.utils import parseaddr, parsedate_to_datetime
from html.parser import HTMLParser
from typing import Any, ClassVar, Protocol
from urllib import parse as urllib_parse

import httpx

from app.core.config import Settings, get_settings
from app.core.crypto import decrypt_text
from app.models import MailboxCredential


CODE_RE = re.compile(r"\b(\d{6})\b")
IMAP_UID_RE = re.compile(rb"\bUID\s+(\d+)\b")
EMAIL_RE = re.compile(r"[A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,}", re.IGNORECASE)
AUTH_METHOD_RE = re.compile(r"\b(spf|dkim|dmarc|compauth|arc)\s*=\s*([a-z]+)", re.IGNORECASE)
FOLDER_MAP = {
    "inbox": "inbox",
    "junk": "junkemail",
    "INBOX": "inbox",
    "Junk": "junkemail",
}
OUTLOOK_REST_BASE = "https://outlook.office.com/api/v2.0"
GMAIL_IMAP_HOST = "imap.gmail.com"
GMAIL_IMAP_PORT = 993
_RECIPIENT_HEADERS = (
    "To",
    "Cc",
    "Delivered-To",
    "X-Original-To",
    "Original-Recipient",
    "Final-Recipient",
    "Envelope-To",
    "X-Envelope-To",
    "X-Forwarded-To",
    "X-MS-Exchange-Organization-OriginalEnvelopeRecipients",
)
_OPENAI_SENDER_DOMAINS = ("openai.com", "chatgpt.com")
_PICKUP_HTTP_LOG_REDACTION: ContextVar[bool] = ContextVar("pickup_http_log_redaction", default=False)


class _PickupHttpLogFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if not _PICKUP_HTTP_LOG_REDACTION.get() or not isinstance(record.args, tuple) or len(record.args) < 2:
            return True
        if isinstance(record.msg, str) and record.msg.startswith("HTTP Request:"):
            args = list(record.args)
            args[1] = "[pickup endpoint redacted]"
            record.args = tuple(args)
        return True


logging.getLogger("httpx").addFilter(_PickupHttpLogFilter())


class _ProxyImap4Ssl(imaplib.IMAP4_SSL):
    def __init__(self, host: str, port: int, *, proxy_url: str, timeout: float | None = None) -> None:
        self._proxy_url = proxy_url
        super().__init__(host, port, timeout=timeout)

    def _create_socket(self, timeout: float | None) -> socket.socket:
        if timeout is not None and not timeout:
            raise ValueError("Non-blocking socket (timeout=0) is not supported")

        proxy = urllib_parse.urlparse(self._proxy_url)
        scheme = (proxy.scheme or "").lower()
        if scheme == "https":
            raise ValueError("HTTPS Gmail proxies are not supported because TLS-to-proxy is unavailable.")
        if scheme not in {"http", "socks5", "socks5h"}:
            raise ValueError("Unsupported Gmail proxy scheme.")
        if not proxy.hostname or proxy.port is None:
            raise ValueError("Gmail proxy URL must include a host and port.")

        import socks

        proxy_type = socks.PROXY_TYPE_HTTP if scheme == "http" else socks.SOCKS5
        raw_socket = socks.socksocket(socket.AF_INET, socket.SOCK_STREAM)
        raw_socket.set_proxy(
            proxy_type,
            proxy.hostname,
            proxy.port,
            rdns=scheme == "socks5h",
            username=urllib_parse.unquote(proxy.username) if proxy.username else None,
            password=urllib_parse.unquote(proxy.password) if proxy.password else None,
        )
        if timeout is not None:
            raw_socket.settimeout(timeout)
        try:
            raw_socket.connect((self.host, self.port))
            return self.ssl_context.wrap_socket(raw_socket, server_hostname=self.host)
        except Exception:
            raw_socket.close()
            raise


def _best_effort_close_imap(client: imaplib.IMAP4) -> None:
    try:
        client.logout()
        return
    except Exception:
        pass

    try:
        client.shutdown()
        return
    except Exception:
        pass

    try:
        sock = getattr(client, "sock", None)
    except Exception:
        return
    if sock is not None:
        try:
            sock.close()
        except Exception:
            pass


@dataclass
class MailFetchResult:
    status: str
    code: str | None = None
    error: str | None = None
    provider_status: str | None = None
    new_refresh_token: str | None = None
    new_access_token: str | None = None


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
    recipients: tuple[str, ...] = ()
    sender_authenticated: bool = False


@dataclass
class MailMessagesResult:
    status: str
    messages: list[MailMessage]
    error: str | None = None
    provider_status: str | None = None
    new_refresh_token: str | None = None
    new_access_token: str | None = None


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
        new_access_token: str | None = None
        last_error: str | None = None
        for folder in ("inbox", "junk"):
            result = await self.fetch_messages(credential, folder, 10)
            target_recipient = credential.gpt_email.strip().casefold()
            if result.status == "ok" and not _mail_result_usable_for_code(result, target_recipient):
                result = await self._fetch_messages_for_code(credential, folder, 10)
            if result.new_refresh_token:
                new_refresh_token = result.new_refresh_token
            if result.new_access_token:
                new_access_token = result.new_access_token
            if result.status != "ok":
                last_error = result.error
                if folder == "inbox":
                    return MailFetchResult(
                        status="failed",
                        error=result.error,
                        provider_status=result.provider_status,
                        new_refresh_token=new_refresh_token,
                        new_access_token=new_access_token,
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
                if target_recipient not in message.recipients or not message.sender_authenticated:
                    continue
                code = message.code or extract_code(message.subject, message.body_preview, message.text)
                if code:
                    return MailFetchResult(
                        status="ok",
                        code=code,
                        new_refresh_token=new_refresh_token,
                        new_access_token=new_access_token,
                    )

        if last_error:
            return MailFetchResult(
                status="not_found",
                error=f"No fresh verification code message was found. Last mailbox error: {last_error}",
                new_refresh_token=new_refresh_token,
                new_access_token=new_access_token,
            )
        return MailFetchResult(
            status="not_found",
            error="No fresh verification code message was found.",
            new_refresh_token=new_refresh_token,
            new_access_token=new_access_token,
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

    async def _fetch_messages_for_code(
        self,
        credential: MailboxCredential,
        folder: str,
        limit: int,
    ) -> MailMessagesResult:
        try:
            return await asyncio.wait_for(
                self._fetch_messages_inner(
                    credential,
                    folder,
                    limit,
                    target_recipient=credential.gpt_email.strip().casefold(),
                ),
                timeout=self.settings.mail_read_timeout_seconds,
            )
        except asyncio.TimeoutError:
            return MailMessagesResult(
                status="failed",
                messages=[],
                error=f"Mailbox read timed out after {self.settings.mail_read_timeout_seconds} seconds.",
            )

    async def _fetch_messages_inner(
        self,
        credential: MailboxCredential,
        folder: str,
        limit: int,
        *,
        target_recipient: str | None = None,
    ) -> MailMessagesResult:
        decrypted_client_id = decrypt_text(credential.encrypted_client_id)
        decrypted_refresh_token = decrypt_text(credential.encrypted_refresh_token)
        encrypted_access_token = getattr(credential, "encrypted_access_token", None)
        decrypted_access_token = decrypt_text(encrypted_access_token)
        if (credential.encrypted_client_id and decrypted_client_id is None) or (
            credential.encrypted_refresh_token and decrypted_refresh_token is None
        ) or (
            encrypted_access_token and decrypted_access_token is None
        ):
            return MailMessagesResult(
                status="failed",
                messages=[],
                error="Mailbox credentials could not be decrypted. Restore the original APP_ENCRYPTION_KEY or reimport this mailbox.",
            )
        client_id = (decrypted_client_id or "").strip()
        refresh_token = (decrypted_refresh_token or "").strip()
        access_token = (decrypted_access_token or "").strip()
        errors: list[str] = []
        if access_token:
            result = await self._fetch_with_stored_access_token(access_token, folder, limit)
            if _mail_result_usable_for_code(result, target_recipient):
                return result
            if result.error:
                errors.append(result.error)
            elif result.status == "ok":
                errors.append("Stored access_token returned an unauthenticated verification message.")
        if not client_id or not refresh_token:
            error = "Missing client_id or refresh_token."
            if errors:
                error = f"{error} Stored access_token also failed: {' | '.join(errors[-2:])}"
            return MailMessagesResult(status="failed", messages=[], error=error)

        base_cache_key = self._base_cache_key(credential, client_id)

        for strategy in self._strategy_order(base_cache_key):
            if strategy in {"graph_imap", "o2_wl_imap"} and _has_imap_network_failure(errors):
                continue
            result = await self._fetch_with_strategy(credential, folder, limit, client_id, refresh_token, base_cache_key, strategy)
            if _mail_result_usable_for_code(result, target_recipient):
                self._strategy_cache[base_cache_key] = strategy
                return result
            if result.error:
                errors.append(result.error)
            elif result.status == "ok":
                errors.append(f"{strategy} returned an unauthenticated verification message.")

        return MailMessagesResult(
            status="failed",
            messages=[],
            error="Unable to read mailbox. " + " | ".join(errors[-4:]),
        )

    def _strategy_order(self, base_cache_key: str) -> list[str]:
        default_order = [
            "graph_no_scope",
            "consumer_graph_no_scope",
            "outlook_rest",
            "graph",
            "o2_imap",
            "graph_imap",
            "o2_wl_imap",
            "password_imap",
            "external_api",
        ]
        preferred = self._strategy_cache.get(base_cache_key)
        if preferred not in default_order:
            return default_order
        return [preferred, *[strategy for strategy in default_order if strategy != preferred]]

    async def _fetch_with_stored_access_token(self, access_token: str, folder: str, limit: int) -> MailMessagesResult:
        graph_result = await self._fetch_graph_messages(access_token, folder, limit)
        if graph_result.status == "ok":
            return graph_result

        rest_result = await self._fetch_outlook_rest_messages(access_token, folder, limit)
        if rest_result.status == "ok":
            return rest_result

        errors = [error for error in (graph_result.error, rest_result.error) if error]
        return MailMessagesResult(
            status="failed",
            messages=[],
            error="Stored access_token could not read mailbox. " + " | ".join(errors[-2:]),
        )

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
        if strategy == "external_api":
            return await self._fetch_external_mail_messages(credential, folder, limit, client_id, refresh_token)

        if strategy == "password_imap":
            return await self._fetch_password_imap_messages(credential, folder, limit)

        if strategy in {"graph_no_scope", "consumer_graph_no_scope"}:
            token_result = await self._cached_access_token(
                base_cache_key=base_cache_key,
                refresh_token=refresh_token,
                strategy=strategy,
                client_id=client_id,
                token_url=self.settings.graph_consumer_token_url
                if strategy == "consumer_graph_no_scope"
                else self.settings.graph_token_url,
                scope=None,
                label="Graph/no-scope" if strategy == "graph_no_scope" else "Graph consumers/no-scope",
            )
            if token_result.status != "ok" or not token_result.access_token:
                return MailMessagesResult(
                    status="failed",
                    messages=[],
                    error=token_result.error or "Graph no-scope token refresh failed.",
                    provider_status=token_result.provider_status,
                )
            graph_result = await self._fetch_graph_messages(token_result.access_token, folder, limit)
            if graph_result.status == "ok":
                graph_result.new_refresh_token = token_result.new_refresh_token
                graph_result.new_access_token = token_result.access_token
                return graph_result

            rest_result = await self._fetch_outlook_rest_messages(token_result.access_token, folder, limit)
            rest_result.new_refresh_token = token_result.new_refresh_token
            rest_result.new_access_token = token_result.access_token
            if rest_result.status != "ok":
                self._drop_cached_access_token(base_cache_key, refresh_token, strategy)
                if graph_result.error and rest_result.error:
                    rest_result.error = f"{graph_result.error} | {rest_result.error}"
            return rest_result

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
            rest_result.new_access_token = token_result.access_token
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
            graph_result.new_access_token = token_result.access_token
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
        imap_result.new_access_token = token_result.access_token
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
        pages = 0

        try:
            async with httpx.AsyncClient(timeout=self.settings.mail_token_timeout_seconds) as client:
                while url and len(messages) < limit:
                    pages += 1
                    if pages > 10:
                        return MailMessagesResult(
                            status="failed",
                            messages=[],
                            error="Graph mail pagination exceeded the page limit.",
                        )
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
                    raw_next_link = payload.get("@odata.nextLink")
                    try:
                        url = _safe_graph_next_link(raw_next_link)
                    except ValueError:
                        return MailMessagesResult(
                            status="failed",
                            messages=[],
                            error="Graph mail pagination returned an invalid destination.",
                        )
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
            "$select": "Id,Subject,BodyPreview,ReceivedDateTime,From,ToRecipients,CcRecipients,InternetMessageHeaders,Body",
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

    async def _fetch_external_mail_messages(
        self,
        credential: MailboxCredential,
        folder: str,
        limit: int,
        client_id: str,
        refresh_token: str,
    ) -> MailMessagesResult:
        base_url = str(self.settings.external_mail_api_base or "").strip().rstrip("/")
        if not base_url:
            return MailMessagesResult(status="failed", messages=[], error="External mail API is not configured.")

        result = await self._request_external_mail_messages(
            base_url=base_url,
            email_address=credential.mailbox_email,
            client_id=client_id,
            refresh_token=refresh_token,
            folder=folder,
            limit=limit,
        )
        if (
            result.status == "failed"
            and result.new_refresh_token
            and result.new_refresh_token != refresh_token
        ):
            retry_result = await self._request_external_mail_messages(
                base_url=base_url,
                email_address=credential.mailbox_email,
                client_id=client_id,
                refresh_token=result.new_refresh_token,
                folder=folder,
                limit=limit,
            )
            if retry_result.status == "ok":
                retry_result.new_refresh_token = retry_result.new_refresh_token or result.new_refresh_token
                return retry_result
        return result

    async def _request_external_mail_messages(
        self,
        *,
        base_url: str,
        email_address: str,
        client_id: str,
        refresh_token: str,
        folder: str,
        limit: int,
    ) -> MailMessagesResult:
        mailbox_name = "Junk" if folder == "junk" else "INBOX"
        url = f"{base_url}/api/mail-all"
        params = {
            "refresh_token": refresh_token,
            "client_id": client_id,
            "email": email_address,
            "mailbox": mailbox_name,
        }
        headers = {
            "Accept": "application/json, text/plain, */*",
            "User-Agent": "Mozilla/5.0",
            "Referer": f"{base_url}/mail.html",
            "Origin": base_url,
        }

        try:
            async with httpx.AsyncClient(timeout=self.settings.external_mail_timeout_seconds) as client:
                response = await client.get(url, params=params, headers=headers)
        except httpx.HTTPError as exc:
            return MailMessagesResult(status="failed", messages=[], error=f"External mail API request failed: {exc}")

        new_refresh_token: str | None = None
        try:
            payload: Any = response.json()
        except ValueError:
            payload = response.text

        if isinstance(payload, dict):
            new_refresh_token = _pick_mail_value(payload, "new_refresh_token")
            data = payload.get("data")
            if isinstance(data, dict):
                new_refresh_token = _pick_mail_value(data, "new_refresh_token") or new_refresh_token

        if response.status_code >= 400:
            detail = _external_mail_error(payload) or response.reason_phrase or response.text[:300]
            return MailMessagesResult(
                status="failed",
                messages=[],
                error=f"External mail API read failed: HTTP {response.status_code}: {detail}",
                provider_status=response.text[:500],
                new_refresh_token=new_refresh_token,
            )

        try:
            raw_messages, payload_refresh_token = _unwrap_external_mail_items(payload)
        except ValueError as exc:
            return MailMessagesResult(
                status="failed",
                messages=[],
                error=f"External mail API response format is invalid: {exc}",
                provider_status=response.text[:500],
                new_refresh_token=new_refresh_token,
            )
        new_refresh_token = payload_refresh_token or new_refresh_token
        messages = [
            _external_message_to_mail_message(message, "junk" if folder == "junk" else "inbox", index)
            for index, message in enumerate(raw_messages[: max(1, min(int(limit), 100))], start=1)
        ]
        return MailMessagesResult(
            status="ok",
            messages=messages,
            new_refresh_token=new_refresh_token if new_refresh_token and new_refresh_token != refresh_token else None,
        )

    async def _fetch_imap_messages(self, credential: MailboxCredential, access_token: str, folder: str, limit: int) -> MailMessagesResult:
        return await asyncio.to_thread(self._fetch_imap_messages_sync, credential, access_token, folder, limit)

    async def _fetch_password_imap_messages(self, credential: MailboxCredential, folder: str, limit: int) -> MailMessagesResult:
        return await asyncio.to_thread(self._fetch_password_imap_messages_sync, credential, folder, limit)

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
                    _best_effort_close_imap(client)
        return MailMessagesResult(status="failed", messages=[], error="; ".join(errors))

    def _fetch_password_imap_messages_sync(self, credential: MailboxCredential, folder: str, limit: int) -> MailMessagesResult:
        password = (decrypt_text(credential.encrypted_password) or "").strip()
        if not password:
            return MailMessagesResult(status="failed", messages=[], error="Missing mailbox password for IMAP password fallback.")

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
                client.login(credential.mailbox_email, password)
                messages = self._read_imap_messages(client, folder, limit)
                self._imap_host_cooldown.pop(host, None)
                return MailMessagesResult(status="ok", messages=messages)
            except imaplib.IMAP4.error as exc:
                errors.append(f"{host}: IMAP password login failed: {exc}")
            except OSError as exc:
                errors.append(f"{host}: IMAP connection failed: {exc}")
                if _is_imap_cooldown_error(exc):
                    self._imap_host_cooldown[host] = time.monotonic() + self.settings.outlook_imap_failure_cooldown_seconds
            finally:
                if client is not None:
                    _best_effort_close_imap(client)
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
            "(FROM TO CC DELIVERED-TO X-ORIGINAL-TO ORIGINAL-RECIPIENT FINAL-RECIPIENT "
            "ENVELOPE-TO X-ENVELOPE-TO X-FORWARDED-TO "
            "X-MS-EXCHANGE-ORGANIZATION-ORIGINALENVELOPERECIPIENTS SUBJECT DATE MESSAGE-ID "
            "AUTHENTICATION-RESULTS CONTENT-TYPE CONTENT-TRANSFER-ENCODING MIME-VERSION)] "
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


class UrlPickupMailAdapter:
    _max_response_bytes = 2_000_000

    async def fetch_code(self, credential: MailboxCredential, after: datetime) -> MailFetchResult:
        result = await self.fetch_messages(credential, "inbox", 50)
        if result.status != "ok":
            return MailFetchResult(
                status="failed",
                error=result.error,
                provider_status=result.provider_status,
            )

        normalized_after = _normalize_mail_datetime(after)
        for message in _sort_pickup_messages(result.messages):
            if message.received_at and _normalize_mail_datetime(message.received_at) < normalized_after:
                continue
            code = extract_code(message.code, message.subject, message.body_preview, message.text)
            if code:
                return MailFetchResult(status="ok", code=code)
        return MailFetchResult(status="not_found", error="No fresh verification code was found at the pickup endpoint.")

    async def fetch_messages(self, credential: MailboxCredential, folder: str, limit: int) -> MailMessagesResult:
        if folder != "inbox":
            return MailMessagesResult(status="ok", messages=[])
        endpoint = str(credential.custom_fetch_url or "").strip()
        if not _valid_pickup_endpoint(endpoint):
            return MailMessagesResult(status="failed", messages=[], error="The pickup endpoint URL is invalid.")

        log_context = _PICKUP_HTTP_LOG_REDACTION.set(True)
        try:
            async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
                response = await client.get(
                    endpoint,
                    headers={"Accept": "application/json, text/html;q=0.9, text/plain;q=0.8"},
                )
        except httpx.HTTPError as exc:
            return MailMessagesResult(
                status="failed",
                messages=[],
                error=f"Pickup endpoint request failed ({type(exc).__name__}).",
            )
        finally:
            _PICKUP_HTTP_LOG_REDACTION.reset(log_context)

        if response.status_code >= 400:
            return MailMessagesResult(
                status="failed",
                messages=[],
                error=f"Pickup endpoint returned HTTP {response.status_code}.",
                provider_status=f"HTTP {response.status_code}",
            )
        if len(response.content) > self._max_response_bytes:
            return MailMessagesResult(
                status="failed",
                messages=[],
                error="Pickup endpoint response exceeded the 2 MB limit.",
            )

        try:
            messages = _pickup_response_messages(response)
        except (UnicodeError, ValueError):
            return MailMessagesResult(
                status="failed",
                messages=[],
                error="Pickup endpoint response could not be parsed.",
            )
        bounded_limit = max(1, min(limit, 50))
        return MailMessagesResult(status="ok", messages=_sort_pickup_messages(messages)[:bounded_limit])


class GmailImapAdapter:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    async def fetch_code(self, credential: MailboxCredential, after: datetime) -> MailFetchResult:
        last_error: str | None = None
        for folder in ("inbox", "junk"):
            result = await self.fetch_messages(credential, folder, 20)
            if result.status != "ok":
                last_error = result.error or last_error
                if folder == "inbox":
                    return MailFetchResult(status="failed", error=result.error, provider_status=result.provider_status)
                continue

            messages = sorted(
                result.messages,
                key=lambda item: item.received_at or datetime.min.replace(tzinfo=timezone.utc),
                reverse=True,
            )
            for message in messages:
                if message.received_at and message.received_at < after:
                    continue
                target_recipient = credential.gpt_email.strip().casefold()
                if target_recipient not in message.recipients or not message.sender_authenticated:
                    continue
                code = message.code or extract_code(message.subject, message.body_preview, message.text)
                if code:
                    return MailFetchResult(status="ok", code=code)

        if last_error:
            return MailFetchResult(
                status="not_found",
                error=f"No fresh verification code message was found. Last mailbox error: {last_error}",
            )
        return MailFetchResult(status="not_found", error="No fresh verification code message was found.")

    async def fetch_messages(self, credential: MailboxCredential, folder: str, limit: int) -> MailMessagesResult:
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(self._fetch_messages_sync, credential, folder, limit),
                timeout=self.settings.mail_read_timeout_seconds,
            )
        except asyncio.TimeoutError:
            return MailMessagesResult(
                status="failed",
                messages=[],
                error=f"Mailbox read timed out after {self.settings.mail_read_timeout_seconds} seconds.",
            )

    def _fetch_messages_sync(self, credential: MailboxCredential, folder: str, limit: int) -> MailMessagesResult:
        encrypted_password = getattr(credential, "encrypted_password", None)
        decrypted_password = decrypt_text(encrypted_password)
        if encrypted_password and decrypted_password is None:
            return MailMessagesResult(
                status="failed",
                messages=[],
                error="Mailbox credentials could not be decrypted. Restore the original APP_ENCRYPTION_KEY or reimport this mailbox.",
            )

        password = (decrypted_password or "").strip()
        if not password:
            return MailMessagesResult(status="failed", messages=[], error="Missing mailbox password for Gmail IMAP.")

        client: imaplib.IMAP4_SSL | None = None
        proxy_url = str(getattr(credential, "proxy_url", None) or "").strip() or None
        try:
            client_type = _ProxyImap4Ssl if proxy_url else imaplib.IMAP4_SSL
            client_kwargs: dict[str, Any] = {
                "timeout": self.settings.outlook_imap_timeout_seconds,
            }
            if proxy_url:
                client_kwargs["proxy_url"] = proxy_url
            client = client_type(GMAIL_IMAP_HOST, GMAIL_IMAP_PORT, **client_kwargs)
            client.login(credential.mailbox_email, password)
            messages = self._read_filtered_messages(client, credential, folder, limit)
            return MailMessagesResult(status="ok", messages=messages)
        except imaplib.IMAP4.error as exc:
            return MailMessagesResult(status="failed", messages=[], error=f"Gmail IMAP login failed: {exc}")
        except (ImportError, ValueError) as exc:
            return MailMessagesResult(
                status="failed",
                messages=[],
                error=f"Gmail proxy configuration failed: {exc}",
            )
        except OSError as exc:
            return MailMessagesResult(status="failed", messages=[], error=f"Gmail IMAP connection failed: {exc}")
        finally:
            if client is not None:
                _best_effort_close_imap(client)

    def _read_filtered_messages(
        self,
        client: imaplib.IMAP4_SSL,
        credential: MailboxCredential,
        folder: str,
        limit: int,
    ) -> list[MailMessage]:
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

        all_uids = [uid for uid in data[0].split() if uid]
        if not all_uids:
            return []

        search_window = min(max(max(1, min(limit, 50)) * 10, 50), 200)
        fetch_uids = all_uids[-search_window:]
        uid_set = b",".join(fetch_uids).decode("ascii", errors="ignore")
        fetch_spec = "(UID BODY.PEEK[HEADER] BODY.PEEK[TEXT]<0.4096>)"
        fetch_status, fetch_data = client.uid("FETCH", uid_set, fetch_spec)
        if fetch_status != "OK":
            return []

        grouped_parts = _group_imap_fetch_parts(fetch_data)
        target_recipient = credential.gpt_email.lower().strip()
        messages: list[MailMessage] = []
        for uid in reversed(fetch_uids):
            uid_text = uid.decode("ascii", errors="ignore")
            chunks = grouped_parts.get(uid_text)
            if not chunks:
                continue
            message = _imap_chunks_to_mail_message(chunks, uid_text, folder)
            if not _imap_chunks_match_recipient(chunks, target_recipient):
                continue
            messages.append(message)
            if len(messages) >= max(1, min(limit, 50)):
                break
        return messages

    def _imap_folder_candidates(self, folder: str) -> list[str]:
        if folder == "junk":
            return ['"[Gmail]/Spam"', '"[Google Mail]/Spam"', '"Spam"']
        return ["INBOX"]


class ManualMailAdapter:
    async def fetch_code(self, credential: MailboxCredential, after: datetime) -> MailFetchResult:
        return MailFetchResult(status="not_found", error="Manual provider cannot fetch codes automatically.")

    async def fetch_messages(self, credential: MailboxCredential, folder: str, limit: int) -> MailMessagesResult:
        return MailMessagesResult(status="ok", messages=[])


class MailAdapterRegistry:
    def __init__(self) -> None:
        outlook = OutlookGraphAdapter()
        gmail = GmailImapAdapter()
        self.adapters: dict[str, MailAdapter] = {
            "outlook": outlook,
            "hotmail": outlook,
            "gmail": gmail,
            "custom": CustomHttpMailAdapter(),
            "url": UrlPickupMailAdapter(),
            "manual": ManualMailAdapter(),
        }

    async def fetch_code(self, credential: MailboxCredential, after: datetime) -> MailFetchResult:
        adapter = self.adapters.get(credential.provider.lower(), self.adapters["manual"])
        return await adapter.fetch_code(credential, after)

    async def fetch_messages(self, credential: MailboxCredential, folder: str, limit: int) -> MailMessagesResult:
        adapter = self.adapters.get(credential.provider.lower(), self.adapters["manual"])
        return await adapter.fetch_messages(credential, folder, limit)


_RECIPIENT_HEADER_NAMES = {name.casefold() for name in _RECIPIENT_HEADERS}


def _safe_graph_next_link(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError("invalid Graph nextLink")
    resolved = urllib_parse.urljoin("https://graph.microsoft.com/", value.strip())
    parsed = urllib_parse.urlparse(resolved)
    if (
        parsed.scheme.casefold() != "https"
        or (parsed.hostname or "").casefold() != "graph.microsoft.com"
        or parsed.port not in {None, 443}
        or parsed.username is not None
        or parsed.password is not None
        or not parsed.path.startswith("/v1.0/")
        or parsed.fragment
    ):
        raise ValueError("invalid Graph nextLink")
    return resolved


def _microsoft_message_recipients(
    item: dict[str, Any],
    *,
    recipient_keys: tuple[str, ...],
    headers_key: str,
) -> tuple[str, ...]:
    candidates: set[str] = set()
    for key in recipient_keys:
        candidates.update(_extract_email_candidates(item.get(key)))
    headers = item.get(headers_key)
    if isinstance(headers, list):
        for header in headers:
            if not isinstance(header, dict):
                continue
            name = str(header.get("name") or header.get("Name") or "").strip().casefold()
            if name not in _RECIPIENT_HEADER_NAMES:
                continue
            candidates.update(_extract_email_candidates(header.get("value") or header.get("Value")))
    return tuple(sorted(candidates))


def _microsoft_sender_authenticated(
    item: dict[str, Any],
    *,
    sender_address: str | None,
    headers_key: str,
) -> bool:
    headers = item.get(headers_key)
    authentication_results: list[str] = []
    if isinstance(headers, list):
        for header in headers:
            if not isinstance(header, dict):
                continue
            name = str(header.get("name") or header.get("Name") or "").strip().casefold()
            if name != "authentication-results":
                continue
            value = str(header.get("value") or header.get("Value") or "").strip()
            if value:
                authentication_results.append(value)
    return _openai_sender_authenticated(sender_address, authentication_results)


def _openai_sender_authenticated(
    sender_address: str | None,
    authentication_results: list[str],
) -> bool:
    sender_domain = _email_domain(sender_address)
    if not _is_openai_domain(sender_domain) or not authentication_results:
        return False

    # The receiving provider prepends its own Authentication-Results header.
    # Ignore later headers because they can be supplied by the sender.
    result = re.sub(r"\s+", " ", authentication_results[0]).strip().casefold()
    if not _looks_like_provider_authentication_result(result):
        return False

    method_matches = list(AUTH_METHOD_RE.finditer(result))
    for index, method_match in enumerate(method_matches):
        method, status = method_match.groups()
        method = method.casefold()
        if status.casefold() != "pass" or method not in {"dkim", "dmarc"}:
            continue
        block_end = method_matches[index + 1].start() if index + 1 < len(method_matches) else len(result)
        result_block = result[method_match.start():block_end]
        identity_keys = ("header.d", "header.i") if method == "dkim" else ("header.from",)
        for identity_key in identity_keys:
            pattern = rf"\b{re.escape(identity_key)}\s*=\s*[\"']?@?([a-z0-9.-]+)"
            for matched_domain in re.findall(pattern, result_block):
                if _is_openai_domain(matched_domain.rstrip(".")):
                    return True
    return False


def _looks_like_provider_authentication_result(value: str) -> bool:
    prefix = value.split(";", 1)[0].strip().rstrip(".")
    if "=" in prefix:
        # Exchange Online commonly omits authserv-id and starts with spf=.
        return True
    auth_service = prefix.split()[0] if prefix else ""
    return (
        auth_service == "mx.google.com"
        or auth_service.endswith(".outlook.com")
        or auth_service.endswith(".protection.outlook.com")
    )


def _email_domain(address: str | None) -> str:
    parsed_address = parseaddr(str(address or ""))[1].strip().casefold()
    if "@" not in parsed_address:
        return ""
    return parsed_address.rsplit("@", 1)[1].rstrip(".")


def _is_openai_domain(domain: str) -> bool:
    return any(domain == root or domain.endswith(f".{root}") for root in _OPENAI_SENDER_DOMAINS)


def _mail_result_usable_for_code(
    result: MailMessagesResult,
    target_recipient: str | None,
) -> bool:
    if result.status != "ok":
        return False
    if not target_recipient:
        return True
    candidates = [
        message
        for message in result.messages
        if target_recipient in message.recipients
        and (message.code or extract_code(message.subject, message.body_preview, message.text))
    ]
    return not candidates or any(message.sender_authenticated for message in candidates)


def _message_recipient_candidates(*messages: dict[str, Any]) -> tuple[str, ...]:
    candidates: set[str] = set()
    for message in messages:
        for key in (
            "to",
            "cc",
            "recipients",
            "to_recipients",
            "toRecipients",
            "delivered_to",
            "original_recipient",
            "originalRecipients",
        ):
            candidates.update(_extract_email_candidates(message.get(key)))
    return tuple(sorted(candidates))


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
        recipients=_microsoft_message_recipients(
            item,
            recipient_keys=("toRecipients", "ccRecipients"),
            headers_key="internetMessageHeaders",
        ),
        sender_authenticated=_microsoft_sender_authenticated(
            item,
            sender_address=sender_address,
            headers_key="internetMessageHeaders",
        ),
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
        recipients=_microsoft_message_recipients(
            item,
            recipient_keys=("ToRecipients", "CcRecipients"),
            headers_key="InternetMessageHeaders",
        ),
        sender_authenticated=_microsoft_sender_authenticated(
            item,
            sender_address=sender_address,
            headers_key="InternetMessageHeaders",
        ),
    )


_PICKUP_CONTAINER_KEYS = {"data", "items", "list", "mails", "messages", "records", "results", "inbox"}
_PICKUP_MESSAGE_KEYS = {
    "body",
    "body_html",
    "body_text",
    "captcha",
    "code",
    "content",
    "date",
    "from",
    "html",
    "internaldate",
    "message",
    "otp",
    "plain",
    "preview",
    "received_at",
    "receiveddatetime",
    "sender",
    "sent_at",
    "snippet",
    "subject",
    "text",
    "title",
    "verification_code",
    "verificationcode",
}
_PICKUP_TEXT_KEYS = {
    "body",
    "body_html",
    "body_text",
    "captcha",
    "code",
    "content",
    "html",
    "message",
    "otp",
    "plain",
    "preview",
    "snippet",
    "subject",
    "text",
    "title",
    "verification_code",
    "verificationcode",
}
_PICKUP_TIME_KEYS = {
    "created_at",
    "date",
    "internaldate",
    "received_at",
    "receiveddatetime",
    "sent_at",
}


def _valid_pickup_endpoint(value: str) -> bool:
    try:
        parsed = urllib_parse.urlparse(value)
        return bool(
            parsed.scheme.casefold() in {"http", "https"}
            and parsed.hostname
            and parsed.username is None
            and parsed.password is None
        )
    except ValueError:
        return False


def _normalize_mail_datetime(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _pickup_local_timezone() -> tzinfo:
    return datetime.now().astimezone().tzinfo or timezone.utc


def _parse_pickup_time(value: str | None) -> datetime | None:
    if not value:
        return None
    normalized = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return _parse_mail_time(value)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=_pickup_local_timezone())
    return parsed


def _sort_pickup_messages(messages: list[MailMessage]) -> list[MailMessage]:
    indexed = list(enumerate(messages))

    def sort_key(item: tuple[int, MailMessage]) -> tuple[bool, datetime, int]:
        index, message = item
        received_at = message.received_at
        return (
            received_at is not None,
            _normalize_mail_datetime(received_at) if received_at else datetime.min.replace(tzinfo=timezone.utc),
            -index,
        )

    return [message for _, message in sorted(indexed, key=sort_key, reverse=True)]


def _pickup_response_messages(response: httpx.Response) -> list[MailMessage]:
    text = response.text
    stripped = text.lstrip()
    content_type = response.headers.get("content-type", "").casefold()
    payload: Any = None
    if "json" in content_type or stripped.startswith(("{", "[")):
        try:
            payload = response.json()
        except ValueError:
            if "json" in content_type:
                raise ValueError("invalid JSON response") from None

    if payload is not None:
        return _pickup_json_messages(payload)
    if "html" in content_type or re.search(r"<(?:!doctype\s+html|html|article|body)\b", stripped, re.I):
        parser = _PickupHtmlParser()
        parser.feed(text)
        parser.close()
        return parser.as_messages()

    plain_text = text.strip()
    if not plain_text:
        return []
    return [
        MailMessage(
            id=f"pickup-{hashlib.sha256(plain_text.encode('utf-8')).hexdigest()[:16]}",
            folder="inbox",
            subject=None,
            sender_name=None,
            sender_address=None,
            body_preview=_truncate(plain_text),
            received_at=None,
            text=plain_text,
            code=extract_code(plain_text),
        )
    ]


def _pickup_json_messages(payload: Any) -> list[MailMessage]:
    candidates: list[Any] = []

    def collect(value: Any, depth: int = 0) -> None:
        if depth > 10:
            return
        if isinstance(value, list):
            for item in value:
                collect(item, depth + 1)
            return
        if not isinstance(value, dict):
            if isinstance(value, (str, int)) and extract_code(str(value)):
                candidates.append(value)
            return

        nested_values = [item for key, item in value.items() if str(key).casefold() in _PICKUP_CONTAINER_KEYS]
        before = len(candidates)
        for nested in nested_values:
            collect(nested, depth + 1)
        if len(candidates) > before:
            return
        if any(str(key).casefold() in _PICKUP_MESSAGE_KEYS for key in value):
            candidates.append(value)

    collect(payload)
    if not candidates:
        semantic_text = "\n".join(_pickup_semantic_texts(payload))
        if extract_code(semantic_text):
            candidates.append(semantic_text)

    messages: list[MailMessage] = []
    for index, candidate in enumerate(candidates):
        message = _external_message_to_mail_message(candidate, "inbox", index)
        candidate_time = _pickup_candidate_time(candidate)
        if candidate_time is not None:
            message.received_at = candidate_time
        semantic_texts = _pickup_semantic_texts(candidate)
        semantic_text = "\n".join(semantic_texts) or None
        normalized_code = extract_code(message.code, semantic_text, message.subject, message.body_preview, message.text)
        message.code = normalized_code
        if message.text is None and semantic_text:
            message.text = semantic_text
        if message.body_preview is None and semantic_text:
            message.body_preview = _truncate(semantic_text)
        messages.append(message)
    return messages


def _pickup_candidate_time(value: Any) -> datetime | None:
    if not isinstance(value, dict):
        return None
    for key, item in value.items():
        if str(key).casefold() in _PICKUP_TIME_KEYS and item is not None:
            parsed = _parse_pickup_time(str(item))
            if parsed is not None:
                return parsed
    raw = value.get("raw")
    return _pickup_candidate_time(raw) if isinstance(raw, dict) else None


def _pickup_semantic_texts(value: Any, depth: int = 0) -> list[str]:
    if depth > 10:
        return []
    if isinstance(value, list):
        result: list[str] = []
        for item in value:
            result.extend(_pickup_semantic_texts(item, depth + 1))
        return result
    if not isinstance(value, dict):
        return [str(value)] if isinstance(value, (str, int)) else []

    result: list[str] = []
    for key, item in value.items():
        normalized_key = str(key).casefold()
        if normalized_key not in _PICKUP_TEXT_KEYS:
            continue
        if isinstance(item, (dict, list)):
            result.extend(_pickup_semantic_texts(item, depth + 1))
        elif item is not None:
            result.append(str(item))
    return result


class _PickupHtmlParser(HTMLParser):
    _field_classes = {"body", "date", "meta", "subject"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._ignored_tag: str | None = None
        self._article_depth = 0
        self._current: dict[str, list[str]] | None = None
        self._captures: list[tuple[str, str]] = []
        self._articles: list[dict[str, str]] = []
        self._page_text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        normalized_tag = tag.casefold()
        if self._ignored_tag:
            return
        if normalized_tag in {"script", "style"}:
            self._ignored_tag = normalized_tag
            return
        if normalized_tag == "article":
            self._article_depth += 1
            if self._current is None:
                self._current = {"all": [], "body": [], "date": [], "meta": [], "subject": []}

        if self._current is not None:
            classes = {
                item.casefold()
                for name, value in attrs
                if name.casefold() == "class" and value
                for item in value.split()
            }
            field = next((name for name in self._field_classes if name in classes), None)
            if field:
                self._captures.append((normalized_tag, field))
            if normalized_tag == "br":
                self._append_text("\n")

    def handle_endtag(self, tag: str) -> None:
        normalized_tag = tag.casefold()
        if self._ignored_tag:
            if normalized_tag == self._ignored_tag:
                self._ignored_tag = None
            return
        for index in range(len(self._captures) - 1, -1, -1):
            if self._captures[index][0] == normalized_tag:
                del self._captures[index]
                break
        if normalized_tag == "article" and self._article_depth:
            self._article_depth -= 1
            if self._article_depth == 0 and self._current is not None:
                self._articles.append({key: _compact_pickup_text(parts) for key, parts in self._current.items()})
                self._current = None
                self._captures.clear()

    def handle_data(self, data: str) -> None:
        if self._ignored_tag or not data:
            return
        self._page_text.append(data)
        self._append_text(data)

    def _append_text(self, value: str) -> None:
        if self._current is None:
            return
        self._current["all"].append(value)
        if self._captures:
            self._current[self._captures[-1][1]].append(value)

    def as_messages(self) -> list[MailMessage]:
        if not self._articles:
            fallback = _compact_pickup_text(self._page_text)
            if not fallback:
                return []
            self._articles.append({"all": fallback, "body": fallback, "date": "", "meta": "", "subject": ""})

        messages: list[MailMessage] = []
        for article in self._articles:
            all_text = article.get("all", "")
            body = article.get("body", "") or all_text
            subject = article.get("subject", "") or None
            date_text = article.get("date", "")
            date_match = re.search(
                r"\b\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}(?::\d{2})?(?:Z|[+-]\d{2}:?\d{2})?\b",
                date_text,
            )
            received_at = _parse_pickup_time(date_match.group(0) if date_match else date_text or None)
            sender_text = re.sub(
                r"^(?:from|\u53d1\u4ef6\u4eba)\s*[:\uff1a]\s*",
                "",
                article.get("meta", ""),
                flags=re.I,
            )
            sender_name, sender_address = parseaddr(sender_text)
            messages.append(
                MailMessage(
                    id=f"pickup-{hashlib.sha256(all_text.encode('utf-8')).hexdigest()[:16]}",
                    folder="inbox",
                    subject=subject,
                    sender_name=sender_name or None,
                    sender_address=sender_address or None,
                    body_preview=_truncate(body),
                    received_at=received_at,
                    text=body,
                    code=extract_code(body, subject, all_text),
                    recipients=tuple(sorted(_extract_email_candidates(all_text))),
                )
            )
        return messages


def _compact_pickup_text(parts: list[str]) -> str:
    return re.sub(r"\s+", " ", "".join(parts)).strip()


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
        recipients=_message_recipient_candidates(message),
    )


def _external_message_to_mail_message(message: Any, folder: str, index: int) -> MailMessage:
    if not isinstance(message, dict):
        text = str(message)
        return MailMessage(
            id=f"external-mail-{index}",
            folder=folder,
            subject=None,
            sender_name=None,
            sender_address=None,
            body_preview=_truncate(text),
            received_at=None,
            text=text,
            code=extract_code(text),
        )

    raw = message.get("raw") if isinstance(message.get("raw"), dict) else {}
    sender = message.get("from") or message.get("from_info") or message.get("sender") or raw.get("from") or raw.get("sender")
    sender_name, sender_address = _external_sender_parts(sender)
    text_content = _pick_mail_value(
        message,
        "text",
        "text_content",
        "plain",
        "plain_text",
        "body",
        "body_text",
    )
    html_content = _pick_mail_value(message, "html", "html_content", "htmlBody", "body_html")
    text_source = text_content or (_strip_html(html_content) if html_content else None)
    snippet = _pick_mail_value(message, "snippet", "summary", "preview") or _truncate(text_source)
    subject = _pick_mail_value(message, "subject", "title") or "(No subject)"
    code = _pick_mail_value(message, "code", "verification_code", "verificationCode", "otp", "captcha") or extract_code(
        subject,
        snippet,
        text_source,
        html_content,
    )

    return MailMessage(
        id=_pick_mail_value(message, "id", "message_id", "uid", "mail_id", "messageId") or f"external-mail-{index}",
        folder=folder,
        subject=subject,
        sender_name=sender_name,
        sender_address=sender_address,
        body_preview=snippet,
        received_at=_parse_mail_time(
            _pick_mail_value(
                message,
                "date",
                "received_at",
                "receivedDateTime",
                "sent_at",
                "created_at",
                "internalDate",
            )
        ),
        text=text_source,
        code=code,
        recipients=_message_recipient_candidates(message, raw),
        sender_authenticated=_openai_sender_authenticated(
            sender_address,
            _external_authentication_results(message, raw),
        ),
    )


def _external_authentication_results(*sources: dict[str, Any]) -> list[str]:
    results: list[str] = []
    for source in sources:
        for key in ("authentication_results", "authenticationResults"):
            value = source.get(key)
            if isinstance(value, str) and value.strip():
                results.append(value.strip())
            elif isinstance(value, list):
                results.extend(str(item).strip() for item in value if str(item).strip())
        headers = source.get("headers") or source.get("internetMessageHeaders")
        if isinstance(headers, list):
            for header in headers:
                if not isinstance(header, dict):
                    continue
                name = str(header.get("name") or header.get("Name") or "").strip().casefold()
                if name != "authentication-results":
                    continue
                value = str(header.get("value") or header.get("Value") or "").strip()
                if value:
                    results.append(value)
    return results


def _external_sender_parts(sender: Any) -> tuple[str | None, str | None]:
    if isinstance(sender, dict):
        name = _pick_mail_value(sender, "name", "display_name")
        address = _pick_mail_value(sender, "address", "email", "mail")
        return name, address
    text = str(sender or "").strip()
    if not text:
        return None, None
    name, address = parseaddr(text)
    return name or None, address or text


def _unwrap_external_mail_items(payload: Any) -> tuple[list[dict[str, Any]], str | None]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)], None
    if not isinstance(payload, dict):
        raise ValueError("payload is not a JSON object or list")

    next_refresh_token = _pick_mail_value(payload, "new_refresh_token")
    data = payload.get("data", payload)
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)], next_refresh_token
    if isinstance(data, dict):
        if data.get("error"):
            raise ValueError(str(data.get("error")))
        if any(key in data for key in {"subject", "text", "html", "send", "date"}):
            return [data], next_refresh_token or _pick_mail_value(data, "new_refresh_token")
        nested = data.get("items") or data.get("messages") or data.get("mails")
        if isinstance(nested, list):
            return [item for item in nested if isinstance(item, dict)], next_refresh_token or _pick_mail_value(data, "new_refresh_token")
        if isinstance(nested, dict):
            return [nested], next_refresh_token or _pick_mail_value(data, "new_refresh_token")
    if payload.get("success") is False:
        detail = _external_mail_error(payload)
        raise ValueError(detail or "provider returned success=false")
    raise ValueError("missing message list")


def _external_mail_error(payload: Any) -> str | None:
    if isinstance(payload, dict):
        for key in ("error", "detail", "message"):
            value = _pick_mail_value(payload, key)
            if value:
                return value
        data = payload.get("data")
        if isinstance(data, dict):
            for key in ("error", "detail", "message"):
                value = _pick_mail_value(data, key)
                if value:
                    return value
    if isinstance(payload, str):
        return payload[:300]
    return None


def _pick_mail_value(mail: Any, *keys: str) -> str | None:
    if not isinstance(mail, dict):
        return None
    for key in keys:
        value = mail.get(key)
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return None


def _imap_message_to_mail_message(raw: bytes, fallback_id: str, folder: str) -> MailMessage:
    message = message_from_bytes(raw)
    text_content, html_content = _message_texts(message)
    body_text = text_content or (_strip_html(html_content) if html_content else None)
    sender_name, sender_address = parseaddr(_decode_header_value(message.get("From")))
    subject = _decode_header_value(message.get("Subject"))
    return MailMessage(
        id=message.get("Message-ID") or fallback_id,
        folder="junk" if folder == "junk" else "inbox",
        subject=subject,
        sender_name=sender_name or None,
        sender_address=sender_address or None,
        body_preview=_truncate(body_text),
        received_at=_parse_email_time(message.get("Date")),
        text=body_text,
        code=extract_code(subject, body_text, html_content),
        recipients=tuple(sorted(_extract_original_recipients_from_message(message))),
        sender_authenticated=_openai_sender_authenticated(
            sender_address,
            [
                decoded
                for value in message.get_all("Authentication-Results", [])
                if (decoded := (_decode_header_value(value) or value).strip())
            ],
        ),
    )


def _imap_chunks_to_raw_message(chunks: list[bytes]) -> bytes:
    header = chunks[0] if chunks else b""
    body = b"\r\n".join(chunks[1:])
    return header.rstrip() + b"\r\n\r\n" + body if body else header


def _imap_chunks_match_recipient(chunks: list[bytes], recipient_email: str) -> bool:
    if not recipient_email:
        return False
    message = message_from_bytes(_imap_chunks_to_raw_message(chunks))
    return recipient_email in _extract_original_recipients_from_message(message)


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
    return _imap_message_to_mail_message(_imap_chunks_to_raw_message(chunks), fallback_id, folder)


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


def _message_texts(message: Any) -> tuple[str | None, str | None]:
    parts = message.walk() if message.is_multipart() else [message]
    plain_parts: list[str] = []
    html_parts: list[str] = []
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
            stripped = text.strip()
            if stripped:
                plain_parts.append(stripped)
        elif part.get_content_type() == "text/html":
            stripped = text.strip()
            if stripped:
                html_parts.append(stripped)
    plain_text = "\n\n".join(plain_parts) or None
    html_text = "\n\n".join(html_parts) or None
    return plain_text, html_text


def _message_preview(message: Any) -> str | None:
    plain_text, html_text = _message_texts(message)
    if plain_text:
        return _truncate(plain_text)
    if html_text:
        return _truncate(_strip_html(html_text))
    return None


def _extract_email_candidates(value: Any) -> set[str]:
    candidates: set[str] = set()
    if value is None:
        return candidates
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="ignore")
    if isinstance(value, str):
        for match in EMAIL_RE.finditer(value):
            candidates.add(match.group(0).lower())
        return candidates
    if isinstance(value, dict):
        for child in value.values():
            candidates.update(_extract_email_candidates(child))
        return candidates
    if isinstance(value, (list, tuple, set)):
        for item in value:
            candidates.update(_extract_email_candidates(item))
    return candidates


def _extract_original_recipients_from_message(message: Any) -> set[str]:
    candidates: set[str] = set()
    for header_name in _RECIPIENT_HEADERS:
        for value in message.get_all(header_name, []):
            candidates.update(_extract_email_candidates(_decode_header_value(value) or value))
    return candidates


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


def _parse_mail_time(value: str | None) -> datetime | None:
    if not value:
        return None
    graph_time = _parse_graph_time(value)
    if graph_time is not None:
        return graph_time
    email_time = _parse_email_time(value)
    if email_time is not None:
        return email_time
    try:
        timestamp = float(value)
    except (TypeError, ValueError):
        return None
    if timestamp > 10_000_000_000:
        timestamp = timestamp / 1000
    try:
        return datetime.fromtimestamp(timestamp, tz=timezone.utc)
    except (OSError, ValueError):
        return None


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
