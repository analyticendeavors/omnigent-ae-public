#!/usr/bin/env bash
# Claude Code statusLine command for the box host: save the payload, print
# nothing (or whatever a chained command prints). 2026-09-24, plan limits.
#
# Claude Code pipes a JSON payload to the statusLine command on every render;
# for a subscription it carries `rate_limits` (the 5-hour and weekly windows).
# The claude-native harness overrides the statusLine with its own shim and
# chains the command in ~/.claude/settings.json with the same stdin
# (upstream omnigent/harnesses/claude_native/bridge.py), so installing this
# there makes every OmniGent Claude session, and every plain `claude` in a
# terminal, leave its latest payload where the limits relay
# (python -m ae_omni_policies.limits_relay) reads it.
#
#   ~/.claude/settings.json:
#     "statusLine": {"type": "command",
#                    "command": "/home/reid/repos/omnigent-ae/deploy/ae-limits-statusline.sh"}
#
# A status bar you already had keeps working: pass its command as the
# arguments and it receives the same stdin, e.g.
#     "command": "/home/reid/repos/omnigent-ae/deploy/ae-limits-statusline.sh npx -y claude-hud"
#
# Shell, not Python: the statusLine runs on Claude Code's render path and
# upstream moved its own capture to a shell shim for the same reason. The
# write is a temp file and a rename, so the relay never reads half a payload;
# an empty stdin leaves the last payload in place. Every step that can fail
# is guarded, so a full disk never fails the render.
set -euo pipefail

target="${AE_LIMITS_CLAUDE_FILE:-$HOME/.omnigent/ae/claude-statusline.json}"
payload=$(cat || true)

if [ -n "$payload" ]; then
  dir=$(dirname "$target")
  if mkdir -p "$dir" 2>/dev/null; then
    tmp="$dir/.claude-statusline.$$"
    if printf '%s' "$payload" > "$tmp" 2>/dev/null; then
      mv -f "$tmp" "$target" 2>/dev/null || rm -f "$tmp"
    else
      rm -f "$tmp"
    fi
  fi
fi

if [ "$#" -gt 0 ]; then
  printf '%s' "$payload" | "$@" || true
fi
exit 0
