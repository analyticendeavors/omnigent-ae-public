// The Admin Center's Fabric section (2026-10-16, renamed 2026-10-18): a card per
// capacity, Wake and Off asking once on one row, and a failed call's sentence on
// its own card. Second pass the same day: the compact row, its meta line, the links menu and keep awake, and the
// row drawn without any of the new fields. Third pass (2026-10-16, review
// fixes): the keep awake projections and a wake's hours come from the server,
// an action's answer is the full row and survives an overlapping read, the
// links menu is cards.ts rowMenu and lifts its card, and touch targets are 44 px.
import type { ExtensionContext } from "@omnigent/extension-sdk";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { FabricCapacity, FabricOverview } from "../src/api";
import { resetMenuState } from "../src/cards";
import {
  activateFabric,
  capacityLinks,
  clockTime,
  deactivateFabric,
  defaultHours,
  duration,
  FABRIC_CAPACITIES_URL,
  freshRows,
  keepAwakeText,
  metaItems,
  money,
  nextDelay,
  POLL_BUSY_MS,
  POLL_IDLE_MS,
  canResize,
  question,
  regionText,
  resizeQuestion,
  skuHourly,
  skuUnits,
  stateText,
  toneOf,
  upPercent,
} from "../src/fabric";
import css from "../src/style.css?raw";

const sdk = vi.hoisted(() => ({ defineExtension: vi.fn() }));
vi.mock("@omnigent/extension-sdk", () => sdk);

type Reply = { ok: boolean; status: number; body: unknown };

function overview(capacities: FabricCapacity[], extra: Partial<FabricOverview> = {}) {
  return { configured: capacities.length > 0, capacities, can_act: true, ...extra };
}

// A fresh row per use: the page updates a card's row in place after an action.
const paused = (): FabricCapacity => ({
  name: "aetraining",
  state: "Paused",
  can_suspend: true,
  wakeBilledHours: 7,
});

/** A context whose server answers from a queue per path; anything unqueued is a 500. */
function stubContext(replies: Record<string, Reply[]>) {
  const request = vi.fn(async ({ path }: { method: string; path: string; body?: unknown }) => {
    const queue = replies[path] ?? [];
    // Who sees what (2026-10-28) is asked before the page draws: an admin, unless a test says.
    if (path === "/v1/ae/access" && !queue.length) return ok({ admin: true, box_admin: true });
    return (
      queue.shift() ?? {
        ok: false,
        status: 500,
        body: { error: { code: "unexpected", message: `no reply queued for ${path}` } },
      }
    );
  });
  const openExternal = vi.fn(async (_url: string) => {});
  const context = {
    view: "fabric",
    server: { request },
    navigation: { openExternal },
    theme: {
      getCurrent: vi.fn(async () => ({ theme: "light" })),
      subscribe: vi.fn(async () => ({ dispose: vi.fn() })),
    },
  } as unknown as ExtensionContext;
  return { context, request, openExternal };
}

const ok = (body: unknown): Reply => ({ ok: true, status: 200, body });

/** A context whose every call waits until the test answers it, in order, per path. */
function gatedContext() {
  const waiting: Record<string, Array<(reply: Reply) => void>> = {};
  const request = vi.fn(
    ({ path }: { method: string; path: string; body?: unknown }) =>
      new Promise<Reply>((resolve) => {
        // Who sees what (2026-10-28) is asked before anything draws: an admin, at once.
        if (path === "/v1/ae/access") resolve(ok({ admin: true, box_admin: true }));
        else (waiting[path] ??= []).push(resolve);
      }),
  );
  const context = {
    view: "fabric",
    server: { request },
    theme: {
      getCurrent: vi.fn(async () => ({ theme: "light" })),
      subscribe: vi.fn(async () => ({ dispose: vi.fn() })),
    },
  } as unknown as ExtensionContext;
  const answer = (path: string, reply: Reply) => {
    const next = waiting[path]?.shift();
    if (!next) throw new Error(`nothing is waiting on ${path}`);
    next(reply);
  };
  return { context, request, answer, pending: (path: string) => waiting[path]?.length ?? 0 };
}

const woke = (state = "Waking") =>
  ok({ name: "aetraining", state, action: "wake", changed: true, can_suspend: true });

function setVisibility(value: "visible" | "hidden") {
  Object.defineProperty(document, "visibilityState", { configurable: true, get: () => value });
}

const flush = async () => {
  for (let i = 0; i < 5; i++) await new Promise((resolve) => setTimeout(resolve, 0));
};

function cardOf(name: string): HTMLElement {
  const node = document.querySelector<HTMLElement>(`[data-capacity="${name}"]`);
  if (!node) throw new Error(`no card for ${name}`);
  return node;
}

/** A button's accessible name: its aria-label, else its text. */
function nameOf(node: HTMLButtonElement): string {
  return node.getAttribute("aria-label") ?? node.textContent?.trim() ?? "";
}

function buttonNamed(root: ParentNode, name: string): HTMLButtonElement {
  const found = [...root.querySelectorAll("button")].find((node) => nameOf(node) === name);
  if (!found) throw new Error(`no button named ${name}`);
  return found;
}

/** The buttons a card shows, menu items left out (they sit in a hidden menu). */
function buttonNames(root: ParentNode): string[] {
  return [...root.querySelectorAll("button")]
    .filter((node) => !node.closest("[role=menu]"))
    .map(nameOf);
}

beforeEach(() => {
  resetMenuState();
  document.body.replaceChildren();
  const root = document.createElement("div");
  root.id = "root";
  document.body.appendChild(root);
});

afterEach(() => {
  deactivateFabric();
  vi.useRealTimers();
  setVisibility("visible");
  vi.restoreAllMocks();
});

