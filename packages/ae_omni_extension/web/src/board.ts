// The Admin Center's cards and their columns (2026-10-20, the dashboard
// redesign the owner approved as concept C): four cards under the tile strip
// (tiles.ts), Deploys, Compute (Fabric capacities and Databricks warehouses),
// Databricks credit and Box health (the host and the server's secrets).
//
// Why fixed column stacks and not one shared grid: in a grid a short card
// leaves a hole under it until the tallest card in its row ends, and the owner
// rejected the mockup that did that. Each column here is a flex stack whose
// last card stretches, so every column ends on the same line. Which card goes
// in which column depends on the page's width, so the cards move between
// column elements when it changes:
//   three columns (1040 px of page and up): Deploys over Box health, then
//     Accounts over Compute, then Azure spend over Databricks credit;
//   two columns (520 px and up): Deploys over Box health over Compute,
//     Accounts over Azure spend over Databricks credit;
//   one column (a phone): Deploys, Accounts, Compute, Databricks credit,
//     Azure spend, Box health.
// 2026-10-22: the Azure spend card (azure.ts) joined. The same day the owner chose
// the columns above (Box health under Deploys, Databricks credit under Azure
// spend, which closes the holes the old defaults left) and asked to drag a
// card elsewhere: on two and three columns each header has a grip
// (boardDrag.ts), the arrangement is saved per column count (boardLayout.ts)
// and "Reset layout" brings the defaults back. A phone is not arranged.
// 2026-10-23: the Key Vault card (vault.ts) joined, under Box health on two
// and three columns and after it on a phone; a saved arrangement without it
// gets it in that default column (boardLayout.ts).
// 2026-10-24: the Git repos card (git.ts) joined, last in the right column on
// three columns, last in the left on two, and last on a phone.
// 2026-10-24: the Presets card (presets.ts) joined, right after Deploys in the
// first column on three and two columns, and after Deploys on a phone.
// 2026-10-29: the Family cost card (familyCost.ts) joined, right after Presets in the first column
// on three and two columns and after Presets on a phone.
// 2026-10-30: the Family cost card folded into the Presets card, now "Spend", as its Families tab;
// a saved arrangement that still names "family" loads without it (boardLayout.ts drops unknown ids).
// 2026-10-21: the Accounts card (accounts.ts) joined, at the head of the
// middle column, where the account nearest its limit reads first.
// 2026-10-28: the Box health card opens with the box monitor (boxHealth.ts), and its
// tile heads the card's folded line, so a hot or full box unfolds the card on a phone.
// A box admin who is not an OmniGent admin gets a board of that one card (fabric.ts),
// and its grip and the Reset layout button have nothing to move.
// 2026-11-06 (phase 4 of the redesign): the cards sit under four group headings, Ship, Box,
// Cloud and Accounts, the same four as the status pills (tiles.ts DOMAINS, whose `cards` list is the
// one source of which group a card is in). Why: the pills said "Cloud" but the cards under it were
// scattered, so a click on a pill had no place to land. Each group is a heading over the same n
// column stacks, so card widths line up from group to group; a card moves only inside its group
// (boardDrag.ts), and the saved arrangement is per group (boardLayout.ts). An integration the
// server says is not configured (Tile.notSetUp) leaves the board and is named in one line at the
// end; a group left with no card draws no heading. A box admin's one-card board has no groups.
// 2026-11-07: the Decision model card (decision.ts) joined the Cloud group, after Key Vault.
// 2026-11-07 (packed board): from two columns up the group sections are gone. Needs you is the first
// tile of column 1 and every card goes into the shortest column by its guessed height, each with a
// small group tag; a fourth column starts at 1,720 px. Why: the owner said "UI still seems to waste
// a ton here" at 16:9 and 4:3; the sections left holes and an empty half beside Needs you. The
// arrangement is saved per column count for the whole board (boardLayout.ts); a phone is unchanged.
// The width comes from a ResizeObserver on the page (the window's resize where
// there is none); moving a card keeps the focus on the control that had it.
//
// 2026-11-14 (concept D2, Reid's pick): the packed board and the drag are gone. adminShell.ts puts each
// area's cards in a plain grid on that area's tab (or on the Overview when the server has four cards or
// fewer), the cards no longer move, and the width only tells a phone (one column) from the rest. The
// arrangement code (boardLayout.ts, boardDrag.ts) and the status pills left with it; ring, adminCard,
// mountBoard and the helpers other files import stay. A card still folds to its header line on a phone.
//
// On a phone each card folds to its header line (tap to open), with the
// card's tile values on that line so a folded card still says how it is.
// Open or folded is remembered per card through sections.ts (ids "admin-*").
// Until the owner picks, Deploys starts open and the others start folded unless
// they need attention (a tile amber or red: the host not connected, a secret
// missing). On a wider page every card is open and the header is not a toggle.

