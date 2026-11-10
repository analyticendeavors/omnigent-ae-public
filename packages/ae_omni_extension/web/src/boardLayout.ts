// Where each Admin Center card sits (2026-10-22), and since 2026-11-07 one packed board.
//
// Why: the cards sit in column stacks (board.ts). On two or more columns the board is packed:
// Needs you is the first tile of column 1, then every card goes into the shortest column by its
// guessed height. The owner can drag a card anywhere on the board; that arrangement is saved per
// column count (2, 3 and 4), for the whole board, in the SDK's `storage.user` like the card
// folds (sections.ts). A phone is one column and is not arranged.
//
// 2026-11-07 (packed board): the saved value moved to a new key, `board.packed`, shaped
// {"2": [[..],[..]], "3": [...], "4": [...]} (only the counts the owner dragged). The old
// per-group `board.layout` is no longer read and not deleted, so a revert gets it back.
//
// A saved arrangement is read defensively: a card id the board no longer has is dropped, a card
// listed twice keeps its first place, and a card the save lacks (one added after it was saved)
// goes to the shortest column by guess. A value that is the wrong shape, the wrong column count
// or has no known card left is ignored, so a bad value can never leave the page without a card.

import type { ExtensionContext } from "@omnigent/extension-sdk";
import type { CardId } from "./board";

export const LAYOUT_KEY = "board.packed";

export type Columns = 1 | 2 | 3 | 4;
export type Packed = 2 | 3 | 4;
export type Layout = CardId[][];
export type Direction = "up" | "down" | "left" | "right";

type Key = "2" | "3" | "4";
const KEYS: readonly Key[] = ["2", "3", "4"];

let saved: Partial<Record<Key, string[][]>> = {};
/** Column counts changed on the page before the stored arrangement arrived; they win over it. */
const touched = new Set<Key>();
let persist: (() => void) | null = null;
const listeners = new Set<() => void>();

function notify(): void {
  for (const listener of listeners) listener();
}

function write(): void {
  persist?.();
  notify();
}

const isColumns = (value: unknown): value is string[][] =>
  Array.isArray(value) &&
  value.every((col) => Array.isArray(col) && col.every((id) => typeof id === "string"));

/** Read the saved arrangement; the page works the same without it. */
export async function loadBoardLayout(context: ExtensionContext): Promise<void> {
  persist = () => {
    try {
      context.storage.user.set(LAYOUT_KEY, saved).catch(() => {
        // storage.user unavailable: kept in memory until the page closes.
      });
    } catch {
      // The same, refused before a promise was made.
    }
  };
  try {
    const found = await context.storage.user.get<unknown>(LAYOUT_KEY);
    if (found && typeof found === "object" && !Array.isArray(found)) {
      const next: Partial<Record<Key, string[][]>> = {};
      let changed = false;
      for (const key of KEYS) {
        // A drag or a reset made while the read was out is newer than what it read.
        if (touched.has(key)) {
          if (saved[key]) next[key] = saved[key];
          continue;
        }
        const entry = (found as Record<string, unknown>)[key];
        // Only a save the board would use counts: one column per column and at least one card,
        // or Reset layout would show for a layout that is not on the page.
        if (isColumns(entry) && entry.length === Number(key) && entry.some((col) => col.length > 0)) {
          next[key] = entry;
          changed = true;
        }
      }
      saved = next;
      if (touched.size) persist?.();
      if (changed) notify();
    }
  } catch {
    // Same as a failed write: the packed default.
  }
}

/**
 * How tall each card is on the packed board, in px, measured live 2026-11-07 at about 435 px a
 * card (three columns on a 4:3 screen; narrower cards are taller, so these err tall). Unsaved
 * cards are placed by these alone (never by what is rendered), so where a card goes depends only
 * on the card set and the column count and never moves after render.
 */
export const CARD_HEIGHT_GUESS: Record<CardId, number> = {
  deploys: 194,
  git: 504,
  social: 511,
  box: 549,
  accounts: 430,
  presets: 398,
  compute: 493,
  credit: 256,
  azure: 747,
  vault: 621,
  decision: 282,
  // 2026-11-08: three rows and a link line, about the Decision card's height.
  "home-security": 260,
};

/** A card's guessed height; a card with no guess counts as this. */
export const DEFAULT_CARD_HEIGHT = 300;
export const guessHeight = (id: CardId): number => CARD_HEIGHT_GUESS[id] ?? DEFAULT_CARD_HEIGHT;

/** The Needs you tile at the head of column 1 (2026-11-07), sources folded: 360 to 466 px live. 360 packed best at 2, 3 and 4 columns in the live check. */
export const NEEDS_YOU_HEIGHT_GUESS = 360;

