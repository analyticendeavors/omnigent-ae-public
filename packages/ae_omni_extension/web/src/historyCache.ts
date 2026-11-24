// The opened row's loaded histories (2026-11-16). A plain module so attention.ts, needsMore.ts and
// needsBulk.ts can all drop an item's entry after something is done to it, without importing each
// other. A cached history is good for HISTORY_TTL_MS; any action on the item forgets it at once, so
// reopening the row shows the action just taken.

import type { AttentionHistory } from "./api";

export const HISTORY_TTL_MS = 30_000;
export const histories = new Map<string, { at: number; value: AttentionHistory | { error: unknown } }>();

/** Drop what is cached for *ids* (after a snooze, dismiss, wake or restore of them). */
export function forgetHistory(...ids: string[]): void {
  for (const id of ids) histories.delete(id);
}

/** Forget every loaded history (the tests start clean). */
export const resetHistories = (): void => histories.clear();
