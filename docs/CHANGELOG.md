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
`pull-secrets.sh` (Key Vault to the two env files, 640 root:<user>, atomically, no
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
allows one memory call and carries on when it is unavailable. A daily-use page (it
was `docs/DAILY-USE.md`, which is no longer in the tree) was written for everything after the deploy: adding a laptop or WSL host (`omnigent login`,
`omnigent host`, `host enable`; Windows needs WSL), `omnigent import --harness claude
--last 25` and where the sessions land, the phone (Tailscale, Safari, Add to Home
Screen, the Dashboard, PR cards, the Run and Paste buttons, Remote Control from the
Claude app), and travel mode with only an iPhone (rewritten
2026-09-24; it said laptop before): code in Claude Code on the web, box work in a
session on the box's host, root steps through Cockpit's terminal, what still needs
a keyboard, a pre-trip checklist with one reboot at home, and what to do when the box
is unreachable. Done when the owner opens the
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
session on a host often replies with a shell command that the owner used to copy by hand
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
`main` the whole release, for when the owner has only a laptop and Claude web
sessions cannot push tags. When CI passes on a push to `main`
it tags the tip `ae-vX.Y.Z.N` (the next N for the pinned upstream tag, 1 after a
sync), but only when something that ships changed since the last tag and the commit
has no `ae-v*` tag yet, then dispatches `image.yml` on that tag to publish the
image; the box's auto-update timer deploys it. Docs-only and tests-only merges make
no tag; to ship one anyway, or re-ship, run the Release workflow on `main` with
`force` on.

Your move (2026-09-24): no core patch. One screen of everything waiting on the owner,
because across a long day they kept asking what was left and will merge from their phone.
`GET /v1/ae/your-move` (`ae_omni_policies/your_move.py`) aggregates
sessions that need them (an open gate, an approval outstanding, `blocked`, `review`),
lot items resurfacing today or overdue, the steward's latest brief and their open pull
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
own colors and a caption, because the owner read that bar as a second meter.

Shorter usage popover and light tooltips (2026-10-14): `patches/0040` and `patches/0041`
(P40, P41, web). The owner found the popover too tall (933 px with their numbers, so it
scrolled) and sent Claude Desktop's context popover as the model. It now opens on the
context header over one bar with the prompt cache parts joined into it, each plan limit
takes two lines (label, reset and percent, then the bar), and the other plan and the
collaborators sit behind "See detailed breakdown"; the explainers moved behind
tap-to-open info toggles. Folded it is 371 px. They also read the light-mode tooltips, a
near-black box with white text, as a dark-mode bug: patch 0041 gives every tooltip the
popover surface in both themes, the style upstream's skill pills already used.
`docs/PLAN-LIMITS.md` ("The shorter popover") has the details; screenshots in
the usage-panel screenshot set (removed before publication). On 2026-10-15 `patches/0047` cleaned its detailed
breakdown: Claude Code's own subagents (rows like "general-purpose:a45b4b17...", which
never have a reading) are gone, rows read by name, a missing reading is one "No reading
yet" line, model ids read as names, and a 1M Claude Code window shows how far it is from
auto-compact (`docs/PLAN-LIMITS.md`, "The cleaner breakdown").

Fast mode switch (2026-10-14): `patches/0044` (P44, server, runner and web). The owner asked
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
web). The owner sent the Agents graph drawn as one top-down tree about 2000 px wide, which
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
After the redesign went live the owner sent the pane with every Model cell empty and each
status dot floating in that gap ("still seems the same??"). The routed model was the
only source, and agents are rarely routed. Child session rows now carry the model the
agent actually ran on: the harness's own report, else the model its own usage was
recorded against (Codex collaborators already report that), else the requested
override. The Claude Code forwarder now reports each subagent's `message.model` to its
mirrored child, so Claude Code subagents fill in from the deploy on; ones that finished
before it stay blank. Claude ids print as names ("Sonnet 5.5"), other ids as the
harness spells them ("gpt-6-astra"), and the status dot sits right of Time. Screenshots
in `docs/screenshots/agents-pane/model-*.png`.

Session rail (2026-10-17): `patches/0071` (P71, web, runner and server). The owner sent
Claude Desktop's session rail and Codex's usage popover and asked for the popover's
detail to move into a pane beside the chat that they can reorder, plus triage,
schedules and the files they sent. A toggle in the chat header (on a phone, "Session
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
of the session rail the owner asked "are you still working? I can't tell": a session row
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
says what it is doing without opening it. The owner asked for a brief preview of whether a
session is waiting or working, so they would not have to click into each one. The hover
card gains a status block under the title, read from the row's own state so it always
matches the row's icon: "Working" with the turn's elapsed time and current step, "Quiet
for 9m" with the last step, "Waiting on you" with the open question or the step awaiting
approval, "Waiting on 2 agents" with each running agent's newest step (two at most), or
"Idle" with the first line of the last reply. The details come from one read of the
session's newest items (`lib/aeSessionPeek.ts`), only while the card is open, re-read
every 5 seconds so an open card follows the session.

Phone session menu (2026-10-20, `patches/0121` and `0122`, web): on a phone, long-pressing
a session or opening its menu showed every Status value and every Move item, about 20 rows,
and the menu ran off the screen (the owner's screenshot that day). Status and Move are now one
row each, Status showing the current value ("Working" with its icon); tapping either opens
its items in place under it, one at a time, so the menu is 11 rows closed and 16 with Status
open. The owner picked this over drill-in pages and a bottom sheet from mockups. Desktop keeps
its side flyouts.

PR link previews (2026-10-20, `patches/0118`, web): resting the pointer on any GitHub
pull request link in chat opens a small card with the state (Open, Draft, Merged,
Closed), repo and number, title, author, lines added and removed, files changed and,
while it is open, its checks. The owner wanted a glance at each pull request a session
reports as done before opening it. The card reads the merge card's `/v1/ae/prs` data and
shares its cache; it fetches only once opened. Touch screens have no hover, so a tap
still just opens the link.

Effort shown when none was picked (2026-10-20, `patches/0119`, runner and web): the owner
could not tell a Claude session's effort before changing it, since the composer's Effort
menu showed no level and no check when they had never picked one. Claude Code then takes
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
5.5 (the owner's screenshot). The check now sits before the model's name.

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
turn the web composer shows a guess at the owner's next message as grey ghost text, the way
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

Disconnected subagents after a restart (2026-10-30, `patches/0254`, server): a subagent
killed mid-tool-call kept reading Running for another hour after every runner restart or
deploy. The restarted runner re-sends the dead subagent's old items and reports it
running; the server stores nothing new but restarted the full 60-minute settle from that
moment. The settle now counts from the subagent's newest stored item, so a restart that
brings only old items settles it at once. A settled subagent now reads grey
"Disconnected" under Finished, not "Done", and a real resume clears that. A subagent
waiting on a background task is timed out the same way, but one blocked on a question or
an approval from you is never settled. The hour is the default of
`OMNIGENT_AE_CLAUDE_SUBAGENT_STALE_AFTER_S`. One limit: a live subagent inside a single
tool call longer than that reads Disconnected until its next record arrives.

A follow-up (`patches/0255`, server) closes a race in that settle: if the subagent
resumed while the Disconnected mark was being written, the mark could overwrite the
resume. The settle now checks again after each step and backs off, clearing its mark.
The startup sweep also covers subagents a restart left waiting. The environment value
must be a finite number of seconds; `inf` and `nan` are ignored.

Merged PR fold (2026-10-20, `patches/0129`, web): a finished session ended on three
full Merged cards under its last message, about 550 px that repeated the TLDR's links
and needed nothing from the owner. Merged and closed pull requests under one assistant
message now fold into a single row ("3 merged #167 #161 #169"); opening it lists one
titled line per pull request, and opening a line shows that pull request's full card
with Review details and the AE compliance review. Open and draft pull requests keep
the full card, with Merge, the recommended method and the Squash or Merge commit
choice, above the row. A card merged from a card on this page stays a card until the
page reloads, so its after-merge note and "Mark session done" do not jump into the fold;
one merged anywhere else folds within 30 seconds, and a closed one that is reopened
comes back as a card.

Prompt suggestions in every session (2026-10-20, `patches/0128`, harness only; the owner's
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

Prompt suggestions keep an offered phrase short (2026-11-21, `patches/0436`): when a reply
ended with an offer such as `Say "draft it" and I'll set it back to draft`, the ghost text
sometimes repeated that whole sentence instead of the phrase the owner would type. The side
call's prompt now asks for the phrase alone, and the cleaner keeps only the quoted phrase
when a guess starts with say, type, reply or answer and the rest of the line speaks as the
assistant ("I"), so a command such as `Type "npm test" into the terminal` stays whole.

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

Child chats in the rail's Agents card (2026-10-29, `patches/0256`, web): the sessions a chat
starts with `create_session` ran outside every card in the rail, so a lead had to scan the
sidebar to learn which helper was working, done or stuck. The Agents card now opens with a
Child chats part when the open chat has such children (sessions whose `ae.orch` label names
it; forks are left out). A child waiting on you is pinned first with its question and two
buttons: Answer opens the action dock's tray at that child's prompt, which is the one real
card for it, and falls back to opening the child's chat when the prompt is not mirrored
here; Open chat goes to the child. The other children are rows with a role badge, what
the child is doing (its newest step), its last reply or error, and a time; closed ones fold
under one "N closed" toggle. The header counts them ("1 needs you · 1 running"), subagents
sit under their own heading, and the card no longer reads "No agents have run" while child
chats exist. The total line is now "This chat and its helpers" and adds the child chats'
spend: they are standalone sessions, so the lead's own usage does not include them (checked
live: every family child reads no parent session). Archived children are read on their own (one read of up to 600 newest
archived sessions shared by every chat, kept a minute, read again when a child leaves the open
list) and fold under "N closed" with the reference ones; a live row always beats the archived
read. The fold says when the read hit its cap or failed. Answer scrolls
the tray to that child's own prompt. Known limit: no pull request chip on a row, because a
row's pull request is not in the list data.

Merged PR row links and carets (2026-10-20, `patches/0131`, web): in the folded row of
merged pull requests, the numbers ("#173 #172 #175") were plain text, and the opened
list's lines gave no sign that they open. Each number is now a link to its pull request,
opening in a new tab, and shows the same preview card as a pull request link in the chat
when the pointer rests on it or it takes keyboard focus; clicking a number no longer
opens the fold. Each opened line starts with a caret that points right while its card is
closed and down while it is open.

Prompt suggestion alternatives (2026-10-20, `patches/0132`, harness, server and web): the
same single Haiku call now returns up to three different guesses at the owner's next message, best
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
Claude accounts could rename an extra Claude login but never remove it, so the owner's personal
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
the owner in a chat was spread over the feed (a question card, a ready merge card far up, a merge
that has not reached the box yet). One compact line now sits above the composer's workspace
bar and gathers it: the most urgent item with its button (a question's "Jump to it", a ready
pull request's Merge, a pending deploy's Admin Center link and dismiss), then a tinted badge
per other kind and a caret that opens a list of every item (design E1 collapsed, D3b opened,
D4 deploy tracking, approved by the owner). Merge keeps the card's confirm step inline in the dock.
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
the owner had to open the sidebar to see which session they were in. The app's own top bar only
switches servers. The header now shows the session's title there, cut short with an
ellipsis when long; the folder icon and sub-agent segments stay hidden, and a sub-agent
still shows only its Back link.

Run pastes multi-line blocks on tmux 3.6 (2026-10-21, `patches/0143`, server): the owner's Run on an
eight-line bash block was refused with "This shell has not turned on bracketed paste". bash
had turned it on before the browser attached, and the attach seed reads that mode from a tmux
format that only exists from tmux 3.7; the box runs 3.6a. The seed now infers it for a bash,
zsh or fish prompt when tmux cannot say, so the browser terminal knows and Run pastes the
block as one.

Session menu widths and one New Chat menu (2026-10-21, `patches/0140`, web): the owner's
screenshot of the chat's session menu showed empty space between the model names and their
shortcut digits, and an Account submenu stretched by its one-line scope hint. The session
menu (304 px), the harness row menu above it (280 px) and the Account (334 px), Effort and
Previous models submenus now each hug their widest row with a shared 160 px floor; the hint
wraps under the account rows. The owner then asked for every associated menu to match, so New
Chat's Claude Code and Codex picker draws the same one-column menu as a chat (it had
account chips in a row, effort listed inline and checks on the right). Measured in the real
app, both places: 214 px session menu, 187 px Account, 160 px Effort and Previous models.

Action dock as one strip (2026-10-21, `patches/0141`, web): with anything waiting the dock
was its own row above the workspace bar, so the composer carried two strips (69 px). The owner
asked whether one would be better, saw the mockups and said build. The dock now lives inside
the workspace bar (37 px, 28 px on a phone): folder and PR chips, a hairline, the most urgent
item with its button, the badges, the bar's own right cluster, then the caret. While anything
waits the folder chip goes to its icon and the PR chip to its icon and count. Badges drop to
icon and count before the lead's text truncates. A live deploy is a green chip. The list opens
as a tray above the bar, and a Merge pressed in the strip opens its confirm in that list.
Nothing waiting leaves the bar as it was. After the dual review the tray always starts closed
and closes on Escape, a confirm gets up to half the screen, and a confirm whose item goes
away closes on its own.

Usage gate in the action dock (2026-10-21, `patches/0142`, web): the owner wanted a prompt before
a session's account runs into its 5-hour or weekly limit, so they can switch model, account or
chat instead of paying overage. At 90% of either window the dock shows "AE Default is at 92%
of its weekly limit, resets Thu 09:00" with Switch model (the composer's model menu), Switch
account (the fork dialog, history included, where the Claude account row picks the login;
hidden for Codex or with no other login) and New chat. Sending stays open; a dismissal lasts
until that window resets, in every chat on that account.

Switch account offers the right login on a chat that is not running (2026-11-09, `patches/0330`
to `0332`, web and server): the owner, near a weekly limit on a phone, tapped Switch account and
"Parent and children" two or three times on a family of chats and nothing moved. Root cause: a
switch on a chat with no running terminal (Move all chats had moved the family a few minutes
earlier while it was idle) only stores the login for the next start, and the web read that stored
pick as the chat's login while the server reads the login the terminal runs on. So the button
offered the login the chat already ran on, and each tap flipped the stored pick back and forth
without restarting anything. The web now reads the running login first, as the server does; the
Account menu sends a switch unless the login is both running and stored, so picking the running
login puts it back; and the switch route answers `"queued": true` when no terminal is running,
which the toast turns into "This chat starts on X at its next message" instead of "now runs on X".
Review fixes (`0331` and the server): "Parent and children" and Move all now also reset a chat that
runs on the login but has another one stored (before, it was skipped and its next message started
on the stored one), and list a chat with no terminal running as `queued` rather than `moved`, so
the toasts say "1 child chat starts on X at its next message" and "2 chats not running start on X
at their next message". A switch that restarts the terminal stamps the running login on the server
too, since the runner's own stamp is best effort; picking the ticked login while another is stored
restarts the terminal instead of only storing it; and a repeated switch with no terminal running
and no stamp yet still answers `queued`.

Switching the default Claude login moves every chat (2026-11-13, `patches/0374`, web and server):
the owner switched the default to "shared:AE Backup" and chats kept billing the old login (extra
usage on). Root cause, three layers: the pick only asked "Move all open chats?" and "Only new
chats" left every chat as it was; move-all listed non-archived chats only, so reopening an archived
chat relaunched it on its stored old login; and a chat mid-reply was skipped with its old labels
restored and nothing ever retried it. The owner chose "Move all, no question". The pick now calls
move-all straight after the select write and shows one toast ("Moved N chats to X; M move when their
reply ends"). Move-all also covers archived chats (their login is stored, no runner is asked, and
they count as queued), and a busy chat keeps the new login stored for its next launch while a
bounded background retry (every 30 s for up to 30 min, dropped if the chat is switched elsewhere
meanwhile) restarts its terminal once the reply ends. The dialog, its store and its tests are
removed. A scheduled task's own account pin and "This chat" switches made afterwards are unchanged.

Review fixes for the login move (2026-11-13, `patches/0375`, web and server). Move-all and the
family walk listed 500 chats at most (newest first), so an account with 1,100 chats left the older
ones on the old login and said nothing; they now page to the end, and a hard cap of 20,000 is
reported as `truncated` in the answer and the toast. A chat with no running terminal (closed or
archived) is no longer put through the fork check, which needs a connected host, a live credential
probe and a loadable agent: it keeps the owner and host-owner checks, probes each host once, needs
no probe for a `shared:` login, and a host login on an offline host is skipped with that reason.
Every request for a chat now takes a claim number and an older one does nothing under the chat's
lock, and a busy chat's new login is kept inside that lock, so a late busy refusal can no longer
overwrite a newer pick. The busy retry keeps going through an unanswered runner and transient
errors until its 30-minute deadline, and stops on a relaunch, a newer request, a lost write
permission or a refusal it cannot cure. The runs of a scheduled task pinned to a login are skipped
("pinned by its scheduled task"), found through the task's run history, so no new label is needed.
A switch with no terminal running stamps the live login label too, so closed and archived chats
read the login they will start on. A `shared:` login moves chats on every host; a host login keeps
the host filter. The toast words "still switching" apart from "move when their reply ends" and
counts many skipped chats by reason.

Usage follows a switch to a shared login at once (2026-11-16, server only): a chat switched in
place to a shared Claude login kept showing the login it had left, in the dock's usage item and
the Usage card, for up to one relay tick (5 minutes). Root cause: `limits._resolve_account` let
the runner's live label win only for a login in the host's list, and the host lists a shared
account only while it is the pick for new chats, so the answer fell back to the relay's older
entry for the session. A live label naming a `shared:` account now wins as it is; only the bound
runner or the switch route writes it. Details in `docs/PLAN-LIMITS.md` ("A chat switched to a
shared login"). Test: `test_a_live_shared_login_the_host_does_not_list_beats_a_stale_relay_entry`.

Action dock keeps merges in view (2026-10-21, `patches/0144`, web): the dock hid an item
while any sliver of its own card was on screen, so a chat with three pull requests to merge
showed one at a time and its lead changed as the owner scrolled. The owner reads the dock as what is
pending now, not what is off screen. Merges, deploys and the usage item now stay whatever
the scroll position; only a question still hides while its card is in view, since that card
floats just above the dock (the owner picked this from mockups). A ready pull request whose card
is in view shows Merge in both places, and Confirm in either is disabled while the other is
merging.

PR preview in the action dock (2026-10-21, `patches/0145`, web): hovering a pull request link
in the chat shows a small preview (state, title, author, size, checks), but the dock's
`#202` only showed a tooltip and opened GitHub. The owner asked for the same preview there, before
the click. The dock's number now uses the chat's hover card, and its tooltip goes, since it
would show over the preview.

Merged PR row on one line (2026-10-21, `patches/0146`, web): on a phone the merged pull
request row's numbers (`#205 #203 #201`) wrapped to a second line beside "3 merged" and the
deploy status, and the owner said it looked odd. They now stay on one line and scroll sideways
when they do not fit; the count and the opened fold still list every one.

Dock preview opens upward (2026-10-21, `patches/0147`, web): the action dock's pull request
preview (`0145`) opened downward, over the composer the owner types in. It now opens above the
bar, the same way the dock's list opens; chat links keep opening as before.

Fewer gh auth status calls (2026-10-21, `patches/0148`, runner): every read of a session's
GitHub panel ran `gh auth status`, and the merge watcher reads every recent session each
two minutes, so the box ran about four a minute. The answer is now cached for 60 seconds
in one file the host's processes share, with only signed-in answers kept.

Draft pull requests in the action dock (2026-10-21, `patches/0149`, web): the dock listed
only pull requests ready to merge, so a draft never showed (the owner's report, with #221 as
the example). A draft now gets its own line, "#n draft · x/y checks", after the merges,
with a Mark ready button; once ready and green it becomes the Merge line.

The thread's PR card is a receipt (2026-10-21, `patches/0150`, web): the card under a chat
message carried its own Merge button while the action dock showed the same pull request's
Merge, so two Merge buttons sat on one screen. The card is now one line (state, number,
title and a status word such as "checks 7/12", "ready", "merged 03:35" or "live") that
opens in place onto the checks, Update branch and the review folds, with no Merge; the dock
owns every live action. The GitHub panel's card keeps its Merge.

Receipt opens with the merge (2026-11-09, `patches/0333`, web): the receipt that opens by itself after a merge did so one render late, so for a moment it read "merged" while still folded, and the web test for that case failed now and then on CI. It now opens in the same render. No visible change.

Open Cost Management on the Azure card (2026-10-22, `patches/0151`, web): the Azure spend
card links to the Azure portal's cost analysis page. The host admits that one URL for
extensions and nothing else on portal.azure.com; the link reveals no subscription.

Every open pull request in the action dock (2026-10-22, `patches/0152`, web): the dock
listed only drafts and pull requests ready to merge, so PR 228 sat outside it while one of
its twelve checks ran and the owner, having said "merge when ready", had nothing to watch
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

Slot limit from Settings (2026-10-22, `packages/ae_omni_policies`): the owner runs four slots
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

Session rail when a session opens (2026-10-22, `patches/0155`, web): the owner wanted the rail,
not the files panel, when a session opens. "Open the session rail in every session" still
let a restored workspace panel come back whenever it fit beside the rail, and the rail then
made way for it. Settings > Chat now asks "When a session opens, show": Session rail (the
new default; a panel left open starts closed on a desktop), Both when they fit (the old
switch on) or Workspace panel (the old switch off). A file, agents or comment link still
opens the panel, and a device that had the switch off keeps the workspace panel. Details
in `docs/PLAN-LIMITS.md` ("When a session opens").

Approval code under Show details (2026-10-22, `patches/0156`, web): on approval cards
that gate the person's own message (the rabbit-hole check, the slot cap) the JSON under
the policy's sentence now sits behind a Show details toggle, closed by default; the owner had
asked for it on 2026-10-17. Cards that gate what the agent is about to do (a tool call's
command, a response) keep their code in view, whoever wrote the policy.

Insertion line between lists (2026-10-22, `patches/0157`, web): dragging a session onto a
row of another project folder, of Sessions, or of another Triage group now shows the
blue line, and the drop files it there (or sets that group's status), then places it at
the line in the manual order. The owner asked for it after moving a session out of a project.

Feature docs per public release (2026-10-22, `scripts/docs-coverage.py`): the README's
showcase and `docs/features` should keep up with the patch series, but the owner wanted that
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
The owner keeps the OmniGent window narrowed to one side of the screen and asked for the
chat to stay visible beside the session rail. The rail floated over the chat below a
fixed 1280 px window width (1600 px with the workspace panel open), whether or not the
sidebar was open, so a 1210 px window with the sidebar closed lost the right third of
the chat and the header's buttons. The rail now docks whenever the window, less the
open sidebar, the open workspace panel (and its 8 px gap), an open files or logs panel
and the rail's own 304 px, leaves the chat 480 px (the floor upstream's workspace
panel already keeps), and floats only below that; with the sidebar closed the chat
shifts left beside it. The owner also asked to shrink the sidebar instead of only opening
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

Idle accounts always have a reading (2026-11-16, the owner): a Claude account other
than the default login was read only when a chat on it replied, so the desk strip
showed "n/a" for an idle one (a day-old reading on 2026-11-16). Chats on such an
account run on its setup token, which cannot read the usage endpoint. The owner now
signs the account in once with a full `claude auth login` through
`deploy/ae-usage-login.sh`, into a slot under `~/.omnigent/ae/usage-logins` that only
the relay opens; the relay reads it through the same no-model `get_usage` request every
12 minutes and posts it as `account_probes`, which becomes that account's reading. The
script proves the read works and that a chat on the account still replies, and removes
the slot when either fails. This reverses the owner's 2026-10-21 call that nothing reads
an account without a session, for this timer read only. No core patch. Deploy the server
before the relay. Details in `docs/PLAN-LIMITS.md` ("Idle accounts").

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
(web; `0086` and `0087` carry the review's fixes). The owner saw a split view pane picker showing `e264fbf40762483e8162b9da917dea7d`:
the pickers knew only the first page of unarchived sessions, so any other session
showed its id. A pane outside that page now fetches its own row, and the pickers read
the session's name, "Loading..." or "Untitled session", with "(archived)" where it
applies. They also had to reopen the session rail in session after session, because a
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

Every row's waiting state (2026-10-18): `patches/0088` and `0089` (web and server). The owner
saw the hourglass and the count of agents they were waiting on only on the session they had
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
again." with a Try again button until the owner refreshed by hand. The first read raced
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
and web). The owner asked to edit, delete and change the policies on Settings > Global
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

Rail slide (2026-10-18): `patches/0096` (web). With one pane at a time live, the owner's
screen recording caught both panes on screen at once during a swap. The workspace panel
slides out over 300 ms (width, fade and a 12 px slide, `.workspace-panel-motion` in
upstream's `index.css`), while the rail appeared at full width at once, so in their
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
from labels but found their text only in the messages the chat had loaded, so the owner saw
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
image. The owner asked to manage them there and chose to let the page own them. The block
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
image's entrypoint now turns on. The owner asked to group sessions by status with clear
separation, or to order them by hand. The sidebar's filter menu gains a Triage grouping
(Blocked, Working, Review, No status, Parked, Reference, each with its glyph and count,
then a collapsed Archived group that loads when opened) and a Manual ordering. Manual
applies inside every list (Sessions, Shared, each project folder, each group): drag a
row onto another row of the same list, or use the row menu's Move items, which also
work on a phone. New sessions land on top. The order follows the owner across devices
through `packages/ae_omni_policies/src/ae_omni_policies/sidebar_prefs.py`, kept in
`/data/ae-sidebar.json`; the grouping choice stays per device, as upstream keeps it.
Settings > Chat has Reset manual order. `0103` keeps the bulk-select bar inside a narrow
sidebar: where the words do not fit, it shows the bare count beside the four icons.

Shortcut chips (2026-10-19, `patches/0104`, web): the New session row's hover chip read
"CtrlAlt N" on Windows and Linux because each key sat in a fixed 16 px square. Keys now
size to their label, and the chip hides when the sidebar is too narrow to show it beside
the label.

Drag in any Ordering (2026-10-20): `patches/0105` (web). The owner first picked the project
sort in the Projects header's ⋯ menu, since the session ordering sits behind the filter
icon, and session drag did nothing. Now dragging a session onto another in its list, or
the row menu's Move items, works from any Ordering. The first move switches that device
to Manual, keeps the list as shown (or the order synced from another device), moves only
that row, and shows a toast saying where to switch back. Older sessions that load later
stay below the arranged ones instead of jumping to the top.

Status ordering by icon (2026-10-20): `patches/0109` (web). The owner picked Ordering: Status
and found a session they had checked as Review sitting between two Working ones, because
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

Dashboard (2026-09-24): the seventh core patch, `patches/0007` (P7, web). The owner found "Your move", "Parking Lot" and
"Slots" three nav entries for one feature, the difference between parked sessions
and the Parking Lot unclear, and Reference a long scroll away. After a design
review (`docs/design/dashboard-review.md`), the extension's two pages are one
Dashboard, reached from one sidebar row, "Dashboard n/3" (P1's Slots row, renamed by
P7; the extension contributes no nav row of its own). The page: a sticky bar of section chips (Working n/3 is the slot count) and the plan limits,
then Your move (gates, approvals, due lot items, pull requests, the brief), Working
n/3, Blocked, the Parking Lot (a parked session shows on its note's card), Review,
Needs a note (parked with no note, or an unfiled note), Unsorted (no status) and
Reference. Every section collapses and stays as the owner left it; Reference and Unsorted
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

Dashboard layout pass (2026-10-14), no core patch. The owner asked twice that day for the
Dashboard to read better on the desktop and the phone. The first pass
(https://github.com/analyticendeavors/omnigent-ae/pull/75) folds empty sections into one
muted "Nothing in:" line, drops their section chips and gives every session card one row
shape. The second pass rebuilds the layout from a render of a busy day, where the page
was 2836 px tall at 1400 px and 4091 px on a 390 px phone, the right column ended at a
quarter of the page, eight lot tasks took 1044 px on the phone, and about 550 px
repeated what Your move already showed. The owner chose four changes: the Parking Lot heads
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
the dashboard-pass screenshot set (removed before publication). On 2026-10-15 the owner found two layout bugs on their 1080
px window: the seventh section chip wrapped to a second row, and a merged card's Dismiss
dropped to a line of its own under Mark session done. The chips now stay one row on
every width, scrolling sideways with a fade when they outgrow it (a mouse wheel over
them scrolls them too), and Dismiss is an icon button in the merged card's head.

Fast open (2026-09-26), no core patch. The owner waited 3 to 5 seconds on every open of
the Dashboard on their iPhone, looking at an empty page. Measured on the box that day
with their login (read-only GETs, two each): `/v1/ae/prs` 2321 ms cold and 12 ms warm
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
the owner asked for each to say what it does. They now read Do it now, Write a note, Save
for later and Drop it. Only the labels changed: each still posts A, B, C or "close
it" as the session's next user turn through `POST /v1/ae/gate/{id}/reply`, and its
tooltip names the letter so it can be matched to the gate's RECOMMEND line. The
recommended button is not marked, because the observer captures the FORK clause
(`ae.fork`) and not RECOMMEND. On a phone the gate's buttons wrap two to a row.

PR cards (2026-09-24): the twelfth core patch, `patches/0012` (P12, web). The owner merged
every pull request by hand on github.com from their phone. Each pull request a session
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
review after the first merge, while three were still open). On 2026-09-26 the owner sent
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
The owner hates copy and paste and works from an iPhone (Safari) and a Windows laptop
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
(P28, web). The owner sent a screenshot of the chat card's confirm step ("Squash and merge
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
leaves the run unfiled with a warning and never fails the fire. The owner chose a server
default (2026-10-13): a create that names no project gets the owner's project named by
`OMNIGENT_SCHEDULED_TASK_DEFAULT_PROJECT` in `config/server.env` (`Automation Notes`),
stored by id so a rename keeps it. The automation dialog has a Project picker. The
seeder (`ae_omni_policies/schedules.py`) fills a missing project on the two shipped
tasks and never replaces one the owner chose.

Auto-resume after a host restart (2026-10-13): the thirty-fourth core patch,
`patches/0034` (P34, server), and `ae_omni_policies/auto_resume.py`. A host restart
(a deploy, an update, a crash) kills every runner it spawned and comes back with new
runner ids, so a session that was mid-turn sat at "The connection to the host dropped
unexpectedly" until the owner clicked Resume, and Resume only relaunched the runner without
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
agent to check before repeating), the one risk the owner accepted with it.

Deploys and background shells (2026-11-23, patch `0443`): on 2026-11-23 a forced deploy restarted the host while a planner chat waited on a 20-minute render it had started as a background shell, and the planner never came back. Its turn had ended, so it read idle: the deploy's busy count (`live_status` in Postgres) left it out, and auto-resume, which revives only chats that were mid-turn, skipped it. Two changes. `deploy/auto-update.sh` now also counts runner-launched Claude processes whose status file (`~/.claude/sessions/<pid>.json`) reads `shell` (turn over, background shell alive), but only on a pass that restarts the host, since a server-only deploy leaves those shells running. Patch `0443` hands the Stop hook's background shell count to turn-edge observers, `background_shells.py` keeps it as the `ae.bg_shells` label, and auto-resume posts one note (`BACKGROUND_RESUME_TEXT`) into an idle chat that carries it when its runner died. Review fixes the same day: a note that does not go out (the server still warming after the host connects) is logged at WARNING and gives the runner's try back; label writes for one chat run one at a time, newest count last; the environ check reads the file directly instead of through a pipe that a large environment could break; a status file whose `pid` or `procStart` does not match the live process (a reused pid) does not count. Known limits: a forgotten long-running background shell holds a host-restarting deploy up to the two-hour cap; a lead waiting only on an in-process `ScheduleWakeup` still sends no signal and is not revived (follow-up).

Scheduled-run retention (2026-10-13): the twenty-ninth core patch, `patches/0029`
(P29, server), and `ae_omni_policies/retention.py`. Every firing of a scheduled task
starts a new session, and the steward and the shutdown note fire every weekday, so about
ten run sessions a week piled up in the sidebar with nothing to reuse or prune them.
The owner chose automatic retention: after a task fires, keep its newest `keep_runs` run
sessions (default 5, one working week) and delete older ones. `keep_runs` is a field on
`/v1/scheduled-tasks` (create, patch, get, list) and on the agent's
`sys_scheduled_task_create` / `_update` tools; null keeps every run, and the seeder never
sends it, so a value the owner sets survives a reseed. P29 is the mechanism: the setting, kept
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
the owner's second note that the cards waste space. Their screenshot of the compact card
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
The owner sent a screenshot of two chat cards, one with its confirm step open, and asked for
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
request. The owner chose the target on the button, purple for merged and the single Ready
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
no core patch. The owner reads the lot on their phone and asked for four
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
workspace and warehouse links). The owner wanted the aetraining Fabric
capacity's wake button, with room for more capacities, inside omnigent-ae next to the
Dashboard; Off came at their request, guarded by a separate suspend key so holders of
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
each short card, and the owner turned that down. Wide panes get three columns, medium ones
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

Context Try again wakes a sleeping runner (2026-11-10, patch `0345`): after a host restart a chat's runner sleeps, and the rail's Context section showed "Context request timed out or the runner is offline" with a Try again that only re-asked the server, which never woke the runner for this route, so every click gave the same answer. `GET /v1/sessions/{id}/context-usage` takes `wake=true`; with no runner client it restores the session through the same helper the model picker uses (`_wake_runner_for_model_change`, now with an `action` word for its offline message, default unchanged), then reads. The web sends `?fresh=true&wake=true` only for the user's own refresh (Try again, the refresh button, the plan popover); first loads, TTL reloads, turn-end reloads and quiet retries never wake. Only a native terminal session on a host is woken, within 45 s (past that the answer is `pending` and the quiet retry reads once the runner is up); a wake failure shows the helper's own message, and a host refusal (harness not configured, workspace missing) shows its reason and keeps Try again. A wake in flight is remembered per session: the quiet retry and a second Try again join it instead of starting another, and a request that woke reads with a timeout that keeps the whole request near 75 s; a wake still running after 120 s is given up as an offline answer, so a hung launch never pins the panel. Patch `0346` regenerates `openapi.json` for the new `wake` parameter. The plain offline reason is now "The session's runner is asleep or did not answer. Try again wakes it." Claude is still starting right after a wake, so the existing quiet retry covers the `pending` answers. Tests in `test_ae_context_usage_routes.py`, `aeContextUsage.test.tsx` and `AeRailContext.test.tsx`.

Merge watcher (2026-10-21): a session continues by itself when its pull request merges, as Claude Code Desktop does, so no session waits for the owner to say "merged". `packages/ae_omni_policies/src/ae_omni_policies/merge_watch.py` runs in the server from the app's lifespan (a route-less router in `DEBUG_ROUTERS`; FastAPI merges an included router's lifespan, so no upstream patch). Every 2 minutes it reads which pull requests the sessions touched in the last day (and any still waiting on a watched pull request) track (`prs.tracked_by_session`, the same `resources/github` list that draws the chat's merge cards); every 10 s (the owner's choice over a webhook) it sends one `GET /repos/{repo}/pulls/{n}` with `If-None-Match` per open one, so an unchanged pull request answers 304 and costs no rate limit. A rate-limit 403 or 429 pauses all polls until `retry-after` or `x-ratelimit-reset`; other failures back off that pull request alone. On open to merged, or to closed without merging, it posts one plain-English note into each session that tracks it ("Merge watcher: PR #196 (title) merged at 17:00Z as 11c89f51 by login. URL Continue: delete its branch and worktree, then check the deploy.") through `POST /v1/sessions/{id}/events` in process, as the session's owner (a five-minute token from the server's own provider), which wakes an idle session the way an external post does. The last seen state, ETag and undelivered notes are kept in `/data/ae-merge-watch.json`, written before a post and after each, so a restart neither repeats a note nor misses a merge that landed while the server was down. The slot cap, the rabbit hole and the fork classifier recognize the note by its opening words (`continuation.py`) and let it through without a slot claim, a turn count or a model call. `AE_MERGE_WATCH=0` turns it off. Tests: `packages/ae_omni_policies/tests/test_merge_watch.py`, plus a case each in `test_slot_cap.py`, `test_rabbit_hole.py` and `test_fork_suspect.py`.

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
The owner also wanted every Fabric capacity they have on the card, each with its SKU, state,
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

2026-10-23: the dashboard's pull request cards (Open PRs and Your move) gained the dock's two state actions, Mark ready for review on a draft and Update branch on a branch that is behind, each pinned to the head the card showed; each card also opens Review details (size, description, and the AE compliance review with its verdict and whether it covers the current head, read only when opened and kept across redraws; on Your move a Review details button reads the card without starting a merge); Reference lines gained a menu that moves a closed session back to Working or Review. No core patch; extension bundle only.

Secret expiry list (2026-10-23, the owner): the Admin Center's Server secrets chips became
one row per key with its Key Vault name, its expiry (amber inside 60 days, red once
past, "no expiry set" otherwise), when it last changed, and Edit and Rotate links. Edit
is the secret's Azure portal page; Rotate is the system that issues the value, with a
hint naming the account it opens (a Databricks login in the wrong tenant had cost an
hour that day). The server never reads Key Vault, so a daily user timer on the box
(`ae-omnigent-secrets-meta`) runs `python -m ae_omni_policies.secrets_relay`, which
lists the vault's secrets (attributes only, never `secret show`), keeps the names
`secrets_meta.VAULT_NAMES` maps to server keys (a test parses `deploy/pull-secrets.sh`
and fails on any difference) and posts name, enabled, expires and updated to the admin-only
`POST /v1/ae/secrets/meta`; the server keeps the last post in `/data/ae-secrets-meta.json`
(`AE_SECRETS_META_FILE`) and `GET /v1/ae/secrets` merges it. A required key that has
expired turns the card red; the tile says how many are expiring. The card says when no
reading exists or the newest is over 36 hours old. There is no Refresh button: the page
already reads again on focus, and `systemctl --user start ae-omnigent-secrets-meta.service`
refreshes the dates at once. Install steps are in `deploy/README.md` (Secret expiry dates).

Admin Center spend polish (2026-10-23, the owner): the Azure spend card said "Cost Management is
rate limiting." under each of its four subscriptions; it now says it once under the month
total. `GET /v1/ae/azure-cost` carries `rateLimit` (`retryAt`, the time the next try is due,
and `goodAt`, when a pass last read any subscription), kept in the state file across a
restart; the card names the next try while it is ahead and otherwise says the numbers are
from the last good reading and how old that is. A subscription's own error (no role, a
refused principal) still shows on its row. The Databricks credit card read "as of Oct 19" four
days later because the server only read spend on a RUNNING warehouse with an auto stop under
30 minutes, at most every 30 minutes; it now reads when the warehouse is already RUNNING and
auto stops in under an hour or never, at most once an hour, and never starts a stopped one.
The card and the tile note show the reading's age ("as of 2 h ago"), amber past 48 hours; the
manual Refresh button is unchanged. No core patch; the server and the extension bundle only.

