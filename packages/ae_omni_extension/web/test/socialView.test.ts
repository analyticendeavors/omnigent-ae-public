// The Social card's full view (socialView.ts, calendar.ts, socialEditor.ts, socialScore.ts,
// socialSettings.ts; 2026-11-06, part 2): the calendar's date arithmetic and keys, the queue,
// the editor's buttons and limits, the Scorecard's numbers and chart, the Settings fold.

import type { ExtensionContext } from "@omnigent/extension-sdk";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { SocialScore, SocialSlot, SocialView } from "../src/api";
import { addDays, addMonths, dayKeyOf, mondayOf, monthWeeks, moveFocus, slotsByDay, weekKeys, weekRange } from "../src/calendar";
import { countChars, LIMIT, localInput, moveDefault, postUrlProblem, rescheduleRange, resetEditorStash } from "../src/socialEditor";
import { chart, kpis } from "../src/socialScore";
import { BOOL_FIELDS, NUMBER_FIELDS, rulesPatch, toolsFrom } from "../src/socialSettings";
import { queueRows, openSocialView, closeSocialView, pushSocialView } from "../src/socialView";
import { drawSettings } from "../src/socialSettings";
import { resetTiles } from "../src/tiles";

const sdk = vi.hoisted(() => ({ defineExtension: vi.fn() }));
vi.mock("@omnigent/extension-sdk", () => sdk);
const copied = vi.hoisted(() => ({ copyText: vi.fn(async (_text: string) => true) }));
vi.mock("../src/lot", () => copied);

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
  has_draft: true,
  draft_preview: "Preview",
  topic: null,
  draft_by: "auto",
  posted: null,
  ...fields,
});

const score = (fields: Partial<SocialScore> = {}): SocialScore => ({
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
  weeks: [
    { week: "2026w39", start: "2026-09-21", posted: 3 },
    { week: "2026w40", start: "2026-09-28", posted: 1 },
    { week: "2026w41", start: "2026-10-05", posted: 2, state: "first" },
    { week: "2026w46", start: "2026-11-09", posted: 1 },
  ],
  first_week: false,
  unposted: [{ id: "li-short-ae-ccccccccccc", kind: "short", platform: "linkedin", channel: "ae", title: "Sample Short", due: "2026-10-27T16:00:00Z" }],
  unposted_total: 1,
  rotation: [
    { id: "diagram", name: "Diagram tool", last: at(-24 * 8), next: at(24 * 2) },
    { id: "skins", name: "Skins & Tools", last: null, next: null },
  ],
  pace: { ae: { long: 1, short: 5, live: 0 }, hc: { long: 0, short: 0, live: 0 } },
  ...fields,
});

function view(fields: Partial<SocialView> = {}): SocialView {
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
    tools: [
      { id: "diagram", name: "Diagram tool", url: "https://tools.example.com/diagram-tool", note: null, active: true },
      { id: "skins", name: "Skins & Tools", url: "https://tools.example.com/skins", note: "Preview", active: true },
    ],
    uploads_meta: { ae: { read_at: at(-0.5), ok: true, error: null, count: 4 }, hc: { read_at: at(-0.5), ok: true, error: null, count: 2 } },
    gaps: {
      ae: { read_failed: false, error: null, stale: false, upload_gap: false },
      hc: { read_failed: false, error: null, stale: false, upload_gap: true },
    },
    score: score(),
    slots: [
      slot("li-expert-2026w46-1", 3),
      slot("yc-video-ae-aaaaaaaaaaa", 4, { kind: "video", platform: "yt_community", channel: "ae", title: "Tour video" }),
      slot("li-tool-2026w46", 50, { kind: "tool", tool: "Diagram tool" }),
      slot("li-old", -30, { state: "posted", posted: { at: at(-29), url: null, via: "manual" } }),
    ],
    slot_count: 4,
    truncated: false,
    channel_ids: { ae: "UCexampleChannelOne", hc: "UCexampleChannelTwo" },
    ...fields,
  };
}

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

const flush = () => vi.advanceTimersByTimeAsync(0);
const q = (sel: string) => document.querySelector<HTMLElement>(sel);
const all = (sel: string) => [...document.querySelectorAll<HTMLElement>(sel)];
const btn = (label: string) => all("button").find((b) => b.textContent === label) as HTMLButtonElement;

beforeEach(() => {
  resetEditorStash();
  resetTiles();
  copied.copyText.mockClear();
  copied.copyText.mockResolvedValue(true);
  document.body.replaceChildren();
  vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout", "Date"] });
  vi.setSystemTime(NOW);
});

afterEach(() => {
  closeSocialView();
  vi.useRealTimers();
});

