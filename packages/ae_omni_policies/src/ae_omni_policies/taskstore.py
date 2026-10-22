"""task-store over Cloudflare Access, ported from the worker's ``TaskStore`` class.

Source: ``task-store-worker/src/index.ts`` in analyticendeavors/ae-claude-skills.
The worker (a Cloudflare Worker behind Microsoft OAuth) is how chat surfaces
reach Reid's task app; this client is how the OmniGent server reaches it, with
the same outbound credential: a Cloudflare Access service token in
``CF-Access-Client-Id`` / ``CF-Access-Client-Secret``. The store answers as the
machine principal ``service:<client-id>.access``, which may GET, POST and PATCH
and is refused DELETE. Nothing here needs DELETE.

Behaviour carried over, because each item was a silent wrong answer when it was
learned (worker README, "Things the code knows"):

- a 3xx is an auth failure (Access redirects an unauthenticated call to its
  login page; following it would read as a 200 of HTML);
- a 403 carrying Cloudflare's ``error code: 1010`` is bot protection, not the
  store, and the fix is the User-Agent, not the allowlist;
- one retry on 5xx, never on 4xx; a transport retry only where a second attempt
  cannot double-write (every GET, and ``POST /buckets``);
- the Parking Lot list must have ``surface: "parking"`` or every parked task
  lands on Reid's phone as a reminder.

Two places deliberately differ from the worker: the 1010 check runs before the
JSON parse (the worker's own copy sits after it and can never fire on the HTML
page), and ``POST /tasks`` is never resent blind, not even on a 5xx (a 502 or
504 from the edge can follow a committed insert); it is re-read by title and
note instead.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from typing import Any, Literal
from urllib.parse import quote_plus
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx

from ae_omni_policies import __version__
from ae_omni_policies.notes import (
    DEFAULT_TZ,
    PARKING_LOT_NAME,
    PARKING_SURFACE,
    QUADRANT_AXES,
    RESURFACE_DAYS,
    NoteFields,
    build_notes,
    estimate_for,
    is_real_date,
    next_from_notes,
    normalize_tags,
    repo_slug,
    resurface_date,
    summary_for,
)

_logger = logging.getLogger(__name__)

# No default origin (2026-10-22): an unset AE_TASK_STORE_URL means
# task-store is off, so a server never calls Analytic Endeavors' own app.
DEFAULT_APP_URL = ""
REQUEST_TIMEOUT_S = 10.0
# Distinct from the worker's "fork-triage" so a task's origin says which door
# filed it. The re-read after a failed create matches on it.
ORIGIN_SURFACE = "omnigent-ae"
# tasks.go-ae.org sits behind bot protection that answers some client agents
# with error 1010 before Access sees the token. Naming ourselves keeps the
# request out of that bucket and makes the origin's logs say who called.
USER_AGENT = f"omnigent-ae/{__version__} (+https://github.com/analyticendeavors/omnigent-ae)"

REDIRECT_ERROR = (
    "Cloudflare Access redirected to its login page, so it did not accept the service "
    "token. Check AE_CF_ACCESS_CLIENT_ID / AE_CF_ACCESS_CLIENT_SECRET and that the Service "
    "Auth policy is still attached to the AE Task Store app."
)
BOT_PROTECTION_ERROR = (
    "Cloudflare bot protection rejected the request (error 1010) before Access saw it. "
    "The service token is not the problem; the User-Agent is."
)
UNFILED_INSTRUCTION = (
    "Report this as UNFILED (target unreachable), file this yourself, with the note above "
    "verbatim."
)
URL_IS = "the Parking Lot list, searched down to this task (the app has no per-task route)"
# The Tabler outline icon set the app paints user-chosen tag icons from
# (webapp/static/tabler.js in agents-and-automation home-server/task-store):
# name -> [path data], about 1 MB, served as a static file next to /api. Read
# once and kept, because a tag's icon is a name and only the app has the set.
ICON_SET_PATH = "/tabler-icons.json"
ICON_SET_TTL_S = 6 * 60 * 60
# A failed read is not retried on every 20 s Dashboard refresh.
ICON_SET_RETRY_S = 10 * 60
# Cloudflare's 1010 page: an HTML 403 that says "error code: 1010". The bare
# substring would also match a task id or a message from the store itself.
_BOT_PROTECTION = re.compile(r"error code:\s*1010", re.IGNORECASE)
# A GitHub issue page, nothing after the number: a pull request (/pull/) or
# another host never matches.
_ISSUE_URL = re.compile(r"https://github\.com/[A-Za-z0-9-]+/[A-Za-z0-9._-]+/issues/[0-9]+/?")

FailureKind = Literal["auth", "client", "server", "transport", "parse"]

# (read at, the set or None when the read failed); module-wide, like the store.
_icon_set: tuple[float, dict[str, list[str]] | None] | None = None


@dataclass(frozen=True)
class Settings:
    """Where the store is and how to get in. All from the environment; never in the repo.

    :param app_url: The app origin, e.g. ``https://tasks.go-ae.org``.
    :param client_id: Cloudflare Access service-token client id.
    :param client_secret: Its secret.
    :param tz: IANA zone the resurface dates are reckoned in.
    """

    app_url: str = DEFAULT_APP_URL
    client_id: str = ""
    client_secret: str = ""
    tz: str = DEFAULT_TZ

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> Settings:
        """Read the ``AE_TASK_STORE_*`` and ``AE_CF_ACCESS_*`` variables.

        :param environ: The mapping to read; ``os.environ`` when omitted.
        :returns: The settings, with the app URL's trailing slash dropped and a
            zone that ``zoneinfo`` knows (a typo falls back to the default, logged).
        """
        env = os.environ if environ is None else environ
        tz = env.get("AE_TASK_STORE_TZ") or DEFAULT_TZ
        try:
            ZoneInfo(tz)
        except (ZoneInfoNotFoundError, ValueError):
            # A typo here must not turn every park into a 500 with no note.
            _logger.warning("AE_TASK_STORE_TZ=%r is not an IANA zone; using %s", tz, DEFAULT_TZ)
            tz = DEFAULT_TZ
        return cls(
            app_url=(env.get("AE_TASK_STORE_URL") or DEFAULT_APP_URL).rstrip("/"),
            client_id=env.get("AE_CF_ACCESS_CLIENT_ID", ""),
            client_secret=env.get("AE_CF_ACCESS_CLIENT_SECRET", ""),
            tz=tz,
        )

    @property
    def configured(self) -> bool:
        """Whether the app's origin and both halves of the service token are present."""
        return bool(self.app_url and self.client_id and self.client_secret)


