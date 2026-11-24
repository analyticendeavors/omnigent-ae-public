// The picture picker on a tool or expert post: tiles in rank order, a pick saves at once and the
// share sheet takes it, None shares the text only, a refused pick goes back (2026-11-15).

import type { ExtensionContext } from "@omnigent/extension-sdk";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { SocialMadeVisual, SocialSlot, SocialView } from "../src/api";
import { resetEditorStash } from "../src/socialEditor";
import { SETTLE_MS, pickLabel } from "../src/socialPicker";
import { resetPosted } from "../src/socialPosted";
import { resetShareProbe } from "../src/socialShare";
import { closeSocialView, openSocialView } from "../src/socialView";
import { resetVisuals } from "../src/socialVisual";
import { resetTiles } from "../src/tiles";

const sdk = vi.hoisted(() => ({ defineExtension: vi.fn() }));
vi.mock("@omnigent/extension-sdk", () => sdk);

const NOW = Date.parse("2026-11-11T20:00:00Z");
const HOUR = 3_600_000;

const MADE: SocialMadeVisual[] = [
  { id: "c1", kind: "screenshot", label: "A real skin", key: "1111111111111111" },
  { id: "c2", kind: "picture", label: "Re-skin and contrast", key: "2222222222222222" },
  { id: "c3", kind: "diagram", label: "Contrast while you build", key: "3333333333333333" },
  { id: "c4", kind: "diagram", label: "13 skins and tools", key: "4444444444444444" },
  { id: "c5", kind: "picture", label: "Swatches", key: "5555555555555555" },
];

const toolSlot = (fields: Partial<SocialSlot> = {}): SocialSlot => ({
  id: "li-tool-2026w47",
  kind: "tool",
  platform: "linkedin",
  channel: null,
  anchor: "skins",
  due: new Date(NOW + 2 * HOUR).toISOString(),
  skippable: false,
  state: "planned",
  rescheduled: false,
  orphan: false,
  draft_error: null,
  title: null,
  tool: "Skins tool",
  url: "https://tools.example.com/skins",
  has_draft: true,
  draft_preview: "Skins tool is coming soon.",
  topic: null,
  draft_by: "auto",
  posted: null,
  visual: { kind: "made", key: "c1:1111111111111111" },
  visuals: MADE,
  visual_choice: "c1",
  visual_choice_by: "auto",
  ...fields,
});

const view = (slots: SocialSlot[]): SocialView =>
  ({
    rules: { tz: "America/Los_Angeles", gap_days: 7, alarm_hhmm: "09:00" },
    tools: [],
    uploads_meta: {},
    gaps: {},
    score: {
      week_posted: 0, week_target: 3, on_pace: true, promoted: 0, promoted_of: 0, missed: 0, streak: 0,
      best_streak: 0, on_time_pct: null, on_time_of: 0, community_30d: { posted: 0, total: 0 }, weeks: [],
      first_week: false, unposted: [], unposted_total: 0, rotation: [], pace: {},
    },
    slots,
    slot_count: slots.length,
    truncated: false,
    channel_ids: {},
    channel_names: {},
  }) as unknown as SocialView;

function media(touch: boolean) {
  window.matchMedia = ((query: string) => ({
    matches: query.includes("pointer: coarse") ? touch : true,
    media: query,
    addEventListener: () => undefined,
    removeEventListener: () => undefined,
  })) as never;
}

function stub(slot: SocialSlot, refuse = false) {
  const request = vi.fn(async (call: { path: string; body?: unknown }) => {
    let body: unknown = view([slot]);
    if (call.path === "/v1/ae/social/slot") {
      body = { ...structuredClone(slot), draft: { text: "Skins tool is coming soon.", topic: null, by: "auto", edited: false } };
    } else if (call.path.startsWith("/v1/ae/social/visual/")) {
      body = { type: "image/jpeg", size: 9, parts: 1, part: 0, data: btoa(`JPEG ${call.path}`) };
    } else if (call.path === "/v1/ae/social/visual-choice" && refuse) {
      return { ok: false, status: 409, body: { error: { code: "conflict", message: "closed" } } };
    } else if (call.path === "/v1/ae/social/visual-choice") {
      body = { ok: true };
    }
    return { ok: true, status: 200, body };
  });
  const share = vi.fn(async (options: { probe?: true }) => (options.probe ? { available: true } : { shared: true }));
  const context = {
    view: "fabric",
    server: { request },
    navigation: { openExternal: vi.fn(async () => undefined), share },
    storage: { user: { get: vi.fn(async () => undefined), set: vi.fn(async () => undefined) } },
  } as unknown as ExtensionContext;
  return { context, request, share };
}

