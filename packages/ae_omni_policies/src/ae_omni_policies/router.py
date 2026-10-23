"""The ``/v1/ae`` routes: health, capacity, the Parking Lot, gate replies, Your move, plan limits,
pull request cards, Fabric capacities, Databricks warehouses and spend, the deploy hold, the box
host's reconnect, the server's secrets by name.

2026-10-20 (Admin Center, Secrets) added ``GET /secrets``
(:mod:`ae_omni_policies.secrets_health`): each key ``deploy/pull-secrets.sh``
writes for the server, with whether the running server has it. Names only,
never a value, its length or a hash.

2026-10-20 (Admin Center, Box host) added ``GET /host`` and ``POST
/host/relink`` (:mod:`ae_omni_policies.host_relink`): whether the box host is
connected, what ``deploy/host-relink.sh`` last reported while it is not, and a
button that asks that helper for a sign-in link. The POST is JSON-only and
origin-checked like the deploy hold's, and refuses while the host is online,
because a relink restarts the host and ends every session on it.

2026-10-19 (Admin Center, deploy hold) added ``GET`` and ``POST /deploy/hold``
and ``POST /deploy/resume`` (:mod:`ae_omni_policies.deploy_hold`): a pause on
the box's automatic deploys for 1, 2 or 3 hours, which ends on its own and
replaces stopping the auto-update timer by hand. The POSTs are JSON-only and
origin-checked like the sidebar's ``PUT``. Resume is a POST, not a DELETE,
because the page's extension SDK sends only GET, POST and PATCH.

2026-10-22 (Admin Center, Azure card) added ``GET /azure-cost``
(:mod:`ae_omni_policies.azure_cost`).
2026-10-19 (Admin Center, Databricks) added ``GET /databricks``,
``POST /databricks/warehouses/{id}/start`` and ``.../stop``, and
``POST /databricks/spend/refresh`` (:mod:`ae_omni_policies.databricks`): the
AWS MVP workspace's SQL warehouses with Start and Stop, and its spend against
the $200 credit. The POSTs are JSON-only and origin-checked like Fabric's,
because a Start bills until the warehouse auto stops and a Refresh wakes it;
``GET`` never starts anything and reads spend only off a warehouse already
running.

2026-10-16 (Fabric Admin) added ``GET /fabric``, ``POST /fabric/{name}/wake`` and
``POST /fabric/{name}/suspend`` (:mod:`ae_omni_policies.fabric`): each Fabric
capacity's state through its Logic App, and the page's Wake and Off buttons.
The two POSTs are JSON-only and origin-checked like Merge, because a wake
bills at least seven hours. ``/capacity`` still means session slots. The
second pass the same day added ``POST /fabric/{name}/keepawake`` with
``{"hours": 1..12}``, checked the same way and needing the suspend key, and
the probe's optional details (SKU, region, price, when it sleeps) on ``GET``.
2026-10-21 (the capacity fleet) added ``POST /fabric/{name}/scale`` with
``{"sku": "F8"}``, checked the same way: with ``AE_FABRIC_CAPACITIES_URL`` and
``AE_FABRIC_CAPACITIES_KEY`` set, ``GET`` lists every capacity the shared Logic
App finds, and wake, suspend and scale go through it (fabric.py, module note).
When the fleet fails before any write is sent, wake and suspend fall back to
the capacity's own wake Logic App, if it has one.

2026-09-26 (Dashboard open speed): ``GET /lot`` and ``GET /lot/tags`` are
served stale-while-revalidate (:mod:`ae_omni_policies.swr`, 20 seconds),
because the lot cost about 320 ms through Cloudflare on every call and the
tags 471 ms cold; every AE write to the lot (park, link, resume into a
session) drops both caches, so a Park never shows the lot as it was.

2026-09-25 (the orchestrator agent) added ``GET /sessions`` and ``POST /sessions``:
the sessions the orchestrator started, and starting one with its brief after
holding a slot under the slot cap's own lock, idempotent by ``ae.orch_key``.
Resume (``POST /lot/{task_id}/session``) became idempotent the same way: a live
session already carrying the task's ``ae.task`` is returned, not duplicated
(self-review, https://github.com/analyticendeavors/omnigent-ae/pull/41).

2026-09-24 (PR cards) added ``GET /prs``, ``GET /prs/{owner}/{repo}/{number}``
and ``POST /prs/{owner}/{repo}/{number}/merge`` (:mod:`ae_omni_policies.prs`):
the pull requests upstream tracks per session, as cards with CI, mergeability
and reviews, and a Merge button that is POST-only, JSON-only, origin-checked
and pinned to the head SHA the card showed.

2026-09-24 added ``GET /your-move`` (:mod:`ae_omni_policies.your_move`): one
payload of everything waiting on Reid (sessions that need him, lot items due,
the steward's latest brief, pull requests), each source isolated.

2026-09-24 (plan limits) added ``GET /limits`` and ``POST /limits/{harness}``:
the box host's relay posts the newest Claude Code and Codex subscription
windows, and the composer's context ring shows them (``limits.py`` says where
the numbers come from).

2026-10-21 added ``GET /limits/accounts``: every signed-in account's newest
reading, for the Admin Center's Accounts card (``limits.LimitStore.accounts``).

2026-09-24 (after the first box deploy) added ``POST /lot/{task_id}/session``:
Resume used to copy the block and open a blank composer, and the copy failed
silently in the sandboxed frame, so Reid landed on an empty session. The route
now creates the session itself, the way the web composer does, and posts the
block as its first user message.

2026-09-25 (https://github.com/analyticendeavors/omnigent-ae/issues/28): each lot
task carries ``repo`` and each ``/lot/tags`` row its ``color`` and ``icon``;
``/lot/tags?icons=true`` adds the icons' path data for the Dashboard's tag chips.

Phase 5 (2026-09-23) added ``GET /lot/tags`` and ``POST /lot/{task_id}/link``,
the two task-store calls the worker's ``list_tags`` and ``link_issue`` tools
make, so the stdio MCP server (``ae_omni_policies.mcp``) can offer all four
worker tools through this server alone and never hold the Access token.

Mounted by ``debug_router_modules: [ae_omni_policies.router]`` in
``config/server.yaml``: ``create_app`` reads :data:`DEBUG_ROUTERS` and includes
each ``(router, prefix, tags)`` entry before the SPA mount
(``omnigent/server/app.py`` ``_load_debug_routers``). The mechanism is a plain
router mount; the "debug" in the name is upstream's, and the handoff keeps a
one-line ``extra_routers`` patch as the fallback if the key goes away.

Auth is per route, upstream's convention: every route here depends on
:func:`ae_omni_policies.auth.require_ae_user`. task-store is reached through
:class:`ae_omni_policies.taskstore.TaskStoreClient` with the service token from
the environment; the store is the system of record and these routes are a view
over it plus the session labels.

Trust boundary: most routes in this module are signed-in routes, not admin
routes. Every authenticated identity sees every top-level session on
``/capacity`` and may label any session through ``/lot/park`` and ``/gate``;
there is no per-user scoping there, because upstream's permission store is not
reachable from a router mounted this way. Admin-only (2026-10-21, pre-publication
audit A3): merging, marking ready and updating a pull request branch with the
server's GitHub token, and reading, setting or ending the deploy hold
(:func:`_admin_only`). Admin-only since 2026-10-22 (public issues 4, 18 and 19):
the Fabric wake, suspend, keep-awake and scale actions, the Databricks start,
stop and spend refresh, the host relink (it ends every live session) and the
plan-limits snapshot post, which feeds the usage gate; the box's limits relay
posts with the admin's stored login. ``/your-move`` lists only sessions the caller may open, and
``/limits?session_id=`` answers a session's account only to a caller who may
open it. Who is an admin is decided by ``GET /v1/me`` asked as the caller
(``shared_accounts_routes.admin_probe``), so signed in must never be treated
as admin.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import time
from datetime import UTC, datetime
from typing import Annotated, Any, Literal
from urllib.parse import quote
from zoneinfo import ZoneInfo

import httpx
from fastapi import APIRouter, Depends, Path, Query, Request
from fastapi.responses import JSONResponse
from omnigent.entities.conversation import Conversation
from omnigent.errors import ErrorCode
from omnigent.runtime import get_conversation_store
from omnigent.server.auth import RESERVED_USER_LOCAL, resolve_auth_header
from omnigent.server.routes._content_type import require_json_content_type
from omnigent.server.routes._origin import require_trusted_origin
from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr

from ae_omni_policies import (
    __version__,
    azure_cost,
    databricks,
    deploy_hold,
    fabric,
    github,
    host_relink,
    labels,
    limits,
    merge_watch,
    pr_compliance,
    pr_deploy,
    prs,
    release,
    secrets_health,
    sessions,
    shared_accounts,
    shared_accounts_routes,
    sidebar_prefs,
    slot_cap,
    slot_limit,
    your_move,
)
from ae_omni_policies.auth import require_ae_user
from ae_omni_policies.notes import resume_block_from_notes
from ae_omni_policies.swr import SwrCache
from ae_omni_policies.taskstore import (
    ParkFields,
    Settings,
    TaskStoreClient,
    TaskStoreError,
    issue_url_of,
)

_logger = logging.getLogger(__name__)

router = APIRouter(dependencies=[Depends(require_ae_user)])

TaskStatus = Literal["open", "active", "blocked", "done", "dropped", "all"]
GateRoute = Literal["A", "B", "C", "close"]
# The letter is the call; "close" is spelled the way the gate line offers it.
GATE_REPLY_TEXT: dict[str, str] = {"A": "A", "B": "B", "C": "C", "close": "close it"}
# Identity rides along on the loopback: the session cookie (accounts / OIDC),
# a bearer token, the proxy identity header, and the browser's Origin so the
# events route sees the same first-party request the page made.
_FORWARDED_HEADERS = ("cookie", "authorization", "origin")
_LOOPBACK_TIMEOUT_S = 30.0

# The lot and its tags, stale-while-revalidate (2026-09-26): served from what
# is held for LOT_TTL_S, then served while one background read replaces it.
# Past LOT_MAX_STALE_S a held answer is not served while task-store fails;
# the read is awaited and the failure is the route's usual 502. A change made
# outside this server (the app, the GitHub bridge closing a task) shows within
# LOT_TTL_S plus one Dashboard poll; an AE write drops both caches at once.
LOT_TTL_S = 20.0
LOT_MAX_STALE_S = 300.0


def _store_message(exc: BaseException) -> str:
    # TaskStoreError messages are written for the page; anything else by class only.
    return str(exc) if isinstance(exc, TaskStoreError) else type(exc).__name__


_lot_cache = SwrCache[tuple[str, int], dict[str, Any]](
    "parking lot", LOT_TTL_S, max_stale_s=LOT_MAX_STALE_S, describe=_store_message
)
_tags_cache = SwrCache[bool, dict[str, Any]](
    "lot tags", LOT_TTL_S, max_stale_s=LOT_MAX_STALE_S, describe=_store_message
)


def forget_lot() -> None:
    """Drop the cached lot and tags after an AE write to the lot (Your move's lot too)."""
    _lot_cache.invalidate()
    _tags_cache.invalidate()
    your_move.forget_answer()


def reset_lot_cache() -> None:
    """For tests: forget the lot and tags and cancel their background reads."""
    _lot_cache.reset()
    _tags_cache.reset()


def _with_refresh_error(answer: dict[str, Any], failed: str | None) -> dict[str, Any]:
    """The held answer, saying why the last background read failed when one did."""
    return answer if failed is None else {**answer, "refresh_error": failed}


# Error bodies follow upstream's ``{"error": {"code", "message"}}`` (the shape
# ``OmnigentError`` renders and the web client reads), with extra fields where
# the caller needs more than a sentence: the failure kind from task-store, or
# the note that must be filed by hand. Returned, not raised, because a router
# mounted from outside ``create_app`` cannot register an exception handler.
CODE_NOT_CONFIGURED = "task_store_not_configured"
CODE_STORE_UNAVAILABLE = "task_store_unavailable"
CODE_UNFILED = "task_store_unfiled"
CODE_REPLY_NOT_POSTED = "gate_reply_not_posted"
CODE_RESUME_NO_TARGET = "resume_no_target"
CODE_RESUME_NOT_CREATED = "resume_session_not_created"
CODE_RESUME_NOT_POSTED = "resume_block_not_posted"

# What a resumed session runs on. The composer's default for this deployment is
# the seeded Claude Code agent (``claude-native-ui``) on the one online host
# (the box); the three variables override that without a code change.
RESUME_AGENT_ENV = "AE_RESUME_AGENT"
RESUME_HOST_ENV = "AE_RESUME_HOST"
RESUME_WORKSPACE_ENV = "AE_RESUME_WORKSPACE"
DEFAULT_RESUME_AGENT = "claude-native-ui"
# The labels the composer stamps for a native-CLI agent (upstream
# ``omnigent/_wrapper_labels.py``; web ``nativeWrapperLabelsForAgent``), so the
# session opens terminal-first like one started by hand.
_NATIVE_WRAPPERS: dict[str, str] = {
    "claude-native-ui": "claude-code-native-ui",
    "codex-native-ui": "codex-native-ui",
}
_UI_MODE_LABEL = "omnigent.ui"
_WRAPPER_LABEL = "omnigent.wrapper"


class ParkRequest(BaseModel):
    """``POST /v1/ae/lot/park``: the worker's ``park_fork`` inputs plus an optional session.

    Field names match the worker's tool schema so a note filed from here and one
    filed from a chat surface carry the same lines. ``from`` is ``from_`` in code.
    """

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    thread: str = Field(min_length=1)
    state: str = Field(min_length=1)
    next: str = Field(min_length=1)
    slot: Literal["never opened", "freed", "swapped-back"]
    quadrant: Literal["hotfix", "feature", "admin"]
    est: str = Field(min_length=1)
    from_: str = Field(alias="from", min_length=1)
    summary: str | None = None
    repo: str | None = None
    tags: list[str] | None = None
    checklist: list[str] | None = None
    due_date: str | None = None
    issue_url: str | None = None
    # When given, the session is marked parked (ae.status) and pointed at the
    # task (ae.task) once the task exists.
    session_id: str | None = None

    def fields(self) -> ParkFields:
        return ParkFields(
            thread=self.thread,
            state=self.state,
            next=self.next,
            slot=self.slot,
            quadrant=self.quadrant,
            est=self.est,
            from_=self.from_,
            summary=self.summary,
            checklist=self.checklist,
            repo=self.repo,
            tags=self.tags,
            due_date=self.due_date,
            issue_url=self.issue_url,
        )


class GateReply(BaseModel):
    """``POST /v1/ae/gate/{session_id}/reply``: one of the gate's four answers."""

    model_config = ConfigDict(extra="forbid")

    route: GateRoute


class LinkRequest(BaseModel):
    """``POST /v1/ae/lot/{task_id}/link``: the worker's ``link_issue`` inputs, minus the id."""

    model_config = ConfigDict(extra="forbid")

    url: str = Field(min_length=1)
    label: str = "GitHub issue"


class CompleteRequest(BaseModel):
    """``POST /v1/ae/lot/{task_id}/complete``: how the work ended, and what to say on the issue."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["done", "dropped"] = "done"
    summary: str = ""


class KeepAwakeRequest(BaseModel):
    """``POST /v1/ae/fabric/{name}/keepawake``: how many more hours, whole, 1 to 12.

    Strict: ``"4"`` or ``4.5`` is a 422, so the Logic App never sees a value
    it would refuse with a 400.
    """

    model_config = ConfigDict(extra="forbid")

    hours: StrictInt = Field(ge=fabric.KEEP_AWAKE_HOURS_MIN, le=fabric.KEEP_AWAKE_HOURS_MAX)


class ScaleRequest(BaseModel):
    """``POST /v1/ae/fabric/{name}/scale``: the new size, one of ``fabric.SKU_CHOICES``.

    The allowlist is checked by fabric.py (a 400 naming the choices), before any call.
    """

    model_config = ConfigDict(extra="forbid")

    sku: StrictStr = Field(min_length=1, max_length=16)


class MergeRequest(BaseModel):
    """``POST /v1/ae/prs/{owner}/{repo}/{number}/merge``: the head the card showed, and how.

    ``sha`` is required: the merge is pinned to it, so a commit pushed after
    the card was read makes the merge a 409 instead of merging it unseen.
    ``method`` defaults to the card's ``default_method``, which is its
    recommended method (2026-09-28). ``session_id`` names
    the session the card sits in, so the answer can offer to set it to review.
    """

    model_config = ConfigDict(extra="forbid")

    sha: str = Field(min_length=7, max_length=64, pattern=r"^[0-9a-f]+$")
    method: github.MergeMethod | None = None
    session_id: str | None = None


class ReadyRequest(BaseModel):
    """The head SHA shown on the card before marking a draft ready."""

    model_config = ConfigDict(extra="forbid")

    sha: str = Field(min_length=7, max_length=64, pattern=r"^[0-9a-f]+$")


class UpdateBranchRequest(ReadyRequest):
    """The displayed head SHA to pin an asynchronous branch update."""


def _error(status_code: int, code: str, message: str, **extra: Any) -> JSONResponse:
    return JSONResponse(
        status_code=status_code, content={"error": {"code": code, "message": message, **extra}}
    )


def _not_found(message: str) -> JSONResponse:
    return _error(404, ErrorCode.NOT_FOUND, message)


def _not_configured(settings: Settings) -> JSONResponse:
    return _error(
        503,
        CODE_NOT_CONFIGURED,
        "task-store is not configured: set AE_TASK_STORE_URL (the app's origin), "
        "AE_CF_ACCESS_CLIENT_ID and AE_CF_ACCESS_CLIENT_SECRET (the Cloudflare Access "
        "service token) in the server's environment.",
        url=settings.app_url,
    )


def _store_failure(exc: TaskStoreError) -> JSONResponse:
    return _error(
        502, CODE_STORE_UNAVAILABLE, str(exc), kind=exc.result.kind, status=exc.result.status
    )


async def _conversation(session_id: str) -> Conversation | None:
    return await asyncio.to_thread(get_conversation_store().get_conversation, session_id)


async def _set_labels(session_id: str, updates: dict[str, str]) -> None:
    await asyncio.to_thread(get_conversation_store().set_labels, session_id, updates)


def forwarded_headers(request: Request) -> dict[str, str]:
    """The caller's identity headers, for a request the server makes on the caller's behalf."""
    names = {*_FORWARDED_HEADERS, resolve_auth_header().lower()}
    return {key: value for key, value in request.headers.items() if key.lower() in names}


async def _loopback(
    request: Request, method: str, path: str, body: dict[str, Any] | None = None
) -> httpx.Response:
    """Call this server's own *path* as the caller.

    In-process through ``httpx.ASGITransport`` by default, so nothing depends on
    the server knowing its own address; ``AE_SELF_URL`` switches to a real HTTP
    call for a deployment that fronts the app with something the routes need to
    see (a proxy that injects identity).
    """
    headers = forwarded_headers(request)
    if body is not None:
        headers["content-type"] = "application/json"
    self_url = (os.environ.get("AE_SELF_URL") or "").rstrip("/")
    if self_url:
        async with httpx.AsyncClient(base_url=self_url, timeout=_LOOPBACK_TIMEOUT_S) as http:
            return await http.request(method, path, json=body, headers=headers)
    transport = httpx.ASGITransport(app=request.app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://ae-loopback", timeout=_LOOPBACK_TIMEOUT_S
    ) as http:
        return await http.request(method, path, json=body, headers=headers)


async def _loopback_post(request: Request, path: str, body: dict[str, Any]) -> httpx.Response:
    """POST *body* to this server's own *path* as the caller (see :func:`_loopback`)."""
    return await _loopback(request, "POST", path, body)


async def _admin_only(request: Request, user: str | None, action: str) -> JSONResponse | None:
    """``None`` for an OmniGent admin, else the 403 ``shared_accounts_routes`` answers.

    Admin is ``GET /v1/me`` asked as the caller (``shared_accounts_routes.admin_probe``),
    so a single-user runtime's one user passes and a signed-in non-admin does not.
    Added 2026-10-21 (pre-publication audit A3): merging with the server's GitHub
    token and holding deploys are admin actions once a second account exists.
    """
    if await shared_accounts_routes.admin_probe(request, user):
        return None
    return _error(
        403,
        shared_accounts_routes.CODE_ADMIN_ONLY,
        f"Only an OmniGent admin can {action}; ask an admin to do it.",
    )


def _transport_failure(exc: httpx.HTTPError, step: str) -> str:
    """What a failed in-process call tells the caller; the details stay in the log.

    httpx's own text names the loopback URL it tried, so it is logged here and
    never sent (2026-10-22, CodeQL py/stack-trace-exposure on the public repo).
    A refused connection never reached the server, so a retry is safe; a timeout
    or a dropped connection may have landed, so the caller checks first.
    """
    _logger.warning("%s failed: %r", step, exc)
    if isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout)):
        return "could not reach the server; try again, and if it repeats the server log says why"
    if isinstance(exc, httpx.TimeoutException):
        return "timed out before an answer, so it may have gone through; check before trying again"
    return (
        "the connection dropped before an answer, so it may have gone through; "
        "check before trying again"
    )


