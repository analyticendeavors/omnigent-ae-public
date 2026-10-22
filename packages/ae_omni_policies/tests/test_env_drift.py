"""The server.env drift check: expected keys from the example, missing ones from the env."""

from __future__ import annotations

from pathlib import Path

from ae_omni_policies import env_drift

EXAMPLE = """\
# A comment line.
OMNIGENT_IMAGE_TAG=ae-v0.18.0.9
# AE_OPTIONAL=commented out, so optional
AE_LIMITS_STATE_FILE=/data/ae-limits.json
# drift-check: pending begin (the OIDC step)
OMNIGENT_OIDC_REDIRECT_URI=https://example.invalid/auth/callback
# drift-check: pending end
  AE_INDENTED=1
OMNIGENT_IMAGE_TAG=again
lower_case=ignored
"""

REPO_EXAMPLE = Path(__file__).resolve().parents[3] / "config" / "server.env.example"


def test_expected_keys_skip_comments_and_pending_blocks() -> None:
    assert env_drift.expected_keys(EXAMPLE) == [
        "OMNIGENT_IMAGE_TAG",
        "AE_LIMITS_STATE_FILE",
        "AE_INDENTED",
    ]


def test_an_unclosed_pending_block_skips_nothing() -> None:
    text = "AE_A=1\n# drift-check: pending begin\nAE_B=1\nAE_C=1\n"
    assert env_drift.expected_keys(text) == ["AE_A", "AE_B", "AE_C"]


def test_missing_names_absent_and_empty_keys(tmp_path: Path) -> None:
    example = tmp_path / "server.env.example"
    example.write_text(EXAMPLE, encoding="utf-8")
    env = {env_drift.EXAMPLE_ENV: str(example), "OMNIGENT_IMAGE_TAG": "x", "AE_INDENTED": " "}
    assert env_drift.missing(env) == ["AE_LIMITS_STATE_FILE", "AE_INDENTED"]


def test_missing_is_none_without_an_example(tmp_path: Path) -> None:
    assert env_drift.missing({env_drift.EXAMPLE_ENV: str(tmp_path / "absent")}) is None


def test_the_repo_example_parses_and_expects_the_oidc_keys() -> None:
    keys = env_drift.expected_keys(REPO_EXAMPLE.read_text(encoding="utf-8"))
    assert "OMNIGENT_IMAGE_TAG" in keys
    # The cutover finished 2026-10-20 and the markers are gone, so the check covers them.
    assert "OMNIGENT_AUTH_PROVIDER" in keys
    assert len([key for key in keys if key.startswith("OMNIGENT_OIDC_")]) == 7


def test_the_graph_url_is_reported_only_when_the_box_lacks_it() -> None:
    """AE_GRAPH_MCP_URL (2026-10-21) is expected, so the Deploys section names it if missing."""
    keys = env_drift.expected_keys(REPO_EXAMPLE.read_text(encoding="utf-8"))
    full = dict.fromkeys(keys, "x") | {env_drift.EXAMPLE_ENV: str(REPO_EXAMPLE)}
    assert env_drift.missing(full) == []
    without = {key: value for key, value in full.items() if key != "AE_GRAPH_MCP_URL"}
    assert env_drift.missing(without) == ["AE_GRAPH_MCP_URL"]
