// The Social card (social.ts, 2026-11-06): the words and numbers, the tile, and the card's
// three buttons against a stubbed server.

import type { ExtensionContext } from "@omnigent/extension-sdk";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { SocialGap, SocialSlot, SocialView } from "../src/api";
import { AeRequestError } from "../src/api";
import {
  activateSocial,
  deactivateSocial,
  dueLabel,
  dueTodayCount,
  headerPill,
  openUrl,
  overdueCount,
  slotTitle,
  socialPill,
  socialPopout,
  socialSection,
  socialTile,
  upNext,
  warnings,
} from "../src/social";
import { resetPosted } from "../src/socialPosted";
import { resetShareProbe } from "../src/socialShare";
import { boldFirstLine, unboldFirstLine } from "../src/socialWords";
import { getTile, resetTiles } from "../src/tiles";

const sdk = vi.hoisted(() => ({ defineExtension: vi.fn() }));
vi.mock("@omnigent/extension-sdk", () => sdk);
const copied = vi.hoisted(() => ({ copyText: vi.fn(async (_text: string) => true) }));
vi.mock("../src/lot", () => copied);

type Reply = { ok: boolean; status: number; body: unknown };

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
  has_draft: true,
  draft_preview: "Preview",
  topic: null,
  draft_by: "auto",
  posted: null,
  ...fields,
});

const quiet: SocialGap = { read_failed: false, error: null, stale: false, upload_gap: false };

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
    tools: [],
    uploads_meta: {
      ae: { read_at: at(-0.5), ok: true, error: null, count: 4 },
      hc: { read_at: at(-0.5), ok: true, error: null, count: 2 },
    },
    gaps: { ae: quiet, hc: quiet },
    score: {
      week_posted: 1,
      week_target: 3,
      on_pace: true,
      promoted: 2,
      promoted_of: 3,
      missed: 1,
      streak: 2,
      best_streak: 4,
      on_time_pct: 80,
      on_time_of: 5,
      community_30d: { posted: 1, total: 2 },
      weeks: [],
      first_week: false,
      unposted: [],
      unposted_total: 0,
      rotation: [],
      pace: { ae: { long: 1, short: 0, live: 0 }, hc: { long: 0, short: 0, live: 0 } },
    },
    slots: [
      slot("li-video-ae-aaaaaaaaaaa", 30, { kind: "video", channel: "ae", title: "Fabric <b>tips</b>" }),
      slot("li-expert-2026w46-1", 3),
      slot("li-short-hc-bbbbbbbbbbb", 8, { kind: "short", channel: "hc", title: "Quick one", skippable: true, has_draft: false }),
      slot("li-tool-2026w46", 50, { kind: "tool", tool: "AE Diagram tool" }),
    ],
    slot_count: 4,
    truncated: false,
    channel_ids: { ae: "UCexampleChannelOne", hc: "UCexampleChannelTwo" },
    channel_names: { ae: "Analytic Endeavors", hc: "Second Co" },
    ...fields,
  };
}

const ok = (body: unknown): Reply => ({ ok: true, status: 200, body });

function stubContext(handler: (call: { method: string; path: string; body?: unknown }) => Reply) {
  const request = vi.fn(async (call: { method: string; path: string; body?: unknown }) => handler(call));
  const openExternal = vi.fn(async (_url: string) => undefined);
  const share = vi.fn(async (options: { text?: string; files?: File[]; probe?: true }) => (options.probe ? { available: true } : { shared: true }) as { shared?: true; available?: boolean });
  const context = {
    view: "fabric",
    server: { request },
    navigation: { openExternal, share },
    storage: { user: { get: vi.fn(async () => undefined), set: vi.fn(async () => undefined) } },
  } as unknown as ExtensionContext;
  return { context, request, openExternal, share };
}

const flush = () => vi.advanceTimersByTimeAsync(0);

