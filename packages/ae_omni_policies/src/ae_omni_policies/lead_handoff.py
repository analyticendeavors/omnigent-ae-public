"""Suggest compacting a large top-level chat, and hand one off when the user asks (2026-10-29).

A token audit found lead chats were 28% of the box's Claude spend: eight grew past 300k tokens
of context (one to 863k) and re-read all of it every turn, and a chat on a 1M-context model does
not compact before about 1M. This module is the server half of the fix:

- a 60 second sweep (:class:`Sweeper`, the shape of :mod:`ae_omni_policies.preset_runs`) that
  labels a lead whose context passed the owner's threshold ``ae.handoff=suggest``, which the web
  shows as a Compact card in the action dock and the rail. It posts nothing into the chat;
- the steps that finish a handoff once the new lead exists (:func:`finish`), used by the route
  ``POST /v1/ae/sessions/{id}/handoff`` and by the sweep's repair. That route and the
  ``hand_off`` tool stay for when the user asks for a new chat; nothing suggests one.

**Which chats.** A lead is a top-level Claude chat the owner opened: no parent, no fork, no
side chat,
``ae.role`` absent or ``lead``, ``ae.orch`` absent or the unknown marker (a family child names a
real lead there), a native Claude wrapper (scheduled runs and check-ins run on other agents),
status ``working`` or ``blocked``. The threshold is ``lead_handoff_tokens`` in the owner's
roles (:mod:`ae_omni_policies.role_prefs`): 300000 by default, 0 is off.

**One label.** The size is the label ``omnigent.last_context_tokens``, used only when it was
written in the last :data:`FRESH_S` seconds. :func:`decide` says what to write: ``suggest``
sets ``ae.handoff=suggest`` and ``ae.handoff_at=<tokens>`` (a busy chat is labelled too; there
is no note to wait for), ``clear`` wipes both when the context fell back under the threshold,
the threshold is 0 or the chat is no longer a lead. ``ae.handoff=snoozed`` ("Not now", and
Compact) waits for :data:`SNOOZE_GROWTH` more tokens past ``ae.handoff_at``. The value
``nudged``, written by the earlier note-posting version, is rewritten to ``suggest``.

**The handoff has a commit point.** The route creates the new lead first, then writes
``ae.handoff_to`` on the old one. Everything after that is idempotent and keyed off the label:
the old lead's children get ``ae.orch`` re-pointed, the check-in rows naming the old id are
rewritten, and the old lead goes to ``reference``. A label write is atomic per session only, so
a crash between those writes is expected; the sweep finishes any lead that carries
``ae.handoff_to`` and still has children pointing at it or a status other than ``reference``.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
import time
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

import httpx
from fastapi import APIRouter, FastAPI

from ae_omni_policies import labels, merge_watch, role_prefs, schedules, sessions

_logger = logging.getLogger(__name__)

ENABLED_ENV = "AE_LEAD_HANDOFF"
SWEEP_INTERVAL_S = 60.0
#: The context label counts only when written this recently (seconds).
FRESH_S = 30 * 60
#: A snoozed lead is suggested again once its context grew this much past its snoozed size.
SNOOZE_GROWTH = 100_000
#: Seconds the sweep waits for a lead's lock before it skips that lead until the next sweep.
LOCK_WAIT_S = 2.0
CONTEXT_LABEL = "omnigent.last_context_tokens"
SIDE_CHAT_LABEL = "omnigent.side_chat"
FORK_SOURCE_LABEL = "omnigent.fork.source_id"
WRAPPER_LABEL = "omnigent.wrapper"
CLAUDE_WRAPPER = "claude-code-native-ui"
#: A handoff packet longer than this is refused (the brief limit of ``POST /v1/ae/sessions``).
PACKET_MAX = 100_000
_LOOPBACK_TIMEOUT_S = 30.0


_locks: dict[tuple[int, str], asyncio.Lock] = {}


def lead_lock(lead_id: str) -> asyncio.Lock:
    """The one lock per lead that the handoff, the snooze route and the sweep all take."""
    return _locks.setdefault((id(asyncio.get_running_loop()), lead_id), asyncio.Lock())


def enabled(environ: Mapping[str, str] | None = None) -> bool:
    """Whether the sweep runs: on unless ``AE_LEAD_HANDOFF=0``."""
    env = os.environ if environ is None else environ
    return (env.get(ENABLED_ENV) or "1").strip() != "0"


def strip_long_context(model: str | None) -> str | None:
    """*model* without a ``[1m]`` suffix: the new lead starts on the plain model.

    A fresh chat has nothing to compact, and the suffix is what let the old one grow to 1M.
    """
    if not model:
        return model
    stripped = re.sub(r"\[1m\]$", "", model.strip(), flags=re.IGNORECASE)
    return stripped or None


def context_tokens(conv: Any) -> int | None:
    """The size of the lead's context in tokens, or ``None`` when the label is missing or bad."""
    raw = labels.label(conv.labels, CONTEXT_LABEL)
    if raw is None:
        return None
    try:
        value = int(float(raw))
    except ValueError:
        return None
    return value if value > 0 else None


