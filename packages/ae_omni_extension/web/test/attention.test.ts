// The Admin Center's Needs you panel (2026-11-03, attention.ts): the two-level tree and how it
// folds, the rows and their buttons, Review (and its slots-full question), Dismiss with its
// Undo and the Dismissed fold, and the states around them. Every call is mocked; names are made up.
import type { ExtensionContext } from "@omnigent/extension-sdk";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { AttentionItem, AttentionOverview, PrCard } from "../src/api";
import {
  activateAttention,
  attentionSection,
  BOX_REASONS,
  buildBlocks,
  comesBackOn,
  countsText,
  originOf,
  DASHBOARD_PAGE_ID,
  deactivateAttention,
  defaultOpen,
  emptyText,
  resetAttentionSeen,
  GROUPS,
  ROWS_SHOWN,
  UNDO_MS,
  waitingText,
} from "../src/attention";
import {
  getFilter,
  resetAttention,
  setAttention,
  setFilter,
  showInNeedsYou,
  liveNeedsLink,
  SOURCE_NAMES,
  sourceRead,
} from "../src/attentionStore";
import { openCleanup } from "../src/boxHealth";
import { resetFolds } from "../src/fold";
import { DOMAINS, resetTiles, setTile } from "../src/tiles";
import { mountVerdict } from "../src/verdict";
import css from "../src/style.css?raw";
import linesText from "./fixtures/attention-lines.json?raw";
import { item, many, overview } from "./attentionFixtures";

// The panel imports the Box card only for its Clean up confirm; that dialog has its own tests.
vi.mock("../src/boxHealth", () => ({ openCleanup: vi.fn(() => true) }));

type Reply = { ok: boolean; status: number; body: unknown };
const ok = (body: unknown): Reply => ({ ok: true, status: 200, body });
const failed = (status: number, code: string, message: string, extra: object = {}): Reply => ({
  ok: false,
  status,
  body: { error: { code, message, ...extra } },
});

interface Call {
  method: string;
  path: string;
  body: unknown;
}

/** A context whose server answers by "METHOD path" from *routes* (a function or a fixed reply). */
function stubContext(routes: Record<string, Reply | ((call: Call) => Reply)>) {
  const calls: Call[] = [];
  const request = vi.fn(async (call: Call) => {
    calls.push(call);
    const route = routes[`${call.method} ${call.path}`];
    if (!route) return failed(500, "unexpected", `unexpected ${call.method} ${call.path}`);
    return typeof route === "function" ? route(call) : route;
  });
  const openSession = vi.fn(async () => undefined);
  const openExternal = vi.fn(async () => undefined);
  const openPage = vi.fn(async () => undefined);
  const context = {
    view: "fabric",
    server: { request },
    navigation: { openSession, openExternal, openPage },
  } as unknown as ExtensionContext;
  return { context, calls, openSession, openExternal, openPage };
}

const LIST = "GET /v1/ae/attention";
const flush = () => vi.advanceTimersByTimeAsync(0);
const q = <T extends HTMLElement = HTMLElement>(node: ParentNode, sel: string) => node.querySelector<T>(sel);
const all = (node: ParentNode, sel: string) => [...node.querySelectorAll<HTMLElement>(sel)];
const text = (node: ParentNode, sel: string) => q(node, sel)?.textContent;

async function mount(context: ExtensionContext): Promise<HTMLElement> {
  const node = attentionSection();
  document.getElementById("root")?.appendChild(node);
  activateAttention(context, node);
  await flush();
  return node;
}

/** The tree line (a source or a kind) by its fold id. */
const line = (node: ParentNode, id: string) => q(node, `[data-line="${id}"]`)!;
const expanded = (node: ParentNode, id: string) => line(node, id).getAttribute("aria-expanded");
const rowsOf = (node: ParentNode, id: string) => all(line(node, id), ".attn-row");
const click = async (node: Element | null | undefined) => {
  (node as HTMLElement).click();
  await flush();
};
const buttonByText = (node: ParentNode, label: string) =>
  all(node, "button").find((b) => b.textContent === label)!;

function setPhone(matches: boolean): void {
  window.matchMedia = vi.fn(() => ({
    matches,
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
  })) as unknown as typeof window.matchMedia;
}

const BOX_OFFER = {
  id: "claude_scratch" as const,
  owner: "UserA",
  where: "box" as const,
  label: "Remove 268 folders",
  confirm: "Remove 268 folders from UserA's Claude temp area.",
  can_run: true,
  why_not: null,
  hint: "Removes stale temp folders.",
};

/** Two failing PRs, an alert, a ready PR (review) and a box temp item (review). */
function sample(extra: AttentionItem[] = []): AttentionOverview {
  return overview([
    ...many(2, "pr-failing"),
    item("alerts-fix-1", { kind: "alerts-fix", title: "1 alert to fix: lodash" }),
    item("pr-ready-1", { kind: "pr-ready", tone: "review", title: "Merge #12 Bump thing" }),
    item("box-temp-1", {
      source: "box",
      kind: "temp",
      tone: "review",
      title: "268 temp folders",
      url: "",
      back_if: "the folders double",
      expires_days: 7,
      offer: BOX_OFFER,
      actions: ["dismiss", "cleanup"],
    }),
    ...extra,
  ]);
}

const NOON = new Date(2026, 10, 3, 12, 0, 0);

beforeEach(() => {
  resetTiles();
  resetAttention();
  resetFolds();
  resetAttentionSeen();
  window.localStorage.clear();
  delete (window as { matchMedia?: unknown }).matchMedia;
  document.body.replaceChildren();
  const root = document.createElement("div");
  root.id = "root";
  document.body.appendChild(root);
  vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout", "Date"] });
  vi.setSystemTime(NOON);
  vi.mocked(openCleanup).mockClear();
});

afterEach(() => {
  deactivateAttention();
  vi.useRealTimers();
});

describe("the groups", () => {
  it("makes one block per source and one group per kind, urgent kinds first", () => {
    const blocks = buildBlocks(sample().items);
    expect(blocks.map((b) => b.name)).toEqual(["Git repos", "Box health"]);
    expect(blocks[0]!.groups.map((g) => g.name)).toEqual(["Alerts to fix", "Failing PRs", "Ready to merge"]);
    expect(blocks[0]).toMatchObject({ urgent: 3, review: 1 });
    expect(blocks[1]!.groups.map((g) => g.name)).toEqual(["Temp folders and leftovers"]);
    expect(buildBlocks(sample().items, "box")).toHaveLength(1);
  });

  it("names the worst item of a line and leaves an item a chat holds out of its counts", () => {
    const held = item("h", { working: { session_id: "s1", title: "Chat", path: "/s/s1" }, title: "Held one" });
    const [block] = buildBlocks([held, item("u", { title: "Urgent one" })]);
    expect(block!.urgent).toBe(1);
    expect(block!.worst?.title).toBe("Urgent one");
    expect(block!.items).toHaveLength(2);
  });

  it("keeps a kind the table does not know in an Other group", () => {
    const [block] = buildBlocks([item("x", { kind: "brand-new" })]);
    expect(block!.groups.map((g) => g.name)).toEqual(["Other"]);
  });

  it("words the counts, and the day an item comes back", () => {
    expect(countsText(6, 17)).toBe("6 urgent, 17 to review");
    expect(countsText(0, 1)).toBe("1 to review");
    expect(countsText(0, 0)).toBe("");
    expect(countsText(0, 0, 1)).toBe("0 to review, 1 chat working");
    expect(countsText(0, 0, 3)).toBe("0 to review, 3 chats working");
    expect(countsText(2, 0, 3)).toBe("2 urgent");
    expect(comesBackOn(3, NOON.getTime())).toBe("Nov 6");
  });

  it("opens only the sources and kinds with an urgent item, and on a phone only the first source", () => {
    const blocks = buildBlocks(
      [...sample().items, item("box-heat", { source: "box", kind: "heat" })],
    );
    const wide = defaultOpen(blocks, false);
    expect(wide.get("attn.git")).toBe(true);
    expect(wide.get("attn.git.pr-failing")).toBe(true);
    expect(wide.get("attn.git.pr-ready")).toBe(false);
    expect(wide.get("attn.box")).toBe(true);
    expect(wide.get("attn.box.temp")).toBe(false);
    const phone = defaultOpen(blocks, true);
    expect(phone.get("attn.git")).toBe(true);
    expect(phone.get("attn.box")).toBe(false);
  });
});

describe("folding", () => {
  it("opens a source with an urgent item and only its urgent kinds; the rest is one line", async () => {
    const { context } = stubContext({ [LIST]: ok(sample()) });
    const node = await mount(context);
    expect(expanded(node, "attn.git")).toBe("true");
    expect(expanded(node, "attn.git.pr-failing")).toBe("true");
    expect(expanded(node, "attn.git.alerts-fix")).toBe("true");
    expect(expanded(node, "attn.git.pr-ready")).toBe("false");
    expect(expanded(node, "attn.box")).toBe("false");
    expect(q<HTMLElement>(line(node, "attn.git.pr-ready"), ".attn-rows")?.hidden).toBe(true);
    expect(q<HTMLElement>(line(node, "attn.box"), ".attn-children")?.hidden).toBe(true);
    // A closed line names its worst item and its counts.
    expect(text(line(node, "attn.box"), ".attn-parent-line .attn-n")).toBe("1 to review");
    expect(text(line(node, "attn.box"), ".attn-parent-line .attn-worst")).toBe("268 temp folders");
    expect(text(line(node, "attn.git.pr-ready"), ".attn-n")).toBe("1 to review");
    expect(text(line(node, "attn.git.pr-ready"), ".attn-worst")).toBe("Merge #12 Bump thing");
    // An open line shows its rows instead.
    expect(q(line(node, "attn.git.pr-failing"), ".attn-worst")).toBeNull();
  });

  it("opens only the first source with an urgent item on a phone", async () => {
    setPhone(true);
    const both = overview([item("g1"), item("b1", { source: "box", kind: "heat", url: "" })]);
    const { context } = stubContext({ [LIST]: ok(both) });
    const node = await mount(context);
    expect(expanded(node, "attn.git")).toBe("true");
    expect(expanded(node, "attn.box")).toBe("false");
  });

  it("opens both urgent sources on a wide page", async () => {
    const both = overview([item("g1"), item("b1", { source: "box", kind: "heat", url: "" })]);
    const { context } = stubContext({ [LIST]: ok(both) });
    const node = await mount(context);
    expect(expanded(node, "attn.git")).toBe("true");
    expect(expanded(node, "attn.box")).toBe("true");
  });

  it("opens and closes a line from a click, and remembers it", async () => {
    const { context } = stubContext({ [LIST]: ok(sample()) });
    const node = await mount(context);
    await click(q(line(node, "attn.git.pr-ready"), ".attn-line"));
    expect(expanded(node, "attn.git.pr-ready")).toBe("true");
    await click(q(line(node, "attn.git"), ".attn-parent-line"));
    expect(expanded(node, "attn.git")).toBe("false");
    expect(window.localStorage.getItem("presets.fold.attn.git")).toBe("closed");
  });

  it("closes everything with Collapse all and opens the urgent lines again with Expand urgent", async () => {
    const { context } = stubContext({ [LIST]: ok(sample()) });
    const node = await mount(context);
    await click(buttonByText(node, "Collapse all"));
    for (const id of ["attn.git", "attn.git.pr-failing", "attn.git.alerts-fix", "attn.git.pr-ready", "attn.box"]) {
      expect(expanded(node, id)).toBe("false");
    }
    await click(q(line(node, "attn.box"), ".attn-parent-line"));
    await click(buttonByText(node, "Expand urgent"));
    expect(expanded(node, "attn.git")).toBe("true");
    expect(expanded(node, "attn.git.pr-failing")).toBe("true");
    // Not urgent: closed again, even though the viewer had opened the box line.
    expect(expanded(node, "attn.git.pr-ready")).toBe("false");
    expect(expanded(node, "attn.box")).toBe("false");
  });

  it("gives every control a tooltip", async () => {
    const { context } = stubContext({ [LIST]: ok(sample()) });
    const node = await mount(context);
    for (const label of ["Expand urgent", "Collapse all"]) {
      expect(buttonByText(node, label).title.length).toBeGreaterThan(10);
    }
  });

  it("restores what the viewer left, after the page is opened again", async () => {
    const a = stubContext({ [LIST]: ok(sample()) });
    let node = await mount(a.context);
    await click(buttonByText(node, "Collapse all"));
    deactivateAttention();
    resetFolds();
    document.body.replaceChildren();
    document.body.appendChild(Object.assign(document.createElement("div"), { id: "root" }));
    const b = stubContext({ [LIST]: ok(sample()) });
    node = await mount(b.context);
    expect(expanded(node, "attn.git")).toBe("false");
    expect(expanded(node, "attn.git.pr-failing")).toBe("false");
  });

  it("opens a line again by itself when it gains an urgent item it has not shown", async () => {
    const first = overview([item("p1"), item("a1", { kind: "alerts-fix" })]);
    let answer = first;
    const { context } = stubContext({ [LIST]: () => ok(answer) });
    const node = await mount(context);
    await click(buttonByText(node, "Collapse all"));
    expect(expanded(node, "attn.git")).toBe("false");
    // The same answer again: still closed.
    window.dispatchEvent(new Event("focus"));
    await flush();
    expect(expanded(node, "attn.git")).toBe("false");
    // A new urgent failing PR: its source and its kind open; the alerts kind (nothing new) stays closed.
    answer = overview([item("p1"), item("a1", { kind: "alerts-fix" }), item("p2")]);
    window.dispatchEvent(new Event("focus"));
    await flush();
    expect(expanded(node, "attn.git")).toBe("true");
    expect(expanded(node, "attn.git.pr-failing")).toBe("true");
    expect(expanded(node, "attn.git.alerts-fix")).toBe("false");
  });

  it("does not reopen a line for a new item that is only to review", async () => {
    let answer = overview([item("p1")]);
    const { context } = stubContext({ [LIST]: () => ok(answer) });
    const node = await mount(context);
    await click(buttonByText(node, "Collapse all"));
    answer = overview([item("p1"), item("r1", { kind: "pr-update", tone: "review" })]);
    window.dispatchEvent(new Event("focus"));
    await flush();
    expect(expanded(node, "attn.git")).toBe("false");
  });
});

