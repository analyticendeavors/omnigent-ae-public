"""Cost targets for the Admin Center's Azure card: a limit you set, a phone alarm when crossed.

Added 2026-10-25. The Azure card shows spend, but only to someone who opens it, and a Fabric
capacity left running costs $0.72 an hour while nobody looks. The owner sets limits on the card
(*Targets*); when spend or a running capacity crosses one, the server files a task-store task with
an alarm, the way :mod:`.secret_alerts` does for expiring secrets.

Rules (stored in ``AE_COST_TARGETS_FILE``, default ``/data/ae-cost-targets.json``; empty turns
persistence off): an id, a name, an on/off switch, a scope, a measure and a limit.

- Measures: ``month_to_date`` (USD), ``latest_finished_day`` (USD, the newest finished day that
  has data), ``month_end_forecast`` (USD, the card's pace figure: spend so far over the days with
  data, scaled to the month; needs three days of data so day one cannot fire it) and
  ``fabric_active_hours`` (hours a Fabric capacity has been Active).
- Scopes: all of Azure, one subscription (its name in ``AE_AZURE_COST_SUBSCRIPTIONS``), one
  service (month to date only; the reading keeps services by month, not by day), one resource
  (latest finished day only; the reading keeps nine days of resources, so no month figure
  exists) or, for the Fabric measure, one capacity. A resource or capacity with no name means
  each one, alerted separately.

No new Azure query: spend rules read only what :mod:`.azure_cost` already holds
(:func:`.azure_cost.snapshot`). Cost Management is rate limited and shared. Each subscription is
judged by its own read time (``readAt``): one over six hours old, or never read, gives status
``cannot-evaluate`` and no alert for the rules that need it; a rate limit alone blanks nothing
(the answer's ``note`` says it). Every target is ``cannot-evaluate`` only when Azure is not
configured, was never read, or every subscription is too old.
Amounts are USD as Azure reports them (its ``CostUSD`` column), so a subscription billed in GBP
is judged against its USD limit. Only a reading that fell back to its own non-USD currency
(Azure sent no ``CostUSD``) is skipped.
A total (all, a service, each resource) that is already over its limit alerts even when another
subscription is unreadable, because the missing part could only add to it; a total under its
limit with a subscription unreadable is ``cannot-evaluate``, never a quiet ``ok``.

Seeding (once, only while the rules file does not exist and Azure spend is configured): month to
date over 600 for "MVP SC" and "MVP 2025", over 50 for "AnalyticEndeavors" and
"AnalyticEndeavorsTools" (each only when config names it), any single resource over 25 a day, and
any Fabric capacity Active over 6 hours. All are editable and deletable. The file records that it
was seeded, so a deleted rule never comes back. Fabric alone does not seed: the defaults come
with the Azure card's configuration.

Alerts: one task per rule and subject per period. The period is the month (month to date,
forecast), the data day (latest finished day) or the Active episode (Fabric). The task is filed
in ``AE_COST_ALERT_LIST`` (default "AE Work") with the ``ae`` tag. A spend alarm is the same 09:00
home-clock alarm :func:`.secret_alerts.alarm_instant` gives secret expiries; a Fabric alarm rings
three minutes out, because a running capacity bills by the hour. A value back under its limit in
the same period closes the task with a line saying why. When the period rolls (a new month, a
newer data day) a spend task is forgotten but left open: it is last period's record, for a person
to tick. A Fabric task closes when the capacity stops being Active. Turning a target off or
deleting it closes its open task. A task someone ticked done stays done for the period.

State is ``AE_COST_ALERT_STATE_FILE`` (default ``/data/ae-cost-alerts.json``). If it is lost, open
tasks are searched for their "Alert key" line before anything is filed again, and a create that
fails without an answer is re-read, never resent blind.

Fabric's reading has ``resumedAt`` only for capacities with a wake Logic App, so the tick keeps
its own record: the first time it saw a capacity Active is saved in the state as the start of the
episode (a recent ``resumedAt`` wins when it exists, within 48 hours), and the hours run from
there. A capacity that was Active for a while before the server first looked is therefore
counted from the first look.

Evaluation runs in the lifespan loop (:data:`lifespan_router`, in ``router.DEBUG_ROUTERS``): a
spend pass a minute after any new Azure reading, and a Fabric pass every hour using the existing
read-only fleet reading. Nothing here resumes, suspends or resizes a capacity, and the loop never
raises: it logs the exception class only. No secret is read, logged or returned.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import math
import os
import re
import tempfile
import threading
import time
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any, Literal
from urllib.parse import quote, unquote

from fastapi import APIRouter, FastAPI
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictStr, ValidationError

from ae_omni_policies import azure_cost, fabric, secret_alerts, taskstore

_logger = logging.getLogger(__name__)

RULES_FILE_ENV = "AE_COST_TARGETS_FILE"
STATE_FILE_ENV = "AE_COST_ALERT_STATE_FILE"
LIST_ENV = "AE_COST_ALERT_LIST"
DEFAULT_LIST = "AE Work"
DATA_DIR = Path("/data")
RULES_FILE_NAME = "ae-cost-targets.json"
STATE_FILE_NAME = "ae-cost-alerts.json"
TAG = "ae"
MAX_RULES = 50
NAME_MAX = 80
SCOPE_NAME_MAX = 120
SPEND_LIMIT_MAX = 10_000_000.0
HOURS_LIMIT_MAX = 744.0
READING_MAX_AGE = timedelta(hours=6)
FRESH_DAYS = 4
FORECAST_MIN_DAYS = 3
RESUMED_MAX_AGE = timedelta(hours=48)
FABRIC_ALARM_DELAY = timedelta(minutes=3)
STARTUP_DELAY_S = 30.0
POLL_S = 60.0
FABRIC_EVERY_S = 3600.0
FABRIC_REFRESH_WAIT_S = 60.0
LIST_SCOPE_MAX = 100
SERVICE_LIST_MAX = 50
REASON_MAX = 160
ANSWER_MAX = 32_768
"""The extension bridge refuses a longer answer; :func:`view` trims to fit."""
OVER_SHOWN = 3

Measure = Literal[
    "month_to_date", "latest_finished_day", "month_end_forecast", "fabric_active_hours"
]
Kind = Literal["all", "subscription", "service", "resource", "capacity"]
Status = Literal["ok", "over", "alerted", "cannot-evaluate", "off"]

MEASURES: tuple[str, ...] = (
    "month_to_date",
    "latest_finished_day",
    "month_end_forecast",
    "fabric_active_hours",
)
#: Which scopes each measure can use, and why the rest cannot (module note).
ALLOWED_KINDS: dict[str, tuple[str, ...]] = {
    "month_to_date": ("all", "subscription", "service"),
    "latest_finished_day": ("all", "subscription", "resource"),
    "month_end_forecast": ("all", "subscription"),
    "fabric_active_hours": ("capacity",),
}
NAME_REQUIRED = {"subscription", "service"}
NAME_FORBIDDEN = {"all"}

_RULE_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
_MARKER_LINE = re.compile(
    r"^Alert key: cost (?P<rule>[a-z0-9-]+) (?P<subject>\S+) (?P<period>\S+)$", re.MULTILINE
)
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


# ── rules ───────────────────────────────────────────────────────────────────


class Scope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Kind
    name: StrictStr | None = None


class RuleBody(BaseModel):
    """``PUT /azure-cost/targets/{id}``: everything about a rule but its id."""

    model_config = ConfigDict(extra="forbid")

    name: StrictStr
    enabled: StrictBool = True
    scope: Scope
    measure: Measure
    limit: Annotated[float, Field(strict=True, gt=0, allow_inf_nan=False)]


class Rule(RuleBody):
    id: str


class RuleError(ValueError):
    """A rule the caller must change; the message says what and how."""


@dataclass(frozen=True)
class Known:
    """The names a scope can point at, as far as the server can tell."""

    subscriptions: tuple[str, ...] = ()
    services: tuple[str, ...] = ()
    resources: tuple[str, ...] = ()
    capacities: tuple[str, ...] = ()


def family_of(measure: str) -> Literal["spend", "fabric"]:
    return "fabric" if measure == "fabric_active_hours" else "spend"


def check(rule_id: str, body: RuleBody, known: Known, existing: list[Rule]) -> Rule:
    """The validated rule, or :class:`RuleError` saying what to change."""
    if not _RULE_ID.match(rule_id):
        raise RuleError(
            "The target id must be 1 to 40 lowercase letters, digits or hyphens, starting with "
            "a letter or digit. Use the id the card made."
        )
    name = body.name.strip()
    if not name or len(name) > NAME_MAX or _CONTROL.search(name):
        raise RuleError(f"Give the target a name of 1 to {NAME_MAX} characters on one line.")
    kind = body.scope.kind
    scope_name = (body.scope.name or "").strip() or None
    if kind not in ALLOWED_KINDS[body.measure]:
        allowed = ", ".join(ALLOWED_KINDS[body.measure])
        raise RuleError(
            f"The measure {body.measure} cannot be set on a {kind} scope. Pick one of: {allowed}."
        )
    if kind in NAME_FORBIDDEN and scope_name:
        raise RuleError(
            "A scope of all Azure takes no name. Clear the name or pick another scope."
        )
    if kind in NAME_REQUIRED and not scope_name:
        raise RuleError(f"A {kind} scope needs a name. Pick one from the list.")
    if scope_name is not None:
        if len(scope_name) > SCOPE_NAME_MAX or _CONTROL.search(scope_name):
            raise RuleError(
                "The scope name is too long or has a line break. Pick it from the list."
            )
        _check_known(kind, scope_name, known)
    ceiling = HOURS_LIMIT_MAX if body.measure == "fabric_active_hours" else SPEND_LIMIT_MAX
    if not math.isfinite(body.limit) or not 0 < body.limit <= ceiling:
        unit = "hours" if body.measure == "fabric_active_hours" else "USD"
        raise RuleError(f"The limit must be a number above 0 and at most {ceiling:g} {unit}.")
    others = [r for r in existing if r.id != rule_id]
    if len(others) >= MAX_RULES:
        raise RuleError(
            f"There are already {MAX_RULES} targets. Delete one before adding another."
        )
    return Rule(
        id=rule_id,
        name=name,
        enabled=body.enabled,
        scope=Scope(kind=kind, name=scope_name),
        measure=body.measure,
        limit=body.limit,
    )


def _check_known(kind: str, name: str, known: Known) -> None:
    options = {
        "subscription": known.subscriptions,
        "service": known.services,
        "resource": known.resources,
        "capacity": known.capacities,
    }[kind]
    if kind == "subscription" and not options:
        raise RuleError(
            "Azure spend is not configured on this server, so no subscription can be picked. "
            "Set AE_AZURE_COST_SUBSCRIPTIONS and the Azure principal first."
        )
    # A resource, service or capacity list is empty until the first reading lands; the name
    # cannot be checked then, and the target says cannot-evaluate until it can.
    if options and name not in options:
        raise RuleError(
            f'The {kind} "{name}" is not one this server reads. Pick one from the list '
            "on the card, or wait for the next reading if it is new."
        )


class RulesFile(BaseModel):
    version: int = 1
    seeded: bool = False
    rules: list[Rule] = Field(default_factory=list)


class RulesFileError(Exception):
    """The rules file exists but cannot be read; nothing is changed until it is fixed."""


def _atomic_write(path: Path, text: str, prefix: str) -> None:
    tmp: str | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=prefix, dir=str(path.parent))
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(tmp, path)
    except OSError:
        if tmp is not None:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
        raise


def _path_from(env_name: str, file_name: str) -> Path | None:
    raw = os.environ.get(env_name)
    if raw is not None:
        return Path(raw.strip()) if raw.strip() else None
    return DATA_DIR / file_name if DATA_DIR.is_dir() else None


class RuleStore:
    """The rules, kept in a JSON file (memory only without a path)."""

    def __init__(self, path: Path | None = None) -> None:
        self._lock = threading.RLock()
        self._path = path
        self._memory = RulesFile()

    def read(self) -> RulesFile:
        """:raises RulesFileError: When the file exists and cannot be read."""
        with self._lock:
            if self._path is None:
                return self._memory.model_copy(deep=True)
            try:
                raw = json.loads(self._path.read_text(encoding="utf-8"))
            except FileNotFoundError:
                return RulesFile()
            except (OSError, ValueError) as exc:
                raise RulesFileError(
                    f"The targets file {self._path} cannot be read ({type(exc).__name__}). "
                    "Fix or remove it; no target changes until then."
                ) from None
            if not isinstance(raw, dict):
                raise RulesFileError(
                    f"The targets file {self._path} is not a JSON object. Fix or remove it."
                )
            kept: list[Rule] = []
            for item in raw.get("rules") or []:
                try:
                    kept.append(Rule.model_validate(item))
                except ValidationError:
                    _logger.warning("cost targets: a stored rule was invalid and is ignored")
            # A file that exists without the flag was not made by a seed: never seed over it.
            return RulesFile(seeded=bool(raw.get("seeded", True)), rules=kept)

    def write(self, data: RulesFile) -> None:
        """:raises OSError: When the file cannot be written."""
        with self._lock:
            if self._path is None:
                self._memory = data.model_copy(deep=True)
                return
            _atomic_write(self._path, data.model_dump_json(), ".ae-cost-targets-")


_rule_store: RuleStore | None = None
_store_lock = threading.Lock()


def rule_store() -> RuleStore:
    global _rule_store
    with _store_lock:
        if _rule_store is None:
            _rule_store = RuleStore(_path_from(RULES_FILE_ENV, RULES_FILE_NAME))
        return _rule_store


def seed_rules(subscriptions: list[str]) -> list[Rule]:
    """The defaults of the module note, for the subscriptions config names."""
    rules: list[Rule] = []
    for name, limit in (
        ("MVP SC", 600.0),
        ("MVP 2025", 600.0),
        ("AnalyticEndeavors", 50.0),
        ("AnalyticEndeavorsTools", 50.0),
    ):
        if name in subscriptions:
            slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
            rules.append(
                Rule(
                    id=f"seed-month-{slug}",
                    name=f"{name} month to date",
                    enabled=True,
                    scope=Scope(kind="subscription", name=name),
                    measure="month_to_date",
                    limit=limit,
                )
            )
    rules.append(
        Rule(
            id="seed-resource-day",
            name="Any resource in one day",
            enabled=True,
            scope=Scope(kind="resource"),
            measure="latest_finished_day",
            limit=25.0,
        )
    )
    rules.append(
        Rule(
            id="seed-fabric-hours",
            name="Any Fabric capacity Active",
            enabled=True,
            scope=Scope(kind="capacity"),
            measure="fabric_active_hours",
            limit=6.0,
        )
    )
    return rules


def load_rules(store: RuleStore | None = None) -> RulesFile:
    """The rules, seeding the defaults once. :raises RulesFileError: A file that cannot be read."""
    rules = store or rule_store()
    data = rules.read()
    if data.seeded:
        return data
    if data.rules:
        # A hand-made file with rules and no flag: it is not ours to seed.
        data.seeded = True
        _try_write(rules, data)
        return data
    snap = azure_cost.snapshot()
    if snap is None:
        return data
    data = RulesFile(seeded=True, rules=seed_rules([name for _, name in snap["subscriptions"]]))
    _try_write(rules, data)
    return data


def _try_write(rules: RuleStore, data: RulesFile) -> None:
    try:
        rules.write(data)
    except OSError as exc:
        _logger.warning("cost targets: rules not saved (%s)", type(exc).__name__)


# ── alert state ─────────────────────────────────────────────────────────────


class Sent(BaseModel):
    """One alert task, as filed."""

    rule_id: str
    family: Literal["spend", "fabric"]
    subject: str
    period: str
    task_id: str
    alarm_at: str | None = None
    sent_at: str
    list: str | None = None
    value: float | None = None


class AlertState(BaseModel):
    alerts: dict[str, Sent] = Field(default_factory=dict)
    """By :func:`key_of`."""
    active_since: dict[str, str] = Field(default_factory=dict)
    """Fabric capacity name to when the tick first saw this Active episode."""
    last_run: datetime | None = None
    last_error: str | None = None


class AlertStore:
    def __init__(self, path: Path | None = None) -> None:
        self._lock = threading.RLock()
        self._path = path
        self._memory = AlertState()

    def read(self) -> AlertState:
        with self._lock:
            if self._path is None:
                return self._memory.model_copy(deep=True)
            try:
                return AlertState.model_validate_json(self._path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return AlertState()

    def write(self, state: AlertState) -> None:
        """:raises OSError: When the file cannot be written."""
        with self._lock:
            if self._path is None:
                self._memory = state.model_copy(deep=True)
                return
            _atomic_write(self._path, state.model_dump_json(), ".ae-cost-alerts-")


_alert_store: AlertStore | None = None


def alert_store() -> AlertStore:
    global _alert_store
    with _store_lock:
        if _alert_store is None:
            _alert_store = AlertStore(_path_from(STATE_FILE_ENV, STATE_FILE_NAME))
        return _alert_store


def reset_stores(rules: RuleStore | None = None, alerts: AlertStore | None = None) -> None:
    """Drop the process-wide stores and the loop's memory (tests change the environment)."""
    global _rule_store, _alert_store, _last_fabric, _seen_at, _dirty
    with _store_lock:
        _rule_store = rules
        _alert_store = alerts
    _last_fabric = None
    _seen_at = None
    _dirty = False


