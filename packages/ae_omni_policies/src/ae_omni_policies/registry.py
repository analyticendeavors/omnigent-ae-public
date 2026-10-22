"""``POLICY_REGISTRY`` for the server's ``policy_modules`` scan.

Same shape as upstream's builtins (``omnigent/policies/builtins/cost.py``):
``GET /v1/policy-registry`` serves these entries, the UI can attach them to a
session with validated ``factory_params``, and the registry doubles as the
allowlist of handlers the server will import (``is_registered_handler``).
"""

from __future__ import annotations

from ae_omni_policies import labels

POLICY_REGISTRY: list[dict[str, object]] = [
    {
        "handler": "ae_omni_policies.slot_cap.slot_cap",
        "kind": "factory",
        "name": "AE Slot Cap",
        "description": (
            "The slot rule as a request-phase gate. A session's first prompt claims a "
            "slot (ae.status=working) while fewer than `limit` top-level sessions are working; "
            "past the limit the prompt is held for approval (mode warn) or dropped (mode block) "
            "with the working sessions named. A message starting with 'ride unparked' runs "
            "one more anyway and marks the session ae.overflow=1. Sessions waiting on an answer "
            "do not count."
        ),
        "params_schema": {
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "description": (
                        "Working sessions allowed at once. Leave unset: the server's "
                        "AE_SLOT_LIMIT is the default and what GET /v1/ae/capacity reports."
                    ),
                    "default": labels.SLOT_LIMIT,
                    "minimum": 1,
                },
                "mode": {
                    "type": "string",
                    "enum": ["warn", "block"],
                    "description": (
                        "warn holds the prompt for approval (ASK); block drops it (DENY)."
                    ),
                    "default": "warn",
                },
                "lot_url": {
                    "type": "string",
                    "description": (
                        "Optional link to the Parking Lot page, appended to the reason text."
                    ),
                },
            },
        },
    },
    {
        "handler": "ae_omni_policies.hurry.hurry_modes",
        "kind": "callable",
        "name": "AE Hurry Modes",
        "description": (
            "Session-scoped triage modes from the branch-discipline rules: a message that is "
            "exactly 'triage off' or 'triage capture' sets ae.triage, 'triage on' clears it. "
            "Always allows; other messages are untouched."
        ),
    },
    {
        "handler": "ae_omni_policies.fork_suspect.fork_suspect",
        "kind": "factory",
        "name": "AE Fork Suspect",
        "description": (
            "Asks the server's policy model (the llm: block) whether a user message starts a "
            "second deliverable and, on yes, allows with ae.fork_suspect=<hash> for a chip. "
            "Never gates. Abstains with no llm: block, on short messages, while ae.triage is "
            "set, and while a gate is open. Verdicts are cached per message in session state."
        ),
        "params_schema": {
            "type": "object",
            "properties": {
                "min_chars": {
                    "type": "integer",
                    "description": "Messages shorter than this are never classified.",
                    "default": 40,
                    "minimum": 0,
                },
                "annotate_prompt": {
                    "type": "boolean",
                    "description": (
                        "On a yes, append one line to the user text telling the agent to "
                        "gate it if it is a fork. Off by default."
                    ),
                    "default": False,
                },
            },
        },
    },
    {
        "handler": "ae_omni_policies.rabbit_hole.rabbit_hole",
        "kind": "factory",
        "name": "AE Rabbit Hole",
        "description": (
            "One ASK per session once it has run past `hours` since ae.started or `turns` "
            "prompts, naming the elapsed time, the turn count and the title. Accept "
            "acknowledges for the session; decline drops that prompt. hours: 0 disables it."
        ),
        "params_schema": {
            "type": "object",
            "properties": {
                "hours": {
                    "type": "number",
                    "description": "Hours since the slot was claimed before the ask; 0 disables.",
                    "default": 4,
                    "minimum": 0,
                },
                "turns": {
                    "type": "integer",
                    "description": "Prompts before the ask; 0 drops the turn threshold.",
                    "default": 60,
                    "minimum": 0,
                },
            },
        },
    },
    {
        "handler": "ae_omni_policies.observer.observe",
        "kind": "callable",
        "name": "AE Observer Fallback",
        "description": (
            "Only for an upstream without patch P3 (the item-observers registry): at each "
            "prompt, scans the session's newest items for the branch-discipline tokens "
            "(FORK:, Absorbed:, UNFILED, THREAD:/NEXT:) and writes the ae.gate, ae.fork, "
            "ae.absorbed, ae.unfiled and ae.next labels the registered observer would have. "
            "Abstains when the patch is present."
        ),
    },
]

__all__ = ["POLICY_REGISTRY"]
