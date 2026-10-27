# Plan limits (2026-09-24, second pass the same day)

Clicking the context ring under a session's composer opens a usage popover with
separate Context window and Plan limits sections. Context rows name the agent and
model for the current session and up to six collaborators. Each session has its own
window; the UI never adds their percentages together. Account limits follow in their
own section, with the active provider first. For Claude Code that is the 5-hour window, weekly for all models, weekly
per model ("Weekly · Fable"), and usage credits spent of the monthly cap, with the plan
(for example "Max 20x") beside the name; Codex keeps its own windows. Each row has a
bar, a percentage and a reset time. The Parking Lot page's capacity strip carries the
first two windows per harness in one line. Asked for by Reid on 2026-09-24 (parked as
https://github.com/analyticendeavors/omnigent-ae/issues/19); the second pass followed a
screenshot of Claude Code's popover the same day. Before this the way to see them was
the `>_` terminal and `/usage`.

## Collaboration and context verification (2026-10-13)

In an existing conversation, open **Auto** beside the model picker. **Change lead**
uses the existing agent switcher, which preserves the conversation and offers
registered agents such as the orchestrator. Native helpers remain available in
every mode; the picker controls additional collaboration, not whether the lead
can use its ordinary subagent tools.

- **Auto** (default): the lead decides whether extra collaborators are useful for
  the request, using configured, reachable agents. Saving Auto creates no sessions.
  The server also sends this preference when no selection has been saved.
- **Solo**: one lead with its native helpers; no new work is assigned to an added
  cross-agent team. Existing explicit Solo preferences stay Solo after the update.
- **Team**: choose collaborators and optionally pin their models. Saving provisions
  idle children, and the lead gets their roster with the next message. It assigns
  bounded tasks and integrates the results.

Save after the current turn finishes. Opening or cancelling the picker starts no
work. Each collaborator has its own conversation and receives relevant task context
from the lead, not a copy of the lead's entire context window.

The picker hides agents needing setup by default, with an explicit way to reveal
them. Selected unavailable agents stay visible so they can be removed. Connected
coding agents appear first; recommendations explain when another provider can give
an independent review. Other connected roles are behind a separate disclosure.
Custom agent names identify separately configured agents.

The picker selects agents and models, not OAuth accounts. The current subscription
configuration allows one login per CLI, and a child inherits the configured CLI
login. Two differently named Claude agents therefore do not establish two distinct
OAuth accounts or usage allowances. Separate account selection needs explicit
profile binding and a verified launch path; it is not implied by a second label.

Manual selections turn off the parent's automatic collaborator routing so it cannot substitute a
different model. A failed save leaves the prior roster and restores routing; a
retry reuses children already created. Returning to Auto or Solo restores the
routing setting that Team replaced, unless it was subsequently changed elsewhere.
For older Team preferences that did not record the prior setting, the override is
cleared to the server default. Neither action deletes or interrupts children.
The selected team stays with this session across an agent switch but is not inherited
by a fork.

The roster travels as framework context to Claude and SDK runners and as Codex's
`additionalContext` on native start/steer requests. This requires an app-server
supporting that experimental field. The lead needs session delegation tools; if
they are unavailable, the framework asks it to report that rather than claim team
work happened. Other registered harnesses can be selected as collaborators.

To check the UI:

1. In a Codex conversation with at least one reply, open the context ring. The
   current row should name Codex and its reported model, with its token count and
   context capacity. The Codex plan section should be first.
2. Select a team including Claude, send a task that can be divided, and inspect
   the child sessions. Opening the usage popup should show independent readings.
3. Switch the lead between providers while idle. The prior context count should
   clear immediately. A missing reading should say **Unavailable**, never 0%.
4. On a phone, scroll the popup to reach both providers' account limits. Check
   that the team dialog's Save and Cancel buttons remain reachable.
5. Open the team picker: Auto should be the default for a conversation without a
   saved preference. Select Team and confirm agents needing setup are hidden until
   requested. Select Solo and confirm its description retains native helpers.
6. Save Team, then Auto while idle. The saved roster should clear without deleting
   the children, and the prior routing preference should be restored.

Context sources are live session telemetry, then the session snapshot, then a
Claude Code status-line fallback only for the same Claude conversation. Old Claude
readings are never used for Codex; status-line readings over an hour old are marked
as possibly out of date. Snapshots refresh once a minute while the popup is open.
The cache billing breakdown remains specific to matching Claude Code telemetry.

Automated UI checks live in `tests/e2e_ui/visual/test_ae_collaboration.py` in core.
Run with a built Storybook or set `AE_UI_STORYBOOK_URL` to the local Storybook URL,
then run `pytest tests/e2e_ui/visual/test_ae_collaboration.py -m visual`.

## Where the numbers come from

Every source is a documented machine interface of a CLI. Nothing scrapes a TUI.

**Claude Code, the statusLine payload.** Claude Code pipes a JSON payload to the
statusLine command on every render. For a Claude subscription it carries

```json
"rate_limits": {
  "five_hour": {"used_percentage": 37.5, "resets_at": 1790266800},
  "seven_day": {"used_percentage": 72,   "resets_at": 1790550000}
},
"context_window": {
  "context_window_size": 1000000,
  "current_usage": {"input_tokens": 5000, "cache_creation_input_tokens": 1800,
                    "cache_read_input_tokens": 210000, "output_tokens": 900}
}
```

`rate_limits` is present after the first reply of a session, a window dropped once its
reset passes (the statusline documentation shipped in the CLI, checked against 2.1.282
on 2026-09-24). The values come from the rate-limit headers on the API responses, so
they work with the box's `CLAUDE_CODE_OAUTH_TOKEN`.

The relay reads that payload from two places, newest wins:

1. `~/.omnigent/ae/claude-statusline.json`, written by `deploy/ae-limits-statusline.sh`
   when it is the statusLine command in `~/.claude/settings.json`. This is how a plain
   `claude` in a terminal reports. Optional since the second pass.
2. The claude-native harness's own capture, `context_raw.json` in each session's bridge
   directory under `/tmp/omnigent-<uid>/claude-native/` (upstream
   `omnigent/harnesses/claude_native/bridge.py`, the `statusLine` block, and
   `status.py`). The harness writes it for every session whatever the settings say,
   and its `bridge.json` names the OmniGent session, which is how the popover finds
   its own session's context. The relay reads only `conversation_id` from that file;
   it also holds the bridge's token, which is never read into a snapshot.

**Why the second source (the "No reading yet" bug, 2026-09-24).** After the statusLine
was installed and the timer enabled, the popover still said "No reading yet" for
Claude after a reply in an existing session. The harness builds a session's settings,
including the chain to `~/.claude/settings.json`'s statusLine command
(`read_user_status_line_command`), once when it launches Claude Code. A session that
started before the settings change never runs the script, so the file in (1) was
never written and the relay had nothing to post. Reading (2) needs no settings change
and no new session.

**Claude Code, `get_usage`.** The per-model weekly windows, usage credits and the plan
come from the claude.ai usage endpoint, which the CLI only calls for a login holding
the `user:profile` OAuth scope. The relay asks the CLI through the stream-json control
request `get_usage` (`skip_behaviors: true`), which answers `subscription_type`,
`rate_limits_available` and `rate_limits` with `five_hour`, `seven_day`,
`model_scoped` (one `{display_name, utilization, resets_at}` per model) and
`extra_usage` (`{is_enabled, monthly_limit, used_credits, utilization, currency}`,
amounts in minor units, cents for USD); the schema is in the CLI (2.1.282). The CLI
starts as `claude -p --input-format stream-json --output-format stream-json
--setting-sources "" --strict-mcp-config --no-session-persistence
--disable-slash-commands`, so no hook, MCP server or transcript is involved and
nothing is sent to a model. The reply names the plan (`max`) but not its tier, so
`AE_LIMITS_CLAUDE_PLAN=Max 20x` in `config/host.env` sets the label.

The box's Claude Code runs on a `claude setup-token` token (`CLAUDE_CODE_OAUTH_TOKEN`,
inference-only), which cannot read that endpoint: the reply then says
`rate_limits_available: false`, the popover keeps the two statusLine windows and shows
a one-line hint. A stored login fixes it (below).

