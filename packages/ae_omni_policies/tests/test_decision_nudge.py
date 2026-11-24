"""The retired nudge policy: an old stored row still loads and does nothing."""

from __future__ import annotations

import importlib

from omnigent.policies.registry import get_entry, is_registered_handler, load_registry

HANDLER = "ae_omni_policies.decision_nudge.nudge"


def test_an_old_stored_row_still_passes_the_registry_check() -> None:
    # The default-policy routes call is_registered_handler on every edit of a stored row.
    load_registry(extra_modules=["ae_omni_policies"])
    try:
        assert is_registered_handler(HANDLER)
        entry = get_entry(HANDLER)
        assert entry is not None and entry.internal_only is True
    finally:
        load_registry()


def test_the_handler_a_stored_row_names_imports_and_abstains() -> None:
    module, _, name = HANDLER.rpartition(".")
    handler = getattr(importlib.import_module(module), name)
    context = {"conversation_id": "c1", "harness": "claude-native", "actor": {"run_as": "reid"}}
    start = {"type": "request", "data": "hello", "context": context}
    tests = {
        "type": "tool_result",
        "target": "Bash",
        "request_data": {"arguments": {"command": "pytest -q"}},
        "context": context,
    }
    assert handler(start) is None
    assert handler(tests) is None
