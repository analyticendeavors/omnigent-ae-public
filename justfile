# omnigent-ae task runner. Recipes mirror upstream's uv and pnpm conventions
# (upstream/justfile, upstream/CONTRIBUTING.md). CI calls these same recipes,
# so every command lives in exactly one place.

set shell := ["bash", "-euo", "pipefail", "-c"]

default:
    @just --list

# OMNIGENT_SKIP_WEB_UI: upstream's setup.py builds the web SPA during uv sync
# unless told not to, and nothing here serves the bundle. The AE package goes
# into that same venv, editable; `uv sync` prunes anything outside upstream's
# lock, so the install always follows it and setup is re-run after a sync.
#
# The lint group carries ruff (upstream runs it as .venv/bin/python -m ruff), so
# `just lint` uses upstream's own pinned ruff rather than whatever is on PATH.
#
# Submodule, upstream's locked test and lint environment, then the AE packages (editable).
setup:
    git submodule update --init
    cd upstream && OMNIGENT_SKIP_WEB_UI=true uv sync --locked --extra all --group test --group lint
    cd upstream && uv pip install -e ../packages/ae_omni_policies -e ../packages/ae_omni_extension

# upstream.lock, .gitmodules, the checked-out submodule and patches/ must agree.
lock-check:
    scripts/check-lock.sh

# Apply patches/ onto the pinned tag in upstream/ (no-op when already applied).
patches-apply:
    scripts/apply-patches.sh

# Regenerate patches/ and upstream.lock from the commits in upstream/.
patches-export:
    scripts/export-patches.sh

# Fail if patches/ or upstream.lock drifted from upstream/ (what CI runs).
patches-check:
    scripts/export-patches.sh --check

# When another PR merges a patch first, an open PR's patch number goes stale. The
# fix was a hand rebuild: PR 129 went through it four times on 2026-10-18 and 19
# (0084 to 0097). This rebases the branch's fork commits onto the base's
# AE_BRANCH_SHA, re-exports, and rewrites the old number to the new one (0NNN and
# P<n>) only in lines the branch added to README.md, patches/README.md,
# docs/UPSTREAM-SYNC.md and this file, so the base's own references stay put.
# Rerun after resolving a rebase conflict in upstream/. Added 2026-10-20
# (https://github.com/analyticendeavors/omnigent-ae/issues/135).
#
# Rebase this branch's fork commits onto base's series, re-export, renumber the docs.
patches-rebase base="origin/main":
    scripts/rebase-patches.sh {{base}}

# Runs from upstream/ so the suite can reuse upstream's server fixtures
# (packages/ae_omni_policies/tests/conftest.py explains how).
#
# The AE packages' own suites, on upstream's venv and fixtures. Two invocations,
# not one: each package carries its own pytest config (rootdir and pythonpath).
#
# tests/deploy (Phase 6, 2026-09-23) holds deploy/ still without a Docker
# daemon or systemd: the compose override parsed and merged with upstream's
# file (`docker compose config` when the CLI is present), the four units
# through configparser, the scripts through `bash -n` and shellcheck, the
# publishing workflow against the repo's standards and actionlint. shellcheck
# and actionlint skip with a warning when absent; CI installs both.
#
# The AE packages' suites, on upstream's venv and fixtures, then the deploy checks.
test:
    cd upstream && uv run --no-sync pytest ../packages/ae_omni_policies/tests -q -p no:cacheprovider
    cd upstream && uv run --no-sync pytest ../packages/ae_omni_extension/tests -q -p no:cacheprovider
    cd upstream && uv run --no-sync pytest ../tests/deploy -q -p no:cacheprovider

# ruff.toml at the repo root carries the same rule set for tests/ and the
# scripts named here; each package keeps its own [tool.ruff].
#
# Ruff over the AE packages, tests/ and the Python scripts with upstream's ruff.
lint:
    cd upstream && uv run --no-sync ruff check ../packages ../tests ../scripts/ae-health.py ../scripts/docs-coverage.py
    cd upstream && uv run --no-sync ruff format --check ../packages ../tests ../scripts/ae-health.py ../scripts/docs-coverage.py

# Maintainer-only: needs node 22.6 or newer and a checkout of analyticendeavors/ae-claude-skills (private).
#
# Rebuild the golden note fixture from the task-store worker's own TypeScript.
golden-note skills:
    scripts/regen-golden-note.sh {{skills}}

