"""Plan limits: the subscription windows Claude Code and Codex report, one shape for both.

Reid asked on 2026-09-24 for what Claude Code's ``/usage`` shows (the 5-hour
window, the weekly window, reset times, percent used) and the Codex
equivalent, one click away from the context ring in a session's composer,
instead of opening the ``>_`` terminal and typing ``/usage``. This module is
the shape that data travels in, the two translations into it, and the
server's store of the latest snapshot per harness. Where the numbers come
from (both are documented machine interfaces of the CLIs, never a TUI
scrape):

Claude Code
    The ``statusLine`` command's stdin carries ``rate_limits``:
    ``five_hour`` and ``seven_day``, each ``{used_percentage, resets_at}``
    with ``resets_at`` in epoch seconds, present for subscribers after the
    first API response (Claude Code's statusline documentation; checked
    against the 2.1.281 CLI on 2026-09-24). The claude-native harness
    already overrides the statusLine and chains the user's own
    ``~/.claude/settings.json`` command with the same stdin
    (``omnigent/harnesses/claude_native/bridge.py``, the ``statusLine``
    block and ``read_user_status_line_command``), so a statusLine command
    on the box host sees every session's payload with no core patch:
    ``deploy/ae-limits-statusline.sh`` saves it and
    :mod:`ae_omni_policies.limits_relay` reads it. The harness's own
    per-session capture of the same payload (``context_raw.json`` in each
    bridge directory) is read too, which also carries each session's
    ``context_window`` (2026-09-24, second pass: a session started before
    the statusLine was installed never chains it, so the capture is the
    reading that does not depend on when a session began).

    The per-model weekly windows, the usage credits and the plan name
    ``/usage`` also shows come from the claude.ai usage endpoint, which the
    CLI only calls for a login holding the ``user:profile`` scope. The
    relay asks the CLI for them through the stream-json ``get_usage``
    control request, run without ``CLAUDE_CODE_OAUTH_TOKEN`` so a stored
    ``claude auth login`` is used; the box's sessions keep the
    inference-only ``claude setup-token`` token, which the CLI prefers over
    a stored login whenever the variable is set. Without such a login those
    rows are absent and :attr:`LimitSnapshot.hint` says how to add them
    (``docs/PLAN-LIMITS.md``).

    A session on a backup account reads its own status-line windows
    (2026-10-17). ``get_usage`` only ever reads the stored default login, so
    the host snapshot described the default account even for a session
    running on a ``claude setup-token`` backup, and the panel showed the
    wrong limits. The harness's statusLine shim now writes a ``claude_account``
    sidecar next to ``context_raw.json`` on every render (empty for the
    default login); the relay records it with the session's windows in its
    :class:`SessionLimits`, and ``GET /v1/ae/limits?session_id=`` answers
    with that session's view (:func:`session_view`).

Codex
    ``codex app-server`` answers ``account/rateLimits/read`` with
    ``rateLimits`` (``primary`` and ``secondary`` windows, each
    ``{usedPercent, windowDurationMins, resetsAt}``, plus ``planType``)
    and, on newer builds, ``rateLimitsByLimitId`` with one such snapshot
    per metered bucket (``codex-rs/app-server-protocol``, ``v2/account.rs``,
    openai/codex at 58838df, 2026-09-24). The ChatGPT login in
    ``~/.codex`` is the identity; no model call is made.

The server keeps the newest snapshot per harness in memory and on disk as well
(``AE_LIMITS_STATE_FILE`` when set, else ``/data/ae-limits.json`` when ``/data``
exists), so a restart shows the last reading instead of nothing until the
relay's next tick.

Per account (2026-10-21, the Admin Center's Accounts card): the store also
keeps the newest reading per (harness, account), so an account with no live
session still shows its last numbers and their age. Reid runs several Claude
logins (the default, personal ones, shared ones) and one Codex login, and
until then usage was visible only per session. The Claude host reading fills
the default login (or the backup account it names), each ``sessions`` entry
fills its own account, and Codex fills its one login. The relay also posts the
host's signed-in Claude logins (``known_accounts``, labels only), so
``GET /v1/ae/limits/accounts`` lists an account before its first reading.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import tempfile
import threading
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

_logger = logging.getLogger(__name__)

Harness = Literal["claude", "codex"]
HARNESSES: tuple[Harness, ...] = ("claude", "codex")
STATE_FILE_ENV = "AE_LIMITS_STATE_FILE"
# The box bind-mounts /data; a server whose env omits STATE_FILE_ENV still keeps
# its readings there, so a deploy's restart does not blank the panel for a relay
# tick (2026-10-20, ae-v0.18.0.9).
DATA_DIR = Path("/data")
DATA_FILE_NAME = "ae-limits.json"
# Sessions whose context a Claude snapshot carries, newest first.
MAX_CONTEXTS = 40
# Accounts the store keeps a reading for, and a snapshot may list as signed in.
MAX_ACCOUNTS = 64
# The state file's key for the per-account readings (additive: an older file has none).
ACCOUNTS_KEY = "accounts"
# A shared account id's prefix (shared_accounts.PREFIX, ae_shared_claude_accounts.PREFIX).
SHARED_PREFIX = "shared:"
# What upstream's account picker calls the CLI's own login (claude_accounts.account_descriptors).
DEFAULT_LABEL = "Default CLI account"
CODEX_LABEL = "Codex"

# Claude Code's statusLine keys, in the order /usage lists them, with the label
# the panel shows and the window's length (the spend limit has no fixed one).
_CLAUDE_WINDOWS: tuple[tuple[str, str, int | None], ...] = (
    ("five_hour", "5-hour limit", 300),
    ("seven_day", "Weekly, all models", 7 * 24 * 60),
    ("spend_limit", "Spend limit", None),
)


class LimitWindow(BaseModel):
    """One metered window: how full it is and when it empties."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=120)
    label: str = Field(min_length=1, max_length=120)
    # Above 100 once a spend limit is exceeded (Claude Code's own contract).
    used_percent: float = Field(ge=0, le=1000)
    resets_at: datetime | None = None
    window_minutes: int | None = Field(default=None, ge=1)


