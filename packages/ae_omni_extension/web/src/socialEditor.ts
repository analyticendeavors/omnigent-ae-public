// The Social view's editor for one post (2026-11-06, part 2): the full draft, a character count
// against LinkedIn's 3,000 limit, Copy, Save and copy, Post on LinkedIn or Post on YouTube, Mark
// posted, Skip and Reschedule. It loads the slot with POST /social/slot (the list carries a
// 160-character preview only) and keeps what you typed until you leave the post; a live re-read
// of the card never touches it.
//
// Phone redesign (same day):
// - A post with no draft starts with a plain starter text built from the post (socialWords.ts
//   `starterText`), so Post is never stuck for lack of text. It is not saved until Save and it
//   never counts as unsaved edits. An expert post has none; its Post opens LinkedIn's composer
//   empty.
// - Post copies first and then opens the site. The copy starts inside the tap, before any
//   `await`: iOS drops a clipboard write that starts after an async round trip.
// - On a phone the editor is a screen of its own: a back link, the text, and a bar pinned to the
//   bottom with Post, Copy, Mark posted and More (Skip, Move, the post link, Post via API).
//   The same buttons are laid out the old way on a desktop.
//
// Every failure says what failed and what to do: a draft that did not save says the text is
// still here, a refused link says the draft is on the clipboard and where to paste it.

import type { ExtensionContext } from "@omnigent/extension-sdk";
import {
  describeError,
  fetchSocialSlot,
  markSocialPosted,
  reopenSocial,
  rescheduleSocial,
  saveSocialDraft,
  skipSocial,
  type SocialSlot,
  type SocialSlotDetail,
  type SocialView,
} from "./api";
import { button, el, notice, replaceChildren } from "./dom";
import { checkIcon, chevronLeftIcon, copyIcon, dotsIcon, externalLinkIcon } from "./icons";
import { copyText } from "./lot";
import { forgetFullDraft } from "./socialDrafts";
import { attachPopover, type Popover } from "./socialMenu";
import { forgetPost, rememberPost } from "./socialPosted";
import { probeShare, shareAvailable, shareToLinkedin, usesShareSheet } from "./socialShare";
import { loadVisual, loadedVisual, visualState, visualUrl } from "./socialVisual";
import {
  CHANNEL_NAMES,
  boldFirstLine,
  dueLabel,
  isFirstLineBold,
  isPhone,
  openUrl,
  postLabel,
  slotTitle,
  starterText,
  unboldFirstLine,
  watchPhone,
} from "./socialWords";

/** LinkedIn's limit, and the server's. */
export const LIMIT = 3000;
/** The server moves a post only inside this range around now. */
export const RESCHEDULE_BACK_DAYS = 1;
export const RESCHEDULE_AHEAD_DAYS = 120;
const DAY = 86_400_000;
/** The Share button waits this long for the picture before it shares the text only. */
export const IMAGE_WAIT_MS = 8_000;

/** Characters the way LinkedIn and the server count them: code points, not UTF-16 units. */
export const countChars = (text: string): number => [...text].length;

/**
 * The text a post with no draft starts with: the starter text, with line 1 in bold for a
 * LinkedIn post (2026-11-07; the "Bold line 1" chip undoes it). A Community post stays plain.
 */
export function initialStarter(slot: SocialSlot, names?: Record<string, string>): string {
  const text = starterText(slot, names);
  return slot.platform === "linkedin" ? boldFirstLine(text) : text;
}

