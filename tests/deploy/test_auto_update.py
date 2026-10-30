"""deploy/auto-update.sh and its user timer, without a box (2026-09-24).

The script runs against a real git repository (a bare "origin" and a clone
standing in for ~/deploy/omnigent-ae) and fakes for docker, curl and systemctl
on PATH, so each test drives one pass and reads back what the box would see:
config/server.env, the state directory and the calls made. deploy/compose.sh
is the real one; its `docker compose` lands in the fake.
"""

from __future__ import annotations

import configparser
import fcntl
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "deploy" / "auto-update.sh"
USER_UNITS = ROOT / "deploy" / "systemd" / "user"
IMAGE = "ghcr.io/analyticendeavors/omnigent-ae"

# `compose ... exec -T postgres` is the pre-upgrade pg_dump (run_dump);
# $FAKE_DOCKER_EXEC picks what it prints: ok (a dump), empty, or fail. The
# same exec running psql is the default busy_count, and prints $FAKE_PSQL.
# `exec -T omnigent` is the default deploy hold read, and prints $FAKE_HOLD_EXEC.
FAKE_DOCKER = """#!/usr/bin/env bash
printf 'docker %s\\n' "$*" >>"$FAKE_CALLS"
case " $* " in
  *" exec -T omnigent "*)
    printf 'import noise\\n%s\\n' "${FAKE_HOLD_EXEC:-none}"
    exit 0
    ;;
  *" exec -T postgres "*"psql"*)
    printf '%s\\n' "${FAKE_PSQL:-0}"
    exit 0
    ;;
  *" exec -T postgres "*)
    case "${FAKE_DOCKER_EXEC:-ok}" in
      ok) printf 'CREATE TABLE fake_dump (id int);\\n'; exit 0 ;;
      empty) exit 0 ;;
      *) echo "service postgres is not running" >&2; exit 1 ;;
    esac
    ;;
esac
if [ "$1" = pull ]; then
  case "${FAKE_DOCKER_PULL:-ok}" in
    ok) echo "$3"; exit 0 ;;
    missing) echo "Error response from daemon: manifest unknown" >&2; exit 1 ;;
    *) echo "Error response from daemon: denied" >&2; exit 1 ;;
  esac
fi
exit 0
"""

# Exit codes for successive curl calls, one per line in $FAKE_CURL_PLAN;
# 0 once the plan runs out.
FAKE_CURL = """#!/usr/bin/env bash
printf 'curl %s\\n' "$*" >>"$FAKE_CALLS"
code=0
if [ -s "$FAKE_CURL_PLAN" ]; then
  code="$(head -n 1 "$FAKE_CURL_PLAN")"
  sed -i 1d "$FAKE_CURL_PLAN"
fi
exit "$code"
"""

FAKE_LOGGER = """#!/usr/bin/env bash
printf '%s %s\\n' "$(basename "$0")" "$*" >>"$FAKE_CALLS"
"""

FAILING = """#!/usr/bin/env bash
printf '%s %s\\n' "$(basename "$0")" "$*" >>"$FAKE_CALLS"
exit 1
"""

# The busy count the tests use (AE_AUTO_UPDATE_BUSY): the next line of
# $FAKE_BUSY_PLAN while it has one, else $FAKE_BUSY, 0 when unset. It records
# nothing, so the call lists stay as they were.
FAKE_BUSY = """#!/usr/bin/env bash
if [ -n "${FAKE_BUSY_PLAN:-}" ] && [ -s "$FAKE_BUSY_PLAN" ]; then
  head -n 1 "$FAKE_BUSY_PLAN"
  sed -i 1d "$FAKE_BUSY_PLAN"
  exit 0
fi
printf '%s\\n' "${FAKE_BUSY:-0}"
"""

# The deploy hold the tests use (AE_AUTO_UPDATE_DEPLOY_HOLD): the next line of
# $FAKE_HOLD_PLAN while it has one, else $FAKE_HOLD, `none` when unset, so a
# test can make the hold appear between the pass's two checks. A line
# `exit:N` exits N with nothing on stdout, as the CLI does for a corrupt
# file. It records nothing, so the call lists stay as they were.
FAKE_HOLD = """#!/usr/bin/env bash
if [ -n "${FAKE_HOLD_PLAN:-}" ] && [ -s "$FAKE_HOLD_PLAN" ]; then
  line="$(head -n 1 "$FAKE_HOLD_PLAN")"
  sed -i 1d "$FAKE_HOLD_PLAN"
else
  line="${FAKE_HOLD:-none}"
fi
case "$line" in
  exit:*) echo "ae deploy hold: the hold file is broken" >&2; exit "${line#exit:}" ;;
esac
printf '%s\\n' "$line"
"""

# A host rebuild that records which commit the checkout was on when it ran.
REBUILD_AT_HEAD = """#!/usr/bin/env bash
printf 'rebuild-host HEAD=%s\\n' "$(git -C "$AE_AUTO_UPDATE_REPO" rev-parse HEAD)" >>"$FAKE_CALLS"
"""

# The script sleeps only between git retries. The fake records the call and,
# when $FAKE_SLEEP_CLEARS names a file, removes it: the other git process
# finishing while the pass waits.
FAKE_SLEEP = """#!/usr/bin/env bash
printf 'sleep %s\\n' "$*" >>"$FAKE_CALLS"
[ -z "${FAKE_SLEEP_CLEARS:-}" ] || rm -f "$FAKE_SLEEP_CLEARS"
"""

GIT_ENV = {
    "GIT_AUTHOR_NAME": "test",
    "GIT_AUTHOR_EMAIL": "test@example.invalid",
    "GIT_COMMITTER_NAME": "test",
    "GIT_COMMITTER_EMAIL": "test@example.invalid",
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_NOSYSTEM": "1",
}


class Box:
    """A throwaway origin, a developer clone that tags releases and a box clone."""

    def __init__(self, tmp: Path) -> None:
        self.tmp = tmp
        self.origin = tmp / "origin.git"
        self.dev = tmp / "dev"
        self.box = tmp / "box"
        self.state = tmp / "state"
        self.settings = tmp / "server.env"
        self.calls_log = tmp / "calls.log"
        self.curl_plan = tmp / "curl-plan"
        self.bin = bin_dir = tmp / "bin"
        bin_dir.mkdir()
        for name, body in {
            "docker": FAKE_DOCKER,
            "curl": FAKE_CURL,
            "systemctl": FAKE_LOGGER,
            "rebuild-host": FAKE_LOGGER,
            "rebuild-host-broken": FAILING,
            "rebuild-host-at-head": REBUILD_AT_HEAD,
            "dump-broken": FAILING,
            "sleep": FAKE_SLEEP,
            "busy": FAKE_BUSY,
            "hold": FAKE_HOLD,
        }.items():
            path = bin_dir / name
            path.write_text(body)
            path.chmod(0o755)
        secrets = tmp / "omnigent-ae.env"
        secrets.write_text("POSTGRES_PASSWORD=test-only\n")
        self.settings.write_text(
            f"OMNIGENT_IMAGE_TAG=ae-v0.13.0.1\nAE_IMAGE={IMAGE}\n"
            "OMNIGENT_PORT=8010\nOMNIGENT_AUTH_ENABLED=1\n"
        )
        self.env = {
            **os.environ,
            **GIT_ENV,
            "HOME": str(tmp),
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "AE_SERVER_SETTINGS": str(self.settings),
            "AE_SERVER_SECRETS": str(secrets),
            "AE_AUTO_UPDATE_STATE": str(self.state),
            "AE_AUTO_UPDATE_REPO": str(self.box),
            "AE_AUTO_UPDATE_HOST_REBUILD": str(bin_dir / "rebuild-host"),
            "AE_AUTO_UPDATE_BUSY": str(bin_dir / "busy"),
            "AE_AUTO_UPDATE_DEPLOY_HOLD": str(bin_dir / "hold"),
            "FAKE_CALLS": str(self.calls_log),
            "FAKE_CURL_PLAN": str(self.curl_plan),
        }
        self.git(tmp, "init", "--quiet", "--bare", "--initial-branch=main", str(self.origin))
        self.git(tmp, "clone", "--quiet", str(self.origin), str(self.dev))
        self.git(self.dev, "checkout", "--quiet", "-b", "main")
        self.release("ae-v0.13.0.1")
        self.git(tmp, "clone", "--quiet", str(self.origin), str(self.box))

    def git(self, cwd: Path, *args: str) -> str:
        return subprocess.run(
            ["git", *args], cwd=cwd, env=self.env, check=True, capture_output=True, text=True
        ).stdout.strip()

    def commit(self, name: str = "change", content: str | None = None) -> None:
        (self.dev / name).write_text(content if content is not None else os.urandom(8).hex())
        self.git(self.dev, "add", name)
        self.git(self.dev, "commit", "--quiet", "-m", f"Change {name}.")

    def release(self, tag: str, *, branch: str = "main") -> None:
        """Commit on BRANCH, tag it and push both, as a release would."""
        if branch != "main":
            self.git(self.dev, "checkout", "--quiet", "-B", branch)
        self.commit()
        self.git(self.dev, "tag", tag)
        self.git(self.dev, "push", "--quiet", "origin", branch, tag)
        if branch != "main":
            self.git(self.dev, "checkout", "--quiet", "main")

    def run(self, **env: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(SCRIPT)],
            env={**self.env, **env},
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )

    def tag(self) -> str:
        for line in self.settings.read_text().splitlines():
            if line.startswith("OMNIGENT_IMAGE_TAG="):
                return line.split("=", 1)[1]
        raise AssertionError("no OMNIGENT_IMAGE_TAG line")

    def last(self) -> str:
        return (self.state / "last").read_text()

    def calls(self) -> list[str]:
        return self.calls_log.read_text().splitlines() if self.calls_log.exists() else []

    def compose_ups(self) -> list[str]:
        return [c for c in self.calls() if c.startswith("docker compose") and " up -d" in c]


