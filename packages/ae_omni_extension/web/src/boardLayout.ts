// Where each Admin Center card sits (2026-10-22): the default columns, the
// arrangement Reid saved by dragging, and the pure steps that move a card.
//
// Why: the cards sit in fixed column stacks (board.ts), and the default left
// holes under the short columns. Reid asked for the Box health card under
// Deploys and Databricks credit under Azure spend, and to be able to drag a
// card elsewhere. The arrangement is saved per column count (two and three;
// a phone is one column and is not arranged), in the SDK's `storage.user`
// like the card folds (sections.ts), so dragging on a wide page never changes
// the two-column view.
//
// A saved arrangement is read defensively: a card id the page no longer has is
// dropped, a card listed twice keeps its first place, and a card the saved
// arrangement lacks (one added after it was saved) goes to its default
// column. An arrangement that is the wrong shape or has no known card left is
// ignored, so a bad value can never leave the page without a card.

import type { ExtensionContext } from "@omnigent/extension-sdk";
import type { CardId } from "./board";

export const LAYOUT_KEY = "board.layout";

export type Columns = 1 | 2 | 3;
export type Layout = CardId[][];
export type Defaults = Record<Columns, Layout>;
export type Direction = "up" | "down" | "left" | "right";

type Saved = Partial<Record<"2" | "3", string[][]>>;

let saved: Saved = {};
/** Column counts changed on the page before the stored arrangement arrived; they win over it. */
const touched = new Set<"2" | "3">();
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
      const next: Saved = {};
      let changed = false;
      for (const key of ["2", "3"] as const) {
        // A drag or a reset made while the read was out is newer than what it read.
        if (touched.has(key)) {
          if (saved[key]) next[key] = saved[key];
          continue;
        }
        const columns = (found as Record<string, unknown>)[key];
        if (isColumns(columns)) {
          next[key] = columns;
          changed = true;
        }
      }
      saved = next;
      if (touched.size) persist?.();
      if (changed) notify();
    }
  } catch {
    // Same as a failed write: the default layout.
  }
}

/** The columns for *n*: the saved arrangement mended against *known*, else the default. */
export function arrangement(n: Columns, defaults: Defaults, known: readonly CardId[]): Layout {
  const base = defaults[n];
  if (n === 1) return base;
  const mine = saved[String(n) as "2" | "3"];
  if (!mine || mine.length !== n) return base;
  const seen = new Set<string>();
  const columns = mine.map((col) =>
    col.filter((id) => {
      if (!known.includes(id as CardId) || seen.has(id)) return false;
      seen.add(id);
      return true;
    }),
  ) as Layout;
  if (seen.size === 0) return base;
  for (const id of known) {
    if (seen.has(id)) continue;
    columns[
      Math.max(
        0,
        base.findIndex((col) => col.includes(id)),
      )
    ].push(id);
  }
  return columns;
}

/** Remember *layout* for *n* columns and tell the board. */
export function setArrangement(n: 2 | 3, layout: Layout): void {
  touched.add(String(n) as "2" | "3");
  saved = { ...saved, [String(n)]: layout.map((col) => [...col]) };
  write();
}

/** Forget every saved arrangement: the default layout again. */
export function resetArrangement(): void {
  touched.add("2");
  touched.add("3");
  saved = {};
  write();
}

export function hasArrangement(): boolean {
  return Object.keys(saved).length > 0;
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