def key_of(rule_id: str, subject: str, period: str) -> str:
    return f"{rule_id}|{quote(subject, safe='')}|{period}"


# ── settings ────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Config:
    on: bool
    off_reason: str | None
    list_name: str
    time: str
    tz: str


def config(environ: Mapping[str, str] | None = None) -> Config:
    """Whether alerts can be filed, and where and when they land."""
    env = os.environ if environ is None else environ
    settings = taskstore.Settings.from_env(env)
    clock = secret_alerts.config(dict(env))
    off_reason = None
    if not settings.configured:
        off_reason = "No task store is configured (AE_TASK_STORE_URL and the Access token)."
    return Config(
        on=off_reason is None,
        off_reason=off_reason,
        list_name=(env.get(LIST_ENV) or "").strip() or DEFAULT_LIST,
        time=clock.time,
        tz=settings.tz,
    )


# ── what the reading holds ──────────────────────────────────────────────────


@dataclass
class Sub:
    name: str
    daily: dict[str, float]
    services: dict[str, dict[str, float]]
    resources: dict[str, dict[str, float]]
    """By resource name (what the card shows), then day."""


@dataclass
class Spend:
    """The Azure reading as the rules need it."""

    today: date
    unavailable: str | None = None
    subs: dict[str, Sub] = field(default_factory=dict)
    problems: dict[str, str] = field(default_factory=dict)
    skipped: dict[str, str] = field(default_factory=dict)
    names: list[str] = field(default_factory=list)
    """Every configured subscription name, readable now or not."""
    note: str | None = None
    """A rate limit is on: shown by the card, never a reason to skip a target."""

    @property
    def month(self) -> str:
        return str(self.today)[:7]


