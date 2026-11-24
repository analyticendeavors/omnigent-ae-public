// An opened Social row in Needs you is ready to post from (2026-11-16): the draft text and picture in the
// row, the card's own actions in the toolbar, Skip post in place of Dismiss, the bulk bar and the
// Dismissed fold. Every server call and every shared card action is mocked; socialActions.ts and
// socialVisual.ts have their own tests.
import type { ExtensionContext } from "@omnigent/extension-sdk";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { AttentionOverview } from "../src/api";
import { activateAttention, attentionSection, deactivateAttention, resetAttentionSeen } from "../src/attention";
import { resetAttention, setAttention } from "../src/attentionStore";
import { resetFolds } from "../src/fold";
import { resetAdmins, resetAssignedView } from "../src/needsAssign";
import { resetHistories } from "../src/needsMore";
import { planButtonCount, resetPosts } from "../src/needsPost";
import { SOCIAL_BULK_NOTE, resetSelection } from "../src/needsSelect";
import { createNeedsTable } from "../src/needsTable";
import { resetTiles } from "../src/tiles";
import css from "../src/style.css?raw";
import { item, overview } from "./attentionFixtures";

vi.mock("../src/boxHealth", () => ({ openCleanup: vi.fn(() => true) }));
const actions = vi.hoisted(() => ({
  copyDraft: vi.fn(async (_text: string): Promise<"Copied" | "Copy failed"> => "Copied"),
  openNeedsEditor: vi.fn(() => false),
  openPost: vi.fn(async () => ({ label: "Copied, opened" }) as { label?: string; note?: { text: string; tone: "info" | "error" } }),
  postPosted: vi.fn(async () => ({ ok: true })),
  postSkipped: vi.fn(async () => ({ ok: true })),
  // The promise clipboard: on by default; a test turns it off to reach the held-text fallback.
  canCopyFresh: vi.fn(() => true),
  written: [] as string[],
  writeRefuses: false,
  startFreshCopy: vi.fn((text: Promise<string>): Promise<boolean> | null => {
    if (!actions.canCopyFresh()) return null;
    return text.then(
      (value) => {
        if (actions.writeRefuses) return false;
        actions.written.push(value);
        return true;
      },
      () => false,
    );
  }),
}));
vi.mock("../src/socialActions", () => actions);
const visual = vi.hoisted(() => ({
  state: "ready" as string,
  blob: null as Blob | null,
  load: vi.fn(),
  canCopy: true,
  pending: null as Promise<Blob | null> | null,
  copy: vi.fn(async (_blob: Blob): Promise<string | null> => null),
  download: vi.fn((_blob: Blob, _name: string) => true),
}));
vi.mock("../src/socialVisual", () => ({
  visualState: (slot: { visual?: unknown } | undefined) => (visual.state === "none" ? "none" : slot?.visual ? visual.state : "none"),
  loadedVisual: () => visual.blob,
  visualUrl: () => (visual.blob ? "blob:picture-1" : null),
  showPictureUrl: () => (visual.blob ? "blob:picture-1" : null),
  revokePictureUrls: () => undefined,
  loadVisual: (...args: unknown[]) => {
    visual.load(...args);
    return visual.pending ?? Promise.resolve(visual.blob);
  },
  canCopyPicture: () => visual.canCopy,
  copyPicture: (blob: Blob) => visual.copy(blob),
  downloadPicture: (blob: Blob, name: string) => visual.download(blob, name),
}));

const flush = () => vi.advanceTimersByTimeAsync(0);
const all = (node: ParentNode, sel: string) => [...node.querySelectorAll<HTMLElement>(sel)];
const q = (node: ParentNode, sel: string) => node.querySelector<HTMLElement>(sel);

type Reply = { ok: boolean; status: number; body: unknown };
const ok = (body: unknown): Reply => ({ ok: true, status: 200, body });

const LONG = Array.from({ length: 14 }, (_, i) => `Line ${i + 1} of the post`).join("\n");
const DETAIL = (text: string | null) => ({
  id: "li-1",
  kind: "video",
  platform: "linkedin",
  channel: "ae",
  title: "Diagram tool",
  tool: null,
  state: "planned",
  has_draft: text !== null,
  draft: text === null ? null : { text, topic: null, by: "auto", edited: false },
  draft_rev: "r1",
  visual: { kind: "tool", key: "k1" },
});
const VIEW = { channel_ids: {}, slots: [] };

let slotAnswer: Reply | (() => Reply) = ok(DETAIL("Hello\nsecond line"));
let currentList: AttentionOverview;
function stub() {
  const request = vi.fn(async ({ method, path }: { method: string; path: string; body?: unknown }) => {
    const key = `${method} ${path}`;
    if (key === "GET /v1/ae/attention") return ok(currentList);
    if (key === "POST /v1/ae/social/reopen") return ok({ ok: true });
    if (key === "POST /v1/ae/social/slot") return typeof slotAnswer === "function" ? slotAnswer() : slotAnswer;
    if (key === "GET /v1/ae/social") return ok(VIEW);
    if (path.startsWith("/v1/ae/attention/history")) return ok({ id: "x", first_seen: null, events: [] });
    if (path === "/v1/ae/attention/admins") return ok({ admins: [{ id: "owner@example.com", name: "owner@example.com" }] });
    return { ok: false, status: 500, body: { error: { code: "x", message: `unexpected ${key}` } } };
  });
  const context = {
    view: "fabric",
    server: { request },
    navigation: { openSession: vi.fn(), openExternal: vi.fn(), openPage: vi.fn() },
  } as unknown as ExtensionContext;
  return { request, context };
}

async function mount(list: AttentionOverview) {
  const openCard = vi.fn();
  const table = createNeedsTable({ openTab: vi.fn(), openCard, hasTab: () => true, measure: () => 1000 });
  const node = attentionSection();
  document.getElementById("root")?.appendChild(node);
  currentList = list;
  const made = stub();
  activateAttention(made.context, node, { waiting: false, table: table.hooks });
  await flush();
  return { node, openCard, ...made };
}

const POST = (fields = {}) =>
  item("social:li-1", { source: "social", kind: "post-overdue", title: "Diagram tool", url: "", slot: "li-1", visual: { kind: "tool", key: "k1" }, actions: ["dismiss"], ...fields });
const ME = { me: "owner@example.com" };
const GIT_ROW = () => item("git:pr-failing:a/b#1", { title: "Fix #1", actions: ["review", "dismiss"] });
const settle = async () => {
  for (let i = 0; i < 6; i += 1) await flush();
};
const open = async (node: HTMLElement, id = "social:li-1") => {
  q(node, `[data-id="${id}"] .nt-toggle`)!.click();
  await flush();
  await flush();
  await flush();
  return q(node, `[data-id="${id}"]`)!;
};
const button = (row: HTMLElement, focus: string) => q(row, `button[data-focus="social:li-1:${focus}"]`)!;
const names = (bar: HTMLElement) =>
  all(bar, ".attn-actions > *")
    .filter((n) => !n.hidden && !(n.classList.contains("menu-wrap") && !n.classList.contains("nt-snooze-drop")))
    .map((n) => (n.classList.contains("nt-assign") ? "assign" : n.classList.contains("nt-snooze-drop") ? "snooze" : n.dataset.focus?.split(":").pop() ?? n.className));

