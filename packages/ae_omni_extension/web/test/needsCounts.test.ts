// One counting rule for the page (2026-11-16, D2 phase 3 fix round 1): an item a working chat holds,
// or one assigned to another admin, is listed but not counted, in every count, pill, cut, Review
// all and fold rule. The server counts the same way; these tests use the page's own fallbacks too.
import type { ExtensionContext } from "@omnigent/extension-sdk";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { type AttentionAssigned, type AttentionOverview, assignedElsewhere, notCounted } from "../src/api";
import { activateAttention, attentionSection, deactivateAttention, resetAttentionSeen } from "../src/attention";
import { resetAttention } from "../src/attentionStore";
import { resetFolds } from "../src/fold";
import { resetAdmins, resetAssignedView } from "../src/needsAssign";
import { resetSelection } from "../src/needsSelect";
import { createNeedsTable } from "../src/needsTable";
import { resetTiles } from "../src/tiles";
import { item, overview } from "./attentionFixtures";

vi.mock("../src/boxHealth", () => ({ openCleanup: vi.fn(() => true) }));

const flush = () => vi.advanceTimersByTimeAsync(0);
const all = (node: ParentNode, sel: string) => [...node.querySelectorAll<HTMLElement>(sel)];
const q = (node: ParentNode, sel: string) => node.querySelector<HTMLElement>(sel);
const ME = "owner@example.com";
const OTHER: AttentionAssigned = { to: "other@example.com", by: ME, at: "2026-11-16T17:00:00Z" };

let table = createNeedsTable({ openTab: vi.fn(), openCard: vi.fn(), hasTab: () => true });

async function mount(list: AttentionOverview): Promise<HTMLElement> {
  const request = vi.fn(async ({ path }: { method: string; path: string }) =>
    path === "/v1/ae/attention" ? { ok: true, status: 200, body: list } : { ok: false, status: 500, body: { error: { code: "x", message: path } } },
  );
  const context = {
    view: "fabric",
    server: { request },
    navigation: { openSession: vi.fn(), openExternal: vi.fn(), openPage: vi.fn() },
  } as unknown as ExtensionContext;
  table = createNeedsTable({ openTab: vi.fn(), openCard: vi.fn(), hasTab: () => true, measure: () => 1000 });
  const node = attentionSection();
  document.getElementById("root")?.appendChild(node);
  activateAttention(context, node, { waiting: false, table: table.hooks });
  await flush();
  return node;
}

beforeEach(() => {
  resetTiles();
  resetAttention();
  resetFolds();
  resetAttentionSeen();
  resetSelection();
  resetAdmins();
  resetAssignedView();
  window.localStorage.clear();
  delete (window as { matchMedia?: unknown }).matchMedia;
  document.body.replaceChildren();
  const root = document.createElement("div");
  root.id = "root";
  document.body.appendChild(root);
  vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout", "Date"] });
  vi.setSystemTime(new Date("2026-11-16T18:00:00Z"));
});

afterEach(() => {
  deactivateAttention();
  vi.useRealTimers();
});

/** Three failing PRs, the first assigned to another admin. */
const THREE = () => {
  const rows = [item("p1", { kind: "pr-failing" }), item("p2", { kind: "pr-failing" }), item("p3", { kind: "pr-failing" })];
  rows[0]!.assigned = OTHER;
  return rows;
};

describe("the rule", () => {
  it("does not count an item assigned to another admin, or held by a working chat, and never one when me is unknown", () => {
    const assigned = item("a", { assigned: OTHER });
    expect(assignedElsewhere(assigned, ME)).toBe(true);
    expect(assignedElsewhere(assigned, OTHER.to)).toBe(false);
    expect(assignedElsewhere(assigned, undefined)).toBe(false);
    expect(notCounted(assigned, ME)).toBe(true);
    expect(notCounted(assigned, undefined)).toBe(false);
    expect(notCounted(item("w", { working: { session_id: "s", title: "t", path: "/x", status: "working" } }), ME)).toBe(true);
    expect(notCounted(item("plain"), ME)).toBe(false);
  });

  it("counts every row when an older server sends no me", async () => {
    const node = await mount(overview(THREE()));
    expect(q(node, ".attn-counts")!.textContent).toBe("3 urgent");
  });
});