def is_lead(conv: Any) -> bool:
    """Whether *conv* may be suggested compacting (the module docstring lists the rules)."""
    held = conv.labels or {}
    if conv.parent_conversation_id is not None or conv.kind != "default" or conv.archived:
        return False
    if held.get(FORK_SOURCE_LABEL) or held.get(SIDE_CHAT_LABEL):
        return False
    if labels.orch_parent(held) is not None:
        return False
    role = labels.label(held, labels.ROLE)
    if role not in (None, "lead"):
        return False
    if labels.label(held, WRAPPER_LABEL) != CLAUDE_WRAPPER:
        return False
    # Only a lead in the middle of work (2026-10-29 review): one in review, parked or reference
    # is not re-reading its context every turn, so a card would only be noise.
    return labels.status_of(held) in (labels.WORKING, labels.BLOCKED)


def decide(conv: Any, tokens: int | None, threshold: int) -> str | None:
    """What to do with *conv*: ``"suggest"``, ``"clear"`` or ``None`` (leave it).

    :param conv: Any top-level row.
    :param tokens: Its fresh context size, ``None`` when unknown or stale.
    :param threshold: The owner's setting; 0 is off.
    """
    mark = labels.label(conv.labels, labels.HANDOFF)
    if threshold <= 0:
        return "clear" if mark is not None else None
    if not is_lead(conv):
        # A chat that left working or blocked loses its card, but keeps a snooze: it comes back
        # to work with the same "Not now" until its context falls under the size.
        if mark is None or mark == labels.HANDOFF_SNOOZED:
            if mark is not None and tokens is not None and tokens < threshold:
                return "clear"
            return None
        return "clear"
    if tokens is None:
        return None
    if tokens < threshold:
        return "clear" if mark is not None else None
    if mark is None or mark == labels.HANDOFF_NUDGED:
        return "suggest"
    if mark == labels.HANDOFF_SNOOZED:
        try:
            at = int(labels.label(conv.labels, labels.HANDOFF_AT) or 0)
        except ValueError:
            at = 0
        return "suggest" if tokens >= at + SNOOZE_GROWTH else None
    return None


def handoff_target(conv: Any) -> str | None:
    """The lead that took over *conv*, or ``None``."""
    return labels.label(conv.labels, labels.HANDOFF_TO)


def children_of(rows: Sequence[Any], lead_id: str) -> list[Any]:
    """The top-level sessions whose ``ae.orch`` names *lead_id*."""
    return [row for row in rows if _bare(labels.orch_parent(row.labels)) == _bare(lead_id)]


def _bare(session_id: str | None) -> str | None:
    """*session_id* without a ``conv_`` prefix, so both spellings of one id compare equal."""
    return session_id.removeprefix("conv_") if session_id else session_id


# ── finishing a handoff ─────────────────────────────────────────────────────

_CHECKIN_PROMPT = re.compile(
    r"^Scheduled orchestrator check-in\.(?: Focus: (?P<note>.*?))?"
    r"(?: Hand it to the orchestrator session| Run the check-in your instructions)"
)