function mount(context: ExtensionContext): HTMLElement {
  // The card header holds the pill; the section holds the rest.
  const card = document.createElement("section");
  card.append(socialPill(), socialPopout(), socialSection());
  document.getElementById("root")?.appendChild(card);
  activateSocial(context, card);
  return card;
}

const q = (node: ParentNode, sel: string) => node.querySelector<HTMLElement>(sel);
const all = (node: ParentNode, sel: string) => [...node.querySelectorAll<HTMLElement>(sel)];
const button = (node: ParentNode, label: string) =>
  all(node, "button").find((b) => b.textContent === label) as HTMLButtonElement;

beforeEach(() => {
  resetPosted();
  resetShareProbe();
  resetTiles();
  copied.copyText.mockClear();
  copied.copyText.mockResolvedValue(true);
  document.body.replaceChildren();
  const root = document.createElement("div");
  root.id = "root";
  document.body.appendChild(root);
  vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout", "Date"] });
  vi.setSystemTime(NOW);
});

afterEach(() => {
  deactivateSocial();
  vi.useRealTimers();
});

describe("words", () => {
  it("labels a due time in the rules' time zone", () => {
    expect(dueLabel(at(1), NOW, TZ)).toBe("Today 1:00 PM");
    expect(dueLabel(at(22), NOW, TZ)).toBe("Tomorrow 10:00 AM");
    expect(dueLabel("2026-11-13T14:00:00Z", NOW, TZ)).toBe("Fri 6:00 AM");
    expect(dueLabel("2026-11-30T17:00:00Z", NOW, TZ)).toBe("Nov 30 9:00 AM");
    expect(dueLabel("2026-11-09T17:00:00Z", NOW, TZ)).toBe("Overdue since Mon");
    expect(dueLabel("2026-10-01T17:00:00Z", NOW, TZ)).toBe("Overdue since Oct 1");
  });

  it("titles each kind of post", () => {
    expect(slotTitle(slot("a", 1, { kind: "video", title: "T" }))).toBe("Video: T");
    expect(slotTitle(slot("a", 1, { kind: "short", title: "T" }))).toBe("Short: T");
    expect(slotTitle(slot("a", 1, { kind: "live", title: "T" }))).toBe("Livestream: T");
    expect(slotTitle(slot("a", 1, { kind: "video", platform: "yt_community", title: "T" }))).toBe("Community post: T");
    expect(slotTitle(slot("a", 1, { kind: "tool", tool: "AE Diagram tool" }))).toBe("Tool: AE Diagram tool");
    expect(slotTitle(slot("a", 1))).toBe("Expert post");
    expect(slotTitle(slot("a", 1, { topic: "Measures" }))).toBe("Expert post: Measures");
  });

  it("lists overdue posts first and skips posted and skipped ones", () => {
    const slots = [
      slot("future", 5),
      slot("late-newer", -3),
      slot("late-older", -30),
      slot("done", -40, { state: "posted" }),
      slot("skipped", 1, { state: "skipped" }),
      slot("later", 9),
      slot("latest", 90),
    ];
    expect(upNext(slots, NOW).map((s) => s.id)).toEqual(["late-older", "late-newer", "future"]);
  });

  it("picks the header pill: overdue, due today, then pace", () => {
    expect(headerPill(view({ slots: [slot("a", -2), slot("b", -1)] }), NOW)).toEqual({ text: "2 overdue", tone: "err" });
    expect(headerPill(view({ slots: [slot("a", 3), slot("b", 5), slot("c", 40)] }), NOW)).toEqual({
      text: "2 due today",
      tone: "warn",
    });
    expect(headerPill(view({ slots: [slot("c", 40)] }), NOW)).toEqual({ text: "On track", tone: "ok" });
    const behind = view({ slots: [] });
    behind.score.on_pace = false;
    expect(headerPill(behind, NOW)).toEqual({ text: "Behind", tone: "warn" });
    expect(overdueCount(view({ slots: [slot("a", -2), slot("b", -1, { state: "posted" })] }), NOW)).toBe(1);
    expect(dueTodayCount(view({ slots: [slot("a", 3), slot("b", 40)] }), NOW)).toBe(1);
  });

  it("words the gaps", () => {
    const v = view({
      gaps: {
        ae: { ...quiet, read_failed: true, error: "invalid_grant" },
        hc: { ...quiet, upload_gap: true, stale: true },
      },
      uploads_meta: {
        ae: { read_at: at(-1), ok: false, error: "invalid_grant", count: 4 },
        hc: { read_at: at(-7), ok: true, error: null, count: 2 },
      },
    });
    expect(warnings(v, NOW)).toEqual([
      { text: "AE read failed: invalid_grant", tone: "err" },
      { text: "HC channel: nothing scheduled in the next 7 days.", tone: "warn" },
      { text: "YouTube read 7 h ago", tone: "warn" },
    ]);
  });
});

