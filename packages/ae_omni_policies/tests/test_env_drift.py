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


def test_the_repo_example_expects_its_required_block_only() -> None:
    keys = env_drift.expected_keys(REPO_EXAMPLE.read_text(encoding="utf-8"))
    for key in ("AE_IMAGE", "OMNIGENT_IMAGE_TAG", "OMNIGENT_ACCOUNTS_BASE_URL"):
        assert key in keys
    # Since 2026-10-22 the example ships accounts mode with the OIDC block and
    # AE_GRAPH_MCP_URL commented (README.md "Quick start"), so a server that does
    # not use them is never told it lacks them.
    assert "OMNIGENT_AUTH_PROVIDER" not in keys
    assert not [key for key in keys if key.startswith("OMNIGENT_OIDC_")]
    assert "AE_GRAPH_MCP_URL" not in keys


def test_a_missing_required_key_is_reported() -> None:
    keys = env_drift.expected_keys(REPO_EXAMPLE.read_text(encoding="utf-8"))
    full = dict.fromkeys(keys, "x") | {env_drift.EXAMPLE_ENV: str(REPO_EXAMPLE)}
    assert env_drift.missing(full) == []
    without = {key: value for key, value in full.items() if key != "AE_IMAGE"}
    assert env_drift.missing(without) == ["AE_IMAGE"]