describe("the pieces", () => {
  it("names each state in plain words with a calm tone", () => {
    expect(stateText("WakeFailed")).toBe("Wake failed");
    expect(stateText("Active")).toBe("Active");
    expect(toneOf("Active")).toBe("on");
    expect(toneOf("Paused")).toBe("off");
    expect(toneOf("Resuming")).toBe("busy");
    expect(toneOf("SuspendFailed")).toBe("failed");
    expect(toneOf("Unknown")).toBe("unknown");
  });

  it("says what each tap costs, word for word", () => {
    expect(question("wake", paused())).toBe(
      "Start aetraining? It bills at least 7 hours (about $5) and is ready in about 2 minutes.",
    );
    // With the price known, the figure is worked out; the hours are the server's.
    expect(question("wake", { ...paused(), hourlyUsd: 0.72 })).toBe(
      "Start aetraining? It bills at least 7 hours (about $5.04) and is ready in about 2 minutes.",
    );
    expect(question("wake", { ...paused(), maxHours: 8, wakeBilledHours: 9 })).toBe(
      "Start aetraining? It bills at least 9 hours and is ready in about 2 minutes.",
    );
    // An answer without the hours: no figure, no rule worked out on the page.
    expect(question("wake", { ...paused(), maxHours: 8, wakeBilledHours: null })).toBe(
      "Start aetraining? It bills by the hour and is ready in about 2 minutes.",
    );
    expect(question("off", paused())).toBe("Pause aetraining? Anyone using it is cut off.");
  });

  it("polls every 15 s while a capacity is on its way, else every 60 s", () => {
    for (const state of ["Waking", "Resuming", "Suspending", "Pausing"]) {
      expect(nextDelay(overview([{ ...paused(), state }]))).toBe(POLL_BUSY_MS);
    }
    expect(nextDelay(overview([paused(), { ...paused(), name: "b", state: "Active" }]))).toBe(
      POLL_IDLE_MS,
    );
    expect(nextDelay(null)).toBe(POLL_IDLE_MS);
    expect(POLL_BUSY_MS).toBe(15_000);
    expect(POLL_IDLE_MS).toBe(60_000);
  });

  it("treats ARM's in-progress states as busy and polls them every 15 s", () => {
    for (const state of ["Provisioning", "Updating", "Scaling", "Preparing", "Deleting"]) {
      expect(toneOf(state)).toBe("busy");
      expect(nextDelay(overview([{ ...paused(), state }]))).toBe(POLL_BUSY_MS);
    }
    expect(toneOf("Suspending")).toBe("busy");
    expect(toneOf("Pausing")).toBe("busy");
  });

  it("drops the rows of a read that predates an action on that capacity", () => {
    const stale = overview([paused(), { ...paused(), name: "b" }]);
    const current = overview([
      { ...paused(), state: "Waking" },
      { ...paused(), name: "b" },
    ]);
    // The read began at tick 3; aetraining's action ended at tick 5.
    const kept = freshRows(stale, current, 3, new Map([["aetraining", 5]]));
    expect(kept.capacities.map((row) => row.state)).toEqual(["Waking", "Paused"]);
    // An action that predates the read changes nothing.
    expect(freshRows(stale, current, 6, new Map([["aetraining", 5]]))).toBe(stale);
    // No current row to keep: the capacity is left out until the next read.
    expect(freshRows(stale, null, 3, new Map([["aetraining", 5]])).capacities).toHaveLength(1);
  });

  it("puts the state pill under the name on a narrow card and beside it on a wide one", () => {
    // Wide: name and pill on one line.
    const wide = css.indexOf(".fabric-title {");
    expect(css.slice(wide, css.indexOf("}", wide))).toMatch(
      /display: flex;\s*align-items: center;\s*gap: 8px;/,
    );
    // Narrow (the card's container query): stacked, so the name keeps its width.
    const query = css.indexOf("@container not (min-width: 30rem) {\n  .fabric-acts {");
    expect(query).toBeGreaterThan(-1);
    const narrow = css.slice(query, css.indexOf("\n}\n", query));
    expect(narrow).toMatch(
      /\.fabric-title \{\s*flex-direction: column;\s*align-items: flex-start;\s*gap: 2px;\s*\}/,
    );
    expect(narrow).toMatch(/\.fabric-name \{\s*max-width: 100%;\s*\}/);
    // The ellipsis stays as the last resort for a very long name.
    const name = css.indexOf(".fabric-name {");
    expect(css.slice(name, css.indexOf("}", name))).toContain("text-overflow: ellipsis");
  });

  it("keeps 32 px buttons with a mouse and 44 px targets 8 px apart on a touch screen", () => {
    const coarse = css.slice(css.indexOf("@media (pointer: coarse) {\n  .card-fabric .btn {"));
    const block = coarse.slice(0, coarse.indexOf("\n}\n"));
    expect(block).toMatch(/\.card-fabric \.btn \{\s*min-height: 44px;\s*min-width: 44px;\s*\}/);
    // The links button is also a .btn-menu, which the Dashboard makes 40 px wide.
    expect(block).toMatch(/\.card-fabric \.btn\.btn-icon \{\s*width: 44px;\s*\}/);
    expect(block).toMatch(
      /\.fabric-acts,\s*\.fabric-choices,\s*\.fabric-confirm-row \{\s*gap: 8px;\s*\}/,
    );
    const icon = css.indexOf(".btn.btn-icon {");
    expect(css.slice(icon, css.indexOf("}", icon))).toContain("width: 32px");
    // The confirm sentence wraps instead of widening a 375 px card.
    const start = css.indexOf(".fabric-confirm-text {");
    const rule = css.slice(start, css.indexOf("}", start));
    expect(rule).toContain("min-width: 0");
    expect(rule).toContain("overflow-wrap: anywhere");
  });
});

