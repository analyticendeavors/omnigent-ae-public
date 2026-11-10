"""The Admin Center's Social card: what to post on LinkedIn and YouTube, and when (2026-11-06).

Why: Reid publishes videos, Shorts and livestreams on two YouTube channels and must promote each
on LinkedIn, promote three of his tools and write a weekly expert post. This module keeps the
YouTube readings the box relay posts, plans the posts due (:func:`plan`), scores how consistent
he is (:func:`score`) and names the gaps (:func:`gaps`). Posting itself stays manual: the card
copies the draft and opens the site, then Reid marks it posted.

Everything here is a pure function of ``(state, now)``; the store, the routes and the relay are
thin shells around it. The state lives in ``/data/ae-social.json`` (``AE_SOCIAL_FILE`` moves it,
an empty value keeps it in memory), written atomically like :mod:`.secret_owners`.

Limits, said once: nothing reads LinkedIn, and YouTube has no Community-post API, so "posted" is
always what Reid says, and the score is only as honest as the mark.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import math
import os
import tempfile
import threading
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, Literal, TypeVar
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ae_omni_policies import social_visual

STATE_FILE_ENV = "AE_SOCIAL_FILE"
# The channels' names as a post says them (the editor's starter text). They come from this JSON
# object, {"ae": "...", "hc": "..."}, because a company name does not belong in a public repo;
# an unset or bad value falls back to DEFAULT_CHANNEL_NAMES (2026-11-06).
CHANNEL_NAMES_ENV = "AE_SOCIAL_CHANNEL_NAMES"
DEFAULT_CHANNEL_NAMES = {"ae": "Channel 1", "hc": "Channel 2"}
DATA_DIR = Path("/data")
DATA_FILE_NAME = "ae-social.json"

CHANNELS = ("ae", "hc")
# The YouTube channel ids and the promoted tools come from the server's environment, not from
# this file, so the public install carries none of the owner's (2026-11-07, card setup).
# AE_SOCIAL_TOOLS seeds only a fresh state: tools already saved in the Social state stay,
# because they are the owner's editable data, not code.
CHANNEL_IDS_ENV = "AE_SOCIAL_CHANNEL_IDS"
TOOLS_ENV = "AE_SOCIAL_TOOLS"
HORIZON_DAYS = 35
HISTORY_DAYS = 63
STALE_HOURS = 6
MAX_ITEMS = 300
SCORECARD_ROWS = 6
"""Rows of the Scorecard's "went public without a post" list that ride in GET /social."""
MAX_DRAFT = 3000
MAX_TOPIC = 120
PREVIEW = 160
RESPONSE_CAP = 32_768
"""The extension bridge's limit on one response body, in characters."""
MARGIN = 1_500
"""Characters kept free under :data:`RESPONSE_CAP` when a response fills slots."""
WEEKS_KEPT = 104
FIXED_MONDAY = date(2024, 1, 1)
FILL_WEEKDAYS = (1, 3, 0, 4)
"""Tue, Thu, Mon, Fri: the order the planner tries a free day when a week is short of posts."""

SlotKind = Literal["video", "short", "live", "community", "tool", "expert"]
Platform = Literal["linkedin", "yt_community"]
SlotState = Literal["planned", "posted", "skipped"]
ChannelId = Literal["ae", "hc"]

SLOT_ID = r"^[A-Za-z0-9_.-]{3,80}$"
VIDEO_ID = r"^[A-Za-z0-9_-]{11}$"
HHMM = r"^([01]\d|2[0-3]):[0-5]\d$"
TOOL_ID = r"^[a-z0-9-]{1,32}$"