# The nine triage-gate scenarios are the observer's fixtures; the harness
# owns the transcripts and the token expectations, this repo owns the
# canonical reply and the labels (Phase 4, 2026-09-23).
#
# Re-port the triage-gate scenarios from a checkout of ae-claude-skills.
triage-fixtures skills:
    scripts/port-triage-fixtures.py {{skills}}

# Needs node 22 and pnpm 11.15 (upstream's packageManager pin; `corepack enable`
# or `npm i -g pnpm@11.15.1`). Installs the web workspace from upstream's
# lockfile, then runs upstream's own lint and type-check over the whole SPA and
# vitest over the suites the patch series adds or touches. P1 (Phase 2,
# 2026-09-23): the AE helpers and components, every Sidebar suite (the patched
# file is upstream's most conflict-prone) and the extension navigation map.
# P2 (Phase 3, 2026-09-23): the extension host services (the server.request
# allowlist, the session projection with ae.* labels) and the SDK page drain.
# P4 (2026-09-24): the sidebar's Status submenu (AeStatusMenu).
# P5 (2026-09-24): the split view (AeSplitPage, its layout helpers) and the
# sidebar item that opens it (covered by the Sidebar suites).
# P6 (2026-09-24): the context ring's plan limits popover (AePlanLimits).
# P9 (2026-09-24): the same suite covers the full usage panel.
# P10 (2026-09-24): the split view's layout presets (the same two P5 suites).
# P11 (2026-09-25): Run on chat shell code blocks (block detection, the
# heredoc and quote guard, the danger list, the input registry, and a page
# test that a Run sends the exact bytes through TerminalView's WebSocket).
# P12 (2026-09-24): the PR cards in the session's GitHub panel and under chat
# messages (AePrCards, AeInlinePrCards, lib/aePrs.ts; the PullRequestPanel and
# BlockRenderer suites cover the two-line insertions; GithubPanel until v0.18.0).
# P11 fixes (patch 0013, 2026-09-25) ride the same three suites.
# P20 (2026-09-25): the review offer after a merge waits for the session's
# last open tracked pull request (lib/aeReviewOffer, and cases in the two
# card suites above).
# P21 (2026-09-25): the merge card's phone restyle and its segmented method
# control ride the AePrCards suite.
# P22 (2026-09-26): the one-row confirm step and the "Mark session done" label
# ride the AePrCards and AeInlinePrCards suites.
# P15 (2026-09-24): Tabler project icons (the lazy loaders against the real
# installed package, the renderer, the picker) and the upstream suites whose
# files it touches (ProjectIconPicker, ProjectRowIcon; Sidebar is above).
# P16 (2026-09-24): the terminal's touch Paste button (AeTerminalPaste; the
# TerminalView insertion is one render expression). P17 (2026-09-24), its
# review fixes, rides the same suite, which also mounts TerminalView.
# P23 (2026-09-27): the review fixes for P15 and P21/P22 ride the Tabler
# suites, AePrCards and the extension services suite (projects.test.ts).
# P24 (2026-09-27): the second review round rides the same Tabler suites; the
# test setup serves the icon set from disk (lib/aeTablerAssetFetch.disk.ts).
# P25 (2026-09-27): navigation.openExternal opens GitHub pull request and
# issue URLs (lib/aeExternalUrls, and a host services case that a GitHub URL
# opens and any other URL the host did not hand out is still refused).
# P26 (2026-09-27): the PR card's one status row rides the AePrCards and
# AeInlinePrCards suites.
# P27 (2026-09-27): openExternal's PopupBlocked rejection rides the
# extension services suite (useExtensionHostServices.aeGithub.test.tsx).
# P28 (2026-09-28): the recommended merge method on the confirm step rides the
# AePrCards suite.
# P30 (2026-10-13): team selection, per-model context and agent-switch refresh.
# P32 (2026-10-13): Auto mode and ready-agent recommendations exercise the same suites.
# P97 (2026-10-18): the token defrag tray (lib/aeTokenDefrag, components/AeTokenDefrag)
# and the held-ring case in the ComposerContextRing suite.
# P99 (2026-10-18): the fork dialog's Claude account row (AeForkClaudeAccount and the
# ForkSessionDialog suite); since v0.18.0 Switch harness and Change lead open that dialog.
# P100 (2026-10-19): the Triage card lists the notes saved on the session (the aeRail
# and aeRailTriage suites, already in the list).
# P117 (2026-10-20): shared Claude accounts in the picker (lib/aeSharedClaudeAccounts and
# the ClaudeAccounts suite; the AeForkClaudeAccount suite is already in the list).
# P123 (2026-10-20): prompt suggestions (lib/aeSuggestion, the store's aeSuggestion
# suite, and the composer's ghost text in ChatComposer.aeSuggestion).
# vitest is called through `pnpm exec`, not `pnpm run test -- paths`: the `--`
# reaches vitest as a literal argument, the filters are ignored and the whole
# SPA suite runs.
#
# CI runs the two halves as separate jobs, and the suites as parallel shards
# (`just web-test 2/4`): one run of the whole list took about 5 minutes of a
# 10 minute job. Split 2026-10-20.
#
# Lint, type-check and the vitest suites the patch series adds or touches (the list is the web-test recipe) in upstream's web workspace.
web-check: web-lint web-test

