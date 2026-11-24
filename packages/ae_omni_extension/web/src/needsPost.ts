// An opened Social row in Needs you is ready to post from (2026-11-16): the draft text, the picture and
// the Social card's own actions (Copy text, Open on LinkedIn, Posted, Copy picture, Download picture and
// Skip post) sit in the row, so nobody scrolls down to the Social card. The post is loaded by the
// card's own routes (`fetchSocialSlot`, `fetchSocial`) and the actions are the card's own functions
// (socialActions.ts, socialVisual.ts). Untrusted text (the draft comes from the drafter and from
// people) only goes through text nodes; no link or image source is built from it.
//
// Skip and Posted change the Social plan, which is what the Social card shows; Snooze only hides the
// row for a while, and the generic Dismiss is not offered on a Social row for that reason.

import type { ExtensionContext } from "@omnigent/extension-sdk";
import {
  AeRequestError,
  type AttentionItem,
  describeError,
  fetchSocial,
  fetchSocialSlot,
  reopenSocial,
  type SocialSlot,
  type SocialSlotDetail,
  type SocialView,
} from "./api";
import { type Panel, reloadPanel } from "./attention";
import { el } from "./dom";
import { sayInRow } from "./rowMessages";
import { checkIcon, copyIcon, downloadIcon, externalLinkIcon, photoIcon, skipIcon } from "./icons";
import { forgetHistory } from "./historyCache";
import { socialPictureFor, toast, toastLine } from "./needsMore";
import { shareAvailable, usesShareSheet } from "./socialShare";
import { canCopyFresh, copyDraft, openNeedsEditor, openPost, postPosted, postSkipped, startFreshCopy } from "./socialActions";
import { rememberDraft } from "./socialDrafts";
import { canCopyPicture, copyPicture, downloadPicture, loadedVisual, revokePictureUrls, visualState } from "./socialVisual";

// ── the post, read again each time its row draws ───────────────────────────

export type PostState =
  | { status: "loading" }
  | { status: "ready"; detail: SocialSlotDetail; view: SocialView | null; text: string | null; rev: string | null; stale?: boolean }
  | { status: "failed"; error: unknown };

const posts = new Map<string, PostState>();
const asking = new Map<string, Promise<void>>();
/** Which read is the newest for a slot: an older read that lands late never replaces a newer result. */
let readSeq = 0;
const appliedSeq = new Map<string, number>();

/** Forget every loaded post and picture URL (the tests, and the Admin Center page being left). */
export function resetPosts(): void {
  posts.clear();
  asking.clear();
  readAt.clear();
  appliedSeq.clear();
  planBusy.clear();
  planButtons.clear();
  revokePictureUrls();
}

/** Drop one post's entry (Posted, Skip and Undo change the plan, so the next opening reads it fresh). */
export function dropPost(slotId: string): void {
  posts.delete(slotId);
  asking.delete(slotId);
  readAt.delete(slotId);
  appliedSeq.delete(slotId);
}

/** The draft the row holds, and when it was read (for "as of" wording); null when there is none. */
export function heldDraft(slotId: string): { text: string; at: number } | null {
  const state = posts.get(slotId);
  const held = readAt.get(slotId);
  return state?.status === "ready" && state.text !== null && held ? { text: state.text, at: held.at } : null;
}

export const postOf = (slotId: string): PostState => posts.get(slotId) ?? { status: "loading" };

/** How long a read stays good on a plain redraw (the page polls every 20 seconds; the row does not ask each time). */
export const POST_FRESH_MS = 2 * 60_000;

/** When each held post was read, and the listed item's signature then (its fingerprint and kind change with the post). */
const readAt = new Map<string, { at: number; sig: string; old: boolean }>();

/** Mark a held post old, so the next time its row opens it is read again (a closed row does this on every draw). The time of the read is kept. */
export function markStale(slotId: string): void {
  const held = readAt.get(slotId);
  if (held) held.old = true;
}

/**
 * Read the post from the server when it needs it: the row has just opened (a closed row marks it stale), a plan call
 * changed it, the listed item's signature changed, or the read is more than two minutes old. A plain poll redraw of an
 * unchanged item reuses what is held and sends nothing. A copy already held stays on show while the fresh one loads
 * (no flicker), and is replaced by it. A held read can be up to two minutes old on a redraw, and the server's item
 * fingerprint does not change when the draft is edited in the Social card, so Copy text and Open on LinkedIn read the
 * slot again at the click instead of trusting the held text. The draft is kept under the slot's draft revision. The card's full view is read only when the slot's platform needs it (a YouTube
 * post needs the channel ids; a LinkedIn post needs nothing from it). A failed read shows as a failure until the next try.
 */