Key Vault card (2026-10-23, the owner approved the concept that day): the Admin Center now
answers two questions in two places. Box health keeps "does this box have what it
needs": its secrets block is "Secrets on this box", collapsed by default to the
counts and a chip per key that needs you (missing, expired, inside 60 days, refused),
with Show keys remembered per user. The host's `CLAUDE_CODE_OAUTH_TOKEN` joins the
rows: the relay checks `/secure/env/omnigent-host.env` by key name only. A new Key
Vault card lists every secret in both vaults (103 and 10 that day) by name, with a
vault switch, four counts (expired, under 60 days, no expiry, untouched 180 days),
a short Needs you list, and a searchable, filterable list grouped by the first word
of each name. The Secrets tile split into "Secrets on box" and "Key Vault". The relay
(`secrets_relay.py`) now posts every secret of both vaults and the server keeps them
all (`/data/ae-secrets-meta.json`); the new `GET /v1/ae/vault` is admin only
(`vault_inventory.py`), and the post's cap rose from 500 to 1,000 names per vault.
Source expiry: a vault secret tagged `ae-source=entra:<appId>` carries its Entra
app's latest secret end date (`az ad app list`, end dates only), and the server
records GitHub's `github-authentication-token-expiration` header for
`AE_GITHUB_TOKEN`; a row's expiry is the earlier date, marked "from Entra" or "from
GitHub". `deploy/vault-source-tags.sh` proposes those tags and sets them with
`--apply`. Last worked: `key_health.py` keeps, in memory, the newest outcome of the
server's own calls per feature (Databricks and Azure cost tokens, GitHub API, task
store, Fabric Logic Apps, a working OIDC sign-in, success only), as a status code or
an error class only, and each box row says "worked 2:07 PM" or "failed: 401 at 1:50 PM". No core
patch; extension bundle and AE package only.

