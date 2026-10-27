// The Admin Center's Databricks section (2026-10-19): a row per SQL warehouse in
// the Fabric row's style, Start and Stop asking once under the row, and the
// spend strip (two bars against the credit, the headline per cadence, "as of"
// the billing data, and its no access, never read and reading states). A GET
// never wakes a warehouse: only Refresh posts a reading.
import type { ExtensionContext } from "@omnigent/extension-sdk";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { DatabricksOverview, DatabricksSpend, DatabricksWarehouse } from "../src/api";
import { resetMenuState } from "../src/cards";
import {
  activateDatabricks,
  asOfText,
  creditHealth,
  creditTile,
  databricksTile,
  readAgeText,
  readingIsStale,
  barPercent,
  barText,
  databricksSection,
  databricksStrip,
  dayText,
  deactivateDatabricks,
  dollars,
  freshOverview,
  metaItems,
  nextDelay,
  POLL_BUSY_MS,
  POLL_IDLE_MS,
  productName,
  question,
  REFRESH_LABEL,
  spendBars,
  spendDays,
  spendStatusText,
  stateText,
  toneOf,
  topProducts,
} from "../src/databricks";
import { activateFabric, deactivateFabric } from "../src/fabric";
import css from "../src/style.css?raw";

const sdk = vi.hoisted(() => ({ defineExtension: vi.fn() }));
vi.mock("@omnigent/extension-sdk", () => sdk);

type Reply = { ok: boolean; status: number; body: unknown };

const HOST = "https://dbc-0000aaaa-1111.cloud.databricks.com";
const ID = "0123456789abcdef";
const PATH = "/v1/ae/databricks";
const REFRESH_PATH = "/v1/ae/databricks/spend/refresh";

// A fresh row per use: the page updates a row in place after an action.
const warehouse = (extra: Partial<DatabricksWarehouse> = {}): DatabricksWarehouse => ({
  id: ID,
  name: "Serverless Starter Warehouse",
  state: "RUNNING",
  size: "2X-Small",
  autoStopMins: 5,
  serverless: true,
  url: `${HOST}/sql/warehouses/${ID}`,
  ...extra,
});

const spend = (extra: Partial<DatabricksSpend> = {}): DatabricksSpend => ({
  status: "ok",
  message: null,
  readAt: "2026-10-19T15:10:00Z",
  asOf: "2026-10-19T12:00:00Z",
  monthUsd: 12.4,
  totalUsd: 61.25,
  creditUsd: 500,
  cadence: "once",
  creditStart: "2030-01-01",
  creditEnd: "2030-12-31",
  warehouseId: ID,
  ...extra,
});

function overview(
  warehouses: DatabricksWarehouse[],
  extra: Partial<DatabricksOverview> = {},
): DatabricksOverview {
  return {
    configured: true,
    can_act: true,
    note: null,
    host: HOST,
    error: null,
    warehouses,
    spend: spend(),
    ...extra,
  };
}

