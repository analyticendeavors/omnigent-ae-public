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

Child prompts in the lead's dock (2026-10-27, the owner): a lead that started sessions could not see their questions or tool permission prompts without opening each chat, and its pop-up answers to a child's question went nowhere. Patch `0210` mirrors a family child's prompts into its lead's stream and snapshot and lists them in the lead's action dock with their real buttons, resolving in the child; two settings in General > Sessions switch the dock lines and the relay. Out of tree, the `ae` MCP server gains `answer_child_question(child_session_id, elicitation_id, answers?, cancel?)` (`packages/ae_omni_policies/src/ae_omni_policies/mcp.py`), which posts to the child's resolve route with the content shape the web card sends (`askUserQuestion.ts`) and refuses unless the relay setting is on, the child's `ae.orch` names the calling session itself (never a caller-supplied id), the prompt is still pending and it is a plain AskUserQuestion (never a tool permission, plan approval, Codex command or MCP prompt); a second answer reports "Already answered". The orchestrator agent allowlists it and its prompt says to relay a child's question with `ask_user` and then answer with it. No existing prompt rule told the owner to open a child chat, so none was removed. Decided: dock concept B over a card in the feed, and relaying on by default.

MCP server questions reach the web (2026-10-27, the owner): a Power BI modeling session asked "Confirm the operation" through Claude Code's `Elicitation` hook, which the wrapper never registered, so the question existed only in the terminal. Patch `0211` registers the hook, parks the question as a normal pending card (the server's schema as a form) and answers Claude with the choice; it also shows in the lead's dock via `0210`. `answer_child_question` refuses it, so only the owner answers an MCP prompt. Every failure leaves the terminal dialog to Claude. URL-mode elicitations stay in the terminal for now.

Narrow split panes (2026-10-27, the owner): in a four-pane split each pane is an iframe about 340 to 360 px wide, and the composer's Send or Interrupt button sat up to 39 px past its border because the action row needed 318 px of 266. Patch `0212` squeezes the row's gaps first, then hides the mic and host badge below about 372 px (never while a take records or the host is not online), and re-measures when the host or mic state changes. See `patches/README.md`.

Choice buttons and a child following its lead (2026-10-27, the owner): an MCP server's "Confirm the operation" Yes / No showed a dropdown with Submit and Reject; the owner wanted one button for either answer. Patch `0213` renders a required field whose schema lists 2 to 4 string choices in `enum` (oneOf and const forms keep the dropdown) as one button per value ("Continue" and "Decline" here, the full text on hover), submitting at once, with a Reject button only when no choice declines (a Yes/No keeps just its two buttons); it applies in the chat, the Inbox and the dock. Out of tree, `rabbit_hole` (`packages/ae_omni_policies/src/ae_omni_policies/rabbit_hole.py`) lets a family child follow its lead: past a threshold, a child whose valid lead (`valid_lead_id`: `ae.orch` names an existing same-owner session, no fork label, no parent; both owners are read strictly, so a failed read asks) has accepted the check is allowed and records its own ack, where before each child held every message, the lead's `post_message` included, until the owner accepted in the child. A lead that declined or was never asked, a foreign or missing lead, a fork and any store read failure still ask. The lead's ack is read from its conversation row's persisted `session_state` (the engine writes it on accept). See `patches/README.md`.

Reload notice (2026-10-27, the owner): after a deploy an open tab kept running the page it loaded, so a live fix looked unfixed until a manual reload, which happened twice today. Patch `0214` compares the tab's own entry script name with the one the server serves now (every 2 minutes, and on focus or return to the tab) and shows one toast, "OmniGent was updated." with Reload and Later; it never reloads by itself; it does nothing in an embedded or desktop host, and a failed lazy chunk stays with the app's own chunk-error boundary. See `patches/README.md`.

