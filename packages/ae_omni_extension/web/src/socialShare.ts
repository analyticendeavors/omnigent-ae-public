// Share to LinkedIn (2026-11-07). On a phone the Home Screen app opens a link in Safari's in-app
// view, and iOS never hands such a link to the LinkedIn app, so the composer link cannot start a
// post there. The share sheet can: it lists LinkedIn, and LinkedIn takes the text and the image.
// The sandboxed frame cannot call navigator.share, so the host does (patch 0300,
// `navigation.share`), from the same tap.
//
// The tap runs, in this order and before any await: copy (so the text is on the clipboard if
// the sheet drops it), remember the tap, call the host. A cancelled sheet forgets the tap and says
// nothing; a finished one asks "Did you post it?" at once.
//
// Whether the browser has a share sheet is asked once, when the full view opens (`probeShare`),
// and cached. The tap chooses the sheet or the composer link from that answer, synchronously: a
// link opened after a failed share runs outside the tap, where iOS can refuse it. A share that
// still fails with ShareUnavailable says to paste, and does not try the link late.
//
// A share the host stopped waiting for (Timeout) is an unknown outcome: the post stays remembered
// and "Did you post it?" asks, with no error note.

import type { ExtensionContext } from "@omnigent/extension-sdk";
import type { SocialSlot } from "./api";
import { copyText } from "./lot";
import { forgetPost, rememberPost, showNow } from "./socialPosted";
import { loadedVisual, visualFile } from "./socialVisual";
import { isTouch } from "./socialWords";

let available: boolean | null = null;
let probing: Promise<boolean> | null = null;

/** Forget the cached probe (tests start clean). */
export function resetShareProbe(): void {
  available = null;
  probing = null;
}

/** The cached answer: whether the browser has a share sheet, or null before it was asked. */
export const shareAvailable = (): boolean | null => available;

/** Ask the host once whether a share sheet exists (nothing is shared); the answer is cached. */
export function probeShare(context: ExtensionContext): Promise<boolean> {
  if (available !== null) return Promise.resolve(available);
  if (probing) return probing;
  probing = Promise.resolve()
    .then(() => context.navigation.share({ probe: true }))
    .then(
      (result) => result?.available !== false,
      () => false, // a host with no share service rejects: there is no sheet
    )
    .then((value) => {
      available = value;
      probing = null;
      return value;
    });
  return probing;
}

/**
 * Whether this post is shared through the share sheet: a LinkedIn post on a touch device whose
 * browser has one (an unanswered probe counts as yes; the tap still falls back to a note).
 */
export const usesShareSheet = (slot: SocialSlot): boolean =>
  slot.platform === "linkedin" && isTouch() && available !== false;

export type ShareOutcome =
  | { kind: "shared"; copied: boolean; filesDropped: boolean }
  | { kind: "cancelled" }
  /** The host stopped waiting: the post may have gone out, so "Did you post it?" asks. */
  | { kind: "unknown" }
  /** No share sheet: the text is copied (`copied`); paste it in LinkedIn. */
  | { kind: "unavailable"; copied: boolean }
  | { kind: "failed"; copied: boolean; error: unknown };

const codeOf = (error: unknown): string => String((error as { code?: unknown } | null)?.code ?? "");

export interface ShareOptions {
  context: ExtensionContext;
  slot: SocialSlot;
  text: string;
}

export async function shareToLinkedin(options: ShareOptions): Promise<ShareOutcome> {
  const { context, slot, text } = options;
  // 1. Copy, synchronously inside the tap (lot.ts copyText runs its selection copy first).
  const copying = copyText(text);
  // 2. Armed before the host call: the app switch can happen while it is still pending.
  const armed = Date.now();
  rememberPost(slot.id, slot.platform, armed);
  // 3. The share sheet, with the picture if it has loaded (never waited for).
  const blob = loadedVisual(slot);
  const name = slot.title ?? slot.tool ?? slot.id;
  try {
    const result = await context.navigation.share({
      text,
      ...(blob ? { files: [visualFile(blob, name)] } : {}),
    });
    showNow(slot.id, armed);
    return { kind: "shared", copied: await copying, filesDropped: result?.files_dropped === true };
  } catch (error) {
    const code = codeOf(error);
    if (code === "Timeout" || code === "RequestTimeout") {
      showNow(slot.id, armed);
      return { kind: "unknown" };
    }
    forgetPost(slot.id, armed);
    if (code === "ShareCancelled") return { kind: "cancelled" };
    // "Unsupported": a host older than the share service does not list the method at all.
    if (code === "ShareUnavailable" || code === "Unsupported") {
      available = false;
      return { kind: "unavailable", copied: await copying };
    }
    return { kind: "failed", copied: await copying, error };
  }
}
