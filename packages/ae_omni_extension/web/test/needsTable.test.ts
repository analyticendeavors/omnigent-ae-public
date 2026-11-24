// The Admin Center's Needs you table (2026-11-14, concept D2; needsTable.ts): the groups and how
// they fold, the columns, the composition bar, the opened row and its panel, and the unreadable-source
// banner. attention.test.ts keeps covering the loading and the actions; every call here is mocked.
import type { ExtensionContext } from "@omnigent/extension-sdk";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { AttentionOverview } from "../src/api";
import { activateAttention, attentionSection, deactivateAttention, resetAttentionSeen } from "../src/attention";
import { getFilter, resetAttention } from "../src/attentionStore";
import { resetFolds, saveFold } from "../src/fold";
import { createNeedsTable } from "../src/needsTable";
import { resetTiles } from "../src/tiles";
import { item, many, overview } from "./attentionFixtures";

vi.mock("../src/boxHealth", () => ({ openCleanup: vi.fn(() => true) }));

let openRefused = false;
const LIST = "GET /v1/ae/attention";
const flush = () => vi.advanceTimersByTimeAsync(0);
const all = (node: ParentNode, sel: string) => [...node.querySelectorAll<HTMLElement>(sel)];
const q = (node: ParentNode, sel: string) => node.querySelector<HTMLElement>(sel);

function stub(answer: AttentionOverview | (() => AttentionOverview)) {
  const request = vi.fn(async ({ method, path }: { method: string; path: string }) => {
    if (`${method} ${path}` === LIST) return { ok: true, status: 200, body: typeof answer === "function" ? answer() : answer };
    return { ok: false, status: 500, body: { error: { code: "unexpected", message: `unexpected ${method} ${path}` } } };
  });
  return {
    request,
    context: {
      view: "fabric",
      server: { request },
      navigation: { openSession: vi.fn(), openExternal: vi.fn(async () => { if (openRefused) throw new Error("URL was not provided by the host"); }), openPage: vi.fn() },
    } as unknown as ExtensionContext,
  };
}

const openTab = vi.fn();
const openCard = vi.fn();
let width = 1000; // the table container's width, in px, for the column count
let table = createNeedsTable({ openTab, openCard, hasTab: () => true });

async function mount(answer: AttentionOverview | (() => AttentionOverview), hasTabs = true): Promise<HTMLElement> {
  table = createNeedsTable({ openTab, openCard, hasTab: () => hasTabs, measure: () => width });
  const node = attentionSection();
  document.getElementById("root")?.appendChild(node);
  activateAttention(stub(answer).context, node, { waiting: false, table: table.hooks });
  await flush();
  return node;
}

const SAMPLE = (): AttentionOverview =>
  overview([
    ...many(2, "pr-failing"),
    item("g-ready", { kind: "pr-ready", tone: "review", title: "Merge #12 Bump thing", actions: ["review", "merge", "dismiss"] }),
    item("box-1", { source: "box", kind: "heat", url: "", title: "CPU at 98C", actions: ["review", "dismiss"] }),
    item("v-1", { source: "vault", kind: "vault-soon", tone: "review", url: "", title: "Key expires", actions: ["review"] }),
    item("v-2", { source: "vault", kind: "vault-soon", tone: "review", url: "", title: "Another key", actions: ["review"] }),
    item("acc-1", { source: "accounts", kind: "account-full", tone: "review", url: "", title: "Window full", actions: ["review"] }),
  ]);

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
  vi.setSystemTime(new Date("2026-11-14T12:00:00Z"));
  openTab.mockClear();
  openCard.mockClear();
  width = 1000;
  openRefused = false;
});

afterEach(() => {
  deactivateAttention();
  vi.useRealTimers();
});

describe("the groups", () => {
  it("draws area rows and source rows as disclosure buttons with aria-expanded and aria-controls, not a listbox", async () => {
    const node = await mount(SAMPLE());
    expect(node.querySelector('[role="tree"], [role="listbox"], [role="treeitem"]')).toBeNull();
    const buttons = all(node, ".nt-grp-btn");
    expect(buttons.length).toBeGreaterThanOrEqual(7);
    for (const button of buttons) {
      expect(button.tagName).toBe("BUTTON");
      expect(["true", "false"]).toContain(button.getAttribute("aria-expanded"));
      expect(document.getElementById(button.getAttribute("aria-controls")!)).not.toBeNull();
    }
  });

  it("opens every area and no source on first sight, urgent or not, and a closed source says what it holds", async () => {
    const node = await mount(SAMPLE());
    const area = (id: string) => q(node, `[data-line="nt.area.${id}"]`)!;
    const toggle = (el: HTMLElement) => q(el, ":scope > .nt-grp-head > .nt-grp-btn")!.getAttribute("aria-expanded");
    expect(["ship", "box", "cloud", "money"].map((id) => toggle(area(id)))).toEqual(["true", "true", "true", "true"]);
    // The Overview shows the areas with their source lines; no source shows rows until it is opened.
    expect(all(node, ".nt-source > .nt-grp-head .nt-grp-btn").every((b) => b.getAttribute("aria-expanded") === "false")).toBe(true);
    expect(all(node, ".nt-rows:not([hidden]) .nt-row")).toHaveLength(0);
    // A closed source names its first item and "N more".
    expect(q(node, '[data-line="attn.vault"] .nt-preview')!.textContent).toBe("Key expires and 1 more");
    expect(q(node, '[data-line="attn.vault"] .nt-rows')!.hidden).toBe(true);
  });

  it("shows at most five rows of an open source and a Show N more button that shows the rest", async () => {
    saveFold("attn.git", true);
    const node = await mount(overview(many(8, "pr-failing")));
    const git = q(node, '[data-line="attn.git"]')!;
    expect(all(git, ".nt-row")).toHaveLength(5);
    const more = q(git, ".nt-more")!;
    expect(more.textContent).toBe("Show 3 more");
    more.click();
    await flush();
    expect(all(q(node, '[data-line="attn.git"]')!, ".nt-row")).toHaveLength(8);
    expect(q(node, '[data-line="attn.git"] .nt-more')!.textContent).toBe("Show fewer");
  });

  it("opens a closed source from its button and remembers it across a reload, and so for an area closed on purpose", async () => {
    let node = await mount(SAMPLE());
    q(node, '[data-line="attn.vault"] .nt-grp-btn')!.click();
    q(node, '[data-line="nt.area.money"] .nt-grp-btn')!.click();
    await flush();
    expect(q(node, '[data-line="attn.vault"] .nt-rows')!.hidden).toBe(false);
    expect(q(node, '[data-line="nt.area.money"] .nt-grp-body')!.hidden).toBe(true);
    deactivateAttention();
    node.remove();
    resetAttention();
    node = await mount(SAMPLE());
    expect(q(node, '[data-line="attn.vault"] .nt-rows')!.hidden).toBe(false);
    expect(q(node, '[data-line="nt.area.money"] .nt-grp-body')!.hidden).toBe(true);
  });

  it("keeps Expand urgent and Collapse all", async () => {
    const node = await mount(SAMPLE());
    const tool = (name: string) => all(node, ".attn-tools button").find((b) => b.getAttribute("aria-label") === name)!;
    tool("Collapse all").click();
    await flush();
    expect(all(node, ".nt-grp-btn").every((b) => b.getAttribute("aria-expanded") === "false")).toBe(true);
    tool("Expand urgent").click();
    await flush();
    expect(q(node, '[data-line="nt.area.ship"] .nt-grp-btn')!.getAttribute("aria-expanded")).toBe("true");
    expect(q(node, '[data-line="nt.area.cloud"] .nt-grp-btn')!.getAttribute("aria-expanded")).toBe("false");
  });

  it("ends an area row with a quiet link to its tab, only when the page has tabs", async () => {
    let node = await mount(SAMPLE());
    q(node, '[data-line="nt.area.box"] .nt-go')!.click();
    expect(openTab).toHaveBeenCalledWith("box");
    deactivateAttention();
    node.remove();
    resetAttention();
    node = await mount(SAMPLE(), false);
    expect(q(node, ".nt-go")).toBeNull();
  });

  it("offers Review N on a source with two or more reviewable items", async () => {
    const node = await mount(SAMPLE());
    expect(q(node, '[data-line="attn.git"] .attn-review-source')!.textContent).toContain("Review 3");
    expect(q(node, '[data-line="attn.vault"] .attn-review-source')!.textContent).toContain("Review 2");
    expect(q(node, '[data-line="attn.box"] .attn-review-source')).toBeNull();
  });
});

describe("the columns and the rows", () => {
  it("has the columns What, Age, Checked and Action (a pick column and the toggle come first), and no Service column", async () => {
    const node = await mount(SAMPLE());
    expect(all(node, ".nt-head > span").map((s) => s.textContent)).toEqual(["", "", "What", "Age", "Checked", "Action"]);
    expect(node.textContent).not.toContain("Service");
  });

  it("shows the kind as a muted prefix only when the source has more than one kind", async () => {
    const node = await mount(SAMPLE());
    const git = q(node, '[data-line="attn.git"]')!;
    expect(all(git, ".nt-kind").map((k) => k.textContent)).toEqual(["Failing PRs · ", "Failing PRs · ", "Ready to merge · "]);
    // Git has two kinds (failing and ready), so each row names its own; Vault has one kind: none.
    expect(q(node, '[data-line="attn.vault"] .nt-kind')).toBeNull();
  });

  it("gives each row its description under the title, when it was read, and its age from the server's first-seen time", async () => {
    const node = await mount(overview([item("only", { kind: "pr-failing", meta: "web · checks failing" })], { read_at: "2026-11-14T11:58:00Z" }));
    const row = q(node, ".nt-row")!;
    expect(q(row, ".attn-meta")!.textContent).toBe("web · checks failing");
    expect(q(row, ".nt-checked")!.textContent).toContain("2 min ago");
    // No first-seen time (an older server, or an unreadable log): the Age cell is empty, never a guess.
    expect(q(row, ".nt-age")!.textContent).toBe("Age: ");
    // Nothing is written to the browser's storage for an age (phase 1 kept one there; phase 2 does not).
    expect(window.localStorage.getItem("attn.first")).toBeNull();
  });

  it("shows only the main button in a closed row, and the other actions wait in the opened panel", async () => {
    const node = await mount(SAMPLE());
    const ready = q(node, '[data-id="g-ready"]')!;
    const primary = all(ready, ".attn-actions [data-primary]");
    expect(primary).toHaveLength(1);
    expect(primary[0]!.dataset.focus).toBe("g-ready:merge");
    expect(q(ready, ".nt-panel")).toBeNull();
  });
});

