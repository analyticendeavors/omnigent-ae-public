"""Notes the server posts into a session to continue its own work, and how policies spot them.

Added 2026-10-21 with the merge watcher (:mod:`ae_omni_policies.merge_watch`).
When a pull request a session tracks is merged or closed, the server posts one
line into that session so it carries on (deletes the branch, checks the
deploy) without Reid typing "merged". That note is a user message on
``POST /v1/sessions/{id}/events``, so the request-phase policies see it like a
typed prompt. Two of them would stop it:

- the slot cap holds a prompt for approval when the session is not
  ``ae.status=working`` and the slots are full, and a merge usually lands
  after the session went to ``review``;
- the rabbit hole asks once a session passes four hours or 60 prompts, which
  a session waiting on a merge often has.

A merge note is neither a new task nor another turn of Reid's, so the slot cap
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


def is_system_continuation(text: str) -> bool:
    """Whether *text* is a note the server posted to continue existing work.

    :param text: The request's user text (``request_user_text``).
    :returns: ``True`` for a merge watcher note.
    """
    return _MERGE_WATCH.match(text.lstrip()) is not None


__all__ = ["MERGE_WATCH_PREFIX", "is_system_continuation"]
