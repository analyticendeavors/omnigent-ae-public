"""The nine triage-gate scenarios, replayed through the real app.

Each fixture under ``fixtures/triage/`` is one scenario of ae-claude-skills'
``scripts/test-triage-gate.sh`` (ported by ``scripts/port-triage-fixtures.py``)
plus the canonical next assistant turn and the labels the observer must
write from it. The transcript and the reply are posted as
``external_conversation_item`` events, the path a native Claude Code
session's transcript takes, so the items go through patch P3's registry and
the observer labels the session on a worker thread.

The fixture's ``reply`` is also held to the harness's own contract, the
line-start grep for the ``must`` and ``must_not`` tokens, so a reply edited
here can never drift from what the harness would accept.

The last test closes the loop the Parking Lot page runs: with the gate open,
``POST /v1/ae/gate/{id}/reply`` posts the letter as the session's next user
turn through the real in-process loopback, the session runs a turn on
upstream's mock LLM and answers, and the gate reads ``answered``.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import httpx
import pytest
from omnigent.runtime import get_conversation_store
from omnigent.server.item_observers import drain_item_observers
from tests.server.helpers import create_test_session

from ae_omni_policies import labels

FIXTURES = Path(__file__).parent / "fixtures" / "triage"
SCENARIOS = sorted(path.stem for path in FIXTURES.glob("*.json"))
# The harness's own list, so a fixture added or dropped on one side is noticed.
HARNESS_SCENARIOS = {
    "fork-new-deliverable",
    "not-fork-fix-in-flight",
    "not-fork-blocker",
    "micro-absorb",
    "hurry-mode",
    "route-b-note",
    "route-c-no-tools",
    "route-c-no-repo",
    "route-c-no-trello",
}


def load(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


def _labels(session_id: str) -> dict[str, str]:
    conv = get_conversation_store().get_conversation(session_id)
    assert conv is not None
    return {key: value for key, value in conv.labels.items() if value}


async def _post_item(client: httpx.AsyncClient, session_id: str, role: str, text: str) -> str:
    block_type = "output_text" if role == "assistant" else "input_text"
    data: dict[str, Any] = {
        "item_type": "message",
        "item_data": {
            "role": role,
            "content": [{"type": block_type, "text": text}],
            **({"agent": "claude"} if role == "assistant" else {}),
        },
        "response_id": "resp_claude_echo",
    }
    resp = await client.post(
        f"/v1/sessions/{session_id}/events",
        json={"type": "external_conversation_item", "data": data},
    )
    assert resp.status_code in (200, 201, 202), resp.text
    return resp.json()["item_id"]


async def _replay(client: httpx.AsyncClient, fixture: dict[str, Any]) -> str:
    session_id = (await create_test_session(client, title=fixture["name"]))["id"]
    # Drain after every post: each item's observer runs fire-and-forget, so a
    # turn posted back to back with the next could otherwise finish after it.
    for turn in fixture["transcript"]:
        await _post_item(client, session_id, turn["role"], turn["text"])
        await drain_item_observers()
    await _post_item(client, session_id, "assistant", fixture["reply"])
    await drain_item_observers()
    return session_id


def test_every_harness_scenario_has_a_fixture_and_a_reply() -> None:
    assert set(SCENARIOS) == HARNESS_SCENARIOS
    for name in SCENARIOS:
        fixture = load(name)
        assert fixture["name"] == name
        assert isinstance(fixture["reply"], str) and fixture["reply"].strip(), name
        assert fixture["transcript"][-1]["role"] == "user", name


@pytest.mark.parametrize("name", SCENARIOS)
def test_the_reply_meets_the_harness_contract(name: str) -> None:
    """The harness greps ``^TOKEN``; a fabricated-URL sentinel (with a ``/``) matches anywhere."""
    fixture = load(name)
    reply = fixture["reply"]
    tokens = fixture["tokens"]
    for token in [*tokens["must"], *tokens["soft"]]:
        assert re.search("^" + re.escape(token), reply, re.MULTILINE), (
            f"{name}: no line starts {token}"
        )
    must_not = tokens["must_not"]
    if must_not and "/" in must_not:
        assert must_not not in reply, f"{name}: reply must not contain {must_not}"
    elif must_not:
        assert not re.search("^" + re.escape(must_not), reply, re.MULTILINE), (
            f"{name}: a line must not start {must_not}"
        )


@pytest.mark.parametrize("name", SCENARIOS)
async def test_the_observer_labels_the_session(client: httpx.AsyncClient, name: str) -> None:
    fixture = load(name)
    session_id = await _replay(client, fixture)
    after = _labels(session_id)
    for key, value in fixture["labels"].items():
        assert after.get(key) == value, f"{name}: {key} is {after.get(key)!r}, expected {value!r}"
    for key in fixture["absent"]:
        assert key not in after, f"{name}: {key} should be absent, is {after[key]!r}"


async def test_the_definition_of_done_labels(client: httpx.AsyncClient) -> None:
    """Handoff section 9, Phase 4, spelled out once in case a fixture's own expectations move."""
    fork = _labels(await _replay(client, load("fork-new-deliverable")))
    assert fork[labels.GATE] == "open"
    assert fork[labels.FORK].startswith("build a customer testimonials section")
    for name in ("micro-absorb", "not-fork-blocker"):
        assert _labels(await _replay(client, load(name)))[labels.ABSORBED] == "1", name
    assert _labels(await _replay(client, load("route-c-no-tools")))[labels.UNFILED] == "1"
    quiet = _labels(await _replay(client, load("not-fork-fix-in-flight")))
    assert not {labels.GATE, labels.FORK, labels.ABSORBED, labels.UNFILED, labels.NEXT} & set(
        quiet
    )