export function loadPost(context: ExtensionContext, slotId: string, sig = "", force = false, now = Date.now()): Promise<void> {
  // A forced read waits for no older one (it may have started before an edit); a plain one joins the read in flight.
  const pending = force ? undefined : asking.get(slotId);
  if (pending) return pending;
  const have = posts.get(slotId);
  const held = readAt.get(slotId);
  if (!force && have?.status === "ready" && held && !held.old && held.sig === sig && now - held.at < POST_FRESH_MS) return Promise.resolve();
  if (!have) posts.set(slotId, { status: "loading" });
  const mine = (readSeq += 1);
  const task = (async () => {
    const detail = await fetchSocialSlot(context, slotId);
    const view = detail.platform === "linkedin" ? null : await fetchSocial(context).catch(() => null);
    // The newer read wins when both land: an older result that arrives late is dropped.
    if (mine < (appliedSeq.get(slotId) ?? 0)) return;
    appliedSeq.set(slotId, mine);
    const text = detail.draft ? detail.draft.text : null;
    if (text !== null) rememberDraft(slotId, text, detail.draft_rev);
    posts.set(slotId, { status: "ready", detail, view, text, rev: detail.draft_rev ?? null });
    readAt.set(slotId, { at: Date.now(), sig, old: false });
  })().catch((error: unknown) => {
    if (mine < (appliedSeq.get(slotId) ?? 0)) return;
    // A failure counts as the newest answer too: an older read that lands later is dropped, not taken as fresh.
    appliedSeq.set(slotId, Math.max(appliedSeq.get(slotId) ?? 0, mine));
    const last = posts.get(slotId);
    if (last?.status === "ready") {
      // Keep the last good draft and when it was read, marked stale; the row says so and the next draw tries again.
      posts.set(slotId, { ...last, stale: true });
      const at = readAt.get(slotId);
      if (at) at.old = true;
    } else {
      posts.set(slotId, { status: "failed", error });
      readAt.delete(slotId);
    }
  });
  const done = task.finally(() => {
    if (asking.get(slotId) === done) asking.delete(slotId);
  });
  asking.set(slotId, done);
  return done;
}

// ── the slot the picture belongs to ─────────────────────────────────────────

/** The slot as the card's picture code keys it: the row's picture identity wins, so the block and the buttons share one blob. */
function slotFor(item: AttentionItem, state: PostState): SocialSlot {
  const detail = state.status === "ready" ? state.detail : null;
  const base = (detail ?? { id: item.slot }) as unknown as SocialSlot;
  const visual = item.visual ?? detail?.visual;
  return { ...base, id: item.slot as string, visual: visual ?? undefined };
}

// ── buttons ─────────────────────────────────────────────────────────────────

function barButton(label: string, icon: SVGElement, title: string, focus: string): HTMLButtonElement {
  const node = el("button", { type: "button", class: "btn btn-quiet nt-labelled attn-btn", title, "data-focus": focus });
  node.append(icon, el("span", { class: "nt-label" }, label));
  return node;
}

const clockOf = (at: number): string => new Date(at).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });

const setLabel = (node: HTMLElement, text: string): void => {
  const label = node.querySelector(".nt-label");
  if (label) label.textContent = text;
};

/** Where a row's result line goes: the sheet's message area, else the opened panel, else the page. */
function lineHost(p: Panel, id: string): HTMLElement {
  const row = p.root.querySelector<HTMLElement>(`.nt-row[data-id="${CSS.escape(id)}"], .attn-row[data-id="${CSS.escape(id)}"]`);
  return p.root.querySelector<HTMLElement>(".nt-sheet-msg") ?? row?.querySelector<HTMLElement>(".nt-panel-main") ?? p.root;
}

// ── Posted and Skip post: one change at a time, checked first, with an Undo ───

/** Slots with a plan call (Posted, Skip, Undo) in flight, and the buttons that act on each. */
const planBusy = new Set<string>();

/** What the listed item says about its post: when it changes, the post is read again. */
const itemSig = (item: AttentionItem): string => `${item.fingerprint}|${item.kind}`;
const planButtons = new Map<string, Set<HTMLButtonElement>>();

const mounted = new WeakSet<HTMLButtonElement>();