Secret expiry alerts and owners (2026-10-23, the owner approved the plan that day): a
secret's expiry now reaches the owner's phone. After the box's daily reading lands
(`POST /v1/ae/secrets/meta`, 06:15), the server files a task-store task for each
secret whose effective expiry (the card's own, vault or Entra or GitHub) is 30 days
or fewer away, moves the same task's alarm at 7 days and once more when it expires,
and closes it when the expiry moves out (the secret was rotated). The alarm is 09:00
on the home clock (`AE_TASK_STORE_TZ`), which is what the task app pushes; tasks land
in `AE_SECRET_ALERT_LIST` ("AE Work" until a DevOps board is chosen) with the `ae`
tag. One task per secret per expiry date, kept in `/data/ae-secret-alerts.json`, and
found again by its "Alert key" line if that file is lost (`secret_alerts.py`).
Owners: each secret can be given an OmniGent member as owner from the Key Vault card,
one row or a whole group at a time (`POST /v1/ae/vault/owner`, people from
`GET /v1/ae/vault/people`, both admin only; the map lives in
`/data/ae-secret-owners.json`, never in Key Vault). Rows and Needs you lines show the
owner's initials, filter chips count each owner and "No owner", Needs you says when an
alarm is set, and the tile says when the next alarm rings. Vault rows gained a Rotate
link (the Entra app's Credentials page for a tagged app secret). Also fixed: the
meta POST's save-failure path named `ErrorCode.INTERNAL`, which does not exist. No
core patch; extension bundle and AE package only.

Key Vault card fix (2026-10-23, the owner): the card read "The Key Vault inventory could not be read (Server response exceeds the limit)" after PR 304. That PR added a rotate link and hint to every row, which took the answer to 91 KB for 116 secrets; the extension bridge refuses more than 64 KiB. Rows now omit `edit_url` (the page builds the portal link from the vault and secret name) and send `rotate_url` and `rotate_hint` only when the issuer has its own page, and a test holds the answer for 116 secrets under 56 KiB. No core patch; extension bundle and AE package only.

Key Vault card, rows in pages (2026-10-24, the owner): PR 306 brought the answer to 51,000 characters, but the card still read "Server response exceeds the limit". The bridge counts two bytes per UTF-16 unit against 64 KiB, so its real cap is 32,768 characters, and PR 306 tested against the wrong number. The card now reads `GET /v1/ae/vault?rows=false` (counts, needs-you list, groups, alerts) and then each vault's rows from the new `GET /v1/ae/vault/rows?vault=<name>&start=<n>`, 25 rows a page, admin only; a test holds the summary and every page under 32,768 characters for 130 worst-case rows. No core patch; extension bundle and AE package only.

Slot card shorter (2026-10-29, the owner): the approval card listed every working session, helpers included, under a count of slots, so "6 of 4 taken" sat above 27 rows and pushed Accept off a phone screen. The card now lists only the sessions that hold a slot (a family is its lead), cuts titles at 48 characters, opens with "Slots full: 6 of 4 taken. Run this as the 7th?", and names the two longest-running sessions as the ones to park. Block mode and the `POST /v1/ae/sessions` 409 keep the "would make 7 of 4" wording and get the same shorter list. No core patch; AE package only. Real buttons and a folded list on the card are a separate patch.

Slot card names the count it would make (2026-10-24, the owner): the owner approved a session at the slot limit and read the card's "Slots full: 4 of 4 working" as if that session were the 4th, so the cap seemed to ask at the limit instead of past it. It had counted right: four others were working and the new one made five. The reason text now reads "Slots full: 4 of 4 taken; this one would make 5 of 4.", on the approval card, the block-mode refusal and the `POST /v1/ae/sessions` 409 alike. No core patch; AE package only.

Git repos card (2026-10-24, the owner): after a backlog cleanup across three GitHub accounts took dozens of `gh` queries, the owner asked for one place that shows every repo. Of two mockups (a card in the Admin Center, a standalone page) they picked the card, read only first, with a read-only token per personal account. `GET /v1/ae/git` (admin only, cached 5 minutes) reads the Dependency Sweep's repo list, then per repo one GraphQL query for open pull requests, branches with no open or merged pull request and open Dependabot alerts. Each alert is checked against its lockfile on the default branch, because GitHub's alerts lag the fix by hours and on 2026-10-22 its dependency graph dropped a push outright: that day 38 open alerts were already fixed on main. Those land under "Fixed on main, waiting on GitHub"; only alerts still vulnerable, or whose lockfile cannot be read, count as needing a fix. Before it shipped, the owner asked for mockups of the card itself and picked "Needs you first" out of three (sections, needs you first, by repo): on the day it was built, every one of the 38 open alerts was GitHub lag and three of the four pull requests were drafts or still in CI, so a card that gave each section equal room mostly showed nothing to do. It follows the Key Vault card's shape (numbers, Needs you, Show all). No core patch; extension bundle and AE package only.

Cost targets on the Azure card (2026-10-25, the owner): the Azure card shows spend, but only to someone who opens it, and a Fabric capacity left running bills $0.72 an hour while nobody looks. The owner asked to set limits and get a task with a phone alarm when one is crossed. The card now has a Targets panel (add, edit, switch on or off, delete with a confirm in the page) and the server (`cost_targets.py`) judges each target against the reading it already holds: month to date, latest finished day and month-end forecast (the card's pace figure, needing three days of data) for all of Azure, one subscription, one service (month to date only) or one resource (latest finished day only); and hours Active for a Fabric capacity. Two limits shaped it. Cost Management is rate limited and shared, so no query was added: a rate-limited, stale or missing reading makes a target `cannot-evaluate` and files nothing, and a subscription billed in another currency is skipped for USD limits. The reading keeps services by month and resources for nine days, which is why a service has no day figure and a resource no month figure. Fabric's reading has no start time for most capacities, so the hourly check records when it first saw each one Active and counts from there (a recent `resumedAt` wins when it exists). Alerts file one task per target and period (month, data day, or Active episode) in the list named by `AE_COST_ALERT_LIST` (default AE Work), with the 09:00 home-clock alarm the expiry alerts use, or an alarm three minutes out for Fabric; they close when the value falls back, and a spend task from a finished month is left open for a person to tick. A lost state file is rebuilt from each task's "Alert key" line. Defaults seed once, only while the targets file does not exist: month to date over $600 for MVP SC and MVP 2025, over $50 for AnalyticEndeavors and AnalyticEndeavorsTools, any resource over $25 a day, any capacity Active over 6 hours; all can be edited or deleted and stay deleted. The extension host sends only GET, POST and PATCH, so the card saves and deletes with POST (`/azure-cost/targets/{id}` and `/{id}/delete`) while `PUT` and `DELETE` work too. The answer is capped under the bridge's 32,768 characters and a test holds it. No core patch; extension bundle and AE package only.

Cost targets retry an unread Fabric pass (2026-11-23, the owner): after a deploy the server's first Fabric pass can time out on the Logic App (8 s), and the cost target loop then held that failed reading for its full hour, so the Fabric active-hours target read "cannot be checked" and Needs you showed "Could not read Cost targets" for up to an hour while the Fabric card already read fine. When the last pass left a capacity or the fleet list unread, or the pass raised, the loop now looks again after `FABRIC_RETRY_S` (5 minutes); a clean pass goes back to the hourly wait. A failed pass files no alert, so a retry adds only a Logic App call. No patch.

Child prompts in the lead's dock (2026-10-27, the owner): a lead that started sessions could not see their questions or tool permission prompts without opening each chat, and its pop-up answers to a child's question went nowhere. Patch `0210` mirrors a family child's prompts into its lead's stream and snapshot and lists them in the lead's action dock with their real buttons, resolving in the child; two settings in General > Sessions switch the dock lines and the relay. Out of tree, the `ae` MCP server gains `answer_child_question(child_session_id, elicitation_id, answers?, cancel?)` (`packages/ae_omni_policies/src/ae_omni_policies/mcp.py`), which posts to the child's resolve route with the content shape the web card sends (`askUserQuestion.ts`) and refuses unless the relay setting is on, the child's `ae.orch` names the calling session itself (never a caller-supplied id), the prompt is still pending and it is a plain AskUserQuestion (never a tool permission, plan approval, Codex command or MCP prompt); a second answer reports "Already answered". The orchestrator agent allowlists it and its prompt says to relay a child's question with `ask_user` and then answer with it. No existing prompt rule told the owner to open a child chat, so none was removed. Decided: dock concept B over a card in the feed, and relaying on by default.

MCP server questions reach the web (2026-10-27, the owner): a Power BI modeling session asked "Confirm the operation" through Claude Code's `Elicitation` hook, which the wrapper never registered, so the question existed only in the terminal. Patch `0211` registers the hook, parks the question as a normal pending card (the server's schema as a form) and answers Claude with the choice; it also shows in the lead's dock via `0210`. `answer_child_question` refuses it, so only the owner answers an MCP prompt. Every failure leaves the terminal dialog to Claude. URL-mode elicitations stay in the terminal for now.

Narrow split panes (2026-10-27, the owner): in a four-pane split each pane is an iframe about 340 to 360 px wide, and the composer's Send or Interrupt button sat up to 39 px past its border because the action row needed 318 px of 266. Patch `0212` squeezes the row's gaps first, then hides the mic and host badge below about 372 px (never while a take records or the host is not online), and re-measures when the host or mic state changes. See `patches/README.md`.

New chat composer on a phone (2026-11-18, patch 0391): on a 390 px iPhone the new chat composer's Start button poked past the card's right edge. Root cause: the landing composer never applied the narrow-row tiers the in-session composer uses (`aeComposerTight`), so its mic, roles preset and voice start button with menu could not shrink. The mic and roles preset now hide in tiers there too.

Choice buttons and a child following its lead (2026-10-27, the owner): an MCP server's "Confirm the operation" Yes / No showed a dropdown with Submit and Reject; the owner wanted one button for either answer. Patch `0213` renders a required field whose schema lists 2 to 4 string choices in `enum` (oneOf and const forms keep the dropdown) as one button per value ("Continue" and "Decline" here, the full text on hover), submitting at once, with a Reject button only when no choice declines (a Yes/No keeps just its two buttons); it applies in the chat, the Inbox and the dock. Out of tree, `rabbit_hole` (`packages/ae_omni_policies/src/ae_omni_policies/rabbit_hole.py`) lets a family child follow its lead: past a threshold, a child whose valid lead (`valid_lead_id`: `ae.orch` names an existing same-owner session, no fork label, no parent; both owners are read strictly, so a failed read asks) has accepted the check is allowed and records its own ack, where before each child held every message, the lead's `post_message` included, until the owner accepted in the child. A lead that declined or was never asked, a foreign or missing lead, a fork and any store read failure still ask. The lead's ack is read from its conversation row's persisted `session_state` (the engine writes it on accept). See `patches/README.md`.

Reload notice (2026-10-27, the owner): after a deploy an open tab kept running the page it loaded, so a live fix looked unfixed until a manual reload, which happened twice today. Patch `0214` compares the tab's own entry script name with the one the server serves now (every 2 minutes, and on focus or return to the tab) and shows one toast, "OmniGent was updated." with Reload and Later; it never reloads by itself; it does nothing in an embedded or desktop host, and a failed lazy chunk stays with the app's own chunk-error boundary. See `patches/README.md`.

Reload toast spelling (2026-10-27, the owner): the toast from patch `0214` read "OmniGent was updated." Patch `0215` changes it to "Omnigent was updated." (title, doc comment and the test's expected text); nothing else changes.

Browser pane pop-ups and screenshots (2026-10-27, the owner): an agent driving StreamYard in the pane could not see the open "Create live stream" dialog in `browser_snapshot`, and `browser_screenshot` timed out while snapshot and click worked. Patch `0216` lets the snapshot walk on through a zero-size portal root (plus shadow roots and same-origin iframes), and screenshots a view the pane is not showing by staging it one pixel inside the window for the capture, with a 10 s cap and a clear error. Patch `0217` fixes the review findings: a chat closed during a capture, a retry budget that counted only sleeps, hidden frames, and a cap on huge pages.

Browser tools on the 0.21 desktop app (2026-11-21, patch `0437`): with the 0.21.0 desktop app, `browser_snapshot` failed with "Unsupported legacy browser action; update the server UI" and `browser_screenshot` returned an empty image. Snapshot, click and type now go through the desktop's typed bridge, so they work on 0.21 and later; older desktops keep the previous path. On 0.21 the snapshot is the desktop's own walk, which can miss a dialog in a portal. A screenshot of a chat whose Browser pane is not on screen now returns an error saying to open the pane, instead of a blank image.

Browser tools on the 0.21 desktop app, review fixes (2026-11-21, patch `0438`): click and type send their arguments the way the older path read them (a numeric ref wins, otherwise the selector; type with no text types an empty string), and a snapshot taken by the desktop's own walk tells the agent that it skips shadow DOM, iframes and dialogs in hidden portal roots.

Child prompt wake (2026-10-28, the owner): answering a family child's prompt in the lead's action dock resumed the child but left an idle lead asleep until the owner typed "continue" (child 75db0142 and lead a30d8192 at 01:55Z). Patch `0218` adds an elicitation-observers registry upstream and `child_wake.py` out of tree: when the lead (same owner, not archived or failed) is idle, one `Child prompt answered: <title> (<id4>...): the owner picked <label>.` note is posted into it as its owner, past the slot cap, the rabbit hole and the fork classifier. A busy lead is waited on for up to 30 seconds and read again before each post; a fork, a native subagent, another owner's lead, a repeat or invented verdict, or an unknown action post nothing. See `patches/README.md`.

Workspace check under Docker (2026-10-28, the owner): the merge watcher's "workspace no longer exists" check (PR 340) read every host path as missing because the server runs in a container that mounts only `/data` and ships an empty `/home`, so four notes into live sessions (/home/<user> and a worktree under it) were dropped, and the child wake note with them. `merge_watch._workspace_gone` is now off in a container (`/.dockerenv` or `/run/.containerenv`; the 4xx `workspace_missing` rule guards there) and for a path that is not absolute POSIX; elsewhere a not-found stat means gone and any other stat error means unknown. No patch.

Dock limit buttons overlapping on a phone (2026-10-28, the owner): on a phone the limit item's text is hidden, so the dock's width check never saw the row overflow and Model, Account and New chat painted over the merge and waiting badges. The strip now clips to its own width and drops New chat on a phone; the opened list keeps it (patch 0219).

Dock list sideways scrollbar (2026-10-28, the owner): opening the action dock drew a small horizontal scrollbar under the limit buttons because the list used the full labels (Copy to another login) and its row ran wider than the tray. The list uses the strip's short labels now and hides its horizontal overflow (patch 0220).

Limit hover card (2026-10-28, the owner): the dock's limit warning showed one window and nothing about the other models. Hovering its icon, or tapping it on a phone, now opens a card with the login's 5-hour, weekly all-models and per-model weekly usage, bars and reset times, the window behind the warning tagged (patch 0221).

Dashboard Flow / Brief switch dead on a phone (2026-10-28, the owner): in the native iPhone app the shell's header floats lower than the page's fixed top padding, so the header sat over the top of every extension page and took the tap on the switch. The page now starts below the header on a phone (patch 0224).

Question buttons running off the phone screen (2026-10-28, the owner): a pending question's choice buttons kept each long answer on one line, so the card and the whole chat scrolled sideways. The buttons now wrap inside the card (patch 0225).

Model menu submenus off the phone screen (2026-10-28, the owner): the composer model menu's Account, Effort and Previous models flyouts opened past the screen edge on a phone and could not be read. They now stay inside the screen (patch 0226).

Fable 5.2 as the main Fable model (2026-11-10): Claude Code 2.1.322's catalog still reports Fable as `claude-fable-5-1[1m]` while `claude-fable-5-2` and `claude-fable-5-2[1m]` both answer on the subscription, so the model menu kept Fable 5.1 as its main row. The fork's known-model list now holds `claude-fable-5-2`, and when a known id is newer than the newest subscription row of its family, that row (and its `[1m]` sibling) is rewritten to the newer id, keeping the `[1m]` suffix, so "Fable 5.2 (1M context)" is the main row and Fable 5.1 lands under Previous models (patch `0339`; Opus, Sonnet and Haiku rows are untouched, and a catalog already on the newest version is unchanged). The effort table gains a Fable 5.2 row, assumed High because 2.1.322's catalog does not list it yet. Outside the fork, the seed presets' `FABLE` is `claude-fable-5-2`, and the model nudge (`newer_models.find`) counts the known ids and their `[1m]` variants as available next to the host catalog, so a preset row on Fable 5.1 is offered Fable 5.2 even while the catalog says 5.1.

Preset reviewer's insights read, compact (2026-11-10): the scheduled reviewer has no Read or Bash tool, so when `preset_insights_read`'s answer spilled to a file (321,737 chars, 11,128 lines, from the near-raw per-run `runs`, `model_changes` and `preset_switches` lists) it had nothing to open it with and the review could not run. `preset_review.build_read` now answers one `stats` row per owner, preset (runs, median cost/wall time/tokens, raises, step-downs, hangs by role, review findings/fixed/reported) instead, grouped by owner as well as preset id so two users' own copies of the same preset are never averaged into one row; `due`, `why`, `newer_models` and `new_models` are filled the same way as before. A second gap: `agents/preset-reviewer/agent.yaml` set no `env:` on its `ae` MCP server, so upstream's stdio transport started the tool subprocess with the SDK's minimal default environment, never `OMNIGENT_RUNNER_PRIMARY_SESSION_ID` (the runner process itself has it; it just never reached the subprocess), and every scheduled call to `preset_insights_read` or `submit_preset_suggestions` failed with "Could not find this session's own id". A harmless `env:` entry now flips that branch to the full runner environment. The reviewer's prompt step 2 describes the `stats` row instead of a job-type grouping (similar files touched, PR or no PR) that the run log never actually recorded (`files_touched` is hardcoded null; there is no PR field). The whole answer now actively stays under `MAX_RESPONSE_CHARS` (40,000): with no run-row bloat left, the one part that still scales with user and preset counts is the admin `presets.users` block, so `build_read` drops it from the tail (owner's own entry last) until the answer fits, flagging `truncated`; `MAX_RUNS` (the per-read run-log sample cap) moved from 400 to 20,000 since the aggregate no longer grows with run count, so sampling bias across presets is now rare, and the prompt tells the reviewer to discount a near-bar gap when `truncated` is true. `test_preset_review.py` covers the owner split, the admin-scale cap and the size regression.

New-model watch (2026-11-10, patch `0340`): Fable 5.2 was found by hand. Now a host systemd user timer (`ae-omnigent-model-watch`, every 6 hours, `model_watch_relay.py`) scans the installed Claude Code binary for `claude-<family>-<major>[-<minor>]` ids (Opus, Sonnet, Haiku, Fable and Mythos), keeps the newest one per family that is newer than the known ids, and asks it one word through `claude -p` with no tools, MCP servers, settings or saved session (at most three ids a pass; a miss is asked again after 24 hours). An id is served only when the CLI exits 0, reports no error, replies ok and names that id in `modelUsage`. The pass posts to `POST /v1/ae/model-watch` (admin only, validated strictly); `GET /v1/ae/model-watch` answers every user's picker. For an id that starts answering, the server fetches the release notes (`https://platform.claude.com/docs/en/models/overview.md`, override `AE_MODEL_WATCH_NOTES_URLS`), runs the preset review now (`preset_review.run_now`, shared with `POST /v1/ae/preset-review/run`) with the notes' excerpt in the read's `new_models`, and files one phone reminder once the suggestions are stored (or at once, naming the reason, when the review cannot start; a review still running after 2 hours is reminded as not finished). The reviewer's step 5b assesses the model against every preset, reasons only from the excerpt and opens its summary with a Retention sentence. The newer-model nudge counts served ids as known, `mythos` is a Claude family, the fork's model pickers read the served ids (`useAeModelWatch`, `aeKnownModels`), and the Spend card's Presets tab gets a New models fold with helper text, the review state, a link to the Suggestions fold, the release-notes link and a warning when the box has not reported in 13 hours. Presets are never changed. `test_known_models_sync.py` fails when the web and Python static lists differ. Review fixes (patch `0341`): only the admin who started a model review, and the one reviewer session that read it, can complete it or see its notes; a newly served id is stored `pending` in the same write and every relay POST restarts pending reviews older than 10 minutes, expires stuck ones and retries unsent reminders (about 5 attempts, `notify` on the model); a weekly review already running keeps the model pending with no alarm; the relay treats an `is_error`, rate limit, login or network failure as inconclusive (never un-serving an id), matches the reply `ok` as the whole reply, saves its binary stamp after every scan and stops asking an id after 10 refusals until the binary changes; `last_report` (host, Claude Code version) is for admins only; a watched id whose family the catalog lacks gets its own row, and an open app rereads the list hourly.

Unlisted Claude ids read as versions (2026-11-10, patch `0343`): a chat switched to `claude-fable-5-2` while the open app predated it showed the raw id ("claude-fable-5-2 (current)") on the composer chip and the model menu's disabled row, because `formatStatusModelLabel` returned the id when no catalog row matched. It now returns the version label (`aeClaudeVersionLabel`: "Fable 5.2", "Opus 5.5", with " (1M context)" for a `[1m]` id) for a full `claude-` id through one shared helper, `formatUnlistedModelLabel`, which `useSessionModelLabel`'s host-option fallback also uses when the host row has no display name. A listed row still wins; Codex ids, aliases ("sonnet_5") and namespaced catalog ids (`system.ai.claude-...`) stay raw. No import cycle (`aeClaudeModels.ts` only imports types). Tests in `composerModelLabel.test.ts`; two raw-id expectations updated. Review fixes (patch `0344`): only a canonical id (optionally `[1m]`) gets the label, so a dated or otherwise suffixed id (`claude-haiku-4-5-20251001`, `[beta]`) keeps its raw text, and an empty host display name no longer shows blank.

A newer model gets its own picker row, and catalog-listed releases alert (2026-11-10, patch `0351`): when a known or watched Claude id was newer than the catalog's row for its family and that row was an alias (`opus`, `fable`), the row was rewritten to read "Opus 6" but kept the alias id, so the new-chat picker started whatever the alias still resolved to. The alias row is now left alone (labeled with its own version) and one new row for the newer id goes in front of it, `[1m]` only when the alias row had it; full-id rows are rewritten in place as before. Separately, the model watch counted the host's cached model catalog as known, so a release Claude Code already listed was never probed, reported or reviewed, and no phone alert fired. Known ids are now the static list plus what the server already holds, and `catalog_ids`, `catalog_dir` and the relay's `catalogs` argument are gone. Review fixes (patch `0352`): the inserted row keeps the alias row's effort options but is never the default row, only a subscription row with the same id blocks the insert, and the relay's saved binary stamp carries a scan rules version so the first pass after this deploy rescans an unchanged Claude Code once.

Follow-up reminder after a review that did not run (2026-11-11): the model watch filed one phone reminder per model, ever (`_queue_reminder` kept a sent slot and `notify` stopped at a stored task id), so when Fable 5.2's review expired ("did not finish") and was run again, the finished review's "5 preset suggestions are waiting" reminder never filed. The not-run reminder also asked for a run from Settings, Roles, but that run never covered the model, since only `started` reviews are read and bound. Now `POST /v1/ae/preset-review/run`, once started, restarts the caller's `not_run` model reviews (`model_watch.rerun_not_run`), and each reminder slot records its `kind`: a `done` reminder after a sent `not_run` one is filed once more as a follow-up task titled "(review done)" under its own key, `Model review key: <id>`, so the first task, still open, neither blocks it nor is found in its place. The earlier task id moves to `prior_task_id`. A slot stored before kinds existed is read from its text. A model never gets more than two reminders. Review fixes: the route flips the reviews before it fires the task (the reviewer can read at once) and puts them back when the run does not start (`model_watch.undo_rerun`, which leaves any review a reader already bound); a review that finishes while its not-run reminder is still being filed turns the queued reminder into the follow-up instead of losing it; and the restart dedupe matches the key as a whole line, so an open task for `claude-opus-6-1` no longer stands in for `claude-opus-6`.

Box health monitor (2026-10-28, the owner): on 2026-10-28 the box's `/tmp` (a 62 GB RAM tmpfs capped at 1,048,576 inodes) ran out of inodes because nothing cleaned Claude Code's per-session temp folders, and every Claude Code Bash call failed with ENOSPC for about 15 minutes. A user timer on the box (`ae-omnigent-box-health.timer`, every five minutes) now posts heat, CPU, memory, storage and bloat to `POST /v1/ae/box-health`; the Admin Center's Box health card shows five lines, up to three Needs you lines and a Details dialog, and there is a Box health tile. It covers both box users and the second box user's container with counts, sizes and ages only, never a folder, project, session, branch, worktree or file name. `GET /v1/ae/box-health` is for OmniGent admins and logins named in `AE_BOX_ADMINS`; a box admin who is not an OmniGent admin sees only that card, and `GET /v1/ae/access` says what a caller may see. No `[ae-patch]`: the extension bridge already allowed GET and POST under `/v1/ae/`.

Box health monitor, review round (2026-10-28): the relay no longer posts a mount path, a failed unit's name or a folder path that could name a client or a repository: only `/`, `/tmp`, `/boot`, `/boot/efi`, `/home`, `/var`, `/secure`, `/run` and `/dev/shm` are named (others are "other mount N"), only `ae-*`, systemd and Docker units are named (the rest are counted), and `AE_BOX_HEALTH_DIRS` takes `label=path` and posts the label. Every posted string is plain ASCII and at most 48 characters. Folder listings are read lazily under the entry cap, the fast reading is posted before the slow scans (which now stop at a 150 second deadline), Docker's volume sizes are read once a day, a container walk that timed out counts nothing as stale, and the container user cannot be root. The page asks who the caller is twice before it believes "not an admin", a poll no longer rebuilds the Details button or an open hint, and hints open above a button near the bottom of the screen.

Admin routes for a second user (2026-10-28, the owner): the owner and the second box user may both be admins for the box, but neither may see anything sensitive about the other's work, only what needs cleaning or monitoring. The second box user's chats run on their own host container, so every non-shared login in the limits store, the whole task store and the box's infra pages are the owner's, and several `/v1/ae` reads answered any signed-in user. The second box user has not signed in yet, so nothing had leaked; this closes it first. Admin only now (403 `admin_only`): `GET /host`, `/secrets`, `/fabric`, `/databricks` (their `can_act` is true on every 200) and every `/lot` route (`/lot`, `/lot/tags`, `/lot/park`, `/lot/{id}/link`, `/complete`, `/resume`, `/session`), which was readable and writable by anyone. Scoped to the caller: `GET /limits/accounts` lists only the Claude `shared:` rows; `GET /limits` makes Codex `null` and blanks the Claude entry unless it is a `shared:` account or a session the relay posted that the caller may open, and cuts `contexts` and `sessions` to that one session (`limits.member_view`); `GET /capacity` and `GET /sessions` keep the box's counts, limit and clock but list only the sessions the caller may open; `GET /prs` scans only those sessions and keeps one scan per caller (it was one shared map, so the first caller's pull requests answered everyone's); `GET /prs/{owner}/{repo}/{number}` and `.../compliance` answer a non-admin only for a pull request a session they may open tracks (403 `forbidden` otherwise); `GET /your-move` gives a non-admin no due lot items and no pull requests, which come from the server's own task-store and GitHub credentials; `GET /release` empties `env_missing` for a non-admin. Left open: `/health`, `/schedule-defaults`, `/claude-accounts/shared`, `/prs/{owner}/{repo}/{number}/deploy`. A single-user runtime's one user passes the admin check, so nothing changes there. The Dashboard shows a non-admin "The Parking Lot is for admins." with no park form, no Needs a note list and Park as a plain status move. The slot check in the status menu counts the caller's own working rows for a non-admin; the slot cap policy still enforces the box's limit. No core patch.

Admin routes for a second user, review fixes (2026-10-28, the owner): two reviewers of the change above found more. `POST /sessions` looked up its idempotency key among every user's sessions, so a guessed key returned another person's session; and its `slots_full` and `family_full` 409s named every working session. The lookup now sees only the caller's sessions, and the 409 lists only theirs, with `others_working` and "Plus N of another user's." for the rest. `GET /capacity` and `GET /sessions` keep the rows scoped for admins too, because neither person sees the other's work, admins included; they add `others_working`, a count of other users' working sessions, so `slots_used` (still the box's, and still counting the second box user's against the owner's limit) adds up. A context-only row in the relay's post no longer counts as proof that a reading is a session's own. An admin check that cannot be answered (a slow or failed `/v1/me`) is no longer taken as "not an admin": confirmed answers are kept 30 seconds and reused for up to 10 minutes while the check is down, otherwise admin-only routes answer a retriable 503 `admin_unknown` and `/your-move` is answered redacted and not cached (its cache is keyed by user and admin). The PR routes (card, compliance, and now `/deploy`) accept `?session_id=` for the chat asking, checked against current access and tracking; without one they use the caller's scan, check access to the tracking session each time, re-read at most once per 30 seconds, and serve a scan at most 10 minutes old. `/health` leaves out the task store's URL for anyone who is not a known admin, and `POST /gate/{id}/reply` needs a sign-in and answers a session the caller cannot open exactly like one that does not exist. The Admin Center's Box host, secrets, Fabric and Databricks sections show one quiet line to a non-admin and stop polling.

Admin check and admin sections, second review fixes (2026-10-28, the owner): only a 200 or a 403 from `/v1/me` settles whether someone is an admin; a 401, 408, 429, any other code or a dropped connection is "could not tell" (last confirmed answer if under 10 minutes old, else a 503 `admin_unknown`) and is never remembered as a no. The Admin Center's Box host, secrets, Fabric and Databricks sections that showed "for admins" look again every 5 minutes, and come back on a 200 instead of staying hidden until a reload. A forced pull-request re-scan uses up its 30 seconds only when it finishes, and also runs when every session the old scan listed is closed to the caller now. Known and left: an admin keeps admin routes for up to 10 minutes while `/v1/me` is down, and nothing coalesces or caps concurrent scans at two users.

Rebase on the box health monitor, last touches (2026-10-28, the owner): `GET /access` and the box-health route now ask the cached admin check, so an unanswered `/v1/me` never turns `admin` into null; a box admin who is not an OmniGent admin still gets 200 on `/box-health` and `/access` and 403 on `/host`, `/secrets`, `/fabric`, `/databricks` and `/lot` (pinned by a test), and the box-only Admin Center page starts none of those sections. A 401 from `/v1/me` drops that user's cached admin answer. The secrets section has its own 5-minute look-again timer after `admin_only`. Known and left: two forced pull request re-scans can overlap.

Leak scan before anything leaves the box (2026-10-28, the owner): nothing internal (IPs, hosts, e-mail, home paths, cloud ids, private repository names, secrets, client or person names) may reach the public fork or release, now that the admin portal and the box-health work read box internals and a second person shares the box. The only check was a fixed-string denylist plus nine token shapes, run by hand when a release is prepared, and patch commits reach the public fork at PR time, days before it. `scripts/leak-scan.py` (stdlib only) adds generic classes (tailnet and RFC1918 addresses, `*.ts.net`, e-mail, home paths, Azure and Databricks endpoints, cloud ids, private repository links, a wider secret list, image metadata, OCR when `tesseract` exists) and applies the private denylist when a bundle is supplied. It runs as the new `leaks` CI job (the lines a PR adds, its paths, commit messages, title and body; the whole tree on a push to `main`), as a `pre-push` hook that `just hooks-install` sets for the repository and for `upstream/` (whose pushes go to the public fork), and by hand with `just leak-scan`. It never prints a matched value, fails closed (exit 2) when it cannot run, and takes reviewed false-positive exceptions from `scripts/leak-allowlist.txt` or an inline marker with a reason; neither can clear a denylist, secret or private-key hit. Triage of `main` on the first run found a real app id in three test files and a home path in this file (both replaced) and a handful of real values in generated patches, deploy units and product code that stay allowlisted under `DECISION` lines until they are replaced. Decided: this PR ships warn-only when the denylist secret is missing; a later PR makes that a failure and adds OCR in CI. `docs/LEAK-SCAN.md`.

Leak scan, first review round (2026-10-28, the owner): two independent reviews of the scanner (https://github.com/analyticendeavors/omnigent-ae/pull/390) each reproduced real leaks passing clean, so this round closes them. The scan now reads every commit on its own (a value added and removed in the next commit still reached the public fork), computes a push range against the destination remote only and exits 2 whenever it cannot compute one, shows no matched value through a file, branch or tag name, and catches addresses followed by a slash or preceded by `@`, JSON and `SECRET_KEY` style secrets, `Bearer`, GitLab and Entra client-secret shapes, commit authors that are not noreply addresses, `.local`, `.lan` and `.internal` names, more home-path and repository-link forms, every non-placeholder GUID, binary files, UTF-16 text and more image formats and PDFs. The denylist configuration fails closed (a secret without its hash variable, or a hash mismatch, is exit 2), CI runs the base commit's scanner so a PR cannot print the denylist, and a catch-all allowlist entry is refused: real values are keyed on their sha256 instead. `just fork-push`, an hourly `fork-backstop.yml` and `leak-pr-text.yml` cover pushes that skip the hook and edits to a PR's title or body. The Azure cost app's Entra id left the source tree: set `AE_AZURE_COST_APP_ID` in config/server.env (the card's link) and config/host.env (the relay); unset, that secret's link opens its Key Vault page. `docs/LEAK-SCAN.md`.

Leak scan, second review round (2026-10-28, the owner): the fix check found that a PR could allowlist its own leak, so a PR scan now reads the allowlist from its base commit; an entry the PR adds or changes fails as a finding of its own unless the PR carries the label `leak-allowlist-approved`, applied by a GitHub user id listed in the repository variable `AE_LEAK_ALLOWLIST_APPROVERS` (unset: nobody approves; set it after the merge), and a push that changes the allowlist removes the label. The PR scan moved from `ci.yml` to `leak-pr.yml` (job `leaks`, which re-runs on labels, edits and pushes); `ci.yml` keeps the push-to-main scan. Allowlist entries for e-mail, address, host, cloud id, home path and Azure endpoint classes must be `sha256:` hashes (a wildcard cleared a whole class), the fork backstop keeps no state while the denylist is missing, the secret-assignment pattern is bounded (100 KB took 23 s), a CIDR or version look-alike no longer hides a host address, `git@` is allowed in a commit identity only for GitHub, GitLab and Bitbucket, `fork-push` refuses `--tags`, `--all` and other options that push unscanned refs, letters-only Bearer tokens and passwords with punctuation are caught, and the pre-push hook refuses a bundle with no pinned hash. The Databricks account id and the five relay app ids move to box settings in PR C (decided 2026-10-29). `docs/LEAK-SCAN.md`.

Leak scan on main stopped every release (2026-10-29, the owner): after https://github.com/analyticendeavors/omnigent-ae/pull/390 merged, the push-to-main scan flagged the merge commit's author, the merger's own GitHub address, so main CI failed on every merge and `release.yml` cut no tag for anyone's change. Main's history never goes public: the public mirror is rebuilt under a fixed identity, and pushes to the fork check identity themselves, so that scan now runs `diff --no-identity` and still checks every pushed commit's message and lines. The fork backstop's first run reads every fork branch and ran past the scanner's 120 s limit, so it sets `AE_LEAK_SCAN_SECONDS=900`. `docs/LEAK-SCAN.md`.

Long approval answers (2026-10-29, the owner): an approval pop-up showed each long answer in full on its button, a block of text on desktop, and the plain option picker still let a long answer run past the card on a phone. A button now shows the words before the first colon (else a cut at a word) with the full text in its tooltip, and the option picker wraps inside the card (patches 0234 and 0235).

Box health cleanup buttons (2026-10-29, the owner): a box admin can now clean up from the Box health card. Details has a Cleanup section and a Needs you line gets a Clean up button; both open a confirm step that says whose items, how many and how big. Four cleanups: a user's Claude temp folders older than two days, old test leftovers at the top of `/tmp` (fixed name patterns), review checkouts with no uncommitted changes, and Docker's dangling images and old build cache. The server queues one request (`POST /v1/ae/box-health/cleanup`, cancel at `/cancel`, one at a time, 409 `box_health_busy`, `box_health_stale` or `box_health_changed`); the box's relay (`box_cleanup.py`) finds it in the answer to its next post, runs it as its own user and posts a result (`/cleanup/result`). Each mapped container also ages its own two-day-old temp folders every hour. The box checks that it is not root, that each folder is a real folder it owns, that the whole tree is old, that no process works in it, that the temp guard's lock on a live session is not held, and that the inode did not change. Nothing in a post, a log line, an API answer or the card names a folder, project, session, repository, branch or file. `AE_BOX_HEALTH_CLEANUP=off` hides the buttons and stops the aging. Also: the "cleanup may not be running" line appears only once a stale folder is a day past the line (the daily tmpfiles run explains up to a day), and `AE_BOX_USER_LABELS` lets the server refuse a reading with an unexpected user label. No `[ae-patch]`.

Box health cleanup buttons, review round 1 (2026-10-29): the relay's `--dry-run` no longer removes anything in containers (it reports what it would age as counts). A leftover or review checkout is old only when the newest mtime anywhere in its tree is old, and a tree holding a `.git` anywhere, an entry of another user or another filesystem, or anything unreadable is kept; the proof is repeated under the lock just before removal. A review checkout with a stash or a commit no remote has is kept. Container removals (the button and the hourly aging) now run fixed freshness, working-folder and command-line checks inside the container right before each `rm` batch, and remove nothing when a check cannot finish. The run is bound to the count the admin confirmed (a larger fresh plan refuses with "press again"), an untaken request expires after 15 minutes, a result is accepted only for a request the box took, and one cleanup stops at 100 seconds so a pass stays inside the unit's timeout. Every confirm says the box runs it within 5 minutes; a container offer calls its items "temp folders (Claude sessions and test runs)" and counts both; a Needs-you line gets a button only for an offer of its own owner and kind; `AE_BOX_USER_LABELS` also checks the labels in failed parts; the dialog keeps focus when it redraws.

Box health cleanup buttons, review round 2 (2026-10-29): the container live-use check now requires a cwd line for every pid of the container user (read from each `/proc/<pid>/status`), so a process that hid its links no longer lets its folder go; a gap is looked at once more and then the batch is skipped. The cleanup's deadline now covers every container command, planning included, and the posts wait at most 10 seconds each. A container session or test run with a `.git` anywhere inside is kept, and the host re-checks live use (and, for review checkouts, the git checks) for each candidate right before it is removed.

Fork backstop scans each commit once (2026-10-29, the owner): the hourly Fork backstop on main ran its whole 15 minutes and exited 2. `cmd_fork` listed each branch's commits with only the previous run's tips excluded, so a commit carried by many branches was scanned again for every one; the public fork's branches share most of the roughly 230-commit patch series, so one run scanned tens of thousands of commits instead of a few hundred. It now keeps a map of commit to hits for the run, scans only commits not yet in it, and adds the cached hits to every branch whose range holds the commit, so a hit in a shared commit still names every branch to delete and is reported once. The deadline error also prints the configured limit (`AE_LEAK_SCAN_SECONDS`) instead of a fixed 120 s. `docs/LEAK-SCAN.md`.

Fork backstop leaves out upstream's history (2026-10-29, the owner): the public fork holds copies of upstream's own branches, so after the per-commit dedupe a cold run still had 5,959 unique commits (about 30 minutes at 0.30 s each). `fork --exclude-refs PREFIX` leaves out everything reachable from upstream's heads and tags, which `fork-backstop.yml` now fetches from `UPSTREAM_REPO`; 592 commits remain and a cold run finished in 232 s. In fork mode the e-mail half of commit-identity applies only to `[ae-patch]` commits, the denylist half of identity always runs, and `@outside-patches` denylist entries are skipped, so the 8 historical fork commits that name published values do not list most branches for deletion every hour. `docs/LEAK-SCAN.md`.

Fork backstop, first review round (2026-10-29, the owner): the denylist check on a commit's author and committer no longer depends on the `[ae-patch]` prefix; `--exclude-refs` prefixes are validated (trailing `/`, no overlap with the fork's refs, at least one match); the workflow refuses an empty or wrong `UPSTREAM_REPO` and a fetch that brings no upstream branches; one notice counts the commits whose e-mail was not checked. `docs/LEAK-SCAN.md`.

Box health cleanup follow-ups (2026-10-29): a zombie process of the container user, which has no cwd link and which the container never reaps, no longer stops all container cleanup for good: it is skipped by its state. A status listing with no `Uid` line is treated as a failed read (nothing is removed) instead of "no process of ours". The check's own helper processes are ignored only when their command line is exactly the pinned one, no longer by process name. Each post to the server is cut at 10 seconds in all (httpx's own timeout is per phase), so a pass is bounded as documented: 195 s for the whole pass plus three posts of at most 10 s each, under the unit's 4 minute timeout.

Box health cleanup follow-ups, review round (2026-10-29): a zombie that still has other threads running (its main thread exited) is now a gap, not skipped, because its live threads are not listed and its leader's links are unreadable; only a zombie with `Threads: 1` is skipped. The status listing is read per pid: every pid needs exactly one `Uid`, `State` and `Threads` record and every line must be one of those records or a command line, so a half-read listing, a duplicate or forged record, or a command line that held a newline fails the batch closed. A pinned helper command line now matches only with exactly one trailing NUL, so extra empty arguments no longer match. A cleanup result post that times out is dropped; the card shows the request as lost and the next reading shows the lower counts (accepted).

Box health cleanup follow-ups, last round (2026-10-29): the working-folder listing of the container live-use check is now NUL-framed (`-printf '%p\t%l\0'`) and read strictly. A folder name that holds a newline can no longer print a second line that reads as another process's cwd record and hides its gap; every record must be a well formed `/proc/<pid>/cwd` or `fd/<n>` link, unique, and the listing must end with a NUL, or the batch fails closed. A process now counts as the container user's by real or effective uid, so a setuid-style process also needs a cwd line.

Box health cleanup, capped at the confirmed count (2026-10-29): an admin confirmed 46 Claude temp folders; by run time the box found 47, because the card's count comes from the hourly reading and one more folder had crossed the two-day line. The relay took the request and removed nothing. Folders age past that line all the time, so the refusal would have repeated. A run now removes at most the confirmed count, the oldest first, for the host actions and the container path alike; `docker_prune` takes no count and is unchanged. Age is the newest mtime the scan proved anywhere in the item's tree (a plain file or socket uses its own mtime), ties go by path, and the order is applied before the 5,000-item limit and before the confirmed count, so every limit keeps the oldest; counts and times only, no name leaves the module. Items left over are counted under the new fixed reason `over_confirmed` and the result says "Aged after you confirmed, kept for the next run". The bound that stays: a run never removes more than the admin confirmed, and the server's press-time check (409 when the offer's count differs from the dialog) is unchanged. The `Changed` exception is gone; the server still words an older relay's `CHANGED` code, and its result model now drops a skip word it does not know instead of answering 422, so a relay newer than the server (they ship in one release, so at most one pass during a deploy) never loses a finished result. While that request was queued the other offers were locked with a reason shown only as a button tooltip, which a phone and a disabled button never show; the list and the confirm step now print the reason under the button. No patch.

Box health card redesign (2026-10-29, Option C, revised): the card keeps its data and every control keeps its helper text, and gets a new layout. The host's live status (dot, name, state pill) moves into the card header between the title and "Updated" (a second header line under 480 px of card and on a phone), filled by the host section's own draw so a poll updates it in place. A status line shows the worst state as a pill (`worstOf`: "N need you", "OK", "No reading", tone from the view), the read time and Details; the five readings are tiles; a "Needs you" panel holds each need with its Clean up button at the right and the queued request as its last row with a clock icon; the host block draws only when it has facts, notices or the button; secrets are one full-width block. Rule misses from PR 351 fixed on the way: every nested surface is `--inner` on `--inner-line`, buttons on it use the `--fg 7%` step, the queued stripe gets the accent, the leftover dividers use `--inner-line`, and `.secret-chip` and the `.fabric-state` pills mix their tint with `--inner` or `--card`, not transparent. Web extension only (`boxHealth.ts`, `host.ts`, `fabric.ts`, `icons.ts`, `style.css`), no patch. `docs/features/admin-center.md`.

Chat file links: Download (2026-10-28, the owner): a chat reply that names a box file as inline code or a markdown link opened it in the viewer, but there was no way to save it, and a chat that had gone to sleep answered a download with a 502. Both forms now open a small menu on right-click, the context-menu key or Shift+F10 with Open and Download, and on a touch screen a download icon follows the name. Download reuses the viewer's own download, so the file saves with its real name and every byte. The download route now wakes an asleep runner first, as attachment upload does. New: `AeChatFileActions.tsx`; about 10 lines in `ChatMarkdown.tsx`; the wake in `routes_resources.py`; the vitest suite joins the `web-test` list. Patch 0239; its review fixes are patch 0240. `docs/features/phone.md`.

Names out of fork-added files (2026-10-28): patch `0241` replaces real people's names and the box's host names in the files the patch series added with neutral words (comments say "the owner", tests use example addresses and demo paths). The model-change approval value, which used to equal the owner's first name, is now `owner`: `escalation.py`, `mcp.py` and `model_log.py` write it, and `model_log.normalize` reads a line or label that still carries the old value as `owner` (the old literal is built from parts), applied where the preset runs, preset stats and preset review read the log and where `escalation.parse_last` reads a session label. Web and server ship in one image, so a short skew window maps an unknown value to none. History is not rewritten; the tree is clean going forward.

Check-in relay, review fix (2026-10-28): patch `0242` makes the relay accept both prompt wordings, so scheduled check-ins stored before `0241` keep posting straight into their session, and `schedules.checkin_prompt` writes the neutral wording.

Leak scrub of the repository's own files (2026-10-29): the leak scanner builds the tenant-domain and Key Vault words from parts and no longer lists the personal-account handle (the private denylist blocks it); the systemd units carry an `__AE_HOME__` token that the install step in `deploy/README.md` renders and checks; the secrets relay's known app ids move to `AE_SECRET_SOURCE_APPS` and the Databricks account id to `AE_DATABRICKS_ACCOUNT_ID` (box settings, unset means no known-pair proposal and no account id in the hint); test fixtures use made-up values built from parts; the allowlist entries these fixes retire were deleted, and `docs/LEAK-SCAN.md` now says a PR scan uses the base allowlist. No patch.

Leak scan fails closed (2026-10-29): a missing private denylist secret now fails `ci.yml`, `leak-pr.yml` and `fork-backstop.yml` (`--denylist-missing fail`; the secret and its hash variable are set, in the Actions and Dependabot stores); the PR scan reads the text in changed images with tesseract OCR (base commit's scanner, `media --ocr require`, installed only when an image changed). No patch.

Cold relaunch keeps the Claude transcript (2026-10-29, the owner's token audit): every cold relaunch rewrote the session's transcript file from server items, so the next call re-wrote the whole history to the prompt cache at the write price (a 157,975 token write on a session whose system prompt still hit). The runner now keeps the file on disk when it covers the server history (every `toolu_*` call id and the last user text are in it), copies it from an old workspace dir when it moved, and rebuilds only when it does not cover; a file ahead of the server is forwarded from the forwarder's own cursor, so no row is skipped in the UI. The session carries `ae.claude_native.resume` (`kept`, `copied` or `rebuilt`) and a running `ae.claude_native.resume_count` (patch 0243). Live check after deploy: restart the runner of a session over 100k tokens; the top of its transcript no longer shows rows all stamped in one second, the first call's `cache_read` is about the size of the history, and the label reads `kept`.

Cold relaunch keep decided by identity, review round 1 (2026-10-29, the owner's token audit): the first version of the transcript keep compared tool call ids and the last user text, which both reviewers showed accepts a file that disagrees with the server. The forwarder now records how many items the server held and the id of the last one each time it moves its cursor, and a cold relaunch keeps the file only when the server still holds exactly that, the cursor's bytes still match, the file ends cleanly and the image repair would change nothing; otherwise it rebuilds as before. The old forwarder is cancelled before its final cursor is read. A session that started before this change rebuilds once on its next relaunch, which writes its first record. A launch that returns before it has a transcript leaves no record at all, so a fresh cursor can never inherit one (patch 0244).

Leak scan push range (2026-10-29): updating a branch that merged `main` was refused by the pre-push hook on `main`'s squash commits (their author check is skipped on `main`). An update now also leaves out the destination's default branch; other tracking refs still count as new (second review: a branch deleted on the remote keeps its tracking ref until a prune). No patch.

Fork backstop fails only on new leaks (2026-10-29): the hourly backstop failed every run on 200 soft hits the fork already carried, and each patch rebase mints new shas, so no key built on commits holds. `scripts/leak-fork-baseline.txt` lists those values by class and hash (no commit, path or date); `fork --baseline` counts them and fails only on a value that is not listed, and `fork --write-baseline` regenerates the file from a fresh scan that ignores the allowlist and markers and refuses while any hard hit remains. A baseline never accepts `denylist`, `secret` or `private-key`, and the scanner exits 2 on a file that holds a denylist entry or its hash. A change to the file needs the `leak-allowlist-approved` label (`leak-pr.yml`, three-dot diff; a push voids the approval), the workflow cache key holds the file's hash, and the file stays out of the public release. `docs/LEAK-SCAN.md`.

Leak scan reads GitHub's merge subject as a branch (2026-11-10): a pull request landed with "Create a merge commit" gets the subject `Merge pull request #<n> from analyticendeavors/<branch>`, and the `private-repo-link` class read the branch's first segment (`docs`) as a private repository, so main's push scan went red and `release.yml` made no tag. On the first line of a commit with two or more parents, that subject's owner link is now dropped when the branch is namespaced (`<segment>/<rest>`) and no other link on the line has the same value. Still hits: a bare name after the owner (it could be a repository), a private repository named inside the branch, and the same subject on a single-parent commit; the denylist and the other classes read the original text (review fix: an earlier draft rewrote the line and hid it from the denylist). `docs/LEAK-SCAN.md`. No patch.

Transcript keep fires (2026-10-29, fix for the keep of patches 0243 and 0244): live, every frontier record went stale right after it was written, because the server adds a resource event right after each cold relaunch and the keep treated it as a foreign item, so every relaunch rebuilt. Items the transcript rebuild leaves out (resource events, errors, reasoning, native tool, routing, slash command and terminal command rows) no longer stale the record or force a rebuild when they trail it; any other item, including an unknown type, still does. A kept transcript moves its record up to the server's end. After two cold relaunches of the same session the second shows `ae.claude_native.resume = kept` (patch 0245).

Lead handoff at 300k (2026-10-29, the owner's token audit; L1 of the token-saving plan): lead chats were 28% of the box's Claude spend because eight grew past 300k tokens of context and re-read it every turn. A 60 second sweep (`lead_handoff.py`) posts one note into an idle top-level Claude chat whose `omnigent.last_context_tokens` (read only when under 30 minutes old) passes the new `lead_handoff_tokens` setting (300000 by default, 0 off; roles state, top level beside `child_relay`; the "Hand off at" row in Settings, patch 0246). The lead writes a packet and calls the new `hand_off` tool, which posts to `POST /v1/ae/sessions/{id}/handoff`: create the new lead (same host, workspace, model without `[1m]`, effort, preset and Claude login, key `handoff:<old id>`), write `ae.handoff_to` on the old lead (the commit point), then re-point the children's `ae.orch`, rebuild the check-in prompt (`schedules.checkin_prompt`, only when it names the old id) or replace the id in other scheduled tasks, and set the old lead to `reference`. Each step after the commit is idempotent, a repeat call finishes what is left, the sweep repairs a lead left half done, and one lock per lead means the button and the tool at once start one new lead. The slot count is taken with the old family left out, so the handoff works at full slots. `post_message` and `schedule_checkin` follow `ae.handoff_to`. The note passes the slot cap, the rabbit hole and the fork classifier like the merge and child wake notes (`continuation.py`). The L2 button is not in this change. See `docs/features/action-dock.md` and `docs/LABELS.md`.

Lead handoff review round 1 (2026-10-29, two reviewers): the new lead's brief is confirmed before the commit point (`ae.handoff_brief=posted`, written after the post succeeds; a retry that finds the new lead by its key posts the packet first); a handoff is finished only when `ae.handoff_done=1` is written, which needs every step clean, and the sweep repairs every committed lead without it, scheduled tasks included; the target must exist, be live and have the same owner before anything is re-pointed, else the marks are cleared and a retry starts a fresh lead under the same key; `merge_watch.post_live` (merge notes, child wake) follows `ae.handoff_to`, five hops with a loop guard (the watcher's PR records are rebuilt from the live sessions each scan, so forwarding is the whole fix; nothing keyed by session id is moved); the sweep and `/handoff/request` take one lock per lead and re-read the labels inside it, with an in-process set of nudged leads as a backstop; only leads in `working` or `blocked` are nudged; other scheduled tasks get only `session_id <old id>` and `session <old id>` replaced and the tasks that name the old id elsewhere are listed in the answer; `hand_off` tells the old lead to stop; `children_of` ignores a `conv_` prefix.

Lead handoff review round 2 (2026-10-29): a handoff whose new lead died after `finish` had set the old lead to `reference` left the family stuck (a retry was refused as "already a reference", the sweep no longer saw a lead, children and tasks pointed at the dead lead). The commit now stores `ae.handoff_prev_status`, and the undo (`clear_commit`) moves the children back, rewrites the check-in and tasks back, restores the status and only then clears the marks; any problem keeps the marks so the undo repeats. The sweep takes a lead's lock with a 2 second wait and skips a busy lead until the next sweep.

Compact card at 300k (2026-10-29, L2 of the token-saving plan; replaces the hand-off note; the owner's call: always compact, never suggest a new chat): the 60 second sweep no longer posts anything into a chat or waits for it to be idle. Past the owner's `lead_handoff_tokens` it labels a top-level Claude chat `ae.handoff=suggest` with the size in `ae.handoff_at`, and the web shows a card in the action dock above the composer and a line in the rail's Context section: "This chat is at 370k tokens. Every reply re-reads all of it." with Compact (two clicks, the same `/compact` the rail button sends, which does not depend on the model, so it works on 1M models) and Not now. Compact also snoozes, Not now snoozes alone, and a snooze waits for 100,000 more tokens. The label is wiped when the context falls back under the size, the setting is off, or the chat leaves working and blocked; the earlier `nudged` value is rewritten to `suggest`. `POST /v1/ae/sessions/{id}/handoff/request` and the note text are gone; `POST .../handoff` and the `hand_off` tool stay for when the user asks for a new chat. The setting is now "Suggest compacting at", key unchanged (patch 0250). Review round 1 (same day): the web shows the card only for the chats the server may label (top-level, not archived, no `ae.orch` family parent, no fork or side chat, role absent or lead, the Claude native wrapper, working or blocked), so a stale `suggest` label on a child, reviewer or other harness offers nothing; a snooze now survives the chat leaving working or blocked and ends only when the context falls under the size or the setting is 0; the sweep reads the owner, context and decision inside the chat's lock and skips a chat whose owner cannot be resolved; the snooze route re-reads under the lock and answers 409 for a chat that handed off, is not eligible, or has no context reading; a 409 from the snooze (from Not now, or after Compact) is a quiet no-op while other errors still toast; the sweep logs once per chat at INFO when it skips one for want of an owner; the old `Lead handoff:` note text is no longer exempt from the slot cap, the rabbit hole and the fork classifier. Still not checked: whether a native compact refreshes `omnigent.last_context_tokens` before the next reply; the snooze on Compact covers it either way.

Lead handoff review round 3 (2026-10-29): `target_ok` now answers `ok`, `gone` or `unknown`. A store or owner lookup that fails, or an owner that reads as `None` on either side, is `unknown`: the sweep's repair logs at info and changes nothing, and the handoff route answers 503 with retry advice. Only a confirmed missing, archived or different-owner lead is `gone` and runs the undo. A permanent problem in the repair or the undo (a hand-edited check-in, no owner client) warns once per lead per process, then logs at debug.

Stand-in Archive family (2026-10-29, patch 0247): right-clicking a Grouping > Family stand-in row (lead archived or not loaded) now offers Archive family, which archives every non-archived session of that family, so a leftover family can be closed. It confirms first while a session is open.

Stand-in Archive family fix (2026-10-29, patch 0248): Archive family on a stand-in whose root session was missing from the loaded pages found nothing to archive; it now archives the sessions the sidebar shows under the stand-in as well, never the wider family of a re-rooted lead.

Pin a lead alone or with its family (2026-10-31, patch 0272): pinning a session that led a family either pinned only the lead or, with Settings > Chat > "Pin a lead's family" on, always took the whole family, and Pinned listed every pin flat, so a pinned family lost its tree. Pinning or unpinning a lead with a family now asks "Pin only the parent, or the parent and its whole family?" (Pin only the parent, Pin the whole family, Cancel; the unpin wording matches). The setting has three choices: Ask each time (the default; the old No reads as this), Always the whole family (the old Yes) and Only the parent. A lead with no family, or none to bring along, toggles at once with no question; if the family cannot be read, Ask each time falls back to the single pin or unpin. Pinned now draws a pinned family as a tree at its lead's pin slot, with its pinned open sessions indented under it on the family's line; a session pinned without its lead stays a flat row, and a family with every session pinned leaves Families. Dragging a lead moves the whole family, and a child row is neither dragged nor a drop target. The lead's chevron folds its pinned sessions, sharing the fold Grouping > Family keeps. Review fixes (patch 0273): dragging a lead onto Pinned or into a project asks like the pin button, and a family pinned by a drop lands at the drop spot with its sessions right after the lead; the newest click's family read wins when two land out of order; moving a pinned lead rewrites its sessions' pin values to follow it, so unpinning only the lead or an Undo keeps them in place; the dialog checks pin spots first and disables the family button with "Needs N pin spots, M free"; it keeps its wording while it closes; and a pinned family session stays fixed even when reordering is off. Follow-ups (patch 0274): dragging a pinned lead into a project or to Chats asks about its family before filing it, so Cancel neither files nor unpins, and a family unpin from a drop leaves no Undo pill; the dialog says "1 pin spot" in the singular. See `patches/README.md` and `docs/features/roles-presets.md`.

Fold test flake (2026-10-31, patch 0275): the web test for a pinned lead's chevron failed now and then on CI because it ran right after a drag test, and dnd-kit swallows clicks for 50 ms after a drag ends. The drag helper in `Sidebar.aePinnedFamilies.test.tsx` now waits that window out. Test only; no change to the app.

Queued Compact runs with a shell going (2026-11-01, patch 0279): the rail's Compact button and the action dock's Compact card queued a compact that waited for the chat to be idle with no background shell, and a chat that ends a turn with a shell still running (a CI wait, a watcher) keeps its shell count until the next turn ends, so in a lead chat the compact stayed queued forever. A turn end now counts as the safe point whether or not shells are running, since they outlive the turn and survive the compact; a running turn or a queued message still holds it back.

Queued Compact waits for its own turn (2026-11-01, patch 0280, review fixes to 0279): once shells stopped holding the plan back, a turn that had nothing to do with it (a shell's task notification, a scheduled prompt) could run and end while the save-notes message was still on its way, and the chat compacted before saving its notes. The plan now counts only the turn that runs once the save-notes message is in the transcript, ignores one an earlier compact left there, and gives up after 15 minutes, compacting nothing, if the message never lands.

Queued Compact fix-check fixes (2026-11-01, patch 0281, fixes to 0280): a user message with no content in the transcript could make a queued compact stop in its save-notes step with no timer to end it; it is now ignored, and the timers start first. An older save-notes message loaded by scrolling back no longer counts as this compact's, and a save-notes message that shows up only after its turn ended still lets the compact run instead of waiting 15 minutes.

Queued Compact runs on the server (2026-11-06, patch 0292): the queued Compact lived in the open page, so leaving the chat cancelled it and a long silent think read as a turn end. On 2026-11-06 one chat lost two of three attempts that way: the save-notes message went into a turn that was still running (the model had thought silently for 27 seconds and the page read the chat as idle), and a second plan died with "Compact cancelled: you left the chat before it ran." Reid's decision: if I queue compact and leave the chat, it should still finish compacting. The server now holds one plan per chat in `/data/ae-compact-queue.json` (`AE_COMPACT_QUEUE_FILE` overrides) and runs save-then-compact itself, as the chat's owner and in process, like the merge watcher (`ae_omni_policies/compact_queue.py`). It judges the turn end by Claude Code's Stop hook, not by the page's PTY-derived idle: patch 0292 adds `omnigent/server/ae_turn_edges.py`, which hands every status edge, interrupt and the Stop hook's completed idle (`turn_completed`, which the server never stored) to registered observers, with four one-line hooks. A bare idle never closes a turn. The chat counts as ready when a turn end follows the last turn start, nothing is queued and two seconds are quiet; after a deploy, a chat with no turn end seen waits for 60 quiet seconds with a live `idle`. Each step is written to the file before it acts, so a restart never sends the save-notes message or the compact twice. The save-notes message is a recognized continuation (`COMPACT_PREP_TEXT`), so the slot cap, the rabbit hole and the fork classifier let it through. Four routes: `GET`, `POST` and `DELETE /v1/ae/sessions/{id}/compact` and `POST .../compact/seen` (the page polls every 1.5 seconds while a plan exists and shows a stored failure as a toast once; the dock's snooze moved into `lead_handoff.snooze` and runs on the server). Only Claude Code chats queue; other harnesses get a 422 that the button shows inline, until their turn-end signal is wired. `AE_COMPACT_QUEUE=0` turns it off (503 on queue). Tested by `test_compact_queue.py`, new cases in `test_router.py`, `test_slot_cap.py`, `test_rabbit_hole.py` and `test_fork_suspect.py`, `tests/server/test_ae_turn_edges.py` (in `upstream-smoke`) and a rewritten `aeCompactPlan.test.ts`. Still not checked live: an Esc typed inside the terminal pane sends no interrupt to the server, so that plan waits for the next turn's Stop or the 15 minute limit; click Compact on an idle chat after the deploy and confirm the save-notes message goes out within about 3 seconds. Review fixes (same day, patch 0293 for the page side): a cancel that lands while the driver is reading the store or the chat's live status no longer lets the tick compact the cancelled chat or delete a plan Reid queued again meanwhile, because every step re-checks that the plan it holds is still the chat's plan. A state-file write that fails now stops the prompt or the compact from going out (the plan goes back a phase and the next tick tries again), so a restart can never repeat a step the file did not record. A plan found in `preparing` after a restart now fails at once with "The server restarted while the chat was saving its notes, so nothing was compacted. Click Compact again.", since the save turn's state died with the old process and the store fallback could compact a cancelled turn; queued plans still resume and a compacting plan keeps its check. An interrupt now ends the turn for the settled check, and a running edge with no real turn behind it falls back to the quiet rule (live idle, nothing pending, 60 seconds with no edge) instead of waiting a day. The restart message for a compact that may not have gone out now reads "The server restarted just after the compact went out. If this chat still shows its full size, click Compact again." The page remembers toasted failure ids in `localStorage` and retries `/seen` on each poll until it lands, so a failed `/seen` plus a reload no longer toasts a failure twice.
Pop-up needs text (2026-11-09, patch 0318): a native chat asked "Apply the proposed copy from the assistant's message?" in a turn that wrote no text block, only a redacted thinking block and the tool call, so the user saw a card about a message that was never shown. The native PreToolUse path now reads the tail of the Claude transcript for a pop-up tool call (`AskUserQuestion`, `mcp__ae__ask_user`) and stamps `ae_reply_text_before` on a copy of the call's arguments, because the server rebuilds the policy event from only the tool name and arguments and drops any other key; a new policy, `ae_omni_policies.popup_text.popup_needs_text` (seeded as `ae_popup_needs_text`), denies the call only when the flag is explicitly false, with a reason telling the model to write its answer as reply text and ask again. An unreadable transcript, another harness or an older hook leaves the flag out and the policy abstains. A server whose policy store already holds the other seeded policies is not re-seeded, so it adds the policy on Settings > Global Policies.
Pop-up needs text review fixes (2026-11-09, patch 0319, review round 1 of patch 0318): the reply-text check split the transcript with `splitlines`, which cut a real line at a raw U+2028; a user line holding a tool result plus text counted as a typed prompt; a plain-string assistant reply was not seen as text; text written before an earlier, answered pop-up excused a later one; and a fixed 2 MiB tail missed a prompt behind a large tool result. The check now splits on newline only, treats any tool result as not a prompt, reads string replies, stops at an earlier pop-up (skipping the call being checked by its `tool_use_id`), reads backward up to 16 MiB and answers unknown past that, and drops any failure. The stamp also removes a flag the model supplied itself and re-reads a no-text answer after 0.3 s.
Pop-up guard removed (2026-11-09, patch 0324, the owner's call): the guard of patches 0318 and 0319 (https://github.com/analyticendeavors/omnigent-ae/pull/520) read the Claude transcript when a pop-up tool call started, but measured on the box, interactive Claude Code writes the turn's text and tool_use to the transcript, and fires MessageDisplay, only after PreToolUse returns, so no wait could see the text; the live test refused a pop-up that had text above it, the policy row was deleted, and the stamp still slept 0.3 s on every pop-up. Patch 0324 deletes the reader and the stamp (the hook file is back to its pre-0318 content), and `popup_text.py`, its registry entry, its seed and its tests go with it. The rule lives as an agent instruction.
Queued Compact counts a Stop that arrives before its items (2026-11-06): the first live check of the server-run Compact failed. On a throwaway Claude Code chat the save-notes message went out one second after the click and the chat answered "ready" a second later, but no compact followed, and the plan was still waiting six minutes on when the 21:11Z deploy ended it. Root cause: the driver counted the save turn as over only when the Stop edge arrived after the reply item, but each forwarder poll reads the transcript before the hook file, so a turn that finishes between the two reads posts its Stop first (here the Stop at 21:05:09.86, the prompt and reply items at 21:05:10.17 to 10.19). The driver now counts the save turn as over once a reply to the prompt is in and a Stop has come since the send, in either order; the compact still waits until no turn has opened since that Stop and two seconds are quiet. Items count as activity for those two seconds while a plan waits, because the save turn's running edge comes from the runner on a separate path and can land just after the reply. A StopFailure is now measured from the send too, so one posted before the prompt item still ends the plan with nothing compacted. Tested by three new cases in `test_compact_queue.py` (the first and third fail on the old code).

Queued Compact waits for a real turn end and resumes the chat after compacting (2026-11-13, patch 0367): a queued Compact paused a chat that was still working. Root cause: the turn that began at 07:00:04 never reached the server as a turn start, because claude-native reports one only through Claude's status file, which publishes on change only, and the forwarder dropped the UserPromptSubmit hook that did fire. The 06:59:33 Stop stayed the newest turn edge, so the queued Compact read the chat as settled the moment its pending inputs drained, posted the save-notes prompt into Claude's input queue, gave up 30 seconds later because no turn had opened, and left the prompt to run at 07:09:59 as the chat's next instruction ("start no new work"). Nothing posted after the compact either, so the chat sat idle. Fix: the fork patch forwards UserPromptSubmit as a `prompt` turn edge (`ae_prompt_edge.py`; an `external_session_status` with `ae_prompt_edge: true`, which calls `notify_turn_edge(sid, "prompt")` and publishes no status, so the badge and runner see nothing new). The driver now never settles on quiet while a prompt edge is newer than the last Stop and work items came after it; it waits for the Stop, and a prompt a hook blocked (no work after it) falls back to the 60 second live-idle rule. A prompt edge after the send also counts as the save prompt having started, so it is no longer cancelled at 30 seconds while it waits in Claude's queue (the 15 minute limit still applies). The save prompt no longer says "start no new work"; it asks for 'ready' or 'ready, resume'. After a compact whose save reply said 'ready, resume', a new `resuming` phase waits for the compaction item and a settled chat, then posts one resume note (`COMPACT_RESUME_TEXT`), once even across a restart; the page sees `resuming` as `compacting`. A compact that fails after the save prompt reached the chat (took too long, ended in an error) posts `COMPACT_CANCELLED_TEXT` so the chat carries on; an interrupt posts nothing. Both notes are recognized continuations for the slot cap, the rabbit hole and the fork classifier. Tested by new cases in `test_compact_queue.py` (a replay of the incident, a blocked prompt, a prep waiting in Claude's queue, resume only on 'ready, resume', the cancelled note, restart in `resuming`), `test_slot_cap.py`, `test_rabbit_hole.py`, `test_fork_suspect.py`, `tests/server/test_ae_turn_edges.py` and `tests/harnesses/claude_native/forwarder/test_status.py` (now in `upstream-smoke`). Review round 1 (same day, outer only): the resume request is saved with the compact step so a restart still resumes; the request now needs the last save reply to be exactly 'ready, resume' (case, punctuation and separators ignored), not a phrase inside a longer reply; every failure after the save prompt was accepted (never started, too long, not clean, a restart, a refused compact) leaves a saved marker and posts the cancelled note once, never after an interrupt; the resume window is 15 minutes and logs when it runs out; a resume note refused with an error is retried 3 times 30 seconds apart, then shown to the page; a prompt with work after it and no Stop settles after 10 idle minutes on a chat that reads idle. Still open: why the status file sent no running edge (the idle badge mid-turn); whether Claude fires UserPromptSubmit for a turn it starts itself from a queued task notification while idle; the live check, a Compact queued mid-turn on a chat with a background subagent must wait for the Stop, compact, and send the resume note only after 'ready, resume'.

Queued Compact reads the resume request from the first line (2026-11-13, outer only): the live check of patch 0367 passed its first half (a Compact queued mid-turn waited for the Stop and compacted) but sent no resume note. Root cause: the request had to be the whole save reply, and the chat answered 'ready, resume' followed by a status paragraph. The first non-empty line of the last save reply now decides (case, punctuation and separators ignored), so a phrase quoted inside a sentence still does not count, and the save prompt asks for the word on its own first line. Review round 1: markdown emphasis or heading marks on that line are ignored, and a note after a colon, an open bracket or a spaced dash ("ready, resume: 2 jobs running") does not hide the answer. Tested by new cases in `test_compact_queue.py`.

Queued Compact survives a Stop followed by a new message (2026-11-13, outer only): Reid queued a Compact on a chat, then 10 seconds into the save-notes turn the page sent a Stop and, 0.2 seconds later, a short follow-up message; the queue read the interrupt as a cancel and dropped the plan (the PR merge he clicked a few seconds later was unrelated). Root cause: `_read_book` called any interrupt after the save prompt unclean, with no room for a redirect. Fix: an interrupt now waits `STEER_GRACE_S` (5 seconds); a user message other than the save prompt in that window sends the plan back to `queued` (no cancelled note), so the save prompt goes out again once the redirected turn reaches its Stop. A bare Stop still cancels after the 5 seconds. The edge book keeps `last_interrupt_mono`, `last_user_seq` and `last_user_mono` for this. Review round 1 (a Sonnet reviewer and Codex): the message counts on either side of the interrupt edge, matched by arrival time, since the forwarder can post it first; server notes (merge watcher, child wake, the Compact notes) never count; a requeued plan keeps `steer_seq` and sends no save prompt until a Stop newer than it arrives, or 60 seconds of quiet on a chat that reads idle (a fast turn whose Stop came before its items), so a lagging prompt or running edge cannot let the save prompt into the redirected turn; a failed state write leaves the plan in `preparing` for the next tick. Tested by new cases in `test_compact_queue.py` (the incident replay through to the compact, the item before the interrupt edge, no edges after the redirect, the Stop before the items, a merge watcher note after a Stop, a failed write, a message after the window, only the save prompt after a Stop).

claude-native chats show running when a turn starts while background helpers run (2026-11-13, patch 0372): a chat with a background helper alive read idle for a whole turn. Root cause: the server publishes running for claude-native only from Claude's status file, which writes on change only and stays busy across a Stop while a helper lives, so the next turn changed nothing and no running edge was published; the prompt edge from patch 0367 arrived but published no status. Fix: new `omnigent/server/ae_prompt_running.py` remembers that a prompt opened since the last turn end; `routes_events.py` publishes `running` (and the running turn edge) on the first assistant message or tool call after it, in both the per-event and the batched item paths. A Stop, a failure or an interrupt clears the memory, so late items of an ended turn never flip the chat back, and a blocked prompt with no work after it never leaves a spinner. The next Stop publishes idle as before. Tested by `tests/server/test_ae_prompt_running.py` (now in `upstream-smoke`). Live check: start a background subagent in a throwaway claude-native chat, let the turn end, send a new message; the chat must read running within a few seconds of the reply starting and idle after the Stop.

Review fixes on patch 0372 (2026-11-13, patch 0373): the forwarder posts items before hooks within a poll, so a turn's first tool call could reach the server before its prompt edge and the chat stayed idle; a new `_prescan_prompt_edges` posts each poll's main-session prompt edges first and records the cursors so the hook phase never posts one twice. A replayed (deduplicated) item no longer flips the chat or spends the open prompt: the flip moved into `_publish_persisted_external_item` and the external assistant message path, which run only for newly persisted items. Every interrupt clears the open prompt (the clear is in `_publish_interrupted`, covering `/interrupt` and `external_session_interrupted`, not only `stop_session`). An open prompt expires after 600 seconds (`OPEN_PROMPT_TTL_S`), so a hook-blocked prompt cannot be taken by an unrelated item much later. Not changed: late items of turn N can still take turn N+1's flag when N+1 is a blocked prompt (narrowed by the replay and expiry fixes); a plain idle without `turn_completed` does not clear the flag, because the status file can publish idle mid-flow; an external assistant message after a blocked prompt flips the badge (bounded by the expiry). Tested by new cases in `tests/server/test_ae_prompt_running.py` and `tests/harnesses/claude_native/forwarder/test_status.py`.

Queued notes survive a reload (2026-11-01, patch 0285): a message sent while a chat was busy waited in a queue that lived only in the page, so a reload, a phone tab discard or the "Omnigent was updated" reload during a long turn lost it without a word (one went this way during a 28-minute turn). The queue is now kept in the tab's session storage and put back once after a reload, ahead of anything queued since, with the same message id so it still sends once and in order. A text-only note under an hour old sends on its own when the chat goes idle; one with attachments (they cannot be kept), one over an hour old, or any note in a browser without Web Locks comes back waiting for Retry, with a toast saying why. A tab duplicated from another leaves the notes to the tab that has them and says so (changed in 0286: they come back waiting for Retry). Still open: a note already taken off the queue is lost if the page dies before it is sent, a closed tab never reopened loses its notes, a note typed in the first second after a reload can go ahead of older restored ones, a restored note for a chat that is off screen and not idle in the sidebar waits unseen until that chat is opened, and a note waiting for Retry holds back later notes in that chat until Retry or remove.

Queued notes review fixes (2026-11-01, patch 0286, review fixes to 0285): split view panes share one tab's storage, so they overwrote each other's queued notes, and reloading one pane deleted every pane's notes with a wrong "other tab" message; each pane now keeps its own. Each note now remembers which signed-in user queued it, so a note is never handed to another user. Attachment names now survive more than one reload. A lock error no longer counts as another tab, and notes that might also live in a tab this one was copied from now come back waiting for Retry instead of being deleted. Still open: after a split view page reload, a note queued in a pane that later moved to another session can be picked up by a different pane; it still sends once to its own chat but does not show in that pane's list. And a note queued before the signed-in user was known comes back as a Retry to whoever signs in next in that tab, who can read it.

Queued notes waiting for sign-in (2026-11-01, patch 0287, fixes to 0286): notes held back because the sign-in could not be confirmed after a reload waited without a word, and if the sign-in check never answered they waited for the life of the page, so retyping them meant a later reload sent both. The check now gives up after 10 seconds, and a toast says how many notes wait for the sign-in and to reload rather than retype them.

Transcript keep recognises its own posts (2026-10-29, second fix for the keep of patches 0243 to 0245): live, the keep still never fired because the runner posts a transcript item over the event tunnel, which names no item id, so the forwarder could not tell its own items from foreign ones and the record never advanced past the seed. The forwarder now derives the id the server gives a post from its source id, a returned id still wins, and a post that failed never counts. When a foreign item holds the record the log says which item and why, once per change. After two cold relaunches of the same session, the first shows the record's item count following the server within a minute and the second shows `ae.claude_native.resume = kept` (patch 0249).

Neutral names in the repo's own files (2026-10-29): the code, comments, docs and test fixtures outside `patches/` and `upstream/` no longer name the box's users. History says "the owner" or "the second box user", model-facing text says "the user", and fixtures use `owner@example.com` and `member@example.com`; tests that check a real value is refused build it from parts at run time. Behaviour changes with it: the orchestrator's pop-up tool is now `ask_user`, and the old name stays a hidden alias (callable, absent from tools/list, `LEGACY_ASK_TOOL` in `mcp.py`) so running sessions and skills keep working until a later PR drops it. The child-wake note reads "the user picked <choice>", and the continuation matcher also accepts any single capitalised word there, so notes already in history still match. The host config sync's personal-marker list moved out of source into a box file (`config/host-scrub.txt`, or `AE_HOST_SCRUB_FILE`; gitignored, with an example), and the sync refuses to run while it is missing or empty. `deploy/pull-secrets.sh` files the secrets under the invoking user's primary group by default (`AE_ENV_GROUP` still overrides). Left as they were: the managed block of CLAUDE.md (generated from ae-claude-skills) and `tests/deploy/fixtures/team-mcp.json` (a byte copy of an ae-claude-skills file that a test compares). Why: defence in depth behind the release scans, and a step toward putting the last names on the private leak denylist (docs/LEAK-SCAN.md).

The hidden alias goes (2026-10-30): the old name of `ask_user`, kept one day as a callable alias outside tools/list, is removed from `mcp.py`; a call by that name now answers as an unknown tool. Nothing on the box or in ae-claude-skills called it, and clients call only the tools that tools/list shows.

Transcript keep survives an image read (2026-10-30, patch 0251): after patch 0249 the keep's record followed the server, yet any session that had viewed an image since its last rebuild still rebuilt on every cold relaunch, because Claude Code's native image-read record carries the image's base64 a second time in `toolUseResult` and the keep refused any file the clone sanitizer would change. The keep now refuses only for a change the model would see, so that one shape stays, and a refused keep logs one INFO line naming the check that failed (ids, counts, types and line numbers only). Live check: Read a PNG in a test session, then cold-relaunch it twice; the second shows `ae.claude_native.resume = kept` and a first call whose cache read is about the history size. A session that still rebuilds shows the new line. If the first call after a keep of a file with an image read writes far more cache than a normal turn, revert the native-shape exemption.

Slots-full approval card (2026-10-29, patch 0252): the slot cap's ask now reads as a decision: the question, then Run as overflow and Decline right under it, then the park hint, with the holding sessions folded behind "Show the N holding slots". The buttons used to sit below a long paragraph and fell off a phone screen.

Slots-full card fixes (2026-10-29, patch 0253): the card no longer mis-splits holder titles that contain a number and a period, reads each holder's detail from the end of its row, appears only for the slot cap's own ask, and drops the holder toggle when only a pending claim holds the slot.

Family cost card (2026-10-29, B1 of the token-saving plan): an Admin Center card ranks each session family's spend over 7 or 30 days. `family_cost.py` sweeps every session's usage every 15 minutes (one process, a `flock`), keeps a per-day snapshot for 35 days in `/data/ae-family-cost.json`, and `GET /v1/ae/family-cost?days=7|30` answers from the last sweep (`warming` until the first). A window is the total now minus the snapshot at its start; a chat started inside the window counts from zero. Checked against Claude transcripts for four families (two with five restarts): within 1.4%; a Claude lead's usage already holds its subagents, a Codex session's native subagents add tokens only (the parent's cost includes them), and there is no cache-write figure. An admin sees every family with a host chip, anyone else their own. "Show cost as" (tokens or dollars) and "Runaway family" (5M tokens or $25 spend today; one amount per unit) are saved per user in the roles state through `POST /v1/ae/family-cost/settings`, because the extension cannot PUT. Out of tree only: no upstream patch.

Family cost review round 1 (2026-10-29, two reviewers): the settings POST now carries the JSON content type and trusted origin checks; the family walk goes up from every session (handoff, `ae.orch`, parent links) so children linked only by parent, subagents of subagents and handoff chains join one family and an orphan is its own lead; a session total that falls adds the sample before it to an offset; days are Pacific time and start at the last sample before midnight; the state file reloads when its mtime changes, and an unreadable one is renamed `.bad-<timestamp>` instead of overwritten.
Family token flag skips cache reads (2026-11-07): the Spend card's "Flag a family over this many tokens a day" now counts input plus output only, so a long chat that re-reads its context every turn no longer trips the 5M default; on 2026-11-07 all 16 families on Needs you were over the token flag and 11 were over it alone. The dollar flag, the ranking, and every displayed total still count cache reads. `family_cost.py` adds `today.flag_tokens`, `attention.py` and the extension's `familyCost.ts` read it (a snapshot from before the change falls back to the full total for up to 15 minutes), and the hint says tokens skip cache reads. Out of tree only: no `[ae-patch]`. `docs/features/admin-center.md`.

Security reviewer slot (2026-11-01, patch 0276): a roles preset's reviewer list gains an optional slot that runs only when the diff is security relevant, with a security-only brief. A slot (Claude or another agent) carries `when: "risky"` and `focus: "security"`; Reviewer 1 never runs conditionally, since fix checks go to it, and old saved presets stay valid unchanged because both fields are absent there. Every starter now ends with one: Opus high, or Sonnet medium on Save tokens, which becomes a two-reviewer preset (Haiku medium, then the security slot). Solo and Quick fix, which had one reviewer, now have two. A user who already saved presets keeps their saved starters: `with_new_starters` only offers a starter id they have not seen, so the changed starters reach people who never saved, and anyone else gets them through Restore starters; no migration was written. The lead notice lists the slot in its numbered position with an explicit trigger list (authentication or OAuth, secrets or key vaults, MCP servers, HTTP endpoints or Workers, parsing of outside input, permissions or policies, anything bound for the public repo) and says to skip it, say so and merge the rest as usual when the diff is not risky. Decided: a serializer drops unset `when` and `focus` from a slot's JSON, so slots saved before this change keep their exact shape. Tradeoff: the trigger is the lead's judgment of the diff, not a path match, so a borderline diff can go either way.

Reviewer personas (2026-11-01, patch 0277, approved by the owner): the security slot of patch 0276 becomes a general mechanism. A reviewer slot names a persona; a persona is a name, a purpose, an optional model and effort, `when` (`always` or `risky`), an optional trigger and instructions. Five are built in and read-only (`general`, `security`, `performance`, `tests`, `accessibility`), defined in code and never stored per user; users add up to 20 of their own in Settings > Roles, validated like presets (no control characters, newlines only in instructions, an id may not reuse a built-in id). `when` moved from the slot to the persona and `focus` is gone; neither had been released, so there is no migration and no legacy reader. Reviewer 1 cannot use a risky persona; a slot naming a deleted persona renders as a plain reviewer with a one-line note. The starters' third slot is now `{persona: security}` (Save tokens keeps its Sonnet medium override). The lead notice lists the instructions of only the personas the preset uses, every line quoted so a user's text cannot forge a notice line, and a lead-picks notice names the personas in brackets and lists the instructions of the shown presets' personas once. Decided: a persona's model fills in only Claude slots, because the model names are Claude's and an agent slot such as Codex has its own; a `trigger` field was added so a user persona that runs only when risky can say what risky means; the built-in personas travel in `GET /roles` as `builtin_personas` so the web has one source. Tradeoff: a persona's instructions are advice to the lead, who copies them into the reviewer's brief; nothing enforces that it does.

Reviewer persona review fixes (2026-11-01, patch 0278): review round 1 of the persona work found eleven things, all fixed. A save that left `personas` out could pass the body check and still fail against the stored personas, a 500; it is now the normal 422 with the "reviewer 1 must always run" message, on the roles save, the Family cost save and applying a preset suggestion. The per-session Customize dialog now gets the persona list, so a persona slot shows its model instead of "The lead chooses". The lead-picks line carries a risky persona's trigger. Removing reviewer 1 drops a risky persona from the reviewer that moves up, with a visible note. Deleting a persona turns its slots into plain reviewers on the persona's model and effort, in saved presets and unsaved drafts, so no preset is left invalid, and the dialog text is true. Quoted instructions keep indentation and blank lines. A tab is allowed in instructions and every refused character is named; bidi, zero-width and other invisible characters are refused and removed from the notice, and a persona's name, purpose and trigger are quoted there. One bad stored persona no longer hides the rest. The cap reason, the missing-persona note and the form's problems are readable by a screen reader. A persona with no effort runs at medium in the spawn too, as the notice says, and the Quick fix starter runs its security check on Sonnet medium, like Save tokens. Existing users keep their saved starters, as before.

Reviewer persona look (2026-11-01, patch 0282, mockup option A picked by the owner): Settings > Roles drew the persona as a native select beside the model picker, which looked different from it (a phone showed the system's own list), left reviewer 1 unnumbered and printed "Reviewer 1 always runs, so risky personas are off here." on every build of the row. Each reviewer now has a header line: its number, a persona picker built from the same popover as the model picker (so the keyboard and screen reader behaviour is the same, and a risky persona on reviewer 1 is a disabled row that says "runs only if risky"), a "when" chip (always in teal, risky diffs in amber) and the remove button. Model and effort sit under the header on a phone and on the same line on a wide screen; a model that comes from the persona says so. The reviewer 1 reason moved into the persona tip beside the Reviewers label, and a full list reads "3 of 3 reviewers.". The personas list shows its purpose in the muted text colour, its model and when as chips, and Duplicate is an icon button on a narrow screen. Styling and layout only: no data, validation or save change. Tradeoff: at the settings page's 670 px width the "from persona" note is a caption under the row instead of inside the model button, because the model name and the Codex tag need the room.

Reviewer persona look review fixes (2026-11-01, patch 0283): review round 1 of patch 0282 found seven things, all fixed. Remove is now last in the page order, so Tab goes persona, model, effort, Remove at every width (a narrow row puts it beside the effort bar). The one-line row starts at a 40rem container, not 32rem, because between the two the model picker had about 50 px. "Remove one to add another." is visible text again. The amber "risky diffs" chip is darker in the light theme to pass AA contrast. A persona or model name that is cut off has a tooltip with its full text. "from persona" sits beside the model picker on a phone and under the row on a wide screen, instead of over the model name. The two info icons beside Reviewers are one tip; to fit its 140 character limit the tip no longer says Codex is skipped when Helpers is own subagents only (the page already shows that note when it applies), and the separate persona tip is gone.

Reviewer persona look fix-check fixes (2026-11-01, patch 0284): the fix check of patch 0283 found three things. "from persona" ran past the right edge of a narrow row, so it is now a caption line under the model picker on every width. The reviewer grid left empty rows (each added a row gap, 16 to 24 px of dead space), so every item now takes the next used row. The "persona is gone" note moved up in the page order to sit right after its persona picker, matching where it shows.

Bridge dir retention (2026-10-29, patch 0257): the ae-v0.18.0.190 deploy restarted the host and every relaunched chat rebuilt its transcript with the reason "no forwarder cursor". Root cause: the host's startup orphan sweep deleted each claude-native bridge dir, cursor and frontier record included, as soon as its runner was dead; the dir was born again only when the new runner prepared it, two minutes later in the logged case, so the sweep won whenever a relaunch lagged by more than about 3 s. A dead-owner claude-native dir now stays for 7 days after the latest of its owner marker, cursor and frontier record (the window codex-native uses), losing only the runtime files (the six a re-prep unlinks, plus `tool_relay.env`, which holds a token). Archive (the UI and the auto-archive sweep both PATCH `archived=true`) never told the runner to delete its bridge dir, so `_archive_stop` now asks the runner for its resource cleanup before stopping the host runner, and an archived chat's dir goes at once. A cold launch also logs one INFO line saying whether the cursor and the record exist. Two things linger for the same window: the attachment cache at `~/.omnigent/attachments/<hash>` (700), which the sweep removes after the rmtree, and `bridge.json`'s token, the long-lived token re-prep already reuses, both behind the same same-user 700 boundary codex-native already accepts for 7 days. The Admin Center box-health cleanup does not touch these dirs: its `/tmp` litter allowlist and the box's tmpfiles rules name other paths. Live check: after the deploy the host log's `native_bridge_orphans` line keeps claude-native dirs; the next relaunch's keep reason line no longer says "no forwarder cursor"; the restart after that keeps the transcript or names the next failed check.

Archiving a session archives its side chats (2026-11-17, patch `0387`): side chats are hidden from the session list, so archiving a lead left them running unseen and still counted as working slots. The archive route now archives each side chat (nested ones too, depth 8), marks it `ae.side_chat.archived_with=<session>`, and defers its runner teardown past the Undo window like the session's; Undo restores only the marked ones, so a side chat archived by hand stays archived. Out of tree, `sessions.drop_orphan_side_chats` leaves a side chat out of `/v1/ae/capacity` and the slot cap when its source session is archived or deleted (the source is looked up when it is missing from the 500-row listing, so a live source past the cap does not orphan it), which also clears side chats orphaned before the patch.

Review fixes for the side chat archive (2026-11-18, patch `0388`): the cascade only touches a side chat the caller owns (a reader of a shared session can fork one that only they own), `ae.side_chat.archived_with` is a server-internal label, the marker is written before the flag, an Undo that lands mid-cascade reverts what was archived, and both cascades walk through side chats archived or restored by hand. Out of tree, `drop_orphan_side_chats` drops a nested chain whose root source is archived, and a bad source label keeps the row instead of failing `/capacity`.

Side chat follow-ups (2026-11-18, patch `0389`): a store error on one side chat no longer stops the archive or Undo walk from reaching its nested side chats, and opening a side chat from an archived chat is refused with a message to unarchive it first. Patch `0390` fixes its review findings: an ownership check that errors prunes the walk instead of passing it, and a side chat opened at the moment its source is archived is archived with it.

Question pop-up wrap (2026-10-29, patch 0258): a long unbroken string in an AskUserQuestion question, such as a pull request URL, ran past the right edge of the card on a phone. Root cause: the question sits in a `fieldset`, whose default minimum width is its content width, so the legend could not shrink and the text could not wrap. The section now has `min-w-0`, and the question and option descriptions use `overflow-wrap: anywhere`. Touches `AskUserQuestionForm.tsx` and the `ApprovalCard` suite.

Question option wrap (2026-10-30, patch 0259): review of patch 0258 found that a long unbroken option label still ran past the card, because the label sits in a flex column that also could not shrink. The label and description wrapper now carries `min-w-0` and `overflow-wrap: anywhere`. Touches `AskUserQuestionForm.tsx` and the `ApprovalCard` suite.

Needs you, phase 1 (2026-11-03, patch 0290): one list of what needs the owner across the Admin Center, with a sidebar badge. The Git card's Needs you rules moved from `git.ts` to the server (`git_center.needs`, pinned by a golden parity file shared with the old TypeScript rules), Box health's uncapped lines feed the same list, and each person can dismiss an item until it gets worse or expires (`attention_dismissals.py`, `/data/ae-attention-dismissals.json`). Review opens a chat briefed on one item; Fix, Merge and the other sources are phases 2 and 3 (`docs/features/admin-center.md`, Needs you).

Admin Center status pills (2026-11-06, the owner, phase 1 of the redesign): the ten status tiles took four rows above Needs you, so "is anything other than Needs you wrong?" needed a scroll. Four pills now read in one row (two by two on a narrow page and on a phone): Ship (release, deploy hold, git), Box (box health, host, secrets), Cloud (Fabric, Databricks, Azure spend, Key Vault) and Accounts (accounts, presets and families). A pill takes its worst member's health, says the members that are not ok first in one line, and counts them ("1 to check", "2 problems"); hover or keyboard focus lists every member, and a click goes to the group's first card. The header gains one sentence from the Needs you counts ("2 urgent, 3 to review.", "Nothing needs you. Everything else is fine."), Needs you shows "N chats waiting on you" as a link to the Session Dashboard, and an empty Needs you reads "Nothing needs you. Checked 11:57 AM." Web extension only (`tiles.ts`, `verdict.ts`, `cardNav.ts`, `attention.ts`, `fabric.ts`, `style.css`); no patch, no server change. `docs/features/admin-center.md`.

Reminder tools (2026-11-03): a box session could not set a phone reminder. The task-store worker (`ae-claude-skills` `task-store-worker`) has `create_task` and `list_lists`, but the box's `ae` MCP server (`ae_omni_policies.mcp`) ported only the five fork-triage tools, so the user had to add the task by hand. Root cause: the port stopped at route C. The server now has `create_task` and `list_lists`, ported step for step (a `due_time` sets `reminder_at` to the same home-clock instant as `due_at`, which is what the app pushes; a date alone is all-day with no push; a list is matched by exact name and never created; a create that timed out or got a 5xx is re-read, never resent: the re-read matches title, origin, due, list and note, and only a task created since the call began), plus `cancel_task`, which is this fork's own. The task app refuses `DELETE` from a machine credential on purpose, so cancel sets the task's status to `dropped` through the PATCH the credential already has: the task leaves the user's lists, its alarm stops (the push query skips dropped tasks), and they can reopen it in the app. It only touches an open task this server created (`origin_surface` is `omnigent-ae`) and carries no `parked` tag, so it cannot drop a task the user made or is working, and it leaves parked forks to `complete_task`, which also closes their issue. New routes `GET /v1/ae/lot/lists`, `POST /v1/ae/lot/create` and `POST /v1/ae/lot/{task_id}/cancel` are admin only like every `/lot` route. Times are read in `AE_TASK_STORE_TZ` (UTC when unset), so the server must have it set to the home zone or an alarm rings at the wrong hour. `create_task` and `cancel_task` sit on `WRITING_TOOLS`, so the propose-only agents still cannot call them.

Box health auto-clean (2026-11-06, the owner): the hourly relay pass now also cleans the box user's own host leftovers without a button: Claude temp folders older than two days, and old `/tmp` test leftovers (never under two days, 40 second budget), through the same guarded plans the buttons run (live sessions, locks, `.git` trees and the age rule still apply). It skips the pass when a button request is queued. Docker prune and review checkouts stay buttons. `AE_BOX_HEALTH_CLEANUP=off` stops it. No `[ae-patch]`.

Box health Docker card counts what Prune Docker frees (2026-11-23, the owner): the card offered a prune and came back after every press, because its size was `docker system df` Reclaimable, which on the containerd image store counts images that running containers use, while the button prunes only dangling images (and old build cache). On the direct path the relay now sizes images from `docker image ls --filter dangling=true`: images no container uses and older than the button's `until` filter (`AE_BOX_HEALTH_STALE_DAYS`), so a press clears the card. Build cache still uses `docker system df`. Under the root-owned gate, which offers only `df`, the old reading stays until the gate gains an image listing. No patch.

Social card, part 1 (2026-11-06): the Admin Center gets a Social card and tile that plan the LinkedIn
and YouTube Community posts for every upload on the AE and HC channels, the tool posts and the weekly
expert post, and score how consistent the posting is. The server side is `ae_omni_policies/social.py`
(store, planner, score, gaps) with admin-only `/v1/ae/social` routes; a two-hourly box timer runs
`ae_omni_policies/social_relay.py` over the youtube-video-publish lister. The compact card is
`social.ts`. The full view, the editor, Needs you and phone alarms, and the weekly drafting run are
later parts. No core patch. See `docs/features/admin-center.md`.

Decision-1 preset advisor (2026-11-06): in a lead-picks chat the lead can call a new `advise_preset` tool with a one-line neutral summary and a few counts, and the server asks a judge model (Microsoft Decision-1) which of the owner's first eight presets fits, plus a task size (information only). It answers `use` with a preset only when the top choice is a real preset, at least `AE_DECISION_MIN_CONFIDENCE` (0.6) and 0.15 ahead of the next; otherwise, and on any error or timeout (3 s, no retry), `decide_yourself`, and the lead picks as before. Raw user messages never go out. The summary is refused, by reason code and never by echoing the text, when it matches the private denylist (`AE_DECISION_DENYLIST_FILE`; a missing or empty file sends nothing), carries a capitalized word that is not on the allow list, repeats any user message in the session, or holds a newline, link, email, path, quote, backtick or long number. New `packages/ae_omni_policies/.../decision_advisor.py`, route `POST /v1/ae/sessions/{id}/advise`, the `advise_preset` tool, an `advisor` list on the preset run line (summary kept only as a hash unless `AE_DECISION_LOG_SUMMARY=1`), and `deploy/pull-secrets.sh` reads `foundry-decision-api-key` and `foundry-decision-endpoint`. One `[ae-patch]` on `ae_roles.py` adds a single line to the lead-picks notice when the advisor is available; the preset list stays. Off unless `AE_DECISION_ENABLED=1`; the judge model retires 2027-02-04, so the feature is built to come out in one pull request.

Decision-1 preset advisor, review round 1 (2026-11-06): a refused summary is never kept in the run log, even with `AE_DECISION_LOG_SUMMARY=1`; a preset's id and text both pass the denylist and name checks before they are sent; the no-passthrough check now fails closed when the transcript cannot be read to the end or a lead has no user text, and also reads messages still queued for the harness; helper sessions are refused; the name check reads any script, checks the first word and a capital after a hyphen, and matches a multi-word allow entry as a whole phrase; whitespace is collapsed before the denylist match; any domain-shaped word, and digits split into groups, are refused; a session is cut off after 10 calls an hour; the size is the legend label of the most likely level, not the float `score`; and the run-log write no longer holds up the reply. No new `[ae-patch]`.
Box health auto-clean log (2026-11-06, the owner): each hourly auto-clean run now writes one journal line per action ("removed N; kept: locked X, recent Y"). The unit logs at warning level, so the earlier info line never showed. Counts only, no names. No `[ae-patch]`.

Box health, one rule for the cleanup scan (2026-11-06, the owner, phase 2 of the Admin Center redesign): a cleanup scan whose count stopped at its entry cap marked the whole box source "could not be read" in Needs you and put a question mark on the sidebar badge, while the Box card and pill said OK. `box_health.scan_certainty()` now answers once (ok, capped, late, cut or missing) for both. A capped scan is one amber Box health need, "Temp folder count stopped early", and a late one is "Cleanup scan is late"; both turn the Bloat line and the Box pill amber and show in Needs you under a new "Cleanup scan" line. "Could not be read" stays for a stale reading, a failed fast part, a cut scan or no slow scan. Dismissed temp items still stay while the count is unsure. No `[ae-patch]`.

Needs you, phase 3 (2026-11-06, the owner, phase 3 of the Admin Center redesign): seven more sources feed the Needs you list, so the status pills, Needs you and the sidebar badge agree: Secrets on box (a key that is missing, expired, refused or expiring soon), Key Vault (expired, expiring, disabled and used by the box, or rotated in only one vault; a secret the box already lists is listed once), Cost targets (a target over its limit), Databricks credit (used up), Box host (rejected, or did not reconnect), Accounts (a weekly window full) and Spend families (over the runaway flag today). Each reads a cache or a saved file and never calls Azure, Databricks, GitHub or the host list; a reading that is missing or too old makes its source "could not be read" instead of empty; no secret value reaches an item, a dismissal or a Review brief. Host and Accounts rows have no Review chat. Each card gets a live "N in Needs you" line. `attention.py`, `attention_dismissals.py`, `vault_inventory.py` (`uncapped`), `host_relink.py` (`state_of`), `shared_accounts.py` (`labels_or_none`), the extension's `attention.ts`, `attentionStore.ts` and seven cards. No `[ae-patch]`. `docs/features/admin-center.md`.

Needs you, waiting link and Review all (2026-11-07, patch 0300): the "N chats waiting on you" line never opened the Session Dashboard, on phone or desktop. Root cause: the SDK sent `navigation.openPage(id)` with `params: undefined`, the host drops any message carrying an undefined value without replying, and the call timed out after 10 s. Core patch `0300` leaves `params` out when the extension passes none. Also at phone width the "N to review" count was drawn over the "Review all N" button (`.attn-label` had no `flex-wrap`), and the waiting link was 18 px tall; the label now wraps and the link is a 44 px touch target.

Admin Center groups (2026-11-06, the owner, phase 4 of the redesign): the cards sit under four headings, Ship, Box, Cloud and Accounts, matching the status pills, and a pill click now lands on its heading. A card drags or arrow-moves only inside its group, the saved layout is kept per group, and a layout saved before the groups still loads with its order kept per group. An integration the server says is not configured (Fabric together with Databricks for Compute, Databricks credit, Azure spend, Key Vault, Git repos) leaves the board and is named in one "Not set up on this server" line; an admins-only answer or an error keeps its card, and an unconfigured Key Vault is no longer amber. Web extension only (`board.ts`, `boardLayout.ts`, `boardDrag.ts`, `cardNav.ts`, `tiles.ts`, `style.css` and the card tiles); no patch, no server change. `docs/features/admin-center.md`. Also in this phase, Databricks credit in Needs you no longer marks the whole list unsure (the "?" on the sidebar badge) just because the saved spend reading is over 48 hours old: a successful reading that is merely old and under the credit stays quiet unless you dismissed that item or it was already at or over the credit, the way an idle account is treated (`attention.py`).

Box health folder share (2026-11-06, the owner): the hourly box scan and the hourly auto-clean now give each Claude chat temp folder (and each pytest run) its own share of the entry cap, `AE_BOX_HEALTH_GROUP_MAX_ENTRIES` (default 100,000, range 100 to 10^8, the root cap wins if smaller). On 2026-11-06 the host's cache temp root held 1.69 million entries and one finished chat's folder held 1.28 million; it spent the single shared cap, so every folder after it in scan order was never counted or proven stale, and the planner stopped there too. `Budget.child` spends from the root budget but refuses an entry past its own share first, so a folder past its share is marked unproven ("at least", never stale, never cleaned) and the walk continues; only the root entry cap or the seconds cap ends it. There is no age pass: an over-share folder stays unproven (the measured big ones hold git checkouts the cleanup keeps on purpose). The container listing is one fixed `find` stream and is unchanged. No `[ae-patch]`.

Decision-1 preset advisor, host render fix (2026-11-06): the advisor line never reached a terminal-mode lead, because the host renders the notice with no `AE_DECISION_*` settings and `_advisor_line` asked `decision_advisor.available()` at render time. `_resolve_roles` (server side) now decides and stores `advisor: true`, `_advisor_line` reads only that flag, and the owner address no longer rides in the notice metadata (core patches `0295`, `0296`).

Social card, part 2 (2026-11-06): the card gets a Pop out button that opens a full view (Week, Month and
Scorecard tabs, a queue of unposted items, an editor with Copy, Save and copy, Open, Mark posted, Skip and
Reschedule, and a Settings fold for every rule and tool, each with a plain-words hint). The Scorecard adds
an 8-week LinkedIn chart, the went-public-without-a-post list, the tool rotation and the upload pace; the
server returns those in `score` and the worst-case size test covers them. The week that holds the first
YouTube read reports "First week" and never counts toward the streak. The Social tile joins the Ship pill
with a short brief. Core patch `0297` lets the host open LinkedIn's share box (with pre-filled text) and a
channel's Posts tab. New web files: `socialView.ts`, `calendar.ts`, `socialEditor.ts`, `socialScore.ts`,
`socialSettings.ts`, `socialWords.ts`. See `docs/features/admin-center.md`.

Social card, part 2, review round 1 (2026-11-06): `GET /social` now carries every open post up to 63 days overdue, not only those since last Monday; the "went public without a post" list dates a livestream by the upload's own public time; unsaved editor text is protected (an inline "Discard unsaved edits?" before switching posts, Close, Mark posted or Skip, and clicking the open post changes nothing); a second Save rules or Save tools in one session is compared with the last save; arrow keys move the tab stop; a live read keeps focus on the chip; Scorecard dates read on the rules' clock; chips wrap at word boundaries; the Move picker starts at the next whole hour on an overdue post; the expert post day is a weekday picker. No new `[ae-patch]`.

Decision-1 preset advisor, required first step (2026-11-06, core patch `0298`): the smoke test on `ae-v0.18.0.226` found 3 of 3 lead-picks leads ignoring the advisor line: it said "Before picking", sat after the general lead-picks sentence, and a lead that did no helper work felt no pick happened; in Claude Code `advise_preset` is also a deferred tool (`mcp__ae__advise_preset`) that must be loaded with ToolSearch first, which nothing said. The line now comes first in the block as "Step 1, for every task including small ones", names the ToolSearch query (Claude Code only; Codex calls the tool directly) and says to pick as before, and say so, when the tool is missing or fails. Still prompt-only, so a lead can skip it; to make skips countable, a lead-picks run line now carries `advisor_on` (true when the advisor was available for the owner), and `advisor: []` with `advisor_on: true` is a skipped call. Tested by `test_ae_roles.py` and `test_preset_runs.py`.

Decision-1 preset advisor, step 1 wording (2026-11-06, core patch `0299`): the summary the lead sends must now describe "the kind of work, not its subject", and the run-line `advisor_on` test drives a fresh session so a raising availability check is really exercised.

Social card on a phone (2026-11-06, the owner, part 3, no `[ae-patch]`): the full view is rebuilt for a phone (at most 560 px wide) after three complaints: the top buttons took three rows, one row per weekday left the screen empty, and Open was disabled on every post without a draft, so there was no way to test a post. The bar is two rows (tabs with a filter menu and a gear, then the range with Today); the Week tab is one agenda list (Overdue, Today, then each day with posts, 14 days from Monday); the Month tab is a grid of dots with the picked day's posts under it; an open post is its own screen with a back link and a Post on LinkedIn (Post on YouTube) bar pinned to the bottom. A post with no draft starts with plain starter text built from the post (`GET /social` rows now carry the upload's or the tool's `url`, and the page carries `channel_names` from `AE_SOCIAL_CHANNEL_NAMES`, which the box's private env file must set so the second channel's name stays out of this public repo; the worst-case size test covers both), and Post is never disabled for lack of text. Post, Copy and the compact card's Open start the clipboard write inside the tap, before any `await`, for iOS; the compact Open is never disabled and opens the editor when there is no draft or the full draft is not loaded. After a Post tap the page asks "Did you post it?" once when it comes back within 30 minutes. Desktop keeps its grid; Open LinkedIn and Open YouTube posts are now the primary Post on LinkedIn and Post on YouTube. Documented in `docs/features/admin-center.md`, Social card. Tests: `test/socialPhone.test.ts`, `test_social.py`.

Judge interface, decision points and `ask_judge` (2026-11-06, no core patch): the judge model retires 2027-02-04, so new code asks a `Judge` (`judge.py`: `yes_no`, `choice`, `score`; `Decision1Judge` and `RuleJudge`) and never names a model. `decision_shadow.py` adds decision points with an `off`, `shadow` or `live` mode per point (`AE_DECISION_POINTS` ceiling, optional promotions file), a timeout, daily and per-minute caps, a circuit breaker, a feature whitelist, and a 30-day shadow log of numbers and enum labels only; no point is wired in yet. `ask_judge` (route `POST /v1/ae/sessions/{id}/judge`, `agent_judge.py`) lets any session ask a guarded typed question, 20 an hour, and returns probabilities with a `low_confidence` flag; its shadow line keeps only `kind`, `n_options`, `n_facts` and `role`. Details in docs/features/decision-advisor.md.

Decision model card counts saved calls, and agents are told when to ask the judge (2026-11-11, core patch `0362`): Calls today read 0 after every deploy and never counted the lead's advisor calls, because it came from a count held in memory. It now reads a saved daily counter (`ae-decision-calls.json` beside the shadow log, `AE_DECISION_CALLS_FILE` to move it, memory only when there is no data directory) that adds one for every request that really goes to the Decision-1 endpoint, from an agent's `ask_judge` or from the lead's advisor, and rolls over at UTC midnight. A call refused before the request goes out (advisor off, rate limited, a refused summary, no presets, the daily or per-minute cap, an open breaker) is not counted; a timeout or error after the request went out is. A failed save keeps the in-memory count and logs a warning. The daily cap still counts agent calls from memory, and the response keeps its keys. Agents had called `ask_judge` once ever, so the lead's roles text now carries one line saying when to call it (picking between approaches, full or scoped tests, review now or fix more, ready to hand off), shown only when the server finds the judge available for the owner (the same answer as `GET /v1/ae/decision`) and folded into the roles line, so the block stays under its line budget. Tests: `test_decision_status.py`, `test_decision_shadow.py`, `test_decision_advisor.py`, `upstream/tests/inner/test_ae_roles.py`.

Decision-1 micro-decisions (2026-11-13, core patches `0370` and `0371`): agents almost never used `ask_judge` (3 shadow lines in total, all tests), so it is built for many small questions. One call now takes one to five questions (`questions`), each with an optional 10 to 160 character `context` line under the same guard and copy check as the question, never logged (the shadow line keeps `has_context` and the batch size only). Per-chat caps count calls: 120 an hour (was 20) and 10 a minute; the server-wide caps count questions: 35 a minute (70% of the deployment's 50 requests a minute; was 30) and 3000 a day (was 400). A 429 from the endpoint answers `rate_limited` with `retry_after_s`, holds every call for the Retry-After, and no longer counts toward the circuit breaker. The roles line (`0371`) is rewritten to ask for batches with context and now also reaches helper sessions; the new `ae_decision_nudge` policy adds one line at the start of a chat, after a test command and after a reviewer returns (`0370` lets the hook send it as context after a tool result), at most one per moment per 10 minutes and 6 an hour, none within 2 minutes of an `ask_judge` call, off with `AE_DECISION_POINTS=nudge:off`. It reads tool names only and logs the moment label. An existing server's policy store is not re-seeded, so the policy is added once through the Global Policies page or its API (the box's is added after the deploy). A policy added by hand lands last, so move it above Voice mode (`ae_voice_style` must stay last). `GET /v1/ae/decision/report?days=7` (admins only) answers counts and ratios from the shadow log, with no chat ids or text, and a refused or rate-limited call now writes one code-only shadow line so it can be counted. New settings: `AE_DECISION_SESSION_HOURLY`, `AE_DECISION_SESSION_MINUTE`, `AE_DECISION_SESSION_QUESTIONS_MINUTE` (20 questions a minute per chat); defaults of `AE_DECISION_MINUTE_CAP` and `AE_DECISION_DAILY_CAP` changed (`config/server.env.example`). Review round (2026-11-13): a batch is now guarded in full before any question is sent (one refused question sends nothing), counts as one failure toward the breaker and runs up to 8 at once; the 429 wait is enforced at the moment a request goes out; the nudge reads Codex `cmd` commands, does nothing in shadow mode, no longer resends the start line after a restart or on a system continuation; and the report counts per question, uses the logged `low_confidence` and streams the log.

Decision-1 report counts saved calls (2026-11-13, no core patch): `GET /v1/ae/decision/report` gave `calls_today` from the in-memory cap counter, which every deploy resets (it read 2 on ae-v0.18.0.293 while the Decision card read 6). It now reads the saved daily counter, the same one `GET /v1/ae/decision` reads. Tests: `test_decision_report.py`.

Judge unblock (2026-11-18, no core patch): `ask_judge` answered 30% of 56 questions in 7 days, and a lead with 4,300 messages always got `no_transcript`. The copy-check read now takes 100 message pages, 2,000 mirrors and 300 children inside one 8 second deadline, and a rate-limited or judge-off call no longer reads the transcript (one read at a time per session). Agent text may start a later sentence with a leading word (pick, where, use, retry and more), tech acronyms pass in capitals, an apostrophe inside a word is not a quote, and a key or level counts as a copy only at 4 or more words. A batch answers each question on its own (no more `batch_refused`), every refusal carries a fixed `hint`, the tool description states the accepted shape, and a `no_transcript` failure is logged as `refused_no_transcript`. `advise_preset` keeps its old rules. Tests: `test_agent_judge.py`.

Judge first word (2026-11-18, no core patch): after the unblock, "Rerun the slow suite now? it stalled on a network wait." was still refused as `proper_noun` only because "Rerun" is not a leading word. `_name_like` gets `opening_free`, which frees the first word of the whole text whatever it is; `ask_judge` sets it for the question and the context line only. A later sentence start still has to be a leading word, a name mid-sentence is still refused, a denylisted first word is still refused, and option descriptions, fact values, levels and keys keep the strict rule (a one-word "Fabrikam" stays refused). `advise_preset`, `check_text` and owner preset text do not change. Tests: `test_agent_judge.py`, `test_decision_advisor.py`.

omni_capacity answers a summary (2026-11-19, no core patch): by default the answer is slots used and free, who holds them, who is blocked and a count per other status; `detail: full` returns every session as before. The steward, shutdown and orchestrator agents ask for full. Usage limits are not part of that answer (they come from `GET /v1/ae/limits`).

Review nudge skips background launches (2026-11-18, no core patch): the `review` moment fired on the tool result of a `Reviewer:` or `Security:` agent call, but for a background reviewer (`run_in_background` true) that result is only the launch receipt, so the nudge landed before any findings and most of the 197 review nudges in a week were noise ([issue 587](https://github.com/analyticendeavors/omnigent-ae/issues/587)). A background launch now returns no moment and does not use up the 10 minute review slot; `report_review` still nudges, so background reviews get one nudge there. Tests: `test_decision_nudge.py`.

Decision-1 judge no longer pushed on agents (2026-11-19, core patch `0423`): a replay of real calls showed a per-choice `ask_judge` call costs about 146 output tokens plus an extra step re-reading context, while the model decides inline in about 28 tokens, with no better quality, so telling agents to call it for every small choice was a net token cost. The judge line is gone from the roles block the prompt hook injects and from the helper notice, and the `ae_decision_nudge` policy is no longer seeded and always abstains (an already-seeded row keeps loading; delete it on the Global Policies page). The `ask_judge` tool, `agent_judge.py`, the route, `decision_status` and the shadow log stay. Tests: `test_ae_roles.py`, `test_policy_seed.py`, `test_decision_nudge.py`.

Admin Center pills turn amber only for something to act on (2026-11-07): a Cost Management rate limit and an old Databricks reading no longer do (the notes still say them), and the Key Vault tile note leads with the secrets to check. Web extension only (`azure.ts`, `databricks.ts`, `vault.ts`).

Azure cost reading order (2026-11-11): each pass now reads the subscriptions in the order of when they were last read, the longest ago first (a subscription never read goes first, ties keep the configured order), instead of the configured order. Cost Management answered 429 after the second query-pair on the live server, and a 429 stops the pass, so the last two subscriptions had not been read for 11 hours while the first two refreshed hourly and three cost-target rules reported cannot-evaluate. A limit that admits only part of the list now rotates through all of it. Only the read order changed: readings, errors and the card stay in configured order. `packages/ae_omni_policies/src/ae_omni_policies/azure_cost.py`; tests in `test_azure_cost.py`. No `[ae-patch]`.

Admin Center Spend tile says "refreshing" instead of "refresh failing" while the server computes new numbers after an idle stretch (2026-11-07): `GET /v1/ae/presets` carries `refreshing: true` then, and the card polls every 4 s until the new answer lands.

Admin Center phone and desktop pass, one Review chat per source (2026-11-07, the owner, no core patch): on a phone the Needs you panel and the page were taller than they had to be, on a wide screen every group drew three columns whatever it held, and Box health, with one item in Cleanup scan and one in Failed units, had no Review all at all because `POST /v1/ae/attention/batch` took one kind line per call. The route now also takes `source` (exactly one of `line` and `source`, else 422): one chat on every item of a source across its lines, named by the source ("Review 2 items: Box health"), with the same eligible filter, holders, key, labels and slots-full answer. Needs you shows a "Review N" button on a source with two or more reviewable items ("Review 20 of N" over the cap of 20); a source with one group shows only that button. Phone: the header is one row with the two tools as 44 px icon buttons, the waiting line is a 44 px chip, sources sit on hairlines with a red or amber count badge, groups are subheadings, and a row's buttons stay on its title line. The page drops the 52 px top padding the host already reserves, pill briefs clamp to one line, a folded card is about 56 px (Social's pill and pop-out, and Box's host chip, hide while folded), and runs of folded cards sit two by two. Desktop: cards keep their natural height (Deploys 160 px, not 474), groups pack into rows first-fit (Ship 3, Box 1 beside Accounts 2, Cloud 3), unsaved cards go tallest first into the shortest column by fixed guessed heights (so they never move after render), a group always draws min(columns, cards) columns, the four status boxes are one height, and collapsed Needs you sources flow into a grid. Measured at 2,024 px the board is 2,122 px tall (2,890 before); the natural heights of Ship (491), Box (481) and Cloud (699) set the floor. Extension bundle and AE package only; the two bugs from the same review, the overlap and the dead waiting link, are a separate change.

Needs you row titles squeezed to one word per line (2026-11-13, the owner, no core patch): on Reid's iPhone a row's title was 36 px wide and broke one word per line ("Fix #557 Bump jsdom from..." ran 371 px tall). Root cause: the phone layout kept the row's actions on the title line in their own grid column, and since the status word (PR 514) and the Fix button (PR 461) joined them that column took its full width first, about 300 px of a 382 px row. The same column left the title 14 px in the 312 px Needs you tile of a 1,440 px desktop board. On a phone the title and description now span the row and the actions take their own line under them; wider than a phone the row wraps, so the actions stay right of the text while the text keeps 16em and drop under it otherwise. A merged row, which has no actions, gets no empty line under it. Measured on the live page with the new bundle: title 344 px at 440, 264 px at 360, 274 px at 1,440 and 390 px at 2,265, no row overflows. `packages/ae_omni_extension/web/src/style.css`; `docs/features/admin-center.md`.

Admin Center cards: turn any card on or off for the server, hide one for yourself, and keep the Social card out of public installs (2026-11-07, the owner, part 1 of 3, no core patch): a Cards panel (header button, a gear on each card, a footer button) lists every card with a switch, an eye and a Setup fold that names the environment variable behind each field and never shows a value. The switch is the server's (`GET /v1/ae/cards`, `POST /v1/ae/cards/state`, admins from a browser only; a stale revision answers 409), the eye is kept in `storage.user`. Off removes the card, its pill share and its Needs you lines and stops its background reads; a present key keeps its Box health row. The Social card exists only with `AE_PRIVATE_CARDS=social`, and its channel ids, tools and names moved out of `social.py` into `AE_SOCIAL_CHANNEL_IDS`, `AE_SOCIAL_TOOLS` and `AE_SOCIAL_CHANNEL_NAMES`. Server: `admin_cards.py`, `auth.require_browser_caller`. Web: `cardsPanel.ts`, `cardState.ts`, `cardPrefs.ts`, `cardsApi.ts`.

Social card: share sheet, picture and bold first line (2026-11-07, core patch 0303): `copyText` copies by selection first and synchronously (the clipboard API always rejects in the sandboxed frame, so the Open tap switched apps before the fallback ran). New host service `navigation.share` opens the phone's share sheet with text and one image; on a touch device the editor's LinkedIn button reads "Share to LinkedIn" and the compact card's Open uses the same path, asking "Did you post it?" at once, with the composer link as the fallback. The server fetches the post's picture (`GET /v1/ae/social/visual/{slot}/{part}`, `social_visual.py`; a public upload's thumbnail or a tool's new `image` link) and serves it in base64 chunks under the bridge cap. Line 1 can be set in sans-serif bold letters ("Bold line 1" chip, on for starter text), the meta row is one line, and `GET /social/week-redraft` (`social_relay week --redraft`) lists unedited auto drafts for a rewrite. Details in docs/features/admin-center.md, Social card.

Social card review fixes (2026-11-07, core patches 0304 and 0305): `navigation.share` gets a 10-minute timeout (a share the sheet held past 10 s was reported as a failure) and a `{probe: true}` form the card asks once when the full view opens, so the tap chooses the sheet or the composer link synchronously; a Timeout is an unknown outcome and asks "Did you post it?". The Share button waits up to 8 s for the picture ("Loading image...") and says when it shares the text only. The picture cache is keyed on the slot and the picture's identity (the video id, or the tool image link, now carried in the slot row's `visual.key`). The host rejects a share Blob over 5 MB or of another type and always names the file `image.jpg` or `image.png`. Bold line 1 skips #hashtags and @mentions. The meta line never truncates the count, and "Starter text" is a short tag on it. The redraft feed leaves out the topics of the slots it redrafts.

Social card: made pictures for tool and expert posts (2026-11-15, the owner, no core patch): an expert post had no picture and a tool post only the generic site card. The weekly drafter now uploads 4 or 5 ranked candidates per post (branded diagrams, generated pictures, a screenshot when the product's own page has a clean one) through `social_relay put-visuals` to the new `POST /v1/ae/social/visuals`; the server keeps the files in `/data/social-visuals/` (`social_made.py`) and the list and choice in the state, preselects the first, and `GET /social/week` names the posts still waiting in `needs_visuals`. The editor's picker shows the chosen picture with every candidate as a tile in rank order plus None (`POST /v1/ae/social/visual-choice`), and Share takes the pick. Tests: `test_social_made.py`, `test/socialPicker.test.ts`. Details in docs/features/admin-center.md, Social card.

Speak replies (2026-11-08, core patch 0313): a speaker button beside the composer's mic reads each reply in the chat aloud as it streams, using the browser's own speech engine, so nothing leaves the browser. It starts at the first finished sentence and skips code blocks, markdown marks and the TLDR recap. The button's menu has Fast answers (low effort while replies are spoken, restored afterwards), a Voice picker and a Speed choice kept per browser, Read last reply and Stop speaking. Speech stops on a send, a chat switch or turning it off, and a send with nothing spoken after six seconds says "Working on it." once. Off by default. Review fixes (core patch 0314): a final no longer repeats sentences already spoken from its preview, both messages of one response are spoken, a status change alone no longer cuts speech, and Fast answers restores the old effort only when the chat is still at low. Fix-check fixes (core patch 0315): a code block is announced once, a repeated reply such as "Done." is spoken again in a later turn, and a rebuilt message queue no longer counts as a send. Menu and voices (2026-11-09, core patch 0316): Speak replies and Fast answers are switch rows with their helper text behind the "i", Voice and Speed are sub-menus, and the Voice list offers natural cloud voices from Azure Speech (four, made by the server; `AE_SPEECH_REGION` plus the `AE_SPEECH_KEY` secret turn them on) ahead of the device's own, which still read any sentence the server cannot make. Review fixes (core patch 0317): one engine speaks a whole response, a failed voice list is retried, and the server caps each user at 200,000 characters a day.

Server dictation (2026-11-09): the image carries upstream's dictation extra, so the composer mic works in the desktop app once the models sit on the data volume (deploy/README.md, "Dictation").

Compact card follows the live context size (2026-11-07, core patch 0307): the card said "This chat is at 307k tokens" while the context popover said 726k. Root cause: the server sweep writes `ae.handoff_at` once, when the chat first crosses the threshold, and never updates it, and the web function `aeCompactSuggestion()` preferred that frozen label over the live `omnigent.last_context_tokens`. The card now reads the live label first, then `ae.handoff_at`, then shows "a lot of"; the server still uses `ae.handoff_at` as the snooze base, so snoozing is unchanged. Web only; no server change. Tested by `aeCompactSuggestion.test.ts`. `docs/features/action-dock.md`. Review round 1 (same day, patch 0308): a test for a live context label that is not a number, which falls back to `ae.handoff_at`.

Sidebar count pills match (2026-11-07, core patch 0309): on Reid's screen the Admin Center's "14" was larger text, off centre and pink while the Session Dashboard's "5/4" beside it was smaller, bold and red. Root cause: the Needs you pill's `text-10` class names a size this theme does not define, so it fell back to 13 px inside a 16 px pill, and its review count used the selected row's colours. Both pills now share `web/src/lib/aeCountPill.ts`: at least 19 px tall, the theme's small text size (`text-xs`, 11.7 px at the default desktop Appearance size) in semibold, text centred, solid red only for something to act on now and a grey tint for a plain count. The review (core patch 0310) found the first tints under AA on the selected row's pink wash (grey text 3.3:1, red 3.5:1 in light, 3.9:1 in dark), so the red is solid with white text (the dark theme's background colour in dark) and the grey count's text is the foreground colour at 80% instead of the muted one; measured on the live page every pill now reads at 5.2:1 or more. The pill's height is a minimum, so a large Appearance size grows it. The Inbox count is upstream's own and is left as it is.

Needs you check at zero (2026-11-10, the owner, core patch 0353): the sidebar badge on the Admin Center row drew nothing when nothing needed the owner, which looked the same as a badge that had not loaded. At a true zero (an answer arrived, at least one source, every source read) it now shows a green check; its tooltip and label say "Nothing needs you". A source that could not be read still gives "?", and a failed route still gives a lone "?": the check never covers a broken read. The compact rail form shows a small check. No answer yet, or a viewer who may see no source, still draws nothing. `docs/features/admin-center.md`.

Admin Center card settings typed in the panel (2026-11-07, card setup PR 2; no `[ae-patch]`): PR 1 showed each card's setup but a new install still had to edit `config/server.env` and restart. The Cards panel now has an input for the plain settings: the Git lists, the Databricks credit, the Azure subscription list and the Fabric price. `POST /v1/ae/cards/settings` (admins, browser only, like the switch) validates each value and saves it in the card state file under the same revision (`admin_cards.py`, `card_config.py`). The environment always wins: `card_config.value()` returns a set, non-blank variable first and the saved value only when the environment has none, so a hand-configured server changes nothing. Every field was audited: only text, list, number, date and bool kinds whose every reader runs in the server process are editable; URLs, hosts, endpoints, the tenant, client ids, secrets, host-relay settings, Social's JSON, and the start-time switches stay in the environment. A save drops the card's cached answer, and a reading already in flight when the setting is saved is discarded when it ends, so it cannot write the old setting back (Azure, Databricks, Git); a rate-limit wait is kept. The live check of PR 1 showed prefix fields as "Not set"; that was correct, because the live server holds no `AE_GIT_TOKEN_<OWNER>` variable and reads its GitHub accounts with `AE_GITHUB_TOKEN`. A prefix field now says "Set for 3 accounts" when one or more variables exist (a count, never names or values) and "Not set" when none do. An optional field with a default now says "Default: <value>". Tested by `test_card_settings.py` and `cardsSettings.test.ts`. `docs/features/admin-center.md`.

Faster chat switching (2026-11-07, core patch 0311): a Firefox profile of the live app showed the main thread about 75% busy, with about 3.8 s of a 27 s recording in the query cache's notify fan-out. Root cause: four hooks (the Session Dashboard count, the session header chip, the rail's labels and row, and the effort default) subscribed to every query-cache event and re-scanned `["conversations"]` on each one, and a session switch fires about 60 fetches and hundreds of events. They now subscribe through `web/src/lib/aeQueryCacheSubscribe.ts`, which wakes only when a query under a key the hook reads is added, removed or updated. Separately, `lib/telemetry.ts` imported OpenTelemetry (and zone.js, which wraps every timer and event listener, about 1 s of CPU) on every page; the setup now lives in `lib/telemetryOtel.ts` and loads only when `VITE_OTEL_EXPORTER_OTLP_ENDPOINT` is set, so with tracing on, requests made before that import resolves are not traced. Tested by `aeQueryCacheSubscribe.test.ts` and a repaint case for each hook. Fork branch `analyticendeavors/omnigent@ae/perf-fast-session-switch`, after `0310`.

Admin Center packed board (2026-11-07, the owner, no core patch): at 16:9 and 4:3 the page wasted a lot of room ("UI still seems to waste a ton here"): the group sections left holes and an empty half beside Needs you. From two columns up the sections are gone. Needs you is the first tile of column 1 and each card goes into the shortest column by its guessed height, with a small group tag; a fourth column starts at 1,720 px. The saved arrangement is one per column count (2, 3, 4) for the whole board under `board.packed`; the old per-group `board.layout` is not read, so a saved layout is dropped until the next drag. A status pill now scrolls to the group's first card and rings the group's cards. The phone is unchanged. Live check (read only, real data): 2265x1274 gives 4 columns and the board ends at 1,642 px (2,189 before); 1700x1275 gives 3 columns and 2,209 px (2,391 before). `packages/ae_omni_extension/web/src/board.ts`, `boardLayout.ts`, `boardDrag.ts`, `cardNav.ts`, `style.css`; PR [#503](https://github.com/analyticendeavors/omnigent-ae/pull/504).

Cards panel switch works from a browser and looks like a switch (2026-11-07, card setup follow-up; no `[ae-patch]`): Reid's first real flip, from his iPhone at 23:47Z on 2026-11-07, was refused with "the cookie holds a machine token, not a browser session". Root cause: `require_browser_caller` (`auth.py`) read the session cookie secret only as text, but upstream's `OIDCConfig` and `AccountsConfig` both hold it as bytes, so on a real server the gate found no secret and refused every browser, desktop included, for the switch and for the setting saves (PR 2). The tests had used a text secret and passed; the live check proved the page sends `Sec-Fetch-Site: same-origin` but never flipped a switch with a real cookie. `_cookie_secret` now accepts bytes, and `test_a_browser_session_on_a_real_oidc_server_passes` builds upstream's own `OIDCConfig`, `UnifiedAuthProvider` and `mint_session_cookie` (it fails on the old code with Reid's exact message). Device-login, refreshed, scoped, wrongly signed and bearer calls are still refused; the runner-token gap is unchanged. The security review found a second gap the fix opens (before it, the gate refused everything): the token `omnigent login` stores straight from a sign-in has the same claims as a browser cookie, so an agent that reads it from `~/.omnigent/auth_tokens.json` and sends it as the cookie passes as that admin. Reid chose to ship the fix with the gap named in the gate's docstring and the Admin Center doc, and to close it with an upstream patch that marks CLI sign-in tokens, in its own pull request. The switch itself (Reid: "not quite obvious that it's a left-right toggle switch") is now a filled 44x24 track, grey when off and green when on, with an 18px knob that travels end to end and the word On or Off beside it; on a touch screen the button is 44px tall while the track keeps its size (it used to grow into a near-square 52x44 with the knob stopping in the middle). The knob takes the page background colour, because a white knob on the dark theme's green reaches only 1.7:1; every part of the switch is at least 4.6:1 against what surrounds it in both themes. `cardsPanel.ts`, `cardsPanel.css`, `cardsPanel.test.ts`, `test_admin_cards.py`.

Admin Center Git company and personal lists (2026-11-08, card setup PR 3, https://github.com/analyticendeavors/omnigent-ae/pull/508; no `[ae-patch]`): the Git card read one company list with tokens set only in the server's environment. Each OmniGent admin can now keep a personal list of their own repos under their own GitHub accounts, and company tokens can be typed in the Cards panel. The panel's Git entry shows each company account's token source (environment, saved with Replace and Clear, or Add), the count of other admins' personal accounts with "Clear their tokens" behind a confirm, and "My GitHub accounts" (Connect another account, Check, tick repos, "Save N repos"); the token lives only in a password box until the save answers, then it is wiped, and `gitSetupApi.ts` cuts it out of any error text. The Git card gains a Company, Mine, All switch (remembered per viewer in `storage.user` key `git.scope`; All sends no query, so an older server still answers) and a "Mine" tag; Needs you gains the `git-mine` source, "My Git repos", where `actionsOf` keeps only Dismiss (plus Open on GitHub) whatever the answer lists, and Review all skips them, because Fix, Review and Merge act as the box's own GitHub login. Without `AE_CARD_SECRETS_KEY` the inputs are replaced by one sentence naming it. Expiry within 14 days shows as a warning; inputs and buttons are 44px tall on a touch screen. The shared line-table fixture `attention-lines.json` gained `git-mine`, a copy of the Git lines. Tested by `gitSetup.test.ts`, `gitScope.test.ts` and `attention.test.ts`; `cardsSettings.test.ts` now uses the Deploys card for its generic fields, because the Git card now reads its own personal routes when opened. Left open: the server process and its environment expose every saved token, a token's write permissions cannot be checked, and issue 506 (a token from `omnigent login` in the cookie slot passes the browser-only gate). `docs/features/admin-center.md`.

Home security alerts in Needs you (2026-11-13, the owner, no core patch): the Home security card's open alerts are now rows in Needs you, a new source "Home security" in two lines, Firewalla alarms and Sentinel alerts. Each visible alert (the home app's own hiding rule, newest first, at most 20, a longer list is unknown) is one row; a critical Firewalla alarm is urgent, everything else is review. The row's one chat button is Investigate (the wrench icon): a read-only chat that reads the firewall's own record of the alert, checks the process behind it and posts a real-threat or false-alarm verdict with one recommended action; it archives, mutes and blocks nothing. A high-effort Opus chat for Firewalla alarms, Sonnet for the rest. Alert text is network-written, so it reaches the chat only inside one fenced block and never the title or label. Dismiss comes back when the alert turns critical. `GET /v1/ae/home-security` still carries no alert text. A new env-only setting, `AE_HOME_SECURITY_RUNBOOK`, names a file on the box the chat reads first; file permissions on it are the owner's job. The card's board title is now "Home security · Firewalla". Details: `docs/features/admin-center.md`.

Investigate chats propose one action (2026-11-14, the owner, no core patch): the Home security Investigate chat may now propose its one recommended action with a fixed command that sends only the alert id and one word (archive, block or reviewed). Nothing runs until the owner approves the proposal in the home app's Security tab; the rest of the chat stays read only. Mute is suggested as text only. Details: `docs/features/admin-center.md`.

Admin Center redesign, phase 1 (2026-11-14, Reid's pick of concept D2; web extension only, no core patch and no server change): the packed board is replaced by a tabbed page. A header (title, verdict, refresh) sits over tabs: Overview, one tab for each area that has a connected card (Ship, Box, Cloud, Accounts) and Services. The Overview holds one grouped Needs you table (area, then source, then rows; What, Checked and Action columns; a composition bar; rows that open in place into one panel with the evidence, the facts, the actions and a hint) and a health rail at its right (500 px from 1,700 px of page; four area chips above the table under that). An area tab shows that area's rows and its cards in a grid; Services is the Cards panel as a page, and a card's gear opens it at that card. A server with four connected cards or fewer gets no area tabs. Every box now differs in color from the box it sits in (a surface ramp for the dark and light themes, scoped to the Admin Center). Hide for me now hides only the card: its items stay in Needs you and in the verdict, and a viewer who hid cards sees a one-time note. The status pills, the drag grips, Reset layout and the packed board are gone (`boardLayout.ts`, `boardDrag.ts` deleted). Review round, 2026-11-15: a phone's opened item is a full-screen sheet attached to the page body (not a modal dialog), so it fills the screen even after the page scrolls and the confirmations and the toast stack on top of it; an area holding a source that could not be read shows a question mark (never the green check) and says "Could not read ..." instead of "Nothing needs you"; every count comes from the server's `by_source` totals, with "N more not shown" when rows were cut; there is no Age column (items carry no time); the Services panel keeps its state across tab changes and the first layout waits for the cards' first reads; 44 px targets on any coarse pointer; every area and source opens by default (5 rows each) and folds only when the list would overflow; Services is a wide one-row-per-card page; area cards fill the width; a quiet day shows the areas as large tiles. Left for later phases: checkboxes and bulk actions, Snooze, Assign, a history box, Done today, Coming up, the Activity tab. New web files `adminShell.ts`, `needsTable.ts`, `needsModel.ts`, `rail.ts`. `docs/features/admin-center.md`.

Admin Center redesign, phase 1, round 5 (2026-11-15, Reid picked option B, area columns; web extension only): on a table container 1,800 px wide or more the Overview splits its areas into clamp(floor(width / 900), 1, 3) columns, each its own table section with its own header row, under the full-width header, toolbar, composition bar and banners. Areas go in today's order into the column with the fewest lines so far, so opening a row or a same-count poll moves nothing; the fold estimate uses the tallest column; DOM and Tab order run column by column. Area tabs, narrow pages and the phone stay one table. Labelled row buttons are 10 px padded on each side.

Browser-only gate refuses command-line sign-in tokens (2026-11-08, issue 506, https://github.com/analyticendeavors/omnigent-ae/issues/506; https://github.com/analyticendeavors/omnigent-ae/pull/509; no `[ae-patch]`): upstream mints one plain session token for browsers and command lines alike, so `require_browser_caller` could not tell the token `omnigent login` stores in `~/.omnigent/auth_tokens.json` from a browser's cookie, and an agent could replay it as the cookie with forged `Sec-Fetch-Site` and `Origin` headers. Root cause: nothing in a browser's credential proved it was a browser. New `browser_proof.py` adds a second cookie, `__Host-ae_browser_proof` (`ae_browser_proof` on plain HTTP; HttpOnly, SameSite=Strict, the session's Max-Age), holding HMAC(K, the exact session token) with K derived from the cookie secret. It is set only on a response that itself sets the session cookie: `GET /auth/callback` (redirect and ticket page), and in accounts mode `POST /auth/login`, `/auth/register` and `/auth/setup` when the request was same-origin and the answer holds no refresh token. Never on the magic-link redeem, page-load re-sends, `/auth/cli-poll` or `/auth/native-token`; sign-out expires it. The gate requires a proof that matches the cookie it came with (constant-time compare, refused when the secret is missing) and answers "This change needs a browser sign-in on this device. Sign out and sign in again, then retry." Accounts mode also guards `POST /auth/invite` and `POST /auth/users/{id}/reset` with the same rules, because an admin's command-line or bootstrap token could otherwise invite an admin or reset its own password and then sign in with forged headers. After the deploy each browser signs out and in once. The iOS app 0.3.1 and the desktop app hold tokens with no proof and lose card switches and GitHub token saves; a "Sign in again on this device" button was planned but is not built, because extension pages run in a frame with `sandbox="allow-scripts"` and cannot navigate the top page. Stays open: an agent reading the browser's cookie store or driving the real browser, `docker exec` on the box, cross-site scripting in the web UI, and admin writes not behind this gate. Tested by `test_browser_proof.py` (real OIDC and accounts routers, the real `/v1/ae/cards/state` and `/v1/ae/git/mine/check` answering only 403 or 422 to `{}`); the fixtures in `test_admin_cards.py` and `test_git_routes.py` now send the proof. `docs/features/admin-center.md`.

Desktop app sign-in passes the browser-only gate (2026-11-09, follow-up to PR 509; https://github.com/analyticendeavors/omnigent-ae/pull/518; no `[ae-patch]`): PR 509 made `require_browser_caller` require a browser proof cookie, so the desktop app, the owner's only client, lost card switches and GitHub token saves. Root cause: it signs in through `POST /auth/native-token`, receives the session in a JSON body and installs its own cookie, so it can never hold the proof, and every renewal after that carries `grant_id`, which the gate refused. New `desktop_signin.py` records that one sign-in: only when routing matched upstream's own `native_token` function (never a path suffix), the answer is 200, and the request is a form body under 16 KiB with exactly one `redirect_uri` that is a loopback address, it stores keyed hashes of the session token, of its login grant (found through the grant store by the refresh token's hash) and of the user, in `/data/ae-desktop-signins.json` (mode 0600, no token in it, at most 8 per user, pruned by expiry). The gate accepts a token without `grant_id` when its hash is recorded and unexpired, and a renewal when its grant is recorded and `act.client_id` is the login grant client; any `scope` is refused. CLI ticket tokens, CLI refresh-grant renewals, bootstrap, runner and device tokens are never recorded and stay refused; upstream already refuses a revoked grant's tokens. A damaged file or failed write fails closed. OIDC mode only. `AE_DESKTOP_SIGNIN_REAUTH=1` (off) adds `reauth=1` to the desktop's `/auth/login` request; it needs `auth_time` in the identity provider's ID tokens first. After the deploy, sign out and in once in the desktop app. Stays open: an agent on the owner's PC that runs its own loopback sign-in through the system browser, an agent reading the app's credential file, and a ticket and a desktop sign-in minted in the same second (identical tokens). Tested by `test_desktop_signin.py` (real OIDC router, `/oauth/token` and `/oauth/revoke`, a real grant store); every `test_browser_proof.py` case still passes. `docs/features/admin-center.md`.

Desktop sign-in review fixes (2026-11-09, review of PR 518): a sign-in is recorded only when its answer carries a refresh token that maps to a login grant of the same user (no grant, no record; with no grant store reachable nothing is installed). The gate checks on every gated write, without the 30 s credential cache and off the event loop, that the recorded grant is still live, so signing out in the app (`/oauth/revoke`) refuses the first native token and every renewal at once; before, the first token stayed valid until its own expiry. Behind a configured base path upstream copies the request scope, which hid the matched endpoint, so the exact path base path + `/auth/native-token` now decides (never a suffix), and an unrecorded 200 from a path ending that way is logged once. The re-authentication switch matches only the exact `/auth/login` path and only a loopback redirect. Smaller: the store prunes grant-less entries on expiry alone, a stat error after a good write still counts as saved, the grant lifetime is read once, and a held response start is sent before any other non-body message. Tested in `test_desktop_signin.py`.

Voice mode, one switch for hands-free talking (2026-11-09, patch 0320): tapping the speaker button turns on a loop where the mic listens, a finished sentence plus 0.6 s sends, the mic is muted until the reply has been spoken, and a pending question card is read aloud and answered by voice; a caret beside the speaker opens the existing menu, which gains a Voice mode switch. The switch writes the new `ae.voice` label through `POST /v1/ae/sessions/{id}/voice`, and the `voice_style` policy adds a one-line spoken-style request to each message while it is on. Off by default, per chat.

Voice mode review fixes (2026-11-09, patch 0321, review of patch 0320): a chat switch no longer sends the other chat's draft; the mic start is cancellable and retried once, and the speaker says "Mic unavailable" when it cannot start; sound heard while muted never reaches the draft; the send mute follows the chat's own status; spoken answers are matched more strictly (short number answers only, negation, "ok done"); the browser switch and the `ae.voice` label are kept in step; a tap on the speaker silences the rest of a reply; the `voice_style` policy keeps the shape of non-dict requests and is seeded last in `config/server.yaml`. A server that already holds the other AE policies does not re-run the seed: add `voice_style` once on the Policies page there.

Voice mode fix-check fixes (2026-11-09, patch 0322, fix check of patch 0321): a mic start turned away by a take that was still finishing no longer leaves Voice mode stuck on "Starting the mic"; a start that stays silent for 5 s counts as failed; a stale cancel flag no longer makes the next click on the mic button start a stream that is dropped at once; "merge it done" picks an option called "Merge it"; one stray formatting change in the chat page is reverted.

Voice mode second fix-check fixes (2026-11-09, patch 0323): a slow first mic start (the speech model loads for up to about 40 s) is no longer treated as a failure, and a multi-pick spoken answer that ends in "I'm done" keeps every pick.

Speak replies menu: pick the microphone and the speaker (2026-11-09, patch 0325): two new rows under Speed list System default and every audio input or output device. The microphone choice is used by server dictation and the level meter; the speaker choice routes the Natural (cloud) voices, and This device voices stay on the system default. Both are saved per device and fall back to System default when the saved device is gone; a rejected device retries once on the default. The menu was also tightened: it is narrower, each hint icon sits beside its label with the switch or value at the right edge, and Read last reply and Stop speaking sit side by side.

Picker review fixes (2026-11-09, patch 0327): devices listed before microphone permission no longer show a duplicate default or hide the saved microphone, long device names no longer run over the hint icon, the devicechange listener is removed with its last subscriber, overlapping device lists keep the newest, and the saved ids live in one shared store.

Picker polish (2026-11-09, patch 0328): the Microphone and Speaker rows show the device model (the part in brackets of a Windows name) with the full name in the tooltip and sub-menu, a saved device ticks no radio before microphone permission and System default clears it, saved-choice readers no longer hold the devicechange listener, and the first device listing starts once.

Picker CI fixes (2026-11-09, patch 0329): the picker test device lists are typed for the full type-check, and the speech rate-limit test freezes the bucket clock so a slow runner cannot refill it.

Send snips (2026-11-10, patch 0334): a new Send snips switch in the speaker menu (per device, off by default). With Voice mode and the switch both on, a picture you attach gets a spoken "Got it." and goes with your next sentence, or alone as "Note this." after 10 seconds of quiet. Only pictures count; nothing is read from the clipboard except in a paste event, and nothing is uploaded until the send. The Windows clipboard watcher is a later part.

Send snips review fixes (2026-11-10, patch 0335, review of patch 0334): a picture restored into the composer (switching chats, a failed send, a recalled queued message) no longer gets a spoken "Got it." or an automatic "Note this." send; only a picture you attach counts. A "Note this." with no picture left is no longer sent, and one that could not be sent is cleared from the draft.

Voice mode spoken style reaches native chats (2026-11-10, patch 0336): the `ae_voice_style` line never reached a native chat. The native hook now returns text a policy appended to the prompt as UserPromptSubmit `additionalContext`. The line is reworded to say the reply is heard, never read, and to override every formatting rule. On a phone the speaker menu shows "Default" in its row values so they are not cut off.

Voice mode spoken style review fixes (2026-11-10, patch 0337, review of patch 0336): for a message typed in the web composer the server evaluates policies at ingest and used to drop the rewritten text, and it answered the native hook's request-phase call with a bare ALLOW, so the hook had nothing to return. The ingest now keeps the appended part for the session, the hook route hands it back as the rewritten message, and the hook returns it as `additionalContext`. The menu caret gets a real 44 pixel box on touch screens instead of an overlapping tap area, and an empty prompt never injects context.

Voice mode spoken style fix-check fixes (2026-11-10, patch 0338): two quick web messages each get the spoken-style line, and a line left behind after Voice mode is turned off no longer rides on a later message.

Voice mode spoken style relay fix (2026-11-10, patch 0342): the Claude native bridge's relay answered every message with an empty body because it did not pass the prompt to the converter, so the spoken-style line never reached native Claude chats; it now returns it as `additionalContext`, and the Kimi and Devin hooks do the same.

Voice mode shows its state (2026-11-10, patch `0347`): a panel above the message box says what Voice mode is doing: a bar on the desktop, and on a phone a full sheet that replaces the message box (with a key to type one message instead). It reads Listening with what it heard, Working on it, Speaking with the sentence being said, or Tap to hear the reply, with a live mic level, and the box border takes the state colour. The speaker button's small word is gone. The speaker button's tap now unlocks audio for later replies, and a reply the browser refuses to play (iOS does this) no longer falls back silently: the panel turns red and one tap plays it.

Voice button on the new-chat page (2026-11-10, patch `0348`): tap Voice mode beside the mic on the new-chat page and talk; your first sentence starts the chat after a short pause, and the chat opens with Voice mode on so the reply is spoken. Tap the button again to cancel.

Voice log (2026-11-10, patch `0349`): the speaker menu has a new row, Add voice log to message. It puts the last two minutes of voice events (mic, sentences, replies, browser errors) into the message box so you can send them to the agent when voice misbehaves on a phone. Nothing leaves the device until you send.

Voice mode fixes (2026-11-10, patch `0350`): a chat started by voice now always opens with Voice mode on, and a failed start no longer leaves it armed. When the mic stops, tapping the panel listens again. On a phone the action row (Interrupt, attach, speaker menu) stays reachable under the sheet. Ending Voice mode clears a reply the browser refused to play. The voice log goes above a slash command instead of becoming its arguments.

create_task asks for a quadrant and an effort (2026-11-10): the `ae` MCP `create_task` tool now requires `quadrant` and `est`; left out, tasks had landed in Q4 with no estimate (ten of them). `quadrant` is `hotfix`, `feature`, `admin` or `neither` (urgent 0, important 0, so Q4 stays a choice). `est` must map to the store's 15, 30, 45 or 60plus: those values, `minutes`, or a size in words (`about an hour`, `a block`, `multi-block`); an `est` that maps to none is refused with the accepted list instead of being dropped. The tool, the `POST /v1/ae/lot/create` route and the store client all refuse a call missing either, before any request. `park_fork` is unchanged. No core patch. The route's only callers are the MCP tool and tests (no Admin Center code uses it), so it is required there too. Internal task creators (model watch, secret alerts, cost targets) post to the store directly and are untouched.

Voice mode listens longer and can listen while it speaks (2026-11-10, patch `0354`): the pause before a finished sentence is sent is now 2 s (was 0.6 s; a new word cancels it and the wait restarts after the next finished sentence; a spoken card answer waits the same 2 s). A new Mic while speaking switch in the speaker menu, saved per device and off by default, opens the mic once audio really plays instead of waiting until the reply ends; thinking, before anything is spoken, stays muted either way, and with the switch off the mic never interrupts. The reply's own voice is not taken for yours: only a run of 3 or more heard words that matches the spoken text is echo, so a real word that also appears in the reply stays, and the reply stops only when at least two new words arrive in a final result or in two interim results in a row. Ctrl+Alt+V (Cmd+Opt+V on a Mac) while Voice mode is on and a reply is playing stops the reply, clears a pending send and leaves the mic listening; in any other state it toggles the mic as before.

Voice mode Fast model (2026-11-11, patch `0355`): a new Fast model row in the speaker menu (Claude chats only) switches the chat to the newest Sonnet while Voice mode is on and puts the chat's own model back when Voice mode ends. A model you pick by hand during voice stays, the old model is saved on the device before the switch so a reload or crash still restores it, and Off keeps the chat's model. Fast answers (low effort) is unchanged. Read last reply now unlocks audio inside the tap and writes a voice log line naming the voice and speed it used.

Voice mode fixes (2026-11-11, patch `0356`): Read last reply now uses a selected cloud voice such as Rocky even before the voice list has loaded or when it failed to load (it used to fall back to the device voice), and unlocks audio after it stops the current reply. The Fast model restore retries once when a switch back is refused and then shows a message, survives turning Voice mode off and on again quickly, no longer drops its saved state on a reload before the chat has loaded, follows a new Fast model pick made while Voice mode is on, and is left alone by a second browser tab that did not make the switch.

Voice mode mic fix (2026-11-11, patch `0357`): on a phone with Bluetooth audio, Voice mode no longer sits on Starting the mic for a minute or more after a spoken reply. A recognizer that ends before it starts, or a start that answers nothing for 4 seconds, is reported as a failed start at once so Voice mode retries, and the composer mic button is no longer left stuck. After 3 seconds on Starting the mic the panel shows Tap to start the mic, which restarts the mic inside your tap. The level bars wake a suspended or interrupted audio context and take a fresh stream when the mic route changes, and the cloud voice player lets go of its audio clip once a reply finishes.

Voice mode mic fixes (2026-11-11, patch `0358`): a stale end or error from a mic start that Voice mode had already given up on can no longer cancel the next start's timer, and a stuck start is reported once, so Voice mode no longer flashes paused while it retries. Tap to start the mic now starts a fresh recognizer inside your tap (Safari refuses to start one that is still shutting down), and shows only for the browser mic, never for the server voice path, where a slow first start is a model loading and the tap no longer cancels it. The give-up timer waits 8 seconds for the first start on a page and 4 after that, and stays off while the microphone permission prompt may be open or the permission is not granted. The level bars get their retry allowance back after 30 seconds of steady audio and retry once when the mic is busy.

Voice mode mic fix (2026-11-11, patch `0359`): on the new-chat screen, Tap to start the mic now starts a fresh recognizer, as it does inside a chat, so on an iPhone a start that is stuck no longer swallows the tap.

Voice mode approval and deaf mic fixes (2026-11-11, patch `0360`): when the session limit (or any plain approve or decline card) is waiting, Voice mode now says so, reading "Slots full. Say run it, or decline.", keeps the mic open and shows Waiting for your OK; saying run it, yes or ok approves, and decline, no or cancel declines. Before, the card was only in the chat view, so the mic stayed muted on Working on it and nothing was spoken. A refused send is now written to the voice log with its reason. On the new-chat page, a spoken message that cannot be sent yet (the host or folder is still loading) is held for up to 10 s with Getting the chat ready, sent the moment the chat is ready, and otherwise reported as Chat not ready instead of dropped. The composer mic gets two watchdogs: a stop that gets no end within 1.5 s replaces the recognizer, and a Voice mode take that says Listening but hears nothing (no audiostart in 3 s, a muted or stalled meter, or an all-zero meter for 6 s with no sound heard by the recognizer) is replaced and retried once, then shows Mic unavailable with Mic went quiet, tap to restart. Listen again while the mic claims to be listening now builds a fresh recognizer instead of doing nothing.

Voice mode approval and mic fixes after review (2026-11-11, patch `0361`): only the session-limit card is answered by voice now; a tool-permission card (a shell command, a file write, a web fetch) always needs a tap, so a spoken yes can never approve one blind. The spoken answer must be exactly run it, yes, yeah, okay, approve or go ahead to accept, or decline, no, cancel, don't run it or stop to decline; a longer sentence, a negation or run it or decline does nothing. Sound from the approval line itself, and anything heard in the 2.5 s after it ends, is ignored, and a card takes one answer. A mic is called deaf only when the speech recognizer itself has shown no life for 6 s as well as the meter looking dead, so a healthy take with an odd meter is left alone; a slow Bluetooth switch gets 5 s. A stop that gets no end waits 4 s, and a late final from the old recognizer is still delivered. Mic went quiet now outranks Waiting for your OK, with Listen again, and a tap gives a fresh retry. On the new-chat page a deaf take retries once, and a message held as Chat not ready is sent as soon as the chat is ready.

Voice speed (2026-11-12, patch `0363`): Rocky's cloud voice makes a sentence in 4 to 13 s, about three times slower than real time, while the Azure voices take under a second, so speech now starts sooner. The first chunk of each reply is a short phrase and is never merged with later sentences; a first sentence over 10 words is cut at its first comma, semicolon, colon or dash after about 6 words (whole if there is none), and only text already received is split. The Working on it. cue is spoken by the device voice when a cloud voice is selected, and the real reply cuts it. A new per-device Voice chat voice setting in the voice menu (default: the first non-Rocky cloud voice, else the device voice; options Same as Speak replies and every voice) speaks replies, Read last reply and the approval line while Voice mode is on; outside Voice mode the Voice picker applies unchanged. Speed gains Fastest 1.5x below Faster 1.25x for cloud and device voices; `/v1/ae/tts` and the Rocky bridge now accept rate 1.5 (they rejected anything but 1 and 1.25), Azure gets a +50% prosody, and `GET /v1/ae/tts/voices` lists each voice's `provider`. A stored speed not in the list reads as Normal.

Voice speed fixes (2026-11-12, patch `0364`): a dash splits the first phrase only with a space on both sides, so a range like 2024–2026 and a word like well-known stay whole. The per-response voice lock is dropped when Voice mode turns on or off, so the next sentence uses the voice that now applies instead of the Voice chat voice. A server that answers with no cloud voices no longer keeps a stale saved Voice chat voice, a saved device voice that is no longer installed falls to the default, and a voice with no provider field and the Rocky id counts as Rocky. The Working on it. cue is forgotten when it finishes on its own, so the next reply does not stop the device speaker for nothing. A test pins Rocky Fastest at 2.25 times its own natural speed.

Voice ring (2026-11-12, patch `0365`): the Voice sheet circle on a phone now has a ring drawn around it. Teal bars rise from the circle edge and turn slowly while you talk, following the mic's frequency bins that the composer meter already reads (no second mic stream). An amber liquid ring ripples around the circle while Rocky talks, driven by a smooth synthetic envelope that gets a small bump on each new sentence; the reply audio is not routed through an audio context, so playback is unchanged. The ring sits quiet and dim in every other state. It draws on a canvas that takes no space and no taps, stops while the page is hidden or Voice mode is off, and with reduced motion on it is a still circle whose brightness shows who is talking. The sheet circle also shows amber instead of blue while Rocky speaks, and the gap above the circle grew by 8 px to give the bars room. Teal and amber come from two new theme colours with a dark value.

Voice ring fixes (2026-11-12, patch `0366`): with reduced motion on, the still ring now draws once for each mode, theme or screen-density change instead of on every mic frame, so it no longer pulses. It redraws when the dark class or the system colour scheme changes, and its colours are read once per theme change. The painted ring is a tenth smaller so it stays inside the gap around the circle and never touches the label or the caption, and the canvas sits behind the circle and text. The sheet's speaking label, disc and ring share one amber colour, darker in light mode so the label is readable. The canvas resizes when the device pixel ratio changes, for example when the window moves to another screen.

Voice mic stall fix (2026-11-13, patch `0368`): on an iPhone the mic could hear one word, then go deaf while the panel still said Listening. Root cause (likely, not proven by a log): WebKit's continuous recognizer stops delivering results after its first final but stays started, so the earlier liveness checks, which look for a dead recognizer, never fired. Two fixes, both silent to Voice mode. On Safari and every iOS browser the recognizer is replaced the moment a final arrives, so it never has to deliver a second one. On any browser, a Voice mode take that heard about 0.6 s of real speech on the mic meter, then 2.5 s of quiet with no result since, gets a fresh recognizer in place, with no "Starting the mic" flash and the text already delivered kept. After 3 such restarts in a minute the take is called deaf and the existing "Mic went quiet, tap to restart" shows. The voice log now records `mic result`, `mic recycle after final` and `mic stalled`.

Voice mic stall fix, review round (2026-11-13, patch `0369`): a slow final no longer lost to the restart (the old recognizer is stopped and its late final delivered once; the quiet window is 3.5 s and counts from audioend or speechend). The stall detector ignores the assistant's own voice and the 1.5 s after it, resets on a hidden tab, and treats steady loud noise as room noise (the floor rises). The restart budget resets on each new take and on a mic tap. A replacement recognizer that ends or errors before it starts is reported as a deaf take, so Voice mode retries rather than reading a manual pause.

Voice log upload and a Voice menu on the new-chat page (2026-11-16, patch `0377`): when Voice mode hits trouble, the browser now sends its last couple of minutes of voice events to the server, so a phone problem can be read afterwards without the phone. It sends when the mic fails to start, goes deaf, never answers a start, or stops on its own, and when Voice mode ends, in a chat and on the new-chat page. A new signed-in route, `POST /v1/ae/voice-log`, takes a reason from a fixed list (`mic_failed`, `mic_deaf`, `mic_stalled`, `mic_paused`, `voice_end`, `manual`), an optional chat id and at most 500 lines, refuses a body over 64 KB, and allows 12 uploads per user per 10 minutes. Each upload is one JSON line in a per-user file under `/data/ae-voice-logs` (`/secure/omnigent/data/ae-voice-logs` on the host), rotated at about 2 MB with the last 5 files kept; nothing from the lines goes to the server's own log. The same reason is sent at most once per 20 seconds. A Send voice log row in the speaker menu, next to Add voice log to message, sends it on demand and shows a toast. The new-chat Voice button gains a caret that opens a menu with the Voice mode switch (the old info icon moved into that row), Mic while speaking, Voice, Voice chat voice, Speed, Microphone, Speaker and Send voice log; the rows are shared with the chat's speaker menu.

Voice log review fixes (2026-11-16, patch `0378`): switching chats no longer counts as Voice ending, and the Voice end upload names the chat Voice came on in. An upload carries only the last two minutes and is trimmed by bytes, so emoji and accented text stay under the 64 KB limit. Automatic uploads share one budget of one per 60 seconds, spent only after the server accepts, so a failed send can be retried; Send voice log is exempt, and the menu says "Voice log sent" only when it was sent. In a chat, a mic stopped from outside is no longer uploaded, because the user's own stop looks the same. The new-chat Voice button shows its hint icon again. On the server, a refused body (413 or 422) no longer uses up the 12-per-10-minutes quota, `t` must be a finite number, and the files are written with `allow_nan=False`. `AE_VOICE_LOG_OWNER=uid:gid` makes the server own the log directory and files as that user (directory 0700, files 0600), and the overlay mounts `/data/ae-voice-logs` from `AE_VOICE_LOG_HOST_DIR`, so the box user can read the logs without sudo.

Voice log fix-check fixes (2026-11-16, patch `0379`): switching from a Voice-on chat to a Voice-off chat stops the mic and logs the end again, with no upload. The server ignores an `AE_VOICE_LOG_OWNER` id outside the platform range instead of failing uploads, and never follows a symlink when it sets owner or mode or opens a log file.

Voice log upload, Level meter and approval buttons (2026-11-16, patch `0380`): the voice log upload is a plain request from an open page (iOS drops a keepalive one) with a 10 s timeout, so a hung upload can no longer leave every later automatic upload throttled; keepalive is used only when the page is hidden or closing, and every result is written to the log and the Voice-end result is toasted. A new per-device Level meter setting (speaker menu and new-chat Voice menu) is off by default on iPhone and Safari, where the meter's second capture can take the mic from speech recognition; with it off the ring and bars follow the recognizer's events. While an approval waits, the Voice panel shows Run it and Decline in every phase, and each approval answer now carries `_meta.source` so the server log names what resolved it.

Voice log, Level meter and approval review fixes (2026-11-16, patch `0381`): with the Level meter off, a take whose recognizer shows no activity for 10 s after it starts gets a fresh recognizer silently (never reported as deaf; at most 6 a minute, then none until the next result), so a mic another app took is picked up again. The stall check on recognizer events now ignores a cough or click (only speech, or a sound that lasted 0.6 s, counts), also catches a recognizer that hung with no end event (6 s after the last sound), and only ever restarts quietly. The Level meter switch takes effect during a take. A spoken or panel approval answer counts as answered only once it went through, so a failed one can be tried again; spoken approval applies only to the session-limit policy by its exact name. A throttled Voice-end upload no longer shows a failure toast, a page-hide upload has its own budget and the reason `page_hidden` (now accepted by `POST /v1/ae/voice-log`), and a timed-out or page-closing upload is not retried. The server writes the resolve log line only after a successful resolve and escapes ids in it.

Action dock test fix (2026-11-16, patch `0382`): two action-dock tests now expect the approval source (`card`) that every approval answer has carried since patch `0380`. No behavior change.

Voice stall review fixes (2026-11-16, patch `0383`): with the Level meter off, a take whose recognizer has sent a speechstart and no end event is treated as live speech for up to 20 s before it counts as hung (it was 6 s, which restarted the mic in the middle of a sentence longer than that, because browser takes send no interim results); after a soundend, speechend or audioend the 3.5 s rule still applies, and the 10 s idle recycle waits while speech is open. Stall restarts seen only through recognizer events stop at 4 a minute (log `mic stall restart capped`, never reported as deaf) until the next result or a new take. Every silent restart now gives the fresh recognizer its full 5 s no-audiostart window, so a slow audiostart after a stall restart is not reported deaf.

Voice take kept alive on iPhone (2026-11-18, patch `0392`): on iPhone and Safari the recognizer sent no speech event until it had a final, so the 10 s idle recycle (Level meter off) treated a user who was still talking as silent and threw their sentence away. Safari now gets interim results; they only count as activity and never reach the draft or send timers. The idle recycle now stops the old recognizer so a final it still owes is delivered, instead of aborting it. The voice log records whether speech synthesis was speaking at mic start and at an idle recycle, and the end and error of a replaced recognizer.

Voice take kept alive, review fixes (2026-11-18, patch `0393`): a sentence the iPhone finishes up to 4 s after the idle recycle stops the old recognizer is still delivered (the window was 1 s).

Voice restart race on iPhone (2026-11-18, patch `0394`): a silent restart started the new recognizer while the old one was still stopping; WebKit aborted the old one and that abort killed the new one, so Voice mode paused with "start mic paused". On WebKit a restart now waits for the old recognizer's end (at most 1.5 s), an "aborted" error within 0.5 s of a restart retries once instead of pausing, and the voice log carries the browser's error message.

Voice restart race, review fixes (2026-11-18, patch `0395`): a stop, Esc or Voice mode stop while a WebKit restart waits for the old recognizer's end now ends the take at once (the new recognizer was never started, so before this the stop did nothing and the mic started anyway). A second restart during that wait waits on the real still-stopping recognizer, not the never-started one. The abort retry is WebKit only, needs a live take that is not disabled or discarded, is used once per take, and is cleared by a result or a user stop; Chrome keeps the deaf report.

Voice mode on iPhone, mic hears nothing after the prime (2026-11-18, patch `0396`): the silent priming utterance stayed speaking for about 10 s on iOS and held the audio session, so the recognizer started meanwhile got no audio. On WebKit with a cloud reply voice the prime no longer speaks it, the cloud prime releases its audio element, and with a device voice the mic start waits up to 1.5 s for the synthesizer to go quiet (then cancels it). The Voice panel sheet now shows Run it, Decline, keyboard and End in one row on a phone.

Voice mode on iPhone, review fixes (2026-11-18, patch `0397`): a stop, Esc, a button tap or a disabled composer while the mic start waits for the priming utterance now ends the take at once; before this the stop was ignored and the mic started afterwards. A restart, the start watchdog or a recognizer swap drops a pending wait first, and a wait whose recognizer was replaced never starts. The wait now covers only the silent prime, never a real reply that is speaking, and never cancels one. With a cloud reply voice on WebKit the synth unlock is still skipped, so a device-voice fallback may need one more tap.

Voice mode on iPhone, mic hears nothing after a spoken reply (2026-11-18, patch `0399`): after the cloud voice played a reply, the audio element kept holding the iOS audio session, so every later recognizer heard nothing ("No speech detected") until Voice ended, and a spoken "run it" on the approval card failed the same way. On WebKit the element is now fully released (stopped, source cleared, reloaded) when a reply ends, is stopped or errors, and the mic start releases it again and waits 150 ms before listening. Other browsers are unchanged.

Rocky voice (2026-11-10): the Voice picker includes Rocky when a local Rocky TTS service is ready. The service uses the upstream text transform, XTTS v2 model, reference audio and speed filter; the existing server speech route delivers its WAV to Voice mode and Speak replies. The model and voice assets remain a separate, optional noncommercial installation.

2026-11-16 (Admin Center D2, phase 2): the server data behind acting on many, and the page that uses it. Why: with a dozen items the page forced one dialog per item, had no way to put one off without dismissing it, could not say how long an item had waited (phase 1 had taken the browser-kept Age out) and forgot what had cleared. Server (`packages/ae_omni_policies`): snooze and wake (a record beside dismissals, judged in `_split_dismissed`, back sooner when the item gets worse), `POST /attention/bulk` (at most 50 ids, a result per id, one write), `POST /attention/review` (one chat across sources), a new `attention_log.py` (`AE_ATTENTION_LOG_FILE`: first seen, events, the Done log with one pass of grace), `GET /attention/done` and `/attention/history`, and Social posts as a source (`post-overdue`, `post-today`). Page (`packages/ae_omni_extension`): checkboxes and a bulk bar, Snooze in the row and a Snoozed fold, the history box, the Age column from first-seen, the Activity tab, Done today in the rail and a Social row's picture. "Done" means "no longer reported", not "fixed", and your own Git items are not in the Done log. Details in `docs/features/admin-center.md`.

2026-11-16 (Admin Center, fixed order and fold padding; web extension only): Needs you keeps one order while you work. Areas always follow the page's order and sources the server's order; urgency shows through the red badges, the rail, the tab badges and Expand urgent, never through position. Why: dismissing the urgent Box item dropped Box below Ship under Reid's hands. Rows keep their place while the page is open (a per-page map from item id to a sequence number, in memory only), and the automatic open and fold plan is made at the first draw and again only when the areas, sources or column count change or the list outgrows the screen. The Dismissed and Snoozed folds got the table's own insets, one ramp step of surface and tighter rows. Details in `docs/features/admin-center.md`.

2026-11-16 (Admin Center D2, phase 3, Assign): an OmniGent admin can give a Needs you item to another admin or to themselves. Why: with two admins the same urgent item sat in both lists and nobody knew who had it. Server (`packages/ae_omni_policies`): `attention_assign.py` (the current admins from the server's own `/auth/users`, cached 60 seconds, unreadable means 503), `GET /attention/admins` and `POST /attention/assign` (admins only, trusted origin, 404 outside the caller's view, 422 for a personal Git item or a non-admin assignee), the assignment kept on the item's row in the activity log with `assigned` and `unassigned` events, cleared by the Done rules, and an item assigned to another admin stays listed but leaves the viewer's counts and Review all. Page (`packages/ae_omni_extension`): Assign in the opened row, the assignee's chip, dimmed rows that stay in place, the "Assigned to me" segment and the new history words. Details in `docs/features/admin-center.md`. No `[ae-patch]`.

2026-11-16 (Admin Center D2, the opened row, layout B; web extension only): the opened Needs you row's panel is a toolbar on top and two columns below. Why: the owner compared three layouts on real rows and picked this one, the shortest panel with no empty column. Every action sits in one toolbar row (the primary first, Snooze as a dropdown menu button, Dismiss last), the evidence, facts, hint and a Social post's picture are on the left, and the history is a timeline on the right. The Assign button from phase 3 sits in the same toolbar. The phone's sheet is unchanged. Phase 1 and 2 tests that pinned the old panel structure were updated (needsTable.test.ts: the panel's children and the history beside the main column; needsPhase2.test.ts: Snooze as a menu button). Details in `docs/features/admin-center.md`. No `[ae-patch]`.

2026-11-16 (Admin Center D2, phase 3 fix round 1): after review, one counting rule everywhere. An item assigned to another admin now leaves every count on the page (header, pills, cut line, Expand urgent, reopen, Review all and Review N), not only the server's. An assignment applies only while its assignee is a current admin; an unreadable admin list or activity log applies none and says "Assignments could not be read"; a box admin sees no assignee and loses no count; a card turned off keeps its assigned rows; the admin-list cache is per caller; every assign is logged. Details in `docs/features/admin-center.md`. No `[ae-patch]`.

2026-11-16 (Admin Center, Social row; web extension only): an opened Social row in Needs you is ready to post from (the draft text, the picture, Copy text, Open on LinkedIn, Posted, Copy picture, Download picture), and Skip post replaces Dismiss on Social rows so the Social card's counts change too (docs/features/admin-center.md).

2026-11-16 (Admin Center, Social row layout; web extension only): an opened Social row is laid out in thirds, the evidence across the top, then the post text, the picture with Copy picture and Download under it, and the facts over the history; the picture fills its column, is capped at 340 px and never cropped, and under 1,400 px of panel width the row is two columns (docs/features/admin-center.md).

2026-11-16 (Admin Center, Social row fix round 1; web extension only): Skip post and Posted leave the closed Social row (it shows Copy text and Snooze), check the slot is still planned before acting, run one at a time per slot and show an Undo for 10 seconds that reopens the post; the row reads its post again each time it draws; Copy picture closes its bitmap and times out; the row's picture URLs are revoked; the three columns end at one height (docs/features/admin-center.md).

2026-11-16 (Admin Center, Social row fix round 2; web extension only): an open Social row no longer reads its post on every poll (it reads on opening, after a plan change, when the listed item changes or after two minutes), the plan-button list drops buttons a redraw left behind, and the "This post changed" and failed-read lines show in the page's message area (docs/features/admin-center.md).

2026-11-16 (Admin Center, Social row fix round 3; web extension only): Copy text and Open on LinkedIn in an open Social row read the slot again at the click (the clipboard write starts inside the click with the read pending), because the server's item fingerprint does not change when a draft is edited in the Social card; the held text is the fallback, with a line saying how old it is (docs/features/admin-center.md).

2026-11-16 (Admin Center, Social row fix round 4; web extension only): when the fresh read at a click fails, the clipboard promise falls back to the last good draft inside the click (so Copy text and Open on LinkedIn really copy it and the line says so); a forced read never joins an older one and the newer result wins; a failed read keeps the last good draft, marked stale, instead of dropping it (docs/features/admin-center.md).

2026-11-16 (Admin Center, Social row fix round 5; web extension only): a failed newer read now counts as the newest answer, so an older read that lands after it is dropped and the held draft and its stale note stay (docs/features/admin-center.md).

2026-11-16 (Admin Center, alerts row): a refused Open on GitHub shows in the opened row's message line instead of the row grid; the alerts row reads "61 Dependabot alerts: Pillow and 12 more packages"; the Social row's picture buttons are there, disabled, while the picture loads. Details in `docs/features/admin-center.md`.

2026-11-17 (Admin Center, space round 1; web extension only): the Overview loads with every area open and every source closed (the owner asked for the top-level items first), a new urgent item marks a closed line with a small dot instead of reopening it, every button in Needs you follows one system (primary, secondary, quiet, icon, link; 32 px with 13 px labels, 44 px on touch) so the area tab link, Review N, Copy text and Close stop looking different, and a phone page gets a one-line header, no composition bar, no checkboxes, a Snooze label and a compact fact list; an area tab keeps its plan. The same day the owner found that collapsed Overview empty at his real window, so the Overview now shows a wall of every card beside the list (the real cards, moved in a compact style and handed back to their tabs when another tab opens), with a Done list under the list, three width bands (1,400 px and up, 760 to 1,399 px, under 760 px) and the health rail gone from the Overview; until the pane ships, a source opened in the list gives the table the full width. Fix round 1 the same day: the Activity tab no longer counts as an area tab, so a server with four cards or fewer keeps its cards under the table (on main before this change, such a server with the Activity route loaded already showed no cards on its Overview, and this repairs that too); touch targets on the wall and the Done list are 44 px; and a line that first appears after a browser's first visit now shows the new-urgent dot (docs/features/admin-center.md).

Dependabot alert pages open from the desktop app (2026-11-16, patch `0384`): the host's GitHub link allowlist now admits `/security/dependabot` and `/security/dependabot/<n>` so an "Alerts to fix" row's Open on GitHub works. See `patches/README.md`.

Child chats compact earlier (2026-11-17, patch `0385`): a Claude session started by another session launches with `CLAUDE_CODE_AUTO_COMPACT_WINDOW=250000`; lead and standalone sessions keep the `autoCompactWindow` setting, and a value already set in the environment wins.

Family children compact earlier too (2026-11-18, patch `0398`): a chat made by the `create_session` tool has no parent session id, only an `ae.orch` label naming its lead, so patch `0385` never reached it. The launch now decides "child" with the same rule as the family view (`is_family_child` in `omnigent/inner/ae_roles.py`): a parent id, or an `ae.orch` label that is not empty and not `1`; forks and standalone handoff chats stay out.

A harness compaction keeps its spinner and defrag strip through turn ends (2026-11-18, patch `0402`): Claude Code can compact in the background past the end of the turn that started it, and the page deleted the "Compacting" spinner (and the defrag strip hid itself) when that turn settled, even though the compaction was still running. The server now marks an `external_compaction_status` `in_progress` as `background: true` on `response.compaction.in_progress`; the page keeps such a spinner through turn ends and clears it only on completed, failed, or an explicit Stop. Messages are not held; the animation just stays honest until the harness reports the finish. A server-run `/compact` behaves as before. Patch `0403` adds a 15-minute staleness cap (a spinner the harness never ended is dropped on the next turn settle), clears the spinner when a persisted compaction marker shows it ended during a stream gap, and makes Stop remove every spinner. Patch `0404` stops a new compaction from inheriting the start time of one whose end was lost.

Claude's background auto precompute no longer shows the compaction strip (2026-11-19, patch `0416`): Claude Code fires PreCompact with `trigger=auto` when it starts precomputing a summary at about 85% of the auto-compact window and applies it only when context reaches the window, possibly never, so the strip eased to about 97% and hung. The forwarder now reads the hook's trigger and posts `in_progress` only for a manual `/compact`; an auto PreCompact still mints the pending token, so the boundary persists once when the summary is applied. If the precompute is not ready at the threshold and Claude compacts synchronously, the strip does not show for those seconds; the marker and ring still land.

A refused manual `/compact` no longer posts a spurious failed after an unused auto precompute (2026-11-19, patch `0417`): the pending compaction token now remembers it came from an auto PreCompact, and the refusal path skips the spinner dismissal for such a token, since an auto precompute never raised a spinner and its token can sit unused for hours. Unknown PreCompact triggers are ignored.

A test now pins the refused `/compact` path (2026-11-19, patch `0418`): a refusal with a pending auto compaction token arms no spinner dismissal, and one with a manual token still does. No behaviour change.

Admin Center pane (2026-11-18, PR 2 of the wall plan; web extension only): at the wide band a click on an area or source line in Needs you opens a pane in the wall's place instead of opening rows under the line, so the list never moves (the approved option 3). The pane holds the source's rows (the table's own row code), the opened row's panel under them in two columns, and the source's real card at the foot, moved in from the wall; held row messages and the bulk selection are shared with the list. Esc, Close, a tab change, a window narrower than 1,400 px and a reload close it, and below 1,400 px a line opens in place as before. The PR 1 interim rule that hid the wall when a source opened is gone (docs/features/admin-center.md).

Admin Center gap fixes (2026-11-18, web extension plus `read_accounts`; the owner's desktop test after the pane shipped): line checkboxes select the rows a line lists (they were disabled on closed lines), an area's chevron is its own fold button, Expand urgent no longer closes areas, Assigned to me keeps the toolbar and a padded empty line and filters the pane, a source filter with Assigned to me reads as one line, Escape closes the pane from the list and the page body, the unreadable-source banners say what to expect, the "Chat working" and "Waiting on you" chips match their buttons, the evidence line loses its double period, and a stale full account reading with its reset ahead is believed so the Accounts source is no longer shown as unreadable. Details in `docs/features/admin-center.md`.

Admin Center look picks (2026-11-18, web extension only; the owner picked them on a vote page of real frames): "Assigned to me" is one toggle chip shaped like Expand urgent (an empty or ticked box, an accent tint when on, the count as muted text) instead of the "All | Assigned to me" pair; wall cards show a left stripe only (red or amber) with no outline, and their header pills always say words ("3 urgent", "1 to review"); at the wide band a source line loses its chevron and ends in an arrow meaning "opens on the right", with the selected line's accent edge meeting the pane's accent edge. Details in `docs/features/admin-center.md`.

Voice mode on iPhone, review fixes (2026-11-18, patch `0400`): the silent clip played by the tap prime is now released before the first mic start, and its own release sets the settle time, so the first mic start on iPhone no longer listens over a held audio session. A clip still playing at mic start is released then, with the full 150 ms settle. A reply that starts during the settle now makes the mic wait until it ends, then release and settle again; a cancelled wait never starts the mic. Other browsers are unchanged.

Voice mode on iPhone, test fix (2026-11-18, patch `0401`): the "Read last reply uses the picked cloud voice and the picked speed" test now counts only the speech request instead of every request, so a late voice-log upload from an earlier test no longer fails it. Test only; the app is unchanged.

Voice mode on iPhone, audio session pinned (2026-11-18, patch `0406`): a phone log showed the first take hearing fine and every take after the first cloud reply getting "No speech detected", even with the audio element fully released. On WebKit with the Safari Audio Session API (`navigator.audioSession`), Voice mode now sets the type to `play-and-record` when it starts, sets it again right before each mic start and after each reply ends, and puts the earlier type back when it ends. Browsers without the API, and Chrome, are untouched. The "mic start browser" log line now shows the current type. The Voice panel also stops repeating the heard text under the ring, because the composer draft already shows it.

Voice mode on iPhone, audio session review fixes (2026-11-18, patch `0407`): starting Voice from the new-chat page released the audio session when the start page unmounted, so the chat ran with the session restored; leaving a Voice chat never restored it, and turning one chat off restored it while another was still on. The session is now held by each open Voice chat (a count), taken inside your tap and handed to the chat once it opens, and put back only when the last Voice chat is gone. Reading a chat's Voice state no longer touches the session.

Voice mode on iPhone, audio session reactivated before a mic start (2026-11-18, patch `0408`): after a spoken reply longer than about a second, iOS 18 switches the page's audio session off and speech recognition never switches it back, so every take after that heard nothing. Right before a recognizer starts, Voice mode now opens the microphone for an instant and closes it again, which turns the session back on; this only runs on iPhone Safari and only after a long reply. If it takes more than 1.5 seconds the take starts anyway, and the voice log records what happened. The keyboard button in the approval row is now the same height as Run it, Decline and End, and centered with them.

Voice mode on iPhone, stopping during the audio session wait (2026-11-18, patch `0409`): if you pressed Esc, stopped the take or the composer was disabled in the short moment Voice mode waits to switch the audio session back on, the take could stay marked as listening with no microphone behind it. That stop now ends the take cleanly. Two smaller fixes: a second restart in that window keeps its own state, and a long reply that ends while the session is being reactivated is still remembered for the next start. If the audio session switch is slow, Voice mode now waits up to 5 seconds for it before starting the microphone, since a late switch could silence the take; if it still arrives late, the microphone restarts once. The voice log field `reactivated` is now `reactivatePending`.

Voice mode on iPhone and iPad, transcribed on the box (2026-11-18, patch `0410`): Apple's speech recognizer on iOS 18 goes deaf after any spoken reply, so a Voice mode start on an iPhone or iPad now sends your voice to the server's dictation (the same stream the desktop app uses) and shows the words as you speak. It is on by default there and has a switch, Transcribe on the box, in the Voice menu (shown only on iPhone or iPad when the server has dictation); turn it off to go back to Apple's recognizer. The mic button you tap yourself is unchanged, and so is every other browser. If a take hears only silence for 4 seconds (or the microphone stays muted), it restarts once without telling you, and a second silence shows the usual "tap to listen" mic. The level meter no longer opens a second microphone on such a take; the bars follow the audio being sent. The voice log now records how long the microphone and the connection took, the audio context state and rate, and each take's word counts, seconds sent and peak level. The keyboard button on the Voice sheet now sits level with End.

Voice mode on iPhone and iPad, fewer false "deaf" takes (2026-11-19, patch `0411`; follow-up to 0410): a quiet room no longer makes the box transcription restart. A take counts as deaf only if the microphone never delivered a single non-zero sample, or stayed muted, or the audio context was not running after 4 seconds; once anything was heard, the take is never called deaf. If the server info has not loaded yet, a Voice start on iPhone or iPad still goes to the box instead of Apple's recognizer. The voice log notes when no Voice tap unlocked the audio context. Stopping Voice mode and starting it again while a quiet restart was connecting no longer leaves the mic stuck off. Closing the composer while the box connection was still starting no longer leaves the microphone open.

Voice mode, faster turns (2026-11-19, patch `0421`, plus `packages/ae_omni_policies/src/ae_omni_policies/tts.py`): about two seconds less dead air per spoken turn. On the box transcription path (iPhone, iPad) the pause before a final is sent is 1 second instead of 2, because the server already waited 1.6 seconds of silence before it finalized; the desktop browser path keeps 2 seconds, and the question-card custom answer follows the same rule. The microphone and the dictation connection now open at the same time instead of one after the other, so the mic comes back sooner after a reply; a denied mic or a failed connection still closes whichever half had opened. The wait after the last spoken sentence before the mic returns is 100 ms instead of 300 ms: the echo guard keeps the spoken text for 1.5 seconds after playback ends, so that gap never carried the echo protection. The natural voice (Azure) reuses one warm connection for every sentence, which drops a fresh TLS handshake of about 0.6 to 0.9 seconds from each one. The voice log adds `reply first text`, once per turn, so a log can split model time from voice time.

Voice mode looks, car mode on the phone and words and keys on the desktop (2026-11-19, patch `0424`): the iPhone Voice sheet is now a car mode panel. The orb is gone; the sheet takes the state colour (3 px top border, tinted background), shows one big state word (Listening, Working, Speaking, Waiting for your OK) and one full-width 56 px button: Send now while listening (new: it sends what was heard at once instead of waiting out the send pause, and stays off until something is heard), Stop while the reply plays, Play the reply, Listen again, or Run it and Decline when a card waits. The keyboard and End buttons sit on the top row next to the level bars. On the desktop the bar's words wrap to two lines instead of being cut off, and key hints sit before End when the chat is wide enough: Enter send and Esc end while listening, the mic key (Ctrl Alt V, or Cmd Option V on a Mac) stop and Esc end while speaking. Two keys change on the desktop with Voice mode on: Enter now sends the heard draft (it used to end the take and leave the draft unsent), and Esc ends Voice mode with the draft kept, only while listening or speaking. Esc still stops a working chat, closes dialogs, menus and the /btw overlay, and discards a dictation take when Voice mode is off. The phone sheet has no key change.

Voice mode keys and sheet fixes (2026-11-19, patch `0425`): Enter and Esc in Voice mode now work only from the composer or the page, and never while the chat is working or a menu or dialog is open, so Enter on a focused button still presses it. Send now stops the mic, waits for the last words, then sends once; if it cannot send, Enter ends the take as before. The phone sheet's outline buttons show in dark mode, its state word is darker in light mode, the desktop bar puts Run it and Decline on their own row when narrow, and the disabled Stop is visible and labelled "Stop (nothing playing yet)".

Voice mode Send now and Esc fixes (2026-11-19, patch `0426`): switching chats or closing the page while Send now is settling no longer sends into the other chat or blocks Enter, and Esc pressed in a terminal, editor or button during a Voice mode take is left to that control instead of discarding the take.

Voice mode polish round 2 (2026-11-20, patches `0427`, `0428`): the phone sheet now fits the rounded composer card, the Stop button while the agent works is drawn as a dimmed button instead of plain text, and on iPhone the mic comes back about 0.6 s sooner after a reply because the audio context resumes while the mic and socket open. The Stop button text is also dark on the amber fill in dark mode.

Team and roles notice sent once per session (2026-11-20, patch `0429`): the lead's notice ("Auto collaboration is enabled..." plus the roles preset) used to arrive again with every prompt typed or woken mid-turn. Claude Code now gets the full notice on a session's first prompt, whenever it changes (the mode, the team members, the preset or a label override, the judge or advisor flags), after a compaction or resume, and every 20th prompt, and a one-line "Omnigent notice vXXXXXXXX still in force" marker otherwise. The auto paragraph is also about 170 characters shorter. No rule was dropped. See `patches/README.md`.

Team and roles notice follow-up (2026-11-20, patch `0430`): the once-per-session notice is sent in full again after a rewind or fork, turning auto collaboration off now tells Claude the notice is withdrawn (and turning it back on sends it in full), an Esc interrupt no longer blocks the 20th-prompt refresh, and the auto paragraph again says routine work does not need a team. Patch `0431` makes the withdrawal fire only when the server says the notice is off, and lets an interrupted turn get the 20th-prompt refresh on time.

Admin Center Needs you polish (2026-11-19, web extension only; the owner's picks after testing on his desktop): the selection bar takes the toolbar's row in place at the same height, so no line moves when a selection starts or ends; the dismissed items sit behind a quiet "N dismissed" link after the counts instead of a box at the foot of the card; and at the wide band every line ends in one arrow column, with the area tab buttons and the Review buttons sharing one width and one right edge; a bulk action's result is a one-line note over the header (never in the flow) with Details for the reasons, focus returns to the toolbar when a selection ends, the Snooze choices are a menu under the button, and every wide line keeps a fixed 120 px action slot. Details in `docs/features/admin-center.md`.

Spend families overcount fixed and a per-user "Flag on" mode (2026-11-23, `packages/` only, no patch): the sweep in `family_cost.py` counted any fall in a usage field as a status-line reset and added the whole earlier sample again, but Claude-native token totals dip a little and per field at each forwarder restart, so a lead with about 35 restarts showed 2.8B input tokens against a real 187M. It now counts deltas per field: a rise adds the rise, a fall below half the earlier sample (or from a sample under 1,000 tokens or one cent) is a reset counted from zero, and a smaller fall adds nothing. The state file is not versioned up (that would set it aside); a new `rule: 2` key marks the one-time in-place migration: it drops the old rule's inflated offsets (and takes them out of every stored day, never below zero) and restarts today's spend at zero, keeping the older days. The default runaway token amount goes from 5M to 50M (a saved value is kept), and a new `flag_on` setting (Tokens, Dollars or Either) picks which limit raises an alert. Flag logic lives once in `family_cost.flagged`; the rows carry `flagged`, `over_tokens` and `over_usd`, and the card and the Attention feed use them. `family_cost_settings` now returns a named tuple with `flag_on` as a fourth field. Details in `docs/features/admin-center.md`.

Claude-native session cost keeps growing across Claude restarts (2026-11-23, patch `0439`): the cost badge froze after the first Claude restart because Claude Code's cost counter starts again near zero in every new process and the server keeps the larger value. The forwarder now carries the earlier processes' spend (`ae_cost_carry.json` in the bridge directory), folding it in only when the Claude process changes, and posts the sum. Totals grow correctly from deploy on; spend already lost stays lost. The policy (budget) figure is unchanged. See `patches/README.md`.

Cost carry seeded from the session's own total (2026-11-23, patch `0440`): the first seed of the Claude-native cost carry (`0439`) read the server's subtree total, which includes child sessions, so a lead with priced builder or Codex children would have counted their cost twice in the family cost card. The seed is now the session's total minus its direct children's totals, floored at zero; a failed read retries on the next poll. See `patches/README.md`.

Cost carry review fixes (2026-11-23, patch `0441`): the Claude process identity now travels inside the same capture file as the cost, so a restart can no longer be counted twice or missed between two writes; the budget figure never reads below the carried total; a slow or failing seed no longer holds up token posts; the seed reads children before the parent and fails rather than use a partial child list; and two forwarders sharing a bridge directory take a lock. See `patches/README.md`.

Cost carry second review fixes (2026-11-23, patch `0442`): the statusLine capture other tools read (`context_raw.json`) is Claude's own JSON again; the process identity travels in a separate paired file. Token counts post only with a known cost, as before, and a stored cost carry can no longer move the in-memory one backwards. See `patches/README.md`.

Needs you merge error spans the row (2026-11-23, the owner): a refused merge on a Needs you pull request row (for example GitHub answering 405 "Pull Request is not mergeable") appended its red message to the row grid with no column set, so it auto-placed into the 30 px pick column and wrapped one word per line under the checkbox. `.nt-row > .flash` now spans the text and action columns like the confirm step, and the text and action column at phone width. Extension stylesheet only; no patch.

Box health alerts only on a fault (2026-11-23, the owner): a CPU at 99C under a load of 98 on 32 threads raised both heat and load alerts during normal work, though this chip holds 98 to 100C under full load by design and limits itself at 100C without damage. The CPU heat alert now fires amber only when every reading in the window is at the hot line or more while load stays under half the threads (a cooling fault: dead fan, blocked vent, dust), and red only when every reading is above 100C at any load; hot under heavy load stays green with a short note. Load no longer adds a Needs you line (the CPU card still shows its colour). The SSD alert moves from 60C to 75C amber and 83C red, the drive's own warning line, since it reads about 57C in normal use. No patch.

Quoted chat words reworded (2026-11-24, the owner, before the ae-v0.18.0.354 public release): comments in `compact_queue.py` and `claude_account.py`, a test default and docstring in `test_compact_queue.py`, one CHANGELOG line and one Admin Center page line quoted the owner's chat messages word for word. Each now says what was decided in neutral words; the test sends "go ahead" in place of the quoted message. No behaviour change, no patch.
