"""deploy/host-relink.sh and its user unit, without a box (2026-10-20).

The script runs with fakes for everything it touches: the server's
host_relink CLI (AE_RELINK_CLI), the host's journal (AE_RELINK_JOURNAL, a
file played once, then the end of the stream, which ends the watcher), the
server's hosts table (AE_RELINK_HOST_ONLINE), `omnigent login`
(AE_RELINK_OMNIGENT, one outcome per attempt from a plan) and systemctl
(AE_RELINK_SYSTEMCTL). Nothing here logs in, restarts a host or writes the
server's files.

The journal lines are the shapes upstream's omnigent/host/connect.py prints:
one at the first rejection, then one at every 30th, carrying the streak.
"""

from __future__ import annotations

import configparser
import os
import signal
import stat
import subprocess
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "deploy" / "host-relink.sh"
UNIT = ROOT / "deploy" / "systemd" / "user" / "ae-omnigent-host-relink.service"

FIRST = (
    "⚠ Connection refused (HTTP 403): the host tunnel was rejected. Retrying; this usually "
    "means the VPN or network dropped."
)
CONNECTED = "✓ Connected as 'example-box' (bbf29d9e), 0 live runner(s). Listening for sessions"


def every_30th(n: int) -> str:
    return (
        "⚠ Connection refused (HTTP 403): the host tunnel was rejected. The server has "
        f"rejected it {n} times in a row; this is no longer a transient network blip."
    )


# The server CLI: records each call and what reached its stdin; `take` answers
# the next line of $FAKE_TAKE_PLAN, else none; `push` fails when FAKE_PUSH=fail.
FAKE_CLI = """#!/usr/bin/env bash
printf 'cli %s\\n' "$*" >>"$FAKE_CALLS"
cat >>"$FAKE_STDIN"
case "$1" in
  take)
    if [ -s "$FAKE_TAKE_PLAN" ]; then head -n 1 "$FAKE_TAKE_PLAN"; sed -i 1d "$FAKE_TAKE_PLAN"
    else echo none; fi ;;
  push) echo "import noise"; [ "${FAKE_PUSH:-ok}" = ok ] && echo ok || { echo failed; exit 1; } ;;
  *) echo ok ;;
esac
"""

# `omnigent login`: the next outcome in $FAKE_LOGIN_PLAN (ok, expired, nolink,
# or wait: print the link and wait 30 s), expired once the plan runs out.
FAKE_OMNIGENT = """#!/usr/bin/env bash
printf 'omnigent %s\\n' "$*" >>"$FAKE_CALLS"
cat >>"$FAKE_STDIN"
outcome=expired
if [ -s "$FAKE_LOGIN_PLAN" ]; then outcome="$(head -n 1 "$FAKE_LOGIN_PLAN")"; sed -i 1d "$FAKE_LOGIN_PLAN"; fi
n="$(grep -c '^omnigent ' "$FAKE_CALLS")"
[ "$outcome" = nolink ] && exit 1
echo "Opening browser for login: $2/auth/login?ticket=T$n"
echo "Waiting for authentication..."
if [ "$outcome" = wait ]; then echo "$$" >>"$FAKE_PIDS"; exec sleep 30; fi
if [ "$outcome" = ok ]; then echo "Logged in as reid@example.com"; exit 0; fi
echo "Error: Login ticket expired or was rejected. Please try again."
exit 1
"""

# systemctl: `is-active` prints $FAKE_ACTIVE (active by default); `restart`
# fails when FAKE_RESTART=fail.
FAKE_SYSTEMCTL = """#!/usr/bin/env bash
printf 'systemctl %s\\n' "$*" >>"$FAKE_CALLS"
case "$2" in
  is-active) state="${FAKE_ACTIVE:-active}"; echo "$state"; [ "$state" = active ] ;;
  restart) [ "${FAKE_RESTART:-ok}" = ok ] ;;
esac
"""

FAKE_ONLINE = """#!/usr/bin/env bash
printf 'online-check\\n' >>"$FAKE_CALLS"
[ "${FAKE_ONLINE:-offline}" = fail ] && exit 1
echo "${FAKE_ONLINE:-offline}"
"""


