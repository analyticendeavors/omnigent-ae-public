"""Where the MCP server and the seeder find the OmniGent server, and with what token.

Every test runs with ``HOME`` in a temp dir and the OmniGent variables
cleared, because a host starts the stdio MCP server with little more than
``HOME`` (``mcp.client.stdio.get_default_environment``). The last test
proves it end to end: ``python -m ae_omni_policies.mcp`` on the SDK's default
environment alone reaches a local HTTP server through the login files.
Written 2026-09-24.
"""

from __future__ import annotations

import json
import sys
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import httpx
import pytest
from mcp import ClientSession, StdioServerParameters, types
from mcp.client.stdio import stdio_client

from ae_omni_policies.mcp import OmniClient
from ae_omni_policies.omni import (
    NOT_CONFIGURED,
    SERVER_URL_ENV,
    TOKEN_ENV,
    Settings,
    http_client,
    resolve_server_url,
)

FAR_FUTURE = time.time() + 86_400
OMNIGENT_VARS = (SERVER_URL_ENV, TOKEN_ENV, "OMNIGENT_CONFIG_HOME", "OMNIGENT_DATA_DIR")


@pytest.fixture()
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A fresh ``HOME`` with no OmniGent state and none of its variables set."""
    monkeypatch.setenv("HOME", str(tmp_path))
    for name in OMNIGENT_VARS:
        monkeypatch.delenv(name, raising=False)
    return tmp_path


def write_config(directory: Path, body: str) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "config.yaml").write_text(body, encoding="utf-8")


def write_logins(directory: Path, logins: dict[str, str]) -> None:
    """``auth_tokens.json`` in upstream's shape: server URL to a session record."""
    directory.mkdir(parents=True, exist_ok=True)
    data = {
        url: {"token": tok, "user_id": "reid", "expires_at": FAR_FUTURE}
        for url, tok in logins.items()
    }
    (directory / "auth_tokens.json").write_text(json.dumps(data), encoding="utf-8")


def test_the_env_var_wins(home: Path) -> None:
    write_config(home / ".omnigent", "server: https://config.test\n")
    write_logins(home / ".omnigent", {"https://env.test": "tok-env"})
    settings = Settings.from_env({SERVER_URL_ENV: "https://env.test/"})
    assert (settings.server_url, settings.source, settings.token) == (
        "https://env.test",
        "env",
        "tok-env",
    )


def test_the_server_login_recorded_in_config_yaml(home: Path) -> None:
    write_config(home / ".omnigent", "model: sonnet\nserver: https://box.test:8000/\n")
    write_logins(
        home / ".omnigent", {"https://box.test:8000": "tok-box", "https://other.test": "tok-o"}
    )
    settings = Settings.from_env({})
    assert (settings.server_url, settings.source, settings.token) == (
        "https://box.test:8000",
        "config",
        "tok-box",
    )


def test_config_home_override_is_honoured(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    write_config(home / ".omnigent", "server: https://ignored.test\n")
    write_config(home / "elsewhere", "server: https://moved.test\n")
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(home / "elsewhere"))
    assert resolve_server_url({}) == ("https://moved.test", "config", "")


def test_the_one_stored_login(home: Path) -> None:
    write_logins(home / ".omnigent", {"https://only.test": "tok-only"})
    settings = Settings.from_env({})
    assert (settings.server_url, settings.source, settings.token) == (
        "https://only.test",
        "login",
        "tok-only",
    )


def test_data_dir_override_is_honoured(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    write_logins(home / "state", {"https://state.test": "tok-s"})
    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(home / "state"))
    settings = Settings.from_env({})
    assert (settings.server_url, settings.token) == ("https://state.test", "tok-s")


def test_the_token_env_var_still_wins(home: Path) -> None:
    write_logins(home / ".omnigent", {"https://only.test": "tok-stored"})
    settings = Settings.from_env({TOKEN_ENV: "tok-explicit"})
    assert (settings.server_url, settings.token) == ("https://only.test", "tok-explicit")


def _recording_client(settings: Settings, seen: list[str]) -> OmniClient:
    """An OmniClient on the real ``http_client``, recording each request's Authorization."""

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("authorization", ""))
        return httpx.Response(200, json={})

    http = http_client(settings, transport=httpx.MockTransport(handler))
    return OmniClient(settings, http=http)


async def test_a_renewed_login_is_sent_without_a_restart(home: Path) -> None:
    # 2026-09-27: the MCP server kept its startup token and every status write
    # answered 401 once the host had renewed the login in auth_tokens.json.
    write_logins(home / ".omnigent", {"https://only.test": "tok-startup"})
    seen: list[str] = []
    client = _recording_client(Settings.from_env({}), seen)
    await client.call("GET", "/v1/sessions")
    write_logins(home / ".omnigent", {"https://only.test": "tok-renewed"})
    await client.call("PATCH", "/v1/sessions/s1", json_body={"labels": {}})
    await client.aclose()
    assert seen == ["Bearer tok-startup", "Bearer tok-renewed"]


