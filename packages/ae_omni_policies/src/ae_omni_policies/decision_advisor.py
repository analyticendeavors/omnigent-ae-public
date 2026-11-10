"""Decision-1 as the "lead picks" advisor: which preset fits a task.

Added 2026-11-06. A lead in a ``lead-picks`` session calls the ``advise_preset`` tool with a
one-line neutral summary and a few counts. The server (never the lead, never the agent host)
holds the key, runs the guard below, asks the judge model one ``choice`` question over the
owner's presets and one ``score`` question for task size, and answers with a verdict. Any
failure, refusal or low confidence answers ``decide_yourself``; a turn is never blocked.

Data rule: raw user prompts never leave the server. Only the lead's summary goes out, and only
after the guard passes. A refusal names a reason CODE, never the text that matched, so a
denylisted term is not echoed into a reply, a log or a run line.

Like ``azure_cost.py`` this builds no ``httpx`` client, so there is no request log, and errors
name an exception class only. The feature is removable in one PR (the judge model retires
2027-02-04): this module, its route and tool, the roles line patch and the secrets block.

Environment (all optional; the advisor is off unless every required one is set):

* ``AE_DECISION_ENABLED`` (``0``): ``1`` turns it on.
* ``AE_DECISION_ENDPOINT``: base URL, no default (the host is not in this repo).
* ``AE_DECISION_API_KEY``: the key, sent as the ``api-key`` header.
* ``AE_DECISION_DEPLOYMENT`` (``decision-1``): the ``model`` in the request body.
* ``AE_DECISION_USERS``: comma-separated owners allowed to use it (empty: nobody).
* ``AE_DECISION_DENYLIST_FILE``: one term per line, a private file; missing or empty fails closed.
* ``AE_DECISION_TERM_ALLOW``: comma-separated capitalized terms that are not names (PR, Fabric).
* ``AE_DECISION_TIMEOUT_S`` (``3``), ``AE_DECISION_MIN_CONFIDENCE`` (``0.6``).
* ``AE_DECISION_LOG_SUMMARY`` (``0``): ``1`` keeps the summary text in the run log.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .judge import GuardedSummary
from .judge import parse_probabilities as _probabilities

_logger = logging.getLogger(__name__)

PATH = "/providers/microsoft/v1/systemone"
USE = "use"
DECIDE_YOURSELF = "decide_yourself"
ABSTAIN = "abstain"
MAX_PRESETS = 8
TEXT_MAX = 200
MARGIN = 0.15
SIZE_LEVELS = ["trivial", "small", "medium", "large"]
MAX_PER_RUN = 10
RATE_WINDOW_S = 3600.0
SIZE_MIN_PROBABILITY = 0.5
KINDS = ("code", "docs", "data", "ops", "research", "other")

# Terms that start with a capital but name no client, person or product.
DEFAULT_TERM_ALLOW = frozenset(
    {"pr", "prs", "api", "ci", "css", "html", "json", "yaml", "sql", "ui", "ux", "url", "mcp",
     "readme", "python", "javascript", "typescript", "github", "docker", "linux"}
)  # fmt: skip

# A scheme, www, or any domain-shaped token (two letters or more per label, so "e.g." and
# versions such as "v1.2" or "3.11" pass).
_URL = re.compile(r"(https?:|www\.|\b[^\W\d_]{2,}(?:\.[^\W\d_]{2,})+\b)", re.IGNORECASE)
_EMAIL = re.compile(r"\S+@\S+")
_PATH = re.compile(r"(^|\s)[~.]?/\w|\\\w|\w/\w")
_DIGITS = re.compile(r"\d{4,}")
_DIGIT_GROUP = re.compile(r"\d+(?:[ .\-]\d+)+")  # "555 12 34", "12-34-56-78"
_QUOTES = re.compile(r"[\"'‘’“”]")
_WORD = re.compile(r"[^\W\d_][\w+#-]*")
# Words that may start a summary without being names (lower case).
LEADING_WORDS = frozenset(
    {"fix", "add", "update", "build", "write", "review", "debug", "draft", "plan", "refactor",
     "design", "investigate", "migrate", "remove", "rename", "test", "document", "set", "create",
     "make", "research", "clean", "move", "check", "run", "deploy", "wire", "port", "split",
     "merge", "speed", "tune", "explain", "summarize", "answer", "triage", "the", "a", "an"}
)  # fmt: skip
SENTENCE_END = re.compile(r"[.!?]\s+$")


def _flag(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "on", "yes"}


def _list(value: str | None) -> list[str]:
    return [part.strip() for part in (value or "").split(",") if part.strip()]


def _number(value: str | None, default: float) -> float:
    try:
        return float(value) if value not in (None, "") else default
    except ValueError:
        return default


@dataclass(frozen=True)
class Config:
    enabled: bool
    endpoint: str
    api_key: str = field(repr=False)
    deployment: str
    users: frozenset[str]
    denylist_file: str
    term_allow: frozenset[str]
    timeout_s: float
    min_confidence: float
    log_summary: bool


def config(environ: Mapping[str, str] | None = None) -> Config:
    env = os.environ if environ is None else environ
    return Config(
        enabled=_flag(env.get("AE_DECISION_ENABLED")),
        endpoint=(env.get("AE_DECISION_ENDPOINT") or "").strip().rstrip("/"),
        api_key=(env.get("AE_DECISION_API_KEY") or "").strip(),
        deployment=(env.get("AE_DECISION_DEPLOYMENT") or "decision-1").strip(),
        users=frozenset(u.lower() for u in _list(env.get("AE_DECISION_USERS"))),
        denylist_file=(env.get("AE_DECISION_DENYLIST_FILE") or "").strip(),
        # A multi-word term ("Power BI") is allowed as a whole phrase only.
        term_allow=DEFAULT_TERM_ALLOW
        | {" ".join(t.lower().split()) for t in _list(env.get("AE_DECISION_TERM_ALLOW"))},
        timeout_s=_number(env.get("AE_DECISION_TIMEOUT_S"), 3.0),
        min_confidence=_number(env.get("AE_DECISION_MIN_CONFIDENCE"), 0.6),
        log_summary=_flag(env.get("AE_DECISION_LOG_SUMMARY")),
    )


def load_denylist(path: str) -> list[str]:
    """The denylist terms; an unset, missing or empty file gives ``[]`` (which fails closed)."""
    if not path:
        return []
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except (OSError, ValueError):  # ValueError: not valid UTF-8, which fails closed
        return []
    return [line.strip() for line in lines if line.strip() and not line.startswith("#")]


def available(owner: str | None, environ: Mapping[str, str] | None = None) -> bool:
    """Whether the advisor would answer for *owner* (on, configured, allowed, denylist present)."""
    cfg = config(environ)
    return bool(
        cfg.enabled
        and cfg.endpoint
        and cfg.api_key
        and owner
        and owner.lower() in cfg.users
        and load_denylist(cfg.denylist_file)
    )


# ---------------------------------------------------------------------------
# The guard. Every check answers a reason code or None; none returns the matched text.


def _squash(text: str) -> str:
    """Runs of whitespace (tabs included) become one space."""
    return " ".join(text.split())


_SEP = re.compile(r"[_\-]+")


def _plain(text: str) -> str:
    """Whitespace squashed and `_`, `-` read as spaces, so `rebill_contoso` hits `contoso`."""
    return _squash(_SEP.sub(" ", text))


def _has_term(text: str, terms: Iterable[str]) -> bool:
    text = _plain(text)
    for term in terms:
        term = _plain(term)
        if term and re.search(rf"(?<!\w){re.escape(term)}(?!\w)", text, re.IGNORECASE):
            return True
    return False


def denylist_hit(text: str, terms: list[str]) -> bool:
    return _has_term(text, terms)


def _name_like(
    text: str,
    allow: frozenset[str],
    *,
    first_word_free: bool = False,
    leading: frozenset[str] = LEADING_WORDS,
) -> bool:
    """True when *text* holds a capitalized word that is not an allowed term.

    The first word of the text is checked too, unless it is a common leading word or allowed
    (``first_word_free`` skips it, and the first word after a full stop, for owner-written
    preset text). A one-letter token is never a name. A capital after a hyphen counts.
    """
    for phrase in sorted((a for a in allow if " " in a), key=len, reverse=True):
        text = re.sub(rf"(?<!\w){re.escape(phrase)}(?!\w)", " ", text, flags=re.IGNORECASE)
    for match in _WORD.finditer(text):
        word = match.group(0)
        first = not text[: match.start()].strip() or (
            first_word_free and SENTENCE_END.search(text[: match.start()]) is not None
        )
        parts = word.split("-")
        for n, part in enumerate(parts):
            if len(part) < 2 or not part[0].isupper():
                continue
            lowered = part.lower()
            if lowered in allow:
                continue
            if n == 0 and first and (first_word_free or lowered in leading):
                continue
            return True
    return False


def check_text(text: str, deny: list[str], allow: frozenset[str]) -> str | None:
    """A refusal code for any string that would leave the server (denylist and names only)."""
    if denylist_hit(text, deny):
        return "denylist"
    if _name_like(text, allow, first_word_free=True):
        return "proper_noun"
    return None


PASSTHROUGH_RUN = 6
_NON_WORD = re.compile(r"[\W_]+")


def _words(text: str) -> list[str]:
    return _NON_WORD.sub(" ", text.lower()).split()


def passthrough(field: str, user_messages: Iterable[str]) -> bool:
    """Whether *field* copies the user, on lower-case words with punctuation, ``_`` and ``-`` gone.

    True when (a) a whole user message of two or more words sits inside the field, (b) a field
    of two or more words sits inside a user message, or (c) they share a run of
    :data:`PASSTHROUGH_RUN` or more words. A one-word field is never a copy under (b).
    """
    mine = _words(field)
    if not mine:
        return False
    padded = f" {' '.join(mine)} "
    grams = {tuple(mine[i : i + PASSTHROUGH_RUN]) for i in range(len(mine) - PASSTHROUGH_RUN + 1)}
    for message in user_messages:
        theirs = _words(message)
        if len(theirs) >= 2 and f" {' '.join(theirs)} " in padded:
            return True
        if len(mine) >= 2 and padded in f" {' '.join(theirs)} ":
            return True
        if grams and any(
            tuple(theirs[i : i + PASSTHROUGH_RUN]) in grams
            for i in range(len(theirs) - PASSTHROUGH_RUN + 1)
        ):
            return True
    return False


def check_summary(
    summary: str,
    deny: list[str],
    allow: frozenset[str],
    user_messages: Iterable[str],
    *,
    leading: frozenset[str] = LEADING_WORDS,
) -> str | None:
    """A refusal code for *summary*, or ``None`` when it may be sent."""
    if not deny:
        return "no_denylist"
    if "\n" in summary or "\r" in summary:
        return "newline"
    if _EMAIL.search(summary):
        return "email"
    if _URL.search(summary):
        return "url"
    if _PATH.search(summary):
        return "path"
    if "`" in summary:
        return "backtick"
    if _QUOTES.search(summary):
        return "quote"
    if _DIGITS.search(summary) or any(
        sum(c.isdigit() for c in m.group(0)) > 3 for m in _DIGIT_GROUP.finditer(summary)
    ):
        return "digits"
    if denylist_hit(summary, deny):
        return "denylist"
    if _name_like(summary, allow, leading=leading):
        return "proper_noun"
    if passthrough(summary, user_messages):
        return "passthrough"
    return None


_minted: dict[str, None] = {}
MINTED_MAX = 4096


def guard_summary(
    summary: str,
    deny: list[str],
    allow: frozenset[str],
    user_messages: Iterable[str],
    *,
    leading: frozenset[str] = LEADING_WORDS,
) -> GuardedSummary | None:
    """*summary* as a :data:`GuardedSummary` when :func:`check_summary` passes, else ``None``.

    The only place one is made. The value is remembered (the last :data:`MINTED_MAX`) so a judge
    adapter can tell a guarded string from a plain one at run time with :func:`is_guarded`.
    """
    if check_summary(summary, deny, allow, user_messages, leading=leading) is not None:
        return None
    _minted.pop(summary, None)
    _minted[summary] = None
    while len(_minted) > MINTED_MAX:
        del _minted[next(iter(_minted))]
    return GuardedSummary(summary)


def is_guarded(value: str) -> bool:
    return value in _minted


# ---------------------------------------------------------------------------
# Request


class AdviseRequest(BaseModel):
    """What the lead supplies. The server builds the judge's ``state`` from these fields only."""

    model_config = ConfigDict(extra="forbid")

    summary: str = Field(min_length=10, max_length=160)
    files: int = Field(default=0, ge=0, le=10000)
    repos: int = Field(default=0, ge=0, le=20)
    kind: Literal["code", "docs", "data", "ops", "research", "other"] = "other"
    needs_design: bool = False
    has_tests: bool = False

    @field_validator("summary")
    @classmethod
    def _strip(cls, value: str) -> str:
        return value.strip()


