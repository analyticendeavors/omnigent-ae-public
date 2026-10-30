"""Expiry alerts and secret owners (2026-10-23): the stages, the task, idempotence, the card."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from ae_omni_policies import (
    secret_alerts,
    secret_owners,
    secrets_meta,
    shared_accounts_routes,
    taskstore,
    vault_inventory,
)

OWNER = {"X-Forwarded-Email": "owner@example.com"}
MEMBER = {"X-Forwarded-Email": "member@example.com"}
# 05:00 in Los Angeles, before the 09:00 alarm.
NOW = datetime(2026, 10, 23, 12, 0, tzinfo=UTC)
APP_ID = "00000000-0000-0000-0000-000000000001"
CFG = secret_alerts.Config(
    on=True, off_reason=None, list_name="AE Work", time="09:00", tz="America/Los_Angeles"
)
BOX = "kv-example-box"
WEB = "kv-example-web"


class FakeStore:
    """The task app's surface the alert pass uses, in memory."""

    def __init__(self) -> None:
        self.tasks: dict[str, dict[str, Any]] = {}
        self.calls: list[tuple[str, Any]] = []
        self.create_fails_after_landing = False
        self.lists = [
            {"id": "L-AE", "name": "AE Work", "hidden": 0},
            {"id": "L-HIDDEN", "name": "Notes", "hidden": 1},
        ]

    def _ok(self, data: Any, status: int = 200) -> taskstore.Result:
        return taskstore.Result(ok=True, status=status, data=data)

    async def list_buckets(self) -> taskstore.Result:
        self.calls.append(("list_buckets", None))
        return self._ok({"buckets": self.lists})

    async def list_tags(self) -> taskstore.Result:
        self.calls.append(("list_tags", None))
        return self._ok({"tags": [{"name": "ae"}, {"name": "hc"}]})

    async def list_tasks(self, status: str = "open") -> taskstore.Result:
        self.calls.append(("list_tasks", status))
        return self._ok({"tasks": [t for t in self.tasks.values() if t["status"] == status]})

    async def get_task(self, task_id: str) -> taskstore.Result:
        self.calls.append(("get_task", task_id))
        if task_id not in self.tasks:
            return taskstore.Result(ok=False, status=404, kind="client", error="404 not found")
        return self._ok(dict(self.tasks[task_id]))

    async def create_task(self, fields: dict[str, Any]) -> taskstore.Result:
        self.calls.append(("create_task", fields))
        task_id = f"T{len(self.tasks) + 1}"
        self.tasks[task_id] = {"id": task_id, "created_at": "2026-10-23T12:00:00Z", **fields}
        if self.create_fails_after_landing:
            return taskstore.Result(ok=False, status=0, kind="transport", error="timed out")
        return self._ok(self.tasks[task_id], 201)

    async def patch_task(self, task_id: str, fields: dict[str, Any]) -> taskstore.Result:
        self.calls.append(("patch_task", (task_id, fields)))
        if task_id not in self.tasks:
            return taskstore.Result(ok=False, status=404, kind="client", error="404 not found")
        self.tasks[task_id].update(fields)
        return self._ok(self.tasks[task_id])

    async def aclose(self) -> None:
        return None

    def made(self) -> list[dict[str, Any]]:
        return [body for name, body in self.calls if name == "create_task"]

    def patched(self) -> list[tuple[str, dict[str, Any]]]:
        return [body for name, body in self.calls if name == "patch_task"]


def _reading(
    box: dict[str, secrets_meta.SecretMeta], web: dict[str, secrets_meta.SecretMeta] | None = None
) -> secrets_meta.Reading:
    return secrets_meta.Reading(
        read_at=NOW, secrets=box, vaults={WEB: web} if web is not None else {}
    )


def _secret(name: str, days: float | None, **fields: Any) -> secrets_meta.SecretMeta:
    expires = NOW + timedelta(days=days) if days is not None else None
    return secrets_meta.SecretMeta(name=name, expires=expires, updated=NOW, **fields)


