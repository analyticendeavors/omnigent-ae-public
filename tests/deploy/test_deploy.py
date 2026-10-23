"""What a machine with no Docker daemon and no systemd can prove about deploy/.

Run by ``just test`` from ``upstream/`` on upstream's venv, like the package
suites. Each test names the file it holds still and the decision behind it
(Phase 6, 2026-09-23; the decisions are in deploy/README.md and the PR that
added them). Tools that may be absent here (shellcheck, actionlint, the
compose CLI) make their test skip with a warning, never pass silently; CI
installs all three. The shellcheck test also covers scripts/*.sh (2026-09-24).
"""

from __future__ import annotations

import configparser
import grp
import os
import pwd
import re
import shutil
import subprocess
import warnings
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
DEPLOY = ROOT / "deploy"
CONFIG = ROOT / "config"
UPSTREAM_COMPOSE = ROOT / "upstream" / "deploy" / "docker" / "docker-compose.yaml"
OVERRIDE = DEPLOY / "docker-compose.override.yml"
WORKFLOWS = ROOT / ".github" / "workflows"
IMAGE_WORKFLOW = WORKFLOWS / "image.yml"

SCRIPTS = sorted(DEPLOY.glob("*.sh"))
# scripts/*.sh (the patch-series scripts) joined the shellcheck run on
# 2026-09-24, once their old findings were fixed; the other script checks
# stay on deploy/, whose conventions they describe.
SHELLCHECKED = SCRIPTS + sorted((ROOT / "scripts").glob("*.sh"))
UNITS = sorted((DEPLOY / "systemd").glob("ae-omnigent*"))

# The one install path the two system units carry (systemd has no home specifier
# for system units); the user units say %h instead. deploy/README.md, step 6.
INSTALL_ROOT = "/home/reid/deploy/omnigent-ae"
# The overlay's env_file follows the checkout: deploy/compose.sh exports these.
SERVER_SETTINGS_REF = (
    "${AE_SERVER_SETTINGS:?run through deploy/compose.sh, which sets AE_SERVER_SETTINGS}"
)
SERVER_SECRETS_REF = (
    "${AE_SERVER_SECRETS:?run through deploy/compose.sh, which sets AE_SERVER_SECRETS}"
)
HOST_SECRETS_PATH = "/secure/env/omnigent-host.env"
IMAGE = "ghcr.io/analyticendeavors/omnigent-ae"


# ── helpers ────────────────────────────────────────────────────────────────


class _ComposeLoader(yaml.SafeLoader):
    """SafeLoader that accepts compose's merge tags (`!reset`, `!override`) as plain values."""


for _tag in ("!reset", "!override"):
    _ComposeLoader.add_constructor(
        _tag,
        lambda loader, node: (
            loader.construct_sequence(node)
            if isinstance(node, yaml.SequenceNode)
            else loader.construct_mapping(node)
            if isinstance(node, yaml.MappingNode)
            else (None if node.value in ("null", "~", "") else loader.construct_scalar(node))
        ),
    )


def _override() -> dict[str, Any]:
    return yaml.load(OVERRIDE.read_text(), Loader=_ComposeLoader)


def _unit(path: Path) -> configparser.RawConfigParser:
    """Parse a systemd unit. Repeated keys (After=, EnvironmentFile=) keep the last value here;
    ``_unit_values`` reads all of them."""
    parser = configparser.RawConfigParser(strict=False, interpolation=None)
    parser.optionxform = str  # type: ignore[assignment,method-assign]
    parser.read_string(path.read_text())
    return parser


def _unit_values(path: Path, section: str, key: str) -> list[str]:
    values: list[str] = []
    current = None
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if line.startswith("[") and line.endswith("]"):
            current = line[1:-1]
        elif current == section and "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            if k.strip() == key:
                values.append(v.strip())
    return values


def _tool(name: str) -> str | None:
    return shutil.which(name)


def _skip_missing(name: str, install_hint: str) -> None:
    message = (
        f"{name} is not installed here; {install_hint}. The check ran in CI, not on this machine."
    )
    warnings.warn(message, stacklevel=2)
    pytest.skip(message)


# ── the compose override ───────────────────────────────────────────────────


def test_override_pins_the_published_image_by_tag() -> None:
    svc = _override()["services"]["omnigent"]
    name, tag = svc["image"].split("}:", 1)
    assert name.startswith("${AE_IMAGE:?"), "the image name comes from config/server.env"
    assert tag.startswith("${OMNIGENT_IMAGE_TAG:?"), (
        "the tag must be required, never default to latest"
    )
    assert svc["build"] is None, (
        "build is reset so the box cannot rebuild upstream's unpatched image"
    )


def test_override_publishes_loopback_8010_only() -> None:
    ports = _override()["services"]["omnigent"]["ports"]
    assert ports == ["127.0.0.1:${OMNIGENT_PORT:-8010}:8000"]
    raw = OVERRIDE.read_text()
    assert "ports: !override" in raw, (
        "without !override compose appends upstream's all-interfaces publish"
    )


def test_override_reads_settings_then_secrets() -> None:
    env_file = _override()["services"]["omnigent"]["env_file"]
    assert env_file == [SERVER_SETTINGS_REF, SERVER_SECRETS_REF]


def test_override_binds_data_and_postgres_under_the_data_dir() -> None:
    # AE_DATA_DIR moves both (2026-10-22, README.md "Quick start"); unset, the
    # box's /secure/omnigent as before.
    services = _override()["services"]
    assert services["omnigent"]["volumes"] == ["${AE_DATA_DIR:-/secure/omnigent}/data:/data"]
    assert services["postgres"]["volumes"] == [
        "${AE_DATA_DIR:-/secure/omnigent}/postgres:/var/lib/postgresql/data"
    ]
    assert services["omnigent"]["extra_hosts"] == ["host.docker.internal:host-gateway"]


def test_override_touches_no_other_service_keys() -> None:
    """The overlay must stay an overlay: upstream's file is never edited, so
    anything upstream owns (depends_on, healthcheck, restart, environment)
    must come from upstream."""
    services = _override()["services"]
    assert set(services) == {"omnigent", "postgres"}
    assert set(services["postgres"]) == {"volumes"}
    assert set(services["omnigent"]) == {
        "image",
        "build",
        "env_file",
        "extra_hosts",
        "ports",
        "volumes",
    }


def test_override_names_no_host_network() -> None:
    """Per-user host networks are one machine's list (2026-10-22): they live in
    the gitignored config/compose.local.yml, never in the shipped overlay."""
    override = _override()
    assert "networks" not in override
    assert "networks" not in override["services"]["omnigent"]
    assert "config/compose.local.yml" in (ROOT / ".gitignore").read_text()


def test_local_compose_example_gives_each_host_container_its_own_internal_network() -> None:
    """The server joins one internal network per containerised user host
    (2026-10-20); it keeps the default network (Postgres and the way out), and
    Postgres joins none of them."""
    override = yaml.safe_load((CONFIG / "compose.local.yml.example").read_text())
    server_networks = override["services"]["omnigent"]["networks"]
    assert "default" in server_networks
    host_networks = {name for name in server_networks if name != "default"}
    assert host_networks, "no host-<user> network is declared"
    assert "postgres" not in override["services"]
    for name in host_networks:
        assert name.startswith("host-"), name
        network = override["networks"][name]
        assert network["name"] == f"omnigent-{name}"
        assert network["internal"] is True, f"{name} must be internal (no route off the box)"
        assert network["driver_opts"]["com.docker.network.bridge.inhibit_ipv4"] == "true", (
            f"{name} must have no gateway address on the box"
        )


HOST_CONTAINER = DEPLOY / "host-container"