def _acl_user(user: str | None) -> str | None:
    """The identity to filter session listings by; ``None`` on a single-user runtime."""
    return None if user is None or user == RESERVED_USER_LOCAL else user


@router.get("/health")
async def health() -> dict[str, Any]:
    """Liveness plus whether task-store is configured (not whether it answers)."""
    settings = Settings.from_env()
    return {
        "ok": True,
        "version": __version__,
        "task_store": {"url": settings.app_url, "configured": settings.configured},
    }


@router.get("/schedule-defaults")
async def schedule_defaults() -> dict[str, Any]:
    """The server's own clock and default project, for the schedule seeder and
    the check-in tool (2026-10-22): ``AE_TASK_STORE_TZ`` (UTC when unset) and
    ``OMNIGENT_SCHEDULED_TASK_DEFAULT_PROJECT`` (``None`` when unset). Both are
    server settings, and the seeder runs on another machine's environment."""
    project = (os.environ.get("OMNIGENT_SCHEDULED_TASK_DEFAULT_PROJECT") or "").strip()
    return {"timezone": Settings.from_env().tz, "project": project or None}


@router.get("/capacity")
async def capacity() -> dict[str, Any]:
    """Every top-level session bucketed by ``ae.status``, with elapsed time and NEXT.

    ``working`` holds the slots (``ae.status=working``, not waiting on the human);
    ``blocked`` is derived: working but ``pending_elicitation_count > 0``, or
    working with a failed last turn (``failed`` on the row says which; neither
    holds a slot, 2026-09-25). ``unlabelled_running`` catches sessions the slot
    cap has not placed yet.
    """
    rows, limit = await asyncio.gather(
        asyncio.to_thread(sessions.list_top_level), asyncio.to_thread(slot_limit.effective_limit)
    )
    now = time.time()
    # The clock, in the zone resurface dates are reckoned in, so a scheduled
    # agent can say which lot items resurface today without guessing the date
    # (the steward, Phase 5, 2026-09-23).
    settings = Settings.from_env()
    local_now = datetime.fromtimestamp(now, ZoneInfo(settings.tz))
    summaries = sorted(
        (sessions.summarize(row, now=now) for row in rows), key=lambda item: item.started
    )
    by_status: dict[str, list[dict[str, Any]]] = {
        "working": [],
        "blocked": [],
        "review": [],
        "parked": [],
        "reference": [],
        "unlabelled_running": [],
    }
    for item in summaries:
        if item.status == labels.WORKING:
            held = not (item.blocked or item.failed)
            by_status["working" if held else "blocked"].append(item.as_dict())
        elif item.status in by_status:
            by_status[item.status].append(item.as_dict())
        elif item.status is None and item.live_status == "running":
            by_status["unlabelled_running"].append(item.as_dict())
    return {
        "limit": limit,
        "slots_used": len(by_status["working"]),
        "now": local_now.isoformat(timespec="seconds"),
        "today": local_now.date().isoformat(),
        "timezone": settings.tz,
        **by_status,
    }


