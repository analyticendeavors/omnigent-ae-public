// The Session Dashboard (2026-09-24 as the Dashboard, renamed 2026-10-17;
// docs/design/dashboard-review.md): one page for
// the three-slot system, replacing the Parking Lot page and the Your move page
// (omnigent-ae Phase 3, 2026-09-23; Your move, 2026-09-24), which the owner found
// incongruent as separate entries. Top to bottom:
//
//   header      the Session Dashboard header (header.ts, 2026-10-17): a
//               read-out of the slots, pull requests, release, Your move and
//               the lot as blocks and pips, or as one sentence; the plan
//               limits under the title. It replaced the jump bar of section
//               chips, since the page already shows every section.
//   Your move   open gates, approvals, due lot items, pull requests, the brief
//   Open PRs    every open pull request a session tracks, with a Merge button
//               (prs.ts, 2026-09-24)
//   Working n/3, Blocked, Parking Lot (parked sessions on their notes' cards,
//   the Park form), then Review, Needs a note, Unsorted and Reference
//   (from 720 px the Parking Lot heads the right column, 2026-10-14)
//
// Every section collapses and is remembered per section (sections.ts);
// Reference and Unsorted start closed. Single column on the phone (375 to
// 430 px, 44 px targets), two independent columns from 720 px. Data
// refreshes on focus and every 20 seconds; each section renders its own
// empty or error state so one failing source never blanks the page.
//
// Header rethink (2026-09-25, https://github.com/analyticendeavors/omnigent-ae/issues/28):
// the strip above the jump bar (a Slots pill and one chip per working or
// blocked session) went, because the sidebar lists those sessions with their
// status and "Dashboard n/3", and the Working and Blocked sections list them
// again. What the sidebar does not have stays, in one row that uses the
// width: the section chips (wrapping on a wide screen instead of scrolling)
// and the plan limits.
//
// Declutter (2026-10-14, the owner's design pass): a section with nothing in it
// no longer draws a card in either column, nor a chip in the jump bar (a
// zero chip pointed at a heading that could not open). One muted line at the
// end, "Nothing in: Open PRs, Needs a note", says which ones are empty.
//
// Second pass (2026-10-14, the owner's approved spec): the Parking Lot heads the
// right column; sections and rows lose their frames; a session row's title
// opens it and its status moves sit in a "..." menu; and what Your move
// shows is not repeated in Working, Blocked or the lot.
//
// Fast open (2026-09-26): the owner waited 3 to 5 seconds on every open on their
// iPhone because the first render awaited all seven calls, so the slowest
// (the pull request scan, 2321 ms cold on the box) set the wait. Now each
// section renders as soon as its own call settles, and before any call lands
// the page paints what this device saw last (snapshot.ts), marked as
// refreshing and with every card's buttons off until that section is re-read:
// a Merge, a gate reply or a Resume is never drawn live from a saved copy.
//
// Both page contributions render this: `lot` (the route patch P1's Slots row
// links to) and `your-move` (kept so old bookmarks land here). The third,
// `fabric` (2026-10-16), is the Admin Center's Fabric section (fabric.ts): the same bundle, a
// different page, picked by the view the host names at activation.

import { defineExtension, type Disposable, type ExtensionContext } from "@omnigent/extension-sdk";
import "./style.css";
import {
  type Capacity,
  describeError,
  fetchCapacity,
  fetchLot,
  fetchLotTags,
  fetchPrs,
  fetchSessions,
  fetchYourMove,
  isLotAdminOnly,
  type Lot,
  type LotTags,
  type Session,
  statusOf,
  type YourMove,
} from "./api";
import { renderCapacity, slotMoves, slotsOf, workingRows } from "./capacity";
import { holdMenus } from "./cards";
import { el, notice, replaceChildren } from "./dom";
import {
  fetchRelease,
  type HeaderData,
  loadHeaderView,
  nextBack,
  lotCounts,
  type Release,
  renderHeader,
} from "./header";
import { loadLotState, openParkForm, renderLot } from "./lot";
import { loadLotFilter, localToday } from "./lotFilter";
import { fetchLimits, type Limits } from "./limits";
import { loadSectionState } from "./sections";
import {
  needsNote,
  renderNeedsNote,
  renderReference,
  renderReview,
  renderUnsorted,
} from "./sessions";
import { type PrsView, renderPrsSection, wasMerged, withoutCarded } from "./prs";
import { loadSnapshot, type Part, type Parts, saveSnapshot } from "./snapshot";
import { setTagStyles } from "./tags";
import { counts, renderYourMoveSection, shownInYourMove, type YourMoveView } from "./yourMove";
import { activateFabric, deactivateFabric } from "./fabric";

