"""When each server key last worked, from the server's own outbound calls (2026-10-23).

Why: the Admin Center's Server secrets list (``secrets_health.py``) says a key is
present and when the vault says it expires, but a key can be present, unexpired
and still refused (rotated at the source and not in the vault, or the other way
round). The calls the server already makes answer that for free, so each call
site records its outcome here and the list shows "worked 2:07 PM" or
"failed: 401 at 1:50 PM" beside the key.

In memory only: it resets when the server restarts, and the page says "since the
server started". Nothing is written to disk.

What is kept is an outcome, a time and a short detail: a status code ("401") or
an exception class name ("ConnectTimeout"). ``record`` refuses anything else, so a
response body, a URL or a token can never land here by mistake.

Call sites record only answers that say something about the key: a success, or
the refusal a bad key gets (401 or 403, a 400 from a token endpoint that names the
client, Access's 302 to its login page). The OIDC sign-in records success only,
because upstream's callback words every exchange failure the same way. A 5xx or a
timeout says the far side is down, not that the key is wrong, so it is left out.

The GitHub token's real expiry also lands here: GitHub sends it in the
``github-authentication-token-expiration`` header of every API answer
(``github.py``), and the list shows it as the key's source expiry.
"""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass
from datetime import UTC, datetime

STARTED = datetime.now(UTC)
"""When this process started counting; the page says "since the server started"."""

_DETAIL = re.compile(r"^(?:[1-5][0-9]{2}|[A-Z][A-Za-z0-9_]{0,47})$")
"""A status code or an exception class name; anything else is replaced."""

GITHUB_EXPIRY_HEADER = "github-authentication-token-expiration"


@dataclass(frozen=True)
class Outcome:
    ok: bool
    at: datetime
    detail: str | None
    """The status code or error class of a failure; ``None`` on success."""
    last_ok: datetime | None
    """The last success, kept across a later failure."""


_lock = threading.Lock()
_outcomes: dict[str, Outcome] = {}
_expiries: dict[str, datetime] = {}


def clean_detail(detail: object) -> str | None:
    """*detail* when it is a status code or an error class name, else ``"error"``."""
    if detail is None:
        return None
    if isinstance(detail, BaseException):
        detail = type(detail).__name__
    text = str(detail)
    return text if _DETAIL.match(text) else "error"


def record(feature: str, ok: bool, detail: object = None, *, now: datetime | None = None) -> None:
    """Note that *feature*'s key just worked (*ok*) or was refused (*detail* says how)."""
    at = now or datetime.now(UTC)
    with _lock:
        before = _outcomes.get(feature)
        last_ok = at if ok else (before.last_ok if before else None)
        _outcomes[feature] = Outcome(
            ok=ok, at=at, detail=None if ok else clean_detail(detail) or "error", last_ok=last_ok
        )


def get(feature: str) -> Outcome | None:
    with _lock:
        return _outcomes.get(feature)


def note_expiry(key: str, when: datetime) -> None:
    """Remember the expiry the issuer reported for server key *key*."""
    with _lock:
        _expiries[key] = when if when.tzinfo else when.replace(tzinfo=UTC)


def expiry(key: str) -> datetime | None:
    with _lock:
        return _expiries.get(key)


def parse_github_expiry(value: str | None) -> datetime | None:
    """GitHub's ``2026-12-01 10:00:00 UTC`` (or ``-0800``) as an aware time, else ``None``."""
    if not value:
        return None
    text = value.strip()
    if text.endswith(" UTC"):
        text = text[:-4] + " +0000"
    elif len(text) == 19:
        text += " +0000"
    try:
        return datetime.strptime(text, "%Y-%m-%d %H:%M:%S %z").astimezone(UTC)
    except ValueError:
        return None


def reset() -> None:
    """Forget everything (tests)."""
    with _lock:
        _outcomes.clear()
        _expiries.clear()


__all__ = [
    "GITHUB_EXPIRY_HEADER",
    "STARTED",
    "Outcome",
    "clean_detail",
    "expiry",
    "get",
    "note_expiry",
    "parse_github_expiry",
    "record",
    "reset",
]
