"""Switch a Claude chat to another login in place, or every open chat at once.

Added 2026-10-27. A chat stays on the login it started with (the runner's
launch keeps the last login), so moving it is a deliberate act: this module
stores the new login on the session, then asks the chat's runner to restart its
Claude terminal, which resumes the same conversation on the new subscription.

======================================================  ====================================
Route                                                   Answers
======================================================  ====================================
``PUT  /sessions/{id}/claude-account``                  ``{"account", "relaunched"}``
``POST /claude-account/move-all``                       ``{"moved", "skipped", "pending"}``
======================================================  ====================================

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

Runner answers: 2xx is ``relaunched``; a refusal (a reply or approval in
progress, a terminal that would not start) puts the old label back and passes
through; no runner bound keeps the label (the next launch uses it); no answer
within the budget is a 202, because the restart may have happened.
"""

from __future__ import annotations

import asyncio
import json
import logging
import weakref
from collections.abc import Callable
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Path, Request
from fastapi.responses import JSONResponse
from omnigent.errors import ErrorCode, OmnigentError
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
BUSY_REASON = "mid-reply"
# The fork check's refusals for a chat that has no Claude login to move.
_NOT_A_LOGIN_CHAT = ("Claude native target", "provider authentication")

# Move-all switches this many chats at once, and answers after this long; the rest finish alone.
MOVE_ALL_CONCURRENCY = 4
MOVE_ALL_CAP_S = 120.0
_background: set[asyncio.Task[Any]] = set()
# The owner a store without per-user sessions (single user, no auth) reports.
LOCAL_OWNER = "local"

_SESSION_PATH = Annotated[str, Path(min_length=1, max_length=128)]
_locks: weakref.WeakValueDictionary[str, asyncio.Lock] = weakref.WeakValueDictionary()
_RUNNER_CODES = {400: ErrorCode.INVALID_INPUT, 409: ErrorCode.CONFLICT}


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