def test_host_container_is_walled_off() -> None:
    """A second user's host (2026-10-20): nothing from the box mounted, no route
    but the internal network to the server and the egress proxy, a non-root
    user, and the proxy handed on to the runners."""
    compose = yaml.safe_load((HOST_CONTAINER / "docker-compose.yml").read_text())
    assert set(compose["services"]) == {"host", "egress"}
    host = compose["services"]["host"]
    assert host["volumes"] == ["home:/home/agent"], "only the named home volume is mounted"
    for key in ("privileged", "cap_add", "devices", "network_mode", "pid", "ipc", "user"):
        assert key not in host, f"host must not set {key}"
    assert "no-new-privileges:true" in host["security_opt"]
    assert host["init"] is True and host["restart"] == "unless-stopped"
    assert set(host["networks"]) == {"server", "egress"}
    networks = compose["networks"]
    assert networks["server"]["external"] is True
    assert networks["server"]["name"] == "omnigent-host-${AE_HOST_USER}"
    assert networks["egress"]["internal"] is True
    assert networks["egress"]["driver_opts"]["com.docker.network.bridge.inhibit_ipv4"] == "true"
    assert "internal" not in networks["outbound"]
    assert set(compose["services"]["egress"]["networks"]) == {"egress", "outbound"}
    env = host["environment"]
    assert env["OMNIGENT_SERVER_URL"] == "http://omnigent:8000"
    assert env["HTTPS_PROXY"] == env["HTTP_PROXY"] == "http://egress:3128"
    assert "omnigent" in env["NO_PROXY"].split(",")
    passthrough = env["OMNIGENT_RUNNER_ENV_PASSTHROUGH"].split(",")
    for name in ("HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY"):
        assert name in passthrough, f"{name} must reach the runners"
    assert list(compose["volumes"]) == ["home"]

    dockerfile = (HOST_CONTAINER / "Dockerfile").read_text().splitlines()
    users = [line.split()[1] for line in dockerfile if line.startswith("USER ")]
    assert users[-1] == "agent", "the image must end on the non-root user"


def test_egress_proxy_refuses_private_and_tailnet_destinations() -> None:
    conf = (HOST_CONTAINER / "squid.conf").read_text()
    blocked = " ".join(line for line in conf.splitlines() if line.startswith("acl blocked_dst"))
    for cidr in (
        "10.0.0.0/8",
        "100.64.0.0/10",
        "127.0.0.0/8",
        "169.254.0.0/16",
        "172.16.0.0/12",
        "192.168.0.0/16",
        "fc00::/7",
    ):
        assert cidr in blocked, f"{cidr} must be refused"
    rules = [line for line in conf.splitlines() if line.startswith("http_access")]
    assert rules.index("http_access deny blocked_dst") < rules.index("http_access allow clients")
    assert rules[-1] == "http_access deny all"


def test_merged_compose_config(tmp_path: Path) -> None:
    """``docker compose config`` over upstream's file plus the override, with the
    env-file variables pointed at temp copies, must yield one omnigent
    service on the published image, one loopback port, bind mounts and no build."""
    if _tool("docker") is None:
        _skip_missing("docker", "install Docker with the compose plugin")
    probe = subprocess.run(
        ["docker", "compose", "version"], capture_output=True, text=True, check=False
    )
    if probe.returncode != 0:
        _skip_missing("docker compose", "install the compose plugin")

    settings = tmp_path / "server.env"
    settings.write_text(
        f"OMNIGENT_IMAGE_TAG=ae-v0.13.0.1\nAE_IMAGE={IMAGE}\n"
        "OMNIGENT_PORT=8010\nOMNIGENT_AUTH_ENABLED=1\n"
    )
    secrets = tmp_path / "omnigent-ae.env"
    secrets.write_text("POSTGRES_PASSWORD=test-only\n")
    override = tmp_path / "override.yml"
    override.write_text(OVERRIDE.read_text())
    result = subprocess.run(
        [
            "docker",
            "compose",
            "--env-file",
            str(settings),
            "--env-file",
            str(secrets),
            "-f",
            str(UPSTREAM_COMPOSE),
            "-f",
            str(override),
            "config",
        ],
        capture_output=True,
        text=True,
        check=False,
        env={
            **os.environ,
            "DOCKER_HOST": "unix:///nonexistent/docker.sock",
            "AE_SERVER_SETTINGS": str(settings),
            "AE_SERVER_SECRETS": str(secrets),
        },
    )
    assert result.returncode == 0, result.stderr
    merged = yaml.safe_load(result.stdout)
    svc = merged["services"]["omnigent"]
    assert svc["image"] == f"{IMAGE}:ae-v0.13.0.1"
    assert "build" not in svc
    assert len(svc["ports"]) == 1
    port = svc["ports"][0]
    assert (port["host_ip"], str(port["published"]), port["target"]) == ("127.0.0.1", "8010", 8000)
    mounts = {(v["type"], v["source"], v["target"]) for v in svc["volumes"]}
    assert mounts == {("bind", "/secure/omnigent/data", "/data")}
    pg_mounts = {
        (v["type"], v["source"], v["target"]) for v in merged["services"]["postgres"]["volumes"]
    }
    assert pg_mounts == {("bind", "/secure/omnigent/postgres", "/var/lib/postgresql/data")}
    # Compose renders extra_hosts as a list or a mapping depending on its version.
    assert "host.docker.internal" in str(svc["extra_hosts"]) and "host-gateway" in str(
        svc["extra_hosts"]
    )
    assert svc["environment"]["DATABASE_URL"].startswith(
        "postgresql+psycopg://omnigent:test-only@postgres"
    )
    assert svc["environment"]["OMNIGENT_AUTH_ENABLED"] == "1"
    assert merged["services"]["postgres"]["image"].startswith("postgres:16")


# ── the systemd units ──────────────────────────────────────────────────────


def test_all_four_units_exist() -> None:
    assert [u.name for u in UNITS] == [
        "ae-omnigent-dump.service",
        "ae-omnigent-dump.timer",
        "ae-omnigent-host.service",
        "ae-omnigent.service",
    ]


@pytest.mark.parametrize("unit", UNITS, ids=lambda p: p.name)
def test_units_parse_and_carry_the_common_keys(unit: Path) -> None:
    parsed = _unit(unit)
    assert parsed.has_section("Unit") and parsed.get("Unit", "Description")
    if unit.suffix == ".service":
        assert parsed.has_section("Service")
        assert parsed.has_option("Service", "ExecStart")
        for exe in _unit_values(unit, "Service", "ExecStart") + _unit_values(
            unit, "Service", "ExecStop"
        ):
            assert exe.startswith(("/", "%h/")), (
                f"{unit.name}: ExecStart/ExecStop must be an absolute path: {exe}"
            )
    else:
        assert parsed.has_section("Timer")
    text = unit.read_text()
    assert "\t" not in text
    for line in text.splitlines():
        assert not line.endswith(" "), f"{unit.name}: trailing space: {line!r}"


def test_server_unit_waits_for_secure_docker_and_tailscale() -> None:
    unit = DEPLOY / "systemd" / "ae-omnigent.service"
    parsed = _unit(unit)
    assert parsed.get("Unit", "RequiresMountsFor") == "/secure"
    after = " ".join(_unit_values(unit, "Unit", "After")).split()
    assert {"docker.service", "tailscaled.service"} <= set(after)
    assert parsed.get("Unit", "Requires") == "docker.service"
    assert parsed.get("Service", "Type") == "oneshot"
    assert parsed.get("Service", "RemainAfterExit") == "yes"
    assert (
        parsed.get("Service", "ExecStart")
        == f"{INSTALL_ROOT}/deploy/compose.sh up -d --remove-orphans"
    )
    assert parsed.get("Service", "ExecStop") == f"{INSTALL_ROOT}/deploy/compose.sh down"
    assert parsed.get("Install", "WantedBy") == "multi-user.target"
    assert "tailscale serve --bg --https 8010 http://localhost:8010" in unit.read_text()