@router.get("/release")
async def release_view() -> dict[str, Any]:
    """The release the box runs, the newest tagged, and the sessions a deploy waits on.

    For the Session Dashboard's release row (``release.py``, 2026-10-17).
    """
    return await release.status()


@router.get("/prs/{owner}/{repo}/{number}/deploy", response_model=None)
async def pull_request_deploy(
    owner: str, repo: str, number: Annotated[int, Path(ge=1)]
) -> dict[str, Any] | JSONResponse:
    """Whether the running release contains a merged pull request (``pr_deploy.py``).

    For the chat's action dock and merged row (2026-10-20). GitHub off or
    failing answers ``state: unknown`` with the reason in ``error``, not an error.
    """
    if (bad := _bad_repo(owner, repo)) is not None:
        return bad
    return await pr_deploy.status(f"{owner}/{repo}", number)


@router.get("/deploy/hold", response_model=None)
async def deploy_hold_view(
    request: Request,
    user: Annotated[str | None, Depends(require_ae_user)],
) -> dict[str, Any] | JSONResponse:
    """The deploy hold in force, if any (``deploy_hold.py`` says why it exists); admins only.

    A corrupt hold file answers ``held`` false with the reason in ``error``:
    ``deploy/auto-update.sh`` deploys then too.
    """
    if (refused := await _admin_only(request, user, "see the deploy hold")) is not None:
        return refused
    reading = await asyncio.to_thread(deploy_hold.store().read)
    return deploy_hold.view(reading).model_dump()


@router.post(
    "/deploy/hold",
    response_model=None,
    dependencies=[Depends(require_json_content_type), Depends(require_trusted_origin)],
)
async def deploy_hold_set(
    request: Request,
    body: deploy_hold.HoldRequest,
    user: Annotated[str | None, Depends(require_ae_user)],
) -> dict[str, Any] | JSONResponse:
    """Hold deploys for ``hours`` (1 to 3) from now, replacing any hold; admins only."""
    if (refused := await _admin_only(request, user, "hold deploys")) is not None:
        return refused
    who = (user or "").strip() or "local"
    try:
        hold = await asyncio.to_thread(deploy_hold.store().set, body.hours, who)
    except deploy_hold.DeployHoldSaveError as exc:
        _logger.warning("deploy hold for %sh by %s not saved: %s", body.hours, who, exc)
        return _error(500, deploy_hold.CODE_NOT_SAVED, str(exc))
    # hold.set_by is what the store kept, so the log names the stored user.
    _logger.info(
        "deploy hold set for %sh until %s by %s",
        body.hours,
        deploy_hold.clock(hold.until),
        hold.set_by,
    )
    return deploy_hold.view(deploy_hold.Reading(hold)).model_dump()


@router.post(
    "/deploy/resume",
    response_model=None,
    dependencies=[Depends(require_json_content_type), Depends(require_trusted_origin)],
)
async def deploy_hold_resume(
    request: Request,
    user: Annotated[str | None, Depends(require_ae_user)],
) -> dict[str, Any] | JSONResponse:
    """End the deploy hold now; idempotent, any body is ignored; admins only."""
    if (refused := await _admin_only(request, user, "resume deploys")) is not None:
        return refused
    who = (user or "").strip() or "local"
    try:
        before = await asyncio.to_thread(deploy_hold.store().clear)
    except deploy_hold.DeployHoldSaveError as exc:
        _logger.warning("deploy hold not cleared for %s: %s", who, exc)
        return _error(500, deploy_hold.CODE_NOT_SAVED, str(exc))
    was = (
        f"was until {deploy_hold.clock(before.hold.until)}"
        if before.hold is not None
        else "none was on"
    )
    _logger.info("deploy hold cleared by %s (%s)", who, was)
    return deploy_hold.view(deploy_hold.Reading(None)).model_dump()


async def _box_host(request: Request) -> tuple[dict[str, Any] | None, str | None]:
    """The box host's ``/v1/hosts`` row (``AE_RESUME_HOST`` names it when set), or why not."""
    response = await _loopback(request, "GET", "/v1/hosts")
    payload = _json_or_none(response)
    if response.status_code >= 300 or not isinstance(payload, dict):
        return None, f"listing the hosts failed: {response.status_code}"
    wanted = (os.environ.get(RESUME_HOST_ENV) or "").strip() or None
    host = host_relink.pick_host(payload.get("hosts") or [], wanted)
    if host is None:
        if wanted:
            return None, f"host {wanted} ({RESUME_HOST_ENV}) is not registered"
        if host_relink.real_hosts(payload.get("hosts") or []):
            return None, f"more than one host could be the box; set {RESUME_HOST_ENV} to one"
        return None, "no host is registered"
    return host, None


def _host_reading() -> tuple[host_relink.Status | None, host_relink.Request | None, str | None]:
    """The helper's report and any pending request; a corrupt file is a reason, not a crash."""
    current = host_relink.store()
    problems: list[str] = []
    status = asked = None
    try:
        status = current.read_status()
    except ValueError as exc:
        problems.append(str(exc))
    try:
        asked = current.read_request()
    except ValueError as exc:
        problems.append(str(exc))
    return status, asked, "; ".join(problems) or None


@router.get("/host")
async def host_view(
    request: Request, user: Annotated[str | None, Depends(require_ae_user)]
) -> dict[str, Any]:
    """The box host's connection and the reconnect helper's last report (``host_relink.py``).

    ``can_act`` says whether the caller may ask for a reconnect link (2026-10-22).
    """
    (host, host_error), can_act = await asyncio.gather(
        _box_host(request), shared_accounts_routes.admin_probe(request, user)
    )
    status, asked, read_error = await asyncio.to_thread(_host_reading)
    error = "; ".join(item for item in (host_error, read_error) if item) or None
    answer = host_relink.view(host, status, asked, error=error).model_dump()
    return {**answer, "can_act": can_act}


@router.post(
    "/host/relink",
    response_model=None,
    dependencies=[Depends(require_json_content_type), Depends(require_trusted_origin)],
)
async def host_relink_request(
    request: Request, user: Annotated[str | None, Depends(require_ae_user)]
) -> dict[str, Any] | JSONResponse:
    """Ask the box's helper for a reconnect link; 409 while the host is connected or a loop runs.

    The helper restarts the host once the sign-in lands, which would end every
    live session, so a connected host is refused here and not only hidden on
    the page. A request while a link is out would queue a second login loop
    after the first, so that is refused here too (2026-10-20). Any body is ignored.
    Admin only (2026-10-22, public issue 4).
    """
    if (refused := await _admin_only(request, user, "reconnect the box host")) is not None:
        return refused
    who = (user or "").strip() or "local"
    host, host_error = await _box_host(request)
    if host is None:
        return _error(502, host_relink.CODE_NO_HOST, host_error or "the box host is unknown")
    if host.get("status") == "online":
        return _error(
            409,
            host_relink.CODE_CONNECTED,
            "The box host is connected, so it needs no reconnect link.",
        )
    status, asked, _ = await asyncio.to_thread(_host_reading)
    if host_relink.view(host, status, asked).state in host_relink.BUSY_STATES:
        return _error(
            409,
            host_relink.CODE_BUSY,
            "A reconnect link is already out; sign in with it, or wait for it to lapse.",
        )
    try:
        await asyncio.to_thread(host_relink.store().request, who)
    except host_relink.HostRelinkSaveError as exc:
        _logger.warning("host relink request by %s not saved: %s", who, exc)
        return _error(500, host_relink.CODE_NOT_SAVED, str(exc))
    _logger.info("host relink requested by %s for %s", who, host.get("name"))
    status, asked, read_error = await asyncio.to_thread(_host_reading)
    # The caller passed the admin gate above, so the card keeps its button.
    answer = host_relink.view(host, status, asked, error=read_error).model_dump()
    return {**answer, "can_act": True}


@router.get("/secrets")
async def secrets_view(_user: Annotated[str | None, Depends(require_ae_user)]) -> dict[str, Any]:
    """Each server secret by name with whether the server has it (``secrets_health.py``)."""
    return secrets_health.view().model_dump()


@router.get("/limits")
async def limits_view(
    request: Request,
    user: Annotated[str | None, Depends(require_ae_user)],
    session_id: Annotated[str | None, Query(max_length=120)] = None,
) -> dict[str, Any]:
    """The newest plan-limit snapshot per harness (``claude``, ``codex``), ``null`` if never seen.

    Posted by the box host's relay (``ae_omni_policies.limits_relay``); read by
    the composer's context-ring popover (patch P6) and the Parking Lot page's
    capacity strip. Added 2026-09-24. ``session_id`` (2026-10-17) answers the
    Claude entry as that session sees it, so a session on a backup account
    shows its own account's limits (``limits.session_view``). A session the
    caller cannot open (2026-10-21, audit A2) is answered like an unknown one:
    the box-wide view, never that session's account windows.
    """
    if session_id is not None and not await _may_open_session(request, user, session_id):
        session_id = None
    return await asyncio.to_thread(limits.store().view, session_id=session_id)


def _shared_labels() -> dict[str, str] | None:
    """Shared account id (``shared:<name>``) to its label; ``None`` if the store is unreadable."""
    try:
        views = shared_accounts.store().views()
    except shared_accounts.SharedAccountsError as exc:
        _logger.warning("limits accounts: shared accounts not read: %s", exc)
        return None
    return {view.account: view.label for view in views}


def _account_row(reading: limits.AccountUsage) -> dict[str, Any]:
    """One Accounts card row: labels, plan, percents and reset times; never an email or token."""
    five, week = limits.headline_windows(reading.windows)
    return {
        "harness": reading.harness,
        "account": reading.account,
        "label": reading.label,
        "plan": reading.plan,
        "five_hour": five.model_dump(mode="json") if five else None,
        "weekly": week.model_dump(mode="json") if week else None,
        "credits": reading.credits.model_dump(mode="json") if reading.credits else None,
        "observed_at": reading.observed_at.isoformat() if reading.observed_at else None,
        "live_sessions": reading.live_sessions,
    }


