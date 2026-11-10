"""Saved GitHub tokens, the token check and personal Git lists (2026-11-08, card setup PR 3).

Nothing here reaches GitHub or writes to /data: transports are ``httpx.MockTransport`` and every
file lives under ``tmp_path``. Names are made up (octo-user, example-org, owner@example.com).
"""

from __future__ import annotations

import json
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from cryptography.fernet import Fernet

from ae_omni_policies import card_secrets, git_personal, git_tokens

MARKER = "ZzSecretMarker9"
TOKEN = "github_pat_" + MARKER + "A1b2C3d4E5F6g7H8i9J0" * 3
OTHER = "github_pat_" + "Q9w8E7r6T5y4U3i2O1p0" * 4


@pytest.fixture()
def key(monkeypatch: pytest.MonkeyPatch) -> str:
    value = Fernet.generate_key().decode()
    monkeypatch.setenv(card_secrets.KEY_ENV, value)
    return value


def _put(store: card_secrets.CardSecretsStore, record_id: str, token: str = TOKEN) -> None:
    store.put(
        record_id,
        token,
        saved_by="owner@example.com",
        github_login="Octo-User",
        expires_at="2026-12-01T00:00:00Z",
    )


# ── card_secrets ──────────────────────────────────────────────────────────────────────────────


def test_a_saved_token_is_encrypted_0600_and_never_in_meta(tmp_path: Path, key: str) -> None:
    path = tmp_path / "secrets.json"
    store = card_secrets.CardSecretsStore(path)
    _put(store, "company/git/example-org")
    assert store.get_token("company/git/example-org") == TOKEN
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert TOKEN not in path.read_text() and MARKER not in path.read_text()
    meta = store.meta("company/git/example-org")
    assert meta["set"] is True and meta["github_login"] == "Octo-User"
    assert meta["saved_by"] == "owner@example.com" and meta["expires_at"] == "2026-12-01T00:00:00Z"
    listed = store.list_meta("company/")
    assert MARKER not in json.dumps([meta, listed]) and "ciphertext" not in json.dumps(listed)
    assert store.meta("company/git/nobody")["set"] is False
    # A second store on the same file reads it back.
    assert card_secrets.CardSecretsStore(path).get_token("company/git/example-org") == TOKEN


def test_without_the_key_nothing_saves_and_nothing_decrypts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "secrets.json"
    first = Fernet.generate_key().decode()
    monkeypatch.setenv(card_secrets.KEY_ENV, first)
    _put(card_secrets.CardSecretsStore(path), "company/git/example-org")
    monkeypatch.delenv(card_secrets.KEY_ENV)
    store = card_secrets.CardSecretsStore(path)
    with pytest.raises(card_secrets.KeyMissingError) as caught:
        _put(store, "company/git/other")
    assert caught.value.code == "card_secrets_key_missing" and caught.value.status_code == 409
    assert card_secrets.KEY_ENV in str(caught.value)
    assert store.get_token("company/git/example-org") is None
    meta = store.meta("company/git/example-org")
    assert meta["set"] is True and "unreadable" not in meta
    assert card_secrets.key_configured() is False
    monkeypatch.setenv(card_secrets.KEY_ENV, "not-a-key")
    with pytest.raises(card_secrets.KeyInvalidError):
        card_secrets.require_key()


def test_a_key_that_cannot_decrypt_marks_the_record_unreadable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "secrets.json"
    monkeypatch.setenv(card_secrets.KEY_ENV, Fernet.generate_key().decode())
    _put(card_secrets.CardSecretsStore(path), "company/git/example-org")
    monkeypatch.setenv(card_secrets.KEY_ENV, Fernet.generate_key().decode())
    store = card_secrets.CardSecretsStore(path)
    assert store.get_token("company/git/example-org") is None
    assert store.meta("company/git/example-org")["unreadable"] is True
    assert store.list_meta("company/")["company/git/example-org"]["unreadable"] is True


def test_rotating_the_key_reseals_every_record_under_the_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "secrets.json"
    old, new = Fernet.generate_key().decode(), Fernet.generate_key().decode()
    monkeypatch.setenv(card_secrets.KEY_ENV, old)
    _put(card_secrets.CardSecretsStore(path), "company/git/example-org")
    before = json.loads(path.read_text())["records"]["company/git/example-org"]["ciphertext"]
    monkeypatch.setenv(card_secrets.KEY_ENV, f"{new},{old}")
    assert card_secrets.CardSecretsStore(path).get_token("company/git/example-org") == TOKEN
    after = json.loads(path.read_text())["records"]["company/git/example-org"]["ciphertext"]
    assert after != before
    # The old key can go now: the new one alone opens the re-sealed record.
    monkeypatch.setenv(card_secrets.KEY_ENV, new)
    assert card_secrets.CardSecretsStore(path).get_token("company/git/example-org") == TOKEN