import { type Child, el } from "./dom";
import { cardGone, cardHidden, CARD_TILES, getCards, onCardState } from "./cardState";
import { foldIcon, settingsIcon } from "./icons";
import { isOpen, setOpen } from "./sections";
import { DOMAINS, type DomainId, getTile, type Health, onTiles, worst } from "./tiles";

export type CardId = "deploys" | "accounts" | "compute" | "credit" | "azure" | "box" | "vault" | "git" | "presets" | "social" | "decision" | "home-security";

/** The cards a board holds; a box admin's page holds only the Box health card. */
export type CardSet = Partial<Record<CardId, HTMLElement>>;

export const FOUR_COLUMNS_PX = 1720;
export const THREE_COLUMNS_PX = 1040;
export const TWO_COLUMNS_PX = 520;

/** The group a card is in (tiles.ts DOMAINS.cards). */
export function groupOf(id: CardId): DomainId {
  return DOMAINS.find((domain) => domain.cards.includes(id))?.id ?? "ship";
}

const domainLabel = (group: DomainId): string => DOMAINS.find((d) => d.id === group)?.label ?? group;

/** True when the server said every integration behind a card is not configured (Tile.notSetUp). */
export function cardNotSetUp(id: CardId): boolean {
  return CARD_TILES[id].every((tile) => getTile(tile)?.notSetUp === true);
}

export { CARD_TILES };

/**
 * The event a card's title raises (bubbles) while the card sits on the Overview wall (2026-11-17):
 * the shell shows the card's own tab and goes to the card. detail is the card id.
 */
export const WALL_OPEN_EVENT = "admin-wall-open";

/** The event a card's gear raises (bubbles); fabric.ts opens the Cards panel at that card. */
export const CARD_SETTINGS_EVENT = "admin-card-settings";

export function columnsFor(width: number): 1 | 2 | 3 | 4 {
  if (width >= FOUR_COLUMNS_PX) return 4;
  if (width >= THREE_COLUMNS_PX) return 3;
  return width >= TWO_COLUMNS_PX ? 2 : 1;
}

/** The worst health among a card's tiles; idle when none has reported. */
export function cardHealth(id: CardId): Health {
  return worst(CARD_TILES[id].flatMap((tile) => getTile(tile)?.health ?? []));
}

/** Where a card starts on a phone until the owner opens or folds it once. */
export function openByDefault(id: CardId): boolean {
  if (id === "deploys") return true;
  const health = cardHealth(id);
  return health === "attention" || health === "problem";
}

/** "Host connected · 8 of 8 secrets": a folded card's tiles, each naming what it is. */
export function summaryText(id: CardId): string {
  return CARD_TILES[id]
    .flatMap((tile) => {
      const found = getTile(tile);
      return found ? [found.summary ?? found.value] : [];
    })
    .join(" · ");
}

const storageId = (id: CardId) => `admin-${id}`;

const SVG_NS = "http://www.w3.org/2000/svg";

/**
 * A ring (the Deploys card's hold, the credit card's credit left): a track
 * and an arc filled to *fraction* (0 to 1) from twelve o'clock, *big* and
 * *small* in the middle. *tone* is a class (ring-warn, ring-ok, ring-err)
 * that colours the arc; *label* names it for a screen reader.
 */
