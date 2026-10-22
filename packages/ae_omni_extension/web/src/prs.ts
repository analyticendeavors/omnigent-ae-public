// Open PRs (2026-09-24): every open pull request a session tracks, as a card
// with CI, mergeability, reviews and a Merge button. Reid is travelling for
// three weeks and merged every pull request by hand on github.com from his
// phone; this puts the merge next to the sessions that opened them.
//
// Which pull requests: upstream's per-session tracking
// (https://github.com/omnigent-ai/omnigent/pull/6935), read by
// `GET /v1/ae/prs` (ae_omni_policies.prs). The server decides whether the
// button is live and why not, so this card and the session's own card (patch
// P11, AePrCards.tsx) never disagree. Merge asks once (method, then Confirm),
// posts the head SHA the card showed, and afterwards offers "Mark session
// done" (sets ae.status to review, which frees a slot) for each session on the
// card that is not already in review or reference and has no other open pull
// request (reviewOffer.ts, 2026-09-25).
//
// 2026-09-26, from Reid's screenshots: the confirm step is one row (a
// segmented method toggle, Confirm merge, Cancel) that wraps to two only on a
// narrow card, and the offer reads "Mark session done" with a "frees a slot"
// hint, because "Set session to review" did not say what it does.
//
// A merged card stays in the section, with its result, until the page is
// closed or Dismiss is tapped, so the Mark session done button survives the
// refresh that drops the pull request from the open list.
//
// Open on GitHub (2026-09-27, openLink.ts) replaced Copy link on every card:
// since core patch P25 the host opens a github.com pull request URL for the
// page, in the device's default browser.
//
// Recommended method (2026-09-28): Reid asked the confirm step to say which
// method to use. The server decides (recommend_merge_method in
// ae_omni_policies.prs: a merge commit when the head is a long-lived branch
// such as dev or release/x, else squash); the toggle opens on that method, its
// segment carries a small "Recommended" tag, and the reason is one muted line
// under the question. The other method stays one tap away.
//
// One status row (2026-09-27, with core patch P26 for the chat's card): the
// chips, then the outcome or the reason Merge is off, then the buttons at the
// row's right end (`statusRow`), wrapping only when the card is too narrow. A
// merged card reads "Merged #9 (squashed) b0413d6" without GitHub's "Pull
// Request successfully merged" echo, and its notes and buttons share that row.
//
// Shared with Your move (2026-09-24): a pull request no session tracks (one a
// subagent or another session opened) is listed only under Your move, which
// had Copy link and nothing else, so Reid could see a green pull request on
// his phone and not merge it. The Merge button, its confirm step and the
// merged card are exported here (mergeButton, keepConfirm, mergeWhy,
// mergedCards) so both sections merge the same way; each merged card stays in
// the section it was merged from.
//
// Calmer cards (2026-10-14, Reid: "make this prettier", with the chat's card in
// core patch P37): the outlined chips read as buttons and the confirm step was
// a box inside the card. The head's icon names the state (merged is purple);
// a card whose Merge is live and clean says "Ready to merge" in one line, and
// the chips (an icon and muted text, a red tint only on what blocks) come back
// only when something is off. The confirm step is a divided part of the card:
// the reason led by "Recommended", the method toggle, then Cancel and a button
// that names the branch it lands on ("Squash into main"), since no other line
// on the card says it.

import type { ExtensionContext } from "@omnigent/extension-sdk";
import {
  AeRequestError,
  describeError,
  type MergeMethod,
  type MergeResult,
  mergePr,
  type OpenPrs,
  type PrCard,
  type PrSessionRef,
  setStatus,
  type YourMove,
} from "./api";
import { announce, body, button, el, empty, flash, notice, replaceChildren, section } from "./dom";
import { type ChipTone, checkIcon, closeIcon, pullIcon, toneIcon } from "./icons";
import { linkButtons } from "./openLink";
import { moreOpenText, noteOpenPrs, resetReviewOffer, splitReviewOffer } from "./reviewOffer";

export interface PrsView {
  data: OpenPrs | null;
  error: unknown;
}

interface Deps {
  context: ExtensionContext;
  refresh: (fresh?: boolean) => Promise<void>;
}

export const SECTION_ID = "prs";