class SocialError(Exception):
    """A request the state cannot honor; *status* is the HTTP answer the route gives."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


class SocialSaveError(OSError):
    """The state file could not be read or written."""


# ── models ────────────────────────────────────────────────────────────────────────────────────


class Item(BaseModel):
    """One YouTube upload as the lister reports it."""

    video_id: str = Field(pattern=VIDEO_ID)
    title: str = Field(max_length=200)
    kind: Literal["long", "short", "live"]
    privacy: Literal["public", "private", "unlisted"]
    public_at: datetime | None = None
    live_state: Literal["upcoming", "live", "none"] = "none"
    duration_s: int | None = Field(default=None, ge=0, le=10_000_000)
    url: str = ""

    @field_validator("public_at")
    @classmethod
    def _utc(cls, value: datetime | None) -> datetime | None:
        return None if value is None else _as_utc(value)

    def with_url(self) -> Item:
        """The link rebuilt from the id and kind; a posted ``url`` is never trusted."""
        path = f"shorts/{self.video_id}" if self.kind == "short" else self.video_id
        host = "https://www.youtube.com" if self.kind == "short" else "https://youtu.be"
        return self.model_copy(update={"url": f"{host}/{path}"})


class ChannelReading(BaseModel):
    read_at: datetime | None = None
    ok: bool = False
    error: str | None = None
    items: list[Item] = Field(default_factory=list)


class Tool(BaseModel):
    id: str = Field(pattern=TOOL_ID)
    name: str = Field(min_length=1, max_length=80)
    url: str = Field(pattern=r"^https://\S{1,290}$")
    note: str | None = Field(default=None, max_length=200)
    active: bool = True
    image: str | None = Field(default=None, pattern=r"^https://\S{1,290}$", max_length=300)
    """The picture that goes in the share sheet with the text; ``None`` shares text only."""

    @field_validator("image")
    @classmethod
    def _fetchable(cls, value: str | None) -> str | None:
        if value is not None and not social_visual.tool_image_ok(value):
            raise ValueError(
                "image must be an https link on a host this server allows "
                "(opengraph.githubassets.com, or those in AE_SOCIAL_IMAGE_HOSTS)"
            )
        return value

    @model_validator(mode="before")
    @classmethod
    def _default_image(cls, data: Any) -> Any:
        # A tool saved before the field existed (no ``image`` key) takes the picture of the
        # AE_SOCIAL_TOOLS entry with the same id, so state already on a server benefits without a
        # re-save; an explicit null is the owner clearing it and stays null. An env picture on a
        # host the server does not allow is ignored (2026-11-07, card setup).
        if isinstance(data, dict) and "image" not in data:
            image = _env_tool_images().get(data.get("id"))
            if image is not None:
                return {**data, "image": image}
        return data


def _env_tool_images() -> dict[str, str]:
    """Tool id to the ``image`` its AE_SOCIAL_TOOLS entry names, when the server may fetch it."""
    try:
        given = json.loads(os.environ.get(TOOLS_ENV) or "[]")
    except ValueError:
        return {}
    if not isinstance(given, list):
        return {}
    return {
        entry["id"]: entry["image"]
        for entry in given
        if isinstance(entry, dict)
        and isinstance(entry.get("id"), str)
        and isinstance(entry.get("image"), str)
        and social_visual.tool_image_ok(entry["image"])
    }


def default_tools() -> list[Tool]:
    """The tools to promote, from :data:`TOOLS_ENV` (a JSON list); none when unset or bad."""
    try:
        given = json.loads(os.environ.get(TOOLS_ENV) or "[]")
    except ValueError:
        return []
    if not isinstance(given, list):
        return []
    tools: list[Tool] = []
    for entry in given:
        if not isinstance(entry, dict):
            continue
        try:
            tools.append(Tool(**entry))
        except (ValueError, TypeError):
            continue
    return tools


class Rules(BaseModel):
    video_li: bool = True
    video_community: bool = True
    short_li: bool = True
    short_li_days: int = Field(default=3, ge=0, le=30)
    live_li: bool = True
    tool_every_days: int = Field(default=7, ge=1, le=365)
    expert_per_week: int = Field(default=1, ge=0, le=7)
    expert_weekday: int = Field(default=2, ge=0, le=6)
    li_weekly_target: int = Field(default=3, ge=0, le=14)
    gap_days: int = Field(default=7, ge=1, le=60)
    alarm_hhmm: str = Field(default="09:00", pattern=HHMM)
    tz: str = Field(default="America/Los_Angeles", max_length=64)

    @field_validator("tz")
    @classmethod
    def _known_zone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ValueError, OSError, KeyError) as exc:
            raise ValueError(f"{value!r} is not a time zone name") from exc
        return value


class Slot(BaseModel):
    id: str = Field(pattern=SLOT_ID)
    kind: SlotKind
    platform: Platform
    channel: ChannelId | None = None
    anchor: str | None = None
    due: datetime
    manual: bool = False
    skippable: bool = False
    state: SlotState = "planned"
    rescheduled: bool = False
    orphan: bool = False
    draft_error: str | None = None
    alarm_task_id: str | None = None

    @field_validator("due")
    @classmethod
    def _utc(cls, value: datetime) -> datetime:
        return _as_utc(value)


class Draft(BaseModel):
    text: str = Field(max_length=MAX_DRAFT)
    topic: str | None = Field(default=None, max_length=MAX_TOPIC)
    by: Literal["auto", "reid"] = "auto"
    at: datetime
    audit_ok: bool | None = None
    edited: bool = False


class Posted(BaseModel):
    at: datetime
    url: str | None = Field(default=None, pattern=r"^https://\S{1,490}$")
    linkedin_urn: str | None = None
    via: Literal["manual", "api"] = "manual"


class State(BaseModel):
    version: int = 1
    uploads: dict[str, ChannelReading] = Field(
        default_factory=lambda: {ch: ChannelReading() for ch in CHANNELS}
    )
    tools: list[Tool] = Field(default_factory=default_tools)
    rules: Rules = Field(default_factory=Rules)
    slots: dict[str, Slot] = Field(default_factory=dict)
    drafts: dict[str, Draft] = Field(default_factory=dict)
    posted: dict[str, Posted] = Field(default_factory=dict)
    best_streak: int = 0
    week_posted: dict[str, int] = Field(default_factory=dict)
    """LinkedIn posts marked posted per local ISO week, keyed by that week's Monday (kept 104
    weeks), so a streak outlives the 63 days the posted marks are kept."""
    start_at: datetime | None = None
    """When uploads were first posted: nothing earlier is planned or scored."""


# ── time helpers ──────────────────────────────────────────────────────────────────────────────


def utcnow() -> datetime:
    return datetime.now(UTC)


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _zone(rules: Rules) -> ZoneInfo:
    return ZoneInfo(rules.tz)


def _local_at(day: date, rules: Rules) -> datetime:
    """*day* at ``alarm_hhmm`` on the wall clock of ``rules.tz``, as UTC."""
    hour, minute = (int(part) for part in rules.alarm_hhmm.split(":"))
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=_zone(rules)).astimezone(
        UTC
    )


def _local_date(moment: datetime, rules: Rules) -> date:
    return moment.astimezone(_zone(rules)).date()


def _monday(day: date) -> date:
    return day - timedelta(days=day.weekday())


def _week_key(monday: date) -> str:
    iso = monday.isocalendar()
    return f"{iso.year}w{iso.week:02d}"


def _week_start(monday: date, rules: Rules) -> datetime:
    return datetime(monday.year, monday.month, monday.day, tzinfo=_zone(rules)).astimezone(UTC)


def _week_of(moment: datetime, rules: Rules) -> date:
    """The Monday (local date) of the ISO week *moment* falls in."""
    return _monday(_local_date(moment, rules))


# ── planning ──────────────────────────────────────────────────────────────────────────────────


def _anchor_item(state: State, slot: Slot) -> Item | None:
    if slot.channel is None or slot.anchor is None:
        return None
    for item in state.uploads.get(slot.channel, ChannelReading()).items:
        if item.video_id == slot.anchor:
            return item
    return None


def _upload_slots(state: State, now: datetime) -> dict[str, Slot]:
    """The slots the stored uploads and the rules call for, by id."""
    if state.start_at is None:
        return {}
    rules = state.rules
    lo, hi = now - timedelta(days=HISTORY_DAYS), now + timedelta(days=HORIZON_DAYS)
    if state.start_at is not None:
        lo = max(lo, state.start_at)
    wanted: dict[str, Slot] = {}
    for channel in CHANNELS:
        for item in state.uploads.get(channel, ChannelReading()).items:
            when = item.public_at
            if when is None:
                continue
            if item.kind == "long":
                if rules.video_li:
                    sid = f"li-video-{channel}-{item.video_id}"
                    wanted[sid] = Slot(
                        id=sid, kind="video", platform="linkedin", channel=channel,
                        anchor=item.video_id, due=when,
                    )  # fmt: skip
                if rules.video_community:
                    sid = f"yc-video-{channel}-{item.video_id}"
                    wanted[sid] = Slot(
                        id=sid, kind="video", platform="yt_community", channel=channel,
                        anchor=item.video_id, due=when,
                    )  # fmt: skip
            elif item.kind == "short":
                if rules.short_li:
                    sid = f"li-short-{channel}-{item.video_id}"
                    wanted[sid] = Slot(
                        id=sid, kind="short", platform="linkedin", channel=channel,
                        anchor=item.video_id, due=when, skippable=True,
                    )  # fmt: skip
            elif rules.live_li:
                before = _local_date(when, rules) - timedelta(days=1)
                sid = f"li-live-{channel}-{item.video_id}"
                wanted[sid] = Slot(
                    id=sid, kind="live", platform="linkedin", channel=channel,
                    anchor=item.video_id, due=_local_at(before, rules),
                )  # fmt: skip
    return {sid: slot for sid, slot in wanted.items() if lo <= slot.due <= hi}


def _is_upload_slot(slot: Slot) -> bool:
    return slot.kind in ("video", "short", "live")


def _frozen(state: State, slot: Slot) -> bool:
    return slot.state != "planned" or slot.id in state.drafts or slot.rescheduled


def _last_use(state: State, tool_id: str, before: datetime) -> datetime | None:
    """When the tool last had a tool post: a posted one by its mark, a planned one by its date
    when that date is earlier than the slot being filled (so one planning pass rotates)."""
    latest: datetime | None = None
    for slot in state.slots.values():
        if slot.kind != "tool" or slot.anchor != tool_id or slot.state == "skipped":
            continue
        record = state.posted.get(slot.id)
        if record is not None:
            when = record.at
        elif slot.due < before:
            when = slot.due
        else:
            continue
        if latest is None or when > latest:
            latest = when
    return latest


def _pick_tool(state: State, due: datetime) -> str | None:
    active = [tool for tool in state.tools if tool.active]
    if not active:
        return None
    epoch = datetime(1970, 1, 1, tzinfo=UTC)
    ranked = sorted(
        enumerate(active), key=lambda pair: (_last_use(state, pair[1].id, due) or epoch, pair[0])
    )
    return ranked[0][1].id


def _weeks_ahead(now: datetime, rules: Rules) -> list[date]:
    """Mondays from this week through the horizon."""
    first = _week_of(now, rules)
    last = _week_of(now + timedelta(days=HORIZON_DAYS), rules)
    out, cur = [], first
    while cur <= last:
        out.append(cur)
        cur += timedelta(days=7)
    return out


def _since_start(state: State, due: datetime) -> bool:
    """Nothing is planned due before the first YouTube read, to the minute."""
    return state.start_at is None or due >= state.start_at


def _expert_n(slot_id: str) -> int | None:
    """The trailing number of ``li-expert-<week>-<n>``."""
    tail = slot_id.rsplit("-", 1)[-1]
    return int(tail) if tail.isdigit() else None


def _free_expert_n(state: State, key: str, start: int) -> int:
    n = start
    while f"li-expert-{key}-{n}" in state.slots:
        n += 1
    return n


def _week_li(state: State, monday: date) -> list[Slot]:
    """Every LinkedIn slot in the week, skipped ones too: a skip ends that obligation."""
    rules = state.rules
    return [
        slot
        for slot in state.slots.values()
        if slot.platform == "linkedin" and _week_of(slot.due, rules) == monday
    ]


def plan(state: State, now: datetime) -> State:
    """The state with its slots planned from the uploads and rules; idempotent.

    Never changes or deletes a slot that is posted or skipped, has a draft, or was rescheduled.
    A planned slot with no draft whose source upload is gone is deleted; with a draft it stays
    and is marked ``orphan``. Slots due more than 63 days ago go, with their drafts and marks.
    ``best_streak`` is raised here too, so every save keeps it.
    """
    state = state.model_copy(deep=True)
    rules = state.rules
    today = _local_date(now, rules)

    # 1. Upload-based slots.
    wanted = _upload_slots(state, now)
    for sid, slot in list(state.slots.items()):
        if not _is_upload_slot(slot) or slot.state != "planned":
            continue
        if sid in wanted:
            if not _frozen(state, slot):
                slot.due, slot.skippable, slot.orphan = (
                    wanted[sid].due,
                    wanted[sid].skippable,
                    False,
                )
            elif not slot.rescheduled:
                slot.orphan = False
        elif _frozen(state, slot):
            if sid in state.drafts and not slot.rescheduled:
                slot.orphan = True
        else:
            del state.slots[sid]
    for sid, slot in wanted.items():
        state.slots.setdefault(sid, slot)

    # 2. Weekly tool and expert slots, oldest week first so the rotation sees earlier picks.
    floor = today
    if state.start_at is not None:
        floor = max(floor, _local_date(state.start_at, rules))
    cadence = max(1, round(rules.tool_every_days / 7))
    # Until a channel has read ok (start_at is None) nothing is planned at all.
    weeks = _weeks_ahead(now, rules) if state.start_at is not None else []
    for monday in weeks:
        key = _week_key(monday)
        # Unfrozen future slots follow the current rules (frozen ones never change).
        for sid, slot in list(state.slots.items()):
            if slot.kind not in ("tool", "expert") or slot.manual or _frozen(state, slot):
                continue
            if _week_of(slot.due, rules) != monday or _local_date(slot.due, rules) < today:
                continue
            if slot.kind == "tool":
                if sid != f"li-tool-{key}" or ((monday - FIXED_MONDAY).days // 7) % cadence:
                    del state.slots[sid]
                continue
            n = _expert_n(sid)
            if n is None or n > rules.expert_per_week:
                del state.slots[sid]
                continue
            moved = monday + timedelta(days=rules.expert_weekday)
            if moved >= today:
                slot.due = _local_at(moved, rules)
        if ((monday - FIXED_MONDAY).days // 7) % cadence == 0:
            sid = f"li-tool-{key}"
            friday = monday + timedelta(days=4)
            due = _local_at(friday, rules)
            existing = state.slots.get(sid)
            if existing is not None and not _frozen(state, existing) and friday >= today:
                existing.due = due
                active = {tool.id for tool in state.tools if tool.active}
                if existing.anchor not in active:
                    del state.slots[sid]
            if (
                sid not in state.slots
                and friday >= floor
                and _since_start(state, due)
                and (tool_id := _pick_tool(state, due))
            ):
                state.slots[sid] = Slot(
                    id=sid, kind="tool", platform="linkedin", anchor=tool_id, due=due
                )
        expert_day = monday + timedelta(days=rules.expert_weekday)
        for n in range(1, rules.expert_per_week + 1):
            sid = f"li-expert-{key}-{n}"
            if (
                sid not in state.slots
                and expert_day >= floor
                and _since_start(state, _local_at(expert_day, rules))
            ):
                state.slots[sid] = Slot(
                    id=sid, kind="expert", platform="linkedin", due=_local_at(expert_day, rules)
                )  # fmt: skip
        _fill_week(state, monday, floor)

    # 3. History.
    cutoff = now - timedelta(days=HISTORY_DAYS)
    for sid, slot in list(state.slots.items()):
        if slot.due < cutoff:
            del state.slots[sid]
    for table in (state.drafts, state.posted):
        for sid in [sid for sid in table if sid not in state.slots]:
            del table[sid]

    oldest = (_week_of(now, rules) - timedelta(weeks=WEEKS_KEPT)).isoformat()
    state.week_posted = {k: v for k, v in state.week_posted.items() if k >= oldest and v > 0}
    state.best_streak = max(state.best_streak, score(state, now)["streak"])
    return state


def _fill_week(state: State, monday: date, today: date) -> None:
    """Add expert slots until the week's LinkedIn posts reach the target."""
    rules = state.rules
    key = _week_key(monday)
    for weekday in FILL_WEEKDAYS:
        if len(_week_li(state, monday)) >= rules.li_weekly_target:
            return
        day = monday + timedelta(days=weekday)
        if day < today:
            continue
        busy = {_local_date(slot.due, rules) for slot in _week_li(state, monday)}
        if day in busy or not _since_start(state, _local_at(day, rules)):
            continue
        n = _free_expert_n(state, key, rules.expert_per_week + 1)
        sid = f"li-expert-{key}-{n}"
        state.slots[sid] = Slot(
            id=sid, kind="expert", platform="linkedin", due=_local_at(day, rules)
        )  # fmt: skip


