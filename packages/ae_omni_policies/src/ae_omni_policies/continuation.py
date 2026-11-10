"""Notes the server posts into a session to continue its own work, and how policies spot them.

Added 2026-10-21 with the merge watcher (:mod:`ae_omni_policies.merge_watch`).
When a pull request a session tracks is merged or closed, the server posts one
line into that session so it carries on (deletes the branch, checks the
deploy) without the owner typing "merged". That note is a user message on
``POST /v1/sessions/{id}/events``, so the request-phase policies see it like a
typed prompt. Two of them would stop it:

- the slot cap holds a prompt for approval when the session is not
  ``ae.status=working`` and the slots are full, and a merge usually lands
  after the session went to ``review``;
- the rabbit hole asks once a session passes four hours or 60 prompts, which
  a session waiting on a merge often has.

The child wake (:mod:`ae_omni_policies.child_wake`, 2026-10-28) posts a second
kind: when the owner answers a family child's prompt in the lead's action dock and the
lead is idle, one line (``Child prompt answered: <title> (<id>...): the user picked
<choice>.``) tells the lead to carry on. It passes the same
three gates for the same reason. (A lead handoff note was a third kind on 2026-10-29; the
sweep no longer posts one, so it is no longer recognized: a prompt that starts with its
words is an ordinary prompt.)

The queued Compact (:mod:`ae_omni_policies.compact_queue`, 2026-11-06) posts a third kind: the
save-your-notes prompt (:data:`COMPACT_PREP_TEXT`) it sends before the compact. It is matched
whole, so words added before it make it an ordinary prompt.

A merge note is neither a new task nor another turn of the owner's, so the slot cap
lets it through without claiming a slot, and the rabbit hole and the fork
classifier abstain on it (no turn counted, no model call).

The note is recognized by its text, because that is all every enforcement site
shares: the server's input gate passes ``{"user_content": ...}`` and the
native harness hooks pass the bare prompt string
(:func:`omnigent.policies.schema.request_user_text`). Anyone who can post into
a session can start a message with the same words; what that buys is one prompt
past the slot cap without a slot, the same reach as typing ``ride unparked``
and smaller (no overflow label), so the text is the marker rather than a
secret.
"""

from __future__ import annotations

import re

#: Every note the merge watcher posts starts with this.
MERGE_WATCH_PREFIX = "Merge watcher: "
_MERGE_WATCH = re.compile(r"^Merge watcher: PR #[1-9][0-9]* \(")
#: Every note the child wake posts starts with this (2026-10-28).
CHILD_WAKE_PREFIX = "Child prompt answered: "
#: Caps of the note's title and picked label; :mod:`ae_omni_policies.child_wake` cuts to them.
TITLE_CAP = 60
PICK_CAP = 80
_CHILD_WAKE = re.compile(
    rf"^Child prompt answered: [^\n]{{1,{TITLE_CAP}}} \([0-9A-Za-z_]{{4}}\.\.\.\): "
    rf"(?:the user|[A-Z][A-Za-z]*) picked [^\n.]{{1,{PICK_CAP}}}\.$"
)


#: The prompt the queued Compact posts so the chat saves its notes before compacting.
COMPACT_PREP_TEXT = (
    "Compaction runs right after this turn. Save anything worth keeping to memory or a handoff "
    "note now: decisions and why, open work, file paths, what to do next. Start no new work and "
    "do not compact yourself. Reply with one line, 'ready', when you are done."
)


def is_system_continuation(text: str) -> bool:
    """Whether *text* is a note the server posted to continue existing work.

    :param text: The request's user text (``request_user_text``).
    :returns: ``True`` for a merge watcher note, a child wake note or the Compact save prompt.
    """
    head = text.strip()
    return (
        _MERGE_WATCH.match(head) is not None
        or _CHILD_WAKE.match(head) is not None
        or head == COMPACT_PREP_TEXT
    )


__all__ = [
    "CHILD_WAKE_PREFIX",
    "COMPACT_PREP_TEXT",
    "MERGE_WATCH_PREFIX",
    "PICK_CAP",
    "TITLE_CAP",
    "is_system_continuation",
]