def test_host_unit_is_upstreams_foreground_service_entry() -> None:
    unit = DEPLOY / "systemd" / "ae-omnigent-host.service"
    parsed = _unit(unit)
    assert parsed.get("Service", "Type") == "simple"
    exec_start = parsed.get("Service", "ExecStart")
    assert exec_start == (
        "%h/.omnigent-host/venv/bin/python -m omnigent.host.service_entry --server ${OMNIGENT_SERVER_URL}"
    )
    assert "--background" not in exec_start
    assert _unit_values(unit, "Service", "EnvironmentFile") == [
        "%h/deploy/omnigent-ae/config/host.env",
        HOST_SECRETS_PATH,
    ]
    assert parsed.get("Service", "Restart") == "on-failure"
    assert parsed.get("Service", "RestartPreventExitStatus") == "78 143"
    path = parsed.get("Service", "Environment")
    assert path.startswith("PATH=%h/.omnigent-host/venv/bin:%h/.local/node/bin:")
    entries = path.removeprefix("PATH=").split(":")
    # fab (uv tool), for claude-native sessions. The Fabric robot wrappers'
    # folder is a box-only drop-in since 2026-10-21, never this tracked unit.
    assert entries == [
        "%h/.omnigent-host/venv/bin",
        "%h/.local/node/bin",
        "%h/.local/bin",
        "/usr/local/bin",
        "/usr/bin",
        "/bin",
    ]
    assert "agents-and-automation" not in unit.read_text()
    # The Fabric service principal stays out of the host's environment.
    assert not any("fabric" in f for f in _unit_values(unit, "Service", "EnvironmentFile"))
    assert parsed.get("Install", "WantedBy") == "default.target", "a user unit"
    assert not parsed.has_option("Service", "User"), "a user unit names no User="


def test_dump_units_run_before_restic_and_outside_the_collision_map() -> None:
    service = DEPLOY / "systemd" / "ae-omnigent-dump.service"
    timer = DEPLOY / "systemd" / "ae-omnigent-dump.timer"
    s = _unit(service)
    assert s.get("Unit", "RequiresMountsFor") == "/secure"
    assert s.get("Service", "Type") == "oneshot"
    assert s.get("Service", "ExecStart") == f"{INSTALL_ROOT}/deploy/backup-omnigent.sh"
    t = _unit(timer)
    assert t.get("Timer", "OnCalendar") == "*-*-* 02:40:00"
    delay = int(t.get("Timer", "RandomizedDelaySec"))
    # 02:40 plus the delay must stay before ae-krisp-ingest at :45 and well
    # before ae-backup at 03:10 (the other nightly jobs on the box).
    assert 0 < delay <= 240
    assert t.get("Timer", "Persistent") == "true"
    assert t.get("Install", "WantedBy") == "timers.target"
    assert "03:10" in timer.read_text(), "the timer comment names the restic slot it runs ahead of"


# ── the scripts ────────────────────────────────────────────────────────────


def test_scripts_are_the_expected_set_and_executable() -> None:
    assert [s.name for s in SCRIPTS] == [
        "ae-limits-statusline.sh",
        "auto-update.sh",
        "backup-omnigent.sh",
        "compose.sh",
        "host-container.sh",
        "host-relink.sh",
        "install-host.sh",
        "pull-secrets.sh",
    ]
    for script in SCRIPTS:
        assert os.access(script, os.X_OK), f"{script.name} is not executable"
        head = script.read_text().splitlines()
        assert head[0] == "#!/usr/bin/env bash"
        assert "set -euo pipefail" in head[:40], (
            f"{script.name} must set -euo pipefail near the top"
        )


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.name)
def test_scripts_parse(script: Path) -> None:
    subprocess.run(["bash", "-n", str(script)], check=True)


@pytest.mark.parametrize("script", SHELLCHECKED, ids=lambda p: str(p.relative_to(ROOT)))
def test_scripts_are_shellcheck_clean(script: Path) -> None:
    if _tool("shellcheck") is None:
        _skip_missing("shellcheck", "apt-get install shellcheck")
    result = subprocess.run(
        ["shellcheck", "-x", str(script.relative_to(ROOT))],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_pull_secrets_never_echoes_a_value_and_writes_atomically() -> None:
    text = (DEPLOY / "pull-secrets.sh").read_text()
    assert "set -x" not in text
    # The four Key Vault names (checked against the vault 2026-09-24), each overridable.
    for name in (
        "omnigent-ae-postgres-password",
        "cf-access-svc-taskstore-client-id",
        "cf-access-svc-taskstore-client-secret",
        "claude-code-oauth-token",
        "omnigent-ae-github-token",
        "aetraining-wake-url",
        "aetraining-suspend-key",
        "fabric-capacities-url",
        "fabric-capacities-key",
    ):
        assert name in text
    assert "--vault-name" in text and "ae-secrets-vault" in text
    assert (
        'install -m "$MODE" -o "$OWNER" -g "$GROUP"' in text and 'mv -f "$dst.tmp" "$dst"' in text
    )
    assert "MODE=640" in text and "GROUP=" in text
    # The keys it writes are the ones the env examples and the compose override expect.
    for key in (
        "POSTGRES_PASSWORD",
        "AE_CF_ACCESS_CLIENT_ID",
        "AE_CF_ACCESS_CLIENT_SECRET",
        "CLAUDE_CODE_OAUTH_TOKEN",
        "AE_GITHUB_TOKEN",
        "AE_FABRIC_WAKE_URL_AETRAINING",
        "AE_FABRIC_SUSPEND_KEY_AETRAINING",
        "AE_FABRIC_CAPACITIES_URL",
        "AE_FABRIC_CAPACITIES_KEY",
    ):
        assert f"printf '%s=%s\\n' {key} " in text
    # No secret is ever an argument: every value goes through printf -v into a variable.
    assert "printf -v" in text
    assert (
        re.search(r"echo .*\$(POSTGRES_PASSWORD|AE_CF_|CLAUDE_CODE|AE_GITHUB|AE_FABRIC)", text)
        is None
    )


def test_pull_secrets_help_and_check_run_without_a_vault(tmp_path: Path) -> None:
    """``--help`` prints the header; ``--check`` against an empty directory
    reports both files missing and exits 1, with no network and no sudo."""
    script = DEPLOY / "pull-secrets.sh"
    helped = subprocess.run([str(script), "--help"], capture_output=True, text=True, check=True)
    assert "Pull the OmniGent deployment's secrets" in helped.stdout
    checked = subprocess.run(
        [str(script), "--check"],
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "AE_SECURE_ENV_DIR": str(tmp_path)},
    )
    assert checked.returncode == 1
    assert checked.stdout.count("MISSING") == 2
    (tmp_path / "omnigent-ae.env").write_text("POSTGRES_PASSWORD=x\nAE_CF_ACCESS_CLIENT_ID=x\n")
    os.chmod(tmp_path / "omnigent-ae.env", 0o640)
    checked = subprocess.run(
        [str(script), "--check"],
        capture_output=True,
        text=True,
        check=False,
        env={
            **os.environ,
            "AE_SECURE_ENV_DIR": str(tmp_path),
            "AE_ENV_OWNER": "root",
            "AE_ENV_GROUP": "root",
        },
    )
    assert checked.returncode == 1
    assert "MISSING AE_CF_ACCESS_CLIENT_SECRET" in checked.stdout
    assert "x" not in [line.split()[-1] for line in checked.stdout.splitlines() if "ok" in line]


