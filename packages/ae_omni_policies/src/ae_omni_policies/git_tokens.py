"""Checking a GitHub token typed into the Cards panel (2026-11-08, card setup PR 3).

Three helpers, all for the token routes in ``router.py``:

- :func:`check_token` is the shape rule, run in the route handler and never as a pydantic
  pattern, because pydantic's 422 echoes the offending input and a pasted token must never be
  echoed back. Its error text never holds any part of the token.
- :func:`verify` asks GitHub who the token belongs to (``GET /user``) and when it expires.
- :func:`visible_repos` lists the repos the token can see, for the picker.

Both calls go to the fixed host :data:`API_URL`, never the ``AE_GITHUB_API_URL`` the server's
own reads honour: a typed token must never be sent to an address a setting can change.
Fine-grained tokens only (``github_pat_``); the environment's own tokens may be any kind.
Nothing here logs a token, and an exception from the HTTP client is reduced to its class name.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import httpx

from ae_omni_policies import github

_logger = logging.getLogger(__name__)

API_URL = "https://api.github.com"
USER_URL = f"{API_URL}/user"
REPOS_URL = f"{API_URL}/user/repos"
TIMEOUT_S = 10.0
PREFIX = "github_pat_"
MIN_LEN = 40
MAX_LEN = 255
#: The most characters a request may carry for a token; beyond this nothing is looked at.
INPUT_MAX = 300
MAX_PAGES = 3
MAX_VISIBLE = 300
EXPIRY_HEADER = "github-authentication-token-expiration"

#: Swapped by tests; production passes no transport.
TRANSPORT: httpx.AsyncBaseTransport | None = None

_SHAPE = re.compile(r"^[A-Za-z0-9_]+$")
_EXPIRY = re.compile(r"^(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2}:\d{2}) (UTC|[+-]\d{4})$")

CODE_BAD_TOKEN = "invalid_token"
CODE_REFUSED = "github_refused_token"
CODE_UNAVAILABLE = "github_unavailable"


class TokenError(Exception):
    """A token that cannot be used, or a check that could not finish; safe to show."""

    status_code = 422
    code = CODE_BAD_TOKEN


class TokenRefused(TokenError):
    code = CODE_REFUSED


class TokenCheckUnavailable(TokenError):
    status_code = 502
    code = CODE_UNAVAILABLE


def check_token(raw: object) -> str:
    """The token *raw* as typed, trimmed, when it has the shape of a fine-grained token.

    :raises TokenError: When it does not. The message states the rule and holds no part of the
        token.
    """
    if not isinstance(raw, str):
        raise TokenError("The token must be text.")
    if len(raw) > INPUT_MAX:
        raise TokenError(
            f"That is too long to be a GitHub token (at most {MAX_LEN} characters). "
            f"Paste a fine-grained token, which starts with {PREFIX}."
        )
    token = raw.strip()
    if not token.startswith(PREFIX):
        raise TokenError(
            f"Paste a fine-grained GitHub token, which starts with {PREFIX}. A classic token "
            "can be set in the server's environment instead."
        )
    if not MIN_LEN <= len(token) <= MAX_LEN or not _SHAPE.fullmatch(token):
        raise TokenError(
            f"That does not look like a fine-grained GitHub token ({MIN_LEN} to {MAX_LEN} "
            "letters, digits and underscores). Copy it again from GitHub."
        )
    return token


def parse_expiry(value: str | None) -> str | None:
    """GitHub's expiry header (``2026-02-05 00:00:00 UTC`` or with ``-0800``) as ISO UTC."""
    if not value:
        return None
    match = _EXPIRY.fullmatch(value.strip())
    if match is None:
        return None
    zone = match.group(3)
    try:
        naive = datetime.fromisoformat(f"{match.group(1)}T{match.group(2)}")
    except ValueError:
        return None
    if zone == "UTC":
        moment = naive.replace(tzinfo=UTC)
    else:
        sign = 1 if zone[0] == "+" else -1
        offset = timedelta(hours=int(zone[1:3]), minutes=int(zone[3:5]))
        moment = naive.replace(tzinfo=timezone(sign * offset))
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": github.USER_AGENT,
    }