async def _run(
    reading: secrets_meta.Reading,
    fake: FakeStore,
    alerts: secret_alerts.AlertStore,
    *,
    now: datetime = NOW,
    owners: dict[str, str] | None = None,
    cfg: secret_alerts.Config = CFG,
) -> secret_alerts.Outcome:
    return await secret_alerts.run(
        reading,
        now=now,
        cfg=cfg,
        client=fake,  # type: ignore[arg-type]
        alerts=alerts,
        owners=owners or {},
    )


# ── the stages and the task ─────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("days", "stage"),
    [(45, None), (30.5, "30"), (8, "30"), (7.5, "7"), (0.5, "7"), (-1, "expired")],
)
def test_stage_of(days: float, stage: str | None) -> None:
    meta = _secret("x", days)
    assert secret_alerts.stage_of(secrets_meta.expiry(meta, NOW)) == stage


def test_the_alarm_is_the_next_nine_on_the_home_clock() -> None:
    tz = "America/Los_Angeles"
    assert secret_alerts.alarm_instant(NOW, tz, "09:00") == datetime(
        2026, 10, 23, 16, 0, tzinfo=UTC
    )
    # After nine: tomorrow.
    late = datetime(2026, 10, 23, 17, 0, tzinfo=UTC)
    assert secret_alerts.alarm_instant(late, tz, "09:00") == datetime(
        2026, 10, 24, 16, 0, tzinfo=UTC
    )
    # The autumn change (2026-11-01): nine on the new offset, not the old one.
    fall = datetime(2026, 11, 1, 8, 0, tzinfo=UTC)
    assert secret_alerts.alarm_instant(fall, tz, "09:00") == datetime(
        2026, 11, 1, 17, 0, tzinfo=UTC
    )


async def test_a_secret_inside_30_days_files_one_task_with_an_alarm() -> None:
    fake, alerts = FakeStore(), secret_alerts.AlertStore(None)
    reading = _reading(
        {
            "ae-auditor-client-secret": _secret(
                "ae-auditor-client-secret",
                45,
                source=f"entra:{APP_ID}",
                source_expires=NOW + timedelta(days=20),
            ),
            "far-off": _secret("far-off", 90),
            "no-date": _secret("no-date", None),
            "off": _secret("off", 3, enabled=False),
        }
    )
    owners = {f"{BOX}/ae-auditor-client-secret": "example.owner@example.com"}
    out = await _run(reading, fake, alerts, owners=owners)
    assert out.created == [f"{BOX}/ae-auditor-client-secret"] and not out.errors
    [body] = fake.made()
    assert body["title"] == "Rotate ae-auditor-client-secret (expires 2026-11-12)"
    assert body["reminder_at"] == body["due_at"] == "2026-10-23T16:00:00Z"
    assert body["due_is_all_day"] == 0 and body["floating"] == 1
    assert body["bucket_id"] == "L-AE" and body["tags"] == ["ae"]
    assert body["origin_surface"] == taskstore.ORIGIN_SURFACE
    assert body["urgent"] == 0 and body["important"] == 1
    note = body["notes"]
    assert note.startswith("**Expires in 20 days**, on 2026-11-12. Date from Entra")
    assert "Owner: Example Owner" in note
    assert f"Credentials/appId/{APP_ID}" in note
    assert (
        f"https://{BOX}{secrets_meta.VAULT_HOST_SUFFIX}/secrets/ae-auditor-client-secret" in note
    )
    assert note.endswith(f"Alert key: {BOX}/ae-auditor-client-secret 2026-11-12 stage 30")
    sent = alerts.read().alerts[f"{BOX}/ae-auditor-client-secret"]
    assert (sent.stage, sent.task_id, sent.list) == ("30", "T1", "AE Work")


