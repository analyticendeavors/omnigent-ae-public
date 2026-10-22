"""The Parking Lot page: Analytic Endeavors' branch-discipline view for OmniGent.

An OmniGent V1 browser extension (``docs/extending/extensions.md`` upstream).
The Python side is one declarative manifest (:mod:`ae_omni_extension.plugin`);
the page itself is the bundle in ``dist/``, built from ``web/`` and committed.
It talks to the server only through the host RPC that patch P2 adds
(``server.request``, ``sessions.listAll`` with ``ae.*`` labels), and task-store
stays the system of record: the page is a view over it.
"""

from __future__ import annotations

# Kept equal to pyproject.toml's project.version and the manifest's version;
# upstream's conformance check compares all three.
__version__ = "0.4.3"

__all__ = ["__version__"]
