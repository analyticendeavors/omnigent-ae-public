// The Social card on a phone (2026-11-06, part 3): the layout switch, the filter menu, the Week
// agenda and the Month dots, starter text, the Post button, a copy that starts inside the tap,
// and "Did you post it?".

import type { ExtensionContext } from "@omnigent/extension-sdk";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import css from "../src/style.css?raw";
import type { SocialSlot, SocialView } from "../src/api";
import { resetEditorStash, LIMIT } from "../src/socialEditor";
import { agendaGroups, agendaRange } from "../src/socialAgenda";
import { forgetFullDraft, loadedDraft, rememberDraft } from "../src/socialDrafts";
import { dismissBanner, noteReturn, rememberPost, resetPosted, WINDOW_MS } from "../src/socialPosted";
import { closeSocialView, openSocialView, pushSocialView } from "../src/socialView";
import { PHONE_MAX, PHONE_QUERY, starterText, unboldFirstLine } from "../src/socialWords";
import { resetTiles } from "../src/tiles";

const sdk = vi.hoisted(() => ({ defineExtension: vi.fn() }));
vi.mock("@omnigent/extension-sdk", () => sdk);
const copied = vi.hoisted(() => ({ copyText: vi.fn(async (_text: string) => true) }));
vi.mock("../src/lot", () => copied);

// Wednesday 2026-11-11, 12:00 in Los Angeles.
const NOW = Date.parse("2026-11-11T20:00:00Z");
const HOUR = 3_600_000;
const TZ = "America/Los_Angeles";
const at = (hours: number) => new Date(NOW + hours * HOUR).toISOString();

const slot = (id: string, hours: number, fields: Partial<SocialSlot> = {}): SocialSlot => ({
  id,
  kind: "expert",
  platform: "linkedin",
  channel: null,
  anchor: null,
  due: at(hours),
  skippable: false,
  state: "planned",
  rescheduled: false,
  orphan: false,
  draft_error: null,
  title: null,
  tool: null,
  url: null,
  has_draft: true,
  draft_preview: "Preview",
  topic: null,
  draft_by: "auto",
  posted: null,
  ...fields,
});

function view(slots: SocialSlot[], fields: Partial<SocialView> = {}): SocialView {
  return {
    rules: {
      video_li: true,
      video_community: true,
      short_li: true,
      short_li_days: 3,
      live_li: true,
      tool_every_days: 7,
      expert_per_week: 1,
      expert_weekday: 2,
      li_weekly_target: 3,
      gap_days: 7,
      alarm_hhmm: "09:00",
      tz: TZ,
    },
    tools: [],
    uploads_meta: {},
    gaps: {},
    score: {
      week_posted: 1,
      week_target: 3,
      on_pace: true,
      promoted: 7,
      promoted_of: 9,
      missed: 2,
      streak: 4,
      best_streak: 6,
      on_time_pct: 86,
      on_time_of: 14,
      community_30d: { posted: 2, total: 2 },
      weeks: [],
      first_week: false,
      unposted: [],
      unposted_total: 0,
      rotation: [],
      pace: { ae: { long: 1, short: 5, live: 0 }, hc: { long: 0, short: 0, live: 0 } },
    },
    slots,
    slot_count: slots.length,
    truncated: false,
    channel_ids: { ae: "UCexampleChannelOne", hc: "UCexampleChannelTwo" },
    channel_names: { ae: "Analytic Endeavors", hc: "Second Co" },
    ...fields,
  };
}

// One of everything the agenda has to sort.
const overOld = slot("over-old", -30, { title: "Old short", kind: "short", channel: "ae", has_draft: false });
const overToday = slot("over-today", -1, { title: "This morning" });
const doneToday = slot("done-today", -2, { state: "posted", posted: { at: at(-2), url: null, via: "manual" } });
const laterToday = slot("later-today", 3);
const tomorrow = slot("tomorrow", 22, { kind: "video", platform: "yt_community", channel: "ae", title: "Tour" });
const skippedMon = slot("skipped-mon", -60, { state: "skipped" });
const far = slot("far", 24 * 20);
const ALL = [overOld, overToday, doneToday, laterToday, tomorrow, skippedMon, far];

type Call = { method: string; path: string; body?: unknown };
function stub(handler: (call: Call) => unknown) {
  const request = vi.fn(async (call: Call) => ({ ok: true, status: 200, body: handler(call) }));
  const openExternal = vi.fn(async (_url: string) => undefined);
  const context = {
    view: "fabric",
    server: { request },
    navigation: { openExternal },
    storage: { user: { get: vi.fn(async () => undefined), set: vi.fn(async () => undefined) } },
  } as unknown as ExtensionContext;
  return { context, request, openExternal };
}

