import asyncio
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select

from app.core.config import Settings, get_settings
from app.core.crypto import decrypt_text, encrypt_text
from app.core.database import AsyncSessionLocal
from app.models import AccountSnapshot, AppEvent, MailboxCredential, RefreshJob, utcnow
from app.services.browser import BrowserRefreshOutcome, ChatGptBrowserRefresher
from app.services.chatgpt_account import ChatGptAccessTokenInvalid, ChatGptAccountStatusChecker
from app.services.chatgpt_protocol import ChatGptProtocolRefresher, ProtocolRefreshOutcome
from app.services.account_exceptions import clear_account_exception
from app.services.events import record_event
from app.services.mail import MailAdapterRegistry
from app.services.memory import ProcessMemorySampler, available_system_memory_bytes
from app.services.openai_oauth import (
    OAUTH_PROTOCOL_EDGE_BLOCK_REASON_CODE,
    OpenAiOAuthOutcome,
    OpenAiOAuthRefresher,
    PhoneVerificationContext,
    is_protocol_edge_verification_blocked,
)
from app.services.openai_token_service import OpenAiAccessTokenInvalid, OpenAiTokenService
from app.services.phone_numbers import (
    BoundPhone,
    describe_phone_source,
    fetch_sms_code,
    get_bound_phone_by_email,
    has_working_sms_url,
    is_manual_phone_source,
    OAuthPhoneResolutionError,
    record_phone_sms_fetch_status,
    require_oauth_phone_match,
)
from app.services.runtime_config import get_runtime_config_service
from app.services.notifications import (
    enqueue_oauth_account_disabled,
    enqueue_oauth_account_enabled,
)
from app.services.sub2api import Sub2ApiClient, looks_deactive_text, sanitize_payload
from app.services.usage_estimate import materialize_usage_reset_times, record_usage_limit_samples


SENSITIVE_ERROR_RE = re.compile(
    r"(?i)((?:access|refresh|id)_token|rt|api_key|password|client_secret|authorization)([\"'\s:=]+)[^\s,}\"']+"
)
BEARER_RE = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+")
RECOVERY_DISABLED_REASON = "Automatic credential refresh is disabled in settings; refresh was not started."
OAUTH_PHONE_VERIFICATION_STOPPED_REASON = (
    "已尝试重新 OAuth，但遇到手机验证码，因设置中“遇到手机验证码时停止”已开启而终止。"
)
logger = logging.getLogger(__name__)
_UNSET = object()


def _phone_hint_from_oauth_outcome(outcome: OpenAiOAuthOutcome) -> str | None:
    if outcome.phone_number_hint:
        return outcome.phone_number_hint
    text = str(outcome.error or "")
    for pattern in (
        r"OpenAI 前端手机号\s*([^，。\s]+)",
        r"手机号\s*([^，。\s]+)\s*的接码链接",
        r"已绑定手机号\s*([^，。\s]+)",
    ):
        match = re.search(pattern, text)
        if not match:
            continue
        hint = match.group(1).strip()
        if re.sub(r"\D", "", hint):
            return hint
    return None


def _redact_error_text(value: str) -> str:
    text = BEARER_RE.sub("Bearer ***redacted***", value)
    text = SENSITIVE_ERROR_RE.sub(r"\1\2***redacted***", text)
    return text[:500]


def _oauth_outcome_requires_phone(outcome: OpenAiOAuthOutcome) -> bool:
    if outcome.status == "add_phone":
        return True
    text = str(outcome.error or "").casefold()
    return any(
        marker in text
        for marker in (
            "/add-phone",
            "phone verification",
            "verify your phone",
            "phone number required",
            "手机号验证",
            "手机验证码",
        )
    )