**Why a stored login does not change the sessions.** In the CLI's auth order
`CLAUDE_CODE_OAUTH_TOKEN` comes before a stored claude.ai login: the token resolver
returns the environment token before it reads `~/.claude/.credentials.json`, and
`claude auth status` lists `CLAUDE_CODE_OAUTH_TOKEN` ahead of `claude.ai` (checked in
the 2.1.282 binary on 2026-09-24; the login screen says the same, "new `claude`
sessions will keep using the old token"). The host unit loads the token from
`/secure/env/omnigent-host.env`, so every OmniGent Claude session keeps the setup
token. The relay's unit never loads that file, and the relay also strips
`CLAUDE_CODE_OAUTH_TOKEN`, `CLAUDE_CODE_OAUTH_TOKEN_FILE_DESCRIPTOR`,
`ANTHROPIC_API_KEY` and `ANTHROPIC_AUTH_TOKEN` from the probe's environment, so only
the probe uses the stored login. No token is written to the repo or printed; the
relay logs percentages only.

**Codex: `codex app-server`.** The app-server's `account/rateLimits/read` request
returns `rateLimits` (`primary` and `secondary` windows, each `{usedPercent,
windowDurationMins, resetsAt}`, and `planType`) and, on current builds,
`rateLimitsByLimitId` with one such snapshot per metered bucket
(`codex-rs/app-server-protocol/src/protocol/v2/account.rs`, openai/codex at 58838df).
The identity is the ChatGPT login in `~/.codex`; no model call is made.

## The path

```
Claude Code statusLine ──> deploy/ae-limits-statusline.sh ──> ~/.omnigent/ae/claude-statusline.json ─┐
claude-native harness ──> /tmp/omnigent-<uid>/claude-native/*/context_raw.json ────────────────────────┤
claude -p (stream-json) <── get_usage ──────────────────────────────────────────────────────────────────┤
codex app-server (stdio) <── account/rateLimits/read ─────────────────────────────────────────────────┤
                                                    python -m ae_omni_policies.limits_relay (timer, 5 min)
                                                                  │  POST /v1/ae/limits/{claude,codex}
                                                                  v
                             server: /v1/ae/limits (ae_omni_policies.router, limits.LimitStore,
                                     AE_LIMITS_STATE_FILE=/data/ae-limits.json)
                                                                  │  GET /v1/ae/limits
                                   ┌──────────────────────────────┴───────────────────────────┐
                    context ring popover (patches P6, P9)               Parking Lot strip (extension)
```

- `packages/ae_omni_policies/src/ae_omni_policies/limits.py`: the snapshot shape
  (windows, `credits`, per-session `contexts`, `hint`), the translations
  (`claude_snapshot`, `session_context`, `with_usage`, `codex_snapshot`), the store
  (newest reading per harness wins; mirrored to `AE_LIMITS_STATE_FILE`, else to
  `/data/ae-limits.json` when `/data` exists. The box's live `server.env` lacked the
  variable, so every deploy blanked the Usage card until the relay's next tick;
  the `/data` default closed that on 2026-10-20).
- `limits_relay.py`: the host side. Each harness is read and posted on its own; a
  failed `get_usage` probe is logged and the status line reading still posts. Since
  2026-09-24 a Codex read that times out or is refused is logged the same way and fails
  the unit only when nothing at all was posted: a slow `codex app-server` had left
  `ae-omnigent-limits` failed every five minutes while the Claude reading posted fine.
  The same pass kills each CLI's whole process group and closes its pipes while the
  event loop still runs, which removes the `Event loop is closed` traceback those runs
  ended with (the node `codex` launcher's child kept the pipes open after a timeout).
- `patches/0006`: `web/src/components/AePlanLimits.tsx` and two lines in
  `ChatPage.tsx` (the popover mounted beside upstream's `ComposerContextRing`, which it
  anchors to on click). `patches/0009` (second pass) changes only those P6 files: the
  context row reads the chat store the ring reads, so `ChatPage.tsx` is unchanged.
- `packages/ae_omni_extension/web/src/limits.ts`: the strip's compact line (unchanged;
  it shows the first two windows, which stay the 5-hour and the all-models weekly).

The popover shows both harnesses whichever session it is opened from: the limits
belong to the accounts. The context row belongs to the session: the ring's live
figures, and the cached / cache-write / new-input bar from that session's last status
line (Claude sessions only). Since patch 0040 (2026-10-14) the plan the session runs on
shows in full and the other plan sits behind "See detailed breakdown".

It closes on a press outside, Escape, a second click on the ring, and (since
2026-09-24) when its window loses focus. Radix only watches its own document, and in
the split view each pane is an iframe, so a click or Escape in another pane used to
leave it open.

## The ring's color and the prompt cache bar (patch P18, 2026-09-24)

Reid asked from his phone for two changes, and `patches/0018` makes both.

**The ring changes color near a full context.** Upstream draws the ring grayscale on
purpose; Claude Code desktop instead warns as the window fills, and that is the cue
Reid wanted before a compaction or a fresh session. The used arc is grey below 60%,
the theme's warning color from 60% and its destructive color from 80%
(`web/src/lib/aeContextTone.ts`, `contextTone`), decided on the rounded percentage the
ring's tooltip prints. The popover's Context window bar uses the same steps, so the
two always agree. The plan limit bars keep their later steps, 70 and 90 (`limitTone`):
a context fills inside one session and the harness compacts it before 100%, while a
plan limit only stops work at 100%. Only the arc is colored; the button and its track
stay muted, and the tokens are the theme's `warning` and `destructive`, which exist in
light and dark mode.

**The thin bar is the prompt cache, not a second meter.** Under the Context window
bar sits a thinner one split into Cached, Cache write and New input: how the tokens of
the session's last request were sent (read from the prompt cache, written to it, or
new), from `context_window.current_usage` in the status line. In three greys it read
as a second usage meter. Since P18 each part has its own chart color (blue, green,
grey; never warning or destructive, which mean nearly full), the legend keeps its
dots, and a caption above it reads "How those tokens were billed (prompt cache)". The
parts are scaled to the whole window, so together they end where the bar above ends.

