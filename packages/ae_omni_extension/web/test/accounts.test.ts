// The Admin Center's Accounts card (2026-10-21): every signed-in account's
// 5-hour and weekly use. Every call is mocked: the route is
// GET /v1/ae/limits/accounts.
import type { ExtensionContext } from "@omnigent/extension-sdk";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  accountRow,
  accountsSection,
  accountsTile,
  activateAccounts,
  ageText,
  deactivateAccounts,
  fullest,
  liveText,
  POLL_MS,
  resetText,
} from "../src/accounts";
import type { AccountRow, AccountsAnswer, AccountWindow } from "../src/api";
import { getTile, resetTiles } from "../src/tiles";

const sdk = vi.hoisted(() => ({ defineExtension: vi.fn() }));
vi.mock("@omnigent/extension-sdk", () => sdk);

type Reply = { ok: boolean; status: number; body: unknown };

const GET = "GET /v1/ae/limits/accounts";
const NOW = Date.parse("2026-10-21T15:00:00Z");
const HOUR = 3_600_000;

function win(id: string, used: number, minutes: number, resetsIn = 2 * HOUR): AccountWindow {
  return {
    id,
    label: id,
    used_percent: used,
    resets_at: new Date(NOW + resetsIn).toISOString(),
    window_minutes: minutes,
  };
}

function row(overrides: Partial<AccountRow> = {}): AccountRow {
  return {
    harness: "claude",
    account: null,
    label: "AE Default",
    plan: "Max",
    five_hour: win("five_hour", 38, 300),
    weekly: win("seven_day", 61, 10080, 3 * 24 * HOUR),
    observed_at: new Date(NOW - 3 * HOUR).toISOString(),
    live_sessions: 2,
    ...overrides,
  };
}

const unread = (label: string, account: string): AccountRow =>
  row({
    account,
    label,
    plan: null,
    five_hour: null,
    weekly: null,
    observed_at: null,
    live_sessions: 0,
  });

const codex = (): AccountRow =>
  row({
    harness: "codex",
    label: "Codex",
    plan: "pro",
    five_hour: win("codex.primary", 12, 300),
    weekly: win("codex.secondary", 92, 10080),
    observed_at: new Date(NOW - 5 * 60_000).toISOString(),
    live_sessions: null,
  });

const answer = (accounts: AccountRow[]): AccountsAnswer => ({
  now: new Date(NOW).toISOString(),
  accounts,
});

const ok = (body: unknown): Reply => ({ ok: true, status: 200, body });
const failed = (code: number, message: string): Reply => ({
  ok: false,
  status: code,
  body: { error: { code: "x", message } },
});

type Call = { method: string; path: string };

function stubContext(replies: Reply[]) {
  const request = vi.fn(async ({ method, path }: Call) => {
    if (`${method} ${path}` !== GET) return failed(500, `unexpected ${method} ${path}`);
    return replies.shift() ?? failed(500, "no reply queued");
  });
  const context = { view: "fabric", server: { request } } as unknown as ExtensionContext;
  return { context, request };
}

const flush = () => vi.advanceTimersByTimeAsync(0);

async function mount(context: ExtensionContext): Promise<HTMLElement> {
  const node = accountsSection();
  document.getElementById("root")?.appendChild(node);
  activateAccounts(context, node);
  await flush();
  return node;
}

const labels = (node: ParentNode) =>
  [...node.querySelectorAll(".acct-label")].map((item) => item.textContent);
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
  deactivateAccounts();
  resetTiles();
  vi.useRealTimers();
  vi.restoreAllMocks();
});