/** How many buttons are registered for *slotId* (the tests). */
export const planButtonCount = (slotId: string): number => planButtons.get(slotId)?.size ?? 0;

/**
 * Register a button that changes the plan for *slotId*: it is disabled while any such call for the slot runs. Every
 * redraw builds new buttons, so registering drops the ones an earlier draw left detached; a new button that is not in
 * the page by the end of the current task was never shown and goes too.
 */
export function registerPlanButton(slotId: string, node: HTMLButtonElement): void {
  let set = planButtons.get(slotId);
  if (!set) planButtons.set(slotId, (set = new Set()));
  for (const old of set) if (mounted.has(old) && !old.isConnected) set.delete(old);
  set.add(node);
  node.disabled = planBusy.has(slotId);
  queueMicrotask(() => {
    if (node.isConnected) mounted.add(node);
    else set.delete(node);
    // The draw that replaced the earlier buttons has finished by now: drop them.
    for (const old of set) if (mounted.has(old) && !old.isConnected) set.delete(old);
  });
}

function setBusy(slotId: string, on: boolean): void {
  if (on) planBusy.add(slotId);
  else planBusy.delete(slotId);
  for (const node of planButtons.get(slotId) ?? []) {
    if (!node.isConnected) planButtons.get(slotId)?.delete(node);
    node.disabled = on;
  }
}

export const UNDO_MS = 10_000;

/**
 * Before acting, read the slot again: act only if it is still this slot and still planned. A row that went stale
 * (the post was posted, skipped or removed in the Social card meanwhile) never acts; it says so and reloads.
 */
async function stillPlanned(p: Panel, item: AttentionItem): Promise<boolean> {
  const slotId = item.slot as string;
  let detail: SocialSlotDetail | null = null;
  let gone = false;
  try {
    detail = await fetchSocialSlot(p.context, slotId);
  } catch (error) {
    if (error instanceof AeRequestError && error.status === 404) gone = true;
    else {
      toastLine(p, `Could not check the post first (${describeError(error)}). Nothing changed; try again.`, "error");
      return false;
    }
  }
  if (gone || !detail || detail.id !== slotId || detail.state !== "planned") {
    const now = detail && detail.state !== "planned" ? ` (it is now ${detail.state})` : "";
    toastLine(p, `This post changed${now}, so nothing was done. Reloading.`, "info");
    dropPost(slotId);
    forgetHistory(item.id);
    await reloadPanel(p).catch(() => undefined);
    return false;
  }
  return true;
}

/**
 * Posted or Skip post: one at a time per slot, after a fresh read says the post is still planned, then the card's
 * own call. The result shows in the page's message line (the row leaves on the reload) with an Undo for ten seconds
 * that reopens the post.
 */
export async function changePlan(p: Panel, item: AttentionItem, kind: "posted" | "skip"): Promise<void> {
  const slotId = item.slot as string;
  if (planBusy.has(slotId)) return;
  setBusy(slotId, true);
  try {
    if (!(await stillPlanned(p, item))) return;
    try {
      if (kind === "posted") await postPosted(p.context, slotId);
      else await postSkipped(p.context, slotId);
    } catch (error) {
      toastLine(
        p,
        kind === "posted"
          ? `Could not mark it as posted (${describeError(error)}). Nothing changed; try again.`
          : `Could not skip it (${describeError(error)}). Nothing changed; try again.`,
        "error",
      );
      return;
    }
    dropPost(slotId);
    forgetHistory(item.id);
    toast(
      p,
      kind === "posted" ? `Marked as posted: ${item.title}` : `Skipped this post: ${item.title}`,
      async () => {
        planBusy.add(slotId);
        try {
          await reopenSocial(p.context, slotId);
          dropPost(slotId);
          forgetHistory(item.id);
        } finally {
          planBusy.delete(slotId);
        }
      },
      UNDO_MS,
    );
    await reloadPanel(p);
  } finally {
    setBusy(slotId, false);
  }
}

/**
 * Skip post for the opened row's toolbar: the card's own skip, after a fresh read, with an Undo. A closed row has
 * no Skip post (one click on a row you did not open would be too easy); neither has Posted.
 */
export function skipButton(p: Panel, item: AttentionItem): HTMLButtonElement {
  const node = barButton("Skip post", skipIcon(), "Skip this post: it leaves the Social plan and stops counting toward the week. Undo stays for 10 seconds.", `${item.id}:skip`);
  node.classList.add("nt-dismiss");
  registerPlanButton(item.slot as string, node);
  node.addEventListener("click", () => {
    if (node.disabled) return;
    void changePlan(p, item, "skip");
  });
  return node;
}