def preset_text(preset: Mapping[str, Any]) -> str:
    text = str(preset.get("when_to_use") or preset.get("description") or "").strip()
    return text[:TEXT_MAX]


def build_questions(criteria: Mapping[str, str]) -> dict[str, Any]:
    return {
        "preset": {
            "type": "choice",
            "instructions": (
                "Which working setup fits this task best? Answer abstain when none clearly fits."
            ),
            "criteria": {**criteria, ABSTAIN: "None of the above clearly fits."},
        },
        "size": {
            "type": "score",
            "instructions": "How big is this task?",
            "criteria": SIZE_LEVELS,
        },
    }


def build_state(request: AdviseRequest) -> dict[str, Any]:
    return {
        "task": request.summary,
        "files": request.files,
        "repos": request.repos,
        "kind": request.kind,
        "needs_design": request.needs_design,
        "has_tests": request.has_tests,
    }


def summary_hash(summary: str) -> str:
    return hashlib.sha256(summary.encode()).hexdigest()[:12]


# ---------------------------------------------------------------------------
# The call


class AdvisorError(Exception):
    """A failed call; the message names a class or status, never a body or a key."""


_transport: httpx.AsyncHTTPTransport | None = None
_transport_loop: asyncio.AbstractEventLoop | None = None


def _shared_transport() -> httpx.AsyncBaseTransport:
    global _transport, _transport_loop
    loop = asyncio.get_running_loop()
    if _transport is None or _transport_loop is not loop:
        _transport = httpx.AsyncHTTPTransport()
        _transport_loop = loop
    return _transport


