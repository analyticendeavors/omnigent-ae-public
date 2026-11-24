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

2026-11-13 (micro-decisions): one call may carry one to five questions (:class:`JudgeBatch`),
because every agent tool call is a model step that re-reads the session's context; each
question may carry a short ``context`` line, checked by the same guard and copy check and never
logged (the shadow line gets ``has_context`` only). The session caps count calls, not questions;
the server-wide caps count questions. A rate limit answers ``rate_limited`` with
``retry_after_s``.
"""

from __future__ import annotations

import asyncio
import contextlib
import math
import os
import re
import time
from collections import deque
from collections.abc import Iterable, Mapping
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from . import decision_advisor as da
from . import decision_shadow as ds
from . import labels
from .judge import Decision1Judge, Judge

POINT = "agent_ask"
#: Per-session caps on calls (not questions); env overrides, helper text in server.env.example.
HOURLY_ENV = "AE_DECISION_SESSION_HOURLY"
MINUTE_ENV = "AE_DECISION_SESSION_MINUTE"
#: Questions (not calls) one session may send in any 60 s, so one chat cannot eat the
#: server-wide minute cap that every chat shares (2026-11-13 review).
QUESTIONS_ENV = "AE_DECISION_SESSION_QUESTIONS_MINUTE"
MAX_PER_HOUR = 120
MAX_PER_MINUTE = 10
MAX_QUESTIONS_PER_MINUTE = 20
RATE_WINDOW_S = 3600.0
BURST_WINDOW_S = 60.0
MAX_BATCH = 5
MAX_CONTEXT = 160
SITUATION = "situation"  # the state key the guarded context travels under
MAX_OPTIONS = 6
MAX_LEVELS = 8
MAX_FACTS = 12
MAX_SESSIONS = 1000
PASSTHROUGH_INSIDE_MIN = 4  # words a key or level needs before "inside a user message" counts
MAX_TEXT = 40  # a level or a string fact (the MCP schema says the same)
KEY = re.compile(r"[a-z][a-z0-9_]{0,31}")
#: Words a question or an option may start with without being a name.
AGENT_LEADING = da.LEADING_WORDS | frozenset(
    {"should", "is", "are", "does", "do", "did", "can", "could", "will", "would", "which",
     "what", "how", "when", "has", "have", "ready", "run", "ask", "keep", "hand", "enough",
     "low", "medium", "high", "small", "large", "big", "tiny", "yes", "no", "safe", "risky",
     "it", "this", "that", "there", "if", "one", "two", "all", "none", "only", "skip", "stop",
     "continue", "more", "less", "full", "scoped", "quick", "deep", "pick", "where", "why",
     "who", "use", "retry", "choose", "try", "prefer", "wait", "ship"}
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
    context: str | None = Field(default=None, min_length=10, max_length=MAX_CONTEXT)


class JudgeBatch(BaseModel):
    """One to five questions answered in one call (one model step for the agent)."""

    model_config = ConfigDict(extra="forbid")

    questions: list[JudgeRequest] = Field(min_length=1, max_length=MAX_BATCH)


def register() -> None:
    """(Re)declare the point; its log features are the only ones ever written."""
    ds.register_point(
        POINT,
        "yes_no",  # a dynamic point takes any kind per call
        {
            "kind": ("yes_no", "choice", "score"),
            "n_options": int,
            "n_facts": int,
            "has_context": bool,
            "batch": int,
            "low_confidence": bool,
            "role": (*labels.ROLE_IDS, "unknown"),
        },
        threshold=0.0,
        dynamic=True,
        default=ds.LIVE,
    )


_calls: dict[str, tuple[int, float]] = {}
_burst: dict[str, deque[float]] = {}
_questions: dict[str, deque[float]] = {}


def _limit(environ: Mapping[str, str] | None, name: str, default: int) -> int:
    env = os.environ if environ is None else environ
    return max(1, int(da._number(env.get(name), default)))


def check_call(
    session: str,
    *,
    now: float | None = None,
    environ: Mapping[str, str] | None = None,
    questions: int = 1,
) -> float | None:
    """``None`` and count the call, or the seconds until the session may call again.

    At most ``AE_DECISION_SESSION_HOURLY`` calls an hour and ``AE_DECISION_SESSION_MINUTE`` in
    any 60 s per session (a batch is one call), and at most
    ``AE_DECISION_SESSION_QUESTIONS_MINUTE`` questions in any 60 s (a batch of five is five).
    """
    stamp = time.monotonic() if now is None else now
    hourly = _limit(environ, HOURLY_ENV, MAX_PER_HOUR)
    per_minute = _limit(environ, MINUTE_ENV, MAX_PER_MINUTE)
    per_minute_questions = _limit(environ, QUESTIONS_ENV, MAX_QUESTIONS_PER_MINUTE)
    count, first = _calls.get(session, (0, stamp))
    if stamp - first >= RATE_WINDOW_S:
        count, first = 0, stamp
    recent = (_burst.get(session) if session in _calls else None) or deque()
    asked = (_questions.get(session) if session in _calls else None) or deque()
    while recent and stamp - recent[0] >= BURST_WINDOW_S:
        recent.popleft()
    while asked and stamp - asked[0] >= BURST_WINDOW_S:
        asked.popleft()
    if count >= hourly:
        _calls[session] = (count, first)
        return max(1.0, first + RATE_WINDOW_S - stamp)
    if len(recent) >= per_minute:
        _calls[session] = (count, first)
        return max(1.0, recent[0] + BURST_WINDOW_S - stamp)
    if len(asked) + questions > per_minute_questions:
        _calls[session] = (count, first)
        return max(1.0, (asked[0] if asked else stamp) + BURST_WINDOW_S - stamp)
    if session not in _calls and len(_calls) >= MAX_SESSIONS:
        for key in [k for k, (_, t) in _calls.items() if stamp - t >= RATE_WINDOW_S]:
            del _calls[key]  # only counters whose hour is over
            _burst.pop(key, None)
            _questions.pop(key, None)
        if len(_calls) >= MAX_SESSIONS:
            return BURST_WINDOW_S  # refuse a new session rather than evict a live counter
    _calls[session] = (count + 1, first)
    recent.append(stamp)
    _burst[session] = recent
    asked.extend([stamp] * questions)
    _questions[session] = asked
    return None


def refund_call(session: str, *, questions: int = 1) -> None:
    """Undo what :func:`check_call` counted for one call that never reached the judge."""
    count, first = _calls.get(session, (0, 0.0))
    if count <= 0:
        return
    _calls[session] = (count - 1, first)
    recent = _burst.get(session)
    if recent:
        recent.pop()
    asked = _questions.get(session)
    for _ in range(min(questions, len(asked or ()))):
        asked.pop()  # type: ignore[union-attr]


def asked_within(session: str, seconds: float, *, now: float | None = None) -> bool:
    """Whether *session* called ``ask_judge`` in the last *seconds* (read only)."""
    stamp = time.monotonic() if now is None else now
    recent = _burst.get(session) if session in _calls else None
    return bool(recent) and stamp - recent[-1] < seconds


def allow_call(
    session: str, *, now: float | None = None, environ: Mapping[str, str] | None = None
) -> bool:
    """:func:`check_call` as a yes/no."""
    return check_call(session, now=now, environ=environ) is None


def _log_refusal(code: str, role: str | None, session: str | None = None, n: int = 1) -> None:
    """Shadow lines for a refused or rate-limited call: *n* lines, one per question.

    The reason code, the role and the session id (like every other shadow line, so the report
    can tell a nudge was followed by a refused ask); never text, a key or a hash. The report
    route counts one ask per line and never returns an id.
    """
    with contextlib.suppress(Exception):  # logging never breaks a tool call
        for _ in range(max(1, n)):
            ds.write_line(
                {
                    "at": ds._iso(ds._now()),
                    "point": POINT,
                    "session": ds._safe_label(session),
                    "features": {"role": role if role in labels.ROLE_IDS else "unknown"},
                    "error_class": ds._safe_label(code),
                }
            )


#: One fixed line per refusal code, so the agent can fix the wording at once. Never built from
#: the agent's text (2026-11-18).
HINTS = {
    "refused_proper_noun": (
        "a capital letter after the first word reads as a name; use lower case, a generic "
        "word, or a listed tech acronym in capitals"
    ),
    "refused_key": "keys are lower_snake_case labels of your own, not words copied from the user",
    "refused_denylist": "a word on the server's block list; describe it in generic terms",
    "refused_path": "a slash reads as a path; write 'pass or fail', not pass/fail",
    "refused_url": "a dotted name reads as a link; leave out file names and domains",
    "refused_email": "leave out addresses",
    "refused_quote": "leave out quote marks; an apostrophe inside a word is fine",
    "refused_backtick": "leave out backticks",
    "refused_newline": "one line only",
    "refused_digits": "numbers of 4 or more digits are refused; put counts in facts as numbers",
    "refused_passthrough": "this repeats the user's own words; say it in words of your own",
    "refused_fact": "a fact string must be short, neutral lower-case words",
    "refused_context": "the context must be one neutral line of plain words",
    "refused_no_denylist": "the judge is not set up on this server; decide yourself",
    "bad_request": "a choice needs two options and a score needs two levels",
}


def _reply(error: str, retry_after: float | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {"answer": None, "probs": {}, "low_confidence": True, "error": error}
    if retry_after is not None:
        out["retry_after_s"] = max(1, math.ceil(retry_after))
    if error in HINTS:
        out["hint"] = HINTS[error]
    return out


def _check(
    text: str,
    cfg: da.Config,
    deny: list[str],
    texts: Iterable[str] | da.OwnerIndex,
    *,
    copy: bool = True,
    inside_min: int = 2,
    opening_free: bool = False,
) -> str | None:
    return da.check_summary(
        text,
        deny,
        cfg.term_allow,
        texts if copy else [],
        leading=AGENT_LEADING,
        agent=True,
        inside_min=inside_min,
        opening_free=opening_free,
    )


def refusal(
    request: JudgeRequest, cfg: da.Config, deny: list[str], texts: list[str] | da.OwnerIndex
) -> str | None:
    """A reason code for the first agent-written text the guard refuses, else ``None``."""
    if request.kind == "choice" and not (request.options and len(request.options) >= 2):
        return "bad_request"
    if request.kind == "score" and not (request.levels and len(request.levels) >= 2):
        return "bad_request"
    # The question and the context line are the agent's own sentences: their first word is free
    # (2026-11-18); every other piece keeps the strict first-word rule.
    free: list[str] = [request.question]
    if request.context:
        free.append(request.context)  # same guard and copy check as the question
    pieces: list[str] = []
    keys = [o.key for o in request.options or []] + list(request.facts or {})
    for key in keys:
        if not KEY.fullmatch(key):
            return "refused_key"
        # A key is text the judge sees: same guard as the question, `_` read as a space. A
        # key is a label, not a copy: it is compared with the user's messages only as a whole
        # message inside it, a shared run of 6 words, or when it is 4 or more words long.
        words = key.replace("_", " ")
        if _check(words, cfg, deny, texts, inside_min=PASSTHROUGH_INSIDE_MIN):
            return "refused_key"
    for option in request.options or []:
        pieces.append(option.description)
    levels = request.levels or []
    for value in (request.facts or {}).values():
        if isinstance(value, str):
            pieces.append(value)
        elif isinstance(value, float) and not math.isfinite(value):
            return "bad_request"
    for piece, opening_free in [(f, True) for f in free] + [(p, False) for p in pieces]:
        code = _check(piece, cfg, deny, texts, opening_free=opening_free)
        if code:
            return f"refused_{code}"
    for level in levels:  # a short level may sit inside a user message, like a key
        code = _check(level, cfg, deny, texts, inside_min=PASSTHROUGH_INSIDE_MIN)
        if code:
            return f"refused_{code}"
    return None


def low_confidence(
    answer: Any, probs: Mapping[str, float], cfg: da.Config, known: list[str]
) -> bool:
    """The advisor's rule (``decision_advisor.decide``): confident enough and ahead by margin."""
    label = ("yes" if answer else "no") if isinstance(answer, bool) else answer
    return not da.decide(label, probs, cfg.min_confidence, known)


