"""Reaching the OmniGent server from outside it: URL and token, one HTTP client.

Two out-of-process callers share this: the stdio MCP server
(``ae_omni_policies.mcp``) a scheduled agent spawns on its host, and the
schedule seeder (``ae_omni_policies.schedules``, run as
``just schedules-seed``). Neither holds a task-store credential; both talk to
the server's ``/v1`` routes with the same identity a person at the CLI has,
the token ``omnigent login`` stores in ``~/.omnigent/auth_tokens.json`` keyed
by server URL (``omnigent/cli_auth.py``). Written 2026-09-23 (Phase 5).

Where the server is, first answer wins:

1. ``OMNIGENT_SERVER_URL``.
2. The ``server`` key in upstream's user config, ``~/.omnigent/config.yaml``
   (``$OMNIGENT_CONFIG_HOME/config.yaml`` when set). ``omnigent login <url>``
   writes it on every successful login (``_remember_default_server`` in
   ``omnigent/cli.py``), and bare ``omnigent`` and ``omnigent host`` read it.
3. The one server ``~/.omnigent/auth_tokens.json`` holds a login for
   (``$OMNIGENT_DATA_DIR/auth_tokens.json`` when set). With two or more and
   no configured server, nothing is picked: the wrong server would answer 401
   or, worse, accept the call.

Only the project-free user config is read, never ``.omnigent/config.yaml`` in
the working directory: the MCP server's cwd is whatever repository the agent
works in. Steps 2 and 3 exist because a host starts a stdio MCP server with
the MCP SDK's default environment (``HOME``, ``LOGNAME``, ``PATH``,
``SHELL``, ``TERM``, ``USER``; ``mcp.client.stdio.get_default_environment``,
used by ``omnigent/tools/mcp.py`` whenever the spec declares no ``env:``), so
the host unit's ``OMNIGENT_SERVER_URL`` never arrives; declaring it under
``env:`` fails spec loading wherever the variable is unset
(``expand_env_vars`` in ``omnigent/spec/parser.py``). Only ``HOME`` is
certain to be there, and the login files hang off it (2026-09-24).

The token: ``OMNIGENT_TOKEN`` when set (the seeder, tests, a launcher that
passes the full environment; a host's stdio MCP server never sees it, for
the same reason), else the login stored for the resolved URL. A single-user server (no auth)
needs neither. A stored login is re-read on every call, never held from
startup: the host renews it in ``auth_tokens.json`` about hourly, and a stdio
MCP server lives as long as its session, so the startup copy expired and every
write (``omni_set_status``) answered 401 while reads still passed (2026-09-27).
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path

import httpx

from ae_omni_policies import __version__

SERVER_URL_ENV = "OMNIGENT_SERVER_URL"
TOKEN_ENV = "OMNIGENT_TOKEN"
REQUEST_TIMEOUT_S = 30.0
USER_AGENT = f"omnigent-ae/{__version__} (+https://github.com/analyticendeavors/omnigent-ae)"
CONFIG_SERVER_KEY = "server"
FIX_HINT = f"Set {SERVER_URL_ENV} to the server's URL, or run `omnigent login <url>` as this user."
NOT_CONFIGURED = f"No OmniGent server to talk to. {FIX_HINT}"


def stored_token(server_url: str) -> str | None:
    """The token ``omnigent login`` stored for *server_url*, or ``None``.

    Read through upstream's own loader so the file's location, key shape and
    expiry rules stay upstream's. Any failure reads as no token: the caller
    then proceeds without one, which a single-user server accepts and an
    authenticated one answers with a 401 the tool reports as text.
    """
    try:
        from omnigent.cli_auth import load_token

        return load_token(server_url)
    except Exception:  # noqa: BLE001 - a missing SDK or a broken file is "no token"
        return None


def _config_path() -> Path:
    """Upstream's user config path, computed now (upstream's default is fixed at import)."""
    from omnigent.config import global_config_path

    return global_config_path(Path.home() / ".omnigent" / "config.yaml")


def _tokens_path() -> Path:
    """Upstream's login store, ``auth_tokens.json`` in the state dir (``OMNIGENT_DATA_DIR``)."""
    from omnigent.cli_auth import _token_file_path

    return _token_file_path()


def configured_server() -> str | None:
    """The ``server`` key ``omnigent login`` records in the user config, or ``None``.

    Read through upstream's own loader and path rule. An unreadable file reads
    as no key: the next source is tried and the final error names the file.
    """
    try:
        from omnigent.config import load_global_config

        value = load_global_config(_config_path()).get(CONFIG_SERVER_KEY)
    except Exception:  # noqa: BLE001 - a missing SDK or broken YAML is "no key"
        return None
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip().rstrip("/")


