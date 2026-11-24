// The Admin Center's D2 phase 2 page (2026-11-16): picking rows and the bulk bar, Snooze from the
// row and the Snoozed fold, the history box, the Age column, the Activity tab, Done today in the
// rail and a Social row's picture. Every server call is mocked; needsTable.test.ts and
// attention.test.ts keep covering phase 1.
import type { ExtensionContext } from "@omnigent/extension-sdk";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { AttentionOverview, DoneAnswer } from "../src/api";
import { activateAttention, attentionSection, deactivateAttention, resetAttentionSeen } from "../src/attention";
import { resetAttention, setAttention, setFilter } from "../src/attentionStore";
import { activityDays, dayKey, dayLabel, doneToday, eventText, getActivity, loadActivity, resetActivity, unreadable } from "../src/activityStore";
import { DONE_MEANS, doneTodayBlock, drawActivity, RAIL_DONE_MAX } from "../src/activityPanel";
import { resetFolds, saveFold } from "../src/fold";
import { resetHistories } from "../src/needsMore";
import { tabsFor } from "../src/needsModel";
import { createNeedsTable } from "../src/needsTable";
import css from "../src/style.css?raw";
import {
  bulkWords,
  chunks,
  clearPicked,
  groupState,
  isPicked,
  joinAnswers,
  keepOnly,
  pickedIds,
  planBulk,
  pickedCount,
  resetSelection,
  setPicked,
  snoozedLine,
  untilText,
} from "../src/needsSelect";
import { resetTiles } from "../src/tiles";
import { item, overview } from "./attentionFixtures";

vi.mock("../src/boxHealth", () => ({ openCleanup: vi.fn(() => true) }));
// The Social card's picture route is covered in its own tests; here only the states matter.
const visual = vi.hoisted(() => ({ state: "ready" as string, blob: null as Blob | null, load: vi.fn() }));
vi.mock("../src/socialVisual", () => ({
  visualState: () => visual.state,
  loadedVisual: () => visual.blob,
  visualUrl: () => (visual.blob ? "blob:picture-1" : null),
  showPictureUrl: () => (visual.blob ? "blob:picture-1" : null),
  revokePictureUrls: () => undefined,
  canCopyPicture: () => false,
  copyPicture: async () => null,
  downloadPicture: () => true,
  loadVisual: (...args: unknown[]) => {
    visual.load(...args);
    return Promise.resolve(visual.blob);
  },
}));

const flush = () => vi.advanceTimersByTimeAsync(0);
const all = (node: ParentNode, sel: string) => [...node.querySelectorAll<HTMLElement>(sel)];
const q = (node: ParentNode, sel: string) => node.querySelector<HTMLElement>(sel);

type Reply = { ok: boolean; status: number; body: unknown };
const ok = (body: unknown): Reply => ({ ok: true, status: 200, body });

