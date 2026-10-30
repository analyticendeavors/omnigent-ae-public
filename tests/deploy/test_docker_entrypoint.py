"""The image's entrypoint loads the AE layer.

Upstream's Docker entrypoint (``upstream/deploy/docker/entrypoint.py``,
``build_app``) ignores ``policy_modules`` and ``debug_router_modules``, so the
first box deploy on 2026-09-24 ran with every ``/v1/ae`` route answering 404
and the slot cap never loaded. ``ae_omni_policies.docker_entrypoint`` wraps
``create_app`` to pass both keys from the same config file. These tests build
the real container app the way the image does and check that the AE routes
are mounted, and pin the image's CMD to the wrapper. Since 2026-10-18 the
wrapper also seeds the Global Policies page from ``ae_seed_policies``, once,
and since 2026-10-20 adds the session cookie expiry middleware under OIDC.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
UPSTREAM = ROOT / "upstream"

_PROBE = """
import json, sys
sys.path.insert(0, {upstream!r})
if {wrap!r}:
    from ae_omni_policies import docker_entrypoint
    docker_entrypoint.install()
    docker_entrypoint.install()  # idempotent
import deploy.docker.entrypoint as ep
built = ep.build_app()
paths = sorted({{getattr(r, "path", "") for r in built.app.routes}})
print(json.dumps([p for p in paths if p.startswith("/v1/ae")]))
"""


# One container boot against a migrated database: build the app, then list the
# global policies the way the Global Policies page reads them.
_SEED_PROBE = """
import json, os, sys
sys.path.insert(0, {upstream!r})
from ae_omni_policies import docker_entrypoint
docker_entrypoint.install()
import deploy.docker.entrypoint as ep
from omnigent.stores.policy_store.sqlalchemy_store import SqlAlchemyPolicyStore
ep.run_migrations(os.environ["DATABASE_URL"])
ep.build_app()
store = SqlAlchemyPolicyStore(os.environ["DATABASE_URL"])
print(json.dumps([[p.id, p.name] for p in store.list_defaults()]))
"""


# One container boot that seeds a slot cap with a limit: the slot counts' source
# (slot_limit.effective_limit) must answer the seeded limit, not AE_SLOT_LIMIT.
_SLOT_PROBE = """
import json, os, sys
sys.path.insert(0, {upstream!r})
from ae_omni_policies import docker_entrypoint
docker_entrypoint.install()
import deploy.docker.entrypoint as ep
ep.run_migrations(os.environ["DATABASE_URL"])
ep.build_app()
from ae_omni_policies import labels, slot_limit
print(json.dumps([labels.SLOT_LIMIT, slot_limit.effective_limit()]))
"""


# The middleware classes on the container app, by name.
_MIDDLEWARE_PROBE = """
import json, sys
sys.path.insert(0, {upstream!r})
from ae_omni_policies import docker_entrypoint
docker_entrypoint.install()
import deploy.docker.entrypoint as ep
built = ep.build_app()
print(json.dumps([m.cls.__name__ for m in built.app.user_middleware]))
"""


def _run_probe(
    tmp_path: Path, probe: str, config: str, extra_env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    data = tmp_path / "data"
    data.mkdir(exist_ok=True)
    (data / "config.yaml").write_text(config)
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("OMNIGENT_", "AE_")) and key != "DATABASE_URL"
    }
    env.update(
        DATABASE_URL=f"sqlite:///{tmp_path / 'db.sqlite'}",
        OMNIGENT_CONFIG=str(data / "config.yaml"),
        OMNIGENT_DATA_DIR=str(data),
        ARTIFACT_DIR=str(tmp_path / "artifacts"),
        OMNIGENT_AUTH_ENABLED="0",
    )
    env.update(extra_env or {})
    return subprocess.run(
        [sys.executable, "-c", probe],
        cwd=UPSTREAM,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=240,
    )


def _ae_routes(tmp_path: Path, *, wrap: bool) -> list[str]:
    ran = _run_probe(
        tmp_path,
        _PROBE.format(upstream=str(UPSTREAM), wrap=wrap),
        "policy_modules: [ae_omni_policies]\ndebug_router_modules: [ae_omni_policies.router]\n",
    )
    assert ran.returncode == 0, ran.stderr[-4000:]
    return json.loads(ran.stdout.strip().splitlines()[-1])


def test_wrapped_container_app_mounts_the_ae_routes(tmp_path: Path) -> None:
    routes = _ae_routes(tmp_path, wrap=True)
    assert "/v1/ae/health" in routes
    assert "/v1/ae/capacity" in routes
    assert "/v1/ae/lot" in routes


# A server in OIDC mode with explicit endpoints, so the probe makes no discovery call.
_OIDC_ENV = {
    "OMNIGENT_AUTH_PROVIDER": "oidc",
    "OMNIGENT_OIDC_ISSUER": "https://login.example.test/v2.0",
    "OMNIGENT_OIDC_CLIENT_ID": "client",
    "OMNIGENT_OIDC_CLIENT_SECRET": "probe-only",
    "OMNIGENT_OIDC_REDIRECT_URI": "https://omnigent.example.test/auth/callback",
    "OMNIGENT_OIDC_AUTHORIZATION_ENDPOINT": "https://login.example.test/authorize",
    "OMNIGENT_OIDC_TOKEN_ENDPOINT": "https://login.example.test/token",
    "OMNIGENT_OIDC_JWKS_URI": "https://login.example.test/keys",
    "OMNIGENT_OIDC_COOKIE_SECRET": "ab" * 32,
}


@pytest.mark.parametrize(
    ("extra_env", "added"),
    [
        (_OIDC_ENV, True),
        # pull-secrets.sh writes the secret whatever the mode; header mode must not install it.
        ({"OMNIGENT_OIDC_COOKIE_SECRET": "ab" * 32}, False),
    ],
)
def test_the_container_adds_the_session_cookie_expiry_only_under_oidc(
    tmp_path: Path, extra_env: dict[str, str], added: bool
) -> None:
    ran = _run_probe(
        tmp_path,
        _MIDDLEWARE_PROBE.format(upstream=str(UPSTREAM)),
        "policy_modules: [ae_omni_policies]\n",
        extra_env,
    )
    assert ran.returncode == 0, ran.stderr[-4000:]
    names = json.loads(ran.stdout.strip().splitlines()[-1])
    assert ("SessionCookieExpiry" in names) is added


def test_the_container_seeds_global_policies_once(tmp_path: Path) -> None:
    """Two boots against one database: the first seeds, the second leaves the rows alone."""
    config = (
        "policy_modules: [ae_omni_policies]\n"
        "ae_seed_policies:\n"
        "  ae_hurry_modes: {type: function, handler: ae_omni_policies.hurry.hurry_modes}\n"
        "  ae_rabbit_hole:\n"
        "    type: function\n"
        "    handler: ae_omni_policies.rabbit_hole.rabbit_hole\n"
        "    factory_params: {hours: 4, turns: 60}\n"
    )
    boots = []
    for _ in range(2):
        ran = _run_probe(tmp_path, _SEED_PROBE.format(upstream=str(UPSTREAM)), config)
        assert ran.returncode == 0, ran.stderr[-4000:]
        boots.append(json.loads(ran.stdout.strip().splitlines()[-1]))
    assert [name for _, name in boots[0]] == ["ae_hurry_modes", "ae_rabbit_hole"]
    assert boots[1] == boots[0]


def test_a_seed_the_factory_rejects_stops_the_container(tmp_path: Path) -> None:
    """Exiting lets the auto-update roll back instead of booting without the policies."""
    config = (
        "policy_modules: [ae_omni_policies]\n"
        "ae_seed_policies:\n"
        "  ae_slot_cap:\n"
        "    type: function\n"
        "    handler: ae_omni_policies.slot_cap.slot_cap\n"
        "    factory_params: {mode: nope}\n"
    )
    ran = _run_probe(tmp_path, _SEED_PROBE.format(upstream=str(UPSTREAM)), config)
    assert ran.returncode != 0
    assert "does not build with these params" in ran.stderr


def test_the_container_hands_the_slot_counts_the_policy_store(tmp_path: Path) -> None:
    """A limit set on the slot cap (here by the seed) reaches every count (2026-10-22)."""
    config = (
        "policy_modules: [ae_omni_policies]\n"
        "ae_seed_policies:\n"
        "  ae_slot_cap:\n"
        "    type: function\n"
        "    handler: ae_omni_policies.slot_cap.slot_cap\n"
        "    factory_params: {mode: warn, limit: 6}\n"
    )
    ran = _run_probe(tmp_path, _SLOT_PROBE.format(upstream=str(UPSTREAM)), config)
    assert ran.returncode == 0, ran.stderr[-4000:]
    env_limit, counted = json.loads(ran.stdout.strip().splitlines()[-1])
    assert (env_limit, counted) == (3, 6)


def test_config_kwargs_reads_both_keys_and_skips_absent_ones() -> None:
    from ae_omni_policies.docker_entrypoint import config_kwargs

    assert config_kwargs(
        {"policy_modules": ["ae_omni_policies"], "debug_router_modules": "ae_omni_policies.router"}
    ) == {
        "policy_modules": ["ae_omni_policies"],
        "debug_router_modules": ["ae_omni_policies.router"],
    }
    assert config_kwargs({"admins": ["owner"]}) == {}


def test_explicit_kwargs_win_over_the_config(monkeypatch, tmp_path: Path) -> None:
    import omnigent.server.app as app_module
    from ae_omni_policies import docker_entrypoint

    seen: list[dict] = []

    def fake_create_app(**kwargs):
        seen.append(kwargs)
        return "app"

    config = tmp_path / "config.yaml"
    config.write_text(
        "policy_modules: [from_config]\ndebug_router_modules: [routes_from_config]\n"
    )
    monkeypatch.setenv("OMNIGENT_CONFIG", str(config))
    monkeypatch.setattr(app_module, "create_app", fake_create_app)
    docker_entrypoint.install()
    assert app_module.create_app(policy_modules=["explicit"]) == "app"
    assert seen == [
        {"policy_modules": ["explicit"], "debug_router_modules": ["routes_from_config"]}
    ]


def test_image_cmd_runs_the_wrapper() -> None:
    dockerfile = (ROOT / "deploy" / "Dockerfile").read_text()
    assert 'CMD ["python", "-m", "ae_omni_policies.docker_entrypoint"]' in dockerfile


def test_the_orchestrator_is_registered_even_when_the_box_line_omits_it(tmp_path: Path) -> None:
    """The box's config/server.env names steward and shutdown only; a release adds the rest."""
    from ae_omni_policies import docker_entrypoint

    orchestrator = tmp_path / "agents" / "orchestrator"
    orchestrator.mkdir(parents=True)
    extra = (str(orchestrator), str(tmp_path / "agents" / "not-in-this-image"))
    box = "/opt/ae/agents/steward:/opt/ae/agents/shutdown"
    env = {"OMNIGENT_BUILTIN_AGENT_DIRS": box}
    assert docker_entrypoint.ensure_agent_dirs(env, extra) == f"{box}:{orchestrator}"
    assert env["OMNIGENT_BUILTIN_AGENT_DIRS"] == f"{box}:{orchestrator}"
    # Listed already, a second pass, or switched off: the line is left alone.
    assert docker_entrypoint.ensure_agent_dirs(env, extra) is None
    off = {"OMNIGENT_BUILTIN_AGENT_DIRS": box, "AE_EXTRA_AGENTS": "off"}
    assert docker_entrypoint.ensure_agent_dirs(off, extra) is None
    assert off["OMNIGENT_BUILTIN_AGENT_DIRS"] == box
    unset: dict[str, str] = {}
    assert docker_entrypoint.ensure_agent_dirs(unset, extra) == str(orchestrator)
    assert docker_entrypoint.EXTRA_AGENT_DIRS == (
        "/opt/ae/agents/orchestrator",
        "/opt/ae/agents/preset-reviewer",
    )


