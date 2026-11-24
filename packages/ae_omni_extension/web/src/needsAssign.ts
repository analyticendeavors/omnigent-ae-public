// Assign, in the Admin Center's Needs you table (2026-11-16, D2 phase 3).
//
// An admin can give an open item to another admin (or themselves): the opened row's Assign button
// opens a native picker of the current admins, the row shows the assignee's initials in a chip, and
// the toolbar's "Assigned to me" segment narrows the table to what is theirs. The server decides
// everything (who is an admin, whether the item can be assigned, which counts it leaves); this file
// draws and asks. An item assigned to someone else stays in place, dimmed, and the counts are the
// server's. The admin list is asked when a row's panel opens and kept 60 seconds, like the server.

import type { ExtensionContext } from "@omnigent/extension-sdk";
import {
  type AttentionAdmin,
  type AttentionItem,
  assignAttention,
  assignedElsewhere,
  describeError,
  fetchAttentionAdmins,
} from "./api";
import { type Panel, reloadPanel } from "./attention";
import { button, el, flash } from "./dom";
import { sayInRow } from "./rowMessages";
import { forgetHistory } from "./historyCache";
import { squareCheckIcon, squareIcon, userPlusIcon } from "./icons";

// ── who ─────────────────────────────────────────────────────────────────────

/** "alice": a login without its mail domain. */
export const nameOf = (login: string): string => login.split("@")[0] || login;

/** Up to two initials of a login: "ann.lee@example.com" is "AL", "alice" is "A". */
export function initialsOf(login: string): string {
  const words = nameOf(login)
    .split(/[^\p{L}\p{N}]+/u)
    .filter(Boolean);
  const first = words[0] ?? "?";
  const letters = words.length > 1 ? [first, words[1]!].map((word) => [...word][0]!).join("") : [...first][0]!;
  return letters.toUpperCase();
}

/** True when *item* is assigned to *me*. */
export const assignedToMe = (item: Pick<AttentionItem, "assigned">, me: string | undefined): boolean =>
  Boolean(item.assigned) && me !== undefined && item.assigned!.to === me;

export { assignedElsewhere };

/** The assignee's chip: initials, with the full id as the accessible name and the tooltip. Null when unassigned. */
export function assigneeChip(item: Pick<AttentionItem, "assigned">, me?: string): HTMLElement | null {
  const held = item.assigned;
  if (!held) return null;
  const mine = held.to === me;
  const name = mine ? `Assigned to you (${held.to})` : `Assigned to ${held.to}`;
  const chip = el(
    "span",
    { class: `nt-assignee${mine ? " is-me" : ""}`, role: "img", "aria-label": name, title: name },
    initialsOf(held.to),
  );
  return chip;
}

// ── the admin list ──────────────────────────────────────────────────────────

const ADMINS_TTL_MS = 60_000;
type AdminsState = { at: number; admins: AttentionAdmin[] } | { at: number; error: unknown };
let adminsHeld: AdminsState | null = null;
let adminsAsk: Promise<AdminsState> | null = null;

/** Forget the held admin list (the tests, and a page that was deactivated). */
export function resetAdmins(): void {
  adminsHeld = null;
  adminsAsk = null;
}

/** The current admins: from the last answer when it is under a minute old, else asked again. A failure is not kept past a retry. */
export function loadAdmins(context: ExtensionContext, now = Date.now()): Promise<AdminsState> {
  if (adminsHeld && "admins" in adminsHeld && now - adminsHeld.at < ADMINS_TTL_MS) return Promise.resolve(adminsHeld);
  adminsAsk ??= fetchAttentionAdmins(context).then(
    (answer): AdminsState => ({ at: Date.now(), admins: answer.admins }),
    (error: unknown): AdminsState => ({ at: Date.now(), error }),
  );
  const ask = adminsAsk;
  void ask.then((state) => {
    if (adminsAsk === ask) adminsAsk = null;
    adminsHeld = "admins" in state ? state : null;
  });
  return ask;
}

// ── the opened row's Assign ─────────────────────────────────────────────────

/** Rows whose picker is open; a redraw (a refresh) keeps it open. */
const pickerOpen = new Set<string>();

/**
 * True when the row can offer Assign at all: a signed-in OmniGent admin (the server's `can_chat`
 * is that) and not a personal Git item. The server checks both again; this only hides the button.
 */
export const assignable = (item: AttentionItem, canChat: boolean): boolean => canChat && item.source !== "git-mine";

async function assign(p: Panel, item: AttentionItem, to: string | null, host: HTMLElement): Promise<void> {
  try {
    const made = await assignAttention(p.context, item.id, to);
    forgetHistory(item.id);
    pickerOpen.delete(item.id);
    await reloadPanel(p);
    const line = made.assigned ? `Assigned to ${nameOf(made.assigned.to)}.` : "Unassigned.";
    const row = p.root.querySelector<HTMLElement>(`.nt-row[data-id="${CSS.escape(item.id)}"], .attn-row[data-id="${CSS.escape(item.id)}"]`);
    const target = p.root.querySelector<HTMLElement>(".nt-sheet-msg") ?? row?.querySelector<HTMLElement>(".nt-panel-main") ?? p.root;
    sayInRow(p, item.id, line, "ok", target);
  } catch (error) {
    sayInRow(p, item.id, `Could not ${to ? "assign" : "unassign"} (${describeError(error)}).`, "error", host);
  }
}

/**
 * The opened row's Assign block: an "Assign" button that opens a picker (a native select of the
 * admins, a confirm button, and Unassign when the item is assigned). Null when the row cannot take
 * it. While the admin list loads the block says so; when it cannot be read the block is one line
 * saying so (never a guess at who is an admin).
 */