describe("the opened row", () => {
  const open = async (node: HTMLElement, id: string) => {
    q(node, `[data-id="${id}"] .nt-toggle`)!.click();
    await flush();
    return q(node, `[data-id="${id}"]`)!;
  };

  it("opens in place with a Close control instead of its action button, and one panel under it", async () => {
    const node = await mount(SAMPLE());
    const row = await open(node, "g-ready");
    expect(row.classList.contains("is-open")).toBe(true);
    expect(q(row, ".nt-toggle")!.getAttribute("aria-expanded")).toBe("true");
    const close = q(row, ":scope > .nt-cell > .nt-close")!;
    expect(close.getAttribute("aria-label")).toContain("Close");
    expect(close.getAttribute("aria-controls")).toBe(q(row, ".nt-panel")!.id);
    // The row's own actions moved into the panel; only Close is left in its cell.
    expect(q(row, ":scope > .nt-cell > .attn-actions")).toBeNull();
    expect(all(row, ":scope > .nt-panel")).toHaveLength(1);
  });

  it("holds the evidence line, the facts in one row, one row of actions and one hint, in that order", async () => {
    const node = await mount(SAMPLE());
    const row = await open(node, "g-ready");
    const main = q(row, ".nt-panel-main")!;
    // Layout B (2026-11-16): a toolbar across the top, then two columns. The left holds the evidence, the
    // facts and the hint; the history is the right column.
    expect([...main.children].map((c) => c.className.split(" ")[0])).toEqual(["nt-toolbar", "nt-panel-msg", "nt-body"]);
    const left = q(main, ".nt-body > .nt-left")!;
    expect([...left.children].map((c) => c.className.split(" ")[0])).toEqual(["nt-evidence", "nt-facts", "nt-hint"]);
    expect(q(main, ".nt-toolbar > .nt-acts")).not.toBeNull();
    expect(q(main, ".nt-evidence")!.textContent).toContain("Git repos reports:");
    expect(q(main, ".nt-evidence")!.textContent).toContain("Read at");
    const facts = all(main, ".nt-fact");
    expect(facts.map((f) => q(f, ".nt-fact-k")!.textContent)).toEqual(["Source", "Group", "Comes back if", "Dismiss lasts"]);
    expect(q(main, ".nt-hint")!.textContent).toContain("Dismiss hides this until a new commit still fails");
    expect(q(main, ".nt-hint")!.textContent).toMatch(/or until [A-Z][a-z]+ \d+\./);
  });

  it("puts the primary action first, the outside link and the card next, and Dismiss last", async () => {
    const node = await mount(SAMPLE());
    const row = await open(node, "g-ready");
    const labels = all(row, ".nt-acts .attn-actions > button").map((b) => b.dataset.focus!.split(":").pop());
    expect(labels[0]).toBe("merge");
    expect(labels[labels.length - 1]).toBe("dismiss");
    expect(labels).toContain("github");
    expect(q(row, ".nt-dismiss")).not.toBeNull();
  });

  it("opens one row at a time, and Close or the toggle closes it", async () => {
    const node = await mount(SAMPLE());
    await open(node, "g-ready");
    const second = await open(node, "box-1");
    expect(all(node, ".nt-row.is-open").map((r) => r.dataset.id)).toEqual(["box-1"]);
    q(second, ".nt-close")!.click();
    await flush();
    expect(all(node, ".nt-row.is-open")).toHaveLength(0);
    expect(q(node, '[data-id="box-1"] .attn-actions [data-primary]')).not.toBeNull();
  });

  it("opens from a click on the row as well, but not from a click on its title link or a button", async () => {
    const node = await mount(SAMPLE());
    q(node, '[data-id="box-1"] .attn-text')!.click();
    await flush();
    expect(q(node, '[data-id="box-1"]')!.classList.contains("is-open")).toBe(true);
    q(node, '[data-id="box-1"] .nt-close')!.click();
    await flush();
    q(node, '[data-id="box-1"] [data-primary]')!.click();
    await flush();
    expect(q(node, '[data-id="box-1"]')!.classList.contains("is-open")).toBe(false);
  });

  it("builds the panel as a main column with the history box beside it", async () => {
    const node = await mount(SAMPLE());
    const row = await open(node, "box-1");
    expect(all(row, ".nt-panel > *").map((c) => c.className)).toEqual(["nt-panel-main"]);
    expect(all(row, ".nt-panel-main > .nt-body > *").map((c) => c.className)).toEqual(["nt-left", "nt-panel-side nt-hist"]);
  });

  it("keeps an open row open across a redraw from the same list", async () => {
    const node = await mount(SAMPLE());
    await open(node, "g-ready");
    window.dispatchEvent(new Event("focus"));
    await flush();
    expect(q(node, '[data-id="g-ready"]')!.classList.contains("is-open")).toBe(true);
  });
});

describe("the composition bar and the banner", () => {
  it("draws one segment per source, its width by its item count, each with a name and a count", async () => {
    const node = await mount(SAMPLE());
    const segs = all(node, ".nt-seg");
    expect(segs.map((s) => q(s, ".nt-seg-lab")!.textContent)).toEqual(["Git repos 3", "Box health 1", "Key Vault 2", "Accounts 1"]);
    expect(segs.map((s) => s.style.flexGrow)).toEqual(["3", "1", "2", "1"]);
    expect(all(segs[0]!, ".nt-bar i").map((i) => i.className)).toEqual(["nt-bar-urgent", "nt-bar-review"]);
    expect(all(segs[0]!, ".nt-bar i").map((i) => i.style.flexGrow)).toEqual(["2", "1"]);
  });

  it("filters to a source with a click, and shows every source again with a second", async () => {
    const node = await mount(SAMPLE());
    q(node, ".nt-seg:nth-child(3)")!.click();
    await flush();
    expect(getFilter()).toBe("vault");
    expect(all(node, ".nt-row").map((r) => r.dataset.id)).toEqual(["v-1", "v-2"]);
    expect(q(node, ".attn-chip")!.textContent).toContain("Key Vault");
    q(node, ".nt-seg.is-on")!.click();
    await flush();
    expect(getFilter()).toBeNull();
  });

  it("draws a dashed grey part for a source that could not be read, and a banner at the top of the table", async () => {
    const node = await mount({ ...SAMPLE(), unknown: ["Key Vault could not be read"] });
    const vault = all(node, ".nt-seg").find((s) => q(s, ".nt-seg-lab")!.textContent!.startsWith("Key Vault"))!;
    expect(q(vault, ".nt-bar-unread")).not.toBeNull();
    expect(q(node, ".nt-banner")!.textContent).toContain("Key Vault could not be read");
    expect(q(node, '[data-line="attn.vault"] .nt-unread')!.textContent).toBe("?");
    // The rows keep their last answer.
    expect(all(node, '[data-line="attn.vault"] .nt-row')).toHaveLength(2);
  });

  it("gives an unreadable source with no rows a segment of its own", async () => {
    const node = await mount(overview([item("g", { kind: "pr-failing" })], { unknown: ["Box health could not be read"] }));
    expect(all(node, ".nt-seg").map((s) => q(s, ".nt-seg-lab b")!.textContent)).toEqual(["Git repos", "Box health"]);
  });
});

describe("an area tab", () => {
  it("shows only that area's sources, without the area row", async () => {
    const node = await mount(SAMPLE());
    table.setArea("cloud");
    await flush();
    expect(all(node, ".nt-source").map((s) => s.dataset.source)).toEqual(["vault"]);
    expect(q(node, ".nt-area")).toBeNull();
    expect(q(node, ".nt-table")!.classList.contains("is-flat")).toBe(true);
    table.setArea("money");
    await flush();
    expect(all(node, ".nt-source").map((s) => s.dataset.source)).toEqual(["accounts"]);
    table.setArea(null);
    await flush();
    expect(all(node, ".nt-area")).toHaveLength(4);
  });

  it("says so when the area has nothing", async () => {
    const node = await mount(overview([item("g", { kind: "pr-failing" })]));
    table.setArea("cloud");
    await flush();
    expect(q(node, ".attn-empty")!.textContent).toBe("Nothing needs you.");
  });
});

describe("hiding a card", () => {
  it("leaves its items in the table (2026-11-14: Hide for me hides only the card)", async () => {
    const { setCards, setHidden } = await import("../src/cardState");
    setCards({
      revision: 1,
      state_ok: true,
      cards: [{ id: "vault", name: "Key Vault", group: "cloud", description: "", sources: ["vault"], on: true, default_on: true, changed_by: null, changed_at: null, fields: [] }],
    });
    setHidden(["vault"]);
    const node = await mount(SAMPLE());
    expect(q(node, '[data-line="attn.vault"]')).not.toBeNull();
    expect(q(node, ".attn-counts")!.textContent).toContain("urgent");
  });
});

// ── the actions inside the table (attention.ts builds the buttons; the table only frames them) ──

type Reply = { ok: boolean; status: number; body: unknown };
const okReply = (body: unknown): Reply => ({ ok: true, status: 200, body });

function routed(routes: Record<string, Reply | (() => Reply)>) {
  const calls: Array<{ method: string; path: string; body: unknown }> = [];
  const request = vi.fn(async (call: { method: string; path: string; body: unknown }) => {
    calls.push(call);
    const route = routes[`${call.method} ${call.path}`];
    if (!route) return { ok: false, status: 500, body: { error: { code: "unexpected", message: `unexpected ${call.method} ${call.path}` } } };
    return typeof route === "function" ? route() : route;
  });
  const context = {
    view: "fabric",
    server: { request },
    navigation: { openSession: vi.fn(), openExternal: vi.fn(), openPage: vi.fn() },
  } as unknown as ExtensionContext;
  return { context, calls };
}

async function mountOn(context: ExtensionContext): Promise<HTMLElement> {
  table = createNeedsTable({ openTab, openCard, hasTab: () => true });
  const node = attentionSection();
  document.getElementById("root")?.appendChild(node);
  activateAttention(context, node, { waiting: false, table: table.hooks });
  await flush();
  return node;
}