def test_pull_secrets_full_run_writes_both_files_and_leaves_no_temp_copy(
    tmp_path: Path,
) -> None:
    """A full run against a fake ``az`` writes both env files and removes its
    plaintext temp directory on exit. The temp directory once survived because
    the EXIT trap read a ``local`` that had gone out of scope (2026-09-24)."""
    if os.geteuid() != 0 and shutil.which("sudo") is None:
        pytest.skip("the placement step needs root or sudo")
    bindir = tmp_path / "bin"
    bindir.mkdir()
    fake_az = bindir / "az"
    fake_az.write_text(
        "#!/bin/bash\n"
        'if [ "$1 $2" = "account show" ]; then exit 0; fi\n'
        'for a; do case "$prev" in --name) echo "value-of-$a";; esac; prev="$a"; done\n'
    )
    fake_az.chmod(0o755)
    envdir = tmp_path / "env"
    envdir.mkdir()
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    group = grp.getgrgid(os.getgid()).gr_name
    owner = pwd.getpwuid(os.getuid()).pw_name
    ran = subprocess.run(
        [str(DEPLOY / "pull-secrets.sh")],
        capture_output=True,
        text=True,
        check=False,
        env={
            **os.environ,
            "PATH": f"{bindir}:{os.environ['PATH']}",
            "AE_SECURE_ENV_DIR": str(envdir),
            "AE_ENV_OWNER": owner,
            "AE_ENV_GROUP": group,
            "TMPDIR": str(scratch),
        },
    )
    assert ran.returncode == 0, ran.stderr
    assert "parameter null or not set" not in ran.stderr
    assert "POSTGRES_PASSWORD=value-of-" in (envdir / "omnigent-ae.env").read_text()
    assert "CLAUDE_CODE_OAUTH_TOKEN=value-of-" in (envdir / "omnigent-host.env").read_text()
    assert list(scratch.iterdir()) == []
    # The fake vault answers every name, the optional ones included.
    server = (envdir / "omnigent-ae.env").read_text()
    assert "AE_GITHUB_TOKEN=value-of-omnigent-ae-github-token" in server
    assert "AE_FABRIC_WAKE_URL_AETRAINING=value-of-aetraining-wake-url" in server
    assert "AE_FABRIC_SUSPEND_KEY_AETRAINING=value-of-aetraining-suspend-key" in server
    assert "AE_FABRIC_CAPACITIES_URL=value-of-fabric-capacities-url" in server
    assert "AE_FABRIC_CAPACITIES_KEY=value-of-fabric-capacities-key" in server


