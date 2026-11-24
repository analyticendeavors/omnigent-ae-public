// What Needs you shows, counted one way for the table, the tab counts and the verdict.
//
// The server's totals count every source it reads. Until 2026-11-14 a card the viewer hid with
// "Hide for me" also left this list, so the counts left it too. Reid's D2 pick changed that:
// hiding a card hides only the card, and its items stay in Needs you and in the verdict, so a
// hidden card can never keep an urgent item out of sight. One helper, in a plain module (not a
// component file), so attention.ts, needsTable.ts and verdict.ts agree.

import { heldByWorkingChat, type AttentionItem, type AttentionOverview } from "./api";

export interface AttentionView {
  items: AttentionItem[];
  dismissed: AttentionItem[];
  /** Open items not held by a working chat, as the server counts them. */
  urgent: number;
  review: number;
  /** Listed items a chat is still working on (not counted above). */
  working: number;
  empty: boolean;
}

export function attentionView(data: AttentionOverview): AttentionView {
  return {
    items: data.items,
    dismissed: data.dismissed,
    urgent: data.counts.urgent,
    review: data.counts.review,
    working: data.items.filter(heldByWorkingChat).length,
    empty: data.items.length === 0,
  };
}