@pytest.fixture
def box(tmp_path: Path) -> Box:
    return Box(tmp_path)


def test_no_new_tag_is_a_quiet_noop(box: Box) -> None:
    result = box.run()
    assert result.returncode == 0, result.stderr
    assert "from=ae-v0.13.0.1 to=ae-v0.13.0.1 result=noop" in box.last()
    assert not any(c.startswith("docker") for c in box.calls())
    assert not (box.state / "log").exists(), "a no-op does not grow the log"


def test_the_image_name_comes_from_the_settings_file(box: Box) -> None:
    # 2026-10-21, https://github.com/analyticendeavors/omnigent-ae-public/issues/13:
    # AE_IMAGE in config/server.env names the package, so a fork's box pulls its
    # own image without editing the script.
    box.settings.write_text(box.settings.read_text().replace(IMAGE, "ghcr.io/someone/fork"))
    box.release("ae-v0.13.0.2")
    result = box.run()
    assert result.returncode == 0, result.stderr
    assert "docker pull --quiet ghcr.io/someone/fork:ae-v0.13.0.2" in box.calls()


def test_missing_image_name_stops_before_anything_changes(box: Box) -> None:
    box.settings.write_text(
        "".join(
            line
            for line in box.settings.read_text().splitlines(keepends=True)
            if not line.startswith("AE_IMAGE=")
        )
    )
    box.release("ae-v0.13.0.2")
    result = box.run()
    assert result.returncode != 0
    assert "has no AE_IMAGE line" in result.stderr
    assert box.tag() == "ae-v0.13.0.1"
    assert not any(c.startswith("docker") for c in box.calls())


def test_tag_not_yet_published_waits_and_changes_nothing(box: Box) -> None:
    box.release("ae-v0.13.0.2")
    result = box.run(FAKE_DOCKER_PULL="missing")
    assert result.returncode == 0, result.stderr
    assert "result=waiting" in box.last()
    assert box.tag() == "ae-v0.13.0.1"
    assert box.compose_ups() == []
    assert not (box.state / "previous").exists()


def test_new_tag_deploys_and_passes_health(box: Box) -> None:
    box.release("ae-v0.13.0.2")
    result = box.run()
    assert result.returncode == 0, result.stderr
    assert box.tag() == "ae-v0.13.0.2"
    assert "from=ae-v0.13.0.1 to=ae-v0.13.0.2 result=ok" in box.last()
    assert (box.state / "log").read_text() == box.last()
    assert (box.state / "previous").read_text().strip() == "ae-v0.13.0.1"
    assert f"docker pull --quiet {IMAGE}:ae-v0.13.0.2" in box.calls()
    assert len(box.compose_ups()) == 1
    # compose.sh hands both env files and both compose files to docker compose.
    up = box.compose_ups()[0]
    assert f"--env-file {box.settings}" in up and "docker-compose.override.yml" in up
    health = [c for c in box.calls() if c.startswith("curl")]
    assert len(health) == 1
    for flag in ("--retry 12", "--retry-delay 5", "--retry-all-errors", "--fail", "--max-time 10"):
        assert flag in health[0]
    assert health[0].endswith("http://127.0.0.1:8010/health")
    # The checkout followed main to the release.
    assert box.git(box.box, "rev-parse", "HEAD") == box.git(box.dev, "rev-parse", "main")
    # No upstream pin moved, so the host is left alone.
    assert not any(c.startswith(("rebuild-host", "systemctl")) for c in box.calls())
    # The next pass sees nothing to do.
    assert box.run().returncode == 0
    assert "result=noop" in box.last()


def test_highest_tag_wins_by_version_sort(box: Box) -> None:
    box.release("ae-v0.13.0.9")
    box.release("ae-v0.13.0.10")
    assert box.run().returncode == 0
    assert box.tag() == "ae-v0.13.0.10"


def test_health_failure_rolls_back_and_is_not_retried(box: Box) -> None:
    box.release("ae-v0.13.0.2")
    box.curl_plan.write_text("22\n0\n")
    result = box.run()
    assert result.returncode == 1
    assert "AUTO-UPDATE rolled-back" in result.stderr
    assert box.tag() == "ae-v0.13.0.1"
    assert "to=ae-v0.13.0.2 result=rolled-back" in box.last()
    assert "result=rolled-back" in (box.state / "log").read_text()
    assert (box.state / "failed").read_text().strip() == "ae-v0.13.0.2"
    assert len(box.compose_ups()) == 2

    box.calls_log.unlink()
    again = box.run()
    assert again.returncode == 0, again.stderr
    assert "result=skipped" in box.last()
    assert box.calls() == [], "a rolled-back tag is not pulled or deployed again"

    # A newer tag moves on, and success clears the marker.
    box.release("ae-v0.13.0.3")
    assert box.run().returncode == 0
    assert box.tag() == "ae-v0.13.0.3"
    assert not (box.state / "failed").exists()


def test_failed_rollback_says_the_server_is_down(box: Box) -> None:
    box.release("ae-v0.13.0.2")
    box.curl_plan.write_text("22\n22\n")
    result = box.run()
    assert result.returncode == 1
    assert "result=rollback-failed" in box.last()
    assert "DOWN" in result.stderr
    assert box.tag() == "ae-v0.13.0.1"


def test_tag_not_on_main_is_refused(box: Box) -> None:
    box.release("ae-v0.13.0.2", branch="side")
    result = box.run()
    assert result.returncode == 1
    assert "result=refused" in box.last() and "not on origin/main" in box.last()
    assert box.tag() == "ae-v0.13.0.1"
    assert not any(c.startswith("docker") for c in box.calls())


def test_pull_error_other_than_missing_fails_loudly(box: Box) -> None:
    box.release("ae-v0.13.0.2")
    result = box.run(FAKE_DOCKER_PULL="denied")
    assert result.returncode == 1
    assert "result=pull-failed" in box.last() and "docker login ghcr.io" in box.last()
    assert box.tag() == "ae-v0.13.0.1"
    assert box.compose_ups() == []


