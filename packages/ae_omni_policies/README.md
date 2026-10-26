# omnigent-ae-policies

The Analytic Endeavors branch-discipline layer for OmniGent, as a Python package that
plugs into seams upstream already ships. Nothing here edits upstream code.

| Module | What it is |
|---|---|
| `labels.py` | the `ae.*` label vocabulary (`docs/LABELS.md` documents it) |
| `slot_cap.py` | the slot rule as a request-phase policy: first prompt claims a slot, and once `AE_SLOT_LIMIT` are working (three by default) the next is held (`warn`) or dropped (`block`) with the working sessions named, `ride unparked` runs one more anyway |
| `hurry.py` | `triage off` / `triage capture` / `triage on` as the `ae.triage` label |
| `observer.py` | the capture side of the gate: reads the assistant's persisted turns through patch P3's item-observers registry and writes `ae.gate`, `ae.fork`, `ae.absorbed`, `ae.unfiled` (each counted line also as `ae.absorbed.<n>` or `ae.unfiled.<n>`, the first 20 of each kind, written under a lock since batches overlap, 2026-10-19), `ae.next` and a glyph-mapped `ae.status`; never gates; `observe` is the request-phase fallback on an unpatched upstream |
| `fork_suspect.py` | a second-model hint through the server's optional `llm:` block: ALLOW plus `ae.fork_suspect=<hash>` when the model says a message starts a second deliverable; abstains with no model, on short messages, under `ae.triage` and while a gate is open; `annotate_prompt` appends one line to the user text |
| `rabbit_hole.py` | one ASK per session past `hours` since `ae.started` or `turns` prompts; accept acknowledges for the session; `hours: 0` disables |
| `sessions.py` | top-level session listing and summaries shared by the policy and the routes; a blocked or failed session holds no slot (failed since 2026-09-25) |
| `notes.py` | the Parking Lot note, ported from the task-store worker (`ae-claude-skills` `task-store-worker/src/index.ts`) |
| `taskstore.py` | task-store over Cloudflare Access: `park`, `list_parked`, the worker's failure modes |
| `your_move.py` | `GET /v1/ae/your-move`: sessions that need Reid (open gate, an approval outstanding, `blocked`, `review`), lot items resurfacing today or overdue, the steward's latest brief and pull requests, each source isolated, the answer cached five seconds and the pull requests sixty (2026-09-24) |
| `github.py` | GitHub over an optional fine-grained token: open pull requests by or awaiting `AE_GITHUB_LOGIN` with their CI state; a pull request's reviews, check runs, statuses and the repo's merge methods; the merge itself, pinned to a SHA; without a token the sections say how to turn it on (2026-09-24) |
| `fabric.py` | The Admin Center's Fabric section (2026-10-16 as Fabric Admin): one Azure Logic App per Fabric capacity, found from `AE_FABRIC_WAKE_URL_<NAME>`; its state (cached ten seconds), wake, and suspend with the capacity's own key, sent as the `x-suspend-key` request header (never in the URL); every call has one 8 s total deadline; the URL and the key never reach a response, an error or a log line (requests skip httpx's client, which logs full URLs). Second pass, 2026-10-16: the probe's optional details (SKU, region, ARM id, price an hour, last resume, window hours), each shape-checked or null so an old-shape answer still reads, plus `sleepsAt`, when the hourly runbook should suspend an idle Active capacity (`sleeps_at`), and keep awake for 1 to 12 hours with the same key. Third pass, 2026-10-16: keep awake has its own `keepAwakeUntil`; `sleeps_at` is the one copy of the runbook's rule (quiet-mark gaps 45 to 100 minutes, 5 minutes of slack for late jobs), and `GET` also returns `keepAwakeOptions` (2, 4 and 8 hours, projected by it) and `wakeBilledHours`; wake, suspend and keep awake answer the full row; `capacityId` is rebuilt in ARM's canonical casing or dropped, to match the host's link allowlist. Capacity fleet, 2026-10-21: with `AE_FABRIC_CAPACITIES_URL` and `AE_FABRIC_CAPACITIES_KEY`, `GET` lists every capacity the shared `la-fabric-capacities` Logic App finds (a POSTed JSON body, the key in the `x-capacity-key` header, same transport, deadline and scrubbing), sorted by name, each with `subscriptionName`, `resourceGroup`, `hourlyUsd` (SKU capacity units times `AE_FABRIC_USD_PER_CU_HOUR`), `canScale` and `skuChoices`; a name that also has a wake URL merges its Logic App's details when its probe answers in time (run at once with the list; a failed probe leaves the details out, never the row). Pause, Start and resize go through the fleet; keep awake stays per capacity. Without the two secrets nothing changes |
| `cost_targets.py` | Cost targets on the Admin Center's Azure card (2026-10-25): limits Reid sets on spend (month to date, latest finished day, month-end forecast; for all of Azure, one subscription, one service or one resource) and on how long a Fabric capacity has been Active. Judged only against the reading `azure_cost.py` already holds and fabric.py's read-only fleet reading, so it adds no Cost Management query and never touches a capacity. A crossed limit files one task-store task per rule and period with a phone alarm (09:00 on the home clock for spend, minutes out for Fabric) and closes it when the value falls back (a spend task is not closed when the period rolls, only forgotten, so last period's record stays open for a person to tick); a rate-limited, stale or missing reading is `cannot-evaluate` and files nothing. Routes beside `/azure-cost` (admin only): `GET /azure-cost/targets`, `PUT` or `POST /azure-cost/targets/{id}` and `DELETE` or `POST /azure-cost/targets/{id}/delete` (the extension host sends only GET, POST and PATCH). Defaults are seeded once |
| `prs.py` | the PR cards (2026-09-24): which pull requests the sessions track (upstream's per-session registry, read through the loopback), a card per pull request (CI counts, mergeability, reviews, merge methods) and the merge gate, cached a minute |
| `router.py` | `/v1/ae/health`, `/capacity`, `/your-move`, `/lot`, `/lot/park`, `/lot/tags`, `/lot/{id}/link`, `/lot/{id}/resume`, `/lot/{id}/session` (start a session with the task's block, 2026-09-24), `/gate/{session_id}/reply`, `/prs`, `/prs/{owner}/{repo}/{n}`, `/prs/{owner}/{repo}/{n}/merge` (2026-09-24), `GET` and `POST /sessions` (the orchestrator's sessions: list them, or start one holding a slot first, idempotent by `ae.orch_key`; 2026-09-25), `GET /fabric`, `POST /fabric/{name}/wake`, `POST /fabric/{name}/suspend` and `POST /fabric/{name}/keepawake` with `{"hours": 1..12}` (Fabric Admin, 2026-10-16; keep awake from the second pass the same day, 409 without the suspend key, 422 outside the hours; `/capacity` is still the session slots), `POST /fabric/{name}/scale` with `{"sku": "F8"}` (the capacity fleet, 2026-10-21; 400 for a size outside F2 to F64 before any call, 409 without the fleet's secrets or for a name two capacities share, 404 for a name the list lacks), `GET` and `PUT /sidebar` (the caller's manual sidebar order with its revision; 409 with the current order when another device saved first; 2026-10-19), `GET` and `POST /deploy/hold` with `{"hours": 1..3}` and `POST /deploy/resume` (the Admin Center's deploy hold, 2026-10-19; 422 outside the hours, 500 when the hold file could not be written) |
| `mcp.py` | the stdio MCP server (`python -m ae_omni_policies.mcp`): the worker's `park_fork`, `link_issue`, `list_tags`, `list_parked` unchanged, plus `omni_capacity`, `omni_set_status`, `omni_transcript` (Phase 5) and the orchestrator's `create_session`, `post_message`, `schedule_checkin`, `list_prs` and `ask_reid` (an MCP elicitation, the pop-up; 2026-09-25); every tool an HTTP call to the OmniGent server except `ask_reid` and the `gh` half of `list_prs`, every failure a text answer; `WRITING_TOOLS` names the ones that change something |
| `omni.py` | the server URL (`OMNIGENT_SERVER_URL`, else the server `omnigent login` recorded under `~/.omnigent/`) and its token (`OMNIGENT_TOKEN`, else the stored login) as one `httpx` client, shared by the MCP server and the seeder (Phase 5; login fallback 2026-09-24) |
| `retention.py` | scheduled-run retention (2026-10-13): after a task fires, deletes its run sessions past the newest `keep_runs` (5 by default, a field on `/v1/scheduled-tasks`) when they are idle, `reference` or archived, free of approvals and open gates, and still titled with the task's name; registered on import through patch P29's fire observers, and never deletes anything without that patch |
| `schedules.py` | the "AE steward" and "AE shutdown note" scheduled tasks as data, seeded by name through `POST /v1/scheduled-tasks` (`scripts/seed-schedules.py`, `just schedules-seed`) (Phase 5); the orchestrator's "AE orchestrator check-in" row, which the orchestrator writes itself (2026-09-25) |
| `triage_notes.py` | the one-time backfill of those notes (2026-10-19): reads the history of each session the caller owns through the API and saves the note behind every counted line that has none, only when the history holds exactly as many lines as the count, never changing a count or a saved note, and reporting a session it cannot read or write without stopping (`scripts/backfill-triage-notes.py`, `just triage-notes-backfill`) |
| `gh.py` | the GitHub CLI on the host, read only (`gh auth status` by exit code, `gh pr list` as cards) for `list_prs`; missing or logged out is a sentence with the fix, never a crash (2026-09-25) |
| `auth.py` | `require_ae_user`, upstream's per-route auth with a provider built from the same environment |
| `registry.py` | `POLICY_REGISTRY`, what `policy_modules` scans |
| `policy_seed.py` | seeds Settings > Global Policies from `ae_seed_policies` when the policy store holds none of its policies, matched by name or handler (2026-10-18): each entry checked the way the page's create route checks one, its factory built once, inserted in config order in one transaction; called by `docker_entrypoint` |
| `sidebar_prefs.py` | each user's manual sidebar order for the web's Manual ordering (2026-10-19): at most 500 session ids with a revision, kept in `AE_SIDEBAR_STATE_FILE` and written atomically, like the plan-limits snapshot |
| `role_prefs.py` | each user's role presets for Settings > Roles (2026-10-23): at most 30 named presets, each a model and effort (or Auto) for the five roles lead, planner, reviewer, builder and helper plus the model change rules, and which one is the default, with a revision, kept in `AE_ROLES_STATE_FILE` and written atomically. A user who never saved reads the four starters (Save tokens, Balanced as the default, Solo, YOLO). Each preset carries a Helpers rule (`any` or `own`, absent read as `any`, 2026-10-23); a user who saved before the Solo starter existed gets it appended once, tracked in `starters_seen`. `GET` and `PUT /v1/ae/roles`; patch 0162's lead notice reads the session owner's presets through it |
| `shared_accounts.py`, `shared_accounts_routes.py` | shared Claude accounts (2026-10-20): company-paid `claude setup-token` logins the admin keeps on the server for every user's account picker, so a second user's host needs no copied token. Name, label, who and when, the token Fernet-encrypted under `AE_SHARED_ACCOUNTS_KEY`; `GET /v1/ae/claude-accounts/shared` lists names and labels to anyone signed in, `POST` adds and `.../{name}/label` and `.../{name}/delete` change them for admins (the server's own `/v1/me`), and `.../{name}/token` hands a token to a host's runner at launch, refusing any browser request (`Sec-Fetch-*`) and reading identity without the cookie. Registers patch 0117's fork hook |
| `merge_watch.py`, `continuation.py` | the merge watcher (2026-10-21): every 10 seconds, one conditional GitHub read (ETag, so an unchanged pull request costs no rate limit) per open pull request attached to a session; on a merge or a close without merging, one note posted into each session that tracks it, as its owner, through `POST /v1/sessions/{id}/events`, so an idle session continues by itself. State in `/data/ae-merge-watch.json` (`AE_MERGE_WATCH_FILE`), so a restart neither repeats a note nor misses a merge made while it was down. The note starts `Merge watcher: PR #n (`, which the slot cap, the rabbit hole and the fork classifier let through on a session that already has a status (`continuation.py`). Runs from the app's lifespan through a route-less router in `DEBUG_ROUTERS`; `AE_MERGE_WATCH=0` turns it off |
| `auto_archive.py` | the auto-archive sweep (2026-10-24): Reid closes a finished chat by setting it to `reference` and it then sat in his session list. Every 60 seconds an idle top-level session (no running turn, sub-agent or background shell, no approval outstanding, no open gate) that has been in reference for `AE_AUTO_ARCHIVE_MINUTES` (default 10) is archived through `PATCH /v1/sessions/{id}` as its owner, the route the web's Archive button uses. The first-seen clocks live in `/data/ae-auto-archive.json` (`AE_AUTO_ARCHIVE_FILE`) and reset when a session leaves reference or is busy; a chat updated within the wait, a session shared with anyone besides its owner, and one unarchived after a sweep archived it (until its status leaves reference and returns) are left alone; one sweeping process (flock). Runs from a route-less router in `DEBUG_ROUTERS`; `AE_AUTO_ARCHIVE=0` turns it off |
| `slot_limit.py` | the slot limit every count reads (2026-10-22): the limit the enabled slot cap policies on Settings > Global Policies hold at (each its own `limit`, or `AE_SLOT_LIMIT` when it sets none; the lowest wins), else `AE_SLOT_LIMIT`; `GET /v1/ae/capacity`, `GET /v1/ae/sessions` and the orchestrator's slot hold use it, so the web UI's counts follow the page. Reads the policy store `docker_entrypoint` hands it on every call; any failure, or no store under `just dev`, falls back to `AE_SLOT_LIMIT` |
| `deploy_hold.py` | the deploy hold (2026-10-19): a pause on the box's automatic deploys for 1, 2 or 3 hours that ends on its own, replacing a stopped auto-update timer nobody remembered to start again; one hold with who set it, kept in `AE_DEPLOY_HOLD_FILE` and written atomically; `python -m ae_omni_policies.deploy_hold` prints `none` or `held <until> <user>` for `deploy/auto-update.sh`, which deploys anyway when the hold cannot be read |

## How it loads

`config/server.yaml` at the repository root:

```yaml
policy_modules: [ae_omni_policies]
debug_router_modules: [ae_omni_policies.router]
ae_seed_policies:
  ae_slot_cap: {type: function, handler: ae_omni_policies.slot_cap.slot_cap, factory_params: {mode: warn}}
  ae_hurry_modes: {type: function, handler: ae_omni_policies.hurry.hurry_modes}
  ae_fork_suspect: {type: function, handler: ae_omni_policies.fork_suspect.fork_suspect, factory_params: {min_chars: 40, annotate_prompt: false}}
  ae_rabbit_hole: {type: function, handler: ae_omni_policies.rabbit_hole.rabbit_hole, factory_params: {hours: 4, turns: 60}}
  ae_observer_fallback: {type: function, handler: ae_omni_policies.observer.observe}
```

Settings > Global Policies owns the policies since 2026-10-18. Under `policies:`
upstream ran them from the image and the page showed them read-only, with no switch,
delete or edit; Reid asked to manage them there. Now, when the container starts
against a policy store holding none of the seed's policies (matched by name or by
handler, so one renamed on the page still counts), `policy_seed.seed_policies`
(called from `docker_entrypoint`) inserts `ae_seed_policies` in order, in one
transaction. A store holding any of them is left alone, so a policy deleted on the
page stays deleted, and a change made there (switched, deleted, or its parameters
edited, patches 0094 and 0095) survives deploys. Global policies made on the page
with other handlers play no part. Three consequences:

- Editing `ae_seed_policies` changes only a brand-new server.
- Deleting every seeded policy on the page brings the seed back at the next
  restart; switch them Off instead.
- `just dev` runs `omnigent server` directly, not the container entrypoint, so a
  local server starts with no AE policies. Add the ones a local test needs on the
  page (Add policy lists them).

The per-session policy list (`GET /v1/sessions/{id}/policies`) shows config-file
defaults as `admin` rows and leaves out the store's global policies, so the AE
policies no longer appear there; nothing in the web UI reads those rows.

Importing the package registers `observer.observe_items` with
`omnigent.server.item_observers` (patch P3). On an upstream without the patch the
import still succeeds, `ae_omni_policies.OBSERVER_REGISTERED` reads `False`, and the
`ae_observer_fallback` policy scans the session's newest items at each prompt instead;
with the patch it abstains.

The classifier needs the server's `llm:` block (`config/server.yaml` carries two
commented examples: an Anthropic model, and Ollama through its OpenAI-compatible
endpoint). Without one, `event["llm_client"]` is `None` and `fork_suspect` abstains;
nothing else here uses a model.

`just setup` installs the package editable into upstream's venv; the image installs it
in stage 2 (`deploy/Dockerfile`).

## Environment

| Variable | Meaning |
|---|---|
| `AE_TASK_STORE_URL` | task-store origin; unset, the Parking Lot and its tools are off (no default origin) |
| `AE_CF_ACCESS_CLIENT_ID`, `AE_CF_ACCESS_CLIENT_SECRET` | the Cloudflare Access service token; without both, `/v1/ae/lot*` answers 503 |
| `AE_TASK_STORE_TZ` | IANA zone for resurface dates, the lot's "today" and the seeded schedules' clock; default `UTC` since 2026-10-22 (it was `America/Los_Angeles`) |
| `AE_SECRET_ALERTS` | `off` stops the expiry alerts (`secret_alerts.py`); they also stay off without a task store |
| `AE_SECRET_ALERT_LIST` | the task-store list expiry alert tasks land in, by name; unset, no list |
| `AE_SECRET_ALERT_TIME` | the alarm's time on the `AE_TASK_STORE_TZ` clock, `HH:MM`, default `09:00` |
| `AE_SECRET_ALERTS_FILE`, `AE_SECRET_OWNERS_FILE` | where the sent alerts and the secret owners are kept; default `/data/ae-secret-alerts.json` and `/data/ae-secret-owners.json`, empty for memory only |
| `AE_AUTO_ARCHIVE`, `AE_AUTO_ARCHIVE_MINUTES`, `AE_AUTO_ARCHIVE_FILE` | the auto-archive sweep (2026-10-24, so a chat Reid closes as `reference` leaves his list on its own): `0` turns it off; minutes in reference before the archive, default `10`; where the first-seen clocks live, default `/data/ae-auto-archive.json` when `/data` exists, else memory; set empty for memory only |
| `AE_SLOT_LIMIT` | working sessions allowed at once, default `3`; read once at startup, and the one setting since 2026-09-24: the slot cap uses it (`config/server.yaml` passes no `limit`) and `GET /v1/ae/capacity` reports it as `limit`, which the web UI's counts follow. Not a whole number of one or more: the import raises and the server does not start |
| `AE_SELF_URL` | optional; the gate reply, Resume's session create and the PR scan call the server through a real HTTP loopback to this base instead of in-process |
| `AE_RESUME_AGENT` | optional; the built-in agent a resumed session binds to, default `claude-native-ui` (Claude Code) |
| `AE_RESUME_HOST` | optional; the host (name or id) a resumed session runs on; unset, the only online host, and none when two are online |
| `AE_RESUME_WORKSPACE` | optional; the absolute workspace for a resumed session; unset, the host's home directory as the host resolves it |
| `AE_LIMITS_STATE_FILE` | optional; a JSON file mirroring the newest plan-limits snapshot per harness, so a restart keeps the last reading (`docs/PLAN-LIMITS.md`); unset, memory only |
| `AE_SIDEBAR_STATE_FILE` | optional; the JSON file holding each user's manual sidebar order (`GET` and `PUT /v1/ae/sidebar`, 2026-10-19); unset, `/data/ae-sidebar.json` when `/data` exists, else memory only |
| `AE_ROLES_STATE_FILE` | optional; the JSON file holding each user's role presets (`GET` and `PUT /v1/ae/roles`, 2026-10-23); unset, `/data/ae-roles.json` when `/data` exists, else memory only |
| `AE_DEPLOY_HOLD_FILE` | optional; the JSON file holding the deploy hold (`/v1/ae/deploy/hold`, read by `deploy/auto-update.sh` through `compose.sh exec`, 2026-10-19); unset, `/data/ae-deploy-hold.json` when `/data` exists, else memory only |
| `AE_SHARED_ACCOUNTS_FILE` | optional; the JSON file holding the shared Claude accounts, tokens encrypted (2026-10-20); unset, `/data/ae-shared-claude-accounts.json` when `/data` exists, else memory only |
| `AE_SHARED_ACCOUNTS_KEY` | optional, a secret; the Fernet key (or several, comma-separated, the first encrypting) for the shared Claude accounts' tokens; unset, they list but none can be added or used, and the error names the variable. `deploy/pull-secrets.sh` writes it from Key Vault `omnigent-ae-shared-accounts-key` |
| `AE_EXTRA_FEATURES` | optional; `off` stops the image's entrypoint from appending `sidebar_grouping_ui` to `OMNIGENT_FEATURES` (2026-10-19) |
| `AE_GITHUB_TOKEN` | optional, a secret; a fine-grained token for the Your move pull requests and the PR cards' Merge button (Contents and Pull requests: read and write; Checks and Commit statuses: read; Workflows: read and write; `deploy/README.md` says why each); unset, both say how to add it. Never logged; `deploy/pull-secrets.sh` writes it from Key Vault `omnigent-ae-github-token` |
| `AE_GITHUB_REPOS` | optional; comma-separated `owner/repo` list for that section; unset, the section asks for it |
| `AE_GITHUB_LOGIN` | optional; whose pull requests count (author or requested reviewer); unset, the token's own login |
| `AE_GITHUB_API_URL` | optional; the GitHub API origin, default `https://api.github.com`; for a local mock or GitHub Enterprise |
| `AE_FABRIC_WAKE_URL_<NAME>` | optional, a secret; one Fabric capacity's Logic App trigger URL (its query carries the signature, so it is the shared wake link). The capacity is `<NAME>` lower-cased, so `AE_FABRIC_WAKE_URL_AETRAINING` is `aetraining`; each such variable adds a card to the Admin Center's Fabric capacity section. Unset, `GET /v1/ae/fabric` says how to add one. Never logged or returned (also scrubbed in percent-encoded forms); `deploy/pull-secrets.sh` writes `AE_FABRIC_WAKE_URL_AETRAINING` from Key Vault `aetraining-wake-url` |
| `AE_FABRIC_SUSPEND_KEY_<NAME>` | optional, a secret; the key (64 lowercase hex characters) that capacity's Logic App wants in the `x-suspend-key` header before it suspends; a wrong or missing key is a 403. Without it the capacity has Wake and no Off or Keep awake, and `POST /v1/ae/fabric/{name}/suspend` and `/keepawake` answer 409. `deploy/pull-secrets.sh` writes `AE_FABRIC_SUSPEND_KEY_AETRAINING` from Key Vault `aetraining-suspend-key` |
| `AE_FABRIC_CAPACITIES_URL` | optional, a secret; the shared `la-fabric-capacities` Logic App's trigger URL (2026-10-21; carries its signature). With the next one, the Admin Center lists every Fabric capacity and can pause, start and resize each; without both, only the `AE_FABRIC_WAKE_URL_<NAME>` capacities show, as before. Never logged or returned; `deploy/pull-secrets.sh` writes it from Key Vault `fabric-capacities-url` |
| `AE_FABRIC_CAPACITIES_KEY` | optional, a secret; the 64 lowercase hex key that Logic App wants in the `x-capacity-key` header on every call (a wrong one is a 403, reported as a 502 naming this variable). Never logged or returned; `deploy/pull-secrets.sh` writes it from Key Vault `fabric-capacities-key` |
| `AE_FABRIC_USD_PER_CU_HOUR` | optional; the F SKU pay-as-you-go price per capacity unit per hour, default `0.18` (F4 is $0.72/h); each fleet row's `hourlyUsd` and the page's resize confirm use it |
| `AE_DATABRICKS_HOST` | optional; the Databricks workspace URL (`https://<workspace>.cloud.databricks.com`) for the Admin Center's Databricks section (2026-10-19, `databricks.py`). The section is on only with this and both secrets below; unset, `GET /v1/ae/databricks` says how to add them |
| `AE_AZURE_COST_SUBSCRIPTIONS` | optional; the subscriptions the Admin Center's Azure card reads, comma separated `<guid>=<name>` pairs (2026-10-22, `azure_cost.py`). The card is on only with this and `AE_AZURE_COST_TENANT_ID`, `AE_AZURE_COST_CLIENT_ID` and `AE_AZURE_COST_CLIENT_SECRET` (a service principal with Cost Management Reader on each); unset, `GET /v1/ae/azure-cost` says how to add them |
| `AE_COST_TARGETS_FILE` | optional; where the cost targets (rules) are kept, default `/data/ae-cost-targets.json`; empty keeps them in memory only (2026-10-25, `cost_targets.py`). The file records that the defaults were seeded, so a deleted target never returns |
| `AE_COST_ALERT_STATE_FILE` | optional; where the filed cost alerts and the Fabric Active-since times are kept, default `/data/ae-cost-alerts.json`; empty keeps them in memory. If it is lost, open and done tasks are searched for their "Alert key" line before anything is filed again |
| `AE_COST_ALERT_LIST` | optional; the task-store list cost alert tasks land in, by name, default `AE Work`; the alarm time and zone are the expiry alerts' (`AE_SECRET_ALERT_TIME`, `AE_TASK_STORE_TZ`) |
| `AE_DATABRICKS_CLIENT_ID` | optional, a secret; the service principal's OAuth client (application) ID. Never logged or returned; `deploy/pull-secrets.sh` writes it from Key Vault `databricks-mvp-client-id` |
| `AE_DATABRICKS_CLIENT_SECRET` | optional, a secret; its OAuth secret, API scope `sql` (warehouses and statement execution). Never logged or returned; `deploy/pull-secrets.sh` writes it from Key Vault `databricks-mvp-client-secret` |
| `AE_DATABRICKS_CREDIT_USD`, `_CREDIT_CADENCE`, `_CREDIT_START`, `_CREDIT_END` | optional; the spend strip's credit, default `200`, `once`, `2026-08-19`, `2027-08-19`. Both bars always show (this month, and since the start); the cadence (`monthly` or `once`) picks which leads |
| `AE_DATABRICKS_STATE_FILE` | optional; the JSON file keeping the last spend reading across restarts, default `/data/ae-databricks.json` |
| `AE_STEWARD_AGENT` | optional; the agent whose newest session is Your move's brief, default `steward` |

The two out-of-process callers, the MCP server and the seeder, read a different pair:

| Variable | Meaning |
|---|---|
| `OMNIGENT_SERVER_URL` | the OmniGent server, as given to `omnigent login` (the stored token is keyed by that exact URL). The MCP server falls back to the `server` key in `~/.omnigent/config.yaml` (`$OMNIGENT_CONFIG_HOME`), then to the single login in `~/.omnigent/auth_tokens.json` (`$OMNIGENT_DATA_DIR`), because a host starts it without this variable; the seeder takes it or `--server-url` |
| `OMNIGENT_TOKEN` | optional; a session token instead of the stored one, for the seeder and for tests |

Identity comes from upstream's own settings (`OMNIGENT_AUTH_PROVIDER`,
`OMNIGENT_AUTH_ENABLED`, `OMNIGENT_LOCAL_SINGLE_USER`). Every authenticated identity
sees every top-level session and may label any of them: the deployment is one admin,
and per-user scoping is not a v1 feature.

## Tests

`just test` runs `tests/` from `upstream/` with upstream's venv, so the suite reuses
upstream's server fixtures (`tests/conftest.py` names them through `pytest_plugins`).
`tests/fixtures/golden_note.json` is generated from the worker's own TypeScript by
`scripts/regen-golden-note.sh <ae-claude-skills checkout>`; `test_notes.py` compares the
port against it byte for byte. Regenerate it when the worker changes and let the test
say what moved.

`tests/fixtures/triage/*.json` are the nine scenarios of ae-claude-skills'
`scripts/test-triage-gate.sh`, ported by `scripts/port-triage-fixtures.py <checkout>`
(`just triage-fixtures <checkout>`): the harness owns each fixture's `transcript`,
`tokens` and `expectation`, this repo owns the canonical `reply` and the `labels` and
`absent` keys the observer must produce from it. `test_triage_fixtures.py` holds every
reply to the harness's own line-start grep, replays each scenario through the real app
as `external_conversation_item` events, and answers an open gate through
`POST /v1/ae/gate/{id}/reply` against a stub runner.

`test_mcp.py` drives the MCP server with the real client over `mcp`'s memory
transport, its HTTP side being upstream's test client on the app, and once over real
stdio as a subprocess; it also holds the shipped agent YAMLs' allowlists to the
server's tool list and away from anything that writes. `test_agents.py` loads the
four YAMLs the way `OMNIGENT_BUILTIN_AGENT_DIRS` loads them. `test_schedules.py`
seeds the two scheduled tasks against the app with a scheduled-task store and the
agents registered, twice, and fires one by hand: with no host online, upstream
records a failed run, and the test asserts that shape.
