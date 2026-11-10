// What Needs you shows once this viewer's hidden cards are taken out (2026-11-07, PR 496 review).
//
// The server's totals count every source it reads. A card the viewer hid with "Hide for me" leaves
// the list, so the header count, the verdict sentence and the empty test must leave it too, or
// hiding Git while it holds the only two urgent items would read "2 urgent" over a blank list
// instead of "Nothing needs you." One helper, in a plain module (not a component file), so
// attention.ts and verdict.ts agree.

import { heldByWorkingChat, type AttentionItem, type AttentionOverview } from "./api";
import { sourceHidden } from "./cardState";

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
  const anyHidden = [...data.items, ...data.dismissed].some((item) => sourceHidden(item.source));
  if (!anyHidden) {
    return {
      items: data.items,
      dismissed: data.dismissed,
      urgent: data.counts.urgent,
      review: data.counts.review,
      working: data.items.filter(heldByWorkingChat).length,
      empty: data.items.length === 0,
    };
  }
  const items = data.items.filter((item) => !sourceHidden(item.source));
  const counted = items.filter((item) => !heldByWorkingChat(item));
  const urgent = counted.filter((item) => item.tone === "urgent").length;
  return {
    items,
    dismissed: data.dismissed.filter((item) => !sourceHidden(item.source)),
    urgent,
    review: counted.length - urgent,
    working: items.length - counted.length,
    empty: items.length === 0,
  };
}