@dataclass
class Finished:
    """What :func:`finish` did, for the route's answer and the log."""

    children: list[str] = field(default_factory=list)
    checkin: str = "none"
    tasks: list[str] = field(default_factory=list)
    status_set: bool = False
    done: bool = False
    #: Tasks that name the old id somewhere other than a ``session_id`` / ``session`` form.
    unchanged_tasks: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "children_moved": self.children,
            "checkin": self.checkin,
            "tasks_rewritten": self.tasks,
            "old_lead_set_to_reference": self.status_set,
            "done": self.done,
            "tasks_naming_the_old_lead_unchanged": self.unchanged_tasks,
            "problems": self.problems,
        }


def rebuilt_checkin(prompt: str, old_id: str, new_id: str) -> str | None:
    """The check-in prompt with *new_id* in place of *old_id*, rebuilt by ``checkin_prompt``.

    ``None`` unless the prompt parses AND rebuilding it with the old id gives it back exactly,
    so a prompt somebody edited by hand is never overwritten with a guess.
    """
    match = _CHECKIN_PROMPT.match(prompt)
    if match is None:
        return None
    note = match.group("note")
    if schedules.checkin_prompt(old_id, note) != prompt:
        return None
    return schedules.checkin_prompt(new_id, note)


async def _rewrite_tasks(http: httpx.AsyncClient, old_id: str, new_id: str, out: Finished) -> None:
    """Point every scheduled task that names *old_id* at *new_id*.

    The server-wide check-in is rebuilt with :func:`schedules.checkin_prompt`; any other task
    (a ``sys_scheduled_task_create`` one) gets ``session_id <old id>`` and ``session <old id>``
    replaced in its prompt, and nothing else.
    """
    try:
        response = await http.get("/v1/scheduled-tasks")
        rows = (
            response.json().get("scheduled_tasks") or [] if response.status_code == 200 else None
        )
    except (httpx.HTTPError, ValueError) as exc:
        out.problems.append(f"could not list the scheduled tasks: {exc.__class__.__name__}")
        return
    if rows is None:
        out.problems.append(f"could not list the scheduled tasks: {response.status_code}")
        return
    for row in rows:
        prompt = row.get("prompt") if isinstance(row, dict) else None
        if not isinstance(prompt, str) or old_id not in prompt:
            continue
        if row.get("name") == schedules.CHECKIN_NAME:
            fresh = rebuilt_checkin(prompt, old_id, new_id)
            if fresh is None:
                out.checkin = "not rewritten"
                out.problems.append(
                    "the check-in prompt does not match the managed shape; run schedule_checkin "
                    f"with session_id {new_id} to repoint it"
                )
                continue
        else:
            # Only the forms a task uses to address the lead; any other mention (a transcript to
            # read, say) stays as written and is listed for the owner.
            fresh = re.sub(rf"\b(session_id|session) {re.escape(old_id)}", rf"\1 {new_id}", prompt)
            if old_id in fresh:
                out.unchanged_tasks.append(str(row.get("name") or row.get("id")))
            if fresh == prompt:
                continue
        try:
            patched = await http.patch(f"/v1/scheduled-tasks/{row['id']}", json={"prompt": fresh})
        except httpx.HTTPError as exc:
            out.problems.append(
                f"could not rewrite task {row.get('id')}: {exc.__class__.__name__}"
            )
            continue
        if patched.status_code != 200:
            out.problems.append(f"could not rewrite task {row.get('id')}: {patched.status_code}")
        elif row.get("name") == schedules.CHECKIN_NAME:
            out.checkin = "rewritten"
        else:
            out.tasks.append(str(row.get("name") or row.get("id")))