def spend_from(snapshot: Mapping[str, Any] | None, now: datetime) -> Spend:
    """The spend context from :func:`.azure_cost.snapshot`; ``unavailable`` says why not."""
    today = now.astimezone(UTC).date()
    spend = Spend(today=today)
    if snapshot is not None:
        spend.names = [name for _sid, name in snapshot.get("subscriptions") or []]
    if snapshot is None:
        spend.unavailable = "Azure spend is not configured on this server."
        return spend
    at = snapshot.get("at")
    if not isinstance(at, datetime):
        spend.unavailable = (
            "Azure has not been read yet; targets are checked after the first reading."
        )
        return spend
    good_at = snapshot.get("goodAt")
    if snapshot.get("limited"):
        # A rate limit is information, not a verdict: each subscription is judged by its own
        # read time below, so numbers read minutes ago still count.
        good = (snapshot.get("rateLimit") or {}).get("goodAt") or good_at
        if isinstance(good, datetime):
            good = f"{good.astimezone(UTC):%Y-%m-%d %H:%M} UTC"
        spend.note = "Azure is rate limiting" + (f"; figures as of {good}" if good else "")
    readings = snapshot.get("readings") or {}
    errors = snapshot.get("errors") or {}
    # A reading with no readAt is of unknown age: treat it as too old, never as the live time.
    fallback = datetime.min.replace(tzinfo=UTC)
    newest: datetime | None = None
    fresh = 0
    for sid, name in snapshot.get("subscriptions") or []:
        reading = readings.get(sid)
        error = errors.get(sid)
        if reading is None:
            spend.problems[name] = f"{name} has not been read yet" + (
                f" ({error})" if error else ""
            )
            continue
        stamp = _parse(reading.get("readAt"))
        read_at = stamp or fallback
        if stamp is not None:
            newest = stamp if newest is None or stamp > newest else newest
        if now - read_at > READING_MAX_AGE:
            since = f"since {stamp.astimezone(UTC):%Y-%m-%d %H:%M} UTC" if stamp else "recently"
            spend.problems[name] = f"{name} has not been read {since}"
        elif error:
            spend.problems[name] = f"{name} could not be read ({error})"
        elif reading.get("currency") not in (None, "USD"):
            spend.skipped[name] = (
                f"{name} is billed in {reading['currency']} and limits are USD, so it is skipped"
            )
        else:
            fresh += 1
            resources: dict[str, dict[str, float]] = {}
            for rid, days in (reading.get("resources") or {}).items():
                merged = resources.setdefault(azure_cost._resource_name(rid), {})
                for day, cost in days.items():
                    merged[day] = merged.get(day, 0.0) + cost
            spend.subs[name] = Sub(
                name=name,
                daily=dict(reading.get("daily") or {}),
                services=reading.get("services") or {},
                resources=resources,
            )
    if newest is not None and not fresh and not spend.skipped and now - newest > READING_MAX_AGE:
        spend.unavailable = (
            f"The last Azure reading is from {newest.astimezone(UTC):%Y-%m-%d %H:%M} UTC, too "
            "old to judge a target. Open the Azure card to see why it stopped."
        )
    return spend


