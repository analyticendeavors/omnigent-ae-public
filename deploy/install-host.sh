#!/usr/bin/env bash
# Build (or rebuild) the Python environment the box host runs from:
# ~/.omnigent-host/venv with omnigent from the pinned submodule and
# packages/ae_omni_policies, so `python -m ae_omni_policies.mcp`, the agents'
# tool, resolves inside it. Phase 6, 2026-09-23. Idempotent; rerun after every
# upstream sync so the host and the server carry the same version.
#
#   deploy/install-host.sh            # as <user>, no sudo
#   AE_HOST_VENV=/elsewhere deploy/install-host.sh
#
# uv, not python3 -m venv: the box has no python3-venv package,
# its system Python may be older than the 3.12 upstream requires, and uv
# downloads a managed interpreter and builds the venv without either. The
# other jobs on the box fall back to `--without-pip` plus get-pip.py for the
# same reason; this is the same answer with fewer moving parts.
#
# OMNIGENT_SKIP_WEB_UI: installing upstream from source builds the web SPA
# unless told not to, and a host never serves it.
set -euo pipefail

# shellcheck source=scripts/_lib.sh
. "$(dirname "${BASH_SOURCE[0]}")/../scripts/_lib.sh"

VENV="${AE_HOST_VENV:-$HOME/.omnigent-host/venv}"
PYTHON_VERSION="${AE_HOST_PYTHON:-$(cat "$UPSTREAM_DIR/.python-version" 2>/dev/null || echo 3.12)}"

# uv, claude and codex live in <user>'s ~/.local/bin and ~/.local/node/bin, which
# the shells OmniGent starts (and the user units) do not put on PATH
# (2026-09-24: the first run from a box session died here).
PATH="$HOME/.local/bin:$HOME/.local/node/bin:$PATH"
command -v uv >/dev/null 2>&1 || die "uv is not on PATH. Install it without sudo: curl -LsSf https://astral.sh/uv/install.sh | sh; then open a new shell."
[ -f "$UPSTREAM_DIR/pyproject.toml" ] || die "upstream/ is not checked out; run: git submodule update --init"

note "python $PYTHON_VERSION venv at $VENV"
uv venv --quiet --allow-existing --python "$PYTHON_VERSION" "$VENV"
OMNIGENT_SKIP_WEB_UI=true uv pip install --quiet --python "$VENV/bin/python" \
  "$UPSTREAM_DIR" \
  "$ROOT/packages/ae_omni_policies"

note "installed: $("$VENV/bin/omnigent" --version)"
"$VENV/bin/python" -c "import ae_omni_policies.mcp, ae_omni_policies.limits_relay" \
  || die "ae_omni_policies.mcp or .limits_relay does not import from $VENV"

# Give the host user's interactive Claude Code and Codex sessions the AE tools too
# (omni_set_status, omni_capacity, park_fork, ...), not only the scheduled
# agents, whose YAML names the server itself. Added 2026-09-24 after sessions
# on the box said they could not flip their own ae.status. User scope, once,
# under the name "ae"; a CLI that is missing or refuses is a warning, never a
# failed install, and a new session picks the server up.
if command -v claude >/dev/null 2>&1; then
  if claude mcp get ae >/dev/null 2>&1; then
    note "claude: MCP server ae already registered"
  elif claude mcp add --scope user ae -- "$VENV/bin/python" -m ae_omni_policies.mcp >/dev/null; then
    note "claude: registered MCP server ae (user scope)"
  else
    warn "claude mcp add ae failed; run by hand: claude mcp add --scope user ae -- $VENV/bin/python -m ae_omni_policies.mcp"
  fi
else
  warn "claude is not on PATH; interactive Claude sessions will not have the AE tools"
fi
if command -v codex >/dev/null 2>&1; then
  if codex mcp get ae >/dev/null 2>&1; then
    note "codex: MCP server ae already registered"
  elif codex mcp add ae -- "$VENV/bin/python" -m ae_omni_policies.mcp >/dev/null; then
    note "codex: registered MCP server ae"
  else
    warn "codex mcp add ae failed; run by hand: codex mcp add ae -- $VENV/bin/python -m ae_omni_policies.mcp"
  fi
fi
note "ok. Next: omnigent login (once, interactively), then the host unit (deploy/README.md, steps 8 to 10)."
