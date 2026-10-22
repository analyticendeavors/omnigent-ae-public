"""Read both CLIs' plan limits on the box host and post them to ``/v1/ae/limits``.

``python -m ae_omni_policies.limits_relay`` runs once and exits; the user timer
``deploy/systemd/user/ae-omnigent-limits.timer`` runs it every five minutes
(2026-09-24). Each harness is read independently and posted on its own, so a
missing Codex login never hides the Claude reading:

- Claude, three sources, newest reading wins:

  1. the statusLine payload ``deploy/ae-limits-statusline.sh`` saved
     (``~/.omnigent/ae/claude-statusline.json`` unless ``--claude-file`` or
     ``AE_LIMITS_CLAUDE_FILE`` says otherwise), its mtime as the reading's
     time; this is how a plain ``claude`` in a terminal reports;
  2. the claude-native harness's own capture of the same payload in each
     session's bridge directory (``context_raw.json`` under
     ``$TMPDIR/omnigent-<uid>/claude-native/``, ``AE_LIMITS_CLAUDE_BRIDGES``
     to move it). The harness builds a session's statusLine, and so its
     chain to ``~/.claude/settings.json``, once when the session starts, so
     a session that predates the settings change never runs the script in
     (1); its capture is written either way (found 2026-09-24, when an
     existing session's reply left the panel on "No reading yet"). The
     capture also carries the session's ``context_window``, posted per
     session for the popover's context row. Since 2026-10-17 the harness also
     writes ``claude_account`` beside it (the backup account the session runs
     on, empty for the default login): each session's own windows are posted
     with its account, and the host windows come from a default-login session
     whenever one has a reading, so a backup account's limits never stand in
     for the default login's;
  3. the CLI's stream-json ``get_usage`` control request, answered from the
     claude.ai usage endpoint, for the per-model weekly windows, the usage
     credits and the plan. The CLI is started without
     ``CLAUDE_CODE_OAUTH_TOKEN`` (and the other credential variables) so it
     uses the login ``claude auth login`` stored; the relay's unit never
     loads that variable anyway. Nothing is sent to a model. With no such
     login the reply says the limits are unavailable and the panel shows a
     one-line hint instead of those rows.

  Nothing at all is posted when none of them has a reading. Since
  2026-10-21 the Claude post also lists the host's signed-in logins
  (``known_accounts``: each one's name and label from upstream's
  ``account_descriptors``, never a token), which only the host can read,
  for the Admin Center's Accounts card.
- Codex: a short-lived ``codex app-server`` on stdio (newline-delimited
  JSON-RPC: ``initialize``, ``initialized``, ``account/rateLimits/read``),
  killed as soon as it answers. No ``codex`` on ``PATH`` posts nothing.

The server and token are the ones ``omnigent login`` stored for this user
(:mod:`ae_omni_policies.omni`), the identity the host itself runs as. Exit
status: 0 when every reading found was posted (or there was nothing to post),
1 when a read or a post failed; the journal says which and what to do. A Codex
reading that fails (a timeout, a refusal, no login) is a soft miss: it is
logged, and fails the run only when nothing at all was posted (2026-09-24, so
a slow ``codex app-server`` no longer leaves the unit failed every five minutes
while the Claude reading posts fine).
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import shutil
import signal
import sys
import tempfile
from collections.abc import AsyncIterator, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from ae_omni_policies import __version__, omni
from ae_omni_policies.limits import (
    MAX_ACCOUNTS,
    MAX_CONTEXTS,
    KnownAccount,
    LimitSnapshot,
    SessionContext,
    SessionLimits,
    claude_snapshot,
    codex_snapshot,
    session_context,
    with_usage,
)

CLAUDE_FILE_ENV = "AE_LIMITS_CLAUDE_FILE"
CLAUDE_BRIDGES_ENV = "AE_LIMITS_CLAUDE_BRIDGES"
CLAUDE_BIN_ENV = "AE_LIMITS_CLAUDE_BIN"
CLAUDE_PLAN_ENV = "AE_LIMITS_CLAUDE_PLAN"
CODEX_BIN_ENV = "AE_LIMITS_CODEX_BIN"
DEFAULT_CLAUDE_FILE = Path("~/.omnigent/ae/claude-statusline.json")
CODEX_TIMEOUT_S = 30.0
CLAUDE_USAGE_TIMEOUT_S = 45.0
# The harness writes these two files into every claude-native bridge directory
# (upstream omnigent/harnesses/claude_native: status.CONTEXT_RAW_FILE and
# bridge._CONFIG_FILE). Only ``conversation_id`` is read from the second; it
# also holds the bridge's bearer token, which never leaves this module.
BRIDGE_CAPTURE = "context_raw.json"
BRIDGE_CONFIG = "bridge.json"
# status.CLAUDE_ACCOUNT_FILE: the backup account name, empty for the default login.
BRIDGE_ACCOUNT = "claude_account"
# The harness's lock file: the owning process id, alive while the session runs.
BRIDGE_OWNER = "owner.pid"
# Credential variables the usage probe must not inherit: each outranks a
# stored login in the CLI's auth order, and the setup token among them lacks
# the profile scope the usage endpoint needs.
_PROBE_UNSET = (
    "CLAUDE_CODE_OAUTH_TOKEN",
    "CLAUDE_CODE_OAUTH_TOKEN_FILE_DESCRIPTOR",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
)
# The probe runs no hooks, MCP servers, skills or settings file and writes no
# transcript: a bare CLI answering one control request.
_PROBE_ARGS = (
    "-p",
    "--input-format",
    "stream-json",
    "--output-format",
    "stream-json",
    "--verbose",
    "--setting-sources",
    "",
    "--strict-mcp-config",
    "--no-session-persistence",
    "--disable-slash-commands",
)
# A status line older than this is still posted (its time is shown), but the
# relay says so, since Claude Code refreshes it only while a session is active.
STALE_AFTER_S = 6 * 60 * 60


class RelayError(Exception):
    """A reading or a post that failed, with what to do about it."""


def claude_file(environ: Mapping[str, str] | None = None) -> Path:
    env = os.environ if environ is None else environ
    raw = (env.get(CLAUDE_FILE_ENV) or "").strip()
    return Path(raw).expanduser() if raw else DEFAULT_CLAUDE_FILE.expanduser()


def claude_bridges(environ: Mapping[str, str] | None = None) -> Path:
    """The claude-native bridge root: ``$TMPDIR/omnigent-<uid>/claude-native``.

    The same path upstream derives (``bridge._BRIDGE_ROOT``, ``stable_user_id``
    is the numeric uid on POSIX); ``AE_LIMITS_CLAUDE_BRIDGES`` overrides it.
    """
    env = os.environ if environ is None else environ
    raw = (env.get(CLAUDE_BRIDGES_ENV) or "").strip()
    if raw:
        return Path(raw).expanduser()
    uid = os.getuid() if hasattr(os, "getuid") else 0
    return Path(tempfile.gettempdir()) / f"omnigent-{uid}" / "claude-native"


def _load_payload(path: Path) -> tuple[dict[str, Any], datetime] | None:
    """A saved statusLine payload and its mtime; ``None`` when there is no file.

    :raises RelayError: When the file exists but is not a JSON object.
    """
    try:
        stat = path.stat()
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise RelayError(f"could not read {path}: {exc}") from exc
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise RelayError(
            f"{path} is not JSON ({exc}); it is rewritten on the next status line render"
        ) from exc
    if not isinstance(payload, dict):
        raise RelayError(f"{path} does not hold a JSON object")
    return payload, datetime.fromtimestamp(stat.st_mtime, tz=UTC)


def _bridge_conversation(bridge_dir: Path) -> str | None:
    try:
        config = json.loads((bridge_dir / BRIDGE_CONFIG).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(config, dict):
        return None
    conversation = config.get("conversation_id") or config.get("active_session_id")
    return conversation if isinstance(conversation, str) and conversation else None


def _bridge_account(bridge_dir: Path) -> str | None:
    """The backup account a bridge's session runs on; ``None`` for the default login."""
    try:
        name = (bridge_dir / BRIDGE_ACCOUNT).read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return None
    return name[:120] or None


