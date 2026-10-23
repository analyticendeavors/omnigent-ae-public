// The Admin Center's Azure spend card (2026-10-22): the month so far, the
// pace, the subscriptions, the top services and the flags. Every call is
// mocked: the route is GET /v1/ae/azure-cost.
import type { ExtensionContext } from "@omnigent/extension-sdk";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { AzureCostAnswer, AzureView } from "../src/api";
import {
  activateAzure,
  azureSection,
  COST_URL,
  azureTile,
  dayText,
  deactivateAzure,
  flagText,
  money,
  paceDelta,
  POLL_MS,
  READING_POLL_MS,
} from "../src/azure";
import { getTile, resetTiles } from "../src/tiles";

const sdk = vi.hoisted(() => ({ defineExtension: vi.fn() }));
vi.mock("@omnigent/extension-sdk", () => sdk);

type Reply = { ok: boolean; status: number; body: unknown };

const GET = "GET /v1/ae/azure-cost";
const NOW = Date.parse("2026-10-21T15:00:00Z");

function view(overrides: Partial<AzureView> = {}): AzureView {
  return {
    currency: "USD",
    month: { spent: 412, pace: 590, lastMonth: 540, through: "2026-10-20" },
    daily: Array.from({ length: 21 }, (_, i) => ({
      date: `2026-09-${i + 1}`,
      cost: 10 + i,
    })),
    subscriptions: [
      { id: "a", name: "Juniper Production", spent: 268, currency: "USD", counted: true, error: null },
      { id: "b", name: "Juniper Dev", spent: 97, currency: "USD", counted: true, error: null },
    ],
    services: [
      { name: "SQL Database", cost: 171 },
      { name: "App Service", cost: 88 },
    ],
    flags: [],
    ...overrides,
  };
}

const flag = {
  resource: "sql-juniper-prod-01",
  subscription: "Juniper Production",
  day: "2026-10-20",
  cost: 10.12,
  average: 0.84,
  ratio: 12,
};

const answer = (v: AzureView | null, extra: Partial<AzureCostAnswer> = {}): AzureCostAnswer => ({
  configured: true,
  note: null,
  reading: false,
  asOf: new Date(NOW - 5 * 60_000).toISOString(),
  view: v,
  ...extra,
});

const ok = (body: unknown): Reply => ({ ok: true, status: 200, body });
const failed = (code: number, message: string): Reply => ({
  ok: false,
  status: code,
  body: { error: { code: "x", message } },
});

type Call = { method: string; path: string };

function stubContext(replies: Reply[], openExternal = vi.fn(async () => undefined)) {
  const request = vi.fn(async ({ method, path }: Call) => {
    if (`${method} ${path}` !== GET) return failed(500, `unexpected ${method} ${path}`);
    return replies.shift() ?? failed(500, "no reply queued");
  });
  const context = {
    view: "fabric",
    server: { request },
    navigation: { openExternal },
  } as unknown as ExtensionContext;
  return { context, request, openExternal };
}

const flush = () => vi.advanceTimersByTimeAsync(0);

async function mount(context: ExtensionContext): Promise<HTMLElement> {
  const node = azureSection();
  document.getElementById("root")?.appendChild(node);
  activateAzure(context, node);
  await flush();
  return node;
}

const notices = (node: ParentNode) =>
  [...node.querySelectorAll(".notice")].map((item) => item.textContent);

beforeEach(() => {
  document.body.replaceChildren();
  const root = document.createElement("div");
  root.id = "root";
  document.body.appendChild(root);
  vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout", "Date"] });
  vi.setSystemTime(NOW);
  resetTiles();
});

afterEach(() => {
  deactivateAzure();
  resetTiles();
  vi.useRealTimers();
  vi.restoreAllMocks();
});

describe("the words", () => {
  it("writes money in whole dollars from $100 and in cents under it", () => {
    expect(money(412.4)).toBe("$412");
    expect(money(10.12)).toBe("$10.12");
    expect(money(1234)).toBe("$1,234");
  });

  it("names a day without a timezone moving it", () => {
    expect(dayText("2026-10-20")).toBe("Oct 20");
    expect(dayText("2026-11-01")).toBe("Nov 1");
  });

  it("gives the pace against last month, or nothing to compare", () => {
    expect(paceDelta(590, 540)).toBe("+9%");
    expect(paceDelta(500, 540)).toBe("-7%");
    expect(paceDelta(500, 0)).toBeNull();
    expect(paceDelta(null, 540)).toBeNull();
  });

  it("says what a flag found", () => {
    expect(flagText(flag, "USD")).toBe(
      "sql-juniper-prod-01 cost $10.12 on Oct 20, 12x its $0.84 a day average",
    );
  });
});

