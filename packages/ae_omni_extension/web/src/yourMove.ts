// Your move (2026-09-24): everything waiting on Reid, readable on a phone.
// Reid runs several sessions at once and merges from his phone and
// kept asking "what's left"; this answers "what is waiting on
// me right now" from `GET /v1/ae/your-move`, each item with the one button
// that resolves it.
//
// Since the Dashboard merge (2026-09-24, docs/design/dashboard-review.md) this
// is the first section of the one Dashboard page instead of a page of its
// own, and it lists only what no other section shows:
//
//   open gates      Do it now / Write a note / Save for later / Drop it in
//                   place, posting A / B / C / close (labels since
//                   2026-09-27, gate.ts)
//   approvals       an approval or question outstanding; Open answers it in
//                   the session (upstream's Inbox answers it too)
//   due             lot items resurfacing today or overdue, with Resume
//   pull requests   open PRs by or awaiting Reid with CI state, a Merge
//                   button and Open on GitHub (Copy link until 2026-09-27,
//                   when core patch P25 let the host open GitHub pull
//                   request URLs for the sandboxed frame; openLink.ts)
//   steward         the latest brief, collapsed
//
// Merge on the pull request cards (2026-09-24): Open PRs lists only pull
// requests a session tracks, and the ones a subagent or another session opened
// often are not tracked, so Reid saw green, mergeable pull requests here with
// only Copy link. Each card now has the Open PRs card's Merge button, confirm
// step and merged card (prs.ts). The full card (merge gate, head SHA, methods)
// is read from `GET /v1/ae/prs/{owner}/{repo}/{number}` only when Merge is
// tapped: the list route's rows carry no mergeability and no reviews, so
// putting the gate in the list, or reading every card on render, would cost
// about five GitHub calls per pull request per minute (30 pull requests is
// over the token's 5,000 an hour). A tap costs one read; a second tap within
// the minute reuses it.
//
// Blocked and review sessions have their own sections below, so they are
// counted in their headings and not repeated here. Each source renders its own
// error, so one failing never blanks the section.
//
// Each item once (2026-10-14, second pass): about 550 px of the phone page
// repeated what this section shows. It decides first: Working and Blocked
// leave out a session it shows (a gate, an approval) and the lot a task it
// shows as due, and their headings say "1 in Your move" (shownInYourMove).
//
// Review (2026-10-14): a session it took out of Working or Blocked kept only
// the gate buttons and Open here, so it could not be parked or moved. Its
// card now carries the "..." menu those sections would give it (cards.ts
// rowMenu, capacity.ts slotMoves).

import type { ExtensionContext } from "@omnigent/extension-sdk";
import {
  type CiState,
  describeError,
  type DueTask,
  fetchPrCard,
  type PrCard,
  replyGate,
  type Session,
  type WaitingPull,
  type WaitingSession,
  type WaitKind,
  type YourMove,
} from "./api";
import { rowMenu } from "./cards";
import { announce, body, button, el, empty, flash, notice, replaceChildren, section } from "./dom";
import { GATE_ROUTES } from "./gate";
import { briefIcon, ciIcon, dueIcon, pullIcon, waitIcon } from "./icons";
import { parkedByTask, shortDate, taskCard } from "./lot";
import { linkButtons, urlLinkButton } from "./openLink";
import {
  detailsRow,
  keepConfirm,
  markDetailsOpen,
  type MergeHooks,
  mergeButton,
  mergedCards,
  mergeWhy,
  mergingNode,
  openConfirm,
  stateButtons,
  statusRow,
  urlKey,
  wasMerged,
} from "./prs";

export interface YourMoveView {
  data: YourMove | null;
  error: unknown;
}

interface Deps {
  context: ExtensionContext;
  /** Re-read the page; `fresh` skips the server's few-second cache after an action. */
  refresh: (fresh?: boolean) => Promise<void>;
}

