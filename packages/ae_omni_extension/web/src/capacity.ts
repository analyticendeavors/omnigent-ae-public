// Capacity: the Working section carries the cards (icon, elapsed, NEXT, and
// the status buttons that write `ae.status` through the labels PATCH); the
// slot count itself is in the Working heading and the header (header.ts). Since
// 2026-10-14 Park and those moves sit in each row's "..." menu (cards.ts),
// and Your move's card for a session it took out of Working or Blocked
// carries the same menu (slotMoves; review, 2026-10-14).
//
// Header rethink (2026-09-25, https://github.com/analyticendeavors/omnigent-ae/issues/28):
// the sticky strip's Slots pill and its chip per working session are gone.
// The sidebar already lists every session with its status and "Dashboard
// n/3", and the Working section lists the same sessions again one inch
// lower, so the strip showed everything twice.
//
// Dashboard merge (2026-09-24, docs/design/dashboard-review.md): Working
// holds only the sessions that hold a slot and its header reads n/3, the same
// number as the strip; Blocked (working but waiting on the human, or set by
// hand) is its own section and holds no slot. A working session with an open
// gate or an unfiled note carries a chip for it instead of a second card in
// another section. Park opens the park form with the session chosen, so a
// park from here always leaves a note; Done is now called Review, the status
// it writes.

import type { ExtensionContext } from "@omnigent/extension-sdk";
import {
  type AeStatus,
  type Capacity,
  type CapacitySession,
  countOf,
  describeError,
  formatElapsed,
  label,
  SLOT_LIMIT,
  type Session,
  startedAt,
  statusOf,
  titleOf,
} from "./api";
import { moveButton, sessionRow } from "./cards";
import { body, button, el, empty, inYourMoveNote, notice, replaceChildren, section } from "./dom";
import { statusIcon } from "./icons";

export interface CapacityView {
  capacity: Capacity | null;
  capacityError: unknown;
  sessions: Session[];
  /** Sessions Your move shows (2026-10-14): drawn there, not again here; counts keep them. */
  inYourMove?: ReadonlySet<string>;
}

interface Deps {
  context: ExtensionContext;
  refresh: () => Promise<void>;
  /** Open the park form with this session chosen (lot.ts). */
  parkSession?: (sessionId: string) => void;
}

/**
 * Working rows from `/v1/ae/capacity` when it answered (it knows who is
 * waiting on an approval), else derived from the session labels alone.
 */
export function workingRows(view: CapacityView): {
  working: CapacitySession[];
  blocked: CapacitySession[];
} {
  if (view.capacity) return { working: view.capacity.working, blocked: view.capacity.blocked };
  const now = Date.now() / 1000;
  const working = view.sessions
    .filter((session) => statusOf(session) === "working")
    .map((session) => fromSession(session, now))
    .sort((left, right) => left.started - right.started);
  const blocked = view.sessions
    .filter((session) => statusOf(session) === "blocked")
    .map((session) => fromSession(session, now));
  return { working, blocked };
}

function fromSession(session: Session, now: number): CapacitySession {
  const started = startedAt(session);
  return {
    id: session.id,
    title: titleOf(session),
    status: statusOf(session),
    live_status: session.status,
    blocked: statusOf(session) === "blocked",
    started,
    elapsed_s: Math.max(0, Math.floor(now - started)),
    elapsed: formatElapsed(now - started),
    next: label(session, "ae.next"),
    gate: label(session, "ae.gate"),
    fork: label(session, "ae.fork"),
    overflow: label(session, "ae.overflow") === "1",
    task: label(session, "ae.task"),
  };
}

