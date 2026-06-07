import json
from dataclasses import dataclass
from pathlib import Path

from app.core.config import Settings, get_settings


@dataclass(frozen=True)
class RoutedMailboxBinding:
    domain: str
    include_subdomains: bool
    mailbox_email: str
    password: str
    proxy_url: str | None = None

    def matches(self, email: str) -> bool:
        domain = email.rsplit("@", 1)[-1].strip().lower() if "@" in email else ""
        if not domain:
            return False
        if domain == self.domain:
            return True
        return self.include_subdomains and domain.endswith(f".{self.domain}")


class RoutedMailConfigService:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._cache_path: str | None = None
        self._cache_mtime_ns: int | None = None
        self._cache_bindings: list[RoutedMailboxBinding] = []

    def binding_for_email(self, email: str) -> RoutedMailboxBinding | None:
        normalized = str(email or "").strip().lower()
        if not normalized or "@" not in normalized:
            return None
        for binding in self._load_bindings():
            if binding.matches(normalized):
                return binding
        return None

    def _load_bindings(self) -> list[RoutedMailboxBinding]:
        config_path = self._resolve_config_path()
        if config_path is None or not config_path.is_file():
            self._cache_path = str(config_path) if config_path else None
            self._cache_mtime_ns = None
            self._cache_bindings = []
            return []

        stat = config_path.stat()
        path_text = str(config_path)
        if self._cache_path == path_text and self._cache_mtime_ns == stat.st_mtime_ns:
            return list(self._cache_bindings)

        try:
            payload = json.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            self._cache_path = path_text
            self._cache_mtime_ns = stat.st_mtime_ns
            self._cache_bindings = []
            return []

        route_accounts = {
            str(item.get("id") or "").strip(): item
            for item in payload.get("mail_route_accounts", [])
            if isinstance(item, dict)
        }

        bindings: list[RoutedMailboxBinding] = []
        for route_domain in payload.get("mail_route_domains", []):
            if not isinstance(route_domain, dict) or not route_domain.get("enabled"):
                continue
            domain = str(route_domain.get("domain") or "").strip().lower().lstrip("@")
            if not domain:
                continue

            route_account = self._resolve_route_account(route_domain, route_accounts)
            if route_account is None:
                continue

            source_type = str(route_account.get("source_type") or "").strip().lower()
            if source_type != "imap":
                continue

            mailbox_email = str(route_account.get("protocol_username") or route_account.get("email") or "").strip().lower()
            password = str(route_account.get("protocol_authorization_code") or route_account.get("protocol_password") or "").strip()
            protocol_host = str(route_account.get("protocol_host") or "").strip().lower()
            if not mailbox_email or not password or not protocol_host:
                continue

            # Current integration targets the same Gmail IMAP route used in mail-manager.
            if protocol_host != "imap.gmail.com":
                continue

            bindings.append(
                RoutedMailboxBinding(
                    domain=domain,
                    include_subdomains=bool(route_domain.get("include_subdomains")),
                    mailbox_email=mailbox_email,
                    password=password,
                    proxy_url=str(route_account.get("proxy_url") or "").strip() or None,
                )
            )

        self._cache_path = path_text
        self._cache_mtime_ns = stat.st_mtime_ns
        self._cache_bindings = bindings
        return list(bindings)

    def _resolve_route_account(self, route_domain: dict, route_accounts: dict[str, dict]) -> dict | None:
        linked_accounts = route_domain.get("route_accounts")
        if isinstance(linked_accounts, list):
            for linked in linked_accounts:
                if not isinstance(linked, dict):
                    continue
                route_account = route_accounts.get(str(linked.get("route_account_id") or "").strip())
                if route_account is not None and route_account.get("enabled"):
                    return route_account

        route_account = route_accounts.get(str(route_domain.get("route_account_id") or "").strip())
        if route_account is not None and route_account.get("enabled"):
            return route_account
        return None

    def _resolve_config_path(self) -> Path | None:
        raw = str(self.settings.mail_manager_route_config_path or "").strip()
        if not raw:
            return None
        path = Path(raw)
        if path.is_absolute():
            return path
        return self.settings.project_root / path


_routed_mail_config_service: RoutedMailConfigService | None = None


def get_routed_mail_config_service() -> RoutedMailConfigService:
    global _routed_mail_config_service
    if _routed_mail_config_service is None:
        _routed_mail_config_service = RoutedMailConfigService()
    return _routed_mail_config_service
