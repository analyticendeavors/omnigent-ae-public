#!/usr/bin/env python3
"""Save the Triage card's notes for lines counted before the observer saved them.

Run as ``just triage-notes-backfill <server-url>`` (which uses upstream's venv,
where the AE package is installed), once after the deploy that carries the
observer's saved notes. The logic is ``ae_omni_policies.triage_notes`` so the
test suite can run it against a fake server; this file only parses the command
line. Written 2026-10-19.
"""

from __future__ import annotations

import sys

from ae_omni_policies.triage_notes import main

if __name__ == "__main__":
    sys.exit(main())