describe("tile", () => {
  it("is a problem when overdue, attention for today, behind or a gap, else ok", () => {
    expect(socialTile(view({ slots: [slot("a", -2)] }), null, NOW)).toMatchObject({
      id: "social",
      value: "1 overdue",
      note: "Streak 2 wk · 1 of 3 this week",
      health: "problem",
    });
    expect(socialTile(view({ slots: [slot("a", 3)] }), null, NOW)).toMatchObject({ value: "1 due today", health: "attention" });
    expect(socialTile(view({ slots: [] }), null, NOW)).toMatchObject({ value: "On track", health: "ok" });
    const gapped = view({ slots: [], gaps: { ae: { ...quiet, upload_gap: true }, hc: quiet } });
    expect(socialTile(gapped, null, NOW).health).toBe("attention");
    const behind = view({ slots: [] });
    behind.score.on_pace = false;
    expect(socialTile(behind, null, NOW)).toMatchObject({ value: "Behind", health: "attention" });
  });

  it("says Admins for a refusal and Unknown for a failure", () => {
    expect(socialTile(null, new AeRequestError(403, {}))).toMatchObject({ value: "Admins", health: "idle" });
    expect(socialTile(null, new Error("x"))).toMatchObject({ value: "Unknown", health: "attention" });
  });
});

describe("the open links", () => {
  it("builds the LinkedIn share link and the channel's Posts tab", () => {
    const v = view();
    expect(openUrl(slot("a", 1), v, "Hello & more")).toBe(
      "https://www.linkedin.com/feed/?shareActive=true&text=Hello%20%26%20more",
    );
    const yc = slot("a", 1, { platform: "yt_community", channel: "hc" });
    expect(openUrl(yc, v, "x")).toBe("https://www.youtube.com/channel/UCexampleChannelTwo/posts");
    expect(openUrl(slot("a", 1, { platform: "yt_community" }), v, "x")).toBeNull();
  });
});

