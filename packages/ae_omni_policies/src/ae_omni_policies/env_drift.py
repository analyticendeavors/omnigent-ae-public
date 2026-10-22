"""Settings ``config/server.env.example`` sets that the running server does not have.

On 2026-10-20 the session rail's Usage card went blank after every deploy: the
box's live ``config/server.env`` had never picked up ``AE_LIMITS_STATE_FILE``
from the example, and nothing said so
(https://github.com/analyticendeavors/omnigent-ae/pull/144 gave that key a
``/data`` default, so it is optional now). The live file is copied from the
example once and edited by hand, so any later required line can be missed the
same way. This module lists those keys for the Admin Center's Deploys section
(through ``release.status``).

The image ships the example at :data:`DEFAULT_EXAMPLE`; :data:`EXAMPLE_ENV`
overrides the path. An expected key is an uncommented ``KEY=value`` line;
commented lines are optional settings. Keys between a ``# drift-check: pending
begin`` line and a ``# drift-check: pending end`` line are expected only after
a step the example names (the OIDC cutover), so they are skipped; a block left
without its end marker skips nothing. The check reads the server's whole
environment, so a key with an empty value counts as missing. Only key names
leave this module, never values.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from pathlib import Path

EXAMPLE_ENV = "AE_SERVER_ENV_EXAMPLE"
DEFAULT_EXAMPLE = Path("/opt/ae/config/server.env.example")
PENDING_BEGIN = "# drift-check: pending begin"
PENDING_END = "# drift-check: pending end"
_KEY = re.compile(r"^([A-Z_][A-Z0-9_]*)=")


def expected_keys(text: str) -> list[str]:
    """The example's uncommented keys, in file order, closed pending blocks left out."""
    keys: list[str] = []
    pending: list[str] | None = None
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith(PENDING_BEGIN):
            pending = []
        elif line.startswith(PENDING_END):
            pending = None
        elif match := _KEY.match(line):
            (keys if pending is None else pending).append(match.group(1))
    if pending:
        keys.extend(pending)
    return list(dict.fromkeys(keys))


def missing(environ: Mapping[str, str] | None = None) -> list[str] | None:
    """Expected keys absent or empty in ``environ``; None when the example cannot be read."""
    env = os.environ if environ is None else environ
    path = Path((env.get(EXAMPLE_ENV) or "").strip() or DEFAULT_EXAMPLE)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    return [key for key in expected_keys(text) if not (env.get(key) or "").strip()]


__all__ = ["expected_keys", "missing"]