def _account_label(name: str) -> str:
    """The account's editable label; the name itself when the accounts module is unavailable."""
    try:
        from omnigent.onboarding.claude_accounts import account_label
    except ImportError:
        return name
    return account_label(name)[:120]


# account_descriptors' families for a Claude subscription login (the selected one is -active).
_LOGIN_FAMILIES = ("claude-oauth", "claude-oauth-active")


def known_accounts() -> list[KnownAccount]:
    """The host's signed-in Claude logins, default first: names and labels, never a token.

    Upstream's ``account_descriptors`` (what the account picker offers): the
    CLI's default login, each named login whose token this host holds, and
    the selected shared account. Empty when the accounts module is missing or
    its file cannot be read.
    """
    try:
        from omnigent.onboarding.claude_accounts import account_descriptors
    except ImportError:
        return []
    try:
        descriptors = account_descriptors()
    except Exception:  # noqa: BLE001 - a broken accounts file must not stop the limits post.
        return []
    found: list[KnownAccount] = []
    seen: set[str | None] = set()
    for entry in descriptors:
        if not isinstance(entry, Mapping) or entry.get("family") not in _LOGIN_FAMILIES:
            continue
        name, label = entry.get("env_var"), entry.get("source")
        if not isinstance(name, str) or not isinstance(label, str) or not label.strip():
            continue
        account = name.strip()[:120] or None
        if account in seen:
            continue
        seen.add(account)
        found.append(KnownAccount(account=account, label=label.strip()[:120]))
    return found[:MAX_ACCOUNTS]


