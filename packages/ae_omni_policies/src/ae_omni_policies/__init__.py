"""Analytic Endeavors' branch-discipline layer for OmniGent.

Loaded by the server through two config keys (config/server.yaml):

- ``policy_modules: [ae_omni_policies]`` scans :data:`POLICY_REGISTRY` so the
  slot cap and the hurry modes appear in ``GET /v1/policy-registry`` and can be
  added on Settings > Global Policies, which owns them since 2026-10-18
  (``ae_seed_policies`` seeds a new server; ``policy_seed.py``).
- ``debug_router_modules: [ae_omni_policies.router]`` mounts the ``/v1/ae``
  routes (health, capacity, the Parking Lot, gate replies).

Importing the package also registers the observer (``observer.py``) with
upstream's item-observers registry, patch P3 of the fork; on an upstream
without the patch the import still succeeds and the observer falls back to
scanning at the next prompt. It registers scheduled-run retention
(``retention.py``) the same way, with patch P29's fire observers; without that
patch no run session is ever pruned. Auto-resume (``auto_resume.py``) registers
with patch P34's host-connect observers; without it an interrupted session waits
for Resume. Everything else runs on seams OmniGent ships.
"""

from __future__ import annotations

from ae_omni_policies import auto_resume as _auto_resume
from ae_omni_policies import observer as _observer
from ae_omni_policies import retention as _retention
from ae_omni_policies.registry import POLICY_REGISTRY

__version__ = "0.1.0"
#: The User-Agent every outbound HTTP client sends. It names the product and
#: version only: no repository link, so a fork sends nothing about this one.
USER_AGENT = f"omnigent-ae/{__version__}"

#: Whether the observer is registered with upstream (patch P3 present).
OBSERVER_REGISTERED = _observer.REGISTERED
#: Whether scheduled-run retention is registered with upstream (patch P29 present).
RETENTION_REGISTERED = _retention.REGISTERED
#: Whether auto-resume after a host restart is registered (patch P34 present).
AUTO_RESUME_REGISTERED = _auto_resume.REGISTERED

__all__ = [
    "AUTO_RESUME_REGISTERED",
    "OBSERVER_REGISTERED",
    "POLICY_REGISTRY",
    "RETENTION_REGISTERED",
    "USER_AGENT",
    "__version__",
]
