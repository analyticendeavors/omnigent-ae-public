// Which sessions a merged Open PRs card offers "Set to review" (2026-09-25).
// Review means a session is done and frees one of the owner's three slots, so the
// offer belongs after the merge of a session's last open pull request, not
// after the first of several: on 2026-09-25 a session that had opened four
// pull requests was offered review after the first merge while three were
// still open. The `/v1/ae/prs` payload lists every open tracked pull request
// with the sessions that track it, so a session is offered review only when
// no other card the page last showed still lists it; otherwise the merged card
// says how many are left. The session's own card (patch P20,
// web/src/lib/aeReviewOffer.ts in the fork) follows the same rule.

import type { OpenPrs, PrCard, PrSessionRef } from "./api";

/** The open cards the Open PRs section last rendered. */
let lastOpen: readonly PrCard[] = [];

/** Keep the open list of each render; a failed refresh keeps the last one. */
export function noteOpenPrs(data: OpenPrs | null): void {
  if (data?.configured) lastOpen = data.items;
}

/** For tests: forget the open list. */
export function resetReviewOffer(): void {
  lastOpen = [];
}

export interface ReviewOffer {
  /** Sessions to offer review: not in review or reference, no other open pull request. */
  offer: PrSessionRef[];
  /** Sessions held back, with how many of their other pull requests are open. */
  waiting: { ref: PrSessionRef; open: number }[];
}

/**
 * Split the merged card's sessions into those to offer review and those still
 * waiting on other open pull requests. `mergedUrl` is left out of the count
 * (the list may still show it until the refresh after the merge).
 */
export function splitReviewOffer(
  refs: readonly PrSessionRef[],
  mergedUrl: string,
  open: readonly PrCard[] = lastOpen,
): ReviewOffer {
  const self = mergedUrl.toLowerCase();
  const result: ReviewOffer = { offer: [], waiting: [] };
  for (const ref of refs) {
    if (ref.status === "review" || ref.status === "reference") continue;
    const count = open.filter(
      (card) =>
        card.url.toLowerCase() !== self &&
        card.state === "open" &&
        (card.sessions ?? []).some((other) => other.id === ref.id),
    ).length;
    if (count === 0) result.offer.push(ref);
    else result.waiting.push({ ref, open: count });
  }
  return result;
}

/** "3 more open pull requests in this session.", or with the title when the card has several. */
export function moreOpenText(wait: { ref: PrSessionRef; open: number }, several: boolean): string {
  const noun = `more open pull request${wait.open === 1 ? "" : "s"}`;
  return several
    ? `"${wait.ref.title}" has ${wait.open} ${noun}.`
    : `${wait.open} ${noun} in this session.`;
}
