// What the opened Needs you row and the table's folds gained in D2 phase 2 (2026-11-16):
// Snooze (in the opened row and the closed row's menu), the Snoozed fold, the history box, a
// Social post's picture and the Age column's text. Drawing only; needsSelect.ts holds the words
// and the server decides everything (snooze, wake and history are routes, not browser state).

import type { ExtensionContext } from "@omnigent/extension-sdk";
import {
  type AttentionEvent,
  type AttentionHistory,
  type AttentionItem,
  describeError,
  fetchAttentionHistory,
  snoozeAttention,
  type SnoozeChoice,
  wakeAttention,
} from "./api";
import { type Panel, originOf, part, reloadPanel } from "./attention";
import { rowMenu } from "./cards";
import { button, el, notice, replaceChildren } from "./dom";
import { sayInRow } from "./rowMessages";
import { chevronIcon, clockIcon, restoreIcon } from "./icons";
import { forgetHistory, HISTORY_TTL_MS, histories, resetHistories } from "./historyCache";
import { sinceText } from "./needsModel";
import { SNOOZE_CHOICES, snoozable, snoozedLine, untilText } from "./needsSelect";
import { loadVisual, visualState, loadedVisual, showPictureUrl } from "./socialVisual";
import type { SocialSlot } from "./api";

// ── Age ─────────────────────────────────────────────────────────────────────

/** "3 d": how long the item has been on the list, from the server's first-seen time; "" when unknown. */
export function ageText(item: Pick<AttentionItem, "first_seen">, now = Date.now()): string {
  const at = item.first_seen ? Date.parse(item.first_seen) : Number.NaN;
  return Number.isNaN(at) ? "" : sinceText(at, now);
}

// ── Snooze ──────────────────────────────────────────────────────────────────

let toastTimer: ReturnType<typeof setTimeout> | null = null;

/** A line in the panel's toast area, with an optional Undo. It stays *ms* (8 seconds unless said). */
export function toast(p: Panel, text: string, undo?: () => Promise<void>, ms = 8_000): void {
  const host = part(p, ".attn-toast");
  const clear = (): void => host.replaceChildren();
  if (toastTimer) clearTimeout(toastTimer);
  const children: HTMLElement[] = [el("span", { class: "attn-undo-text" }, text)];
  if (undo) {
    children.push(
      button("Undo", {
        title: "Bring this item back now",
        onClick: async () => {
          try {
            await undo();
            clear();
            await reloadPanel(p);
          } catch (error) {
            replaceChildren(host, [notice(`Could not undo (${describeError(error)}).`, "error")]);
          }
        },
      }),
    );
  }
  replaceChildren(host, [el("div", { class: "attn-undo", role: "status" }, ...children)]);
  toastTimer = setTimeout(clear, ms);
}

/** A plain result line (an error or a note) in the page's message area, which stays when a row leaves. It stays *ms*. */
export function toastLine(p: Panel, text: string, tone: "error" | "info" | "ok", ms = 8_000): void {
  const host = part(p, ".attn-toast");
  if (toastTimer) clearTimeout(toastTimer);
  replaceChildren(host, [notice(text, tone)]);
  toastTimer = setTimeout(() => host.replaceChildren(), ms);
}

/** Snooze one item and say until when; Undo wakes it. */
export async function snoozeItem(p: Panel, item: AttentionItem, choice: SnoozeChoice, anchor: HTMLElement): Promise<void> {
  try {
    const made = await snoozeAttention(p.context, item.id, choice);
    forgetHistory(item.id);
    toast(p, `${snoozedLine(made.until)}: ${item.title}`, async () => {
      await wakeAttention(p.context, item.id);
      forgetHistory(item.id);
    });
    await reloadPanel(p);
  } catch (error) {
    sayInRow(p, item.id, `Could not snooze (${describeError(error)}).`, "error", anchor.closest<HTMLElement>(".nt-panel-main, .nt-sheet-msg, .attn-row") ?? p.root);
  }
}

