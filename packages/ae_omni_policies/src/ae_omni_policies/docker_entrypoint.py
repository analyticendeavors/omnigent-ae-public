"""Container entrypoint: upstream's Docker entrypoint plus the AE config keys.

Upstream's ``deploy/docker/entrypoint.py`` (``build_app``, v0.13.0 and still
at v0.18.0) builds the app from the server config but passes only ``admins``
and ``allowed_domains`` to ``create_app``; ``policy_modules`` and
``debug_router_modules`` are read by ``omnigent server`` (``omnigent/cli.py``)
and ignored in the container. The first box deploy on 2026-09-24 ran without
the AE layer because of it: every ``/v1/ae`` route answered 404, and the slot
cap and observer never loaded.

This module wraps ``omnigent.server.app.create_app`` so both keys come from the
same config file the entrypoint already loads, then runs the upstream script
unchanged. A value the caller passes explicitly wins, so the wrapper turns into
a no-op the day upstream passes the keys itself. No file under ``upstream/``
changes.

The wrapper also seeds the Global Policies page (2026-10-18): upstream's
``main()`` has run migrations by the time it calls ``create_app`` with the
policy store, so :func:`ae_omni_policies.policy_seed.seed_policies` inserts
``ae_seed_policies`` there when the store holds none of those policies yet.

Since 2026-10-20 it also adds :mod:`ae_omni_policies.session_cookie` to the app
when the server signs in with OIDC, so the iOS app keeps its sign-in across
launches.

Since 2026-10-21 it fills the Graphiti MCP URL into the shipped agent specs
before upstream registers them (:func:`render_agent_dirs`). Upstream's
single-file loader never expands ``${VAR}``, and the runner loads the
registered bundle as stored, so the spec it registers must carry the literal
URL; with ``AE_GRAPH_MCP_URL`` unset the agent registers without that server.

Run as ``python -m ae_omni_policies.docker_entrypoint`` (the image's CMD).
"""

from __future__ import annotations

import json
import os
import runpy
import shutil
import sys
import tempfile
from collections.abc import Iterable, MutableMapping
from functools import wraps
from pathlib import Path
from typing import Any

UPSTREAM_ENTRYPOINT = os.environ.get("AE_UPSTREAM_ENTRYPOINT", "/app/entrypoint.py")
# Agents every release registers even when config/server.env on the box names
# only the older ones (2026-09-25, the orchestrator). That file is Reid's, and
# the auto-update never rewrites its agent line, so a new agent listed only in
# config/server.env.example would reach the box by hand at best. Appended to
# OMNIGENT_BUILTIN_AGENT_DIRS here when the directory is in the image and not
# listed yet; AE_EXTRA_AGENTS=off leaves the line exactly as configured.
BUILTIN_AGENTS_ENV = "OMNIGENT_BUILTIN_AGENT_DIRS"
EXTRA_AGENTS_ENV = "AE_EXTRA_AGENTS"
EXTRA_AGENT_DIRS = ("/opt/ae/agents/orchestrator",)
# Release features every build turns on even though config/server.env on the box
# lists only the older ones (2026-10-19, sidebar grouping and manual order). That
# file is Reid's unmanaged one and the auto-update rewrites only the image tag,
# so a flag set there would reach the box by hand at best. Appended to
# OMNIGENT_FEATURES here when the installed omnigent knows the name: upstream
# raises on an unknown name at startup and plans to retire this flag around
# 0.19, so an unknown name is skipped, never appended. AE_EXTRA_FEATURES=off
# leaves the line exactly as configured.
FEATURES_ENV = "OMNIGENT_FEATURES"
EXTRA_FEATURES_ENV = "AE_EXTRA_FEATURES"
EXTRA_FEATURES = ("sidebar_grouping_ui",)
_CONFIG_KEYS = ("policy_modules", "debug_router_modules")
# The Graphiti MCP URL (2026-10-21). agents/steward and agents/shutdown ship the
# placeholder below as their box_memory url; render_agent_dirs fills it from
# this variable, or drops the server when the variable is unset or empty, before
# upstream registers the specs. On the box the value is http://ae-llm-box:8000/mcp.
GRAPH_MCP_ENV = "AE_GRAPH_MCP_URL"
GRAPH_MCP_TOKEN = "${" + GRAPH_MCP_ENV + "}"


