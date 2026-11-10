// The Admin Center header's one-sentence verdict (2026-11-06): what Needs you holds, and whether
// anything else is wrong, so the answer reads before the pills or the list are looked at.
//
// It reads the two stores the page already fills (attentionStore.ts and tiles.ts) and adds no
// request. Until the Needs you list has been read it says nothing, never "Nothing needs you".

import { attentionView } from "./attentionView";
import { getAttention, onAttention } from "./attentionStore";
import { onCardState } from "./cardState";
import { el, replaceChildren } from "./dom";
import { allReported, onTiles, pillStates } from "./tiles";

/**
 * The sentence's parts: the Needs you counts, then the optional endings.
 *
 * "Everything else is fine." needs all of: every pill drawn is ok; every member tile of every
 * group has reported at least once (allReported, so a section that has not answered yet is never
 * read as healthy); no source the list could not read; the newest read did not fail; and a full
 * page (*fine* false in the box-only view, which holds one card).
 */
export function verdictParts(fine = true): { urgent: number; rest: string } | null {
  const { data, error } = getAttention();
  if (!data) return null;
  const { urgent, review } = attentionView(data);
  const stale = Boolean(error);
  const pills = pillStates();
  const allFine =
    fine && !stale && data.unknown.length === 0 && allReported() && pills.length > 0 && pills.every((pill) => pill.health === "ok");
  const tail =
    (allFine ? " Everything else is fine." : "") +
    (data.unknown.length ? " Some sources could not be read." : "") +
    (stale ? " Last answer." : "");
  if (!urgent && !review) {
    return { urgent: 0, rest: `${stale ? "0 to review" : "Nothing needs you"}.${tail}` };
  }
  const reviewText = review ? `${review} to review` : "";
  if (urgent) return { urgent, rest: `${review ? `, ${reviewText}` : ""}.${tail}` };
  return { urgent: 0, rest: `${reviewText}.${tail}` };
}

function draw(node: HTMLElement, fine: boolean): void {
  const parts = verdictParts(fine);
  if (!parts) {
    replaceChildren(node, []);
    return;
  }
  replaceChildren(node, [
    parts.urgent ? el("span", { class: "verdict-urgent" }, `${parts.urgent} urgent`) : null,
    parts.rest,
  ]);
}

/** Keep *node* showing the verdict; call after resetTiles and resetAttention (they drop listeners). */
export function mountVerdict(node: HTMLElement, options: { fine?: boolean } = {}): void {
  const fine = options.fine !== false;
  draw(node, fine);
  onAttention(() => draw(node, fine));
  onTiles(() => draw(node, fine));
  // Hiding a card changes what Needs you holds for this viewer.
  onCardState(() => draw(node, fine));
}