describe("the words", () => {
  it("says how old a reading is, or that there is none", () => {
    expect(ageText(null, NOW)).toBe("no reading yet");
    expect(ageText("not a date", NOW)).toBe("no reading yet");
    expect(ageText(new Date(NOW - 20_000).toISOString(), NOW)).toBe("as of just now");
    expect(ageText(new Date(NOW - 12 * 60_000).toISOString(), NOW)).toBe("as of 12m ago");
    expect(ageText(new Date(NOW - 3 * HOUR).toISOString(), NOW)).toBe("as of 3h ago");
    expect(ageText(new Date(NOW - 50 * HOUR).toISOString(), NOW)).toBe("as of 2d ago");
  });

  it("names the reset, or says the window has reset since the reading", () => {
    expect(resetText(win("five_hour", 1, 300), NOW)).toMatch(/^resets /);
    expect(resetText(win("five_hour", 1, 300, -60_000), NOW)).toBe("reset since this reading");
    expect(resetText({ ...win("five_hour", 1, 300), resets_at: null }, NOW)).toBe("");
  });

  it("counts live sessions only where they are counted", () => {
    expect(liveText(row())).toBe("2 live sessions");
    expect(liveText(row({ live_sessions: 1 }))).toBe("1 live session");
    expect(liveText(row({ live_sessions: 0 }))).toBe("No live session");
    expect(liveText(codex())).toBe("");
  });

  it("finds the fullest window, skipping one that has reset since", () => {
    const top = fullest([row(), codex()], NOW);
    expect([top?.row.label, top?.name, top?.percent]).toEqual(["Codex", "weekly", 92]);
    const reset = codex();
    reset.weekly = win("codex.secondary", 99, 10080, -60_000);
    expect(fullest([reset], NOW)?.percent).toBe(12);
    expect(fullest([unread("Mine", "Personal")], NOW)).toBeNull();
  });

  it("colours the tile by the fullest window: 70 amber, 90 red", () => {
    expect(accountsTile([row()], NOW).health).toBe("ok");
    expect(accountsTile([row({ five_hour: win("five_hour", 70, 300) })], NOW).health).toBe(
      "attention",
    );
    const near = accountsTile([row(), codex()], NOW);
    expect(near).toMatchObject({
      value: "2",
      health: "problem",
      summary: "Codex 92% weekly",
    });
    expect(near.note).toMatch(/Manage in Settings$/);
    expect(near.title).toMatch(/Settings > Claude accounts/);
    expect(accountsTile([unread("Mine", "Personal")], NOW)).toMatchObject({
      health: "idle",
      summary: "1 account, no reading yet",
    });
  });
});

describe("a row", () => {
  it("draws the label, plan, both bars, live sessions and the age", () => {
    const node = accountRow(row(), NOW);
    expect(node.querySelector(".acct-label")?.textContent).toBe("AE Default");
    expect(node.querySelector(".acct-plan")?.textContent).toBe("Max");
    const bars = [...node.querySelectorAll<HTMLElement>(".acct-bar")];
    expect(bars.map((bar) => bar.querySelector(".acct-bar-value")?.textContent)).toEqual([
      "38%",
      "61%",
    ]);
    expect(bars.map((bar) => bar.getAttribute("aria-valuenow"))).toEqual(["38", "61"]);
    expect(bars[0]?.getAttribute("aria-label")).toMatch(/^5-hour: 38% used, resets /);
    expect(node.querySelector<HTMLElement>(".acct-fill")?.style.width).toBe("38%");
    expect(node.querySelector(".acct-meta")?.textContent).toBe("2 live sessions · as of 3h ago");
  });

  it("uses the 70 and 90 steps on each bar, and marks a window that has reset", () => {
    const tones = (r: AccountRow) =>
      [...accountRow(r, NOW).querySelectorAll(".acct-bar")].map((bar) => bar.className);
    expect(
      tones(row({ five_hour: win("five_hour", 69, 300), weekly: win("seven_day", 70, 10080) })),
    ).toEqual(["acct-bar acct-ok", "acct-bar acct-warn"]);
    expect(
      tones(row({ five_hour: win("five_hour", 90, 300), weekly: win("seven_day", 1, 10080, -1) })),
    ).toEqual(["acct-bar acct-err", "acct-bar acct-reset"]);
    // Over 100 (a spend limit past its cap) still fills the bar to the end only.
    const over = accountRow(row({ five_hour: win("five_hour", 140, 300) }), NOW);
    expect(over.querySelector<HTMLElement>(".acct-fill")?.style.width).toBe("100%");
  });

  it("draws only the window a plan has, and No reading when a read row has neither", () => {
    const weeklyOnly = accountRow(row({ five_hour: null }), NOW);
    expect(
      [...weeklyOnly.querySelectorAll(".acct-bar-name")].map((name) => name.textContent),
    ).toEqual(["Weekly"]);
    expect(weeklyOnly.textContent).not.toContain("No reading");
    const none = accountRow(row({ five_hour: null, weekly: null }), NOW);
    expect([...none.querySelectorAll(".acct-bar-none")]).toHaveLength(2);
    expect(none.textContent).toContain("No reading");
  });

  it("says no reading yet for a signed-in account the relay has not read", () => {
    const node = accountRow(unread("Mine", "Personal"), NOW);
    expect(node.classList.contains("acct-unread")).toBe(true);
    expect(node.querySelector(".acct-bars")).toBeNull();
    expect(node.querySelector(".acct-plan")).toBeNull();
    expect(node.querySelector(".acct-meta")?.textContent).toBe("No live session · no reading yet");
  });
});