const MERGEABLE_TEXT: Record<PrCard["mergeable"]["state"], string> = {
  clean: "No conflicts",
  unstable: "Mergeable",
  conflicted: "Conflicts",
  blocked: "Blocked",
  behind: "Behind base",
  draft: "Draft",
  checking: "Checking conflicts",
  merged: "Merged",
  closed: "Closed",
};

const REVIEW_TEXT: Record<PrCard["review"]["state"], string> = {
  approved: "Approved",
  changes_requested: "Changes requested",
  review_required: "Review requested",
  none: "No review",
  unknown: "Reviews unknown",
};

const CI_TONE: Record<PrCard["ci"]["state"], ChipTone> = {
  passing: "ok",
  failing: "err",
  pending: "wait",
  none: "muted",
  unknown: "muted",
};

const MERGEABLE_TONE: Record<PrCard["mergeable"]["state"], ChipTone> = {
  clean: "ok",
  unstable: "warn",
  conflicted: "err",
  blocked: "warn",
  behind: "warn",
  draft: "muted",
  checking: "wait",
  merged: "ok",
  closed: "muted",
};

const REVIEW_TONE: Record<PrCard["review"]["state"], ChipTone> = {
  approved: "ok",
  changes_requested: "err",
  review_required: "warn",
  none: "muted",
  unknown: "muted",
};

/** The method toggle's labels; the confirm button names the method and the branch. */
const METHOD_SHORT: Record<MergeMethod, string> = {
  squash: "Squash",
  merge: "Merge commit",
};

/** "Squash into main"; without a base branch, GitHub's own words. */
export function confirmLabel(method: MergeMethod, base: string | null): string {
  if (base) return `${method === "squash" ? "Squash" : "Merge"} into ${base}`;
  return method === "squash" ? "Squash and merge" : "Create merge commit";
}

/** The section a merge started in; its merged card stays there. */
export type MergeOrigin = "prs" | "your-move";

/** What a card needs from its section to merge: where it sits, and how to redraw it. */
export interface MergeHooks {
  origin: MergeOrigin;
  /** The card to show after a refused merge, from the fresh card the 409 carried. */
  rebuild: (fresh: PrCard) => HTMLElement;
  /** Called once the merge went through, before the merged card replaces the open one. */
  onMerged?: () => void;
}

/**
 * How long a merged pull request stays out of the open lists after Dismiss:
 * the server's 60-second pull request and card caches plus one 20-second poll,
 * so a cached answer that still lists it open cannot bring the card back.
 */
const TOMBSTONE_MS = 80_000;

/** Every map below is keyed by urlKey(url): GitHub's URL, lowercased. */
export const urlKey = (url: string) => url.toLowerCase();

/** Cards merged from this page, kept with their result until dismissed. */
const merged = new Map<string, { card: PrCard; result: MergeResult; origin: MergeOrigin }>();
/** When each pull request was merged from this page; Dismiss leaves these. */
const tombstones = new Map<string, number>();
/** Cards with the confirm step open, and the method chosen: a refresh re-renders
 * the section every 20 seconds and on focus, and must not close it. */
const confirming = new Map<string, MergeMethod>();
/** The live node of each card whose merge POST is in flight: a re-render reuses
 * it rather than drawing a second card, so the result lands where it was asked. */
const inFlight = new Map<string, HTMLElement>();

/** Whether this page merged the pull request at *url* in the last TOMBSTONE_MS, or still shows it merged. */
export function wasMerged(url: string): boolean {
  const key = urlKey(url);
  if (merged.has(key)) return true;
  const at = tombstones.get(key);
  if (at === undefined) return false;
  if (Date.now() - at < TOMBSTONE_MS) return true;
  tombstones.delete(key);
  return false;
}

/** The card node whose merge is in flight for *url*, to re-insert as it is. */
export function mergingNode(url: string): HTMLElement | undefined {
  return inFlight.get(urlKey(url));
}

/** Open the confirm step on the card's next render (Your move, after its tap read the card). */
export function openConfirm(card: PrCard): void {
  confirming.set(urlKey(card.url), startMethod(card));
}

/** The method the confirm step opens on: the server's recommendation, else its default. */
function startMethod(card: PrCard): MergeMethod {
  const { methods, recommended_method, default_method } = card.merge;
  if (recommended_method && methods.includes(recommended_method)) return recommended_method;
  return default_method ?? methods[0];
}

