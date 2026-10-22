#!/usr/bin/env python3
"""Print Claude Code's built-in default effort per model, read from its binary.

Claude Code ships a model catalog inside its executable: each model carries an
``effort_options`` list (the picker's words, "Extra" for xhigh) and a ``runtime``
block with ``default_effort``, the level it runs at when no settings file names
one. The web's CLAUDE_CODE_DEFAULT_EFFORT table (upstream patch, web/src/lib/
aeEffortDefault.ts) is this script's output; rerun it after a Claude Code bump
and update the table when a row changes.

Usage: scripts/claude-effort-defaults.py [path-to-claude-binary]
The default path is the ``claude`` on PATH, resolved through its symlink.
"""

from __future__ import annotations

import re
import shutil
import sys
from pathlib import Path

FAMILIES = ("Opus", "Sonnet", "Fable", "Haiku", "Mythos")
NAME = re.compile(r'name:"((?:' + "|".join(FAMILIES) + r') [0-9.]+[^"]*)"')


def main() -> int:
    if len(sys.argv) > 1:
        path = Path(sys.argv[1])
    else:
        found = shutil.which("claude")
        if not found:
            print("claude is not on PATH; pass the binary's path", file=sys.stderr)
            return 1
        path = Path(found).resolve()
    data = path.read_bytes().decode("utf-8", "replace")
    rows: dict[tuple[str, str], set[str]] = {}
    for match in re.finditer(r"effort_options:\[", data):
        before = data[max(0, match.start() - 900) : match.start()]
        after = data[match.end() : match.end() + 1600]
        names = NAME.findall(before)
        ids = re.findall(r'\bid:"(claude-[-0-9a-z]+)"', before)
        default = re.search(r'default_effort:"(\w+)"', after)
        if not names or not default:
            continue
        rows.setdefault((names[-1], ids[-1] if ids else "?"), set()).add(default.group(1))
    if not rows:
        print(f"no model catalog found in {path}", file=sys.stderr)
        return 1
    print(f"# {path}")
    for (name, model_id), levels in sorted(rows.items()):
        print(f"{name:12} {model_id:28} {', '.join(sorted(levels))}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
