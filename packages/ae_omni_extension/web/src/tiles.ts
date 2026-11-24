// The Admin Center's status tile strip (2026-10-20, the dashboard redesign
// the owner approved as concept C): one small tile per thing the page watches,
// across the top of the page, so the state of the box reads at a glance before
// any card is opened.
//
// Why: the page had grown to six sections in one 720 px column, and on a wide
// pane the answer to "is anything wrong?" sat several scrolls down. The tiles
// read only what the sections already fetch: each section builds its own tile
// from its own state (deploys.ts, boxHealth.ts, host.ts, fabric.ts, databricks.ts,
// secrets.ts, vault.ts, accounts.ts, git.ts) and hands it here after every draw, so the strip adds no request
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
// 2026-11-06: the ten tiles became four domain pills (Ship, Box, Cloud, Accounts).
// Why: ten tiles wrapped to four rows above Needs you, so "is anything other than Needs you
// wrong?" took a scroll to answer; four pills read in one row (two by two on a phone).
// Each pill is the worst health of its members, its one line says the members that are not ok
// first, and a hover or focus popover lists every member. Clicking a pill goes to the its
// group's heading on a phone and rings the group's cards on the packed board (cardNav.ts goToGroup), the first card when the board has no groups. The tile store and setTile are unchanged, so no section moved.
//
// 2026-11-14 (concept D2, Reid's pick): the pill strip is gone. rail.ts draws each area's members from this
// store beside the Needs you table, and verdict.ts still asks pillStates() whether every area is ok.
// The store, setTile and the "Updated" time are unchanged.
//
// The cards' folded header lines carry the same values on a phone (board.ts). The page header's
// "Updated" time and its refresh button live here too: every section reports
// when it last answered and registers its normal read, and the button runs
// those reads (never the Databricks spend Refresh, which wakes a warehouse).

import { tileGone } from "./cardState";
import {
  boltIcon,
  cloudIcon,
  databaseIcon,
  gaugeIcon,
  calendarEventIcon,
  gitBranchIcon,
  keyIcon,
  lockIcon,
  pauseIcon,
  rocketIcon,
  scaleIcon,
  serverIcon,
  shieldCheckIcon,
  sharedIcon,
  tagIcon,
} from "./icons";

export type Health = "ok" | "attention" | "idle" | "problem";

/** "credit" is in no pill; "azure", "presets" and "family" are members without a tile of their own. */
export type TileId =
  | "release"
  | "hold"
  | "boxhealth"
  | "host"
  | "fabric"
  | "databricks"
  | "secrets"
  | "vault"
  | "decision"
  | "homesecurity"
  | "git"
  | "social"
  | "accounts"
  | "credit"
  | "azure"
  | "presets"
  | "family";

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
  /** A short phrase (3 to 5 words) for the status pill's line; never the note. Falls back to summary, then value. */
  brief?: string;
  /**
   * The server said this integration is not configured (2026-11-06). Never set for an error,
   * a 403, an admins-only answer or "Reading...". The board leaves the card out and the
   * pills leave the tile out; the health stays idle.
   */
  notSetUp?: true;
  /**
   * The read behind this tile failed or only partly came back (2026-11-18). A not-set-up tile that carries it is
   * not a clean answer: the wall keeps a card it has already drawn instead of dropping it on a bad read.
   */
  failed?: true;
}

/** The strip's order and each tile's label and icon. */
export const TILE_ORDER: ReadonlyArray<{ id: TileId; label: string; icon: () => SVGElement }> = [
  { id: "release", label: "Release", icon: tagIcon },
  { id: "hold", label: "Deploy hold", icon: pauseIcon },
  // 2026-10-28: the box monitor's tile (boxHealth.ts), a tenth, just before the host's.
  { id: "boxhealth", label: "Box health", icon: () => gaugeIcon() },
  { id: "host", label: "Box host", icon: serverIcon },
  { id: "fabric", label: "Fabric", icon: () => boltIcon() },
  { id: "databricks", label: "Databricks", icon: databaseIcon },
  // 2026-10-23: the Secrets tile split in two, the box's keys and every vault secret.
  { id: "secrets", label: "Secrets on box", icon: keyIcon },
  { id: "vault", label: "Key Vault", icon: () => lockIcon() },
  // 2026-11-07: the Decision model card (decision.ts), in the Cloud pill.
  { id: "decision", label: "Decision model", icon: () => scaleIcon() },
  // 2026-11-08: the Home security card (homeSecurity.ts), in the Box pill.
  { id: "homesecurity", label: "Home security", icon: () => shieldCheckIcon() },
  { id: "accounts", label: "Accounts", icon: sharedIcon },
  // 2026-10-24: the Git repos tile (git.ts), a ninth.
  { id: "git", label: "Git", icon: () => gitBranchIcon() },
  // 2026-11-06: the Social card's tile (social.ts), last.
  { id: "social", label: "Social", icon: () => calendarEventIcon() },
];