async def test_repeat_readings_and_a_restart_file_nothing_more(tmp_path: Path) -> None:
    fake = FakeStore()
    path = tmp_path / "alerts.json"
    reading = _reading({"a": _secret("a", 20)})
    await _run(reading, fake, secret_alerts.AlertStore(path))
    await _run(reading, fake, secret_alerts.AlertStore(path), now=NOW + timedelta(hours=6))
    assert len(fake.made()) == 1 and not fake.patched()


async def test_the_7_day_stage_moves_the_same_tasks_alarm() -> None:
    fake, alerts = FakeStore(), secret_alerts.AlertStore(None)
    meta = _secret("a", 20)
    await _run(_reading({"a": meta}), fake, alerts)
    later = NOW + timedelta(days=14)
    out = await _run(_reading({"a": meta}), fake, alerts, now=later)
    assert out.rearmed == [f"{BOX}/a"] and len(fake.made()) == 1
    task = fake.tasks["T1"]
    assert task["notes"].startswith("**Expires in 6 days**")
    assert task["reminder_at"] == "2026-11-06T17:00:00Z" and task["urgent"] == 1
    assert "stage 7" in task["notes"]
    # Expired: one last alarm on the same task.
    out = await _run(_reading({"a": meta}), fake, alerts, now=NOW + timedelta(days=21))
    assert out.rearmed == [f"{BOX}/a"] and fake.tasks["T1"]["notes"].startswith("**Expired**")


async def test_a_task_ticked_done_stays_done_until_the_next_stage() -> None:
    fake, alerts = FakeStore(), secret_alerts.AlertStore(None)
    meta = _secret("a", 20)
    await _run(_reading({"a": meta}), fake, alerts)
    fake.tasks["T1"]["status"] = "done"
    await _run(_reading({"a": meta}), fake, alerts, now=NOW + timedelta(days=1))
    assert len(fake.made()) == 1
    assert alerts.read().alerts[f"{BOX}/a"].done
    assert secret_alerts.infos(alerts.read()) == {}
    await _run(_reading({"a": meta}), fake, alerts, now=NOW + timedelta(days=14))
    assert len(fake.made()) == 2 and fake.tasks["T1"]["status"] == "done"


async def test_a_rotated_secret_closes_its_task() -> None:
    fake, alerts = FakeStore(), secret_alerts.AlertStore(None)
    await _run(_reading({"a": _secret("a", 20)}), fake, alerts)
    out = await _run(_reading({"a": _secret("a", 400)}), fake, alerts)
    assert out.closed == [f"{BOX}/a"]
    assert fake.tasks["T1"]["status"] == "done"
    assert fake.tasks["T1"]["notes"].endswith("Closed 2026-10-23: the expiry moved to 2027-11-27.")
    assert alerts.read().alerts == {}


async def test_a_vault_missing_from_the_reading_closes_nothing() -> None:
    fake, alerts = FakeStore(), secret_alerts.AlertStore(None)
    await _run(_reading({}, {"w": _secret("w", 20)}), fake, alerts)
    out = await _run(_reading({}), fake, alerts)
    assert not out.closed and fake.tasks["T1"]["status"] == "open"
    # Read, but without the secret: it left the vault.
    out = await _run(_reading({}, {"other": _secret("other", 300)}), fake, alerts)
    assert out.closed == [f"{WEB}/w"]


async def test_a_lost_state_file_adopts_the_open_task() -> None:
    fake = FakeStore()
    await _run(_reading({"a": _secret("a", 20)}), fake, secret_alerts.AlertStore(None))
    fresh = secret_alerts.AlertStore(None)
    out = await _run(_reading({"a": _secret("a", 20)}), fake, fresh)
    assert not out.created and len(fake.made()) == 1
    assert fresh.read().alerts[f"{BOX}/a"].task_id == "T1"


async def test_a_create_that_times_out_is_re_read_not_resent() -> None:
    fake, alerts = FakeStore(), secret_alerts.AlertStore(None)
    fake.create_fails_after_landing = True
    out = await _run(_reading({"a": _secret("a", 20)}), fake, alerts)
    assert out.created == [f"{BOX}/a"] and len(fake.made()) == 1 and not out.errors