describe("the Admin Center page", () => {
  it("shows a non-admin the state and no action, size picker included", async () => {
    const { context } = stubContext({
      "/v1/ae/fabric": [
        ok(
          overview(
            [
              { name: "aetraining", state: "Paused", can_suspend: true },
              { name: "aeprod", state: "Active", can_suspend: true },
            ],
            { can_act: false },
          ),
        ),
      ],
    });
    await activateFabric(context);
    await flush();
    expect(buttonNames(cardOf("aetraining"))).toEqual([]);
    expect(buttonNames(cardOf("aeprod"))).toEqual([]);
    expect(cardOf("aeprod").textContent).toContain("Active");
    expect(document.querySelector(".card-fabric select")).toBeNull();
  });

  it("renders one card per capacity with only the button its state allows", async () => {
    const { context } = stubContext({
      "/v1/ae/fabric": [
        ok(
          overview([
            { name: "aetraining", state: "Paused", can_suspend: true },
            { name: "aeprod", state: "Active", can_suspend: true },
            { name: "aedev", state: "Active", can_suspend: false },
            { name: "aeqa", state: "Resuming", can_suspend: true },
            {
              name: "aebroken",
              state: "Unknown",
              can_suspend: false,
              error: "aebroken: the Logic App answered 500 to the status call.",
            },
          ]),
        ),
      ],
    });
    await activateFabric(context);
    await flush();
    expect(document.querySelector("h1")?.textContent).toBe("Admin Center");
    expect(buttonNames(cardOf("aetraining"))).toEqual(["Start"]);
    expect(cardOf("aetraining").textContent).toContain("Paused");
    expect(buttonNames(cardOf("aeprod"))).toEqual(["Pause aeprod"]);
    // Active without a suspend key: Off is not offered.
    expect(buttonNames(cardOf("aedev"))).toEqual([]);
    expect(buttonNames(cardOf("aeqa"))).toEqual([]);
    expect(cardOf("aeqa").classList.contains("fabric-busy")).toBe(true);
    // One capacity's error stays on its own card.
    const broken = cardOf("aebroken").querySelector(".notice-error");
    expect(broken?.textContent).toBe("aebroken: the Logic App answered 500 to the status call.");
    expect(cardOf("aetraining").querySelector(".notice-error")).toBeNull();
    // Every state is a dot (decorative) and a pill that says it; no emoji anywhere.
    expect(cardOf("aeprod").querySelector(".fabric-dot")?.getAttribute("aria-hidden")).toBe("true");
    expect(cardOf("aeprod").querySelector(".fabric-state")?.textContent).toBe("Active");
    expect(document.body.textContent).not.toMatch(/\p{Extended_Pictographic}/u);
    // Fabric's own line, in the Compute card's header since the dashboard redesign (2026-10-20).
    const refreshed = document.querySelector<HTMLElement>(
      '.admin-card[data-card="compute"] .admin-card-top .refreshed',
    );
    expect(refreshed?.textContent).toMatch(/^Updated /);
    expect(refreshed?.title).toBe("Again in 15 s or on focus.");
  });

  it("shows the server's how-to sentence when nothing is configured", async () => {
    const note = "No Fabric capacity is configured: the server has none.";
    const { context } = stubContext({
      "/v1/ae/fabric": [ok(overview([], { note }))],
    });
    await activateFabric(context);
    await flush();
    expect(document.querySelector(".empty")?.textContent).toBe(note);
    expect(document.querySelectorAll("article")).toHaveLength(0);
  });

  it("puts the capacities first in the Compute card, under its own heading", async () => {
    const { context } = stubContext({
      "/v1/ae/fabric": [ok(overview([paused()]))],
    });
    await activateFabric(context);
    await flush();
    // Since the dashboard redesign (2026-10-20) the card's heading names it.
    const heading = document.getElementById("admin-compute-h");
    expect(heading?.tagName).toBe("H2");
    expect(heading?.textContent).toBe("Compute");
    const card = heading?.closest(".admin-card");
    expect(card?.getAttribute("aria-labelledby")).toBe("admin-compute-h");
    const list = card?.querySelector("#fabric-capacities");
    expect(list?.getAttribute("aria-label")).toBe("Fabric capacities");
    // Fabric's rows come before Databricks' in the card.
    const body = card?.querySelector(".admin-card-body");
    expect(body?.firstElementChild?.contains(list ?? null)).toBe(true);
  });

  it("asks once on one row before waking, and Cancel sends nothing", async () => {
    const { context, request } = stubContext({
      "/v1/ae/fabric": [ok(overview([paused()]))],
      "/v1/ae/fabric/aetraining/wake": [
        ok({
          name: "aetraining",
          state: "Waking",
          action: "wake",
          changed: true,
          can_suspend: true,
        }),
      ],
    });
    await activateFabric(context);
    await flush();
    buttonNamed(cardOf("aetraining"), "Start").click();
    await flush();
    const row = cardOf("aetraining").querySelector<HTMLElement>(".fabric-confirm-row");
    expect(row).not.toBeNull();
    expect(row?.querySelector(".fabric-confirm-text")?.textContent).toBe(
      "Start aetraining? It bills at least 7 hours (about $5) and is ready in about 2 minutes.",
    );
    // The sentence, Cancel and the button that acts share the one row, under
    // the row whose Wake now reads as open.
    expect(buttonNames(row!)).toEqual(["Cancel", "Start now"]);
    expect(buttonNamed(cardOf("aetraining"), "Start").getAttribute("aria-expanded")).toBe("true");

    buttonNamed(row!, "Cancel").click();
    await flush();
    expect(cardOf("aetraining").querySelector(".fabric-confirm-row")).toBeNull();
    expect(request.mock.calls.some(([call]) => call.method === "POST")).toBe(false);

    buttonNamed(cardOf("aetraining"), "Start").click();
    await flush();
    buttonNamed(cardOf("aetraining"), "Start now").click();
    await flush();
    const posts = request.mock.calls.filter(([call]) => call.method === "POST");
    expect(posts).toHaveLength(1);
    expect(posts[0][0]).toEqual({
      method: "POST",
      path: "/v1/ae/fabric/aetraining/wake",
      body: {},
    });
    // The card shows what the Logic App answered; no Wake to tap twice.
    expect(cardOf("aetraining").querySelector(".fabric-state")?.textContent).toBe("Waking");
    expect(buttonNames(cardOf("aetraining"))).toEqual([]);
    expect(document.getElementById("toast")?.textContent).toBe("Starting aetraining.");
  });

  it("asks before turning off and posts suspend", async () => {
    const { context, request } = stubContext({
      "/v1/ae/fabric": [ok(overview([{ ...paused(), state: "Active" }]))],
      "/v1/ae/fabric/aetraining/suspend": [
        ok({
          name: "aetraining",
          state: "Suspending",
          action: "suspend",
          changed: true,
          can_suspend: true,
        }),
      ],
    });
    await activateFabric(context);
    await flush();
    buttonNamed(cardOf("aetraining"), "Pause aetraining").click();
    await flush();
    expect(cardOf("aetraining").querySelector(".fabric-confirm-text")?.textContent).toBe(
      "Pause aetraining? Anyone using it is cut off.",
    );
    const go = buttonNamed(cardOf("aetraining"), "Pause");
    expect(go.classList.contains("btn-danger")).toBe(true);
    go.click();
    await flush();
    const post = request.mock.calls.find(([call]) => call.method === "POST");
    expect(post?.[0].path).toBe("/v1/ae/fabric/aetraining/suspend");
    expect(cardOf("aetraining").querySelector(".fabric-state")?.textContent).toBe("Suspending");
  });

  it("shows a failed call's error on its card and offers the button again", async () => {
    const message =
      "aetraining: the Logic App did not answer the wake call within 8 s; it may still be under way.";
    const { context } = stubContext({
      "/v1/ae/fabric": [
        ok(overview([paused(), { name: "aeprod", state: "Paused", can_suspend: false }])),
      ],
      "/v1/ae/fabric/aetraining/wake": [
        { ok: false, status: 504, body: { error: { code: "fabric_timeout", message } } },
      ],
    });
    await activateFabric(context);
    await flush();
    buttonNamed(cardOf("aetraining"), "Start").click();
    await flush();
    buttonNamed(cardOf("aetraining"), "Start now").click();
    await flush();
    const error = cardOf("aetraining").querySelector(".notice-error");
    expect(error?.textContent).toBe(`Start failed: ${message}`);
    expect(buttonNames(cardOf("aetraining"))).toEqual(["Start"]);
    expect(cardOf("aetraining").querySelector(".fabric-state")?.textContent).toBe("Paused");
    // The other card is untouched.
    expect(cardOf("aeprod").querySelector(".notice-error")).toBeNull();
  });

  it("says the list could not be read when the first read fails", async () => {
    const { context } = stubContext({
      "/v1/ae/fabric": [
        { ok: false, status: 502, body: { error: { code: "x", message: "server down" } } },
      ],
    });
    await activateFabric(context);
    await flush();
    expect(document.querySelector(".admin-fabric .page-status .notice-error")?.textContent).toBe(
      "Fabric capacities could not be read: server down",
    );
  });
});

