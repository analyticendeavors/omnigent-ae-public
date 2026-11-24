// The opened row's panel in layout B (2026-11-16, the owner's pick): one toolbar across the top
// with every action, Snooze as a menu button, and two columns under it (the evidence, facts, hint
// and picture on the left, the history on the right). The phone's sheet keeps its single column.
import type { ExtensionContext } from "@omnigent/extension-sdk";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { AttentionOverview } from "../src/api";
import { activateAttention, attentionSection, deactivateAttention, resetAttentionSeen } from "../src/attention";
import { resetAttention } from "../src/attentionStore";
import { resetFolds, saveFold } from "../src/fold";
import { resetAdmins, resetAssignedView } from "../src/needsAssign";
import { resetHistories } from "../src/needsMore";
import { resetSelection } from "../src/needsSelect";
import { createNeedsTable } from "../src/needsTable";
import { resetTiles } from "../src/tiles";
import { item, overview } from "./attentionFixtures";

vi.mock("../src/boxHealth", () => ({ openCleanup: vi.fn(() => true) }));
const visual = vi.hoisted(() => ({ state: "ready" as string, blob: null as Blob | null }));
vi.mock("../src/socialVisual", () => ({
  visualState: () => visual.state,
  loadedVisual: () => visual.blob,
  visualUrl: () => (visual.blob ? "blob:picture-1" : null),
  showPictureUrl: () => (visual.blob ? "blob:picture-1" : null),
  revokePictureUrls: () => undefined,
  canCopyPicture: () => false,
  copyPicture: async () => null,
  downloadPicture: () => true,
  loadVisual: () => Promise.resolve(visual.blob),
}));

const flush = () => vi.advanceTimersByTimeAsync(0);
const all = (node: ParentNode, sel: string) => [...node.querySelectorAll<HTMLElement>(sel)];
const q = (node: ParentNode, sel: string) => node.querySelector<HTMLElement>(sel);
/** The toolbar's visible items: the row's own "..." menu (only Open on GitHub, which the toolbar has as a button) is hidden by style.css. */
const shown = (bar: ParentNode) => all(bar, ".attn-actions > *").filter((n) => !n.classList.contains("menu-wrap") || n.classList.contains("nt-snooze-drop"));
const ok = (body: unknown) => ({ ok: true, status: 200, body });

async function mount(list: AttentionOverview) {
  // The Overview opens collapsed (2026-11-17); these tests need the git rows drawn, so the person opened it.
  saveFold("attn.git", true);
  const request = vi.fn(async ({ method, path }: { method: string; path: string }) => {
    if (`${method} ${path}` === "GET /v1/ae/attention") return ok(list);
    if (path === "/v1/ae/attention/admins") return ok({ admins: [{ id: "owner@example.com", name: "owner@example.com" }] });
    if (path.startsWith("/v1/ae/attention/history")) {
      return ok({ id: "x", first_seen: null, events: [{ at: "2026-11-15T09:00:00Z", what: "first-seen", by: null }] });
    }
    return { ok: false, status: 500, body: { error: { code: "unexpected", message: path } } };
  });
  const context = {
    view: "fabric",
    server: { request },
    navigation: { openSession: vi.fn(), openExternal: vi.fn(), openPage: vi.fn() },
  } as unknown as ExtensionContext;
  const table = createNeedsTable({ openTab: vi.fn(), openCard: vi.fn(), hasTab: () => true, measure: () => 1000 });
  const node = attentionSection();
  document.getElementById("root")?.appendChild(node);
  activateAttention(context, node, { waiting: false, table: table.hooks });
  await flush();
  return { node, request };
}