/**
 * matchMedia that reports a phone or not, and lets a test change its mind. `touch` answers the
 * `(pointer: coarse)` query (2026-11-07); it is false unless a test asks, so a narrow window
 * is not taken for a finger.
 */
function width(phone: boolean, touch = false) {
  let now = phone;
  const listeners = new Set<(event: { matches: boolean }) => void>();
  const queries: string[] = [];
  window.matchMedia = ((query: string) => {
    queries.push(query);
    return {
      get matches() {
        return query.includes("pointer: coarse") ? touch : now;
      },
      media: query,
      addEventListener: (_: string, listener: (event: { matches: boolean }) => void) => listeners.add(listener),
      removeEventListener: (_: string, listener: (event: { matches: boolean }) => void) => listeners.delete(listener),
    };
  }) as never;
  return {
    queries,
    set: (value: boolean) => {
      now = value;
      for (const listener of [...listeners]) listener({ matches: value });
    },
  };
}

const flush = () => vi.advanceTimersByTimeAsync(0);
const q = (sel: string) => document.querySelector<HTMLElement>(sel);
const all = (sel: string) => [...document.querySelectorAll<HTMLElement>(sel)];
const btn = (label: string) => all("button").find((b) => b.textContent === label) as HTMLButtonElement;

beforeEach(() => {
  resetEditorStash();
  resetTiles();
  resetPosted();
  copied.copyText.mockClear();
  copied.copyText.mockResolvedValue(true);
  document.body.replaceChildren();
  vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout", "Date"] });
  vi.setSystemTime(NOW);
});

afterEach(() => {
  closeSocialView();
  vi.useRealTimers();
  delete (window as { matchMedia?: unknown }).matchMedia;
});

const open = (slots: SocialSlot[], handler: (call: Call) => unknown = () => view(slots)) => {
  const s = stub(handler);
  const refresh = vi.fn(async () => undefined);
  openSocialView(s.context, view(slots), null, refresh);
  return { ...s, refresh };
};

describe("the phone breakpoint", () => {
  it("is one number in the JS and the CSS", () => {
    expect(PHONE_QUERY).toBe(`(max-width: ${PHONE_MAX}px)`);
    expect(css).toContain(`@media (max-width: ${PHONE_MAX}px) {\n  .ae-dialog:has(.sv-dialog-card)`);
  });

  it("draws the agenda on a phone and the week grid on a desktop, and follows a resize", () => {
    const w = width(true);
    open(ALL);
    expect(q(".sv-agenda")).not.toBeNull();
    expect(all(".sv-day")).toHaveLength(0);
    expect(q(".sv-queue")?.textContent).toBe(""); // the agenda replaces "Not posted yet"
    expect(w.queries).toContain(PHONE_QUERY);
    w.set(false);
    expect(q(".sv-agenda")).toBeNull();
    expect(all(".sv-day")).toHaveLength(7);
    expect(q(".sv-queue")?.textContent).toContain("Not posted yet");
    w.set(true);
    expect(q(".sv-agenda")).not.toBeNull();
  });
});