def test_pin_holds_an_older_tag(box: Box) -> None:
    box.release("ae-v0.13.0.2")
    assert box.run().returncode == 0
    assert box.tag() == "ae-v0.13.0.2"
    box.state.joinpath("pin").write_text("ae-v0.13.0.1\n")
    assert box.run().returncode == 0
    assert box.tag() == "ae-v0.13.0.1"
    box.release("ae-v0.13.0.3")
    assert box.run().returncode == 0
    assert box.tag() == "ae-v0.13.0.1", "a pin wins over a newer tag"
    assert "result=noop" in box.last()


def _release_touching(box: Box, path: str, tag: str) -> None:
    (box.dev / path).parent.mkdir(parents=True, exist_ok=True)
    box.commit(path)
    box.git(box.dev, "tag", tag)
    box.git(box.dev, "push", "--quiet", "origin", "main", tag)


def test_upstream_pin_move_rebuilds_the_host(box: Box) -> None:
    box.commit("upstream.lock", "UPSTREAM_TAG=v0.14.0\n")
    box.git(box.dev, "tag", "ae-v0.14.0.1")
    box.git(box.dev, "push", "--quiet", "origin", "main", "ae-v0.14.0.1")
    result = box.run()
    assert result.returncode == 0, result.stderr
    assert box.tag() == "ae-v0.14.0.1"
    calls = box.calls()
    assert "rebuild-host " in calls
    assert "systemctl --user restart ae-omnigent-host.service" in calls


def test_host_rebuild_survives_a_waiting_pass(box: Box) -> None:
    # 2026-09-24, ae-v0.13.0.8 (https://github.com/analyticendeavors/omnigent-ae/pull/26):
    # the first pass found the image unpublished and fast-forwarded the
    # checkout anyway; the pass that deployed then saw no checkout movement
    # and skipped the rebuild, so the host venv lacked the limits relay.
    _release_touching(box, "upstream.lock", "ae-v0.13.0.2")
    waiting = box.run(FAKE_DOCKER_PULL="missing")
    assert waiting.returncode == 0, waiting.stderr
    assert "result=waiting" in box.last()
    assert box.git(box.box, "rev-parse", "HEAD") == box.git(box.dev, "rev-parse", "main")
    deployed = box.run()
    assert deployed.returncode == 0, deployed.stderr
    assert box.tag() == "ae-v0.13.0.2"
    assert "rebuild-host " in box.calls()
    assert "systemctl --user restart ae-omnigent-host.service" in box.calls()


def test_a_policies_change_rebuilds_the_host(box: Box) -> None:
    # The relay and the agents' MCP server run from the host venv, which
    # installs packages/ae_omni_policies, so a change there rebuilds it too.
    _release_touching(box, "packages/ae_omni_policies/src/limits_relay.py", "ae-v0.13.0.2")
    assert box.run().returncode == 0
    assert "rebuild-host " in box.calls()


def test_a_release_that_leaves_the_host_alone_does_not_rebuild(box: Box) -> None:
    _release_touching(box, "packages/ae_omni_extension/web/src/main.ts", "ae-v0.13.0.2")
    assert box.run().returncode == 0
    assert box.tag() == "ae-v0.13.0.2"
    assert not any(c.startswith(("rebuild-host", "systemctl")) for c in box.calls())


def test_an_unknown_running_tag_rebuilds_the_host(box: Box) -> None:
    box.settings.write_text(box.settings.read_text().replace("ae-v0.13.0.1", "ae-v0.12.9.9"))
    box.release("ae-v0.13.0.2")
    assert box.run().returncode == 0
    assert "rebuild-host " in box.calls(), "no tag to compare against: rebuild to be safe"


def test_host_rebuild_has_uv_on_path(box: Box) -> None:
    # uv lives in ~/.local/bin (deploy/install-host.sh); a pass started by
    # hand, not by the unit that spells PATH out, must still find it.
    recorder = box.tmp / "bin" / "rebuild-host"
    recorder.write_text(
        '#!/usr/bin/env bash\nprintf \'rebuild-host PATH=%s\\n\' "$PATH" >>"$FAKE_CALLS"\n'
    )
    _release_touching(box, "upstream.lock", "ae-v0.13.0.2")
    assert box.run().returncode == 0
    rebuilds = [c for c in box.calls() if c.startswith("rebuild-host")]
    assert len(rebuilds) == 1
    assert f"{box.tmp}/.local/bin" in rebuilds[0].split("PATH=", 1)[1].split(":")


# ── trip hardening (2026-09-24) ────────────────────────────────────────────


def test_a_held_index_lock_is_retried_until_it_clears(box: Box) -> None:
    # 2026-09-24 19:00 PDT on the box: a session's `git status` held
    # .git/index.lock when the merge ran, and the pass failed with a record
    # blaming local commits that did not exist.
    box.release("ae-v0.13.0.2")
    lock = box.box / ".git" / "index.lock"
    lock.touch()
    result = box.run(FAKE_SLEEP_CLEARS=str(lock))
    assert result.returncode == 0, result.stderr
    assert box.tag() == "ae-v0.13.0.2"
    assert "result=ok" in box.last()
    assert [c for c in box.calls() if c.startswith("sleep")] == ["sleep 2"]
    assert box.git(box.box, "rev-parse", "HEAD") == box.git(box.dev, "rev-parse", "main")


def test_a_lock_that_never_clears_records_gits_own_error(box: Box) -> None:
    box.release("ae-v0.13.0.2")
    lock = box.box / ".git" / "index.lock"
    lock.touch()
    result = box.run(AE_AUTO_UPDATE_GIT_TRIES="3")
    assert result.returncode == 1
    last = box.last()
    assert "result=git-failed" in last
    # git says "fatal:" or "error:" depending on the step and the version.
    assert f": Unable to create '{lock}': File exists." in last
    assert re.search(r"failed in \S+: (fatal|error): Unable to create", last)
    assert "still held after 3 tries" in last
    assert "local commits" not in last, "the record quotes git instead of guessing"
    assert "\n" not in last.rstrip("\n"), "the record stays one line"
    assert len([c for c in box.calls() if c.startswith("sleep")]) == 2
    assert lock.exists(), "a lock file is never deleted"
    assert box.tag() == "ae-v0.13.0.1"
    assert box.compose_ups() == []


def test_a_git_failure_that_is_not_a_lock_fails_at_once_with_gits_line(box: Box) -> None:
    box.release("ae-v0.13.0.2")
    # A local commit in the box checkout makes the fast-forward impossible.
    (box.box / "local").write_text("x")
    box.git(box.box, "add", "local")
    box.git(box.box, "commit", "--quiet", "-m", "Local change.")
    result = box.run()
    assert result.returncode == 1
    assert "result=git-failed" in box.last()
    assert "detail=git merge --ff-only origin/main failed" in box.last()
    assert "fatal:" in box.last()
    assert not any(c.startswith("sleep") for c in box.calls()), "only a held lock is retried"


def test_a_failed_host_rebuild_is_retried_by_a_noop_pass(box: Box) -> None:
    _release_touching(box, "upstream.lock", "ae-v0.13.0.2")
    broken = box.run(AE_AUTO_UPDATE_HOST_REBUILD=str(box.bin / "rebuild-host-broken"))
    assert broken.returncode == 1
    assert "result=host-failed" in box.last()
    assert box.tag() == "ae-v0.13.0.2", "the server stays on the healthy new tag"
    pending = box.state / "host-pending"
    assert pending.read_text().strip() == "ae-v0.13.0.2"

    # Still broken on the next pass: the unit fails again, the marker stays.
    box.calls_log.unlink()
    again = box.run(AE_AUTO_UPDATE_HOST_REBUILD=str(box.bin / "rebuild-host-broken"))
    assert again.returncode == 1
    assert "result=host-failed" in box.last() and "failed again" in box.last()
    assert pending.exists()
    assert box.compose_ups() == [], "the retry deploys nothing"

    # Fixed: the noop pass rebuilds, restarts the host and clears the marker.
    box.calls_log.unlink()
    fixed = box.run()
    assert fixed.returncode == 0, fixed.stderr
    assert "rebuild-host " in box.calls()
    assert "systemctl --user restart ae-omnigent-host.service" in box.calls()
    assert not pending.exists()
    assert "result=host-ok" in (box.state / "log").read_text()
    assert "result=noop" in box.last()
    assert box.compose_ups() == []

    # And the pass after that has nothing left to retry.
    box.calls_log.unlink()
    assert box.run().returncode == 0
    assert box.calls() == []