_GRAPH_SPEC = """name: {name}
tools:
  ae:
    type: mcp
    command: python
  box_memory:
    type: mcp
    # filled from AE_GRAPH_MCP_URL
    url: ${{AE_GRAPH_MCP_URL}}
prompt: hello
"""


def _agent(root: Path, name: str, text: str) -> Path:
    agent = root / "agents" / name
    agent.mkdir(parents=True)
    (agent / "agent.yaml").write_text(text, encoding="utf-8")
    return agent


def test_the_graph_url_is_filled_in_before_the_agents_register(tmp_path: Path) -> None:
    """Set: the copy carries the URL, keeps its directory name and every other line."""
    from ae_omni_policies import docker_entrypoint

    steward = _agent(tmp_path, "steward", _GRAPH_SPEC.format(name="steward"))
    plain = _agent(tmp_path, "orchestrator", "name: orchestrator\nprompt: hi\n")
    box = f"{steward}: {plain}"  # upstream strips each entry, so this does too
    env = {"OMNIGENT_BUILTIN_AGENT_DIRS": box, "AE_GRAPH_MCP_URL": " http://graph:8000/mcp "}
    out = tmp_path / "rendered"
    out.mkdir()
    value = docker_entrypoint.render_agent_dirs(env, out)
    assert value == env["OMNIGENT_BUILTIN_AGENT_DIRS"]
    rendered, kept = value.split(":")
    assert kept == str(plain), "a spec without the placeholder is registered from the image"
    assert Path(rendered).name == "steward" and Path(rendered).parent.parent == out
    text = (Path(rendered) / "agent.yaml").read_text()
    assert text == _GRAPH_SPEC.format(name="steward").replace(
        "${AE_GRAPH_MCP_URL}", '"http://graph:8000/mcp"'
    )
    assert "${AE_GRAPH_MCP_URL}" in (steward / "agent.yaml").read_text(), "the image is untouched"


