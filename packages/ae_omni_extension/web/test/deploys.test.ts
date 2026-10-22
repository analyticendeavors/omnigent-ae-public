// The Admin Center's Deploys section (2026-10-19): first on the page, it says
// which release runs and which waits, and holds the box's automatic deploys for
// 1, 2 or 3 hours with no confirm step. Resume ends a hold. Every call is
// mocked: the routes are /v1/ae/release, GET and POST /v1/ae/deploy/hold and
// POST /v1/ae/deploy/resume.
import type { ExtensionContext } from "@omnigent/extension-sdk";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { DeployHold } from "../src/api";
import { resetMenuState } from "../src/cards";
import { whenText } from "../src/databricks";
import {
  activateDeploys,
  deactivateDeploys,
  deploysSection,
  envText,
  holdLines,
  holdTile,
  idleText,
  leftText,
  nextDelay,
  POLL_MS,
  releaseLines,
  waitReason,
  waitingText,
} from "../src/deploys";
import { activateFabric, deactivateFabric } from "../src/fabric";
import type { Release } from "../src/header";
import css from "../src/style.css?raw";

const sdk = vi.hoisted(() => ({ defineExtension: vi.fn() }));
vi.mock("@omnigent/extension-sdk", () => sdk);

type Reply = { ok: boolean; status: number; body: unknown };

const GET_RELEASE = "GET /v1/ae/release";
const GET_HOLD = "GET /v1/ae/deploy/hold";
const POST_HOLD = "POST /v1/ae/deploy/hold";
const POST_RESUME = "POST /v1/ae/deploy/resume";

const NOW = Date.parse("2026-10-20T02:34:00Z");
/** 1 h 12 min after NOW. */
const UNTIL = "2026-10-20T03:46:00Z";

const release = (extra: Partial<Release> = {}): Release => ({
  running: "ae-v0.18.0.5",
  latest: "ae-v0.18.0.5",
  state: "current",
  mid_turn: 0,
  error: null,
  ...extra,
});

const notHeld = (extra: Partial<DeployHold> = {}): DeployHold => ({
  held: false,
  until: null,
  set_at: null,
  set_by: null,
  remaining_s: 0,
  choices: [1, 2, 3],
  error: null,
  ...extra,
});

const held = (extra: Partial<DeployHold> = {}): DeployHold => ({
  held: true,
  until: UNTIL,
  set_at: "2026-10-20T01:46:00Z",
  set_by: "reid",
  remaining_s: 72 * 60,
  choices: [1, 2, 3],
  error: null,
  ...extra,
});

const ok = (body: unknown): Reply => ({ ok: true, status: 200, body });
const failed = (status: number, message: string): Reply => ({
  ok: false,
  status,
  body: { error: { code: "x", message } },
});

const theme = () => ({
  getCurrent: vi.fn(async () => ({ theme: "light" })),
  subscribe: vi.fn(async () => ({ dispose: vi.fn() })),
});

type Call = { method: string; path: string; body?: unknown };

/** A context whose server answers from a queue per "METHOD path"; anything unqueued is a 500. */
function stubContext(replies: Record<string, Reply[]>) {
  const request = vi.fn(async ({ method, path }: Call) => {
    const queue = replies[`${method} ${path}`] ?? [];
    return queue.shift() ?? failed(500, `no reply queued for ${path}`);
  });
  const context = {
    view: "fabric",
    server: { request },
    navigation: { openExternal: vi.fn() },
    theme: theme(),
  } as unknown as ExtensionContext;
  return { context, request };
}

/** A context whose every call waits until the test answers it, in order, per "METHOD path". */
function gatedContext() {
  const waiting: Record<string, Array<(reply: Reply) => void>> = {};
  const request = vi.fn(
    ({ method, path }: Call) =>
      new Promise<Reply>((resolve) => {
        (waiting[`${method} ${path}`] ??= []).push(resolve);
      }),
  );
  const context = {
    view: "fabric",
    server: { request },
    theme: theme(),
  } as unknown as ExtensionContext;
  const answer = (key: string, reply: Reply) => {
    const next = waiting[key]?.shift();
    if (!next) throw new Error(`nothing is waiting on ${key}`);
    next(reply);
  };
  return { context, request, answer, pending: (key: string) => waiting[key]?.length ?? 0 };
}