export function assignBlock(
  p: Panel,
  item: AttentionItem,
  canChat: boolean,
  me: string | undefined,
  inline = false,
): HTMLElement | null {
  if (!assignable(item, canChat)) return null;
  // inline: a part of the opened row's toolbar (a button, and the picker beside it once open), with no box of its own.
  const group = el("div", { class: `nt-assign${inline ? " is-inline" : ""}`, role: "group", "aria-label": `Assign ${item.title}` });
  const draw = (state: AdminsState | null): void => {
    group.replaceChildren();
    if (!inline) group.append(el("span", { class: "nt-assign-k" }, userPlusIcon(), "Assign"));
    if (!state) {
      group.append(el("span", { class: "meta" }, "Loading the admins..."));
      return;
    }
    if (!("admins" in state)) {
      group.append(
        el("span", { class: "meta nt-assign-bad", role: "status" }, `The list of admins could not be read (${describeError(state.error)}), so Assign is off. Close this item and open it again to try once more.`),
      );
      return;
    }
    const open = pickerOpen.has(item.id);
    const toggle = button("Assign", {
      title: "Give this item to an admin: it counts in their Needs you, not yours",
      onClick: () => {
        if (pickerOpen.has(item.id)) pickerOpen.delete(item.id);
        else pickerOpen.add(item.id);
        draw(state);
        group.querySelector<HTMLElement>(open ? ".nt-assign-toggle" : "select")?.focus({ preventScroll: true });
      },
    });
    toggle.classList.add("nt-assign-toggle");
    if (inline) {
      toggle.prepend(userPlusIcon());
      toggle.classList.add("nt-labelled", "attn-btn");
    }
    toggle.dataset.focus = `${item.id}:assign`;
    toggle.setAttribute("aria-expanded", String(open));
    toggle.setAttribute("aria-label", `Assign ${item.title} to an admin`);
    const current = item.assigned;
    if (!open) {
      group.append(toggle);
      if (current) {
        group.append(el("span", { class: "nt-assign-now" }, assigneeChip(item, me), ` ${current.to === me ? "You" : nameOf(current.to)}`));
      }
      return;
    }
    const selectId = `nt-assign-sel-${item.id.replace(/[^A-Za-z0-9_-]+/g, "-")}`;
    const select = el("select", { class: "nt-assign-select", id: selectId, "aria-label": `Admin to assign ${item.title} to` });
    select.append(el("option", { value: "" }, "Choose an admin"));
    for (const admin of state.admins) {
      const option = el("option", { value: admin.id }, admin.id === me ? `${admin.name} (you)` : admin.name);
      if (current?.to === admin.id) option.setAttribute("selected", "");
      select.append(option);
    }
    const confirm = button("Confirm", {
      kind: "primary",
      title: "Assign this item to the chosen admin",
      onClick: () => {
        if (select.value) void assign(p, item, select.value, group);
        else flash(group, "Choose an admin first.", "error");
      },
    });
    confirm.dataset.focus = `${item.id}:assign-confirm`;
    group.append(toggle, select, confirm);
    if (current) {
      const off = button("Unassign", {
        title: "Take the assignment off: it counts for everyone again",
        onClick: () => void assign(p, item, null, group),
      });
      off.dataset.focus = `${item.id}:unassign`;
      group.append(off);
    }
  };
  draw(null);
  void loadAdmins(p.context).then(draw);
  return group;
}

// ── "Assigned to me" ────────────────────────────────────────────────────────

const VIEW_KEY = "needs.assigned.";
const viewMemory = new Map<string, boolean>();

/** Whether *me* left the table on "Assigned to me" (remembered per person, in this browser). */
export function assignedOnly(me: string | undefined): boolean {
  const who = me ?? "";
  try {
    const raw = window.localStorage.getItem(VIEW_KEY + who);
    if (raw === "on") return true;
    if (raw === "off") return false;
  } catch {
    // No storage in this frame: the state kept in memory.
  }
  return viewMemory.get(who) ?? false;
}

export function saveAssignedOnly(me: string | undefined, on: boolean): void {
  const who = me ?? "";
  viewMemory.set(who, on);
  try {
    window.localStorage.setItem(VIEW_KEY + who, on ? "on" : "off");
  } catch {
    // Kept in memory until the page closes.
  }
}

/** Forget the in-memory choice (the tests). */
export function resetAssignedView(): void {
  viewMemory.clear();
}

/**
 * The toolbar chip "Assigned to me N" (2026-11-18, replacing the two-button "All | Assigned to me" group):
 * one toggle button, an empty box when off (everything shows) and a ticked box with an accent tint when on
 * (only the viewer's rows show); a second click turns it off. The count of rows assigned to the viewer shows
 * as muted text when above zero. Null for a viewer who cannot be assigned anything.
 */
export function assignedSegment(items: AttentionItem[], me: string | undefined, canChat: boolean, onChange: () => void): HTMLElement | null {
  if (!canChat) return null;
  const mine = items.filter((item) => assignedToMe(item, me)).length;
  const on = assignedOnly(me);
  const chip = el(
    "button",
    {
      type: "button",
      class: `nt-view-chip${on ? " is-on" : ""}`,
      "aria-pressed": String(on),
      title: "Show only the items assigned to you",
      "data-focus": "view:mine",
    },
    on ? squareCheckIcon() : squareIcon(),
    el("span", { class: "nt-view-label" }, "Assigned to me"),
    mine > 0 ? el("span", { class: "nt-view-n" }, String(mine)) : null,
  );
  chip.addEventListener("click", () => {
    saveAssignedOnly(me, !assignedOnly(me));
    onChange();
  });
  return chip;
}
