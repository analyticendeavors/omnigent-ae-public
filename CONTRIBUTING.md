# Contributing

omnigent-ae is a thin fork of OmniGent: an upstream release tag plus a short patch series, with everything else kept out of tree in `packages/`, `agents/`, `config/` and `deploy/`. `README.md` has the layout. This page covers how to build, test and send a change.

Everyone taking part follows the [code of conduct](CODE_OF_CONDUCT.md). Security problems go through [SECURITY.md](SECURITY.md), never a public issue.

## Build and test

Commands live in the `justfile`, and CI runs the same recipes. You need `just`, `uv`, `git`, and Node and pnpm for the web and extension recipes.

- `just setup`: initialise the `upstream/` submodule, install upstream's locked environment, then the AE packages editable.
- `just lint`: ruff check and format check over `packages/`, `tests/` and the Python scripts.
- `just test`: the AE package suites and the `tests/deploy` checks.
- `just web-check`: lint, type-check and the vitest suites for the patched web UI.
- `just ext-build`: type-check and test the Parking Lot extension, rebuild its bundle, and fail if the committed `dist/` differs.
- `just hooks-install`: run once in the clone you push from; a pre-push hook then scans every push (the superproject and `upstream/`) for internal identifiers. CI runs the same scan on every PR. `docs/LEAK-SCAN.md` has the classes and how to clear a false positive.
- `just fork-push BRANCH`: push a branch of `upstream/` to the public fork with the leak scan run first, whether or not the hook is installed. A push with `--no-verify` skips the hook; only this recipe and the hourly fork backstop still catch it.
- `just upstream-smoke`: the upstream test files that cover the AE patches.

Run `just --list` for the rest. Two `tests/deploy` tests call `sudo` and fail on a machine without passwordless sudo.

## The thin-fork rules

1. Never edit a file under `upstream/` on a feature branch of this repo. A change to OmniGent's own code is a commit on the fork branch `analyticendeavors/omnigent@ae/vX.Y.Z`, with the subject prefix `[ae-patch]`. The series branch is whatever `upstream.lock`'s `AE_BRANCH` names (today `ae/v0.18.0-public`; the `-public` suffix marks the series regenerated for publication).
2. Generate patches with `just patches-export`. Never hand-edit `patches/*.patch` or `upstream.lock`; CI fails on drift.
3. Prefer a policy, an extension or an out-of-tree router over a patch, and a patch over copying upstream logic. New logic goes in new files so a patch touches as few upstream lines as possible.

## How the patches and the lock relate

`upstream/` is a submodule pinned to the fork branch, which is the upstream release tag plus the patch series. `patches/` is the reviewable form of that series. `upstream.lock` names the tag, the fork branch and the commit each points at. `just lock-check` and `just patches-check` fail when the three disagree. If another PR merges a patch first and your patch number goes stale, `just patches-rebase` renumbers it. `docs/UPSTREAM-SYNC.md` has the full procedure.

## Commits and pull requests

- Commit subjects are full sentences.
- Any pull request or issue you mention carries its full URL, not a bare number.
- No secrets, tokens or passwords, and no contact or prospect data. No internal IPs, host names, e-mail addresses, home paths or private repository links either; `scripts/leak-scan.py` fails the PR on them (`docs/LEAK-SCAN.md`).
- Run `just lint` and `just test` before opening a pull request, and fill in the pull request template.