/** Labels for the member tiles that TILE_ORDER does not list. */
const EXTRA_LABELS: Partial<Record<TileId, string>> = {
  azure: "Azure spend",
  presets: "Spend",
  family: "Family cost",
  credit: "Databricks credit",
};

export function tileLabel(id: TileId): string {
  return TILE_ORDER.find((spec) => spec.id === id)?.label ?? EXTRA_LABELS[id] ?? id;
}

export type DomainId = "ship" | "box" | "cloud" | "money";

export interface Domain {
  id: DomainId;
  label: string;
  icon: () => SVGElement;
  members: TileId[];
  /** The cards in this group, in order: the one source of which group a card is in (board.ts groupOf). */
  cards: string[];
}

/** The four pills, in order (2026-11-06). */
export const DOMAINS: ReadonlyArray<Domain> = [
  {
    id: "ship",
    label: "Ship",
    icon: rocketIcon,
    members: ["release", "hold", "git", "social"],
    cards: ["deploys", "git", "social"],
  },
  { id: "box", label: "Box", icon: serverIcon, members: ["boxhealth", "host", "secrets", "homesecurity"], cards: ["box", "home-security"] },
  {
    id: "cloud",
    label: "Cloud",
    icon: cloudIcon,
    members: ["fabric", "databricks", "azure", "vault", "decision"],
    cards: ["compute", "credit", "azure", "vault", "decision"],
  },
  {
    id: "money",
    label: "Accounts",
    icon: () => sharedIcon(),
    members: ["accounts", "presets", "family"],
    cards: ["accounts", "presets"],
  },
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
  // A failed read says nothing about "not configured": the card must come back with its error
  // state and leave the Not set up line (2026-11-06, review).
  const { notSetUp: _gone, ...rest } = tile;
  return {
    ...rest,
    note: tile.note ? `${tile.note} \u00b7 last answer` : "last answer",
    // The pill line shows only the brief, so it carries the hint too (2026-11-06).
    brief: `${tile.brief ?? tile.summary ?? tile.value}, last answer`,
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
let updated: HTMLElement | null = null;

/** A section's tile after a draw; *at* is when its answer arrived, if it did. */
export function setTile(tile: Tile, at?: number): void {
  tiles.set(tile.id, tile);
  if (at && at > updatedAt) updatedAt = at;
  drawUpdated();
  for (const listener of listeners) listener();
}

/** Every member tile of every group has called setTile at least once (the verdict's "fine" needs it). */
export function allReported(): boolean {
  return DOMAINS.every((domain) => domain.members.every((id) => tiles.has(id)));
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

/** Keep the newest answer's time in *time* from now on (the page header's "Updated"). */
export function mountUpdated(time: HTMLElement): void {
  updated = time;
  drawUpdated();
}

export interface PillState {
  domain: Domain;
  health: Health;
  /** Members that have reported, in the domain's order, idle ones included. */
  members: Tile[];
}

/** The pills that would be drawn: a domain whose members are all missing or idle is left out. */
export function pillStates(): PillState[] {
  return DOMAINS.flatMap((domain) => {
    const members = domain.members.flatMap((id) => {
      const tile = tiles.get(id);
      return tile && !tile.notSetUp && !tileGone(id) ? [tile] : [];
    });
    const health = worst(members.map((tile) => tile.health));
    return health === "idle" ? [] : [{ domain, health, members }];
  });
}

/** What a member reads as in the rail: its brief, else summary, else value (never the note). */
export const memberText = (tile: Tile) => tile.brief ?? tile.summary ?? tile.value;

function drawUpdated(): void {
  if (!updated) return;
  updated.textContent = updatedAt
    ? `Updated ${new Date(updatedAt).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })}`
    : "";
}

/** Forget every tile, reader and listener (deactivate, and tests). */
export function resetTiles(): void {
  tiles.clear();
  listeners.clear();
  refreshers.clear();
  updatedAt = 0;
  updated = null;
}
