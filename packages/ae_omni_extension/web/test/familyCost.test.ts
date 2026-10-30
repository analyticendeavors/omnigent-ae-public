// The Admin Center's Family cost card (2026-10-29): ranking, the unit setting with the other
// unit on hover, the 7 d / 30 d toggle, the Needs you line and the warming and empty states.
// Every call is mocked: GET /v1/ae/family-cost?days=N and POST /v1/ae/family-cost/settings.
import type { ExtensionContext } from "@omnigent/extension-sdk";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { FamilyCostAnswer, FamilyRow } from "../src/api";
import {
  activateFamilyCost,
  amountText,
  DAYS_KEY,
  deactivateFamilyCost,
  familyCostSection,
  familyTile,
  fromInputValue,
  hoverText,
  needsYou,
  POLL_MS,
  rankFamilies,
  RUNAWAY_HINT,
  settingsOf,
  sharePct,
  STALE_MS,
  TODAY_NOTE,
  tokensText,
  toInputValue,
  UNIT_HINT,
  updatedText,
  WARMING_POLL_MS,
} from "../src/familyCost";
import { HINT_MAX } from "../src/hint";
import { getTile, resetTiles } from "../src/tiles";

const sdk = vi.hoisted(() => ({ defineExtension: vi.fn() }));
vi.mock("@omnigent/extension-sdk", () => sdk);

type Reply = { ok: boolean; status: number; body: unknown };
const NOW = Date.parse("2026-10-29T15:00:00Z");

function family(overrides: Partial<FamilyRow> = {}): FamilyRow {
  return {
    lead_id: "lead-a",
    title: "Session token audit",
    host: "box",
    owner: "a@x.test",
    status: "working",
    cost_usd: 18.5,
    tokens: { input: 3_000_000, output: 300_000, cache_read: 89_000_000, total: 92_300_000 },
    today: { cost_usd: 2, tokens_total: 4_000_000 },
    by_role: [
      { role: "reviewer", sessions: 2, cost_usd: 3, tokens_total: 9_000_000 },
      { role: "lead", sessions: 1, cost_usd: 12, tokens_total: 70_000_000 },
      { role: "codex", sessions: 1, cost_usd: 3.5, tokens_total: 13_300_000 },
    ],
    sessions: 4,
    lead_context_tokens: 250_000,
    restarts: 5,
    flagged: false,
    ...overrides,
  };
}

function small(): FamilyRow {
  return family({
    lead_id: "lead-b",
    title: "Quick fix",
    host: "laptop",
    cost_usd: 40,
    tokens: { input: 10_000, output: 5_000, cache_read: 100_000, total: 115_000 },
    today: { cost_usd: 30, tokens_total: 115_000 },
    by_role: [{ role: "lead", sessions: 1, cost_usd: 40, tokens_total: 115_000 }],
    sessions: 1,
    lead_context_tokens: null,
    restarts: 0,
  });
}

function answer(extra: Partial<FamilyCostAnswer> = {}): FamilyCostAnswer {
  const families = [family(), small()];
  return {
    state: "ready",
    days: 7,
    unit_default: "tokens",
    settings: { unit: "tokens", runaway_tokens: 5_000_000, runaway_usd: 25 },
    computed_at: "2026-10-29T14:50:00Z",
    history_since: "2026-10-23",
    partial: false,
    families,
    totals: { families: 2, cost_usd: 58.5, tokens_total: 92_415_000, flagged: 0 },
    truncated: false,
    ...extra,
  };
}

const ok = (body: unknown): Reply => ({ ok: true, status: 200, body });
const failed = (status: number, code: string, message: string): Reply => ({
  ok: false,
  status,
  body: { error: { code, message } },
});

function stubContext(replies: Reply[], saves: Reply[] = []) {
  const reads: string[] = [];
  const posts: unknown[] = [];
  const request = vi.fn(
    async ({ method, path, body }: { method: string; path: string; body?: unknown }) => {
      if (method === "POST" && path === "/v1/ae/family-cost/settings") {
        posts.push(body);
        return saves.shift() ?? failed(500, "x", "no save queued");
      }
      if (method !== "GET" || !path.startsWith("/v1/ae/family-cost?days=")) {
        return failed(500, "x", `unexpected ${method} ${path}`);
      }
      reads.push(path);
      return replies.shift() ?? failed(500, "x", "no reply queued");
    },
  );
  const context = { view: "fabric", server: { request } } as unknown as ExtensionContext;
  return { context, request, reads, posts };
}

