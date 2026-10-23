"""The ``ae.*`` label vocabulary.

One place for the keys and values every module reads and writes. ``docs/LABELS.md``
documents them for people; Phase 2's ``web/src/lib/aeLabels.ts`` mirrors them for
the UI. The ``ae.`` namespace is free of upstream's reserved keys
(``omnigent.*``, ``omni_project``; see
``omnigent/server/routes/_sessions/helpers.py`` ``_reject_server_reserved_label_seed``).

Policies cannot delete a label: ``set_labels`` is an upsert. A key is cleared by
writing ``""``, so every reader here treats an empty or blank value as unset.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping

NAMESPACE = "ae."

STATUS = "ae.status"
STARTED = "ae.started"
GATE = "ae.gate"
# The call id of the pop-up question (AskUserQuestion) that asks the open gate,
# so its answer, which lands as a tool result, can close it (2026-10-23).
GATE_ASK = "ae.gate_ask"
FORK = "ae.fork"
NEXT = "ae.next"
ABSORBED = "ae.absorbed"
UNFILED = "ae.unfiled"
FORK_SUSPECT = "ae.fork_suspect"
TRIAGE = "ae.triage"
TASK = "ae.task"
OVERFLOW = "ae.overflow"
# Sessions the orchestrator agent started (POST /v1/ae/sessions, 2026-09-25):
# who started it (the orchestrator's session id), the idempotency key a retry
# is matched on, and the repository and branch its pull request will come from.
ORCH = "ae.orch"
ORCH_KEY = "ae.orch_key"
REPO = "ae.repo"
BRANCH = "ae.branch"
# The runner id an automatic resume was last tried for (auto_resume.py, 2026-10-13).
AUTORESUME = "ae.autoresume"

WORKING = "working"
BLOCKED = "blocked"
REVIEW = "review"
PARKED = "parked"
REFERENCE = "reference"
STATUS_VALUES = (WORKING, BLOCKED, REVIEW, PARKED, REFERENCE)

GATE_OPEN = "open"
GATE_ANSWERED = "answered"

TRIAGE_OFF = "off"
TRIAGE_CAPTURE = "capture"
TRIAGE_VALUES = (TRIAGE_OFF, TRIAGE_CAPTURE)

# The slot cap: working sessions allowed at once. One setting since 2026-09-24,
# so moving from three slots to four is one line in config/server.env
# (AE_SLOT_LIMIT=4) and a container recreate. Since 2026-10-22 a limit set on the
# slot cap policy on Settings > Global Policies replaces it for the policy and, through
# slot_limit.effective_limit and GET /v1/ae/capacity, for every count in the web UI.
SLOT_LIMIT_ENV = "AE_SLOT_LIMIT"
DEFAULT_SLOT_LIMIT = 3


def slot_limit_from_env(environ: Mapping[str, str] | None = None) -> int:
    """Read :data:`SLOT_LIMIT_ENV`: a positive integer, :data:`DEFAULT_SLOT_LIMIT` when unset.

    :param environ: The mapping to read; ``os.environ`` when omitted.
    :returns: The slot limit.
    :raises ValueError: On a value that is not a whole number of one or more, so a
        typo stops the server at startup instead of silently capping at three.
    """
    env = os.environ if environ is None else environ
    raw = (env.get(SLOT_LIMIT_ENV) or "").strip()
    if not raw:
        return DEFAULT_SLOT_LIMIT
    try:
        value = int(raw)
    except ValueError:
        value = 0
    if value < 1:
        raise ValueError(
            f"{SLOT_LIMIT_ENV}={raw!r} is not a whole number of one or more. Set "
            f"{SLOT_LIMIT_ENV} in config/server.env to the number of slots (for example "
            f"{SLOT_LIMIT_ENV}=4), or remove the line for the default of {DEFAULT_SLOT_LIMIT}."
        )
    return value


# Read once at import, which is server startup (the policy registry scan).
SLOT_LIMIT = slot_limit_from_env()

# ``ae.fork`` and ``ae.next`` hold one line each; the store caps a label value at
# 256 characters (upstream ``omnigent/db/db_models.py`` ``LABEL_VALUE_MAX_LEN``).
TEXT_CAP = 200

# One label per ``Absorbed:`` and ``UNFILED`` line (2026-10-19): ``ae.absorbed.1``,
# ``ae.absorbed.2`` and on, and ``ae.unfiled.1`` and on, numbered by the count the
# line brought ``ae.absorbed`` or ``ae.unfiled`` to, each the line's text clipped to
# :data:`TEXT_CAP`. The session rail's Triage card lists them, so a note stays
# readable after its message is too old for the chat to have loaded; before, the
# card said "The lines are in older messages not loaded here." A label value holds
# 256 characters, hence one label per note. Only the first :data:`NOTE_CAP` of each
# kind are kept, since every label rides in each sidebar row: at most about 8 KB a
# session. Past the cap the card shows what the loaded messages hold.
NOTE_CAP = 20
_NOTE_KEY = re.compile(r"^ae\.(?:absorbed|unfiled)\.\d+$")


def label(labels: Mapping[str, str] | None, key: str) -> str | None:
    """Return the label's value, or ``None`` when missing, empty or blank.

    :param labels: A session's labels, e.g. ``event["context"]["labels"]``.
    :param key: The label key, e.g. :data:`STATUS`.
    :returns: The stripped value, or ``None`` when the key is unset.
    """
    if not labels:
        return None
    value = labels.get(key)
    if value is None:
        return None
    value = value.strip()
    return value or None


def status_of(labels: Mapping[str, str] | None) -> str | None:
    """Return the session's ``ae.status``, or ``None`` when it has none yet."""
    return label(labels, STATUS)


def is_working(labels: Mapping[str, str] | None) -> bool:
    """Whether the session holds a slot (``ae.status`` is ``working``)."""
    return status_of(labels) == WORKING


def clip(text: str, cap: int = TEXT_CAP) -> str:
    """Collapse *text* to one line and cut it to *cap* characters.

    :param text: Any string, possibly multi-line.
    :param cap: Maximum length; the result carries ``...`` when cut.
    :returns: A single line of at most *cap* characters.
    """
    flat = " ".join(text.split())
    if len(flat) <= cap:
        return flat
    return flat[: cap - 3].rstrip() + "..."


def count(labels: Mapping[str, str] | None, key: str) -> int:
    """A counter label (:data:`ABSORBED`, :data:`UNFILED`) as a number; 0 when unset or not one."""
    raw = label(labels, key)
    return int(raw) if raw and raw.isdigit() else 0


def note_key(counter: str, number: int) -> str:
    """The label holding note *number* of *counter*, e.g. ``"ae.absorbed.3"``.

    :param counter: :data:`ABSORBED` or :data:`UNFILED`.
    :param number: The note's place in the count, from 1.
    :returns: The label key.
    """
    return f"{counter}.{number}"


def is_note_key(key: str) -> bool:
    """Whether *key* holds one saved note (:func:`note_key`), not a count or a status."""
    return bool(_NOTE_KEY.match(key))


def ae_labels_without_notes(labels: Mapping[str, str]) -> dict[str, str]:
    """The ``ae.*`` labels minus the saved notes, for answers that echo a session's labels."""
    return {k: v for k, v in labels.items() if k.startswith(NAMESPACE) and not is_note_key(k)}
