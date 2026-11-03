// The Spend card (2026-10-30): the Presets card with the Family cost card as its Families tab.
// The tab bar's ARIA and keys, the remembered tab, the one 7 d / 30 d toggle for both panels, the
// warning pill on the Families tab, and one info icon holding every footnote. Every call is
// mocked: GET /v1/ae/presets, GET /v1/ae/family-cost and the side reads the Presets panel makes.
import type { ExtensionContext } from "@omnigent/extension-sdk";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { FamilyCostAnswer, PresetsAnswer } from "../src/api";
import {
  COST_NOTE as FAMILY_COST_NOTE,
  deactivateFamilyCost,
  TODAY_NOTE,
  TOKENS_NOTE as FAMILY_TOKENS_NOTE,
} from "../src/familyCost";
import {
  activatePresets,
  COST_NOTE,
  deactivatePresets,
  footnote,
  pillText,
  presetsSection,
  TAB_KEY,
  TOKENS_NOTE,
} from "../src/presets";
import { activateFamilyCost } from "../src/familyCost";
import { DAYS_KEY, notesText, resetSpend } from "../src/spend";
import { resetTiles } from "../src/tiles";

const sdk = vi.hoisted(() => ({ defineExtension: vi.fn() }));
vi.mock("@omnigent/extension-sdk", () => sdk);

const NOW = Date.parse("2026-10-30T15:00:00Z");
const ok = (body: unknown) => ({ ok: true, status: 200, body });
const missing = { ok: false, status: 404, body: { error: { code: "x", message: "none" } } };

function presetsAnswer(days: number): PresetsAnswer {
  return {
    state: "ready",
    mode: "runs",
    days,
    show: "both",
    asOf: "2026-10-30T14:50:00Z",
    kpis: {
      runs: 4,
      cost_usd: 10,
      tokens_total: 1000,
      median_cost_usd: 1,
      median_seconds: 60,
      cost_change_pct: null,
      time_change_pct: null,
      tokens_change_pct: null,
    },
    takeaways: [],
    presets: [
      {
        id: "deep",
        name: "Deep debug",
        color: "amber",
        runs: 4,
        cost_usd: 10,
        tokens_total: 1000,
        median_cost_usd: 1,
        median_seconds: 60,
        models: [],
        daily: [],
      },
    ],
  } as unknown as PresetsAnswer;
}

function familyAnswer(days: number, over: number): FamilyCostAnswer {
  const row = (i: number, usd: number) => ({
    lead_id: `lead-${i}`,
    title: `Family ${i}`,
    host: "box",
    owner: "a@x.test",
    status: "working",
    cost_usd: usd,
    tokens: { input: 1, output: 1, cache_read: 1, total: 100 },
    today: { cost_usd: usd, tokens_total: 100 },
    by_role: [],
    sessions: 1,
    lead_context_tokens: null,
    restarts: 0,
    flagged: false,
  });
  const families = Array.from({ length: 3 }, (_, i) => row(i, i < over ? 50 : 1));
  return {
    state: "ready",
    days,
    unit_default: "tokens",
    settings: { unit: "tokens", runaway_tokens: 5_000_000, runaway_usd: 25 },
    computed_at: "2026-10-30T14:50:00Z",
    history_since: null,
    partial: false,
    families,
    totals: { families: 3, cost_usd: 100, tokens_total: 300, flagged: over },
    truncated: false,
  } as unknown as FamilyCostAnswer;
}

function stub(over = 3) {
  const reads: string[] = [];
  const request = vi.fn(async ({ method, path }: { method: string; path: string }) => {
    reads.push(path);
    const days = Number(/days=(\d+)/.exec(path)?.[1] ?? 0);
    if (method === "GET" && path.startsWith("/v1/ae/presets?")) return ok(presetsAnswer(days));
    if (method === "GET" && path.startsWith("/v1/ae/family-cost?")) return ok(familyAnswer(days, over));
    return missing;
  });
  const context = { view: "fabric", server: { request }, navigation: { openSession: vi.fn() } };
  return { context: context as unknown as ExtensionContext, reads };
}

const flush = () => vi.advanceTimersByTimeAsync(0);

async function mount(context: ExtensionContext): Promise<HTMLElement> {
  const node = presetsSection();
  document.getElementById("root")?.appendChild(node);
  activatePresets(context, node);
  activateFamilyCost(context, node);
  await flush();
  return node;
}

const tabs = (node: HTMLElement) => [...node.querySelectorAll<HTMLButtonElement>('[role="tab"]')];
const panels = (node: HTMLElement) => [...node.querySelectorAll<HTMLElement>('[role="tabpanel"]')];
const key = (target: HTMLElement, name: string) =>
  target.dispatchEvent(new KeyboardEvent("keydown", { key: name, bubbles: true, cancelable: true }));

beforeEach(() => {
  document.body.replaceChildren();
  const root = document.createElement("div");
  root.id = "root";
  document.body.appendChild(root);
  vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout", "Date"] });
  vi.setSystemTime(NOW);
  window.localStorage.clear();
  resetSpend();
  resetTiles();
});

afterEach(() => {
  deactivatePresets();
  deactivateFamilyCost();
  resetSpend();
  resetTiles();
  vi.useRealTimers();
  vi.restoreAllMocks();
});