async def test_an_explicit_token_is_not_swapped_for_the_login(home: Path) -> None:
    write_logins(home / ".omnigent", {"https://only.test": "tok-stored"})
    seen: list[str] = []
    client = _recording_client(Settings.from_env({TOKEN_ENV: "tok-explicit"}), seen)
    await client.call("GET", "/v1/sessions")
    write_logins(home / ".omnigent", {"https://only.test": "tok-renewed"})
    await client.call("GET", "/v1/sessions")
    await client.aclose()
    assert seen == ["Bearer tok-explicit", "Bearer tok-explicit"]


async def test_a_direct_request_on_the_client_also_sends_the_renewed_login(home: Path) -> None:
    # schedule_checkin takes the httpx client from OmniClient and calls it
    # directly; the renewed login has to reach that path too.
    write_logins(home / ".omnigent", {"https://only.test": "tok-startup"})
    seen: list[str] = []
    client = _recording_client(Settings.from_env({}), seen)
    write_logins(home / ".omnigent", {"https://only.test": "tok-renewed"})
    await client._client().post("/v1/scheduled_tasks", json={})
    await client.aclose()
    assert seen == ["Bearer tok-renewed"]


async def test_a_logged_out_login_sends_no_token(home: Path) -> None:
    write_logins(home / ".omnigent", {"https://only.test": "tok-startup"})
    seen: list[str] = []
    client = _recording_client(Settings.from_env({}), seen)
    (home / ".omnigent" / "auth_tokens.json").write_text("{}", encoding="utf-8")
    await client.call("GET", "/v1/sessions")
    await client.aclose()
    assert seen == [""]


def test_several_logins_and_no_config_picks_none(home: Path) -> None:
    write_logins(home / ".omnigent", {"https://a.test": "tok-a", "https://b.test": "tok-b"})
    settings = Settings.from_env({})
    assert not settings.configured
    assert settings.token is None
    assert "logins for 2 servers (https://a.test, https://b.test)" in settings.problem
    assert "none is picked" in settings.problem
    assert SERVER_URL_ENV in settings.problem
    assert "omnigent login <url>" in settings.problem


def test_nothing_anywhere_says_what_to_do(home: Path) -> None:
    settings = Settings.from_env({})
    assert not settings.configured
    assert str(home / ".omnigent" / "config.yaml") in settings.problem
    assert "holds no login" in settings.problem
    assert SERVER_URL_ENV in settings.problem
    assert "omnigent login <url>" in settings.problem


def test_broken_files_read_as_absent(home: Path) -> None:
    write_config(home / ".omnigent", "server: [unclosed\n")
    (home / ".omnigent" / "auth_tokens.json").write_text("{not json", encoding="utf-8")
    url, source, problem = resolve_server_url({})
    assert (url, source) == ("", "")
    assert "holds no login" in problem


def test_settings_built_directly_carry_the_generic_problem() -> None:
    assert Settings().problem == NOT_CONFIGURED
    assert SERVER_URL_ENV in NOT_CONFIGURED


class _Recorder(BaseHTTPRequestHandler):
    """Answers ``GET /v1/ae/capacity`` and remembers what arrived."""

    seen: list[dict[str, Any]] = []

    def do_GET(self) -> None:
        type(self).seen.append({"path": self.path, "auth": self.headers.get("authorization")})
        body = json.dumps({"cap": 3, "open": 1}).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:
        return


@pytest.fixture()
def http_server() -> Iterator[str]:
    """A loopback HTTP server on a free port, for the subprocess to reach."""
    _Recorder.seen = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Recorder)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


async def test_stdio_subprocess_on_the_sdk_default_env_reaches_the_server(
    home: Path, http_server: str
) -> None:
    """What a host does: no ``env:`` in the spec, so the SDK's default environment only.

    ``env=None`` makes ``stdio_client`` build the child's environment from
    ``get_default_environment()``, which carries ``HOME`` (the temp one) and
    no ``OMNIGENT_*`` at all. The URL and the token must come from the files.
    """
    write_config(home / ".omnigent", f"server: {http_server}\n")
    write_logins(home / ".omnigent", {http_server: "tok-host"})
    params = StdioServerParameters(
        command=sys.executable, args=["-m", "ae_omni_policies.mcp"], env=None
    )
    async with stdio_client(params) as (read, write), ClientSession(read, write) as remote:
        await remote.initialize()
        result = await remote.call_tool("omni_capacity", {})
    assert result.content and isinstance(result.content[0], types.TextContent)
    assert json.loads(result.content[0].text) == {"cap": 3, "open": 1}
    assert _Recorder.seen == [{"path": "/v1/ae/capacity", "auth": "Bearer tok-host"}]
