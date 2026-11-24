// Scroll to an Admin Center card and focus its header (2026-11-06), opening it first on a phone.
//
// 2026-11-14 (concept D2): the cards live on tabs now, and a card on a tab that is not showing is
// in the page but hidden. The card raises `admin-reveal-card` first; adminShell.ts switches to the
// tab that holds it, so the scroll and the focus land on something visible. The group jump the
// status pills used (goToGroup) left with the pills.
//
// Why a module of its own: the Key Vault link on the Secrets card (secrets.ts) goes to a card, and
// secrets.ts cannot import the shell's file for this without a cycle. It reads the DOM only.

/** The event a card raises (bubbles) before it is scrolled to; the shell shows its tab. */
export const REVEAL_CARD_EVENT = "admin-reveal-card";

/** Go to the card *id* (board.ts CardId); false when this page has no such card. */
export function goToCard(id: string): boolean {
  const card = document.querySelector<HTMLElement>(`.admin-card[data-card="${id}"]`);
  if (!card) return false;
  card.dispatchEvent(new CustomEvent(REVEAL_CARD_EVENT, { bubbles: true, detail: id }));
  // board.ts folds cards on a phone; the header toggle opens it.
  if (card.classList.contains("is-folded")) {
    card.querySelector<HTMLButtonElement>(".admin-card-toggle")?.click();
  }
  card.scrollIntoView?.({ block: "start", behavior: "smooth" });
  card.querySelector<HTMLElement>(".admin-card-toggle")?.focus({ preventScroll: true });
  return true;
}