def known_from(spend: Spend, capacities: list[str]) -> Known:
    services = sorted(
        {s for sub in spend.subs.values() for s in sub.services.get(spend.month, {})}
    )
    resources = sorted({r for sub in spend.subs.values() for r in sub.resources})
    return Known(
        subscriptions=tuple(
            dict.fromkeys([*spend.names, *spend.subs, *spend.problems, *spend.skipped])
        ),
        services=tuple(services),
        resources=tuple(resources),
        capacities=tuple(capacities),
    )


# ── evaluation ──────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Hit:
    """One subject over its limit."""

    subject: str
    """What the title names: "MVP 2025", "Azure", a resource or a capacity."""
    value: float
    period: str
    data_date: str | None = None
    since: str | None = None
    hourly: float | None = None


@dataclass
class Evaluation:
    status: Literal["ok", "over", "cannot-evaluate"]
    reason: str | None = None
    value: float | None = None
    data_date: str | None = None
    period: str | None = None
    """The rule's current period (the month, or the newest data day); a spend alert whose
    period differs has rolled."""
    hits: list[Hit] = field(default_factory=list)


def _cannot(reason: str) -> Evaluation:
    return Evaluation(status="cannot-evaluate", reason=reason)


def _sum_days(subs: list[Sub], pick: Any = None) -> dict[str, float]:
    out: dict[str, float] = {}
    for sub in subs:
        for day, amount in (sub.daily if pick is None else pick(sub)).items():
            out[day] = out.get(day, 0.0) + amount
    return out


def _month_sum(daily: Mapping[str, float], month: str) -> float:
    return sum(a for d, a in daily.items() if d.startswith(month))


def _latest(daily: Mapping[str, float], today: date) -> tuple[str, float] | None:
    days = [d for d, a in daily.items() if d < str(today) and a > 0]
    if not days:
        return None
    newest = max(days)
    return newest, daily[newest]