class Credits(BaseModel):
    """Usage credits (Claude's extra usage): spent of the monthly cap, in minor units.

    Minor units of ``currency`` (cents for USD), as the CLI's ``get_usage``
    reply carries them; ``limit_minor`` is ``None`` for an uncapped plan.
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool
    used_minor: float | None = Field(default=None, ge=0)
    limit_minor: float | None = Field(default=None, ge=0)
    used_percent: float | None = Field(default=None, ge=0, le=1000)
    currency: str = Field(default="USD", min_length=1, max_length=8)


class SessionContext(BaseModel):
    """One Claude session's context window, from its latest status line.

    ``conversation_id`` is the OmniGent session the claude-native harness
    recorded in the bridge directory, so the popover can pick its own. The
    three token counts are ``context_window.current_usage`` from Claude
    Code's statusLine payload (``input_tokens``, ``cache_creation_input_tokens``,
    ``cache_read_input_tokens``); together they are the context in use.
    """

    model_config = ConfigDict(extra="forbid")

    conversation_id: str = Field(min_length=1, max_length=120)
    observed_at: datetime
    model: str | None = Field(default=None, max_length=120)
    window_size: int = Field(ge=1)
    used_tokens: int = Field(ge=0)
    input_tokens: int = Field(default=0, ge=0)
    cache_write_tokens: int = Field(default=0, ge=0)
    cache_read_tokens: int = Field(default=0, ge=0)


class SessionLimits(BaseModel):
    """One Claude session's own plan windows and their account, from its latest status line.

    Apart from :class:`SessionContext`, which needs a context reading: a status
    line carries limits without one before the first reply after ``/clear``.
    ``account`` is the backup account the session runs on; ``None`` for the
    box's default login, or a capture from before the account file existed.
    """

    model_config = ConfigDict(extra="forbid")

    conversation_id: str = Field(min_length=1, max_length=120)
    observed_at: datetime
    account: str | None = Field(default=None, max_length=120)
    account_label: str | None = Field(default=None, max_length=120)
    windows: list[LimitWindow] = Field(default_factory=list, max_length=4)


class KnownAccount(BaseModel):
    """One Claude login the host has signed in, as the relay found it: a name and a label.

    ``account`` is ``None`` for the CLI's default login. Never an email or a
    token: account names and labels share the host's alphabet (letters,
    digits, spaces, ``-`` and ``_``; ``shared:`` before a shared account).
    """

    model_config = ConfigDict(extra="forbid")

    account: str | None = Field(default=None, max_length=120)
    label: str = Field(min_length=1, max_length=120)


class LimitSnapshot(BaseModel):
    """The latest reading for one harness, as the host relay posts it.

    ``windows`` is empty when the CLI answered but reported no limits (an API
    key login, a plan without them); ``note`` then says why. ``hint`` is the
    one line that says how to get the rows a reading lacks (Claude's
    per-model windows and credits without a profile-scoped login).
    ``account`` and ``account_label`` name the backup account the windows
    belong to, ``None`` for the default login: the relay sets them when only a
    backup session has a reading, :func:`session_view` for a session on one.
    ``sessions`` carries each session's own windows and account.
    ``known_accounts`` (Claude only, 2026-10-21) lists the host's signed-in
    logins, so the Accounts card can show one that has no reading yet.
    """

    model_config = ConfigDict(extra="forbid")

    harness: Harness
    source: str = Field(min_length=1, max_length=60)
    observed_at: datetime
    plan: str | None = Field(default=None, max_length=60)
    windows: list[LimitWindow] = Field(default_factory=list, max_length=16)
    note: str | None = Field(default=None, max_length=500)
    credits: Credits | None = None
    contexts: list[SessionContext] = Field(default_factory=list, max_length=MAX_CONTEXTS)
    hint: str | None = Field(default=None, max_length=300)
    account: str | None = Field(default=None, max_length=120)
    account_label: str | None = Field(default=None, max_length=120)
    sessions: list[SessionLimits] = Field(default_factory=list, max_length=MAX_CONTEXTS)
    known_accounts: list[KnownAccount] = Field(default_factory=list, max_length=MAX_ACCOUNTS)


class AccountUsage(BaseModel):
    """One account's newest reading: what the Admin Center's Accounts card shows per row.

    ``account`` is ``None`` for the harness's default login. ``plan`` and
    ``credits`` come only from a source that carries them (the Claude default
    login's usage endpoint, the Codex plan type). ``observed_at`` is ``None``
    for a signed-in account with no reading yet. ``live_sessions`` counts the
    sessions the relay last saw on the account; ``None`` where nothing counts
    them (Codex).
    """

    model_config = ConfigDict(extra="forbid")

    harness: Harness
    account: str | None = Field(default=None, max_length=120)
    label: str = Field(min_length=1, max_length=120)
    plan: str | None = Field(default=None, max_length=60)
    windows: list[LimitWindow] = Field(default_factory=list, max_length=16)
    credits: Credits | None = None
    observed_at: datetime | None = None
    live_sessions: int | None = Field(default=None, ge=0)


def _epoch(value: object) -> datetime | None:
    """Epoch seconds as an aware UTC datetime; ``None`` for anything else."""
    if isinstance(value, bool) or not isinstance(value, int | float) or value <= 0:
        return None
    try:
        return datetime.fromtimestamp(float(value), tz=UTC)
    except (OverflowError, OSError, ValueError):
        return None


def _percent(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return max(0.0, min(float(value), 1000.0))


# ── Claude Code ─────────────────────────────────────────────────────────


def claude_snapshot(
    payload: Mapping[str, Any], observed_at: datetime, *, now: datetime | None = None
) -> LimitSnapshot:
    """Translate one Claude Code statusLine payload into a snapshot.

    :param payload: The statusLine stdin JSON, as the statusLine command saved it.
    :param observed_at: When Claude Code rendered it (the saved file's mtime).
    :param now: The clock, for tests; a window whose reset has passed is
        dropped, as Claude Code itself drops it.
    :returns: The snapshot; no windows and a note when the payload carries none.
    """
    now = now or datetime.now(UTC)
    raw = payload.get("rate_limits")
    windows: list[LimitWindow] = []
    if isinstance(raw, Mapping):
        for key, label, minutes in _CLAUDE_WINDOWS:
            entry = raw.get(key)
            if not isinstance(entry, Mapping):
                continue
            used = _percent(entry.get("used_percentage"))
            if used is None:
                continue
            resets_at = _epoch(entry.get("resets_at"))
            if resets_at is not None and resets_at <= now:
                continue
            windows.append(
                LimitWindow(
                    id=key,
                    label=label,
                    used_percent=used,
                    resets_at=resets_at,
                    window_minutes=minutes,
                )
            )
    note = None
    if not windows:
        note = (
            "Claude Code reported no plan limits in its last status line: they appear "
            "for a Claude subscription after the first reply of a session, and not for "
            "an API key."
        )
    return LimitSnapshot(
        harness="claude",
        source="statusline",
        observed_at=observed_at,
        windows=windows,
        note=note,
    )


def _count(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int | float) or value < 0:
        return 0
    return int(value)


def session_context(
    payload: Mapping[str, Any], conversation_id: str, observed_at: datetime
) -> SessionContext | None:
    """One session's context window from its statusLine payload; ``None`` before its first reply.

    :param payload: The statusLine stdin JSON the harness captured for the session.
    :param conversation_id: The OmniGent session the bridge directory belongs to.
    :param observed_at: When Claude Code rendered it (the capture's mtime).
    """
    raw = payload.get("context_window")
    if not isinstance(raw, Mapping):
        return None
    size = raw.get("context_window_size")
    usage = raw.get("current_usage")
    if isinstance(size, bool) or not isinstance(size, int) or size <= 0:
        return None
    if not isinstance(usage, Mapping):
        return None
    input_tokens = _count(usage.get("input_tokens"))
    cache_write = _count(usage.get("cache_creation_input_tokens"))
    cache_read = _count(usage.get("cache_read_input_tokens"))
    model = payload.get("model")
    name = None
    if isinstance(model, Mapping):
        raw_name = model.get("display_name") or model.get("id")
        name = raw_name.strip()[:120] if isinstance(raw_name, str) and raw_name.strip() else None
    return SessionContext(
        conversation_id=conversation_id[:120],
        observed_at=observed_at,
        model=name,
        window_size=size,
        used_tokens=input_tokens + cache_write + cache_read,
        input_tokens=input_tokens,
        cache_write_tokens=cache_write,
        cache_read_tokens=cache_read,
    )


# The plan names ``get_usage`` reports as ``subscription_type``, as /usage prints them.
_PLAN_NAMES = {"pro": "Pro", "max": "Max", "team": "Team", "enterprise": "Enterprise"}

# The rows the claude.ai usage endpoint has carried per model before it sent
# ``model_scoped``; used only when that list is absent.
_LEGACY_MODEL_WINDOWS = (("seven_day_opus", "Opus"), ("seven_day_sonnet", "Sonnet"))

PROFILE_HINT = "Per-model limits and credits: run `claude auth login` on the box."


def _iso(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _amount(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float) or value < 0:
        return None
    return float(value)


def _slug(text: str) -> str:
    out = "".join(ch if ch.isalnum() else "-" for ch in text.lower()).strip("-")
    return "-".join(part for part in out.split("-") if part) or "model"


def _usage_window(
    entry: object, window_id: str, label: str, minutes: int, now: datetime
) -> LimitWindow | None:
    if not isinstance(entry, Mapping):
        return None
    used = _percent(entry.get("utilization"))
    if used is None:
        return None
    resets_at = _iso(entry.get("resets_at"))
    if resets_at is not None and resets_at <= now:
        return None
    return LimitWindow(
        id=window_id[:120],
        label=label[:120],
        used_percent=used,
        resets_at=resets_at,
        window_minutes=minutes,
    )


def with_usage(
    snapshot: LimitSnapshot | None,
    reply: Mapping[str, Any] | None,
    observed_at: datetime,
    *,
    plan_label: str | None = None,
    now: datetime | None = None,
) -> LimitSnapshot | None:
    """Fold the CLI's ``get_usage`` reply into the statusLine snapshot.

    The reply is the claude.ai usage endpoint read just now, so where it has a
    window it replaces the statusLine's; it adds the per-model weekly windows
    (``model_scoped``, labelled "Weekly · <model>"), the usage credits and the
    plan. ``rate_limits_available`` false (no profile-scoped login, the box's
    default) leaves the statusLine reading as it was and sets
    :data:`PROFILE_HINT`.

    :param snapshot: The statusLine snapshot, or ``None`` when no status line exists.
    :param reply: The ``get_usage`` control response body, or ``None`` when the
        CLI could not be asked (the hint is set then too).
    :param observed_at: When the reply arrived.
    :param plan_label: A plan name to show instead of the reported one
        (``AE_LIMITS_CLAUDE_PLAN``, e.g. "Max 20x": the reply names the plan,
        not its tier).
    :returns: The merged snapshot; ``None`` only when there is neither.
    """
    now = now or datetime.now(UTC)
    limits_raw = reply.get("rate_limits") if reply else None
    available = bool(reply and reply.get("rate_limits_available")) and isinstance(
        limits_raw, Mapping
    )
    if not available or not isinstance(limits_raw, Mapping):
        if snapshot is None:
            return None
        return snapshot.model_copy(update={"hint": PROFILE_HINT})

    windows: list[LimitWindow] = []
    for key, label, minutes in _CLAUDE_WINDOWS[:2]:
        window = _usage_window(limits_raw.get(key), key, label, minutes or 0, now)
        if window is not None:
            windows.append(window)
    scoped = limits_raw.get("model_scoped")
    if isinstance(scoped, list):
        for entry in scoped:
            if not isinstance(entry, Mapping):
                continue
            name = entry.get("display_name")
            if not isinstance(name, str) or not name.strip():
                continue
            window = _usage_window(
                entry,
                f"seven_day.model.{_slug(name)}",
                f"Weekly · {name.strip()}",
                7 * 24 * 60,
                now,
            )
            if window is not None:
                windows.append(window)
    else:
        for key, name in _LEGACY_MODEL_WINDOWS:
            window = _usage_window(limits_raw.get(key), key, f"Weekly · {name}", 7 * 24 * 60, now)
            if window is not None:
                windows.append(window)
    if snapshot is not None and snapshot.account is None:
        # Anything the statusLine had that the endpoint did not (a gateway's
        # spend limit, or a window the reply left out) stays. Not a backup
        # account's status line: the reply is the default login's, and the
        # merged snapshot names no account (2026-10-21 review).
        have = {w.id for w in windows}
        windows.extend(w for w in snapshot.windows if w.id not in have)
    # /usage's order: 5-hour, weekly for all models, weekly per model, the rest.
    rank = {"five_hour": 0, "seven_day": 1}
    windows.sort(key=lambda w: rank.get(w.id, 2 if w.id.startswith("seven_day") else 3))

    credits = None
    extra = limits_raw.get("extra_usage")
    if isinstance(extra, Mapping) and isinstance(extra.get("is_enabled"), bool):
        currency = extra.get("currency")
        currency = (
            currency.strip()[:8] if isinstance(currency, str) and currency.strip() else "USD"
        )
        used = extra.get("used_credits")
        cap = extra.get("monthly_limit")
        credits = Credits(
            enabled=extra["is_enabled"],
            used_minor=_amount(used),
            limit_minor=_amount(cap),
            used_percent=_percent(extra.get("utilization")),
            currency=currency,
        )

    subscription = reply.get("subscription_type") if reply else None
    plan = plan_label or (
        _PLAN_NAMES.get(subscription, subscription.title())
        if isinstance(subscription, str) and subscription
        else None
    )
    return LimitSnapshot(
        harness="claude",
        source="usage" if snapshot is None else "statusline+usage",
        observed_at=observed_at,
        plan=plan[:60] if plan else None,
        windows=windows[:16],
        note=None if windows else "Claude's usage endpoint answered with no plan limits.",
        credits=credits,
        contexts=snapshot.contexts if snapshot is not None else [],
        sessions=snapshot.sessions if snapshot is not None else [],
    )


BACKUP_HINT = "Per-model limits, credits and the plan show only for the box's default login."
NO_WINDOWS_NOTE = (
    "This session's last status line carried no current limits; they return after its next reply."
)


def _known_label(snapshot: LimitSnapshot) -> str | None:
    """The host's label for the login *snapshot* describes, from ``known_accounts``.

    ``None`` when the host listed no login for ``snapshot.account`` (``None`` is
    the default login), so the caller keeps its own fallback.
    """
    return next((k.label for k in snapshot.known_accounts if k.account == snapshot.account), None)


def session_view(snapshot: LimitSnapshot, session_id: str) -> LimitSnapshot:
    """The Claude snapshot as one session sees it: its account's windows, not the host's.

    A session on a backup account gets its own status-line windows and none of
    the default login's plan, credits or per-model rows (``get_usage`` read
    those for the default login). A session on the default login keeps the
    host reading, which is its own account's and at least as new as its own
    status line (the usage endpoint, or the newest default-login status line),
    unless only a backup session had a reading for the host; its own windows
    and their time replace that one. Neither ever borrows the other account's
    numbers: with no windows of its own, the answer has none and says why.

    :param snapshot: The stored Claude snapshot.
    :param session_id: The OmniGent session asking, e.g. ``"conv_abc"``.
    :returns: *snapshot* unchanged when the session is unknown, or is on the
        default login and the host reading is the default login's too.
    """
    own = next((s for s in snapshot.sessions if s.conversation_id == session_id), None)
    if own is None or (own.account is None and snapshot.account is None):
        return snapshot
    update: dict[str, Any] = {
        "source": "statusline",
        "observed_at": own.observed_at,
        "windows": list(own.windows),
        "note": None if own.windows else NO_WINDOWS_NOTE,
        "account": own.account,
        "account_label": own.account_label,
    }
    if own.account is not None:
        update.update(plan=None, credits=None, hint=BACKUP_HINT)
    return snapshot.model_copy(update=update)


# ── Codex ───────────────────────────────────────────────────────────────


def _duration_label(minutes: int | None, fallback: str) -> str:
    if minutes is None:
        return fallback
    if minutes == 7 * 24 * 60:
        return "Weekly limit"
    if minutes % (24 * 60) == 0:
        days = minutes // (24 * 60)
        return f"{days}-day limit"
    if minutes % 60 == 0:
        return f"{minutes // 60}-hour limit"
    return f"{minutes}-minute limit"


def _codex_windows(snapshot: Mapping[str, Any], *, default_bucket: bool) -> list[LimitWindow]:
    limit_id = snapshot.get("limitId")
    bucket = limit_id if isinstance(limit_id, str) and limit_id else "codex"
    name = snapshot.get("limitName")
    prefix = ""
    if not default_bucket:
        prefix = f"{name.strip()}: " if isinstance(name, str) and name.strip() else f"{bucket}: "
    windows: list[LimitWindow] = []
    for slot, fallback in (("primary", "Primary limit"), ("secondary", "Secondary limit")):
        entry = snapshot.get(slot)
        if not isinstance(entry, Mapping):
            continue
        used = _percent(entry.get("usedPercent"))
        if used is None:
            continue
        minutes = entry.get("windowDurationMins")
        minutes = minutes if isinstance(minutes, int) and minutes > 0 else None
        windows.append(
            LimitWindow(
                id=f"{bucket}.{slot}",
                label=(prefix + _duration_label(minutes, fallback))[:120],
                used_percent=used,
                resets_at=_epoch(entry.get("resetsAt")),
                window_minutes=minutes,
            )
        )
    return windows


def codex_snapshot(response: Mapping[str, Any], observed_at: datetime) -> LimitSnapshot:
    """Translate a Codex ``account/rateLimits/read`` result into a snapshot.

    The multi-bucket view wins when present (the ``codex`` bucket first, then
    the rest by id); the single ``rateLimits`` view is the fallback.

    :param response: The JSON-RPC ``result`` object.
    :param observed_at: When the answer arrived.
    """
    windows: list[LimitWindow] = []
    plan: str | None = None
    by_id = response.get("rateLimitsByLimitId")
    single = response.get("rateLimits")
    snapshots: list[tuple[str, Mapping[str, Any]]] = []
    if isinstance(by_id, Mapping) and by_id:
        snapshots = sorted(
            ((key, value) for key, value in by_id.items() if isinstance(value, Mapping)),
            key=lambda item: (item[0] != "codex", item[0]),
        )
    elif isinstance(single, Mapping):
        snapshots = [("codex", single)]
    for index, (_, snap) in enumerate(snapshots):
        windows.extend(_codex_windows(snap, default_bucket=index == 0))
        raw_plan = snap.get("planType")
        if plan is None and isinstance(raw_plan, str) and raw_plan:
            plan = raw_plan
    note = None
    if not windows:
        note = "Codex answered with no plan limits: they appear for a ChatGPT login."
    elif response.get("ordinaryUsageAllowed") is False:
        note = "Codex says ordinary usage is not allowed right now."
    return LimitSnapshot(
        harness="codex",
        source="app-server",
        observed_at=observed_at,
        plan=plan,
        windows=windows[:16],
        note=note,
    )


# ── per account ─────────────────────────────────────────────────────────


AccountKey = tuple[str, str]
_EPOCH = datetime.fromtimestamp(0, tz=UTC)


def _key(harness: str, account: str | None) -> AccountKey:
    return (harness, account or "")


def account_readings(snapshot: LimitSnapshot) -> list[AccountUsage]:
    """What one snapshot says about each account, host reading first.

    Claude: the host reading is the default login's (or the backup account it
    names), with the plan and credits only for the default login; each
    session's own windows are its account's. Codex: the one login. A reading
    without windows says nothing about an account and is left out, so a
    status line from before a session's first reply cannot blank one.
    """
    if snapshot.harness == "codex":
        if not snapshot.windows:
            return []
        return [
            AccountUsage(
                harness="codex",
                label=CODEX_LABEL,
                plan=snapshot.plan,
                windows=list(snapshot.windows),
                observed_at=snapshot.observed_at,
            )
        ]
    out: list[AccountUsage] = []
    if snapshot.windows:
        default = snapshot.account is None
        out.append(
            AccountUsage(
                harness="claude",
                account=snapshot.account,
                label=snapshot.account_label or snapshot.account or DEFAULT_LABEL,
                plan=snapshot.plan if default else None,
                windows=list(snapshot.windows),
                credits=snapshot.credits if default else None,
                observed_at=snapshot.observed_at,
            )
        )
    for session in snapshot.sessions:
        if not session.windows:
            continue
        out.append(
            AccountUsage(
                harness="claude",
                account=session.account,
                label=session.account_label or session.account or DEFAULT_LABEL,
                windows=list(session.windows),
                observed_at=session.observed_at,
            )
        )
    return out


def headline_windows(
    windows: list[LimitWindow],
) -> tuple[LimitWindow | None, LimitWindow | None]:
    """The 5-hour and the weekly window /usage leads with; ``None`` when absent.

    Claude's by id (``five_hour``, ``seven_day``: never a per-model row, which
    is weekly too). Codex's by length, the first of each: its own bucket comes
    before any model bucket (:func:`codex_snapshot`).
    """
    five = next((w for w in windows if w.id == "five_hour"), None) or next(
        (w for w in windows if w.window_minutes == 300), None
    )
    # Claude's weekly rows are all seven_day*: only the aggregate one counts,
    # never a per-model or legacy (seven_day_opus) row standing in for it.
    week = next((w for w in windows if w.id == "seven_day"), None) or next(
        (
            w
            for w in windows
            if w.window_minutes == 7 * 24 * 60 and not w.id.startswith("seven_day")
        ),
        None,
    )
    return five, week


def merge_accounts(
    readings: list[AccountUsage],
    known: list[KnownAccount],
    shared: Mapping[str, str] | None,
    live: Mapping[str, int],
    *,
    claude_seen: bool = False,
) -> list[AccountUsage]:
    """Every account to show: signed-in ones (read or not) and every other one with a reading.

    Claude first: the default login, then the host's logins in its order, then
    the server's shared accounts by label, then any other account a reading
    names; then Codex, when its login has answered. A label comes from the
    shared-accounts store for a shared account (the admin's label), else the
    host's, else the reading's. An account that has left the host's list (or
    the shared store) drops out unless a session still runs on it; with no
    list from the host, every reading shows. Nothing Claude shows before the
    store has heard of a Claude login at all.

    :param readings: The store's newest reading per account.
    :param known: The host's signed-in Claude logins, from the newest Claude post.
    :param shared: Shared account id (``shared:<name>``) to its label, from the
        server's store; ``None`` when it could not be read (nothing is pruned then).
    :param live: Claude account (``""`` for the default) to its session count.
    :param claude_seen: Whether the store holds a Claude post, which means the
        default login exists even with no reading of its own.
    """
    by_key = {_key(r.harness, r.account): r for r in readings}
    shared_map = shared or {}
    known_keys = {_key("claude", entry.account) for entry in known}

    def gone(key: AccountKey) -> bool:
        if key[1] == "" or live.get(key[1], 0) > 0:
            return False
        if key[1].startswith(SHARED_PREFIX):
            return shared is not None and key[1] not in shared_map
        return bool(known) and key not in known_keys

    order: list[AccountKey] = []
    labels: dict[AccountKey, str] = {}
    for entry in known:
        key = _key("claude", entry.account)
        labels.setdefault(key, entry.label)
        if key not in order:
            order.append(key)
    for account in sorted(shared_map, key=lambda a: (shared_map[a].lower(), a)):
        key = _key("claude", account)
        labels[key] = f"{shared_map[account]} (shared)"[:120]
        if key not in order:
            order.append(key)
    order.extend(
        sorted(
            (k for k in by_key if k[0] == "claude" and k not in order and not gone(k)),
            key=lambda k: (by_key[k].label.lower(), k[1]),
        )
    )
    default = _key("claude", None)
    if order or claude_seen or default in by_key:
        order = [default, *(k for k in order if k != default)]
    # Codex keeps its row however many Claude accounts there are.
    order = order[: MAX_ACCOUNTS - 1]
    if _key("codex", None) in by_key:
        order.append(_key("codex", None))
    out: list[AccountUsage] = []
    for key in order:
        harness, account = key
        held = by_key.get(key)
        label = labels.get(key) or (held.label if held else None) or account or DEFAULT_LABEL
        # Only a Claude login can be listed without a reading; Codex needs one to appear.
        base = held or AccountUsage(harness="claude", account=account or None, label=label)
        update: dict[str, Any] = {"label": label}
        if harness == "claude":
            update["live_sessions"] = live.get(account, 0)
        out.append(base.model_copy(update=update))
    return out


# ── the server's store ──────────────────────────────────────────────────


def _newest(snapshot: LimitSnapshot) -> datetime:
    """The newest reading *snapshot* carries: its own, or any session's."""
    times = [snapshot.observed_at]
    times.extend(c.observed_at for c in snapshot.contexts)
    times.extend(s.observed_at for s in snapshot.sessions)
    return max(times)