class Box:
    """A temp dir with the fakes, and the environment that points the script at them."""

    def __init__(self, tmp: Path) -> None:
        self.tmp = tmp
        self.calls = tmp / "calls"
        self.stdin = tmp / "stdin"
        self.pids = tmp / "pids"
        self.journal = tmp / "journal"
        self.take_plan = tmp / "take-plan"
        self.login_plan = tmp / "login-plan"
        self.state = tmp / "state"
        for path in (self.calls, self.stdin, self.pids, self.take_plan, self.login_plan):
            path.write_text("")
        bin_dir = tmp / "bin"
        bin_dir.mkdir()
        journal = f'#!/usr/bin/env bash\n[ -f "{self.journal}" ] && cat "{self.journal}"\nexit 0\n'
        for name, body in (
            ("cli", FAKE_CLI),
            ("omnigent", FAKE_OMNIGENT),
            ("systemctl", FAKE_SYSTEMCTL),
            ("online", FAKE_ONLINE),
            ("journal", journal),
        ):
            path = bin_dir / name
            path.write_text(body)
            path.chmod(0o755)
        self.env = {
            "PATH": os.environ["PATH"],
            "HOME": str(tmp),
            "OMNIGENT_SERVER_URL": "https://box.tailnet.example:8010",
            "AE_RELINK_CLI": str(bin_dir / "cli"),
            "AE_RELINK_JOURNAL": str(bin_dir / "journal"),
            "AE_RELINK_HOST_ONLINE": str(bin_dir / "online"),
            "AE_RELINK_OMNIGENT": str(bin_dir / "omnigent"),
            "AE_RELINK_SYSTEMCTL": str(bin_dir / "systemctl"),
            "AE_RELINK_STATE_DIR": str(self.state),
            "AE_RELINK_URL_WAIT": "2",
            "AE_RELINK_RETRY_PAUSE": "0",
            "AE_RELINK_POLL_FAST": "3600",
            "AE_RELINK_POLL_SLOW": "3600",
            "AE_RELINK_POLL_IDLE": "3600",
            "FAKE_CALLS": str(self.calls),
            "FAKE_STDIN": str(self.stdin),
            "FAKE_PIDS": str(self.pids),
            "FAKE_TAKE_PLAN": str(self.take_plan),
            "FAKE_LOGIN_PLAN": str(self.login_plan),
        }

    def run(self, *args: str, **env: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(SCRIPT), *args],
            env={**self.env, **env},
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )

    def lines(self, *lines: str) -> None:
        self.journal.write_text("".join(f"{line}\n" for line in lines))

    def logins(self, *outcomes: str) -> None:
        self.login_plan.write_text("".join(f"{item}\n" for item in outcomes))

    def takes(self, *replies: str) -> None:
        self.take_plan.write_text("".join(f"{item}\n" for item in replies))

    def called(self, prefix: str) -> list[str]:
        return [line for line in self.calls.read_text().splitlines() if line.startswith(prefix)]


@pytest.fixture
def box(tmp_path: Path) -> Box:
    return Box(tmp_path)


def _restarts(box: Box) -> list[str]:
    return box.called("systemctl --user restart ae-omnigent-host.service")


# ── 1. what triggers a run ──────────────────────────────────────────────


def test_the_first_rejection_alone_runs_nothing_and_reports_streak_1(box: Box) -> None:
    box.lines(FIRST)
    result = box.run("watch")
    assert result.returncode == 1 and "journal" in result.stderr
    assert box.called("omnigent") == []
    assert box.called("cli status rejected")[0].split()[3] == "streak=1"


def test_the_30th_rejection_line_runs_the_login_once_and_restarts_once(box: Box) -> None:
    box.lines(FIRST, every_30th(30))
    box.logins("ok")
    box.run("watch")
    assert box.called("omnigent") == ["omnigent login https://box.tailnet.example:8010"]
    assert len(_restarts(box)) == 1
    # The tile's count is the host's own streak, not a count of journal lines.
    streaks = [line.split()[3] for line in box.called("cli status rejected")]
    assert streaks == ["streak=1", "streak=30"]
    pushes = box.called("cli push ")
    assert pushes[0].split()[2] == "https://box.tailnet.example:8010/auth/login?ticket=T1"
    phases = [line.split()[2] for line in box.called("cli status")]
    assert phases[-3:] == ["sending", "sent", "restarting"]