describe("calendar dates", () => {
  it("reads a post's day on the rules' clock, not the browser's", () => {
    expect(dayKeyOf(Date.parse("2026-11-11T03:00:00Z"), TZ)).toBe("2026-11-10");
    expect(dayKeyOf(Date.parse("2026-11-11T20:00:00Z"), TZ)).toBe("2026-11-11");
    expect(dayKeyOf(NOW, "Not/AZone")).toMatch(/^2026-11-1[12]$/);
  });

  it("finds Monday, the week, the month grid and the range label", () => {
    expect(mondayOf("2026-11-11")).toBe("2026-11-09");
    expect(mondayOf("2026-11-15")).toBe("2026-11-09");
    expect(weekKeys("2026-11-11")[6]).toBe("2026-11-15");
    expect(addDays("2026-12-30", 3)).toBe("2027-01-02");
    expect(addMonths("2026-12-15", 1)).toBe("2027-01-01");
    const weeks = monthWeeks("2026-11-11");
    expect(weeks[0][0]).toBe("2026-10-26");
    expect(weeks.at(-1)?.[6]).toBe("2026-12-06");
    expect(weeks.every((w) => w.length === 7)).toBe(true);
    expect(weekRange("2026-11-11")).toBe("Nov 9 to 15, 2026");
    expect(weekRange("2026-11-30")).toBe("Nov 30 to Dec 6, 2026");
  });

  it("groups posts by day and filters by platform", () => {
    const slots = view().slots;
    const all = slotsByDay(slots, TZ, "all");
    expect(all.get("2026-11-11")?.map((s) => s.id)).toEqual(["li-expert-2026w46-1", "yc-video-ae-aaaaaaaaaaa"]);
    expect([...slotsByDay(slots, TZ, "youtube").values()].flat().map((s) => s.id)).toEqual(["yc-video-ae-aaaaaaaaaaa"]);
    expect([...slotsByDay(slots, TZ, "linkedin").values()].flat().some((s) => s.platform === "yt_community")).toBe(false);
  });

  it("moves the focus with the arrows", () => {
    expect(moveFocus("2026-11-11", "ArrowRight")).toBe("2026-11-12");
    expect(moveFocus("2026-11-11", "ArrowLeft")).toBe("2026-11-10");
    expect(moveFocus("2026-11-11", "ArrowDown")).toBe("2026-11-18");
    expect(moveFocus("2026-11-11", "ArrowUp")).toBe("2026-11-04");
    expect(moveFocus("2026-11-11", "Home")).toBe("2026-11-09");
    expect(moveFocus("2026-11-11", "End")).toBe("2026-11-15");
    expect(moveFocus("2026-11-11", "a")).toBeNull();
  });
});

describe("the queue", () => {
  it("lists open posts, overdue first", () => {
    const slots = [slot("late2", -2), slot("soon", 1), slot("late1", -9), slot("done", -3, { state: "posted" })];
    expect(queueRows(slots, NOW, "all").map((s) => s.id)).toEqual(["late1", "late2", "soon"]);
    expect(queueRows([slot("c", 1, { platform: "yt_community" }), slot("l", 2)], NOW, "linkedin").map((s) => s.id)).toEqual(["l"]);
  });
});

describe("the full view", () => {
  const open = (v: SocialView, handler: (call: Call) => unknown = () => v) => {
    const s = stub(handler);
    const refresh = vi.fn(async () => undefined);
    openSocialView(s.context, v, null, refresh);
    return { ...s, refresh };
  };

  it("draws the week with each post's kind, title, time, draft state and channel", () => {
    open(view());
    const chips = all(".sv-chip");
    const community = chips.find((c) => c.dataset.slot === "yc-video-ae-aaaaaaaaaaa");
    expect(community?.textContent).toContain("Community");
    expect(community?.textContent).toContain("Tour video");
    expect(community?.textContent).toContain("draft");
    expect(community?.textContent).toContain("AE");
    expect(community?.textContent).toMatch(/\d:\d\d/);
    expect(all(".sv-day")).toHaveLength(7);
    expect(q(".sv-day.is-today")?.dataset.day).toBe("2026-11-11");
    expect(q(".sv-range")?.textContent).toBe("Nov 9 to 15, 2026");
  });

  it("switches to the month and steps the range", () => {
    open(view());
    btn("Month").click();
    expect(all(".sv-day").length).toBeGreaterThanOrEqual(35);
    expect(q(".sv-range")?.textContent).toBe("November 2026");
    q('[aria-label="Next month"]')?.click();
    expect(q(".sv-range")?.textContent).toBe("December 2026");
  });

  it("moves between days with the arrow keys and opens the day on Enter", async () => {
    const { request } = open(view(), ({ path }) =>
      path === "/v1/ae/social/slot" ? { ...slot("li-expert-2026w46-1", 3), draft: { text: "Body", topic: null, by: "auto", edited: false } } : view(),
    );
    const today = q('.sv-day[data-day="2026-11-11"]') as HTMLElement;
    today.focus();
    today.dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowRight", bubbles: true }));
    expect(document.activeElement?.getAttribute("data-day")).toBe("2026-11-12");
    // Enter on a day with two posts lists them; it does not guess one.
    today.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true }));
    await flush();
    expect(q(".sv-daylist")?.textContent).toContain("Posts on Wed Nov 11 (2)");
    expect(request).not.toHaveBeenCalled();
    // A day with one post goes straight to the editor.
    const next = q('.sv-day[data-day="2026-11-13"]') as HTMLElement;
    expect(next).not.toBeNull();
    const tue = view({ slots: [slot("only", 24 * 2)] });
    pushSocialView(tue, null);
    (q('.sv-day[data-day="2026-11-13"]') as HTMLElement).dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true }));
    await flush();
    expect(request).toHaveBeenCalledWith({ method: "POST", path: "/v1/ae/social/slot", body: { id: "only" } });
  });

  it("redraws the calendar from a live read but keeps what is typed in the editor", async () => {
    const detail = { ...slot("li-expert-2026w46-1", 3), draft: { text: "Body", topic: null, by: "auto", edited: false } };
    open(view(), ({ path }) => (path === "/v1/ae/social/slot" ? detail : view()));
    (q('[data-slot="li-expert-2026w46-1"]') as HTMLElement).click();
    await flush();
    const area = q(".sv-ed-text") as HTMLTextAreaElement;
    area.value = "Body, edited";
    area.dispatchEvent(new Event("input"));
    pushSocialView(view({ slots: [slot("li-expert-2026w46-1", 3), slot("new-one", 5)] }), null);
    expect((q(".sv-ed-text") as HTMLTextAreaElement).value).toBe("Body, edited");
    expect(q(".sv-qlist")?.children.length).toBe(2);
  });

  it("shows the scorecard: five numbers, the chart, the lists and Post late", async () => {
    const { request } = open(view(), ({ path }) =>
      path === "/v1/ae/social/slot" ? { ...slot("li-short-ae-ccccccccccc", -200, { kind: "short" }), draft: null } : view(),
    );
    btn("Scorecard").click();
    const boxes = all(".sv-kpis .social-stat").map((b) => b.textContent);
    expect(boxes).toHaveLength(5);
    expect(boxes[0]).toContain("1 of 3");
    expect(boxes[2]).toContain("4 weeks");
    expect(boxes[2]).toContain("Best: 6 weeks");
    expect(boxes[3]).toContain("86%");
    expect(boxes[4]).toContain("2 of 2");
    expect(q(".sv-chart")).not.toBeNull();
    expect(all(".sv-panel").map((p) => p.querySelector("h4")?.textContent)).toEqual([
      "LinkedIn posts per week",
      "Went public without a post (1)",
      "Tool rotation",
      "Upload pace, next 14 days",
    ]);
    expect(q(".sv-score")?.textContent).toContain("last promoted 8 d ago");
    expect(q(".sv-score")?.textContent).toContain("not promoted yet");
    expect(q(".sv-score")?.textContent).toContain("1 video, 5 Shorts");
    expect(q(".sv-score")?.textContent).toContain("nothing scheduled");
    expect(all(".sv-score .social-chip").map((c) => c.textContent)).toEqual(["OK", "Gap"]);
    btn("Post late").click();
    await flush();
    expect(request).toHaveBeenCalledWith({ method: "POST", path: "/v1/ae/social/slot", body: { id: "li-short-ae-ccccccccccc" } });
    expect(q(".sv-seg.is-on")?.textContent).toBe("Week");
    expect(q(".sv-ed-text")).not.toBeNull();
  });
});

