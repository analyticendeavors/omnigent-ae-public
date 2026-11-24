// Select and act on many (2026-11-16, D2 phase 2): the checkboxes in the Needs you table and the
// bulk bar that appears while anything is selected.
//
// Every row has a real checkbox with a label that names its item; each source and each area has a
// select-all checkbox that shows `indeterminate` when only some of its rows are picked. The bar is
// a labelled region: "N selected", Dismiss, Snooze (three choices), Review N (only for someone who
// can start chats) and Clear. Each action answers per item ("Dismissed 7, 2 could not be
// dismissed", with the reasons), and the ones that failed stay selected so they can be tried again.
// Selection itself lives in needsSelect.ts (ids, so it survives a redraw and drops what vanished).

import { type AttentionItem, type BulkAnswer, bulkAttention, describeError, type SnoozeChoice } from "./api";
import { type Panel, reloadPanel, reviewMany } from "./attention";
import { button, el, replaceChildren } from "./dom";
import { forgetHistory } from "./historyCache";
import { clockIcon, closeIcon, eyeOffIcon, listCheckIcon } from "./icons";
import {
  bulkWords,
  chunks,
  clearPicked,
  groupState,
  isPicked,
  joinAnswers,
  onSelection,
  pickedCount,
  planBulk,
  reviewCap,
  setPicked,
  SNOOZE_CHOICES,
  SOCIAL_BULK_NOTE,
  togglePicked,
  untilText,
} from "./needsSelect";

const groupIds = new WeakMap<HTMLInputElement, string[]>();

/** A row's checkbox: a native input inside a label that names the item. */
export function pickBox(item: Pick<AttentionItem, "id" | "title">): HTMLElement {
  const input = el("input", { type: "checkbox", class: "nt-pick-in", "data-pick": item.id, "data-focus": `${item.id}:pick` });
  input.checked = isPicked(item.id);
  input.addEventListener("change", () => togglePicked(item.id));
  return el("label", { class: "nt-pick", title: "Select this item" }, input, el("span", { class: "sr-only" }, `Select ${item.title}`));
}

/**
 * A source's or area's select-all checkbox over *ids* (mixed state shown with `indeterminate`).
 * *ids* are every row the list shows for the line under the current filters (Assigned to me, a source
 * filter), drawn or not: a closed line, and every source line at the wide band, selects its rows too
 * (2026-11-18: the owner's desktop test, where every line box was disabled). With no rows it is disabled.
 */
export function groupPickBox(ids: string[], name: string): HTMLElement {
  const input = el("input", { type: "checkbox", class: "nt-pick-in", "data-pick-group": "" });
  groupIds.set(input, ids);
  input.disabled = ids.length === 0;
  paintGroup(input);
  input.addEventListener("change", () => setPicked(ids, groupState(ids) !== "all"));
  return el(
    "label",
    {
      class: "nt-pick nt-pick-group",
      title: ids.length ? `Select every row listed in ${name}` : `Nothing is listed in ${name} to select`,
    },
    input,
    el("span", { class: "sr-only" }, `Select all listed in ${name}`),
  );
}

function paintGroup(input: HTMLInputElement): void {
  const state = groupState(groupIds.get(input) ?? []);
  input.checked = state === "all";
  input.indeterminate = state === "some";
}

// Other places that draw rows (the pane) register their element here, so the bulk bar's selection reaches
// their checkboxes too (2026-11-18: one selection for every place a row can appear).
const pickRoots = new Set<ParentNode>();
export function watchPicks(root: ParentNode): () => void {
  pickRoots.add(root);
  return () => void pickRoots.delete(root);
}

/** Bring every checkbox under *root* in line with the selection (no redraw, so focus stays). */
export function syncPicks(root: ParentNode): void {
  for (const input of root.querySelectorAll<HTMLInputElement>("input[data-pick]")) {
    input.checked = isPicked(input.dataset.pick ?? "");
  }
  for (const input of root.querySelectorAll<HTMLInputElement>("input[data-pick-group]")) paintGroup(input);
}

export interface BulkBar {
  node: HTMLElement;
  /** The result note under the toolbar row (absolutely placed, never in the layout flow). */
  note: HTMLElement;
  /** A visually hidden polite status node: selection entry and exit, and every action's result text. */
  status: HTMLElement;
  /** Redraw the bar for the listed items (call after every table draw). */
  update: (items: AttentionItem[], canChat: boolean, shown?: ReadonlySet<string>) => void;
  /** Call when a table draw has finished: if a selection ended in it and focus fell to the body, the first tool takes it. */
  settle: () => void;
  stop: () => void;
}

