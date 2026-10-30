"""The manifest entry point for the Parking Lot extension.

Loaded by ``omnigent.extensions.registry`` through the
``[project.entry-points."omnigent.extensions"]`` table; returns only
declarative values, per ``docs/extending/extension_manifest.md``. Nothing here
imports the runtime or the UI.
"""

from __future__ import annotations

from omnigent.extensions import (
    EXTENSION_API_VERSION,
    ExtensionEntrypoints,
    ExtensionManifest,
    ExtensionPermission,
    PageContribution,
    PrimaryNavigationContribution,
)

from ae_omni_extension import __version__

EXTENSION_ID = "analyticendeavors.parking-lot"
PAGE_ID = f"{EXTENSION_ID}.lot"
# The Dashboard's route, and the one the sidebar's Slots pill links to (web/src/lib/aeLabels.ts,
# AE_PARKING_LOT_PATH): /extensions/analyticendeavors.parking-lot/lot.
PAGE_ROUTE = "lot"
# Your move (2026-09-24) was a page of its own. Since the Dashboard merge the
# same day (docs/design/dashboard-review.md) it is the Dashboard's first
# section; the page stays, without a nav row, so old bookmarks land on the
# Dashboard.
YOUR_MOVE_PAGE_ID = f"{EXTENSION_ID}.your-move"
YOUR_MOVE_ROUTE = "your-move"
# Admin Center (2026-10-16 as Fabric Admin; renamed 2026-10-18): the Fabric
# capacities' Wake and Off buttons (aetraining first, room for more), on a
# page of its own with the extension's one nav row. The owner wanted it inside
# omnigent-ae next to the Dashboard; the row sits in upstream's extension slot
# between Inbox and Usage. The Dashboard keeps patch P7's sidebar row and no nav row here.
FABRIC_PAGE_ID = f"{EXTENSION_ID}.fabric"
FABRIC_ROUTE = "fabric"
FABRIC_NAV_ID = f"{EXTENSION_ID}.fabric-nav"
# One of the names upstream's ExtensionPrimaryNavigation maps to a lucide
# icon (web/src/extensions/ExtensionPrimaryNavigation.tsx); an unknown name
# falls back to a puzzle piece. Gauge reads as capacity.
FABRIC_NAV_ICON = "gauge"
# Moves with the pin (docs/UPSTREAM-SYNC.md, step 4), together with the
# `omnigent` dependency in pyproject.toml.
REQUIRES_OMNIGENT = ">=0.18,<0.20"


def get_manifest() -> ExtensionManifest:
    """The declarative contribution: the Dashboard (two routes to one page) and the Admin Center.

    The owner found "Your move", "Parking Lot" and "Slots" three entries for one
    feature (2026-09-24). The Dashboard's one entry is patch P7's "Dashboard
    n/3" row in the sidebar, which links to ``PAGE_ROUTE``. The Admin Center
    (2026-10-16, first called Fabric Admin) is a separate page with the
    extension's only nav row.
    """
    return ExtensionManifest(
        id=EXTENSION_ID,
        display_name="Parking Lot",
        distribution="omnigent-ae-extension",
        version=__version__,
        requires_omnigent=REQUIRES_OMNIGENT,
        extension_api=EXTENSION_API_VERSION,
        entrypoints=ExtensionEntrypoints(
            browser="dist/extension.js",
            browser_css="dist/extension.css",
        ),
        permissions=frozenset(
            {
                ExtensionPermission.NAVIGATION,
                ExtensionPermission.SESSIONS_READ,
                ExtensionPermission.STORAGE_USER,
                ExtensionPermission.SERVER_REQUEST,
            }
        ),
        activation_events=(
            f"onPage:{PAGE_ID}",
            f"onPage:{YOUR_MOVE_PAGE_ID}",
            f"onPage:{FABRIC_PAGE_ID}",
        ),
        pages=(
            PageContribution(
                id=YOUR_MOVE_PAGE_ID,
                title="Session Dashboard",
                route=YOUR_MOVE_ROUTE,
                view="your-move",
            ),
            PageContribution(
                id=PAGE_ID,
                title="Session Dashboard",
                route=PAGE_ROUTE,
                view="lot",
            ),
            PageContribution(
                id=FABRIC_PAGE_ID,
                title="Admin Center",
                route=FABRIC_ROUTE,
                view="fabric",
            ),
        ),
        # The Dashboard has no nav row here since patch P7 (2026-09-24): the
        # sidebar's "Dashboard n/3" row links to it and carries the slot
        # count, which a contribution's static label cannot. The Admin Center's
        # row (2026-10-16) is the only one.
        primary_navigation=(
            PrimaryNavigationContribution(
                id=FABRIC_NAV_ID,
                label="Admin Center",
                page=FABRIC_PAGE_ID,
                icon=FABRIC_NAV_ICON,
            ),
        ),
    )


__all__ = [
    "EXTENSION_ID",
    "FABRIC_NAV_ID",
    "FABRIC_PAGE_ID",
    "FABRIC_ROUTE",
    "PAGE_ID",
    "PAGE_ROUTE",
    "REQUIRES_OMNIGENT",
    "YOUR_MOVE_PAGE_ID",
    "YOUR_MOVE_ROUTE",
    "get_manifest",
]
