"""The judge interface: one typed question in, one ``Verdict`` out, any model behind it.

Added 2026-11-06. The judge model (Microsoft Decision-1) retires 2027-02-04, so nothing that asks
a question names a model: a decision point calls a :class:`Judge`, and an adapter turns the call
into whatever the current model speaks. Swapping the model is writing one new adapter.

Data rule (the owner's, binding): a judge may see only numbers, booleans, fixed enum labels and
a :data:`GuardedSummary`, a string that only ``decision_advisor.guard_summary`` can mint after
``check_summary`` passes. Never raw user text, paths, URLs, names or hashes of text. Adapters
enforce it a second time: a ``state`` value outside that set returns ``Verdict(error="bad_state")``
and nothing is sent.

Adapters never raise and never put input text in ``Verdict.error``; it is a short class name.
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, NewType, Protocol

if TYPE_CHECKING:
    from .decision_advisor import Config

#: A summary string that passed the guard. Only ``decision_advisor.guard_summary`` makes one.
GuardedSummary = NewType("GuardedSummary", str)

State = Mapping[str, "int | float | bool | str | GuardedSummary"]

YES, NO = "yes", "no"


@dataclass(frozen=True)
class Verdict:
    """One judge answer. ``error`` is a short class name, never a message that holds input."""

    answer: str | bool | None
    probs: dict[str, float] = field(default_factory=dict)
    latency_ms: int = 0
    error: str | None = None
    judge: str = ""


class Judge(Protocol):
    """What a decision point talks to. ``name`` and ``version`` go to the shadow log."""

    name: str
    version: str

    async def yes_no(self, point: str, state: State, question: str) -> Verdict: ...

    async def choice(self, point: str, state: State, options: Mapping[str, str]) -> Verdict: ...

    async def score(self, point: str, state: State, levels: Sequence[str]) -> Verdict: ...


def parse_probabilities(answer: Any) -> dict[str, float]:
    """The ``probabilities`` of one judge answer, numbers only; anything else gives ``{}``."""
    probs = answer.get("probabilities") if isinstance(answer, dict) else None
    if not isinstance(probs, dict):
        return {}
    return {
        str(k): float(v)
        for k, v in probs.items()
        if isinstance(v, int | float) and not isinstance(v, bool)
    }


class ValidatedState(dict):  # type: ignore[type-arg]
    """A state ``decision_shadow.ask`` already filtered and checked, once, before it queued.

    Adapters accept it without a second look, so a guarded string cannot be evicted from the
    minted set while the call waits for a permit.
    """


def valid_probs(probs: Mapping[str, float]) -> bool:
    """Non-empty, every value finite and within 0 to 1."""
    return bool(probs) and all(math.isfinite(p) and 0.0 <= p <= 1.0 for p in probs.values())


def state_ok(point: str, state: State) -> bool:
    """Whether every value in *state* is allowed for *point* (see ``decision_shadow``)."""
    if type(state) is ValidatedState:
        return True
    from . import decision_shadow  # circular import

    return decision_shadow.validate_state(point, state)


def _ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


class Decision1Judge:
    """Adapter for the Decision-1 endpoint (the request shape ``decision_advisor`` builds).

    The endpoint has ``choice`` and ``score`` question types and no yes/no type, so
    :meth:`yes_no` is a two-option choice over ``"yes"`` and ``"no"``. The *question* is written
    by the calling code, never taken from a user, and is sent as the instructions.
    """

    name = "decision-1"

    def __init__(self, cfg: Config | None = None) -> None:
        self._cfg = cfg

    @property
    def cfg(self) -> Config:
        from . import decision_advisor as da  # circular import

        return self._cfg if self._cfg is not None else da.config()

    @property
    def version(self) -> str:
        return self.cfg.deployment

    async def _run(
        self, point: str, state: State, question: dict[str, Any]
    ) -> tuple[dict[str, Any] | None, Verdict | None, float]:
        """``(answer, None, t0)`` on success, else ``(None, error verdict, t0)``."""
        from . import decision_advisor as da  # circular import

        started = time.monotonic()
        cfg = self.cfg
        if not (cfg.endpoint and cfg.api_key):
            return None, Verdict(None, error="not_configured", judge=self.name), started
        if not state_ok(point, state):
            return None, Verdict(None, error="bad_state", judge=self.name), started
        body = {"model": cfg.deployment, "state": dict(state), "questions": {"q": question}}
        try:
            data = await da._post(cfg, body)
        except da.AdvisorError as exc:
            return (
                None,
                Verdict(None, error=str(exc), latency_ms=_ms(started), judge=self.name),
                started,
            )
        except Exception as exc:  # noqa: BLE001 (a judge never raises; the class name is enough)
            return (
                None,
                Verdict(None, error=type(exc).__name__, latency_ms=_ms(started), judge=self.name),
                started,
            )
        answer = data["answers"].get("q")
        if not isinstance(answer, dict):
            return (
                None,
                Verdict(None, error="bad_shape", latency_ms=_ms(started), judge=self.name),
                started,
            )
        return answer, None, started

    async def _choice(
        self, point: str, state: State, options: Mapping[str, str], instructions: str
    ) -> Verdict:
        question = {"type": "choice", "instructions": instructions, "criteria": dict(options)}
        answer, failed, started = await self._run(point, state, question)
        if failed is not None or answer is None:
            return failed or Verdict(None, error="bad_shape", judge=self.name)
        picked = answer.get("choice")
        probs = parse_probabilities(answer)
        if not valid_probs(probs):
            return Verdict(None, error="bad_shape", latency_ms=_ms(started), judge=self.name)
        return Verdict(
            picked if isinstance(picked, str) and picked in options else None,
            probs,
            _ms(started),
            None,
            self.name,
        )

    async def yes_no(self, point: str, state: State, question: str) -> Verdict:
        verdict = await self._choice(
            point, state, {YES: "The answer is yes.", NO: "The answer is no."}, question
        )
        answer = verdict.answer
        return Verdict(
            answer == YES if answer in (YES, NO) else None,
            verdict.probs,
            verdict.latency_ms,
            verdict.error,
            self.name,
        )

    async def choice(self, point: str, state: State, options: Mapping[str, str]) -> Verdict:
        return await self._choice(point, state, options, "Which option fits this state best?")

    async def score(self, point: str, state: State, levels: Sequence[str]) -> Verdict:
        question = {
            "type": "score",
            "instructions": "Which level fits this state best?",
            "criteria": list(levels),
        }
        answer, failed, started = await self._run(point, state, question)
        if failed is not None or answer is None:
            return failed or Verdict(None, error="bad_shape", judge=self.name)
        raw = parse_probabilities(answer)
        if not valid_probs(raw):
            return Verdict(None, error="bad_shape", latency_ms=_ms(started), judge=self.name)
        legend = answer.get("legend") if isinstance(answer.get("legend"), dict) else {}

        def label(key: str) -> str | None:
            name = legend.get(key)
            if isinstance(name, str) and name in levels:
                return name
            return levels[int(key)] if key.isdigit() and int(key) < len(levels) else None

        probs = {name: p for k, p in raw.items() if (name := label(k)) is not None}
        if not probs:
            return Verdict(None, error="bad_shape", latency_ms=_ms(started), judge=self.name)
        top = max(probs, key=lambda k: probs[k])
        return Verdict(top, probs, _ms(started), None, self.name)


class RuleJudge:
    """Wraps a plain ``callable(state) -> answer``: the baseline, or a point with no model.

    Does no I/O. A callable that raises gives ``Verdict(error=<exception class name>)``.
    """

    version = "rule"

    def __init__(self, rule: Callable[[State], Any], name: str = "rule") -> None:
        self._rule = rule
        self.name = name

    def _verdict(self, state: State) -> Verdict:
        started = time.monotonic()
        try:
            answer = self._rule(state)
        except Exception as exc:  # noqa: BLE001 (a judge never raises)
            return Verdict(
                None, error=type(exc).__name__, latency_ms=_ms(started), judge=self.name
            )
        if answer is not None and not isinstance(answer, str | bool):
            return Verdict(None, error="bad_answer", latency_ms=_ms(started), judge=self.name)
        return Verdict(answer, {}, _ms(started), None, self.name)

    async def yes_no(self, point: str, state: State, question: str) -> Verdict:  # noqa: ARG002
        return self._verdict(state)

    async def choice(self, point: str, state: State, options: Mapping[str, str]) -> Verdict:  # noqa: ARG002
        return self._verdict(state)

    async def score(self, point: str, state: State, levels: Sequence[str]) -> Verdict:  # noqa: ARG002
        return self._verdict(state)
