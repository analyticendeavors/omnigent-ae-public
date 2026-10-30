"""deploy/host-config-sync.sh against a fixture ae-claude-skills repo (2026-10-28).

The fixture is a throwaway git repo with a manifest, team files, a personal
skill and files that carry a personal marker. Anything that needs a container
runs against a throwaway `synctest-*` container started from the host image
the box already has; it never touches an omnigent-host-<user> container. The
tests that need Docker (or that image) skip when they are absent; CI's
runners have Docker but not the image, so there they run the scan tests only.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "deploy" / "host-config-sync.sh"
UNIT_DIR = ROOT / "deploy" / "systemd" / "user"

MANIFEST = """\
# fixture manifest
skills/alpha team any
skills/linuxy team linux
skills/winonly team windows
skills/secret personal any
"""

FIXTURES = Path(__file__).parent / "fixtures"

# The personal markers, built from parts so this file passes scripts/leak-scan.py.
HOME_PATH = "/home/" + "owner"
WIN_HOME = "C:\\Users\\" + "winuser"
WIN_PROJECT_DIR = "C-" + "-Users-" + "winuser"
MAIL_MARKER = "Own" + "er@example.com"

# The scrub file the script reads (AE_HOST_SCRUB_FILE): neutral stand-ins for the real list,
# every value built from parts so no marker is greppable in this file.
_BRAND = "skills/analytic-endeavors-brand-guidelines/references/rulebook.md"
_BRAND_COWORK = "cowork-plugins/analytic-endeavors-brand-guidelines/" + _BRAND
SCRUB_MARKERS = [
    "own" + "er@",
    "own" + "er.person@",
    HOME_PATH,
    WIN_HOME,
    WIN_PROJECT_DIR,
    "dev-own" + "er",
    "hc-client-secret",
    "youtube-refresh-token-hc",
    "tenant-" + "frag1",
    "1681" + "862",
    "example" + "corp",
    "example " + "corp",
    "example-" + "corp",
    "example_" + "corp",
    "hc-referral",
    "-" + "-account hc",
    "amount_at_300",
    ".claude/" + "memory",
    "one" + "drive",
    "C:/" + "GitHub",
]
SCRUB_ALLOWED = {_BRAND: "example " + "corp", _BRAND_COWORK: "example " + "corp"}


def scrub_text() -> str:
    lines = ["# fixture scrub list", "", *SCRUB_MARKERS]
    lines += [f"{rel}\t{value}" for rel, value in SCRUB_ALLOWED.items()]
    return "\n".join(lines) + "\n"


# The merged ae-claude-skills files (PR 245), copied verbatim into tests/deploy/fixtures/.
MERGED_MCP = json.loads((FIXTURES / "team-mcp.json").read_text())
MERGED_SETTINGS = json.loads((FIXTURES / "team-settings.json").read_text())
SERVERS = ["esignatures-oauth", "freshbooks", "powerbi-modeling-sp"]
FB = "freshbooks-mcp/servers"


def mcp_json(setups: dict[str, list[str]] | None = None) -> str:
    """The merged team-mcp.json with cheap setup commands (no npm in tests)."""
    mcp = json.loads(json.dumps(MERGED_MCP))
    cheap = setups or {
        "freshbooks": ["echo ran >> ../../setup-ran.log"],
        "powerbi-modeling-sp": ["echo ran2 >> ../../setup2-ran.log"],
    }
    for server in mcp["servers"]:
        if "setup" in server:
            server["setup"] = cheap[server["name"]]
    return json.dumps(mcp)


BASE = {
    "claude-config/skills-manifest.txt": MANIFEST,
    "claude-config/team-CLAUDE.md": "# Team rules\nUse the shared skills.\n",
    "claude-config/team-settings.json": (FIXTURES / "team-settings.json").read_text(),
    "claude-config/team-mcp.json": mcp_json(),
    "scripts/register-team-mcp.sh": (FIXTURES / "register-team-mcp.sh").read_text(),
    "claude-config/mcp/pbi-mcp.sh": "#!/usr/bin/env bash\necho pbi\n",
    f"{FB}/freshbooks-server.js": "console.log('fb');\n",
    f"{FB}/package.json": "{}\n",
    "skills/alpha/SKILL.md": "---\nname: alpha\n---\nAlpha.\n",
    "skills/alpha/helper.txt": "help\n",
    "skills/linuxy/SKILL.md": "---\nname: linuxy\n---\n",
    "skills/winonly/SKILL.md": "---\nname: winonly\n---\n",
    "skills/secret/SKILL.md": "---\nname: secret\n---\nmail owner@example.com\n",
    "claude-config/global-CLAUDE.md": "The owner's rules, " + HOME_PATH + "\n",
}


def git(repo: Path, *args: str, date: str | None = None) -> str:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.com",
        **({"GIT_AUTHOR_DATE": date, "GIT_COMMITTER_DATE": date} if date else {}),
    }
    out = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True, env=env
    )
    return out.stdout.strip()


def make_repo(tmp: Path, files: dict[str, str | bytes | None], age_hours: float = 0) -> Path:
    """A fixture clone whose origin/main holds `files` (None drops a BASE file)."""
    repo = tmp / "skills-clone"
    shutil.rmtree(repo, ignore_errors=True)
    repo.mkdir(parents=True)
    git(repo, "init", "-q", "-b", "main")
    for rel, text in {**BASE, **files}.items():
        if text is None:
            continue
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(text, bytes):
            p.write_bytes(text)
        else:
            p.write_text(text)
        if rel.endswith(".sh"):
            p.chmod(0o755)
    git(repo, "add", "-A")
    date = None
    if age_hours:
        date = f"{int(time.time() - age_hours * 3600)} +0000"
    git(repo, "commit", "-q", "-m", "fixture", date=date)
    git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    return repo


def run_sync(
    repo: Path,
    container: str = "synctest-none",
    *args: str,
    user: str = "member",
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    full = {
        **os.environ,
        "AE_SKILLS_CLONE": str(repo),
        "AE_SYNC_CONTAINER": container,
        **(env or {}),
    }
    return subprocess.run(
        [str(SCRIPT), user, *args], capture_output=True, text=True, check=False, env=full
    )


@pytest.fixture(autouse=True)
def scrub_file(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path_factory.mktemp("scrub") / "host-scrub.txt"
    path.write_text(scrub_text())
    monkeypatch.setenv("AE_HOST_SCRUB_FILE", str(path))
    return path


@pytest.fixture
def lock_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    d = tmp_path / "tmp"
    d.mkdir()
    monkeypatch.setenv("TMPDIR", str(d))
    return d


# ── host side: no container needed ──


def test_dry_run_lists_the_allowlist_and_sends_nothing(tmp_path: Path, lock_dir: Path) -> None:
    repo = make_repo(tmp_path, {})
    r = run_sync(repo, "synctest-none", "--dry-run")
    assert r.returncode == 0, r.stderr
    assert "skills: alpha linuxy" in r.stdout.lower() or "Skills: alpha linuxy" in r.stdout
    assert "skills/alpha/SKILL.md" in r.stdout
    assert "skills/linuxy/SKILL.md" in r.stdout
    assert "skills/winonly" not in r.stdout
    assert "skills/secret" not in r.stdout
    assert "global-CLAUDE.md" not in r.stdout
    assert "nothing was sent or changed" in r.stdout


def test_a_missing_manifest_aborts(tmp_path: Path, lock_dir: Path) -> None:
    repo = make_repo(tmp_path, {"claude-config/skills-manifest.txt": None})
    r = run_sync(repo, "synctest-none", "--dry-run")
    assert r.returncode != 0
    assert "skills-manifest.txt is missing" in r.stderr


@pytest.mark.parametrize(
    "manifest",
    [
        "skills/alpha team\n",
        "skills/alpha everyone any\n",
        "skills/alpha team mac\n",
        "# only a comment\n",
    ],
)
def test_an_unparsable_manifest_aborts(tmp_path: Path, lock_dir: Path, manifest: str) -> None:
    repo = make_repo(tmp_path, {"claude-config/skills-manifest.txt": manifest})
    r = run_sync(repo, "synctest-none", "--dry-run")
    assert r.returncode != 0
    assert "skills-manifest.txt" in r.stderr


def test_a_manifest_path_that_is_not_a_folder_aborts(tmp_path: Path, lock_dir: Path) -> None:
    repo = make_repo(tmp_path, {"claude-config/skills-manifest.txt": "skills/ghost team any\n"})
    r = run_sync(repo, "synctest-none", "--dry-run")
    assert r.returncode != 0
    assert "skills/ghost" in r.stderr


def test_team_claude_md_is_required(tmp_path: Path, lock_dir: Path) -> None:
    repo = make_repo(tmp_path, {"claude-config/team-CLAUDE.md": None})
    r = run_sync(repo, "synctest-none", "--dry-run")
    assert r.returncode != 0
    assert "team-CLAUDE.md is missing" in r.stderr


def test_optional_files_are_skipped_with_a_warning(tmp_path: Path, lock_dir: Path) -> None:
    repo = make_repo(
        tmp_path,
        {"claude-config/team-settings.json": None, "claude-config/team-mcp.json": None},
    )
    r = run_sync(repo, "synctest-none", "--dry-run")
    assert r.returncode == 0, r.stderr
    assert "team-settings.json is not at" in r.stderr


def test_a_marker_in_a_team_file_aborts_before_anything_is_sent(
    tmp_path: Path, lock_dir: Path
) -> None:
    repo = make_repo(tmp_path, {"skills/alpha/SKILL.md": "mail " + MAIL_MARKER + "\n"})
    r = run_sync(repo, "synctest-none", "--dry-run")
    assert r.returncode != 0
    assert "skills/alpha/SKILL.md" in r.stderr and "owner@" in r.stderr
    assert "nothing was sent" in r.stderr


@pytest.mark.parametrize(
    "marker",
    [
        HOME_PATH,
        WIN_HOME,
        WIN_PROJECT_DIR,
        "dev-owner",
        "hc-client-secret",
        "youtube-refresh-token-hc",
    ],
)
def test_every_personal_marker_aborts(tmp_path: Path, lock_dir: Path, marker: str) -> None:
    repo = make_repo(tmp_path, {"skills/linuxy/notes.md": f"see {marker} for it\n"})
    r = run_sync(repo, "synctest-none", "--dry-run")
    assert r.returncode != 0
    assert marker.lower().replace("\\", "/") in r.stderr.lower()


@pytest.mark.parametrize(
    "path",
    [
        "skills/alpha/.credentials.json",
        "skills/alpha/history.jsonl",
        "skills/alpha/settings.json",
        "skills/alpha/settings.local.json",
        "skills/alpha/global-CLAUDE.md",
        "skills/alpha/hb-get.sh",
        "skills/alpha/.env",
        "skills/alpha/.env.prod",
        "skills/alpha/node_modules/pkg/index.js",
        "skills/alpha/.config/gh/hosts.yml",
        "skills/alpha/.ssh/config",
        "skills/alpha/policy-limits.json",
        "skills/alpha/memory-backup/a.md",
    ],
)
def test_every_deny_path_aborts(tmp_path: Path, lock_dir: Path, path: str) -> None:
    repo = make_repo(tmp_path, {path: "harmless\n"})
    r = run_sync(repo, "synctest-none", "--dry-run")
    assert r.returncode != 0, path
    assert path in r.stderr
    assert "deny" in r.stderr


@pytest.mark.parametrize(
    "path",
    [
        "skills/alpha/state/x.md",
        "skills/alpha/src/ae/state/filters.tsx",
        "skills/alpha/sub/sessions/x.md",
        "skills/alpha/plans/x.md",
        "skills/alpha/projects/x.md",
        "skills/alpha/file-history/x.md",
        "skills/alpha/paste-cache/x.md",
        "skills/alpha/shell-snapshots/x.md",
        "skills/alpha/session-env/x.md",
    ],
)
def test_plain_english_folder_names_pass_when_nested(
    tmp_path: Path, lock_dir: Path, path: str
) -> None:
    repo = make_repo(tmp_path, {path: "ordinary code\n"})
    r = run_sync(repo, "synctest-none", "--dry-run")
    assert r.returncode == 0, r.stderr
    assert path in r.stdout


@pytest.mark.parametrize(
    "root",
    [
        "state",
        "sessions",
        "plans",
        "projects",
        "file-history",
        "paste-cache",
        "shell-snapshots",
        "session-env",
    ],
)
def test_plain_english_folder_names_abort_at_the_root_of_the_synced_tree(
    tmp_path: Path, lock_dir: Path, root: str
) -> None:
    mcp = json.loads(BASE["claude-config/team-mcp.json"])
    mcp["servers"][0]["sync_paths"].append(root)
    repo = make_repo(
        tmp_path,
        {f"{root}/x.md": "claude home state\n", "claude-config/team-mcp.json": json.dumps(mcp)},
    )
    r = run_sync(repo, "synctest-none", "--dry-run")
    assert r.returncode != 0
    assert f"{root}/x.md" in r.stderr
    assert f"deny root path '{root}/'" in r.stderr


def test_team_settings_with_a_refused_key_aborts(tmp_path: Path, lock_dir: Path) -> None:
    repo = make_repo(
        tmp_path,
        {"claude-config/team-settings.json": json.dumps({"env": {"A": "1"}, "hooks": {}})},
    )
    r = run_sync(repo, "synctest-none", "--dry-run")
    assert r.returncode != 0
    assert "hooks" in r.stderr


def test_a_symlink_in_the_archive_aborts(tmp_path: Path, lock_dir: Path) -> None:
    repo = make_repo(tmp_path, {})
    (repo / "skills/alpha/link").symlink_to("/etc/passwd")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "link")
    git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    r = run_sync(repo, "synctest-none", "--dry-run")
    assert r.returncode != 0
    assert "symlink" in r.stderr


def test_the_working_tree_is_never_read(tmp_path: Path, lock_dir: Path) -> None:
    repo = make_repo(tmp_path, {})
    (repo / "skills/alpha/SKILL.md").write_text("uncommitted owner@example.com\n")
    (repo / "skills/alpha/new-untracked.md").write_text("x\n")
    r = run_sync(repo, "synctest-none", "--dry-run")
    assert r.returncode == 0, r.stderr
    assert "new-untracked" not in r.stdout


def test_origin_main_is_required_and_a_local_main_is_never_used(
    tmp_path: Path, lock_dir: Path
) -> None:
    repo = make_repo(tmp_path, {})
    git(repo, "update-ref", "-d", "refs/remotes/origin/main")
    r = run_sync(repo, "synctest-none", "--dry-run")
    assert r.returncode != 0
    assert "refs/remotes/origin/main does not exist" in r.stderr


def test_an_old_origin_main_warns_but_syncs(tmp_path: Path, lock_dir: Path) -> None:
    repo = make_repo(tmp_path, {}, age_hours=8)
    r = run_sync(repo, "synctest-none", "--dry-run")
    assert r.returncode == 0, r.stderr
    assert "hours old" in r.stderr
    fresh = run_sync(make_repo(tmp_path, {}), "synctest-none", "--dry-run")
    assert "hours old" not in fresh.stderr


def test_a_container_that_is_not_running_is_a_skipped_sync(tmp_path: Path, lock_dir: Path) -> None:
    if shutil.which("docker") is None:
        pytest.skip("docker is not installed here")
    repo = make_repo(tmp_path, {})
    r = run_sync(repo, f"synctest-absent-{uuid.uuid4().hex[:8]}")
    assert r.returncode == 0, r.stderr
    assert "not running; sync skipped" in r.stdout


def test_container_names_are_limited(tmp_path: Path, lock_dir: Path) -> None:
    repo = make_repo(tmp_path, {})
    r = run_sync(repo, "somebody-else", "--dry-run")
    assert r.returncode != 0
    assert "omnigent-host-<name>" in r.stderr


def test_host_container_dispatches_sync_config(tmp_path: Path, lock_dir: Path) -> None:
    repo = make_repo(tmp_path, {})
    env = {**os.environ, "AE_SKILLS_CLONE": str(repo), "AE_SYNC_CONTAINER": "synctest-none"}
    r = subprocess.run(
        [str(ROOT / "deploy" / "host-container.sh"), "member", "sync-config", "--dry-run"],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    assert r.returncode == 0, r.stderr
    assert "dry run" in r.stdout


# ── the units ──


def test_sync_units_are_a_oneshot_and_a_thirty_minute_persistent_timer() -> None:
    svc = (UNIT_DIR / "ae-host-config-sync@.service").read_text()
    assert "Type=oneshot" in svc
    assert "host-container.sh %i sync-config" in svc
    assert "ExecStart=%h/" in svc
    for forbidden in ("restart", "stop", "down", "compose"):
        assert forbidden not in next(ln for ln in svc.splitlines() if ln.startswith("Exec"))
    tmr = (UNIT_DIR / "ae-host-config-sync@.timer").read_text()
    assert "OnCalendar=*:0/30" in tmr
    assert "RandomizedDelaySec=120" in tmr
    assert "Persistent=true" in tmr
    assert "WantedBy=timers.target" in tmr


# ── inside a throwaway container ──


def _image() -> str | None:
    if shutil.which("docker") is None:
        return None
    if os.environ.get("AE_SYNC_TEST_IMAGE"):
        return os.environ["AE_SYNC_TEST_IMAGE"]
    out = subprocess.run(
        ["docker", "image", "ls", "omnigent-ae-host", "--format", "{{.Repository}}:{{.Tag}}"],
        capture_output=True,
        text=True,
        check=False,
    )
    tags = out.stdout.split()
    return tags[0] if tags else None


@pytest.fixture(scope="session")
def host_image() -> Iterator[str]:
    """The host image, with jq: the Dockerfile installs it, an older image gets a throwaway layer."""
    image = _image()
    if image is None:
        pytest.skip("docker or an omnigent-ae-host image is not available here")
    has_jq = subprocess.run(
        ["docker", "run", "--rm", "--entrypoint", "sh", image, "-c", "command -v jq"],
        capture_output=True,
        check=False,
    )
    if has_jq.returncode == 0:
        yield image
        return
    tag = f"synctest-image:{os.getpid()}"
    subprocess.run(
        ["docker", "build", "-q", "-t", tag, "-"],
        input=f"FROM {image}\nUSER root\nRUN apt-get update -qq && apt-get install -y -qq --no-install-recommends jq\nUSER agent\n",
        text=True,
        check=True,
        capture_output=True,
    )
    try:
        yield tag
    finally:
        subprocess.run(["docker", "rmi", "-f", tag], capture_output=True, check=False)


# A stub `claude mcp get|list|add|remove` that keeps one folder per server under
# $HOME/stub (the same shape as ae-claude-skills scripts/test-register-team-mcp.sh).
# $HOME/stub/fail-add names a server whose first `add` fails (once), for the restore path.
STUB_CLAUDE = r"""#!/usr/bin/env bash
set -euo pipefail
S="$HOME/stub"
mkdir -p "$S"
echo "$*" >>"$S/calls.log"
[ "$1" = mcp ] || exit 2
verb=$2; shift 2
case "$verb" in
  list)
    for d in "$S"/srv-*; do [ -d "$d" ] && echo "${d##*/srv-}"; done || true
    ;;
  get)
    d="$S/srv-$1"
    [ -d "$d" ] || { echo "No MCP server named \"$1\"." >&2; exit 1; }
    echo "$1:"
    echo "  Scope: User config (available in all your projects)"
    echo "  Type: $(cat "$d/type")"
    if [ "$(cat "$d/type")" = stdio ]; then
      echo "  Command: $(cat "$d/command")"
      echo "  Args: $(cat "$d/args")"
      echo "  Environment:"
      sed 's/^/    /' "$d/env"
    else
      echo "  URL: $(cat "$d/url")"
    fi
    ;;
  remove)
    if [ "$1" = --scope ]; then shift 2; fi
    echo "remove $1" >>"$S/mutations.log"
    rm -rf "$S/srv-$1"
    ;;
  add)
    shift 2
    transport=$2; shift 2
    name=$1; shift
    if [ -f "$S/fail-add" ] && [ "$(cat "$S/fail-add")" = "$name" ] && [ ! -f "$S/failed-$name" ]; then
      touch "$S/failed-$name"; exit 1
    fi
    d="$S/srv-$name"
    rm -rf "$d"; mkdir -p "$d"
    echo "$transport" >"$d/type"
    : >"$d/env"
    if [ "$transport" = http ]; then
      echo "$1" >"$d/url"
    else
      while [ "$1" = --env ]; do echo "$2" >>"$d/env"; shift 2; done
      [ "$1" = -- ] && shift
      echo "$1" >"$d/command"; shift
      echo "$*" >"$d/args"
    fi
    echo "add $name" >>"$S/mutations.log"
    ;;
