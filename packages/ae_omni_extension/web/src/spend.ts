// What the Spend card's two panels share (2026-10-30): the 7 d / 30 d window, the count of
// flagged families, and the footnotes.
//
// Why: the Presets card and the Family cost card became one card, "Spend", with a Presets tab and
// a Families tab (presets.ts, familyCost.ts). One window toggle drives both panels, so the day
// count lives here, not in either panel's file. The Families tab shows a warning pill when a
// family is over its limit, and every footnote of both panels sits in one info tooltip in the
// card header, so each panel reports its count and its notes here and presets.ts draws them.

import { resetFolds } from "./fold";

export const DAYS_KEY = "presets.days";
export const DEFAULT_DAYS = 30;
export const WINDOWS = [7, 30] as const;

export type PanelId = "presets" | "families";
type Topic = "days" | "flagged" | "notes";

/** The remembered window; the default when storage is missing or holds something else. */
export function loadDays(): number {
  try {
    const raw = window.localStorage.getItem(DAYS_KEY);
    const days = Number(raw);
    if ((WINDOWS as readonly number[]).includes(days)) return days;
  } catch {
    // No storage in this frame: the default.
  }
  return DEFAULT_DAYS;
}

export function saveDays(days: number): void {
  try {
    window.localStorage.setItem(DAYS_KEY, String(days));
  } catch {
    // Kept in memory until the page closes.
  }
}

let days = DEFAULT_DAYS;
let flagged = 0;
const notes: Record<PanelId, string[]> = { presets: [], families: [] };
const listeners: Record<Topic, Set<() => void>> = {
  days: new Set(),
  flagged: new Set(),
  notes: new Set(),
};

const tell = (topic: Topic) => {
  for (const listener of [...listeners[topic]]) listener();
};

/** Subscribe to *topic*; the returned function stops it. */
export function onSpend(topic: Topic, listener: () => void): () => void {
  listeners[topic].add(listener);
  return () => listeners[topic].delete(listener);
}

/** Read the remembered window; each panel's activate calls this first, in either order. */
export function syncDays(): number {
  days = loadDays();
  return days;
}

export const spendDays = (): number => days;

/** Choose the window for both panels and remember it. */
export function setSpendDays(next: number): void {
  if (next === days) return;
  days = next;
  saveDays(next);
  tell("days");
}

export const flaggedCount = (): number => flagged;

export function setFlaggedCount(count: number): void {
  if (count === flagged) return;
  flagged = count;
  tell("flagged");
}

/** Replace one panel's footnotes. */
export function setNotes(panel: PanelId, lines: readonly string[]): void {
  if (notes[panel].join("\n") === lines.join("\n")) return;
  notes[panel] = [...lines];
  tell("notes");
}

/** Every footnote of both panels, one per line, each said once. */
export function notesText(): string {
  return [...new Set([...notes.presets, ...notes.families])].join("\n");
}

/** Forget the shared state (a deactivated page and the tests). */
export function resetSpend(): void {
  days = DEFAULT_DAYS;
  flagged = 0;
  notes.presets = [];
  notes.families = [];
  resetFolds();
  for (const topic of Object.keys(listeners) as Topic[]) listeners[topic].clear();
}
