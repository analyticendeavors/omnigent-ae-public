// The Families tab of the Spend card (2026-10-29 as the Family cost card): ranking, the unit
// setting with the other unit on hover, the either-amount flag, the typed amounts (5M, 500K),
// one-line rows with a detail, the filter, and the warming and empty states. The window is the
// card's one toggle (spend.ts), tested with the card in spend.test.ts.
// Every call is mocked: GET /v1/ae/family-cost?days=N and POST /v1/ae/family-cost/settings.
import type { ExtensionContext } from "@omnigent/extension-sdk";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { FamilyCostAnswer, FamilyRow } from "../src/api";
import {
  activateFamilyCost,
  amountText,
  COST_NOTE,
  deactivateFamilyCost,
  familyCostSection,
  familiesLine,
  familyNotes,
  familyTile,
  filterFamilies,
  flaggedFamilies,
  flaggedTotal,
  hoverText,
  needsYou,
  parseDollarAmount,
  parseTokenAmount,
  POLL_MS,
  rankFamilies,
  readDollarAmount,
  readTokenAmount,
  RUNAWAY_HINT,
  SAVED_MS,
  settingsOf,
  sharePct,
  shortTokens,
  STALE_MS,
  TODAY_NOTE,
  TOKENS_NOTE,
  tokensText,
  TOKEN_FORMAT_ERROR,
  TOKEN_MAX_ERROR,
  TOKEN_MIN_ERROR,
  UNIT_HINT,
  USD_MIN_ERROR,
  updatedText,
  WARMING_POLL_MS,
} from "../src/familyCost";
import { HINT_MAX } from "../src/hint";
import { FOLD_KEY } from "../src/fold";
import { flaggedCount, resetSpend } from "../src/spend";
import { getTile, resetTiles } from "../src/tiles";
import { resetAttention, setAttention } from "../src/attentionStore";
import { item as needItem, overview as needOverview } from "./attentionFixtures";

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
const tokensInput = (node: HTMLElement) =>
  node.querySelector<HTMLInputElement>(".family-runaway-tokens")!;
const usdInput = (node: HTMLElement) => node.querySelector<HTMLInputElement>(".family-runaway-usd")!;
const type = (input: HTMLInputElement, text: string) => {
  input.value = text;
  input.dispatchEvent(new Event("change"));
};

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
    expect(hoverText("tokens", family())).toMatch(
      /^92\.3M tokens\. \$18\.50\. Input 3\.0M \(with cache writes\)/,
    );
    expect(hoverText("dollars", family())).toMatch(/^\$18\.50\. 92\.3M tokens\./);
  });

  it("keeps both hints within the length rule", () => {
    expect(UNIT_HINT.length).toBeLessThanOrEqual(HINT_MAX);
    expect(RUNAWAY_HINT.length).toBeLessThanOrEqual(HINT_MAX);
    expect(RUNAWAY_HINT).toMatch(/^Either amount flags a family/);
    expect(RUNAWAY_HINT).toContain("0 or blank turns it off");
  });
});

describe("typed amounts, commas and tiny values", () => {
  it("takes a comma only as a thousands group", () => {
    for (const bad of ["5,5M", "5,5", "12,34", "1,23,456", ",5", "5,"]) {
      expect(readTokenAmount(bad)).toEqual({ ok: false, error: TOKEN_FORMAT_ERROR });
    }
    expect(readDollarAmount("1,5")).toMatchObject({ ok: false });
    expect(parseDollarAmount("$1,000")).toBe(1_000);
    expect(parseDollarAmount("$1,000.50")).toBe(1_000.5);
  });

  it("calls a nonzero amount that rounds to nothing an error, not off", () => {
    expect(readTokenAmount("0.4")).toEqual({ ok: false, error: TOKEN_MIN_ERROR });
    expect(readTokenAmount("0.0000000004B")).toEqual({ ok: false, error: TOKEN_MIN_ERROR });
    expect(readDollarAmount("$0.004")).toEqual({ ok: false, error: USD_MIN_ERROR });
    expect(parseTokenAmount("0.0")).toBe(0);
    expect(parseDollarAmount("0.00")).toBe(0);
    expect(parseTokenAmount("0.6")).toBe(1);
    expect(parseDollarAmount("0.01")).toBe(0.01);
  });
});