/**
 * The closed row's action: Copy text. The draft is read fresh (a closed row has not loaded the post), then copied.
 */
export function closedCopyButton(p: Panel, item: AttentionItem): HTMLButtonElement {
  const node = barButton("Copy text", copyIcon(), "Copy the draft", `${item.id}:copy`);
  node.addEventListener("click", () => {
    if (node.disabled) return;
    node.disabled = true;
    void (async () => {
      try {
        await loadPost(p.context, item.slot as string, itemSig(item), true);
        const state = postOf(item.slot as string);
        const text = state.status === "ready" ? state.text : null;
        if (state.status === "failed") {
          sayInRow(p, item.id, `The post could not be loaded (${describeError(state.error)}). Open the row and try again.`, "error", p.root);
        } else if (text === null) {
          sayInRow(p, item.id, "No draft yet. Open the row, then Open the post to write it.", "info", p.root);
        } else {
          setLabel(node, await copyDraft(text));
        }
      } finally {
        node.disabled = false;
      }
    })();
  });
  return node;
}

export interface SocialRow {
  /** The post's text section (heading, draft, Show all, count and state). Its own element, so a layout can move it. */
  post: HTMLElement;
  /** The picture section (heading, the picture, Copy picture and Download under it, its pixel size). */
  picture: HTMLElement;
  /** Copy text, Open on LinkedIn, Posted: the toolbar's first three buttons (the picture buttons sit under the picture). */
  buttons: HTMLButtonElement[];
  /** What the hint under the post says: Skip post and Snooze, since a Social row has no Dismiss. */
  hint: string;
}

