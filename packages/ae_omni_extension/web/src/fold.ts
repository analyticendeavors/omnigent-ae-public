// The one-line disclosure the Spend card's panels share (2026-10-30): the insight callouts, the
// review's suggestions, the preset table and the families list are each closed to a single line by
// default, so a tab is a few hundred pixels tall instead of a screenful. Each fold's state is
// remembered per viewer; a poll rebuilds the body, so the state is read here on every draw.

import { el, type Child } from "./dom";
import { chevronIcon } from "./icons";

export const FOLD_KEY = "presets.fold.";
const foldMemory = new Map<string, boolean>();

export function loadFold(id: string): boolean {
  try {
    const raw = window.localStorage.getItem(FOLD_KEY + id);
    if (raw === "open") return true;
    if (raw === "closed") return false;
  } catch {
    // No storage in this frame: the state kept in memory.
  }
  return foldMemory.get(id) ?? false;
}

/** What the viewer left *id* as, or null when they never opened or closed it (the Needs you panel). */
export function rememberedFold(id: string): boolean | null {
  try {
    const raw = window.localStorage.getItem(FOLD_KEY + id);
    if (raw === "open") return true;
    if (raw === "closed") return false;
  } catch {
    // No storage in this frame: the state kept in memory.
  }
  return foldMemory.get(id) ?? null;
}

/** Forget the in-memory states (a deactivated page and the tests). */
export function resetFolds(): void {
  foldMemory.clear();
}

export function saveFold(id: string, open: boolean): void {
  foldMemory.set(id, open);
  try {
    window.localStorage.setItem(FOLD_KEY + id, open ? "open" : "closed");
  } catch {
    // Kept in memory until the page closes.
  }
}

let foldCounter = 0;

export interface FoldOptions {
  /**
   * Show the fold open whatever the viewer left it as, without saving that (the families list while
   * a filter is typed). A click does nothing then; clearing the filter returns to the saved state.
   */
  forceOpen?: boolean;
}

/** A one-line disclosure: a button with *summary*, and *content* shown while it is open. */
export function fold(
  id: string,
  extraClass: string,
  summary: Child[],
  content: HTMLElement,
  options: FoldOptions = {},
): HTMLElement {
  const bodyId = `presets-fold-${++foldCounter}`;
  const forced = Boolean(options.forceOpen);
  const open = forced || loadFold(id);
  const head = el(
    "button",
    {
      type: "button",
      class: "presets-fold-head",
      "data-fold": id,
      "aria-expanded": String(open),
      "aria-controls": bodyId,
    },
    el("span", { class: "presets-fold-text" }, ...summary),
    chevronIcon(),
  );
  const body = el("div", { class: "presets-fold-body", id: bodyId, hidden: !open }, content);
  const wrap = el(
    "div",
    { class: `presets-fold ${extraClass}${open ? " is-open" : ""}` },
    head,
    body,
  );
  head.addEventListener("click", () => {
    if (forced) return;
    const next = head.getAttribute("aria-expanded") !== "true";
    saveFold(id, next);
    head.setAttribute("aria-expanded", String(next));
    body.hidden = !next;
    wrap.classList.toggle("is-open", next);
  });
  return wrap;
}

/** A fold line of "<lead> <name> <tail>" whose name takes the spare room and ends in an ellipsis, so the line stays one line. */
export function foldLine(lead: string, name: string, tail: string): HTMLElement {
  return el(
    "span",
    { class: "presets-fold-line" },
    // The spaces are for the text a screen reader or a copy gets; the flex gap spaces the line.
    lead ? el("span", { class: "presets-fold-lead" }, lead) : null,
    lead ? " " : null,
    el("span", { class: "presets-fold-name", title: name }, name),
    tail ? " " : null,
    tail ? el("span", { class: "presets-fold-tail" }, tail) : null,
  );
}