# Upstream's lint and type-check over the whole web workspace.
web-lint:
    cd upstream && pnpm install --frozen-lockfile --filter web
    cd upstream && pnpm --filter web run lint
    cd upstream && pnpm --filter web run type-check

# The vitest suites in the web-check list; `shard` is vitest's `--shard` (index/count).
web-test shard="1/1":
    cd upstream && pnpm install --frozen-lockfile --filter web
    cd upstream && pnpm --filter web exec vitest run --shard={{shard}} src/lib/aeLabels src/lib/aeSlotLimit src/lib/aeAppBadge src/components/AeStatusIcon src/components/AeSlotsPill src/components/AeSessionChip src/shell/ChatHeader src/shell/HeaderConversationMenu src/shell/aePinFamily src/shell/Sidebar src/shell/AeStatusMenu src/extensions/ExtensionPrimaryNavigation src/extensions/services src/extensions/sdkSessions src/lib/aeSplitLayout src/pages/AeSplitPage src/components/AePlanLimits src/components/AeContextCategories src/components/aeContextUsage src/components/aeSessionContext src/components/AeTeamControl src/lib/aeTeam src/lib/aeTeamPresets src/store/chatStore.test src/lib/sessionsApi.test src/lib/aeRunCommand src/lib/aeTerminalInput src/components/AeRunCodeButton src/components/AePrCards src/components/AeInlinePrCards src/lib/aePrReceipt src/lib/aeReviewOffer src/shell/PullRequestPanel src/shell/ForkSessionDialog src/shell/AeForkClaudeAccount src/shell/ClaudeAccounts src/shell/AeClaudeAccountSubmenu src/lib/aeSharedClaudeAccounts src/components/blocks/BlockRenderer src/components/blocks/ApprovalCard src/components/blocks/AeApprovalPreview src/lib/aeTablerIcon src/components/AeTablerIconPicker src/components/ProjectIconPicker src/shell/ProjectRowIcon src/components/AeTerminalPaste src/lib/aeContextTone src/components/composer/ComposerContextRing src/pages/InboxPage src/lib/aeExternalUrls src/pages/ChatPage.composer src/pages/ChatPage.mention src/pages/ChatPage.statusLine src/components/AeFastMode src/lib/aeFastMode src/components/KeyboardShortcut src/shell/aeAgentsPaneModel src/shell/aeAgentsGraphLayout src/shell/AeAgentsSections src/shell/SubagentsPanel src/shell/SubagentsGraphView src/lib/aeSessionRail src/shell/AeSessionRail src/components/aeRail src/lib/aeRailTriage src/lib/aeRailScheduled src/lib/aeRailSentFiles src/lib/aeSessionActivity src/hooks/aeSessionActivityHooks src/components/AeSessionActivity src/components/AeSessionHoverStatus src/components/AePrLinkHoverCard src/lib/aeEffortDefault src/lib/aeSessionPeek src/lib/aeSuggestion src/store/aeSuggestion src/components/composer/ChatComposer.aeSuggestion src/store/aeLastEventAt src/components/SessionStateBadge src/pages/settings/AeChatSettingsSection src/shell/settingsNav src/pages/SettingsPage.test src/shell/AppShell.aeSessionRail src/lib/aeActiveTurn src/components/chat/Transcript.activeTurn src/lib/aePolicyEdit src/components/AePolicyEditButton src/pages/PoliciesPage src/hooks/useDefaultPolicies src/lib/aeTokenDefrag src/components/AeTokenDefrag src/lib/aeSessionOrder src/lib/aeTriage src/lib/aeFamily src/lib/sidebarViewPreferences src/hooks/useAeSidebarOrder src/shell/aeSidebarStore src/pages/settings/AeSessionListSetting src/lib/aeClaudeModels src/shell/NewChatDialog.test src/hooks/useHosts src/hooks/useAePickedClaudeAccount src/lib/aeActionDock src/components/AeActionDock src/lib/aeLimitGate src/lib/aeRoles src/pages/settings/AeRolesSettingsSection src/lib/aeStartOn src/lib/aeSessionRoles src/hooks/useAeSessionPreset src/lib/aeLeadModel src/lib/aeReviewers src/pages/settings/AeReviewerRows src/hooks/useAeReviewerAgents src/pages/settings/AePresetInsightsBlock src/lib/aeSettingHints src/components/AeHint src/lib/aeCodexModels src/lib/aeModelFold src/pages/settings/AeFoldPicker src/lib/aeModelUpdates src/pages/settings/AeModelUpdates src/components/AeModelChangeBand src/lib/aeLeadRowSlots src/lib/aeSpendDisplay src/pages/settings/AeSpendDisplaySetting src/lib/aeModelChange src/lib/aeSessionLinks src/components/blocks/ChatMarkdown

