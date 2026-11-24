// The Admin Center's D2 phase 3 page (2026-11-16): Assign in the opened row, the assignee's chip,
// "Assigned to me", a row that is another admin's, and the history words. Every server call is
// mocked; needsPhase2.test.ts and needsTable.test.ts keep covering the earlier phases.
import type { ExtensionContext } from "@omnigent/extension-sdk";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { AttentionOverview } from "../src/api";
import { eventText } from "../src/activityStore";
import { activateAttention, attentionSection, deactivateAttention, resetAttentionSeen } from "../src/attention";
import { resetAttention } from "../src/attentionStore";
import { resetFolds } from "../src/fold";
import { initialsOf, resetAdmins, resetAssignedView } from "../src/needsAssign";
import { eventWords, resetHistories } from "../src/needsMore";
import { resetSelection } from "../src/needsSelect";
import { createNeedsTable } from "../src/needsTable";
import { resetTiles } from "../src/tiles";
import { item, overview } from "./attentionFixtures";

vi.mock("../src/boxHealth", () => ({ openCleanup: vi.fn(() => true) }));

const flush = () => vi.advanceTimersByTimeAsync(0);
const all = (node: ParentNode, sel: string) => [...node.querySelectorAll<HTMLElement>(sel)];
const q = <T extends HTMLElement = HTMLElement>(node: ParentNode, sel: string) => node.querySelector<T>(sel);

type Reply = { ok: boolean; status: number; body: unknown };
const ok = (body: unknown): Reply => ({ ok: true, status: 200, body });
const bad = (status: number, message: string): Reply => ({ ok: false, status, body: { error: { code: "x", message } } });

const ADMINS = ok({
  admins: [
    { id: "owner@example.com", name: "owner@example.com" },
    { id: "other@example.com", name: "other@example.com" },
  ],
});
const ME = "owner@example.com";
const OTHER = { to: "other@example.com", by: ME, at: "2026-11-16T17:00:00Z" };
const MINE = { to: ME, by: "other@example.com", at: "2026-11-16T17:30:00Z" };

function stub(list: AttentionOverview, extra: Record<string, Reply | (() => Reply)> = {}) {
  const request = vi.fn(async ({ method, path }: { method: string; path: string; body?: unknown }) => {
    const key = `${method} ${path}`;
    if (key === "GET /v1/ae/attention") return ok(list);
    const answer = extra[key];
    if (answer) return typeof answer === "function" ? answer() : answer;
    return bad(500, `unexpected ${key}`);
  });
  const context = {
    view: "fabric",
    server: { request },
    navigation: { openSession: vi.fn(), openExternal: vi.fn(), openPage: vi.fn() },
  } as unknown as ExtensionContext;
  return { request, context };
}

const posted = (request: ReturnType<typeof stub>["request"], path: string) =>
  request.mock.calls.filter(([call]) => call.method === "POST" && call.path === path).map(([call]) => call.body);

async function mount(list: AttentionOverview, extra: Record<string, Reply | (() => Reply)> = {}) {
  const table = createNeedsTable({ openTab: vi.fn(), openCard: vi.fn(), hasTab: () => true, measure: () => 1000 });
  const node = attentionSection();
  document.getElementById("root")?.appendChild(node);
  const made = stub(list, extra);
  activateAttention(made.context, node, { waiting: false, table: table.hooks });
  await flush();
  return { node, table, ...made };
}

const open = async (node: HTMLElement, id: string) => {
  q(node, `[data-id="${id}"] .nt-toggle`)!.click();
  await flush();
  await flush();
  return q(node, `[data-id="${id}"]`)!;
};