Capture = tuple[dict[str, Any], datetime, str | None, str | None]


def scan_bridges(root: Path) -> list[Capture]:
    """Every session capture under *root*, newest first.

    Each is (payload, mtime, conversation id, backup account or ``None``). A
    capture that is missing, half written or unreadable is skipped: the
    harness rewrites it on the session's next render.
    """
    found: list[Capture] = []
    try:
        children = list(root.iterdir())
    except OSError:
        return found
    for child in children:
        if not child.is_dir():
            continue
        try:
            loaded = _load_payload(child / BRIDGE_CAPTURE)
        except RelayError:
            continue
        if loaded is None:
            continue
        found.append((loaded[0], loaded[1], _bridge_conversation(child), _bridge_account(child)))
    found.sort(key=lambda item: item[1], reverse=True)
    return found


def _owner_alive(bridge_dir: Path) -> datetime | None:
    """When the owner started (``owner.pid``'s mtime); ``None`` when its process is gone."""
    path = bridge_dir / BRIDGE_OWNER
    try:
        pid = int(path.read_text(encoding="utf-8").strip())
        mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
        if pid <= 0:
            return None
        os.kill(pid, 0)
    except (OSError, ValueError):
        return None
    return mtime


def scan_live(root: Path) -> list[tuple[str, datetime, str | None]]:
    """Every running claude-native session under *root*: (conversation id, started, account).

    A running session is a bridge directory whose ``owner.pid`` names a live
    process. This needs no status line capture, which the harness writes only
    after the session's first render: a session that has not rendered yet (the
    first minutes after a deploy, a fresh one) is live all the same
    (2026-10-21, the default login's "No live session").
    """
    found: list[tuple[str, datetime, str | None]] = []
    try:
        children = list(root.iterdir())
    except OSError:
        return found
    for child in children:
        if not child.is_dir():
            continue
        conversation = _bridge_conversation(child)
        started = _owner_alive(child) if conversation else None
        if conversation is None or started is None:
            continue
        found.append((conversation, started, _bridge_account(child)))
    found.sort(key=lambda item: item[1], reverse=True)
    return found