class _Prepared(BaseModel):
    """One question after every guard, with the exact arguments the judge will get."""

    kind: Literal["yes_no", "choice", "score"]
    facts: dict[str, Any]
    args: dict[str, Any]
    known: list[str]
    shown: dict[str, Any]


def prepare(
    request: JudgeRequest,
    cfg: da.Config,
    deny: list[str],
    *,
    role: str | None,
    user_messages: list[str] | da.OwnerIndex,
    batch: int,
) -> tuple[str | None, _Prepared | None]:
    """The whole guard, synchronously: a refusal code, or the question ready to send.

    Runs for every question of a batch before any of them is sent, so a bad last question
    cannot leave the earlier ones already on their way to the judge.
    """
    code = refusal(request, cfg, deny, user_messages)
    if code:
        return code, None
    facts: dict[str, Any] = {}
    for key, value in (request.facts or {}).items():
        if isinstance(value, str):
            value = da.guard_summary(
                value, deny, cfg.term_allow, [], leading=AGENT_LEADING, agent=True
            )
            if value is None:
                return "refused_fact", None
        facts[key] = value
    if request.context:
        situation = da.guard_summary(
            request.context,
            deny,
            cfg.term_allow,
            [],
            leading=AGENT_LEADING,
            agent=True,
            opening_free=True,
        )
        if situation is None:
            return "refused_context", None
        facts[SITUATION] = situation
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
        "has_context": bool(request.context),
        "batch": batch,
        "role": role if role in labels.ROLE_IDS else "unknown",
    }
    return None, _Prepared(kind=request.kind, facts=facts, args=args, known=known, shown=shown)