def _accounts_view() -> dict[str, Any]:
    rows = limits.store().accounts(_shared_labels())
    return {
        "now": datetime.now(UTC).isoformat(),
        "accounts": [_account_row(row) for row in rows],
    }


@router.get("/limits/accounts")
async def limits_accounts(
    _user: Annotated[str | None, Depends(require_ae_user)],
) -> dict[str, Any]:
    """Every signed-in account's newest 5-hour and weekly reading, Claude then Codex.

    Added 2026-10-21 for the Admin Center's Accounts card. Signed in, like
    ``GET /limits`` without ``session_id``: the box-wide numbers, no session
    named. The signed-in Claude logins come from the relay's newest post
    (``known_accounts``: the host's ``account_descriptors``, read on the host,
    since this server runs apart from it) and the shared ones from the
    server's own store, so an account with no reading yet still has a row
    with ``observed_at`` null. Codex has a row once its login has answered.
    """
    return await asyncio.to_thread(_accounts_view)


async def _may_open_session(request: Request, user: str | None, session_id: str) -> bool:
    """Whether the caller may open *session_id*, asked of ``GET /v1/sessions/{id}`` as them.

    Upstream's own access check (owner, share, admin) answers, since its
    permission store is not reachable from this router. Always true on a
    single-user runtime; any failure is a no.
    """
    if _acl_user(user) is None:
        return True
    # The four flags skip the snapshot's expensive parts; the access check still runs first.
    lean = (
        "include_items=false&include_liveness=false&include_live_status=false&include_usage=false"
    )
    try:
        response = await _loopback(
            request, "GET", f"/v1/sessions/{quote(session_id, safe='')}?{lean}"
        )
    except httpx.HTTPError as exc:
        _logger.warning("session access check failed: %s", type(exc).__name__)
        return False
    if response.status_code not in (200, 403, 404):
        _logger.warning("session access check answered %s", response.status_code)
    return response.status_code == 200


@router.post(
    "/limits/{harness}",
    response_model=None,
    dependencies=[Depends(require_json_content_type), Depends(require_trusted_origin)],
)
async def limits_post(
    request: Request,
    harness: limits.Harness,
    body: limits.LimitSnapshot,
    user: Annotated[str | None, Depends(require_ae_user)],
) -> dict[str, Any] | JSONResponse:
    """Record one harness's snapshot; an older reading than the one held is ignored.

    Admin only (2026-10-22, public issue 18): the reading decides whether new
    sessions are blocked. The box's relay (``limits_relay.py``) posts JSON with
    the admin's stored login and no ``Origin``, so it passes both checks.
    """
    if (refused := await _admin_only(request, user, "record plan usage")) is not None:
        return refused
    if body.harness != harness:
        return _error(
            400,
            ErrorCode.INVALID_INPUT,
            f"the body is for {body.harness!r} but the path names {harness!r}",
        )
    kept = await asyncio.to_thread(limits.store().put, body)
    return {"kept": kept, "harness": harness}


@router.get("/sidebar")
async def sidebar_view(user: Annotated[str | None, Depends(require_ae_user)]) -> dict[str, Any]:
    """The caller's manual sidebar order and its revision (empty at 0 when never saved).

    Added 2026-10-19 so a manual session order follows the user across devices
    (``sidebar_prefs.py`` says why).
    """
    state = await asyncio.to_thread(sidebar_prefs.store().get, user or "local")
    return state.model_dump()


@router.put(
    "/sidebar",
    response_model=None,
    dependencies=[Depends(require_json_content_type), Depends(require_trusted_origin)],
)
async def sidebar_put(
    body: sidebar_prefs.SidebarOrderPut, user: Annotated[str | None, Depends(require_ae_user)]
) -> dict[str, Any] | JSONResponse:
    """Save the whole order when ``revision`` is the one held; 409 carries the current state."""
    try:
        order = sidebar_prefs.normalize_order(body.order)
    except ValueError as exc:
        return _error(400, ErrorCode.INVALID_INPUT, str(exc))
    try:
        result = await asyncio.to_thread(
            sidebar_prefs.store().put, user or "local", order, body.revision
        )
    except sidebar_prefs.SidebarSaveError as exc:
        return _error(503, sidebar_prefs.CODE_NOT_SAVED, str(exc))
    if result.conflict:
        return _error(
            409,
            ErrorCode.CONFLICT,
            "The sidebar order changed on another device; reload it and retry the move.",
            order=result.state.order,
            revision=result.state.revision,
        )
    return result.state.model_dump()


@router.get("/your-move")
async def your_move_view(
    user: Annotated[str | None, Depends(require_ae_user)],
    fresh: Annotated[bool, Query()] = False,
) -> dict[str, Any]:
    """Everything waiting on Reid right now; see :mod:`ae_omni_policies.your_move`.

    Always a 200: a source that fails carries its own ``ok: false`` and error,
    so the page renders the rest. ``fresh=true`` skips the few-second cache.
    Sessions are those the caller may open, the steward brief the caller's own
    (2026-10-21, audit A2).
    """
    return await your_move.build(fresh=fresh, user=_acl_user(user))