@dataclass(frozen=True)
class Result:
    """One call's outcome. ``kind`` names the failure the way the worker does.

    :param ok: Whether the call succeeded (2xx with parseable JSON or an empty body).
    :param status: HTTP status, ``0`` when the request never arrived.
    :param data: The parsed JSON body on success.
    :param kind: ``auth`` (Access refused), ``client`` (4xx), ``server`` (5xx),
        ``transport`` (never arrived), ``parse`` (2xx but not JSON).
    :param error: Human-readable failure text.
    """

    ok: bool
    status: int
    data: Any = None
    kind: FailureKind | None = None
    error: str = ""


class TaskStoreError(Exception):
    """A read the caller cannot proceed without failed; carries the :class:`Result`."""

    def __init__(self, result: Result, what: str) -> None:
        super().__init__(f"Could not {what}: {result.error}")
        self.result = result


@dataclass(frozen=True)
class ParkFields:
    """``park_fork``'s inputs, the note lines plus the optional extras.

    Field names follow the worker's tool schema; ``from_`` is ``from`` there.
    """

    thread: str
    state: str
    next: str
    slot: str
    quadrant: str
    est: str
    from_: str
    summary: str | None = None
    checklist: list[str] | None = None
    repo: str | None = None
    tags: list[str] | None = None
    due_date: str | None = None
    issue_url: str | None = None