describe("the bar", () => {
  it("is two rows: tabs with a filter and a gear, then the range with Today on the right", () => {
    width(true);
    open(ALL);
    const top = q(".sv-bar-top") as HTMLElement;
    expect(top.querySelector(".sv-tabs")?.textContent).toBe("WeekMonthScore");
    expect(top.querySelector('[aria-label="Filter posts"]')).not.toBeNull();
    expect(top.querySelector('[aria-label="Settings"]')?.getAttribute("title")).toBeTruthy();
    expect(q(".sv-nav .sv-range")?.textContent).toBe("Nov 9 to 22");
    expect(q(".sv-nav .sv-today")?.textContent).toBe("Today");
  });

  it("keeps Scorecard as the tab's name on a desktop", () => {
    width(false);
    open(ALL);
    expect(q(".sv-tabs")?.textContent).toBe("WeekMonthScorecard");
  });

  it("opens a filter menu with a tick on the active choice, and a dot when a filter is on", () => {
    width(false);
    open(ALL);
    const trigger = q(".sv-filter-btn") as HTMLButtonElement;
    const menu = q(".sv-filter-menu") as HTMLElement;
    expect(menu.hidden).toBe(true);
    expect((q(".sv-filter-dot") as HTMLElement).hidden).toBe(true);
    trigger.click();
    expect(menu.hidden).toBe(false);
    const items = all(".sv-menu-item");
    expect(items.map((i) => i.textContent)).toEqual(["All posts", "LinkedIn only", "YouTube only"]);
    expect(items.map((i) => i.getAttribute("aria-checked"))).toEqual(["true", "false", "false"]);
    items[2].click();
    expect(menu.hidden).toBe(true);
    expect((q(".sv-filter-dot") as HTMLElement).hidden).toBe(false);
    expect(trigger.getAttribute("aria-label")).toContain("YouTube only");
    expect(all(".sv-day .sv-chip").every((c) => c.classList.contains("k-community"))).toBe(true);
    trigger.click();
    expect(all(".sv-menu-item").map((i) => i.getAttribute("aria-checked"))).toEqual(["false", "false", "true"]);
  });

  it("closes the menu on Escape (not the dialog) and on an outside click", () => {
    width(false);
    open(ALL);
    const trigger = q(".sv-filter-btn") as HTMLButtonElement;
    trigger.click();
    expect(document.activeElement?.classList.contains("sv-menu-item")).toBe(true);
    document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
    expect((q(".sv-filter-menu") as HTMLElement).hidden).toBe(true);
    expect(q(".sv-dialog-card")).not.toBeNull();
    expect(document.activeElement).toBe(trigger);
    trigger.click();
    (q(".sv-body") as HTMLElement).click();
    expect((q(".sv-filter-menu") as HTMLElement).hidden).toBe(true);
    // With nothing open, Escape closes the dialog as before.
    document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
    expect(q(".sv-dialog-card")).toBeNull();
  });

  it("keeps the menu open through a live read", () => {
    width(true);
    open(ALL);
    (q(".sv-filter-btn") as HTMLButtonElement).click();
    pushSocialView(view(ALL), null);
    expect((q(".sv-filter-menu") as HTMLElement).hidden).toBe(false);
  });

  it("opens Settings from the gear", () => {
    width(true);
    open(ALL);
    const gear = q('[aria-label="Settings"]') as HTMLButtonElement;
    gear.click();
    expect((q(".sv-settings") as HTMLElement).hidden).toBe(false);
    expect(gear.getAttribute("aria-expanded")).toBe("true");
  });
});

describe("the Week list", () => {
  const groupsOf = (slots: SocialSlot[], day = "2026-11-11", filter: "all" | "linkedin" | "youtube" = "all") =>
    agendaGroups(slots, TZ, filter, day, NOW).map((g) => [g.kind, g.day, g.slots.map((s) => s.id)]);

  it("labels the 14-day range from the week's Monday", () => {
    expect(agendaRange("2026-11-11")).toBe("Nov 9 to 22");
    expect(agendaRange("2026-11-25")).toBe("Nov 23 to Dec 6");
  });

  it("puts overdue first, then today, then the days to come, and leaves empty days and far posts out", () => {
    expect(groupsOf(ALL)).toEqual([
      ["overdue", null, ["over-old", "over-today"]],
      ["today", "2026-11-11", ["done-today", "later-today"]],
      ["day", "2026-11-12", ["tomorrow"]],
      ["earlier", null, ["skipped-mon"]],
    ]);
  });

  it("shows overdue only when the range holds today", () => {
    const next = groupsOf(ALL, "2026-11-25");
    expect(next.some(([kind]) => kind === "overdue")).toBe(false);
    expect(groupsOf(ALL, "2026-10-21").some(([kind]) => kind === "overdue")).toBe(false);
  });

  it("follows the filter", () => {
    expect(groupsOf(ALL, "2026-11-11", "youtube")).toEqual([["day", "2026-11-12", ["tomorrow"]]]);
  });

  it("draws cards: badge, title, draft state, time; posted and skipped are dimmed; empty says so", () => {
    width(true);
    open(ALL);
    const cards = all(".sv-ac");
    expect(cards.map((c) => c.dataset.slot)).toEqual(["over-old", "over-today", "done-today", "later-today", "tomorrow", "skipped-mon"]);
    const heads = all(".sv-ag-head");
    expect(heads.map((h) => h.className.replace("sv-ag-head ", ""))).toEqual(["g-overdue", "g-today", "g-day", "g-earlier"]);
    expect(heads[1].textContent).toBe("Today, Wed Nov 11");
    expect(heads[2].textContent).toBe("Thu, Nov 12" + "1 post");
    expect(heads[3].textContent).toBe("EARLIER" + "1");
    expect(cards[5].querySelector(".sv-ac-time")?.textContent).toBe("Mon 12:00 AM");
    const old = cards[0];
    expect(old.classList.contains("is-late")).toBe(true);
    expect(old.textContent).toContain("Short: Old short");
    expect(old.textContent).toContain("No draft");
    expect(old.querySelector(".sv-ac-time")?.textContent).toBe("Tue");
    expect(cards[2].classList.contains("st-posted")).toBe(true);
    expect(cards[2].textContent).toContain("Posted");
    expect(cards[5].classList.contains("st-skipped")).toBe(true);
    expect(cards[5].textContent).toContain("Skipped");
    expect(cards[4].querySelector(".sv-ac-badge")?.getAttribute("aria-label")).toBe("YouTube");
    expect(cards[3].querySelector(".sv-ac-badge")?.getAttribute("aria-label")).toBe("LinkedIn");
    pushSocialView(view([]), null);
    expect(q(".sv-agenda-empty")?.textContent).toBe("Nothing planned from Nov 9 to 22.");
  });

  it("steps a week at a time and opens the editor on a tap", async () => {
    width(true);
    const { request } = open(ALL, ({ path, body }) =>
      path === "/v1/ae/social/slot"
        ? { ...slot((body as { id: string }).id, 3), draft: { text: "Body", topic: null, by: "auto", edited: false } }
        : view(ALL),
    );
    (q('[aria-label="Next week"]') as HTMLElement).click();
    expect(q(".sv-range")?.textContent).toBe("Nov 16 to 29");
    (q('[aria-label="Previous week"]') as HTMLElement).click();
    expect(q(".sv-range")?.textContent).toBe("Nov 9 to 22");
    (q('.sv-ac[data-slot="later-today"]') as HTMLElement).click();
    await flush();
    expect(request).toHaveBeenCalledWith({ method: "POST", path: "/v1/ae/social/slot", body: { id: "later-today" } });
    // The editor is a screen of its own: the bar and the list step aside, a back link appears.
    expect((q(".sv-bar") as HTMLElement).hidden).toBe(true);
    expect((q(".sv-left") as HTMLElement).hidden).toBe(true);
    expect(q(".sv-ed-back")?.textContent).toBe("Week");
    (q(".sv-ed-back") as HTMLElement).click();
    expect((q(".sv-bar") as HTMLElement).hidden).toBe(false);
    expect((q(".sv-left") as HTMLElement).hidden).toBe(false);
  });
});