async def test_a_missing_list_files_with_no_list_and_says_so() -> None:
    fake, alerts = FakeStore(), secret_alerts.AlertStore(None)
    fake.lists = []
    out = await _run(_reading({"a": _secret("a", 20)}), fake, alerts)
    assert fake.made()[0]["bucket_id"] is None
    assert 'no single visible list is called "AE Work"' in out.errors[0]
    assert alerts.read().last_error


async def test_off_or_unconfigured_files_nothing() -> None:
    fake = FakeStore()
    off = secret_alerts.config({"AE_SECRET_ALERTS": "off"})
    assert not off.on and "AE_SECRET_ALERTS" in (off.off_reason or "")
    await _run(_reading({"a": _secret("a", 20)}), fake, secret_alerts.AlertStore(None), cfg=off)
    assert fake.calls == []
    bare = secret_alerts.config({})
    assert not bare.on and "task store" in (bare.off_reason or "")
    ready = secret_alerts.config(
        {
            "AE_TASK_STORE_URL": "https://tasks.test",
            "AE_CF_ACCESS_CLIENT_ID": "id",
            "AE_CF_ACCESS_CLIENT_SECRET": "s",
            "AE_TASK_STORE_TZ": "America/Los_Angeles",
            "AE_SECRET_ALERT_LIST": "AE Work",
            "AE_SECRET_ALERT_TIME": "25:00",
        }
    )
    assert ready.on and ready.list_name == "AE Work" and ready.time == "09:00"


async def test_the_card_shows_the_open_alert_and_the_next_alarm() -> None:
    fake, alerts = FakeStore(), secret_alerts.AlertStore(None)
    reading = _reading({"a": _secret("a", 20), "b": _secret("b", 200)})
    await _run(reading, fake, alerts)
    state = alerts.read()
    summary = secret_alerts.summary(state, CFG, NOW)
    assert summary.on and summary.open == 1 and summary.list == "AE Work"
    assert summary.next is not None and summary.next.alarm_at == "2026-10-23T16:00:00Z"
    got = vault_inventory.view(
        reading,
        NOW,
        owners={f"{BOX}/b": "owner@example.com"},
        alerts=secret_alerts.infos(state),
    )
    rows = {row.name: row for row in got.vaults[0].secrets}
    assert rows["a"].alert is not None and rows["a"].alert.stage == "30"
    assert rows["a"].owner is None and rows["b"].owner == "owner@example.com"
    assert got.vaults[0].counts.no_owner == 1
    [need] = got.vaults[0].needs_you
    assert (need.name, need.alarm_at) == ("a", "2026-10-23T16:00:00Z")
    assert rows["a"].rotate_url is None and rows["a"].rotate_hint is None


# ── owners ──────────────────────────────────────────────────────────────────


def test_owner_names_read_from_the_id() -> None:
    assert secret_owners.display_name("owner.person@example.com") == "Owner Person"
    assert secret_owners.display_name("example_owner@example.com") == "Example Owner"
    assert secret_owners.display_name("@x") == "@x"


def test_the_owner_file_sets_and_clears(tmp_path: Path) -> None:
    owners = secret_owners.SecretOwnersStore(tmp_path / "owners.json")
    owners.set(BOX, ["a", "b"], "member@example.com")
    owners.set(BOX, ["b"], None)
    again = secret_owners.SecretOwnersStore(tmp_path / "owners.json")
    assert again.read() == {f"{BOX}/a": "member@example.com"}
    assert again.writable and not secret_owners.SecretOwnersStore(None).writable


def _seed_reading() -> None:
    store = secrets_meta.SecretsMetaStore(None)
    store.put(
        secrets_meta.MetaPost.model_validate(
            {
                "secrets": [{"name": "a"}, {"name": "b"}],
                "vaults": [{"vault": WEB, "secrets": [{"name": "w"}]}],
            }
        )
    )
    secrets_meta.reset_store(store)


