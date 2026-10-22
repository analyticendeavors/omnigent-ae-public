// The Admin Center's status tile strip (2026-10-20, the dashboard redesign
// Reid approved as concept C): one small tile per thing the page watches,
// across the top of the page, so the state of the box reads at a glance before
// any card is opened.
//
// Why: the page had grown to six sections in one 720 px column, and on a wide
// pane the answer to "is anything wrong?" sat several scrolls down. The tiles
// read only what the sections already fetch: each section builds its own tile
// from its own state (deploys.ts, host.ts, fabric.ts, databricks.ts,
// secrets.ts, accounts.ts) and hands it here after every draw, so the strip adds no request
// of its own. A tile has a label with an icon, one value, a muted note and a
// coloured left edge by health: green ok, amber needs attention, grey idle,
// red a problem. The Deploy hold tile adds a bar of the hold's time left.
//
// Shared accounts is a count tile: their management moves to OmniGent's
// Settings > Claude accounts, and an extension page cannot open a Settings
// page (the SDK's navigation.openPage opens only the extension's own pages and
// openExternal only URLs the host handed out), so the tile says where to go.
// Since 2026-10-21 it is the Accounts tile (accounts.ts): every signed-in
// account, not only the shared ones, and the fullest 5-hour or weekly window,
// coloured by the same steps as the Accounts card's bars.
//
// The strip is hidden below 520 px of page (style.css); the cards' folded
// header lines carry the same values there (board.ts). The page header's
// "Updated" time and its refresh button live here too: every section reports
// when it last answered and registers its normal read, and the button runs
// those reads (never the Databricks spend Refresh, which wakes a warehouse).

import { el, replaceChildren } from "./dom";
import {
  boltIcon,
  databaseIcon,
  keyIcon,
  pauseIcon,
  serverIcon,
  sharedIcon,
  tagIcon,
} from "./icons";

export type Health = "ok" | "attention" | "idle" | "problem";

/** "credit" is not drawn in the strip: it carries the credit card's folded line and health. */
export type TileId =
  | "release"
  | "hold"
  | "host"
  | "fabric"
  | "databricks"
  | "secrets"
  | "accounts"
  | "credit";

export interface Tile {
  id: TileId;
  value: string;
  note: string;
  health: Health;
  /** A status dot before the value, in the health's colour. */
  dot?: boolean;
  /** The Deploy hold's bar, 0 to 100. */
  bar?: number | null;
  /** A tooltip with more than the note says. */
  title?: string;
  /** How a folded card's header line names it ("Fabric active"); the value when absent. */
  summary?: string;
}

/** The strip's order and each tile's label and icon. */
export const TILE_ORDER: ReadonlyArray<{ id: TileId; label: string; icon: () => SVGElement }> = [
  { id: "release", label: "Release", icon: tagIcon },
  { id: "hold", label: "Deploy hold", icon: pauseIcon },
  { id: "host", label: "Box host", icon: serverIcon },
  { id: "fabric", label: "Fabric", icon: () => boltIcon() },
  { id: "databricks", label: "Databricks", icon: databaseIcon },
  { id: "secrets", label: "Secrets", icon: keyIcon },
  { id: "accounts", label: "Accounts", icon: sharedIcon },
];

const RANK: Record<Health, number> = { problem: 3, attention: 2, ok: 1, idle: 0 };

/** The worse of several healths: a problem beats attention beats ok beats idle. */
export function worst(healths: Health[]): Health {
  return healths.reduce<Health>((a, b) => (RANK[b] > RANK[a] ? b : a), "idle");
}

/**
 * A tile built from the last good answer while the newest read failed: the
 * note says so and the health is at least "attention", so the strip never
 * shows a green tile over a card that is saying "the refresh failed".
 */
export function staleTile(tile: Tile, error: unknown): Tile {
  if (!error) return tile;
  return {
    ...tile,
    note: tile.note ? `${tile.note} \u00b7 last answer` : "last answer",
    health: worst([tile.health, "attention"]),
  };
}

/** "1 h 50 m" or "12 m" for a count of seconds, rounded up to the minute. */
export function shortLeft(seconds: number): string {
  const minutes = Math.max(1, Math.ceil(seconds / 60));
  const hours = Math.floor(minutes / 60);
  const rest = minutes % 60;
  if (hours === 0) return `${rest} m`;
  return rest === 0 ? `${hours} h` : `${hours} h ${rest} m`;
}

// ── the store ───────────────────────────────────────────────────────────────

const tiles = new Map<TileId, Tile>();
const listeners = new Set<() => void>();
const refreshers = new Map<string, () => unknown>();
/** The newest answer any section reported, for the page header's "Updated". */
let updatedAt = 0;
let strip: HTMLElement | null = null;
let updated: HTMLElement | null = null;

/** A section's tile after a draw; *at* is when its answer arrived, if it did. */
export function setTile(tile: Tile, at?: number): void {
  tiles.set(tile.id, tile);
  if (at && at > updatedAt) updatedAt = at;
  draw();
  for (const listener of listeners) listener();
}

export function getTile(id: TileId): Tile | undefined {
  return tiles.get(id);
}

/** Called after every tile change (board.ts folds and sums its cards from them). */
export function onTiles(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

/** A section's normal read, for the page header's refresh button. */
export function registerRefresh(id: string, read: () => unknown): void {
  refreshers.set(id, read);
}

/** Run every registered read once. */
export function refreshAll(): void {
  for (const read of refreshers.values()) void read();
}

/** Draw the strip into *node* and the newest answer's time into *time* from now on. */
export function mountTiles(node: HTMLElement, time: HTMLElement): void {
  strip = node;
  updated = time;
  draw();
}

function tileNode(spec: (typeof TILE_ORDER)[number], tile: Tile): HTMLElement {
  const bar =
    tile.bar == null
      ? null
      : el("div", { class: "tile-bar", "aria-hidden": "true" }, el("i", {}));
  if (bar) (bar.firstElementChild as HTMLElement).style.width = `${tile.bar}%`;
  return el(
    "div",
    {
      class: `tile tile-${tile.health}`,
      "data-tile": tile.id,
      role: "group",
      "aria-label": `${spec.label}: ${tile.value}, ${tile.note}`,
      title: tile.title,
    },
    el("span", { class: "tile-label" }, spec.icon(), spec.label),
    el(
      "span",
      { class: "tile-value" },
      tile.dot ? el("span", { class: "tile-dot", "aria-hidden": "true" }) : null,
      tile.value,
    ),
    el("span", { class: "tile-note" }, tile.note),
    bar,
  );
}

function draw(): void {
  if (strip) {
    replaceChildren(
      strip,
      TILE_ORDER.flatMap((spec) => {
        const tile = tiles.get(spec.id);
        return tile ? [tileNode(spec, tile)] : [];
      }),
    );
  }
  if (updated) {
    updated.textContent = updatedAt
      ? `Updated ${new Date(updatedAt).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })}`
      : "";
  }
}

/** Forget every tile, reader and listener (deactivate, and tests). */
export function resetTiles(): void {
  tiles.clear();
  listeners.clear();
  refreshers.clear();
  updatedAt = 0;
  strip = null;
  updated = null;
}
