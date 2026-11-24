// The Admin Center pane (2026-11-18, PR 2 of the wall plan): a source or an area opens in the wall's place.
//
// Why: the approved option 3 opens a source beside the Needs you list instead of under its line, so the
// list never moves or reflows while the owner works through a source. At the wide band (1,400 px of
// container and up) a click on a line fills this pane; below it a line opens its rows in place, as before.
//
// This file only lays out and keeps focus. needsTable.ts builds what goes in it (`view`): the heading, the
// source's rows made by the table's own row code, the opened row's panel and the source's real card. The
// card is moved in, never copied: the wall is told it is lent (`lend`) so it does not take it back until the
// pane clears or shows another source. The pane is never saved: a reload starts with it closed.

import type { AttentionSource } from "./api";
import { setCardWall } from "./board";
import { cardCounts } from "./needsModel";
import { el } from "./dom";
import { closeIcon } from "./icons";
import type { DomainId } from "./tiles";

/** What a pane shows: an area (a heading per source) or one source. */
export type PaneTarget = { kind: "area"; id: DomainId } | { kind: "source"; id: AttentionSource };

export interface PaneSection {
  /** The source's name as a heading (an area's pane; null for a source's own pane, where the header names it). */
  name: string | null;
  pills: HTMLElement | null;
  review: HTMLElement | null;
  /** The rows (and "Show N more"), already built. */
  rows: HTMLElement;
}

export interface PaneView {
  heading: { icon: Element | null; name: string; pills: HTMLElement | null; review: HTMLElement | null };
  sections: PaneSection[];
  /** The opened row: its title with a Close, then its panel. */
  detail: HTMLElement | null;
  /** The source's own card (the real element) with its label, or null for an area or a source with no card. */
  card: { id: string; node: HTMLElement; label: string } | null;
}

export interface PaneDeps {
  /** Build what to show for *target* from the page's current answer. */
  view: (target: PaneTarget) => PaneView;
  /** Draw the list again (it marks the selected line) and so the pane; false when there was no list to draw. */
  redraw: () => boolean;
  /** Put the focus back on the line that opened *target*. */
  focusLine: (target: PaneTarget) => void;
  /** Tell the wall which card the pane holds (null: none), so it takes the card back. */
  lend: (card: string | null) => void;
  /** The pane opened or closed (the shell shows it in the wall's place). */
  onChange?: (open: boolean) => void;
  /** The pane was cleared (not merely rebuilt): the table forgets the row it had open. */
  onClear?: () => void;
}

export interface Pane {
  /** The pane's element (the table paints held row messages in it and syncs its checkboxes). */
  root: HTMLElement;
  /** Open *target*; *focus* (default true) moves the focus to the heading. */
  show: (target: PaneTarget, focus?: boolean) => void;
  /** Close it; *focus* (default true, and always when the focus was inside the pane) sends the focus back to the line that opened it. */
  clear: (focus?: boolean) => void;
  current: () => PaneTarget | null;
  /** Draw it again from the table (after every list draw). */
  refresh: () => void;
  /** Put the lent card back in the foot when a board's rebuild pulled it away. */
  retake: () => void;
  stop: () => void;
}

/** Input types that take no typed text, so Escape stays free for the pane. */
const NON_TEXT_INPUTS = new Set(["checkbox", "radio", "button", "submit", "reset", "range", "color", "image", "file"]);

export const sameTarget = (a: PaneTarget | null, b: PaneTarget | null): boolean => !!a && !!b && a.kind === b.kind && a.id === b.id;

