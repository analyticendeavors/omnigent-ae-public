// Share to LinkedIn on a phone, the picture, the bold first line, the one-line meta and the
// copy order (2026-11-07).

import type { ExtensionContext } from "@omnigent/extension-sdk";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { SocialSlot, SocialView } from "../src/api";
import { copyText } from "../src/lot";
import { LIMIT, countChars, resetEditorStash } from "../src/socialEditor";
import { noteReturn, resetPosted } from "../src/socialPosted";
import { resetShareProbe } from "../src/socialShare";
import { closeSocialView, openSocialView } from "../src/socialView";
import { IMAGE_WAIT_MS } from "../src/socialEditor";
import { resetVisuals } from "../src/socialVisual";
import { boldFirstLine, isFirstLineBold, unboldFirstLine } from "../src/socialWords";
import { resetTiles } from "../src/tiles";

const sdk = vi.hoisted(() => ({ defineExtension: vi.fn() }));
vi.mock("@omnigent/extension-sdk", () => sdk);

const NOW = Date.parse("2026-11-11T20:00:00Z");
const HOUR = 3_600_000;
const SHORT = "https://www.youtube.com/shorts/bbbbbbbbbbb";

const target = (fields: Partial<SocialSlot> = {}): SocialSlot => ({
  id: "li-short-ae-bbbbbbbbbbb",
  kind: "short",
  platform: "linkedin",
  channel: "ae",
  anchor: "bbbbbbbbbbb",
  due: new Date(NOW + 2 * HOUR).toISOString(),
  skippable: false,
  state: "planned",
  rescheduled: false,
  orphan: false,
  draft_error: null,
  title: "My Short",
  tool: null,
  url: SHORT,
  has_draft: false,
  draft_preview: null,
  topic: null,
  draft_by: null,
  posted: null,
  visual: { kind: "thumbnail" },
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
    channel_ids: { ae: "UCexampleChannelOne" },
    channel_names: { ae: "Analytic Endeavors" },
  }) as unknown as SocialView;

/** matchMedia: `(pointer: coarse)` is `touch`; the phone query is `phone`. */
function media(touch: boolean, phone = true) {
  window.matchMedia = ((query: string) => ({
    matches: query.includes("pointer: coarse") ? touch : phone,
    media: query,
    addEventListener: () => undefined,
    removeEventListener: () => undefined,
  })) as never;
}

const JPEG_B64 = btoa("JPEGBYTES");
function stub(slot: SocialSlot, draft: string | null = null) {
  const request = vi.fn(async (call: { path: string }) => {
    let body: unknown = view([slot]);
    if (call.path === "/v1/ae/social/slot") {
      body = { ...slot, draft: draft && { text: draft, topic: null, by: "reid", edited: true }, has_draft: draft !== null };
    } else if (call.path.startsWith("/v1/ae/social/visual/")) {
      body = { type: "image/jpeg", size: 9, parts: 1, part: 0, data: JPEG_B64 };
    }
    return { ok: true, status: 200, body };
  });
  const openExternal = vi.fn(async (_url: string) => undefined);
  type Options = { text?: string; files?: File[]; probe?: true };
  type Answer = { shared?: true; files_dropped?: true; available?: boolean };
  // A probe says a share sheet exists; anything else is a share that goes through.
  const share = vi.fn(async (options: Options): Promise<Answer> => (options.probe ? { available: true } : { shared: true }));
  const context = {
    view: "fabric",
    server: { request },
    navigation: { openExternal, share },
    storage: { user: { get: vi.fn(async () => undefined), set: vi.fn(async () => undefined) } },
  } as unknown as ExtensionContext;
  return { context, request, openExternal, share };
}

const flush = () => vi.advanceTimersByTimeAsync(0);
const q = (sel: string) => document.querySelector<HTMLElement>(sel);
const all = (sel: string) => [...document.querySelectorAll<HTMLElement>(sel)];
const btn = (label: string) => all("button").find((b) => b.textContent === label) as HTMLButtonElement;

/** The calls that really shared (a probe shares nothing). */
const shares = (share: { mock: { calls: Array<[{ probe?: true; text?: string; files?: File[] }]> } }) =>
  share.mock.calls.map((c) => c[0]).filter((o) => !o.probe);