const click = async (node: Element | null | undefined) => {
  (node as HTMLElement).click();
  await flush();
};
const byText = (node: ParentNode, label: string) => all(node, "button").find((b) => b.textContent === label)!;

describe("Dismiss and Restore inside the table", () => {
  const id = "git:contoso/web#12";

  it("dismisses a row through the same dialog, offers Undo, and Restore brings it back", async () => {
    let listed = overview([item(id, { title: "Fix #12 Bump thing" })]);
    const dismissedItem = () =>
      item(id, { title: "Fix #12 Bump thing", dismissed: { at: "2026-11-14T11:00:00Z", reason: "Waiting on CI", expires_at: "2026-11-17T11:00:00Z" } });
    const { context, calls } = routed({
      "GET /v1/ae/attention": () => okReply(listed),
      "POST /v1/ae/attention/dismiss": () => {
        listed = overview([], { dismissed: [dismissedItem()] });
        return okReply({ id, expires_at: "2026-11-17T11:00:00Z", reason: "Waiting on CI" });
      },
      "POST /v1/ae/attention/restore": () => {
        listed = overview([item(id, { title: "Fix #12 Bump thing" })]);
        return okReply({ id, restored: true });
      },
    });
    const node = await mountOn(context);
    // Dismiss is in the opened row's panel, not the closed row.
    expect(q(node, '[data-focus$=":dismiss"]')).not.toBeNull();
    await click(q(node, '[data-id="' + id + '"] .nt-toggle'));
    await click(q(node, '.nt-panel [data-focus$=":dismiss"]'));
    const dialog = q(document.body, '[role="dialog"]')!;
    expect(q(dialog, ".attn-dlg-item")!.textContent).toBe("Fix #12 Bump thing");
    q(dialog as HTMLElement, "input.attn-reason")!.setAttribute("value", "");
    (q(dialog, "input.attn-reason") as HTMLInputElement).value = "Waiting on CI";
    await click(byText(dialog, "Dismiss"));
    expect(calls.find((c) => c.path.endsWith("/attention/dismiss"))!.body).toEqual({ id, reason: "Waiting on CI" });
    expect(q(node, ".nt-row")).toBeNull();
    expect(q(node, ".attn-undo-text")!.textContent).toMatch(/^Dismissed: /);
    // The Dismissed fold lists it; Restore calls the route and the row returns.
    await click(q(node, ".nt-dlink"));
    await click(q(node, ".attn-restore"));
    expect(calls.some((c) => c.path.endsWith("/attention/restore"))).toBe(true);
    expect(q(node, '[data-id="' + id + '"]')).not.toBeNull();
  });
});

describe("Merge inside the table", () => {
  const SHA = "a1b2c3d4e5f60718293a4b5c6d7e8f9012345678";
  const URL = "https://github.com/contoso/web/pull/731";
  const ready = () =>
    item("pr-ready-731", { kind: "pr-ready", tone: "review", title: "Merge #731 Bump thing", url: URL, actions: ["review", "merge", "dismiss"] });
  const prCard = {
    repo: "contoso/web", number: 731, url: URL, title: "Bump thing", state: "open", draft: false, author: "someone",
    head_ref: "bump", base_ref: "main", head_sha: SHA, updated_at: "2026-11-14T10:00:00Z",
    ci: { state: "passing", passing: 6, failing: 0, pending: 0, total: 6 },
    mergeable: { state: "clean", detail: "No conflicts with the base branch." },
    review: { state: "approved", approvals: 1, changes_requested: 0, requested: [] },
    merge: { methods: ["squash", "merge"], default_method: "squash", allowed: true, reason: null },
  };

  it("reads the card on a click, opens the confirm under the row, and merges only on Confirm", async () => {
    const { context, calls } = routed({
      "GET /v1/ae/attention": okReply(overview([ready()])),
      "GET /v1/ae/prs/contoso/web/731": okReply(prCard),
      "POST /v1/ae/prs/contoso/web/731/merge": okReply({ merged: true, repo: "contoso/web", number: 731, method: "squash", sha: SHA, message: "ok", session: null }),
    });
    const node = await mountOn(context);
    await click(q(node, '.nt-row [data-focus$=":merge"]'));
    // The row is drawn again around the confirm; ask for it afresh.
    expect(q(node, ".nt-row > .pr-confirm")).not.toBeNull();
    expect(calls.filter((c) => c.path.endsWith("/merge"))).toHaveLength(0);
    // The facts a Merge click read show in the opened row's panel.
    await click(q(node, ".nt-row .nt-toggle"));
    expect(q(node, ".nt-evidence")!.textContent).toContain("6 of 6 checks passing");
    expect(all(node, ".nt-fact-k").map((n) => n.textContent)).toEqual(expect.arrayContaining(["Checks", "Approvals", "Mergeable"]));
    await click(byText(q(node, ".pr-confirm")!, "Squash into main"));
    expect(calls.filter((c) => c.path.endsWith("/merge"))[0]!.body).toEqual({ sha: SHA, method: "squash" });
    expect(q(node, ".nt-row")!.textContent).toContain("Merged #731 (squashed) a1b2c3d");
  });
});

describe("an opened row whose item leaves the list", () => {
  it("closes, and the next row opens only when asked", async () => {
    let listed = overview([item("a", { kind: "pr-failing" }), item("b", { kind: "pr-failing" })]);
    const { context } = routed({ "GET /v1/ae/attention": () => okReply(listed) });
    const node = await mountOn(context);
    await click(q(node, '[data-id="a"] .nt-toggle'));
    expect(q(node, '[data-id="a"]')!.classList.contains("is-open")).toBe(true);
    listed = overview([item("b", { kind: "pr-failing" })]);
    window.dispatchEvent(new Event("focus"));
    await flush();
    expect(q(node, '[data-id="a"]')).toBeNull();
    expect(all(node, ".nt-row.is-open")).toHaveLength(0);
    // The id coming back does not reopen a row nobody asked for.
    listed = overview([item("a", { kind: "pr-failing" }), item("b", { kind: "pr-failing" })]);
    window.dispatchEvent(new Event("focus"));
    await flush();
    expect(all(node, ".nt-row.is-open")).toHaveLength(0);
  });

  it("names the toggle by the item's own title and sets aria-controls only while the panel exists", async () => {
    const { context } = routed({ "GET /v1/ae/attention": okReply(overview([item("a", { kind: "pr-failing", title: "Fix #9 Bump thing" })])) });
    const node = await mountOn(context);
    const toggle = q(node, '[data-id="a"] .nt-toggle')!;
    expect(toggle.hasAttribute("aria-label")).toBe(false);
    expect(toggle.textContent).toContain("Fix #9 Bump thing");
    expect(toggle.hasAttribute("aria-controls")).toBe(false);
    await click(toggle);
    const open = q(node, '[data-id="a"] .nt-toggle')!;
    expect(document.getElementById(open.getAttribute("aria-controls")!)).not.toBeNull();
  });
});

describe("a source that could not be read", () => {
  const unreadGit = (items = [item("v", { source: "vault", kind: "vault-soon", url: "", tone: "review", actions: ["review"] })]) =>
    overview(items, { unknown: ["Git repos could not be read"] });

  it("says what could not be read on an area tab with no rows, never Nothing needs you", async () => {
    const node = await mount(unreadGit());
    table.setArea("ship");
    await flush();
    expect(q(node, ".attn-empty")!.textContent).toBe("Could not read Git repos.");
    expect(q(node, ".nt-seg-lab")?.textContent).toContain("not read");
  });

  it("says it on the Overview too when nothing else needs you, and with a source filter", async () => {
    const node = await mount(unreadGit([]));
    expect(q(node, ".attn-empty")!.textContent).toBe("Nothing else needs you, but Git repos could not be read, so it may be missing items.");
    expect(q(node, ".nt-seg .nt-bar-unread")).not.toBeNull();
    expect(q(node, ".nt-seg-lab")!.textContent).toBe("Git repos not read");
    q(node, ".nt-seg")!.click();
    await flush();
    expect(q(node, ".attn-empty")!.textContent).toBe("Could not read Git repos.");
  });

  it("names the source on its marker and says both halves of the banner wording", async () => {
    const node = await mount(overview([item("g")], { unknown: ["Git repos could not be read"] }));
    expect(q(node, ".nt-unread")!.getAttribute("title")).toBe(
      "Git repos could not be read on the last refresh, so its rows may be out of date. It tries again on the next refresh.",
    );
  });

  it("shows on an area tab only the banners for sources in that area", async () => {
    const node = await mount(overview([item("b", { source: "box", kind: "heat", url: "" })], { unknown: ["Git repos could not be read", "Box health could not be read"] }));
    expect(all(node, ".nt-banner")).toHaveLength(2);
    table.setArea("box");
    await flush();
    expect(all(node, ".nt-banner").map((b) => b.textContent)).toEqual(["Box health could not be read on the last refresh, so its rows may be out of date. It tries again on the next refresh."]);
    table.setArea("cloud");
    await flush();
    expect(all(node, ".nt-banner")).toHaveLength(0);
  });
});

describe("counts come from by_source, never from the rows the server cut", () => {
  it("counts an area tab and its rows' pills from by_source, and says how many more were not shown", async () => {
    const rows = many(3, "pr-failing");
    const answer = overview(rows, {
      counts: { urgent: 9, review: 1, by_source: { git: { urgent: 9, review: 1 } } },
      more: 7,
    });
    const node = await mount(answer);
    expect(q(node, ".attn-counts")!.textContent).toBe("9 urgent, 1 to review");
    expect(q(node, ".attn-more.meta")!.textContent).toBe("7 more not shown");
    table.setArea("ship");
    await flush();
    // The area tab says the same numbers, and the 7 rows that were cut.
    expect(q(node, ".attn-counts")!.textContent).toBe("9 urgent, 1 to review");
    expect(q(node, ".attn-more.meta")!.textContent).toBe("7 more not shown");
    expect(q(node, ".nt-source .nt-pill-urgent")!.textContent).toBe("9 urgent");
  });
});