def test_delete_and_delete_prefix_and_ids(tmp_path: Path, key: str) -> None:
    store = card_secrets.CardSecretsStore(tmp_path / "secrets.json")
    _put(store, card_secrets.user_id("owner@example.com", "Octo-User"))
    _put(store, card_secrets.user_id("owner@example.com", "example-org"))
    _put(store, card_secrets.user_id("other@example.com", "octo-user"))
    _put(store, card_secrets.company_id("Example-Org"))
    assert store.delete_prefix(card_secrets.user_prefix("owner@example.com")) == 2
    assert store.meta(card_secrets.user_id("other@example.com", "octo-user"))["set"] is True
    assert store.delete(card_secrets.company_id("example-org")) is True
    assert store.delete(card_secrets.company_id("example-org")) is False
    with pytest.raises(ValueError):
        _put(store, "user/a/b/git/x")  # a login with a slash cannot name a record
    assert card_secrets.valid_login("owner@example.com") and not card_secrets.valid_login("a/b")


def test_an_unreadable_file_is_never_overwritten(tmp_path: Path, key: str) -> None:
    path = tmp_path / "secrets.json"
    path.write_text("{broken", encoding="utf-8")
    store = card_secrets.CardSecretsStore(path)
    with pytest.raises(card_secrets.SaveError):
        _put(store, "company/git/example-org")
    assert path.read_text() == "{broken"


# ── git_tokens ────────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "bad",
    [
        "ghp_" + "a" * 36,
        "github_pat_short",
        "github_pat_" + "a" * 300,
        "github_pat_" + MARKER + "-" * 40,
        "github_pat_" + MARKER + " " * 3 + "a" * 40,
        123,
        None,
    ],
)
def test_a_bad_token_is_refused_and_the_message_holds_no_part_of_it(bad: Any) -> None:
    with pytest.raises(git_tokens.TokenError) as caught:
        git_tokens.check_token(bad)
    message = str(caught.value)
    assert MARKER not in message and "github_pat_" + "a" * 5 not in message
    assert caught.value.status_code == 422


def test_a_good_token_is_trimmed() -> None:
    assert git_tokens.check_token(f"  {TOKEN}\n") == TOKEN
    assert git_tokens.check_token("github_pat_" + "a" * 29) == "github_pat_" + "a" * 29


def test_the_expiry_header_parses_to_utc() -> None:
    assert git_tokens.parse_expiry("2026-02-05 00:00:00 UTC") == "2026-02-05T00:00:00Z"
    assert git_tokens.parse_expiry("2026-02-05 00:00:00 -0800") == "2026-02-05T08:00:00Z"
    assert git_tokens.parse_expiry("2026-02-05 12:30:00 +0530") == "2026-02-05T07:00:00Z"
    assert git_tokens.parse_expiry(None) is None
    assert git_tokens.parse_expiry("never") is None


def _github(handler: Any) -> httpx.MockTransport:
    return httpx.MockTransport(handler)


async def test_verify_asks_the_fixed_host_whatever_the_environment_says(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AE_GITHUB_API_URL", "https://evil.example.test")
    seen: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.url.host, request.url.path))
        return httpx.Response(
            200,
            json={"login": "Octo-User"},
            headers={"github-authentication-token-expiration": "2026-02-05 00:00:00 UTC"},
        )

    login, expires = await git_tokens.verify(TOKEN, transport=_github(handler))
    assert (login, expires) == ("Octo-User", "2026-02-05T00:00:00Z")
    assert seen == [("api.github.com", "/user")]


async def test_verify_tells_a_refusal_from_a_hiccup() -> None:
    with pytest.raises(git_tokens.TokenRefused) as refused:
        await git_tokens.verify(TOKEN, transport=_github(lambda _r: httpx.Response(401)))
    assert refused.value.status_code == 422 and MARKER not in str(refused.value)
    with pytest.raises(git_tokens.TokenCheckUnavailable) as down:
        await git_tokens.verify(TOKEN, transport=_github(lambda _r: httpx.Response(503)))
    assert down.value.status_code == 502 and MARKER not in str(down.value)

    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route", request=request)

    with pytest.raises(git_tokens.TokenCheckUnavailable):
        await git_tokens.verify(TOKEN, transport=_github(boom))


async def test_visible_repos_pages_three_times_and_caps_at_300() -> None:
    pages: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params["page"])
        pages.append(page)
        assert request.url.host == "api.github.com"
        assert request.url.params["affiliation"] == "owner,collaborator,organization_member"
        rows = [
            {"full_name": f"example-org/repo-{page}-{n}", "private": n % 2 == 0}
            for n in range(100)
        ]
        return httpx.Response(200, json=rows)

    repos = await git_tokens.visible_repos(TOKEN, transport=_github(handler))
    assert pages == [1, 2, 3] and len(repos) == 300
    assert repos[0] == {"repo": "example-org/repo-1-0", "private": True}


