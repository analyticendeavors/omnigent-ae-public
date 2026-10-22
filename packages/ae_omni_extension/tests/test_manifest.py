"""The manifest and the committed bundle pass upstream's own conformance check.

``check_extension_package`` (``docs/extending/extension_manifest.md``) validates
the manifest, compares it with ``pyproject.toml``, and resolves the browser
bundle from the package root the way the server does at startup. The pattern
is upstream's ``tests/extensions/test_hello_extension.py``.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from omnigent.extensions import ExtensionPermission, check_extension_package
from omnigent.extensions.assets import ASSET_SCRIPT, ASSET_STYLES, ExtensionAssetError

from ae_omni_extension import __version__
from ae_omni_extension.plugin import (
    EXTENSION_ID,
    FABRIC_PAGE_ID,
    PAGE_ID,
    YOUR_MOVE_PAGE_ID,
    get_manifest,
)

PROJECT_ROOT = Path(__file__).parents[1]
PACKAGE_ROOT = PROJECT_ROOT / "src" / "ae_omni_extension"


def test_manifest_matches_the_project_and_resolves_the_bundle() -> None:
    manifest = get_manifest()
    bundle = check_extension_package(
        manifest, project_root=PROJECT_ROOT, package_root=PACKAGE_ROOT
    )
    assert bundle is not None
    assert b"omnigent-extension" in bundle.assets[ASSET_SCRIPT].content
    assert bundle.assets[ASSET_STYLES].content.strip()
    # The bundle is one self-contained IIFE: nothing loads at runtime.
    assert b"import(" not in bundle.assets[ASSET_SCRIPT].content


def test_manifest_declares_the_page_the_sidebar_links_to() -> None:
    manifest = get_manifest()
    assert manifest.id == EXTENSION_ID
    assert manifest.version == __version__
    assert [page.route for page in manifest.pages] == ["your-move", "lot", "fabric"]
    assert [page.view for page in manifest.pages] == ["your-move", "lot", "fabric"]
    assert [page.title for page in manifest.pages] == [
        "Session Dashboard",
        "Session Dashboard",
        "Admin Center",
    ]
    assert manifest.activation_events == (
        f"onPage:{PAGE_ID}",
        f"onPage:{YOUR_MOVE_PAGE_ID}",
        f"onPage:{FABRIC_PAGE_ID}",
    )
    assert manifest.permissions == frozenset(
        {
            ExtensionPermission.NAVIGATION,
            ExtensionPermission.SESSIONS_READ,
            ExtensionPermission.STORAGE_USER,
            ExtensionPermission.SERVER_REQUEST,
        }
    )


def test_the_one_nav_row_is_admin_center() -> None:
    # The Dashboard has none since patch P7 (2026-09-24): the sidebar's
    # "Dashboard n/3" row links to the lot route and carries the count.
    # The Admin Center (2026-10-16 as Fabric Admin) gets the extension slot's row.
    (nav,) = get_manifest().primary_navigation
    assert nav.page == FABRIC_PAGE_ID
    assert nav.label == "Admin Center"
    # A name upstream's ExtensionPrimaryNavigation maps; others fall back to a puzzle.
    assert nav.icon == "gauge"


def test_conformance_rejects_a_drifted_distribution_name() -> None:
    with pytest.raises(ExtensionAssetError, match="does not match pyproject"):
        check_extension_package(
            replace(get_manifest(), distribution="wrong-distribution"),
            project_root=PROJECT_ROOT,
            package_root=PACKAGE_ROOT,
        )