What Reid expected there was a `/context`-style breakdown of what fills the window
(system prompt, tools, memory files, messages). The status line payload carries only
the window size and the three input counts above, so that breakdown would need a new
source (for example Claude Code's `/context` output per session), and the popover does
not invent it.

## The shorter popover (patch 0040, 2026-10-14)

Reid found the popover too tall (933 px with his numbers, so it scrolled), picked five
fixes and then sent Claude Desktop's context popover as the model to follow. Patch
0040 changes only `AePlanLimits.tsx` and its suites.

- The "Usage" title is gone. The popover opens on "Context window" and "167.4k / 1M
  (17%)", the percent in the ring's tone.
- The prompt cache parts are joined segments inside the context bar itself, each sized
  by its share of the live figure (the status line's breakdown can lag the ring), with
  one legend line under the bar. P18's caption and second bar are gone. From 60% used
  the bar is one warning or destructive fill, so it still agrees with the ring; the
  legend still names the parts.
- Each plan limit is two lines: the label, then the reset and the percent ("Resets in
  21m" under a day, "Resets Wed 4:00 AM" after), with the bar under it. The full
  "Resets ... (in ...)" text is the row's title and the bar's label. "Weekly, all
  models" reads "Weekly · all models", whole amounts drop their cents ("$0 of $200")
  and "Updated 3m ago" reads "3m ago".
- The plan the session does not run on and the collaborators' context rows sit behind
  "See detailed breakdown", folded again each time the popover opens.
- The two explainer sentences sit behind info toggles beside the headings; a tap shows
  them, since a phone has no hover.
- The popover is 22rem wide. With Reid's numbers it is 371 px folded and 591 px open.

## The cleaner breakdown (patch 0047, 2026-10-15)

Reid's screenshot of the detailed breakdown on 2026-10-15 showed rows like
"general-purpose:a45b4b1759d6fb802", each "Unavailable" with a sentence and a repeated
"Claude Code" line, and he asked for the detail Claude's own surfaces give. Those rows
were Claude Code's own subagents: they run inside its process, and Claude Code reports
no context for them.

- Child sessions labelled `omnigent.wrapper=claude-code-native-ui-subagent` leave the
  collaborators list (before the six-row cap). A collaborator is named by its task
  summary or title, never a bare `type:hexid`.
- A collaborator without a reading is one line, "No reading yet", with the reason in
  its tooltip; a plan without a reading says "No reading yet" and keeps what to do
  about it (the relay timer, `codex login`) in its tooltip.
- Model ids read as names: "claude-opus-5-5[1m]" is "Opus 5.5".
- A Claude Code session on a 1M window shows "About 524k until auto-compact" under the
  context bar. Claude Code's docs give no percentage or reserve; native 1M models
  compact at about 967k tokens by default, and a changed `autoCompactWindow` (or
  `CLAUDE_CODE_AUTO_COMPACT_WINDOW`) would make the figure wrong. The box sets
  neither. A 200k window has no documented point, so it shows no countdown.

Still missing: the per-category breakdown Claude Code's `/context` shows (system
prompt, tools, MCP tools, memory files, skills, messages). The status line carries no
categories, and the only documented source is the Agent SDK's `get_context_usage` on a
separate Claude process; that work was handed to its own chat on 2026-10-15.

## Turning it on (as the host's user)

The server side ships with the image. The host side, once:

1. The relay in the host venv: `~/.omnigent-host/venv/bin/python -m
   ae_omni_policies.limits_relay --help`. The auto-update rebuilds the venv on any
   release that changes `packages/ae_omni_policies` or the upstream pin (since
   2026-09-24; before, it could skip the rebuild, see `deploy/README.md`); if the
   command is missing, run `deploy/install-host.sh`.
2. The timer:

   ```
   cp deploy/systemd/user/ae-omnigent-limits.{service,timer} ~/.config/systemd/user/
   systemctl --user daemon-reload
   systemctl --user enable --now ae-omnigent-limits.timer
   ```

3. Optional, for the per-model rows, credits and the plan: in OmniGent's `>_`
   terminal, `env -u CLAUDE_CODE_OAUTH_TOKEN claude auth login`, open the URL it
   prints, sign in with the Max account, paste the code back. Check with `env -u
   CLAUDE_CODE_OAUTH_TOKEN claude auth status` (`"authMethod": "claude.ai"`). Add
   `AE_LIMITS_CLAUDE_PLAN=Max 20x` to `config/host.env` for the tier in the header.
4. Optional, for plain `claude` in a terminal: point its statusLine at the script in
   `~/.claude/settings.json`:

   ```json
   "statusLine": {"type": "command", "command": "<checkout>/deploy/ae-limits-statusline.sh"}
   ```

   If a statusLine is already set, keep it by passing its command as arguments.

Check with `~/.omnigent-host/venv/bin/python -m ae_omni_policies.limits_relay
--dry-run` (prints what it would post) and `journalctl --user -u
ae-omnigent-limits -n 20` (one line per harness per pass, with the fix when one
fails).

## What the panel says when something is missing

| Panel | Cause | Fix |
|---|---|---|
| Claude: "No reading yet. Send one message in any Claude session..." | no session has rendered a status line since the relay started, or the timer is off | send a message; still empty after five minutes, step 2 |
| Claude: "reported no plan limits" | the last status line had no `rate_limits` (API key login, or no reply yet) | send a message in a Claude session |
| Claude: "Per-model limits and credits: run `claude auth login` on the box." | no stored login with the profile scope | step 3 |
| No "Context window" row | the ring has no figures and no status line capture exists for this session yet | send a message in the session |
| Codex: "No reading yet", journal says `codex app-server refused` or `exited before answering` | no ChatGPT login for `<box-user>` | `codex login` as `<box-user>` |
| "Updated 5h ago" on Claude | no Claude session has rendered since and no stored login | expected: the statusLine only runs while a session is active; step 3 makes the reading current every five minutes |
| "Could not load plan limits (404)" | the server predates this change | wait for the auto-deploy |

## Context categories (2026-10-15)

For a running claude-native session, opening the usage popover or the session rail's
Context or Connectors section (before 2026-10-17, **See detailed breakdown**) asks
`GET /v1/sessions/{id}/context-usage` for a context snapshot. The route checks edit
access and forwards to that session's runner. A new runner module starts a
non-persisting fork of its Claude transcript in the same workspace, carrying the
live process's model, MCP configuration, plugin paths and prompt settings. Hooks
and status-line writes are disabled. It sends `initialize`, waits for MCP startup
within a bounded budget, then sends `get_context_usage` with summary detail.
The runner caches successful snapshots for three minutes and coalesces concurrent
reads. No prompt is sent to a model. The existing five-minute limits relay stays
responsible for account usage and prompt-cache telemetry.

The panel lists every category returned by Claude, including system prompt,
system tools, MCP instructions and tools, custom agents, memory files, skills,
messages, free space and the autocompact buffer. Deferred tools are listed but do
not occupy the used bar. Tool, file, agent and skill counts expand below the rows.
After the response arrives, the header and segmented bar use the same snapshot;
the label identifies its time and estimated nature. The countdown uses Claude's
returned threshold and honors disabled autocompaction. Before a snapshot is
available, the existing context reading and estimated countdown remain available.

This reconstructs context from the persisted transcript and current configuration;
it cannot inspect unsaved state inside the interactive process. Summary counts are
estimates and differ from the textual `/context` report. Missing or still-connecting
MCP servers are called out. Offline runners and unsupported CLI versions show an
unavailable message; the rail's refresh or Try again button retries. Configuration and
credentials never enter the response or cache. The launch lookup currently targets
the Linux host's `/proc` interface; other platforms report unavailable.

### Research evidence

On `<box-hostname>` with Claude Code 2.1.301, the first structured summary answered in
0.355 seconds but arrived before MCP tools loaded. A second run with a five-second
startup wait took 5.313 seconds and included 106 MCP tools. The forked textual
`/context` candidate took 2.955 seconds, reported zero model turns and zero cost,
and returned rounded Markdown tables. Structured totals were 143,313 tokens;
the textual report estimated 140.8k. Both returned or implied a 33k buffer in a 1M
window. The structured response reported `autoCompactThreshold: 967000`.

The implementation's bounded startup probe took 2.34 seconds and returned 11
categories and 106 tools. The live transcript SHA-256, byte count and modification
time matched before and after the research probes. The implementation check also
verified both bridge context files remained unchanged. These checks demonstrate
preservation for the tested idle session, not exact parity with live in-memory
context. Maintained UI examples are in the usage-panel screenshot set (removed before publication).

Review hardening (2026-10-16): probe MCP bridge advertisements use a private temporary
directory, removed after the subprocess exits. Launch tool restrictions and prompt
file options are preserved. Failed probes have a 30-second cooldown. The panel
refreshes an expanded snapshot every three minutes and returns to live context
when folded. Empty MCP discovery remains incomplete.

The final isolation check took 2.323 seconds and returned 11 categories, totaling
163,181 tokens in a 1M window. All 19 files checked across the live bridge directory
and transcript remained byte-identical, including the bridge advertisement. That
session had incomplete MCP discovery, which the panel labels. Earlier transcript
and context-file checks did not cover bridge advertisements; the temporary bridge
fix closes that gap. Malformed category rows now reject the whole snapshot.

The expanded context window shows its color legend directly beneath the bar. Free space and the autocompact buffer use the same swatches as their bar regions. Deferred tools remain in the detailed rows because they do not occupy the bar.

## The session rail (patch 0071, 2026-10-17)

Reid sent Claude Desktop's session rail and Codex's usage popover on 2026-10-17 and
asked for the popover to keep basic bars while the full context, the environment,
connectors, outputs and changes move to a pane beside the chat that he can reorder.
Before the build he added triage, schedules and the files he sent.

The popover now shows, in order: the context window figure over one bar, colored by
category once a fresh snapshot exists for a claude-native session (otherwise the prompt
cache bar); the three largest categories plus Other; a lane per running collaborator,
each with its own percent and never summed, and a count of Claude Code's own subagents,
which report no reading; the auto-compact line with the model and the reading's age;
the session's own plan; and "Show details in session rail", which closes the popover
and opens the rail (on a phone, its sheet) on the Context section. "See detailed
breakdown" is gone; the other plan and the collaborators' details live in the rail.

The rail (`web/src/shell/AeSessionRail.tsx`, sections in `web/src/components/aeRail/`):

- Triage: `ae.status` as a switch (the sidebar's Status menu code, slot cap included),
  `ae.started` as "Started 2h 10m ago", the user turns once the whole history is
  loaded, "4th working" for `ae.overflow`, an open gate (`ae.gate`, `ae.fork`,
  `ae.fork_suspect`) with Do it now / Write a note / Save for later / Drop it through
  `POST /v1/ae/gate/{id}/reply`, the absorbed and unfiled counts from their labels side
  by side, the open one listing its lines below them (`patches/0098`, 2026-10-18): the
  notes the observer saved on the session (`ae.absorbed.<n>`, `patches/0100`,
  2026-10-19), then any line in the loaded messages not saved yet, `ae.next`, and the
  parked task from `ae.task`.
- Environment: the host and whether it is online, the workspace folder, the live branch
  from the GitHub panel's query, and the permission mode.
- Context: Claude Code's categories by size with the deferred tools apart, the MCP tool,
  memory file, skill and custom agent lists, the auto-compact countdown, Compact session
  (two clicks; the same `compact` event as `/compact`, offered only where `/compact` is),
  and the snapshot's model and age with a refresh button.
- Agents: each collaborator's own context window, then Claude Code's subagents by name
  (patch 0111, 2026-10-20). A running one carries one line on what it is doing: "Waiting
  on you" when it holds a prompt, else its newest tool call's own description ("Read
  header of patch 0041"), polled every 15 s; its newest message is only the prompt it
  was given, since its work lands as tool calls. Finished ones fold under a Finished
  group, closed until opened and remembered per browser, newest first, eight shown;
  rows cleared in the Agents tab stay hidden here too.
  "No agents have run in this session." means the list loaded empty; "Nothing running
  now." means only finished ones exist. Reid asked on 2026-10-20 to see running agents
  and a separate Finished group without opening the Agents tab; the old single count
  dropped a subagent the moment it finished, so a loaded list looked like an empty one.
  Any row opens the Agents tab.
- Scheduled: scheduled tasks whose prompt names the session (the check-in does) and the
  ones the session created with `sys_scheduled_task_create`, with Pause and Resume, and a
  "Before closing" line counting active schedules and running agents.
- Usage: both plans, the session's own first and tagged.
- Connectors: Claude Code's MCP servers from the snapshot, Codex's from
  `GET /v1/sessions/{id}/ae-mcp-status`; folded by default.
- Sent files: images and files on the session's loaded user messages.
- Outputs: files the workspace's change list marks created; eight before "Show all".
- Changes: the session's linked pull requests with state and checks, and the
  changed-file count, which opens the Changes tab.

Docking: the rail takes its own 304 px column whenever the window, less the open
sidebar, the open workspace panel and its gap, and an open files or logs panel, still
leaves the chat 480 px; otherwise it floats over the chat's right edge under a small
"Session" header that closes it, as Escape does. Patches 0075 and 0076 set this rule
on 2026-10-17. Before them, the rail floated below a fixed 1280 px window width
(1600 px with the workspace panel open), which covered the chat in a window narrowed
to one side of the screen even with the sidebar closed.

One pane at a time: the rail never floats over an open workspace, files or logs panel.
When the two do not fit side by side, the one opened last stays. Opening the rail
closes the panels (the workspace panel through its own header toggle, so as with that
click new sessions then start with it collapsed); opening a panel, narrowing the
window, dragging the sidebar or the panel wider, or switching to a session whose
workspace panel is open closes the rail and marks it `closedForPanel` in
`ae.sessionRail.v1`. A rail closed that way reopens by itself once no panel needs the
room (the panel closes, or the window leaves space for both), as it was before, so in
a window too narrow to dock it floats over the chat again; this holds across a reload.
Closing or opening it by hand clears the mark, so a rail the user closed stays closed. Patch 0083 set this rule on 2026-10-18, after the floating rail
covered half of an open workspace panel.

The swap slides (patch 0096, 2026-10-18): the rail uses the workspace panel's motion
(width, fade and a 12 px slide over 300 ms with `ease-in-out`, `.workspace-panel-motion`
in upstream's `index.css`). It slides in from width 0 through `@starting-style` and stays
mounted for the 300 ms slide out (`data-state="closed"`, `inert`). Both slides start
in the same frame, since the one-pane rule settles in a layout effect, so the chat's
width moves straight from one value to the other. Before this, the rail appeared at full
width beside a workspace panel still sliding out, and Reid's recording caught both on
screen with the chat squeezed to 110 px. A rail making way for the workspace panel as it
opens slides out in its own row. It leaves at once instead when it would float even
without the panel (it would slide across it), beside a files or logs panel (those take
their full width at once, so the slide would squeeze the chat), and beside a panel that
was already open (a narrower window, a session switch, a restore on load), which
`useOnePaneAtATime` tells apart by whether the panel was open the render before the
clash. The closing column is `inert` (set as an attribute; React 18 drops the prop), and
`prefers-reduced-motion` drops the slide. Since the v0.18 sync, upstream animates the
workspace panel only when it opens or closes through `setRightPanelOpenAnimated` (the header
toggle, a link, a tab) and snaps it on a session restore. The shell passes that flag as
`workspace.animating`, and only a panel sliding open makes the rail slide out (patch 0101,
2026-10-19). Before it, a switch to a session whose panel was saved open, with "Open the
session rail in every session" off, squeezed the chat to 181 px for about 280 ms.

Open in every session (patches 0085 and 0087, 2026-10-18, on by default): Reid had to
reopen the rail in session after session, because a session that restored its open
workspace panel closed the rail on any window up to about 1440 px wide. With the
preference on, a desktop rail opens whenever the user opens or switches to a session.
The restore rule (patch 0087): when the shell restores a session's workspace panel
(`aeRailRestoredPanelOpen`, called from AppShell's two restore points), a panel left open
that would not fit beside the rail starts closed for this view of the session. That
automatic decision is never saved as the user's choice: the session's own saved state
and the app-wide default stay as they were, so turning the setting off brings the panel
back exactly as the user left it. A file or comment link in the URL keeps its panel, and
the rail gives way to it when the two do not fit. A new chat's switch from its temp id to
the server id that create returned is not a session switch (the chat store records the
pair in `lib/aeTempPromotions.ts`); moving from an unsent new chat or from home to any
other session is. Every later panel open, by mouse, keyboard, hotkey
or the agent, follows the one-pane rule above: the pane opened last stays, and the rail
returns at the next session switch. Phones are unchanged: an auto-opened sheet would
cover the chat. The preference is `openInEverySession` in `ae.sessionRail.v1`, per
device, and older saved layouts load with it on; Settings > Chat, a section added for
chat options, carries the switch "Open the session rail in every session". Off, the rail
behaves as before.

When a session opens (patch 0155, 2026-10-22, Session rail by default): Reid still saw
sessions open on the files panel, not the rail. On a wide window the restore rule kept a
panel that fit, and before a resize measured it the shell's first restore assumed the
panel's 240 px minimum, so the panel came back and the rail made way for it. The switch
became a three-way choice, Settings > Chat > "When a session opens, show": Session rail
(`rail`, the default) opens the rail and starts a restored panel closed whether or not
the two fit; Both when they fit (`both`) is the old switch on, the restore rule above;
Workspace panel (`panel`) is the old switch off. In all three a file, agents or comment
link in the URL opens the panel, and the user can open either pane by hand afterwards.
A new chat's temp id draws no rail, so under Session rail it keeps the Both rule. The choice is `sessionOpenView` in `ae.sessionRail.v1`, per device; a layout saved with
the old switch off loads as Workspace panel, and any other layout as Session rail. The
logic stays in `lib/aeSessionRail.ts` (`aeRailRestoredPanelOpen`,
`aeRailSkipsRestoredPanel`, `aeRailOpensOnSwitch`); AppShell's two restore points are
unchanged.

Patch 0085 first closed the panels through the header toggle until the user's first
click in the session. The review on 2026-10-18 found that this blocked keyboard users,
closed panels the agent opened while the user typed, reopened the rail when a new chat's
id became real, and saved every automatic close as the user's choice and the app-wide
default. Patch 0087 replaced it with the restore rule.

The toggle's icon (patch 0085, 2026-10-18): the header toggle drew Tabler
layout-sidebar-right-expand and -collapse, a box with a divider, beside the workspace
panel's lucide panel-right, also a box with a divider, and Reid could not tell them
apart. The toggle and the phone menu's "Session rail" item now draw Tabler
list-details, one shape for both states; the button's pressed state and fill show
whether the rail is open. Reid picked list-details over info-square-rounded and
report-analytics from a sheet of real header shots.

Cost: a section fetches only while the rail (or the sheet) is open, the section
unfolded and the tab visible. The context snapshot sits in one cache keyed by session
and harness and model: one request at a time per key, reused for three minutes,
refreshed every three minutes while anything shows it, and on the refresh button. The
runner keeps its own three-minute cache as well.

While Claude starts (patches 0091 to 0093, 2026-10-18): a new session's first read
reached the runner before Claude was up, with no `context_raw.json` from the statusLine
shim, no session id in it, or no live Claude process yet. Each of those answered the
same "Could not read Claude context" sentence as a real failure, started the 30-second
backoff, and the web kept that answer as a good one for three minutes, so Context and
Connectors showed the error with Try again until a manual refresh. The runner now
answers those cases, and a session whose harness it has not loaded yet,
`{"available": false, "pending": true, "reason": ...}` with the actual not-ready reason
and no backoff, and it logs every real failure (the exception class and a first line
with paths cut to their last part). The server marks a runner-offline or timed-out
answer `"offline": true`. The web counts only an available snapshot (or a final answer)
as fresh and retries only a pending answer, quietly, after 2, 4, 8, 16 and then every
30 seconds for about two minutes; meanwhile Context shows "Reading context
categories…" (with the live percentage when the chat store has one) and Connectors
"Checking connectors…", with no error. When the window ends the runner's own reason
shows with Try again, and later automatic reloads do not open a new quiet window until
the session's status changes or someone clicks Refresh or Try again. Every other
unavailable answer shows at once: offline and a real probe failure with Try again, and
a final one (retrying can never fix it: another harness, or a host with no `/proc`)
with no button. Each turn end of the focused session (its status leaves running or
waiting for idle or failed, the edge `useFileContent` refreshes on), Refresh and Try
again read with `?fresh=true`, which skips the runner's three-minute cache, so a turn's
new context replaces the pre-turn snapshot. A section that stops watching drops its
loading state with its retries.

The probe already waited for `mcp_status` to settle; it now passes the servers on as
`mcpServers`, each only `{name, status}`, the status one of connected, failed,
needs-auth, pending, disabled or unknown. Codex reports MCP startup through
`session_mcp_startup` events, and upstream drops the map once nothing is starting,
failures included, so the server keeps the last settled map per session in memory
(`routes_ae_mcp_status.py`, fed by three lines in `_publish_mcp_startup`) and serves
names and statuses only, to editors of the session. The map is empty after a server
restart until the session starts its MCP servers again.

Gaps: OmniGent has no network-access setting, so Environment shows none; Outputs counts
a git workspace's untracked files as created whoever made them and cannot see files
written outside the workspace (a scratchpad, /tmp); Sent files and the absorbed lines
read only the messages the chat has loaded. Screenshots are in
the session-rail screenshot set (removed before publication).

## Usage per session account (patches 0078 and 0079, 2026-10-17)

Reid runs sessions on a backup Claude account ("AE Backup", a `claude setup-token`
token the runner injects as `CLAUDE_CODE_OAUTH_TOKEN`, patch 0046) and noticed on
2026-10-17 that the session rail's Usage showed his default login's numbers instead:
weekly 100% on the default login where the backup stood at 4%. The relay kept one
Claude reading per host and overwrote it with `get_usage`, which reads only the stored
default login, and `GET /v1/ae/limits` could not tell sessions apart.

Now:

- The runner's account environment carries `OMNIGENT_CLAUDE_ACCOUNT=<name>` beside the
  token, and the statusLine shim writes it to `claude_account` in the bridge directory
  on every render, just before `context_raw.json` (empty for the default login).
- The relay posts each session's own statusLine windows, account and label (read from
  the accounts file without loading a secret) in `sessions`, apart from `contexts`, so a
  status line with limits but no context reading (after `/clear`) still counts. The
  host-level reading prefers the newest capture from a default-login session, and names
  the account when only a backup session has a reading. The chained
  `claude-statusline.json` is read only when no session capture exists: it holds
  whichever CLI rendered last, on any account, and its script runs from
  `<checkout>`, a working clone that a deploy does not update.
- `GET /v1/ae/limits?session_id=<id>` answers with that session's account. A
  backup-account session gets its own 5-hour and weekly windows, no plan or credits,
  and the hint "Per-model limits, credits and the plan show only for the box's default
  login." (a setup token cannot read the usage endpoint). A default-login session keeps
  the host reading, its own account's and at least as new. Neither borrows the other
  account's numbers: with no windows of its own, a session gets none and a note.
  Without `session_id` the answer is unchanged.
- The store keeps a post when any part of it is newer than what it holds (the host
  reading or any session's), since the host reading can now be an idle session's older
  status line; a delayed retry, older in every part, is still refused.
- The rail and the popover ask for their own session and cache each session
  separately; the header names a backup account ("Claude Code · AE Backup").

Gaps: a session's numbers refresh only when it renders a status line, so an idle
session shows its last reading with its age, and before its first status line it has
none and shows the host reading. A session launched before patch 0078 has no
`claude_account` file and counts as the default login until it relaunches, which a
deploy does. A gateway or provider session (`ANTHROPIC_*` keys) also counts as the
default login, as every session did before; the box runs none. During a deploy the
new relay may post the new fields to the old server once, which answers 422 until the
server restarts; the next tick succeeds.

### A new chat before its first reply (patch 0202, 2026-10-26)

Gap closed. On 2026-10-26 a new chat on AE Backup showed AE Default's numbers (98%
weekly, and the "AE Default is at 98%" banner) until the relay's next 5-minute tick
posted the chat's first status line: `session_view` returned the host reading for any
session not in `snapshot.sessions`. Verified live that day: session b56f56a9 answered
account None / AE Default at 19:44, then `shared:AE Backup` after the 19:45 tick.

- The runner writes `claude_account` in the bridge directory at launch (to a temp file,
  then renamed, so it never races the shim's own write), from the same environment the
  terminal gets: empty for the default login, the account name otherwise. The relay's
  live scan therefore lists an unrendered session with its real account.
- `GET /v1/ae/limits?session_id=` also takes `account=` (empty for the default login). A
  session's account is found in this order: its entry in `snapshot.sessions` (the relay
  has seen it); the server label `omnigent.claude_native.account` a fork carries, which
  is trusted as it is (it was checked at fork time, so it needs no entry in the host's
  login list and has no age limit); the `account=` hint, which the Usage panels fill
  from the login picked for new chats (the host's `claude-oauth-active` row) and freeze
  at the first value seen for each session. The hint is the picker's value today, not
  the session's, so the server uses it only for a session created in the last 10
  minutes (`LIMITS_HINT_WINDOW_S`, two relay ticks; after that the relay lists every
  live session with its launch-time account). An older session without a relay entry,
  for example one whose terminal exited, gets the plain host answer. A hint for an
  account the host does not list as signed in is ignored too, and so is any hint or
  label for a session the caller cannot open.
- The session is answered from its own account's newest stored reading
  (`LimitStore._accounts`, the same readings `/limits/accounts` serves), never the host
  reading of another account. A backup account drops the plan and credits and keeps
  the backup hint. A backup session with no windows of its own now shows the account's
  newest reading, taken from another session on it, at that reading's time, with a note
  that says so (Reid's call, 2026-10-26: a stale number from the right account beats
  none). An account with no reading at all gets empty windows and "No reading yet for
  <label>; it shows after this session's first reply."
- `account=` without `session_id` does nothing; no web caller needs it.

The panels wait for the host's logins to answer (not stale cached ones, while a refetch
is in flight) before their first request, so neither the default login's numbers nor the
previous pick shows first. A session launched before 0202 has no file
until its first render, as before.

## Accounts summary (2026-10-21)

Reid runs several Claude logins (the default, personal ones, shared ones) and one
Codex login. Usage was visible only per session (the rail's Usage card,
`GET /v1/ae/limits?session_id=`), so finding the account with room meant opening
sessions one by one. The Admin Center's Accounts card now lists every signed-in
account in one place.

- The store (`limits.LimitStore`) keeps the newest reading per (harness, account)
  beside the per-harness snapshot, updated on every post, kept or not (a post that is
  older as a whole can still carry the only reading for one account). The Claude host
  reading fills the default login, or the backup account it names when only a backup
  session had a reading; each `sessions` entry fills its own account; Codex fills its
  one login. Newest `observed_at` wins, and a tie keeps the held reading. Plan and
  credits come only from the default login's usage endpoint; a newer session reading
  on that login keeps the last plan, never the credits (they would read as current
  under the newer time). A reading with no windows is ignored, so a status line from
  before a session's first reply cannot blank an account. Since the same day the
  default login's usage reading no longer folds in a backup account's status-line
  windows when only a backup session had a reading.
- The readings persist in the same state file under a new `accounts` key. A file from
  before the key loads unchanged, and its held snapshot fills the accounts on load.
- The relay posts the host's signed-in Claude logins with the Claude snapshot as
  `known_accounts` (each one's name and label from upstream's `account_descriptors`,
  never a token). Only the host can read that list: the server runs in its own
  container with its own home directory. Shared accounts come from the server's own
  store (`shared_accounts.py`), with the admin's label.
- `GET /v1/ae/limits` fills `harnesses.claude.account_label` from the held
  `known_accounts` when the reading carries no label, so a session on the default login
  reads "AE Default" in the usage strip, as the model menu and the Accounts card do
  (`account` stays null; without a host list the strip falls back to "Default CLI
  account"). `known_accounts` itself stays out of that answer.
- `GET /v1/ae/limits/accounts` (signed in, like `GET /v1/ae/limits`) answers
  `{now, accounts: [...]}`. Each row: `harness`, `account` (null for the default
  login), `label`, `plan`, `five_hour` and `weekly` (each a window with
  `used_percent` and `resets_at`, or null), `credits`, `observed_at` (null for a
  signed-in account with no reading yet) and `live_sessions` (the sessions the relay
  last saw on it: sessions with a status line capture, plus every running claude-native
  session whose `owner.pid` names a live process, so one that has not rendered yet
  (just after a deploy) still counts, the default login's included; null for
  Codex, where nothing counts them). Claude rows come first: the default login, the
  host's logins in its order, the shared accounts by label, then any other account a
  reading names. An account that has left the host's list or the shared store drops
  out unless a session still runs on it. Codex follows once its login has answered
  with windows. Labels, plans, percents and reset times only: never an email, a token
  or a secret's name. `GET /v1/ae/limits` leaves `known_accounts` out.
- The card (`packages/ae_omni_extension/web/src/accounts.ts`) shows each row's label,
  plan, a 5-hour and a weekly bar (amber from 70%, red from 90%, as `limitTone` in
  `AePlanLimits.tsx`; a plan with only one window, Codex on a weekly-only plan, draws
  just that bar, and "No reading" shows only when a read row has neither), live sessions and the reading's age ("as of 3h ago", "no
  reading yet"). It reads every minute while visible and on focus.

Gaps: an idle account's numbers are as old as its last status line; nothing probes an
account without a session (Reid's call, 2026-10-21). The login list rides the Claude
post, so a host with no Claude reading at all (no status line, no usage answer) posts
no list either, and only the shared accounts show. The live session counts are as old
as the relay's last post. A relay newer than its server gets 422 for every Claude post
until the server runs this version (the `known_accounts` field): deploy the server
first, as `auto-update.sh` does, and never roll the server back alone.
