// The Admin Center's Social card (2026-11-06): what to post on LinkedIn and YouTube
// Community next, how consistent the posting has been, and Copy, Open and Posted for the
// next post. Data comes from `GET /v1/ae/social` (ae_omni_policies/social.py, admins only),
// which the box relay keeps fed from the YouTube uploads.
//
// Why: promoting each video, Short and livestream, three tools and a weekly expert post
// depended on remembering. The card plans them, scores the habit and keeps the three
// clicks short. Nothing here posts: LinkedIn cannot be read or written from the server and
// YouTube has no Community-post API, so Copy puts the draft on the clipboard, Open goes to
// the site and Posted is Reid saying so. This file is the compact card; the full view, the
// editor and the settings are a later part.
//
// The card reads on activate and on focus (deduped, with a generation counter like git.ts)
// and after every action. Every string reaches the page through textContent.

import type { ExtensionContext } from "@omnigent/extension-sdk";
import {
  AeRequestError,
  describeError,
  fetchSocial,
  fetchSocialSlot,
  markSocialPosted,
  type SocialSlot,
  type SocialView,
} from "./api";
import { button, el, notice, replaceChildren } from "./dom";
import {
  alertTriangleIcon,
  arrowsMaximizeIcon,
  checkIcon,
  circleCheckIcon,
  copyIcon,
  externalLinkIcon,
  flameIcon,
  linkedinIcon,
  youtubeIcon,
} from "./icons";
import { copyText } from "./lot";
import {
  ago,
  CHANNEL_NAMES,
  dueLabel,
  dueTodayCount,
  headerPill,
  openUrl,
  overdueCount,
  slotTitle,
  upNext,
  warnings,
} from "./socialWords";
import { loadedDraft, rememberDraft, resetDrafts } from "./socialDrafts";
import { probeShare, shareAvailable, shareToLinkedin, usesShareSheet } from "./socialShare";
import { loadVisual, visualState } from "./socialVisual";
import { activeBanner, bannerNode, forgetPost, noteReturn, onBannerChange, rememberPost } from "./socialPosted";
import { closeSocialView, openSocialView, pushSocialView, socialViewOpen } from "./socialView";
import { type Health, registerRefresh, setTile, staleTile, type Tile } from "./tiles";

export { loadedDraft, resetDrafts } from "./socialDrafts";
export {
  dueLabel,
  dueTodayCount,
  headerPill,
  openUrl,
  overdueCount,
  slotTitle,
  upNext,
  warnings,
  UP_NEXT,
} from "./socialWords";

/** The newest reading across the channels, for the footer. */
function newestRead(view: SocialView): string | null {
  const reads = Object.values(view.uploads_meta).flatMap((meta) => (meta.read_at ? [meta.read_at] : []));
  return reads.length ? reads.reduce((a, b) => (Date.parse(a) >= Date.parse(b) ? a : b)) : null;
}

export function socialTile(view: SocialView | null, error: unknown, now = Date.now()): Tile {
  if (!view) {
    const forbidden = error instanceof AeRequestError && error.status === 403;
    return {
      id: "social",
      value: forbidden ? "Admins" : error ? "Unknown" : "Reading...",
      note: forbidden ? "admins only" : error ? "could not be read" : "",
      health: error && !forbidden ? "attention" : "idle",
    };
  }
  const late = overdueCount(view, now);
  const today = dueTodayCount(view, now);
  const pill = headerPill(view, now);
  const gapped = Object.values(view.gaps).some((gap) => gap.upload_gap || gap.read_failed || gap.stale);
  const behind = !view.score.first_week && !view.score.on_pace;
  const health: Health = late > 0 ? "problem" : today > 0 || behind || gapped ? "attention" : "ok";
  // The Ship pill's line: 3 to 5 words (2026-11-06).
  const brief =
    late > 0
      ? `${late} ${late === 1 ? "post" : "posts"} overdue`
      : today > 0
        ? `${today} due today`
        : view.score.first_week
          ? "First week"
          : behind
            ? "Behind this week"
            : `On pace, streak ${view.score.streak} wk`;
  return {
    id: "social",
    value: pill.text,
    summary: pill.text,
    brief,
    note: view.score.first_week
      ? `First week · ${view.score.week_posted} of ${view.score.week_target} this week`
      : `Streak ${view.score.streak} wk · ${view.score.week_posted} of ${view.score.week_target} this week`,
    health,
  };
}