const open = async (node: HTMLElement, id: string) => {
  q(node, `[data-id="${CSS.escape(id)}"] .nt-toggle`)!.click();
  await flush();
  await flush();
  return q(node, `[data-id="${CSS.escape(id)}"]`)!;
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
  visual.state = "ready";
  visual.blob = null;
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

const GIT = () =>
  item("git:pr-ready:contoso/web#8", {
    kind: "pr-ready",
    tone: "review",
    title: "Merge #8 Bump",
    url: "https://github.com/contoso/web/pull/8",
    actions: ["review", "merge", "dismiss"],
  });
const POST = (fields = {}) =>
  item("social:li-1", { source: "social", kind: "post-overdue", title: "Diagram tool", url: "", slot: "li-1", visual: { kind: "tool", key: "https://img.example/x.jpg" }, actions: ["dismiss"], ...fields });
const ME = { me: "owner@example.com" };

describe("the toolbar", () => {
  it("holds every action in one row: primary first, the others after it, Snooze, then Dismiss last", async () => {
    const { node } = await mount(overview([GIT()], ME));
    const row = await open(node, "git:pr-ready:contoso/web#8");
    const bar = q(row, ".nt-panel .nt-toolbar")!;
    const kids = shown(bar);
    const names = kids.map((n) => {
      if (n.classList.contains("nt-assign")) return "assign";
      if (n.classList.contains("nt-snooze-drop")) return "snooze";
      return n.dataset.focus?.split(":").pop() ?? n.className;
    });
    expect(names[0]).toBe("merge"); // the primary
    expect(names.slice(-2)).toEqual(["snooze", "dismiss"]);
    const at = (name: string) => names.indexOf(name);
    expect(at("review")).toBeGreaterThan(0);
    expect(at("github")).toBeGreaterThan(at("review"));
    expect(at("assign")).toBeGreaterThan(at("github"));
    expect(at("snooze")).toBeGreaterThan(at("assign"));
    // The toolbar is one box with no other action elsewhere in the panel.
    expect(all(row, ".nt-panel .attn-actions")).toHaveLength(1);
    expect(q(row, ".nt-snooze")).toBeNull();
  });

  it("keeps every action, fact and box the panel had: nothing was dropped", async () => {
    const { node } = await mount(overview([GIT()], ME));
    const row = await open(node, "git:pr-ready:contoso/web#8");
    const panel = q(row, ".nt-panel")!;
    for (const part of [".nt-evidence", ".nt-facts", ".nt-hint", ".nt-hist", ".nt-assign", ".nt-snooze-drop", ".nt-dismiss", '[data-extra="github"]', '[data-focus$=":review"]', '[data-focus$=":merge"]']) {
      expect(q(panel, part), part).not.toBeNull();
    }
    expect(all(panel, ".nt-fact")).toHaveLength(4);
  });

  it("makes Snooze a menu button: it opens on a click, a choice is a menu item, Escape closes it and the focus returns", async () => {
    const { node } = await mount(overview([GIT()], ME));
    const row = await open(node, "git:pr-ready:contoso/web#8");
    const trigger = q(row, ".nt-snooze-drop .btn-menu") as HTMLButtonElement;
    const menu = q(row, ".nt-snooze-drop .menu")!;
    expect(trigger.getAttribute("aria-haspopup")).toBe("menu");
    expect(trigger.getAttribute("aria-controls")).toBe(menu.id);
    expect(trigger.getAttribute("aria-label")).toBe("Snooze Merge #8 Bump");
    expect(menu.hidden).toBe(true);
    trigger.focus();
    trigger.dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowDown", bubbles: true }));
    expect(menu.hidden).toBe(false);
    expect(trigger.getAttribute("aria-expanded")).toBe("true");
    const items = all(menu, '[role="menuitem"]');
    expect(items.map((b) => b.textContent)).toEqual(["Tomorrow", "Monday", "In a week"]);
    expect(document.activeElement).toBe(items[0]);
    items[0]!.dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowDown", bubbles: true }));
    expect(document.activeElement).toBe(items[1]);
    items[1]!.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
    expect(menu.hidden).toBe(true);
    expect(trigger.getAttribute("aria-expanded")).toBe("false");
    expect(document.activeElement).toBe(trigger);
  });

  it("gives a row that cannot be snoozed no Snooze, and the toolbar still ends with Dismiss", async () => {
    const { node } = await mount(overview([item("m", { source: "git-mine", actions: ["dismiss"] })], ME));
    const row = await open(node, "m");
    expect(q(row, ".nt-snooze-drop")).toBeNull();
    expect(q(row, ".nt-assign")).toBeNull();
    expect(shown(row).pop()!.classList.contains("nt-dismiss")).toBe(true);
  });
});

describe("the two columns", () => {
  it("puts the evidence, facts and hint on the left and the history on the right", async () => {
    const { node } = await mount(overview([GIT()], ME));
    const row = await open(node, "git:pr-ready:contoso/web#8");
    const body = q(row, ".nt-panel .nt-body")!;
    expect([...body.children].map((c) => c.className.split(" ")[0])).toEqual(["nt-left", "nt-panel-side"]);
    expect(q(body, ".nt-panel-side")!.classList.contains("nt-hist")).toBe(true);
    expect(q(body, ".nt-left .nt-hist")).toBeNull();
  });

  it("lays a Social row out in thirds: the evidence across the top, then the post, the picture, and the facts over the history", async () => {
    visual.blob = new Blob(["x"], { type: "image/jpeg" });
    const { node } = await mount(overview([POST()], ME));
    const row = await open(node, "social:li-1");
    const grid = q(row, ".nt-panel .nt-b3")!;
    expect([...grid.children].map((c) => c.className.split(" ").slice(0, 2).join(" "))).toEqual(["nt-b3-ev", "nt-b3-col nt-b3-post", "nt-b3-col nt-b3-pic", "nt-b3-col nt-b3-side"]);
    expect(q(grid, ".nt-b3-ev > .nt-evidence")).not.toBeNull();
    expect(q(grid, ".nt-b3-post > .nt-post")).not.toBeNull();
    expect(q(grid, ".nt-b3-post > .nt-hint")!.textContent).toMatch(/Skip post/);
    expect(q(grid, ".nt-b3-pic > .nt-picbox .nt-pic-img")).not.toBeNull();
    const side = q(grid, ".nt-b3-side")!;
    expect([...side.children].map((c) => c.className.split(" ")[0])).toEqual(["nt-facts", "nt-panel-side"]);
    expect(side.lastElementChild!.classList.contains("nt-hist")).toBe(true);
    expect(q(row, ".nt-left")).toBeNull();
  });

  it("keeps a Git row's two columns as they are", async () => {
    const { node } = await mount(overview([GIT()], ME));
    const row = await open(node, "git:pr-ready:contoso/web#8");
    expect(q(row, ".nt-b3")).toBeNull();
    expect([...q(row, ".nt-body")!.children].map((c) => c.className.split(" ")[0])).toEqual(["nt-left", "nt-panel-side"]);
  });

  it("says No picture yet as one short line on the left, not a box", async () => {
    visual.state = "none";
    const { node } = await mount(overview([POST({ visual: null })], ME));
    const row = await open(node, "social:li-1");
    const line = q(row, ".nt-picbox > .nt-pic")!;
    expect(line.dataset.state).toBe("none");
    expect(line.textContent).toBe("No picture yet");
    expect(line.children).toHaveLength(1);
    expect(line.firstElementChild!.tagName).toBe("P");
    expect(q(row, ".nt-hist .nt-pic")).toBeNull();
  });
});

describe("the phone's sheet", () => {
  it("keeps its single column: evidence and facts, the pinned actions, Assign and Snooze boxes, then the history", async () => {
    window.matchMedia = vi.fn(() => ({ matches: true, addEventListener: vi.fn(), removeEventListener: vi.fn() })) as unknown as typeof window.matchMedia;
    const { node } = await mount(overview([GIT()], ME));
    q(node, '[data-id="git:pr-ready:contoso/web#8"] .nt-toggle')!.click();
    await flush();
    await flush();
    const sheet = q(document.body, ".nt-sheet")!;
    expect(sheet).not.toBeNull();
    const main = q(sheet, ".nt-panel-main")!;
    expect([...main.children].map((c) => c.className.split(" ")[0])).toEqual([
      "nt-evidence",
      "nt-facts",
      "nt-acts",
      "nt-assign",
      "nt-snooze",
      "nt-hint",
      "nt-panel-side",
    ]);
    expect(q(main, ".nt-toolbar")).toBeNull();
    expect(q(main, ".nt-snooze-drop")).toBeNull();
    expect(q(main, ".nt-assign")!.classList.contains("is-inline")).toBe(false);
  });
});
