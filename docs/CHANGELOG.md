# omnigent-ae changelog

The dated build diary that opened README.md until 2026-10-20: what was asked, what was built and why, Phase 0 to Phase 7 and then by date, as it was written (not in date order). New entries go here; the README stays a newcomer page. Paths are relative to the repository root unless they are links.

Phase 0 (2026-09-11): both repos exist, upstream pinned at `v0.13.0` (now v0.18.0, see `upstream.lock`), the patch
series is empty, CI proves the machinery.

Phase 1 (2026-09-23): `packages/ae_omni_policies` runs on upstream's own seams with no
core patch: the slot cap (`ae.status=working`, three at a time, warn or block on the
fourth prompt, `ride unparked` to run one anyway), the hurry modes, and the `/v1/ae`
routes (health, capacity, the Parking Lot over task-store, gate replies). Sessions
carry their status as the `ae.status` label (`docs/LABELS.md`).

Phase 2 (2026-09-23): the first core patch, `patches/0001` (P1, web). Every session
row in the sidebar prefixes a lucide icon for its `ae.status` (working CirclePlay,
blocked CirclePause, review CircleCheck, parked CircleParking, reference Archive),
blocked being derived from a working session with an approval prompt outstanding;
the primary navigation carries a "Slots n/3" pill counted from the sidebar's own
session cache, red at three, linking to the Parking Lot page that Phase 3 adds; a
leading legacy title glyph is stripped for display only. Screenshots from a real
server at the three viewports the handoff names are in `docs/screenshots/phase-2/`.

Phase 3 (2026-09-23): the second core patch, `patches/0002` (P2, extensions), and
the Parking Lot page. P2 gives extension pages a `server.request` host method behind
a permission of the same name: an allowlist over the host's authenticated fetch
(`GET` and `POST` under `/v1/ae/`, an `ae.*`-only labels `PATCH /v1/sessions/{id}`,
one user message to `POST /v1/sessions/{id}/events`; everything else, `DELETE`
included, is refused before any network call), plus `ae.*` labels and `archived` on
extension session summaries. `packages/ae_omni_extension` is the page
(`/extensions/analyticendeavors.parking-lot/lot`, the Slots pill's target): a sticky
capacity strip with Park / Done / Blocked, open gates with A / B / C / Close (relabelled
Do it now / Write a note / Save for later / Drop it on 2026-09-27), the lot
over task-store with Resume and a Park form, then Review, Parked, Unfiled and
Reference sessions. Its screenshots were removed on 2026-10-21: they showed a live task's client name.

Phase 4 (2026-09-23): the third core patch, `patches/0003` (P3, server), and the
capture side of the gate. P3 adds `omnigent/server/item_observers.py`, a registry the
server calls after it persists a harness's items (the native transcript bridge and the
runner relay's text flush, two lines each), fire-and-forget. `ae_omni_policies` gains
the observer (`observer.py`, registered when the package imports; a request-phase scan
is the fallback on an unpatched upstream): per assistant turn, a line starting
`FORK:` sets `ae.gate=open` and `ae.fork`, `Absorbed:` and `UNFILED` lines count into
`ae.absorbed` and `ae.unfiled`, a `THREAD:` / `NEXT:` note sets `ae.next`, a
`Title: <glyph> <name>` line or a glyph-prefixed rename maps the glyph to `ae.status`,
and a user turn while the gate is open sets `ae.gate=answered`; it never gates and
never writes a glyph back. The Parking Lot page's Open gates section therefore fills,
and its A / B / C / Close buttons post the letter as the session's next user turn (the
labels changed on 2026-09-27; the letters posted did not).
Two more policies: `fork_suspect`, a second-model hint through the server's optional
`llm:` block (any provider upstream supports; with no block it abstains, so the fork
runs without a model box) that never gates and only sets `ae.fork_suspect` for a chip;
and `rabbit_hole`, one ASK per session past four hours or sixty turns, `hours: 0` to
disable. The nine scenarios of ae-claude-skills' `scripts/test-triage-gate.sh` are
fixtures (`packages/ae_omni_policies/tests/fixtures/triage/`, ported by
`scripts/port-triage-fixtures.py`) replayed through the real app.

Phase 5 (2026-09-23): the agents, the schedules and the stdio MCP server, no core
patch. `ae_omni_policies.mcp` (`python -m ae_omni_policies.mcp`) exposes the task-store
worker's four tools with their names, input schemas and output shapes unchanged
(`park_fork`, `link_issue`, `list_tags`, `list_parked`) plus `omni_capacity`,
`omni_set_status` and `omni_transcript`, every one a thin call to the OmniGent server
(the server and token `omnigent login` recorded, or `OMNIGENT_SERVER_URL` and
`OMNIGENT_TOKEN` when they arrive), so the
agent's host never holds the Cloudflare Access token; two routes were added for it,
`GET /v1/ae/lot/tags` and `POST /v1/ae/lot/{task_id}/link`, and `/v1/ae/capacity`
now carries `today` in the lot's timezone. `agents/` holds three single-file specs:
`steward` (weekdays 08:00, the morning brief over the lot and the slots), `shutdown`
(weekdays 17:30, a THREAD / STATE / NEXT note per working session from its
transcript) and `reviewer-codex` (a Codex reviewer, off by default). The two scheduled
agents are propose-only by structure: their MCP allowlists carry only read tools
(`tests/test_mcp.py` holds them to it), they arrive placed as `ae.status=reference`
through the YAML's seeded label so the slot cap abstains, and their prompts carry the
"No human present" rule. Schedules are rows, so `just schedules-seed <server-url>`
creates or patches "AE steward" and "AE shutdown note" by name through
`POST /v1/scheduled-tasks` (`America/Los_Angeles`, `max_cost_usd` set). What runs
live needs the box host: with no host online, a manual run records a failed run,
which is what the suite asserts.