export function ring(
  fraction: number,
  big: string,
  small: string,
  tone: string,
  label: string,
): HTMLElement {
  const radius = 40;
  const length = 2 * Math.PI * radius;
  const part = Math.min(1, Math.max(0, fraction));
  const svg = document.createElementNS(SVG_NS, "svg");
  svg.setAttribute("viewBox", "0 0 92 92");
  svg.setAttribute("aria-hidden", "true");
  for (const [cls, dash] of [
    ["ring-track", ""],
    ["ring-arc", `${(part * length).toFixed(2)} ${length.toFixed(2)}`],
  ]) {
    const circle = document.createElementNS(SVG_NS, "circle");
    circle.setAttribute("class", cls);
    circle.setAttribute("cx", "46");
    circle.setAttribute("cy", "46");
    circle.setAttribute("r", String(radius));
    if (dash) circle.setAttribute("stroke-dasharray", dash);
    svg.appendChild(circle);
  }
  return el(
    "div",
    { class: `ring ${tone}`, role: "img", "aria-label": label, title: label },
    svg,
    el(
      "span",
      { class: "ring-mid" },
      el("b", { class: "ring-big" }, big),
      el("span", { class: "ring-small" }, small),
    ),
  );
}

/**
 * A card: a header with an icon badge, the title and *extra* (a pill, an
 * "Updated" time, a button) at the right, then *body*. The heading's button
 * folds the card on a phone.
 */
export function adminCard(
  id: CardId,
  title: string,
  icon: SVGElement,
  extra: Child[],
  ...body: Child[]
): HTMLElement {
  const toggle = el(
    "button",
    { type: "button", class: "admin-card-toggle", "aria-controls": `admin-${id}-body` },
    el("span", { class: `admin-badge admin-badge-${id}`, "aria-hidden": "true" }, icon),
    el("span", { class: "admin-card-title" }, title),
    el("span", { class: "admin-card-sum" }),
    foldIcon(),
  );
  // The gear opens the Cards panel at this card (2026-11-07); the board needs no context, so it
  // only raises an event that fabric.ts listens for.
  const gear = el("button", {
    type: "button",
    class: "admin-gear",
    title: "Card settings",
    "aria-label": `Settings for ${title}`,
  });
  gear.appendChild(settingsIcon(16));
  gear.addEventListener("click", () => {
    gear.dispatchEvent(new CustomEvent(CARD_SETTINGS_EVENT, { bubbles: true, detail: id }));
  });
  const node = el(
    "section",
    {
      class: `admin-card admin-card-${id}`,
      "data-card": id,
      "data-group": groupOf(id),
      "aria-labelledby": `admin-${id}-h` },
    el("span", { class: "admin-card-tag" }, domainLabel(groupOf(id))),
    el(
      "header",
      { class: "admin-card-top" },
      el("h3", { class: "admin-card-h", id: `admin-${id}-h` }, toggle),
      ...extra,
      gear,
    ),
    el("div", { class: "admin-card-body", id: `admin-${id}-body` }, ...body),
  );
  toggle.addEventListener("click", () => {
    if (node.dataset.wall === "1") {
      toggle.dispatchEvent(new CustomEvent(WALL_OPEN_EVENT, { bubbles: true, detail: id }));
      return;
    }
    const board = node.closest<HTMLElement>(".admin-board");
    if (board?.dataset.columns !== "1") return;
    setOpen(storageId(id), node.classList.contains("is-folded"));
    applyFold(node, true);
    pairShelf(board);
  });
  return node;
}

/**
 * The two-up shelf (phone option 2, 2026-11-07): in a one-column stack, runs of two or more folded
 * cards in a row are marked `is-half` in pairs, so each pair sits side by side. A run of three
 * pairs its first two; an odd last card stays full width. Wider layouts have no halves.
 */
export function pairShelf(board: HTMLElement): void {
  const one = board.dataset.columns === "1";
  const columns = board.querySelectorAll<HTMLElement>(".admin-col");
  for (const col of columns.length ? [...columns] : [board]) {
    const run: HTMLElement[] = [];
    const flush = () => {
      for (let i = 0; i + 1 < run.length; i += 2) {
        run[i].classList.add("is-half");
        run[i + 1].classList.add("is-half");
      }
      run.length = 0;
    };
    for (const card of col.querySelectorAll<HTMLElement>(":scope > .admin-card")) {
      card.classList.remove("is-half");
      if (one && card.classList.contains("is-folded")) run.push(card);
      else flush();
    }
    flush();
  }
}

/**
 * Put a card on, or take it off, the Overview wall (wall.ts): its title becomes the control that opens
 * the card on its tab. Decided here because applyFold runs on every tile change and must not undo it.
 */
