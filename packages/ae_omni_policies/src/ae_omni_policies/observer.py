"""The observer: the agent writes the gate, the product captures it.

The branch-discipline rules make the agent write a handful of fixed tokens at
the start of a line (``FORK:``, ``Absorbed:``, ``UNFILED``, the ``THREAD:`` /
``NEXT:`` note) and leave every call to the human. This module reads those
lines from the assistant's own persisted turns and turns them into session
labels: ``ae.gate=open`` plus ``ae.fork`` when a gate is written, counts for
``ae.absorbed`` and ``ae.unfiled`` with each line's text in a label of its own
(``ae.absorbed.1`` and on, :func:`labels.note_key`), ``ae.next`` from a handoff note, and
``ae.status`` from a title glyph the agent still flips on legacy surfaces. It
never gates and never writes a glyph back (decided 2026-09-23): the Parking
Lot page renders the open gate from the labels, and its A / B / C / Close
buttons post the reply through ``/v1/ae/gate/{id}/reply``.

Tokens count only at the start of a line, exactly as ae-claude-skills'
``scripts/test-triage-gate.sh`` greps them; a mention inside prose ("no FORK:
needed here") is not a gate.

Two ways in:

- With patch P3 applied, upstream's ``omnigent.server.item_observers``
  registry calls :func:`observe_items` after every persisted batch, on a
  worker thread, for native sessions (the transcript bridge) and relay
  harnesses (the text flush) alike. Registration happens when this module
  imports, which ``policy_modules: [ae_omni_policies]`` does at server start.
- Without P3 (an unpatched upstream), :func:`observe` is a request-phase
  policy that scans the last :data:`FALLBACK_LIMIT` items of the session at
  the human's next prompt, remembering the newest item it has seen in
  ``session_state`` so nothing is counted twice. Icons then move only when
  the human replies, which is why P3 exists.
"""

from __future__ import annotations

import json
import logging
import re
import threading
from collections.abc import Mapping, Sequence
from typing import Any

from omnigent.entities.conversation import ConversationItem, FunctionCallData, MessageData
from omnigent.policies.schema import PolicyEvent, PolicyResponse
from omnigent.runtime import get_conversation_store

from ae_omni_policies import labels, sessions
from ae_omni_policies.slot_cap import STORE_ERRORS

try:
    from omnigent.server.item_observers import register_item_observer
except ImportError:  # an upstream without patch P3
    register_item_observer = None

_logger = logging.getLogger(__name__)

#: Whether upstream carries patch P3; the fallback policy abstains when it does.
HAVE_ITEM_OBSERVERS = register_item_observer is not None

# The tokens, anchored to the start of a line (re.MULTILINE), as the test
# harness greps them: ``grep -q "^FORK:"`` and friends.
FORK_LINE = re.compile(r"^FORK:[ \t]*(.*?)[ \t]*$", re.MULTILINE)
# These two capture the line's text after the token, which becomes the note
# (the Triage card's ``scanAeTriageLines`` strips the same tokens).
ABSORBED_LINE = re.compile(r"^Absorbed:[ \t]*(.*?)[ \t]*$", re.MULTILINE)
UNFILED_LINE = re.compile(r"^UNFILED[ \t:,.-]*(.*?)[ \t]*$", re.MULTILINE)
THREAD_LINE = re.compile(r"^THREAD:", re.MULTILINE)
NEXT_LINE = re.compile(r"^NEXT:[ \t]*(.*?)[ \t]*$", re.MULTILINE)
# ``Title: <glyph> <name>``: the way an agent announces a rename on a surface
# with no title tool. Only a glyph-prefixed title carries a status.
TITLE_LINE = re.compile(r"^Title:[ \t]*(.*?)[ \t]*$", re.MULTILINE)
# The agent-facing rename tool (``omnigent/tools/builtins/session_rename.py``).
RENAME_TOOLS = frozenset({"sys_session_rename"})