class RefreshService:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.sub2api = Sub2ApiClient(self.settings)
        self.mail = MailAdapterRegistry()
        self.browser = ChatGptBrowserRefresher(self.settings)
        self.protocol = ChatGptProtocolRefresher(self.settings)
        self.openai_oauth = OpenAiOAuthRefresher(self.settings)
        self.openai_tokens = OpenAiTokenService(self.settings)
        self.account_status = ChatGptAccountStatusChecker(self.settings)
        self.runtime_config = get_runtime_config_service()
        self._running: set[str] = set()
        self._manual_jobs: set[int] = set()
        self._active_protocol_refreshes = 0
        self._active_browser_refreshes = 0
        self._protocol_concurrency_condition = asyncio.Condition()
        self._browser_concurrency_condition = asyncio.Condition()

    async def enqueue(
        self,
        account: dict,
        reason: str,
        allow_deactive: bool = False,
        *,
        manual: bool = False,
    ) -> int | None:
        if not manual and not await self.runtime_config.get_recovery_enabled():
            return None
        email = self.sub2api.account_email(account)
        if not email:
            return None
        normalized = email.lower()
        if normalized in self._running:
            return None

        async with AsyncSessionLocal() as db:
            active_job = await db.scalar(
                select(RefreshJob).where(RefreshJob.email == normalized, RefreshJob.status.in_(["queued", "running"]))
            )
            if active_job:
                return None
            existing = await db.scalar(select(AccountSnapshot).where(AccountSnapshot.email == normalized))
            if existing and existing.deactive and not allow_deactive:
                return None
            job = RefreshJob(
                email=normalized,
                sub2api_account_id=self.sub2api.account_id(account),
                status="queued",
                reason=reason,
            )
            db.add(job)
            await db.commit()
            await db.refresh(job)
            job_id = job.id

        if manual:
            self._manual_jobs.add(job_id)
        asyncio.create_task(self._run(job_id, account))
        return job_id

    def can_try_protocol_refresh(self, account: dict) -> bool:
        if self.sub2api.account_has_refresh_token(account):
            return True
        email = self.sub2api.account_email(account)
        if not email:
            return False
        return self._has_local_openai_token_cache(email)

    async def enqueue_by_email(self, email: str, reason: str = "manual") -> int | None:
        normalized = email.lower()
        accounts, _ = self.sub2api.dedupe_accounts_by_email(
            [account for account in await self.sub2api.list_accounts() if self.sub2api.is_gpt_account(account)]
        )
        for account in accounts:
            if self.sub2api.account_email(account) == normalized:
                return await self.enqueue(
                    account,
                    reason,
                    allow_deactive=True,
                    manual=True,
                )
        return None

    async def _run(self, job_id: int, account: dict) -> None:
        email = (self.sub2api.account_email(account) or "").lower()
        if not email or email in self._running:
            return

        self._running.add(email)
        try:
            sampler = ProcessMemorySampler(interval_seconds=1.0)
            try:
                async with sampler:
                    try:
                        await self._run_inner(job_id, email, account)
                    except Exception as exc:
                        await self._safe_finish(
                            job_id,
                            email,
                            "failed",
                            f"Refresh task crashed before it could finish cleanly: {_redact_error_text(str(exc))}",
                        )
            finally:
                if sampler.peak_rss_bytes:
                    await self._safe_record_memory_peak(job_id, email, sampler.peak_rss_bytes)
        finally:
            self._running.discard(email)
            self._manual_jobs.discard(job_id)

    async def _acquire_protocol_slot(self) -> None:
        async with self._protocol_concurrency_condition:
            while True:
                limit = await self.runtime_config.get_protocol_refresh_max_concurrency()
                if limit == 0 or self._active_protocol_refreshes < limit:
                    self._active_protocol_refreshes += 1
                    return
                await self._protocol_concurrency_condition.wait()

    async def _release_protocol_slot(self) -> None:
        async with self._protocol_concurrency_condition:
            self._active_protocol_refreshes = max(0, self._active_protocol_refreshes - 1)
            self._protocol_concurrency_condition.notify_all()

    async def _acquire_browser_slot(self) -> None:
        async with self._browser_concurrency_condition:
            while True:
                limit = await self.runtime_config.get_browser_refresh_max_concurrency()
                if limit == 0 or self._active_browser_refreshes < limit:
                    self._active_browser_refreshes += 1
                    return
                await self._browser_concurrency_condition.wait()

    async def _release_browser_slot(self) -> None:
        async with self._browser_concurrency_condition:
            self._active_browser_refreshes = max(0, self._active_browser_refreshes - 1)
            self._browser_concurrency_condition.notify_all()

    async def wake_concurrency(self) -> None:
        async with self._protocol_concurrency_condition:
            self._protocol_concurrency_condition.notify_all()
        async with self._browser_concurrency_condition:
            self._browser_concurrency_condition.notify_all()

    async def _run_inner(self, job_id: int, email: str, account: dict) -> None:
        if await self._finish_if_recovery_disabled(job_id, email):
            return

        token_state = await self._load_local_openai_tokens(email)
        has_refresh_token = bool(str(token_state.get("refresh_token") or "").strip())
        if has_refresh_token and await self._try_protocol_slot_refresh(job_id, email, account):
            return

        credential = await self._load_credential(email)
        if credential is None:
            protocol_failure = await self._latest_protocol_failure_summary(job_id, email)
            reason = "No enabled mailbox credential exists for this GPT account; OAuth re-login was not started."
            if protocol_failure:
                reason = f"{protocol_failure}；且无绑定邮箱，未继续 OAuth 重新登录。"
            await self._finish(
                job_id,
                email,
                "failed",
                reason,
            )
            return
        if credential.disabled:
            protocol_failure = await self._latest_protocol_failure_summary(job_id, email)
            reason = "Mailbox credential is disabled; OAuth re-login was not started."
            if protocol_failure:
                reason = f"{protocol_failure}；邮箱凭据已禁用，未继续 OAuth 重新登录。"
            await self._finish(
                job_id,
                email,
                "failed",
                reason,
            )
            return

        await self._mark_started(job_id, email)

        async def fetch_code(after: datetime) -> str | None:
            deadline = asyncio.get_running_loop().time() + self.settings.verification_code_timeout_seconds
            strict_lookup_after = after - timedelta(seconds=15)
            fallback_lookup_after = after - timedelta(seconds=self.settings.verification_code_lookup_grace_seconds)
            fallback_started = False
            last_error = "No verification code found yet."
            while asyncio.get_running_loop().time() < deadline:
                remaining = deadline - asyncio.get_running_loop().time()
                use_fallback_window = fallback_started or remaining <= max(
                    self.settings.verification_code_poll_seconds * 3,
                    18,
                )
                lookup_after = fallback_lookup_after if use_fallback_window else strict_lookup_after
                result = await self.mail.fetch_code(credential, lookup_after)
                if result.status == "ok" and result.code:
                    await self._mail_success(credential.id, result.new_refresh_token, result.new_access_token)
                    return result.code
                if result.status == "failed":
                    last_error = result.error or "Mailbox fetch failed."
                    await self._mail_error(credential.id, last_error)
                    return None
                last_error = result.error or last_error
                fallback_started = fallback_started or use_fallback_window
                await asyncio.sleep(self.settings.verification_code_poll_seconds)
            await self._mail_error(credential.id, last_error)
            return None

        oauth_login_mode = await self.runtime_config.get_oauth_login_mode()
        stop_on_phone_verification = await self.runtime_config.get_oauth_stop_on_phone_verification()
        oauth_source = oauth_login_mode
        if oauth_login_mode == "browser":
            oauth_outcome = await self._oauth_with_browser_slot(
                job_id,
                email,
                fetch_code,
                stop_on_phone_verification=stop_on_phone_verification,
            )
        else:
            oauth_outcome = await self._oauth_with_protocol_slot(job_id, email, fetch_code)
            if (
                oauth_outcome is not None
                and oauth_outcome.status == "failed"
                and is_protocol_edge_verification_blocked(oauth_outcome.error)
            ):
                await self._record_openai_oauth_warning(
                    job_id,
                    email,
                    oauth_outcome.error or OAUTH_PROTOCOL_EDGE_BLOCK_REASON_CODE,
                    will_fallback=True,
                    fallback_target="headless browser",
                )
                oauth_source = "browser"
                oauth_outcome = await self._oauth_with_browser_slot(
                    job_id,
                    email,
                    fetch_code,
                    stop_on_phone_verification=stop_on_phone_verification,
                )
        if oauth_outcome is None:
            return
        if stop_on_phone_verification and _oauth_outcome_requires_phone(oauth_outcome):
            await self._finish_oauth_phone_verification_stopped(
                job_id,
                email,
                oauth_source,
                oauth_outcome.error,
            )
            return
        if oauth_outcome.status in {"ok", "deactive", "add_phone"}:
            await self._handle_oauth_outcome(job_id, email, account, oauth_outcome, source=oauth_source)
            return
        preferred_failure_reason = self._preferred_openai_failure_reason(oauth_outcome.error)
        await self._record_openai_oauth_warning(
            job_id,
            email,
            oauth_outcome.error or f"OpenAI OAuth {oauth_login_mode} refresh_token acquisition failed.",
            will_fallback=False,
        )
        await self._finish(
            job_id,
            email,
            "failed",
            preferred_failure_reason
            or oauth_outcome.error
            or f"OpenAI OAuth {oauth_login_mode} refresh_token acquisition failed.",
        )

    async def _try_protocol_slot_refresh(self, job_id: int, email: str, account: dict) -> bool:
        await self._acquire_protocol_slot()
        try:
            if await self._finish_if_recovery_disabled(job_id, email):
                return True
            await self._mark_started(job_id, email)
            return await self._try_local_openai_refresh_token(job_id, email, account)
        finally:
            await self._release_protocol_slot()

    async def _finish_if_recovery_disabled(self, job_id: int, email: str) -> bool:
        if job_id in self._manual_jobs or await self.runtime_config.get_recovery_enabled():
            return False
        await self._finish(job_id, email, "skipped", RECOVERY_DISABLED_REASON)
        return True

    async def _refresh_with_protocol_slot(self, job_id: int, email: str, fetch_code) -> ProtocolRefreshOutcome | None:
        await self._acquire_protocol_slot()
        try:
            if await self._finish_if_recovery_disabled(job_id, email):
                return None
            return await self.protocol.refresh_access_token(email, fetch_code)
        finally:
            await self._release_protocol_slot()

    async def _refresh_with_browser_slot(
        self,
        job_id: int,
        email: str,
        fetch_code,
    ) -> BrowserRefreshOutcome | None:
        await self._acquire_browser_slot()
        try:
            if await self._finish_if_recovery_disabled(job_id, email):
                return None
            memory_error = await self._browser_memory_guard_error()
            if memory_error:
                await self._finish(job_id, email, "failed", memory_error)
                return None
            return await self.browser.refresh_access_token(email, fetch_code)
        finally:
            await self._release_browser_slot()

    async def _oauth_with_protocol_slot(self, job_id: int, email: str, fetch_code) -> OpenAiOAuthOutcome | None:
        await self._acquire_protocol_slot()
        try:
            if await self._finish_if_recovery_disabled(job_id, email):
                return None
            return await self.openai_oauth.refresh_with_protocol(email, fetch_code)
        finally:
            await self._release_protocol_slot()

    async def _oauth_with_browser_slot(
        self,
        job_id: int,
        email: str,
        fetch_code,
        *,
        stop_on_phone_verification: bool = False,
    ) -> OpenAiOAuthOutcome | None:
        active_phone = None if stop_on_phone_verification else await self._load_bound_phone(email)
        baseline_markers: dict[tuple[str, str], str] = {}

        async def load_baseline(phone: BoundPhone | None) -> None:
            if phone is None or not has_working_sms_url(phone.sms_url):
                return
            key = (phone.phone_number, phone.sms_url)
            if key in baseline_markers:
                return
            async with AsyncSessionLocal() as db:
                baseline_result = await fetch_sms_code(phone, self.settings)
                await record_phone_sms_fetch_status(db, phone.phone_id, baseline_result)
                await db.commit()
            marker = baseline_result.snapshot_key or baseline_result.code
            if marker:
                baseline_markers[key] = marker

        async def resolve_phone(page_phone_hint: str | None, force_rotate: bool = False) -> BoundPhone | None:
            nonlocal active_phone
            if force_rotate and active_phone is not None:
                raise RuntimeError(f"OpenAI 前端手机号 {active_phone.phone_number} 已达到可绑定账号上限，已停止自动 OAuth。")
            async with AsyncSessionLocal() as db:
                try:
                    if page_phone_hint:
                        resolved = await require_oauth_phone_match(db, email, page_phone_hint)
                    elif active_phone is not None:
                        resolved = active_phone
                    else:
                        raise OAuthPhoneResolutionError("OpenAI 前端未显示可识别手机号，且当前账号未绑定手机号库记录，已停止自动 OAuth。")
                except OAuthPhoneResolutionError as exc:
                    raise RuntimeError(str(exc)) from exc
                await db.commit()
            if resolved is not None:
                if resolved.sms_cdk or is_manual_phone_source(resolved.sms_url):
                    raise RuntimeError(
                        f"OpenAI 前端手机号 {resolved.phone_number} 对应的是 CDK/手动接码源 {resolved.sms_cdk or describe_phone_source(resolved.sms_url)}，已停止自动 OAuth，请手动完成验证。"
                    )
                if not has_working_sms_url(resolved.sms_url):
                    raise RuntimeError(
                        f"OpenAI 前端手机号 {resolved.phone_number} 在库中存在，但接码链接不存在或不可用，已停止自动 OAuth。"
                    )
                active_phone = resolved
                await load_baseline(active_phone)
            return active_phone

        await load_baseline(active_phone)

        async def fetch_phone_code(requested_at: datetime) -> str | None:
            nonlocal active_phone
            if active_phone is None:
                return None
            requested_at_utc = requested_at.astimezone(timezone.utc)
            deadline = asyncio.get_running_loop().time() + self.settings.verification_code_timeout_seconds
            last_error: str | None = None
            repeated_code_observations = 0
            while asyncio.get_running_loop().time() < deadline:
                async with AsyncSessionLocal() as db:
                    result = await fetch_sms_code(active_phone, self.settings)
                    await record_phone_sms_fetch_status(db, active_phone.phone_id, result)
                    await db.commit()
                if result.status == "ok" and result.code:
                    key = (active_phone.phone_number, active_phone.sms_url)
                    current_marker = result.snapshot_key or result.code
                    previous_marker = baseline_markers.get(key)
                    sms_timestamp = _parse_sms_timestamp(result.timestamp_text)
                    is_fresh_by_time = sms_timestamp is not None and sms_timestamp >= requested_at_utc
                    if current_marker and current_marker != previous_marker:
                        baseline_markers[key] = current_marker
                        return result.code
                    if previous_marker is None:
                        baseline_markers[key] = current_marker or result.code
                        return result.code
                    if is_fresh_by_time:
                        baseline_markers[key] = current_marker or result.code
                        return result.code
                    if not result.has_timestamp and result.code and current_marker == previous_marker:
                        repeated_code_observations += 1
                        if repeated_code_observations >= 2:
                            return result.code
                if result.status == "failed":
                    last_error = result.error or "SMS code fetch failed."
                    break
                if result.error:
                    last_error = result.error
                await asyncio.sleep(self.settings.verification_code_poll_seconds)
            if last_error and active_phone is not None:
                raise RuntimeError(
                    f"当前账号触发 OpenAI 手机号验证；手机号 {active_phone.phone_number} 的接码链接已过期或不可用：{last_error}。"
                )
            return None

        await self._acquire_browser_slot()
        try:
            if await self._finish_if_recovery_disabled(job_id, email):
                return None
            memory_error = await self._browser_memory_guard_error()
            if memory_error:
                return OpenAiOAuthOutcome(status="failed", error=memory_error)
            phone_context = (
                PhoneVerificationContext(phone_number=active_phone.phone_number, sms_url=active_phone.sms_url)
                if active_phone is not None
                else None
            )
            return await self.openai_oauth.refresh_with_browser(
                email,
                fetch_code,
                phone_context,
                fetch_phone_code,
                resolve_phone,
                stop_on_phone_verification=stop_on_phone_verification,
            )
        finally:
            await self._release_browser_slot()

    async def _browser_memory_guard_error(self) -> str | None:
        threshold_mb = await self.runtime_config.get_browser_min_available_memory_mb()
        if threshold_mb <= 0:
            return None
        available_bytes = available_system_memory_bytes()
        if available_bytes is None:
            return None
        threshold_bytes = threshold_mb * 1024 * 1024
        if available_bytes >= threshold_bytes:
            return None
        return (
            "Skipped browser login because available system memory is "
            f"{_format_bytes(available_bytes)}, below the configured browser threshold "
            f"{_format_bytes(threshold_bytes)}."
        )

    async def _try_access_token_status_refresh(self, job_id: int, email: str, account: dict) -> bool:
        if await self._try_sub2api_protocol_refresh(job_id, email, account):
            return True

        if await self._try_local_openai_refresh_token(job_id, email, account):
            return True

        if await self._try_sub2api_account_status_refresh(job_id, email, account):
            return True

        access_token = self.sub2api.account_access_token(account)
        if access_token and await self._try_direct_access_token_status_refresh(
            job_id,
            email,
            account,
            access_token=access_token,
            success_prefix="Access token status check succeeded",
            will_fallback=True,
            persist_local_access_token=True,
        ):
            return True

        if await self._try_local_openai_access_token_status_refresh(
            job_id,
            email,
            account,
            exclude_access_token=access_token,
        ):
            return True
        return False

    async def _try_direct_access_token_status_refresh(
        self,
        job_id: int,
        email: str,
        account: dict,
        *,
        access_token: str,
        success_prefix: str,
        will_fallback: bool,
        persist_local_access_token: bool,
    ) -> bool:
        try:
            status = await self.account_status.check(access_token)
        except ChatGptAccessTokenInvalid as exc:
            await self._record_access_token_status_warning(job_id, email, str(exc), will_fallback=will_fallback)
            return False
        except Exception as exc:
            await self._record_access_token_status_warning(job_id, email, str(exc), will_fallback=will_fallback)
            return False

        if status.deactive:
            reason = "ChatGPT account check reported account_deactivated."
            await self._mark_deactive(email, reason)
            await self._finish(job_id, email, "deactive", reason)
            return True

        if persist_local_access_token:
            await self._store_local_openai_tokens(email, access_token=access_token)

        plan_text = f"plan={status.plan_type}" if status.plan_type else "plan not returned"
        await self._finalize_session_refresh(
            job_id,
            email,
            account,
            session=status.session,
            access_token=access_token,
            success_prefix=f"{success_prefix}; {plan_text}",
            skip_account_check_merge=True,
        )
        return True

    async def _try_sub2api_account_status_refresh(self, job_id: int, email: str, account: dict) -> bool:
        try:
            status = await self.sub2api.check_openai_account_status(account)
        except Exception as exc:
            await self._record_access_token_status_warning(job_id, email, str(exc), will_fallback=True)
            return False

        if not status:
            await self._record_sub2api_check_status_unavailable(job_id, email)
            return False

        message = str(status.get("message") or "sub2api access-token account status check failed")
        if status.get("deactive"):
            await self._record_access_token_status_warning(
                job_id,
                email,
                "sub2api access-token account status check reported account_deactivated; falling back to direct ChatGPT/browser verification.",
                will_fallback=True,
            )
            return False

        if not status.get("token_valid"):
            await self._record_access_token_status_warning(job_id, email, message, will_fallback=True)
            return False

        usage_status = await self._refresh_sub2api_usage(job_id, email, account)

        changed = status.get("changed_credentials")
        changed_keys = [str(item) for item in changed] if isinstance(changed, list) else []
        changed_text = ", ".join(changed_keys) if changed_keys else "no credential fields changed"
        plan_type = str(status.get("plan_type") or "").strip()
        plan_text = f"plan={plan_type}" if plan_type else "plan not returned"
        subscription_keys: list[str] = []
        try:
            subscription_keys = await self.sub2api.reassert_subscription_state_from_session(account, status, "")
        except Exception as exc:
            await self._finish(job_id, email, "failed", f"sub2api subscription state sync failed: {_redact_error_text(str(exc))}")
            return True

        access_token = self.sub2api.account_access_token(account)
        if access_token:
            direct_subscription_keys, finished = await self._reassert_subscription_from_access_token(
                job_id,
                email,
                account,
                access_token,
            )
            if finished:
                return True
            subscription_keys = sorted(set(subscription_keys) | set(direct_subscription_keys))
        elif self._account_has_access_token_hint(account):
            await self._record_missing_runtime_access_token(job_id, email, source="sub2api_check_status")

        subscription_text = (
            f"reasserted session subscription fields: {', '.join(subscription_keys)}"
            if subscription_keys
            else "no session subscription fields found"
        )
        remote_account, remote_issue = await self._verify_remote_recovery(account)
        if remote_issue is not None:
            await self._finish(job_id, email, "failed", remote_issue, remote_account=remote_account)
            return True
        await self._finish(
            job_id,
            email,
            "succeeded",
            f"sub2api access-token account status check succeeded; {plan_text}; updated sub2api credentials: {changed_text}; {usage_status}; {subscription_text}.",
            remote_account=remote_account,
        )
        return True

    async def _reassert_subscription_from_access_token(
        self,
        job_id: int,
        email: str,
        account: dict,
        access_token: str,
    ) -> tuple[list[str], bool]:
        try:
            status = await self.account_status.check(access_token)
        except ChatGptAccessTokenInvalid as exc:
            await self._record_access_token_status_warning(job_id, email, str(exc), will_fallback=False)
            return [], False
        except Exception as exc:
            await self._record_access_token_status_warning(job_id, email, str(exc), will_fallback=False)
            return [], False

        if status.deactive:
            reason = "ChatGPT accounts/check reported account_deactivated."
            await self._mark_deactive(email, reason)
            await self._finish(job_id, email, "deactive", reason)
            return [], True

        try:
            subscription_keys = await self.sub2api.reassert_subscription_state_from_session(
                account,
                status.session,
                access_token,
            )
        except Exception as exc:
            await self._finish(job_id, email, "failed", f"sub2api subscription state sync failed: {_redact_error_text(str(exc))}")
            return [], True
        return subscription_keys, False

    async def _try_local_openai_refresh_token(self, job_id: int, email: str, account: dict) -> bool:
        token_state = await self._load_local_openai_tokens(email)
        refresh_token = str(token_state.get("refresh_token") or "").strip()
        if not refresh_token:
            return False

        client_id = str(token_state.get("client_id") or self.settings.openai_oauth_client_id or "").strip() or None
        try:
            token_data = await self.openai_tokens.refresh_token(refresh_token=refresh_token, client_id=client_id)
            access_token = str(token_data.get("access_token") or "").strip()
            profile = await self.openai_tokens.fetch_profile(access_token)
        except Exception as exc:
            await self._store_local_openai_tokens(email, refresh_token=None)
            await self._record_local_openai_refresh_token_warning(job_id, email, str(exc), invalid=True)
            return False

        if profile.deactive:
            reason = "OpenAI refresh_token profile check reported account_deactivated."
            await self._mark_deactive(email, reason)
            await self._finish(job_id, email, "deactive", reason)
            return True

        expires_at = _openai_token_expires_at(token_data)
        merged_session = _merge_openai_session_tokens(
            profile.session,
            access_token=access_token,
            refresh_token=str(token_data.get("refresh_token") or "").strip() or None,
            id_token=str(token_data.get("id_token") or "").strip() or None,
            client_id=client_id,
            expires_at=expires_at,
        )
        await self._store_local_openai_tokens(
            email,
            access_token=access_token,
            refresh_token=str(token_data.get("refresh_token") or "").strip() or None,
            id_token=str(token_data.get("id_token") or "").strip() or None,
            client_id=client_id,
            expires_at=expires_at,
        )
        await self._finalize_session_refresh(
            job_id,
            email,
            account,
            session=merged_session,
            access_token=access_token,
            success_prefix="OpenAI refresh_token refresh succeeded via local cached token; no browser login was needed",
            skip_account_check_merge=True,
        )
        return True

    async def _try_local_openai_access_token_status_refresh(
        self,
        job_id: int,
        email: str,
        account: dict,
        *,
        exclude_access_token: str | None,
    ) -> bool:
        token_state = await self._load_local_openai_tokens(email)
        access_token = str(token_state.get("access_token") or "").strip()
        if not access_token or access_token == str(exclude_access_token or "").strip():
            return False

        client_id = str(token_state.get("client_id") or self.settings.openai_oauth_client_id or "").strip() or None
        id_token = str(token_state.get("id_token") or "").strip() or None
        try:
            profile = await self.openai_tokens.fetch_profile(access_token)
        except OpenAiAccessTokenInvalid as exc:
            await self._store_local_openai_tokens(email, access_token=None, expires_at=None)
            await self._record_local_openai_access_token_warning(job_id, email, str(exc), invalid=True)
            return False
        except Exception as exc:
            await self._record_local_openai_access_token_warning(job_id, email, str(exc), invalid=False)
            return False

        if profile.deactive:
            reason = "Local cached access_token profile check reported account_deactivated."
            await self._mark_deactive(email, reason)
            await self._finish(job_id, email, "deactive", reason)
            return True

        merged_session = _merge_openai_session_tokens(
            profile.session,
            access_token=access_token,
            id_token=id_token,
            client_id=client_id,
        )
        await self._finalize_session_refresh(
            job_id,
            email,
            account,
            session=merged_session,
            access_token=access_token,
            success_prefix="Local cached access_token status check succeeded",
            skip_account_check_merge=True,
        )
        return True

    async def _try_sub2api_protocol_refresh(self, job_id: int, email: str, account: dict) -> bool:
        if not self.sub2api.account_has_refresh_token(account):
            return False

        try:
            refreshed_account = await self.sub2api.refresh_account_credentials(account)
        except Exception as exc:
            await self._record_protocol_refresh_warning(job_id, email, str(exc), will_fallback=True)
            return False

        if refreshed_account is None:
            return False

        usage_status = await self._refresh_sub2api_usage(job_id, email, account)
        subscription_keys: list[str] = []
        access_token = self.sub2api.account_access_token(account)
        if access_token:
            subscription_keys, finished = await self._reassert_subscription_from_access_token(
                job_id,
                email,
                account,
                access_token,
            )
            if finished:
                return True
        elif self._account_has_access_token_hint(account):
            await self._record_missing_runtime_access_token(job_id, email, source="sub2api_refresh")
        subscription_text = (
            f"reasserted session subscription fields: {', '.join(subscription_keys)}"
            if subscription_keys
            else "no session subscription fields found"
        )
        remote_account, remote_issue = await self._verify_remote_recovery(account)
        if remote_issue is not None:
            await self._finish(job_id, email, "failed", remote_issue, remote_account=remote_account)
            return True
        await self._finish(
            job_id,
            email,
            "succeeded",
            f"sub2api OAuth token refresh succeeded via stored refresh_token; {usage_status}; {subscription_text}; no browser login was needed.",
            remote_account=remote_account,
        )
        return True

    async def _handle_browser_outcome(
        self,
        job_id: int,
        email: str,
        account: dict,
        outcome: BrowserRefreshOutcome | ProtocolRefreshOutcome,
        source: str = "browser",
        preferred_failure_reason: str | None = None,
    ) -> None:
        if outcome.status == "deactive":
            await self._mark_deactive(email, outcome.error or "Account is deactive.")
            await self._finish(job_id, email, "deactive", outcome.error or "Account is deactive.")
            return

        if outcome.status != "ok" or not outcome.access_token:
            await self._finish(
                job_id,
                email,
                "failed",
                preferred_failure_reason or outcome.error or "Failed to refresh access token.",
            )
            return

        success_prefix = (
            "Session refreshed via ChatGPT protocol; no browser login was needed"
            if source == "protocol"
            else "Session refreshed"
        )
        await self._store_local_openai_tokens(
            email,
            access_token=outcome.access_token,
            expires_at=_session_expires_at(outcome.session),
        )
        await self._finalize_session_refresh(
            job_id,
            email,
            account,
            session=outcome.session,
            access_token=outcome.access_token,
            success_prefix=success_prefix,
            preferred_failure_reason=preferred_failure_reason,
        )

    async def _handle_oauth_outcome(
        self,
        job_id: int,
        email: str,
        account: dict,
        outcome: OpenAiOAuthOutcome,
        source: str,
    ) -> None:
        if outcome.status == "deactive":
            await self._mark_deactive(email, outcome.error or "Account is deactive.")
            await self._finish(job_id, email, "deactive", outcome.error or "Account is deactive.")
            return
        if outcome.status == "add_phone":
            phone = await self._bind_oauth_phone_hint(email, outcome)
            if phone is None:
                phone = await self._load_bound_phone(email)
            detail = outcome.error or "OpenAI OAuth requires add phone."
            if phone is not None:
                source = phone.sms_cdk or phone.sms_url
                detail = f"{detail} 已绑定手机号 {phone.phone_number}，接码信息 {source}。"
            await self._finish(job_id, email, "failed", detail)
            return
        if outcome.status != "ok" or not outcome.access_token or not outcome.refresh_token:
            await self._finish(job_id, email, "failed", outcome.error or "OpenAI OAuth did not return a refresh_token.")
            return

        source_text = "protocol" if source == "protocol" else "headless browser"
        await self._store_local_openai_tokens(
            email,
            access_token=outcome.access_token,
            refresh_token=outcome.refresh_token,
            id_token=outcome.id_token,
            client_id=_session_client_id(outcome.session) or self.settings.openai_oauth_client_id,
            expires_at=_session_expires_at(outcome.session),
        )
        await self._finalize_session_refresh(
            job_id,
            email,
            account,
            session=outcome.session,
            access_token=outcome.access_token,
            success_prefix=f"OpenAI OAuth refresh_token acquired via {source_text}",
        )

    async def _finish_oauth_phone_verification_stopped(
        self,
        job_id: int,
        email: str,
        oauth_login_mode: str,
        provider_detail: str | None,
    ) -> None:
        await self._record_event_safely(
            "oauth_phone_verification_stopped",
            OAUTH_PHONE_VERIFICATION_STOPPED_REASON,
            email,
            {
                "job_id": job_id,
                "reason_code": "oauth_phone_verification_stopped",
                "oauth_login_mode": oauth_login_mode,
                "provider_detail": _redact_error_text(provider_detail or ""),
            },
        )
        await self._finish(
            job_id,
            email,
            "failed",
            OAUTH_PHONE_VERIFICATION_STOPPED_REASON,
        )

    async def _finalize_session_refresh(
        self,
        job_id: int,
        email: str,
        account: dict,
        *,
        session: dict[str, Any] | None,
        access_token: str,
        success_prefix: str,
        preferred_failure_reason: str | None = None,
        skip_account_check_merge: bool = False,
    ) -> None:
        working_session = dict(session) if isinstance(session, dict) else {}
        if not skip_account_check_merge:
            merge_outcome = OpenAiOAuthOutcome(status="ok", access_token=access_token, session=working_session)
            if await self._merge_account_check_status_or_mark_deactive(job_id, email, merge_outcome):
                return
            working_session = merge_outcome.session or working_session

        try:
            changed_keys = await self.sub2api.update_credentials_from_session(account, working_session, access_token)
        except Exception as exc:
            await self._finish(job_id, email, "failed", f"sub2api token update failed: {_redact_error_text(str(exc))}")
            return

        await self._test_sub2api_for_deactivation(job_id, email, account)
        usage_status = await self._refresh_sub2api_usage(job_id, email, account)
        try:
            subscription_keys = await self.sub2api.reassert_subscription_state_from_session(
                account,
                working_session,
                access_token,
            )
        except Exception as exc:
            await self._finish(job_id, email, "failed", f"sub2api subscription state sync failed: {_redact_error_text(str(exc))}")
            return

        changed_text = ", ".join(changed_keys) if changed_keys else "no credential fields changed"
        plan_type = self.sub2api.session_plan_type(working_session)
        plan_text = f"plan={plan_type}" if plan_type else "plan not returned"
        subscription_text = (
            f"reasserted session subscription fields: {', '.join(subscription_keys)}"
            if subscription_keys
            else "no session subscription fields found"
        )
        remote_account, remote_issue = await self._verify_remote_recovery(account, require_stable=True)
        if remote_issue is not None:
            final_reason = self._merge_preferred_failure_reason(remote_issue, preferred_failure_reason)
            await self._finish(
                job_id,
                email,
                "failed",
                final_reason,
                access_token_tail=access_token[-8:],
                remote_account=remote_account,
            )
            return
        await self._finish(
            job_id,
            email,
            "succeeded",
            f"{success_prefix}; {plan_text}; updated sub2api credentials: {changed_text}; {usage_status}; {subscription_text}.",
            access_token_tail=access_token[-8:],
            remote_account=remote_account,
        )

    async def _bind_oauth_phone_hint(self, email: str, outcome: OpenAiOAuthOutcome) -> BoundPhone | None:
        hint = _phone_hint_from_oauth_outcome(outcome)
        if not hint:
            return None
        async with AsyncSessionLocal() as db:
            try:
                phone = await require_oauth_phone_match(db, email, hint)
            except (OAuthPhoneResolutionError, ValueError):
                await db.rollback()
                return None
            await db.commit()
            return phone

    def _preferred_openai_failure_reason(self, value: str | None) -> str | None:
        text = str(value or "").strip()
        if not text:
            return None
        lowered = text.lower()
        if "too many phone verification requests" in lowered or "rate_limit_exceeded" in lowered:
            return text
        if "接码链接已过期" in text or "接码链接" in text and "不可用" in text:
            return text
        if "otp 应用验证" in text or "authenticator app" in lowered or "totp" in lowered:
            return text
        return None

    def _merge_preferred_failure_reason(self, fallback_reason: str, preferred_reason: str | None) -> str:
        if not preferred_reason:
            return fallback_reason
        generic_markers = (
            "sub2api still reports the account in error state after refresh",
            "authentication failed (401)",
            "unauthorized",
            "token_revoked",
        )
        lowered = fallback_reason.lower()
        if any(marker in lowered for marker in generic_markers):
            return preferred_reason
        return fallback_reason

    async def _merge_account_check_status_or_mark_deactive(
        self,
        job_id: int,
        email: str,
        outcome: BrowserRefreshOutcome | ProtocolRefreshOutcome | OpenAiOAuthOutcome,
    ) -> bool:
        if not outcome.access_token:
            return False

        try:
            status = await self.account_status.check_with_urllib(outcome.access_token)
        except Exception as exc:
            await self._record_access_token_status_warning(
                job_id,
                email,
                f"accounts/check subscription status merge failed: {exc}",
                will_fallback=False,
            )
            return False

        if status.deactive:
            reason = "ChatGPT accounts/check reported account_deactivated for refreshed session token."
            await self._mark_deactive(email, reason)
            await self._finish(job_id, email, "deactive", reason)
            return True

        outcome.session = _merge_session_status(outcome.session, status.session)
        return False

    async def _refresh_sub2api_usage(self, job_id: int, email: str, account: dict) -> str:
        try:
            usage = await self.sub2api.refresh_account_usage_data(account)
        except Exception as exc:
            if looks_deactive_text(str(exc)):
                await self._record_usage_refresh_warning(job_id, email, str(exc))
                return "sub2api usage refresh returned deactivation text; see events"
            await self._record_usage_refresh_warning(job_id, email, str(exc))
            return "sub2api usage refresh failed; see events"

        if usage is not None:
            usage = materialize_usage_reset_times(usage)
            await record_usage_limit_samples(self.sub2api, [account], {self.sub2api.account_id(account) or "": usage})
            return "sub2api usage refreshed"
        return "sub2api usage endpoint unavailable"

    async def _test_sub2api_for_deactivation(self, job_id: int, email: str, account: dict) -> str | None:
        try:
            is_deactive = await self.sub2api.test_account_for_deactivation(account)
        except Exception as exc:
            error = str(exc)
            if looks_deactive_text(error):
                await self._record_sub2api_test_warning(job_id, email, error)
                return None
            await self._record_sub2api_test_warning(job_id, email, error)
            return None

        if is_deactive:
            await self._record_sub2api_test_warning(
                job_id,
                email,
                "sub2api connection test reported account_deactivated after successful session refresh; ignored.",
            )
        return None

    async def _verify_remote_recovery(self, account: dict, require_stable: bool = False) -> tuple[dict | None, str | None]:
        account_id = self.sub2api.account_id(account)
        try:
            remote = await self.sub2api.get_account(account)
        except Exception as exc:
            return None, f"Could not verify sub2api account state after refresh: {_redact_error_text(str(exc))}"
        if remote is None:
            return None, f"Could not verify sub2api account state after refresh: account {account_id or 'unknown'} was not found in remote list."
        account.clear()
        account.update(remote)
        if self.sub2api.is_deactive_account(remote):
            return remote, "sub2api still reports the account as deactivated after refresh."
        if self.sub2api.is_error_account(remote):
            status = self.sub2api.account_status(remote) or "unknown"
            detail = self.sub2api.account_error_message(remote)
            suffix = f" error={detail}" if detail else ""
            return remote, f"sub2api still reports the account in error state after refresh: status={status}{suffix}."
        if require_stable:
            await asyncio.sleep(5)
            try:
                stable_remote = await self.sub2api.get_account(account)
            except Exception as exc:
                return remote, f"Could not complete delayed sub2api verification after refresh: {_redact_error_text(str(exc))}"
            if stable_remote is None:
                return remote, f"Could not complete delayed sub2api verification after refresh: account {account_id or 'unknown'} disappeared from remote list."
            account.clear()
            account.update(stable_remote)
            if self.sub2api.is_deactive_account(stable_remote):
                return stable_remote, "sub2api reported the account as deactivated shortly after refresh."
            if self.sub2api.is_error_account(stable_remote):
                status = self.sub2api.account_status(stable_remote) or "unknown"
                detail = self.sub2api.account_error_message(stable_remote)
                suffix = f" error={detail}" if detail else ""
                return stable_remote, (
                    "sub2api returned the account to error state shortly after refresh: "
                    f"status={status}{suffix}."
                )
            remote = stable_remote
        return remote, None

    async def cleanup_stale_jobs(self) -> int:
        async with AsyncSessionLocal() as db:
            result = await db.execute(select(RefreshJob).where(RefreshJob.status.in_(["queued", "running"])))
            jobs = list(result.scalars().all())
            now = utcnow()
            for job in jobs:
                job.status = "failed"
                job.reason = "Service restarted before refresh finished."
                if job.started_at is None:
                    job.started_at = now
                job.finished_at = now

            snapshots_result = await db.execute(select(AccountSnapshot).where(AccountSnapshot.refreshing.is_(True)))
            snapshots = list(snapshots_result.scalars().all())
            for snapshot in snapshots:
                snapshot.refreshing = False
                if snapshot.last_error is None:
                    snapshot.last_error = "Previous refresh was interrupted by service restart."

            count = len(jobs)
            if count or snapshots:
                await db.commit()
                await self._record_event_safely(
                    "refresh_cleanup",
                    f"Marked {count} stale refresh jobs as failed after startup.",
                    details={"jobs": count, "snapshots": len(snapshots)},
                )
            else:
                await db.commit()
            return count

    async def _load_local_openai_tokens(self, email: str) -> dict[str, Any]:
        async with AsyncSessionLocal() as db:
            snapshot = await db.scalar(select(AccountSnapshot).where(AccountSnapshot.email == email))
            if snapshot is None:
                return {}
            return {
                "refresh_token": decrypt_text(snapshot.encrypted_openai_refresh_token),
                "access_token": decrypt_text(snapshot.encrypted_openai_access_token),
                "id_token": decrypt_text(snapshot.encrypted_openai_id_token),
                "client_id": decrypt_text(snapshot.encrypted_openai_client_id),
                "expires_at": snapshot.openai_token_expires_at,
            }

    def _has_local_openai_token_cache(self, email: str) -> bool:
        try:
            import sqlite3

            db_url = str(self.settings.database_url or "")
            if not db_url.startswith("sqlite"):
                return False
            db_path = db_url.split("///", 1)[-1].strip()
            if not db_path:
                return False
            from pathlib import Path

            path = Path(db_path)
            if not path.is_absolute():
                path = self.settings.project_root / path
            with sqlite3.connect(path) as conn:
                row = conn.execute(
                    (
                        "SELECT encrypted_openai_refresh_token "
                        "FROM account_snapshots WHERE lower(email)=lower(?) LIMIT 1"
                    ),
                    (email,),
                ).fetchone()
            if not row:
                return False
            return bool(row[0])
        except Exception:
            return False

    async def _store_local_openai_tokens(
        self,
        email: str,
        *,
        refresh_token: str | None | object = _UNSET,
        access_token: str | None | object = _UNSET,
        id_token: str | None | object = _UNSET,
        client_id: str | None | object = _UNSET,
        expires_at: datetime | None | object = _UNSET,
    ) -> None:
        async with AsyncSessionLocal() as db:
            snapshot = await db.scalar(select(AccountSnapshot).where(AccountSnapshot.email == email))
            if snapshot is None:
                return
            if refresh_token is not _UNSET:
                snapshot.encrypted_openai_refresh_token = encrypt_text(refresh_token if isinstance(refresh_token, str) else None)
            if access_token is not _UNSET:
                snapshot.encrypted_openai_access_token = encrypt_text(access_token if isinstance(access_token, str) else None)
            if id_token is not _UNSET:
                snapshot.encrypted_openai_id_token = encrypt_text(id_token if isinstance(id_token, str) else None)
            if client_id is not _UNSET:
                snapshot.encrypted_openai_client_id = encrypt_text(client_id if isinstance(client_id, str) else None)
            if expires_at is not _UNSET:
                snapshot.openai_token_expires_at = expires_at if isinstance(expires_at, datetime) else None
            await db.commit()

    async def _load_bound_phone(self, email: str) -> BoundPhone | None:
        async with AsyncSessionLocal() as db:
            return await get_bound_phone_by_email(db, email)

    async def _load_credential(self, email: str) -> MailboxCredential | None:
        async with AsyncSessionLocal() as db:
            result = await db.execute(select(MailboxCredential).where(MailboxCredential.gpt_email == email))
            credential = result.scalar_one_or_none()
            if credential:
                db.expunge(credential)
            return credential

    async def _mark_started(self, job_id: int, email: str) -> None:
        async with AsyncSessionLocal() as db:
            job = await db.get(RefreshJob, job_id)
            if job:
                job.status = "running"
                if job.started_at is None:
                    job.started_at = utcnow()
            snapshot = await db.scalar(select(AccountSnapshot).where(AccountSnapshot.email == email))
            if snapshot:
                snapshot.refreshing = True
                snapshot.last_error = None
            await db.commit()
        await self._record_event_safely("refresh_started", "Refresh job started.", email, {"job_id": job_id})

    async def _finish(
        self,
        job_id: int,
        email: str,
        status: str,
        reason: str,
        access_token_tail: str | None = None,
        remote_account: dict | None = None,
    ) -> None:
        async with AsyncSessionLocal() as db:
            job = await db.get(RefreshJob, job_id)
            if job:
                job.status = status
                job.reason = reason
                job.access_token_tail = access_token_tail
                job.finished_at = utcnow()
            snapshot = await db.scalar(select(AccountSnapshot).where(AccountSnapshot.email == email))
            if snapshot:
                was_deactive = bool(snapshot.deactive)
                snapshot.refreshing = False
                snapshot.last_error = None if status == "succeeded" else reason
                if status == "failed":
                    snapshot.auto_refresh_locked = True
                elif status in {"succeeded", "deactive"}:
                    snapshot.auto_refresh_locked = False
                if status == "succeeded":
                    if remote_account is not None:
                        snapshot.sub2api_account_id = self.sub2api.account_id(remote_account)
                        snapshot.platform = self.sub2api.account_platform(remote_account)
                        snapshot.account_type = self.sub2api.account_type(remote_account)
                        snapshot.status = self.sub2api.account_status(remote_account) or "recovered"
                        snapshot.schedulable = self.sub2api.account_schedulable(remote_account)
                        snapshot.deactive = self.sub2api.is_deactive_account(remote_account)
                        snapshot.raw = sanitize_payload(remote_account)
                    else:
                        snapshot.deactive = False
                        snapshot.status = snapshot.status or "recovered"
                elif remote_account is not None:
                    snapshot.sub2api_account_id = self.sub2api.account_id(remote_account)
                    snapshot.platform = self.sub2api.account_platform(remote_account)
                    snapshot.account_type = self.sub2api.account_type(remote_account)
                    snapshot.status = self.sub2api.account_status(remote_account)
                    snapshot.schedulable = self.sub2api.account_schedulable(remote_account)
                    snapshot.deactive = self.sub2api.is_deactive_account(remote_account)
                    snapshot.raw = sanitize_payload(remote_account)
                if was_deactive and not snapshot.deactive:
                    await enqueue_oauth_account_enabled(
                        db,
                        email,
                        reason or "OAuth account refresh restored the account.",
                        account_id=snapshot.sub2api_account_id,
                    )
            account_id = self.sub2api.account_id(remote_account) if remote_account is not None else None
            if account_id is None and job is not None:
                account_id = job.sub2api_account_id
            if status == "succeeded":
                await clear_account_exception(db, source="sync", email=email, sub2api_account_id=account_id, commit=False)
            await db.commit()
        await self._record_event_safely(f"refresh_{status}", reason, email, {"job_id": job_id})

    async def _safe_finish(
        self,
        job_id: int,
        email: str,
        status: str,
        reason: str,
        access_token_tail: str | None = None,
    ) -> None:
        try:
            await self._finish(job_id, email, status, reason, access_token_tail=access_token_tail)
        except Exception:
            logger.exception("Failed to persist refresh job %s final status %s.", job_id, status)

    async def _record_memory_peak(self, job_id: int, email: str, peak_rss_bytes: int) -> None:
        async with AsyncSessionLocal() as db:
            job = await db.get(RefreshJob, job_id)
            if job:
                job.memory_peak_rss_bytes = peak_rss_bytes
                await db.commit()
        await self._record_event_safely(
            "refresh_memory_peak",
            f"Refresh process tree peak RSS was {_format_bytes(peak_rss_bytes)}.",
            email,
            {"job_id": job_id, "memory_peak_rss_bytes": peak_rss_bytes},
        )

    async def _safe_record_memory_peak(self, job_id: int, email: str, peak_rss_bytes: int) -> None:
        try:
            await self._record_memory_peak(job_id, email, peak_rss_bytes)
        except Exception:
            logger.exception("Failed to persist refresh job %s memory peak.", job_id)

    async def _record_event_safely(
        self,
        kind: str,
        message: str,
        email: str | None = None,
        details: dict | None = None,
    ) -> None:
        try:
            async with AsyncSessionLocal() as db:
                await record_event(db, kind, message, email, details)
        except Exception:
            logger.exception("Failed to persist app event %s.", kind)

    async def _mark_deactive(self, email: str, reason: str) -> None:
        async with AsyncSessionLocal() as db:
            snapshot = await db.scalar(select(AccountSnapshot).where(AccountSnapshot.email == email))
            if snapshot:
                was_deactive = bool(snapshot.deactive)
                if not snapshot.deactive:
                    snapshot.usage_estimate_enabled = False
                snapshot.deactive = True
                snapshot.refreshing = False
                snapshot.auto_refresh_locked = False
                snapshot.last_error = reason
                if not was_deactive:
                    await enqueue_oauth_account_disabled(db, email, reason)
            await db.commit()
        await self._record_event_safely("account_deactive", reason, email)

    async def _record_usage_refresh_warning(self, job_id: int, email: str, error: str) -> None:
        await self._record_event_safely(
            "sub2api_usage_refresh_failed",
            "sub2api usage refresh failed after credentials update.",
            email,
            {"job_id": job_id, "error": _redact_error_text(error)},
        )

    async def _record_sub2api_test_warning(self, job_id: int, email: str, error: str) -> None:
        await self._record_event_safely(
            "sub2api_connection_test_failed",
            "sub2api connection test failed after credentials update.",
            email,
            {"job_id": job_id, "error": _redact_error_text(error)},
        )

    async def _record_access_token_status_warning(
        self,
        job_id: int,
        email: str,
        error: str,
        will_fallback: bool,
    ) -> None:
        await self._record_event_safely(
            "access_token_status_check_failed",
            "Access-token account status check failed; falling back to browser login."
            if will_fallback
            else "Access-token account status check failed.",
            email,
            {"job_id": job_id, "error": _redact_error_text(error), "will_fallback": will_fallback},
        )

    async def _record_protocol_refresh_warning(
        self,
        job_id: int,
        email: str,
        error: str,
        will_fallback: bool,
    ) -> None:
        await self._record_event_safely(
            "sub2api_protocol_refresh_failed",
            "sub2api protocol token refresh failed; falling back to browser login."
            if will_fallback
            else "sub2api protocol token refresh failed.",
            email,
            {"job_id": job_id, "error": _redact_error_text(error), "will_fallback": will_fallback},
        )

    async def _record_sub2api_check_status_unavailable(self, job_id: int, email: str) -> None:
        await self._record_event_safely(
            "sub2api_check_status_unavailable",
            "sub2api /check-status 没有返回可用结果；继续尝试其他协议路径。",
            email,
            {"job_id": job_id},
        )

    async def _record_missing_runtime_access_token(self, job_id: int, email: str, source: str) -> None:
        await self._record_event_safely(
            "runtime_access_token_missing",
            "账号标记存在 access_token，但运行时账户对象里没有可直接使用的 access_token；无法继续直连 ChatGPT 账号状态检查。",
            email,
            {"job_id": job_id, "source": source},
        )

    async def _record_local_openai_refresh_token_warning(
        self,
        job_id: int,
        email: str,
        error: str,
        *,
        invalid: bool,
    ) -> None:
        await self._record_event_safely(
            "local_openai_refresh_token_failed",
            "Persisted OpenAI refresh_token refresh failed and the token was invalidated; proceeding to mailbox OAuth re-login.",
            email,
            {"job_id": job_id, "error": _redact_error_text(error), "invalid": invalid},
        )

    async def _record_local_openai_access_token_warning(
        self,
        job_id: int,
        email: str,
        error: str,
        *,
        invalid: bool,
    ) -> None:
        await self._record_event_safely(
            "local_openai_access_token_failed",
            "Local cached OpenAI access_token status check failed; falling back to other paths."
            if not invalid
            else "Local cached OpenAI access_token is invalid; falling back to other paths.",
            email,
            {"job_id": job_id, "error": _redact_error_text(error), "invalid": invalid},
        )

    def _account_has_access_token_hint(self, account: dict) -> bool:
        credentials_status = account.get("credentials_status")
        if not isinstance(credentials_status, dict):
            return False
        return any(credentials_status.get(key) is True for key in ("has_access_token", "hasAccessToken", "has_at"))

    async def _latest_protocol_failure_summary(self, job_id: int, email: str) -> str | None:
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(AppEvent)
                .where(
                    AppEvent.email == email,
                    AppEvent.kind.in_(
                        [
                            "sub2api_protocol_refresh_failed",
                            "access_token_status_check_failed",
                            "chatgpt_protocol_refresh_failed",
                            "local_openai_refresh_token_failed",
                            "local_openai_access_token_failed",
                        ]
                    ),
                )
                .order_by(AppEvent.created_at.desc())
                .limit(10)
            )
            for event in result.scalars().all():
                details = event.details if isinstance(event.details, dict) else {}
                if details.get("job_id") != job_id:
                    continue
                summary = self._protocol_failure_summary(event.kind, str(details.get("error") or ""))
                if summary:
                    return summary
        return None

    def _protocol_failure_summary(self, kind: str, error: str) -> str | None:
        text = _redact_error_text(error)
        http_match = re.search(r"\bHTTP\s+(\d{3})\b", text, re.I)
        if kind == "sub2api_protocol_refresh_failed":
            if "/refresh" in text:
                status = http_match.group(1) if http_match else None
                return f"sub2api /refresh 接口返回 {status}" if status else "sub2api /refresh 接口调用失败"
            return "sub2api 协议刷新失败"
        if kind == "access_token_status_check_failed":
            status = http_match.group(1) if http_match else None
            return f"access_token 账号状态检查返回 {status}" if status else "access_token 账号状态检查失败"
        if kind == "chatgpt_protocol_refresh_failed":
            status = http_match.group(1) if http_match else None
            return f"ChatGPT 协议刷新返回 {status}" if status else "ChatGPT 协议刷新失败"
        if kind == "local_openai_refresh_token_failed":
            return "本地缓存 OpenAI refresh_token 刷新失败"
        if kind == "local_openai_access_token_failed":
            return "本地缓存 OpenAI access_token 检查失败"
        return None

    async def _record_chatgpt_protocol_warning(
        self,
        job_id: int,
        email: str,
        error: str,
        will_fallback: bool,
    ) -> None:
        await self._record_event_safely(
            "chatgpt_protocol_refresh_failed",
            "ChatGPT protocol session refresh failed; falling back to browser login."
            if will_fallback
            else "ChatGPT protocol session refresh failed.",
            email,
            {"job_id": job_id, "error": _redact_error_text(error), "will_fallback": will_fallback},
        )

    async def _record_openai_oauth_warning(
        self,
        job_id: int,
        email: str,
        error: str,
        will_fallback: bool,
        fallback_target: str = "ChatGPT session refresh",
    ) -> None:
        edge_blocked = is_protocol_edge_verification_blocked(error)
        message = (
            (
                "OpenAI OAuth protocol login was blocked by edge verification; falling back to "
                f"{fallback_target}."
                if will_fallback
                else "OpenAI OAuth protocol login was blocked by edge verification; switch OAuth login mode to "
                "headless browser in Settings and retry."
            )
            if edge_blocked
            else (
                f"OpenAI OAuth refresh_token acquisition failed; falling back to {fallback_target}."
                if will_fallback
                else "OpenAI OAuth refresh_token acquisition failed."
            )
        )
        details: dict[str, Any] = {
            "job_id": job_id,
            "error": _redact_error_text(error),
            "will_fallback": will_fallback,
        }
        if edge_blocked:
            details["reason_code"] = OAUTH_PROTOCOL_EDGE_BLOCK_REASON_CODE
        await self._record_event_safely(
            "openai_oauth_refresh_token_failed",
            message,
            email,
            details,
        )

    async def _mail_error(self, credential_id: int, error: str) -> None:
        async with AsyncSessionLocal() as db:
            credential = await db.get(MailboxCredential, credential_id)
            if credential:
                credential.last_error = error
                await db.commit()

    async def _mail_success(
        self,
        credential_id: int,
        new_refresh_token: str | None = None,
        new_access_token: str | None = None,
    ) -> None:
        async with AsyncSessionLocal() as db:
            credential = await db.get(MailboxCredential, credential_id)
            if credential:
                credential.last_error = None
                credential.last_success_at = datetime.now(timezone.utc)
                if new_refresh_token:
                    credential.encrypted_refresh_token = encrypt_text(new_refresh_token)
                if new_access_token:
                    credential.encrypted_access_token = encrypt_text(new_access_token)
                await db.commit()


