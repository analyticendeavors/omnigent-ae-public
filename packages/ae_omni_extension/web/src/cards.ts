// One session row for every section that lists sessions (2026-10-14, Reid's
// design pass on the Dashboard): Working and Blocked (capacity.ts) and
// Review, Needs a note and Unsorted (sessions.ts) each drew their own card,
// and the status buttons (Working, Review, Reference, ...) sat beside Open at
// the same weight, so every card read as three or four equal actions.
//
// Second pass (2026-10-14, Reid's approved spec): a row is the status icon,
// the title, tags and age on one line, then NEXT. The title is the Open
// control (a button styled as the title); the Open button went. Park and the
// status moves sit in a "..." menu at the line's right end; a button the
// section adds (File a note) stays in view under NEXT. The menu is a menu
// button: aria-haspopup and aria-expanded, menuitem buttons, arrow keys,
// Escape and a click outside close it and focus returns to the button. It
// opens up or to the right when the frame has no room, and the 20 s refresh
// reopens it on the redrawn row, as an open merge confirm step comes back
// (prs.ts keepConfirm).
//
// Review (2026-10-14): that reopening is one explicit step at the end of
// main.ts render (holdMenus), not a microtask each drawn menu queued. It
// moves the focus only when the focus was in the menu, and a menu button or
// item that loses its row to the redraw hands the focus on, never to body.
//
// 2026-10-16 (Fabric Admin review): the Admin Center's Fabric section links
// menu is this menu too, with its own icon and name (RowMenuTrigger), so it gets the same
// viewport flip and redraw handling. While a menu is open its card carries
// `menu-open`, which lifts the card over the cards after it: a card that is
// its own stacking context (Fabric's container-type) would otherwise paint
// the next card over the menu.

import type { ExtensionContext } from "@omnigent/extension-sdk";
import { type AeStatus, describeError, setStatus } from "./api";
import { announce, append, button, type Child, el, flash } from "./dom";
import { dotsIcon } from "./icons";

interface Deps {
  context: ExtensionContext;
  refresh: () => Promise<void>;
}

export interface SessionRow {
  id: string;
  title: string;
  /** The row's colour accent: a status, or "unsorted". */
  kind: AeStatus | "unsorted";
  icon: Child;
  /** Chips after the title (gate open, n unfiled). */
  tags?: Child[];
  /** Right-aligned on the line: elapsed time, or why the row is listed. */
  meta?: string;
  next?: string | null;
  /** Lines under NEXT ("riding unparked"). */
  notes?: Child[];
  /** Buttons that stay in view under NEXT (File a note). */
  actions?: HTMLButtonElement[];
  /** Park and the status moves: the items of the row's "..." menu. */
  moves?: HTMLButtonElement[];
}

export function sessionRow(deps: Deps, row: SessionRow): HTMLElement {
  const card = el("article", { class: `card card-${row.kind} card-session`, "data-session": row.id });
  const open = el(
    "button",
    { type: "button", class: "card-open", title: `Open ${row.title}` },
    row.title,
  );
  open.addEventListener("click", () => void deps.context.navigation.openSession(row.id));
  const moves = row.moves ?? [];
  card.appendChild(
    el(
      "div",
      { class: "card-head" },
      row.icon,
      el("h3", { class: "card-title" }, open),
      ...(row.tags ?? []),
      row.meta ? el("span", { class: "meta" }, row.meta) : null,
      moves.length > 0 ? rowMenu(row, moves) : null,
    ),
  );
  if (row.next)
    card.appendChild(el("p", { class: "next" }, el("span", { class: "k" }, "NEXT "), row.next));
  append(card, row.notes ?? []);
  const actions = row.actions ?? [];
  if (actions.length > 0) card.appendChild(el("div", { class: "actions" }, ...actions));
  return card;
}

/**
 * A "Move to" item that writes `ae.status` and refreshes; a failure flashes
 * on the row holding the pressed item.
 */
export function moveButton(
  deps: Deps,
  session: { id: string; title: string },
  text: string,
  status: AeStatus,
  title: string,
): HTMLButtonElement {
  const node = button(text, {
    title,
    onClick: async (pressed) => {
      try {
        await setStatus(deps.context, session.id, status);
        announce(`${session.title}: marked ${status}.`);
        await deps.refresh();
      } catch (error) {
        const card = pressed.closest<HTMLElement>("article.card");
        if (card) flash(card, describeError(error), "error");
      }
    },
  });
  node.classList.add("btn-move");
  return node;
}

// ── the "..." menu ──────────────────────────────────────────────────────────

/** A row's section and its data-session, data-task, data-pr or data-capacity: how a redraw finds it again. */
interface Where {
  section: string;
  key: (typeof ROW_KEYS)[number];
  value: string;
}

interface OpenMenu {
  /** The row the menu was opened on. */
  where: Where | null;
  trigger: HTMLButtonElement;
  /** The item that last had focus, put back after a redraw. */
  focused: number | null;
  close: (returnFocus: boolean) => void;
}

