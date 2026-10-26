"""The Admin Center's read-only "Git repos" card (2026-10-24): ``GET /v1/ae/git``.

One answer for every repo across several GitHub accounts: open pull requests with
merge state, open Dependabot alerts split into "fix" (a copy inside the vulnerable
range is installed on the default branch, or we cannot tell) and "waiting" (every
installed copy is already outside the range, so GitHub only has to catch up),
branches with no open or merged pull request, and the last Dependency Sweep runs.

Everything is read only and every call is bounded by :data:`REQUEST_TIMEOUT_S`.
The contract (shape, field rules, size cap) is ``.agents/contract.md`` of the
feature branch; the field rules are repeated where the code enforces them.

Configuration, all from the server environment (no real names ship in the repo):

- ``AE_GIT_SWEEP_CONFIG``: ``owner/repo:path`` of a Dependency Sweep config JSON
  (``repos`` and ``catchUpBranches`` are used), read from that repo's default
  branch on every load.
- ``AE_GIT_REPOS``: comma-separated ``owner/repo`` added to that list.
- ``AE_GIT_VIEW_ONLY``: comma-separated ``owner/repo`` shown but left out of totals.
- ``AE_GIT_SWEEP_WORKFLOW``: workflow file name, default ``dependency-sweep.yml``.
- ``AE_GIT_KEEP_BRANCHES``: comma-separated ``owner/repo:branch`` kept on purpose; they
  stay listed (``kept: true``) but are not old branches (2026-10-24).
- ``AE_GIT_TOKEN_<OWNER>``: a read-only token per owner (owner upper-cased, every
  run of non-alphanumerics as ``_``); an owner without one falls back to
  ``AE_GITHUB_TOKEN``. Secrets: never logged, never returned.
- ``AE_GITHUB_API_URL``: the API origin, as :mod:`ae_omni_policies.github`.

The answer is cached with :class:`~ae_omni_policies.swr.SwrCache` (300 s). Lockfile
reads are cached by (repo, manifest path, default-branch commit), so a quiet repo
costs no lockfile calls on the next load.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote

import httpx

from ae_omni_policies import github
from ae_omni_policies.swr import SwrCache

_logger = logging.getLogger(__name__)

TTL_S = 300.0
REQUEST_TIMEOUT_S = github.REQUEST_TIMEOUT_S
GRAPHQL_TIMEOUT_S = 25.0
CONCURRENCY = 6
MAX_LOCKFILE_BYTES = 20 * 1024 * 1024
TITLE_MAX = 120
MAX_PRS = 50
MAX_ALERTS = 100
MAX_BRANCHES = 50
# The extension bridge refuses 32,768 characters; stay well under.
SIZE_BUDGET = 28_000
# A first load that failed for every repo is remembered this long, so card opens do not
# each re-read every repo while GitHub or a token is down (2026-10-24).
FAILED_MEMORY_S = 60.0

SWEEP_CONFIG_ENV = "AE_GIT_SWEEP_CONFIG"
REPOS_ENV = "AE_GIT_REPOS"
VIEW_ONLY_ENV = "AE_GIT_VIEW_ONLY"
WORKFLOW_ENV = "AE_GIT_SWEEP_WORKFLOW"
KEEP_ENV = "AE_GIT_KEEP_BRANCHES"
TOKEN_PREFIX = "AE_GIT_TOKEN_"
DEFAULT_WORKFLOW = "dependency-sweep.yml"

NOT_CONFIGURED = (
    "The Git repos card is off: set AE_GIT_SWEEP_CONFIG (owner/repo:path of a Dependency "
    "Sweep config) and/or AE_GIT_REPOS (comma-separated owner/repo) in the server's "
    "environment, then restart the stack."
)

_SEVERITY_ORDER = {"CRITICAL": 0, "HIGH": 1, "MODERATE": 2, "LOW": 3}

_QUERY = """
query($owner: String!, $name: String!) {
  repository(owner: $owner, name: $name) {
    defaultBranchRef { name target { oid } }
    pushedAt
    isArchived
    pullRequests(states: OPEN, first: 50) {
      nodes {
        number title url isDraft mergeStateStatus author { login } updatedAt headRefName
        commits(last: 1) { nodes { commit { statusCheckRollup { state } } } }
      }
    }
    refs(refPrefix: "refs/heads/", first: 100) {
      nodes {
        name
        target { ... on Commit { committedDate } }
        associatedPullRequests(first: 5, states: [OPEN, MERGED]) { totalCount }
      }
    }
    vulnerabilityAlerts(states: OPEN, first: 100) {
      nodes {
        number createdAt vulnerableManifestPath
        securityVulnerability {
          severity vulnerableVersionRange
          firstPatchedVersion { identifier }
          package { name ecosystem }
        }
      }
    }
  }
}
"""

try:  # packaging ships in the server's environment; the fallback keeps the card honest without it
    from packaging.version import InvalidVersion
    from packaging.version import Version as _PepVersion
except ImportError:  # pragma: no cover - exercised only where packaging is absent
    _PepVersion = None  # type: ignore[assignment,misc]

    class InvalidVersion(ValueError):  # type: ignore[no-redef]
        pass


# ── settings ──────────────────────────────────────────────────────────────────


def owner_env_name(owner: str) -> str:
    """``fabrikam-labs`` to ``AE_GIT_TOKEN_FABRIKAM_LABS``."""
    return TOKEN_PREFIX + re.sub(r"[^A-Za-z0-9]+", "_", owner).upper()


def _split_repos(raw: str | None) -> list[str]:
    return [item.strip() for item in (raw or "").split(",") if github._REPO.match(item.strip())]


class Settings:
    """What to read and with which tokens; all from the environment."""

    def __init__(self, environ: Mapping[str, str] | None = None) -> None:
        env = os.environ if environ is None else environ
        self._env = env
        self.api_url = (env.get(github.API_URL_ENV) or "").strip().rstrip("/") or github.API_URL
        raw = (env.get(SWEEP_CONFIG_ENV) or "").strip()
        self.sweep_repo: str | None = None
        self.sweep_path: str | None = None
        repo, _, path = raw.partition(":")
        if github._REPO.match(repo.strip()) and path.strip():
            self.sweep_repo, self.sweep_path = repo.strip(), path.strip().lstrip("/")
        self.extra_repos = _split_repos(env.get(REPOS_ENV))
        self.view_only = _split_repos(env.get(VIEW_ONLY_ENV))
        self.workflow = (env.get(WORKFLOW_ENV) or "").strip() or DEFAULT_WORKFLOW
        self.sweep_set = bool(raw)
        self.kept: set[tuple[str, str]] = set()
        for item in (env.get(KEEP_ENV) or "").split(","):
            kept_repo, _, kept_branch = item.strip().partition(":")
            if github._REPO.match(kept_repo.strip()) and kept_branch.strip():
                self.kept.add((kept_repo.strip().lower(), kept_branch.strip()))

    def is_kept(self, repo: str, branch: str) -> bool:
        return (repo.lower(), branch) in self.kept

    @property
    def configured(self) -> bool:
        return self.sweep_set or bool(self.extra_repos) or bool(self.view_only)

    def token_for(self, owner: str) -> str:
        own = (self._env.get(owner_env_name(owner)) or "").strip()
        return own or (self._env.get(github.TOKEN_ENV) or "").strip()

    def no_token_note(self, owner: str) -> str:
        return f"No token: set {owner_env_name(owner)} in the server's secrets file."

    @property
    def graphql_url(self) -> str:
        # GitHub Enterprise Server: https://HOST/api/v3 -> https://HOST/api/graphql.
        if self.api_url.endswith("/api/v3"):
            return self.api_url[: -len("v3")] + "graphql"
        return self.api_url + "/graphql"


# ── version ranges ────────────────────────────────────────────────────────────

_SEMVER = re.compile(
    r"^v?(\d+)(?:\.(\d+))?(?:\.(\d+))?(?:-([0-9A-Za-z.-]+))?(?:\+[0-9A-Za-z.-]+)?$"
)
_COMPARATOR = re.compile(r"^(>=|<=|>|<|=)?\s*(\S+)$")


def _npm_key(text: str) -> tuple[Any, ...] | None:
    m = _SEMVER.match(text.strip())
    if m is None:
        return None
    nums = tuple(int(g or 0) for g in m.group(1, 2, 3))
    if m.group(4) is None:
        return (*nums, (1,))
    ids = tuple((0, int(i), "") if i.isdigit() else (1, 0, i) for i in m.group(4).split("."))
    return (*nums, (0, ids))


def _numeric_key(text: str) -> tuple[int, ...] | None:
    nums = re.findall(r"\d+", text)
    if not nums:
        return None
    return tuple(int(n) for n in nums) + (0,) * max(0, 4 - len(nums))


def _version_key(ecosystem: str, text: str) -> Any:
    """A comparable for *text*, or ``None`` when it cannot be read."""
    if ecosystem == "NPM":
        return _npm_key(text)
    if _PepVersion is not None:
        try:
            return _PepVersion(text.strip())
        except InvalidVersion:
            return None
    return _numeric_key(text)


def in_range(ecosystem: str, version: str, version_range: str) -> bool | None:
    """Whether *version* satisfies GitHub's range text (``>= 5.0.0-beta.0, < 7.1.0``).

    ``None`` when the range or the version cannot be read.
    """
    key = _version_key(ecosystem, version)
    if key is None or not version_range.strip():
        return None
    for part in version_range.split(","):
        m = _COMPARATOR.match(part.strip())
        if m is None:
            return None
        bound = _version_key(ecosystem, m.group(2))
        if bound is None:
            return None
        op = m.group(1) or "="
        try:
            ok = {
                ">=": key >= bound,
                "<=": key <= bound,
                ">": key > bound,
                "<": key < bound,
                "=": key == bound,
            }[op]
        except TypeError:
            return None
        if not ok:
            return False
    return True


# ── lockfile parsing ──────────────────────────────────────────────────────────

Index = dict[str, list[str]]


def parse_npm_lock(text: str) -> Index | None:
    """Every installed version by package name from a lockfile v2/v3 ``packages`` map."""
    try:
        data = json.loads(text)
    except ValueError:
        return None
    packages = data.get("packages") if isinstance(data, dict) else None
    if not isinstance(packages, dict):
        return None
    index: Index = {}
    marker = "node_modules/"
    for key, entry in packages.items():
        at = key.rfind(marker)
        if at < 0 or not isinstance(entry, dict):
            continue
        version = entry.get("version")
        if isinstance(version, str):
            versions = index.setdefault(key[at + len(marker) :], [])
            if version not in versions:
                versions.append(version)
    return index


def normalize_pip(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


_PIN = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)\s*(?:\[[^\]]*\])?\s*==\s*([^\s;#]+)")


def parse_requirements(text: str) -> Index:
    index: Index = {}
    for line in text.splitlines():
        m = _PIN.match(line.strip())
        if m:
            versions = index.setdefault(normalize_pip(m.group(1)), [])
            if m.group(2) not in versions:
                versions.append(m.group(2))
    return index


def lockfile_path(ecosystem: str, manifest: str) -> str | None:
    """The file that pins versions for an alert's manifest, or ``None`` when unsupported."""
    base = manifest.rsplit("/", 1)[-1]
    folder = manifest[: len(manifest) - len(base)]
    if ecosystem == "NPM":
        if base in ("package.json", "package-lock.json"):
            return folder + "package-lock.json"
        return None
    if ecosystem == "PIP" and re.fullmatch(r"requirements[^/]*\.txt", base):
        return manifest
    return None