describe("an area link only for an area that has its own tab", () => {
  it("draws the link for a ship tab that exists and not for a box tab that does not", async () => {
    table = createNeedsTable({ openTab, openCard, hasTab: (area) => area === "ship" });
    const node = attentionSection();
    document.getElementById("root")?.appendChild(node);
    activateAttention(stub(SAMPLE()).context, node, { waiting: false, table: table.hooks });
    await flush();
    expect(all(node, ".nt-go").map((link) => link.textContent)).toEqual(["Ship tab"]);
    // A secondary button with a Tabler arrow drawn in the DOM after its label, and no "›".
    const link = q(node, ".nt-go")!;
    expect(link.classList.contains("btn-secondary")).toBe(true);
    expect(link.textContent).not.toContain("›");
    expect(link.lastElementChild!.tagName.toLowerCase()).toBe("svg");
    expect(link.lastElementChild!.getAttribute("aria-hidden")).toBe("true");
  });
});

describe("the open plan", () => {
  it("opens every area and no source, in the fixed order, when the person has toggled nothing", async () => {
    Object.defineProperty(window, "innerHeight", { value: 2000, configurable: true });
    const node = await mount(SAMPLE());
    expect(all(node, ".nt-area > .nt-grp-head .nt-grp-btn").map((b) => b.getAttribute("aria-expanded"))).toEqual(["true", "true", "true", "true"]);
    expect(all(node, ".nt-source > .nt-grp-head .nt-grp-btn").every((b) => b.getAttribute("aria-expanded") === "false")).toBe(true);
    // The areas keep the page's order.
    expect(all(node, ".nt-area").map((a) => a.dataset.area)).toEqual(["ship", "box", "cloud", "money"]);
    Object.defineProperty(window, "innerHeight", { value: 768, configurable: true });
  });

  it("ignores the screen height, and a person's own choice still wins and survives a read", async () => {
    const original = window.innerHeight;
    Object.defineProperty(window, "innerHeight", { value: 560, configurable: true });
    try {
      const node = await mount(SAMPLE());
      const open = (line: string) => q(node, `[data-line="${line}"] .nt-grp-btn`)!.getAttribute("aria-expanded");
      expect(["ship", "box", "cloud", "money"].map((a) => open(`nt.area.${a}`))).toEqual(["true", "true", "true", "true"]);
      // The person opens the Key Vault source and closes Accounts; a later read keeps both.
      await click(q(node, '[data-line="attn.vault"] .nt-grp-btn'));
      await click(q(node, '[data-line="nt.area.money"] .nt-grp-btn'));
      window.dispatchEvent(new Event("focus"));
      await flush();
      expect(open("attn.vault")).toBe("true");
      expect(open("nt.area.money")).toBe("false");
    } finally {
      Object.defineProperty(window, "innerHeight", { value: original, configurable: true });
    }
  });
});

describe("the fixed order and the open plan (2026-11-16, collapsed 2026-11-17)", () => {
  const original = window.innerHeight;
  const setHeight = (value: number) => Object.defineProperty(window, "innerHeight", { value, configurable: true });
  const poll = async () => {
    window.dispatchEvent(new Event("focus"));
    await flush();
  };
  const isOpen = (node: HTMLElement, line: string) => q(node, `[data-line="${line}"] .nt-grp-btn`)?.getAttribute("aria-expanded");
  const working = { session_id: "s", title: "Review", path: "/x", status: "working" } as const;
  afterEach(() => setHeight(original));

  it("does not move a group when its urgent items go to a chat, or when one is dismissed", async () => {
    setHeight(400);
    let answer = overview([...SAMPLE().items, ...many(3, "pr-ready", { tone: "urgent" })]);
    const node = await mount(() => answer);
    expect(isOpen(node, "nt.area.ship")).toBe("true");
    expect(isOpen(node, "attn.git")).toBe("false");
    answer = overview(answer.items.map((row) => (row.source === "git" ? { ...row, working } : row)));
    await poll();
    expect(isOpen(node, "nt.area.ship")).toBe("true");
    expect(isOpen(node, "attn.git")).toBe("false");
    answer = overview(answer.items.filter((row) => row.source !== "box"));
    await poll();
    expect(all(node, ".nt-area").map((a) => a.dataset.area)).toEqual(["ship", "cloud", "money"]);
    expect(isOpen(node, "nt.area.ship")).toBe("true");
  });

  it("opens a new area and keeps every source closed when a read adds one", async () => {
    setHeight(400);
    let answer = overview([...many(3, "pr-failing"), item("v1", { source: "vault", kind: "vault-soon", tone: "review", url: "" })]);
    const node = await mount(() => answer);
    expect(isOpen(node, "nt.area.cloud")).toBe("true");
    answer = overview([
      ...answer.items,
      item("a1", { source: "accounts", kind: "account-full", tone: "review", url: "" }),
    ]);
    await poll();
    expect(all(node, ".nt-area").map((a) => a.dataset.area)).toEqual(["ship", "cloud", "money"]);
    expect(isOpen(node, "nt.area.money")).toBe("true");
    expect(all(node, ".nt-source > .nt-grp-head .nt-grp-btn").every((b) => b.getAttribute("aria-expanded") === "false")).toBe(true);
  });

  it("keeps a source the person opened, open, across a re-plan", async () => {
    setHeight(400);
    let answer = overview([...many(4, "pr-failing"), item("v1", { source: "vault", kind: "vault-soon", tone: "review", url: "" })]);
    const node = await mount(() => answer);
    await click(q(node, '[data-line="attn.vault"] .nt-grp-btn'));
    expect(isOpen(node, "attn.vault")).toBe("true");
    answer = overview([...answer.items, item("a1", { source: "accounts", kind: "account-full", tone: "review", url: "" })]);
    await poll();
    expect(isOpen(node, "attn.vault")).toBe("true");
  });

  it("does not fold an area when the list grows past the screen", async () => {
    setHeight(2000);
    let answer = overview([
      item("g1", { kind: "pr-ready", tone: "review" }),
      item("v1", { source: "vault", kind: "vault-soon", tone: "review", url: "" }),
      item("a1", { source: "accounts", kind: "account-full", tone: "review", url: "" }),
    ]);
    const node = await mount(() => answer);
    setHeight(400);
    answer = overview([
      ...answer.items,
      ...Array.from({ length: 6 }, (_, n) => item(`v-more-${n}`, { source: "vault", kind: "vault-soon", tone: "review", url: "" })),
    ]);
    await poll();
    expect(all(node, ".nt-area > .nt-grp-head .nt-grp-btn").every((b) => b.getAttribute("aria-expanded") === "true")).toBe(true);
  });

  it("keeps the same open plan when the column count changes", async () => {
    const watchers: Array<() => void> = [];
    vi.stubGlobal(
      "ResizeObserver",
      class {
        constructor(cb: () => void) {
          watchers.push(cb);
        }
        observe() {}
        disconnect() {}
      },
    );
    try {
      setHeight(1000);
      const rows = overview([
        item("g1", { kind: "pr-ready", tone: "review" }),
        ...many(2, "heat", { source: "box", tone: "review", url: "" }),
        ...many(3, "vault-soon", { source: "vault", tone: "review", url: "" }),
        ...many(2, "account-full", { source: "accounts", tone: "review", url: "" }),
      ]);
      width = 2530;
      const node = await mount(() => rows);
      const areas = () => all(node, ".nt-area > .nt-grp-head .nt-grp-btn").map((b) => b.getAttribute("aria-expanded"));
      expect(all(node, ".nt-col")).toHaveLength(2);
      expect(areas().every((v) => v === "true")).toBe(true);
      width = 1000;
      for (const watch of watchers) watch();
      await flush();
      expect(all(node, ".nt-col")).toHaveLength(0);
      expect(areas().every((v) => v === "true")).toBe(true);
      expect(all(node, ".nt-source > .nt-grp-head .nt-grp-btn").every((b) => b.getAttribute("aria-expanded") === "false")).toBe(true);
    } finally {
      vi.unstubAllGlobals();
    }
  });

  it("keeps today's budgeted plan on an area tab: Cloud alone fits the screen, so its source opens", async () => {
    setHeight(400);
    const node = await mount(SAMPLE());
    expect(isOpen(node, "attn.vault")).toBe("false");
    table.setArea("cloud");
    await flush();
    expect(isOpen(node, "attn.vault")).toBe("true");
    table.setArea(null);
    await flush();
    expect(isOpen(node, "attn.vault")).toBe("false");
    expect(isOpen(node, "nt.area.cloud")).toBe("true");
  });
});


// ── the phone's opened item: a sheet on body, below the shared dialogs ──────────