def _forecast(daily: Mapping[str, float], today: date) -> tuple[float, str] | None:
    month = str(today)[:7]
    with_data = sorted(d for d, a in daily.items() if d.startswith(month) and a > 0)
    if len(with_data) < FORECAST_MIN_DAYS:
        return None
    last = with_data[-1]
    in_month = (today.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
    return _month_sum(daily, month) / int(last[-2:]) * in_month.day, last


def _scope_subs(rule: Rule, spend: Spend) -> tuple[list[Sub], list[str]]:
    """The subscriptions a rule looks at, and the reasons some it needs are unreadable."""
    if rule.scope.kind == "subscription":
        name = rule.scope.name or ""
        sub = spend.subs.get(name)
        if sub is not None:
            return [sub], []
        reason = (
            spend.problems.get(name)
            or spend.skipped.get(name)
            or f'"{name}" is not one of the subscriptions this server reads '
            "(AE_AZURE_COST_SUBSCRIPTIONS). Edit the target to pick another."
        )
        return [], [reason]
    return list(spend.subs.values()), list(spend.problems.values())


def evaluate_spend(rule: Rule, spend: Spend) -> Evaluation:
    """A spend rule against the reading. Pure; asks Azure nothing."""
    if spend.unavailable:
        return _cannot(spend.unavailable)
    subs, blocking = _scope_subs(rule, spend)
    kind, name, measure = rule.scope.kind, rule.scope.name, rule.measure
    if kind == "subscription" and not subs:
        return _cannot(blocking[0])
    if not subs and blocking:
        return _cannot("; ".join(blocking))
    month = spend.month
    # (subject, daily amounts) per thing judged.
    subjects: list[tuple[str, dict[str, float]]]
    service_total: float | None = None
    if kind == "all":
        subjects = [("Azure", _sum_days(subs))]
    elif kind == "subscription":
        subjects = [(name or "", _sum_days(subs))]
    elif kind == "service":
        service_total = sum(s.services.get(month, {}).get(name or "", 0.0) for s in subs)
        subjects = [(f"{name} (Azure service)", {})]
    else:
        # Never merged across subscriptions: two resources that share a name are two things.
        pairs = [
            (r, s) for s in subs for r in ([name] if name else s.resources) if r in s.resources
        ]
        pairs.sort(key=lambda pair: (pair[0], pair[1].name))
        counts: dict[str, int] = {}
        for r, _s in pairs:
            counts[r] = counts.get(r, 0) + 1
        subjects = [(r if counts[r] == 1 else f"{r} ({s.name})", s.resources[r]) for r, s in pairs]
    hits: list[Hit] = []
    values: list[float] = []
    dates: list[str] = []
    period: str | None = None
    for subject, daily in subjects:
        if measure == "month_to_date":
            value = service_total if kind == "service" else _month_sum(daily, month)
            through = _latest_any(daily)
            found: tuple[float, str, str | None] | None = (value, month, through)
        elif measure == "month_end_forecast":
            projected = _forecast(daily, spend.today)
            if projected is None:
                return _cannot(
                    f"Too early in the month for a forecast; it needs {FORECAST_MIN_DAYS} "
                    "days of data."
                )
            found = (projected[0], month, projected[1])
        else:
            latest = _latest(daily, spend.today)
            if latest is None or latest[0] < str(spend.today - timedelta(days=FRESH_DAYS)):
                continue
            found = (latest[1], latest[0], latest[0])
            period = max(period or "", latest[0])
        if found is None:
            continue
        value, per, data_date = found
        if measure != "latest_finished_day":
            period = per
        values.append(value)
        if data_date:
            dates.append(data_date)
        if value > rule.limit:
            hits.append(
                Hit(subject=subject, value=round(value, 2), period=per, data_date=data_date)
            )
    shown = round(max(values), 2) if values else None
    data_date = max(dates) if dates else None
    if hits:
        return Evaluation("over", None, shown, data_date, period, hits)
    if blocking:
        return Evaluation(
            "cannot-evaluate",
            "Not all subscriptions could be read: " + "; ".join(blocking),
            shown,
            data_date,
            period,
        )
    return Evaluation("ok", None, shown, data_date, period)


def _latest_any(daily: Mapping[str, float]) -> str | None:
    days = [d for d, a in daily.items() if a > 0]
    return max(days) if days else None


def _parse(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _iso(when: datetime) -> str:
    return when.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def track_active(state: AlertState, rows: list[dict[str, Any]], now: datetime) -> list[str]:
    """Record when each Active capacity was first seen Active; forget the ones that stopped.
    Returns the capacities that began an episode in this call."""
    began: list[str] = []
    seen = {row.get("name") for row in rows}
    for gone in [name for name in state.active_since if name not in seen]:
        del state.active_since[gone]  # left the fleet; a later Active episode starts afresh
    for row in rows:
        name = row.get("name")
        if not isinstance(name, str) or row.get("error"):
            continue
        if row.get("state") == "Active":
            resumed = _parse(row.get("resumedAt"))
            if name in state.active_since:
                # Paused and resumed between two ticks: the resume begins a new episode.
                held = _parse(state.active_since[name])
                if held is not None and resumed is not None and held < resumed <= now:
                    state.active_since[name] = _iso(resumed)
                continue
            start = now
            if resumed is not None and now - RESUMED_MAX_AGE <= resumed <= now:
                start = resumed
            state.active_since[name] = _iso(start)
            began.append(name)
        else:
            state.active_since.pop(name, None)
    return began


def evaluate_fabric(
    rule: Rule,
    rows: list[dict[str, Any]] | None,
    note: str | None,
    since: Mapping[str, str],
    now: datetime,
) -> Evaluation:
    """A Fabric rule against the last fleet reading and the recorded Active starts."""
    if rows is None:
        return _cannot(note or "Fabric has not been read yet; the hourly check will read it.")
    by_name = {str(r.get("name")): r for r in rows}
    wanted = [rule.scope.name] if rule.scope.name else sorted(by_name)
    hits: list[Hit] = []
    hours: list[float] = []
    problems: list[str] = []
    for name in wanted:
        row = by_name.get(name or "")
        if row is None:
            if rule.scope.name:
                return _cannot(
                    f'The Fabric capacity "{name}" is not in the fleet list'
                    + (f" ({note})" if note else "")
                    + ". Edit the target to pick another."
                )
            continue
        if row.get("error"):
            problems.append(f"{name} could not be read ({row['error']})")
            continue
        start = _parse(since.get(name or ""))
        if row.get("state") != "Active" or start is None:
            hours.append(0.0)
            continue
        elapsed = max((now - start).total_seconds() / 3600, 0.0)
        hours.append(elapsed)
        if elapsed > rule.limit:
            hourly = row.get("hourlyUsd")
            hits.append(
                Hit(
                    subject=str(name),
                    value=round(elapsed, 1),
                    period=_iso(start),
                    since=_iso(start),
                    hourly=hourly if isinstance(hourly, (int, float)) else None,
                )
            )
    shown = round(max(hours), 1) if hours else None
    if hits:
        return Evaluation("over", None, shown, None, None, hits)
    if problems or note:
        return Evaluation("cannot-evaluate", "; ".join(problems) or note, shown)
    return Evaluation("ok", None, shown)


# ── what the task says ──────────────────────────────────────────────────────


def _money(amount: float) -> str:
    text = f"{amount:,.2f}"
    return "$" + (text[:-3] if text.endswith(".00") else text)


def title_of(rule: Rule, hit: Hit) -> str:
    limit = _money(rule.limit)
    if rule.measure == "month_to_date":
        return f"{hit.subject} is at {_money(hit.value)} this month (limit {limit})"
    if rule.measure == "month_end_forecast":
        return f"{hit.subject} is on pace for {_money(hit.value)} this month (limit {limit})"
    if rule.measure == "latest_finished_day":
        return f"{hit.subject} cost {_money(hit.value)} on {hit.period} (limit {limit} a day)"
    return (
        f"Fabric capacity {hit.subject} has been Active for {hit.value:g} hours "
        f"(limit {rule.limit:g})"
    )


def notes_of(rule: Rule, hit: Hit) -> str:
    lines = [f"Target: {rule.name}", title_of(rule, hit) + "."]
    if rule.measure == "fabric_active_hours":
        lines.append(f"Active since {hit.since}.")
        if hit.hourly:
            lines.append(f"A running capacity costs about {_money(hit.hourly)} an hour.")
        lines.append("Pause it from the Fabric card if nobody is using it.")
    else:
        if hit.data_date:
            lines.append(f"Data through {hit.data_date}.")
        lines.append(
            "Azure posts costs 8 to 24 hours late, so the newest day is not in this number."
        )
    lines += [
        "Edit or turn off this target: Admin Center, Azure card, Targets.",
        "",
        f"Alert key: cost {rule.id} {quote(hit.subject, safe='')} {hit.period}",
    ]
    return "\n".join(lines)


def _task_body(rule: Rule, hit: Hit, alarm: datetime) -> dict[str, Any]:
    at = _iso(alarm)
    return {
        "notes": notes_of(rule, hit),
        "due_at": at,
        "due_is_all_day": 0,
        # The alarm is what the app pushes to the phone; a due time alone never rings.
        "reminder_at": at,
        "floating": 1,
        "important": 1,
        "urgent": 1 if rule.measure == "fabric_active_hours" else 0,
    }


# ── the pass ────────────────────────────────────────────────────────────────


@dataclass
class Outcome:
    created: list[str] = field(default_factory=list)
    closed: list[str] = field(default_factory=list)
    forgotten: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


_run_lock = asyncio.Lock()


class _Lookups:
    """The app's lists, tags and open tasks, each read at most once per pass."""

    def __init__(self, api: taskstore.TaskStoreClient, cfg: Config) -> None:
        self.api = api
        self.cfg = cfg
        self._bucket: tuple[str | None, str | None] | None = None
        self._tags: list[str] | None = None
        self._open: list[dict[str, Any]] | None = None
        self._done: list[dict[str, Any]] | None = None

    async def bucket(self, out: Outcome) -> tuple[str | None, str | None]:
        if self._bucket is None:
            self._bucket = (None, None)
            listed = await self.api.list_buckets()
            rows = (listed.data or {}).get("buckets") if listed.ok else None
            want = self.cfg.list_name.lower()
            hits = [
                row
                for row in rows or []
                if isinstance(row, dict)
                and not row.get("hidden")
                and str(row.get("name") or "").strip().lower() == want
            ]
            if len(hits) == 1:
                self._bucket = (hits[0].get("id"), hits[0].get("name"))
            elif listed.ok:
                out.errors.append(
                    f'no single visible list is called "{self.cfg.list_name}"; filed with no '
                    f"list. Fix {LIST_ENV}."
                )
            else:
                out.errors.append(f"could not read the lists ({listed.error}); filed with no list")
        return self._bucket

    async def tags(self) -> list[str]:
        if self._tags is None:
            listed = await self.api.list_tags()
            names = (listed.data or {}).get("tags") if listed.ok else None
            known = {(row.get("name") if isinstance(row, dict) else row) for row in names or []}
            self._tags = [TAG] if TAG in known else []
        return self._tags

    async def _ours(self, status: str) -> list[dict[str, Any]] | None:
        listed = await self.api.list_tasks(status)
        if not listed.ok or not isinstance(listed.data, dict):
            return None
        return [
            task
            for task in listed.data.get("tasks") or []
            if isinstance(task, dict) and task.get("origin_surface") == taskstore.ORIGIN_SURFACE
        ]

    async def open_tasks(self) -> list[dict[str, Any]] | None:
        if self._open is None:
            self._open = await self._ours("open")
        return self._open

    async def done_tasks(self) -> list[dict[str, Any]] | None:
        if self._done is None:
            self._done = await self._ours("done")
        return self._done


_CLOSED_BY_US = re.compile(r"^Closed \d{4}-\d{2}-\d{2}: ", re.MULTILINE)


async def _read(api: taskstore.TaskStoreClient, task_id: str) -> tuple[str | None, str]:
    """(status, notes) of the task; ``"gone"`` when the app has none, ``None`` when unreadable."""
    read = await api.get_task(task_id)
    if read.ok and isinstance(read.data, dict):
        return str(read.data.get("status") or "open"), str(read.data.get("notes") or "")
    if read.status == 404:
        return "gone", ""
    return None, ""


async def _close(
    api: taskstore.TaskStoreClient, sent: Sent, key: str, reason: str, now: datetime, out: Outcome
) -> bool:
    """Close the alert's task if it is still open; ``False`` when it must be tried again."""
    status, notes = await _read(api, sent.task_id)
    if status is None:
        out.errors.append(f"could not read the task for {key}; closing it next time")
        return False
    if status == "open":
        closed = await api.patch_task(
            sent.task_id,
            {
                "status": "done",
                "notes": f"{notes}\n\nClosed {now.date().isoformat()}: {reason}".strip(),
            },
        )
        if not closed.ok:
            out.errors.append(f"could not close the task for {key} ({closed.error})")
            return False
        out.closed.append(key)
    return True


async def run(
    family: Literal["spend", "fabric"],
    *,
    rules: list[Rule],
    spend: Spend | None = None,
    rows: list[dict[str, Any]] | None = None,
    note: str | None = None,
    now: datetime | None = None,
    cfg: Config | None = None,
    client: taskstore.TaskStoreClient | None = None,
    alerts: AlertStore | None = None,
) -> Outcome:
    """File and close this family's alert tasks. Never raises; failures are logged and kept in
    the state's ``last_error``, and the next pass tries again."""
    cfg = cfg or config()
    out = Outcome()
    if not cfg.on:
        return out
    at = now or datetime.now(UTC)
    alerts = alerts or alert_store()
    async with _run_lock:
        own_client = client is None
        api = client or taskstore.TaskStoreClient(taskstore.Settings.from_env())
        try:
            await _pass(family, rules, spend, rows, note, at, cfg, api, alerts, out)
        except Exception as exc:  # noqa: BLE001  a background pass must not die silently
            _logger.warning("cost targets: pass failed (%s)", type(exc).__name__)
            out.errors.append(f"the pass failed ({type(exc).__name__})")
        finally:
            if own_client:
                await api.aclose()
        state = alerts.read()
        state.last_run = at
        state.last_error = "; ".join(out.errors) or None
        try:
            alerts.write(state)
        except OSError as exc:
            _logger.warning("cost targets: state not saved (%s)", type(exc).__name__)
    if out.created or out.closed or out.errors:
        _logger.info(
            "cost targets: created %d, closed %d, errors %d",
            len(out.created),
            len(out.closed),
            len(out.errors),
        )
    return out


async def _pass(
    family: str,
    rules: list[Rule],
    spend: Spend | None,
    rows: list[dict[str, Any]] | None,
    note: str | None,
    now: datetime,
    cfg: Config,
    api: taskstore.TaskStoreClient,
    alerts: AlertStore,
    out: Outcome,
) -> None:
    state = alerts.read()
    lookups = _Lookups(api, cfg)
    if family == "fabric" and rows is not None:
        began = track_active(state, rows, now)
        if began and any(r.enabled and r.measure == "fabric_active_hours" for r in rules):
            await _restore_starts(lookups, state, began, {r.id for r in rules})
        alerts.write(state)
    by_id = {rule.id: rule for rule in rules}
    evals: dict[str, Evaluation] = {}
    wanted: dict[str, tuple[Rule, Hit]] = {}
    for rule in rules:
        if not rule.enabled or family_of(rule.measure) != family:
            continue
        if family == "spend":
            ev = evaluate_spend(rule, spend or Spend(today=now.date(), unavailable="No reading."))
        else:
            ev = evaluate_fabric(rule, rows, note, state.active_since, now)
        evals[rule.id] = ev
        for hit in ev.hits:
            wanted[key_of(rule.id, hit.subject, hit.period)] = (rule, hit)

    def save() -> None:
        alerts.write(state)

    # 1. Close what no longer stands.
    for key, sent in list(state.alerts.items()):
        if sent.family != family:
            continue
        rule = by_id.get(sent.rule_id)
        reason: str | None = None
        forget_only = False
        if rule is None:
            reason = "the target was deleted."
        elif not rule.enabled:
            reason = "the target was turned off."
        elif family_of(rule.measure) != family:
            reason = "the target was edited to measure something else."
        else:
            ev = evals[rule.id]
            if ev.status == "cannot-evaluate" or key in wanted:
                continue
            if family == "spend" and ev.period is not None and ev.period != sent.period:
                forget_only = True
            else:
                reason = (
                    f"the value is under the limit of {_money_or_hours(rule)} now, "
                    "or the target was edited."
                )
        if (
            not forget_only
            and reason is not None
            and not await _close(api, sent, key, reason, now, out)
        ):
            continue
        if forget_only:
            out.forgotten.append(key)
        del state.alerts[key]
        save()

    # 2. File what is over and not yet filed.
    alarm = secret_alerts.alarm_instant(now, cfg.tz, cfg.time)
    for key, (rule, hit) in sorted(wanted.items()):
        if key in state.alerts:
            continue
        adopted, looked = await _adopt(lookups, rule, hit, family)
        if not looked:
            out.errors.append(f"could not read the open tasks; {key} waits for the next pass")
            continue
        if adopted is not None:
            state.alerts[adopted[0]] = adopted[1]
            save()
            continue
        when = now + FABRIC_ALARM_DELAY if family == "fabric" else alarm
        body = _task_body(rule, hit, when)
        made = await _create(lookups, rule, hit, body, out)
        if made is None:
            continue
        task_id, list_name = made
        state.alerts[key] = Sent(
            rule_id=rule.id,
            family=family,  # type: ignore[arg-type]
            subject=hit.subject,
            period=hit.period,
            task_id=task_id,
            alarm_at=body["reminder_at"],
            sent_at=_iso(now),
            list=list_name,
            value=hit.value,
        )
        save()
        out.created.append(key)


async def _restore_starts(
    lookups: _Lookups, state: AlertState, began: list[str], rule_ids: set[str]
) -> None:
    """A capacity first seen Active now may be mid-episode with a lost state file: an open task
    of ours for it carries the real start in its "Alert key" line, so keep that start."""
    tasks = await lookups.open_tasks()
    for task in tasks or []:
        for match in _MARKER_LINE.finditer(str(task.get("notes") or "")):
            subject = unquote(match["subject"])
            start = _parse(match["period"])
            if match["rule"] in rule_ids and subject in began and start is not None:
                held = _parse(state.active_since.get(subject))
                if held is None or start < held:
                    state.active_since[subject] = _iso(start)


def _money_or_hours(rule: Rule) -> str:
    return f"{rule.limit:g} hours" if rule.measure == "fabric_active_hours" else _money(rule.limit)


async def _adopt(
    lookups: _Lookups, rule: Rule, hit: Hit, family: str
) -> tuple[tuple[str, Sent] | None, bool]:
    """An open task of ours for this alert, found by its "Alert key" line (a Fabric one by rule
    and capacity alone, so a lost start time cannot file a second task), and whether the open
    tasks could be read at all."""
    tasks = await lookups.open_tasks()
    if tasks is None:
        return None, False
    # A spend alert is per period, so a task already ticked done for this rule, subject and
    # period still counts as filed; a lost state file must not file a second one.
    if family == "spend":
        finished = await lookups.done_tasks()
        if finished is None:
            return None, False
        # One this module closed (it appends a "Closed <date>:" line) ended because the value
        # fell back; going over again in the period files afresh, so it is not adopted.
        tasks = [*tasks, *(t for t in finished if not _CLOSED_BY_US.search(str(t.get("notes"))))]
    for task in tasks:
        for match in _MARKER_LINE.finditer(str(task.get("notes") or "")):
            subject = unquote(match["subject"])
            if match["rule"] != rule.id or subject != hit.subject:
                continue
            if family == "spend" and match["period"] != hit.period:
                continue
            if not isinstance(task.get("id"), str):
                continue
            key = key_of(rule.id, subject, match["period"])
            return (
                key,
                Sent(
                    rule_id=rule.id,
                    family=family,  # type: ignore[arg-type]
                    subject=subject,
                    period=match["period"],
                    task_id=task["id"],
                    alarm_at=_normal(task.get("reminder_at")),
                    sent_at=str(task.get("created_at") or ""),
                ),
            ), True
    return None, True


def _normal(value: object) -> str | None:
    parsed = _parse(value)
    return _iso(parsed) if parsed else None


async def _create(
    lookups: _Lookups, rule: Rule, hit: Hit, body: dict[str, Any], out: Outcome
) -> tuple[str, str | None] | None:
    bucket_id, list_name = await lookups.bucket(out)
    title = title_of(rule, hit)
    full = {
        **body,
        "title": title,
        "kind": "task",
        "status": "open",
        "tags": await lookups.tags(),
        "origin_surface": taskstore.ORIGIN_SURFACE,
        "bucket_id": bucket_id,
    }
    created = await lookups.api.create_task(full)
    if not created.ok and created.kind in ("transport", "server"):
        # It may have landed (a timeout, or an edge 5xx after the commit): re-read, never resend.
        listed = await lookups.api.list_tasks("open")
        found = (
            (listed.data or {}).get("tasks") if listed.ok and isinstance(listed.data, dict) else []
        )
        match = next(
            (
                task
                for task in found or []
                if isinstance(task, dict)
                and task.get("title") == title
                and task.get("origin_surface") == taskstore.ORIGIN_SURFACE
                and task.get("notes") == body["notes"]
            ),
            None,
        )
        if match is not None:
            created = taskstore.Result(ok=True, status=201, data=match)
    if not created.ok:
        out.errors.append(f"could not file the task for {rule.id} ({created.error})")
        return None
    task = created.data if isinstance(created.data, dict) else {}
    task_id = task.get("id")
    if not isinstance(task_id, str) or not task_id:
        out.errors.append(f"the app answered {created.status} without a task id for {rule.id}")
        return None
    return task_id, list_name


# ── what the card shows ─────────────────────────────────────────────────────


#: The Fabric tick's last reading: (when, rows or None, note). Memory only; GET never calls Fabric.
_last_fabric: tuple[datetime, list[dict[str, Any]] | None, str | None] | None = None
_seen_at: datetime | None = None
#: Set by an edit so the loop re-judges at its next look instead of waiting for a new reading.
_dirty = False


def put_rule(rule_id: str, body: RuleBody) -> Rule:
    """Create or replace a rule. :raises RuleError: Input to change. :raises RulesFileError: An
    unreadable file. :raises OSError: A file that cannot be written."""
    global _seen_at, _dirty
    store = rule_store()
    with store._lock:  # one read-modify-write at a time
        data = load_rules(store)
        rule = check(rule_id, body, known_now(), data.rules)
        data.rules = [r for r in data.rules if r.id != rule_id] + [rule]
        data.seeded = True
        store.write(data)
    _seen_at, _dirty = None, True
    return rule


def delete_rule(rule_id: str) -> bool:
    """Delete a rule; ``False`` when there is none. Same errors as :func:`put_rule`."""
    global _seen_at, _dirty
    store = rule_store()
    with store._lock:  # one read-modify-write at a time
        data = load_rules(store)
        if all(r.id != rule_id for r in data.rules):
            return False
        data.rules = [r for r in data.rules if r.id != rule_id]
        data.seeded = True
        store.write(data)
    _seen_at, _dirty = None, True
    return True


def view(
    data: RulesFile,
    state: AlertState,
    spend: Spend,
    cfg: Config,
    now: datetime,
    fabric_view: tuple[datetime, list[dict[str, Any]] | None, str | None] | None,
) -> dict[str, Any]:
    """``GET /azure-cost/targets``: each rule with its status, and the names a scope can take."""
    rows = fabric_view[1] if fabric_view else None
    note = fabric_view[2] if fabric_view else None
    out: list[dict[str, Any]] = []
    for rule in data.rules:
        out.append(rule_row(rule, state, spend, rows, note, now))
    capacities = sorted({str(r.get("name")) for r in rows or [] if r.get("name")})
    known = known_from(spend, capacities)
    answer = {
        "max": MAX_RULES,
        "rules": out,
        "scopes": {
            "subscriptions": list(known.subscriptions),
            "services": list(known.services[:SERVICE_LIST_MAX]),
            "resources": list(known.resources[:LIST_SCOPE_MAX]),
            "capacities": list(known.capacities),
        },
        "alerts": {
            "on": cfg.on,
            "offReason": cfg.off_reason,
            "list": cfg.list_name,
            "time": cfg.time,
            "tz": cfg.tz,
            "open": len(state.alerts),
            "lastRun": state.last_run.isoformat() if state.last_run else None,
            "lastError": state.last_error,
        },
        "note": spend.note,
    }
    return _fit(answer)


def _size(answer: Mapping[str, Any]) -> int:
    return len(json.dumps(answer, separators=(",", ":")))


def _fit(answer: dict[str, Any]) -> dict[str, Any]:
    """Hold the answer under :data:`ANSWER_MAX` as serialized: shed the lowest-value text first
    (the pick-lists, then the over-limit names, then the reasons) and say so in ``truncated``."""
    if _size(answer) <= ANSWER_MAX:
        return answer
    answer["truncated"] = True
    scopes = answer["scopes"]
    for key in ("resources", "services"):
        while scopes[key] and _size(answer) > ANSWER_MAX:
            scopes[key] = scopes[key][: len(scopes[key]) // 2]
    for field_name in ("over", "reason"):
        for row in answer["rules"]:
            if _size(answer) <= ANSWER_MAX:
                return answer
            row.pop(field_name, None)
            if field_name == "reason":
                row["reason"] = None
    return answer


def rule_row(
    rule: Rule,
    state: AlertState,
    spend: Spend,
    rows: list[dict[str, Any]] | None,
    note: str | None,
    now: datetime,
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "id": rule.id,
        "name": rule.name,
        "enabled": rule.enabled,
        "scope": {"kind": rule.scope.kind, "name": rule.scope.name},
        "measure": rule.measure,
        "limit": rule.limit,
        "status": "off",
        "reason": None,
        "value": None,
        "dataDate": None,
        "alert": None,
    }
    if not rule.enabled:
        return row
    if family_of(rule.measure) == "spend":
        ev = evaluate_spend(rule, spend)
    else:
        ev = evaluate_fabric(rule, rows, note, state.active_since, now)
    open_alerts = [s for s in state.alerts.values() if s.rule_id == rule.id]
    row["status"] = "alerted" if ev.status == "over" and open_alerts else ev.status
    # The extension bridge refuses an answer over 32,768 characters (the Key Vault card
    # learned that on 2026-10-24), so a row's free text is capped.
    row["reason"] = ev.reason[:REASON_MAX] if ev.reason else None
    row["value"] = ev.value
    row["dataDate"] = ev.data_date
    if ev.status == "over":
        row["over"] = [h.subject for h in ev.hits[:OVER_SHOWN]]
    if open_alerts:
        newest = max(open_alerts, key=lambda s: s.sent_at)
        row["alert"] = {
            "sentAt": newest.sent_at,
            "alarmAt": newest.alarm_at,
            "list": newest.list,
        }
    return row


def current_spend(now: datetime | None = None) -> Spend:
    return spend_from(azure_cost.snapshot(), now or datetime.now(UTC))


def view_now(data: RulesFile, now: datetime | None = None) -> dict[str, Any]:
    """:func:`view` with the live stores and the last readings."""
    at = now or datetime.now(UTC)
    return view(data, alert_store().read(), current_spend(at), config(), at, _last_fabric)


def row_now(rule: Rule, now: datetime | None = None) -> dict[str, Any]:
    """:func:`rule_row` with the live stores and the last readings."""
    at = now or datetime.now(UTC)
    rows, note = (_last_fabric[1], _last_fabric[2]) if _last_fabric else (None, None)
    return rule_row(rule, alert_store().read(), current_spend(at), rows, note, at)


def fabric_memory() -> tuple[datetime, list[dict[str, Any]] | None, str | None] | None:
    return _last_fabric


def known_now(now: datetime | None = None) -> Known:
    capacities = sorted(
        {str(r.get("name")) for r in (_last_fabric[1] if _last_fabric else None) or []}
    )
    return known_from(current_spend(now), capacities)


# ── the loop ────────────────────────────────────────────────────────────────


async def fabric_reading() -> tuple[list[dict[str, Any]] | None, str | None]:
    """The fleet rows and a note, from fabric.py's read-only overview; the rows are ``None``
    when Fabric is not configured. Its answer is served at any age and refreshed behind, so
    this waits for that refresh once."""
    env = os.environ
    if fabric.fleet(env) is None and not fabric.capacities(env):
        return None, "Fabric is not configured on this server."
    answer = await fabric.overview(env)
    task = fabric.refresh_task()
    if task is not None and not task.done():
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(asyncio.shield(task), FABRIC_REFRESH_WAIT_S)
        answer = await fabric.overview(env)
    rows = [r for r in answer.get("capacities") or [] if isinstance(r, dict)]
    return rows, answer.get("fleetError") or None


async def tick(*, fabric_due: bool, now: datetime | None = None) -> None:
    """One look: a spend pass when Azure has a new reading, a Fabric pass when due."""
    global _last_fabric, _seen_at, _dirty
    at = now or datetime.now(UTC)
    cfg = config()
    if not cfg.on:
        return
    data = load_rules()
    snap = azure_cost.snapshot()
    if snap is not None and snap["at"] is not None and snap["at"] != _seen_at:
        _seen_at = snap["at"]
        await run("spend", rules=data.rules, spend=spend_from(snap, at), now=at, cfg=cfg)
    if fabric_due:
        _dirty = False
        rows, note = await fabric_reading()
        _last_fabric = (at, rows, note)
        if rows is not None or any(r.measure == "fabric_active_hours" for r in data.rules):
            await run("fabric", rules=data.rules, rows=rows, note=note, now=at, cfg=cfg)


async def _loop() -> None:
    await asyncio.sleep(STARTUP_DELAY_S)
    next_fabric = 0.0
    while True:
        due = _dirty or time.monotonic() >= next_fabric
        try:
            await tick(fabric_due=due)
            if due:
                next_fabric = time.monotonic() + FABRIC_EVERY_S
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001  the loop must outlive any one bad pass
            _logger.warning("cost targets: tick failed (%s)", type(exc).__name__)
            if due:
                next_fabric = time.monotonic() + FABRIC_EVERY_S
        await asyncio.sleep(POLL_S)


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """Seed the rules at boot and run the checking loop."""
    task: asyncio.Task[None] | None = None
    try:
        with contextlib.suppress(RulesFileError):
            load_rules()
        task = asyncio.get_running_loop().create_task(_loop())
    except Exception as exc:  # noqa: BLE001  targets must never stop the server starting
        _logger.warning("cost targets: loop not started (%s)", type(exc).__name__)
    try:
        yield
    finally:
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task


#: No routes; included for its lifespan, like azure_cost's (see the module docstring).
lifespan_router = APIRouter(lifespan=lifespan)
