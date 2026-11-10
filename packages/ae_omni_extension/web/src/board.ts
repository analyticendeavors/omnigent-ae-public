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
// On a phone each card folds to its header line (tap to open), with the
// card's tile values on that line so a folded card still says how it is.
// Open or folded is remembered per card through sections.ts (ids "admin-*").
// Until the owner picks, Deploys starts open and the others start folded unless
// they need attention (a tile amber or red: the host not connected, a secret
// missing). On a wider page every card is open and the header is not a toggle.

import { type Layout, onLayoutChange, packedArrangement } from "./boardLayout";
import { enableDrag } from "./boardDrag";
import { type Child, el } from "./dom";
import { cardGone, cardHidden, CARD_TILES, getCards, onCardState } from "./cardState";
import { foldIcon, gripIcon, settingsIcon } from "./icons";
import { isOpen, setOpen } from "./sections";
import { DOMAINS, type DomainId, getTile, type Health, onTiles, worst } from "./tiles";

export type CardId = "deploys" | "accounts" | "compute" | "credit" | "azure" | "box" | "vault" | "git" | "presets" | "social" | "decision" | "home-security";

/** The cards a board holds; a box admin's page holds only the Box health card. */
export type CardSet = Partial<Record<CardId, HTMLElement>>;

export const FOUR_COLUMNS_PX = 1720;
export const THREE_COLUMNS_PX = 1040;
export const TWO_COLUMNS_PX = 520;

/** The cards in each group on a phone (one column); wider pages pack the board instead. */
export const GROUP_LAYOUTS: Record<1, Record<DomainId, Layout>> = {
  1: {
    ship: [["deploys", "git", "social"]],
    box: [["box", "home-security"]],
    cloud: [["compute", "credit", "azure", "vault", "decision"]],
    money: [["accounts", "presets"]],
  },
};

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
  // Drag handle, shown on two and three columns only (style.css).
  const grip = el("button", {
    type: "button",
    class: "admin-grip",
    title: `Drag to move ${title}`,
    "aria-label": `Move ${title}. Drag, or use the arrow keys.`,
  });
  grip.appendChild(gripIcon());
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
      grip,
    ),
    el("div", { class: "admin-card-body", id: `admin-${id}-body` }, ...body),
  );
  toggle.addEventListener("click", () => {
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
  for (const col of board.querySelectorAll<HTMLElement>(".admin-col")) {
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
const titleOf = (cards: CardSet, id: CardId): string =>
  cards[id]?.querySelector(".admin-card-title")?.textContent ?? id;

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

export function arrange(
  board: HTMLElement,
  cards: CardSet,
  n: 1 | 2 | 3 | 4,
  force = false,
  attention?: HTMLElement,
): void {
  const rebuilt = force || board.dataset.columns !== String(n);
  if (rebuilt) {
    const focused = document.activeElement as HTMLElement | null;
    const inAttention = !!focused && !!attention?.contains(focused);
    const wasInside = !!focused && board.contains(focused) && !inAttention;
    const here = (Object.keys(cards) as CardId[]).filter(
      (id) => !cardNotSetUp(id) && !cardGone(id),
    );
    const refocus = (note: HTMLElement) => {
      if (wasInside && focused && !board.contains(focused)) {
        // The old footer or card is gone: the new footer takes the focus, or the board when it
        // is hidden (nothing is left unset), so it never falls back to the page body.
        note.tabIndex = -1;
        if (!note.hidden) note.focus({ preventScroll: true });
        else {
          board.tabIndex = -1;
          board.focus({ preventScroll: true });
        }
      }
    };
    if (board.dataset.single !== undefined) {
      // A box admin's board: n columns, the card in the first, no groups (2026-11-06).
      const columns = Array.from({ length: n }, (_, at) =>
        el("div", { class: "admin-col" }, ...(at === 0 ? here.map((id) => cards[id]) : [])),
      );
      board.replaceChildren(...columns);
    } else if (n !== 1) {
      // Packed (2026-11-07): n column stacks, Needs you first in column 1, then the footer.
      const order = DOMAINS.flatMap((domain) => domain.cards.filter((id): id is CardId => here.includes(id as CardId)));
      for (const id of here) if (!order.includes(id)) order.push(id);
      const columns = packedArrangement(n, order).map((ids, at) =>
        el(
          "div",
          { class: "admin-col" },
          ...(at === 0 && attention ? [attention] : []),
          ...ids.map((id) => cards[id]),
        ),
      );
      const note = footerNote(cards);
      board.replaceChildren(...columns, note);
      board.dataset.packed = "";
      refocus(note);
    } else {
      // A phone: the group sections as before, and Needs you right before the board.
      if (attention && attention.nextElementSibling !== board) board.before(attention);
      const sections: HTMLElement[] = [];
      for (const domain of DOMAINS) {
        const known = here.filter((id) => groupOf(id) === domain.id);
        if (!known.length) continue;
        const columns = GROUP_LAYOUTS[1][domain.id]
          .map((ids) => ids.filter((id) => known.includes(id)))
          .filter((ids) => ids.length > 0)
          .map((ids) => el("div", { class: "admin-col" }, ...ids.map((id) => cards[id])));
        const groupNode = el(
          "section",
          { class: "admin-group", "data-group": domain.id },
          el(
            "h2",
            { class: "admin-group-h" },
            domain.icon(),
            domain.label,
            el("span", { class: "admin-group-line" }),
          ),
          el("div", { class: "admin-group-cols" }, ...columns),
        );
        // It spans (and divides into) only its own columns; the board packs the rest around it.
        groupNode.style.gridColumn = `span ${columns.length}`;
        (groupNode.querySelector(".admin-group-cols") as HTMLElement).style.setProperty(
          "--cols",
          String(columns.length),
        );
        sections.push(groupNode);
      }
      const note = footerNote(cards);
      board.replaceChildren(...sections, note);
      delete board.dataset.packed;
      refocus(note);
    }
    board.dataset.columns = String(n);
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
  // The Reset layout button shows or hides with the column count (fabric.ts).
  if (rebuilt) board.dispatchEvent(new Event("admin-arranged"));
}

/**
 * Lay the cards out in *board* by the width of *page*, again whenever it
 * changes and whenever a tile changes (a card that turns red unfolds unless
 * the owner folded it themselves). `refold` applies the remembered folds once they are
 * read; `stop` stops listening.
 */
export function mountBoard(
  page: HTMLElement,
  board: HTMLElement,
  cards: CardSet,
  attention?: HTMLElement,
): { stop: () => void; refold: () => void } {
  let width = page.clientWidth || window.innerWidth;
  const notSet = () =>
    (Object.keys(cards) as CardId[]).filter((id) => cardNotSetUp(id) || cardGone(id)).sort().join(",");
  let lastNotSet = notSet();
  const layout = (force = false) => {
    lastNotSet = notSet();
    arrange(board, cards, columnsFor(width), force, attention);
  };
  const refold = () => {
    for (const card of Object.values(cards)) applyFold(card, board.dataset.columns === "1");
    pairShelf(board);
  };
  layout();
  // A card that becomes (or stops being) not set up joins or leaves the board; anything else refolds.
  const stopTiles = onTiles(() => {
    if (notSet() !== lastNotSet) layout(true);
    else refold();
  });
  const stopLayout = onLayoutChange(() => layout(true));
  // A card turned off, switched back on, hidden or shown joins or leaves the board (2026-11-07).
  const stopCards = onCardState(() => layout(true));
  const stopDrag = enableDrag(board, cards);
  const stopAll = () => {
    stopTiles();
    stopLayout();
    stopCards();
    stopDrag();
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
  };
}