describe("the phone's opened item", () => {
  function setPhone(matches: boolean): void {
    window.matchMedia = vi.fn(() => ({ matches, addEventListener: vi.fn(), removeEventListener: vi.fn() })) as unknown as typeof window.matchMedia;
  }
  afterEach(() => {
    delete (window as { matchMedia?: unknown }).matchMedia;
    for (const node of all(document, ".nt-sheet")) node.remove();
  });
  const sheetOf = () => q(document.body, ".nt-sheet")!;

  it("is a dialog on document.body, named by the item's title, with Close focused and the actions inside", async () => {
    setPhone(true);
    const node = await mount(SAMPLE());
    const opener = q(node, '[data-id="box-1"] .nt-toggle')!;
    opener.focus();
    await click(opener);
    const sheet = sheetOf();
    // Outside the page's container (a container is the containing block of fixed descendants), and
    // not a modal <dialog>, which would make the rest of the page inert.
    expect(sheet.parentElement).toBe(document.body);
    expect(node.contains(sheet)).toBe(false);
    expect(sheet.tagName).toBe("DIV");
    expect(sheet.getAttribute("role")).toBe("dialog");
    expect(sheet.getAttribute("aria-modal")).toBe("true");
    expect(document.getElementById(sheet.getAttribute("aria-labelledby")!)!.textContent).toBe("CPU at 98C");
    expect(document.activeElement).toBe(q(sheet, ".nt-close"));
    expect(q(sheet, ".nt-acts [data-primary]")).not.toBeNull();
    // Its Close is no disclosure of the row, and the row behind it holds none.
    expect(q(sheet, ".nt-close")!.hasAttribute("aria-expanded")).toBe(false);
    expect(q(sheet, ".nt-close")!.hasAttribute("aria-controls")).toBe(false);
    expect(q(sheet, ".nt-close")!.getAttribute("aria-label")).toBe("Close CPU at 98C");
    expect(q(node, '[data-id="box-1"] > .nt-cell .nt-close')).toBeNull();
  });

  it("keeps Tab inside, closes on Escape and gives the focus back to the row", async () => {
    setPhone(true);
    saveFold("attn.box", true);
    const node = await mount(SAMPLE());
    const opener = q(node, '[data-id="box-1"] .nt-toggle')!;
    opener.focus();
    await click(opener);
    const sheet = sheetOf();
    const buttons = all(sheet, "button");
    const last = buttons[buttons.length - 1]!;
    last.focus();
    const tab = new KeyboardEvent("keydown", { key: "Tab", bubbles: true, cancelable: true });
    last.dispatchEvent(tab);
    expect(tab.defaultPrevented).toBe(true);
    expect(document.activeElement).toBe(buttons[0]);
    const shift = new KeyboardEvent("keydown", { key: "Tab", shiftKey: true, bubbles: true, cancelable: true });
    buttons[0]!.dispatchEvent(shift);
    expect(document.activeElement).toBe(last);
    sheet.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true, cancelable: true }));
    await flush();
    expect(q(document.body, ".nt-sheet")).toBeNull();
    expect(document.activeElement).toBe(q(node, '[data-id="box-1"] .nt-toggle'));
  });

  it("does not trap a Tab pressed outside the sheet (the handler is on the sheet, not document)", async () => {
    setPhone(true);
    const node = await mount(SAMPLE());
    await click(q(node, '[data-id="box-1"] .nt-toggle'));
    const outside = document.createElement("button");
    document.body.append(outside);
    outside.focus();
    const tab = new KeyboardEvent("keydown", { key: "Tab", bubbles: true, cancelable: true });
    outside.dispatchEvent(tab);
    expect(tab.defaultPrevented).toBe(false);
    expect(document.activeElement).toBe(outside);
    outside.remove();
  });

  it("opens a Dismiss confirmation from the sheet that is reachable: nothing is inert and focus lands in it", async () => {
    setPhone(true);
    const node = await mount(SAMPLE());
    await click(q(node, '[data-id="box-1"] .nt-toggle'));
    await click(q(sheetOf(), '[data-focus$=":dismiss"]'));
    const confirmation = q(document.body, '.ae-dialog [role="dialog"], .ae-dialog')!;
    expect(confirmation).not.toBeNull();
    // The confirmation is a body-level dialog stacked above the sheet, and no ancestor is inert.
    expect(confirmation.closest(".ae-dialog")!.parentElement).toBe(document.body);
    for (let n: Element | null = confirmation; n; n = n.parentElement) expect(n.hasAttribute("inert")).toBe(false);
    expect(sheetOf().hasAttribute("inert")).toBe(false);
    expect(confirmation.contains(document.activeElement)).toBe(true);
    // The sheet is still open behind it.
    expect(sheetOf()).not.toBeNull();
  });

  it("keeps the same sheet node, its scroll and its focus across a redraw of the same open row", async () => {
    setPhone(true);
    const node = await mount(SAMPLE());
    await click(q(node, '[data-id="box-1"] .nt-toggle'));
    const before = sheetOf();
    const scroller = q(before, ".nt-panel-main")!;
    scroller.scrollTop = 40;
    const dismiss = q(before, '[data-focus$=":dismiss"]') as HTMLElement | null;
    const target = dismiss ?? q(before, ".nt-acts button")!;
    target.focus();
    const mark = target.dataset.focus;
    window.dispatchEvent(new Event("focus"));
    await flush();
    expect(sheetOf()).toBe(before);
    expect(all(document.body, ".nt-sheet")).toHaveLength(1);
    expect(q(sheetOf(), ".nt-panel-main")).toBe(scroller);
    expect(scroller.scrollTop).toBe(40);
    expect((document.activeElement as HTMLElement).dataset.focus).toBe(mark);
    // A different row opening makes a new sheet and moves the focus to its Close.
    await click(q(node, '[data-id="v-1"] .nt-toggle'));
    expect(sheetOf()).not.toBe(before);
    expect(all(document.body, ".nt-sheet")).toHaveLength(1);
    expect(document.activeElement).toBe(q(sheetOf(), ".nt-close"));
  });

  it("closes from its Close button, and is an ordinary in-row panel when the screen is not a phone", async () => {
    setPhone(true);
    let node = await mount(SAMPLE());
    await click(q(node, '[data-id="box-1"] .nt-toggle'));
    await click(q(sheetOf(), ".nt-close"));
    expect(q(document.body, ".nt-sheet")).toBeNull();
    deactivateAttention();
    node.remove();
    resetAttention();
    setPhone(false);
    node = await mount(SAMPLE());
    await click(q(node, '[data-id="box-1"] .nt-toggle'));
    expect(q(document.body, ".nt-sheet")).toBeNull();
    expect(q(node, '[data-id="box-1"] .nt-panel')!.getAttribute("role")).toBe("group");
  });

  it("closes the sheet when its item leaves the list, and when the table is disposed", async () => {
    setPhone(true);
    const node = await mount(SAMPLE());
    await click(q(node, '[data-id="box-1"] .nt-toggle'));
    expect(sheetOf()).not.toBeNull();
    table.dispose();
    expect(q(document.body, ".nt-sheet")).toBeNull();
  });

  it("puts the Merge confirm in the sheet, above the actions", async () => {
    setPhone(true);
    const { context } = routed({
      "GET /v1/ae/attention": okReply(overview([item("pr-ready-731", { kind: "pr-ready", tone: "review", title: "Merge #731 Bump thing", url: "https://github.com/contoso/web/pull/731", actions: ["review", "merge", "dismiss"] })])),
      "GET /v1/ae/prs/contoso/web/731": okReply({
        repo: "contoso/web", number: 731, url: "https://github.com/contoso/web/pull/731", title: "Bump thing", state: "open", draft: false, author: "someone",
        head_ref: "bump", base_ref: "main", head_sha: "a1b2c3d4e5f60718293a4b5c6d7e8f9012345678", updated_at: "2026-11-14T10:00:00Z",
        ci: { state: "passing", passing: 6, failing: 0, pending: 0, total: 6 },
        mergeable: { state: "clean", detail: "No conflicts with the base branch." },
        review: { state: "approved", approvals: 1, changes_requested: 0, requested: [] },
        merge: { methods: ["squash", "merge"], default_method: "squash", allowed: true, reason: null },
      }),
    });
    const node = await mountOn(context);
    await click(q(node, ".nt-toggle"));
    await click(q(sheetOf(), '[data-focus$=":merge"]'));
    const confirm = q(sheetOf(), ".pr-confirm")!;
    expect(confirm).not.toBeNull();
    expect(confirm.nextElementSibling!.classList.contains("nt-acts")).toBe(true);
  });
});

describe("the bar's counts and the cut line with a source filter", () => {
  it("sizes and labels each segment from by_source, with held-by-chat items added, like the pills", async () => {
    const held = item("h1", { kind: "pr-failing", tone: "review", working: { session_id: "s", title: "Review", path: "/x", status: "working" } });
    const answer = overview([...many(2, "pr-failing"), held], {
      counts: { urgent: 9, review: 1, by_source: { git: { urgent: 9, review: 1 } } },
    });
    const node = await mount(answer);
    const seg = q(node, ".nt-seg")!;
    // 9 urgent + 1 to review + 1 with a chat, not the three listed rows.
    expect(q(seg, ".nt-seg-lab")!.textContent).toBe("Git repos 11");
    expect(seg.style.flexGrow).toBe("11");
    expect(all(seg, ".nt-bar i").map((i) => i.style.flexGrow)).toEqual(["9", "1", "1"]);
  });

  it("counts the cut line for the filtered source only", async () => {
    const answer = overview([...many(2, "pr-failing"), item("v1", { source: "vault", kind: "vault-soon", tone: "review", url: "", actions: ["review"] })], {
      counts: { urgent: 5, review: 3, by_source: { git: { urgent: 5, review: 0 }, vault: { urgent: 0, review: 3 } } },
      more: 6,
    });
    const node = await mount(answer);
    expect(q(node, ".attn-more.meta")!.textContent).toBe("6 more not shown");
    all(node, ".nt-seg")[1]!.click();
    await flush();
    // Vault: counted 3, listed 1.
    expect(q(node, ".attn-more.meta")!.textContent).toBe("2 more not shown");
    all(node, ".nt-seg")[0]!.click();
    await flush();
    expect(q(node, ".attn-more.meta")!.textContent).toBe("3 more not shown");
  });
});

describe("errors inside the phone sheet", () => {
  function setPhone(matches: boolean): void {
    window.matchMedia = vi.fn(() => ({ matches, addEventListener: vi.fn(), removeEventListener: vi.fn() })) as unknown as typeof window.matchMedia;
  }
  afterEach(() => {
    delete (window as { matchMedia?: unknown }).matchMedia;
    for (const n of all(document, ".nt-sheet")) n.remove();
  });

  it("shows a failed Review's message inside the sheet while it is open, not only in the row behind", async () => {
    setPhone(true);
    const { context } = routed({
      "GET /v1/ae/attention": okReply(overview([item("box-1", { source: "box", kind: "heat", url: "", title: "CPU at 98C", actions: ["review", "dismiss"] })])),
      "POST /v1/ae/attention/session": { ok: false, status: 502, body: { error: { code: "bad_gateway", message: "The chat service is down" } } },
    });
    const node = await mountOn(context);
    await click(q(node, ".nt-toggle"));
    const sheet = q(document.body, ".nt-sheet")!;
    await click(q(sheet, '[data-focus$=":review"]'));
    await flush();
    const inSheet = q(sheet, ".nt-sheet-msg .notice")!;
    expect(inSheet).not.toBeNull();
    expect(inSheet.textContent).toContain("Could not start the review");
    expect(inSheet.textContent).toContain("The chat service is down");
    // The sheet is the same node, still open.
    expect(q(document.body, ".nt-sheet")).toBe(sheet);
  });
});

