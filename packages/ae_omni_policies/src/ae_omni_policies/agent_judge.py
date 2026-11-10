"""``ask_judge``: a guarded, typed judgment any session can ask for mid-work.

Added 2026-11-06. Any lead, helper, builder, reviewer or planner can ask the judge model a
yes/no, a choice between named options, or a score on ordered levels. The answer is
probabilities, not reasons. The server holds the key and runs the advisor's guard over every
piece of agent-written text; a refusal names a reason code, never the text that matched.

Data rule (the owner's, binding): the judge sees only numbers, booleans and guarded strings.
The shadow line keeps only ``kind``, ``n_options``, ``n_facts`` and ``role``; never the
question, the options or the facts.

The decision point is ``agent_ask`` and is live by default (the answer only goes back to the
agent); ``AE_DECISION_POINTS=agent_ask:off`` is the kill switch.
"""

from __future__ import annotations

import math
import re
import time
from collections.abc import Iterable, Mapping
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from . import decision_advisor as da
from . import decision_shadow as ds
from . import labels
from .judge import Decision1Judge, Judge

POINT = "agent_ask"
MAX_PER_HOUR = 20
RATE_WINDOW_S = 3600.0
MAX_OPTIONS = 6
MAX_LEVELS = 8
MAX_FACTS = 12
MAX_SESSIONS = 1000
MAX_TEXT = 40  # a level or a string fact (the MCP schema says the same)
KEY = re.compile(r"[a-z][a-z0-9_]{0,31}")
#: Words a question or an option may start with without being a name.
AGENT_LEADING = da.LEADING_WORDS | frozenset(
    {"should", "is", "are", "does", "do", "did", "can", "could", "will", "would", "which",
     "what", "how", "when", "has", "have", "ready", "run", "ask", "keep", "hand", "enough",
     "low", "medium", "high", "small", "large", "big", "tiny", "yes", "no", "safe", "risky",
     "it", "this", "that", "there", "if", "one", "two", "all", "none", "only", "skip", "stop",
     "continue", "more", "less", "full", "scoped", "quick", "deep"}
)  # fmt: skip