const flush = async () => {
  for (let i = 0; i < 4; i += 1) await vi.advanceTimersByTimeAsync(0);
};
/** Past the settle delay, then let the save and the loads finish. */
const settle = async () => {
  await vi.advanceTimersByTimeAsync(SETTLE_MS);
  await flush();
};
const choices = (request: { mock: { calls: Array<[{ path: string; body?: unknown }]> } }) =>
  request.mock.calls.filter((c) => c[0].path === "/v1/ae/social/visual-choice").map((c) => c[0].body);
const q = (sel: string) => document.querySelector<HTMLElement>(sel);
const all = (sel: string) => [...document.querySelectorAll<HTMLElement>(sel)];
const paths = (request: { mock: { calls: Array<[{ path: string }]> } }) => request.mock.calls.map((c) => c[0].path);

async function open(slot: SocialSlot, refuse = false) {
  const s = stub(slot, refuse);
  openSocialView(s.context, view([slot]), null, vi.fn(async () => undefined), slot.id);
  await flush();
  return s;
}

beforeEach(() => {
  resetEditorStash();
  resetTiles();
  resetPosted();
  resetVisuals();
  resetShareProbe();
  let n = 0;
  URL.createObjectURL = vi.fn(() => `blob:picture-${(n += 1)}`);
  document.body.replaceChildren();
  vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout", "Date"] });
  vi.setSystemTime(NOW);
  media(false);
});

afterEach(() => {
  closeSocialView();
  vi.useRealTimers();
  delete (window as { matchMedia?: unknown }).matchMedia;
});

describe("pickLabel", () => {
  it("names the rank, the kind, the label and who picked", () => {
    expect(pickLabel(toolSlot())).toBe("1 of 5 · Screenshot: A real skin · the drafter's pick");
    expect(pickLabel(toolSlot({ visual_choice: "c3", visual_choice_by: "reid" }))).toBe(
      "3 of 5 · Diagram: Contrast while you build · your pick",
    );
    expect(pickLabel(toolSlot({ visual_choice: "none" }))).toBe("No picture: this post shares the text only.");
  });
});