describe("a stale message in the phone sheet", () => {
  function setPhone(matches: boolean): void {
    window.matchMedia = vi.fn(() => ({ matches, addEventListener: vi.fn(), removeEventListener: vi.fn() })) as unknown as typeof window.matchMedia;
  }
  afterEach(() => {
    delete (window as { matchMedia?: unknown }).matchMedia;
    for (const n of all(document, ".nt-sheet")) n.remove();
  });

  it("goes when a retry succeeds and the row stays open, so the sheet shows only what the row shows now", async () => {
    setPhone(true);
    let attempts = 0;
    const { context } = routed({
      "GET /v1/ae/attention": okReply(overview([item("box-1", { source: "box", kind: "heat", url: "", title: "CPU at 98C", actions: ["review", "dismiss"] })])),
      "POST /v1/ae/attention/session": () => {
        attempts += 1;
        return attempts === 1
          ? { ok: false, status: 502, body: { error: { code: "bad_gateway", message: "The chat service is down" } } }
          : okReply({ session_id: "s1", path: "/chats/s1", existing: false });
      },
    });
    const node = await mountOn(context);
    await click(q(node, ".nt-toggle"));
    const sheet = q(document.body, ".nt-sheet")!;
    await click(q(sheet, '[data-focus$=":review"]'));
    await flush();
    expect(q(sheet, ".nt-sheet-msg")!.textContent).toContain("Could not start the review");
    // The retry works; the list redraws with the row still open.
    await click(q(sheet, '[data-focus$=":review"]'));
    await flush();
    expect(attempts).toBe(2);
    expect(q(document.body, ".nt-sheet")).toBe(sheet);
    expect(q(sheet, ".nt-sheet-msg")!.childElementCount).toBe(0);
    expect(q(sheet, ".nt-sheet-msg")!.textContent).toBe("");
  });

  it("stops watching a row once the sheet closes", async () => {
    setPhone(true);
    const disconnect = vi.spyOn(MutationObserver.prototype, "disconnect");
    const { context } = routed({ "GET /v1/ae/attention": okReply(overview([item("box-1", { source: "box", kind: "heat", url: "", actions: ["review", "dismiss"] })])) });
    const node = await mountOn(context);
    await click(q(node, ".nt-toggle"));
    disconnect.mockClear();
    await click(q(document.body, ".nt-sheet .nt-close"));
    expect(disconnect).toHaveBeenCalled();
    expect(q(document.body, ".nt-sheet")).toBeNull();
    disconnect.mockRestore();
  });
});

describe("a row with no main action", () => {
  it("shows its Dismiss as the closed row's labelled action, and leaves the cell empty without one", async () => {
    const { context } = routed({
      "GET /v1/ae/attention": okReply(
        overview(
          [
            item("heat", { source: "box", kind: "heat", url: "", title: "CPU at 98C", actions: ["dismiss"] }),
            item("fixed", { source: "box", kind: "scan", url: "", title: "Not dismissible", dismissible: false, actions: [] }),
          ],
          { can_chat: false },
        ),
      ),
    });
    const node = await mountOn(context);
    const dismiss = q(node, '[data-id="heat"] .nt-cell [data-focus$=":dismiss"]')!;
    expect(dismiss.hasAttribute("data-primary")).toBe(true);
    expect(dismiss.textContent).toContain("Dismiss");
    expect(q(node, '[data-id="heat"] .nt-cell .attn-actions [data-primary]')).toBe(dismiss);
    // Snooze stays at the far right, after Dismiss, as on every other row.
    const acts = q(node, '[data-id="heat"] .nt-cell .attn-actions')!;
    expect(acts.lastElementChild!.classList.contains("nt-snooze-menu")).toBe(true);
    // No Dismiss either: the cell stays empty.
    expect(q(node, '[data-id="fixed"] .nt-cell .attn-actions button')).toBeNull();
  });

  it("keeps Dismiss quiet when the row has a real primary action", async () => {
    const { context } = routed({
      "GET /v1/ae/attention": okReply(overview([item("box-1", { source: "box", kind: "heat", url: "", actions: ["review", "dismiss"] })])),
    });
    const node = await mountOn(context);
    expect(q(node, '[data-id="box-1"] [data-focus$=":dismiss"]')!.hasAttribute("data-primary")).toBe(false);
    expect(q(node, '[data-id="box-1"] [data-focus$=":review"]')!.hasAttribute("data-primary")).toBe(true);
  });
});

// Option B (2026-11-15): the Overview of a wide page splits its areas into columns.
const BUSY = (): AttentionOverview =>
  overview([
    ...many(4, "pr-failing"),
    item("g-ready", { kind: "pr-ready", tone: "review", title: "Merge #12 Bump thing", actions: ["review", "merge", "dismiss"] }),
    item("box-1", { source: "box", kind: "heat", url: "", title: "CPU at 98C", actions: ["review", "dismiss"] }),
    item("box-2", { source: "box", kind: "heat", url: "", title: "Disk at 91 percent", actions: ["review", "dismiss"] }),
    ...[1, 2, 3].map((n) =>
      item(`v-${n}`, { source: "vault", kind: "vault-soon", tone: "review", url: "", title: `Key ${n} expires`, actions: ["review"] }),
    ),
    ...[1, 2].map((n) =>
      item(`acc-${n}`, { source: "accounts", kind: "account-full", tone: "review", url: "", title: `Window ${n} full`, actions: ["review"] }),
    ),
  ]);

const colsOf = (node: HTMLElement) =>
  all(node, ".nt-col").map((col) => all(col, ":scope > .nt-area").map((a) => a.dataset.area));

