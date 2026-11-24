"""The web's built-in Claude model list and the server's must be the same list."""

from __future__ import annotations

import re
from pathlib import Path

from ae_omni_policies import newer_models

TS = Path(__file__).resolve().parents[3] / "upstream/web/src/lib/aeClaudeModels.ts"


def _web_list() -> list[str]:
    text = TS.read_text(encoding="utf-8")
    body = re.search(r"AE_CLAUDE_KNOWN_MODELS[^=]*=\s*\[(.*?)\];", text, re.DOTALL)
    assert body, "AE_CLAUDE_KNOWN_MODELS not found in aeClaudeModels.ts"
    return re.findall(r'"([^"]+)"', body.group(1))


def test_the_web_and_python_known_model_lists_match() -> None:
    web = _web_list()
    assert web, "the web list parsed empty"
    assert list(newer_models.KNOWN_CLAUDE_MODELS) == web, (
        "upstream/web/src/lib/aeClaudeModels.ts AE_CLAUDE_KNOWN_MODELS and "
        "newer_models.KNOWN_CLAUDE_MODELS differ; change both (and the fork patch) together"
    )