const flush = () => vi.advanceTimersByTimeAsync(0);

async function mount(context: ExtensionContext): Promise<HTMLElement> {
  const node = familyCostSection();
  document.getElementById("root")?.appendChild(node);
  activateFamilyCost(context, node);
  await flush();
  return node;
}

const rows = (node: HTMLElement) => [...node.querySelectorAll<HTMLElement>(".family-row")];
const segs = (node: HTMLElement, group: string) =>
  [...node.querySelectorAll<HTMLElement>(`[aria-label="${group}"] .family-seg`)];

beforeEach(() => {
  document.body.replaceChildren();
  const root = document.createElement("div");
  root.id = "root";
  document.body.appendChild(root);
  vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout", "Date"] });
  vi.setSystemTime(NOW);
  window.localStorage.clear();
  resetTiles();
});

afterEach(() => {
  deactivateFamilyCost();
  resetTiles();
  vi.useRealTimers();
  vi.restoreAllMocks();
});

describe("the words", () => {
  it("writes tokens and the number each unit shows first", () => {
    expect(tokensText(950)).toBe("950");
    expect(tokensText(92_300_000)).toBe("92.3M");
    expect(tokensText(1_500_000_000)).toBe("1.5B");
    expect(amountText("tokens", 18.5, 92_300_000)).toBe("92.3M tokens");
    expect(amountText("dollars", 18.5, 92_300_000)).toBe("$18.50");
  });

  it("puts the other unit on hover, with the breakdown", () => {
    expect(hoverText("tokens", family())).toMatch(/^\$18\.50\. Input 3\.0M \(with cache writes\)/);
    expect(hoverText("dollars", family())).toMatch(/^92\.3M tokens\./);
  });

  it("keeps both hints within the length rule", () => {
    expect(UNIT_HINT.length).toBeLessThanOrEqual(HINT_MAX);
    expect(RUNAWAY_HINT.length).toBeLessThanOrEqual(HINT_MAX);
    expect(UNIT_HINT).toBe("Which number the family cost card shows first; the other appears on hover.");
  });

  it("reads and writes the runaway input in millions of tokens or dollars", () => {
    const settings = { unit: "tokens" as const, runaway_tokens: 5_000_000, runaway_usd: 25 };
    expect(toInputValue(settings, "tokens")).toBe("5");
    expect(toInputValue(settings, "dollars")).toBe("25");
    expect(fromInputValue("2.5", "tokens")).toBe(2_500_000);
    expect(fromInputValue("12.345", "dollars")).toBe(12.35);
    expect(fromInputValue("", "tokens")).toBeNull();
    expect(fromInputValue("-1", "dollars")).toBeNull();
  });
});

describe("ranking", () => {
  it("ranks by the chosen unit and sizes the bar against the biggest", () => {
    const list = [small(), family()];
    expect(rankFamilies(list, "tokens").map((r) => r.lead_id)).toEqual(["lead-a", "lead-b"]);
    expect(rankFamilies(list, "dollars").map((r) => r.lead_id)).toEqual(["lead-b", "lead-a"]);
    expect(sharePct(family(), list, "tokens")).toBe(100);
    expect(sharePct(small(), list, "tokens")).toBe(0);
    expect(sharePct(small(), list, "dollars")).toBe(100);
    expect(sharePct(family(), list, "dollars")).toBe(46);
  });
});

describe("Needs you", () => {
  const settings = { unit: "dollars" as const, runaway_tokens: 5_000_000, runaway_usd: 25 };

  it("names the families whose spend today passed the threshold in the chosen unit", () => {
    expect(needsYou(answer(), settings, "dollars")).toBe(
      "1 family passed $25.00 today: Quick fix.",
    );
    expect(needsYou(answer(), settings, "tokens")).toBeNull();
    expect(needsYou(answer(), { ...settings, runaway_tokens: 1_000_000 }, "tokens")).toBe(
      "1 family passed 1.0M tokens today: Session token audit.",
    );
  });

  it("stays quiet when the flag is off, nothing passed, or the card is warming", () => {
    expect(needsYou(answer(), { ...settings, runaway_usd: 0 }, "dollars")).toBeNull();
    expect(needsYou(answer(), { ...settings, runaway_usd: 500 }, "dollars")).toBeNull();
    expect(needsYou({ state: "warming", days: 7, unit_default: "tokens" }, settings, "dollars")).toBeNull();
  });
});