async function openEditor(slot: SocialSlot, draft: string | null = null) {
  const s = stub(slot, draft);
  openSocialView(s.context, view([slot]), null, vi.fn(async () => undefined), slot.id);
  await flush();
  await flush();
  return s;
}

beforeEach(() => {
  resetEditorStash();
  resetTiles();
  resetPosted();
  resetVisuals();
  resetShareProbe();
  URL.createObjectURL = vi.fn(() => "blob:picture");
  document.body.replaceChildren();
  vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout", "Date"] });
  vi.setSystemTime(NOW);
});

afterEach(() => {
  closeSocialView();
  vi.useRealTimers();
  delete (window as { matchMedia?: unknown }).matchMedia;
});

describe("copyText", () => {
  afterEach(() => {
    Reflect.deleteProperty(document, "execCommand");
    Reflect.deleteProperty(navigator, "clipboard");
  });

  it("does the selection copy synchronously, before any await, and the clipboard API only if it failed", async () => {
    const order: string[] = [];
    document.execCommand = vi.fn(() => {
      order.push("exec");
      return true;
    });
    const write = vi.fn(async () => {
      order.push("clipboard");
    });
    Object.defineProperty(navigator, "clipboard", { value: { writeText: write }, configurable: true });
    const done = copyText("hello"); // not awaited: the selection copy has already run
    expect(document.execCommand).toHaveBeenCalledWith("copy");
    expect(order).toEqual(["exec"]);
    await expect(done).resolves.toBe(true);
    expect(write).not.toHaveBeenCalled();

    document.execCommand = vi.fn(() => false);
    await expect(copyText("again")).resolves.toBe(true);
    expect(write).toHaveBeenCalledWith("again");
    write.mockRejectedValueOnce(new Error("denied"));
    await expect(copyText("third")).resolves.toBe(false);
  });
});

describe("the bold first line", () => {
  it("maps A-Z, a-z and 0-9 on line 1 to Mathematical Sans-Serif Bold and back", () => {
    expect(boldFirstLine("AZaz09")).toBe("\u{1D5D4}\u{1D5ED}\u{1D5EE}\u{1D607}\u{1D7EC}\u{1D7F5}");
    expect(unboldFirstLine(boldFirstLine("Hello, World 2026!"))).toBe("Hello, World 2026!");
    const text = "Line one\n\nLine two https://youtu.be/aaaaaaaaaaa";
    expect(boldFirstLine(text).split("\n").slice(1).join("\n")).toBe("\nLine two https://youtu.be/aaaaaaaaaaa");
    expect(boldFirstLine("é ü stay")).toContain("é ü ");
    // A link on line 1 is never touched (a bold-lettered link would not open).
    expect(boldFirstLine("Tool: https://a.test/x")).toBe(`${boldFirstLine("Tool:")} https://a.test/x`);
    expect(unboldFirstLine("\u{1D5D4}\nkeep \u{1D5EE}")).toBe("A\nkeep \u{1D5EE}");
  });

  it("knows when line 1 is bold", () => {
    expect(isFirstLineBold(boldFirstLine("Hello there\nrest"))).toBe(true);
    expect(isFirstLineBold("Hello there")).toBe(false);
    expect(isFirstLineBold(`${boldFirstLine("Hi")} there`)).toBe(false); // plain letters mixed in
    expect(isFirstLineBold("")).toBe(false);
    expect(isFirstLineBold("123 !!!")).toBe(false);
    expect(isFirstLineBold(boldFirstLine("Tool: https://a.test/x"))).toBe(true);
  });
});

