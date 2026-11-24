// The Overview wall (2026-11-17, option 3 "list and wall"): every card on one screen, by area.
//
// Why: the owner found the collapsed default empty at his real window. The Overview now holds the REAL
// cards, moved out of their tabs' boards into a compact style (style.css, scoped to `.wall-tile`), so
// every number stays live and every visible button works with no second copy of a card. A card sits in
// one place at a time: the shell takes the cards when the Overview shows and gives them back to their
// boards (board.ts `layout`) when another tab opens, and takes them again after a board rebuilds.
//
// The wall draws one section per area in DOMAINS order (a small label, then a six-column grid of that
// area's cards in `domain.cards` order). A card that is off, hidden for this viewer or not set up is
// skipped, and an area left with none draws no label. Each card sits in a tile wrapper that carries the
// border, the background, the grid span and, on its left edge, what needs you for the card's source
// (red for urgent, amber for review). A redraw changes only what differs: a card already in its place is
// not moved, and a focused control keeps its focus.

import { type CardId, type CardSet, cardNotSetUp, setCardWall } from "./board";
import { cardGone, CARD_TILES, onCardState } from "./cardState";
import { onAttention } from "./attentionStore";
import { lastCpuHistory } from "./boxHealth";
import { el } from "./dom";
import { areaLabel, cardCounts } from "./needsModel";
import { heatMeter } from "./rail";
import { DOMAINS, getTile, onTiles } from "./tiles";

/**
 * How many of the grid's six columns each of an area's cards spans, by how many cards the area shows:
 * one spans 6; two span 3 and 3 (Box health takes 4 beside one other card); three span 2 each; four go
 * 3 and 3 on two rows; five go 2, 2, 2 then 3, 3; six or more continue in rows of three.
 */
export function spansFor(ids: readonly string[]): number[] {
  const n = ids.length;
  if (n === 0) return [];
  if (n === 1) return [6];
  if (n === 2) return ids.includes("box") ? ids.map((id) => (id === "box" ? 4 : 2)) : [3, 3];
  if (n === 4) return [3, 3, 3, 3];
  if (n === 5) return [2, 2, 2, 3, 3];
  const spans: number[] = [];
  for (let i = 0; i < n; i += 3) {
    const left = Math.min(3, n - i);
    spans.push(...(left === 1 ? [6] : left === 2 ? [3, 3] : [2, 2, 2]));
  }
  return spans;
}

/** The cards one area shows on the wall, in the area's order. */
export function wallCards(domainCards: readonly string[], cards: CardSet, drawn?: ReadonlySet<string>): CardId[] {
  return domainCards.filter(
    (id): id is CardId => !!cards[id as CardId] && !cardGone(id) && (!cardNotSetUp(id as CardId) || keptOnBadRead(id as CardId, drawn)),
  );
}

/**
 * A card this page has already drawn stays when its tile turns not-set-up on a read that failed (2026-11-18): only a
 * clean not-set-up answer takes it off. A card the first clean read says is not set up was never drawn, so it stays off.
 */
function keptOnBadRead(id: CardId, drawn?: ReadonlySet<string>): boolean {
  return !!drawn?.has(id) && CARD_TILES[id].some((tile) => getTile(tile)?.failed === true);
}

/** The pills in a card's header: always full words ("3 urgent", "1 to review"), never a bare number (2026-11-18). */
function pillsFor(counts: { urgent: number; review: number } | null): HTMLElement | null {
  if (!counts || (!counts.urgent && !counts.review)) return null;
  const parts: Array<{ tone: "urgent" | "review"; words: string }> = [];
  if (counts.urgent) parts.push({ tone: "urgent", words: `${counts.urgent} urgent` });
  if (counts.review) parts.push({ tone: "review", words: `${counts.review} to review` });
  const label = parts.map((part) => part.words).join(", ");
  return el(
    "span",
    { class: "nt-pills wall-pills", role: "img", "aria-label": label, title: label },
    ...parts.map((part) =>
      el("span", { class: `nt-pill nt-pill-${part.tone}`, title: part.words }, part.words),
    ),
  );
}

export interface Wall {
  /** Move the cards onto the wall (a no-op for a card already in its place). */
  take: () => void;
  /** Hand every card back (the shell then asks each board to place them again); false when it held none. */
  release: () => boolean;
  /** The pane holds this card (null: none): the wall leaves it where it is, and takes the last one back. */
  lend: (card: string | null) => void;
  stop: () => void;
}

