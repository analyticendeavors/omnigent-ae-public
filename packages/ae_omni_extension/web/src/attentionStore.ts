// What the Needs you panel last read (2026-11-03), shared with the cards that link to it.
//
// Why a store of its own: the Git repos card and the Box health card no longer list their own
// Needs you rows; each says "N in Needs you" and that line opens the panel filtered to the
// card's source. The panel (attention.ts) imports boxHealth.ts for its Clean up button, so the
// cards cannot import the panel without a cycle. They read this module instead, and the panel
// writes it.

import { heldByWorkingChat, type AttentionItem, type AttentionOverview, type AttentionSource } from "./api";
import { el } from "./dom";

/** The words for each source, as the panel's parent lines and the filter chip say them. */
export const SOURCE_NAMES: Record<AttentionSource, string> = {
  git: "Git repos",
  "git-mine": "My Git repos",
  box: "Box health",
  host: "Box host",
  cost: "Cost targets",
  databricks: "Databricks credit",
  secrets: "Secrets on box",
  vault: "Key Vault",
  accounts: "Accounts",
  family: "Spend families",
};

interface Store {
  data: AttentionOverview | null;
  /** The newest read failed; the last good data (if any) stays. */
  error: unknown;
  filter: AttentionSource | null;
}

let store: Store = { data: null, error: null, filter: null };
const listeners = new Set<() => void>();

function emit(): void {
  for (const listener of [...listeners]) listener();
}

export function getAttention(): Readonly<Store> {
  return store;
}

/** A read finished: *data* replaces the last answer; a failure keeps it and records *error*. */
export function setAttention(data: AttentionOverview | null, error: unknown = null): void {
  store = { ...store, data: data ?? store.data, error: data ? null : error };
  emit();
}

