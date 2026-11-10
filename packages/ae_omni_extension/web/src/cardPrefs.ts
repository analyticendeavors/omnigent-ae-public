// "Hide for me" (2026-11-07, card setup): the cards this viewer put away, remembered in the
// SDK's `storage.user` under key `cards.hidden` (an array of card ids), in memory when storage
// is unavailable. Nothing changes on the server: the card, its Needs you lines and its pill
// share leave this viewer's page only. Same pattern as sections.ts.

import type { ExtensionContext } from "@omnigent/extension-sdk";
import { getHidden, setHidden } from "./cardState";

export const HIDDEN_KEY = "cards.hidden";

/** Read the remembered list; the page works the same without it. */
export async function loadCardPrefs(context: ExtensionContext): Promise<void> {
  try {
    const saved = await context.storage.user.get<unknown>(HIDDEN_KEY);
    if (Array.isArray(saved)) setHidden(saved.filter((id): id is string => typeof id === "string"));
  } catch {
    // Storage unavailable: nothing is hidden until the viewer hides something this visit.
  }
}

/** Hide or show one card for this viewer, and remember it. */
export function setCardHidden(context: ExtensionContext, id: string, hide: boolean): void {
  const next = new Set(getHidden());
  if (hide) next.add(id);
  else next.delete(id);
  setHidden(next);
  try {
    context.storage.user.set(HIDDEN_KEY, [...next]).catch(() => {
      // Refused: remembered in memory until the page closes.
    });
  } catch {
    // The same, refused before a promise was made.
  }
}