/** The "..." menu items of a Working (*shown* "working") or Blocked row. */
function movesOf(
  deps: Deps,
  row: { id: string; title: string },
  shown: AeStatus,
): HTMLButtonElement[] {
  const move = (text: string, status: AeStatus, title: string) =>
    moveButton(deps, row, text, status, title);
  if (shown !== "working") {
    return [move("Move to Working", "working", "Set ae.status back to working")];
  }
  const parkSession = deps.parkSession;
  // Park opens the park form rather than writing a status; it leads the
  // row's "..." menu (2026-10-14, second pass; beside Open until then).
  return [
    parkSession
      ? button("Park", {
          title: "File a note to the Parking Lot for this session; filing marks it parked",
          onClick: () => parkSession(row.id),
        })
      : move("Park", "parked", "Set ae.status to parked"),
    move("Move to Review", "review", "Done: set ae.status to review"),
    move("Move to Blocked", "blocked", "Set ae.status to blocked"),
  ];
}

/**
 * The menu items Working or Blocked would give *row*, for a card drawn in
 * their place (2026-10-14, review): they leave out what Your move shows, so
 * its card offers Park and the moves. None when neither lists the session.
 */
export function slotMoves(
  view: CapacityView,
  deps: Deps,
  row: { id: string; title: string },
): HTMLButtonElement[] {
  const { working, blocked } = workingRows(view);
  if (working.some((at) => at.id === row.id)) return movesOf(deps, row, "working");
  if (blocked.some((at) => at.id === row.id)) return movesOf(deps, row, "blocked");
  return [];
}

function sessionCard(
  deps: Deps,
  row: CapacitySession,
  shown: AeStatus,
  session: Session | undefined,
): HTMLElement {
  const unfiled = session ? countOf(session, "ae.unfiled") : 0;
  const moves = movesOf(deps, row, shown);
  return sessionRow(deps, {
    id: row.id,
    title: row.title,
    kind: shown,
    icon: statusIcon(shown),
    tags: [
      row.gate === "open" ? el("span", { class: "tag tag-gate" }, "gate open") : null,
      unfiled > 0 ? el("span", { class: "tag tag-unfiled" }, `${unfiled} unfiled`) : null,
    ],
    meta: row.elapsed,
    next: row.next,
    notes: [row.overflow ? el("p", { class: "meta" }, "riding unparked") : null],
    moves,
  });
}

/** Slots used and the limit: the server's count when it answered, else the labels'. */
export function slotsOf(view: CapacityView): { used: number; limit: number } {
  const { working } = workingRows(view);
  return {
    used: view.capacity?.slots_used ?? working.length,
    limit: view.capacity?.limit ?? SLOT_LIMIT,
  };
}

export function renderCapacity(
  hosts: { working: HTMLElement; blocked: HTMLElement },
  view: CapacityView,
  deps: Deps,
): void {
  const { working, blocked } = workingRows(view);
  const { used, limit } = slotsOf(view);
  // Each item once (2026-10-14): a session Your move shows is not drawn
  // again; the heading keeps the true count and says where it went.
  const skip = view.inYourMove;
  const drawn = (rows: CapacitySession[]) => (skip ? rows.filter((row) => !skip.has(row.id)) : rows);
  const workingShown = drawn(working);
  const blockedShown = drawn(blocked);

  const byId = new Map(view.sessions.map((session) => [session.id, session]));
  const node = section(
    "working",
    "Working",
    `${used}/${limit}`,
    inYourMoveNote(working.length - workingShown.length, working.length),
  );
  const target = body(node);
  if (view.capacityError && !view.capacity) {
    target.appendChild(
      notice(
        `Capacity route unavailable: ${describeError(view.capacityError)}. Showing session labels only.`,
        "info",
      ),
    );
  }
  const list = el("div", { class: "cards" });
  for (const row of workingShown)
    list.appendChild(sessionCard(deps, row, "working", byId.get(row.id)));
  if (working.length === 0) list.appendChild(empty("No working sessions. Every slot is free."));
  target.appendChild(list);
  replaceChildren(hosts.working, [node]);

  const blockedNode = section(
    "blocked",
    "Blocked",
    blocked.length,
    inYourMoveNote(blocked.length - blockedShown.length, blocked.length),
  );
  const blockedList = el("div", { class: "cards" });
  for (const row of blockedShown)
    blockedList.appendChild(sessionCard(deps, row, "blocked", byId.get(row.id)));
  body(blockedNode).appendChild(blockedList);
  replaceChildren(hosts.blocked, [blockedNode]);
}
