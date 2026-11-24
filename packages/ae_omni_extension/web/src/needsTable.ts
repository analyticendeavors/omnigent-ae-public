// The Admin Center's Needs you table (2026-11-14, concept D2; replaces the tree attention.ts drew).
//
// Why: the tree gave every source its own box and every kind its own fold, so a busy page was a
// stack of small panels. One table groups the same items by area, then source, then rows, with
// the columns What, Checked and Action, and a row opens in place under itself. This file
// only draws; attention.ts still loads, counts and acts (Review, Fix, Merge, Dismiss, Clean up and
// their dialogs), and the rows' buttons are the ones it builds. needsModel.ts holds the grouping
// and the words, so a test can read them without a page.
//
// The group rows are disclosure buttons (aria-expanded, aria-controls), not a listbox. On first
// sight only paths that hold an urgent item are open; a closed area row lists its sources with
// counts and a closed source row names its first item "and N more". At most three rows show under
// an open source, then "Show N more". The open or closed state is remembered per person with
// fold.ts, as before, and a line that gains an urgent item it has not shown before opens again.
//
// An opened row stays the header. Its own action button gives way to a Close control, and under
// it one panel holds: the evidence line, the item's facts in one row of equal boxes, one row of
// actions (the primary first, Dismiss at the far right) and one hint line. The panel is built as
// a main column so phase 2 can add a history box beside it without moving anything.
//
// The pane (2026-11-18, needsPane.ts): at the wide band a click on an area or source line fills a pane in the
// wall's place instead of opening rows under the line. The pane's rows are made by this file's own row code
// (shell and finish, through attention.ts rowNode), so a row looks and acts the same in both places; one
// message store and one selection serve both.

import type { AttentionItem, AttentionOverview, AttentionSource } from "./api";
import {
  type Block,
  type Panel,
  type TableHooks,
  comesBackOn,
  countsText,
  dismissedList,
  draw as redrawPanel,
  emptyText,
  isPhone,
  mergeFacts,
  part,
  readCardOf,
  markNewUrgent,
  reviewAllNode,
  rowNode,
  slug,
  toolButton,
  trackRowOrder,
} from "./attention";
import { attentionView } from "./attentionView";
import { setFilter, SOURCE_NAMES } from "./attentionStore";
import { getCards } from "./cardState";
import { button, el, notice, replaceChildren } from "./dom";
import { describeError } from "./api";
import { rememberedFold, saveFold } from "./fold";
import { arrowRightIcon, checkIcon, chevronIcon, chevronsDownIcon, chevronsUpIcon, closeIcon, externalLinkIcon, layoutGridIcon } from "./icons";
import {
  type AreaGroup,
  type Fact,
  AREA_OF,
  agoText,
  areaCounts,
  areaCut,
  sourceCut,
  areaFoldId,
  areaPreview,
  buildAreas,
  assignColumns,
  placeLines,
  columnCount,
  planOpen,
  planKey,
  listCost,
  evidenceText,
  factsOf,
  hasSeveralKinds,
  kindName,
  rowsShown,
  sourcePreview,
  sourceCounts,
  sourceList,
  sourcesOfArea,
  unknownLines,
  unreadSources,
} from "./needsModel";
import { createBulkBar, groupPickBox, pickBox, syncPicks, watchPicks } from "./needsBulk";
import { type Pane, type PaneDeps, type PaneSection, type PaneTarget, type PaneView, sameTarget } from "./needsPane";
import { closedCopyButton, markStale, skipButton, socialRow } from "./needsPost";
import { ageText, historyBox, showPost, snoozeBlock, snoozeDropdown, snoozeMenu, snoozedNode, socialPicture } from "./needsMore";
import { assignBlock, assignedElsewhere, assignedOnly, assignedSegment, assignedToMe, assigneeChip, saveAssignedOnly } from "./needsAssign";
import { keepOnly } from "./needsSelect";
import { openOrShow } from "./openLink";
import { createRowMessages, type RowMessage, type RowMessages } from "./rowMessages";
import { DOMAINS, type DomainId } from "./tiles";

/** An item with the kind to name before its title, set when its source has several kinds. */
type TableItem = AttentionItem & { kindPrefix?: string };

export interface NeedsTableOptions {
  /** Open an area's tab (the quiet link at the end of an area row). */
  openTab: (area: DomainId) => void;
  /** True when *area* has a tab of its own (a box admin's page, or a server with four cards or fewer, has none). */
  hasTab: (area: DomainId) => boolean;
  /** Open the card that owns a source (the opened row's "Open card"). */
  openCard: (card: string, area: DomainId) => void;
  /** The table container's width in px (tests give one; the page measures the element). */
  measure?: (host: HTMLElement) => number;
  /** True while a line may fill the pane (the Overview at the wide band); false: a line opens its rows in place. */
  paneWide?: () => boolean;
  /** The real card of a source's card id when the wall would show it, else null (the pane's foot). */
  cardNode?: (card: string) => HTMLElement | null;
}

export interface NeedsTable {
  hooks: TableHooks;
  /** Limit the table to one area (an area tab), or null for all of them. */
  setArea: (area: DomainId | null) => void;
  /** Draw again (the tabs changed, so the area rows' links to them did too). */
  redraw: () => void;
  /** Remove what the table put outside its own element (the phone's sheet). */
  dispose: () => void;
  /** Give the table the pane it fills (null: none). */
  attachPane: (pane: Pane | null) => void;
  /** What the pane needs from the table. */
  paneDeps: Pick<PaneDeps, "view" | "redraw" | "focusLine" | "onClear">;
  /** Open the source that holds row *id* in the pane with that row open; false when there is no pane or no such row. */
  openInPane: (id: string) => boolean;
}

// ── small parts ─────────────────────────────────────────────────────────────

let tableSeq = 0;

const clock = (iso: string): string => {
  const at = Date.parse(iso);
  return Number.isNaN(at) ? "" : new Date(at).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
};

/** Give a control the mark a redraw uses to hand the focus back to it. */
function marked<T extends HTMLElement>(node: T, mark: string): T {
  node.setAttribute("data-focus", mark);
  return node;
}

/** The small dot on a closed line's head: an urgent item arrived since the person last opened it. An overlay, so it never moves the line. */
function newMarker(): HTMLElement {
  return el("span", { class: "nt-new", role: "img", "aria-label": "new urgent item", title: "New urgent item since you last opened this" });
}

function pills(urgent: number, review: number, working: number): HTMLElement {
  return el(
    "span",
    { class: "nt-pills", "aria-label": countsText(urgent, review, working) || "Nothing counted", role: "img" },
    urgent ? el("span", { class: "nt-pill nt-pill-urgent" }, `${urgent} urgent`) : null,
    review ? el("span", { class: "nt-pill nt-pill-review" }, `${review} to review`) : null,
    working ? el("span", { class: "nt-pill nt-pill-chat" }, `${working} with a chat`) : null,
  );
}

/** A source's counts by the page's one rule (the server's by_source), the rows' own when it has none. */
function sourcePills(block: Pick<Block, "source" | "urgent" | "review" | "working">): [number, number, number] {
  const counts = sourceCounts(block.source);
  return counts ? [counts.urgent, counts.review, block.working] : [block.urgent, block.review, block.working];
}

/** An area's counts by the page's one rule (the server's by_area), the rows' own when it has none. */
function areaPills(area: Pick<AreaGroup, "id" | "urgent" | "review" | "working">): [number, number, number] {
  const counts = areaCounts(area.id);
  return counts ? [counts.urgent, counts.review, area.working] : [area.urgent, area.review, area.working];
}

const PRIMARY_ORDER = [":merge", ":cleanup", ":fix", ":review"];
const LABELS: Record<string, string> = {
  merge: "Merge",
  fix: "Fix",
  review: "Review",
  dismiss: "Dismiss",
  skip: "Skip post",
};

function cardOf(source: AttentionSource): string | null {
  return getCards()?.cards.find((card) => card.sources.includes(source))?.id ?? null;
}

// ── the table ───────────────────────────────────────────────────────────────