describe("races and busy states (review fixes, 2026-10-16)", () => {
  it("a read that began before a Wake cannot put Paused back or slow the poll", async () => {
    const timeouts = vi.spyOn(window, "setTimeout");
    const gate = gatedContext();
    await activateFabric(gate.context);
    gate.answer("/v1/ae/fabric", ok(overview([paused()])));
    await flush();
    buttonNamed(cardOf("aetraining"), "Start").click();
    await flush();
    // A second read starts (a focus event) and is still out when Wake lands.
    window.dispatchEvent(new Event("focus"));
    await flush();
    expect(gate.pending("/v1/ae/fabric")).toBe(1);
    buttonNamed(cardOf("aetraining"), "Start now").click();
    await flush();
    gate.answer("/v1/ae/fabric/aetraining/wake", woke());
    await flush();
    expect(cardOf("aetraining").querySelector(".fabric-state")?.textContent).toBe("Waking");
    // The old read finally answers with the state from before the Wake.
    gate.answer("/v1/ae/fabric", ok(overview([paused()])));
    await flush();
    expect(cardOf("aetraining").querySelector(".fabric-state")?.textContent).toBe("Waking");
    expect(buttonNames(cardOf("aetraining"))).toEqual([]);
    // flush() itself uses 0 ms timers; the page's own last schedule is the 15 s busy poll.
    const delays = timeouts.mock.calls.map((call) => call[1]).filter((delay) => delay);
    expect(delays.at(-1)).toBe(POLL_BUSY_MS);
  });

  it("disables the acting button and says so while the Wake call is out", async () => {
    const gate = gatedContext();
    await activateFabric(gate.context);
    gate.answer("/v1/ae/fabric", ok(overview([paused()])));
    await flush();
    buttonNamed(cardOf("aetraining"), "Start").click();
    await flush();
    const go = buttonNamed(cardOf("aetraining"), "Start now");
    go.click();
    await flush();
    expect(go.disabled).toBe(true);
    expect(go.textContent).toBe("Starting...");
    expect(buttonNamed(cardOf("aetraining"), "Cancel").disabled).toBe(true);
    // A read answering meanwhile redraws the list; the card stays busy and a second tap posts nothing.
    window.dispatchEvent(new Event("focus"));
    await flush();
    gate.answer("/v1/ae/fabric", ok(overview([paused()])));
    await flush();
    const again = cardOf("aetraining").querySelector<HTMLButtonElement>(
      ".fabric-confirm-row .btn-primary",
    );
    expect(again?.disabled).toBe(true);
    again?.click();
    await flush();
    expect(gate.request.mock.calls.filter(([call]) => call.method === "POST")).toHaveLength(1);
    gate.answer("/v1/ae/fabric/aetraining/wake", woke());
    await flush();
    expect(cardOf("aetraining").querySelector(".fabric-state")?.textContent).toBe("Waking");
  });

  it("skips the scheduled read while the tab is hidden and reads on return", async () => {
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
    const { context, request } = stubContext({
      "/v1/ae/fabric": [
        ok(overview([paused()])),
        ok(overview([paused()])),
        ok(overview([paused()])),
      ],
    });
    // The Databricks section under this one reads on its own clock (2026-10-19).
    const fabricReads = () =>
      request.mock.calls.filter(([call]) => call.path === "/v1/ae/fabric").length;
    await activateFabric(context);
    await vi.advanceTimersByTimeAsync(0);
    expect(fabricReads()).toBe(1);
    setVisibility("hidden");
    await vi.advanceTimersByTimeAsync(POLL_IDLE_MS + 1000);
    expect(fabricReads()).toBe(1);
    setVisibility("visible");
    document.dispatchEvent(new Event("visibilitychange"));
    await vi.advanceTimersByTimeAsync(0);
    expect(fabricReads()).toBe(2);
    // Visible again: the schedule is back.
    await vi.advanceTimersByTimeAsync(POLL_IDLE_MS + 1000);
    expect(fabricReads()).toBe(3);
  });
});

describe("the bundle's entry point", () => {
  it("draws the Admin Center for the fabric view and the Dashboard for the others", async () => {
    await import("../src/main");
    const lifecycle = sdk.defineExtension.mock.calls[0][0] as {
      activate(context: ExtensionContext): Promise<void>;
      deactivate(): void;
    };
    const { context } = stubContext({ "/v1/ae/fabric": [ok(overview([paused()]))] });
    await lifecycle.activate(context);
    await flush();
    expect(document.querySelector("h1")?.textContent).toBe("Admin Center");
    expect(document.querySelector(".jump")).toBeNull();
    lifecycle.deactivate();
  });
});

// ── second pass (2026-10-16): the compact row's details, links and keep awake ──

const NOW = Date.parse("2026-10-16T20:14:00Z");
const CAPACITY_ID =
  "/subscriptions/0b1c2d3e-4f50-6172-8394-a5b6c7d8e9f0/resourceGroups/rg-ae-fabric" +
  "/providers/Microsoft.Fabric/capacities/aetraining";

/** An Active capacity with every field the server sends, its projections as fabric.py works them out. */
const full = (): FabricCapacity => ({
  name: "aetraining",
  state: "Active",
  can_suspend: true,
  can_keep_awake: true,
  sku: "F4",
  region: "northcentralus",
  capacityId: CAPACITY_ID,
  hourlyUsd: 0.72,
  resumedAt: "2026-10-16T18:00:00Z",
  maxHours: 6,
  sleepsAt: "2026-10-17T01:20:00Z",
  keepAwakeUntil: null,
  // From 20:14: 2 h and 4 h end before the window does; 8 h (04:14) means the
  // 04:20 run marks quiet and 05:20 suspends.
  keepAwakeOptions: [
    { hours: 2, sleepsAt: "2026-10-17T01:20:00Z", addsTime: false },
    { hours: 4, sleepsAt: "2026-10-17T01:20:00Z", addsTime: false },
    { hours: 8, sleepsAt: "2026-10-17T05:20:00Z", addsTime: true },
  ],
  wakeBilledHours: 7,
});

