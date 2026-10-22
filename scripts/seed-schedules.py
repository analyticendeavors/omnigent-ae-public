#!/usr/bin/env python3
"""Seed the AE steward and shutdown-note scheduled tasks on an OmniGent server.

Run as ``just schedules-seed <server-url>`` (which uses upstream's venv, where
the AE package is installed). The logic is ``ae_omni_policies.schedules`` so
the test suite can run it against the app in process; this file only parses
the command line. Written 2026-09-23 (Phase 5).
"""

from __future__ import annotations

import sys

from ae_omni_policies.schedules import main

if __name__ == "__main__":
    sys.exit(main())
