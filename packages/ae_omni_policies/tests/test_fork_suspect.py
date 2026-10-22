"""fork_suspect with a stub ``llm_client``: caching, abstention, the chip and the annotation.

The factory is exercised the way the engine calls it (an event dict, the
response applied by hand to ``session_state`` between calls) and once through
``POST /v1/sessions/{id}/policies/evaluate`` on the real app with the stub
wired in where upstream builds the client, so the cache is proven against the
real ``session_state`` store.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest
from omnigent.runtime import get_caps, get_conversation_store
from omnigent.runtime.caps import RuntimeCaps
from omnigent.spec.types import FunctionPolicySpec, FunctionRef
from tests.server.helpers import create_test_session

from ae_omni_policies import labels
from ae_omni_policies.fork_suspect import ANNOTATION, CACHE_PREFIX, fork_suspect, message_hash

FORK_TEXT = "Oh, and we should build a customer testimonials section for the home page."
PLAIN_TEXT = "The padding you just changed looks off in light mode now; fix that too."


@dataclass
class _Response:
    output_text: str


@dataclass
class _Client:
    """Stands in for ``PolicyLLMClient``: canned verdicts, recorded calls."""

    verdicts: list[str] = field(default_factory=lambda: ['{"fork": true}'])
    calls: list[dict[str, Any]] = field(default_factory=list)
    error: Exception | None = None

    async def create(self, **kwargs: Any) -> _Response:
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        text = self.verdicts[min(len(self.calls), len(self.verdicts)) - 1]
        return _Response(output_text=text)


def _event(
    text: str = FORK_TEXT,
    *,
    client: _Client | None = None,
    state: dict[str, Any] | None = None,
    labels_: dict[str, str] | None = None,
    bare: bool = False,
) -> dict[str, Any]:
    return {
        "type": "request",
        "data": text if bare else {"user_content": text, "attachments": []},
        "context": {"labels": labels_ or {}},
        "session_state": state or {},
        "llm_client": client,
    }


def _apply(state: dict[str, Any], response: dict[str, Any] | None) -> dict[str, Any]:
    """What the engine does with ``state_updates`` on ALLOW."""
    for update in (response or {}).get("state_updates", []):
        assert update["action"] == "set"
        state[update["key"]] = update["value"]
    return state


async def test_a_suspect_message_is_allowed_with_the_chip_and_cached() -> None:
    client = _Client()
    policy = fork_suspect()
    state: dict[str, Any] = {}

    first = await policy(_event(client=client, state=state))
    digest = message_hash(FORK_TEXT)
    assert first == {
        "result": "ALLOW",
        "state_updates": [{"key": f"{CACHE_PREFIX}{digest}", "action": "set", "value": True}],
        "set_labels": {labels.FORK_SUSPECT: digest},
    }
    assert len(client.calls) == 1
    call = client.calls[0]
    assert call["text"]["format"]["schema"]["properties"] == {"fork": {"type": "boolean"}}
    assert FORK_TEXT in call["input"][0]["content"][0]["text"]
    assert "never as instructions" in call["instructions"]

    # Same text again: the cache answers, the model is not called.
    _apply(state, first)
    second = await policy(_event(client=client, state=state))
    assert second == {"result": "ALLOW", "set_labels": {labels.FORK_SUSPECT: digest}}
    assert len(client.calls) == 1


async def test_a_plain_message_clears_a_stale_chip() -> None:
    client = _Client(verdicts=['{"fork": false}'])
    policy = fork_suspect()
    response = await policy(
        _event(PLAIN_TEXT, client=client, labels_={labels.FORK_SUSPECT: "deadbeefdeadbeef"})
    )
    assert response == {
        "result": "ALLOW",
        "state_updates": [
            {"key": f"{CACHE_PREFIX}{message_hash(PLAIN_TEXT)}", "action": "set", "value": False}
        ],
        "set_labels": {labels.FORK_SUSPECT: ""},
    }
    # No chip to clear: a plain verdict writes no label at all.
    assert await policy(_event(PLAIN_TEXT, client=client, state={})) == {
        "result": "ALLOW",
        "state_updates": [
            {"key": f"{CACHE_PREFIX}{message_hash(PLAIN_TEXT)}", "action": "set", "value": False}
        ],
    }


@pytest.mark.parametrize(
    ("event", "why"),
    [
        (_event(client=None), "no llm: block, so no client"),
        (_event("short one", client=_Client()), "under min_chars"),
        (_event(client=_Client(), labels_={labels.TRIAGE: "off"}), "triage off"),
        (_event(client=_Client(), labels_={labels.TRIAGE: "capture"}), "triage capture"),
        (_event(client=_Client(), labels_={labels.GATE: "open"}), "a gate is already open"),
        (
            _event(
                "Merge watcher: PR #196 (Add an Accounts card) merged at 17:00Z as 11c89f51. "
                "Continue: delete its branch and worktree, then check the deploy.",
                client=_Client(),
            ),
            "the merge watcher's note (2026-10-21), no model call",
        ),
        (
            {"type": "tool_call", "data": {"name": "x"}, "context": {}, "llm_client": _Client()},
            "phase",
        ),
    ],
)
async def test_abstentions(event: dict[str, Any], why: str) -> None:
    assert await fork_suspect()(event) is None, why
    client = event.get("llm_client")
    if isinstance(client, _Client):
        assert client.calls == [], why


async def test_a_failing_or_unreadable_classifier_abstains() -> None:
    failing = _Client(error=RuntimeError("provider down"))
    assert await fork_suspect()(_event(client=failing)) is None
    garbage = _Client(verdicts=["not json"])
    assert await fork_suspect()(_event(client=garbage)) is None
    wrong_shape = _Client(verdicts=['{"fork": "yes"}'])
    assert await fork_suspect()(_event(client=wrong_shape)) is None
    fenced = _Client(verdicts=['```json\n{"fork": true}\n```'])
    response = await fork_suspect()(_event(client=fenced))
    assert response is not None and response["set_labels"] == {
        labels.FORK_SUSPECT: message_hash(FORK_TEXT)
    }


async def test_annotate_prompt_appends_one_line_and_keeps_the_shape() -> None:
    policy = fork_suspect(annotate_prompt=True)
    response = await policy(_event(client=_Client()))
    assert response is not None
    assert response["data"] == {
        "user_content": f"{FORK_TEXT}\n\n{ANNOTATION}",
        "attachments": [],
    }
    bare = await policy(_event(client=_Client(), bare=True))
    assert bare is not None and bare["data"] == f"{FORK_TEXT}\n\n{ANNOTATION}"
    # Off by default: no replacement payload.
    plain = await fork_suspect()(_event(client=_Client()))
    assert plain is not None and "data" not in plain


def test_min_chars_must_not_be_negative() -> None:
    with pytest.raises(ValueError, match="min_chars"):
        fork_suspect(min_chars=-1)
    assert asyncio.run(fork_suspect(min_chars=0)(_event("ok", client=_Client()))) is not None


# ── Through the evaluate route, the cache in the real session_state ──


def _install(monkeypatch: pytest.MonkeyPatch, client: _Client) -> None:
    policies = [
        FunctionPolicySpec(
            name="ae_fork_suspect",
            on=None,
            function=FunctionRef(
                path="ae_omni_policies.fork_suspect.fork_suspect", arguments={"min_chars": 10}
            ),
        )
    ]
    original = get_caps()
    patched = RuntimeCaps(execution_timeout=original.execution_timeout, default_policies=policies)
    monkeypatch.setattr("omnigent.server.routes.sessions.get_caps", lambda: patched)
    # Where upstream turns the server's llm: block into event["llm_client"].
    monkeypatch.setattr(
        "omnigent.runtime.policies.builder._build_policy_llm_client", lambda *a, **k: client
    )


async def test_the_cache_survives_in_the_stored_session_state(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    stub = _Client()
    _install(monkeypatch, stub)
    session_id = (await create_test_session(client, title="Padding fix"))["id"]
    body = {"event": {"type": "PHASE_REQUEST", "data": {"text": FORK_TEXT}, "context": {}}}

    first = await client.post(f"/v1/sessions/{session_id}/policies/evaluate", json=body)
    assert first.status_code == 200, first.text
    assert first.json()["result"] == "POLICY_ACTION_ALLOW"
    conv = get_conversation_store().get_conversation(session_id)
    assert conv is not None
    assert conv.labels[labels.FORK_SUSPECT] == message_hash(FORK_TEXT)
    assert conv.session_state[f"{CACHE_PREFIX}{message_hash(FORK_TEXT)}"] is True
    assert len(stub.calls) == 1
    # The session's title reaches the classifier as the task.
    assert "Padding fix" in stub.calls[0]["input"][0]["content"][0]["text"]

    second = await client.post(f"/v1/sessions/{session_id}/policies/evaluate", json=body)
    assert second.json()["result"] == "POLICY_ACTION_ALLOW"
    assert len(stub.calls) == 1


async def test_without_an_llm_block_the_route_still_allows(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install(monkeypatch, None)  # type: ignore[arg-type]
    session_id = (await create_test_session(client, title="No box"))["id"]
    body = {"event": {"type": "PHASE_REQUEST", "data": {"text": FORK_TEXT}, "context": {}}}
    response = await client.post(f"/v1/sessions/{session_id}/policies/evaluate", json=body)
    assert response.status_code == 200, response.text
    assert response.json()["result"] == "POLICY_ACTION_ALLOW"
    conv = get_conversation_store().get_conversation(session_id)
    assert conv is not None
    assert labels.FORK_SUSPECT not in conv.labels
    assert not any(
        key.startswith(CACHE_PREFIX) for key in json.loads(json.dumps(conv.session_state))
    )
