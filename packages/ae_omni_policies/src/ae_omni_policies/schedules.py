"""The two scheduled tasks, seeded by name through ``POST /v1/scheduled-tasks``.

Scheduled tasks are rows, not config: upstream creates them through its API
and the Tasks page (``web/src/pages/TasksPage.tsx``) lists them. So the
steward and the shutdown note are declared here as data and written by
:func:`seed`, which ``scripts/seed-schedules.py`` runs as
``just schedules-seed <server-url>``. Idempotent by name: list first, create
what is missing, patch what drifted, leave the rest alone (a task Reid paused
stays paused; ``state`` is never sent). Written 2026-09-23 (Phase 5).

Times decided 2026-09-23 (Reid): the steward on weekdays at 08:00 and the
shutdown note on weekdays at 17:30, both ``America/Los_Angeles``, as RRULEs in
upstream's grammar (``omnigent/server/scheduled/rrule.py``; the floor is one
hour, so daily rules are far inside it). Both agents are propose-only, so a
run that spends more than a couple of dollars is a run that went somewhere it
should not; ``max_cost_usd`` says so.

Where a run happens is resolved at fire time, not here: with no ``host_id``
the server picks the owner's online host and defaults the workspace to that
host's home directory, and records a failed run when no host is online. The
box host (``omnigent host``, handoff section 10) is what makes a fire real.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from ae_omni_policies.omni import SERVER_URL_ENV, TOKEN_ENV, Settings, http_client, stored_token

TIMEZONE = "America/Los_Angeles"
WEEKDAYS = "FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR"
# Per run. A brief costs cents; a cap of two dollars leaves room for a long
# transcript read and stops a loop before it matters.
MAX_COST_USD = 2.0
# The sidebar project each run session opens in (the task-project core patch,
# 2026-10-13), by name; the seeder resolves it through GET /v1/projects.
PROJECT = "Automation Notes"

STEWARD_PROMPT = (
    "Run the morning steward brief now. Read the lot and the slots with your tools, "
    "write the one report your instructions describe, and stop. Nobody is present: "
    "never ask, never park, never file."
)
SHUTDOWN_PROMPT = (
    "Run the end-of-day shutdown note now. For every working session, read its newest "
    "messages with your tools and draft the handoff note your instructions describe, "
    "then stop. Nobody is present: never ask, never change a status, never file."
)


@dataclass(frozen=True)
class Schedule:
    """One scheduled task as ``POST /v1/scheduled-tasks`` takes it, minus the agent id.

    :param name: The row's name; the idempotency key.
    :param agent: The built-in agent's name (the directory under ``agents/``).
    :param rrule: The recurrence in upstream's grammar.
    :param prompt: The user turn each fire sends; the rules live in the agent YAML.
    :param timezone: IANA zone the RRULE is evaluated in.
    :param max_cost_usd: The per-run spend cap.
    :param project: The project run sessions open in, by name, or ``None``.
    """

    name: str
    agent: str
    rrule: str
    prompt: str
    timezone: str = TIMEZONE
    max_cost_usd: float = MAX_COST_USD
    project: str | None = PROJECT

    def body(self, agent_id: str) -> dict[str, Any]:
        """The create body for this schedule bound to *agent_id*."""
        return {
            "name": self.name,
            "prompt": self.prompt,
            "rrule": self.rrule,
            "agent_id": agent_id,
            "timezone": self.timezone,
            "max_cost_usd": self.max_cost_usd,
        }


STEWARD = Schedule(
    name="AE steward",
    agent="steward",
    rrule=f"{WEEKDAYS};BYHOUR=8;BYMINUTE=0",
    prompt=STEWARD_PROMPT,
)
SHUTDOWN = Schedule(
    name="AE shutdown note",
    agent="shutdown",
    rrule=f"{WEEKDAYS};BYHOUR=17;BYMINUTE=30",
    prompt=SHUTDOWN_PROMPT,
)
SCHEDULES: tuple[Schedule, ...] = (STEWARD, SHUTDOWN)

# The orchestrator's check-in (2026-09-25): one row, by name, that the
# orchestrator agent writes itself through the ``schedule_checkin`` tool, not
# the seeder: how often depends on the work in flight. Upstream's floor is one
# hour and a rule must fire at least twice, so a check-in is hourly or slower
# and a one-off "in 20 minutes" is not possible; every fire starts a fresh
# orchestrator session, which hands the check-in to the orchestrator session
# Reid is talking to when there is one, so its context carries over.
CHECKIN_NAME = "AE orchestrator check-in"
CHECKIN_AGENT = "orchestrator"
MAX_CHECKIN_HOURS = 24


def checkin_rrule(every_hours: int, until: date | None = None, tz: str = TIMEZONE) -> str:
    """``FREQ=HOURLY;INTERVAL=n``, ending after *until* (a local date) when given.

    RFC 5545 wants ``UNTIL`` in UTC when the start is zoned, so the last
    minute of *until* in *tz* is converted.

    :raises ValueError: When *every_hours* is outside one to :data:`MAX_CHECKIN_HOURS`.
    """
    if not 1 <= every_hours <= MAX_CHECKIN_HOURS:
        raise ValueError(f"every_hours must be 1 to {MAX_CHECKIN_HOURS}; got {every_hours}")
    rule = f"FREQ=HOURLY;INTERVAL={every_hours}"
    if until is not None:
        end = datetime.combine(until, time(23, 59, 59), ZoneInfo(tz)).astimezone(UTC)
        rule += f";UNTIL={end.strftime('%Y%m%dT%H%M%SZ')}"
    return rule


def checkin_prompt(session_id: str | None, note: str | None) -> str:
    """What each check-in fire sends the fresh orchestrator session."""
    lines = ["Scheduled orchestrator check-in."]
    if note:
        lines.append(f"Focus: {' '.join(note.split())}")
    if session_id:
        lines.append(
            f"Hand it to the orchestrator session Reid talks to: call post_message once "
            f'with session_id {session_id} and the text "Check-in: run your check-in now." '
            "Then stop. Only if that call fails, run the check-in yourself as your "
            "instructions describe."
        )
    else:
        lines.append("Run the check-in your instructions describe, then stop.")
    return " ".join(lines)


def checkin_schedule(
    every_hours: int,
    *,
    until: date | None = None,
    session_id: str | None = None,
    note: str | None = None,
) -> Schedule:
    """The check-in row, in the same shape the seeder writes."""
    return Schedule(
        name=CHECKIN_NAME,
        agent=CHECKIN_AGENT,
        rrule=checkin_rrule(every_hours, until),
        prompt=checkin_prompt(session_id, note),
    )


# The fields a patch compares. ``state``, ``host_id`` and ``workspace`` are
# left to the person: pausing a task or pinning it to a host is theirs. So is
# ``keep_runs`` (patch P29, 2026-10-13): a task never given one keeps its
# newest five run sessions, and a reseed must not undo a change Reid made.
MANAGED_FIELDS = ("prompt", "rrule", "agent_id", "timezone", "max_cost_usd")


class SeedError(Exception):
    """A seed could not finish; the message says which call failed and why."""


def _failure(what: str, response: httpx.Response) -> SeedError:
    text = " ".join(response.text.split())[:300]
    return SeedError(f"{what}: {response.status_code} {text}")


async def agents_by_name(http: httpx.AsyncClient) -> dict[str, str]:
    """Every built-in agent's id by name, through ``GET /v1/agents``."""
    found: dict[str, str] = {}
    after: str | None = None
    for _ in range(100):
        params: dict[str, Any] = {"limit": 1000, "order": "asc"}
        if after:
            params["after"] = after
        response = await http.get("/v1/agents", params=params)
        if response.status_code != 200:
            raise _failure("listing the agents", response)
        page = response.json()
        for agent in page.get("data") or []:
            if isinstance(agent, dict) and agent.get("name") and agent.get("id"):
                found[str(agent["name"])] = str(agent["id"])
        if not page.get("has_more") or not page.get("last_id"):
            break
        after = str(page["last_id"])
    return found