describe("the editor", () => {
  it("starts a LinkedIn post with line 1 bold, counts code points, and the chip rewrites line 1", async () => {
    media(false);
    await openEditor(target());
    const area = q(".sv-ed-text") as HTMLTextAreaElement;
    const plain = `New Short on the Analytic Endeavors channel: My Short.\n\n${SHORT}`;
    expect(area.value).toBe(boldFirstLine(plain));
    const chip = q(".sv-bold") as HTMLButtonElement;
    expect(chip.getAttribute("aria-pressed")).toBe("true");
    expect(q(".sv-ed-count")?.textContent).toBe(`${countChars(area.value)} / ${LIMIT.toLocaleString("en-US")}`);
    expect(countChars(area.value)).toBeLessThan(area.value.length); // astral letters count once
    chip.click();
    expect(area.value).toBe(plain);
    expect(chip.getAttribute("aria-pressed")).toBe("false");
    chip.click();
    expect(area.value).toBe(boldFirstLine(plain));
    expect(chip.getAttribute("aria-pressed")).toBe("true");
  });

  it("leaves a saved draft as it is and offers the chip off", async () => {
    media(false);
    await openEditor(target({ has_draft: true }), "A saved draft\n\nbody");
    expect((q(".sv-ed-text") as HTMLTextAreaElement).value).toBe("A saved draft\n\nbody");
    expect(q(".sv-bold")?.getAttribute("aria-pressed")).toBe("false");
  });

  it("keeps a Community post plain and hides the chip", async () => {
    media(false);
    await openEditor(target({ platform: "yt_community", kind: "video", visual: undefined }));
    expect((q(".sv-ed-text") as HTMLTextAreaElement).value).toBe(`New video: My Short\n\n${SHORT}`);
    expect((q(".sv-bold") as HTMLElement).hidden).toBe(true);
  });

  it("puts due time, channel, count and the bold chip on one line", async () => {
    media(false);
    await openEditor(target());
    const meta = q(".sv-ed-meta") as HTMLElement;
    const line = [...meta.children].filter((c) => !c.classList.contains("sv-ed-dirty"));
    expect(line.map((c) => c.className)).toEqual(["sv-ed-info", "sv-bold"]);
    expect(q(".sv-ed-info")?.textContent).toMatch(/^Due Today \d+:00 [AP]M · AE · [\d,]+ \/ 3,000$/);
    expect(q(".sv-ed-head .sv-ed-sub")).toBeNull(); // no second line under the title
    expect(q(".sv-ed-head .sv-ed-title")).not.toBeNull();
  });

  it("shows the picture as a blob: image above the Post bar, loaded when the editor opens", async () => {
    media(true);
    const { request } = await openEditor(target());
    expect(request.mock.calls.some(([c]) => c.path === "/v1/ae/social/visual/li-short-ae-bbbbbbbbbbb/0")).toBe(true);
    const img = q(".sv-ed-vis img") as HTMLImageElement;
    expect(img.getAttribute("src")).toBe("blob:picture");
    expect(q(".sv-ed-vis")?.textContent).toContain("Visual: the video's thumbnail");
    expect(q(".sv-ed-vis")?.textContent).toContain("Goes into the share sheet with the text. Long-press to save it.");
    const host = q(".sv-editor") as HTMLElement;
    const kids = [...host.children];
    expect(kids.indexOf(q(".sv-ed-vis") as HTMLElement)).toBe(kids.indexOf(q(".sv-ed-bar") as HTMLElement) - 1);
  });

  it("says why there is no picture, and shows nothing for an expert post", async () => {
    media(true);
    await openEditor(target({ visual: undefined, visual_note: "The thumbnail shows once the video is public." }));
    expect(q(".sv-ed-vis-note")?.textContent).toBe("The thumbnail shows once the video is public.");
    expect(q(".sv-ed-vis")).toBeNull();
    closeSocialView();
    await openEditor(target({ kind: "expert", visual: undefined, title: null, url: null }));
    expect(q(".sv-ed-vis")).toBeNull();
    expect(q(".sv-ed-vis-note")).toBeNull();
  });

  it("says the picture is unavailable when the server cannot fetch it", async () => {
    media(true);
    const slot = target();
    const s = stub(slot);
    s.request.mockImplementation(async (call: { path: string }) =>
      call.path.startsWith("/v1/ae/social/visual/")
        ? { ok: false, status: 502, body: { error: { message: "no" } } }
        : { ok: true, status: 200, body: call.path === "/v1/ae/social/slot" ? { ...slot, draft: null } : view([slot]) },
    );
    openSocialView(s.context, view([slot]), null, vi.fn(async () => undefined), slot.id);
    await flush();
    await flush();
    expect(q(".sv-ed-thumb")?.textContent).toBe("Image unavailable");
  });
});