describe("the details (second pass)", () => {
  it("writes ARM's lowercase region names out and leaves the rest alone", () => {
    expect(regionText("northcentralus")).toBe("North Central US");
    expect(regionText("eastus2")).toBe("East US 2");
    expect(regionText("southeastasia")).toBe("Southeast Asia");
    expect(regionText("uksouth")).toBe("UK South");
    expect(regionText("germanywestcentral")).toBe("Germany West Central");
    expect(regionText("North Central US")).toBe("North Central US");
    // A piece the list does not know: as ARM wrote it.
    expect(regionText("centraluseuap")).toBe("centraluseuap");
    expect(regionText(null)).toBeNull();
  });

  it("says money to the cent and time spans in hours and minutes", () => {
    expect(money(1.608)).toBe("$1.61");
    expect(money(5)).toBe("$5.00");
    expect(duration(2 * 3_600_000 + 14 * 60_000 + 59_000)).toBe("2 h 14 m");
    expect(duration(9 * 60_000)).toBe("9 m");
  });

  it("puts SKU, region, price, uptime with its cost and the sleep time on the meta line", () => {
    expect(metaItems(full(), NOW)).toEqual([
      "F4",
      "North Central US",
      "$0.72/h",
      "up 2 h 14 m, about $1.61",
      `sleeps around ${clockTime(Date.parse("2026-10-17T01:20:00Z"))} if idle`,
    ]);
    // Paused: what a wake bills, from the price.
    expect(metaItems({ ...full(), state: "Paused", sleepsAt: null }, NOW)).toEqual([
      "F4",
      "North Central US",
      "$0.72/h",
      "Start bills at least 7 h, about $5.04",
    ]);
    // Busy: only the note, as in the mockup.
    expect(metaItems({ ...full(), state: "Resuming" }, NOW)).toEqual([
      "Ready in about 2 minutes. Checking every 15 s.",
    ]);
    // A sleep time already past or a resume in the future is not shown.
    expect(
      metaItems(
        { ...full(), sleepsAt: "2026-10-16T19:00:00Z", resumedAt: "2026-10-16T23:00:00Z" },
        NOW,
      ),
    ).toEqual(["F4", "North Central US", "$0.72/h"]);
  });

  it("draws without any of the new fields", () => {
    const bare: FabricCapacity = { name: "aetraining", state: "Active", can_suspend: true };
    expect(metaItems(bare, NOW)).toEqual([]);
    // Without the server's hours the page says nothing about what a wake bills.
    expect(metaItems({ ...bare, state: "Paused" }, NOW)).toEqual([]);
    expect(metaItems({ ...bare, state: "Paused", wakeBilledHours: 7 }, NOW)).toEqual([
      "Start bills at least 7 h, about $5",
    ]);
    // Uptime without a price says how long, not what it cost.
    expect(metaItems({ ...bare, resumedAt: "2026-10-16T20:00:00Z" }, NOW)).toEqual(["up 14 m"]);
    expect(capacityLinks(bare)).toEqual([]);
  });

  it("links the capacity in the Azure portal and Fabric's capacity settings", () => {
    expect(capacityLinks(full())).toEqual([
      { label: "Open in Azure", url: `https://portal.azure.com/#resource${CAPACITY_ID}/overview` },
      { label: "Open in Fabric", url: FABRIC_CAPACITIES_URL },
    ]);
    expect(FABRIC_CAPACITIES_URL).toBe("https://app.fabric.microsoft.com/admin-portal/capacities");
  });

  it("says until when keep awake keeps it up and what it adds, never less", () => {
    const sleeps = Date.parse("2026-10-17T01:20:00Z");
    // The server's 05:20 for 8 h, four hours past 01:20 at $0.72.
    const eight = keepAwakeText(full(), 8, NOW);
    expect(eight).toEqual({
      text: `Keeps it up until about ${clockTime(Date.parse("2026-10-17T05:20:00Z"))} (about $2.88 more).`,
      adds: true,
    });
    // The server says 2 h adds nothing: the page says so.
    expect(keepAwakeText(full(), 2, NOW)).toEqual({
      text: `It already stays up until about ${clockTime(sleeps)} if idle; 2 h adds nothing.`,
      adds: false,
    });
    expect(defaultHours(full())).toBe(8);
    // Every choice adds time: the shortest is picked first.
    const open = (capacity: FabricCapacity): FabricCapacity => ({
      ...capacity,
      keepAwakeOptions: capacity.keepAwakeOptions?.map((option) => ({
        ...option,
        sleepsAt: null,
        addsTime: true,
      })),
    });
    expect(defaultHours(open(full()))).toBe(2);
    // No sleep time from the server: at least the hours, with the price when known.
    expect(keepAwakeText(open({ ...full(), sleepsAt: null }), 4, NOW)).toEqual({
      text: "Keeps it up at least 4 more hours (about $2.88).",
      adds: true,
    });
    expect(
      keepAwakeText(open({ ...full(), sleepsAt: null, hourlyUsd: null }), 4, NOW).text,
    ).toBe("Keeps it up at least 4 more hours.");
    // No choices from the server: nothing to pick.
    expect(defaultHours({ ...full(), keepAwakeOptions: [] })).toBeNull();
  });

  it("holds no copy of the runbook's rule", async () => {
    const source = (await import("../src/fabric.ts?raw")).default as string;
    expect(source).not.toMatch(/maxHours \?\? 6|wakeHours|Math\.ceil/);
    expect(source).not.toContain("KEEP_AWAKE_CHOICES");
  });
});

const KEPT_SLEEPS = "2026-10-17T05:20:00Z";

/** What the server answers a keep awake of 8 hours from 20:14: the full row. */
const keptRow = () => ({
  ...full(),
  action: "keepawake",
  changed: true,
  keepAwakeUntil: "2026-10-17T04:14:00Z",
  sleepsAt: KEPT_SLEEPS,
  keepAwakeOptions: [2, 4, 8].map((hours) => ({
    hours,
    sleepsAt: KEPT_SLEEPS,
    addsTime: false,
  })),
});