async def _post(cfg: Config, body: dict[str, Any]) -> dict[str, Any]:
    request = httpx.Request(
        "POST",
        cfg.endpoint + PATH,
        headers={"api-key": cfg.api_key, "Content-Type": "application/json"},
        content=json.dumps(body).encode(),
        extensions={"timeout": httpx.Timeout(cfg.timeout_s).as_dict()},
    )
    transport = _shared_transport()
    try:
        async with asyncio.timeout(cfg.timeout_s):
            response = await transport.handle_async_request(request)
            try:
                raw = await response.aread()
            finally:
                await response.aclose()
    except (httpx.TimeoutException, TimeoutError):
        raise AdvisorError("timeout") from None
    except httpx.HTTPError as exc:
        raise AdvisorError(type(exc).__name__) from None
    if response.status_code != 200:
        raise AdvisorError(f"http_{response.status_code}")
    try:
        data = json.loads(raw)
    except ValueError:
        raise AdvisorError("bad_json") from None
    if not isinstance(data, dict) or not isinstance(data.get("answers"), dict):
        raise AdvisorError("bad_shape")
    return data


def decide(
    choice: Any, probs: Mapping[str, float], min_confidence: float, known: Iterable[str]
) -> bool:
    """True when the top choice is a real preset, confident enough, and ahead by the margin."""
    known_ids = set(known)
    if not isinstance(choice, str) or choice == ABSTAIN or choice not in known_ids:
        return False
    top = probs.get(choice)
    if top is None or top < min_confidence:
        return False
    rest = [v for k, v in probs.items() if k != choice]
    return top - (max(rest) if rest else 0.0) >= MARGIN