/** What the section takes from the rest of the page (2026-10-14, review). */
export interface YourMoveExtras {
  /** Every session: a due task shows the parked ones hanging off it, as the lot does. */
  sessions?: Session[];
  /** A session card's "..." menu: Park and the moves Working or Blocked would offer it. */
  moves?: (row: WaitingSession) => HTMLButtonElement[];
}

const KIND_TEXT: Record<WaitKind, string> = {
  gate: "Open gate",
  awaiting: "Waiting on your answer",
  blocked: "Blocked",
  review: "Ready for review",
};

const CI_TEXT: Record<CiState, string> = {
  passing: "CI passing",
  failing: "CI failing",
  pending: "CI running",
  none: "no checks",
  unknown: "CI unknown",
};

/** How long a tap reuses a card it read: the server's CARD_TTL_S. */
const CARD_TTL_MS = 60_000;

// Every map below is keyed by urlKey(url), the convention prs.ts uses.

/** Each pull request's last good card, and the last read's error when it failed. */
const held = new Map<string, { card: PrCard | null; error: unknown; at: number }>();
/** Reads in flight, so a second tap on a re-drawn card waits for the first. */
const reading = new Map<string, Promise<void>>();
/** The node each pull request last rendered as, redrawn when its read lands. */
const shown = new Map<string, { node: HTMLElement; deps: Deps; pull: WaitingPull }>();

/** For tests: forget the pull requests' cards. */
export function resetYourMoveState(): void {
  held.clear();
  reading.clear();
  shown.clear();
}

/** Read *pull*'s full card; a failed read keeps the last good card and records the error. */
function readCard(deps: Deps, pull: WaitingPull): Promise<void> {
  const key = urlKey(pull.url);
  const pending = reading.get(key);
  if (pending) return pending;
  const run = fetchPrCard(deps.context, pull)
    .then(
      (card) => {
        held.set(key, { card, error: null, at: Date.now() });
      },
      (error: unknown) => {
        held.set(key, { card: held.get(key)?.card ?? null, error, at: Date.now() });
      },
    )
    .finally(() => reading.delete(key));
  reading.set(key, run);
  return run;
}

/** Draw *key*'s card again, in place, if it is on the page. */
function redraw(key: string): void {
  const current = shown.get(key);
  if (!current?.node.isConnected) return;
  const next = pullCard(current.deps, current.pull);
  if (next !== current.node) current.node.replaceWith(next);
}

/**
 * Merge tapped on a card with no fresh read: read it (unless a good read is
 * under a minute old), open the confirm step when the gate is open, redraw.
 * A shut gate shows its reason with Merge off; a failed read says so.
 */
async function tapMerge(deps: Deps, pull: WaitingPull): Promise<void> {
  const key = urlKey(pull.url);
  const entry = held.get(key);
  if (!entry?.card || entry.error || Date.now() - entry.at >= CARD_TTL_MS) {
    await readCard(deps, pull);
  }
  const card = held.get(key)?.card;
  if (card?.merge.allowed) openConfirm(card);
  redraw(key);
}

/** Drop held cards and drawn nodes for pull requests the list no longer has. */
function prune(pulls: WaitingPull[]): void {
  const listed = new Set(pulls.map((pull) => urlKey(pull.url)));
  for (const key of [...held.keys()]) if (!listed.has(key)) held.delete(key);
  for (const key of [...shown.keys()]) if (!listed.has(key)) shown.delete(key);
}

/** Session kinds this section shows; blocked and review have their own sections. */
const SHOWN_KINDS: ReadonlySet<WaitKind> = new Set(["gate", "awaiting"]);

function waitingSessions(data: YourMove): WaitingSession[] {
  return data.sessions.ok ? data.sessions.items.filter((row) => SHOWN_KINDS.has(row.kind)) : [];
}

/**
 * What the section shows, by id (2026-10-14, each item once): Working and
 * Blocked leave out these sessions and the lot these due tasks (main.ts).
 */
export function shownInYourMove(data: YourMove): { sessions: Set<string>; tasks: Set<string> } {
  return {
    sessions: new Set(waitingSessions(data).map((row) => row.id)),
    tasks: new Set(data.lot.ok ? data.lot.items.map((task) => task.id) : []),
  };
}