/** For tests: forget merged cards. */
export function resetPrsState(): void {
  merged.clear();
  tombstones.clear();
  confirming.clear();
  inFlight.clear();
  resetReviewOffer();
}

/** "6/6 checks", "1 of 6 checks failing", "4/6 passed, 2 running", "No checks". */
export function ciText(ci: PrCard["ci"]): string {
  switch (ci.state) {
    case "passing":
      return `${ci.passing}/${ci.total} checks`;
    case "failing":
      return `${ci.failing} of ${ci.total} checks failing`;
    case "pending":
      return `${ci.passing}/${ci.total} passed, ${ci.pending} running`;
    case "none":
      return "No checks";
    default:
      return "Checks unknown";
  }
}

/** The count the heading shows: open cards, "off" without a token. */
export function prCount(view: PrsView): number | string | undefined {
  if (!view.data) return undefined;
  if (!view.data.configured) return "off";
  return view.data.items.length;
}

/**
 * Your move's pull requests minus the ones Open PRs already shows as cards,
 * so a pull request appears once on the Dashboard.
 */
export function withoutCarded(yourMove: YourMove, prs: OpenPrs | null): YourMove {
  if (!prs?.configured || !yourMove.pulls.ok) return yourMove;
  const carded = new Set(prs.items.map((card) => card.url.toLowerCase()));
  const items = yourMove.pulls.items.filter((pull) => !carded.has(pull.url.toLowerCase()));
  if (items.length === yourMove.pulls.items.length) return yourMove;
  const removed = yourMove.pulls.items.length - items.length;
  return {
    ...yourMove,
    total: Math.max(0, yourMove.total - removed),
    pulls: { ...yourMove.pulls, items },
  };
}

function chip(text: string, tone: ChipTone, title?: string): HTMLElement {
  return el("span", { class: `pr-chip pr-${tone}`, title }, toneIcon(tone), text);
}

/** Merge is live and nothing needs a look: one Ready line stands in for the chips. */
export function isReady(card: PrCard): boolean {
  return (
    card.state === "open" &&
    card.merge.allowed &&
    card.mergeable.state === "clean" &&
    (card.ci.state === "passing" || card.ci.state === "none")
  );
}

/** "Ready to merge · 6/6 checks", plus the review when there is one to name. */
function readyLine(card: PrCard): HTMLElement {
  const parts = [ciText(card.ci)];
  if (card.review.state === "approved") parts.push(REVIEW_TEXT.approved);
  if (card.review.state === "review_required") parts.push(REVIEW_TEXT.review_required);
  return el(
    "p",
    { class: "pr-ready" },
    toneIcon("ok"),
    el(
      "span",
      {},
      el("span", { class: "pr-ready-text" }, "Ready to merge"),
      el("span", { class: "pr-ready-meta" }, ` · ${parts.join(" · ")}`),
    ),
  );
}

/** What leads the status row: the outcome of a closed card, the Ready line, or the chips. */
function statusLead(card: PrCard): HTMLElement {
  if (card.state === "merged") return el("p", { class: "pr-merged-text" }, "Merged");
  if (card.state === "closed") return el("p", { class: "pr-closed" }, "Closed without merging");
  if (isReady(card)) return readyLine(card);
  return el(
    "p",
    { class: "pr-chips" },
    chip(ciText(card.ci), CI_TONE[card.ci.state]),
    chip(
      MERGEABLE_TEXT[card.mergeable.state],
      MERGEABLE_TONE[card.mergeable.state],
      card.mergeable.detail,
    ),
    chip(REVIEW_TEXT[card.review.state], REVIEW_TONE[card.review.state]),
  );
}

function sessionLinks(deps: Deps, refs: PrSessionRef[]): HTMLElement | null {
  if (refs.length === 0) return null;
  const line = el("p", { class: "meta pr-sessions" }, "from ");
  refs.forEach((ref, index) => {
    const link = el("button", { type: "button", class: "linkish" }, ref.title);
    link.addEventListener("click", () => void deps.context.navigation.openSession(ref.id));
    if (index > 0) line.append(", ");
    line.append(link);
  });
  return line;
}