async def _ask_one(
    prepared: _Prepared,
    cfg: da.Config,
    *,
    session: str,
    environ: Mapping[str, str] | None,
    judge: Judge | None,
    batch_failures: list[bool],
) -> dict[str, Any]:
    """One prepared question to the judge. Never raises."""
    try:
        known = prepared.known

        def late(verdict: Any) -> dict[str, Any]:
            return {"low_confidence": low_confidence(verdict.answer, verdict.probs, cfg, known)}

        register()
        outcome = await ds.ask(
            POINT,
            judge or Decision1Judge(cfg),
            prepared.kind,
            prepared.facts,
            baseline=None,
            session=session,
            environ=environ,
            log_features=prepared.shown,
            late_features=late,
            batch_failures=batch_failures,
            order=known,
            **prepared.args,
        )
        if outcome.mode == ds.OFF:
            return _reply("judge_off")
        verdict = outcome.verdict
        if verdict is None:
            if outcome.error in (da.RATE_LIMITED, "rate_limited"):
                return _reply("rate_limited", da.retry_after_s() or da.DEFAULT_RETRY_S)
            if outcome.error == "minute_cap":
                return _reply("rate_limited", 30.0)  # the server-wide minute is full
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


def _questions_of(request: JudgeRequest | JudgeBatch) -> list[JudgeRequest]:
    return request.questions if isinstance(request, JudgeBatch) else [request]