def with_live(
    snapshot: LimitSnapshot | None, live: list[tuple[str, datetime, str | None]]
) -> LimitSnapshot | None:
    """*snapshot* with a (window-less) session entry for each running session it lacks.

    The Accounts card counts ``sessions``; an entry here carries no windows,
    so it changes no reading. ``None`` stays ``None``: no Claude reading, no post.
    """
    if snapshot is None or not live:
        return snapshot
    have = {s.conversation_id for s in snapshot.sessions}
    extra: list[SessionLimits] = []
    for conversation, started, account in live:
        if conversation[:120] in have or len(snapshot.sessions) + len(extra) >= MAX_CONTEXTS:
            continue
        have.add(conversation[:120])
        extra.append(
            SessionLimits(
                conversation_id=conversation[:120],
                # Never newer than the reading: the store compares freshness by the
                # newest time a snapshot carries, and a start time must not move it.
                observed_at=min(started, snapshot.observed_at),
                account=account,
                account_label=_account_label(account) if account else None,
            )
        )
    return snapshot.model_copy(update={"sessions": [*snapshot.sessions, *extra]})


def read_claude(
    path: Path, *, bridges: Path | None = None, now: datetime | None = None
) -> LimitSnapshot | None:
    """The Claude snapshot from the saved status lines, or ``None`` when there is none.

    The newest payload that carries ``rate_limits`` gives the windows (the
    newest of any when none does, so the snapshot says why), preferring a
    default-login session's capture over a backup account's, which the
    snapshot then names. The bridge captures give each session's context and
    its own windows and account. The chained file is read only when no
    session capture exists: it holds whichever CLI rendered last, on any
    account, so beside the captures it could pass a backup's numbers off as
    the default login's (counted as the default login, as before 2026-10-17).

    :param path: The file ``deploy/ae-limits-statusline.sh`` writes.
    :param bridges: The claude-native bridge root, or ``None`` to skip it.
    :raises RelayError: When *path* is read and is not a JSON object.
    """
    captures = scan_bridges(bridges) if bridges is not None else []
    # (payload, mtime, account)
    candidates = [(payload, mtime, account) for payload, mtime, _, account in captures]
    if not candidates:
        chained = _load_payload(path)
        if chained is None:
            return None
        candidates.append((chained[0], chained[1], None))
    with_limits = [c for c in candidates if isinstance(c[0].get("rate_limits"), Mapping)]
    default_with_limits = [c for c in with_limits if c[2] is None]
    payload, observed, host_account = (default_with_limits or with_limits or candidates)[0]
    snapshot = claude_snapshot(payload, observed, now=now)
    labels = {account: _account_label(account) for *_, account in captures if account}
    if host_account is not None:
        snapshot = snapshot.model_copy(
            update={"account": host_account, "account_label": labels[host_account]}
        )
    contexts: list[SessionContext] = []
    sessions: list[SessionLimits] = []
    seen: set[str] = set()
    for capture, mtime, conversation, account in captures:
        if conversation is None or conversation in seen:
            continue
        seen.add(conversation)
        if len(sessions) < MAX_CONTEXTS:
            sessions.append(
                SessionLimits(
                    conversation_id=conversation[:120],
                    observed_at=mtime,
                    account=account,
                    account_label=labels[account] if account else None,
                    windows=claude_snapshot(capture, mtime, now=now).windows[:4],
                )
            )
        context = session_context(capture, conversation, mtime)
        if context is not None and len(contexts) < MAX_CONTEXTS:
            contexts.append(context)
    return snapshot.model_copy(update={"contexts": contexts, "sessions": sessions})


def _probe_env(environ: Mapping[str, str] | None = None) -> dict[str, str]:
    env = dict(os.environ if environ is None else environ)
    for key in _PROBE_UNSET:
        env.pop(key, None)
    return env