# ── waiting for quiet (2026-10-17) ─────────────────────────────────────────


def test_a_session_mid_turn_defers_the_deploy_until_quiet(box: Box) -> None:
    box.release("ae-v0.13.0.2")
    head = box.git(box.box, "rev-parse", "HEAD")
    busy = box.run(FAKE_BUSY="2")
    assert busy.returncode == 0, busy.stderr
    assert "to=ae-v0.13.0.2 result=deferred" in box.last()
    assert "2 session(s) mid-turn" in box.last() and "goes ahead anyway at" in box.last()
    assert box.tag() == "ae-v0.13.0.1"
    assert box.git(box.box, "rev-parse", "HEAD") == head, "nothing moves, the checkout included"
    assert not any(c.startswith("docker") for c in box.calls())
    # The clock runs from the running tag, so a newer merge cannot reset it.
    assert (box.state / "deferred").read_text().split()[0] == "ae-v0.13.0.1"
    assert not (box.state / "log").exists(), "a deferral does not grow the log"
    assert "No such file" not in busy.stderr

    quiet = box.run()
    assert quiet.returncode == 0, quiet.stderr
    assert box.tag() == "ae-v0.13.0.2"
    assert "result=ok" in box.last()
    assert not (box.state / "deferred").exists()


def test_a_tag_that_waited_past_the_limit_deploys_anyway(box: Box) -> None:
    box.release("ae-v0.13.0.2")
    started = int(time.time()) - 7201
    (box.state).mkdir(exist_ok=True)
    (box.state / "deferred").write_text(f"ae-v0.13.0.1 {started}\n")
    result = box.run(FAKE_BUSY="1")
    assert result.returncode == 0, result.stderr
    assert box.tag() == "ae-v0.13.0.2"
    assert "going ahead anyway" in result.stdout + result.stderr
    assert not (box.state / "deferred").exists(), "a deploy that lands clears the clock"


def test_a_newer_tag_keeps_the_clock_and_the_limit_can_be_set(box: Box) -> None:
    box.release("ae-v0.13.0.2")
    since = int(time.time()) - 600
    (box.state).mkdir(exist_ok=True)
    (box.state / "deferred").write_text(f"ae-v0.13.0.1 {since}\n")
    waiting = box.run(FAKE_BUSY="1", AE_AUTO_UPDATE_MAX_DEFER="900")
    assert waiting.returncode == 0, waiting.stderr
    assert "to=ae-v0.13.0.2 result=deferred" in box.last()

    # A merge lands while the sessions stay busy: the wait it inherits is the
    # same ten minutes, not a fresh start.
    box.release("ae-v0.13.0.3")
    still = box.run(FAKE_BUSY="1", AE_AUTO_UPDATE_MAX_DEFER="900")
    assert "to=ae-v0.13.0.3 result=deferred" in box.last(), still.stderr
    assert (box.state / "deferred").read_text().split() == ["ae-v0.13.0.1", str(since)]

    done = box.run(FAKE_BUSY="1", AE_AUTO_UPDATE_MAX_DEFER="300")
    assert done.returncode == 0, done.stderr
    assert box.tag() == "ae-v0.13.0.3"


def _wait_state(box: Box, *, since_ago: int, last_pass_ago: int | None, name: str = "deferred"):
    now = int(time.time())
    box.state.mkdir(exist_ok=True)
    (box.state / name).write_text(f"ae-v0.13.0.1 {now - since_ago}\n")
    if last_pass_ago is not None:
        (box.state / "last-pass").write_text(f"{now - last_pass_ago}\n")
    return now


def test_no_heartbeat_never_restarts_the_wait(box: Box) -> None:
    box.release("ae-v0.13.0.2")
    _wait_state(box, since_ago=7300, last_pass_ago=None)
    result = box.run(FAKE_BUSY="1")
    assert box.tag() == "ae-v0.13.0.2", result.stderr
    assert "starts again" not in result.stdout + result.stderr
    assert (box.state / "last-pass").read_text().strip().isdigit()


def test_a_pause_longer_than_the_gap_starts_the_wait_again(box: Box) -> None:
    box.release("ae-v0.13.0.2")
    now = _wait_state(box, since_ago=3 * 3600, last_pass_ago=40 * 60)
    result = box.run(FAKE_BUSY="1")
    assert result.returncode == 0, result.stderr
    assert "to=ae-v0.13.0.2 result=deferred" in box.last()
    assert "no pass for 40 minutes" in result.stdout + result.stderr
    assert "starts again" in result.stdout + result.stderr
    assert box.tag() == "ae-v0.13.0.1"
    key, new_since = (box.state / "deferred").read_text().split()
    assert key == "ae-v0.13.0.1" and abs(int(new_since) - now) < 60
    assert abs(int((box.state / "last-pass").read_text()) - now) < 60


def test_regular_passes_keep_the_clock_past_the_limit(box: Box) -> None:
    box.release("ae-v0.13.0.2")
    _wait_state(box, since_ago=7300, last_pass_ago=5 * 60)
    result = box.run(FAKE_BUSY="1")
    assert result.returncode == 0, result.stderr
    assert box.tag() == "ae-v0.13.0.2"
    assert "going ahead anyway" in result.stdout + result.stderr


def test_a_failing_pass_still_beats_the_heartbeat(box: Box) -> None:
    box.state.mkdir()
    box.git(box.box, "remote", "set-url", "origin", str(box.tmp / "gone.git"))
    old = int(time.time()) - 40 * 60
    (box.state / "last-pass").write_text(f"{old}\n")
    failed = box.run()
    assert "result=fetch-failed" in box.last(), failed.stderr
    assert int((box.state / "last-pass").read_text()) > old + 39 * 60

    # The next pass sees a short gap, so a long wait is not restarted.
    box.git(box.box, "remote", "set-url", "origin", str(box.tmp / "origin.git"))
    box.release("ae-v0.13.0.2")
    _wait_state(box, since_ago=7300, last_pass_ago=None)
    (box.state / "last-pass").write_text(f"{int(time.time()) - 5 * 60}\n")
    result = box.run(FAKE_BUSY="1")
    assert "starts again" not in result.stdout + result.stderr


def test_the_host_retry_wait_restarts_after_a_pause_too(box: Box) -> None:
    _release_touching(box, "upstream.lock", "ae-v0.13.0.2")
    broken = box.run(AE_AUTO_UPDATE_HOST_REBUILD=str(box.bin / "rebuild-host-broken"))
    assert broken.returncode == 1
    now = int(time.time())
    (box.state / "deferred-host").write_text(f"ae-v0.13.0.2 {now - 3 * 3600}\n")
    (box.state / "last-pass").write_text(f"{now - 40 * 60}\n")
    busy = box.run(FAKE_BUSY="1")
    assert "starts again" in busy.stdout + busy.stderr
    key, new_since = (box.state / "deferred-host").read_text().split()
    assert key == "ae-v0.13.0.2" and abs(int(new_since) - now) < 60
    assert (box.state / "host-pending").exists()


@pytest.mark.parametrize("value", ["abc", "600"])
def test_a_bad_pause_gap_falls_back_to_thirty_minutes(box: Box, value: str) -> None:
    box.release("ae-v0.13.0.2")
    _wait_state(box, since_ago=3 * 3600, last_pass_ago=40 * 60)
    result = box.run(FAKE_BUSY="1", AE_AUTO_UPDATE_PAUSE_GAP=value)
    out = result.stdout + result.stderr
    assert "using 1800" in out
    assert "starts again" in out, "the 40 minute gap still counts against 1800"