/** "2026-11-10T09:00" in the browser's zone, for a datetime-local input. */
export function localInput(ms: number): string {
  const d = new Date(ms);
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

/**
 * Where the Move picker starts: the post's own time, or the next whole hour from *now* when the
 * post is overdue (its old time is outside what the server accepts), kept inside the range.
 */
export function moveDefault(dueMs: number, now: number): number {
  const range = { min: now - RESCHEDULE_BACK_DAYS * DAY, max: now + RESCHEDULE_AHEAD_DAYS * DAY };
  const wanted = dueMs < now ? (Math.floor(now / 3_600_000) + 1) * 3_600_000 : dueMs;
  return Math.min(Math.max(wanted, range.min), range.max);
}

/** The picker's range for *now*. */
export const rescheduleRange = (now: number) => ({
  min: localInput(now - RESCHEDULE_BACK_DAYS * DAY),
  max: localInput(now + RESCHEDULE_AHEAD_DAYS * DAY),
});

/** Why a post link cannot be saved, or null when it is empty or fine. */
export function postUrlProblem(value: string): string | null {
  const url = value.trim();
  if (!url) return null;
  if (!/^https:\/\/\S{1,490}$/.test(url)) return "The post link must start with https:// and have no spaces.";
  return null;
}

/**
 * Unsaved text by post id, for this page session: the dialog's X (or the card going away) closes
 * the view without asking, so the text is kept and comes back, with a note, when that post is
 * opened again. A Discard the user confirmed forgets it.
 */
const stash = new Map<string, string>();

/** Forget every kept text (tests start clean). */
export const resetEditorStash = (): void => stash.clear();

/** Forget the open post's kept text: the user chose to discard it. */
export function forgetDraft(host: HTMLElement): void {
  if (host.dataset.slot) stash.delete(host.dataset.slot);
}

/** Per editor host: stops watching the phone breakpoint, and reads the post again. */
const watchers = new WeakMap<HTMLElement, () => void>();
const reloaders = new WeakMap<HTMLElement, () => Promise<void>>();
/** The open editor's More popover: closed when the editor redraws or goes away, so its document
 *  listeners (Escape, outside click) never outlive it. */
const popovers = new WeakMap<HTMLElement, Popover>();

/** Read the open post again (its state may have changed elsewhere), keeping what is typed. */
export async function reloadEditor(host: HTMLElement): Promise<void> {
  await reloaders.get(host)?.();
}

export interface EditorHooks {
  context: ExtensionContext;
  /** The newest view, for the channel ids and the time zone. */
  view: () => SocialView | null;
  /** Read the card again after a change (the calendar and the queue follow). */
  changed: () => Promise<void>;
  /** The editor closed (or the post went away): the view clears its selection. */
  closed: () => void;
  /** What the phone's back link says it goes back to: "Week" or "Month". */
  backLabel?: () => string;
}

export function openEditor(host: HTMLElement, slotId: string, hooks: EditorHooks): void {
  watchers.get(host)?.();
  replaceChildren(host, [el("p", { class: "meta" }, "Loading the post...")]);
  delete host.dataset.dirty; // any guard has been answered by now
  host.dataset.slot = slotId;
  void fetchSocialSlot(hooks.context, slotId).then(
    (detail) => {
      if (host.dataset.slot === slotId) draw(host, detail, hooks);
    },
    (error: unknown) => {
      if (host.dataset.slot !== slotId) return;
      delete host.dataset.slot; // a later click on the same post tries again
      replaceChildren(host, [
        notice(`Could not load that post (${describeError(error)}). Close it and open it again; if it keeps failing, reload the page.`, "error"),
        closeButton(host, hooks),
      ]);
    },
  );
}

function closeButton(host: HTMLElement, hooks: EditorHooks): HTMLElement {
  return button("Close", {
    title: "Close the editor",
    onClick: () =>
      guard(host, () => {
        forgetDraft(host);
        closeEditor(host);
        hooks.closed();
      }),
  });
}

/** True while the open editor holds text that was not saved. */
export const isDirty = (host: HTMLElement): boolean => host.dataset.dirty === "1";

/**
 * Run *proceed* at once when nothing is unsaved; otherwise ask inline first. The page is
 * sandboxed with allow-scripts only, so window.confirm is not available.
 */
export function guard(host: HTMLElement, proceed: () => void): void {
  const status = host.querySelector<HTMLElement>(".sv-ed-status");
  if (!isDirty(host) || !status) {
    proceed();
    return;
  }
  const discard = button("Discard", {
    kind: "danger",
    title: "Throw away the edits you have not saved",
    // The flag stays until the action has worked: a failed Mark posted or Skip keeps the text
    // and the flag (the redraw after a success, or the editor closing, clears them).
    onClick: () => proceed(),
  });
  const keep = button("Keep editing", { title: "Go back to the text", onClick: () => replaceChildren(status, []) });
  replaceChildren(status, [
    el("div", { class: "sv-ed-guard", role: "alert" }, el("span", {}, "Discard unsaved edits?"), discard, keep),
  ]);
}

export function closeEditor(host: HTMLElement): void {
  popovers.get(host)?.close();
  popovers.delete(host);
  watchers.get(host)?.();
  watchers.delete(host);
  reloaders.delete(host);
  delete host.dataset.dirty;
  delete host.dataset.slot;
  host.classList.remove("is-phone");
  replaceChildren(host, []);
}

function draw(host: HTMLElement, detail: SocialSlotDetail, hooks: EditorHooks, fresh = true): void {
  void probeShare(hooks.context); // cached after the first ask; the tap reads the answer
  popovers.get(host)?.close();
  watchers.get(host)?.();
  const view = hooks.view();
  const tz = view?.rules.tz ?? "UTC";
  let slot = detail;
  // The starter text stands in for a draft that does not exist yet. `base` is what the box holds
  // as "not edited": the saved draft, or the starter text, so the starter is never "unsaved".
  let starter = detail.draft ? "" : initialStarter(detail, view?.channel_names);
  const saved = { text: detail.draft?.text ?? starter };
  const status = el("div", { class: "sv-ed-status", "aria-live": "polite" });
  const say = (text: string, tone: "error" | "info" | "ok") => replaceChildren(status, [notice(text, tone)]);
  const clear = () => replaceChildren(status, []);

  const area = el("textarea", {
    class: "sv-ed-text",
    rows: "12",
    "aria-label": "Post text",
    placeholder: slot.has_draft ? "" : "No draft yet. Write the post here, then Save and copy.",
    spellcheck: "true",
  });
  area.value = saved.text;
  // Text kept from an earlier visit (the view was closed with the X while it was unsaved).
  const kept = fresh ? stash.get(detail.id) : undefined;
  const restored = kept !== undefined && kept !== saved.text;
  if (restored) area.value = kept;
  const count = el("span", { class: "sv-ed-count", "aria-live": "polite" });
  const dirtyNote = el("span", { class: "sv-ed-dirty" });
  const text = () => area.value;
  const buttons: HTMLButtonElement[] = [];
  const over = () => countChars(text()) > LIMIT;
  const empty = () => text().trim().length === 0;
  const limitNote = el("p", { class: "sv-ed-limit", role: "alert" });
  limitNote.hidden = true;
  // "Bold line 1": LinkedIn shows plain text, so line 1 is set in Unicode's sans-serif bold
  // letters (socialWords.ts). Pressed means line 1 is bold now; a tap rewrites line 1 either way.
  const boldChip = el(
    "button",
    {
      type: "button",
      class: "sv-bold",
      "aria-pressed": "false",
      title: "Set the first line in bold letters (LinkedIn shows it bold in the feed)",
    },
    el("b", {}, "B"),
    " Bold line 1",
  );
  // True while the Share button waits for the picture (at most IMAGE_WAIT_MS).
  let imageWaiting = false;
  boldChip.addEventListener("click", () => {
    area.value = isFirstLineBold(area.value) ? unboldFirstLine(area.value) : boldFirstLine(area.value);
    area.dispatchEvent(new Event("input"));
  });

  const sync = () => {
    const n = countChars(text());
    count.textContent = `${n.toLocaleString("en-US")} / ${LIMIT.toLocaleString("en-US")}`;
    count.className = `sv-ed-count${n > LIMIT ? " is-over" : n > LIMIT * 0.9 ? " is-near" : ""}`;
    if (text() !== saved.text) {
      host.dataset.dirty = "1";
      stash.set(slot.id, text());
    } else {
      delete host.dataset.dirty;
      stash.delete(slot.id);
    }
    const usingStarter = !slot.draft && starter !== "" && text() === starter;
    // "Starter text" is a short tag that fits on the meta line; its full sentence is the tooltip.
    dirtyNote.textContent =
      text() !== saved.text
        ? "Unsaved edits"
        : usingStarter
          ? "Starter text"
          : slot.draft
            ? `Draft by ${slot.draft.by === "reid" || slot.draft.edited ? "you" : "the weekly run"}`
            : "";
    const starterTip = text() === saved.text && usingStarter ? "Starter text, edit before posting" : "";
    if (starterTip) dirtyNote.setAttribute("title", starterTip);
    else dirtyNote.removeAttribute("title");
    dirtyNote.classList.toggle("is-starter", usingStarter && text() === saved.text);
    boldChip.setAttribute("aria-pressed", isFirstLineBold(text()) ? "true" : "false");
    limitNote.hidden = !over();
    limitNote.textContent = over() ? `Over ${LIMIT.toLocaleString("en-US")} characters. Shorten the text to post or copy it.` : "";
    for (const b of buttons) {
      // Post is never stuck for lack of text (an expert post opens the composer empty); Copy
      // and Save need something to copy or save. All of them wait for the limit.
      if (b.dataset.needs === "text") b.disabled = empty() || over();
      if (b.dataset.needs === "save") b.disabled = empty() || over();
      if (b.dataset.needs === "limit") b.disabled = over() || (b === post && imageWaiting);
      if (b.disabled && over()) b.title = `Over ${LIMIT.toLocaleString("en-US")} characters: shorten the text first`;
      else if (b.dataset.title) b.title = b.dataset.title;
    }
  };
  area.addEventListener("input", sync);

  const needs = (b: HTMLButtonElement, what: "text" | "save" | "limit") => {
    b.dataset.needs = what;
    if (b.title) b.dataset.title = b.title;
    buttons.push(b);
    return b;
  };

  const saveDraft = async (): Promise<boolean> => {
    try {
      const value = text();
      await saveSocialDraft(hooks.context, slot.id, value, slot.topic);
      saved.text = value;
      // Saved: the post now has a draft, so the starter text is no longer on offer, and the
      // compact card's cached full draft for it is stale.
      starter = "";
      slot = {
        ...slot,
        has_draft: true,
        draft_preview: value.slice(0, 160),
        draft: { text: value, topic: slot.topic, by: "reid", edited: true },
      };
      forgetFullDraft(slot.id);
      return true;
    } catch (error) {
      say(`The draft did not save (${describeError(error)}). Your text is still here; try Save and copy again.`, "error");
      return false;
    }
  };

  const copy = button("Copy", {
    title: "Copy the text above, as it is now",
    onClick: async (self) => {
      clear();
      // The clipboard write starts here, inside the tap, before any await.
      const ok = copyText(text());
      self.textContent = (await ok) ? "Copied" : "Copy failed";
      if (self.textContent === "Copy failed") say("The clipboard refused the copy. Select the text and copy it by hand.", "error");
      self.prepend(copyIcon());
      sync();
    },
  });
  copy.prepend(copyIcon());
  needs(copy, "text");

  const saveCopy = button("Save and copy", {
    title: "Keep your edits (the weekly run will not overwrite them), then copy",
    onClick: async (self) => {
      clear();
      // The clipboard write starts here, inside the tap, before the save's round trip (iOS
      // drops a write that starts after one), from the text on screen.
      const copying = copyText(text());
      if (!(await saveDraft())) {
        const copiedOnly = await copying;
        if (copiedOnly) say("The text was copied, but the draft did not save. Your text is still here; try Save and copy again.", "error");
        return;
      }
      const ok = await copying;
      self.textContent = ok ? "Saved, copied" : "Saved";
      self.prepend(copyIcon());
      if (!ok) say("Saved, but the clipboard refused the copy. Select the text and copy it by hand.", "info");
      await hooks.changed();
      sync();
    },
  });
  saveCopy.prepend(copyIcon());
  needs(saveCopy, "save");

  const site = slot.platform === "linkedin" ? "LinkedIn" : "YouTube";
  // A LinkedIn post on a touch device goes through the phone's share sheet (socialShare.ts).
  const sheet = usesShareSheet(slot);
  const post = button(sheet ? "Share to LinkedIn" : postLabel(slot), {
    kind: "primary",
    title: sheet
      ? "Open your phone's share sheet with the text and the image, then pick LinkedIn (you still press Post there)"
      : slot.platform === "linkedin"
        ? "Copy the text, then open LinkedIn's composer with it filled in (you still press Post there)"
        : "Copy the text, then open the channel's Posts tab (you still press Post there)",
    onClick: async () => {
      clear();
      const value = text();
      // Sheet or link is chosen here, in the tap, from the cached probe answer (socialShare.ts).
      if (sheet && value && shareAvailable() !== false) {
        const outcome = await shareToLinkedin({ context: hooks.context, slot, text: value });
        if (outcome.kind === "unavailable") {
          say(
            `${outcome.copied ? "Draft copied" : "Could not copy"}. This browser has no share sheet; paste it at linkedin.com/feed.`,
            "info",
          );
        } else if (outcome.kind === "failed") {
          say(
            `${outcome.copied ? "Draft copied" : "Could not copy"}. Could not open the share sheet from here (${describeError(outcome.error)}); paste it at linkedin.com/feed.`,
            "info",
          );
        } else if (outcome.kind === "shared" && outcome.filesDropped) {
          say("Shared the text. This phone's share sheet could not take the image; long-press it below to save it.", "info");
        }
        return;
      }
      // The clipboard write starts here, inside the tap, before any await (iOS drops a write
      // that starts after a round trip). An empty text (an expert post) copies nothing.
      const copying = value ? copyText(value) : Promise.resolve(false);
      const url = view ? openUrl(slot, view, value) : null;
      if (!url) {
        const copied = await copying;
        say(`${copied ? "Draft copied. " : ""}No channel link for this post; open YouTube Studio's Posts tab by hand.`, "info");
        return;
      }
      // Armed before the host call: on iOS the app switch happens while openExternal is still
      // pending, so the first visible or focus event can come before it resolves.
      const armed = Date.now();
      rememberPost(slot.id, slot.platform, armed);
      try {
        await hooks.context.navigation.openExternal(url);
      } catch (error) {
        forgetPost(slot.id, armed);
        const copied = await copying;
        const where = slot.platform === "linkedin" ? "linkedin.com/feed" : "the channel's Posts tab";
        say(
          `${copied ? "Draft copied" : "Could not copy"}. Could not open the site from here (${describeError(error)}); paste it at ${where}.`,
          "info",
        );
        return;
      }
      const copied = await copying;
      say(
        copied ? `Draft copied and ${site} opened. Press Post there.` : value ? `${site} opened. The clipboard refused the copy; the text is in the box above.` : `${site} opened.`,
        "ok",
      );
    },
  });
  post.prepend(externalLinkIcon());
  needs(post, "limit");
  const readyHint = slot.visual ? "Opens your phone's share sheet with the text and the image" : "Opens your phone's share sheet with the text";
  const failedHint = "The image could not load; this shares the text only.";
  const shareHint = sheet ? el("p", { class: "meta sv-ed-hint" }, readyHint) : null;
  const setPostLabel = (label: string) => {
    post.textContent = label;
    post.prepend(externalLinkIcon());
  };
  // The button waits for the picture (at most IMAGE_WAIT_MS) so a share never goes out as text
  // only by accident; a picture that failed or is late says so under the button.
  if (sheet && slot.visual && visualState(slot) === "loading") {
    imageWaiting = true;
    setPostLabel("Loading image...");
    const settle = () => {
      if (!imageWaiting) return;
      imageWaiting = false;
      if (host.dataset.slot !== slot.id) return;
      setPostLabel("Share to LinkedIn");
      if (shareHint && visualState(slot) !== "ready") shareHint.textContent = failedHint;
      sync();
    };
    void loadVisual(hooks.context, slot).then(() => {
      settle();
      // A picture that arrives after the wait ended makes the hint true again.
      if (shareHint && visualState(slot) === "ready") shareHint.textContent = readyHint;
    });
    setTimeout(settle, IMAGE_WAIT_MS);
  } else if (sheet && slot.visual && visualState(slot) === "failed" && shareHint) {
    shareHint.textContent = failedHint;
  }

  // ── the picture that goes in the share sheet with the text ──
  // Loaded now, so the File is ready before the tap; the server fetches it (the frame cannot).
  const visualRow = ((): HTMLElement | null => {
    if (!slot.visual) return slot.visual_note ? el("p", { class: "meta sv-ed-vis-note" }, slot.visual_note) : null;
    const thumb = el("div", { class: "sv-ed-thumb is-loading" }, "Loading image...");
    const row = el(
      "div",
      { class: "sv-ed-vis" },
      thumb,
      el(
        "div",
        { class: "sv-ed-vis-t" },
        el("b", {}, slot.visual.kind === "tool" ? "Visual: the tool image" : "Visual: the video's thumbnail"),
        el(
          "span",
          {},
          sheet ? "Goes into the share sheet with the text. Long-press to save it." : "Long-press or right-click to save it, then attach it in LinkedIn.",
        ),
      ),
    );
    const show = (blob: Blob | null) => {
      const url = blob ? visualUrl(blob) : null;
      if (url) {
        thumb.replaceWith(el("img", { class: "sv-ed-thumb", src: url, alt: "The image shared with this post" }));
      } else {
        thumb.className = "sv-ed-thumb is-failed";
        thumb.textContent = blob ? "Saved for sharing" : "Image unavailable";
      }
    };
    const have = loadedVisual(slot);
    if (have) show(have);
    else void loadVisual(hooks.context, slot).then(show);
    return row;
  })();

  const postUrl = el("input", {
    type: "url",
    class: "sv-ed-url",
    placeholder: "Post link (optional)",
    "aria-label": "Link to the finished post (optional)",
    maxlength: "490",
  });
  const posted = button("Mark posted", {
    kind: slot.state === "planned" ? "quiet" : "primary",
    title: "Say this is posted; the link is optional",
    onClick: () => {
      clear();
      const problem = postUrlProblem(postUrl.value);
      if (problem) {
        say(problem, "error");
        return;
      }
      guard(host, async () => {
        try {
          await markSocialPosted(hooks.context, slot.id, postUrl.value.trim() || undefined);
          await hooks.changed();
          await reload(false);
        } catch (error) {
          say(`Could not mark it posted (${describeError(error)}). Nothing changed; try again.`, "error");
        }
      });
    },
  });
  posted.prepend(checkIcon());
  posted.classList.add("sv-ed-mark");

  const skip = button("Skip", {
    title: slot.skippable ? "Skip this optional post" : "Skip this post: it stops counting toward your week",
    onClick: () => {
      clear();
      guard(host, async () => {
        try {
          await skipSocial(hooks.context, slot.id);
          await hooks.changed();
          await reload(false);
        } catch (error) {
          say(`Could not skip it (${describeError(error)}). Nothing changed; try again.`, "error");
        }
      });
    },
  });

  const reopen = button("Reopen", {
    title: "Put this post back in the queue",
    onClick: async () => {
      clear();
      try {
        await reopenSocial(hooks.context, slot.id);
        await hooks.changed();
        await reload();
      } catch (error) {
        say(`Could not reopen it (${describeError(error)}). Nothing changed; try again.`, "error");
      }
    },
  });

  const range = rescheduleRange(Date.now());
  const when = el("input", {
    type: "datetime-local",
    class: "sv-ed-when",
    min: range.min,
    max: range.max,
    value: localInput(moveDefault(Date.parse(slot.due), Date.now())),
    "aria-label": "New date and time (your device's clock)",
  });
  const move = button("Move", {
    title: "Move this post to the date and time on the left (your device's clock; a day back to 120 days ahead)",
    onClick: async () => {
      clear();
      const ms = new Date(when.value).getTime();
      if (!when.value || Number.isNaN(ms)) {
        say("Pick a date and time first.", "error");
        return;
      }
      const now = Date.now();
      if (ms < now - RESCHEDULE_BACK_DAYS * DAY || ms > now + RESCHEDULE_AHEAD_DAYS * DAY) {
        say("Pick a time from a day ago to 120 days ahead.", "error");
        return;
      }
      try {
        await rescheduleSocial(hooks.context, slot.id, new Date(ms).toISOString());
        await hooks.changed();
        await reload();
      } catch (error) {
        say(`Could not move it (${describeError(error)}). Nothing changed; pick another time.`, "error");
      }
    },
  });

  const api = button("Post via API", {
    title: "Needs a LinkedIn app; later phase",
    disabled: true,
    onClick: () => undefined,
  });

  /** Read the post again after an action: the state and the due time may have changed. */
  async function reload(keepTyped = true): Promise<void> {
    try {
      const next = await fetchSocialSlot(hooks.context, slot.id);
      if (host.dataset.slot !== slot.id) return;
      slot = next;
      // Keep what is typed unless the user just discarded it (Mark posted, Skip).
      const typed = area.value;
      if (!keepTyped) stash.delete(slot.id);
      draw(host, next, hooks, false);
      const again = host.querySelector<HTMLTextAreaElement>(".sv-ed-text");
      if (keepTyped && again && typed !== (next.draft?.text ?? initialStarter(next, hooks.view()?.channel_names))) {
        again.value = typed;
        again.dispatchEvent(new Event("input"));
      }
    } catch (error) {
      say(`Saved, but could not read the post again (${describeError(error)}). Reload the page to see it.`, "info");
    }
  }
  reloaders.set(host, () => reload());

  const closed = slot.state !== "planned";
  const late = slot.state === "planned" && Date.parse(slot.due) < Date.now();
  const head = el("div", { class: "sv-ed-head" }, el("h4", { class: "sv-ed-title" }, slotTitle(slot)));
  if (slot.draft_error) head.appendChild(notice(`The weekly run could not draft this: ${slot.draft_error}`, "error"));
  if (slot.posted?.url) head.appendChild(el("p", { class: "meta" }, `Post link: ${slot.posted.url}`));

  // One line under the title (2026-11-07): when it is due, the channel, the count, and the bold
  // chip. What the box holds (unsaved, starter, draft by) wraps under it only when it has
  // something to say.
  const dueText =
    slot.state === "posted"
      ? `Posted${slot.posted ? ` ${dueLabel(slot.posted.at, Date.now(), tz)}` : ""}`
      : slot.state === "skipped"
        ? "Skipped"
        : `${late ? "" : "Due "}${dueLabel(slot.due, Date.now(), tz)}`;
  const channelName = slot.channel ? (CHANNEL_NAMES[slot.channel] ?? slot.channel.toUpperCase()) : "";
  // The due text truncates on a narrow phone; the channel and the count never do.
  const info = el(
    "span",
    { class: "sv-ed-info" },
    el(
      "span",
      { class: "sv-ed-sub" },
      dueText,
      slot.rescheduled ? " · moved" : "",
      slot.skippable && slot.state === "planned" ? " · optional" : "",
    ),
    el("span", { class: "sv-ed-tail" }, channelName ? ` · ${channelName} · ` : " · ", count),
  );
  boldChip.hidden = slot.platform !== "linkedin";
  const meta = el("div", { class: "sv-ed-meta" }, info, boldChip, dirtyNote);
  const apiRow = el("div", { class: "sv-ed-row sv-ed-api" }, api, el("span", { class: "meta" }, "Needs a LinkedIn app; later phase."));

  // ── the phone's back link and More panel ──
  const back = button(hooks.backLabel?.() ?? "Week", {
    title: "Back to the list",
    onClick: () =>
      guard(host, () => {
        forgetDraft(host);
        closeEditor(host);
        hooks.closed();
      }),
  });
  back.classList.add("sv-ed-back");
  back.prepend(chevronLeftIcon());

  const moreWrap = el("div", { class: "sv-more-wrap" });
  const moreButton = el("button", {
    type: "button",
    class: "btn btn-quiet sv-more-btn",
    "aria-label": "More actions",
    "aria-haspopup": "true",
    title: "Skip, move, or add the post link",
  });
  moreButton.append(dotsIcon(), el("span", {}, "More"));
  const morePanel = el("div", { class: "sv-pop sv-more-panel", role: "group", "aria-label": "More actions" });
  moreWrap.append(moreButton, morePanel);
  const morePop = attachPopover(moreButton, morePanel);
  popovers.set(host, morePop);

  /** Lay the same controls out for the width now: the phone's screen or the desktop's rows. */
  const place = () => {
    if (host.dataset.slot !== slot.id) return;
    morePop.close();
    const phone = isPhone();
    host.classList.toggle("is-phone", phone);
    if (phone) {
      // Skip, Move, the post link and Post via API live behind More.
      replaceChildren(morePanel, [
        el("div", { class: "sv-ed-row" }, postUrl),
        el("div", { class: "sv-ed-row sv-ed-move" }, el("span", { class: "meta" }, "Move to"), when, move),
        el("div", { class: "sv-ed-row" }, skip),
        apiRow,
      ]);
      const bar = closed
        ? el("div", { class: "sv-ed-bar" }, el("div", { class: "sv-ed-bar-row" }, reopen, copy))
        : el(
            "div",
            { class: "sv-ed-bar" },
            post,
            shareHint,
            el("div", { class: "sv-ed-bar-row" }, copy, posted, moreWrap),
          );
      replaceChildren(host, [
        back,
        head,
        meta,
        limitNote,
        area,
        status,
        closed ? null : el("div", { class: "sv-ed-row sv-ed-save" }, saveCopy),
        visualRow,
        bar,
      ]);
      return;
    }
    const actionRow = closed
      ? el("div", { class: "sv-ed-row" }, reopen, copy, closeButton(host, hooks))
      : el("div", { class: "sv-ed-row" }, post, saveCopy, copy, closeButton(host, hooks));
    const postedRow = closed ? null : el("div", { class: "sv-ed-row sv-ed-posted" }, postUrl, posted, skip);
    const moveRow = closed ? null : el("div", { class: "sv-ed-row sv-ed-move" }, el("span", { class: "meta" }, "Move to"), when, move);
    replaceChildren(host, [head, meta, limitNote, area, status, visualRow, actionRow, postedRow, moveRow, apiRow]);
  };
  place();
  watchers.set(
    host,
    watchPhone(() => place()),
  );
  sync();
  if (restored) say("Unsaved edits restored.", "info");
}