describe("the scorecard numbers and chart", () => {
  it("reads First week in place of On pace and Behind, and never as a streak week", () => {
    const [week, , streak] = kpis(score({ first_week: true, on_pace: false, streak: 0, best_streak: 0 }));
    expect(week.note).toBe("First week");
    expect(week.good).toBe(true);
    expect(streak.note).toContain("First week is not counted");
  });

  it("draws weeks before the start as muted notes, not zero bars", () => {
    const svg = chart(
      score({
        weeks: [
          { week: "a", start: "2026-10-26", posted: 0, state: "before_start" },
          { week: "b", start: "2026-11-02", posted: 2, state: "first" },
          { week: "c", start: "2026-11-09", posted: 1 },
        ],
      }),
    );
    expect(svg.querySelectorAll(".sv-bar-before")).toHaveLength(1);
    expect(svg.textContent).toContain("before start");
    expect(svg.textContent).toContain("first week");
    expect(svg.textContent).toContain("1 so far");
    expect(svg.querySelector(".sv-target")?.getAttribute("y1")).not.toBeNull();
    expect(svg.textContent).toContain("Target 3");
    // The current week is outlined; the two before it are bars or a note.
    expect(svg.querySelectorAll(".sv-bar-now")).toHaveLength(1);
    expect(svg.querySelectorAll(".sv-bar-open")).toHaveLength(1);
  });
});