async def list_scheduled(http: httpx.AsyncClient) -> dict[str, dict[str, Any]]:
    """The caller's scheduled tasks by name, through ``GET /v1/scheduled-tasks``."""
    response = await http.get("/v1/scheduled-tasks")
    if response.status_code != 200:
        raise _failure("listing the scheduled tasks", response)
    rows = response.json().get("scheduled_tasks") or []
    return {str(row["name"]): row for row in rows if isinstance(row, dict) and row.get("name")}


async def projects_by_name(http: httpx.AsyncClient) -> dict[str, str]:
    """The caller's projects' ids by name (case-folded), or ``{}`` without the projects API."""
    response = await http.get("/v1/projects")
    if response.status_code == 404:
        return {}
    if response.status_code != 200:
        raise _failure("listing the projects", response)
    return {
        str(row["name"]).strip().casefold(): str(row["id"])
        for row in response.json().get("data") or []
        if isinstance(row, dict) and row.get("name") and row.get("id")
    }


async def seed(
    http: httpx.AsyncClient, schedules: tuple[Schedule, ...] = SCHEDULES
) -> list[dict[str, Any]]:
    """Create or update *schedules* on the server *http* points at.

    :param http: A client for the server, identity headers set.
    :param schedules: What to ensure; the two shipped ones by default.
    :returns: One ``{"name", "id", "action", "agent_id"}`` per schedule, ``action``
        being ``created``, ``updated`` or ``unchanged``.
    :raises SeedError: When an agent is not registered or a call fails. Earlier
        schedules in the tuple are already written by then; a rerun finishes.
    """
    agents = await agents_by_name(http)
    existing = await list_scheduled(http)
    projects = await projects_by_name(http)
    results: list[dict[str, Any]] = []
    for schedule in schedules:
        agent_id = agents.get(schedule.agent)
        if agent_id is None:
            raise SeedError(
                f"agent {schedule.agent!r} is not registered on the server. Add its directory "
                f"to OMNIGENT_BUILTIN_AGENT_DIRS (config/server.yaml explains) and restart."
            )
        body = schedule.body(agent_id)
        project_id = (
            projects.get(schedule.project.strip().casefold()) if schedule.project else None
        )
        row = existing.get(schedule.name)
        if row is None:
            if project_id is not None:
                body["project_id"] = project_id
            response = await http.post("/v1/scheduled-tasks", json=body)
            if response.status_code != 200:
                raise _failure(f"creating {schedule.name!r}", response)
            results.append(
                {
                    "name": schedule.name,
                    "id": response.json()["id"],
                    "action": "created",
                    "agent_id": agent_id,
                }
            )
            continue
        drift = {key: body[key] for key in MANAGED_FIELDS if row.get(key) != body[key]}
        # Fill a missing project, never replace one: moving a task to another
        # project is Reid's call and a reseed must not undo it. A server without
        # the task-project patch has no project_id key and is left alone.
        if project_id is not None and "project_id" in row and row["project_id"] is None:
            drift["project_id"] = project_id
        if not drift:
            results.append(
                {
                    "name": schedule.name,
                    "id": row["id"],
                    "action": "unchanged",
                    "agent_id": agent_id,
                }
            )
            continue
        response = await http.patch(f"/v1/scheduled-tasks/{row['id']}", json=drift)
        if response.status_code != 200:
            raise _failure(f"updating {schedule.name!r}", response)
        results.append(
            {
                "name": schedule.name,
                "id": row["id"],
                "action": "updated",
                "agent_id": agent_id,
                "changed": sorted(drift),
            }
        )
    return results


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="seed-schedules",
        description=(
            "Create or update the AE steward and shutdown-note scheduled tasks on an "
            "OmniGent server, by name. Identity: the token `omnigent login <url>` stored, "
            f"or {TOKEN_ENV}."
        ),
    )
    parser.add_argument(
        "server_url",
        nargs="?",
        default=os.environ.get(SERVER_URL_ENV, ""),
        help=f"The server, e.g. https://ae-llm-01.example.ts.net:8000 (default: {SERVER_URL_ENV})",
    )
    parser.add_argument("--json", action="store_true", help="print the result as JSON")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """The script's entry point; prints one line per schedule, 1 on any failure."""
    return asyncio.run(amain(argv))