export function mountWall(host: HTMLElement, cards: CardSet): Wall {
  const sections = new Map<string, { section: HTMLElement; grid: HTMLElement }>();
  const tiles = new Map<string, HTMLElement>();
  let taken = false;
  // Every card this wall has drawn during the page's life (see keptOnBadRead).
  const drawn = new Set<string>();
  // The card the pane holds (needsPane.ts): not moved and not decorated until it is handed back.
  let lent: string | null = null;

  /** Put *node* at *index* among *parent*'s children, moving it only when it is not already there. */
  const place = (parent: HTMLElement, node: HTMLElement, index: number): void => {
    if (parent.children[index] !== node) parent.insertBefore(node, parent.children[index] ?? null);
  };

  const section = (id: string): { section: HTMLElement; grid: HTMLElement } => {
    let found = sections.get(id);
    if (found) return found;
    const domain = DOMAINS.find((d) => d.id === id)!;
    const grid = el("div", { class: "wall-grid" });
    const node = el(
      "section",
      { class: "wall-area", "data-area": id, "aria-label": areaLabel(domain.id) },
      el(
        "div",
        { class: "wall-area-label" },
        el("span", { class: "wall-area-icon", "aria-hidden": "true" }, domain.icon()),
        el("span", { class: "wall-area-name" }, domain.label),
        el("span", { class: "wall-area-rule", "aria-hidden": "true" }),
      ),
      grid,
    );
    found = { section: node, grid };
    sections.set(id, found);
    return found;
  };

  const decorate = (id: CardId, tile: HTMLElement): void => {
    const counts = cardCounts(id);
    const tone = counts?.urgent ? "urgent" : counts?.review ? "review" : "none";
    if (tile.dataset.tone !== tone) tile.dataset.tone = tone;
    const card = cards[id]!;
    const top = card.querySelector<HTMLElement>(".admin-card-top");
    if (top) {
      const next = pillsFor(counts);
      const old = top.querySelector<HTMLElement>(":scope > .wall-pills");
      if (!next) old?.remove();
      else if (!old) top.querySelector(".admin-card-h")?.after(next);
      else if (old.getAttribute("aria-label") !== next.getAttribute("aria-label") || old.textContent !== next.textContent) old.replaceWith(next);
    }
    // The Box health tile shows the CPU heat line under the card, inside the same wrapper. It is built
    // again only when the readings changed, so a tick that changes nothing leaves the node alone.
    const history = id === "box" ? lastCpuHistory() : null;
    const key = history ? history.join(",") : "";
    const old = tile.querySelector<HTMLElement>(":scope > .wall-heat");
    if (!history) old?.remove();
    else if (old?.dataset.key !== key) {
      const heat = heatMeter();
      if (heat) {
        heat.classList.add("wall-heat");
        heat.dataset.key = key;
        // The line keeps a natural height and stretches into spare height (an absolutely placed svg in a
        // box that grows), so it never sets the tile's height by its aspect ratio.
        const svg = heat.querySelector("svg");
        if (svg) {
          svg.setAttribute("preserveAspectRatio", "none");
          const chart = el("span", { class: "wall-heat-chart" });
          svg.replaceWith(chart);
          chart.append(svg);
        }
        if (old) old.replaceWith(heat);
        else tile.append(heat);
      }
    }
    if (id === "box") {
      // The wall's spare height goes to the heat line first (style.css reads this flag).
      if (tile.querySelector(":scope > .wall-heat")) host.dataset.heat = "1";
      else delete host.dataset.heat;
    }
  };

  const draw = (): void => {
    if (!taken) return;
    const focused = host.contains(document.activeElement) ? (document.activeElement as HTMLElement) : null;
    const plan = DOMAINS.map((domain) => ({ domain, ids: wallCards(domain.cards, cards, drawn) })).filter((entry) => entry.ids.length);
    const wanted = new Set(plan.flatMap((entry) => entry.ids as string[]));
    for (const id of wanted) drawn.add(id);
    // A card that left the plan (turned off, hidden, not set up) goes with its wrapper.
    for (const [id, tile] of [...tiles]) {
      if (wanted.has(id)) continue;
      const card = cards[id as CardId];
      if (card) {
        setCardWall(card, false);
        card.querySelector(":scope > .admin-card-top > .wall-pills")?.remove();
      }
      tile.remove();
      tiles.delete(id);
    }
    if (!wanted.has("box")) delete host.dataset.heat;
    const areas = new Set(plan.map((entry) => entry.domain.id as string));
    for (const [id, found] of [...sections]) {
      if (areas.has(id)) continue;
      found.section.remove();
      sections.delete(id);
    }
    plan.forEach((entry, areaIndex) => {
      const { section: node, grid } = section(entry.domain.id);
      place(host, node, areaIndex);
      const spans = spansFor(entry.ids);
      entry.ids.forEach((id, index) => {
        const card = cards[id]!;
        let tile = tiles.get(id);
        if (!tile) {
          tile = el("div", { class: "wall-tile", "data-card": id, "data-area": entry.domain.id });
          tiles.set(id, tile);
        }
        const span = String(spans[index]);
        if (tile.dataset.span !== span) tile.dataset.span = span;
        if (id === lent) {
          place(grid, tile, index);
          return;
        }
        if (card.parentElement !== tile) {
          setCardWall(card, true);
          tile.prepend(card);
        } else if (card.dataset.wall !== "1") setCardWall(card, true);
        place(grid, tile, index);
        if (id !== lent) decorate(id, tile);
      });
    });
    if (focused && focused.isConnected && document.activeElement !== focused) focused.focus({ preventScroll: true });
  };

  const stops = [onTiles(draw), onCardState(draw), onAttention(draw)];

  return {
    take: () => {
      taken = true;
      draw();
    },
    release: () => {
      taken = false;
      const held = tiles.size > 0;
      for (const [id, tile] of tiles) {
        const card = cards[id as CardId];
        if (card) {
          setCardWall(card, false);
          card.querySelector(":scope > .admin-card-top > .wall-pills")?.remove();
        }
        tile.remove();
      }
      tiles.clear();
      lent = null;
      delete host.dataset.heat;
      for (const found of sections.values()) found.section.remove();
      sections.clear();
      return held;
    },
    lend: (card) => {
      if (lent === card) return;
      lent = card;
      // A card handed back is drawn into its tile again at once.
      draw();
    },
    stop: () => {
      for (const stop of stops) stop();
    },
  };
}