_refresh_service: RefreshService | None = None


def get_refresh_service() -> RefreshService:
    global _refresh_service
    if _refresh_service is None:
        _refresh_service = RefreshService()
    return _refresh_service


def _format_bytes(value: int) -> str:
    mib = value / 1024 / 1024
    if mib < 1024:
        return f"{mib:.1f} MiB"
    return f"{mib / 1024:.2f} GiB"


def _parse_sms_timestamp(value: str | None) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    normalized = text.replace("/", "-")
    candidates = [normalized, normalized.replace(" ", "T", 1)]
    for candidate in candidates:
        try:
            parsed = datetime.fromisoformat(candidate)
        except ValueError:
            continue
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    return None


def _parse_iso_datetime(value: str | None) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    normalized = text.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _openai_token_expires_at(token_data: dict[str, Any] | None) -> datetime | None:
    data = token_data if isinstance(token_data, dict) else {}
    expires_in = data.get("expires_in")
    if isinstance(expires_in, (int, float)):
        return datetime.now(timezone.utc) + timedelta(seconds=max(int(expires_in), 0))
    return _parse_iso_datetime(str(data.get("expires_at") or ""))


def _session_expires_at(session: dict[str, Any] | None) -> datetime | None:
    data = session if isinstance(session, dict) else {}
    for key in ("expires_at", "expiresAt"):
        parsed = _parse_iso_datetime(str(data.get(key) or ""))
        if parsed is not None:
            return parsed
    tokens = data.get("tokens") if isinstance(data.get("tokens"), dict) else {}
    for key in ("expires_at", "expiresAt"):
        parsed = _parse_iso_datetime(str(tokens.get(key) or ""))
        if parsed is not None:
            return parsed
    return None


