"""The box health relay: fake /sys and /proc, privacy, containers and posting (2026-10-28)."""

from __future__ import annotations

import asyncio
import dataclasses
import io
import json
import os
import re
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import httpx
import pytest

from ae_omni_policies import box_health, box_health_scan, omni
from ae_omni_policies import box_health_relay as relay
from ae_omni_policies.box_health_relay import RunResult, StreamResult

NOW = time.time()
DAY = 86400
UUID_A = "3f2a1c00-1111-4222-8333-444455556666"
TOKENS = (
    "ZQX-client-acme",
    "ZQX-secret-branch",
    "ZQX-private-repo",
    "ZQX-wt-name",
    "ZQX-mount-client",
    "ZQX-sys-unit",
    "ZQX-user-unit",
    "ZQX-folder-path",
    "ZQX-ps-name",
    "ZQX-volume-home",
    "ZQX-exception",
)
MOUNT_T, SYS_UNIT_T, USER_UNIT_T, DIR_T, PS_T, VOL_T, EXC_T = TOKENS[4:]
ME = os.getuid()


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def age(path: Path, days: float) -> None:
    stamp = NOW - days * DAY
    os.utime(path, (stamp, stamp), follow_symlinks=False)


# ── a fake /sys, /proc and mounts ───────────────────────────────────────────


def fake_box(tmp_path: Path, *, shuffle: bool = False) -> relay.Roots:
    sys_root, proc = tmp_path / "sys", tmp_path / "proc"
    chips = [
        ("k10temp", {"temp1_input": "91000", "temp1_label": "Tctl"}),
        (
            "nvme",
            {
                "temp1_input": "48900",
                "temp1_label": "Composite",
                "temp2_input": "70000",
                "temp2_label": "Sensor 1",
            },
        ),
        ("acpitz_0", {"temp1_input": "89000"}),
        ("amdgpu", {"temp1_input": "80000", "temp1_label": "edge", "power1_average": "112400000"}),
        ("r8169_0_c100:00", {"temp1_input": "50000"}),
    ]
    order = [4, 2, 0, 3, 1] if shuffle else list(range(5))
    for slot, index in enumerate(order):
        name, files = chips[index]
        write(sys_root / "class" / "hwmon" / f"hwmon{slot}" / "name", name)
        for fname, value in files.items():
            write(sys_root / "class" / "hwmon" / f"hwmon{slot}" / fname, value)
    write(proc / "loadavg", "11.10 8.40 5.40 3/1500 99999")
    write(
        proc / "stat",
        "cpu  1000 0 500 8000 500 0 0 0 0 0\ncpu0 500 0 250 4000 250 0 0 0 0 0\n"
        "cpu1 500 0 250 4000 250 0 0 0 0 0\n",
    )
    write(proc / "uptime", "532000.12 99999.0")
    write(
        proc / "meminfo",
        "MemTotal: 131072000 kB\nMemFree: 1000 kB\nMemAvailable: 78643200 kB\n"
        "Shmem: 12582912 kB\nSwapTotal: 8388608 kB\nSwapFree: 4194304 kB\n",
    )
    write(proc / "pressure" / "cpu", "some avg10=12.00 avg60=9.10 avg300=6.00 total=1\n")
    write(
        proc / "pressure" / "memory",
        "some avg10=0.00 avg60=1.50 avg300=0.00 total=1\n"
        "full avg10=0.00 avg60=0.50 avg300=0.00 total=1\n",
    )
    mounts = write(
        tmp_path / "mounts",
        "\n".join(
            [
                "/dev/nvme0n1p2 / ext4 rw 0 0",
                "tmpfs /tmp tmpfs rw 0 0",
                "overlay /var/lib/docker/overlay2/x overlay rw 0 0",
                "/dev/loop1 /snap/core/1 squashfs ro 0 0",
                "tmpfs /run/user/1000 tmpfs rw 0 0",
                "/dev/nvme0n1p1 /boot/efi vfat rw 0 0",
                f"/dev/nvme0n1p9 /mnt/{MOUNT_T} ext4 rw 0 0",
                "/dev/nvme0n1p3 /var/lib/docker/vol ext4 rw 0 0",
                "/dev/nvme0n1p2 / ext4 rw 0 0",
                "server:/export /mnt/nfs nfs4 rw 0 0",
            ]
        ),
    )
    return relay.Roots(sys=sys_root, proc=proc, mounts=mounts, tmp=str(tmp_path / "tmp"))


def fake_statvfs(_path: str) -> os.statvfs_result:
    return os.statvfs_result((4096, 4096, 1000, 400, 300, 1000, 500, 500, 0, 255))


# ── a fake runner ───────────────────────────────────────────────────────────


class FakeRunner:
    """Records every argument list and answers from a table keyed by what it asks."""

    def __init__(self, answers: Mapping[str, Any] | None = None) -> None:
        self.calls: list[list[str]] = []
        self.answers = dict(answers or {})

    def key(self, argv: Sequence[str]) -> str:
        argv = list(argv)
        joined = " ".join(argv)
        if argv[:2] == ["docker", "exec"]:
            if "-newermt" in argv:
                return "fresh"
            if argv[-2:] == ["id", "-u"]:
                return "uid"
            if "/proc" in argv:
                if "grep" in argv:
                    return "uids"
                return "cmdlines" if "-exec" in argv else "procs"
            if "rm" in argv:
                return "rm"
            if "%y\t%U" in joined:
                return "tmp_list"
            if "/tmp/claude-" in joined:
                return "claude_tree"
            if "pytest-of-" in joined:
                return "pytest_tree"
            if "%T@\n" in joined:
                return "worktrees_list"
            return "worktrees_du"
        if argv[:2] == ["docker", "ps"]:
            return "ps"
        if argv[:2] == ["docker", "inspect"]:
            return "inspect"
        if argv[:4] == ["docker", "system", "df", "-v"]:
            return "volumes"
        if argv[:3] == ["docker", "system", "df"]:
            return "docker_df"
        if argv[0] == "journalctl":
            return "journal"
        if argv[:2] == ["systemctl", "--user"]:
            return "failed_user"
        if argv[0] == "systemctl":
            return "failed_system"
        return argv[0]

    def run(
        self, argv: Sequence[str], timeout: float, env: Mapping[str, str] | None = None
    ) -> RunResult:
        self.calls.append(list(argv))
        answer = self.answers.get(self.key(argv), RunResult(0, ""))
        return answer if isinstance(answer, RunResult) else RunResult(0, str(answer))

    def stream(self, argv: Sequence[str], timeout: float, max_lines: int) -> StreamResult:
        self.calls.append(list(argv))
        answer = self.answers.get(self.key(argv), StreamResult())
        if isinstance(answer, StreamResult):
            return answer
        return StreamResult(lines=list(answer)[:max_lines])

    def container_calls(self) -> list[list[str]]:
        return [
            c
            for c in self.calls
            if c[:2] in (["docker", "exec"], ["docker", "ps"], ["docker", "inspect"])
        ]


DOCKER_DF = "\n".join(
    json.dumps(row)
    for row in (
        {"Type": "Images", "TotalCount": "210", "Size": "98.01GB", "Reclaimable": "5.82GB (5%)"},
        {"Type": "Containers", "TotalCount": "12", "Size": "392.5MB", "Reclaimable": "0B (0%)"},
        {
            "Type": "Local Volumes",
            "TotalCount": "13",
            "Size": "193.4GB",
            "Reclaimable": "557.6MB (0%)",
        },
        {"Type": "Build Cache", "TotalCount": "21", "Size": "156.1MB", "Reclaimable": "24.37MB"},
    )
)
JOURNAL = "Archived and active journals take up 494.0M in the file system."
PS_ROW = json.dumps({"Names": PS_T, "State": "running", "Size": "293MB (virtual 5.15GB)"})
MOUNTS = json.dumps(
    [{"Type": "volume", "Name": VOL_T, "Destination": "/home/agent", "Source": "/x"}]
)
VOLUMES = json.dumps(
    {"Volumes": [{"Name": VOL_T, "Size": "1.2GB"}, {"Name": "other", "Size": "9GB"}]}
)


LIVE = {
    # one live process of the container user, with a working folder
    "uids": [
        "/proc/7/status:Uid:\t1001\t1001\t1001\t1001\n",
        "/proc/7/status:State:\tS (sleeping)\n",
        "/proc/7/status:Threads:\t1\n",
    ],
    "procs": ["/proc/7/cwd\t/home/agent\0"],
}


def container_answers() -> dict[str, Any]:
    old = NOW - 5 * DAY
    return {
        "docker_df": DOCKER_DF,
        "journal": JOURNAL,
        "failed_system": f"● {SYS_UNIT_T}.service loaded failed failed X\n"
        "  ae-omnigent-limits.service loaded failed failed Y\n"
        "  docker.service loaded failed failed Z\n",
        "failed_user": f"  {USER_UNIT_T}.service loaded failed failed U\n"
        f"  ae-omnigent-host-container@{EXC_T}.service loaded failed failed V\n",
        "ps": PS_ROW,
        "inspect": MOUNTS,
        "volumes": VOLUMES,
        "tmp_list": [
            f"d\t1001\t{NOW}\tclaude-1001\n",
            f"d\t1001\t{NOW}\tpytest-of-agent\n",
            f"d\t1001\t{old}\tsec-appr-{TOKENS[0]}\n",
            f"f\t1001\t{old}\t{TOKENS[1]}.log\n",
        ],
        "claude_tree": [
            f"1\td\t{old}\t8\t-{TOKENS[0]}\n",
            f"2\td\t{old}\t8\t-{TOKENS[0]}/{UUID_A}\n",
            f"3\tf\t{old}\t8\t-{TOKENS[0]}/{UUID_A}/{TOKENS[1]}.txt\n",
        ],
        "pytest_tree": [f"1\td\t{old}\t8\tpytest-3\n"],
        "uid": ["1001\n"],
        **LIVE,
        "worktrees_list": [f"{old}\n", f"{NOW}\n"],
        "worktrees_du": "2048\t/home/agent/worktrees\n",
    }


