// Fast open (2026-09-26): each section renders as its own call settles, and
// the page first paints this device's saved copy with every card button off
// (Merge above all) until that section is re-read.
import type { ExtensionContext } from "@omnigent/extension-sdk";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type {
  Capacity,
  CapacitySession,
  Lot,
  OpenPrs,
  PrCard,
  Session,
  YourMove,
} from "../src/api";
import { resetMenuState } from "../src/cards";
import { resetLotState } from "../src/lot";
import { resetPrsState } from "../src/prs";
import { resetSectionState } from "../src/sections";
import { resetSnapshotState, SNAPSHOT_VERSION, snapshotKey } from "../src/snapshot";
import { resetYourMoveState } from "../src/yourMove";

vi.mock("@omnigent/extension-sdk", () => ({ defineExtension: vi.fn() }));

const { activateDashboard, deactivateDashboard, REFRESHING_TITLE } = await import("../src/main");

const SHA = "a1b2c3d4e5f60718293a4b5c6d7e8f9012345678";
const URL_23 = "https://github.com/analyticendeavors/omnigent-ae/pull/23";

function card(number: number, extra: Partial<PrCard> = {}): PrCard {
  return {
    repo: "analyticendeavors/omnigent-ae",
    number,
    url: `https://github.com/analyticendeavors/omnigent-ae/pull/${number}`,
    title: `PR ${number}`,
    state: "open",
    draft: false,
    author: "ae-reid-havens",
    head_ref: `claude/branch-${number}`,
    base_ref: "main",
    head_sha: SHA,
    updated_at: "2026-09-24T10:00:00Z",
    ci: { state: "passing", passing: 6, failing: 0, pending: 0, total: 6 },
    mergeable: { state: "clean", detail: "No conflicts with the base branch." },
    review: { state: "none", approvals: 0, changes_requested: 0, requested: [] },
    merge: { methods: ["squash", "merge"], default_method: "squash", allowed: true, reason: null },
    sessions: [{ id: "s1", title: "Merge button", status: "working" }],
    ...extra,
  };
}

const PRS: OpenPrs = { configured: true, items: [card(23)], errors: [], sessions_scanned: 3 };

const YOUR_MOVE: YourMove = {
  now: "2026-09-26T09:00:00-07:00",
  today: "2026-09-26",
  timezone: "America/Los_Angeles",
  total: 2,
  cached: false,
  sessions: {
    ok: true,
    counts: { gate: 1, awaiting: 0, blocked: 0, review: 0 },
    items: [
      {
        id: "conv_gate",
        title: "Pricing padding",
        kind: "gate",
        kinds: ["gate"],
        status: "working",
        fork: "EUR formatting is broken",
        next: null,
        task: null,
        pending: 0,
        updated_at: 1,
        since: "12m",
      },
    ],
  },
  lot: { ok: true, today: "2026-09-26", items: [] },
  steward: { ok: true, agent: "steward", brief: null },
  pulls: {
    ok: true,
    configured: true,
    login: "ae-reid-havens",
    repos: ["analyticendeavors/omnigent-ae"],
    errors: [],
    items: [
      {
        repo: "analyticendeavors/omnigent-ae",
        number: 23,
        title: "Add the Your move panel",
        url: URL_23,
        author: "ae-reid-havens",
        draft: false,
        updated_at: null,
        why: ["author"],
        ci: "passing",
      },
    ],
  },
};

const LOT: Lot = {
  list: { id: "L", name: "Parking Lot" },
  count: 1,
  tasks: [
    {
      id: "T-4",
      title: "sidebar-search-shortcut",
      quadrant: "q3",
      estimate: "15",
      status: "open",
      tags: [],
      created_at: null,
      resurfaces: "2026-09-30",
      next: "Open the sidebar file",
      links: [],
      url: "https://tasks.example/",
    },
  ],
};

const CAPACITY = {
  limit: 3,
  slots_used: 0,
  working: [],
  blocked: [],
  review: [],
  parked: [],
  reference: [],
  unlabelled_running: [],
  today: "2026-09-26",
} as Capacity;