async def finish(store: Any, http: httpx.AsyncClient | None, old_id: str, new_id: str) -> Finished:
    """The idempotent steps after the commit point (``ae.handoff_to`` on the old lead).

    Re-points the children, rewrites the scheduled tasks and sets the old lead to
    ``reference``, which frees its slot. Safe to run again: each step only touches what still
    names the old lead. A step that fails is reported in ``problems`` and the rest still run.

    :param http: A client for this server's own API as the lead's owner; ``None`` skips the
        scheduled tasks.
    """
    out = Finished()
    rows = await asyncio.to_thread(sessions.list_top_level, store)
    for child in children_of(rows, old_id):
        try:
            await asyncio.to_thread(store.set_labels, child.id, {labels.ORCH: new_id})
            out.children.append(child.id)
        except Exception as exc:  # noqa: BLE001 (the repair sweep finishes it)
            out.problems.append(f"could not move child {child.id}: {exc.__class__.__name__}")
    if http is not None:
        await _rewrite_tasks(http, old_id, new_id, out)
    else:
        out.problems.append(
            "could not act as the lead's owner, so the scheduled tasks were skipped"
        )
    try:
        await asyncio.to_thread(store.set_labels, old_id, {labels.STATUS: labels.REFERENCE})
        out.status_set = True
    except Exception as exc:  # noqa: BLE001
        out.problems.append(f"could not set the old lead to reference: {exc.__class__.__name__}")
    if out.status_set and not out.problems:
        # The done marker: written only when every step ran clean, and the sweep's repair
        # never looks at the lead again once it is there.
        try:
            await asyncio.to_thread(store.set_labels, old_id, {labels.HANDOFF_DONE: "1"})
            out.done = True
        except Exception as exc:  # noqa: BLE001
            out.problems.append(f"could not mark the handoff done: {exc.__class__.__name__}")
    return out


TARGET_OK = "ok"
TARGET_GONE = "gone"
TARGET_UNKNOWN = "unknown"


async def target_ok(
    get: Callable[[str], Any], same_owner: Callable[[str], Any], new_id: str
) -> str:
    """Whether the lead named by ``ae.handoff_to`` is still a live lead of the same owner.

    Three answers, because undoing a healthy handoff on a failed lookup would leave two live
    leads: :data:`TARGET_OK`, :data:`TARGET_GONE` (read fine and it is missing, archived or
    another user's) and :data:`TARGET_UNKNOWN` (a lookup raised, or an owner could not be
    read). Only ``gone`` may be undone.

    :param get: ``await get(id)`` is the conversation or ``None`` (it raises on a store error).
    :param same_owner: ``await same_owner(id)`` is ``True`` or ``False``, or ``None`` when an
        owner could not be read.
    """
    try:
        conv = await get(new_id)
        if conv is None:
            return TARGET_GONE
        if getattr(conv, "archived", False):
            return TARGET_GONE
        same = await same_owner(new_id)
    except Exception:  # noqa: BLE001 (a failed lookup is not a verdict)
        _logger.info("lead handoff: could not look up %s", new_id, exc_info=True)
        return TARGET_UNKNOWN
    if same is None:
        return TARGET_UNKNOWN
    return TARGET_OK if same else TARGET_GONE


_warned: set[tuple[str, str]] = set()


def warn_once(kind: str, lead_id: str, message: str, *args: Any) -> None:
    """Log *message* at warning the first time for (*kind*, *lead_id*) in a process, then at debug.

    A permanent problem (a hand-edited check-in, no owner client) would otherwise repeat every
    sweep.
    """
    key = (kind, lead_id)
    if key in _warned:
        _logger.debug(message, *args)
        return
    _warned.add(key)
    _logger.warning(message, *args)


def info_once(kind: str, lead_id: str, message: str, *args: Any) -> None:
    """Log *message* at info the first time for (*kind*, *lead_id*) in a process, then at debug."""
    key = (kind, lead_id)
    if key in _warned:
        _logger.debug(message, *args)
        return
    _warned.add(key)
    _logger.info(message, *args)