def test_a_pass_that_fails_after_the_limit_keeps_the_clock(box: Box) -> None:
    box.release("ae-v0.13.0.2")
    since = int(time.time()) - 7300
    (box.state).mkdir(exist_ok=True)
    (box.state / "deferred").write_text(f"ae-v0.13.0.1 {since}\n")
    unpublished = box.run(FAKE_BUSY="1", FAKE_DOCKER_PULL="missing")
    assert unpublished.returncode == 0, unpublished.stderr
    assert "result=waiting" in box.last()
    assert (box.state / "deferred").read_text().split() == ["ae-v0.13.0.1", str(since)]

    published = box.run(FAKE_BUSY="1")
    assert published.returncode == 0, published.stderr
    assert box.tag() == "ae-v0.13.0.2", "no second two-hour wait"


def test_a_turn_started_during_the_pull_defers_before_the_tag_is_written(box: Box) -> None:
    box.release("ae-v0.13.0.2")
    plan = box.tmp / "busy-plan"
    plan.write_text("0\n2\n")
    result = box.run(FAKE_BUSY_PLAN=str(plan))
    assert result.returncode == 0, result.stderr
    assert "result=deferred" in box.last()
    assert f"docker pull --quiet {IMAGE}:ae-v0.13.0.2" in box.calls()
    assert box.compose_ups() == []
    assert box.tag() == "ae-v0.13.0.1"
    assert not (box.state / "previous").exists()


def test_a_busy_clone_still_fails_loudly_while_sessions_are_mid_turn(box: Box) -> None:
    box.release("ae-v0.13.0.2")
    box.git(box.box, "checkout", "--quiet", "-b", "someone-else")
    result = box.run(FAKE_BUSY="2")
    assert result.returncode == 1
    assert "result=busy" in box.last()


def test_a_count_that_hangs_times_out_and_deploys_as_before(box: Box) -> None:
    box.release("ae-v0.13.0.2")
    real_sleep = shutil.which("sleep")
    assert real_sleep
    hang = box.bin / "busy-hangs"
    hang.write_text(f"#!/usr/bin/env bash\nexec {real_sleep} 30\n")
    hang.chmod(0o755)
    started = time.monotonic()
    result = box.run(AE_AUTO_UPDATE_BUSY=str(hang), AE_AUTO_UPDATE_BUSY_TIMEOUT="1")
    assert result.returncode == 0, result.stderr
    assert time.monotonic() - started < 20
    assert box.tag() == "ae-v0.13.0.2"
    assert "could not count the sessions mid-turn" in result.stdout + result.stderr


def test_a_zero_limit_deploys_at_once_with_sessions_mid_turn(box: Box) -> None:
    # deploy/README.md, Day to day: the by-hand pass that does not wait.
    box.release("ae-v0.13.0.2")
    result = box.run(FAKE_BUSY="3", AE_AUTO_UPDATE_MAX_DEFER="0")
    assert result.returncode == 0, result.stderr
    assert box.tag() == "ae-v0.13.0.2"
    assert not (box.state / "deferred").exists()


@pytest.mark.parametrize("busy", ["not a number", "-1"])
def test_a_count_that_is_not_a_number_deploys_as_before(box: Box, busy: str) -> None:
    box.release("ae-v0.13.0.2")
    result = box.run(FAKE_BUSY=busy)
    assert result.returncode == 0, result.stderr
    assert box.tag() == "ae-v0.13.0.2"
    assert "could not count the sessions mid-turn" in result.stdout + result.stderr


def test_a_failing_count_deploys_as_before(box: Box) -> None:
    box.release("ae-v0.13.0.2")
    result = box.run(AE_AUTO_UPDATE_BUSY=str(box.bin / "dump-broken"))
    assert result.returncode == 0, result.stderr
    assert box.tag() == "ae-v0.13.0.2"
    assert "could not count the sessions mid-turn" in result.stdout + result.stderr


def test_the_default_count_asks_postgres_for_running_sessions(box: Box) -> None:
    box.release("ae-v0.13.0.2")
    result = box.run(AE_AUTO_UPDATE_BUSY="", FAKE_PSQL="3")
    assert result.returncode == 0, result.stderr
    assert "3 session(s) mid-turn" in box.last()
    (query,) = [c for c in box.calls() if "psql" in c]
    assert " exec -T postgres " in query
    assert "live_status IN (2, 3)" in query and "runner_last_seen >=" in query
    assert "- 300" in query
    assert box.tag() == "ae-v0.13.0.1"


def test_a_pending_host_rebuild_waits_for_quiet_too(box: Box) -> None:
    _release_touching(box, "upstream.lock", "ae-v0.13.0.2")
    broken = box.run(AE_AUTO_UPDATE_HOST_REBUILD=str(box.bin / "rebuild-host-broken"))
    assert broken.returncode == 1
    pending = box.state / "host-pending"
    assert pending.exists()

    box.calls_log.unlink()
    log_before = (box.state / "log").read_text()
    busy = box.run(FAKE_BUSY="1")
    assert busy.returncode == 0, busy.stderr
    out = busy.stdout + busy.stderr
    assert "result=deferred" in out and "host rebuild pending" in out
    assert (box.state / "log").read_text() == log_before, "a deferral does not grow the log"
    assert not any(c.startswith(("rebuild-host", "systemctl")) for c in box.calls())
    assert pending.exists()
    assert (box.state / "deferred-host").read_text().split()[0] == "ae-v0.13.0.2"

    box.calls_log.unlink(missing_ok=True)
    quiet = box.run()
    assert quiet.returncode == 0, quiet.stderr
    assert "systemctl --user restart ae-omnigent-host.service" in box.calls()
    assert not pending.exists()
    assert not (box.state / "deferred-host").exists()


def _rebuild_heads(box: Box) -> list[str]:
    return [c.split("HEAD=", 1)[1] for c in box.calls() if c.startswith("rebuild-host HEAD=")]


def test_a_pending_rebuild_never_builds_from_a_newer_checkout(box: Box) -> None:
    # Review of 2026-09-24: the retry ran at the end of the pass, after the
    # fast-forward, so a pass waiting for tag B's image rebuilt the host from
    # B's code while the server still ran A, and cleared host-pending.
    broken = {"AE_AUTO_UPDATE_HOST_REBUILD": str(box.bin / "rebuild-host-broken")}
    at_head = {"AE_AUTO_UPDATE_HOST_REBUILD": str(box.bin / "rebuild-host-at-head")}
    pending = box.state / "host-pending"
    _release_touching(box, "upstream.lock", "ae-v0.13.0.2")
    assert box.run(**broken).returncode == 1
    assert pending.read_text().strip() == "ae-v0.13.0.2"

    # B is merged, its image not built yet. The retry still fails, and the
    # pass fast-forwards to B after it.
    _release_touching(box, "upstream.lock", "ae-v0.13.0.3")
    b = box.git(box.dev, "rev-parse", "ae-v0.13.0.3^{commit}")
    waiting = box.run(FAKE_DOCKER_PULL="missing", **broken)
    assert waiting.returncode == 1, "a failed retry fails a pass that deploys nothing"
    assert "result=host-failed" in box.last()
    assert box.git(box.box, "rev-parse", "HEAD") == b
    assert box.tag() == "ae-v0.13.0.2"

    # The rebuild works again, but the checkout now carries B's host code
    # and the server runs A: no rebuild, host-pending stays, the log says why.
    box.calls_log.unlink()
    deferred = box.run(FAKE_DOCKER_PULL="missing", **at_head)
    assert deferred.returncode == 0, deferred.stderr
    assert _rebuild_heads(box) == [], "never built from B while A runs"
    assert pending.exists()
    log = (box.state / "log").read_text()
    assert "result=host-deferred" in log and "not the running tag ae-v0.13.0.2" in log
    assert "result=waiting" in box.last()

    # B's image lands: the deploy rebuilds from B and clears the marker.
    box.calls_log.unlink()
    assert box.run(**at_head).returncode == 0
    assert box.tag() == "ae-v0.13.0.3"
    assert _rebuild_heads(box) == [b]
    assert not pending.exists()