@contextlib.asynccontextmanager
async def _child(*argv: str, **kwargs: Any) -> AsyncIterator[asyncio.subprocess.Process]:
    """A CLI on stdin and stdout pipes, torn down completely while the loop still runs.

    The child starts a session of its own so that leaving the block kills its
    whole process group, not just the direct child: ``codex`` is a node
    launcher whose native binary inherits the pipes. The subprocess transport
    is then closed here, while the event loop is alive. Left open (the direct
    child gone, a descendant still holding the pipes, found 2026-09-24 after a
    Codex timeout), the transport's ``__del__`` closed it after ``asyncio.run``
    had closed the loop and raised ``RuntimeError: Event loop is closed``.
    """
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        start_new_session=True,
        limit=4 * 1024 * 1024,
        **kwargs,
    )
    try:
        yield proc
    finally:
        await _reap(proc)


async def _reap(proc: asyncio.subprocess.Process) -> None:
    killpg = getattr(os, "killpg", None)
    if killpg is not None:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            killpg(proc.pid, signal.SIGKILL)
    with contextlib.suppress(ProcessLookupError):
        proc.kill()
    # asyncio has no public way to close a subprocess transport; Process keeps
    # it as ``_transport``. Closing it closes the pipes from this side, so the
    # wait below cannot hang on a descendant that escaped the process group.
    transport = getattr(proc, "_transport", None)
    if transport is not None:
        transport.close()
    with contextlib.suppress(Exception):
        await proc.wait()


async def _claude_usage_exchange(claude_bin: str) -> dict[str, Any]:
    request_id = "ae-limits-usage"
    async with _child(claude_bin, *_PROBE_ARGS, env=_probe_env(), cwd=str(Path.home())) as proc:
        assert proc.stdin is not None and proc.stdout is not None
        request = {
            "type": "control_request",
            "request_id": request_id,
            "request": {"subtype": "get_usage", "skip_behaviors": True},
        }
        proc.stdin.write((json.dumps(request) + "\n").encode("utf-8"))
        await proc.stdin.drain()
        while True:
            message = await _read_line(proc.stdout)
            if message is None:
                raise RelayError(
                    "claude exited before answering get_usage; `claude --version` as this "
                    "user should name 2.1.281 or newer"
                )
            if message.get("type") != "control_response":
                continue
            response = message.get("response")
            if not isinstance(response, dict) or response.get("request_id") != request_id:
                continue
            if response.get("subtype") != "success":
                raise RelayError(f"claude refused get_usage: {response.get('error')}")
            body = response.get("response")
            return body if isinstance(body, dict) else {}


async def read_claude_usage(
    claude_bin: str, *, timeout: float = CLAUDE_USAGE_TIMEOUT_S
) -> dict[str, Any]:
    """Ask the Claude CLI for ``/usage``'s structured data (``get_usage``).

    :raises RelayError: When the CLI cannot start, refuses, or times out.
    """
    try:
        return await asyncio.wait_for(_claude_usage_exchange(claude_bin), timeout)
    except TimeoutError as exc:
        raise RelayError(f"claude did not answer get_usage within {timeout:.0f}s") from exc
    except OSError as exc:
        raise RelayError(f"could not start {claude_bin}: {exc}") from exc


async def _read_line(stream: asyncio.StreamReader) -> dict[str, Any] | None:
    raw = await stream.readline()
    if not raw:
        return None
    try:
        message = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return message if isinstance(message, dict) else {}


async def _codex_exchange(codex_bin: str) -> dict[str, Any]:
    async with _child(codex_bin, "app-server") as proc:
        return await _codex_talk(proc)


