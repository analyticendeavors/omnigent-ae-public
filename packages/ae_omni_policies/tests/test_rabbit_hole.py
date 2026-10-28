"""rabbit_hole: turn counting, the one ASK, the acknowledgement, the off switch."""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from omnigent.entities.conversation import Conversation
from omnigent.runtime import get_caps, get_conversation_store
from omnigent.runtime.caps import RuntimeCaps
from omnigent.spec.types import FunctionPolicySpec, FunctionRef
from sqlalchemy.exc import OperationalError
from tests.server.helpers import create_test_session

from ae_omni_policies import labels, sessions
from ae_omni_policies import rabbit_hole as rabbit_module
from ae_omni_policies.rabbit_hole import ACK_KEY, TURNS_KEY, rabbit_hole

NOW = 1_800_000_000
HOUR = 3_600


def _event(
    state: dict[str, Any] | None = None, labels_: dict[str, str] | None = None
) -> dict[str, Any]:
    return {
        "type": "request",
        "data": {"user_content": "keep going"},
        "context": {"labels": labels_ or {}},
        "session_state": state or {},
    }


def _conv(
    id: str = "conv_a", title: str = "Fix the build", parent: str | None = None
) -> Conversation:
    return Conversation(
        id=id,
        created_at=NOW - 5 * HOUR,
        updated_at=NOW,
        root_conversation_id=parent or id,
        title=title,
        kind="sub_agent" if parent else "default",
        parent_conversation_id=parent,
    )


@pytest.fixture(autouse=True)
def _pin(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rabbit_module, "_now", lambda: float(NOW))


@pytest.fixture()
def store(monkeypatch: pytest.MonkeyPatch) -> dict[str, Conversation]:
    monkeypatch.setattr(sessions, "current_session_id", lambda: "conv_a")
    rows = {"conv_a": _conv()}

    class _Store:
        def get_conversation(self, conversation_id: str) -> Conversation | None:
            return rows.get(conversation_id)

    monkeypatch.setattr(sessions, "get_conversation_store", lambda: _Store())
    return rows


COUNTED = {
    "result": "ALLOW",
    "state_updates": [{"key": TURNS_KEY, "action": "increment", "value": 1}],
}


def test_under_both_thresholds_every_prompt_is_counted(store: dict[str, Conversation]) -> None:
    policy = rabbit_hole(hours=4, turns=60)
    started = {labels.STARTED: str(NOW - 3 * HOUR)}
    assert policy(_event(labels_=started)) == COUNTED
    assert policy(_event({TURNS_KEY: 58}, started)) == COUNTED


def test_past_the_time_threshold_one_ask_names_the_session(store: dict[str, Conversation]) -> None:
    policy = rabbit_hole(hours=4, turns=60)
    response = policy(_event({TURNS_KEY: 11}, {labels.STARTED: str(NOW - 4 * HOUR - 600)}))
    assert response is not None and response["result"] == "ASK"
    assert response["reason"].startswith(
        'Rabbit hole check: "Fix the build" has run for 4h 10m over 12 turns '
        "(threshold 4h or 60 turns). "
    )
    assert "Accept to keep going" in response["reason"]
    assert response["state_updates"] == [
        {"key": TURNS_KEY, "action": "increment", "value": 1},
        {"key": ACK_KEY, "action": "set", "value": "1"},
    ]


def test_past_the_turn_threshold_without_a_clock(store: dict[str, Conversation]) -> None:
    response = rabbit_hole(hours=4, turns=60)(_event({TURNS_KEY: 59}))
    assert response is not None and response["result"] == "ASK"
    assert '"Fix the build" has run for 60 turns (threshold 4h or 60 turns)' in response["reason"]


def test_an_acknowledged_session_is_only_counted(store: dict[str, Conversation]) -> None:
    policy = rabbit_hole(hours=4, turns=60)
    over = {labels.STARTED: str(NOW - 9 * HOUR)}
    assert policy(_event({TURNS_KEY: 200, ACK_KEY: "1"}, over)) == COUNTED


def test_hours_zero_turns_the_policy_off_and_turns_zero_drops_that_axis(
    store: dict[str, Conversation],
) -> None:
    assert rabbit_hole(hours=0, turns=60)(_event({TURNS_KEY: 500})) is None
    only_time = rabbit_hole(hours=4, turns=0)
    assert only_time(_event({TURNS_KEY: 500})) == COUNTED
    response = only_time(_event({TURNS_KEY: 1}, {labels.STARTED: str(NOW - 5 * HOUR)}))
    assert response is not None and response["result"] == "ASK"
    assert "(threshold 4h)" in response["reason"]