describe("the rows", () => {
  it("shows at most three rows under a kind, then Show N more, and Show fewer folds back", async () => {
    const { context } = stubContext({ [LIST]: ok(overview(many(5, "pr-failing"))) });
    const node = await mount(context);
    expect(ROWS_SHOWN).toBe(3);
    expect(rowsOf(node, "attn.git.pr-failing")).toHaveLength(3);
    const more = q(line(node, "attn.git.pr-failing"), ".attn-more")!;
    expect(more.textContent).toBe("Show 2 more");
    expect(more.getAttribute("title")).toBeTruthy();
    await click(more);
    expect(rowsOf(node, "attn.git.pr-failing")).toHaveLength(5);
    expect(text(line(node, "attn.git.pr-failing"), ".attn-more")).toBe("Show fewer");
    await click(q(line(node, "attn.git.pr-failing"), ".attn-more"));
    expect(rowsOf(node, "attn.git.pr-failing")).toHaveLength(3);
  });

  it("shows one row on a phone", async () => {
    setPhone(true);
    const { context } = stubContext({ [LIST]: ok(overview(many(3, "pr-failing"))) });
    const node = await mount(context);
    expect(rowsOf(node, "attn.git.pr-failing")).toHaveLength(1);
    expect(text(line(node, "attn.git.pr-failing"), ".attn-more")).toBe("Show 2 more");
  });

  it("has no more button for three rows or fewer", async () => {
    const { context } = stubContext({ [LIST]: ok(overview(many(3, "pr-failing"))) });
    const node = await mount(context);
    expect(q(node, ".attn-more")).toBeNull();
  });

  it("draws a tone dot, a linked title, the meta and the buttons, each named", async () => {
    const { context, openExternal } = stubContext({ [LIST]: ok(sample()) });
    const node = await mount(context);
    const [row] = rowsOf(node, "attn.git.pr-failing");
    expect(row!.classList.contains("bh-need")).toBe(true);
    expect(row!.dataset.tone).toBe("urgent");
    expect(q(row!, ".bh-dot")?.classList.contains("bh-red")).toBe(true);
    const title = q<HTMLAnchorElement>(row!, ".attn-title")!;
    expect(title.tagName).toBe("A");
    expect(text(row!, ".attn-meta")).toBe("Git repos · Failing PRs · web · checks failing");
    expect(text(row!, ".attn-status")).toBe("Waiting on you");
    title.click();
    await flush();
    expect(openExternal).toHaveBeenCalledWith(title.href);
    for (const control of all(row!, "button.btn-icon")) {
      expect(control.getAttribute("aria-label")).toBeTruthy();
      expect(control.getAttribute("title")).toBeTruthy();
    }
    const review = q(row!, '[data-focus$=":review"]')!;
    expect(review.getAttribute("aria-label")).toMatch(/^Review /);
    expect(q(row!, '[data-focus$=":dismiss"]')?.getAttribute("aria-label")).toMatch(/^Dismiss /);
    expect(q(review, "svg")).not.toBeNull();
  });

  it("puts Open on GitHub in the dots menu of a Git row, and opens it through the host", async () => {
    const { context, openExternal } = stubContext({ [LIST]: ok(sample()) });
    const node = await mount(context);
    const [row] = rowsOf(node, "attn.git.pr-failing");
    const trigger = q<HTMLButtonElement>(row!, ".btn-menu")!;
    expect(trigger.getAttribute("aria-label")).toMatch(/^More actions for /);
    trigger.click();
    const open = all(row!, ".menu button").find((b) => b.textContent === "Open on GitHub")!;
    open.click();
    await flush();
    expect(openExternal).toHaveBeenCalledWith(expect.stringMatching(/^https:\/\/github\.com\//));
  });

  it("falls back to the URL as text when the host refuses the link", async () => {
    const { context, openExternal } = stubContext({ [LIST]: ok(sample()) });
    const node = await mount(context);
    openExternal.mockRejectedValueOnce(new Error("PopupBlocked"));
    const [row] = rowsOf(node, "attn.git.pr-failing");
    q(row!, ".attn-title")!.click();
    await flush();
    expect(text(row!, ".link-copy")).toMatch(/^https:\/\/github\.com\//);
  });

  it("draws a Box title as text and gives it the card's Clean up button with the offer's label", async () => {
    const { context } = stubContext({ [LIST]: ok(sample()) });
    const node = await mount(context);
    await click(q(line(node, "attn.box"), ".attn-parent-line"));
    await click(q(line(node, "attn.box.temp"), ".attn-line"));
    const [row] = rowsOf(node, "attn.box.temp");
    expect(q(row!, ".attn-title")?.tagName).toBe("SPAN");
    expect(q(row!, ".btn-menu")).toBeNull();
    const clean = q<HTMLButtonElement>(row!, ".attn-clean")!;
    expect(clean.textContent).toBe("Remove 268 folders");
    expect(clean.title).toBe(BOX_OFFER.hint);
    clean.click();
    expect(openCleanup).toHaveBeenCalledWith(BOX_OFFER, clean);
  });

  it("says so, under the row, when the card no longer lists that cleanup", async () => {
    vi.mocked(openCleanup).mockReturnValueOnce(false);
    const { context } = stubContext({ [LIST]: ok(sample()) });
    const node = await mount(context);
    await click(q(line(node, "attn.box"), ".attn-parent-line"));
    await click(q(line(node, "attn.box.temp"), ".attn-line"));
    const [row] = rowsOf(node, "attn.box.temp");
    await click(q(row!, ".attn-clean"));
    expect(text(row!, ".flash")).toMatch(/Read again/);
  });

  it("leaves Clean up off a Box row whose offer cannot run", async () => {
    const blocked = item("b", {
      source: "box",
      kind: "temp",
      url: "",
      offer: { ...BOX_OFFER, can_run: false, why_not: "The box is busy." },
    });
    const { context } = stubContext({ [LIST]: ok(overview([blocked])) });
    const node = await mount(context);
    expect(q(node, ".attn-clean")).toBeNull();
  });

  it("offers Review only when the viewer can start chats and the item allows it", async () => {
    const noChat = overview(many(1, "pr-failing"), { can_chat: false });
    const a = stubContext({ [LIST]: ok(noChat) });
    let node = await mount(a.context);
    expect(q(node, '[data-focus$=":review"]')).toBeNull();
    expect(q(node, '[data-focus$=":dismiss"]')).not.toBeNull();
    deactivateAttention();
    document.body.replaceChildren();
    document.body.appendChild(Object.assign(document.createElement("div"), { id: "root" }));
    const noReview = overview([item("x", { actions: ["dismiss"] })]);
    const b = stubContext({ [LIST]: ok(noReview) });
    node = await mount(b.context);
    expect(q(node, '[data-focus$=":review"]')).toBeNull();
  });

  it("leaves Dismiss off an item that cannot be dismissed", async () => {
    const { context } = stubContext({ [LIST]: ok(overview([item("x", { dismissible: false })])) });
    const node = await mount(context);
    expect(q(node, '[data-focus$=":dismiss"]')).toBeNull();
  });

  it("says why a dismissed item came back, in its meta", async () => {
    const { context } = stubContext({ [LIST]: ok(overview([item("x", { back: "3 days passed" })])) });
    const node = await mount(context);
    expect(text(node, ".attn-row .attn-meta")).toBe("Git repos · Failing PRs · web · checks failing · back: 3 days passed");
  });

  it("replaces the buttons with Claude is on it and an Open chat link when a chat holds the item", async () => {
    const held = item("held", {
      title: "Fix #5 Bump thing",
      working: { session_id: "s5", title: "Review of PR 5", path: "/s/s5" },
    });
    const { context, openSession } = stubContext({ [LIST]: ok(overview([held, item("free")])) });
    const node = await mount(context);
    const row = all(node, ".attn-row").find((r) => r.dataset.id === "held")!;
    expect(text(row, ".attn-working")).toContain("Claude is on it: Review of PR 5");
    expect(q(row, ".btn-icon")).toBeNull();
    await click(q(row, ".attn-open-chat"));
    expect(openSession).toHaveBeenCalledWith("s5");
    // The count leaves it out: one urgent item is free.
    expect(text(node, ".attn-counts")).toBe("1 urgent");
    expect(text(node, ".attn-parent-line .attn-n")).toBe("1 urgent");
    expect(text(row, ".attn-status")).toBe("Chat working");
  });

  it("says how many chats are working when nothing else counts", async () => {
    const hold = { session_id: "s5", title: "Review of PR 5", path: "/s/s5" };
    const { context } = stubContext({ [LIST]: ok(overview([item("held", { working: hold })])) });
    const node = await mount(context);
    expect(text(node, ".attn-counts")).toBe("0 to review, 1 chat working");
    expect(text(node, ".attn-parent-line .attn-n")).toBe("0 to review, 1 chat working");
    expect(node.textContent).not.toContain("held by chats");
  });

  it("counts an item a finished chat holds, and says the answer is in the chat", async () => {
    const hold = { session_id: "s6", title: "Review of PR 6", path: "/s/s6", status: "waiting" as const };
    const waiting = item("waits", { working: hold });
    const { context, openSession } = stubContext({ [LIST]: ok(overview([waiting])) });
    const node = await mount(context);
    const row = all(node, ".attn-row").find((r) => r.dataset.id === "waits")!;
    expect(text(row, ".attn-status")).toBe("Waiting on you");
    expect(text(row, ".attn-working")).toBe(
      "Review of PR 6 finished. Its answer, and any command to run, is in the chat. Open chat",
    );
    expect(text(node, ".attn-counts")).toBe("1 urgent");
    expect(q(row, '[data-focus$=":dismiss"]')).not.toBeNull();
    await click(q(row, ".attn-open-chat"));
    expect(openSession).toHaveBeenCalledWith("s6");
  });

  it("names where an item comes from; a dismissed row has no pill, one detail line and a one-line reason", async () => {
    const gone = item("gone", { dismissed: { at: "2026-11-02T10:00:00Z", reason: "later", expires_at: "2026-11-09T10:00:00Z" } });
    const { context } = stubContext({ [LIST]: ok(overview([item("a")], { dismissed: [gone] })) });
    const node = await mount(context);
    expect(originOf(item("b", { source: "box", kind: "units" }))).toBe("Box health · Failed units");
    const row = q(node, ".attn-dismissed-row")!;
    // The fold already says Dismissed (2026-11-08): no pill on each row.
    expect(q(row, ".attn-status")).toBeNull();
    expect(text(row, ".attn-meta")).toMatch(/ · back Nov \d+$/);
    expect(text(row, ".attn-meta")).not.toContain("later");
    // Restore is the compact Review all button, placed right after the title.
    const restore = q(row, ".attn-restore")!;
    expect(restore.classList.contains("attn-review-all")).toBe(true);
    expect(restore.previousElementSibling?.classList.contains("attn-title")).toBe(true);
    // The reason is one line; a click or tap opens the full text.
    const why = q<HTMLButtonElement>(row, ".attn-dismissed-reason")!;
    expect(why.textContent).toBe("later");
    expect(why.title).toBe("later");
    expect(why.getAttribute("aria-expanded")).toBe("false");
    await click(why);
    expect(why.getAttribute("aria-expanded")).toBe("true");
    // jsdom does no layout: the reason's 44 px tap target on a phone is read from the CSS.
    expect(css).toMatch(
      /@media \(pointer: coarse\), \(max-width: 520px\) \{\s*\.attn-dismissed-reason \{\s*min-height: 44px;/,
    );
  });
});

describe("Review", () => {
  const SESSION = "POST /v1/ae/attention/session";
  const id = "git:contoso/web#12";

  it("posts the session, then marks the row as worked with a link to the chat", async () => {
    let state = overview([item(id)]);
    const { context, calls, openSession } = stubContext({
      [LIST]: () => ok(state),
      [SESSION]: () => {
        state = overview([
          item(id, { working: { session_id: "s9", title: "Review chat", path: "/s/s9" } }),
        ]);
        return ok({ session_id: "s9", path: "/s/s9", existing: false, item_id: id });
      },
    });
    const node = await mount(context);
    await click(q(node, '[data-focus$=":review"]'));
    const post = calls.find((c) => c.path.endsWith("/session"))!;
    expect(post.body).toEqual({ id, action: "review" });
    const row = q(node, ".attn-row")!;
    expect(row.textContent).toContain("Claude is on it");
    expect(q(row, ".btn-icon")).toBeNull();
    await click(q(row, ".attn-open-chat"));
    expect(openSession).toHaveBeenCalledWith("s9");
  });

  it("shows the chat as started right away, before the server lists the item as held", async () => {
    const { context } = stubContext({
      [LIST]: ok(overview([item(id)])),
      [SESSION]: ok({ session_id: "s9", path: "/s/s9", existing: false, item_id: id }),
    });
    const node = await mount(context);
    await click(q(node, '[data-focus$=":review"]'));
    expect(text(node, ".attn-working")).toMatch(/^Chat started\. Claude is on it: /);
    expect(q(node, ".attn-open-chat")?.getAttribute("title")).toContain("/s/s9");
  });

  it("says a failure under the row", async () => {
    const { context } = stubContext({
      [LIST]: ok(overview([item(id)])),
      [SESSION]: failed(500, "boom", "The server could not start it."),
    });
    const node = await mount(context);
    await click(q(node, '[data-focus$=":review"]'));
    expect(text(node, ".attn-row .flash")).toMatch(/The server could not start it/);
  });

  const full = failed(409, "slots_full", "All slots are taken.", {
    working: [
      { session_id: "w1", title: "Build the badge" },
      { session_id: "w2", title: "Fix the sweep" },
    ],
    limit: 3,
    brief: "BRIEF: review PR 12",
  });

  it("asks first when the slots are full: who holds them, Copy brief, Start anyway", async () => {
    const { context, calls } = stubContext({ [LIST]: ok(overview([item(id)])), [SESSION]: full });
    const node = await mount(context);
    await click(q(node, '[data-focus$=":review"]'));
    const dialog = q(document.body, '[role="dialog"]')!;
    expect(dialog).not.toBeNull();
    expect(all(dialog, ".attn-holders li").map((li) => li.textContent)).toEqual(["Build the badge", "Fix the sweep"]);
    expect(dialog.textContent).toContain("All 3 slots are taken");
    expect(buttonByText(dialog, "Copy brief")).toBeTruthy();
    expect(buttonByText(dialog, "Start anyway (over the slot limit)")).toBeTruthy();
    // Only the one plain post so far: nothing started and no ride_unparked.
    const posts = calls.filter((c) => c.path.endsWith("/session"));
    expect(posts).toHaveLength(1);
    expect(posts[0]!.body).toEqual({ id, action: "review" });
  });

  it("copies the brief to the clipboard", async () => {
    const writeText = vi.fn(async () => undefined);
    Object.defineProperty(navigator, "clipboard", { value: { writeText }, configurable: true });
    const { context } = stubContext({ [LIST]: ok(overview([item(id)])), [SESSION]: full });
    const node = await mount(context);
    await click(q(node, '[data-focus$=":review"]'));
    await click(buttonByText(document.body, "Copy brief"));
    expect(writeText).toHaveBeenCalledWith("BRIEF: review PR 12");
    expect(text(document.body, ".attn-dlg-status")).toBe("Copied the brief.");
  });

  it("shows the brief as text when the clipboard is unavailable", async () => {
    Object.defineProperty(navigator, "clipboard", {
      value: { writeText: vi.fn(async () => Promise.reject(new Error("denied"))) },
      configurable: true,
    });
    const { context } = stubContext({ [LIST]: ok(overview([item(id)])), [SESSION]: full });
    const node = await mount(context);
    await click(q(node, '[data-focus$=":review"]'));
    await click(buttonByText(document.body, "Copy brief"));
    expect(text(document.body, ".attn-brief")).toBe("BRIEF: review PR 12");
  });

  it("sends ride_unparked only when Start anyway is pressed", async () => {
    let calls1 = 0;
    const { context, calls } = stubContext({
      [LIST]: ok(overview([item(id)])),
      [SESSION]: (call) => {
        calls1 += 1;
        const body = call.body as { ride_unparked?: boolean };
        return body.ride_unparked
          ? ok({ session_id: "s9", path: "/s/s9", existing: false, item_id: id })
          : full;
      },
    });
    const node = await mount(context);
    await click(q(node, '[data-focus$=":review"]'));
    expect(calls1).toBe(1);
    await click(buttonByText(document.body, "Start anyway (over the slot limit)"));
    const posts = calls.filter((c) => c.path.endsWith("/session"));
    expect(posts.map((c) => c.body)).toEqual([{ id, action: "review" }, { id, action: "review", ride_unparked: true }]);
    expect(q(document.body, '[role="dialog"]')).toBeNull();
    expect(text(node, ".attn-working")).toContain("Claude is on it");
  });
});

describe("Dismiss", () => {
  const id = "git:contoso/web#12";
  const DISMISS = "POST /v1/ae/attention/dismiss";
  const RESTORE = "POST /v1/ae/attention/restore";

  it("opens a dialog with the item, a reason box and the day it comes back", async () => {
    const { context } = stubContext({ [LIST]: ok(overview([item(id, { title: "Fix #12 Bump thing" })])) });
    const node = await mount(context);
    await click(q(node, '[data-focus$=":dismiss"]'));
    const dialog = q(document.body, '[role="dialog"]')!;
    expect(text(dialog, ".attn-dlg-item")).toBe("Fix #12 Bump thing");
    expect(text(dialog, ".attn-comes-back")).toBe("Comes back if a new commit still fails, or on Nov 6");
    const field = q<HTMLInputElement>(dialog, "input.attn-reason")!;
    expect(field.maxLength).toBe(200);
    expect(q(dialog, "label")?.getAttribute("for")).toBe(field.id);
    expect(buttonByText(dialog, "Dismiss").title).toBeTruthy();
  });

  it("uses the item's own days for the date", async () => {
    const { context } = stubContext({ [LIST]: ok(overview([item(id, { expires_days: 14 })])) });
    const node = await mount(context);
    await click(q(node, '[data-focus$=":dismiss"]'));
    expect(text(document.body, ".attn-comes-back")).toBe("Comes back if a new commit still fails, or on Nov 17");
  });

  it("posts the reason, closes, and offers Undo for 8 seconds", async () => {
    let listed = overview([item(id)]);
    const { context, calls } = stubContext({
      [LIST]: () => ok(listed),
      [DISMISS]: () => {
        listed = overview([], { dismissed: [item(id, { dismissed: { at: "2026-11-03T17:00:00Z", reason: "Waiting on CI", expires_at: "2026-11-06T17:00:00Z" } })] });
        return ok({ id, expires_at: "2026-11-06T17:00:00Z", reason: "Waiting on CI" });
      },
      [RESTORE]: () => {
        listed = overview([item(id)]);
        return ok({ id, restored: true });
      },
    });
    const node = await mount(context);
    await click(q(node, '[data-focus$=":dismiss"]'));
    q<HTMLInputElement>(document.body, "input.attn-reason")!.value = "Waiting on CI";
    await click(buttonByText(q(document.body, '[role="dialog"]')!, "Dismiss"));
    const post = calls.find((c) => c.path.endsWith("/attention/dismiss"))!;
    expect(post.path).toBe("/v1/ae/attention/dismiss");
    expect(post.body).toEqual({ id, reason: "Waiting on CI" });
    expect(q(document.body, '[role="dialog"]')).toBeNull();
    expect(q(node, ".attn-row")).toBeNull();
    expect(text(node, ".attn-undo-text")).toMatch(/^Dismissed: /);
    // Undo calls restore and brings the row back.
    await click(buttonByText(node, "Undo"));
    expect(calls.some((c) => c.path.endsWith("/attention/restore"))).toBe(true);
    expect(q(node, ".attn-undo")).toBeNull();
    expect(q(node, ".attn-row")).not.toBeNull();
  });

  it("takes the Undo away after the 8 seconds", async () => {
    const { context } = stubContext({
      [LIST]: ok(overview([item(id)])),
      [DISMISS]: ok({ id, expires_at: "2026-11-06T17:00:00Z", reason: "" }),
    });
    const node = await mount(context);
    await click(q(node, '[data-focus$=":dismiss"]'));
    await click(buttonByText(q(document.body, '[role="dialog"]')!, "Dismiss"));
    expect(UNDO_MS).toBe(8000);
    expect(q(node, ".attn-undo")).not.toBeNull();
    await vi.advanceTimersByTimeAsync(UNDO_MS - 1);
    expect(q(node, ".attn-undo")).not.toBeNull();
    await vi.advanceTimersByTimeAsync(2);
    expect(q(node, ".attn-undo")).toBeNull();
  });

  it("posts an empty body when no reason is typed", async () => {
    const { context, calls } = stubContext({
      [LIST]: ok(overview([item(id)])),
      [DISMISS]: ok({ id, expires_at: "2026-11-06T17:00:00Z", reason: "" }),
    });
    const node = await mount(context);
    await click(q(node, '[data-focus$=":dismiss"]'));
    await click(buttonByText(q(document.body, '[role="dialog"]')!, "Dismiss"));
    expect(calls.find((c) => c.path.endsWith("/attention/dismiss"))!.body).toEqual({ id });
  });

  it("offers a Box item the fixed reasons, not free text", async () => {
    const boxId = "box:temp:UserA";
    const { context, calls } = stubContext({
      [LIST]: ok(overview([item(boxId, { source: "box", kind: "temp", tone: "review", url: "", expires_days: 7, back_if: "the folders double" })])),
      ["POST /v1/ae/attention/dismiss"]: ok({ id: boxId, expires_at: "2026-11-10T17:00:00Z", reason: "Known" }),
    });
    const node = await mount(context);
    await click(q(line(node, "attn.box"), ".attn-parent-line"));
    await click(q(line(node, "attn.box.temp"), ".attn-line"));
    await click(q(node, '[data-focus$=":dismiss"]'));
    const select = q<HTMLSelectElement>(document.body, "select.attn-reason")!;
    expect(q(document.body, "input.attn-reason")).toBeNull();
    expect([...select.options].map((o) => o.textContent)).toEqual(["No reason", ...BOX_REASONS]);
    expect(text(document.body, ".attn-comes-back")).toBe("Comes back if the folders double, or on Nov 10");
    select.value = "Known";
    await click(buttonByText(q(document.body, '[role="dialog"]')!, "Dismiss"));
    expect(calls.find((c) => c.path.endsWith("/attention/dismiss"))!.body).toEqual({ id: boxId, reason: "Known" });
  });

  it("keeps the dialog open and says why when the server refuses", async () => {
    const { context } = stubContext({
      [LIST]: ok(overview([item(id)])),
      [DISMISS]: failed(422, "bad_reason", "That reason is not allowed."),
    });
    const node = await mount(context);
    await click(q(node, '[data-focus$=":dismiss"]'));
    await click(buttonByText(q(document.body, '[role="dialog"]')!, "Dismiss"));
    expect(q(document.body, '[role="dialog"]')).not.toBeNull();
    expect(text(document.body, ".attn-dlg-status")).toMatch(/That reason is not allowed/);
  });

  it("lists dismissed items in a closed fold with their reason, and Restore brings one back", async () => {
    let listed = overview([item("open-1")], {
      dismissed: [
        item(id, {
          title: "Fix #12 Bump thing",
          dismissed: { at: "2026-11-02T10:00:00Z", reason: "Fix planned", expires_at: "2026-11-09T10:00:00Z" },
        }),
      ],
    });
    const { context, calls } = stubContext({
      [LIST]: () => ok(listed),
      [RESTORE]: () => {
        listed = overview([item("open-1"), item(id)]);
        return ok({ id, restored: true });
      },
    });
    const node = await mount(context);
    const head = q<HTMLButtonElement>(node, ".attn-dismissed-head")!;
    expect(head.textContent).toBe("Dismissed (1)");
    expect(head.getAttribute("aria-expanded")).toBe("false");
    expect(q<HTMLElement>(node, ".attn-dismissed-list")?.hidden).toBe(true);
    await click(head);
    expect(q<HTMLElement>(node, ".attn-dismissed-list")?.hidden).toBe(false);
    const row = q(node, ".attn-dismissed-row")!;
    expect(row.textContent).toContain("Fix #12 Bump thing");
    expect(text(row, ".attn-dismissed-reason")).toBe("Fix planned");
    const restore = q(row, "button")!;
    expect(restore.getAttribute("aria-label")).toBe("Restore Fix #12 Bump thing");
    await click(restore);
    expect(calls.some((c) => c.path.endsWith("/attention/restore"))).toBe(true);
    expect(q(node, ".attn-dismissed")).toBeNull();
    // The fold stays as the viewer left it through the redraw.
    expect(all(node, ".attn-row")).toHaveLength(2);
  });

  it("keeps the Dismissed fold open through a redraw", async () => {
    const answer = overview([item("a")], {
      dismissed: [item(id, { dismissed: { at: "2026-11-02T10:00:00Z", reason: "", expires_at: "2026-11-09T10:00:00Z" } })],
    });
    const { context } = stubContext({ [LIST]: ok(answer) });
    const node = await mount(context);
    await click(q(node, ".attn-dismissed-head"));
    await click(buttonByText(node, "Collapse all"));
    expect(q<HTMLElement>(node, ".attn-dismissed-list")?.hidden).toBe(false);
    // No reason given: no reason line at all.
    expect(q(node, ".attn-dismissed-reason")).toBeNull();
  });
});

describe("the tree", () => {
  it("is a labelled tree of treeitems with aria-expanded, and groups for the rows", async () => {
    const { context } = stubContext({ [LIST]: ok(sample()) });
    const node = await mount(context);
    const tree = q(node, "[role=tree]")!;
    expect(tree.getAttribute("aria-label")).toBe("Needs you");
    const parent = line(node, "attn.git");
    expect(parent.getAttribute("role")).toBe("treeitem");
    expect(parent.getAttribute("aria-level")).toBe("1");
    expect(parent.getAttribute("aria-expanded")).toBe("true");
    const labelled = document.getElementById(parent.getAttribute("aria-labelledby")!)!;
    expect(labelled.textContent).toContain("Git repos");
    expect(labelled.textContent).toContain("3 urgent, 1 to review");
    const child = line(node, "attn.git.pr-failing");
    expect(child.getAttribute("aria-level")).toBe("2");
    const group = q(child, "[role=group]")!;
    expect(group.getAttribute("aria-label")).toBe("Failing PRs, 2 items");
    expect(all(group, "[role=treeitem]").every((r) => r.getAttribute("aria-level") === "3")).toBe(true);
    // A row is a leaf: it has no aria-expanded.
    expect(rowsOf(node, "attn.git.pr-failing")[0]!.hasAttribute("aria-expanded")).toBe(false);
    expect(q(parent, "[role=group]")?.getAttribute("aria-label")).toBe("Git repos groups");
  });

  it("has one tab stop (roving tabindex)", async () => {
    const { context } = stubContext({ [LIST]: ok(sample()) });
    const node = await mount(context);
    const stops = all(node, '[role=treeitem][tabindex="0"]');
    expect(stops).toHaveLength(1);
    expect(stops[0]).toBe(line(node, "attn.git"));
  });

  it("moves with the arrow keys, Home and End, opens with Right and closes with Left", async () => {
    const { context } = stubContext({ [LIST]: ok(sample()) });
    const node = await mount(context);
    const press = async (target: Element, key: string) => {
      target.dispatchEvent(new KeyboardEvent("keydown", { key, bubbles: true, cancelable: true }));
      await flush();
    };
    const git = line(node, "attn.git");
    git.focus();
    expect(document.activeElement).toBe(git);
    // Down goes to the first kind inside the open source.
    await press(git, "ArrowDown");
    const first = document.activeElement as HTMLElement;
    expect(first.dataset.line).toMatch(/^attn\.git\./);
    expect(first.tabIndex).toBe(0);
    expect(git.tabIndex).toBe(-1);
    await press(first, "ArrowUp");
    expect(document.activeElement).toBe(git);
    // End goes to the last visible item: the Box line is closed, so it is the last.
    await press(git, "End");
    expect((document.activeElement as HTMLElement).dataset.line).toBe("attn.box");
    await press(document.activeElement!, "Home");
    expect(document.activeElement).toBe(git);
    // Right on a closed line opens it; Left closes it; Left on a closed child goes to its parent.
    const box = line(node, "attn.box");
    box.focus();
    await press(box, "ArrowRight");
    expect(expanded(node, "attn.box")).toBe("true");
    expect(document.activeElement).toBe(line(node, "attn.box"));
    await press(document.activeElement!, "ArrowRight");
    expect((document.activeElement as HTMLElement).dataset.line).toBe("attn.box.temp");
    await press(document.activeElement!, "ArrowLeft");
    expect(document.activeElement).toBe(line(node, "attn.box"));
    await press(document.activeElement!, "ArrowLeft");
    expect(expanded(node, "attn.box")).toBe("false");
    // Enter toggles.
    await press(document.activeElement!, "Enter");
    expect(expanded(node, "attn.box")).toBe("true");
  });

  it("moves into the rows of an open kind", async () => {
    const { context } = stubContext({ [LIST]: ok(overview(many(2, "pr-failing"))) });
    const node = await mount(context);
    const kind = line(node, "attn.git.pr-failing");
    kind.focus();
    kind.dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowRight", bubbles: true, cancelable: true }));
    expect(document.activeElement).toBe(rowsOf(node, "attn.git.pr-failing")[0]);
    document.activeElement!.dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowDown", bubbles: true, cancelable: true }));
    expect(document.activeElement).toBe(rowsOf(node, "attn.git.pr-failing")[1]);
  });

  it("leaves the keys alone when a button inside a row has the focus", async () => {
    const { context } = stubContext({ [LIST]: ok(sample()) });
    const node = await mount(context);
    const button = q<HTMLButtonElement>(node, '[data-focus$=":dismiss"]')!;
    button.focus();
    const event = new KeyboardEvent("keydown", { key: "ArrowDown", bubbles: true, cancelable: true });
    button.dispatchEvent(event);
    expect(event.defaultPrevented).toBe(false);
    expect(document.activeElement).toBe(button);
  });
});

describe("the header and the states", () => {
  it("names the counts and offers the tree's tools", async () => {
    const big = overview(many(2, "pr-failing"), {
      counts: { urgent: 6, review: 17, by_source: { git: { urgent: 6, review: 17 } } },
    });
    const { context } = stubContext({ [LIST]: ok(big) });
    const node = await mount(context);
    expect(text(node, ".attn-h")).toBe("Needs you");
    expect(text(node, ".attn-counts")).toBe("6 urgent, 17 to review");
  });

  it("says how many items the server left out to fit the page", async () => {
    const { context } = stubContext({ [LIST]: ok(overview(many(2, "pr-failing"), { more: 7 })) });
    const node = await mount(context);
    expect(text(node, ".attn-more")).toBe("7 more not shown");
    const { context: whole } = stubContext({ [LIST]: ok(overview(many(2, "pr-failing"))) });
    expect(q(await mount(whole), ".attn-more")).toBeNull();
  });

  it("says Nothing needs you when there is nothing, with no tree and no tools", async () => {
    const { context } = stubContext({ [LIST]: ok(overview([])) });
    const node = await mount(context);
    expect(text(node, ".attn-empty")).toMatch(/^Nothing needs you\. Checked .+\.$/);
    expect(q(node, ".attn-empty svg")).not.toBeNull();
    expect(q(node, "[role=tree]")).toBeNull();
    expect(q(node, ".attn-tools button")).toBeNull();
    expect(text(node, ".attn-counts")).toBe("");
  });

  it("lists what could not be read", async () => {
    const answer = overview([item("a")], { unknown: ["Git repos could not be read; the count may be low"] });
    const { context } = stubContext({ [LIST]: ok(answer) });
    const node = await mount(context);
    expect(text(node, ".attn-unknown")).toBe("Git repos could not be read; the count may be low");
  });

  it("shows a route error with the page's notice, never a blank", async () => {
    const { context } = stubContext({ [LIST]: failed(500, "boom", "The server is down.") });
    const node = await mount(context);
    const note = q(node, ".attn-notes .notice-error")!;
    expect(note.getAttribute("role")).toBe("alert");
    expect(note.textContent).toBe("Needs you could not be read (The server is down.).");
    expect(q(node, "[role=tree]")).toBeNull();
  });

  it("keeps the last answer and says so when a later read fails", async () => {
    let answer: Reply = ok(sample());
    const { context } = stubContext({ [LIST]: () => answer });
    const node = await mount(context);
    answer = failed(502, "bad_gateway", "Bad gateway.");
    window.dispatchEvent(new Event("focus"));
    await flush();
    expect(q(node, "[role=tree]")).not.toBeNull();
    expect(text(node, ".attn-notes .notice-error")).toMatch(/shows the last answer; the refresh failed \(Bad gateway\.\)/);
  });

  it("says Reading before the first answer", async () => {
    let release: (reply: Reply) => void = () => undefined;
    const gate = new Promise<Reply>((resolve) => (release = resolve));
    const request = vi.fn(() => gate);
    const context = { server: { request }, navigation: {} } as unknown as ExtensionContext;
    const node = await mount(context);
    expect(text(node, ".attn-body")).toBe("Reading...");
    release(ok(overview([])));
    await flush();
    expect(text(node, ".attn-empty")).toMatch(/^Nothing needs you\./);
  });

  it("filters to one source from a card's link, and clears with the chip", async () => {
    const { context } = stubContext({ [LIST]: ok(sample()) });
    const node = await mount(context);
    expect(all(node, ".attn-parent")).toHaveLength(2);
    showInNeedsYou("box");
    await flush();
    expect(getFilter()).toBe("box");
    expect(all(node, ".attn-parent").map((p) => p.dataset.line)).toEqual(["attn.box"]);
    // The filtered source opens so the link lands on something.
    expect(expanded(node, "attn.box")).toBe("true");
    const chip = q(node, ".attn-chip")!;
    expect(chip.textContent).toBe("Showing 1 of 5 · Box health · Show all");
    expect(chip.getAttribute("aria-label")).toBe("Showing 1 of 5, only Box health. Show all");
    await click(chip);
    expect(getFilter()).toBeNull();
    expect(all(node, ".attn-parent")).toHaveLength(2);
    expect(q(node, ".attn-chip")).toBeNull();
  });

  it("scrolls the panel into view from a card's link", async () => {
    const { context } = stubContext({ [LIST]: ok(sample()) });
    const node = await mount(context);
    const scroll = vi.fn();
    node.scrollIntoView = scroll;
    showInNeedsYou("git");
    expect(scroll).toHaveBeenCalled();
    setFilter(null);
  });

  it("reads again on the page's refresh button and on focus", async () => {
    const { context, calls } = stubContext({ [LIST]: ok(sample()) });
    await mount(context);
    const reads = () => calls.filter((c) => c.path === "/v1/ae/attention").length;
    expect(reads()).toBe(1);
    const { refreshAll } = await import("../src/tiles");
    refreshAll();
    await flush();
    expect(reads()).toBe(2);
    window.dispatchEvent(new Event("focus"));
    await flush();
    expect(reads()).toBe(3);
  });

  it("does not redraw an unchanged answer, so an open menu stays open", async () => {
    const { context } = stubContext({ [LIST]: ok(sample()) });
    const node = await mount(context);
    const first = rowsOf(node, "attn.git.pr-failing")[0]!;
    window.dispatchEvent(new Event("focus"));
    await flush();
    expect(rowsOf(node, "attn.git.pr-failing")[0]).toBe(first);
  });
});

describe("reading again after the source cards finish", () => {
  it("re-reads the list once, debounced, when Git and Box finish a read", async () => {
    const { context, calls } = stubContext({ [LIST]: ok(sample()) });
    await mount(context);
    const reads = () => calls.filter((c) => c.path === "/v1/ae/attention").length;
    expect(reads()).toBe(1);
    // Both cards finish within the same moment: one more read, not two.
    sourceRead();
    sourceRead();
    expect(reads()).toBe(1);
    await vi.advanceTimersByTimeAsync(300);
    expect(reads()).toBe(2);
  });

  it("picks up what Git found after the panel's first, early read", async () => {
    let n = 0;
    const { context } = stubContext({
      [LIST]: () => ok(n++ === 0 ? overview([]) : sample()),
    });
    const node = await mount(context);
    expect(text(node, ".attn-empty") ?? text(node, ".attn-body")).toContain("Nothing needs you");
    sourceRead();
    await vi.advanceTimersByTimeAsync(300);
    expect(all(node, ".attn-row").length).toBeGreaterThan(0);
  });

  it("stops re-reading once the panel is gone", async () => {
    const { context, calls } = stubContext({ [LIST]: ok(sample()) });
    await mount(context);
    deactivateAttention();
    sourceRead();
    await vi.advanceTimersByTimeAsync(500);
    expect(calls.filter((c) => c.path === "/v1/ae/attention").length).toBe(1);
  });
});

describe("a re-read never tears down what the person is doing", () => {
  const id = "git:contoso/web#12";

  it("keeps an open Dismiss dialog and the reason typed into it", async () => {
    let n = 0;
    const { context } = stubContext({
      // Each read differs, so the panel really redraws its rows.
      [LIST]: () => ok(overview([item(id), item(`other-${n++}`)])),
    });
    const node = await mount(context);
    await click(q(node, '[data-focus$=":dismiss"]'));
    const field = q<HTMLInputElement>(document.body, "input.attn-reason")!;
    field.value = "Waiting on CI";
    field.focus();
    sourceRead();
    await vi.advanceTimersByTimeAsync(300);
    expect(n).toBeGreaterThan(1);
    const after = q<HTMLInputElement>(document.body, "input.attn-reason");
    expect(q(document.body, '[role="dialog"]')).not.toBeNull();
    expect(after).toBe(field);
    expect(after!.value).toBe("Waiting on CI");
    expect(document.activeElement).toBe(field);
  });

  it("keeps the Undo toast through a re-read", async () => {
    let listed = overview([item(id)]);
    const { context } = stubContext({
      [LIST]: () => ok(listed),
      "POST /v1/ae/attention/dismiss": () => {
        listed = overview([], { dismissed: [item(id, { dismissed: { at: "2026-11-03T17:00:00Z", reason: "", expires_at: "2026-11-06T17:00:00Z" } })] });
        return ok({ id, expires_at: "2026-11-06T17:00:00Z", reason: "" });
      },
    });
    const node = await mount(context);
    await click(q(node, '[data-focus$=":dismiss"]'));
    await click(buttonByText(q(document.body, '[role="dialog"]')!, "Dismiss"));
    expect(text(node, ".attn-undo-text")).toMatch(/^Dismissed: /);
    listed = overview([item("another")], { dismissed: listed.dismissed });
    sourceRead();
    await vi.advanceTimersByTimeAsync(300);
    expect(text(node, ".attn-undo-text")).toMatch(/^Dismissed: /);
  });

  it("keeps keyboard focus on the row control it was on", async () => {
    let n = 0;
    const { context } = stubContext({ [LIST]: () => ok(overview([item(id), item(`other-${n++}`)])) });
    const node = await mount(context);
    const review = q<HTMLButtonElement>(node, `[data-focus="${id}:dismiss"]`)!;
    review.focus();
    sourceRead();
    await vi.advanceTimersByTimeAsync(300);
    expect((document.activeElement as HTMLElement | null)?.dataset.focus).toBe(`${id}:dismiss`);
  });
});

describe("Review all", () => {
  const BATCH = "POST /v1/ae/attention/batch";
  const LINE = "attn.git.pr-failing";
  // Git has one group here, so the source button stands alone and the group repeats nothing.
  const reviewAll = (node: ParentNode) => q(node, `[data-focus="attn.git:review-all"]`);

  it("keeps its line table identical to the server's (one fixture, two ports)", () => {
    const spec = JSON.parse(linesText) as Record<string, Array<{ key: string; name: string; kinds: string[] }>>;
    expect(GROUPS).toEqual(spec);
  });

  it("shows on a line with two or more reviewable items, counting them", async () => {
    const { context } = stubContext({ [LIST]: ok(overview(many(3, "pr-failing"))) });
    const node = await mount(context);
    const button = reviewAll(node)!;
    expect(button.textContent).toBe("Review 3");
    expect(button.getAttribute("title")).toBe(
      "Opens one chat that reads every item in this source and reports one root cause per item. It changes nothing.",
    );
    expect(button.querySelector("svg.icon-list-check")).not.toBeNull();
  });

  it("is hidden for one item, for held or Review-less items, for a box-only admin and for Temp", async () => {
    const one = stubContext({ [LIST]: ok(overview(many(1, "pr-failing"))) });
    expect(reviewAll(await mount(one.context))).toBeNull();
    deactivateAttention();
    const heldOne = [
      ...many(1, "pr-failing"),
      item("pr-failing-h", { working: { session_id: "s1", title: "Chat", path: "/s/s1" } }),
    ];
    const held = stubContext({ [LIST]: ok(overview(heldOne)) });
    expect(reviewAll(await mount(held.context))).toBeNull();
    deactivateAttention();
    document.body.querySelector(".attn")?.remove();
    const noChat = stubContext({ [LIST]: ok(overview(many(3, "pr-failing"), { can_chat: false })) });
    expect(reviewAll(await mount(noChat.context))).toBeNull();
    deactivateAttention();
    document.body.querySelector(".attn")?.remove();
    const temp = many(3, "temp", {
      source: "box",
      tone: "review",
      url: "",
      actions: ["dismiss", "cleanup"],
    });
    const box = stubContext({ [LIST]: ok(overview(temp)) });
    const node = await mount(box.context);
    expect(q(node, '[data-focus$=":review-all"]')).toBeNull();
  });

  it("caps at 20 and says so", async () => {
    const { context, calls } = stubContext({
      [LIST]: ok(overview(many(34, "pr-failing"))),
      [BATCH]: ok({ session_id: "s9", path: "/s/s9", existing: false }),
    });
    const node = await mount(context);
    expect(reviewAll(node)!.textContent).toBe("Review 20 of 34");
    await click(reviewAll(node));
    const post = calls.find((c) => c.path.endsWith("/batch"))!;
    const body = post.body as { source: string; ids: string[] };
    expect(body.source).toBe("git");
    expect(body.ids).toEqual(many(34, "pr-failing").slice(0, 20).map((i) => i.id));
  });

  it("does not fold the line when clicked, and marks every item sent as Claude is on it", async () => {
    const { context, calls } = stubContext({
      [LIST]: ok(overview(many(2, "pr-failing"))),
      [BATCH]: ok({ session_id: "s9", path: "/s/s9", existing: false, item_ids: ["pr-failing-1", "pr-failing-2"] }),
    });
    const node = await mount(context);
    expect(expanded(node, LINE)).toBe("true");
    await click(reviewAll(node));
    expect(expanded(node, LINE)).toBe("true");
    expect(calls.find((c) => c.path.endsWith("/batch"))!.body).toEqual({
      source: "git",
      ids: ["pr-failing-1", "pr-failing-2"],
    });
    const working = all(node, ".attn-working");
    expect(working).toHaveLength(2);
    expect(working[0]!.textContent).toMatch(/^Chat started\. Claude is on it: /);
    expect(reviewAll(node)).toBeNull();
  });

  describe("on a source with several groups", () => {
    const boxItems = (scan: number, units: number) => [
      ...many(scan, "scan", { source: "box", tone: "review", url: "" }),
      ...many(units, "units", { source: "box", tone: "urgent", url: "" }),
    ];
    const sourceButton = (node: ParentNode) => q(node, '[data-focus="attn.box:review-all"]');

    it("shows a Review N source button for Box health with 2 items across two groups", async () => {
      const { context } = stubContext({ [LIST]: ok(overview(boxItems(1, 1))) });
      const node = await mount(context);
      expect(sourceButton(node)!.textContent).toBe("Review 2");
      // Each group has one item, so no group button; the source button is the only one.
      expect(all(node, ".attn-review-all")).toHaveLength(1);
    });

    it("is hidden at 1 item", async () => {
      const { context } = stubContext({ [LIST]: ok(overview(boxItems(1, 0))) });
      expect(sourceButton(await mount(context))).toBeNull();
    });

    it("keeps the group buttons when the source has several groups", async () => {
      const { context } = stubContext({ [LIST]: ok(overview(boxItems(2, 1))) });
      const node = await mount(context);
      expect(sourceButton(node)!.textContent).toBe("Review 3");
      expect(all(node, ".attn-review-all")).toHaveLength(2);
    });

    it("posts {source} and does not fold the source line", async () => {
      const { context, calls } = stubContext({
        [LIST]: ok(overview(boxItems(1, 1))),
        [BATCH]: ok({ session_id: "s9", path: "/s/s9", existing: false }),
      });
      const node = await mount(context);
      const folded = expanded(node, "attn.box");
      await click(sourceButton(node));
      const post = calls.find((c) => c.path.endsWith("/batch"))!;
      expect(post.body).toEqual({ source: "box", ids: ["units-1", "scan-1"] });
      expect(expanded(node, "attn.box")).toBe(folded);
    });
  });

  it("leaves the keyboard fold on the line itself", async () => {
    const { context } = stubContext({ [LIST]: ok(overview(many(2, "pr-failing"))) });
    const node = await mount(context);
    const treeitem = line(node, LINE);
    treeitem.focus();
    treeitem.dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowLeft", bubbles: true }));
    await flush();
    expect(expanded(node, LINE)).toBe("false");
    // A key on the button is the button's own, not the tree's.
    reviewAll(node)?.dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowLeft", bubbles: true }));
    await flush();
    expect(expanded(node, LINE)).toBe("false");
  });

  it("asks first when the slots are full and starts anyway with ride_unparked", async () => {
    const full = failed(409, "slots_full", "All slots are taken.", {
      working: [{ session_id: "w1", title: "Build the badge" }],
      limit: 3,
      brief: "BRIEF: review 2 items",
    });
    const { context, calls } = stubContext({
      [LIST]: ok(overview(many(2, "pr-failing"))),
      [BATCH]: (call) =>
        (call.body as { ride_unparked?: boolean }).ride_unparked
          ? ok({ session_id: "s9", path: "/s/s9", existing: false })
          : full,
    });
    const node = await mount(context);
    await click(reviewAll(node));
    const dialog = q(document.body, '[role="dialog"]')!;
    expect(all(dialog, ".attn-holders li").map((li) => li.textContent)).toEqual(["Build the badge"]);
    await click(buttonByText(dialog, "Start anyway (over the slot limit)"));
    const posts = calls.filter((c) => c.path.endsWith("/batch")).map((c) => c.body);
    expect(posts).toEqual([
      { source: "git", ids: ["pr-failing-1", "pr-failing-2"] },
      { source: "git", ids: ["pr-failing-1", "pr-failing-2"], ride_unparked: true },
    ]);
    expect(q(document.body, '[role="dialog"]')).toBeNull();
    expect(all(node, ".attn-working")).toHaveLength(2);
  });

  it("says a failure on the line", async () => {
    const { context } = stubContext({
      [LIST]: ok(overview(many(2, "pr-failing"))),
      [BATCH]: failed(409, "attention_batch_empty", "Every item in this line is dismissed, gone or has no Review; refresh the list."),
    });
    const node = await mount(context);
    await click(reviewAll(node));
    expect(text(line(node, "attn.git"), ".flash")).toMatch(/Every item in this line is dismissed/);
  });
});


// ── Fix chats and the Merge button (2026-11-05) ─────────────────────────────

describe("Fix", () => {
  const SESSION = "POST /v1/ae/attention/session";
  const id = "git:pr-failing:contoso/web#7";
  const NOTE = "Pushes a fix commit to the PR branch. No force-push. The merge later goes through ae-merge.";
  const fixable = (fields: Partial<AttentionItem> = {}) =>
    item(id, { actions: ["review", "fix", "dismiss"], fix_note: NOTE, fix_mode: "chat", ...fields });

  it("shows a Fix button between Review and Dismiss, titled with the server's approval note", async () => {
    const { context } = stubContext({ [LIST]: ok(overview([fixable()])) });
    const node = await mount(context);
    const buttons = all(q(node, ".attn-row")!, ".attn-actions > button");
    expect(buttons.map((b) => b.dataset.focus!.split(":").pop())).toEqual(["review", "fix", "dismiss"]);
    const fix = q(node, '[data-focus$=":fix"]')!;
    expect(fix.getAttribute("title")).toBe(NOTE);
    expect(fix.getAttribute("aria-label")).toMatch(/^Fix /);
    expect(q(fix, ".icon-tool")).not.toBeNull();
  });

  it("uses the terminal icon for a Fix that ends in a command", async () => {
    const unit = fixable({ source: "box", kind: "units", url: "", fix_mode: "command" });
    const { context } = stubContext({ [LIST]: ok(overview([unit])) });
    const node = await mount(context);
    expect(q(q(node, '[data-focus$=":fix"]')!, ".icon-terminal")).not.toBeNull();
  });

  it("is hidden when the item has no fix, for a viewer who cannot chat and while a chat holds it", async () => {
    const none = stubContext({ [LIST]: ok(overview([item(id)])) });
    let node = await mount(none.context);
    expect(q(node, '[data-focus$=":fix"]')).toBeNull();
    deactivateAttention();
    document.body.replaceChildren(Object.assign(document.createElement("div"), { id: "root" }));
    const boxOnly = stubContext({ [LIST]: ok(overview([fixable()], { can_chat: false })) });
    node = await mount(boxOnly.context);
    expect(q(node, '[data-focus$=":fix"]')).toBeNull();
    deactivateAttention();
    document.body.replaceChildren(Object.assign(document.createElement("div"), { id: "root" }));
    const held = fixable({ working: { session_id: "s1", title: "Review chat", path: "/s/s1" } });
    const onIt = stubContext({ [LIST]: ok(overview([held])) });
    node = await mount(onIt.context);
    expect(q(node, '[data-focus$=":fix"]')).toBeNull();
    expect(node.textContent).toContain("Claude is on it");
  });

  it("opens a confirm with the item, what the chat does and the approval; Cancel starts nothing", async () => {
    const { context, calls } = stubContext({ [LIST]: ok(overview([fixable()])) });
    const node = await mount(context);
    await click(q(node, '[data-focus$=":fix"]'));
    const dialog = q(document.body, '[role="dialog"]')!;
    expect(dialog).not.toBeNull();
    expect(text(dialog, ".attn-dlg-item")).toContain("Fix #");
    expect(text(dialog, ".attn-fix-job")).toMatch(/fixes the failing checks/);
    expect(text(dialog, ".attn-fix-note")).toBe(`Approval: ${NOTE}`);
    expect(buttonByText(dialog, "Start Fix chat").getAttribute("title")).toMatch(/still asks you/);
    await click(buttonByText(dialog, "Cancel"));
    expect(q(document.body, '[role="dialog"]')).toBeNull();
    expect(calls.filter((c) => c.path.endsWith("/session"))).toHaveLength(0);
  });

  it("posts the fix action on Start, then shows Claude is on it", async () => {
    let state = overview([fixable()]);
    const { context, calls } = stubContext({
      [LIST]: () => ok(state),
      [SESSION]: () => {
        state = overview([fixable({ working: { session_id: "s9", title: "Fix: contoso/web#7", path: "/s/s9" } })]);
        return ok({ session_id: "s9", path: "/s/s9", existing: false, item_id: id });
      },
    });
    const node = await mount(context);
    await click(q(node, '[data-focus$=":fix"]'));
    await click(buttonByText(q(document.body, '[role="dialog"]')!, "Start Fix chat"));
    const posts = calls.filter((c) => c.path.endsWith("/session"));
    expect(posts.map((c) => c.body)).toEqual([{ id, action: "fix" }]);
    expect(q(document.body, '[role="dialog"]')).toBeNull();
    expect(text(node, ".attn-working")).toMatch(/Claude is on it/);
  });

  it("asks about the slot limit and sends ride_unparked with the fix action only on Start anyway", async () => {
    const full = failed(409, "slots_full", "All slots are taken.", {
      working: [{ session_id: "w1", title: "Build the badge" }],
      limit: 3,
      brief: "BRIEF: fix PR 7",
    });
    const { context, calls } = stubContext({
      [LIST]: ok(overview([fixable()])),
      [SESSION]: (call) =>
        (call.body as { ride_unparked?: boolean }).ride_unparked
          ? ok({ session_id: "s9", path: "/s/s9", existing: false, item_id: id })
          : full,
    });
    const node = await mount(context);
    await click(q(node, '[data-focus$=":fix"]'));
    await click(buttonByText(q(document.body, '[role="dialog"]')!, "Start Fix chat"));
    const dialog = q(document.body, '[role="dialog"]')!;
    expect(dialog.textContent).toContain("No free slot for a Fix chat");
    await click(buttonByText(dialog, "Start anyway (over the slot limit)"));
    const posts = calls.filter((c) => c.path.endsWith("/session")).map((c) => c.body);
    expect(posts).toEqual([
      { id, action: "fix" },
      { id, action: "fix", ride_unparked: true },
    ]);
  });

  it("says a failure under the row in the Fix chat's own words", async () => {
    const { context } = stubContext({
      [LIST]: ok(overview([fixable()])),
      [SESSION]: failed(500, "boom", "The server could not start it."),
    });
    const node = await mount(context);
    await click(q(node, '[data-focus$=":fix"]'));
    await click(buttonByText(q(document.body, '[role="dialog"]')!, "Start Fix chat"));
    expect(text(node, ".attn-row .flash")).toMatch(/Could not start the Fix chat \(.*could not start it/);
  });

  it("keeps four icon buttons and the menu on a phone row, which wraps under the text", async () => {
    setPhone(true);
    const { context } = stubContext({ [LIST]: ok(overview([fixable()])) });
    const node = await mount(context);
    const row = q(node, ".attn-row")!;
    expect(all(row, ".attn-actions .btn-icon").length).toBe(4);
    // jsdom does no layout: the rules that keep the row inside 360 px are read as text.
    const rule = (selector: string) => {
      const start = css.indexOf(`${selector} {`);
      expect(start, selector).toBeGreaterThanOrEqual(0);
      return css.slice(start, css.indexOf("}", start));
    };
    expect(rule(".attn-actions")).toContain("flex-wrap: wrap");
    const phone = css.slice(css.indexOf("@media (pointer: coarse), (max-width: 520px) {\n  .attn .btn,"));
    expect(phone).toContain("grid-column: 2");
    expect(phone).toMatch(/width: 44px/);
    // 4 buttons at 44 px with 4 px gaps are 188 px, inside a 360 px screen's row.
    expect(4 * 44 + 3 * 4).toBeLessThan(360 - 2 * 16 - 8);
  });
});

describe("Merge on a ready pull request", () => {
  const SHA = "a1b2c3d4e5f60718293a4b5c6d7e8f9012345678";
  // prs.ts keeps its confirm and merge state by URL for the life of the module, so every test
  // gets a pull request number of its own.
  let num = 200;
  let URL = "";
  let CARD_PATH = "";
  let MERGE = "";
  beforeEach(() => {
    num += 1;
    URL = `https://github.com/contoso/web/pull/${num}`;
    CARD_PATH = `GET /v1/ae/prs/contoso/web/${num}`;
    MERGE = `POST /v1/ae/prs/contoso/web/${num}/merge`;
  });
  const ready = () =>
    item("pr-ready-1", {
      kind: "pr-ready",
      tone: "review",
      title: `Merge #${num} Bump thing`,
      url: URL,
      actions: ["review", "merge", "dismiss"],
    });
  const prCard = (extra: Partial<PrCard> = {}): PrCard => ({
    repo: "contoso/web",
    number: num,
    url: URL,
    title: "Bump thing",
    state: "open",
    draft: false,
    author: "someone",
    head_ref: "bump",
    base_ref: "main",
    head_sha: SHA,
    updated_at: "2026-11-03T10:00:00Z",
    ci: { state: "passing", passing: 6, failing: 0, pending: 0, total: 6 },
    mergeable: { state: "clean", detail: "No conflicts with the base branch." },
    review: { state: "approved", approvals: 1, changes_requested: 0, requested: [] },
    merge: { methods: ["squash", "merge"], default_method: "squash", allowed: true, reason: null },
    ...extra,
  });
  const mergeBtn = (node: ParentNode) => q<HTMLButtonElement>(node, '[data-focus$=":merge"]');
  const merged = () => ({ merged: true, repo: "contoso/web", number: num, method: "squash", sha: SHA, message: "ok", session: null });

  it("shows a Merge button with its tooltip to an admin, and not to a box-only viewer", async () => {
    const yes = stubContext({ [LIST]: ok(overview([ready()])) });
    let node = await mount(yes.context);
    expect(mergeBtn(node)!.getAttribute("title")).toBe("Merge (asks to confirm first)");
    expect(q(mergeBtn(node)!, ".icon-git-merge")).not.toBeNull();
    expect(q(node, '[data-focus$=":fix"]')).toBeNull();
    deactivateAttention();
    document.body.replaceChildren(Object.assign(document.createElement("div"), { id: "root" }));
    const no = stubContext({ [LIST]: ok(overview([ready()], { can_chat: false })) });
    node = await mount(no.context);
    expect(mergeBtn(node)).toBeNull();
  });

  it("reads the fresh card on a click and opens the confirm; nothing merges until Confirm", async () => {
    const { context, calls } = stubContext({
      [LIST]: ok(overview([ready()])),
      [CARD_PATH]: ok(prCard()),
      [MERGE]: ok(merged()),
    });
    const node = await mount(context);
    await click(mergeBtn(node));
    expect(calls.some((c) => c.path === `/v1/ae/prs/contoso/web/${num}`)).toBe(true);
    const row = q(node, ".attn-row")!;
    const confirm = q(row, ".pr-confirm")!;
    expect(confirm).not.toBeNull();
    expect(q(row, ".attn-actions")!.hidden).toBe(true);
    expect(text(row, ".attn-merge-facts")).toContain("6 of 6 checks passing");
    expect(calls.filter((c) => c.method === "POST" && c.path.endsWith("/merge"))).toHaveLength(0);
    await click(buttonByText(confirm, "Cancel"));
    expect(q(row, ".pr-confirm")).toBeNull();
    expect(calls.filter((c) => c.path.endsWith("/merge"))).toHaveLength(0);
  });

  it("merges through mergePr on confirm, pinned to the head, and marks the row merged", async () => {
    const { context, calls } = stubContext({
      [LIST]: ok(overview([ready()])),
      [CARD_PATH]: ok(prCard()),
      [MERGE]: ok(merged()),
    });
    const node = await mount(context);
    await click(mergeBtn(node));
    await click(buttonByText(q(node, ".pr-confirm")!, "Squash into main"));
    const posts = calls.filter((c) => c.path.endsWith("/merge"));
    expect(posts).toHaveLength(1);
    expect(posts[0]!.body).toEqual({ sha: SHA, method: "squash" });
    expect(text(node, ".attn-row")).toContain(`Merged #${num} (squashed) a1b2c3d`);
    // The refresh the merge triggers still lists the item (a cached read): the note stays.
    window.dispatchEvent(new Event("focus"));
    await flush();
    expect(text(node, ".attn-row")).toContain(`Merged #${num} (squashed)`);
    expect(mergeBtn(node)).toBeNull();
  });

  it("shows the card's reason with Merge off when the gate is shut", async () => {
    const shut = prCard({ merge: { methods: ["squash"], default_method: "squash", allowed: false, reason: "Checks are still running." } });
    const { context, calls } = stubContext({ [LIST]: ok(overview([ready()])), [CARD_PATH]: ok(shut) });
    const node = await mount(context);
    await click(mergeBtn(node));
    expect(mergeBtn(node)!.disabled).toBe(true);
    expect(mergeBtn(node)!.getAttribute("title")).toBe("Checks are still running.");
    expect(text(node, ".attn-row .pr-why")).toBe("Checks are still running.");
    expect(q(node, ".pr-confirm")).toBeNull();
    expect(calls.filter((c) => c.path.endsWith("/merge"))).toHaveLength(0);
  });

  it("says so under the row when the card cannot be read", async () => {
    const { context } = stubContext({
      [LIST]: ok(overview([ready()])),
      [CARD_PATH]: failed(502, "github", "GitHub did not answer."),
    });
    const node = await mount(context);
    await click(mergeBtn(node));
    expect(text(node, ".attn-row .flash")).toMatch(/Could not check whether it can merge/);
    expect(mergeBtn(node)!.disabled).toBe(false);
  });

  it("keeps the confirm open through a redraw", async () => {
    let read = "2026-11-03T20:00:00Z";
    const { context } = stubContext({
      [LIST]: () => ok({ ...overview([ready()]), read_at: read }),
      [CARD_PATH]: ok(prCard()),
    });
    const node = await mount(context);
    await click(mergeBtn(node));
    expect(q(node, ".pr-confirm")).not.toBeNull();
    read = "2026-11-03T20:05:00Z";
    const before = q(node, ".attn-row")!;
    // A changed answer redraws the whole tree: a new row node must come back with its confirm.
    window.dispatchEvent(new Event("focus"));
    await flush();
    const after = q(node, ".attn-row")!;
    expect(after).not.toBe(before);
    expect(q(after, ".pr-confirm")).not.toBeNull();
    expect(q(after, ".attn-actions")!.hidden).toBe(true);
  });

  it("shows the result of a merge that was in flight when the list redrew", async () => {
    let release: (reply: Reply) => void = () => undefined;
    const pending = new Promise<Reply>((resolve) => {
      release = resolve;
    });
    let read = "2026-11-03T20:00:00Z";
    const { context } = stubContext({
      [LIST]: () => ok({ ...overview([ready()]), read_at: read }),
      [CARD_PATH]: ok(prCard()),
      [MERGE]: (() => pending) as unknown as () => Reply,
    });
    const node = await mount(context);
    await click(mergeBtn(node));
    const asked = q(node, ".attn-row")!;
    await click(buttonByText(q(node, ".pr-confirm")!, "Squash into main"));
    read = "2026-11-03T20:09:00Z";
    window.dispatchEvent(new Event("focus"));
    await flush();
    // The redraw puts the row that asked back (the same node), with its merge still in flight.
    expect(all(node, ".attn-row")).toHaveLength(1);
    expect(q(node, ".attn-row")).toBe(asked);
    release(ok(merged()));
    await flush();
    await flush();
    expect(text(node, ".attn-row")).toContain(`Merged #${num} (squashed)`);
  });

  it("keeps an open confirm through a redraw past 60 seconds and reads the card again", async () => {
    let read = "2026-11-03T20:00:00Z";
    const { context, calls } = stubContext({
      [LIST]: () => ok({ ...overview([ready()]), read_at: read }),
      [CARD_PATH]: ok(prCard()),
    });
    const node = await mount(context);
    await click(mergeBtn(node));
    expect(q(node, ".pr-confirm")).not.toBeNull();
    const cardReads = () => calls.filter((c) => c.path === `/v1/ae/prs/contoso/web/${num}`).length;
    expect(cardReads()).toBe(1);
    vi.setSystemTime(new Date(NOON.getTime() + 61_000));
    read = "2026-11-03T20:05:00Z";
    window.dispatchEvent(new Event("focus"));
    await flush();
    await flush();
    expect(q(node, ".pr-confirm")).not.toBeNull();
    expect(cardReads()).toBe(2);
    expect(text(node, ".attn-row .flash")).toBeUndefined();
  });

  it("says so, and forgets the confirm, when the card read behind it shuts the gate", async () => {
    let allowed = true;
    let read = "2026-11-03T20:00:00Z";
    const { context } = stubContext({
      [LIST]: () => ok({ ...overview([ready()]), read_at: read }),
      [CARD_PATH]: () =>
        ok(
          prCard(
            allowed
              ? {}
              : { merge: { methods: ["squash"], default_method: "squash", allowed: false, reason: "Checks are running." } },
          ),
        ),
    });
    const node = await mount(context);
    await click(mergeBtn(node));
    allowed = false;
    vi.setSystemTime(new Date(NOON.getTime() + 61_000));
    read = "2026-11-03T20:07:00Z";
    window.dispatchEvent(new Event("focus"));
    await flush();
    await flush();
    expect(q(node, ".pr-confirm")).toBeNull();
    expect(text(node, ".attn-row .flash")).toBe("The merge confirm closed; press Merge again.");
    const { confirmIsOpen } = await import("../src/prs");
    expect(confirmIsOpen(URL)).toBe(false);
  });

  it("drops the confirm when the list no longer has that pull request", async () => {
    let items = [ready()];
    const { context } = stubContext({
      [LIST]: () => ok(overview(items)),
      [CARD_PATH]: ok(prCard()),
    });
    const node = await mount(context);
    await click(mergeBtn(node));
    const { confirmIsOpen } = await import("../src/prs");
    expect(confirmIsOpen(URL)).toBe(true);
    items = [];
    window.dispatchEvent(new Event("focus"));
    await flush();
    expect(confirmIsOpen(URL)).toBe(false);
  });

  const recordPath = () => `GET /v1/ae/prs/contoso/web/${num}/compliance`;
  const report = (extra: object = {}) => ({
    body: "ok",
    verdict: "approved",
    url: "https://github.com/contoso/web/pull/1#c",
    updated_at: "2026-11-03T09:00:00Z",
    reviewed_sha: SHA,
    revision: "current",
    ...extra,
  });

  it("shows the review record's verdict and that it is current, as display only", async () => {
    const { context, calls } = stubContext({
      [LIST]: ok(overview([ready()])),
      [CARD_PATH]: ok(prCard()),
      [recordPath()]: ok({ report: report() }),
    });
    const node = await mount(context);
    await click(mergeBtn(node));
    expect(text(node, ".attn-merge-record")).toBe("Review record: approved, current with the head commit");
    expect(q(node, ".pr-confirm")).not.toBeNull();
    expect(calls.filter((c) => c.path.endsWith("/merge"))).toHaveLength(0);
  });

  it("shows a stale record, a blocked one and no record at all; a failed read never blocks Merge", async () => {
    const run = async (reply: Reply | undefined) => {
      num += 1;
      URL = `https://github.com/contoso/web/pull/${num}`;
      CARD_PATH = `GET /v1/ae/prs/contoso/web/${num}`;
      deactivateAttention();
      document.body.replaceChildren(Object.assign(document.createElement("div"), { id: "root" }));
      const routes: Record<string, Reply> = { [CARD_PATH]: ok(prCard()) };
      if (reply) routes[recordPath()] = reply;
      const { context } = stubContext({ [LIST]: ok(overview([ready()])), ...routes });
      const node = await mount(context);
      await click(mergeBtn(node));
      return { words: text(node, ".attn-merge-record"), confirm: q(node, ".pr-confirm") };
    };
    const stale = await run(ok({ report: report({ revision: "stale", verdict: "blocked" }) }));
    expect(stale.words).toBe("Review record: blocked, stale: the head commit changed since");
    expect((await run(ok({ report: null }))).words).toBe("No review record on this PR");
    const failedRead = await run(failed(502, "github", "GitHub did not answer."));
    expect(failedRead.words).toMatch(/^Review record not read \(/);
    expect(failedRead.confirm).not.toBeNull();
  });
});

describe("Fix meeting a Review that already holds the item", () => {
  const SESSION = "POST /v1/ae/attention/session";
  const id = "git:pr-failing:contoso/web#7";
  const base = () => item(id, { actions: ["review", "fix", "dismiss"], fix_note: "Note." });

  it("flashes that a Review holds it, links the chat and does not show a started Fix", async () => {
    const { context } = stubContext({
      [LIST]: ok(overview([base()])),
      [SESSION]: ok({ session_id: "s5", path: "/s/s5", existing: true, item_id: id, holder_action: "review" }),
    });
    const node = await mount(context);
    await click(q(node, '[data-focus$=":fix"]'));
    await click(buttonByText(q(document.body, '[role="dialog"]')!, "Start Fix chat"));
    const row = q(node, ".attn-row")!;
    expect(text(row, ".flash")).toBe(
      "A Review chat already holds this item; open it, or wait for it to finish before Fix.",
    );
    expect(q(row, ".attn-open-chat")).not.toBeNull();
    expect(text(row, ".attn-working")).not.toMatch(/^Chat started/);
  });

  it("says nothing extra when the holder is the same kind of chat", async () => {
    const { context } = stubContext({
      [LIST]: ok(overview([base()])),
      [SESSION]: ok({ session_id: "s5", path: "/s/s5", existing: true, item_id: id, holder_action: "fix" }),
    });
    const node = await mount(context);
    await click(q(node, '[data-focus$=":fix"]'));
    await click(buttonByText(q(document.body, '[role="dialog"]')!, "Start Fix chat"));
    expect(q(node, ".attn-row .flash")).toBeNull();
    expect(q(node, ".attn-open-chat")).not.toBeNull();
  });

  it("flashes the other way when a Review click meets a Fix", async () => {
    const { context } = stubContext({
      [LIST]: ok(overview([base()])),
      [SESSION]: ok({ session_id: "s5", path: "/s/s5", existing: true, item_id: id, holder_action: "fix" }),
    });
    const node = await mount(context);
    await click(q(node, '[data-focus$=":review"]'));
    expect(text(node, ".attn-row .flash")).toBe(
      "A Fix chat already holds this item; open it, or wait for it to finish before Review.",
    );
  });
});


describe("the verdict, the Dashboard link and the empty state (2026-11-06)", () => {
  const counts = (urgent: number, review: number) => ({ urgent, review, by_source: {} });
  const verdict = () => {
    const node = document.createElement("p");
    mountVerdict(node);
    return node;
  };
  const okPill = () =>
    setTile({ id: "release", value: "v1", note: "up to date", health: "ok" });
  /** Every member of every group has reported, all ok. */
  const allOk = () => {
    for (const domain of DOMAINS) {
      for (const id of domain.members) setTile({ id, value: "fine", note: "", health: "ok" });
    }
  };

  afterEach(() => {
    resetTiles();
    resetAttention();
  });

  it("says nothing until the Needs you list has been read", () => {
    okPill();
    expect(verdict().textContent).toBe("");
  });

  it("counts urgent and review, with urgent in its own span", () => {
    const node = verdict();
    setAttention(overview([item("a")], { counts: counts(2, 3) }));
    expect(node.textContent).toBe("2 urgent, 3 to review.");
    expect(node.querySelector(".verdict-urgent")?.textContent).toBe("2 urgent");
    setAttention(overview([item("a")], { counts: counts(2, 0) }));
    expect(node.textContent).toBe("2 urgent.");
    setAttention(overview([item("a")], { counts: counts(0, 1) }));
    expect(node.textContent).toBe("1 to review.");
    expect(node.querySelector(".verdict-urgent")).toBeNull();
  });

  it("says Nothing needs you, and adds that everything else is fine only when every group has reported ok", () => {
    const node = verdict();
    setAttention(overview([], { counts: counts(0, 0) }));
    expect(node.textContent).toBe("Nothing needs you.");
    okPill();
    // One group reported; the others have not, so "fine" is not yet earned.
    expect(node.textContent).toBe("Nothing needs you.");
    allOk();
    expect(node.textContent).toBe("Nothing needs you. Everything else is fine.");
    setTile({ id: "host", value: "Down", note: "not connected", health: "attention" });
    expect(node.textContent).toBe("Nothing needs you.");
  });

  it("never pairs 'fine' with 'could not be read'", () => {
    const node = verdict();
    allOk();
    setAttention(overview([item("a")], { counts: counts(0, 1), unknown: ["Git repos could not be read"] }));
    expect(node.textContent).toBe("1 to review. Some sources could not be read.");
  });

  it("drops the Nothing needs you and fine claims when the newest read failed", () => {
    const node = verdict();
    allOk();
    setAttention(overview([], { counts: counts(0, 0) }));
    expect(node.textContent).toBe("Nothing needs you. Everything else is fine.");
    setAttention(null, new Error("boom"));
    expect(node.textContent).toBe("0 to review. Last answer.");
    setAttention(overview([item("a")], { counts: counts(1, 2) }));
    setAttention(null, new Error("boom"));
    expect(node.textContent).toBe("1 urgent, 2 to review. Last answer.");
    resetAttention();
    const fresh = verdict();
    setAttention(null, new Error("never read"));
    expect(fresh.textContent).toBe("");
  });

  it("never says everything else is fine in the box-only view", () => {
    const node = document.createElement("p");
    mountVerdict(node, { fine: false });
    allOk();
    setAttention(overview([], { counts: counts(0, 0) }));
    expect(node.textContent).toBe("Nothing needs you.");
  });

  it("shows the Dashboard link with the count of chats waiting, and opens the page", async () => {
    const waiting = (n: number) => ({
      sessions: { ok: true, items: Array.from({ length: n }, (_, i) => ({ id: `s${i}`, kind: "gate" })), counts: {} },
      lot: { ok: true, items: [] },
      pulls: { ok: true, items: [] },
    });
    const { context, openPage } = stubContext({ [LIST]: ok(overview([])), "GET /v1/ae/your-move": ok(waiting(2)) });
    const node = await mount(context);
    const link = q(node, ".attn-wait-link")!;
    expect(link.textContent).toBe("2 chats waiting on you");
    expect(q(node, ".attn-wait")!.hidden).toBe(false);
    await click(link);
    expect(openPage).toHaveBeenCalledWith(DASHBOARD_PAGE_ID);
    expect(waitingText(1)).toBe("1 chat waiting on you");
  });

  it("applies only the newest Your move read, and reads it again on focus", async () => {
    const waiting = (n: number) =>
      ok({
        sessions: { ok: true, items: Array.from({ length: n }, (_, i) => ({ id: `s${i}`, kind: "gate" })), counts: {} },
        lot: { ok: true, items: [] },
        pulls: { ok: true, items: [] },
      });
    const replies: Array<(reply: Reply) => void> = [];
    const route = () => new Promise<Reply>((resolve) => replies.push(resolve));
    const { context } = stubContext({ [LIST]: ok(overview([])), "GET /v1/ae/your-move": route as never });
    const node = await mount(context);
    expect(replies).toHaveLength(1);
    window.dispatchEvent(new Event("focus"));
    await flush();
    expect(replies).toHaveLength(2);
    replies[1](waiting(3));
    await flush();
    replies[0](waiting(1));
    await flush();
    expect(text(node, ".attn-wait-link")).toBe("3 chats waiting on you");
  });

  it("hides the Dashboard link at zero and when the read fails", async () => {
    const none = { sessions: { ok: true, items: [], counts: {} }, lot: { ok: true, items: [] }, pulls: { ok: true, items: [] } };
    const { context } = stubContext({ [LIST]: ok(overview([])), "GET /v1/ae/your-move": ok(none) });
    const node = await mount(context);
    expect(q(node, ".attn-wait")!.hidden).toBe(true);
    expect(q(node, ".attn-wait-link")).toBeNull();
    const broken = stubContext({ [LIST]: ok(overview([])) });
    const again = await mount(broken.context);
    expect(q(again, ".attn-wait")!.hidden).toBe(true);
  });

  it("keeps the plain empty line when a source could not be read", async () => {
    const { context } = stubContext({ [LIST]: ok(overview([], { unknown: ["Box health could not be read"] })) });
    const node = await mount(context);
    expect(text(node, ".attn-empty")).toBe("Nothing needs you.");
    expect(emptyText("not a date")).toBe("Nothing needs you.");
  });
});

describe("the ten sources (2026-11-06, git-mine 2026-11-08)", () => {
  const SOURCES = ["git", "git-mine", "box", "host", "cost", "databricks", "secrets", "vault", "accounts", "family"] as const;

  it("names every source, and the groups and the names cover the same ten", () => {
    expect(Object.keys(SOURCE_NAMES).sort()).toEqual([...SOURCES].sort());
    expect(Object.keys(GROUPS).sort()).toEqual([...SOURCES].sort());
    expect(SOURCE_NAMES).toMatchObject({
      "git-mine": "My Git repos",
      secrets: "Secrets on box",
      vault: "Key Vault",
      cost: "Cost targets",
      databricks: "Databricks credit",
      host: "Box host",
      accounts: "Accounts",
      family: "Spend families",
    });
  });

  it("puts the panel's parents in the server's order", () => {
    const kinds: Record<string, string> = {
      git: "pr-failing",
      "git-mine": "pr-failing",
      box: "heat",
      host: "host-failed",
      cost: "cost-over",
      databricks: "credit-over",
      secrets: "secret-soon",
      vault: "vault-apart",
      accounts: "account-full",
      family: "family-over",
    };
    // Built in reverse, to prove the order is the table's and not the input's.
    const items = [...SOURCES].reverse().map((source) => item(`${source}-1`, { source, kind: kinds[source]! }));
    const blocks = buildBlocks(items);
    expect(blocks.map((b) => b.name)).toEqual(SOURCES.map((source) => SOURCE_NAMES[source]));
    for (const block of blocks) expect(block.groups.map((g) => g.name)).not.toContain("Other");
  });
});

describe("liveNeedsLink listeners (2026-11-06)", () => {
  afterEach(() => resetAttention());

  it("stops once its slot has left the page", () => {
    resetAttention();
    const slot = liveNeedsLink("host");
    document.body.appendChild(slot);
    setAttention(overview([item("h1", { source: "host", kind: "host-failed" })]));
    expect(slot.textContent).toBe("1 in Needs you");
    slot.remove();
    setAttention(overview([]));
    const frozen = slot.textContent;
    setAttention(overview([item("h2", { source: "host", kind: "host-failed" })]));
    expect(slot.textContent).toBe(frozen);
  });

  it("stops after a few reads when it was never put on the page", () => {
    resetAttention();
    const slot = liveNeedsLink("host");
    for (let n = 0; n < 5; n += 1) setAttention(overview([]));
    const frozen = slot.textContent;
    setAttention(overview([item("h1", { source: "host", kind: "host-failed" })]));
    expect(slot.textContent).toBe(frozen);
  });
});

describe("personal Git items (git-mine, 2026-11-08)", () => {
  const mineItem = (id: string, fields: Partial<AttentionItem> = {}) =>
    item(`git-mine:octo-user:${id}`, {
      source: "git-mine",
      kind: "pr-ready",
      tone: "review",
      title: `Merge ${id}`,
      url: `https://github.com/example-org/mine/pull/${id.replace(/\D/g, "") || "1"}`,
      // A future answer that listed every action must still get Dismiss only.
      actions: ["review", "fix", "merge", "dismiss"],
      ...fields,
    });
  const labelled = (node: ParentNode, prefix: string) => all(node, `button[aria-label^="${prefix}"]`);

  it("shows Dismiss and Open on GitHub, and no Review, Fix or Merge, whatever the answer lists", async () => {
    const { context } = stubContext({
      [LIST]: ok(overview([mineItem("a1"), mineItem("a2")], { sources: ["git", "git-mine"] })),
    });
    const node = await mount(context);
    expect(line(node, "attn.git-mine")).not.toBeNull();
    await click(line(node, "attn.git-mine"));
    const rows = all(node, ".attn-row").filter((row) => row.textContent?.includes("Merge a"));
    expect(rows).toHaveLength(2);
    for (const row of rows) {
      expect(labelled(row, "Dismiss ")).toHaveLength(1);
      expect(labelled(row, "More actions for ")).toHaveLength(1);
      expect(labelled(row, "Review ")).toHaveLength(0);
      expect(labelled(row, "Fix ")).toHaveLength(0);
      expect(labelled(row, "Merge ")).toHaveLength(0);
    }
  });

  it("leaves personal items out of Review all, even with two or more", async () => {
    const { context } = stubContext({
      [LIST]: ok(overview([mineItem("a1"), mineItem("a2"), mineItem("a3")], { sources: ["git", "git-mine"] })),
    });
    const node = await mount(context);
    expect(all(node, ".attn-review-all")).toHaveLength(0);
  });

  it("names the group My Git repos, and a company item beside it keeps its buttons", async () => {
    const company = item("pr-failing-9", { actions: ["review", "fix", "dismiss"] });
    const { context } = stubContext({
      [LIST]: ok(overview([company, mineItem("a1")], { sources: ["git", "git-mine"] })),
    });
    const node = await mount(context);
    expect(node.textContent).toContain("My Git repos");
    expect(labelled(node, "Review ").length).toBeGreaterThan(0);
  });
});