/** The opened row's Snooze: three choices in one labelled group. Null when the item cannot be snoozed. */
export function snoozeBlock(p: Panel, item: AttentionItem): HTMLElement | null {
  if (!snoozable(item)) return null;
  const group = el("div", { class: "nt-snooze", role: "group", "aria-label": `Snooze ${item.title}` });
  group.append(el("span", { class: "nt-snooze-k" }, clockIcon(), "Snooze"));
  for (const choice of SNOOZE_CHOICES) {
    const b = button(choice.label, { title: `${choice.title}. It comes back sooner if it gets worse.`, onClick: () => void snoozeItem(p, item, choice.id, group) });
    b.dataset.focus = `${item.id}:snooze:${choice.id}`;
    b.setAttribute("aria-label", `Snooze ${item.title} until ${choice.label}`);
    group.append(b);
  }
  return group;
}

/** The closed row's Snooze menu (the opened row hides it and shows `snoozeBlock`). Null when it cannot be snoozed. */
export function snoozeMenu(p: Panel, item: AttentionItem): HTMLElement | null {
  if (!snoozable(item)) return null;
  const menu = rowMenu(
    { title: item.title },
    SNOOZE_CHOICES.map((choice) => button(choice.label, { title: choice.title, onClick: () => snoozeItem(p, item, choice.id, p.root) })),
    // The word is a real label in the DOM; style.css shows it on a phone and keeps the icon alone elsewhere.
    {
      icon: el("span", { class: "nt-snooze-face" }, clockIcon(), el("span", { class: "nt-label nt-snooze-word" }, "Snooze")),
      label: `Snooze ${item.title}`,
      title: "Snooze: hide it for a while",
      className: "btn-icon attn-btn",
    },
  );
  menu.classList.add("nt-snooze-menu");
  return menu;
}

/**
 * The opened row's Snooze as a dropdown button in the toolbar (layout B, 2026-11-16): the word
 * "Snooze" and a caret, opening the same three choices in a WAI-ARIA menu button (`rowMenu`:
 * Enter, Space or the arrows open it, the arrows move, Escape closes and returns the focus to the
 * button). Null when the item cannot be snoozed.
 */
export function snoozeDropdown(p: Panel, item: AttentionItem): HTMLElement | null {
  if (!snoozable(item)) return null;
  const face = el("span", { class: "nt-snooze-face" }, clockIcon(), el("span", { class: "nt-label" }, "Snooze"), chevronIcon());
  const menu = rowMenu(
    { title: item.title },
    SNOOZE_CHOICES.map((choice) =>
      button(choice.label, {
        title: `${choice.title}. It comes back sooner if it gets worse.`,
        onClick: () => snoozeItem(p, item, choice.id, p.root),
      }),
    ),
    { icon: face, label: `Snooze ${item.title}`, title: "Snooze: hide it for a while. It comes back sooner if it gets worse.", className: "nt-labelled nt-snooze-btn" },
  );
  menu.classList.add("nt-snooze-drop");
  menu.querySelector<HTMLElement>(".btn-menu")!.dataset.focus = `${item.id}:snooze`;
  return menu;
}

let snoozedOpen = false;

/** The "Snoozed (N)" fold under the table, each row with Wake. */
export function snoozedNode(p: Panel, items: AttentionItem[]): HTMLElement | null {
  if (!items.length) return null;
  const listId = "attn-snoozed-list";
  const head = el(
    "button",
    {
      type: "button",
      class: "attn-dismissed-head attn-snoozed-head",
      "aria-expanded": String(snoozedOpen),
      "aria-controls": listId,
      title: "Items you snoozed. Wake brings one back now.",
    },
    chevronIcon(),
    `Snoozed (${items.length})`,
  );
  const list = el(
    "ul",
    { class: "attn-dismissed-list attn-snoozed-list", id: listId, hidden: !snoozedOpen },
    ...items.map((item) => {
      const wake = el("button", {
        type: "button",
        class: "attn-review-all attn-restore",
        title: "Put this item back in Needs you now",
        "aria-label": `Wake ${item.title}`,
      });
      wake.append(restoreIcon(), el("span", {}, "Wake"));
      const row: HTMLElement = el(
        "li",
        { class: "attn-dismissed-row", "data-id": item.id },
        el("span", { class: "attn-title" }, item.title),
        wake,
        el("span", { class: "attn-meta" }, [originOf(item), item.snoozed ? `back ${untilText(item.snoozed.until)}` : ""].filter(Boolean).join(" · ")),
      );
      wake.addEventListener("click", async () => {
        if (wake.disabled) return;
        wake.disabled = true;
        try {
          await wakeAttention(p.context, item.id);
          forgetHistory(item.id);
          await reloadPanel(p);
        } catch (error) {
          sayInRow(p, item.id, `Could not wake (${describeError(error)}).`, "error", row);
        } finally {
          wake.disabled = false;
        }
      });
      return row;
    }),
  );
  head.addEventListener("click", () => {
    snoozedOpen = !snoozedOpen;
    head.setAttribute("aria-expanded", String(snoozedOpen));
    list.hidden = !snoozedOpen;
  });
  return el("div", { class: "attn-dismissed attn-snoozed" }, head, list);
}

