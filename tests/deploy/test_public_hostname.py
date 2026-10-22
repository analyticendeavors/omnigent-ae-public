"""The second hostname: omnigent.go-ae.org through Cloudflare (2026-09-24).

AE's own runbook for it lives in a private repository (moved out of deploy/ on
2026-10-21). What a checkout can prove about it:
the allowlist in config/server.env.example, read by upstream's own parser and
policy, admits both origins and nothing near them; the base URL stays the
tailnet one, so hosts, invite and magic links keep using it; and the upstream
iOS shell still refuses a cross-origin redirect in its main frame, which is
the fact the doc's Access decision rests on. A sync that changes that code
fails here, so the runbook gets reread rather than silently going stale.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SERVER_EXAMPLE = ROOT / "config" / "server.env.example"
HOST_EXAMPLE = ROOT / "config" / "host.env.example"
IOS_WEBVIEW = ROOT / "upstream" / "web" / "ios" / "Omnigent" / "OmnigentWebView.swift"

PUBLIC_ORIGIN = "https://omnigent.<your-domain>"


def _active(path: Path) -> dict[str, str]:
    """The uncommented KEY=value lines of an env example."""
    out: dict[str, str] = {}
    for raw in path.read_text().splitlines():
        m = re.match(r"^([A-Z][A-Z0-9_]+)=(.*)$", raw.strip())
        if m:
            out[m.group(1)] = m.group(2).strip()
    return out


@pytest.fixture
def allowlist(monkeypatch: pytest.MonkeyPatch) -> frozenset[str]:
    ws_origin = pytest.importorskip("omnigent.server.ws_origin")
    value = _active(SERVER_EXAMPLE)["OMNIGENT_WS_ALLOWED_ORIGINS"]
    monkeypatch.setenv("OMNIGENT_WS_ALLOWED_ORIGINS", value)
    return ws_origin.parse_allowed_origins()


def test_the_allowlist_holds_exactly_the_tailnet_and_the_public_origin(
    allowlist: frozenset[str],
) -> None:
    base = _active(SERVER_EXAMPLE)["OMNIGENT_ACCOUNTS_BASE_URL"]
    assert allowlist == {base, PUBLIC_ORIGIN}


@pytest.mark.parametrize(
    ("origin", "allowed"),
    [
        (PUBLIC_ORIGIN, True),
        ("https://<box-hostname>.<tailnet>.ts.net:8010", True),
        # Non-browser clients (the host, the runner) send no Origin at all.
        (None, True),
        # Exact match only: the scheme, a trailing slash, a port or a
        # neighbouring name is a different origin and a 4403.
        ("http://omnigent.<your-domain>", False),
        ("https://omnigent.<your-domain>/", False),
        ("https://omnigent.<your-domain>:443", False),
        ("https://tasks.<your-domain>", False),
        ("https://omnigent.<your-domain>.evil.example", False),
    ],
)
def test_upstream_policy_admits_both_names_and_nothing_near_them(
    allowlist: frozenset[str], origin: str | None, allowed: bool
) -> None:
    from omnigent.server.ws_origin import origin_allowed

    # Accounts mode is not local single-user mode.
    assert origin_allowed(origin, local_mode=False, extra_allowed=allowlist) is allowed


def test_the_base_url_and_the_hosts_stay_on_the_tailnet() -> None:
    server = _active(SERVER_EXAMPLE)
    host = _active(HOST_EXAMPLE)
    # Invite, magic and device-login links are built from the base URL; hosts
    # log in with it and never cross Cloudflare Access.
    assert ".ts.net:8010" in server["OMNIGENT_ACCOUNTS_BASE_URL"]
    assert host["OMNIGENT_SERVER_URL"] == server["OMNIGENT_ACCOUNTS_BASE_URL"]
    # Both names are https, so the __Host- session cookie works on each; it is
    # host-only, so each name keeps its own login.
    assert server["OMNIGENT_ACCOUNTS_BASE_URL"].startswith("https://")


def test_the_ios_shell_still_refuses_cross_origin_main_frame_navigation() -> None:
    """AE's Cloudflare runbook, "The native iOS app and Access", rests on this."""
    if not IOS_WEBVIEW.is_file():
        pytest.skip("upstream/ is not checked out")
    swift = IOS_WEBVIEW.read_text()
    policy = swift[swift.index("decidePolicyFor navigationAction") :]
    policy = policy[: policy.index("createWebViewWith")]
    assert "url.omnigentOrigin != pinnedOrigin" in policy
    assert "startLogin(in: webView)" in policy and "decisionHandler(.cancel)" in policy
    provisional = swift[swift.index("didStartProvisionalNavigation") :][:600]
    assert "url.omnigentOrigin != pinnedOrigin" in provisional and "stopLoading()" in provisional
