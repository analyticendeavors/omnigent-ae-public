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
  run of non-alphanumerics as ``_``); an owner without one uses the token saved for it in the
  Cards panel (:mod:`ae_omni_policies.card_secrets`, 2026-11-08), then ``AE_GITHUB_TOKEN``.
  Secrets: never logged, never returned.
- ``AE_GITHUB_API_URL``: the API origin, as :mod:`ae_omni_policies.github`.

Two scopes (2026-11-08, card setup PR 3): ``company`` is the list above, read with company
tokens and shown to every admin; ``mine`` is one admin's own list (:mod:`ae_omni_policies.
git_personal`), read only with that admin's own token for each repo's account, never a company
token, ``AE_GITHUB_TOKEN`` or another user's token, and only from the fixed ``api.github.com``.
The cache, the failed-first-load memory and the last good answer are keyed by (scope, login), so
no answer ever crosses users.

The answer is cached with :class:`~ae_omni_policies.swr.SwrCache` (300 s). Lockfile
reads are cached by (repo, manifest path, default-branch commit), so a quiet repo
costs no lockfile calls on the next load.
"""

from __future__ import annotations

import asyncio
import contextvars
import hashlib
import json
import logging
import math
import os
import re
import time
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote

import httpx

from ae_omni_policies import admin_cards, card_config, card_secrets, git_personal, github
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
#: Branch comparisons (ahead counts) one load may make; the answer is no longer capped first.
MAX_FILL_AHEAD = 100
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

SCOPE_COMPANY = "company"
SCOPE_MINE = "mine"
#: Personal reads go here and nowhere else: a typed-in token is never sent to an address a
#: setting can change.
PERSONAL_API_URL = "https://api.github.com"
#: Swapped by tests; production passes no transport.
TRANSPORT: httpx.AsyncBaseTransport | None = None

NOT_CONFIGURED = (
    "The Git repos card is off: set AE_GIT_SWEEP_CONFIG (owner/repo:path of a Dependency "
    "Sweep config) and/or AE_GIT_REPOS (comma-separated owner/repo) in the Cards panel "
    "(Git repos, Setup), or in the server's environment, which overrides the panel."
)

_SEVERITY_ORDER = {"CRITICAL": 0, "HIGH": 1, "MODERATE": 2, "LOW": 3}

_QUERY = """
query($owner: String!, $name: String!) {
  repository(owner: $owner, name: $name) {
    defaultBranchRef { name target { oid } }
    pushedAt
    isArchived
    pullRequests(states: OPEN, first: 50) {
      pageInfo { hasNextPage }
      nodes {
        number title url isDraft mergeStateStatus author { login } updatedAt headRefName
        isCrossRepository authorAssociation
        commits(last: 1) {
          nodes {
            commit {
              oid
              statusCheckRollup {
                state
                contexts(first: 20) {
                  totalCount
                  nodes {
                    __typename
                    ... on CheckRun {
                      name
                      conclusion
                      status
                      completedAt
                      checkSuite {
                        app { slug }
                        workflowRun { workflow { name } }
                      }
                    }
                    ... on StatusContext { context state }
                  }
                }
              }
            }
          }
        }
      }
    }
    refs(refPrefix: "refs/heads/", first: 100) {
      pageInfo { hasNextPage }
      nodes {
        name
        target { ... on Commit { oid committedDate } }
        associatedPullRequests(first: 5, states: [OPEN, MERGED]) { totalCount }
      }
    }
    vulnerabilityAlerts(states: OPEN, first: 100) {
      pageInfo { hasNextPage }
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

    scope = SCOPE_COMPANY
    login: str | None = None

    def __init__(self, environ: Mapping[str, str] | None = None) -> None:
        env = os.environ if environ is None else environ
        self._env = env
        # A token saved in the Cards panel is honoured only when the process environment is read
        # (a caller that passes its own mapping, as the tests do, gets exactly that mapping).
        self._saved_ok = env is os.environ
        self.api_url = (env.get(github.API_URL_ENV) or "").strip().rstrip("/") or github.API_URL
        # Typed-in settings (2026-11-07): the environment still wins, see card_config.
        raw = (card_config.value(SWEEP_CONFIG_ENV, environ) or "").strip()
        self.sweep_repo: str | None = None
        self.sweep_path: str | None = None
        repo, _, path = raw.partition(":")
        if github._REPO.match(repo.strip()) and path.strip():
            self.sweep_repo, self.sweep_path = repo.strip(), path.strip().lstrip("/")
        self.extra_repos = _split_repos(card_config.value(REPOS_ENV, environ))
        self.view_only = _split_repos(card_config.value(VIEW_ONLY_ENV, environ))
        self.workflow = (
            card_config.value(WORKFLOW_ENV, environ) or ""
        ).strip() or DEFAULT_WORKFLOW
        self.sweep_set = bool(raw)
        self.kept: set[tuple[str, str]] = set()
        for item in (card_config.value(KEEP_ENV, environ) or "").split(","):
            kept_repo, _, kept_branch = item.strip().partition(":")
            if github._REPO.match(kept_repo.strip()) and kept_branch.strip():
                self.kept.add((kept_repo.strip().lower(), kept_branch.strip()))

    def is_kept(self, repo: str, branch: str) -> bool:
        return (repo.lower(), branch) in self.kept

    @property
    def configured(self) -> bool:
        return self.sweep_set or bool(self.extra_repos) or bool(self.view_only)

    def group_of(self, repo: str) -> str:
        """The account a repo's token belongs to: its owner for the company list."""
        return repo.partition("/")[0]

    def identity(self, repo: str) -> str:  # noqa: ARG002 (the personal override needs *repo*)
        """Whose credential reads *repo*: ``company``, or ``user/<login>/<account>`` (personal)."""
        return "company"

    def token_for(self, owner: str) -> str:
        """The company token for *owner*: its environment variable, then the one saved in the
        Cards panel, then ``AE_GITHUB_TOKEN``."""
        own = (self._env.get(owner_env_name(owner)) or "").strip()
        if own:
            return own
        if self._saved_ok:
            try:
                saved = card_secrets.get_token(card_secrets.company_id(owner))
            except card_secrets.CardSecretsError:
                saved = None
            if saved:
                return saved
        return (self._env.get(github.TOKEN_ENV) or "").strip()

    def no_token_note(self, owner: str) -> str:
        return (
            f"No token: set {owner_env_name(owner)} in the server's environment, or add one "
            "in the Cards panel (Git repos, Company tokens)."
        )

    @property
    def graphql_url(self) -> str:
        # GitHub Enterprise Server: https://HOST/api/v3 -> https://HOST/api/graphql.
        if self.api_url.endswith("/api/v3"):
            return self.api_url[: -len("v3")] + "graphql"
        return self.api_url + "/graphql"


class PersonalSettings(Settings):
    """One admin's own list: their repos, each read with their token for that repo's account.

    No sweep, no workflow, no kept branches, nothing view only. The token for a repo is the one
    this login saved for the repo's account (:func:`git_personal.token_for`) and nothing else:
    never a company token, never ``AE_GITHUB_TOKEN``. The API address is fixed.
    """

    scope = SCOPE_MINE

    def __init__(self, login: str, repos: Sequence[Mapping[str, str]]) -> None:
        self._env = {}
        self._saved_ok = False
        self.login = login
        self.api_url = PERSONAL_API_URL
        self.sweep_repo = None
        self.sweep_path = None
        self.workflow = DEFAULT_WORKFLOW
        self.sweep_set = False
        self.view_only = []
        self.kept = set()
        self._accounts: dict[str, str] = {}
        self.extra_repos = []
        for item in repos:
            repo = str(item.get("repo") or "")
            if github._REPO.match(repo) and repo.lower() not in self._accounts:
                self._accounts[repo.lower()] = str(item.get("account") or "").lower()
                self.extra_repos.append(repo)

    @property
    def configured(self) -> bool:
        return bool(self.extra_repos)

    def group_of(self, repo: str) -> str:
        return self._accounts.get(repo.lower(), "")

    def identity(self, repo: str) -> str:
        return f"user/{self.login}/{self.group_of(repo)}"

    def token_for(self, owner: str) -> str:
        """The login's own token for GitHub account *owner* (here the account, not the owner)."""
        return git_personal.token_for(self.login or "", owner) if owner else ""

    def no_token_note(self, owner: str) -> str:
        return (
            f"No token saved for {owner or 'this repo'}: connect it again in the Cards panel "
            "(Git repos, My GitHub accounts)."
        )


# A personal load says so, so an error tells the admin to reconnect their account rather than
# to set a company variable. A ContextVar because the repo reads run as separate tasks.
_personal_load: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "ae_git_personal_load", default=False
)


def _token_name(owner: str) -> str:
    """What an error tells the admin to replace."""
    if _personal_load.get():
        return f"your token for {owner} in the Cards panel (My GitHub accounts)"
    return f"{owner_env_name(owner)} (or AE_GITHUB_TOKEN)"


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
            f"replace {_token_name(owner)}."
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

# (credential identity, repo lower, lockfile path, default-branch oid) to the parsed index. The
# identity (:meth:`Settings.identity`) is part of the key because GitHub answers by token: what
# one login's token could read, or got a 404 for, says nothing about another's or the company's.
# Only readings that finished are kept (a network failure is retried on the next load).
_lock_cache: dict[tuple[str, str, str, str], Index | None] = {}
_LOCK_CACHE_MAX = 300
# A 404 is kept for an hour only: GitHub answers 404 for a private repo when the token lacks
# Contents read, so a fixed token must be picked up without waiting for a push (2026-10-24).
_lock_missing: dict[tuple[str, str, str, str], float] = {}
NOT_FOUND_TTL_S = 3600.0


def reset_lock_cache() -> None:
    _lock_cache.clear()
    _lock_missing.clear()
    _commit_cache.clear()


def drop_identity(prefix: str) -> None:
    """Forget the cached lockfile readings of every credential identity starting with *prefix*.

    Called when a token is saved or cleared: ``company`` for the company's, ``user/<login>/`` for
    one login's.
    """
    for cache in (_lock_cache, _lock_missing, _commit_cache):
        for key in [k for k in cache if k[0].startswith(prefix)]:
            del cache[key]


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
    key = (settings.identity(repo), repo.lower(), path, oid)
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
_commit_cache: dict[tuple[str, str, str, str], str | None] = {}


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
    key = (settings.identity(repo), repo.lower(), path, oid)
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


_FAILED_RUNS = frozenset(
    {"FAILURE", "TIMED_OUT", "CANCELLED", "STARTUP_FAILURE", "ACTION_REQUIRED"}
)
_FAILED_STATUS = frozenset({"FAILURE", "ERROR"})
_PASSED_RUNS = frozenset({"SUCCESS", "NEUTRAL"})
#: Runs that leave a check green without standing in for a cancelled one.
_OK_RUNS = _PASSED_RUNS | {"SKIPPED"}


def _run_key(node: dict[str, Any]) -> tuple[str, str, str]:
    """A check run's identity: its app, its workflow and its full name, so two workflows (or
    two apps) publishing the same job name never stand in for each other."""
    suite = node.get("checkSuite") if isinstance(node.get("checkSuite"), dict) else {}
    app = (suite.get("app") or {}).get("slug") if isinstance(suite.get("app"), dict) else None
    run = suite.get("workflowRun") if isinstance(suite.get("workflowRun"), dict) else {}
    workflow = (
        (run.get("workflow") or {}).get("name") if isinstance(run.get("workflow"), dict) else None
    )
    return (str(app or ""), str(workflow or ""), str(node.get("name") or ""))


def _superseded(nodes: list[Any]) -> set[int]:
    """The ids of the cancelled check runs a later passed run of the same check stands for.

    GitHub counts every cancelled run as a failure. A concurrency group cancels the duplicate
    runs one push starts, and the run that finishes stands for the check (a leak scan,
    2026-11-06). A cancel counts as superseded only when a run with the same app, workflow and
    name passed and finished at or after it, so a person cancelling the newest run after an
    older one passed still shows. A missing time never supersedes.
    """
    passed: dict[tuple[str, str, str], str] = {}
    for node in nodes:
        if isinstance(node, dict) and node.get("__typename") != "StatusContext":
            done = node.get("completedAt")
            if node.get("conclusion") in _PASSED_RUNS and isinstance(done, str) and done:
                key = _run_key(node)
                passed[key] = max(passed.get(key, ""), done)
    out: set[int] = set()
    for node in nodes:
        if not isinstance(node, dict) or node.get("__typename") == "StatusContext":
            continue
        done = node.get("completedAt")
        if node.get("conclusion") != "CANCELLED" or not isinstance(done, str) or not done:
            continue
        latest = passed.get(_run_key(node))
        if latest is not None and latest >= done:
            out.add(id(node))
    return out


def _failing_checks(rollup: Any) -> list[str] | None:
    """The sorted names of the failing checks on a head commit, ``None`` when unknowable.

    ``None`` (not a short list) when GitHub has more than the 20 checks the query reads, so a
    truncated list never looks like a change (plan 3.2, 2026-11-03). A cancelled run a later
    passed run of the same check stands for is not failing (``_superseded``).
    """
    if not isinstance(rollup, dict):
        return []
    contexts = rollup.get("contexts") or {}
    nodes = contexts.get("nodes") or []
    if int(contexts.get("totalCount") or 0) > len(nodes):
        return None
    superseded = _superseded(nodes)
    names: set[str] = set()
    for node in nodes:
        if not isinstance(node, dict):
            continue
        if node.get("__typename") == "StatusContext":
            if node.get("state") in _FAILED_STATUS:
                names.add(_cut(node.get("context"), 80))
        elif node.get("conclusion") in _FAILED_RUNS and id(node) not in superseded:
            names.add(_cut(node.get("name"), 80))
    return sorted(n for n in names if n)


def _checks_state(rollup: Any, failing: list[str] | None) -> Any:
    """GitHub's rollup state, with a FAILURE that only superseded cancelled runs caused undone.

    The state changes only when every check is accounted for: each run passed, is unfinished, or
    is a superseded cancel, and each status is SUCCESS or unfinished. Then it is PENDING while
    anything is unfinished and SUCCESS otherwise. Any other value (STALE, a conclusion GitHub
    adds later) keeps GitHub's own state, so nothing unknown ever reads green.
    """
    if not isinstance(rollup, dict):
        return None
    state = rollup.get("state")
    if state not in _FAILED_STATUS or failing != []:
        return state
    nodes = (rollup.get("contexts") or {}).get("nodes") or []
    if not nodes:
        return state
    superseded = _superseded(nodes)
    pending = False
    for node in nodes:
        if not isinstance(node, dict):
            return state
        if node.get("__typename") == "StatusContext":
            if node.get("state") in ("PENDING", "EXPECTED"):
                pending = True
            elif node.get("state") != "SUCCESS":
                return state
        elif node.get("status") not in (None, "COMPLETED") or node.get("conclusion") is None:
            pending = True
        elif node.get("conclusion") not in _OK_RUNS and id(node) not in superseded:
            return state
    return "PENDING" if pending else "SUCCESS"


def wire(value: Any) -> Any:
    """*value* without the ``_facts`` the Needs you list keeps (they never go to the page)."""
    if isinstance(value, dict):
        return {k: wire(v) for k, v in value.items() if k != "_facts"}
    if isinstance(value, list):
        return [wire(v) for v in value]
    return value


def _repo_error(repo: dict[str, Any], note: str) -> dict[str, Any]:
    repo.update(
        error=_cut(note, 240), prs=[], alerts=[], branches=[], _facts={"prs": {}, "branches": {}}
    )
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
                    f"alerts (read) permission to {_token_name(owner)}.",
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
    # Facts the Needs you list keys on (head commit, failing check names, branch heads). They
    # ride in the cached row but never go to the page (:func:`wire`): the answer is size-capped.
    facts: dict[str, Any] = {"prs": {}, "branches": {}}
    row["_facts"] = facts
    # A connection with a next page was read in part: an item missing from it is not known to
    # be gone, so the repo's scope is unclean (dismissals kept, "?" shown) (2026-11-03).
    if any(
        ((data.get(name) or {}).get("pageInfo") or {}).get("hasNextPage")
        for name in ("pullRequests", "refs", "vulnerabilityAlerts")
    ):
        facts["truncated"] = True
    for node in (data.get("pullRequests") or {}).get("nodes") or []:
        if not node:
            continue
        commits = (node.get("commits") or {}).get("nodes") or []
        commit = (commits[0] or {}).get("commit") or {} if commits else {}
        rollup = commit.get("statusCheckRollup")
        failing = _failing_checks(rollup)
        facts["prs"][str(node.get("number"))] = {
            "sha": str(commit.get("oid") or ""),
            "failing": failing,
        }
        prs.append(
            {
                "number": node.get("number"),
                "title": _cut(node.get("title"), TITLE_MAX),
                "url": node.get("url"),
                "draft": bool(node.get("isDraft")),
                "merge_state": node.get("mergeStateStatus") or "UNKNOWN",
                "checks": _checks_state(rollup, failing),
                "author": (node.get("author") or {}).get("login"),
                # Only an explicit false means the head is in this repository; a missing or odd
                # value counts as a fork, so a Fix never runs code it cannot vouch for.
                "cross_repo": node.get("isCrossRepository") is not False,
                "author_association": node.get("authorAssociation"),
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
        facts["branches"][bname] = str((node.get("target") or {}).get("oid") or "")
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


def _surfaceable(repo: dict[str, Any], when: float) -> list[dict[str, Any]]:
    """The repo's branches :func:`needs` would turn into an item (not kept, old enough).

    Only these cost a compare call; the order (oldest first) is kept.
    """
    out = []
    for branch in repo["branches"]:
        if branch.get("kept"):
            continue
        at = _stamp(branch.get("last_commit_at"))
        if not math.isnan(at) and when - at > OLD_BRANCH_DAYS * 86400:
            out.append(branch)
    return out


async def _fill_ahead(
    client: httpx.AsyncClient,
    settings: Settings,
    repo: dict[str, Any],
    token: str,
    sem: asyncio.Semaphore,
    limit: int,
    when: float,
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
        wanted = _surfaceable(repo, when)
        picked = wanted[:limit]
        if len(picked) < len(wanted):
            # A branch the list would show, left uncompared, makes the repo's scope unclean
            # (like a paginated read).
            repo.setdefault("_facts", {})["truncated"] = True
        await asyncio.gather(*(one(b) for b in picked))


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
            "AE_GIT_SWEEP_CONFIG is not owner/repo:path; fix it in the Cards panel, or in the "
            "server's environment if it is set there (the environment overrides the panel)."
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
    text = json.dumps(wire(payload), ensure_ascii=False, separators=(",", ":"))
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
            "scope": settings.scope,
        }
    marker = _personal_load.set(settings.scope == SCOPE_MINE)
    try:
        return await _load_configured(settings, stamp, transport, now)
    finally:
        _personal_load.reset(marker)


async def _load_configured(
    settings: Settings,
    stamp: str,
    transport: httpx.AsyncBaseTransport | None,
    now: datetime | None,
) -> dict[str, Any]:
    sem = asyncio.Semaphore(CONCURRENCY)
    async with httpx.AsyncClient(transport=transport) as client:
        sweep, config_repos, catch_up, config_error = await _load_sweep(client, settings)
        pairs = _dedupe(settings, config_repos)
        owners: dict[str, str] = {}
        for repo, _ in pairs:
            owners.setdefault(settings.group_of(repo).lower(), settings.group_of(repo))
        tokens = {key: settings.token_for(owner) for key, owner in owners.items()}

        async def run(repo: str, view_only: bool) -> dict[str, Any]:
            token = tokens[settings.group_of(repo).lower()]
            if not token:
                note = settings.no_token_note(settings.group_of(repo))
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
        for r in rows:
            r["scope"] = settings.scope

        def count(r: dict[str, Any]) -> int:
            return len(r["prs"]) + len(r["alerts"]) + len(r["branches"])

        rows.sort(key=lambda r: (r["view_only"], count(r) == 0, -count(r), r["repo"].lower()))
        # The answer is kept whole: the Needs you list reads every alert and branch. Only the
        # Git card's page is cut to fit (:func:`page`). Totals count everything.
        totals = _totals(rows)
        for r in rows:
            for alert in r["alerts"]:
                alert.pop("_full_package", None)
                alert.pop("_full_manifest", None)
        # Compare calls are bounded per load, oldest branches first (the list is oldest first).
        # Only branches the Needs you list would show are compared.
        when = (now or datetime.now(UTC)).timestamp()
        budget = MAX_FILL_AHEAD
        todo: list[tuple[dict[str, Any], int]] = []
        for r in rows:
            if r["branches"] and not r["error"]:
                want = len(_surfaceable(r, when)) if r["default_branch"] else 0
                take = min(want, budget)
                budget -= take
                todo.append((r, take))
        await asyncio.gather(
            *(
                _fill_ahead(
                    client,
                    settings,
                    r,
                    tokens[settings.group_of(r["repo"]).lower()],
                    sem,
                    take,
                    when,
                )
                for r, take in todo
            )
        )
        for r in rows:
            for branch in r["branches"]:
                branch.pop("_full_name", None)

    accounts = []
    for key, owner in owners.items():
        count_all = sum(1 for repo in live if settings.group_of(repo).lower() == key)
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
        "scope": settings.scope,
    }
    return payload


# The paged copy of the last answer handed to :func:`page`, per cache key. The held answer keeps
# a strong reference, so its identity cannot be reused by another object. A hit needs the very
# same repo list, so one key's page can never be served for another's answer.
_page_memo: dict[str, tuple[Any, Any, dict[str, Any]]] = {}


def page(answer: dict[str, Any], memo_key: str | None = "git") -> dict[str, Any]:
    """The answer cut to fit the Git card's page (caps, then the bridge's size budget).

    A fresh copy (:func:`wire`, so no ``_facts``); the held answer is never changed. Memoized
    on the answer's repo list and error, so a last-good copy of the same read hits. *memo_key*
    is the answer's cache key (:func:`cache_key`); ``None`` skips the memo (a merged answer).
    """
    repos, error = answer.get("repos"), answer.get("error")
    held = _page_memo.get(memo_key) if memo_key is not None else None
    if held is not None and held[0] is repos and held[1] == error:
        return held[2]
    paged: dict[str, Any] = wire(answer)
    rows = paged["repos"]
    cut = _cap(rows, "prs", MAX_PRS, lambda p: 1 if p["draft"] else 0)
    cut |= _cap(rows, "alerts", MAX_ALERTS, lambda a: 0 if a["bucket"] == "fix" else 1)
    cut |= _cap(rows, "branches", MAX_BRANCHES, lambda _b: 0)
    if cut:
        paged["list_cut"] = True
    _fit(paged)
    if memo_key is not None:
        _page_memo[memo_key] = (repos, error, paged)
    return paged


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


def company_owners(environ: Mapping[str, str] | None = None) -> list[str]:
    """The GitHub accounts of the company repo list, as first written, without any read.

    From ``AE_GIT_SWEEP_CONFIG``'s own repo, ``AE_GIT_REPOS``, ``AE_GIT_VIEW_ONLY`` and the
    accounts of the held company answer (which includes the sweep config's repos). Case-
    insensitive, in first-seen order.
    """
    settings = Settings(environ)
    repos = [*([settings.sweep_repo] if settings.sweep_repo else []), *settings.extra_repos]
    names = [r.partition("/")[0] for r in [*repos, *settings.view_only]]
    held = _cache.peek("git")
    names += [str(a.get("owner") or "") for a in (held or {}).get("accounts") or []]
    seen: dict[str, str] = {}
    for name in names:
        if name:
            seen.setdefault(name.lower(), name)
    return list(seen.values())


def cache_key(scope: str = SCOPE_COMPANY, login: str | None = None) -> str:
    """The key of one (scope, login) in the cache and in the failed-load memory.

    ``"git"`` for the company list, ``"git-mine:<login>"`` for one admin's own.
    """
    if scope == SCOPE_MINE:
        if not login:
            raise ValueError("a personal read names the login")
        return f"git-mine:{login}"
    return "git"


def _settings_for(scope: str, login: str | None) -> Settings:
    if scope != SCOPE_MINE:
        return Settings()
    assert login
    if not git_personal.is_active_admin(login):
        return PersonalSettings(login, [])
    return PersonalSettings(login, git_personal.store().get(login)[1])


# (monotonic time, answer) of the last first-ever load per key that failed for every repo.
_failed: dict[str, tuple[float, dict[str, Any]]] = {}
# Bumped by reset_cache() and invalidate(): a read that began before a settings save must not
# leave its failed first-load memory behind for the new settings (2026-11-07). One counter for
# everything and one per key, so a personal save touches only that login.
_epoch = 0
_key_epoch: dict[str, int] = {}


def _epoch_of(key: str) -> tuple[int, int]:
    return _epoch, _key_epoch.get(key, 0)


def invalidate(login: str | None = None) -> None:
    """After a Git setting or token is saved in the panel: forget one scope's answers.

    Without *login*, the company list's (a company setting or token changed); with it, that
    login's own (they saved or cleared a personal account). Unlike :func:`reset_cache` this
    cancels nothing: a read in flight finishes for the caller that asked, but stores nothing and
    leaves no failed-load memory, so the next request reads with the new settings.
    """
    key = cache_key(SCOPE_MINE, login) if login else cache_key()
    drop_identity(f"user/{login}/" if login else "company")
    _key_epoch[key] = _key_epoch.get(key, 0) + 1
    _failed.pop(key, None)
    _page_memo.pop(key, None)
    _cache.forget(key)


def reset_cache() -> None:
    global _epoch
    _epoch += 1
    _failed.clear()
    _page_memo.clear()
    _key_epoch.clear()
    _cache.reset()
    reset_lock_cache()


def _with_last_good(held: dict[str, Any], why: str) -> dict[str, Any]:
    clock = str(held.get("updated_at") or "")[11:16]
    when = f" from {clock}Z" if clock else ""
    return {**held, "error": f"Showing the last good read{when}: {why}"}


async def overview(
    *,
    fresh: bool = False,
    scope: str = SCOPE_COMPANY,
    login: str | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> dict[str, Any]:
    """The answer for one scope: cached 300 s, a failed load keeps the last good one.

    *scope* ``"company"`` is the server's list; ``"mine"`` is *login*'s own, read with their own
    tokens. Everything held (answer, failed-first-load memory, last good answer) is keyed by
    (scope, login), so one login's answer is never served to another. *transport* is for tests.
    """
    key = cache_key(scope, login)
    settings = _settings_for(scope, login)
    transport = transport or TRANSPORT

    def loaded(use: Settings) -> Any:
        if transport is None:
            return load(use)
        return load(use, transport=transport)

    if not settings.configured:
        return await loaded(settings)

    async def do_load() -> dict[str, Any]:
        answer = await loaded(settings)
        # A load that read repos and got an error from every one is an outage, not news:
        # raise so the cache keeps the last good answer.
        if answer["repos"] and all(r["error"] for r in answer["repos"]):
            raise _AllFailed(answer)
        return answer

    epoch = _epoch_of(key)
    failed = _failed.get(key)
    if not fresh and failed is not None and _cache.peek(key) is None:
        if time.monotonic() - failed[0] < FAILED_MEMORY_S:
            return failed[1]
        _failed.pop(key, None)
    try:
        value = await _cache.get(key, do_load, fresh=fresh)
    except Exception as exc:  # noqa: BLE001 - the card answers with an error line instead
        _logger.warning("git card: load failed (%s)", type(exc).__name__)
        held = _cache.peek(key)
        if held is not None:
            return _with_last_good(held, _describe(exc))
        if isinstance(exc, _AllFailed):
            failed_answer = {**exc.payload, "error": str(exc)}
            if epoch == _epoch_of(key):
                _failed[key] = (time.monotonic(), failed_answer)
            return failed_answer
        answer = await loaded(
            PersonalSettings(login or "", []) if scope == SCOPE_MINE else Settings({})
        )
        answer.update(
            configured=True,
            setup=None,
            accounts=[],
            repos=[],
            sweep=None,
            error=f"The Git repos read failed ({type(exc).__name__}); try again.",
        )
        return answer
    if epoch == _epoch_of(key):
        _failed.pop(key, None)
    why = _cache.last_error(key)
    # A value served while its refresh failed (an error is set only when the value is older).
    return _with_last_good(value, why) if why else value


def merge(company: dict[str, Any], mine: dict[str, Any]) -> dict[str, Any]:
    """An admin's All view: the company answer and their own, a repo in both shown once.

    The duplicate is dropped from the personal side (the company row stays, under Company),
    except when the company row has an error and the personal row read fine. The dedupe is by
    repo name, case-insensitive, and happens here only. Totals are recomputed for the merged
    rows. Fresh copies; neither held answer is changed. Personal rows keep ``scope: "mine"`` and
    company rows ``scope: "company"``. ``accounts`` lists both scopes (each entry carries its
    ``scope``), ``error`` is the company's else the personal one's, and
    ``configured`` is true when either side is.
    """
    theirs = {str(r["repo"]).lower(): r for r in mine.get("repos") or []}
    own = {str(r["repo"]).lower() for r in company.get("repos") or []}
    rows = []
    for row in company.get("repos") or []:
        # The company row wins, unless it failed to read and the admin's own token read it fine:
        # then the working row is the one worth showing (it keeps its "mine" scope).
        personal = theirs.get(str(row["repo"]).lower())
        if personal is not None and row.get("error") and not personal.get("error"):
            rows.append(personal)
        else:
            rows.append(row)
    rows += [r for r in mine.get("repos") or [] if str(r["repo"]).lower() not in own]
    rows.sort(key=lambda r: (r["view_only"], _count(r) == 0, -_count(r), r["repo"].lower()))
    accounts = [
        *({**a, "scope": SCOPE_COMPANY} for a in company.get("accounts") or []),
        *({**a, "scope": SCOPE_MINE} for a in mine.get("accounts") or []),
    ]
    return {
        "configured": bool(company.get("configured") or mine.get("configured")),
        "setup": company.get("setup") if not mine.get("configured") else None,
        "updated_at": max(str(company.get("updated_at") or ""), str(mine.get("updated_at") or "")),
        "error": company.get("error") or mine.get("error"),
        "accounts": accounts,
        "totals": _totals(rows),
        "repos": rows,
        "sweep": company.get("sweep"),
        "scope": "all",
        **({"list_cut": True} if company.get("list_cut") or mine.get("list_cut") else {}),
    }


def _count(r: dict[str, Any]) -> int:
    return len(r["prs"]) + len(r["alerts"]) + len(r["branches"])


# ── Needs you (2026-11-03, Admin Center v1.5) ─────────────────────────────────

#: A waiting alert older than this is stuck (GitHub's alerts lag a fix by hours).
STUCK_HOURS = 24
#: A branch with no PR, not kept, whose last commit is older than this is an old branch.
OLD_BRANCH_DAYS = 14
#: The Dependency Sweep runs every 6 hours; no run started for 8 is late.
SWEEP_LATE_HOURS = 8
_SEVERITIES = ("CRITICAL", "HIGH", "MODERATE", "LOW")
_RANK = {"urgent": 0, "review": 1}
_ID_SUBJECT_MAX = 100


def _plural(n: int, one: str, many: str | None = None) -> str:
    return f"{n} {one if n == 1 else (many or one + 's')}"


def _stamp(iso: str | None) -> float:
    """Epoch seconds of an ISO time, ``nan`` when it is missing or unreadable."""
    if not iso:
        return float("nan")
    try:
        return datetime.fromisoformat(str(iso).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return float("nan")


def _when_short(iso: str, now: float) -> str:
    at = _stamp(iso)
    if math.isnan(at):
        return ""
    moment = datetime.fromtimestamp(at, UTC)
    if moment.date() == datetime.fromtimestamp(now, UTC).date():
        return moment.strftime("%H:%M UTC")
    return f"{moment.strftime('%b')} {moment.day}"


def _age_text(iso: str | None, now: float) -> str:
    at = _stamp(iso)
    if math.isnan(at):
        return ""
    days = max(0, int((now - at) // 86400))
    if days == 0:
        return "today"
    if days < 14:
        return _plural(days, "day")
    if days < 60:
        return _plural(days // 7, "week")
    if days < 730:
        return _plural(days // 30, "month")
    return _plural(days // 365, "year")


def _ago(iso: str | None, now: float) -> str:
    age = _age_text(iso, now)
    return age if age in ("today", "") else f"{age} ago"


def _short_name(repo: str) -> str:
    return repo.partition("/")[2] or repo


def _subject(text: str) -> str:
    """*text* for an id: short, with a hash when it had to be cut (ids become label values)."""
    if len(text) <= _ID_SUBJECT_MAX:
        return text
    return text[: _ID_SUBJECT_MAX - 9] + "~" + hashlib.sha1(text.encode()).hexdigest()[:8]


def _package_list(alerts: list[dict[str, Any]]) -> str:
    names = list(dict.fromkeys(str(a.get("package") or "") for a in alerts))
    shown = ", ".join(names[:3])
    return f"{shown} and {len(names) - 3} more" if len(names) > 3 else shown


def error_code(note: str | None) -> str:
    """A repo or account error as a short code (state keeps the code, never the sentence)."""
    text = (note or "").lower()
    for needle, code in (
        ("no token", "no_token"),
        ("rate limit", "rate_limited"),
        ("dependabot alerts", "alerts_permission"),
        ("did not answer", "no_answer"),
        ("unreadable", "unreadable"),
        ("not found", "not_found"),
        ("cannot", "refused"),
    ):
        if needle in text:
            return code
    return "error"


def _namespace(login: str | None) -> str:
    """The prefix of a Needs you item id and scope id: ``git``, or ``git-mine:<login>``."""
    return f"git-mine:{login}" if login else "git"


def needs(
    overview: Mapping[str, Any], now: float | None = None, *, login: str | None = None
) -> list[dict[str, Any]]:
    """What needs the owner on the Git card: a port of ``analyze()`` in ``git.ts`` (2026-11-03).

    One rule set serves the badge and the card, so the card draws this list rather than its
    own. Each item is ``{id, kind, tone, title, meta, url, state, scope}``: *tone* is
    ``urgent`` for the card's err rows and ``review`` for its accent and warn rows, *state* is
    the small canonical fact set the "worse" rule compares (never a time or an "ago" text),
    and *scope* names the read that vouches for the item's absence. Worst first, in
    ``analyze()``'s order inside a tone. ``tests/test_git_center.py`` and ``git.test.ts`` pin
    the ids against the old TypeScript rules.

    With *login* (a personal answer) the item ids read ``git-mine:<login>:<rest>`` and the
    scope ids ``git-mine:<login>:repo:<owner/name>``, so one admin's items can never collide
    with, or be found as, another's or the company's.
    """
    when = time.time() if now is None else now
    space = _namespace(login)
    repos = [r for r in overview.get("repos") or [] if not r.get("view_only")]
    head: list[dict[str, Any]] = []
    pr_rows: list[dict[str, Any]] = []
    stuck_rows: list[dict[str, Any]] = []
    branch_rows: list[dict[str, Any]] = []

    def item(kind: str, tone: str, subject: str, scope: str, **fields: Any) -> dict[str, Any]:
        return {
            "id": f"{space}:{kind}:{_subject(subject)}",
            "kind": kind,
            "tone": tone,
            "scope": space + scope[len("git") :],
            **fields,
        }

    explained: set[str] = set()
    for account in overview.get("accounts") or []:
        if account.get("connected"):
            continue
        note = account.get("note") or "No token for this account."
        explained.add(note)
        owner = str(account.get("owner") or "")
        head.append(
            item(
                "account",
                "urgent",
                owner,
                "git:accounts",
                title=f"{owner} is not connected",
                meta=note,
                url=f"https://github.com/{owner}",
                state={"owner": owner, "error": "no_token"},
            )
        )
    for repo in repos:
        if repo.get("error") and repo["error"] not in explained:
            head.append(
                item(
                    "repo-error",
                    "urgent",
                    repo["repo"],
                    f"git:repo:{repo['repo']}",
                    title=f"{repo['repo']} could not be read",
                    meta=repo["error"],
                    url=repo.get("url") or f"https://github.com/{repo['repo']}",
                    state={"repo": repo["repo"], "error": error_code(repo["error"])},
                )
            )

    sweep = overview.get("sweep")
    runs = (sweep or {}).get("runs") or []
    latest = runs[0] if runs else None
    if sweep and not latest and not sweep.get("error"):
        head.append(
            item(
                "sweep",
                "urgent",
                "dependency-sweep",
                "git:sweep",
                title="No Dependency Sweep runs found",
                meta="The workflow is set up but has no runs yet",
                url=sweep.get("url") or "",
                state={"issue": "none", "run": None},
            )
        )
    if sweep and latest:
        started = _stamp(latest.get("created_at"))
        run_id = latest.get("id")
        if (latest.get("conclusion") or "") in ("failure", "timed_out", "startup_failure"):
            head.append(
                item(
                    "sweep",
                    "urgent",
                    "dependency-sweep",
                    "git:sweep",
                    title="The latest Dependency Sweep failed",
                    meta=f"Started {_when_short(latest.get('created_at') or '', when)}",
                    url=latest.get("url") or "",
                    state={"issue": "failed", "run": run_id},
                )
            )
        elif not math.isnan(started) and when - started > SWEEP_LATE_HOURS * 3600:
            hours = int((when - started) // 3600)
            head.append(
                item(
                    "sweep",
                    "urgent",
                    "dependency-sweep",
                    "git:sweep",
                    title=f"No Dependency Sweep has started in {_plural(hours, 'hour')}",
                    meta="It runs every 6 hours; the last one started "
                    f"{_when_short(latest.get('created_at') or '', when)}",
                    url=latest.get("url") or "",
                    state={"issue": "late", "run": run_id},
                )
            )

    for repo in repos:
        name = _short_name(repo["repo"])
        scope = f"git:repo:{repo['repo']}"
        facts = repo.get("_facts") or {}
        alerts = repo.get("alerts") or []
        fix = [a for a in alerts if a.get("bucket") == "fix"]
        if fix:
            worst = next(
                (lvl for lvl in _SEVERITIES if any(a.get("severity") == lvl for a in fix)), None
            )
            head.append(
                item(
                    "alerts-fix",
                    "urgent",
                    repo["repo"],
                    scope,
                    title=f"{_plural(len(fix), 'alert')} to fix: {_package_list(fix)}",
                    meta=f"{name} · worst is {worst.lower()}" if worst else name,
                    url=f"https://github.com/{repo['repo']}/security/dependabot",
                    state={
                        "repo": repo["repo"],
                        "alerts": sorted(int(a.get("number") or 0) for a in fix),
                        "severity": worst,
                    },
                )
            )
        stuck: list[dict[str, Any]] = []
        for alert in (a for a in alerts if a.get("bucket") == "waiting"):
            since = _stamp(alert.get("waiting_since") or alert.get("created_at"))
            if not math.isnan(since) and when - since > STUCK_HOURS * 3600:
                stuck.append(alert)
        if stuck:
            oldest = sorted(
                (a.get("waiting_since") or a.get("created_at") or "" for a in stuck),
                key=lambda v: (math.isnan(_stamp(v)), _stamp(v)),
            )[0]
            stuck_rows.append(
                item(
                    "alerts-stuck",
                    "review",
                    repo["repo"],
                    scope,
                    title=f"{_plural(len(stuck), 'alert')} fixed on {repo.get('default_branch')}, "
                    "still open on GitHub",
                    meta=f"{name} · stuck {_age_text(oldest, when)} · a lockfile push or "
                    "GitHub Support clears them",
                    url=f"https://github.com/{repo['repo']}/security/dependabot",
                    state={
                        "repo": repo["repo"],
                        "alerts": sorted(int(a.get("number") or 0) for a in stuck),
                    },
                )
            )
        for pr in repo.get("prs") or []:
            if pr.get("draft"):
                continue
            number = pr.get("number")
            fact = (facts.get("prs") or {}).get(str(number)) or {}
            sha = str(fact.get("sha") or "")
            checks = pr.get("checks")
            merge = pr.get("merge_state") or "UNKNOWN"
            failing = checks in ("FAILURE", "ERROR")
            conflicts = merge == "DIRTY"
            subject = f"{repo['repo']}#{number}"
            url = pr.get("url") or ""
            if failing or conflicts:
                words = ", ".join(
                    w for w, on in (("checks failing", failing), ("conflicts", conflicts)) if on
                )
                pr_rows.append(
                    item(
                        "pr-failing",
                        "urgent",
                        subject,
                        scope,
                        title=f"Fix #{number} {pr.get('title')}",
                        meta=f"{name} · {words}",
                        url=url,
                        cross_repo=bool(pr.get("cross_repo", True)),
                        state={
                            "repo": repo["repo"],
                            "number": number,
                            "sha": sha,
                            "failing": fact.get("failing") if failing else [],
                            "conflicts": conflicts,
                        },
                    )
                )
            elif merge == "CLEAN":
                green = "checks green" if checks == "SUCCESS" else _checks_text(checks)
                pr_rows.append(
                    item(
                        "pr-ready",
                        "review",
                        subject,
                        scope,
                        title=f"Merge #{number} {pr.get('title')}",
                        meta=f"{name} · {green}, no conflicts",
                        url=url,
                        state={"repo": repo["repo"], "number": number, "sha": sha},
                    )
                )
            elif (
                merge == "UNSTABLE"
                or checks == "PENDING"
                # The sweep rebases and re-reads these itself; BLOCKED is a review it won't give.
                or (_is_dependabot(pr) and merge in ("BEHIND", "UNKNOWN"))
            ):
                continue
            elif merge in ("BEHIND", "BLOCKED"):
                behind = merge == "BEHIND"
                pr_rows.append(
                    item(
                        "pr-update",
                        "review",
                        subject,
                        scope,
                        title=(
                            f"Update #{number} {pr.get('title')}"
                            if behind
                            else f"#{number} {pr.get('title')} is blocked"
                        ),
                        meta=(
                            f"{name} · behind {repo.get('default_branch')}"
                            if behind
                            else f"{name} · waiting on a review or a required check"
                        ),
                        url=url,
                        cross_repo=bool(pr.get("cross_repo", True)),
                        state={
                            "repo": repo["repo"],
                            "number": number,
                            "sha": sha,
                            "merge": merge,
                        },
                    )
                )
        old: list[tuple[float, dict[str, Any]]] = []
        for branch in repo.get("branches") or []:
            if branch.get("kept"):
                continue
            at = _stamp(branch.get("last_commit_at"))
            # A branch with no readable date is not called old: nothing says it is.
            if not math.isnan(at) and when - at > OLD_BRANCH_DAYS * 86400:
                old.append((at, branch))
        for at, branch in old:
            ahead = branch.get("ahead")
            bname = str(branch.get("name") or "")
            branch_rows.append(
                {
                    "_at": at,
                    **item(
                        "branch",
                        "review",
                        f"{repo['repo']}:{bname}",
                        scope,
                        title=(
                            f"{bname} has {_plural(ahead, 'commit')} and no PR"
                            if ahead
                            else f"{bname}, no PR"
                        ),
                        meta=f"{name} · last commit {_ago(branch.get('last_commit_at'), when)}",
                        url=branch.get("url") or "",
                        state={
                            "repo": repo["repo"],
                            "branch": bname,
                            "sha": str((facts.get("branches") or {}).get(bname) or ""),
                        },
                    ),
                }
            )
    # Old branches sort oldest first across repos, as the card did.
    branch_rows.sort(key=lambda row: row["_at"])
    for row in branch_rows:
        del row["_at"]
    ordered = [*head, *pr_rows, *stuck_rows, *branch_rows]
    ordered.sort(key=lambda row: _RANK[row["tone"]])  # stable: the order built above stands
    return ordered


def _checks_text(checks: str | None) -> str:
    if checks == "SUCCESS":
        return "checks passed"
    if checks in ("FAILURE", "ERROR"):
        return "checks failing"
    if checks in ("PENDING", "EXPECTED"):
        return "checks running"
    return "no checks"


def _is_dependabot(pr: Mapping[str, Any]) -> bool:
    return str(pr.get("author") or "").lower().startswith("dependabot")


def scope_ids(overview: Mapping[str, Any], login: str | None = None) -> list[dict[str, Any]]:
    """The reads behind :func:`needs`: ``{id, ok}`` per repo, the accounts and the sweep.

    A scope is not ``ok`` when its read failed or when the lists were cut to fit the page,
    since an item missing from either is not known to be gone. With *login*, the ids are the
    personal ones (:func:`needs`).
    """
    space = _namespace(login)
    cut = bool(overview.get("list_cut"))
    out = [{"id": f"{space}:accounts", "ok": bool(overview.get("configured"))}]
    for repo in overview.get("repos") or []:
        if repo.get("view_only"):
            continue
        truncated = bool((repo.get("_facts") or {}).get("truncated"))
        out.append(
            {
                "id": f"{space}:repo:{repo['repo']}",
                "ok": not repo.get("error") and not cut and not truncated,
            }
        )
    sweep = overview.get("sweep")
    if sweep is not None:
        out.append({"id": f"{space}:sweep", "ok": not sweep.get("error")})
    return out


async def cached(
    *, kick: bool = True, scope: str = SCOPE_COMPANY, login: str | None = None
) -> tuple[dict[str, Any] | None, float | None, str | None]:
    """(held answer, its age in seconds, why the last refresh failed): the badge's read.

    Nothing is awaited: ``(None, None, ...)`` when nothing is held yet. With *kick*, a missing
    or past-TTL answer starts one background :func:`overview` (single-flight in the cache), so
    the next read is fresh; this never passes ``fresh=True``. A personal read (*scope*
    ``"mine"``) names the *login* it is for and sees only that login's own answer.
    """
    key = cache_key(scope, login)
    held = _cache.peek(key)
    age = _cache.age_s(key)
    if kick and (held is None or (age is not None and age >= TTL_S)):
        _kick(scope, login)
    return held, age, _cache.last_error(key)


async def refresh_due(scope: str = SCOPE_COMPANY, login: str | None = None) -> bool:
    """Whether :func:`cached` with *kick* would start a background read now."""
    key = cache_key(scope, login)
    if not _settings_for(scope, login).configured or _cache.refreshing(key):
        return False
    age = _cache.age_s(key)
    return _cache.peek(key) is None or (age is not None and age >= TTL_S)


_kicked: set[asyncio.Task[Any]] = set()


def _kick(scope: str = SCOPE_COMPANY, login: str | None = None) -> None:
    key = cache_key(scope, login)
    # A switched-off Git card reads nothing, for anyone: re-checked here because a scheduled
    # refresh may have been queued before the switch.
    if admin_cards.all_off("git") or not _settings_for(scope, login).configured:
        return
    if _cache.refreshing(key):
        return

    async def run() -> None:
        if admin_cards.all_off("git"):
            return
        try:
            await overview(scope=scope, login=login)
        except Exception as exc:  # noqa: BLE001 (a background refresh never raises into a route)
            _logger.warning("git needs: background read failed (%s)", type(exc).__name__)

    try:
        task = asyncio.get_running_loop().create_task(run())
    except RuntimeError:
        return
    _kicked.add(task)
    task.add_done_callback(_kicked.discard)


__all__ = [
    "SCOPE_COMPANY",
    "SCOPE_MINE",
    "PersonalSettings",
    "Settings",
    "cache_key",
    "cached",
    "in_range",
    "invalidate",
    "load",
    "merge",
    "needs",
    "overview",
    "owner_env_name",
    "page",
    "refresh_due",
    "reset_cache",
    "scope_ids",
    "wire",
]
