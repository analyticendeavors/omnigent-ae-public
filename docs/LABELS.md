# Session labels: the `ae.*` vocabulary

Status is a label on the session, never a title glyph. OmniGent keeps labels per
conversation (`conversation_labels`, one atomic upsert per batch), pushes changes to
the web UI, and hands every policy a read-only snapshot in
`event["context"]["labels"]`. The `ae.` namespace is free of upstream's reserved keys
(`omnigent.*`, `omni_project`). The code that owns these names is
`packages/ae_omni_policies/src/ae_omni_policies/labels.py`; Phase 2 mirrors it in
`web/src/lib/aeLabels.ts`. Written 2026-09-23 with Phase 1; the observer's and the
classifier's keys moved from "later writers" to written with Phase 4 (2026-09-23).

| Key | Values | Written by | Later writers |
|---|---|---|---|
| `ae.status` | `working`, `blocked`, `review`, `parked`, `reference` | `slot_cap` claims `working` on a session's first prompt (Phase 1); `POST /v1/ae/lot/park` sets `parked`; the Parking Lot page (Phase 3) writes any value through an `ae.*`-only labels PATCH (Park, Done, Blocked, Working, Reference); the observer (Phase 4) maps a glyph on a `Title: <glyph> <name>` line or a glyph-prefixed rename to the status, and a leading glyph on the session's own title once, while the session has no status; the stdio MCP server's `omni_set_status` (Phase 5, 2026-09-23) writes any of the five values through upstream's `PATCH /v1/sessions/{id}` labels, for a person or an interactive session and never for the two scheduled agents, whose allowlists leave it out; an agent YAML's `labels: {ae.status: reference}` seeds it before the first prompt (the steward, the shutdown note, reviewer-codex), which is how a scheduled run is placed without holding a slot; the sidebar's Status submenu (patch P4, 2026-09-24) writes any of the five through the same PATCH for the session's owner, label-only like the page, and asks `/v1/ae/capacity` before `working` | |
| `ae.started` | epoch seconds | `slot_cap` when it claims a slot | |
| `ae.overflow` | `1` | `slot_cap` when a fourth session runs anyway (`ride unparked`, or the warn-mode ASK accepted) | |
| `ae.triage` | `off`, `capture`; `""` clears | `hurry_modes` on `triage off` / `triage capture` / `triage on` | |
| `ae.task` | task-store id | `POST /v1/ae/lot/park` with a `session_id` | |
| `ae.gate` | `open`, `answered` | the observer sets `open` on an assistant line starting `FORK:` and `answered` on the next user turn (Phase 4); `POST /v1/ae/gate/{id}/reply` sets `answered` after posting the letter (the page's gate buttons call it, Phase 3; labelled A / B / C / Close until 2026-09-27, now Do it now / Write a note / Save for later / Drop it) | |
| `ae.fork` | the `FORK:` clause, one line, 200 chars | the observer, from the last `FORK:` line of the turn (Phase 4) | |
| `ae.next` | the last `NEXT:` line, one line, 200 chars | the observer, from an assistant turn carrying both a `THREAD:` and a `NEXT:` line (Phase 4) | |
| `ae.absorbed`, `ae.unfiled` | counts | the observer, one per assistant line starting `Absorbed:` or `UNFILED`, continuing from the current count (Phase 4) | |
| `ae.absorbed.<n>`, `ae.unfiled.<n>` | the line's text after its token, one line, 200 chars | the observer, one label per counted line, `<n>` being the count that line brought its counter to; the first 20 of each kind, since every label rides in each sidebar row (2026-10-19). The session rail's Triage card lists them, so a note stays readable after its message is too old to be loaded. `scripts/backfill-triage-notes.py` (`just triage-notes-backfill`) saves them for lines counted before then, in each session the caller owns (not side chats, which the session list leaves out), and only for a counter whose history holds exactly as many lines as it counts | |
| `ae.orch` | the orchestrator's session id, or `1` | `POST /v1/ae/sessions` (the orchestrator's `create_session`, 2026-09-25) on every session it starts | |
| `ae.orch_key` | the idempotency key, 64 chars (the branch name by convention; `auto-` and a hash when none is given) | the same route; a create whose key matches a live session (status working, blocked or unset) returns that session instead | |
| `ae.repo`, `ae.branch` | `owner/name` lowercased; the branch the session pushes | the same route, when the orchestrator gives them; `list_prs` matches a pull request to its session by them | |
| `ae.autoresume` | the runner id an automatic resume was tried for | `auto_resume` when a host reconnects and it resumes a session that runner left interrupted (2026-10-13); a session whose `runner_id` still equals it is not tried again | |
| `ae.fork_suspect` | message hash (16 hex chars); `""` clears | `fork_suspect` when the policy model says a message starts a second deliverable; the next plain message clears it (Phase 4) | |

## Semantics