def settings_for(tmp_path: Path, **env: str) -> relay.Settings:
    base = {
        "AE_BOX_USERS": f"{ME}=UserA,container:omnigent-host-example=UserB",
        "AE_BOX_HEALTH_CLAUDE_ROOTS": str(tmp_path / "claude-{uid}"),
        "AE_BOX_HEALTH_DIRS": f"data={tmp_path / DIR_T},{tmp_path / 'dir1'}",
        "AE_BOX_HEALTH_WORKTREES": str(tmp_path / "worktrees"),
        "AE_BOX_HEALTH_STATE_FILE": str(tmp_path / "state.json"),
        "AE_BOX_HEALTH_GUARD_FILE": str(tmp_path / "guard.json"),
        **env,
    }
    return relay.settings_from_env(base, home=str(tmp_path))


def fake_tree(tmp_path: Path) -> None:
    """UserA's files, every name carrying a token that must never leave the box."""
    tmp = tmp_path / "tmp"
    root = tmp_path / f"claude-{ME}" / f"-{TOKENS[0]}" / UUID_A
    write(root / "tasks" / f"{TOKENS[1]}.txt", "x")
    for folder in (root / "tasks", root, root.parent):
        age(folder, 5)
    age(root / "tasks" / f"{TOKENS[1]}.txt", 5)
    litter = tmp / f"sec-appr-{TOKENS[2]}"
    write(litter / "f.txt", "x")
    age(litter, 5)
    write(tmp / f"{TOKENS[3]}.dat", "x")
    for token in (TOKENS[0], TOKENS[3]):
        write(tmp_path / "worktrees" / token / "README", "x")
    write(tmp_path / DIR_T / "f", "x")
    write(tmp_path / "dir1" / "f", "x")
    write(
        tmp_path / "guard.json",
        json.dumps(
            {
                "locked": 2,
                "failed": 0,
                "claude_processes": 3,
                "claude_unmatched": 0,
                "claude_remote": 1,
                "updated_at": int(NOW),
                "pid": os.getpid(),
            }
        ),
    )


def collect(tmp_path: Path, runner: FakeRunner, **kw: Any):
    roots = fake_box(tmp_path)
    settings = kw.pop("settings", None) or settings_for(tmp_path)
    return relay.collect(settings, runner=runner, roots=roots, now=NOW, home=str(tmp_path), **kw)


# ── collectors ──────────────────────────────────────────────────────────────


def test_hwmon_is_matched_by_name_even_when_the_indexes_shuffle(tmp_path: Path) -> None:
    straight, _ = relay.read_sensors(fake_box(tmp_path / "a").sys)
    shuffled, power = relay.read_sensors(fake_box(tmp_path / "b", shuffle=True).sys)
    assert sorted((s.chip, s.label, s.c) for s in straight) == sorted(
        (s.chip, s.label, s.c) for s in shuffled
    )
    by_chip = {s.chip: s for s in shuffled}
    assert by_chip["k10temp"].c == 91.0 and by_chip["nvme"].c == 48.9
    assert not any(s.label == "Sensor 1" for s in shuffled)
    assert power == pytest.approx(112.4)
    assert "r8169_0_c100:00" in by_chip
    post = box_health.BoxHealthPost(read_at="2026-10-28T00:00:00Z", sensors=shuffled)
    assert box_health.cpu_temp(post) == 91.0 and box_health.nvme_temp(post) == 48.9


def test_load_memory_and_pressure_are_parsed(tmp_path: Path) -> None:
    proc = fake_box(tmp_path).proc
    cpu, counters = relay.read_cpu(proc, None)
    assert (cpu.threads, cpu.load15, cpu.busy_pct, cpu.psi_some_avg60) == (2, 5.4, None, 9.1)
    assert counters == {"total": 10000, "idle": 8500}
    mem = relay.read_memory(proc)
    assert mem.total_b == 131072000 * 1024 and mem.available_b == 78643200 * 1024
    assert (mem.psi_some_avg60, mem.psi_full_avg60) == (1.5, 0.5)


def test_busy_percent_needs_the_cached_counters(tmp_path: Path) -> None:
    proc = fake_box(tmp_path).proc
    write(proc / "stat", "cpu  1500 0 500 8500 500 0 0 0 0 0\ncpu0 1 0 0 1 0 0 0 0 0 0\n")
    cpu, _ = relay.read_cpu(proc, {"total": 10000, "idle": 8500})
    assert cpu.busy_pct == 50.0
    write(proc / "stat", "cpu  1 0 0 1 0 0 0 0 0 0\ncpu0 1 0 0 1 0 0 0 0 0 0\n")
    assert relay.read_cpu(proc, {"total": 10000, "idle": 8500})[0].busy_pct is None


def test_mounts_keep_local_filesystems_and_drop_skipped_ones(tmp_path: Path) -> None:
    roots = fake_box(tmp_path)
    rows = relay.read_mounts(roots.mounts, relay.settings_from_env({}).skip_mounts, fake_statvfs)
    assert sorted(m.path for m in rows) == sorted(["/", "/tmp", "/boot/efi", "other mount 1"])
    efi = next(m for m in rows if m.path == "/boot/efi")
    assert efi.inodes == 1000
    vfat = os.statvfs_result((4096, 4096, 1000, 400, 300, 0, 0, 0, 0, 255))
    rows = relay.read_mounts(roots.mounts, [], lambda _p: vfat)
    assert next(m for m in rows if m.path == "/boot/efi").inodes is None
    used = rows[0]
    assert used.used_b == (1000 - 400) * 4096 and used.avail_b == 300 * 4096


def test_docker_and_journal_sizes_parse() -> None:
    assert relay.parse_docker_size("98.01GB") == 98_010_000_000
    assert relay.parse_docker_size("5.82GB (5%)") == 5_820_000_000
    assert relay.parse_docker_size("293MB (virtual 5.15GB)") == 293_000_000
    assert relay.parse_docker_size("1.2kB") == 1200
    assert relay.parse_docker_size("nonsense") is None
    assert relay.parse_journal_usage(JOURNAL) == int(494.0 * 1024**2)
    docker = relay.read_docker(FakeRunner({"docker_df": DOCKER_DF}))
    assert (docker.images, docker.images_reclaim_b, docker.volumes_b) == (
        210,
        5_820_000_000,
        193_400_000_000,
    )
    with pytest.raises(relay.RelayError):
        relay.read_docker(FakeRunner({"docker_df": RunResult(127)}))


def test_failed_units_are_named_only_from_the_allowlist() -> None:
    runner = FakeRunner(
        {
            "failed_system": "● ae-omnigent-limits.service loaded failed failed A\n"
            "  acme-client.service loaded failed failed B\n"
            "  systemd-resolved.service loaded failed failed C\n"
            "  docker.service loaded failed failed D\n"
            "  some-repo.timer loaded failed failed E\n",
            "failed_user": "  ae-omnigent-host-container@userb.service loaded failed failed F\n"
            "  mine.service loaded failed failed G\n",
        }
    )
    assert relay.read_failed_units(runner) == (
        ["ae-omnigent-limits.service", "systemd-resolved.service", "docker.service"],
        2,
        ["ae-omnigent-host-container@.service"],
        1,
    )
    with pytest.raises(relay.RelayError):
        relay.read_failed_units(FakeRunner({"failed_system": RunResult(1)}))


def test_the_guard_file_decides_running_and_tracks_unmatched(tmp_path: Path) -> None:
    path = tmp_path / "guard.json"
    row = {
        "locked": 5,
        "failed": 0,
        "claude_processes": 6,
        "claude_unmatched": 2,
        "claude_remote": 4,
        "updated_at": int(NOW) - 10,
        "pid": 7,
    }
    path.write_text(json.dumps(row))
    guard, streak = relay.read_guard(str(path), NOW, alive=lambda _p: True)
    assert (guard.running, guard.locked, guard.claude_remote, guard.unmatched_readings) == (
        True,
        5,
        4,
        1,
    )
    again, streak = relay.read_guard(str(path), NOW + 600, alive=lambda _p: True, previous=streak)
    assert (again.unmatched_readings, again.unmatched_minutes) == (2, 10)
    path.write_text(json.dumps({**row, "claude_unmatched": 0}))
    clear, streak = relay.read_guard(str(path), NOW, alive=lambda _p: True)
    assert (clear.unmatched_readings, streak) == (0, {})
    assert not relay.read_guard(str(path), NOW + 200, alive=lambda _p: True)[0].running
    assert not relay.read_guard(str(path), NOW, alive=lambda _p: False)[0].running
    assert not relay.read_guard(str(tmp_path / "missing"), NOW)[0].running


# ── the users map ───────────────────────────────────────────────────────────


def test_users_map_parsing() -> None:
    users = relay.parse_users("1000=UserA, 1001=UserB ,container:omnigent-host-example=UserB")
    assert users.uids == {1000: "UserA", 1001: "UserB"} and users.bad == 0
    assert users.containers == {"omnigent-host-example": "UserB"}
    bad = relay.parse_users("abc=Nope,1002=../x,container:-bad name=X,container:ok=Fine,1003")
    assert bad.bad == 4 and bad.containers == {"ok": "Fine"}
    assert relay.label_of(999, bad) == "System" and relay.label_of(None, bad) == "System"
    assert relay.label_of(1000, users) == "UserA"


def test_a_bad_users_entry_is_a_failed_part(tmp_path: Path) -> None:
    fake_tree(tmp_path)
    post, _, failed = collect(
        tmp_path,
        FakeRunner(container_answers()),
        settings=settings_for(tmp_path, AE_BOX_USERS="oops"),
    )
    assert "users" in failed and "users" in post.failed


# ── containers: only fixed read-only commands ───────────────────────────────


def full_run(tmp_path: Path, answers: dict[str, Any] | None = None):
    fake_tree(tmp_path)
    runner = FakeRunner(answers or container_answers())
    post, state, failed = collect(tmp_path, runner)
    return post, state, failed, runner