const callsTo = (request: ReturnType<typeof vi.fn>, key: string) =>
  request.mock.calls
    .map(([call]) => call as Call)
    .filter((call) => `${call.method} ${call.path}` === key);

function setVisibility(value: "visible" | "hidden") {
  Object.defineProperty(document, "visibilityState", { configurable: true, get: () => value });
}

const flush = () => vi.advanceTimersByTimeAsync(0);

/** Mount the bare section and start it, as activateFabric does. */
async function mount(context: ExtensionContext): Promise<HTMLElement> {
  const node = deploysSection();
  document.getElementById("root")?.appendChild(node);
  activateDeploys(context, node);
  await flush();
  return node;
}

/** The ring's time left ("1 h 12 min left"), or null with no hold on (2026-10-20). */
const left = (node: ParentNode) => node.querySelector(".deploy-ring .ring")?.getAttribute("aria-label") ?? null;

/** What the card says: its lines, then the ring's time left when a hold is on. */
const lines = (node: ParentNode) => [
  ...[...node.querySelectorAll(".deploy-line")].map((line) => line.textContent),
  ...(left(node) ? [left(node)] : []),
];

const mutedLines = (node: ParentNode) =>
  [...node.querySelectorAll(".deploy-line-muted")].map((line) => line.textContent);

/** The card's buttons, not its header's fold toggle (board.ts). */
const buttons = (node: ParentNode) => [
  ...node.querySelectorAll<HTMLButtonElement>("button:not(.admin-card-toggle):not(.admin-grip)"),
];

/** A segment of the hold control reads "1 h" and is named "Hold 1 h". */
const nameOf = (item: HTMLButtonElement) =>
  item.getAttribute("aria-label") ?? item.textContent?.trim();

const buttonNames = (node: ParentNode) => buttons(node).map(nameOf);

function buttonNamed(node: ParentNode, name: string): HTMLButtonElement {
  const found = buttons(node).find((item) => nameOf(item) === name);
  if (!found) throw new Error(`no button named ${name}`);
  return found;
}

const statusOf = (node: ParentNode) => node.querySelector(".deploy-status")?.textContent ?? "";

beforeEach(() => {
  resetMenuState();
  document.body.replaceChildren();
  const root = document.createElement("div");
  root.id = "root";
  document.body.appendChild(root);
  vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout", "Date"] });
  vi.setSystemTime(NOW);
});

afterEach(() => {
  deactivateDeploys();
  deactivateFabric();
  vi.useRealTimers();
  setVisibility("visible");
  vi.restoreAllMocks();
});