beforeEach(() => {
  resetTiles();
  resetAttention();
  resetFolds();
  resetAttentionSeen();
  resetSelection();
  resetHistories();
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

const ROWS = () => [
  item("a", { title: "Alpha", actions: ["review", "dismiss"] }),
  item("b", { title: "Bravo", actions: ["review", "dismiss"] }),
  item("c", { title: "Charlie", actions: ["review", "dismiss"] }),
];
const withAssigned = (rows = ROWS()) => {
  rows[0]!.assigned = OTHER;
  rows[2]!.assigned = MINE;
  return rows;
};

describe("the Assign picker in the opened row", () => {
  it("lists the admins from the route and assigns the chosen one", async () => {
    const { node, request } = await mount(overview(ROWS(), { me: ME }), {
      "GET /v1/ae/attention/admins": ADMINS,
      "POST /v1/ae/attention/assign": ok({ id: "a", assigned: OTHER }),
    });
    const row = await open(node, "a");
    const block = q(row, ".nt-assign")!;
    expect(block.getAttribute("aria-label")).toBe("Assign Alpha");
    q(block, ".nt-assign-toggle")!.click();
    const select = q(row, ".nt-assign-select") as HTMLSelectElement;
    expect(all(select, "option").map((o) => [o.getAttribute("value"), o.textContent])).toEqual([
      ["", "Choose an admin"],
      ["owner@example.com", "owner@example.com (you)"],
      ["other@example.com", "other@example.com"],
    ]);
    // Nothing chosen: nothing is sent.
    q(row, ".nt-assign .btn-primary")!.click();
    await flush();
    expect(posted(request, "/v1/ae/attention/assign")).toEqual([]);
    expect(q(row, ".nt-assign .flash")!.textContent).toBe("Choose an admin first.");
    expect(q(row, ".nt-assign-select + .btn-primary + button")).toBeNull(); // no Unassign yet
    select.value = "other@example.com";
    q(row, ".nt-assign .btn-primary")!.click();
    await flush();
    await flush();
    expect(posted(request, "/v1/ae/attention/assign")).toEqual([{ id: "a", to: "other@example.com" }]);
    // The admin list was asked once for the whole page's session of this row.
    expect(request.mock.calls.filter(([c]) => c.path === "/v1/ae/attention/admins")).toHaveLength(1);
  });

  it("offers Unassign when the item is assigned, and says who it is assigned to", async () => {
    const rows = ROWS();
    rows[0]!.assigned = OTHER;
    const { node, request } = await mount(overview(rows, { me: ME }), {
      "GET /v1/ae/attention/admins": ADMINS,
      "POST /v1/ae/attention/assign": ok({ id: "a", assigned: null }),
    });
    const row = await open(node, "a");
    expect(q(row, ".nt-assign-now")!.textContent).toContain("other");
    q(row, ".nt-assign-toggle")!.click();
    const select = q(row, ".nt-assign-select") as HTMLSelectElement;
    expect(select.value).toBe("other@example.com"); // the current assignee is preselected
    const off = all(row, ".nt-assign button").find((b) => b.textContent === "Unassign")!;
    off.click();
    await flush();
    await flush();
    expect(posted(request, "/v1/ae/attention/assign")).toEqual([{ id: "a", to: null }]);
  });

  it("is hidden for a personal Git item and for a viewer who cannot chat (a box admin)", async () => {
    const mine = overview([item("m", { source: "git-mine", actions: ["dismiss"] })], { me: ME });
    const first = await mount(mine, { "GET /v1/ae/attention/admins": ADMINS });
    expect(q(await open(first.node, "m"), ".nt-assign")).toBeNull();
    expect(first.request.mock.calls.some(([c]) => c.path === "/v1/ae/attention/admins")).toBe(false);
    deactivateAttention();
    document.body.replaceChildren();
    const root = document.createElement("div");
    root.id = "root";
    document.body.appendChild(root);
    const second = await mount(overview(ROWS(), { me: ME, can_chat: false }), { "GET /v1/ae/attention/admins": ADMINS });
    expect(q(await open(second.node, "a"), ".nt-assign")).toBeNull();
    expect(q(second.node, ".nt-view-chip")).toBeNull(); // no "Assigned to me" either
    expect(second.request.mock.calls.some(([c]) => c.path === "/v1/ae/attention/admins")).toBe(false);
  });

  it("says so when the admins cannot be read, and offers no picker", async () => {
    const { node } = await mount(overview(ROWS(), { me: ME }), {
      "GET /v1/ae/attention/admins": bad(503, "The list of OmniGent admins could not be read"),
    });
    const row = await open(node, "a");
    const line = q(row, ".nt-assign .nt-assign-bad")!;
    expect(line.textContent).toMatch(/list of admins could not be read/);
    expect(q(row, ".nt-assign-toggle")).toBeNull();
    expect(q(row, ".nt-assign-select")).toBeNull();
  });

  it("shows the server's words when an assign is refused", async () => {
    const { node } = await mount(overview(ROWS(), { me: ME }), {
      "GET /v1/ae/attention/admins": ADMINS,
      "POST /v1/ae/attention/assign": bad(422, "'x' is not a current OmniGent admin. Pick a name from the picker."),
    });
    const row = await open(node, "a");
    q(row, ".nt-assign-toggle")!.click();
    (q(row, ".nt-assign-select") as HTMLSelectElement).value = "other@example.com";
    q(row, ".nt-assign .btn-primary")!.click();
    await flush();
    await flush();
    // The result goes to the opened row's message line (full width), not into the picker.
    expect(q(row, ".nt-panel-msg")!.textContent).toMatch(/Could not assign .*not a current OmniGent admin/);
    expect(q(row, ".nt-assign .flash")).toBeNull();
  });
});

describe("the assignee's chip and a row that is another admin's", () => {
  it("shows initials with the full id as the accessible name and tooltip", async () => {
    const { node } = await mount(overview(withAssigned(), { me: ME }));
    const chip = q(node, '[data-id="a"] .nt-assignee')!;
    expect(chip.textContent).toBe("O");
    expect(chip.getAttribute("aria-label")).toBe("Assigned to other@example.com");
    expect(chip.getAttribute("title")).toBe("Assigned to other@example.com");
    expect(chip.getAttribute("role")).toBe("img");
    const mine = q(node, '[data-id="c"] .nt-assignee')!;
    expect(mine.getAttribute("aria-label")).toBe("Assigned to you (owner@example.com)");
    expect(mine.classList.contains("is-me")).toBe(true);
    expect(q(node, '[data-id="b"] .nt-assignee')).toBeNull();
  });

  it("dims only a row assigned to someone else, and every row keeps its place", async () => {
    const rows = ROWS();
    const before = await mount(overview(rows, { me: ME }));
    const order = all(before.node, ".nt-row").map((r) => r.dataset.id);
    deactivateAttention();
    document.body.replaceChildren();
    const root = document.createElement("div");
    root.id = "root";
    document.body.appendChild(root);
    const after = await mount(overview(withAssigned(ROWS()).map((r, i) => ({ ...r, id: rows[i]!.id })), { me: ME }));
    expect(all(after.node, ".nt-row").map((r) => r.dataset.id)).toEqual(order);
    expect(q(after.node, '[data-id="a"]')!.classList.contains("is-elsewhere")).toBe(true);
    expect(q(after.node, '[data-id="b"]')!.classList.contains("is-elsewhere")).toBe(false);
    expect(q(after.node, '[data-id="c"]')!.classList.contains("is-elsewhere")).toBe(false);
  });

  it("leaves the counts as the server says: an item assigned elsewhere is listed but not counted", async () => {
    const rows = withAssigned();
    const list = overview(rows, { me: ME });
    // The server's rule: the item assigned to another admin leaves the counts.
    list.counts = { urgent: 2, review: 0, by_source: { git: { urgent: 2, review: 0 } } };
    const { node } = await mount(list);
    expect(all(node, ".nt-row")).toHaveLength(3);
    expect(q(node, ".attn-counts")!.textContent).toMatch(/2 urgent/);
  });
});

describe('"Assigned to me"', () => {
  it("is one chip: a toggle button that counts the items assigned to the viewer and filters to them", async () => {
    const { node } = await mount(overview(withAssigned(), { me: ME }));
    expect(q(node, ".nt-view")).toBeNull(); // the old two-button group is gone (2026-11-18)
    const chip = q<HTMLButtonElement>(node, ".nt-view-chip")!;
    expect(chip.textContent).toBe("Assigned to me1");
    expect(q(chip, ".nt-view-n")!.textContent).toBe("1");
    expect(chip.getAttribute("aria-pressed")).toBe("false");
    expect(q(chip, ".icon-square")).not.toBeNull(); // an empty box when off
    expect(all(node, ".nt-row")).toHaveLength(3);
    chip.click();
    await flush();
    expect(all(node, ".nt-row").map((r) => r.dataset.id)).toEqual(["c"]);
    const on = q<HTMLButtonElement>(node, ".nt-view-chip")!;
    expect(on.classList.contains("is-on")).toBe(true);
    expect(on.getAttribute("aria-pressed")).toBe("true");
    expect(q(on, ".icon-square-check")).not.toBeNull(); // a ticked box when on
    on.click(); // a second click turns it off
    await flush();
    expect(all(node, ".nt-row")).toHaveLength(3);
    expect(q(node, ".nt-view-chip")!.getAttribute("aria-pressed")).toBe("false");
  });

  it("keeps its normal look and still toggles while a source filter is on (turned on through the strip)", async () => {
    const { node } = await mount(overview(withAssigned(), { me: ME }));
    const seg = q<HTMLButtonElement>(node, ".nt-seg")!;
    seg.click(); // the strip's source filter, as a user turns it on
    await flush();
    expect(q(node, ".nt-seg.is-on")).not.toBeNull(); // the filter is on, and the other segments are dimmed
    const chip = q<HTMLButtonElement>(node, ".nt-view-chip")!;
    expect(chip.disabled).toBe(false);
    expect(chip.hasAttribute("aria-disabled")).toBe(false);
    expect(chip.classList.contains("is-off")).toBe(false); // the strip's dim class never lands on the chip
    expect(chip.className).toBe("nt-view-chip");
    expect(chip.getAttribute("aria-pressed")).toBe("false");
    chip.click();
    await flush();
    const on = q<HTMLButtonElement>(node, ".nt-view-chip")!;
    expect(on.getAttribute("aria-pressed")).toBe("true");
    expect(on.disabled).toBe(false);
    expect(on.className).toBe("nt-view-chip is-on");
    on.click(); // and off again, with the source filter still on
    await flush();
    expect(q(node, ".nt-view-chip")!.getAttribute("aria-pressed")).toBe("false");
    expect(q(node, ".nt-seg.is-on")).not.toBeNull();
  });

  it("shows no count when nothing is assigned to the viewer", async () => {
    const { node } = await mount(overview(ROWS(), { me: ME }));
    const chip = q(node, ".nt-view-chip")!;
    expect(chip.textContent).toBe("Assigned to me");
    expect(q(chip, ".nt-view-n")).toBeNull();
  });

  it("is remembered for the viewer and says so when nothing is assigned to them", async () => {
    const first = await mount(overview(ROWS(), { me: ME }));
    q<HTMLButtonElement>(first.node, ".nt-view-chip")!.click();
    await flush();
    expect(q(first.node, ".attn-empty")!.textContent).toBe("Nothing is assigned to you.");
    expect(window.localStorage.getItem(`needs.assigned.${ME}`)).toBe("on");
    deactivateAttention();
    document.body.replaceChildren();
    const root = document.createElement("div");
    root.id = "root";
    document.body.appendChild(root);
    resetAssignedView(); // a new page load reads the stored choice, not memory
    const again = await mount(overview(withAssigned(), { me: ME }));
    expect(all(again.node, ".nt-row").map((r) => r.dataset.id)).toEqual(["c"]);
    // Another person on the same browser has their own choice.
    expect(window.localStorage.getItem("needs.assigned.other@example.com")).toBeNull();
  });
});

describe("assigned and unassigned in the words of the page", () => {
  it("writes the history box rows with who, to whom and when the system ended it", async () => {
    const { node } = await mount(overview(ROWS(), { me: ME }), {
      "GET /v1/ae/attention/admins": ADMINS,
      "GET /v1/ae/attention/history?id=a": ok({
        id: "a",
        first_seen: "2026-11-15T09:00:00Z",
        events: [
          { at: "2026-11-15T09:00:00Z", what: "first-seen", by: null },
          { at: "2026-11-16T09:00:00Z", what: "assigned", by: "alice@example.com", to: "bob@example.com" },
          { at: "2026-11-16T10:00:00Z", what: "unassigned", by: "alice@example.com" },
          { at: "2026-11-16T11:00:00Z", what: "unassigned", by: "system" },
          { at: "2026-11-16T11:00:00Z", what: "done", by: null },
          { at: "2026-11-16T12:00:00Z", what: "back", by: null },
        ],
      }),
    });
    const row = await open(node, "a");
    const rows = all(row, ".nt-hist-row");
    expect(rows.map((r) => r.textContent!.replace(/\s+\S.*\d{1,2}:\d{2}.*$/, ""))).toHaveLength(6);
    expect(all(row, ".nt-hist-what").map((n) => n.textContent)).toEqual([
      "First seen",
      "Assigned to bob",
      "Unassigned",
      "Unassigned (the item left the list)",
      "No longer reported",
      "Reported again",
    ]);
    expect(all(row, ".nt-hist-by").map((n) => n.textContent)).toEqual([" by alice", " by alice"]);
  });

  it("writes the Done log's lines for assigned and unassigned", () => {
    const base = { id: "a", source: "git" as const, at: "2026-11-16T09:00:00Z", title: "Bump y" };
    expect(eventText({ ...base, what: "assigned", by: "alice@example.com", to: "bob@example.com" }, "Git repos")).toBe(
      "alice assigned Bump y to bob",
    );
    expect(eventText({ ...base, what: "unassigned", by: "alice@example.com" }, "Git repos")).toBe("alice unassigned Bump y");
    expect(eventText({ ...base, what: "unassigned", by: "system" }, "Git repos")).toBe(
      "Bump y was unassigned because it left the list",
    );
    expect(eventWords({ what: "assigned", by: "a", to: undefined })).toBe("Assigned");
  });

  it("takes initials from a login", () => {
    expect(initialsOf("ann.lee@example.com")).toBe("AL");
    expect(initialsOf("alice@example.com")).toBe("A");
    expect(initialsOf("ann_lee")).toBe("AL");
    expect(initialsOf("local")).toBe("L");
  });
});