export function createNeedsTable(options: NeedsTableOptions): NeedsTable {
  let scope: DomainId | null = null;
  let current: Panel | null = null;
  let lastData: AttentionOverview | null = null;

  // The phone's opened item (2026-11-15): a fixed sheet on document.body, outside the page's container
  // (a container is the containing block of a fixed descendant) and not a modal <dialog>, which would
  // make the rest of the page inert and leave the confirmations attention.ts opens (Dismiss, Fix, Review,
  // "no free slot") unreachable behind it. Its z-index sits below those dialogs and the toasts. One node
  // lives while the same row stays open; a redraw updates its content in place.
  interface SheetSpec {
    id: string;
    tone: string;
    title: string;
    close: HTMLElement;
    main: HTMLElement;
    /** The row's text, where attention.ts flashes its messages (null when the row has none). */
    rowText: HTMLElement | null;
  }
  interface SheetLive {
    id: string;
    node: HTMLElement;
    msg: HTMLElement;
    title: HTMLElement;
    scroller: HTMLElement;
  }
  let rowObserver: MutationObserver | null = null;
  let pending: SheetSpec | null = null;
  let sheet: SheetLive | null = null;
  let drawing = false;
  // The Overview's columns (2026-11-15): the width the page last drew for, and what watches it.
  let drawnCols = 1;
  /** Lines closed with an urgent item not seen open yet (set each draw). */
  let newMarks = new Set<string>();
  let widthWatch: ResizeObserver | null = null;
  // The open plan (2026-11-16): made at the first draw, and again only when the areas or sources change,
  // the column count changes, or the list grows past the screen. Dismissing or handing items to a chat never re-folds a group.
  // One plan per scope (the Overview and each area tab): a tab plans for its own room.
  const plans = new Map<string, { key: string; cols: number; count: number; map: Map<string, boolean> }>();
  // Which column each area sits in, kept per scope while the areas and the column count stay the same
  // (2026-11-16: nothing moves while the owner works); it is made again only when an area appears or leaves,
  // or the column count changes.
  const placements = new Map<string, { key: string; split: number; of: Map<DomainId, number> }>();
  // The bulk bar (needsBulk.ts) keeps one node for the page's life: it is built with the first panel.
  let bulk: ReturnType<typeof createBulkBar> | null = null;
  const dismissedId = `nt-dismissed-${++tableSeq}`;
  const measure = options.measure ?? ((host: HTMLElement) => host.getBoundingClientRect().width);
  // The pane (needsPane.ts): its own opened row, the rows it shows (a held message paints for those), the
  // areas of the last draw it reads from, and a flag that says its rows are being built.
  let pane: Pane | null = null;
  let stopPicks: (() => void) | null = null;
  let paneOpen: string | null = null;
  let paneIds = new Set<string>();
  let paneBuild = false;
  let lastAreas: AreaGroup[] = [];
  /** Whether the last draw was narrowed to Assigned to me (the pane's empty lines say so). */
  let lastMine = false;
  const inWide = (): boolean => !scope && !!pane && (options.paneWide?.() ?? false);

  // The row message (rowMessages.ts): the opened row's own message line, or the page's when no row is open.
  const paintPane = (now: RowMessage | null): void => {
    const root = pane?.root;
    if (!root || !pane?.current()) return;
    root.querySelectorAll<HTMLElement>(".nt-panel-msg, .nt-page-msg").forEach((node) => node.replaceChildren());
    if (!now || !paneIds.has(now.id)) return;
    const line = [...root.querySelectorAll<HTMLElement>(".nt-panel-msg")].find((node) => node.dataset.row === now.id) ?? root.querySelector<HTMLElement>(".nt-pane-msg");
    line?.replaceChildren(now.line.cloneNode(true));
  };
  const paintMessage = (p: Panel, now: RowMessage | null, left: string | null): void => {
    paintPane(now);
    p.root.querySelectorAll<HTMLElement>(".nt-panel-msg, .nt-page-msg").forEach((node) => node.replaceChildren());
    // A phone's sheet shows the message of its own row, and (rather than dropping it) of a row behind it.
    if (sheet && (now || left === sheet.id)) sheet.msg.replaceChildren(...(now ? [now.line.cloneNode(true)] : []));
    if (!now) return;
    if (sheet) return;
    const row = [...p.root.querySelectorAll<HTMLElement>(".attn-row")].find((r) => r.dataset.id === now.id);
    const line = row?.querySelector<HTMLElement>(":scope .nt-panel-msg") ?? p.root.querySelector<HTMLElement>(".nt-page-msg");
    line?.replaceChildren(now.line.cloneNode(true));
  };
  const messages: RowMessages = createRowMessages(paintMessage);

  const clearOnAction = (p: Panel, id: string, event: Event): void => {
    if (messages.current()?.id !== id) return;
    const target = event.target as HTMLElement | null;
    // The row's own toggle is not an action: closing the open row keeps its message (it moves to the page line).
    if (target?.closest(".nt-toggle")) return;
    if (target?.closest("button, [role='menuitem']")) messages.set(p, id, null);
  };

  const visibleIds = (list: AreaGroup[]): Set<string> => new Set(list.flatMap((area) => area.items.map((item) => item.id)));

  const stopObserving = (): void => {
    rowObserver?.disconnect();
    rowObserver = null;
  };

  const removeSheet = (): void => {
    stopObserving();
    sheet?.node.remove();
    sheet = null;
  };

  /** A flash (an inline error under an action) appeared in a row: while that row's sheet is open it shows there too. */
  const showSheetMessage = (id: string, flash: HTMLElement): void => {
    if (sheet && sheet.id === id) sheet.msg.replaceChildren(flash.cloneNode(true));
  };

  const syncSheet = (p: Panel): void => {
    const want = pending;
    pending = null;
    if (!want || p.openRow !== want.id) {
      removeSheet();
      return;
    }
    if (sheet && sheet.id === want.id) {
      // The same row stays open: update in place, keep the node, its scroll and its focus.
      const active = document.activeElement as HTMLElement | null;
      const mark = active && sheet.node.contains(active) ? (active.dataset.focus ?? null) : null;
      sheet.node.dataset.tone = want.tone;
      sheet.title.textContent = want.title;
      // Only what the row shows now: an old error or note goes, and a flash the new row already
      // carries comes back (the observer fills it again for later ones).
      sheet.msg.replaceChildren();
      const current = want.rowText?.querySelector<HTMLElement>(":scope > .flash");
      if (current) sheet.msg.append(current.cloneNode(true));
      const held = messages.current();
      if (held) sheet.msg.append(held.line.cloneNode(true));
      sheet.scroller.replaceChildren(...want.main.childNodes);
      if (mark && !sheet.node.contains(document.activeElement)) {
        [...sheet.node.querySelectorAll<HTMLElement>("[data-focus]")].find((n) => n.dataset.focus === mark)?.focus({ preventScroll: true });
      }
      return;
    }
    // A sheet of another row goes; the observer finish just set for this row stays.
    sheet?.node.remove();
    sheet = null;
    const id = want.id;
    const titleId = "nt-sheet-title";
    // Where a Fix, Review or Merge error shows while the sheet is open (showSheetMessage).
    const msg = el("div", { class: "nt-sheet-msg", role: "status", "aria-live": "polite" });
    const heldNow = messages.current();
    if (heldNow) msg.append(heldNow.line.cloneNode(true));
    const title = el("h2", { class: "nt-sheet-title", id: titleId }, want.title);
    const node = el(
      "div",
      { class: "nt-sheet", role: "dialog", "aria-modal": "true", "aria-labelledby": titleId, "data-tone": want.tone },
      // .attn brings the panel's button and row styles; display: contents (style.css) keeps its frame out.
      el("div", { class: "attn nt-sheet-in" }, el("header", { class: "nt-sheet-top" }, title, want.close), msg, want.main),
    );
    // The handler is on the sheet itself, never on document: focus inside a confirmation opened from
    // the sheet (a separate .ae-dialog on body) is not pulled back into the sheet.
    // Any action in the sheet starts a new result: the row's old message goes first (capture, before the action runs).
    node.addEventListener("click", (event) => clearOnAction(p, id, event), true);
    node.addEventListener("keydown", (event) => {
      if (event.key === "Escape") {
        if (p.openRow !== id) return;
        event.preventDefault();
        event.stopPropagation();
        toggleRow(p, id);
        return;
      }
      if (event.key !== "Tab") return;
      const items = [...node.querySelectorAll<HTMLElement>("button, [href], input, select, textarea, [tabindex]")].filter(
        (n) => !n.hasAttribute("disabled") && n.getAttribute("tabindex") !== "-1" && !n.closest("[hidden]"),
      );
      const first = items[0];
      const last = items[items.length - 1];
      if (!first || !last) return;
      if (event.shiftKey && (document.activeElement === first || !node.contains(document.activeElement))) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && (document.activeElement === last || !node.contains(document.activeElement))) {
        event.preventDefault();
        first.focus();
      }
    });
    document.body.append(node);
    sheet = { id, node, msg, title, scroller: want.main };
    // A different row opened: focus moves to Close, and only now.
    want.close.focus({ preventScroll: true });
  };

  const toggleRow = (p: Panel, id: string, inPane = false): void => {
    const closing = (inPane ? paneOpen : p.openRow) === id;
    // Opening a different row clears the message; the same row keeps it, opened or closed by its toggle (it moves between the page line and the panel).
    const held = messages.current();
    if (held && held.id !== id && !closing) messages.set(p, held.id, null);
    if (inPane) paneOpen = closing ? null : id;
    else p.openRow = closing ? null : id;
    redrawPanel(p, true);
    // Closing hands the focus back to the row that was open (a sheet's Close is gone by now).
    if (closing) {
      const back = [...(inPane ? (pane?.root ?? p.root) : p.root).querySelectorAll<HTMLElement>("[data-focus]")].find((n) => n.dataset.focus === `${id}:toggle`);
      back?.focus({ preventScroll: true });
    } else if (inPane) {
      // Opening a row in the pane moves the focus to its Close, under the rows.
      [...(pane?.root.querySelectorAll<HTMLElement>("[data-focus]") ?? [])].find((n) => n.dataset.focus === `${id}:dclose`)?.focus({ preventScroll: true });
    } else {
      // Opening a phone's sheet moves the focus into it, to Close.
      document.querySelector<HTMLElement>(".nt-sheet .nt-close")?.focus({ preventScroll: true });
    }
  };

  // ── a row ─────────────────────────────────────────────────────────────────

  const shell: TableHooks["shell"] = (p, item, meta) => {
    const tone = item.tone === "urgent" ? "red" : "amber";
    const inPane = paneBuild;
    const open = (inPane ? paneOpen : p.openRow) === item.id;
    // The panel exists only while the row is open, so aria-controls is set then (finish) and not here.
    // The button's name is the item's own title, so the name holds the words a sighted person reads.
    const toggle = el(
      "button",
      {
        type: "button",
        class: "nt-toggle",
        "aria-expanded": String(open),
        title: open ? "Close this item" : "Open this item",
        "data-focus": `${item.id}:toggle`,
      },
      chevronIcon(),
      el("span", { class: "sr-only" }, meta ? `${item.title}, ${meta}` : item.title),
    );
    const text = el("div", { class: "attn-text" });
    const readAt = lastData?.read_at ?? "";
    const checked = agoText(readAt);
    const actions = el("div", { class: "attn-actions" });
    const cell = el("div", { class: "nt-cell" }, actions);
    const age = ageText(item);
    const row = el(
      "div",
      {
        class: `bh-need attn-row nt-row bh-t-${tone}${open ? " is-open" : ""}`,
        "data-key": `row:${item.id}`,
        "data-id": item.id,
        "data-kind": item.kind,
        "data-tone": item.tone,
      },
      pickBox(item),
      toggle,
      text,
      el(
        "span",
        { class: "nt-age", title: item.first_seen ? `On the list since ${clock(item.first_seen)}` : "How long it has been on the list" },
        el("span", { class: "sr-only" }, "Age: "),
        age,
      ),
      el(
        "span",
        { class: "nt-checked", title: readAt ? `The list was read at ${clock(readAt)}` : undefined },
        el("span", { class: "sr-only" }, "Checked: "),
        checked,
      ),
      cell,
    );
    text.append(el("span", { class: `bh-dot bh-${tone}`, "aria-hidden": "true" }));
    const prefix = (item as TableItem).kindPrefix;
    if (prefix) text.append(el("span", { class: "nt-kind" }, `${prefix} · `));
    toggle.addEventListener("click", () => toggleRow(p, item.id, inPane));
    // Any action on the row, opened or closed (header buttons, the panel's buttons), starts a new result: the old
    // message goes first (capture, before the action runs).
    row.addEventListener("click", (event) => clearOnAction(p, item.id, event), true);
    row.addEventListener("click", (event) => {
      const target = event.target as HTMLElement;
      if (target.closest("a, button, input, label, select, textarea, .menu, .pr-confirm, .nt-panel, .attn-working")) return;
      toggleRow(p, item.id, inPane);
    });
    // Age and checked again on the second line when their columns are folded (a narrow page).
    if (checked) text.dataset.checked = checked;
    const fold = [age ? `Age ${age}` : "", checked ? `Checked ${checked}` : ""].filter(Boolean).join(" · ");
    if (fold) text.dataset.fold = fold;
    return { row, text, actions };
  };

  const finish: TableHooks["finish"] = (p, row, item, data) => {
    const inPane = paneBuild;
    const open = (inPane ? paneOpen : p.openRow) === item.id;
    const cell = row.querySelector<HTMLElement>(":scope > .nt-cell");
    const actions = row.querySelector<HTMLElement>(".attn-actions");
    const toggle = row.querySelector<HTMLButtonElement>(":scope > .nt-toggle");
    if (!cell || !actions || !toggle) return;
    row.classList.toggle("is-open", open);
    toggle.setAttribute("aria-expanded", String(open));
    // A pane's panel id carries a prefix so no id can collide with a list row's (a row may be in both places).
    const pid = `${inPane ? "pane-" : ""}nt-p-${slug(item.id)}`;
    if (open) toggle.setAttribute("aria-controls", pid);
    else toggle.removeAttribute("aria-controls");
    toggle.title = open ? "Close this item" : "Open this item";
    row.querySelector(":scope > .nt-panel")?.remove();
    row.querySelector(":scope > .nt-cell > .nt-close")?.remove();

    // The assignee's initials next to the title, and a row that is another admin's, dimmed a little
    // (it stays in place; the counts are the server's).
    row.querySelector(":scope > .attn-text .nt-assignee")?.remove();
    const chip = assigneeChip(item, data.me);
    if (chip) row.querySelector(":scope > .attn-text .attn-title")?.after(chip);
    row.classList.toggle("is-elsewhere", assignedElsewhere(item, data.me));

    // The primary action is the first of Merge, a cleanup, Fix, Review that the row offers.
    // A Social row has no generic Dismiss: it only hides the row, while the Social card counts the post
    // from the plan. The opened row's Skip post (the card's own skip) takes its place; the closed row shows
    // Copy text instead and never Skip post or Posted. Snooze stays on both.
    const social = item.source === "social" && Boolean(item.slot);
    if (social && !actions.querySelector('[data-focus$=":skip"], [data-focus$=":copy"]')) {
      const generic = [...actions.querySelectorAll<HTMLButtonElement>("button[data-focus]")].find((node) => node.dataset.focus?.endsWith(":dismiss"));
      if (generic) {
        generic.replaceWith(open ? skipButton(p, item) : closedCopyButton(p, item));
        // A closed row marks its post old, so the next opening reads it again.
        if (!open) markStale(item.slot as string);
      }
    }
    const buttons = [...actions.querySelectorAll<HTMLButtonElement>("button[data-focus]")];
    const kindOf = (node: HTMLElement) => node.dataset.focus?.slice(node.dataset.focus.lastIndexOf(":")) ?? "";
    const primary = PRIMARY_ORDER.map((suffix) => buttons.find((node) => kindOf(node) === suffix)).find(Boolean);
    for (const node of buttons) {
      const kind = kindOf(node).slice(1);
      node.toggleAttribute("data-primary", node === primary);
      if (!node.querySelector(".nt-label") && !node.classList.contains("attn-clean")) {
        const word = item.source === "home" && kind === "review" ? "Investigate" : LABELS[kind];
        if (word) {
          node.append(el("span", { class: "nt-label" }, word));
          node.classList.remove("btn-icon");
          node.classList.add("nt-labelled");
        }
      }
    }
    if (primary) primary.classList.add("nt-primary");
    // A row with no Merge, Clean up, Fix or Review (a box admin's, where chats are off) still has
    // something to do: its Dismiss is the closed row's action, labelled like the others.
    else {
      const only = buttons.find((node) => kindOf(node) === ":dismiss" || (social && kindOf(node) === ":copy"));
      only?.toggleAttribute("data-primary", true);
    }

    // Snooze sits in the row's own menu (the opened panel shows its three choices instead, and
    // style.css hides the menu then). Only an item the server can snooze gets one. It always goes
    // last, so the clock sits at the far right on every row, also where Dismiss is the row's action.
    if (!actions.querySelector(".nt-snooze-menu")) {
      const menu = snoozeMenu(p, item);
      if (menu) actions.append(menu);
    }

    if (!open) {
      cell.replaceChildren(actions);
      return;
    }

    // Open: the actions move into the panel, and the cell holds Close.
    const close = button("", { title: "Close this item", onClick: () => toggleRow(p, item.id, inPane) });
    close.classList.add("nt-close", "btn-quiet");
    // On a phone the panel is a sheet with its own Close, which is no disclosure of the row; on a
    // desktop the row's Close is the disclosure's control while the panel exists.
    const phone = isPhone();
    if (!phone) {
      close.setAttribute("aria-expanded", "true");
      close.setAttribute("aria-controls", pid);
    }
    close.setAttribute("aria-label", `Close ${item.title}`);
    close.replaceChildren(closeIcon(), el("span", {}, "Close"));
    close.dataset.focus = `${item.id}:close`;
    cell.replaceChildren(...(phone ? [] : [close]));

    const dismiss = buttons.find((node) => kindOf(node) === ":dismiss" || kindOf(node) === ":skip");
    const extras: HTMLElement[] = [];
    if (item.url.startsWith("https://github.com/") && !actions.querySelector('[data-extra="github"]')) {
      const github = button("", { title: "Open this on GitHub in a new tab", onClick: () => void openOrShow(p.context, item.url, row, (line) => messages.set(p, item.id, line)) });
      github.classList.add("nt-labelled");
      github.dataset.extra = "github";
      github.dataset.focus = `${item.id}:github`;
      github.replaceChildren(externalLinkIcon(), el("span", { class: "nt-label" }, "Open on GitHub"));
      extras.push(github);
    }
    const card = item.source === "social" ? null : cardOf(item.source);
    if (card && !actions.querySelector('[data-extra="card"]')) {
      const open = button("", {
        title: `Show the ${SOURCE_NAMES[item.source]} card`,
        onClick: () => options.openCard(card, AREA_OF[item.source]),
      });
      open.classList.add("nt-labelled");
      open.dataset.extra = "card";
      open.dataset.focus = `${item.id}:card`;
      open.replaceChildren(layoutGridIcon(16), el("span", { class: "nt-label" }, "Open card"));
      extras.push(open);
    }
    if (item.source === "social" && item.slot && !actions.querySelector('[data-extra="post"]')) {
      const slot = item.slot;
      const post = button("", {
        title: "Open the Social card and go to this post",
        onClick: () => {
          options.openCard("social", AREA_OF.social);
          showPost(slot);
        },
      });
      post.classList.add("nt-labelled");
      post.dataset.extra = "post";
      post.dataset.focus = `${item.id}:post`;
      post.replaceChildren(layoutGridIcon(16), el("span", { class: "nt-label" }, "Open the post"));
      extras.push(post);
    }
    // Social: Copy text, Open on LinkedIn, Posted, Copy picture, Download picture lead the toolbar (the card's
    // own actions), then Open the post, Assign, Snooze, and Skip post last on the right.
    const sr = social ? socialRow(p, item) : null;
    if (sr) {
      for (const node of sr.buttons) {
        if (dismiss) actions.insertBefore(node, dismiss);
        else actions.appendChild(node);
      }
      sr.buttons[0]?.toggleAttribute("data-primary", true);
      sr.buttons[0]?.classList.add("nt-primary");
    }
    for (const node of extras) {
      if (dismiss) actions.insertBefore(node, dismiss);
      else actions.appendChild(node);
    }
    if (dismiss) dismiss.classList.add("nt-dismiss");
    // The primary action leads the row of actions.
    if (primary && actions.firstElementChild !== primary) actions.insertBefore(primary, actions.firstChild);

    const read = readCardOf(item);
    const mergeExtra: Fact[] = read
      ? [
          { label: "Checks", value: `${read.card.ci.passing} of ${read.card.ci.total} passing` },
          { label: "Approvals", value: String(read.card.review.approvals) },
          { label: "Mergeable", value: read.card.mergeable.detail || read.card.mergeable.state },
        ]
      : [];
    const facts = factsOf(item, mergeExtra, (days) => comesBackOn(days));
    const hint = sr
      ? sr.hint
      : item.dismissible
      ? `Dismiss hides this until ${item.back_if || "it gets worse"}, or until ${comesBackOn(item.expires_days)}.`
      : "";
    const panelId = pid;
    const acts = el("div", { class: "nt-acts" }, actions);
    const picture = sr ? null : socialPicture(p.context, item);
    const evidence = el("p", { class: "nt-evidence" }, evidenceText(item, clock(data.read_at), read ? mergeFacts(read.card) : null));
    const factsNode = el(
      "div",
      { class: "nt-facts" },
      ...facts.map((fact) =>
        el("div", { class: "nt-fact" }, el("span", { class: "nt-fact-k" }, fact.label), el("span", { class: "nt-fact-v" }, fact.value)),
      ),
    );
    const hintNode = hint ? el("p", { class: "nt-hint meta" }, hint) : null;
    let mainBody: HTMLElement;
    if (phone) {
      // The sheet keeps its one column: evidence, facts, the pinned actions, then Assign and Snooze in their own boxes.
      mainBody = el(
        "div",
        { class: "nt-panel-main" },
        evidence,
        sr?.post,
        sr?.picture,
        factsNode,
        acts,
        assignBlock(p, item, data.can_chat, data.me),
        snoozeBlock(p, item),
        picture,
        hintNode,
      );
    } else {
      // Layout B (2026-11-16, the owner's pick): one toolbar across the top holds every action, the primary
      // first, the secondary ones after it, then Snooze as a dropdown and Dismiss last on the right. Below it
      // two columns: the evidence, facts, hint and (Social) picture on the left, the history on the right.
      actions.querySelectorAll(":scope > .nt-assign, :scope > .nt-snooze-drop").forEach((node) => node.remove());
      const assign = assignBlock(p, item, data.can_chat, data.me, true);
      const drop = snoozeDropdown(p, item);
      for (const node of [assign, drop]) {
        if (!node) continue;
        if (dismiss) actions.insertBefore(node, dismiss);
        else actions.appendChild(node);
      }
      const bar = el("div", { class: "nt-toolbar" }, acts);
      if (sr) {
        // A Social row (the owner's pick, 2026-11-16): the evidence across the full width, then three columns,
        // the post text, the picture with its buttons, and the facts over the history. Each piece is its own
        // element (nt-post, nt-picbox, nt-facts, nt-hist), so a layout moves them without touching the code.
        const cols = el(
          "div",
          { class: "nt-b3" },
          el("div", { class: "nt-b3-ev" }, evidence),
          el("div", { class: "nt-b3-col nt-b3-post" }, sr.post, hintNode),
          el("div", { class: "nt-b3-col nt-b3-pic" }, sr.picture),
          el("div", { class: "nt-b3-col nt-b3-side" }, factsNode),
        );
        mainBody = el("div", { class: "nt-panel-main" }, bar, cols);
      } else {
        const left = el("div", { class: "nt-left" }, evidence, factsNode, hintNode, picture);
        mainBody = el("div", { class: "nt-panel-main" }, bar, el("div", { class: "nt-body" }, left));
      }
    }
    const history = historyBox(p.context, item);
    let panel: HTMLElement | null = null;
    if (phone) {
      // The Merge confirm is built into the row; on a phone it belongs in the sheet, above the actions.
      const confirm = row.querySelector<HTMLElement>(":scope > .pr-confirm");
      if (confirm) mainBody.insertBefore(confirm, acts);
      const rowText = row.querySelector<HTMLElement>(":scope > .attn-text");
      mainBody.append(history);
      pending = { id: item.id, tone: item.tone, title: item.title, close, main: mainBody, rowText };
      // attention.ts flashes errors into the row's text, which is behind the sheet: mirror them. One
      // observer at a time; a rebuilt row replaces the one of the row it replaced.
      stopObserving();
      if (rowText && typeof MutationObserver !== "undefined") {
        rowObserver = new MutationObserver(() => {
          const found = rowText.querySelector<HTMLElement>(":scope > .flash");
          if (found) showSheetMessage(item.id, found);
          else if (sheet && sheet.id === item.id) sheet.msg.replaceChildren();
        });
        rowObserver.observe(rowText, { childList: true });
      }
      // A rebuild outside a table draw (the Merge flow) updates the sheet at once.
      if (!drawing) syncSheet(p);
    } else {
      (mainBody.querySelector(".nt-b3-side") ?? mainBody.querySelector(".nt-body"))?.append(history);
      // The row's message line: full width under the toolbar, drawn from state so a redraw keeps it.
      const msgLine = el("div", { class: "nt-panel-msg", role: "status", "aria-live": "polite", "data-row": item.id });
      const held = messages.current();
      if (held && held.id === item.id) msgLine.append(held.line.cloneNode(true));
      mainBody.insertBefore(msgLine, mainBody.querySelector(".nt-toolbar")?.nextSibling ?? null);
      panel = el("div", { class: `nt-panel nt-b${sr ? " nt-panel-social" : ""}`, id: panelId, role: "group", "aria-label": `${item.title}, details` }, mainBody);
    }
    if (panel) row.appendChild(panel);
  };

  // ── groups ────────────────────────────────────────────────────────────────

  const fold = (p: Panel, id: string, next: boolean): void => {
    saveFold(id, next);
    redrawPanel(p, true);
  };

  /**
   * A click on an area or source line. At the wide band it fills the pane (a second click on the selected
   * line closes it) and the line's saved open or closed state is left alone; below it, the line folds as before.
   */
  const onLine = (p: Panel, target: PaneTarget, id: string, next: boolean): void => {
    if (!inWide()) {
      fold(p, id, next);
      return;
    }
    paneOpen = null;
    if (sameTarget(pane!.current(), target)) pane!.clear(true);
    else pane!.show(target);
  };
  const selected = (target: PaneTarget): boolean => sameTarget(pane?.current() ?? null, target);

  /** "Show N more" under a source's rows (the list's and the pane's). */
  function moreNode(p: Panel, block: Block, rest: number, expanded: boolean): HTMLElement {
    const more = el(
      "button",
      {
        type: "button",
        class: "linkish attn-more nt-more",
        "aria-expanded": String(expanded),
        title: expanded ? "Show only the first rows again" : "Show every row in this source",
        "data-focus": `${block.id}:more`,
      },
      expanded ? "Show fewer" : `Show ${rest} more`,
    );
    more.addEventListener("click", () => {
      if (p.more.has(block.id)) p.more.delete(block.id);
      else p.more.add(block.id);
      redrawPanel(p, true);
    });
    return more;
  }

  /** The arrow at a wide source line's right end: decorative, but a click on it is a click on the line. */
  function opensArrow(line: HTMLElement): HTMLElement {
    const arrow = el("span", { class: "nt-opens", "aria-hidden": "true" }, arrowRightIcon());
    arrow.addEventListener("click", () => line.click());
    return arrow;
  }

  /** The dismissed link's accessible name, built in one place: "Show 9 dismissed items" or "Hide 1 dismissed item". */
  const dismissedWords = (n: number, open: boolean): string => `${open ? "Hide" : "Show"} ${n} dismissed ${n === 1 ? "item" : "items"}`;

  /** The "N dismissed" link: a sibling after the counts (never inside the clipped counts span), set by drawTable. */
  function dismissedLink(p: Panel, n: number): HTMLElement {
    const link = el("button", {
      type: "button",
      class: "nt-dlink",
      "aria-expanded": String(p.dismissedOpen),
      "aria-controls": dismissedId,
      "aria-label": dismissedWords(n, p.dismissedOpen),
      title: "Items you hid. Restore brings one back.",
      "data-focus": "dismissed:link",
    });
    link.textContent = `${n} dismissed`;
    link.addEventListener("click", () => setDismissed(p, !p.dismissedOpen, n));
    return link;
  }

  function setDismissed(p: Panel, open: boolean, n: number): void {
    p.dismissedOpen = open;
    const link = p.root.querySelector<HTMLElement>(".nt-dlink");
    link?.setAttribute("aria-expanded", String(open));
    link?.setAttribute("aria-label", dismissedWords(n, open));
    const box = p.root.querySelector<HTMLElement>(`#${dismissedId}`);
    if (box) box.hidden = !open;
  }

  /** The dismissed list at the end of the card: inside the card's padding, no box, Restore on every row. */
  function dismissedSection(p: Panel, items: AttentionItem[]): HTMLElement | null {
    if (!items.length) return null;
    const hide = el("button", { type: "button", class: "nt-dhide", "aria-label": dismissedWords(items.length, true) }, "Hide");
    hide.addEventListener("click", () => {
      setDismissed(p, false, items.length);
      p.root.querySelector<HTMLElement>(".nt-dlink")?.focus();
    });
    return el(
      "div",
      { class: "nt-dismissed", id: dismissedId, hidden: !p.dismissedOpen },
      el("div", { class: "nt-dhead" }, el("span", {}, `Dismissed ${items.length}`), el("span", { class: "nt-dsp" }), hide),
      dismissedList(p, items, `${dismissedId}-list`, false),
    );
  }

  function sourceNode(
    p: Panel,
    area: AreaGroup,
    entry: AreaGroup["sources"][number],
    open: boolean,
    data: AttentionOverview,
    unread: boolean,
  ): HTMLElement {
    const block: Block = entry.block;
    const bodyId = `nt-b-${slug(block.id)}`;
    const expanded = p.more.has(block.id);
    const { shown, rest } = rowsShown(block.items, expanded);
    const several = hasSeveralKinds(block.items);
    const wide = inWide();
    const head = el(
      "button",
      {
        type: "button",
        class: "nt-grp-btn",
        "aria-expanded": String(open),
        "aria-controls": bodyId,
        "data-focus": `${block.id}:fold`,
      },
      // At the wide band a source line opens the pane on the right: no chevron (the line keeps its indent) and an
      // arrow at the line's right end; below it the line folds in place and keeps its chevron (2026-11-18).
      wide ? null : el("span", { class: "nt-chev", "aria-hidden": "true" }, chevronIcon()),
      el("span", { class: "nt-grp-name" }, SOURCE_NAMES[block.source]),
      pills(...sourcePills(block)),
      unread ? el("span", { class: "nt-unread", title: `${SOURCE_NAMES[block.source]} could not be read on the last refresh, so its rows may be out of date. It tries again on the next refresh.` }, "?") : null,
      !open && newMarks.has(block.id) ? newMarker() : null,
      open ? null : el("span", { class: "nt-preview" }, sourcePreview(block)),
    );
    head.addEventListener("click", () => onLine(p, { kind: "source", id: block.source }, block.id, !open));
    const real = block.groups.filter((group) => group.key !== "other");
    const review = reviewAllNode(p, block, { source: block.source }, real.flatMap((group) => group.items), data, "source");
    const rows = el(
      "div",
      { class: "nt-rows", id: bodyId, hidden: !open },
      ...shown.map((item) => {
        // The kind prefix is set on a copy, so the shared item stays as the server sent it.
        const withKind: TableItem = several ? { ...item, kindPrefix: kindName(item) } : item;
        return rowNode(p, withKind, data);
      }),
    );
    if (block.items.length > shown.length || expanded) rows.appendChild(moreNode(p, block, rest, expanded));
    const mine = selected({ kind: "source", id: block.source });
    if (mine) head.setAttribute("aria-current", "true");
    return el(
      "div",
      { class: `nt-grp nt-source${mine ? " is-selected" : ""}`, "data-line": block.id, "data-source": block.source, "data-area": area.id },
      el(
        "div",
        { class: `nt-grp-head${wide ? " has-opens" : ""}` },
        groupPickBox(block.items.map((i) => i.id), SOURCE_NAMES[block.source]),
        head,
        wide ? el("div", { class: "nt-slot" }, review) : review,
        wide ? opensArrow(head) : null,
      ),
      rows,
    );
  }

  function areaNode(
    p: Panel,
    area: AreaGroup,
    open: boolean,
    openOf: (id: string) => boolean,
    data: AttentionOverview,
    unread: Set<AttentionSource>,
  ): HTMLElement {
    const id = areaFoldId(area.id);
    const bodyId = `nt-b-${slug(id)}`;
    const domain = DOMAINS.find((d) => d.id === area.id)!;
    // At the wide band the line opens the pane and only the chevron folds, so only the chevron carries the fold state;
    // below it the line folds too and keeps `aria-expanded` and `aria-controls` (2026-11-18, review).
    const lineFolds = !inWide();
    const head = el(
      "button",
      {
        type: "button",
        class: "nt-grp-btn",
        ...(lineFolds ? { "aria-expanded": String(open), "aria-controls": bodyId } : {}),
        "data-focus": `${id}:fold`,
      },
      el("span", { class: "nt-grp-icon", "aria-hidden": "true" }, domain.icon()),
      el("span", { class: "nt-grp-name" }, area.label),
      pills(...areaPills(area)),
      !open && newMarks.has(id) ? newMarker() : null,
      open ? null : el("span", { class: "nt-preview" }, areaPreview(area)),
    );
    head.addEventListener("click", () => onLine(p, { kind: "area", id: area.id }, id, !open));
    // The chevron is its own button (2026-11-18): it folds the area in place at every width, while the rest of
    // the line keeps its action (the pane at the wide band, a fold below it).
    const chev = el(
      "button",
      {
        type: "button",
        class: "nt-fold",
        "aria-expanded": String(open),
        "aria-controls": bodyId,
        "aria-label": `${open ? "Fold" : "Unfold"} ${area.label}`,
        title: `${open ? "Fold" : "Unfold"} ${area.label}`,
        "data-focus": `${id}:chev`,
      },
      chevronIcon(),
    );
    chev.addEventListener("click", () => fold(p, id, !open));
    const mine = selected({ kind: "area", id: area.id });
    if (mine) head.setAttribute("aria-current", "true");
    const go = el(
      "button",
      { type: "button", class: "btn btn-secondary nt-go", title: `Open the ${area.label} tab`, "aria-label": `Open the ${area.label} tab` },
      el("span", { class: "nt-label" }, `${area.label} tab`),
      // At the wide band the line ends in the shared arrow column instead (2026-11-19), so the button has no icon.
      inWide() ? null : arrowRightIcon(),
    );
    go.addEventListener("click", () => options.openTab(area.id));
    return el(
      "div",
      { class: `nt-grp nt-area${mine ? " is-selected" : ""}`, "data-line": id, "data-area": area.id },
      el(
        "div",
        { class: `nt-grp-head${inWide() ? " has-opens" : ""}` },
        groupPickBox(
          area.sources.flatMap((entry) => entry.block.items.map((i) => i.id)),
          area.label,
        ),
        chev,
        head,
        inWide() ? el("div", { class: "nt-slot" }, options.hasTab(area.id) ? go : null) : options.hasTab(area.id) ? go : null,
        inWide() ? opensArrow(head) : null,
      ),
      el(
        "div",
        { class: "nt-grp-body", id: bodyId, hidden: !open },
        ...area.sources.map((entry) => sourceNode(p, area, entry, openOf(entry.block.id), data, unread.has(entry.source))),
      ),
    );
  }

  // ── the pane ──────────────────────────────────────────────────────────────

  const focusLine = (target: PaneTarget): void => {
    if (!current) return;
    const root = current.root;
    const find = (key: string): HTMLElement | undefined =>
      [...root.querySelectorAll<HTMLElement>("[data-focus]")].find((n) => n.dataset.focus === key && !n.closest("[hidden]"));
    const sourceLine = target.kind === "source" ? lastAreas.flatMap((area) => area.sources).find((entry) => entry.source === target.id)?.block.id : undefined;
    const area = target.kind === "area" ? target.id : AREA_OF[target.id];
    // The line that opened the pane; its area's line when that one is folded away or gone; then the list heading.
    const node = (sourceLine ? find(`${sourceLine}:fold`) : undefined) ?? find(`${areaFoldId(area)}:fold`);
    if (node) {
      node.focus({ preventScroll: true });
      return;
    }
    const heading = root.querySelector<HTMLElement>("#needs-you-h, .attn-h");
    if (heading) {
      heading.tabIndex = -1;
      heading.focus({ preventScroll: true });
    }
  };

  /** The pane's rows for one source: this file's own row code, with the pane's opened row. */
  function paneSection(p: Panel, data: AttentionOverview, block: Block, heading: boolean, panels: Array<{ item: AttentionItem; panel: HTMLElement }>): { section: PaneSection; review: HTMLElement | null } {
    const expanded = p.more.has(block.id);
    const { shown, rest } = rowsShown(block.items, expanded);
    const several = hasSeveralKinds(block.items);
    const build = (item: AttentionItem): HTMLElement => {
      const withKind: TableItem = several ? { ...item, kindPrefix: kindName(item) } : item;
      const row = rowNode(p, withKind, data);
      // Stable focus keys for the row's own controls, so a redraw can give the focus back (the title link; the
      // checkbox and the buttons carry theirs).
      row.querySelectorAll<HTMLElement>(":scope > .attn-text a:not([data-focus])").forEach((link, i) => (link.dataset.focus = `${item.id}:link${i}`));
      // The opened row's panel goes under the rows, not inside the row.
      const panel = row.querySelector<HTMLElement>(":scope > .nt-panel");
      if (panel && paneOpen === item.id) {
        panel.remove();
        panels.push({ item, panel });
      }
      return row;
    };
    paneBuild = true;
    try {
      const rows = el("div", { class: "nt-rows" }, ...shown.map(build));
      // An opened row past the "Show N more" cap keeps its panel (its row stays behind the cap).
      const hidden = paneOpen ? block.items.find((item) => item.id === paneOpen && !shown.includes(item)) : undefined;
      if (hidden) build(hidden);
      for (const item of block.items) paneIds.add(item.id);
      if (!block.items.length) {
        const words = lastMine ? `Nothing in ${SOURCE_NAMES[block.source]} is assigned to you` : `Nothing needs you in ${SOURCE_NAMES[block.source]} now`;
        rows.append(el("p", { class: "attn-empty" }, words));
      }
      if (block.items.length > shown.length || expanded) rows.appendChild(moreNode(p, block, rest, expanded));
      const real = block.groups.filter((group) => group.key !== "other");
      const review = reviewAllNode(p, block, { source: block.source }, real.flatMap((group) => group.items), data, "source");
      return {
        section: { name: heading ? SOURCE_NAMES[block.source] : null, pills: heading ? pills(...sourcePills(block)) : null, review: heading ? review : null, rows },
        review,
      };
    } finally {
      paneBuild = false;
    }
  }

  const paneView = (target: PaneTarget): PaneView => {
    paneIds = new Set();
    const p = current;
    const data = lastData;
    const label = target.kind === "source" ? SOURCE_NAMES[target.id] : (DOMAINS.find((d) => d.id === target.id)?.label ?? "");
    const area = target.kind === "area" ? lastAreas.find((a) => a.id === target.id) : undefined;
    if (!p || !data) {
      return { heading: { icon: null, name: label, pills: null, review: null }, sections: [{ name: null, pills: null, review: null, rows: el("p", { class: "meta" }, "Reading...") }], detail: null, card: null };
    }
    const panels: Array<{ item: AttentionItem; panel: HTMLElement }> = [];
    let heading: PaneView["heading"];
    let sections: PaneSection[];
    let card: PaneView["card"] = null;
    if (target.kind === "area") {
      const domain = DOMAINS.find((d) => d.id === target.id);
      const sources = area?.sources ?? [];
      sections = sources.map((entry) => paneSection(p, data, entry.block, true, panels).section);
      if (!sections.length) {
        const words = lastMine ? `Nothing in ${label} is assigned to you.` : `Nothing needs you in ${label}.`;
        sections = [{ name: null, pills: null, review: null, rows: el("p", { class: "attn-empty" }, words) }];
      }
      heading = {
        icon: domain ? domain.icon() : null,
        name: label,
        pills: area ? pills(...areaPills(area)) : null,
        review: null,
      };
    } else {
      const entry = lastAreas.flatMap((a) => a.sources).find((e) => e.source === target.id);
      const block: Block =
        entry?.block ?? ({ id: `attn.${target.id}`, source: target.id, items: [], groups: [], urgent: 0, review: 0, working: 0 } as unknown as Block);
      const built = paneSection(p, data, block, false, panels);
      sections = [built.section];
      const cardId = cardOf(target.id);
      const node = cardId ? (options.cardNode?.(cardId) ?? null) : null;
      if (cardId && node) {
        card = { id: cardId, node, label: `${label} card` };
      }
      const badge = node?.querySelector<HTMLElement>(".admin-badge")?.cloneNode(true) as Element | null | undefined;
      badge?.querySelectorAll("[id]").forEach((n) => n.removeAttribute("id"));
      const domain = DOMAINS.find((d) => d.id === AREA_OF[target.id]);
      heading = {
        icon: badge ?? (domain ? domain.icon() : null),
        name: label,
        pills: pills(...sourcePills(block)),
        review: built.review,
      };
    }
    let detail: HTMLElement | null = null;
    const opened = panels[0];
    if (opened) {
      const { item, panel } = opened;
      const tone = item.tone === "urgent" ? "red" : "amber";
      const close = button("", { title: "Close this item", onClick: () => toggleRow(p, item.id, true) });
      close.classList.add("btn-quiet", "nt-close", "nt-detail-close");
      close.setAttribute("aria-label", `Close ${item.title}`);
      close.dataset.focus = `${item.id}:dclose`;
      close.replaceChildren(closeIcon(), el("span", { class: "nt-label" }, "Close"));
      const prefix = (item as TableItem).kindPrefix;
      detail = el(
        "section",
        { class: `nt-pane-detail bh-t-${tone}`, "aria-label": `${item.title}, opened` },
        el(
          "div",
          { class: "nt-detail-t" },
          el("span", { class: `bh-dot bh-${tone}`, "aria-hidden": "true" }),
          el("div", { class: "nt-detail-text" }, el("span", { class: "attn-title" }, prefix ? `${prefix} · ${item.title}` : item.title), item.meta ? el("span", { class: "attn-meta" }, item.meta) : null),
          close,
        ),
        panel,
      );
      // The panel's buttons are no longer inside the row, so the row's "any action starts a new result" listener
      // is set here too (capture, before the action runs).
      detail.addEventListener("click", (event) => clearOnAction(p, item.id, event), true);
    }
    return { heading, sections, detail, card };
  };

  const paneDeps: NeedsTable["paneDeps"] = {
    view: paneView,
    onClear: () => {
      paneOpen = null;
    },
    redraw: () => {
      if (!current) return false;
      redrawPanel(current, true);
      return true;
    },
    focusLine,
  };

  // ── the composition bar ───────────────────────────────────────────────────

  function compositionBar(areas: AreaGroup[], data: AttentionOverview, filter: AttentionSource | null): HTMLElement | null {
    const unread = unreadSources(data);
    const segs: HTMLElement[] = [];
    const sources = new Map<AttentionSource, Block>();
    for (const area of areas) for (const entry of area.sources) sources.set(entry.source, entry.block);
    // A source that could not be read still gets a (dashed) segment, even with no rows.
    for (const source of unread) {
      if (!sources.has(source) && (!scope || AREA_OF[source] === scope)) sources.set(source, null as unknown as Block);
    }
    for (const [source, block] of sources) {
      // The same counts as the pills (by_source, with held-by-chat items added), not the listed rows.
      const [u, r, w] = sourcePills(block ?? { source, urgent: 0, review: 0, working: 0 });
      const n = u + r + w;
      const parts: HTMLElement[] = [];
      const bar = (cls: string, count: number) => {
        if (count > 0) parts.push(el("i", { class: cls, style: `flex-grow:${count}` }));
      };
      bar("nt-bar-urgent", u);
      bar("nt-bar-review", r);
      bar("nt-bar-chat", w);
      if (unread.has(source)) bar("nt-bar-unread", Math.max(1, Math.ceil(n / 4)));
      const seg = el(
        "button",
        {
          type: "button",
          class: `nt-seg${filter === source ? " is-on" : ""}${filter && filter !== source ? " is-off" : ""}`,
          style: `flex-grow:${Math.max(n, 1)}`,
          "aria-pressed": String(filter === source),
          "data-focus": `seg:${source}`,
          title: `Show only ${SOURCE_NAMES[source]}`,
          "aria-label": `${SOURCE_NAMES[source]}, ${unread.has(source) && n === 0 ? "not read" : `${n} ${n === 1 ? "item" : "items"}`}${unread.has(source) && n > 0 ? ", could not be read" : ""}. Show only this source`,
        },
        el("span", { class: "nt-seg-lab" }, el("b", {}, SOURCE_NAMES[source]), unread.has(source) && n === 0 ? " not read" : ` ${n}`),
        el("span", { class: "nt-bar" }, ...parts),
      );
      seg.addEventListener("click", () => setFilter(filter === source ? null : source));
      segs.push(seg);
    }
    return segs.length ? el("div", { class: "nt-comp", role: "group", "aria-label": "Needs you by source" }, ...segs) : null;
  }

  // ── the whole table ───────────────────────────────────────────────────────

  const drawTable: TableHooks["draw"] = (p, { data, error, filter, phone }) => {
    current = p;
    lastData = data;
    pending = null;
    drawing = true;
    const counts = part(p, ".attn-counts");
    const tools = part(p, ".attn-tools");
    const notes = part(p, ".attn-notes");
    const body = part(p, ".attn-body");
    p.root.classList.add("nt");
    let comp = p.root.querySelector<HTMLElement>(":scope > .nt-comp-slot");
    if (!comp) {
      comp = el("div", { class: "nt-comp-slot" });
      notes.before(comp);
    }
    if (!bulk) bulk = createBulkBar(p);
    // The selection bar lives in the header, in the toolbar's row (2026-11-19): while rows are picked it takes the
    // tools' place (style.css), so nothing below moves when a selection starts or ends.
    const top = p.root.querySelector(":scope > .attn-top");
    for (const mount of [bulk.node, bulk.note, bulk.status]) if (!mount.isConnected) top?.appendChild(mount);
    // The dismissed list's id is this table's own, so two tables drawn at once never share one.
    const hadDismissed = p.root.querySelector<HTMLElement>(`#${dismissedId}`);
    const focusWasInDismissed = !!hadDismissed && !hadDismissed.hidden && hadDismissed.contains(document.activeElement);
    const notesList: Array<Node | null> = [];
    if (error) {
      notesList.push(
        notice(
          data
            ? `Needs you shows the last answer; the refresh failed (${describeError(error)}).`
            : `Needs you could not be read (${describeError(error)}).`,
          "error",
        ),
      );
    }
    if (!data) {
      counts.textContent = "";
      replaceChildren(tools, []);
      replaceChildren(comp, []);
      replaceChildren(notes, notesList);
      replaceChildren(body, [error ? null : el("p", { class: "meta" }, "Reading...")]);
      drawing = false;
      removeSheet();
      return;
    }
    // An unreadable source keeps today's behavior (its rows keep their last answer and say so);
    // here it is one banner at the top of the table.
    // An area tab shows only the lines about sources in that area; a line about no known source
    // (Assignments could not be read, Dismissals could not be read) shows on every tab, so a tab never hides it.
    for (const line of unknownLines(data)) {
      if (scope && line.source && AREA_OF[line.source] !== scope) continue;
      notesList.push(
        line.words.startsWith("Assignments")
          ? el("p", { class: "nt-banner attn-unknown" }, `${line.words}, so every item counts for you until it can. Reload the page to try again.`)
          : line.source
          ? el("p", { class: "nt-banner attn-unknown" }, `${line.words} on the last refresh, so its rows may be out of date. It tries again on the next refresh.`)
          : el("p", { class: "nt-banner attn-unknown" }, line.words, ". Its rows keep their last answer."),
      );
    }
    if (filter) {
      const listed = attentionView(data).items;
      const shownCount = listed.filter((item) => item.source === filter).length;
      // Two filters at once (a source and Assigned to me): one line says what is shown, one Show all clears both.
      const both = !!data.can_chat && assignedOnly(data.me);
      const mineHere = both ? listed.filter((item) => item.source === filter && assignedToMe(item, data.me)).length : 0;
      const words = both ? `Assigned to you in ${SOURCE_NAMES[filter]}: ${mineHere || "nothing"}` : `Showing ${shownCount} of ${listed.length}`;
      const clear = el(
        "button",
        {
          type: "button",
          class: "attn-chip",
          "data-focus": "filter:clear",
          title: both ? "Show every source and every assignee again" : "Show every source again",
          "aria-label": both ? `${words}. Show all` : `${words}, only ${SOURCE_NAMES[filter]}. Show all`,
        },
        both ? `${words} · Show all` : `${words} · ${SOURCE_NAMES[filter]} · Show all`,
      );
      clear.addEventListener("click", () => {
        if (both) saveAssignedOnly(data.me, false);
        setFilter(null);
      });
      notesList.push(el("div", { class: "attn-filter" }, clear));
    }
    replaceChildren(notes, notesList);

    // Rows keep their place while the page is open: number the whole answer, drop what left.
    trackRowOrder(attentionView(data).items);
    // "Assigned to me" narrows the table to what is the viewer's; the counts stay the server's.
    const mineOnly = data.can_chat && assignedOnly(data.me);
    const mineOf = <T extends AttentionItem>(rows: T[]): T[] => (mineOnly ? rows.filter((item) => assignedToMe(item, data.me)) : rows);
    const items = mineOf(attentionView(data).items.filter((item) => !scope || AREA_OF[item.source] === scope));
    const everyArea = buildAreas(items, {});
    lastAreas = everyArea;
    lastMine = !!mineOnly;
    if (paneOpen && !everyArea.some((area) => area.items.some((item) => item.id === paneOpen))) paneOpen = null;
    const areas = filter ? buildAreas(items, { only: filter }) : everyArea;
    // The selection follows what is listed here: an item that left, or that this tab or filter does
    // not show, drops out of it, so a bulk action never reaches what the person cannot see.
    // The selection is reconciled against the whole answer, before the tab and filter: switching
    // tab or filtering never unselects an item that still exists, only leaving the list does.
    keepOnly(attentionView(data).items.map((item) => item.id));
    const visible = areas.flatMap((area) => area.items);
    bulk.update(attentionView(data).items, data.can_chat, new Set(visible.map((item) => item.id)));
    // A message about a row that is no longer listed goes; it never moves to the page line.
    const heldMsg = messages.current();
    if (heldMsg && !visibleIds(areas).has(heldMsg.id)) messages.drop();
    // An opened row whose item left the list (or the tab's filter) closes.
    if (p.openRow && !areas.some((area) => area.items.some((item) => item.id === p.openRow))) p.openRow = null;
    // Open: every area and source, in the fixed order; the last ones fold only when the list would run
    // past the screen. A person's own open or closed choice always wins.
    // The Overview of a wide page splits its areas into columns; an area tab holds one area, so it never does.
    const wanted = scope ? 1 : columnCount(measure(body), drawnCols);
    drawnCols = wanted;
    if (!widthWatch && typeof ResizeObserver !== "undefined") {
      widthWatch = new ResizeObserver(() => {
        if (current && !scope && columnCount(measure(body), drawnCols) !== drawnCols) redrawPanel(current, true);
      });
      widthWatch.observe(body);
    }
    const budget = Math.max(320, (typeof window === "undefined" ? 900 : window.innerHeight) - 300);
    const scopeKey = scope ?? "overview";
    const flat = scope !== null;
    // Areas are placed across every column the width allows (so a filter that hides areas never changes the
    // placement); the table draws as many columns as the placement uses.
    const placeSplit = flat ? 1 : wanted;
    const areaKey = areas.map((area) => area.id).join(",");
    let placed = placements.get(scopeKey);
    if (!placed || placed.split !== placeSplit) {
      // The column count changed (or this is the first draw): place everything.
      const cols = assignColumns(
        areas.map((area) => placeLines(area)),
        placeSplit,
      );
      placed = { key: areaKey, split: placeSplit, of: new Map(areas.map((area, i) => [area.id, cols[i] ?? 0])) };
      placements.set(scopeKey, placed);
    } else if (placed.key !== areaKey) {
      // An area appeared or left: every area still present keeps its column, a gap stays where one left,
      // and a new area goes in the column with the fewest lines (the same rule).
      const kept = new Map<DomainId, number>();
      const lines = Array.from({ length: placeSplit }, () => 0);
      for (const area of areas) {
        const col = placed.of.get(area.id);
        if (col !== undefined) {
          kept.set(area.id, col);
          lines[col] = (lines[col] ?? 0) + placeLines(area);
        }
      }
      for (const area of areas) {
        if (kept.has(area.id)) continue;
        const col = lines.indexOf(Math.min(...lines));
        kept.set(area.id, col);
        lines[col] = (lines[col] ?? 0) + placeLines(area);
      }
      placed = { key: areaKey, split: placeSplit, of: kept };
      placements.set(scopeKey, placed);
    }
    const columnOf = (id: DomainId): number => placed.of.get(id) ?? 0;
    const split = areas.length <= 1 ? 1 : Math.max(1, ...areas.map((area) => columnOf(area.id) + 1));
    const key = planKey(areas);
    const count = areas.reduce((sum, area) => sum + area.items.length, 0);
    let plan = plans.get(scopeKey);
    // The Overview opens areas only (collapsed); an area tab keeps the budgeted plan.
    const collapsed = !scope;
    const grew = !collapsed && plan !== undefined && count > plan.count && listCost(areas, wanted, columnOf) > budget;
    if (!plan || plan.key !== key || plan.cols !== wanted || grew) {
      const fresh = planOpen(areas, budget, rememberedFold, wanted, columnOf, collapsed);
      // An automatic re-plan may only open a group, never fold one that is open on screen; a list that
      // outgrew the screen plans it all again. A person's own choice wins over both (openOf).
      if (plan && !collapsed && !grew && plan.cols === wanted) for (const [id, was] of plan.map) if (was && fresh.has(id)) fresh.set(id, true);
      plan = { key, cols: wanted, count, map: fresh };
      plans.set(scopeKey, plan);
    } else {
      plan.count = count;
    }
    const defaults = plan.map;
    const openOf = (id: string) => rememberedFold(id) ?? defaults.get(id) ?? false;
    // At the wide band a source's rows live only in the pane: its line always draws closed (its saved state is kept
    // for the narrower bands), and the line the pane shows counts as seen. Area folds open and close in the list.
    const wide = inWide();
    const sourceIds = new Set(everyArea.flatMap((area) => area.sources.map((entry) => entry.block.id)));
    const paneSource = (() => {
      const t = pane?.current();
      return t?.kind === "source" ? everyArea.flatMap((area) => area.sources).find((entry) => entry.source === t.id)?.block.id : undefined;
    })();
    const drawOpen = (id: string) => (wide && sourceIds.has(id) ? false : openOf(id));
    const seenOpen = (id: string) => (wide && sourceIds.has(id) ? id === paneSource : openOf(id));
    // A closed line with an urgent item not seen open yet gets a "new" marker; nothing reopens.
    newMarks = markNewUrgent(
      [
        ...everyArea.map((area) => ({ id: areaFoldId(area.id), items: area.items })),
        ...everyArea.flatMap((area) => area.sources.map((entry) => ({ id: entry.block.id, items: entry.block.items }))),
      ],
      seenOpen,
    );
    const view = attentionView(data);
    const dismissed = mineOf(view.dismissed).filter(
      (item) => (!filter || item.source === filter) && (!scope || AREA_OF[item.source] === scope),
    );
    const unread = unreadSources(data);
    const unreadHere = (filter ? [filter] : scope ? sourcesOfArea(scope) : [...unread]).filter((source) => unread.has(source));

    // Today's counts: by the server's rule (by_source) for the whole list and for an area tab, never
    // from the rows, which the server may have cut to fit the page.
    if (scope) {
      const area = areaCounts(scope);
      const working = everyArea.reduce((sum, a) => sum + a.working, 0);
      const urgent = area?.urgent ?? everyArea.reduce((sum, a) => sum + a.urgent, 0);
      const review = area?.review ?? everyArea.reduce((sum, a) => sum + a.review, 0);
      counts.textContent = urgent + review + working ? countsText(urgent, review, working) : "";
    } else {
      counts.textContent = !view.empty ? countsText(view.urgent, view.review, view.working) : "";
    }
    // "N dismissed" after the counts opens the dismissed list at the end of the card (2026-11-19): no box, closed on
    // every page load, absent when nothing is dismissed.
    top?.querySelector(":scope > .nt-dlink")?.remove();
    if (dismissed.length) counts.after(dismissedLink(p, dismissed.length));
    else if (p.dismissedOpen) {
      // The last dismissed item was restored with the list open: the list is gone, so the open state resets and
      // focus (which sat on the Restore button) goes to the card heading.
      p.dismissedOpen = false;
      const heading = part(p, ".attn-h");
      heading.tabIndex = -1;
      if (focusWasInDismissed || !document.activeElement || document.activeElement === document.body) heading.focus();
    }
    const cut = filter ? sourceCut(filter) : scope ? areaCut(scope) : (data.more ?? 0);
    // Expand urgent and Collapse all act on what is listed (the filtered areas), never on a hidden area or source.
    const allIds = (): void => {
      for (const area of areas) {
        saveFold(areaFoldId(area.id), false);
        if (!inWide()) for (const entry of area.sources) saveFold(entry.block.id, false);
      }
    };
    replaceChildren(tools, [
      assignedSegment(attentionView(data).items, data.me, data.can_chat, () => redrawPanel(p, true)),
      // The same buttons in both modes (2026-11-18): with nothing listed they stay, disabled, so the toolbar never moves.
      marked(toolButton("Expand urgent", chevronsDownIcon(), {
            title: "Open every area that has an urgent item and leave the others as they are",
            onClick: () => {
              const wideNow = inWide();
              for (const area of areas) {
                // An area with an urgent item opens, and below wide so do its urgent sources; every other area and
                // the sources inside it stay exactly as they were (2026-11-18).
                if (area.urgent <= 0) continue;
                saveFold(areaFoldId(area.id), true);
                if (!wideNow) for (const entry of area.sources) saveFold(entry.block.id, entry.block.urgent > 0);
              }
              // At the wide band the first urgent source, in the list's order, fills the pane.
              const first = wideNow ? areas.flatMap((area) => area.sources).find((entry) => entry.block.urgent > 0) : undefined;
              if (first) {
                paneOpen = null;
                pane!.show({ kind: "source", id: first.source });
              } else redrawPanel(p, true);
            },
          }), "tool:expand"),
      marked(toolButton("Collapse all", chevronsUpIcon(), {
            title: "Close every area and source",
            onClick: () => {
              allIds();
              // At the wide band the pane closes with the areas (its line is folded away).
              if (inWide() && pane?.current()) pane.clear(false);
              else redrawPanel(p, true);
            },
          }), "tool:collapse"),
    ]);
    if (!areas.length) for (const b of tools.querySelectorAll<HTMLButtonElement>(".btn")) b.disabled = true;
    // The strip keeps every source while Assigned to me is on: it counts the server's way, not the narrowed rows.
    const stripAreas = mineOnly ? buildAreas(attentionView(data).items.filter((item) => !scope || AREA_OF[item.source] === scope), {}) : everyArea;
    replaceChildren(comp, [compositionBar(stripAreas, data, filter)]);

    let table: HTMLElement | null = null;
    if (areas.length) {
      const head = el(
        "div",
        { class: "nt-head", "aria-hidden": "true" },
        el("span", {}),
        el("span", {}),
        el("span", {}, "What"),
        el("span", {}, "Age"),
        el("span", {}, "Checked"),
        el("span", {}, "Action"),
      );
      const groups = flat
        ? areas.flatMap((area) =>
            area.sources.map((entry) => sourceNode(p, area, entry, openOf(entry.block.id), data, unread.has(entry.source))),
          )
        : areas.map((area) => areaNode(p, area, openOf(areaFoldId(area.id)), drawOpen, data, unread));
      if (split > 1) {
        // Each area keeps the column it was placed in (the fewest lines so far when it was placed); every
        // column is its own table section with its own header row, in DOM order column by column.
        const columns = Array.from({ length: split }, (_, col) => {
          const mine = areas.flatMap((area) => (columnOf(area.id) === col ? [area] : []));
          // A column whose areas all left stays as a blank spacer of the same width (nothing else moves):
          // no header row, and nothing for assistive technology.
          if (!mine.length) return el("div", { class: "nt-col is-empty", "aria-hidden": "true", "data-col": String(col) });
          return el(
            "div",
            {
              class: "nt-col",
              role: "group",
              "aria-label": `Needs you, column ${col + 1} of ${split}: ${mine.map((area) => area.label).join(", ")}`,
              "data-col": String(col),
            },
            head.cloneNode(true),
            ...groups.filter((_, i) => columnOf(areas[i]!.id) === col),
          );
        });
        table = el(
          "div",
          { class: "nt-table is-cols", role: "group", "aria-label": "Needs you", style: `--nt-n:${split}` },
          ...columns,
        );
      } else {
        table = el("div", { class: `nt-table${flat ? " is-flat" : ""}`, role: "group", "aria-label": "Needs you" }, head, ...groups);
      }
    }
    const empty = (): HTMLElement | null => {
      if (unreadHere.length) {
        // An unread source is not an empty one: say what could not be read, never "Nothing needs you".
        const names = sourceList(unreadHere);
        return el(
          "p",
          { class: "attn-empty attn-empty-unread" },
          filter || scope
            ? `Could not read ${names}.`
            : `Nothing else needs you, but ${names} could not be read, so ${unreadHere.length > 1 ? "they" : "it"} may be missing items.`,
        );
      }
      if (mineOnly && !areas.length) return filter ? null : el("p", { class: "attn-empty" }, "Nothing is assigned to you.");
      if (!(view.empty || filter || scope)) return null;
      return el(
        "p",
        { class: "attn-empty" },
        ...(view.empty && !filter && !scope && data.unknown.length === 0
          ? [checkIcon(), emptyText(data.read_at)]
          : ["Nothing needs you."]),
      );
    };
    replaceChildren(body, [
      el("div", { class: "nt-page-msg", role: "status", "aria-live": "polite" }),
      table ?? empty(),
      cut ? el("p", { class: "attn-more meta" }, `${cut} more not shown`) : null,
      snoozedNode(
        p,
        mineOf(data.snoozed ?? []).filter((item) => (!filter || item.source === filter) && (!scope || AREA_OF[item.source] === scope)),
      ),
      dismissedSection(p, dismissed),
    ]);
    syncPicks(p.root);
    // The pane draws from this answer too (never while the table is limited to an area tab).
    if (pane?.current()) {
      pane.refresh();
      syncPicks(pane.root);
    }
    bulk.settle();
    drawing = false;
    syncSheet(p);
    // A closed row's message shows in the page's line (an opened row's was drawn with its panel).
    const held = messages.current();
    if (held && !p.root.querySelector(".nt-panel-msg:not(:empty)") && !sheet) paintMessage(p, held, null);
    else if (held) paintPane(held);
    void phone;
  };

  return {
    hooks: { draw: drawTable, shell, finish, message: (p, id, line) => messages.set(p, id, line) },
    redraw: () => {
      if (current) redrawPanel(current, true);
    },
    dispose: () => {
      messages.stop();
      widthWatch?.disconnect();
      widthWatch = null;
      bulk?.stop();
      bulk?.node.remove();
      bulk = null;
      stopPicks?.();
      stopPicks = null;
      removeSheet();
    },
    attachPane: (next) => {
      stopPicks?.();
      pane = next;
      stopPicks = next ? watchPicks(next.root) : null;
    },
    paneDeps,
    openInPane: (id) => {
      const entry = lastAreas.flatMap((area) => area.sources).find((e) => e.block.items.some((item) => item.id === id));
      if (!entry || !inWide()) return false;
      paneOpen = id;
      pane!.show({ kind: "source", id: entry.source });
      return true;
    },
    setArea: (area) => {
      if (scope === area) return;
      scope = area;
      if (current) redrawPanel(current, true);
    },
  };
}