# ── score ─────────────────────────────────────────────────────────────────────────────────────


def _li_posted_in_week(state: State, monday: date) -> int:
    """LinkedIn posts marked posted in the week: the larger of the kept marks and the stored
    count, which outlives the marks (they go after 63 days)."""
    rules = state.rules
    start = _week_start(monday, rules)
    end = _week_start(monday + timedelta(days=7), rules)
    marked = sum(
        1
        for slot in state.slots.values()
        if slot.platform == "linkedin"
        and slot.state == "posted"
        and (record := state.posted.get(slot.id)) is not None
        and start <= record.at < end
    )
    return max(marked, state.week_posted.get(monday.isoformat(), 0))


def _slot_done_window(slot: Slot, rules: Rules) -> datetime:
    return slot.due + timedelta(days=rules.short_li_days) if slot.kind == "short" else slot.due


def score(state: State, now: datetime) -> dict[str, Any]:
    """The numbers the card shows; the meanings are in ``docs/features/admin-center.md``."""
    rules = state.rules
    target = rules.li_weekly_target
    monday = _week_of(now, rules)
    day_number = now.astimezone(_zone(rules)).isoweekday()
    week_posted = _li_posted_in_week(state, monday)
    on_pace = week_posted >= math.floor(target * day_number / 7)

    promoted = missed = 0
    lo = now - timedelta(days=30)
    for channel in CHANNELS:
        for item in state.uploads.get(channel, ChannelReading()).items:
            if item.public_at is None or not lo <= item.public_at <= now:
                continue
            mine = [
                slot
                for slot in state.slots.values()
                if slot.channel == channel
                and slot.anchor == item.video_id
                and _is_upload_slot(slot)
            ]
            if not mine:
                continue
            finished = all(slot.state != "planned" for slot in mine)
            if not finished and not all(_slot_done_window(s, rules) <= now for s in mine):
                continue
            if finished:
                promoted += 1
            else:
                missed += 1
    denominator = promoted + missed

    first_monday = _week_of(state.start_at, rules) if state.start_at is not None else None
    streak, cursor = 0, monday - timedelta(days=7)
    for _ in range(WEEKS_KEPT):
        if (
            state.start_at is not None
            and _week_start(cursor + timedelta(days=7), rules) <= state.start_at
        ):
            break  # a week wholly before the start neither counts nor breaks
        if cursor == first_monday:
            break  # the first week is a partial one: it neither counts nor breaks
        if _li_posted_in_week(state, cursor) < target or target == 0:
            break
        streak += 1
        cursor -= timedelta(days=7)
    first_week = first_monday == monday
    if target > 0 and week_posted >= target and not first_week:
        streak += 1

    recent = [
        (state.slots[sid], record)
        for sid, record in state.posted.items()
        if sid in state.slots and record.at >= now - timedelta(days=30)
    ]
    on_time = sum(1 for slot, record in recent if record.at - slot.due <= timedelta(hours=24))
    community = [
        slot
        for slot in state.slots.values()
        if slot.platform == "yt_community" and lo <= slot.due <= now
    ]
    weeks = []
    for back in range(7, -1, -1):
        week = monday - timedelta(days=7 * back)
        row: dict[str, Any] = {
            "week": _week_key(week),
            "start": week.isoformat(),
            "posted": _li_posted_in_week(state, week),
        }
        if state.start_at is not None:
            if _week_start(week + timedelta(days=7), rules) <= state.start_at:
                row["state"] = "before_start"
            elif week == first_monday:
                row["state"] = "first"
        weeks.append(row)
    return {
        "week_posted": week_posted,
        "week_target": target,
        "on_pace": on_pace,
        "promoted": promoted,
        "promoted_of": denominator,
        "missed": missed,
        "streak": streak,
        "best_streak": max(state.best_streak, streak),
        "on_time_pct": round(100 * on_time / len(recent)) if recent else None,
        "on_time_of": len(recent),
        "community_30d": {
            "posted": sum(1 for slot in community if slot.state == "posted"),
            "total": len(community),
        },
        "weeks": weeks,
        "first_week": first_week,
        **_scorecard(state, now),
    }