describe("the tile", () => {
  it("is idle and says so when the card is not set up", () => {
    expect(azureTile(null, false, false, null)).toMatchObject({
      health: "idle",
      summary: "Not set up",
    });
  });

  it("reads the month and is ok when nothing is off", () => {
    expect(azureTile(view(), true, false, null)).toMatchObject({
      value: "$412",
      note: "pace +9%",
      health: "ok",
      summary: "$412 this month",
    });
  });

  it("says nothing about pace when there is no last month to compare", () => {
    const first = view({ month: { spent: 40, pace: 90, lastMonth: 0, through: "2026-10-20" } });
    expect(azureTile(first, true, false, null)).toMatchObject({ note: "", health: "ok" });
  });

  it("says the spike check is unavailable when a counted row has a note, health unchanged", () => {
    const sub = {
      id: "a",
      name: "Juniper Production",
      spent: 268,
      currency: "USD",
      counted: true,
      error: null,
      note: "Spike check unavailable: x. Totals are complete.",
    };
    const tile = azureTile(view({ subscriptions: [sub] }), true, false, null);
    expect(tile).toMatchObject({
      health: "ok",
      note: "spike check unavailable",
      summary: "$412 this month · spike check unavailable",
    });
  });

  it("counts flags and goes amber", () => {
    const one = azureTile(view({ flags: [flag] }), true, false, null);
    expect(one).toMatchObject({
      health: "attention",
      summary: "$412 this month · 1 flag",
    });
    const two = azureTile(view({ flags: [flag, flag] }), true, false, null);
    expect(two.summary).toBe("$412 this month · 2 flags");
  });

  it("goes amber when a subscription could not be read", () => {
    const sub = {
      id: "b",
      name: "Juniper Dev",
      spent: 0,
      currency: "USD",
      counted: true,
      error: "rate limiting",
    };
    const tile = azureTile(view({ subscriptions: [sub] }), true, false, null);
    expect(tile).toMatchObject({
      health: "attention",
      summary: "$412 this month · 1 not read",
    });
  });
});