async def _get(
    client: httpx.AsyncClient, url: str, token: str, params: dict[str, Any] | None = None
) -> httpx.Response:
    try:
        return await client.get(url, headers=_headers(token), params=params, timeout=TIMEOUT_S)
    except httpx.HTTPError as exc:
        _logger.warning("ae git tokens: GitHub did not answer (%s)", type(exc).__name__)
        raise TokenCheckUnavailable(
            "GitHub did not answer in time; nothing was saved. Try again in a moment."
        ) from exc


async def verify(
    token: str, *, transport: httpx.AsyncBaseTransport | None = None
) -> tuple[str, str | None]:
    """(GitHub login, expiry as ISO UTC or ``None``) for *token*.

    :raises TokenRefused: GitHub answered 401: the token is wrong, expired or revoked.
    :raises TokenCheckUnavailable: Anything else that is not a 200; worth trying again.
    """
    async with httpx.AsyncClient(transport=transport or TRANSPORT) as client:
        response = await _get(client, USER_URL, token)
    if response.status_code == 401:
        raise TokenRefused("GitHub refused this token: it is expired, revoked or mistyped.")
    if response.status_code != 200:
        _logger.warning(
            "ae git tokens: GitHub answered %s to the token check", response.status_code
        )
        raise TokenCheckUnavailable(
            f"GitHub answered {response.status_code} to the token check; nothing was saved. "
            "Try again in a moment."
        )
    try:
        login = response.json().get("login")
    except (ValueError, AttributeError):
        login = None
    if not isinstance(login, str) or not login.strip():
        raise TokenCheckUnavailable("GitHub sent an answer this check cannot read; try again.")
    return login.strip(), parse_expiry(response.headers.get(EXPIRY_HEADER))


async def visible_repos(
    token: str, *, transport: httpx.AsyncBaseTransport | None = None
) -> list[dict[str, Any]]:
    """The repos *token* can see, ``[{"repo": "owner/name", "private": bool}]``, at most 300.

    Three pages of 100, sorted by name. A page that fails stops the list there.
    """
    found: list[dict[str, Any]] = []
    params: dict[str, Any] = {
        "per_page": 100,
        "sort": "full_name",
        "affiliation": "owner,collaborator,organization_member",
    }
    async with httpx.AsyncClient(transport=transport or TRANSPORT) as client:
        for page in range(1, MAX_PAGES + 1):
            response = await _get(client, REPOS_URL, token, {**params, "page": page})
            if response.status_code == 401:
                raise TokenRefused(
                    "GitHub refused this token: it is expired, revoked or mistyped."
                )
            if response.status_code != 200:
                raise TokenCheckUnavailable(
                    f"GitHub answered {response.status_code} listing the repos; try again."
                )
            try:
                rows = response.json()
            except ValueError:
                rows = None
            if not isinstance(rows, list):
                raise TokenCheckUnavailable("GitHub sent a repo list this check cannot read.")
            for row in rows:
                name = row.get("full_name") if isinstance(row, dict) else None
                if isinstance(name, str) and github._REPO.match(name):
                    found.append({"repo": name, "private": bool(row.get("private"))})
            if len(rows) < 100 or len(found) >= MAX_VISIBLE:
                break
    return found[:MAX_VISIBLE]


__all__ = [
    "CODE_BAD_TOKEN",
    "CODE_REFUSED",
    "CODE_UNAVAILABLE",
    "INPUT_MAX",
    "TRANSPORT",
    "TokenCheckUnavailable",
    "TokenError",
    "TokenRefused",
    "check_token",
    "parse_expiry",
    "verify",
    "visible_repos",
]
