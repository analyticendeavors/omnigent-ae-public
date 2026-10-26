"""The stdio MCP server a scheduled agent gets as its ``tools``.

``python -m ae_omni_policies.mcp`` speaks MCP over stdin and stdout and
exposes the tools in :data:`TOOLS`. Four are the task-store worker's, names, input schemas
and output shapes unchanged (``task-store-worker/src/index.ts`` in
analyticendeavors/ae-claude-skills): ``park_fork``, ``link_issue``,
``list_tags``, ``list_parked``. ``complete_task`` (2026-09-28) finishes a
parked task and closes its issue in one call. Three are this fork's: ``omni_capacity`` (the
slots), ``omni_set_status`` (``ae.status`` on a session) and
``omni_transcript`` (a session's newest messages). Written 2026-09-23
(Phase 5).

2026-09-25 added the orchestrator agent's five: ``create_session``,
``post_message``, ``schedule_checkin``, ``list_prs`` (the server's cards plus
``gh`` on the host, :mod:`ae_omni_policies.gh`) and ``ask_reid`` (an MCP
elicitation, which OmniGent shows as a pop-up). The first three write and sit
on :data:`WRITING_TOOLS`. ``report_review`` (2026-10-24) records a lead's final review counts
for the preset run log (:mod:`ae_omni_policies.preset_runs`) and is a writer too.
``preset_insights_read`` and ``submit_preset_suggestions`` (2026-10-24) are the scheduled
preset reviewer's pair; the second writes.

Every tool is a thin HTTP call to the OmniGent server with the token
``omnigent login`` stored (or ``OMNIGENT_TOKEN``), through
:mod:`ae_omni_policies.omni`: ``OMNIGENT_SERVER_URL`` when it arrives, else
the server that login recorded under ``~/.omnigent/`` (a host launches this
with little more than ``HOME``; the module docstring there says why). The worker tools go
to the ``/v1/ae/lot`` routes, the session tools to upstream's own session
routes; nothing here holds the Cloudflare Access token, so the agent's host
never sees ``AE_CF_*``. A failure of any kind comes back as the tool's text
(the worker's ``{"error": ...}`` and ``{"outcome": "unfiled", ...}`` shapes),
never as a crash: a scheduled run has nobody to read a traceback.

What the agent may call is not decided here. The server offers every tool;
the agent YAML's ``tools:`` allowlist under its MCP entry filters them
(``docs/AGENT_YAML_SPEC.md``, ``MCPServerConfig.tools``), which is how the
steward and the shutdown agent are kept propose-only: neither lists
``park_fork``, ``link_issue`` or ``omni_set_status``. ``tests/test_mcp.py``
checks the allowlists against :data:`TOOL_NAMES`.

The schemas below are hand-written copies of the worker's zod definitions,
not derived from Python signatures, so ``from`` can keep its name and the
descriptions the worker gives the model arrive unchanged.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from collections.abc import Sequence
from datetime import date
from typing import Any

import httpx
from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

from ae_omni_policies import gh, labels, prs, schedules
from ae_omni_policies.notes import NoteFields, build_notes, repo_slug, summary_for
from ae_omni_policies.omni import Settings, http_client
from ae_omni_policies.taskstore import UNFILED_INSTRUCTION

_logger = logging.getLogger(__name__)

SERVER_NAME = "ae-omni"
DEFAULT_TRANSCRIPT_ITEMS = 20
MAX_TRANSCRIPT_ITEMS = 200
# The items route caps a page at 1000; a transcript is mostly messages but
# tool calls sit between them, so ask for a few times what is wanted.
_ITEMS_PER_MESSAGE = 4

NOT_LINKED_INSTRUCTION = (
    "The task and the issue both exist, but nothing joins them. Report the task as "
    "filed and give Reid the issue URL inline."
)

# ── the worker's schemas, verbatim ───────────────────────────────────────

PARK_FORK_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "summary": {
            "type": "string",
            "description": (
                "The gate's FORK: clause, naming the fork in one line. It becomes the bold "
                "line at the top of the note, which is what Reid reads when scanning the "
                "lot weeks later. Defaults to the first sentence of state."
            ),
        },
        "thread": {
            "type": "string",
            "description": (
                "THREAD short-name. Becomes the task title, so keep it a name, not a sentence."
            ),
        },
        "state": {
            "type": "string",
            "description": ("STATE: where it stands in one line (what exists, what is decided)."),
        },
        "next": {
            "type": "string",
            "description": (
                "NEXT: one physical action that resumes it (a command, a file, a sentence "
                "to finish). 'Look into X' is not a physical action."
            ),
        },
        "slot": {
            "type": "string",
            "enum": ["never opened", "freed", "swapped-back"],
            "description": "SLOT: the gate's slot outcome.",
        },
        "quadrant": {
            "type": "string",
            "enum": ["hotfix", "feature", "admin"],
            "description": (
                "QUADRANT from the gate's urgent x important call: both is hotfix, "
                "important only is feature, urgent only is admin. Mapped to the app's "
                "lanes here; do not send a priority."
            ),
        },
        "est": {
            "type": "string",
            "description": (
                "EST: the rough size in the gate's own words ('minutes', 'about an hour', "
                "'a block', 'multi-block'). Stored verbatim on the note; the app's estimate "
                "field is derived from it."
            ),
        },
        "from": {
            "type": "string",
            "description": "FROM: this thread's task, surface and date.",
        },
        "checklist": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "Optional resume steps, rendered as a checklist at the top of the note. "
                "Supply one only for 2+ discrete steps; a single step belongs in NEXT."
            ),
        },
        "repo": {
            "type": "string",
            "description": (
                "The repository the fork belongs to, as owner/name (a full GitHub URL is "
                "accepted and reduced). Renders as a link at the top of the note and as a "
                "REPO: line in the resume block. Same signal that decides whether an issue "
                "is opened at all: a fork belonging to no repository sends nothing here."
            ),
        },
        "tags": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "Domain tags beyond `parked`, which is always added here and never needs "
                "sending. Propose these in the gate's own message so Reid sees them before "
                "anything is written, and take them from the vocabulary list_tags returns "
                "rather than inventing near-duplicates. Lowercased and deduped here."
            ),
        },
        "due_date": {
            "type": "string",
            "description": (
                "Override the resurface date, as YYYY-MM-DD, or the literal 'none' to file "
                "with no date at all. Left out, the date defaults by quadrant: hotfix 3 "
                "days out, admin 14, feature 30. The date means come back and decide, not "
                "do it by then."
            ),
        },
        "issue_url": {
            "type": "string",
            "description": (
                "The linked GitHub issue, for a repo-scoped fork only. Pass the URL the "
                "issue creation returned. A fork that belongs to no repository gets no issue."
            ),
        },
    },
    "required": ["thread", "state", "next", "slot", "quadrant", "est", "from"],
    "additionalProperties": False,
}

LIST_TAGS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {},
    "additionalProperties": False,
}

LINK_ISSUE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "task_id": {"type": "string", "description": "The task id park_fork returned."},
        "issue_url": {
            "type": "string",
            "description": "The URL the GitHub issue creation returned. Never one you composed.",
        },
        "label": {"type": "string", "description": "Link label. Defaults to 'GitHub issue'."},
    },
    "required": ["task_id", "issue_url"],
    "additionalProperties": False,
}

COMPLETE_TASK_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "task_id": {
            "type": "string",
            "description": "The parked task's id, as Resume or list_parked gives it.",
        },
        "status": {
            "type": "string",
            "enum": ["done", "dropped"],
            "description": "done when the work shipped; dropped when it was decided against.",
        },
        "summary": {
            "type": "string",
            "description": "One sentence for the issue comment: what shipped, with its PR URL.",
        },
    },
    "required": ["task_id"],
    "additionalProperties": False,
}

LIST_PARKED_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "status": {
            "type": "string",
            "enum": ["open", "active", "blocked", "done", "dropped", "all"],
            "description": "Status to list. Defaults to open, which is what a parked fork is.",
        },
        "limit": {
            "type": "integer",
            "minimum": 1,
            "maximum": 100,
            "description": "How many to return. Defaults to 20.",
        },
    },
    "additionalProperties": False,
}

# ── this fork's schemas ──────────────────────────────────────────────────

OMNI_CAPACITY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {},
    "additionalProperties": False,
}

OMNI_SET_STATUS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "session_id": {
            "type": "string",
            "description": "The OmniGent session id, e.g. conv_... as omni_capacity lists it.",
        },
        "status": {
            "type": "string",
            "enum": list(labels.STATUS_VALUES),
            "description": (
                "The new ae.status: working (holds a slot), blocked (waiting on a person), "
                "review (done, handoff written), parked (not now, the note is in the lot), "
                "reference (closed, lookup only)."
            ),
        },
    },
    "required": ["session_id", "status"],
    "additionalProperties": False,
}

OMNI_TRANSCRIPT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "session_id": {
            "type": "string",
            "description": "The OmniGent session id, e.g. conv_... as omni_capacity lists it.",
        },
        "last_n": {
            "type": "integer",
            "minimum": 1,
            "maximum": MAX_TRANSCRIPT_ITEMS,
            "description": f"How many of the newest messages to return. Defaults to "
            f"{DEFAULT_TRANSCRIPT_ITEMS}.",
        },
    },
    "required": ["session_id"],
    "additionalProperties": False,
}

# ── the orchestrator's schemas (2026-09-25) ─────────────────────────────

CREATE_SESSION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "title": {
            "type": "string",
            "description": "The session title Reid sees: a name for the work, not a sentence.",
        },
        "brief": {
            "type": "string",
            "description": (
                "The first message: everything the session needs to start with zero shared "
                "context (goal, repo, branch, finish line, how to report back)."
            ),
        },
        "harness": {
            "type": "string",
            "enum": ["claude", "codex"],
            "description": "claude starts claude-native-ui, codex starts codex-native-ui.",
        },
        "workspace": {
            "type": "string",
            "description": (
                "The repository directory on the host, absolute or relative to its home "
                "(~/repos/omnigent-ae). Defaults to the host's home."
            ),
        },
        "host": {
            "type": "string",
            "description": "Host name or id. Defaults to the one online host.",
        },
        "key": {
            "type": "string",
            "description": (
                "Idempotency key, up to 64 characters: a retry with the same key returns the "
                "live session it started instead of a second one. Use one key per piece of "
                "work, e.g. the branch name."
            ),
        },
        "repo": {
            "type": "string",
            "description": "owner/name of the repository its pull request will be opened on.",
        },
        "branch": {
            "type": "string",
            "description": "The branch the session pushes, so its pull request can be matched.",
        },
        "orchestrator": {
            "type": "string",
            "description": (
                "Your own session id, recorded on the new session (filled in when left out). "
                "It joins your family: the sidebar groups it under you, and you and the "
                "sessions you start hold one slot between you, up to 3 of them working at once."
            ),
        },
        "role": {
            "type": "string",
            "enum": ["lead", "planner", "reviewer", "builder", "helper"],
            "description": (
                "The role this session plays; it gets the model and effort your preset sets "
                "for that role."
            ),
        },
        "model": {
            "type": "string",
            "description": "A model catalog id for the session; wins over the role's model.",
        },
        "effort": {
            "type": "string",
            "enum": ["low", "medium", "high", "xhigh", "max"],
            "description": "Reasoning effort for the session; wins over the role's effort.",
        },
        "preset": {
            "type": "string",
            "description": "A preset id the new session opens on, for the helpers it starts.",
        },
        "ride_unparked": {
            "type": "boolean",
            "description": (
                "Start it past the slot limit. Only after Reid picked that in ask_reid; the "
                "session is marked overflow."
            ),
        },
        "standalone": {
            "type": "boolean",
            "description": (
                "Open a chat of its own for Reid instead of a helper (a handoff): it joins "
                "no family, holds its own slot, and runs as lead on preset (your default "
                "preset when left out). Pass key (or a distinct title) for each handoff, or "
                "a live chat with the same title is returned. Give its link to Reid."
            ),
        },
    },
    "required": ["title", "brief"],
    "additionalProperties": False,
}

POST_MESSAGE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "session_id": {"type": "string", "description": "The session to message (conv_...)."},
        "text": {
            "type": "string",
            "description": "The message, posted as the session's next user turn.",
        },
    },
    "required": ["session_id", "text"],
    "additionalProperties": False,
}

SCHEDULE_CHECKIN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "every_hours": {
            "type": "integer",
            "minimum": 1,
            "maximum": schedules.MAX_CHECKIN_HOURS,
            "description": "Hours between check-ins; one is the server's floor. Defaults to 1.",
        },
        "until": {
            "type": "string",
            "description": "Last day to check in, YYYY-MM-DD in the server's timezone. Optional.",
        },
        "session_id": {
            "type": "string",
            "description": (
                "Your own session id (conv_...): each check-in is handed to this session so "
                "the conversation carries on."
            ),
        },
        "note": {"type": "string", "description": "What the check-ins should focus on."},
        "stop": {
            "type": "boolean",
            "description": "Pause the check-ins instead (nothing left in flight).",
        },
    },
    "additionalProperties": False,
}

LIST_PRS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "repos": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "owner/name repositories to read with gh on the host, besides the ones the "
                "orchestrated sessions name."
            ),
        },
        "urls": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "Pull request URLs the sessions reported. Each one not among the open cards is "
                "read by itself from the server, so a merge is seen even without gh."
            ),
        },
        "include_merged": {
            "type": "boolean",
            "description": "Also the ten most recently merged per repository, for close-out.",
        },
        "fresh": {"type": "boolean", "description": "Re-scan which sessions track what."},
    },
    "additionalProperties": False,
}

ASK_REID_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "question": {"type": "string", "description": "One decision, one line."},
        "options": {
            "type": "array",
            "minItems": 2,
            "maxItems": 4,
            "items": {"type": "string"},
            "description": (
                "Two to four answers in plain English, each saying what will happen. The "
                "first is the recommended one and is shown with (Recommended)."
            ),
        },
    },
    "required": ["question", "options"],
    "additionalProperties": False,
}

REPORT_REVIEW_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "reviewers_run": {
            "type": "integer",
            "minimum": 0,
            "description": "How many reviewers ran in this review.",
        },
        "findings_returned": {
            "type": "integer",
            "minimum": 0,
            "description": "How many findings the reviewers returned after you merged them.",
        },
        "findings_fixed": {
            "type": "integer",
            "minimum": 0,
            "description": "How many of those findings you fixed.",
        },
        "call_id": {
            "type": "string",
            "minLength": 1,
            "maxLength": 100,
            "description": (
                "A name for this review round, e.g. 'final review' or 'fix check 2'. A second "
                "call with a name already counted in this run is ignored, so a repeated call "
                "never counts twice."
            ),
        },
    },
    "required": ["reviewers_run", "findings_returned", "findings_fixed"],
    "additionalProperties": False,
}

PRESET_INSIGHTS_READ_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {},
    "additionalProperties": False,
}

_PRESET_CHANGE_PATHS = (
    "roles.(lead|planner|builder|helper|reviewer), roles.reviewer.reviewers.(0|1|2) or "
    "rules.(raise_or_lower|lead_steps_down|auto_raises|helpers|review_merge|"
    "reviewer_unavailable)"
)
SUBMIT_PRESET_SUGGESTIONS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "summary": {
            "type": "string",
            "minLength": 1,
            "maxLength": 800,
            "description": "One paragraph on what the review found.",
        },
        "suggestions": {
            "type": "array",
            "maxItems": 5,
            "description": "At most five suggestions, or no_change items for what you checked.",
            "items": {
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "enum": ["own", "starter", "no_change"]},
                    "owner": {
                        "type": ["string", "null"],
                        "description": "The preset's owner. Omit for your own; others need admin.",
                    },
                    "preset_id": {
                        "type": ["string", "null"],
                        "description": "Required for own and no_change.",
                    },
                    "title": {"type": "string", "minLength": 1, "maxLength": 120},
                    "why": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 800,
                        "description": "Run count and the numbers it rests on.",
                    },
                    "saving": {
                        "type": ["string", "null"],
                        "maxLength": 200,
                        "description": "Expected saving, such as 'about $5 per run'. "
                        "Required for own and starter.",
                    },
                    "changes": {
                        "type": "array",
                        "maxItems": 6,
                        "description": "own needs at least one; no_change none.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "path": {
                                    "type": "string",
                                    "description": f"One of {_PRESET_CHANGE_PATHS}.",
                                },
                                "value": {
                                    "description": "The new JSON value for that path, in the "
                                    "preset's own shape."
                                },
                            },
                            "required": ["path", "value"],
                            "additionalProperties": False,
                        },
                    },
                },
                "required": ["kind", "title", "why"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["summary", "suggestions"],
    "additionalProperties": False,
}

SET_SESSION_MODEL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "session_id": {
            "type": "string",
            "description": "The session to change: your own, or one you started.",
        },
        "model": {
            "type": "string",
            "description": "The model catalog id, such as claude-opus-5-5. Omit to keep it.",
        },
        "effort": {
            "type": "string",
            "enum": ["low", "medium", "high", "xhigh", "max"],
            "description": "The reasoning effort. Leave out to keep it.",
        },
        "reason": {
            "type": "string",
            "maxLength": 200,
            "description": "One plain sentence on why, shown to Reid in the notice and the log.",
        },
    },
    "required": ["session_id", "reason"],
    "additionalProperties": False,
}

TOOLS: list[types.Tool] = [
    types.Tool(
        name="park_fork",
        description=(
            "Route C of the fork-triage gate: file a parked fork as a task in the Parking Lot "
            "list of Reid's task-store. Call it only after Reid answers C at the gate, and "
            "call it FIRST: task, then the GitHub issue (repo-scoped forks only), then "
            "link_issue to point the task back at it. That order is load-bearing. A failed "
            "issue leaves a visible task; the reverse leaves an issue pointing at nothing. "
            "Pass issue_url here only when the issue somehow already exists. Returns the "
            "task id and a URL that opens it in the app; report those, never a URL you "
            "composed yourself."
        ),
        inputSchema=PARK_FORK_SCHEMA,
    ),
    types.Tool(
        name="list_tags",
        description=(
            "Reid's existing tag vocabulary, with how many tasks carry each one. Read this "
            "BEFORE proposing tags in a fork-triage gate message, so the suggestion comes "
            "from tags he already uses instead of minting a near-duplicate nobody can "
            "filter on. `parked` is added by park_fork and never needs proposing."
        ),
        inputSchema=LIST_TAGS_SCHEMA,
    ),
    types.Tool(
        name="link_issue",
        description=(
            "Point an already-parked task at the GitHub issue opened for it. This is the "
            "third and last step of route C for a repo-scoped fork: park_fork, then the "
            "issue, then this. Pass the task id park_fork returned and the URL the issue "
            "creation returned. If it fails, hand Reid the issue URL inline rather than "
            "leaving the two silently unconnected."
        ),
        inputSchema=LINK_ISSUE_SCHEMA,
    ),
    types.Tool(
        name="list_parked",
        description=(
            "Read the Parking Lot, so 'offer the top of the parking lot as the next pull' "
            "works from any surface. Returns each parked task's id, title, lane, size and "
            "its NEXT: line."
        ),
        inputSchema=LIST_PARKED_SCHEMA,
    ),
    types.Tool(
        name="complete_task",
        description=(
            "Finish a parked task the moment its work is done: marks it done (or dropped) "
            "in task-store, then closes its linked GitHub issue with a comment, so the lot, "
            "the task app and the issue all agree at once. Call it once the work has "
            "shipped; never rely on a Closes line in a pull request, which GitHub ignores "
            "on a merge into dev. Report the task and issue URLs it returns."
        ),
        inputSchema=COMPLETE_TASK_SCHEMA,
    ),
    types.Tool(
        name="omni_capacity",
        description=(
            "The slots as the OmniGent server sees them: the limit and every top-level session by "
            "ae.status (working, blocked, review, parked, reference, unlabelled_running) "
            "with its title, elapsed time, NEXT line and open gate, plus the current date "
            "and time in the lot's timezone. A working session waiting on an approval "
            "(blocked: true) or whose last turn failed (failed: true) is listed under blocked "
            "and holds no slot. Read only."
        ),
        inputSchema=OMNI_CAPACITY_SCHEMA,
    ),
    types.Tool(
        name="omni_set_status",
        description=(
            "Set a session's ae.status label on the OmniGent server: the status field that "
            "replaces the title glyph. A scheduled agent never calls this; a person or an "
            "interactive session does, after the human's call. To flip the session you are "
            "running in, read its id (conv_...) from the omnigent session-info tool first."
        ),
        inputSchema=OMNI_SET_STATUS_SCHEMA,
    ),
    types.Tool(
        name="omni_transcript",
        description=(
            "A session's newest messages (role and text, oldest first) from the OmniGent "
            "server, for drafting a THREAD / STATE / NEXT note about where it stands. "
            "Read only."
        ),
        inputSchema=OMNI_TRANSCRIPT_SCHEMA,
    ),
    types.Tool(
        name="create_session",
        description=(
            "Start an OmniGent session on the box (claude-native or codex-native, in a "
            "repository directory) and post its brief as the first message. Holds a slot: at "
            "the limit it answers slots_full with who holds them (blocked and failed sessions "
            "hold none). Idempotent by key: a retry returns the live session (existing: true). "
            "Pass role (planner, reviewer, builder, helper) for the model and effort your "
            "preset sets for that role, or standalone to open a handoff chat for Reid. "
            "Returns the session id and its URL."
        ),
        inputSchema=CREATE_SESSION_SCHEMA,
    ),
    types.Tool(
        name="post_message",
        description=(
            "Post a user message into a session: a re-brief on a merge conflict or red CI, a "
            "nudge, or a check-in handed to the orchestrator."
        ),
        inputSchema=POST_MESSAGE_SCHEMA,
    ),
    types.Tool(
        name="schedule_checkin",
        description=(
            "Create, change or pause the one recurring orchestrator check-in (a scheduled task "
            "on the OmniGent server). Hourly at most; each fire hands the check-in to "
            "session_id when given."
        ),
        inputSchema=SCHEDULE_CHECKIN_SCHEMA,
    ),
    types.Tool(
        name="list_prs",
        description=(
            "Pull requests with CI, mergeability and review, and the session that owns each: "
            "the server's cards (GET /v1/ae/prs) plus gh on the host. Says plainly when either "
            "source is missing. Also lists the orchestrated sessions. Read only."
        ),
        inputSchema=LIST_PRS_SCHEMA,
    ),
    types.Tool(
        name="ask_reid",
        description=(
            "Ask Reid one decision as a pop-up (an elicitation) and wait for the answer. "
            "Recommended option first. Answers with his pick, or with how to ask in chat "
            "when pop-ups are not available."
        ),
        inputSchema=ASK_REID_SCHEMA,
    ),
    types.Tool(
        name="set_session_model",
        description=(
            "Change the model or effort of your own session or one you started, under your "
            "preset's rules. It may apply at once and tell Reid, ask him first in a pop-up, or "
            "be refused; the answer says which. A Codex helper takes effort changes only."
        ),
        inputSchema=SET_SESSION_MODEL_SCHEMA,
    ),
    types.Tool(
        name="report_review",
        description=(
            "After the final review of your work, record what it found: reviewers run, "
            "findings returned and findings fixed. Counts only; it feeds the preset run log. "
            "Calls in one run add up; pass call_id so a repeat of the same round is not "
            "counted twice."
        ),
        inputSchema=REPORT_REVIEW_SCHEMA,
    ),
    types.Tool(
        name="preset_insights_read",
        description=(
            "Preset reviewer only: read how role presets performed since the last review "
            "(runs, model changes, preset switches, the presets). Answers due false with a "
            "reason when no review is due. Call it once."
        ),
        inputSchema=PRESET_INSIGHTS_READ_SCHEMA,
    ),
    types.Tool(
        name="submit_preset_suggestions",
        description=(
            "Preset reviewer only: submit the review's summary and at most five suggestions. "
            "The server checks them and answers an error naming what to fix. Call it once."
        ),
        inputSchema=SUBMIT_PRESET_SUGGESTIONS_SCHEMA,
    ),
]
TOOL_NAMES: tuple[str, ...] = tuple(tool.name for tool in TOOLS)
# The tools that change something. A propose-only agent's allowlist must not
# name any of these; ``tests/test_mcp.py`` holds the shipped YAMLs to it.
WRITING_TOOLS: frozenset[str] = frozenset(
    {
        "park_fork",
        "link_issue",
        "complete_task",
        "omni_set_status",
        "create_session",
        "post_message",
        "schedule_checkin",
        "set_session_model",
        "report_review",
        "submit_preset_suggestions",
    }
)


def _text(payload: Any) -> list[types.TextContent]:
    """One JSON text block, indented like the worker's ``ok()``."""
    return [types.TextContent(type="text", text=json.dumps(payload, indent=2))]