@router.get("/lot", response_model=None)
async def lot(
    status: Annotated[TaskStatus, Query()] = "open",
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> dict[str, Any] | JSONResponse:
    """The Parking Lot as the worker's ``list_parked`` returns it.

    Stale-while-revalidate for :data:`LOT_TTL_S` (2026-09-26); ``refresh_error``
    is set while a background read is failing and the held lot is served.
    """
    settings = Settings.from_env()
    if not settings.configured:
        return _not_configured(settings)

    async def load() -> dict[str, Any]:
        async with TaskStoreClient(settings) as client:
            return await client.list_parked(status=status, limit=limit)

    key = (status, limit)
    try:
        answer = await _lot_cache.get(key, load)
    except TaskStoreError as exc:
        return _store_failure(exc)
    return _with_refresh_error(answer, _lot_cache.last_error(key))


@router.post("/lot/park", response_model=None)
async def park(body: ParkRequest) -> dict[str, Any] | JSONResponse:
    """File a parked fork; optionally mark the session parked and point it at the task.

    The session is checked before anything is filed, so a wrong id costs a 404
    and not a task nobody asked for. Order after that is the worker's: task,
    stamp, issue link.
    """
    settings = Settings.from_env()
    if not settings.configured:
        return _not_configured(settings)
    if body.session_id is not None and await _conversation(body.session_id) is None:
        return _not_found(f"no session {body.session_id}")
    async with TaskStoreClient(settings) as client:
        outcome = await client.park(body.fields())
    # Before the answer, filed or not: an unfiled park may still have made a task.
    forget_lot()
    if outcome.get("outcome") != "filed":
        return _error(
            502,
            CODE_UNFILED,
            str(outcome.get("reason") or "task-store did not file the fork"),
            outcome="unfiled",
            note=outcome.get("note"),
            instruction=outcome.get("instruction"),
        )
    if body.session_id is not None:
        writes = {labels.STATUS: labels.PARKED, labels.TASK: str(outcome["task_id"])}
        await _set_labels(body.session_id, writes)
        outcome["session"] = {"id": body.session_id, "labels": writes}
    return outcome


@router.get("/lot/tags", response_model=None)
async def lot_tags(
    icons: Annotated[bool, Query()] = False,
) -> dict[str, Any] | JSONResponse:
    """The tag vocabulary as the worker's ``list_tags`` returns it, busiest first.

    Each row carries the tag's ``color`` and ``icon`` from the app; ``icons=true``
    adds the icons' path data, which the Dashboard's lot cards paint (issue 28).
    Stale-while-revalidate like ``/lot`` (2026-09-26).
    """
    settings = Settings.from_env()
    if not settings.configured:
        return _not_configured(settings)

    async def load() -> dict[str, Any]:
        async with TaskStoreClient(settings) as client:
            return await client.tags(icons=icons)

    try:
        answer = await _tags_cache.get(icons, load)
    except TaskStoreError as exc:
        return _store_failure(exc)
    return _with_refresh_error(answer, _tags_cache.last_error(icons))


@router.post("/lot/{task_id}/link", response_model=None)
async def lot_link(task_id: str, body: LinkRequest) -> dict[str, Any] | JSONResponse:
    """Attach a URL to a parked task: the worker's ``link_issue``.

    The outcome is the worker's, ``linked`` or ``not_linked`` with the
    instruction to hand the URL over inline; a store that refuses the link
    is a 200 with ``not_linked``, not a 502, because the task and the issue
    both exist and the caller still has a report to write.
    """
    settings = Settings.from_env()
    if not settings.configured:
        return _not_configured(settings)
    async with TaskStoreClient(settings) as client:
        linked = await client.link_issue(task_id, body.url, body.label)
    forget_lot()
    return linked


# The GitHub-to-task-store bridge (sync_github.py in agents-and-automation)
# pushes a done task's close to its issue on a 15-minute timer; this names it
# when the close here could not happen.
ISSUE_FALLBACK = "The issue closes on the next task-store GitHub sync, within 15 minutes."


@router.post("/lot/{task_id}/complete", response_model=None)
async def lot_complete(task_id: str, body: CompleteRequest) -> dict[str, Any] | JSONResponse:
    """Finish a parked task: mark it done (or dropped), then close its linked issue.

    Why (2026-09-28): the resumed session used to rely on ``Closes <issue>``
    in its pull request, which GitHub only honours on a merge into the default
    branch; a merge into ``dev`` left both open. The task moves first because
    it is the system of record; a failed issue close is an outcome with the
    sync as its fallback, not an error.
    """
    settings = Settings.from_env()
    if not settings.configured:
        return _not_configured(settings)
    async with TaskStoreClient(settings) as client:
        outcome = await client.complete(task_id, body.status)
    forget_lot()
    if outcome["outcome"] != "completed":
        return outcome
    issue = outcome.pop("issue_url")
    outcome["issue"] = {"url": issue, "closed": False, "note": "The task links no issue."}
    if issue is None:
        return outcome
    gh_settings = github.GitHubSettings.from_env()
    if not gh_settings.configured:
        outcome["issue"]["note"] = f"No GitHub token here. {ISSUE_FALLBACK}"
        return outcome
    repo, _, number = issue.removeprefix("https://github.com/").partition("/issues/")
    verb = "Completed" if body.status == "done" else "Dropped"
    comment = f"{verb} from OmniGent. {body.summary}".strip()
    reason: Literal["completed", "not_planned"] = (
        "completed" if body.status == "done" else "not_planned"
    )
    try:
        async with github.GitHubClient(gh_settings) as gh_client:
            await gh_client.close_issue(repo, int(number), reason, comment)
    except github.GitHubError as exc:
        outcome["issue"]["note"] = f"Could not close it here ({exc}). {ISSUE_FALLBACK}"
        return outcome
    outcome["issue"] = {"url": issue, "closed": True}
    return outcome


async def _resume_block(task_id: str) -> dict[str, Any] | JSONResponse:
    """The task's Resume in Claude block, or the error response that explains its absence."""
    settings = Settings.from_env()
    if not settings.configured:
        return _not_configured(settings)
    async with TaskStoreClient(settings) as client:
        result = await client.get_task(task_id)
    if not result.ok:
        if result.status == 404:
            return _not_found(f"no task {task_id}")
        return _store_failure(TaskStoreError(result, "read the task"))
    task = result.data if isinstance(result.data, dict) else {}
    block = resume_block_from_notes(task.get("notes"))
    if block is None:
        return _not_found(f"task {task_id} has no Resume in Claude block in its notes")
    return {
        "task_id": task.get("id", task_id),
        "title": task.get("title"),
        "block": block,
        "fenced": f"```\n{block}\n```",
        "issue_url": issue_url_of(task.get("links")),
    }


def first_message(resumed: dict[str, Any]) -> str:
    """What Resume posts: the fenced block, plus the line that says how to finish it.

    Why (2026-09-24): so lot work closes when it ships. It first asked for
    ``Closes <url>`` in the pull request; 2026-09-28 replaced that with the
    ``complete_task`` tool, because GitHub ignores ``Closes`` on a merge into
    ``dev`` and a parked task stayed open after its work merged.
    """
    issue = resumed.get("issue_url")
    closes = f" and closes {issue}" if issue else ""
    return (
        f"{resumed['fenced']}\n\nWhen this work is finished, call the `complete_task` tool "
        f"with task_id `{resumed['task_id']}`. It marks this parked task done{closes}."
    )


@router.post("/lot/{task_id}/resume", response_model=None)
async def resume(task_id: str) -> dict[str, Any] | JSONResponse:
    """The task's fenced "Resume in Claude" block, to paste into a new session. Reads only."""
    return await _resume_block(task_id)


class _NoTarget(Exception):
    """No host, agent or workspace to start the resumed session on; the message says which."""


def _json_or_none(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return None


async def _resume_agent(request: Request, name: str | None = None) -> dict[str, Any]:
    """The built-in agent a resumed session binds to (``GET /v1/agents``, by name).

    :param name: The agent to find instead of the Resume default (the orchestrator's
        sessions name ``claude-native-ui`` or ``codex-native-ui`` themselves).
    """
    wanted = name or (os.environ.get(RESUME_AGENT_ENV) or "").strip() or DEFAULT_RESUME_AGENT
    response = await _loopback(request, "GET", "/v1/agents?limit=1000")
    payload = _json_or_none(response)
    if response.status_code >= 300 or not isinstance(payload, dict):
        raise _NoTarget(f"listing the agents failed: {response.status_code}")
    for agent in payload.get("data") or []:
        if isinstance(agent, dict) and agent.get("name") == wanted and agent.get("id"):
            return agent
    if name:
        raise _NoTarget(f"no built-in agent named {wanted} on this server")
    raise _NoTarget(f"no built-in agent named {wanted} (set {RESUME_AGENT_ENV} to another)")


async def _resume_host(request: Request, name: str | None = None) -> dict[str, Any]:
    """The host the composer would pick: the named one, else the only online one.

    :param name: A host id or name that wins over ``AE_RESUME_HOST``.
    """
    response = await _loopback(request, "GET", "/v1/hosts")
    payload = _json_or_none(response)
    if response.status_code >= 300 or not isinstance(payload, dict):
        raise _NoTarget(f"listing the hosts failed: {response.status_code}")
    # The same selection the Admin Center's Box host card makes (host_relink.py).
    online = [
        host
        for host in host_relink.real_hosts(payload.get("hosts") or [])
        if host.get("status") == "online"
    ]
    wanted = name or (os.environ.get(RESUME_HOST_ENV) or "").strip()
    if wanted:
        match = host_relink.named(online, wanted)
        if match is not None:
            return match
        source = "" if name else f" ({RESUME_HOST_ENV})"
        raise _NoTarget(f"host {wanted}{source} is not online")
    if len(online) == 1:
        return online[0]
    if not online:
        raise _NoTarget("no host is online: start the host service on the box, then Resume again")
    names = ", ".join(str(host.get("name") or host.get("host_id")) for host in online)
    raise _NoTarget(f"{len(online)} hosts are online ({names}); set {RESUME_HOST_ENV} to one")


async def _host_home(request: Request, host_id: str) -> str | None:
    """The host's home directory, resolved by the host the way a scheduled fire does.

    Upstream's own helper (``omnigent/server/routes/_workspace_validation.py``
    ``_ask_host_stat``, which ``scheduled/fire.py`` uses for a task that pinned
    no workspace). It is private upstream, so any failure to reach it is a
    missing workspace, not a 500; ``AE_RESUME_WORKSPACE`` skips it entirely.
    """
    try:
        from omnigent.server.routes._workspace_validation import (
            WorkspaceValidationError,
            _ask_host_stat,
        )
    except ImportError:
        _logger.warning("resume: upstream's host stat helper moved; set %s", RESUME_WORKSPACE_ENV)
        return None
    registry = getattr(request.app.state, "host_registry", None)
    conn = registry.get(host_id) if registry is not None else None
    if conn is None:
        return None
    try:
        stat = await _ask_host_stat(host_registry=registry, host_conn=conn, path="~")
    except (WorkspaceValidationError, TimeoutError, OSError) as exc:
        _logger.warning("resume: could not resolve the home directory on %s: %s", host_id, exc)
        return None
    canonical = stat.get("canonical_path")
    return canonical if stat.get("exists") and isinstance(canonical, str) else None


async def _resume_workspace(request: Request, host_id: str) -> str:
    configured = (os.environ.get(RESUME_WORKSPACE_ENV) or "").strip()
    if configured:
        return configured
    home = await _host_home(request, host_id)
    if home is None:
        raise _NoTarget(
            f"the host's home directory could not be resolved; set {RESUME_WORKSPACE_ENV}"
        )
    return home


def _message(text: str) -> dict[str, Any]:
    return {
        "type": "message",
        "data": {"role": "user", "content": [{"type": "input_text", "text": text}]},
    }


@router.post("/lot/{task_id}/session", response_model=None)
async def resume_session(request: Request, task_id: str) -> dict[str, Any] | JSONResponse:
    """Start a session for a parked task, with its Resume in Claude block as the first message.

    The message ends by asking the session to call ``complete_task`` with the
    task id when the work is finished (:func:`first_message`).

    The same two calls the web composer makes, through the loopback as the
    caller: ``POST /v1/sessions`` (agent, host, workspace, the native-wrapper
    labels and ``ae.task``), then the block as a user message on
    ``POST /v1/sessions/{id}/events``. Every failure answers with the block
    (``block``, ``fenced``) in the error body so the page can show it for a
    hand paste; a failure after the create also carries ``session_id``.
    """
    resumed = await _resume_block(task_id)
    if isinstance(resumed, JSONResponse):
        return resumed
    carry = {"block": resumed["block"], "fenced": resumed["fenced"]}
    rows = await asyncio.to_thread(sessions.list_top_level)
    live = _live_session(rows, labels.TASK, str(resumed["task_id"]))
    if live is not None:
        # A retry after a slow phone, or a second tap: open the session the
        # first one started instead of a duplicate (self-review 2026-09-25,
        # https://github.com/analyticendeavors/omnigent-ae/pull/41).
        return {**_existing(live), "task_id": str(resumed["task_id"]), **carry}
    try:
        agent = await _resume_agent(request)
        host = await _resume_host(request)
        workspace = await _resume_workspace(request, str(host["host_id"]))
    except _NoTarget as exc:
        return _error(503, CODE_RESUME_NO_TARGET, f"no session was started: {exc}", **carry)
    except httpx.HTTPError as exc:
        reason = _transport_failure(exc, "finding an agent, host and workspace")
        return _error(503, CODE_RESUME_NO_TARGET, f"no session was started: {reason}", **carry)

    task_ref = str(resumed["task_id"])
    session_labels = {labels.TASK: task_ref}
    wrapper = _NATIVE_WRAPPERS.get(str(agent.get("name")))
    if wrapper is not None:
        session_labels |= {_UI_MODE_LABEL: "terminal", _WRAPPER_LABEL: wrapper}
    create: dict[str, Any] = {
        "agent_id": agent["id"],
        "host_id": host["host_id"],
        "workspace": workspace,
        "labels": session_labels,
    }
    if resumed.get("title"):
        create["title"] = str(resumed["title"])[:200]
    try:
        created = await _loopback(request, "POST", "/v1/sessions", create)
    except httpx.HTTPError as exc:
        reason = _transport_failure(exc, "creating the session")
        return _error(
            502, CODE_RESUME_NOT_CREATED, f"creating the session failed: {reason}", **carry
        )
    snapshot = _json_or_none(created)
    session_id = snapshot.get("id") if isinstance(snapshot, dict) else None
    if created.status_code >= 300 or not isinstance(session_id, str):
        return _error(
            502,
            CODE_RESUME_NOT_CREATED,
            f"creating the session failed: {created.status_code} {created.text[:200]}",
            **carry,
        )
    # The task now has a live session; the lot's next read is a fresh one.
    forget_lot()

    events = f"/v1/sessions/{session_id}/events"
    try:
        posted = await _loopback(request, "POST", events, _message(first_message(resumed)))
        failure = None if posted.status_code < 300 else f"{posted.status_code} {posted.text[:200]}"
    except httpx.HTTPError as exc:
        failure = _transport_failure(exc, "posting the first message")
    if failure is not None:
        return _error(
            502,
            CODE_RESUME_NOT_POSTED,
            f"session {session_id} started but the block was not posted: {failure}",
            session_id=session_id,
            **carry,
        )
    return {
        "session_id": session_id,
        "task_id": task_ref,
        "title": resumed.get("title"),
        "host": {"id": host["host_id"], "name": host.get("name")},
        "agent": {"id": agent["id"], "name": agent.get("name")},
        "workspace": workspace,
        "labels": session_labels,
        **carry,
    }


# ── sessions the orchestrator starts ─────────────────────────────────────

# Statuses a retry reuses instead of starting a duplicate: the session is
# still the live piece of work (unset means created a moment ago, before the
# slot cap or the create's own label placed it). Review, parked and reference
# are finished or shelved, so asking again starts a fresh session.
_LIVE_STATUSES = (None, labels.WORKING, labels.BLOCKED)


def _live_session(rows: list[Conversation], key: str, value: str) -> Conversation | None:
    """The newest top-level session labelled *key* = *value* that is still live."""
    for conv in rows:
        if labels.label(conv.labels, key) == value and labels.status_of(conv.labels) in (
            _LIVE_STATUSES
        ):
            return conv
    return None


def _existing(conv: Conversation) -> dict[str, Any]:
    """The answer for a create that found its session already there."""
    return {
        "session_id": conv.id,
        "existing": True,
        "title": sessions.display_title(conv),
        "status": labels.status_of(conv.labels),
        "blocked": sessions.is_blocked(conv),
        "failed": sessions.is_failed(conv),
        "path": f"/c/{conv.id}",
        "labels": labels.ae_labels_without_notes(conv.labels),
    }


CODE_SLOTS_FULL = "slots_full"
CODE_SESSION_NO_TARGET = "session_no_target"
CODE_SESSION_NOT_CREATED = "session_not_created"
CODE_BRIEF_NOT_POSTED = "session_brief_not_posted"
# Which built-in agent each harness choice binds, the two the composer offers
# for a terminal-first session on the box.
HARNESS_AGENTS: dict[str, str] = {"claude": "claude-native-ui", "codex": "codex-native-ui"}
_KEY_MAX = 64


class SessionRequest(BaseModel):
    """``POST /v1/ae/sessions``: a session to start on a host, and its first message.

    ``workspace`` is the repository directory on the host: absolute, or
    relative to the host's home (``~/repos/x`` and ``repos/x`` are the same).
    ``key`` makes the create idempotent: a live session carrying the same
    ``ae.orch_key`` is returned instead of a second one. Left out, the key is
    derived from the harness, workspace and title.
    """

    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=200)
    brief: str = Field(min_length=1, max_length=100_000)
    harness: Literal["claude", "codex"] = "claude"
    workspace: str | None = Field(default=None, min_length=1, max_length=1024)
    host: str | None = Field(default=None, min_length=1, max_length=200)
    key: str | None = Field(default=None, min_length=1, max_length=_KEY_MAX)
    repo: str | None = Field(default=None, min_length=3, max_length=200)
    branch: str | None = Field(default=None, min_length=1, max_length=200)
    orchestrator: str | None = Field(default=None, min_length=1, max_length=100)
    ride_unparked: bool = False


def _session_key(body: SessionRequest) -> str:
    if body.key:
        return body.key.strip()
    seed = "\n".join((body.harness, body.workspace or "", " ".join(body.title.lower().split())))
    return "auto-" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16]