async def clear_commit(
    store: Any, http: httpx.AsyncClient | None, old_id: str, dead_id: str
) -> Finished:
    """Undo a handoff whose new lead (*dead_id*) is gone, so the family is whole again.

    ``finish`` may already have set the old lead to ``reference`` and re-pointed some of its
    children and tasks. Put back, in this order: the children whose ``ae.orch`` names the dead
    lead, the check-in and the tasks that were rewritten to it, and the old lead's status (the
    one stored in ``ae.handoff_prev_status`` at the commit point, ``working`` when none). Only
    when all of that worked are the marks cleared (``ae.handoff_to``, ``_brief``, ``_done``,
    ``_prev_status``); any problem leaves them so the next try repeats the undo.

    :returns: What was put back; ``cleared`` is true only when the marks were removed.
    """
    out = Finished()
    rows = await asyncio.to_thread(sessions.list_top_level, store)
    for child in children_of(rows, dead_id):
        try:
            await asyncio.to_thread(store.set_labels, child.id, {labels.ORCH: old_id})
            out.children.append(child.id)
        except Exception as exc:  # noqa: BLE001
            out.problems.append(f"could not move child {child.id} back: {exc.__class__.__name__}")
    if http is not None:
        await _rewrite_tasks(http, dead_id, old_id, out)
    else:
        out.problems.append("could not act as the lead's owner, so the scheduled tasks stayed")
    try:
        old = await asyncio.to_thread(store.get_conversation, old_id)
        prev = labels.label(old.labels if old else None, labels.HANDOFF_PREV_STATUS)
        if prev not in labels.STATUS_VALUES or prev == labels.REFERENCE:
            prev = labels.WORKING
        await asyncio.to_thread(store.set_labels, old_id, {labels.STATUS: prev})
        out.status_set = True
    except Exception as exc:  # noqa: BLE001
        out.problems.append(f"could not restore the old lead's status: {exc.__class__.__name__}")
    if not out.problems:
        try:
            await asyncio.to_thread(
                store.set_labels,
                old_id,
                {
                    labels.HANDOFF_TO: "",
                    labels.HANDOFF_BRIEF: "",
                    labels.HANDOFF_DONE: "",
                    labels.HANDOFF_PREV_STATUS: "",
                },
            )
            out.done = True
        except Exception as exc:  # noqa: BLE001
            out.problems.append(f"could not clear the handoff marks: {exc.__class__.__name__}")
    return out


@asynccontextmanager
async def try_lead_lock(lead_id: str, wait: float | None = None) -> AsyncIterator[bool]:
    """The lead's lock if it frees within *wait* seconds; yields whether it was taken.

    The sweep uses this so one slow network call under a lead's lock cannot stall every other
    lead: a busy lead is skipped until the next sweep.
    """
    lock = lead_lock(lead_id)
    try:
        await asyncio.wait_for(lock.acquire(), LOCK_WAIT_S if wait is None else wait)
    except TimeoutError:
        yield False
        return
    try:
        yield True
    finally:
        lock.release()


def needs_repair(conv: Any, rows: Sequence[Any]) -> bool:
    """Whether *conv* carries ``ae.handoff_to`` and no ``ae.handoff_done`` yet.

    *rows* is kept for callers that pass the sweep's rows; the marker alone decides.
    """
    del rows
    return (
        handoff_target(conv) is not None and labels.label(conv.labels, labels.HANDOFF_DONE) is None
    )


# ── the sweep ───────────────────────────────────────────────────────────────


def label_age(store: Any, conv: Any, key: str, *, now: float) -> float | None:
    """Seconds since label *key* of *conv* was last written, or the row's own age.

    The store's rows carry a per-label ``updated_at`` the ``Conversation`` does not expose, so
    it is read from the label table. When that cannot be done (another store implementation)
    the conversation's own ``updated_at`` stands in, and ``None`` means neither is known.
    """
    try:
        from omnigent.db.db_models import (
            SqlConversationLabel,
            current_workspace_id,
        )
        from sqlalchemy import select

        with store._conv_session("lead_handoff_label_age") as session:
            stamp = session.execute(
                select(SqlConversationLabel.updated_at).where(
                    SqlConversationLabel.workspace_id == current_workspace_id(),
                    SqlConversationLabel.conversation_id == conv.id,
                    SqlConversationLabel.key == key,
                )
            ).scalar_one_or_none()
        if stamp is not None:
            return max(0.0, now - float(stamp))
    except Exception:  # noqa: BLE001 (fall back to the row's own age)
        _logger.debug("lead handoff: label age unreadable for %s", conv.id, exc_info=True)
    _logger.warning(
        "lead handoff: no label age for %s on %s; using the conversation's own updated_at",
        conv.id,
        key,
    )
    updated = getattr(conv, "updated_at", None)
    return max(0.0, now - float(updated)) if isinstance(updated, (int, float)) else None