describe("the Month dots", () => {
  it("draws a square per day with a dot per post, today in blue and the picked day listed below", () => {
    width(true);
    open(ALL);
    (all(".sv-seg").find((b) => b.textContent === "Month") as HTMLElement).click();
    expect(all(".sv-cal-dots .sv-day").length).toBeGreaterThanOrEqual(35);
    expect(all(".sv-cal-dots .sv-chip")).toHaveLength(0);
    expect(all(".sv-cal-head span").map((s) => s.textContent).join("")).toBe("MTWTFSS");
    const today = q('.sv-day[data-day="2026-11-11"]') as HTMLElement;
    expect(today.classList.contains("is-today")).toBe(true);
    // Wed Nov 11 holds overdue-this-morning, done-today and later-today: three LinkedIn dots.
    expect(today.querySelectorAll(".sv-dot.d-li")).toHaveLength(3);
    expect((q('.sv-day[data-day="2026-11-12"]') as HTMLElement).querySelectorAll(".sv-dot.d-yt")).toHaveLength(1);
    // Today's posts are listed until another day is picked.
    expect(q(".sv-daylist .sv-ag-head")?.textContent).toBe("Wed, Nov 11" + "3 posts");
    (q('.sv-day[data-day="2026-11-12"]') as HTMLElement).click();
    expect((q('.sv-day[data-day="2026-11-12"]') as HTMLElement).classList.contains("is-selected")).toBe(true);
    expect(all(".sv-daylist .sv-ac").map((c) => c.dataset.slot)).toEqual(["tomorrow"]);
    // Tapping a day lists its posts; it does not open one.
    expect(q(".sv-editor")?.hasAttribute("hidden")).toBe(true);
  });

  it("keeps the desktop month as chips", () => {
    width(false);
    open(ALL);
    (all(".sv-seg").find((b) => b.textContent === "Month") as HTMLElement).click();
    expect(all(".sv-cal-month .sv-chip").length).toBeGreaterThan(0);
    expect(all(".sv-dot")).toHaveLength(0);
  });
});

