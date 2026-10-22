"""The slot cap through ``POST /v1/sessions/{id}/policies/evaluate`` on the real app.

This is the path a native Claude Code session takes: its UserPromptSubmit hook
posts ``PHASE_REQUEST`` here, a DENY drops the prompt, an ASK is held server-
side as an elicitation and collapsed to ALLOW or DENY when the human answers.
The policy rides in as a server-wide default through ``RuntimeCaps.default_policies``,
the pattern upstream's own ``tests/server/integration/test_sessions_policy_evaluate.py``
uses. On the box it has been a global policy in the policy store since 2026-10-18
(``policy_seed``); both kinds feed the same policy engine.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest
from omnigent.runtime import get_caps, get_conversation_store, session_stream
from omnigent.runtime.caps import RuntimeCaps
from omnigent.spec.types import FunctionPolicySpec, FunctionRef
from tests.server.helpers import create_test_session

from ae_omni_policies import labels

TITLES = ("Fix pricing padding", "Renewal note", "CSV export header")


def _install(monkeypatch: pytest.MonkeyPatch, mode: str, *, hurry: bool = False) -> None:
    """Install the AE policies as the runtime's server-wide defaults."""
    policies = [
        FunctionPolicySpec(
            name="ae_slot_cap",
            on=None,
            function=FunctionRef(
                path="ae_omni_policies.slot_cap.slot_cap",
                arguments={"limit": 3, "mode": mode},
            ),
        )
    ]
    if hurry:
        policies.append(
            FunctionPolicySpec(
                name="ae_hurry_modes",
                on=None,
                function=FunctionRef(path="ae_omni_policies.hurry.hurry_modes"),
            )
        )
    original = get_caps()
    patched = RuntimeCaps(
        execution_timeout=original.execution_timeout,
        default_policies=policies,
    )
    monkeypatch.setattr("omnigent.server.routes.sessions.get_caps", lambda: patched)


def _prompt(text: str = "hello", *, bare: bool = False) -> dict[str, Any]:
    """What the native hook posts: ``PHASE_REQUEST`` with the prompt in ``data.text``.

    ``omnigent/native/native_policy_hook.py`` sends ``{"text": prompt}``; the
    route also accepts the prompt as a bare string (``bare=True``) for older
    callers, and the context builder normalizes both.
    """
    data: Any = text if bare else {"text": text}
    return {
        "event": {"type": "PHASE_REQUEST", "data": data, "context": {"harness": "claude-native"}}
    }


async def _session(
    client: httpx.AsyncClient, title: str, seed: dict[str, str] | None = None
) -> str:
    snapshot = await create_test_session(client, title=title, labels=seed)
    return snapshot["id"]


async def _three_working(client: httpx.AsyncClient) -> list[str]:
    ids = []
    for index, title in enumerate(TITLES):
        seed = {labels.STATUS: "working", labels.STARTED: str(1_700_000_000 + index)}
        if index == 0:
            seed[labels.NEXT] = "verify light mode"
        ids.append(await _session(client, title, seed))
    return ids


def _labels(session_id: str) -> dict[str, str]:
    conv = get_conversation_store().get_conversation(session_id)
    assert conv is not None
    return dict(conv.labels)


async def _evaluate(
    client: httpx.AsyncClient, session_id: str, text: str = "hello", *, bare: bool = False
) -> dict:
    response = await client.post(
        f"/v1/sessions/{session_id}/policies/evaluate", json=_prompt(text, bare=bare)
    )
    assert response.status_code == 200, response.text
    return response.json()


async def _drain_elicitation_id(session_id: str, timeout_s: float = 5.0) -> str:
    """The id of the first elicitation the session publishes (the held ASK)."""
    async with asyncio.timeout(timeout_s):
        async for event in session_stream.subscribe(session_id):
            if event.get("type") == "response.elicitation_request":
                eid = event.get("elicitation_id")
                assert isinstance(eid, str) and eid, f"missing id: {event!r}"
                return eid
    raise AssertionError("subscribe loop ended without an elicitation event")