# ── HTTP ──────────────────────────────────────────────────────────────────────


class _Fail(Exception):
    """A read that failed; the message is safe for the page (no token, no header)."""


class _NotFound(_Fail):
    """GitHub answered 404: a final answer, safe to remember for that commit."""


class _TooLarge(_Fail):
    """A file over the size cap: a final answer for that commit."""


def _status_fail(status: int, what: str, owner: str) -> _Fail:
    note = _status_note(status, what, owner)
    return _NotFound(note) if status == 404 else _Fail(note)


def _headers(token: str, accept: str = "application/vnd.github+json") -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": accept,
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": github.USER_AGENT,
    }


def _status_note(status: int, what: str, owner: str) -> str:
    if status == 401:
        return (
            f"GitHub refused the token for {owner} (401): it is expired or wrong; "
            f"replace {owner_env_name(owner)} (or AE_GITHUB_TOKEN)."
        )
    if status == 403:
        return (
            f"GitHub refused {what} (403): the token lacks the permission or is rate "
            "limited; check it can read this repo (Metadata, Contents, Pull requests, "
            "Dependabot alerts)."
        )
    if status == 404:
        return f"GitHub could not find {what} (404): check the name and that the token can see it."
    return f"GitHub answered {status} for {what}; try again later."


async def _get_json(client: httpx.AsyncClient, url: str, token: str, what: str, owner: str) -> Any:
    try:
        response = await client.get(url, headers=_headers(token), timeout=REQUEST_TIMEOUT_S)
    except httpx.HTTPError as exc:
        raise _Fail(f"GitHub did not answer for {what} ({type(exc).__name__}).") from exc
    if response.status_code != 200:
        raise _status_fail(response.status_code, what, owner)
    try:
        return response.json()
    except ValueError as exc:
        raise _Fail(f"GitHub sent something unreadable for {what}.") from exc