const REFRESH_MS = 20_000;
/** What a card's button says while its section shows this device's saved copy. */
export const REFRESHING_TITLE = "Refreshing: this turns on once the section is re-read.";

interface State {
  sessions: Session[];
  sessionsError: unknown;
  // The session list has answered at least once (2026-10-14): until then a
  // zero in Review or Reference means "not read yet", not "empty".
  sessionsLoaded: boolean;
  capacity: Capacity | null;
  capacityError: unknown;
  lot: Lot | null;
  lotError: unknown;
  yourMove: YourMoveView;
  prs: PrsView;
  // Plan limits for the top bar (2026-09-24); a failure only hides them.
  limits: Limits | null;
  // Tag colours and icons (2026-09-25); a failure keeps the last, or plain chips.
  tags: LotTags | null;
  // The header's release row (2026-10-17); a failure only hides it.
  release: Release | null;
  refreshedAt: number | null;
  // Parts drawn from this device's saved copy, not yet re-read (2026-09-26).
  cached: Set<Part>;
  // The remembered state (views, sections, the saved copy) has been read.
  ready: boolean;
}

/** The hosts that draw each part; their card buttons stay off while it is cached. */
const CACHED_HOSTS: Record<Part, (hosts: Hosts) => HTMLElement[]> = {
  yourMove: (hosts) => [hosts.yourMove],
  prs: (hosts) => [hosts.prs],
  lot: (hosts) => [hosts.lot],
  capacity: (hosts) => [hosts.working, hosts.blocked],
  tags: () => [],
  limits: () => [],
};

/**
 * Your move as drawn: a pull request Open PRs shows as a card is not listed
 * again. Filtered at render time, so when Your move lands before the pull
 * requests it renders at once and loses the carded ones when they arrive.
 */
function yourMoveView(state: State): YourMoveView {
  const data = state.yourMove.data;
  return { data: data ? withoutCarded(data, state.prs.data) : null, error: state.yourMove.error };
}

interface Hosts {
  head: HTMLElement;
  status: HTMLElement;
  yourMove: HTMLElement;
  prs: HTMLElement;
  working: HTMLElement;
  blocked: HTMLElement;
  lot: HTMLElement;
  review: HTMLElement;
  needsNote: HTMLElement;
  unsorted: HTMLElement;
  reference: HTMLElement;
  empties: HTMLElement;
  footer: HTMLElement;
}

let themeSubscription: Disposable | null = null;
let timer: ReturnType<typeof setInterval> | null = null;
let onFocus: (() => void) | null = null;

function layout(): Hosts {
  const root = document.getElementById("root");
  if (!root) throw new Error("Extension root is missing");
  const head = el("header", { class: "top" });
  const status = el("div", { class: "page-status", "aria-live": "polite" });
  const toast = el("div", { class: "page-status", id: "toast", "aria-live": "assertive" });
  const host = (name: string) => el("div", { class: `slot-${name}` });
  const hosts = {
    yourMove: host("your-move"),
    prs: host("prs"),
    working: host("working"),
    blocked: host("blocked"),
    lot: host("lot"),
    review: host("review"),
    needsNote: host("needs-note"),
    unsorted: host("unsorted"),
    reference: host("reference"),
    empties: host("empties"),
  };
  // The Parking Lot heads the side column (2026-10-14): under Blocked it left
  // the right column blank for most of the page. The phone keeps reading
  // order through an explicit `order` per slot (style.css).
  const grid = el(
    "main",
    { class: "grid" },
    el("div", { class: "col-main" }, hosts.yourMove, hosts.prs, hosts.working, hosts.blocked),
    el(
      "div",
      { class: "col-side" },
      hosts.lot,
      hosts.review,
      hosts.needsNote,
      hosts.unsorted,
      hosts.reference,
      hosts.empties,
    ),
  );
  const footer = el("footer", { class: "page-foot" });
  root.replaceChildren(head, status, toast, grid, footer);
  return { head, status, footer, ...hosts };
}