async def test_the_owner_route_is_admin_only_and_checks_names(
    client: httpx.AsyncClient, ae_admins: set[str]
) -> None:
    _seed_reading()
    body = {"vault": BOX, "names": ["a", "b"], "owner": "member@example.com"}
    assert (await client.post("/v1/ae/vault/owner", json=body, headers=MEMBER)).status_code == 403
    got = await client.post("/v1/ae/vault/owner", json=body, headers=OWNER)
    assert got.status_code == 200, got.text
    assert got.json() == {
        "updated": 2,
        "owners": {"a": "member@example.com", "b": "member@example.com"},
    }
    unknown = {"vault": WEB, "names": ["a"], "owner": None}
    refused = await client.post("/v1/ae/vault/owner", json=unknown, headers=OWNER)
    assert refused.status_code == 400 and "not in kv-example-web" in refused.text
    bad = {"vault": BOX, "names": ["a"], "owner": "member"}
    assert (await client.post("/v1/ae/vault/owner", json=bad, headers=OWNER)).status_code == 400
    cleared = await client.post(
        "/v1/ae/vault/owner", json={"vault": BOX, "names": ["b"], "owner": None}, headers=OWNER
    )
    assert cleared.json()["owners"] == {"b": None}
    view = (await client.get("/v1/ae/vault", headers=OWNER)).json()
    rows = {row["name"]: row for row in view["vaults"][0]["secrets"]}
    assert rows["a"]["owner"] == "member@example.com" and rows["b"]["owner"] is None
    assert view["vaults"][0]["counts"]["no_owner"] == 1
    assert view["owners_editable"] is False
    assert view["alerts"]["on"] is False