describe("starter text", () => {
  const upload = (fields: Partial<SocialSlot>) =>
    slot("x", 1, { channel: "ae", title: "Fabric Tips", url: "https://youtu.be/aaaaaaaaaaa", ...fields });

  it("has one template per kind and none for an expert post", () => {
    expect(starterText(upload({ kind: "video" }))).toBe("New video on the Analytic Endeavors channel: Fabric Tips.\n\nhttps://youtu.be/aaaaaaaaaaa");
    expect(starterText(upload({ kind: "video", platform: "yt_community" }))).toBe("New video: Fabric Tips\n\nhttps://youtu.be/aaaaaaaaaaa");
    // The channel's name comes from the server; without it the short code stands in.
    expect(starterText(upload({ kind: "short", channel: "hc", url: "https://www.youtube.com/shorts/bbbbbbbbbbb" }), { hc: "Second Co" })).toBe(
      "New Short on the Second Co channel: Fabric Tips.\n\nhttps://www.youtube.com/shorts/bbbbbbbbbbb",
    );
    expect(starterText(upload({ kind: "short", channel: "hc" }))).toContain("on the HC channel:");
    expect(starterText(upload({ kind: "live" }))).toBe("Live on YouTube soon: Fabric Tips.\n\nhttps://youtu.be/aaaaaaaaaaa");
    expect(starterText(slot("t", 1, { kind: "tool", tool: "Diagram tool", url: "https://tools.example.com/diagram-tool" }))).toBe(
      "Diagram tool: https://tools.example.com/diagram-tool",
    );
    expect(starterText(slot("e", 1, { kind: "expert", topic: "Something", url: null }))).toBe("");
    // No link (an older server) or no title: nothing to start from.
    expect(starterText(upload({ kind: "video", url: null }))).toBe("");
    expect(starterText(upload({ kind: "video", title: null }))).toBe("");
    // A title that ends in a question mark gets no second stop.
    expect(starterText(upload({ kind: "short", title: "Why dbt?" }))).toContain("channel: Why dbt?\n\n");
  });
});