Phase 6 (2026-09-23): deployment hardening, no core patch and no pin move (the sync
drill against the next upstream tag is its own later PR). `deploy/` now holds what
the box runs: `docker-compose.override.yml` layered on upstream's compose file (the
published image by tag, `127.0.0.1:8010` because `:8000` and `:8001` are the Graphiti
MCPs on the box, `env_file` from `config/server.env` plus `/secure/env/omnigent-ae.env`,
bind mounts under `/secure/omnigent` for `/data` and the Postgres data directory,
`host.docker.internal` for Ollama), `compose.sh` (that command with every path filled
in), four systemd units (`ae-omnigent` for the stack after `/secure`, Docker and
Tailscale; `ae-omnigent-dump` at 02:40 local, ahead of the box's restic run at 03:10
and outside its timer collision map; `ae-omnigent-host`, a user unit running
upstream's own foreground service entry, which is what `omnigent host enable` writes,
not `--background`, whose detached daemon systemd could not supervise),
`pull-secrets.sh` (Key Vault to the two env files, 640 root:reid, atomically, no
value ever echoed; the vault names are the handoff's proposal and unverified),
`backup-omnigent.sh` (a consistent `pg_dump`, newest seven), `install-host.sh` (the
host's Python through `uv`, from the pinned submodule) and `README.md`, the runbook
from a bare box to a green `/v1/ae/health`, then upgrade and rollback.
`config/server.env.example` and `config/host.env.example` name every variable the
server and the host read, checked against v0.13.0. `.github/workflows/image.yml`
publishes `ghcr.io/analyticendeavors/omnigent-ae:<tag>` on an `ae-v*` tag push.
`tests/deploy` proves what a machine without Docker or systemd can (the override
parsed and merged, the units, the scripts through shellcheck, the workflow through
actionlint and the repo's standards). Found on the way and fixed after it
(2026-09-24): the agents' stdio MCP server starts with the MCP SDK's default
environment, so `OMNIGENT_SERVER_URL` never reaches it from the host unit; it now
falls back to the server `omnigent login` recorded under `~/.omnigent/` (Phase 5
below).

Phase 7 (2026-09-24): daily use, no core patch. The steward and the shutdown note
carry the box memory as a second MCP tool, `box_memory`: the business Graphiti graph
over streamable HTTP at `http://box.example:8000/mcp` (the tailnet name, because
Graphiti binds the box's Tailscale IP and not loopback), with an allowlist of its four
read tools; `add_memory`, the two deletes and `clear_graph` never reach an agent, nor
does the personal graph on `:8001` (`packages/ae_omni_policies/tests/test_agents.py` holds both). Each prompt
allows one memory call and carries on when it is unavailable. `docs/DAILY-USE.md` is
the page for everything after the deploy: adding a laptop or WSL host (`omnigent login`,
`omnigent host`, `host enable`; Windows needs WSL), `omnigent import --harness claude
--last 25` and where the sessions land, the phone (Tailscale, Safari, Add to Home
Screen, the Dashboard, PR cards, the Run and Paste buttons, Remote Control from the
Claude app), and travel mode with only an iPhone (rewritten
2026-09-24; it said laptop before): code in Claude Code on the web, box work in a
session on the box's host, root steps through Cockpit's terminal, what still needs
a keyboard, a pre-trip checklist with one reboot at home, and what to do when the box
is unreachable. Done when Reid opens the
lot on the phone and resumes a parked task into a session on a host; that last step
needs the box and a person, and is the checklist's final line.

After the first live deploy (2026-09-24), no core patch: the Parking Lot cards are
compact with a toggle for the details (remembered per task in `storage.user`),
Resume is the card's right-aligned action, the page's two columns are independent
stacks with Reference in the main column, and Resume starts the session itself
through `POST /v1/ae/lot/{id}/session` (Claude Code on the one online host, the
block as the first message, `ae.task` on the session) instead of opening a blank
composer. Its before and after screenshots were removed on 2026-10-21: they showed a live task's client name.
Since 2026-09-28 that first message ends by asking the session to call the
`complete_task` tool with the task id when the work is finished: it marks the task
done and closes its linked GitHub issue (route C's `link_issue`) with a comment. It
replaced a `Closes <issue URL>` line in the pull request, which GitHub ignores on a
merge into `dev`.

Session Status menu (2026-09-24): the fourth core patch, `patches/0004` (P4, web).
Right-clicking a session in the sidebar, or opening its kebab, shows a Status
submenu (Working, Blocked, Review, Parked, Reference) with the stored `ae.status`
checked, for the session's owner. It writes what the Parking Lot page's buttons
write, one label through upstream's `PATCH /v1/sessions/{id}`: Parked is label-only,
as the page's Park is (the note is filed from the page's Park form, and the item's
tooltip says so), and Review is the page's Done. The labels PATCH does not consult
the slot cap, which runs at prompt time, so Working asks `GET /v1/ae/capacity` first
and refuses at three with the working titles (the sidebar's own count when that
route cannot answer); `ride unparked` in a prompt stays the only way to run a
fourth. The extension SDK has no menu contribution point, which is why this is a
patch and not part of the Parking Lot extension; the logic is the new
`web/src/shell/AeStatusMenu.tsx` and the upstream edit is eight lines in
`Sidebar.tsx`.

Split view (2026-09-24): the fifth core patch, `patches/0005` (P5, web). Several
sessions side by side in one window, like the Claude desktop app's multi-panel
layout: "Open in split view" on a sidebar session's menu (kebab or right-click, any
row, desktop only) opens `/split?s=a,b` with the session being viewed and the chosen
one; up to four panes, each a full session with its composer, a session switcher, an
"open alone" button and a close button; dividers drag or take the arrow keys; the
layout is remembered in local storage and restored by a bare `/split`. Below 1024 px
the route falls back to the first pane's session on its own. Each pane is a frame of
the ordinary `/c/:id` route: upstream's chat store is a module-scope singleton with
one foreground conversation, so ChatPage cannot render twice in one React tree, and a
frame gives each pane its own store, streams and keyboard focus without touching it.
No upstream tag through v0.18.0 ships a split view; upstream tracks it in
https://github.com/omnigent-ai/omnigent/issues/888 with a draft at
https://github.com/omnigent-ai/omnigent/pull/6758, and when that ships this patch is
dropped in the sync. Screenshots in `docs/screenshots/split-panes/`.

Split view layout presets (2026-09-24): the tenth core patch, `patches/0010` (P10,
web), which changes only the three files P5 created. A picker in the split view's top
bar arranges the panes like the Claude Code desktop app: 2 side by side, 2 stacked,
2+1 (two stacked, one beside them) and its mirror, a 2x2 grid, 3 and 4 side by side.
Dividers resize columns and the panes stacked in a column (pointer, or focus and the
arrow keys: left and right for columns, up and down for stacked panes). Each pane's
grip swaps it with another, dragged onto the target or moved a slot at a time with
the arrow keys. "Add pane" past the preset's slots switches to the next preset that
fits (2 stacked becomes 2+1, 2+1 becomes the grid, side by side gains a column), and
closing a pane shrinks to the preset of what is left. Panes are positioned from the
layout rather than reordered in the DOM, so switching presets or swapping panes never
reloads a frame (a moved iframe would drop its stream and draft). The layout stays in
`ae.splitView.v1`, now as version 2 (`{v: 2, preset, panes, cols, rows}`); a version 1
value migrates to side by side with its widths. The URL adds `&l=<preset>` only when
the layout is not side by side, so older links mean what they did. Desktop only, as
before. Screenshots at 1440x900 of every preset and the picker are in
`docs/screenshots/split-panes/`.

Run on box (2026-09-25): the eleventh core patch, `patches/0011` (P11, web). A
session on a host often replies with a shell command that Reid used to copy by hand
into a shell, usually on the phone. Assistant code blocks tagged bash, sh, shell,
zsh, console or powershell, or untagged blocks that read as commands, now carry a
Run button beside upstream's copy button, for the owner of a session whose agent
declares shells (the gate upstream's "+ New shell" uses; hidden for other viewers,
in side chats and in user messages). Run opens a confirm sheet with the exact text
and a plain line for sudo, rm -rf, curl | sh or dd; sudo still asks for the password
in the terminal and nothing here sees it. On Run the session's selected or newest
shell (a new one of the default type when there is none) opens as a rail tab, or
full screen on the phone, and the block is pasted there as one paste followed by
Enter, through the terminal's own WebSocket and xterm's paste, the path the keyboard
takes; a block with an open heredoc, quote or trailing continuation is pasted
without Enter and says so. It targets a user shell, never the agent's own pane
(Claude Code's TUI), because a command typed there would become a prompt. No server
route is added. `patches/0013` (same
day) fixes what review found after the merge: the paste now waits for the shell to draw
so it is bracketed (a multi-line block is never sent line by line), a powershell block
runs only in a PowerShell shell, transcripts keep heredoc bodies and never replay
output, Run hides while a block is still streaming, and the terminal gets the focus
after a Run so a sudo password can be typed straight away.

Release on merge (2026-09-24): `.github/workflows/release.yml` makes a merge to
`main` the whole release, for when Reid has only a laptop and Claude web
sessions cannot push tags. When CI passes on a push to `main`
it tags the tip `ae-vX.Y.Z.N` (the next N for the pinned upstream tag, 1 after a
sync), but only when something that ships changed since the last tag and the commit
has no `ae-v*` tag yet, then dispatches `image.yml` on that tag to publish the
image; the box's auto-update timer deploys it. Docs-only and tests-only merges make
no tag; to ship one anyway, or re-ship, run the Release workflow on `main` with
`force` on.

Your move (2026-09-24): no core patch. One screen of everything waiting on Reid,
because across a long day he kept asking what was left and will merge from his phone.
`GET /v1/ae/your-move` (`ae_omni_policies/your_move.py`) aggregates
sessions that need him (an open gate, an approval outstanding, `blocked`, `review`),
lot items resurfacing today or overdue, the steward's latest brief and his open pull
requests with CI state, each source isolated so one failure shows inline and the rest
render, cached five seconds (pull requests sixty). The extension gains a "Your move"
page, first in the AE nav, and a compact block on top of the Parking Lot page. Pull
requests need an optional read-only GitHub token, `AE_GITHUB_TOKEN`, from Key Vault
`omnigent-ae-github-token` through `deploy/pull-secrets.sh`; without it the section
says how to add it. It adds no prompt rule (CLAUDE.md rule 4). Screenshots are in
the your-move screenshot set (removed before publication).

Plan limits (2026-09-24): the sixth core patch, `patches/0006` (P6, web). Clicking
the context ring under a session's composer opens a popover with the Claude Code and
Codex subscription windows (percent used, reset time and countdown, when the reading
was taken), and the Parking Lot page's capacity strip shows them in one line. The
numbers are documented CLI interfaces, never a TUI scrape: Claude Code's statusLine
payload (`rate_limits.five_hour` and `seven_day`), which the claude-native harness
already chains to `~/.claude/settings.json`'s statusLine command, so
`deploy/ae-limits-statusline.sh` there saves it with no harness patch; and
`codex app-server`'s `account/rateLimits/read`. A user timer on the box
(`ae-omnigent-limits`, every five minutes) runs `python -m
ae_omni_policies.limits_relay`, which posts both to `POST /v1/ae/limits/{harness}`;
the server keeps the newest per harness (`AE_LIMITS_STATE_FILE`, else
`/data/ae-limits.json` when `/data` exists). The same day,
`patches/0009` (P9, web, the P6 files only) made the popover match Claude Code's own:
the session's context window with a cached / cache-write / new-input bar, the 5-hour
window, weekly for all models and per model, usage credits and the plan. The relay
now also reads each claude-native session's own status line capture (so a session
that predates the statusLine setting still reports) and asks the CLI's `get_usage`
control request for the per-model rows, which need a stored `claude auth login` with
the profile scope; the sessions keep the setup token. The popover now closes when its
window loses focus (a click in another split pane). `docs/PLAN-LIMITS.md` has the
citations, the host steps and what each empty state means; screenshots in
the plan-limits screenshot set (removed before publication) and the usage-panel screenshot set (removed before publication).
`patches/0018` (P18, web, 2026-09-24) colors the ring as the context fills, as
Claude Code desktop does (warning from 60%, destructive from 80%; the popover's
Context window bar uses the same steps), and gives the prompt cache bar under it its
own colors and a caption, because Reid read that bar as a second meter.

Shorter usage popover and light tooltips (2026-10-14): `patches/0040` and `patches/0041`
(P40, P41, web). Reid found the popover too tall (933 px with his numbers, so it
scrolled) and sent Claude Desktop's context popover as the model. It now opens on the
context header over one bar with the prompt cache parts joined into it, each plan limit
takes two lines (label, reset and percent, then the bar), and the other plan and the
collaborators sit behind "See detailed breakdown"; the explainers moved behind
tap-to-open info toggles. Folded it is 371 px. He also read the light-mode tooltips, a
near-black box with white text, as a dark-mode bug: patch 0041 gives every tooltip the
popover surface in both themes, the style upstream's skill pills already used.
`docs/PLAN-LIMITS.md` ("The shorter popover") has the details; screenshots in
the usage-panel screenshot set (removed before publication). On 2026-10-15 `patches/0047` cleaned its detailed
breakdown: Claude Code's own subagents (rows like "general-purpose:a45b4b17...", which
never have a reading) are gone, rows read by name, a missing reading is one "No reading
yet" line, model ids read as names, and a 1M Claude Code window shows how far it is from
auto-compact (`docs/PLAN-LIMITS.md`, "The cleaner breakdown").

Fast mode switch (2026-10-14): `patches/0044` (P44, server, runner and web). Reid asked
for Claude Desktop's fast mode toggle in the model menu. A Claude session's model menu
now ends with a Fast mode switch ("Faster Opus output; bills usage credits"), enabled on
Opus 5.5, Opus 5 and Opus 4.8 only. It reads the session's state each time the menu
opens through `GET /v1/sessions/{id}/fast-mode`, and a flip goes through `PUT` to the
runner, which types `/fast on` or `/fast off` into the Claude Code terminal and replies
once the statusLine snapshot's `fast_mode` agrees (the claude-native status writer now
keeps that key), or reverts the switch with the reason. Codex has no fast mode. On the
box fast mode is per session (`deploy/README.md`, "Fast mode, per session"). The same
pull request carries `patches/0042`, the review fixes on the usage popover and tooltips
(shortcut tooltips are light too, the config-error tooltip has one outline, the legend
keeps its dots only while the bar is drawn in their colors, and an explainer opened
while loading stays open), and `patches/0043`, which repairs the ChatPage composer,
mention and statusLine suites: they had failed unseen since patch 0030 because the team
control needs a QueryClient, and CI's web check now runs them. The dual review (Codex
and Claude, 2026-10-14) found ways fast mode could stay on, billing credits, while the
switch showed off; `patches/0045` closes them: turning on needs the per-session setting
on the host, a typed `/fast` is reported as pending ("Applies when the current reply
finishes") rather than failed, a per-session lock serializes flips, off works on any
model, model picks wait for a pending flip, and a flip never starts a terminal.
Screenshots in `docs/screenshots/fast-mode/`.

Agents pane like Claude Desktop's Background tasks (2026-10-16): `patches/0055` (P55,
web). Reid sent the Agents graph drawn as one top-down tree about 2000 px wide, which
the tall right pane shrank to dots, with "feels like the agents graph should have a
left/right option as the pane on right is usually taller", and Claude Desktop's
Background tasks pane as the model. The graph now runs left to right whenever the pane
is taller than wide, a button beside the zoom controls cycles automatic, top-down and
left to right (remembered per device), and the fit stops at 70% zoom anchored on the
root so the pane pans instead of shrinking. The list shows Running agents first with
their task name, model, a live timer and the last line ("No output yet" until there is
one), folds Finished into one counted line with a Clear button (and a button to bring
cleared rows back), and puts the session's task list (Claude Code's TodoWrite items,
which OmniGent already receives) in a folding Plan section under the agents. A Stop
square shows only on agents OmniGent launched; Claude Code's own subagents run inside
the main agent's process, so stopping one alone is not possible. A dual review (Codex
and Claude) caught six bugs before the merge, among them stopped agents stuck under
Running, one session's Clear leaking into the next, and the graph snapping back after
a pan; all six are fixed in the same patch. Screenshots in
`docs/screenshots/agents-pane/`.

Agents pane Model column (2026-10-16): `patches/0063` (P63, server, runner and web).
After the redesign went live Reid sent the pane with every Model cell empty and each
status dot floating in that gap ("still seems the same??"). The routed model was the
only source, and agents are rarely routed. Child session rows now carry the model the
agent actually ran on: the harness's own report, else the model its own usage was
recorded against (Codex collaborators already report that), else the requested
override. The Claude Code forwarder now reports each subagent's `message.model` to its
mirrored child, so Claude Code subagents fill in from the deploy on; ones that finished
before it stay blank. Claude ids print as names ("Sonnet 5.5"), other ids as the
harness spells them ("gpt-6-astra"), and the status dot sits right of Time. Screenshots
in `docs/screenshots/agents-pane/model-*.png`.

