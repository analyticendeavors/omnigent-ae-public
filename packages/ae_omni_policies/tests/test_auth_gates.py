"""Admin gates and the app-provider check on the AE routes (2026-10-22).

Public issues 4, 18 and 19: the Fabric and Databricks compute actions, the host
relink and the plan-limits post are admin only. Public issue 5: a credential the
app's own provider no longer accepts (a revoked device, a disabled account) is a
401 on every AE route, asked through ``GET /v1/me`` and cached briefly.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
import respx

from ae_omni_policies import auth

STEVE = {"X-Forwarded-Email": "steve@example.com"}
REID = {"X-Forwarded-Email": "reid@example.com"}
WAREHOUSE = "0123456789abcdef"
SNAPSHOT = {
    "harness": "claude",
    "source": "statusline",
    "observed_at": datetime(2026, 10, 22, 12, 0, tzinfo=UTC).isoformat(),
}

GATED = [
    ("/v1/ae/fabric/aetraining/wake", {}),
    ("/v1/ae/fabric/aetraining/suspend", {}),
    ("/v1/ae/fabric/aetraining/keepawake", {"hours": 2}),
    ("/v1/ae/fabric/aetraining/scale", {"sku": "F4"}),
    (f"/v1/ae/databricks/warehouses/{WAREHOUSE}/start", {}),
    (f"/v1/ae/databricks/warehouses/{WAREHOUSE}/stop", {}),
    ("/v1/ae/databricks/spend/refresh", {}),
    ("/v1/ae/host/relink", {}),
    ("/v1/ae/limits/claude", SNAPSHOT),
]


@pytest.mark.parametrize(("path", "body"), GATED, ids=[path for path, _ in GATED])
async def test_compute_relink_and_limits_writes_are_admin_only(
    client: httpx.AsyncClient, ae_admins: set[str], path: str, body: dict[str, Any]
) -> None:
    with respx.mock(assert_all_called=False) as mock:
        mock.route().respond(500)
        refused = await client.post(path, json=body, headers=STEVE)
        assert not mock.calls
        admitted = await client.post(path, json=body, headers=REID)
    assert refused.status_code == 403, refused.text
    assert refused.json()["error"]["code"] == "admin_only"
    # Past the gate an admin meets whatever comes next (unconfigured here), never the gate.
    assert admitted.status_code != 403 or admitted.json()["error"]["code"] != "admin_only"


@pytest.mark.parametrize("path", ["/v1/ae/fabric", "/v1/ae/databricks", "/v1/ae/host"])
async def test_the_compute_and_host_views_say_who_can_act(
    client: httpx.AsyncClient, ae_admins: set[str], path: str
) -> None:
    # The page hides the action buttons on can_act false (2026-10-22); the reading stays.
    member = await client.get(path, headers=STEVE)
    admin = await client.get(path, headers=REID)
    assert member.status_code == 200, member.text
    assert admin.status_code == 200, admin.text
    assert member.json()["can_act"] is False
    assert admin.json()["can_act"] is True


async def test_limits_post_checks_content_type_and_origin(client: httpx.AsyncClient) -> None:
    foreign = await client.post(
        "/v1/ae/limits/claude", json=SNAPSHOT, headers={"Origin": "https://evil.example"}
    )
    form = await client.post(
        "/v1/ae/limits/claude",
        content=b"harness=claude",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    relay = await client.post("/v1/ae/limits/claude", json=SNAPSHOT)
    assert foreign.status_code == 403, foreign.text
    assert form.status_code == 415, form.text
    assert relay.status_code == 200, relay.text


async def test_a_credential_the_app_no_longer_accepts_is_a_401(
    client: httpx.AsyncClient, ae_admins: set[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    asked: list[str] = []
    accepted = {"reid@example.com"}

    async def check(_request: Any, user: str) -> bool:
        asked.append(user)
        return user in accepted

    monkeypatch.setattr(auth, "session_check", check)
    revoked = await client.get("/v1/ae/sidebar", headers=STEVE)
    again = await client.get("/v1/ae/sidebar", headers=STEVE)
    assert revoked.status_code == 401, revoked.text
    assert again.status_code == 401
    # A no is never cached: each request asks again.
    assert asked == ["steve@example.com", "steve@example.com"]

    first = await client.get("/v1/ae/sidebar", headers=REID)
    second = await client.get("/v1/ae/sidebar", headers=REID)
    assert (first.status_code, second.status_code) == (200, 200), first.text
    # A yes is trusted for SESSION_CHECK_TTL_S, so the second request does not ask.
    assert asked.count("reid@example.com") == 1


async def test_a_yes_expires_after_the_ttl(
    client: httpx.AsyncClient, ae_admins: set[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    answers = iter([True, False])

    async def check(_request: Any, _user: str) -> bool:
        return next(answers)

    monkeypatch.setattr(auth, "session_check", check)
    monkeypatch.setattr(auth, "SESSION_CHECK_TTL_S", 0.0)
    assert (await client.get("/v1/ae/sidebar", headers=REID)).status_code == 200
    # Account disabled since: the next request asks again and is refused.
    assert (await client.get("/v1/ae/sidebar", headers=REID)).status_code == 401


async def test_the_real_check_asks_the_apps_own_provider(
    client: httpx.AsyncClient, ae_permissions: set[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    # ae_permissions gives the app its own multi-user (header) provider.
    monkeypatch.setattr(auth, "session_check", auth._app_accepts)
    response = await client.get("/v1/ae/sidebar", headers=STEVE)
    assert response.status_code == 200, response.text


async def test_single_user_runtime_never_asks(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def check(_request: Any, _user: str) -> bool:
        raise AssertionError("a single-user runtime has no account to revoke")

    monkeypatch.setattr(auth, "session_check", check)
    assert (await client.get("/v1/ae/sidebar")).status_code == 200


async def test_two_bearer_tokens_of_one_user_never_share_a_cached_yes(
    client: httpx.AsyncClient, ae_admins: set[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    async def check(request: Any, _user: str) -> bool:
        return request.headers.get("authorization") == "Bearer good"

    monkeypatch.setattr(auth, "session_check", check)
    good = await client.get("/v1/ae/sidebar", headers={**REID, "Authorization": "Bearer good"})
    revoked = await client.get(
        "/v1/ae/sidebar", headers={**REID, "Authorization": "Bearer revoked"}
    )
    assert good.status_code == 200, good.text
    assert revoked.status_code == 401, revoked.text


async def test_no_answer_from_the_app_is_a_503_not_a_sign_out(
    client: httpx.AsyncClient, ae_admins: set[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    async def check(_request: Any, _user: str) -> None:
        return None

    monkeypatch.setattr(auth, "session_check", check)
    response = await client.get("/v1/ae/sidebar", headers=REID)
    assert response.status_code == 503, response.text


async def test_the_relay_origin_sentinel_passes_the_origin_check(
    client: httpx.AsyncClient,
) -> None:
    from ae_omni_policies import omni

    origin = omni.Settings().headers()["origin"]
    response = await client.post("/v1/ae/limits/claude", json=SNAPSHOT, headers={"Origin": origin})
    assert response.status_code == 200, response.text