// ── drawing ─────────────────────────────────────────────────────────────────

interface Parts {
  root: HTMLElement;
  popout: HTMLButtonElement;
  pill: HTMLElement;
  status: HTMLElement;
  scores: HTMLElement;
  warns: HTMLElement;
  next: HTMLElement;
  footer: HTMLElement;
}

interface State {
  view: SocialView | null;
  error: unknown;
  at: number;
}

function scoreBox(label: string, value: string, note: string, good: boolean, extra?: SVGElement): HTMLElement {
  return el(
    "div",
    { class: `social-stat ${good ? "stat-ok" : "stat-attention"}` },
    el("span", { class: "social-stat-l" }, label),
    el("span", { class: "social-stat-n" }, extra ?? "", value),
    el("span", { class: "social-stat-note" }, note),
  );
}

function drawScores(parts: Parts, view: SocialView): void {
  const s = view.score;
  replaceChildren(parts.scores, [
    scoreBox(
      "This week",
      `${s.week_posted} of ${s.week_target}`,
      s.first_week ? "First week" : s.on_pace ? "On pace" : "Behind",
      s.first_week || s.on_pace,
    ),
    scoreBox(
      "Promoted",
      `${s.promoted} of ${s.promoted_of}`,
      s.missed > 0 ? `${s.missed} missed` : "All done",
      s.missed === 0,
    ),
    scoreBox(
      "Streak",
      `${s.streak} wk`,
      s.first_week ? "Starts next week" : s.week_posted >= s.week_target ? "Target hit" : "Not yet",
      s.first_week || s.week_posted >= s.week_target,
      flameIcon(),
    ),
  ]);
}

const platformIcon = (slot: SocialSlot) => (slot.platform === "linkedin" ? linkedinIcon() : youtubeIcon());

function rowFor(slot: SocialSlot, view: SocialView, now: number, first: boolean, run: Runner): HTMLElement {
  if (first) preload(run.context, slot);
  const late = Date.parse(slot.due) < now;
  const meta = el(
    "span",
    { class: "social-meta" },
    el("span", { class: late ? "social-due is-late" : "social-due" }, dueLabel(slot.due, now, view.rules.tz)),
    el(
      "span",
      { class: `social-chip ${slot.has_draft ? "chip-ok" : "chip-warn"}` },
      slot.has_draft ? "Draft ready" : "No draft",
    ),
    slot.channel && slot.kind !== "tool" && slot.kind !== "expert"
      ? el("span", { class: "social-chip" }, CHANNEL_NAMES[slot.channel] ?? slot.channel)
      : "",
    slot.skippable ? el("span", { class: "social-chip" }, "optional") : "",
  );
  const row = el(
    "div",
    { class: `social-row${late ? " is-late" : ""}` },
    el("span", { class: "social-platform" }, platformIcon(slot)),
    el("div", { class: "social-row-text" }, el("span", { class: "social-title" }, slotTitle(slot)), meta),
  );
  if (first) row.appendChild(actions(slot, view, run));
  return row;
}

/** Runs a row action: *after* "refresh" reads the card again (Posted), "note" only redraws the
 *  note (Copy and Open keep their "Copied" label, which a full redraw would reset). */
interface Runner {
  context: ExtensionContext;
  go: (task: () => Promise<void>, after: "refresh" | "note") => Promise<void>;
  /** Open the full view's editor on a post. */
  openEditor: (slot: SocialSlot, opener: HTMLElement) => void;
}

