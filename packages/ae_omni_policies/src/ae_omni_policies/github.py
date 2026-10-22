"""GitHub client for the Your move pull requests and the PR cards' Merge button.

Added 2026-09-24. Reid merges from his phone while travelling and kept asking
which pull requests were his to act on; the server had no GitHub credential,
so this reads an optional one from the environment:

- ``AE_GITHUB_TOKEN``: a fine-grained personal access token on the repos
  below. A secret: it reaches the server through ``deploy/pull-secrets.sh``
  (Key Vault ``omnigent-ae-github-token``), never through the repo, and is
  never logged or returned. Read-only (Pull requests, Checks) was enough for
  Your move; the PR cards' Merge button (2026-09-24, :mod:`ae_omni_policies.prs`)
  needs Contents and Pull requests read and write, Checks and Commit statuses
  read, and Workflows read and write for a pull request that changes
  ``.github/workflows`` (deploy/README.md, "The GitHub token", has the list).
- ``AE_GITHUB_REPOS``: comma-separated ``owner/repo`` list for Your move's
  pull requests; none by default, and the section says to set it
  (:data:`NO_REPOS`).
- ``AE_GITHUB_LOGIN``: whose pull requests count; by default the token's own
  login, read from ``GET /user`` once per token for the life of the process
  (2026-10-22: the defaults used to name Analytic Endeavors' repos and login).
  A token that cannot read ``/user`` (a GitHub App token) needs it set.
- ``AE_GITHUB_API_URL``: the API origin, default ``https://api.github.com``;
  set only for a local mock (the screenshots) or GitHub Enterprise.

Without a token the section reports how to turn it on instead of failing.

Per repo, one ``GET /repos/{repo}/pulls?state=open`` (author and requested
reviewers are on every row), then one ``GET .../commits/{sha}/check-runs``
per matching pull request for its CI state. Every call is bounded by
:data:`REQUEST_TIMEOUT_S`; one repo failing is reported next to the others'
results, never instead of them.
"""

from __future__ import annotations

import asyncio
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

import httpx

from ae_omni_policies import __version__

API_URL = "https://api.github.com"
DEFAULT_REPOS: tuple[str, ...] = ()
DEFAULT_LOGIN = ""
REQUEST_TIMEOUT_S = 10.0
# One person's open pull requests across a handful of repos; a cap keeps a
# busy repo from turning one page load into a hundred check-run calls.
MAX_PULLS = 30
TOKEN_ENV = "AE_GITHUB_TOKEN"
REPOS_ENV = "AE_GITHUB_REPOS"
LOGIN_ENV = "AE_GITHUB_LOGIN"
API_URL_ENV = "AE_GITHUB_API_URL"
USER_AGENT = f"omnigent-ae/{__version__} (+https://github.com/analyticendeavors/omnigent-ae)"
# Owner and name as GitHub spells them; no dot-only segment, so never a path walk.
_REPO = re.compile(r"^[A-Za-z0-9_-][A-Za-z0-9_.-]*/[A-Za-z0-9_-][A-Za-z0-9_.-]*$")

CiState = Literal["passing", "failing", "pending", "none", "unknown"]
# Check-run conclusions that need a person (GitHub's list). "neutral" and
# "skipped" pass; "stale" is GitHub giving up on a run, which reads as failing.
_FAILING = {"failure", "timed_out", "cancelled", "action_required", "startup_failure", "stale"}

HOW_TO_ENABLE = (
    "Pull requests are off: the server has no GitHub token. Create a fine-grained token "
    "(Contents and Pull requests: read and write; Checks and Commit statuses: read; "
    "Workflows: read and write) for the repos in AE_GITHUB_REPOS, set it as "
    "AE_GITHUB_TOKEN in the server's environment, then restart the stack "
    "(deploy/README.md, 'The GitHub token')."
)
# (API origin, token) to the token's own login; a token's login never changes.
_logins: dict[tuple[str, str], str] = {}


def reset_login_cache() -> None:
    _logins.clear()


NO_REPOS = (
    "Pull requests are off: no repos to read. Set AE_GITHUB_REPOS to a comma-separated "
    "owner/repo list in the server's environment, then restart the stack."
)