describe("the compact row on the page (second pass)", () => {
  beforeEach(() => {
    vi.useFakeTimers({ toFake: ["Date"] });
    vi.setSystemTime(NOW);
  });

  it("lays out dot, name, pill, actions and the meta line in one row", async () => {
    const { context } = stubContext({ "/v1/ae/fabric": [ok(overview([full()]))] });
    await activateFabric(context);
    await flush();
    const row = cardOf("aetraining").querySelector(".fabric-row")!;
    expect([...row.children].map((node) => node.className)).toEqual([
      "fabric-dot",
      "fabric-title",
      "fabric-acts",
      "fabric-meta",
      // Up since its wake, toward its sleep (2026-10-20).
      "fabric-upbar",
    ]);
    const up = upPercent(full(), NOW);
    expect(up).not.toBeNull();
    expect(row.querySelector<HTMLElement>(".fabric-upbar > i")?.style.width).toBe(`${up}%`);
    expect(row.querySelector(".fabric-state")?.textContent).toBe("Active");
    expect([...row.querySelectorAll(".fabric-meta-item")].map((node) => node.textContent)).toEqual(
      metaItems(full(), NOW),
    );
    // Links, keep awake and off, each a labelled icon button.
    expect(buttonNames(row.querySelector(".fabric-acts")!)).toEqual([
      "Links for aetraining",
      "Keep aetraining awake longer",
      "Pause aetraining",
    ]);
    for (const node of row.querySelectorAll<HTMLButtonElement>(".fabric-acts .btn-icon")) {
      expect(node.textContent).toBe("");
      expect(node.querySelector("svg")?.getAttribute("aria-hidden")).toBe("true");
    }
  });

  it("degrades to the first pass's row without the new fields", async () => {
    const { context } = stubContext({
      "/v1/ae/fabric": [
        ok(
          overview([
            { name: "aetraining", state: "Active", can_suspend: true },
            { name: "aeprod", state: "Paused", can_suspend: false, wakeBilledHours: 7 },
          ]),
        ),
      ],
    });
    await activateFabric(context);
    await flush();
    // No capacityId: no links; no can_keep_awake: no keep awake; no meta line at all.
    expect(buttonNames(cardOf("aetraining"))).toEqual(["Pause aetraining"]);
    expect(cardOf("aetraining").querySelector(".fabric-meta")).toBeNull();
    expect(cardOf("aeprod").querySelector(".fabric-meta")?.textContent).toBe(
      "Start bills at least 7 h, about $5",
    );
    expect(document.body.textContent).not.toContain("undefined");
    expect(document.body.textContent).not.toContain("null");
    expect(document.body.textContent).not.toContain("NaN");
  });

  it("opens the links in a menu that works from the keyboard", async () => {
    const { context, openExternal } = stubContext({ "/v1/ae/fabric": [ok(overview([full()]))] });
    await activateFabric(context);
    await flush();
    const trigger = buttonNamed(cardOf("aetraining"), "Links for aetraining");
    const menu = document.getElementById(trigger.getAttribute("aria-controls")!)!;
    expect(trigger.getAttribute("aria-haspopup")).toBe("menu");
    expect(menu.getAttribute("role")).toBe("menu");
    expect(menu.hidden).toBe(true);
    trigger.click();
    expect(menu.hidden).toBe(false);
    expect(trigger.getAttribute("aria-expanded")).toBe("true");
    const items = [...menu.querySelectorAll<HTMLButtonElement>("[role=menuitem]")];
    expect(items.map((item) => item.textContent)).toEqual(["Open in Azure", "Open in Fabric"]);
    expect(document.activeElement).toBe(items[0]);
    menu.dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowDown", bubbles: true }));
    expect(document.activeElement).toBe(items[1]);
    menu.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
    expect(menu.hidden).toBe(true);
    expect(document.activeElement).toBe(trigger);
    // ArrowUp on the button opens on the last item; choosing it opens the link.
    trigger.dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowUp", bubbles: true }));
    expect(document.activeElement).toBe(items[1]);
    items[1].click();
    await flush();
    expect(openExternal).toHaveBeenCalledWith(FABRIC_CAPACITIES_URL);
    // A press outside closes an open menu.
    const again = buttonNamed(cardOf("aetraining"), "Links for aetraining");
    again.click();
    document.body.dispatchEvent(new Event("pointerdown", { bubbles: true }));
    expect(again.getAttribute("aria-expanded")).toBe("false");
  });

  it("shows a link the host would not open as text to copy", async () => {
    const { context, openExternal } = stubContext({ "/v1/ae/fabric": [ok(overview([full()]))] });
    openExternal.mockRejectedValueOnce(new Error("not allowed"));
    await activateFabric(context);
    await flush();
    buttonNamed(cardOf("aetraining"), "Links for aetraining").click();
    buttonNamed(cardOf("aetraining"), "Open in Azure").click();
    await flush();
    const note = cardOf("aetraining").querySelector(".fabric-link-note");
    expect(note?.textContent).toContain("Could not open Azure from here (not allowed).");
    expect(note?.querySelector("code")?.textContent).toBe(
      `https://portal.azure.com/#resource${CAPACITY_ID}/overview`,
    );
    expect(buttonNames(note!)).toEqual(["Copy"]);
  });

  it("keeps it awake after choosing hours in the chooser, and draws the answer's row", async () => {
    const { context, request } = stubContext({
      "/v1/ae/fabric": [ok(overview([full()])), ok(overview([full()]))],
      "/v1/ae/fabric/aetraining/keepawake": [ok(keptRow())],
    });
    await activateFabric(context);
    await flush();
    const keep = buttonNamed(cardOf("aetraining"), "Keep aetraining awake longer");
    keep.click();
    await flush();
    const panel = cardOf("aetraining").querySelector<HTMLElement>(".fabric-keep")!;
    expect(panel.getAttribute("role")).toBe("group");
    expect(
      buttonNamed(cardOf("aetraining"), "Keep aetraining awake longer").getAttribute(
        "aria-expanded",
      ),
    ).toBe("true");
    const choices = [...panel.querySelectorAll<HTMLButtonElement>(".fabric-choice")];
    expect(choices.map((node) => node.textContent)).toEqual(["2 h", "4 h", "8 h"]);
    // 2 h and 4 h end before the current sleep time, so 8 h is picked first.
    expect(defaultHours(full())).toBe(8);
    expect(choices.map((node) => node.getAttribute("aria-pressed"))).toEqual([
      "false",
      "false",
      "true",
    ]);
    // A choice that adds nothing says so, and the button stays off.
    const text = () => panel.querySelector(".fabric-confirm-text")?.textContent;
    choices[0].click();
    expect(text()).toBe(keepAwakeText(full(), 2, NOW).text);
    expect(buttonNamed(panel, "Keep awake").disabled).toBe(true);
    choices[2].click();
    expect(choices[2].getAttribute("aria-pressed")).toBe("true");
    expect(text()).toBe(keepAwakeText(full(), 8, NOW).text);
    expect(text()).not.toMatch(/shorten|earlier/i);
    const go = buttonNamed(panel, "Keep awake");
    expect(go.disabled).toBe(false);
    go.click();
    await flush();
    const post = request.mock.calls.find(([call]) => call.method === "POST");
    expect(post?.[0]).toEqual({
      method: "POST",
      path: "/v1/ae/fabric/aetraining/keepawake",
      body: { hours: 8 },
    });
    expect(document.getElementById("toast")?.textContent).toBe(
      "Keeping aetraining up 8 more hours.",
    );
    expect(cardOf("aetraining").querySelector(".fabric-keep")).toBeNull();
    // The answer is the new row: the card says the new sleep time with no read after.
    expect(cardOf("aetraining").querySelector(".fabric-meta")?.textContent).toContain(
      `sleeps around ${clockTime(Date.parse(KEPT_SLEEPS))} if idle`,
    );
    expect(request.mock.calls.filter(([call]) => call.path === "/v1/ae/fabric")).toHaveLength(1);
  });

  it("keeps the action's row over a read that was out, then reads once more", async () => {
    const gate = gatedContext();
    await activateFabric(gate.context);
    gate.answer("/v1/ae/fabric", ok(overview([full(), { ...full(), name: "aeprod" }])));
    await flush();
    buttonNamed(cardOf("aetraining"), "Keep aetraining awake longer").click();
    await flush();
    // A read starts (a focus event) and is still out when the keep awake answers.
    window.dispatchEvent(new Event("focus"));
    await flush();
    expect(gate.pending("/v1/ae/fabric")).toBe(1);
    buttonNamed(cardOf("aetraining"), "Keep awake").click();
    await flush();
    gate.answer("/v1/ae/fabric/aetraining/keepawake", ok(keptRow()));
    await flush();
    const sleeps = () => cardOf("aetraining").querySelector(".fabric-meta")?.textContent;
    const kept = `sleeps around ${clockTime(Date.parse(KEPT_SLEEPS))} if idle`;
    expect(sleeps()).toContain(kept);
    // The read that was out lands with the row from before the keep awake.
    gate.answer(
      "/v1/ae/fabric",
      ok(overview([full(), { ...full(), name: "aeprod", sku: "F8" }])),
    );
    await flush();
    expect(sleeps()).toContain(kept);
    // Its other rows are taken.
    expect(cardOf("aeprod").querySelector(".fabric-meta")?.textContent).toContain("F8");
    // One fresh read follows at once, and its row is taken.
    expect(gate.pending("/v1/ae/fabric")).toBe(1);
    gate.answer(
      "/v1/ae/fabric",
      ok(overview([{ ...keptRow(), sku: "F16" }, { ...full(), name: "aeprod" }])),
    );
    await flush();
    expect(sleeps()).toContain("F16");
    expect(sleeps()).toContain(kept);
    // And no more than one.
    await flush();
    expect(gate.pending("/v1/ae/fabric")).toBe(0);
  });

  it("lifts the card with the open links menu over the card after it", async () => {
    const { context } = stubContext({
      "/v1/ae/fabric": [ok(overview([full(), { ...full(), name: "aeprod" }]))],
    });
    await activateFabric(context);
    await flush();
    const first = cardOf("aetraining");
    // The cards are their own stacking contexts (container queries).
    expect(css).toMatch(/\.card-fabric \{[^}]*container-type: inline-size;/);
    const trigger = buttonNamed(first, "Links for aetraining");
    expect(trigger.classList.contains("btn-menu")).toBe(true);
    trigger.click();
    expect(first.classList.contains("menu-open")).toBe(true);
    expect(cardOf("aeprod").classList.contains("menu-open")).toBe(false);
    const start = css.indexOf(".card.menu-open {");
    const rule = css.slice(start, css.indexOf("}", start));
    expect(rule).toContain("position: relative");
    expect(rule).toMatch(/z-index: [1-9]/);
    // The open menu comes back on the redrawn card, still lifted.
    window.dispatchEvent(new Event("focus"));
    await flush();
    const again = cardOf("aetraining");
    expect(again).not.toBe(first);
    expect(again.classList.contains("menu-open")).toBe(true);
    expect(buttonNamed(again, "Links for aetraining").getAttribute("aria-expanded")).toBe("true");
    buttonNamed(again, "Links for aetraining").click();
    expect(again.classList.contains("menu-open")).toBe(false);
  });

  it("disables the chooser while the keep awake call is out, and shows a failure on the card", async () => {
    const gate = gatedContext();
    await activateFabric(gate.context);
    gate.answer("/v1/ae/fabric", ok(overview([full()])));
    await flush();
    buttonNamed(cardOf("aetraining"), "Keep aetraining awake longer").click();
    await flush();
    const go = buttonNamed(cardOf("aetraining"), "Keep awake");
    go.click();
    await flush();
    expect(go.disabled).toBe(true);
    expect(go.textContent).toBe("Keeping awake...");
    expect(buttonNamed(cardOf("aetraining"), "Cancel").disabled).toBe(true);
    expect(buttonNamed(cardOf("aetraining"), "2 h").disabled).toBe(true);
    gate.answer("/v1/ae/fabric/aetraining/keepawake", {
      ok: false,
      status: 502,
      body: {
        error: {
          code: "fabric_failed",
          message: "aetraining: the Logic App refused the suspend key (403).",
        },
      },
    });
    await flush();
    expect(cardOf("aetraining").querySelector(".notice-error")?.textContent).toBe(
      "Keep awake failed: aetraining: the Logic App refused the suspend key (403).",
    );
    expect(gate.request.mock.calls.filter(([call]) => call.method === "POST")).toHaveLength(1);
  });

  it("opens one step at a time: the chooser closes the confirm step and back", async () => {
    const { context } = stubContext({ "/v1/ae/fabric": [ok(overview([full()]))] });
    await activateFabric(context);
    await flush();
    buttonNamed(cardOf("aetraining"), "Pause aetraining").click();
    await flush();
    expect(cardOf("aetraining").querySelector(".fabric-confirm")).not.toBeNull();
    buttonNamed(cardOf("aetraining"), "Keep aetraining awake longer").click();
    await flush();
    expect(cardOf("aetraining").querySelector(".fabric-confirm")).toBeNull();
    expect(cardOf("aetraining").querySelector(".fabric-keep")).not.toBeNull();
    // Tapping the open step's button again closes it.
    buttonNamed(cardOf("aetraining"), "Keep aetraining awake longer").click();
    await flush();
    expect(cardOf("aetraining").querySelector(".fabric-keep")).toBeNull();
  });
});