describe("the editor's Post button", () => {
  const noDraft = (fields: Partial<SocialSlot> = {}) =>
    slot("short-1", 2, { kind: "short", channel: "ae", title: "Q", has_draft: false, draft_preview: null, url: "https://www.youtube.com/shorts/bbbbbbbbbbb", ...fields });
  const run = async (target: SocialSlot, draft: { text: string } | null = null) => {
    const s = open([target], ({ path }) =>
      path === "/v1/ae/social/slot" ? { ...target, draft: draft && { text: draft.text, topic: null, by: "auto", edited: false } } : view([target]),
    );
    (q(`[data-slot="${target.id}"]`) as HTMLElement).click();
    await flush();
    return s;
  };
  const type = (text: string) => {
    const area = q(".sv-ed-text") as HTMLTextAreaElement;
    area.value = text;
    area.dispatchEvent(new Event("input"));
  };

  it("fills the box with starter text, says so, and does not call it unsaved", async () => {
    width(false);
    await run(noDraft());
    const area = q(".sv-ed-text") as HTMLTextAreaElement;
    expect(unboldFirstLine(area.value)).toBe("New Short on the Analytic Endeavors channel: Q.\n\nhttps://www.youtube.com/shorts/bbbbbbbbbbb");
    expect(q(".sv-ed-meta")?.textContent).toContain("Starter text");
    expect(q(".sv-ed-meta")?.textContent).not.toContain("Unsaved");
    expect(q(".sv-ed-text")?.closest(".sv-editor")?.getAttribute("data-dirty")).toBeNull();
    // Closing needs no "Discard?" question.
    btn("Close").click();
    expect(q(".sv-ed-guard")).toBeNull();
  });

  it("calls an edit of the starter text unsaved, and going back to it clean again", async () => {
    width(false);
    await run(noDraft());
    const starter = (q(".sv-ed-text") as HTMLTextAreaElement).value;
    type(`${starter} Edited`);
    expect(q(".sv-ed-meta")?.textContent).toContain("Unsaved edits");
    expect(q(".sv-ed-meta")?.textContent).not.toContain("Starter text");
    type(starter);
    expect(q(".sv-ed-meta")?.textContent).toContain("Starter text");
    expect(q(".sv-editor")?.getAttribute("data-dirty")).toBeNull();
  });

  it("uses a saved draft in place of the starter text", async () => {
    width(false);
    await run(noDraft({ has_draft: true }), { text: "My words" });
    expect((q(".sv-ed-text") as HTMLTextAreaElement).value).toBe("My words");
    expect(q(".sv-ed-meta")?.textContent).not.toContain("Starter text");
  });

  it("is enabled with an empty box for an expert post, and opens the composer empty", async () => {
    width(false);
    const { openExternal } = await run(slot("expert-1", 2, { has_draft: false, draft_preview: null }));
    expect((q(".sv-ed-text") as HTMLTextAreaElement).value).toBe("");
    const post = btn("Post on LinkedIn");
    expect(post.disabled).toBe(false);
    expect(btn("Copy").disabled).toBe(true);
    post.click();
    await flush();
    expect(openExternal).toHaveBeenCalledWith("https://www.linkedin.com/feed/?shareActive=true");
    expect(copied.copyText).not.toHaveBeenCalled();
  });

  it("is disabled over 3,000 characters, with the reason", async () => {
    width(false);
    await run(noDraft());
    type("a".repeat(LIMIT + 1));
    const post = btn("Post on LinkedIn");
    expect(post.disabled).toBe(true);
    expect(btn("Copy").disabled).toBe(true);
    expect(post.title).toContain("Over 3,000 characters");
    expect(q(".sv-ed-limit")?.hidden).toBe(false);
    type("a".repeat(LIMIT));
    expect(post.disabled).toBe(false);
    expect(q(".sv-ed-limit")?.hidden).toBe(true);
  });

  it("starts the copy inside the tap, before any await, for Post and for Copy", async () => {
    width(true);
    const { openExternal } = await run(noDraft());
    const text = (q(".sv-ed-text") as HTMLTextAreaElement).value;
    btn("Copy").click();
    expect(copied.copyText).toHaveBeenCalledTimes(1);
    expect(copied.copyText).toHaveBeenCalledWith(text);
    await flush();
    btn("Post on LinkedIn").click();
    // Synchronous: nothing has been awaited since the tap.
    expect(copied.copyText).toHaveBeenCalledTimes(2);
    expect(openExternal).toHaveBeenCalledTimes(1);
    expect(copied.copyText.mock.invocationCallOrder[1]).toBeLessThan(openExternal.mock.invocationCallOrder[0]);
    await flush();
    expect(openExternal.mock.calls[0][0]).toContain("https://www.linkedin.com/feed/?shareActive=true&text=");
  });

  it("says where to paste when the host refuses the link, and does not ask 'Did you post it?'", async () => {
    width(false);
    const { openExternal } = await run(noDraft());
    openExternal.mockRejectedValueOnce(new Error("not allowed"));
    btn("Post on LinkedIn").click();
    await flush();
    await flush();
    expect(q(".sv-ed-status")?.textContent).toContain("Draft copied");
    expect(q(".sv-ed-status")?.textContent).toContain("linkedin.com/feed");
    vi.setSystemTime(NOW + 60_000);
    expect(noteReturn(NOW + 60_000)).toBe(false);
  });

  it("lays out a phone's editor with the Post bar pinned and Skip, Move, the link and Post via API behind More", async () => {
    width(true);
    await run(noDraft());
    expect(q(".sv-editor")?.classList.contains("is-phone")).toBe(true);
    const bar = q(".sv-ed-bar") as HTMLElement;
    expect(bar.firstElementChild?.textContent).toBe("Post on LinkedIn");
    expect(bar.firstElementChild?.classList.contains("btn-primary")).toBe(true);
    expect([...(bar.querySelector(".sv-ed-bar-row") as HTMLElement).children].map((c) => (c.querySelector(".sv-more-btn") ? "More" : c.textContent))).toEqual(["Copy", "Mark posted", "More"]);
    const panel = q(".sv-more-panel") as HTMLElement;
    expect(panel.hidden).toBe(true);
    (q(".sv-more-btn") as HTMLElement).click();
    expect(panel.hidden).toBe(false);
    expect(panel.querySelector(".sv-ed-url")).not.toBeNull();
    expect(panel.querySelector(".sv-ed-when")).not.toBeNull();
    expect(panel.textContent).toContain("Skip");
    expect(panel.textContent).toContain("Move");
    const api = [...panel.querySelectorAll("button")].find((b) => b.textContent === "Post via API") as HTMLButtonElement;
    expect(api.disabled).toBe(true);
    document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
    expect(panel.hidden).toBe(true);
    expect(q(".sv-dialog-card")).not.toBeNull();
  });

  it("asks before the back link throws away edits", async () => {
    width(true);
    await run(noDraft());
    type("Something new");
    (q(".sv-ed-back") as HTMLElement).click();
    expect(q(".sv-ed-guard")).not.toBeNull();
    btn("Keep editing").click();
    expect(q(".sv-ed-text")).not.toBeNull();
  });

  it("re-lays the open editor out when the width crosses the breakpoint, keeping the text", async () => {
    const w = width(false);
    await run(noDraft());
    type("Typed on a desktop");
    expect(q(".sv-ed-bar")).toBeNull();
    w.set(true);
    expect(q(".sv-ed-bar")).not.toBeNull();
    expect((q(".sv-ed-text") as HTMLTextAreaElement).value).toBe("Typed on a desktop");
    w.set(false);
    expect(q(".sv-ed-bar")).toBeNull();
    expect(btn("Post on LinkedIn")).toBeDefined();
  });

  it("opens the editor on a given post when the view opens", async () => {
    width(true);
    const target = noDraft();
    const s = stub(({ path }) => (path === "/v1/ae/social/slot" ? { ...target, draft: null } : view([target])));
    openSocialView(s.context, view([target]), null, vi.fn(async () => undefined), "short-1");
    await flush();
    expect(unboldFirstLine((q(".sv-ed-text") as HTMLTextAreaElement).value)).toContain("New Short on the Analytic Endeavors channel");
  });
});