async def _get_raw(
    client: httpx.AsyncClient, url: str, token: str, what: str, owner: str, limit: int
) -> str:
    """A file's raw text, stopping at *limit* bytes."""
    chunks: list[bytes] = []
    size = 0
    try:
        async with client.stream(
            "GET",
            url,
            headers=_headers(token, "application/vnd.github.raw"),
            timeout=REQUEST_TIMEOUT_S,
        ) as response:
            if response.status_code != 200:
                raise _status_fail(response.status_code, what, owner)
            async for chunk in response.aiter_bytes():
                size += len(chunk)
                if size > limit:
                    raise _TooLarge(f"{what} is over {limit // (1024 * 1024)} MB.")
                chunks.append(chunk)
    except httpx.HTTPError as exc:
        raise _Fail(f"GitHub did not answer for {what} ({type(exc).__name__}).") from exc
    return b"".join(chunks).decode("utf-8", errors="replace")


def _cut(text: str | None, limit: int) -> str:
    text = text or ""
    return text if len(text) <= limit else text[: limit - 1] + "…"


# ── the load ──────────────────────────────────────────────────────────────────

# (repo lower, lockfile path, default-branch oid) to the parsed index. Only readings
# that finished are kept (a network failure is retried on the next load).
_lock_cache: dict[tuple[str, str, str], Index | None] = {}
_LOCK_CACHE_MAX = 300
# A 404 is kept for an hour only: GitHub answers 404 for a private repo when the token lacks
# Contents read, so a fixed token must be picked up without waiting for a push (2026-10-24).
_lock_missing: dict[tuple[str, str, str], float] = {}
NOT_FOUND_TTL_S = 3600.0


