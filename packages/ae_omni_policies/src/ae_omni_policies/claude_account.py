"""Switch a Claude chat to another login in place, or every open chat at once.

Added 2026-10-27. A chat stays on the login it started with (the runner's
launch keeps the last login), so moving it is a deliberate act: this module
stores the new login on the session, then asks the chat's runner to restart its
Claude terminal, which resumes the same conversation on the new subscription.

========================================  ==============================================
Route                                     Answers
========================================  ==============================================
``PUT  /sessions/{id}/claude-account``    ``{"account", "relaunched"}``
``POST /claude-account/move-all``         ``{"moved", "skipped", "pending", "queued"}``
========================================  ==============================================

The login rides on the session as ``omnigent.claude_native.account``, the label
a fork's pick uses and the runner reads at launch. It is server-internal for
clients, so only this server writes it. The checks are the fork's
(``validate_fork_claude_account``): a Claude native chat with no provider
credentials, on a connected host the caller owns, with a login that host lists.

``include_children`` on the PUT (added 2026-10-28) adds a ``children`` list and moves
the chat's family children too: the sessions whose ``ae.orch`` label names it, and
theirs, owned by the same user. A fork and a sub-agent never move with the parent.
Each child goes through the same :func:`switch`; one that is mid-reply is skipped and
listed, not failed.

Move-all (changed 2026-11-13, the owner's call: no prompt) reaches every chat the caller
may write: open, closed and archived. An archived chat has no runner, so its login is stored
(both labels) and it is listed ``queued``. A chat mid-reply or waiting on an approval keeps
the new login stored for its next launch and is listed ``pending`` (reason
:data:`PENDING_REASON`) while a bounded background retry restarts its terminal once the reply
ends; only a real failure is ``skipped``.

Review fixes (2026-11-13): move-all pages through every chat (no 500-row cut; a hard cap is
reported as ``"truncated"``); a chat with no running terminal is checked by
:func:`validate_stored_claude_account` (owner and host-owner checks, one host probe per
host, none for a ``shared:`` login) instead of the fork check; every request for a chat takes
a claim number and an older one does nothing under the chat's lock (so a late busy refusal
cannot overwrite a newer pick); the retry keeps going through unanswered and transient
failures until its deadline; the runs of a scheduled task pinned to a login are skipped; and
a ``shared:`` login moves chats on every host.

Runner answers: 2xx is ``relaunched``; a refusal (a reply or approval in
progress, a terminal that would not start) puts the old label back and passes
through; no runner bound keeps the label (the next launch uses it) and answers
``"queued": true`` (added 2026-11-09, so the web says the chat moves at its next
message rather than now); no answer within the budget is a 202, because the
restart may have happened.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import logging
import weakref
from collections.abc import Callable
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Path, Request
from fastapi.responses import JSONResponse
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.onboarding.ae_shared_claude_accounts import known_on_server
from omnigent.onboarding.claude_accounts import CLAUDE_ACCOUNT_LABEL_KEY
from omnigent.runtime import get_agent_store, get_conversation_store
from omnigent.server.routes._content_type import require_json_content_type
from omnigent.server.routes._origin import require_trusted_origin
from pydantic import BaseModel, ConfigDict, StrictStr

from ae_omni_policies import limits, sessions
from ae_omni_policies.auth import require_ae_user

_logger = logging.getLogger(__name__)

router = APIRouter(dependencies=[Depends(require_ae_user)])

CHANGE_EVENT = "claude_account_change"
# Past the runner's worst case: closing the pane, then the launch's own startup wait.
FORWARD_TIMEOUT_S = 90.0
# What the runner answers when a reply or an approval is in progress.
BUSY_CODE = "claude_account_busy"
# The fork check's refusals for a chat that has no Claude login to move.
_NOT_A_LOGIN_CHAT = ("Claude native target", "provider authentication")

# Move-all switches this many chats at once, and answers after this long; the rest finish alone.
MOVE_ALL_CONCURRENCY = 4
MOVE_ALL_CAP_S = 120.0
_background: set[asyncio.Task[Any]] = set()
# A busy chat is retried every RETRY_EVERY_S for up to RETRY_FOR_S, one retry task per chat.
RETRY_EVERY_S = 30.0
RETRY_FOR_S = 1800.0
PENDING_REASON = "moves when its reply ends"
STILL_SWITCHING_REASON = "still switching"
PINNED_REASON = "pinned by its scheduled task"
_retries: dict[str, asyncio.Task[Any]] = {}
# The latest login asked for per chat, as (claim number, account). A switch that wakes up under
# the chat's lock holding an older claim does nothing, so a slow move to B can never overwrite
# a newer pick of A (a late busy refusal used to).
_claims: dict[str, tuple[int, str]] = {}
_claim_numbers = itertools.count(1)
_PROVIDER_FAMILIES = frozenset({"claude-provider-env", "claude-provider-config"})
_ACCOUNT_FAMILIES = frozenset({"claude-oauth", "claude-oauth-active"})
# The owner a store without per-user sessions (single user, no auth) reports.
LOCAL_OWNER = "local"

_SESSION_PATH = Annotated[str, Path(min_length=1, max_length=128)]
_locks: weakref.WeakValueDictionary[str, asyncio.Lock] = weakref.WeakValueDictionary()
_RUNNER_CODES = {400: ErrorCode.INVALID_INPUT, 409: ErrorCode.CONFLICT}
# Marks a switch answered with no terminal running: the chat starts on the login next time.
_QUEUED = {"queued": True}
QUEUED_REASON = "starts at its next message"


class AccountBody(BaseModel):
    """Body of ``PUT /sessions/{id}/claude-account``; ``""`` is the default login."""

    model_config = ConfigDict(extra="forbid")

    account: StrictStr


class SwitchBody(AccountBody):
    """Body of the PUT: ``include_children`` also moves the chat's family children."""

    include_children: bool = False


