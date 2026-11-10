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
// The cards' folded header lines carry the same values on a phone (board.ts). The page header's
// "Updated" time and its refresh button live here too: every section reports
// when it last answered and registers its normal read, and the button runs
// those reads (never the Databricks spend Refresh, which wakes a warehouse).

import { onCardState, tileGone } from "./cardState";
import { goToCard, goToGroup } from "./cardNav";
import { el, replaceChildren } from "./dom";
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
let strip: HTMLElement | null = null;
/** Redraws the pills when a card is switched off or hidden (2026-11-07). */
let stopCards: (() => void) | null = null;
let updated: HTMLElement | null = null;

/** A section's tile after a draw; *at* is when its answer arrived, if it did. */
export function setTile(tile: Tile, at?: number): void {
  tiles.set(tile.id, tile);
  if (at && at > updatedAt) updatedAt = at;
  draw();
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

/** Draw the strip into *node* and the newest answer's time into *time* from now on. */
export function mountTiles(node: HTMLElement, time: HTMLElement): void {
  strip = node;
  updated = time;
  stopCards?.();
  stopCards = onCardState(() => draw());
  pills.clear();
  node.replaceChildren();
  node.tabIndex = -1;
  draw();
}

const plural = (n: number, one: string, many = `${one}s`) => `${n} ${n === 1 ? one : many}`;

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

/** The count at a pill's right edge: "1 to check", "2 problems", nothing when ok. */
export function pillCount(state: PillState): string {
  const n = (health: Health) => state.members.filter((tile) => tile.health === health).length;
  if (state.health === "problem") return plural(n("problem"), "problem");
  if (state.health === "attention") return `${n("attention")} to check`;
  return "";
}

/** The compact number in the pill's header: how many members are not ok ("" when ok). */
export function pillNumber(state: PillState): string {
  if (state.health === "ok") return "";
  const bad = state.members.filter((tile) => tile.health === state.health).length;
  return String(bad);
}

/** What the pill's line says for a member: its brief, else summary, else value (never the note). */
export const memberText = (tile: Tile) => tile.brief ?? tile.summary ?? tile.value;

/** "Release v1, up to date; Deploy hold: Off" for the pill's title and aria-label. */
export function pillDescription(state: PillState): string {
  const members = state.members.map((tile) =>
    tile.note ? `${tileLabel(tile.id)}: ${tile.value}, ${tile.note}` : `${tileLabel(tile.id)}: ${tile.value}`,
  );
  const count = pillCount(state);
  return `${state.domain.label}${count ? `, ${count}` : ""}: ${members.join("; ")}`;
}

/**
 * The pill's one line: problem members first, then attention (both bold), then the ok ones.
 * Idle members are skipped, and a part whose text repeats an earlier one is dropped.
 */
function pillLine(state: PillState): Node[] {
  const live = state.members.filter((tile) => tile.health !== "idle");
  const order: Health[] = ["problem", "attention", "ok"];
  const sorted = order.flatMap((health) => live.filter((tile) => tile.health === health));
  const seen = new Set<string>();
  const parts: Node[] = [];
  for (const tile of sorted) {
    const words = memberText(tile);
    const key = words.trim().toLowerCase();
    if (!key || seen.has(key)) continue;
    seen.add(key);
    parts.push(tile.health === "ok" ? document.createTextNode(words) : el("b", {}, words));
  }
  return parts.flatMap((part, at) => (at ? [document.createTextNode(" \u00b7 "), part] : [part]));
}

function popoverRows(state: PillState): HTMLElement[] {
  return state.members.map((tile) =>
    el(
      "div",
      { class: `pill-pop-row tile-${tile.health}`, title: tile.title },
      el("span", { class: "pill-dot" }),
      el("span", { class: "pill-pop-name" }, tileLabel(tile.id)),
      el("span", { class: "pill-pop-value" }, tile.note ? `${tile.value} \u00b7 ${tile.note}` : tile.value),
    ),
  );
}

/** One pill's nodes, built once per mount and patched by patchPill (2026-11-06, review). */
interface PillNodes {
  wrap: HTMLElement;
  button: HTMLButtonElement;
  count: HTMLElement;
  line: HTMLElement;
  pop: HTMLElement;
  bar: HTMLElement;
  fill: HTMLElement;
}

const pills = new Map<DomainId, PillNodes>();

function buildPill(domain: Domain): PillNodes {
  const count = el("span", { class: "pill-count" });
  const line = el("span", { class: "pill-line" });
  const fill = el("i", {});
  const bar = el("div", { class: "tile-bar", "aria-hidden": "true" }, fill);
  const pop = el("div", { class: "pill-pop", "aria-hidden": "true" });
  const button = el(
    "button",
    { type: "button", class: "pill", "data-pill": domain.id },
    el(
      "span",
      { class: "pill-h" },
      el("span", { class: "pill-dot", "aria-hidden": "true" }),
      domain.icon(),
      el("span", { class: "pill-name" }, domain.label),
      count,
    ),
    line,
    bar,
  );
  button.addEventListener("click", () => {
    if (goToGroup(domain.id)) return;
    for (const card of domain.cards) if (goToCard(card)) return;
  });
  return { wrap: el("div", { class: "pill-wrap" }, button, pop), button, count, line, pop, bar, fill };
}

/** Update a pill's text, class and popover in place, so focus and an open popover survive a draw. */
function patchPill(nodes: PillNodes, state: PillState): void {
  const number = pillNumber(state);
  const description = pillDescription(state);
  const hold = state.members.find((tile) => tile.id === "hold" && tile.bar != null);
  nodes.button.className = `pill pill-${state.health}`;
  if (nodes.button.getAttribute("aria-label") !== description) nodes.button.setAttribute("aria-label", description);
  nodes.count.textContent = number;
  nodes.count.hidden = !number;
  replaceChildren(nodes.line, pillLine(state));
  replaceChildren(nodes.pop, popoverRows(state));
  nodes.bar.hidden = !hold;
  nodes.fill.style.width = hold ? `${hold.bar}%` : "0%";
}

function draw(): void {
  if (strip) {
    const states = pillStates();
    const wanted = states.map((state) => {
      let nodes = pills.get(state.domain.id);
      if (!nodes) {
        nodes = buildPill(state.domain);
        pills.set(state.domain.id, nodes);
      }
      patchPill(nodes, state);
      return nodes.wrap;
    });
    const drawn = new Set(states.map((state) => state.domain.id));
    for (const [id, nodes] of pills) {
      if (drawn.has(id)) continue;
      // A focused pill that goes away hands focus to the strip, not to the body.
      const hadFocus = nodes.wrap.contains(document.activeElement);
      nodes.wrap.remove();
      if (hadFocus) strip.focus({ preventScroll: true });
    }
    // Only move a node that is out of place, so a focused pill is never torn out of the page.
    wanted.forEach((wrap, at) => {
      if (strip!.children[at] !== wrap) strip!.insertBefore(wrap, strip!.children[at] ?? null);
    });
    while (strip.children.length > wanted.length) strip.lastElementChild?.remove();
  }
  if (updated) {
    updated.textContent = updatedAt
      ? `Updated ${new Date(updatedAt).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })}`
      : "";
  }
}

/** Forget every tile, reader and listener (deactivate, and tests). */
export function resetTiles(): void {
  stopCards?.();
  stopCards = null;
  tiles.clear();
  listeners.clear();
  refreshers.clear();
  updatedAt = 0;
  strip = null;
  updated = null;
  pills.clear();
}