export function setCardWall(card: HTMLElement, on: boolean): void {
  if (on) card.dataset.wall = "1";
  else delete card.dataset.wall;
  applyFold(card, false);
}

/** Fold or open one card for the current layout. */
function applyFold(card: HTMLElement, phone: boolean): void {
  const id = card.dataset.card as CardId;
  // On the Overview wall (wall.ts) the card is always open and its title opens the card on its own tab.
  const wall = card.dataset.wall === "1";
  const open = wall || !phone || isOpen(storageId(id), openByDefault(id));
  card.classList.toggle("is-folded", !open);
  const toggle = card.querySelector<HTMLButtonElement>(".admin-card-toggle");
  const content = card.querySelector<HTMLElement>(".admin-card-body");
  if (content) content.hidden = !open;
  if (toggle && wall) {
    const words = `Open the ${titleText(card)} card on the ${domainLabel(groupOf(id))} tab`;
    toggle.removeAttribute("aria-expanded");
    toggle.removeAttribute("aria-controls");
    toggle.removeAttribute("tabindex");
    toggle.title = words;
    toggle.setAttribute("aria-label", words);
  } else if (toggle) {
    toggle.removeAttribute("title");
    toggle.removeAttribute("aria-label");
    toggle.setAttribute("aria-controls", `admin-${id}-body`);
    if (phone) {
      toggle.setAttribute("aria-expanded", String(open));
      toggle.removeAttribute("tabindex");
    } else {
      // Not a control on a wide page: every card is open there.
      toggle.removeAttribute("aria-expanded");
      toggle.tabIndex = -1;
    }
  }
  const sum = card.querySelector<HTMLElement>(".admin-card-sum");
  if (sum) sum.textContent = phone && !open ? summaryText(id) : "";
  card.dataset.health = cardHealth(id);
}

/**
 * Put the cards in *n* columns of *board*, keeping the focus where it was.
 * *force* lays them out again even when the column count is unchanged (a
 * saved arrangement changed).
 *
 * The plain "admin-arranged" event fires only when the columns were replaced
 * (2026-10-22): the column count, a saved arrangement and the phone switch all
 * change only then. A scrollbar that appears mid-drag narrows the page and
 * calls this again at the same column count, and a drag must survive that;
 * only a rebuild takes its column away. A tile changing health does not come
 * through here (mountBoard refolds), only a width change does.
 */
/** The title a card shows, for the footer line. */
const titleText = (card: HTMLElement | undefined): string => card?.querySelector(".admin-card-title")?.textContent ?? "";
const titleOf = (cards: CardSet, id: CardId): string => titleText(cards[id]) || id;

function shortDate(iso: string | null): string {
  const when = iso ? new Date(iso) : null;
  return when && !Number.isNaN(when.getTime())
    ? when.toLocaleDateString(undefined, { month: "short", day: "numeric" })
    : "";
}

/**
 * The line under the board (2026-11-07): "Turned off: Social (by <login>, Nov 7). Hidden for you:
 * Spend. Not set up on this server: Key Vault." Each part shows only when it has cards, and a
 * "Cards settings" button opens the panel. Hidden when no part has cards.
 */
export function footerNote(cards: CardSet): HTMLElement {
  const ids = Object.keys(cards) as CardId[];
  const rows = getCards()?.cards ?? [];
  // Only a card with a row in the answer is "turned off": one the answer does not list (a private
  // card this server never named) leaves the board without being mentioned.
  const off = ids.filter((id) => rows.some((r) => r.id === id && !r.on));
  const hiddenIds = ids.filter((id) => cardHidden(id) && !off.includes(id));
  const notSet = ids.filter((id) => !cardGone(id) && cardNotSetUp(id));
  const offText = off.map((id) => {
    const row = rows.find((r) => r.id === id);
    const day = shortDate(row?.changed_at ?? null);
    const by = row?.changed_by ? ` (by ${row.changed_by}${day ? `, ${day}` : ""})` : "";
    return `${titleOf(cards, id)}${by}`;
  });
  const parts = [
    offText.length ? `Turned off: ${offText.join(", ")}.` : "",
    hiddenIds.length ? `Hidden for you: ${hiddenIds.map((id) => titleOf(cards, id)).join(", ")}.` : "",
    notSet.length ? `Not set up on this server: ${notSet.map((id) => titleOf(cards, id)).join(", ")}.` : "",
  ].filter(Boolean);
  const settings = el(
    "button",
    {
      type: "button",
      class: "btn btn-quiet admin-notset-open",
      title: "Open the Cards panel to turn cards on or off, hide one for yourself, or see how to set one up",
    },
    "Cards settings",
  );
  settings.addEventListener("click", () => {
    settings.dispatchEvent(new CustomEvent(CARD_SETTINGS_EVENT, { bubbles: true, detail: null }));
  });
  const note = el("p", { class: "admin-notset" }, `${parts.join(" ")} `, settings);
  note.hidden = parts.length === 0;
  return note;
}