def test_sub_agents_and_other_phases_abstain(store: dict[str, Conversation]) -> None:
    store["conv_a"] = _conv(parent="conv_parent")
    assert rabbit_hole(hours=4, turns=2)(_event({TURNS_KEY: 5})) is None
    assert rabbit_hole()({"type": "tool_call", "data": {"name": "x"}, "context": {}}) is None


def test_a_store_outage_counts_but_does_not_ask(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Broken:
        def get_conversation(self, conversation_id: str) -> Conversation | None:
            raise OperationalError("select", {}, Exception("locked"))

    monkeypatch.setattr(sessions, "current_session_id", lambda: "conv_a")
    monkeypatch.setattr(sessions, "get_conversation_store", lambda: _Broken())
    assert rabbit_hole(hours=4, turns=2)(_event({TURNS_KEY: 5})) == COUNTED


# ── A family child follows its lead's answer (2026-10-27) ────────────

OVER = {TURNS_KEY: 5}
CHILD_ACKED = {
    "result": "ALLOW",
    "state_updates": [
        {"key": TURNS_KEY, "action": "increment", "value": 1},
        {"key": ACK_KEY, "action": "set", "value": "1"},
    ],
}


class _FamilyStore:
    def __init__(
        self, rows: dict[str, Conversation], owners: dict[str, str] | None = None
    ) -> None:
        self.rows = rows
        self.owners = owners or {}
        self.broken = False
        self.owner_error = False

    def get_conversation(self, conversation_id: str) -> Conversation | None:
        if self.broken and conversation_id == "conv_lead":
            raise OperationalError("select", {}, Exception("locked"))
        return self.rows.get(conversation_id)

    def get_session_owner(self, conversation_id: str, owner_only: bool = False) -> str | None:
        if self.owner_error:
            raise OperationalError("select", {}, Exception("locked"))
        return self.owners.get(conversation_id, "reid")


def _family(
    monkeypatch: pytest.MonkeyPatch,
    *,
    lead_state: dict[str, Any] | None = None,
    child_labels: dict[str, str] | None = None,
    lead: bool = True,
    owners: dict[str, str] | None = None,
) -> _FamilyStore:
    child = _conv()
    child.labels = child_labels if child_labels is not None else {labels.ORCH: "conv_lead"}
    rows = {"conv_a": child}
    if lead:
        lead_row = _conv(id="conv_lead", title="Lead")
        lead_row.session_state = lead_state or {}
        rows["conv_lead"] = lead_row
    family = _FamilyStore(rows, owners)
    monkeypatch.setattr(sessions, "current_session_id", lambda: "conv_a")
    monkeypatch.setattr(sessions, "get_conversation_store", lambda: family)
    return family


def _asks(response: dict[str, Any] | None) -> bool:
    return response is not None and response["result"] == "ASK"


def test_a_child_of_an_accepted_lead_is_allowed_and_gets_its_own_ack(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _family(monkeypatch, lead_state={ACK_KEY: "1"})
    assert rabbit_hole(hours=4, turns=2)(_event(OVER)) == CHILD_ACKED


def test_a_child_of_an_unaccepted_lead_asks(monkeypatch: pytest.MonkeyPatch) -> None:
    _family(monkeypatch, lead_state={TURNS_KEY: 90})
    assert _asks(rabbit_hole(hours=4, turns=2)(_event(OVER)))


def test_a_child_under_the_threshold_is_only_counted(monkeypatch: pytest.MonkeyPatch) -> None:
    _family(monkeypatch, lead_state={ACK_KEY: "1"})
    assert rabbit_hole(hours=4, turns=60)(_event(OVER)) == COUNTED


def test_a_foreign_owner_or_missing_lead_asks(monkeypatch: pytest.MonkeyPatch) -> None:
    _family(
        monkeypatch,
        lead_state={ACK_KEY: "1"},
        owners={"conv_a": "reid", "conv_lead": "someone-else"},
    )
    assert _asks(rabbit_hole(hours=4, turns=2)(_event(OVER)))
    _family(monkeypatch, lead=False)
    assert _asks(rabbit_hole(hours=4, turns=2)(_event(OVER)))


def test_a_fork_or_unlabelled_session_asks(monkeypatch: pytest.MonkeyPatch) -> None:
    fork = {labels.ORCH: "conv_lead", "omnigent.fork.source_id": "conv_other"}
    _family(monkeypatch, lead_state={ACK_KEY: "1"}, child_labels=fork)
    assert _asks(rabbit_hole(hours=4, turns=2)(_event(OVER)))
    _family(monkeypatch, lead_state={ACK_KEY: "1"}, child_labels={labels.ORCH: "1"})
    assert _asks(rabbit_hole(hours=4, turns=2)(_event(OVER)))
    _family(monkeypatch, lead_state={ACK_KEY: "1"}, child_labels={})
    assert _asks(rabbit_hole(hours=4, turns=2)(_event(OVER)))


def test_a_store_error_reading_the_lead_asks(monkeypatch: pytest.MonkeyPatch) -> None:
    family = _family(monkeypatch, lead_state={ACK_KEY: "1"})
    family.broken = True
    assert _asks(rabbit_hole(hours=4, turns=2)(_event(OVER)))


def test_thresholds_must_not_be_negative() -> None:
    with pytest.raises(ValueError, match="thresholds"):
        rabbit_hole(hours=-1)
    with pytest.raises(ValueError, match="thresholds"):
        rabbit_hole(turns=-1)


# ── Through the evaluate route: held, accepted, acknowledged ─────────


def _install(monkeypatch: pytest.MonkeyPatch, turns: int) -> None:
    policies = [
        FunctionPolicySpec(
            name="ae_rabbit_hole",
            on=None,
            function=FunctionRef(
                path="ae_omni_policies.rabbit_hole.rabbit_hole",
                arguments={"hours": 4, "turns": turns},
            ),
        )
    ]
    original = get_caps()
    patched = RuntimeCaps(execution_timeout=original.execution_timeout, default_policies=policies)
    monkeypatch.setattr("omnigent.server.routes.sessions.get_caps", lambda: patched)


async def test_the_ask_is_held_once_and_accept_acknowledges(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio

    from omnigent.runtime import session_stream

    _install(monkeypatch, turns=2)
    session_id = (await create_test_session(client, title="Long haul"))["id"]
    body = {"event": {"type": "PHASE_REQUEST", "data": {"text": "go"}, "context": {}}}

    first = await client.post(f"/v1/sessions/{session_id}/policies/evaluate", json=body)
    assert first.json()["result"] == "POLICY_ACTION_ALLOW"
    conv = get_conversation_store().get_conversation(session_id)
    assert conv is not None and conv.session_state[TURNS_KEY] == 1

    async def drain() -> str:
        async with asyncio.timeout(5.0):
            async for event in session_stream.subscribe(session_id):
                if event.get("type") == "response.elicitation_request":
                    return event["elicitation_id"]
        raise AssertionError("no elicitation")

    waiting = asyncio.create_task(drain())
    await asyncio.sleep(0.05)
    evaluate = asyncio.create_task(
        client.post(f"/v1/sessions/{session_id}/policies/evaluate", json=body)
    )
    elicitation_id = await waiting
    verdict = await client.post(
        f"/v1/sessions/{session_id}/elicitations/{elicitation_id}/resolve",
        json={"action": "accept"},
    )
    assert verdict.status_code == 202, verdict.text
    assert (await evaluate).json()["result"] == "POLICY_ACTION_ALLOW"
    conv = get_conversation_store().get_conversation(session_id)
    assert conv is not None
    assert conv.session_state[TURNS_KEY] == 2
    assert conv.session_state[ACK_KEY] == "1"

    third = await client.post(f"/v1/sessions/{session_id}/policies/evaluate", json=body)
    assert third.json()["result"] == "POLICY_ACTION_ALLOW"
    conv = get_conversation_store().get_conversation(session_id)
    assert conv is not None and conv.session_state[TURNS_KEY] == 3


def test_a_merge_watcher_note_is_neither_counted_nor_asked(
    store: dict[str, Conversation],
) -> None:
    # 2026-10-21: the merge watcher's note continues the work; it is not Reid's turn.
    policy = rabbit_hole(hours=4, turns=60)
    event = _event({TURNS_KEY: 80}, {labels.STARTED: str(NOW - 9 * HOUR)})
    event["data"] = {"user_content": "Merge watcher: PR #7 (Fix it) merged at 09:00Z as abc."}
    assert policy(event) is None
    event["data"] = "Merge watcher: PR #7 (Fix it) was closed without merging."
    assert policy(event) is None
    event["data"] = {"user_content": "keep going"}
    response = policy(event)
    assert response is not None and response["result"] == "ASK"


def test_a_failed_owner_read_asks(monkeypatch: pytest.MonkeyPatch) -> None:
    family = _family(monkeypatch, lead_state={ACK_KEY: "1"})
    family.owner_error = True
    assert _asks(rabbit_hole(hours=4, turns=2)(_event(OVER)))


def test_a_missing_owner_asks(monkeypatch: pytest.MonkeyPatch) -> None:
    family = _family(monkeypatch, lead_state={ACK_KEY: "1"})
    family.get_session_owner = lambda *_a, **_k: None  # type: ignore[method-assign]
    assert _asks(rabbit_hole(hours=4, turns=2)(_event(OVER)))