describe("typed amounts", () => {
  it("reads tokens as typed: suffixes, separators, plain numbers, blank and zero", () => {
    expect(parseTokenAmount("5M")).toBe(5_000_000);
    expect(parseTokenAmount("5m")).toBe(5_000_000);
    expect(parseTokenAmount("5.5M")).toBe(5_500_000);
    expect(parseTokenAmount("500k")).toBe(500_000);
    expect(parseTokenAmount("500K")).toBe(500_000);
    expect(parseTokenAmount("1B")).toBe(1_000_000_000);
    expect(parseTokenAmount("5,000,000")).toBe(5_000_000);
    expect(parseTokenAmount("5000000")).toBe(5_000_000);
    expect(parseTokenAmount("1,000")).toBe(1_000);
    expect(parseTokenAmount("1,234.5K")).toBe(1_234_500);
    expect(parseTokenAmount("5 000 000")).toBe(5_000_000);
    expect(parseTokenAmount(" 5M tokens ")).toBe(5_000_000);
    // A plain number is tokens: 5 means 5 tokens.
    expect(parseTokenAmount("5")).toBe(5);
    expect(parseTokenAmount("")).toBe(0);
    expect(parseTokenAmount("   ")).toBe(0);
    expect(parseTokenAmount("0")).toBe(0);
  });

  it("rejects negatives, junk and more than the server takes, and says the limit", () => {
    for (const bad of ["-5M", "abc", "5MM", "5x", "1.2.3", "M"]) {
      expect(parseTokenAmount(bad)).toBeNull();
    }
    expect(parseTokenAmount("1.1B")).toBeNull();
    expect(parseTokenAmount("5000000000")).toBeNull();
    const over = readTokenAmount("2B");
    expect(over).toEqual({ ok: false, error: TOKEN_MAX_ERROR });
    expect(TOKEN_MAX_ERROR).toContain("1B");
  });

  it("reads dollars as typed", () => {
    expect(parseDollarAmount("$25")).toBe(25);
    expect(parseDollarAmount("25")).toBe(25);
    expect(parseDollarAmount("25.50")).toBe(25.5);
    expect(parseDollarAmount("1,000")).toBe(1000);
    expect(parseDollarAmount("")).toBe(0);
    expect(parseDollarAmount("100000")).toBe(100_000);
    expect(parseDollarAmount("100001")).toBeNull();
    expect(parseDollarAmount("-1")).toBeNull();
    expect(parseDollarAmount("5M")).toBeNull();
    expect(parseDollarAmount("abc")).toBeNull();
  });

  it("shows a saved amount short, and plain digits when no short form reads back the same", () => {
    expect(shortTokens(5_000_000)).toBe("5M");
    expect(shortTokens(500_000)).toBe("500K");
    expect(shortTokens(1_000_000_000)).toBe("1B");
    expect(shortTokens(1_500_000)).toBe("1.5M");
    expect(shortTokens(5_500_000)).toBe("5.5M");
    expect(shortTokens(1_234_567)).toBe("1234567");
    expect(shortTokens(999_999)).toBe("999999");
    expect(shortTokens(0)).toBe("0");
    expect(shortTokens(5)).toBe("5");
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

  it("filters by title or host, ignoring case", () => {
    const list = [family(), small()];
    expect(filterFamilies(list, "").map((r) => r.lead_id)).toEqual(["lead-a", "lead-b"]);
    expect(filterFamilies(list, "TOKEN").map((r) => r.lead_id)).toEqual(["lead-a"]);
    expect(filterFamilies(list, "lap").map((r) => r.lead_id)).toEqual(["lead-b"]);
    expect(filterFamilies(list, "nothing")).toEqual([]);
  });
});

describe("Needs you", () => {
  const settings = { unit: "tokens" as const, runaway_tokens: 5_000_000, runaway_usd: 25 };

  it("flags a family that passed either amount today and names the amount that tripped", () => {
    // "Quick fix" spent $30 today (over $25) but only 115K tokens; the other spent 4.0M tokens, $2.
    expect(needsYou(answer(), settings)).toBe("1 family passed $25 today: Quick fix.");
    expect(needsYou(answer(), { ...settings, runaway_tokens: 1_000_000 })).toBe(
      "2 families passed 1M tokens or $25 today: Session token audit, Quick fix.",
    );
    expect(needsYou(answer(), { ...settings, runaway_tokens: 1_000_000, runaway_usd: 0 })).toBe(
      "1 family passed 1M tokens today: Session token audit.",
    );
  });

  it("does not depend on the unit shown", () => {
    const one = needsYou(answer(), { ...settings, unit: "dollars" });
    expect(one).toBe(needsYou(answer(), { ...settings, unit: "tokens" }));
  });

  it("stays quiet when both are off, nothing passed, or the card is warming", () => {
    expect(needsYou(answer(), { ...settings, runaway_usd: 0, runaway_tokens: 0 })).toBeNull();
    expect(needsYou(answer(), { ...settings, runaway_usd: 500 })).toBeNull();
    expect(needsYou({ state: "warming", days: 7, unit_default: "tokens" }, settings)).toBeNull();
    expect(flaggedFamilies(null, settings)).toEqual([]);
  });

  it("says a 0 amount turns that one off", () => {
    const off = { ...settings, runaway_tokens: 0 };
    expect(flaggedFamilies(answer(), off).map((r) => r.lead_id)).toEqual(["lead-b"]);
    expect(flaggedFamilies(answer(), { ...off, runaway_usd: 0 })).toEqual([]);
  });

  it("checks the token flag against input plus output, not cache reads", () => {
    const base = answer();
    const rows = (base.families ?? []).map((r) =>
      r.lead_id === "lead-a" ? { ...r, today: { ...r.today, flag_tokens: 200_000 } } : r,
    );
    const tokensOnly = { ...settings, runaway_tokens: 1_000_000, runaway_usd: 0 };
    // lead-a's 4M tokens today are nearly all cache reads: 200K of them count.
    expect(flaggedFamilies({ ...base, families: rows }, tokensOnly)).toEqual([]);
    // A snapshot without flag_tokens falls back to the full total.
    expect(flaggedFamilies(base, tokensOnly).map((r) => r.lead_id)).toEqual(["lead-a"]);
  });
});

describe("the tile", () => {
  it("is reading, then ok, then amber for a runaway or an old answer", () => {
    expect(familyTile(null, null, "tokens").value).toBe("Reading...");
    expect(familyTile(null, new Error("x"), "tokens").health).toBe("attention");
    const quiet = { unit: "tokens" as const, runaway_tokens: 0, runaway_usd: 0 };
    const ready = familyTile(answer({ settings: quiet }), null, "tokens", 7, NOW);
    expect(ready).toMatchObject({ value: "2 families", health: "ok" });
    expect(ready.note).toBe("top 92.3M tokens in 7 d");
    // Quick fix passed $25 today, so the default amounts flag it in either unit.
    const hot = familyTile(answer(), null, "tokens", 7, NOW);
    expect(hot).toMatchObject({ health: "attention" });
    expect(hot.note).toBe("1 runaway · 92.4M tokens in 7 d");
    // The pill line says the runaway, not the token figure the note repeats.
    expect(hot.brief).toBe("1 runaway today");
    expect(ready.brief).toBe("2 families");
    expect(familyTile(answer(), null, "dollars", 7, NOW).note).toBe("1 runaway · $58.50 in 7 d");
    expect(
      familyTile(answer({ settings: quiet }), null, "tokens", 7, NOW + STALE_MS + 60_000).health,
    ).toBe("attention");
  });

  it("says when it was updated by the server's time", () => {
    expect(updatedText(null)).toBe("");
    expect(updatedText(answer(), NOW)).toMatch(/^Updated /);
    expect(updatedText(answer(), NOW + STALE_MS + 60_000)).toMatch(/refresh failing$/);
    expect(settingsOf(null).unit).toBe("tokens");
  });

  it("keeps the footnotes for the card's info icon", () => {
    expect(familyNotes(null)).toEqual([TOKENS_NOTE, COST_NOTE, TODAY_NOTE]);
    expect(TOKENS_NOTE).toContain("cache reads");
    expect(TODAY_NOTE).toContain("Pacific");
    const notes = familyNotes(answer({ truncated: true, partial: true }));
    expect(notes).toContain("The list is cut to the biggest families.");
    expect(notes.join(" ")).toContain("History starts 2026-10-23");
  });
});

describe("the card", () => {
  it("lists the families biggest first, one line each", async () => {
    const { context, reads } = stubContext([ok(answer())]);
    const node = await mount(context);
    expect(reads).toEqual(["/v1/ae/family-cost?days=30"]);
    const list = rows(node);
    expect(list.map((r) => r.getAttribute("data-lead"))).toEqual(["lead-a", "lead-b"]);
    const line = list[0]!.querySelector<HTMLButtonElement>(".family-line")!;
    expect(line.tagName).toBe("BUTTON");
    expect(line.querySelector(".family-title")?.textContent).toBe("Session token audit");
    expect(line.querySelector(".family-host")?.textContent).toBe("box");
    expect(line.querySelector(".family-amount")?.textContent).toBe("92.3M");
    expect(line.querySelector(".family-amount")?.getAttribute("title")).toMatch(
      /^92\.3M tokens\. \$18\.50\./,
    );
    expect(line.querySelector<HTMLElement>(".family-bar i")!.style.width).toBe("100%");
    // The default row holds no role chips and no counts: they are in the detail.
    expect(line.querySelector(".family-chip")).toBeNull();
    expect(line.textContent).not.toContain("restart");
    expect(getTile("family")?.value).toBe("2 families");
  });

  it("opens a detail with the roles, context, restarts, sessions and today's spend, one at a time", async () => {
    const { context } = stubContext([ok(answer())]);
    const node = await mount(context);
    const lines = () => [...node.querySelectorAll<HTMLButtonElement>(".family-line")];
    expect(lines().map((b) => b.getAttribute("aria-expanded"))).toEqual(["false", "false"]);
    expect(node.querySelector<HTMLElement>(".family-detail")!.hidden).toBe(true);
    lines()[0]!.click();
    expect(lines().map((b) => b.getAttribute("aria-expanded"))).toEqual(["true", "false"]);
    const detail = rows(node)[0]!.querySelector<HTMLElement>(".family-detail")!;
    expect(detail.hidden).toBe(false);
    expect(lines()[0]!.getAttribute("aria-controls")).toBe(detail.id);
    const chips = [...detail.querySelectorAll(".family-chip")].map((c) => c.textContent);
    expect(chips).toEqual(["lead incl. subagents 70.0M", "reviewer 9.0M", "Codex 13.3M"]);
    expect(detail.querySelector(".family-facts")?.textContent).toBe(
      "context 250K · 5 restarts · 4 sessions · today 4.0M tokens",
    );
    lines()[1]!.click();
    expect(lines().map((b) => b.getAttribute("aria-expanded"))).toEqual(["false", "true"]);
    lines()[1]!.click();
    expect(lines().map((b) => b.getAttribute("aria-expanded"))).toEqual(["false", "false"]);
  });

  it("keeps the list in a box that scrolls, with a filter above it", async () => {
    const many = Array.from({ length: 30 }, (_, i) =>
      family({ lead_id: `lead-${i}`, title: `Family ${i}`, host: i % 2 ? "box" : "laptop" }),
    );
    const { context } = stubContext([
      ok(answer({ families: many, totals: { families: 30, cost_usd: 1, tokens_total: 1, flagged: 0 } })),
    ]);
    const node = await mount(context);
    expect(node.querySelector(".family-box")).not.toBeNull();
    const filter = node.querySelector<HTMLInputElement>(".family-filter-input")!;
    expect(filter.getAttribute("aria-label")).toBe("Filter families");
    expect(filter.closest<HTMLElement>(".family-filter")!.hidden).toBe(false);
    filter.value = "FAMILY 2";
    filter.dispatchEvent(new Event("input"));
    // Family 2 and Family 20 to 29.
    expect(rows(node)).toHaveLength(11);
    expect(node.querySelector(".family-count")?.textContent).toBe("Showing 11 of 30 families");
    filter.value = "laptop";
    filter.dispatchEvent(new Event("input"));
    expect(rows(node)).toHaveLength(15);
    filter.value = "zzz";
    filter.dispatchEvent(new Event("input"));
    expect(rows(node)).toHaveLength(0);
    expect(node.querySelector(".family-body")?.textContent).toContain('No family matches "zzz".');
    filter.value = "";
    filter.dispatchEvent(new Event("input"));
    expect(rows(node)).toHaveLength(30);
    expect(node.querySelector(".family-count")).toBeNull();
  });

  it("says how many of the families it shows when the server cut the list", async () => {
    const { context } = stubContext([
      ok(answer({ truncated: true, totals: { families: 9, cost_usd: 1, tokens_total: 1, flagged: 0 } })),
    ]);
    const node = await mount(context);
    expect(node.querySelector(".family-count")?.textContent).toBe("Showing 2 of 9 families");
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
    expect(list[0]!.querySelector(".family-amount")?.getAttribute("title")).toMatch(/^\$40\.00\. 115K tokens\./);
    // The unit no longer picks the amount: the same line either way.
    expect(node.querySelector(".family-needs")?.textContent).toBe(
      "Needs you: 1 family passed $25 today: Quick fix.",
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

  it("shows both flag amounts side by side with their units inside the boxes and one hint", async () => {
    const { context } = stubContext([ok(answer())]);
    const node = await mount(context);
    const settings = node.querySelector(".family-settings")!;
    expect(settings.textContent).toContain("Flag a family over");
    expect(tokensInput(node).value).toBe("5M");
    expect(usdInput(node).value).toBe("25");
    expect(tokensInput(node).getAttribute("inputmode")).toBe("decimal");
    expect(tokensInput(node).type).toBe("text");
    // The unit is in the same box as its input, so it cannot wrap onto its own line.
    expect(tokensInput(node).parentElement!.textContent).toBe("tokens");
    expect(usdInput(node).parentElement!.textContent).toBe("$");
    expect(tokensInput(node).parentElement).toBe(tokensInput(node).closest(".family-input"));
    const hints = [...settings.querySelectorAll<HTMLElement>("[data-hint]")].map((h) =>
      h.getAttribute("data-hint"),
    );
    expect(hints).toEqual([RUNAWAY_HINT]);
    expect(
      [...node.querySelectorAll<HTMLElement>(".family-unit [data-hint]")].map((h) =>
        h.getAttribute("data-hint"),
      ),
    ).toEqual([UNIT_HINT]);
  });

  it("saves a token amount typed as 5M, 500k or 5,000,000 and shows the Saved tick", async () => {
    const { context, posts } = stubContext(
      [ok(answer())],
      [
        ok({ unit: "tokens", runaway_tokens: 500_000, runaway_usd: 25 }),
        ok({ unit: "tokens", runaway_tokens: 7_000_000, runaway_usd: 25 }),
      ],
    );
    const node = await mount(context);
    type(tokensInput(node), "500k");
    await flush();
    expect(posts).toEqual([{ runaway_family_tokens: 500_000 }]);
    expect(tokensInput(node).value).toBe("500K");
    const saved = node.querySelector(".family-limit-tokens .family-saved");
    expect(saved?.textContent).toBe("Saved");
    expect(saved?.querySelector("svg")).not.toBeNull();
    await vi.advanceTimersByTimeAsync(SAVED_MS);
    expect(node.querySelector(".family-limit-tokens .family-saved")).toBeNull();
    type(tokensInput(node), "7,000,000");
    await flush();
    expect(posts[1]).toEqual({ runaway_family_tokens: 7_000_000 });
    expect(tokensInput(node).value).toBe("7M");
  });

  it("says Saved when the same amount is typed again, and does not send it", async () => {
    const { context, posts } = stubContext([ok(answer())]);
    const node = await mount(context);
    type(tokensInput(node), "5m");
    await flush();
    expect(posts).toEqual([]);
    expect(tokensInput(node).value).toBe("5M");
    expect(node.querySelector(".family-limit-tokens .family-saved")?.textContent).toBe("Saved");
    // Enter commits too.
    type(usdInput(node), "25");
    usdInput(node).dispatchEvent(new KeyboardEvent("keydown", { key: "Enter" }));
    expect(node.querySelector(".family-limit-usd .family-saved")?.textContent).toBe("Saved");
  });

  it("saves dollars and turns an amount off with a blank", async () => {
    const { context, posts } = stubContext(
      [ok(answer())],
      [
        ok({ unit: "tokens", runaway_tokens: 5_000_000, runaway_usd: 12.5 }),
        ok({ unit: "tokens", runaway_tokens: 0, runaway_usd: 12.5 }),
      ],
    );
    const node = await mount(context);
    type(usdInput(node), "$12.50");
    await flush();
    expect(posts).toEqual([{ runaway_family_usd: 12.5 }]);
    expect(usdInput(node).value).toBe("12.5");
    type(tokensInput(node), "");
    await flush();
    expect(posts[1]).toEqual({ runaway_family_tokens: 0 });
    expect(tokensInput(node).value).toBe("0");
  });

  it("flags by either amount at once, before the server answers", async () => {
    const { context } = stubContext([ok(answer())], [ok({ unit: "tokens", runaway_tokens: 1_000_000, runaway_usd: 25 })]);
    const node = await mount(context);
    expect(node.querySelector(".family-needs")?.textContent).toBe(
      "Needs you: 1 family passed $25 today: Quick fix.",
    );
    type(tokensInput(node), "1M");
    await flush();
    expect(node.querySelector(".family-needs")?.textContent).toBe(
      "Needs you: 2 families passed 1M tokens or $25 today: Session token audit, Quick fix.",
    );
    expect(node.querySelectorAll(".family-row.is-hot")).toHaveLength(2);
  });

  it("keeps the text and says what to type when it cannot be read or is over the limit", async () => {
    const { context, posts } = stubContext([ok(answer())]);
    const node = await mount(context);
    type(tokensInput(node), "lots");
    expect(posts).toEqual([]);
    expect(tokensInput(node).value).toBe("lots");
    expect(tokensInput(node).getAttribute("aria-invalid")).toBe("true");
    expect(node.querySelector(".family-limit-tokens .family-field-error")?.textContent).toContain(
      "5M",
    );
    type(tokensInput(node), "5000000000");
    expect(node.querySelector(".family-limit-tokens .family-field-error")?.textContent).toBe(
      TOKEN_MAX_ERROR,
    );
    type(usdInput(node), "200000");
    expect(node.querySelector(".family-limit-usd .family-field-error")?.textContent).toContain(
      "$100,000",
    );
    expect(posts).toEqual([]);
    // Typing again clears the error.
    tokensInput(node).dispatchEvent(new Event("input"));
    expect(node.querySelector(".family-limit-tokens .family-field-error")).toBeNull();
  });

  it("keeps the typed text and shows the server's error when the save fails", async () => {
    const { context } = stubContext([ok(answer())], [failed(422, "bad", "amount refused")]);
    const node = await mount(context);
    type(tokensInput(node), "2m");
    await flush();
    expect(tokensInput(node).value).toBe("2m");
    expect(node.querySelector(".family-limit-tokens .family-field-error")?.textContent).toContain(
      "amount refused",
    );
    expect(node.querySelector(".family-limit-tokens .family-saved")).toBeNull();
  });

  it("leaves a number being typed alone when a poll lands", async () => {
    const { context } = stubContext([ok(answer()), ok(answer())]);
    const node = await mount(context);
    document.body.appendChild(node);
    const input = tokensInput(node);
    input.focus();
    input.value = "3";
    await vi.advanceTimersByTimeAsync(POLL_MS);
    expect(tokensInput(node)).toBe(input);
    expect(input.value).toBe("3");
  });

  it("reads the card's window from storage and asks for it", async () => {
    window.localStorage.setItem("presets.days", "7");
    const { context, reads } = stubContext([ok(answer())]);
    await mount(context);
    expect(reads).toEqual(["/v1/ae/family-cost?days=7"]);
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
      "No family spent anything in the last 30 days.",
    );
    expect(node.querySelector<HTMLElement>(".family-filter")!.hidden).toBe(true);
    await vi.advanceTimersByTimeAsync(POLL_MS - 1);
    expect(reads).toHaveLength(1);
    await vi.advanceTimersByTimeAsync(1);
    expect(reads).toHaveLength(2);
    expect(rows(node)).toHaveLength(2);
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

// ── the save and poll races (review round 1) ───────────────────────────────

interface Pending {
  resolve: (reply: Reply) => void;
  body?: unknown;
}

/** A server whose every answer waits for the test to resolve it. */
function heldContext() {
  const gets: Pending[] = [];
  const posts: Pending[] = [];
  const request = vi.fn(
    ({ method, body }: { method: string; path: string; body?: unknown }) =>
      new Promise<Reply>((resolve) => {
        (method === "POST" ? posts : gets).push({ resolve, body });
      }),
  );
  const context = { view: "fabric", server: { request } } as unknown as ExtensionContext;
  return { context, gets, posts };
}

const settings = (extra: Partial<FamilyCostAnswer["settings"]> = {}) => ({
  unit: "tokens" as const,
  runaway_tokens: 5_000_000,
  runaway_usd: 25,
  ...extra,
});

/** Mount against a held server and answer the first read. */
async function mountHeld() {
  const held = heldContext();
  const node = familyCostSection();
  document.getElementById("root")?.appendChild(node);
  activateFamilyCost(held.context, node);
  held.gets[0]!.resolve(ok(answer()));
  await flush();
  return { ...held, node };
}

const typeKeys = (input: HTMLInputElement, text: string) => {
  input.value = text;
  input.dispatchEvent(new Event("input"));
};

describe("save and poll races", () => {
  it("makes Enter and the change event after it one save", async () => {
    const { node, posts } = await mountHeld();
    typeKeys(tokensInput(node), "7M");
    tokensInput(node).dispatchEvent(new KeyboardEvent("keydown", { key: "Enter" }));
    tokensInput(node).dispatchEvent(new Event("change"));
    expect(posts).toHaveLength(1);
    // Nothing says Saved before the server answers.
    expect(node.querySelector(".family-limit-tokens .family-saved")).toBeNull();
    posts[0]!.resolve(ok(settings({ runaway_tokens: 7_000_000 })));
    await flush();
    expect(posts).toHaveLength(1);
    expect(node.querySelector(".family-limit-tokens .family-saved")?.textContent).toBe("Saved");
  });

  it("does not let a poll that started before a save undo it", async () => {
    const { node, gets, posts } = await mountHeld();
    await vi.advanceTimersByTimeAsync(POLL_MS);
    expect(gets).toHaveLength(2);
    typeKeys(tokensInput(node), "1M");
    tokensInput(node).dispatchEvent(new Event("change"));
    // The old poll lands while the save is out, then the save lands.
    gets[1]!.resolve(ok(answer()));
    await flush();
    expect(node.querySelector(".family-needs")?.textContent).toContain("1M tokens");
    expect(tokensInput(node).value).toBe("1M");
    posts[0]!.resolve(ok(settings({ runaway_tokens: 1_000_000 })));
    await flush();
    expect(node.querySelector(".family-needs")?.textContent).toContain("1M tokens");
    expect(tokensInput(node).value).toBe("1M");
  });

  it("does not let a poll that started during a save undo it after the save lands", async () => {
    const { node, gets, posts } = await mountHeld();
    typeKeys(tokensInput(node), "1M");
    tokensInput(node).dispatchEvent(new Event("change"));
    await vi.advanceTimersByTimeAsync(POLL_MS);
    expect(gets).toHaveLength(2);
    posts[0]!.resolve(ok(settings({ runaway_tokens: 1_000_000 })));
    await flush();
    gets[1]!.resolve(ok(answer()));
    await flush();
    expect(tokensInput(node).value).toBe("1M");
    expect(node.querySelector(".family-needs")?.textContent).toContain("1M tokens");
    // The next poll starts after the save and carries the new amounts.
    await vi.advanceTimersByTimeAsync(POLL_MS);
    gets[2]!.resolve(ok(answer({ settings: settings({ runaway_tokens: 1_000_000 }) })));
    await flush();
    expect(tokensInput(node).value).toBe("1M");
  });

  it("keeps the unit toggle where it was put through an old poll", async () => {
    const { node, gets, posts } = await mountHeld();
    await vi.advanceTimersByTimeAsync(POLL_MS);
    segs(node, "Show cost as")[1]!.click();
    gets[1]!.resolve(ok(answer()));
    await flush();
    expect(segs(node, "Show cost as")[1]!.getAttribute("aria-pressed")).toBe("true");
    posts[0]!.resolve(ok(settings({ unit: "dollars" })));
    await flush();
    expect(segs(node, "Show cost as")[1]!.getAttribute("aria-pressed")).toBe("true");
  });

  it("leaves text typed after the request went out alone when the answer lands", async () => {
    const { node, posts } = await mountHeld();
    typeKeys(tokensInput(node), "1M");
    tokensInput(node).dispatchEvent(new Event("change"));
    typeKeys(tokensInput(node), "2M");
    posts[0]!.resolve(ok(settings({ runaway_tokens: 1_000_000 })));
    await flush();
    expect(tokensInput(node).value).toBe("2M");
    expect(node.querySelector(".family-limit-tokens .family-saved")).toBeNull();
    // A failure leaves it alone too.
    typeKeys(tokensInput(node), "3M");
    tokensInput(node).dispatchEvent(new Event("change"));
    typeKeys(tokensInput(node), "4M");
    posts[1]!.resolve(failed(422, "bad", "amount refused"));
    await flush();
    expect(tokensInput(node).value).toBe("4M");
    expect(node.querySelector(".family-limit-tokens .family-field-error")).toBeNull();
  });

  it("keeps two saves apart: each answer takes only its own field", async () => {
    const { node, posts } = await mountHeld();
    typeKeys(tokensInput(node), "1M");
    tokensInput(node).dispatchEvent(new Event("change"));
    typeKeys(usdInput(node), "10");
    usdInput(node).dispatchEvent(new Event("change"));
    expect(posts).toHaveLength(2);
    // Each reply is what the server held when it handled that request: the other field is old.
    posts[1]!.resolve(ok(settings({ runaway_usd: 10 })));
    await flush();
    posts[0]!.resolve(ok(settings({ runaway_tokens: 1_000_000 })));
    await flush();
    expect(tokensInput(node).value).toBe("1M");
    expect(usdInput(node).value).toBe("10");
    expect(node.querySelector(".family-needs")?.textContent).toContain("1M tokens or $10");
  });

  it("rolls back only the field whose save failed", async () => {
    const { node, posts } = await mountHeld();
    typeKeys(tokensInput(node), "1M");
    tokensInput(node).dispatchEvent(new Event("change"));
    typeKeys(usdInput(node), "10");
    usdInput(node).dispatchEvent(new Event("change"));
    posts[0]!.resolve(failed(422, "bad", "amount refused"));
    await flush();
    posts[1]!.resolve(ok(settings({ runaway_usd: 10 })));
    await flush();
    expect(node.querySelector(".family-limit-tokens .family-field-error")?.textContent).toContain(
      "amount refused",
    );
    expect(tokensInput(node).value).toBe("1M");
    expect(usdInput(node).value).toBe("10");
    expect(node.querySelector(".family-limit-usd .family-saved")?.textContent).toBe("Saved");
    // The flag is back on 5M tokens and on the new $10.
    expect(node.querySelector(".family-needs")?.textContent).toContain("$10");
    expect(node.querySelector(".family-needs")?.textContent).not.toContain("1M tokens");
  });

  it("counts flagged families from the server when the list is cut, until a local edit", async () => {
    const cut = answer({
      settings: settings({ runaway_tokens: 1_000_000 }),
      families: [family()],
      totals: { families: 9, cost_usd: 58.5, tokens_total: 92_415_000, flagged: 3 },
      truncated: true,
    });
    const { context } = stubContext(
      [ok(cut)],
      [ok(settings({ runaway_tokens: 3_000_000 }))],
    );
    const node = await mount(context);
    expect(flaggedCount()).toBe(3);
    expect(node.querySelector(".family-needs")?.textContent).toBe(
      "Needs you: 3 families passed 1M tokens today: Session token audit and 2 more.",
    );
    // After an edit the echo is older, so the rows we have are counted until the next poll.
    type(tokensInput(node), "3M");
    await flush();
    expect(flaggedCount()).toBe(1);
  });

  it("flaggedTotal trusts the server only for the amounts it echoed", () => {
    const cut = answer({
      families: [family()],
      totals: { families: 9, cost_usd: 1, tokens_total: 1, flagged: 4 },
    });
    expect(flaggedTotal(cut, settings())).toBe(4);
    expect(flaggedTotal(cut, settings({ runaway_tokens: 3_000_000 }))).toBe(1);
    expect(flaggedTotal(null, settings())).toBe(0);
  });

  it("shows the host pill only when the answer has more than one host", async () => {
    const { context } = stubContext([ok(answer())]);
    const node = await mount(context);
    expect(node.querySelectorAll(".family-host")).toHaveLength(2);
    deactivateFamilyCost();
    const same = stubContext([
      ok(answer({ families: [family(), { ...small(), host: "box" }] })),
    ]);
    const other = await mount(same.context);
    expect(other.querySelectorAll(".family-host")).toHaveLength(0);
    expect(other.querySelector(".family-row .family-facts")?.textContent).toContain("host box");
  });
});

// ── the families fold and the filter above it ──────────────────────────────

const famHead = (node: HTMLElement) =>
  node.querySelector<HTMLButtonElement>('.presets-fold-head[data-fold="families"]');
const famBody = (node: HTMLElement) => document.getElementById(famHead(node)!.getAttribute("aria-controls")!)!;
const filterBox = (node: HTMLElement) => node.querySelector<HTMLInputElement>(".family-filter-input")!;
const typeFilter = (node: HTMLElement, text: string) => {
  filterBox(node).value = text;
  filterBox(node).dispatchEvent(new Event("input"));
};
const lineText = (parts: HTMLElement | string) => (typeof parts === "string" ? parts : parts.textContent);

describe("the families fold", () => {
  it("starts closed with a one-line summary of the biggest family", async () => {
    const { context } = stubContext([ok(answer())]);
    const node = await mount(context);
    expect(famHead(node)!.getAttribute("aria-expanded")).toBe("false");
    expect(famBody(node).hidden).toBe(true);
    expect(famHead(node)!.textContent).toBe("2 families, biggest: Session token audit 92.3M");
    expect(famHead(node)!.querySelector(".presets-fold-name")?.getAttribute("title")).toBe(
      "Session token audit",
    );
    // The filter is outside the fold and always there.
    expect(filterBox(node).closest(".presets-fold")).toBeNull();
    expect(node.querySelector<HTMLElement>(".family-filter")!.hidden).toBe(false);
  });

  it("writes the line in the current unit, singular, and with a count of matches", () => {
    const one = [family()];
    expect(lineText(familiesLine(one, 1, 1, false, "tokens"))).toBe("1 family, biggest: Session token audit 92.3M");
    expect(lineText(familiesLine([family(), small()], 2, 2, false, "dollars"))).toBe(
      "2 families, biggest: Quick fix $40.00",
    );
    expect(lineText(familiesLine(one, 1, 9, true, "tokens"))).toBe("1 match");
    expect(lineText(familiesLine(one, 3, 9, true, "tokens"))).toBe("3 matches");
    expect(lineText(familiesLine([], 0, 0, false, "tokens"))).toBe("0 families");
  });

  it("draws no fold for an empty answer, as before", async () => {
    const { context } = stubContext([
      ok(answer({ families: [], totals: { families: 0, cost_usd: 0, tokens_total: 0, flagged: 0 } })),
    ]);
    const node = await mount(context);
    expect(famHead(node)).toBeNull();
  });

  it("opens and remembers on a click, and keeps Showing N of M inside", async () => {
    const cut = answer({ totals: { families: 9, cost_usd: 1, tokens_total: 1, flagged: 0 }, truncated: true });
    const { context } = stubContext([ok(cut)]);
    const node = await mount(context);
    expect(famBody(node).querySelector(".family-count")?.textContent).toBe("Showing 2 of 9 families");
    famHead(node)!.click();
    expect(famBody(node).hidden).toBe(false);
    expect(window.localStorage.getItem(FOLD_KEY + "families")).toBe("open");
  });

  it("opens as soon as a filter is typed, shows only the matches, and keeps focus in the box", async () => {
    const { context } = stubContext([ok(answer())]);
    const node = await mount(context);
    filterBox(node).focus();
    typeFilter(node, "quick");
    expect(famHead(node)!.getAttribute("aria-expanded")).toBe("true");
    expect(famBody(node).hidden).toBe(false);
    expect(famHead(node)!.textContent).toBe("1 match");
    expect(rows(node).map((r) => r.getAttribute("data-lead"))).toEqual(["lead-b"]);
    expect(document.activeElement).toBe(filterBox(node));
    // Live: another keystroke narrows again.
    typeFilter(node, "quick fixx");
    expect(node.querySelector(".family-body")?.textContent).toContain('No family matches "quick fixx".');
    expect(document.activeElement).toBe(filterBox(node));
    // Nothing was saved by typing.
    expect(window.localStorage.getItem(FOLD_KEY + "families")).toBeNull();
  });

  it("goes back to the viewer's remembered state when the filter is cleared", async () => {
    const { context } = stubContext([ok(answer())]);
    const node = await mount(context);
    typeFilter(node, "quick");
    typeFilter(node, "");
    expect(famHead(node)!.getAttribute("aria-expanded")).toBe("false");
    expect(rows(node)).toHaveLength(2);
    expect(window.localStorage.getItem(FOLD_KEY + "families")).toBeNull();
    // A fold left open stays open through typing and clearing.
    famHead(node)!.click();
    typeFilter(node, "quick");
    typeFilter(node, "");
    expect(famHead(node)!.getAttribute("aria-expanded")).toBe("true");
    expect(window.localStorage.getItem(FOLD_KEY + "families")).toBe("open");
  });

  it("ignores a click on the fold while a filter forces it open", async () => {
    const { context } = stubContext([ok(answer())]);
    const node = await mount(context);
    typeFilter(node, "quick");
    famHead(node)!.click();
    expect(famHead(node)!.getAttribute("aria-expanded")).toBe("true");
    expect(window.localStorage.getItem(FOLD_KEY + "families")).toBeNull();
  });

  it("keeps the filter's focus and the fold's state through a poll", async () => {
    const { context } = stubContext([ok(answer()), ok(answer({ computed_at: "2026-10-29T14:59:00Z" }))]);
    const node = await mount(context);
    document.body.appendChild(node);
    famHead(node)!.click();
    filterBox(node).focus();
    typeFilter(node, "quick");
    await vi.advanceTimersByTimeAsync(POLL_MS);
    expect(document.activeElement).toBe(filterBox(node));
    expect(famHead(node)!.getAttribute("aria-expanded")).toBe("true");
    expect(rows(node)).toHaveLength(1);
    // A focused fold button stays focused through a poll as well.
    typeFilter(node, "");
    famHead(node)!.focus();
    await vi.advanceTimersByTimeAsync(POLL_MS);
    expect(document.activeElement).toBe(famHead(node));
  });
});


describe("the Needs you link (2026-11-06)", () => {
  afterEach(() => resetAttention());

  it("shows its source's count and follows the panel's reads", async () => {
    resetAttention();
    const { context } = stubContext([ok(answer())]);
    const node = await mount(context);
    const link = () => node.querySelector(".needs-link");
    expect(link()?.textContent).toBe("Reading Needs you...");
    setAttention(needOverview([needItem("family-1", { source: "family", kind: "family-over" })]));
    expect(link()?.textContent).toBe("1 in Needs you");
    setAttention(needOverview([]));
    expect(link()?.textContent).toBe("Nothing needs you.");
  });
});