def _message(status: int, data: Any) -> str:
    """The most useful sentence in a failed answer."""
    if isinstance(data, dict):
        error = data.get("error")
        if isinstance(error, dict) and error.get("message"):
            return f"{status} {error['message']}"
        if isinstance(error, str):
            return f"{status} {error}"
        detail = data.get("detail")
        if detail:
            return f"{status} {json.dumps(detail) if not isinstance(detail, str) else detail}"
    if isinstance(data, str) and data.strip():
        flat = " ".join(data.split())
        return f"{status} {flat[:200]}"
    return f"{status} (no body)"


class OmniClient:
    """The HTTP side of every tool: one call, one ``(status, body)`` answer.

    :param settings: Where the server is and the token to send.
    :param http: An ``httpx.AsyncClient`` to use instead of one built from
        *settings* (tests point it at the app through ``ASGITransport``).
    """

    def __init__(self, settings: Settings, *, http: httpx.AsyncClient | None = None) -> None:
        self.settings = settings
        self._http = http

    def _client(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = http_client(self.settings)
        return self._http

    async def aclose(self) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    async def call(
        self,
        method: str,
        path: str,
        *,
        json_body: Any | None = None,
        params: dict[str, Any] | None = None,
    ) -> tuple[int, Any]:
        """Send one request. ``0`` and an error string when it never arrived."""
        if not self.settings.configured:
            return 0, {"error": {"message": self.settings.problem}}
        try:
            response = await self._client().request(method, path, json=json_body, params=params)
        except httpx.HTTPError as exc:
            # The class name only: an httpx error's text can carry the request
            # URL and its query, and this answer goes to the agent.
            reason = exc.__class__.__name__
            return 0, {
                "error": {"message": f"{self.settings.server_url} did not answer: {reason}"}
            }
        text = response.text
        data: Any = text
        if text.strip():
            try:
                data = json.loads(text)
            except ValueError:
                data = text
        else:
            data = None
        return response.status_code, data


# ── the tools ────────────────────────────────────────────────────────────


async def park_fork(client: OmniClient, args: dict[str, Any]) -> Any:
    status, data = await client.call("POST", "/v1/ae/lot/park", json_body=args)
    if status == 200 and isinstance(data, dict):
        return data
    error = data.get("error") if isinstance(data, dict) else None
    error = error if isinstance(error, dict) else {}
    note = error.get("note")
    if not note:
        # The server did not get as far as building the note (unreachable, not
        # configured, refused the body), so the agent still gets one to file
        # by hand: the same lines the worker would have written.
        note = build_notes(
            NoteFields(
                summary=summary_for(args.get("summary"), str(args.get("state", ""))),
                thread=str(args.get("thread", "")),
                repo=repo_slug(args.get("repo")),
                state=str(args.get("state", "")),
                next=str(args.get("next", "")),
                slot=str(args.get("slot", "")),
                quadrant=str(args.get("quadrant", "")),
                est=str(args.get("est", "")),
                from_=str(args.get("from", "")),
            ),
            args.get("checklist"),
        )
    return {
        "outcome": "unfiled",
        "reason": _message(status, data),
        "note": note,
        "instruction": error.get("instruction") or UNFILED_INSTRUCTION,
    }


async def list_tags(client: OmniClient, args: dict[str, Any]) -> Any:  # noqa: ARG001
    status, data = await client.call("GET", "/v1/ae/lot/tags")
    if status == 200 and isinstance(data, dict):
        return data
    return {"error": f"Could not read the tags: {_message(status, data)}"}


async def link_issue(client: OmniClient, args: dict[str, Any]) -> Any:
    task_id = str(args["task_id"])
    issue_url = str(args["issue_url"])
    body = {"url": issue_url, "label": args.get("label") or "GitHub issue"}
    status, data = await client.call("POST", f"/v1/ae/lot/{task_id}/link", json_body=body)
    if status == 200 and isinstance(data, dict):
        return data
    return {
        "outcome": "not_linked",
        "reason": _message(status, data),
        "issue_url": issue_url,
        "instruction": NOT_LINKED_INSTRUCTION,
    }


async def list_parked(client: OmniClient, args: dict[str, Any]) -> Any:
    params = {"status": args.get("status") or "open", "limit": int(args.get("limit") or 20)}
    status, data = await client.call("GET", "/v1/ae/lot", params=params)
    if status == 200 and isinstance(data, dict):
        return data
    return {"error": f"Could not read the lists: {_message(status, data)}"}


async def complete_task(client: OmniClient, args: dict[str, Any]) -> Any:
    task_id = str(args["task_id"])
    body = {"status": args.get("status") or "done", "summary": str(args.get("summary") or "")}
    status, data = await client.call("POST", f"/v1/ae/lot/{task_id}/complete", json_body=body)
    if status == 200 and isinstance(data, dict):
        return data
    return {"outcome": "not_completed", "task_id": task_id, "reason": _message(status, data)}


async def omni_capacity(client: OmniClient, args: dict[str, Any]) -> Any:  # noqa: ARG001
    status, data = await client.call("GET", "/v1/ae/capacity")
    if status == 200 and isinstance(data, dict):
        return data
    return {"error": f"Could not read the capacity: {_message(status, data)}"}


async def omni_set_status(client: OmniClient, args: dict[str, Any]) -> Any:
    session_id = str(args["session_id"])
    value = str(args["status"])
    if value not in labels.STATUS_VALUES:
        return {"error": f"status must be one of {', '.join(labels.STATUS_VALUES)}; got {value!r}"}
    status, data = await client.call(
        "PATCH", f"/v1/sessions/{session_id}", json_body={"labels": {labels.STATUS: value}}
    )
    if status == 200 and isinstance(data, dict):
        stored = data.get("labels") if isinstance(data.get("labels"), dict) else {}
        return {
            "session_id": session_id,
            "status": value,
            "labels": labels.ae_labels_without_notes(stored),
        }
    return {"error": f"Could not set the status on {session_id}: {_message(status, data)}"}


def _item_text(item: dict[str, Any]) -> str:
    content = item.get("content")
    if isinstance(content, str):
        return content
    parts: list[str] = []
    for part in content or []:
        if isinstance(part, dict):
            text = part.get("text")
            if isinstance(text, str) and text:
                parts.append(text)
    return "\n".join(parts)


async def omni_transcript(client: OmniClient, args: dict[str, Any]) -> Any:
    session_id = str(args["session_id"])
    last_n = int(args.get("last_n") or DEFAULT_TRANSCRIPT_ITEMS)
    last_n = max(1, min(last_n, MAX_TRANSCRIPT_ITEMS))
    params = {"order": "desc", "limit": min(1000, last_n * _ITEMS_PER_MESSAGE)}
    status, data = await client.call("GET", f"/v1/sessions/{session_id}/items", params=params)
    if status != 200 or not isinstance(data, dict):
        return {"error": f"Could not read session {session_id}: {_message(status, data)}"}
    messages: list[dict[str, Any]] = []
    for item in data.get("data") or []:
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        text = _item_text(item)
        if not text.strip():
            continue
        messages.append(
            {
                "id": item.get("id"),
                "role": item.get("role"),
                "created_at": item.get("created_at"),
                "text": text,
            }
        )
        if len(messages) == last_n:
            break
    messages.reverse()
    return {"session_id": session_id, "count": len(messages), "items": messages}


# ── the orchestrator's tools (2026-09-25) ──────────────────────────────


def session_url(settings: Settings, path: str | None) -> str | None:
    """The session's page on the server Reid opens, from the route's ``/c/<id>`` path."""
    if not path or not settings.server_url:
        return None
    return f"{settings.server_url}{path}"


async def create_session(client: OmniClient, args: dict[str, Any]) -> Any:
    body = {key: value for key, value in args.items() if value not in (None, "")}
    if "orchestrator" not in body:
        # The runner puts the calling session's id in the MCP server's environment.
        caller = _caller_session()
        if caller:
            body["orchestrator"] = caller
    status, data = await client.call("POST", "/v1/ae/sessions", json_body=body)
    if status == 200 and isinstance(data, dict):
        return data | {"url": session_url(client.settings, data.get("path"))}
    error = data.get("error") if isinstance(data, dict) else None
    if status == 409 and isinstance(error, dict) and error.get("code") == "family_full":
        return {
            "outcome": "family_full",
            "reason": error.get("message"),
            "working": error.get("working") or [],
            "limit": error.get("limit"),
            "instruction": (
                "Nothing was started. Wait for one of your sessions to finish or set it to "
                "review, then start this one."
            ),
        }
    if status == 409 and isinstance(error, dict):
        instruction = (
            "Nothing was started. Give Reid the handoff note as a block to paste into a new "
            "chat, and tell him every slot is taken."
            if body.get("standalone")
            else "Nothing was started. Ask Reid with ask_reid: wait for a slot (recommended), "
            "park one of the working sessions, or start it anyway with ride_unparked."
        )
        return {
            "outcome": "slots_full",
            "reason": error.get("message"),
            "working": error.get("working") or [],
            "limit": error.get("limit"),
            "instruction": instruction,
        }
    answer: dict[str, Any] = {"error": f"Could not start the session: {_message(status, data)}"}
    if isinstance(error, dict) and error.get("session_id"):
        answer["session_id"] = error["session_id"]
        answer["url"] = session_url(client.settings, error.get("path"))
    return answer


async def post_message(client: OmniClient, args: dict[str, Any]) -> Any:
    session_id = str(args["session_id"])
    text = str(args["text"])
    if not text.strip():
        return {"error": "text is empty; nothing was posted"}
    event = {
        "type": "message",
        "data": {"role": "user", "content": [{"type": "input_text", "text": text}]},
    }
    status, data = await client.call("POST", f"/v1/sessions/{session_id}/events", json_body=event)
    if 200 <= status < 300:
        return {"posted": True, "session_id": session_id, "status": status}
    return {"error": f"Could not post to {session_id}: {_message(status, data)}"}


async def schedule_checkin(client: OmniClient, args: dict[str, Any]) -> Any:
    if not client.settings.configured:
        return {"error": client.settings.problem}
    http = client._client()
    try:
        existing = (await schedules.list_scheduled(http)).get(schedules.CHECKIN_NAME)
        if args.get("stop"):
            if existing is None:
                return {"name": schedules.CHECKIN_NAME, "action": "none", "state": "absent"}
            response = await http.patch(
                f"/v1/scheduled-tasks/{existing['id']}", json={"state": "paused"}
            )
            if response.status_code != 200:
                return {"error": f"Could not pause the check-in: {_message_of(response)}"}
            return {"name": schedules.CHECKIN_NAME, "id": existing["id"], "action": "paused"}
        zone, _ = await schedules.server_defaults(http)
        until_raw = str(args.get("until") or "").strip()
        try:
            until = date.fromisoformat(until_raw) if until_raw else None
            schedule = schedules.checkin_schedule(
                int(args.get("every_hours") or 1),
                until=until,
                session_id=str(args.get("session_id") or "").strip() or None,
                note=str(args.get("note") or "").strip() or None,
                timezone=zone,
            )
        except ValueError as exc:
            return {"error": f"Nothing was scheduled: {exc}"}
        agent_id = (await schedules.agents_by_name(http)).get(schedules.CHECKIN_AGENT)
        if agent_id is None:
            return {
                "error": (
                    f"The {schedules.CHECKIN_AGENT} agent is not registered on the server; add "
                    "agents/orchestrator to OMNIGENT_BUILTIN_AGENT_DIRS and restart."
                )
            }
        body = schedule.body(agent_id)
        if existing is None:
            response = await http.post("/v1/scheduled-tasks", json=body)
            action = "created"
        else:
            drift = {
                key: body[key]
                for key in schedules.MANAGED_FIELDS
                if existing.get(key) != body[key]
            }
            if existing.get("state") == "paused":
                drift["state"] = "active"
            if not drift:
                return _checkin_answer(existing, "unchanged")
            response = await http.patch(f"/v1/scheduled-tasks/{existing['id']}", json=drift)
            action = "updated"
    except schedules.SeedError as exc:
        return {"error": f"Could not reach the scheduled tasks: {exc}"}
    except httpx.HTTPError as exc:
        return {"error": f"Could not reach the scheduled tasks: {exc.__class__.__name__}"}
    if response.status_code != 200:
        return {"error": f"Could not save the check-in: {_message_of(response)}"}
    return _checkin_answer(response.json(), action)


def _message_of(response: httpx.Response) -> str:
    try:
        data: Any = response.json()
    except ValueError:
        data = response.text
    return _message(response.status_code, data)


def _checkin_answer(row: dict[str, Any], action: str) -> dict[str, Any]:
    return {
        "name": row.get("name", schedules.CHECKIN_NAME),
        "id": row.get("id"),
        "action": action,
        "rrule": row.get("rrule"),
        "timezone": row.get("timezone"),
        "state": row.get("state"),
        "next_run_at": row.get("next_run_at"),
    }


def _owner(session: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": session.get("id"),
        "title": session.get("title"),
        "status": session.get("status"),
        "blocked": session.get("blocked"),
        "failed": session.get("failed"),
    }


def _attention(pr: dict[str, Any]) -> str | None:
    """What the orchestrator owes this pull request, if anything."""
    if pr.get("state") == "merged":
        return "close_out"
    if pr.get("state") != "open":
        return None
    if (pr.get("mergeable") or {}).get("state") in ("conflicted", "behind"):
        return "rebrief_conflict"
    if (pr.get("ci") or {}).get("state") == "failing":
        return "rebrief_ci"
    if (pr.get("review") or {}).get("state") == "changes_requested":
        return "rebrief_review"
    return None


async def list_prs(client: OmniClient, args: dict[str, Any]) -> Any:
    errors: list[str] = []
    status, data = await client.call("GET", "/v1/ae/sessions")
    orchestrated: list[dict[str, Any]] = []
    if status == 200 and isinstance(data, dict):
        orchestrated = [row for row in data.get("items") or [] if isinstance(row, dict)]
    else:
        errors.append(f"orchestrated sessions not read: {_message(status, data)}")
    by_url: dict[str, dict[str, Any]] = {}

    params = {"fresh": "true"} if args.get("fresh") else None
    status, data = await client.call("GET", "/v1/ae/prs", params=params)
    server: dict[str, Any]
    if status == 200 and isinstance(data, dict):
        server = {"configured": bool(data.get("configured")), "note": data.get("note")}
        for item in data.get("items") or []:
            if isinstance(item, dict) and item.get("url"):
                owners = [_owner(ref) for ref in item.get("sessions") or []]
                by_url[str(item["url"]).lower()] = item | {
                    "sessions": owners,
                    "source": ["server"],
                }
        for row in data.get("errors") or []:
            if isinstance(row, dict) and row.get("url"):
                errors.append(f"{row['url']}: {row.get('message')}")
    else:
        server = {"configured": False, "note": f"GET /v1/ae/prs failed: {_message(status, data)}"}

    if server["configured"]:
        for raw in args.get("urls") or []:
            parsed = prs.parse_pull_url(str(raw))
            if parsed is None:
                errors.append(f"{raw}: not a github.com pull request URL")
                continue
            repo, number = parsed
            url = f"https://github.com/{repo}/pull/{number}"
            if url in by_url:
                continue
            status, card = await client.call("GET", f"/v1/ae/prs/{repo}/{number}")
            if status == 200 and isinstance(card, dict):
                by_url[url] = card | {"sessions": [], "source": ["server"]}
            else:
                errors.append(f"{url}: {_message(status, card)}")

    repos = {str(repo).strip().lower() for repo in args.get("repos") or [] if str(repo).strip()}
    repos |= {str(row["repo"]) for row in orchestrated if row.get("repo")}
    gh_state = await gh.status()
    if gh_state["available"] and repos:
        states = ["open", "merged"] if args.get("include_merged") else ["open"]
        for repo in sorted(repos):
            for state in states:
                try:
                    cards = await gh.pull_requests(
                        gh_state["binary"], repo, state=state, limit=30 if state == "open" else 10
                    )
                except RuntimeError as exc:
                    errors.append(str(exc))
                    continue
                for card in cards:
                    url = str(card.get("url") or "").lower()
                    if not url:
                        continue
                    held = by_url.get(url)
                    if held is not None:
                        held["source"] = [*held["source"], "gh"]
                        continue
                    by_url[url] = card | {"sessions": [], "source": ["gh"]}

    for row in orchestrated:
        row["url"] = session_url(client.settings, row.get("path"))
    for pr in by_url.values():
        if not pr["sessions"]:
            pr["sessions"] = [
                _owner(row)
                for row in orchestrated
                if row.get("repo") == pr.get("repo")
                and row.get("branch")
                and row.get("branch") == pr.get("head_ref")
            ]
        for owner in pr["sessions"]:
            owner["url"] = session_url(client.settings, owner["id"] and f"/c/{owner['id']}")
        pr["attention"] = _attention(pr)
        pr.pop("merge", None)
    items = sorted(by_url.values(), key=lambda pr: str(pr.get("updated_at") or ""), reverse=True)
    return {
        "prs": items,
        "sessions": orchestrated,
        "server_cards": server,
        "gh": {"available": gh_state["available"], "reason": gh_state["reason"]},
        "errors": errors,
    }


async def ask_reid(session: Any, args: dict[str, Any]) -> Any:
    question = " ".join(str(args.get("question") or "").split())
    options = [" ".join(str(option).split()) for option in args.get("options") or []]
    options = [option for option in options if option]
    if not question or not 2 <= len(options) <= 4:
        return {"error": "ask_reid needs a question and two to four options"}
    shown = [f"{options[0]} (Recommended)", *options[1:]]
    schema = {
        "type": "object",
        "properties": {
            "choice": {"type": "string", "title": question, "enum": shown},
        },
        "required": ["choice"],
    }
    fallback = (
        "Ask in chat instead: the question on one line, then the options numbered, "
        "recommended first. Wait for the answer."
    )
    try:
        result = await session.elicit(question, schema)
    except Exception as exc:  # noqa: BLE001 - a client without elicitation is a fallback
        return {
            "answer": None,
            "reason": f"pop-ups are not available here: {exc.__class__.__name__}",
            "instruction": fallback,
        }
    if result.action != "accept":
        return {"answer": None, "action": result.action, "instruction": "Reid dismissed it."}
    picked = str((result.content or {}).get("choice") or "")
    if picked in shown:
        picked = options[shown.index(picked)]
    return {"answer": picked, "action": "accept", "recommended": picked == options[0]}


async def set_session_model(client: OmniClient, session: Any, args: dict[str, Any]) -> Any:
    """Change a session's model or effort; on ``needs_approval`` ask Reid, and on a yes confirm."""
    session_id = str(args["session_id"])
    body = {
        key: args.get(key)
        for key in ("model", "effort", "reason")
        if args.get(key) not in (None, "")
    }
    caller = _caller_session()
    if not caller:
        # Without a caller the route would treat this as the browser and skip the rules.
        return {
            "error": (
                "Could not find this session's own id (OMNIGENT_RUNNER_PRIMARY_SESSION_ID is "
                "not set), so nothing was changed. Ask Reid to change the model in the picker."
            )
        }
    body["caller"] = caller
    path = f"/v1/ae/sessions/{session_id}/model"
    status, data = await client.call("POST", path, json_body=body)
    if status != 200 or not isinstance(data, dict):
        return {"error": f"Could not change the model: {_message(status, data)}"}
    if data.get("status") != "needs_approval":
        return data
    options = list(data.get("options") or [])
    if len(options) < 2:
        return data | {"instruction": "Ask Reid in chat whether to make this change."}
    asked = await ask_reid(session, {"question": data.get("question"), "options": options})
    if "reason" in asked:
        # No pop-ups on this client: hand the question back for the chat.
        return data | {
            "instruction": (
                "Ask Reid in chat: the question on one line, then the two options, recommended "
                "first. If he says yes, call set_session_model again."
            )
        }
    if asked.get("answer") != options[0]:
        return data | {"status": "declined"}
    status, confirmed = await client.call("POST", path, json_body=body | {"confirmed": True})
    if status != 200 or not isinstance(confirmed, dict):
        return {"error": f"Could not change the model: {_message(status, confirmed)}"}
    if confirmed.get("status") == "changed":
        confirmed["approved"] = "reid"
    return confirmed


async def report_review(client: OmniClient, args: dict[str, Any]) -> Any:
    """Record the lead's review counts for the preset run log, as the calling session."""
    caller = (
        os.environ.get("OMNIGENT_RUNNER_PRIMARY_SESSION_ID")
        or os.environ.get("OMNIGENT_TERMINAL_LAUNCH_SESSION_ID")
        or ""
    ).strip()
    if not caller:
        return {
            "error": (
                "Could not find this session's own id (OMNIGENT_RUNNER_PRIMARY_SESSION_ID is "
                "not set), so nothing was recorded."
            )
        }
    body = {key: args.get(key) for key in REPORT_REVIEW_SCHEMA["required"]}
    if args.get("call_id") is not None:
        body["call_id"] = args["call_id"]
    status, data = await client.call(
        "POST", f"/v1/ae/sessions/{caller}/review-report", json_body=body
    )
    if status != 200 or not isinstance(data, dict):
        return {"error": f"Could not record the review: {_message(status, data)}"}
    return data


def _caller_session() -> str:
    return (
        os.environ.get("OMNIGENT_RUNNER_PRIMARY_SESSION_ID")
        or os.environ.get("OMNIGENT_TERMINAL_LAUNCH_SESSION_ID")
        or ""
    ).strip()


_NO_OWN_ID = (
    "Could not find this session's own id (OMNIGENT_RUNNER_PRIMARY_SESSION_ID is not set), "
    "so nothing was {done}."
)


async def preset_insights_read(client: OmniClient, args: dict[str, Any]) -> Any:  # noqa: ARG001
    """The preset reviewer's one read, as the calling session."""
    caller = _caller_session()
    if not caller:
        return {"error": _NO_OWN_ID.format(done="read")}
    status, data = await client.call(
        "POST", f"/v1/ae/sessions/{caller}/preset-insights", json_body={}
    )
    if status != 200 or not isinstance(data, dict):
        return {"error": f"Could not read the preset insights: {_message(status, data)}"}
    return data


async def submit_preset_suggestions(client: OmniClient, args: dict[str, Any]) -> Any:
    """Submit the reviewer's suggestions, as the calling session."""
    caller = _caller_session()
    if not caller:
        return {"error": _NO_OWN_ID.format(done="submitted")}
    body = {key: args.get(key) for key in SUBMIT_PRESET_SUGGESTIONS_SCHEMA["properties"]}
    status, data = await client.call(
        "POST", f"/v1/ae/sessions/{caller}/preset-suggestions", json_body=body
    )
    if status != 200 or not isinstance(data, dict):
        return {"error": f"Could not submit the suggestions: {_message(status, data)}"}
    return data


HANDLERS = {
    "park_fork": park_fork,
    "list_tags": list_tags,
    "link_issue": link_issue,
    "list_parked": list_parked,
    "complete_task": complete_task,
    "omni_capacity": omni_capacity,
    "omni_set_status": omni_set_status,
    "omni_transcript": omni_transcript,
    "create_session": create_session,
    "post_message": post_message,
    "schedule_checkin": schedule_checkin,
    "list_prs": list_prs,
    "report_review": report_review,
    "preset_insights_read": preset_insights_read,
    "submit_preset_suggestions": submit_preset_suggestions,
}
# Tools that talk to Reid through the MCP session, not the server.
SESSION_HANDLERS = {"ask_reid": ask_reid}
# Tools that need both: the server for the change and the MCP session to ask Reid.
CLIENT_SESSION_HANDLERS = {"set_session_model": set_session_model}


def create_server(client: OmniClient) -> Server:
    """The MCP server over *client*: every tool in :data:`TOOLS`, each answer a JSON text block."""
    server: Server = Server(SERVER_NAME)

    @server.list_tools()
    async def _list_tools() -> list[types.Tool]:
        return list(TOOLS)

    @server.call_tool()
    async def _call_tool(
        name: str, arguments: dict[str, Any] | None
    ) -> Sequence[types.TextContent]:
        handler = HANDLERS.get(name)
        asker = SESSION_HANDLERS.get(name)
        both = CLIENT_SESSION_HANDLERS.get(name)
        if handler is None and asker is None and both is None:
            return _text({"error": f"unknown tool {name!r}; tools are {', '.join(TOOL_NAMES)}"})
        try:
            if both is not None:
                return _text(
                    await both(client, server.request_context.session, dict(arguments or {}))
                )
            if asker is not None:
                return _text(await asker(server.request_context.session, dict(arguments or {})))
            assert handler is not None
            return _text(await handler(client, dict(arguments or {})))
        except Exception as exc:
            _logger.exception("tool %s failed", name)
            if isinstance(exc, httpx.HTTPError):
                # Same as call(): an httpx error's text can carry the URL.
                return _text({"error": f"{name} failed: {exc.__class__.__name__}"})
            return _text({"error": f"{name} failed: {exc.__class__.__name__}: {exc}"})

    return server


async def serve(client: OmniClient) -> None:
    """Run the server over stdio until the client hangs up."""
    server = create_server(client)
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


def main() -> int:
    """``python -m ae_omni_policies.mcp``: stdio, settings from the environment."""
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
    settings = Settings.from_env()
    if not settings.configured:
        # Start anyway: the agent then reads why every call fails, instead of
        # the harness reporting a dead MCP with no reason.
        _logger.warning("%s", settings.problem)
    client = OmniClient(settings)
    try:
        asyncio.run(serve(client))
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())


__all__ = [
    "CLIENT_SESSION_HANDLERS",
    "HANDLERS",
    "SESSION_HANDLERS",
    "TOOLS",
    "TOOL_NAMES",
    "WRITING_TOOLS",
    "OmniClient",
    "create_server",
    "main",
    "serve",
]
