// Open gates: the four answers to a session whose `ae.gate` is `open`, with
// the FORK clause the observer captured (`ae.fork`). Since the Dashboard
// merge (2026-09-24) the cards live in the Your move section (yourMove.ts);
// this module keeps the answers. A button posts the
// letter as the session's next user turn through `POST /v1/ae/gate/{id}/reply`,
// which answers 409 unless the gate is open. The observer that sets `open`
// (ae_omni_policies/observer.py, through patch P3) landed with Phase 4 on
// 2026-09-23; a stale button after the gate was answered elsewhere shows the
// 409 text.
//
// Labels (2026-09-27): the buttons read "A", "B", "C" and "Close" until Reid
// asked for each to say what it does, so they now read as plain actions. Only
// the visible label changed: the route, and so the letter posted, is the same,
// and the tooltip names the letter so it still matches the gate's RECOMMEND
// line. The card does not mark the recommended button: the observer captures
// only the FORK clause (`ae.fork`), not RECOMMEND.

import type { GateRoute } from "./api";

export const GATE_ROUTES: [GateRoute, string, string][] = [
  ["A", "Do it now", "A, act now: absorb it into this thread"],
  ["B", "Write a note", "B, handoff note here, then back to the task"],
  ["C", "Save for later", "C, file it to the Parking Lot"],
  ["close", "Drop it", "Close: neither urgent nor important, so drop it"],
];