class Sweeper:
    """One sweep per :meth:`sweep`; :meth:`run` repeats it every :data:`SWEEP_INTERVAL_S`.

    :param store: The conversation store.
    :param owner: ``owner(session_id)`` is the session's owner, ``None`` when unknown.
    :param threshold: ``threshold(owner)`` is the owner's setting in tokens (0 is off).
    :param repair: ``await repair(old_id, new_id)`` finishes a half-done handoff.
    """

    def __init__(
        self,
        *,
        store: Any,
        owner: Callable[[str], str | None],
        threshold: Callable[[str], int] | None = None,
        repair: Callable[[str, str], Any] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._store = store
        self._owner = owner
        self._threshold = threshold or (lambda user: role_prefs.store().lead_handoff_tokens(user))
        self._repair = repair
        self._clock = clock

    async def sweep(self) -> dict[str, list[str]]:
        """Run once. Returns the ids acted on: ``suggested``, ``cleared`` and ``repaired``."""
        done: dict[str, list[str]] = {"suggested": [], "cleared": [], "repaired": []}
        rows = await asyncio.to_thread(sessions.list_top_level, self._store)
        now = self._clock()
        for conv in rows:
            try:
                if needs_repair(conv, rows):
                    if self._repair is not None and await self._repair(
                        conv.id, str(handoff_target(conv))
                    ):
                        done["repaired"].append(conv.id)
                    continue
                if handoff_target(conv) is not None:
                    continue
                # A chat that is no lead and carries no mark has nothing to decide.
                if not is_lead(conv) and labels.label(conv.labels, labels.HANDOFF) is None:
                    continue
                await self._one(conv, now, done)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 (one bad row must not stop the sweep)
                _logger.warning("lead handoff: skipped %s", conv.id, exc_info=True)
        return done

    async def _one(self, conv: Any, now: float, done: dict[str, list[str]]) -> None:
        async with try_lead_lock(conv.id) as got:
            if not got:
                return  # busy (a handoff or a click): this lead waits for the next sweep
            # Everything is read again inside the lock, so a handoff, a snooze or a context
            # that fell while the sweep waited cannot be overwritten from a stale reading.
            fresh = await asyncio.to_thread(self._store.get_conversation, conv.id)
            if fresh is None or handoff_target(fresh) is not None:
                return
            owner = await asyncio.to_thread(self._owner, conv.id)
            if not owner:
                # No owner means no threshold to apply; the next sweep looks again.
                info_once("no-owner", conv.id, "lead handoff: no owner for %s, skipped", conv.id)
                return
            threshold = await asyncio.to_thread(self._threshold, owner)
            tokens = context_tokens(fresh)
            if tokens is not None:
                age = await asyncio.to_thread(
                    label_age, self._store, fresh, CONTEXT_LABEL, now=now
                )
                if age is None or age > FRESH_S:
                    tokens = None
            action = decide(fresh, tokens, threshold)
            if action is None:
                return
            if action == "clear":
                await asyncio.to_thread(
                    self._store.set_labels, conv.id, {labels.HANDOFF: "", labels.HANDOFF_AT: ""}
                )
                done["cleared"].append(conv.id)
                return
            await asyncio.to_thread(
                self._store.set_labels,
                conv.id,
                {labels.HANDOFF: labels.HANDOFF_SUGGEST, labels.HANDOFF_AT: str(tokens)},
            )
        done["suggested"].append(conv.id)

    async def run(self) -> None:
        while True:
            try:
                await self.sweep()
            except asyncio.CancelledError:
                raise
            except Exception:
                _logger.exception("lead handoff: sweep failed")
            await asyncio.sleep(SWEEP_INTERVAL_S)


def owner_client(app: FastAPI, headers: dict[str, str]) -> httpx.AsyncClient:
    """A client for this server's own API with *headers*, as ``merge_watch._call`` reaches it."""
    self_url = (os.environ.get("AE_SELF_URL") or "").rstrip("/")
    if self_url:
        return httpx.AsyncClient(base_url=self_url, timeout=_LOOPBACK_TIMEOUT_S, headers=headers)
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://ae-loopback",
        timeout=_LOOPBACK_TIMEOUT_S,
        headers=headers,
    )