async def _session_workspace(request: Request, host_id: str, wanted: str | None) -> str:
    """*wanted* made absolute against the host's home, or the Resume default."""
    if not wanted:
        return await _resume_workspace(request, host_id)
    wanted = wanted.strip()
    if wanted.startswith("/"):
        return wanted.rstrip("/") or "/"
    home = await _host_home(request, host_id)
    if home is None:
        raise _NoTarget(
            f"{wanted} is relative and the host's home directory could not be resolved; "
            "give an absolute path"
        )
    rest = wanted[1:] if wanted.startswith("~") else wanted
    return f"{home.rstrip('/')}/{rest.lstrip('/')}".rstrip("/")


@router.get("/sessions")
async def orchestrated_sessions() -> dict[str, Any]:
    """Every live top-level session the orchestrator started, with repo, branch and key.

    What the orchestrator reads on each check-in to find the session that owns a
    pull request (by ``ae.repo`` and ``ae.branch``) and the ones to close out.
    """
    rows, limit = await asyncio.gather(
        asyncio.to_thread(sessions.list_top_level), asyncio.to_thread(slot_limit.effective_limit)
    )
    now = time.time()
    items = []
    for conv in rows:
        if labels.label(conv.labels, labels.ORCH) is None:
            continue
        summary = sessions.summarize(conv, now=now).as_dict()
        items.append(
            summary
            | {
                "orchestrator": labels.label(conv.labels, labels.ORCH),
                "key": labels.label(conv.labels, labels.ORCH_KEY),
                "repo": labels.label(conv.labels, labels.REPO),
                "branch": labels.label(conv.labels, labels.BRANCH),
                "workspace": conv.workspace,
                "path": f"/c/{conv.id}",
            }
        )
    working = sessions.working_sessions(rows)
    return {
        "limit": limit,
        "slots_used": len(working),
        "items": items,
    }


@router.post(
    "/sessions",
    response_model=None,
    dependencies=[Depends(require_json_content_type), Depends(require_trusted_origin)],
)
async def start_session(request: Request, body: SessionRequest) -> dict[str, Any] | JSONResponse:
    """Start a top-level session on a host and post its brief, holding a slot first.

    The orchestrator agent's ``create_session`` tool (2026-09-25). The calls are
    Resume's: agent, host and workspace resolved the composer's way, then
    ``POST /v1/sessions`` and the brief on ``POST /v1/sessions/{id}/events``,
    through the loopback as the caller. Before that, two checks:

    - Idempotent: a live session with the same ``ae.orch_key`` is returned as
      it is (``existing: true``) and nothing is posted.
    - The slots: the same count the slot cap makes (blocked and failed sessions
      hold none), taken under its lock. Full answers 409 with who holds them,
      unless ``ride_unparked``, which Reid chooses; the session then carries
      ``ae.overflow=1``. The session is created already ``working``, so the
      slot cap passes its first prompt without counting it twice.
    """
    key = _session_key(body)
    rows = await asyncio.to_thread(sessions.list_top_level)
    live = _live_session(rows, labels.ORCH_KEY, key)
    if live is not None:
        return _existing(live) | {"key": key}
    hold = f"orch:{key}"
    limit = await asyncio.to_thread(slot_limit.effective_limit)
    held, full, working, pending = slot_cap.reserve(hold, rows, limit, riding=body.ride_unparked)
    if not held:
        now = time.time()
        return _error(
            409,
            CODE_SLOTS_FULL,
            slot_cap.reason_text(
                [sessions.summarize(conv, now=now) for conv in working],
                limit,
                "block",
                None,
                pending=pending,
            ),
            working=[sessions.summarize(conv, now=now).as_dict() for conv in working],
            limit=limit,
        )
    try:
        return await _create_briefed(request, body, key, overflow=full)
    finally:
        slot_cap.release(hold)


async def _create_briefed(
    request: Request, body: SessionRequest, key: str, *, overflow: bool
) -> dict[str, Any] | JSONResponse:
    try:
        agent = await _resume_agent(request, HARNESS_AGENTS[body.harness])
        host = await _resume_host(request, body.host)
        workspace = await _session_workspace(request, str(host["host_id"]), body.workspace)
    except _NoTarget as exc:
        return _error(503, CODE_SESSION_NO_TARGET, f"no session was started: {exc}")
    except httpx.HTTPError as exc:
        reason = _transport_failure(exc, "finding an agent, host and workspace")
        return _error(503, CODE_SESSION_NO_TARGET, f"no session was started: {reason}")
    session_labels = {
        labels.STATUS: labels.WORKING,
        labels.STARTED: str(int(time.time())),
        labels.ORCH: body.orchestrator or "1",
        labels.ORCH_KEY: key,
        _UI_MODE_LABEL: "terminal",
        _WRAPPER_LABEL: _NATIVE_WRAPPERS[str(agent["name"])],
    }
    if body.repo:
        session_labels[labels.REPO] = body.repo.strip().lower()
    if body.branch:
        session_labels[labels.BRANCH] = body.branch.strip()
    if overflow:
        session_labels[labels.OVERFLOW] = "1"
    create = {
        "agent_id": agent["id"],
        "host_id": host["host_id"],
        "workspace": workspace,
        "labels": session_labels,
        "title": body.title.strip(),
    }
    try:
        created = await _loopback(request, "POST", "/v1/sessions", create)
    except httpx.HTTPError as exc:
        reason = _transport_failure(exc, "creating the session")
        return _error(502, CODE_SESSION_NOT_CREATED, f"creating the session failed: {reason}")
    snapshot = _json_or_none(created)
    session_id = snapshot.get("id") if isinstance(snapshot, dict) else None
    if created.status_code >= 300 or not isinstance(session_id, str):
        return _error(
            502,
            CODE_SESSION_NOT_CREATED,
            f"creating the session failed: {created.status_code} {created.text[:200]}",
        )
    try:
        posted = await _loopback(
            request, "POST", f"/v1/sessions/{session_id}/events", _message(body.brief)
        )
        failure = None if posted.status_code < 300 else f"{posted.status_code} {posted.text[:200]}"
    except httpx.HTTPError as exc:
        failure = _transport_failure(exc, "posting the brief")
    if failure is not None:
        return _error(
            502,
            CODE_BRIEF_NOT_POSTED,
            f"session {session_id} started but the brief was not posted: {failure}. "
            "Post it with post_message, or set the session to review.",
            session_id=session_id,
            path=f"/c/{session_id}",
        )
    return {
        "session_id": session_id,
        "existing": False,
        "key": key,
        "title": create["title"],
        "path": f"/c/{session_id}",
        "host": {"id": host["host_id"], "name": host.get("name")},
        "agent": {"id": agent["id"], "name": agent.get("name")},
        "workspace": workspace,
        "overflow": overflow,
        "labels": session_labels,
    }


@router.post("/gate/{session_id}/reply", response_model=None)
async def gate_reply(
    request: Request, session_id: str, body: GateReply
) -> dict[str, Any] | JSONResponse:
    """Answer an open gate: post the letter as the session's next user turn.

    The agent wrote the gate and waits for the call; the call is one word. It
    goes through ``POST /v1/sessions/{id}/events`` as the caller, the same way
    a typed reply would, then the session's ``ae.gate`` flips to ``answered``.
    """
    conv = await _conversation(session_id)
    if conv is None:
        return _not_found(f"no session {session_id}")
    gate = labels.label(conv.labels, labels.GATE)
    if gate != labels.GATE_OPEN:
        # A stale button or a double click must not drop a bare letter into a
        # session with nothing to answer.
        return _error(
            409,
            ErrorCode.CONFLICT,
            f"no open gate on session {session_id}: ae.gate is {gate or 'unset'}",
        )
    text = GATE_REPLY_TEXT[body.route]
    event = {
        "type": "message",
        "data": {"role": "user", "content": [{"type": "input_text", "text": text}]},
    }
    try:
        response = await _loopback_post(request, f"/v1/sessions/{session_id}/events", event)
    except httpx.HTTPError as exc:
        reason = _transport_failure(exc, "posting the reply")
        return _error(502, CODE_REPLY_NOT_POSTED, f"posting the reply failed: {reason}")
    if response.status_code >= 300:
        return _error(
            502,
            CODE_REPLY_NOT_POSTED,
            f"posting the reply failed: {response.status_code} {response.text[:200]}",
        )
    await _set_labels(session_id, {labels.GATE: labels.GATE_ANSWERED})
    your_move.forget_answer()
    return {"posted": True, "route": body.route, "text": text, "status": response.status_code}


def _github_not_configured() -> JSONResponse:
    return _error(503, prs.CODE_NOT_CONFIGURED, prs.NOT_CONFIGURED)


def _bad_repo(owner: str, repo: str) -> JSONResponse | None:
    if prs.valid_repo(owner, repo):
        return None
    return _error(400, ErrorCode.INVALID_INPUT, f"{owner}/{repo} is not a GitHub repository name")


@router.get("/prs")
async def pull_requests(
    request: Request, fresh: Annotated[bool, Query()] = False
) -> dict[str, Any]:
    """Every open pull request a session tracks, as a card; see :mod:`ae_omni_policies.prs`.

    ``fresh=true`` re-reads which sessions track what; the cards keep their
    minute-long cache either way (GitHub's rate limit).
    """

    async def fetch(path: str) -> httpx.Response:
        return await _loopback(request, "GET", path)

    return await prs.open_pull_requests(fetch, fresh=fresh)


@router.get("/prs/{owner}/{repo}/{number}", response_model=None)
async def pull_request(
    owner: str,
    repo: str,
    number: Annotated[int, Path(ge=1)],
    fresh: Annotated[bool, Query()] = False,
) -> dict[str, Any] | JSONResponse:
    """One pull request's card (the session's GitHub panel reads this per tracked PR)."""
    if (bad := _bad_repo(owner, repo)) is not None:
        return bad
    settings = github.GitHubSettings.from_env()
    if not settings.configured:
        return _github_not_configured()
    async with github.GitHubClient(settings) as client:
        try:
            return await prs.read_card(client, f"{owner}/{repo}", number, fresh=fresh)
        except github.GitHubError as exc:
            return _error(502, prs.CODE_GITHUB_FAILED, str(exc))