async def test_the_people_route_passes_on_ids_only(
    client: httpx.AsyncClient, ae_admins: set[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    async def users(_request: Any, path: str, *, cookie: bool = True) -> httpx.Response:
        assert path == "/auth/users"
        return httpx.Response(
            200,
            json={
                "users": [
                    {"id": "owner@example.com", "is_admin": True, "last_login_at": "x"},
                    {"id": "member@example.com", "is_admin": False},
                    {"no_id": True},
                ]
            },
        )

    monkeypatch.setattr(shared_accounts_routes, "ask_as_caller", users)
    assert (await client.get("/v1/ae/vault/people", headers=MEMBER)).status_code == 403
    got = await client.get("/v1/ae/vault/people", headers=OWNER)
    assert got.json() == {
        "users": [
            {"id": "owner@example.com", "is_admin": True},
            {"id": "member@example.com", "is_admin": False},
        ]
    }

    async def refused(_request: Any, path: str, *, cookie: bool = True) -> httpx.Response:
        return httpx.Response(404, json={"detail": "Not Found"})

    monkeypatch.setattr(shared_accounts_routes, "ask_as_caller", refused)
    assert (await client.get("/v1/ae/vault/people", headers=OWNER)).json() == {"users": []}


# ── review of PR 304 (2026-10-23) ───────────────────────────────────────────


async def test_a_failed_recovery_read_files_nothing() -> None:
    fake = FakeStore()
    await _run(_reading({"a": _secret("a", 20)}), fake, secret_alerts.AlertStore(None))

    async def down(status: str = "open") -> taskstore.Result:
        return taskstore.Result(ok=False, status=503, kind="server", error="503 unavailable")

    fake.list_tasks = down  # type: ignore[method-assign]
    out = await _run(_reading({"a": _secret("a", 20)}), fake, secret_alerts.AlertStore(None))
    assert len(fake.made()) == 1 and "could not read the open tasks" in out.errors[0]


async def test_an_empty_vault_closes_nothing() -> None:
    fake, alerts = FakeStore(), secret_alerts.AlertStore(None)
    await _run(_reading({"a": _secret("a", 20)}), fake, alerts)
    out = await _run(_reading({}), fake, alerts)
    assert not out.closed and fake.tasks["T1"]["status"] == "open"


async def test_a_reopened_task_is_re_armed_not_doubled() -> None:
    fake, alerts = FakeStore(), secret_alerts.AlertStore(None)
    meta = _secret("a", 20)
    await _run(_reading({"a": meta}), fake, alerts)
    fake.tasks["T1"]["status"] = "done"
    await _run(_reading({"a": meta}), fake, alerts, now=NOW + timedelta(days=1))
    fake.tasks["T1"]["status"] = "open"
    out = await _run(_reading({"a": meta}), fake, alerts, now=NOW + timedelta(days=14))
    assert out.rearmed == [f"{BOX}/a"] and len(fake.made()) == 1


async def test_re_arming_keeps_what_a_person_wrote_below_the_key() -> None:
    fake, alerts = FakeStore(), secret_alerts.AlertStore(None)
    meta = _secret("a", 20)
    await _run(_reading({"a": meta}), fake, alerts)
    fake.tasks["T1"]["notes"] += "\n\nAsked Member to rotate it."
    await _run(_reading({"a": meta}), fake, alerts, now=NOW + timedelta(days=14))
    notes = fake.tasks["T1"]["notes"]
    assert "stage 7" in notes and notes.endswith("Asked Member to rotate it.")


def test_an_unreadable_owner_file_is_not_overwritten(tmp_path: Path) -> None:
    path = tmp_path / "owners.json"
    path.write_text("{not json", encoding="utf-8")
    owners = secret_owners.SecretOwnersStore(path)
    with pytest.raises(secret_owners.OwnerSaveError):
        owners.set(BOX, ["a"], "member@example.com")
    assert path.read_text(encoding="utf-8") == "{not json"


# ── paged rows (2026-10-24) ─────────────────────────────────────────────────

# The bridge counts two bytes per UTF-16 unit against 64 KiB: 32,768 characters.
BRIDGE_CHARS = 32_768


async def test_the_card_reads_the_summary_and_rows_in_pages_under_the_bridge_cap(
    client: httpx.AsyncClient, ae_admins: set[str]
) -> None:
    names = [f"some-service-with-a-long-name-client-secret-{i:03d}" for i in range(130)]
    store = secrets_meta.SecretsMetaStore(None)
    store.put(
        secrets_meta.MetaPost.model_validate(
            {
                "secrets": [
                    {
                        "name": name,
                        "expires": "2026-11-10T00:00:00+00:00",
                        "updated": "2026-04-15T16:16:01.123456+00:00",
                        "source": f"entra:{APP_ID}",
                        "source_expires": "2026-11-09T00:00:00+00:00",
                    }
                    for name in names
                ],
                "vaults": [{"vault": WEB, "secrets": [{"name": "w"}]}],
            }
        )
    )
    secrets_meta.reset_store(store)
    secret_owners.reset_store(secret_owners.SecretOwnersStore(None))
    secret_owners.store().set(BOX, names, "someone.with.a.long.name@example-company.com")
    summary = await client.get("/v1/ae/vault?rows=false", headers=OWNER)
    assert summary.status_code == 200 and len(summary.text) < BRIDGE_CHARS
    assert all(view["secrets"] == [] for view in summary.json()["vaults"])
    assert summary.json()["vaults"][0]["count"] == 130
    got: list[str] = []
    start: int | None = 0
    while start is not None:
        page = await client.get(f"/v1/ae/vault/rows?vault={BOX}&start={start}", headers=OWNER)
        assert page.status_code == 200, page.text
        assert len(page.text) < BRIDGE_CHARS, len(page.text)
        body = page.json()
        got += [row["name"] for row in body["secrets"]]
        start = body["next"]
    assert got == sorted(names)
    assert (await client.get(f"/v1/ae/vault/rows?vault={BOX}", headers=MEMBER)).status_code == 403
    empty = await client.get("/v1/ae/vault/rows?vault=no-such-vault", headers=OWNER)
    assert empty.json() == {"vault": "no-such-vault", "secrets": [], "next": None}