async def test_first_prompt_claims_a_slot(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install(monkeypatch, "block")
    session_id = await _session(client, "Fresh")
    assert _labels(session_id).get(labels.STATUS) is None

    body = await _evaluate(client, session_id)
    assert body["result"] == "POLICY_ACTION_ALLOW"
    after = _labels(session_id)
    assert after[labels.STATUS] == "working"
    assert after[labels.STARTED].isdigit()
    assert labels.OVERFLOW not in after

    # The second prompt on a placed session never counts again.
    assert (await _evaluate(client, session_id))["result"] == "POLICY_ACTION_ALLOW"


async def test_fourth_prompt_is_denied_in_block_mode(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install(monkeypatch, "block")
    await _three_working(client)
    fourth = await _session(client, "Fourth thing")

    body = await _evaluate(client, fourth, bare=True)
    assert body["result"] == "POLICY_ACTION_DENY"
    reason = body["reason"]
    assert reason.startswith("Slots full: 3 of 3 working. ")
    for title in TITLES:
        assert title in reason
    assert "1. Fix pricing padding (next: verify light mode)" in reason
    assert 'resend starting with "ride unparked"' in reason
    assert labels.STATUS not in _labels(fourth)

    # The escape phrase runs the fourth and marks the overrun.
    body = await _evaluate(client, fourth, "ride unparked, this one is quick")
    assert body["result"] == "POLICY_ACTION_ALLOW"
    after = _labels(fourth)
    assert after[labels.STATUS] == "working"
    assert after[labels.OVERFLOW] == "1"


async def test_fourth_prompt_is_held_then_allowed_on_accept_in_warn_mode(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install(monkeypatch, "warn")
    await _three_working(client)
    fourth = await _session(client, "Fourth thing")

    drain = asyncio.create_task(_drain_elicitation_id(fourth))
    await asyncio.sleep(0.05)
    evaluate = asyncio.create_task(
        client.post(f"/v1/sessions/{fourth}/policies/evaluate", json=_prompt())
    )
    elicitation_id = await drain
    # Held: nothing is written while the human has not answered.
    assert labels.STATUS not in _labels(fourth)

    verdict = await client.post(
        f"/v1/sessions/{fourth}/elicitations/{elicitation_id}/resolve",
        json={"action": "accept"},
    )
    assert verdict.status_code == 202, verdict.text
    response = await evaluate
    assert response.status_code == 200, response.text
    assert response.json()["result"] == "POLICY_ACTION_ALLOW"
    after = _labels(fourth)
    assert after[labels.STATUS] == "working"
    assert after[labels.OVERFLOW] == "1"
    assert after[labels.STARTED].isdigit()


async def test_declining_the_warn_writes_nothing(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install(monkeypatch, "warn")
    await _three_working(client)
    fourth = await _session(client, "Fourth thing")

    drain = asyncio.create_task(_drain_elicitation_id(fourth))
    await asyncio.sleep(0.05)
    evaluate = asyncio.create_task(
        client.post(f"/v1/sessions/{fourth}/policies/evaluate", json=_prompt())
    )
    elicitation_id = await drain
    verdict = await client.post(
        f"/v1/sessions/{fourth}/elicitations/{elicitation_id}/resolve",
        json={"action": "decline"},
    )
    assert verdict.status_code == 202, verdict.text
    response = await evaluate
    assert response.json()["result"] == "POLICY_ACTION_DENY"
    assert labels.STATUS not in _labels(fourth)


async def test_a_blocked_session_frees_its_slot(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install(monkeypatch, "block")
    ids = await _three_working(client)
    get_conversation_store().set_pending_elicitation_count(ids[1], 1)
    fourth = await _session(client, "Fourth thing")
    assert (await _evaluate(client, fourth))["result"] == "POLICY_ACTION_ALLOW"
    assert _labels(fourth)[labels.STATUS] == "working"


async def test_hurry_mode_rides_with_the_claim(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install(monkeypatch, "block", hurry=True)
    session_id = await _session(client, "Slammed")
    assert (await _evaluate(client, session_id, "triage off"))["result"] == "POLICY_ACTION_ALLOW"
    after = _labels(session_id)
    assert after[labels.TRIAGE] == "off"
    assert after[labels.STATUS] == "working"
    assert (await _evaluate(client, session_id, "triage on"))["result"] == "POLICY_ACTION_ALLOW"
    assert labels.label(_labels(session_id), labels.TRIAGE) is None


async def test_a_sub_agent_runs_inside_its_parent_slot(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A sub-agent's message is never held, denied or labelled: it is the parent's slot."""
    _install(monkeypatch, "block")
    ids = await _three_working(client)
    store = get_conversation_store()
    parent = store.get_conversation(ids[0])
    assert parent is not None
    child = store.create_conversation(
        kind="sub_agent",
        parent_conversation_id=parent.id,
        agent_id=parent.agent_id,
        sub_agent_name="helper",
    )
    assert (await _evaluate(client, child.id))["result"] == "POLICY_ACTION_ALLOW"
    assert labels.STATUS not in _labels(child.id)

    # The parent's own next prompt is unaffected by the child.
    assert (await _evaluate(client, parent.id))["result"] == "POLICY_ACTION_ALLOW"