/** What the header reads out (header.ts); *yourMove* as drawn. */
function headerData(
  state: State,
  yourMove: YourMoveView,
  folded: ReadonlySet<string>,
  ready: boolean,
): HeaderData {
  const view = {
    capacity: state.capacity,
    capacityError: state.capacityError,
    sessions: state.sessions,
  };
  const { used, limit } = slotsOf(view);
  const openTasks = state.lot ? new Set(state.lot.tasks.map((task) => task.id)) : null;
  const count = (predicate: (session: Session) => boolean) =>
    state.sessions.filter(predicate).length;
  const today = state.capacity?.today ?? localToday();
  return {
    used,
    limit,
    working: workingRows(view).working,
    yourMove: yourMove.data ? counts(yourMove.data).total : undefined,
    // A pull request merged from this page leaves the header when it leaves the section.
    prs: state.prs.data?.configured
      ? state.prs.data.items.filter((card) => !wasMerged(card.url))
      : null,
    lot: state.lot
      ? {
          count: state.lot.count,
          next: nextBack(state.lot.tasks, today),
          ...lotCounts(state.lot.tasks, today),
        }
      : null,
    release: state.release,
    needsNote: count((session) => needsNote(session, openTasks) !== null),
    reference: count((session) => statusOf(session) === "reference"),
    limits: state.limits,
    folded,
    ready,
  };
}

function render(
  hosts: Hosts,
  state: State,
  appDeps: {
    context: ExtensionContext;
    refresh: (fresh?: boolean) => Promise<void>;
    parkSession: (sessionId: string) => void;
  },
): void {
  // A non-admin has no Parking Lot (2026-10-28): nothing to file a note into, so no
  // Park form button and no "Needs a note" list; Park on a working card sets the status.
  const lotAdminOnly = isLotAdminOnly(state.lotError);
  const deps = lotAdminOnly ? { ...appDeps, parkSession: undefined } : appDeps;
  // An open "..." menu and the focus on one, put back once the page is drawn
  // (2026-10-14, review): the last step below.
  const keepMenus = holdMenus();
  // Each item once (2026-10-14): Your move decides first, and what it shows
  // is passed down so Working, Blocked and the lot leave it out. Display
  // only: the slot count and every heading's count stay the true ones.
  // Only a fresh answer decides (2026-10-14, review): a saved copy has its
  // buttons off and a failed refresh may be stale, so then nothing is left out.
  const yourMove = yourMoveView(state);
  const fresh = yourMove.error || state.cached.has("yourMove") ? null : yourMove.data;
  const inYourMove = fresh ? shownInYourMove(fresh) : null;
  const view = {
    capacity: state.capacity,
    capacityError: state.capacityError,
    sessions: state.sessions,
    inYourMove: inYourMove?.sessions,
  };
  renderCapacity({ working: hosts.working, blocked: hosts.blocked }, view, deps);
  hosts.status.replaceChildren();
  if (state.sessionsError) {
    hosts.status.appendChild(
      notice(`Sessions could not be listed: ${describeError(state.sessionsError)}`, "error"),
    );
  }
  if (state.cached.size > 0) {
    hosts.status.appendChild(
      el("p", { class: "meta refreshing" }, "Showing this device's last view; refreshing."),
    );
  }
  if (state.yourMove.data && state.yourMove.error) {
    hosts.status.appendChild(
      notice(
        `Your move shows the last answer; the refresh failed: ${describeError(state.yourMove.error)}`,
        "error",
      ),
    );
  }
  // Before Your move: its due cards draw tag chips too.
  setTagStyles(state.tags);
  // A session it took out of Working or Blocked keeps their menu, a due task
  // its parked sessions (2026-10-14, review).
  renderYourMoveSection(hosts.yourMove, yourMove, deps, {
    sessions: state.sessions,
    moves: (row) => slotMoves(view, deps, row),
  });
  renderPrsSection(hosts.prs, state.prs, deps);
  renderLot(
    hosts.lot,
    {
      lot: state.lot,
      lotError: state.lotError,
      sessions: state.sessions,
      today: state.capacity?.today ?? null,
      inYourMove: inYourMove?.tasks,
    },
    deps,
  );
  renderReview(hosts.review, state.sessions, deps);
  const openTasks = state.lot ? new Set(state.lot.tasks.map((task) => task.id)) : null;
  renderNeedsNote(hosts.needsNote, lotAdminOnly ? [] : state.sessions, openTasks, deps);
  renderUnsorted(hosts.unsorted, state.sessions, deps);
  renderReference(hosts.reference, state.sessions, deps);
  const folded = foldEmpties(hosts, state);
  // Your move as drawn above: worked out once per paint (2026-10-14, review).
  renderHeader(hosts.head, deps.context, headerData(state, yourMove, folded, state.ready), () =>
    render(hosts, state, appDeps),
  );
  replaceChildren(hosts.footer, [
    state.refreshedAt === null
      ? null
      : el(
          "p",
          { class: "meta refreshed" },
          `Refreshed ${new Date(state.refreshedAt).toLocaleTimeString()}; again in ${REFRESH_MS / 1000} s or on focus.`,
        ),
  ]);
  markCached(hosts, state.cached);
  keepMenus();
}

