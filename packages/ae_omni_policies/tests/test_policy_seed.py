"""The Global Policies page owns the AE policies; config/server.yaml only seeds them.

Since 2026-10-18 the five AE policies sit under ``ae_seed_policies:`` and
``ae_omni_policies.policy_seed`` inserts them into the policy store when it
holds none of them, matched by name or handler (the container runs it through
``docker_entrypoint``). These tests pin the real config's seed, that rule, the
order the page shows, and the one-transaction failure path.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml
from omnigent.db.db_models import uuid_to_bytes
from omnigent.stores.policy_store.sqlalchemy_store import SqlAlchemyPolicyStore

from ae_omni_policies import policy_seed
from ae_omni_policies.policy_seed import (
    SeedError,
    SeedPolicy,
    check_seed,
    parse_seed,
    seed_ids,
    seed_policies,
)

SERVER_YAML = Path(__file__).resolve().parents[3] / "config" / "server.yaml"
MODULES = ["ae_omni_policies"]
LOT_URL = "/extensions/analyticendeavors.parking-lot/lot"
RABBIT_HOLE = "ae_omni_policies.rabbit_hole.rabbit_hole"
EXPECTED = [
    SeedPolicy(
        "ae_slot_cap", "ae_omni_policies.slot_cap.slot_cap", {"mode": "warn", "lot_url": LOT_URL}
    ),
    SeedPolicy("ae_hurry_modes", "ae_omni_policies.hurry.hurry_modes", None),
    SeedPolicy(
        "ae_fork_suspect",
        "ae_omni_policies.fork_suspect.fork_suspect",
        {"min_chars": 40, "annotate_prompt": False},
    ),
    SeedPolicy("ae_rabbit_hole", RABBIT_HOLE, {"hours": 4, "turns": 60}),
    SeedPolicy("ae_observer_fallback", "ae_omni_policies.observer.observe", None),
]


@pytest.fixture()
def store(db_uri: str) -> SqlAlchemyPolicyStore:
    return SqlAlchemyPolicyStore(db_uri)


def _server_config() -> dict[str, Any]:
    return yaml.safe_load(SERVER_YAML.read_text())


def _seed_config(**entries: dict[str, Any]) -> dict[str, Any]:
    return {"ae_seed_policies": entries}


def _rows(store: SqlAlchemyPolicyStore) -> list[SeedPolicy]:
    return [SeedPolicy(p.name, p.handler, p.factory_params) for p in store.list_defaults()]


def _add(store: SqlAlchemyPolicyStore, name: str, handler: str, fill: str) -> None:
    store.create_default(policy_id=fill * 32, name=name, type="python", handler=handler)


def test_the_server_config_seeds_the_five_ae_policies_in_order() -> None:
    cfg = _server_config()
    assert parse_seed(cfg) == EXPECTED
    # Under `policies:` as well they would run twice: once from the image, once from the store.
    assert "policies" not in cfg


def test_the_server_config_seed_passes_the_create_checks() -> None:
    cfg = _server_config()
    check_seed(parse_seed(cfg), cfg["policy_modules"])


def test_an_empty_store_gets_the_seed_in_config_order(store: SqlAlchemyPolicyStore) -> None:
    ids = seed_policies(store, _server_config(), MODULES)
    assert len(ids) == 5
    assert _rows(store) == EXPECTED
    assert [p.id for p in store.list_defaults()] == ids
    assert all(p.enabled and p.type == "python" for p in store.list_defaults())


def test_a_second_run_seeds_nothing(store: SqlAlchemyPolicyStore) -> None:
    first = seed_policies(store, _server_config(), MODULES)
    assert seed_policies(store, _server_config(), MODULES) == []
    assert [p.id for p in store.list_defaults()] == first


def test_a_global_policy_with_another_handler_does_not_block_the_seed(
    store: SqlAlchemyPolicyStore,
) -> None:
    _add(store, "made_on_the_page", "omnigent.policies.builtins.safety.ask_on_os_tools", "a")
    assert len(seed_policies(store, _server_config(), MODULES)) == 5
    rows = _rows(store)
    assert (
        SeedPolicy("made_on_the_page", "omnigent.policies.builtins.safety.ask_on_os_tools", None)
        in rows
    )
    assert [row for row in rows if row.name != "made_on_the_page"] == EXPECTED


def test_an_ae_policy_added_by_hand_blocks_the_seed(store: SqlAlchemyPolicyStore) -> None:
    """Someone already manages the AE policies on the page; the seed stays out of the way."""
    _add(store, "my_rabbit_hole", RABBIT_HOLE, "b")
    assert seed_policies(store, _server_config(), MODULES) == []
    assert [p.name for p in store.list_defaults()] == ["my_rabbit_hole"]


def test_a_policy_deleted_on_the_page_stays_deleted(store: SqlAlchemyPolicyStore) -> None:
    seed_policies(store, _server_config(), MODULES)
    fork_suspect = next(p for p in store.list_defaults() if p.name == "ae_fork_suspect")
    store.delete_default(fork_suspect.id)
    assert seed_policies(store, _server_config(), MODULES) == []
    assert "ae_fork_suspect" not in [p.name for p in store.list_defaults()]


def test_a_policy_renamed_on_the_page_still_counts(store: SqlAlchemyPolicyStore) -> None:
    seed_policies(store, _server_config(), MODULES)
    for policy in store.list_defaults():
        if policy.name == "ae_slot_cap":
            store.update_default(policy.id, name="slots")
        else:
            store.delete_default(policy.id)
    assert seed_policies(store, _server_config(), MODULES) == []
    assert [p.name for p in store.list_defaults()] == ["slots"]


def test_deleting_every_seeded_policy_brings_the_seed_back(store: SqlAlchemyPolicyStore) -> None:
    """The documented exception: a store holding none of them looks like a new server."""
    seed_policies(store, _server_config(), MODULES)
    for policy in store.list_defaults():
        store.delete_default(policy.id)
    assert len(seed_policies(store, _server_config(), MODULES)) == 5
    assert _rows(store) == EXPECTED


def test_an_unregistered_handler_fails_and_writes_nothing(store: SqlAlchemyPolicyStore) -> None:
    cfg = _seed_config(sneaky={"type": "function", "handler": "os.system"})
    with pytest.raises(SeedError, match="no module in policy_modules registers"):
        seed_policies(store, cfg, MODULES)
    assert store.list_defaults() == []


def test_a_policy_module_that_does_not_import_is_named(store: SqlAlchemyPolicyStore) -> None:
    with pytest.raises(SeedError, match="'ae_omni_policies_gone' does not import"):
        seed_policies(store, _server_config(), ["ae_omni_policies_gone"])
    assert store.list_defaults() == []


def test_a_value_the_factory_rejects_fails_and_writes_nothing(
    store: SqlAlchemyPolicyStore,
) -> None:
    """``mode: nope`` passes the schema's type check; only building the factory catches it."""
    cfg = _seed_config(
        ae_slot_cap={
            "type": "function",
            "handler": "ae_omni_policies.slot_cap.slot_cap",
            "factory_params": {"mode": "nope"},
        }
    )
    with pytest.raises(SeedError, match="does not build with these params"):
        seed_policies(store, cfg, MODULES)
    assert store.list_defaults() == []