class Option(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str = Field(min_length=1, max_length=32)
    description: str = Field(min_length=1, max_length=100)


class JudgeRequest(BaseModel):
    """What the agent supplies. Every string is guarded before anything leaves the server."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["yes_no", "choice", "score"]
    question: str = Field(min_length=5, max_length=160)
    options: list[Option] | None = Field(default=None, max_length=MAX_OPTIONS)
    levels: list[Annotated[str, Field(max_length=MAX_TEXT)]] | None = Field(
        default=None, max_length=MAX_LEVELS
    )
    facts: dict[str, int | float | bool | Annotated[str, Field(max_length=MAX_TEXT)]] | None = (
        Field(default=None, max_length=MAX_FACTS)
    )


def register() -> None:
    """(Re)declare the point; its log features are the only ones ever written."""
    ds.register_point(
        POINT,
        "yes_no",  # a dynamic point takes any kind per call
        {
            "kind": ("yes_no", "choice", "score"),
            "n_options": int,
            "n_facts": int,
            "role": (*labels.ROLE_IDS, "unknown"),
        },
        threshold=0.0,
        dynamic=True,
        default=ds.LIVE,
    )


_calls: dict[str, tuple[int, float]] = {}


def allow_call(session: str, *, now: float | None = None) -> bool:
    """At most :data:`MAX_PER_HOUR` calls per session per hour."""
    stamp = time.monotonic() if now is None else now
    count, first = _calls.get(session, (0, stamp))
    if stamp - first >= RATE_WINDOW_S:
        count, first = 0, stamp
    if count >= MAX_PER_HOUR:
        _calls[session] = (count, first)
        return False
    if session not in _calls and len(_calls) >= MAX_SESSIONS:
        for key in [k for k, (_, t) in _calls.items() if stamp - t >= RATE_WINDOW_S]:
            del _calls[key]  # only counters whose hour is over
        if len(_calls) >= MAX_SESSIONS:
            return False  # refuse a new session rather than evict a live counter
    _calls[session] = (count + 1, first)
    return True


def _reply(error: str) -> dict[str, Any]:
    return {"answer": None, "probs": {}, "low_confidence": True, "error": error}


def _check(
    text: str, cfg: da.Config, deny: list[str], texts: Iterable[str], *, copy: bool = True
) -> str | None:
    return da.check_summary(
        text, deny, cfg.term_allow, texts if copy else [], leading=AGENT_LEADING
    )


def refusal(
    request: JudgeRequest, cfg: da.Config, deny: list[str], texts: list[str]
) -> str | None:
    """A reason code for the first agent-written text the guard refuses, else ``None``."""
    if request.kind == "choice" and not (request.options and len(request.options) >= 2):
        return "bad_request"
    if request.kind == "score" and not (request.levels and len(request.levels) >= 2):
        return "bad_request"
    pieces: list[str] = [request.question]
    keys = [o.key for o in request.options or []] + list(request.facts or {})
    for key in keys:
        if not KEY.fullmatch(key):
            return "refused_key"
        # A key is text the judge sees: same guard as the question, `_` read as a space. A
        # one-word key is not compared with the user's messages (it is a label, not a copy).
        words = key.replace("_", " ")
        if _check(words, cfg, deny, texts, copy=len(words.split()) > 1):
            return "refused_key"
    for option in request.options or []:
        pieces.append(option.description)
    pieces += request.levels or []
    for value in (request.facts or {}).values():
        if isinstance(value, str):
            pieces.append(value)
        elif isinstance(value, float) and not math.isfinite(value):
            return "bad_request"
    for piece in pieces:
        code = _check(piece, cfg, deny, texts)
        if code:
            return f"refused_{code}"
    return None


def low_confidence(
    answer: Any, probs: Mapping[str, float], cfg: da.Config, known: list[str]
) -> bool:
    """The advisor's rule (``decision_advisor.decide``): confident enough and ahead by margin."""
    label = ("yes" if answer else "no") if isinstance(answer, bool) else answer
    return not da.decide(label, probs, cfg.min_confidence, known)


async def run(
    request: JudgeRequest,
    *,
    session: str,
    owner: str | None,
    role: str | None,
    user_messages: list[str],
    environ: Mapping[str, str] | None = None,
    judge: Judge | None = None,
) -> dict[str, Any]:
    """The reply for the route. Never raises; a failure is ``error`` and ``low_confidence``."""
    try:
        cfg = da.config(environ)
        if not (cfg.enabled and cfg.endpoint and cfg.api_key and owner):
            return _reply("judge_off")
        if owner.lower() not in cfg.users:
            return _reply("judge_off")
        if not allow_call(session):
            return _reply("rate_limited")
        deny = da.load_denylist(cfg.denylist_file)
        if not deny:
            return _reply("refused_no_denylist")
        code = refusal(request, cfg, deny, user_messages)
        if code:
            return _reply(code)
        facts: dict[str, Any] = {}
        for key, value in (request.facts or {}).items():
            if isinstance(value, str):
                value = da.guard_summary(value, deny, cfg.term_allow, [], leading=AGENT_LEADING)
                if value is None:
                    return _reply("refused_fact")
            facts[key] = value
        args: dict[str, Any]
        if request.kind == "yes_no":
            args = {"question": request.question}
            known = ["yes", "no"]
        elif request.kind == "choice":
            options = {o.key: o.description for o in request.options or []}
            args, known = {"options": options}, list(options)
        else:
            levels = list(request.levels or [])
            args, known = {"levels": levels}, levels
        shown = {
            "kind": request.kind,
            "n_options": len(request.options or []),
            "n_facts": len(facts),
            "role": role if role in labels.ROLE_IDS else "unknown",
        }
        register()
        outcome = await ds.ask(
            POINT,
            judge or Decision1Judge(cfg),
            request.kind,
            facts,
            baseline=None,
            session=session,
            environ=environ,
            log_features=shown,
            order=known,
            **args,
        )
        if outcome.mode == ds.OFF:
            return _reply("judge_off")
        verdict = outcome.verdict
        if verdict is None:
            return _reply(outcome.error or "error")
        if not outcome.acted:
            return _reply("shadow_mode")
        return {
            "answer": verdict.answer,
            "probs": {k: round(v, 3) for k, v in verdict.probs.items()},
            "low_confidence": low_confidence(verdict.answer, verdict.probs, cfg, known),
        }
    except Exception:  # noqa: BLE001 (a tool call never breaks the agent's turn)
        return _reply("error")