describe("the Overview in columns", () => {
  it("counts clamp(floor(width / 900), 1, 3) columns, and one under 1,800 px", async () => {
    const counts: Record<number, number> = {};
    for (const w of [1700, 1800, 2530, 5120]) {
      width = w;
      deactivateAttention();
      document.getElementById("root")!.replaceChildren();
      resetAttention();
      const node = await mount(BUSY());
      counts[w] = all(node, ".nt-col").length || 1;
      if (counts[w] === 1) expect(q(node, ".nt-table")!.classList.contains("is-cols")).toBe(false);
    }
    expect(counts).toEqual({ 1700: 1, 1800: 2, 2530: 2, 5120: 3 });
  });

  it("puts each area in the column with the fewest lines so far, header row per column", async () => {
    width = 2530;
    const node = await mount(BUSY());
    // Lines are counted (headings, rows, "Show N more"), never live pixels.
    const cols = colsOf(node);
    expect(cols.flat().sort()).toEqual(["box", "cloud", "money", "ship"]);
    expect(cols[0]![0]).toBe("ship");
    expect(cols[1]![0]).toBe("box");
    // Each column is its own table section: a group with a name and its own What, Checked, Action header.
    for (const col of all(node, ".nt-col")) {
      expect(col.getAttribute("role")).toBe("group");
      expect(col.getAttribute("aria-label")).toMatch(/^Needs you, column \d of 2: /);
      expect(all(col, ":scope > .nt-head")).toHaveLength(1);
      expect(q(col, ".nt-head")!.getAttribute("aria-hidden")).toBe("true");
    }
    // DOM order runs column by column, so a keyboard reads one area at a time.
    expect(all(node, ".nt-area").map((a) => a.dataset.area)).toEqual(cols.flat());
    // The toolbar and the composition bar sit above the columns, once.
    expect(all(node, ".attn-tools")).toHaveLength(1);
    expect(q(node, ".nt-comp")!.compareDocumentPosition(q(node, ".nt-table")!) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });

  it("moves no area when a row opens, and keeps assignment and focus across a same-count redraw", async () => {
    width = 5120;
    saveFold("attn.vault", true);
    const node = await mount(BUSY());
    const before = colsOf(node);
    expect(before).toHaveLength(3);
    const toggle = q(node, '[data-area="cloud"] .nt-row [data-focus$=":toggle"]')!;
    toggle.focus();
    toggle.click();
    await flush();
    expect(colsOf(node)).toEqual(before);
    // The opened row stays inside its column.
    const open = q(node, ".nt-row.is-open")!;
    expect(open.closest(".nt-col")).toBe(all(node, ".nt-col")[before.findIndex((c) => c.includes("cloud"))]);
    // A redraw with the same counts (a poll) changes nothing, and the focus stays where it was.
    const focused = document.activeElement?.getAttribute("data-focus");
    expect(focused).toMatch(/:toggle$/);
    table.redraw();
    await flush();
    expect(colsOf(node)).toEqual(before);
    expect(document.activeElement?.getAttribute("data-focus")).toBe(focused);
  });

  it("leaves the assignment alone when an area is folded, on Collapse all and on Show more", async () => {
    width = 2530;
    const node = await mount(() => overview([...many(8, "pr-failing"), ...BUSY().items.slice(4)]));
    const before = colsOf(node);
    expect(before.flat().sort()).toEqual(["box", "cloud", "money", "ship"]);
    q(node, '[data-line="nt.area.ship"] .nt-grp-btn')!.click();
    await flush();
    expect(colsOf(node)).toEqual(before);
    q(node, '[data-line="nt.area.ship"] .nt-grp-btn')!.click();
    await flush();
    q(node, '[data-line="attn.git"] .nt-more')!.click();
    await flush();
    expect(all(node, '[data-line="attn.git"] .nt-row').length).toBeGreaterThan(5);
    expect(colsOf(node)).toEqual(before);
    all(node, ".attn-tools button").find((b) => b.getAttribute("aria-label") === "Collapse all")!.click();
    await flush();
    expect(all(node, ".nt-grp-btn").every((b) => b.getAttribute("aria-expanded") === "false")).toBe(true);
    expect(colsOf(node)).toEqual(before);
  });

  it("keeps every area in its column when a poll brings or clears rows", async () => {
    width = 2530;
    let extra = 0;
    const node = await mount(() =>
      overview([
        ...BUSY().items,
        ...Array.from({ length: extra }, (_, n) =>
          item(`box-x${n}`, { source: "box", kind: "heat", url: "", title: `Heat ${n}`, actions: ["review", "dismiss"] }),
        ),
      ]),
    );
    const before = colsOf(node);
    extra = 8;
    window.dispatchEvent(new Event("focus"));
    await flush();
    expect(colsOf(node)).toEqual(before);
    extra = 0;
    window.dispatchEvent(new Event("focus"));
    await flush();
    expect(colsOf(node)).toEqual(before);
  });

  it("places the columns again when an area appears", async () => {
    width = 2530;
    let withMoney = false;
    const node = await mount(() =>
      overview([...BUSY().items.filter((row) => withMoney || row.source !== "accounts")]),
    );
    expect(colsOf(node).flat().sort()).toEqual(["box", "cloud", "ship"]);
    withMoney = true;
    window.dispatchEvent(new Event("focus"));
    await flush();
    expect(colsOf(node).flat().sort()).toEqual(["box", "cloud", "money", "ship"]);
  });

  const colOf = (node: HTMLElement) =>
    Object.fromEntries(all(node, ".nt-col").flatMap((col, i) => all(col, ":scope > .nt-area").map((a) => [a.dataset.area, i])));

  it("does not move the areas that stay when an area appears or empties", async () => {
    width = 2530;
    let rows = BUSY().items.filter((row) => row.source !== "accounts");
    const node = await mount(() => overview(rows));
    const before = colOf(node);
    rows = BUSY().items;
    window.dispatchEvent(new Event("focus"));
    await flush();
    const added = colOf(node);
    for (const area of Object.keys(before)) expect(added[area]).toBe(before[area]);
    expect(Object.keys(added).sort()).toEqual(["box", "cloud", "money", "ship"]);
    // Box empties: Ship, Cloud and Accounts keep their columns.
    rows = BUSY().items.filter((row) => row.source !== "box");
    window.dispatchEvent(new Event("focus"));
    await flush();
    const left = colOf(node);
    expect(Object.keys(left).sort()).toEqual(["cloud", "money", "ship"]);
    for (const area of Object.keys(left)) expect(left[area]).toBe(added[area]);
  });

  it("draws a column whose areas all left as a hidden blank spacer, and moves nothing else", async () => {
    width = 5120;
    let rows = BUSY().items;
    const node = await mount(() => overview(rows));
    const before = colOf(node);
    // Every area in the middle column leaves (Box and Accounts sit there at this width).
    const leaving = Object.entries(before).filter(([, col]) => col === 1).map(([area]) => area);
    expect(leaving.length).toBeGreaterThan(0);
    const sourceOf = { ship: "git", box: "box", cloud: "vault", money: "accounts" } as const;
    rows = BUSY().items.filter((row) => !leaving.some((area) => row.source === sourceOf[area as keyof typeof sourceOf]));
    window.dispatchEvent(new Event("focus"));
    await flush();
    const cols = all(node, ".nt-col");
    expect(cols).toHaveLength(3);
    const gap = cols[1]!;
    expect(gap.classList.contains("is-empty")).toBe(true);
    expect(gap.getAttribute("aria-hidden")).toBe("true");
    expect(gap.querySelector(".nt-head, .nt-area")).toBeNull();
    expect(gap.hasAttribute("role")).toBe(false);
    expect(gap.hasAttribute("aria-label")).toBe(false);
    const after = colOf(node);
    for (const area of Object.keys(after)) expect(after[area]).toBe(before[area]);
  });

  it("does not move the areas that stay when a source filter hides some and is cleared", async () => {
    width = 2530;
    const node = await mount(BUSY());
    const before = colOf(node);
    q(node, '[data-focus="seg:vault"]')!.click();
    await flush();
    q(node, '[data-focus="filter:clear"]')!.click();
    await flush();
    expect(colOf(node)).toEqual(before);
  });

  it("plans the open state and places the columns from the same, source-filtered areas", async () => {
    width = 2530;
    const node = await mount(BUSY());
    q(node, '[data-focus="seg:vault"]')!.click();
    await flush();
    // One area is left, so one table: its source is open (planned on it, not on every area).
    expect(all(node, ".nt-area").map((a) => a.dataset.area)).toEqual(["cloud"]);
    expect(all(node, ".nt-col")).toHaveLength(0);
    expect(q(node, '[data-line="attn.vault"] .nt-rows')!.hidden).toBe(false);
  });

  it("follows a ResizeObserver to another count of columns, and keeps two columns 20 px under 1,800", async () => {
    const watchers: Array<() => void> = [];
    vi.stubGlobal(
      "ResizeObserver",
      class {
        constructor(cb: () => void) {
          watchers.push(cb);
        }
        observe() {}
        disconnect() {}
      },
    );
    try {
      width = 1000;
      const node = await mount(BUSY());
      expect(all(node, ".nt-col")).toHaveLength(0);
      const resize = async (w: number) => {
        width = w;
        for (const watch of watchers) watch();
        await flush();
      };
      await resize(2530);
      expect(all(node, ".nt-col")).toHaveLength(2);
      await resize(1780);
      expect(all(node, ".nt-col")).toHaveLength(2);
      await resize(1700);
      expect(all(node, ".nt-col")).toHaveLength(0);
      await resize(1780);
      expect(all(node, ".nt-col")).toHaveLength(0);
    } finally {
      vi.unstubAllGlobals();
    }
  });

  it("keeps the focus on a toolbar button across a change in the column count", async () => {
    width = 1000;
    const node = await mount(BUSY());
    const tool = all(node, ".attn-tools button").find((b) => b.getAttribute("aria-label") === "Expand urgent")!;
    expect(tool.dataset.focus).toBe("tool:expand");
    tool.focus();
    width = 2530;
    table.redraw();
    await flush();
    expect(document.activeElement?.getAttribute("data-focus")).toBe("tool:expand");
  });

  it("keeps an area tab one table, whatever the width", async () => {
    width = 5120;
    const node = await mount(BUSY());
    table.setArea("ship");
    await flush();
    expect(all(node, ".nt-col")).toHaveLength(0);
    expect(all(node, ".nt-table")).toHaveLength(1);
    expect(q(node, ".nt-table")!.classList.contains("is-cols")).toBe(false);
  });
});

describe("a row's message (2026-11-16)", () => {
  const GH = "https://github.com/contoso/web/security/dependabot";
  const SET = (): AttentionOverview =>
    overview([item("alerts-1", { kind: "alerts-fix", tone: "urgent", title: "61 Dependabot alerts: Pillow", url: GH, actions: ["dismiss"] }), ...many(2, "pr-failing")]);

  it("a refused Open on GitHub shows in the opened row's message line, never inside the row grid, and a redraw keeps it", async () => {
    openRefused = true;
    const node = await mount(SET());
    q(node, '[data-id="alerts-1"] .nt-toggle')!.click();
    await flush();
    q(node, '[data-id="alerts-1"] [data-extra="github"]')!.click();
    await flush();
    const line = () => q(node, '[data-id="alerts-1"] .nt-panel > .nt-panel-main > .nt-panel-msg')!;
    expect(line().textContent).toContain("Could not open GitHub from here");
    expect(q(line(), "code")!.textContent).toBe(GH);
    expect(q(node, '[data-id="alerts-1"]')!.querySelector(":scope > .flash")).toBeNull();
    table.redraw();
    await flush();
    expect(line().textContent).toContain(GH);
    await vi.advanceTimersByTimeAsync(31_000);
    expect(line().textContent).toBe("");
  });

  it("a closed row's message shows in the page's message line", async () => {
    openRefused = true;
    const node = await mount(SET());
    q(node, '[data-id="alerts-1"] .attn-title')!.click();
    await flush();
    expect(q(node, ".nt-page-msg")!.textContent).toContain(GH);
    expect(q(node, '[data-id="alerts-1"]')!.querySelector(":scope > .flash")).toBeNull();
  });
});

describe("every row action's result goes through the row message (2026-11-16, fix round 1)", () => {
  const FAIL = { ok: false, status: 502, body: { error: { code: "bad_gateway", message: "The chat service is down" } } };
  const box = (id: string, extra: Record<string, unknown> = {}) =>
    item(id, { source: "box", kind: "heat", url: "", title: `CPU at ${id}`, actions: ["review", "dismiss"], ...extra });
  const msgOf = (node: HTMLElement, id: string) => q(node, `[data-id="${id}"] .nt-panel > .nt-panel-main > .nt-panel-msg`)!;

  it("a failed Review on an opened row shows full width in its message line and survives a redraw", async () => {
    const { context } = routed({ [LIST]: okReply(overview([box("box-1")])), "POST /v1/ae/attention/session": FAIL });
    const node = await mountOn(context);
    await click(q(node, '[data-id="box-1"] .nt-toggle'));
    await click(q(node, '[data-id="box-1"] .nt-panel [data-focus$=":review"]'));
    expect(msgOf(node, "box-1").textContent).toContain("Could not start the review");
    expect(msgOf(node, "box-1").textContent).toContain("The chat service is down");
    // Nothing landed in the What cell or the row grid.
    expect(q(node, '[data-id="box-1"] .attn-text .flash')).toBeNull();
    expect(q(node, '[data-id="box-1"]')!.querySelector(":scope > .flash")).toBeNull();
    table.redraw();
    await flush();
    expect(msgOf(node, "box-1").textContent).toContain("Could not start the review");
    await vi.advanceTimersByTimeAsync(31_000);
    expect(msgOf(node, "box-1").textContent).toBe("");
  });

  it("a failed Snooze on an opened row shows in its message line and survives a redraw", async () => {
    const { context } = routed({ [LIST]: okReply(overview([box("box-1")])), "POST /v1/ae/attention/snooze": FAIL });
    const node = await mountOn(context);
    await click(q(node, '[data-id="box-1"] .nt-toggle'));
    await click(q(node, '[data-id="box-1"] .nt-panel [aria-label="Snooze CPU at box-1"]'));
    await click(all(node, '[data-id="box-1"] [role="menuitem"], [data-id="box-1"] .nt-snooze button').find((b) => /until/.test(b.getAttribute("aria-label") ?? "") || b.textContent === "Tomorrow") ?? null);
    expect(msgOf(node, "box-1").textContent).toContain("Could not snooze");
    table.redraw();
    await flush();
    expect(msgOf(node, "box-1").textContent).toContain("Could not snooze");
  });

  it("a held message goes when a different row opens", async () => {
    const { context } = routed({ [LIST]: okReply(overview([box("box-1"), box("box-2")])), "POST /v1/ae/attention/session": FAIL });
    const node = await mountOn(context);
    await click(q(node, '[data-id="box-1"] .nt-toggle'));
    await click(q(node, '[data-id="box-1"] .nt-panel [data-focus$=":review"]'));
    expect(msgOf(node, "box-1").textContent).toContain("Could not start");
    await click(q(node, '[data-id="box-2"] .nt-toggle'));
    expect(q(node, '[data-id="box-2"] .nt-panel-msg')!.textContent).toBe("");
    expect(q(node, ".nt-page-msg")!.textContent).toBe("");
    await click(q(node, '[data-id="box-1"] .nt-toggle'));
    expect(msgOf(node, "box-1").textContent).toBe("");
  });

  it("a held message goes at the start of the next action on that row", async () => {
    let attempts = 0;
    const { context } = routed({
      [LIST]: okReply(overview([box("box-1")])),
      "POST /v1/ae/attention/session": () => {
        attempts += 1;
        return attempts === 1 ? FAIL : { ok: false, status: 502, body: { error: { code: "x", message: "Still down" } } };
      },
    });
    const node = await mountOn(context);
    await click(q(node, '[data-id="box-1"] .nt-toggle'));
    await click(q(node, '[data-id="box-1"] .nt-panel [data-focus$=":review"]'));
    expect(msgOf(node, "box-1").textContent).toContain("The chat service is down");
    await click(q(node, '[data-id="box-1"] .nt-panel [data-focus$=":review"]'));
    expect(msgOf(node, "box-1").textContent).toContain("Still down");
    expect(msgOf(node, "box-1").textContent).not.toContain("The chat service is down");
  });

  it("a held message goes when another action on the closed row is pressed", async () => {
    let attempts = 0;
    const { context } = routed({
      [LIST]: okReply(overview([box("box-1")])),
      "POST /v1/ae/attention/session": () => {
        attempts += 1;
        return attempts === 1 ? FAIL : { ok: false, status: 502, body: { error: { code: "x", message: "Still down" } } };
      },
    });
    const node = await mountOn(context);
    // The row stays closed: its header Review shows the failure in the page's message line.
    await click(q(node, '[data-id="box-1"] [data-focus$=":review"]'));
    expect(q(node, ".nt-page-msg")!.textContent).toContain("The chat service is down");
    await click(q(node, '[data-id="box-1"] [data-focus$=":review"]'));
    expect(q(node, ".nt-page-msg")!.textContent).toContain("Still down");
    expect(q(node, ".nt-page-msg")!.textContent).not.toContain("The chat service is down");
  });

  it("closing the open row by its own toggle keeps its message, which moves to the page line", async () => {
    const { context } = routed({ [LIST]: okReply(overview([box("box-1")])), "POST /v1/ae/attention/session": FAIL });
    const node = await mountOn(context);
    await click(q(node, '[data-id="box-1"] .nt-toggle'));
    await click(q(node, '[data-id="box-1"] .nt-panel [data-focus$=":review"]'));
    expect(msgOf(node, "box-1").textContent).toContain("Could not start the review");
    await click(q(node, '[data-id="box-1"] .nt-toggle'));
    expect(q(node, '[data-id="box-1"] .nt-panel')).toBeNull();
    expect(q(node, ".nt-page-msg")!.textContent).toContain("Could not start the review");
  });

  it("a message about a row that left the list is dropped, never moved to the page line", async () => {
    let listed = overview([box("box-1"), box("box-2")]);
    const { context } = routed({ [LIST]: () => okReply(listed), "POST /v1/ae/attention/session": FAIL });
    const node = await mountOn(context);
    await click(q(node, '[data-id="box-1"] .nt-toggle'));
    await click(q(node, '[data-id="box-1"] .nt-panel [data-focus$=":review"]'));
    expect(msgOf(node, "box-1").textContent).toContain("Could not start");
    listed = overview([box("box-2")]);
    window.dispatchEvent(new Event("focus"));
    await flush();
    await flush();
    expect(q(node, '[data-id="box-1"]')).toBeNull();
    expect(q(node, ".nt-page-msg")!.textContent).toBe("");
  });

  it("a second Open on GitHub restarts the 30 second hold", async () => {
    openRefused = true;
    const GH = "https://github.com/contoso/web/security/dependabot";
    const { context } = routed({ [LIST]: okReply(overview([item("alerts-1", { kind: "alerts-fix", tone: "urgent", title: "61 Dependabot alerts: Pillow", url: GH, actions: ["dismiss"] })])) });
    (context.navigation as { openExternal: unknown }).openExternal = vi.fn(async () => {
      throw new Error("URL was not provided by the host");
    });
    const node = await mountOn(context);
    await click(q(node, '[data-id="alerts-1"] .nt-toggle'));
    const open = () => click(q(node, '[data-id="alerts-1"] [data-extra="github"]'));
    await open();
    await vi.advanceTimersByTimeAsync(20_000);
    await open();
    await vi.advanceTimersByTimeAsync(20_000);
    // 40 s after the first press, 20 s after the second: still held.
    expect(msgOf(node, "alerts-1").textContent).toContain(GH);
    await vi.advanceTimersByTimeAsync(11_000);
    expect(msgOf(node, "alerts-1").textContent).toBe("");
  });

  it("on a phone, a message for another row shows in the open sheet's message area", async () => {
    window.matchMedia = vi.fn(() => ({ matches: true, addEventListener: vi.fn(), removeEventListener: vi.fn() })) as unknown as typeof window.matchMedia;
    const GH = "https://github.com/contoso/web/security/dependabot";
    const { context } = routed({ [LIST]: okReply(overview([item("alerts-1", { kind: "alerts-fix", tone: "urgent", title: "61 Dependabot alerts: Pillow", url: GH, actions: ["dismiss"] }), box("box-1")])) });
    (context.navigation as { openExternal: unknown }).openExternal = vi.fn(async () => {
      throw new Error("URL was not provided by the host");
    });
    const node = await mountOn(context);
    // The sheet is open for box-1; a result for alerts-1 (a row behind it) lands: it shows in the sheet, not nowhere.
    await click(q(node, '[data-id="box-1"] .nt-toggle'));
    const sheet = q(document.body, ".nt-sheet")!;
    await click(q(node, '[data-id="alerts-1"] .attn-title'));
    expect(q(sheet, ".nt-sheet-msg")!.textContent).toContain(GH);
    await click(q(sheet, ".nt-close"));
    for (const n of all(document, ".nt-sheet")) n.remove();
    delete (window as { matchMedia?: unknown }).matchMedia;
  });
});

describe("the new-urgent marker (2026-11-17)", () => {
  const poll = async () => {
    window.dispatchEvent(new Event("focus"));
    await flush();
  };
  const head = (node: HTMLElement, line: string) => q(node, `[data-line="${line}"] > .nt-grp-head .nt-grp-btn`)!;
  const markers = (node: HTMLElement) => all(node, ".nt-new");

  it("marks a closed source that gains an urgent item, never reopens it, and clears on open", async () => {
    let answer = overview([...many(1, "pr-failing")]);
    const node = await mount(() => answer);
    // The first draw in a browser counts the current urgent items as seen: no marker storm.
    expect(markers(node)).toHaveLength(0);
    answer = overview([...many(2, "pr-failing")]);
    await poll();
    expect(markers(node)).toHaveLength(1);
    const dot = markers(node)[0]!;
    expect(head(node, "attn.git").contains(dot)).toBe(true);
    expect(dot.getAttribute("aria-label")).toBe("new urgent item");
    expect(dot.getAttribute("title")).toBeTruthy();
    // It adds no text to the line: the head's visible text is what it was.
    expect(dot.textContent).toBe("");
    // The line stays closed: the marker replaces the old reopen.
    expect(head(node, "attn.git").getAttribute("aria-expanded")).toBe("false");
    // Opening marks the current ids seen and clears the marker; closing again shows none.
    await click(head(node, "attn.git"));
    expect(markers(node)).toHaveLength(0);
    await click(head(node, "attn.git"));
    expect(head(node, "attn.git").getAttribute("aria-expanded")).toBe("false");
    expect(markers(node)).toHaveLength(0);
    // Another new one while it is closed marks it again.
    answer = overview([...many(3, "pr-failing")]);
    await poll();
    expect(markers(node)).toHaveLength(1);
  });

  it("does not mark for a review-only item or an unchanged read, and still works with no storage", async () => {
    const getItem = vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new Error("no storage");
    });
    const setItem = vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("no storage");
    });
    try {
      let answer = overview([...many(1, "pr-failing")]);
      const node = await mount(() => answer);
      await poll();
      expect(markers(node)).toHaveLength(0);
      answer = overview([...many(1, "pr-failing"), item("r1", { kind: "pr-update", tone: "review" })]);
      await poll();
      expect(markers(node)).toHaveLength(0);
      answer = overview([...many(2, "pr-failing")]);
      await poll();
      expect(markers(node)).toHaveLength(1);
    } finally {
      getItem.mockRestore();
      setItem.mockRestore();
    }
  });
});