/** A capacity row (Working or Blocked). */
function slot(id: string, title: string, blocked = false): CapacitySession {
  return {
    id,
    title,
    status: blocked ? "blocked" : "working",
    live_status: null,
    blocked,
    started: 1,
    elapsed_s: 60,
    elapsed: "1m",
    next: null,
    gate: null,
    fork: null,
    overflow: false,
    task: null,
  };
}

/** Your move's gate (Working holds the session) and approval (Blocked holds it). */
const GATE_AND_ASK: YourMove["sessions"] = {
  ok: true,
  counts: { gate: 1, awaiting: 1, blocked: 0, review: 0 },
  items: [
    {
      id: "conv_gate",
      title: "Pricing padding",
      kind: "gate",
      kinds: ["gate"],
      status: "working",
      fork: "EUR formatting is broken",
      next: null,
      task: null,
      pending: 0,
      updated_at: 1,
      since: "12m",
    },
    {
      id: "conv_ask",
      title: "Renewal note",
      kind: "awaiting",
      kinds: ["awaiting"],
      status: "working",
      fork: null,
      next: null,
      task: null,
      pending: 1,
      updated_at: 1,
      since: "3m",
    },
  ],
};

/** Capacity with the gate and one more session working, the approval blocked. */
const BUSY: Capacity = {
  ...CAPACITY,
  slots_used: 2,
  working: [slot("conv_gate", "Pricing padding"), slot("conv_other", "Sidebar fix")],
  blocked: [slot("conv_ask", "Renewal note", true)],
};

type Reply = { ok: boolean; status: number; body: unknown };

/** One pending server answer per path, settled by the test. */
function deferred() {
  let settle!: (reply: Reply) => void;
  const promise = new Promise<Reply>((resolve) => {
    settle = resolve;
  });
  return { promise, settle };
}

const PATHS = {
  capacity: "/v1/ae/capacity",
  lot: "/v1/ae/lot?status=open&limit=100",
  yourMove: "/v1/ae/your-move",
  limits: "/v1/ae/limits",
  prs: "/v1/ae/prs",
  tags: "/v1/ae/lot/tags?icons=true",
} as const;

type Name = keyof typeof PATHS;

function stubContext(saved: Record<string, unknown> = {}) {
  const pending = Object.fromEntries(
    (Object.keys(PATHS) as Name[]).map((name) => [name, deferred()]),
  ) as Record<Name, ReturnType<typeof deferred>>;
  const byPath = new Map<string, Name>(
    (Object.entries(PATHS) as [Name, string][]).map(([name, path]) => [path, name]),
  );
  const request = vi.fn(async ({ path }: { path: string }) => {
    const name = byPath.get(path);
    if (!name) return { ok: true, status: 200, body: {} };
    return pending[name].promise;
  });
  const store = new Map<string, unknown>(Object.entries(saved));
  const set = vi.fn(async (key: string, value: unknown) => {
    store.set(key, value);
  });
  const context = {
    server: { request },
    sessions: { listAll: vi.fn(async () => []) },
    navigation: { openSession: vi.fn(), openNewSession: vi.fn(), openPage: vi.fn() },
    storage: {
      user: {
        get: vi.fn(async (key: string) => store.get(key) ?? null),
        set,
        delete: vi.fn(async (key: string) => {
          store.delete(key);
        }),
      },
    },
    theme: {
      getCurrent: vi.fn(async () => ({ theme: "light" })),
      subscribe: vi.fn(async () => ({ dispose: vi.fn() })),
    },
  } as unknown as ExtensionContext;
  const answer = (name: Name, body: unknown) =>
    pending[name].settle({ ok: true, status: 200, body });
  const fail = (name: Name, status = 502) =>
    pending[name].settle({
      ok: false,
      status,
      body: { error: { code: "down", message: "task-store did not answer" } },
    });
  return { context, answer, fail, set, store };
}

const flush = async () => {
  for (let i = 0; i < 5; i++) await new Promise((resolve) => setTimeout(resolve, 0));
};

const saved = (data: unknown) => ({ v: SNAPSHOT_VERSION, at: 1, data });

function section(id: string): HTMLElement {
  return document.getElementById(id) as HTMLElement;
}

