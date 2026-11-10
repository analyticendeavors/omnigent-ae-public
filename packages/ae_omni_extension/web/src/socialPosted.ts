// "Did you post it?" (2026-11-06, phone redesign). Post on LinkedIn and Post on YouTube open the
// site in another tab or app; nothing can see whether the post was made. So the page remembers
// the tap, and when it becomes visible again within 30 minutes it asks once, with one tap to
// say yes. The memory lives in the page only: a reload forgets it, which is fine for a 30-minute
// window.
//
// Shown once per tap, never for a post that is no longer planned, and on one surface at a time:
// the full view when it is open, the compact card otherwise (both read `activeBanner`).

import type { ExtensionContext } from "@omnigent/extension-sdk";
import { describeError, markSocialPosted, type SocialSlot, type SocialView } from "./api";
import { button, el, notice, replaceChildren } from "./dom";
import { siteName } from "./socialWords";

/** A return later than this after the tap asks nothing. */
export const WINDOW_MS = 30 * 60_000;
/** A return sooner than this is the tap's own focus echo, not the user coming back. */
export const DWELL_MS = 2_000;

interface Pending {
  slotId: string;
  platform: SocialSlot["platform"];
  at: number;
  shown: boolean;
}

let pending: Pending[] = [];
let active: Pending | null = null;
const listeners = new Set<() => void>();

const notify = (): void => {
  for (const listener of [...listeners]) listener();
};

/** Tell whoever shows the banner to look again (the full view closing hands it to the card). */
export const announceBanner = notify;

/** Redraw callback for whoever shows the banner; returns the stop function. */
export function onBannerChange(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

/** Forget everything (tests start clean). */
export function resetPosted(): void {
  pending = [];
  active = null;
  listeners.clear();
}

/** Remember a successful Post tap. */
export function rememberPost(slotId: string, platform: SocialSlot["platform"], now: number): void {
  pending.push({ slotId, platform, at: now, shown: false });
}

/**
 * The share sheet finished (2026-11-07): ask "Did you post it?" at once for the tap made at
 * *armed*. Unlike a link opened in another tab, a share sheet that resolves has already handed the
 * text to the app, and the page may never see a visibility change to ask on.
 */
export function showNow(slotId: string, armed: number): void {
  const tap = pending.find((p) => p.slotId === slotId && p.at === armed);
  if (!tap) return;
  tap.shown = true;
  active = tap;
  notify();
}

/** Forget a tap (the host refused the link, so nothing was opened); drops its banner too. */
export function forgetPost(slotId: string, at: number): void {
  pending = pending.filter((p) => !(p.slotId === slotId && p.at === at));
  if (active && active.slotId === slotId && active.at === at) {
    active = null;
    notify();
  }
}

/**
 * The page is visible (or focused) again at *now*: raise the banner for the newest tap that has
 * not asked yet and is inside the window. Returns whether a banner was raised.
 */
export function noteReturn(now: number): boolean {
  pending = pending.filter((p) => now - p.at <= WINDOW_MS);
  const ready = pending.filter((p) => !p.shown && now - p.at >= DWELL_MS).sort((a, b) => b.at - a.at)[0];
  if (!ready) return false;
  ready.shown = true;
  active = ready;
  notify();
  return true;
}

/** The banner to show for *view*, or null; a post that is no longer planned drops it. */
export function activeBanner(view: SocialView | null): { slotId: string; platform: SocialSlot["platform"] } | null {
  if (!active || !view) return null;
  const slot = view.slots.find((s) => s.id === active?.slotId);
  if (!slot || slot.state !== "planned") {
    active = null;
    return null;
  }
  return active;
}

export function dismissBanner(): void {
  if (!active) return;
  active = null;
  notify();
}

export interface BannerOptions {
  context: ExtensionContext;
  slotId: string;
  platform: SocialSlot["platform"];
  /** The post's title, so the line says which post it asks about. */
  title: string;
  /** Read the card again after the post was marked. */
  refresh: () => Promise<void>;
  /** Runs after the post was marked and the card was read again. */
  posted?: () => void;
}

/** The banner: the question, "Yes, mark posted" and "Not yet". */
export function bannerNode(options: BannerOptions): HTMLElement {
  const status = el("div", { class: "sv-banner-status", "aria-live": "polite" });
  const yes = button("Yes, mark posted", {
    kind: "primary",
    title: "Mark this post as posted, without a link",
    onClick: async () => {
      replaceChildren(status, []);
      try {
        await markSocialPosted(options.context, options.slotId);
      } catch (error) {
        replaceChildren(status, [notice(`Could not mark it posted (${describeError(error)}). Nothing changed; try again.`, "error")]);
        return;
      }
      dismissBanner();
      await options.refresh();
      options.posted?.();
    },
  });
  const no = button("Not yet", { title: "Ask no more about this tap", onClick: () => dismissBanner() });
  return el(
    "div",
    { class: "sv-banner", role: "group", "aria-label": "Did you post it?" },
    el("p", { class: "sv-banner-q" }, el("strong", {}, `Back from ${siteName(options.platform)}.`), " Did you post it?"),
    el("p", { class: "meta sv-banner-title" }, options.title),
    el("div", { class: "sv-banner-row" }, yes, no),
    status,
  );
}