describe("the words", () => {
  it("says which release waits and for how many busy sessions", () => {
    expect(waitingText(release())).toBeNull();
    const waiting = release({ latest: "ae-v0.18.0.6", state: "waiting" });
    expect(waitingText(waiting)).toBe("ae-v0.18.0.6 is waiting to deploy");
    expect(waitingText({ ...waiting, mid_turn: 1 })).toBe(
      "ae-v0.18.0.6 is waiting to deploy for 1 busy session",
    );
    expect(waitingText({ ...waiting, mid_turn: 2 })).toBe(
      "ae-v0.18.0.6 is waiting to deploy for 2 busy sessions",
    );
  });

  it("says a hold, not busy sessions, is what a waiting release waits for", () => {
    const waiting = release({ latest: "ae-v0.18.0.7", state: "waiting", mid_turn: 2 });
    expect(waitingText(waiting, true)).toBe("ae-v0.18.0.7 is waiting for the hold to end");
    expect(waitingText({ ...waiting, latest: null }, true)).toBe(
      "A newer release is waiting for the hold to end",
    );
    expect(waitingText(waiting, false)).toBe("ae-v0.18.0.7 is waiting to deploy for 2 busy sessions");
    expect(releaseLines(waiting, null, true).map((line) => line.text)).toEqual([
      "ae-v0.18.0.5 \u2192 ae-v0.18.0.7 queued",
      "Waiting for the hold to end",
    ]);
    expect(waitReason(waiting, false)).toBe("Waiting for 2 busy sessions");
    expect(waitReason({ ...waiting, mid_turn: 0 }, false)).toBeNull();
  });

  it("says the newest release is unknown, with the reason", () => {
    expect(releaseLines(release({ state: "unknown", error: "GitHub answered 502" }), null)).toEqual(
      [
        {
          text: "Running ae-v0.18.0.5",
          muted: false,
          release: { running: "ae-v0.18.0.5", queued: null },
        },
        { text: "The newest release is unknown (GitHub answered 502).", muted: true },
      ],
    );
    expect(releaseLines(null, new Error("server down"))).toEqual([
      { text: "The newest release is unknown (server down).", muted: true },
    ]);
  });

  it("names the settings the server lacks, and stays quiet when none or unknown", () => {
    expect(envText(["AE_LIMITS_STATE_FILE"])).toBe(
      "The server lacks 1 setting config/server.env.example sets: AE_LIMITS_STATE_FILE.",
    );
    expect(releaseLines(release({ env_missing: ["AE_A", "AE_B"] }), null).at(-1)).toEqual({
      text: "The server lacks 2 settings config/server.env.example sets: AE_A, AE_B.",
      muted: false,
    });
    expect(envText([])).toBeNull();
    expect(envText(null)).toBeNull();
    expect(envText(undefined)).toBeNull();
  });

  it("counts the time left in hours and minutes, rounded up", () => {
    expect(leftText(72 * 60)).toBe("1 h 12 min left");
    expect(leftText(72 * 60 - 1)).toBe("1 h 12 min left");
    expect(leftText(7200)).toBe("2 h left");
    expect(leftText(600)).toBe("10 min left");
    expect(leftText(30)).toBe("1 min left");
  });

  it("says deploys run on their own when nothing holds them", () => {
    expect(idleText([1, 2, 3])).toBe(
      "Deploys run automatically. A hold pauses them for up to 3 hours and ends on its own.",
    );
  });

  it("reads every 60 s, or just after a hold that ends sooner", () => {
    expect(nextDelay(null, NOW, NOW)).toBe(POLL_MS);
    expect(nextDelay(notHeld(), NOW, NOW)).toBe(POLL_MS);
    expect(nextDelay(held(), NOW, NOW)).toBe(POLL_MS);
    expect(nextDelay(held({ remaining_s: 20 }), NOW, NOW)).toBe(21_000);
    expect(nextDelay(held({ remaining_s: 20 }), NOW, NOW + 30_000)).toBe(1000);
  });
});