@dataclass(frozen=True)
class GitHubSettings:
    """Where to look and with what; all from the environment."""

    token: str = ""
    repos: tuple[str, ...] = DEFAULT_REPOS
    login: str = DEFAULT_LOGIN
    api_url: str = API_URL

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> GitHubSettings:
        """Read ``AE_GITHUB_TOKEN``, ``AE_GITHUB_REPOS`` and ``AE_GITHUB_LOGIN``.

        A malformed repo entry is dropped rather than sent to GitHub as a path.
        """
        env = os.environ if environ is None else environ
        raw = env.get(REPOS_ENV) or ""
        repos = tuple(item.strip() for item in raw.split(",") if _REPO.match(item.strip()))
        return cls(
            token=(env.get(TOKEN_ENV) or "").strip(),
            repos=repos or DEFAULT_REPOS,
            login=(env.get(LOGIN_ENV) or "").strip() or DEFAULT_LOGIN,
            api_url=(env.get(API_URL_ENV) or "").strip().rstrip("/") or API_URL,
        )

    @property
    def configured(self) -> bool:
        return bool(self.token)


class GitHubError(Exception):
    """A GitHub call that did not answer 200; the message says which and why, never the token."""


def ci_state(check_runs: list[dict[str, Any]]) -> CiState:
    """Fold a commit's check runs into one state: any failing, else any running, else passing."""
    if not check_runs:
        return "none"
    if any(run.get("conclusion") in _FAILING for run in check_runs):
        return "failing"
    if any(run.get("status") != "completed" for run in check_runs):
        return "pending"
    return "passing"


def _why(response: httpx.Response) -> str:
    try:
        message = response.json().get("message")
    except (ValueError, AttributeError):
        message = None
    hint = ""
    if response.status_code == 401:
        hint = "; the token is invalid or expired, replace it in Key Vault"
    elif response.status_code in (403, 404):
        hint = "; check the token can read this repo (Pull requests and Checks: read)"
    return f"{response.status_code} {message or response.reason_phrase}{hint}"


@dataclass(frozen=True)
class ConditionalPull:
    """What a conditional ``GET .../pulls/{n}`` answered, for the merge watcher.

    ``status`` is GitHub's code (200, 304, 403, 429, ...); ``pull`` is the body
    on a 200 only; ``etag`` is the validator to send next time (GitHub's, or the
    one sent when a 304 carried none); ``headers`` holds the rate-limit headers
    (``retry-after``, ``x-ratelimit-remaining``, ``x-ratelimit-reset``).
    """

    status: int
    etag: str | None
    pull: dict[str, Any] | None
    headers: dict[str, str]


_RATE_HEADERS = ("retry-after", "x-ratelimit-remaining", "x-ratelimit-reset")


# The merge methods a card offers, in the order the default is picked from.
MergeMethod = Literal["squash", "merge"]
MERGE_METHODS: tuple[MergeMethod, ...] = ("squash", "merge")


@dataclass(frozen=True)
class MergeOutcome:
    """What ``PUT .../merge`` answered: merged or not, and GitHub's own sentence."""

    status: int
    merged: bool
    sha: str | None
    message: str


@dataclass(frozen=True)
class UpdateBranchOutcome:
    """A 202 acknowledges a queued update; it does not confirm a changed head."""

    status: int
    accepted: bool
    message: str