# ── git_personal ──────────────────────────────────────────────────────────────────────────────


def test_normalize_lowercases_accounts_drops_repeats_and_caps() -> None:
    kept = git_personal.normalize(
        [
            {"repo": "example-org/web", "account": "Octo-User"},
            {"repo": "Example-Org/WEB", "account": "octo-user"},
            {"repo": "example-org/web", "account": "other-user"},
        ]
    )
    assert kept == [
        {"repo": "example-org/web", "account": "octo-user"},
        {"repo": "example-org/web", "account": "other-user"},
    ]
    for bad in ({"repo": "nope", "account": "a"}, {"repo": "example-org/web", "account": ""}):
        with pytest.raises(ValueError):
            git_personal.normalize([bad])
    many = [{"repo": f"example-org/r{n}", "account": "a"} for n in range(31)]
    with pytest.raises(ValueError):
        git_personal.normalize(many)
    assert len(git_personal.normalize(many[:30])) == 30


def test_the_store_revisions_conflicts_touches_hourly_and_is_0600(tmp_path: Path) -> None:
    path = tmp_path / "personal.json"
    store = git_personal.PersonalStore(path)
    now = datetime(2026, 11, 8, 12, 0, tzinfo=UTC)
    repos = [{"repo": "example-org/web", "account": "octo-user"}]
    assert store.get("a@example.com") == (0, [])
    assert store.touch("a@example.com", now=now) is False  # no list yet: nothing recorded
    done = store.put("a@example.com", repos, 0, now=now)
    assert done.conflict is False and done.revision == 1
    stale = store.put("a@example.com", [], 0, now=now)
    assert stale.conflict is True and stale.revision == 1 and stale.repos == repos
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    # put stamps seen_at, so a touch inside the hour writes nothing.
    assert store.touch("a@example.com", now=now + timedelta(minutes=5)) is False
    assert git_personal.PersonalStore(path).get("a@example.com") == (1, repos)
    assert store.active(now) == ["a@example.com"]
    assert store.active(now + timedelta(days=8)) == []
    assert store.counts() == {"a@example.com": (1, 1)}


def test_clear_user_drops_the_list_and_every_token_of_that_login_only(key: str) -> None:
    store = git_personal.store()
    store.put("a@example.com", [{"repo": "example-org/web", "account": "octo-user"}], 0)
    store.put("b@example.com", [{"repo": "example-org/api", "account": "octo-user"}], 0)
    for login in ("a@example.com", "b@example.com"):
        card_secrets.put(
            card_secrets.user_id(login, "octo-user"),
            TOKEN,
            saved_by=login,
            github_login="Octo-User",
            expires_at=None,
        )
    assert git_personal.clear_user("a@example.com") == (True, 1)
    assert store.get("a@example.com") == (0, [])
    assert git_personal.token_for("a@example.com", "octo-user") == ""
    assert git_personal.token_for("b@example.com", "octo-user") == TOKEN
    assert store.get("b@example.com")[1]


class _Asked:
    """Stands in for ``ask_as_caller``: counts the asks and answers what it is given."""

    def __init__(self, answer: httpx.Response | None) -> None:
        self.answer = answer
        self.count = 0

    async def __call__(self, _request: Any, path: str, **_kw: Any) -> httpx.Response | None:
        assert path.startswith("/auth/users")
        self.count += 1
        return self.answer


def _roster(*people: tuple[str, bool]) -> httpx.Response:
    return httpx.Response(
        200, json={"users": [{"id": name, "is_admin": admin} for name, admin in people]}
    )


@pytest.fixture()
def two_people(key: str) -> None:
    store = git_personal.store()
    for login in ("gone@example.com", "member@example.com", "owner@example.com"):
        store.put(login, [{"repo": "example-org/web", "account": "octo-user"}], 0)
        card_secrets.put(
            card_secrets.user_id(login, "octo-user"),
            TOKEN,
            saved_by=login,
            github_login="Octo-User",
            expires_at=None,
        )


