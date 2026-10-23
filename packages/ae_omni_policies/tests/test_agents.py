"""The shipped agent YAMLs, loaded the way ``OMNIGENT_BUILTIN_AGENT_DIRS`` loads them.

``_ensure_extra_builtin_agents`` (``omnigent/server/app.py``) materializes
each entry with ``materialize_bundle`` and registers it under the directory
name; the spec is parsed by ``omnigent.spec.load`` when a session binds it.
Both steps run here on the real files under ``agents/``.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import httpx
import pytest
from omnigent.server import app as server_app
from omnigent.spec import load as load_spec
from omnigent.spec import materialize_bundle

from ae_omni_policies import docker_entrypoint, labels

AGENTS = Path(__file__).resolve().parents[3] / "agents"
EXPECTED = {
    "steward": ("claude-sdk", ["list_parked", "omni_capacity"]),
    "shutdown": ("claude-sdk", ["omni_capacity", "omni_transcript"]),
    "reviewer-codex": ("codex", None),
    "orchestrator": (
        "claude-sdk",
        [
            "omni_capacity",
            "omni_transcript",
            "omni_set_status",
            "list_parked",
            "create_session",
            "post_message",
            "schedule_checkin",
            "list_prs",
            "ask_reid",
        ],
    ),
}

# Box memory (Phase 7, 2026-09-24): the business Graphiti MCP over streamable
# HTTP on the box's tailnet name, never the personal graph on 8001. Its write
# tools, as the Graphiti README in agents-and-automation lists them, must never
# reach an agent's allowlist; get_status is harmless but not needed. The specs
# ship a placeholder url that the image's entrypoint fills from AE_GRAPH_MCP_URL
# before registering them (2026-10-21); the box's value stands in for it here.
BOX_MEMORY = "box_memory"
BOX_MEMORY_URL = "http://box.example:8000/mcp"
BOX_MEMORY_READ = ["search_nodes", "search_memory_facts", "get_episodes", "get_entity_edge"]
BOX_MEMORY_WRITE = {"add_memory", "delete_episode", "delete_entity_edge", "clear_graph"}
WITH_BOX_MEMORY = ("steward", "shutdown")


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_yaml_parses_with_harness_allowlist_and_seeded_status(name: str, tmp_path: Path) -> None:
    harness, allowlist = EXPECTED[name]
    spec = load_spec(materialize_bundle(AGENTS / name, tmp_path / name))
    assert spec.name == name
    assert spec.executor.config.get("harness") == harness
    assert spec.executor.model is None, "no model pinned; the host's provider default applies"
    assert spec.guardrails is not None and spec.guardrails.labels is not None
    assert spec.guardrails.labels[labels.STATUS].initial == labels.REFERENCE
    if allowlist is None:
        assert spec.mcp_servers == []
        return
    servers = {server.name: server for server in spec.mcp_servers}
    assert set(servers) == ({"ae", BOX_MEMORY} if name in WITH_BOX_MEMORY else {"ae"})
    server = servers["ae"]
    assert server.transport == "stdio"
    assert server.command == "python"
    assert server.args == ["-m", "ae_omni_policies.mcp"]
    assert server.env == {}, "the subprocess inherits the host's environment; no secrets in YAML"
    assert server.tools == allowlist


def _rendered(name: str, tmp_path: Path, url: str | None) -> Path:
    """The agent directory the image's entrypoint registers, with *url* in the env or not."""
    env = {docker_entrypoint.BUILTIN_AGENTS_ENV: str(AGENTS / name)}
    if url is not None:
        env[docker_entrypoint.GRAPH_MCP_ENV] = url
    root = tmp_path / "rendered"
    root.mkdir()
    assert docker_entrypoint.render_agent_dirs(env, root) is not None
    return Path(env[docker_entrypoint.BUILTIN_AGENTS_ENV])


@pytest.mark.parametrize("name", WITH_BOX_MEMORY)
def test_box_memory_ships_the_placeholder(name: str, tmp_path: Path) -> None:
    """The repo file carries no host; upstream's loader leaves the token as is."""
    text = (AGENTS / name / "agent.yaml").read_text(encoding="utf-8")
    assert "box.example:8000/mcp" not in text.replace(f"(on the box: {BOX_MEMORY_URL})", "")
    spec = load_spec(materialize_bundle(AGENTS / name, tmp_path / name))
    (memory,) = [server for server in spec.mcp_servers if server.name == BOX_MEMORY]
    assert memory.url == docker_entrypoint.GRAPH_MCP_TOKEN