describe("the section on the page", () => {
  it("leads the Admin Center, above Fabric capacity, with its own heading", async () => {
    const { context } = stubContext({
      [GET_RELEASE]: [ok(release())],
      [GET_HOLD]: [ok(notHeld())],
      "GET /v1/ae/fabric": [ok({ configured: false, capacities: [], note: "No Fabric." })],
    });
    await activateFabric(context);
    await flush();
    // Since the dashboard redesign (2026-10-20): the first card of the first column.
    const first = document.querySelector<HTMLElement>(".admin-board > .admin-col .admin-card");
    expect(first?.dataset.card).toBe("deploys");
    const heading = document.getElementById("admin-deploys-h");
    expect(heading?.tagName).toBe("H2");
    expect(heading?.textContent).toBe("Deploys");
    expect(first?.querySelector<HTMLElement>(".deploy-pill")?.hidden).toBe(true);
    expect(lines(first as HTMLElement)).toEqual([
      "Running ae-v0.18.0.5",
      "Deploys run automatically. A hold pauses them for up to 3 hours and ends on its own.",
    ]);
  });

  it("shows the holder's name only, with the full address in the title", () => {
    const email = "pat.lee@example.com";
    const [line] = holdLines(held({ set_by: email }), null, NOW);
    expect(line?.text).toBe(`Held until ${whenText(Date.parse(UNTIL), NOW)} by pat.lee`);
    expect(line?.by).toMatchObject({ name: "pat.lee", full: email });
    expect(holdTile(held({ set_by: email }), null, NOW, NOW)).toMatchObject({
      note: "held by pat.lee",
      title: `Held by ${email}`,
    });
    // A value with no "@" shows whole, with no tooltip to add.
    expect(holdLines(held({ set_by: "reid" }), null, NOW)[0]?.by?.name).toBe("reid");
    expect(holdTile(held({ set_by: "reid" }), null, NOW, NOW).title).toBeUndefined();
  });

  it("draws the holder in a span that never breaks and carries the address", async () => {
    const { context } = stubContext({
      [GET_RELEASE]: [ok(release())],
      [GET_HOLD]: [ok(held({ set_by: "pat.lee@example.com" }))],
    });
    const node = await mount(context);
    const span = node.querySelector<HTMLElement>(".deploy-holder");
    expect(span?.textContent).toBe("pat.lee");
    expect(span?.title).toBe("pat.lee@example.com");
  });

  it("says what runs, what waits and the time left on a hold, with Resume", async () => {
    const { context } = stubContext({
      [GET_RELEASE]: [ok(release({ latest: "ae-v0.18.0.6", state: "waiting", mid_turn: 2 }))],
      [GET_HOLD]: [ok(held())],
    });
    const node = await mount(context);
    expect(lines(node)).toEqual([
      "ae-v0.18.0.5 \u2192 ae-v0.18.0.6 queued",
      "Waiting for the hold to end",
      `Held until ${whenText(Date.parse(UNTIL), NOW)} by reid`,
      "1 h 12 min left",
    ]);
    expect(mutedLines(node)).toEqual(["Waiting for the hold to end"]);
    expect(node.querySelector(".ring-big")?.textContent).toBe("1:12");
    expect(node.querySelector<HTMLElement>(".deploy-pill")?.hidden).toBe(false);
    expect(buttonNames(node)).toEqual(["Hold 1 h", "Hold 2 h", "Hold 3 h", "Resume"]);
    // Icons are decorative Tabler svgs beside the words; no emoji anywhere.
    // The hold control's first segment carries the pause icon for all three (2026-10-20).
    expect(buttons(node).map((item) => item.querySelectorAll("svg").length)).toEqual([1, 0, 0, 1]);
    for (const svg of node.querySelectorAll(".deploy-acts svg")) {
      expect(svg.getAttribute("aria-hidden")).toBe("true");
    }
    expect(node.textContent).not.toMatch(/\p{Extended_Pictographic}/u);
    expect(node.querySelector(".deploy-status")?.getAttribute("aria-live")).toBe("polite");
  });

  it("counts the time left down between reads", async () => {
    const { context } = stubContext({
      [GET_RELEASE]: [ok(release()), ok(release())],
      [GET_HOLD]: [ok(held()), ok(held({ remaining_s: 71 * 60 }))],
    });
    const node = await mount(context);
    expect(left(node)).toBe("1 h 12 min left");
    await vi.advanceTimersByTimeAsync(POLL_MS);
    expect(left(node)).toBe("1 h 11 min left");
  });

  it("offers only the Hold buttons when nothing is held, and says one busy session", async () => {
    const { context } = stubContext({
      [GET_RELEASE]: [ok(release({ latest: "ae-v0.18.0.6", state: "waiting", mid_turn: 1 }))],
      [GET_HOLD]: [ok(notHeld())],
    });
    const node = await mount(context);
    expect(lines(node)).toEqual([
      "ae-v0.18.0.5 \u2192 ae-v0.18.0.6 queued",
      "Waiting for 1 busy session",
      "Deploys run automatically. A hold pauses them for up to 3 hours and ends on its own.",
    ]);
    expect(buttonNames(node)).toEqual(["Hold 1 h", "Hold 2 h", "Hold 3 h"]);
  });

  it("says a hold the server could not read lets deploys go ahead", async () => {
    const { context } = stubContext({
      [GET_RELEASE]: [ok(release({ state: "unknown", error: "GitHub answered 502" }))],
      [GET_HOLD]: [ok(notHeld({ error: "the hold file is not valid JSON" }))],
    });
    const node = await mount(context);
    expect(mutedLines(node)).toEqual([
      "The newest release is unknown (GitHub answered 502).",
      "The hold could not be read (the hold file is not valid JSON), so deploys go ahead.",
    ]);
    expect(buttonNames(node)).toEqual(["Hold 1 h", "Hold 2 h", "Hold 3 h"]);
  });

  it("says why when both reads fail, and still offers a hold", async () => {
    const { context } = stubContext({});
    const node = await mount(context);
    expect(mutedLines(node)).toEqual([
      "The newest release is unknown (GET /v1/ae/release answered 500).",
      "The hold could not be read (no reply queued for /v1/ae/deploy/hold).",
    ]);
    expect(buttonNames(node)).toEqual(["Hold 1 h", "Hold 2 h", "Hold 3 h"]);
  });
});