/** A context whose server answers from a queue per path; anything unqueued is a 500. */
function stubContext(replies: Record<string, Reply[]>, refuseLinks = false) {
  const request = vi.fn(async ({ path }: { method: string; path: string; body?: unknown }) => {
    const queue = replies[path] ?? [];
    return (
      queue.shift() ?? {
        ok: false,
        status: 500,
        body: { error: { code: "unexpected", message: `no reply queued for ${path}` } },
      }
    );
  });
  const openExternal = vi.fn(async (_url: string) => {
    if (refuseLinks) throw new Error("This URL is not allowed");
  });
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

function setVisibility(value: "visible" | "hidden") {
  Object.defineProperty(document, "visibilityState", { configurable: true, get: () => value });
}

const flush = async () => {
  for (let i = 0; i < 5; i++) await new Promise((resolve) => setTimeout(resolve, 0));
};

/**
 * The warehouses' block and the spend strip in one wrapper: activateFabric
 * puts them in two cards (2026-10-20), and a bare test needs neither card.
 */
function frame(): HTMLElement {
  const node = document.createElement("div");
  node.append(databricksSection(), databricksStrip());
  return node;
}

/** Mount the bare section and its spend strip and start them, as activateFabric does. */
async function mount(context: ExtensionContext): Promise<HTMLElement> {
  const node = frame();
  document.getElementById("root")?.appendChild(node);
  activateDatabricks(context, node);
  await flush();
  return node;
}

function cardOf(id: string): HTMLElement {
  const node = document.querySelector<HTMLElement>(`[data-warehouse="${id}"]`);
  if (!node) throw new Error(`no card for ${id}`);
  return node;
}

function nameOf(node: HTMLButtonElement): string {
  return node.getAttribute("aria-label") ?? node.textContent?.trim() ?? "";
}

function buttonNamed(root: ParentNode, name: string): HTMLButtonElement {
  const found = [...root.querySelectorAll("button")].find((node) => nameOf(node) === name);
  if (!found) throw new Error(`no button named ${name}`);
  return found;
}

function buttonNames(root: ParentNode): string[] {
  return [...root.querySelectorAll("button")].map(nameOf);
}

const posts = (request: ReturnType<typeof vi.fn>) =>
  request.mock.calls
    .map(([call]) => call as { method: string; path: string })
    .filter((call) => call.method === "POST")
    .map((call) => call.path);

beforeEach(() => {
  resetMenuState();
  document.body.replaceChildren();
  const root = document.createElement("div");
  root.id = "root";
  document.body.appendChild(root);
});

afterEach(() => {
  deactivateDatabricks();
  deactivateFabric();
  vi.useRealTimers();
  setVisibility("visible");
  vi.restoreAllMocks();
});

describe("the pieces", () => {
  it("names each state in plain words with Fabric's tones", () => {
    expect(stateText("RUNNING")).toBe("Running");
    expect(stateText("STOPPED")).toBe("Stopped");
    expect(toneOf("RUNNING")).toBe("on");
    expect(toneOf("STOPPED")).toBe("off");
    expect(toneOf("STARTING")).toBe("busy");
    expect(toneOf("STOPPING")).toBe("busy");
    expect(toneOf("SOMETHING_NEW")).toBe("unknown");
  });

  it("says size, auto stop and serverless on the meta line, or the busy note", () => {
    expect(metaItems(warehouse())).toEqual(["2X-Small", "auto stop 5 min", "serverless"]);
    expect(metaItems(warehouse({ autoStopMins: 0, serverless: false }))).toEqual([
      "2X-Small",
      "no auto stop",
    ]);
    expect(metaItems(warehouse({ autoStopMins: null }))).toEqual(["2X-Small", "serverless"]);
    expect(metaItems(warehouse({ state: "STARTING" }))).toEqual([
      "Starting. Checking every 15 s.",
    ]);
  });

  it("says what Start bills until, and what Stop cuts off", () => {
    expect(question("start", warehouse())).toBe(
      "Start Serverless Starter Warehouse? It bills until it auto stops after 5 min idle.",
    );
    expect(question("start", warehouse({ autoStopMins: 0 }))).toBe(
      "Start Serverless Starter Warehouse? It has no auto stop, so it bills until someone stops it.",
    );
    expect(question("stop", warehouse())).toBe(
      "Stop Serverless Starter Warehouse? A query still running on it is cancelled.",
    );
  });

  it("polls every 15 s while a warehouse moves or spend is read, else every 60 s", () => {
    expect(nextDelay(null)).toBe(POLL_IDLE_MS);
    expect(nextDelay(overview([warehouse()]))).toBe(POLL_IDLE_MS);
    expect(nextDelay(overview([warehouse({ state: "STOPPED" })]))).toBe(POLL_IDLE_MS);
    expect(nextDelay(overview([warehouse({ state: "STARTING" })]))).toBe(POLL_BUSY_MS);
    expect(nextDelay(overview([warehouse({ state: "STOPPING" })]))).toBe(POLL_BUSY_MS);
    expect(nextDelay(overview([warehouse()], { spend: spend({ status: "reading" }) }))).toBe(
      POLL_BUSY_MS,
    );
    expect(nextDelay(overview([], { configured: false, spend: null }))).toBe(POLL_IDLE_MS);
  });

  it("says money as whole dollars or to the cent, and fills a bar up to the credit", () => {
    expect(dollars(500)).toBe("$500");
    expect(dollars(12.4)).toBe("$12.40");
    expect(barText(12.4, 500)).toBe("$12.40 of $500");
    expect(barText(null, 500)).toBe("not read yet of $500");
    expect(barPercent(125, 500)).toBe(25);
    expect(barPercent(600, 500)).toBe(100);
    expect(barPercent(null, 500)).toBe(0);
  });

  it("leads with the whole credit when granted once and with this month when monthly", () => {
    const once = spendBars(spend());
    expect(once.map((bar) => [bar.key, bar.head])).toEqual([
      ["total", true],
      ["month", false],
    ]);
    expect(once[0].label).toBe(`Credit since ${dayText("2030-01-01")}`);
    const monthly = spendBars(spend({ cadence: "monthly" }));
    expect(monthly.map((bar) => [bar.key, bar.head])).toEqual([
      ["month", true],
      ["total", false],
    ]);
    expect(monthly[0].label).toBe("This month");
  });

  it("reads a credit date as the calendar day, whatever the viewer's zone", () => {
    const text = dayText("2030-01-01");
    expect(text).toContain("1");
    expect(text).toContain("2030");
    expect(dayText("not a date")).toBe("not a date");
  });

  it("says how old the billing data is", () => {
    const now = Date.parse("2026-10-19T12:30:00");
    const today = asOfText(spend({ readAt: null, asOf: "2026-10-19T12:00:00" }), now);
    expect(today).toMatch(/^as of \d{1,2}:00\s?[AP]M \(billing data lags a few hours\)$/);
    const yesterday = asOfText(spend({ readAt: null, asOf: "2026-10-18T09:00:00" }), now);
    expect(yesterday).toMatch(/^as of [A-Z][a-z]{2} 18, \d{1,2}:00\s?[AP]M \(/);
    expect(asOfText(spend({ readAt: null, asOf: null }), now)).toBeNull();
  });

  it("says how long ago the spend was read, from the reading's time", () => {
    const now = Date.parse("2026-10-19T17:10:00Z");
    expect(asOfText(spend({ readAt: "2026-10-19T15:10:00Z" }), now)).toBe(
      "as of 2 h ago (billing data lags a few hours)",
    );
    expect(readAgeText(now - 30_000, now)).toBe("just now");
    expect(readAgeText(now - 35 * 60_000, now)).toBe("35 min ago");
    expect(readAgeText(now - 3 * 86_400_000, now)).toBe("3 d ago");
  });

  it("turns amber past 48 hours: the line, and the credit's health", () => {
    const now = Date.parse("2026-10-23T15:10:00Z");
    expect(readingIsStale(spend({ readAt: "2026-10-21T15:11:00Z" }), now)).toBe(false);
    expect(readingIsStale(spend({ readAt: "2026-10-21T15:10:00Z" }), now)).toBe(true);
    expect(readingIsStale(spend({ readAt: null }), now)).toBe(false);
    expect(creditHealth(spend({ readAt: "2026-10-21T15:10:00Z" }), now)).toBe("attention");
    expect(creditHealth(spend({ readAt: "2026-10-23T14:10:00Z" }), now)).toBe("ok");
  });

  it("puts the reading's age in the tile note", () => {
    const readAt = new Date(Date.now() - 2 * 3_600_000).toISOString();
    const tile = databricksTile(overview([], { spend: spend({ readAt }) }), null);
    expect(tile.note).toMatch(/ used · as of 2 h ago$/);
  });

  it("ends the error line with one period whether or not the server's message has one", () => {
    expect(spendStatusText(spend({ status: "error", message: "The query timed out." }))).toBe(
      "Spend could not be read: The query timed out.",
    );
    expect(spendStatusText(spend({ status: "error", message: "Is the warehouse gone?" }))).toBe(
      "Spend could not be read: Is the warehouse gone?",
    );
    expect(spendStatusText(spend({ status: "error", message: "the query timed out" }))).toBe(
      "Spend could not be read: the query timed out.",
    );
    expect(spendStatusText(spend({ status: "error", message: null }))).toBe(
      "Spend could not be read: the reading failed.",
    );
  });

  it("names the grant when the service principal cannot read billing", () => {
    const message =
      "The service principal needs USE CATALOG on system, USE SCHEMA on system.billing " +
      "and SELECT on system.billing.usage and system.billing.list_prices.";
    expect(spendStatusText(spend({ status: "no_access", message }))).toBe(message);
    expect(spendStatusText(spend({ status: "no_access", message: null }))).toMatch(
      /USE CATALOG.*USE SCHEMA.*SELECT/,
    );
    expect(spendStatusText(spend({ status: "ok" }))).toBeNull();
    // A non-admin has no Refresh, so the line does not mention one.
    expect(spendStatusText(spend({ status: "never" }), false)).toBe("Spend has not been read yet.");
  });

  it("keeps an action's row and a Refresh's spend over a read that began before it", () => {
    const current = overview([warehouse({ state: "STARTING" })], {
      spend: spend({ status: "reading" }),
    });
    const fresh = overview([warehouse({ state: "STOPPED" })], {
      spend: spend({ status: "never" }),
    });
    const kept = freshOverview(fresh, current, 3, new Map([[ID, 4]]), 5);
    expect(kept.warehouses[0].state).toBe("STARTING");
    expect(kept.spend?.status).toBe("reading");
    const taken = freshOverview(fresh, current, 6, new Map([[ID, 4]]), 5);
    expect(taken.warehouses[0].state).toBe("STOPPED");
    expect(taken.spend?.status).toBe("never");
  });

  it("keeps the strip inside a 390 px card and touch targets at 44 px", () => {
    const top = css.indexOf(".dbx-spend-top {");
    expect(css.slice(top, css.indexOf("}", top))).toContain("flex-wrap: wrap");
    const bar = css.indexOf(".dbx-bar-top {");
    expect(css.slice(bar, css.indexOf("}", bar))).toContain("flex-wrap: wrap");
    const asOf = css.indexOf(".dbx-asof {");
    expect(css.slice(asOf, css.indexOf("}", asOf))).toContain("overflow-wrap: anywhere");
    // The rows and the strip are Fabric cards, so Fabric's 44 px rule covers them.
    expect(css).toMatch(/\.card-fabric \.btn \{\s*min-height: 44px;\s*min-width: 44px;\s*\}/);
    expect(css).toMatch(/\.dbx-host \.btn\.btn-icon \{\s*width: 44px;\s*min-height: 44px;\s*\}/);
  });
});

describe("the section on the page", () => {
  it("puts the warehouses in the Compute card and the spend in the credit card", async () => {
    const { context } = stubContext({
      "/v1/ae/fabric": [ok({ configured: false, capacities: [], note: "No Fabric." })],
      [PATH]: [ok(overview([warehouse()]))],
    });
    await activateFabric(context);
    await flush();
    // Since the dashboard redesign (2026-10-20): under Fabric's rows in Compute.
    const compute = document.querySelector('.admin-card[data-card="compute"]');
    expect(cardOf(ID).closest(".admin-card")).toBe(compute);
    expect(compute?.querySelector(".admin-databricks")).not.toBeNull();
    // The workspace link sits under the Databricks heading, not in the Compute header.
    expect(compute?.querySelector(".admin-databricks .dbx-head .dbx-host button")).not.toBeNull();
    expect(compute?.querySelector(".admin-card-top .dbx-host")).toBeNull();
    const credit = document.querySelector('.admin-card[data-card="credit"]');
    expect(document.getElementById("admin-credit-h")?.textContent).toBe("Databricks credit");
    expect(credit?.querySelector(".dbx-spend")).not.toBeNull();
    expect(credit?.querySelector(".ring")?.getAttribute("aria-label")).toMatch(/credit left$/);
  });

  it("shows the server's how-to sentence when Databricks is not configured", async () => {
    const note =
      "Databricks is not configured: put databricks-mvp-client-id and " +
      "databricks-mvp-client-secret in Key Vault and run deploy/pull-secrets.sh.";
    const { context } = stubContext({
      [PATH]: [ok(overview([], { configured: false, note, host: null, spend: null }))],
    });
    const section = await mount(context);
    expect(section.querySelector(".empty")?.textContent).toBe(note);
    expect(section.querySelectorAll("article")).toHaveLength(0);
    expect(section.querySelector(".dbx-spend")).toBeNull();
    expect(section.querySelector(".dbx-host button")).toBeNull();
  });

  it("shows a non-admin the warehouses and the link, never Start, Stop or Refresh", async () => {
    const stopped = "fedcba9876543210";
    const { context } = stubContext({
      [PATH]: [
        ok(
          overview(
            [warehouse(), warehouse({ id: stopped, name: "Reporting", state: "STOPPED", url: "" })],
            { can_act: false },
          ),
        ),
      ],
    });
    await mount(context);
    expect(buttonNames(cardOf(ID))).toEqual(["Open Serverless Starter Warehouse in Databricks"]);
    expect(buttonNames(cardOf(stopped))).toEqual([]);
    expect(document.querySelector(".dbx-spend")).not.toBeNull();
    expect(document.querySelector(".dbx-spend button")).toBeNull();
  });

  it("draws each warehouse with its state, meta line and only the button its state allows", async () => {
    const stopped = "fedcba9876543210";
    const starting = "00000000000000aa";
    const { context } = stubContext({
      [PATH]: [
        ok(
          overview([
            warehouse(),
            warehouse({ id: stopped, name: "Reporting", state: "STOPPED", url: `${HOST}/x` }),
            warehouse({ id: starting, name: "Loads", state: "STARTING", url: "" }),
          ]),
        ),
      ],
    });
    await mount(context);
    const running = cardOf(ID);
    expect(running.classList.contains("fabric-on")).toBe(true);
    expect(running.querySelector(".fabric-state")?.textContent).toBe("Running");
    expect(running.querySelector(".fabric-dot")?.getAttribute("aria-hidden")).toBe("true");
    expect(
      [...running.querySelectorAll(".fabric-meta-item")].map((node) => node.textContent),
    ).toEqual(["2X-Small", "auto stop 5 min", "serverless"]);
    expect(buttonNames(running)).toEqual([
      "Open Serverless Starter Warehouse in Databricks",
      "Stop Serverless Starter Warehouse",
    ]);
    expect(buttonNames(cardOf(stopped))).toEqual(["Open Reporting in Databricks", "Start"]);
    expect(cardOf(stopped).classList.contains("fabric-off")).toBe(true);
    expect(buttonNames(cardOf(starting))).toEqual([]);
    expect(cardOf(starting).classList.contains("fabric-busy")).toBe(true);
    expect(cardOf(starting).querySelector(".fabric-meta")?.textContent).toBe(
      "Starting. Checking every 15 s.",
    );
    // Tabler icons only (svg), no emoji anywhere.
    expect(document.body.textContent).not.toMatch(/\p{Extended_Pictographic}/u);
    const refreshed = document.querySelector<HTMLElement>(".admin-databricks .refreshed");
    expect(refreshed?.textContent).toMatch(/^Updated /);
    expect(refreshed?.title).toBe("Again in 15 s or on focus.");
  });

  it("asks once before starting, Cancel sends nothing, and Start now posts start", async () => {
    const stoppedRow = warehouse({ state: "STOPPED" });
    const { context, request } = stubContext({
      [PATH]: [ok(overview([stoppedRow]))],
      [`${PATH}/warehouses/${ID}/start`]: [ok(warehouse({ state: "STARTING" }))],
    });
    await mount(context);
    const start = buttonNamed(cardOf(ID), "Start");
    expect(start.getAttribute("aria-expanded")).toBe("false");
    start.click();
    await flush();
    const panel = cardOf(ID).querySelector(".fabric-confirm");
    expect(panel?.getAttribute("aria-label")).toBe("Confirm starting Serverless Starter Warehouse");
    expect(panel?.querySelector(".fabric-confirm-text")?.textContent).toBe(
      "Start Serverless Starter Warehouse? It bills until it auto stops after 5 min idle.",
    );
    buttonNamed(cardOf(ID), "Cancel").click();
    await flush();
    expect(cardOf(ID).querySelector(".fabric-confirm")).toBeNull();
    expect(posts(request)).toEqual([]);
    buttonNamed(cardOf(ID), "Start").click();
    await flush();
    buttonNamed(cardOf(ID), "Start now").click();
    await flush();
    expect(posts(request)).toEqual([`${PATH}/warehouses/${ID}/start`]);
    expect(cardOf(ID).querySelector(".fabric-state")?.textContent).toBe("Starting");
    expect(cardOf(ID).querySelector(".fabric-confirm")).toBeNull();
  });

  it("asks before stopping and posts stop; a failure stays on the card", async () => {
    const { context, request } = stubContext({
      [PATH]: [ok(overview([warehouse()]))],
      [`${PATH}/warehouses/${ID}/stop`]: [
        {
          ok: false,
          status: 502,
          body: { error: { code: "upstream", message: "Databricks answered 503." } },
        },
      ],
    });
    await mount(context);
    buttonNamed(cardOf(ID), "Stop Serverless Starter Warehouse").click();
    await flush();
    expect(cardOf(ID).querySelector(".fabric-confirm-text")?.textContent).toBe(
      "Stop Serverless Starter Warehouse? A query still running on it is cancelled.",
    );
    buttonNamed(cardOf(ID), "Stop now").click();
    await flush();
    expect(posts(request)).toEqual([`${PATH}/warehouses/${ID}/stop`]);
    expect(cardOf(ID).querySelector(".notice-error")?.textContent).toBe(
      "Stop failed: Databricks answered 503.",
    );
    // Still running: Stop is offered again.
    expect(buttonNames(cardOf(ID))).toContain("Stop Serverless Starter Warehouse");
  });

  it("shows a link the host would not open as text to copy", async () => {
    const { context, openExternal } = stubContext({ [PATH]: [ok(overview([warehouse()]))] }, true);
    await mount(context);
    buttonNamed(cardOf(ID), "Open Serverless Starter Warehouse in Databricks").click();
    await flush();
    expect(openExternal).toHaveBeenCalledWith(`${HOST}/sql/warehouses/${ID}`);
    expect(cardOf(ID).querySelector(".link-copy")?.textContent).toBe(
      `${HOST}/sql/warehouses/${ID}`,
    );
    buttonNamed(document, "Open the workspace in Databricks").click();
    await flush();
    expect(openExternal).toHaveBeenLastCalledWith(HOST);
    expect(document.querySelector(".admin-databricks .page-status .link-copy")?.textContent).toBe(
      HOST,
    );
  });

  it("says the listing failed and still shows spend", async () => {
    const { context } = stubContext({
      [PATH]: [
        ok(
          overview([], {
            error: { code: "timeout", message: "Databricks did not answer in 10 s." },
          }),
        ),
      ],
    });
    const section = await mount(context);
    expect(section.querySelector(".page-status .notice-error")?.textContent).toBe(
      "Warehouses could not be read: Databricks did not answer in 10 s.",
    );
    expect(section.querySelector(".empty")).toBeNull();
    expect(section.querySelector(".dbx-spend")).not.toBeNull();
  });
});

describe("the spend strip", () => {
  it("shows both bars, the whole credit first when granted once, and how old the data is", async () => {
    const { context } = stubContext({ [PATH]: [ok(overview([warehouse()]))] });
    const section = await mount(context);
    const bars = [...section.querySelectorAll<HTMLElement>(".dbx-bar")];
    expect(bars.map((bar) => bar.dataset.bar)).toEqual(["total", "month"]);
    expect(bars[0].classList.contains("dbx-bar-head")).toBe(true);
    expect(bars[1].classList.contains("dbx-bar-head")).toBe(false);
    expect(bars[0].querySelector(".dbx-bar-label")?.textContent).toBe(
      `Credit since ${dayText("2030-01-01")}`,
    );
    expect(bars[0].querySelector(".dbx-bar-value")?.textContent).toBe("$61.25 of $500");
    expect(bars[1].querySelector(".dbx-bar-label")?.textContent).toBe("This month");
    expect(bars[1].querySelector(".dbx-bar-value")?.textContent).toBe("$12.40 of $500");
    const meter = bars[1].querySelector(".dbx-track");
    expect(meter?.getAttribute("role")).toBe("meter");
    expect(meter?.getAttribute("aria-valuenow")).toBe("12.4");
    expect(meter?.getAttribute("aria-valuemax")).toBe("500");
    expect(bars[1].querySelector<HTMLElement>(".dbx-fill")?.style.width).toBe("2.48%");
    expect(section.querySelector(".dbx-asof")?.textContent).toMatch(
      /^as of .+ \(billing data lags a few hours\)$/,
    );
    const refresh = buttonNamed(section, REFRESH_LABEL);
    expect(refresh.querySelector("svg")).not.toBeNull();
    expect(refresh.disabled).toBe(false);
  });

  it("draws no credit when the server has none: this month's spend only, no error", async () => {
    const none = spend({ creditUsd: null, creditStart: null, creditEnd: null });
    const data = overview([warehouse()], { spend: none });
    const { context } = stubContext({ [PATH]: [ok(data)] });
    const section = await mount(context);
    expect(section.querySelector(".dbx-bar")).toBeNull();
    expect(section.querySelector(".ring")).toBeNull();
    expect(section.textContent).toContain("This month: $12.40");
    expect(section.textContent).not.toContain("credit");
    expect(section.querySelector(".dbx-spend")?.getAttribute("title")).toBeNull();
    expect(databricksTile(data, null).note).toMatch(/^\$12\.40 this month/);
    const folded = creditTile(data);
    expect(folded.value).toBe("");
    expect(folded.health).toBe("idle");
  });

  it("leads with this month when the credit is monthly, and marks a bar past the credit", async () => {
    const { context } = stubContext({
      [PATH]: [
        ok(overview([warehouse()], { spend: spend({ cadence: "monthly", totalUsd: 612.5 }) })),
      ],
    });
    const section = await mount(context);
    const bars = [...section.querySelectorAll<HTMLElement>(".dbx-bar")];
    expect(bars.map((bar) => bar.dataset.bar)).toEqual(["month", "total"]);
    expect(bars[0].classList.contains("dbx-bar-head")).toBe(true);
    expect(bars[1].classList.contains("dbx-bar-over")).toBe(true);
    expect(bars[1].querySelector(".dbx-bar-value")?.textContent).toBe("$612.50 of $500");
    expect(bars[1].querySelector<HTMLElement>(".dbx-fill")?.style.width).toBe("100%");
  });

  it("names the missing grant when the service principal cannot read billing", async () => {
    const message =
      "The service principal needs USE CATALOG on system, USE SCHEMA on system.billing " +
      "and SELECT on system.billing.usage and system.billing.list_prices.";
    const { context } = stubContext({
      [PATH]: [
        ok(
          overview([warehouse()], {
            spend: spend({
              status: "no_access",
              message,
              monthUsd: null,
              totalUsd: null,
              asOf: null,
              readAt: null,
            }),
          }),
        ),
      ],
    });
    const section = await mount(context);
    expect(section.querySelector(".dbx-spend .notice-info")?.textContent).toBe(message);
    expect(section.querySelector(".dbx-bar")).toBeNull();
    expect(section.querySelector(".dbx-asof")).toBeNull();
  });

  it("says spend has not been read yet and that Refresh wakes the warehouse", async () => {
    const { context } = stubContext({
      [PATH]: [
        ok(
          overview([warehouse({ state: "STOPPED" })], {
            spend: spend({
              status: "never",
              monthUsd: null,
              totalUsd: null,
              asOf: null,
              readAt: null,
            }),
          }),
        ),
      ],
    });
    const section = await mount(context);
    expect(section.querySelector(".dbx-spend .dbx-asof")?.textContent).toBe(
      "Spend has not been read yet. Refresh wakes the warehouse to read it.",
    );
    expect(section.querySelector(".dbx-bar")).toBeNull();
    expect(buttonNamed(section, REFRESH_LABEL).disabled).toBe(false);
  });

  it("shows a quiet reading line, keeps the last numbers and holds Refresh", async () => {
    const { context } = stubContext({
      [PATH]: [ok(overview([warehouse()], { spend: spend({ status: "reading" }) }))],
    });
    const section = await mount(context);
    const reading = section.querySelector(".dbx-reading");
    expect(reading?.textContent).toBe("Reading spend...");
    expect(reading?.classList.contains("notice")).toBe(false);
    expect(section.querySelectorAll(".dbx-bar")).toHaveLength(2);
    expect(buttonNamed(section, REFRESH_LABEL).disabled).toBe(true);
    expect(section.querySelector<HTMLElement>(".refreshed")?.title).toBe(
      "Again in 15 s or on focus.",
    );
  });

  it("posts a reading only on Refresh, then reads again in 15 s", async () => {
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
    const never = spend({ status: "never", monthUsd: null, totalUsd: null, asOf: null });
    const { context, request } = stubContext({
      [PATH]: [
        ok(overview([warehouse()], { spend: never })),
        ok(overview([warehouse()], { spend: spend({ status: "reading" }) })),
      ],
      [REFRESH_PATH]: [ok(spend({ status: "reading" }))],
    });
    const node = frame();
    document.getElementById("root")?.appendChild(node);
    activateDatabricks(context, node);
    await vi.advanceTimersByTimeAsync(0);
    buttonNamed(node, REFRESH_LABEL).click();
    await vi.advanceTimersByTimeAsync(0);
    expect(posts(request)).toEqual([REFRESH_PATH]);
    expect(node.querySelector(".dbx-reading")?.textContent).toBe("Reading spend...");
    const reads = () => request.mock.calls.filter(([call]) => call.path === PATH).length;
    expect(reads()).toBe(1);
    await vi.advanceTimersByTimeAsync(POLL_BUSY_MS);
    expect(reads()).toBe(2);
  });

  it("never posts a reading from a GET, a poll or a focus, even when spend was never read", async () => {
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
    const never = () =>
      ok(
        overview([warehouse()], {
          spend: spend({ status: "never", monthUsd: null, totalUsd: null, asOf: null }),
        }),
      );
    const { context, request } = stubContext({
      [PATH]: [never(), never(), never(), never(), never()],
    });
    const node = frame();
    document.getElementById("root")?.appendChild(node);
    activateDatabricks(context, node);
    await vi.advanceTimersByTimeAsync(0);
    await vi.advanceTimersByTimeAsync(POLL_IDLE_MS * 2 + 1000);
    window.dispatchEvent(new Event("focus"));
    await vi.advanceTimersByTimeAsync(0);
    const calls = request.mock.calls.map(([call]) => call as { method: string; path: string });
    expect(calls.length).toBeGreaterThanOrEqual(4);
    expect(calls.every((call) => call.method === "GET" && call.path === PATH)).toBe(true);
  });

  it("skips the scheduled read while the tab is hidden and reads on return", async () => {
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
    const { context, request } = stubContext({
      [PATH]: [
        ok(overview([warehouse()])),
        ok(overview([warehouse()])),
        ok(overview([warehouse()])),
      ],
    });
    const node = frame();
    document.getElementById("root")?.appendChild(node);
    activateDatabricks(context, node);
    await vi.advanceTimersByTimeAsync(0);
    expect(request).toHaveBeenCalledTimes(1);
    setVisibility("hidden");
    await vi.advanceTimersByTimeAsync(POLL_IDLE_MS + 1000);
    expect(request).toHaveBeenCalledTimes(1);
    setVisibility("visible");
    document.dispatchEvent(new Event("visibilitychange"));
    await vi.advanceTimersByTimeAsync(0);
    expect(request).toHaveBeenCalledTimes(2);
  });
});

describe("a second activation (review fix, 2026-10-19)", () => {
  it("leaves a read still out from the first one unable to draw or touch the timer", async () => {
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
    const waiting: Array<(reply: Reply) => void> = [];
    const request = vi.fn(
      ({ path }: { method: string; path: string }) =>
        new Promise<Reply>((resolve) => {
          if (path !== PATH) throw new Error(`unexpected ${path}`);
          waiting.push(resolve);
        }),
    );
    const context = {
      view: "fabric",
      server: { request },
      navigation: { openExternal: vi.fn() },
    } as unknown as ExtensionContext;
    const first = frame();
    activateDatabricks(context, first);
    await vi.advanceTimersByTimeAsync(0);
    expect(waiting).toHaveLength(1);
    deactivateDatabricks();
    const second = frame();
    document.getElementById("root")?.appendChild(second);
    activateDatabricks(context, second);
    await vi.advanceTimersByTimeAsync(0);
    expect(waiting).toHaveLength(2);
    // The second activation's read lands first and schedules its 60 s poll.
    waiting[1](ok(overview([warehouse({ name: "Second" })])));
    await vi.advanceTimersByTimeAsync(0);
    expect(second.querySelector(".fabric-name")?.textContent).toBe("Second");
    // The first activation's read lands late: nothing drawn, timer untouched.
    waiting[0](ok(overview([warehouse({ name: "First" })])));
    await vi.advanceTimersByTimeAsync(0);
    expect(first.querySelector("article")).toBeNull();
    expect(first.querySelector(".refreshed")?.textContent).toBe("");
    expect(second.querySelector(".fabric-name")?.textContent).toBe("Second");
    // The second activation's poll still fires, and draws into the second section.
    await vi.advanceTimersByTimeAsync(POLL_IDLE_MS);
    expect(waiting).toHaveLength(3);
    waiting[2](ok(overview([warehouse({ name: "Second again" })])));
    await vi.advanceTimersByTimeAsync(0);
    expect(second.querySelector(".fabric-name")?.textContent).toBe("Second again");
    expect(first.querySelector("article")).toBeNull();
  });
});

describe("the spend breakdown (2026-10-22)", () => {
  const days = (usd: number[]) =>
    usd.map((value, i) => ({ date: `2026-10-${String(i + 1).padStart(2, "0")}`, usd: value }));
  const products = [
    { product: "SQL", usd: 6.25 },
    { product: "JOBS", usd: 3 },
    { product: "INTERACTIVE", usd: 2 },
    { product: "DLT", usd: 1.5 },
    { product: "MODEL_SERVING", usd: 0.75 },
    { product: "DEFAULT_STORAGE", usd: 0.5 },
    { product: "APPS", usd: 0 },
  ];

  it("names products in plain words, title casing a code it does not know", () => {
    expect(productName("SQL")).toBe("SQL");
    expect(productName("JOBS")).toBe("Jobs");
    expect(productName("INTERACTIVE")).toBe("Notebooks");
    expect(productName("NOTEBOOKS")).toBe("Notebooks");
    expect(productName("DLT")).toBe("Lakeflow pipelines");
    expect(productName("MODEL_SERVING")).toBe("Model serving");
    expect(productName("DEFAULT_STORAGE")).toBe("Default Storage");
    expect(productName("OTHER")).toBe("Other");
  });

  it("keeps the five biggest products with spend and only days with some spend", () => {
    const top = topProducts(spend({ products }));
    expect(top.map((row) => row.name)).toEqual([
      "SQL",
      "Jobs",
      "Notebooks",
      "Lakeflow pipelines",
      "Model serving",
    ]);
    expect(topProducts(spend())).toEqual([]);
    expect(
      topProducts(
        spend({
          products: [
            { product: "INTERACTIVE", usd: 4 },
            { product: "SQL", usd: 5 },
            { product: "NOTEBOOKS", usd: 2 },
          ],
        }),
      ),
    ).toEqual([
      { name: "Notebooks", usd: 6 },
      { name: "SQL", usd: 5 },
    ]);
    expect(spendDays(spend({ days: days([-3, 1.5]) }))?.map((day) => day.usd)).toEqual([0, 1.5]);
    expect(spendDays(spend({ days: days(Array(21).fill(0)) }))).toBeNull();
    expect(spendDays(spend({ days: days([0, 1.5]) }))?.length).toBe(2);
    expect(spendDays(spend())).toBeNull();
  });

  it("draws the sparkline and the top five under the ring", async () => {
    const daily = days([...Array(20).fill(0.5), 3]);
    const { context } = stubContext({
      [PATH]: [ok(overview([warehouse()], { spend: spend({ products, days: daily }) }))],
    });
    const section = await mount(context);
    const block = section.querySelector(".dbx-spend .dbx-breakdown");
    expect(block).not.toBeNull();
    // Under the ring and the bars, not beside them.
    expect(block?.previousElementSibling?.classList.contains("dbx-spend-main")).toBe(true);
    const spark = block?.querySelector("svg.azure-spark");
    expect(spark?.getAttribute("aria-label")).toBe("Daily spend for the last 21 days, highest $3");
    const points = spark?.querySelector("polyline")?.getAttribute("points")?.split(" ") ?? [];
    expect(points).toHaveLength(21);
    expect(points[20]).toBe("300.0,4.0");
    expect(block?.querySelector(".azure-caption")?.textContent).toBe(
      "Daily spend, last 21 days. Billing data lags a few hours.",
    );
    const rows = [...(block?.querySelectorAll(".dbx-products li") ?? [])].map((li) =>
      [...li.children].map((child) => child.textContent),
    );
    expect(rows).toEqual([
      ["SQL", "$6.25"],
      ["Jobs", "$3"],
      ["Notebooks", "$2"],
      ["Lakeflow pipelines", "$1.50"],
      ["Model serving", "$0.75"],
    ]);
    expect(section.querySelector(".dbx-note")).toBeNull();
  });

  it("draws nothing new with no spend in the rows, nor from an older server", async () => {
    for (const extra of [
      { products: [], days: days(Array(21).fill(0)), monthUsd: 0, totalUsd: 0 },
      {},
      { products: null, days: null, note: null },
    ]) {
      const { context } = stubContext({
        [PATH]: [ok(overview([warehouse()], { spend: spend(extra) }))],
      });
      const section = await mount(context);
      expect(section.querySelector(".dbx-spend-main")).not.toBeNull();
      expect(section.querySelector(".dbx-breakdown")).toBeNull();
      expect(section.querySelector(".azure-spark")).toBeNull();
      expect(section.querySelector(".dbx-note")).toBeNull();
      deactivateDatabricks();
      section.remove();
    }
  });

  it("says why when the breakdown failed, and keeps the totals", async () => {
    const note =
      "Breakdown unavailable: The spend breakdown query failed. The totals are complete.";
    const { context } = stubContext({
      [PATH]: [ok(overview([warehouse()], { spend: spend({ note }) }))],
    });
    const section = await mount(context);
    expect(section.querySelector(".dbx-note")?.textContent).toBe(note);
    expect(section.querySelector(".notice-error")).toBeNull();
    expect(section.querySelectorAll(".dbx-bar")).toHaveLength(2);
    expect(section.querySelector(".dbx-breakdown")).toBeNull();
  });
});