def test_a_failed_host_unit_is_a_trigger(box: Box) -> None:
    # A host that never connected gives up after three rejections, with no 30th line.
    box.lines(FIRST)
    box.logins("ok")
    result = box.run("watch", FAKE_ACTIVE="failed")
    assert "has failed" in result.stderr
    assert len(box.called("omnigent")) == 1 and len(_restarts(box)) == 1


def test_a_cleanly_stopped_host_unit_is_left_alone(box: Box) -> None:
    box.lines("some line")
    box.run("watch", FAKE_ACTIVE="inactive")
    assert box.called("omnigent") == []


def test_the_cooldown_holds_through_the_rest_of_the_streak(box: Box) -> None:
    box.lines(FIRST, every_30th(30), every_30th(60), every_30th(90))
    box.run("watch", AE_RELINK_ATTEMPTS="1")
    assert len(box.called("omnigent")) == 1
    assert box.called("cli giveup") == ["cli giveup 1"]


def test_without_a_cooldown_the_streak_runs_it_again(box: Box) -> None:
    box.lines(FIRST, every_30th(30), every_30th(60))
    box.run("watch", AE_RELINK_ATTEMPTS="1", AE_RELINK_COOLDOWN="0")
    assert len(box.called("omnigent")) == 2


def test_a_connect_line_ends_the_streak(box: Box) -> None:
    box.lines(FIRST, CONNECTED, FIRST)
    box.run("watch", AE_RELINK_THRESHOLD="2")
    assert box.called("omnigent") == []
    assert box.called("cli status connected") == ["cli status connected"]


# ── 2. connected comes from the server and systemd, never the journal ──


def test_a_request_while_the_server_sees_the_host_offline_runs_the_login(box: Box) -> None:
    # The last journal line says connected; the server knows better.
    box.lines(CONNECTED)
    box.takes("request 1792520000 reid")
    box.logins("ok")
    box.run("watch", FAKE_ONLINE="offline")
    assert len(box.called("omnigent")) == 1 and len(_restarts(box)) == 1


def test_a_request_while_the_server_sees_the_host_online_does_nothing(box: Box) -> None:
    box.lines(FIRST)
    box.takes("request 1792520000 reid")
    result = box.run("watch", FAKE_ONLINE="online")
    assert box.called("omnigent") == [] and _restarts(box) == []
    assert "sees the host online" in result.stderr


def test_a_request_runs_when_the_online_check_fails(box: Box) -> None:
    box.lines("some line")
    box.takes("request 1792520000 reid")
    box.logins("ok")
    box.run("watch", FAKE_ONLINE="fail")
    assert len(box.called("omnigent")) == 1


def test_the_healthy_path_never_asks_the_server(box: Box) -> None:
    box.lines(*["some line"] * 5)
    box.run("watch", AE_RELINK_POLL_SLOW="1", AE_RELINK_POLL_IDLE="3600")
    # The first pass takes once; after that only systemctl is-active runs.
    assert box.called("cli take") == ["cli take"]
    assert box.called("online-check") == []


# ── 3. one incident, one login, one restart ─────────────────────────────


def test_requests_left_before_or_during_a_loop_are_dropped(box: Box) -> None:
    box.lines(FIRST, every_30th(30))
    # Taken at the loop's start (left before it) and after the restart (left during it).
    box.takes("none", "request 1 reid", "request 2 reid")
    box.logins("ok")
    result = box.run("watch")
    assert len(box.called("omnigent")) == 1 and len(_restarts(box)) == 1
    assert result.stderr.count("dropped a pending reconnect request") == 2


# ── 5. why a loop failed ────────────────────────────────────────────────


def test_an_expired_link_retries_up_to_five_times_then_says_lapsed(box: Box) -> None:
    box.lines(FIRST, every_30th(30))
    box.run("watch")
    assert len(box.called("omnigent")) == 5
    assert _restarts(box) == []
    assert box.called("cli status failed") == [
        "cli status failed attempt=5 attempts=5 reason=lapsed"
    ]
    assert box.called("cli giveup") == ["cli giveup 5"]


def test_a_sign_in_whose_restart_fails_says_restart(box: Box) -> None:
    box.logins("ok")
    result = box.run("relink", FAKE_RESTART="fail")
    assert result.returncode == 1
    assert box.called("cli status failed") == [
        "cli status failed attempt=1 attempts=5 reason=restart"
    ]


