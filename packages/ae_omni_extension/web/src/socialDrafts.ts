// Full drafts the compact card has on hand (2026-11-06, review round 1). A tap copies from here
// *inside the tap*: iOS drops a clipboard write that starts after an async round trip.
//
// The list carries a 160-character preview only, so an entry is keyed on the row's `draft_rev`
// (a fingerprint of the whole text), not on the preview: an edit past character 160 leaves the
// preview alone but changes the revision. The editor drops the entry when it saves.

import type { SocialSlot } from "./api";
import { PREVIEW_CHARS } from "./socialWords";

const drafts = new Map<string, { text: string; rev: string | null }>();

/** The draft text already on hand, or null when the tap would have to fetch it first. */
export function loadedDraft(slot: SocialSlot): string | null {
  const hit = drafts.get(slot.id);
  if (hit && hit.rev !== null && hit.rev === (slot.draft_rev ?? null)) return hit.text;
  // A preview shorter than the cut is the whole text.
  if (slot.draft_preview !== null && slot.draft_preview.length < PREVIEW_CHARS) return slot.draft_preview;
  return null;
}

/** Keep the full text fetched for a post, under the revision it came with. */
export function rememberDraft(id: string, text: string, rev: string | null | undefined): void {
  drafts.set(id, { text, rev: rev ?? null });
}

/** Drop a post's entry (the editor saved it). */
export const forgetFullDraft = (id: string): void => void drafts.delete(id);

/** Forget everything (tests start clean; the card activating starts clean). */
export const resetDrafts = (): void => drafts.clear();