Session rail (2026-10-17): `patches/0071` (P71, web, runner and server). Reid sent
Claude Desktop's session rail and Codex's usage popover and asked for the popover's
detail to move into a pane beside the chat that he can reorder, plus triage,
schedules and the files he sent. A toggle in the chat header (on a phone, "Session
rail" in the session menu) opens a column of folding sections about the open session:
Triage (a status switch, time since it started, an open gate with the Dashboard's
four buttons, the absorbed and unfiled lines and the next step), Environment (host,
folder, branch, permission mode), Context (Claude Code's categories by size, deferred
tools, the tool, memory and skill lists, and Compact session), Agents (each
collaborator's own window), Scheduled (check-ins and schedules aimed at the session,
with Pause, and what is still running before a close), Usage (both plans), Connectors
(MCP servers and their status), Sent files, Outputs (files the session created) and
Changes (linked pull requests and the changed-file count). Sections fold and reorder
by drag, or by their menu on a phone; the order, folds and open state are kept per
device. The usage popover keeps the basics, one context bar with its top three
categories, a lane per running agent and the session's own plan, and its "Show
details in session rail" button replaces "See detailed breakdown". The popover and
the rail share one context snapshot per session and model, reused for three minutes
and fetched only while one of them shows it. The runner now returns each MCP server's
name and status with the snapshot, nothing else from the configuration, and a new
route, `GET /v1/sessions/{id}/ae-mcp-status`, keeps a Codex session's last MCP
startup map, which upstream clears once startup settles. Known gaps: Outputs counts a
git workspace's untracked files as created and misses files written outside the
workspace, and OmniGent has no network-access setting to show. Screenshots in
the session-rail screenshot set (removed before publication); the data paths are in `docs/PLAN-LIMITS.md` ("The
session rail").

Session activity indicator (2026-10-17): `patches/0074` (P74, web and server). Mid-build
of the session rail Reid asked "are you still working? I can't tell": a session row
showed a spinner only while its turn ran, so a session whose turn had ended while
agents, a background shell or a check-in were still going looked finished, and a long
silent step looked frozen. Every session list row and websocket update now carries
`background_task_count` (the server's existing background-shell tally) and
`active_child_count` (running child sessions: subagents and collaborators), counted from
data the list already loads. The sidebar adds three row states: waiting (a pulsing
hourglass with the count), quiet (an amber spinner once a running session has had no
update for five minutes) and scheduled (an alarm with the time a check-in aimed at the
session fires next). Above the composer a status line shows the elapsed time, the
current step and the last activity while a turn runs ("no new output for 6m" once it
goes quiet), one chip per agent and background shell while the session waits, and
"Paused until 9:00 AM" when only a check-in will wake it. Claude Code's own timers
(Monitor, ScheduleWakeup, cron) and agents OmniGent does not mirror stay invisible.
The same day Claude sessions were asked to post one line at every step boundary
naming what finished and what starts next. Screenshots in
the session-activity screenshot set (removed before publication).

Hover status (2026-10-20, `patches/0116`, web): hovering a session in the sidebar now
says what it is doing without opening it. Reid asked for a brief preview of whether a
session is waiting or working, so he would not have to click into each one. The hover
card gains a status block under the title, read from the row's own state so it always
matches the row's icon: "Working" with the turn's elapsed time and current step, "Quiet
for 9m" with the last step, "Waiting on you" with the open question or the step awaiting
approval, "Waiting on 2 agents" with each running agent's newest step (two at most), or
"Idle" with the first line of the last reply. The details come from one read of the
session's newest items (`lib/aeSessionPeek.ts`), only while the card is open, re-read
every 5 seconds so an open card follows the session.

Phone session menu (2026-10-20, `patches/0121` and `0122`, web): on a phone, long-pressing
a session or opening its menu showed every Status value and every Move item, about 20 rows,
and the menu ran off the screen (Reid's screenshot that day). Status and Move are now one
row each, Status showing the current value ("Working" with its icon); tapping either opens
its items in place under it, one at a time, so the menu is 11 rows closed and 16 with Status
open. Reid picked this over drill-in pages and a bottom sheet from mockups. Desktop keeps
its side flyouts.

PR link previews (2026-10-20, `patches/0118`, web): resting the pointer on any GitHub
pull request link in chat opens a small card with the state (Open, Draft, Merged,
Closed), repo and number, title, author, lines added and removed, files changed and,
while it is open, its checks. Reid wanted a glance at each pull request a session
reports as done before opening it. The card reads the merge card's `/v1/ae/prs` data and
shares its cache; it fetches only once opened. Touch screens have no hover, so a tap
still just opens the link.

Effort shown when none was picked (2026-10-20, `patches/0119`, runner and web): Reid
could not tell a Claude session's effort before changing it, since the composer's Effort
menu showed no level and no check when he had never picked one. Claude Code then takes
`effortLevel` from its settings, which the web never saw. At each launch without
`--effort` the runner now reads that level the way Claude does (managed policy, the
workspace's `.claude/settings.local.json` and `.claude/settings.json`, then the launch
account's user settings in `CLAUDE_CONFIG_DIR`, else `~/.claude`) and writes it to the
session label `ae.claude_effort_default`. The menu reads "Effort: High (default)" with
High checked, or "Effort: Claude default" when no file names a level (the second Claude
account's settings on the box name none). A label rather than the session's
`reasoning_effort`, after the dual review: that field is the user's pick, so saving the
default there would pin it as `--effort` on every relaunch and could overwrite a pick made
during launch. A session picks this up at its next launch.

Model check placement (2026-10-20, `patches/0120`, web): in the composer menu's
two-column model grid the check sat at the right edge of the picked model's cell,
beside the next column's model, so a session on Opus 5.5 looked like it was on Haiku
5.5 (Reid's screenshot). The check now sits before the model's name.

Per-model default effort (2026-10-20, `patches/0124`, runner and web): the first
release (`0119`) read only a top-level `effortLevel`, but Claude Code 2.1.304 keeps
effort per model (`modelSettings["claude-opus-5-5"].effortLevel` in `settings.json`).
The box's file has only the per-model entry, so after the ae-v0.18.0.22 deploy every
session's menu said "Effort: Claude default" while Opus ran at high. The runner now
records both kinds in the session label `ae.claude_effort_default` as a small JSON
object (`{"*": <top-level level>, "claude-opus-5-5": "high"}`), and the menu shows the
entry for the session's current model, else the `*` entry, else "Claude default". A
session picks this up at its next launch.

Prompt suggestions (2026-10-20, `patches/0123`, harness, server and web;
https://github.com/analyticendeavors/omnigent-ae/issues/45): after each finished Claude
turn the web composer shows a guess at Reid's next message as grey ghost text, the way
Claude Code's own terminal does in print mode. Tab, or a tap on its "Tab to use" chip
("Tap to use" below 640 px), fills it into the box; nothing is ever sent. A tap on the grey
text focuses the box as usual. Typing hides it, Esc dismisses it until the next turn, the
next turn clears it, and it never shows while a turn runs. Claude Code's own
suggestions exist only in print/SDK mode: a spike on the box (Claude Code 2.1.304) found
none drawn in the interactive TUI OmniGent runs and none in the transcript, so there was
nothing to scrape. Instead the forwarder, on each parent turn's `Stop` hook, makes one
background side call, `claude -p --model haiku`, with about 2,000 characters of the last
prompt and reply, isolated from hooks, settings, MCP servers, tools, session persistence
and OmniGent's environment, in a temporary cwd and Claude config dir of its own (so it never
rewrites `~/.claude.json`), with its process group killed after 15 seconds; a newer turn,
a prompt typed in the terminal and the forwarder's exit all cancel a call in flight.
Sub-agent sessions, turns that end waiting on an answer, sessions on a named Claude account
(the call would bill the default login) and sessions on a gateway, Bedrock or Vertex get
none. Off unless `AE_PROMPT_SUGGESTIONS=1` reaches the runner (`config/host.env.example`
says how). Each turn costs one small Haiku call on the host's Claude login.

Stuck Running subagents (2026-10-20, `patches/0125`, server): a subagent that died
before its first quiet spell, or across a server restart, stayed Running in the rail
forever, and its parent session read "Waiting on 1 agent". One died a second after
launch when a deploy restarted the host, and still read running hours later. The
60-minute check that settles a silent subagent (`0112`) was armed only on its first
quiet spell and lived in memory, so a restart dropped it. The server now arms it the
moment a subagent starts running, and at startup it sweeps the subagents still marked
running: one silent for an hour with no recorded return settles to finished, the rest
get the check back for the time remaining.

Merged PR fold (2026-10-20, `patches/0129`, web): a finished session ended on three
full Merged cards under its last message, about 550 px that repeated the TLDR's links
and needed nothing from Reid. Merged and closed pull requests under one assistant
message now fold into a single row ("3 merged #167 #161 #169"); opening it lists one
titled line per pull request, and opening a line shows that pull request's full card
with Review details and the AE compliance review. Open and draft pull requests keep
the full card, with Merge, the recommended method and the Squash or Merge commit
choice, above the row. A card merged from a card on this page stays a card until the
page reloads, so its after-merge note and "Mark session done" do not jump into the fold;
one merged anywhere else folds within 30 seconds, and a closed one that is reopened
comes back as a card.

Prompt suggestions in every session (2026-10-20, `patches/0128`, harness only; Reid's
call after https://github.com/analyticendeavors/omnigent-ae/pull/164 shipped with a skip
for named Claude accounts): the ghost text now also follows a Claude session on a named
account such as "AE Backup", and a Codex session. A named-account session's side call runs
on that account, with its token resolved the way the terminal launch resolves it
(`account_env_for_launch`: this host's secret, or the server for a shared account) and
handed to the call only as `CLAUDE_CODE_OAUTH_TOKEN` in its environment, so nothing is
written to disk; a name this host cannot resolve skips the call. A Codex session's call is
the same Claude Haiku call on the host's active Claude account (what a Claude terminal
started without a choice gets), whatever model Codex runs, made when Codex reports the turn
complete. A raw `ANTHROPIC_API_KEY` in the runner's environment skips the call, as a
gateway does. The web needed no change. The same `AE_PROMPT_SUGGESTIONS=1` switch covers both,
and it reaches Codex sessions through the same `OMNIGENT_RUNNER_ENV_PASSTHROUGH` entry,
since both forwarders run inside the runner.

Failed subagents in the rail and the Agents tab (2026-10-20, `patches/0130`, server and
web): a Claude Code subagent that died mid-run read as finished. Seen live: "Build Admin
Center grid redesign" stopped at 23:49Z with three image reads unanswered, its parent
recorded it returned failed at 23:50Z, and after the 00:15Z deploy restarted the server the
rail's Agents section read "Nothing running now." and "3 finished". The child list took a
row's status only from the server's in-memory cache, which a restart empties, so the failure
the row still keeps in the database was dropped; the list now falls back to it. A failed
subagent now sits in its own always-open Failed group, with a warning icon, in both the rail
and the Agents tab, and the rail's summary reads "1 running · 1 failed · 2 finished". The
rail also reads the same live list as the Agents tab, which used to show a resumed subagent
as running a minute before the rail did. The time shown still counts from the first launch:
a resume leaves no trace the list can read.

Merged PR row links and carets (2026-10-20, `patches/0131`, web): in the folded row of
merged pull requests, the numbers ("#173 #172 #175") were plain text, and the opened
list's lines gave no sign that they open. Each number is now a link to its pull request,
opening in a new tab, and shows the same preview card as a pull request link in the chat
when the pointer rests on it or it takes keyboard focus; clicking a number no longer
opens the fold. Each opened line starts with a caret that points right while its card is
closed and down while it is open.

Prompt suggestion alternatives (2026-10-20, `patches/0132`, harness, server and web): the
same single Haiku call now returns up to three different guesses at Reid's next message, best
first. The chip reads "Tab to use | 1/3", Alt+] and Alt+[ step through the guesses (a tap on
the counter does the same on a phone), Tab still takes the one shown, and a long guess wraps
instead of being cut off. Settings > Chat has Suggestion keys to record other combos; it
refuses one the app already uses (Ctrl+Alt+V for dictation, for one) and saves per browser.

Closed subagents off the waiting badge (2026-10-20, `patches/0133`, server): a sidebar row's
hourglass count stayed at 2 for hours after both of its busy subagents were closed. Seen
live on session `a2e393e9f4ad4f6caac2e2b3f7ae3b1e`: children
`2cfcded225104966a7fffa1e915b385b` and `ee95b92ff2444b1eba7a458485d05d58` were closed with
`sys_session_close` (archived) while still marked running, their runners were gone, so no
idle edge ever came, and `GET /v1/sessions/<id>` kept reading `running` even after a deploy
emptied the count. A closed child now leaves the count the moment the archive lands, even
if a late busy edge arrives, on every replica and after a restart, and the archive
teardown settles its status to idle, stored status included. An Undo brings a
still-running child back into the count.

Real effort level and a one-column model menu (2026-10-20, `patches/0134`, web): the
chat composer's model menu said "Effort: Claude default" whenever no effort was pinned,
and the chip under the composer named only the model. Both now show the level the session
actually runs at: a settings-file level the runner recorded, else Claude Code's own default
for the model (Fable 5.1 High, Opus 5.5 and Sonnet 5.5 Medium), with Claude Code's words
("Extra" for xhigh), so the chip reads "Fable 5.1 1M High" like desktop Claude Code's
"Opus 5.5 Extra". The menu itself is one column like desktop's: an Account row with the
logins (host and shared) one level down, the models as numbered rows a digit can pick,
"Effort  High ›" with the default tagged in the submenu, Previous models, and a one-line
Fast mode toggle. `scripts/claude-effort-defaults.py` prints the default table from the
installed Claude Code binary for the next bump.

Remove a personal Claude login (2026-10-20, `patches/0135`, server, host and web): Settings,
Claude accounts could rename an extra Claude login but never remove it, so Reid's personal
"AE Backup" could not go once the shared one existed. Every personal login now has Remove
beside Rename, asking once; it deletes the login and its token from the host, and new chats
fall back to the default CLI login when the removed one was selected. Running chats keep the
token they launched with. The default CLI login and shared accounts have no Remove (shared
ones keep "Remove for everyone" in their own group).

Chat text fills the column (2026-10-20, `patches/0136`, web): assistant text was capped at
80ch while user bubbles, merged PR rows, inline PR cards and the composer sat on a wider
column, so their right edges never lined up. Assistant text now fills the chat column (about
110 characters, 120ch at the wide tier), the merged PR fold and inline PR card are full
width, and above 1920px the column stays 48rem instead of growing to 56rem, which had left
text at 80ch beside a wide empty strip.

Action dock above the composer (2026-10-20, `patches/0137`, web and server): what waits on
Reid in a chat was spread over the feed (a question card, a ready merge card far up, a merge
that has not reached the box yet). One compact line now sits above the composer's workspace
bar and gathers it: the most urgent item with its button (a question's "Jump to it", a ready
pull request's Merge, a pending deploy's Admin Center link and dismiss), then a tinted badge
per other kind and a caret that opens a list of every item (design E1 collapsed, D3b opened,
D4 deploy tracking, approved by Reid). Merge keeps the card's confirm step inline in the dock.
An item hides while its own card is on screen, and nothing waiting draws nothing. Deploy lines
are for omnigent-ae only: `GET /v1/ae/prs/{owner}/{repo}/{n}/deploy`
(`ae_omni_policies/pr_deploy.py`) says whether the running release contains the pull
request's merge commit, with the busy sessions and the deploy hold it waits on; the feed's
merged row shows the same "waiting for deploy" or "live in ae-vX".

Dock merge confirm shown whole (2026-10-21, `patches/0138`, web): opening Merge from a line
in the opened dock list put the confirm step inside the list's 120 px scroll area, so the
method choice and the confirm button were cut off (seen in the built-dock screenshots). While
a confirm is open the list drops its height cap, and the line scrolls into view.

Session title in the phone app header (2026-10-21, `patches/0139`, web): in the iOS and
Android apps the header showed only the sidebar button, the slots chip and the menu, so
Reid had to open the sidebar to see which session he was in. The app's own top bar only
switches servers. The header now shows the session's title there, cut short with an
ellipsis when long; the folder icon and sub-agent segments stay hidden, and a sub-agent
still shows only its Back link.

Run pastes multi-line blocks on tmux 3.6 (2026-10-21, `patches/0143`, server): Reid's Run on an
eight-line bash block was refused with "This shell has not turned on bracketed paste". bash
had turned it on before the browser attached, and the attach seed reads that mode from a tmux
format that only exists from tmux 3.7; the box runs 3.6a. The seed now infers it for a bash,
zsh or fish prompt when tmux cannot say, so the browser terminal knows and Run pastes the
block as one.

Session menu widths and one New Chat menu (2026-10-21, `patches/0140`, web): Reid's
screenshot of the chat's session menu showed empty space between the model names and their
shortcut digits, and an Account submenu stretched by its one-line scope hint. The session
menu (304 px), the harness row menu above it (280 px) and the Account (334 px), Effort and
Previous models submenus now each hug their widest row with a shared 160 px floor; the hint
wraps under the account rows. Reid then asked for every associated menu to match, so New
Chat's Claude Code and Codex picker draws the same one-column menu as a chat (it had
account chips in a row, effort listed inline and checks on the right). Measured in the real
app, both places: 214 px session menu, 187 px Account, 160 px Effort and Previous models.

Action dock as one strip (2026-10-21, `patches/0141`, web): with anything waiting the dock
was its own row above the workspace bar, so the composer carried two strips (69 px). Reid
asked whether one would be better, saw the mockups and said build. The dock now lives inside
the workspace bar (37 px, 28 px on a phone): folder and PR chips, a hairline, the most urgent
item with its button, the badges, the bar's own right cluster, then the caret. While anything
waits the folder chip goes to its icon and the PR chip to its icon and count. Badges drop to
icon and count before the lead's text truncates. A live deploy is a green chip. The list opens
as a tray above the bar, and a Merge pressed in the strip opens its confirm in that list.
Nothing waiting leaves the bar as it was. After the dual review the tray always starts closed
and closes on Escape, a confirm gets up to half the screen, and a confirm whose item goes
away closes on its own.

Usage gate in the action dock (2026-10-21, `patches/0142`, web): Reid wanted a prompt before
a session's account runs into its 5-hour or weekly limit, so he can switch model, account or
chat instead of paying overage. At 90% of either window the dock shows "AE Default is at 92%
of its weekly limit, resets Thu 09:00" with Switch model (the composer's model menu), Switch
account (the fork dialog, history included, where the Claude account row picks the login;
hidden for Codex or with no other login) and New chat. Sending stays open; a dismissal lasts
until that window resets, in every chat on that account.

Action dock keeps merges in view (2026-10-21, `patches/0144`, web): the dock hid an item
while any sliver of its own card was on screen, so a chat with three pull requests to merge
showed one at a time and its lead changed as Reid scrolled. Reid reads the dock as what is
pending now, not what is off screen. Merges, deploys and the usage item now stay whatever
the scroll position; only a question still hides while its card is in view, since that card
floats just above the dock (Reid picked this from mockups). A ready pull request whose card
is in view shows Merge in both places, and Confirm in either is disabled while the other is
merging.

PR preview in the action dock (2026-10-21, `patches/0145`, web): hovering a pull request link
in the chat shows a small preview (state, title, author, size, checks), but the dock's
`#202` only showed a tooltip and opened GitHub. Reid asked for the same preview there, before
the click. The dock's number now uses the chat's hover card, and its tooltip goes, since it
would show over the preview.

Merged PR row on one line (2026-10-21, `patches/0146`, web): on a phone the merged pull
request row's numbers (`#205 #203 #201`) wrapped to a second line beside "3 merged" and the
deploy status, and Reid said it looked odd. They now stay on one line and scroll sideways
when they do not fit; the count and the opened fold still list every one.

Dock preview opens upward (2026-10-21, `patches/0147`, web): the action dock's pull request
preview (`0145`) opened downward, over the composer Reid types in. It now opens above the
bar, the same way the dock's list opens; chat links keep opening as before.

Fewer gh auth status calls (2026-10-21, `patches/0148`, runner): every read of a session's
GitHub panel ran `gh auth status`, and the merge watcher reads every recent session each
two minutes, so the box ran about four a minute. The answer is now cached for 60 seconds
in one file the host's processes share, with only signed-in answers kept.

Draft pull requests in the action dock (2026-10-21, `patches/0149`, web): the dock listed
only pull requests ready to merge, so a draft never showed (Reid's report, with #221 as
the example). A draft now gets its own line, "#n draft · x/y checks", after the merges,
with a Mark ready button; once ready and green it becomes the Merge line.

The thread's PR card is a receipt (2026-10-21, `patches/0150`, web): the card under a chat
message carried its own Merge button while the action dock showed the same pull request's
Merge, so two Merge buttons sat on one screen. The card is now one line (state, number,
title and a status word such as "checks 7/12", "ready", "merged 03:35" or "live") that
opens in place onto the checks, Update branch and the review folds, with no Merge; the dock
owns every live action. The GitHub panel's card keeps its Merge.

Open Cost Management on the Azure card (2026-10-22, `patches/0151`, web): the Azure spend
card links to the Azure portal's cost analysis page. The host admits that one URL for
extensions and nothing else on portal.azure.com; the link reveals no subscription.

Every open pull request in the action dock (2026-10-22, `patches/0152`, web): the dock
listed only drafts and pull requests ready to merge, so PR 228 sat outside it while one of
its twelve checks ran and Reid, having said "merge when ready", had nothing to watch
(https://github.com/analyticendeavors/omnigent-ae/pull/228). Every other open pull request
of the session now gets a waiting line with the thread receipt's word and tone ("checks
11/12", "conflicts", "1 check failed", "behind main", "review requested"), after the drafts
and before the deploys, and an Update branch button when GitHub allows one. Once ready it
becomes the Merge line. Several fold into one "n not ready" badge, so the bar stays one line.

Readable skills and file lists (2026-10-22, `patches/0153`, web): the slash command menu's Skills
list and the `@` file menu were see-through in the dark theme, so the chat behind them ran into
the rows. Both now blur what is behind them, as the other frosted popovers do.

Waiting line keeps its word on a phone (2026-10-22, `patches/0154`, web): the live check of
`0152` (https://github.com/analyticendeavors/omnigent-ae/pull/240) found that at 390 px a
waiting line showed only "#240" and its icon. The dock hides a line's detail on a phone
because a button beside it names the state (Merge, Mark ready); a waiting line usually has
no button, so its word ("checks 11/12") now stays.

Slot limit from Settings (2026-10-22, `packages/ae_omni_policies`): Reid runs four slots
and expected to change the number on Settings > Global Policies. The slot cap policy has
taken a `limit` there since the Edit button (`patches/0095`), but every count in the web
UI read `AE_SLOT_LIMIT` through `GET /v1/ae/capacity`, so a limit set on the page held
prompts at one number while the Dashboard and the sidebar showed another. `slot_limit.py`
now answers for the counts by the rule the policies enforce: each enabled slot cap
holds at its own `limit`, or at `AE_SLOT_LIMIT` when it sets none, and the lowest wins;
with none on, `AE_SLOT_LIMIT`. The capacity route, the orchestrator's session list and
its slot hold read it on every request. The web UI shows a new number on a reload, or
when its window regains focus once its last read is five minutes old (patch 0014's
cache, whose comment still says the limit moves only at a restart). Without a `limit`
on the page nothing changes.

Session rail when a session opens (2026-10-22, `patches/0155`, web): Reid wanted the rail,
not the files panel, when a session opens. "Open the session rail in every session" still
let a restored workspace panel come back whenever it fit beside the rail, and the rail then
made way for it. Settings > Chat now asks "When a session opens, show": Session rail (the
new default; a panel left open starts closed on a desktop), Both when they fit (the old
switch on) or Workspace panel (the old switch off). A file, agents or comment link still
opens the panel, and a device that had the switch off keeps the workspace panel. Details
in `docs/PLAN-LIMITS.md` ("When a session opens").

Approval code under Show details (2026-10-22, `patches/0156`, web): on approval cards
that gate the person's own message (the rabbit-hole check, the slot cap) the JSON under
the policy's sentence now sits behind a Show details toggle, closed by default; Reid had
asked for it on 2026-10-17. Cards that gate what the agent is about to do (a tool call's
command, a response) keep their code in view, whoever wrote the policy.

Insertion line between lists (2026-10-22, `patches/0157`, web): dragging a session onto a
row of another project folder, of Sessions, or of another Triage group now shows the
blue line, and the drop files it there (or sets that group's status), then places it at
the line in the manual order. Reid asked for it after moving a session out of a project.

Feature docs per public release (2026-10-22, `scripts/docs-coverage.py`): the README's
showcase and `docs/features` should keep up with the patch series, but Reid wanted that
once per public release, not on every merge. The script lists the core patches added
since a release tag that no page under `docs/features` links and
`docs/features/uncited-patches.txt` does not excuse (review fixes and plumbing, each with
its reason), plus, for information, the merges that changed `packages/` or `agents/`
without a patch. The public release's prepare step runs the tag's own copy, pinned by its
sha256 in the release script because prepare holds admin credentials, and stops on a gap
until the pages cover it or `--docs-checked` is passed; exit 2 means the check could not
run, never a gap.

Patch series without private fixtures (2026-10-21, `patches/0106` and `0137`): the
pre-publication audit (finding RS-codex-01) found that the patch that scrubbed AE's real
Databricks workspace host from two web test fixtures left it in the text of `0106`, which
added them, and in its own removed lines, so a fresh public history would still ship it.
The fork series was rewritten: the scrub is folded into `0106`, so no patch ever carries
the real host, and the action dock's holder-name test and comment in `0137` use
`you@example.com` instead of a real work address (findings R1-claude-29 and S1-claude-04,
which also covered `packages/ae_omni_policies/tests/test_pr_deploy.py`). The series drops
from 146 patches to 145, and every patch after `0139` moves down one number.

Rail docking and sidebar grab strip (2026-10-17): `patches/0075` and `0076` (P75, web).
Reid keeps the OmniGent window narrowed to one side of the screen and asked for the
chat to stay visible beside the session rail. The rail floated over the chat below a
fixed 1280 px window width (1600 px with the workspace panel open), whether or not the
sidebar was open, so a 1210 px window with the sidebar closed lost the right third of
the chat and the header's buttons. The rail now docks whenever the window, less the
open sidebar, the open workspace panel (and its 8 px gap), an open files or logs panel
and the rail's own 304 px, leaves the chat 480 px (the floor upstream's workspace
panel already keeps), and floats only below that; with the sidebar closed the chat
shifts left beside it. Reid also asked to shrink the sidebar instead of only opening
or closing it. Upstream already resizes it from its right edge (220 px up to half the
window), but the grab strip was 4 px wide and invisible until hovered, so it is now
8 px and draws a line on hover and keyboard focus, and the session list sits 4 px
further in so its scrollbar stays clear of the strip. `0076` carries the dual review's
fixes (the push panels and the gap were first left out). Screenshots in
the session-rail screenshot set (removed before publication) (`docked-1210-dark.png`, `floating-1009-dark.png`).

Usage per session account (2026-10-17): `patches/0078` and `0079` (web and runner) and
the AE package's relay and route. In a session running on the backup Claude account,
the rail's Usage showed the default login's limits (weekly 100% where the backup stood
at 4%): the relay kept one reading per host and overwrote it with `get_usage`, which
reads only the stored default login. The runner now records each session's account
beside its status-line capture, the relay keeps each session's own windows and
account, and `GET /v1/ae/limits?session_id=` answers with that session's account. A
backup account shows its 5-hour and weekly windows with a hint that per-model limits,
credits and the plan show only for the default login, and the header names it
("Claude Code · AE Backup"). Details in `docs/PLAN-LIMITS.md` ("Usage per session
account").

Claude accounts kept out of the tests (2026-10-17): `patches/0080` (tests). Run on the
box during the usage work, `tests/test_claude_native.py` (`tests/harnesses/claude_native/` since v0.18.0) built a Claude launch env from
the real `~/.omnigent/claude-accounts.json`, so its exact-env assertion failed and
pytest printed AE Backup's real token (CI has no accounts and does not run that file).
`tests/conftest.py` now redirects the accounts file and the secrets home whenever they
resolve to the developer's real `~/.omnigent`, so a test isolated through
`OMNIGENT_CONFIG_HOME` or `HOME` keeps its own, and drops an inherited
`OMNIGENT_CLAUDE_ACCOUNT`. Two exact-env tests now expect the empty
`OMNIGENT_CLAUDE_ACCOUNT` that patch 0078 puts into every Claude launch env. Gap: the
redirect is in-process, so a test that starts an `omnigent` subprocess without its own
config home still reads the real files.

Omnigent connector status (2026-10-17): `patches/0081` and `0082` (runner). The session
rail's Connectors section showed omnigent as failed in every Claude session while the
live session's omnigent server worked. The context probe behind that section copied the
omnigent bridge into the system temp dir, and the bridge refuses a bridge dir outside
its allowed roots, so the probe's own copy of the server exited before it connected.
The copies now go under the claude-native bridge root, which the orphan sweep leaves
alone because they carry no `owner.pid`. `0082` carries the dual review's fix: a bridge
root with the wrong owner or mode now gets the route's usual "unavailable" answer and
30-second backoff instead of a server error.

One pane at a time (2026-10-18): `patches/0083` (web). With the session rail and the
workspace panel both open in a window too narrow for both, the rail floated over the
workspace panel and hid its right half. Now, when the open rail would float over an
open workspace, files or logs panel, the pane opened last stays and the other closes.
Opening the rail closes the panels, the workspace one through its own header toggle,
so as with that click its `?file=` link clears and it stays collapsed in this session
and in new ones. Opening a panel, narrowing the window, widening the sidebar or
switching to a session whose workspace is open closes the rail instead, and a rail
closed that way reopens by itself once no panel needs the room, because the workspace
panel's open state is saved per session while the rail's is one setting for every
session. A rail the user closes stays closed, and over the
chat alone the rail still floats in a narrow window. Details in `docs/PLAN-LIMITS.md`
("The session rail").

Split view names and the rail in every session (2026-10-18): `patches/0084` to `0087`
(web; `0086` and `0087` carry the review's fixes). Reid saw a split view pane picker showing `e264fbf40762483e8162b9da917dea7d`:
the pickers knew only the first page of unarchived sessions, so any other session
showed its id. A pane outside that page now fetches its own row, and the pickers read
the session's name, "Loading..." or "Untitled session", with "(archived)" where it
applies. He also had to reopen the session rail in session after session, because a
session that restored its open workspace panel closed the rail on any window up to
about 1440 px, and could not tell the rail toggle from the workspace panel toggle. The
rail toggle and the phone menu item now draw Tabler list-details, and a new setting,
Settings > Chat > "Open the session rail in every session" (on by default, per
device), opens the rail whenever a session opens on a desktop, and a workspace panel
the session left open starts closed when the two would not fit. That automatic choice
is never saved over the user's own. Any later panel open, by the user or the agent,
still swaps with the rail; phones are unchanged. A pane whose row fetch fails, or whose
list fails, falls back to its frame's title or "Untitled session", never an endless
"Loading...". Details in `docs/PLAN-LIMITS.md` ("The session rail").

Every row's waiting state (2026-10-18): `patches/0088` and `0089` (web and server). Reid
saw the hourglass and the count of agents he was waiting on only on the session he had
open; every other row whose own turn had ended while its agents still worked showed the
plain spinner. The list row's `status` reads running while any direct child is busy, and
only the open chat knew the session's own turn was over. Each list row and websocket
update now also carries `turn_active` (the session's own turn, without the child
roll-up) and `blocked_on` (the dialog a running session is parked on, from its latest
status edge), so every row, and a collapsed project's marker, reads waiting with the
count, and a row parked on a terminal dialog never turns quiet, open or not. A server
without the fields keeps the old behavior. `0089` carries the review's fixes: a dialog
no longer outlives a status reset, and the open row trusts its chat over the list's
one-tick-old flags.

Turn rail end (2026-10-18): `patches/0090` (web). The left-edge turn rail lit the user
turn at the middle of the viewport, so a chat scrolled to its very bottom still lit the
turn before the last. Now, within 4 px of the transcript's real end (the scroll
container's `scrollHeight`, which includes the spacer below the last turn), the last
user turn is lit, whether the chat ends on that message or on the assistant's reply;
anywhere else the midpoint rule is unchanged. The check is `aeAtTranscriptEnd` in
`web/src/lib/aeActiveTurn.ts`; tests are `aeActiveTurn` and `Transcript.activeTurn`,
run by `just web-check`.

Rail loading while Claude starts (2026-10-18): `patches/0091` to `0093` (runner, server
and web). In a new session the rail's Context and Connectors sections both showed
"Could not read Claude context. Resume the session, check its MCP connections, and try
again." with a Try again button until Reid refreshed by hand. The first read raced
Claude's start (no statusLine reading, session id or live process yet), the runner
answered that like a real failure and backed off for 30 seconds, and the web cached the
error for three minutes. Now the runner answers `pending` with the real not-ready
reason, the server marks a lost runner `offline`, and the web keeps both sections
loading and retries pending answers quietly (2, 4, 8, 16, then 30 seconds, for about two
minutes) before that reason shows. Offline answers and real failures show Try again at
once, a final one (another harness) shows its reason with no button, and each turn end
or refresh reads past the runner's cache. Details in
`docs/PLAN-LIMITS.md` ("The session rail").

Editing a global policy's parameters (2026-10-18): `patches/0094` and `0095` (server
and web). Reid asked to edit, delete and change the policies on Settings > Global
Policies. The page could switch and delete a stored global policy but not change its
parameters, because `PATCH /v1/policies/{id}` took only name, handler and enabled. Now
every stored policy whose registry entry takes parameters has a pencil beside its
switch; it opens the add dialog prefilled, with the name fixed, and Save sends the new
parameters; a field left as it was keeps its stored value exactly. The server builds
the factory once before saving, on edit and on add, so a value the factory refuses (a
negative rabbit hole threshold, an unknown slot cap mode) is refused with the factory's
own reason instead of breaking policy evaluation for every session. A cleared field
falls back to the factory's default. Policies from the server config's `policies:` block
keep no controls, as before. Details in `patches/README.md`; screenshots in
`docs/screenshots/policy-edit/`.

Rail slide (2026-10-18): `patches/0096` (web). With one pane at a time live, Reid's
screen recording caught both panes on screen at once during a swap. The workspace panel
slides out over 300 ms (width, fade and a 12 px slide, `.workspace-panel-motion` in
upstream's `index.css`), while the rail appeared at full width at once, so in his
1136 px window with the sidebar open the chat dropped from 414 px to 110 px before
settling at 437 px. Opening the workspace instead made the rail vanish and the chat
lurch from 437 px to 734 px before shrinking to 414 px. The rail now slides in and out
with the workspace panel's own motion, started in the same frame, so the two trade
places and the chat glides between its two widths (413 to 437 px and back, logged
frame by frame against the box). Making way for the workspace panel as it opens, the
rail slides out in its own row instead of floating over the panel. It still leaves at
once when it would float even with no panel open (a very narrow window), beside a files
or logs panel, which takes its full width at once, and beside a panel that was already
open (a narrower window, a session switch, a restore on load); reduced motion skips the
slide. While it slides out it is inert, so it takes no focus or clicks. Only the AE file
`AeSessionRail.tsx` and its tests change; no upstream-owned file. The dual review's
fixes (the files and logs panels, a panel already open, inert) are in the same patch.

Token defrag (2026-10-18): `patches/0097` (web). Compaction showed only a line of text
in the thread. While a conversation compacts, the tray above the composer now plays a
small defragmenter: the system prompt, tool, memory and skill pills stay put, and
message pills hop left one at a time, line up at the end of a pink summary pill and
melt into it. When compaction finishes the pills pour into the context ring, which
holds the old count until they land and then eases down to the new one, and the
marker in the thread adds the before and after counts ("531.6K → 159.5K tokens") for a
compaction the page watched finish, until a reload or a reconnect brings the marker back
from history. Compaction reports only its start and finish, so
the percent is a time estimate that eases toward 97% and the last pills wait for the
real finish. Clicking the strip opens a Win98-style "Defragmenting Context C:" window
with a live cluster map, a legend and a completion box. Settings > Chat has a Token
defrag switch that turns it off for the device.

Triage counts side by side (2026-10-18): `patches/0098` (web). The rail's Triage card
gave Absorbed and Unfiled a row each, which left the card taller than it needed to be.
The two counts now share one row. Opening one shades it and lists its lines full width
below the row, one list at a time.

Claude account on fork (2026-10-18): `patches/0099` (server and web). Upstream v0.17.0
retired switching a session's agent in place, which is how a running Claude session
moved to another of the host's Claude accounts. Now the fork dialog's run-config section
has a Claude account row for a Claude native fork that stays on the source session's
host: "Same as this session" (the default, nothing sent) or one of the host's detected
Claude logins. The server checks the pick against that host (the caller must own it, and
it must have no provider credentials) and stamps the account label on the fork; a fork
bound to another host fails at launch if that host lacks the account. Switch harness and
Change lead open the same fork dialog.

Saved Absorbed notes (2026-10-19): `patches/0100` (web) and the observer in
`packages/ae_omni_policies`. The rail's Triage card counted Absorbed: and UNFILED lines
from labels but found their text only in the messages the chat had loaded, so Reid saw
"Absorbed 1" over "The lines are in older messages not loaded here." The observer now
saves each counted line as a label of its own (`ae.absorbed.1` and on, `ae.unfiled.1` and
on; a label holds 256 characters, so one per note, the first 20 of each kind), and the
card lists those notes first, then any loaded line not saved yet. Lines counted before the
change get their notes from `just triage-notes-backfill <url>`, run once after the
deploy; it writes only notes that are missing, and leaves alone a session whose history
holds a different number of lines than it counts.

Rail slide after a session switch (2026-10-19): `patches/0101` (web). The v0.18 sync made
upstream animate the workspace panel only when its header toggle, a link or a tab opens or
closes it (`setRightPanelOpenAnimated`, which sets `data-animate-visibility`); a session
restore now snaps it. So with Settings > Chat > "Open the session rail in every session"
off, switching from a session with its panel closed to one with it saved open snapped the
panel to full width while the rail slid out over 300 ms: both showed, and the chat dropped
from 437 px to 181 px for about 280 ms (logged twice on the box). The shell now passes that
flag to the rail as `workspace.animating` (one line in `AppShell.tsx`), and the rail slides
out in its row only when the panel it made way for is sliding open; a snapped panel makes it
leave at once, as a files or logs panel already does. Logged again on a dev build: the
switch shows one pane at a time with the chat going straight from 437 to 414 px, and the
header swaps still glide.

Global Policies owned by the page (2026-10-18), no core patch. Settings > Global
Policies showed the five AE policies with a Config badge and no switch, delete or
edit, because they ran from the `policies:` block of `config/server.yaml` inside the
image. Reid asked to manage them there and chose to let the page own them. The block
is now `ae_seed_policies`, and the container's entrypoint wrapper inserts it into the
policy store, in one transaction, when the store holds none of its policies (matched
by name or handler; `packages/ae_omni_policies/src/ae_omni_policies/policy_seed.py`).
Each entry is checked the way the page's create route checks one, and its factory is
built once, so a bad value stops the container and the auto-update rolls back. From
that first boot on, the five are ordinary global policies: switched, edited (through
`patches/0094` and `0095`) and deleted on the page, with the change surviving
deploys. Editing the block changes only a brand-new server, and deleting every
seeded policy brings the block back at the next restart. Details in
`packages/ae_omni_policies/README.md` ("How it loads").

Sidebar sort modes (2026-10-19): `patches/0102` and `0103` (web), the `GET` and `PUT
/v1/ae/sidebar` route, and upstream's `sidebar_grouping_ui` release flag, which the
image's entrypoint now turns on. Reid asked to group sessions by status with clear
separation, or to order them by hand. The sidebar's filter menu gains a Triage grouping
(Blocked, Working, Review, No status, Parked, Reference, each with its glyph and count,
then a collapsed Archived group that loads when opened) and a Manual ordering. Manual
applies inside every list (Sessions, Shared, each project folder, each group): drag a
row onto another row of the same list, or use the row menu's Move items, which also
work on a phone. New sessions land on top. The order follows Reid across devices
through `packages/ae_omni_policies/src/ae_omni_policies/sidebar_prefs.py`, kept in
`/data/ae-sidebar.json`; the grouping choice stays per device, as upstream keeps it.
Settings > Chat has Reset manual order. `0103` keeps the bulk-select bar inside a narrow
sidebar: where the words do not fit, it shows the bare count beside the four icons.

Shortcut chips (2026-10-19, `patches/0104`, web): the New session row's hover chip read
"CtrlAlt N" on Windows and Linux because each key sat in a fixed 16 px square. Keys now
size to their label, and the chip hides when the sidebar is too narrow to show it beside
the label.

Drag in any Ordering (2026-10-20): `patches/0105` (web). Reid first picked the project
sort in the Projects header's ⋯ menu, since the session ordering sits behind the filter
icon, and session drag did nothing. Now dragging a session onto another in its list, or
the row menu's Move items, works from any Ordering. The first move switches that device
to Manual, keeps the list as shown (or the order synced from another device), moves only
that row, and shows a toast saying where to switch back. Older sessions that load later
stay below the arranged ones instead of jumping to the top.

Status ordering by icon (2026-10-20): `patches/0109` (web). Reid picked Ordering: Status
and found a session he had checked as Review sitting between two Working ones, because
upstream's Status ordering reads live activity (approvals, unread, running, idle), not
the status icons. It now sorts by the icon first, in the Triage order (Blocked, Working,
Review, No status, Parked, Reference), then by live activity, then newest first; project
folders sort by icon, then newest first.

Shared Claude accounts (2026-10-20): `patches/0117` (host, server and web) and
`ae_omni_policies/shared_accounts.py` with its routes. Why: so a second AE user's host
can use company-paid Claude accounts that the admin manages, without copying tokens
between hosts. Until now a named Claude account lived on one host. The server now keeps
a pool of shared accounts (name, label, the `claude setup-token` token encrypted with
Fernet under `AE_SHARED_ACCOUNTS_KEY`, who added it and when) in
`/data/ae-shared-claude-accounts.json`. Every user's account picker, in the composer,
Settings and the fork dialog, lists them after that host's own logins with a Shared
tag (Tabler users-group); a host selects one by its id, `shared:<name>`, which no host
login can be, so a host's own "Team" and the shared "Team" never clash. At launch the
session's runner fetches the token from `GET /v1/ae/claude-accounts/shared/<name>/token`
with its own server credential (the session owner's, so a host owned by a user who is
not an admin works) and puts it only in the launch environment: never on the host's
disk, never cached. That route refuses any request carrying a browser's `Sec-Fetch-*`
headers and reads identity without the cookie, so no page ever receives a token; the
list route answers names and labels only. Admins add, relabel and remove shared accounts
on the Admin Center's Shared Claude accounts section (`GET` and `POST
/v1/ae/claude-accounts/shared`, `POST .../{name}/label` and `.../{name}/delete`); admin
is the server's own `/v1/me` answer. The key goes in Key Vault and
`/secure/env/omnigent-ae.env`: `deploy/README.md`, "Shared Claude accounts". Usage and
the status line name a shared session's account `shared:<name>`, shown as
"<name> (shared)".

Shared Claude accounts in Settings (2026-10-20): `patches/0127`. Why: the pool is
managed where the accounts are used, beside each host's own Claude logins, instead of on
a separate Admin Center page. Settings, Claude accounts, now ends with a Shared group
(Tabler users-group) listing every shared account with its id, `shared:<name>`, and who
added it; the host's own list no longer repeats them. Everyone sees the group and can
select a shared account there for new chats. An admin (the server's own `/v1/me`
answer, or the list route's `can_manage`, which also covers a single-user install) also
gets a Tabler pencil (Rename) and trash (Remove) on each row, Remove asking once, and
a + Add button. The Add form (Name, Label optional, the OAuth token from
`claude setup-token`) stays closed until + Add is pressed and closes on Cancel or after
a successful add; the token field is a password input cleared on submit, whatever the
answer. The routes are the same ones the Admin Center section uses, and the server
checks admin on every change, so the controls are only chrome. No server change.

Team selection and model context (2026-10-13, core patches `0030` and `0032`):
the composer has an Auto / Solo / Team control with a lead switcher and collaborators
from the live agent and model catalogs. Auto lets the lead choose useful collaborators,
while native subagents remain available in every mode. Agents needing setup are hidden
until requested. Saving Team provisions idle child sessions; the lead receives their
roster with the next message and coordinates their work. Specific selections turn off
automatic collaborator routing. The usage popup labels the active agent and model,
then shows each child session's separate context reading above the account-wide
plan limits. Missing readings say Unavailable, and switching providers clears the
old meter immediately. See `docs/PLAN-LIMITS.md` for verification steps.

Dashboard (2026-09-24): the seventh core patch, `patches/0007` (P7, web). Reid found "Your move", "Parking Lot" and
"Slots" three nav entries for one feature, the difference between parked sessions
and the Parking Lot unclear, and Reference a long scroll away. After a design
review (`docs/design/dashboard-review.md`), the extension's two pages are one
Dashboard, reached from one sidebar row, "Dashboard n/3" (P1's Slots row, renamed by
P7; the extension contributes no nav row of its own). The page: a sticky bar of section chips (Working n/3 is the slot count) and the plan limits,
then Your move (gates, approvals, due lot items, pull requests, the brief), Working
n/3, Blocked, the Parking Lot (a parked session shows on its note's card), Review,
Needs a note (parked with no note, or an unfiled note), Unsorted (no status) and
Reference. Every section collapses and stays as Reid left it; Reference and Unsorted
start closed. Park on a working card opens the park form so a park leaves a note.
P7 also adds a chip to the session header with the session's status and the slot
count ("n/3", red at three; a tap opens the Dashboard), so the count is on every
session and on the phone, where the sidebar is a closed drawer. Its upstream edit is
two lines in `ChatHeader.tsx`. The eighth patch, `patches/0008` (P8, web, no
upstream file touched), puts the same count on the installed app's icon through the
Badging API (`navigator.setAppBadge`; on iOS 16.4 and later for a Home Screen app,
once notification permission is granted, which upstream already asks for on the
first tap). Before and after screenshots are in
the dashboard-review screenshot set (removed before publication).

Dashboard layout pass (2026-10-14), no core patch. Reid asked twice that day for the
Dashboard to read better on the desktop and the phone. The first pass
(https://github.com/analyticendeavors/omnigent-ae/pull/75) folds empty sections into one
muted "Nothing in:" line, drops their section chips and gives every session card one row
shape. The second pass rebuilds the layout from a render of a busy day, where the page
was 2836 px tall at 1400 px and 4091 px on a 390 px phone, the right column ended at a
quarter of the page, eight lot tasks took 1044 px on the phone, and about 550 px
repeated what Your move already showed. Reid chose four changes: the Parking Lot heads
the right column (Your move, Open PRs, Working and Blocked stay on the left, and the
phone order is unchanged); each lot task is one line (quadrant icon, title, date or
"today", a parked marker, the chevron and Resume), with repo, tags, NEXT, the task-store
link and a parked session behind the chevron; sections are a heading over a rule and
their items borderless rows with a colored left edge, where a session's title opens it
and Park and the status moves sit in a "..." menu (arrow keys, Escape, focus back to the
button, kept open across the 20-second refresh); and each item shows once: what Your
move shows (a gate, an approval, a task due today) is left out of Working, Blocked and
the lot, whose headings say "1 in Your move" and keep the true count, while a lot search
or filter still looks at every task. The page is now 1131 px at 1400 and 2422 px on the
phone, and the visible buttons went from 91 to 61. The dual review (Codex and Claude,
2026-10-14) found that hiding repeats had taken Park and the status moves from sessions
shown only in Your move and a parked session from a due task; Your move's cards now
carry the same "..." menu and parked rows, repeats are hidden only while Your move's
answer is fresh, the Filters sheet is built from every task, and the menu no longer
moves focus on a refresh. Out of tree only (`packages/ae_omni_extension/web/src`:
`main.ts`, `lot.ts`, `cards.ts`, `capacity.ts`, `sessions.ts`, `yourMove.ts`, `dom.ts`,
`icons.ts`, `style.css`). Before and after screenshots are in
the dashboard-pass screenshot set (removed before publication). On 2026-10-15 Reid found two layout bugs on his 1080
px window: the seventh section chip wrapped to a second row, and a merged card's Dismiss
dropped to a line of its own under Mark session done. The chips now stay one row on
every width, scrolling sideways with a fade when they outgrow it (a mouse wheel over
them scrolls them too), and Dismiss is an icon button in the merged card's head.

Fast open (2026-09-26), no core patch. Reid waited 3 to 5 seconds on every open of
the Dashboard on his iPhone, looking at an empty page. Measured on the box that day
with his login (read-only GETs, two each): `/v1/ae/prs` 2321 ms cold and 12 ms warm
(the tracked-PR scan, one loopback per session, 40 sessions, 6 at a time, cached a
minute), `/v1/ae/lot?status=open` about 320 ms on every call (task-store through
Cloudflare, uncached), `/v1/ae/lot/tags?icons=true` 471 ms cold and 122 ms warm,
`/v1/ae/your-move` 245 ms cold and 6 ms warm. The page awaited all seven calls before
its first render, so the cold scan, whenever the Dashboard had been closed for a
minute, set the wait. Three changes. The server serves the scan, the list's cards,
the lot and the tags stale-while-revalidate (`ae_omni_policies/swr.py`): a held
answer past its TTL (60 s for the scan and cards, 20 s for the lot and tags) comes
back at once while one background read replaces it, so only the first read after a
server start waits; the scan runs 12 sessions at a time. `?fresh=true` on `/prs`
still awaits a scan (Merge and the MCP `list_prs` tool rely on it), any AE write to
the lot (park, link, resume into a session) drops the lot and tag caches so a Park
never shows the old lot, and a failed background read keeps the last answer and says
so (`errors` on `/prs`, `refresh_error` on `/lot`). What can be stale: a PR card up
to a minute plus one poll (Merge re-reads the gate fresh and is pinned to the head
SHA, so a stale card can never merge something unseen), the scan the same, and a lot
change made outside this server (the app, the GitHub bridge closing a task) 20 s plus
one poll; past five minutes a failing task-store is no longer papered over. The page
renders each section as its own call settles, and first paints this device's last
answers (`storage.user`, `web/src/snapshot.ts`), dimmed with a "refreshing" line and
every card button off (Merge above all) until that section is re-read.

Gate buttons (2026-09-27): an open gate's buttons read "A", "B", "C" and "Close", and
Reid asked for each to say what it does. They now read Do it now, Write a note, Save
for later and Drop it. Only the labels changed: each still posts A, B, C or "close
it" as the session's next user turn through `POST /v1/ae/gate/{id}/reply`, and its
tooltip names the letter so it can be matched to the gate's RECOMMEND line. The
recommended button is not marked, because the observer captures the FORK clause
(`ae.fork`) and not RECOMMEND. On a phone the gate's buttons wrap two to a row.

PR cards (2026-09-24): the twelfth core patch, `patches/0012` (P12, web). Reid merged
every pull request by hand on github.com from his phone. Each pull request a session
tracks (upstream's per-session tracking,
https://github.com/omnigent-ai/omnigent/pull/6935) is now a card at the top of the
session's GitHub panel and, compact, under the chat message that names it, and every open one across sessions is a card in the
Dashboard's new Open PRs section, right under Your move (a pull request with a card
is no longer repeated in Your move's list). A card shows the title, repo and head
branch, CI with counts, mergeability (clean, conflicted, blocked, behind, draft,
checking) and review state, and a Merge button that is off with the reason when CI
is red or running, the branch conflicts, protection blocks it or a reviewer asked
for changes. Merge asks once (squash or merge commit, whichever the repo allows,
squash first) and is pinned to the head SHA the card showed; afterwards the card
offers "Mark session done" (it sets the session to review, which frees a slot), but
only when that merge closed the session's last open tracked pull request (patch
`0020`, P20, 2026-09-25: a session that had opened four pull requests was offered
review after the first merge, while three were still open). On 2026-09-26 Reid sent
screenshots of a 1100 px card: the method buttons sat on one row with Confirm merge
and Cancel on a second, and a black "Set this session to review" button did not say
what it does. Patch `0022` (P22, web) puts the confirm step on one row (a compact
Squash / Merge commit toggle when the repo allows both, Confirm merge, Cancel) that
wraps to two only on a narrow card such as a 375 px phone, and relabels the offer
"Mark session done", an outline button with a muted "frees a slot" hint beside it;
the Dashboard's cards got the same row and label out of tree. It builds on patch
`0021` (below), whose stacked phone buttons it replaces. While others are open the merged card offers
nothing and says how many are left in a muted line ("3 more open pull requests in
this session."); the Dashboard's merged card applies the same rule per session from
the Open PRs list (`packages/ae_omni_extension/web/src/reviewOffer.ts`). On a phone
both surfaces gave Merge the full card width until 2026-09-27 (`0026`, below) and draw
the method as a segmented control with the chosen one ticked (patch `0021`, P21, 2026-09-25,
https://github.com/analyticendeavors/omnigent-ae/pull/53; its quiet Cancel under
Confirm gave way to `0022`'s one row). Three routes in `ae_omni_policies/prs.py` and
`router.py`: `GET /v1/ae/prs` (reads each session's tracked list through the
server's own loopback, cached a minute), `GET /v1/ae/prs/{owner}/{repo}/{n}` and
`POST .../merge` (JSON only, origin-checked, a signed-in user, the gate re-read
fresh). They use `AE_GITHUB_TOKEN`, which now needs write scopes
(`deploy/README.md`, "The GitHub token", has the exact list). Screenshots in
the pr-cards screenshot set (removed before publication).

Open on GitHub (2026-09-27): the twenty-fifth core patch, `patches/0025` (P25, web).
Reid hates copy and paste and works from an iPhone (Safari) and a Windows laptop
(Firefox), yet every Dashboard pull request card offered only Copy link: the page runs
in a frame with `sandbox="allow-scripts"`, and the host's `navigation.openExternal`
opened only URLs it had handed out itself. The patch lets the host also open a
github.com pull request or issue URL, written exactly the way GitHub's API returns it
(`web/src/lib/aeExternalUrls.ts` in the fork; the upstream edit is one condition), and
the Open PRs and Your move cards now carry Open on GitHub, with Tabler's external-link
icon, in place of Copy link (`packages/ae_omni_extension/web/src/openLink.ts`). A tap
opens a new tab in a browser, the OS default browser from the desktop app (upstream's
Electron window-open handler sends web links to `shell.openExternal`), and Safari from
the iPhone Home Screen app. If the host refuses (a server without the patch, or a
timeout), the button copies the link as before and says so on the card. The session's
own cards (patch `0012`) already link their title to GitHub and are unchanged.

Open on GitHub fallback (2026-09-27, review of
https://github.com/analyticendeavors/omnigent-ae/pull/58): the twenty-seventh core
patch, `patches/0027` (P27, web). Both reviewers (Claude and Codex) found that a tab
the browser blocked looked opened: the host called `window.open` with `noopener`,
which returns null even on success, and the call runs one MessagePort hop after the
tap, where iOS Safari may block it; the copy fallback never ran, and Copy link was
gone. The host now opens without `noopener`, clears the new tab's `opener` itself, and
rejects with `PopupBlocked` when no tab opened (native shells keep upstream's call,
since their policy reports null for every open). On the Dashboard a small Copy sits
beside Open on GitHub again, the path that always works, and a refused open shows the
URL as selectable text in the card's status line ("Tap and hold to copy"), with no
clipboard write, which iOS refuses after the round trip. A merged card's Dismiss is
now its own group at the row's end, so the "frees a slot" hint stays beside Mark
session done.

Recommended merge method (2026-09-28): the twenty-eighth core patch, `patches/0028`
(P28, web). Reid sent a screenshot of the chat card's confirm step ("Squash and merge
#60 into main?", Squash ticked) and asked that tapping Merge show which method is
recommended, select it by default, and keep the other selectable. Squash was always
preselected, and a squash is wrong when a pull request promotes one long-lived branch
into another: it puts a commit on the base that the head never gets, so the next
promotion conflicts. The rule lives in the server, `recommend_merge_method` in
`ae_omni_policies/prs.py`: a merge commit when the head branch is `main`, `master`,
`dev`, `develop`, `staging` or starts with `release/` or `hotfix/` ("Merge commit
keeps dev and main in step; a squash would make the next promotion conflict."),
otherwise squash ("Squash lands this change as one commit on main."), and when the repo
allows only the other method, that one ("The only method this repo allows."). The
card's `merge` block gains `recommended_method` and `recommended_reason`, and
`default_method` now equals the recommendation, so a merge POST without a method uses
it too (both UIs always send one). Both confirm steps, the chat card (`0028`) and the
Dashboard's Open PRs and Your move cards (`packages/ae_omni_extension/web/src/prs.ts`,
`style.css`), open on the recommended method, tag its segment with a small muted
"Recommended" pill that also carries the reason as its tooltip, and show the reason as
one muted line under the question. The question follows the selected method as
before, and the other method is one tap away.

Scheduled runs open in a project (2026-10-13): the thirty-third core patch,
`patches/0033` (P33, server and web), and the seeder. Every firing opened its session outside
any project, so runs piled up loose in the sidebar; the steward and the shutdown note
had been moved into "Automation Notes" by hand. A scheduled task now carries an
optional `project_id` on `/v1/scheduled-tasks` (create, patch, get, list) and on the
agent's `sys_scheduled_task_create` / `_update` tools, kept in its own table
(`ae_scheduled_task_project`, created on first use, no Alembic revision). It must name
one of the owner's projects; null or "" leaves runs unfiled. Each fire files the new
session with the same store call the sidebar's Move to project uses; a deleted project
leaves the run unfiled with a warning and never fails the fire. Reid chose a server
default (2026-10-13): a create that names no project gets the owner's project named by
`OMNIGENT_SCHEDULED_TASK_DEFAULT_PROJECT` in `config/server.env` (`Automation Notes`),
stored by id so a rename keeps it. The automation dialog has a Project picker. The
seeder (`ae_omni_policies/schedules.py`) fills a missing project on the two shipped
tasks and never replaces one Reid chose.

Auto-resume after a host restart (2026-10-13): the thirty-fourth core patch,
`patches/0034` (P34, server), and `ae_omni_policies/auto_resume.py`. A host restart
(a deploy, an update, a crash) kills every runner it spawned and comes back with new
runner ids, so a session that was mid-turn sat at "The connection to the host dropped
unexpectedly" until Reid clicked Resume, and Resume only relaunched the runner without
continuing the turn. Upstream already continues an interrupted turn (a "your runner was
interrupted, continue" prompt), but only for sub-agents whose parent's old runner
reconnects. P34 is the mechanism: observers run after a host connects, and a
request-free version of Resume that asks the relaunched runner to continue. The
policy is out of tree: on a host's connect, its top-level sessions that upstream counts
as interrupted (mid-turn, or failed with `runner_disconnected` or
`runner_failed_to_start`; not archived, closed or stopped on purpose) and touched in the
last six hours, one try per dead runner (`ae.autoresume`). `AE_AUTO_RESUME=0` turns it
off. Since 2026-10-18 a session whose runner is still alive is left alone and
keeps its one try for the restart that really kills it: a deploy restarts the server
first, the old host reconnects for a few seconds with its runners alive (upstream skips
the continue turn then), and on ae-v0.16.0.50 that reconnect spent the try, so the real
restart three seconds later was skipped. Alive means the server's runner router has it
connected or the connecting host reports the process alive (tunnels can lag the host's).
Skipped interrupted sessions are logged at INFO.
A resumed turn may repeat an action whose outcome was lost (the prompt asks the
agent to check before repeating), the one risk Reid accepted with it.

Scheduled-run retention (2026-10-13): the twenty-ninth core patch, `patches/0029`
(P29, server), and `ae_omni_policies/retention.py`. Every firing of a scheduled task
starts a new session, and the steward and the shutdown note fire every weekday, so about
ten run sessions a week piled up in the sidebar with nothing to reuse or prune them.
Reid chose automatic retention: after a task fires, keep its newest `keep_runs` run
sessions (default 5, one working week) and delete older ones. `keep_runs` is a field on
`/v1/scheduled-tasks` (create, patch, get, list) and on the agent's
`sys_scheduled_task_create` / `_update` tools; null keeps every run, and the seeder never
sends it, so a value Reid sets survives a reseed. P29 is the mechanism: the setting, kept
in its own table (`ae_scheduled_task_retention`, created on first use, no Alembic
revision), a fire-observer seam awaited after each successful fire, and a request-free
delete that mirrors `DELETE /v1/sessions/{id}`. The policy is out of tree and fails
safe: only sessions a run row of the same task points at, never the run that just fired,
and older ones only when idle (not running, waiting or failed, no running sub-agent or
background shell), with no approval outstanding and no open gate, `ae.status` neither
working, blocked, review nor parked and either `reference` or archived, and still titled
with the task's name (a renamed run was kept on purpose). There is no migration: the
first firing after deploy walks every run row the task has and clears the backlog under
the same rules. Retention does not look at projects; patch 0033 above
files runs into one, and pruning works the same inside it.

Dense PR cards (2026-09-27): the twenty-sixth core patch, `patches/0026` (P26, web),
Reid's second note that the cards waste space. His screenshot of the compact card
under a chat message, about 1200 px wide, had every piece on its own full-width row
with the right two-thirds empty: the chips, a green "Merged #55 (squashed), b0413d6.
Pull Request successfully merged", a muted "1 more open pull request in this
session.", and on a conflicted card the reason and then a tall disabled Merge. Each
card now has one status row that wraps only when it does not fit: the chips, then the
outcome or the reason Merge is off, then Merge or Mark session done at the row's right
end. A merged card reads "Merged (squashed) b0413d6" (GitHub's echo is gone) with the
"N more open" note inline beside it. Buttons are 32 px with a mouse and 40 px on a
touch screen, the Dashboard's size, and the card's padding and row gaps are tighter.
The patch touches only `0012`'s own `AePrCards.tsx` and its two suites; no upstream
file. The Dashboard's Open PRs cards and Your move pull request rows get the same row
out of tree (`statusRow` in `packages/ae_omni_extension/web/src/prs.ts`, `.pr-status`
in `style.css`); there a merged card keeps its number ("Merged #55 (squashed)
b0413d6"), since it has no repo line, and Dismiss sits at the row's end. On a 375 px
phone the chips, the reason and the buttons each take a row, the buttons sharing
theirs.

Calmer PR cards (2026-10-14): the thirty-seventh core patch, `patches/0037` (P37, web).
Reid sent a screenshot of two chat cards, one with its confirm step open, and asked for
suggestions to make them prettier or better designed. Three outlined chips read as
buttons, the confirm step nested a "Recommended" pill inside a segment inside a bordered
box inside the card, and a long title was cut to one line. The header now leads with the
pull request's state in GitHub's colors (Tabler `git-pull-request` green when open,
`git-merge` purple when merged, closed red, draft gray), the title wraps to two lines,
and the short repo name and number sit at the right end (under the title on a phone). A
card whose Merge is live shows one line, "Ready to merge · 8/8 checks", in place of
three chips; the chips come back only when something blocks the merge, each an icon and
a few words, gray when fine and red on a faint tint only for failing checks, conflicts
or requested changes. The confirm step is a section of the card under a divider:
"Recommended · <reason>" on one muted line, then the method toggle and, at the right
end, Cancel and a button that names the target ("Squash into main", "Merge into main"),
since the question line that named the base branch is gone. A merged card reads "Merged
(squashed) b0413d6" in purple and drops the chips, which described the open pull
request. Reid chose the target on the button, purple for merged and the single Ready
line (2026-10-14). The patch touches only AE files (`AePrCards.tsx`,
`AeInlinePrCards.tsx`, `aePrs.ts`, their suites) and a new `aePrIcons.ts`; the
Dashboard's Open PRs cards and Your move pull request rows get the same design out of
tree (`prs.ts`, `icons.ts`, `style.css`, with a `--merged` token in both themes). The
dual review (2026-10-14: Codex found nothing, Claude's /code-review six findings, all
fixed) added patch 0039 and Dashboard fixes: a neutral icon until a card's state is
read, a merged or closed Dashboard card's border follows its state, Cancel stays compact
beside the wider confirm button on a phone, and on a narrow Dashboard card "Open on
GitHub" reads "GitHub" while Copy is a copy icon named "Copy link" at every width, so
Merge, GitHub and Copy share one row.

Lot search and tags (2026-09-25, https://github.com/analyticendeavors/omnigent-ae/issues/28),
no core patch. Reid reads the lot on his phone and asked for four
things. Every tag shows on a lot card, wrapping, in task-store's own colour and Tabler
icon (`GET /v1/ae/lot/tags` rows now carry `color` and `icon`; `?icons=true` adds the
icons' path data, read once from the app's `/tabler-icons.json` and kept), and a tap
on a tag filters by it; `parked`, which every lot task carries, is left off. The
collapsed card shows the task's repository (`repo` on each `/v1/ae/lot` task, from
the note's `REPO:` line or a GitHub link). The Parking Lot section has one search
field (title and NEXT) and a Filters sheet (quadrant, overdue or this week, tag,
repo), remembered per device. The header lost its strip (the Slots pill and a chip
per session repeated the sidebar); the section chips carry the slot count on
Working and wrap across the width beside the plan limits. Screenshots in
the lot-search screenshot set (removed before publication).

Fabric Admin (2026-10-16, renamed the Admin Center on 2026-10-18). The page itself is
extension code; three core patches serve it (`0059`, the Dashboard row no longer lights
on this page, `0066`, the Azure and Fabric link allowlist, and `0106`, its Databricks
workspace and warehouse links). Reid wanted the aetraining Fabric
capacity's wake button, with room for more capacities, inside omnigent-ae next to the
Dashboard; Off came at his request, guarded by a separate suspend key so holders of
the shared wake link can only wake. The extension's first nav row (first "Fabric Admin", now "Admin Center",
in upstream's extension slot) opens a page with a card per capacity, its state, and
Wake or Off, each asking once on one row. `GET /v1/ae/fabric` and the two POSTs
(`ae_omni_policies/fabric.py`) reach one Azure Logic App per capacity, found from
`AE_FABRIC_WAKE_URL_<NAME>`, with the optional `AE_FABRIC_SUSPEND_KEY_<NAME>` for
Off; both come from Key Vault through `deploy/pull-secrets.sh` and never reach the
page or a log line. A second pass the same day made each capacity one compact row
with its SKU, region, price, uptime and expected sleep time, a links menu (Azure,
Fabric) and Keep awake (`POST /v1/ae/fabric/{name}/keepawake`, 1 to 12 hours, same
key). On 2026-10-18 the page was renamed Admin Center because it becomes the hub for
the admin sections still to come (Databricks compute, spend, deploy hold, secrets
health); the Fabric capacity cards moved under their own "Fabric capacity" heading,
the first section. The page id, route, API paths and `AE_FABRIC_*` variables did not
change, so old links keep working. Details in `packages/ae_omni_extension/README.md`,
"Admin Center". On 2026-10-19 a second section, Databricks, joined it: each SQL warehouse
on the Databricks on AWS workspace with Start and Stop, and a spend strip showing this
month's spend and the spend since the credit started, each against the $200 credit
(`GET /v1/ae/databricks`, `POST /v1/ae/databricks/warehouses/{id}/start` and `/stop`,
`POST /v1/ae/databricks/spend/refresh`, `ae_omni_policies/databricks.py`). It signs in
as a service principal whose OAuth secret has scope `sql` only, and reads spend from
`system.billing` only when the warehouse is already running or someone taps Refresh,
so it never wakes a warehouse on a timer. Setup in `deploy/README.md`, "The Admin
Center's Databricks secrets (optional)". Later on 2026-10-19 a Deploys section went
to the top of the page. It shows the running release, whether a newer one is
waiting and on how many busy sessions, and buttons to hold deploys for 1, 2 or 3
hours, plus Resume (`GET` and `POST /v1/ae/deploy/hold`, `POST /v1/ae/deploy/resume`,
`ae_omni_policies/deploy_hold.py`). `deploy/auto-update.sh` reads the hold through
`compose.sh exec` before each pass and again just before it writes a new tag. While
the hold is on it deploys nothing and rebuilds no host. When it cannot read the hold,
it deploys anyway. The hold ends on its own. Why: a deploy ends every turn in flight,
and the only way to hold one was to stop the auto-update timer by hand, which no page
showed. Details in `deploy/README.md`, "The deploy hold".

The Admin Center as a dashboard (2026-10-20, extension only). By then the page had six
sections in one 720 px column, and on a wide pane you had to scroll to learn whether
anything was wrong. It now uses the pane's full width. A header holds the title, when
the newest answer arrived, and one refresh button that runs each section's normal read
(never the Databricks spend Refresh, which wakes a warehouse). Under it is a strip of
status tiles: Release, Deploy hold, Box host, Fabric, Databricks, Secrets and Shared
accounts. Each tile has a coloured left edge: green is fine, amber needs a look, grey
is idle and red is a problem. The tiles show only what the sections already fetch, so
the strip adds no requests. Four cards sit below the tiles: Deploys (a ring counting
the hold down, the release line, the hold choices as one segmented control and Resume),
Compute (the Fabric capacities, then the Databricks warehouses), Databricks credit (a
ring of the credit left beside the two bars) and Box health (the host, then the server's
secrets as chips). Each column is its own stack, and the last card in each one
stretches, so the columns end on one line. A shared grid would have left a hole under
each short card, and Reid turned that down. Wide panes get three columns, medium ones
get two, and below 520 px the tiles go away and each card folds to its header line.
The page remembers each card's fold. Shared accounts now appear only as a count tile,
because adding and removing them moves to OmniGent's Settings, a page an extension
cannot open. Details in `packages/ae_omni_extension/web/src/board.ts` and `tiles.ts`.

PR cards include Update Branch and a separate AE compliance review disclosure.
Update Branch sends the displayed head SHA to GitHub and waits for a new revision;
an accepted request keeps Update Branch and Merge disabled across copies of the
card. The server requires a signed-in user and a trusted browser origin. Opening
AE compliance review fetches the full latest trusted bot report with its source,
update time, and verdict. Reports without a reviewed SHA remain unverified, and
reports for other revisions are labelled. The report does not change merge gates.

Context categories (2026-10-15): Claude Code sessions now load a context snapshot
when the Usage popover's detailed breakdown opens (since 2026-10-17, when the popover
or the session rail's Context section opens). Category rows and bar segments
show what occupies the window, with expandable tool, memory, agent and skill
counts and Claude's reported autocompact threshold. Snapshots are estimated,
cached for three minutes, and identify missing MCP connections. See
[Plan limits](PLAN-LIMITS.md#context-categories-2026-10-15) for the data path
and reconstruction limits.

Expanded context usage includes a wrapping color legend immediately below the context window bar.

Merge watcher (2026-10-21): a session continues by itself when its pull request merges, as Claude Code Desktop does, so no session waits for Reid to say "merged". `packages/ae_omni_policies/src/ae_omni_policies/merge_watch.py` runs in the server from the app's lifespan (a route-less router in `DEBUG_ROUTERS`; FastAPI merges an included router's lifespan, so no upstream patch). Every 2 minutes it reads which pull requests the sessions touched in the last day (and any still waiting on a watched pull request) track (`prs.tracked_by_session`, the same `resources/github` list that draws the chat's merge cards); every 10 s (Reid's choice over a webhook) it sends one `GET /repos/{repo}/pulls/{n}` with `If-None-Match` per open one, so an unchanged pull request answers 304 and costs no rate limit. A rate-limit 403 or 429 pauses all polls until `retry-after` or `x-ratelimit-reset`; other failures back off that pull request alone. On open to merged, or to closed without merging, it posts one plain-English note into each session that tracks it ("Merge watcher: PR #196 (title) merged at 17:00Z as 11c89f51 by login. URL Continue: delete its branch and worktree, then check the deploy.") through `POST /v1/sessions/{id}/events` in process, as the session's owner (a five-minute token from the server's own provider), which wakes an idle session the way an external post does. The last seen state, ETag and undelivered notes are kept in `/data/ae-merge-watch.json`, written before a post and after each, so a restart neither repeats a note nor misses a merge that landed while the server was down. The slot cap, the rabbit hole and the fork classifier recognize the note by its opening words (`continuation.py`) and let it through without a slot claim, a turn count or a model call. `AE_MERGE_WATCH=0` turns it off. Tests: `packages/ae_omni_policies/tests/test_merge_watch.py`, plus a case each in `test_slot_cap.py`, `test_rabbit_hole.py` and `test_fork_suspect.py`.

Rebase bot settles changelog conflicts (2026-10-21, `scripts/rebase-patches.sh`): the bot merged only the doc files listed in `DOCS`, and `docs/CHANGELOG.md` was not one of them after the build diary moved there (https://github.com/analyticendeavors/omnigent-ae/pull/193), so every patch PR that added a changelog line stopped the bot with "a person has to merge it" (https://github.com/analyticendeavors/omnigent-ae/pull/199, renumbered by hand). `docs/CHANGELOG.md` is now in `DOCS`: where both sides only added lines at the same spot it keeps main's lines and puts the branch's after them, and the renumber rewrites stale patch numbers in the lines the branch added. Test: `tests/deploy/test_patch_rebase.py` (the both-sides case now adds a changelog line on each side; it fails on the old list with that error).

Rebase bot moves the "after" note (2026-10-21, `scripts/rebase-patches.sh`, https://github.com/analyticendeavors/omnigent-ae/issues/214): the bot renumbered a branch's own patch numbers but left the end of its `patches/README.md` entry, "after `0NNN`", naming the base patch it used to follow. https://github.com/analyticendeavors/omnigent-ae/pull/204 moved from 0142 to 0144 and needed two commits to fix the note by hand. In the lines the branch added, "after" plus the number before the branch's first old patch now becomes "after" plus main's last patch; other mentions of that number stay. Test: the both-sides case in `tests/deploy/test_patch_rebase.py` (fails on the old script).

Fabric capacity fleet (2026-10-21): the Admin Center's Compute card showed aetraining
as Unknown with "the Logic App did not answer the status call within 8 s". The cause
was in the Logic App, not the server: `la-aetraining-wake` held its reply until five
Azure Automation and Activity Log reads finished, and those occasionally stalled (one
took 85 s; 6 of 50 runs passed 8 s). agents-and-automation
caps each read at 3 s, but that cap may not bound a stalled read (a 20 s cap was
already on the 85 s read), so the guard is this server: a row's state comes from
the shared Logic App's ARM read, and a failed details probe keeps the last good
details (without offering keep awake from them).
Reid also wanted every Fabric capacity he has on the card, each with its SKU, state,
Pause or Start, and a resize. The server still holds no Azure credential: a shared
Logic App, `la-fabric-capacities`, does the ARM calls, reached with two optional
secrets, `AE_FABRIC_CAPACITIES_URL` and `AE_FABRIC_CAPACITIES_KEY` (Key Vault
`fabric-capacities-url` and `fabric-capacities-key`, through `deploy/pull-secrets.sh`).
With them `GET /v1/ae/fabric` lists every capacity, merges aetraining's own Logic App
details when its probe answers in time (a slow probe only leaves them out), and prices
each row from its SKU (`AE_FABRIC_USD_PER_CU_HOUR`, default 0.18). Pause and Start go
through the fleet; the new `POST /v1/ae/fabric/{name}/scale` checks the size against
F2 to F64 before any call. A write does not list: the Logic App checks the id, GETs
the capacity, writes, and answers that one row, so the server updates that row only.
The list keeps the 8 s deadline; a write gets 9.5 s (`FLEET_WRITE_TIMEOUT_S`, under
the extension SDK's 10 s), and one that runs out says it may still be under way.
When the fleet fails before any write can have gone out (the list failed, timed out
or missed the capacity, or the fleet answered 403 or 404), Start and Pause fall back
to the capacity's own wake Logic App when it has one; a write that timed out or
answered 502 never falls back. A capacity whose subscription Azure did not answer
for keeps its last known row, marked with an error sentence that names no id. With
the fleet set up, keep awake answers the fleet's row, so the page keeps the fleet's
SKU and price. On the page each row's SKU is a size picker, and a pick
opens a confirm naming both sizes and both prices an hour; only its Resize button
sends the call. Details in `packages/ae_omni_policies/src/ae_omni_policies/fabric.py`
(module note) and `packages/ae_omni_extension/README.md`.