def make_repair(
    store: Any,
    app: Any,
    identity: Any,
    client_for: Callable[[Any, dict[str, str]], Any] | None = None,
) -> Callable[[str, str], Any]:
    """The sweep's repair: finish a committed handoff, or undo one whose new lead is truly gone.

    An owner or store lookup that fails is *unknown*, never *gone*: nothing is changed and the
    next sweep looks again, so a transient error cannot undo a working handoff.

    :param identity: Has ``owner(session_id)`` (``None`` on a failed lookup) and
        ``await headers(session_id)`` (``None`` when the server cannot act as the owner).
    :param client_for: ``client_for(app, headers)`` is an async context manager for this
        server's own API; defaults to :func:`owner_client`.
    """
    client_for = client_for or owner_client

    async def repair(old_id: str, new_id: str) -> bool:
        async def get(session_id: str) -> Any:
            return await asyncio.to_thread(store.get_conversation, session_id)

        async def same_owner(session_id: str) -> bool | None:
            mine, theirs = await asyncio.gather(
                asyncio.to_thread(identity.owner, old_id),
                asyncio.to_thread(identity.owner, session_id),
            )
            if mine is None or theirs is None:
                return None  # a failed lookup answers None: unknown, not different
            return mine == theirs

        async with try_lead_lock(old_id) as got:
            if not got:
                return False  # busy: the next sweep tries again
            headers = await identity.headers(old_id)
            async with client_for(app, headers or {}) as http:
                live = http if headers is not None else None
                verdict = await target_ok(get, same_owner, new_id)
                if verdict == TARGET_UNKNOWN:
                    _logger.info(
                        "lead handoff: could not tell whether %s (the new lead of %s) is "
                        "still live; nothing was changed, the next sweep looks again",
                        new_id,
                        old_id,
                    )
                    return False
                if verdict == TARGET_GONE:
                    _logger.warning(
                        "lead handoff: %s names %s, which is gone, archived or another "
                        "user's; the family is put back on %s so a retry starts afresh",
                        old_id,
                        new_id,
                        old_id,
                    )
                    undone = await clear_commit(store, live, old_id, new_id)
                    if undone.problems:
                        warn_once(
                            "undo",
                            old_id,
                            "lead handoff: undoing %s -> %s left %s",
                            old_id,
                            new_id,
                            undone.problems,
                        )
                    return False
                result = await finish(store, live, old_id, new_id)
        if result.problems:
            warn_once("repair", old_id, "lead handoff repair of %s: %s", old_id, result.problems)
        return result.done

    return repair


def start(app: FastAPI) -> asyncio.Task[None] | None:
    """Start the sweep on the running loop, or ``None`` when it is off or cannot start."""
    if not enabled():
        _logger.info("lead handoff off (%s=0)", ENABLED_ENV)
        return None
    try:
        from omnigent.runtime import get_conversation_store

        store = get_conversation_store()
        identity = merge_watch.OwnerIdentity()

        repair = make_repair(store, app, identity)

        sweeper = Sweeper(store=store, owner=identity.owner, repair=repair)
        return asyncio.get_running_loop().create_task(sweeper.run(), name="ae-lead-handoff")
    except Exception:
        _logger.exception("lead handoff: could not start; the sweep is off until a restart")
        return None


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Run the sweep for the life of the server."""
    task = start(app)
    try:
        yield
    finally:
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task


#: No routes; included for its lifespan (see the module docstring).
lifespan_router = APIRouter(lifespan=lifespan)

__all__ = [
    "Finished",
    "Sweeper",
    "children_of",
    "context_tokens",
    "decide",
    "enabled",
    "finish",
    "handoff_target",
    "is_lead",
    "lifespan_router",
    "needs_repair",
    "rebuilt_checkin",
    "strip_long_context",
]