def _scorecard(state: State, now: datetime) -> dict[str, Any]:
    """The Scorecard tab's lists: went public without a post, tool rotation, upload pace."""
    rules = state.rules
    lo = now - timedelta(days=30)
    missed_rows: list[tuple[datetime, Slot]] = []
    for slot in state.slots.values():
        if not _is_upload_slot(slot) or slot.state != "planned":
            continue
        item = _anchor_item(state, slot)
        # A livestream's slot is its announcement the day before; the upload's own
        # public_at says when it went public, so nothing is missed before that.
        public = item.public_at if item is not None and item.public_at is not None else slot.due
        window = public + timedelta(days=rules.short_li_days) if slot.kind == "short" else public
        if lo <= public <= now and window <= now:
            missed_rows.append((public, slot))
    missed_rows.sort(key=lambda row: row[0], reverse=True)
    unposted = []
    for public, slot in missed_rows[:SCORECARD_ROWS]:
        item = _anchor_item(state, slot)
        unposted.append(
            {
                "id": slot.id,
                "kind": slot.kind,
                "platform": slot.platform,
                "channel": slot.channel,
                "title": (item.title[:80] if item is not None else None),
                "due": _iso(public),
            }
        )
    rotation = []
    for tool in state.tools:
        if not tool.active:
            continue
        last: datetime | None = None
        nxt: Slot | None = None
        for slot in state.slots.values():
            if slot.kind != "tool" or slot.anchor != tool.id:
                continue
            record = state.posted.get(slot.id)
            if record is not None and (last is None or record.at > last):
                last = record.at
            if slot.state == "planned" and (nxt is None or slot.due < nxt.due):
                nxt = slot
        rotation.append(
            {
                "id": tool.id,
                "name": tool.name,
                "last": _iso(last) if last is not None else None,
                "next": _iso(nxt.due) if nxt is not None else None,
            }
        )
    horizon = now + timedelta(days=14)
    pace: dict[str, dict[str, int]] = {}
    for channel in CHANNELS:
        counts = {"long": 0, "short": 0, "live": 0}
        for item in state.uploads.get(channel, ChannelReading()).items:
            if item.public_at is not None and now <= item.public_at <= horizon:
                counts[item.kind] += 1
        pace[channel] = counts
    return {
        "unposted": unposted,
        "unposted_total": len(missed_rows),
        "rotation": rotation,
        "pace": pace,
    }