# The page's bundle is committed (upstream's asset resolver reads it from the
# installed package, and the image needs no Node), so the build must be
# reproducible: install from the lockfile, build, and fail on any difference
# from what is in git. The SDK is upstream's private workspace package,
# reached by the path alias in web/vite.config.ts. Added 2026-09-23 (Phase 3).
# The page's vitest suite (web/test, jsdom: the lot's Resume flow and card
# toggle) runs before the build, so CI's web job covers it (2026-09-24).
#
# Type-check and test the Parking Lot page, rebuild its bundle, fail if the committed dist drifted.
ext-build:
    cd packages/ae_omni_extension/web && pnpm install --frozen-lockfile
    cd packages/ae_omni_extension/web && pnpm run type-check
    cd packages/ae_omni_extension/web && pnpm run test
    cd packages/ae_omni_extension/web && pnpm run build
    git diff --exit-code -- packages/ae_omni_extension/src/ae_omni_extension/dist

# upstream's own diagnostic for the installed extension: manifest accepted,
# bundle resolved from the package (`setup` installs it editable).
#
# `omni extensions doctor` for the Parking Lot page, in upstream's venv.
ext-doctor:
    cd upstream && uv run --no-sync omni extensions list
    cd upstream && uv run --no-sync omni extensions doctor analyticendeavors.parking-lot

# The upstream suites the AE layer leans on, run inside our pinned tree, plus
# the suite patch P3 adds (tests/server/test_item_observers.py, Phase 4,
# 2026-09-23): the registry the AE observer hooks is upstream code, so its
# tests ride with upstream's, not with the AE packages'.
#
# tests/inner runs in a second pytest process (2026-10-19). Importing
# tests/inner/conftest.py arms faulthandler.dump_traceback_later(90,
# repeat=True), and that dump walks every thread's frames without the GIL, so
# when it lands mid-test it can segfault the run (exit 139, no traceback). The
# combined run passed 90 seconds on GitHub runners and crashed about one run in
# six; the inner files alone finish well inside the first 90 seconds. New
# tests/inner files go on the second line.
upstream-smoke:
    cd upstream && uv run --no-sync pytest tests/policies tests/extensions tests/server/test_item_observers.py tests/server/scheduled/test_retention.py tests/server/scheduled/test_task_project.py tests/server/test_host_connect_observers.py tests/server/test_identity_migration.py tests/server/test_identity_migration_extra.py tests/server/integration/test_ae_team.py tests/runner/test_ae_fast_mode.py tests/runner/test_ae_context_usage.py tests/runner/test_ae_launch_effort.py tests/server/routes/test_ae_context_usage_routes.py tests/server/routes/test_ae_mcp_status_routes.py tests/server/routes/test_ae_session_activity.py tests/server/routes/test_ae_fast_mode_routes.py tests/server/test_openapi_drift.py tests/server/routes/test_ae_fork_claude_account.py tests/onboarding/test_ae_shared_claude_accounts.py tests/onboarding/test_claude_accounts.py tests/server/test_ae_child_model.py tests/harnesses/claude_native/forwarder/test_ae_subagent_model.py tests/server/routes/test_ae_default_policy_params.py tests/server/routes/test_default_policies.py tests/server/integration/test_default_policy_routes.py tests/stores/test_ae_update_default_factory_params.py tests/harnesses/claude_native/test_ae_launch_account.py tests/runner/test_app_claude_native_launch_args.py tests/harnesses/claude_native/forwarder/test_ae_prompt_suggest.py tests/harnesses/codex_native/forwarder/test_ae_prompt_suggest.py tests/server/integration/test_ae_session_suggestion.py tests/runner/test_ae_gh_auth_cache.py -q -m "not databricks"
    cd upstream && uv run --no-sync pytest tests/inner/test_ae_team.py tests/inner/test_native_attachments.py tests/inner/test_codex_native_executor.py -q -m "not databricks"