Reload toast spelling (2026-10-27, the owner): the toast from patch `0214` read "OmniGent was updated." Patch `0215` changes it to "Omnigent was updated." (title, doc comment and the test's expected text); nothing else changes.

Browser pane pop-ups and screenshots (2026-10-27, the owner): an agent driving StreamYard in the pane could not see the open "Create live stream" dialog in `browser_snapshot`, and `browser_screenshot` timed out while snapshot and click worked. Patch `0216` lets the snapshot walk on through a zero-size portal root (plus shadow roots and same-origin iframes), and screenshots a view the pane is not showing by staging it one pixel inside the window for the capture, with a 10 s cap and a clear error. Patch `0217` fixes the review findings: a chat closed during a capture, a retry budget that counted only sleeps, hidden frames, and a cap on huge pages.

Child prompt wake (2026-10-28, the owner): answering a family child's prompt in the lead's action dock resumed the child but left an idle lead asleep until the owner typed "continue" (child 75db0142 and lead a30d8192 at 01:55Z). Patch `0218` adds an elicitation-observers registry upstream and `child_wake.py` out of tree: when the lead (same owner, not archived or failed) is idle, one `Child prompt answered: <title> (<id4>...): the owner picked <label>.` note is posted into it as its owner, past the slot cap, the rabbit hole and the fork classifier. A busy lead is waited on for up to 30 seconds and read again before each post; a fork, a native subagent, another owner's lead, a repeat or invented verdict, or an unknown action post nothing. See `patches/README.md`.

Workspace check under Docker (2026-10-28, the owner): the merge watcher's "workspace no longer exists" check (PR 340) read every host path as missing because the server runs in a container that mounts only `/data` and ships an empty `/home`, so four notes into live sessions (/home/<user> and a worktree under it) were dropped, and the child wake note with them. `merge_watch._workspace_gone` is now off in a container (`/.dockerenv` or `/run/.containerenv`; the 4xx `workspace_missing` rule guards there) and for a path that is not absolute POSIX; elsewhere a not-found stat means gone and any other stat error means unknown. No patch.

Dock limit buttons overlapping on a phone (2026-10-28, the owner): on a phone the limit item's text is hidden, so the dock's width check never saw the row overflow and Model, Account and New chat painted over the merge and waiting badges. The strip now clips to its own width and drops New chat on a phone; the opened list keeps it (patch 0219).

Dock list sideways scrollbar (2026-10-28, the owner): opening the action dock drew a small horizontal scrollbar under the limit buttons because the list used the full labels (Copy to another login) and its row ran wider than the tray. The list uses the strip's short labels now and hides its horizontal overflow (patch 0220).

Limit hover card (2026-10-28, the owner): the dock's limit warning showed one window and nothing about the other models. Hovering its icon, or tapping it on a phone, now opens a card with the login's 5-hour, weekly all-models and per-model weekly usage, bars and reset times, the window behind the warning tagged (patch 0221).

Dashboard Flow / Brief switch dead on a phone (2026-10-28, the owner): in the native iPhone app the shell's header floats lower than the page's fixed top padding, so the header sat over the top of every extension page and took the tap on the switch. The page now starts below the header on a phone (patch 0224).

Question buttons running off the phone screen (2026-10-28, the owner): a pending question's choice buttons kept each long answer on one line, so the card and the whole chat scrolled sideways. The buttons now wrap inside the card (patch 0225).

Model menu submenus off the phone screen (2026-10-28, the owner): the composer model menu's Account, Effort and Previous models flyouts opened past the screen edge on a phone and could not be read. They now stay inside the screen (patch 0226).

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

Transcript keep fires (2026-10-29, fix for the keep of patches 0243 and 0244): live, every frontier record went stale right after it was written, because the server adds a resource event right after each cold relaunch and the keep treated it as a foreign item, so every relaunch rebuilt. Items the transcript rebuild leaves out (resource events, errors, reasoning, native tool, routing, slash command and terminal command rows) no longer stale the record or force a rebuild when they trail it; any other item, including an unknown type, still does. A kept transcript moves its record up to the server's end. After two cold relaunches of the same session the second shows `ae.claude_native.resume = kept` (patch 0245).

Lead handoff at 300k (2026-10-29, the owner's token audit; L1 of the token-saving plan): lead chats were 28% of the box's Claude spend because eight grew past 300k tokens of context and re-read it every turn. A 60 second sweep (`lead_handoff.py`) posts one note into an idle top-level Claude chat whose `omnigent.last_context_tokens` (read only when under 30 minutes old) passes the new `lead_handoff_tokens` setting (300000 by default, 0 off; roles state, top level beside `child_relay`; the "Hand off at" row in Settings, patch 0246). The lead writes a packet and calls the new `hand_off` tool, which posts to `POST /v1/ae/sessions/{id}/handoff`: create the new lead (same host, workspace, model without `[1m]`, effort, preset and Claude login, key `handoff:<old id>`), write `ae.handoff_to` on the old lead (the commit point), then re-point the children's `ae.orch`, rebuild the check-in prompt (`schedules.checkin_prompt`, only when it names the old id) or replace the id in other scheduled tasks, and set the old lead to `reference`. Each step after the commit is idempotent, a repeat call finishes what is left, the sweep repairs a lead left half done, and one lock per lead means the button and the tool at once start one new lead. The slot count is taken with the old family left out, so the handoff works at full slots. `post_message` and `schedule_checkin` follow `ae.handoff_to`. The note passes the slot cap, the rabbit hole and the fork classifier like the merge and child wake notes (`continuation.py`). The L2 button is not in this change. See `docs/features/action-dock.md` and `docs/LABELS.md`.

Lead handoff review round 1 (2026-10-29, two reviewers): the new lead's brief is confirmed before the commit point (`ae.handoff_brief=posted`, written after the post succeeds; a retry that finds the new lead by its key posts the packet first); a handoff is finished only when `ae.handoff_done=1` is written, which needs every step clean, and the sweep repairs every committed lead without it, scheduled tasks included; the target must exist, be live and have the same owner before anything is re-pointed, else the marks are cleared and a retry starts a fresh lead under the same key; `merge_watch.post_live` (merge notes, child wake) follows `ae.handoff_to`, five hops with a loop guard (the watcher's PR records are rebuilt from the live sessions each scan, so forwarding is the whole fix; nothing keyed by session id is moved); the sweep and `/handoff/request` take one lock per lead and re-read the labels inside it, with an in-process set of nudged leads as a backstop; only leads in `working` or `blocked` are nudged; other scheduled tasks get only `session_id <old id>` and `session <old id>` replaced and the tasks that name the old id elsewhere are listed in the answer; `hand_off` tells the old lead to stop; `children_of` ignores a `conv_` prefix.

Lead handoff review round 2 (2026-10-29): a handoff whose new lead died after `finish` had set the old lead to `reference` left the family stuck (a retry was refused as "already a reference", the sweep no longer saw a lead, children and tasks pointed at the dead lead). The commit now stores `ae.handoff_prev_status`, and the undo (`clear_commit`) moves the children back, rewrites the check-in and tasks back, restores the status and only then clears the marks; any problem keeps the marks so the undo repeats. The sweep takes a lead's lock with a 2 second wait and skips a busy lead until the next sweep.

Compact card at 300k (2026-10-29, L2 of the token-saving plan; replaces the hand-off note; the owner's call: always compact, never suggest a new chat): the 60 second sweep no longer posts anything into a chat or waits for it to be idle. Past the owner's `lead_handoff_tokens` it labels a top-level Claude chat `ae.handoff=suggest` with the size in `ae.handoff_at`, and the web shows a card in the action dock above the composer and a line in the rail's Context section: "This chat is at 370k tokens. Every reply re-reads all of it." with Compact (two clicks, the same `/compact` the rail button sends, which does not depend on the model, so it works on 1M models) and Not now. Compact also snoozes, Not now snoozes alone, and a snooze waits for 100,000 more tokens. The label is wiped when the context falls back under the size, the setting is off, or the chat leaves working and blocked; the earlier `nudged` value is rewritten to `suggest`. `POST /v1/ae/sessions/{id}/handoff/request` and the note text are gone; `POST .../handoff` and the `hand_off` tool stay for when the user asks for a new chat. The setting is now "Suggest compacting at", key unchanged (patch 0250). Review round 1 (same day): the web shows the card only for the chats the server may label (top-level, not archived, no `ae.orch` family parent, no fork or side chat, role absent or lead, the Claude native wrapper, working or blocked), so a stale `suggest` label on a child, reviewer or other harness offers nothing; a snooze now survives the chat leaving working or blocked and ends only when the context falls under the size or the setting is 0; the sweep reads the owner, context and decision inside the chat's lock and skips a chat whose owner cannot be resolved; the snooze route re-reads under the lock and answers 409 for a chat that handed off, is not eligible, or has no context reading; a 409 from the snooze (from Not now, or after Compact) is a quiet no-op while other errors still toast; the sweep logs once per chat at INFO when it skips one for want of an owner; the old `Lead handoff:` note text is no longer exempt from the slot cap, the rabbit hole and the fork classifier. Still not checked: whether a native compact refreshes `omnigent.last_context_tokens` before the next reply; the snooze on Compact covers it either way.

Lead handoff review round 3 (2026-10-29): `target_ok` now answers `ok`, `gone` or `unknown`. A store or owner lookup that fails, or an owner that reads as `None` on either side, is `unknown`: the sweep's repair logs at info and changes nothing, and the handoff route answers 503 with retry advice. Only a confirmed missing, archived or different-owner lead is `gone` and runs the undo. A permanent problem in the repair or the undo (a hand-edited check-in, no owner client) warns once per lead per process, then logs at debug.

Stand-in Archive family (2026-10-29, patch 0247): right-clicking a Grouping > Family stand-in row (lead archived or not loaded) now offers Archive family, which archives every non-archived session of that family, so a leftover family can be closed. It confirms first while a session is open.

Stand-in Archive family fix (2026-10-29, patch 0248): Archive family on a stand-in whose root session was missing from the loaded pages found nothing to archive; it now archives the sessions the sidebar shows under the stand-in as well, never the wider family of a re-rooted lead.

Transcript keep recognises its own posts (2026-10-29, second fix for the keep of patches 0243 to 0245): live, the keep still never fired because the runner posts a transcript item over the event tunnel, which names no item id, so the forwarder could not tell its own items from foreign ones and the record never advanced past the seed. The forwarder now derives the id the server gives a post from its source id, a returned id still wins, and a post that failed never counts. When a foreign item holds the record the log says which item and why, once per change. After two cold relaunches of the same session, the first shows the record's item count following the server within a minute and the second shows `ae.claude_native.resume = kept` (patch 0249).

Neutral names in the repo's own files (2026-10-29): the code, comments, docs and test fixtures outside `patches/` and `upstream/` no longer name the box's users. History says "the owner" or "the second box user", model-facing text says "the user", and fixtures use `owner@example.com` and `member@example.com`; tests that check a real value is refused build it from parts at run time. Behaviour changes with it: the orchestrator's pop-up tool is now `ask_user`, and the old name stays a hidden alias (callable, absent from tools/list, `LEGACY_ASK_TOOL` in `mcp.py`) so running sessions and skills keep working until a later PR drops it. The child-wake note reads "the user picked <choice>", and the continuation matcher also accepts any single capitalised word there, so notes already in history still match. The host config sync's personal-marker list moved out of source into a box file (`config/host-scrub.txt`, or `AE_HOST_SCRUB_FILE`; gitignored, with an example), and the sync refuses to run while it is missing or empty. `deploy/pull-secrets.sh` files the secrets under the invoking user's primary group by default (`AE_ENV_GROUP` still overrides). Left as they were: the managed block of CLAUDE.md (generated from ae-claude-skills) and `tests/deploy/fixtures/team-mcp.json` (a byte copy of an ae-claude-skills file that a test compares). Why: defence in depth behind the release scans, and a step toward putting the last names on the private leak denylist (docs/LEAK-SCAN.md).

The hidden alias goes (2026-10-30): the old name of `ask_user`, kept one day as a callable alias outside tools/list, is removed from `mcp.py`; a call by that name now answers as an unknown tool. Nothing on the box or in ae-claude-skills called it, and clients call only the tools that tools/list shows.

Transcript keep survives an image read (2026-10-30, patch 0251): after patch 0249 the keep's record followed the server, yet any session that had viewed an image since its last rebuild still rebuilt on every cold relaunch, because Claude Code's native image-read record carries the image's base64 a second time in `toolUseResult` and the keep refused any file the clone sanitizer would change. The keep now refuses only for a change the model would see, so that one shape stays, and a refused keep logs one INFO line naming the check that failed (ids, counts, types and line numbers only). Live check: Read a PNG in a test session, then cold-relaunch it twice; the second shows `ae.claude_native.resume = kept` and a first call whose cache read is about the history size. A session that still rebuilds shows the new line. If the first call after a keep of a file with an image read writes far more cache than a normal turn, revert the native-shape exemption.

Slots-full approval card (2026-10-29, patch 0252): the slot cap's ask now reads as a decision: the question, then Run as overflow and Decline right under it, then the park hint, with the holding sessions folded behind "Show the N holding slots". The buttons used to sit below a long paragraph and fell off a phone screen.

Slots-full card fixes (2026-10-29, patch 0253): the card no longer mis-splits holder titles that contain a number and a period, reads each holder's detail from the end of its row, appears only for the slot cap's own ask, and drops the holder toggle when only a pending claim holds the slot.

Family cost card (2026-10-29, B1 of the token-saving plan): an Admin Center card ranks each session family's spend over 7 or 30 days. `family_cost.py` sweeps every session's usage every 15 minutes (one process, a `flock`), keeps a per-day snapshot for 35 days in `/data/ae-family-cost.json`, and `GET /v1/ae/family-cost?days=7|30` answers from the last sweep (`warming` until the first). A window is the total now minus the snapshot at its start; a chat started inside the window counts from zero. Checked against Claude transcripts for four families (two with five restarts): within 1.4%; a Claude lead's usage already holds its subagents, a Codex session's native subagents add tokens only (the parent's cost includes them), and there is no cache-write figure. An admin sees every family with a host chip, anyone else their own. "Show cost as" (tokens or dollars) and "Runaway family" (5M tokens or $25 spend today; one amount per unit) are saved per user in the roles state through `POST /v1/ae/family-cost/settings`, because the extension cannot PUT. Out of tree only: no upstream patch.

Family cost review round 1 (2026-10-29, two reviewers): the settings POST now carries the JSON content type and trusted origin checks; the family walk goes up from every session (handoff, `ae.orch`, parent links) so children linked only by parent, subagents of subagents and handoff chains join one family and an orphan is its own lead; a session total that falls adds the sample before it to an offset; days are Pacific time and start at the last sample before midnight; the state file reloads when its mtime changes, and an unreadable one is renamed `.bad-<timestamp>` instead of overwritten.

Bridge dir retention (2026-10-29, patch 0257): the ae-v0.18.0.190 deploy restarted the host and every relaunched chat rebuilt its transcript with the reason "no forwarder cursor". Root cause: the host's startup orphan sweep deleted each claude-native bridge dir, cursor and frontier record included, as soon as its runner was dead; the dir was born again only when the new runner prepared it, two minutes later in the logged case, so the sweep won whenever a relaunch lagged by more than about 3 s. A dead-owner claude-native dir now stays for 7 days after the latest of its owner marker, cursor and frontier record (the window codex-native uses), losing only the runtime files (the six a re-prep unlinks, plus `tool_relay.env`, which holds a token). Archive (the UI and the auto-archive sweep both PATCH `archived=true`) never told the runner to delete its bridge dir, so `_archive_stop` now asks the runner for its resource cleanup before stopping the host runner, and an archived chat's dir goes at once. A cold launch also logs one INFO line saying whether the cursor and the record exist. Two things linger for the same window: the attachment cache at `~/.omnigent/attachments/<hash>` (700), which the sweep removes after the rmtree, and `bridge.json`'s token, the long-lived token re-prep already reuses, both behind the same same-user 700 boundary codex-native already accepts for 7 days. The Admin Center box-health cleanup does not touch these dirs: its `/tmp` litter allowlist and the box's tmpfiles rules name other paths. Live check: after the deploy the host log's `native_bridge_orphans` line keeps claude-native dirs; the next relaunch's keep reason line no longer says "no forwarder cursor"; the restart after that keeps the transcript or names the next failed check.

Question pop-up wrap (2026-10-29, patch 0258): a long unbroken string in an AskUserQuestion question, such as a pull request URL, ran past the right edge of the card on a phone. Root cause: the question sits in a `fieldset`, whose default minimum width is its content width, so the legend could not shrink and the text could not wrap. The section now has `min-w-0`, and the question and option descriptions use `overflow-wrap: anywhere`. Touches `AskUserQuestionForm.tsx` and the `ApprovalCard` suite.

Question option wrap (2026-10-30, patch 0259): review of patch 0258 found that a long unbroken option label still ran past the card, because the label sits in a flex column that also could not shrink. The label and description wrapper now carries `min-w-0` and `overflow-wrap: anywhere`. Touches `AskUserQuestionForm.tsx` and the `ApprovalCard` suite.