describe("the editor", () => {
  const draftFor = (text: string | null, fields: Partial<SocialSlot> = {}) => ({
    ...slot("li-expert-2026w46-1", 3, fields),
    draft: text === null ? null : { text, topic: null, by: "auto", edited: false },
  });
  const run = async (handler: (call: Call) => unknown = () => draftFor("Hello 'world'")) => {
    const v = view();
    const s = stub(handler);
    const refresh = vi.fn(async () => undefined);
    openSocialView(s.context, v, null, refresh);
    (q('[data-slot="li-expert-2026w46-1"]') as HTMLElement).click();
    await flush();
    return { ...s, refresh };
  };

  it("counts characters against the 3,000 limit and blocks Copy over it", async () => {
    await run();
    const area = q(".sv-ed-text") as HTMLTextAreaElement;
    expect(q(".sv-ed-count")?.textContent).toBe("13 / 3,000");
    area.value = "a".repeat(LIMIT + 1);
    area.dispatchEvent(new Event("input"));
    expect(q(".sv-ed-count")?.textContent).toBe("3,001 / 3,000");
    expect(q(".sv-ed-count")?.className).toContain("is-over");
    expect(btn("Copy").disabled).toBe(true);
    expect(btn("Save and copy").disabled).toBe(true);
    expect(btn("Post on LinkedIn").disabled).toBe(true);
    expect(q(".sv-ed-limit")?.textContent).toContain("Over 3,000 characters");
    expect(countChars("\u{1F680}".repeat(3))).toBe(3);
  });

  it("Copy copies what is on screen, edits included", async () => {
    await run();
    const area = q(".sv-ed-text") as HTMLTextAreaElement;
    area.value = "Edited text";
    area.dispatchEvent(new Event("input"));
    expect(q(".sv-ed-meta")?.textContent).toContain("Unsaved edits");
    btn("Copy").click();
    await flush();
    expect(copied.copyText).toHaveBeenCalledWith("Edited text");
  });

  it("Save and copy saves first, then copies, then reads the card again", async () => {
    const { request, refresh } = await run();
    const area = q(".sv-ed-text") as HTMLTextAreaElement;
    area.value = "New words";
    area.dispatchEvent(new Event("input"));
    btn("Save and copy").click();
    await flush();
    await flush();
    expect(request).toHaveBeenCalledWith({ method: "POST", path: "/v1/ae/social/draft", body: { id: "li-expert-2026w46-1", text: "New words" } });
    expect(copied.copyText).toHaveBeenCalledWith("New words");
    expect(refresh).toHaveBeenCalled();
  });

  it("says the text is still there when the save fails, and does not copy", async () => {
    const s = stub(({ path }) => (path === "/v1/ae/social/draft" ? undefined : draftFor("Body")));
    s.request.mockImplementation(async (call: Call) =>
      call.path === "/v1/ae/social/draft"
        ? ({ ok: false, status: 500, body: { error: { code: "x", message: "disk full" } } } as never)
        : ({ ok: true, status: 200, body: draftFor("Body") } as never),
    );
    openSocialView(s.context, view(), null, vi.fn(async () => undefined));
    (q('[data-slot="li-expert-2026w46-1"]') as HTMLElement).click();
    await flush();
    btn("Save and copy").click();
    await flush();
    await flush();
    expect(q(".sv-ed-status")?.textContent).toContain("did not save");
    expect(q(".sv-ed-status")?.textContent).toContain("still here");
    // The copy starts in the tap, before the save (iOS), so it is on the clipboard either way.
    expect(q(".sv-ed-status")?.textContent).toContain("was copied");
  });

  it("Post on LinkedIn copies, then opens the composer with the text (apostrophe as %27)", async () => {
    const { openExternal } = await run();
    btn("Post on LinkedIn").click();
    await flush();
    expect(copied.copyText).toHaveBeenCalledWith("Hello 'world'");
    expect(openExternal).toHaveBeenCalledWith("https://www.linkedin.com/feed/?shareActive=true&text=Hello%20%27world%27");
  });

  it("keeps the draft copied and says where to paste when the host refuses the link", async () => {
    const s = stub(() => draftFor("Body"));
    s.openExternal.mockRejectedValue(new Error("URL was not provided by the host"));
    openSocialView(s.context, view(), null, vi.fn(async () => undefined));
    (q('[data-slot="li-expert-2026w46-1"]') as HTMLElement).click();
    await flush();
    btn("Post on LinkedIn").click();
    await flush();
    await flush();
    expect(q(".sv-ed-status")?.textContent).toContain("Draft copied");
    expect(q(".sv-ed-status")?.textContent).toContain("paste it at linkedin.com/feed");
  });

  it("opens the channel's Posts tab for a Community post", async () => {
    const s = stub(() => ({ ...slot("yc-video-ae-aaaaaaaaaaa", 4, { kind: "video", platform: "yt_community", channel: "ae", title: "Tour video" }), draft: { text: "Body", topic: null, by: "auto", edited: false } }));
    openSocialView(s.context, view(), null, vi.fn(async () => undefined));
    (q('[data-slot="yc-video-ae-aaaaaaaaaaa"]') as HTMLElement).click();
    await flush();
    btn("Post on YouTube").click();
    await flush();
    expect(s.openExternal).toHaveBeenCalledWith("https://www.youtube.com/channel/UCexampleChannelOne/posts");
    expect(q(".sv-ed-text")).not.toBeNull();
  });

  it("marks posted with an optional link, and refuses a link that is not https", async () => {
    const { request, refresh } = await run();
    (q(".sv-ed-url") as HTMLInputElement).value = "http://linkedin.com/x";
    btn("Mark posted").click();
    await flush();
    expect(q(".sv-ed-status")?.textContent).toContain("must start with https://");
    expect(request).not.toHaveBeenCalledWith(expect.objectContaining({ path: "/v1/ae/social/posted" }));
    (q(".sv-ed-url") as HTMLInputElement).value = "https://www.linkedin.com/posts/abc";
    btn("Mark posted").click();
    await flush();
    await flush();
    expect(request).toHaveBeenCalledWith({
      method: "POST",
      path: "/v1/ae/social/posted",
      body: { id: "li-expert-2026w46-1", url: "https://www.linkedin.com/posts/abc" },
    });
    expect(refresh).toHaveBeenCalled();
    expect(postUrlProblem("")).toBeNull();
    expect(postUrlProblem(" https://a.example/x ")).toBeNull();
    expect(postUrlProblem("https://a b")).not.toBeNull();
  });

  it("skips, and shows the server's range in the reschedule picker", async () => {
    const { request } = await run();
    const when = q(".sv-ed-when") as HTMLInputElement;
    const range = rescheduleRange(NOW);
    expect(when.min).toBe(range.min);
    expect(when.max).toBe(range.max);
    expect(Date.parse(range.max) - Date.parse(range.min)).toBeGreaterThanOrEqual(121 * 24 * HOUR - HOUR);
    expect(localInput(NOW)).toMatch(/^\d{4}-\d\d-\d\dT\d\d:\d\d$/);
    btn("Skip").click();
    await flush();
    expect(request).toHaveBeenCalledWith({ method: "POST", path: "/v1/ae/social/skip", body: { id: "li-expert-2026w46-1" } });
  });

  it("Move sends an ISO time and refuses one outside the range", async () => {
    const { request } = await run();
    const when = q(".sv-ed-when") as HTMLInputElement;
    when.value = localInput(NOW + 200 * 24 * HOUR);
    btn("Move").click();
    await flush();
    expect(q(".sv-ed-status")?.textContent).toContain("a day ago to 120 days ahead");
    expect(request).not.toHaveBeenCalledWith(expect.objectContaining({ path: "/v1/ae/social/reschedule" }));
    when.value = localInput(NOW + 5 * 24 * HOUR);
    btn("Move").click();
    await flush();
    const call = request.mock.calls.map((c) => c[0] as Call).find((c) => c.path === "/v1/ae/social/reschedule");
    expect((call?.body as { due: string }).due).toMatch(/Z$/);
  });

  it("keeps Post via API disabled with its hint", async () => {
    await run();
    const api = btn("Post via API");
    expect(api.disabled).toBe(true);
    expect(api.title).toBe("Needs a LinkedIn app; later phase");
    expect(q(".sv-ed-api")?.textContent).toContain("Needs a LinkedIn app; later phase.");
  });

  it("says what to do when a post will not load", async () => {
    const s = stub(() => ({}));
    s.request.mockImplementation(async () => ({ ok: false, status: 404, body: { error: { code: "not_found", message: "no slot" } } }) as never);
    openSocialView(s.context, view(), null, vi.fn(async () => undefined));
    (q('[data-slot="li-expert-2026w46-1"]') as HTMLElement).click();
    await flush();
    expect(q(".sv-editor")?.textContent).toContain("Could not load that post");
    expect(q(".sv-editor")?.textContent).toContain("open it again");
  });
});