/** A server that answers the list and whatever else the test queues by "METHOD path". */
function stub(list: AttentionOverview, extra: Record<string, Reply | (() => Reply)> = {}) {
  const request = vi.fn(async ({ method, path }: { method: string; path: string; body?: unknown }) => {
    const key = `${method} ${path}`;
    if (key === "GET /v1/ae/attention") return ok(list);
    const answer = extra[key];
    if (answer) return typeof answer === "function" ? answer() : answer;
    return { ok: false, status: 500, body: { error: { code: "unexpected", message: `unexpected ${key}` } } };
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

const openCard = vi.fn();
async function mount(list: AttentionOverview, extra: Record<string, Reply | (() => Reply)> = {}) {
  const table = createNeedsTable({ openTab: vi.fn(), openCard, hasTab: () => true, measure: () => 1000 });
  const node = attentionSection();
  document.getElementById("root")?.appendChild(node);
  const made = stub(list, extra);
  activateAttention(made.context, node, { waiting: false, table: table.hooks });
  await flush();
  return { node, table, ...made };
}

beforeEach(() => {
  resetTiles();
  resetAttention();
  resetFolds();
  resetAttentionSeen();
  resetSelection();
  resetActivity();
  resetHistories();
  visual.state = "ready";
  visual.blob = null;
  visual.load.mockClear();
  window.localStorage.clear();
  delete (window as { matchMedia?: unknown }).matchMedia;
  document.body.replaceChildren();
  const root = document.createElement("div");
  root.id = "root";
  document.body.appendChild(root);
  vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout", "Date"] });
  vi.setSystemTime(new Date("2026-11-16T18:00:00Z"));
  openCard.mockClear();
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
const pick = async (node: HTMLElement, id: string) => {
  q(node, `input[data-pick="${id}"]`)!.click();
  await flush();
};
const bar = (node: HTMLElement) => q(node, ".nt-bulk")!;
const barButton = (node: HTMLElement, name: RegExp) => all(bar(node), "button").find((b) => name.test(b.textContent ?? ""))!;

describe("picking rows", () => {
  it("gives each row a native checkbox in a label that names its item, and no bar until something is picked", async () => {
    const { node } = await mount(overview(ROWS()));
    const boxes = all(node, "input[data-pick]");
    expect(boxes.map((b) => b.getAttribute("type"))).toEqual(["checkbox", "checkbox", "checkbox"]);
    expect(all(node, "label.nt-pick .sr-only").map((n) => n.textContent)).toContain("Select Alpha");
    expect(bar(node).hidden).toBe(true);
    expect(bar(node).getAttribute("role")).toBe("region");
    expect(bar(node).getAttribute("aria-label")).toBe("Selected items");
    expect(q(node, ".nt-bulk-status")!.getAttribute("aria-live")).toBe("polite");
  });

  it("shows N selected with Dismiss, Snooze, Review N and Clear, and Clear empties it", async () => {
    const { node } = await mount(overview(ROWS()));
    await pick(node, "a");
    await pick(node, "b");
    expect(bar(node).hidden).toBe(false);
    expect(q(bar(node), ".nt-bulk-n")!.textContent).toBe("2 selected");
    expect(all(bar(node), "button").map((b) => b.textContent)).toEqual(["Dismiss", "Snooze", "Review 2", "Clear"]);
    barButton(node, /Clear/).click();
    await flush();
    expect(bar(node).hidden).toBe(true);
    expect(all(node, "input[data-pick]").some((b) => (b as HTMLInputElement).checked)).toBe(false);
  });

  it("hides Review N for someone who cannot start chats", async () => {
    const { node } = await mount(overview(ROWS(), { can_chat: false }));
    await pick(node, "a");
    expect(all(bar(node), "button").map((b) => b.textContent)).toEqual(["Dismiss", "Snooze", "Clear"]);
  });

  it("offers a personal Git row Dismiss only, and says so", async () => {
    const mine = item("m", { source: "git-mine", title: "Mine", actions: ["dismiss"] });
    const { node } = await mount(overview([item("a", { title: "Alpha" }), mine]));
    await pick(node, "m");
    expect(all(bar(node), "button").map((b) => b.textContent)).toEqual(["Dismiss", "Clear"]);
    expect(q(bar(node), ".nt-bulk-note")!.textContent).toContain("Dismiss only");
    await pick(node, "a");
    expect(all(bar(node), "button").map((b) => b.textContent)).toEqual(["Dismiss", "Snooze", "Review 1", "Clear"]);
  });

  it("shows a select-all per source and area, mixed when only some rows are picked", async () => {
    // The Overview opens collapsed (2026-11-17): the person opened the source so its rows are drawn.
    saveFold("attn.git", true);
    const { node } = await mount(overview(ROWS()));
    const group = q(node, '[data-source="git"] input[data-pick-group]') as HTMLInputElement;
    expect(group.checked).toBe(false);
    await pick(node, "a");
    expect(group.indeterminate).toBe(true);
    group.click();
    await flush();
    expect(group.checked).toBe(true);
    expect(group.indeterminate).toBe(false);
    expect(pickedIds().sort()).toEqual(["a", "b", "c"]);
    const area = q(node, '[data-area="ship"] > .nt-grp-head input[data-pick-group]') as HTMLInputElement;
    expect(area.checked).toBe(true);
    group.click();
    await flush();
    expect(pickedIds()).toEqual([]);
  });

  it("keeps the selection through a redraw and drops an id that left the list", async () => {
    let list = overview(ROWS());
    const { node, context } = await mount(list);
    await pick(node, "a");
    await pick(node, "b");
    list = overview(ROWS().filter((row) => row.id !== "b"));
    (context.server.request as unknown as ReturnType<typeof vi.fn>).mockImplementation(async () => ok(list));
    window.dispatchEvent(new Event("focus"));
    await flush();
    await flush();
    expect(pickedIds()).toEqual(["a"]);
    expect((q(node, 'input[data-pick="a"]') as HTMLInputElement).checked).toBe(true);
    expect(q(bar(node), ".nt-bulk-n")!.textContent).toBe("1 selected");
  });
});

describe("a bulk dismiss", () => {
  const answer = (results: Array<{ id: string; ok: boolean; status: number; error?: string }>) =>
    ok({ results, succeeded: results.filter((r) => r.ok).length, failed: results.filter((r) => !r.ok).length });

  it("says what happened per item, with the reasons, and keeps the failed ones selected", async () => {
    const { node, request } = await mount(overview(ROWS()), {
      "POST /v1/ae/attention/bulk": answer([
        { id: "a", ok: true, status: 200 },
        { id: "b", ok: false, status: 409, error: "A stale reading cannot be dismissed." },
        { id: "c", ok: false, status: 404, error: "No longer on the list." },
      ]),
    });
    for (const id of ["a", "b", "c"]) await pick(node, id);
    barButton(node, /Dismiss/).click();
    await flush();
    await flush();
    expect(posted(request, "/v1/ae/attention/bulk")).toEqual([{ action: "dismiss", ids: ["a", "b", "c"] }]);
    expect(q(node, ".nt-bulk-line")!.textContent).toBe("Dismissed 1, 2 could not be dismissed");
    expect(all(node, ".nt-bulk-why li").map((li) => li.textContent)).toEqual([
      "A stale reading cannot be dismissed.",
      "No longer on the list.",
    ]);
    expect(pickedIds().sort()).toEqual(["b", "c"]);
    expect(bar(node).hidden).toBe(false);
  });

  it("still says 'Selection cleared' when Clear is pressed while a partial result note is up", async () => {
    const { node } = await mount(overview(ROWS()), {
      "POST /v1/ae/attention/bulk": answer([
        { id: "a", ok: true, status: 200 },
        { id: "b", ok: false, status: 409, error: "A stale reading cannot be dismissed." },
      ]),
    });
    for (const id of ["a", "b"]) await pick(node, id);
    barButton(node, /Dismiss/).click();
    await flush();
    await flush();
    expect(pickedIds()).toEqual(["b"]);
    barButton(node, /Clear/).click();
    await flush();
    expect(q(node, ".nt-bulk-status")!.textContent).toBe("Selection cleared");
  });

  it("reads the list again after it, and an all-failed request says it could not", async () => {
    const { node, request } = await mount(overview(ROWS()), {
      "POST /v1/ae/attention/bulk": { ok: false, status: 503, body: { error: { code: "attention_dismissals_unreadable", message: "file unreadable" } } },
    });
    await pick(node, "a");
    barButton(node, /Dismiss/).click();
    await flush();
    await flush();
    expect(q(node, ".nt-bulk-line")!.textContent).toContain("Could not dismiss");
    expect(pickedIds()).toEqual(["a"]);
    const reads = request.mock.calls.filter(([call]) => call.method === "GET").length;
    expect(reads).toBeGreaterThanOrEqual(1);
  });

  it("sends more than 50 ids in pieces of 50", () => {
    const ids = Array.from({ length: 120 }, (_, i) => `id-${i}`);
    expect(chunks(ids).map((piece) => piece.length)).toEqual([50, 50, 20]);
  });
});

describe("a bulk snooze and Review N", () => {
  it("snoozes the selected rows from the three choices and says until when", async () => {
    const { node, request } = await mount(overview(ROWS()), {
      "POST /v1/ae/attention/bulk": ok({ results: [{ id: "a", ok: true, status: 200 }, { id: "b", ok: true, status: 200 }], succeeded: 2, failed: 0, until: "2026-11-17T17:00:00Z" }),
    });
    await pick(node, "a");
    await pick(node, "b");
    barButton(node, /^Snooze/).click();
    await flush();
    expect(all(bar(node), ".nt-bulk-choice").map((b) => b.textContent)).toEqual(["Tomorrow", "Monday", "In a week"]);
    barButton(node, /Tomorrow/).click();
    await flush();
    await flush();
    expect(posted(request, "/v1/ae/attention/bulk")).toEqual([{ action: "snooze", ids: ["a", "b"], until: "tomorrow" }]);
    expect(q(node, ".nt-bulk-line")!.textContent).toMatch(/^Snoozed 2 until \w{3} \d+:\d{2}; back sooner if they get worse$/);
    expect(pickedIds()).toEqual([]);
  });

  it("starts one Review across the selected rows", async () => {
    const { node, request } = await mount(overview(ROWS()), {
      "POST /v1/ae/attention/review": ok({ session_id: "s1", path: "/c/s1", title: "Review 2 items from 1 source", item_ids: ["a", "b"] }),
    });
    await pick(node, "a");
    await pick(node, "b");
    barButton(node, /Review 2/).click();
    await flush();
    await flush();
    expect(posted(request, "/v1/ae/attention/review")).toEqual([{ ids: ["a", "b"] }]);
    expect(pickedIds()).toEqual([]);
  });
});

describe("Snooze in the opened row and the Snoozed fold", () => {
  const open = async (node: HTMLElement, id: string) => {
    q(node, `[data-id="${id}"] .nt-toggle`)!.click();
    await flush();
    return q(node, `[data-id="${id}"]`)!;
  };

  it("offers the three choices in the opened row's Snooze menu and says when it will be back", async () => {
    // Layout B (2026-11-16): Snooze is a dropdown button in the toolbar, no longer a box of three buttons.
    const { node, request } = await mount(overview(ROWS()), {
      "POST /v1/ae/attention/snooze": ok({ id: "a", until: "2026-11-23T17:00:00Z" }),
    });
    const row = await open(node, "a");
    expect(q(row, ".nt-snooze")).toBeNull();
    const drop = q(row, ".nt-snooze-drop")!;
    const trigger = q(drop, ".btn-menu")!;
    expect(trigger.textContent).toBe("Snooze");
    expect(trigger.getAttribute("aria-haspopup")).toBe("menu");
    expect(trigger.getAttribute("aria-expanded")).toBe("false");
    trigger.click();
    expect(trigger.getAttribute("aria-expanded")).toBe("true");
    const choices = all(drop, '[role="menuitem"]');
    expect(choices.map((b) => b.textContent)).toEqual(["Tomorrow", "Monday", "In a week"]);
    choices[1]!.click();
    await flush();
    await flush();
    expect(posted(request, "/v1/ae/attention/snooze")).toEqual([{ id: "a", until: "monday" }]);
    const toast = q(node, ".attn-toast")!.textContent!;
    expect(toast).toMatch(/Snoozed until \w{3} \d+:\d{2}; back sooner if it gets worse: Alpha/);
    expect(q(node, ".attn-toast")!.querySelector("button")!.textContent).toBe("Undo");
  });

  it("gives a personal Git item and an item with no rule for worse no Snooze at all", async () => {
    const { node } = await mount(
      overview([item("m", { source: "git-mine", actions: ["dismiss"] }), item("s", { source: "box", kind: "stale", dismissible: false, actions: ["review"] })]),
    );
    expect(q(await open(node, "m"), ".nt-snooze-drop")).toBeNull();
    expect(q(await open(node, "s"), ".nt-snooze-drop")).toBeNull();
    expect(q(node, ".nt-snooze-menu")).toBeNull();
  });

  it("lists snoozed items in a Snoozed (N) fold and Wake brings one back", async () => {
    const asleep = item("z", { title: "Zulu", snoozed: { at: "2026-11-16T10:00:00Z", until: "2026-11-23T17:00:00Z" } });
    const { node, request } = await mount(overview(ROWS(), { snoozed: [asleep] }), {
      "POST /v1/ae/attention/wake": ok({ id: "z", woke: true }),
    });
    const head = q(node, ".attn-snoozed-head")!;
    expect(head.textContent).toBe("Snoozed (1)");
    expect(head.getAttribute("aria-expanded")).toBe("false");
    head.click();
    expect(head.getAttribute("aria-expanded")).toBe("true");
    const row = q(node, ".attn-snoozed-list li")!;
    expect(row.textContent).toContain("Zulu");
    expect(row.textContent).toMatch(/back \w{3} \d+:\d{2}/);
    q(row, "button")!.click();
    await flush();
    expect(posted(request, "/v1/ae/attention/wake")).toEqual([{ id: "z" }]);
  });
});

describe("the history box", () => {
  const open = async (node: HTMLElement, id: string) => {
    q(node, `[data-id="${id}"] .nt-toggle`)!.click();
    await flush();
    await flush();
    return q(node, `[data-id="${id}"]`)!;
  };

  it("loads the item's history when the row opens and lists each event with its time", async () => {
    const { node, request } = await mount(overview(ROWS()), {
      "GET /v1/ae/attention/history?id=a": ok({
        id: "a",
        first_seen: "2026-11-15T09:00:00Z",
        events: [
          { at: "2026-11-15T09:00:00Z", what: "first-seen", by: null },
          { at: "2026-11-16T09:00:00Z", what: "snoozed", by: "alice@example.com" },
          { at: "2026-11-16T10:00:00Z", what: "woke", by: "alice@example.com" },
          { at: "2026-11-16T11:00:00Z", what: "chat-started", by: "alice@example.com" },
        ],
      }),
    });
    expect(request.mock.calls.some(([call]) => String(call.path).startsWith("/v1/ae/attention/history"))).toBe(false);
    const row = await open(node, "a");
    const box = q(row, ".nt-hist")!;
    expect(q(box, ".nt-hist-h")!.textContent).toBe("History");
    expect(all(box, ".nt-hist-what").map((n) => n.textContent)).toEqual(["First seen", "Snoozed", "Woke", "Chat started"]);
    expect(q(box, ".nt-hist-by")!.textContent).toBe(" by alice");
    expect(all(box, ".nt-hist-when").every((n) => n.textContent!.length > 0)).toBe(true);
  });

  it("encodes the id so the bridge's query pattern accepts it", async () => {
    const { node, request } = await mount(overview([item("git:pr-failing:contoso/web#7", { title: "Seven" })]));
    await open(node, "git:pr-failing:contoso/web#7");
    const path = request.mock.calls.map(([call]) => String(call.path)).find((p) => p.startsWith("/v1/ae/attention/history"))!;
    expect(path).toBe("/v1/ae/attention/history?id=git%3Apr-failing%3Acontoso%2Fweb%237");
    expect(path.split("?")[1]).toMatch(/^[A-Za-z0-9_.~%+=&-]*$/);
  });

  it("says so when the log cannot be read, never that there is no history", async () => {
    const { node } = await mount(overview(ROWS()), {
      "GET /v1/ae/attention/history?id=a": ok({ id: "a", first_seen: null, events: [], unknown: true }),
    });
    const row = await open(node, "a");
    expect(q(row, ".nt-hist-body")!.textContent).toContain("could not be read");
    expect(q(row, ".nt-hist-body")!.textContent).not.toContain("Nothing recorded");
  });

  it("says so when the history route fails", async () => {
    const { node } = await mount(overview(ROWS()));
    const row = await open(node, "b");
    expect(q(row, ".nt-hist-body")!.textContent).toContain("History could not be read");
  });
});

describe("the Age column", () => {
  it("comes from the server's first-seen time, and a row without one has an empty Age", async () => {
    const { node } = await mount(
      overview([
        item("old", { title: "Old", first_seen: "2026-11-13T18:00:00Z" }),
        item("new", { title: "New", first_seen: "2026-11-16T17:00:00Z" }),
        item("none", { title: "None", first_seen: null }),
      ]),
    );
    const age = (id: string) => q(node, `[data-id="${id}"] .nt-age`)!.textContent;
    expect(age("old")).toBe("Age: 3 d");
    expect(age("new")).toBe("Age: 1 h");
    expect(age("none")).toBe("Age: ");
    expect(all(node, ".nt-head > span").map((s) => s.textContent)).toEqual(["", "", "What", "Age", "Checked", "Action"]);
  });

  it("repeats Age and Checked on the row's second line for the folded columns", async () => {
    const { node } = await mount(overview([item("old", { first_seen: "2026-11-13T18:00:00Z" })], { read_at: "2026-11-16T17:58:00Z" }));
    expect(q(node, '[data-id="old"] .attn-text')!.dataset.fold).toBe("Age 3 d · Checked 2 min ago");
  });
});

describe("a Social row", () => {
  const post = (fields = {}) =>
    item("social:li-tool-1", { source: "social", kind: "post-overdue", title: "Diagram tool", url: "", slot: "li-tool-1", visual: { kind: "tool", key: "https://img.example/x.jpg" }, actions: ["dismiss"], ...fields });
  const open = async (node: HTMLElement) => {
    q(node, '[data-id="social:li-tool-1"] .nt-toggle')!.click();
    await flush();
    return q(node, '[data-id="social:li-tool-1"]')!;
  };

  it("shows the post's picture from the card's own route, as a blob", async () => {
    visual.blob = new Blob(["x"], { type: "image/jpeg" });
    const { node } = await mount(overview([post()]));
    const row = await open(node);
    const img = q(row, ".nt-pic-img") as HTMLImageElement;
    expect(img.getAttribute("src")).toBe("blob:picture-1");
    expect(img.alt).toBe("Picture for Diagram tool");
  });

  it("starts loading the picture when it is not loaded yet", async () => {
    visual.state = "loading";
    const { node } = await mount(overview([post()], { me: "owner@example.com" }));
    await open(node);
    expect(visual.load).toHaveBeenCalled();
  });

  it("says No picture yet for a post with none, and never loads one", async () => {
    visual.state = "none";
    const { node } = await mount(overview([post({ visual: null })]));
    const row = await open(node);
    expect(q(row, ".nt-pic")!.textContent).toBe("No picture yet");
    expect(q(row, ".nt-pic-img")).toBeNull();
    expect(visual.load).not.toHaveBeenCalled();
  });

  it("says why when the picture is not there yet, and when it failed to load", async () => {
    visual.state = "none";
    const { node } = await mount(overview([post({ visual: null, visual_note: "The thumbnail shows once the video is public." })]));
    expect(q(await open(node), ".nt-pic")!.textContent).toBe("The thumbnail shows once the video is public.");
    deactivateAttention();
    document.body.replaceChildren();
    const root = document.createElement("div");
    root.id = "root";
    document.body.appendChild(root);
    visual.state = "failed";
    const second = await mount(overview([post()]));
    expect(q(await open(second.node), ".nt-pic")!.textContent).toContain("could not be loaded");
  });

  it("builds no link or image source from item text", async () => {
    visual.state = "none";
    const { node } = await mount(overview([post({ title: "https://evil.example/x.png", visual: null })]));
    const row = await open(node);
    expect(all(row, "img")).toHaveLength(0);
    expect(all(row, "a").some((a) => a.getAttribute("href")?.includes("evil"))).toBe(false);
  });

  it("opens the Social card at the post from its own button", async () => {
    const { node } = await mount(overview([post()]));
    const row = await open(node);
    const button = q(row, '[data-extra="post"]')!;
    expect(button.textContent).toBe("Open the post");
    button.click();
    expect(openCard).toHaveBeenCalledWith("social", "ship");
  });
});

// ── the Activity tab, the log store and Done today ──────────────────────────

const DONE = (fields: Partial<DoneAnswer> = {}): DoneAnswer => ({
  items: [
    { id: "git:pr-ready:a/b#1", source: "git", kind: "pr-ready", title: "Merge #1", done_at: "2026-11-16T15:30:00Z", first_seen: "2026-11-14T10:00:00Z" },
    { id: "vault:x", source: "vault", kind: "vault-soon", title: "Key x", done_at: "2026-11-16T09:00:00Z", first_seen: null },
    { id: "git:pr-ready:a/b#0", source: "git", kind: "pr-ready", title: "Merge #0", done_at: "2026-11-15T12:00:00Z", first_seen: null },
  ],
  events: [{ id: "git:pr-ready:a/b#2", source: "git", what: "dismissed", at: "2026-11-15T13:00:00Z", by: "alice@example.com", title: "Bump y" }],
  unknown: [],
  read_at: "2026-11-16T17:59:00Z",
  ...fields,
});

function serve(answer: DoneAnswer | Reply) {
  const body = "ok" in answer ? answer : ok(answer);
  const request = vi.fn(async () => body);
  return { request, context: { server: { request } } as unknown as ExtensionContext };
}

describe("the activity store", () => {
  it("reads seven days, not again within 30 s unless forced, and keeps the last answer on a failure", async () => {
    const made = serve(DONE());
    await loadActivity(made.context, false, 1_000);
    expect(made.request).toHaveBeenCalledWith({ method: "GET", path: "/v1/ae/attention/done?days=7", body: undefined });
    expect(getActivity().loaded).toBe(true);
    await loadActivity(made.context, false, 5_000);
    expect(made.request).toHaveBeenCalledTimes(1);
    await loadActivity(made.context, true, 6_000);
    expect(made.request).toHaveBeenCalledTimes(2);
    const bad = serve({ ok: false, status: 500, body: {} });
    await loadActivity(bad.context, true, 70_000);
    expect(getActivity().data).not.toBeNull();
    expect(getActivity().error).toBeTruthy();
  });

  it("stays unloaded when the server has no such route", async () => {
    await loadActivity(serve({ ok: false, status: 404, body: {} }).context, true);
    expect(getActivity().loaded).toBe(false);
  });

  it("groups by local day, newest first, and picks out today's entries", () => {
    const now = Date.parse("2026-11-16T18:00:00Z");
    const days = activityDays(DONE());
    expect(days.map((d) => d.key)).toEqual([dayKey("2026-11-16T15:30:00Z"), dayKey("2026-11-15T12:00:00Z")]);
    expect(days[1]!.events).toHaveLength(1);
    expect(doneToday(DONE(), now).map((d) => d.title)).toEqual(["Merge #1", "Key x"]);
    expect(dayLabel(dayKey(now), now)).toBe("Today");
    expect(dayLabel(dayKey(now - 86_400_000), now)).toBe("Yesterday");
    expect(eventText(DONE().events![0]!, "Git repos")).toBe("alice dismissed Bump y");
    expect(eventText({ ...DONE().events![0]!, title: "", by: null }, "Git repos")).toBe("Someone dismissed an item in Git repos");
  });
});

describe("the Activity tab", () => {
  it("lists each day's Done entries with their source and when they left, and the day's events", async () => {
    await loadActivity(serve(DONE()).context, true);
    const host = document.createElement("div");
    drawActivity(host, Date.parse("2026-11-16T18:00:00Z"));
    expect(q(host, ".act-note")!.textContent).toContain(DONE_MEANS);
    expect(all(host, ".act-day-h").map((h) => h.textContent)).toEqual(["Today", "Yesterday"]);
    const today = q(host, '.act-day[aria-label="Today"]')!;
    expect(all(today, ".act-what").map((n) => n.textContent)).toEqual(["Merge #1", "Key x"]);
    expect(all(today, ".act-src").map((n) => n.textContent)).toEqual(["Git repos", "Key Vault"]);
    expect(all(today, ".act-when")[0]!.textContent).toMatch(/^left \d+:\d{2}/);
    const yesterday = q(host, '.act-day[aria-label="Yesterday"]')!;
    expect(all(yesterday, ".act-event .act-what").map((n) => n.textContent)).toEqual(["alice dismissed Bump y"]);
  });

  it("says the log could not be read, never an empty week", async () => {
    await loadActivity(serve(DONE({ items: [], events: [], unknown: ["The activity log could not be read"] })).context, true);
    expect(unreadable()).toBe(true);
    const host = document.createElement("div");
    drawActivity(host);
    expect(host.textContent).toContain("The activity log could not be read");
    expect(host.textContent).toContain("not an empty week");
    expect(q(host, ".act-empty")).toBeNull();
  });

  it("says Nothing was dropped only when the log read fine and was empty", async () => {
    await loadActivity(serve(DONE({ items: [], events: [] })).context, true);
    const host = document.createElement("div");
    drawActivity(host);
    expect(q(host, ".act-empty")!.textContent).toContain("Nothing was dropped");
  });

  it("is a tab after the area tabs and before Services, only when the server has the route", () => {
    const connected = { ship: ["git"], box: ["box"], cloud: ["fabric", "azure"], money: ["spend", "accounts"] } as never;
    expect(tabsFor(connected).map((t) => t.id)).toEqual(["overview", "ship", "box", "cloud", "money", "services"]);
    expect(tabsFor(connected, true).map((t) => t.id)).toEqual(["overview", "ship", "box", "cloud", "money", "activity", "services"]);
    const few = { ship: ["git"], box: [], cloud: [], money: [] } as never;
    expect(tabsFor(few, true).map((t) => t.id)).toEqual(["overview", "activity", "services"]);
  });
});

describe("Done today in the rail", () => {
  it("is absent before the route answers", () => {
    expect(doneTodayBlock(null)).toBeNull();
  });

  it("shows a count, up to five entries and See all in Activity", async () => {
    const items = Array.from({ length: 7 }, (_, n) => ({
      id: `i${n}`,
      source: "git" as const,
      kind: "pr-ready",
      title: `Merge #${n}`,
      done_at: `2026-11-16T1${n}:00:00Z`,
      first_seen: null,
    }));
    await loadActivity(serve(DONE({ items, events: [] })).context, true);
    const open = vi.fn();
    const block = doneTodayBlock(open, Date.parse("2026-11-16T18:00:00Z"))!;
    expect(q(block, ".rail-counts")!.textContent).toBe("7");
    expect(all(block, ".rail-member")).toHaveLength(RAIL_DONE_MAX);
    q(block, ".rail-done-all")!.click();
    expect(open).toHaveBeenCalled();
  });

  it("says so on a day with nothing, and says the log could not be read rather than 0", async () => {
    await loadActivity(serve(DONE({ items: [], events: [] })).context, true);
    const quiet = doneTodayBlock(null)!;
    expect(quiet.textContent).toContain("Nothing has dropped off the list yet today");
    expect(q(quiet, ".rail-done-all")).toBeNull();
    resetActivity();
    await loadActivity(serve(DONE({ items: [], unknown: ["The activity log could not be read"] })).context, true);
    expect(doneTodayBlock(null)!.textContent).toContain("could not be read");
    expect(doneTodayBlock(null)!.querySelector(".nt-pill-done")).toBeNull();
  });
});

// ── the selection and its words, without a page ─────────────────────────────

describe("selection words", () => {
  it("counts a group's state", () => {
    resetSelection();
    expect(groupState([])).toBe("none");
    setPicked(["a"], true);
    expect(groupState(["a", "b"])).toBe("some");
    setPicked(["b"], true);
    expect(groupState(["a", "b"])).toBe("all");
    keepOnly(["a"]);
    expect(isPicked("b")).toBe(false);
    clearPicked();
    expect(groupState(["a", "b"])).toBe("none");
  });

  it("plans what each button acts on, in list order", () => {
    resetSelection();
    const rows = [
      item("a", { actions: ["review", "dismiss"] }),
      item("m", { source: "git-mine", actions: ["dismiss"] }),
      item("h", { source: "box", kind: "stale", dismissible: false, actions: ["review"] }),
    ];
    setPicked(["a", "m", "h"], true);
    const plan = planBulk(rows);
    expect(plan.dismiss).toEqual(["a", "m", "h"]);
    expect(plan.snooze).toEqual(["a"]);
    expect(plan.review).toEqual(["a", "h"]);
    expect(plan.dismissOnly).toBe(1);
  });

  it("words a partial bulk answer with its reasons once each", () => {
    const answer = joinAnswers([
      { results: [{ id: "a", ok: true, status: 200 }, { id: "b", ok: false, status: 404, error: "gone" }], succeeded: 1, failed: 1 },
      { results: [{ id: "c", ok: false, status: 404, error: "gone" }], succeeded: 0, failed: 1, until: "2026-11-17T17:00:00Z" },
    ]);
    const words = bulkWords("dismiss", answer);
    expect(words.line).toBe("Dismissed 1, 2 could not be dismissed");
    expect(words.failed).toEqual(["b", "c"]);
    expect(words.reasons).toEqual([{ error: "gone", count: 2 }]);
    expect(bulkWords("snooze", { results: [], succeeded: 0, failed: 0 }).line).toBe("Snoozed 0");
  });

  it("says when a snooze ends on the viewer's clock", () => {
    expect(untilText("not a date")).toBe("");
    expect(untilText("2026-11-23T09:05:00")).toBe("Mon 9:05");
    expect(snoozedLine("2026-11-23T09:00:00")).toBe("Snoozed until Mon 9:00; back sooner if it gets worse");
    expect(snoozedLine(undefined)).toBe("Snoozed; back sooner if it gets worse");
  });
});



// ── fix round 1 (2026-11-16) ────────────────────────────────────────────────

describe("the selection through tabs and filters", () => {
  it("keeps an id selected when the tab or the source filter hides it, and says so", async () => {
    const mixed = overview([
      item("a", { title: "Alpha" }),
      item("b1", { source: "box", kind: "heat", url: "", title: "Heat", actions: ["review", "dismiss"] }),
    ]);
    const { node, table } = await mount(mixed);
    await pick(node, "a");
    table.setArea("box");
    await flush();
    expect(pickedIds()).toEqual(["a"]);
    expect(q(bar(node), ".nt-bulk-n")!.textContent).toBe("1 selected");
    expect(q(bar(node), ".nt-bulk-note")!.textContent).toContain("another tab or filter");
    table.setArea(null);
    setFilter("box");
    await flush();
    expect(pickedIds()).toEqual(["a"]);
    window.dispatchEvent(new Event("focus"));
    await flush();
    await flush();
    expect(pickedIds()).toEqual(["a"]);
    setFilter(null);
    await flush();
    expect((q(node, 'input[data-pick="a"]') as HTMLInputElement).checked).toBe(true);
  });
});

describe("a bulk request in pieces", () => {
  it("applies each chunk as it returns, and keeps the failed and unsent ids selected when a later chunk fails", async () => {
    const rows = Array.from({ length: 60 }, (_, n) => item(`r${n}`, { title: `Row ${n}` }));
    let calls = 0;
    const { node, request } = await mount(overview(rows), {
      "POST /v1/ae/attention/bulk": () => {
        calls += 1;
        if (calls === 2) return { ok: false, status: 500, body: { error: { code: "boom", message: "server fell over" } } };
        const ids = (request.mock.calls.filter(([c]) => c.method === "POST").at(-1)![0].body as { ids: string[] }).ids;
        return ok({ results: ids.map((id) => ({ id, ok: true, status: 200 })), succeeded: ids.length, failed: 0 });
      },
    });
    setPicked(
      rows.map((r) => r.id),
      true,
    );
    await flush();
    const before = request.mock.calls.filter(([c]) => c.method === "GET").length;
    barButton(node, /Dismiss/).click();
    await flush();
    await flush();
    await flush();
    expect(calls).toBe(2);
    const line = q(node, ".nt-bulk-line")!.textContent!;
    expect(line).toContain("Dismissed 50");
    expect(line).toContain("Could not dismiss the rest");
    expect(line).toContain("10 items were not sent and stay selected");
    expect(pickedCount()).toBe(10);
    expect(pickedIds().every((id) => Number(id.slice(1)) >= 50)).toBe(true);
    expect(request.mock.calls.filter(([c]) => c.method === "GET" && c.path === "/v1/ae/attention").length).toBeGreaterThan(before);
  });
});

describe("select-all reaches every row the list shows for the line", () => {
  // 2026-11-18: a line's box selects its rows drawn or not (it was disabled on a closed line, and at the wide band
  // every source line draws closed).
  it("picks all rows of a source, shown or behind Show more, open or closed", async () => {
    saveFold("attn.git", true);
    const rows = Array.from({ length: 8 }, (_, n) => item(`s${n}`, { title: `Row ${n}` }));
    const { node } = await mount(overview(rows));
    const group = () => q(node, '[data-source="git"] input[data-pick-group]') as HTMLInputElement;
    expect(group().disabled).toBe(false);
    group().click();
    await flush();
    expect(pickedCount()).toBe(8);
    expect(group().checked).toBe(true);
    clearPicked();
    saveFold("attn.git", false);
    q(node, '[data-source="git"] .nt-grp-btn')!.click();
    await flush();
    expect(group().disabled).toBe(false);
    group().click();
    await flush();
    expect(pickedCount()).toBe(8);
    expect(q(node, ".nt-bulk")!.hidden).toBe(false);
  });

  it("selects a closed area line's rows, and shows mixed state as indeterminate", async () => {
    const { node } = await mount(
      overview([
        item("u1", { source: "vault", kind: "vault-soon", tone: "review", url: "", actions: ["review"] }),
        item("u2", { source: "vault", kind: "vault-soon", tone: "review", url: "", actions: ["review"] }),
      ]),
    );
    q(node, '[data-line="nt.area.cloud"] > .nt-grp-head .nt-grp-btn')!.click();
    await flush();
    const area = () => q(node, '[data-line="nt.area.cloud"] > .nt-grp-head input[data-pick-group]') as HTMLInputElement;
    expect(area().disabled).toBe(false);
    area().click();
    await flush();
    expect(pickedCount()).toBe(2);
    setPicked(["u2"], false);
    await flush();
    expect(area().indeterminate).toBe(true);
    expect(q(node, '[data-line="nt.area.cloud"] > .nt-grp-head .nt-pick')!.title).toContain("Select every row");
  });
});

describe("the history cache after an action", () => {
  it("is dropped after a snooze, so reopening the row reads the history again", async () => {
    const { node, request } = await mount(overview(ROWS()), {
      "GET /v1/ae/attention/history?id=a": ok({ id: "a", first_seen: null, events: [] }),
      "POST /v1/ae/attention/snooze": ok({ id: "a", until: "2026-11-23T17:00:00Z" }),
    });
    const reads = () => request.mock.calls.filter(([c]) => String(c.path).startsWith("/v1/ae/attention/history")).length;
    const toggle = () => q(node, '[data-id="a"] .nt-toggle')!.click();
    toggle();
    await flush();
    await flush();
    expect(reads()).toBe(1);
    q(node, '[data-id="a"] .nt-snooze-drop .btn-menu')!.click();
    q(node, '[data-id="a"] .nt-snooze-drop [role="menuitem"]')!.click();
    await flush();
    await flush();
    toggle();
    await flush();
    toggle();
    await flush();
    await flush();
    expect(reads()).toBe(2);
  });
});

describe("what the lists say when part of the answer is missing", () => {
  it("says the newest read failed in the rail, and how many rows the answer left out", async () => {
    await loadActivity(serve(DONE({ more_items: 7, more_events: 3 })).context, true);
    await loadActivity(serve({ ok: false, status: 500, body: {} }).context, true, Date.now() + 99_999);
    const block = doneTodayBlock(null)!;
    expect(block.textContent).toContain("The newest read failed; this is the last answer.");
    expect(block.textContent).toContain("7 more done items not shown.");
    const host = document.createElement("div");
    drawActivity(host);
    expect(all(host, ".act-more").map((n) => n.textContent)).toEqual([
      "7 more done items not shown (the oldest).",
      "3 more actions not shown (the oldest).",
    ]);
  });

  it("names the items a Review left out and why", async () => {
    const { node } = await mount(overview(ROWS()), {
      "POST /v1/ae/attention/review": ok({
        session_id: "s1",
        path: "/c/s1",
        item_ids: ["a"],
        skipped: [{ id: "b", why: "Home security items are reviewed on their own." }],
      }),
    });
    await pick(node, "a");
    await pick(node, "b");
    barButton(node, /Review 2/).click();
    await flush();
    await flush();
    expect(q(node, ".flash")!.textContent).toBe("Left out of the review: Bravo (Home security items are reviewed on their own.).");
  });
});

describe("a bulk action's result and focus (2026-11-19, review round 1)", () => {
  const tool = (node: HTMLElement) => q(node, ":scope > .attn-top > .attn-tools button, :scope > .attn-top > .attn-tools input")!;

  it("a successful action on the last selected item hides the bar, shows the tools, puts focus on the first tool, and adds no row", async () => {
    const { node } = await mount(overview(ROWS()), {
      "POST /v1/ae/attention/bulk": ok({ results: [{ id: "a", ok: true, status: 200 }], succeeded: 1, failed: 0 }),
    });
    await pick(node, "a");
    expect(q(node, ".nt-bulk-status")!.textContent).toBe("1 selected, selection actions shown");
    barButton(node, /Dismiss/).focus();
    barButton(node, /Dismiss/).click();
    await flush();
    await flush();
    expect(bar(node).hidden).toBe(true);
    expect(bar(node).children).toHaveLength(1);
    expect(q(bar(node), ".nt-bulk-result")).toBeNull();
    // The result is a note out of the bar, and the polite status says it.
    expect(q(node, ".nt-bulk-line")!.textContent).toBe("Dismissed 1");
    expect(q(node, ".nt-bulk-status")!.textContent).toBe("Dismissed 1");
    expect(q(node, ".nt-bulk-details")!.hidden).toBe(true);
    // The note took the toolbar row, so focus waits on its Close button; closing it hands focus to the first tool.
    expect(document.activeElement).toBe(q(node, ".nt-bulk-x"));
    q(node, ".nt-bulk-x")!.click();
    expect(document.activeElement).toBe(tool(node));
  });

  it("a partial failure leaves the bar as it was, with the reasons behind a Details button and popover", async () => {
    const { node } = await mount(overview(ROWS()), {
      "POST /v1/ae/attention/bulk": ok({
        results: [
          { id: "a", ok: true, status: 200 },
          { id: "b", ok: false, status: 409, error: "A stale reading cannot be dismissed." },
        ],
        succeeded: 1,
        failed: 1,
      }),
    });
    await pick(node, "a");
    await pick(node, "b");
    barButton(node, /Dismiss/).click();
    await flush();
    await flush();
    expect(bar(node).hidden).toBe(false);
    expect(bar(node).children).toHaveLength(1);
    expect(q(bar(node), ".nt-bulk-result, .nt-bulk-why")).toBeNull();
    const note = q(node, ".attn-top > .nt-bulk-result")!;
    expect(note.hidden).toBe(false);
    expect(q(note, ".nt-bulk-line")!.textContent).toBe("Dismissed 1, 1 could not be dismissed");
    const pop = q(note, ".nt-bulk-pop")!;
    expect(pop.hidden).toBe(true);
    const details = q(note, ".nt-bulk-details")!;
    expect(details.hidden).toBe(false);
    details.click();
    expect(pop.hidden).toBe(false);
    expect(details.getAttribute("aria-expanded")).toBe("true");
    expect(all(pop, "li").map((li) => li.textContent)).toEqual(["A stale reading cannot be dismissed."]);
    // The note's close button hides it, and a new selection hides it too.
    q(note, ".nt-bulk-x")!.click();
    expect(note.hidden).toBe(true);
  });

  it("hides the note by itself after 6 seconds and when a new selection starts", async () => {
    vi.useFakeTimers();
    try {
      const { node } = await mount(overview(ROWS()), {
        "POST /v1/ae/attention/bulk": ok({ results: [{ id: "a", ok: true, status: 200 }], succeeded: 1, failed: 0 }),
      });
      await pick(node, "a");
      barButton(node, /Dismiss/).click();
      await vi.advanceTimersByTimeAsync(10);
      const note = q(node, ".attn-top > .nt-bulk-result")!;
      expect(note.hidden).toBe(false);
      await vi.advanceTimersByTimeAsync(6100);
      expect(note.hidden).toBe(true);
      await pick(node, "b");
      barButton(node, /Dismiss/).click();
      await vi.advanceTimersByTimeAsync(10);
      expect(note.hidden).toBe(false);
      await pick(node, "c");
      expect(note.hidden).toBe(true);
    } finally {
      vi.useRealTimers();
    }
  });

  it("a refresh that removes the last selected row hides the bar and gives focus to the first tool", async () => {
    const { node } = await mount(overview(ROWS()));
    await pick(node, "a");
    barButton(node, /Dismiss/).focus();
    setAttention(overview(ROWS().filter((r) => r.id !== "a")));
    await flush();
    expect(bar(node).hidden).toBe(true);
    expect(document.activeElement).toBe(tool(node));
    expect(q(node, ".nt-bulk-status")!.textContent).toBe("Selection cleared");
  });

  it("shows the Snooze choices as a menu under the button, out of the bar's flow, and Escape closes it", async () => {
    const { node } = await mount(overview(ROWS()));
    await pick(node, "a");
    barButton(node, /^Snooze/).click();
    await flush();
    const menu = q(bar(node), ".nt-bulk-snooze > .nt-bulk-menu")!;
    expect(all(menu, "button").map((b) => b.textContent)).toEqual(["Tomorrow", "Monday", "In a week"]);
    expect(css).toMatch(/\.nt-bulk-menu \{\s*position: absolute;/);
    bar(node).dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
    expect(q(bar(node), ".nt-bulk-menu")).toBeNull();
    expect(document.activeElement).toBe(barButton(node, /^Snooze/));
  });

  it("closes the Snooze menu on a press outside the group and when focus leaves it, without moving focus", async () => {
    const { node } = await mount(overview(ROWS()));
    await pick(node, "a");
    barButton(node, /^Snooze/).click();
    await flush();
    expect(q(bar(node), ".nt-bulk-menu")).not.toBeNull();
    // A press inside the group keeps it; one outside closes it and leaves focus where it was.
    q(bar(node), ".nt-bulk-menu button")!.dispatchEvent(new Event("pointerdown", { bubbles: true }));
    expect(q(bar(node), ".nt-bulk-menu")).not.toBeNull();
    const other = q(node, ".attn-top > .attn-tools button")!;
    other.focus();
    document.body.dispatchEvent(new Event("pointerdown", { bubbles: true }));
    expect(q(bar(node), ".nt-bulk-menu")).toBeNull();
    expect(document.activeElement).toBe(other);
    // Focus moving out of the group closes it too.
    barButton(node, /^Snooze/).click();
    await flush();
    expect(q(bar(node), ".nt-bulk-menu")).not.toBeNull();
    barButton(node, /^Snooze/).dispatchEvent(new FocusEvent("focusout", { bubbles: true, relatedTarget: barButton(node, /Clear/) }));
    expect(q(bar(node), ".nt-bulk-menu")).toBeNull();
  });

  it("restarts the 6 second timer when Details are closed, and starts none after the table is gone", async () => {
    const bulk = { results: [{ id: "a", ok: true, status: 200 }, { id: "b", ok: false, status: 409, error: "Stale." }], succeeded: 1, failed: 1 };
    const { node, table } = await mount(overview(ROWS()), { "POST /v1/ae/attention/bulk": ok(bulk) });
    await pick(node, "a");
    await pick(node, "b");
    barButton(node, /Dismiss/).click();
    await flush();
    await flush();
    const note = q(node, ".attn-top > .nt-bulk-result")!;
    q(note, ".nt-bulk-details")!.click();
    await vi.advanceTimersByTimeAsync(20000);
    expect(note.hidden).toBe(false);
    q(note, ".nt-bulk-details")!.click();
    await vi.advanceTimersByTimeAsync(5000);
    expect(note.hidden).toBe(false);
    await vi.advanceTimersByTimeAsync(1500);
    expect(note.hidden).toBe(true);
    // After dispose a late result starts no timer and shows nothing.
    table.dispose();
    expect(vi.getTimerCount()).toBe(0);
  });

  it("applies the inline note rules at every width, ahead of the stack-band comment, and keeps focus when a new selection starts on the note's Close", async () => {
    const at = css.indexOf(".attn-top > .nt-bulk-result.is-inline {");
    const open = css.indexOf("@container admin (min-width: 521px) {", css.lastIndexOf("}\n\n", at));
    expect(at).toBeGreaterThan(0);
    // The three is-inline rules sit before the 521 px container block opens, so they apply on the phone too.
    expect(open).toBeGreaterThan(at);
    expect(css.indexOf(".attn-top:has(> .nt-bulk-result.is-inline:not([hidden])) > :is(.attn-tools, .nt-bulk)")).toBeLessThan(open);
    expect(css.indexOf('.as-ov[data-wall="stack"] .attn-top > .nt-bulk-result.is-inline {')).toBeLessThan(open);
    const comment = css.indexOf("At the stack band the tools share the heading's row");
    expect(css.slice(comment).match(/^[^{]*\{/)![0]).toMatch(/\.attn-counts, \.nt-dlink\)/);

    const { node } = await mount(overview(ROWS()), {
      "POST /v1/ae/attention/bulk": ok({ results: [{ id: "a", ok: true, status: 200 }], succeeded: 1, failed: 0 }),
    });
    const comp = q(node, ":scope > .nt-comp-slot")!;
    Object.defineProperty(comp, "offsetHeight", { configurable: true, value: 12 });
    await pick(node, "a");
    barButton(node, /Dismiss/).click();
    await flush();
    await flush();
    expect(document.activeElement).toBe(q(node, ".nt-bulk-x"));
    await pick(node, "b");
    expect(document.activeElement).not.toBe(document.body);
    expect(q(node, ".attn-top > .nt-bulk-result")!.hidden).toBe(true);
  });

  it("puts the note in the toolbar row's place when the progress bars slot is short, and over the header only when it has room", async () => {
    const { node } = await mount(overview(ROWS()), {
      "POST /v1/ae/attention/bulk": ok({ results: [{ id: "a", ok: true, status: 200 }], succeeded: 1, failed: 0 }),
    });
    const comp = q(node, ":scope > .nt-comp-slot")!;
    Object.defineProperty(comp, "offsetHeight", { configurable: true, value: 12 });
    await pick(node, "a");
    barButton(node, /Dismiss/).click();
    await flush();
    await flush();
    expect(q(node, ".attn-top > .nt-bulk-result")!.classList.contains("is-inline")).toBe(true);
    expect(css).toMatch(/\.attn-top:has\(> \.nt-bulk-result\.is-inline:not\(\[hidden\]\)\) > :is\(\.attn-tools, \.nt-bulk\) \{\s*display: none;/);
    expect(css).toMatch(/\.attn-top > \.nt-bulk-result\.is-inline \{\s*position: static;[^}]*height: var\(--ctl-h, 32px\);/);
    Object.defineProperty(comp, "offsetHeight", { configurable: true, value: 60 });
    await pick(node, "b");
    barButton(node, /Dismiss/).click();
    await flush();
    await flush();
    expect(q(node, ".attn-top > .nt-bulk-result")!.classList.contains("is-inline")).toBe(false);
  });

  it("scopes the rule that hides the tools to where the bar shows, and keeps the counts readable to assistive tech at the stack band", () => {
    expect(css).toMatch(/@container admin \(min-width: 521px\) \{\s*\.attn-top:has\(> \.nt-bulk\[data-count\]:not\(\[data-count="0"\], \[hidden\]\)\) > \.attn-tools \{\s*display: none;/);
    const stack = css.slice(css.indexOf('.as-ov[data-wall="stack"] .attn-top:has(> .nt-bulk'));
    const rule = stack.slice(0, stack.indexOf("}"));
    expect(rule).toContain("clip: rect(0 0 0 0)");
    expect(rule).not.toContain("display: none");
    // The phone layout hides the bar, so the tools stay (the hiding rule is not outside the container query).
    expect(css).not.toMatch(/\n\.attn-top:has\(> \.nt-bulk\[data-count\][^{]*\{\s*display: none/);
  });

  it("says leaving selection mode once, 'Selection cleared', and gives the arrows a 44 px hit box on coarse pointers", async () => {
    const { node } = await mount(overview(ROWS()));
    await pick(node, "a");
    barButton(node, /Clear/).click();
    await flush();
    expect(q(node, ".nt-bulk-status")!.textContent).toBe("Selection cleared");
    expect(css).toMatch(/@media \(pointer: coarse\) \{\s*\.nt-opens \{\s*width: 44px;\s*height: 44px;/);
  });
});