describe("the card", () => {
  it("draws the pill, the three score boxes, the next three posts and the footer", async () => {
    const { context } = stubContext(() => ok(view()));
    const card = mount(context);
    await flush();
    expect(q(card, ".social-pill")?.textContent).toBe("2 due today");
    const boxes = all(card, ".social-stat").map((b) => b.textContent);
    expect(boxes[0]).toContain("1 of 3");
    expect(boxes[0]).toContain("On pace");
    expect(boxes[1]).toContain("2 of 3");
    expect(boxes[1]).toContain("1 missed");
    expect(boxes[2]).toContain("2 wk");
    expect(boxes[2]).toContain("Not yet");
    const rows = all(card, ".social-row");
    expect(rows).toHaveLength(3);
    expect(rows[0].querySelector(".social-title")?.textContent).toBe("Expert post");
    expect(rows[1].textContent).toContain("Short: Quick one");
    expect(rows[1].textContent).toContain("No draft");
    expect(rows[1].textContent).toContain("optional");
    expect(rows[1].textContent).toContain("HC");
    expect(rows[2].textContent).toContain("Video: Fabric <b>tips</b>");
    expect(rows[2].querySelector("b")).toBeNull();
    expect(all(card, ".social-actions")).toHaveLength(1);
    expect(q(card, ".social-footer")?.textContent).toContain("On time 80% · 30 d");
    expect(q(card, ".social-footer")?.textContent).toContain("YouTube read 30 min ago");
    expect(getTile("social")?.value).toBe("2 due today");
  });

  it("says what it waits for when nothing is planned", async () => {
    const empty = view({
      slots: [],
      uploads_meta: { ae: { read_at: null, ok: false, error: null, count: 0 }, hc: { read_at: null, ok: false, error: null, count: 0 } },
    });
    const { context } = stubContext(() => ok(empty));
    const card = mount(context);
    await flush();
    expect(q(card, ".social-empty")?.textContent).toBe("Nothing planned yet. Waiting for the first YouTube read.");
  });

  it("shows a refusal as a notice and the Admins tile", async () => {
    const { context } = stubContext(() => ({ ok: false, status: 403, body: { error: { code: "admin_only", message: "Only an admin" } } }));
    const card = mount(context);
    await flush();
    expect(q(card, ".notice-error")?.textContent).toContain("could not be read");
    expect(getTile("social")?.value).toBe("Admins");
  });

  // A preview that fills the server's 160-character cut is not the whole text.
  const longPreview = "p".repeat(160);
  const withDraft = (text: string, rev = "rev00001") => (call: { method: string; path: string }) =>
    call.path === "/v1/ae/social/slot"
      ? ok({ ...slot("li-expert-2026w46-1", 3), draft_rev: rev, draft: { text, topic: null, by: "auto", edited: false } })
      : ok(view({ slots: view().slots.map((s) => (s.id === "li-expert-2026w46-1" ? { ...s, draft_preview: longPreview, draft_rev: rev } : s)) }));

  it("fetches the first post's full draft ahead, and Copy copies it inside the tap", async () => {
    const { context, request } = stubContext(withDraft("The full draft"));
    const card = mount(context);
    await flush();
    expect(request).toHaveBeenCalledWith({ method: "POST", path: "/v1/ae/social/slot", body: { id: "li-expert-2026w46-1" } });
    const copy = button(card, "Copy");
    copy.click();
    // No await between the tap and the clipboard write: iOS drops one that starts later.
    expect(copied.copyText).toHaveBeenCalledWith("The full draft");
    await flush();
    expect(copy.textContent).toBe("Copied");
  });

  it("Copy opens the editor instead when the full draft is not loaded yet", async () => {
    let release: (reply: Reply) => void = () => undefined;
    const slow = new Promise<Reply>((resolve) => (release = resolve));
    const { context, request } = stubContext(((call: { method: string; path: string }) =>
      call.path === "/v1/ae/social/slot"
        ? slow
        : ok(view({ slots: view().slots.map((s) => (s.id === "li-expert-2026w46-1" ? { ...s, draft_preview: longPreview } : s)) }))) as never);
    const card = mount(context);
    await flush();
    button(card, "Copy").click();
    expect(copied.copyText).not.toHaveBeenCalled();
    await flush();
    expect(document.querySelector(".sv-dialog-card")).not.toBeNull();
    expect(request).toHaveBeenCalledWith({ method: "POST", path: "/v1/ae/social/slot", body: { id: "li-expert-2026w46-1" } });
    release(ok({ ...slot("li-expert-2026w46-1", 3), draft: { text: "late", topic: null, by: "auto", edited: false } }));
    await flush();
  });

  it("Copy is disabled with a reason when the first post has no draft, but Open is not", async () => {
    const v = view({ slots: [slot("li-short-hc-bbbbbbbbbbb", 1, { kind: "short", channel: "hc", title: "Q", has_draft: false, draft_preview: null })] });
    const { context } = stubContext(() => ok(v));
    const card = mount(context);
    await flush();
    const copy = button(card, "Copy");
    expect(copy.disabled).toBe(true);
    expect(copy.title).toBe("No draft yet");
    expect(button(card, "Open").disabled).toBe(false);
  });

  it("Open with no draft opens the editor on that post, with its starter text", async () => {
    const short = slot("li-short-hc-bbbbbbbbbbb", 1, {
      kind: "short",
      channel: "hc",
      title: "Q",
      has_draft: false,
      draft_preview: null,
      url: "https://www.youtube.com/shorts/bbbbbbbbbbb",
    });
    const { context, openExternal } = stubContext(({ path }) =>
      path === "/v1/ae/social/slot" ? ok({ ...short, draft: null }) : ok(view({ slots: [short] })),
    );
    const card = mount(context);
    await flush();
    button(card, "Open").click();
    await flush();
    const area = document.querySelector<HTMLTextAreaElement>(".sv-ed-text");
    // Line 1 of a LinkedIn starter is bold by default (2026-11-07); the link below it is plain.
    expect(area?.value).toBe(boldFirstLine("New Short on the Second Co channel: Q.\n\nhttps://www.youtube.com/shorts/bbbbbbbbbbb"));
    expect(unboldFirstLine(area?.value ?? "")).toBe("New Short on the Second Co channel: Q.\n\nhttps://www.youtube.com/shorts/bbbbbbbbbbb");
    expect(document.querySelector(".sv-ed-meta")?.textContent).toContain("Starter text");
    expect(openExternal).not.toHaveBeenCalled();
  });

  it("Open copies inside the tap, then opens LinkedIn with the text", async () => {
    const { context, openExternal } = stubContext(withDraft("Words & more"));
    const card = mount(context);
    await flush();
    button(card, "Open").click();
    expect(copied.copyText).toHaveBeenCalledWith("Words & more");
    await flush();
    expect(openExternal).toHaveBeenCalledWith("https://www.linkedin.com/feed/?shareActive=true&text=Words%20%26%20more");
    expect(copied.copyText.mock.invocationCallOrder[0]).toBeLessThan(openExternal.mock.invocationCallOrder[0]);
  });

  it("Open on a touch device shares through the share sheet, copying first, instead of opening the link", async () => {
    window.matchMedia = ((query: string) => ({
      matches: query.includes("pointer: coarse"),
      media: query,
      addEventListener: () => undefined,
      removeEventListener: () => undefined,
    })) as never;
    try {
      const { context, openExternal, share } = stubContext(withDraft("Words & more"));
      const card = mount(context);
      await flush();
      const open = button(card, "Open");
      open.click();
      expect(copied.copyText).toHaveBeenCalledWith("Words & more");
      const real = () => share.mock.calls.filter((c) => !("probe" in c[0]));
      expect(real()).toHaveLength(1); // inside the tap (a probe ran earlier and shares nothing)
      const at = share.mock.calls.findIndex((c) => !("probe" in c[0]));
      expect(copied.copyText.mock.invocationCallOrder[0]).toBeLessThan(share.mock.invocationCallOrder[at]);
      await flush();
      expect(real()[0][0].text).toBe("Words & more");
      expect(openExternal).not.toHaveBeenCalled();
      expect(open.textContent).toBe("Shared");
    } finally {
      delete (window as { matchMedia?: unknown }).matchMedia;
    }
  });

  it("an edit past the preview is seen: Copy uses the new text, not the cached old one", async () => {
    const head = "h".repeat(160);
    const state = { rev: "rev00001", text: `${head}${"o".repeat(440)}` };
    const mk = () => view({ slots: view().slots.map((s) => (s.id === "li-expert-2026w46-1" ? { ...s, draft_preview: head, draft_rev: state.rev } : s)) });
    const { context } = stubContext(({ path }) =>
      path === "/v1/ae/social/slot"
        ? ok({ ...slot("li-expert-2026w46-1", 3), draft_rev: state.rev, draft: { text: state.text, topic: null, by: "reid", edited: true } })
        : ok(mk()),
    );
    const card = mount(context);
    await flush();
    button(card, "Copy").click();
    expect(copied.copyText).toHaveBeenLastCalledWith(`${head}${"o".repeat(440)}`);
    // The tail is edited and saved elsewhere: same preview, new revision.
    state.rev = "rev00002";
    state.text = `${head}${"n".repeat(440)}`;
    window.dispatchEvent(new Event("focus"));
    await flush();
    await flush();
    copied.copyText.mockClear();
    button(card, "Copy").click();
    expect(copied.copyText).toHaveBeenCalledTimes(1);
    expect(copied.copyText).toHaveBeenCalledWith(`${head}${"n".repeat(440)}`);
  });

  it("Open arms 'Did you post it?' before the host call resolves, so the app switch is not missed", async () => {
    const { context, openExternal } = stubContext(withDraft("Words"));
    let release: () => void = () => undefined;
    openExternal.mockImplementationOnce(() => new Promise<undefined>((resolve) => (release = () => resolve(undefined))));
    const card = mount(context);
    await flush();
    button(card, "Open").click();
    await vi.advanceTimersByTimeAsync(3_000);
    document.dispatchEvent(new Event("visibilitychange"));
    await flush();
    expect(q(card, ".sv-banner")?.textContent).toContain("Back from LinkedIn");
    release();
    await flush();
  });

  it("Open forgets the tap when the host refuses the link", async () => {
    const { context, openExternal } = stubContext(withDraft("Words"));
    openExternal.mockRejectedValueOnce(new Error("not allowed"));
    const card = mount(context);
    await flush();
    button(card, "Open").click();
    await flush();
    await vi.advanceTimersByTimeAsync(3_000);
    document.dispatchEvent(new Event("visibilitychange"));
    await flush();
    expect(q(card, ".sv-banner")).toBeNull();
  });

  it("Open says so when the host refuses the link, and the draft stays copied", async () => {
    const { context, openExternal } = stubContext(withDraft("Words"));
    openExternal.mockRejectedValueOnce(new Error("not allowed"));
    const card = mount(context);
    await flush();
    button(card, "Open").click();
    await flush();
    expect(copied.copyText).toHaveBeenCalledWith("Words");
    expect(q(card, ".notice")?.textContent).toContain("Draft copied");
    expect(q(card, ".notice")?.textContent).toContain("linkedin.com/feed");
  });

  it("Posted marks the post and reads the card again", async () => {
    let posted = false;
    const { context, request } = stubContext(({ method, path }) => {
      if (`${method} ${path}` === "POST /v1/ae/social/posted") {
        posted = true;
        return ok({ ok: true });
      }
      const v = view();
      if (posted) {
        v.slots = v.slots.filter((s) => s.id !== "li-expert-2026w46-1");
        v.score.week_posted = 2;
      }
      return ok(v);
    });
    const card = mount(context);
    await flush();
    button(card, "Posted").click();
    await flush();
    await flush();
    expect(request).toHaveBeenCalledWith({ method: "POST", path: "/v1/ae/social/posted", body: { id: "li-expert-2026w46-1" } });
    expect(all(card, ".social-stat")[0].textContent).toContain("2 of 3");
    expect(all(card, ".social-row")[0].textContent).toContain("Short: Quick one");
  });

  it("shows a failed action", async () => {
    const { context } = stubContext(({ path }) =>
      path === "/v1/ae/social/posted" ? { ok: false, status: 404, body: { error: { code: "not_found", message: "no slot" } } } : ok(view()),
    );
    const card = mount(context);
    await flush();
    button(card, "Posted").click();
    await flush();
    await flush();
    expect(q(card, ".notice-error")?.textContent).toContain("That did not go through");
  });

  it("reads again on focus and does not draw after it is deactivated", async () => {
    const { context, request } = stubContext(() => ok(view()));
    const card = mount(context);
    await flush();
    window.dispatchEvent(new Event("focus"));
    await flush();
    expect(request).toHaveBeenCalledTimes(2);
    deactivateSocial();
    window.dispatchEvent(new Event("focus"));
    await flush();
    expect(request).toHaveBeenCalledTimes(2);
    expect(card.isConnected).toBe(true);
  });
});