function mergeButtons(root: ParentNode): HTMLButtonElement[] {
  return [...root.querySelectorAll<HTMLButtonElement>("button")].filter(
    (node) => node.textContent?.trim() === "Merge",
  );
}

/** A session as the host lists it. */
function listed(id: string, title: string, labels: Record<string, string>): Session {
  return {
    id,
    title,
    status: "idle",
    unread: false,
    titleProvisional: false,
    workspace: null,
    gitBranch: null,
    projectId: null,
    labels,
    archived: false,
    createdAt: 1,
    updatedAt: 1,
  };
}

/** Answer every route (*parts* overrides), so the first load settles and a later refresh runs. */
function answerAll(
  answer: (name: Name, body: unknown) => void,
  parts: Partial<Record<Name, unknown>> = {},
): void {
  const bodies: Record<Name, unknown> = {
    capacity: CAPACITY,
    lot: LOT,
    yourMove: YOUR_MOVE,
    limits: { now: "x", harnesses: {} },
    prs: { ...PRS, items: [] },
    tags: { count: 0, tags: [] },
    ...parts,
  };
  for (const name of Object.keys(PATHS) as Name[]) answer(name, bodies[name]);
}

/** The items of a row's "..." menu in *sectionId*. */
function menuItems(sectionId: string, sessionId: string): HTMLButtonElement[] {
  const row = section(sectionId).querySelector<HTMLElement>(`[data-session="${sessionId}"]`)!;
  const trigger = row.querySelector<HTMLButtonElement>(".btn-menu")!;
  const menu = document.getElementById(trigger.getAttribute("aria-controls")!)!;
  return [...menu.querySelectorAll<HTMLButtonElement>('[role="menuitem"]')];
}

/** Open a row's "..." menu and press its item named *text*. */
function pick(sectionId: string, sessionId: string, text: string): void {
  const row = section(sectionId).querySelector<HTMLElement>(`[data-session="${sessionId}"]`)!;
  row.querySelector<HTMLButtonElement>(".btn-menu")!.click();
  menuItems(sectionId, sessionId)
    .find((node) => node.textContent === text)!
    .click();
}