// ── the history box ─────────────────────────────────────────────────────────

export { resetHistories };

const EVENT_WORDS: Record<AttentionEvent["what"], string> = {
  "first-seen": "First seen",
  dismissed: "Dismissed",
  snoozed: "Snoozed",
  woke: "Woke",
  restored: "Restored",
  "chat-started": "Chat started",
  done: "No longer reported",
  back: "Reported again",
  assigned: "Assigned",
  unassigned: "Unassigned",
};

/** "alice": a login without its mail domain. */
const nameOf = (login: string): string => login.split("@")[0] || login;

/** One history row's words before "by <who>": "Assigned to bob", "Unassigned", "Unassigned (the item left the list)". */
export function eventWords(event: Pick<AttentionEvent, "what" | "by" | "to">): string {
  if (event.what === "assigned") return event.to ? `Assigned to ${nameOf(event.to)}` : "Assigned";
  if (event.what === "unassigned" && event.by === "system") return "Unassigned (the item left the list)";
  return EVENT_WORDS[event.what];
}

/** "Mon, Nov 16, 9:04 AM": a time in the viewer's own clock. */
export const whenText = (iso: string): string => {
  const at = new Date(iso);
  return Number.isNaN(at.getTime())
    ? ""
    : at.toLocaleString([], { weekday: "short", month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
};

function drawHistory(box: HTMLElement, value: AttentionHistory | { error: unknown } | null): void {
  const list = box.querySelector<HTMLElement>(".nt-hist-body");
  if (!list) return;
  if (!value) {
    list.replaceChildren(el("p", { class: "meta" }, "Loading history..."));
    return;
  }
  if ("error" in value) {
    list.replaceChildren(el("p", { class: "meta nt-hist-bad" }, `History could not be read (${describeError(value.error)}).`));
    return;
  }
  if (value.unknown) {
    list.replaceChildren(el("p", { class: "meta nt-hist-bad" }, "History could not be read: the server's activity log is unreadable, so this is not an empty history."));
    return;
  }
  const rows = value.events.map((event) =>
    el(
      "li",
      { class: "nt-hist-row", "data-what": event.what },
      el("span", { class: "nt-hist-what" }, eventWords(event)),
      event.by && event.by !== "system" ? el("span", { class: "nt-hist-by" }, ` by ${nameOf(event.by)}`) : null,
      el("span", { class: "nt-hist-when meta" }, whenText(event.at)),
    ),
  );
  list.replaceChildren(
    rows.length ? el("ol", { class: "nt-hist-list" }, ...rows) : el("p", { class: "meta" }, "Nothing recorded for this item yet."),
  );
}

/** The history box of the opened row: loaded from the server when the row opens, redrawn in place. */
export function historyBox(context: ExtensionContext, item: Pick<AttentionItem, "id" | "title">, now = Date.now()): HTMLElement {
  const heading = `nt-hist-h-${item.id.replace(/[^A-Za-z0-9_-]+/g, "-")}`;
  const box = el(
    "aside",
    { class: "nt-panel-side nt-hist", "aria-labelledby": heading },
    el("h3", { class: "nt-hist-h", id: heading }, "History"),
    el("div", { class: "nt-hist-body", "aria-live": "polite" }),
  );
  const held = histories.get(item.id);
  drawHistory(box, held?.value ?? null);
  if (held && now - held.at < HISTORY_TTL_MS) return box;
  fetchAttentionHistory(context, item.id).then(
    (value) => {
      histories.set(item.id, { at: Date.now(), value });
      if (box.isConnected || !held) drawHistory(box, value);
    },
    (error: unknown) => {
      histories.set(item.id, { at: Date.now(), value: { error } });
      drawHistory(box, { error });
    },
  );
  return box;
}

// ── a Social post's picture ─────────────────────────────────────────────────

/**
 * The picture of a Social row, from the card's own route (`socialVisual.ts`): only the data: and
 * blob: path the card uses, never a link or an image source built from item text. A post with no
 * picture says so.
 */
export function socialPicture(context: ExtensionContext, item: AttentionItem, onSize?: (width: number, height: number) => void): HTMLElement | null {
  return socialPictureFor(context, item, () => ({ id: item.slot as string, visual: item.visual ?? undefined }) as unknown as SocialSlot, onSize)?.frame ?? null;
}

/**
 * The same picture for a slot that *getSlot* names each time it paints: the picture's identity can be only on the
 * slot's own detail (the row's item has none), so the row hands over the detail's slot once it has loaded and calls
 * `repaint`. It loads the picture whenever the slot has one that is not loaded yet.
 */
export function socialPictureFor(
  context: ExtensionContext,
  item: AttentionItem,
  getSlot: () => SocialSlot,
  onSize?: (width: number, height: number) => void,
  /** Called after every paint of the frame, so what sits beside it (the picture buttons) follows its state. */
  onPaint?: () => void,
): { frame: HTMLElement; repaint: () => void } | null {
  if (item.source !== "social" || !item.slot) return null;
  const frame = el("figure", { class: "nt-pic", "data-state": "loading" });
  let shownBlob: Blob | null = null;
  let shownUrl: string | null = null;
  let asked = "";
  const paint = (): void => {
    paintFrame();
    onPaint?.();
  };
  const paintFrame = (): void => {
    const slot = getSlot();
    const state = visualState(slot);
    frame.dataset.state = state;
    // The frame is a fixed-height stage in style.css (the cap plus padding and borders) while it loads, once the
    // picture shows and if it fails (until the "No picture yet" line replaces it), so nothing moves when the
    // picture lands. The picture is contained and centred in it, never cropped.
    if (state === "none") {
      replaceChildren(frame, [el("p", { class: "nt-pic-note meta" }, item.visual_note || "No picture yet")]);
      return;
    }
    if (state === "failed") {
      replaceChildren(frame, [el("p", { class: "nt-pic-note meta" }, "The picture could not be loaded. Open the post to try again.")]);
      return;
    }
    const blob = loadedVisual(slot);
    if (state === "ready" && blob) {
      // One display URL per picture shown here; a replaced picture revokes the one before it.
      if (blob !== shownBlob || !shownUrl) {
        shownUrl = showPictureUrl(item.slot as string, blob);
        shownBlob = blob;
      }
    }
    if (state === "ready" && shownUrl) {
      const img = el("img", { class: "nt-pic-img", alt: `Picture for ${item.title}`, src: shownUrl });
      // The picture's own pixel size, once it has decoded.
      const report = (): void => {
        if (onSize && img.naturalWidth > 0 && img.naturalHeight > 0) onSize(img.naturalWidth, img.naturalHeight);
      };
      img.addEventListener("load", report);
      if (img.complete) report();
      replaceChildren(frame, [img]);
      return;
    }
    replaceChildren(frame, [el("p", { class: "nt-pic-note meta" }, state === "ready" ? "The picture is ready but this page cannot show it here." : "Loading picture...")]);
    // Loading: start it once per picture identity, and paint again when it lands.
    const key = `${slot.id}|${slot.visual?.key ?? ""}`;
    if (asked !== key) {
      asked = key;
      void loadVisual(context, slot).then(paint);
    }
  };
  paint();
  return { frame, repaint: paint };
}

/** Scroll to and focus a post on the Social card once it is on the page (the card draws its posts after it loads). */
export function showPost(slot: string, tries = 20): void {
  const found = [...document.querySelectorAll<HTMLElement>("[data-slot]")].find((n) => n.dataset.slot === slot);
  if (found) {
    found.scrollIntoView?.({ block: "center", behavior: "smooth" });
    found.focus?.({ preventScroll: true });
    return;
  }
  if (tries > 0) setTimeout(() => showPost(slot, tries - 1), 150);
}