async def switch(request: Request, user: str | None, conv: Any, account: str) -> dict[str, Any]:
    """Move *conv* to *account*; the answer is the route's body (``pending`` when unanswered).

    :raises OmnigentError: The fork checks refused the login for this chat.
    :raises Refused: The runner would not restart the terminal; nothing changed.
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
        # Fresh under the lock: a switch queued behind another sees its label.
        fresh = await asyncio.to_thread(get_conversation_store().get_conversation, conv.id)
        conv = fresh or conv
        agent = await asyncio.to_thread(get_agent_store().get, conv.agent_id)
        await validate_fork_claude_account(
            request,
            account=account,
            agent=agent,
            source=conv,
            user_id=user,
            host_type="external",
        )
        labels = conv.labels or {}
        old = labels.get(CLAUDE_ACCOUNT_LABEL_KEY)
        old_live = labels.get(limits.ACCOUNT_LIVE_LABEL)
        if old == account and current_account(labels) == account:
            return {"account": account, "relaunched": False}
        await asyncio.to_thread(
            get_conversation_store().set_labels, conv.id, {CLAUDE_ACCOUNT_LABEL_KEY: account}
        )
        if current_account(labels) == account:
            # The terminal already runs on it; the label above just locks the chat there.
            return {"account": account, "relaunched": False}
        runner_router = get_server_runner_router()
        if await facade._get_runner_client(conv.id, runner_router) is None:
            return {"account": account, "relaunched": False}
        result = await facade._forward_session_change_to_runner(
            conv.id, runner_router, {"type": CHANGE_EVENT}, timeout_s=FORWARD_TIMEOUT_S
        )
        if result is None:
            # The restart may have happened; the label stays and the web re-reads.
            return {"account": account, "relaunched": False, "pending": True}
        if not 200 <= result.status_code < 300:
            await _restore(conv.id, old, old_live)
            raise _refusal(result.status_code, result.body)
        return {"account": account, "relaunched": True}


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

    Owner only (an admin counts). 200 ``{"account", "relaunched"}``; 202 with
    ``"pending": true`` when the runner did not answer in time; 409 when a reply
    or approval is in progress; 400, 403 or 409 from the fork checks when the
    chat or host cannot use the login.

    With ``include_children`` the answer also holds ``children``
    (``{"moved", "skipped", "pending"}``, each a list of ``{id, title}`` rows, the
    last two with a ``reason``; ``error`` is added when the cascade itself failed and
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
        answer = await switch(request, user, conv, body.account)
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

    rows = await asyncio.to_thread(
        sessions.list_top_level, accessible_by=ae_router._acl_user(user)
    )
    store = get_conversation_store()

    def owner_of(session_id: str) -> str | None:
        return _session_owner(store, session_id)

    family = await asyncio.to_thread(_descendants, parent.id, rows, owner_of)
    todo = []
    for child in family:
        if child.agent_id is None or current_account(child.labels) == account:
            continue
        if await _may_write(user, child.id):
            todo.append(child)
    moved, skipped, pending = await _move_many(request, user, todo, account)
    return {
        "moved": [{"id": c.id, "title": c.title or c.id} for c in moved],
        "skipped": skipped,
        "pending": pending,
    }


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
    """Move every open Claude chat the caller owns to the login, one after another.

    Chats already on the login are left alone; a chat in the middle of a reply,
    or that the fork checks refuse, is listed in ``skipped`` with its reason. A
    chat not finished within :data:`MOVE_ALL_CAP_S` is listed in ``pending``
    and keeps switching.
    Chats that have no Claude login to move (other harnesses, provider
    credentials) are not listed at all.
    """
    from ae_omni_policies import router as ae_router

    rows = await asyncio.to_thread(
        sessions.list_top_level, accessible_by=ae_router._acl_user(user)
    )
    todo = []
    for conv in rows:
        if conv.agent_id is None or (body.host_id and conv.host_id != body.host_id):
            continue
        if current_account(conv.labels) == body.account:
            continue
        if not await _may_write(user, conv.id):
            continue
        todo.append(conv)
    moved, skipped, pending = await _move_many(request, user, todo, body.account)
    return {"moved": [conv.id for conv in moved], "skipped": skipped, "pending": pending}


async def _move_many(
    request: Request, user: str | None, convs: list[Any], account: str
) -> tuple[list[Any], list[dict[str, str]], list[dict[str, str]]]:
    """Switch *convs* a few at a time: ``(moved chats, skipped rows, still-pending rows)``.

    Answers after :data:`MOVE_ALL_CAP_S`; a chat not done by then is listed pending and
    finishes alone. Chats with no Claude login to move are left out of all three.
    """
    gate = asyncio.Semaphore(MOVE_ALL_CONCURRENCY)

    async def one(conv: Any) -> tuple[str, dict[str, str] | None]:
        async with gate:
            return await _move_one(request, user, conv, account)

    tasks = {conv.id: asyncio.create_task(one(conv)) for conv in convs}
    _background.update(tasks.values())
    for task in tasks.values():
        task.add_done_callback(_background.discard)
    if tasks:
        await asyncio.wait(tasks.values(), timeout=MOVE_ALL_CAP_S)
    moved: list[Any] = []
    skipped: list[dict[str, str]] = []
    pending: list[dict[str, str]] = []
    for conv in convs:
        task = tasks[conv.id]
        if not task.done():
            # Left running (a cancelled switch could strand its label); the chat finishes alone.
            pending.append(
                {"id": conv.id, "title": conv.title or conv.id, "reason": "still switching"}
            )
            continue
        outcome, row = task.result()
        if outcome == "moved":
            moved.append(conv)
        elif row is not None:
            skipped.append(row)
    return moved, skipped, pending


async def _move_one(
    request: Request, user: str | None, conv: Any, account: str
) -> tuple[str, dict[str, str] | None]:
    """One chat of a move-all: ``("moved"|"skipped"|"ignored", the skipped row)``."""

    def row(reason: str) -> dict[str, str]:
        return {"id": conv.id, "title": conv.title or conv.id, "reason": reason}

    # Access may have been revoked since the list was built; check fresh, just before the write.
    if not await _may_write(user, conv.id):
        return "ignored", None
    try:
        await switch(request, user, conv, account)
    except Refused as refused:
        return "skipped", row(BUSY_REASON if refused.code == BUSY_CODE else refused.detail)
    except OmnigentError as exc:
        if any(part in str(exc) for part in _NOT_A_LOGIN_CHAT):
            return "ignored", None
        return "skipped", row(str(exc))
    except Exception:  # noqa: BLE001 (one chat's failure must not stop the rest)
        _logger.warning("move-all: could not switch %s", conv.id, exc_info=True)
        return "skipped", row("could not be switched")
    return "moved", None