def reset_lock_cache() -> None:
    _lock_cache.clear()
    _lock_missing.clear()
    _commit_cache.clear()


async def _read_index(
    client: httpx.AsyncClient,
    settings: Settings,
    repo: str,
    owner: str,
    token: str,
    ecosystem: str,
    path: str,
    oid: str,
) -> Index | None:
    key = (repo.lower(), path, oid)
    if key in _lock_cache:
        return _lock_cache[key]
    missed = _lock_missing.get(key)
    if missed is not None:
        if time.monotonic() - missed < NOT_FOUND_TTL_S:
            return None
        del _lock_missing[key]
    url = f"{settings.api_url}/repos/{repo}/contents/{quote(path, safe='/')}?ref={oid}"
    try:
        text = await _get_raw(client, url, token, f"{path} in {repo}", owner, MAX_LOCKFILE_BYTES)
    except _Fail as exc:
        # A missing or oversize file is a final answer; anything else may pass, so it is not kept.
        if not isinstance(exc, _NotFound | _TooLarge):
            return None
        if isinstance(exc, _NotFound):
            if len(_lock_missing) >= _LOCK_CACHE_MAX:
                _lock_missing.pop(next(iter(_lock_missing)))
            _lock_missing[key] = time.monotonic()
            return None
        index = None
    else:
        # A 20 MB lockfile takes long enough to parse that it must not hold the event loop.
        parser = parse_npm_lock if ecosystem == "NPM" else parse_requirements
        index = await asyncio.to_thread(parser, text)
    if len(_lock_cache) >= _LOCK_CACHE_MAX:
        _lock_cache.pop(next(iter(_lock_cache)))
    _lock_cache[key] = index
    return index


# (repo lower, lockfile path, default-branch oid) to the date of the last commit on the default
# branch that touched that lockfile; only readings that finished are kept.
_commit_cache: dict[tuple[str, str, str], str | None] = {}


async def _lockfile_commit_date(
    client: httpx.AsyncClient,
    settings: Settings,
    repo: str,
    owner: str,
    token: str,
    default: str,
    path: str,
    oid: str,
) -> str | None:
    key = (repo.lower(), path, oid)
    if key in _commit_cache:
        return _commit_cache[key]
    url = (
        f"{settings.api_url}/repos/{repo}/commits?sha={quote(default, safe='')}"
        f"&path={quote(path, safe='/')}&per_page=1"
    )
    try:
        body = await _get_json(client, url, token, f"the last commit of {path} in {repo}", owner)
        date = body[0]["commit"]["committer"]["date"]
        date = date if isinstance(date, str) and date else None
    except (_Fail, LookupError, TypeError):
        # A failed read is not kept: the next load tries again.
        return None
    if len(_commit_cache) >= _LOCK_CACHE_MAX:
        _commit_cache.pop(next(iter(_commit_cache)))
    _commit_cache[key] = date
    return date


def _bucket(
    ecosystem: str, package: str, version_range: str, index: Index | None
) -> tuple[str, list[str]]:
    if index is None:
        return "fix", []
    name = normalize_pip(package) if ecosystem == "PIP" else package
    installed = sorted(index.get(name, []))
    if not installed:
        return "fix", []
    for version in installed:
        # Unreadable counts as inside the range: never hide an alert.
        if in_range(ecosystem, version, version_range) is not False:
            return "fix", installed
    return "waiting", installed


def _repo_error(repo: dict[str, Any], note: str) -> dict[str, Any]:
    repo.update(error=_cut(note, 240), prs=[], alerts=[], branches=[])
    return repo


def _path_of(error: dict[str, Any]) -> str:
    return ".".join(str(p) for p in (error.get("path") or []))