# ── gaps ──────────────────────────────────────────────────────────────────────────────────────


def gaps(state: State, now: datetime) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    horizon = now + timedelta(days=state.rules.gap_days)
    for channel in CHANNELS:
        reading = state.uploads.get(channel, ChannelReading())
        if reading.read_at is None:
            out[channel] = {
                "read_failed": False,
                "error": None,
                "stale": False,
                "upload_gap": False,
            }
            continue
        coming = any(
            item.public_at is not None and now < item.public_at <= horizon
            for item in reading.items
        )
        out[channel] = {
            "read_failed": not reading.ok,
            "error": reading.error if not reading.ok else None,
            "stale": now - reading.read_at > timedelta(hours=STALE_HOURS),
            "upload_gap": reading.ok and not coming,
        }
    return out


# ── the page ──────────────────────────────────────────────────────────────────────────────────


def channel_ids() -> dict[str, str]:
    """The channels' YouTube ids from :data:`CHANNEL_IDS_ENV` (a JSON object); ``{}`` when unset
    or bad. A channel with no id is left out, and every reader copes with that."""
    try:
        given = json.loads(os.environ.get(CHANNEL_IDS_ENV) or "{}")
    except ValueError:
        return {}
    if not isinstance(given, dict):
        return {}
    return {
        channel: value.strip()
        for channel in CHANNELS
        if isinstance(value := given.get(channel), str) and value.strip()
    }


def channel_names() -> dict[str, str]:
    """The channel names for starter text: the env override over the defaults, per channel."""
    names = dict(DEFAULT_CHANNEL_NAMES)
    try:
        given = json.loads(os.environ.get(CHANNEL_NAMES_ENV) or "{}")
    except ValueError:
        return names
    if isinstance(given, dict):
        for channel in CHANNELS:
            value = given.get(channel)
            if isinstance(value, str) and 0 < len(value.strip()) <= 60:
                names[channel] = value.strip()
    return names


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _public_now(item: Item, now: datetime) -> bool:
    """Whether viewers can see *item* now: public, or scheduled to go public already."""
    return item.privacy == "public" or (item.public_at is not None and item.public_at <= now)