def fail_reply(
    request: JudgeRequest | JudgeBatch, error: str, retry_after: float | None = None
) -> dict[str, Any]:
    """*error* shaped like the reply to *request* (one answer per question for a batch)."""
    one = _reply(error, retry_after)
    if isinstance(request, JudgeBatch):
        return {"answers": [dict(one) for _ in request.questions]}
    return one


def precheck(
    request: JudgeRequest | JudgeBatch,
    *,
    session: str,
    owner: str | None,
    role: str | None,
    environ: Mapping[str, str] | None = None,
) -> tuple[dict[str, Any] | None, da.Config | None, list[str]]:
    """The cheap checks, before any transcript is read (2026-11-18).

    Judge on and allowed for the owner, the session's rate (this counts the call), and the
    denylist. Returns ``(reply, None, [])`` when the call is answered here, else
    ``(None, config, denylist)``. Never raises.
    """
    n = len(_questions_of(request))
    try:
        cfg = da.config(environ)
        if not (cfg.enabled and cfg.endpoint and cfg.api_key and owner):
            return fail_reply(request, "judge_off"), None, []
        if owner.lower() not in cfg.users:
            return fail_reply(request, "judge_off"), None, []
        wait = check_call(session, environ=environ, questions=n)
        if wait is not None:
            _log_refusal("rate_limited", role, session, n)
            return fail_reply(request, "rate_limited", wait), None, []
        deny = da.load_denylist(cfg.denylist_file)
        if not deny:
            _log_refusal("refused_no_denylist", role, session, n)
            return fail_reply(request, "refused_no_denylist"), None, []
        return None, cfg, deny
    except Exception:  # noqa: BLE001 (a tool call never breaks the agent's turn)
        return fail_reply(request, "error"), None, []