async def _load_repo(
    client: httpx.AsyncClient,
    settings: Settings,
    repo: str,
    view_only: bool,
    catch_up: set[str],
    token: str,
    sem: asyncio.Semaphore,
) -> dict[str, Any]:
    owner, _, name = repo.partition("/")
    row: dict[str, Any] = {
        "repo": repo,
        "url": f"https://github.com/{repo}",
        "default_branch": None,
        "view_only": view_only,
        "pushed_at": None,
        "error": None,
        "prs": [],
        "alerts": [],
        "branches": [],
    }
    async with sem:
        try:
            try:
                response = await client.post(
                    settings.graphql_url,
                    headers=_headers(token),
                    json={"query": _QUERY, "variables": {"owner": owner, "name": name}},
                    timeout=GRAPHQL_TIMEOUT_S,
                )
            except httpx.HTTPError as exc:
                return _repo_error(
                    row, f"GitHub did not answer for {repo} ({type(exc).__name__}); try again."
                )
            if response.status_code != 200:
                return _repo_error(row, _status_note(response.status_code, repo, owner))
            try:
                body = response.json()
            except ValueError:
                return _repo_error(row, f"GitHub sent something unreadable for {repo}.")
            data = (body.get("data") or {}).get("repository")
            errors = [e for e in body.get("errors") or [] if isinstance(e, dict)]
            if data is None:
                if any(e.get("type") == "RATE_LIMITED" for e in errors):
                    return _repo_error(row, f"Rate limited by GitHub for {repo}; try again later.")
                return _repo_error(row, _status_note(404, repo, owner))
            if any("vulnerabilityAlerts" in _path_of(e) for e in errors) or (
                data.get("vulnerabilityAlerts") is None
            ):
                return _repo_error(
                    row,
                    f"The token cannot read Dependabot alerts for {repo}: add the Dependabot "
                    f"alerts (read) permission to {owner_env_name(owner)} (or AE_GITHUB_TOKEN).",
                )
            if errors:
                return _repo_error(row, f"GitHub reported an error reading {repo}; try again.")
            if data.get("isArchived"):
                row["archived"] = True
                return row
            return await _fill_repo(client, settings, row, data, catch_up, token, owner)
        except _Fail as exc:
            return _repo_error(row, str(exc))
        except Exception as exc:  # noqa: BLE001 - one repo never takes the card down
            _logger.warning("git card: reading %s failed (%s)", repo, type(exc).__name__)
            return _repo_error(row, f"Reading {repo} failed ({type(exc).__name__}); try again.")


async def _fill_repo(
    client: httpx.AsyncClient,
    settings: Settings,
    row: dict[str, Any],
    data: dict[str, Any],
    catch_up: set[str],
    token: str,
    owner: str,
) -> dict[str, Any]:
    repo = row["repo"]
    default = (data.get("defaultBranchRef") or {}).get("name")
    oid = ((data.get("defaultBranchRef") or {}).get("target") or {}).get("oid") or ""
    row["default_branch"] = default
    row["pushed_at"] = data.get("pushedAt")

    prs = []
    for node in (data.get("pullRequests") or {}).get("nodes") or []:
        if not node:
            continue
        commits = (node.get("commits") or {}).get("nodes") or []
        rollup = (
            ((commits[0] or {}).get("commit") or {}).get("statusCheckRollup") if commits else None
        )
        prs.append(
            {
                "number": node.get("number"),
                "title": _cut(node.get("title"), TITLE_MAX),
                "url": node.get("url"),
                "draft": bool(node.get("isDraft")),
                "merge_state": node.get("mergeStateStatus") or "UNKNOWN",
                "checks": (rollup or {}).get("state"),
                "author": (node.get("author") or {}).get("login"),
                "updated_at": node.get("updatedAt"),
            }
        )
    prs.sort(key=lambda p: p["updated_at"] or "", reverse=True)
    row["prs"] = prs

    branches = []
    for node in (data.get("refs") or {}).get("nodes") or []:
        bname = (node or {}).get("name")
        if not bname or bname == default or bname in catch_up:
            continue
        if ((node.get("associatedPullRequests") or {}).get("totalCount") or 0) > 0:
            continue
        branches.append(
            {
                "name": _cut(bname, TITLE_MAX),
                "_full_name": bname,
                "url": f"https://github.com/{repo}/tree/{quote(bname, safe='/')}",
                "last_commit_at": (node.get("target") or {}).get("committedDate"),
                "ahead": None,
                "kept": settings.is_kept(repo, bname),
            }
        )
    branches.sort(key=lambda b: b["last_commit_at"] or "")
    row["branches"] = branches

    alerts = []
    for node in (data.get("vulnerabilityAlerts") or {}).get("nodes") or []:
        vuln = (node or {}).get("securityVulnerability") or {}
        package = (vuln.get("package") or {}).get("name") or ""
        eco = ((vuln.get("package") or {}).get("ecosystem") or "").upper()
        alerts.append(
            {
                "number": node.get("number"),
                "created_at": node.get("createdAt"),
                "waiting_since": None,
                "url": f"https://github.com/{repo}/security/dependabot/{node.get('number')}",
                "severity": vuln.get("severity"),
                "package": _cut(package, 80),
                "ecosystem": eco,
                "manifest": _cut(node.get("vulnerableManifestPath"), 120),
                "range": _cut(vuln.get("vulnerableVersionRange"), 80),
                "patched": _cut((vuln.get("firstPatchedVersion") or {}).get("identifier"), 40)
                or None,
                "installed": [],
                "bucket": "fix",
                "_full_package": package,
                "_full_manifest": node.get("vulnerableManifestPath") or "",
            }
        )
    alerts.sort(
        key=lambda a: (_SEVERITY_ORDER.get(str(a["severity"]).upper(), 9), a["package"].lower())
    )
    wanted: dict[tuple[str, str], None] = {}
    for alert in alerts:
        path = lockfile_path(alert["ecosystem"], alert["_full_manifest"])
        if path is not None and oid:
            wanted[(alert["ecosystem"], path)] = None
    indexes: dict[tuple[str, str], Index | None] = {}
    # The repo's own semaphore slot is held; lockfile reads share it, so at most
    # CONCURRENCY calls of any kind are in flight.
    for eco, path in wanted:
        indexes[(eco, path)] = await _read_index(
            client, settings, repo, owner, token, eco, path, oid
        )
    for alert in alerts:
        path = lockfile_path(alert["ecosystem"], alert["_full_manifest"])
        index = indexes.get((alert["ecosystem"], path)) if path is not None else None
        alert["bucket"], alert["installed"] = _bucket(
            alert["ecosystem"], alert["_full_package"], alert["range"], index
        )
        alert["installed"] = [_cut(v, 40) for v in alert["installed"][:6]]
    # When did a waiting alert start waiting: when it opened, or when the fix reached the
    # lockfile, whichever is later. One call per distinct lockfile.
    waiting_paths = {
        path
        for alert in alerts
        if alert["bucket"] == "waiting"
        and (path := lockfile_path(alert["ecosystem"], alert["_full_manifest"])) is not None
    }
    commit_dates: dict[str, str | None] = {}
    if default:
        for path in waiting_paths:
            commit_dates[path] = await _lockfile_commit_date(
                client, settings, repo, owner, token, default, path, oid
            )
    for alert in alerts:
        if alert["bucket"] != "waiting":
            continue
        path = lockfile_path(alert["ecosystem"], alert["_full_manifest"])
        committed = commit_dates.get(path) if path is not None else None
        if committed:
            alert["waiting_since"] = max(str(alert["created_at"] or ""), committed) or None
    row["alerts"] = alerts
    return row