/** The section hosts in page order with their labels, for folding the empty ones. */
const SECTION_HOSTS: [keyof Hosts, string][] = [
  ["yourMove", "Your move"],
  ["prs", "Open PRs"],
  ["working", "Working"],
  ["blocked", "Blocked"],
  ["lot", "Parking Lot"],
  ["review", "Review"],
  ["needsNote", "Needs a note"],
  ["unsorted", "Unsorted"],
  ["reference", "Reference"],
];

/**
 * Section ids whose source answered fresh and without an error, so a zero
 * there is a real zero. A section still loading, failed, or drawn from this
 * device's saved copy keeps its dimmed heading instead of being folded.
 */
function answered(state: State): Set<string> {
  const ids = new Set<string>();
  const sessions = state.sessionsLoaded && !state.sessionsError;
  if (state.yourMove.data && !state.yourMove.error && !state.cached.has("yourMove"))
    ids.add("your-move");
  if (state.prs.data && !state.prs.error && !state.cached.has("prs")) ids.add("prs");
  // Blocked comes from the capacity route when it answered, else from the labels.
  if (state.capacity ? !state.cached.has("capacity") : sessions) ids.add("blocked");
  if (sessions) for (const id of ["review", "needs-note", "unsorted", "reference"]) ids.add(id);
  return ids;
}

/**
 * Hide every answered section whose count is zero (dom.ts marks it
 * `is-empty`) and name them on one line instead. Returns the folded ids.
 */
function foldEmpties(hosts: Hosts, state: State): Set<string> {
  const ready = answered(state);
  const folded = new Set<string>();
  const listed: string[] = [];
  for (const [key, name] of SECTION_HOSTS) {
    const node = hosts[key].querySelector<HTMLElement>(":scope > .section");
    const fold = !!node && node.classList.contains("is-empty") && ready.has(node.id);
    hosts[key].hidden = fold;
    if (fold && node) {
      folded.add(node.id);
      listed.push(name);
    }
  }
  replaceChildren(hosts.empties, [
    listed.length > 0 ? el("p", { class: "empties meta" }, `Nothing in: ${listed.join(", ")}`) : null,
  ]);
  return folded;
}