def test_without_the_graph_url_the_server_is_dropped_and_logged(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Unset or empty: the agent still registers, without box_memory, and the log says why."""
    import yaml
    from ae_omni_policies import docker_entrypoint

    for i, env_extra in enumerate(({}, {"AE_GRAPH_MCP_URL": "  "})):
        steward = _agent(tmp_path / str(i), "steward", _GRAPH_SPEC.format(name="steward"))
        single = tmp_path / str(i) / "solo.yaml"
        single.write_text(_GRAPH_SPEC.format(name="solo"), encoding="utf-8")
        env = {"OMNIGENT_BUILTIN_AGENT_DIRS": f"{steward}:{single}", **env_extra}
        out = tmp_path / str(i) / "rendered"
        out.mkdir()
        directory, solo = docker_entrypoint.render_agent_dirs(env, out).split(":")
        assert Path(solo).name == "solo.yaml"
        for path in (Path(directory) / "agent.yaml", Path(solo)):
            text = path.read_text()
            assert "AE_GRAPH_MCP_URL" not in text
            data = yaml.safe_load(text)
            assert list(data["tools"]) == ["ae"]
            assert data["prompt"] == "hello"
        err = capsys.readouterr().err
        assert err.count("\n") == 1, "one line for the whole set"
        assert "AE_GRAPH_MCP_URL is unset" in err and "steward, solo.yaml" in err


def test_specs_without_the_placeholder_are_left_alone(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """No token, a missing path, or no line at all: nothing is copied, nothing is logged."""
    from ae_omni_policies import docker_entrypoint

    plain = _agent(tmp_path, "orchestrator", "name: orchestrator\nprompt: hi\n")
    box = f"{plain}:{tmp_path / 'not-in-this-image'}"
    env = {"OMNIGENT_BUILTIN_AGENT_DIRS": box}
    out = tmp_path / "rendered"
    out.mkdir()
    assert docker_entrypoint.render_agent_dirs(env, out) is None
    assert env["OMNIGENT_BUILTIN_AGENT_DIRS"] == box
    assert docker_entrypoint.render_agent_dirs({}, out) is None
    assert not any(out.iterdir())
    assert capsys.readouterr().err == ""
    # With no root given, no temporary directory is made when nothing needs one.
    before = set(Path(tempfile.gettempdir()).glob("ae-agents-*"))
    assert docker_entrypoint.render_agent_dirs(dict(env)) is None
    assert set(Path(tempfile.gettempdir()).glob("ae-agents-*")) == before


def test_a_url_with_yaml_characters_stays_one_string(tmp_path: Path) -> None:
    """The value is quoted, so a '#' or ': ' in it cannot truncate or break the spec."""
    import yaml
    from ae_omni_policies import docker_entrypoint

    steward = _agent(tmp_path, "steward", _GRAPH_SPEC.format(name="steward"))
    url = "http://graph:8000/mcp #x: y"
    env = {"OMNIGENT_BUILTIN_AGENT_DIRS": str(steward), "AE_GRAPH_MCP_URL": url}
    out = tmp_path / "rendered"
    out.mkdir()
    rendered = Path(docker_entrypoint.render_agent_dirs(env, out))
    data = yaml.safe_load((rendered / "agent.yaml").read_text())
    assert data["tools"]["box_memory"]["url"] == url


def test_a_spec_that_cannot_be_rendered_is_skipped_not_fatal(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Unset: a token the drop cannot remove, or YAML that does not parse, keeps that
    agent off the line (never registered with the token) and the server starting."""
    from ae_omni_policies import docker_entrypoint

    odd = _agent(tmp_path, "odd", "name: odd\nprompt: see ${AE_GRAPH_MCP_URL}\n")
    broken = _agent(tmp_path, "broken", "name: [broken\nurl: ${AE_GRAPH_MCP_URL}\n")
    plain = _agent(tmp_path, "orchestrator", "name: orchestrator\nprompt: hi\n")
    env = {"OMNIGENT_BUILTIN_AGENT_DIRS": f"{odd}:{broken}:{plain}"}
    out = tmp_path / "rendered"
    out.mkdir()
    assert docker_entrypoint.render_agent_dirs(env, out) == str(plain)
    err = capsys.readouterr().err
    assert f"{odd} (" in err and f"{broken} (" in err and "skipping it" in err


def test_sidebar_grouping_is_turned_on_without_touching_the_box_line() -> None:
    """config/server.env is the owner's file and names harness_install only; the image adds the rest."""
    from ae_omni_policies import docker_entrypoint

    known = ("harness_install", "sidebar_grouping_ui")
    unset: dict[str, str] = {}
    assert docker_entrypoint.ensure_features(unset, known=known) == "sidebar_grouping_ui"
    assert unset["OMNIGENT_FEATURES"] == "sidebar_grouping_ui"

    env = {"OMNIGENT_FEATURES": "harness_install"}
    assert (
        docker_entrypoint.ensure_features(env, known=known)
        == "harness_install,sidebar_grouping_ui"
    )
    assert env["OMNIGENT_FEATURES"] == "harness_install,sidebar_grouping_ui"
    # Listed already (spacing and a second pass included): the line is left alone.
    assert docker_entrypoint.ensure_features(env, known=known) is None
    spaced = {"OMNIGENT_FEATURES": " sidebar_grouping_ui , harness_install"}
    assert docker_entrypoint.ensure_features(spaced, known=known) is None
    assert spaced["OMNIGENT_FEATURES"] == " sidebar_grouping_ui , harness_install"
    assert docker_entrypoint.EXTRA_FEATURES == ("sidebar_grouping_ui",)


def test_the_features_off_switch_and_unknown_names() -> None:
    from ae_omni_policies import docker_entrypoint

    off = {"OMNIGENT_FEATURES": "harness_install", "AE_EXTRA_FEATURES": "  OFF "}
    assert docker_entrypoint.ensure_features(off, known=("sidebar_grouping_ui",)) is None
    assert off["OMNIGENT_FEATURES"] == "harness_install"
    # A name the installed omnigent does not know would fail its startup: never appended.
    env = {"OMNIGENT_FEATURES": "harness_install"}
    assert docker_entrypoint.ensure_features(env, known=("harness_install",)) is None
    assert env["OMNIGENT_FEATURES"] == "harness_install"
    assert docker_entrypoint.ensure_features(env, extra=("not_a_flag",), known=()) is None


def test_features_are_skipped_when_omnigent_has_no_flag_module(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ae_omni_policies import docker_entrypoint

    monkeypatch.setitem(sys.modules, "omnigent.server.feature_flags", None)
    env = {"OMNIGENT_FEATURES": "harness_install"}
    assert docker_entrypoint.ensure_features(env) is None
    assert env["OMNIGENT_FEATURES"] == "harness_install"


def test_features_default_to_what_the_installed_omnigent_knows() -> None:
    from ae_omni_policies import docker_entrypoint

    env: dict[str, str] = {}
    value = docker_entrypoint.ensure_features(env)
    # Pinned v0.18.0 knows the flag; once upstream retires it, this returns None instead.
    assert value in (None, "sidebar_grouping_ui")