describe("Did you post it?", () => {
  const T0 = NOW;
  const planned = slot("li-1", 3, { kind: "video", channel: "ae", title: "A post" });

  it("raises one banner when the page comes back within 30 minutes, and only once", () => {
    rememberPost("li-1", "linkedin", T0);
    expect(noteReturn(T0 + 10_000)).toBe(true);
    expect(noteReturn(T0 + 20_000)).toBe(false);
  });

  it("ignores the tap's own focus echo, and a return after 30 minutes", () => {
    rememberPost("li-1", "linkedin", T0);
    expect(noteReturn(T0 + 500)).toBe(false);
    expect(noteReturn(T0 + WINDOW_MS + 1)).toBe(false);
    rememberPost("li-2", "linkedin", T0);
    expect(noteReturn(T0 + WINDOW_MS)).toBe(true);
  });

  it("shows in the full view above the list, Yes posts it with no link, then reads the card again", async () => {
    width(true);
    const { request, refresh } = open([planned], ({ path }) => (path === "/v1/ae/social/posted" ? { ok: true } : view([planned])));
    rememberPost("li-1", "linkedin", NOW - 60_000);
    window.dispatchEvent(new Event("focus"));
    expect(noteReturn(NOW)).toBe(true);
    const banner = q(".sv-banner") as HTMLElement;
    expect(banner.textContent).toContain("Back from LinkedIn. Did you post it?");
    expect(banner.textContent).toContain("A post");
    btn("Yes, mark posted").click();
    await flush();
    await flush();
    expect(request).toHaveBeenCalledWith({ method: "POST", path: "/v1/ae/social/posted", body: { id: "li-1" } });
    expect(refresh).toHaveBeenCalled();
    expect(q(".sv-banner")).toBeNull();
  });

  it("says YouTube for a Community post, and Not yet dismisses it for good", () => {
    width(true);
    const community = slot("yc-1", 3, { kind: "video", platform: "yt_community", channel: "ae", title: "Tour" });
    open([community]);
    rememberPost("yc-1", "yt_community", NOW - 60_000);
    noteReturn(NOW);
    expect(q(".sv-banner")?.textContent).toContain("Back from YouTube.");
    btn("Not yet").click();
    expect(q(".sv-banner")).toBeNull();
    expect(noteReturn(NOW + 1_000)).toBe(false);
  });

  it("says what failed and keeps the banner when marking fails", async () => {
    width(true);
    const s = stub(() => view([planned]));
    s.request.mockImplementation(async (call: Call) =>
      call.path === "/v1/ae/social/posted"
        ? ({ ok: false, status: 500, body: { error: { code: "x", message: "disk full" } } } as never)
        : ({ ok: true, status: 200, body: view([planned]) } as never),
    );
    openSocialView(s.context, view([planned]), null, vi.fn(async () => undefined));
    rememberPost("li-1", "linkedin", NOW - 60_000);
    noteReturn(NOW);
    btn("Yes, mark posted").click();
    await flush();
    await flush();
    expect(q(".sv-banner-status")?.textContent).toContain("Could not mark it posted");
    expect(q(".sv-banner-status")?.textContent).toContain("try again");
    expect(q(".sv-banner")).not.toBeNull();
    dismissBanner();
  });

  it("never shows for a post that is no longer planned", () => {
    width(true);
    open([{ ...planned, state: "posted" }]);
    rememberPost("li-1", "linkedin", NOW - 60_000);
    noteReturn(NOW);
    expect(q(".sv-banner")).toBeNull();
  });
});