describe("Share to LinkedIn", () => {
  it("reads Share to LinkedIn on a touch device and Post on LinkedIn on a desktop", async () => {
    media(true);
    await openEditor(target());
    expect(btn("Share to LinkedIn")).toBeDefined();
    expect(q(".sv-ed-hint")?.textContent).toContain("share sheet with the text and the image");
    closeSocialView();
    media(false, false);
    await openEditor(target());
    expect(btn("Post on LinkedIn")).toBeDefined();
    expect(btn("Share to LinkedIn")).toBeUndefined();
    expect(q(".sv-ed-hint")).toBeNull();
  });

  it("asks once whether there is a share sheet, when the view opens, and shares nothing", async () => {
    media(true);
    const { share } = await openEditor(target());
    const probes = share.mock.calls.filter((c) => c[0].probe);
    expect(probes).toHaveLength(1);
    expect(shares(share)).toHaveLength(0);
  });

  it("copies, remembers the tap, then shares the text with the picture as a File, all before any await", async () => {
    media(true);
    const { share, openExternal } = await openEditor(target());
    const calls: string[] = [];
    document.execCommand = vi.fn(() => {
      calls.push("copy");
      return true;
    });
    share.mockImplementation(async () => {
      calls.push("share");
      return { shared: true };
    });
    btn("Share to LinkedIn").click();
    expect(calls).toEqual(["copy", "share"]); // synchronous: nothing awaited since the tap
    await flush();
    const options = shares(share)[0];
    expect(options.text).toBe(boldFirstLine(`New Short on the Analytic Endeavors channel: My Short.\n\n${SHORT}`));
    expect(options.files).toHaveLength(1);
    expect(options.files?.[0]).toBeInstanceOf(File);
    expect(options.files?.[0].name).toBe("my-short.jpg");
    expect(options.files?.[0].type).toBe("image/jpeg");
    expect(openExternal).not.toHaveBeenCalled();
    Reflect.deleteProperty(document, "execCommand");
  });

  it("asks 'Did you post it?' at once when the share sheet finishes", async () => {
    media(true);
    await openEditor(target());
    expect(q(".sv-banner")).toBeNull();
    btn("Share to LinkedIn").click();
    await flush();
    await flush();
    expect(q(".sv-banner")?.textContent).toContain("Back from LinkedIn. Did you post it?");
    vi.setSystemTime(NOW + 60_000);
    expect(noteReturn(NOW + 60_000)).toBe(false); // not asked a second time
  });

  it("opens the composer link from the tap when the probe said there is no share sheet", async () => {
    media(true);
    const slot = target();
    const s = stub(slot);
    s.share.mockImplementation(async (o) => (o.probe ? { available: false } : { shared: true }));
    openSocialView(s.context, view([slot]), null, vi.fn(async () => undefined)); // the probe runs here
    await flush();
    (q(`[data-slot="${slot.id}"]`) as HTMLElement).click();
    await flush();
    await flush();
    expect(btn("Post on LinkedIn")).toBeDefined(); // the label follows the probe
    btn("Post on LinkedIn").click();
    await flush();
    expect(shares(s.share)).toHaveLength(0);
    expect(s.openExternal.mock.calls[0][0]).toContain("https://www.linkedin.com/feed/?shareActive=true&text=");
  });

  it("does not open a link late when a share fails with ShareUnavailable: it says to paste", async () => {
    media(true);
    const { share, openExternal } = await openEditor(target());
    document.execCommand = vi.fn(() => true);
    share.mockRejectedValueOnce(Object.assign(new Error("none"), { code: "ShareUnavailable" }));
    btn("Share to LinkedIn").click();
    await flush();
    await flush();
    expect(openExternal).not.toHaveBeenCalled();
    expect(q(".sv-ed-status")?.textContent).toContain("Draft copied");
    expect(q(".sv-ed-status")?.textContent).toContain("linkedin.com/feed");
    expect(q(".sv-banner")).toBeNull();
    Reflect.deleteProperty(document, "execCommand");
    vi.setSystemTime(NOW + 60_000);
    expect(noteReturn(NOW + 60_000)).toBe(false); // the tap was forgotten
  });

  it.each(["Timeout", "RequestTimeout"])("treats %s as an unknown outcome: asks 'Did you post it?' and shows no error", async (code) => {
    media(true);
    const { share } = await openEditor(target());
    share.mockRejectedValueOnce(Object.assign(new Error("timed out"), { code }));
    btn("Share to LinkedIn").click();
    await flush();
    await flush();
    expect(q(".sv-banner")?.textContent).toContain("Did you post it?");
    expect(q(".sv-ed-status")?.textContent).toBe("");
  });

  it("forgets the tap and shows nothing when the share sheet is cancelled", async () => {
    media(true);
    const { share, openExternal } = await openEditor(target());
    share.mockRejectedValueOnce(Object.assign(new Error("closed"), { code: "ShareCancelled" }));
    btn("Share to LinkedIn").click();
    await flush();
    await flush();
    expect(q(".sv-ed-status")?.textContent).toBe("");
    expect(q(".sv-banner")).toBeNull();
    expect(openExternal).not.toHaveBeenCalled();
    vi.setSystemTime(NOW + 60_000);
    expect(noteReturn(NOW + 60_000)).toBe(false);
  });

  it("says what failed and where to paste when the share sheet fails", async () => {
    media(true);
    const { share } = await openEditor(target());
    share.mockRejectedValueOnce(Object.assign(new Error("boom"), { code: "ShareFailed" }));
    btn("Share to LinkedIn").click();
    await flush();
    await flush();
    expect(q(".sv-ed-status")?.textContent).toContain("Could not open the share sheet");
    expect(q(".sv-ed-status")?.textContent).toContain("linkedin.com/feed");
    expect(q(".sv-banner")).toBeNull();
  });

  it("tells the user when the share sheet took the text only", async () => {
    media(true);
    const { share } = await openEditor(target());
    share.mockResolvedValueOnce({ shared: true, files_dropped: true });
    btn("Share to LinkedIn").click();
    await flush();
    await flush();
    expect(q(".sv-ed-status")?.textContent).toContain("could not take the image");
  });

  it("keeps the desktop path: Post on LinkedIn copies and opens the web composer", async () => {
    media(false, false);
    const { share, openExternal } = await openEditor(target());
    btn("Post on LinkedIn").click();
    await flush();
    expect(shares(share)).toHaveLength(0);
    expect(openExternal.mock.calls[0][0]).toContain("https://www.linkedin.com/feed/?shareActive=true&text=");
  });

  it("keeps a Community post on copy plus the Posts tab, even on a touch device", async () => {
    media(true);
    const { share, openExternal } = await openEditor(target({ platform: "yt_community", kind: "video", visual: undefined }));
    expect(btn("Share to LinkedIn")).toBeUndefined();
    btn("Post on YouTube").click();
    await flush();
    expect(shares(share)).toHaveLength(0);
    expect(openExternal.mock.calls[0][0]).toContain("/posts");
  });
});

