"""Every local link and image in README.md and docs/features resolves (2026-10-22).

The feature pages cite core patches by file name (patches/NNNN-<slug>.patch) and
show media from docs/media. A patch renamed or renumbered by an upstream sync or
the rebase bot, or a clip dropped from docs/media, would otherwise break those
pages without any check noticing. Links to other sites and in-page anchors are
not followed.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PAGES = [ROOT / "README.md", *sorted((ROOT / "docs" / "features").glob("*.md"))]

# Markdown links and images, and the src, href and srcset attributes of the HTML
# the README's showcase and the pages use for pictures and the grid.
TARGET = re.compile(r"\]\(([^)\s]+)\)|(?:src|href|srcset)=\"([^\"]+)\"")
SKIP = ("http://", "https://", "mailto:", "#")


def local_targets(page: Path) -> list[str]:
    text = page.read_text()
    found = []
    for match in TARGET.finditer(text):
        target = match.group(1) or match.group(2)
        if target.startswith(SKIP):
            continue
        found.append(target.split("#", 1)[0])
    return found


def test_the_feature_pages_exist() -> None:
    assert len(PAGES) > 1, "docs/features has no pages; the README's showcase links to them"


@pytest.mark.parametrize("page", PAGES, ids=lambda p: str(p.relative_to(ROOT)))
def test_every_local_link_resolves(page: Path) -> None:
    missing = [t for t in local_targets(page) if not (page.parent / t).exists()]
    assert not missing, f"{page.relative_to(ROOT)} links to files that do not exist: {missing}"


def test_every_media_file_is_used() -> None:
    used = {(page.parent / t).resolve() for page in PAGES for t in local_targets(page)}
    unused = [
        p.name for p in sorted((ROOT / "docs" / "media").glob("*")) if p.resolve() not in used
    ]
    assert not unused, f"docs/media holds files no page shows; remove them: {unused}"