describe("review round 1 fixes", () => {
  const noDraft = (fields: Partial<SocialSlot> = {}) =>
    slot("short-1", 2, { kind: "short", channel: "ae", title: "Q", has_draft: false, draft_preview: null, url: "https://www.youtube.com/shorts/bbbbbbbbbbb", ...fields });
  const openEditor = async (target: SocialSlot, handler?: (call: Call) => unknown) => {
    const s = open([target], handler ?? (({ path }) => (path === "/v1/ae/social/slot" ? { ...target, draft: null } : view([target]))));
    (q(`[data-slot="${target.id}"]`) as HTMLElement).click();
    await flush();
    return s;
  };

  it("1: Save and copy drops the compact card's cached full draft for that post", async () => {
    width(false);
    rememberDraft("short-1", "old full text", "aaaa1111");
    expect(loadedDraft(noDraft({ draft_preview: "p".repeat(160), draft_rev: "aaaa1111" }))).toBe("old full text");
    // A new revision (an edit past the preview) misses; so does a save through the editor.
    expect(loadedDraft(noDraft({ draft_preview: "p".repeat(160), draft_rev: "bbbb2222" }))).toBeNull();
    await openEditor(noDraft());
    btn("Save and copy").click();
    await flush();
    await flush();
    expect(loadedDraft(noDraft({ draft_preview: "p".repeat(160), draft_rev: "aaaa1111" }))).toBeNull();
    forgetFullDraft("short-1");
  });

  it("2: Post arms 'Did you post it?' before the host call resolves, and forgets it if the call is refused", async () => {
    width(false);
    let release: () => void = () => undefined;
    let refuse: (e: Error) => void = () => undefined;
    const s = await openEditor(noDraft());
    s.openExternal.mockImplementationOnce(() => new Promise<undefined>((resolve, reject) => ((release = () => resolve(undefined)), (refuse = reject))));
    btn("Post on LinkedIn").click();
    await vi.advanceTimersByTimeAsync(3_000); // the app switch: the host call is still pending
    expect(noteReturn(Date.now())).toBe(true);
    release();
    await flush();
    resetPosted();
    // A refused link: nothing was opened, so nothing is asked.
    s.openExternal.mockImplementationOnce(() => new Promise<undefined>((_, reject) => (refuse = reject)));
    btn("Post on LinkedIn").click();
    refuse(new Error("not allowed"));
    await flush();
    await flush();
    await vi.advanceTimersByTimeAsync(3_000);
    expect(noteReturn(Date.now())).toBe(false);
  });

  it("3: Save and copy starts the copy in the tap, before the save round trip", async () => {
    width(true);
    await openEditor(noDraft());
    const text = (q(".sv-ed-text") as HTMLTextAreaElement).value;
    btn("Save and copy").click();
    expect(copied.copyText).toHaveBeenCalledWith(text); // synchronously
    await flush();
    await flush();
    expect(q(".sv-ed-status")?.textContent).not.toContain("did not save");
  });

  it("3: Save and copy says the text was copied but not saved when only the save failed", async () => {
    width(true);
    const target = noDraft();
    const s = await openEditor(target);
    s.request.mockImplementation(async (call: Call) =>
      call.path === "/v1/ae/social/draft"
        ? ({ ok: false, status: 500, body: { error: { code: "x", message: "disk full" } } } as never)
        : ({ ok: true, status: 200, body: { ...target, draft: null } } as never),
    );
    const text = (q(".sv-ed-text") as HTMLTextAreaElement).value;
    btn("Save and copy").click();
    expect(copied.copyText).toHaveBeenCalledWith(text);
    await flush();
    await flush();
    expect(q(".sv-ed-status")?.textContent).toContain("was copied, but the draft did not save");
    expect(q(".sv-ed-status")?.textContent).toContain("still here");
    expect(q(".sv-ed-meta")?.textContent).toContain("Starter text"); // nothing was saved
  });

  it("4: a phone's Week and Month say the list is cut when the server cut it", () => {
    width(true);
    open(ALL);
    pushSocialView(view(ALL, { truncated: true }), null);
    expect(q(".sv-queue")?.textContent).toContain("The list is cut to fit");
    (all(".sv-seg").find((b) => b.textContent === "Month") as HTMLElement).click();
    expect(q(".sv-queue")?.textContent).toContain("The list is cut to fit");
    pushSocialView(view(ALL, { truncated: false }), null);
    expect(q(".sv-queue")?.textContent).toBe("");
  });

  it("5: closing the editor with More open removes its listeners, and Back returns focus to the card", async () => {
    width(true);
    const target = noDraft();
    const spy = vi.spyOn(document, "removeEventListener");
    await openEditor(target);
    (q(".sv-more-btn") as HTMLElement).click();
    expect(q("[data-menu-open]")).not.toBeNull();
    (q(".sv-ed-back") as HTMLElement).click();
    expect(spy.mock.calls.some(([type]) => type === "keydown")).toBe(true);
    expect(document.activeElement?.getAttribute("data-slot")).toBe("short-1");
    spy.mockRestore();
    // The next Escape closes the dialog; nothing swallows it.
    document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
    expect(q(".sv-dialog-card")).toBeNull();
  });

  it("5: a redraw of the editor (a width change) closes an open More", async () => {
    const w = width(true);
    await openEditor(noDraft());
    (q(".sv-more-btn") as HTMLElement).click();
    w.set(false);
    expect(q("[data-menu-open]")).toBeNull();
  });

  it("7: after Save and copy on untouched starter text the post counts as drafted", async () => {
    width(false);
    await openEditor(noDraft());
    expect(q(".sv-ed-meta")?.textContent).toContain("Starter text");
    btn("Save and copy").click();
    await flush();
    await flush();
    expect(q(".sv-ed-meta")?.textContent).not.toContain("Starter text");
    expect(q(".sv-ed-meta")?.textContent).toContain("Draft by you");
    expect(q(".sv-ed-meta")?.textContent).not.toContain("Unsaved");
  });
});