def _session_client_id(session: dict[str, Any] | None) -> str | None:
    data = session if isinstance(session, dict) else {}
    for key in ("client_id", "clientId"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    tokens = data.get("tokens") if isinstance(data.get("tokens"), dict) else {}
    for key in ("client_id", "clientId"):
        value = tokens.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _merge_openai_session_tokens(
    session: dict[str, Any] | None,
    *,
    access_token: str,
    refresh_token: str | None = None,
    id_token: str | None = None,
    client_id: str | None = None,
    expires_at: datetime | None = None,
) -> dict[str, Any]:
    merged = dict(session) if isinstance(session, dict) else {}
    tokens = merged.get("tokens") if isinstance(merged.get("tokens"), dict) else {}
    merged["access_token"] = access_token
    merged["accessToken"] = access_token
    tokens["access_token"] = access_token
    if refresh_token:
        merged["refresh_token"] = refresh_token
        merged["refreshToken"] = refresh_token
        tokens["refresh_token"] = refresh_token
    if id_token:
        merged["id_token"] = id_token
        merged["idToken"] = id_token
        tokens["id_token"] = id_token
    if client_id:
        merged["client_id"] = client_id
        tokens["client_id"] = client_id
    if expires_at is not None:
        expires_text = expires_at.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        merged["expires_at"] = expires_text
        tokens["expires_at"] = expires_text
    if tokens:
        merged["tokens"] = tokens
    return merged


def _merge_session_status(
    session: dict | None,
    status_session: dict | None,
) -> dict:
    merged = dict(session) if isinstance(session, dict) else {}
    if not isinstance(status_session, dict):
        return merged

    for key, value in status_session.items():
        current = merged.get(key)
        if isinstance(current, dict) and isinstance(value, dict):
            nested = dict(value)
            nested.update(current)
            merged[key] = nested
        elif current in (None, "", [], {}):
            merged[key] = value
    return merged
