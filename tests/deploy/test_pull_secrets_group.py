"""deploy/pull-secrets.sh files the secrets under the invoking user's group by default (2026-10-29).

The default used to be a literal user name, which fails on any box whose deploy user is
someone else. ``AE_ENV_GROUP`` still overrides it.
"""

from __future__ import annotations

import re
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "deploy" / "pull-secrets.sh"


def test_the_group_defaults_to_the_invoking_users_primary_group() -> None:
    text = SCRIPT.read_text()
    match = re.search(r"^GROUP=(.*)$", text, re.MULTILINE)
    assert match is not None
    assert match.group(1) == '"${AE_ENV_GROUP:-$(id -gn)}"'


def test_the_usage_text_documents_the_default() -> None:
    header = SCRIPT.read_text().split("set -euo pipefail", 1)[0]
    assert "AE_ENV_GROUP" in header and "id -gn" in header
