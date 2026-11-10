"""Admin Center Git repos card (2026-10-24): ``/v1/ae/git`` with GitHub mocked.

Nothing here reaches GitHub: every call goes through ``httpx.MockTransport``.
Owners are made up (contoso, fabrikam, northwind).
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import unquote

import httpx
import pytest

from ae_omni_policies import git_center
from ae_omni_policies.git_center import Settings, in_range, load

TOKEN = "github_pat_never_logged_0123456789"
NOW_ENV = {
    "AE_GIT_REPOS": "contoso/web, Contoso/WEB, fabrikam/api",
    "AE_GITHUB_TOKEN": TOKEN,
}


@pytest.fixture(autouse=True)
def _fresh() -> Any:
    git_center.reset_cache()
    yield
    git_center.reset_cache()


def repo_data(
    *,
    default: str = "main",
    prs: list[dict[str, Any]] | None = None,
    refs: list[dict[str, Any]] | None = None,
    alerts: list[dict[str, Any]] | None = None,
    archived: bool = False,
) -> dict[str, Any]:
    return {
        "defaultBranchRef": {"name": default, "target": {"oid": "abc123"}},
        "pushedAt": "2026-10-24T23:10:00Z",
        "isArchived": archived,
        "pullRequests": {"nodes": prs or []},
        "refs": {"nodes": refs or []},
        "vulnerabilityAlerts": {"nodes": alerts or []},
    }


def pr(
    number: int, title: str = "Bump x", updated: str = "2026-10-24T22:00:00Z"
) -> dict[str, Any]:
    return {
        "number": number,
        "title": title,
        "url": f"https://github.com/contoso/web/pull/{number}",
        "isDraft": False,
        "mergeStateStatus": "CLEAN",
        "author": {"login": "dependabot[bot]"},
        "updatedAt": updated,
        "headRefName": "dependabot/x",
        "commits": {"nodes": [{"commit": {"statusCheckRollup": {"state": "SUCCESS"}}}]},
    }


def ref(name: str, prs: int = 0, date: str = "2026-07-20T16:10:50Z") -> dict[str, Any]:
    return {
        "name": name,
        "target": {"committedDate": date},
        "associatedPullRequests": {"totalCount": prs},
    }


def alert(
    number: int,
    package: str = "undici",
    *,
    created: str = "2026-10-20T10:00:00Z",
    eco: str = "NPM",
    manifest: str = "worker/package.json",
    rng: str = ">= 7.0.0, < 7.30.1",
    severity: str = "HIGH",
) -> dict[str, Any]:
    return {
        "number": number,
        "createdAt": created,
        "vulnerableManifestPath": manifest,
        "securityVulnerability": {
            "severity": severity,
            "vulnerableVersionRange": rng,
            "firstPatchedVersion": {"identifier": "7.30.1"},
            "package": {"name": package, "ecosystem": eco},
        },
    }


def npm_lock(**versions: str | list[str]) -> str:
    packages: dict[str, Any] = {"": {"name": "root"}}
    for name, value in versions.items():
        name = name.replace("__", "/")
        for i, v in enumerate([value] if isinstance(value, str) else value):
            key = f"node_modules/{name}" if i == 0 else f"node_modules/zz/node_modules/{name}"
            packages[key] = {"version": v}
    return json.dumps({"lockfileVersion": 3, "packages": packages})


class Fake:
    """A tiny GitHub: repos by name, files by (repo, path), compare counts."""

    def __init__(self) -> None:
        self.repos: dict[str, dict[str, Any] | int] = {}
        self.files: dict[tuple[str, str], str | int] = {}
        self.ahead: dict[str, int] = {}
        self.commits: dict[tuple[str, str], str | int] = {}
        self.runs: list[dict[str, Any]] | int = []
        self.calls: list[str] = []
        self.auth: list[str] = []
        self.graphql_errors: dict[str, list[dict[str, Any]]] = {}
        self.graphql_null: set[str] = set()
        self.compare_queries: list[str] = []
        self.runs_body: Any = None

    def __call__(self, request: httpx.Request) -> httpx.Response:
        url = request.url
        path = unquote(url.path)
        self.calls.append(f"{request.method} {path}")
        self.auth.append(request.headers.get("authorization", ""))
        if request.method == "POST" and path == "/graphql":
            variables = json.loads(request.content)["variables"]
            name = f"{variables['owner']}/{variables['name']}"
            value = self.repos.get(name)
            if isinstance(value, int):
                return httpx.Response(value, json={"message": "nope"})
            body: dict[str, Any] = {"data": {"repository": value}}
            if name in self.graphql_null:
                body = {"data": None}
            if name in self.graphql_errors:
                body["errors"] = self.graphql_errors[name]
            return httpx.Response(200, json=body)
        parts = path.split("/")
        if "/contents/" in path:
            repo = "/".join(parts[2:4])
            file_path = path.split("/contents/", 1)[1]
            content = self.files.get((repo, file_path))
            if content is None:
                return httpx.Response(404, json={"message": "Not Found"})
            if isinstance(content, int):
                return httpx.Response(content, json={"message": "no"})
            return httpx.Response(200, text=content)
        if path.endswith("/commits"):
            repo = "/".join(parts[2:4])
            wanted = url.params.get("path", "")
            found = self.commits.get((repo, wanted))
            if found is None:
                return httpx.Response(200, json=[])
            if isinstance(found, int):
                return httpx.Response(found, json={"message": "no"})
            return httpx.Response(200, json=[{"commit": {"committer": {"date": found}}}])
        if "/compare/" in path:
            self.compare_queries.append(url.query.decode())
            spec = path.split("/compare/", 1)[1]
            branch = spec.split("...", 1)[1]
            if branch not in self.ahead:
                return httpx.Response(500, json={"message": "boom"})
            return httpx.Response(200, json={"ahead_by": self.ahead[branch]})
        if "/actions/workflows/" in path:
            if self.runs_body is not None:
                return httpx.Response(200, json=self.runs_body)
            if isinstance(self.runs, int):
                return httpx.Response(self.runs, json={"message": "no"})
            return httpx.Response(200, json={"workflow_runs": self.runs})
        return httpx.Response(404, json={"message": "unexpected"})

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self)


async def run(fake: Fake, env: dict[str, str]) -> dict[str, Any]:
    return await load(Settings(env), transport=fake.transport)


# ── ranges ────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("eco", "version", "rng", "expected"),
    [
        ("NPM", "7.30.1", ">= 7.0.0, < 7.30.1", False),
        ("NPM", "7.30.0", ">= 7.0.0, < 7.30.1", True),
        ("NPM", "5.0.0-beta.1", ">= 5.0.0-beta.0, < 7.1.0", True),
        ("NPM", "5.0.0-alpha.9", ">= 5.0.0-beta.0, < 7.1.0", False),
        ("NPM", "7.1.0-rc.1", "< 7.1.0", True),
        ("NPM", "1.2.3", "= 1.2.3", True),
        ("NPM", "2.0.1", "<= 2.0", False),
        ("NPM", "2.0.0", "<= 2.0", True),
        ("NPM", "1.2.10", "> 1.2.9", True),
        ("PIP", "2026.10.19", "< 2026.10.20", True),
        ("PIP", "2026.10.20", "< 2026.10.20", False),
        ("PIP", "2.0.0rc1", "< 2.0.0", True),
        ("NPM", "not-a-version", "< 1.0.0", None),
        ("NPM", "1.0.0", "", None),
    ],
)
def test_in_range(eco: str, version: str, rng: str, expected: bool | None) -> None:
    assert in_range(eco, version, rng) is expected


# ── buckets ───────────────────────────────────────────────────────────────────


async def test_npm_waiting_when_installed_is_patched() -> None:
    fake = Fake()
    fake.repos["contoso/web"] = repo_data(alerts=[alert(216)])
    fake.files[("contoso/web", "worker/package-lock.json")] = npm_lock(undici="7.30.1")
    out = await run(fake, {"AE_GIT_REPOS": "contoso/web", "AE_GITHUB_TOKEN": TOKEN})
    a = out["repos"][0]["alerts"][0]
    assert a["bucket"] == "waiting" and a["installed"] == ["7.30.1"]
    assert a["url"] == "https://github.com/contoso/web/security/dependabot/216"
    assert out["totals"]["alerts_waiting"] == 1 and out["totals"]["alerts_fix"] == 0


async def test_npm_two_copies_one_vulnerable_is_fix() -> None:
    fake = Fake()
    fake.repos["contoso/web"] = repo_data(alerts=[alert(1)])
    fake.files[("contoso/web", "worker/package-lock.json")] = npm_lock(undici=["7.30.1", "7.10.0"])
    out = await run(fake, {"AE_GIT_REPOS": "contoso/web", "AE_GITHUB_TOKEN": TOKEN})
    a = out["repos"][0]["alerts"][0]
    assert a["bucket"] == "fix" and a["installed"] == ["7.10.0", "7.30.1"]


async def test_npm_scoped_package_and_missing_package() -> None:
    fake = Fake()
    fake.repos["contoso/web"] = repo_data(
        alerts=[alert(1, "@scope/pkg", rng="< 2.0.0"), alert(2, "ghost")]
    )
    fake.files[("contoso/web", "worker/package-lock.json")] = npm_lock(**{"@scope__pkg": "2.1.0"})
    out = await run(fake, {"AE_GIT_REPOS": "contoso/web", "AE_GITHUB_TOKEN": TOKEN})
    by = {a["number"]: a for a in out["repos"][0]["alerts"]}
    assert by[1]["bucket"] == "waiting"
    assert by[2]["bucket"] == "fix" and by[2]["installed"] == []


async def test_lockfile_is_read_once_per_path_and_cached_across_loads() -> None:
    fake = Fake()
    fake.repos["contoso/web"] = repo_data(alerts=[alert(1), alert(2, "other")])
    fake.files[("contoso/web", "worker/package-lock.json")] = npm_lock(
        undici="7.30.1", other="1.0.0"
    )
    env = {"AE_GIT_REPOS": "contoso/web", "AE_GITHUB_TOKEN": TOKEN}
    await run(fake, env)
    await run(fake, env)
    assert sum("/contents/" in c for c in fake.calls) == 1


async def test_pip_pin_and_normalised_name() -> None:
    fake = Fake()
    fake.repos["contoso/svc"] = repo_data(
        alerts=[alert(5, "Some_Pkg", eco="PIP", manifest="requirements.txt", rng="< 2026.10.20")]
    )
    fake.files[("contoso/svc", "requirements.txt")] = (
        "# pins\nsome-pkg[extra]==2026.10.19 ; python_version > '3.8'\nother==1.0\n"
    )
    out = await run(fake, {"AE_GIT_REPOS": "contoso/svc", "AE_GITHUB_TOKEN": TOKEN})
    a = out["repos"][0]["alerts"][0]
    assert a["bucket"] == "fix" and a["installed"] == ["2026.10.19"]


@pytest.mark.parametrize(
    ("eco", "manifest"),
    [
        ("NPM", "yarn.lock"),
        ("NPM", "pnpm-lock.yaml"),
        ("PIP", "Pipfile.lock"),
        ("RUBYGEMS", "Gemfile.lock"),
    ],
)
async def test_unknown_lockfile_is_fix(eco: str, manifest: str) -> None:
    fake = Fake()
    fake.repos["contoso/web"] = repo_data(alerts=[alert(1, eco=eco, manifest=manifest)])
    out = await run(fake, {"AE_GIT_REPOS": "contoso/web", "AE_GITHUB_TOKEN": TOKEN})
    a = out["repos"][0]["alerts"][0]
    assert a["bucket"] == "fix" and a["installed"] == []
    assert not any("/contents/" in c for c in fake.calls)


@pytest.mark.parametrize("content", ["{not json", json.dumps({"lockfileVersion": 1}), 500, None])
async def test_unreadable_lockfile_is_fix(content: str | int | None) -> None:
    fake = Fake()
    fake.repos["contoso/web"] = repo_data(alerts=[alert(1)])
    if content is not None:
        fake.files[("contoso/web", "worker/package-lock.json")] = content
    out = await run(fake, {"AE_GIT_REPOS": "contoso/web", "AE_GITHUB_TOKEN": TOKEN})
    assert out["repos"][0]["alerts"][0]["bucket"] == "fix"
    assert out["repos"][0]["error"] is None


async def test_oversize_lockfile_is_fix(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(git_center, "MAX_LOCKFILE_BYTES", 10)
    fake = Fake()
    fake.repos["contoso/web"] = repo_data(alerts=[alert(1)])
    fake.files[("contoso/web", "worker/package-lock.json")] = npm_lock(undici="7.30.1")
    out = await run(fake, {"AE_GIT_REPOS": "contoso/web", "AE_GITHUB_TOKEN": TOKEN})
    assert out["repos"][0]["alerts"][0]["bucket"] == "fix"


async def test_alerts_sorted_by_severity_then_package() -> None:
    fake = Fake()
    fake.repos["contoso/web"] = repo_data(
        alerts=[
            alert(1, "zeta", severity="LOW", manifest="yarn.lock"),
            alert(2, "beta", severity="CRITICAL", manifest="yarn.lock"),
            alert(3, "alpha", severity="HIGH", manifest="yarn.lock"),
            alert(4, "aardvark", severity="HIGH", manifest="yarn.lock"),
        ]
    )
    out = await run(fake, {"AE_GIT_REPOS": "contoso/web", "AE_GITHUB_TOKEN": TOKEN})
    assert [a["number"] for a in out["repos"][0]["alerts"]] == [2, 4, 3, 1]


# ── branches ──────────────────────────────────────────────────────────────────


async def test_branch_filtering_and_ahead() -> None:
    fake = Fake()
    fake.repos["contoso/web"] = repo_data(
        refs=[
            ref("main"),
            ref("dev"),
            ref("merged-already", prs=1),
            ref("closed-unmerged", prs=0, date="2026-07-01T00:00:00Z"),
            ref("feat/x", date="2026-08-01T00:00:00Z"),
            ref("nocompare", date="2026-09-01T00:00:00Z"),
        ]
    )
    fake.ahead = {"feat/x": 61, "closed-unmerged": 2}
    fake.files[("contoso/automation", "sweep.json")] = json.dumps(
        {"repos": ["contoso/web"], "catchUpBranches": ["dev"]}
    )
    fake.runs = []
    env = {"AE_GIT_SWEEP_CONFIG": "contoso/automation:sweep.json", "AE_GITHUB_TOKEN": TOKEN}
    out = await run(fake, env)
    branches = out["repos"][0]["branches"]
    assert [b["name"] for b in branches] == ["closed-unmerged", "feat/x", "nocompare"]
    assert [b["ahead"] for b in branches] == [2, 61, None]
    assert branches[1]["url"] == "https://github.com/contoso/web/tree/feat/x"
    assert out["totals"]["branches"] == 3


async def test_long_branch_name_is_cut_only_for_display() -> None:
    long = "feature/" + "b" * 142
    fake = Fake()
    fake.repos["contoso/web"] = repo_data(refs=[ref(long)])
    fake.ahead = {long: 7}
    out = await run(fake, {"AE_GIT_REPOS": "contoso/web", "AE_GITHUB_TOKEN": TOKEN})
    branch = out["repos"][0]["branches"][0]
    assert len(branch["name"]) == 120 and branch["name"].endswith("…")
    assert branch["url"] == f"https://github.com/contoso/web/tree/{long}"
    assert branch["ahead"] == 7
    assert "_full_name" not in branch


async def test_null_merge_state_becomes_unknown() -> None:
    fake = Fake()
    node = pr(1)
    node["mergeStateStatus"] = None
    fake.repos["contoso/web"] = repo_data(prs=[node])
    out = await run(fake, {"AE_GIT_REPOS": "contoso/web", "AE_GITHUB_TOKEN": TOKEN})
    assert out["repos"][0]["prs"][0]["merge_state"] == "UNKNOWN"


async def test_transient_lockfile_error_is_not_cached() -> None:
    fake = Fake()
    fake.repos["contoso/web"] = repo_data(alerts=[alert(1)])
    env = {"AE_GIT_REPOS": "contoso/web", "AE_GITHUB_TOKEN": TOKEN}
    fake.files[("contoso/web", "worker/package-lock.json")] = 403
    first = await run(fake, env)
    assert first["repos"][0]["alerts"][0]["bucket"] == "fix"
    fake.files[("contoso/web", "worker/package-lock.json")] = npm_lock(undici="7.30.1")
    second = await run(fake, env)
    assert second["repos"][0]["alerts"][0]["bucket"] == "waiting"
    assert sum("/contents/" in c for c in fake.calls) == 2


async def test_not_found_lockfile_is_cached() -> None:
    fake = Fake()
    fake.repos["contoso/web"] = repo_data(alerts=[alert(1)])
    env = {"AE_GIT_REPOS": "contoso/web", "AE_GITHUB_TOKEN": TOKEN}
    await run(fake, env)
    await run(fake, env)
    assert sum("/contents/" in c for c in fake.calls) == 1


# ── repos, accounts, tokens ───────────────────────────────────────────────────


async def test_view_only_is_shown_but_not_counted() -> None:
    fake = Fake()
    fake.repos["contoso/web"] = repo_data(prs=[pr(1)])
    fake.repos["northwind/lib"] = repo_data(prs=[pr(2), pr(3)], refs=[ref("old")])
    fake.ahead = {"old": 1}
    env = {
        "AE_GIT_REPOS": "contoso/web",
        "AE_GIT_VIEW_ONLY": "northwind/lib",
        "AE_GITHUB_TOKEN": TOKEN,
    }
    out = await run(fake, env)
    assert [r["repo"] for r in out["repos"]] == ["contoso/web", "northwind/lib"]
    assert out["repos"][1]["view_only"] is True
    assert out["totals"] == {
        "repos": 1,
        "prs": 1,
        "prs_ready": 1,
        "alerts_fix": 0,
        "alerts_waiting": 0,
        "branches": 0,
        "branches_kept": 0,
        "repo_errors": 0,
    }


async def test_dedupe_case_insensitive_and_archived_skipped() -> None:
    fake = Fake()
    fake.repos["contoso/web"] = repo_data()
    fake.repos["fabrikam/api"] = repo_data(archived=True)
    out = await run(fake, NOW_ENV)
    assert [r["repo"] for r in out["repos"]] == ["contoso/web"]
    assert sum(c.startswith("POST") for c in fake.calls) == 2


async def test_missing_token_means_not_connected_and_no_calls() -> None:
    fake = Fake()
    fake.repos["contoso/web"] = repo_data(prs=[pr(1)])
    env = {"AE_GIT_REPOS": "contoso/web,fabrikam-labs/api", "AE_GIT_TOKEN_CONTOSO": TOKEN}
    out = await run(fake, env)
    accounts = {a["owner"]: a for a in out["accounts"]}
    assert accounts["contoso"]["connected"] is True and accounts["contoso"]["repos"] == 1
    fab = accounts["fabrikam-labs"]
    assert fab["connected"] is False and "AE_GIT_TOKEN_FABRIKAM_LABS" in fab["note"]
    bad = next(r for r in out["repos"] if r["repo"] == "fabrikam-labs/api")
    assert bad["error"] == fab["note"] and bad["prs"] == []
    assert all("fabrikam" not in c for c in fake.calls)
    assert out["totals"]["repo_errors"] == 1


async def test_owner_token_wins_over_fallback() -> None:
    fake = Fake()
    fake.repos["contoso/web"] = repo_data()
    await run(
        fake,
        {"AE_GIT_REPOS": "contoso/web", "AE_GIT_TOKEN_CONTOSO": "own", "AE_GITHUB_TOKEN": "fb"},
    )
    assert set(fake.auth) == {"Bearer own"}


async def test_one_repo_failing_leaves_the_others() -> None:
    fake = Fake()
    fake.repos["contoso/web"] = repo_data(prs=[pr(1)])
    fake.repos["fabrikam/api"] = 401
    out = await run(fake, NOW_ENV)
    web = next(r for r in out["repos"] if r["repo"] == "contoso/web")
    api = next(r for r in out["repos"] if r["repo"] == "fabrikam/api")
    assert len(web["prs"]) == 1 and web["error"] is None
    assert "401" in api["error"] and "AE_GIT_TOKEN_FABRIKAM" in api["error"]
    assert out["totals"]["repo_errors"] == 1
    assert TOKEN not in json.dumps(git_center.wire(out))


async def test_alert_permission_error_names_the_fix() -> None:
    fake = Fake()
    fake.repos["contoso/web"] = repo_data()
    fake.graphql_errors["contoso/web"] = [
        {"type": "FORBIDDEN", "path": ["repository", "vulnerabilityAlerts"], "message": "x"}
    ]
    out = await run(fake, {"AE_GIT_REPOS": "contoso/web", "AE_GITHUB_TOKEN": TOKEN})
    assert "Dependabot alerts" in out["repos"][0]["error"]


async def test_repo_sort_order() -> None:
    fake = Fake()
    fake.repos["contoso/quiet"] = repo_data()
    fake.repos["contoso/busy"] = repo_data(prs=[pr(1), pr(2)])
    fake.repos["contoso/mid"] = repo_data(prs=[pr(3)])
    fake.repos["contoso/viewonly"] = repo_data(prs=[pr(4)])
    env = {
        "AE_GIT_REPOS": "contoso/quiet,contoso/busy,contoso/mid",
        "AE_GIT_VIEW_ONLY": "contoso/viewonly",
        "AE_GITHUB_TOKEN": TOKEN,
    }
    out = await run(fake, env)
    assert [r["repo"] for r in out["repos"]] == [
        "contoso/busy",
        "contoso/mid",
        "contoso/quiet",
        "contoso/viewonly",
    ]


# ── sweep and configuration ───────────────────────────────────────────────────


async def test_sweep_runs_and_config_repos() -> None:
    fake = Fake()
    fake.repos["contoso/web"] = repo_data()
    fake.files[("contoso/automation", "cfg/sweep.json")] = json.dumps({"repos": ["contoso/web"]})
    fake.runs = [
        {
            "id": 9,
            "html_url": "https://github.com/contoso/automation/actions/runs/9",
            "created_at": "2026-10-24T20:54:00Z",
            "status": "completed",
            "conclusion": "success",
        }
    ]
    env = {"AE_GIT_SWEEP_CONFIG": "contoso/automation:cfg/sweep.json", "AE_GITHUB_TOKEN": TOKEN}
    out = await run(fake, env)
    assert out["configured"] is True and out["error"] is None
    assert out["sweep"]["url"].endswith(
        "/contoso/automation/actions/workflows/dependency-sweep.yml"
    )
    assert out["sweep"]["runs"][0]["conclusion"] == "success" and out["sweep"]["error"] is None
    assert [r["repo"] for r in out["repos"]] == ["contoso/web"]


async def test_sweep_runs_unreadable_sets_error() -> None:
    fake = Fake()
    fake.files[("contoso/automation", "s.json")] = json.dumps({"repos": []})
    fake.runs = 403
    env = {"AE_GIT_SWEEP_CONFIG": "contoso/automation:s.json", "AE_GITHUB_TOKEN": TOKEN}
    out = await run(fake, env)
    assert out["sweep"]["runs"] == [] and "403" in out["sweep"]["error"]


async def test_bad_sweep_config_still_loads_listed_repos() -> None:
    fake = Fake()
    fake.repos["contoso/web"] = repo_data()
    env = {
        "AE_GIT_SWEEP_CONFIG": "contoso/automation:missing.json",
        "AE_GIT_REPOS": "contoso/web",
        "AE_GITHUB_TOKEN": TOKEN,
    }
    out = await run(fake, env)
    assert "404" in out["error"] and len(out["repos"]) == 1


async def test_not_configured_gives_setup_sentence() -> None:
    out = await load(Settings({}))
    assert out["configured"] is False and out["sweep"] is None
    assert out["accounts"] == [] and out["repos"] == []
    assert "AE_GIT_SWEEP_CONFIG" in out["setup"] and "AE_GIT_REPOS" in out["setup"]


def test_enterprise_graphql_url() -> None:
    assert Settings({}).graphql_url == "https://api.github.com/graphql"
    ghe = Settings({"AE_GITHUB_API_URL": "https://git.contoso.test/api/v3/"})
    assert ghe.graphql_url == "https://git.contoso.test/api/graphql"


# ── size ──────────────────────────────────────────────────────────────────────


async def test_worst_case_stays_under_the_bridge_limit() -> None:
    fake = Fake()
    names = [f"contoso/service-{i:02d}" for i in range(40)]
    long = "T" * 400
    for n, name in enumerate(names):
        fake.repos[name] = repo_data(
            prs=[pr(i + 1, long) for i in range(10)],
            refs=[
                ref(f"feature/{'b' * 60}-{i}", date=f"2026-0{1 + i % 9}-01T00:00:00Z")
                for i in range(20)
            ],
            alerts=[
                alert(
                    i + 1,
                    "p" * 100 + str(i),
                    manifest="m/" * 80 + "yarn.lock",
                    rng=">= 1.0.0, < 2.0.0 " * 20,
                )
                for i in range(30)
            ],
        )
    fake.ahead = {f"feature/{'b' * 60}-{i}": i for i in range(20)}
    full = await run(fake, {"AE_GIT_REPOS": ",".join(names), "AE_GITHUB_TOKEN": TOKEN})
    assert "list_cut" not in full  # the held answer is whole
    out = git_center.page(full)
    text = json.dumps(git_center.wire(out), ensure_ascii=False, separators=(",", ":"))
    assert len(text) < 30_000
    assert out["totals"]["prs"] == 400 and out["totals"]["alerts_fix"] == 1200
    assert out["list_cut"] is True
    assert all(len(p["title"]) <= 120 for r in out["repos"] for p in r["prs"])
    # Default JSONResponse (spaced separators) is still under the 32,768 limit.
    assert len(json.dumps(git_center.wire(out), ensure_ascii=False)) < 32_768


async def _big_answer(branches: int = 60) -> dict[str, Any]:
    fake = Fake()
    fake.repos["contoso/web"] = repo_data(
        prs=[pr(i + 1, "T" * 300) for i in range(60)],
        refs=[
            ref(f"feature/{'b' * 60}-{i}", date=f"2026-0{1 + i % 9}-01T00:00:00Z")
            for i in range(branches)
        ],
        alerts=[
            alert(i + 1, "p" * 80 + str(i), manifest="m/" * 60 + "yarn.lock") for i in range(130)
        ],
    )
    return await run(fake, {"AE_GIT_REPOS": "contoso/web", "AE_GITHUB_TOKEN": TOKEN})


async def test_the_held_answer_is_whole_and_the_needs_list_reads_all_of_it() -> None:
    full = await _big_answer()
    assert "list_cut" not in full
    repo = full["repos"][0]
    assert (len(repo["prs"]), len(repo["alerts"]), len(repo["branches"])) == (60, 130, 60)
    items = git_center.needs(full, 1_800_000_000.0)
    fix = next(i for i in items if i["kind"] == "alerts-fix")
    assert fix["title"].startswith("130 alerts to fix")
    assert any(i["kind"] == "branch" for i in items)
    assert all(s["ok"] for s in git_center.scope_ids(full))
    assert not any("_full_" in k for a in repo["alerts"] for k in a)


async def test_page_cuts_a_copy_and_leaves_the_held_answer_alone() -> None:
    full = await _big_answer()
    before = json.dumps(full, sort_keys=True)
    paged = git_center.page(full)
    assert json.dumps(full, sort_keys=True) == before
    assert paged["list_cut"] is True and paged["totals"] == full["totals"]
    assert git_center._size(paged) <= git_center.SIZE_BUDGET
    assert len(paged["repos"][0]["alerts"]) < 130
    assert "_facts" not in paged["repos"][0]
    assert git_center.page(full) is paged  # memoized on the same read
    # A last-good copy (new dict, same repos) hits the memo; a new read does not.
    assert git_center.page({**full, "error": None}) is paged
    other = await _big_answer()
    assert git_center.page(other) is not paged
    # A paged answer, if one ever reached the Needs you list, stays unclean.
    repo_scopes = [s["ok"] for s in git_center.scope_ids(paged) if s["id"].startswith("git:repo:")]
    assert repo_scopes == [False]


async def test_page_matches_the_old_cut_for_a_plain_load() -> None:
    fake = Fake()
    fake.repos["contoso/web"] = repo_data(prs=[pr(i + 1) for i in range(60)])
    full = await run(fake, {"AE_GIT_REPOS": "contoso/web", "AE_GITHUB_TOKEN": TOKEN})
    paged = git_center.page(full)
    assert paged["list_cut"] is True
    assert [p["number"] for p in paged["repos"][0]["prs"]] == [
        p["number"] for p in full["repos"][0]["prs"]
    ][: git_center.MAX_PRS]
    assert paged["totals"]["prs"] == 60
    small = git_center.page(await run(Fake(), {"AE_GIT_REPOS": "", "AE_GITHUB_TOKEN": TOKEN}))
    assert "list_cut" not in small


async def test_branch_comparisons_are_bounded_and_the_repo_goes_unclean() -> None:
    fake = Fake()
    count = git_center.MAX_FILL_AHEAD + 20
    fake.repos["contoso/web"] = repo_data(
        refs=[
            ref(f"old/{i:03d}", date=f"2026-01-01T00:{i // 60:02d}:{i % 60:02d}Z")
            for i in range(count)
        ]
    )
    out = await run(fake, {"AE_GIT_REPOS": "contoso/web", "AE_GITHUB_TOKEN": TOKEN})
    assert sum("/compare/" in c for c in fake.calls) == git_center.MAX_FILL_AHEAD
    assert len(out["repos"][0]["branches"]) == count
    repo_scopes = [s["ok"] for s in git_center.scope_ids(out) if s["id"].startswith("git:repo:")]
    assert repo_scopes == [False]


def _old_refs(prefix: str, n: int, date: str = "2026-01-01T00:00:00Z") -> list[dict[str, Any]]:
    return [ref(f"{prefix}/{i:03d}", date=date) for i in range(n)]


async def test_the_compare_budget_is_spent_across_repos_and_the_tail_goes_unclean() -> None:
    fake = Fake()
    for name in ("contoso/a", "contoso/b", "contoso/c"):
        fake.repos[name] = repo_data(refs=_old_refs("old", 60))
    env = {"AE_GIT_REPOS": "contoso/a,contoso/b,contoso/c", "AE_GITHUB_TOKEN": TOKEN}
    out = await run(fake, env)
    per_repo = {
        r: sum(f"/repos/{r}/compare/" in c for c in fake.calls)
        for r in ("contoso/a", "contoso/b", "contoso/c")
    }
    assert sum(per_repo.values()) == git_center.MAX_FILL_AHEAD
    assert sorted(per_repo.values()) == [0, 40, 60]
    marked = {r["repo"]: bool(r["_facts"].get("truncated")) for r in out["repos"]}
    assert sum(marked.values()) == 2  # the repo that got 40 and the one that got 0
    assert all(marked[name] for name in per_repo if per_repo[name] < 60)


async def test_a_counted_repo_gets_its_compares_before_a_view_only_repo_gets_any() -> None:
    """Rows sort view-only last and the budget loop walks that order (2026-11-04, Part B1)."""
    fake = Fake()
    fake.repos["contoso/a"] = repo_data(refs=_old_refs("old", 70))
    fake.repos["northwind/lib"] = repo_data(refs=_old_refs("view", 60))
    env = {
        "AE_GIT_REPOS": "contoso/a",
        "AE_GIT_VIEW_ONLY": "northwind/lib",
        "AE_GITHUB_TOKEN": TOKEN,
    }
    await run(fake, env)
    counted = sum("/repos/contoso/a/compare/" in c for c in fake.calls)
    viewing = sum("/repos/northwind/lib/compare/" in c for c in fake.calls)
    assert (counted, viewing) == (70, git_center.MAX_FILL_AHEAD - 70)
    # A counted repo that wants more than the whole budget leaves a view-only repo none.
    fake = Fake()
    fake.repos["contoso/a"] = repo_data(refs=_old_refs("old", git_center.MAX_FILL_AHEAD + 5))
    fake.repos["northwind/lib"] = repo_data(refs=_old_refs("view", 60))
    await run(fake, env)
    assert sum("/repos/contoso/a/compare/" in c for c in fake.calls) == git_center.MAX_FILL_AHEAD
    assert not any("/repos/northwind/lib/compare/" in c for c in fake.calls)


async def test_a_failed_compare_changes_only_the_branch_title_not_its_id_or_fingerprint() -> None:
    """``ahead`` is ``None`` after a failed compare; the item keeps its id and state (Part B2)."""
    env = {"AE_GIT_REPOS": "contoso/web", "AE_GITHUB_TOKEN": TOKEN}
    when = datetime(2026, 11, 3, tzinfo=UTC)
    answers = []
    for ahead in ({}, {"old/000": 4}):
        fake = Fake()
        fake.repos["contoso/web"] = repo_data(refs=_old_refs("old", 1))
        fake.ahead = ahead
        out = await load(Settings(env), transport=fake.transport, now=when)
        answers.append(out)
    failed, counted = (
        next(r for r in git_center.needs(out, when.timestamp()) if r["kind"] == "branch")
        for out in answers
    )
    assert answers[0]["repos"][0]["branches"][0]["ahead"] is None
    assert answers[1]["repos"][0]["branches"][0]["ahead"] == 4
    assert failed["id"] == counted["id"] == "git:branch:contoso/web:old/000"
    assert failed["state"] == counted["state"]
    assert set(failed["state"]) == {"repo", "branch", "sha"}
    assert failed["title"] == "old/000, no PR"
    assert counted["title"] == "old/000 has 4 commits and no PR"


async def test_young_and_kept_branches_cost_no_compare() -> None:
    fake = Fake()
    young = _old_refs("young", 5, date="2026-11-03T00:00:00Z")
    fake.repos["contoso/web"] = repo_data(refs=[*young, *_old_refs("keep", 5)])
    env = {
        "AE_GIT_REPOS": "contoso/web",
        "AE_GITHUB_TOKEN": TOKEN,
        "AE_GIT_KEEP_BRANCHES": ",".join(f"contoso/web:keep/{i:03d}" for i in range(5)),
    }
    out = await load(
        Settings(env), transport=fake.transport, now=datetime(2026, 11, 4, tzinfo=UTC)
    )
    assert not any("/compare/" in c for c in fake.calls)
    assert not out["repos"][0]["_facts"].get("truncated")
    # With the keep list gone, only the old ones are compared; the young ones still are not.
    fake.calls.clear()
    git_center.reset_cache()
    env.pop("AE_GIT_KEEP_BRANCHES")
    await load(Settings(env), transport=fake.transport, now=datetime(2026, 11, 4, tzinfo=UTC))
    assert sum("/compare/" in c for c in fake.calls) == 5


def test_size_counts_utf16_units() -> None:
    assert git_center._size({"t": "\U0001f600"}) == len('{"t":"xx"}')
    assert git_center._size({"t": "abc"}) == len('{"t":"abc"}')


async def test_emoji_titles_are_fitted_by_utf16_units() -> None:
    fake = Fake()
    names = [f"contoso/service-{i:02d}" for i in range(20)]
    for name in names:
        fake.repos[name] = repo_data(
            prs=[pr(i + 1, "\U0001f600" * 120) for i in range(10)],
            alerts=[
                alert(i + 1, "\U0001f600" * 100 + str(i), manifest="yarn.lock") for i in range(10)
            ],
        )
    out = git_center.page(
        await run(fake, {"AE_GIT_REPOS": ",".join(names), "AE_GITHUB_TOKEN": TOKEN})
    )
    text = json.dumps(git_center.wire(out), ensure_ascii=False, separators=(",", ":"))
    assert len(text.encode("utf-16-le")) // 2 < 30_000
    assert out["list_cut"] is True


# ── route ─────────────────────────────────────────────────────────────────────


async def test_git_route_is_admin_only(client: httpx.AsyncClient, ae_admins: set[str]) -> None:
    refused = await client.get(
        "/v1/ae/git", headers={"X-Forwarded-Email": "nobody@contoso.example"}
    )
    assert refused.status_code == 403, refused.text
    assert refused.json()["error"]["code"] == "admin_only"
    allowed = await client.get("/v1/ae/git", headers={"X-Forwarded-Email": "owner@example.com"})
    assert allowed.status_code == 200, allowed.text
    assert allowed.json()["configured"] is False


async def test_git_route_serves_cached_and_fresh(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AE_GIT_REPOS", "contoso/web")
    monkeypatch.setenv("AE_GITHUB_TOKEN", TOKEN)
    fake = Fake()
    fake.repos["contoso/web"] = repo_data(prs=[pr(1)])
    real: Callable[..., Any] = git_center.load

    async def with_fake(settings: Settings | None = None, **kw: Any) -> dict[str, Any]:
        return await real(settings, transport=fake.transport, **kw)

    monkeypatch.setattr(git_center, "load", with_fake)
    first = (await client.get("/v1/ae/git")).json()
    assert first["totals"]["prs"] == 1
    fake.repos["contoso/web"] = repo_data(prs=[pr(1), pr(2)])
    assert (await client.get("/v1/ae/git")).json()["totals"]["prs"] == 1
    assert (await client.get("/v1/ae/git?fresh=true")).json()["totals"]["prs"] == 2


# ── the cached read keeps the last good answer ────────────────────────────────


def _wire(monkeypatch: pytest.MonkeyPatch, fake: Fake) -> None:
    monkeypatch.setenv("AE_GIT_REPOS", "contoso/web, fabrikam/api")
    monkeypatch.setenv("AE_GITHUB_TOKEN", TOKEN)
    real: Callable[..., Any] = git_center.load

    async def with_fake(settings: Settings | None = None, **kw: Any) -> dict[str, Any]:
        return await real(settings, transport=fake.transport, **kw)

    monkeypatch.setattr(git_center, "load", with_fake)


async def test_all_repos_failing_keeps_the_last_good_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = Fake()
    fake.repos["contoso/web"] = repo_data(prs=[pr(1)])
    fake.repos["fabrikam/api"] = repo_data()
    _wire(monkeypatch, fake)
    good = await git_center.overview()
    assert good["error"] is None and len(good["repos"]) == 2
    fake.repos["contoso/web"] = 500
    fake.repos["fabrikam/api"] = 500
    again = await git_center.overview(fresh=True)
    assert [r["repo"] for r in again["repos"]] == ["contoso/web", "fabrikam/api"]
    assert again["totals"]["prs"] == 1
    assert again["error"].startswith("Showing the last good read from ")
    assert "failed for every repo" in again["error"]
    # The cached copy is not edited: a later good load clears the note.
    fake.repos["contoso/web"] = repo_data(prs=[pr(1), pr(2)])
    fake.repos["fabrikam/api"] = repo_data()
    healed = await git_center.overview(fresh=True)
    assert healed["error"] is None and healed["totals"]["prs"] == 2


async def test_partial_failure_still_returns_normally(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = Fake()
    fake.repos["contoso/web"] = repo_data(prs=[pr(1)])
    fake.repos["fabrikam/api"] = 500
    _wire(monkeypatch, fake)
    out = await git_center.overview()
    assert out["error"] is None
    assert out["totals"]["repo_errors"] == 1 and out["totals"]["prs"] == 1


async def test_first_load_all_failing_returns_the_repo_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = Fake()
    fake.repos["contoso/web"] = 500
    fake.repos["fabrikam/api"] = 500
    _wire(monkeypatch, fake)
    out = await git_center.overview()
    assert out["error"] and all(r["error"] for r in out["repos"])


# ── waiting_since, kept branches, failed-answer memory (round 2, 2026-10-24) ──


async def _waiting(created: str, committed: str | int | None) -> dict[str, Any]:
    fake = Fake()
    fake.repos["contoso/web"] = repo_data(alerts=[alert(1, created=created)])
    fake.files[("contoso/web", "worker/package-lock.json")] = npm_lock(undici="7.30.1")
    if committed is not None:
        fake.commits[("contoso/web", "worker/package-lock.json")] = committed
    out = await run(fake, {"AE_GIT_REPOS": "contoso/web", "AE_GITHUB_TOKEN": TOKEN})
    return out["repos"][0]["alerts"][0]


async def test_waiting_since_is_the_later_lockfile_commit() -> None:
    a = await _waiting("2026-10-20T10:00:00Z", "2026-10-22T08:00:00Z")
    assert a["created_at"] == "2026-10-20T10:00:00Z"
    assert a["waiting_since"] == "2026-10-22T08:00:00Z"


async def test_waiting_since_is_the_alert_date_when_newer_than_the_commit() -> None:
    a = await _waiting("2026-10-23T10:00:00Z", "2026-10-22T08:00:00Z")
    assert a["waiting_since"] == "2026-10-23T10:00:00Z"


@pytest.mark.parametrize("committed", [None, 500])
async def test_waiting_since_is_null_when_the_commit_is_unreadable(committed: int | None) -> None:
    a = await _waiting("2026-10-20T10:00:00Z", committed)
    assert a["bucket"] == "waiting" and a["waiting_since"] is None
    assert a["created_at"] == "2026-10-20T10:00:00Z"


async def test_fix_alerts_have_no_waiting_since_and_commits_read_once_per_lockfile() -> None:
    fake = Fake()
    fake.repos["contoso/web"] = repo_data(
        alerts=[alert(1), alert(2, "x"), alert(3, "undici", manifest="api/package.json")]
    )
    fake.files[("contoso/web", "worker/package-lock.json")] = npm_lock(undici="7.30.1", x="1.0.0")
    fake.files[("contoso/web", "api/package-lock.json")] = npm_lock(undici="7.0.0")
    fake.commits[("contoso/web", "worker/package-lock.json")] = "2026-10-22T08:00:00Z"
    out = await run(fake, {"AE_GIT_REPOS": "contoso/web", "AE_GITHUB_TOKEN": TOKEN})
    by = {a["number"]: a for a in out["repos"][0]["alerts"]}
    assert by[3]["bucket"] == "fix" and by[3]["waiting_since"] is None
    assert by[1]["waiting_since"] == by[2]["waiting_since"] == "2026-10-22T08:00:00Z"
    assert sum(1 for c in fake.calls if c.endswith("/commits")) == 1


async def test_kept_branches_match_repo_case_insensitively_and_branch_exactly() -> None:
    fake = Fake()
    fake.repos["contoso/web"] = repo_data(refs=[ref("keep-me"), ref("Keep-Me"), ref("other")])
    fake.repos["fabrikam/api"] = repo_data(refs=[ref("keep-me")])
    fake.ahead.update({"keep-me": 1, "Keep-Me": 1, "other": 1})
    env = {
        "AE_GIT_REPOS": "contoso/web, fabrikam/api",
        "AE_GIT_VIEW_ONLY": "fabrikam/api",
        "AE_GIT_KEEP_BRANCHES": "Contoso/WEB:keep-me, fabrikam/api:keep-me, bad entry",
        "AE_GITHUB_TOKEN": TOKEN,
    }
    out = await run(fake, env)
    web = next(r for r in out["repos"] if r["repo"] == "contoso/web")
    kept = {b["name"]: b["kept"] for b in web["branches"]}
    assert kept == {"keep-me": True, "Keep-Me": False, "other": False}
    # Kept branches stay in `branches`; `branches_kept` skips the view-only repo.
    assert out["totals"]["branches"] == 3 and out["totals"]["branches_kept"] == 1


async def test_failed_first_load_is_remembered_for_60_seconds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = Fake()
    fake.repos["contoso/web"] = 500
    fake.repos["fabrikam/api"] = 500
    _wire(monkeypatch, fake)
    clock = [1000.0]
    monkeypatch.setattr(git_center.time, "monotonic", lambda: clock[0])
    first = await git_center.overview()
    reads = len(fake.calls)
    assert reads and first["error"]
    clock[0] += 59
    assert await git_center.overview() == first
    assert len(fake.calls) == reads
    # A forced refresh still reads, and a read after 60 s starts over.
    await git_center.overview(fresh=True)
    assert len(fake.calls) > reads
    reads = len(fake.calls)
    clock[0] += 61
    await git_center.overview()
    assert len(fake.calls) > reads
    # A good read clears the memory.
    fake.repos["contoso/web"] = repo_data(prs=[pr(1)])
    fake.repos["fabrikam/api"] = repo_data()
    healed = await git_center.overview(fresh=True)
    assert healed["error"] is None and (await git_center.overview()) == healed


# ── final-review fixes (2026-10-24) ───────────────────────────────────────────

ONE = {"AE_GIT_REPOS": "contoso/web", "AE_GITHUB_TOKEN": TOKEN}


async def test_fix_alerts_survive_the_alert_cap_and_list_cut_is_set() -> None:
    fake = Fake()
    # The busier repo sorts first and holds only waiting alerts; the other holds one fix alert.
    fake.repos["contoso/web"] = repo_data(alerts=[alert(i, f"p{i}") for i in range(1, 121)])
    fake.repos["fabrikam/api"] = repo_data(alerts=[alert(1, "needsfix", manifest="yarn.lock")])
    for i in range(1, 121):
        fake.files[("contoso/web", "worker/package-lock.json")] = npm_lock(
            **{f"p{k}": "7.30.1" for k in range(1, 121)}
        )
    env = {"AE_GIT_REPOS": "contoso/web,fabrikam/api", "AE_GITHUB_TOKEN": TOKEN}
    out = git_center.page(await run(fake, env))
    shown = [a for r in out["repos"] for a in r["alerts"]]
    assert len(shown) <= 100
    assert out["totals"]["alerts_fix"] == 1 and any(a["bucket"] == "fix" for a in shown)
    assert out["list_cut"] is True


async def test_ready_prs_survive_the_pr_cap_before_drafts() -> None:
    fake = Fake()
    drafts = [{**pr(i), "isDraft": True} for i in range(1, 61)]
    fake.repos["contoso/web"] = repo_data(prs=drafts)
    fake.repos["fabrikam/api"] = repo_data(prs=[pr(500)])
    out = git_center.page(
        await run(fake, {"AE_GIT_REPOS": "contoso/web,fabrikam/api", "AE_GITHUB_TOKEN": TOKEN})
    )
    shown = [p for r in out["repos"] for p in r["prs"]]
    assert len(shown) == 50 and any(not p["draft"] for p in shown)
    assert out["list_cut"] is True


async def test_list_cut_absent_when_nothing_trimmed() -> None:
    fake = Fake()
    fake.repos["contoso/web"] = repo_data(prs=[pr(1)])
    out = await run(fake, ONE)
    assert not out.get("list_cut")


async def test_not_found_lockfile_is_retried_after_an_hour(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = Fake()
    fake.repos["contoso/web"] = repo_data(alerts=[alert(1)])
    clock = [1000.0]
    monkeypatch.setattr(git_center.time, "monotonic", lambda: clock[0])
    await run(fake, ONE)
    clock[0] += 3500
    await run(fake, ONE)
    assert sum("/contents/" in c for c in fake.calls) == 1
    clock[0] += 200
    await run(fake, ONE)
    assert sum("/contents/" in c for c in fake.calls) == 2


async def test_compare_asks_for_one_commit() -> None:
    fake = Fake()
    fake.repos["contoso/web"] = repo_data(refs=[ref("feat/x")])
    fake.ahead = {"feat/x": 3}
    await run(fake, ONE)
    assert fake.compare_queries == ["per_page=1"]


@pytest.mark.parametrize(
    "config", [{"repos": 5}, ["a"], {"repos": [3, None], "catchUpBranches": 4}]
)
async def test_sweep_config_of_the_wrong_shape_is_an_error_sentence(config: Any) -> None:
    fake = Fake()
    fake.files[("contoso/automation", "s.json")] = json.dumps(config)
    env = {"AE_GIT_SWEEP_CONFIG": "contoso/automation:s.json", "AE_GITHUB_TOKEN": TOKEN}
    out = await run(fake, env)
    assert out["configured"] is True
    if not (isinstance(config, dict) and config.get("repos") == [3, None]):
        assert "not readable JSON" in out["error"]


@pytest.mark.parametrize("body", [["x"], {"workflow_runs": 5}, {"workflow_runs": [1]}])
async def test_sweep_runs_of_the_wrong_shape_set_the_sweep_error(body: Any) -> None:
    fake = Fake()
    fake.files[("contoso/automation", "s.json")] = json.dumps({"repos": []})
    fake.runs_body = body
    env = {"AE_GIT_SWEEP_CONFIG": "contoso/automation:s.json", "AE_GITHUB_TOKEN": TOKEN}
    out = await run(fake, env)
    assert out["sweep"]["runs"] == [] and "cannot read" in out["sweep"]["error"]


async def test_rate_limited_graphql_reads_as_rate_limited() -> None:
    fake = Fake()
    fake.repos["contoso/web"] = repo_data()
    fake.graphql_null.add("contoso/web")
    fake.graphql_errors["contoso/web"] = [{"type": "RATE_LIMITED", "message": "API rate limit"}]
    out = await run(fake, ONE)
    error = out["repos"][0]["error"]
    assert "ate limited" in error and "404" not in error


async def test_archived_repos_are_not_counted_in_accounts() -> None:
    fake = Fake()
    fake.repos["contoso/web"] = repo_data()
    fake.repos["fabrikam/api"] = repo_data(archived=True)
    out = await run(fake, NOW_ENV)
    counts = {a["owner"]: a["repos"] for a in out["accounts"]}
    assert counts["contoso"] == 1 and counts["fabrikam"] == 0


# ── Needs you: the port of analyze() and the facts the query now reads (2026-11-03) ─────────

GOLDEN = Path(__file__).parents[2] / "ae_omni_extension/web/test/fixtures/git-parity.json"


def test_needs_gives_the_same_ids_and_tones_as_the_old_typescript_rules() -> None:
    """One golden file, two ports: ``gitParity.test.ts`` pins the old rules to it."""
    golden = json.loads(GOLDEN.read_text())
    assert golden["expected"].keys() == golden["scenarios"].keys()
    for name, scenario in golden["scenarios"].items():
        got = git_center.needs(scenario["overview"], golden["now"] / 1000)
        assert [{"id": n["id"], "tone": n["tone"]} for n in got] == golden["expected"][name], name


async def test_the_query_reads_head_commits_failing_check_names_and_branch_heads() -> None:
    fake = Fake()
    failing = pr(5)
    failing["commits"] = {
        "nodes": [
            {
                "commit": {
                    "oid": "abc123",
                    "statusCheckRollup": {
                        "state": "FAILURE",
                        "contexts": {
                            "totalCount": 4,
                            "nodes": [
                                {
                                    "__typename": "CheckRun",
                                    "name": "unit",
                                    "conclusion": "FAILURE",
                                },
                                {
                                    "__typename": "CheckRun",
                                    "name": "lint",
                                    "conclusion": "SUCCESS",
                                },
                                {
                                    "__typename": "StatusContext",
                                    "context": "ci/legacy",
                                    "state": "ERROR",
                                },
                                {
                                    "__typename": "CheckRun",
                                    "name": "e2e",
                                    "conclusion": "TIMED_OUT",
                                },
                            ],
                        },
                    },
                }
            }
        ]
    }
    many = pr(6)
    many["commits"] = {
        "nodes": [
            {
                "commit": {
                    "oid": "def456",
                    "statusCheckRollup": {
                        "state": "FAILURE",
                        "contexts": {
                            "totalCount": 25,
                            "nodes": [
                                {"__typename": "CheckRun", "name": "a", "conclusion": "FAILURE"}
                            ],
                        },
                    },
                }
            }
        ]
    }
    old_ref = ref("feature/x", date="2026-01-01T00:00:00Z")
    old_ref["target"]["oid"] = "headsha"
    fake.repos["contoso/web"] = repo_data(prs=[failing, many], refs=[old_ref])
    out = await run(fake, {"AE_GIT_REPOS": "contoso/web", "AE_GITHUB_TOKEN": TOKEN})
    facts = out["repos"][0]["_facts"]
    assert facts["prs"]["5"] == {"sha": "abc123", "failing": ["ci/legacy", "e2e", "unit"]}
    # More than the 20 checks the query reads: the names are unknowable, never a short list.
    assert facts["prs"]["6"] == {"sha": "def456", "failing": None}
    assert facts["branches"] == {"feature/x": "headsha"}
    assert "_facts" not in json.dumps(git_center.wire(out))
    assert "abc123" not in json.dumps(git_center.wire(out))


def _run(
    name: str, conclusion: str | None, at: str | None = None, workflow: str = "CI"
) -> dict[str, Any]:
    return {
        "__typename": "CheckRun",
        "name": name,
        "conclusion": conclusion,
        "status": "COMPLETED" if conclusion else "IN_PROGRESS",
        "completedAt": at,
        "checkSuite": {
            "app": {"slug": "github-actions"},
            "workflowRun": {"workflow": {"name": workflow}},
        },
    }


def _rollup_pr(number: int, state: str, runs: list[dict[str, Any]]) -> dict[str, Any]:
    node = pr(number)
    node["commits"] = {
        "nodes": [
            {
                "commit": {
                    "oid": f"sha{number}",
                    "statusCheckRollup": {
                        "state": state,
                        "contexts": {"totalCount": len(runs), "nodes": runs},
                    },
                }
            }
        ]
    }
    return node


async def test_a_cancelled_duplicate_of_a_passed_check_is_not_a_failing_pr() -> None:
    # 2026-11-06: one push started three leaks scans, the concurrency group cancelled two,
    # the third passed, and GitHub's rollup read FAILURE with every check green.
    t1, t2, t3 = "2026-11-06T04:10:18Z", "2026-11-06T04:10:26Z", "2026-11-06T04:10:48Z"
    fake = Fake()
    fake.repos["contoso/web"] = repo_data(
        prs=[
            _rollup_pr(
                1,
                "FAILURE",
                [
                    _run("leaks", "CANCELLED", t1),
                    _run("leaks", "SUCCESS", t3),
                    _run("leaks", "CANCELLED", t2),
                    _run("unit", "SKIPPED", t1),
                ],
            ),
            # A cancel with no passed run of its check still fails.
            _rollup_pr(2, "FAILURE", [_run("leaks", "CANCELLED", t1), _run("unit", None)]),
            # Superseded cancel with a run still going: pending, not green.
            _rollup_pr(
                3,
                "FAILURE",
                [_run("leaks", "CANCELLED", t1), _run("leaks", "SUCCESS", t2), _run("unit", None)],
            ),
            _rollup_pr(
                4, "FAILURE", [_run("leaks", "CANCELLED", t1), _run("unit", "FAILURE", t1)]
            ),
            # Same job name in another workflow never stands in for the cancelled one.
            _rollup_pr(
                5,
                "FAILURE",
                [_run("build", "CANCELLED", t1, "Release"), _run("build", "SUCCESS", t3, "CI")],
            ),
            # A person cancelled the newest run after an older one passed: still failing.
            _rollup_pr(
                6, "FAILURE", [_run("leaks", "SUCCESS", t1), _run("leaks", "CANCELLED", t3)]
            ),
            # A conclusion the code does not classify keeps GitHub's own state.
            _rollup_pr(
                7,
                "FAILURE",
                [
                    _run("leaks", "CANCELLED", t1),
                    _run("leaks", "SUCCESS", t2),
                    _run("old", "STALE", t1),
                ],
            ),
        ]
    )
    out = await run(fake, {"AE_GIT_REPOS": "contoso/web", "AE_GITHUB_TOKEN": TOKEN})
    repo = out["repos"][0]
    assert {n: f["failing"] for n, f in repo["_facts"]["prs"].items()} == {
        "1": [],
        "2": ["leaks"],
        "3": [],
        "4": ["leaks", "unit"],
        "5": ["build"],
        "6": ["leaks"],
        "7": [],
    }
    assert {p["number"]: p["checks"] for p in repo["prs"]} == {
        1: "SUCCESS",
        2: "FAILURE",
        3: "PENDING",
        4: "FAILURE",
        5: "FAILURE",
        6: "FAILURE",
        7: "FAILURE",
    }
    flagged = {
        i["state"]["number"]
        for i in git_center.needs(out, 1_800_000_000.0)
        if i["kind"] == "pr-failing"
    }
    assert flagged == {2, 4, 5, 6, 7}


def test_a_failure_with_no_checks_to_account_for_keeps_githubs_state() -> None:
    assert (
        git_center._checks_state({"state": "FAILURE", "contexts": {"nodes": []}}, []) == "FAILURE"
    )


def test_error_codes_are_short_and_carry_no_sentence() -> None:
    assert git_center.error_code("No token: set AE_GIT_TOKEN_X") == "no_token"
    assert (
        git_center.error_code("Rate limited by GitHub for a/b; try again later.") == "rate_limited"
    )
    assert git_center.error_code("GitHub did not answer for a/b (X); try again.") == "no_answer"
    assert git_center.error_code("something else") == "error"