/** The opened Social row's post block and the card's actions. Loads the post when it first draws. */
export function socialRow(p: Panel, item: AttentionItem): SocialRow {
  const { context } = p;
  const slotId = item.slot as string;
  // Separate pieces with their own classes (nt-post the text, nt-picbox the picture), so a layout can move each.
  const post = el("section", { class: "nt-post", "aria-label": "The post" });
  const picture = el("section", { class: "nt-picbox", "aria-label": "The picture" });
  const line = (text: string, tone: "error" | "info" | "ok"): void => sayInRow(p, item.id, text, tone, lineHost(p, item.id));

  const copy = barButton("Copy text", copyIcon(), "Copy the draft", `${item.id}:copy`);
  const open = barButton("Open on LinkedIn", externalLinkIcon(), "Copy the draft, then open the site", `${item.id}:open`);
  const posted = barButton("Posted", checkIcon(), "Mark this post as posted: the Social card counts it too", `${item.id}:posted`);
  const copyPic = barButton("Copy picture", photoIcon(), "Copy the picture to the clipboard", `${item.id}:copy-picture`);
  const savePic = barButton("Download", downloadIcon(), "Save the picture as a file", `${item.id}:download-picture`);
  for (const node of [copyPic, savePic]) node.classList.add("nt-sm");
  const buttons = [copy, open, posted];
  for (const node of [...buttons, copyPic, savePic]) node.dataset.social = "1";
  const size = el("span", { class: "meta nt-picsize" });
  const made = socialPictureFor(
    context,
    item,
    () => slotFor(item, postOf(slotId)),
    (w, h) => {
      size.textContent = `${w} \u00d7 ${h}`;
    },
    () => syncPicture(),
  );
  const frame = made?.frame ?? null;
  registerPlanButton(slotId, posted);

  let expanded = false;

  /** The picture buttons and the size line follow the picture's state (also when the picture lands, after the block drew). */
  function syncPicture(): void {
    const pic = visualState(slotFor(item, postOf(slotId)));
    // Both buttons are there from the first draw when the post has a picture, disabled while it loads, so
    // they never pop in (2026-11-16); with no picture (or a failed one) they are not there at all.
    const has = pic === "ready" || pic === "loading";
    copyPic.hidden = !(has && canCopyPicture());
    savePic.hidden = !has;
    copyPic.disabled = pic !== "ready";
    savePic.disabled = pic !== "ready";
    if (pic === "loading") size.textContent = "Loading picture";
    else if (pic !== "ready" && size.textContent !== "") size.textContent = "";
    else if (pic === "ready" && size.textContent === "Loading picture") size.textContent = "";
  }

  const paint = (): void => {
    const state = postOf(slotId);
    const ready = state.status === "ready" ? state : null;
    const text = ready?.text ?? null;
    // The buttons that need the draft or the picture wait for them; the others always work.
    copy.disabled = text === null;
    copy.title = text === null ? "No draft yet: open the post to write it" : "Copy the draft";
    open.disabled = text === null;
    open.title = text === null ? "No draft yet: open the post to write it" : "Copy the draft, then open the site";
    if (ready) setLabel(open, `Open on ${ready.detail.platform === "linkedin" ? "LinkedIn" : "YouTube"}`);
    syncPicture();

    const children: Array<HTMLElement | null> = [el("h3", { class: "nt-sec-h" }, "Post")];
    if (state.status === "loading") {
      children.push(el("p", { class: "nt-post-note meta", role: "status" }, "Loading the post..."));
    } else if (state.status === "failed") {
      children.push(
        el("p", { class: "nt-post-note nt-post-bad", role: "status" }, `The post could not be loaded (${describeError(state.error)}). Open the post to see it, or close this item and open it again.`),
      );
    } else if (text === null) {
      children.push(el("p", { class: "nt-post-note meta" }, "No draft yet. Open the post to write it."));
    } else {
      if (ready?.stale) {
        const at = readAt.get(slotId)?.at;
        children.push(el("p", { class: "nt-post-note meta nt-post-stale", role: "status" }, `Could not read the newest draft; showing the one from ${at ? clockOf(at) : "earlier"}.`));
      }
      const lines = text.split("\n").length;
      const long = lines > 10 || text.length > 900;
      children.push(el("div", { class: `nt-post-text${long && !expanded ? " is-collapsed" : ""}` }, text));
      const foot = el("div", { class: "nt-post-foot" });
      if (long) {
        const toggle = el("button", { type: "button", class: "linkish nt-post-more", "aria-expanded": String(expanded) }, expanded ? "Show less" : "Show all");
        toggle.addEventListener("click", () => {
          expanded = !expanded;
          paint();
        });
        foot.append(toggle);
      }
      const draftState = ready?.detail.draft?.edited ? "Edited draft" : "Draft ready";
      foot.append(el("span", { class: "meta nt-post-meta" }, `${[...text].length} characters \u00b7 ${draftState}`));
      children.push(foot);
    }
    post.replaceChildren(...children.filter((n): n is HTMLElement => n !== null));
    // The picture's identity can be only on the slot's detail: paint it again now that the detail may have it.
    made?.repaint();
  };

  const asOf = (at: number): string => `Copied the draft as of ${clockOf(at)}; reopen the row if you just edited it.`;

  /**
   * Read the slot again (forced). Resolves to the draft's text, and to the last good draft when the read failed (the
   * post keeps it, marked stale; `held` says so), so a clipboard write inside the click still has text to take. Rejects
   * only when there is no draft at all, or the draft is gone.
   */
  const readFresh = (): Promise<{ text: string; held: boolean }> =>
    loadPost(context, slotId, itemSig(item), true).then(() => {
      paint();
      const state = postOf(slotId);
      if (state.status === "failed") throw state.error;
      if (state.status !== "ready" || state.text === null) throw new Error("There is no draft for this post any more");
      return { text: state.text, held: state.stale === true };
    });

  copy.addEventListener("click", () => {
    const held = heldDraft(slotId);
    if (held === null) {
      line("There is no draft to copy yet. Open the post to write it.", "info");
      return;
    }
    // Inside the tap, before any await: the slot is read again and the clipboard write starts with that read still
    // pending (a ClipboardItem takes a promise), so what is copied is the draft as it is now, edits in the Social card included.
    const fresh = canCopyFresh() ? readFresh() : null;
    fresh?.catch(() => undefined);
    const writing = fresh ? startFreshCopy(fresh.then((read) => read.text)) : null;
    if (writing === null) {
      // No promise clipboard here: the held text is copied inside the tap, and the wording says how old it is.
      void copyDraft(held.text).then((word) => {
        setLabel(copy, word);
        if (word === "Copy failed") line("The clipboard refused the copy. Open the post and select the text there.", "error");
        else line(asOf(held.at), "info");
      });
      return;
    }
    void (async () => {
      const wrote = await writing;
      let read: { text: string; held: boolean } | null = null;
      let readError: unknown = null;
      try {
        read = await (fresh as Promise<{ text: string; held: boolean }>);
      } catch (error) {
        readError = error;
      }
      if (wrote) {
        setLabel(copy, "Copied");
        // The read failed and the write took the last good draft: say so, with its time.
        if (read?.held) line(asOf(heldDraft(slotId)?.at ?? held.at), "info");
        return;
      }
      // The browser refused the write: the held text is the last try (outside the gesture), and the wording says so.
      const word = await copyDraft(held.text);
      setLabel(copy, word);
      if (word === "Copy failed") {
        line(readError !== null ? `Could not read the post again (${describeError(readError)}), and the clipboard refused the copy. Open the post and select the text there.` : "The clipboard refused the copy. Open the post and select the text there.", "error");
      } else {
        line(asOf(held.at), "info");
      }
    })();
  });

  open.addEventListener("click", () => {
    const state = postOf(slotId);
    const held = heldDraft(slotId);
    if (state.status !== "ready" || state.text === null || held === null) {
      line("There is no draft to open yet. Open the post to write it.", "info");
      return;
    }
    const slot = slotFor(item, state);
    const view = state.view;
    if (!view && state.detail.platform !== "linkedin") {
      line("The Social card could not be read, so the site link is unknown. Copy the text and paste it in the site.", "error");
      return;
    }
    if (openNeedsEditor(slot, state.text)) {
      line("The picture is still loading. Try again in a moment, or open the post.", "info");
      return;
    }
    const show = (result: Awaited<ReturnType<typeof openPost>>, note: string): void => {
      if (result.label) setLabel(open, result.label);
      if (result.note) line(result.note.text, result.note.tone);
      else if (note) line(note, "info");
    };
    const failed = (error: unknown): void => line(`That did not go through (${describeError(error)}).`, "error");
    const sharing = usesShareSheet(slot) && shareAvailable() !== false;
    // The share sheet needs the tap's permission, which a read would outlast: it takes the held text, and says how old it is.
    // Everywhere else the slot is read again inside the tap, the clipboard write starts with that read pending, and the
    // site opens (with the fresh text) once the copy has settled, as the card does.
    const fresh = !sharing && canCopyFresh() ? readFresh() : null;
    fresh?.catch(() => undefined);
    const writing = fresh ? startFreshCopy(fresh.then((read) => read.text)) : null;
    if (writing === null) {
      const note = asOf(held.at).replace("Copied the draft", "Used the draft");
      void openPost(context, slot, view, held.text).then((result) => show(result, note), failed);
      return;
    }
    void (async () => {
      let text = held.text;
      let note = "";
      try {
        const read = await (fresh as Promise<{ text: string; held: boolean }>);
        text = read.text;
        // The read failed and the write took the last good draft: the site opens with that one, and the line says so.
        if (read.held) note = asOf(heldDraft(slotId)?.at ?? held.at).replace("Copied the draft", "Copied and opened the draft");
      } catch {
        note = asOf(held.at).replace("Copied the draft", "Used the draft");
      }
      const result = await openPost(context, slot, view, text, writing);
      show(result, note);
    })().catch(failed);
  });

  posted.addEventListener("click", () => {
    if (posted.disabled) return;
    void changePlan(p, item, "posted");
  });

  const nameOf = (): string => {
    const state = postOf(slotId);
    const d = state.status === "ready" ? state.detail : null;
    return d?.title ?? d?.tool ?? slotId;
  };

  copyPic.addEventListener("click", () => {
    const blob = loadedVisual(slotFor(item, postOf(slotId)));
    if (!blob) return;
    // Started here, inside the click, before any await (the PNG conversion runs inside the clipboard write).
    void copyPicture(blob).then((failure) => {
      setLabel(copyPic, failure === null ? "Picture copied" : "Copy failed");
      if (failure !== null) line(`Could not copy the picture (${failure}).${/download/i.test(failure) ? "" : " Use Download instead."}`, "error");
    });
  });

  savePic.addEventListener("click", () => {
    const blob = loadedVisual(slotFor(item, postOf(slotId)));
    if (!blob) return;
    if (downloadPicture(blob, nameOf())) setLabel(savePic, "Downloaded");
    else line("This page cannot save the picture here. Open the post and save it from there.", "error");
  });

  picture.append(el("h3", { class: "nt-sec-h" }, "Picture"), frame ?? "", el("div", { class: "nt-picacts" }, copyPic, savePic, size));
  paint();
  // Read again every time the row draws; the copy held (if any) stays on show until the fresh one lands.
  void loadPost(context, slotId, itemSig(item)).then(paint);
  return {
    post,
    picture,
    buttons,
    hint: "Skip post takes it out of the Social plan. Snooze hides this row for a while.",
  };
}