describe("the card", () => {
  it("shows the month, the pace, the subscriptions and the services", async () => {
    const { context } = stubContext([ok(answer(view()))]);
    const node = await mount(context);
    expect(node.querySelector(".azure-big")?.textContent).toBe("$412 this month, through Oct 20");
    expect(node.querySelector(".azure-pace")?.textContent).toBe(
      "Pace $590 vs $540 last month (+9%)",
    );
    expect([...node.querySelectorAll(".azure-sub-name")].map((n) => n.textContent)).toEqual([
      "Juniper Production",
      "Juniper Dev",
    ]);
    const bar = node.querySelector<HTMLElement>(".azure-sub .azure-bar i");
    expect(bar?.style.width).toBe("65%");
    expect([...node.querySelectorAll(".azure-services li")].map((n) => n.textContent)).toEqual([
      "SQL Database$171",
      "App Service$88.00",
    ]);
    expect(node.querySelector(".azure-spark polyline")?.getAttribute("points")).toBeTruthy();
    expect(node.querySelector(".azure-refreshed")?.textContent).toMatch(/^Updated /);
    expect(getTile("azure")).toMatchObject({ value: "$412", health: "ok" });
  });

  it("names the resource that jumped", async () => {
    const { context } = stubContext([ok(answer(view({ flags: [flag] })))]);
    const node = await mount(context);
    const found = node.querySelector(".azure-flag");
    expect(found?.textContent).toContain("Check this: sql-juniper-prod-01 cost $10.12 on Oct 20");
    expect(found?.textContent).toContain("Juniper Production");
    expect(getTile("azure")?.health).toBe("attention");
  });

  it("shows the server's how-to sentence when it is not set up", async () => {
    const note = "Azure spend is not configured: put azure-cost-client-secret in Key Vault.";
    const { context } = stubContext([ok({ configured: false, note })]);
    const node = await mount(context);
    expect(node.querySelector(".empty")?.textContent).toBe(note);
    expect(getTile("azure")).toMatchObject({ value: "Off", health: "idle" });
  });

  it("tells a non-admin quietly that Azure spend is for admins, and stops asking", async () => {
    const refused: Reply = {
      ok: false,
      status: 403,
      body: { error: { code: "admin_only", message: "Only an OmniGent admin can see Azure spend." } },
    };
    const { context, request } = stubContext([refused]);
    const node = await mount(context);
    expect(node.querySelector(".empty")?.textContent).toBe("Azure spend is for admins.");
    expect(notices(node)).toEqual([]);
    expect(getTile("azure")).toMatchObject({ health: "idle" });
    await vi.advanceTimersByTimeAsync(POLL_MS * 2);
    expect(request).toHaveBeenCalledTimes(1);
  });

  it("says Reading while the server's first read runs, and asks again in seconds", async () => {
    const { context, request } = stubContext([
      ok(answer(null, { reading: true, asOf: null })),
      ok(answer(view())),
    ]);
    const node = await mount(context);
    expect(node.querySelector(".azure-list")?.textContent).toBe("Reading...");
    expect(getTile("azure")?.value).toBe("Reading");
    await vi.advanceTimersByTimeAsync(READING_POLL_MS);
    expect(request).toHaveBeenCalledTimes(2);
    expect(node.querySelector(".azure-big")?.textContent).toContain("$412");
  });

  it("shows a subscription's error under its row", async () => {
    const sub = {
      id: "b",
      name: "Juniper Dev",
      spent: 0,
      currency: "USD",
      counted: true,
      error: "Cost Management is rate limiting.",
    };
    const { context } = stubContext([ok(answer(view({ subscriptions: [sub] })))]);
    const node = await mount(context);
    expect(node.querySelector(".azure-sub-err")?.textContent).toBe(
      "Cost Management is rate limiting.",
    );
  });

  it("shows a subscription's note as muted text and does not count it as not read", async () => {
    const sub = {
      id: "b",
      name: "Juniper Dev",
      spent: 97,
      currency: "USD",
      counted: true,
      error: null,
      note: "Spike check unavailable: Cost Management answered 500. Totals are complete.",
    };
    const { context } = stubContext([ok(answer(view({ subscriptions: [sub] })))]);
    const node = await mount(context);
    expect(node.querySelector(".azure-sub-caveat")?.textContent).toContain("Totals are complete.");
    expect(node.querySelector(".azure-sub-err")).toBeNull();
    expect(node.textContent).not.toContain("not read");
  });

  it("shows a subscription billed in pounds as its own line, outside the total", async () => {
    const uk = {
      id: "c",
      name: "Juniper Sandbox",
      spent: 12.5,
      currency: "GBP",
      counted: false,
      error: null,
    };
    const subs = [...view().subscriptions, uk];
    const { context } = stubContext([ok(answer(view({ subscriptions: subs })))]);
    const node = await mount(context);
    const line = node.querySelector(".azure-sub-apart");
    expect(line?.querySelector(".azure-amt")?.textContent).toBe("\u00a312.50");
    expect(line?.querySelector(".azure-bar")).toBeNull();
    expect(line?.textContent).toContain("Billed in GBP, so it is not in the USD total.");
    // The counted rows keep their bars and dollars.
    expect(node.querySelectorAll(".azure-sub .azure-bar")).toHaveLength(2);
    // The currency note is not an error.
    expect(line?.querySelector(".azure-sub-err")).toBeNull();
  });

  it("lists the pounds line last however the subscriptions are ordered, and keeps its error apart", async () => {
    const uk = {
      id: "c",
      name: "Juniper Sandbox",
      spent: 12.5,
      currency: "GBP",
      counted: false,
      error: "Cost Management is rate limiting.",
    };
    const subs = [uk, ...view().subscriptions];
    const { context } = stubContext([ok(answer(view({ subscriptions: subs })))]);
    const node = await mount(context);
    const names = [...node.querySelectorAll(".azure-sub-name")].map((n) => n.textContent);
    expect(names).toEqual(["Juniper Production", "Juniper Dev", "Juniper Sandbox"]);
    const line = node.querySelector(".azure-sub-apart");
    expect(line?.querySelector(".azure-sub-note")?.textContent).toContain("Billed in GBP");
    expect(line?.querySelector(".azure-sub-err")?.textContent).toBe(
      "Cost Management is rate limiting.",
    );
  });

  it("reads a row from an older server that sends no currency as counted, in the total's", async () => {
    const old = { id: "a", name: "Juniper Production", spent: 268, error: null };
    const { context } = stubContext([
      ok(answer(view({ subscriptions: [old] as unknown as AzureView["subscriptions"] }))),
    ]);
    const node = await mount(context);
    expect(node.querySelector(".azure-sub .azure-amt")?.textContent).toBe("$268");
    expect(node.querySelector(".azure-sub-apart")).toBeNull();
  });

  it("reads again every minute and on focus", async () => {
    const { context, request } = stubContext([
      ok(answer(view())),
      ok(answer(view())),
      ok(answer(view())),
    ]);
    await mount(context);
    expect(request).toHaveBeenCalledTimes(1);
    await vi.advanceTimersByTimeAsync(POLL_MS);
    expect(request).toHaveBeenCalledTimes(2);
    window.dispatchEvent(new Event("focus"));
    await flush();
    expect(request).toHaveBeenCalledTimes(3);
  });

  it("drops the old numbers when the server says it is no longer set up", async () => {
    const note = "Azure spend is not configured.";
    const { context } = stubContext([ok(answer(view())), ok({ configured: false, note })]);
    const node = await mount(context);
    window.dispatchEvent(new Event("focus"));
    await flush();
    expect(node.querySelector(".azure-big")).toBeNull();
    expect(node.querySelector(".empty")?.textContent).toBe(note);
    expect(getTile("azure")).toMatchObject({ value: "Off" });
  });

  it("keeps the last good numbers and marks the tile when a read fails", async () => {
    const { context } = stubContext([ok(answer(view())), failed(502, "upstream down")]);
    const node = await mount(context);
    window.dispatchEvent(new Event("focus"));
    await flush();
    expect(node.querySelector(".azure-big")?.textContent).toContain("$412");
    expect(notices(node)).toHaveLength(1);
    expect(notices(node)[0]).toMatch(/^Azure spend could not be read/);
    expect(getTile("azure")?.note).toMatch(/last answer$/);
  });

  it("opens the portal's cost analysis from the button", async () => {
    const { context, openExternal } = stubContext([ok(answer(view()))]);
    const node = await mount(context);
    node.querySelector<HTMLButtonElement>(".azure-open")?.click();
    await flush();
    expect(openExternal).toHaveBeenCalledWith(COST_URL);
    expect(node.querySelector(".azure-link .notice")).toBeNull();
  });

  it("opens one tab however often the button is tapped while the host answers", async () => {
    let release: () => void = () => {};
    const slow = vi.fn(() => new Promise<undefined>((done) => (release = () => done(undefined))));
    const { context } = stubContext([ok(answer(view()))], slow);
    const node = await mount(context);
    const open = node.querySelector<HTMLButtonElement>(".azure-open");
    open?.click();
    open?.click();
    await flush();
    expect(slow).toHaveBeenCalledTimes(1);
    release();
    await flush();
    node.querySelector<HTMLButtonElement>(".azure-open")?.click();
    await flush();
    expect(slow).toHaveBeenCalledTimes(2);
  });

  it("shows the URL to copy when the host will not open it", async () => {
    const refused = vi.fn(async () => {
      throw new Error("PermissionDenied");
    });
    const { context } = stubContext([ok(answer(view()))], refused);
    const node = await mount(context);
    node.querySelector<HTMLButtonElement>(".azure-open")?.click();
    await flush();
    expect(node.querySelector(".azure-link .notice")?.textContent).toContain(
      "Could not open Azure",
    );
    expect(node.querySelector(".azure-link code")?.textContent).toBe(COST_URL);
  });

  it("has no link while the card is not set up", async () => {
    const { context } = stubContext([ok({ configured: false, note: "off" })]);
    const node = await mount(context);
    expect(node.querySelector(".azure-open")).toBeNull();
  });

  it("stops reading once deactivated", async () => {
    const { context, request } = stubContext([ok(answer(view())), ok(answer(view()))]);
    await mount(context);
    deactivateAzure();
    window.dispatchEvent(new Event("focus"));
    await vi.advanceTimersByTimeAsync(2 * POLL_MS);
    expect(request).toHaveBeenCalledTimes(1);
  });
});
