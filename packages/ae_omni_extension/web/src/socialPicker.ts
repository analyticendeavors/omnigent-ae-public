// The picture picker for a tool or expert post (2026-11-15). The weekly drafter makes 4 or 5
// candidates per post (branded diagrams, generated pictures and, for a clean product page, a
// screenshot), ranks them, and the server preselects the first. The editor shows the chosen one
// full width with every candidate as a small tile below it, in rank order, plus "None" (layout A
// on the 2026-11-15 vote). One tap picks; the pick is saved at once and the share sheet takes it.
//
// The tiles are a WAI-ARIA radio group: one tab stop (the checked tile), arrow keys move the
// pick, Space and Enter pick the focused tile. A pick shows at once; the full picture and the
// save follow once the picks stop for SETTLE_MS, so arrowing past tiles loads and writes nothing.
// Saves go one at a time and only the newest waiting pick is sent; a refused save goes back to
// the last pick the server confirmed (2026-11-15 review).

import type { ExtensionContext } from "@omnigent/extension-sdk";
import { chooseSocialVisual, describeError, type SocialMadeVisual, type SocialSlot } from "./api";
import { el, replaceChildren } from "./dom";
import { loadPreview, loadVisual, visualUrl } from "./socialVisual";

/** How long the picks must stop before the full picture loads and the pick is saved. */
export const SETTLE_MS = 350;

/** Where the picture goes, said under it (the editor's single-picture row says the same). */
export const HINT_SHEET = "Goes into the share sheet with the text. Long-press to save it.";
export const HINT_DESKTOP = "Long-press or right-click to save it, then attach it in LinkedIn.";

export interface PickerHooks {
  context: ExtensionContext;
  /** The post; its `visual`, `visual_choice` and `visual_choice_by` follow the pick. */
  slot: SocialSlot;
  /** The share sheet is the way out on this device (the hint says long-press, not right-click). */
  sheet: boolean;
  /** The pick changed and settled (or went back): the editor waits for the new picture before Share. */
  picked: () => void;
  /** A pick is showing but not settled yet: the editor holds Share until it is. */
  settling: () => void;
  /** The server saved a pick: the card reads again so its other views share the same picture. */
  saved: () => void;
  /** The server refused the pick; it has gone back already. */
  failed: (message: string) => void;
}

const KIND: Record<SocialMadeVisual["kind"], string> = {
  diagram: "Diagram",
  picture: "Picture",
  screenshot: "Screenshot",
};

/** The words for the current pick: "1 of 5 · Screenshot: A real skin · the drafter's pick". */
export function pickLabel(slot: SocialSlot): string {
  const made = slot.visuals ?? [];
  if (slot.visual_choice === "none") return "No picture: this post shares the text only.";
  const at = made.findIndex((m) => m.id === slot.visual_choice);
  if (at < 0) return "";
  const who = slot.visual_choice_by === "reid" ? "your pick" : "the drafter's pick";
  return `${at + 1} of ${made.length} · ${KIND[made[at].kind]}: ${made[at].label} · ${who}`;
}

function apply(slot: SocialSlot, choice: string, by: SocialSlot["visual_choice_by"] = "reid"): void {
  slot.visual_choice = choice;
  slot.visual_choice_by = by;
  const made = (slot.visuals ?? []).find((m) => m.id === choice);
  if (made) {
    slot.visual = { kind: "made", key: `${made.id}:${made.key}` };
    delete slot.visual_note;
  } else {
    delete slot.visual;
    slot.visual_note = "No picture: you picked none.";
  }
}