describe("the Dashboard's first paint", () => {
  beforeEach(() => {
    resetLotState();
    resetSectionState();
    resetPrsState();
    resetYourMoveState();
    resetSnapshotState();
    resetMenuState();
    document.body.replaceChildren();
    const root = document.createElement("div");
    root.id = "root";
    document.body.appendChild(root);
  });

  afterEach(() => {
    deactivateDashboard();
    vi.restoreAllMocks();
  });

  it("renders each section as its own call settles, not after the slowest", async () => {
    const { context, answer } = stubContext();
    await activateDashboard(context);
    answer("yourMove", YOUR_MOVE);
    answer("lot", LOT);
    await flush();
    // Pull requests are still out: Your move and the lot are already drawn.
    expect(section("your-move").querySelector('[data-session="conv_gate"]')).not.toBeNull();
    expect(section("lot").querySelector('[data-task="T-4"]')).not.toBeNull();
    expect(section("prs").textContent).toContain("Loading.");
    // Until Open PRs lands, #23 is listed under Your move.
    expect(section("your-move").querySelector(`[data-pr="${URL_23}"]`)).not.toBeNull();
    answer("prs", PRS);
    await flush();
    expect(section("prs").querySelector(`[data-pr="${URL_23}"]`)).not.toBeNull();
    // Now it is a card in Open PRs, so it is not repeated under Your move.
    expect(section("your-move").querySelector(`[data-pr="${URL_23}"]`)).toBeNull();
    expect(document.querySelector(".page-foot .refreshed")).toBeNull();
  });

  it("paints the saved copy first, with Merge off until the fresh card arrives", async () => {
    const { context, answer } = stubContext({
      [snapshotKey("prs")]: saved(PRS),
      [snapshotKey("lot")]: saved(LOT),
      [snapshotKey("yourMove")]: saved(YOUR_MOVE),
    });
    await activateDashboard(context);
    await flush();
    // Nothing has answered; the saved copy is on screen, marked as refreshing.
    const prs = section("prs");
    const [merge] = mergeButtons(prs);
    expect(merge.disabled).toBe(true);
    expect(merge.title).toBe(REFRESHING_TITLE);
    expect(prs.parentElement?.classList.contains("is-refreshing")).toBe(true);
    expect(section("lot").querySelector('[data-task="T-4"]')).not.toBeNull();
    const gate = [...section("your-move").querySelectorAll<HTMLButtonElement>("button.btn")];
    expect(gate.length).toBeGreaterThan(0);
    expect(gate.every((node) => node.disabled)).toBe(true);
    expect(document.querySelector(".page-status .refreshing")?.textContent).toContain(
      "refreshing",
    );
    // Section toggles still work on the saved copy.
    expect(prs.querySelector<HTMLButtonElement>(".section-toggle")?.disabled).toBe(false);

    answer("lot", LOT);
    await flush();
    // Another section landing redraws Open PRs from the saved copy: still off.
    expect(mergeButtons(section("prs"))[0].disabled).toBe(true);

    answer("prs", { ...PRS, items: [card(23, { title: "Fresh" })] });
    await flush();
    const fresh = mergeButtons(section("prs"))[0];
    expect(fresh.disabled).toBe(false);
    expect(section("prs").textContent).toContain("Fresh");
    expect(section("prs").parentElement?.classList.contains("is-refreshing")).toBe(false);
    // Your move is still the saved copy.
    expect(document.querySelector(".page-status .refreshing")).not.toBeNull();
    answer("yourMove", YOUR_MOVE);
    await flush();
    expect(document.querySelector(".page-status .refreshing")).toBeNull();
  });

  it("keeps saved cards with Merge off when the refresh fails", async () => {
    const { context, fail } = stubContext({ [snapshotKey("prs")]: saved(PRS) });
    await activateDashboard(context);
    fail("prs");
    await flush();
    const prs = section("prs");
    expect(prs.textContent).toContain("Showing the last answer; the refresh failed");
    expect(mergeButtons(prs)[0].disabled).toBe(true);
  });

  it("never lets a saved copy overwrite an answer that already landed", async () => {
    const { context, answer } = stubContext({
      [snapshotKey("prs")]: saved({ ...PRS, items: [card(23, { title: "Saved" })] }),
    });
    const activating = activateDashboard(context);
    answer("prs", { ...PRS, items: [card(23, { title: "Live" })] });
    await activating;
    await flush();
    expect(section("prs").textContent).toContain("Live");
    expect(mergeButtons(section("prs"))[0].disabled).toBe(false);
  });

  it("keeps each fresh answer for the next open", async () => {
    const { context, answer, set, store } = stubContext();
    await activateDashboard(context);
    answer("prs", PRS);
    answer("capacity", CAPACITY);
    answer("lot", LOT);
    answer("yourMove", YOUR_MOVE);
    answer("limits", { now: "x", harnesses: {} });
    answer("tags", { count: 0, tags: [] });
    await flush();
    expect(store.get(snapshotKey("prs"))).toMatchObject({ v: SNAPSHOT_VERSION, data: PRS });
    expect(store.get(snapshotKey("lot"))).toMatchObject({ data: LOT });
    expect(document.querySelector(".page-foot .refreshed")).not.toBeNull();
    const writes = set.mock.calls.length;
    expect(writes).toBe(6);
  });

  it("ignores a saved copy in an older shape", async () => {
    const { context } = stubContext({
      [snapshotKey("prs")]: { v: SNAPSHOT_VERSION - 1, at: 1, data: PRS },
      [snapshotKey("lot")]: saved({ nope: true }),
    });
    await activateDashboard(context);
    await flush();
    expect(section("prs").textContent).toContain("Loading.");
    expect(section("lot").textContent).toContain("Loading the lot.");
    expect(document.querySelector(".page-status .refreshing")).toBeNull();
  });
  it("folds empty sections into one line under the Session Dashboard header", async () => {
    const { context, answer } = stubContext();
    await activateDashboard(context);
    answer("yourMove", YOUR_MOVE);
    answer("lot", LOT);
    answer("prs", { ...PRS, items: [] });
    answer("capacity", CAPACITY);
    await flush();
    // No sessions and no open pull requests: six sections are empty.
    for (const id of ["prs", "blocked", "review", "needs-note", "unsorted", "reference"]) {
      expect(section(id).parentElement?.hidden).toBe(true);
    }
    expect(section("your-move").parentElement?.hidden).toBe(false);
    expect(section("lot").parentElement?.hidden).toBe(false);
    expect(document.querySelector(".empties")?.textContent).toBe(
      "Nothing in: Open PRs, Blocked, Review, Needs a note, Unsorted, Reference",
    );
    // The header reads out instead of jumping (2026-10-17).
    expect(document.querySelector(".jump")).toBeNull();
    expect(document.querySelector(".top .hd-title")?.textContent).toBe("Session Dashboard");
  });
  it("never folds a section that has not answered or failed", async () => {
    const { context, answer, fail } = stubContext();
    // The session list never answers; the pull request scan fails.
    (context.sessions as { listAll: unknown }).listAll = vi.fn(() => new Promise(() => {}));
    await activateDashboard(context);
    answer("yourMove", YOUR_MOVE);
    answer("lot", LOT);
    answer("capacity", CAPACITY);
    fail("prs");
    await flush();
    for (const id of ["prs", "review", "needs-note", "unsorted", "reference"]) {
      expect(section(id).parentElement?.hidden).toBe(false);
    }
    // Blocked came from the capacity route, which answered: a real zero.
    expect(document.querySelector(".empties")?.textContent).toBe("Nothing in: Blocked");
  });

  // Second pass (2026-10-14): the Parking Lot heads the side column; the
  // phone's one column keeps reading order (style.css, layout.test.ts).
  it("puts the Parking Lot at the head of the side column", async () => {
    const { context } = stubContext();
    await activateDashboard(context);
    const slots = (column: string) =>
      [...document.querySelectorAll(`${column} > [class^="slot-"]`)].map((node) => node.className);
    expect(slots(".col-main")).toEqual(["slot-your-move", "slot-prs", "slot-working", "slot-blocked"]);
    expect(slots(".col-side")).toEqual([
      "slot-lot",
      "slot-review",
      "slot-needs-note",
      "slot-unsorted",
      "slot-reference",
      "slot-empties",
    ]);
  });

  // Each item once (2026-10-14): what Your move shows is not drawn again.
  it("leaves Your move's gate, approval and due task out of Working, Blocked and the lot", async () => {
    const { context, answer } = stubContext();
    await activateDashboard(context);
    const due = { ...LOT.tasks[0], resurfaces: "2026-09-26" };
    answer("yourMove", {
      ...YOUR_MOVE,
      sessions: GATE_AND_ASK,
      lot: { ok: true, today: "2026-09-26", items: [{ ...due, overdue: false }] },
    });
    answer("capacity", BUSY);
    answer("lot", {
      ...LOT,
      count: 2,
      tasks: [due, { ...LOT.tasks[0], id: "T-5", title: "graphiti-dedupe" }],
    });
    answer("prs", { ...PRS, items: [] });
    await flush();
    const note = (id: string) => section(id).querySelector(".section-h .section-note")?.textContent;
    const count = (id: string) => section(id).querySelector(".section-h .count")?.textContent;
    // Your move shows them...
    expect(section("your-move").querySelector('[data-session="conv_gate"]')).not.toBeNull();
    expect(section("your-move").querySelector('[data-session="conv_ask"]')).not.toBeNull();
    expect(section("your-move").querySelector('[data-task="T-4"]')).not.toBeNull();
    // ...so Working leaves the gate out and says where it went; the slots stay.
    expect(section("working").querySelector('[data-session="conv_gate"]')).toBeNull();
    expect(section("working").querySelector('[data-session="conv_other"]')).not.toBeNull();
    expect(count("working")).toBe("2/3");
    expect(note("working")).toBe("1 in Your move");
    expect(document.querySelector(".hd-flow h2 b")?.textContent).toBe("2 of 3");
    // Blocked's only session is in Your move: the note, no rows, no empty state.
    expect(count("blocked")).toBe("1");
    expect(note("blocked")).toBe("In Your move");
    expect(section("blocked").querySelector("[data-session], .empty")).toBeNull();
    expect(section("blocked").parentElement?.hidden).toBe(false);
    // The lot lists the other task only, with the true count.
    expect(section("lot").querySelector('[data-task="T-4"]')).toBeNull();
    expect(section("lot").querySelector('[data-task="T-5"]')).not.toBeNull();
    expect(count("lot")).toBe("2");
    expect(note("lot")).toBe("1 in Your move");
    // A search looks at every task, so it still finds the one Your move shows,
    // and the heading's note goes while it is on (2026-10-14, review).
    const search = section("lot").querySelector<HTMLInputElement>(".lot-search-input")!;
    search.value = "sidebar-search";
    search.dispatchEvent(new Event("input"));
    expect(section("lot").querySelector('[data-task="T-4"]')).not.toBeNull();
    expect(note("lot")).toBeUndefined();
    expect(count("lot")).toBe("2");
    search.value = "";
    search.dispatchEvent(new Event("input"));
    expect(section("lot").querySelector('[data-task="T-4"]')).toBeNull();
    expect(note("lot")).toBe("1 in Your move");
  });

  // Review (2026-10-14): a move that takes its row away hands the focus to
  // the heading of the section the row was in, or to the next row when that
  // section folds; never to body.
  it("keeps the focus on the page when a move takes its row away", async () => {
    const { context, answer } = stubContext();
    const pricing = listed("conv_w", "Pricing fix", { "ae.status": "working" });
    const renewal = listed("conv_b", "Renewal note", { "ae.status": "blocked" });
    (context.sessions as { listAll: unknown }).listAll = vi.fn(async () => [pricing, renewal]);
    const capacity: Capacity = {
      ...CAPACITY,
      slots_used: 1,
      working: [slot("conv_w", "Pricing fix")],
      blocked: [slot("conv_b", "Renewal note", true)],
    };
    await activateDashboard(context);
    answerAll(answer, { capacity });
    await flush();
    // The server moves it: Working no longer lists it, its label says review.
    capacity.working = [];
    capacity.slots_used = 0;
    pricing.labels["ae.status"] = "review";
    pick("working", "conv_w", "Move to Review");
    await flush();
    expect(section("review").querySelector('[data-session="conv_w"]')).not.toBeNull();
    expect(document.activeElement).toBe(section("working").querySelector(".section-toggle"));
    // Blocked's only row goes back to Working: Blocked folds, the next row takes the focus.
    capacity.blocked = [];
    capacity.working = [slot("conv_b", "Renewal note")];
    renewal.labels["ae.status"] = "working";
    pick("blocked", "conv_b", "Move to Working");
    await flush();
    expect(section("blocked").parentElement?.hidden).toBe(true);
    expect(document.activeElement).toBe(
      section("lot").querySelector('[data-task="T-4"] .card-toggle'),
    );
  });

  // Review (2026-10-14): Working and Blocked leave out what Your move shows,
  // so Your move's card carries the "..." menu they would have given it.
  it("gives a session in Your move the menu Working or Blocked would have", async () => {
    const { context, answer } = stubContext();
    const gate = listed("conv_gate", "Pricing padding", { "ae.status": "working" });
    (context.sessions as { listAll: unknown }).listAll = vi.fn(async () => [gate]);
    await activateDashboard(context);
    answerAll(answer, { yourMove: { ...YOUR_MOVE, sessions: GATE_AND_ASK }, capacity: BUSY });
    await flush();
    const text = (nodes: HTMLElement[]) => nodes.map((node) => node.textContent?.trim());
    expect(text(menuItems("your-move", "conv_gate"))).toEqual([
      "Park",
      "Move to Review",
      "Move to Blocked",
    ]);
    expect(text(menuItems("your-move", "conv_ask"))).toEqual(["Move to Working"]);
    // The gate's own buttons stay in view.
    const card = section("your-move").querySelector<HTMLElement>('[data-session="conv_gate"]')!;
    expect(text([...card.querySelectorAll<HTMLElement>(".actions .btn")])).toEqual([
      "Do it now",
      "Write a note",
      "Save for later",
      "Drop it",
      "Open",
    ]);
    // Park opens the park form with the session chosen (jsdom has no scrolling).
    const scroll = vi.fn();
    Object.defineProperty(Element.prototype, "scrollIntoView", {
      value: scroll,
      configurable: true,
    });
    pick("your-move", "conv_gate", "Park");
    await flush();
    Reflect.deleteProperty(Element.prototype, "scrollIntoView");
    expect(scroll).toHaveBeenCalled();
    expect(document.querySelector<HTMLDetailsElement>("#park")?.open).toBe(true);
    expect(document.querySelector<HTMLSelectElement>("#park-session_id")?.value).toBe("conv_gate");
    // A move writes ae.status.
    pick("your-move", "conv_ask", "Move to Working");
    await flush();
    expect(context.server.request).toHaveBeenCalledWith({
      method: "PATCH",
      path: "/v1/sessions/conv_ask",
      body: { labels: { "ae.status": "working" } },
    });
  });

  // Review (2026-10-14): the lot leaves a due task to Your move, so Your
  // move's row for it carries the parked marker and the parked session's row.
  it("shows a due task's parked session in Your move, as the lot would", async () => {
    const { context, answer } = stubContext();
    const parked = listed("conv_p", "Sidebar search", { "ae.status": "parked", "ae.task": "T-4" });
    (context.sessions as { listAll: unknown }).listAll = vi.fn(async () => [parked]);
    const due = { ...LOT.tasks[0], resurfaces: "2026-09-26" };
    await activateDashboard(context);
    answerAll(answer, {
      yourMove: {
        ...YOUR_MOVE,
        lot: { ok: true, today: "2026-09-26", items: [{ ...due, overdue: false }] },
      },
      lot: { ...LOT, tasks: [due] },
    });
    await flush();
    expect(section("lot").querySelector('[data-task="T-4"]')).toBeNull();
    const card = section("your-move").querySelector<HTMLElement>('[data-task="T-4"]')!;
    expect(card.querySelector(".task-head .parked-mark title")?.textContent).toBe(
      "Parked session: Sidebar search",
    );
    const row = card.querySelector<HTMLElement>(".card-details .task-session")!;
    expect(row.textContent).toContain("Parked session: Sidebar search");
    [...row.querySelectorAll<HTMLButtonElement>("button")]
      .find((node) => node.textContent === "Open")!
      .click();
    await flush();
    expect(context.navigation.openSession).toHaveBeenCalledWith("conv_p");
  });

  // Review (2026-10-14): a saved copy of Your move has its buttons off, so it
  // hides nothing; Working, Blocked and the lot list all they hold, no notes.
  it("leaves nothing out behind a saved copy of Your move", async () => {
    const due = { ...LOT.tasks[0], resurfaces: "2026-09-26" };
    const waiting: YourMove = {
      ...YOUR_MOVE,
      sessions: GATE_AND_ASK,
      lot: { ok: true, today: "2026-09-26", items: [{ ...due, overdue: false }] },
    };
    const { context, answer } = stubContext({ [snapshotKey("yourMove")]: saved(waiting) });
    await activateDashboard(context);
    answer("capacity", BUSY);
    answer("lot", { ...LOT, tasks: [due] });
    await flush();
    const note = (id: string) => section(id).querySelector(".section-h .section-note")?.textContent;
    const gate = section("your-move").querySelector<HTMLElement>('[data-session="conv_gate"]')!;
    const off = [...gate.querySelectorAll<HTMLButtonElement>("button.btn")];
    expect(off.length).toBeGreaterThan(0);
    expect(off.every((node) => node.disabled)).toBe(true);
    expect(section("working").querySelector('[data-session="conv_gate"]')).not.toBeNull();
    expect(section("blocked").querySelector('[data-session="conv_ask"]')).not.toBeNull();
    expect(section("lot").querySelector('[data-task="T-4"]')).not.toBeNull();
    for (const id of ["working", "blocked", "lot"]) expect(note(id)).toBeUndefined();
    // The fresh answer decides again.
    answer("yourMove", waiting);
    await flush();
    expect(section("working").querySelector('[data-session="conv_gate"]')).toBeNull();
    expect(note("working")).toBe("1 in Your move");
  });
});