esac
"""


def _start(image: str) -> str:
    name = f"synctest-{uuid.uuid4().hex[:10]}"
    subprocess.run(
        ["docker", "run", "-d", "--rm", "--name", name, image, "sleep", "900"],
        check=True,
        capture_output=True,
    )
    return name


def _root(name: str, script: str) -> None:
    subprocess.run(
        ["docker", "exec", "-u", "root", name, "bash", "-c", script],
        check=True,
        capture_output=True,
    )


@pytest.fixture
def box(host_image: str, lock_dir: Path) -> Iterator[str]:
    """A throwaway container whose `claude` records its argv (jq is the real one)."""
    name = _start(host_image)
    try:
        subprocess.run(
            [
                "docker",
                "exec",
                "-i",
                "-u",
                "root",
                name,
                "sh",
                "-c",
                "cat > /usr/local/bin/claude && chmod +x /usr/local/bin/claude",
            ],
            input=STUB_CLAUDE,
            text=True,
            check=True,
            capture_output=True,
        )
        yield name
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, check=False)


@pytest.fixture
def box_real_claude(host_image: str, lock_dir: Path) -> Iterator[str]:
    name = _start(host_image)
    try:
        yield name
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, check=False)


@pytest.fixture
def box_no_jq(host_image: str, lock_dir: Path) -> Iterator[str]:
    name = _start(host_image)
    try:
        _root(name, 'rm -f "$(command -v jq)"; ! command -v jq')
        yield name
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, check=False)


def sh(container: str, script: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", "exec", "-u", "agent", container, "bash", "-c", script],
        capture_output=True,
        text=True,
        check=False,
    )


def registered(container: str) -> list[str]:
    """The server names the stub claude holds, sorted."""
    out = sh(container, "ls ~/stub 2>/dev/null | sed -n 's/^srv-//p' | sort").stdout
    return out.split()


def state(container: str) -> str:
    """Every file under ~/.claude with its checksum and mtime, to prove a rerun changes nothing."""
    r = sh(
        container,
        "cd ~/.claude && find . -type f -printf '%p %T@ %m\\n' | sort; find . -type f -exec sha256sum {} + | sort",
    )
    # The lock files are re-opened (mtime moves) on every run; they carry no state.
    return "".join(
        ln + "\n"
        for ln in r.stdout.splitlines()
        if ".ae-sync/lock" not in ln and "settings.lock" not in ln
    )


def test_a_full_sync_places_everything_as_the_agent_user(tmp_path: Path, box: str) -> None:
    repo = make_repo(tmp_path, {})
    r = run_sync(repo, box)
    assert r.returncode == 0, r.stderr + r.stdout

    # team skills land with the marker; personal and other-surface skills do not
    assert (
        sh(
            box,
            "test -f ~/.claude/skills/alpha/SKILL.md && test -f ~/.claude/skills/alpha/.ae-managed",
        ).returncode
        == 0
    )
    assert sh(box, "test -f ~/.claude/skills/alpha/helper.txt").returncode == 0
    assert sh(box, "test -f ~/.claude/skills/linuxy/.ae-managed").returncode == 0
    assert sh(box, "test -e ~/.claude/skills/secret").returncode != 0
    assert sh(box, "test -e ~/.claude/skills/winonly").returncode != 0
    assert sh(box, "test -e ~/.claude/global-CLAUDE.md").returncode != 0

    # everything is owned by the container's user; nothing is root's
    assert sh(box, "find ~/.claude ! -user agent | head -1").stdout.strip() == ""
    assert sh(box, "find ~/.claude -uid 61000 | wc -l").stdout.strip() != "0"

    # team rules, the import file (created once), mcp, register, staging cleaned
    assert "Team rules" in sh(box, "cat ~/.claude/ae-team/CLAUDE.md").stdout
    own = sh(box, "cat ~/.claude/CLAUDE.md").stdout
    assert "@~/.claude/ae-team/CLAUDE.md" in own and "Your own notes go below this line" in own
    assert (
        sh(
            box, "test -f ~/.claude/ae-team/mcp/freshbooks-mcp/servers/freshbooks-server.js"
        ).returncode
        == 0
    )
    assert sh(box, "test -f ~/.claude/ae-team/mcp/claude-config/mcp/pbi-mcp.sh").returncode == 0
    assert sh(box, "test -x ~/.claude/ae-team/scripts/register-team-mcp.sh").returncode == 0
    assert sh(box, "test -f ~/.claude/ae-team/scripts/team-mcp.json").returncode == 0
    assert registered(box) == SERVERS
    fb_env = sh(box, "cat ~/stub/srv-freshbooks/env").stdout.split()
    fb_secret = next(
        s for s in MERGED_MCP["servers"] if "FRESHBOOKS_REFRESH_SECRET" in s.get("env", {})
    )
    assert sorted(fb_env) == [
        "FRESHBOOKS_ACCOUNTS=AE",
        f"FRESHBOOKS_REFRESH_SECRET={fb_secret['env']['FRESHBOOKS_REFRESH_SECRET']}",
    ]
    assert sh(box, "cat ~/stub/srv-esignatures-oauth/url").stdout.startswith("https://esignatures")
    assert sh(box, "cat ~/stub/srv-powerbi-modeling-sp/args").stdout.strip() == (
        "/home/agent/.claude/ae-team/mcp/claude-config/mcp/pbi-mcp.sh"
    )
    assert "ran register-team-mcp.sh" in r.stdout
    assert "REGISTRATION SKIPPED" not in r.stdout + r.stderr
    assert sh(box, "cat ~/.claude/ae-team/mcp/setup2-ran.log").stdout.strip() == "ran2"
    assert sh(box, "cat ~/.claude/ae-team/mcp/setup-ran.log").stdout.strip() == "ran"
    assert sh(box, "ls ~/.claude/.ae-sync | grep -c '^run-'").stdout.strip() == "0"
    assert (
        sh(
            box,
            "python3 -c \"import json,os;d=json.load(open(os.path.expanduser('~/.claude/settings.json')));print(d['env']['DOTNET_EnableDiagnostics'], len(d['allowedMcpServers']))\"",
        ).stdout.strip()
        == "0 7"
    )


def test_a_second_run_changes_nothing(tmp_path: Path, box: str) -> None:
    repo = make_repo(tmp_path, {})
    assert run_sync(repo, box).returncode == 0
    before = state(box)
    mutations = sh(box, "cat ~/stub/mutations.log").stdout
    r = run_sync(repo, box)
    assert r.returncode == 0, r.stderr
    assert state(box) == before
    # The real register script found all three servers identical and touched none.
    assert r.stdout.count(": unchanged") == 3, r.stdout
    assert sh(box, "cat ~/stub/mutations.log").stdout == mutations
    # Setup ran once, because its stamp matched the second time.
    assert sh(box, "cat ~/.claude/ae-team/mcp/setup-ran.log").stdout.strip() == "ran"


def test_a_user_own_skill_survives_and_a_removed_team_skill_goes(tmp_path: Path, box: str) -> None:
    repo = make_repo(tmp_path, {})
    sh(box, "mkdir -p ~/.claude/skills/mine && echo mine > ~/.claude/skills/mine/SKILL.md")
    assert run_sync(repo, box).returncode == 0
    assert sh(box, "test -d ~/.claude/skills/linuxy").returncode == 0

    repo = make_repo(
        tmp_path,
        {
            "claude-config/skills-manifest.txt": MANIFEST.replace(
                "skills/linuxy team linux\n", "skills/linuxy personal linux\n"
            )
        },
    )
    r = run_sync(repo, box)
    assert r.returncode == 0, r.stderr
    assert sh(box, "test -e ~/.claude/skills/linuxy").returncode != 0
    assert sh(box, "test -d ~/.claude/skills/alpha").returncode == 0
    assert sh(box, "cat ~/.claude/skills/mine/SKILL.md").stdout.strip() == "mine"
    assert sh(box, "test -e ~/.claude/skills/mine/.ae-managed").returncode != 0


def test_a_user_folder_with_a_team_skill_name_is_left_alone(tmp_path: Path, box: str) -> None:
    repo = make_repo(tmp_path, {})
    sh(box, "mkdir -p ~/.claude/skills/alpha && echo theirs > ~/.claude/skills/alpha/SKILL.md")
    r = run_sync(repo, box)
    assert r.returncode == 0, r.stderr
    assert "left alone" in r.stderr
    assert sh(box, "cat ~/.claude/skills/alpha/SKILL.md").stdout.strip() == "theirs"


def test_a_changed_team_skill_is_replaced_whole(tmp_path: Path, box: str) -> None:
    repo = make_repo(tmp_path, {})
    assert run_sync(repo, box).returncode == 0
    repo = make_repo(tmp_path, {"skills/alpha/helper.txt": None, "skills/alpha/new.txt": "new\n"})
    assert run_sync(repo, box).returncode == 0
    assert sh(box, "test -e ~/.claude/skills/alpha/helper.txt").returncode != 0
    assert sh(box, "cat ~/.claude/skills/alpha/new.txt").stdout.strip() == "new"
    assert sh(box, "ls -A ~/.claude/skills").stdout.split() == ["alpha", "linuxy"]


def test_claude_md_is_created_once_and_never_overwritten(tmp_path: Path, box: str) -> None:
    repo = make_repo(tmp_path, {})
    assert run_sync(repo, box).returncode == 0
    sh(box, "echo 'my own note' >> ~/.claude/CLAUDE.md")
    mine = sh(box, "cat ~/.claude/CLAUDE.md").stdout
    assert run_sync(repo, box).returncode == 0
    assert sh(box, "cat ~/.claude/CLAUDE.md").stdout == mine

    sh(box, "echo 'no import line any more' > ~/.claude/CLAUDE.md")
    r = run_sync(repo, box)
    assert r.returncode == 0
    assert "no longer imports the team rules" in r.stderr
    assert sh(box, "cat ~/.claude/CLAUDE.md").stdout.strip() == "no import line any more"


def test_settings_merge_is_deep_keeps_the_users_keys_and_unions_the_lists(
    tmp_path: Path, box: str
) -> None:
    repo = make_repo(tmp_path, {})
    mine = {
        "model": "mine",
        "permissions": {"allow": ["Bash(ls)"]},
        "env": {"MY_VAR": "1", "DOTNET_EnableDiagnostics": "1"},
        "enabledPlugins": {"mine@market": True},
        "extraKnownMarketplaces": {"mine": {"source": {"source": "github", "repo": "me/m"}}},
        "allowedMcpServers": [{"serverName": "mine"}, {"serverName": "freshbooks"}],
    }
    sh(box, f"cat > ~/.claude/settings.json <<'EOF'\n{json.dumps(mine)}\nEOF")
    sh(box, "chmod 640 ~/.claude/settings.json")
    assert run_sync(repo, box).returncode == 0
    merged = json.loads(sh(box, "cat ~/.claude/settings.json").stdout)
    assert merged["model"] == "mine"
    assert merged["permissions"] == {"allow": ["Bash(ls)"]}
    assert merged["env"]["MY_VAR"] == "1"
    assert merged["env"]["DOTNET_EnableDiagnostics"] == "0"  # the team value wins per leaf
    assert merged["env"]["DOTNET_hostBuilder__reloadConfigOnChange"] == "false"
    assert set(merged["enabledPlugins"]) == {"mine@market", *MERGED_SETTINGS["enabledPlugins"]}
    assert set(merged["extraKnownMarketplaces"]) == {
        "mine",
        *MERGED_SETTINGS["extraKnownMarketplaces"],
    }
    names = [e.get("serverName") or e.get("serverUrl") for e in merged["allowedMcpServers"]]
    assert len(names) == len(set(names))
    assert names[:2] == ["mine", "freshbooks"]  # the user's entries stay first
    assert {
        e.get("serverName") or e.get("serverUrl") for e in MERGED_SETTINGS["allowedMcpServers"]
    } <= set(names)
    assert sh(box, "stat -c %a ~/.claude/settings.json").stdout.strip() == "640"
    before = state(box)
    assert run_sync(repo, box).returncode == 0
    assert state(box) == before


def test_a_new_settings_file_is_private(tmp_path: Path, box: str) -> None:
    assert run_sync(make_repo(tmp_path, {}), box).returncode == 0
    assert sh(box, "stat -c %a ~/.claude/settings.json").stdout.strip() == "600"


def test_a_malformed_settings_file_is_left_alone_and_the_sync_fails(
    tmp_path: Path, box: str
) -> None:
    sh(box, "echo '{not json' > ~/.claude/settings.json")
    r = run_sync(make_repo(tmp_path, {}), box)
    assert r.returncode != 0
    assert "SETTINGS NOT MERGED" in r.stderr
    assert sh(box, "cat ~/.claude/settings.json").stdout.strip() == "{not json"
    assert sh(box, "test -f ~/.claude/skills/alpha/.ae-managed").returncode == 0


def test_an_existing_node_modules_survives_a_resync(tmp_path: Path, box: str) -> None:
    repo = make_repo(tmp_path, {})
    assert run_sync(repo, box).returncode == 0
    sh(
        box,
        "mkdir -p ~/.claude/ae-team/mcp/freshbooks-mcp/servers/node_modules/x && echo y > ~/.claude/ae-team/mcp/freshbooks-mcp/servers/node_modules/x/i.js",
    )
    repo = make_repo(tmp_path, {f"{FB}/freshbooks-server.js": "console.log('v2');\n"})
    assert run_sync(repo, box).returncode == 0
    assert (
        sh(
            box, "cat ~/.claude/ae-team/mcp/freshbooks-mcp/servers/freshbooks-server.js"
        ).stdout.strip()
        == "console.log('v2');"
    )
    assert (
        sh(
            box, "test -f ~/.claude/ae-team/mcp/freshbooks-mcp/servers/node_modules/x/i.js"
        ).returncode
        == 0
    )


def test_dry_run_writes_nothing_in_the_container(tmp_path: Path, box: str) -> None:
    repo = make_repo(tmp_path, {})
    r = run_sync(repo, box, "--dry-run")
    assert r.returncode == 0, r.stderr
    assert sh(box, "test -e ~/.claude/skills").returncode != 0
    assert sh(box, "ls -A ~/.claude | wc -l").stdout.strip() == "0"


def test_a_marker_aborts_the_real_sync_and_the_container_stays_untouched(
    tmp_path: Path, box: str
) -> None:
    repo = make_repo(tmp_path, {"skills/alpha/SKILL.md": "x " + HOME_PATH + "/y\n"})
    r = run_sync(repo, box)
    assert r.returncode != 0
    assert HOME_PATH in r.stderr
    assert sh(box, "test -e ~/.claude/skills").returncode != 0
    assert sh(box, "test -e ~/.claude/.ae-sync").returncode != 0


def test_registration_skipped_is_one_loud_line_and_the_sync_still_succeeds(
    tmp_path: Path, box_no_jq: str
) -> None:
    repo = make_repo(tmp_path, {})
    r = run_sync(repo, box_no_jq)
    assert r.returncode == 0, r.stderr
    loud = [
        ln for ln in (r.stdout + r.stderr).splitlines() if ln.startswith("REGISTRATION SKIPPED:")
    ]
    assert len(loud) == 1, r.stdout + r.stderr
    assert "jq" in loud[0]
    # everything else was still placed
    assert sh(box_no_jq, "test -f ~/.claude/skills/alpha/.ae-managed").returncode == 0
    assert sh(box_no_jq, "test -e ~/claude-calls.log").returncode != 0


def test_registration_with_the_real_claude_cli_adds_the_servers(
    tmp_path: Path, box_real_claude: str
) -> None:
    repo = make_repo(tmp_path, {})
    r = run_sync(repo, box_real_claude)
    assert r.returncode == 0, r.stderr + r.stdout
    assert "REGISTRATION SKIPPED" not in r.stdout + r.stderr
    config = json.loads(sh(box_real_claude, "cat ~/.claude.json").stdout)
    assert sorted(config["mcpServers"]) == SERVERS


# ── registration against the merged register-team-mcp.sh ──


def test_a_failed_add_restores_the_old_server_and_fails_the_sync(tmp_path: Path, box: str) -> None:
    sh(
        box,
        "d=~/stub/srv-freshbooks; mkdir -p $d && echo stdio > $d/type && echo node > $d/command "
        "&& echo /old/path.js > $d/args && echo FRESHBOOKS_ACCOUNTS=AE > $d/env "
        "&& echo freshbooks > ~/stub/fail-add",
    )
    r = run_sync(make_repo(tmp_path, {}), box)
    assert r.returncode != 0
    assert "FAILED to add" in r.stderr
    assert "previous definition restored" in r.stderr
    assert sh(box, "cat ~/stub/srv-freshbooks/args").stdout.strip() == "/old/path.js"
    assert registered(box) == SERVERS  # the other two registered; freshbooks was put back


@pytest.mark.parametrize(
    "team_mcp",
    ['{"servers": []}', '{"servers": [{"name": "x"}]}'],
    ids=["empty-servers", "missing-fields"],
)
def test_an_invalid_team_mcp_schema_fails_in_the_register_script(
    tmp_path: Path, box: str, team_mcp: str
) -> None:
    repo = make_repo(tmp_path, {"claude-config/team-mcp.json": team_mcp})
    r = run_sync(repo, box)
    assert r.returncode != 0
    assert 'needs a non-empty "servers" list' in r.stderr
    assert registered(box) == []


def test_invalid_json_in_team_mcp_stops_before_anything_is_sent(
    tmp_path: Path, lock_dir: Path
) -> None:
    repo = make_repo(tmp_path, {"claude-config/team-mcp.json": "{not json"})
    r = run_sync(repo, "synctest-none", "--dry-run")
    assert r.returncode != 0
    assert "team-mcp.json is not usable" in r.stderr


# ── the shell flow ──


def test_scan_two_inside_the_container_removes_the_stage_and_names_paths_only(
    tmp_path: Path, box: str
) -> None:
    secret = "UNIQUE-TOKEN-4f9a"
    r = run_sync(
        make_repo(tmp_path, {}),
        box,
        env={
            "AE_SYNC_TEST_INJECT_FILE": "skills/alpha/leak.md",
            "AE_SYNC_TEST_INJECT_CONTENT": f"{secret} {HOME_PATH}",
        },
    )
    out = r.stdout + r.stderr
    assert r.returncode != 0
    assert "SCAN FAILED in the container" in out
    assert "skills/alpha/leak.md" in out
    assert secret not in out  # names only, never content
    assert sh(box, "ls ~/.claude/.ae-sync | grep -c '^run-'").stdout.strip() == "0"
    assert sh(box, "test -e ~/.claude/skills").returncode != 0


def test_a_second_sync_in_the_container_waits_for_the_first(tmp_path: Path, box: str) -> None:
    sh(box, "mkdir -p ~/.claude/.ae-sync")
    subprocess.run(
        [
            "docker",
            "exec",
            "-d",
            "-u",
            "agent",
            box,
            "bash",
            "-c",
            "exec 8>$HOME/.claude/.ae-sync/lock; flock 8; sleep 30",
        ],
        check=True,
    )
    time.sleep(1)
    r = run_sync(make_repo(tmp_path, {}), box)
    assert r.returncode == 0, r.stderr
    assert "another sync is running" in r.stdout
    assert sh(box, "test -e ~/.claude/skills").returncode != 0


def test_the_host_lock_lives_under_the_runtime_dir_not_tmpdir(
    tmp_path: Path, lock_dir: Path
) -> None:
    runtime = tmp_path / "run"
    runtime.mkdir()
    r = run_sync(
        make_repo(tmp_path, {}),
        "synctest-none",
        "--dry-run",
        env={"XDG_RUNTIME_DIR": str(runtime)},
    )
    assert r.returncode == 0, r.stderr
    assert (runtime / "ae-host-config-sync-member.lock").exists()
    assert not list(lock_dir.glob("*.lock"))


def _fake_docker(tmp_path: Path, body: str) -> dict[str, str]:
    bindir = tmp_path / "fakebin"
    bindir.mkdir()
    (bindir / "docker").write_text("#!/bin/sh\n" + body)
    (bindir / "docker").chmod(0o755)
    return {"PATH": f"{bindir}:{os.environ['PATH']}"}


def test_a_docker_error_is_a_failure_not_a_skipped_sync(tmp_path: Path, lock_dir: Path) -> None:
    env = _fake_docker(
        tmp_path,
        'echo "Cannot connect to the Docker daemon at unix:///var/run/docker.sock" >&2; exit 1\n',
    )
    r = run_sync(make_repo(tmp_path, {}), "synctest-none", env=env)
    assert r.returncode != 0
    assert "docker inspect" in r.stderr and "Cannot connect" in r.stderr
    assert "sync skipped" not in r.stdout


def test_a_missing_container_is_the_one_line_skip(tmp_path: Path, lock_dir: Path) -> None:
    env = _fake_docker(tmp_path, 'echo "Error: No such object: x" >&2; exit 1\n')
    r = run_sync(make_repo(tmp_path, {}), "synctest-none", env=env)
    assert r.returncode == 0
    assert r.stdout.strip().endswith("is not running; sync skipped")
    assert len(r.stdout.strip().splitlines()) == 1


def test_a_production_target_must_be_omnigent_host_user(tmp_path: Path, lock_dir: Path) -> None:
    r = run_sync(make_repo(tmp_path, {}), "omnigent-host-someone-else", "--dry-run")
    assert r.returncode != 0
    assert "refused" in r.stderr


# ── skill placement safety ──


def test_swap_artifacts_stay_out_of_skills_and_a_users_lookalike_folder_survives(
    tmp_path: Path, box: str
) -> None:
    sh(
        box,
        "mkdir -p ~/.claude/skills/.ae-new-mine ~/.claude/skills/.ae-old-mine "
        "&& echo keep > ~/.claude/skills/.ae-new-mine/f && echo keep > ~/.claude/skills/.ae-old-mine/f",
    )
    assert run_sync(make_repo(tmp_path, {}), box).returncode == 0
    assert sh(
        box, "cat ~/.claude/skills/.ae-new-mine/f ~/.claude/skills/.ae-old-mine/f"
    ).stdout.split() == [
        "keep",
        "keep",
    ]
    assert sh(box, "ls -A ~/.claude/skills").stdout.split() == [
        ".ae-new-mine",
        ".ae-old-mine",
        "alpha",
        "linuxy",
    ]


def test_an_interrupted_swap_is_recovered_without_touching_unowned_folders(
    tmp_path: Path, box: str
) -> None:
    sh(
        box,
        "mkdir -p ~/.claude/.ae-sync/swap/old-alpha ~/.claude/.ae-sync/swap/new-zeta ~/.claude/skills/theirs "
        "&& echo old > ~/.claude/.ae-sync/swap/old-alpha/SKILL.md && : > ~/.claude/.ae-sync/swap/old-alpha/.ae-managed "
        "&& echo theirs > ~/.claude/skills/theirs/SKILL.md",
    )
    r = run_sync(make_repo(tmp_path, {}), box)
    assert r.returncode == 0, r.stderr
    assert "restored skill alpha from an interrupted swap" in r.stderr
    assert "Alpha." in sh(box, "cat ~/.claude/skills/alpha/SKILL.md").stdout
    assert sh(box, "ls -A ~/.claude/.ae-sync/swap").stdout.strip() == ""
    assert sh(box, "cat ~/.claude/skills/theirs/SKILL.md").stdout.strip() == "theirs"


# ── unit tests on the helper ──


def _helper():  # type: ignore[no-untyped-def]
    import importlib.util

    spec = importlib.util.spec_from_file_location("hcs", ROOT / "deploy" / "host-config-sync.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_merge_settings_unit() -> None:
    h = _helper()
    cur = {
        "a": 1,
        "env": {"X": "1", "Y": "2"},
        "allowedMcpServers": [{"serverName": "u"}, {"serverName": "t"}],
        "list": [1, 2],
    }
    team = {
        "env": {"Y": "9"},
        "allowedMcpServers": [
            {"serverName": "t"},
            {"serverUrl": "https://x/*"},
            {"serverName": "t"},
        ],
        "list": [2, 3],
        "new": {"k": 1},
    }
    out = h.merge_settings(cur, team)
    assert out["a"] == 1
    assert out["env"] == {"X": "1", "Y": "9"}
    assert out["allowedMcpServers"] == [
        {"serverName": "u"},
        {"serverName": "t"},
        {"serverUrl": "https://x/*"},
    ]
    assert out["list"] == [1, 2, 3]
    assert out["new"] == {"k": 1}


def test_a_setup_command_is_killed_after_the_timeout(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    h = _helper()
    h.SETUP_TIMEOUT_SECONDS = 1
    started = time.time()
    assert h.run("sleep 30", tmp_path, "slow setup") is False
    assert time.time() - started < 15
    assert "timed out" in capsys.readouterr().err


# ── the scan: normalisation and the rest of the marker list ──

MARKER_VARIANTS = {
    "upper-case": "see " + "OWNER" + "@example.com",
    "spaced-at": "mail owner " + "@" + " example.com",
    "zero-width": "see ow" + "\u200b" + "ner@example.com",
    "fullwidth": "see " + "\uff4f\uff57\uff4e\uff45\uff52" + "@example.com",
    "doubled-backslashes": "path " + "C:\\\\Users\\\\" + "winuser" + "\\\\x",
    "forward-slashes-windows": "path " + "C:/Users/" + "winuser" + "/x",
    "wrapped-two-lines": "owner\n@example.com",
    "wrapped-company": "work at Example\nCorp today",
    "owner-person-address": "write to " + "owner." + "person@example.com",
    "company-underscore": "use " + "example_" + "corp",
    "company-hyphen": "use " + "Example-" + "Corp",
    "onedrive": "saved in " + "One" + "Drive",
    "claude-memory": "read " + ".claude/" + "memory/x.md",
    "checkout-root": "cd " + "C:/" + "GitHub/repo",
    "tenant-fragment": "tenant " + "tenant-" + "frag1",
}


@pytest.mark.parametrize("text", list(MARKER_VARIANTS.values()), ids=list(MARKER_VARIANTS))
def test_marker_variants_abort(tmp_path: Path, lock_dir: Path, text: str) -> None:
    repo = make_repo(tmp_path, {"skills/alpha/notes.md": text + "\n"})
    r = run_sync(repo, "synctest-none", "--dry-run")
    assert r.returncode != 0, text
    assert "skills/alpha/notes.md" in r.stderr and "personal marker" in r.stderr


@pytest.mark.parametrize(
    "encoding", ["utf-16", "utf-16-be", "utf-32", "utf-8-sig"], ids=lambda e: e
)
def test_bom_encoded_files_are_decoded_and_scanned(
    tmp_path: Path, lock_dir: Path, encoding: str
) -> None:
    data = ("see " + HOME_PATH + "\n").encode(encoding)
    repo = make_repo(tmp_path, {"skills/alpha/notes.txt": data})
    r = run_sync(repo, "synctest-none", "--dry-run")
    assert r.returncode != 0
    assert "personal marker" in r.stderr


def test_bomless_utf16_is_scanned_with_the_nuls_dropped(tmp_path: Path, lock_dir: Path) -> None:
    data = ("see " + HOME_PATH + "\n").encode("utf-16-le")
    repo = make_repo(tmp_path, {"skills/alpha/notes.txt": data})
    assert run_sync(repo, "synctest-none", "--dry-run").returncode != 0


def test_an_undecodable_text_file_fails_instead_of_being_skipped(
    tmp_path: Path, lock_dir: Path
) -> None:
    repo = make_repo(tmp_path, {"skills/alpha/notes.md": b"caf\xe9 \xff\xfe not utf-8"})
    r = run_sync(repo, "synctest-none", "--dry-run")
    assert r.returncode != 0
    assert "notes.md" in r.stderr and "not valid UTF-8" in r.stderr


def test_a_real_png_is_skipped_but_a_fake_one_is_scanned(tmp_path: Path, lock_dir: Path) -> None:
    png = b"\x89PNG\r\n\x1a\n" + bytes(range(256)) + HOME_PATH.encode()
    ok = make_repo(tmp_path, {"skills/alpha/pic.png": png})
    assert run_sync(ok, "synctest-none", "--dry-run").returncode == 0
    fake = make_repo(tmp_path, {"skills/alpha/pic.png": b"not a png " + HOME_PATH.encode()})
    assert run_sync(fake, "synctest-none", "--dry-run").returncode != 0


def test_the_brand_rulebook_may_name_the_second_company(tmp_path: Path, lock_dir: Path) -> None:
    brand = "skills/analytic-endeavors-brand-guidelines"
    files = {
        "claude-config/skills-manifest.txt": MANIFEST + f"{brand} team any\n",
        f"{brand}/SKILL.md": "---\nname: brand\n---\n",
        f"{brand}/references/rulebook.md": "Example " + "Corp and AE are two companies.\n",
    }
    ok = run_sync(make_repo(tmp_path, files), "synctest-none", "--dry-run")
    assert ok.returncode == 0, ok.stderr
    files[f"{brand}/references/other.md"] = "Example " + "Corp appears here.\n"
    assert run_sync(make_repo(tmp_path, files), "synctest-none", "--dry-run").returncode != 0


@pytest.mark.parametrize(
    "path",
    [
        "skills/alpha/History.jsonl",
        "skills/alpha/.SSH/config",
        "skills/alpha/Settings.json",
        "skills/alpha/.Credentials.JSON",
        "skills/alpha/.ENV",
        "skills/alpha/Node_Modules/x.js",
        "skills/alpha/.Config/GH/hosts.yml",
        "skills/alpha/Global-Claude.md",
        "skills/alpha/POLICY-LIMITS.json",
    ],
)
def test_deny_names_match_in_any_case(tmp_path: Path, lock_dir: Path, path: str) -> None:
    repo = make_repo(tmp_path, {path: "harmless\n"})
    r = run_sync(repo, "synctest-none", "--dry-run")
    assert r.returncode != 0, path
    assert path in r.stderr and "deny" in r.stderr


@pytest.mark.parametrize(
    "settings",
    [
        {"permissions": {"allow": ["Bash"]}},
        {"hooks": {}},
        {"model": "x"},
        {"statusLine": {}},
        {"autoMode": True},
        {"apiKeyHelper": "/bin/x"},
        {"awsAuthRefresh": "x"},
        {"sandbox": {}},
        {"enableAllProjectMcpServers": True},
        {"env": {"ANTHROPIC_API_KEY": "x"}},
        {"env": {"anthropic_base_url": "x"}},
    ],
    ids=lambda d: next(iter(d)) + ("-" + next(iter(d["env"])) if "env" in d else ""),
)
def test_team_settings_outside_the_allowlist_are_refused(
    tmp_path: Path, lock_dir: Path, settings: dict
) -> None:
    repo = make_repo(tmp_path, {"claude-config/team-settings.json": json.dumps(settings)})
    r = run_sync(repo, "synctest-none", "--dry-run")
    assert r.returncode != 0
    assert "team-settings.json" in r.stderr


def test_a_manifest_with_no_team_rows_fails_the_plan(tmp_path: Path, lock_dir: Path) -> None:
    manifest = "skills/alpha personal any\nskills/linuxy team windows\n"
    repo = make_repo(tmp_path, {"claude-config/skills-manifest.txt": manifest})
    r = run_sync(repo, "synctest-none", "--dry-run")
    assert r.returncode != 0
    assert "none is scope team" in r.stderr


def test_literal_pathspecs_keep_a_glob_in_the_manifest_from_matching(
    tmp_path: Path, lock_dir: Path
) -> None:
    repo = make_repo(
        tmp_path,
        {"claude-config/skills-manifest.txt": MANIFEST + "skills/* team any\n"},
    )
    r = run_sync(repo, "synctest-none", "--dry-run")
    assert r.returncode != 0
    assert "skills/*" in r.stderr


# ── the fixtures are the merged ae-claude-skills files ──

SKILLS_CLONE = Path.home() / "repos" / "ae-claude-skills"


@pytest.mark.parametrize(
    "fixture,repo_path",
    [
        ("register-team-mcp.sh", "scripts/register-team-mcp.sh"),
        ("team-mcp.json", "claude-config/team-mcp.json"),
        ("team-settings.json", "claude-config/team-settings.json"),
    ],
)
def test_fixtures_equal_ae_claude_skills_origin_main(fixture: str, repo_path: str) -> None:
    if not (SKILLS_CLONE / ".git").exists():
        pytest.skip("the box's ae-claude-skills clone is not here")
    r = subprocess.run(
        ["git", "-C", str(SKILLS_CLONE), "show", f"refs/remotes/origin/main:{repo_path}"],
        capture_output=True,
        check=False,
    )
    if r.returncode != 0:
        pytest.skip("origin/main of the clone does not have the file")
    assert (FIXTURES / fixture).read_bytes() == r.stdout, (
        f"tests/deploy/fixtures/{fixture} drifted from ae-claude-skills origin/main; "
        "copy the new version over it"
    )


def test_scan_dir_refuses_a_symlinked_directory(tmp_path: Path) -> None:
    h = _helper()
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "lnk").symlink_to("/etc")
    with pytest.raises(SystemExit):
        h.scan_dir(
            str(tmp_path), {"expected_files": [], "scrub": {"markers": ["x"], "allowed": {}}}
        )


# ── the scrub file: missing, unreadable or empty means the sync refuses to run ──


def test_a_missing_scrub_file_refuses_and_says_what_to_write(
    tmp_path: Path, lock_dir: Path
) -> None:
    repo = make_repo(tmp_path, {})
    gone = tmp_path / "no-such-scrub.txt"
    r = run_sync(repo, "synctest-none", "--dry-run", env={"AE_HOST_SCRUB_FILE": str(gone)})
    assert r.returncode != 0
    assert str(gone) in r.stderr and "cannot be read" in r.stderr
    assert "deploy/README.md" in r.stderr and "host-scrub.txt.example" in r.stderr
    assert "nothing was sent" not in r.stdout


@pytest.mark.parametrize(
    "body", ["", "\n\n", "# only a comment\n\n"], ids=["empty", "blank", "comment"]
)
def test_an_empty_scrub_file_refuses(tmp_path: Path, lock_dir: Path, body: str) -> None:
    repo = make_repo(tmp_path, {})
    empty = tmp_path / "empty-scrub.txt"
    empty.write_text(body)
    r = run_sync(repo, "synctest-none", "--dry-run", env={"AE_HOST_SCRUB_FILE": str(empty)})
    assert r.returncode != 0
    assert "no marker lines" in r.stderr and "deploy/README.md" in r.stderr


def test_the_scrub_file_defaults_to_config_host_scrub_txt() -> None:
    h = _helper()
    assert h.SCRUB_DEFAULT == ROOT / "config" / "host-scrub.txt"
    assert h.SCRUB_ENV == "AE_HOST_SCRUB_FILE"


def test_the_example_scrub_file_parses_and_holds_only_placeholders() -> None:
    h = _helper()
    scrub = h.load_scrub(ROOT / "config" / "host-scrub.txt.example")
    assert scrub["markers"] and scrub["allowed"]
    assert "owner@example.com" not in scrub["markers"]