def test_a_pending_rebuild_runs_before_the_fast_forward(box: Box) -> None:
    at_head = {"AE_AUTO_UPDATE_HOST_REBUILD": str(box.bin / "rebuild-host-at-head")}
    box.state.mkdir()
    (box.state / "host-pending").write_text("ae-v0.13.0.1\n")
    a = box.git(box.box, "rev-parse", "HEAD")
    _release_touching(box, "upstream.lock", "ae-v0.13.0.2")
    result = box.run(FAKE_DOCKER_PULL="missing", **at_head)
    assert result.returncode == 0, result.stderr
    assert _rebuild_heads(box) == [a], "built from the running tag's checkout, not B's"
    assert not (box.state / "host-pending").exists()
    assert "result=host-ok" in (box.state / "log").read_text()
    assert box.git(box.box, "rev-parse", "HEAD") != a, "then the pass moves on to B"


def test_a_fetch_failure_does_not_block_a_pending_rebuild(box: Box) -> None:
    box.state.mkdir()
    (box.state / "host-pending").write_text("ae-v0.13.0.1\n")
    box.git(box.box, "remote", "set-url", "origin", str(box.tmp / "gone.git"))
    result = box.run()
    assert result.returncode == 1
    assert "result=fetch-failed" in box.last()
    assert "rebuild-host " in box.calls()
    assert "systemctl --user restart ae-omnigent-host.service" in box.calls()
    assert not (box.state / "host-pending").exists()
    assert "result=host-ok" in (box.state / "log").read_text()


def test_a_failed_host_restart_is_pending_too(box: Box) -> None:
    (box.bin / "systemctl").write_text(FAILING)
    _release_touching(box, "upstream.lock", "ae-v0.13.0.2")
    assert box.run().returncode == 1
    assert (box.state / "host-pending").read_text().strip() == "ae-v0.13.0.2"


def _dumps(box: Box) -> list[Path]:
    return sorted((box.state / "dumps").glob("omnigent-*.sql.gz"))


def test_an_upstream_move_dumps_the_database_before_up(box: Box) -> None:
    dumps = box.state / "dumps"
    dumps.mkdir(parents=True)
    for day in ("01", "02", "03"):
        (dumps / f"omnigent-2026-01-{day}T000000Z-ae-v0.12.0.1.sql.gz").write_text("old")
    _release_touching(box, "upstream.lock", "ae-v0.14.0.1")
    result = box.run()
    assert result.returncode == 0, result.stderr
    assert box.tag() == "ae-v0.14.0.1"
    calls = box.calls()
    dump_at = next(i for i, c in enumerate(calls) if " exec -T postgres " in c)
    up_at = next(i for i, c in enumerate(calls) if " up -d" in c)
    assert dump_at < up_at, "the dump comes before the new image starts"
    # The same command shape as deploy/backup-omnigent.sh, through compose.sh.
    assert f"--env-file {box.settings}" in calls[dump_at]
    assert "pg_dump --no-owner --no-privileges" in calls[dump_at]
    names = [p.name for p in _dumps(box)]
    assert len(names) == 3, "the newest three are kept"
    assert "omnigent-2026-01-01T000000Z-ae-v0.12.0.1.sql.gz" not in names
    newest = _dumps(box)[-1]
    assert newest.name.endswith("-ae-v0.13.0.1.sql.gz"), "named for the tag it belongs to"
    assert subprocess.run(
        ["gzip", "-dc", str(newest)], capture_output=True, text=True, check=True
    ).stdout.startswith("CREATE TABLE fake_dump")
    assert newest.stat().st_mode & 0o777 == 0o600
    assert dumps.stat().st_mode & 0o777 == 0o700
    assert not list(dumps.glob("*.tmp"))


def test_an_ae_only_release_takes_no_dump(box: Box) -> None:
    _release_touching(box, "packages/ae_omni_extension/web/src/main.ts", "ae-v0.13.0.2")
    assert box.run().returncode == 0
    assert box.tag() == "ae-v0.13.0.2"
    assert not any(" exec " in c for c in box.calls())
    assert not (box.state / "dumps").exists()


@pytest.mark.parametrize(
    ("env", "reason"),
    [
        ({"FAKE_DOCKER_EXEC": "fail"}, "exited non-zero"),
        ({"FAKE_DOCKER_EXEC": "empty"}, "is empty"),
        ({"AE_AUTO_UPDATE_DUMP": "dump-broken"}, "exited non-zero"),
    ],
    ids=["compose-exec-fails", "empty-dump", "override-fails"],
)
def test_a_failed_dump_blocks_the_deploy(box: Box, env: dict[str, str], reason: str) -> None:
    env = {k: str(box.bin / v) if k == "AE_AUTO_UPDATE_DUMP" else v for k, v in env.items()}
    _release_touching(box, "upstream.lock", "ae-v0.14.0.1")
    result = box.run(**env)
    assert result.returncode == 1
    assert "AUTO-UPDATE dump-failed" in result.stderr
    last = box.last()
    assert "result=dump-failed" in last and reason in last and "nothing was deployed" in last
    assert "result=dump-failed" in (box.state / "log").read_text()
    assert box.tag() == "ae-v0.13.0.1"
    assert box.compose_ups() == []
    assert not (box.state / "previous").exists()
    assert not (box.state / "failed").exists(), "the tag is tried again on the next pass"
    assert _dumps(box) == [] and not list((box.state / "dumps").glob("*.tmp"))
    if "AE_AUTO_UPDATE_DUMP" in env:
        assert "dump-broken " in box.calls()


def test_hold_upstream_holds_an_upstream_move_and_lets_ae_only_tags_through(box: Box) -> None:
    box.state.mkdir()
    hold = box.state / "hold-upstream"
    hold.touch()

    # Same upstream part (0.13.0): deploys as usual.
    box.release("ae-v0.13.0.2")
    assert box.run().returncode == 0
    assert box.tag() == "ae-v0.13.0.2"

    # The upstream part moves (0.13.0 to 0.14.0): held, nothing touched.
    box.calls_log.unlink()
    head_before = box.git(box.box, "rev-parse", "HEAD")
    _release_touching(box, "upstream.lock", "ae-v0.14.0.1")
    held = box.run()
    assert held.returncode == 0, held.stderr
    assert box.tag() == "ae-v0.13.0.2"
    last = box.last()
    assert "to=ae-v0.14.0.1 result=held" in last
    assert "from 0.13.0 to 0.14.0" in last and f"rm {hold}" in last
    assert box.calls() == [], "no pull, no dump, no deploy"
    assert box.git(box.box, "rev-parse", "HEAD") == head_before, "the checkout stays put"
    assert not (box.state / "failed").exists()
    assert "result=held" not in (box.state / "log").read_text(), "a hold does not grow the log"

    # Lifted: the held tag deploys on the next pass, dump first.
    hold.unlink()
    assert box.run().returncode == 0
    assert box.tag() == "ae-v0.14.0.1"
    assert len(_dumps(box)) == 1


def test_a_held_pass_still_retries_a_pending_host_rebuild(box: Box) -> None:
    box.state.mkdir()
    (box.state / "hold-upstream").touch()
    (box.state / "host-pending").write_text("ae-v0.13.0.1\n")
    _release_touching(box, "upstream.lock", "ae-v0.14.0.1")
    assert box.run().returncode == 0
    assert "rebuild-host " in box.calls()
    assert not (box.state / "host-pending").exists()
    assert "result=held" in box.last()


def test_runbook_documents_the_hold_and_the_dump_restore() -> None:
    text = (ROOT / "deploy" / "README.md").read_text()
    assert "~/.omnigent-ae-auto-update/hold-upstream" in text
    assert "~/.omnigent-ae-auto-update/dumps" in text
    assert "host-pending" in text