/** Dim the sections drawn from the saved copy and turn their card buttons off. */
function markCached(hosts: Hosts, cached: Set<Part>): void {
  const stale = new Set([...cached].flatMap((part) => CACHED_HOSTS[part](hosts)));
  for (const part of Object.keys(CACHED_HOSTS) as Part[]) {
    for (const host of CACHED_HOSTS[part](hosts)) {
      host.classList.toggle("is-refreshing", stale.has(host));
    }
  }
  for (const host of stale) {
    for (const node of host.querySelectorAll<HTMLButtonElement>("article button.btn")) {
      node.disabled = true;
      node.title = REFRESHING_TITLE;
    }
  }
}

type Settled<T> = { ok: true; value: T } | { ok: false; reason: unknown };

function settled<T>(promise: Promise<T>): Promise<Settled<T>> {
  return promise.then(
    (value) => ({ ok: true, value }),
    (reason: unknown) => ({ ok: false, reason }),
  );
}

/**
 * Start every call; each one updates *state* and calls *paint* as soon as it
 * settles, so one slow source never holds back the others (2026-09-26).
 * Resolves once all seven have settled.
 */
function load(
  context: ExtensionContext,
  state: State,
  fresh: boolean,
  paint: () => void,
  fetched: Set<Part>,
): Promise<void> {
  const on = <T>(promise: Promise<T>, apply: (result: Settled<T>) => void, part?: Part) =>
    settled(promise).then((result) => {
      if (part) fetched.add(part);
      apply(result);
      paint();
    });
  // A good answer replaces the saved copy and is kept for the next open.
  const fresher = <P extends Part>(part: P, value: Parts[P]) => {
    state.cached.delete(part);
    saveSnapshot(context, part, value);
  };
  return Promise.all([
    on(fetchSessions(context), (result) => {
      if (result.ok) {
        state.sessions = result.value.filter((session) => !session.archived);
        state.sessionsError = null;
        state.sessionsLoaded = true;
      } else {
        state.sessionsError = result.reason;
      }
    }),
    on(
      fetchCapacity(context),
      (result) => {
        state.cached.delete("capacity");
        if (result.ok) {
          state.capacity = result.value;
          state.capacityError = null;
          fresher("capacity", result.value);
        } else {
          state.capacity = null;
          state.capacityError = result.reason;
        }
      },
      "capacity",
    ),
    on(
      fetchLot(context),
      (result) => {
        state.cached.delete("lot");
        if (result.ok) {
          state.lot = result.value;
          state.lotError = null;
          fresher("lot", result.value);
        } else {
          state.lot = null;
          state.lotError = result.reason;
        }
      },
      "lot",
    ),
    on(
      fetchYourMove(context, fresh),
      (result) => {
        // A failed refresh keeps the last answer and says so; a saved copy
        // stays marked, its buttons off.
        if (result.ok) {
          state.yourMove = { data: result.value, error: null };
          fresher("yourMove", result.value);
        } else {
          state.yourMove = { data: state.yourMove.data, error: result.reason };
        }
      },
      "yourMove",
    ),
    on(
      fetchLimits(context),
      (result) => {
        state.cached.delete("limits");
        state.limits = result.ok ? result.value : null;
        if (result.ok) fresher("limits", result.value);
      },
      "limits",
    ),
    on(
      fetchPrs(context, fresh),
      (result) => {
        // A failed refresh keeps the last cards and says so; saved cards keep
        // Merge off until a fresh answer lands.
        if (result.ok) {
          state.prs = { data: result.value, error: null };
          fresher("prs", result.value);
        } else {
          state.prs = { data: state.prs.data, error: result.reason };
        }
      },
      "prs",
    ),
    on(fetchRelease(context), (result) => {
      state.release = result.ok ? result.value : null;
    }),
    on(
      fetchLotTags(context),
      (result) => {
        // Styling only: a failure keeps the last tags, saved or not.
        state.cached.delete("tags");
        if (result.ok) {
          state.tags = result.value;
          fresher("tags", result.value);
        }
      },
      "tags",
    ),
  ]).then(() => {
    state.refreshedAt = Date.now();
  });
}