describe("leaving the page during activation (dual review, 2026-10-20)", () => {
  it("starts no section, and leaves no focus listener, once deactivated mid-await", async () => {
    const { context, request } = stubContext({});
    let release: (value: { theme: string }) => void = () => {};
    const theme = context.theme as unknown as { getCurrent: ReturnType<typeof vi.fn> };
    theme.getCurrent.mockImplementationOnce(
      () => new Promise<{ theme: string }>((resolve) => (release = resolve)),
    );
    const activating = activateFabric(context);
    deactivateFabric();
    release({ theme: "light" });
    await activating;
    // Let any read already out finish, so a focus read would be a new request.
    await flush();
    request.mockClear();
    window.dispatchEvent(new Event("focus"));
    await flush();
    expect(request).not.toHaveBeenCalled();
  });
});

describe("the capacity fleet (2026-10-21)", () => {
  const SKUS = ["F2", "F4", "F8", "F16", "F32", "F64"];
  const ZERO = "00000000-0000-0000-0000-000000000000";
  const fleetRow = (name: string, sku: string, state: string): FabricCapacity => ({
    name,
    state,
    can_suspend: true,
    can_keep_awake: false,
    sku,
    region: "North Central US",
    subscriptionName: "Placeholder subscription",
    capacityId: `/subscriptions/${ZERO}/resourceGroups/rg-placeholder/providers/Microsoft.Fabric/capacities/${name}`,
    hourlyUsd: (Number(sku.slice(1)) * 0.18),
    canScale: true,
    skuChoices: SKUS,
    wakeBilledHours: null,
  });
  const rows = () => [
    fleetRow("salesdemo", "F8", "Active"),
    fleetRow("aetraining", "F4", "Paused"),
    fleetRow("teamlab", "F4", "Active"),
  ];
  const pick = (name: string, sku: string) => {
    const select = cardOf(name).querySelector<HTMLSelectElement>("select.fabric-size");
    if (!select) throw new Error(`no size picker on ${name}`);
    select.value = sku;
    select.dispatchEvent(new Event("change"));
  };

  it("prices a size from the current one and says both in the resize question", () => {
    const row = fleetRow("teamlab", "F4", "Active");
    expect(skuUnits("F64")).toBe(64);
    expect(skuUnits("P1")).toBeNull();
    expect(skuHourly(row, "F8")).toBeCloseTo(1.44);
    expect(resizeQuestion(row, "F8")).toBe(
      "Resize teamlab from F4 ($0.72/h) to F8 ($1.44/h)? Billing changes at once.",
    );
    // No price known: the sizes alone, never a made-up figure.
    expect(resizeQuestion({ ...row, hourlyUsd: null }, "F2")).toBe(
      "Resize teamlab from F4 to F2? Billing changes at once.",
    );
    expect(canResize(row)).toBe(true);
    expect(canResize({ ...row, state: "Paused" })).toBe(true);
    expect(canResize({ ...row, state: "Updating" })).toBe(false);
    expect(canResize({ ...row, canScale: false })).toBe(false);
    expect(canResize({ name: "old", state: "Active", can_suspend: true, sku: "F4" })).toBe(false);
  });

  it("draws one row per capacity returned, each with its size, state and Wake or Off", async () => {
    const { context } = stubContext({
      "/v1/ae/fabric": [ok(overview(rows(), { fleet: true }))],
    });
    await activateFabric(context);
    await flush();
    const cards = [...document.querySelectorAll<HTMLElement>("#fabric-capacities article")];
    expect(cards.map((card) => card.dataset.capacity)).toEqual([
      "salesdemo",
      "aetraining",
      "teamlab",
    ]);
    for (const card of cards) {
      const select = card.querySelector<HTMLSelectElement>("select.fabric-size");
      expect([...(select?.options ?? [])].map((option) => option.value)).toEqual(SKUS);
      expect(select?.getAttribute("aria-label")).toBe(`Size of ${card.dataset.capacity}`);
    }
    expect(cardOf("salesdemo").querySelector<HTMLSelectElement>("select")?.value).toBe("F8");
    expect(cardOf("teamlab").querySelector<HTMLSelectElement>("select")?.value).toBe("F4");
    // The picker stands in for the SKU; the price and subscription still show.
    const meta = cardOf("teamlab").querySelector(".fabric-meta")?.textContent ?? "";
    expect(meta).toContain("$0.72/h");
    expect(meta).toContain("Placeholder subscription");
    expect(buttonNames(cardOf("aetraining"))).toEqual(["Links for aetraining", "Start"]);
    expect(buttonNames(cardOf("teamlab"))).toEqual([
      "Links for teamlab",
      "Pause teamlab",
    ]);
    // A capacity with no runbook: no keep awake, no minimum wake bill.
    expect(cardOf("aetraining").textContent).not.toContain("Start bills");
  });

  it("asks before a resize, naming both sizes and prices; Cancel sends nothing", async () => {
    const { context, request } = stubContext({
      "/v1/ae/fabric": [ok(overview(rows(), { fleet: true }))],
    });
    await activateFabric(context);
    await flush();
    pick("teamlab", "F8");
    await flush();
    const step = cardOf("teamlab").querySelector(".fabric-resize");
    expect(step?.querySelector(".fabric-confirm-text")?.textContent).toBe(
      "Resize teamlab from F4 ($0.72/h) to F8 ($1.44/h)? Billing changes at once.",
    );
    // The picker keeps showing the size being asked about.
    expect(cardOf("teamlab").querySelector<HTMLSelectElement>("select")?.value).toBe("F8");
    expect(request.mock.calls.some(([call]) => call.method === "POST")).toBe(false);
    buttonNamed(cardOf("teamlab"), "Cancel").click();
    await flush();
    expect(cardOf("teamlab").querySelector(".fabric-resize")).toBeNull();
    expect(cardOf("teamlab").querySelector<HTMLSelectElement>("select")?.value).toBe("F4");
    expect(request.mock.calls.some(([call]) => call.method === "POST")).toBe(false);
    // Picking the current size again opens nothing.
    pick("teamlab", "F4");
    await flush();
    expect(cardOf("teamlab").querySelector(".fabric-resize")).toBeNull();
  });

  it("sends the resize only from its confirm and redraws the row from the answer", async () => {
    const { context, request } = stubContext({
      "/v1/ae/fabric": [ok(overview(rows(), { fleet: true }))],
      "/v1/ae/fabric/teamlab/scale": [
        ok({
          ...fleetRow("teamlab", "F8", "Updating"),
          action: "scale",
          changed: true,
        }),
      ],
    });
    await activateFabric(context);
    await flush();
    pick("teamlab", "F8");
    await flush();
    buttonNamed(cardOf("teamlab"), "Resize").click();
    await flush();
    const posts = request.mock.calls.filter(([call]) => call.method === "POST");
    expect(posts).toHaveLength(1);
    expect(posts[0][0].path).toBe("/v1/ae/fabric/teamlab/scale");
    expect(posts[0][0].body).toEqual({ sku: "F8" });
    // Updating is busy: the picker steps aside until ARM settles.
    expect(cardOf("teamlab").querySelector(".fabric-state")?.textContent).toBe("Updating");
    expect(cardOf("teamlab").querySelector("select.fabric-size")).toBeNull();
    expect(cardOf("teamlab").querySelector(".fabric-resize")).toBeNull();
  });

  it("keeps a failed resize's sentence on its own row", async () => {
    const { context } = stubContext({
      "/v1/ae/fabric": [ok(overview(rows(), { fleet: true }))],
      "/v1/ae/fabric/salesdemo/scale": [
        {
          ok: false,
          status: 502,
          body: { error: { code: "fabric_failed", message: "salesdemo: ARM refused it." } },
        },
      ],
    });
    await activateFabric(context);
    await flush();
    pick("salesdemo", "F16");
    await flush();
    buttonNamed(cardOf("salesdemo"), "Resize").click();
    await flush();
    expect(cardOf("salesdemo").querySelector(".notice-error")?.textContent).toContain(
      "Resize failed:",
    );
    expect(cardOf("teamlab").querySelector(".notice-error")).toBeNull();
  });

  it("says when the list is partial, above the rows it has", async () => {
    const fleetError = "Azure did not answer for 1 subscription; capacities there are missing.";
    const { context } = stubContext({
      "/v1/ae/fabric": [ok(overview(rows(), { fleet: true, fleetError }))],
    });
    await activateFabric(context);
    await flush();
    expect(document.querySelector(".admin-fabric .page-status")?.textContent).toBe(fleetError);
    expect(document.querySelectorAll("#fabric-capacities article")).toHaveLength(3);
  });

  it("keeps the size picker inside the meta line, where it wraps on a phone", () => {
    expect(css).toMatch(/\.fabric-size \{[^}]*max-width: 100%/);
    expect(css).toMatch(/\.card-fabric \.fabric-size \{[^}]*min-height: 44px/);
  });
});