describe("hold and resume", () => {
  it("posts the hours at once, holds every button while out, then shows the answer", async () => {
    const gate = gatedContext();
    const node = deploysSection();
    document.getElementById("root")?.appendChild(node);
    activateDeploys(gate.context, node);
    gate.answer(GET_RELEASE, ok(release()));
    gate.answer(GET_HOLD, ok(notHeld()));
    await flush();
    buttonNamed(node, "Hold 2 h").click();
    await flush();
    // No confirm step: the post is out.
    expect(callsTo(gate.request, POST_HOLD)).toEqual([
      { method: "POST", path: "/v1/ae/deploy/hold", body: { hours: 2 } },
    ]);
    expect(buttonNames(node)).toEqual(["Hold 1 h", "Holding...", "Hold 3 h"]);
    expect(buttons(node).every((item) => item.disabled)).toBe(true);
    // A second tap while it is out sends nothing.
    buttonNamed(node, "Hold 1 h").click();
    await flush();
    expect(callsTo(gate.request, POST_HOLD)).toHaveLength(1);
    gate.answer(POST_HOLD, ok(held({ remaining_s: 7200, until: "2026-10-20T04:34:00Z" })));
    await flush();
    const until = whenText(Date.parse("2026-10-20T04:34:00Z"), NOW);
    expect(statusOf(node)).toBe(`Deploys are on hold until ${until}.`);
    expect(node.querySelector(".deploy-status .notice-ok")).not.toBeNull();
    expect(lines(node).slice(-2)).toEqual([`Held until ${until} by reid`, "2 h left"]);
    expect(buttonNames(node)).toEqual(["Hold 1 h", "Hold 2 h", "Hold 3 h", "Resume"]);
    expect(buttons(node).some((item) => item.disabled)).toBe(false);
    // The hold came with the answer; the release is read again.
    expect(gate.pending(GET_RELEASE)).toBe(1);
    expect(gate.pending(GET_HOLD)).toBe(0);
    gate.answer(GET_RELEASE, ok(release({ running: "ae-v0.18.0.6", latest: "ae-v0.18.0.6" })));
    await flush();
    expect(lines(node)[0]).toBe("Running ae-v0.18.0.6");
    expect(statusOf(node)).toBe(`Deploys are on hold until ${until}.`);
  });

  it("resumes with a POST and drops Resume", async () => {
    const { context, request } = stubContext({
      [GET_RELEASE]: [ok(release()), ok(release())],
      [GET_HOLD]: [ok(held())],
      [POST_RESUME]: [ok(notHeld())],
    });
    const node = await mount(context);
    buttonNamed(node, "Resume").click();
    await flush();
    expect(callsTo(request, POST_RESUME)).toHaveLength(1);
    expect(statusOf(node)).toBe("Deploys resumed. They run automatically again.");
    expect(buttonNames(node)).toEqual(["Hold 1 h", "Hold 2 h", "Hold 3 h"]);
    expect(lines(node)).toContain(
      "Deploys run automatically. A hold pauses them for up to 3 hours and ends on its own.",
    );
    expect(callsTo(request, GET_RELEASE)).toHaveLength(2);
    expect(callsTo(request, GET_HOLD)).toHaveLength(1);
  });

  it("says why an action failed on the status line and frees the buttons", async () => {
    const { context } = stubContext({
      [GET_RELEASE]: [ok(release()), ok(release())],
      [GET_HOLD]: [ok(held())],
      [POST_HOLD]: [failed(500, "The deploy hold could not be saved.")],
    });
    const node = await mount(context);
    buttonNamed(node, "Hold 3 h").click();
    await flush();
    expect(statusOf(node)).toBe("Could not hold deploys: The deploy hold could not be saved.");
    expect(node.querySelector(".deploy-status .notice-error")?.getAttribute("role")).toBe("alert");
    // The hold read before still shows, and every button is back.
    expect(lines(node)).toContain("1 h 12 min left");
    expect(buttonNames(node)).toEqual(["Hold 1 h", "Hold 2 h", "Hold 3 h", "Resume"]);
    expect(buttons(node).some((item) => item.disabled)).toBe(false);
  });

  it("keeps an action's hold over a read that began before it", async () => {
    const gate = gatedContext();
    const node = deploysSection();
    document.getElementById("root")?.appendChild(node);
    activateDeploys(gate.context, node);
    gate.answer(GET_RELEASE, ok(release()));
    gate.answer(GET_HOLD, ok(notHeld()));
    await flush();
    // A focus read goes out, then Hold 1 h.
    window.dispatchEvent(new Event("focus"));
    await flush();
    buttonNamed(node, "Hold 1 h").click();
    await flush();
    gate.answer(POST_HOLD, ok(held({ remaining_s: 3600 })));
    await flush();
    expect(buttonNames(node)).toContain("Resume");
    // The focus read answers late with the hold as it was: it is dropped.
    gate.answer(GET_RELEASE, ok(release()));
    gate.answer(GET_HOLD, ok(notHeld()));
    await flush();
    expect(buttonNames(node)).toContain("Resume");
    expect(lines(node)).toContain("1 h left");
  });
});