async def _fill_ahead(
    client: httpx.AsyncClient,
    settings: Settings,
    repo: dict[str, Any],
    token: str,
    sem: asyncio.Semaphore,
) -> None:
    owner = repo["repo"].partition("/")[0]

    async def one(branch: dict[str, Any]) -> None:
        name = branch["_full_name"]
        url = (
            f"{settings.api_url}/repos/{repo['repo']}/compare/"
            f"{quote(repo['default_branch'], safe='/')}...{quote(name, safe='/')}?per_page=1"
        )
        async with sem:
            try:
                body = await _get_json(client, url, token, "a branch comparison", owner)
                ahead = body.get("ahead_by")
                branch["ahead"] = ahead if isinstance(ahead, int) else None
            except (_Fail, AttributeError):
                branch["ahead"] = None

    if repo["default_branch"]:
        await asyncio.gather(*(one(b) for b in repo["branches"]))


async def _load_sweep(
    client: httpx.AsyncClient, settings: Settings
) -> tuple[dict[str, Any] | None, list[str], set[str], str | None]:
    """(sweep block, config repos, catch-up branches, config error)."""
    if not settings.sweep_set:
        return None, [], set(), None
    repos: list[str] = []
    catch_up: set[str] = set()
    sweep: dict[str, Any] = {"url": None, "runs": [], "error": None}
    config_error: str | None = None
    if settings.sweep_repo is None or settings.sweep_path is None:
        sweep["error"] = (
            "AE_GIT_SWEEP_CONFIG is not owner/repo:path; fix it in the server's environment."
        )
        return sweep, repos, catch_up, sweep["error"]
    repo = settings.sweep_repo
    owner = repo.partition("/")[0]
    sweep["url"] = f"https://github.com/{repo}/actions/workflows/{settings.workflow}"
    token = settings.token_for(owner)
    if not token:
        sweep["error"] = settings.no_token_note(owner)
        return sweep, repos, catch_up, sweep["error"]
    try:
        text = await _get_raw(
            client,
            f"{settings.api_url}/repos/{repo}/contents/{quote(settings.sweep_path, safe='/')}",
            token,
            f"the sweep config {settings.sweep_path} in {repo}",
            owner,
            1024 * 1024,
        )
        config = json.loads(text)
        for item in config.get("repos") or []:
            candidate = (
                item
                if isinstance(item, str)
                else (item.get("repo") or item.get("name") or item.get("full_name"))
                if isinstance(item, dict)
                else None
            )
            if isinstance(candidate, str) and github._REPO.match(candidate):
                repos.append(candidate)
        catch_up = {b for b in config.get("catchUpBranches") or [] if isinstance(b, str)}
    except _Fail as exc:
        config_error = str(exc)
    except Exception:  # noqa: BLE001 - a config of the wrong shape is a sentence, not a crash
        config_error = f"The sweep config {settings.sweep_path} in {repo} is not readable JSON."
    try:
        body = await _get_json(
            client,
            f"{settings.api_url}/repos/{repo}/actions/workflows/{quote(settings.workflow)}"
            "/runs?per_page=3",
            token,
            f"the {settings.workflow} runs (the token needs Actions: read)",
            owner,
        )
        sweep["runs"] = [
            {
                "id": run.get("id"),
                "url": run.get("html_url"),
                "created_at": run.get("created_at"),
                "status": run.get("status"),
                "conclusion": run.get("conclusion"),
            }
            for run in (body.get("workflow_runs") or [])[:3]
        ]
    except _Fail as exc:
        sweep["error"] = str(exc)
    except Exception:  # noqa: BLE001 - a body of the wrong shape is a sentence, not a crash
        sweep["runs"] = []
        sweep["error"] = (
            f"The {settings.workflow} runs in {repo} came back in a shape the card cannot read."
        )
    return sweep, repos, catch_up, config_error