def test_overlapping_pass_exits_at_once(box: Box) -> None:
    box.release("ae-v0.13.0.2")
    box.state.mkdir()
    with (box.state / "lock").open("w") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        result = box.run()
    assert result.returncode == 0
    assert "another auto-update pass is running" in result.stdout
    assert box.tag() == "ae-v0.13.0.1"
    assert box.calls() == []


def test_script_never_uses_sudo_or_reads_the_secrets_itself() -> None:
    code = [line for line in SCRIPT.read_text().splitlines() if not line.lstrip().startswith("#")]
    body = "\n".join(code)
    assert "sudo" not in body
    assert "/secure/env" not in body
    assert "flock -n" in body


# ── the deploy hold (2026-10-19) ───────────────────────────────────────────


def _held(seconds: int = 3600, by: str = "owner") -> tuple[str, str]:
    until = int(time.time()) + seconds
    return f"held {until} {by}", time.strftime("%H:%MZ", time.gmtime(until))


def test_a_new_tag_while_held_records_on_hold_and_moves_nothing(box: Box) -> None:
    box.release("ae-v0.13.0.2")
    head = box.git(box.box, "rev-parse", "HEAD")
    box.state.mkdir()
    (box.state / "deferred").write_text(f"ae-v0.13.0.1 {int(time.time()) - 600}\n")
    line, ends = _held()
    result = box.run(FAKE_HOLD=line)
    assert result.returncode == 0, result.stderr
    last = box.last()
    assert "from=ae-v0.13.0.1 to=none result=on-hold" in last
    assert f"held until {ends} by owner" in last
    assert box.tag() == "ae-v0.13.0.1"
    assert box.calls() == [], "no pull, no compose, no rebuild"
    assert box.git(box.box, "rev-parse", "HEAD") == head, "the checkout stays put"
    assert box.git(box.box, "tag", "--list", "ae-v0.13.0.2") == "", "not even fetched"
    assert not (box.state / "deferred").exists(), "the busy wait starts fresh after"
    assert not (box.state / "previous").exists()
    assert not (box.state / "log").exists(), "a hold does not grow the log"


def test_a_held_pass_leaves_a_pending_host_rebuild_alone(box: Box) -> None:
    box.state.mkdir()
    (box.state / "host-pending").write_text("ae-v0.13.0.1\n")
    (box.state / "deferred-host").write_text(f"ae-v0.13.0.1 {int(time.time()) - 600}\n")
    result = box.run(FAKE_HOLD=_held()[0])
    assert result.returncode == 0, result.stderr
    assert "result=on-hold" in box.last()
    assert not any(c.startswith(("rebuild-host", "systemctl")) for c in box.calls())
    assert (box.state / "host-pending").exists()
    assert not (box.state / "deferred-host").exists()

    # Lifted: the next pass rebuilds as before.
    assert box.run().returncode == 0
    assert "rebuild-host " in box.calls()
    assert not (box.state / "host-pending").exists()


@pytest.mark.parametrize(
    ("answer", "says"),
    [
        ("exit:2", "(exit 2)"),
        ("garbage", "(got: garbage)"),
        ("held soon owner", "(got: held soon owner)"),
        ("held 123", "(got: held 123)"),
    ],
)
def test_a_hold_that_cannot_be_read_deploys_with_a_note(box: Box, answer: str, says: str) -> None:
    box.release("ae-v0.13.0.2")
    result = box.run(FAKE_HOLD=answer)
    assert result.returncode == 0, result.stderr
    out = result.stdout + result.stderr
    assert f"could not read the deploy hold {says}; going ahead as if there were none" in out
    assert box.tag() == "ae-v0.13.0.2"
    assert "result=ok" in box.last()


def test_a_hold_read_that_hangs_times_out_and_deploys(box: Box) -> None:
    box.release("ae-v0.13.0.2")
    real_sleep = shutil.which("sleep")
    assert real_sleep
    hang = box.bin / "hold-hangs"
    hang.write_text(f"#!/usr/bin/env bash\nexec {real_sleep} 30\n")
    hang.chmod(0o755)
    started = time.monotonic()
    result = box.run(AE_AUTO_UPDATE_DEPLOY_HOLD=str(hang), AE_AUTO_UPDATE_BUSY_TIMEOUT="1")
    assert result.returncode == 0, result.stderr
    assert time.monotonic() - started < 20
    assert "could not read the deploy hold (no answer in 1s)" in result.stdout + result.stderr
    assert box.tag() == "ae-v0.13.0.2"


@pytest.mark.parametrize("answer", ["none", "expired"])
def test_no_hold_or_an_expired_one_deploys_as_before(box: Box, answer: str) -> None:
    box.release("ae-v0.13.0.2")
    line = _held(-60)[0] if answer == "expired" else answer
    result = box.run(FAKE_HOLD=line)
    assert result.returncode == 0, result.stderr
    assert box.tag() == "ae-v0.13.0.2"
    assert "result=ok" in box.last()
    assert "deploy hold" not in result.stdout + result.stderr


def test_a_hold_set_during_the_pull_stops_before_the_tag_is_written(box: Box) -> None:
    box.release("ae-v0.13.0.2")
    box.state.mkdir()
    # Sessions busy past the limit: the second wait goes ahead, and the hold
    # still stops the pass, taking the old clock with it.
    (box.state / "deferred").write_text(f"ae-v0.13.0.1 {int(time.time()) - 7300}\n")
    (box.state / "deferred-host").write_text(f"ae-v0.13.0.1 {int(time.time()) - 600}\n")
    line, ends = _held(7200, "member")
    plan = box.tmp / "hold-plan"
    plan.write_text(f"none\n{line}\n")
    result = box.run(FAKE_HOLD_PLAN=str(plan), FAKE_BUSY="1")
    assert result.returncode == 0, result.stderr
    last = box.last()
    assert "to=ae-v0.13.0.2 result=on-hold" in last
    assert f"ae-v0.13.0.2 deploys after the hold set by member ends at {ends}" in last
    assert f"docker pull --quiet {IMAGE}:ae-v0.13.0.2" in box.calls()
    assert box.compose_ups() == []
    assert box.tag() == "ae-v0.13.0.1"
    assert not (box.state / "previous").exists()
    assert not (box.state / "deferred").exists()
    assert not (box.state / "deferred-host").exists(), "the second check clears both clocks"

    # Lifted: the next pass deploys the tag it already pulled.
    done = box.run()
    assert done.returncode == 0, done.stderr
    assert box.tag() == "ae-v0.13.0.2"


def test_after_a_hold_busy_sessions_get_a_fresh_two_hours(box: Box) -> None:
    box.release("ae-v0.13.0.2")
    box.state.mkdir()
    (box.state / "deferred").write_text(f"ae-v0.13.0.1 {int(time.time()) - 7300}\n")
    assert box.run(FAKE_HOLD=_held()[0], FAKE_BUSY="1").returncode == 0
    assert "result=on-hold" in box.last()

    now = int(time.time())
    result = box.run(FAKE_BUSY="1")
    assert result.returncode == 0, result.stderr
    assert "to=ae-v0.13.0.2 result=deferred" in box.last()
    assert "going ahead anyway" not in result.stdout + result.stderr
    key, since = (box.state / "deferred").read_text().split()
    assert key == "ae-v0.13.0.1" and abs(int(since) - now) < 60
    assert box.tag() == "ae-v0.13.0.1"


def test_the_default_hold_read_asks_the_server_through_compose(box: Box) -> None:
    box.release("ae-v0.13.0.2")
    line, ends = _held()
    result = box.run(AE_AUTO_UPDATE_DEPLOY_HOLD="", FAKE_HOLD_EXEC=line)
    assert result.returncode == 0, result.stderr
    assert f"held until {ends} by owner" in box.last(), "the last stdout line counts"
    (read,) = [c for c in box.calls() if "deploy_hold" in c]
    assert " exec -T omnigent python -m ae_omni_policies.deploy_hold" in read
    assert box.tag() == "ae-v0.13.0.1"