/** Fetch the full draft ahead of the tap; the list carries a 160-character preview only. */
function preload(context: ExtensionContext, slot: SocialSlot): void {
  // The share-sheet probe and the picture load ahead of the tap too (neither can load after it).
  if (slot.platform === "linkedin") void probeShare(context);
  if (usesShareSheet(slot) && slot.visual) void loadVisual(context, slot);
  if (!slot.has_draft || loadedDraft(slot) !== null) return;
  void fetchSocialSlot(context, slot.id).then(
    (detail) => {
      if (detail.draft) rememberDraft(slot.id, detail.draft.text, detail.draft_rev);
    },
    () => undefined, // the tap falls back to the editor
  );
}

function actions(slot: SocialSlot, view: SocialView, run: Runner): HTMLElement {
  const { context } = run;
  const noDraft = !slot.has_draft;
  const copy = button("Copy", {
    title: noDraft ? "No draft yet" : "Copy the draft",
    disabled: noDraft,
    onClick: async (self) => {
      const text = loadedDraft(slot);
      if (text === null) {
        // Not loaded yet: copying would start after a round trip, which iOS drops.
        run.openEditor(slot, self);
        return;
      }
      // The clipboard write starts here, inside the tap, before any await.
      const copying = copyText(text);
      await run.go(async () => {
        self.textContent = (await copying) ? "Copied" : "Copy failed";
      }, "note");
    },
  });
  copy.prepend(copyIcon());
  const openButton = button("Open", {
    title: noDraft ? "No draft yet: opens the post with starter text" : "Copy the draft, then open the site",
    onClick: async (self) => {
      // Never disabled: with no draft it opens the editor, where the starter text and the Post
      // button are; with a draft it copies and opens the site.
      const text = noDraft ? null : loadedDraft(slot);
      if (text === null) {
        run.openEditor(slot, self);
        return;
      }
      const url = openUrl(slot, view, text);
      if (usesShareSheet(slot) && shareAvailable() !== false) {
        // The picture is still loading: the editor's button waits for it, so open that instead of
        // sharing the text alone by accident.
        if (visualState(slot) === "loading") {
          run.openEditor(slot, self);
          return;
        }
        // The share sheet (socialShare.ts): its copy, the remembered tap and the host call start
        // here, inside the tap, before run.go awaits anything.
        const sharing = shareToLinkedin({ context, slot, text });
        await run.go(async () => {
          const outcome = await sharing;
          if (outcome.kind === "shared") {
            self.textContent = "Shared";
            if (outcome.filesDropped) setNote("Shared the text. This phone's share sheet could not take the image.", "info");
          } else if (outcome.kind === "unavailable") {
            setNote(`${outcome.copied ? "Draft copied" : "Could not copy"}. This browser has no share sheet; paste it at linkedin.com/feed.`, "info");
          } else if (outcome.kind === "failed") {
            setNote(
              `${outcome.copied ? "Draft copied" : "Could not copy"}. Could not open the share sheet from here (${describeError(outcome.error)}); paste it at linkedin.com/feed.`,
              "info",
            );
          }
        }, "note");
        return;
      }
      const copying = copyText(text); // inside the tap, before any await
      await run.go(async () => {
        if (!url) {
          const copied = await copying;
          setNote(`${copied ? "Draft copied. " : ""}No channel link for this post.`, "info");
          return;
        }
        // Armed before the host call: on iOS the app switch happens while openExternal is still
        // pending, so the first visible or focus event can come before it resolves.
        const armed = Date.now();
        rememberPost(slot.id, slot.platform, armed);
        try {
          await context.navigation.openExternal(url);
        } catch (error) {
          forgetPost(slot.id, armed);
          // The host opens only links on its list; the draft is on the clipboard either way.
          const copied = await copying;
          const where = slot.platform === "linkedin" ? "linkedin.com/feed" : "the channel's Posts tab";
          setNote(
            `${copied ? "Draft copied" : "Could not copy"}. Could not open the site from here (${describeError(error)}); paste it at ${where}.`,
            "info",
          );
          return;
        }
        const copied = await copying;
        self.textContent = copied ? "Copied, opened" : "Opened";
      }, "note");
    },
  });
  openButton.prepend(externalLinkIcon());
  const posted = button("Posted", {
    title: "Mark this post as posted",
    onClick: async () => {
      await run.go(async () => {
        await markSocialPosted(context, slot.id);
      }, "refresh");
    },
  });
  posted.prepend(checkIcon());
  return el("div", { class: "social-actions" }, copy, openButton, posted);
}