class _Runner:
    """Upstream's attribution-test runner stub: acks a forwarded turn 202 and records it."""

    def __init__(self) -> None:
        self.posted: list[tuple[str, dict[str, Any]]] = []

    async def post(self, path: str, *, json: dict[str, Any], **_: Any) -> Any:
        self.posted.append((path, json))

        class _Resp:
            status_code = 202
            headers: dict[str, str] = {}
            text = ""

        return _Resp()


async def _noop(*_: Any, **__: Any) -> None:
    return


@pytest.mark.parametrize("route", ["A", "close"])
async def test_a_gate_reply_lands_as_the_next_user_turn(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch, route: str
) -> None:
    """The page's button, end to end: the letter is persisted as a user item and forwarded.

    The session is bound to a stub runner the way upstream's own events tests
    do it (``tests/server/integration/test_sessions_attribution.py``): the
    route persists the user message before forwarding, and the stub records
    the forwarded turn, which is the session receiving it.
    """
    from omnigent.server.routes import sessions as sessions_mod
    from omnigent.server.routes.sessions import routes_events as events_mod

    runner = _Runner()

    async def _bound(*_: Any, **__: Any) -> _Runner:
        return runner

    monkeypatch.setattr(sessions_mod, "_get_runner_client", _bound)
    monkeypatch.setattr(sessions_mod, "_ensure_runner_relay_ready", _noop)
    monkeypatch.setattr(events_mod, "_ensure_runner_relay_ready", _noop)

    session_id = await _replay(client, load("fork-new-deliverable"))
    assert _labels(session_id)[labels.GATE] == "open"

    response = await client.post(f"/v1/ae/gate/{session_id}/reply", json={"route": route})
    assert response.status_code == 200, response.text
    expected = "close it" if route == "close" else route
    assert response.json() == {"posted": True, "route": route, "text": expected, "status": 202}

    items = (await client.get(f"/v1/sessions/{session_id}/items")).json()["data"]
    messages = [item for item in items if item.get("type") == "message"]
    last = messages[-1]
    assert last["role"] == "user"
    assert [block.get("text") for block in last["content"]] == [expected]
    # The runner got the same turn.
    assert len(runner.posted) == 1
    path, forwarded = runner.posted[0]
    assert session_id in path
    assert expected in json.dumps(forwarded)
    await drain_item_observers()
    assert _labels(session_id)[labels.GATE] == "answered"

    # A second click has nothing to answer.
    again = await client.post(f"/v1/ae/gate/{session_id}/reply", json={"route": route})
    assert again.status_code == 409, again.text