# OMNIGENT_EXTENSION_DEV_BUNDLES points the server at the extension's package
# root on disk, so a `just ext-build` is picked up on the next start without a
# reinstall (upstream/docs/extending/browser_extensions.md, "Development
# override"). The value is read once at startup and logs a warning; it is a
# local convenience, never a deployment setting.
#
# Run the server locally with the AE layer and the Parking Lot page loaded.
dev:
    cd upstream && OMNIGENT_EXTENSION_DEV_BUNDLES="{\"analyticendeavors.parking-lot\": \"$(pwd)/../packages/ae_omni_extension/src/ae_omni_extension\"}" uv run --no-sync omnigent server --config ../config/server.yaml

# Two-stage image: upstream's server image from the patched tree, then the AE layer.
image:
    docker build -f upstream/deploy/docker/Dockerfile -t omnigent-ae-base:local upstream
    docker build -f deploy/Dockerfile --build-arg BASE=omnigent-ae-base:local -t omnigent-ae:local .

# The image must run the pinned version, list the Parking Lot extension with its
# bundle resolved, and import the AE packages.
image-smoke:
    docker run --rm omnigent-ae:local omni --version
    docker run --rm omnigent-ae:local omni extensions list | grep -F analyticendeavors.parking-lot
    docker run --rm omnigent-ae:local omni extensions doctor analyticendeavors.parking-lot
    docker run --rm omnigent-ae:local python -c "import ae_omni_policies; print('ae_omni_policies', ae_omni_policies.__version__)"
    docker run --rm omnigent-ae:local python -c "import ae_omni_extension; print('ae_omni_extension', ae_omni_extension.__version__)"

# Move the pin to a newer upstream release tag (procedure: docs/UPSTREAM-SYNC.md).
sync tag:
    scripts/sync-upstream.sh {{tag}}

# Scheduled tasks are rows created through POST /v1/scheduled-tasks, not
# config, so the steward and the shutdown note are seeded by name (create or
# patch, never a duplicate; a paused task stays paused). Identity is the token
# `omnigent login <url>` stored, or OMNIGENT_TOKEN. The two agents must be
# registered first (OMNIGENT_BUILTIN_AGENT_DIRS, config/server.yaml), or the
# seed stops and says so. Phase 5, 2026-09-23.
#
# Create or update the "AE steward" and "AE shutdown note" scheduled tasks.
schedules-seed url:
    cd upstream && uv run --no-sync python ../scripts/seed-schedules.py {{url}}

# Save the Triage card's note for every Absorbed: and UNFILED line a session counted
# before the observer saved notes (2026-10-19). Run once after that deploy; it writes
# only missing notes, so a second run writes nothing. Identity as schedules-seed.
triage-notes-backfill url:
    cd upstream && uv run --no-sync python ../scripts/backfill-triage-notes.py {{url}}

# /v1/ae/health sits behind the login like every /v1/ae route, so a bare curl
# answers 401 in accounts mode; this sends the token `omnigent login` stored
# for the URL (or OMNIGENT_TOKEN). The runbook's last step (deploy/README.md).
# Phase 6, 2026-09-23.
#
# GET /v1/ae/health on the server at U with the stored login; exit 1 unless ok.
ae-health url:
    cd upstream && uv run --no-sync python ../scripts/ae-health.py {{url}}

# The feature docs keep up once per public release, never on every merge (2026-10-22):
# the public release's prepare step runs this check on the tag it publishes.
#
# Core patches since the release tag T that no page under docs/features covers; exit 1 if any.
docs-coverage since:
    cd upstream && uv run --no-sync python ../scripts/docs-coverage.py --since {{since}}