class LimitStore:
    """The newest snapshot per harness, optionally mirrored to a JSON file.

    A snapshot older than the one held is ignored (two hosts, or a delayed
    retry, must not roll the panel back). Age is the newest reading a snapshot
    carries, its own or any session's: the host reading can be an idle
    default-login session's older status line while other sessions move on.

    Beside it, the newest reading per (harness, account) (:func:`account_readings`),
    updated on every post, kept or not: a post refused as a whole can still
    carry a newer reading for one account. Newest ``observed_at`` wins; a
    tie keeps the held one, which is the host reading when a status-line-only
    host reading and its own session arrive together.
    """

    def __init__(self, path: Path | None = None) -> None:
        self._lock = threading.Lock()
        self._path = path
        self._entries: dict[str, dict[str, Any]] = {}
        self._accounts: dict[AccountKey, AccountUsage] = {}
        self._loaded = False

    def _load(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        if self._path is None:
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except (OSError, json.JSONDecodeError) as exc:
            _logger.warning("ae limits: ignoring unreadable %s: %s", self._path, exc)
            return
        if not isinstance(raw, dict):
            return
        for harness in HARNESSES:
            entry = raw.get(harness)
            if not isinstance(entry, dict):
                continue
            try:
                snapshot = LimitSnapshot.model_validate(entry.get("snapshot"))
                received_at = datetime.fromisoformat(str(entry.get("received_at")))
            except (ValidationError, ValueError, TypeError):
                continue
            self._entries[harness] = {"snapshot": snapshot, "received_at": received_at}
        rows = raw.get(ACCOUNTS_KEY)
        if not isinstance(rows, list):
            # A file from before 2026-10-21: the held snapshots still name their accounts.
            for entry in self._entries.values():
                self._note(entry["snapshot"])
            return
        for row in rows:
            try:
                reading = AccountUsage.model_validate(row)
            except ValidationError:
                continue
            if reading.observed_at is not None:
                self._accounts[_key(reading.harness, reading.account)] = reading

    def _note(self, snapshot: LimitSnapshot) -> bool:
        """Fold *snapshot*'s account readings in; returns whether any changed."""
        changed = False
        for reading in account_readings(snapshot):
            key = _key(reading.harness, reading.account)
            held = self._accounts.get(key)
            if held is not None and held.observed_at and reading.observed_at:
                if held.observed_at >= reading.observed_at:
                    continue
                # A session's status line carries no plan: the default login
                # keeps the usage endpoint's last one. Never its credits, which
                # would then read as current under the newer time (review).
                if reading.plan is None:
                    reading = reading.model_copy(update={"plan": held.plan})
            self._accounts[key] = reading
            changed = True
        if len(self._accounts) > MAX_ACCOUNTS:
            oldest = sorted(self._accounts, key=lambda k: self._accounts[k].observed_at or _EPOCH)
            for key in oldest[: len(self._accounts) - MAX_ACCOUNTS]:
                del self._accounts[key]
        return changed

    def _save(self) -> None:
        if self._path is None:
            return
        body: dict[str, Any] = {
            harness: {
                "snapshot": entry["snapshot"].model_dump(mode="json"),
                "received_at": entry["received_at"].isoformat(),
            }
            for harness, entry in self._entries.items()
        }
        body[ACCOUNTS_KEY] = [r.model_dump(mode="json") for r in self._accounts.values()]
        tmp: str | None = None
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(prefix=".ae-limits-", dir=str(self._path.parent))
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(body, handle, separators=(",", ":"))
            os.replace(tmp, self._path)
        except OSError as exc:
            _logger.warning("ae limits: could not write %s: %s", self._path, exc)
            if tmp is not None:
                with contextlib.suppress(OSError):
                    os.unlink(tmp)

    def put(self, snapshot: LimitSnapshot, *, now: datetime | None = None) -> bool:
        """Keep *snapshot* unless a newer one is held; returns whether it was kept."""
        with self._lock:
            self._load()
            noted = self._note(snapshot)
            held = self._entries.get(snapshot.harness)
            if held is not None and _newest(held["snapshot"]) > _newest(snapshot):
                if noted:
                    self._save()
                return False
            self._entries[snapshot.harness] = {
                "snapshot": snapshot,
                "received_at": now or datetime.now(UTC),
            }
            self._save()
            return True

    def view(
        self, *, now: datetime | None = None, session_id: str | None = None
    ) -> dict[str, Any]:
        """What ``GET /v1/ae/limits`` answers: every harness, ``null`` when never seen.

        With *session_id* the Claude entry is that session's view (:func:`session_view`).
        """
        with self._lock:
            self._load()
            harnesses: dict[str, Any] = {}
            for harness in HARNESSES:
                entry = self._entries.get(harness)
                if entry is None:
                    harnesses[harness] = None
                    continue
                snapshot = entry["snapshot"]
                if session_id is not None and harness == "claude":
                    snapshot = session_view(snapshot, session_id)
                # The host's login list is the Accounts route's alone, but its
                # label names the login the answer describes (2026-10-21): the
                # default login's reading carries none of its own.
                label = snapshot.account_label or _known_label(snapshot)
                body = snapshot.model_dump(mode="json", exclude={"known_accounts"})
                body["account_label"] = label
                body["received_at"] = entry["received_at"].isoformat()
                harnesses[harness] = body
        return {"now": (now or datetime.now(UTC)).isoformat(), "harnesses": harnesses}

    def accounts(self, shared: Mapping[str, str] | None = None) -> list[AccountUsage]:
        """Every account to show (:func:`merge_accounts`), with the held Claude post's counts.

        The signed-in logins and the live session counts come from the newest
        Claude post the store kept: the relay lists every session it saw then.

        :param shared: Shared account id to label, from the server's store;
            ``None`` when it could not be read.
        """
        with self._lock:
            self._load()
            readings = list(self._accounts.values())
            entry = self._entries.get("claude")
        known: list[KnownAccount] = []
        live: dict[str, int] = {}
        if entry is not None:
            snapshot: LimitSnapshot = entry["snapshot"]
            known = list(snapshot.known_accounts)
            for session in snapshot.sessions:
                live[session.account or ""] = live.get(session.account or "", 0) + 1
        return merge_accounts(readings, known, shared, live, claude_seen=entry is not None)


_store: LimitStore | None = None
_store_lock = threading.Lock()


def _default_path() -> Path | None:
    raw = (os.environ.get(STATE_FILE_ENV) or "").strip()
    if raw:
        return Path(raw)
    if DATA_DIR.is_dir():
        return DATA_DIR / DATA_FILE_NAME
    return None


def store() -> LimitStore:
    """The process-wide store, built on first use from :data:`STATE_FILE_ENV` or ``/data``."""
    global _store
    with _store_lock:
        if _store is None:
            _store = LimitStore(_default_path())
        return _store


def reset_store(replacement: LimitStore | None = None) -> None:
    """Drop the process-wide store (tests change the environment between cases)."""
    global _store
    with _store_lock:
        _store = replacement


__all__ = [
    "ACCOUNTS_KEY",
    "BACKUP_HINT",
    "CODEX_LABEL",
    "DEFAULT_LABEL",
    "HARNESSES",
    "MAX_ACCOUNTS",
    "MAX_CONTEXTS",
    "NO_WINDOWS_NOTE",
    "PROFILE_HINT",
    "STATE_FILE_ENV",
    "AccountUsage",
    "Credits",
    "Harness",
    "KnownAccount",
    "LimitSnapshot",
    "LimitStore",
    "LimitWindow",
    "SessionContext",
    "SessionLimits",
    "account_readings",
    "claude_snapshot",
    "codex_snapshot",
    "headline_windows",
    "merge_accounts",
    "reset_store",
    "session_context",
    "session_view",
    "store",
    "with_usage",
]