def config_kwargs(cfg: dict[str, Any]) -> dict[str, list[str]]:
    """Return the AE ``create_app`` keyword arguments present in ``cfg``.

    :param cfg: The parsed server config.
    :returns: ``policy_modules`` and ``debug_router_modules`` as string
        lists, each only when the config sets it.
    """
    from omnigent.server.server_config import config_str_list

    return {key: config_str_list(cfg[key]) for key in _CONFIG_KEYS if cfg.get(key)}


def ensure_agent_dirs(
    environ: MutableMapping[str, str], extra: tuple[str, ...] = EXTRA_AGENT_DIRS
) -> str | None:
    """Append each of *extra* that exists to :data:`BUILTIN_AGENTS_ENV` in *environ*.

    :param environ: The environment to read and update (``os.environ`` in the image).
    :param extra: Agent directories to make sure are listed.
    :returns: The new value when it changed, else ``None``.
    """
    if (environ.get(EXTRA_AGENTS_ENV) or "").strip().lower() == "off":
        return None
    current = [part for part in (environ.get(BUILTIN_AGENTS_ENV) or "").split(os.pathsep) if part]
    listed = {os.path.normpath(part) for part in current}
    added = [
        path for path in extra if os.path.isdir(path) and os.path.normpath(path) not in listed
    ]
    if not added:
        return None
    value = os.pathsep.join([*current, *added])
    environ[BUILTIN_AGENTS_ENV] = value
    return value


def ensure_features(
    environ: MutableMapping[str, str],
    extra: tuple[str, ...] = EXTRA_FEATURES,
    known: Iterable[str] | None = None,
) -> str | None:
    """Append each of *extra* that omnigent knows to :data:`FEATURES_ENV` in *environ*.

    :param environ: The environment to read and update (``os.environ`` in the image).
    :param extra: Feature names to make sure are listed.
    :param known: The names the installed omnigent accepts; defaults to the values
        of ``omnigent.server.feature_flags.Feature``. When that module cannot be
        imported nothing is appended.
    :returns: The new value when it changed, else ``None``.
    """
    if (environ.get(EXTRA_FEATURES_ENV) or "").strip().lower() == "off":
        return None
    if known is None:
        try:
            from omnigent.server.feature_flags import Feature
        except ImportError:
            return None
        known = [feature.value for feature in Feature]
    known_names = set(known)
    current = [name.strip() for name in (environ.get(FEATURES_ENV) or "").split(",")]
    current = [name for name in current if name]
    added = [name for name in extra if name in known_names and name not in current]
    if not added:
        return None
    value = ",".join([*current, *added])
    environ[FEATURES_ENV] = value
    return value


def _spec_path(entry: Path) -> Path:
    """The YAML file behind one agent entry: the file itself, or ``agent.yaml`` in it."""
    return entry if entry.is_file() else entry / "agent.yaml"


def _drop_token_servers(text: str) -> str:
    """Return the spec without the ``tools`` entries whose url is the placeholder.

    :param text: The agent.yaml text.
    :returns: The spec dumped back to YAML (comments are not kept).
    """
    import yaml

    data = yaml.safe_load(text)
    tools = data.get("tools") if isinstance(data, dict) else None
    if isinstance(tools, dict):
        for name in [n for n, tool in tools.items() if isinstance(tool, dict)]:
            if GRAPH_MCP_TOKEN in str(tools[name].get("url", "")):
                del tools[name]
    return yaml.safe_dump(data, sort_keys=False, allow_unicode=True)


