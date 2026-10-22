#!/usr/bin/env bash
# ============================================================
# regen-golden-note.sh: rebuild the golden fixture for the notes.py port from
# the task-store worker's OWN TypeScript.
#
# packages/ae_omni_policies/src/ae_omni_policies/notes.py is a port of the note
# assembly in ae-claude-skills task-store-worker/src/index.ts. Two copies of one
# note shape drift, so the fixture the port is tested against is not written by
# hand: this script extracts the worker's pure functions (QUADRANT_AXES,
# normalizeTags, estimateFor, the whole "note assembly" section) verbatim, runs
# them under node's type stripping over tests/fixtures/golden_note_inputs.json,
# and writes tests/fixtures/golden_note.json. test_notes.py then compares the
# Python port against that file byte for byte.
#
# Run it when the worker changes (or when the inputs file does), commit the
# result, and let the test say what moved. Needs node 22.6 or newer (for
# --experimental-strip-types) and a checkout of ae-claude-skills.
#
# Usage:
#   scripts/regen-golden-note.sh <path to an ae-claude-skills checkout>
# ============================================================
set -euo pipefail

skills="${1:?usage: scripts/regen-golden-note.sh <path to an ae-claude-skills checkout>}"
src="$skills/task-store-worker/src/index.ts"
[ -f "$src" ] || { echo "no worker source at $src" >&2; exit 2; }
command -v node >/dev/null 2>&1 || { echo "node not found; needs node 22.6 or newer" >&2; exit 3; }

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
fixtures="$root/packages/ae_omni_policies/tests/fixtures"
inputs="$fixtures/golden_note_inputs.json"
out="$fixtures/golden_note.json"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

# The worker's functions, extracted by their own anchors so a moved line number
# never silently ships a stale copy. Each range ends at the first line that
# matches its terminator, which for a top-level function is the `}` in column 0.
{
  echo "// Extracted from $src by scripts/regen-golden-note.sh; do not edit."
  awk '/^const QUADRANT_AXES/,/^type Quadrant = /' "$src"
  awk '/^function normalizeTags/,/^}/' "$src"
  awk '/^function estimateFor/,/^}/' "$src"
  # "// --- note assembly" through the line before "const ok = " (the MCP glue).
  awk '/^\/\/ --- note assembly/,/^const ok = /' "$src" | sed '$d'
  cat <<'HARNESS'

// --- harness ---------------------------------------------------------
const fs = require("node:fs");
const inputs = JSON.parse(fs.readFileSync(process.argv[2], "utf8"));
const result: Record<string, Record<string, unknown>> = {
  notes: {},
  estimate_for: {},
  repo_slug: {},
  first_sentence: {},
  normalize_tags: {},
  next_from_notes: {},
};
for (const [name, c] of Object.entries(inputs.notes) as [string, any][]) {
  // The same defaulting park_fork applies before buildNotes (index.ts, the
  // `note` object in the park_fork handler).
  const note = {
    summary: c.summary?.trim() || firstSentence(c.state),
    thread: c.thread,
    repo: repoSlug(c.repo),
    state: c.state,
    next: c.next,
    slot: c.slot,
    quadrant: c.quadrant,
    est: c.est,
    from: c.from,
  };
  result.notes[name] = buildNotes(note, c.checklist, c.task_url);
}
for (const s of inputs.estimate_for) result.estimate_for[s] = estimateFor(s);
for (const s of inputs.repo_slug) result.repo_slug[s] = repoSlug(s);
for (const s of inputs.first_sentence) result.first_sentence[s] = firstSentence(s);
for (const [name, tags] of Object.entries(inputs.normalize_tags) as [string, string[]][]) {
  result.normalize_tags[name] = normalizeTags(tags);
}
for (const [name, notes] of Object.entries(inputs.next_from_notes) as [string, string][]) {
  result.next_from_notes[name] = nextFromNotes(notes);
}
process.stdout.write(JSON.stringify(result, null, 2) + "\n");
HARNESS
} > "$work/golden.ts"

node --experimental-strip-types "$work/golden.ts" "$inputs" > "$work/golden_note.json" 2> "$work/node.err" \
  || { echo "node failed:" >&2; cat "$work/node.err" >&2; exit 1; }
mv "$work/golden_note.json" "$out"
echo "wrote $out from $src"
