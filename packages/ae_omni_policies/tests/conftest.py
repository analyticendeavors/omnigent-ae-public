"""Test plumbing for the AE layer.

The suite runs from ``upstream/`` (``just test``), so upstream's own server
fixtures are reused rather than copied: ``tests.conftest`` (``db_uri``, the
environment guardrails) and ``tests.server.conftest`` (``runtime_init``,
``mock_llm``, ``client``, the elicitation resets). Only ``app`` is overridden,
to build the FastAPI app with the AE layer loaded the way ``config/server.yaml``
loads it: ``policy_modules`` and ``debug_router_modules``.

This directory has no ``__init__.py`` on purpose: upstream's fixtures live in a
package named ``tests`` and a second package of that name would shadow it.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI

pytest_plugins = ["tests.conftest", "tests.server.conftest"]


@pytest.fixture(autouse=True)
def _ae_environment(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin the env the AE layer reads, and reset its per-process caches.

    Header auth on a single-user runtime is what ``omnigent server`` sets up on
    a loopback bind: every request without an identity header is ``local``.
    The task-store settings point at a host only respx answers.
    """
    monkeypatch.setenv("OMNIGENT_AUTH_PROVIDER", "header")
    monkeypatch.setenv("OMNIGENT_LOCAL_SINGLE_USER", "1")
    monkeypatch.setenv("AE_TASK_STORE_URL", "https://tasks.test")
    monkeypatch.setenv("AE_CF_ACCESS_CLIENT_ID", "test-client-id.access")
    monkeypatch.setenv("AE_CF_ACCESS_CLIENT_SECRET", "test-client-secret")
    monkeypatch.delenv("AE_SELF_URL", raising=False)
    monkeypatch.setenv("AE_VAULT_NAME", "kv-example-box")
    # create_session reads the calling session from these; a test that wants one sets it.
    monkeypatch.delenv("OMNIGENT_RUNNER_PRIMARY_SESSION_ID", raising=False)
    monkeypatch.delenv("OMNIGENT_TERMINAL_LAUNCH_SESSION_ID", raising=False)
    # The merge watcher polls GitHub from the app's lifespan; only its own tests run it.
    monkeypatch.setenv("AE_MERGE_WATCH", "0")
    # The auto-archive sweep likewise; only its own tests run it.
    monkeypatch.setenv("AE_AUTO_ARCHIVE", "0")
    # Expiry alerts run after every vault reading; only their own tests run them.
    monkeypatch.setenv("AE_SECRET_ALERTS", "off")
    monkeypatch.setenv("AE_SECRET_OWNERS_FILE", "")
    monkeypatch.setenv("AE_SECRET_ALERTS_FILE", "")
    # Cost targets keep their rules and alert state in memory; only their own tests use files.
    monkeypatch.setenv("AE_COST_TARGETS_FILE", "")
    monkeypatch.setenv("AE_COST_ALERT_STATE_FILE", "")
    from ae_omni_policies import (
        auth,
        cost_targets,
        prs,
        router,
        secret_alerts,
        secret_owners,
        secrets_meta,
        slot_cap,
        taskstore,
    )

    async def accepted(_request: Any, _user: str) -> bool:
        return True

    # The app-provider check (GET /v1/me) has its own tests in test_auth_gates.py.
    monkeypatch.setattr(auth, "session_check", accepted)
    auth.reset_provider_cache()
    slot_cap.reset_pending_claims()
    taskstore.reset_icon_set()
    # The lot, tag and pull request caches outlive a request (2026-09-26).
    router.reset_lot_cache()
    prs.reset_cache()
    secrets_meta.reset_store()
    secret_owners.reset_store()
    secret_alerts.reset_store()
    cost_targets.reset_stores()
    yield
    auth.reset_provider_cache()
    slot_cap.reset_pending_claims()
    taskstore.reset_icon_set()
    router.reset_lot_cache()
    prs.reset_cache()
    secrets_meta.reset_store()
    secret_owners.reset_store()
    secret_alerts.reset_store()
    cost_targets.reset_stores()


@pytest.fixture()
def ae_admins(monkeypatch: pytest.MonkeyPatch) -> set[str]:
    """Multi-user header auth with reid@example.com the only admin.

    The admin question is answered from the returned set instead of ``GET /v1/me``
    (``shared_accounts_routes.admin_probe``, which the admin-only routes ask).
    Name the caller with an ``X-Forwarded-Email`` request header.
    """
    from ae_omni_policies import auth, shared_accounts_routes

    monkeypatch.delenv("OMNIGENT_LOCAL_SINGLE_USER")
    auth.reset_provider_cache()
    allowed = {"reid@example.com"}

    async def probe(_request: Any, user: str | None) -> bool:
        return user in allowed

    monkeypatch.setattr(shared_accounts_routes, "admin_probe", probe)
    return allowed


@pytest.fixture()
def ae_stores(runtime_init: None, db_uri: str, tmp_path: Path) -> dict[str, Any]:
    """The stores ``create_app`` takes, built once per test so a test can reach them.

    A copy of what ``tests/server/conftest.py`` ``app`` builds at v0.13.0,
    unchanged through v0.18.0 (re-check when the pin moves), plus the two config keys the AE layer
    needs. ``test_agents.py`` seeds built-in agents through the same stores
    the app serves; ``test_schedules.py`` adds a scheduled-task store.

    :param runtime_init: Upstream fixture: stores initialized, mock LLM patched.
    :param db_uri: SQLite database URI.
    :param tmp_path: Pytest temp directory for artifacts and cache.
    """
    from omnigent.runtime.agent_cache import AgentCache
    from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
    from omnigent.stores.artifact_store.local import LocalArtifactStore
    from omnigent.stores.comment_store.sqlalchemy_store import SqlAlchemyCommentStore
    from omnigent.stores.conversation_store.sqlalchemy_store import (
        SqlAlchemyConversationStore,
    )
    from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore

    artifact_store = LocalArtifactStore(str(tmp_path / "artifacts"))
    return {
        "agent_store": SqlAlchemyAgentStore(db_uri),
        "file_store": SqlAlchemyFileStore(db_uri),
        "conversation_store": SqlAlchemyConversationStore(db_uri),
        "artifact_store": artifact_store,
        "agent_cache": AgentCache(artifact_store=artifact_store, cache_dir=tmp_path / "cache"),
        "comment_store": SqlAlchemyCommentStore(db_uri),
        "policy_modules": ["ae_omni_policies"],
        "debug_router_modules": ["ae_omni_policies.router"],
    }


@pytest.fixture()
def ae_permissions(ae_admins: set[str]) -> set[str]:
    """Multi-user (:func:`ae_admins`) with upstream's session permissions on.

    A session belongs to the user who created it, so ``GET /v1/sessions/{id}``
    and the ``accessible_by`` listing answer per user, as on a multi-user server.
    """
    return ae_admins


@pytest.fixture()
def app(ae_stores: dict[str, Any], db_uri: str, request: pytest.FixtureRequest) -> FastAPI:
    """The app upstream's ``client`` fixture drives, with the AE layer loaded.

    A test that asks for :func:`ae_permissions` gets the permission store and the
    header auth provider upstream's ``auth_app`` fixture adds.
    """
    from omnigent.server.app import create_app

    if "ae_permissions" not in request.fixturenames:
        return create_app(**ae_stores)
    from omnigent.server.auth import UnifiedAuthProvider
    from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore

    return create_app(
        **ae_stores,
        permission_store=SqlAlchemyPermissionStore(db_uri),
        auth_provider=UnifiedAuthProvider(source="header"),
    )