def size_label(answer: Mapping[str, Any]) -> str | None:
    """The legend label of the most likely size level, when it holds at least half the weight.

    The live ``score`` is a probability-weighted index (a float), so it is not read as a level.
    """
    legend, probs = answer.get("legend"), _probabilities(answer)
    if not isinstance(legend, dict) or not probs:
        return None
    top = max(probs, key=lambda k: probs[k])
    label = legend.get(top)
    if probs[top] < SIZE_MIN_PROBABILITY or not isinstance(label, str) or not label.strip():
        return None
    return label.strip()[:30]


_calls: dict[str, tuple[int, float]] = {}


def _allow_call(session: str, *, now: float | None = None) -> bool:
    """At most :data:`MAX_PER_RUN` calls per session per hour (a looping lead stops costing)."""
    stamp = time.monotonic() if now is None else now
    count, first = _calls.get(session, (0, stamp))
    if stamp - first >= RATE_WINDOW_S:
        count, first = 0, stamp
    if count >= MAX_PER_RUN:
        _calls[session] = (count, first)
        return False
    if len(_calls) > 1000:
        _calls.clear()
    _calls[session] = (count + 1, first)
    return True


def _reply(verdict: str, **extra: Any) -> dict[str, Any]:
    return {"verdict": verdict, **extra}