def form_component(value: str) -> str:
    """Encode like ``URLSearchParams``: ``+`` for spaces, ``*-._`` and alphanumerics bare."""
    return quote_plus(value, safe="*").replace("~", "%7E")


def view_url(app_url: str, title: str, bucket_id: str | None) -> str:
    """The Parking Lot scoped to a search on the task's own title.

    The app has no per-task route, so this is the closest honest thing. Built
    from what the store returned, never from a guessed id.
    """
    pairs: list[tuple[str, str]] = []
    if bucket_id:
        pairs.append(("list", bucket_id))
    pairs.append(("q", title))
    query = "&".join(f"{form_component(k)}={form_component(v)}" for k, v in pairs)
    return f"{app_url}/#/all?{query}"


def repo_of(notes_text: str | None, links: list[Any] | None = None) -> str | None:
    """The repository a parked task belongs to, as ``owner/name``, or ``None``.

    The note's ``REPO:`` line first (every note filed with a repo carries one;
    task-store has no repo field of its own), then the first GitHub link on the
    task (route C links the issue it opened). Added 2026-09-25 so a lot card can
    show its repository without expanding (issue 28).
    """
    for line in (notes_text or "").split("\n"):
        if line.startswith("REPO: "):
            slug = repo_slug(line[len("REPO: ") :])
            if slug:
                return slug
    for link in links or []:
        url = link.get("url") if isinstance(link, dict) else None
        if isinstance(url, str) and "github.com/" in url.lower():
            slug = repo_slug(url)
            if slug:
                return slug
    return None


def issue_url_of(links: list[Any] | None) -> str | None:
    """The first link on a task that is a GitHub issue URL, trailing slash dropped, or ``None``.

    Route C links the issue it opened as a task link (``link_issue``). Added
    2026-09-24 so Resume can tell the session to close that issue from its pull
    request; a pull request link, or an issue on any other host, is not one.
    """
    for link in links or []:
        url = link.get("url") if isinstance(link, dict) else None
        if isinstance(url, str) and _ISSUE_URL.fullmatch(url.strip()):
            return url.strip().rstrip("/")
    return None