def _dedupe(settings: Settings, config_repos: Sequence[str]) -> list[tuple[str, bool]]:
    """(repo, view_only) in order, case-insensitive."""
    view = {r.lower() for r in settings.view_only}
    seen: dict[str, str] = {}
    for repo in [*config_repos, *settings.extra_repos, *settings.view_only]:
        seen.setdefault(repo.lower(), repo)
    return [(repo, key in view) for key, repo in seen.items()]


def _size(payload: Any) -> int:
    # The extension bridge counts UTF-16 code units, so an emoji is two.
    text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return len(text.encode("utf-16-le", errors="surrogatepass")) // 2


def _fit(payload: dict[str, Any]) -> None:
    """Trim the lists (never the totals) until the answer fits the bridge's limit."""
    total = _size(payload)
    if total <= SIZE_BUDGET:
        return
    payload["list_cut"] = True
    total += len(',"list_cut":true')
    repos = payload["repos"]
    for kind, pick in (
        ("alerts", lambda a: a["bucket"] == "waiting"),
        ("branches", lambda _b: True),
        ("alerts", lambda _a: True),
        ("prs", lambda _p: True),
    ):
        for repo in reversed(repos):
            items = repo[kind]
            for i in range(len(items) - 1, -1, -1):
                if total <= SIZE_BUDGET:
                    return
                if pick(items[i]):
                    total -= _size(items[i]) + 1
                    del items[i]
    # Lists are empty and it is still big: drop quiet repos from the end of the list.
    while repos and total > SIZE_BUDGET:
        total -= _size(repos.pop()) + 1