@router.get("/prs/{owner}/{repo}/{number}/compliance", response_model=None)
async def pull_request_compliance(
    owner: str,
    repo: str,
    number: Annotated[int, Path(ge=1)],
    fresh: Annotated[bool, Query()] = False,
) -> dict[str, Any] | JSONResponse:
    """The latest trusted compliance comment, loaded only when requested."""
    if (bad := _bad_repo(owner, repo)) is not None:
        return bad
    settings = github.GitHubSettings.from_env()
    if not settings.configured:
        return _github_not_configured()
    async with github.GitHubClient(settings) as client:
        try:
            report = await pr_compliance.read_report(
                client, f"{owner}/{repo}", number, fresh=fresh
            )
            return {"report": report}
        except github.GitHubError as exc:
            return _error(502, prs.CODE_GITHUB_FAILED, str(exc))


@router.post(
    "/prs/{owner}/{repo}/{number}/update-branch",
    response_model=None,
    dependencies=[Depends(require_json_content_type), Depends(require_trusted_origin)],
)
async def update_pull_request_branch(
    request: Request,
    owner: str,
    repo: str,
    number: Annotated[int, Path(ge=1)],
    body: UpdateBranchRequest,
    user: Annotated[str | None, Depends(require_ae_user)],
) -> dict[str, Any] | JSONResponse:
    """Request an update of a known behind branch. A 202 means pending only."""
    if user is None:
        return _error(401, ErrorCode.UNAUTHORIZED, "Updating a branch needs a signed-in user.")
    if (refused := await _admin_only(request, user, "update a pull request branch")) is not None:
        return refused
    if (bad := _bad_repo(owner, repo)) is not None:
        return bad
    settings = github.GitHubSettings.from_env()
    if not settings.configured:
        return _github_not_configured()
    full = f"{owner}/{repo}"
    async with github.GitHubClient(settings) as client:
        try:
            pull = await client.pull(full, number)
        except github.GitHubError as exc:
            return _error(502, prs.CODE_GITHUB_FAILED, str(exc))
        if (pull.get("head") or {}).get("sha") != body.sha:
            prs.forget(full, number)
            card = await _ready_error_card(client, full, number)
            return _error(
                409,
                prs.CODE_HEAD_MOVED,
                "New commits arrived since the card was read; refresh before updating.",
                **({"card": card} if card is not None else {}),
            )
        gate = prs.update_branch_gate(pull)
        if not gate["allowed"]:
            prs.forget(full, number)
            return _error(409, prs.CODE_UPDATE_BLOCKED, gate["reason"])
        try:
            outcome = await client.update_branch(full, number, body.sha)
        except github.GitHubError as exc:
            return _error(502, prs.CODE_GITHUB_FAILED, str(exc))
        finally:
            # Even a timeout can follow a successful write on GitHub's side.
            prs.forget(full, number)
            your_move.reset_cache()
        if not outcome.accepted:
            card = await _ready_error_card(client, full, number)
            hint = f"; {prs.MERGE_SCOPES_HINT}" if outcome.status in (403, 404) else ""
            code = prs.CODE_UPDATE_REFUSED
            if card is not None and card.get("head_sha") != body.sha:
                code = prs.CODE_HEAD_MOVED
            status = 409 if outcome.status in (405, 409, 422) else 502
            return _error(
                status,
                code,
                f"GitHub did not accept the branch update: {outcome.message}{hint}",
                **({"card": card} if card is not None else {}),
            )
    _logger.info("branch update accepted %s#%s for %s", full, number, user)
    return JSONResponse(
        status_code=202,
        content={"accepted": True, "message": outcome.message, "head_sha": body.sha},
    )


@router.post(
    "/prs/{owner}/{repo}/{number}/ready",
    response_model=None,
    dependencies=[Depends(require_json_content_type), Depends(require_trusted_origin)],
)
async def ready_pull_request(
    request: Request,
    owner: str,
    repo: str,
    number: Annotated[int, Path(ge=1)],
    body: ReadyRequest,
    user: Annotated[str | None, Depends(require_ae_user)],
) -> dict[str, Any] | JSONResponse:
    """Mark an open draft ready after checking the displayed head on a fresh read."""
    if user is None:
        return _error(401, ErrorCode.UNAUTHORIZED, "Review needs a signed-in user.")
    if (refused := await _admin_only(request, user, "mark a pull request ready")) is not None:
        return refused
    if (bad := _bad_repo(owner, repo)) is not None:
        return bad
    settings = github.GitHubSettings.from_env()
    if not settings.configured:
        return _github_not_configured()
    full = f"{owner}/{repo}"
    async with github.GitHubClient(settings) as client:
        try:
            pull = await client.pull(full, number)
        except github.GitHubError as exc:
            return _error(502, prs.CODE_GITHUB_FAILED, str(exc))
        head_sha = (pull.get("head") or {}).get("sha")
        if head_sha != body.sha:
            card = await _ready_error_card(client, full, number)
            return _error(
                409,
                prs.CODE_HEAD_MOVED,
                "New commits arrived since the card was read; look again, then mark ready.",
                **({"card": card} if card is not None else {}),
            )
        if prs.pull_state(pull) != "open":
            return _error(
                409, prs.CODE_READY_BLOCKED, "Only an open pull request can be marked ready."
            )
        if pull.get("draft"):
            node_id = pull.get("node_id")
            if not isinstance(node_id, str) or not node_id:
                return _error(
                    502, prs.CODE_GITHUB_FAILED, "GitHub did not provide the pull request ID."
                )
            try:
                await client.mark_ready(node_id)
            except github.GitHubError as exc:
                return _error(502, prs.CODE_READY_REFUSED, str(exc))
        prs.forget(full, number)
        try:
            card = await prs.read_card(client, full, number, fresh=True)
        except github.GitHubError as exc:
            return _error(502, prs.CODE_GITHUB_FAILED, str(exc))
    _logger.info("marked ready %s#%s for %s", full, number, user)
    return card


async def _ready_error_card(
    client: github.GitHubClient, repo: str, number: int
) -> dict[str, Any] | None:
    try:
        return await prs.read_card(client, repo, number, fresh=True)
    except github.GitHubError:
        return None


@router.post(
    "/prs/{owner}/{repo}/{number}/merge",
    response_model=None,
    dependencies=[Depends(require_json_content_type), Depends(require_trusted_origin)],
)
async def merge_pull_request(
    request: Request,
    owner: str,
    repo: str,
    number: Annotated[int, Path(ge=1)],
    body: MergeRequest,
    user: Annotated[str | None, Depends(require_ae_user)],
) -> dict[str, Any] | JSONResponse:
    """Merge one pull request, after re-checking the gate on a fresh read.

    Refused with a 409 and the fresh card when the gate is shut (CI red or
    running, conflicted, blocked, changes requested) or the head moved past
    ``sha``; GitHub's own refusal is a 409 with its sentence. Never retried.
    """
    if user is None:
        return _error(401, ErrorCode.UNAUTHORIZED, "Merging needs a signed-in user.")
    if (refused := await _admin_only(request, user, "merge pull requests")) is not None:
        return refused
    if (bad := _bad_repo(owner, repo)) is not None:
        return bad
    settings = github.GitHubSettings.from_env()
    if not settings.configured:
        return _github_not_configured()
    full = f"{owner}/{repo}"
    async with github.GitHubClient(settings) as client:
        try:
            card = await prs.read_card(client, full, number, fresh=True)
        except github.GitHubError as exc:
            return _error(502, prs.CODE_GITHUB_FAILED, str(exc))
        if not card["merge"]["allowed"]:
            return _error(409, prs.CODE_MERGE_BLOCKED, str(card["merge"]["reason"]), card=card)
        if card["head_sha"] != body.sha:
            return _error(
                409,
                prs.CODE_HEAD_MOVED,
                "New commits arrived since the card was read; look again, then merge.",
                card=card,
            )
        method = body.method or card["merge"]["default_method"]
        if method not in card["merge"]["methods"]:
            return _error(
                400,
                ErrorCode.INVALID_INPUT,
                f"{full} does not allow {method} merges; it allows "
                f"{', '.join(card['merge']['methods'])}.",
            )
        try:
            outcome = await client.merge(full, number, method, body.sha)
        except github.GitHubError as exc:
            return _error(502, prs.CODE_GITHUB_FAILED, str(exc))
    prs.forget(full, number)
    your_move.reset_cache()
    if not outcome.merged:
        hint = f"; {prs.MERGE_SCOPES_HINT}" if outcome.status in (403, 404) else ""
        code = 409 if outcome.status in (405, 409, 422) else 502
        return _error(
            code, prs.CODE_MERGE_REFUSED, f"GitHub did not merge: {outcome.message}{hint}"
        )
    _logger.info("merged %s#%s (%s) for %s", full, number, method, user)
    answer: dict[str, Any] = {
        "merged": True,
        "repo": full.lower(),
        "number": number,
        "method": method,
        "sha": outcome.sha,
        "message": outcome.message,
        "session": None,
    }
    if body.session_id is not None:
        conv = await _conversation(body.session_id)
        if conv is not None:
            status = labels.status_of(conv.labels)
            answer["session"] = {
                "id": conv.id,
                "status": status,
                # Where upstream's labels PATCH can take it: anything not already
                # in review or closed as reference.
                "offer_review": status not in (labels.REVIEW, labels.REFERENCE),
            }
    return answer


def _fabric_failure(exc: fabric.FabricError) -> JSONResponse:
    # The message is built from the capacity's name and status only (fabric.py).
    return _error(exc.status, exc.code, str(exc))


@router.get("/fabric")
async def fabric_view(
    request: Request, user: Annotated[str | None, Depends(require_ae_user)]
) -> dict[str, Any]:
    """Every Fabric capacity the environment names, with its state (:mod:`.fabric`).

    Always a 200: a capacity whose Logic App fails carries ``error`` on its own
    row. With none configured, ``note`` says how to turn the page on.
    ``can_act`` says whether the caller may use the actions, so the page hides
    them from a non-admin (2026-10-22; the action routes refuse one anyway).
    """
    answer, can_act = await asyncio.gather(
        fabric.overview(), shared_accounts_routes.admin_probe(request, user)
    )
    return {**answer, "can_act": can_act}


_FABRIC_ACTION_WORDS = {
    "wake": "wake a Fabric capacity",
    "suspend": "turn off a Fabric capacity",
    "keepawake": "keep a Fabric capacity awake",
    "scale": "resize a Fabric capacity",
}