def find_parking_lot(buckets: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The bucket named Parking Lot on the ``parking`` surface, or ``None``."""
    for bucket in buckets:
        if not isinstance(bucket, dict):
            continue
        if (
            bucket.get("surface") == PARKING_SURFACE
            and str(bucket.get("name") or "").lower() == PARKING_LOT_NAME.lower()
        ):
            return bucket
    return None


def reset_icon_set() -> None:
    """For tests: forget the icon set :meth:`TaskStoreClient.icon_set` kept."""
    global _icon_set
    _icon_set = None


def _style(value: Any) -> str | None:
    """A tag's colour or icon name as the store keeps it, or ``None`` when unset."""
    return value.strip() if isinstance(value, str) and value.strip() else None


def _describe(exc: Exception) -> str:
    if isinstance(exc, httpx.TimeoutException):
        return f"no answer within {REQUEST_TIMEOUT_S:g}s"
    return str(exc) or exc.__class__.__name__


def _snippet(text: str) -> str:
    flat = " ".join(text.split())
    return f"{flat[:200]}..." if len(flat) > 200 else flat


class TaskStoreClient:
    """Async client for the store's ``/api`` surface. Use as ``async with``.

    :param settings: Where the store is and the service token.
    :param http: An ``httpx.AsyncClient`` to reuse; one is created (and closed
        on exit) when omitted.
    """

    def __init__(self, settings: Settings, *, http: httpx.AsyncClient | None = None) -> None:
        self.settings = settings
        self._http = http
        self._owns_http = http is None

    async def __aenter__(self) -> TaskStoreClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        """Close the owned HTTP client, if any."""
        if self._owns_http and self._http is not None:
            await self._http.aclose()
            self._http = None

    def _client(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(
                timeout=httpx.Timeout(REQUEST_TIMEOUT_S),
                # Access answers an unauthenticated call with a 302 to its login
                # page; following it would turn a broken token into a 200 of HTML.
                follow_redirects=False,
            )
        return self._http

    async def _once(self, method: str, path: str, body: Any | None = None) -> Result:
        headers = {
            "CF-Access-Client-Id": self.settings.client_id,
            "CF-Access-Client-Secret": self.settings.client_secret,
            "accept": "application/json",
            "user-agent": USER_AGENT,
        }
        content = None
        if body is not None:
            headers["content-type"] = "application/json"
            content = json.dumps(body)
        try:
            response = await self._client().request(
                method, f"{self.settings.app_url}/api{path}", headers=headers, content=content
            )
        except httpx.HTTPError as exc:
            return Result(ok=False, status=0, kind="transport", error=_describe(exc))

        status = response.status_code
        if 300 <= status < 400:
            return Result(ok=False, status=status, kind="auth", error=REDIRECT_ERROR)

        text = response.text
        if status == 403 and _BOT_PROTECTION.search(text):
            # Cloudflare's own 1010 arrives as a 403 that looks like the store
            # refusing the principal. It is not: the request never reached the
            # origin. Checked before the JSON parse, where it can fire.
            return Result(ok=False, status=403, kind="auth", error=BOT_PROTECTION_ERROR)
        data: Any = None
        if text.strip():
            try:
                data = json.loads(text)
            except ValueError:
                kind: FailureKind = (
                    "parse" if response.is_success else ("server" if status >= 500 else "client")
                )
                return Result(
                    ok=False,
                    status=status,
                    kind=kind,
                    error=f"{status} did not answer JSON: {_snippet(text)}",
                )
        if not response.is_success:
            message = data.get("error") if isinstance(data, dict) else None
            return Result(
                ok=False,
                status=status,
                kind="server" if status >= 500 else "client",
                error=f"{status} {message}" if message else f"{status} {_snippet(text)}",
            )
        return Result(ok=True, status=status, data=data)

    async def _request(
        self,
        method: str,
        path: str,
        body: Any | None = None,
        *,
        retry_transport: bool = False,
    ) -> Result:
        """One retry on 5xx, never on 4xx; a transport retry only when *retry_transport*."""
        first = await self._once(method, path, body)
        if first.ok:
            return first
        again = first.kind == "server" or (first.kind == "transport" and retry_transport)
        return await self._once(method, path, body) if again else first

    # ── the store's surface ──────────────────────────────────────────────

    async def list_buckets(self) -> Result:
        return await self._request("GET", "/buckets", retry_transport=True)

    async def create_parking_bucket(self, name: str = PARKING_LOT_NAME) -> Result:
        # Idempotent on the store side (ensure_bucket returns the existing row).
        return await self._request(
            "POST", "/buckets", {"name": name, "surface": PARKING_SURFACE}, retry_transport=True
        )

    async def create_task(self, fields: dict[str, Any]) -> Result:
        """One attempt, never retried blind: a create may have landed whatever the answer.

        The worker resends on 5xx; a 502 or 504 from the edge after the origin
        committed would then park the fork twice. :meth:`park` re-reads instead.
        """
        return await self._once("POST", "/tasks", fields)

    async def list_tasks(self, status: str = "open") -> Result:
        return await self._request(
            "GET", f"/tasks?status={form_component(status)}", retry_transport=True
        )

    async def get_task(self, task_id: str) -> Result:
        return await self._request(
            "GET", f"/tasks/{form_component(task_id)}", retry_transport=True
        )

    async def list_tags(self) -> Result:
        return await self._request("GET", "/tags", retry_transport=True)

    async def icon_set(self, *, now: float | None = None) -> dict[str, list[str]] | None:
        """The app's Tabler icon set, read once per :data:`ICON_SET_TTL_S` and kept.

        A static file, not an API route, so it is read here rather than through
        :meth:`_request`, with the same service token. ``None`` when it cannot
        be read; a failure is remembered for :data:`ICON_SET_RETRY_S`, so the
        tags still render (colour, no icon) and the store is not asked for a
        megabyte on every refresh.
        """
        global _icon_set
        at = time.monotonic() if now is None else now
        if _icon_set is not None:
            read_at, kept = _icon_set
            if at - read_at < (ICON_SET_TTL_S if kept is not None else ICON_SET_RETRY_S):
                return kept
        data: dict[str, list[str]] | None = None
        try:
            response = await self._client().get(
                f"{self.settings.app_url}{ICON_SET_PATH}",
                headers={
                    "CF-Access-Client-Id": self.settings.client_id,
                    "CF-Access-Client-Secret": self.settings.client_secret,
                    "accept": "application/json",
                    "user-agent": USER_AGENT,
                },
            )
            parsed = response.json() if response.status_code == 200 else None
            if isinstance(parsed, dict):
                data = {
                    str(name): [d for d in paths if isinstance(d, str)]
                    for name, paths in parsed.items()
                    if isinstance(paths, list)
                }
            else:
                _logger.warning("task-store icon set answered %s", response.status_code)
        except (httpx.HTTPError, ValueError) as exc:
            _logger.warning("task-store icon set unreadable: %s", _describe(exc))
        _icon_set = (at, data)
        return data

    async def patch_task(self, task_id: str, fields: dict[str, Any]) -> Result:
        return await self._request("PATCH", f"/tasks/{form_component(task_id)}", fields)

    async def add_link(self, task_id: str, url: str, label: str) -> Result:
        return await self._request(
            "POST",
            f"/tasks/{form_component(task_id)}/links",
            {"kind": "url", "url": url, "label": label},
        )

    def view_url(self, title: str, bucket_id: str | None) -> str:
        return view_url(self.settings.app_url, title, bucket_id)

    # ── the worker's tools ───────────────────────────────────────────────

    async def park(self, fields: ParkFields, *, today: date | None = None) -> dict[str, Any]:
        """File a parked fork: the worker's ``park_fork`` handler, step for step.

        1. Resolve the lot (create it with ``surface: parking`` when absent; a
           store that cannot answer is a warning, not a lost fork).
        2. Create the task from the note. No priority, ever.
        3. Stamp the note with its own address (a second write; the store mints
           the id), so the resume block carries where the task lives.
        4. Attach the issue link last: a missing link is a note away from fixed,
           a missing task is the fork gone.

        :param fields: The note lines and extras.
        :param today: Override for the resurface date's "today" (tests).
        :returns: ``{"outcome": "filed", ...}`` with the task id and the app URL,
            or ``{"outcome": "unfiled", "reason", "note", "instruction"}``.
        """
        warnings: list[str] = []

        bucket: dict[str, Any] | None = None
        buckets = await self.list_buckets()
        if not buckets.ok:
            warnings.append(f"Could not read the lists ({buckets.error}); filed with no list.")
        else:
            payload = buckets.data if isinstance(buckets.data, dict) else {}
            bucket = find_parking_lot(payload.get("buckets") or [])
            if bucket is None:
                made = await self.create_parking_bucket()
                if made.ok and isinstance(made.data, dict):
                    bucket = made.data.get("bucket")
                else:
                    warnings.append(
                        f"Could not create the {PARKING_LOT_NAME} list ({made.error}); "
                        "filed with no list."
                    )

        repo = repo_slug(fields.repo)
        if fields.repo and not repo:
            warnings.append(
                f'Ignored repo "{fields.repo}"; it is not owner/name or a GitHub URL. '
                "The note carries no repository link."
            )
        note = NoteFields(
            summary=summary_for(fields.summary, fields.state),
            thread=fields.thread,
            repo=repo,
            state=fields.state,
            next=fields.next,
            slot=fields.slot,
            quadrant=fields.quadrant,
            est=fields.est,
            from_=fields.from_,
        )
        axes = QUADRANT_AXES[note.quadrant]

        asked = (fields.due_date or "").strip().lower()
        due: str | None
        if asked == "none":
            due = None
        elif asked and is_real_date(asked):
            due = asked
        else:
            # The store parse-checks due_at and answers 400, which is a client
            # error never retried; a typed date would report the fork UNFILED
            # while the store was healthy. Fall back and say so.
            if asked:
                warnings.append(
                    f'Ignored due_date "{fields.due_date}"; it is not a real YYYY-MM-DD date or '
                    "'none'. Used the quadrant default instead."
                )
            due = resurface_date(self.settings.tz, RESURFACE_DAYS[note.quadrant], today=today)

        notes_text = build_notes(note, fields.checklist)
        body: dict[str, Any] = {
            "title": fields.thread,
            "notes": notes_text,
            "kind": "task",
            "status": "open",
            "urgent": axes["urgent"],
            "important": axes["important"],
            "estimate": estimate_for(fields.est),
            "tags": normalize_tags(fields.tags),
            "origin_surface": ORIGIN_SURFACE,
            "bucket_id": bucket.get("id") if bucket else None,
            # Midnight UTC of the calendar date, all-day: the one shape that
            # survives the app's round trip.
            "due_at": f"{due}T00:00:00Z" if due else None,
            "due_is_all_day": 1 if due else 0,
        }

        created = await self.create_task(body)
        if not created.ok and created.kind in ("transport", "server"):
            # The POST may well have landed: a timeout, or a 5xx from the edge
            # after the origin committed. Re-read rather than send it again:
            # there is no idempotency key, and two parked copies of one fork is
            # worse than one report of an unfiled one.
            match = await self._find_created(fields.thread, notes_text)
            if match is not None:
                warnings.append(
                    f"The create call failed ({created.error}) but the task was there on "
                    "re-read; not sent twice."
                )
                created = Result(ok=True, status=201, data=match)
            elif created.kind == "server":
                # Nothing landed, so one more attempt cannot double-write.
                created = await self.create_task(body)
        unfiled: dict[str, Any] = {
            "outcome": "unfiled",
            "note": notes_text,
            "instruction": UNFILED_INSTRUCTION,
        }
        if not created.ok:
            return {**unfiled, "reason": created.error}

        task = created.data if isinstance(created.data, dict) else {}
        task_id = task.get("id")
        if not isinstance(task_id, str) or not task_id:
            return {**unfiled, "reason": f"the store answered {created.status} without a task id"}
        url = self.view_url(
            str(task.get("title") or fields.thread), bucket.get("id") if bucket else None
        )

        stamped = await self.patch_task(
            task_id, {"notes": build_notes(note, fields.checklist, url)}
        )
        if not stamped.ok:
            warnings.append(
                f"The note is missing its TASK: link ({stamped.error}). Everything else is filed."
            )

        issue_link: str | None = None
        if fields.issue_url:
            linked = await self.add_link(task_id, fields.issue_url, "GitHub issue")
            if linked.ok:
                issue_link = fields.issue_url
            else:
                warnings.append(
                    f"The task was filed but the issue link did not attach ({linked.error}). "
                    f"Give Reid the issue URL inline: {fields.issue_url}"
                )

        return {
            "outcome": "filed",
            "task_id": task_id,
            "title": task.get("title"),
            "url": url,
            "url_is": URL_IS,
            "list": (
                {
                    "id": bucket.get("id"),
                    "name": bucket.get("name"),
                    "surface": bucket.get("surface"),
                }
                if bucket
                else None
            ),
            "quadrant": task.get("quadrant"),
            "estimate": task.get("estimate"),
            "tags": task.get("tags"),
            "resurfaces": due,
            "issue_link": issue_link,
            "warnings": warnings,
        }

    async def _find_created(self, title: str, notes: str) -> dict[str, Any] | None:
        """The task a failed create may have left behind: same title, same note, ours, newest.

        The whole note is matched, not the title alone: a fork parked again under
        a THREAD name already in the lot must not claim (and re-stamp) the older
        task.
        """
        recheck = await self.list_tasks("open")
        if not recheck.ok or not isinstance(recheck.data, dict):
            return None
        candidates = [
            task
            for task in recheck.data.get("tasks") or []
            if isinstance(task, dict)
            and task.get("title") == title
            and task.get("origin_surface") == ORIGIN_SURFACE
            and task.get("notes") == notes
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda task: str(task.get("created_at") or ""))

    async def list_parked(self, status: str = "open", limit: int = 20) -> dict[str, Any]:
        """Read the lot: the worker's ``list_parked``, same fields per task.

        :param status: Task status to list; ``open`` is what a parked fork is.
        :param limit: How many to return.
        :returns: ``{"list", "status", "count", "tasks"}``; ``list`` is ``None``
            with a ``note`` when no Parking Lot list exists yet.
        :raises TaskStoreError: When the lists or the tasks cannot be read.
        """
        buckets = await self.list_buckets()
        if not buckets.ok:
            raise TaskStoreError(buckets, "read the lists")
        payload = buckets.data if isinstance(buckets.data, dict) else {}
        bucket = find_parking_lot(payload.get("buckets") or [])
        if bucket is None:
            return {
                "list": None,
                "count": 0,
                "tasks": [],
                "note": "No Parking Lot list exists yet.",
            }

        listed = await self.list_tasks(status)
        if not listed.ok:
            raise TaskStoreError(listed, "read the tasks")
        tasks_payload = listed.data if isinstance(listed.data, dict) else {}
        parked = [
            task
            for task in tasks_payload.get("tasks") or []
            if isinstance(task, dict) and task.get("bucket_id") == bucket.get("id")
        ]
        return {
            "list": {"id": bucket.get("id"), "name": bucket.get("name")},
            "status": status,
            "count": len(parked),
            "tasks": [self._present(task, str(bucket.get("id"))) for task in parked[:limit]],
        }

    async def tags(self, *, icons: bool = False) -> dict[str, Any]:
        """The tag vocabulary: the worker's ``list_tags``, busiest tag first.

        Added 2026-09-23 (Phase 5) for ``GET /v1/ae/lot/tags`` and the MCP
        tool of the same name. The store answers ``{"tags": [{"name", "icon",
        "color"}], "colors": [...], "counts": {name: n | {"tasks": n}}}``; the
        worker flattens that to one row per tag with its task count and sorts
        by use, so a gate message proposes tags Reid already filters on.

        2026-09-25 (issue 28): each row also carries the tag's ``color`` (a
        name from the app's palette, e.g. ``teal``) and ``icon`` (a Tabler icon
        name), both ``None`` when unset, so the Dashboard paints a tag the way
        the app does. With *icons*, the answer adds ``icons``: the path data
        of every icon a row names (from :meth:`icon_set`), and ``icons_note``
        when the set could not be read.

        :returns: ``{"count", "tags": [{"name", "tasks", "color", "icon"}]}``,
            plus ``icons`` (and ``icons_note``) when asked.
        :raises TaskStoreError: When the tags cannot be read.
        """
        listed = await self.list_tags()
        if not listed.ok:
            raise TaskStoreError(listed, "read the tags")
        payload = listed.data if isinstance(listed.data, dict) else {}
        counts = payload.get("counts") if isinstance(payload.get("counts"), dict) else {}
        rows: list[dict[str, Any]] = []
        for tag in payload.get("tags") or []:
            if not isinstance(tag, dict):
                continue
            name = tag.get("name")
            used = counts.get(name)
            rows.append(
                {
                    "name": name,
                    "tasks": used.get("tasks") if isinstance(used, dict) else used,
                    "color": _style(tag.get("color")),
                    "icon": _style(tag.get("icon")),
                }
            )
        rows.sort(key=lambda row: row["tasks"] or 0, reverse=True)
        out: dict[str, Any] = {"count": len(rows), "tags": rows}
        if icons:
            wanted = sorted({row["icon"] for row in rows if row["icon"]})
            known = await self.icon_set() if wanted else {}
            if known is None:
                out["icons"] = {}
                out["icons_note"] = (
                    f"The app's icon set ({ICON_SET_PATH}) could not be read; tags show "
                    "their colour without an icon."
                )
            else:
                out["icons"] = {name: known[name] for name in wanted if name in known}
        return out

    async def link_issue(
        self, task_id: str, issue_url: str, label: str | None = None
    ) -> dict[str, Any]:
        """Point a parked task at its GitHub issue: the worker's ``link_issue``.

        Route C's third and last step for a repo-scoped fork. Never raises: the
        task and the issue both exist whatever happens here, so a failure is an
        outcome the caller reports (the issue URL inline), not an exception.

        :param task_id: The task ``park`` returned.
        :param issue_url: The URL the issue creation returned.
        :param label: Link label; ``GitHub issue`` when omitted.
        :returns: ``{"outcome": "linked", "task_id", "issue_url", "link_id"}`` or
            ``{"outcome": "not_linked", "reason", "issue_url", "instruction"}``.
        """
        linked = await self.add_link(task_id, issue_url, label or "GitHub issue")
        if not linked.ok:
            return {
                "outcome": "not_linked",
                "reason": linked.error,
                "issue_url": issue_url,
                "instruction": (
                    "The task and the issue both exist, but nothing joins them. Report the "
                    "task as filed and give Reid the issue URL inline."
                ),
            }
        link_id = linked.data.get("id") if isinstance(linked.data, dict) else None
        return {
            "outcome": "linked",
            "task_id": task_id,
            "issue_url": issue_url,
            "link_id": link_id,
        }

    async def complete(self, task_id: str, status: str = "done") -> dict[str, Any]:
        """Mark a parked task ``done`` (or ``dropped``) and name the issue it links.

        Added 2026-09-28: a pull request merged into ``dev`` never fires its
        ``Closes`` line, so the issue and the task both stayed open after the
        work shipped. The caller closes the returned issue itself; this only
        moves the task. Never raises.

        :param task_id: The parked task's id.
        :param status: ``done`` or ``dropped``.
        :returns: ``{"outcome": "completed", "task_id", "title", "status",
            "issue_url"}`` or ``{"outcome": "not_completed", "task_id", "reason"}``.
        """
        read = await self.get_task(task_id)
        if not read.ok:
            return {"outcome": "not_completed", "task_id": task_id, "reason": read.error}
        task = read.data if isinstance(read.data, dict) else {}
        patched = await self.patch_task(task_id, {"status": status})
        if not patched.ok:
            return {"outcome": "not_completed", "task_id": task_id, "reason": patched.error}
        return {
            "outcome": "completed",
            "task_id": task_id,
            "title": task.get("title"),
            "status": status,
            "issue_url": issue_url_of(task.get("links")),
        }

    def _present(self, task: dict[str, Any], bucket_id: str) -> dict[str, Any]:
        due_at = task.get("due_at")
        return {
            "id": task.get("id"),
            "title": task.get("title"),
            "quadrant": task.get("quadrant"),
            "estimate": task.get("estimate"),
            "status": task.get("status"),
            "tags": task.get("tags"),
            "created_at": task.get("created_at"),
            # The resurface date, as the calendar date it was parked against.
            "resurfaces": str(due_at)[:10] if due_at else None,
            "next": next_from_notes(task.get("notes")),
            # 2026-09-25 (issue 28): shown on the collapsed card.
            "repo": repo_of(task.get("notes"), task.get("links")),
            "links": [
                link.get("url") for link in task.get("links") or [] if isinstance(link, dict)
            ],
            "url": self.view_url(str(task.get("title") or ""), bucket_id),
        }


__all__ = [
    "ORIGIN_SURFACE",
    "USER_AGENT",
    "ParkFields",
    "Result",
    "Settings",
    "TaskStoreClient",
    "TaskStoreError",
    "find_parking_lot",
    "form_component",
    "issue_url_of",
    "repo_of",
    "reset_icon_set",
    "view_url",
]