beforeEach(() => {
  resetTiles();
  resetAttention();
  resetFolds();
  resetAttentionSeen();
  resetSelection();
  resetHistories();
  resetAdmins();
  resetAssignedView();
  resetPosts();
  slotAnswer = ok(DETAIL("Hello\nsecond line"));
  visual.load.mockClear();
  visual.state = "ready";
  visual.pending = null;
  visual.blob = new Blob(["x"], { type: "image/png" });
  visual.canCopy = true;
  for (const fn of [actions.copyDraft, actions.openNeedsEditor, actions.openPost, actions.postPosted, actions.postSkipped, visual.copy, visual.download]) fn.mockClear();
  actions.copyDraft.mockResolvedValue("Copied");
  actions.canCopyFresh.mockReturnValue(true);
  actions.written.length = 0;
  actions.writeRefuses = false;
  actions.startFreshCopy.mockClear();
  actions.openNeedsEditor.mockReturnValue(false);
  visual.copy.mockResolvedValue(null);
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

describe("the post in the row", () => {
  it("shows the draft with its line breaks as text, above the facts, with the picture from the card's blob", async () => {
    const { node } = await mount(overview([POST()], ME));
    const row = await open(node);
    const text = q(row, ".nt-post .nt-post-text")!;
    expect(text.textContent).toBe("Hello\nsecond line");
    expect(text.querySelector("*")).toBeNull();
    expect(q(row, ".nt-picbox .nt-pic-img")!.getAttribute("src")).toBe("blob:picture-1");
    expect(q(row, ".nt-post .nt-sec-h")!.textContent).toBe("Post");
    expect(q(row, ".nt-picbox .nt-sec-h")!.textContent).toBe("Picture");
    expect(q(row, ".nt-post-meta")!.textContent).toBe("17 characters \u00b7 Draft ready");
    expect(q(row, ".nt-post-more")).toBeNull();
  });

  it("collapses a long draft and Show all expands it", async () => {
    slotAnswer = ok(DETAIL(LONG));
    const { node } = await mount(overview([POST()], ME));
    const row = await open(node);
    expect(q(row, ".nt-post-text")!.classList.contains("is-collapsed")).toBe(true);
    const more = q(row, ".nt-post-more")!;
    expect(more.textContent).toBe("Show all");
    more.click();
    expect(q(row, ".nt-post-text")!.classList.contains("is-collapsed")).toBe(false);
    expect(q(row, ".nt-post-text")!.textContent).toBe(LONG);
    expect(q(row, ".nt-post-more")!.textContent).toBe("Show less");
  });

  it("says No draft yet and keeps Open the post, with Copy text and Open on LinkedIn off", async () => {
    slotAnswer = ok(DETAIL(null));
    const { node } = await mount(overview([POST()], ME));
    const row = await open(node);
    expect(q(row, ".nt-post-note")!.textContent).toContain("No draft yet");
    expect((button(row, "copy") as HTMLButtonElement).disabled).toBe(true);
    expect((button(row, "open") as HTMLButtonElement).disabled).toBe(true);
    expect(q(row, '[data-extra="post"]')!.textContent).toBe("Open the post");
  });

  it("says in words that the post is loading, then that it failed, and the rest of the row stays", async () => {
    slotAnswer = { ok: false, status: 500, body: { error: { code: "boom", message: "the box is asleep" } } };
    const { node } = await mount(overview([POST()], ME));
    const row = await open(node);
    expect(q(row, ".nt-post-bad")!.textContent).toMatch(/could not be loaded/);
    expect(q(row, ".nt-facts")).not.toBeNull();
    expect(q(row, ".nt-hist")).not.toBeNull();
  });

  it("says Loading the post while the slot is still being read", async () => {
    let release: () => void = () => undefined;
    const gate = new Promise<void>((resolve) => (release = resolve));
    slotAnswer = () => {
      void gate;
      return ok(DETAIL("later"));
    };
    const { node, request } = await mount(overview([POST()], ME));
    request.mockImplementation(async ({ method, path }: { method: string; path: string }) => {
      if (`${method} ${path}` === "POST /v1/ae/social/slot") {
        await gate;
        return ok(DETAIL("later"));
      }
      if (`${method} ${path}` === "GET /v1/ae/attention") return ok(overview([POST()], ME));
      return ok({ id: "x", first_seen: null, events: [], channel_ids: {}, slots: [], admins: [] });
    });
    q(node, '[data-id="social:li-1"] .nt-toggle')!.click();
    await flush();
    expect(q(node, ".nt-post-note")!.textContent).toBe("Loading the post...");
    release();
    await flush();
    await flush();
    expect(q(node, ".nt-post-text")!.textContent).toBe("later");
  });
});

describe("the toolbar", () => {
  it("runs Copy text, Open on LinkedIn, Posted, Open the post, Assign, Snooze, then Skip post", async () => {
    const { node } = await mount(overview([POST()], ME));
    const row = await open(node);
    expect(names(q(row, ".nt-toolbar")!)).toEqual(["copy", "open", "posted", "post", "assign", "snooze", "skip"]);
    expect(q(row, ".nt-toolbar [data-focus$=':dismiss']")).toBeNull();
    expect(button(row, "copy").classList.contains("nt-primary")).toBe(true);
    expect(button(row, "copy").textContent).toBe("Copy text");
    expect(button(row, "open").textContent).toBe("Open on LinkedIn");
  });

  it("Copy text copies the draft as it is now, read again at the click, and says Copied", async () => {
    const { node } = await mount(overview([POST()], ME));
    const row = await open(node);
    // The draft is edited in the Social card after the row opened.
    slotAnswer = ok({ ...DETAIL("An edit made in the card"), draft_rev: "r2" });
    button(row, "copy").click();
    await settle();
    expect(actions.startFreshCopy).toHaveBeenCalledTimes(1);
    expect(actions.written).toEqual(["An edit made in the card"]);
    expect(actions.copyDraft).not.toHaveBeenCalled();
    expect(button(row, "copy").textContent).toBe("Copied");
    expect(q(row, ".nt-post-text")!.textContent).toBe("An edit made in the card");
  });

  it("Copy text starts the clipboard write inside the click, before the read finishes", async () => {
    const { node, request } = await mount(overview([POST()], ME));
    const row = await open(node);
    let release: () => void = () => undefined;
    const gate = new Promise<void>((resolve) => (release = resolve));
    request.mockImplementation(async ({ method, path }: { method: string; path: string }) => {
      if (`${method} ${path}` === "POST /v1/ae/social/slot") {
        await gate;
        return ok({ ...DETAIL("Late edit"), draft_rev: "r3" });
      }
      return ok({ id: "x", first_seen: null, events: [], channel_ids: {}, slots: [], admins: [] });
    });
    button(row, "copy").click();
    expect(actions.startFreshCopy).toHaveBeenCalledTimes(1);
    expect(actions.written).toEqual([]);
    release();
    await settle();
    expect(actions.written).toEqual(["Late edit"]);
  });

  it("without a promise clipboard it copies the held text and says how old it is", async () => {
    actions.canCopyFresh.mockReturnValue(false);
    const { node } = await mount(overview([POST()], ME));
    const row = await open(node);
    button(row, "copy").click();
    await settle();
    expect(actions.copyDraft).toHaveBeenCalledWith("Hello\nsecond line");
    expect(button(row, "copy").textContent).toBe("Copied");
    expect(q(row, ".flash")!.textContent).toMatch(/^Copied the draft as of \d{1,2}:\d{2}.*; reopen the row if you just edited it\.$/);
  });

  it("when the fresh read fails the write still takes the held draft inside the click, and the line says so", async () => {
    const { node } = await mount(overview([POST()], ME));
    const row = await open(node);
    slotAnswer = { ok: false, status: 500, body: { error: { code: "boom", message: "the box is asleep" } } };
    button(row, "copy").click();
    await settle();
    // The clipboard promise itself fell back: the held text was written, and nothing ran outside the click.
    expect(actions.written).toEqual(["Hello\nsecond line"]);
    expect(actions.copyDraft).not.toHaveBeenCalled();
    expect(button(row, "copy").textContent).toBe("Copied");
    expect(q(row, ".flash")!.textContent).toMatch(/^Copied the draft as of .*; reopen the row if you just edited it\.$/);
  });

  it("keeps the last good draft, marked stale with its time, after a failed read, and the next click still copies it", async () => {
    const { node } = await mount(overview([POST()], ME));
    const row = await open(node);
    slotAnswer = { ok: false, status: 500, body: { error: { code: "boom", message: "the box is asleep" } } };
    button(row, "copy").click();
    await settle();
    expect(q(row, ".nt-post-text")!.textContent).toBe("Hello\nsecond line");
    expect(q(row, ".nt-post-stale")!.textContent).toMatch(/^Could not read the newest draft; showing the one from \d{1,2}:\d{2}.*\.$/);
    actions.written.length = 0;
    button(row, "copy").click();
    await settle();
    expect(actions.written).toEqual(["Hello\nsecond line"]);
    expect(q(row, ".nt-post-note.meta:not(.nt-post-stale)")?.textContent ?? "").not.toMatch(/No draft/);
    // The server is back: the stale note goes with the fresh read.
    slotAnswer = ok({ ...DETAIL("Back again"), draft_rev: "r5" });
    button(row, "copy").click();
    await settle();
    expect(actions.written.at(-1)).toBe("Back again");
    expect(q(row, ".nt-post-stale")).toBeNull();
  });

  it("Copy text says Copy failed, and what to do, when the clipboard refuses", async () => {
    actions.writeRefuses = true;
    actions.copyDraft.mockResolvedValue("Copy failed");
    const { node } = await mount(overview([POST()], ME));
    const row = await open(node);
    button(row, "copy").click();
    await settle();
    expect(button(row, "copy").textContent).toBe("Copy failed");
    expect(q(row, ".flash")!.textContent).toMatch(/clipboard refused/);
  });

  it("Open on LinkedIn opens with the draft as it is now, after the copy has started with the read pending", async () => {
    const { node } = await mount(overview([POST()], ME));
    const row = await open(node);
    slotAnswer = ok({ ...DETAIL("Edited after the row opened"), draft_rev: "r2" });
    button(row, "open").click();
    await settle();
    expect(actions.startFreshCopy).toHaveBeenCalledTimes(1);
    expect(actions.openPost).toHaveBeenCalledTimes(1);
    const args = actions.openPost.mock.calls[0] as unknown as [unknown, { id: string }, unknown, string, Promise<boolean>];
    expect(args[1].id).toBe("li-1");
    expect(args[3]).toBe("Edited after the row opened");
    expect(args[4]).toBeInstanceOf(Promise);
    expect(button(row, "open").textContent).toBe("Copied, opened");
  });

  it("Open on LinkedIn without a promise clipboard opens with the held draft and says how old it is", async () => {
    actions.canCopyFresh.mockReturnValue(false);
    const { node } = await mount(overview([POST()], ME));
    const row = await open(node);
    actions.openPost.mockResolvedValueOnce({});
    button(row, "open").click();
    await settle();
    const args = actions.openPost.mock.calls[0] as unknown as unknown[];
    expect(args[3]).toBe("Hello\nsecond line");
    expect(args[4]).toBeUndefined();
    expect(q(row, ".flash")!.textContent).toMatch(/^Used the draft as of .*; reopen the row if you just edited it\.$/);
  });

  it("Open on LinkedIn, when the fresh read fails, opens with the held draft that the write has taken, and says so", async () => {
    const { node } = await mount(overview([POST()], ME));
    const row = await open(node);
    slotAnswer = { ok: false, status: 500, body: { error: { code: "boom", message: "the box is asleep" } } };
    actions.openPost.mockResolvedValueOnce({});
    button(row, "open").click();
    await settle();
    const args = actions.openPost.mock.calls[0] as unknown as unknown[];
    expect(args[3]).toBe("Hello\nsecond line");
    expect(actions.written).toEqual(["Hello\nsecond line"]);
    // The copy it waits on really did succeed with that text.
    await expect(args[4] as Promise<boolean>).resolves.toBe(true);
    expect(q(row, ".flash")!.textContent).toMatch(/^Copied and opened the draft as of .*; reopen the row if you just edited it\.$/);
  });

  it("Open on LinkedIn waits, with a note, while the picture the share sheet needs is loading", async () => {
    actions.openNeedsEditor.mockReturnValue(true);
    const { node } = await mount(overview([POST()], ME));
    const row = await open(node);
    button(row, "open").click();
    await flush();
    expect(actions.openPost).not.toHaveBeenCalled();
    expect(q(row, ".flash")!.textContent).toMatch(/still loading/);
  });

  it("Posted calls the card's posted call, then the row says Marked as posted", async () => {
    const { node, request } = await mount(overview([POST()], ME));
    const row = await open(node);
    const before = request.mock.calls.filter(([c]) => c.path === "/v1/ae/attention").length;
    button(row, "posted").click();
    await settle();
    expect(actions.postPosted).toHaveBeenCalledWith(expect.anything(), "li-1");
    expect(request.mock.calls.filter(([c]) => c.path === "/v1/ae/attention").length).toBeGreaterThan(before);
    expect(q(node, ".attn-toast")!.textContent).toContain("Marked as posted");
  });

  it("Posted says what failed and that nothing changed", async () => {
    actions.postPosted.mockRejectedValueOnce(new Error("offline"));
    const { node } = await mount(overview([POST()], ME));
    const row = await open(node);
    button(row, "posted").click();
    await settle();
    expect(node.textContent).toMatch(/Could not mark it as posted .*Nothing changed/);
  });

  it("Skip post calls the card's skip and the row says so", async () => {
    const { node } = await mount(overview([POST()], ME));
    const row = await open(node);
    button(row, "skip").click();
    await settle();
    expect(actions.postSkipped).toHaveBeenCalledWith(expect.anything(), "li-1");
    expect(q(node, ".attn-toast")!.textContent).toContain("Skipped this post");
  });

  it("a closed Social row shows Copy text and Snooze, never Skip post, Posted or Dismiss", async () => {
    const { node } = await mount(overview([POST()], ME));
    const row = q(node, '[data-id="social:li-1"]')!;
    expect(q(row, "[data-focus$=':skip']")).toBeNull();
    expect(q(row, "[data-focus$=':posted']")).toBeNull();
    expect(q(row, "[data-focus$=':dismiss']")).toBeNull();
    expect(q(row, ".attn-actions [data-focus$=':copy']")!.textContent).toBe("Copy text");
    expect(q(row, ".nt-snooze-menu")).not.toBeNull();
    expect(row.textContent).not.toMatch(/Skip post|Posted/);
  });

  it("the closed row's Copy text reads the draft and copies it", async () => {
    const { node } = await mount(overview([POST()], ME));
    const row = q(node, '[data-id="social:li-1"]')!;
    q(row, ".attn-actions [data-focus$=':copy']")!.click();
    await settle();
    expect(actions.copyDraft).toHaveBeenCalledWith("Hello\nsecond line");
    expect(q(row, ".attn-actions [data-focus$=':copy']")!.textContent).toBe("Copied");
  });

  it("a Social panel is the only panel that is a container", async () => {
    const both = await mount(overview([POST(), GIT_ROW()], ME));
    expect(q(await open(both.node), ".nt-panel")!.classList.contains("nt-panel-social")).toBe(true);
    const git = await open(both.node, "git:pr-failing:a/b#1");
    expect(q(git, ".nt-panel")!.classList.contains("nt-panel-social")).toBe(false);
  });
});

describe("the picture buttons", () => {
  it("Copy picture hands the loaded blob to the shared copy and says Picture copied", async () => {
    const { node } = await mount(overview([POST()], ME));
    const row = await open(node);
    button(row, "copy-picture").click();
    await flush();
    expect(visual.copy).toHaveBeenCalledWith(visual.blob);
    expect(button(row, "copy-picture").textContent).toBe("Picture copied");
  });

  it("Copy picture names the exact failure and points to Download picture", async () => {
    visual.copy.mockResolvedValue("the browser refused");
    const { node } = await mount(overview([POST()], ME));
    const row = await open(node);
    button(row, "copy-picture").click();
    await flush();
    expect(button(row, "copy-picture").textContent).toBe("Copy failed");
    expect(q(row, ".flash")!.textContent).toBe("Could not copy the picture (the browser refused). Use Download instead.");
  });

  it("without an image clipboard only Download picture is offered, and it saves under the post's name", async () => {
    visual.canCopy = false;
    const { node } = await mount(overview([POST()], ME));
    const row = await open(node);
    expect(button(row, "copy-picture").hidden).toBe(true);
    expect(button(row, "download-picture").hidden).toBe(false);
    button(row, "download-picture").click();
    expect(visual.download).toHaveBeenCalledWith(visual.blob, "Diagram tool");
  });

  it("shows both buttons from the first draw, disabled with a loading note, and enables them in place when the picture lands", async () => {
    visual.state = "loading";
    let land: (blob: Blob) => void = () => undefined;
    visual.pending = new Promise<Blob | null>((resolve) => {
      land = resolve;
    });
    const { node } = await mount(overview([POST()], ME));
    const row = await open(node);
    const acts = q(row, ".nt-picacts")!;
    const before = [...acts.children];
    const frameBefore = q(row, ".nt-pic") as HTMLElement;
    // The frame is a fixed-height stage (style.css) while it loads and once ready: one height rule for both states,
    // the cap plus padding and borders, so the block neither grows nor shrinks when the blob lands.
    const stage = css.match(/((?:\.nt-picbox \.nt-pic\[data-state="(?:loading|ready|failed)"\],?\s*){3})\{([^}]*)\}/);
    expect(stage).not.toBeNull();
    expect(stage![2]).toMatch(/height: calc\(var\(--nt-pic-cap, 340px\) \+ 18px\)/);
    expect(stage![2]).toContain("box-sizing: border-box");
    expect(stage![1]).toContain('[data-state="loading"]');
    expect(stage![1]).toContain('[data-state="ready"]');
    expect(frameBefore.dataset.state).toBe("loading");
    expect(button(row, "copy-picture").hidden).toBe(false);
    expect(button(row, "download-picture").hidden).toBe(false);
    expect((button(row, "copy-picture") as HTMLButtonElement).disabled).toBe(true);
    expect((button(row, "download-picture") as HTMLButtonElement).disabled).toBe(true);
    expect(button(row, "copy-picture").textContent).toBe("Copy picture");
    expect(q(acts, ".nt-picsize")!.textContent).toBe("Loading picture");
    visual.state = "ready";
    visual.blob = new Blob(["x"], { type: "image/png" });
    land(visual.blob);
    await settle();
    expect((button(row, "copy-picture") as HTMLButtonElement).disabled).toBe(false);
    expect((button(row, "download-picture") as HTMLButtonElement).disabled).toBe(false);
    expect(q(acts, ".nt-picsize")!.textContent).not.toBe("Loading picture");
    // The same nodes, in the same places: nothing was added or moved.
    const after = [...acts.children];
    expect(after).toHaveLength(before.length);
    after.forEach((node, i) => expect(node).toBe(before[i]));
    // The same frame node, now ready: the stage rule still applies and no inline height overrides it.
    expect(q(row, ".nt-pic")).toBe(frameBefore);
    expect(frameBefore.dataset.state).toBe("ready");
    expect(frameBefore.style.height).toBe("");
    expect(frameBefore.style.minHeight).toBe("");
  });

  it("offers neither picture button when there is no picture", async () => {
    visual.state = "none";
    visual.blob = null;
    const { node } = await mount(overview([POST({ visual: null })], ME));
    const row = await open(node);
    expect(button(row, "copy-picture").hidden).toBe(true);
    expect(button(row, "download-picture").hidden).toBe(true);
  });
});

describe("the bulk bar and the Dismissed fold", () => {
  const GIT = () => item("git:pr-failing:a/b#1", { title: "Fix #1", actions: ["review", "dismiss"] });
  const pick = async (node: HTMLElement, id: string) => {
    q(node, `input[data-pick="${id}"]`)!.click();
    await flush();
  };
  const dismissButton = (node: HTMLElement) => all(q(node, ".nt-bulk")!, "button").find((b) => /^Dismiss/.test(b.textContent ?? ""))!;

  it("takes the toolbar's place in the header while rows are picked, and gives it back on Clear (2026-11-19)", async () => {
    const { node } = await mount(overview([POST(), GIT()], ME));
    const top = q(node, ".attn-top")!;
    const bar = q(node, ".nt-bulk")!;
    const tools = q(node, ".attn-tools")!;
    // The bar sits in the header with the tools, never above the list, so the list has nothing to shift for.
    expect(bar.parentElement).toBe(top);
    expect(tools.parentElement).toBe(top);
    expect(bar.hidden).toBe(true);
    expect(css).toMatch(/\.attn-top:has\(> \.nt-bulk\[data-count\]:not\(\[data-count="0"\], \[hidden\]\)\) > \.attn-tools \{\s*display: none;/);
    expect(css).not.toMatch(/\.nt-bulk \{[^}]*position: sticky/);
    // The line's own checkbox is always on screen (a row's can sit in a folded line).
    const box = q(node, ".nt-source input[data-pick-group]") as HTMLInputElement;
    box.focus();
    box.click();
    await flush();
    expect(bar.hidden).toBe(false);
    expect(bar.dataset.count).toBe("1");
    expect(bar.parentElement).toBe(top);
    // Focus stays on the checkbox that was ticked.
    expect(document.activeElement).toBe(box);
    all(bar, "button").find((b) => b.textContent === "Clear")!.click();
    await flush();
    expect(bar.hidden).toBe(true);
    expect(bar.dataset.count).toBe("0");
    // Clear returns the focus to the toolbar's first control.
    expect(tools.contains(document.activeElement)).toBe(true);
  });

  it("Dismiss leaves Social posts out and the result line says where their buttons are", async () => {
    const { node, request } = await mount(overview([POST(), GIT()], ME));
    await pick(node, "social:li-1");
    await pick(node, "git:pr-failing:a/b#1");
    request.mockImplementation(async ({ path, body }: { method: string; path: string; body?: unknown }) => {
      if (path === "/v1/ae/attention/bulk") {
        const ids = (body as { ids: string[] }).ids;
        return ok({ results: ids.map((id) => ({ id, ok: true, status: 200 })), succeeded: ids.length, failed: 0 });
      }
      return ok(overview([POST()], ME));
    });
    dismissButton(node).click();
    await flush();
    await flush();
    const sent = request.mock.calls.filter(([c]) => c.path === "/v1/ae/attention/bulk").flatMap(([c]) => (c.body as { ids: string[] }).ids);
    expect(sent).toEqual(["git:pr-failing:a/b#1"]);
    expect(q(node, ".nt-bulk-line")!.textContent).toContain(SOCIAL_BULK_NOTE);
  });

  it("Dismiss with only Social posts selected sends nothing and says so", async () => {
    const { node, request } = await mount(overview([POST()], ME));
    await pick(node, "social:li-1");
    dismissButton(node).click();
    await flush();
    expect(request.mock.calls.some(([c]) => c.path === "/v1/ae/attention/bulk")).toBe(false);
    expect(q(node, ".nt-bulk-line")!.textContent).toBe(SOCIAL_BULK_NOTE);
  });

  it("Snooze still applies to a selected Social post", async () => {
    const { node } = await mount(overview([POST()], ME));
    await pick(node, "social:li-1");
    expect(all(q(node, ".nt-bulk")!, "button").some((b) => /^Snooze/.test(b.textContent ?? ""))).toBe(true);
  });

  it("a Social row in the Dismissed fold offers Skip post beside Restore, and it skips", async () => {
    const hidden = POST({ dismissed: { reason: "", at: "2026-11-10T00:00:00Z", expires_at: "2026-11-20T00:00:00Z", by: "owner@example.com" } as never });
    const { node } = await mount(overview([], { ...ME, dismissed: [hidden] }));
    q(node, ".nt-dlink")!.click();
    await flush();
    const row = q(node, '.attn-dismissed-row[data-id="social:li-1"]')!;
    expect(q(row, ".attn-restore:not(.attn-skip)")!.textContent).toBe("Restore");
    const skip = q(row, ".attn-skip")!;
    expect(skip.textContent).toBe("Skip post");
    skip.click();
    await settle();
    expect(actions.postSkipped).toHaveBeenCalledWith(expect.anything(), "li-1");
    expect(q(node, ".attn-toast")!.textContent).toContain("Skipped this post");
  });

  it("shows a quiet N dismissed link after the counts, closed at first, and no old Dismissed box (2026-11-19)", async () => {
    const hidden = POST({ dismissed: { reason: "", at: "2026-11-10T00:00:00Z", expires_at: "2026-11-20T00:00:00Z", by: "owner@example.com" } as never });
    const { node } = await mount(overview([GIT()], { ...ME, dismissed: [hidden] }));
    const link = q(node, ".attn-top > .attn-counts + .nt-dlink")!;
    expect(link.tagName).toBe("BUTTON");
    expect(link.textContent).toBe("1 dismissed");
    expect(link.getAttribute("aria-label")).toBe("Show 1 dismissed item");
    expect(link.getAttribute("aria-expanded")).toBe("false");
    expect(q(node, ".attn-counts")!.textContent).not.toContain("dismissed");
    expect(q(node, ".attn-dismissed")).toBeNull();
    expect(q(node, ".nt-dismissed")!.hidden).toBe(true);
    link.click();
    await flush();
    expect(link.getAttribute("aria-expanded")).toBe("true");
    expect(link.getAttribute("aria-label")).toBe("Hide 1 dismissed item");
    expect(q(node, ".nt-dismissed")!.hidden).toBe(false);
    expect(q(node, ".nt-dismissed .attn-restore")).not.toBeNull();
    // Hide closes it and hands the focus back to the link.
    q(node, ".nt-dhide")!.click();
    await flush();
    expect(q(node, ".nt-dismissed")!.hidden).toBe(true);
    expect(link.getAttribute("aria-expanded")).toBe("false");
    expect(document.activeElement).toBe(link);
  });

  it("restoring the last dismissed item with the list open resets the open state and focuses the card heading", async () => {
    const hidden = POST({ dismissed: { reason: "", at: "2026-11-10T00:00:00Z", expires_at: "2026-11-20T00:00:00Z", by: "owner@example.com" } as never });
    const { node, request } = await mount(overview([GIT()], { ...ME, dismissed: [hidden] }));
    q(node, ".nt-dlink")!.click();
    await flush();
    request.mockImplementation(async ({ method, path }: { method: string; path: string }) => {
      if (`${method} ${path}` === "POST /v1/ae/attention/restore") return ok({ id: "social:li-1", restored: true });
      return ok(overview([GIT()], ME));
    });
    const restore = q(node, ".nt-dismissed .attn-restore")!;
    restore.focus();
    restore.click();
    await flush();
    await flush();
    expect(q(node, ".nt-dlink")).toBeNull();
    expect(q(node, ".nt-dismissed")).toBeNull();
    expect(document.activeElement).toBe(q(node, ".attn-h"));
  });

  it("builds the link's name in one helper and scopes the list's id to its own table", async () => {
    const hidden = POST({ dismissed: { reason: "", at: "2026-11-10T00:00:00Z", expires_at: "2026-11-20T00:00:00Z", by: "owner@example.com" } as never });
    const one = await mount(overview([GIT()], { ...ME, dismissed: [hidden] }));
    const two = await mount(overview([GIT()], { ...ME, dismissed: [hidden] }));
    const a = q(one.node, ".nt-dismissed")!.id;
    const b = q(two.node, ".nt-dismissed")!.id;
    expect(a).toMatch(/^nt-dismissed-\d+$/);
    expect(a).not.toBe(b);
    expect(q(one.node, ".nt-dlink")!.getAttribute("aria-controls")).toBe(a);
    expect(q(one.node, ".nt-dhide")!.getAttribute("aria-label")).toBe("Hide 1 dismissed item");
    expect(css).toMatch(/\.nt-dlink \{\s*flex: none;/);
  });

  it("styles a selected area line like a source line (the right-edge accent only) and sets the bar's button height in one rule", () => {
    const rule = css.match(/\.nt-area\.is-selected > \.nt-grp-head\.has-opens \{([^}]*)\}/)![1]!;
    expect(rule).toContain("inset -3px 0 0 var(--accent)");
    expect(rule).not.toContain("inset 3px 0 0");
    expect(css.match(/\.attn \.nt-bulk \.nt-bulk-btn,\s*\.attn \.nt-bulk \.nt-bulk-choice \{[^}]*height: 28px/g)).toHaveLength(1);
    expect(css).not.toContain("whatever the shared button rule says");
  });

  it("shows no dismissed link and no dismissed list when nothing is dismissed", async () => {
    const { node } = await mount(overview([GIT()], ME));
    expect(q(node, ".nt-dlink")).toBeNull();
    expect(q(node, ".nt-dismissed")).toBeNull();
  });

  it("a Git row in the Dismissed fold has no Skip post", async () => {
    const { node } = await mount(overview([], { ...ME, dismissed: [GIT()] }));
    q(node, ".nt-dlink")!.click();
    await flush();
    expect(q(node, ".attn-skip")).toBeNull();
  });
});

describe("the picture column", () => {
  it("holds Copy picture and Download right under the picture, not in the toolbar", async () => {
    const { node } = await mount(overview([POST()], ME));
    const row = await open(node);
    const acts = q(row, ".nt-b3-pic .nt-picacts")!;
    expect(all(acts, "button").map((b) => b.textContent)).toEqual(["Copy picture", "Download"]);
    expect(q(row, ".nt-toolbar [data-focus$=':copy-picture']")).toBeNull();
    expect(q(row, ".nt-toolbar [data-focus$=':download-picture']")).toBeNull();
    expect(acts.previousElementSibling!.classList.contains("nt-pic")).toBe(true);
  });

  it("says the picture's pixel size once it has decoded", async () => {
    const { node } = await mount(overview([POST()], ME));
    const row = await open(node);
    const img = q(row, ".nt-pic-img") as HTMLImageElement;
    Object.defineProperty(img, "naturalWidth", { configurable: true, value: 1200 });
    Object.defineProperty(img, "naturalHeight", { configurable: true, value: 628 });
    img.dispatchEvent(new Event("load"));
    expect(q(row, ".nt-picsize")!.textContent).toBe("1200 \u00d7 628");
  });

  it("the stylesheet caps the picture at the shared height, fills the width and keeps contain", () => {
    expect(css).toMatch(/\.nt-b3 \{[^}]*--nt-pic-cap: 340px/);
    const rule = css.match(/\.nt-picbox \.nt-pic \.nt-pic-img \{([^}]*)\}/)![1]!;
    expect(rule).toMatch(/width: 100%/);
    expect(rule).toMatch(/max-height: var\(--nt-pic-cap, 340px\)/);
    expect(rule).toMatch(/object-fit: contain/);
    expect(rule).not.toMatch(/object-fit: (cover|fill)/);
    // The collapsed text fills the column (its content does not set the row's height) and is never under ten lines.
    expect(css).toMatch(/\.nt-post-text\.is-collapsed \{[^}]*flex: 1 1 0;[^}]*min-height: calc\(1\.5em \* 10/);
  });
});

describe("the thirds and their fallback", () => {
  it("three equal columns, with the evidence spanning them", () => {
    expect(css).toMatch(/\.nt-b3 \{[^}]*grid-template-columns: repeat\(3, minmax\(0, 1fr\)\)/);
    expect(css).toMatch(/\.nt-b3-ev \{[^}]*grid-column: 1 \/ -1/);
  });

  it("under 1400 px of panel width it is two columns with the facts and history in a full-width row", () => {
    const query = css.match(/@container ntpanel \(max-width: 1399px\) \{([\s\S]*?)\n\}\n/)!;
    expect(query).not.toBeNull();
    expect(query[1]).toMatch(/\.nt-b3 \{[^}]*repeat\(2, minmax\(0, 1fr\)\)/);
    expect(query[1]).toMatch(/\.nt-b3-side \{[^}]*grid-column: 1 \/ -1/);
    expect(css).toMatch(/\.nt-panel\.nt-panel-social \{\s*container: ntpanel \/ inline-size/);
    expect(css).not.toMatch(/\.nt-panel \{\s*container:/);
  });

  it("the phone sheet keeps one column with the same pieces", async () => {
    // The sheet is not a grid of columns: its main body is one flex column (style.css, .nt-sheet .nt-panel-main).
    expect(css).toMatch(/\.nt-sheet \.nt-picbox/);
    expect(css).not.toMatch(/\.nt-sheet \.nt-b3/);
  });
});

describe("Skip post and Posted are guarded", () => {
  it("a stale slot never acts: it says the post changed and reloads", async () => {
    const { node, request } = await mount(overview([POST()], ME));
    const row = await open(node);
    slotAnswer = ok({ ...DETAIL("Hello"), state: "posted" });
    const before = request.mock.calls.filter(([c]) => c.path === "/v1/ae/attention").length;
    button(row, "skip").click();
    await settle();
    expect(actions.postSkipped).not.toHaveBeenCalled();
    expect(node.textContent).toMatch(/This post changed \(it is now posted\), so nothing was done/);
    expect(request.mock.calls.filter(([c]) => c.path === "/v1/ae/attention").length).toBeGreaterThan(before);
  });

  it("a slot the server no longer has does not act either", async () => {
    const { node } = await mount(overview([POST()], ME));
    const row = await open(node);
    slotAnswer = { ok: false, status: 404, body: { error: { code: "not_found", message: "no such post" } } };
    button(row, "posted").click();
    await settle();
    expect(actions.postPosted).not.toHaveBeenCalled();
    expect(node.textContent).toMatch(/This post changed/);
  });

  it("a read that fails says nothing changed and does not act", async () => {
    const { node } = await mount(overview([POST()], ME));
    const row = await open(node);
    slotAnswer = { ok: false, status: 500, body: { error: { code: "boom", message: "the box is asleep" } } };
    button(row, "skip").click();
    await settle();
    expect(actions.postSkipped).not.toHaveBeenCalled();
    expect(node.textContent).toMatch(/Could not check the post first .*Nothing changed/);
  });

  it("Undo reopens the post, for ten seconds, and the line is visible after the row has left", async () => {
    const { node, request } = await mount(overview([POST()], ME));
    const row = await open(node);
    currentList = overview([], ME);
    button(row, "skip").click();
    await settle();
    expect(q(node, '[data-id="social:li-1"]')).toBeNull();
    const line = q(node, ".attn-toast .attn-undo")!;
    expect(line.textContent).toContain("Skipped this post: Diagram tool");
    await vi.advanceTimersByTimeAsync(9_900);
    expect(q(node, ".attn-toast .attn-undo")).not.toBeNull();
    currentList = overview([POST()], ME);
    q(line, "button")!.click();
    await settle();
    const reopened = request.mock.calls.filter(([c]) => c.path === "/v1/ae/social/reopen");
    expect(reopened).toHaveLength(1);
    expect((reopened[0]![0] as { body: unknown }).body).toEqual({ id: "li-1" });
    expect(q(node, '[data-id="social:li-1"]')).not.toBeNull();
  });

  it("the Undo line goes after ten seconds", async () => {
    const { node } = await mount(overview([POST()], ME));
    const row = await open(node);
    button(row, "posted").click();
    await settle();
    expect(q(node, ".attn-toast .attn-undo")).not.toBeNull();
    await vi.advanceTimersByTimeAsync(10_100);
    expect(q(node, ".attn-toast .attn-undo")).toBeNull();
  });

  it("Posted then Skip quickly sends one call", async () => {
    const { node } = await mount(overview([POST()], ME));
    const row = await open(node);
    button(row, "posted").click();
    expect((button(row, "skip") as HTMLButtonElement).disabled).toBe(true);
    button(row, "skip").click();
    await settle();
    expect(actions.postPosted).toHaveBeenCalledTimes(1);
    expect(actions.postSkipped).not.toHaveBeenCalled();
  });
});

describe("the post is read again", () => {
  it("copies the new draft after an edit in the Social card, once the row is opened again", async () => {
    const { node } = await mount(overview([POST()], ME));
    let row = await open(node);
    button(row, "copy").click();
    await settle();
    expect(actions.written.at(-1)).toBe("Hello\nsecond line");
    // The draft is changed in the card (a new revision), then the row is closed and opened.
    slotAnswer = ok({ ...DETAIL("A new draft from the card"), draft_rev: "r2" });
    q(node, '[data-id="social:li-1"] .nt-toggle')!.click();
    await settle();
    row = await open(node);
    button(row, "copy").click();
    await settle();
    expect(actions.written.at(-1)).toBe("A new draft from the card");
  });

  it("keeps the copy it has on show while the fresh one loads", async () => {
    const { node, request } = await mount(overview([POST()], ME));
    let row = await open(node);
    expect(q(row, ".nt-post-text")!.textContent).toBe("Hello\nsecond line");
    q(node, '[data-id="social:li-1"] .nt-toggle')!.click();
    await settle();
    let release: () => void = () => undefined;
    const gate = new Promise<void>((resolve) => (release = resolve));
    request.mockImplementation(async ({ method, path }: { method: string; path: string }) => {
      if (`${method} ${path}` === "POST /v1/ae/social/slot") {
        await gate;
        return ok({ ...DETAIL("Fresh text"), draft_rev: "r9" });
      }
      if (`${method} ${path}` === "GET /v1/ae/attention") return ok(currentList);
      return ok({ id: "x", first_seen: null, events: [], channel_ids: {}, slots: [], admins: [] });
    });
    q(node, '[data-id="social:li-1"] .nt-toggle')!.click();
    await flush();
    row = q(node, '[data-id="social:li-1"]')!;
    expect(q(row, ".nt-post-text")!.textContent).toBe("Hello\nsecond line");
    release();
    await settle();
    expect(q(row, ".nt-post-text")!.textContent).toBe("Fresh text");
  });

  it("drops the held post after Posted", async () => {
    const { node, request } = await mount(overview([POST()], ME));
    const row = await open(node);
    button(row, "posted").click();
    await settle();
    const slotReads = request.mock.calls.filter(([c]) => c.path === "/v1/ae/social/slot").length;
    expect(slotReads).toBeGreaterThanOrEqual(2);
    const { postOf } = await import("../src/needsPost");
    expect(postOf("li-1").status).not.toBe("ready");
  });

  it("forgets every post when the Admin Center page is left", async () => {
    const { node } = await mount(overview([POST()], ME));
    await open(node);
    const { postOf } = await import("../src/needsPost");
    expect(postOf("li-1").status).toBe("ready");
    deactivateAttention();
    expect(postOf("li-1").status).toBe("loading");
  });
});

describe("a picture whose identity is only on the slot's detail", () => {
  it("loads it for the detail's slot when the row's item has none", async () => {
    visual.state = "loading";
    const { node } = await mount(overview([POST({ visual: null, visual_note: undefined })], ME));
    await open(node);
    expect(visual.load).toHaveBeenCalled();
    const slot = visual.load.mock.calls.at(-1)![1] as { id: string; visual: unknown };
    expect(slot.id).toBe("li-1");
    expect(slot.visual).toEqual({ kind: "tool", key: "k1" });
  });

  it("shows the picture once it has loaded", async () => {
    const { node } = await mount(overview([POST({ visual: null })], ME));
    const row = await open(node);
    expect(q(row, ".nt-picbox .nt-pic-img")).not.toBeNull();
  });
});

describe("how often the row reads its post", () => {
  const slotReads = (request: ReturnType<typeof stub>["request"]) => request.mock.calls.filter(([c]) => c.path === "/v1/ae/social/slot").length;
  const viewReads = (request: ReturnType<typeof stub>["request"]) => request.mock.calls.filter(([c]) => c.path === "/v1/ae/social" && c.method === "GET").length;

  it("makes no new request on three polls with an unchanged item", async () => {
    const { node, request } = await mount(overview([POST()], ME));
    await open(node);
    const slots = slotReads(request);
    expect(slots).toBe(1);
    const before = q(node, ".nt-post");
    for (let i = 0; i < 3; i += 1) {
      await vi.advanceTimersByTimeAsync(20_000);
      setAttention(overview([POST({ fingerprint: "fp-social:li-1" })], { ...ME, read_at: new Date(Date.now()).toISOString() }));
      await settle();
    }
    // The row really was drawn again (a new post block), and still sent nothing.
    expect(q(node, ".nt-post")).not.toBe(before);
    expect(q(node, ".nt-post-text")!.textContent).toBe("Hello\nsecond line");
    expect(slotReads(request)).toBe(slots);
    expect(viewReads(request)).toBe(0);
  });

  it("reads again when the listed item's fingerprint changes", async () => {
    const { node, request } = await mount(overview([POST({ fingerprint: "a" })], ME));
    await open(node);
    setAttention(overview([POST({ fingerprint: "b" })], ME));
    await settle();
    expect(slotReads(request)).toBe(2);
  });

  it("reads again on a redraw once the read is two minutes old", async () => {
    const { node, request } = await mount(overview([POST({ fingerprint: "a" })], ME));
    await open(node);
    await vi.advanceTimersByTimeAsync(121_000);
    setAttention(overview([POST({ fingerprint: "a" })], { ...ME, read_at: new Date(Date.now()).toISOString() }));
    await settle();
    expect(slotReads(request)).toBe(2);
  });

  it("reads again each time the row opens (a closed row marks its read old)", async () => {
    const { node, request } = await mount(overview([POST()], ME));
    await open(node);
    q(node, '[data-id="social:li-1"] .nt-toggle')!.click();
    await settle();
    await open(node);
    expect(slotReads(request)).toBe(2);
  });

  it("reads the card's full view only for a post that is not on LinkedIn", async () => {
    slotAnswer = ok({ ...DETAIL("A community post"), platform: "yt_community" });
    const { node, request } = await mount(overview([POST()], ME));
    await open(node);
    expect(viewReads(request)).toBe(1);
  });

  it("Open on LinkedIn works with no view read at all", async () => {
    const { node } = await mount(overview([POST()], ME));
    const row = await open(node);
    button(row, "open").click();
    await settle();
    expect(actions.openPost).toHaveBeenCalledTimes(1);
    expect((actions.openPost.mock.calls[0] as unknown as unknown[])[2]).toBeNull();
  });
});

describe("plan buttons are not kept after a redraw", () => {
  it("keeps only the live row's buttons however many times it redraws", async () => {
    const { node } = await mount(overview([POST()], ME));
    await open(node);
    await settle();
    const live = planButtonCount("li-1");
    expect(live).toBeGreaterThan(0);
    const first = q(node, ".nt-post");
    for (let i = 0; i < 6; i += 1) {
      setAttention(overview([POST()], { ...ME, read_at: new Date(Date.now() + i * 1000).toISOString() }));
      await settle();
    }
    expect(q(node, ".nt-post")).not.toBe(first);
    expect(planButtonCount("li-1")).toBe(live);
  });
});

describe("a stale post's line goes to the page's message area", () => {
  it("shows 'This post changed' even when the reload drops the row", async () => {
    const { node } = await mount(overview([POST()], ME));
    const row = await open(node);
    slotAnswer = ok({ ...DETAIL("Hello"), state: "skipped" });
    currentList = overview([], ME);
    button(row, "posted").click();
    await settle();
    expect(q(node, '[data-id="social:li-1"]')).toBeNull();
    expect(q(node, ".attn-toast")!.textContent).toMatch(/This post changed \(it is now skipped\)/);
    expect(actions.postPosted).not.toHaveBeenCalled();
  });

  it("shows a failed read there too", async () => {
    const { node } = await mount(overview([POST()], ME));
    const row = await open(node);
    slotAnswer = { ok: false, status: 500, body: { error: { code: "boom", message: "the box is asleep" } } };
    currentList = overview([], ME);
    button(row, "skip").click();
    await settle();
    expect(q(node, ".attn-toast")!.textContent).toMatch(/Could not check the post first .*Nothing changed/);
  });
});

describe("a forced read waits for no older one", () => {
  it("starts a new read even while an older one is in flight, and the newer result wins when both land", async () => {
    const { node, request } = await mount(overview([POST()], ME));
    await open(node);
    q(node, '[data-id="social:li-1"] .nt-toggle')!.click();
    await settle();
    const gates: Array<() => void> = [];
    let reads = 0;
    request.mockImplementation(async ({ method, path }: { method: string; path: string }) => {
      if (`${method} ${path}` === "POST /v1/ae/social/slot") {
        reads += 1;
        const mine = reads;
        await new Promise<void>((resolve) => gates.push(resolve));
        return ok({ ...DETAIL(mine === 1 ? "Old read" : "New read"), draft_rev: `r${mine}` });
      }
      if (`${method} ${path}` === "GET /v1/ae/attention") return ok(currentList);
      return ok({ id: "x", first_seen: null, events: [], channel_ids: {}, slots: [], admins: [] });
    });
    q(node, '[data-id="social:li-1"] .nt-toggle')!.click();
    await flush();
    const row = q(node, '[data-id="social:li-1"]')!;
    expect(reads).toBe(1);
    // Copy text is clicked while the opening read is still out: it must not reuse it.
    await flush();
    button(row, "copy").click();
    expect(reads).toBe(2);
    // The newer read lands first, then the older one: the older never replaces it.
    gates[1]!();
    await settle();
    gates[0]!();
    await settle();
    expect(actions.written).toEqual(["New read"]);
    expect(q(row, ".nt-post-text")!.textContent).toBe("New read");
  });
});

describe("a failed newer read still outranks an older one", () => {
  it("drops an older read that lands after a newer forced read failed, keeping the held text and the stale note", async () => {
    const { node, request } = await mount(overview([POST()], ME));
    await open(node);
    q(node, '[data-id="social:li-1"] .nt-toggle')!.click();
    await settle();
    const gates: Array<(answer: Reply) => void> = [];
    let reads = 0;
    request.mockImplementation(async ({ method, path }: { method: string; path: string }) => {
      if (`${method} ${path}` === "POST /v1/ae/social/slot") {
        reads += 1;
        return await new Promise<Reply>((resolve) => gates.push(resolve));
      }
      if (`${method} ${path}` === "GET /v1/ae/attention") return ok(currentList);
      return ok({ id: "x", first_seen: null, events: [], channel_ids: {}, slots: [], admins: [] });
    });
    // The row reopens (read 1 is out), then Copy text starts a newer forced read (read 2).
    q(node, '[data-id="social:li-1"] .nt-toggle')!.click();
    await flush();
    const row = q(node, '[data-id="social:li-1"]')!;
    button(row, "copy").click();
    expect(reads).toBe(2);
    // The newer read fails first; the older one, which may predate the edit, lands afterwards.
    gates[1]!({ ok: false, status: 500, body: { error: { code: "boom", message: "the box is asleep" } } });
    await settle();
    expect(q(row, ".nt-post-stale")).not.toBeNull();
    gates[0]!(ok({ ...DETAIL("A read from before the edit"), draft_rev: "r0" }));
    await settle();
    expect(q(row, ".nt-post-text")!.textContent).toBe("Hello\nsecond line");
    expect(q(row, ".nt-post-stale")).not.toBeNull();
  });
});