function reviewOffers(
  deps: Deps,
  refs: PrSessionRef[],
  target: HTMLElement,
  url: string,
): HTMLElement[] {
  const { offer, waiting } = splitReviewOffer(refs, url);
  const notes = waiting.map((wait) =>
    el("span", { class: "meta pr-sessions pr-more-open" }, moreOpenText(wait, refs.length > 1)),
  );
  if (offer.length === 0) return notes;
  // Optional, so a quiet (outline) button, with the hint on screen for the
  // phone, where the title never shows (2026-09-26).
  const row = el("div", { class: "actions pr-done" });
  for (const ref of offer) {
    row.appendChild(
      button(offer.length === 1 ? "Mark session done" : `Mark "${ref.title}" done`, {
        title: `Set "${ref.title}" to Review (done), which frees one of your slots`,
        onClick: async (node) => {
          try {
            await setStatus(deps.context, ref.id, "review");
            ref.status = "review";
            node.replaceWith(
              el("span", { class: "meta" }, `"${ref.title}" marked done. Its slot is free.`),
            );
            await deps.refresh(true);
          } catch (error) {
            flash(target, describeError(error), "error");
          }
        },
      }),
    );
  }
  row.appendChild(
    el(
      "span",
      { class: "meta pr-done-hint" },
      offer.length === 1 ? "frees a slot" : "each frees a slot",
    ),
  );
  return [...notes, row];
}

function mergedCard(deps: Deps, card: PrCard, result: MergeResult): HTMLElement {
  const node = el("article", { class: "card card-pr card-pr-merged", "data-pr": card.url });
  // Dismiss is an icon in the head's corner (2026-10-15). As a third group on
  // the status row it dropped to a line of its own beside Mark session done.
  const dismiss = button("Dismiss", {
    title: "Dismiss",
    onClick: () => {
      merged.delete(urlKey(card.url));
      node.remove();
    },
  });
  dismiss.classList.add("btn-dismiss");
  dismiss.setAttribute("aria-label", "Dismiss");
  dismiss.replaceChildren(closeIcon());
  node.appendChild(
    el(
      "div",
      { class: "card-head" },
      pullIcon(false, "merged"),
      el("h3", { class: "card-title pull-title", title: card.title }, card.title),
      dismiss,
    ),
  );
  // GitHub's own message ("Pull Request successfully merged") only repeats
  // this line, so it is not shown (2026-09-27).
  const how = result.method === "squash" ? "squashed" : "merge commit";
  const said = el(
    "span",
    { class: "pr-merged-text", role: "status" },
    `Merged #${card.number} (${how})${result.sha ? ` ${result.sha.slice(0, 7)}` : ""}`,
  );
  node.appendChild(statusRow(said, ...reviewOffers(deps, card.sessions ?? [], node, card.url)));
  return node;
}

/** Merged cards that started in *origin*, oldest first. */
export function mergedCards(deps: Deps, origin: MergeOrigin): HTMLElement[] {
  return [...merged.values()]
    .filter((entry) => entry.origin === origin)
    .map(({ card, result }) => mergedCard(deps, card, result));
}

