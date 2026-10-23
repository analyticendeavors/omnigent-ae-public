# omnigent-ae: rules for Claude sessions in this repository

This is a thin fork of OmniGent. Read `README.md` for the layout and
`docs/UPSTREAM-SYNC.md` for how the upstream pin moves. The brief for the whole
product is internal to Analytic Endeavors.

## The thin-fork rule

1. Never edit a file under `upstream/` on a feature branch of this repo. A change to
   OmniGent's own code is a patch: a commit on the fork branch
   `analyticendeavors/omnigent@ae/vX.Y.Z` (subject prefix `[ae-patch]`), exported
   with `just patches-export`, committed here as `patches/NNNN-*.patch` together with
   `upstream.lock` and the submodule pointer. The series branch is whatever
   `upstream.lock`'s `AE_BRANCH` names (today `ae/v0.18.0-public`; the `-public`
   suffix marks the series regenerated for publication). Logic goes in new files so a patch
   touches upstream files in as few lines as possible. A constant two web files
   share lives in `web/src/lib/`, never in a component file: the vitest suites
   mock whole component modules, and a missing export fails every test that
   renders them (59 composer tests on 2026-10-20).
2. Never hand-edit `patches/*.patch`, `upstream.lock` or the managed block at the end
   of this file. All three are generated; CI fails on drift.
3. Everything AE-specific lives out of tree: `packages/`, `agents/`, `config/`,
   `deploy/`. Prefer a policy, an extension or an out-of-tree router over a patch;
   prefer a patch over a fork of upstream logic.
4. Every behavior moved into a policy or the UI should let a prompt rule be retired
   or shrink; new behavior that only adds a rule does not ship.

## Workflow standards (.github/workflows/*.yml)

Same standards as agents-and-automation, written out here because a web session in
this repository sees only this checkout:

1. Every job sets `timeout-minutes`; every workflow has a top-level `permissions`
   block; event-triggered workflows declare `concurrency`.
2. `run:` blocks use `set -euo pipefail`; polling loops cap their iterations.
3. `curl` calls carry `--max-time` and `--fail` on the `curl` line.
4. Cron expressions carry a comment with the schedule in local time.
5. Secrets reach scripts through `env:` blocks only, never interpolated into `run:`
   bodies or URLs; never log a secret.
6. No `${{ }}` expression inside a large `run:` body (a run body with an expression
   is parsed as one template capped at 21,000 characters; pass values via `env:`).
7. Global npm installs are version-pinned. New workflows are documented in
   `README.md`. Do not flag an action version as invalid just because it is newer
   than your training data.

## Review guidance

- Commit subjects are sentences. Docs carry the why, with dates. Error messages say
  what failed and what to do about it.
- Never reference a PR, issue or task without its URL.
- No hardcoded secrets, tokens or passwords; no contact or prospect data in the repo.