describe("waiting for the picture", () => {
  /** An editor whose picture answers only when the test says so (`release`), or fails. */
  async function slowPicture(fail = false) {
    media(true);
    const slot = target();
    const s = stub(slot);
    let release: () => void = () => undefined;
    s.request.mockImplementation(async (call: { path: string }) => {
      if (call.path.startsWith("/v1/ae/social/visual/")) {
        await new Promise<void>((resolve) => (release = resolve));
        return fail
          ? { ok: false, status: 502, body: { error: { message: "no" } } }
          : { ok: true, status: 200, body: { type: "image/jpeg", size: 9, parts: 1, part: 0, data: JPEG_B64 } };
      }
      return { ok: true, status: 200, body: call.path === "/v1/ae/social/slot" ? { ...slot, draft: null } : view([slot]) };
    });
    openSocialView(s.context, view([slot]), null, vi.fn(async () => undefined), slot.id);
    await flush();
    await flush();
    return { ...s, release: () => release() };
  }
  const post = () => q(".sv-ed-bar > .btn-primary") as HTMLButtonElement;

  it("reads Loading image... and is disabled while the picture loads, then shares with it", async () => {
    const s = await slowPicture();
    expect(post().textContent).toBe("Loading image...");
    expect(post().disabled).toBe(true);
    s.release();
    await flush();
    await flush();
    expect(post().textContent).toBe("Share to LinkedIn");
    expect(post().disabled).toBe(false);
    post().click();
    await flush();
    expect(shares(s.share)[0].files).toHaveLength(1);
  });

  it("enables the button after 8 seconds with a note that the text goes alone", async () => {
    const s = await slowPicture();
    expect(post().disabled).toBe(true);
    await vi.advanceTimersByTimeAsync(IMAGE_WAIT_MS - 1);
    expect(post().disabled).toBe(true);
    await vi.advanceTimersByTimeAsync(2);
    expect(post().disabled).toBe(false);
    expect(post().textContent).toBe("Share to LinkedIn");
    expect(q(".sv-ed-hint")?.textContent).toBe("The image could not load; this shares the text only.");
    post().click();
    await flush();
    expect(shares(s.share)[0].files).toBeUndefined();
  });

  it("enables the button at once, with the note, when the picture fails", async () => {
    const s = await slowPicture(true);
    s.release();
    await flush();
    await flush();
    expect(post().disabled).toBe(false);
    expect(q(".sv-ed-hint")?.textContent).toBe("The image could not load; this shares the text only.");
    expect(q(".sv-ed-thumb")?.textContent).toBe("Image unavailable");
  });
});