function confirmRow(
  deps: Deps,
  card: PrCard,
  node: HTMLElement,
  actions: HTMLElement,
  hooks: MergeHooks,
): void {
  const methods = card.merge.methods;
  const key = urlKey(card.url);
  // One confirm step per card while its merge is in flight (2026-09-27).
  if (inFlight.has(key)) return;
  const held = confirming.get(key);
  const chosen = {
    method: held && methods.includes(held) ? held : startMethod(card),
  };
  const recommended = card.merge.recommended_method;
  const why = card.merge.recommended_reason;
  confirming.set(key, chosen.method);
  const box = el("div", { class: "pr-confirm", role: "group", "aria-label": "Confirm merge" });
  // The reason leads with "Recommended" only when there is a choice to make.
  if (why) {
    const choice = methods.length > 1 && !!recommended && methods.includes(recommended);
    box.appendChild(
      el(
        "p",
        { class: "pr-confirm-why" },
        choice ? el("span", { class: "pr-confirm-rec" }, "Recommended") : null,
        choice ? " · " : null,
        why,
      ),
    );
  }
  // One row: the method toggle (only when the repo allows both), then Cancel
  // and the confirm button at its right end; the stylesheet wraps it to two
  // rows on a phone. The buttons come first, so the toggle's handler only
  // ever calls what is already declared (review, 2026-10-14).
  let picker: HTMLElement | null = null;
  const cancel = () => {
    if (inFlight.has(key)) return;
    confirming.delete(key);
    box.remove();
    actions.hidden = false;
  };
  const cancelButton = button("Cancel", { onClick: cancel });
  const go = button("", {
    kind: "primary",
    onClick: async () => {
      // One POST per card: the button is off while it runs, and a confirm
      // step drawn by a re-render meanwhile cannot send a second.
      if (inFlight.has(key)) return;
      inFlight.set(key, node);
      // The method is sent: changing it or cancelling now would relabel the
      // button or reopen Merge under a merge already on its way.
      for (const input of picker?.querySelectorAll("input") ?? []) input.disabled = true;
      cancelButton.disabled = true;
      try {
        const result = await mergePr(deps.context, card, chosen.method);
        inFlight.delete(key);
        confirming.delete(key);
        merged.set(key, { card, result, origin: hooks.origin });
        tombstones.set(key, Date.now());
        hooks.onMerged?.();
        node.replaceWith(mergedCard(deps, card, result));
        announce(`Merged ${card.repo}#${card.number}.`);
        await deps.refresh(true);
      } catch (error) {
        inFlight.delete(key);
        cancel();
        // A refused merge (gate shut, head moved) carries the fresh card.
        const fresh =
          error instanceof AeRequestError
            ? (error.body as { error?: { card?: PrCard } } | null)?.error?.card
            : undefined;
        let target = node;
        if (fresh) {
          target = hooks.rebuild(fresh);
          node.replaceWith(target);
        }
        flash(target, `Not merged: ${describeError(error)}`, "error");
      }
    },
  });
  // The button follows the chosen method and `say` is its label's one writer;
  // a long branch name is cut by the stylesheet, so the title keeps it whole.
  const say = () => {
    const label = confirmLabel(chosen.method, card.base_ref);
    go.textContent = label;
    go.title = label;
  };
  say();
  if (methods.length > 1) {
    picker = el("div", { class: "pr-methods", role: "radiogroup", "aria-label": "Method" });
    for (const method of methods) {
      // The label wraps its input, so no id and no `for`: two cards with the
      // same number in different repos cannot check each other's radio.
      const input = el("input", {
        type: "radio",
        name: `method-${card.repo}-${card.number}`,
        value: method,
        checked: method === chosen.method,
      });
      input.addEventListener("change", () => {
        if (inFlight.has(key)) return;
        chosen.method = method;
        confirming.set(key, method);
        say();
      });
      picker.appendChild(
        el(
          "label",
          { class: "pr-method", title: method === recommended && why ? why : undefined },
          input,
          checkIcon(),
          el("span", { class: "pr-method-text" }, METHOD_SHORT[method]),
        ),
      );
    }
  }
  box.appendChild(el("div", { class: "actions pr-confirm-row" }, picker, cancelButton, go));
  actions.hidden = true;
  node.appendChild(box);
}

/**
 * The card's one status row: chips or the outcome, the reason, then the
 * `.actions` at the right end (style.css `.pr-status`). It wraps only when the
 * card is too narrow; the confirm step still opens under it.
 */
export function statusRow(...parts: (HTMLElement | null)[]): HTMLElement {
  const row = el("div", { class: "pr-status" });
  for (const part of parts) if (part) row.appendChild(part);
  return row;
}

/** The line that says why Merge is off, or null when it is live. */
export function mergeWhy(card: PrCard): HTMLElement | null {
  if (card.merge.allowed || !card.merge.reason) return null;
  return el("p", { class: "pr-why" }, card.merge.reason);
}

/**
 * The Merge button: live when the server's gate is open, else disabled with the
 * reason as its title. A tap opens the confirm step under *actions* in *node*.
 */
export function mergeButton(
  deps: Deps,
  card: PrCard,
  node: HTMLElement,
  actions: HTMLElement,
  hooks: MergeHooks,
): HTMLButtonElement {
  return button("Merge", {
    kind: "primary",
    disabled: !card.merge.allowed,
    title: card.merge.allowed
      ? `Merge #${card.number} (asks to confirm first)`
      : (card.merge.reason ?? "Merge is off"),
    onClick: () => confirmRow(deps, card, node, actions, hooks),
  });
}

/**
 * Reopen a confirm step the 20-second re-render closed, or forget it when the
 * gate shut meanwhile. Call once *actions* is the last child of *node*.
 */