describe("the picker", () => {
  it("shows every candidate in rank order plus None, the first checked, as one radio group", async () => {
    const { request } = await open(toolSlot());
    const group = q(".sv-pick-strip");
    expect(group?.getAttribute("role")).toBe("radiogroup");
    const tiles = all(".sv-pick-tile");
    expect(tiles.map((t) => t.getAttribute("aria-label"))).toEqual([
      "1 of 5: Screenshot, A real skin",
      "2 of 5: Picture, Re-skin and contrast",
      "3 of 5: Diagram, Contrast while you build",
      "4 of 5: Diagram, 13 skins and tools",
      "5 of 5: Picture, Swatches",
      "No picture",
    ]);
    expect(tiles.map((t) => t.getAttribute("aria-checked"))).toEqual(["true", "false", "false", "false", "false", "false"]);
    expect(tiles.map((t) => t.tabIndex)).toEqual([0, -1, -1, -1, -1, -1]);
    expect(q(".sv-pick-label")?.textContent).toBe("1 of 5 · Screenshot: A real skin · the drafter's pick");
    // The chosen picture is asked for by its id, the tiles as previews.
    expect(paths(request)).toContain("/v1/ae/social/visual/li-tool-2026w47/0?c=c1");
    expect(paths(request)).toContain("/v1/ae/social/visual/li-tool-2026w47/0?c=c4&preview=true");
    expect(q(".sv-pick-img")?.tagName).toBe("IMG");
    expect(q(".sv-ed-vis")).toBeNull(); // the old single-picture row is not drawn
  });

  it("a tap picks, saves at once and loads that picture", async () => {
    const { request } = await open(toolSlot());
    all(".sv-pick-tile")[2].click();
    await settle();
    const choice = request.mock.calls.find((c) => c[0].path === "/v1/ae/social/visual-choice");
    expect(choice?.[0].body).toEqual({ id: "li-tool-2026w47", choice: "c3" });
    expect(all(".sv-pick-tile")[2].getAttribute("aria-checked")).toBe("true");
    expect(q(".sv-pick-label")?.textContent).toBe("3 of 5 · Diagram: Contrast while you build · your pick");
    expect(paths(request)).toContain("/v1/ae/social/visual/li-tool-2026w47/0?c=c3");
  });

  it("arrow keys move the pick like a radio group", async () => {
    const { request } = await open(toolSlot());
    const first = all(".sv-pick-tile")[0];
    first.dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowLeft", bubbles: true }));
    await settle();
    // Left from the first wraps to None.
    expect(all(".sv-pick-tile")[5].getAttribute("aria-checked")).toBe("true");
    expect(document.activeElement).toBe(all(".sv-pick-tile")[5]);
    const sent = request.mock.calls.filter((c) => c[0].path === "/v1/ae/social/visual-choice").map((c) => c[0].body);
    expect(sent).toEqual([{ id: "li-tool-2026w47", choice: "none" }]);
  });

  it("Share takes the picture just picked", async () => {
    media(true);
    const { share } = await open(toolSlot());
    all(".sv-pick-tile")[1].click();
    await settle();
    const shareButton = [...document.querySelectorAll<HTMLButtonElement>("button")].find((b) => b.textContent === "Share to LinkedIn");
    expect(shareButton?.disabled).toBe(false);
    shareButton?.click();
    await flush();
    const sent = share.mock.calls.map((c) => c[0] as { probe?: true; files?: File[] }).filter((o) => !o.probe);
    expect(sent).toHaveLength(1);
    expect(await sent[0].files?.[0].text()).toBe("JPEG /v1/ae/social/visual/li-tool-2026w47/0?c=c2");
  });

  it("None shares the text only and says so", async () => {
    media(true);
    await open(toolSlot());
    all(".sv-pick-tile")[5].click();
    await settle();
    expect(q(".sv-pick-img")?.textContent).toBe("No picture");
    expect(q(".sv-pick-label")?.textContent).toBe("No picture: this post shares the text only.");
    expect(q(".sv-ed-hint")?.textContent).toBe("Opens your phone's share sheet with the text");
  });

  it("a refused pick goes back and says why", async () => {
    await open(toolSlot(), true);
    all(".sv-pick-tile")[1].click();
    await settle();
    expect(all(".sv-pick-tile")[0].getAttribute("aria-checked")).toBe("true");
    expect(q(".sv-pick-label")?.textContent).toBe("1 of 5 · Screenshot: A real skin · the drafter's pick");
    expect(q(".sv-ed-status")?.textContent).toContain("Could not save the picture pick");
  });

  it("quick picks send one save, for the last one, and Share waits until they settle", async () => {
    media(true);
    const { request } = await open(toolSlot());
    all(".sv-pick-tile")[1].click();
    all(".sv-pick-tile")[2].click();
    all(".sv-pick-tile")[3].click();
    await flush();
    const share = [...document.querySelectorAll<HTMLButtonElement>("button")].find((b) => b.textContent?.includes("image") || b.textContent === "Share to LinkedIn");
    expect(share?.textContent).toBe("Loading image...");
    expect(share?.disabled).toBe(true);
    expect(choices(request)).toEqual([]);
    expect(paths(request)).not.toContain("/v1/ae/social/visual/li-tool-2026w47/0?c=c2");
    await settle();
    expect(choices(request)).toEqual([{ id: "li-tool-2026w47", choice: "c4" }]);
    expect(share?.textContent).toBe("Share to LinkedIn");
    expect(share?.disabled).toBe(false);
  });

  it("a saved pick reads the card again so its other views share it", async () => {
    const s = stub(toolSlot());
    const changed = vi.fn(async () => undefined);
    openSocialView(s.context, view([toolSlot()]), null, changed, "li-tool-2026w47");
    await flush();
    expect(changed).not.toHaveBeenCalled();
    all(".sv-pick-tile")[1].click();
    await settle();
    expect(changed).toHaveBeenCalledTimes(1);
  });

  it("a post picked as none opens with None checked and no picture", async () => {
    const { visual: _drop, ...rest } = toolSlot();
    await open({ ...rest, visual_choice: "none", visual_choice_by: "reid", visual_note: "No picture: you picked none." });
    expect(all(".sv-pick-tile")[5].getAttribute("aria-checked")).toBe("true");
    expect(q(".sv-pick-img")?.textContent).toBe("No picture");
  });
});
