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
| `ae.status` | `working`, `blocked`, `review`, `parked`, `reference` | `slot_cap` claims `working` on a session's first prompt (Phase 1); `POST /v1/ae/lot/park` sets `parked`; the Parking Lot page (Phase 3) writes any value through an `ae.*`-only labels PATCH (Park, Done, Blocked, Working, Reference); the observer (Phase 4) maps a glyph on a `Title: <glyph> <name>` line or a glyph-prefixed rename to the status, and a leading glyph on the session's own title once, while the session has no status; the stdio MCP server's `omni_set_status` (Phase 5, 2026-09-23) writes any of the five values through upstream's `PATCH /v1/sessions/{id}` labels, for a person or an interactive session and never for the two scheduled agents, whose allowlists leave it out; an agent YAML's `labels: {ae.status: reference}` seeds it before the first prompt (the steward, the shutdown note, reviewer-codex, preset-reviewer), which is how a scheduled run is placed without holding a slot; the sidebar's Status submenu (patch P4, 2026-09-24) writes any of the five through the same PATCH for the session's owner, label-only like the page, and asks `/v1/ae/capacity` before `working` | |
| `ae.started` | epoch seconds | `slot_cap` when it claims a slot | |
| `ae.overflow` | `1` | `slot_cap` when a fourth session runs anyway (`ride unparked`, or the warn-mode ASK accepted) | |
| `ae.triage` | `off`, `capture`; `""` clears | `hurry_modes` on `triage off` / `triage capture` / `triage on` | |
| `ae.task` | task-store id | `POST /v1/ae/lot/park` with a `session_id` | |
| `ae.gate` | `open`, `answered` | the observer sets `open` on an assistant line starting `FORK:` and `answered` on the next user turn (Phase 4) or, since 2026-10-23, on a pick in the gate's pop-up question (see `ae.gate_ask`), which lands as a tool result rather than a user turn; `POST /v1/ae/gate/{id}/reply` sets `answered` after posting the letter (the page's gate buttons call it, Phase 3; labelled A / B / C / Close until 2026-09-27, now Do it now / Write a note / Save for later / Drop it) | |
| `ae.gate_ask` | the call id of the gate's pop-up; `""` clears | the observer, from the first `AskUserQuestion` call while the gate is open; its result answers the gate when it begins with a pick (`Your questions have been answered` or `The user answered:`), and clears it either way, as do a new `FORK:` line and a typed answer (2026-10-23) | |
| `ae.fork` | the `FORK:` clause, one line, 200 chars | the observer, from the last `FORK:` line of the turn (Phase 4) | |
| `ae.next` | the last `NEXT:` line, one line, 200 chars | the observer, from an assistant turn carrying both a `THREAD:` and a `NEXT:` line (Phase 4) | |
| `ae.absorbed`, `ae.unfiled` | counts | the observer, one per assistant line starting `Absorbed:` or `UNFILED`, continuing from the current count (Phase 4) | |
| `ae.absorbed.<n>`, `ae.unfiled.<n>` | the line's text after its token, one line, 200 chars | the observer, one label per counted line, `<n>` being the count that line brought its counter to; the first 20 of each kind, since every label rides in each sidebar row (2026-10-19). The session rail's Triage card lists them, so a note stays readable after its message is too old to be loaded. `scripts/backfill-triage-notes.py` (`just triage-notes-backfill`) saves them for lines counted before then, in each session the caller owns (not side chats, which the session list leaves out), and only for a counter whose history holds exactly as many lines as it counts | |
| `ae.orch` | the starting session's id, or `1` when it gave none (since 2026-10-24 the `create_session` tool fills it from the calling session's runner environment when the lead passes none; a long-lived MCP process keeps the id of the session it was started in, which is the same owner, so it is an attribution limit and not a permission one; the lead must own the session it names, else the child records `1`; a `standalone` start, a handoff chat the lead opens for Reid since 2026-10-25, always records `1` and takes its role, `lead` by default, from the preset it opens on, the opener lending only its own model and effort to a `session` role) | `POST /v1/ae/sessions` (the orchestrator's `create_session`, 2026-09-25) on every session it starts. A fork copies it, so a fork of a child stays in the family | |
| `ae.orch_key` | the idempotency key, 64 chars (the branch name by convention; `auto-` and a hash when none is given) | the same route; a create whose key matches a live session (status working, blocked or unset) returns that session instead | |
| `ae.repo`, `ae.branch` | `owner/name` lowercased; the branch the session pushes | the same route, when the orchestrator gives them; `list_prs` matches a pull request to its session by them | |
| `ae.role` | `lead`, `planner`, `reviewer`, `builder`, `helper` | `POST /v1/ae/sessions` (the `create_session` tool's `role`, 2026-10-24) on a session started with a role; unset otherwise. The role fills the session's model and effort from the caller's preset: a fixed setting gives its model and effort, `session` the caller's own (its harness too, since 2026-10-25), `auto` and lead-picks leave both unset, and a reviewer slot run by Codex starts the Codex agent. An explicit `model` or `effort` wins. A `standalone` start is `lead` unless told otherwise, and takes the role from the preset it opens on | |
| `ae.preset` | a role preset id such as `balanced`, or `lead-picks` | `POST /v1/ae/sessions` on every session it starts (the request's `preset`, else the owner's default, which is what the child resolves without an `ae.team` label); `PUT /v1/ae/sessions/{id}/preset` with body `{"preset": id}` on a switch (returns `{"ok": true, "preset": id}`, 403 unless the caller owns the session (or is an admin), 404 for an unknown one, 422 for an id that is not one of the owner's presets or `lead-picks`, and a no-op when the label already holds the id). Each set or switch appends one line `{"session", "from", "to", "at", "by"}` to the preset log (`AE_PRESET_LOG_FILE`, default `/data/ae-preset-log.jsonl`, rotated to `.1` past 5 MB); a log failure never fails the change. A request that carries `preset` also writes the child's `ae.team` label so it opens on that preset | |
| `ae.model_start` | `<model>\|<effort>` (an empty part when the session ran its default) | `POST /v1/ae/sessions/{id}/model` stamps it the first time the session's model or effort changes, from the values before the change. A lead that stepped itself down may climb back to this value without asking; anything above it follows the preset's rule | |
| `ae.model_last` | compact JSON under 256 characters: `{"from","to","kind","reason","at","caller","approved","undo","undone"}` (`from` and `to` are `{model, effort}`, `at` is unix seconds, `undo` is the raw override pair before the change) | `POST /v1/ae/sessions/{id}/model` rewrites it on every successful change (raise, lower, lead step-down, Undo, which sets `undone`); `reason` is cut so the value fits. The change band reads it, and a line stays for 24 hours | |
| `ae.raises` | integer string | `POST /v1/ae/sessions/{id}/model` adds one for each raise made on the lead path (a bigger model family, or the same family at higher effort). Lowers, undo and the browser path never count. Once it reaches the preset's `auto_raises`, the next raise needs Reid's approval | |
| `ae.raise_cap` | integer string | Set with `ae.raises`: the caller's preset `auto_raises` when the raise was counted | |
| (route) | `POST /v1/ae/sessions/{id}/model` | Body `{"model", "effort", "reason", "caller", "confirmed", "undo"}`. The browser path (no `caller`, for example Undo) needs ownership of the session and always applies. The lead path (the `set_session_model` tool sets `caller` from its own session id) needs ownership of both sessions, and the target must be the caller or carry `ae.orch` equal to it; the caller's preset `rules` decide (`raise_or_lower` off refuses, `ask` needs approval, `tell` applies and says so). A Codex helper (agent `codex-native-ui`) takes effort changes only. Answers 200 with `status` `changed`, `needs_approval`, `refused` or `unchanged` (403 and 404 for access, 422 for a bad body). Each change appends a line `{"session", "caller", "by", "from", "to", "kind", "reason", "approved", "at"}` to the model log (`AE_MODEL_LOG_FILE`, default `/data/ae-model-log.jsonl`, rotated to `.1` past 5 MB); a log failure never fails the change | |
| (route) | `GET /v1/ae/sessions/{lead_id}/model-changes` | Needs ownership of the lead (403, 404 for an unknown session). Answers `{"changes": [...]}`, newest first: the lead and every top-level session whose `ae.orch` is the lead, each with a parseable `ae.model_last` from the last 24 hours, as `{"session_id", "title", "role", "is_lead", "change"}`. Reads labels only, never the model log | |
| (route) | `POST /v1/ae/sessions/{id}/review-report` | Body `{"reviewers_run", "findings_returned", "findings_fixed"}` (non-negative integers), plus an optional `call_id` (1 to 100 characters, the review round's name; since 2026-10-25, https://github.com/analyticendeavors/omnigent-ae/issues/328). The `report_review` tool posts it as the calling session after the lead's final review (the roles notice asks for it); a family child reports for the lead that started it (`ae.orch`). Needs ownership (403, 404 for an unknown session, 422 for a bad body). The resolved lead (after the `ae.orch` walk) must also be writable by the caller, else 403. Calls in one run add up in the preset run sweep's state file until the run's line is written, except a call whose `call_id` the run already counted (answered `repeat` with the totals unchanged; the run remembers its last 100 ids, before and after the line); a report within 30 minutes after the line was written appends an amended line (same `run_key`, `rev` one higher), an older one is dropped, and nothing is kept while the owner's insights are off. Answers `{"ok", "session_id", "status" (`stored`, `amended`, `dropped`, `repeat`, `off`), "note", "totals"}` | |
| (file) | `/data/ae-preset-runs.jsonl` | The preset run log (`AE_PRESET_RUNS_LOG_FILE`, empty turns it off; rotated to `.1` past 5 MB). A 60 second sweep (`preset_runs.py`, state in `AE_PRESET_RUNS_STATE_FILE`, default `/data/ae-preset-runs-state.json`, `AE_PRESET_RUNS=0` turns the sweep off) appends one line when a lead session (not a family child) first reaches `ae.status` `review` or `reference`, or is archived after being seen working; a lead that ends again appends a new line, and a reader keeps the last per session. Written only while the owner's `insights.enabled` (the roles list) is true. A lead first seen already ended more than 7 minutes ago is marked without a line, and a lead with no cost, no agents and no items is not logged. An end is marked written only after its line is in the log; a line whose `run_key` is already in the last 64 KB of the log is not appended again. The family walk goes down `ae.orch` and parent links to depth 5 and stops at 200 sessions (a warning is logged); a native subagent's model and effort are read from the lead's last 500 tool calls, and are null when the call is not in them. Line (plus `run_key` and `rev`; the preset, `fingerprint` and `revision` are taken when the run is first seen live): `session`, `user`, `title`, `preset` (an id, `custom`, or `lead-picks`), `fingerprint`, `revision`, `lead` (`model`, `effort`), `wall_s`, `active_s` (always null), `cost` (`total_usd`, `by_model`), `agents` (`session`, `kind` of `session`, `codex` or `subagent`, `role`, `model`, `effort`, `seconds`, `cost_usd`, `hung`), `model_changes` (from the model log), `review` (`reported`, `reviewers_run`, `findings_returned`, `findings_fixed`, `reviewer_sessions`), `outcome`, `files_touched` (always null), `at`. A child that is running with no update for 900 seconds is marked `hung`, sticky for the run. A `preset-reviewer` run (the scheduled preset review) writes no line: it is skipped so the review never counts as a preset run | |
| (route) | `GET /v1/ae/presets?days=7\|30` | Preset insights for the Admin Center's Presets card (`preset_stats.py`, 2026-10-24). Admins only; any other `days` is 422. Reads the run log above (both files, the highest `rev` per `run_key`, `at` inside the window) and answers per preset the runs, median wall time and cost, cost by model, raises and step-downs, hung runs, a daily cost series and the newest 50 runs, plus at most three fixed-rule takeaways (a preset with 5 or more runs whose median cost or time is 25% or more off the rest, and a role hung in 3 or more runs). With no run in the window it answers `"mode": "fallback"` from session totals and the model log, with no takeaways. Never waits: it answers the cache and recomputes in a thread when the cache is over 5 minutes old, and answers `{"state": "warming"}` when there is none (or `{"state": "error", "note"}` when the first recompute failed). Log mode covers only users with insights on; fallback reads at most 2000 sessions (archived ones included). Each run keeps the `at` of its first line, and each recent run carries `started_at` (end time minus `wall_s`) | |
| (file) | `/data/ae-preset-stats.json` | The cached answer of `GET /v1/ae/presets` for each window (`AE_PRESET_STATS_FILE`, empty keeps it in memory only). Written (atomic replace) before the in-memory cache is replaced; a restart serves it instead of warming | |
| (file) | `/data/ae-preset-suggestions.json` | The preset reviewer's suggestions (`AE_PRESET_SUGGESTIONS_FILE`; memory only when `/data` is absent; `preset_suggestions.py`, 2026-10-24). Shape `{"version": 1, "reviews": [...], "suggestions": [...], "forced": {owner: epoch}}`; written atomically, a corrupt file is moved aside. Caps: 50 reviews (the newest per owner is never dropped), 300 suggestions (oldest closed ones go first), 5 suggestions per review, 6 changes per suggestion, 2000 bytes per change value, and the Run now flag expires after 30 minutes. A suggestion's status is `open`, `applying`, `applied`, `undoing`, `undone`, `dismissed`, `issue_opening` or `issue_opened`; Apply and Undo write the `-ing` status before the preset changes and the final one after, and a later try settles a record left in between by comparing the live preset | |
| (route) | `POST /v1/ae/sessions/{id}/preset-insights`, `POST /v1/ae/sessions/{id}/preset-suggestions` | The `preset_insights_read` and `submit_preset_suggestions` tools of the `preset-reviewer` agent. Both need the caller to be able to write the session and the session to run `preset-reviewer` (403 `not_reviewer`). The read answers `due` and, when due, the runs, model changes and presets in scope (run titles cut to 80 characters and reasons to 120, control characters removed). A submit stores up to five suggestions (`own`, `starter`, `no_change`), refuses a second one from the same session (409), and renames the session `Preset review <Mon D>` | |
| (route) | `GET /v1/ae/preset-suggestions[?all=true]`, `POST /v1/ae/preset-suggestions/{id}/apply\|undo\|dismiss\|issue` | The caller's suggestions (every user's with `all=true`, admins only), each with `can_apply`, `can_undo` and `changes_text` (one line per change). Apply and Undo need the owner and answer the new roles plus `insights_task`; 409 `preset_changed`, `preset_gone`, `conflict`. Dismiss takes an `open` or `undone` suggestion (owner, or an admin). Issue (admins) opens a GitHub issue for a `starter` suggestion in `AE_PRESET_ISSUE_REPO` (default the private `analyticendeavors/omnigent-ae`); 409 `repo_not_private` unless the repo is private, 409 `issue_in_progress` while another click holds it | |
| (route) | `POST /v1/ae/preset-review/run`, `GET /v1/ae/preset-review` | Run now makes sure the review task exists, flags the next read due and fires the task (202; 409 `insights_off`, `task_paused`, `task_missing`, `review_running`; 502 `run_failed`). Status answers the task, last and next review, the reviewer model and the open count. `PUT /v1/ae/roles` also creates, patches or deletes the task (`insights_task`) | |
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
- **A family holds one slot** (2026-10-23). A family is a session and every
  session whose `ae.orch` chain leads to it (`sessions.family_key`); the lead
  holds the slot and the sessions it started ride inside it, so a lead and three
  children count once. Reid decided it while planning role routing: the slot
  limit counts pieces of work he tracks, and a family is one, reporting through
  its lead. The slot cap, `POST /v1/ae/sessions` and `GET /v1/ae/capacity`
  (`slots_used`, and a `family` key on each working row) count this way; so do
  the web counts (patch 0161), and the Session Dashboard's header draws one slot
  block per family, led by the lead, with `+N` for the rest. To keep one lead
  from fanning out without end, a family may have `family_children` sessions
  past its lead busy at once (a helper that finished its turn does not count,
  but one started under a minute ago does; 3 unless the slot cap policy sets it on
  Settings > Global Policies, 0 lifts it), counting creates and first prompts
  still in flight: `POST /v1/ae/sessions` answers 409 `family_full` past it and never `slots_full`
  when it names a lead (2026-10-24), and
  the slot cap never holds a member's first prompt (the lead is the one session),
  it marks the member `ae.overflow=1` instead. Only a session the caller
  may open can lead a family; another `orchestrator` is recorded as `1`. A fork
  of a family member copies its `ae.orch` and stays in the family; a fork of any
  other session is related in the sidebar (through `omnigent.fork.source_id`)
  but keeps its own slot, since a fork is often a new piece of work.
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
