"""Box health: the colour rules, the store, the view's size and the /v1/ae routes (2026-10-28)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from ae_omni_policies import box_health
from ae_omni_policies.box_health import (
    BoxHealthPost,
    BoxHealthStore,
    ClaudeRoot,
    Cpu,
    Docker,
    Guard,
    LitterGroup,
    Memory,
    Mount,
    Sensor,
    SlowScan,
    UserHealth,
    Worktrees,
)

NOW = datetime(2026, 10, 28, 17, 30, tzinfo=UTC)
GIB = 1024**3
OWNER = {"X-Forwarded-Email": "owner@example.com"}
MEMBER = {"X-Forwarded-Email": "member@example.com"}


def sensors(cpu: float = 70, nvme: float = 45) -> list[Sensor]:
    return [
        Sensor(chip="k10temp", label="Tctl", c=cpu),
        Sensor(chip="nvme", label="Composite", c=nvme),
        Sensor(chip="acpitz_0", label="temp1", c=60),
        Sensor(chip="amdgpu", label="edge", c=55),
    ]


def mount(path: str = "/", used_pct: float = 20, inode_pct: float | None = 3) -> Mount:
    size = 1000 * GIB
    used = int(size * used_pct / 100)
    return Mount(
        path=path,
        fstype="ext4",
        size_b=size,
        used_b=used,
        avail_b=size - used,
        inodes=1_000_000 if inode_pct is not None else None,
        inodes_used=int(1_000_000 * inode_pct / 100) if inode_pct is not None else None,
    )


def slow(users: list[UserHealth] | None = None, docker: Docker | None = None) -> SlowScan:
    return SlowScan(
        read_at=NOW,
        stale_days=2,
        slow_minutes=60,
        users=users or [],
        docker=docker or Docker(),
        journal_b=500_000_000,
    )


def make_post(
    *,
    at: datetime = NOW,
    cpu_c: float = 70,
    nvme_c: float = 45,
    load15: float = 5.0,
    used_pct: float = 40,
    mounts: list[Mount] | None = None,
    users: list[UserHealth] | None = None,
    docker: Docker | None = None,
    failed: list[str] | None = None,
    units: list[str] | None = None,
    guard: Guard | None = None,
    other_units: int = 0,
) -> BoxHealthPost:
    total = 128 * GIB
    return BoxHealthPost(
        host="box-host",
        read_at=at,
        uptime_s=5000,
        failed=failed or [],
        sensors=sensors(cpu_c, nvme_c),
        gpu_power_w=100,
        cpu=Cpu(threads=32, load1=load15, load5=load15, load15=load15),
        memory=Memory(total_b=total, available_b=int(total * (1 - used_pct / 100))),
        mounts=mounts if mounts is not None else [mount()],
        failed_units=units or [],
        failed_units_other=other_units,
        guard=guard or Guard(running=True, locked=3, failed=0, age_s=20),
        slow=slow(users, docker),
    )


def stored(
    post: BoxHealthPost,
    history_c: list[float] | None = None,
    received_at: datetime | None = None,
) -> box_health.Stored:
    """A stored reading whose history is *history_c*, five minutes apart, ending at the post."""
    samples = []
    for step, temp in enumerate(reversed(history_c or [])):
        samples.append(
            box_health.Sample(
                t=(post.read_at - timedelta(minutes=5 * step)).timestamp(), cpu_c=temp
            )
        )
    samples.reverse()
    return box_health.Stored(
        reading=post, received_at=received_at or post.read_at, history=samples
    )


def tone_of(view: box_health.BoxHealthView, area: str) -> str:
    return next(a.tone for a in view.areas if a.id == area)


def view_of(post: BoxHealthPost, history: list[float] | None = None, now: datetime = NOW):
    return box_health.view(stored(post, history), now, box_health.config({}))


# ── the colour rules ────────────────────────────────────────────────────────


def test_a_clean_box_is_green_with_five_areas() -> None:
    view = view_of(make_post())
    assert (view.state, view.tone, view.needs) == ("ok", "green", [])
    assert [a.id for a in view.areas] == ["heat", "cpu", "memory", "storage", "bloat"]
    assert all(len(a.value) <= 24 for a in view.areas)


def test_no_reading_is_grey_with_the_setup_command() -> None:
    view = box_health.view(None, NOW, box_health.config({}))
    assert (view.state, view.tone) == ("missing", "grey")
    assert "ae-omnigent-box-health.timer" in (view.setup or "")


def test_a_reading_older_than_fifteen_minutes_is_amber() -> None:
    fresh = view_of(make_post(at=NOW - timedelta(minutes=15)))
    old = view_of(make_post(at=NOW - timedelta(minutes=16)))
    assert fresh.state == "ok"
    assert (old.state, old.tone) == ("stale", "amber")
    assert old.needs[0].key == "stale"


def test_one_hot_cpu_reading_stays_green() -> None:
    view = view_of(make_post(cpu_c=91), [70, 72, 91])
    assert tone_of(view, "heat") == "green"
    assert "not sustained" in next(a.title for a in view.areas if a.id == "heat")


def test_cpu_is_amber_only_when_every_sample_in_the_window_is_hot() -> None:
    assert tone_of(view_of(make_post(cpu_c=90), [90, 90, 90, 90]), "heat") == "amber"
    assert tone_of(view_of(make_post(cpu_c=90), [90, 89.9, 90, 90]), "heat") == "green"
    assert tone_of(view_of(make_post(cpu_c=90), [90, 90]), "heat") == "green"


def test_cpu_is_red_when_sustained_five_degrees_higher_or_at_98() -> None:
    assert tone_of(view_of(make_post(cpu_c=95), [95, 95, 96, 95]), "heat") == "red"
    assert tone_of(view_of(make_post(cpu_c=98), [60]), "heat") == "red"
    assert tone_of(view_of(make_post(cpu_c=97.9), [60]), "heat") == "green"


def test_ssd_edges() -> None:
    assert tone_of(view_of(make_post(nvme_c=59.9)), "heat") == "green"
    assert tone_of(view_of(make_post(nvme_c=60)), "heat") == "amber"
    assert tone_of(view_of(make_post(nvme_c=68)), "heat") == "red"


@pytest.mark.parametrize(
    ("load15", "tone"), [(31.9, "green"), (32, "amber"), (63.9, "amber"), (64, "red")]
)
def test_load_edges(load15: float, tone: str) -> None:
    assert tone_of(view_of(make_post(load15=load15)), "cpu") == tone


@pytest.mark.parametrize(
    ("used", "tone"), [(89.9, "green"), (90, "amber"), (96.9, "amber"), (97, "red")]
)
def test_memory_edges(used: float, tone: str) -> None:
    assert tone_of(view_of(make_post(used_pct=used)), "memory") == tone


def test_a_mount_takes_the_worse_of_space_and_inodes() -> None:
    view = view_of(make_post(mounts=[mount("/tmp", 25, 96)]))
    assert tone_of(view, "storage") == "red"
    need = view.needs[0]
    assert (need.tone, need.key) == ("red", "mount:/tmp:inodes")
    assert "960,000 of 1,000,000 inodes" in need.detail
    assert tone_of(view_of(make_post(mounts=[mount("/", 85, 3)])), "storage") == "amber"
    assert tone_of(view_of(make_post(mounts=[mount("/", 84.9, 3)])), "storage") == "green"


def test_a_filesystem_without_inodes_has_no_inode_percentage() -> None:
    view = view_of(make_post(mounts=[mount("/boot/efi", 6, None)]))
    row = view.details["mounts"][0]
    assert row["inodes_pct"] is None
    assert tone_of(view, "storage") == "green"


def user(label: str = "Owner", stale: int = 0, where: str = "tmp", **kw: Any) -> UserHealth:
    return UserHealth(
        label=label,
        uid=1000,
        claude=[
            ClaudeRoot(
                where=where,
                sessions=10,
                stale_sessions=stale,
                files=1000,
                bytes=10**9,
                oldest_days=4,
            )
        ],
        **kw,
    )


def test_stale_claude_sessions_name_their_owner() -> None:
    view = view_of(make_post(users=[user("Owner", 268), user("Member", 0)]))
    assert tone_of(view, "bloat") == "amber"
    need = view.needs[0]
    assert need.owner == "Owner" and need.text.startswith("Owner: 268 Claude temp folders")


def test_litter_needs_five_thousand_stale_entries_for_one_user() -> None:
    def litter(n: int) -> UserHealth:
        return user(litter=[LitterGroup(group="sec-appr-*", entries=n, stale=n)])

    assert tone_of(view_of(make_post(users=[litter(4999)])), "bloat") == "green"
    assert tone_of(view_of(make_post(users=[litter(5000)])), "bloat") == "amber"


def test_docker_reclaim_edge() -> None:
    small = Docker(images_reclaim_b=19 * GIB)
    big = Docker(images_reclaim_b=15 * GIB, build_reclaim_b=5 * GIB)
    assert tone_of(view_of(make_post(docker=small)), "bloat") == "green"
    assert tone_of(view_of(make_post(docker=big)), "bloat") == "amber"


def test_a_stopped_container_and_a_fat_layer_are_amber_lines_for_their_owner() -> None:
    down = UserHealth(label="Member", kind="container", running=False)
    fat = UserHealth(label="Member", kind="container", running=True, writable_b=5 * GIB)
    view = view_of(make_post(users=[down]))
    assert view.needs[0].text == "Member's container is not running"
    view = view_of(make_post(users=[fat]))
    assert "writable layer" in view.needs[0].text
    ok = UserHealth(label="Member", kind="container", running=True, writable_b=5 * GIB - 1)
    assert view_of(make_post(users=[ok])).needs == []


def test_failed_units_and_unread_parts() -> None:
    view = view_of(
        make_post(
            units=["ae-a.service", "ae-b.service", "ae-c.service", "ae-d.service"], other_units=2
        )
    )
    assert view.needs[0].text == (
        "6 failed units: ae-a.service, ae-b.service, ae-c.service +1 and 2 other"
    )
    assert view_of(make_post(other_units=1)).needs[0].text == "1 failed unit: 1 other"
    view = view_of(make_post(failed=["docker", "sensors"]))
    assert tone_of(view, "heat") == "grey"
    assert any(n.text.startswith("The box could not read: docker") for n in view.needs)


def test_the_guard_not_running_is_an_amber_line_and_running_is_counted() -> None:
    down = view_of(make_post(guard=Guard(running=False)))
    assert down.needs[0].key == "guard" and tone_of(down, "bloat") == "amber"
    up = view_of(make_post(guard=Guard(running=True, locked=7, age_s=10)))
    assert "7 live sessions protected" in next(a.title for a in up.areas if a.id == "bloat")
    assert up.details["guard"]["locked"] == 7


def test_needs_sort_red_first_then_furthest_past_the_threshold() -> None:
    post = make_post(
        mounts=[mount("/", 86), mount("/tmp", 99), mount("/var", 91)], units=["ae-x.service"]
    )
    keys = [n.key for n in view_of(post).needs]
    assert keys[0] == "mount:/tmp:space"
    assert keys.index("mount:/var:space") < keys.index("mount:/:space")


def test_history_downsamples_to_ninety_six_points_keeping_the_highest() -> None:
    history = [
        box_health.Sample(t=(NOW - timedelta(minutes=5 * i)).timestamp(), cpu_c=float(i % 40))
        for i in range(287, -1, -1)
    ]
    post = make_post()
    view = box_health.view(box_health.Stored(reading=post, received_at=NOW, history=history), NOW)
    assert len(view.history.cpu_c) == 96
    assert view.history.cpu_c[-1] is not None
    assert max(v for v in view.history.cpu_c if v is not None) == 39.0


def test_settings_rows_carry_hints_and_never_the_admin_logins(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AE_BOX_ADMINS", "Member@Example.com, other@example.com")
    view = view_of(make_post())
    row = next(r for r in view.settings if r.name == "AE_BOX_ADMINS")
    assert row.value == "2 logins"
    assert "example.com" not in view.model_dump_json()


@pytest.mark.parametrize("setting", box_health.SETTINGS, ids=lambda s: s.name)
def test_every_hint_is_short_plain_and_documented(setting: box_health.Setting) -> None:
    assert 0 < len(setting.hint) <= box_health.HINT_MAX
    assert "—" not in setting.hint and "--" not in setting.hint
    root = Path(__file__).resolve().parents[3] / "config"
    name = "server.env.example" if setting.side == "server" else "host.env.example"
    assert setting.name in (root / name).read_text(encoding="utf-8")


# ── the store ───────────────────────────────────────────────────────────────


def test_a_post_under_four_minutes_after_the_last_replaces_the_sample(tmp_path: Path) -> None:
    store = BoxHealthStore(tmp_path / "bh.json")
    assert store.put(make_post(at=NOW)) == 1
    assert store.put(make_post(at=NOW + timedelta(minutes=3, seconds=59))) == 1
    assert store.put(make_post(at=NOW + timedelta(minutes=8))) == 2


def test_history_stops_at_288_samples(tmp_path: Path) -> None:
    store = BoxHealthStore(tmp_path / "bh.json")
    for i in range(300):
        store.put(make_post(at=NOW + timedelta(minutes=5 * i)))
    read = store.read()
    assert read is not None and len(read.history) == box_health.HISTORY_MAX


def test_the_store_survives_a_corrupt_file_and_keeps_memory_without_a_path(tmp_path: Path) -> None:
    path = tmp_path / "bh.json"
    path.write_text("{not json")
    store = BoxHealthStore(path)
    assert store.read() is None
    assert store.put(make_post()) == 1
    assert BoxHealthStore(None).put(make_post()) == 1


# ── the routes ──────────────────────────────────────────────────────────────


def body() -> dict[str, Any]:
    return json.loads(make_post().model_dump_json())


async def test_access_tells_each_caller_what_they_may_see(
    client: httpx.AsyncClient, ae_admins: set[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    assert (await client.get("/v1/ae/access", headers=OWNER)).json() == {
        "admin": True,
        "box_admin": True,
    }
    assert (await client.get("/v1/ae/access", headers=MEMBER)).json() == {
        "admin": False,
        "box_admin": False,
    }
    monkeypatch.setenv("AE_BOX_ADMINS", "MEMBER@example.com")
    assert (await client.get("/v1/ae/access", headers=MEMBER)).json() == {
        "admin": False,
        "box_admin": True,
    }


async def test_a_box_admin_reads_but_cannot_post(
    client: httpx.AsyncClient, ae_admins: set[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    assert (await client.get("/v1/ae/box-health", headers=MEMBER)).status_code == 403
    monkeypatch.setenv("AE_BOX_ADMINS", "member@example.com")
    assert (await client.get("/v1/ae/box-health", headers=MEMBER)).status_code == 200
    assert (await client.post("/v1/ae/box-health", json=body(), headers=MEMBER)).status_code == 403
    ok = await client.post("/v1/ae/box-health", json=body(), headers=OWNER)
    assert ok.status_code == 200, ok.text
    assert ok.json() == {"kept": True, "samples": 1}
    seen = (await client.get("/v1/ae/box-health", headers=MEMBER)).json()
    assert seen["state"] in ("ok", "stale") and seen["host"] == "box-host"
    assert "member@example.com" not in json.dumps(seen)


async def test_post_checks_the_origin_and_the_limits(client: httpx.AsyncClient) -> None:
    foreign = await client.post(
        "/v1/ae/box-health", json=body(), headers={"Origin": "https://evil.example"}
    )
    assert foreign.status_code == 403, foreign.text
    too_many = {**body(), "failed_units": ["a.service"] * 11}
    assert (await client.post("/v1/ae/box-health", json=too_many)).status_code == 422
    bad_path = {
        **body(),
        "mounts": [{"path": "rel", "fstype": "ext4", "size_b": 1, "used_b": 0, "avail_b": 1}],
    }
    assert (await client.post("/v1/ae/box-health", json=bad_path)).status_code == 422
    nested = body()
    nested["slow"]["users"][0:0] = [{"label": "../etc", "kind": "box"}]
    assert (await client.post("/v1/ae/box-health", json=nested)).status_code == 422


async def test_a_single_user_runtime_is_everything(client: httpx.AsyncClient) -> None:
    assert (await client.get("/v1/ae/access")).json() == {"admin": True, "box_admin": True}
    assert (await client.post("/v1/ae/box-health", json=body())).status_code == 200


# ── the size cap ────────────────────────────────────────────────────────────


def test_the_worst_case_view_stays_under_the_bridge_cap() -> None:
    """The extension bridge refuses an answer over 32,768 characters; stay under 30,000.

    Every posted string is at its longest the model allows (48 characters, 32 for labels),
    and every list is full."""
    longest = sorted(box_health.LITTER_GROUP_NAMES, key=len, reverse=True)[:10]
    groups = [
        LitterGroup(group=name, entries=99999, stale=99999, bytes=10**11) for name in longest
    ]
    roots = [
        ClaudeRoot(
            where=w,
            sessions=99999,
            stale_sessions=99999,
            projects=999,
            files=10**9,
            bytes=10**12,
            stale_files=10**9,
            stale_bytes=10**12,
            oldest_days=123.45,
            partial=True,
        )
        for w in ("tmp", "cache", "container")
    ]
    users = [
        UserHealth(
            label=f"{i}" + "U" * 31,
            kind="container" if i % 2 else "box",
            uid=1000 + i,
            running=True,
            writable_b=10**11,
            home_b=10**12,
            claude=roots,
            litter=groups,
            worktrees=Worktrees(count=60, bytes=10**12, older_14d=60, with_changes=60),
        )
        for i in range(5)
    ]
    safe = sorted(box_health.SAFE_MOUNTS)
    names = safe + [f"other mount {i}" for i in range(1, 12)]
    unit = "ae-" + "u" * 37 + ".service"
    assert len(unit) == box_health.MAX_STRING
    post = make_post(
        mounts=[mount(name, 96, 96) for name in names[:20]],
        users=users,
        units=[unit] * 10,
        other_units=99999,
        failed=[f"container {chr(65 + i) * 23} volume" for i in range(20)],
        docker=Docker(images_reclaim_b=10**12),
    )
    post.failed_user_units = [unit] * 10
    post.sensors = [Sensor(chip="c" * 32, label="L" * 32, c=99) for _ in range(24)]
    assert post.slow is not None
    post.slow.dirs = [
        box_health.DirSize(label="D" * 32, bytes=10**12, files=10**9, partial=True)
        for _ in range(6)
    ]
    history = [
        box_health.Sample(
            t=(NOW - timedelta(minutes=5 * i)).timestamp(),
            cpu_c=99.5,
            nvme_c=99.5,
            load1=999.5,
            mem_pct=99.5,
            disk_pct=99.5,
        )
        for i in range(287, -1, -1)
    ]
    view = box_health.view(box_health.Stored(reading=post, received_at=NOW, history=history), NOW)
    dump = view.model_dump(mode="json")
    size = len(json.dumps(dump))
    parts = {key: len(json.dumps(value)) for key, value in dump.items()}
    parts.update({f"details.{k}": len(json.dumps(v)) for k, v in dump["details"].items()})
    assert size < 30_000, (size, parts)


@pytest.mark.parametrize(
    "bad",
    [
        {
            "mounts": [
                {"path": "/mnt/client", "fstype": "ext4", "size_b": 1, "used_b": 0, "avail_b": 1}
            ]
        },
        {
            "mounts": [
                {"path": "/caf\u00e9", "fstype": "ext4", "size_b": 1, "used_b": 0, "avail_b": 1}
            ]
        },
        {"failed_units": ["acme-client.service"]},
        {"failed_units": ["ae-caf\u00e9.service"]},
        {"failed_units": ["ae-" + "x" * 60 + ".service"]},
        {"sensors": [{"chip": "k10temp", "label": "T\u00e9", "c": 1}]},
        {"sensors": [{"chip": "k10temp", "label": "T" * 33, "c": 1}]},
        {"failed": ["x" * 49]},
        {"failed": ["caf\u00e9"]},
        {"host": "h" * 49},
    ],
)
def test_a_name_outside_the_allowlist_or_over_48_characters_is_refused(
    bad: dict[str, object],
) -> None:
    good = json.loads(make_post().model_dump_json())
    BoxHealthPost.model_validate(good)
    with pytest.raises(ValueError):
        BoxHealthPost.model_validate({**good, **bad})


def test_slow_labels_and_folders_are_plain_and_short() -> None:
    for label in ("caf\u00e9", "x" * 33, "../etc", ""):
        with pytest.raises(ValueError):
            box_health.DirSize(label=label, bytes=1)
        with pytest.raises(ValueError):
            UserHealth(label=label)
    box_health.DirSize(label="cache", bytes=1)


def test_unrelated_failed_parts_do_not_grey_the_bloat_area() -> None:
    view = view_of(
        make_post(failed=["journal", "docker", "worktrees", "users", "container Member"])
    )
    assert tone_of(view, "bloat") == "green"
    assert any(n.text.startswith("The box could not read") for n in view.needs)
    missing = make_post(failed=["sensors"])
    missing.slow = None
    assert tone_of(view_of(missing), "bloat") == "grey"
    assert tone_of(view_of(missing), "heat") == "grey"


def test_the_stale_rule_uses_the_smaller_of_the_two_clocks() -> None:
    old_box_clock = make_post(at=NOW - timedelta(minutes=40))
    fresh_on_server = stored(old_box_clock, received_at=NOW - timedelta(minutes=2))
    assert box_health.view(fresh_on_server, NOW, box_health.config({})).state == "ok"
    live_box_dead_server_copy = stored(
        make_post(at=NOW - timedelta(minutes=1)), received_at=NOW - timedelta(minutes=40)
    )
    assert box_health.view(live_box_dead_server_copy, NOW, box_health.config({})).state == "ok"
    both_old = stored(
        make_post(at=NOW - timedelta(minutes=40)), received_at=NOW - timedelta(minutes=30)
    )
    assert box_health.view(both_old, NOW, box_health.config({})).state == "stale"
    future = stored(
        make_post(at=NOW + timedelta(hours=3)), received_at=NOW - timedelta(minutes=30)
    )
    seen = box_health.view(future, NOW, box_health.config({}))
    assert seen.state == "stale" and seen.age_s == 30 * 60


def test_the_unit_allowlist_drops_instances_and_names_only_known_families() -> None:
    assert box_health.safe_unit("ae-omnigent-host-container@member.service") == (
        "ae-omnigent-host-container@.service"
    )
    assert box_health.safe_unit("systemd-resolved.service") == "systemd-resolved.service"
    assert box_health.safe_unit("docker.socket") == "docker.socket"
    for name in ("acme.service", "dockerfoo.service", "ae-.service", "ae-x.exe", "x" * 60):
        assert box_health.safe_unit(name) is None, name


@pytest.mark.parametrize(
    "part",
    [
        *sorted(box_health.FIXED_PARTS),
        "user Owner",
        "container Member",
        "container Member scan",
        "container Member volume",
    ],
)
def test_every_part_the_relay_can_name_is_accepted(part: str) -> None:
    BoxHealthPost.model_validate({**json.loads(make_post().model_dump_json()), "failed": [part]})


@pytest.mark.parametrize(
    "part", ["acme-client", "worktree acme", "container", "user ", "docker acme", "slow scans x"]
)
def test_a_failed_part_outside_the_fixed_set_is_refused(part: str) -> None:
    with pytest.raises(ValueError):
        BoxHealthPost.model_validate(
            {**json.loads(make_post().model_dump_json()), "failed": [part]}
        )


def test_a_tmp_group_outside_the_fixed_set_is_refused() -> None:
    for name in ("sec-appr-*", "tmp*", "other", "*-review.*", "dotnet-diagnostic-*"):
        LitterGroup(group=name, entries=1)
    for name in ("acme-*", "client", "sec-appr-acme", "ok"):
        with pytest.raises(ValueError):
            LitterGroup(group=name, entries=1)