def stored_servers() -> list[str]:
    """Every server URL ``auth_tokens.json`` holds a record for, sorted; ``[]`` on any failure."""
    try:
        data = json.loads(_tokens_path().read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - a missing SDK, file or bad JSON is "no logins"
        return []
    if not isinstance(data, dict):
        return []
    return sorted({key.rstrip("/") for key, value in data.items() if isinstance(value, dict)})


def resolve_server_url(environ: Mapping[str, str] | None = None) -> tuple[str, str, str]:
    """Where the server is, as ``(url, source, problem)``; see the module docstring for the order.

    :param environ: The mapping to read :data:`SERVER_URL_ENV` from; ``os.environ``
        when omitted. The login files follow upstream, which reads ``os.environ``.
    :returns: ``(url, source, "")`` with *source* one of ``"env"``, ``"config"``,
        ``"login"``; or ``("", "", problem)`` where *problem* says what was looked
        at and what to do.
    """
    env = os.environ if environ is None else environ
    url = (env.get(SERVER_URL_ENV) or "").strip().rstrip("/")
    if url:
        return url, "env", ""
    url = configured_server() or ""
    if url:
        return url, "config", ""
    servers = stored_servers()
    if len(servers) == 1:
        return servers[0], "login", ""
    try:
        config, tokens = str(_config_path()), str(_tokens_path())
    except Exception:  # noqa: BLE001 - no SDK: name the default locations
        config, tokens = "~/.omnigent/config.yaml", "~/.omnigent/auth_tokens.json"
    if servers:
        problem = (
            f"No OmniGent server to talk to: {SERVER_URL_ENV} is unset, {config} names no "
            f"{CONFIG_SERVER_KEY!r}, and {tokens} holds logins for {len(servers)} servers "
            f"({', '.join(servers)}), so none is picked. {FIX_HINT}"
        )
    else:
        problem = (
            f"No OmniGent server to talk to: {SERVER_URL_ENV} is unset, {config} names no "
            f"{CONFIG_SERVER_KEY!r}, and {tokens} holds no login. {FIX_HINT}"
        )
    return "", "", problem


@dataclass(frozen=True)
class Settings:
    """Where the server is and what to send as identity.

    :param server_url: The server origin, trailing slash dropped; ``""`` when unset.
    :param token: A session token, or ``None`` to send no ``Authorization``.
    :param source: Where *server_url* came from: ``"env"``, ``"config"``, ``"login"``,
        or ``""`` when it was given directly or not found.
    :param problem: Why no server is known and what to do; what every tool
        answers while *server_url* is empty.
    :param from_login: Whether *token* came from ``auth_tokens.json``, so
        :meth:`current_token` re-reads it instead of sending the startup copy.
    """

    server_url: str = ""
    token: str | None = None
    source: str = ""
    problem: str = NOT_CONFIGURED
    from_login: bool = False

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> Settings:
        """Resolve the server (:func:`resolve_server_url`) and its token.

        :param environ: The mapping to read; ``os.environ`` when omitted.
        """
        env = os.environ if environ is None else environ
        url, source, problem = resolve_server_url(env)
        token = (env.get(TOKEN_ENV) or "").strip() or None
        from_login = bool(url) and token is None
        if from_login:
            token = stored_token(url)
        return cls(
            server_url=url,
            token=token,
            source=source,
            problem=problem or NOT_CONFIGURED,
            from_login=from_login,
        )

    @property
    def configured(self) -> bool:
        """Whether a server URL is known."""
        return bool(self.server_url)

    def current_token(self) -> str | None:
        """The token to send now. An explicit token is sent as given; a stored
        login is read from disk as it is this moment, and ``None`` once it has
        expired or been logged out, so the startup copy is never sent again."""
        if self.from_login:
            return stored_token(self.server_url)
        return self.token

    def headers(self) -> dict[str, str]:
        """Who is calling. The bearer token is not here: :class:`LoginAuth`
        adds the current one to each request. ``origin`` is upstream's
        first-party sentinel (``OMNIGENT_INTERNAL_WS_ORIGIN``), so the
        origin-checked writes such as ``POST /v1/ae/limits`` keep passing once
        upstream stops letting a missing ``Origin`` through (2026-10-22)."""
        return {
            "accept": "application/json",
            "origin": "omnigent://internal",
            "user-agent": USER_AGENT,
        }


class LoginAuth(httpx.Auth):
    """Sets ``Authorization`` on every request from :meth:`Settings.current_token`.

    On the client rather than at each call site, so every path that holds the
    client (``OmniClient.call``, ``schedule_checkin``, the schedule helpers)
    sends the login as it is now, not as it was at startup (2026-09-28 review
    of the 2026-09-27 fix, which covered ``call`` only).
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def auth_flow(self, request: httpx.Request) -> Iterator[httpx.Request]:
        token = self.settings.current_token()
        if token:
            request.headers["authorization"] = f"Bearer {token}"
        yield request


def http_client(
    settings: Settings, *, transport: httpx.AsyncBaseTransport | None = None
) -> httpx.AsyncClient:
    """An ``httpx.AsyncClient`` for the server *settings* names.

    :param settings: URL and token.
    :param transport: A transport to substitute for the network, e.g.
        ``httpx.ASGITransport(app=...)`` in tests.
    :returns: A client with the base URL, the identity headers and a timeout set.
    """
    kwargs: dict[str, object] = {}
    if transport is not None:
        kwargs["transport"] = transport
    return httpx.AsyncClient(
        base_url=settings.server_url or "http://omnigent.invalid",
        headers=settings.headers(),
        auth=LoginAuth(settings),
        timeout=httpx.Timeout(REQUEST_TIMEOUT_S),
        follow_redirects=False,
        **kwargs,  # type: ignore[arg-type]
    )


__all__ = [
    "CONFIG_SERVER_KEY",
    "FIX_HINT",
    "NOT_CONFIGURED",
    "REQUEST_TIMEOUT_S",
    "SERVER_URL_ENV",
    "TOKEN_ENV",
    "LoginAuth",
    "Settings",
    "configured_server",
    "http_client",
    "resolve_server_url",
    "stored_servers",
    "stored_token",
]
