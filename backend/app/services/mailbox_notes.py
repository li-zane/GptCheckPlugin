from __future__ import annotations

from dataclasses import dataclass
import re
from urllib import parse as urllib_parse

from pydantic import EmailStr, TypeAdapter


_EMAIL_ADAPTER = TypeAdapter(EmailStr)
_MAILBOX_RECORD_PATTERN = re.compile(
    r"^\s*(?:邮箱记录|mailbox\s+record)\s*[:：]\s*(?P<record>.+?)\s*$",
    flags=re.IGNORECASE,
)


@dataclass(frozen=True)
class NotedMailboxBinding:
    gpt_email: str
    mailbox_email: str
    custom_fetch_url: str


def parse_noted_mailbox_binding(note_text: str, expected_gpt_email: str) -> NotedMailboxBinding | None:
    expected = _email(expected_gpt_email)
    if expected is None:
        return None

    for raw_line in str(note_text or "").splitlines():
        match = _MAILBOX_RECORD_PATTERN.fullmatch(raw_line)
        if match is None:
            continue
        parts = [part.strip() for part in match.group("record").split("----")]
        if len(parts) == 2:
            gpt_email = _email(parts[0])
            mailbox_email = gpt_email
            pickup_url = _pickup_url(parts[1])
        elif len(parts) == 3:
            gpt_email = _email(parts[0])
            mailbox_email = _email(parts[1])
            pickup_url = _pickup_url(parts[2])
        else:
            continue
        if gpt_email != expected or mailbox_email is None or pickup_url is None:
            continue
        return NotedMailboxBinding(
            gpt_email=gpt_email,
            mailbox_email=mailbox_email,
            custom_fetch_url=pickup_url,
        )
    return None


def _email(value: str) -> str | None:
    try:
        return str(_EMAIL_ADAPTER.validate_python(value)).lower()
    except ValueError:
        return None


def _pickup_url(value: str) -> str | None:
    normalized = str(value or "").strip()
    parsed = urllib_parse.urlparse(normalized)
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        return None
    return normalized