/** What the section counts: gates and approvals, due lot items, pull requests. */
export function counts(data: YourMove): {
  sessions: number;
  due: number;
  pulls: number;
  total: number;
} {
  const sessions = waitingSessions(data).length;
  const due = data.lot.ok ? data.lot.items.length : 0;
  // A pull request merged from this page is out of the count as soon as it is
  // out of the list, even while a cached answer still has it.
  const pulls = data.pulls.ok ? data.pulls.items.filter((pull) => !wasMerged(pull.url)).length : 0;
  return { sessions, due, pulls, total: sessions + due + pulls };
}

function sessionCard(deps: Deps, row: WaitingSession, extras: YourMoveExtras): HTMLElement {
  const card = el("article", {
    class: `card card-wait card-wait-${row.kind}`,
    "data-session": row.id,
  });
  const title = el("h3", { class: "card-title" }, row.title);
  title.addEventListener("click", () => void deps.context.navigation.openSession(row.id));
  const moves = extras.moves?.(row) ?? [];
  card.appendChild(
    el(
      "div",
      { class: "card-head" },
      waitIcon(row.kind),
      title,
      el("span", { class: "meta" }, row.since),
      moves.length > 0 ? rowMenu(row, moves) : null,
    ),
  );
  const why = row.kinds.map((kind) => KIND_TEXT[kind]).join(" · ");
  card.appendChild(
    el(
      "p",
      { class: "card-sub why" },
      why,
      row.pending > 1 ? ` (${row.pending} outstanding)` : null,
    ),
  );
  if (row.kind === "gate") {
    card.appendChild(
      el(
        "p",
        { class: "fork" },
        el("span", { class: "k" }, "FORK: "),
        row.fork ?? "(clause not captured)",
      ),
    );
  } else if (row.next) {
    card.appendChild(el("p", { class: "next" }, el("span", { class: "k" }, "NEXT "), row.next));
  }
  const actions = el("div", { class: "actions" });
  if (row.kind === "gate") {
    for (const [route, text, hint] of GATE_ROUTES) {
      actions.appendChild(
        button(text, {
          title: hint,
          kind: route === "close" ? "quiet" : "primary",
          onClick: async () => {
            try {
              const result = await replyGate(deps.context, row.id, route);
              announce(`${row.title}: posted "${result.text}" as the next user turn.`);
              await deps.refresh(true);
            } catch (error) {
              flash(card, describeError(error), "error");
            }
          },
        }),
      );
    }
  }
  actions.appendChild(
    button("Open", {
      kind: row.kind === "gate" ? "quiet" : "primary",
      title: row.kind === "awaiting" ? "Open the session to answer it" : "Open the session",
      onClick: () => deps.context.navigation.openSession(row.id),
    }),
  );
  card.appendChild(actions);
  return card;
}

