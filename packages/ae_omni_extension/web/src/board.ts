// The Admin Center's cards and their columns (2026-10-20, the dashboard
// redesign Reid approved as concept C): four cards under the tile strip
// (tiles.ts), Deploys, Compute (Fabric capacities and Databricks warehouses),
// Databricks credit and Box health (the host and the server's secrets).
//
// Why fixed column stacks and not one shared grid: in a grid a short card
// leaves a hole under it until the tallest card in its row ends, and Reid
// rejected the mockup that did that. Each column here is a flex stack whose
// last card stretches, so every column ends on the same line. Which card goes
// in which column depends on the page's width, so the cards move between
// column elements when it changes:
//   three columns (1040 px of page and up): Deploys over Databricks credit,
//     then Accounts over Compute, then Box health;
//   two columns (520 px and up): Deploys over Compute, Accounts over
//     Databricks credit over Box health;
//   one column (a phone): Deploys, Accounts, Compute, Databricks credit, Box
//     health.
// 2026-10-21: the Accounts card (accounts.ts) joined, at the head of the
// middle column, where the account nearest its limit reads first.
// The width comes from a ResizeObserver on the page (the window's resize where
// there is none); moving a card keeps the focus on the control that had it.
//
// On a phone each card folds to its header line (tap to open), with the
// card's tile values on that line so a folded card still says how it is.
// Open or folded is remembered per card through sections.ts (ids "admin-*").
// Until Reid picks, Deploys starts open and the others start folded unless
// they need attention (a tile amber or red: the host not connected, a secret
// missing). On a wider page every card is open and the header is not a toggle.

import { type Child, el } from "./dom";
import { foldIcon } from "./icons";
import { isOpen, setOpen } from "./sections";
import { getTile, type Health, onTiles, type TileId, worst } from "./tiles";

export type CardId = "deploys" | "accounts" | "compute" | "credit" | "box";

export const THREE_COLUMNS_PX = 1040;
export const TWO_COLUMNS_PX = 520;

/** The cards in each column, by column count. */
export const LAYOUTS: Record<1 | 2 | 3, CardId[][]> = {
  3: [
    ["deploys", "credit"],
    ["accounts", "compute"],
    ["box"],
  ],
  2: [
    ["deploys", "compute"],
    ["accounts", "credit", "box"],
  ],
  1: [["deploys", "accounts", "compute", "credit", "box"]],
};

/** The tiles whose health and values a card's folded header line carries. */
export const CARD_TILES: Record<CardId, TileId[]> = {
  deploys: ["hold", "release"],
  accounts: ["accounts"],
  compute: ["fabric", "databricks"],
  credit: ["credit"],
  box: ["host", "secrets"],
};

export function columnsFor(width: number): 1 | 2 | 3 {
  if (width >= THREE_COLUMNS_PX) return 3;
  return width >= TWO_COLUMNS_PX ? 2 : 1;
}

/** The worst health among a card's tiles; idle when none has reported. */
export function cardHealth(id: CardId): Health {
  return worst(CARD_TILES[id].flatMap((tile) => getTile(tile)?.health ?? []));
}

/** Where a card starts on a phone until Reid opens or folds it once. */
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
  const node = el(
    "section",
    { class: `admin-card admin-card-${id}`, "data-card": id, "aria-labelledby": `admin-${id}-h` },
    el(
      "header",
      { class: "admin-card-top" },
      el("h2", { class: "admin-card-h", id: `admin-${id}-h` }, toggle),
      ...extra,
    ),
    el("div", { class: "admin-card-body", id: `admin-${id}-body` }, ...body),
  );
  toggle.addEventListener("click", () => {
    const board = node.closest<HTMLElement>(".admin-board");
    if (board?.dataset.columns !== "1") return;
    setOpen(storageId(id), node.classList.contains("is-folded"));
    applyFold(node, true);
  });
  return node;
}

/** Fold or open one card for the current layout. */
function applyFold(card: HTMLElement, phone: boolean): void {
  const id = card.dataset.card as CardId;
  const open = !phone || isOpen(storageId(id), openByDefault(id));
  card.classList.toggle("is-folded", !open);
  const toggle = card.querySelector<HTMLButtonElement>(".admin-card-toggle");
  const content = card.querySelector<HTMLElement>(".admin-card-body");
  if (content) content.hidden = !open;
  if (toggle) {
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

/** Put the cards in *n* columns of *board*, keeping the focus where it was. */
export function arrange(board: HTMLElement, cards: Record<CardId, HTMLElement>, n: 1 | 2 | 3): void {
  if (board.dataset.columns !== String(n)) {
    const focused = document.activeElement as HTMLElement | null;
    const columns = LAYOUTS[n].map((ids) =>
      el("div", { class: "admin-col" }, ...ids.map((id) => cards[id])),
    );
    board.replaceChildren(...columns);
    board.dataset.columns = String(n);
    if (focused && focused !== document.activeElement && board.contains(focused)) {
      focused.focus({ preventScroll: true });
    }
  }
  for (const card of Object.values(cards)) applyFold(card, n === 1);
}

/**
 * Lay the cards out in *board* by the width of *page*, again whenever it
 * changes and whenever a tile changes (a card that turns red unfolds unless
 * Reid folded it himself). `refold` applies the remembered folds once they are
 * read; `stop` stops listening.
 */
export function mountBoard(
  page: HTMLElement,
  board: HTMLElement,
  cards: Record<CardId, HTMLElement>,
): { stop: () => void; refold: () => void } {
  let width = page.clientWidth || window.innerWidth;
  const layout = () => arrange(board, cards, columnsFor(width));
  const refold = () => {
    for (const card of Object.values(cards)) applyFold(card, board.dataset.columns === "1");
  };
  layout();
  const stopTiles = onTiles(refold);
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
        stopTiles();
      },
      refold,
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
      stopTiles();
    },
    refold,
  };
}