def _slot_row(state: State, slot: Slot, now: datetime | None = None) -> dict[str, Any]:
    now = now or utcnow()
    item = _anchor_item(state, slot)
    tool = (
        next((t for t in state.tools if t.id == slot.anchor), None)
        if slot.kind == "tool"
        else None
    )
    draft = state.drafts.get(slot.id)
    record = state.posted.get(slot.id)
    row: dict[str, Any] = {
        "id": slot.id,
        "kind": slot.kind,
        "platform": slot.platform,
        "channel": slot.channel,
        "anchor": slot.anchor,
        "due": _iso(slot.due),
        "skippable": slot.skippable,
        "state": slot.state,
        "rescheduled": slot.rescheduled,
        "orphan": slot.orphan,
        "draft_error": slot.draft_error,
        "title": item.title if item is not None else None,
        "tool": tool.name if tool is not None else None,
        # The link the starter text carries: the upload's, rebuilt from its id, or the tool's.
        "url": (
            item.with_url().url if item is not None else tool.url if tool is not None else None
        ),
        "has_draft": draft is not None,
        "draft_preview": draft.text[:PREVIEW] if draft is not None else None,
        "topic": draft.topic if draft is not None else None,
        "draft_by": draft.by if draft is not None else None,
        "posted": (
            {"at": _iso(record.at), "url": record.url, "via": record.via}
            if record is not None
            else None
        ),
    }
    if item is not None:
        # The thumbnail only exists for viewers once the video is public (2026-11-07).
        if _public_now(item, now):
            # `key` is the picture's identity: the card re-fetches when it changes.
            row["visual"] = {"kind": "thumbnail", "key": item.video_id}
        else:
            row["visual_note"] = "The thumbnail shows once the video is public."
    elif tool is not None and social_visual.tool_image_ok(tool.image):
        row["visual"] = {"kind": "tool", "key": tool.image}
    if draft is not None:
        # A fingerprint of the whole text: the preview stops at PREVIEW characters, so the
        # compact card's cached full draft is keyed on this and an edit past that is seen.
        row["draft_rev"] = hashlib.sha256(draft.text.encode("utf-8")).hexdigest()[:8]
    return row


def view(state: State, now: datetime) -> dict[str, Any]:
    """``GET /social``: under :data:`RESPONSE_CAP` characters; ``truncated`` flags a cut."""
    rules = state.rules
    first = _week_start(_week_of(now, rules) - timedelta(days=7), rules)
    last = now + timedelta(days=28)
    # Every open post still on the books (HISTORY_DAYS back) rides along, so an overdue one
    # older than last Monday is never dropped; posted and skipped ones keep the short window.
    oldest = now - timedelta(days=HISTORY_DAYS)
    window = sorted(
        (
            slot
            for slot in state.slots.values()
            if first <= slot.due <= last
            or (slot.state == "planned" and oldest <= slot.due < first)
        ),
        key=lambda s: s.due,
    )
    # Open posts first (soonest first), then the closed ones, newest first.
    ranked = [s for s in window if s.state == "planned"] + [
        s for s in reversed(window) if s.state != "planned"
    ]
    payload: dict[str, Any] = {
        "rules": rules.model_dump(mode="json"),
        "tools": [tool.model_dump(mode="json") for tool in state.tools],
        "uploads_meta": {
            ch: {
                "read_at": _iso(reading.read_at) if reading.read_at else None,
                "ok": reading.ok,
                "error": reading.error,
                "count": len(reading.items),
            }
            for ch, reading in state.uploads.items()
        },
        "gaps": gaps(state, now),
        "score": score(state, now),
        "slots": [],
        "slot_count": len(window),
        "truncated": True,
        "channel_ids": channel_ids(),
        "channel_names": channel_names(),
    }
    kept = _fill_slots(payload, (_slot_row(state, slot, now) for slot in ranked))
    kept.sort(key=lambda row: row["due"])
    payload["slots"] = kept
    payload["truncated"] = len(kept) < len(window)
    return payload


def _fill_slots(payload: dict[str, Any], rows: Any) -> list[dict[str, Any]]:
    """Rows from *rows*, in order, while the whole response stays under the cap less a margin."""
    used = len(json.dumps(payload, ensure_ascii=False))
    kept: list[dict[str, Any]] = []
    for row in rows:
        size = len(json.dumps(row, ensure_ascii=False)) + 2
        if used + size > RESPONSE_CAP - MARGIN:
            break
        kept.append(row)
        used += size
    return kept


def slot_detail(state: State, slot_id: str) -> dict[str, Any]:
    slot = state.slots.get(slot_id)
    if slot is None:
        raise SocialError(404, f"no social slot {slot_id!r}; reload the card and try again")
    row = _slot_row(state, slot, utcnow())
    draft = state.drafts.get(slot_id)
    row["draft"] = draft.model_dump(mode="json") if draft is not None else None
    return row


def visual_source(
    state: State, slot_id: str, now: datetime
) -> tuple[str, list[str], tuple[str, ...]]:
    """What the visual route fetches for a slot: a cache key, the links to try, the hosts allowed.

    :raises SocialError: 404 when the slot is unknown or has no image (an expert post, a video
        that is not public yet, a tool with no usable image link).
    """
    slot = state.slots.get(slot_id)
    if slot is None:
        raise SocialError(404, f"no social slot {slot_id!r}; reload the card and try again")
    row = _slot_row(state, slot, now)
    visual = row.get("visual")
    if visual is None:
        raise SocialError(404, row.get("visual_note") or "this post has no image")
    if visual["kind"] == "thumbnail":
        return (
            f"yt:{slot.anchor}",
            social_visual.thumbnail_urls(slot.anchor or ""),
            (social_visual.YOUTUBE_HOST,),
        )
    tool = next(t for t in state.tools if t.id == slot.anchor)
    return f"tool:{tool.image}", [tool.image or ""], social_visual.tool_image_hosts()