def test_every_container_command_is_a_template_with_a_mapped_container(tmp_path: Path) -> None:
    _, _, _, runner = full_run(tmp_path)
    calls = runner.container_calls()
    assert {runner.key(c) for c in calls} >= {
        "ps",
        "tmp_list",
        "claude_tree",
        "worktrees_list",
        "inspect",
    }
    mapped = ["omnigent-host-example"]
    for argv in calls:
        assert relay.matches_container_template(argv, mapped, "agent"), argv
        assert "-u" not in argv or argv[argv.index("-u") + 1] == "agent"
        assert not {"sh", "bash", "-c", ";", "|"} & set(argv)
        assert not any(re.search(r"[;|&`]", part) for part in argv)
    for argv in calls:
        if argv[:2] == ["docker", "exec"]:
            assert argv[2:4] == ["-u", "agent"] and argv[4] == "omnigent-host-example"
            assert "timeout" in argv


def test_a_container_that_is_not_mapped_is_never_run(tmp_path: Path) -> None:
    _, _, _, runner = full_run(tmp_path)
    assert all("omnigent-postgres-1" not in c for c in runner.calls)
    good = relay.container_argv("tmp_list", container="omnigent-host-example", user="agent")
    assert relay.matches_container_template(good, ["omnigent-host-example"], "agent")
    other = relay.container_argv("tmp_list", container="omnigent-postgres-1", user="agent")
    assert not relay.matches_container_template(other, ["omnigent-host-example"], "agent")


def test_a_command_as_root_or_another_user_never_matches() -> None:
    good = relay.container_argv("tmp_list", container="omnigent-host-example", user="agent")
    as_root = [*good[:3], "root", *good[4:]]
    assert as_root != good and as_root[3] == "root"
    assert not relay.matches_container_template(as_root, ["omnigent-host-example"], "agent")
    assert not relay.matches_container_template(as_root, ["omnigent-host-example"], "root")
    other = [*good[:3], "someone", *good[4:]]
    assert not relay.matches_container_template(other, ["omnigent-host-example"], "agent")
    with pytest.raises(ValueError):
        relay.container_argv("ps", container="omnigent-host-example", user="root")


def test_the_container_user_setting_refuses_root() -> None:
    env = {"AE_BOX_HEALTH_CONTAINER_USER": "root"}
    assert relay.settings_from_env(env, home="/h").container_user == "agent"
    assert (
        relay.settings_from_env({"AE_BOX_HEALTH_CONTAINER_USER": "bob"}, home="/h").container_user
        == "bob"
    )


@pytest.mark.parametrize("name", ["", "-x", "a b", "a;b", "a/b", "x" * 64])
def test_a_bad_container_name_or_user_is_refused(name: str) -> None:
    with pytest.raises(ValueError):
        relay.container_argv("ps", container=name, user="agent")
    with pytest.raises(ValueError):
        relay.container_argv("ps", container="ok", user=name or "Bad User")
    with pytest.raises(ValueError):
        relay.container_argv("claude_tree", container="ok", user="agent", n="1;rm")


def test_every_template_placeholder_is_one_of_three() -> None:
    for key, template in relay.CONTAINER_COMMANDS.items():
        left = re.findall(r"<[a-z]+>", " ".join(template))
        assert set(left) <= {"<c>", "<user>", "<n>"}, key
        assert template[0] == "docker" and "sh" not in template


def test_a_stopped_container_runs_nothing_else(tmp_path: Path) -> None:
    answers = {**container_answers(), "ps": ""}
    post, _, _, runner = full_run(tmp_path, answers)
    userb = next(u for u in post.slow.users if u.kind == "container")
    assert userb.running is False and userb.claude == []
    assert [runner.key(c) for c in runner.container_calls()] == ["ps"]


def test_the_container_is_measured_as_counts(tmp_path: Path) -> None:
    fake_tree(tmp_path)
    settings = settings_for(tmp_path, AE_BOX_HEALTH_CLEANUP="off")
    post, _, failed = collect(tmp_path, FakeRunner(container_answers()), settings=settings)
    userb = next(u for u in post.slow.users if u.kind == "container")
    assert userb.label == "UserB" and userb.running
    assert (userb.writable_b, userb.home_b) == (293_000_000, 1_200_000_000)
    root = userb.claude[0]
    assert (root.where, root.sessions, root.stale_sessions) == ("container", 1, 1)
    assert userb.pytest and userb.pytest.entries == 1 and userb.pytest.stale == 1
    assert userb.worktrees and (userb.worktrees.count, userb.worktrees.older_14d) == (2, 0)
    assert userb.worktrees.bytes == 2048 * 1024
    assert {g.group for g in userb.litter} == {"sec-appr-*", "other"}
    assert failed == []


# ── privacy ─────────────────────────────────────────────────────────────────