describe("the settings fold", () => {
  const draw = (v = view()) => {
    const host = document.createElement("div");
    document.body.appendChild(host);
    // The rules route answers with the rules it now holds; the tools route with a count.
    const s = stub(({ path, body }) =>
      path.endsWith("/rules") ? { rules: { ...v.rules, ...(body as object) } } : { tools: 2 },
    );
    const changed = vi.fn(async () => undefined);
    drawSettings(host, v, { context: s.context, changed });
    return { host, ...s, changed, v };
  };

  it("gives every rule and every tool a hint", () => {
    const { host, v } = draw();
    const rows = [...host.querySelectorAll(".sv-set-row")];
    const rules = BOOL_FIELDS.length + NUMBER_FIELDS.length + 3; // the weekday, the time and the zone
    expect(rows).toHaveLength(rules + v.tools.length * 3); // switch, framing line, image link
    for (const row of rows) {
      const tip = row.querySelector<HTMLElement>(".ae-hint-btn");
      expect(tip, row.textContent ?? "").not.toBeNull();
      expect((tip?.dataset.hint ?? "").length).toBeGreaterThan(20);
      expect(tip?.dataset.hint?.length).toBeLessThanOrEqual(140 * 2);
    }
    for (const f of [...BOOL_FIELDS, ...NUMBER_FIELDS]) {
      expect(host.querySelector(`[data-rule="${f.key}"]`), f.key).not.toBeNull();
    }
  });

  it("saves only the rules that changed", async () => {
    const { host, request, changed } = draw();
    (host.querySelector('[data-rule="li_weekly_target"]') as HTMLInputElement).value = "4";
    (host.querySelector('[data-rule="short_li"]') as HTMLInputElement).checked = false;
    [...host.querySelectorAll("button")].find((b) => b.textContent === "Save rules")?.click();
    await flush();
    await flush();
    expect(request).toHaveBeenCalledWith({ method: "POST", path: "/v1/ae/social/rules", body: { short_li: false, li_weekly_target: 4 } });
    expect(changed).toHaveBeenCalled();
  });

  it("refuses a value outside the server's range with what to enter", async () => {
    const { host, request } = draw();
    (host.querySelector('[data-rule="gap_days"]') as HTMLInputElement).value = "99";
    [...host.querySelectorAll("button")].find((b) => b.textContent === "Save rules")?.click();
    await flush();
    expect(host.querySelector(".sv-set-status")?.textContent).toContain("whole number from 1 to 60");
    expect(request).not.toHaveBeenCalled();
    expect(() => rulesPatch(host, view().rules)).toThrow(/whole number from 1 to 60/);
  });

  it("saves the tool switches and framing lines as a whole list", async () => {
    const { host, request, v } = draw();
    (host.querySelector('[data-tool="diagram"][data-field="active"]') as HTMLInputElement).checked = false;
    (host.querySelector('[data-tool="diagram"][data-field="note"]') as HTMLInputElement).value = "  Call it a preview  ";
    [...host.querySelectorAll("button")].find((b) => b.textContent === "Save tools")?.click();
    await flush();
    await flush();
    const body = { tools: [{ ...v.tools[0], active: false, note: "Call it a preview" }, v.tools[1]] };
    expect(request).toHaveBeenCalledWith({ method: "POST", path: "/v1/ae/social/tools", body });
    expect(toolsFrom(host, v.tools)[1].note).toBe("Preview");
  });

  it("saves a tool's image link, and an emptied one as null", async () => {
    const { host, request, v } = draw();
    const field = (id: string) => host.querySelector(`[data-tool="${id}"][data-field="image"]`) as HTMLInputElement;
    expect(field("diagram").getAttribute("type")).toBe("url");
    field("diagram").value = " https://tools.example.com/og-card-1200.jpg ";
    [...host.querySelectorAll("button")].find((b) => b.textContent === "Save tools")?.click();
    await flush();
    await flush();
    const tools = (request.mock.calls[0][0] as { body: { tools: Array<{ id: string; image?: string | null }> } }).body.tools;
    expect(tools[0].image).toBe("https://tools.example.com/og-card-1200.jpg");
    expect("image" in tools[1]).toBe(false); // an older server sent none, and the box is empty
    field("diagram").value = "https://tools.example.com/a.jpg";
    expect(toolsFrom(host, [{ ...v.tools[0], image: null }])[0].image).toBe("https://tools.example.com/a.jpg");
    field("diagram").value = "";
    expect(toolsFrom(host, [{ ...v.tools[0], image: "https://tools.example.com/a.jpg" }])[0].image).toBeNull();
  });

  it("opens from the gear button and is not redrawn by a live read", async () => {
    const s = stub(() => view());
    openSocialView(s.context, view(), null, vi.fn(async () => undefined));
    expect(q(".sv-settings")?.hidden).toBe(true);
    (q('[aria-label="Settings"]') as HTMLElement).click();
    expect(q(".sv-settings")?.hidden).toBe(false);
    const input = q('[data-rule="li_weekly_target"]') as HTMLInputElement;
    input.value = "5";
    pushSocialView(view(), null);
    expect((q('[data-rule="li_weekly_target"]') as HTMLInputElement).value).toBe("5");
  });
});