async def _codex_talk(proc: asyncio.subprocess.Process) -> dict[str, Any]:
    assert proc.stdin is not None and proc.stdout is not None

    async def send(message: dict[str, Any]) -> None:
        assert proc.stdin is not None
        proc.stdin.write((json.dumps(message) + "\n").encode("utf-8"))
        await proc.stdin.drain()

    async def answer(request_id: int) -> dict[str, Any]:
        assert proc.stdout is not None
        while True:
            message = await _read_line(proc.stdout)
            if message is None:
                raise RelayError(
                    "codex app-server exited before answering; run `codex login` as this "
                    "user if it has never been logged in"
                )
            if message.get("id") != request_id or "method" in message:
                continue  # a notification or a server request; not ours
            error = message.get("error")
            if error:
                detail = error.get("message") if isinstance(error, dict) else error
                raise RelayError(f"codex app-server refused request {request_id}: {detail}")
            result = message.get("result")
            return result if isinstance(result, dict) else {}

    await send(
        {
            "id": 1,
            "method": "initialize",
            "params": {"clientInfo": {"name": "omnigent-ae-limits", "version": __version__}},
        }
    )
    await answer(1)
    await send({"method": "initialized"})
    await send(
        {
            "id": 2,
            "method": "account/rateLimits/read",
            "params": {"excludeResetCreditDetails": True},
        }
    )
    return await answer(2)


async def read_codex(codex_bin: str, *, timeout: float | None = None) -> LimitSnapshot:
    """Ask ``codex app-server`` for the account's rate limits.

    :param timeout: Seconds to wait for the answer; ``CODEX_TIMEOUT_S`` when ``None``.
    :raises RelayError: When the process cannot start, refuses, or times out.
    """
    if timeout is None:
        timeout = CODEX_TIMEOUT_S
    try:
        result = await asyncio.wait_for(_codex_exchange(codex_bin), timeout)
    except TimeoutError as exc:
        raise RelayError(f"codex app-server did not answer within {timeout:.0f}s") from exc
    except OSError as exc:
        raise RelayError(f"could not start {codex_bin} app-server: {exc}") from exc
    return codex_snapshot(result, datetime.now(UTC))


async def post(client: httpx.AsyncClient, snapshot: LimitSnapshot) -> None:
    """``POST /v1/ae/limits/{harness}``.

    :raises RelayError: On a transport failure or a non-2xx answer.
    """
    path = f"/v1/ae/limits/{snapshot.harness}"
    try:
        response = await client.post(path, json=snapshot.model_dump(mode="json"))
    except httpx.HTTPError as exc:
        raise RelayError(f"POST {path} failed: {exc}") from exc
    if response.status_code == 401:
        raise RelayError(
            f"POST {path} answered 401: the stored login expired; run `omnigent login "
            f"{client.base_url}` as this user"
        )
    if response.status_code >= 300:
        raise RelayError(f"POST {path} answered {response.status_code}: {response.text[:200]}")


