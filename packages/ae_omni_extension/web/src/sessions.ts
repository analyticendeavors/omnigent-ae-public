// The label-driven session lists: Review (Review on a working card sent it
// here), Needs a note, Unsorted and Reference. Each row's title opens its
// session; its status moves sit in the row's "..." menu (cards.ts, 2026-10-14).
//
// Dashboard merge (2026-09-24, docs/design/dashboard-review.md): "Parked
// sessions" and "Unfiled" are gone as lists. A parked session with a note
// shows on its note's card in the Parking Lot (lot.ts); the ones that break
// the rule that everything parked has a resumable note land in Needs a note,
// together with sessions whose agent could not file a note (`ae.unfiled`).
// Unsorted is sessions with no `ae.status` at all (imports, never prompted),
// which used to appear nowhere. Reference is one line per session.

import type { ExtensionContext } from "@omnigent/extension-sdk";
import { type AeStatus, countOf, label, type Session, statusOf, titleOf } from "./api";
import { moveButton, rowMenu, sessionRow } from "./cards";
import { body, button, el, replaceChildren, section } from "./dom";
import { statusIcon, unsortedIcon } from "./icons";

interface Deps {
  context: ExtensionContext;
  refresh: () => Promise<void>;
}

interface Move {
  text: string;
  status: AeStatus;
  title: string;
}

function list(
  deps: Deps,
  id: string,
  heading: string,
  sessions: Session[],
  shown: AeStatus | null,
  moves: Move[],
  extra?: (session: Session) => string | undefined,
  action?: (session: Session) => HTMLButtonElement | null,
): HTMLElement {
  const node = section(id, heading, sessions.length);
  const target = body(node);
  const cards = el("div", { class: "cards" });
  for (const session of sessions) {
    const status = shown ?? statusOf(session);
    const more = action?.(session);
    const named = { id: session.id, title: titleOf(session) };
    cards.appendChild(
      sessionRow(deps, {
        ...named,
        kind: status ?? "unsorted",
        icon: status ? statusIcon(status) : unsortedIcon(),
        meta: extra?.(session),
        next: label(session, "ae.next"),
        actions: more ? [more] : [],
        moves: moves.map((move) => moveButton(deps, named, move.text, move.status, move.title)),
      }),
    );
  }
  target.appendChild(cards);
  return node;
}

const TO_WORKING: Move = {
  text: "Move to Working",
  status: "working",
  title: "Set ae.status back to working (takes a slot on the next prompt)",
};
const TO_REVIEW: Move = {
  text: "Move to Review",
  status: "review",
  title: "Set ae.status to review",
};
const TO_REFERENCE: Move = {
  text: "Move to Reference",
  status: "reference",
  title: "Close it: set ae.status to reference",
};

export function renderReview(host: HTMLElement, sessions: Session[], deps: Deps): void {
  const rows = sessions.filter((session) => statusOf(session) === "review");
  replaceChildren(host, [
    list(deps, "review", "Review", rows, "review", [TO_WORKING, TO_REFERENCE]),
  ]);
}

/** Why a session needs a note, or null when it does not. */
export function needsNote(session: Session, openTasks: ReadonlySet<string> | null): string | null {
  const reasons: string[] = [];
  if (statusOf(session) === "parked") {
    const task = label(session, "ae.task");
    if (!task) reasons.push("parked without a note");
    else if (openTasks && !openTasks.has(task)) reasons.push(`task ${task} is not open in the lot`);
  }
  const unfiled = countOf(session, "ae.unfiled");
  if (unfiled > 0) reasons.push(`${unfiled} unfiled note${unfiled === 1 ? "" : "s"}`);
  return reasons.length > 0 ? reasons.join("; ") : null;
}

export function renderNeedsNote(
  host: HTMLElement,
  sessions: Session[],
  openTasks: ReadonlySet<string> | null,
  deps: Deps & { parkSession?: (sessionId: string) => void },
): void {
  const rows = sessions.filter((session) => needsNote(session, openTasks) !== null);
  const parkSession = deps.parkSession;
  replaceChildren(host, [
    list(
      deps,
      "needs-note",
      "Needs a note",
      rows,
      null,
      [],
      (session) => needsNote(session, openTasks) ?? undefined,
      (session) =>
        parkSession && !label(session, "ae.task")
          ? button("File a note", {
              kind: "primary",
              title: "Open the park form with this session chosen",
              onClick: () => parkSession(session.id),
            })
          : null,
    ),
  ]);
}

export function renderUnsorted(host: HTMLElement, sessions: Session[], deps: Deps): void {
  const rows = sessions.filter((session) => statusOf(session) === null);
  replaceChildren(host, [
    list(deps, "unsorted", "Unsorted", rows, null, [TO_WORKING, TO_REVIEW, TO_REFERENCE]),
  ]);
}

/** Reference: one line per session, the title opens it (lookup only). */
export function renderReference(host: HTMLElement, sessions: Session[], deps: Deps): void {
  const rows = sessions.filter((session) => statusOf(session) === "reference");
  const node = section("reference", "Reference", rows.length);
  const lines = el("ul", { class: "lines" });
  for (const session of rows) {
    const open = button("", {
      title: "Open the session",
      onClick: () => deps.context.navigation.openSession(session.id),
    });
    open.classList.add("line");
    open.append(statusIcon("reference"), el("span", { class: "line-title" }, titleOf(session)));
    // Reopen: Reference is closed, and the dashboard could not undo it (2026-10-23).
    const named = { id: session.id, title: titleOf(session) };
    lines.appendChild(
      el(
        "li",
        {},
        open,
        rowMenu(named, [
          moveButton(deps, named, TO_WORKING.text, TO_WORKING.status, TO_WORKING.title),
          moveButton(deps, named, TO_REVIEW.text, TO_REVIEW.status, TO_REVIEW.title),
        ]),
      ),
    );
  }
  body(node).appendChild(lines);
  replaceChildren(host, [node]);
}