# The legacy title glyphs, one per status (``web/src/lib/aeLabels.ts``
# ``AE_LEGACY_GLYPHS`` mirrors this). The reference glyph usually arrives with
# the emoji presentation selector U+FE0F; the check below strips it.
GLYPHS: dict[str, str] = {
    "\U0001f7e1": labels.WORKING,  # 🟡
    "\U0001f534": labels.BLOCKED,  # 🔴
    "✅": labels.REVIEW,  # ✅
    "\U0001f4cc": labels.PARKED,  # 📌
    "\U0001f5c2": labels.REFERENCE,  # 🗂
}
_GLYPH_PREFIX = re.compile("^(" + "|".join(map(re.escape, GLYPHS)) + r")️?(?:\s|$)")

# The registry runs each persisted batch on a worker thread of its own, so two
# batches of one session can overlap; reading the counts and writing the next
# numbers under one lock keeps two lines from taking the same note number.
_LABEL_LOCK = threading.Lock()

# Fallback (no P3): how many of the newest items one request scans, and the
# session_state key that remembers the newest item already scanned.
FALLBACK_LIMIT = 6
SEEN_KEY = "_ae_observed"


def glyph_status(title: str | None) -> str | None:
    """The status a leading legacy glyph stands for, or ``None``.

    :param title: A session title or a ``Title:`` line's value, e.g. ``"🟡 Fix the build"``.
    :returns: One of :data:`labels.STATUS_VALUES`, or ``None`` when the title
        does not start with a glyph.
    """
    if not title:
        return None
    match = _GLYPH_PREFIX.match(title.lstrip())
    return GLYPHS[match.group(1)] if match else None


def message_text(data: MessageData) -> str:
    """Every text block of a message, joined by newlines."""
    parts = [
        block["text"]
        for block in data.content
        if isinstance(block, dict) and isinstance(block.get("text"), str)
    ]
    return "\n".join(parts)


def note_text(raw: str, token: str) -> str:
    """A line's text as its saved note: one line, clipped, the token when it is blank."""
    return labels.clip(raw.strip() or token)


def _count_lines(
    updates: dict[str, str], counter: str, count: int, notes: Sequence[str], token: str
) -> int:
    """Add *notes* to *counter*'s count, saving each under :func:`labels.note_key`."""
    for raw in notes:
        count += 1
        updates[counter] = str(count)
        if count <= labels.NOTE_CAP:
            updates[labels.note_key(counter, count)] = note_text(raw, token)
    return count


def _rename_title(data: FunctionCallData) -> str | None:
    try:
        arguments = json.loads(data.arguments or "{}")
    except ValueError:
        return None
    title = arguments.get("title") if isinstance(arguments, dict) else None
    return title if isinstance(title, str) else None


def updates_for(items: Sequence[ConversationItem], current: Mapping[str, str]) -> dict[str, str]:
    """The label writes a batch of persisted items calls for.

    Pure: reads the items and the session's current labels, returns the
    upsert. Counters continue from their current values, and each counted line
    is saved under its number; the last ``FORK:`` line in the batch wins; a user
    turn while the gate is open answers it.

    :param items: Persisted items, oldest first.
    :param current: The session's labels before the batch.
    :returns: ``{key: value}`` for ``set_labels``; empty when nothing applies.
    """
    updates: dict[str, str] = {}
    absorbed = labels.count(current, labels.ABSORBED)
    unfiled = labels.count(current, labels.UNFILED)
    gate = labels.label(current, labels.GATE)
    for item in items:
        data = item.data
        if item.type == "message" and isinstance(data, MessageData):
            text = message_text(data)
            if data.role == "assistant":
                forks = FORK_LINE.findall(text)
                if forks:
                    gate = labels.GATE_OPEN
                    updates[labels.GATE] = gate
                    updates[labels.FORK] = labels.clip(forks[-1])
                absorbed = _count_lines(
                    updates, labels.ABSORBED, absorbed, ABSORBED_LINE.findall(text), "Absorbed"
                )
                unfiled = _count_lines(
                    updates, labels.UNFILED, unfiled, UNFILED_LINE.findall(text), "UNFILED"
                )
                nexts = NEXT_LINE.findall(text)
                if nexts and THREAD_LINE.search(text):
                    updates[labels.NEXT] = labels.clip(nexts[-1])
                for title in TITLE_LINE.findall(text):
                    status = glyph_status(title)
                    if status:
                        updates[labels.STATUS] = status
            elif not data.is_meta and gate == labels.GATE_OPEN:
                gate = labels.GATE_ANSWERED
                updates[labels.GATE] = gate
        elif isinstance(data, FunctionCallData) and data.name in RENAME_TOOLS:
            status = glyph_status(_rename_title(data))
            if status:
                updates[labels.STATUS] = status
    return updates