async def advise(
    request: AdviseRequest,
    presets: list[dict[str, Any]],
    user_messages: Iterable[str],
    *,
    owner: str | None,
    environ: Mapping[str, str] | None = None,
    session: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """``(reply, log_entry)``. The reply goes to the lead; the entry goes to the run log.

    Never raises: every failure is ``decide_yourself`` with a ``reason`` code.
    """
    started = time.monotonic()
    cfg = config(environ)
    entry: dict[str, Any] = {
        "at": int(time.time()),
        "summary_sha": summary_hash(request.summary),
        "summary_len": len(request.summary),
        "files": request.files,
        "repos": request.repos,
        "kind": request.kind,
        "needs_design": request.needs_design,
        "has_tests": request.has_tests,
    }

    def done(verdict: str, reason: str | None = None, **extra: Any) -> tuple[dict, dict]:
        entry["verdict"] = verdict
        entry["latency_ms"] = int((time.monotonic() - started) * 1000)
        if reason:
            entry["reason"] = reason
        reply = _reply(verdict, **({"reason": reason} if reason else {}), **extra)
        return reply, entry

    if not cfg.enabled:
        return done(DECIDE_YOURSELF, "advisor_off")
    if not (cfg.endpoint and cfg.api_key and owner and owner.lower() in cfg.users):
        return done(DECIDE_YOURSELF, "advisor_off")
    if session is not None and not _allow_call(session):
        return done(DECIDE_YOURSELF, "rate_limited")
    deny = load_denylist(cfg.denylist_file)
    refusal = check_summary(request.summary, deny, cfg.term_allow, user_messages)
    if refusal:
        return done(DECIDE_YOURSELF, f"refused_{refusal}")
    criteria: dict[str, str] = {}
    titles: dict[str, str] = {}
    for preset in presets[:MAX_PRESETS]:
        pid, text = preset.get("id"), preset_text(preset)
        # Everything sent for a preset (its id and its text) passes the name and denylist checks.
        if (
            not isinstance(pid, str)
            or not text
            or check_text(pid, deny, cfg.term_allow)
            or check_text(text, deny, cfg.term_allow)
        ):
            continue
        criteria[pid] = text
        titles[pid] = str(preset.get("title") or pid)
    if not criteria:
        return done(DECIDE_YOURSELF, "no_presets")
    body = {
        "model": cfg.deployment,
        "state": build_state(request),
        "questions": build_questions(criteria),
    }
    if cfg.log_summary:  # only a summary that is actually sent is kept
        entry["summary"] = request.summary
    try:
        data = await _post(cfg, body)
    except AdvisorError as exc:
        entry["error_class"] = str(exc)
        return done(DECIDE_YOURSELF, "error")
    except Exception as exc:  # noqa: BLE001 (a turn is never blocked)
        entry["error_class"] = type(exc).__name__
        return done(DECIDE_YOURSELF, "error")
    answers = data["answers"]
    pick = answers.get("preset") if isinstance(answers.get("preset"), dict) else {}
    probs = _probabilities(pick)
    entry["preset_probabilities"] = {k: round(v, 3) for k, v in probs.items()}
    size = answers.get("size") if isinstance(answers.get("size"), dict) else {}
    entry["size_probabilities"] = {k: round(v, 3) for k, v in _probabilities(size).items()}
    label = size_label(size)
    extra: dict[str, Any] = {"size": label} if label else {}
    choice = pick.get("choice")
    if decide(choice, probs, cfg.min_confidence, criteria):
        return done(
            USE,
            preset=choice,
            title=titles[choice],
            confidence=round(probs[choice], 3),
            **extra,
        )
    return done(DECIDE_YOURSELF, "low_confidence", **extra)