def render_agent_dirs(environ: MutableMapping[str, str], root: Path | None = None) -> str | None:
    """Fill :data:`GRAPH_MCP_ENV` into the listed agent specs that carry its placeholder.

    Each :data:`BUILTIN_AGENTS_ENV` entry whose spec holds :data:`GRAPH_MCP_TOKEN`
    is copied under *root* (same directory or file name, so the agent keeps its
    name) with the placeholder replaced by the variable's value, quoted, and the
    entry points at the copy. With the variable unset or empty the copy leaves out
    the MCP servers whose url is the placeholder, and one line on stderr says so.
    An entry that cannot be rendered, or whose copy would still hold the
    placeholder, is logged and left off the line, so it never registers with the
    token and never stops the server (upstream skips a bad entry the same way).

    :param environ: The environment to read and update (``os.environ`` in the image).
    :param root: An empty directory for the rendered copies; a new temporary
        directory, made only when a spec needs rendering, when omitted.
    :returns: The new :data:`BUILTIN_AGENTS_ENV` value when it changed, else ``None``.
    """
    import yaml

    url = (environ.get(GRAPH_MCP_ENV) or "").strip()
    entries = [part.strip() for part in (environ.get(BUILTIN_AGENTS_ENV) or "").split(os.pathsep)]
    entries = [part for part in entries if part]
    rendered: list[str] = []
    dropped: list[str] = []
    changed = False
    for index, entry in enumerate(entries):
        source = Path(entry)
        try:
            text = _spec_path(source).read_text(encoding="utf-8")
        except OSError:
            rendered.append(entry)  # upstream logs a bad path and skips it
            continue
        if GRAPH_MCP_TOKEN not in text:
            rendered.append(entry)
            continue
        changed = True
        try:
            # json.dumps quotes the value as a YAML double-quoted scalar.
            text = (
                text.replace(GRAPH_MCP_TOKEN, json.dumps(url))
                if url
                else _drop_token_servers(text)
            )
            if GRAPH_MCP_TOKEN in text:
                raise ValueError(f"{GRAPH_MCP_TOKEN} appears outside an MCP url")
            if root is None:
                root = Path(tempfile.mkdtemp(prefix="ae-agents-"))
            target = root / str(index) / source.name
            if source.is_dir():
                shutil.copytree(source, target)
                spec = target / "agent.yaml"
            else:
                target.parent.mkdir(parents=True)
                spec = target
            spec.write_text(text, encoding="utf-8")
        except (OSError, ValueError, yaml.YAMLError) as exc:  # never stop the server
            print(
                f"ae: cannot fill {GRAPH_MCP_ENV} into {entry} ({exc}); skipping it, so "
                "this start registers no new copy of that agent.",
                file=sys.stderr,
                flush=True,
            )
            continue
        rendered.append(str(target))
        if not url:
            dropped.append(source.name)
    if dropped:
        print(
            f"ae: {GRAPH_MCP_ENV} is unset, so the Graphiti memory MCP (box_memory) is "
            f"off for {', '.join(dropped)}; set it in config/server.env to turn it on.",
            file=sys.stderr,
            flush=True,
        )
    if not changed:
        return None
    value = os.pathsep.join(rendered)
    environ[BUILTIN_AGENTS_ENV] = value
    return value


def install() -> None:
    """Wrap ``create_app`` so it receives the AE keys from the server config.

    The wrapper also seeds the policy store from ``ae_seed_policies`` when
    the caller passes one. Idempotent: a second call leaves the first wrapper
    in place.
    """
    import omnigent.server.app as app_module
    from omnigent.server.server_config import load_server_config

    original = app_module.create_app
    if getattr(original, "_ae_wrapped", False):
        return

    @wraps(original)
    def create_app(*args: Any, **kwargs: Any) -> Any:
        # The dict upstream's build_app built its own settings from, when it passes one.
        cfg = kwargs.get("server_config")
        if cfg is None:
            cfg = load_server_config()
        for key, value in config_kwargs(cfg).items():
            if kwargs.get(key) is None:
                kwargs[key] = value
        if kwargs.get("policy_store") is not None:
            from ae_omni_policies.policy_seed import seed_policies

            seed_policies(kwargs["policy_store"], cfg, kwargs.get("policy_modules"))
        app = original(*args, **kwargs)
        from ae_omni_policies.session_cookie import install as install_cookie_expiry

        install_cookie_expiry(app, os.environ)
        return app

    create_app._ae_wrapped = True  # type: ignore[attr-defined]
    app_module.create_app = create_app


def main() -> None:
    """Install the wrapper, then run upstream's entrypoint as ``__main__``."""
    if not os.path.isfile(UPSTREAM_ENTRYPOINT):
        raise SystemExit(
            f"upstream entrypoint not found at {UPSTREAM_ENTRYPOINT}. "
            "This module runs inside the omnigent-ae image; set "
            "AE_UPSTREAM_ENTRYPOINT to its path elsewhere."
        )
    ensure_agent_dirs(os.environ)
    render_agent_dirs(os.environ)
    ensure_features(os.environ)
    install()
    runpy.run_path(UPSTREAM_ENTRYPOINT, run_name="__main__")


if __name__ == "__main__":
    main()