/** Put *id* into the shortest of *columns* (ties go left), by *heights* so far. */
function place(columns: Layout, heights: number[], id: CardId, heightOf: (id: CardId) => number): void {
  let best = 0;
  for (let i = 1; i < columns.length; i += 1) if (heights[i] < heights[best]) best = i;
  columns[best].push(id);
  heights[best] += heightOf(id);
}

/** *ids* in order, each into the shortest column so far; column 1 starts with the Needs you tile. */
export function packLayout(
  n: Packed,
  ids: readonly CardId[],
  heightOf: (id: CardId) => number = guessHeight,
): Layout {
  const columns: Layout = Array.from({ length: n }, () => []);
  const heights = new Array<number>(n).fill(0);
  heights[0] = NEEDS_YOU_HEIGHT_GUESS;
  for (const id of ids) place(columns, heights, id, heightOf);
  return columns;
}

/**
 * The columns of the packed board for *n*: the saved arrangement mended against *known* (the cards
 * on the board, in default order), else packed by guess. Always *n* columns.
 */
export function packedArrangement(
  n: Packed,
  known: readonly CardId[],
  heightOf: (id: CardId) => number = guessHeight,
): Layout {
  const mine = saved[String(n) as Key];
  if (!mine || mine.length !== n) return packLayout(n, known, heightOf);
  const seen = new Set<string>();
  const columns = mine.map((col) =>
    col.filter((id) => {
      if (!known.includes(id as CardId) || seen.has(id)) return false;
      seen.add(id);
      return true;
    }),
  ) as Layout;
  if (seen.size === 0) return packLayout(n, known, heightOf);
  const heights = columns.map((col, at) =>
    col.reduce((sum, id) => sum + heightOf(id), at === 0 ? NEEDS_YOU_HEIGHT_GUESS : 0),
  );
  for (const id of known) if (!seen.has(id)) place(columns, heights, id, heightOf);
  return columns;
}

/** Remember *layout* as the board's columns for *n* columns, the other counts untouched, and tell the board. */
export function setArrangement(n: Packed, layout: Layout): void {
  const key = String(n) as Key;
  touched.add(key);
  saved = { ...saved, [key]: layout.map((col) => [...col]) };
  write();
}

/** Forget every saved arrangement: the packed default again. */
export function resetArrangement(): void {
  for (const key of KEYS) touched.add(key);
  saved = {};
  write();
}

/** Whether an arrangement is saved, for *n* columns when given, for any width otherwise. */
export function hasArrangement(n?: Packed): boolean {
  return n === undefined ? Object.keys(saved).length > 0 : String(n) in saved;
}

/** Call *listener* after every change to the saved arrangement; the returned function stops it. */
export function onLayoutChange(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

/** For tests: forget what the module remembers. */
export function resetBoardLayoutState(): void {
  saved = {};
  touched.clear();
  persist = null;
  listeners.clear();
}

/** *layout* with card *id* moved one step, or null when it cannot move that way. */
export function moveCard(layout: Layout, id: CardId, direction: Direction): Layout | null {
  const from = layout.findIndex((col) => col.includes(id));
  if (from < 0) return null;
  const at = layout[from].indexOf(id);
  const next = layout.map((col) => [...col]);
  next[from].splice(at, 1);
  let column = from;
  let index = at;
  if (direction === "up") {
    if (at === 0) return null;
    index = at - 1;
  } else if (direction === "down") {
    if (at >= layout[from].length - 1) return null;
    index = at + 1;
  } else {
    column = from + (direction === "left" ? -1 : 1);
    if (column < 0 || column >= layout.length) return null;
    index = Math.min(at, next[column].length);
  }
  next[column].splice(index, 0, id);
  return next;
}

export interface ColumnBox {
  left: number;
  right: number;
  /** The cards in the column that are not being dragged, top to bottom. */
  cards: Array<{ top: number; bottom: number }>;
}

/** Which column and place a card dropped at (*x*, *y*) takes: the column under *x*, else the nearest. */
export function dropTarget(
  columns: ColumnBox[],
  x: number,
  y: number,
): { column: number; index: number } {
  let column = columns.findIndex((col) => x >= col.left && x < col.right);
  if (column < 0) {
    let best = Number.POSITIVE_INFINITY;
    columns.forEach((col, i) => {
      const distance = Math.abs((col.left + col.right) / 2 - x);
      if (distance < best) {
        best = distance;
        column = i;
      }
    });
  }
  if (column < 0) return { column: 0, index: 0 };
  const index = columns[column].cards.filter((card) => (card.top + card.bottom) / 2 < y).length;
  return { column, index };
}
