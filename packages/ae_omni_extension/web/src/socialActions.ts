// The Social card's actions, shared (2026-11-16). The card (social.ts) and an opened Social row in
// Needs you (needsPost.ts) call the same functions, so Copy, Open on LinkedIn, Posted and Skip
// behave the same in both places and a fix is made once.
//
// Everything that touches the clipboard or the host starts *inside the tap*: a function here runs
// up to its first `await` at once, so a caller invokes it from the click handler before awaiting
// anything (iOS drops a clipboard write or a link that starts after a round trip).

import type { ExtensionContext } from "@omnigent/extension-sdk";
import { describeError, markSocialPosted, skipSocial, type SocialSlot, type SocialView } from "./api";
import { copyText } from "./lot";
import { forgetPost, rememberPost } from "./socialPosted";
import { shareAvailable, shareToLinkedin, usesShareSheet } from "./socialShare";
import { visualState } from "./socialVisual";
import { openUrl } from "./socialWords";

/** Copy the draft; the words the button then shows. */
export async function copyDraft(text: string): Promise<"Copied" | "Copy failed"> {
  return (await copyText(text)) ? "Copied" : "Copy failed";
}

/** Mark the post as posted (the card's own call). */
export const postPosted = (context: ExtensionContext, slotId: string): Promise<{ ok: boolean }> => markSocialPosted(context, slotId);

/** Skip the post: it leaves the plan (the card's own call). */
export const postSkipped = (context: ExtensionContext, slotId: string): Promise<{ ok: boolean }> => skipSocial(context, slotId);

/**
 * Whether Open must go to the editor instead: there is no draft loaded to copy, or the picture the
 * share sheet would carry is still loading (sharing the text alone by accident is worse).
 */
export function openNeedsEditor(slot: SocialSlot, text: string | null): boolean {
  if (text === null) return true;
  return usesShareSheet(slot) && shareAvailable() !== false && visualState(slot) === "loading";
}

export interface OpenResult {
  /** What the button says afterwards, when it changes. */
  label?: string;
  /** A line to leave under the header. */
  note?: { text: string; tone: "error" | "info" };
}

/**
 * Open: copy the draft, then the share sheet where the card would use it, otherwise the site.
 * Call it from the tap, after `openNeedsEditor` said no; it starts the copy before any await.
 */
export async function openPost(
  context: ExtensionContext,
  slot: SocialSlot,
  view: SocialView | null,
  text: string,
  /** A copy already started inside the tap (the row's fresh-text clipboard write); the site path waits for it instead of copying again. */
  started?: Promise<boolean> | null,
): Promise<OpenResult> {
  const url = openUrl(slot, view, text);
  if (usesShareSheet(slot) && shareAvailable() !== false) {
    const outcome = await shareToLinkedin({ context, slot, text });
    if (outcome.kind === "shared") {
      return {
        label: "Shared",
        note: outcome.filesDropped ? { text: "Shared the text. This phone's share sheet could not take the image.", tone: "info" } : undefined,
      };
    }
    if (outcome.kind === "unavailable") {
      return { note: { text: `${outcome.copied ? "Draft copied" : "Could not copy"}. This browser has no share sheet; paste it at linkedin.com/feed.`, tone: "info" } };
    }
    if (outcome.kind === "failed") {
      return {
        note: {
          text: `${outcome.copied ? "Draft copied" : "Could not copy"}. Could not open the share sheet from here (${describeError(outcome.error)}); paste it at linkedin.com/feed.`,
          tone: "info",
        },
      };
    }
    return {};
  }
  const copying = started ?? copyText(text); // inside the tap, before any await
  if (!url) {
    const copied = await copying;
    return { note: { text: `${copied ? "Draft copied. " : ""}No channel link for this post.`, tone: "info" } };
  }
  // Armed before the host call: on iOS the app switch happens while openExternal is still pending.
  const armed = Date.now();
  rememberPost(slot.id, slot.platform, armed);
  try {
    await context.navigation.openExternal(url);
  } catch (error) {
    forgetPost(slot.id, armed);
    // The host opens only links on its list; the draft is on the clipboard either way.
    const copied = await copying;
    const where = slot.platform === "linkedin" ? "linkedin.com/feed" : "the channel's Posts tab";
    return {
      note: {
        text: `${copied ? "Draft copied" : "Could not copy"}. Could not open the site from here (${describeError(error)}); paste it at ${where}.`,
        tone: "info",
      },
    };
  }
  return { label: (await copying) ? "Copied, opened" : "Opened" };
}

/** Whether the clipboard takes a ClipboardItem whose text is still a promise (so a fresh read can finish after the tap). */
export function canCopyFresh(): boolean {
  return typeof ClipboardItem !== "undefined" && typeof navigator !== "undefined" && typeof navigator.clipboard?.write === "function";
}

/**
 * Start the clipboard write now, inside the tap, with the text still being read: `clipboard.write` takes the item at
 * once and waits for the promise, so the user gesture is kept. Resolves true when the text went to the clipboard, false
 * when the read failed or the browser refused. Null where a ClipboardItem with a promise is not supported (the caller
 * then copies the text it holds, inside the tap).
 */
export function startFreshCopy(text: Promise<string>): Promise<boolean> | null {
  if (!canCopyFresh()) return null;
  try {
    const blob = text.then((value) => new Blob([value], { type: "text/plain" }));
    return navigator.clipboard.write([new ClipboardItem({ "text/plain": blob })]).then(
      () => true,
      () => false,
    );
  } catch {
    return Promise.resolve(false);
  }
}