describe("the section", () => {
  it("groups the rows Claude then Codex and hands the page its tile", async () => {
    const { context } = stubContext([ok(answer([codex(), row(), unread("Mine", "Personal")]))]);
    const node = await mount(context);
    const groups = [...node.querySelectorAll<HTMLElement>(".acct-group")];
    expect(groups.map((group) => group.dataset.group)).toEqual(["claude", "codex"]);
    expect(groups.map((group) => group.querySelector(".acct-group-h")?.textContent)).toEqual([
      "Claude",
      "Codex",
    ]);
    expect(labels(node)).toEqual(["AE Default", "Mine", "Codex"]);
    expect(node.querySelector(".accounts-refreshed")?.textContent).toMatch(/^Updated /);
    expect(getTile("accounts")).toMatchObject({ value: "3", health: "problem" });
  });

  it("leaves out a group with no account", async () => {
    const { context } = stubContext([ok(answer([row()]))]);
    const node = await mount(context);
    expect(node.querySelector('[data-group="codex"]')).toBeNull();
  });

  it("reads again every minute and on focus", async () => {
    const { context, request } = stubContext([
      ok(answer([row()])),
      ok(answer([row({ five_hour: win("five_hour", 75, 300) })])),
      ok(answer([row()])),
    ]);
    const node = await mount(context);
    expect(request).toHaveBeenCalledTimes(1);
    await vi.advanceTimersByTimeAsync(POLL_MS);
    expect(request).toHaveBeenCalledTimes(2);
    expect(node.querySelector(".acct-bar-value")?.textContent).toBe("75%");
    window.dispatchEvent(new Event("focus"));
    await flush();
    expect(request).toHaveBeenCalledTimes(3);
  });

  it("keeps the last good rows and marks the tile when a read fails", async () => {
    const { context } = stubContext([ok(answer([row()])), failed(502, "upstream down")]);
    const node = await mount(context);
    window.dispatchEvent(new Event("focus"));
    await flush();
    expect(labels(node)).toEqual(["AE Default"]);
    expect(notices(node)).toHaveLength(1);
    expect(notices(node)[0]).toMatch(/^The accounts could not be read/);
    const tile = getTile("accounts");
    expect(tile?.note).toMatch(/last answer$/);
    expect(tile?.health).toBe("attention");
  });

  it("does not say the same failure twice", async () => {
    const { context } = stubContext([failed(500, "boom"), failed(500, "boom")]);
    const node = await mount(context);
    const first = node.querySelector(".notice");
    expect(first?.textContent).toMatch(/^The accounts could not be read/);
    window.dispatchEvent(new Event("focus"));
    await flush();
    // Redrawing the alert would make a screen reader announce it again.
    expect(node.querySelector(".notice")).toBe(first);
    expect(getTile("accounts")).toMatchObject({ value: "Unknown", health: "attention" });
  });

  it("says so when no account has signed in", async () => {
    const { context } = stubContext([ok(answer([]))]);
    const node = await mount(context);
    expect(node.querySelector(".accounts-list")?.textContent).toBe("No account has signed in yet.");
  });

  it("stops reading once deactivated", async () => {
    const { context, request } = stubContext([ok(answer([row()])), ok(answer([row()]))]);
    await mount(context);
    deactivateAccounts();
    window.dispatchEvent(new Event("focus"));
    await vi.advanceTimersByTimeAsync(2 * POLL_MS);
    expect(request).toHaveBeenCalledTimes(1);
  });
});