async def _fabric_action(
    request: Request,
    name: str,
    action: Literal["wake", "suspend", "keepawake", "scale"],
    user: str | None,
    hours: int | None = None,
    sku: str | None = None,
) -> dict[str, Any] | JSONResponse:
    if user is None:
        return _error(
            401,
            ErrorCode.UNAUTHORIZED,
            "Waking or turning off a Fabric capacity needs a signed-in user. "
            "Sign in again, then retry.",
        )
    # Every action changes the bill, so admin only (2026-10-22, public issues 4 and 19).
    if (refused := await _admin_only(request, user, _FABRIC_ACTION_WORDS[action])) is not None:
        return refused
    if action != "keepawake":
        # Pause, Start and resize go through the shared Logic App when it is set
        # up (fabric.py, 2026-10-21); keep awake stays on the capacity's own.
        shared = fabric.fleet()
        try:
            if action == "scale":
                fabric.check_sku(name, sku)
                if shared is None:
                    return _error(
                        409, fabric.CODE_FLEET_NOT_CONFIGURED, fabric.FLEET_NOT_CONFIGURED
                    )
            if shared is not None:
                answer = await fabric.fleet_act(shared, name, action, sku=sku)
                _logger.info(
                    "fabric %s%s on %s through the fleet: %s (changed %s) for %s",
                    action,
                    f" to {sku}" if action == "scale" else "",
                    answer["name"],
                    answer["state"],
                    answer["changed"],
                    user,
                )
                return answer
        except fabric.FabricError as exc:
            # When nothing can have been written (the list failed or missed the
            # capacity, or the fleet refused the key or the capacity first), a
            # capacity with its own wake Logic App still starts and pauses
            # through that, as GET still shows its row from it. A write that
            # timed out or answered 502 may have gone out: never a second try.
            own = fabric.find(name)
            fallback = (
                exc.unsent
                and action != "scale"
                and own is not None
                and (action == "wake" or own.can_suspend)
            )
            if not fallback:
                _logger.warning("fabric %s on %s failed for %s: %s", action, name, user, exc)
                if exc.status == 404:
                    return _not_found(str(exc))
                return _fabric_failure(exc)
            _logger.warning(
                "fabric %s on %s: the fleet failed before writing (%s); "
                "using the capacity's own Logic App for %s",
                action,
                name,
                exc,
                user,
            )
    capacity = fabric.find(name)
    if capacity is None:
        return _not_found(
            f"No Fabric capacity is called {name!r} here; the page lists the ones the "
            "server has a wake URL for."
        )
    if action == "suspend" and not capacity.can_suspend:
        return _error(
            409, fabric.CODE_SUSPEND_NOT_CONFIGURED, fabric.suspend_not_configured(capacity)
        )
    if action == "keepawake" and not capacity.can_keep_awake:
        return _error(
            409,
            fabric.CODE_SUSPEND_NOT_CONFIGURED,
            fabric.suspend_not_configured(capacity, "Keep awake"),
        )
    try:
        answer = await fabric.act(capacity, action, hours=hours)
    except fabric.FabricError as exc:
        _logger.warning("fabric %s on %s failed for %s: %s", action, capacity.name, user, exc)
        return _fabric_failure(exc)
    _logger.info(
        "fabric %s on %s: %s (changed %s) for %s",
        action,
        capacity.name,
        answer["state"],
        answer["changed"],
        user,
    )
    return answer


@router.post(
    "/fabric/{name}/wake",
    response_model=None,
    dependencies=[Depends(require_json_content_type), Depends(require_trusted_origin)],
)
async def fabric_wake(
    request: Request, name: str, user: Annotated[str | None, Depends(require_ae_user)]
) -> dict[str, Any] | JSONResponse:
    """Wake one capacity (the Logic App resumes it only when Paused); never retried.

    404 for a name the environment does not configure; a Logic App failure is a
    502, its timeout a 504, each with a sentence that names no secret.
    """
    return await _fabric_action(request, name, "wake", user)


@router.post(
    "/fabric/{name}/suspend",
    response_model=None,
    dependencies=[Depends(require_json_content_type), Depends(require_trusted_origin)],
)
async def fabric_suspend(
    request: Request, name: str, user: Annotated[str | None, Depends(require_ae_user)]
) -> dict[str, Any] | JSONResponse:
    """Turn one capacity off (the Logic App suspends it only when Active); never retried.

    409 when the capacity has no suspend key, so holders of the shared wake
    link alone can never turn it off; otherwise as :func:`fabric_wake`.
    """
    return await _fabric_action(request, name, "suspend", user)


@router.post(
    "/fabric/{name}/keepawake",
    response_model=None,
    dependencies=[Depends(require_json_content_type), Depends(require_trusted_origin)],
)
async def fabric_keep_awake(
    request: Request,
    name: str,
    body: KeepAwakeRequest,
    user: Annotated[str | None, Depends(require_ae_user)],
) -> dict[str, Any] | JSONResponse:
    """Keep one capacity up for ``hours`` more (the Logic App acts only when Active).

    Needs the suspend key like Off (409 without it); ``hours`` outside 1 to 12
    is a 422; otherwise as :func:`fabric_wake`. Like Wake and Off, it answers
    the capacity's full row as ``GET`` shows it, with the new ``keepAwakeUntil``,
    ``sleepsAt`` and ``keepAwakeOptions`` (fabric.py ``act``).
    """
    return await _fabric_action(request, name, "keepawake", user, body.hours)


@router.post(
    "/fabric/{name}/scale",
    response_model=None,
    dependencies=[Depends(require_json_content_type), Depends(require_trusted_origin)],
)
async def fabric_scale(
    request: Request,
    name: str,
    body: ScaleRequest,
    user: Annotated[str | None, Depends(require_ae_user)],
) -> dict[str, Any] | JSONResponse:
    """Resize one capacity to ``sku`` through the shared capacities Logic App; never retried.

    A size outside ``fabric.SKU_CHOICES`` is a 400 before any call; without
    the fleet's URL and key a 409 that says how to add them; a name the list
    does not have is a 404, and one two capacities share a 409. Billing
    changes at once, so the page asks first. Answers the full row like Wake.
    """
    return await _fabric_action(request, name, "scale", user, sku=body.sku)


def _databricks_failure(exc: databricks.DatabricksError) -> JSONResponse:
    # The message is scrubbed of the client id, the secret and the tokens (databricks.py).
    return _error(exc.status, exc.code, str(exc))


def _databricks_not_configured() -> JSONResponse:
    return _error(409, databricks.CODE_NOT_CONFIGURED, databricks.not_configured_note())


@router.get("/azure-cost", response_model=None)
async def azure_cost_view(
    request: Request,
    user: Annotated[str | None, Depends(require_ae_user)],
) -> dict[str, Any] | JSONResponse:
    """This month's Azure spend across the configured subscriptions; admins only.

    Never waits on Azure: it answers the cache and starts a background reading
    when the cache is over an hour old (:mod:`.azure_cost`). Without a service
    principal, ``note`` says how to turn the card on. Admins only since
    2026-10-22: tenant spend and resource names are not for every signed-in user.
    """
    if (refused := await _admin_only(request, user, "see Azure spend")) is not None:
        return refused
    return await azure_cost.overview()


@router.get("/databricks")
async def databricks_view(
    request: Request, user: Annotated[str | None, Depends(require_ae_user)]
) -> dict[str, Any]:
    """The Databricks workspace's warehouses and spend (:mod:`.databricks`).

    Always a 200: a listing that fails rides in ``error``. With no credential,
    ``note`` says how to turn the section on. Starts a background spend
    reading only when the warehouse it runs on is already RUNNING and the last
    attempt is stale; it never starts a warehouse. ``can_act`` as on ``/fabric``.
    """
    answer, can_act = await asyncio.gather(
        databricks.overview(), shared_accounts_routes.admin_probe(request, user)
    )
    return {**answer, "can_act": can_act}


async def _databricks_action(
    request: Request, warehouse_id: str, action: databricks.Action, user: str | None
) -> dict[str, Any] | JSONResponse:
    if user is None:
        return _error(
            401,
            ErrorCode.UNAUTHORIZED,
            "Starting or stopping a Databricks warehouse needs a signed-in user. "
            "Sign in again, then retry.",
        )
    # A running warehouse bills, so admin only (2026-10-22, public issue 4).
    if (
        refused := await _admin_only(request, user, f"{action} a Databricks warehouse")
    ) is not None:
        return refused
    ws = databricks.workspace()
    if ws is None:
        return _databricks_not_configured()
    if not databricks.WAREHOUSE_ID.match(warehouse_id):
        return _not_found(
            f"No Databricks warehouse has the id {warehouse_id!r} here; an id is 16 hex "
            "characters, as the page lists them."
        )
    try:
        row = await databricks.act(ws, warehouse_id, action)
    except databricks.DatabricksError as exc:
        _logger.warning("databricks %s on %s failed for %s: %s", action, warehouse_id, user, exc)
        return _databricks_failure(exc)
    _logger.info("databricks %s on %s: %s for %s", action, warehouse_id, row["state"], user)
    return row


@router.post(
    "/databricks/warehouses/{warehouse_id}/start",
    response_model=None,
    dependencies=[Depends(require_json_content_type), Depends(require_trusted_origin)],
)
async def databricks_start(
    request: Request, warehouse_id: str, user: Annotated[str | None, Depends(require_ae_user)]
) -> dict[str, Any] | JSONResponse:
    """Start one warehouse; it bills until it auto stops. Never retried.

    404 for an id the workspace does not list, 409 without a credential, a
    Databricks failure 502 and its timeout 504, each with a sentence that names
    no secret. Answers the warehouse's row after the call.
    """
    return await _databricks_action(request, warehouse_id, "start", user)


@router.post(
    "/databricks/warehouses/{warehouse_id}/stop",
    response_model=None,
    dependencies=[Depends(require_json_content_type), Depends(require_trusted_origin)],
)
async def databricks_stop(
    request: Request, warehouse_id: str, user: Annotated[str | None, Depends(require_ae_user)]
) -> dict[str, Any] | JSONResponse:
    """Stop one warehouse; otherwise as :func:`databricks_start`."""
    return await _databricks_action(request, warehouse_id, "stop", user)


@router.post(
    "/databricks/spend/refresh",
    response_model=None,
    dependencies=[Depends(require_json_content_type), Depends(require_trusted_origin)],
)
async def databricks_refresh(
    request: Request,
    user: Annotated[str | None, Depends(require_ae_user)],
) -> dict[str, Any] | JSONResponse:
    """Read spend now, in the background; a query on a stopped warehouse wakes it.

    Answers the ``spend`` object (``reading`` while it runs; a refresh during a
    reading joins it). 409 without a credential or a serverless warehouse to
    read on; the listing's failure 502 or 504.
    """
    if user is None:
        return _error(
            401,
            ErrorCode.UNAUTHORIZED,
            "Reading Databricks spend needs a signed-in user. Sign in again, then retry.",
        )
    # The read can wake a stopped warehouse, so admin only (2026-10-22, public issue 4).
    if (refused := await _admin_only(request, user, "refresh Databricks spend")) is not None:
        return refused
    ws = databricks.workspace()
    if ws is None:
        return _databricks_not_configured()
    try:
        spend = await databricks.refresh(ws, databricks.credit())
    except databricks.DatabricksError as exc:
        _logger.warning("databricks spend refresh failed for %s: %s", user, exc)
        return _databricks_failure(exc)
    _logger.info("databricks spend refresh on %s for %s", spend["warehouseId"], user)
    return spend


# Consumed by ``create_app(debug_router_modules=...)``. The shared Claude accounts
# (2026-10-20) are a router of their own: their token route reads identity itself,
# without the cookie, so it cannot sit behind this router's blanket dependency.
DEBUG_ROUTERS: list[tuple[APIRouter, str, list[str]]] = [
    (router, "/v1/ae", ["ae"]),
    (shared_accounts_routes.router, "/v1/ae", ["ae"]),
    # No routes: included for its lifespan, which runs the merge watcher (2026-10-21).
    (merge_watch.lifespan_router, "", ["ae"]),
    # No routes: loads the Azure card's saved reading at boot (2026-10-22).
    (azure_cost.lifespan_router, "", ["ae"]),
]

__all__ = [
    "DEBUG_ROUTERS",
    "GATE_REPLY_TEXT",
    "GateReply",
    "KeepAwakeRequest",
    "LinkRequest",
    "MergeRequest",
    "ParkRequest",
    "router",
]