export function buildPicker(hooks: PickerHooks): HTMLElement {
  const { context, slot } = hooks;
  const made = slot.visuals ?? [];
  const main = el("div", { class: "sv-pick-main" });
  const label = el("p", { class: "meta sv-pick-label", "aria-live": "polite" });
  const strip = el("div", { class: "sv-pick-strip", role: "radiogroup", "aria-label": "Picture for this post" });
  const hint = el(
    "p",
    { class: "meta sv-pick-hint" },
    hooks.sheet ? HINT_SHEET : HINT_DESKTOP,
  );

  const tiles: HTMLButtonElement[] = [];
  const choices = [...made.map((m) => m.id), "none"];

  const showMain = () => {
    if (!slot.visual) {
      replaceChildren(main, [el("div", { class: "sv-pick-img is-none" }, "No picture")]);
      return;
    }
    const key = slot.visual.key;
    replaceChildren(main, [el("div", { class: "sv-pick-img is-loading" }, "Loading image...")]);
    void loadVisual(context, slot).then((blob) => {
      if (slot.visual?.key !== key) return; // picked again while this one loaded
      const url = blob ? visualUrl(blob) : null;
      replaceChildren(main, [
        url
          ? el("img", { class: "sv-pick-img", src: url, alt: `The picture shared with this post: ${pickLabel(slot)}` })
          : el("div", { class: "sv-pick-img is-failed" }, blob ? "Saved for sharing" : "Image unavailable"),
      ]);
    });
  };

  const showChecked = () => {
    for (const [i, tile] of tiles.entries()) {
      const on = choices[i] === slot.visual_choice;
      tile.setAttribute("aria-checked", on ? "true" : "false");
      tile.tabIndex = on ? 0 : -1;
      tile.classList.toggle("is-on", on);
    }
    if (!tiles.some((t) => t.tabIndex === 0) && tiles[0]) tiles[0].tabIndex = 0;
    label.textContent = pickLabel(slot);
  };

  // The last pick the server confirmed (the drafter's, at first), and the save queue.
  let confirmed = { choice: slot.visual_choice ?? "c1", by: slot.visual_choice_by };
  let saving = false;
  let waiting: string | null = null;
  let timer: ReturnType<typeof setTimeout> | undefined;

  const save = async () => {
    if (saving) return;
    saving = true;
    while (waiting !== null) {
      const next = waiting;
      waiting = null;
      try {
        await chooseSocialVisual(context, slot.id, next);
        confirmed = { choice: next, by: "reid" };
        if (waiting === null) hooks.saved();
      } catch (error) {
        if (waiting !== null) continue; // a newer pick is queued; it decides
        if (slot.visual_choice !== next) continue; // picked again while this one was saving
        apply(slot, confirmed.choice, confirmed.by);
        showChecked();
        showMain();
        hooks.picked();
        hooks.failed(`Could not save the picture pick (${describeError(error)}). Try again.`);
      }
    }
    saving = false;
  };

  const choose = (choice: string) => {
    if (choice === slot.visual_choice) return;
    apply(slot, choice);
    showChecked();
    hooks.settling();
    clearTimeout(timer);
    timer = setTimeout(() => {
      showMain();
      hooks.picked();
      waiting = slot.visual_choice ?? choice;
      void save();
    }, SETTLE_MS);
  };

  choices.forEach((choice, i) => {
    const entry = made[i];
    const name = entry ? `${i + 1} of ${made.length}: ${KIND[entry.kind]}, ${entry.label}` : "No picture";
    const tile = el("button", {
      type: "button",
      role: "radio",
      class: `sv-pick-tile${entry ? "" : " is-none"}`,
      "aria-label": name,
      title: name,
    });
    if (entry) {
      tile.append(el("span", { class: "sv-pick-rank", "aria-hidden": "true" }, String(i + 1)));
      void loadPreview(context, slot.id, entry).then((blob) => {
        const url = blob ? visualUrl(blob) : null;
        if (url) tile.prepend(el("img", { src: url, alt: "" }));
      });
    } else {
      tile.append(el("span", {}, "None"));
    }
    tile.addEventListener("click", () => choose(choice));
    tile.addEventListener("keydown", (event) => {
      const step = { ArrowRight: 1, ArrowDown: 1, ArrowLeft: -1, ArrowUp: -1 }[event.key];
      if (step === undefined) return;
      event.preventDefault();
      const next = (i + step + tiles.length) % tiles.length;
      tiles[next].focus();
      choose(choices[next]);
    });
    tiles.push(tile);
    strip.append(tile);
  });

  showChecked();
  showMain();
  return el("div", { class: "sv-pick" }, main, strip, label, hint);
}