export function mountPane(host: HTMLElement, deps: PaneDeps): Pane {
  let target: PaneTarget | null = null;
  let lent: string | null = null;
  let lentNode: HTMLElement | null = null;
  host.classList.add("nt-pane");
  host.hidden = true;
  const name = el("h3", { class: "nt-pane-name", tabindex: "-1" });
  const iconSlot = el("span", { class: "nt-pane-icon", "aria-hidden": "true" });
  const slotPills = el("span", { class: "nt-pane-pills" });
  const slotReview = el("span", { class: "nt-pane-review" });
  const close = el("button", { type: "button", class: "btn btn-quiet nt-close nt-pane-close", title: "Close this pane (Esc)", "data-focus": "pane:close" }, closeIcon(), el("span", { class: "nt-label" }, "Close"));
  const msg = el("div", { class: "nt-page-msg nt-pane-msg", role: "status", "aria-live": "polite" });
  const head = el("header", { class: "nt-pane-h" }, iconSlot, name, slotPills, el("span", { class: "nt-pane-sp" }), slotReview, close);
  const content = el("div", { class: "nt-pane-main" });
  const foot = el("section", { class: "nt-pane-card", hidden: true });
  const footLabel = el("div", { class: "nt-pane-card-l" });
  const footHost = el("div", { class: "nt-pane-card-in" });
  foot.append(footLabel, footHost);
  const body = el("div", { class: "nt-pane-body" }, msg, content, foot);
  // .attn brings the rows' and buttons' styles; its own frame stays out of the layout (display: contents).
  host.append(el("div", { class: "attn nt-pane-attn" }, head, body));

  const setLent = (id: string | null, node: HTMLElement | null): void => {
    if (id === lent && node === lentNode) return;
    // Tell the wall first, so its next draw skips this card; the old card goes back to its tile.
    lent = id;
    lentNode = node;
    deps.lend(id);
  };

  const release = (): void => {
    foot.hidden = true;
    footLabel.textContent = "";
    footHost.classList.remove("wall-tile");
    delete footHost.dataset.card;
    delete footHost.dataset.tone;
    footHost.replaceChildren();
    setLent(null, null);
  };

  // One check for both listeners: Escape belongs to whatever already takes it (2026-11-18, review): an event that was
  // handled (an open menu closes itself and marks it), a dialog, a text field, a select, an editable area, a menu or
  // list box, or the trigger of an open popup.
  const protectedTarget = (event: KeyboardEvent): boolean => {
    if (event.defaultPrevented) return true;
    const at = event.target as HTMLElement | null;
    if (!at || typeof at.closest !== "function") return false;
    if (at.closest("dialog, [role='dialog'], [role='menu'], [role='menuitem'], [role='listbox'], select, textarea, [contenteditable]:not([contenteditable='false'])")) return true;
    // Only text-like inputs keep Escape; a ticked checkbox or a pressed button must not trap it (2026-11-18, review).
    const field = at.closest("input");
    if (field && !NON_TEXT_INPUTS.has(field.type)) return true;
    return at.getAttribute("aria-expanded") === "true" && at.hasAttribute("aria-haspopup");
  };

  const onKey = (event: KeyboardEvent): void => {
    if (event.key !== "Escape" || !target || host.hidden || protectedTarget(event)) return;
    event.preventDefault();
    event.stopPropagation();
    api.clear(true);
  };
  host.addEventListener("keydown", onKey);
  // Escape also closes the pane while focus is in the Needs you list or on the page body (2026-11-18: it used to
  // work only with focus inside the pane). Inside the pane the host's own listener decides.
  const onDocKey = (event: KeyboardEvent): void => {
    // A pane whose host left the page (the shell rebuilt it) drops its own listener.
    if (!host.isConnected) {
      document.removeEventListener("keydown", onDocKey);
      return;
    }
    if (event.key !== "Escape" || !target || host.hidden || protectedTarget(event)) return;
    const at = event.target as HTMLElement | null;
    if (!at || at === document.documentElement || host.contains(at)) return;
    const onBody = at === document.body;
    if (!onBody && !at.closest("#needs-you")) return;
    event.preventDefault();
    api.clear(true);
  };
  document.addEventListener("keydown", onDocKey);
  close.addEventListener("click", () => api.clear(true));

  const api: Pane = {
    root: host,
    show: (next, focus = true) => {
      target = next;
      host.hidden = false;
      deps.onChange?.(true);
      // The list draws again (the selected line) and its draw ends in `refresh`; with no list draw, draw here.
      if (!deps.redraw()) api.refresh();
      if (focus) name.focus({ preventScroll: true });
    },
    clear: (focus = true) => {
      if (!target) return;
      const was = target;
      // The focus was inside the pane (Escape, Close, or the window narrowing): it goes to the line that opened it.
      const inside = host.contains(document.activeElement);
      target = null;
      host.hidden = true;
      content.replaceChildren();
      msg.replaceChildren();
      release();
      deps.onChange?.(false);
      deps.onClear?.();
      deps.redraw();
      if (focus || inside) deps.focusLine(was);
    },
    current: () => target,
    refresh: () => {
      if (!target) return;
      const view = deps.view(target);
      const active = document.activeElement as HTMLElement | null;
      const mark = active && host.contains(active) ? (active.dataset.focus ?? null) : null;
      const scroll = body.scrollTop;
      name.textContent = view.heading.name;
      iconSlot.replaceChildren(...(view.heading.icon ? [view.heading.icon] : []));
      slotPills.replaceChildren(...(view.heading.pills ? [view.heading.pills] : []));
      slotReview.replaceChildren(...(view.heading.review ? [view.heading.review] : []));
      host.setAttribute("aria-label", `${view.heading.name}, opened from Needs you`);
      const parts: Node[] = [];
      for (const section of view.sections) {
        parts.push(
          el(
            "section",
            { class: "nt-pane-sec" },
            section.name ? el("div", { class: "nt-pane-sec-h" }, el("h4", {}, section.name), section.pills, section.review) : null,
            el("div", { class: "nt-table nt-pane-table", role: "group", "aria-label": section.name ?? view.heading.name }, section.rows),
          ),
        );
      }
      if (view.detail) parts.push(view.detail);
      content.replaceChildren(...parts);
      if (view.card) {
        // The real card, moved in: one instance. The wall is told first so it never takes it back meanwhile.
        setLent(view.card.id, view.card.node);
        // The wall's compact style (which parts of each card show) applies in the foot too: it is the same card.
        setCardWall(view.card.node, true);
        // The foot keeps the wall's tone, so the card's left stripe shows here too (2026-11-18), from the same counts the wall
        // uses. The card's own header (and so its pills) stays hidden: the pane's header already shows this source's pills.
        const counts = cardCounts(view.card.id);
        footHost.dataset.tone = counts?.urgent ? "urgent" : counts?.review ? "review" : "none";
        view.card.node.querySelector(":scope > .admin-card-top > .wall-pills")?.remove();
        footLabel.textContent = view.card.label;
        footHost.classList.add("wall-tile");
        footHost.dataset.card = view.card.id;
        if (view.card.node.parentElement !== footHost) footHost.replaceChildren(view.card.node);
        foot.hidden = false;
      } else release();
      body.scrollTop = scroll;
      if (mark) [...host.querySelectorAll<HTMLElement>("[data-focus]")].find((n) => n.dataset.focus === mark)?.focus({ preventScroll: true });
    },
    retake: () => {
      if (!target || !lentNode || lentNode.parentElement === footHost) return;
      footHost.replaceChildren(lentNode);
      setCardWall(lentNode, true);
    },
    stop: () => {
      host.removeEventListener("keydown", onKey);
      document.removeEventListener("keydown", onDocKey);
      if (target) {
        target = null;
        host.hidden = true;
        release();
      }
    },
  };
  return api;
}