/** The bulk bar: one node for the page's life; `update` fits it to the list and the selection. */
export function createBulkBar(p: Panel): BulkBar {
  let items: AttentionItem[] = [];
  let canChat = false;
  let shownIds: ReadonlySet<string> | null = null;
  let busy = false;
  let snoozeOpen = false;
  const count = el("span", { class: "nt-bulk-n" });
  const note = el("span", { class: "nt-bulk-note meta" });
  const controls = el("div", { class: "nt-bulk-acts" });
  const node = el(
    "div",
    { class: "nt-bulk", role: "region", "aria-label": "Selected items", hidden: true },
    el("div", { class: "nt-bulk-row" }, count, controls, note),
  );
  // The live region sits outside the bar (the bar is display:none at zero) and never takes space.
  const status = el("div", { class: "nt-bulk-status sr-only", "aria-live": "polite", role: "status" });
  // An action's result is a one-line note laid over the card header under the toolbar row (style.css), never in the
  // flow, so a result cannot move the list. The reasons sit behind Details in a popover anchored to the note.
  const line = el("span", { class: "nt-bulk-line" });
  const why = el("div", { class: "nt-bulk-pop", role: "group", "aria-label": "Why some items could not be changed", hidden: true });
  const details = el("button", { type: "button", class: "nt-bulk-details", "aria-expanded": "false", hidden: true }, "Details");
  const close = el("button", { type: "button", class: "nt-bulk-x", "aria-label": "Close this message", title: "Close this message" }, closeIcon());
  const result = el("div", { class: "nt-bulk-result", hidden: true }, line, details, close, why);
  let timer: ReturnType<typeof setTimeout> | undefined;
  let suppressExit = false;
  let prevN = 0;
  let clearing = false;
  let lost = false;
  let stopped = false;

  /** The toolbar's first control (the tools come back where the bar was). */
  const toolsFirst = (): HTMLElement | null =>
    node.parentElement?.querySelector<HTMLElement>(":scope > .attn-tools button, :scope > .attn-tools input") ?? null;

  const focusLost = (): boolean => {
    const at = document.activeElement;
    return !at || at === document.body || node.contains(at) || !at.isConnected;
  };

  const setWhy = (open: boolean): void => {
    why.hidden = !open;
    details.setAttribute("aria-expanded", String(open));
    // Open Details keeps the note; closing them starts the 6 seconds again while the note is still shown.
    clearTimeout(timer);
    if (!open && !result.hidden && !stopped) timer = setTimeout(() => show(""), 6000);
  };
  const closeSnooze = (): void => {
    if (!snoozeOpen) return;
    snoozeOpen = false;
    draw();
  };
  // A press anywhere outside the Snooze group, or focus leaving it, closes the menu; focus is never moved for it.
  const onPointerDown = (event: Event): void => {
    if (!(event.target as Element | null)?.closest?.(".nt-bulk-snooze")) closeSnooze();
  };
  document.addEventListener("pointerdown", onPointerDown);
  node.addEventListener("focusout", (event) => {
    const to = (event as FocusEvent).relatedTarget as Element | null;
    if (to && !to.closest(".nt-bulk-snooze")) closeSnooze();
  });
  node.addEventListener("keydown", (event) => {
    if (event.key !== "Escape" || !snoozeOpen) return;
    snoozeOpen = false;
    draw();
    node.querySelector<HTMLElement>("[data-focus='snooze:toggle']")?.focus();
  });
  details.addEventListener("click", () => setWhy(!!why.hidden));
  close.addEventListener("click", () => {
    show("");
    (node.hidden ? toolsFirst() : node.querySelector<HTMLElement>("button"))?.focus();
  });

  const show = (text: string, reasons: Array<{ error: string; count: number }> = [], tone: "ok" | "error" = "ok"): void => {
    clearTimeout(timer);
    if (stopped) return;
    const focusInNote = result.contains(document.activeElement);
    suppressExit = !!text;
    if (text) status.textContent = text; // an empty call hides the note; the status line keeps what draw() said
    result.dataset.result = text ? tone : "";
    result.hidden = !text;
    line.textContent = text;
    replaceChildren(why, [
      reasons.length ? el("ul", { class: "nt-bulk-why" }, ...reasons.map((r) => el("li", {}, r.count > 1 ? `${r.error} (${r.count} items)` : r.error))) : null,
    ]);
    details.hidden = !reasons.length;
    // With no room under the toolbar row (the progress bars slot is empty or short) the note takes the toolbar row's
    // place instead of covering the first list line (style.css, .is-inline).
    const comp = p.root.querySelector<HTMLElement>(":scope > .nt-comp-slot");
    result.classList.toggle("is-inline", !!text && (comp?.offsetHeight ?? 0) < 40);
    setWhy(false);
    const at = document.activeElement;
    if (text && result.classList.contains("is-inline")) {
      // The note took the toolbar row's place, so the tools are hidden: focus that fell or sat there goes to Close.
      if (!at || at === document.body || !!node.parentElement?.querySelector(":scope > .attn-tools")?.contains(at)) close.focus();
    } else if (!text && focusInNote) {
      // The note went (timeout or close) while focus was on its buttons: it goes back to the toolbar / bar.
      (node.hidden ? toolsFirst() : node.querySelector<HTMLElement>("button"))?.focus();
    }
  };

  const run = async (action: "dismiss" | "snooze", ids: string[], until?: SnoozeChoice, extra = ""): Promise<void> => {
    if (busy || !ids.length) return;
    busy = true;
    snoozeOpen = false;
    draw();
    const verb = action === "dismiss" ? "dismiss" : "snooze";
    // Each chunk is applied as it comes back: what worked leaves the selection at once, so a later
    // chunk that fails can never hide it. What failed, and what was never sent, stays selected.
    const answers: BulkAnswer[] = [];
    let stopped: unknown = null;
    try {
      for (const piece of chunks(ids)) {
        const answer = await bulkAttention(p.context, action, piece, until);
        answers.push(answer);
        const worked = answer.results.filter((r) => r.ok).map((r) => r.id);
        forgetHistory(...worked);
        setPicked(worked, false);
      }
    } catch (error) {
      stopped = error;
    }
    try {
      const joined = joinAnswers(answers);
      const words = bulkWords(action, joined);
      const when = action === "snooze" && joined.until ? ` until ${untilText(joined.until)}; back sooner if they get worse` : "";
      const line = `${words.line}${joined.succeeded ? when : ""}${extra ? `. ${extra}` : ""}`;
      if (stopped === null) {
        show(line, words.reasons, words.failed.length ? "error" : "ok");
      } else {
        const sent = new Set(joined.results.map((r) => r.id));
        const unsent = ids.filter((id) => !sent.has(id)).length;
        show(
          `${joined.results.length ? `${line}. ` : ""}Could not ${verb} the rest (${describeError(stopped)}). ${unsent} ${unsent === 1 ? "item was" : "items were"} not sent and stay selected; try again.`,
          words.reasons,
          "error",
        );
      }
    } finally {
      // The first chunks may have changed the list even when a later one failed: read it again.
      await reloadPanel(p).catch(() => undefined);
    }
    busy = false;
    draw();
  };

  const draw = (): void => {
    const plan = planBulk(items);
    const n = plan.items.length;
    node.hidden = n === 0;
    count.textContent = n ? `${n} selected` : "";
    node.dataset.count = String(n);
    const controlNodes: Array<HTMLElement | null> = [];
    if (n) {
      const dismiss = button("", {
        title: "Hide the selected items until they get worse or the dismissal runs out",
        onClick: () => {
          const skipped = plan.social ? SOCIAL_BULK_NOTE : "";
          // Only Social posts selected: nothing to send, so the line says where their buttons are.
          if (!plan.dismiss.length) show(skipped, [], "error");
          else void run("dismiss", plan.dismiss, undefined, skipped);
        },
      });
      dismiss.classList.add("nt-bulk-btn");
      dismiss.replaceChildren(eyeOffIcon(), el("span", {}, "Dismiss"));
      dismiss.setAttribute("aria-label", `Dismiss ${n} selected`);
      dismiss.disabled = busy;
      controlNodes.push(dismiss);
      if (plan.snooze.length) {
        const open = button("", {
          title: "Hide the selected items for a while; they come back sooner if they get worse",
          onClick: () => {
            snoozeOpen = !snoozeOpen;
            draw();
            node.querySelector<HTMLElement>(".nt-bulk-snooze [data-focus='snooze:toggle']")?.focus();
          },
        });
        open.classList.add("nt-bulk-btn");
        open.dataset.focus = "snooze:toggle";
        open.setAttribute("aria-expanded", String(snoozeOpen));
        open.setAttribute("aria-label", `Snooze ${plan.snooze.length} selected`);
        open.replaceChildren(clockIcon(), el("span", {}, "Snooze"));
        open.disabled = busy;
        const choices = snoozeOpen
          ? SNOOZE_CHOICES.map((choice) => {
              const b = button(choice.label, { title: choice.title, onClick: () => void run("snooze", plan.snooze, choice.id) });
              b.classList.add("nt-bulk-choice");
              b.setAttribute("aria-label", `Snooze ${plan.snooze.length} selected until ${choice.label}`);
              return b;
            })
          : [];
        // The choices are a small menu under the Snooze button, out of the flow: inline they overflowed the card at the
        // wide band and made the bar taller at the stack band (measured 2026-11-19).
        controlNodes.push(
          el(
            "span",
            { class: "nt-bulk-snooze", role: "group", "aria-label": "Snooze the selected items" },
            open,
            choices.length ? el("span", { class: "nt-bulk-menu", role: "group", "aria-label": "Snooze until" }, ...choices) : null,
          ),
        );
      }
      if (canChat && plan.review.length) {
        const take = reviewCap(plan.review.length);
        const words = plan.review.length > take ? `Review ${take} of ${plan.review.length}` : `Review ${plan.review.length}`;
        const review = button("", {
          title: "Open one chat that reads the selected items, from any source, and reports. It changes nothing.",
          onClick: () => {
            const chosen = items.filter((item) => plan.review.includes(item.id));
            // reviewMany says a failure by appending a notice to its host; the note takes that text instead.
            const host = el("div");
            void reviewMany(p, chosen, review, host).then(() => {
              const said = host.querySelector(".flash");
              if (said) show(said.textContent ?? "", [], said.className.includes("error") ? "error" : "ok");
              setPicked(
                chosen.filter((item) => p.worked.has(item.id)).map((item) => item.id),
                false,
              );
            });
          },
        });
        review.classList.add("nt-bulk-btn");
        review.setAttribute("aria-label", `${words} selected items`);
        review.replaceChildren(listCheckIcon(), el("span", {}, words));
        review.disabled = busy;
        controlNodes.push(review);
      }
      const clear = button("Clear", {
        title: "Unselect everything",
        onClick: () => {
          clearing = true;
          clearPicked();
          clearing = false;
          // The tools come back in the bar's place; focus goes to the first of them.
          toolsFirst()?.focus();
        },
      });
      clear.classList.add("nt-bulk-btn", "btn-quiet");
      clear.disabled = busy;
      controlNodes.push(clear);
    }
    replaceChildren(controls, controlNodes);
    const away = shownIds ? plan.items.filter((item) => !shownIds!.has(item.id)).length : 0;
    note.textContent = [
      plan.dismissOnly
        ? `${plan.dismissOnly} of these ${plan.dismissOnly === 1 ? "takes" : "take"} Dismiss only (your own Git repos).`
        : "",
      away ? `${away} selected ${away === 1 ? "is" : "are"} on another tab or filter; the buttons reach them too.` : "",
    ]
      .filter(Boolean)
      .join(" ");
    // Entering and leaving selection mode is said once, through the status node (never on every redraw).
    const began = prevN === 0 && n > 0;
    const ended = prevN > 0 && n === 0;
    prevN = n;
    if (began) {
      lost = false;
      suppressExit = false;
      status.textContent = `${n} selected, selection actions shown`;
    }
    if (ended) {
      if (!suppressExit) status.textContent = "Selection cleared";
      // Any end other than Clear (an action finishing, a refresh dropping the last row, a filter change): if focus
      // sat in the bar or fell to the body, it goes to the toolbar's first control.
      if (!clearing && focusLost()) {
        lost = true;
        toolsFirst()?.focus();
      }
    }
  };

  // A pick or a clear changes the bar and the checkboxes; an old result goes when the selection moves on.
  let lastCount = pickedCount();
  const unsubscribe = onSelection(() => {
    const moved = pickedCount() !== lastCount && !busy;
    lastCount = pickedCount();
    // A selection the person changes (Clear, unticking) is announced even while an old result note is still up.
    if (moved) suppressExit = false;
    syncPicks(p.root);
    for (const other of pickRoots) syncPicks(other);
    draw();
    // The old result goes after the redraw, so the bar is back before the note's Close button (where focus may sit) hides.
    if (moved) show("");
  });

  return {
    node,
    note: result,
    status,
    settle: () => {
      // A table draw rebuilds the tools after the bar was fitted, so the first tool is looked up again here.
      if (lost && focusLost()) toolsFirst()?.focus();
      lost = false;
    },
    update: (next, chat, shown) => {
      items = next;
      canChat = chat;
      shownIds = shown ?? null;
      lastCount = pickedCount();
      draw();
    },
    stop: () => {
      stopped = true;
      clearTimeout(timer);
      document.removeEventListener("pointerdown", onPointerDown);
      unsubscribe();
    },
  };
}