/** Put this device's saved copy of every part no call has answered yet into *state*. */
function applySnapshot(state: State, saved: Partial<Parts>, fetched: Set<Part>): void {
  const take = <P extends Part>(part: P, put: (value: Parts[P]) => void) => {
    const value = saved[part];
    if (value === undefined || fetched.has(part)) return;
    put(value as Parts[P]);
    state.cached.add(part);
  };
  take("yourMove", (value: YourMove) => {
    state.yourMove = { data: value, error: null };
  });
  take("prs", (value) => {
    state.prs = { data: value, error: null };
  });
  take("lot", (value) => {
    state.lot = value;
  });
  take("tags", (value) => {
    state.tags = value;
  });
  take("capacity", (value) => {
    state.capacity = value;
  });
  take("limits", (value) => {
    state.limits = value;
  });
}

export async function activateDashboard(context: ExtensionContext): Promise<void> {
  const hosts = layout();
  const state: State = {
    sessions: [],
    sessionsError: null,
    sessionsLoaded: false,
    capacity: null,
    capacityError: null,
    lot: null,
    lotError: null,
    yourMove: { data: null, error: null },
    prs: { data: null, error: null },
    limits: null,
    tags: null,
    release: null,
    refreshedAt: null,
    cached: new Set(),
    ready: false,
  };
  // Parts a call has answered; the saved copy never overwrites one of them.
  const fetched = new Set<Part>();
  // Which lot cards and sections are open and the lot's filter, read once, and
  // this device's saved copy of the last answers; the first data render waits
  // for all of them so remembered state never flashes. All are local reads.
  const ready = Promise.all([
    loadLotState(context),
    loadSectionState(context),
    loadLotFilter(context),
    loadHeaderView(context),
    loadSnapshot(context).then((saved) => applySnapshot(state, saved, fetched)),
  ]).then(() => {
    state.ready = true;
    render(hosts, state, deps);
  });
  const paint = () => {
    void ready.then(() => render(hosts, state, deps));
  };
  let inFlight: Promise<void> | null = null;
  const refresh = (fresh = false): Promise<void> => {
    if (inFlight) return inFlight;
    // Section by section on the first load; afterwards (the poll, focus, after
    // an action) one render when all have settled, as before, so a page
    // already on screen is not redrawn seven times every 20 seconds.
    const each = state.refreshedAt === null ? paint : () => undefined;
    inFlight = Promise.all([load(context, state, fresh, each, fetched), ready])
      .then(() => render(hosts, state, deps))
      .finally(() => {
        inFlight = null;
      });
    return inFlight;
  };
  const deps = { context, refresh, parkSession: openParkForm };
  render(hosts, state, deps);
  await start(context, () => refresh());
}

/** Theme, then the first load and the refresh cadence. */
async function start(context: ExtensionContext, refresh: () => Promise<void>): Promise<void> {
  const theme = await context.theme.getCurrent();
  document.documentElement.dataset.theme = theme.theme;
  themeSubscription = await context.theme.subscribe((next) => {
    document.documentElement.dataset.theme = next.theme;
  });

  // Ready first, data second: the host allows ten seconds for activation
  // and a slow server must not turn into "activation timed out".
  void refresh();
  timer = setInterval(() => void refresh(), REFRESH_MS);
  onFocus = () => {
    if (document.visibilityState === "visible") void refresh();
  };
  window.addEventListener("focus", onFocus);
  document.addEventListener("visibilitychange", onFocus);
}

export function deactivateDashboard(): void {
  themeSubscription?.dispose();
  themeSubscription = null;
  if (timer) clearInterval(timer);
  timer = null;
  if (onFocus) {
    window.removeEventListener("focus", onFocus);
    document.removeEventListener("visibilitychange", onFocus);
  }
  onFocus = null;
}

defineExtension({
  async activate(context) {
    if (context.view === "fabric") await activateFabric(context);
    else await activateDashboard(context);
  },
  deactivate() {
    deactivateFabric();
    deactivateDashboard();
  },
});