describe("a signed-in non-admin (2026-10-28)", () => {
  it("gets one quiet line and no more reads", async () => {
    const denied: Reply = { ok: false, status: 403, body: { error: { code: "admin_only", message: "Only an OmniGent admin can see this; ask an admin to do it." } } };
    const { context, request } = stubContext({ "/v1/ae/fabric": [denied, denied] });
    await activateFabric(context);
    await flush();
    expect(document.body.textContent).toContain("Fabric capacities are for admins.");
    expect(document.body.textContent).not.toContain("Fabric capacities could not be read");
    window.dispatchEvent(new Event("focus"));
    await flush();
    expect(request.mock.calls.filter(([call]) => call.path === "/v1/ae/fabric")).toHaveLength(1);
  });
});

describe("a false admin_only answer (2026-10-28)", () => {
  it("is looked at again in five minutes and the cards come back on a 200", async () => {
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout", "Date"] });
    try {
      const denied: Reply = { ok: false, status: 403, body: { error: { code: "admin_only", message: "Only an OmniGent admin can see this; ask an admin to do it." } } };
      const { context, request } = stubContext({
        "/v1/ae/fabric": [denied, ok(overview([{ name: "aeprod", state: "Active", can_suspend: true }]))],
      });
      await activateFabric(context);
      await vi.advanceTimersByTimeAsync(0);
      expect(document.body.textContent).toContain("Fabric capacities are for admins.");
      const reads = () => request.mock.calls.filter(([call]) => call.path === "/v1/ae/fabric").length;
      await vi.advanceTimersByTimeAsync(4 * 60_000);
      expect(reads()).toBe(1);
      await vi.advanceTimersByTimeAsync(61_000);
      expect(reads()).toBe(2);
      expect(document.body.textContent).not.toContain("Fabric capacities are for admins.");
      expect(document.querySelector('[data-capacity="aeprod"]')).not.toBeNull();
    } finally {
      vi.useRealTimers();
    }
  });
});