@pytest.mark.parametrize("name", WITH_BOX_MEMORY)
def test_box_memory_is_the_business_graph_and_read_only(name: str, tmp_path: Path) -> None:
    """Upstream's loader sees an HTTP MCP whose allowlist holds no write tool."""
    source = _rendered(name, tmp_path, BOX_MEMORY_URL)
    assert docker_entrypoint.GRAPH_MCP_TOKEN not in (source / "agent.yaml").read_text()
    spec = load_spec(materialize_bundle(source, tmp_path / name))
    assert spec.name == name
    (memory,) = [server for server in spec.mcp_servers if server.name == BOX_MEMORY]
    assert memory.transport == "http"
    assert memory.url == BOX_MEMORY_URL
    assert memory.headers == {}, "Tailscale is the boundary; no token in YAML"
    assert memory.tools == BOX_MEMORY_READ
    assert not set(memory.tools) & BOX_MEMORY_WRITE


@pytest.mark.parametrize("name", WITH_BOX_MEMORY)
def test_without_the_url_the_agent_registers_without_box_memory(
    name: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A deploy with no Graphiti still loads the agent, with the AE tools only."""
    source = _rendered(name, tmp_path, "")
    assert docker_entrypoint.GRAPH_MCP_TOKEN not in (source / "agent.yaml").read_text()
    spec = load_spec(materialize_bundle(source, tmp_path / name))
    harness, allowlist = EXPECTED[name]
    assert spec.name == name
    assert spec.executor.config.get("harness") == harness
    assert spec.guardrails is not None and spec.guardrails.labels is not None
    assert spec.guardrails.labels[labels.STATUS].initial == labels.REFERENCE
    assert [server.name for server in spec.mcp_servers] == ["ae"]
    assert spec.mcp_servers[0].tools == allowlist
    raw = load_spec(materialize_bundle(AGENTS / name, tmp_path / "raw"))
    assert spec.instructions == raw.instructions and spec.description == raw.description
    assert docker_entrypoint.GRAPH_MCP_ENV in capsys.readouterr().err


def test_no_agent_reaches_a_memory_write_tool_or_the_personal_graph() -> None:
    """Every shipped spec, including ones that do not attach box memory today."""
    for path in sorted(AGENTS.glob("*/agent.yaml")):
        text = path.read_text(encoding="utf-8")
        assert ":8001" not in text, f"{path.parent.name} reaches the personal graph"
        for tool in BOX_MEMORY_WRITE:
            assert f"- {tool}" not in text, f"{path.parent.name} allows {tool}"


def test_prompts_are_plain_english() -> None:
    """No glyphs, no em dashes, no double hyphens (the writing rules)."""
    banned = ("—", "--", "\U0001f7e1", "\U0001f534", "✅", "\U0001f4cc", "\U0001f5c2")
    for path in sorted(AGENTS.glob("*/agent.yaml")):
        text = path.read_text(encoding="utf-8")
        for token in banned:
            assert token not in text, f"{path.name} carries {token!r}"


def test_reviewer_is_read_only_by_policy(tmp_path: Path) -> None:
    spec = load_spec(materialize_bundle(AGENTS / "reviewer-codex", tmp_path / "r"))
    assert spec.os_env is not None and spec.os_env.type == "caller_process"
    assert spec.guardrails is not None
    names = {policy.name for policy in spec.guardrails.policies}
    assert names == {"read_only_os", "blast_radius"}


async def test_builtin_agent_dirs_registers_the_shipped_agents(
    client: httpx.AsyncClient, ae_stores: dict[str, Any]
) -> None:
    """The env line the README documents seeds both agents; the catalog lists them."""
    agent_store = ae_stores["agent_store"]
    artifact_store = ae_stores["artifact_store"]
    agent_cache = ae_stores["agent_cache"]
    env = os.pathsep.join(str(AGENTS / name) for name in ("steward", "shutdown", "orchestrator"))
    os.environ[server_app._EXTRA_BUILTIN_AGENTS_ENV] = env
    try:
        server_app._ensure_extra_builtin_agents(agent_store, artifact_store, agent_cache)
    finally:
        del os.environ[server_app._EXTRA_BUILTIN_AGENTS_ENV]
    response = await client.get("/v1/agents", params={"limit": 1000})
    assert response.status_code == 200, response.text
    rows = {row["name"]: row for row in response.json()["data"]}
    assert rows["steward"]["harness"] == "claude-sdk"
    assert rows["shutdown"]["harness"] == "claude-sdk"
    assert rows["orchestrator"]["harness"] == "claude-sdk"
    assert "reviewer-codex" not in rows, "off by default: not in the documented env line"


def test_the_orchestrator_prompt_carries_reids_rules() -> None:
    """Four lines, a full URL for every pull request, pop-ups, never a merge."""
    text = (AGENTS / "orchestrator" / "agent.yaml").read_text(encoding="utf-8")
    for rule in (
        "At most four short lines",
        "give its full URL exactly as a",
        "ask_reid",
        "the recommended one first",
        "You never merge",
        "never print or store a token",
        "Never call ask_reid in a scheduled",
        "If gh.available is",
    ):
        assert rule in text, rule