describe("the tab bar", () => {
  it("follows the ARIA tabs pattern", async () => {
    const node = await mount(stub().context);
    const list = node.querySelector('[role="tablist"]')!;
    expect(list.getAttribute("aria-label")).toBe("Spend");
    const [presets, families] = tabs(node);
    expect(tabs(node).map((t) => t.querySelector(".spend-tab-label")?.textContent)).toEqual([
      "Presets",
      "Families",
    ]);
    expect(presets!.getAttribute("aria-selected")).toBe("true");
    expect(families!.getAttribute("aria-selected")).toBe("false");
    // Roving tabindex: only the chosen tab is in the tab order.
    expect(presets!.tabIndex).toBe(0);
    expect(families!.tabIndex).toBe(-1);
    const [first, second] = panels(node);
    expect(presets!.getAttribute("aria-controls")).toBe(first!.id);
    expect(families!.getAttribute("aria-controls")).toBe(second!.id);
    expect(first!.getAttribute("aria-labelledby")).toBe(presets!.id);
    expect(second!.getAttribute("aria-labelledby")).toBe(families!.id);
    expect(first!.hidden).toBe(false);
    expect(second!.hidden).toBe(true);
    // A panel takes focus even when nothing in it can.
    expect(first!.getAttribute("tabindex")).toBe("0");
    expect(second!.getAttribute("tabindex")).toBe("0");
  });

  it("switches on click and remembers the tab", async () => {
    const node = await mount(stub().context);
    tabs(node)[1]!.click();
    expect(tabs(node)[1]!.getAttribute("aria-selected")).toBe("true");
    expect(panels(node)[0]!.hidden).toBe(true);
    expect(panels(node)[1]!.hidden).toBe(false);
    expect(window.localStorage.getItem(TAB_KEY)).toBe("families");
    expect(presetsSection().querySelector('[role="tab"][aria-selected="true"]')?.textContent).toContain(
      "Families",
    );
  });

  it("moves with Left, Right, Home and End, wrapping at the ends", async () => {
    const node = await mount(stub().context);
    const [presets, families] = tabs(node);
    presets!.focus();
    key(presets!, "ArrowRight");
    expect(document.activeElement).toBe(families);
    expect(families!.getAttribute("aria-selected")).toBe("true");
    expect(panels(node)[1]!.hidden).toBe(false);
    key(families!, "ArrowRight");
    expect(document.activeElement).toBe(presets);
    key(presets!, "ArrowLeft");
    expect(document.activeElement).toBe(families);
    key(families!, "Home");
    expect(document.activeElement).toBe(presets);
    key(presets!, "End");
    expect(document.activeElement).toBe(families);
    expect(window.localStorage.getItem(TAB_KEY)).toBe("families");
  });
});

describe("the warning pill", () => {
  it("tells the Presets tab how many families are over today", async () => {
    const node = await mount(stub(2).context);
    const pill = node.querySelector<HTMLElement>(".spend-tab-pill")!;
    expect(pill.hidden).toBe(false);
    expect(pill.textContent).toBe("2 over today");
    expect(pill.closest('[role="tab"]')).toBe(tabs(node)[1]);
    expect(pillText(0)).toBe("");
  });

  it("stays hidden when no family is over", async () => {
    const node = await mount(stub(0).context);
    expect(node.querySelector<HTMLElement>(".spend-tab-pill")!.hidden).toBe(true);
  });
});

describe("a deactivated page", () => {
  it("leaves no pill or footnote behind for the next one", async () => {
    const first = await mount(stub(2).context);
    expect(first.querySelector<HTMLElement>(".spend-tab-pill")!.hidden).toBe(false);
    deactivatePresets();
    deactivateFamilyCost();
    expect(notesText()).toBe("");
    // A page that mounts again, before any answer, starts clean.
    document.body.replaceChildren();
    const root = document.createElement("div");
    root.id = "root";
    document.body.appendChild(root);
    const again = presetsSection();
    root.appendChild(again);
    activatePresets({ view: "fabric", server: { request: () => new Promise(() => {}) } } as never, again);
    expect(again.querySelector<HTMLElement>(".spend-tab-pill")!.hidden).toBe(true);
    expect(again.querySelector(".spend-info [data-hint]")).toBeNull();
  });
});

describe("the one window", () => {
  it("changes both panels, and the Families panel has no toggle of its own", async () => {
    window.localStorage.setItem(DAYS_KEY, "30");
    const { context, reads } = stub();
    const node = await mount(context);
    expect(reads.filter((p) => p.includes("days=30")).sort()).toEqual([
      "/v1/ae/family-cost?days=30",
      "/v1/ae/presets?days=30",
    ]);
    expect(node.querySelectorAll('[aria-label="Window"]')).toHaveLength(1);
    const seven = [...node.querySelectorAll<HTMLButtonElement>(".presets-seg")].find(
      (b) => b.textContent === "7 d",
    );
    seven!.click();
    await flush();
    expect(reads).toContain("/v1/ae/presets?days=7");
    expect(reads).toContain("/v1/ae/family-cost?days=7");
    expect(window.localStorage.getItem(DAYS_KEY)).toBe("7");
    expect(window.localStorage.getItem("familycost.days")).toBeNull();
  });
});

describe("the info icon", () => {
  it("holds the footnotes of both panels, once each", async () => {
    const node = await mount(stub().context);
    const text = node.querySelector(".spend-info [data-hint]")?.getAttribute("data-hint") ?? "";
    expect(text.split("\n")).toEqual([
      footnote("both"),
      FAMILY_TOKENS_NOTE,
      FAMILY_COST_NOTE,
      TODAY_NOTE,
    ]);
    expect(text).toContain(COST_NOTE);
    expect(text).toContain(TOKENS_NOTE);
    expect(notesText()).toBe(text);
    // No footnote is drawn as text in either panel.
    expect(node.querySelector(".presets-foot")).toBeNull();
    expect(node.querySelector(".family-foot")).toBeNull();
    expect(node.querySelectorAll(".spend-info [data-hint]")).toHaveLength(1);
  });
});