async def run(
    *,
    claude_path: Path,
    codex_bin: str | None,
    settings: omni.Settings,
    bridges: Path | None = None,
    claude_bin: str | None = None,
    plan_label: str | None = None,
    dry_run: bool = False,
    transport: httpx.AsyncBaseTransport | None = None,
    out: Any = None,
) -> int:
    """Read what there is, post it, report each harness on one line; the exit status.

    A failed ``get_usage`` probe is reported but is not a failure: the status
    line reading still posts, and the panel's hint says what the probe needs.
    A failed Codex read is a soft miss: reported, and a failure only when
    nothing else was posted either.
    """
    out = out or sys.stdout
    failures = 0
    codex_missed = False
    snapshots: list[LimitSnapshot] = []
    now = datetime.now(UTC)

    claude: LimitSnapshot | None = None
    claude_ok = True
    try:
        claude = read_claude(claude_path, bridges=bridges, now=now)
    except RelayError as exc:
        print(f"claude: {exc}", file=out)
        failures += 1
        claude_ok = False
    else:
        if claude is None:
            print(
                f"claude: no status line yet (none at {claude_path}"
                + (f" or under {bridges}" if bridges is not None else "")
                + "); send a message in any Claude session (docs/PLAN-LIMITS.md)",
                file=out,
            )
        else:
            age = (now - claude.observed_at).total_seconds()
            if age > STALE_AFTER_S:
                print(f"claude: last status line is {age / 3600:.1f}h old", file=out)
    if claude_ok:
        reply: dict[str, Any] | None = None
        if claude_bin is None:
            print("claude: no claude on PATH; per-model limits not read", file=out)
        else:
            try:
                reply = await read_claude_usage(claude_bin)
            except RelayError as exc:
                print(f"claude: {exc}", file=out)
            else:
                if not reply.get("rate_limits_available"):
                    print(
                        "claude: get_usage has no plan limits for this login (no profile "
                        "scope); per-model limits and credits need `claude auth login` "
                        "(docs/PLAN-LIMITS.md)",
                        file=out,
                    )
        claude = with_usage(claude, reply, datetime.now(UTC), plan_label=plan_label)
        if bridges is not None:
            claude = with_live(claude, scan_live(bridges))
        if claude is not None:
            snapshots.append(claude.model_copy(update={"known_accounts": known_accounts()}))

    if codex_bin is None:
        print("codex: no codex on PATH; nothing to read", file=out)
    else:
        try:
            snapshots.append(await read_codex(codex_bin))
        except RelayError as exc:
            print(f"codex: {exc}", file=out)
            codex_missed = True

    if dry_run:
        for snapshot in snapshots:
            print(json.dumps(snapshot.model_dump(mode="json"), indent=2), file=out)
        return 1 if failures or (codex_missed and not snapshots) else 0
    if not snapshots:
        return 1 if failures or codex_missed else 0
    if not settings.configured:
        print(settings.problem, file=out)
        return 1
    posted = 0
    async with omni.http_client(settings, transport=transport) as client:
        for snapshot in snapshots:
            try:
                await post(client, snapshot)
            except RelayError as exc:
                print(f"{snapshot.harness}: {exc}", file=out)
                failures += 1
            else:
                posted += 1
                summary = ", ".join(
                    f"{w.label} {w.used_percent:.0f}%" for w in snapshot.windows
                ) or (snapshot.note or "no windows")
                print(f"{snapshot.harness}: posted ({summary})", file=out)
    return 1 if failures or (codex_missed and not posted) else 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m ae_omni_policies.limits_relay",
        description="Post Claude Code's and Codex's plan limits to the OmniGent server.",
    )
    parser.add_argument("--claude-file", type=Path, default=None)
    parser.add_argument(
        "--claude-bridges",
        type=Path,
        default=None,
        help=f"the claude-native bridge root (default: ${CLAUDE_BRIDGES_ENV}, else "
        "$TMPDIR/omnigent-<uid>/claude-native)",
    )
    parser.add_argument(
        "--claude-bin",
        default=None,
        help=f"the claude executable for get_usage (default: ${CLAUDE_BIN_ENV}, else "
        "claude on PATH)",
    )
    parser.add_argument(
        "--codex-bin",
        default=None,
        help=f"the codex executable (default: ${CODEX_BIN_ENV}, else codex on PATH)",
    )
    parser.add_argument("--dry-run", action="store_true", help="print, do not post")
    args = parser.parse_args(argv)
    codex_bin = args.codex_bin or os.environ.get(CODEX_BIN_ENV) or shutil.which("codex")
    claude_bin = args.claude_bin or os.environ.get(CLAUDE_BIN_ENV) or shutil.which("claude")
    plan_label = (os.environ.get(CLAUDE_PLAN_ENV) or "").strip() or None
    return asyncio.run(
        run(
            claude_path=args.claude_file or claude_file(),
            codex_bin=codex_bin,
            bridges=args.claude_bridges or claude_bridges(),
            claude_bin=claude_bin,
            plan_label=plan_label,
            settings=omni.Settings.from_env(),
            dry_run=args.dry_run,
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())