describe("the tile", () => {
  it("is reading, then ok, then amber for a runaway or an old answer", () => {
    expect(familyTile(null, null, "tokens").value).toBe("Reading...");
    expect(familyTile(null, new Error("x"), "tokens").health).toBe("attention");
    const ready = familyTile(answer(), null, "tokens", 7, NOW);
    expect(ready).toMatchObject({ value: "2 families", health: "ok" });
    expect(ready.note).toBe("top 92.3M tokens in 7 d");
    const hot = answer({ totals: { families: 2, cost_usd: 58.5, tokens_total: 92_415_000, flagged: 1 } });
    expect(familyTile(hot, null, "dollars", 7, NOW)).toMatchObject({ health: "attention" });
    expect(familyTile(hot, null, "dollars", 7, NOW).note).toBe("1 runaway · $58.50 in 7 d");
    expect(familyTile(answer(), null, "tokens", 7, NOW + STALE_MS + 60_000).health).toBe("attention");
  });

  it("says when it was updated by the server's time", () => {
    expect(updatedText(null)).toBe("");
    expect(updatedText(answer(), NOW)).toMatch(/^Updated /);
    expect(updatedText(answer(), NOW + STALE_MS + 60_000)).toMatch(/refresh failing$/);
    expect(settingsOf(null).unit).toBe("tokens");
  });
});