/** The line under the header that an action leaves behind; cleared by the next action. */
let note: { text: string; tone: "error" | "info" } | null = null;
function setNote(text: string, tone: "error" | "info"): void {
  note = { text, tone };
}

function draw(parts: Parts, state: State, context: ExtensionContext, refresh: () => Promise<void>): void {
  const now = Date.now();
  const view = state.view;
  const redraw = () => draw(parts, state, context, refresh);
  const run: Runner = {
    context,
    go: async (task, after) => {
      note = null;
      let failed = false;
      try {
        await task();
      } catch (error) {
        failed = true;
        setNote(`That did not go through (${describeError(error)}).`, "error");
      }
      if (after === "refresh") await refresh();
      if (after === "refresh" || failed || note) redraw();
    },
    openEditor: (slot, opener) => {
      if (view) openSocialView(context, view, opener, refresh, slot.id);
    },
  };
  const said = state.error ? describeError(state.error) : "";
  // "Did you post it?" shows here while the full view is closed (it shows there when open).
  const asked = socialViewOpen() ? null : activeBanner(view);
  const askedSlot = asked ? view?.slots.find((s) => s.id === asked.slotId) : undefined;
  replaceChildren(parts.status, [
    said ? notice(`The Social card could not be read (${said}).`, "error") : null,
    note ? notice(note.text, note.tone) : null,
    asked
      ? bannerNode({ context, slotId: asked.slotId, platform: asked.platform, title: askedSlot ? slotTitle(askedSlot) : "", refresh })
      : null,
  ]);
  if (!view) {
    parts.pill.textContent = "";
    parts.pill.className = "social-pill";
    parts.pill.hidden = true;
    for (const part of [parts.scores, parts.warns, parts.next, parts.footer]) replaceChildren(part, []);
    if (!state.error) replaceChildren(parts.next, [el("p", { class: "meta" }, "Reading...")]);
    setTile(socialTile(null, state.error), state.at || undefined);
    return;
  }
  const pill = headerPill(view, now);
  parts.pill.hidden = false;
  parts.pill.className = `social-pill pill-${pill.tone}`;
  replaceChildren(parts.pill, [
    pill.tone === "err" ? alertTriangleIcon() : pill.tone === "ok" ? circleCheckIcon() : null,
    pill.text,
  ]);
  drawScores(parts, view);
  replaceChildren(
    parts.warns,
    warnings(view, now).map((warning) =>
      el("p", { class: `social-warn warn-${warning.tone}` }, alertTriangleIcon(), warning.text),
    ),
  );
  const rows = upNext(view.slots, now);
  const read = newestRead(view);
  replaceChildren(parts.next, [
    el("h3", { class: "social-h" }, "Up next"),
    rows.length
      ? el("div", { class: "social-rows" }, ...rows.map((slot, at) => rowFor(slot, view, now, at === 0, run)))
      : el(
          "p",
          { class: "meta social-empty" },
          read ? "Nothing left to post right now." : "Nothing planned yet. Waiting for the first YouTube read.",
        ),
  ]);
  replaceChildren(parts.footer, [
    el(
      "span",
      {},
      view.score.on_time_pct === null
        ? "On time: no posts in 30 d"
        : `On time ${view.score.on_time_pct}% · 30 d`,
    ),
    el("span", {}, read ? `YouTube read ${ago(read, now)}` : "YouTube not read yet"),
  ]);
  setTile(state.error ? staleTile(socialTile(view, null, now), state.error) : socialTile(view, null, now), state.at);
  parts.popout.disabled = false;
  pushSocialView(view, state.error);
}