describe("part 2 additions to the card", () => {
  it("writes the apostrophe as %27 so the host's allowlist round trip holds", () => {
    expect(openUrl(slot("a", 1), view(), "it's")).toBe("https://www.linkedin.com/feed/?shareActive=true&text=it%27s");
    expect(openUrl(slot("a", 1), view(), "")).toBe("https://www.linkedin.com/feed/?shareActive=true");
    expect(openUrl(slot("a", 1), view(), "\ud800")).toBe("https://www.linkedin.com/feed/?shareActive=true");
  });

  it("says First week instead of On pace or Behind, in the pill, the boxes and the tile", async () => {
    const first = view({ slots: [], score: { ...view().score, first_week: true, on_pace: false } });
    expect(headerPill(first, NOW)).toEqual({ text: "First week", tone: "ok" });
    expect(socialTile(first, null, NOW)).toMatchObject({ brief: "First week", health: "ok" });
    const { context } = stubContext(() => ok(first));
    const card = mount(context);
    await flush();
    const boxes = all(card, ".social-stat").map((b) => b.textContent);
    expect(boxes[0]).toContain("First week");
    expect(boxes[0]).not.toContain("Behind");
  });

  it("gives the tile a brief of three to five words for the Ship pill", () => {
    const words = (text: string | undefined) => (text ?? "").split(/\s+/).length;
    const overdue = view({ slots: [slot("a", -5), slot("b", -2)] });
    expect(socialTile(overdue, null, NOW).brief).toBe("2 posts overdue");
    expect(socialTile(view({ slots: [slot("a", -5)] }), null, NOW).brief).toBe("1 post overdue");
    expect(socialTile(view({ slots: [slot("a", 2)] }), null, NOW).brief).toBe("1 due today");
    const calm = socialTile(view({ slots: [slot("a", 50)] }), null, NOW);
    expect(calm.brief).toBe("On pace, streak 2 wk");
    expect(words(calm.brief)).toBeLessThanOrEqual(5);
    const behind = view({ slots: [], score: { ...view().score, on_pace: false } });
    expect(socialTile(behind, null, NOW).brief).toBe("Behind this week");
  });

  it("pops out the full view, and keeps it live when the card reads again", async () => {
    const { context, request } = stubContext(() => ok(view()));
    const card = mount(context);
    const pop = q(card, ".social-popout") as HTMLButtonElement;
    expect(pop.disabled).toBe(true);
    await flush();
    expect(pop.disabled).toBe(false);
    pop.click();
    const dialog = document.querySelector(".sv-dialog-card");
    expect(dialog).not.toBeNull();
    expect(dialog?.querySelectorAll(".sv-seg.is-on")[0].textContent).toBe("Week");
    expect(dialog?.textContent).toContain("Not posted yet (4)");
    expect(dialog?.querySelector(".sv-qrow")?.textContent).toContain("Expert post");
    request.mockImplementation(async () => ok(view({ slots: [slot("li-expert-2026w46-1", 3)] })));
    window.dispatchEvent(new Event("focus"));
    await flush();
    expect(document.querySelector(".sv-dialog-card")?.textContent).toContain("Not posted yet (1)");
    deactivateSocial();
    expect(document.querySelector(".sv-dialog-card")).toBeNull();
  });
});
