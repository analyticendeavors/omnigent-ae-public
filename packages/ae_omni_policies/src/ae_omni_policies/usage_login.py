"""Usage logins: the one-time sign-in that lets the limits relay read an idle account.

Added 2026-11-16 (docs/PLAN-LIMITS.md, "Idle accounts"). The owner asked that every
subscription always have a reading. A Claude account other than the default login
was read only when a chat on it replied, because chats run on its ``claude
setup-token`` token, which is inference-only and cannot read the usage endpoint.
A full ``claude auth login`` of the account (the ``user:profile`` scope) can, with
no model call. ``deploy/ae-usage-login.sh`` drives this module to make one in a
slot only the relay reads.

``python -m ae_omni_policies.usage_login <command>``:

``new [ACCOUNT]``  makes (or finds) the slot for ACCOUNT and prints its path, then
                   ``created`` or ``existing`` on a second line. With no
                   ACCOUNT it takes the server's only shared account and refuses,
                   with a count, when there is none or more than one.
``check-read SLOT`` reads the slot once through ``get_usage``; PASS or FAIL.
``check-chat SLOT`` proves a chat on the slot's account still replies on the token
                   chats use (one short model reply); PASS or FAIL.
``remove SLOT``    deletes the slot (its login stays valid at claude.ai until it
                   expires or is signed out there; it is never signed out here,
                   since a sign-out could reach the account's other tokens).

Nothing here prints an account name, label, id or token: lines say "that account".
"""

from __future__ import annotations

import argparse
import asyncio
import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path

import httpx

from ae_omni_policies import omni
from ae_omni_policies.limits import SHARED_PREFIX, headline_windows, with_usage
from ae_omni_policies.limits_relay import (
    _PROBE_UNSET,
    _SLOT_UNSET,
    ACCOUNT_ID,
    SLOT_ACCOUNT,
    SLOT_CONFIG,
    SLOT_NAME,
    RelayError,
    read_claude_usage,
    scan_usage_logins,
    usage_login_env,
    usage_logins_root,
)

# Logins the slot root must never sit in or over: the default login 31 chats and the
# remote-control processes use, and the remote-control copy (2026-11-16 plan review).
_FORBIDDEN = ("~/.claude", "~/.claude-acct2")
CHAT_TIMEOUT_S = 90.0
CHAT_PROMPT = "Reply with the single word OK."
_CHAT_ARGS = (
    "-p",
    CHAT_PROMPT,
    "--model",
    "haiku",
    "--setting-sources",
    "",
    "--strict-mcp-config",
    "--no-session-persistence",
    "--disable-slash-commands",
)


class UsageLoginError(Exception):
    """A step refused; the message says what and what to do, never naming the account."""


def safe_root(environ: Mapping[str, str] | None = None) -> Path:
    """The slot root, resolved, refused when it is or lies inside a protected login."""
    root = usage_logins_root(environ).resolve()
    for raw in _FORBIDDEN:
        forbidden = Path(raw).expanduser().resolve()
        if root == forbidden or forbidden in root.parents or root in forbidden.parents:
            raise UsageLoginError(
                "refusing: the usage-login folder overlaps a Claude login chats use; "
                "unset AE_LIMITS_USAGE_LOGINS"
            )
    return root


def slot_path(raw: str, environ: Mapping[str, str] | None = None) -> Path:
    """*raw* as a slot directly under the root, or :class:`UsageLoginError`."""
    root = safe_root(environ)
    slot = Path(raw).expanduser()
    if slot.is_symlink():
        raise UsageLoginError("refusing: that slot is a symbolic link")
    slot = slot.resolve()
    if slot.parent != root or not SLOT_NAME.fullmatch(slot.name):
        raise UsageLoginError(
            "refusing: not a usage-login slot (slot-<n> in the usage-login folder)"
        )
    if (slot / SLOT_CONFIG).is_symlink():
        raise UsageLoginError("refusing: that slot's config folder is a symbolic link")
    return slot


def slot_account(slot: Path) -> str:
    try:
        account = (slot / SLOT_ACCOUNT).read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise UsageLoginError("that slot has no account file; remove it and start again") from exc
    if not ACCOUNT_ID.fullmatch(account):
        raise UsageLoginError("that slot's account file is not a valid account id")
    return account


async def _shared_accounts(settings: omni.Settings) -> list[str]:
    if not settings.configured:
        raise UsageLoginError(settings.problem or "no OmniGent server login on this host")
    async with omni.http_client(settings) as client:
        try:
            response = await client.get("/v1/ae/claude-accounts/shared")
        except httpx.HTTPError as exc:
            raise UsageLoginError(
                f"could not list the shared accounts: {type(exc).__name__}"
            ) from exc
    if response.status_code != 200:
        raise UsageLoginError(f"listing the shared accounts answered {response.status_code}")
    rows = response.json().get("accounts", [])
    return [
        row["account"]
        for row in rows
        if isinstance(row, dict) and isinstance(row.get("account"), str)
    ]