# ── the user units ─────────────────────────────────────────────────────────


def _unit(path: Path) -> configparser.RawConfigParser:
    parser = configparser.RawConfigParser(strict=True, interpolation=None)
    parser.optionxform = str  # type: ignore[assignment,method-assign]
    parser.read_string(path.read_text())
    return parser


def test_user_units_are_the_expected_set() -> None:
    assert sorted(p.name for p in USER_UNITS.iterdir()) == [
        "ae-host-config-sync@.service",
        "ae-host-config-sync@.timer",
        "ae-omnigent-auto-update.service",
        "ae-omnigent-auto-update.timer",
        "ae-omnigent-box-health.service",
        "ae-omnigent-box-health.timer",
        "ae-omnigent-host-container@.service",
        "ae-omnigent-host-relink.service",
        "ae-omnigent-limits.service",
        "ae-omnigent-limits.timer",
        "ae-omnigent-secrets-meta.service",
        "ae-omnigent-secrets-meta.timer",
    ]


def test_limits_relay_units_run_the_module_every_five_minutes() -> None:
    # The plan limits relay (docs/PLAN-LIMITS.md, 2026-09-24).
    s = _unit(USER_UNITS / "ae-omnigent-limits.service")
    assert s.get("Service", "Type") == "oneshot"
    assert s.get("Service", "ExecStart") == (
        "%h/.omnigent-host/venv/bin/python -m ae_omni_policies.limits_relay"
    )
    assert s.get("Service", "Environment").startswith(
        "PATH=%h/.omnigent-host/venv/bin:%h/.local/node/bin:"
    )
    assert s.get("Service", "EnvironmentFile") == "-%h/deploy/omnigent-ae/config/host.env"
    assert not s.has_option("Service", "User"), "a user unit names no User="
    assert not s.has_section("Install"), "the timer starts it"
    path = USER_UNITS / "ae-omnigent-limits.timer"
    t = _unit(path)
    assert t.get("Timer", "OnCalendar") == "*:0/5"
    assert t.get("Install", "WantedBy") == "timers.target"
    assert "local time" in path.read_text()


def test_box_health_relay_units_run_the_module_every_five_minutes() -> None:
    # The box health monitor (deploy/README.md "Box health relay", 2026-10-28).
    s = _unit(USER_UNITS / "ae-omnigent-box-health.service")
    assert s.get("Service", "Type") == "oneshot"
    assert s.get("Service", "ExecStart") == (
        "%h/.omnigent-host/venv/bin/python -m ae_omni_policies.box_health_relay"
    )
    assert s.get("Service", "EnvironmentFile") == "-%h/deploy/omnigent-ae/config/host.env"
    assert "/usr/bin" in s.get("Service", "Environment")
    assert (s.get("Service", "Nice"), s.get("Service", "IOSchedulingClass")) == ("10", "idle")
    assert s.get("Service", "TimeoutStartSec") == "4min"
    assert not s.has_option("Service", "User"), "a user unit names no User="
    assert not s.has_section("Install"), "the timer starts it"
    path = USER_UNITS / "ae-omnigent-box-health.timer"
    t = _unit(path)
    assert t.get("Timer", "OnCalendar") == "*:0/5"
    assert t.get("Timer", "RandomizedDelaySec") == "30"
    assert t.get("Timer", "OnBootSec") == "2min"
    assert t.get("Install", "WantedBy") == "timers.target"
    assert "local time" in path.read_text()


def test_secrets_meta_relay_units_run_the_module_daily() -> None:
    # The Server secrets list's expiry dates (deploy/README.md, 2026-10-23).
    s = _unit(USER_UNITS / "ae-omnigent-secrets-meta.service")
    assert s.get("Service", "Type") == "oneshot"
    assert s.get("Service", "ExecStart") == (
        "%h/.omnigent-host/venv/bin/python -m ae_omni_policies.secrets_relay"
    )
    # az lives in ~/.local/bin, which a systemd user session does not have.
    assert "%h/.local/bin" in s.get("Service", "Environment")
    assert s.get("Service", "EnvironmentFile") == "-%h/deploy/omnigent-ae/config/host.env"
    assert not s.has_option("Service", "User"), "a user unit names no User="
    assert not s.has_section("Install"), "the timer starts it"
    path = USER_UNITS / "ae-omnigent-secrets-meta.timer"
    t = _unit(path)
    assert t.get("Timer", "OnCalendar") == "*-*-* 06:15:00"
    assert t.get("Timer", "Persistent") == "true"
    assert t.get("Install", "WantedBy") == "timers.target"
    assert "local time" in path.read_text()


def test_auto_update_service_is_a_user_oneshot_running_the_script() -> None:
    s = _unit(USER_UNITS / "ae-omnigent-auto-update.service")
    assert s.get("Unit", "Description")
    assert s.get("Service", "Type") == "oneshot"
    assert s.get("Service", "ExecStart") == "%h/deploy/omnigent-ae/deploy/auto-update.sh"
    assert s.get("Service", "Environment").startswith("PATH=%h/.local/bin:")
    assert s.has_option("Service", "TimeoutStartSec")
    assert not s.has_option("Service", "User"), "a user unit names no User="
    assert not s.has_section("Install"), "the timer starts it"


def test_auto_update_timer_every_ten_minutes_with_jitter() -> None:
    path = USER_UNITS / "ae-omnigent-auto-update.timer"
    t = _unit(path)
    assert t.get("Timer", "OnCalendar") == "*:0/10"
    assert 0 < int(t.get("Timer", "RandomizedDelaySec")) <= 120
    assert t.get("Install", "WantedBy") == "timers.target"
    assert "local time" in path.read_text(), "the schedule comment names the local time"


@pytest.mark.parametrize("unit", sorted(USER_UNITS.iterdir()), ids=lambda p: p.name)
def test_user_units_have_no_tabs_or_trailing_space(unit: Path) -> None:
    for line in unit.read_text().splitlines():
        assert "\t" not in line and not line.endswith(" "), f"{unit.name}: {line!r}"


# ── A busy checkout (2026-10-13, the move to a deploy clone) ───────────────


def test_a_checkout_on_another_branch_is_not_reset(box: Box) -> None:
    box.git(box.box, "checkout", "--quiet", "-b", "work")
    head = box.git(box.box, "rev-parse", "HEAD")
    box.release("ae-v0.13.0.2")
    result = box.run()
    assert result.returncode == 1
    assert "result=busy" in box.last()
    assert "is on work, not main" in box.last()
    assert box.git(box.box, "symbolic-ref", "--short", "HEAD") == "work"
    assert box.git(box.box, "rev-parse", "HEAD") == head
    assert box.tag() == "ae-v0.13.0.1"
    assert box.compose_ups() == []
    assert not (box.state / "failed").exists(), "a busy pass never marks the tag failed"

    box.git(box.box, "checkout", "--quiet", "main")
    assert box.run().returncode == 0
    assert box.tag() == "ae-v0.13.0.2"


def test_an_edited_tracked_file_is_kept_and_blocks_the_deploy(box: Box) -> None:
    (box.box / "change").write_text("someone's work in progress")
    box.release("ae-v0.13.0.2")
    result = box.run()
    assert result.returncode == 1
    assert "result=busy" in box.last()
    assert "M change" in box.last()
    assert (box.box / "change").read_text() == "someone's work in progress"
    assert box.compose_ups() == []


def test_an_untracked_file_does_not_make_the_checkout_busy(box: Box) -> None:
    (box.box / "scratch.txt").write_text("not tracked")
    box.release("ae-v0.13.0.2")
    assert box.run().returncode == 0
    assert box.tag() == "ae-v0.13.0.2"


def test_a_busy_checkout_with_nothing_to_deploy_stays_a_noop(box: Box) -> None:
    box.git(box.box, "checkout", "--quiet", "-b", "work")
    assert box.run().returncode == 0
    assert "result=noop" in box.last()