describe("review round 1 fixes", () => {
  const two = (id: string) => ({
    ...slot(id, id === "li-expert-2026w46-1" ? 3 : 4, id === "li-expert-2026w46-1" ? {} : { kind: "video", platform: "yt_community", channel: "ae", title: "Tour video" }),
    draft: { text: `Saved ${id}`, topic: null, by: "auto", edited: false },
  });
  const setup = async () => {
    const s = stub(({ path, body }) => (path === "/v1/ae/social/slot" ? two((body as { id: string }).id) : view()));
    const refresh = vi.fn(async () => undefined);
    openSocialView(s.context, view(), null, refresh);
    (q('.sv-qrow[data-slot="li-expert-2026w46-1"]') as HTMLElement).click();
    await flush();
    return { ...s, refresh };
  };
  const type = (text: string) => {
    const area = q(".sv-ed-text") as HTMLTextAreaElement;
    area.value = text;
    area.dispatchEvent(new Event("input"));
  };

  it("keeps the text when the open post is clicked again", async () => {
    const { request } = await setup();
    type("My unsaved words");
    (q('.sv-qrow[data-slot="li-expert-2026w46-1"]') as HTMLElement).click();
    await flush();
    expect((q(".sv-ed-text") as HTMLTextAreaElement).value).toBe("My unsaved words");
    expect(request).toHaveBeenCalledTimes(1);
    expect(q(".sv-ed-guard")).toBeNull();
  });

  it("asks before switching posts with unsaved edits, and Keep editing stays", async () => {
    await setup();
    type("My unsaved words");
    (q('.sv-qrow[data-slot="yc-video-ae-aaaaaaaaaaa"]') as HTMLElement).click();
    await flush();
    expect(q(".sv-ed-guard")?.textContent).toContain("Discard unsaved edits?");
    btn("Keep editing").click();
    expect(q(".sv-ed-guard")).toBeNull();
    expect((q(".sv-ed-text") as HTMLTextAreaElement).value).toBe("My unsaved words");
    (q('.sv-qrow[data-slot="yc-video-ae-aaaaaaaaaaa"]') as HTMLElement).click();
    btn("Discard").click();
    await flush();
    expect((q(".sv-ed-text") as HTMLTextAreaElement).value).toBe("Saved yc-video-ae-aaaaaaaaaaa");
  });

  it("asks before Close, Mark posted and Skip, and a clean editor does not ask", async () => {
    const { request } = await setup();
    btn("Close").click();
    expect(q(".sv-editor")?.textContent).toBe("");
    (q('.sv-qrow[data-slot="li-expert-2026w46-1"]') as HTMLElement).click();
    await flush();
    type("edited");
    btn("Close").click();
    expect(q(".sv-ed-guard")).not.toBeNull();
    btn("Keep editing").click();
    btn("Skip").click();
    expect(q(".sv-ed-guard")).not.toBeNull();
    expect(request).not.toHaveBeenCalledWith(expect.objectContaining({ path: "/v1/ae/social/skip" }));
    btn("Discard").click();
    await flush();
    await flush();
    expect(request).toHaveBeenCalledWith({ method: "POST", path: "/v1/ae/social/skip", body: { id: "li-expert-2026w46-1" } });
    // After the confirmed Skip nothing says "Unsaved edits" on the post.
    expect(q(".sv-ed-meta")?.textContent).not.toContain("Unsaved edits");
    expect((q(".sv-ed-text") as HTMLTextAreaElement).value).toBe("Saved li-expert-2026w46-1");
    type("edited again");
    btn("Mark posted").click();
    expect(q(".sv-ed-guard")).not.toBeNull();
  });

  it("starts the Move picker at the next whole hour for an overdue post", async () => {
    const next = Math.floor(NOW / HOUR) * HOUR + HOUR;
    expect(moveDefault(NOW - 20 * 24 * HOUR, NOW)).toBe(next);
    expect(moveDefault(NOW + 5 * 24 * HOUR, NOW)).toBe(NOW + 5 * 24 * HOUR);
    expect(moveDefault(NOW + 300 * 24 * HOUR, NOW)).toBe(NOW + 120 * 24 * HOUR);
    const s = stub(() => ({ ...slot("old", -24 * 20), draft: null }));
    openSocialView(s.context, view({ slots: [slot("old", -24 * 20)] }), null, vi.fn(async () => undefined));
    (q('.sv-qrow[data-slot="old"]') as HTMLElement).click();
    await flush();
    expect((q(".sv-ed-when") as HTMLInputElement).value).toBe(localInput(next));
    btn("Move").click();
    await flush();
    expect(s.request.mock.calls.some((c) => (c[0] as Call).path === "/v1/ae/social/reschedule")).toBe(true);
  });

  it("moves the roving tab stop with the arrow keys", () => {
    openSocialView(stub(() => view()).context, view(), null, vi.fn(async () => undefined));
    const today = q('.sv-day[data-day="2026-11-11"]') as HTMLElement;
    today.focus();
    today.dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowRight", bubbles: true }));
    const stops = all(".sv-day").filter((d) => d.tabIndex === 0);
    expect(stops.map((d) => d.dataset.day)).toEqual(["2026-11-12"]);
  });

  it("gives focus back to a chip, by its post, after a live read", () => {
    openSocialView(stub(() => view()).context, view(), null, vi.fn(async () => undefined));
    const chip = q('.sv-chip[data-slot="yc-video-ae-aaaaaaaaaaa"]') as HTMLElement;
    chip.focus();
    expect(document.activeElement).toBe(chip);
    pushSocialView(view(), null);
    const again = document.activeElement as HTMLElement;
    expect(again.classList.contains("sv-chip")).toBe(true);
    expect(again.dataset.slot).toBe("yc-video-ae-aaaaaaaaaaa");
  });

  it("dates the Scorecard lists on the rules' clock, not the UTC date", () => {
    // 03:00 UTC on Nov 12 is still Nov 11 in Los Angeles.
    const late = score({
      unposted: [{ id: "x1", kind: "short", platform: "linkedin", channel: "ae", title: "Late one", due: "2026-11-12T03:00:00Z" }],
      rotation: [{ id: "diagram", name: "Diagram tool", last: null, next: "2026-11-12T03:00:00Z" }],
    });
    openSocialView(stub(() => view()).context, view({ score: late }), null, vi.fn(async () => undefined));
    btn("Scorecard").click();
    expect(q(".sv-score")?.textContent).toContain("public Nov 11");
    expect(q(".sv-score")?.textContent).toContain("next Nov 11");
  });

  it("saves the rules twice in one session, taking each answer as the new baseline", async () => {
    const host = document.createElement("div");
    document.body.appendChild(host);
    const s = stub(({ body }) => ({ rules: { ...view().rules, ...(body as object) } }));
    drawSettings(host, view(), { context: s.context, changed: vi.fn(async () => undefined) });
    const target = () => host.querySelector('[data-rule="li_weekly_target"]') as HTMLInputElement;
    const save = () => [...host.querySelectorAll("button")].find((b) => b.textContent === "Save rules")?.click();
    target().value = "4";
    save();
    await flush();
    await flush();
    target().value = "3"; // back to the value the page opened with
    save();
    await flush();
    await flush();
    const bodies = s.request.mock.calls.map((c) => (c[0] as Call).body);
    expect(bodies).toEqual([{ li_weekly_target: 4 }, { li_weekly_target: 3 }]);
    expect(host.querySelector(".sv-set-status")?.textContent).toContain("Rules saved");
  });

  it("saves the tools twice in one session", async () => {
    const host = document.createElement("div");
    document.body.appendChild(host);
    const s = stub(() => ({ tools: 2 }));
    drawSettings(host, view(), { context: s.context, changed: vi.fn(async () => undefined) });
    const on = () => host.querySelector('[data-tool="diagram"][data-field="active"]') as HTMLInputElement;
    const save = () => [...host.querySelectorAll("button")].find((b) => b.textContent === "Save tools")?.click();
    on().checked = false;
    save();
    await flush();
    await flush();
    on().checked = true;
    save();
    await flush();
    await flush();
    const sent = s.request.mock.calls.map((c) => ((c[0] as Call).body as { tools: Array<{ active: boolean }> }).tools[0].active);
    expect(sent).toEqual([false, true]);
  });

  it("offers the expert post day as weekday names, Monday to Sunday, with the same 0 to 6 values", () => {
    const host = document.createElement("div");
    document.body.appendChild(host);
    drawSettings(host, view(), { context: stub(() => ({})).context, changed: vi.fn(async () => undefined) });
    const select = host.querySelector('select[data-rule="expert_weekday"]') as HTMLSelectElement;
    expect([...select.options].map((o) => [o.value, o.textContent])).toEqual([
      ["0", "Monday"], ["1", "Tuesday"], ["2", "Wednesday"], ["3", "Thursday"], ["4", "Friday"], ["5", "Saturday"], ["6", "Sunday"],
    ]);
    expect(select.value).toBe("2");
    select.value = "4";
    expect(rulesPatch(host, view().rules)).toEqual({ expert_weekday: 4 });
    expect(select.closest(".sv-set-row")?.querySelector(".ae-hint-btn")).not.toBeNull();
  });

  it("keeps the dirty flag and the text when the call after a confirmed Discard fails", async () => {
    const { request } = await setup();
    request.mockImplementation(async (call: Call) =>
      call.path === "/v1/ae/social/skip"
        ? ({ ok: false, status: 500, body: { error: { code: "x", message: "boom" } } } as never)
        : ({ ok: true, status: 200, body: two((call.body as { id: string }).id) } as never),
    );
    type("precious words");
    btn("Skip").click();
    btn("Discard").click();
    await flush();
    await flush();
    expect(q(".sv-ed-status")?.textContent).toContain("Could not skip it");
    expect((q(".sv-ed-text") as HTMLTextAreaElement).value).toBe("precious words");
    expect(q(".sv-editor")?.dataset.dirty).toBe("1");
    expect(q(".sv-ed-meta")?.textContent).toContain("Unsaved edits");
    // Close still asks, because the text is still unsaved.
    btn("Close").click();
    expect(q(".sv-ed-guard")).not.toBeNull();
  });

  it("restores unsaved text, with a note, after the dialog's X closed the view", async () => {
    await setup();
    type("kept through the X");
    (q(".ae-dialog-close") as HTMLElement).click();
    expect(q(".sv-dialog-card")).toBeNull();
    const s = stub(({ path, body }) => (path === "/v1/ae/social/slot" ? two((body as { id: string }).id) : view()));
    openSocialView(s.context, view(), null, vi.fn(async () => undefined));
    (q('.sv-qrow[data-slot="li-expert-2026w46-1"]') as HTMLElement).click();
    await flush();
    expect((q(".sv-ed-text") as HTMLTextAreaElement).value).toBe("kept through the X");
    expect(q(".sv-ed-status")?.textContent).toContain("Unsaved edits restored");
    expect(q(".sv-ed-meta")?.textContent).toContain("Unsaved edits");
  });

  it("forgets the text when the user confirmed Discard", async () => {
    await setup();
    type("thrown away");
    (q('.sv-qrow[data-slot="yc-video-ae-aaaaaaaaaaa"]') as HTMLElement).click();
    btn("Discard").click();
    await flush();
    (q('.sv-qrow[data-slot="li-expert-2026w46-1"]') as HTMLElement).click();
    await flush();
    expect((q(".sv-ed-text") as HTMLTextAreaElement).value).toBe("Saved li-expert-2026w46-1");
    expect(q(".sv-ed-status")?.textContent).not.toContain("restored");
  });
});

