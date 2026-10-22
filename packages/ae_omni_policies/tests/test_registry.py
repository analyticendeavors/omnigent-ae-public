"""The registry loads through upstream's scan and lists every AE handler."""

from __future__ import annotations

from omnigent.policies.registry import get_entry, is_registered_handler, load_registry

from ae_omni_policies import POLICY_REGISTRY


def test_upstream_scan_picks_up_the_module() -> None:
    load_registry(extra_modules=["ae_omni_policies"])
    try:
        for entry in POLICY_REGISTRY:
            handler = str(entry["handler"])
            assert is_registered_handler(handler), handler
            found = get_entry(handler)
            assert found is not None
            assert found.name.startswith("AE ")
        slot_cap = get_entry("ae_omni_policies.slot_cap.slot_cap")
        assert slot_cap is not None and slot_cap.kind == "factory"
        assert slot_cap.params_schema is not None
        assert set(slot_cap.params_schema["properties"]) == {"limit", "mode", "lot_url"}
        hurry = get_entry("ae_omni_policies.hurry.hurry_modes")
        assert hurry is not None and hurry.kind == "callable"
        suspect = get_entry("ae_omni_policies.fork_suspect.fork_suspect")
        assert suspect is not None and suspect.kind == "factory"
        assert set(suspect.params_schema["properties"]) == {"min_chars", "annotate_prompt"}
        rabbit = get_entry("ae_omni_policies.rabbit_hole.rabbit_hole")
        assert rabbit is not None and rabbit.kind == "factory"
        assert set(rabbit.params_schema["properties"]) == {"hours", "turns"}
        fallback = get_entry("ae_omni_policies.observer.observe")
        assert fallback is not None and fallback.kind == "callable"
    finally:
        load_registry()