def week_context(state: State, now: datetime, *, redraft: bool = False) -> dict[str, Any]:
    """``GET /social/week``: open slots due within a week (overdue too) that lack a draft.

    Oldest due first, so overdue comes before soonest. Each row carries only what the drafter
    needs, and rows stop once the response would pass the cap (``truncated`` says so).

    With *redraft* (``GET /social/week-redraft``, 2026-11-07) an open slot whose draft is the
    drafter's own and unedited joins the list, flagged ``redraft: true``, so the drafter can
    write it again once the starter rules change; :func:`apply_drafts` still never touches a
    draft by Reid or an edited one.
    """
    end = now + timedelta(days=7)

    def stale_auto(slot: Slot) -> bool:
        draft = state.drafts.get(slot.id)
        return draft is not None and draft.by == "auto" and not draft.edited

    due = sorted(
        (
            slot
            for slot in state.slots.values()
            if slot.state == "planned"
            and slot.due <= end
            and (slot.id not in state.drafts or (redraft and stale_auto(slot)))
        ),
        key=lambda s: s.due,
    )

    def lean(slot: Slot) -> dict[str, Any]:
        item = _anchor_item(state, slot)
        tool = next((t for t in state.tools if t.id == slot.anchor), None)
        context: dict[str, Any] = {}
        if item is not None:
            context = {
                "title": item.title,
                "kind": item.kind,
                "privacy": item.privacy,
                "public_at": _iso(item.public_at) if item.public_at else None,
                "url": item.url,
                "channel": slot.channel,
            }
        elif slot.kind == "tool" and tool is not None:
            context = {"name": tool.name, "url": tool.url, "note": tool.note}
        row: dict[str, Any] = {
            "id": slot.id,
            "kind": slot.kind,
            "platform": slot.platform,
            "due": _iso(slot.due),
            "draft_error": slot.draft_error,
            "context": context,
        }
        if slot.id in state.drafts:
            row["redraft"] = True
        return row

    redrafting = {slot.id for slot in due if slot.id in state.drafts}
    topics = [
        draft.topic
        for sid, draft in state.drafts.items()
        if draft.topic
        and sid in state.slots
        and state.slots[sid].kind == "expert"
        # A slot being redrafted does not list its own old topic as one to avoid.
        and not (redraft and sid in redrafting)
    ]
    payload: dict[str, Any] = {
        "slots": [],
        "slot_count": len(due),
        "truncated": True,
        "recent_topics": topics[-12:],
    }
    kept = _fill_slots(payload, (lean(slot) for slot in due))
    payload["slots"] = kept
    payload["truncated"] = len(kept) < len(due)
    return payload


# ── mutations (each runs inside the store's lock on a copy of the state) ─────────────────────


class ChannelPost(BaseModel):
    ok: bool
    error: str | None = None
    items: list[Item] = Field(default_factory=list, max_length=MAX_ITEMS)

    @field_validator("error")
    @classmethod
    def _short(cls, value: str | None) -> str | None:
        return None if value is None else value[:200]


class UploadsPost(BaseModel):
    """``POST /v1/ae/social/uploads``: the lister's JSON; fields outside the contract drop."""

    model_config = ConfigDict(extra="ignore")

    read_at: datetime
    channels: dict[str, ChannelPost]

    @field_validator("channels")
    @classmethod
    def _known(cls, value: dict[str, ChannelPost]) -> dict[str, ChannelPost]:
        known = {ch: post for ch, post in value.items() if ch in CHANNELS}
        if not known:
            raise ValueError("the post names no channel; expected ae or hc")
        return known


def apply_uploads(state: State, body: UploadsPost, now: datetime | None = None) -> None:
    """Keep the reading; the first call also sets ``start_at`` (*now*, else the reading's time).

    A ``read_at`` in the future is clamped to *now*, so a skewed clock cannot hide staleness.
    """
    read_at = _as_utc(body.read_at)
    if now is not None:
        read_at = min(read_at, now)
    if state.start_at is None and any(post.ok for post in body.channels.values()):
        state.start_at = now or read_at
    for channel, post in body.channels.items():
        reading = state.uploads.setdefault(channel, ChannelReading())
        reading.read_at = read_at
        reading.ok = post.ok
        reading.error = None if post.ok else (post.error or "read failed")
        if post.ok:
            reading.items = sorted(
                (item.with_url() for item in post.items),
                key=lambda i: (
                    i.public_at is None,
                    i.public_at or datetime.min.replace(tzinfo=UTC),
                ),
            )


def _slot(state: State, slot_id: str) -> Slot:
    slot = state.slots.get(slot_id)
    if slot is None:
        raise SocialError(404, f"no social slot {slot_id!r}; reload the card and try again")
    return slot


def mark_posted(state: State, slot_id: str, url: str | None, now: datetime) -> None:
    slot = _slot(state, slot_id)
    if slot.state == "posted":
        return
    record = Posted(at=now, url=url)  # raises on a bad url
    slot.state = "posted"
    state.posted[slot_id] = record
    if slot.platform == "linkedin":
        key = _week_of(now, state.rules).isoformat()
        state.week_posted[key] = state.week_posted.get(key, 0) + 1


def reopen(state: State, slot_id: str) -> None:
    slot = _slot(state, slot_id)
    record = state.posted.pop(slot_id, None)
    if slot.state == "posted" and record is not None and slot.platform == "linkedin":
        key = _week_of(record.at, state.rules).isoformat()
        state.week_posted[key] = max(0, state.week_posted.get(key, 0) - 1)
    slot.state = "planned"


def skip(state: State, slot_id: str) -> None:
    slot = _slot(state, slot_id)
    if slot.state == "posted":
        raise SocialError(409, "that post is marked posted; reopen it before skipping")
    slot.state = "skipped"


def reschedule(state: State, slot_id: str, due: datetime, now: datetime) -> None:
    slot = _slot(state, slot_id)
    if slot.state != "planned":
        raise SocialError(409, "only a planned post can move; reopen it first")
    moved = _as_utc(due)
    if not now - timedelta(days=1) <= moved <= now + timedelta(days=120):
        raise SocialError(422, "pick a time from a day ago to 120 days ahead")
    slot.due = moved
    slot.rescheduled = True


def edit_draft(state: State, slot_id: str, text: str, topic: str | None, now: datetime) -> None:
    slot = _slot(state, slot_id)
    state.drafts[slot_id] = Draft(text=text, topic=topic, by="reid", at=now, edited=True)
    slot.draft_error = None


def add_expert(state: State, day: date, now: datetime) -> str:
    rules = state.rules
    today = _local_date(now, rules)
    if day < today or day > today + timedelta(days=120):
        raise SocialError(422, "pick a date from today to 120 days ahead")
    key = _week_key(_monday(day))
    sid = f"li-expert-{key}-{_free_expert_n(state, key, rules.expert_per_week + 1)}"
    state.slots[sid] = Slot(
        id=sid, kind="expert", platform="linkedin", due=_local_at(day, rules), manual=True
    )
    return sid


