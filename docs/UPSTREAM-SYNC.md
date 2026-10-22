# Upstream sync

How `omnigent-ae` follows OmniGent releases without becoming a hard fork, and
what CI enforces at every step. Written 2026-09-11, the day the fork was pinned
to `v0.13.0`.

## Why a thin fork

Upstream merges hundreds of PRs a week, releases a minor weekly, and carries over
a hundred schema migrations. A hard fork rots in weeks. So the upstream tree is
never edited in place: it is a git submodule (`upstream/`) pinned to a branch on
our fork, `analyticendeavors/omnigent@ae/vX.Y.Z`, which is the upstream release
tag plus a short patch series. The reviewable form of that series is `patches/`;
the executable form is the fork branch; `upstream.lock` names both, and CI fails
when any of the three disagree. The series branch is whatever `upstream.lock`'s
`AE_BRANCH` names (today `ae/v0.18.0-public`; the `-public` suffix marks the series
regenerated for publication).

Everything that is ours and does not need to touch upstream code lives out of
tree: policies in `packages/ae_omni_policies` (Phase 1, 2026-09-23), the Parking
Lot page in `packages/ae_omni_extension`, agents in `agents/`, config in
`config/`. Phase 0 was the machinery below with an empty series; Phase 1 needed
no core patch. Phase 2 (2026-09-23) added the first: `patches/0001`, the
`ae.status` icon and the slots pill in upstream's sidebar, ten lines across
three upstream files and the rest in new files, so the series is now what the
weekly sync has to carry. Phase 3 (2026-09-23) added `patches/0002`: the
`server.request` host method and `ae.*` labels on extension session
summaries, the bridge the Parking Lot page uses; again the allowlist lives in
a new file and the upstream edits are a line or a few each (`api.py`,
`registry.ts`, `useExtensionHostServices.ts`, `sessions.ts`, `types.ts`, the
SDK's context type, the docs and the tests the new fields touch). Phase 4
(2026-09-23) added `patches/0003`: the item-observers registry
(`omnigent/server/item_observers.py`, new) that the AE observer hooks to read
what the assistant wrote, with two lines each (an import and a call) in
`orchestration.py` (`_persist_external_conversation_item`) and `helpers.py`
(`_flush_relay_text`), plus its own test module; the AE layer imports it under
`try/except ImportError`, so a sync that drops the patch degrades to the
request-phase scan instead of failing to load.
Phase 5 (2026-09-23) added no patch, but three more things a sync can move:
the single-file agent grammar the specs in `agents/` are written in
(`omnigent/spec/omnigent.py`; `test_agents.py` loads them), the scheduled-tasks
API the seeder calls (`omnigent/server/routes/scheduled_tasks.py`;
`test_schedules.py` seeds and fires against it), and the `mcp` package, whose
pin in `packages/ae_omni_policies/pyproject.toml` copies upstream's so the two
never disagree; bump it with the omnigent pin in step 4.
`patches/0004` (2026-09-24) is the sidebar's Status submenu: a new
`web/src/shell/AeStatusMenu.tsx` and eight lines in `Sidebar.tsx`'s
`ConversationMenuItems`, the component upstream renders under both the kebab
and the right-click menu. A sync that reshapes that menu body is where it
conflicts; the component takes the menu primitives as props, so re-seating it
is the whole fix.
`patches/0005` (2026-09-24) is the split view: three new web files and ten
lines across `App.tsx`, `Sidebar.tsx` (after `0004`'s submenu, so the two
conflict together) and `AppShell.tsx`'s `initialSidebarOpen`. Each pane is a
frame of `/c/:id`, so the patch leans on nothing inside the chat store; a sync
that renames `/c/:id`, moves the sidebar menu or reworks the initial sidebar
state is where it breaks. Upstream's own split view is a draft
(https://github.com/omnigent-ai/omnigent/pull/6758); the sync that brings it
drops this patch instead of rebasing it.
`patches/0010` (2026-09-24) adds layout presets to the split view and edits
only files `0005` created, so it cannot conflict with upstream; it applies or
drops together with `0005`. Upstream v0.15.0 still has no split view of its own
(checked 2026-09-24: https://github.com/omnigent-ai/omnigent/pull/6758 is a
draft), so the presets are built on `0005` rather than on an upstream view.
`patches/0011` (2026-09-25) is Run on chat shell code blocks: four new web
files and a few lines in `TerminalSession.ts` (two methods), `TerminalView.tsx`
(a registration beside `sessionRef.current = terminalSession`),
`components/ai-elements/message.tsx` (`ChatCodeBlockPre`'s button row) and
`AppShell.tsx` (the hook call after `useNewShellHotkey`). A sync that reworks
the terminal attach (the `TerminalSession` constructor or its WebSocket), the
chat code block header, or AppShell's shell tabs is where it conflicts; the
page test in `AeRunCodeButton.test.tsx` fails if the bytes stop reaching the
socket. It also finds assistant text by BlockRenderer's
`data-testid="assistant-text-section"`; if that marker goes, Run silently
disappears, which the same suite catches. `patches/0013` (same day) reshapes the
two `TerminalSession` methods into four small hooks (it relies on xterm's
`onWriteParsed` and `modes.bracketedPasteMode`) and reads the streaming mode from
Streamdown's exported `StreamdownContext`; an xterm or Streamdown upgrade that
renames either shows up in the same suites.
`patches/0029` (2026-10-13) is scheduled-run retention's mechanism: a new
`omnigent/server/scheduled/retention.py` (the `keep_runs` setting in its own
table, the fire-observer registry, a request-free session delete), two lines in
`fire.py` (an import and a call after the successful fire's `_record_run`), the
setting resolved and stored beside each store call in
`routes/scheduled_tasks.py`, the field on `ScheduledTask`, and the two agent
tool schemas and dispatch allowlists. A sync that reshapes the fire's success
path or the routes is where it conflicts. `delete_session_server_side` copies
the server-owned steps of `DELETE /v1/sessions/{id}` (`routes_events.py`), so
on every sync diff that route against it: a new per-session cache or cleanup
step upstream adds there must be added here too, or a pruned session leaks it.
`patches/0031` (2026-10-13) extends `0003` to the batched path upstream added
in v0.16.0 (https://github.com/omnigent-ai/omnigent/pull/8240): one
`notify_items_persisted` call after the store append in
`_persist_external_conversation_items` (`orchestration.py`), so sub-agent
transcripts the claude-native forwarder posts as one array reach the AE
observer too. `test_item_observers.py` posts such an array; a sync that renames
that function or moves its append is where it conflicts.
`patches/0033` (2026-10-13) is the task-project patch: it adds
`omnigent/server/scheduled/task_project.py` (the setting in its own table, the
default by name from `OMNIGENT_SCHEDULED_TASK_DEFAULT_PROJECT`, and
`file_fired_session`), `project_id` beside `keep_runs` in the scheduled-task routes,
entity and tool schemas, three lines in `fire.py` (import, `FireDeps.project_store`,
one call after `create_conversation`), `project_store` passed in `app.py`, and a
Project picker in the automation dialog (`AeTaskProjectField.tsx`, new). A sync that
reshapes `_create_session` or the routes' `keep_runs` lines is where it conflicts.
`patches/0034` (2026-10-13) is auto-resume's mechanism: a new
`omnigent/server/host_connect_observers.py` (an observer registry run in the
background after a host tunnel registers, and `resume_interrupted_session`,
the `retry_session` recovery without a request), `app.py`'s `on_host_connect`
given its own callback that also notifies the observers, and a
`resume_interrupted_turn` keyword passed through `_recover_retry_session` and
`_ensure_runner_session_initialized` to the session initializer, which already
takes it for child restoration. `test_host_connect_observers.py` covers it; a
sync that reshapes `_recover_retry_session` or renames the host tunnel's
`on_host_connect` is where it conflicts.
`patches/0035` (2026-10-14) brings the sidebar's slot count in line with the
server: `isAeSlot` in `web/src/lib/aeLabels.ts` (patch 0001) no longer counts a
working session blocked on an approval or whose last turn failed, the two
exclusions `ae_omni_policies.sessions.working_sessions` has made since
2026-09-25. The sidebar read "Dashboard 4/3" while the Working section read 3/3.
A sync that reshapes `isAeSlot` or the `Conversation` row's `status` or
`pending_elicitations_count` is where it conflicts.
`patches/0036` (2026-10-14) completes upstream's accounts-to-OIDC identity remap
(`omnigent debug migrate-accounts-to-oidc`) for the Microsoft sign-in cutover
(AE's own cutover runbook, kept in a private repository). Upstream rewrote users, grants, comments, policies,
account tokens and hosts; a new `omnigent/server/identity_migration_extra.py`
rewrites the rest (agents, device grants, projects, scheduled tasks,
conversation items, preferences, connections, daily costs, the
`omnigent.turn_actor` label) in the same transaction, and
`identity_migration.py` gains one import and one call.
`test_identity_migration_extra.py` covers it. A sync that adds a user-id column
or reshapes `remap_identities` is where it conflicts; a new column must join
the extra module, or the remap leaves it behind (the cutover's read-only
leftover scan catches that before the server starts).
`patches/0037` (2026-10-14) redesigns the pull request cards that patch 0012 added
and 0020 to 0023, 0026 and 0028 reshaped: a state icon and a two-line title in the
header, one "Ready to merge" line when the merge gate is open, quiet chips
otherwise, and the confirm step as a divided section whose button names the target
branch. It touches only AE files (`AePrCards.tsx`, `AeInlinePrCards.tsx`,
`aePrs.ts`, a new `aePrIcons.ts` and their suites), so a sync conflicts with it only
where it conflicts with those earlier patches.
`patches/0038` (2026-10-14) fixes the review findings on 0036: a moved
connection's secret is re-encrypted under the new owner (the credential cipher's
context includes the user id; `connections_reconnect` counts rows it cannot
read), and a forced merge keeps the higher approved checkpoint in
`user_daily_cost` instead of the sum.
`patches/0039` (2026-10-14) fixes the review findings on 0037: a card with no data
yet shows a muted git-pull-request icon labelled "Pull request" rather than the
green open one, and on a narrow card Cancel stays compact so the confirm button,
which names the branch, takes the rest of the row.
`patches/0040` (2026-10-14) shortens the usage popover, changing only P6's
`AePlanLimits.tsx` and its suites: Claude Desktop's layout, the prompt cache parts
joined into the context bar, two-line limit rows, and the other plan and
collaborators behind "See detailed breakdown".
`patches/0041` (2026-10-14) changes one class string in upstream's
`web/src/components/ui/tooltip.tsx` (the popover surface in light mode too, not a
near-black box) and one in `ComposerContextRing.tsx` (its dimmed second line), plus
that suite's assertion. A sync that restyles `TooltipContent` is where it conflicts.
`patches/0042` (2026-10-14) fixes the review findings on 0040 and 0041:
`KeyboardShortcut.tsx` drops its forced slate chip (shortcut tooltips take the
popover surface too), the config-error tooltip in `ChatPage.tsx` drops its own
border, and `AePlanLimits.tsx` hides the legend dots while the bar is toned or stale
and keeps the plan explainer open across loading. `ChatPage.composer.test.tsx` gains
a stand-in for AeTeamControl.
`patches/0043` (2026-10-14) gives `ChatPage.mention.test.tsx` and
`ChatPage.statusLine.test.tsx` the same stand-in. Since 0030 those three suites had
failed because AeTeamControl needs a QueryClient; a sync that reshapes
AeTeamControl's imports is where they conflict.
`patches/0044` (2026-10-14) adds the Fast mode switch: new
`omnigent/runner/ae_fast_mode.py`,
`omnigent/server/routes/sessions/routes_ae_fast_mode.py`,
`web/src/components/AeFastMode.tsx` and `web/src/lib/aeFastMode.ts`, plus dispatch
lines in `runner/app.py` (whose pane helper `runner/native_controls.py` exposes on
`NativeControls` since v0.18.0), a mount in `routes/sessions/__init__.py`, three lines in
the claude-native `status.py` that keep `fast_mode` in `context.json`, the menu
mount in `ChatPage.tsx` and two separator counts in `ChatPage.composer.test.tsx`. A
sync that reshapes the runner's `/events` dispatch, `normalize_status_payload` or
the session harness picker's menu is where it conflicts.
`patches/0045` (2026-10-14) fixes the dual-review findings on 0044: turning fast
mode on requires `fastModePerSessionOptIn` in the host's Claude settings, the runner
answers 202 pending (never a failure) once `/fast` is typed, the server waits 75
seconds, a per-session lock replaces the "already there" shortcut, off stays allowed
on any model, a pending flip disables the model and effort picks, and a flip never
starts a Claude Code terminal. It also regenerates upstream's `openapi.json` for the
new `/v1/sessions/{session_id}/fast-mode` path, which
`tests/server/test_openapi_drift.py` checks; upstream-smoke now runs that test, so a
patch that adds a route without regenerating it fails CI.
`patches/0047` (2026-10-15) cleans the usage popover's detailed breakdown, changing
only P6's `AePlanLimits.tsx` and P30's `aeSessionContext.ts` and their suites: child
sessions labelled `omnigent.wrapper=claude-code-native-ui-subagent` (Claude Code's
own subagents, which report no context of their own) leave the collaborators list,
rows are named by task summary or title instead of a bare `type:hexid`, a missing
reading is one "No reading yet" line with the reason in its tooltip, Claude model
ids read as names, and a Claude Code session on a 1M window shows "About N until
auto-compact" (Claude Code compacts a native 1M window at about 967k tokens by
default). A sync that renames the native-subagent wrapper label is where it breaks
quietly.
`patches/0055` (2026-10-16) redesigns the Agents pane. The logic is in new files
(`aeAgentsPaneModel.ts`, `aeAgentsGraphLayout.ts`, `AeAgentsGraphControls.tsx`,
`AeAgentsSections.tsx`, `lib/aeAgentsIcons.ts` and their suites); upstream files change
in a few lines: `useChildSessions.ts` keeps the `created_at` and `updated_at` the
server already sends, `SubagentsGraphView.tsx` picks the layout direction, moves the
handles to the node sides when sideways and swaps `fitView` for the readable fit, and
`SubagentsPanel.tsx` renders the child rows through the Running and Finished sections,
puts the Model, Time and Stop columns on each row and mounts the Plan section. One
`SubagentsPanel.test.tsx` case now expects running rows above finished ones. A sync that
reshapes `SubagentRow`, `layoutTree` or the child-session wire fields is where it
conflicts. Known limit: a finished agent's own agents nest under it and fold with it
even while they run. Claude Code's subagents never nest, so the case is rare, and
lifting it would mean fetching the children of every finished row.
`patches/0063` (2026-10-16) fills the Agents pane's Model column. New modules
`server/routes/_sessions/ae_child_model.py` (which model to show for a child) and
`harnesses/claude_native/ae_subagent_model.py` (post each subagent's model once per
change) carry the logic; upstream files change in a few lines: `ChildSessionSummary`
gains `model` (so `openapi.json` is regenerated), `_child_session_summary` fills it,
`_forward_one_subagent` calls the reporter after each transcript read, and the web
maps the field and prints it. One `SubagentsPanel.test.tsx` case now expects
"Sonnet 5" for a routed `databricks-claude-sonnet-5`. A sync that changes
`TranscriptReadResult.latest_model`, `reported_model` or the child-session wire
fields is where it conflicts.

`patches/0051` (2026-10-15) adds on-demand Claude context categories: new
runner and session-route modules, a dispatch in `runner/app.py`, and a route mount
in `routes/sessions/__init__.py`. The web adds its own fetch hook and category
component, mounted in `AePlanLimits.tsx`. A sync that changes runner control
forwarding, bridge identity lookup, the context status payload, or the popover
mount is where it conflicts. The route is included in `openapi.json`.
The runner reconstructs a non-persisting fork using Claude's summary control
request; changes to that protocol or Linux process launch arguments need a fresh
probe. See `docs/PLAN-LIMITS.md` for measured limitations and the cached data path.

`patches/0071` (2026-10-17) adds the session rail. The logic is in new files
(`web/src/lib/aeSessionRail.ts`, `web/src/shell/AeSessionRail.tsx` and
`AeSessionRailToggle.tsx`, `web/src/components/aeRail/`,
`omnigent/server/routes/sessions/routes_ae_mcp_status.py`) and in AE files from earlier
patches (`AePlanLimits.tsx`, `AeContextCategories.tsx`, `aeContextUsage.ts`,
`aeSessionContext.ts`, `runner/ae_context_usage.py`). Upstream files change in a few
lines: `AppShell.tsx` mounts the rail as the last child of the content row (inside
`CanvasWorkspace` since v0.18.0) and hands it
the workspace panel's own controls, `ChatHeader.tsx` places the toggle after the right
panel toggle and wraps the phone menu's workspace items (one `ChatHeader.test.tsx` case
lists the new "Session rail" item), `_sessions/helpers.py` records each settled Codex
MCP startup map in `_publish_mcp_startup`, `routes/sessions/__init__.py` mounts the new
route, and `openapi.json` is regenerated for it. A sync that reshapes AppShell's content
row, the header's mobile menu items or `_publish_mcp_startup` is where it conflicts.

Patch `0072` adds Update Branch and the AE compliance review disclosure to the
existing PR card. It changes `AePrCards.tsx`, its test, and `aePrs.ts`; the GitHub
client, routes, and report parser live in the AE package outside the upstream tree.
Branch updates carry the displayed head SHA and treat GitHub's 202 response as
pending. The UI shares progress across cards and polls for a changed revision.
Reports load when their disclosure opens and preserve the full comment. An
explicit reviewed SHA is required to label a report current; missing SHA metadata
stays unverified. Compliance comments do not participate in merge gating.

`patches/0074` (2026-10-17) adds the session activity indicator. The logic is in new
files (`omnigent/server/routes/_sessions/ae_session_activity.py`,
`web/src/lib/aeSessionActivity.ts`, `web/src/hooks/aeSessionActivityHooks.ts`,
`web/src/components/AeSessionActivity.tsx`). Upstream files change in a few lines:
`SessionListItem` in `schemas.py` gains `background_task_count` and `active_child_count`
(so `openapi.json` is regenerated), `_build_session_list_item` in
`_sessions/orchestration.py` fills them, the sidebar's row state and project marker in
`Sidebar.tsx` call the AE refinements, `useSessionState.ts`, `SessionStateBadge.tsx` and
`conversationState.ts` know the new states, `useConversations.ts` maps the two fields,
`chatStore.ts` stamps the last event time in `tapSessionEvents`, `Transcript.tsx` mounts
the status line in place of the working indicator, and `BackgroundTaskIndicator.tsx`
hides its pill while the line shows the same shells. Two `Sidebar.test.tsx` cases give
their rows a fresh `updated_at`, since an old one now reads as quiet. A sync that
reshapes `SessionListItem`, `_build_session_list_item`, the sidebar's row state or the
transcript's working indicator is where it conflicts.

## The files

| File | Holds |
|---|---|
| `upstream/` | submodule, checked out at `AE_BRANCH_SHA` |
| `upstream.lock` | `UPSTREAM_REPO`, `UPSTREAM_TAG`, `UPSTREAM_SHA`, `AE_FORK`, `AE_BRANCH`, `AE_BRANCH_SHA`, `PATCH_COUNT` |
| `.gitmodules` | the fork URL and `ae/vX.Y.Z` branch (must match the lock) |
| `patches/NNNN-*.patch` | `git format-patch` output for `UPSTREAM_SHA..AE_BRANCH_SHA` |
| `scripts/*.sh` | the four operations below; `just` recipes call them |

## Operations

- `just lock-check`: the lock, `.gitmodules`, the checked-out submodule and
  `patches/` agree.
- `just patches-apply`: apply `patches/` onto the pristine tag in `upstream/`
  with `git am --3way`. No-op if already applied or empty. Stops on conflict
  and prints how to continue; it never resolves one.
- `just patches-export`: regenerate `patches/` and the lock from the commits in
  `upstream/`. `just patches-check` does the same into a temp dir and fails on
  any difference.
- `just patches-rebase [base]`: when another PR merged a patch first, carry
  this branch's fork commits onto the base's (default `origin/main`)
  `AE_BRANCH_SHA` with `git rebase --onto`, re-export, and rewrite each stale
  patch number (`0NNN` and `P<n>`) to its new one in the lines this branch
  added to `README.md`, `patches/README.md`, this file, `docs/CHANGELOG.md`
  and the `justfile`.
  Lines the base owns are never touched, so it is safe after merging main
  into the branch. A rebase conflict stops it; resolve in `upstream/`,
  `git -C upstream rebase --continue`, and rerun. Then rerun the suites,
  commit, force-push the fork topic branch and fix the PR title. Added
  2026-10-20 after PR 129 was renumbered by hand four times
  (https://github.com/analyticendeavors/omnigent-ae/issues/135).
  `--ci` is the same run without a person: the patch rebase bot below.
- `just sync vX.Y.Z`: the procedure below.

## Patch rebase bot

Added 2026-10-20. Every PR that adds an `[ae-patch]` claims the next patch number
when it is written, so once one merges, every other open patch PR conflicts on
`patches/*.patch` (each file's `[PATCH n/N]` line names the new total), on
`upstream.lock`, on the `upstream` pointer and on the doc entries that both sides
appended in the same places. That day PR 164 and PR 171 were each rebased by hand
three times in 90 minutes. `.github/workflows/patch-rebase.yml` now does it.

When it runs: on every push to `main`, for each open PR into `main` that touches
`patches/` and that GitHub reads as `CONFLICTING` (it polls `mergeable` up to eight
times, ten seconds apart, while GitHub computes it). By hand: Actions, Patch
rebase, Run workflow, with a PR number to rebase that one PR whatever GitHub reads.
PRs from other repositories are skipped. One run per PR at a time, never cancelled
half way.

What `scripts/rebase-patches.sh origin/main --ci` does on the PR branch (the
workflow runs main's copy of the scripts with `PATCH_ROOT` set to the PR checkout,
since a PR cut before the bot existed carries a script without `--ci`):

1. Merges `origin/main` into the branch. `patches/*.patch`, `upstream.lock` and the
   `upstream` pointer keep the branch's side; step 3 rewrites all three. In
   `README.md`, `patches/README.md`, this file, `docs/CHANGELOG.md` and the
   `justfile` (the files the renumber already covers), a conflict where both sides
   only added lines at the same spot keeps main's lines and puts the branch's
   after them. Any other conflict, including a doc line both sides edited, aborts
   the merge, names the files and exits non-zero.
2. Rebases the branch's fork commits onto main's `AE_BRANCH_SHA`. A conflict there
   aborts the rebase and names the files as `upstream/<path>`.
3. Re-exports and renumbers exactly as `just patches-rebase` does. Main is now an
   ancestor of the branch, so the lines the branch added are the PR's own lines and
   main's entries are never renumbered. The branch's "after `0NNN`" note in
   `patches/README.md`, which names the patch before its first one, moves to
   main's last patch.
4. Commits the merge and then the renumbered result. It never pushes.

Then the workflow runs `just patches-check` and `just lint`, and only if both pass
hands the new commits as git bundles to a second job on a fresh runner, which
checks them against main's lock, the PR's old head and the fork's branches and
pushes: first the fork topic branch (`--force-with-lease` against the commit the PR
pinned), so the new pointer is reachable on the fork when CI checks it out, then the
PR branch (a plain push of a merge, so a head that moved during the run is refused,
and the fork branch is put back). It comments the new patch number on the PR. When
anything fails it pushes nothing and comments what needs a person, once per PR head.

The fork topic branch is the one branch on the fork whose head is the PR's pinned
`AE_BRANCH_SHA`, never `ae/vX.Y.Z`. It moves only when the PR has fork commits of
its own and the rebase moved them; then no such branch, or two, and the bot stops
and says so. It leaves the PR title and body alone, and an "after `NNNN`" line in the
PR's own docs still names the old predecessor; both are edited by hand.

### The PATCH_BOT_TOKEN secret

`GITHUB_TOKEN` cannot push to the fork `analyticendeavors/omnigent`, and a push made
with it starts no CI run, so the bot pushes and comments with a repository secret.
Only that second job sees it, and it runs no PR code; the job that runs the PR's
scripts, `justfile` and `uv sync` has only the read-only `GITHUB_TOKEN` (a separate
runner, because code on the same runner could read a later step's environment). Without the secret the workflow
warns and passes. To add it:

1. Signed in as an account with write access to both repositories, open
   https://github.com/settings/personal-access-tokens/new (fine-grained token).
2. Resource owner `analyticendeavors`; repository access "Only select
   repositories": `analyticendeavors/omnigent-ae` and `analyticendeavors/omnigent`.
3. Repository permissions: Contents read and write (the pushes), Pull requests read
   and write (the comments), Workflows read and write (a merge of main that carries
   a `.github/workflows/` change is refused without it). Metadata read comes with
   them.
4. Generate it. If the organization approves fine-grained tokens, approve it at
   https://github.com/organizations/analyticendeavors/settings/personal-access-token-requests.
5. Paste it as `PATCH_BOT_TOKEN` at
   https://github.com/analyticendeavors/omnigent-ae/settings/secrets/actions/new, and
   keep the value in Key Vault `ae-secrets-vault` as `patch-bot-token` for the next
   rotation.
6. Check it: Actions, Patch rebase, Run workflow with an open patch PR's number.

## Syncing to a new upstream release

Cadence: every upstream release tag, within a week of it.

1. `just sync vX.Y.Z`. The script fetches the tag from the real upstream, pushes
   the tag to the fork (CI only talks to the fork), checks it out in `upstream/`,
   rewrites `UPSTREAM_TAG` and `UPSTREAM_SHA`, and re-applies the series.
2. On a conflict it stops. Resolve in `upstream/`, `git add`, then
   `git -C upstream am --continue`, then `just sync --finish vX.Y.Z` (or run
   `scripts/sync-upstream.sh --finish vX.Y.Z`) to re-export and update the lock.
3. `just setup lint test upstream-smoke web-check ext-build ext-doctor image`.
   `setup` again, because `uv sync` prunes the editable AE packages and the
   test suite reuses upstream's fixtures, which move with the tag;
   `packages/ae_omni_policies/tests/conftest.py` names what it copies.
   `ext-build` matters on a sync because the page bundles upstream's
   extension SDK from `upstream/sdks/web-extension/src`: a changed SDK changes
   the committed `dist/`, and the recipe fails until it is rebuilt and committed.
4. Bump the `omnigent` pin in `packages/ae_omni_policies/pyproject.toml` and
   `packages/ae_omni_extension/pyproject.toml` when the minor changes (both read
   `>=0.18,<0.20` since the v0.18.0 sync), together with `REQUIRES_OMNIGENT` in
   `packages/ae_omni_extension/src/ae_omni_extension/plugin.py`; upstream
   rejects the extension at load when its range excludes the running version.
5. `git -C upstream push origin HEAD:refs/heads/ae/vX.Y.Z`, commit the lock,
   `patches/`, `.gitmodules` and the submodule pointer together, open the PR
   `chore(upstream): vX.Y.Z`.
6. After the merge, tag the product repo `ae-vX.Y.Z.1` (the `.N` counts AE
   releases on the same upstream tag). The tag push runs
   `.github/workflows/image.yml`, which builds the two stages as CI does, smokes
   them, and pushes `ghcr.io/analyticendeavors/omnigent-ae:ae-vX.Y.Z.1`. On the
   box: that tag into `config/server.env`, `deploy/compose.sh pull`,
   `deploy/compose.sh up -d`, then `deploy/install-host.sh` and a host restart
   so the host runs the same version as the server (`deploy/README.md`,
   "Upgrade"; Phase 6, 2026-09-23).

Record every conflict you resolved in the PR body: which patch, which upstream
change, how it was settled. That log is what keeps the patches tiny over time.

## What CI enforces

| Job | Proves |
|---|---|
| `patches` | `just lock-check`; `just patches-check` on the checked-out pointer; then reset `upstream/` to the tag, `just patches-apply`, and the applied tree must equal `AE_BRANCH_SHA` (compared by tree, since a re-applied commit gets the runner's committer identity) |
| `upstream-smoke` | `just setup` and `just upstream-smoke` (`tests/policies`, `tests/extensions`, `tests/server/test_item_observers.py` from patch P3 and `tests/server/scheduled/test_retention.py` from patch P29,
`tests/server/scheduled/test_task_project.py` from patch P33,
`tests/server/test_host_connect_observers.py` from patch P34,
`tests/server/test_identity_migration.py` (upstream's, for the function P36 extends) and `test_identity_migration_extra.py` from patches P36 and P38,
`tests/runner/test_ae_fast_mode.py` and `tests/server/routes/test_ae_fast_mode_routes.py` from patch P44,
upstream's `tests/server/test_openapi_drift.py` (since 2026-10-14),
`tests/server/routes/test_ae_fork_claude_account.py` from patch P99 (since 2026-10-18), `tests/onboarding/test_ae_shared_claude_accounts.py` from patch P117 (since 2026-10-20), the policy edit suites from P94 (`tests/server/routes/test_ae_default_policy_params.py`, `tests/server/routes/test_default_policies.py`, `tests/server/integration/test_default_policy_routes.py`, `tests/stores/test_ae_update_default_factory_params.py`, since 2026-10-18), `-m "not databricks"`) |
| `python` | `just setup`, `just lint`, `just test`, `just ext-doctor`: both AE packages' ruff pass and suites on upstream's venv and server fixtures; the extension's manifest is accepted and its bundle resolves |
| `web` | `just web-lint`: `pnpm install --frozen-lockfile --filter web` in the patched tree, then upstream's `lint` and `type-check` over the whole SPA; then `just ext-build`, which rebuilds the Parking Lot bundle and fails if the committed `dist/` differs |
| `web-test` | `just web-test <n>/4` in four parallel shards (vitest `--shard`): vitest over the suites the patches add or touch (the AE helpers and components, every `Sidebar` suite, the extension navigation map, the extension host services, the SDK page drain, and since 2026-10-14 the ChatPage composer, mention and statusLine suites, which had failed unseen since patch 0030). Split out of `web` on 2026-10-20: one run of the whole list took about 5 of that job's 10 minutes. `just web-check` still runs both halves locally |
| `image` | stage 1 from `upstream/deploy/docker/Dockerfile`, stage 2 from `deploy/Dockerfile`; `omni --version` must print the pinned version; `omni extensions list` must name `analyticendeavors.parking-lot` and `doctor` must resolve its bundle; both AE packages must import |
| `rules` | the managed block in `CLAUDE.md` matches `claude-config/BRANCH-DISCIPLINE-CLAUDE-RULES.md` in ae-claude-skills (needs the `RULES_SOURCE_TOKEN` secret; without it the job checks only the markers, warns, and passes) |

## Shallow checkouts

CI checks the submodule out at depth 1, so the tag commit and the commits of the
series can be absent. The scripts fetch the tag from the fork with a bounded
depth and deepen from the shallow boundary as far as the series needs; that is
why the sync script pushes every pinned tag to the fork.

## Conventions

- Fork branches: `ae/vX.Y.Z`, one per upstream tag we have pinned. Never rebase
  or force-push an `ae/*` branch that a merged lock points at.
- Patch commits: subject prefix `[ae-patch]`, one commit per patch, logic in new
  files so each patch touches upstream files in as few lines as possible.
- A patch that changes `web/**` re-runs `just web-check` here; upstream's own
  `E2E UI Required` gate (an LLM judge over `pull_request_target`) never runs on
  the fork, where Actions stay disabled, so the viewport checks the handoff
  names are done by hand or with Playwright and filed as screenshots under
  `docs/screenshots/`.
- Actions stay disabled on the fork; all CI runs here.

## Sync log

### v0.16.0 to v0.18.0 (2026-10-18)

Overdue sync, one jump (Reid's call, 2026-10-18; v0.17.0 skipped as a pin, the way
v0.14.0 was). Upstream moved 541 commits between the tags (254 to v0.17.0, 287 more to
v0.18.0, `a8676d23e88df7918f4f8ef1c7c4e3186e710354`). This is the first sync run with
`just sync` itself: `scripts/apply-patches.sh` now passes `--keep-non-patch` (plain
`git am` stripped the `[ae-patch]` prefix, which is why the last two syncs
cherry-picked), and `scripts/sync-upstream.sh --finish` no longer refuses the repo
state the start leaves behind. The source of the series was `patches/`, not the fork
branch: `ae/v0.16.0` was 18 commits behind main's pin, since later patches were pinned
from side branches.

Per patch: 59 of the 83 applied without a stop. 24 stopped on a conflict, and git's
three-way merge put one patch's lines in the wrong function without stopping (0049,
below). Of PR 124's four, PR 125's six, PR 127's two, PR 131's one and PR 129's one
(each carried after it merged mid-sync), two of PR 124's stopped, as did PR 129's, and
one of PR 125's hunks applied cleanly in the wrong place (`0093`, below). Every conflict is in the table, together
with every fix the series needed once it had applied, review fixes included (each
folded into the patch that owns the code):

| Patch | What moved upstream | How it was settled |
|---|---|---|
| `0007` slots in the header | `ChatHeader.tsx` dropped its React hook import and the trailing row's `MOBILE_GLASS_PILL` | Upstream lines kept; the `AeSessionChip` import and mount re-seated (still 2 lines). |
| `0012` merge cards | `GithubPanel.tsx` deleted; the PR panel is `PullRequestPanel.tsx` on the new `git_providers` layer (https://github.com/omnigent-ai/omnigent/pull/8914, https://github.com/omnigent-ai/omnigent/pull/8916); `fetchGithubInfo` in `hooks/useGithub` became `fetchPullRequestInfo` in `hooks/usePullRequests` | Git followed the rename. `AePrCards` mounts in `PullRequestPanel.tsx` after upstream's new warnings row, `AeInlinePrCards` after `BlockRenderer`'s new stream-interruption notice (still 2 lines each). The rename is applied in `AeInlinePrCards.tsx`; `0020` (`aeReviewOffer.ts`), `0071` (`aeRailQueries.ts`) and `0025`/`0027`/`0066` (host services imports) take the same rename. |
| `0019` Inbox title on phones | The Inbox card was rebuilt (https://github.com/omnigent-ai/omnigent/pull/9252): the kind column hides below `sm`, the collapsed title is `min-w-0 flex-1 truncate` | Superseded. The code hunk is dropped; the patch keeps only its test, rewritten for the new card, so the phone fit stays pinned and the numbering stays stable. |
| `0029` keep_runs | Scheduled tasks bind a per-user agent copy and delete it if the store write fails (https://github.com/omnigent-ai/omnigent/pull/8677); `DELETE /v1/sessions/{id}` now clears `_intentional_stop_sessions` with `pop` (it is a cache, no longer a set) | Upstream blocks kept whole, retention lines after them. `delete_session_server_side` takes the `pop`; the rest of the route still matches it step for step. |
| `0030` composer teams | `_forward_native_terminal_message` gained `input_delivery` and an input log call (https://github.com/omnigent-ai/omnigent/pull/9487); the lead switcher's `SwitchAgentDialog` is gone with switch-agent (see `0062`) | Both parameters and kwargs kept, the team notice after upstream's log call. Change lead opens the fork dialog. |
| `0033` task project | The routes take an `artifact_store` and define `owner_id` themselves | Both kwargs kept. The project lookup, which can 404, runs before `_bind_agent` so a bad project never leaves an orphan agent copy. |
| `0034` host-connect observers | `app.py` imports `create_harness_startup_router` at the same spot | Both imports kept. |
| `0041` light tooltips | `TooltipContent` gained a shortcut-keys gap and forced dark `kbd` chips for its near-black surface (https://github.com/omnigent-ai/omnigent/pull/8877) | The popover-surface substitutions applied to the new string; the forced dark chip colors dropped (Kbd's own default suits the popover, as `0042` decided). Still one line. |
| `0042` popover review fixes | `KeyboardShortcutHint` removed (the tooltip takes `shortcut={keys}`); the composer's "Couldn't update configuration" tooltip removed | The forced slate class dropped from upstream's version; the obsolete className hunk and its assertions dropped. After review, the `+` between shortcut key groups (new in v0.18.0) takes `text-muted-foreground`: upstream's `text-white/50` vanished on the light popover. |
| `0044` Fast mode | The native-harness control handlers moved out of `runner/app.py` into `runner/native_controls.py` (https://github.com/omnigent-ai/omnigent/pull/8856), including `_prepare_claude_native_pane_for_injection` | `NativeControls` exposes the pane helper and `app.py` binds it beside the other handlers (3 lines). |
| `0045` Fast mode fixes | The composer suite's imports changed | Import re-seated. |
| `0046` backup accounts and approvals | The Codex approval-mode handler moved to `native_controls.py` and reads the `/permissions` menu from the pane; `codex_approval_modes.py` dropped `menu_key`; `NewChatDialog` renamed `sandboxInferenceConfigured`; two new AppShell fork-source tests expect one session read | The RPC-first block ported into `native_controls.py` (the patch no longer touches `runner/app.py` there); docstring and the `isEntryConfigurable` line re-seated. The fork-source tests now expect `useSession`'s one plain retry after a failed `refreshState` read, which this patch adds. |
| `0048` accounts in Settings | Settings gained Harnesses and Custom Agents sections (https://github.com/omnigent-ai/omnigent/pull/9083, https://github.com/omnigent-ai/omnigent/pull/9408) | The AI accounts section re-seated beside them. |
| `0049` browser sign-in | No conflict. `host/frames.py`'s install-harness and store-secret decoders, and the matching two result branches in `routes/host_tunnel.py`'s receive loop, end with the same lines, and the three-way merge put the `login_*` fields on the install-harness side of both | Moved back to the store-secret side, as on the v0.16.0 pin. The decoder was caught by `tests/host/test_connect.py`; the tunnel only by the broad run, since every install-harness result then crashed the host tunnel (`test_hosts_install_harness.py`). |
| `0053` request-log records | Queued-command attachments: subagent hand-backs and task completions now become hidden `is_meta` items with `subagent_return_id` | The system-queued filter moved below upstream's hand-back branch, so those items survive and peer chatter still never renders as a user bubble. Its tests moved with upstream's to `tests/harnesses/claude_native/`. |
| `0055` Agents pane | The tree was redrawn (https://github.com/omnigent-ai/omnigent/pull/8970): a root guide line, connectors, a status avatar on the left (`StatusIndicator` removed) | Rows still render through `AeAgentsSections`, which passes each row its index so the guide runs within a section; `AeAgentRowMeta` keeps Model and Time and leaves status to upstream's avatar. |
| `0062` question dialogs and handoff | Switch-agent retired (https://github.com/omnigent-ai/omnigent/pull/8795): `POST .../switch-agent` answers 410 and `switch_conversation_agent` and `SwitchAgentDialog` are gone | Reid's call (2026-10-18): Switch harness opens upstream's fork dialog, whose route already checks a saved inference snapshot against the target harness. The switch-route, store and test changes dropped; question dialogs unchanged. |
| `0063` Model column | The Claude forwarder tests moved into `tests/harnesses/claude_native/forwarder/` | `test_ae_subagent_model.py` moved there and imports `_support`. |
| `0067` compact accounts and handoff | Same retirement | The account switch through switch-agent dropped; the label guard, the binding refresh on `session.agent_changed`, compact accounts and Codex `effort_known` kept. Moving a running session to another account is now the new `0099`. |
| `0071` session rail | The content row is wrapped in `CanvasWorkspace` (https://github.com/omnigent-ai/omnigent/pull/9573); the phone menu gained side chats | The rail mounts as `CanvasWorkspace`'s last child (outside canvas mode it renders the same flex row). The toggle suite's menu props gained the three side-chat fields. |
| `0074` activity indicator | The row padding gained an Updated-timestamp reserve; `Transcript.tsx` imports moved | The wide-marker reserve sits before it (the two never apply together). |
| `0075` rail docking | The sidebar scroller lost its `md:mr-1` gutter | `md:mr-2` added to upstream's classes for the 8 px grab strip; the test asserts both. |
| `0077` live progress | New imports and a forwarder state field at the same spots | Both kept. |
| `0085` rail toggle icon and Chat settings | `settingsNav.tsx` replaced the Customize section with Harnesses and Custom Agents | The Chat section id goes in ahead of them, where it sat before Customize. |
| `0087` rail open in every session, review fixes | `AppShell.tsx`'s two panel restores gained a canvas-mode default, a `showAgents` URL signal and the new `setRightPanelOpenImmediately` setter | Upstream's expressions and setter kept, wrapped in `aeRailRestoredPanelOpen`. Its AppShell suite imports `usePullRequests` and spreads the real `useWorkspaceChangedFiles` module, which gained exports. After review, the rail's docking check and both restores also count the canvas card's width (`canvasReservedWidth`), since the rail now sits inside `CanvasWorkspace` (`0071`); the suite's panel mock follows the rename to `PullRequestPanel`. |
| `0093` rail context review fixes | `openapi.json` is v0.18.0's | No stop, but the hunk adding the context route's `fresh` query parameter matched generic JSON lines and landed on another route. Regenerated with `scripts/dump_openapi.py`; caught by `tests/server/test_openapi_drift.py`. |

`0084` to `0087` (split view names and the rail in every session,
https://github.com/analyticendeavors/omnigent-ae/pull/124) and `0088` to `0093` (every
row's waiting state, the turn rail's last tick and the rail's context while Claude starts,
https://github.com/analyticendeavors/omnigent-ae/pull/125) and `0094` and `0095` (editing
a global policy's parameters, https://github.com/analyticendeavors/omnigent-ae/pull/127)
`0096` (the rail sliding with the workspace panel,
https://github.com/analyticendeavors/omnigent-ae/pull/131) and `0097` (the token defrag in
the composer tray, https://github.com/analyticendeavors/omnigent-ae/pull/129) merged while
this sync ran and were carried onto v0.18.0 the same way. PR 131's patch touches only AE
files, identical in both series, and applied unchanged. PR 129's stopped on three one-line
conflicts, each keeping both sides: imports in `StatusBlocks.tsx`, upstream's new
`subagent_activity` branch beside the compaction marker's `itemId` in
`chatBubbleParts.tsx`, and upstream's new archive-worktree key beside
`AE_TOKEN_DEFRAG_KEY` in `settingsPortability.ts`. The three upstream behaviors its note
below asks a sync to re-check still hold on v0.18.0. PR 127's `PoliciesPage.tsx` hunk took
a three-way merge around an import v0.18.0 added; its server patch has four twin-shape
hunks (the create and update paths of the schema, the store and `openapi.json` look
alike), each checked to sit on the update path as on v0.16.0. Two new patches ride along.
`0098` (2026-10-18, Reid's request) puts the session rail's Absorbed and Unfiled counts
side by side. `0099` (2026-10-18, Reid's call) picks the Claude account when forking a
Claude session. The fork request takes `claude_account` (at most 64 characters, like the
host's account names); `routes/sessions/ae_fork_claude_account.py` checks it with the
rules the switch route used (a Claude native target with no bound provider credentials,
the caller's own connected local host, a detected Claude login) and the fork stamps
`omnigent.claude_native.account` through its extra labels. An external fork binds its host
after the fork request, so the checks run against the source's host and the dialog offers
the row only for a fork that stays there. Binding a fork elsewhere still needs the caller
to own that host (the runner launch route), and a launch there fails with "The selected
Claude account is unavailable on this host." if it lacks the account. The row hides for a
sandbox fork and on a host signed in with provider credentials, and a pick drops when its
row hides or the host stops listing it.

Upstream now supersedes: `0019` (above; propose dropping it in a pass that renumbers the
series). Split view is still not upstream: https://github.com/omnigent-ai/omnigent/pull/6758
is open and marked WIP, so `0005` and `0010` stay.

What the next sync should watch:

- Silent mis-merges like `0049`'s happen where upstream code has twin shapes. This sync ran
  the upstream suites the series touches (`tests/server`, `harnesses`, `runner`, `inner`,
  `onboarding`, `host`, `entities`, `stores`, `extensions`, `policies`) on the patched tree
  and on pristine v0.18.0 and compared the failures; do that again, with `--timeout 120 -p
  no:rerunfailures` (with upstream's 300-second timeout and flaky reruns, a few hung tests
  kept a run going past 45 minutes). Remove `/tmp/pytest-of-reid/pytest-*` after a killed
  run: three killed runs used up `/tmp`'s inodes on 2026-10-18. A scan for hunks whose
  context and removed lines match more than one place in the file they patch finds the
  candidates in seconds; this sync's were `0049`'s three, `0093`'s `openapi.json` hunk
  and `0094`'s four, each checked by hand.
- Ten upstream tests fail on the patched tree by design, as they do on the v0.16.0 pin:
  forwards that assert their exact content meet `0030`'s team notice
  (`test_session_resources.py`, six cases, four of them new in v0.18.0;
  `test_sessions_child_sessions.py`, two), the Claude terminal env gains
  `OMNIGENT_CLAUDE_ACCOUNT` (`test_app_sessions_native_terminals_autocreate.py`), and the
  Claude hook imports `httpx` (`test_claude_native_message_display_hook.py`'s import-light
  check).
- Four `tests/host/test_connect.py` cases fail on pristine v0.18.0 too on the box: they read
  the box's real Claude setup.
- `AePrCards` assume GitHub; with `git_providers` a GitLab or Azure DevOps session hands
  them non-GitHub URLs.
- The Agents pane fits upstream's avatar plus the Model, Time and Stop columns in a narrow
  rail; the rail sits in the conversation pane in canvas mode. Each section draws its own
  root guide, so the line breaks between sections, and rows show no status word (the
  avatar carries status); check both against the live pane.
- A hunk with generic context can apply cleanly in the wrong place: `0093`'s `openapi.json`
  hunk did. Run the drift test after carrying patches.

Tooling: the box pushed the `v0.18.0` tag to the fork without trouble. The sync ran in its
own worktree with its own submodule clone, because the script's depth-50 fetch would make
the module repo the other worktrees share shallow. `just` and `pnpm` are not on the box's
PATH; `uvx --from rust-just just` and `corepack pnpm@11.15.1` stand in. The omnigent pins
moved to `>=0.18,<0.20`.

### v0.15.0 to v0.16.0 (2026-10-05)

Weekly sync. The fork branch `ae/v0.16.0` was rebuilt by cherry-picking the 28
`[ae-patch]` commits from `ae/v0.15.0` in order onto `v0.16.0`
(`82a74473ee4c01163c4269f4296b10a158f0e47e`).

Per patch: 27 of 28 applied clean (git's three-way merge absorbed the context
drift in 0001, 0004 to 0007, 0011, 0012, 0015 to 0018; the exported patches
changed only in hunk offsets and context lines). One conflict:

| Patch | Result | What moved upstream, and how it was settled |
|---|---|---|
| `0003` item observers | conflict in `orchestration.py`, resolved | `_persist_external_conversation_item` reworked its pending-input bookkeeping (`_restore_drained_inputs` and `_release_drained_inputs` replace the inline Kiro restore loop, and `skipped_kiro_pending` became `skipped_pending`). Our hunk inserted `notify_items_persisted` between the dedup early return and that loop. Settled by keeping upstream's block whole and putting the one call line first, after the dedup return, where it was. Still two lines in the file (import and call). |

Upstream features that overlap ours, and what was kept: nothing in v0.16.0
supersedes a patch or an out-of-tree feature; the PR body's "Upstream now
supersedes" section has the per-entry list. To watch:

- Batched sub-agent transcripts (https://github.com/omnigent-ai/omnigent/pull/8240)
  add `_persist_external_conversation_items` in `orchestration.py`, which calls
  `conversation_store.append` directly and never reaches 0003's hook. Items in
  claude-native sub-agent batches are invisible to the AE observer. Fixed by
  `patches/0031` (2026-10-13).
- Idle native sessions are now reaped even with a tab open
  (https://github.com/omnigent-ai/omnigent/pull/8320), so a pane left untouched
  in the split view can be reaped while on screen.
- Split view: still none upstream in v0.16.0; `0005` and `0010` stay.

Tooling: the session's git proxy again would not put the `v0.16.0` tag on the
fork (the push answered "Everything up-to-date" and `git ls-remote` shows no
tag); the sha fallback in `_lib.sh` covers it. `deploy/docker/entrypoint.py`
and `tests/server/conftest.py` are unchanged between the tags, so the AE copies
that mirror them stay as they are. The omnigent pins moved to `>=0.16,<0.18`.

### v0.13.0 to v0.15.0 (2026-09-24)

Upstream moved 553 commits between the tags (v0.14.0 was skipped as a pin; the
series went straight onto v0.15.0). The fork branch `ae/v0.15.0` was rebuilt by
cherry-picking the six `[ae-patch]` commits from `ae/v0.13.0` in order, which
keeps the subject prefix `git am` would strip.

Per patch:

| Patch | Result | What moved upstream, and how it was settled |
|---|---|---|
| `0001` status icons, slots pill | applied clean; test harness fixed | `Sidebar.tsx` changed by about 960 lines, none on our three insertion points. The sidebar now reads its rows through `SidebarDataProvider` (`web/src/hooks/useSidebarData.tsx`) and a scope cache (`useScopeCache`), so `Sidebar.aeStatus.test.tsx` failed 16 of 16 with "SidebarDataProvider is required". Fixed in the P1 commit the way upstream's own Sidebar suites do it: the provider around the render, and `@/hooks/useScopeCache` mocked with `@/test/mockScopeCache`. The slots pill still reads `["conversations", ...]`: the scope cache's key is `["conversations", "", false, null, visibility]` with the same page shape, so no code change; the suite now seeds that exact key. |
| `0002` `server.request`, `ae.*` labels | applied clean | The SDK's `sessions.ts` gained a stale-cursor restart and the host's session list query now sends `visibility=all`. No overlap with the labels or the allowlist. The Parking Lot bundle bundles the SDK, so `dist/` was rebuilt. |
| `0003` item observers | applied clean | `helpers.py` and `orchestration.py` changed by 1,400 and 1,700 lines; `_persist_external_conversation_item` and `_flush_relay_text` kept their shape and the two-line call sites landed where they were. |
| `0004` Status submenu | applied clean | `ConversationMenuItems` kept its `C` menu primitives; the submenu still sits above Stop / Archive / Delete. |
| `0005` split view | applied clean, kept | https://github.com/omnigent-ai/omnigent/pull/6758 has not landed: no split view in v0.15.0 (the only "split" strings are the diff viewer's layout toggle). `App.tsx` wrapped its routes in a `ChunkLoadErrorBoundary`; the `/split` route line merged inside the renamed `AppRoutes`. |
| `0006` plan limits popover | conflict in `ChatPage.tsx`, reworked | v0.15.0 moved the context ring out of `ComposerStatusLine` into the composer's workspace bar as `ComposerContextRing`, which is itself a `<button>` inside a tooltip, and `ChatPage.composer.test.tsx` pins the ring's parent to the bar's `gap-1` row. Wrapping it (the v0.13.0 approach) nests a button in a button and breaks that upstream test. The popover now renders no element beside the ring: it listens for clicks on `[data-testid="composer-context-ring"]` and anchors to the clicked ring through Radix's `virtualRef`. `ChatPage.tsx` went from six lines to two. Its suite renders upstream's real ring. |

Upstream features that overlap ours, and what was kept:

- Per-session pull request tracking (https://github.com/omnigent-ai/omnigent/pull/6935):
  the runner records the pull requests a session opens (`omnigent/runner/pr_observer.py`,
  `session_prs.py`) and serves them as session resources. Kept Your move's pull
  request section (`ae_omni_policies.github`): it answers a different question
  (every pull request waiting on Reid across repos, with CI state), and no patch
  duplicated upstream's.
- Latest-message errors in the sidebar (https://github.com/omnigent-ai/omnigent/pull/7251):
  upstream's error state renders through `SessionStateBadge`, the row's trailing
  indicator; P1's `ae.status` icon sits before the title. Both kept; they do not collide.
- Fuzzy session finder, Cmd+Option+S (https://github.com/omnigent-ai/omnigent/pull/7226):
  no AE equivalent; nothing to drop.
- Approval prompts offering auto mode (https://github.com/omnigent-ai/omnigent/pull/6926):
  upstream's Inbox and approval flow. Your move counts sessions with
  `pending_elicitation_count > 0`, which is upstream's own field, so it follows
  upstream's approvals rather than duplicating them. Kept.
- `deploy/docker/entrypoint.py` still passes neither `policy_modules` nor
  `debug_router_modules` to `create_app`; `ae_omni_policies.docker_entrypoint` stays.

Tooling changes the sync needed:

- The session's git proxy refused the tag push (`git push origin refs/tags/v0.15.0`
  answered HTTP 403) while branch pushes worked, so the fork had no `v0.15.0` tag.
  `scripts/_lib.sh` (`ensure_tag_commit`) and the `patches` job in `ci.yml` now fall
  back to fetching `UPSTREAM_SHA` by sha, which GitHub serves because the tag commit
  is an ancestor of `ae/v0.15.0`; `sync-upstream.sh` warns instead of stopping when
  the tag push fails. Pushing the tag to the fork from a machine that can is still
  the tidy state: `git -C upstream push origin refs/tags/v0.15.0:refs/tags/v0.15.0`.
- Upstream's `uv.toml` now sets `exclude-newer = "P7D"`, a duration uv 0.8 cannot
  parse ("failed to parse year in date"). `just setup` needs a current uv (0.12.18
  worked); CI's `setup-uv` installs the latest.
- The omnigent pins moved to `>=0.15,<0.17` in both pyprojects and
  `REQUIRES_OMNIGENT`; the `mcp` pin already matched upstream's `mcp>=1.0,<2`.

What the next sync should watch:

- `ComposerContextRing`'s test id. P6 finds the ring by it; upstream's composer
  suites pin it today, and P6's own suite fails if it changes.
- The sidebar data layer (`useSidebarData`, `useScopeCache`) is new in this range
  and still moving. If the scope cache stops using the `["conversations", ...]`
  key, the slots pill reads zero with no test failing in upstream's suites; P1's
  suite seeds the key by hand, so re-check it against `useScopeCache.ts`.
- The extension host's session list now asks for `visibility=all`, so the Parking
  Lot and Your move see sessions shared with the viewer as well as their own.
  Harmless on a single-user box; revisit if the box ever has a second user.
- https://github.com/omnigent-ai/omnigent/pull/6758 (split view) is still a draft.
  When it ships, drop `0005`, `0010` (its layout presets, 2026-09-24) and the P5
  cases in `Sidebar.aeStatus.test.tsx`; check upstream's view for the presets
  (side by side, stacked, 2+1, grid) before dropping them.
- `just upstream-smoke` hung once in `tests/server/test_item_observers.py`
  (`drain_item_observers`, the 300 s pytest timeout) on the first run after
  `just setup`, then passed on seven reruns, cold caches included. Not
  reproduced; if CI shows it, the lead is an observer task that never finishes
  in `asyncio.to_thread`.

Context review hardening (2026-10-16), patch 0057: isolates the temporary MCP bridge,
requires edit access for probes, preserves launch restrictions, bounds repeated
failures, and refreshes expanded snapshots. The runner and route suites cover
these boundaries. Exporting the concurrent fork histories renumbered the four
intervening patches to 0052 through 0055; their commits are preserved. The later
terminal sign-in parsing fix is patch 0056.

Patch 0058 (2026-10-16) rejects malformed context categories and nonfinite counts,
isolates each bridge separately, and prevents overlapping snapshot refreshes.
Unknown bridge launch wrappers report unavailable rather than starting a probe.

Patch 0064 (2026-10-16) adds a wrapping context window color legend inside `AeCategoryBar` in `AeContextCategories.tsx`. It shares swatches with the bar and excludes deferred tools.

Patch 0065 (2026-10-16) uses those same swatches in the detailed category rows and tests free-space and buffer consistency. The existing Agents pane model patch stays at 0063.

Patch 0097 (2026-10-18), the token defrag, leans on three upstream behaviors that a
sync should re-check. The chat store keeps `compaction_loading` blocks after the
`compaction` block lands (only `buildBubbles` drops them), so the strip counts a run as
on screen while a loading block is newer than the newest marker. How a run ends comes
from the AE stream tap, which sees `compaction_completed` and `compaction_failed` before
`handleSessionEvent` (so it also reads the ring's count before the store replaces it);
the session stream has no replay, so each end arrives once. And live blocks carry
`ctx.clientCreatedAtS` (stamped in `blockStream.ctx`): a marker's before and after
counts are the ones noted at or before that stamp. If any of these move, the counts
quietly disappear or the strip stops without its finish; `aeTokenDefrag.test.ts` and
`AeTokenDefrag.test.tsx` pin the first and third, and the second rests on
`tapSessionEvents` in `chatStore.ts`.

Patch 0123 (2026-10-20), prompt suggestions, leans on Claude Code's CLI and on two forwarder
spots a sync should re-check. The side call's isolation is only as good as its flags
(`--safe-mode`, `--setting-sources ""`, `disableAllHooks`, `--strict-mcp-config`, `--tools ""`,
`--no-session-persistence`): after a Claude Code upgrade, run `claude --help` for each and
repeat the marker-file check `patches/README.md` describes, because a hook that fires from
the side call would start another one. If Claude Code ships suggestions in the interactive
TUI (drawn or written to the transcript), drop the side call and relay those instead. In
`forwarder.py` the hook sits on the `Stop` idle edge after `post_external_session_status`,
on the parent `UserPromptSubmit` branch and in the loop's two `CancelledError` exits; if
upstream moves any, the `test_ae_prompt_suggest.py` hook-up cases fail. The reply text is
the `Stop` payload's `last_assistant_message`, carried on `ClaudeHookRecord`; if Claude Code
drops that field the transcript tail takes over. The private config dir relies on Claude
Code reading `.credentials.json` from `CLAUDE_CONFIG_DIR`; check one real call after an
upgrade, as `patches/README.md` describes.

Patch 0128 (2026-10-20), prompt suggestions in every session, adds two more spots to
re-check. Named Claude accounts resolve through `claude_accounts.account_env`; if upstream
changes how the launch puts an account on the terminal (`account_env_for_launch` in
`claude_native/main.py`), change `account_login_env` in `harnesses/ae_prompt_suggest_core.py`
to match. In the Codex `forwarder.py` the hook sits after `_post_turn_status_edge` in
`_handle_terminal_turn_boundary_inner`, in the `turn/started` branch of
`_maybe_handle_turn_event`, at the top of `_handle_completed_item_inner`, in
`_ensure_user_message_posted`, after `note_parent_rotation` and in `supervise_forwarder`'s
`finally`; if upstream moves any, the Codex `test_ae_prompt_suggest.py` cases fail. If Codex
stops sending a turn's items before its `turn/completed`, the noted text goes stale and
only the notification's own `turn.items` is left.

Patch 0132 (2026-10-20), suggestion alternatives, keeps its own list of the app's hotkeys
(`RESERVED` in `web/src/lib/aeSuggestionKeys.ts`) so a rebound cycle key cannot take one over.
When a sync adds or moves a hotkey in `web/src/hooks/use*Hotkey.ts`, add it there too;
`aeSuggestionKeys.test.ts` pins the current ones. `ReplyDraftBlocks.tsx` types its props from
`ComposerTextInput`, whose `aeSuggestion` prop is now a plain string with `onAeCycle` beside it.