/** An area tab's lane: at least 620 px wide, 14 px apart (style.css, .as-board). */
const LANE_PX = 620;
const LANE_GAP_PX = 14;

/**
 * How many lanes an area tab's board (.as-board) gets: as many 620 px lanes as its width holds, never more
 * than it has cards. Any other board, and a phone, keeps one column. A hidden board reads its page's width.
 */
function laneCount(board: HTMLElement, n: number, pageWidth: number, cardCount: number): number {
  if (n === 1 || !board.classList.contains("as-board")) return 1;
  const width = board.clientWidth || pageWidth;
  return Math.max(1, Math.min(cardCount, Math.floor((width + LANE_GAP_PX) / (LANE_PX + LANE_GAP_PX))));
}

/**
 * 2026-11-22: the cards take turns across the lanes in page order (first card left, second right, third
 * left again), and each lane stacks its cards with no space between them. So a short card leaves no hole
 * beside a tall one, the order reads left to right, and a card that grows moves only the cards under it
 * in its own lane. Why not the shortest lane by height: the cards are still loading when they are placed,
 * so their heights say nothing yet, and choosing again later would move cards while they are being read.
 *
 * The cards stay in one column element in page order, so Tab and a screen reader go left to right too
 * (Deploys, Git, Social), not down one lane and then the next. style.css sets each card's lane from
 * --lane and --lanes; placeLanes sets its top from the heights of the cards above it in that lane.
 */
const placers = new WeakMap<HTMLElement, ResizeObserver>();

function placeLanes(col: HTMLElement, lanes: number): void {
  const bottoms = Array.from({ length: lanes }, () => 0);
  let i = 0;
  for (const card of col.querySelectorAll<HTMLElement>(":scope > .admin-card")) {
    const lane = i++ % lanes;
    card.style.setProperty("--lane", String(lane));
    card.style.top = `${bottoms[lane]}px`;
    // The exact height, not offsetHeight: whole pixels would add up to a 1 px overlap down a long lane.
    bottoms[lane]! += card.getBoundingClientRect().height + LANE_GAP_PX;
  }
  col.style.height = `${Math.max(0, Math.max(...bottoms) - LANE_GAP_PX)}px`;
}

function intoLanes(board: HTMLElement, cards: HTMLElement[], lanes: number): HTMLElement {
  placers.get(board)?.disconnect();
  placers.delete(board);
  const col = el("div", { class: "admin-col" }, ...cards);
  for (const card of cards) {
    card.style.removeProperty("--lane");
    card.style.removeProperty("top");
  }
  if (lanes < 2) return col;
  col.style.setProperty("--lanes", String(lanes));
  placeLanes(col, lanes);
  // A card that loads, grows, folds or is first shown (a hidden tab measures 0 px) places the lanes again.
  // It runs after layout and before paint, so a card is never drawn over another; moving a card's top
  // changes no card's size, so it does not loop.
  if (typeof ResizeObserver !== "undefined") {
    const observer = new ResizeObserver(() => {
      if (col.isConnected) placeLanes(col, lanes);
    });
    for (const card of cards) observer.observe(card);
    placers.set(board, observer);
  }
  return col;
}

/** Stop placing *board*'s lanes (its mount stopped). */
function stopLanes(board: HTMLElement): void {
  placers.get(board)?.disconnect();
  placers.delete(board);
}