export function onAttention(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

// A source card finished a read (2026-11-03, review): the server's Needs you list is built from
// the Git and Box answers those cards just caused, so the list read alongside them may have run
// before they finished (a cold or stale Git cache). The cards call sourceRead(); the panel sets
// the handler that reads the list again. Debounced, so both cards finishing is one read.
const REREAD_MS = 250;
let rereader: (() => void) | null = null;
let rereadTimer: ReturnType<typeof setTimeout> | null = null;

export function setRereader(read: (() => void) | null): void {
  rereader = read;
  if (!read && rereadTimer !== null) {
    clearTimeout(rereadTimer);
    rereadTimer = null;
  }
}

export function sourceRead(): void {
  if (!rereader || rereadTimer !== null) return;
  rereadTimer = setTimeout(() => {
    rereadTimer = null;
    rereader?.();
  }, REREAD_MS);
}

export function getFilter(): AttentionSource | null {
  return store.filter;
}

export function setFilter(filter: AttentionSource | null): void {
  if (store.filter === filter) return;
  store = { ...store, filter };
  emit();
}

/** The listed items of *source* (open, not dismissed; a held item stays listed), or null before any read. */
export function sourceItems(source: AttentionSource): AttentionItem[] | null {
  return store.data ? store.data.items.filter((item) => item.source === source) : null;
}

/** How many items *source* has in the panel list, held ones included, or null before the first read. */
export function listedCount(source: AttentionSource): number | null {
  return sourceItems(source)?.length ?? null;
}

/**
 * How many items of *source* need the owner, or null before the first read. The one counting
 * rule for the whole page (2026-11-08): open in the panel, not dismissed, and not held by a chat
 * that is still working. A chat that finished or is blocked leaves the item counted.
 */
export function attentionCount(source: AttentionSource): number | null {
  const bucket = serverCounts(source);
  if (bucket) return bucket.urgent + bucket.review;
  const items = sourceItems(source);
  return items ? items.filter((item) => !heldByWorkingChat(item)).length : null;
}

/** How many of *source*'s items are urgent (not held by a working chat), or null before the first read. */
export function urgentCount(source: AttentionSource): number | null {
  const bucket = serverCounts(source);
  if (bucket) return bucket.urgent;
  const items = sourceItems(source);
  return items ? items.filter((item) => item.tone === "urgent" && !heldByWorkingChat(item)).length : null;
}

/**
 * The server's own count for *source*, same rule, taken over the rows: a long answer drops rows
 * from its tail to fit (`more`), and its counts stay full, so the cards match the header.
 */
function serverCounts(source: AttentionSource): { urgent: number; review: number } | null {
  const bySource = store.data?.counts?.by_source;
  if (!bySource) return null;
  return bySource[source] ?? { urgent: 0, review: 0 };
}

/** Filter the panel to *source* and scroll to it. */
export function showInNeedsYou(source: AttentionSource): void {
  setFilter(source);
  const panel = document.getElementById("needs-you");
  panel?.scrollIntoView?.({ block: "start", behavior: "smooth" });
}

const OPEN_HINT = (source: AttentionSource) =>
  `Open the Needs you panel, showing only ${SOURCE_NAMES[source]}`;

/**
 * The card's one line about the panel: "N in Needs you" (a link-styled button that opens the
 * panel filtered to *source*; N is the counting rule above), "Nothing needs you." at zero, and
 * a muted word before the first read or when it failed. Box health and Secrets use
 * `needsButton` in their header instead.
 */
export function needsLink(source: AttentionSource): HTMLElement {
  const n = attentionCount(source);
  if (n === null) {
    return el(
      "p",
      { class: "needs-link meta" },
      store.error ? "Needs you could not be read." : "Reading Needs you...",
    );
  }
  if (n === 0) return el("p", { class: "needs-link needs-link-none meta" }, "Nothing needs you.");
  const link = el(
    "button",
    { type: "button", class: "linkish needs-link-btn", title: OPEN_HINT(source) },
    `${n} in Needs you`,
  );
  link.addEventListener("click", () => showInNeedsYou(source));
  return el("p", { class: "needs-link" }, link);
}

/**
 * Just the button, for a card header (2026-11-08): "See in Needs you", or null before the first
 * read and when the panel lists nothing for *source*.
 */
export function needsButton(source: AttentionSource): HTMLElement | null {
  const listed = listedCount(source);
  if (!listed) return null;
  const link = el(
    "button",
    {
      type: "button",
      class: "linkish needs-link-btn",
      title: OPEN_HINT(source),
      "aria-label": `See ${SOURCE_NAMES[source]} in Needs you`,
    },
    "See in Needs you",
  );
  link.addEventListener("click", () => showInNeedsYou(source));
  return link;
}

/** `needsButton`, kept current like `liveNeedsLink`: a slot that is empty when there is nothing to show. */
export function liveNeedsButton(source: AttentionSource): HTMLElement {
  const slot = el("span", { class: "needs-button-slot" }, needsButton(source));
  let attached = false;
  let reads = 0;
  const stop = onAttention(() => {
    if (slot.isConnected) attached = true;
    if (!slot.isConnected && (attached || ++reads > UNATTACHED_READS)) {
      stop();
      return;
    }
    slot.replaceChildren(...[needsButton(source)].filter((node): node is HTMLElement => node !== null));
  });
  return slot;
}

/** A slot never put on the page stops listening after this many panel reads. */
const UNATTACHED_READS = 3;

/**
 * `needsLink`, kept current (2026-11-06). The seven cards added in phase 3 redraw only when their
 * own answer changes, so a plain `needsLink` would stay on "Reading Needs you..." after the panel
 * read. This wraps it in a slot that swaps the link on every panel read for as long as the slot
 * is on the page, and stops listening once the card has been replaced.
 */
export function liveNeedsLink(source: AttentionSource): HTMLElement {
  const slot = el("div", { class: "needs-link-slot" }, needsLink(source));
  let attached = false;
  let reads = 0;
  const stop = onAttention(() => {
    if (slot.isConnected) attached = true;
    // Gone from the page after being on it, or never put on it within a few reads: stop.
    if (!slot.isConnected && (attached || ++reads > UNATTACHED_READS)) {
      stop();
      return;
    }
    slot.replaceChildren(needsLink(source));
  });
  return slot;
}

/** Forget everything (deactivate, and the tests). */
export function resetAttention(): void {
  store = { data: null, error: null, filter: null };
  listeners.clear();
}