function pullCard(deps: Deps, pull: WaitingPull): HTMLElement {
  // A card whose merge is in flight is not drawn again: its result lands on it.
  const live = mergingNode(pull.url);
  if (live) return live;
  const key = urlKey(pull.url);
  const entry = held.get(key);
  const full = entry?.card ?? null;
  const card = el("article", { class: `card card-pull card-ci-${pull.ci}`, "data-pr": pull.url });
  card.appendChild(
    el(
      "div",
      { class: "card-head" },
      pullIcon(full?.draft ?? pull.draft),
      el("h3", { class: "card-title pull-title", title: pull.title }, pull.title),
      ciIcon(pull.ci),
    ),
  );
  const why = pull.why.includes("review_requested")
    ? pull.why.includes("author")
      ? "yours, review requested"
      : `review requested by ${pull.author ?? "someone"}`
    : "yours";
  const url = el("p", { class: "meta url" });
  url.appendChild(urlLinkButton(deps.context, pull.url, card));
  card.appendChild(url);
  // One status row (2026-09-27): the CI line, the reason, then the buttons.
  const sub = el(
    "p",
    { class: "card-sub" },
    el("span", { class: "repo" }, `${pull.repo}#${pull.number}`),
    el("span", { class: `ci ci-${pull.ci}` }, CI_TEXT[pull.ci]),
    (full?.draft ?? pull.draft) ? el("span", { class: "tag" }, "draft") : null,
    el("span", {}, why),
  );
  const off = full ? mergeWhy(full) : null;
  let failed: HTMLElement | null = null;
  if (entry?.error) {
    const said = describeError(entry.error);
    failed = el(
      "p",
      { class: "pr-why" },
      full
        ? `Showing the last read; reading it again failed: ${said}. `
        : `Could not check whether it can merge: ${said}. `,
      "Tap Merge to try again, or Open on GitHub to merge there.",
    );
  }
  const hooks: MergeHooks = {
    origin: "your-move",
    rebuild: (fresh) => {
      held.set(key, { card: fresh, error: null, at: Date.now() });
      return pullCard(deps, pull);
    },
    // A merged pull request's card is not kept: a later listing reads afresh.
    onMerged: () => held.delete(key),
  };
  const actions = el("div", { class: "actions" });
  const fresh = full !== null && !entry?.error && Date.now() - (entry?.at ?? 0) < CARD_TTL_MS;
  if (fresh && full) actions.append(...stateButtons(deps, full, card, hooks));
  actions.appendChild(
    fresh
      ? mergeButton(deps, full, card, actions, hooks)
      : button("Merge", {
          kind: "primary",
          title: "Checks CI, reviews and conflicts on GitHub, then asks to confirm",
          onClick: () => tapMerge(deps, pull),
        }),
  );
  // No read on render or poll (the rule above); one tap reads the card, then its
  // state buttons and Review details show without starting a merge (review, 2026-10-23).
  if (!full) {
    actions.appendChild(
      button("Review details", {
        title: "Read this pull request from GitHub: checks, description and compliance review",
        onClick: async () => {
          markDetailsOpen(pull.url);
          await readCard(deps, pull);
          redraw(key);
        },
      }),
    );
  }
  actions.append(...linkButtons(deps.context, pull.url, card));
  card.appendChild(statusRow(sub, off, failed, actions));
  const more = full ? detailsRow(deps, full) : null;
  if (more) card.appendChild(more);
  if (full) keepConfirm(deps, full, card, actions, hooks);
  shown.set(key, { node: card, deps, pull });
  return card;
}

function when(epochSeconds: number): string {
  return new Date(epochSeconds * 1000).toLocaleString(undefined, {
    weekday: "short",
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
  });
}

/** A labelled group inside the section: a small heading, then its cards. */
function group(title: string, ...children: (HTMLElement | null)[]): HTMLElement {
  return el("div", { class: "ym-group" }, el("h3", { class: "ym-group-h" }, title), ...children);
}

function cardsOf(nodes: HTMLElement[]): HTMLElement {
  const cards = el("div", { class: "cards" });
  cards.append(...nodes);
  return cards;
}

/**
 * A due lot task as the lot draws it, one line (2026-10-14): "today" in the
 * warning colour ("resurfaces today" until then, which took the title's
 * width on a phone); an overdue task says since when, in the error colour.
 * Its parked sessions hang off it as in the lot, which no longer lists it
 * (2026-10-14, review).
 */
function dueCard(deps: Deps, task: DueTask, today: string, parked?: Session[]): HTMLElement {
  const card = taskCard(deps, task, { today, parked });
  if (task.overdue) {
    card.classList.add("is-overdue");
    if (task.resurfaces)
      card
        .querySelector(".resurface")
        ?.replaceChildren(dueIcon(), `overdue since ${shortDate(task.resurfaces)}`);
  }
  return card;
}