describe("the picture's identity", () => {
  it("loads again when a tool's image link changes, so the old picture is never shared", async () => {
    media(true);
    const tool = (image: string) =>
      target({ id: "li-tool-2026w46", kind: "tool", anchor: "diagram", tool: "D", title: null, url: "https://x.test/d", visual: { kind: "tool", key: image } });
    const first = await openEditor(tool("https://tools.example.com/a.jpg"));
    const fetches = (r: typeof first.request) => r.mock.calls.filter(([c]) => c.path.startsWith("/v1/ae/social/visual/")).length;
    expect(fetches(first.request)).toBe(1);
    closeSocialView();
    const same = await openEditor(tool("https://tools.example.com/a.jpg"));
    expect(fetches(same.request)).toBe(0); // cached for the same identity
    closeSocialView();
    const changed = await openEditor(tool("https://tools.example.com/b.jpg"));
    expect(fetches(changed.request)).toBe(1); // a new image link is fetched again
  });
});

describe("bold line 1 and the meta line", () => {
  it("skips #hashtags and @mentions as it skips links", () => {
    expect(boldFirstLine("Hello #PowerBI and @Reid at https://a.test/x now")).toBe(
      `${boldFirstLine("Hello")} #PowerBI ${boldFirstLine("and")} @Reid ${boldFirstLine("at")} https://a.test/x ${boldFirstLine("now")}`,
    );
    expect(boldFirstLine("a#b")).toBe(boldFirstLine("a#b")); // a # inside a word is not a hashtag
    expect(unboldFirstLine(boldFirstLine("Go #Fabric"))).toBe("Go #Fabric");
    expect(isFirstLineBold(boldFirstLine("Go #Fabric @me"))).toBe(true);
  });

  it("keeps the count and the chip from shrinking: only the due text truncates", async () => {
    const css = (await import("../src/style.css?raw")).default;
    expect(css).toMatch(/\.sv-ed-sub \{[^}]*overflow: hidden;[^}]*text-overflow: ellipsis;/);
    expect(css).toMatch(/\.sv-ed-tail \{\s*flex: 0 0 auto;/);
    expect(css).toMatch(/\.sv-bold \{\s*flex: 0 0 auto;/);
    media(false);
    await openEditor(target());
    expect(q(".sv-ed-info > .sv-ed-sub")).not.toBeNull();
    expect(q(".sv-ed-info > .sv-ed-tail .sv-ed-count")).not.toBeNull();
  });

  it("shows 'Starter text' as a short tag on the meta line, with the sentence as its tooltip", async () => {
    media(false);
    await openEditor(target());
    const tag = q(".sv-ed-dirty") as HTMLElement;
    expect(tag.textContent).toBe("Starter text");
    expect(tag.getAttribute("title")).toBe("Starter text, edit before posting");
    expect(tag.parentElement?.className).toBe("sv-ed-meta");
  });
});