export function arrange(
  board: HTMLElement,
  cards: CardSet,
  n: 1 | 2 | 3 | 4,
  force = false,
  attention?: HTMLElement,
  width = 0,
): void {
  const here = (Object.keys(cards) as CardId[]).filter((id) => !cardNotSetUp(id) && !cardGone(id));
  // The page's group order (tiles.ts DOMAINS), then any card a group does not name.
  const order = DOMAINS.flatMap((domain) => domain.cards.filter((id): id is CardId => here.includes(id as CardId)));
  for (const id of here) if (!order.includes(id)) order.push(id);
  const placed = order.filter((id) => cards[id]!.dataset.wall !== "1");
  const lanes = laneCount(board, n, width, placed.length);
  const rebuilt = force || board.dataset.columns !== String(n) || board.dataset.lanes !== String(lanes);
  if (rebuilt) {
    const focused = document.activeElement as HTMLElement | null;
    const inAttention = !!focused && !!attention?.contains(focused);
    const wasInside = !!focused && board.contains(focused) && !inAttention;
    const note = footerNote(cards);
    // A box admin's one-card board (data-single) has no footer: nothing there can be turned off.
    const single = board.dataset.single !== undefined;
    // A card the Overview wall holds stays where it is (2026-11-17): moving it would drop the focus a
    // control in it holds. The shell's give-back lays this board out again when the cards return.
    board.replaceChildren(intoLanes(board, placed.map((id) => cards[id]!), lanes), ...(single ? [] : [note]));
    board.dataset.columns = String(n);
    board.dataset.lanes = String(lanes);
    if (wasInside && focused && !board.contains(focused) && !single) {
      // The old footer or card is gone: the new footer takes the focus, or the board when it
      // is hidden (nothing is left unset), so it never falls back to the page body.
      note.tabIndex = -1;
      if (!note.hidden) note.focus({ preventScroll: true });
      else {
        board.tabIndex = -1;
        board.focus({ preventScroll: true });
      }
    }
    if (
      focused &&
      focused !== document.activeElement &&
      (board.contains(focused) || !!attention?.contains(focused))
    ) {
      focused.focus({ preventScroll: true });
    }
  }
  for (const card of Object.values(cards)) applyFold(card, n === 1);
  pairShelf(board);
  if (rebuilt) board.dispatchEvent(new Event("admin-arranged"));
}

/**
 * Lay the cards out in *board* by the width of *page* (a phone folds them), again whenever it
 * changes and whenever a tile changes (a card that turns red unfolds unless
 * the owner folded it themselves). `refold` applies the remembered folds once they are
 * read; `stop` stops listening.
 */
export function mountBoard(
  page: HTMLElement,
  board: HTMLElement,
  cards: CardSet,
  attention?: HTMLElement,
): { stop: () => void; refold: () => void; layout: (force?: boolean) => void } {
  let width = page.clientWidth || window.innerWidth;
  const notSet = () =>
    (Object.keys(cards) as CardId[]).filter((id) => cardNotSetUp(id) || cardGone(id)).sort().join(",");
  let lastNotSet = notSet();
  // A listener already queued when stop() ran must not move cards back into a board that is gone.
  let stopped = false;
  const layout = (force = false) => {
    if (stopped) return;
    lastNotSet = notSet();
    arrange(board, cards, columnsFor(width), force, attention, width);
  };
  const refold = () => {
    if (stopped) return;
    for (const card of Object.values(cards)) applyFold(card, board.dataset.columns === "1");
    pairShelf(board);
  };
  layout();
  // A card that becomes (or stops being) not set up joins or leaves the board; anything else refolds.
  const stopTiles = onTiles(() => {
    if (notSet() !== lastNotSet) layout(true);
    else refold();
  });
  // A card turned off, switched back on, hidden or shown joins or leaves the board (2026-11-07).
  const stopCards = onCardState(() => layout(true));
  const stopAll = () => {
    stopped = true;
    stopLanes(board);
    stopTiles();
    stopCards();
  };
  if (typeof ResizeObserver !== "undefined") {
    const observer = new ResizeObserver((entries) => {
      const next = entries[entries.length - 1]?.contentRect.width;
      if (!next || next === width) return;
      width = next;
      layout();
    });
    observer.observe(page);
    return {
      stop: () => {
        observer.disconnect();
        stopAll();
      },
      refold,
      layout,
    };
  }
  const onResize = () => {
    width = page.clientWidth || window.innerWidth;
    layout();
  };
  window.addEventListener("resize", onResize);
  return {
    stop: () => {
      window.removeEventListener("resize", onResize);
      stopAll();
    },
    refold,
    layout,
  };
}