class GitHubClient:
    """An async context manager over ``httpx.AsyncClient`` with the token in the header."""

    def __init__(
        self, settings: GitHubSettings, transport: httpx.AsyncBaseTransport | None = None
    ):
        self._settings = settings
        self._http = httpx.AsyncClient(
            base_url=settings.api_url,
            timeout=REQUEST_TIMEOUT_S,
            transport=transport,
            headers={
                "Authorization": f"Bearer {settings.token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": USER_AGENT,
            },
        )

    async def __aenter__(self) -> GitHubClient:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self._http.aclose()

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        try:
            response = await self._http.get(path, params=params)
        except httpx.HTTPError as exc:
            raise GitHubError(f"GET {path} failed: {type(exc).__name__}") from exc
        if response.status_code != 200:
            raise GitHubError(f"GET {path} answered {_why(response)}")
        try:
            return response.json()
        except ValueError as exc:
            raise GitHubError(f"GET {path} answered invalid JSON") from exc

    async def tag_names(self, repo: str, prefix: str) -> list[str]:
        """Every tag name starting with *prefix*, through the matching-refs call.

        For the Session Dashboard's release row. Unlike ``/tags`` this is every
        match in one answer, not a page in GitHub's own order.
        """
        refs = await self._get(f"/repos/{repo}/git/matching-refs/tags/{prefix}")
        if not isinstance(refs, list):
            raise GitHubError(f"GET matching-refs for {repo} answered with no list")
        names: list[str] = []
        for ref in refs:
            name = ref.get("ref") if isinstance(ref, dict) else None
            if isinstance(name, str) and name.startswith("refs/tags/"):
                names.append(name.removeprefix("refs/tags/"))
        return names

    async def pull(self, repo: str, number: int) -> dict[str, Any]:
        """``GET /repos/{repo}/pulls/{number}``: the one call that carries mergeability."""
        payload = await self._get(f"/repos/{repo}/pulls/{number}")
        if not isinstance(payload, dict):
            raise GitHubError(f"GET /repos/{repo}/pulls/{number} answered a non-object")
        return payload

    async def pull_if_changed(self, repo: str, number: int, etag: str | None) -> ConditionalPull:
        """``GET /repos/{repo}/pulls/{number}`` with ``If-None-Match`` when *etag* is set.

        For the merge watcher (``merge_watch.py``, 2026-10-21), which asks every
        10 seconds: GitHub answers 304 while nothing changed, and a 304 does not
        count against the token's rate limit. Every status is returned, not
        raised, so the caller can back off on 403 and 429.

        :raises GitHubError: Only when the request itself failed (network,
            timeout) or a 200 carried no JSON object.
        """
        path = f"/repos/{repo}/pulls/{number}"
        headers = {"If-None-Match": etag} if etag else None
        try:
            response = await self._http.get(path, headers=headers)
        except httpx.HTTPError as exc:
            raise GitHubError(f"GET {path} failed: {type(exc).__name__}") from exc
        rate = {name: response.headers[name] for name in _RATE_HEADERS if name in response.headers}
        validator = response.headers.get("etag") or etag
        if response.status_code != 200:
            return ConditionalPull(response.status_code, validator, None, rate)
        try:
            payload = response.json()
        except ValueError as exc:
            raise GitHubError(f"GET {path} answered invalid JSON") from exc
        if not isinstance(payload, dict):
            raise GitHubError(f"GET {path} answered a non-object")
        return ConditionalPull(200, response.headers.get("etag"), payload, rate)

    async def reviews(self, repo: str, number: int) -> list[dict[str, Any]]:
        """Every review on the pull request, oldest first (GitHub's order)."""
        payload = await self._get(f"/repos/{repo}/pulls/{number}/reviews", {"per_page": 100})
        rows = payload if isinstance(payload, list) else []
        return [row for row in rows if isinstance(row, dict)]

    async def issue_comments(self, repo: str, number: int) -> list[dict[str, Any]]:
        """Read all comment pages, or fail instead of presenting a partial latest report.

        Construct each path locally, never forward credentials to pagination URLs.
        Ten full pages is an explicit failure, even if exactly 1,000 comments exist.
        """
        path = f"/repos/{repo}/issues/{number}/comments"
        rows: list[dict[str, Any]] = []
        for page in range(1, 11):
            payload = await self._get(path, {"per_page": 100, "page": page})
            if not isinstance(payload, list) or any(not isinstance(r, dict) for r in payload):
                raise GitHubError(f"GET {path} answered malformed comments")
            rows.extend(payload)
            if len(payload) < 100:
                return rows
        raise GitHubError("Compliance comments exceed the safe pagination limit; open GitHub.")

    async def update_branch(self, repo: str, number: int, sha: str) -> UpdateBranchOutcome:
        """Queue one update pinned to the displayed head, without retrying a write."""
        path = f"/repos/{repo}/pulls/{number}/update-branch"
        try:
            response = await self._http.put(path, json={"expected_head_sha": sha})
        except httpx.HTTPError as exc:
            raise GitHubError(
                f"PUT {path} failed: {type(exc).__name__}; the update outcome is unknown. "
                "Refresh the card before trying again."
            ) from exc
        if response.status_code == 202:
            return UpdateBranchOutcome(
                202, True, "Branch update accepted; GitHub is processing it."
            )
        return UpdateBranchOutcome(response.status_code, False, _why(response))

    async def check_runs(self, repo: str, sha: str) -> list[dict[str, Any]]:
        """The latest check run per check on *sha* (GitHub's default ``filter=latest``)."""
        payload = await self._get(f"/repos/{repo}/commits/{sha}/check-runs", {"per_page": 100})
        runs = payload.get("check_runs") if isinstance(payload, dict) else None
        return [run for run in runs or [] if isinstance(run, dict)]

    async def statuses(self, repo: str, sha: str) -> list[dict[str, Any]]:
        """Commit statuses on *sha*, one per context (the combined status's rows)."""
        payload = await self._get(f"/repos/{repo}/commits/{sha}/status", {"per_page": 100})
        rows = payload.get("statuses") if isinstance(payload, dict) else None
        return [row for row in rows or [] if isinstance(row, dict)]

    async def compare_status(self, repo: str, base: str, head: str) -> str:
        """GitHub's compare ``status`` of *head* against *base*.

        ``ahead`` or ``identical`` means *head* contains *base*; ``behind`` or
        ``diverged`` means it does not. For the action dock's deploy line
        (``pr_deploy.py``, 2026-10-20).
        """
        path = f"/repos/{repo}/compare/{base}...{head}"
        payload = await self._get(path, {"per_page": 1})
        status = payload.get("status") if isinstance(payload, dict) else None
        if not isinstance(status, str):
            raise GitHubError(f"GET {path} answered with no status")
        return status

    async def repository(self, repo: str) -> dict[str, Any]:
        """``GET /repos/{repo}``: the merge methods the repo allows."""
        payload = await self._get(f"/repos/{repo}")
        if not isinstance(payload, dict):
            raise GitHubError(f"GET /repos/{repo} answered a non-object")
        return payload

    async def merge(self, repo: str, number: int, method: MergeMethod, sha: str) -> MergeOutcome:
        """``PUT /repos/{repo}/pulls/{number}/merge`` pinned to *sha*.

        GitHub refuses (409) when the head moved past *sha*, so a commit pushed
        after the card was read is never merged unseen. Any answer is returned,
        not raised: 405, 409 and 422 carry the sentence the card shows.
        """
        path = f"/repos/{repo}/pulls/{number}/merge"
        try:
            response = await self._http.put(path, json={"merge_method": method, "sha": sha})
        except httpx.HTTPError as exc:
            raise GitHubError(f"PUT {path} failed: {type(exc).__name__}") from exc
        try:
            body = response.json()
        except ValueError:
            body = {}
        body = body if isinstance(body, dict) else {}
        if response.status_code == 200:
            return MergeOutcome(
                status=200,
                merged=bool(body.get("merged", True)),
                sha=body.get("sha") if isinstance(body.get("sha"), str) else None,
                message=str(body.get("message") or "Pull Request successfully merged"),
            )
        return MergeOutcome(
            status=response.status_code, merged=False, sha=None, message=_why(response)
        )

    async def mark_ready(self, node_id: str) -> None:
        """Mark a draft pull request ready using its REST ``node_id``."""
        path = "/graphql"
        query = (
            "mutation($pullRequestId: ID!) { "
            "markPullRequestReadyForReview(input: {pullRequestId: $pullRequestId}) "
            "{ pullRequest { isDraft } } }"
        )
        try:
            response = await self._http.post(
                path, json={"query": query, "variables": {"pullRequestId": node_id}}
            )
        except httpx.HTTPError as exc:
            raise GitHubError(f"POST {path} failed: {type(exc).__name__}") from exc
        if response.status_code != 200:
            raise GitHubError(f"POST {path} answered {_why(response)}")
        try:
            payload = response.json()
        except ValueError as exc:
            raise GitHubError(f"POST {path} answered invalid JSON") from exc
        if not isinstance(payload, dict) or payload.get("errors"):
            raise GitHubError(f"POST {path} answered GraphQL errors")
        data = payload.get("data")
        mutation = data.get("markPullRequestReadyForReview") if isinstance(data, dict) else None
        pull = mutation.get("pullRequest") if isinstance(mutation, dict) else None
        if not isinstance(pull, dict) or pull.get("isDraft") is not False:
            raise GitHubError(f"POST {path} did not confirm a ready pull request")

    async def close_issue(
        self, repo: str, number: int, reason: Literal["completed", "not_planned"], comment: str
    ) -> None:
        """Comment on the issue, then close it with *reason*.

        Needs Issues read and write on the token (added 2026-09-28 for the
        ``complete_task`` tool). An issue already closed is closed again,
        which GitHub accepts; the comment still lands.
        """
        base = f"/repos/{repo}/issues/{number}"
        for method, path, body in (
            ("POST", f"{base}/comments", {"body": comment}),
            ("PATCH", base, {"state": "closed", "state_reason": reason}),
        ):
            try:
                response = await self._http.request(method, path, json=body)
            except httpx.HTTPError as exc:
                raise GitHubError(f"{method} {path} failed: {type(exc).__name__}") from exc
            if response.status_code not in (200, 201):
                raise GitHubError(f"{method} {path} answered {_why(response)}")

    async def _ci(self, repo: str, sha: str) -> CiState:
        if not sha:
            return "unknown"
        try:
            payload = await self._get(f"/repos/{repo}/commits/{sha}/check-runs", {"per_page": 100})
        except GitHubError:
            return "unknown"
        runs = payload.get("check_runs") if isinstance(payload, dict) else None
        return ci_state([run for run in runs or [] if isinstance(run, dict)])

    async def login(self) -> str:
        """``AE_GITHUB_LOGIN``, else the token's own login from ``GET /user``."""
        if self._settings.login:
            return self._settings.login
        key = (self._settings.api_url, self._settings.token)
        if key in _logins:
            return _logins[key]
        payload = await self._get("/user")
        login = payload.get("login") if isinstance(payload, dict) else None
        if not isinstance(login, str) or not login:
            raise GitHubError("GET /user answered without a login")
        _logins[key] = login
        return login

    @staticmethod
    def _mine(pull: dict[str, Any], login: str) -> list[str]:
        """Why this pull request is waiting on *login*: authored, review requested, or both."""
        login = login.lower()
        why: list[str] = []
        author = (pull.get("user") or {}).get("login") or ""
        if author.lower() == login:
            why.append("author")
        reviewers = pull.get("requested_reviewers") or []
        if any(((reviewer or {}).get("login") or "").lower() == login for reviewer in reviewers):
            why.append("review_requested")
        return why

    async def open_pulls(self) -> dict[str, Any]:
        """Open pull requests authored by or awaiting a review from the login, with CI state.

        :returns: ``{"login", "repos", "pulls": [...], "errors": [{"repo", "message"}]}``;
            ``pulls`` newest first, capped at :data:`MAX_PULLS`.
        """
        settings = self._settings
        try:
            login = await self.login()
        except GitHubError as exc:
            return {
                "login": "",
                "repos": list(settings.repos),
                "pulls": [],
                "errors": [
                    {
                        "repo": "GitHub",
                        "message": f"could not read the token's own login ({exc}); "
                        "set AE_GITHUB_LOGIN to whose pull requests count",
                    }
                ],
            }

        async def one_repo(repo: str) -> list[dict[str, Any]]:
            rows = await self._get(
                f"/repos/{repo}/pulls",
                {"state": "open", "per_page": 100, "sort": "updated", "direction": "desc"},
            )
            found: list[dict[str, Any]] = []
            for pull in rows if isinstance(rows, list) else []:
                if not isinstance(pull, dict):
                    continue
                why = self._mine(pull, login)
                if why:
                    found.append({"repo": repo, "why": why, "raw": pull})
            return found

        results = await asyncio.gather(
            *(one_repo(repo) for repo in settings.repos), return_exceptions=True
        )
        matches: list[dict[str, Any]] = []
        errors: list[dict[str, str]] = []
        for repo, result in zip(settings.repos, results, strict=True):
            if isinstance(result, GitHubError):
                errors.append({"repo": repo, "message": str(result)})
            elif isinstance(result, BaseException):
                errors.append({"repo": repo, "message": f"{type(result).__name__}"})
            else:
                matches.extend(result)
        matches.sort(key=lambda row: str(row["raw"].get("updated_at") or ""), reverse=True)
        matches = matches[:MAX_PULLS]
        states = await asyncio.gather(
            *(
                self._ci(row["repo"], str((row["raw"].get("head") or {}).get("sha") or ""))
                for row in matches
            )
        )
        pulls = []
        for row, state in zip(matches, states, strict=True):
            raw = row["raw"]
            pulls.append(
                {
                    "repo": row["repo"],
                    "number": raw.get("number"),
                    "title": raw.get("title"),
                    "url": raw.get("html_url"),
                    "author": (raw.get("user") or {}).get("login"),
                    "draft": bool(raw.get("draft")),
                    "updated_at": raw.get("updated_at"),
                    "why": row["why"],
                    "ci": state,
                }
            )
        return {
            "login": login,
            "repos": list(settings.repos),
            "pulls": pulls,
            "errors": errors,
        }


__all__ = [
    "DEFAULT_LOGIN",
    "DEFAULT_REPOS",
    "HOW_TO_ENABLE",
    "MERGE_METHODS",
    "NO_REPOS",
    "ConditionalPull",
    "GitHubClient",
    "GitHubError",
    "GitHubSettings",
    "MergeMethod",
    "MergeOutcome",
    "ci_state",
    "reset_login_cache",
]
