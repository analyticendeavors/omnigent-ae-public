// What the page knows about the Admin Center's cards (2026-11-07, card setup).
//
// The server's card answer (`GET /v1/ae/cards`, admins only) says which cards are on and what
// is set up; the viewer's own "Hide for me" list (cardPrefs.ts) says which cards this person
// has put away. The board, the pills and Needs you ask here whether a card is gone. A plain
// module with no DOM, never a component file: the vitest suites mock whole component modules,
// and a missing export would fail every test that renders them.

import type { CardId } from "./board";
import type { TileId } from "./tiles";

/**
 * The tiles whose health and values a card's folded header line carries. Here, in a plain module,
 * because board.ts and tiles.ts both need it and board.ts imports tiles.ts (2026-11-07).
 */
export const CARD_TILES: Record<CardId, TileId[]> = {
  deploys: ["hold", "release"],
  accounts: ["accounts"],
  compute: ["fabric", "databricks"],
  credit: ["credit"],
  azure: ["azure"],
  box: ["boxhealth", "host", "secrets"],
  vault: ["vault"],
  decision: ["decision"],
  git: ["git"],
  presets: ["presets", "family"],
  social: ["social"],
  "home-security": ["homesecurity"],
};

export type CardGroup = "ship" | "box" | "cloud" | "money";

export interface CardField {
  label: string;
  /** The variable name, or its prefix when it ends in an underscore. Never a value. */
  env: string;
  kind: "text" | "list" | "number" | "date" | "bool" | "url" | "secret";
  hint: string;
  env_only: boolean;
  status: "set" | "missing";
  /** Typed in the panel (2026-11-07). Absent on an older server, which reads as false. */
  editable?: boolean;
  /** Where the value comes from: the environment wins, then the panel, then the code's default. */
  source?: "env" | "saved" | "default" | "missing";
  /** Read by the host's relay or scripts: set in config/host.env, not on the server. */
  host?: boolean;
  /** What the code uses when nothing is set (never a secret). */
  default?: string | null;
  saved_by?: string | null;
  saved_at?: string | null;
  /** The value typed in the panel for an editable field. An environment value is never sent. */
  value?: string | null;
  /** For a prefix field (one variable per account or capacity): how many are set, never names. */
  count?: number | null;
  unit?: string | null;
}

export interface CardRow {
  id: string;
  name: string;
  group: CardGroup;
  description: string;
  /** The Needs you sources this card owns (the server's attention.SOURCES). */
  sources: string[];
  on: boolean;
  default_on: boolean;
  changed_by: string | null;
  changed_at: string | null;
  fields: CardField[];
}

export interface CardsAnswer {
  revision: number;
  /** False when the server's state file is damaged: it shows defaults and refuses a switch. */
  state_ok: boolean;
  cards: CardRow[];
}

let answer: CardsAnswer | null = null;
let hidden = new Set<string>();
const listeners = new Set<() => void>();

function notify(): void {
  for (const listener of [...listeners]) listener();
}

/** The latest server answer, or null before one arrived (or for a viewer who is not an admin). */
export function getCards(): CardsAnswer | null {
  return answer;
}

/**
 * Take a server answer. One older than the one held is ignored (2026-11-07): a slow opening read
 * landing after a switch would otherwise flip the card back. `null` clears.
 */
export function setCards(next: CardsAnswer | null): void {
  if (next && answer && next.revision < answer.revision) return;
  answer = next;
  notify();
}

/** The ids this viewer has hidden for themselves. */
export function getHidden(): ReadonlySet<string> {
  return hidden;
}

export function setHidden(ids: Iterable<string>): void {
  hidden = new Set(ids);
  notify();
}

/**
 * Whether the server turned the card off. A card the answer does not list (a private card the
 * server did not opt in to) counts as off; with no answer yet nothing is off, so a page that
 * cannot read the cards (not an admin) behaves as before.
 */
export function cardOff(id: string): boolean {
  if (!answer) return false;
  const row = answer.cards.find((card) => card.id === id);
  return row ? !row.on : true;
}

/** Whether this viewer hid the card with "Hide for me". */
export function cardHidden(id: string): boolean {
  return hidden.has(id);
}

/** Off for everyone, or hidden for this viewer: the card leaves the page. */
export function cardGone(id: string): boolean {
  return cardOff(id) || cardHidden(id);
}

/** True when the tile belongs to a card that is off or hidden for this viewer. */
export function tileGone(tile: string): boolean {
  return (Object.keys(CARD_TILES) as CardId[]).some(
    (card) => (CARD_TILES[card] as string[]).includes(tile) && cardGone(card),
  );
}

/** True when this viewer hid the card that owns a Needs you source (off ones are gone server side). */
export function sourceHidden(source: string): boolean {
  return (
    answer?.cards.some((card) => card.sources.includes(source) && hidden.has(card.id)) ?? false
  );
}

/** Called after every change; returns the function that removes the listener. */
export function onCardState(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

/** For tests: forget everything. */
export function resetCardState(): void {
  answer = null;
  hidden = new Set();
  listeners.clear();
}