describe("settings refresh failures", () => {
  it("reports a refresh failure after a good save on its own, not as a failed save", async () => {
    const host = document.createElement("div");
    document.body.appendChild(host);
    const s = stub(({ body }) => ({ rules: { ...view().rules, ...(body as object) } }));
    const changed = vi.fn(async () => {
      throw new Error("card read failed");
    });
    drawSettings(host, view(), { context: s.context, changed });
    (host.querySelector('[data-rule="li_weekly_target"]') as HTMLInputElement).value = "5";
    [...host.querySelectorAll("button")].find((b) => b.textContent === "Save rules")?.click();
    await flush();
    await flush();
    const said = host.querySelector(".sv-set-status")?.textContent ?? "";
    expect(said).toContain("Saved; the card could not refresh");
    expect(said).not.toContain("did not save");
    // The baseline moved: setting it back to the opening value is a change, not "No rule changed".
    (host.querySelector('[data-rule="li_weekly_target"]') as HTMLInputElement).value = "3";
    [...host.querySelectorAll("button")].find((b) => b.textContent === "Save rules")?.click();
    await flush();
    await flush();
    expect(s.request).toHaveBeenCalledTimes(2);
  });

  it("does the same for tools", async () => {
    const host = document.createElement("div");
    document.body.appendChild(host);
    const s = stub(() => ({ tools: 2 }));
    drawSettings(host, view(), {
      context: s.context,
      changed: vi.fn(async () => {
        throw new Error("card read failed");
      }),
    });
    (host.querySelector('[data-tool="diagram"][data-field="active"]') as HTMLInputElement).checked = false;
    [...host.querySelectorAll("button")].find((b) => b.textContent === "Save tools")?.click();
    await flush();
    await flush();
    expect(host.querySelector(".sv-set-status")?.textContent).toContain("Saved; the card could not refresh");
  });
});