def observe_items(session_id: str, items: Sequence[ConversationItem]) -> dict[str, str]:
    """The registered observer (P3): label the session from a persisted batch.

    Runs on a worker thread; store errors propagate to the registry, which
    logs them. A leading glyph on the session's own title maps to
    ``ae.status`` once, only while the session has no status yet.

    :param session_id: The conversation the items belong to.
    :param items: The persisted items, oldest first.
    :returns: The labels written, for tests and logs.
    """
    store = get_conversation_store()
    with _LABEL_LOCK:
        conv = store.get_conversation(session_id)
        if conv is None:
            return {}
        updates = updates_for(items, conv.labels)
        if labels.STATUS not in updates and labels.status_of(conv.labels) is None:
            status = glyph_status(conv.title)
            if status:
                updates[labels.STATUS] = status
        if updates:
            store.set_labels(session_id, updates)
    if updates:
        _logger.debug("observer labelled session=%s %s", session_id, updates)
    return updates


def observe(event: PolicyEvent) -> PolicyResponse | None:
    """Request-phase fallback for an upstream without patch P3.

    Scans the newest :data:`FALLBACK_LIMIT` items of the session being
    prompted, down to the item remembered in ``session_state`` from the last
    scan, and writes the same labels :func:`observe_items` would have. The
    prompt itself is the human's turn, so an open gate is answered by it.
    Abstains when P3 is present, off a session route, or when the store
    does not answer (a capture that cannot read must not stop work).

    :param event: The policy event; only the ``request`` phase is read.
    :returns: ALLOW with the writes, or ``None`` to abstain.
    """
    if event.get("type") != "request" or HAVE_ITEM_OBSERVERS:
        return None
    session_id = sessions.current_conversation_id()
    if not session_id:
        return None
    state = event.get("session_state") or {}
    seen = state.get(SEEN_KEY)
    try:
        store = get_conversation_store()
        page = store.list_items(session_id, limit=FALLBACK_LIMIT, order="desc")
        conv = store.get_conversation(session_id)
    except STORE_ERRORS:
        _logger.warning("observer could not read the store; skipping the scan", exc_info=True)
        return None
    fresh: list[ConversationItem] = []
    for item in page.data:
        if item.id == seen:
            break
        fresh.append(item)
    if not fresh:
        return None
    fresh.reverse()
    context: dict[str, Any] = event.get("context") or {}
    current = conv.labels if conv is not None else context.get("labels") or {}
    updates = updates_for(fresh, current)
    gate = updates.get(labels.GATE, labels.label(current, labels.GATE))
    if gate == labels.GATE_OPEN:
        updates[labels.GATE] = labels.GATE_ANSWERED
    response: PolicyResponse = {
        "result": "ALLOW",
        "state_updates": [{"key": SEEN_KEY, "action": "set", "value": fresh[-1].id}],
    }
    if updates:
        response["set_labels"] = updates
    return response


def register() -> bool:
    """Register :func:`observe_items` with upstream's registry when it exists.

    :returns: ``True`` when registered (P3 present), ``False`` otherwise.
    """
    if register_item_observer is None:
        _logger.info(
            "omnigent.server.item_observers is missing (patch P3 not applied); "
            "the observer falls back to scanning at the next prompt"
        )
        return False
    register_item_observer(observe_items)
    return True


REGISTERED = register()

__all__ = [
    "ABSORBED_LINE",
    "FALLBACK_LIMIT",
    "FORK_LINE",
    "GLYPHS",
    "HAVE_ITEM_OBSERVERS",
    "NEXT_LINE",
    "REGISTERED",
    "RENAME_TOOLS",
    "SEEN_KEY",
    "THREAD_LINE",
    "TITLE_LINE",
    "UNFILED_LINE",
    "glyph_status",
    "message_text",
    "note_text",
    "observe",
    "observe_items",
    "register",
    "updates_for",
]