def test_no_name_reaches_the_post_the_view_or_the_output(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    post, state, _, _runner = full_run(tmp_path)
    body = post.model_dump_json()
    stored = box_health.Stored(reading=post, received_at=post.read_at)
    view = box_health.view(stored, post.read_at).model_dump_json()
    out = io.StringIO()
    import asyncio

    asyncio.run(
        relay.run(
            settings=settings_for(tmp_path),
            omni_settings=omni.Settings(),
            runner=FakeRunner(container_answers()),
            roots=fake_box(tmp_path),
            dry_run=True,
            out=out,
            now=NOW,
            home=str(tmp_path),
        )
    )
    seen = capsys.readouterr()
    everything = "\n".join(
        [body, view, json.dumps(state), out.getvalue(), seen.out, seen.err, caplog.text]
    )
    for token in (*TOKENS, "secret", "acme", str(tmp_path), "README"):
        assert token not in everything, token
    assert "/home" not in body.replace("/home/agent", "")
    users = {u.label: u for u in post.slow.users}
    assert users["UserA"].claude[0].stale_sessions == 1 and users["UserA"].worktrees.count == 2
    assert users["UserA"].litter and all(g.inodes is None for g in users["UserA"].litter)


def test_the_relay_never_walks_into_a_litter_folder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_tree(tmp_path)
    opened: list[str] = []
    real = os.scandir

    def spy(path: Any) -> Any:
        opened.append(str(path))
        return real(path)

    monkeypatch.setattr(os, "scandir", spy)
    collect(tmp_path, FakeRunner(container_answers()))
    assert not any("sec-appr-" in p for p in opened)


# ── slow-scan caching, state and the post ───────────────────────────────────


def test_the_slow_scan_is_cached_until_it_is_due(tmp_path: Path) -> None:
    fake_tree(tmp_path)
    first = FakeRunner(container_answers())
    post, state, _ = collect(tmp_path, first)
    assert any(first.key(c) == "ps" for c in first.calls)
    again = FakeRunner(container_answers())
    cached, _, _ = relay.collect(
        settings_for(tmp_path),
        runner=again,
        roots=fake_box(tmp_path),
        state=state,
        now=NOW + 30 * 60,
        home=str(tmp_path),
    )
    assert not any(again.key(c) == "ps" for c in again.calls)
    assert cached.slow is not None and cached.slow.read_at == post.slow.read_at
    late = FakeRunner(container_answers())
    relay.collect(
        settings_for(tmp_path),
        runner=late,
        roots=fake_box(tmp_path),
        state=state,
        now=NOW + 59 * 60,
        home=str(tmp_path),
    )
    assert any(late.key(c) == "ps" for c in late.calls)
    forced = FakeRunner(container_answers())
    relay.collect(
        settings_for(tmp_path),
        runner=forced,
        roots=fake_box(tmp_path),
        state=state,
        now=NOW + 60,
        force_slow=True,
        home=str(tmp_path),
    )
    assert any(forced.key(c) == "ps" for c in forced.calls)


def test_a_failed_part_is_named_and_the_rest_is_posted(tmp_path: Path) -> None:
    fake_tree(tmp_path)
    answers = {**container_answers(), "docker_df": RunResult(127)}
    post, _, failed = collect(tmp_path, FakeRunner(answers))
    assert "docker" in failed and post.slow.docker is None and post.cpu is not None


async def run_relay(
    tmp_path: Path, transport: httpx.AsyncBaseTransport, **kw: Any
) -> tuple[int, str]:
    fake_tree(tmp_path)
    out = io.StringIO()
    code = await relay.run(
        settings=settings_for(tmp_path),
        omni_settings=omni.Settings(server_url="https://omni.test", token="t"),
        runner=FakeRunner(kw.pop("answers", container_answers())),
        roots=fake_box(tmp_path),
        transport=transport,
        out=out,
        now=NOW,
        home=str(tmp_path),
        **kw,
    )
    return code, out.getvalue()


async def test_dry_run_prints_and_posts_nothing(tmp_path: Path) -> None:
    def refuse(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("a dry run must not post")

    code, text = await run_relay(tmp_path, httpx.MockTransport(refuse), dry_run=True)
    assert code == 0 and '"cpu"' in text
    assert not (tmp_path / "state.json").exists()


async def test_a_post_keeps_state_and_exits_zero(tmp_path: Path) -> None:
    seen: list[httpx.Request] = []

    def accept(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"kept": True, "samples": 7})

    code, text = await run_relay(tmp_path, httpx.MockTransport(accept))
    assert code == 0 and "7 samples" in text
    assert seen[0].url.path == "/v1/ae/box-health"
    assert json.loads(seen[0].content)["cpu"]["threads"] == 2
    state = json.loads((tmp_path / "state.json").read_text())
    assert state["cpu_stat"] and state["slow"]
    assert (tmp_path / "state.json").stat().st_mode & 0o777 == 0o600


async def test_a_401_says_to_log_in_again(tmp_path: Path) -> None:
    code, text = await run_relay(
        tmp_path, httpx.MockTransport(lambda _r: httpx.Response(401, json={}))
    )
    assert code == 1 and "omnigent login" in text


async def test_a_failed_part_makes_the_exit_status_one(tmp_path: Path) -> None:
    code, text = await run_relay(
        tmp_path,
        httpx.MockTransport(lambda _r: httpx.Response(200, json={"kept": True, "samples": 1})),
        answers={**container_answers(), "journal": RunResult(1)},
    )
    assert code == 1 and "journal" in text


# ── review of PR 389 (2026-10-28) ───────────────────────────────────────────


def test_folders_are_posted_by_label_and_a_bare_path_is_folder_n() -> None:
    assert relay.parse_dirs("cache=~/.cache, npm=~/.npm") == [
        ("cache", "~/.cache"),
        ("npm", "~/.npm"),
    ]
    assert relay.parse_dirs(f"/mnt/{DIR_T},data=/srv/x,../bad=/y") == [
        ("folder 1", f"/mnt/{DIR_T}"),
        ("data", "/srv/x"),
        ("folder 3", "/y"),
    ]


def test_other_mounts_are_numbered_and_never_named(tmp_path: Path) -> None:
    roots = fake_box(tmp_path)
    rows = relay.read_mounts(roots.mounts, [], fake_statvfs)
    assert not any(MOUNT_T in m.path for m in rows)
    other = next(m for m in rows if m.path.startswith("other mount"))
    assert other.path == "other mount 1" and other.size_b > 0
    with pytest.raises(ValueError):
        box_health.Mount(path=f"/mnt/{MOUNT_T}", fstype="ext4", size_b=1, used_b=0, avail_b=1)


async def test_a_refusal_never_prints_the_response_text(tmp_path: Path) -> None:
    code, text = await run_relay(
        tmp_path,
        httpx.MockTransport(lambda _r: httpx.Response(500, text=f"boom {EXC_T} /mnt/{MOUNT_T}")),
    )
    assert code == 1 and EXC_T not in text and MOUNT_T not in text and "500" in text


def test_an_exception_message_never_reaches_the_post_or_the_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    fake_tree(tmp_path)

    def boom(*_a: Any, **_k: Any) -> Any:
        raise OSError(13, f"Permission denied: '/mnt/{EXC_T}'")

    monkeypatch.setattr(relay, "read_memory", boom)
    monkeypatch.setattr(box_health_scan, "scan_claude_root", boom)
    post, state, failed = collect(tmp_path, FakeRunner(container_answers()))
    assert "memory" in failed and any(p.startswith("user ") for p in failed)
    assert EXC_T not in "\n".join(
        [post.model_dump_json(), json.dumps(state), caplog.text, *capsys.readouterr()]
    )

    async def exploding(**_k: Any) -> int:
        raise OSError(13, f"Permission denied: '/mnt/{EXC_T}'")

    monkeypatch.setattr(relay, "run", exploding)
    assert relay.main(["--dry-run"]) == 1
    seen = capsys.readouterr()
    assert EXC_T not in seen.out + seen.err and "PermissionError" in seen.err


def test_a_container_walk_that_timed_out_is_partial_and_counts_nothing_stale(
    tmp_path: Path,
) -> None:
    answers = container_answers()
    answers["claude_tree"] = StreamResult(lines=answers["claude_tree"], returncode=124)
    post, _, failed, _ = full_run(tmp_path, answers)
    userb = next(u for u in post.slow.users if u.kind == "container")
    root = userb.claude[0]
    assert root.partial and root.stale_sessions == 0
    assert "container UserB scan" in failed


def test_a_claude_name_with_odd_digits_in_a_container_is_litter_not_a_crash(
    tmp_path: Path,
) -> None:
    answers = container_answers()
    answers["tmp_list"] = [
        f"d\t1001\t{NOW}\tclaude-١٢\n",
        f"d\t1001\t{NOW}\tclaude-12345678901\n",
        f"d\t1001\t{NOW}\tclaude-7\n",
    ]
    post, _, failed, runner = full_run(tmp_path, answers)
    asked = [
        c
        for c in runner.calls
        if "find" in c and "-newermt" not in c and any(p.startswith("/tmp/claude-") for p in c)
    ]
    assert [p for c in asked for p in c if p.startswith("/tmp/claude-")] == ["/tmp/claude-7"]
    userb = next(u for u in post.slow.users if u.kind == "container")
    assert sum(g.entries for g in userb.litter) == 2 and "container UserB" not in failed


def test_a_value_error_in_a_container_part_is_a_failed_part(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_tree(tmp_path)
    real = relay.container_argv

    def picky(key: str, **kw: Any) -> list[str]:
        if key == "claude_tree":
            raise ValueError("not digits")
        return real(key, **kw)

    monkeypatch.setattr(relay, "container_argv", picky)
    post, _, failed = collect(tmp_path, FakeRunner(container_answers()))
    assert "container UserB" in failed and post.cpu is not None


def test_docker_volume_sizes_are_read_once_a_day_and_cached_by_hash(tmp_path: Path) -> None:
    fake_tree(tmp_path)

    def pass_at(state: dict[str, Any], when: float) -> tuple[FakeRunner, dict[str, Any]]:
        runner = FakeRunner(container_answers())
        _, new_state, _ = relay.collect(
            settings_for(tmp_path),
            runner=runner,
            roots=fake_box(tmp_path),
            state=state,
            now=when,
            force_slow=True,
            home=str(tmp_path),
        )
        return runner, new_state

    first, state = pass_at({}, NOW)
    second, state = pass_at(state, NOW + 2 * 3600)
    third, state = pass_at(state, NOW + 25 * 3600)
    count = lambda r: sum(1 for c in r.calls if r.key(c) == "volumes")  # noqa: E731
    assert (count(first), count(second), count(third)) == (1, 0, 1)
    assert VOL_T not in json.dumps(state)
    assert set(state["volumes"]["sizes"]) and all(len(k) == 16 for k in state["volumes"]["sizes"])


async def test_the_fast_reading_is_posted_before_the_slow_scan_and_survives_its_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(relay, "SLOW_DEADLINE_S", 0.0)
    bodies: list[dict[str, Any]] = []

    def accept(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json={"kept": True, "samples": len(bodies)})

    code, _text = await run_relay(tmp_path, httpx.MockTransport(accept))
    assert len(bodies) == 2
    first, final = bodies
    assert first["cpu"]["threads"] == 2 and first["slow"] is None
    assert "slow scans cut short" in final["failed"] and code == 1
    assert final["slow"] is not None and final["cpu"] == first["cpu"]


async def test_a_broken_slow_scan_still_leaves_the_fast_reading_posted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def explode(*_a: Any, **_k: Any) -> Any:
        raise RuntimeError(f"disk {EXC_T} on fire")

    monkeypatch.setattr(relay, "slow_scan", explode)
    bodies: list[dict[str, Any]] = []

    def accept(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json={"kept": True, "samples": 1})

    code, text = await run_relay(tmp_path, httpx.MockTransport(accept))
    assert len(bodies) == 2 and bodies[0]["cpu"] is not None
    assert "slow scans" in bodies[1]["failed"] and code == 1 and EXC_T not in text


def test_the_slow_pass_hands_each_command_the_time_left() -> None:
    now = [0.0]
    deadline = relay.Deadline(150, clock=lambda: now[0])
    seen: list[float] = []

    class Inner:
        def run(self, argv: Any, timeout: float, env: Any = None) -> RunResult:
            seen.append(timeout)
            return RunResult(0, "")

        def stream(self, argv: Any, timeout: float, max_lines: int) -> StreamResult:
            seen.append(timeout)
            return StreamResult()

    runner = relay.DeadlineRunner(Inner(), deadline)
    runner.run(["x"], 120)
    now[0] = 100.0
    runner.run(["x"], 120)
    runner.stream(["x"], 120, 10)
    now[0] = 151.0
    assert runner.run(["x"], 120).returncode == 124
    assert runner.stream(["x"], 120, 10).returncode == 124
    assert seen == [120, 50.0, 50.0]


def test_a_container_without_a_worktrees_folder_is_not_a_failure(tmp_path: Path) -> None:
    answers = container_answers()
    answers["worktrees_list"] = StreamResult(lines=[], returncode=1)
    post, _, failed, _ = full_run(tmp_path, answers)
    userb = next(u for u in post.slow.users if u.kind == "container")
    assert userb.worktrees is None and "container UserB scan" not in failed


def test_find_exit_1_with_output_is_a_complete_walk_that_noted_unreadable_folders(
    tmp_path: Path,
) -> None:
    answers = container_answers()
    answers["claude_tree"] = StreamResult(lines=answers["claude_tree"], returncode=1)
    post, _, failed, _ = full_run(tmp_path, answers)
    userb = next(u for u in post.slow.users if u.kind == "container")
    root = userb.claude[0]
    assert not root.partial and root.had_unreadable
    assert root.stale_sessions == 1
    assert "container UserB scan" not in failed


@pytest.mark.parametrize("code", [124, 128, 137, 143])
def test_a_timeout_or_signal_exit_is_partial_and_a_failed_part(tmp_path: Path, code: int) -> None:
    answers = container_answers()
    answers["claude_tree"] = StreamResult(lines=answers["claude_tree"], returncode=code)
    post, _, failed, _ = full_run(tmp_path, answers)
    root = next(u for u in post.slow.users if u.kind == "container").claude[0]
    assert root.partial and root.stale_sessions == 0 and not root.had_unreadable
    assert "container UserB scan" in failed


def test_find_exit_1_with_no_output_is_a_failure_for_a_tree_walk(tmp_path: Path) -> None:
    answers = container_answers()
    answers["claude_tree"] = StreamResult(lines=[], returncode=1)
    _, _, failed, _ = full_run(tmp_path, answers)
    assert "container UserB scan" in failed


async def test_the_state_is_saved_after_the_first_post_even_if_the_slow_pass_dies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def die(*_a: Any, **_k: Any) -> Any:
        raise KeyboardInterrupt

    monkeypatch.setattr(relay, "run_slow", die)
    seen: list[int] = []

    def accept(request: httpx.Request) -> httpx.Response:
        seen.append(1)
        return httpx.Response(200, json={"kept": True, "samples": 1})

    with pytest.raises(KeyboardInterrupt):
        await run_relay(tmp_path, httpx.MockTransport(accept))
    state = json.loads((tmp_path / "state.json").read_text())
    assert len(seen) == 1 and state["cpu_stat"]["total"] == 10000


# ── cleanup buttons and container aging (2026-10-29) ────────────────────────

RM_PREFIX = [
    "docker",
    "exec",
    "-u",
    "agent",
    "omnigent-host-example",
    "timeout",
    "120",
    "rm",
    "-rf",
    "--one-file-system",
    "--",
]
SESSION_PATH = f"/tmp/claude-1001/-{TOKENS[0]}/{UUID_A}"
PYTEST_PATH = "/tmp/pytest-of-agent/pytest-3"


def rm_calls(runner: FakeRunner) -> list[list[str]]:
    return [c for c in runner.calls if "rm" in c and c[:2] == ["docker", "exec"]]


def test_old_container_folders_are_aged_with_the_one_fixed_rm_template(tmp_path: Path) -> None:
    post, _, failed, runner = full_run(tmp_path)
    calls = rm_calls(runner)
    assert calls == [[*RM_PREFIX, SESSION_PATH, PYTEST_PATH]]
    assert relay.matches_container_template(calls[0], ["omnigent-host-example"], "agent")
    userb = next(u for u in post.slow.users if u.kind == "container")
    assert (userb.claude[0].sessions, userb.claude[0].stale_sessions) == (0, 0)
    assert userb.pytest and userb.pytest.stale == 0
    assert post.slow.cleanup and post.slow.cleanup.auto_aged[0].owner == "UserB"
    assert post.slow.cleanup.auto_aged[0].count == 2
    assert not [a for a in post.slow.cleanup.actions if a.owner == "UserB"]
    assert TOKENS[0] not in post.model_dump_json() and "cleanup" not in failed


def test_with_cleanup_off_nothing_is_removed_and_no_button_is_offered(tmp_path: Path) -> None:
    fake_tree(tmp_path)
    runner = FakeRunner(container_answers())
    settings = settings_for(tmp_path, AE_BOX_HEALTH_CLEANUP="off")
    post, _, _ = collect(tmp_path, runner, settings=settings)
    assert rm_calls(runner) == []
    assert post.slow.cleanup and post.slow.cleanup.enabled is False
    assert post.slow.cleanup.actions == [] and post.slow.cleanup.auto_aged == []


def test_a_cut_walk_removes_nothing(tmp_path: Path) -> None:
    answers = container_answers()
    answers["claude_tree"] = StreamResult(lines=answers["claude_tree"], truncated=True)
    answers["pytest_tree"] = StreamResult(lines=answers["pytest_tree"], returncode=124)
    _, _, _, runner = full_run(tmp_path, answers)
    assert rm_calls(runner) == []


def test_a_tree_with_a_fresh_file_deep_inside_is_kept(tmp_path: Path) -> None:
    answers = container_answers()
    answers["claude_tree"] = [
        *answers["claude_tree"],
        f"6\tf\t{NOW - 60}\t8\t-{TOKENS[0]}/{UUID_A}/a/b/c/d/late.txt\n",
    ]
    _, _, _, runner = full_run(tmp_path, answers)
    assert SESSION_PATH not in " ".join(" ".join(c) for c in rm_calls(runner))


def test_only_paths_that_match_the_strict_patterns_reach_rm(tmp_path: Path) -> None:
    _, _, _, runner = full_run(tmp_path)
    mapped = ["omnigent-host-example"]
    for bad in (
        "/tmp/claude-1001/../../etc",
        "/tmp/claude-1001/-p/not-a-uuid",
        f"/tmp/claude-1001/p/{UUID_A}",
        f"/tmp/claude-1001/-p/{UUID_A}/extra",
        f"/tmp/claude-1001/-p;x/{UUID_A}",
        "/tmp/pytest-of-root/pytest-3",
        "/home/agent",
        "/",
        f"/tmp/claude-1001/-p/{UUID_A}\n",
    ):
        with pytest.raises(ValueError):
            relay.container_rm_argv("omnigent-host-example", "agent", [bad])
        assert not relay.matches_container_template([*RM_PREFIX, bad], mapped, "agent")
    assert not relay.matches_container_template(
        [*RM_PREFIX, SESSION_PATH], ["someone-else"], "agent"
    )
    assert not relay.matches_container_template(
        ["docker", "exec", "-u", "root", "omnigent-host-example", "rm", "-rf", "--", SESSION_PATH],
        mapped,
        "agent",
    )
    with pytest.raises(ValueError):
        relay.container_rm_argv("omnigent-host-example", "agent", [SESSION_PATH] * 201)
    with pytest.raises(ValueError):
        relay.container_rm_argv("omnigent-host-example", "root", [SESSION_PATH])
    assert runner.calls


def test_rm_runs_in_batches_of_two_hundred(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    runner = FakeRunner({"uid": ["1001\n"], **LIVE})
    paths = [f"/tmp/claude-1001/-p/{i:08x}-1111-4222-8333-444455556666" for i in range(450)]
    removed = relay.container_remove("omnigent-host-example", settings, runner, paths)
    assert len(removed.done) == 450
    assert [len(c) - len(RM_PREFIX) for c in rm_calls(runner)] == [200, 200, 50]


def test_the_host_offer_counts_owners_old_items(tmp_path: Path) -> None:
    fake_tree(tmp_path)
    litter = tmp_path / "tmp" / "sec-appr-AbC123"
    write(litter / "f", "x")
    age(litter / "f", 5)
    age(litter, 5)
    post, _, _ = collect(tmp_path, FakeRunner(container_answers()))
    actions = {(a.action, a.owner): a for a in post.slow.cleanup.actions}
    assert actions[("claude_scratch", "UserA")].count == 1
    assert actions[("tmp_litter", "UserA")].count == 1
    assert actions[("tmp_litter", "UserA")].groups[0].group == "sec-appr-*"
    assert actions[("docker_prune", "all")].bytes > 0
    assert "AbC123" not in post.model_dump_json()


def test_a_request_for_something_the_relay_did_not_offer_is_refused(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    for request in (
        {"id": "a" * 32, "action": "rm_rf", "owner": "UserA"},
        {"id": "a" * 32, "action": "tmp_litter", "owner": "UserB"},
        {"id": "a" * 32, "action": "tmp_litter", "owner": "Nobody"},
        {"id": "a" * 32, "action": "docker_prune", "owner": "UserA"},
    ):
        result = relay.run_cleanup_request(
            request, settings, FakeRunner(), now=NOW, home=str(tmp_path)
        )
        assert result["ok"] is False and result["errors"] == ["NOT_POSSIBLE"], request
    off = settings_for(tmp_path, AE_BOX_HEALTH_CLEANUP="off")
    gone = relay.run_cleanup_request(
        {"id": "a" * 32, "action": "tmp_litter", "owner": "UserA"},
        off,
        FakeRunner(),
        now=NOW,
        home=str(tmp_path),
    )
    assert gone["errors"] == ["DISABLED"]


def test_a_container_request_lists_proves_and_removes(tmp_path: Path) -> None:
    runner = FakeRunner(container_answers())
    result = relay.run_cleanup_request(
        {"id": "a" * 32, "action": "claude_scratch", "owner": "UserB"},
        settings_for(tmp_path),
        runner,
        now=NOW,
        home=str(tmp_path),
    )
    assert result["ok"] and result["removed"] == 2
    assert rm_calls(runner) == [[*RM_PREFIX, SESSION_PATH, PYTEST_PATH]]
    assert TOKENS[0] not in json.dumps(result)


async def test_the_post_answer_hands_over_a_request_the_relay_runs_and_reports(
    tmp_path: Path,
) -> None:
    fake_tree(tmp_path)
    litter = tmp_path / "tmp" / "sec-appr-AbC123"
    write(litter / "f", "x")
    age(litter / "f", 5)
    age(litter, 5)
    seen: list[tuple[str, dict[str, Any]]] = []
    posts = 0

    def answer(request: httpx.Request) -> httpx.Response:
        nonlocal posts
        payload = json.loads(request.content)
        seen.append((request.url.path, payload))
        if request.url.path == "/v1/ae/box-health":
            posts += 1
            extra = (
                {"cleanup": {"id": "c" * 32, "action": "tmp_litter", "owner": "UserA", "count": 1}}
                if posts == 1
                else {}
            )
            return httpx.Response(200, json={"kept": True, "samples": posts, **extra})
        return httpx.Response(200, json={"kept": True})

    code, text = await run_relay(tmp_path, httpx.MockTransport(answer))
    assert code == 0, text
    paths = [p for p, _ in seen]
    assert paths == [
        "/v1/ae/box-health",
        "/v1/ae/box-health/cleanup/result",
        "/v1/ae/box-health",
    ]
    result = seen[1][1]
    assert result["id"] == "c" * 32 and result["ok"] and result["removed"] == 1
    assert not litter.exists()
    # The rescan after the run no longer offers the leftovers.
    offered = {a["action"] for a in seen[2][1]["slow"]["cleanup"]["actions"]}
    assert "tmp_litter" not in offered
    assert "AbC123" not in json.dumps(seen)


# ── round 1 review of PR 395 ────────────────────────────────────────────────

PROJECT_PATH = f"/tmp/claude-1001/-{TOKENS[0]}"


async def test_a_dry_run_never_removes_anything_in_a_container(tmp_path: Path) -> None:
    fake_tree(tmp_path)
    runner = FakeRunner(container_answers())
    out = io.StringIO()
    code = await relay.run(
        settings=settings_for(tmp_path),
        omni_settings=omni.Settings(server_url="https://omni.test", token="t"),
        runner=runner,
        roots=fake_box(tmp_path),
        dry_run=True,
        out=out,
        now=NOW,
        home=str(tmp_path),
    )
    assert code == 0, out.getvalue()
    assert rm_calls(runner) == []
    assert not [c for c in runner.calls if relay.container_path_ok(c[-1], "agent")]
    text = out.getvalue()
    body = json.loads(text[text.index('{\n  "') :])
    aged = body["slow"]["cleanup"]["auto_aged"]
    assert aged and aged[0]["count"] == 2  # what it would age, as counts
    userb = next(u for u in body["slow"]["users"] if u["kind"] == "container")
    assert userb["claude"][0]["stale_sessions"] == 1  # nothing was subtracted


def test_dry_run_settings_stop_container_remove_itself(tmp_path: Path) -> None:
    settings = dataclasses.replace(settings_for(tmp_path), dry_run=True)
    runner = FakeRunner()
    removed = relay.container_remove("omnigent-host-example", settings, runner, [SESSION_PATH])
    assert removed.done == set() and runner.calls == []


def aged_paths(runner: FakeRunner) -> str:
    return " ".join(" ".join(c) for c in rm_calls(runner))


@pytest.mark.parametrize(
    ("check", "lines"),
    [
        ("fresh", [f"{SESSION_PATH}/tasks/new.txt\n"]),
        ("procs", ["/proc/7/cwd\t/home/agent\0", f"/proc/9/cwd\t{SESSION_PATH}/tasks\0"]),
        ("procs", ["/proc/7/cwd\t/home/agent\0", f"/proc/9/fd/4\t{SESSION_PATH}\0"]),
        (
            "procs",
            ["/proc/7/cwd\t/home/agent\0", f"/proc/9/cwd\t{PROJECT_PATH}\0"],
        ),  # a process works in the project folder above it
        ("cmdlines", [f"claude\0--resume\0{UUID_A}\0"]),
        ("cmdlines", [f"claude\0--session-id={UUID_A}\0"]),
    ],
)
def test_a_container_folder_in_use_or_changed_lately_is_kept(
    tmp_path: Path, check: str, lines: list[str]
) -> None:
    answers = container_answers()
    answers[check] = lines
    _, _, _, runner = full_run(tmp_path, answers)
    assert SESSION_PATH not in aged_paths(runner)
    assert PYTEST_PATH in aged_paths(runner)  # the unrelated run is still aged


def test_the_root_or_tmp_as_a_working_folder_does_not_protect_everything(tmp_path: Path) -> None:
    answers = container_answers()
    answers["procs"] = [
        "/proc/7/cwd\t/home/agent\0",
        "/proc/9/cwd\t/\0",
        "/proc/9/fd/3\t/tmp\0",
        "/proc/9/fd/4\tpipe:[1]\0",
    ]
    _, _, _, runner = full_run(tmp_path, answers)
    assert SESSION_PATH in aged_paths(runner)


@pytest.mark.parametrize("check", ["fresh", "uid", "uids", "procs", "cmdlines"])
@pytest.mark.parametrize("how", ["exit", "cut", "timeout"])
def test_a_check_that_fails_or_is_cut_short_removes_nothing(
    tmp_path: Path, check: str, how: str
) -> None:
    answers = container_answers()
    answers[check] = {
        "exit": StreamResult(returncode=2),
        "cut": StreamResult(truncated=True),
        "timeout": StreamResult(returncode=124),
    }[how]
    post, _, failed, runner = full_run(tmp_path, answers)
    assert rm_calls(runner) == []
    assert "container UserB cleanup" in failed
    userb = next(u for u in post.slow.users if u.kind == "container")
    assert userb.claude[0].stale_sessions == 1  # still reported, not subtracted


def test_the_guard_runs_right_before_every_rm_batch_with_fixed_argv(tmp_path: Path) -> None:
    _, _, _, runner = full_run(tmp_path)
    keys = [runner.key(c) for c in runner.calls if c[:2] == ["docker", "exec"]]
    at = keys.index("rm")
    assert keys[at - 5 : at] == ["fresh", "uid", "uids", "procs", "cmdlines"]
    mapped = ["omnigent-host-example"]
    for argv in runner.calls:
        if runner.key(argv) in {"fresh", "uid", "uids", "procs", "cmdlines"}:
            assert relay.matches_container_template(argv, mapped, "agent"), argv
            assert argv[2:5] == ["-u", "agent", "omnigent-host-example"]
            assert "timeout" in argv and not {"sh", "bash", "-c"} & set(argv)


def test_the_new_templates_are_pinned_and_refuse_other_shapes() -> None:
    mapped = ["omnigent-host-example"]
    fresh = relay.container_fresh_argv("omnigent-host-example", "agent", [SESSION_PATH], 1234)
    assert fresh == [
        "docker", "exec", "-u", "agent", "omnigent-host-example", "timeout", "30", "find",
        SESSION_PATH, "-newermt", "@1234", "-print",
    ]  # fmt: skip
    assert relay.matches_container_template(fresh, mapped, "agent")
    for bad in (
        [*fresh[:-3], "-newermt", "@12x4", "-print"],
        [*fresh[:-3], "-newermt", "@1234", "-delete"],
        [*fresh[:8], "/etc", *fresh[9:]],
        [*fresh[:8], "-o", *fresh[9:]],
    ):
        assert not relay.matches_container_template(bad, mapped, "agent"), bad
    procs = relay.container_argv("procs", container="omnigent-host-example", user="agent")
    assert procs[7:9] == ["find", "/proc"] and procs[-2:] == ["-printf", "%p\t%l\\0"]
    uid = relay.container_argv("uid", container="omnigent-host-example", user="agent")
    assert uid[-3:] == ["timeout", "30", "id"] or uid[-2:] == ["id", "-u"]
    uids = relay.container_argv("uids", container="omnigent-host-example", user="agent")
    assert uids[5:] == [
        "timeout", "30", "find", "/proc", "-mindepth", "2", "-maxdepth", "2",
        "(", "-path", "/proc/[0-9]*/status",
        "-exec", "grep", "-H", "-e", "^Uid:", "-e", "^State:", "-e", "^Threads:", "{}", "+", ")",
        "-o",
        "(", "-path", "/proc/[0-9]*/cmdline", "-exec", "grep", "-a", "-H", "", "{}", "+", ")",
    ]  # fmt: skip
    assert relay.matches_container_template(uid, mapped, "agent")
    assert relay.matches_container_template(uids, mapped, "agent")
    assert not relay.matches_container_template([*uids, "/etc/shadow"], mapped, "agent")
    assert relay.matches_container_template(procs, mapped, "agent")
    cmd = relay.container_argv("cmdlines", container="omnigent-host-example", user="agent")
    assert cmd[-4:] == ["-exec", "cat", "{}", "+"]
    assert not relay.matches_container_template([*cmd, "/etc/shadow"], mapped, "agent")
    with pytest.raises(ValueError):
        relay.container_fresh_argv("omnigent-host-example", "agent", ["/etc"], 1)


def test_the_container_item_limit_keeps_the_oldest_and_ties_go_by_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    answers = container_answers()
    answers["claude_tree"] = [
        f"1\td\t{NOW - 3 * DAY}\t8\t-{TOKENS[0]}\n",
        f"2\td\t{NOW - 3 * DAY}\t8\t-{TOKENS[0]}/{UUID_A}\n",
        f"3\tf\t{NOW - 3 * DAY}\t8\t-{TOKENS[0]}/{UUID_A}/{TOKENS[1]}.txt\n",
    ]
    answers["pytest_tree"] = [f"1\td\t{NOW - 6 * DAY}\t8\tpytest-3\n"]
    monkeypatch.setattr(relay.box_cleanup, "MAX_ITEMS", 1)
    runner = FakeRunner(answers)
    result = relay.run_cleanup_request(
        {"id": "a" * 32, "action": "claude_scratch", "owner": "UserB", "count": 5},
        settings_for(tmp_path),
        runner,
        now=NOW,
        home=str(tmp_path),
    )
    assert result["removed"] == 1 and rm_calls(runner) == [[*RM_PREFIX, PYTEST_PATH]]
    # Equal ages: the path that sorts first goes first.
    answers["claude_tree"] = [
        f"1\td\t{NOW - 6 * DAY}\t8\t-{TOKENS[0]}\n",
        f"2\td\t{NOW - 6 * DAY}\t8\t-{TOKENS[0]}/{UUID_A}\n",
        f"3\tf\t{NOW - 6 * DAY}\t8\t-{TOKENS[0]}/{UUID_A}/{TOKENS[1]}.txt\n",
    ]
    runner = FakeRunner(answers)
    relay.run_cleanup_request(
        {"id": "a" * 32, "action": "claude_scratch", "owner": "UserB", "count": 5},
        settings_for(tmp_path),
        runner,
        now=NOW,
        home=str(tmp_path),
    )
    assert rm_calls(runner) == [[*RM_PREFIX, SESSION_PATH]]  # "/tmp/claude-" < "/tmp/pytest-"


def test_a_request_bound_to_a_smaller_count_removes_only_the_oldest(tmp_path: Path) -> None:
    answers = container_answers()
    answers["claude_tree"] = [
        f"1\td\t{NOW - 3 * DAY}\t8\t-{TOKENS[0]}\n",
        f"2\td\t{NOW - 3 * DAY}\t8\t-{TOKENS[0]}/{UUID_A}\n",
        f"3\tf\t{NOW - 3 * DAY}\t8\t-{TOKENS[0]}/{UUID_A}/{TOKENS[1]}.txt\n",
    ]
    answers["pytest_tree"] = [f"1\td\t{NOW - 6 * DAY}\t8\tpytest-3\n"]
    runner = FakeRunner(answers)
    result = relay.run_cleanup_request(
        {"id": "a" * 32, "action": "claude_scratch", "owner": "UserB", "count": 1},
        settings_for(tmp_path),
        runner,
        now=NOW,
        home=str(tmp_path),
    )
    assert result["ok"] is True and result["errors"] == [] and result["removed"] == 1
    assert result["skipped"] == {"over_confirmed": 1}
    assert rm_calls(runner) == [[*RM_PREFIX, PYTEST_PATH]]  # the older of the two
    tmp = tmp_path / "tmp"
    litter = tmp / "sec-appr-AbC123"
    write(litter / "f", "x")
    age(litter / "f", 5)
    age(litter, 5)
    host = relay.run_cleanup_request(
        {"id": "a" * 32, "action": "tmp_litter", "owner": "UserA", "count": 0},
        settings_for(tmp_path),
        FakeRunner(),
        now=NOW,
        home=str(tmp_path),
        tmp_root=str(tmp),
    )
    assert host["errors"] == [] and host["removed"] == 0
    assert host["skipped"] == {"over_confirmed": 1}
    assert litter.exists()
    exact = relay.run_cleanup_request(
        {"id": "a" * 32, "action": "tmp_litter", "owner": "UserA", "count": 1},
        settings_for(tmp_path),
        FakeRunner(),
        now=NOW,
        home=str(tmp_path),
        tmp_root=str(tmp),
    )
    assert exact["ok"] and exact["removed"] == 1 and not litter.exists()


def test_a_cleanup_past_its_deadline_stops_cleanly_with_a_partial_result(
    tmp_path: Path,
) -> None:
    runner = FakeRunner(container_answers())
    result = relay.run_cleanup_request(
        {"id": "a" * 32, "action": "claude_scratch", "owner": "UserB", "count": 5},
        settings_for(tmp_path),
        runner,
        now=NOW,
        home=str(tmp_path),
        deadline_s=0,
    )
    assert result["partial"] is True and result["removed"] == 0 and rm_calls(runner) == []


def test_a_container_offer_counts_test_runs_in_its_files_and_bytes(tmp_path: Path) -> None:
    fake_tree(tmp_path)
    settings = dataclasses.replace(settings_for(tmp_path), dry_run=True)
    post, _, _ = collect(tmp_path, FakeRunner(container_answers()), settings=settings)
    userb = next(a for a in post.slow.cleanup.actions if a.owner == "UserB")
    assert userb.count == 2 and userb.files == 3 and userb.bytes == 3 * 8 * 512


def test_the_whole_pass_leaves_room_under_the_units_timeout() -> None:
    assert relay.CLEANUP_DEADLINE_S + relay.SLOW_DEADLINE_S > relay.PASS_DEADLINE_S
    assert relay.PASS_DEADLINE_S < 240  # TimeoutStartSec=4min in the unit
    unit = (
        Path(__file__).resolve().parents[3] / "deploy/systemd/user/ae-omnigent-box-health.service"
    )
    assert "TimeoutStartSec=4min" in unit.read_text(encoding="utf-8")


# ── round 2 of the PR 395 review ────────────────────────────────────────────


class SeqRunner(FakeRunner):
    """Answers a check from a list, one entry per call (the last one repeats)."""

    def stream(self, argv: Sequence[str], timeout: float, max_lines: int) -> StreamResult:
        key = self.key(argv)
        answer = self.answers.get(key)
        if isinstance(answer, list) and answer and isinstance(answer[0], list):
            self.calls.append(list(argv))
            lines = answer.pop(0) if len(answer) > 1 else answer[0]
            return StreamResult(lines=list(lines)[:max_lines])
        return super().stream(argv, timeout, max_lines)


def test_a_process_with_no_cwd_line_stops_the_batch_even_when_find_exits_one(
    tmp_path: Path,
) -> None:
    # pid 7 is the container user's, but set PR_SET_DUMPABLE=0: its links are unreadable, so
    # it is missing from the link listing, and find still exits 1 as it always does here.
    answers = container_answers()
    answers["uids"] = status(7)
    answers["procs"] = StreamResult(lines=[], returncode=1)
    _, _, failed, runner = full_run(tmp_path, answers)
    assert rm_calls(runner) == [] and "container userb cleanup" in [f.lower() for f in failed]
    assert [runner.key(c) for c in runner.calls].count("procs") == 2  # looked once more


def test_a_gap_that_closes_on_the_second_look_lets_the_batch_go(tmp_path: Path) -> None:
    answers = container_answers()
    answers["uids"] = status(7)
    answers["procs"] = [[], ["/proc/7/cwd\t/home/agent\0"]]
    fake_tree(tmp_path)
    runner = SeqRunner(answers)
    collect(tmp_path, runner)
    assert len(rm_calls(runner)) == 1


HELPER = relay._uids_helper_argvs("omnigent-host-example", "agent")


def status(
    pid: int,
    uid: str = "1001",
    state: str = "S (sleeping)",
    threads: int = 1,
    euid: str | None = None,
) -> list[str]:
    euid = uid if euid is None else euid
    return [
        f"/proc/{pid}/status:Uid:\t{uid}\t{euid}\t{uid}\t{uid}\n",
        f"/proc/{pid}/status:State:\t{state}\n",
        f"/proc/{pid}/status:Threads:\t{threads}\n",
    ]


def cmdline(pid: int, *argv: str) -> str:
    return f"/proc/{pid}/cmdline:" + "\0".join(argv) + "\0\n"


def test_other_users_and_links_that_are_not_paths_are_not_gaps() -> None:
    gap, targets = relay._proc_gap(
        [*status(7), *status(9, "0")],
        ["/proc/7/cwd\t/home/agent\0", "/proc/7/fd/3\tpipe:[5]\0", "/proc/7/fd/4\t/tmp\0"],
        "1001",
    )
    assert gap == set() and targets == ["/home/agent"]
    gap, _ = relay._proc_gap(status(7), [], "1001")
    assert gap == {"7"}


def test_a_zombie_of_the_container_user_is_not_a_gap() -> None:
    # A zombie has no cwd link and the container has no init to reap it; it holds nothing.
    gap, _ = relay._proc_gap(
        [*status(7), *status(8, state="Z (zombie)")], ["/proc/7/cwd\t/home/agent\0"], "1001"
    )
    assert gap == set()
    gap, _ = relay._proc_gap([*status(8, state="Z (zombie)"), *status(9)], [], "1001")
    assert gap == {"9"}  # a live process without a cwd line still stops the batch


def test_a_status_listing_without_any_uid_line_is_an_error() -> None:
    with pytest.raises(ValueError):
        relay._proc_gap([], ["/proc/7/cwd\t/home/agent\0"], "1001")
    with pytest.raises(ValueError):
        relay._proc_gap(["/proc/7/status:State:\tS (sleeping)\n"], [], "1001")


def test_a_failed_status_read_removes_nothing_even_with_exit_one(tmp_path: Path) -> None:
    answers = container_answers()
    answers["uids"] = StreamResult(lines=[], returncode=1)
    _, _, failed, runner = full_run(tmp_path, answers)
    assert rm_calls(runner) == [] and "container UserB cleanup" in failed


def test_a_zombie_does_not_stall_container_cleanup(tmp_path: Path) -> None:
    answers = container_answers()
    answers["uids"] = [*status(7), *status(8, state="Z (zombie)")]  # one thread, nothing left
    _, _, failed, runner = full_run(tmp_path, answers)
    assert len(rm_calls(runner)) == 1 and "container UserB cleanup" not in failed


def test_a_helper_is_ignored_only_when_its_command_line_is_the_pinned_one() -> None:
    inner, execs = HELPER
    timeout_argv = list(inner)
    find_argv = list(inner[2:])
    grep_argv = [*execs[0], "/proc/7/status", "/proc/8/status"]
    cat_argv = [*execs[1], "/proc/7/cmdline"]
    listing = [
        *status(7),
        *status(20),
        *status(21),
        *status(22),
        *status(23),
        cmdline(20, *timeout_argv),
        cmdline(21, *find_argv),
        cmdline(22, *grep_argv),
        cmdline(23, *cat_argv),
    ]
    links = ["/proc/7/cwd\t/home/agent\0"]
    assert relay._proc_gap(listing, links, "1001", HELPER)[0] == set()
    # A process that is merely called find, or whose arguments differ, is not the check.
    for pid, argv in (
        (30, ["find", "/home/agent", "-name", "x"]),
        (31, ["timeout", "30", "find", "/proc", "-delete"]),
        (32, [*execs[0], "/etc/shadow"]),
        (33, [*execs[0]]),
        (34, ["grep", "-H", "-e", "^Uid:", "/proc/7/status"]),
    ):
        gap, _ = relay._proc_gap([*status(pid), cmdline(pid, *argv)], [], "1001", HELPER)
        assert gap == {str(pid)}, argv
    # With no command line read for it, a pid is never taken for a helper.
    assert relay._proc_gap(status(40), [], "1001", HELPER)[0] == {"40"}


ZOMBIE = "Z (zombie)"


def test_a_zombie_with_other_threads_still_running_is_a_gap() -> None:
    # The main thread exited, the other threads run: State is Z, the leader's cwd and fds are
    # unreadable and the threads under /proc/<pid>/task are never listed.
    listing = [*status(7), *status(8, state=ZOMBIE, threads=2)]
    links = ["/proc/7/cwd\t/home/agent\0"]
    assert relay._proc_gap(listing, links, "1001")[0] == {"8"}
    # Even a readable cwd line does not clear it: the threads' own folders are unknown.
    assert relay._proc_gap(listing, [*links, "/proc/8/cwd\t/home/agent\0"], "1001")[0] == {"8"}
    assert relay._proc_gap([*status(7), *status(8, state=ZOMBIE)], links, "1001")[0] == set()


def test_a_threaded_zombie_stops_container_cleanup(tmp_path: Path) -> None:
    answers = container_answers()
    answers["uids"] = [*status(7), *status(8, state=ZOMBIE, threads=3)]
    _, _, failed, runner = full_run(tmp_path, answers)
    assert rm_calls(runner) == [] and "container UserB cleanup" in failed


def test_every_pid_needs_exactly_one_uid_state_and_threads_record() -> None:
    links = ["/proc/7/cwd\t/home/agent\0"]
    good = status(7)
    assert relay._proc_gap(good, links, "1001")[0] == set()
    # One pid lost its Uid line while another pid's Uid line is there: fail closed.
    broken = [good[1], good[2], *status(9, "0")]
    with pytest.raises(ValueError):
        relay._proc_gap([*good, broken[0], broken[1]], links, "1001")
    for drop in range(3):
        partial = [line for at, line in enumerate(status(8)) if at != drop]
        with pytest.raises(ValueError):
            relay._proc_gap([*good, *partial], links, "1001")
    with pytest.raises(ValueError):
        relay._proc_gap([*good, good[0]], links, "1001")  # a second Uid record
    with pytest.raises(ValueError):  # a command line for a pid with no status records
        relay._proc_gap([*good, cmdline(55, "sleep", "9")], links, "1001")


def test_a_half_read_listing_removes_nothing_in_the_container(tmp_path: Path) -> None:
    answers = container_answers()
    answers["uids"] = [*status(7), "/proc/8/status:State:\tS (sleeping)\n"]
    _, _, failed, runner = full_run(tmp_path, answers)
    assert rm_calls(runner) == [] and "container UserB cleanup" in failed
    assert [runner.key(c) for c in runner.calls].count("uids") == 2  # looked once more


def test_a_command_line_with_a_newline_fails_the_batch_closed() -> None:
    links = ["/proc/7/cwd\t/home/agent\0"]
    inner, _ = HELPER
    # grep splits an argument that holds a newline into several lines; the tail has no prefix.
    split = [*status(7), *status(8), "/proc/8/cmdline:sleep\0one\n", "two\0\n"]
    with pytest.raises(ValueError):
        relay._proc_gap(split, links, "1001", HELPER)
    # Forged 1: the argument tail is a whole second record that makes the pid look like the
    # pinned helper; the pid then has two command line records.
    forged_helper = [
        *status(7),
        *status(8),
        "/proc/8/cmdline:sleep\0x\n",
        cmdline(8, *inner),
    ]
    with pytest.raises(ValueError):
        relay._proc_gap(forged_helper, links, "1001", HELPER)
    # Forged 2: a forged zombie marker for a live pid duplicates its real State record.
    forged_zombie = [*status(7), *status(8), "/proc/8/status:State:\tZ (zombie)\n"]
    with pytest.raises(ValueError):
        relay._proc_gap(forged_zombie, links, "1001", HELPER)
    # A forged Threads record cannot turn a threaded zombie into a single-thread one either.
    forged_threads = [
        *status(7),
        *status(8, state=ZOMBIE, threads=2),
        "/proc/8/status:Threads:\t1\n",
    ]
    with pytest.raises(ValueError):
        relay._proc_gap(forged_threads, links, "1001", HELPER)


def test_a_newline_in_an_argument_stalls_cleanup_but_removes_nothing(tmp_path: Path) -> None:
    answers = container_answers()
    inner, _ = HELPER
    answers["uids"] = [
        *LIVE["uids"],
        *status(8),
        "/proc/8/cmdline:sleep\0x\n",
        cmdline(8, *inner),
    ]
    _, _, failed, runner = full_run(tmp_path, answers)
    assert rm_calls(runner) == [] and "container UserB cleanup" in failed


def test_extra_empty_arguments_after_a_pinned_command_line_do_not_match() -> None:
    inner, execs = HELPER
    links = ["/proc/7/cwd\t/home/agent\0"]
    exact = [*status(7), *status(20)]
    plain = cmdline(20, *inner)
    assert relay._proc_gap([*exact, plain], links, "1001", HELPER)[0] == set()
    # One more NUL: the same command line plus an empty argument is not the pinned one.
    padded = f"/proc/20/cmdline:{chr(0).join(inner)}\0\0\n"
    assert relay._proc_gap([*exact, padded], links, "1001", HELPER)[0] == {"20"}
    pinned_grep = [*execs[1], "/proc/7/cmdline"]  # its own empty argument is part of the pin
    ok = [*status(7), *status(21), cmdline(21, *pinned_grep)]
    assert relay._proc_gap(ok, links, "1001", HELPER)[0] == set()
    padded_grep = f"/proc/21/cmdline:{chr(0).join(pinned_grep)}\0\0\n"
    assert relay._proc_gap([*status(7), *status(21), padded_grep], links, "1001", HELPER)[0] == {
        "21"
    }


def link(pid: int, kind: str, target: str) -> str:
    """One record of the NUL-framed ``procs`` listing."""
    return f"/proc/{pid}/{kind}\t{target}\0"


def test_a_forged_link_inside_a_folder_name_cannot_hide_a_gap() -> None:
    # A same-user process sits in a folder named "x<newline>/proc/5/cwd<tab>/y": the old
    # newline framing printed a second, well formed line that read as pid 5's cwd record.
    listing = [*status(7), *status(5)]
    forged = link(7, "cwd", "x\n/proc/5/cwd\t/y")
    assert relay._proc_gap(listing, [forged], "1001")[0] == {"5"}  # pid 5 is still a gap
    # The whole forged text is pid 7's one target; it is not split into records.
    assert relay._read_links([forged]) == [("7", "cwd", "x\n/proc/5/cwd\t/y")]
    # A tab or a newline in a target stays inside its record too.
    assert relay._read_links([link(7, "cwd", "a\tb\nc")]) == [("7", "cwd", "a\tb\nc")]


def test_the_old_newline_framed_forgery_is_refused() -> None:
    # If a listing is not NUL-framed (an old box, a cut stream) it fails closed.
    with pytest.raises(ValueError):
        relay._read_links(["/proc/7/cwd\t/home/agent\n"])
    with pytest.raises(ValueError):
        relay._read_links(["/proc/7/cwd\t/home/agent\n/proc/5/cwd\t/y\n"])


def test_a_link_listing_that_is_odd_fails_the_batch_closed() -> None:
    cwd = link(7, "cwd", "/home/agent")
    assert relay._read_links([]) == []
    assert relay._read_links([cwd, link(7, "fd/3", "pipe:[5]")]) == [
        ("7", "cwd", "/home/agent"),
        ("7", "fd/3", "pipe:[5]"),
    ]
    for bad in (
        [cwd + cwd],  # the same link twice
        [cwd.removesuffix("\0")],  # cut short, not terminated
        [cwd + "\0"],  # an empty record
        [cwd + "junk\0"],  # a record that is not a link
        [cwd + "/proc/7/environ\t/x\0"],  # a kind we never asked for
        [cwd + "/proc/x/cwd\t/x\0"],  # a pid that is not digits
        ["/proc/7/cwd/home/agent\0"],  # no tab
        ["x" * (relay.LINKS_MAX_CHARS + 1)],  # too large
    ):
        with pytest.raises(ValueError):
            relay._read_links(bad)
    # chunks of the stream are joined before they are split: a record may span chunks
    assert relay._read_links([cwd[:6], cwd[6:]]) == [("7", "cwd", "/home/agent")]


def test_a_forged_link_listing_stops_container_cleanup(tmp_path: Path) -> None:
    answers = container_answers()
    answers["procs"] = [link(7, "cwd", "/home/agent"), link(7, "cwd", "/home/agent")]
    _, _, failed, runner = full_run(tmp_path, answers)
    assert rm_calls(runner) == [] and "container UserB cleanup" in failed
    assert [runner.key(c) for c in runner.calls].count("procs") == 2  # looked once more


def test_a_process_whose_effective_uid_is_the_container_user_needs_a_cwd_line() -> None:
    # A setuid-style process: real uid root, effective uid the container user.
    listing = [*status(7), *status(9, uid="0", euid="1001")]
    links = [link(7, "cwd", "/home/agent")]
    assert relay._proc_gap(listing, links, "1001")[0] == {"9"}
    assert relay._proc_gap(listing, [*links, link(9, "cwd", "/home/agent")], "1001")[0] == set()
    # The other way round (real uid the user's) still counts, and a pid of neither does not.
    assert relay._proc_gap([*status(7), *status(8, euid="0")], links, "1001")[0] == {"8"}
    assert relay._proc_gap([*status(7), *status(10, uid="0")], links, "1001")[0] == set()


def test_an_effective_uid_process_without_a_cwd_line_stops_container_cleanup(
    tmp_path: Path,
) -> None:
    answers = container_answers()
    answers["uids"] = [*status(7), *status(9, uid="0", euid="1001")]
    _, _, failed, runner = full_run(tmp_path, answers)
    assert rm_calls(runner) == [] and "container UserB cleanup" in failed


def test_the_uids_template_reads_status_and_cmdline_with_two_pinned_execs() -> None:
    inner, execs = HELPER
    assert inner[:3] == ["timeout", "30", "find"]
    assert execs == [
        ["grep", "-H", "-e", "^Uid:", "-e", "^State:", "-e", "^Threads:"],
        ["grep", "-a", "-H", ""],
    ]


async def test_an_expired_deadline_stops_every_container_command_and_still_reports(
    tmp_path: Path,
) -> None:
    runner = FakeRunner(container_answers())
    result = relay.run_cleanup_request(
        {"id": "a" * 32, "action": "claude_scratch", "owner": "UserB", "count": 5},
        settings_for(tmp_path),
        runner,
        now=NOW,
        home=str(tmp_path),
        deadline_s=0,
    )
    assert runner.calls == []  # the runner never ran a command, listing included
    assert result["partial"] is True and result["removed"] == 0


def test_the_pass_cap_leaves_room_for_three_posts() -> None:
    assert relay.PASS_DEADLINE_S + 3 * relay.POST_TIMEOUT_S < 240


async def test_a_post_that_trickles_is_cut_at_ten_seconds_in_all(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # httpx's timeout is per phase, so a server that keeps answering a byte at a time never
    # trips it; the whole post must stop at the cap.
    monkeypatch.setattr(relay, "POST_TIMEOUT_S", 0.05)

    async def slow(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(5)
        return httpx.Response(200, json={"kept": True, "samples": 1})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(slow), base_url="https://omni.test"
    ) as client:
        began = time.monotonic()
        with pytest.raises(relay.RelayError):
            await relay.post_result(client, {"id": "a", "ok": True})
        with pytest.raises(relay.RelayError):
            await relay.post_answer(
                client, box_health.BoxHealthPost(read_at="2026-10-29T00:00:00Z")
            )
        assert time.monotonic() - began < 2


async def test_the_posts_wait_at_most_ten_seconds(tmp_path: Path) -> None:
    seen: list[float] = []

    def answer(request: httpx.Request) -> httpx.Response:
        seen.append(request.extensions["timeout"]["read"])
        return httpx.Response(200, json={"kept": True, "samples": 1})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(answer), base_url="https://omni.test"
    ) as client:
        await relay.post_result(client, {"id": "a", "ok": True})
        await relay.post_answer(client, box_health.BoxHealthPost(read_at="2026-10-29T00:00:00Z"))
    assert seen == [10.0, 10.0]


def test_a_container_session_with_a_git_checkout_inside_is_never_removed(
    tmp_path: Path,
) -> None:
    answers = container_answers()
    answers["claude_tree"] = [
        *answers["claude_tree"],
        f"4\td\t{NOW - 5 * DAY}\t8\t-{TOKENS[0]}/{UUID_A}/repo/.git\n",
    ]
    _, _, _, runner = full_run(tmp_path, answers)
    assert SESSION_PATH not in aged_paths(runner)
    assert PYTEST_PATH in aged_paths(runner)
    button = relay.run_cleanup_request(
        {"id": "a" * 32, "action": "claude_scratch", "owner": "UserB", "count": 9},
        settings_for(tmp_path),
        FakeRunner(answers),
        now=NOW,
        home=str(tmp_path),
    )
    assert button["skipped"].get("git") == 1 and button["removed"] == 1
