// Scroll to an Admin Center card or group and focus its header (2026-11-06), opening it first on a phone;
// the packed board (2026-11-07) rings the group's cards instead of landing on a heading.
//
// Why a module of its own: the Key Vault link on the Secrets card (secrets.ts) and the status
// pills (tiles.ts) both go to a card, and board.ts imports tiles.ts, so neither could import the
// other's file for this without a cycle. It reads the DOM only.

/** Go to the card *id* (board.ts CardId); false when this page has no such card. */
export function goToCard(id: string): boolean {
  const card = document.querySelector<HTMLElement>(`.admin-card[data-card="${id}"]`);
  if (!card) return false;
  // board.ts folds cards on a phone; the header toggle opens it.
  if (card.classList.contains("is-folded")) {
    card.querySelector<HTMLButtonElement>(".admin-card-toggle")?.click();
  }
  card.scrollIntoView?.({ block: "start", behavior: "smooth" });
  card.querySelector<HTMLElement>(".admin-card-toggle")?.focus({ preventScroll: true });
  return true;
}

/** How long a group's cards stay ringed after a status pill sends you to them. */
export const FLASH_MS = 1600;

/** The pending un-flash per card, so a repeat click restarts the ring instead of ending it early. */
const flashTimers = new WeakMap<HTMLElement, number>();

/**
 * Go to the group *id* (tiles.ts DomainId). On a phone the board has group sections: scroll the
 * heading into view, open its first card if folded and focus that card's header. On the packed
 * board (2026-11-07) there are no sections: scroll the group's first card into view, focus its
 * header, and ring every card of the group for a moment (`is-flash`). False when the page has
 * no such group or card (the box-only view has none, and a group with every card not set up is
 * not drawn).
 */
export function goToGroup(id: string): boolean {
  const group = document.querySelector<HTMLElement>(`.admin-group[data-group="${id}"]`);
  if (group) {
    group.scrollIntoView?.({ block: "start", behavior: "smooth" });
    const card = group.querySelector<HTMLElement>(".admin-card");
    if (card?.classList.contains("is-folded")) {
      card.querySelector<HTMLButtonElement>(".admin-card-toggle")?.click();
    }
    card?.querySelector<HTMLElement>(".admin-card-toggle")?.focus({ preventScroll: true });
    return true;
  }
  const cards = [
    ...document.querySelectorAll<HTMLElement>(`.admin-board[data-packed] .admin-card[data-group="${id}"]`),
  ];
  if (!cards.length) return false;
  cards[0].scrollIntoView?.({ block: "start", behavior: "smooth" });
  cards[0].querySelector<HTMLElement>(".admin-card-toggle")?.focus({ preventScroll: true });
  for (const card of cards) {
    // A second click restarts the flash; the first click's timer would cut it short.
    window.clearTimeout(flashTimers.get(card));
    card.classList.add("is-flash");
    flashTimers.set(
      card,
      window.setTimeout(() => {
        card.classList.remove("is-flash");
        flashTimers.delete(card);
      }, FLASH_MS),
    );
  }
  return true;
}