describe("the card", () => {
  it("lists the families biggest first with host chip, role chips, context and restarts", async () => {
    const { context, reads } = stubContext([ok(answer())]);
    const node = await mount(context);
    expect(reads).toEqual(["/v1/ae/family-cost?days=7"]);
    const list = rows(node);
    expect(list.map((r) => r.getAttribute("data-lead"))).toEqual(["lead-a", "lead-b"]);
    expect(list[0]!.querySelector(".family-host")?.textContent).toBe("box");
    expect(list[0]!.querySelector(".family-amount")?.textContent).toBe("92.3M tokens");
    expect(list[0]!.querySelector(".family-amount")?.getAttribute("title")).toMatch(/^\$18\.50\./);
    // The lead's chip comes first and says it holds its subagents.
    const chips = [...list[0]!.querySelectorAll(".family-chip")].map((c) => c.textContent);
    expect(chips).toEqual(["lead incl. subagents 70.0M", "reviewer 9.0M", "Codex 13.3M"]);
    expect(list[0]!.querySelector(".family-facts")?.textContent).toBe(
      "context 250K · 5 restarts · 4 sessions",
    );
    expect(list[1]!.querySelector(".family-facts")?.textContent).toBe("0 restarts · 1 session");
    expect(list[0]!.querySelector<HTMLElement>(".family-bar i")!.style.width).toBe("100%");
    expect(getTile("family")?.value).toBe("2 families");
  });

  it("switches the unit: the main number swaps, the ranking follows, the choice is saved", async () => {
    const { context, posts } = stubContext(
      [ok(answer())],
      [ok({ unit: "dollars", runaway_tokens: 5_000_000, runaway_usd: 25 })],
    );
    const node = await mount(context);
    segs(node, "Show cost as")[1]!.click();
    await flush();
    expect(posts).toEqual([{ family_cost_unit: "dollars" }]);
    const list = rows(node);
    expect(list.map((r) => r.getAttribute("data-lead"))).toEqual(["lead-b", "lead-a"]);
    expect(list[0]!.querySelector(".family-amount")?.textContent).toBe("$40.00");
    expect(list[0]!.querySelector(".family-amount")?.getAttribute("title")).toMatch(/^115K tokens\./);
    expect(node.querySelector(".family-needs")?.textContent).toBe(
      "Needs you: 1 family passed $25.00 today: Quick fix.",
    );
  });

  it("puts the unit back and says so when the save fails", async () => {
    const { context } = stubContext([ok(answer())], [failed(503, "not_saved", "could not write")]);
    const node = await mount(context);
    segs(node, "Show cost as")[1]!.click();
    await flush();
    expect(segs(node, "Show cost as")[0]!.getAttribute("aria-pressed")).toBe("true");
    expect(node.querySelector(".family-status")?.textContent).toContain("could not write");
  });

  it("saves the runaway amount in the shown unit", async () => {
    const { context, posts } = stubContext(
      [ok(answer())],
      [ok({ unit: "tokens", runaway_tokens: 2_500_000, runaway_usd: 25 })],
    );
    const node = await mount(context);
    const input = node.querySelector<HTMLInputElement>(".family-runaway")!;
    expect(input.value).toBe("5");
    input.value = "2.5";
    input.dispatchEvent(new Event("change"));
    await flush();
    expect(posts).toEqual([{ runaway_family_tokens: 2_500_000 }]);
  });

  it("carries the two hints and no more height than the controls", async () => {
    const { context } = stubContext([ok(answer())]);
    const node = await mount(context);
    const hints = [...node.querySelectorAll<HTMLElement>(".family-settings [data-hint]")].map(
      (h) => h.getAttribute("data-hint"),
    );
    expect(hints).toEqual([UNIT_HINT, RUNAWAY_HINT]);
  });

  it("toggles 7 d and 30 d, remembers it, and drops the old window's rows", async () => {
    const { context, reads } = stubContext([ok(answer()), ok(answer({ days: 30, families: [small()] }))]);
    const node = await mount(context);
    segs(node, "Window")[1]!.click();
    await flush();
    expect(reads).toEqual(["/v1/ae/family-cost?days=7", "/v1/ae/family-cost?days=30"]);
    expect(window.localStorage.getItem(DAYS_KEY)).toBe("30");
    expect(rows(node).map((r) => r.getAttribute("data-lead"))).toEqual(["lead-b"]);
    expect(segs(node, "Window")[1]!.getAttribute("aria-pressed")).toBe("true");
  });

  it("shows the reading state, then polls quickly until the first sweep lands", async () => {
    const { context, reads } = stubContext([
      ok({ state: "warming", days: 7, unit_default: "tokens" }),
      ok(answer()),
    ]);
    const node = await mount(context);
    expect(node.querySelector(".family-body")?.textContent).toContain("Reading the families");
    expect(getTile("family")?.value).toBe("Reading...");
    await vi.advanceTimersByTimeAsync(WARMING_POLL_MS);
    expect(reads).toHaveLength(2);
    expect(rows(node)).toHaveLength(2);
  });

  it("says plainly when no family spent anything, and polls once a minute", async () => {
    const { context, reads } = stubContext([
      ok(answer({ families: [], totals: { families: 0, cost_usd: 0, tokens_total: 0, flagged: 0 } })),
      ok(answer()),
    ]);
    const node = await mount(context);
    expect(node.querySelector(".family-body")?.textContent).toContain(
      "No family spent anything in the last 7 days.",
    );
    await vi.advanceTimersByTimeAsync(POLL_MS - 1);
    expect(reads).toHaveLength(1);
    await vi.advanceTimersByTimeAsync(1);
    expect(reads).toHaveLength(2);
    expect(rows(node)).toHaveLength(2);
  });

  it("notes a cut list and a short history", async () => {
    const { context } = stubContext([ok(answer({ truncated: true, partial: true }))]);
    const node = await mount(context);
    const text = node.querySelector(".family-body")?.textContent ?? "";
    expect(text).toContain("The list is cut to the biggest families.");
    expect(text).toContain("History starts 2026-10-23");
    expect(text).toContain(TODAY_NOTE);
    expect(TODAY_NOTE).toContain("Pacific");
  });

  it("shows a failed read and keeps the last rows", async () => {
    const { context } = stubContext([ok(answer()), failed(500, "boom", "the server broke")]);
    const node = await mount(context);
    await vi.advanceTimersByTimeAsync(POLL_MS);
    expect(node.querySelector(".family-status")?.textContent).toContain("the server broke");
    expect(rows(node)).toHaveLength(2);
    expect(getTile("family")?.health).toBe("attention");
  });
});