/** The card's body; activateSocial fills it. The pill goes in the card header (see pillNode). */
export function socialSection(): HTMLElement {
  return el(
    "div",
    { class: "admin-block admin-social" },
    el("div", { class: "page-status social-status" }),
    el("div", { class: "social-stats" }),
    el("div", { class: "social-warns" }),
    el("div", { class: "social-next" }),
    el("div", { class: "social-footer" }),
  );
}

/** The header's Pop out button; activateSocial wires it to the full view. */
export function socialPopout(): HTMLElement {
  const b = el("button", {
    type: "button",
    class: "btn btn-quiet btn-icon social-popout",
    "aria-label": "Pop out the Social view",
    title: "Open the full Social view: calendar, scorecard and settings",
  });
  b.appendChild(arrowsMaximizeIcon());
  return b;
}

/** The header pill; mount it in the card header, activateSocial finds it by class. */
export function socialPill(): HTMLElement {
  const pill = el("span", { class: "social-pill" });
  pill.hidden = true;
  return pill;
}

let onFocus: (() => void) | null = null;
let offBanner: (() => void) | null = null;
/** Bumped by every activate and deactivate, as in git.ts. */
let generation = 0;

function stopListening(): void {
  if (!onFocus) return;
  window.removeEventListener("focus", onFocus);
  document.removeEventListener("visibilitychange", onFocus);
  onFocus = null;
  offBanner?.();
  offBanner = null;
}

/** Read and draw the card *root* (the card holding socialSection), again on every focus. */
export function activateSocial(context: ExtensionContext, root: HTMLElement): void {
  const find = <T extends HTMLElement>(selector: string): T => {
    const found = root.querySelector<T>(selector);
    if (!found) throw new Error(`Social card is missing ${selector}`);
    return found;
  };
  const popout = find<HTMLButtonElement>(".social-popout");
  const parts: Parts = {
    root,
    popout,
    pill: find(".social-pill"),
    status: find(".social-status"),
    scores: find(".social-stats"),
    warns: find(".social-warns"),
    next: find(".social-next"),
    footer: find(".social-footer"),
  };
  const mine = ++generation;
  const live = () => generation === mine;
  stopListening();
  note = null;
  resetDrafts();
  const state: State = { view: null, error: null, at: 0 };
  let reading: Promise<void> | null = null;
  const refresh = (): Promise<void> => {
    if (reading) return reading;
    reading = fetchSocial(context)
      .then(
        (answer) => {
          if (!live()) return;
          state.view = answer;
          state.at = Date.now();
          state.error = null;
        },
        (error: unknown) => {
          if (live()) state.error = error ?? new Error("Request failed");
        },
      )
      .then(() => {
        if (live()) draw(parts, state, context, refresh);
      })
      .finally(() => {
        reading = null;
      });
    return reading;
  };
  popout.onclick = () => {
    if (state.view) openSocialView(context, state.view, popout, refresh);
  };
  popout.disabled = true;
  draw(parts, state, context, refresh);
  void refresh();
  registerRefresh("social", () => (live() ? refresh() : undefined));
  onFocus = () => {
    if (document.visibilityState !== "visible") return;
    // Back on the page after a Post tap: ask "Did you post it?" once (socialPosted.ts).
    noteReturn(Date.now());
    void refresh();
  };
  offBanner = onBannerChange(() => {
    if (live()) draw(parts, state, context, refresh);
  });
  window.addEventListener("focus", onFocus);
  document.addEventListener("visibilitychange", onFocus);
}

export function deactivateSocial(): void {
  generation++;
  closeSocialView();
  stopListening();
}