def test_a_sign_in_on_the_second_link_restarts_once(box: Box) -> None:
    box.logins("expired", "ok")
    box.run("relink")
    assert len(box.called("omnigent")) == 2
    assert len(_restarts(box)) == 1
    assert box.called("cli giveup") == []


def test_a_login_that_prints_no_link_is_retried_without_a_push(box: Box) -> None:
    box.logins("nolink", "ok")
    box.run("relink")
    assert len(box.called("omnigent")) == 2
    assert len(box.called("cli push ")) == 1
    assert len(_restarts(box)) == 1


# ── 6. a reminder that could not be sent ────────────────────────────────


def test_a_failed_push_stops_that_login_and_moves_on(box: Box) -> None:
    box.logins("wait", "wait")
    started = time.monotonic()
    result = box.run("relink", FAKE_PUSH="fail", AE_RELINK_ATTEMPTS="2")
    # Both logins would wait 30 s each; neither is waited out.
    assert time.monotonic() - started < 20
    assert result.returncode == 1
    assert len(box.called("omnigent")) == 2
    assert box.called("cli status sent") == []
    assert any("could not be sent" in line for line in box.called("cli status sending"))
    assert box.called("cli status failed") == [
        "cli status failed attempt=2 attempts=2 reason=push"
    ]
    for pid in box.pids.read_text().split():
        with pytest.raises(ProcessLookupError):
            os.kill(int(pid), 0)


# ── 7. the ticket file on a signal ──────────────────────────────────────


@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGINT, signal.SIGHUP])
def test_a_signal_removes_the_ticket_file_and_stops_the_login(box: Box, sig: int) -> None:
    box.logins("wait")
    proc = subprocess.Popen(
        [str(SCRIPT), "relink"],
        env=box.env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    deadline = time.monotonic() + 20
    while not box.called("cli status sent") and time.monotonic() < deadline:
        time.sleep(0.1)
    assert (box.state / "login.txt").exists()
    proc.send_signal(sig)
    assert proc.wait(timeout=20) == 128 + sig
    assert not (box.state / "login.txt").exists()
    for pid in box.pids.read_text().split():
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                os.kill(int(pid), 0)
            except ProcessLookupError:
                break
            time.sleep(0.1)
        else:
            pytest.fail(f"login {pid} still running")


# ── the rest ────────────────────────────────────────────────────────────


def test_children_never_read_the_journal(box: Box) -> None:
    box.lines(FIRST, every_30th(30), every_30th(60), CONNECTED)
    box.logins("ok")
    box.run("watch")
    # Every line reached the watcher: the connect line after the run was seen.
    assert box.called("cli status connected")
    assert box.stdin.read_text() == ""


def test_the_login_output_is_private_and_removed(box: Box) -> None:
    box.logins("expired", "ok")
    box.run("relink")
    assert stat.S_IMODE(box.state.stat().st_mode) == 0o700
    assert list(box.state.iterdir()) == []


def test_relink_by_hand_exits_1_after_five_lapsed_links(box: Box) -> None:
    result = box.run("relink")
    assert result.returncode == 1
    assert len(box.called("omnigent")) == 5


def test_a_missing_server_url_stops_before_anything_runs(box: Box) -> None:
    result = box.run("watch", OMNIGENT_SERVER_URL="")
    assert result.returncode == 1 and "OMNIGENT_SERVER_URL" in result.stderr
    assert box.calls.read_text() == ""


def test_an_unknown_mode_prints_the_usage(box: Box) -> None:
    result = box.run("nope")
    assert result.returncode == 1 and "usage" in result.stderr


def test_the_unit_runs_the_watcher_from_the_deploy_clone() -> None:
    unit = configparser.ConfigParser(interpolation=None, strict=False)
    unit.read(UNIT)
    service = unit["Service"]
    assert service["ExecStart"] == "%h/deploy/omnigent-ae/deploy/host-relink.sh watch"
    assert service["Type"] == "simple" and service["Restart"] == "always"
    assert service["EnvironmentFile"] == "-%h/deploy/omnigent-ae/config/host.env"
    assert unit["Install"]["WantedBy"] == "default.target"