class MoveAllBody(AccountBody):
    """Body of ``POST /claude-account/move-all``; ``host_id`` limits it to one host's chats."""

    host_id: StrictStr | None = None


class Refused(Exception):
    """The runner refused the switch; the label is back as it was."""

    def __init__(self, status: int, code: str, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.code = code
        self.detail = detail


def current_account(labels: dict[str, str] | None) -> str | None:
    """The login a chat's terminal is on: the runner's last launch, else the server label.

    The live label wins because it is what the terminal really launched on; the
    server label can name a login a refused or failed restart never reached.
    """
    held = labels or {}
    if limits.ACCOUNT_LIVE_LABEL in held:
        return held[limits.ACCOUNT_LIVE_LABEL]
    return held.get(CLAUDE_ACCOUNT_LABEL_KEY)


def _refusal(status: int, body: str) -> Refused:
    """The runner's ``{"error", "detail"}`` answer as a :class:`Refused`."""
    try:
        payload = json.loads(body)
    except (TypeError, ValueError):
        payload = None
    payload = payload if isinstance(payload, dict) else {}
    detail = payload.get("detail")
    code = payload.get("error")
    return Refused(
        status,
        code if isinstance(code, str) else "runner_error",
        detail.strip()
        if isinstance(detail, str) and detail.strip()
        else f"the runner returned status {status}.",
    )


async def _restore(session_id: str, old: str | None, old_live: str | None) -> None:
    """Put both labels back as they were; the card reads the live one."""
    store = get_conversation_store()
    for key, value in ((CLAUDE_ACCOUNT_LABEL_KEY, old), (limits.ACCOUNT_LIVE_LABEL, old_live)):
        if value is None:
            await asyncio.to_thread(store.delete_label, session_id, key)
        else:
            await asyncio.to_thread(store.set_labels, session_id, {key: value})


def _claim(session_id: str, account: str) -> int:
    """Record that *account* is now the latest login asked for *session_id*; the claim number."""
    number = next(_claim_numbers)
    _claims[session_id] = (number, account)
    earlier = _retries.pop(session_id, None)
    if earlier is not None:
        earlier.cancel()  # a retry for an older request has nothing left to do
    return number


def _claim_is_latest(session_id: str, claim: int | None) -> bool:
    return claim is None or _claims.get(session_id, (None, ""))[0] == claim


def _invalid(message: str, code: ErrorCode = ErrorCode.INVALID_INPUT) -> OmnigentError:
    return OmnigentError(message, code=code)


async def _stored_claude_check(agent: Any, conv: Any) -> None:
    """Raise unless *conv* is a Claude native chat on no provider credentials.

    The fork check's agent half, kept for a chat with no running terminal. An agent that
    cannot be loaded (deleted, bundle gone) falls back to the chat's own login labels: a
    chat that ever launched Claude carries one, so it still moves.
    """
    from omnigent.harness_aliases import canonicalize_harness
    from omnigent.onboarding.claude_accounts import CLAUDE_PROVIDER_ENV_KEYS
    from omnigent.server.routes import sessions as facade

    loaded = None
    if agent is not None and getattr(agent, "bundle_location", None) is not None:
        try:
            loaded = await asyncio.to_thread(
                facade.get_agent_cache().load, agent.id, agent.bundle_location
            )
        except Exception:  # noqa: BLE001 (a missing or broken bundle falls back to the labels)
            loaded = None
    if loaded is None:
        held = conv.labels or {}
        if CLAUDE_ACCOUNT_LABEL_KEY not in held and limits.ACCOUNT_LIVE_LABEL not in held:
            raise _invalid("Claude account selection requires a Claude native target.")
        return
    executor = loaded.spec.executor
    if canonicalize_harness(executor.harness_kind) != "claude-native":
        raise _invalid("Claude account selection requires a Claude native target.")
    config = getattr(executor, "config", None)
    envs = (
        getattr(loaded.spec, "env", None),
        getattr(executor, "env", None),
        config.get("env") if isinstance(config, dict) else None,
    )
    if (
        getattr(conv, "inference_snapshot", None) is not None
        or getattr(executor, "auth", None) is not None
        or getattr(executor, "profile", None)
        or any(
            isinstance(env, dict) and any(env.get(key) for key in CLAUDE_PROVIDER_ENV_KEYS)
            for env in envs
        )
    ):
        raise _invalid("Claude account selection conflicts with bound provider authentication.")


async def _host_verdict(
    request: Request, user: str | None, host_id: str | None, account: str
) -> None:
    """Raise unless *user* owns *host_id* and *account* is usable there (one probe at most).

    A ``shared:`` login lives on the server, so it needs no probe. A host login (and the
    default one) is read from the host's detected credentials, which needs the host online.
    """
    from omnigent.server.auth import RESERVED_USER_LOCAL
    from omnigent.server.routes.hosts import _proxy_detect_credentials

    if host_id is None:
        raise _invalid("Claude account selection requires a local connected host.")
    host_store = getattr(request.app.state, "host_store", None)
    host = (
        await asyncio.to_thread(host_store.get_host, host_id) if host_store is not None else None
    )
    if host is None or host.sandbox_provider is not None:
        raise _invalid("Claude account selection requires a local registered host.")
    if host.user_id != (user or RESERVED_USER_LOCAL):
        raise _invalid(
            "Claude account selection requires ownership of the session's host.",
            ErrorCode.FORBIDDEN,
        )
    if known_on_server(account):
        return
    registry = getattr(request.app.state, "host_registry", None)
    host_conn = registry.get(host_id) if registry is not None else None
    if host_conn is None:
        name = getattr(host, "name", None) or host_id
        raise _invalid(
            f"{account or 'The default login'} is a login on the host {name}, which is offline, "
            "so it cannot be checked.",
            ErrorCode.CONFLICT,
        )
    detected = await _proxy_detect_credentials(host_registry=registry, host_conn=host_conn)
    credentials = detected.get("credentials", [])
    if any(item.get("family") in _PROVIDER_FAMILIES for item in credentials):
        raise _invalid("Claude account selection conflicts with host provider authentication.")
    available = {
        item.get("env_var") or ""
        for item in credentials
        if item.get("family") in _ACCOUNT_FAMILIES
    }
    if account not in available:
        raise _invalid("Selected Claude account is unavailable on this host.")


async def validate_stored_claude_account(
    request: Request,
    *,
    account: str,
    agent: Any,
    conv: Any,
    user: str | None,
    host_checks: dict[tuple[str | None, str], asyncio.Future[None]] | None = None,
) -> None:
    """The checks for writing a login onto a chat with no running terminal (2026-11-13).

    The fork check needs a connected host, a live credential probe and a loadable agent for
    every chat, so closed and archived chats on an offline host stayed on the old login. This
    keeps the owner and host-owner checks, and probes the host once per ``(host, account)``
    through *host_checks* (a shared login needs no probe). Raises like the fork check.
    """
    await _stored_claude_check(agent, conv)
    if host_checks is None:
        await _host_verdict(request, user, conv.host_id, account)
        return
    key = (conv.host_id, account)
    check = host_checks.get(key)
    if check is None:
        check = host_checks[key] = asyncio.ensure_future(
            _host_verdict(request, user, conv.host_id, account)
        )
    await asyncio.shield(check)


async def switch(
    request: Request,
    user: str | None,
    conv: Any,
    account: str,
    *,
    force: bool = False,
    hold_busy: bool = False,
    claim: int | None = None,
    host_checks: dict[tuple[str | None, str], asyncio.Future[None]] | None = None,
) -> dict[str, Any]:
    """Move *conv* to *account*; the answer is the route's body (``pending`` when unanswered).

    *force* skips the "never stamped, stored pick equals it" shortcut: a busy chat has its pick
    stored already, yet its running terminal may still be on another login. *hold_busy* keeps
    the new login stored when the runner says the chat is busy (still raising :class:`Refused`),
    inside the lock, so the move-all can retry. *claim* is the number :func:`_claim` gave this
    request: a request that is no longer the latest for the chat answers ``"superseded"`` and
    writes nothing. *host_checks* shares one host probe across a move-all.

    :raises OmnigentError: The checks refused the login for this chat.
    :raises Refused: The runner would not restart the terminal; nothing changed (but the
        stored login, with *hold_busy* on a busy chat).
    """
    from omnigent.server.routes import sessions as facade
    from omnigent.server.routes.sessions.ae_fork_claude_account import (
        validate_fork_claude_account,
    )
    from omnigent.server.routes.sessions.routes_events import get_server_runner_router

    lock = _locks.get(conv.id)
    if lock is None:
        lock = _locks[conv.id] = asyncio.Lock()
    async with lock:
        if not _claim_is_latest(conv.id, claim):
            return {"account": account, "relaunched": False, "superseded": True}
        # Fresh under the lock: a switch queued behind another sees its label.
        fresh = await asyncio.to_thread(get_conversation_store().get_conversation, conv.id)
        conv = fresh or conv
        agent = await asyncio.to_thread(get_agent_store().get, conv.agent_id)
        runner_router = get_server_runner_router()
        bound = await facade._get_runner_client(conv.id, runner_router) is not None
        if bound:
            await validate_fork_claude_account(
                request,
                account=account,
                agent=agent,
                source=conv,
                user_id=user,
                host_type="external",
            )
        else:
            await validate_stored_claude_account(
                request,
                account=account,
                agent=agent,
                conv=conv,
                user=user,
                host_checks=host_checks,
            )
        labels = conv.labels or {}
        old = labels.get(CLAUDE_ACCOUNT_LABEL_KEY)
        old_live = labels.get(limits.ACCOUNT_LIVE_LABEL)
        if old != account:
            await asyncio.to_thread(
                get_conversation_store().set_labels, conv.id, {CLAUDE_ACCOUNT_LABEL_KEY: account}
            )
        if old_live == account and old in (None, account):
            # The terminal last launched on it and no other login is stored: nothing to restart.
            return {"account": account, "relaunched": False}
        if not bound:
            # No terminal is running: the next launch reads the label (``queued`` tells the web).
            # The live label follows it, so a closed or archived chat shows the login it will
            # start on and a repeat of this switch finds nothing left to do.
            if old_live != account:
                await asyncio.to_thread(
                    get_conversation_store().set_labels,
                    conv.id,
                    {limits.ACCOUNT_LIVE_LABEL: account},
                )
            return {"account": account, "relaunched": False, **_QUEUED}
        if old_live is None and old == account and not force:
            # Never stamped: a launch reads the stored pick, so a running terminal is on it.
            return {"account": account, "relaunched": False}
        result = await facade._forward_session_change_to_runner(
            conv.id, runner_router, {"type": CHANGE_EVENT}, timeout_s=FORWARD_TIMEOUT_S
        )
        if result is None:
            # The restart may have happened; the label stays and the web re-reads.
            return {"account": account, "relaunched": False, "pending": True}
        if not 200 <= result.status_code < 300:
            refusal = _refusal(result.status_code, result.body)
            if not (hold_busy and refusal.code == BUSY_CODE):
                await _restore(conv.id, old, old_live)
            raise refusal
        # The runner relaunched on the stored pick; stamp it here too, since its own stamp
        # is best effort and a missed one would leave the web showing the old login.
        await asyncio.to_thread(
            get_conversation_store().set_labels, conv.id, {limits.ACCOUNT_LIVE_LABEL: account}
        )
        return {"account": account, "relaunched": True}


def settled(labels: dict[str, str] | None, account: str) -> bool:
    """Whether the chat runs on *account* with no other login stored: a switch changes nothing."""
    stored = (labels or {}).get(CLAUDE_ACCOUNT_LABEL_KEY)
    return current_account(labels) == account and stored in (None, account)


async def _may_write(user: str | None, session_id: str) -> bool:
    from ae_omni_policies import router as ae_router

    return await ae_router._may_write_session(user, session_id)


@router.put(
    "/sessions/{session_id}/claude-account",
    response_model=None,
    dependencies=[Depends(require_json_content_type), Depends(require_trusted_origin)],
)
async def put_claude_account(
    request: Request,
    session_id: _SESSION_PATH,
    body: SwitchBody,
    user: Annotated[str | None, Depends(require_ae_user)],
) -> dict[str, Any] | JSONResponse:
    """Switch the chat's Claude login and restart its terminal on it.

    Owner only (an admin counts). 200 ``{"account", "relaunched"}`` (plus
    ``"queued": true`` when no terminal is running to restart); 202 with
    ``"pending": true`` when the runner did not answer in time; 409 when a reply
    or approval is in progress; 400, 403 or 409 from the fork checks when the
    chat or host cannot use the login.

    With ``include_children`` the answer also holds ``children``
    (``{"moved", "skipped", "pending", "queued"}``, each a list of ``{id, title}`` rows,
    the last three with a ``reason``; a queued child had no terminal running and starts
    on the login at its next message; ``error`` is added when the cascade itself failed and
    no child was attempted). It runs only after the chat's own switch went
    through or is pending; a refused chat leaves its children alone. The status is
    202 only when the chat itself is pending.
    """
    from ae_omni_policies import router as ae_router

    conv = await ae_router._conversation(session_id)
    if conv is None or conv.agent_id is None:
        return ae_router._not_found(f"No session {session_id}.")
    if not await _may_write(user, session_id):
        return ae_router._error(
            403,
            ErrorCode.FORBIDDEN,
            "Only the chat's owner can switch its Claude login.",
        )
    try:
        answer = await switch(
            request, user, conv, body.account, claim=_claim(conv.id, body.account)
        )
    except Refused as refused:
        raise OmnigentError(
            refused.detail, code=_RUNNER_CODES.get(refused.status, ErrorCode.RUNNER_UNAVAILABLE)
        ) from refused
    if body.include_children:
        try:
            answer["children"] = await _switch_children(request, user, conv, body.account)
        except Exception:  # noqa: BLE001 (the chat already switched; its answer must survive)
            _logger.warning(
                "claude-account: child cascade failed for %s", session_id, exc_info=True
            )
            answer["children"] = {
                "moved": [],
                "skipped": [],
                "pending": [],
                "queued": [],
                "error": "children not attempted",
            }
    if answer.get("pending"):
        return JSONResponse(status_code=202, content=answer)
    return answer


def _session_owner(store: Any, session_id: str) -> str | None:
    """Who owns *session_id*: ``local`` only for a store with no owners at all (single user).

    ``None`` when the lookup fails or finds no owner, which excludes the chat.
    """
    getter = getattr(store, "get_session_owner", None)
    if not callable(getter):
        return LOCAL_OWNER
    try:
        owner = getter(session_id, owner_only=True)
    except Exception:  # noqa: BLE001 (an unreadable owner must exclude the chat)
        _logger.warning("claude-account: owner lookup failed for %s", session_id, exc_info=True)
        return None
    return owner if isinstance(owner, str) and owner else None


def _descendants(root: str, rows: list[Any], owner_of: Callable[[str], str | None]) -> list[Any]:
    """The family below *root*: breadth first over ``ae.orch``, nearest first, loop-safe.

    Stops :data:`sessions.FAMILY_DEPTH` levels down. Every row must share the root's
    owner (``ae.orch`` is not reserved, so a session can name any lead), and the
    root itself is never listed.
    """
    owner = owner_of(root)
    if owner is None:
        return []
    seen = {root}
    found: list[Any] = []
    level = [root]
    for _ in range(sessions.FAMILY_DEPTH):
        nxt: list[str] = []
        for parent_id in level:
            for child in sessions.children_of(rows, parent_id):
                if child.id in seen or owner_of(child.id) != owner:
                    continue
                seen.add(child.id)
                found.append(child)
                nxt.append(child.id)
        if not nxt:
            break
        level = nxt
    return found


async def _switch_children(
    request: Request, user: str | None, parent: Any, account: str
) -> dict[str, list[dict[str, str]]]:
    """Move the parent's family children to *account*; the route's ``children`` answer."""
    from ae_omni_policies import router as ae_router

    rows, truncated = await asyncio.to_thread(
        sessions.list_every_top_level,
        accessible_by=ae_router._acl_user(user),
        include_archived=True,
    )
    if truncated:
        _logger.warning("claude-account: family walk for %s cut at the row cap", parent.id)
    store = get_conversation_store()

    def owner_of(session_id: str) -> str | None:
        return _session_owner(store, session_id)

    family = await asyncio.to_thread(_descendants, parent.id, rows, owner_of)
    pinned = await _pinned_run_ids(request)
    todo = []
    pinned_rows: list[dict[str, str]] = []
    for child in family:
        if child.agent_id is None or settled(child.labels, account):
            continue
        if not await _may_write(user, child.id):
            continue
        if child.id in pinned:
            pinned_rows.append(_row(child, PINNED_REASON))
        else:
            todo.append(child)
    moved, skipped, pending, queued = await _move_many(request, user, todo, account)
    return {
        "moved": [{"id": c.id, "title": c.title or c.id} for c in moved],
        "skipped": pinned_rows + skipped,
        "pending": pending,
        "queued": queued,
    }


def _row(conv: Any, reason: str) -> dict[str, str]:
    return {"id": conv.id, "title": conv.title or conv.id, "reason": reason}


async def _pinned_run_ids(request: Request) -> set[str]:
    """The sessions that scheduled tasks pinned to a Claude login created (2026-11-13).

    The fire path stamps the pin on each run session as its login, so a move-all would
    overwrite it (the weekly rulebook sync must stay on the main account). The runs table
    names those sessions, old ones included, so no marker label is needed. A server with no
    scheduled tasks has none.
    """
    scheduler = getattr(request.app.state, "scheduled_task_scheduler", None)
    task_store = getattr(scheduler, "_store", None)
    if task_store is None:
        return set()
    return await asyncio.to_thread(_pinned_run_ids_sync, task_store)


def _pinned_run_ids_sync(task_store: Any) -> set[str]:
    from omnigent.server.scheduled.task_account import accounts_for

    from ae_omni_policies.retention import run_conversation_ids

    tasks = task_store.list()
    pins = accounts_for(task_store, [task.id for task in tasks])
    found: set[str] = set()
    for task_id, pin in pins.items():
        if pin is not None:
            found.update(run_conversation_ids(task_store, task_id))
    return found


@router.post(
    "/claude-account/move-all",
    response_model=None,
    dependencies=[Depends(require_json_content_type), Depends(require_trusted_origin)],
)
async def move_all_claude_chats(
    request: Request,
    body: MoveAllBody,
    user: Annotated[str | None, Depends(require_ae_user)],
) -> dict[str, Any]:
    """Move every Claude chat the caller may write to the login, one after another.

    Open, closed and archived chats all move, however many there are (the list is paged to
    the end; past a hard cap the answer carries ``"truncated": true``). Chats already on the
    login are left alone; a chat the checks refuse, or the run of a scheduled task pinned to a
    login, is listed in ``skipped`` with its reason. A chat in
    the middle of a reply or waiting on an approval is listed in ``pending``: its login is
    stored for its next launch and a background retry restarts it once the reply ends
    (:data:`PENDING_REASON`). A chat not finished within :data:`MOVE_ALL_CAP_S` is also
    ``pending`` (:data:`STILL_SWITCHING_REASON`) and keeps switching. A chat with no terminal
    running (archived ones too) is listed in ``queued``: its login is stored and it starts on
    it at its next message. ``host_id`` limits a host login to that host's chats; a ``shared:``
    login is on the server for every host, so it moves them all.
    Chats that have no Claude login to move (other harnesses, provider
    credentials) are not listed at all.
    """
    from ae_omni_policies import router as ae_router

    rows, truncated = await asyncio.to_thread(
        sessions.list_every_top_level,
        accessible_by=ae_router._acl_user(user),
        include_archived=True,
    )
    one_host = body.host_id if body.host_id and not known_on_server(body.account) else None
    pinned = await _pinned_run_ids(request)
    todo = []
    pinned_rows: list[dict[str, str]] = []
    for conv in rows:
        if conv.agent_id is None or (one_host and conv.host_id != one_host):
            continue
        if settled(conv.labels, body.account):
            continue
        if not await _may_write(user, conv.id):
            continue
        if conv.id in pinned:
            pinned_rows.append(_row(conv, PINNED_REASON))
        else:
            todo.append(conv)
    moved, skipped, pending, queued = await _move_many(request, user, todo, body.account)
    answer: dict[str, Any] = {
        "moved": [conv.id for conv in moved],
        "skipped": pinned_rows + skipped,
        "pending": pending,
        "queued": queued,
    }
    if truncated:
        answer["truncated"] = True
    return answer


async def _move_many(
    request: Request, user: str | None, convs: list[Any], account: str
) -> tuple[list[Any], list[dict[str, str]], list[dict[str, str]], list[dict[str, str]]]:
    """Switch *convs* a few at a time: ``(moved, skipped rows, still-pending rows, queued rows)``.

    A queued chat had no terminal running: its login is stored and it starts on it next time.

    Answers after :data:`MOVE_ALL_CAP_S`; a chat not done by then is listed pending and
    finishes alone. Chats with no Claude login to move are left out of all three.
    """
    gate = asyncio.Semaphore(MOVE_ALL_CONCURRENCY)
    # One host probe per (host, account) for the whole move, however many chats share a host.
    host_checks: dict[tuple[str | None, str], asyncio.Future[None]] = {}

    # Claim every chat up front, so a pick made while this move is still running wins over it.
    claims = {conv.id: _claim(conv.id, account) for conv in convs}

    async def one(conv: Any) -> tuple[str, dict[str, str] | None]:
        async with gate:
            return await _move_one(request, user, conv, account, claims[conv.id], host_checks)

    tasks = {conv.id: asyncio.create_task(one(conv)) for conv in convs}
    _background.update(tasks.values())
    for task in tasks.values():
        task.add_done_callback(_background.discard)
    if tasks:
        await asyncio.wait(tasks.values(), timeout=MOVE_ALL_CAP_S)
    moved: list[Any] = []
    skipped: list[dict[str, str]] = []
    pending: list[dict[str, str]] = []
    queued: list[dict[str, str]] = []
    for conv in convs:
        task = tasks[conv.id]
        if not task.done():
            # Left running (a cancelled switch could strand its label); the chat finishes alone.
            pending.append(_row(conv, STILL_SWITCHING_REASON))
            continue
        outcome, row = task.result()
        if outcome == "moved":
            moved.append(conv)
        elif outcome == "queued" and row is not None:
            queued.append(row)
        elif outcome == "pending" and row is not None:
            pending.append(row)
        elif row is not None:
            skipped.append(row)
    return moved, skipped, pending, queued


async def _move_one(
    request: Request,
    user: str | None,
    conv: Any,
    account: str,
    claim: int,
    host_checks: dict[tuple[str | None, str], asyncio.Future[None]] | None = None,
) -> tuple[str, dict[str, str] | None]:
    """One chat of a move-all: ``("moved"|"queued"|"pending"|"skipped"|"ignored", its row)``."""
    # Access may have been revoked since the list was built; check fresh, just before the write.
    if not await _may_write(user, conv.id):
        return "ignored", None
    try:
        answer = await switch(
            request,
            user,
            conv,
            account,
            hold_busy=True,
            claim=claim,
            host_checks=host_checks,
        )
    except Refused as refused:
        if refused.code == BUSY_CODE:
            # switch kept the new login stored (inside the chat's lock); retry the restart.
            _schedule_retry(request, user, conv, account, claim)
            return "pending", _row(conv, PENDING_REASON)
        return "skipped", _row(conv, refused.detail)
    except OmnigentError as exc:
        if any(part in str(exc) for part in _NOT_A_LOGIN_CHAT):
            return "ignored", None
        return "skipped", _row(conv, str(exc))
    except Exception:  # noqa: BLE001 (one chat's failure must not stop the rest)
        _logger.warning("move-all: could not switch %s", conv.id, exc_info=True)
        return "skipped", _row(conv, "could not be switched")
    if answer.get("superseded"):
        return "ignored", None
    if answer.get("queued"):
        return "queued", _row(conv, QUEUED_REASON)
    if answer.get("pending"):
        # The runner did not answer: the restart may have happened. Keep asking until it does.
        _schedule_retry(request, user, conv, account, claim)
        return "pending", _row(conv, STILL_SWITCHING_REASON)
    return "moved", None


def _schedule_retry(
    request: Request, user: str | None, conv: Any, account: str, claim: int
) -> None:
    """Start the one background retry for *conv*, replacing an earlier one.

    A request that a newer one already superseded gets none.
    """
    if not _claim_is_latest(conv.id, claim):
        return
    earlier = _retries.pop(conv.id, None)
    if earlier is not None:
        earlier.cancel()
    task = asyncio.create_task(_retry_busy(request, user, conv, account, claim))
    _retries[conv.id] = task
    _background.add(task)

    def done(finished: asyncio.Task[Any]) -> None:
        _background.discard(finished)
        if _retries.get(conv.id) is finished:
            del _retries[conv.id]

    task.add_done_callback(done)


def _permanent(refusal: Refused) -> bool:
    """A runner refusal a retry cannot cure: a client error other than the busy 409."""
    return 400 <= refusal.status < 500 and refusal.status != 409 and refusal.code != BUSY_CODE


async def _retry_busy(
    request: Request, user: str | None, conv: Any, account: str, claim: int
) -> None:
    """Retry the restart every :data:`RETRY_EVERY_S` until :data:`RETRY_FOR_S` has passed.

    A busy chat, an unanswered runner (``pending``) and a transient failure all keep
    retrying. It stops on a confirmed relaunch (or a chat with no terminal left), when a newer
    request for the chat took over (a later "This chat" switch wins), when the chat is gone or
    the caller lost write access, and on a refusal a retry cannot cure.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + RETRY_FOR_S
    while loop.time() < deadline:
        await asyncio.sleep(RETRY_EVERY_S)
        try:
            fresh = await asyncio.to_thread(get_conversation_store().get_conversation, conv.id)
            if fresh is None:
                _logger.info("claude-account: retry for %s ends, the chat is gone", conv.id)
                return
            if not _claim_is_latest(conv.id, claim):
                return
            if not await _may_write(user, conv.id):
                _logger.info("claude-account: retry for %s ends, write access lost", conv.id)
                return
            answer = await switch(
                request, user, fresh, account, force=True, hold_busy=True, claim=claim
            )
        except Refused as refused:
            if _permanent(refused):
                _logger.info("claude-account: retry for %s gave up: %s", conv.id, refused)
                return
        except OmnigentError as exc:
            if exc.code not in (ErrorCode.CONFLICT, ErrorCode.RUNNER_UNAVAILABLE):
                _logger.info("claude-account: retry for %s gave up: %s", conv.id, exc)
                return
        except Exception:  # noqa: BLE001 (a transient failure keeps the retry going)
            _logger.warning("claude-account: retry for %s failed", conv.id, exc_info=True)
        else:
            if answer.get("superseded") or not answer.get("pending"):
                return
    _logger.info("claude-account: retry for %s gave up after %.0f s", conv.id, RETRY_FOR_S)
