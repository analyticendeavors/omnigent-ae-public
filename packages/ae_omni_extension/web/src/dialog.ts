// A modal dialog, shared (2026-10-28, the Box health Details; presets.ts keeps
// its own copy for now, and moving it here is a separate change).
//
// It follows the WAI-ARIA "Dialog (Modal)" pattern: `role="dialog"`,
// `aria-modal="true"` and `aria-labelledby` pointing at the title; Tab and
// Shift+Tab stay inside; Escape closes it, handled in the capture phase so it
// does not also close something behind; a click on the backdrop closes it;
// focus starts on Close and goes back to the control that opened it.
//
// On a phone (520 px and narrower) the card is a full-width sheet with a
// scrolling body (style.css .ae-dialog-card).

import { el } from "./dom";
import { closeIcon } from "./icons";
import { closeOpenHint, hasOpenHint } from "./hint";

export interface DialogOptions {
  title: string;
  body: Node;
  /** The control that opened it; focus returns here on close. */
  opener: HTMLElement | null;
  /** An id prefix, unique among dialogs that can be open together. */
  id: string;
  /** The Close button's name for a screen reader. */
  closeLabel?: string;
  /** The Close button's tooltip. */
  closeTitle?: string;
  /** Extra class on the card, for a caller's own sizing. */
  className?: string;
  /** Called after the dialog closes, whichever way. */
  onClose?: () => void;
}

export interface OpenDialog {
  close: () => void;
  node: HTMLElement;
  closeButton: HTMLButtonElement;
}

const FOCUSABLE = "button, [href], input, select, textarea, [tabindex]";

export function openDialog(options: DialogOptions): OpenDialog {
  const titleId = `${options.id}-title`;
  const closeButton = el("button", {
    type: "button",
    class: "ae-dialog-close btn btn-quiet",
    "aria-label": options.closeLabel ?? "Close",
    title: options.closeTitle,
  });
  closeButton.appendChild(closeIcon());
  const card = el(
    "div",
    {
      class: `ae-dialog-card${options.className ? ` ${options.className}` : ""}`,
      role: "dialog",
      "aria-modal": "true",
      "aria-labelledby": titleId,
    },
    el("div", { class: "ae-dialog-top" }, el("h3", { id: titleId }, options.title), closeButton),
    el("div", { class: "ae-dialog-body" }, options.body),
  );
  const backdrop = el("div", { class: "ae-dialog" }, card);
  let closed = false;
  const items = () =>
    [...card.querySelectorAll<HTMLElement>(FOCUSABLE)].filter(
      (item) =>
        !item.hasAttribute("disabled") &&
        item.getAttribute("tabindex") !== "-1" &&
        !item.closest("[hidden]"),
    );
  const onKey = (event: KeyboardEvent) => {
    if (event.key === "Escape") {
      // An open hint takes this Escape first (its own listener, registered later, closes
      // it); the next one closes the dialog.
      if (hasOpenHint()) return;
      event.preventDefault();
      event.stopPropagation();
      event.stopImmediatePropagation();
      close();
    } else if (event.key === "Tab") {
      const list = items();
      if (!list.length) return;
      const first = list[0];
      const last = list[list.length - 1];
      const active = document.activeElement;
      if (event.shiftKey && (active === first || !card.contains(active))) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && (active === last || !card.contains(active))) {
        event.preventDefault();
        first.focus();
      }
    }
  };
  function close(): void {
    if (closed) return;
    closed = true;
    closeOpenHint();
    document.removeEventListener("keydown", onKey, true);
    backdrop.remove();
    if (options.opener?.isConnected) options.opener.focus();
    options.onClose?.();
  }
  document.addEventListener("keydown", onKey, true);
  backdrop.addEventListener("click", (event) => {
    if (event.target === backdrop) close();
  });
  closeButton.addEventListener("click", close);
  document.body.appendChild(backdrop);
  closeButton.focus();
  return { close, node: backdrop, closeButton };
}