def test_pull_secrets_runs_without_the_optional_secrets(tmp_path: Path) -> None:
    """The GitHub token (Your move, 2026-09-24) and the Admin Center's Fabric pair
    (2026-10-16) are optional: a vault without them still writes both files,
    leaves the keys out, says which feature stays off, and ``--check`` reports
    them absent without failing."""
    if os.geteuid() != 0 and shutil.which("sudo") is None:
        pytest.skip("the placement step needs root or sudo")
    bindir = tmp_path / "bin"
    bindir.mkdir()
    fake_az = bindir / "az"
    fake_az.write_text(
        "#!/bin/bash\n"
        'if [ "$1 $2" = "account show" ]; then exit 0; fi\n'
        'for a; do case "$prev" in --name) name="$a";; esac; prev="$a"; done\n'
        'case "$name" in omnigent-ae-github-token|aetraining-*|fabric-capacities-*)\n'
        '  echo "SecretNotFound" >&2; exit 3;;\n'
        "esac\n"
        'echo "value-of-$name"\n'
    )
    fake_az.chmod(0o755)
    envdir = tmp_path / "env"
    envdir.mkdir()
    group = grp.getgrgid(os.getgid()).gr_name
    owner = pwd.getpwuid(os.getuid()).pw_name
    env = {
        **os.environ,
        "PATH": f"{bindir}:{os.environ['PATH']}",
        "AE_SECURE_ENV_DIR": str(envdir),
        "AE_ENV_OWNER": owner,
        "AE_ENV_GROUP": group,
    }
    ran = subprocess.run(
        [str(DEPLOY / "pull-secrets.sh")], capture_output=True, text=True, check=False, env=env
    )
    assert ran.returncode == 0, ran.stderr
    assert "omnigent-ae-github-token" in ran.stdout and "stays off" in ran.stdout
    assert "aetraining-wake-url" in ran.stdout and "aetraining-suspend-key" in ran.stdout
    server = (envdir / "omnigent-ae.env").read_text()
    assert "AE_CF_ACCESS_CLIENT_SECRET=value-of-" in server
    assert "AE_GITHUB_TOKEN" not in server
    assert "AE_FABRIC" not in server
    checked = subprocess.run(
        [str(DEPLOY / "pull-secrets.sh"), "--check"],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    assert checked.returncode == 0, checked.stdout
    assert "absent AE_GITHUB_TOKEN (optional)" in checked.stdout
    assert "absent AE_FABRIC_WAKE_URL_AETRAINING (optional)" in checked.stdout
    assert "absent AE_FABRIC_SUSPEND_KEY_AETRAINING (optional)" in checked.stdout
    assert "absent AE_FABRIC_CAPACITIES_URL (optional)" in checked.stdout
    assert "absent AE_FABRIC_CAPACITIES_KEY (optional)" in checked.stdout


@pytest.mark.parametrize(
    "secret",
    [
        "aetraining-wake-url",
        "aetraining-suspend-key",
        "fabric-capacities-url",
        "fabric-capacities-key",
    ],
)
def test_pull_secrets_rejects_a_fabric_value_with_a_dollar(tmp_path: Path, secret: str) -> None:
    """Compose interpolates ``$`` in the env file, so a Fabric value holding one
    would arrive altered. The run stops before writing anything, names the
    secret, and never prints the value (2026-10-16)."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    fake_az = bindir / "az"
    fake_az.write_text(
        "#!/bin/bash\n"
        'if [ "$1 $2" = "account show" ]; then exit 0; fi\n'
        'for a; do case "$prev" in --name) name="$a";; esac; prev="$a"; done\n'
        f'if [ "$name" = {secret} ]; then echo '
        "'"
        "pa$sswordDOLLARVALUE"
        "'"
        "; exit 0; fi\n"
        'echo "value-of-$name"\n'
    )
    fake_az.chmod(0o755)
    envdir = tmp_path / "env"
    envdir.mkdir()
    ran = subprocess.run(
        [str(DEPLOY / "pull-secrets.sh")],
        capture_output=True,
        text=True,
        check=False,
        env={
            **os.environ,
            "PATH": f"{bindir}:{os.environ['PATH']}",
            "AE_SECURE_ENV_DIR": str(envdir),
            "AE_ENV_OWNER": pwd.getpwuid(os.getuid()).pw_name,
            "AE_ENV_GROUP": grp.getgrgid(os.getgid()).gr_name,
        },
    )
    assert ran.returncode != 0
    output = ran.stdout + ran.stderr
    assert secret in output
    assert "Compose interpolates" in output
    assert "DOLLARVALUE" not in output
    assert list(envdir.iterdir()) == []


def test_backup_script_dumps_through_compose_and_keeps_seven() -> None:
    text = (DEPLOY / "backup-omnigent.sh").read_text()
    assert "/secure/omnigent/backups" in text
    assert 'KEEP="${AE_BACKUP_KEEP:-7}"' in text
    assert "mountpoint -q" in text
    assert "exec -T postgres sh -c 'exec pg_dump" in text
    assert "gzip -t" in text and 'mv -f "$tmp" "$out"' in text
    assert "03:10" in (DEPLOY / "systemd" / "ae-omnigent-dump.timer").read_text()


def test_backup_script_refuses_an_unmounted_volume(tmp_path: Path) -> None:
    result = subprocess.run(
        [str(DEPLOY / "backup-omnigent.sh")],
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "AE_BACKUP_DIR": str(tmp_path / "omnigent" / "backups")},
    )
    assert result.returncode == 1
    assert "not a mountpoint" in result.stderr
    assert not (tmp_path / "omnigent").exists()


def test_compose_wrapper_names_both_files_and_both_compose_files() -> None:
    text = (DEPLOY / "compose.sh").read_text()
    assert "exec docker compose" in text
    for needle in (
        '--env-file "$SERVER_SETTINGS"',
        '--env-file "$SERVER_SECRETS"',
        '-f "$UPSTREAM_COMPOSE"',
        '-f "$OVERRIDE"',
        '-f "$LOCAL_COMPOSE"',
        'config/compose.local.yml"',
        "config/secrets.env",
        "/secure/env/omnigent-ae.env",
        "deploy/docker-compose.override.yml",
        "docker-compose.yaml",
    ):
        assert needle in text
    missing = subprocess.run(
        [str(DEPLOY / "compose.sh"), "config"],
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "AE_SERVER_SETTINGS": "/nonexistent/server.env"},
    )
    assert missing.returncode == 1 and "config/server.env.example" in missing.stderr


def _compose_args(
    tmp_path: Path,
    env: dict[str, str],
    command: tuple[str, ...] = ("config",),
    expect_refusal: bool = False,
) -> list[str]:
    """What compose.sh hands docker, from a copy of the checkout with a stub docker."""
    root = tmp_path / "checkout"
    (root / "deploy").mkdir(parents=True)
    (root / "scripts").mkdir()
    (root / "upstream" / "deploy" / "docker").mkdir(parents=True)
    shutil.copy2(DEPLOY / "compose.sh", root / "deploy" / "compose.sh")
    shutil.copy2(ROOT / "scripts" / "_lib.sh", root / "scripts" / "_lib.sh")
    (root / "upstream" / "deploy" / "docker" / "docker-compose.yaml").write_text("services: {}\n")
    (root / "config").mkdir()
    (root / "config" / "server.env").write_text("OMNIGENT_PORT=8010\n")
    for name, body in env.items():
        if name.startswith("config/"):
            (root / name).write_text(body)
    stub = tmp_path / "bin"
    stub.mkdir()
    # `ps` answers with $FAKE_PS_OWNERS (the config files of a running project
    # named omnigent),
    # `network ls` with nothing; `compose` prints the arguments it was given.
    (stub / "docker").write_text(
        '#!/bin/sh\ncase "$1" in\n'
        '  ps) case "$*" in *"project=omnigent "*) printf "%s\\n" "${FAKE_PS_OWNERS:-}" ;; esac; exit 0 ;;\n'
        "  network) exit 0 ;;\n"
        'esac\nprintf "%s\\n" "$@"\n'
    )
    (stub / "docker").chmod(0o755)
    run_env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("AE_SERVER_", "COMPOSE_PROJECT_NAME", "FAKE_PS_OWNERS"))
        and k != "PATCH_ROOT"
    }
    run_env |= {k: v for k, v in env.items() if not k.startswith("config/")}
    run_env["PATH"] = f"{stub}:{run_env['PATH']}"
    result = subprocess.run(
        [str(root / "deploy" / "compose.sh"), *command],
        capture_output=True,
        text=True,
        check=False,
        env=run_env,
    )
    if expect_refusal:
        assert result.returncode == 1, result.stdout
        return [result.stderr]
    assert result.returncode == 0, result.stderr
    return [line.replace(str(root), "<root>") for line in result.stdout.splitlines()]


def test_compose_wrapper_prefers_a_checkout_secrets_file_and_adds_the_local_compose(
    tmp_path: Path,
) -> None:
    """config/secrets.env wins over /secure, so a try-out checkout on the box never
    starts on the live secrets; config/compose.local.yml rides along when it
    exists (2026-10-22, README.md "Quick start")."""
    args = _compose_args(
        tmp_path,
        {
            "config/secrets.env": "POSTGRES_PASSWORD=test-only\n",
            "config/compose.local.yml": "services: {}\n",
        },
    )
    assert args[args.index("--env-file", 2) + 1] == "<root>/config/secrets.env"
    files = [args[i + 1] for i, arg in enumerate(args) if arg == "-f"]
    assert files[-1] == "<root>/config/compose.local.yml"
    assert files[1].endswith("deploy/docker-compose.override.yml")


def test_compose_wrapper_takes_named_secrets_and_no_local_compose(tmp_path: Path) -> None:
    secrets = tmp_path / "named.env"
    secrets.write_text("POSTGRES_PASSWORD=test-only\n")
    args = _compose_args(
        tmp_path,
        {"config/secrets.env": "POSTGRES_PASSWORD=other\n", "AE_SERVER_SECRETS": str(secrets)},
    )
    assert args[args.index("--env-file", 2) + 1] == str(secrets)
    assert not any(arg.endswith("compose.local.yml") for arg in args)


def test_compose_wrapper_refuses_a_project_another_checkout_runs(tmp_path: Path) -> None:
    """A Quick start beside the full deploy must not recreate or stop the running
    stack, which upstream's compose file names `omnigent` too (from the Quick
    start's review, 2026-10-22)."""
    other = "/srv/deploy/upstream/deploy/docker/docker-compose.yaml,/srv/deploy/deploy/docker-compose.override.yml"
    base = {"config/secrets.env": "POSTGRES_PASSWORD=test-only\n", "FAKE_PS_OWNERS": other}
    for verb in (("up", "-d"), ("down",), ("restart", "omnigent")):
        (err,) = _compose_args(tmp_path / verb[0], base, verb, expect_refusal=True)
        assert "already runs from another checkout" in err and "COMPOSE_PROJECT_NAME" in err
    # Read-only commands and another project name go through.
    assert _compose_args(tmp_path / "config", base)[0] == "compose"
    renamed = base | {"COMPOSE_PROJECT_NAME": "omnigent-try"}
    assert "up" in _compose_args(tmp_path / "renamed", renamed, ("up", "-d"))
    flagged = _compose_args(tmp_path / "flag", base, ("-p", "omnigent-try", "up", "-d"))
    assert "omnigent-try" in flagged


def test_compose_wrapper_lets_its_own_checkout_through(tmp_path: Path) -> None:
    # The box's server and Postgres were last created from two different
    # checkouts; one container from this one is enough.
    own = str(tmp_path / "own" / "checkout" / "deploy" / "docker-compose.override.yml")
    owners = f"/elsewhere/deploy/docker-compose.override.yml\nupstream.yaml,{own}"
    args = _compose_args(
        tmp_path / "own", {"FAKE_PS_OWNERS": owners, "config/secrets.env": "X=1\n"}, ("up", "-d")
    )
    assert args[-2:] == ["up", "-d"]


def test_install_host_uses_uv_and_the_pinned_submodule() -> None:
    text = (DEPLOY / "install-host.sh").read_text()
    assert "uv venv" in text and "uv pip install" in text
    assert "OMNIGENT_SKIP_WEB_UI=true" in text
    assert '"$UPSTREAM_DIR"' in text and 'packages/ae_omni_policies"' in text
    assert "import ae_omni_policies.mcp" in text
    # The limits relay runs from this venv too (docs/PLAN-LIMITS.md, 2026-09-24).
    assert "ae_omni_policies.limits_relay" in text


def test_install_host_registers_the_ae_tools_for_interactive_sessions() -> None:
    # Interactive Claude Code and Codex sessions on the box get the same stdio
    # server the scheduled agents name in their YAML, so a session can flip its
    # own ae.status (2026-09-24). Idempotent, and a missing CLI only warns.
    text = (DEPLOY / "install-host.sh").read_text()
    server = '"$VENV/bin/python" -m ae_omni_policies.mcp'
    assert f"claude mcp add --scope user ae -- {server}" in text
    assert f"codex mcp add ae -- {server}" in text
    assert "claude mcp get ae" in text and "codex mcp get ae" in text
    registration = text.split("claude mcp get ae", 1)[1]
    assert "die " not in registration


def test_install_host_puts_the_user_bin_dirs_on_path_before_needing_uv() -> None:
    # Shells started by OmniGent lack ~/.local/bin, where uv lives (2026-09-24).
    text = (DEPLOY / "install-host.sh").read_text()
    path_line = text.index('PATH="$HOME/.local/bin:$HOME/.local/node/bin:$PATH"')
    assert path_line < text.index("command -v uv")


# ── the env examples ───────────────────────────────────────────────────────

SERVER_VARS = {
    "OMNIGENT_IMAGE_TAG",
    "AE_IMAGE",
    "OMNIGENT_PORT",
    "OMNIGENT_AUTH_ENABLED",
    "OMNIGENT_ACCOUNTS_BASE_URL",
    "OMNIGENT_WS_ALLOWED_ORIGINS",
    "OMNIGENT_CONFIG",
    "OMNIGENT_BUILTIN_AGENT_DIRS",
    "AE_TASK_STORE_URL",
    "AE_TASK_STORE_TZ",
    "AE_SELF_URL",
    "AE_SLOT_LIMIT",
    "OMNIGENT_SCHEDULED_TASK_DEFAULT_PROJECT",
    "OMNIGENT_GRANT_MAX_LIFETIME_DAYS",
    "POSTGRES_PASSWORD",
    "AE_CF_ACCESS_CLIENT_ID",
    "AE_CF_ACCESS_CLIENT_SECRET",
    "OMNIGENT_ACCOUNTS_COOKIE_SECRET",
    "OMNIGENT_AUTH_PROVIDER",
    "OMNIGENT_OIDC_ISSUER",
    "OMNIGENT_OIDC_CLIENT_ID",
    "OMNIGENT_OIDC_REDIRECT_URI",
    "OMNIGENT_OIDC_EMAIL_CLAIM",
    "OMNIGENT_OIDC_SKIP_EMAIL_VERIFICATION",
    "OMNIGENT_OIDC_SESSION_TTL_HOURS",
    "OMNIGENT_OIDC_CLIENT_SECRET",
    "OMNIGENT_OIDC_COOKIE_SECRET",
    "AE_FABRIC_CAPACITIES_URL",
    "AE_FABRIC_CAPACITIES_KEY",
}
HOST_VARS = {
    "OMNIGENT_SERVER_URL",
    "OMNIGENT_RUNNER_ENV_PASSTHROUGH",
    "OMNIGENT_TOKEN",
    "CLAUDE_CODE_OAUTH_TOKEN",
}
SECRET_VARS = {
    "POSTGRES_PASSWORD",
    "AE_CF_ACCESS_CLIENT_ID",
    "AE_CF_ACCESS_CLIENT_SECRET",
    "OMNIGENT_ACCOUNTS_COOKIE_SECRET",
    "OMNIGENT_OIDC_CLIENT_SECRET",
    "OMNIGENT_OIDC_COOKIE_SECRET",
    "OMNIGENT_TOKEN",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "ANTHROPIC_API_KEY",
    "AE_FABRIC_CAPACITIES_URL",
    "AE_FABRIC_CAPACITIES_KEY",
}


def test_server_yaml_ships_a_placeholder_admin() -> None:
    """The real admin lives in the data volume's roster file, not in git (2026-10-21).

    Upstream merges ``admins:`` with ``<data_dir>/admins`` (``/data/admins`` in
    the container), so the tracked file names nobody real.
    """
    config = yaml.safe_load((CONFIG / "server.yaml").read_text())
    assert config["admins"] == ["you@example.com"]
    assert "/data/admins" in (CONFIG / "server.yaml").read_text()


def _env_example(path: Path) -> tuple[dict[str, str], set[str]]:
    """Active assignments and the names mentioned in commented lines."""
    active: dict[str, str] = {}
    mentioned: set[str] = set()
    for raw in path.read_text().splitlines():
        line = raw.strip()
        m = re.match(r"^#?\s*([A-Z][A-Z0-9_]+)=(.*)$", line)
        if not m:
            continue
        key, value = m.group(1), m.group(2).strip()
        if line.startswith("#"):
            mentioned.add(key)
        else:
            active[key] = value
    return active, mentioned


@pytest.mark.parametrize(
    ("example", "required"),
    [(CONFIG / "server.env.example", SERVER_VARS), (CONFIG / "host.env.example", HOST_VARS)],
    ids=["server", "host"],
)
def test_env_examples_name_every_variable_and_no_secret_value(
    example: Path, required: set[str]
) -> None:
    active, mentioned = _env_example(example)
    assert required <= (set(active) | mentioned), required - (set(active) | mentioned)
    for key in SECRET_VARS & set(active):
        pytest.fail(
            f"{example.name} assigns the secret {key}; secrets stay commented, values come from /secure/env"
        )
    for key, value in active.items():
        assert not re.fullmatch(r"[0-9a-f]{32,}", value), (
            f"{example.name}: {key} looks like a secret"
        )
        assert not value.startswith(("sk-", "ghp_", "github_pat_")), (
            f"{example.name}: {key} looks like a secret"
        )


def test_env_examples_agree_with_the_override_and_the_units() -> None:
    server, _ = _env_example(CONFIG / "server.env.example")
    host, _ = _env_example(CONFIG / "host.env.example")
    assert server["OMNIGENT_PORT"] == "8010"
    # 90 days, not upstream's 30, so a login made before a long absence
    # outlasts it (2026-09-24, deploy/README.md "Re-login").
    assert server["OMNIGENT_GRANT_MAX_LIFETIME_DAYS"] == "90"
    # The Quick start's server (2026-10-22): this machine, accounts mode, the
    # base URL leading the allowlist.
    assert server["OMNIGENT_ACCOUNTS_BASE_URL"] == "http://127.0.0.1:8010"
    assert server["OMNIGENT_WS_ALLOWED_ORIGINS"] == server["OMNIGENT_ACCOUNTS_BASE_URL"]
    assert server["OMNIGENT_AUTH_ENABLED"] == "1"
    assert not any(key.startswith("OMNIGENT_OIDC_") for key in server), (
        "OIDC stays commented: an issuer, even a placeholder, switches accounts mode off"
    )
    assert "OMNIGENT_AUTH_PROVIDER" not in server
    assert "AE_GRAPH_MCP_URL" not in server, "a placeholder URL registers a dead MCP server"
    # The OIDC block, uncommented, is the Microsoft sign-in the full deploy
    # uses (2026-10-14): one callback host and a 30-day session.
    _, mentioned = _env_example(CONFIG / "server.env.example")
    text = (CONFIG / "server.env.example").read_text()
    assert {
        "OMNIGENT_AUTH_PROVIDER",
        "OMNIGENT_OIDC_ISSUER",
        "OMNIGENT_OIDC_REDIRECT_URI",
    } <= mentioned
    assert "# OMNIGENT_OIDC_REDIRECT_URI=https://omnigent.<your-domain>/auth/callback" in text
    assert "# OMNIGENT_OIDC_SESSION_TTL_HOURS=720" in text
    assert text.index("REQUIRED") < text.index("OMNIGENT_IMAGE_TAG=") < text.index("OPTIONAL")
    assert server["AE_SELF_URL"] == "http://127.0.0.1:8000", (
        "inside the container the server is on 8000"
    )
    # Config and agents ride the image (2026-09-24), so a tag is the whole deploy.
    assert server["OMNIGENT_CONFIG"] == "/opt/ae/config/server.yaml"
    assert server["OMNIGENT_BUILTIN_AGENT_DIRS"] == (
        "/opt/ae/agents/steward:/opt/ae/agents/shutdown:/opt/ae/agents/orchestrator"
    )
    assert host["OMNIGENT_SERVER_URL"] == server["OMNIGENT_ACCOUNTS_BASE_URL"]
    assert set(host["OMNIGENT_RUNNER_ENV_PASSTHROUGH"].split(",")) == {
        "OMNIGENT_SERVER_URL",
        "OMNIGENT_TOKEN",
    }


def test_env_example_names_exist_upstream_or_in_the_ae_layer() -> None:
    """Every OMNIGENT_* name in the examples must be read somewhere in the pinned
    upstream tree; every AE_* name in packages/ae_omni_policies, the two box
    scripts that read server.env for themselves (the overlay, for AE_IMAGE and AE_DATA_DIR, and
    deploy/auto-update.sh; AE_IMAGE, 2026-10-21, https://github.com/analyticendeavors/omnigent-ae-public/issues/13)
    or, for a patch's own switch (AE_PROMPT_SUGGESTIONS, patch 0123, 2026-10-20),
    the patched tree."""
    upstream = ROOT / "upstream" / "omnigent"
    if not upstream.is_dir():
        pytest.skip("upstream/ is not checked out")
    corpus = "".join(p.read_text(errors="ignore") for p in upstream.rglob("*.py"))
    corpus += (ROOT / "upstream" / "deploy" / "docker" / "docker-compose.yaml").read_text()
    ae = "".join(
        p.read_text() for p in (ROOT / "packages" / "ae_omni_policies" / "src").rglob("*.py")
    )
    # The overlay's interpolations only: a name in its comments reads nothing.
    ae += " ".join(re.findall(r"\$\{([A-Z][A-Z0-9_]*)", OVERRIDE.read_text()))
    ae += (DEPLOY / "auto-update.sh").read_text()
    for example in (CONFIG / "server.env.example", CONFIG / "host.env.example"):
        active, mentioned = _env_example(example)
        for key in set(active) | mentioned:
            if key.startswith("OMNIGENT_"):
                assert key in corpus, f"{example.name}: the pinned upstream reads no {key}"
            elif key.startswith("AE_"):
                assert key in ae or key in corpus, (
                    f"{example.name}: neither the AE layer nor the patched upstream tree reads {key}"
                )


# ── the publishing workflow ────────────────────────────────────────────────


def _workflow() -> dict[str, Any]:
    return yaml.safe_load(IMAGE_WORKFLOW.read_text())


def test_image_workflow_triggers_and_permissions() -> None:
    wf = _workflow()
    on = wf[True] if True in wf else wf["on"]  # PyYAML reads the bare key `on` as boolean True
    assert on["push"] == {"tags": ["ae-v*"]}
    assert on["workflow_dispatch"]["inputs"]["push"]["type"] == "boolean"
    assert on["workflow_dispatch"]["inputs"]["push"]["default"] is False
    assert wf["permissions"] == {"contents": "read"}, "writes belong to the job"
    assert wf["jobs"]["image"]["permissions"] == {
        "contents": "read",
        "packages": "write",
        "id-token": "write",
        "attestations": "write",
    }
    assert "concurrency" in wf and "group" in wf["concurrency"]
    assert wf["env"] == {"AE_IMAGE": "${{ vars.AE_IMAGE }}"}, (
        "IMAGE is written by the decide step, never derived a second time"
    )


def test_image_workflow_meets_the_repo_standards() -> None:
    wf = _workflow()
    for name, job in wf["jobs"].items():
        assert isinstance(job.get("timeout-minutes"), int), f"job {name} has no timeout-minutes"
        for step in job["steps"]:
            run = step.get("run")
            if run is None:
                continue
            assert run.lstrip().startswith("set -euo pipefail"), (
                f"step {step.get('name')} run body must start with set -euo pipefail"
            )
            assert "${{" not in run, (
                f"step {step.get('name')} interpolates an expression inside its run body; pass it through env:"
            )
            for line in run.splitlines():
                if line.strip().startswith("curl"):
                    assert "--max-time" in line and "--fail" in line
    text = IMAGE_WORKFLOW.read_text()
    assert "npm install -g" not in text or re.search(r"npm install -g \S+@\d", text)


def test_image_workflow_mirrors_the_ci_image_job() -> None:
    """Stage 1 and stage 2 are built the way ci.yml builds them; the smoke checks are the same."""
    ci = yaml.safe_load((WORKFLOWS / "ci.yml").read_text())
    ci_steps = {s.get("name"): s for s in ci["jobs"]["image"]["steps"]}
    rel_steps = {s.get("name"): s for s in _workflow()["jobs"]["image"]["steps"]}
    stage1 = "Build stage 1 (upstream server image from the patched tree)"
    for key in ("context", "file", "push", "load", "cache-from", "cache-to", "provenance"):
        assert rel_steps[stage1]["with"][key] == ci_steps[stage1]["with"][key], key
    assert rel_steps[stage1]["uses"] == ci_steps[stage1]["uses"]
    assert (
        "docker build -f deploy/Dockerfile --build-arg BASE="
        in rel_steps["Build stage 2 (AE layer)"]["run"]
    )
    smoke = rel_steps[
        "Image runs the pinned version, lists the Parking Lot extension and imports the AE packages"
    ]["run"]
    for needle in (
        "omni --version",
        "omni extensions list",
        "omni extensions doctor analyticendeavors.parking-lot",
        "import ae_omni_policies",
    ):
        assert needle in smoke


def test_image_workflow_pushes_only_the_tag_with_the_workflow_token() -> None:
    rel_steps = {s.get("name"): s for s in _workflow()["jobs"]["image"]["steps"]}
    # The decide step owns the whole gate (AE_IMAGE set and valid), so the
    # login and the push need only PUSH (2026-10-21, https://github.com/analyticendeavors/omnigent-ae-public/issues/13).
    login = rel_steps["Log in to GHCR"]
    assert login["if"] == "env.PUSH == 'true'"
    assert login["with"]["password"] == "${{ secrets.GITHUB_TOKEN }}"
    assert login["with"]["registry"] == "ghcr.io"
    push = rel_steps["Push the tag"]
    assert push["if"] == "env.PUSH == 'true'"
    dry = rel_steps["Dry run, not pushed"]
    assert dry["if"] == "env.PUSH != 'true'"
    assert 'docker push "$IMAGE:$IMAGE_TAG"' in push["run"]
    decide = rel_steps["Decide the image tag and whether to push"]["run"]
    assert "tag:ae-v*)" in decide, "only an ae-v* tag ref may set push=true"
    assert "dry-run-" in decide


def _decide(tmp_path: Path, **env: str) -> tuple[subprocess.CompletedProcess[str], str]:
    steps = {s.get("name"): s for s in _workflow()["jobs"]["image"]["steps"]}
    body = steps["Decide the image tag and whether to push"]["run"]
    out = tmp_path / "github_env"
    out.write_text("")
    defaults = {
        "REF_TYPE": "tag",
        "REF_NAME": "ae-v0.18.0.60",
        "SHA": "a" * 40,
        "EVENT": "push",
        "PUSH_INPUT": "",
        "OWNER": "AnalyticEndeavors",
        "AE_IMAGE": "",
    }
    result = subprocess.run(
        ["bash", "-c", body],
        cwd=ROOT,
        env={"PATH": os.environ["PATH"], "GITHUB_ENV": str(out), **defaults, **env},
        capture_output=True,
        text=True,
        check=False,
    )
    return result, out.read_text()


def test_image_workflow_publishes_only_when_the_variable_is_set(tmp_path: Path) -> None:
    result, written = _decide(tmp_path)
    assert result.returncode == 0, result.stderr
    assert "PUSH=false" in written, "no AE_IMAGE variable: build and smoke, never push"
    assert "IMAGE=omnigent-ae-local\n" in written
    assert "AE_IMAGE is not set" in result.stdout

    result, written = _decide(tmp_path, AE_IMAGE=IMAGE)
    assert result.returncode == 0, result.stderr
    assert "PUSH=true" in written and "IMAGE_TAG=ae-v0.18.0.60" in written
    assert f"IMAGE={IMAGE}\n" in written

    result, written = _decide(tmp_path, AE_IMAGE=IMAGE, REF_TYPE="branch", REF_NAME="main")
    assert result.returncode == 0, result.stderr
    assert "PUSH=false" in written, "a branch never pushes, variable or not"


@pytest.mark.parametrize(
    "value",
    [
        "ghcr.io/someone-else/omnigent-ae",
        "docker.io/analyticendeavors/omnigent-ae",
        "ghcr.io/AnalyticEndeavors/omnigent-ae",
        "ghcr.io/analyticendeavors/omnigent-ae:latest",
        "ghcr.io/analyticendeavors/..",
        "ghcr.io/analyticendeavors/-x",
        "ghcr.io/analyticendeavors/a___b",
        "ghcr.io/analyticendeavors/x/",
    ],
)
def test_image_workflow_refuses_a_package_outside_the_owner(tmp_path: Path, value: str) -> None:
    result, written = _decide(tmp_path, AE_IMAGE=value)
    assert result.returncode != 0
    assert "must be ghcr.io/analyticendeavors/<name>" in result.stdout
    assert written == ""


@pytest.mark.parametrize(
    "value",
    ["ghcr.io/analyticendeavors/omnigent/server", "ghcr.io/analyticendeavors/a__b.c-d"],
)
def test_image_workflow_accepts_nested_and_separated_names(tmp_path: Path, value: str) -> None:
    result, written = _decide(tmp_path, AE_IMAGE=value)
    assert result.returncode == 0, result.stdout + result.stderr
    assert f"IMAGE={value}\n" in written


def test_image_workflow_attests_only_a_pushed_public_image() -> None:
    steps = _workflow()["jobs"]["image"]["steps"]
    names = [s.get("name") for s in steps]
    attest = steps[names.index("Attest the build provenance")]
    assert names.index("Attest the build provenance") == names.index("Push the tag") + 1
    assert attest["if"] == "env.PUSH == 'true' && github.event.repository.private == false"
    assert attest["uses"].startswith("actions/attest-build-provenance@")
    assert attest["with"] == {
        "subject-name": "${{ env.IMAGE }}",
        "subject-digest": "${{ env.IMAGE_DIGEST }}",
        "push-to-registry": True,
    }
    push = steps[names.index("Push the tag")]["run"]
    assert 'docker buildx imagetools inspect "$IMAGE:$IMAGE_TAG"' in push
    assert 'echo "IMAGE_DIGEST=$digest" >> "$GITHUB_ENV"' in push


# Every third-party action is pinned to a commit, with its release in a
# comment for Dependabot to keep current (2026-10-22): a moved tag cannot
# change what runs.
PINNED_USES = re.compile(r"^\s*(?:- )?uses: [\w.-]+/[\w./-]+@[0-9a-f]{40} # v\d+(?:\.\d+)*\s*$")


@pytest.mark.parametrize("workflow", sorted(WORKFLOWS.glob("*.yml")), ids=lambda p: p.name)
def test_workflow_actions_are_pinned_to_a_commit(workflow: Path) -> None:
    for number, line in enumerate(workflow.read_text().splitlines(), 1):
        if "uses:" in line and not line.lstrip().startswith("#"):
            if "uses: ./" in line or "uses: docker://" in line:
                continue  # a local action or a container image, not a tag to pin
            assert PINNED_USES.match(line), (
                f"{workflow.name}:{number} is not pinned: {line.strip()}"
            )


def test_scorecard_runs_only_on_a_public_repository() -> None:
    wf = yaml.safe_load((WORKFLOWS / "scorecard.yml").read_text())
    assert wf["permissions"] == "read-all"
    analysis = wf["jobs"]["analysis"]
    assert analysis["needs"] == "visibility"
    assert analysis["if"] == "needs.visibility.outputs.public == 'true'"
    # Scorecard refuses to publish from a job with run steps or a job env.
    assert "env" not in analysis
    assert all("run" not in step for step in analysis["steps"])
    read = wf["jobs"]["visibility"]["steps"][0]["run"]
    assert read.lstrip().startswith("set -euo pipefail")
    assert "${{" not in read


def test_dependabot_leaves_the_upstream_pin_alone() -> None:
    path = ROOT / ".github" / "dependabot.yml"
    if not path.exists():
        pytest.skip("no Dependabot config: the public sync leaves it out on purpose")
    config = yaml.safe_load(path.read_text())
    ecosystems = {u["package-ecosystem"] for u in config["updates"]}
    assert ecosystems == {"github-actions", "pip", "npm", "docker"}
    assert "gitsubmodule" not in ecosystems, "upstream moves with upstream.lock and the patches"
    pip = next(u for u in config["updates"] if u["package-ecosystem"] == "pip")
    assert {"dependency-name": "omnigent"} in pip["ignore"]


def test_image_workflow_passes_actionlint() -> None:
    if _tool("actionlint") is None:
        _skip_missing("actionlint", "uv tool install actionlint-py")
    result = subprocess.run(
        [
            "actionlint",
            str(IMAGE_WORKFLOW.relative_to(ROOT)),
            str((WORKFLOWS / "ci.yml").relative_to(ROOT)),
            str((WORKFLOWS / "patch-rebase.yml").relative_to(ROOT)),
            str((WORKFLOWS / "scorecard.yml").relative_to(ROOT)),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


# ── the runbook ────────────────────────────────────────────────────────────


def test_runbook_covers_the_handoff_steps_in_order() -> None:
    text = (DEPLOY / "README.md").read_text()
    order = [
        "pull-secrets.sh",
        "mkdir -p /secure/omnigent/data /secure/omnigent/postgres /secure/omnigent/backups",
        "/opt/ae/config/server.yaml",
        "ae-omnigent.service",
        "compose.sh up -d",
        "tailscale serve --bg --https 8010 http://localhost:8010",
        "install-host.sh",
        "omnigent login",
        "ae-omnigent-host",
        "just schedules-seed",
        "/v1/ae/health",
        "## Upgrade",
        "## Automatic updates",
        "docker login ghcr.io -u <github-user>",
        "systemctl --user enable --now ae-omnigent-auto-update.timer",
        "## Rollback",
    ]
    position = -1
    for needle in order:
        found = text.find(needle, position + 1)
        assert found > position, f"deploy/README.md: {needle!r} missing or out of order"
        position = found
    for path in (
        "ae-omnigent-dump.timer",
        "backup-omnigent.sh",
        ".github/workflows/image.yml",
        "deploy/systemd/user/ae-omnigent-auto-update.timer",
        "~/.omnigent-ae-auto-update/pin",
        "systemctl --user stop ae-omnigent-auto-update.timer",
    ):
        assert path in text


def test_nothing_deployed_runs_from_the_working_checkout() -> None:
    """Units and the compose overlay name the deploy clone, never ~/repos (2026-10-13)."""
    deployed = [
        *UNITS,
        *sorted((DEPLOY / "systemd" / "user").glob("ae-omnigent*")),
        DEPLOY / "docker-compose.override.yml",
    ]
    for path in deployed:
        assert "/home/reid/repos/omnigent-ae" not in path.read_text(), path.name