def new_slot(account: str | None, *, settings: omni.Settings | None = None) -> Path:
    """The slot for *account* (or the only shared account), made 0700 if new.

    :func:`created` says afterwards whether this call made it.
    """
    if account is None:
        shared = asyncio.run(_shared_accounts(settings or omni.Settings.from_env()))
        if len(shared) != 1:
            raise UsageLoginError(
                f"the server has {len(shared)} shared accounts; name the one to sign in"
            )
        account = shared[0]
    account = account.strip()
    if not ACCOUNT_ID.fullmatch(account):
        raise UsageLoginError(
            "not an account id (letters, digits, spaces, - or _, with shared: before a "
            "shared account); the default login needs no usage login"
        )
    root = safe_root()
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(root, 0o700)
    for slot, held in scan_usage_logins(root):
        if held == account:
            _CREATED.discard(slot)
            return slot
    taken = {p.name for p in root.iterdir()}
    number = next(n for n in range(1, 1000) if f"slot-{n}" not in taken)
    slot = root / f"slot-{number}"
    slot.mkdir(mode=0o700)
    try:
        (slot / SLOT_CONFIG).mkdir(mode=0o700)
        target = slot / SLOT_ACCOUNT
        target.write_text(account + "\n", encoding="utf-8")
        os.chmod(target, 0o600)
    except OSError as exc:
        shutil.rmtree(slot, ignore_errors=True)
        raise UsageLoginError(f"could not make the slot ({type(exc).__name__})") from exc
    _CREATED.add(slot)
    return slot


_CREATED: set[Path] = set()


def created(slot: Path) -> bool:
    """Whether :func:`new_slot` made *slot* in this process (not an earlier sign-in's)."""
    return slot in _CREATED


def check_read(slot: Path, claude_bin: str) -> tuple[bool, str]:
    """One ``get_usage`` read through *slot*; (passed, the line to print)."""
    try:
        reply = asyncio.run(read_claude_usage(claude_bin, env=usage_login_env(slot)))
    except RelayError:
        return False, "FAIL: the usage read did not answer for that account; sign in again."
    observed = datetime.now(UTC)
    merged = with_usage(None, reply, observed, now=observed)
    if not reply.get("rate_limits_available") or merged is None or not merged.windows:
        return False, (
            "FAIL: that sign-in cannot read usage (it needs a full claude.ai sign-in, "
            "not a setup token); sign in again."
        )
    _, week = headline_windows(merged.windows)
    weekly = f" (weekly {week.used_percent:.0f}%)" if week is not None else ""
    return True, f"PASS: the usage read works for that account{weekly}."


def _chat_token(account: str) -> str:
    """The token chats on *account* launch with, exactly as a host fetches it."""
    try:
        if account.startswith(SHARED_PREFIX):
            from omnigent.onboarding.ae_shared_claude_accounts import fetch_shared_token

            # Upstream finds the server only through these variables, which a plain
            # shell lacks; the relay's own login file names it (2026-11-16 review).
            if not (os.environ.get("OMNIGENT_SERVER_URL") or os.environ.get("RUNNER_SERVER_URL")):
                server = omni.Settings.from_env().server_url
                if server:
                    os.environ["OMNIGENT_SERVER_URL"] = server

            token, _label = fetch_shared_token(account)
            return token
        from omnigent.onboarding.claude_accounts import account_env

        return account_env(account)["CLAUDE_CODE_OAUTH_TOKEN"]
    except Exception as exc:  # every cause means the same: no chat token on this host.
        raise UsageLoginError(
            f"could not get the token chats on that account use ({type(exc).__name__})"
        ) from exc


def check_chat(slot: Path, claude_bin: str) -> tuple[bool, str]:
    """Prove a chat on *slot*'s account still replies on the chats' own token.

    One short model reply (the owner accepted that cost for this one gate,
    2026-11-16), in a fresh empty config directory, so the default login is
    never read. Output is checked, never printed.
    """
    token = _chat_token(slot_account(slot))
    env = dict(os.environ)
    for key in (*_PROBE_UNSET, *_SLOT_UNSET):
        env.pop(key, None)
    with tempfile.TemporaryDirectory(prefix="ae-usage-check-") as config:
        env.update(
            CLAUDE_CODE_OAUTH_TOKEN=token, CLAUDE_CONFIG_DIR=config, DISABLE_AUTOUPDATER="1"
        )
        try:
            done = subprocess.run(  # fixed argv, no shell
                [claude_bin, *_CHAT_ARGS],
                env=env,
                cwd=config,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=CHAT_TIMEOUT_S,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return False, f"FAIL: a chat on that account did not reply ({type(exc).__name__})."
    if done.returncode != 0 or not done.stdout.strip():
        return False, f"FAIL: a chat on that account did not reply (exit {done.returncode})."
    return True, "PASS: a chat on that account still replies."


def remove_slot(slot: Path) -> None:
    shutil.rmtree(slot)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m ae_omni_policies.usage_login")
    sub = parser.add_subparsers(dest="command", required=True)
    new = sub.add_parser("new")
    new.add_argument("account", nargs="?", default=None)
    for name in ("check-read", "check-chat", "remove"):
        sub.add_parser(name).add_argument("slot")
    args = parser.parse_args(argv)
    claude_bin = shutil.which("claude") or "claude"
    try:
        if args.command == "new":
            slot = new_slot(args.account)
            print(slot)
            print("created" if created(slot) else "existing")
            return 0
        slot = slot_path(args.slot)
        if args.command == "remove":
            remove_slot(slot)
            print("Removed that usage login.")
            return 0
        check = check_read if args.command == "check-read" else check_chat
        passed, line = check(slot, claude_bin)
        print(line)
        return 0 if passed else 1
    except UsageLoginError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