export function keepConfirm(
  deps: Deps,
  card: PrCard,
  node: HTMLElement,
  actions: HTMLElement,
  hooks: MergeHooks,
): void {
  if (card.merge.allowed && confirming.has(urlKey(card.url))) {
    confirmRow(deps, card, node, actions, hooks);
  } else {
    confirming.delete(urlKey(card.url));
  }
}

/** One pull request's card; the Dashboard and its tests build it the same way. */
export function prCard(deps: Deps, card: PrCard): HTMLElement {
  const live = mergingNode(card.url);
  if (live) return live;
  const hooks: MergeHooks = {
    origin: "prs",
    rebuild: (fresh) => prCard(deps, { ...fresh, sessions: card.sessions }),
  };
  // The left border follows the state: purple once merged, the plain border
  // when closed, and the CI colour only while the pull request is open.
  const border =
    card.state === "merged"
      ? " card-pr-merged"
      : card.state === "open"
        ? ` card-ci-${card.ci.state}`
        : "";
  const node = el("article", { class: `card card-pr${border}`, "data-pr": card.url });
  node.appendChild(
    el(
      "div",
      { class: "card-head" },
      pullIcon(card.draft, card.state),
      el("h3", { class: "card-title pull-title", title: card.title }, card.title),
    ),
  );
  node.appendChild(
    el(
      "p",
      { class: "card-sub" },
      el("span", { class: "repo" }, `${card.repo}#${card.number}`),
      card.head_ref ? el("code", { class: "pr-branch" }, card.head_ref) : null,
    ),
  );
  const sessions = sessionLinks(deps, card.sessions ?? []);
  if (sessions) node.appendChild(sessions);
  // A merged or closed card (a refused merge's fresh card) says so and has
  // no Merge to offer.
  const open = card.state === "open";
  const actions = el("div", { class: "actions" });
  if (open) actions.appendChild(mergeButton(deps, card, node, actions, hooks));
  actions.append(...linkButtons(deps.context, card.url, node));
  node.appendChild(statusRow(statusLead(card), open ? mergeWhy(card) : null, actions));
  keepConfirm(deps, card, node, actions, hooks);
  return node;
}

/** The Open PRs section: merged cards first, then open ones newest first. */
export function renderPrsSection(host: HTMLElement, view: PrsView, deps: Deps): void {
  // A pull request merged from this page (either section) is not open, even
  // while the server's 60-second cache still lists it, so it cannot hold back
  // the review offer on its session's last merge (2026-09-25).
  noteOpenPrs(
    view.data?.configured
      ? { ...view.data, items: view.data.items.filter((card) => !wasMerged(card.url)) }
      : view.data,
  );
  const data = view.data;
  const done = mergedCards(deps, "prs");
  if (!data) {
    const node = section(SECTION_ID, "Open PRs");
    body(node).append(
      ...done,
      view.error
        ? notice(`Pull requests could not be read: ${describeError(view.error)}`, "error")
        : empty("Loading."),
    );
    replaceChildren(host, [node]);
    return;
  }
  if (!data.configured) {
    const node = section(SECTION_ID, "Open PRs", "off");
    body(node).appendChild(
      el(
        "p",
        { class: "meta ym-off" },
        'Off: the server has no GitHub token (deploy/README.md, "The GitHub token").',
      ),
    );
    replaceChildren(host, [node]);
    return;
  }
  const open = data.items.filter((card) => !wasMerged(card.url));
  const count =
    open.length === 0 && (done.length > 0 || data.errors.length > 0) ? "0" : open.length;
  const node = section(SECTION_ID, "Open PRs", count);
  const target = body(node);
  if (view.error) {
    target.appendChild(
      notice(`Showing the last answer; the refresh failed: ${describeError(view.error)}`, "error"),
    );
  }
  if (done.length > 0 || open.length > 0) {
    const cards = el("div", { class: "cards" });
    cards.append(...done, ...open.map((card) => prCard(deps, card)));
    target.appendChild(cards);
  } else {
    target.appendChild(empty("No open pull requests in any session."));
  }
  // Pull requests GitHub would not show are worth a line each; a session whose
  // runner and host are both asleep is not (it is most of them, most days).
  for (const error of data.errors) {
    if (error.url) target.appendChild(notice(`${error.url}: ${error.message}`, "error"));
  }
  replaceChildren(host, [node]);
}