/** Each drawn menu's open step, by its button, for holdMenus to reopen it. */
const openers = new WeakMap<HTMLButtonElement, (focus: number | null) => void>();

let current: OpenMenu | null = null;
let listening = false;
let menus = 0;

/** For tests: forget the open menu. */
export function resetMenuState(): void {
  current = null;
}

const ROW_KEYS = ["session", "task", "pr", "capacity"] as const;

function whereOf(row: HTMLElement | null): Where | null {
  const section = row?.closest("section")?.id;
  if (!row || !section) return null;
  for (const key of ROW_KEYS) {
    const value = row.dataset[key];
    if (value !== undefined) return { section, key, value };
  }
  return null;
}

/** The row at *where* on the page as it is now. */
function rowAt(where: Where | null): HTMLElement | null {
  if (!where) return null;
  const section = document.getElementById(where.section);
  const rows = [...(section?.querySelectorAll<HTMLElement>("article.card") ?? [])];
  return rows.find((row) => row.dataset[where.key] === where.value) ?? null;
}

/** On the page, not turned off, and inside nothing hidden: it can take the focus. */
function usable(node: HTMLElement | null | undefined): node is HTMLElement {
  return (
    !!node && node.isConnected && !(node as HTMLButtonElement).disabled && !node.closest("[hidden]")
  );
}

/** The first control in *row* that can take the focus, by *selectors* in order. */
function controlOf(row: HTMLElement | null, selectors: string[]): HTMLElement | null {
  for (const selector of selectors) {
    const found = [...(row?.querySelectorAll<HTMLElement>(selector) ?? [])].find(usable);
    if (found) return found;
  }
  return null;
}

/** A row's own controls, the quiet ones first: its menu button, title, chevron, then any. */
const ROW_CONTROLS = [".btn-menu", ".card-open", ".card-toggle", "button"];

/** Where each row after *row* on the page sits, in page order. */
function rowsAfter(row: HTMLElement): (Where | null)[] {
  const rows = [...document.querySelectorAll<HTMLElement>("section article.card")];
  return rows.slice(rows.indexOf(row) + 1).map(whereOf);
}

function toggleOf(section: string): HTMLElement | null {
  const toggle = document.getElementById(section)?.querySelector<HTMLElement>(".section-toggle");
  return usable(toggle) ? toggle : null;
}

/**
 * Call before the page redraws and call what it returns once the redraw is
 * done (main.ts render; 2026-10-14, review of the second pass). An open menu
 * reopens on its row's new button; the focus goes back to the same item
 * only when it was in the menu, and never scrolls the page. When a menu
 * button or item had the focus and the redraw took it, the focus moves to
 * the row's new button, else to the heading of the section the row was in,
 * else to the next row: never to the page itself. A frame without the focus
 * is left without it.
 */
export function holdMenus(): () => void {
  const open = current?.trigger.isConnected ? current : null;
  const active = document.hasFocus() ? document.activeElement : null;
  const wrap = active instanceof HTMLElement ? active.closest<HTMLElement>(".menu-wrap") : null;
  const row = wrap?.closest<HTMLElement>("article.card") ?? null;
  const where = whereOf(row);
  const after = row && where ? rowsAfter(row) : [];
  return () => {
    if (open && current === open && !open.trigger.isConnected) {
      current = null;
      const trigger = rowAt(open.where)?.querySelector<HTMLButtonElement>(".btn-menu");
      const reopen = usable(trigger) ? openers.get(trigger) : undefined;
      const inside = wrap === open.trigger.parentElement && open.focused !== null;
      if (reopen) {
        reopen(inside ? open.focused : null);
        if (inside) return;
      }
    }
    if (!wrap || !where || wrap.isConnected) return;
    if (document.activeElement && document.activeElement !== document.body) return;
    const targets = [
      () => controlOf(rowAt(where), [".btn-menu"]),
      () => toggleOf(where.section),
      ...after.map((next) => () => controlOf(rowAt(next), ROW_CONTROLS)),
      () => [...document.querySelectorAll<HTMLElement>(".section-toggle")].find(usable) ?? null,
    ];
    for (const find of targets) {
      const target = find();
      if (target) {
        target.focus({ preventScroll: true });
        return;
      }
    }
  };
}

/** A press anywhere outside the open menu and its button closes it. */
function onPointerDown(event: Event): void {
  if (!current) return;
  if (!current.trigger.isConnected) {
    current = null;
    return;
  }
  const wrap = current.trigger.parentElement;
  if (wrap && event.target instanceof Node && wrap.contains(event.target)) return;
  current.close(false);
}

/**
 * Open under the button, right-aligned; up when the frame has no room below
 * and there is room above, to the right when the left edge would cut it.
 */