describe("counts with an item assigned to another admin", () => {
  it("leaves the tab-scoped header out, from the rows when the answer has no by_source", async () => {
    const list = overview(THREE(), { me: ME });
    list.counts = { urgent: 2, review: 0, by_source: undefined as never };
    const node = await mount(list);
    table.setArea("ship");
    await flush();
    expect(q(node, ".attn-counts")!.textContent).toBe("2 urgent");
    expect(all(node, ".nt-row")).toHaveLength(3); // listed, in place
  });

  it("leaves the source's pills out, with no 'with a chat' count for it", async () => {
    const list = overview(THREE(), { me: ME });
    list.counts = { urgent: 2, review: 0, by_source: undefined as never };
    const node = await mount(list);
    const pills = q(node, ".nt-source .nt-pills")!;
    expect(pills.getAttribute("aria-label")).toBe("2 urgent");
    expect(q(node, ".nt-source .nt-pill-urgent")!.textContent).toBe("2 urgent");
  });

  it("offers Review all for the two it can take, not three", async () => {
    const node = await mount(overview(THREE(), { me: ME }));
    const button = all(node, ".nt-source .attn-review-all").find((b) => /^Review \d+/.test(b.textContent ?? ""))!;
    expect(button.textContent).toBe("Review 2");
  });

  it("counts the cut line against the counted rows only", async () => {
    const list = overview(THREE(), { me: ME, more: 2 });
    // The server counted 4 (two rows were cut to fit); 2 of the 3 listed rows are counted.
    list.counts = { urgent: 4, review: 0, by_source: { git: { urgent: 4, review: 0 } } };
    const node = await mount(list);
    all(node, ".nt-seg")[0]!.click();
    await flush();
    expect(q(node, ".attn-more.meta")!.textContent).toBe("2 more not shown");
  });

  it("does not open a fold, or reopen one, for an urgent item that is another admin's", async () => {
    const rows = [item("p1", { kind: "pr-failing" }), item("b1", { source: "box", kind: "mount", tone: "review", url: "", actions: ["review"] })];
    rows[0]!.assigned = OTHER;
    const node = await mount(overview(rows, { me: ME }));
    const git = () => q(node, '.nt-source[data-source="git"] > .nt-grp-head .nt-grp-btn')!;
    expect(git().getAttribute("aria-expanded")).toBe("false"); // closed by the collapsed first-sight plan
    q(node, '[data-focus="tool:expand"]')!.click();
    await flush();
    expect(git().getAttribute("aria-expanded")).toBe("false"); // no urgent item counts there
  });
});

describe("what the page says when assignments cannot be read", () => {
  it("shows the server's line as a banner that says every item counts", async () => {
    const node = await mount(overview(THREE(), { me: ME, unknown: ["Assignments could not be read"] }));
    const banner = all(node, ".nt-banner").find((b) => b.textContent?.startsWith("Assignments"))!;
    expect(banner.textContent).toBe("Assignments could not be read, so every item counts for you until it can. Reload the page to try again.");
  });

  it("shows the same banner on an area tab, which a source-less line must never hide", async () => {
    const node = await mount(overview(THREE(), { me: ME, unknown: ["Assignments could not be read"] }));
    table.setArea("ship");
    await flush();
    expect(all(node, ".nt-banner").some((b) => b.textContent?.startsWith("Assignments could not be read"))).toBe(true);
    table.setArea("box");
    await flush();
    expect(all(node, ".nt-banner").some((b) => b.textContent?.startsWith("Assignments could not be read"))).toBe(true);
    // A line about a source still shows only on its own area's tab.
    const other = await (async () => {
      deactivateAttention();
      document.body.replaceChildren();
      const root = document.createElement("div");
      root.id = "root";
      document.body.appendChild(root);
      return mount(overview(THREE(), { me: ME, unknown: ["Key Vault could not be read"] }));
    })();
    table.setArea("ship");
    await flush();
    expect(all(other, ".nt-banner")).toHaveLength(0);
  });
});

describe("an assignee who has the item hidden", () => {
  it("leaves it counted for everyone else: the chip stays and `hidden` ends the exclusion", async () => {
    expect(assignedElsewhere(item("a", { assigned: { ...OTHER, hidden: true } }), ME)).toBe(false);
    expect(notCounted(item("a", { assigned: { ...OTHER, hidden: true } }), ME)).toBe(false);
    expect(assignedElsewhere(item("a", { assigned: OTHER }), ME)).toBe(true);
    const rows = THREE();
    rows[0]!.assigned = { ...OTHER, hidden: true };
    const list = overview(rows, { me: ME });
    list.counts = { urgent: 3, review: 0, by_source: undefined as never };
    const node = await mount(list);
    expect(q(node, ".attn-counts")!.textContent).toBe("3 urgent");
    expect(q(node, '[data-id="p1"] .nt-assignee')).not.toBeNull();
    expect(q(node, '[data-id="p1"]')!.classList.contains("is-elsewhere")).toBe(false);
  });
});