- **A slot is `ae.status=working`**, not a session whose live status is `running`:
  sub-agents and scheduled runs run without being slots. Only top-level, non-archived
  sessions are counted. The limit is `AE_SLOT_LIMIT` in `config/server.env`, three
  when unset (`SLOT_LIMIT` in `labels.py`). It became one setting on 2026-09-24 so
  four slots is one line: set `AE_SLOT_LIMIT=4`, then
  `deploy/compose.sh up -d --force-recreate omnigent`. The slot cap, `GET
  /v1/ae/capacity` and, through that route, the sidebar's Dashboard count, the
  session chip, the Status submenu and the Dashboard page all follow it; a value
  that is not a whole number of one or more stops the server at startup with the
  variable named.
- **Blocked is derived, not stored.** A working session whose
  `pending_elicitation_count` is above zero is waiting on the human, shows as blocked,
  and does not hold a slot until it resumes (decided 2026-09-23). The value `blocked`
  exists for the page and the MCP tool to set by hand; on the next prompt it counts
  again, like `parked`.
- **Failed holds no slot either** (2026-09-25). A working session whose last turn
  failed (upstream's `live_status` `failed`, the last turn status the relay observed)
  shows as blocked with `failed: true` on `/v1/ae/capacity` and is not counted, so a
  crashed session no longer holds a slot for hours unnoticed. Why: on 2026-09-24
  `omni_capacity` showed 3 of 3 used, one of them failed for 6h 34m. Like blocked,
  it is derived (`sessions.is_failed`), and the slot cap, the capacity route (and
  through it the MCP `omni_capacity` tool and every web count) and the
  orchestrator's `POST /v1/ae/sessions` all read the same helper; nothing writes
  `blocked` for it.
  Its next prompt counts again, like a blocked one's. Two readers do not derive it
  (checked 2026-09-24): the sidebar row icon (patch 0001's `aeDisplayStatus`) derives
  blocked from a pending approval only, so a failed session still shows the working
  icon there; and the Dashboard page's fallback, used only when `/v1/ae/capacity`
  does not answer, counts from labels alone.
- **Sessions the orchestrator starts are counted before they exist.**
  `POST /v1/ae/sessions` takes the slot cap's lock, counts the same rows and pending
  claims the policy does, holds a claim while it creates the session, and creates it
  already `working` (with `ae.started`), so the slot cap passes its first prompt
  without counting it twice. Full answers 409 `slots_full` unless Reid chose
  `ride_unparked`, which marks `ae.overflow=1`. Resume
  (`POST /v1/ae/lot/{id}/session`) is idempotent the same way since 2026-09-25: a
  live session already carrying the task's `ae.task` is returned, not duplicated.
- **The labels PATCH counts nothing.** The slot cap runs at prompt time, so a
  `working` written by hand is not checked by the server. The sidebar's Status
  submenu asks `GET /v1/ae/capacity` before it writes `working` and refuses at the
  limit with the working titles (decided 2026-09-24); `ride unparked` in a prompt
  stays the only way it lets one more run. The Parking Lot page's Working button (on a
  blocked card) does not check yet.
- **Placed sessions.** `working` already holds a slot and `reference` never takes
  one, so both pass without a count. Every other status (`blocked`, `parked`,
  `review`) goes through the count again on its next prompt: returning to a session
  is opening it, and with three working that means parking one first. `reference` is
  how an agent opts out of slots: seed `ae.status: reference` in its YAML
  (`guardrails.labels`), and the engine writes it before the first prompt.
- **Sub-agents run inside their parent's slot.** Server-wide policies reach every
  conversation, so the slot cap looks up the row it is evaluating (upstream's ambient
  session id) and abstains on any conversation that has a parent. Sub-agent rows are
  never counted and never labelled.
- **Claims are counted before they land.** The slot cap's label write is applied by
  the engine after the policy answers, so a claim is remembered in the server process
  for a minute and counted alongside the rows; two first prompts in the same instant
  no longer both pass. The window is one process; a second server process would not
  see it.
- **`""` means unset.** Policies cannot delete a label (`set_labels` is an upsert), so a
  key is cleared by writing an empty string, and every reader treats blank as absent.
- **Values are strings.** `ae.started` is epoch seconds as text; the store caps a value
  at 256 characters and the observer clips `ae.fork` and `ae.next` to 200.
- **Legacy glyphs.** Sessions imported from Claude Code default to `reference`; a
  leading title glyph (🟡 🔴 ✅ 📌 🗂️) maps to a status once, when the observer next
  sees an item on a session that has no status yet, and is stripped for display
  (Phase 2). An agent that still flips the glyph in a `Title:` line or a rename moves
  the status the same way. Nothing here ever writes a glyph back.
- **Tokens count at the start of a line only.** `FORK:`, `Absorbed:`, `UNFILED`,
  `THREAD:` and `NEXT:` are read exactly as ae-claude-skills'
  `scripts/test-triage-gate.sh` greps them (`^TOKEN`); a mention inside prose is not a
  gate. Only assistant turns are read for them; a user turn only answers an open gate.
- **The observer never gates.** A `response`-phase ASK behaves as a DENY at upstream's
  output gate, so the capture is an item observer (patch P3) that writes labels only;
  the human answers from the Parking Lot page or by typing the letter.