function place(trigger: HTMLElement, menu: HTMLElement): void {
  menu.classList.remove("menu-up", "menu-right");
  const box = menu.getBoundingClientRect();
  const from = trigger.getBoundingClientRect();
  if (box.bottom > window.innerHeight && from.top - box.height - 4 >= 0) {
    menu.classList.add("menu-up");
  }
  if (box.left < 0) menu.classList.add("menu-right");
}

/** How a menu's button looks and is named, when not the "..." of a session row. */
export interface RowMenuTrigger {
  icon?: SVGElement;
  /** The button's accessible name; the menu takes it too. */
  label?: string;
  /** The tooltip; the items' names by default. */
  title?: string;
  /** More classes on the button; `btn-menu` always stays, holdMenus finds it by that. */
  className?: string;
}

/**
 * A row's "..." button and its menu of *items*: session rows (sessionRow),
 * since the review of 2026-10-14 Your move's session cards, and since
 * 2026-10-16 a Fabric capacity's links (fabric.ts), with *look*'s icon and name.
 */
export function rowMenu(
  row: { title: string },
  items: HTMLButtonElement[],
  look: RowMenuTrigger = {},
): HTMLElement {
  const menuId = `row-menu-${++menus}`;
  const trigger = el(
    "button",
    {
      type: "button",
      class: `btn btn-quiet btn-menu${look.className ? ` ${look.className}` : ""}`,
      "aria-haspopup": "menu",
      "aria-expanded": "false",
      "aria-controls": menuId,
      "aria-label": look.label ?? `More actions for ${row.title}`,
      title: look.title ?? items.map((item) => item.textContent).join(", "),
    },
    look.icon ?? dotsIcon(),
  );
  const menu = el("div", {
    class: "menu",
    id: menuId,
    role: "menu",
    "aria-label": look.label ?? `Actions for ${row.title}`,
    hidden: true,
  });
  for (const item of items) {
    item.setAttribute("role", "menuitem");
    item.tabIndex = -1;
    item.classList.add("menu-item");
    menu.appendChild(item);
  }
  const state: OpenMenu = { where: null, trigger, focused: null, close };
  const focusItem = (index: number, quiet = false) => {
    const count = items.length;
    state.focused = ((index % count) + count) % count;
    items[state.focused].focus(quiet ? { preventScroll: true } : undefined);
  };
  function close(returnFocus: boolean): void {
    menu.hidden = true;
    menu.classList.remove("menu-up", "menu-right");
    trigger.closest("article.card")?.classList.remove("menu-open");
    trigger.setAttribute("aria-expanded", "false");
    if (current === state) current = null;
    if (returnFocus) trigger.focus();
  }
  function open(focus: number | null, quiet = false): void {
    if (current && current !== state) current.close(false);
    if (!listening) {
      document.addEventListener("pointerdown", onPointerDown, true);
      listening = true;
    }
    menu.hidden = false;
    trigger.setAttribute("aria-expanded", "true");
    const card = trigger.closest<HTMLElement>("article.card");
    // Over the cards after it (module note, 2026-10-16).
    card?.classList.add("menu-open");
    state.where = whereOf(card);
    state.focused = focus;
    current = state;
    place(trigger, menu);
    if (focus !== null) focusItem(focus, quiet);
  }
  // holdMenus reopens a redrawn row's menu through this, without scrolling.
  openers.set(trigger, (focus) =>
    open(focus === null ? null : Math.min(focus, items.length - 1), true),
  );
  trigger.addEventListener("click", () => {
    if (menu.hidden) open(0);
    else close(true);
  });
  trigger.addEventListener("keydown", (event) => {
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault();
      open(event.key === "ArrowDown" ? 0 : items.length - 1);
    } else if (event.key === "Escape" && !menu.hidden) {
      event.preventDefault();
      close(true);
    }
  });
  menu.addEventListener("keydown", (event) => {
    const at = items.indexOf(document.activeElement as HTMLButtonElement);
    if (event.key === "ArrowDown") focusItem(at < 0 ? 0 : at + 1);
    else if (event.key === "ArrowUp") focusItem(at < 0 ? items.length - 1 : at - 1);
    else if (event.key === "Home") focusItem(0);
    else if (event.key === "End") focusItem(items.length - 1);
    else if (event.key === "Escape") close(true);
    else if (event.key === "Tab") {
      // Back on the button, so the key's own move goes on from there.
      close(true);
      return;
    } else return;
    event.preventDefault();
    event.stopPropagation();
  });
  menu.addEventListener("focusin", (event) => {
    const at = items.indexOf(event.target as HTMLButtonElement);
    if (at >= 0) state.focused = at;
  });
  // Closed before the item acts (capture), so an item that moves focus on
  // (Park opens the park form) keeps it there.
  menu.addEventListener(
    "click",
    (event) => {
      if (items.includes(event.target as HTMLButtonElement)) close(true);
    },
    true,
  );
  return el("div", { class: "menu-wrap" }, trigger, menu);
}