describe("one button system (2026-11-17)", () => {
  it("gives Review N the secondary kind, and Snooze a real label in the DOM", async () => {
    saveFold("attn.git", true);
    const node = await mount(overview(many(3, "pr-failing")));
    const review = all(node, ".attn-review-all").find((b) => /^Review \d+/.test(b.textContent ?? ""));
    expect(review).toBeTruthy();
    expect(review!.classList.contains("btn")).toBe(true);
    expect(review!.classList.contains("btn-secondary")).toBe(true);
    // Snooze on a closed row: an icon button whose word is a real label (a phone page shows it).
    const word = q(node, ".nt-row .nt-snooze-menu .nt-snooze-word");
    expect(word).not.toBeNull();
    expect(word!.textContent).toBe("Snooze");
  });

  it("marks the toolbar's primary, quiet and secondary buttons of an opened row", async () => {
    saveFold("attn.git", true);
    const node = await mount(SAMPLE());
    await click(q(node, '[data-id="g-ready"] .nt-toggle'));
    const row = q(node, '[data-id="g-ready"]')!;
    const toolbar = q(row, ".nt-toolbar")!;
    expect(all(toolbar, ".nt-primary")).toHaveLength(1);
    expect(q(toolbar, ".nt-dismiss")!.classList.contains("btn")).toBe(true);
    expect(q(toolbar, ".nt-snooze-btn")!.classList.contains("btn-quiet")).toBe(true);
    expect(q(row, ".nt-close")!.classList.contains("btn-quiet")).toBe(true);
    // Open on GitHub is a labelled secondary button: neither the primary nor one of the quiet kinds.
    const github = all(toolbar, ".nt-labelled").find((b) => b.textContent === "Open on GitHub");
    expect(github).toBeTruthy();
    expect(github!.classList.contains("nt-primary")).toBe(false);
    expect(github!.matches(".nt-snooze-btn, .nt-dismiss, .nt-close")).toBe(false);
  });
});
