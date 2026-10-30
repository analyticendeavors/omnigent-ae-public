// A small info button that explains a setting or a control in one line
// (2026-10-28, the Box health monitor).
//
// Why: every setting ships with helper text that adds no height to its row.
// Upstream's React UI has the same thing (patch 0196, `AeHint`); the extension
// is plain TypeScript, so this copies the behaviour: the text opens on keyboard
// focus, on hover and on a tap (a click toggles it), closes on Escape, blur, a
// tap outside or the pointer leaving, and is wired to the button with
// `aria-describedby` on a `role="tooltip"` span. The tip floats over the page
// (position: fixed, placed from the button's rectangle and kept inside the
// viewport) so a dialog's scroll area never clips it and the row does not grow.
//
// HINT_MAX is the longest text a hint may carry; boxHealth.test.ts and
// hint.test.ts fail on a longer one, the same rule the server's settings table
// holds (box_health.py).

import { el } from "./dom";
import { infoIcon } from "./icons";

export const HINT_MAX = 140;

let counter = 0;
/** The tip that is open now: one at a time. */
let open: { close: () => void } | null = null;

/** Whether a tip is open: a dialog leaves Escape to it first. */
export function hasOpenHint(): boolean {
  return open !== null;
}

/** Close the open tip, if any (a dialog does this as it closes, so no listener is left behind). */
export function closeOpenHint(): void {
  open?.close();
}

/**
 * The info control for *text*, named for a screen reader by *label* ("About
 * Stale minutes"). The returned node holds the button (marked `data-hint` with
 * the text) and the tip.
 */
export function hint(text: string, label: string): HTMLElement {
  const id = `ae-hint-${++counter}`;
  const button = el("button", {
    type: "button",
    class: "ae-hint-btn",
    "aria-label": `About ${label}`,
    "aria-describedby": id,
    "aria-expanded": "false",
    "data-hint": text,
  });
  button.appendChild(infoIcon(14));
  const tip = el("span", { class: "ae-hint-tip", id, role: "tooltip", hidden: true }, text);
  const node = el("span", { class: "ae-hint" }, button, tip);

  let pinned = false;
  // Below the button when it fits; above it when it would run off the bottom of the
  // screen (a hint near the foot of a phone); clamped inside the viewport either way.
  // Measured after the tip is shown, since its height depends on its text and width.
  const place = () => {
    const rect = button.getBoundingClientRect();
    const width = Math.min(260, window.innerWidth - 16);
    tip.style.width = `${width}px`;
    const left = Math.max(
      8,
      Math.min(rect.left + rect.width / 2 - width / 2, window.innerWidth - width - 8),
    );
    const height = tip.getBoundingClientRect().height;
    let top = rect.bottom + 6;
    if (top + height > window.innerHeight - 8) top = rect.top - 6 - height;
    top = Math.max(8, Math.min(top, window.innerHeight - height - 8));
    tip.style.left = `${left}px`;
    tip.style.top = `${top}px`;
  };
  const show = () => {
    if (open && open.close !== close) open.close();
    open = { close };
    tip.hidden = false;
    button.setAttribute("aria-expanded", "true");
    place();
  };
  function close(): void {
    pinned = false;
    tip.hidden = true;
    button.setAttribute("aria-expanded", "false");
    document.removeEventListener("keydown", onKey, true);
    document.removeEventListener("pointerdown", onOutside, true);
    if (open?.close === close) open = null;
  }
  const onKey = (event: KeyboardEvent) => {
    if (event.key !== "Escape") return;
    // This Escape closes the tip and nothing behind it (a dialog listens too).
    event.preventDefault();
    event.stopPropagation();
    event.stopImmediatePropagation();
    close();
  };
  const onOutside = (event: Event) => {
    if (!node.contains(event.target as Node)) close();
  };
  const arm = () => {
    show();
    document.addEventListener("keydown", onKey, true);
    document.addEventListener("pointerdown", onOutside, true);
  };
  button.addEventListener("focus", arm);
  button.addEventListener("blur", close);
  button.addEventListener("mouseenter", () => {
    if (tip.hidden) arm();
  });
  button.addEventListener("mouseleave", () => {
    if (!pinned && document.activeElement !== button) close();
  });
  button.addEventListener("click", (event) => {
    event.stopPropagation();
    if (!tip.hidden && pinned) {
      close();
    } else {
      pinned = true;
      if (tip.hidden) arm();
    }
  });
  return node;
}