def test_a_failed_insert_leaves_no_partial_seed(
    store: SqlAlchemyPolicyStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The inserts share one transaction, so the next boot still seeds all five."""
    clash = ["c" * 32, "d" * 32, "c" * 32, "e" * 32, "f" * 32]
    monkeypatch.setattr(policy_seed, "seed_ids", lambda count: clash[:count])
    with pytest.raises(Exception, match=r"(?i)unique|identity|conflict"):
        seed_policies(store, _server_config(), MODULES)
    assert store.list_defaults() == []
    monkeypatch.undo()
    assert len(seed_policies(store, _server_config(), MODULES)) == 5


@pytest.mark.parametrize(
    "declared",
    [
        {"ae_rabbit_hole": {"type": "function", "handler": RABBIT_HOLE}},
        {"rabbit_hole_debug": {"type": "function", "handler": RABBIT_HOLE}},
    ],
    ids=["same name", "same handler"],
)
def test_a_policy_also_under_policies_is_refused(
    store: SqlAlchemyPolicyStore, declared: dict[str, Any]
) -> None:
    rabbit_hole = {"type": "function", "handler": RABBIT_HOLE}
    cfg = {"policies": declared, **_seed_config(ae_rabbit_hole=rabbit_hole)}
    with pytest.raises(SeedError, match="would run twice"):
        seed_policies(store, cfg, MODULES)
    assert store.list_defaults() == []


def test_no_seed_key_seeds_nothing(store: SqlAlchemyPolicyStore) -> None:
    assert seed_policies(store, {"policy_modules": MODULES}, MODULES) == []
    assert store.list_defaults() == []


def test_seed_ids_sort_in_entry_order() -> None:
    ids = seed_ids(40)
    assert all(len(policy_id) == 32 for policy_id in ids)
    assert sorted(ids, key=uuid_to_bytes) == ids
    with pytest.raises(SeedError, match="the limit is 256"):
        seed_ids(257)