describe("review fixes", () => {
  /** Mount on a gated context with the first two reads answered. */
  async function mountGated(first: DeployHold) {
    const gate = gatedContext();
    const node = deploysSection();
    document.getElementById("root")?.appendChild(node);
    activateDeploys(gate.context, node);
    gate.answer(GET_RELEASE, ok(release()));
    gate.answer(GET_HOLD, ok(first));
    await flush();
    return { gate, node };
  }

  it("keeps the last good hold and Resume when a later hold read fails", async () => {
    const { gate, node } = await mountGated(held());
    vi.advanceTimersByTime(POLL_MS);
    await flush();
    gate.answer(GET_RELEASE, ok(release()));
    gate.answer(GET_HOLD, failed(500, "boom"));
    await flush();
    expect(lines(node).some((line) => line?.startsWith("The hold could not be read"))).toBe(true);
    expect(lines(node).some((line) => line?.startsWith("Held until"))).toBe(true);
    expect(buttonNames(node)).toContain("Resume");
  });

  it("reads the hold again after a failed action, even past a read already out", async () => {
    const { gate, node } = await mountGated(notHeld());
    // A focus read goes out and stays out.
    window.dispatchEvent(new Event("focus"));
    await flush();
    buttonNamed(node, "Hold 1 h").click();
    await flush();
    gate.answer(POST_HOLD, failed(504, "timed out"));
    await flush();
    expect(statusOf(node)).toBe("Could not hold deploys: timed out");
    // The early read answers with the old state and is dropped.
    gate.answer(GET_RELEASE, ok(release()));
    gate.answer(GET_HOLD, ok(notHeld()));
    await flush();
    expect(buttonNames(node)).not.toContain("Resume");
    // A new pair of reads goes out and shows the hold the server did apply.
    expect(gate.pending(GET_HOLD)).toBe(1);
    gate.answer(GET_RELEASE, ok(release()));
    gate.answer(GET_HOLD, ok(held({ remaining_s: 3600 })));
    await flush();
    expect(lines(node)).toContain("1 h left");
    expect(buttonNames(node)).toContain("Resume");
    // The hold now differs from the state at the failure: the failure notice clears.
    expect(statusOf(node)).toBe("");
  });

  it("clears a failure notice when a read shows the hold did apply, with a new end", async () => {
    const { gate, node } = await mountGated(held());
    buttonNamed(node, "Hold 3 h").click();
    await flush();
    gate.answer(POST_HOLD, failed(504, "timed out"));
    await flush();
    expect(statusOf(node)).toBe("Could not hold deploys: timed out");
    gate.answer(GET_RELEASE, ok(release()));
    gate.answer(GET_HOLD, ok(held({ until: "2026-10-20T05:34:00Z", remaining_s: 3 * 3600 })));
    await flush();
    expect(lines(node)).toContain("3 h left");
    expect(statusOf(node)).toBe("");
  });

  it("clears the status notice when a read shows the other held state", async () => {
    const { gate, node } = await mountGated(notHeld());
    buttonNamed(node, "Hold 1 h").click();
    await flush();
    gate.answer(POST_HOLD, ok(held({ remaining_s: 3600 })));
    await flush();
    gate.answer(GET_RELEASE, ok(release()));
    await flush();
    expect(statusOf(node)).toMatch(/^Deploys are on hold until/);
    // A read that still shows the hold leaves the notice.
    vi.advanceTimersByTime(POLL_MS);
    await flush();
    gate.answer(GET_RELEASE, ok(release()));
    gate.answer(GET_HOLD, ok(held({ remaining_s: 3500 })));
    await flush();
    expect(statusOf(node)).toMatch(/^Deploys are on hold until/);
    // Someone resumes from another tab: the next read drops the notice.
    vi.advanceTimersByTime(POLL_MS);
    await flush();
    gate.answer(GET_RELEASE, ok(release()));
    gate.answer(GET_HOLD, ok(notHeld()));
    await flush();
    expect(statusOf(node)).toBe("");
  });

  it("puts the focus on the first Hold button after Resume removes its own button", async () => {
    const { gate, node } = await mountGated(held());
    document.body.appendChild(node);
    buttonNamed(node, "Resume").focus();
    buttonNamed(node, "Resume").click();
    await flush();
    gate.answer(POST_RESUME, ok(notHeld()));
    await flush();
    expect(buttonNames(node)).not.toContain("Resume");
    expect(document.activeElement).toBe(buttonNamed(node, "Hold 1 h"));
  });

  it("leaves the focus on a control elsewhere that the user moved to meanwhile", async () => {
    const { gate, node } = await mountGated(notHeld());
    const other = document.createElement("input");
    document.body.appendChild(other);
    buttonNamed(node, "Hold 2 h").focus();
    buttonNamed(node, "Hold 2 h").click();
    await flush();
    other.focus();
    gate.answer(POST_HOLD, ok(held()));
    await flush();
    expect(document.activeElement).toBe(other);
  });
});