class DraftIn(BaseModel):
    id: str = Field(pattern=SLOT_ID)
    text: str = Field(min_length=1, max_length=MAX_DRAFT)
    topic: str | None = Field(default=None, max_length=MAX_TOPIC)
    audit_ok: bool | None = None


class DraftErrorIn(BaseModel):
    id: str = Field(pattern=SLOT_ID)
    error: str = Field(min_length=1, max_length=300)


def apply_drafts(
    state: State, drafts: list[DraftIn], errors: list[DraftErrorIn], now: datetime
) -> dict[str, list[str]]:
    """Write the weekly drafter's drafts; one by Reid, or edited, is never overwritten."""
    written: list[str] = []
    kept: list[str] = []
    for item in drafts:
        slot = state.slots.get(item.id)
        if slot is None or slot.state != "planned":
            kept.append(item.id)
            continue
        existing = state.drafts.get(item.id)
        if existing is not None and (existing.by == "reid" or existing.edited):
            kept.append(item.id)
            continue
        state.drafts[item.id] = Draft(
            text=item.text, topic=item.topic, by="auto", at=now, audit_ok=item.audit_ok
        )
        slot.draft_error = None
        written.append(item.id)
    flagged: list[str] = []
    for err in errors:
        slot = state.slots.get(err.id)
        if slot is not None and slot.id not in state.drafts:
            slot.draft_error = err.error
            flagged.append(err.id)
    return {"written": written, "kept": kept, "errors_set": flagged}


class RulesPatch(BaseModel):
    """``POST /v1/ae/social/rules``: any subset of the rules, validated as a whole."""

    model_config = ConfigDict(extra="forbid")

    video_li: bool | None = None
    video_community: bool | None = None
    short_li: bool | None = None
    short_li_days: int | None = None
    live_li: bool | None = None
    tool_every_days: int | None = None
    expert_per_week: int | None = None
    expert_weekday: int | None = None
    li_weekly_target: int | None = None
    gap_days: int | None = None
    alarm_hhmm: str | None = None
    tz: str | None = None


def apply_rules(state: State, patch: RulesPatch) -> None:
    merged = {**state.rules.model_dump(), **patch.model_dump(exclude_none=True)}
    state.rules = Rules.model_validate(merged)


class ToolsPost(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tools: list[Tool] = Field(max_length=12)

    @field_validator("tools")
    @classmethod
    def _unique(cls, value: list[Tool]) -> list[Tool]:
        ids = [tool.id for tool in value]
        if len(set(ids)) != len(ids):
            raise ValueError("tool ids must be unique")
        return value


# ── the store ─────────────────────────────────────────────────────────────────────────────────

T = TypeVar("T")


class SocialStore:
    """The state in a JSON file (memory only without a path); every change under one lock."""

    def __init__(self, path: Path | None = None) -> None:
        self._lock = threading.RLock()
        self._path = path
        self._memory = State()

    def _load(self) -> State:
        if self._path is None:
            return self._memory.model_copy(deep=True)
        try:
            text = self._path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return State()
        except OSError as exc:
            raise SocialSaveError(f"could not read {self._path}: {exc}") from exc
        try:
            return State.model_validate_json(text)
        except ValueError as exc:
            raise SocialSaveError(
                f"{self._path} is not a valid social file; fix or remove it"
            ) from exc

    def read(self) -> State:
        """The state; a file that cannot be read is an error, never a silent empty state."""
        with self._lock:
            return self._load()

    def mutate(self, change: Callable[[State], T], *, now: datetime | None = None) -> T:
        """Run *change* on the state, re-plan, save atomically; what *change* returned.

        :raises SocialError: When *change* refuses; nothing is saved.
        :raises SocialSaveError: When the file cannot be read or written.
        """
        with self._lock:
            state = self._load()
            result = change(state)
            state = plan(state, now or utcnow())
            if self._path is None:
                self._memory = state
                return result
            tmp: str | None = None
            try:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                fd, tmp = tempfile.mkstemp(prefix=".ae-social-", dir=str(self._path.parent))
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    handle.write(state.model_dump_json())
                os.replace(tmp, self._path)
            except OSError as exc:
                if tmp is not None:
                    with contextlib.suppress(OSError):
                        os.unlink(tmp)
                raise SocialSaveError(str(exc)) from exc
            return result


def _default_path() -> Path | None:
    raw = (os.environ.get(STATE_FILE_ENV) or "").strip()
    if raw:
        return Path(raw)
    if STATE_FILE_ENV in os.environ:
        return None
    if DATA_DIR.is_dir():
        return DATA_DIR / DATA_FILE_NAME
    return None


_store: SocialStore | None = None
_store_lock = threading.Lock()


def store() -> SocialStore:
    global _store
    with _store_lock:
        if _store is None:
            _store = SocialStore(_default_path())
        return _store


def reset_store(replacement: SocialStore | None = None) -> None:
    """Drop the process-wide store (tests change the environment between cases)."""
    global _store
    with _store_lock:
        _store = replacement


__all__ = [
    "CHANNELS",
    "CHANNEL_IDS_ENV",
    "RESPONSE_CAP",
    "STATE_FILE_ENV",
    "TOOLS_ENV",
    "ChannelPost",
    "Draft",
    "DraftErrorIn",
    "DraftIn",
    "Item",
    "Posted",
    "Rules",
    "RulesPatch",
    "Slot",
    "SocialError",
    "SocialSaveError",
    "SocialStore",
    "State",
    "Tool",
    "ToolsPost",
    "UploadsPost",
    "add_expert",
    "apply_drafts",
    "apply_rules",
    "apply_uploads",
    "channel_ids",
    "edit_draft",
    "gaps",
    "mark_posted",
    "plan",
    "reopen",
    "reschedule",
    "reset_store",
    "score",
    "skip",
    "slot_detail",
    "store",
    "utcnow",
    "view",
    "week_context",
]
