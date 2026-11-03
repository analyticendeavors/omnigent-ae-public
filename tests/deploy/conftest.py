"""Markers for tests/deploy.

``docker``: the test runs the docker CLI against a daemon (or needs the docker
compose plugin) at runtime. The CI box runner user has no docker access on
purpose, so the python job deselects these there (``-m "not docker"``) and the
image job runs them on a GitHub-hosted runner
(``-m docker``). On a GitHub-hosted runner nothing is deselected.
"""

from __future__ import annotations

import pytest

# Fixtures in test_host_config_sync.py that start a throwaway container: any test
# using one needs docker too. (test_auto_update.py has an unrelated fixture named box.)
_DOCKER_FIXTURES = frozenset({"host_image", "box", "box_real_claude", "box_no_jq"})


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "docker: runs docker (or docker compose) at runtime; see tests/deploy/conftest.py",
    )


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    for item in items:
        in_sync_tests = item.path.name == "test_host_config_sync.py"
        if in_sync_tests and _DOCKER_FIXTURES & set(getattr(item, "fixturenames", ())):
            item.add_marker(pytest.mark.docker)