describe("keeping it fresh", () => {
  it("reads both every 60 s and on focus, and stops when the page closes", async () => {
    const many = (reply: () => Reply) => Array.from({ length: 6 }, reply);
    const { context, request } = stubContext({
      [GET_RELEASE]: many(() => ok(release())),
      [GET_HOLD]: many(() => ok(notHeld())),
    });
    await mount(context);
    const reads = () => [callsTo(request, GET_RELEASE).length, callsTo(request, GET_HOLD).length];
    expect(reads()).toEqual([1, 1]);
    await vi.advanceTimersByTimeAsync(POLL_MS);
    expect(reads()).toEqual([2, 2]);
    window.dispatchEvent(new Event("focus"));
    await flush();
    expect(reads()).toEqual([3, 3]);
    deactivateDeploys();
    await vi.advanceTimersByTimeAsync(POLL_MS * 3);
    window.dispatchEvent(new Event("focus"));
    document.dispatchEvent(new Event("visibilitychange"));
    await flush();
    expect(reads()).toEqual([3, 3]);
  });

  it("skips the scheduled read while the tab is hidden and reads on return", async () => {
    const { context, request } = stubContext({
      [GET_RELEASE]: [ok(release()), ok(release())],
      [GET_HOLD]: [ok(notHeld()), ok(notHeld())],
    });
    await mount(context);
    setVisibility("hidden");
    await vi.advanceTimersByTimeAsync(POLL_MS + 1000);
    expect(callsTo(request, GET_HOLD)).toHaveLength(1);
    setVisibility("visible");
    document.dispatchEvent(new Event("visibilitychange"));
    await flush();
    expect(callsTo(request, GET_HOLD)).toHaveLength(2);
  });

  it("reads again just after a hold ends rather than a minute later", async () => {
    const { context, request } = stubContext({
      [GET_RELEASE]: [ok(release()), ok(release())],
      [GET_HOLD]: [ok(held({ remaining_s: 20 })), ok(notHeld())],
    });
    const node = await mount(context);
    expect(left(node)).toBe("1 min left");
    await vi.advanceTimersByTimeAsync(21_000);
    expect(callsTo(request, GET_HOLD)).toHaveLength(2);
    expect(buttonNames(node)).toEqual(["Hold 1 h", "Hold 2 h", "Hold 3 h"]);
  });

  it("lets nothing that answers after the page closed touch the section", async () => {
    const gate = gatedContext();
    const node = deploysSection();
    document.getElementById("root")?.appendChild(node);
    activateDeploys(gate.context, node);
    await flush();
    expect(lines(node)).toEqual(["Reading..."]);
    deactivateDeploys();
    gate.answer(GET_RELEASE, ok(release()));
    gate.answer(GET_HOLD, ok(held()));
    await flush();
    expect(lines(node)).toEqual(["Reading..."]);
    expect(buttonNames(node)).toEqual(["Hold 1 h", "Hold 2 h", "Hold 3 h"]);
  });

  it("lets an action that answers after the page closed touch nothing", async () => {
    const gate = gatedContext();
    const node = deploysSection();
    document.getElementById("root")?.appendChild(node);
    activateDeploys(gate.context, node);
    gate.answer(GET_RELEASE, ok(release()));
    gate.answer(GET_HOLD, ok(notHeld()));
    await flush();
    buttonNamed(node, "Hold 1 h").click();
    await flush();
    deactivateDeploys();
    gate.answer(POST_HOLD, ok(held()));
    await flush();
    expect(statusOf(node)).toBe("");
    expect(buttonNames(node)).toEqual(["Holding...", "Hold 2 h", "Hold 3 h"]);
    expect(gate.pending(GET_RELEASE)).toBe(0);
  });
});

describe("phone layout", () => {
  it("wraps the buttons at 375 px and gives them the 44 px touch targets", () => {
    const block = (selector: string) => {
      const start = css.indexOf(`${selector} {`);
      expect(start, `${selector} rule`).toBeGreaterThanOrEqual(0);
      return css.slice(start, css.indexOf("}", start));
    };
    expect(block(".deploy-acts")).toContain("flex-wrap: wrap");
    expect(block(".deploy-line")).toContain("overflow-wrap: anywhere");
    // The card is a Fabric card, so Fabric's 44 px rule covers its buttons.
    expect(deploysSection().querySelector(".card-fabric")).not.toBeNull();
    expect(css).toMatch(/\.card-fabric \.btn \{\s*min-height: 44px;\s*min-width: 44px;\s*\}/);
  });
});