async def test_a_roster_without_the_login_clears_it_and_a_non_admin_is_only_skipped(
    monkeypatch: pytest.MonkeyPatch, two_people: None
) -> None:
    from ae_omni_policies import shared_accounts_routes

    asked = _Asked(_roster(("owner@example.com", True), ("member@example.com", False)))
    monkeypatch.setattr(shared_accounts_routes, "ask_as_caller", asked)
    await git_personal.sync_roster(object(), now=1000.0)  # type: ignore[arg-type]
    assert git_personal.store().get("gone@example.com") == (0, [])
    assert git_personal.token_for("gone@example.com", "octo-user") == ""
    # Present but not an admin: kept, skipped by every read.
    assert git_personal.store().get("member@example.com")[1]
    assert git_personal.token_for("member@example.com", "octo-user") == TOKEN
    assert git_personal.is_active_admin("member@example.com") is False
    assert git_personal.is_active_admin("owner@example.com") is True
    # Asked again inside ten minutes: not asked.
    await git_personal.sync_roster(object(), now=1000.0 + 599)  # type: ignore[arg-type]
    assert asked.count == 1
    await git_personal.sync_roster(object(), now=1000.0 + 601)  # type: ignore[arg-type]
    assert asked.count == 2


@pytest.mark.parametrize(
    "answer",
    [None, httpx.Response(500), httpx.Response(401), _roster(), httpx.Response(200, text="x")],
)
async def test_a_failed_or_empty_roster_deletes_nothing(
    monkeypatch: pytest.MonkeyPatch, two_people: None, answer: httpx.Response | None
) -> None:
    from ae_omni_policies import shared_accounts_routes

    monkeypatch.setattr(shared_accounts_routes, "ask_as_caller", _Asked(answer))
    await git_personal.sync_roster(object(), now=1000.0)  # type: ignore[arg-type]
    for login in ("gone@example.com", "member@example.com", "owner@example.com"):
        assert git_personal.store().get(login)[1]
        assert git_personal.token_for(login, "octo-user") == TOKEN
    assert git_personal.roster() is None  # no good answer: nothing held


async def test_a_roster_that_fills_the_limit_prunes_nobody_and_asks_for_the_most(
    monkeypatch: pytest.MonkeyPatch, two_people: None
) -> None:
    from ae_omni_policies import shared_accounts_routes

    asked_paths: list[str] = []
    full = _roster(
        *((f"person{n}@example.com", True) for n in range(git_personal.ROSTER_LIMIT - 1)),
        ("owner@example.com", True),
    )

    async def ask(_request: Any, path: str, **_kw: Any) -> httpx.Response:
        asked_paths.append(path)
        return full

    monkeypatch.setattr(shared_accounts_routes, "ask_as_caller", ask)
    await git_personal.sync_roster(object(), now=1000.0, caller="owner@example.com")  # type: ignore[arg-type]
    assert asked_paths == [f"/auth/users?limit={git_personal.ROSTER_LIMIT}"]
    # The answer may be cut short, so a stored login missing from it is not "gone"...
    for login in ("gone@example.com", "member@example.com", "owner@example.com"):
        assert git_personal.store().get(login)[1]
        assert git_personal.token_for(login, "octo-user") == TOKEN
    # ...and it is not taken as "not an admin" either; one that is listed keeps its flag.
    assert git_personal.is_active_admin("gone@example.com") is True
    assert git_personal.roster() is not None
    # One fewer than the limit is the whole list: now the missing login is pruned.
    git_personal.reset_roster()
    short = _roster(*((f"p{n}@example.com", True) for n in range(5)), ("owner@example.com", True))
    monkeypatch.setattr(shared_accounts_routes, "ask_as_caller", _Asked(short))
    await git_personal.sync_roster(object(), now=2000.0, caller="owner@example.com")  # type: ignore[arg-type]
    assert git_personal.store().get("gone@example.com") == (0, [])


async def test_the_caller_is_never_pruned_even_when_the_roster_leaves_them_out(
    monkeypatch: pytest.MonkeyPatch, two_people: None
) -> None:
    from ae_omni_policies import shared_accounts_routes

    monkeypatch.setattr(
        shared_accounts_routes,
        "ask_as_caller",
        _Asked(_roster(("member@example.com", False), ("other@example.com", True))),
    )
    await git_personal.sync_roster(object(), now=1000.0, caller="owner@example.com")  # type: ignore[arg-type]
    assert git_personal.store().get("owner@example.com")[1]
    assert git_personal.token_for("owner@example.com", "octo-user") == TOKEN
    assert git_personal.store().get("gone@example.com") == (0, [])  # others still go


def test_a_record_this_version_does_not_know_survives_a_save(tmp_path: Path, key: str) -> None:
    path = tmp_path / "secrets.json"
    odd = {"ciphertext": "x", "note": "written by a newer version"}
    path.write_text(
        json.dumps({"version": 1, "records": {"future/kind/thing": odd, "bad": "not an object"}}),
        encoding="utf-8",
    )
    store = card_secrets.CardSecretsStore(path)
    _put(store, "company/git/example-org")
    store.delete("company/git/example-org")
    records = json.loads(path.read_text())["records"]
    assert records == {"future/kind/thing": odd, "bad": "not an object"}
    # Unknown entries are never served or listed.
    assert store.list_meta("") == {}
    assert store.meta("future/kind/thing")["set"] is False