def _guard_all(
    questions: list[JudgeRequest],
    cfg: da.Config,
    deny: list[str],
    role: str | None,
    user_messages: list[str],
) -> tuple[list[_Prepared | None], dict[int, str]]:
    """Every question through the whole guard, each on its own (sync; run in a thread)."""
    index = da.OwnerIndex(user_messages)  # the owner's words split once for the whole call
    ready: list[_Prepared | None] = []
    codes: dict[int, str] = {}
    for i, q in enumerate(questions):
        code, prepared = prepare(
            q, cfg, deny, role=role, user_messages=index, batch=len(questions)
        )
        ready.append(prepared)
        if prepared is None:
            codes[i] = code or "error"
    return ready, codes


async def run_prepared(
    request: JudgeRequest | JudgeBatch,
    cfg: da.Config,
    deny: list[str],
    *,
    session: str,
    role: str | None,
    user_messages: list[str],
    environ: Mapping[str, str] | None = None,
    judge: Judge | None = None,
) -> dict[str, Any]:
    """Guard and ask after :func:`precheck` passed. Never raises.

    Each question is answered or refused on its own (2026-11-18): a refused question carries
    its own code, ``hint`` and (in a batch) ``index``; the others are sent and answered.
    """
    questions = _questions_of(request)
    batch = isinstance(request, JudgeBatch)
    try:
        ready, codes = await asyncio.to_thread(
            _guard_all, questions, cfg, deny, role, user_messages
        )
        for code in codes.values():
            _log_refusal(code, role, session)
        batch_failures: list[bool] = []
        asked = await asyncio.gather(
            *(
                _ask_one(
                    prepared,
                    cfg,
                    session=session,
                    environ=environ,
                    judge=judge,
                    batch_failures=batch_failures,
                )
                for prepared in ready
                if prepared is not None
            )
        )
        answers = iter(asked)
        replies: list[dict[str, Any]] = []
        for i, prepared in enumerate(ready):
            if prepared is None:
                one = _reply(codes[i])
                if batch:
                    one["index"] = i
                replies.append(one)
            else:
                replies.append(next(answers))
        return {"answers": replies} if batch else replies[0]
    except Exception:  # noqa: BLE001 (a tool call never breaks the agent's turn)
        return fail_reply(request, "error")


async def run(
    request: JudgeRequest | JudgeBatch,
    *,
    session: str,
    owner: str | None,
    role: str | None,
    user_messages: list[str],
    environ: Mapping[str, str] | None = None,
    judge: Judge | None = None,
) -> dict[str, Any]:
    """The reply for the route. Never raises; a failure is ``error`` and ``low_confidence``.

    A single question answers as before; a :class:`JudgeBatch` answers ``{"answers": [...]}``
    in the order asked, each element shaped like a single reply. The route calls
    :func:`precheck` and :func:`run_prepared` itself so it can read the transcript between them.
    """
    reply, cfg, deny = precheck(request, session=session, owner=owner, role=role, environ=environ)
    if reply is not None or cfg is None:
        return reply or fail_reply(request, "error")
    return await run_prepared(
        request,
        cfg,
        deny,
        session=session,
        role=role,
        user_messages=user_messages,
        environ=environ,
        judge=judge,
    )
