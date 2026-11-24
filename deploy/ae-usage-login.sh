#!/usr/bin/env bash
# One-time sign-in of a Claude account other than the default login, so the limits
# relay can read its usage while no chat runs on it (2026-11-16; docs/PLAN-LIMITS.md,
# "Idle accounts"). Run on the box host, as its user:
#
#   bash deploy/ae-usage-login.sh            # the server's only shared account
#   bash deploy/ae-usage-login.sh '<id>'     # another account, e.g. shared:<name>
#
# It makes a slot under ~/.omnigent/ae/usage-logins (0700), runs `claude auth login`
# in that slot's own config folder (open the link it prints, sign in AS THAT ACCOUNT,
# paste the code back), then two checks that print PASS or FAIL:
#   1. the usage read works through the new sign-in;
#   2. a chat on that account still replies on the token chats use (one short reply).
# Either FAIL removes the slot. The relay reads the account from its next tick.
# Nothing here prints an account name or a token.
set -euo pipefail

PY="${AE_USAGE_LOGIN_PYTHON:-$HOME/.omnigent-host/venv/bin/python}"
export PATH="$HOME/.local/node/bin:$PATH"
# Every variable that could make `claude` use another login, or another folder.
unset CLAUDE_CODE_OAUTH_TOKEN CLAUDE_CODE_OAUTH_TOKEN_FILE_DESCRIPTOR \
  ANTHROPIC_API_KEY ANTHROPIC_AUTH_TOKEN CLAUDE_CONFIG_DIR

made="$("$PY" -m ae_omni_policies.usage_login new ${1:+"$1"})"
slot="$(printf '%s\n' "$made" | sed -n 1p)"
state="$(printf '%s\n' "$made" | sed -n 2p)"

finished=0
cleanup() {
  # A slot this run made goes again on any early exit (a FAIL, Ctrl-C), so the relay
  # never keeps trying a half-made one. A slot an earlier sign-in made is kept.
  if [ "$finished" != 1 ] && [ "$state" = created ] && [ -d "$slot" ]; then
    "$PY" -m ae_omni_policies.usage_login remove "$slot" >/dev/null 2>&1 || true
    echo "Nothing was kept." >&2
  fi
}
trap cleanup EXIT
trap 'exit 130' INT TERM

case "$(basename "$slot")" in
  slot-[0-9]*) ;;
  *) echo "FAIL: not a usage-login slot" >&2; exit 1 ;;
esac
# usage_login refuses a folder that overlaps a chat login; check here too, both ways,
# before pointing `claude auth login` anywhere.
if [ -L "$slot/config" ]; then
  echo "FAIL: that slot's config folder is a symbolic link" >&2
  exit 1
fi
config="$(realpath -m "$slot/config")"
for protected in "$HOME/.claude" "$HOME/.claude-acct2"; do
  real="$(realpath -m "$protected")"
  # The slot's folder inside (or equal to) a chat login, or a chat login inside it.
  case "$config/" in "$real"/*)
    echo "FAIL: refusing to sign in inside a login chats use" >&2
    exit 1
    ;;
  esac
  case "$real/" in "$config"/*)
    echo "FAIL: refusing to sign in over a login chats use" >&2
    exit 1
    ;;
  esac
done

# The relay takes this lock before it reads a slot, so it leaves the slot alone
# while the sign-in writes it (2026-11-16 security review). The checks inherit it.
exec 9>"$slot/lock"
if ! flock -n 9; then
  echo "FAIL: the relay is reading that slot right now; run this again in a minute" >&2
  exit 1
fi

echo "Sign in AS THE ACCOUNT TO READ: open the link below, approve, paste the code back."
if ! CLAUDE_CONFIG_DIR="$config" DISABLE_AUTOUPDATER=1 claude auth login --claudeai; then
  echo "FAIL: the sign-in did not finish." >&2
  exit 1
fi

"$PY" -m ae_omni_policies.usage_login check-read "$slot" || exit 1
# The hard gate: if a chat on the account no longer replies, the slot goes, made
# now or earlier, and the owner is told.
if ! "$PY" -m ae_omni_policies.usage_login check-chat "$slot"; then
  "$PY" -m ae_omni_policies.usage_login remove "$slot" || true
  finished=1
  echo "FAIL: the usage login was removed; tell the lead chat." >&2
  exit 1
fi
finished=1
echo "Done: the relay reads that account within 5 minutes."