async def load(
    settings: Settings | None = None,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Read every configured repo and build the card's answer (never raises for GitHub)."""
    settings = settings or Settings()
    stamp = (now or datetime.now(UTC)).strftime("%Y-%m-%dT%H:%M:%SZ")
    if not settings.configured:
        return {
            "configured": False,
            "setup": NOT_CONFIGURED,
            "updated_at": stamp,
            "error": None,
            "accounts": [],
            "totals": _totals([]),
            "repos": [],
            "sweep": None,
        }
    sem = asyncio.Semaphore(CONCURRENCY)
    async with httpx.AsyncClient(transport=transport) as client:
        sweep, config_repos, catch_up, config_error = await _load_sweep(client, settings)
        pairs = _dedupe(settings, config_repos)
        owners: dict[str, str] = {}
        for repo, _ in pairs:
            owners.setdefault(repo.partition("/")[0].lower(), repo.partition("/")[0])
        tokens = {key: settings.token_for(owner) for key, owner in owners.items()}

        async def run(repo: str, view_only: bool) -> dict[str, Any]:
            token = tokens[repo.partition("/")[0].lower()]
            if not token:
                note = settings.no_token_note(repo.partition("/")[0])
                return _repo_error(
                    {
                        "repo": repo,
                        "url": f"https://github.com/{repo}",
                        "default_branch": None,
                        "view_only": view_only,
                        "pushed_at": None,
                    },
                    note,
                )
            return await _load_repo(client, settings, repo, view_only, catch_up, token, sem)

        rows = list(await asyncio.gather(*(run(r, v) for r, v in pairs)))
        rows = [r for r in rows if not r.pop("archived", False)]
        live = [r["repo"] for r in rows]

        def count(r: dict[str, Any]) -> int:
            return len(r["prs"]) + len(r["alerts"]) + len(r["branches"])

        rows.sort(key=lambda r: (r["view_only"], count(r) == 0, -count(r), r["repo"].lower()))
        # Caps across the lists. Fix alerts and non-draft PRs are kept first, across all repos;
        # the totals above still count everything.
        totals = _totals(rows)
        cut = _cap(rows, "prs", MAX_PRS, lambda p: 1 if p["draft"] else 0)
        cut |= _cap(rows, "alerts", MAX_ALERTS, lambda a: 0 if a["bucket"] == "fix" else 1)
        cut |= _cap(rows, "branches", MAX_BRANCHES, lambda _b: 0)
        for r in rows:
            for alert in r["alerts"]:
                alert.pop("_full_package", None)
                alert.pop("_full_manifest", None)
        await asyncio.gather(
            *(
                _fill_ahead(client, settings, r, tokens[r["repo"].partition("/")[0].lower()], sem)
                for r in rows
                if r["branches"] and not r["error"]
            )
        )
        for r in rows:
            for branch in r["branches"]:
                branch.pop("_full_name", None)

    accounts = []
    for key, owner in owners.items():
        count_all = sum(1 for repo in live if repo.partition("/")[0].lower() == key)
        accounts.append(
            {
                "owner": owner,
                "repos": count_all,
                "connected": bool(tokens[key]),
                "note": None if tokens[key] else settings.no_token_note(owner),
            }
        )
    payload: dict[str, Any] = {
        "configured": True,
        "setup": None,
        "updated_at": stamp,
        "error": config_error,
        "accounts": accounts,
        "totals": totals,
        "repos": rows,
        "sweep": sweep,
    }
    if cut:
        payload["list_cut"] = True
    _fit(payload)
    return payload


def _cap(
    rows: list[dict[str, Any]], kind: str, limit: int, priority: Callable[[Any], int]
) -> bool:
    """Keep at most ``limit`` items of ``kind`` across the rows, best priority first.

    Order within each row is unchanged. Returns True when anything was dropped.
    """
    ranked = sorted(
        ((priority(item), ri, ii) for ri, r in enumerate(rows) for ii, item in enumerate(r[kind])),
        key=lambda t: t[0],
    )
    keep = {(ri, ii) for _, ri, ii in ranked[:limit]}
    dropped = len(ranked) > limit
    for ri, r in enumerate(rows):
        r[kind] = [item for ii, item in enumerate(r[kind]) if (ri, ii) in keep]
    return dropped


def _totals(rows: list[dict[str, Any]]) -> dict[str, int]:
    counted = [r for r in rows if not r["view_only"]]
    alerts = [a for r in counted for a in r["alerts"]]
    prs = [p for r in counted for p in r["prs"]]
    return {
        "repos": len(counted),
        "prs": len(prs),
        "prs_ready": sum(1 for p in prs if not p["draft"] and p["merge_state"] == "CLEAN"),
        "alerts_fix": sum(1 for a in alerts if a["bucket"] == "fix"),
        "alerts_waiting": sum(1 for a in alerts if a["bucket"] == "waiting"),
        "branches": sum(len(r["branches"]) for r in counted),
        "branches_kept": sum(1 for r in counted for b in r["branches"] if b.get("kept")),
        "repo_errors": sum(1 for r in counted if r["error"]),
    }


# ── the cached read ───────────────────────────────────────────────────────────


class _AllFailed(Exception):
    """Every repo of a load came back with an error (an outage or a dead token)."""

    def __init__(self, payload: dict[str, Any]) -> None:
        first = next((r["error"] for r in payload["repos"] if r["error"]), "")
        super().__init__(f"GitHub failed for every repo ({first})")
        self.payload = payload


def _describe(exc: BaseException) -> str:
    # _AllFailed carries only page-safe notes; other errors report the class name only.
    return str(exc) if isinstance(exc, _AllFailed) else type(exc).__name__


_cache = SwrCache[str, dict[str, Any]]("git repos", TTL_S, describe=_describe)


# (monotonic time, answer) of the last first-ever load that failed for every repo.
_failed: tuple[float, dict[str, Any]] | None = None


def reset_cache() -> None:
    global _failed
    _failed = None
    _cache.reset()
    reset_lock_cache()


def _with_last_good(held: dict[str, Any], why: str) -> dict[str, Any]:
    clock = str(held.get("updated_at") or "")[11:16]
    when = f" from {clock}Z" if clock else ""
    return {**held, "error": f"Showing the last good read{when}: {why}"}


async def overview(*, fresh: bool = False) -> dict[str, Any]:
    """The answer for ``GET /v1/ae/git``: cached 300 s, a failed load keeps the last good one."""
    settings = Settings()
    if not settings.configured:
        return await load(settings)

    async def do_load() -> dict[str, Any]:
        answer = await load(settings)
        # A load that read repos and got an error from every one is an outage, not news:
        # raise so the cache keeps the last good answer.
        if answer["repos"] and all(r["error"] for r in answer["repos"]):
            raise _AllFailed(answer)
        return answer

    global _failed
    if not fresh and _failed is not None and _cache.peek("git") is None:
        if time.monotonic() - _failed[0] < FAILED_MEMORY_S:
            return _failed[1]
        _failed = None
    try:
        value = await _cache.get("git", do_load, fresh=fresh)
    except Exception as exc:  # noqa: BLE001 - the card answers with an error line instead
        _logger.warning("git card: load failed (%s)", type(exc).__name__)
        held = _cache.peek("git")
        if held is not None:
            return _with_last_good(held, _describe(exc))
        if isinstance(exc, _AllFailed):
            failed = {**exc.payload, "error": str(exc)}
            _failed = (time.monotonic(), failed)
            return failed
        answer = await load(Settings({}))
        answer.update(
            configured=True,
            setup=None,
            accounts=[],
            repos=[],
            sweep=None,
            error=f"The Git repos read failed ({type(exc).__name__}); try again.",
        )
        return answer
    _failed = None
    why = _cache.last_error("git")
    # A value served while its refresh failed (an error is set only when the value is older).
    return _with_last_good(value, why) if why else value


__all__ = ["Settings", "in_range", "load", "overview", "owner_env_name", "reset_cache"]