async def amain(argv: list[str] | None = None) -> int:
    """`:func:`main` without the event loop, for a caller that already runs one."""
    args = _parse_args(argv)
    url = str(args.server_url).strip().rstrip("/")
    if not url:
        print(f"seed-schedules: give the server URL, or set {SERVER_URL_ENV}.", file=sys.stderr)
        return 2
    token = (os.environ.get(TOKEN_ENV) or "").strip() or stored_token(url)
    if token is None:
        print(
            f"seed-schedules: no stored login for {url} and {TOKEN_ENV} is unset; proceeding "
            "without a token, which only a single-user server accepts.",
            file=sys.stderr,
        )
    settings = Settings(server_url=url, token=token)
    try:
        async with http_client(settings) as http:
            results = await seed(http)
    except (SeedError, httpx.HTTPError) as exc:
        print(f"seed-schedules: {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(results, indent=2))
    else:
        for row in results:
            changed = f" ({', '.join(row['changed'])})" if row.get("changed") else ""
            print(f"{row['action']:<9} {row['name']}  id={row['id']}{changed}")
    return 0


__all__ = [
    "CHECKIN_AGENT",
    "CHECKIN_NAME",
    "MANAGED_FIELDS",
    "MAX_CHECKIN_HOURS",
    "MAX_COST_USD",
    "PROJECT",
    "SCHEDULES",
    "SHUTDOWN",
    "STEWARD",
    "TIMEZONE",
    "Schedule",
    "SeedError",
    "agents_by_name",
    "amain",
    "checkin_prompt",
    "checkin_rrule",
    "checkin_schedule",
    "list_scheduled",
    "main",
    "projects_by_name",
    "seed",
]