function pullsGroup(deps: Deps, data: YourMove): HTMLElement | null {
  const part = data.pulls;
  if (!part.ok) return notice(`Pull requests could not be read: ${part.error.message}`, "error");
  if (!part.configured) {
    // One quiet line: the full how-to lives in deploy/README.md.
    return el(
      "p",
      { class: "meta ym-off" },
      part.missing === "repos"
        ? "Pull requests are off: no repos to read. Set AE_GITHUB_REPOS to an owner/repo list."
        : 'Pull requests are off: the server has no GitHub token (deploy/README.md, "The GitHub token").',
    );
  }
  const errors = part.errors.map((failure) =>
    notice(`${failure.repo}: ${failure.message}`, "error"),
  );
  prune(part.items);
  // Merged from here: the result stays until Dismiss, the open card goes.
  const done = mergedCards(deps, "your-move");
  const open = part.items.filter((pull) => !wasMerged(pull.url));
  if (open.length === 0 && errors.length === 0 && done.length === 0) return null;
  return group(
    `Pull requests (${open.length})`,
    ...errors,
    cardsOf([...done, ...open.map((pull) => pullCard(deps, pull))]),
  );
}

function stewardBrief(deps: Deps, data: YourMove): HTMLElement | null {
  const part = data.steward;
  if (!part.ok) return notice(`The brief could not be read: ${part.error.message}`, "error");
  const brief = part.brief;
  if (!brief) return null;
  const details = el("details", { class: "brief" });
  details.appendChild(
    el(
      "summary",
      {},
      briefIcon(),
      el(
        "span",
        { class: "brief-when" },
        `Steward's brief, ${when(brief.message_at ?? brief.created_at)}`,
      ),
    ),
  );
  details.appendChild(
    el(
      "div",
      { class: "brief-text" },
      brief.text ??
        (brief.live_status === "running"
          ? "The steward is still writing."
          : "The run left no reply."),
    ),
  );
  if (brief.truncated) {
    details.appendChild(el("p", { class: "meta" }, "Cut short here; the session has the rest."));
  }
  details.appendChild(
    el(
      "div",
      { class: "actions" },
      button("Open the run", {
        onClick: () => deps.context.navigation.openSession(brief.session_id),
      }),
    ),
  );
  return details;
}

/**
 * The Dashboard's first section: open gates and approvals, due lot items,
 * pull requests, the brief. Each source renders its own error.
 */
export function renderYourMoveSection(
  host: HTMLElement,
  view: YourMoveView,
  deps: Deps,
  extras: YourMoveExtras = {},
): void {
  const data = view.data;
  if (!data) {
    const node = section("your-move", "Your move");
    body(node).appendChild(
      view.error
        ? notice(`Your move could not be read: ${describeError(view.error)}`, "error")
        : empty("Loading."),
    );
    replaceChildren(host, [node]);
    return;
  }
  const n = counts(data);
  const brief = stewardBrief(deps, data);
  const pulls = pullsGroup(deps, data);
  // Nothing waiting but a brief or a pull request note to show: "0" keeps the body.
  const count = n.total === 0 && (brief || pulls) ? "0" : n.total;
  const node = section("your-move", "Your move", count);
  const target = body(node);
  if (!data.sessions.ok) {
    target.appendChild(
      notice(`Sessions could not be read: ${data.sessions.error.message}`, "error"),
    );
  }
  const waiting = waitingSessions(data);
  if (waiting.length > 0) {
    target.appendChild(cardsOf(waiting.map((row) => sessionCard(deps, row, extras))));
  }
  const lot = data.lot;
  if (!lot.ok) {
    const kind = lot.error.code === "task_store_not_configured" ? "info" : "error";
    target.appendChild(notice(`task-store: ${lot.error.message}`, kind));
  } else if (lot.items.length > 0) {
    const parked = parkedByTask(extras.sessions ?? []);
    target.appendChild(
      group(
        `Due from the lot (${lot.items.length})`,
        cardsOf(lot.items.map((task) => dueCard(deps, task, lot.today, parked.get(task.id)))),
      ),
    );
  }
  if (pulls) target.appendChild(pulls);
  if (n.total === 0) target.appendChild(empty("Nothing is waiting on you."));
  if (brief) target.appendChild(brief);
  replaceChildren(host, [node]);
}
