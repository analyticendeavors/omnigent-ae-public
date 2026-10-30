// The Admin Center's Box health monitor (2026-10-28, boxHealth.ts): the summary's order and
// "+n more", the missing and stale states, the tile, the Details dialog, the hints, and who
// sees what (a box admin who is not an OmniGent admin gets one card and one tile).
import type { ExtensionContext } from "@omnigent/extension-sdk";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { BoxActionOffer, BoxCleanupState, BoxHealthView, BoxUser } from "../src/api";
import { cardHealth, openByDefault } from "../src/board";
import {
  activateBoxHealth,
  agoText,
  BOX_HEALTH_HINTS,
  POLL_MS,
  boxHealthSection,
  countText,
  deactivateBoxHealth,
  detailsBody,
  FAST_POLL_MS,
  offerForNeed,
  openDetails,
  sizeText,
  sparkline,
  summaryNode,
  summaryOf,
  tileOf,
  updateSummary,
  worstOf,
} from "../src/boxHealth";
import { ACCESS_RETRY_MS, activateFabric, deactivateFabric } from "../src/fabric";
import { HINT_MAX, hasOpenHint } from "../src/hint";
import { getTile, resetTiles } from "../src/tiles";

const sdk = vi.hoisted(() => ({ defineExtension: vi.fn() }));
vi.mock("@omnigent/extension-sdk", () => sdk);

const GIB = 1024 ** 3;

function view(extra: Partial<BoxHealthView> = {}): BoxHealthView {
  return {
    state: "ok",
    tone: "green",
    read_at: "2026-10-28T17:30:00Z",
    age_s: 180,
    slow_read_at: "2026-10-28T17:05:00Z",
    slow_age_s: 1500,
    host: "box-example",
    needs: [],
    areas: [
      { id: "heat", label: "Heat", tone: "green", value: "CPU 91C · SSD 49C", title: "one hot reading" },
      { id: "cpu", label: "CPU", tone: "green", value: "Load 11 of 32", title: "" },
      { id: "memory", label: "Memory", tone: "green", value: "42 of 122 GiB", title: "" },
      { id: "storage", label: "Storage", tone: "green", value: "/tmp 46% inodes", title: "" },
      { id: "bloat", label: "Bloat", tone: "green", value: "Nothing stale", title: "" },
    ],
    details: {
      heat: { cpu_c: 91, nvme_c: 49, chassis_c: 89, gpu_c: 80, gpu_power_w: 112 },
      cpu: { threads: 32, load1: 11, load5: 8, load15: 5, busy_pct: 34, psi_some_avg60: 9 },
      memory: {
        total_b: 122 * GIB,
        used_b: 42 * GIB,
        used_pct: 34,
        shmem_b: 0,
        swap_total_b: 8 * GIB,
        swap_free_b: 4 * GIB,
        tmpfs: [{ path: "/tmp", used_b: 12 * GIB }],
      },
      mounts: [
        { path: "/tmp", fstype: "tmpfs", size_b: 62 * GIB, used_b: 12 * GIB, space_pct: 19, inodes: 1048576, inodes_used: 482000, inodes_pct: 46 },
      ],
      users: [
        {
          label: "UserA",
          kind: "box",
          claude: [{ where: "tmp", readable: true, sessions: 477, stale_sessions: 268, files: 252775, bytes: 13 * GIB }],
          litter: [{ group: "sec-appr-*", entries: 7081, stale: 7000 }],
          worktrees: { count: 24, bytes: 6 * GIB, older_14d: 6, with_changes: 3, partial: false },
        },
        { label: "UserB", kind: "box", claude: [{ where: "tmp", readable: false, top_age_days: 0.3 }], litter: [] },
        { label: "UserB", kind: "container", running: true, writable_b: 293e6, home_b: 1.2 * GIB, claude: [], litter: [] },
      ],
      guard: { running: true, locked: 14, failed: 0, age_s: 10, claude_processes: 12, claude_unmatched: 0, claude_remote: 4 },
      system: { uptime_s: 532000, failed_units: [], failed_user_units: [] },
    },
    history: { step_s: 900, end: null, cpu_c: [60, null, 70], nvme_c: [], load1: [], mem_pct: [], disk_pct: [] },
    settings: [
      { name: "AE_BOX_HEALTH_STALE_MINUTES", side: "server", value: "15", hint: "A reading older than this many minutes turns the card amber." },
      { name: "AE_BOX_USERS", side: "host", value: "set on the box", hint: "Maps box user ids and host containers to the names the card shows." },
    ],
    setup: null,
    ...extra,
  };
}

const need = (n: number, tone: "amber" | "red" = "amber") => ({
  key: `k${n}`,
  area: "bloat",
  tone,
  text: `Need ${n}`,
  detail: `Detail ${n}`,
  owner: null,
});

type Reply = { ok: boolean; status: number; body: unknown };
const ok = (body: unknown): Reply => ({ ok: true, status: 200, body });

function stubContext(replies: Record<string, Reply[]>) {
  const request = vi.fn(async ({ path }: { method: string; path: string }) => {
    return (
      replies[path]?.shift() ?? { ok: false, status: 500, body: { error: { message: `no reply for ${path}` } } }
    );
  });
  const context = {
    view: "fabric",
    server: { request },
    theme: {
      getCurrent: vi.fn(async () => ({ theme: "light" })),
      subscribe: vi.fn(async () => ({ dispose: vi.fn() })),
    },
  } as unknown as ExtensionContext;
  return { context, request, paths: () => request.mock.calls.map((c) => c[0].path) };
}

const flush = async () => {
  for (let i = 0; i < 6; i++) await new Promise((resolve) => setTimeout(resolve, 0));
};

beforeEach(() => {
  document.body.replaceChildren();
  const root = document.createElement("div");
  root.id = "root";
  document.body.appendChild(root);
  resetTiles();
});

afterEach(() => {
  vi.useRealTimers();
  deactivateBoxHealth();
  deactivateFabric();
  resetTiles();
  vi.restoreAllMocks();
});

describe("the words", () => {
  it("shortens sizes, counts and ages", () => {
    expect(sizeText(13 * GIB)).toBe("13 GB");
    expect(sizeText(1.5 * GIB)).toBe("1.5 GB");
    expect(sizeText(null)).toBe("n/a");
    expect(countText(26132)).toBe("26k");
    expect(countText(1204)).toBe("1,204");
    expect(countText(1_050_000)).toBe("1.05M");
    expect(agoText(10)).toBe("just now");
    expect(agoText(180)).toBe("3 m ago");
    expect(agoText(7200)).toBe("2 h ago");
  });
});

describe("the summary", () => {
  it("shows the worst three needs, then how many more", () => {
    const v = view({ needs: [need(1, "red"), need(2), need(3), need(4), need(5)] });
    const s = summaryOf(v);
    expect(s.needs.map((n) => n.text)).toEqual(["Need 1", "Need 2", "Need 3"]);
    expect(s.more).toBe(2);
    const node = summaryNode(v, () => {});
    expect(node.querySelector(".bh-more")?.textContent).toBe("+2 more in Details");
    expect([...node.querySelectorAll(".bh-tile .bh-lbl")].map((n) => n.textContent)).toEqual([
      "Heat",
      "CPU",
      "Memory",
      "Storage",
      "Bloat",
    ]);
  });

  it("puts the status line first, then the five tiles, then the Needs you panel", () => {
    const node = summaryNode(view({ needs: [need(1)] }), () => {});
    expect([...node.children].map((n) => n.className)).toEqual(["bh-status", "bh-rest"]);
    const kids = [...node.querySelector(".bh-rest")!.children].map((n) => n.className);
    expect(kids).toEqual(["bh-tiles", "bh-todo"]);
    expect(node.querySelector(".bh-age")?.textContent).toBe("read 3 m ago");
    expect(node.querySelectorAll(".bh-tiles > .bh-tile[title]")).toHaveLength(5);
    expect(node.querySelector(".bh-tile")?.getAttribute("aria-label")).toBe("Heat: CPU 91C · SSD 49C");
    expect(node.querySelector(".bh-todo .bh-todo-h")?.textContent).toBe("Needs you");
    expect(node.querySelectorAll(".bh-todo .bh-need")).toHaveLength(1);
  });

  it("draws no Needs you panel when nothing needs the reader", () => {
    const node = summaryNode(view(), () => {});
    expect(node.querySelector(".bh-todo")).toBeNull();
    expect(node.querySelectorAll(".bh-tile")).toHaveLength(5);
  });

  it("tints a tile by its tone", () => {
    const v = view();
    v.areas[0].tone = "amber";
    const node = summaryNode(v, () => {});
    expect(node.querySelector(".bh-tile")?.classList.contains("bh-t-amber")).toBe(true);
  });

  it("draws a missing reading as one grey line plus the setup command", () => {
    const v = view({ state: "missing", tone: "grey", read_at: null, age_s: null, setup: "systemctl --user enable --now x.timer" });
    const node = summaryNode(v, () => {});
    expect(node.textContent).toContain("No reading from the box yet.");
    expect(node.querySelector("code")?.textContent).toBe("systemctl --user enable --now x.timer");
    expect(node.querySelector(".bh-age")?.textContent).toBe("no reading yet");
  });

  it("dims a stale reading", () => {
    const node = summaryNode(view({ state: "stale", tone: "amber", age_s: 2520, needs: [need(1)] }), () => {});
    expect(node.classList.contains("is-stale")).toBe(true);
  });
});

describe("the worst-state pill", () => {
  it("says N need you in the server's tone, OK when green, No reading when missing", () => {
    expect(worstOf(view({ tone: "amber", needs: [need(1), need(2), need(3)] }))).toEqual({
      text: "3 need you",
      tone: "amber",
    });
    expect(worstOf(view())).toEqual({ text: "OK", tone: "green" });
    expect(worstOf(view({ state: "missing", tone: "grey" }))).toEqual({ text: "No reading", tone: "grey" });
  });

  it("draws a neutral icon for no reading and a warning icon for a need", () => {
    const missing = summaryNode(view({ state: "missing", tone: "grey" }), () => {});
    const need1 = summaryNode(view({ tone: "amber", needs: [need(1)] }), () => {});
    const icon = (n: HTMLElement) => n.querySelector(".bh-worst svg")?.getAttribute("class");
    expect(icon(missing)).toContain("icon-circle-dashed");
    expect(icon(need1)).toContain("icon-alert");
  });

  it("keeps the age and its hint together in one nowrap group", () => {
    const node = summaryNode(view(), () => {});
    const read = node.querySelector(".bh-when .bh-read")!;
    expect(read.querySelector(".bh-age")).not.toBeNull();
    expect(read.querySelector(".ae-hint-btn")).not.toBeNull();
  });

  it("is drawn in the status line with a title, and kept in step by updateSummary", () => {
    const node = summaryNode(view(), () => {});
    const details = node.querySelector(".bh-details");
    const pill = node.querySelector<HTMLElement>(".bh-worst")!;
    expect(pill.textContent).toBe("OK");
    expect(pill.classList.contains("bh-worst-green")).toBe(true);
    expect(pill.getAttribute("title")).toBeTruthy();
    updateSummary(node, view({ tone: "red", needs: [need(1, "red"), need(2)] }));
    expect(node.querySelector(".bh-worst")).toBe(pill);
    expect(pill.textContent).toBe("2 need you");
    expect(pill.classList.contains("bh-worst-red")).toBe(true);
    expect(pill.classList.contains("bh-worst-green")).toBe(false);
    expect(node.querySelector(".bh-details")).toBe(details);
  });
});

describe("the tile", () => {
  it("is OK when green and names the worst need otherwise", () => {
    expect(tileOf(view(), null)).toMatchObject({ id: "boxhealth", value: "OK", health: "ok", summary: "Box OK" });
    const bad = tileOf(view({ tone: "red", needs: [need(1, "red"), need(2)] }), null);
    expect(bad).toMatchObject({ value: "Need 1", health: "problem", note: "2 need you" });
    expect(tileOf(view({ state: "missing", tone: "grey" }), null).health).toBe("idle");
    expect(tileOf(null, new Error("x")).health).toBe("attention");
  });

  it("makes the card a problem, and unfolds it on a phone, when it is red", async () => {
    const { context } = stubContext({
      "/v1/ae/box-health": [ok(view({ tone: "red", needs: [need(1, "red")] }))],
    });
    const root = boxHealthSection();
    document.body.appendChild(root);
    activateBoxHealth(context, root);
    await flush();
    expect(getTile("boxhealth")?.health).toBe("problem");
    expect(cardHealth("box")).toBe("problem");
    expect(openByDefault("box")).toBe(true);
  });
});

describe("the Details dialog", () => {
  it("opens as a labelled modal, closes on Escape and puts focus back on Details", () => {
    const v = view({ needs: [need(1)] });
    const node = summaryNode(v, (opener) => openDetails(v, opener));
    document.body.appendChild(node);
    const details = node.querySelector<HTMLButtonElement>(".bh-details")!;
    expect(details.getAttribute("aria-haspopup")).toBe("dialog");
    details.focus();
    details.click();
    const card = document.querySelector('[role="dialog"]')!;
    expect(card.getAttribute("aria-modal")).toBe("true");
    expect(document.getElementById(card.getAttribute("aria-labelledby")!)?.textContent).toBe(
      "Box health details",
    );
    document.body.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
    expect(document.querySelector('[role="dialog"]')).toBeNull();
    expect(document.activeElement).toBe(details);
  });

  it("draws every section from the fixture, per user, with no names", () => {
    const text = detailsBody(view({ needs: [need(1)] })).textContent ?? "";
    for (const heading of ["NEEDS YOU", "HEAT", "CPU", "MEMORY", "STORAGE", "CLAUDE TEMP", "/TMP", "WORKTREES", "CONTAINERS", "OTHER", "SYSTEM", "SETTINGS"]) {
      expect(text.toUpperCase()).toContain(heading);
    }
    expect(text).toContain("not readable (aged by the box rule, changed 0.3 d ago)");
    expect(text).toContain("14 live sessions protected");
    expect(text).toContain("4 Remote Control");
    expect(text).toContain("24 worktrees");
  });

  it("draws a sparkline that breaks at a gap and says its highest", () => {
    const svg = sparkline([60, null, 70, 72], "CPU temperature over 24 hours");
    expect(svg.getAttribute("role")).toBe("img");
    expect(svg.getAttribute("aria-label")).toBe("CPU temperature over 24 hours, highest 72");
    expect(svg.querySelectorAll("polyline")).toHaveLength(1);
    expect(svg.querySelectorAll("circle")).toHaveLength(1);
  });
});

describe("hints", () => {
  it("gives every [data-hint] control in the section and the dialog a short, plain hint", () => {
    const v = view({ needs: [need(1)] });
    const section = summaryNode(v, () => {});
    const body = detailsBody(v);
    const found = [...section.querySelectorAll("[data-hint]"), ...body.querySelectorAll("[data-hint]")];
    expect(found.length).toBeGreaterThanOrEqual(6);
    for (const control of found) {
      const text = control.getAttribute("data-hint") ?? "";
      expect(text.length).toBeGreaterThan(0);
      expect(text.length).toBeLessThanOrEqual(HINT_MAX);
      expect(text).not.toContain("—");
      expect(text).not.toContain("--");
    }
    for (const text of Object.values(BOX_HEALTH_HINTS)) {
      expect(text.length).toBeLessThanOrEqual(HINT_MAX);
    }
  });
});

describe("who sees what", () => {
  it("shows a box admin who is not an OmniGent admin one card and one tile, and asks for nothing else", async () => {
    const { context, paths } = stubContext({
      "/v1/ae/access": [ok({ admin: false, box_admin: true })],
      "/v1/ae/box-health": [ok(view())],
    });
    await activateFabric(context);
    await flush();
    expect(document.querySelectorAll("[data-card]")).toHaveLength(1);
    expect(document.querySelector("[data-card]")?.getAttribute("data-card")).toBe("box");
    expect(document.querySelectorAll("[data-tile]")).toHaveLength(1);
    expect(document.querySelector("[data-tile]")?.getAttribute("data-tile")).toBe("boxhealth");
    expect(new Set(paths())).toEqual(new Set(["/v1/ae/access", "/v1/ae/box-health"]));
    expect(document.querySelector(".admin-grip")).not.toBeNull();
    expect(document.querySelector(".admin-board")?.hasAttribute("data-single")).toBe(true);
    expect(document.querySelector(".admin-reset")).toBeNull();
    expect(document.querySelector(".host-list")).toBeNull();
  });

  it("shows a plain user one line, after asking twice who they are", async () => {
    vi.useFakeTimers();
    const none = { admin: false, box_admin: false };
    const { context, paths } = stubContext({ "/v1/ae/access": [ok(none), ok(none)] });
    const done = activateFabric(context);
    await vi.advanceTimersByTimeAsync(ACCESS_RETRY_MS + 10);
    await done;
    expect(document.body.textContent).toContain(
      "The Admin Center is for admins. Ask an admin if you need access.",
    );
    expect(document.querySelectorAll("[data-card]")).toHaveLength(0);
    expect(paths()).toEqual(["/v1/ae/access", "/v1/ae/access"]);
  });

  it("believes a second answer when the first said no (a probe that failed for a moment)", async () => {
    vi.useFakeTimers();
    const { context, paths } = stubContext({
      "/v1/ae/access": [ok({ admin: false, box_admin: false }), ok({ admin: true, box_admin: true })],
      "/v1/ae/box-health": [ok(view())],
    });
    const done = activateFabric(context);
    await vi.advanceTimersByTimeAsync(ACCESS_RETRY_MS + 10);
    await done;
    await vi.advanceTimersByTimeAsync(10);
    expect(document.body.textContent).not.toContain("The Admin Center is for admins");
    expect(document.querySelectorAll("[data-card]").length).toBeGreaterThan(5);
    expect(paths().filter((p) => p === "/v1/ae/access")).toHaveLength(2);
  });

  it("asks again after a box-admin answer, so an admin whose first probe failed gets the whole page", async () => {
    vi.useFakeTimers();
    const { context, paths } = stubContext({
      "/v1/ae/access": [
        ok({ admin: false, box_admin: true }),
        ok({ admin: true, box_admin: true }),
      ],
      "/v1/ae/box-health": [ok(view())],
    });
    const done = activateFabric(context);
    await vi.advanceTimersByTimeAsync(ACCESS_RETRY_MS + 10);
    await done;
    await vi.advanceTimersByTimeAsync(10);
    expect(document.querySelectorAll("[data-card]").length).toBeGreaterThan(5);
    expect(paths().filter((p) => p === "/v1/ae/access")).toHaveLength(2);
  });

  it("keeps the box-only page when the second ask fails after a box-admin answer", async () => {
    vi.useFakeTimers();
    const { context } = stubContext({
      "/v1/ae/access": [ok({ admin: false, box_admin: true }), { ok: false, status: 500, body: {} }],
      "/v1/ae/box-health": [ok(view())],
    });
    const done = activateFabric(context);
    await vi.advanceTimersByTimeAsync(ACCESS_RETRY_MS + 10);
    await done;
    await vi.advanceTimersByTimeAsync(10);
    expect(document.querySelectorAll("[data-card]")).toHaveLength(1);
  });

  it("asks again after an error and uses the answer", async () => {
    vi.useFakeTimers();
    const { context } = stubContext({
      "/v1/ae/access": [
        { ok: false, status: 500, body: {} },
        ok({ admin: false, box_admin: true }),
      ],
      "/v1/ae/box-health": [ok(view())],
    });
    const done = activateFabric(context);
    await vi.advanceTimersByTimeAsync(ACCESS_RETRY_MS + 10);
    await done;
    await vi.advanceTimersByTimeAsync(10);
    expect(document.querySelectorAll("[data-card]")).toHaveLength(1);
  });

  it("draws the whole page for an admin, with the monitor first in the Box health card", async () => {
    const { context } = stubContext({
      "/v1/ae/access": [ok({ admin: true, box_admin: true })],
      "/v1/ae/box-health": [ok(view())],
    });
    await activateFabric(context);
    await flush();
    expect(document.querySelectorAll("[data-card]").length).toBeGreaterThan(5);
    const body = document.querySelector('[data-card="box"] .admin-card-body')!;
    expect(body.firstElementChild?.className).toContain("admin-boxhealth");
    expect(document.querySelector('[data-tile="boxhealth"]')).not.toBeNull();
  });

  it("draws the page as before when /v1/ae/access cannot be read, even on a second try", async () => {
    vi.useFakeTimers();
    const { context, paths } = stubContext({});
    const done = activateFabric(context);
    await vi.advanceTimersByTimeAsync(ACCESS_RETRY_MS + 10);
    await done;
    await vi.advanceTimersByTimeAsync(10);
    expect(document.querySelectorAll("[data-card]").length).toBeGreaterThan(5);
    expect(paths()).toContain("/v1/ae/fabric");
    expect(paths()).toContain("/v1/ae/box-health");
  });
});

describe("the dialog and the poll", () => {
  async function openFromPage(second: Partial<BoxHealthView> = {}) {
    vi.useFakeTimers();
    const { context } = stubContext({
      "/v1/ae/box-health": [ok(view({ age_s: 180 })), ok(view({ age_s: 3600, ...second }))],
    });
    const root = boxHealthSection();
    document.body.appendChild(root);
    activateBoxHealth(context, root);
    await vi.advanceTimersByTimeAsync(0);
    const details = root.querySelector<HTMLButtonElement>(".bh-details")!;
    details.focus();
    details.click();
    return { root, details, advance: (ms: number) => vi.advanceTimersByTimeAsync(ms) };
  }

  it("keeps the Details button through a poll, so closing returns focus to it", async () => {
    const { root, details, advance } = await openFromPage();
    expect(document.querySelector('[role="dialog"]')).not.toBeNull();
    await advance(60_000);
    expect(root.querySelector(".bh-age")?.textContent).toBe("read 60 m ago");
    expect(root.querySelector(".bh-details")).toBe(details);
    expect(details.isConnected).toBe(true);
    document.body.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
    expect(document.querySelector('[role="dialog"]')).toBeNull();
    expect(document.activeElement).toBe(details);
  });

  it("does not close an open hint on the summary when the poll redraws", async () => {
    const { root, advance } = await openFromPage();
    document.body.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
    const hintButton = root.querySelector<HTMLButtonElement>(".bh-when .ae-hint-btn")!;
    hintButton.click();
    expect(hasOpenHint()).toBe(true);
    await advance(60_000);
    expect(root.querySelector(".bh-when .ae-hint-btn")).toBe(hintButton);
    expect(hasOpenHint()).toBe(true);
    expect(hintButton.getAttribute("aria-expanded")).toBe("true");
  });

  it("updates the lines under the header in place", () => {
    const first = summaryNode(view(), () => {});
    const head = first.querySelector(".bh-status");
    updateSummary(first, view({ state: "stale", tone: "amber", age_s: 2520, needs: [need(1)] }));
    expect(first.querySelector(".bh-status")).toBe(head);
    expect(first.classList.contains("is-stale")).toBe(true);
    expect(first.querySelectorAll(".bh-need")).toHaveLength(1);
    expect(first.querySelector(".bh-age")?.textContent).toBe("read 42 m ago");
  });

  it("lets an open hint inside the dialog take Escape first, then the dialog closes", () => {
    const v = view();
    const opener = document.createElement("button");
    document.body.appendChild(opener);
    opener.focus();
    openDetails(v, opener);
    const tipButton = document.querySelector<HTMLButtonElement>(".ae-dialog .ae-hint-btn")!;
    tipButton.focus();
    expect(hasOpenHint()).toBe(true);
    tipButton.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
    expect(hasOpenHint()).toBe(false);
    expect(document.querySelector('[role="dialog"]')).not.toBeNull();
    tipButton.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
    expect(document.querySelector('[role="dialog"]')).toBeNull();
    expect(document.activeElement).toBe(opener);
  });

  it("closes an open hint, and its listeners, when the dialog closes another way", () => {
    const v = view();
    const opener = document.createElement("button");
    document.body.appendChild(opener);
    openDetails(v, opener);
    document.querySelector<HTMLButtonElement>(".ae-dialog .ae-hint-btn")!.click();
    expect(hasOpenHint()).toBe(true);
    document.querySelector<HTMLButtonElement>(".ae-dialog-close")!.click();
    expect(hasOpenHint()).toBe(false);
  });
});

// ── cleanup (2026-10-28, Box health PR 2) ───────────────────────────────────

const offer = (extra: Partial<BoxActionOffer> = {}): BoxActionOffer => ({
  id: "claude_scratch",
  owner: "UserA",
  label: "Remove 268 folders",
  count: 268,
  files: 12000,
  bytes: 3 * GIB,
  confirm: "Remove 268 stale Claude temp folders for UserA, about 3.0 GB. Live sessions are skipped.",
  can_run: true,
  why_not: null,
  hint: "Removes Claude temp folders nothing has touched for the stale age.",
  ...extra,
});

const FOUR: BoxActionOffer[] = [
  offer(),
  offer({ id: "tmp_litter", label: "Remove 7,000 leftovers", count: 7000, confirm: "Remove 7,000 old entries at the top of /tmp for UserA." }),
  offer({ id: "review_checkouts", label: "Remove 5 checkouts", count: 5, confirm: "Remove 5 review checkouts for UserA." }),
  offer({ id: "docker_prune", owner: "all", label: "Free 4.2 GB", count: 1, confirm: "Remove unused Docker images and build cache. Containers keep running." }),
];

const cleanup = (state: BoxCleanupState["state"], extra: Partial<BoxCleanupState> = {}): BoxCleanupState => ({
  state,
  action: "claude_scratch",
  owner: "UserA",
  label: "Claude temp folders",
  by: "UserA",
  requested_at: "2026-10-28T18:00:00Z",
  taken_at: null,
  finished_at: null,
  result: null,
  message: `Message for ${state}.`,
  can_cancel: state === "queued",
  can_retry: ["failed", "expired", "lost"].includes(state),
  ...extra,
});

const withActions = (extra: Partial<BoxHealthView> = {}) =>
  view({ actions: FOUR, details: { ...view().details, cleanup: { enabled: true, auto_aged: [] } }, ...extra });

const click = (node: Element | null | undefined) => (node as HTMLElement).click();
const buttonsIn = (root: ParentNode, text: string) =>
  [...root.querySelectorAll<HTMLButtonElement>("button")].filter((b) => b.textContent === text);

describe("cleanup offers", () => {
  it("shows the server's confirm text verbatim for each of the four actions", () => {
    for (const o of FOUR) {
      document.body.replaceChildren();
      openDetails(withActions(), null);
      click(buttonsIn(document, o.label)[0]);
      expect(document.querySelector(".bh-confirm-text")?.textContent).toBe(o.confirm);
      expect(buttonsIn(document.querySelector(".bh-confirm")!, o.label)).toHaveLength(1);
      expect(buttonsIn(document, "Cancel")).toHaveLength(1);
      click(document.querySelector(".ae-dialog-close"));
    }
  });

  it("draws no offer buttons for an older server or an empty list", () => {
    for (const v of [view(), view({ actions: [] })]) {
      const body = detailsBody(v);
      expect(body.querySelector(".bh-offer")).toBeNull();
      expect(body.textContent).toContain("Nothing to clean up right now.");
    }
  });

  it("disables a button that cannot run, with the reason as its title", () => {
    const blocked = offer({ can_run: false, why_not: "A request is already queued." });
    const body = detailsBody(withActions({ actions: [blocked] }));
    const button = body.querySelector<HTMLButtonElement>(".bh-offer-btn")!;
    expect(button.disabled).toBe(true);
    expect(button.title).toBe("A request is already queued.");
  });

  it("shows why a locked offer is locked, in the list and in the confirm step", () => {
    const blocked = offer({ can_run: false, why_not: "Another cleanup is queued or running." });
    const body = detailsBody(withActions({ actions: [blocked] }));
    const button = body.querySelector<HTMLButtonElement>(".bh-offer-btn")!;
    const why = body.querySelector<HTMLElement>(".bh-offer .bh-why")!;
    expect(why.textContent).toBe("Another cleanup is queued or running.");
    expect(button.getAttribute("aria-describedby")).toBe(why.id);
    openDetails(withActions({ actions: [blocked] }), null, blocked);
    const confirmWhy = document.querySelector<HTMLElement>(".bh-confirm .bh-why")!;
    const primary = document.querySelector<HTMLButtonElement>(".bh-confirm .btn-primary")!;
    expect(confirmWhy.textContent).toBe("Another cleanup is queued or running.");
    expect(primary.getAttribute("aria-describedby")).toBe(confirmWhy.id);
  });

  it("falls back to a plain reason when a locked offer gives none", () => {
    const body = detailsBody(withActions({ actions: [offer({ can_run: false, why_not: null })] }));
    expect(body.querySelector(".bh-why")!.textContent).toBe("The box cannot run this one now.");
  });

  it("shows no reason line for an offer that can run", () => {
    const body = detailsBody(withActions({ actions: [offer()] }));
    expect(body.querySelector(".bh-why")).toBeNull();
    expect(body.querySelector(".bh-offer-btn")!.hasAttribute("aria-describedby")).toBe(false);
    openDetails(withActions({ actions: [offer()] }), null, offer());
    expect(document.querySelector(".bh-confirm .bh-why")).toBeNull();
  });

  it("opens the confirm step with a disabled primary button when it cannot run", () => {
    const blocked = offer({ can_run: false, why_not: "The box is busy." });
    openDetails(withActions({ actions: [blocked] }), null, blocked);
    const primary = document.querySelector<HTMLButtonElement>(".bh-confirm .btn-primary")!;
    expect(primary.disabled).toBe(true);
    expect(primary.title).toBe("The box is busy.");
  });

  it("switches everything off with one line when the box has cleanup disabled", () => {
    const v = withActions({ details: { ...view().details, cleanup: { enabled: false, auto_aged: [] } } });
    const body = detailsBody(v);
    expect(body.textContent).toContain("Cleanup is switched off on the box");
    expect(body.querySelector(".bh-offer-btn")).toBeNull();
  });

  it("says what aged out on its own, by count and label, and hides the line when empty", () => {
    const aged = detailsBody(
      withActions({ details: { ...view().details, cleanup: { enabled: true, auto_aged: [{ owner: "UserB", count: 12 }] } } }),
    );
    expect(aged.textContent).toContain("Aged automatically in the last hour: 12 folders in UserB's container");
    expect(detailsBody(withActions()).textContent).not.toContain("Aged automatically");
  });

  it("matches a Needs-you line to an offer by action and owner (any owner for Docker)", () => {
    const v = withActions();
    const n = (action: string | null, owner: string | null) => ({ ...need(1), action: action as never, owner });
    expect(offerForNeed(v, n("claude_scratch", "UserA"))?.label).toBe("Remove 268 folders");
    expect(offerForNeed(v, n("claude_scratch", "UserB"))).toBeNull();
    expect(offerForNeed(v, n("docker_prune", null))?.id).toBe("docker_prune");
    expect(offerForNeed(v, n(null, "UserA"))).toBeNull();
  });

  it("puts Clean up on a Needs line with a matching offer, and opens the confirm", () => {
    const v = withActions({
      needs: [{ ...need(1), action: "claude_scratch", owner: "UserA" }, { ...need(2), action: "tmp_litter", owner: "UserB" }, need(3)],
    });
    const node = summaryNode(v, () => {});
    document.body.appendChild(node);
    const buttons = node.querySelectorAll<HTMLButtonElement>(".bh-need-act");
    expect(buttons).toHaveLength(1);
    expect(buttons[0].parentElement?.lastElementChild).toBe(buttons[0]);
    expect(buttons[0].getAttribute("data-hint")).toBe(BOX_HEALTH_HINTS.cleanUp);
    buttons[0].click();
    expect(document.querySelector(".bh-confirm-text")?.textContent).toBe(FOUR[0].confirm);
  });
});

describe("cleanup states", () => {
  const summaryOfState = (c: BoxCleanupState) =>
    summaryNode(withActions({ cleanup: c }), () => {});

  it("queued shows the message and Cancel request; running shows none", () => {
    const queued = summaryOfState(cleanup("queued"));
    expect(queued.querySelector(".bh-cleanup-msg")?.textContent).toBe("Message for queued.");
    expect(buttonsIn(queued, "Cancel request")).toHaveLength(1);
    const running = summaryOfState(cleanup("running"));
    expect(running.querySelector(".bh-cleanup-msg")?.textContent).toBe("Message for running.");
    expect(running.querySelector(".bh-cleanup button")).toBeNull();
    expect(summaryOfState(cleanup("untaken")).querySelector(".bh-c-untaken")).not.toBeNull();
  });

  it("puts the queued request in the Needs you panel as its last row, clock first, Cancel last", () => {
    const node = summaryNode(
      withActions({ needs: [{ ...need(1), action: "claude_scratch", owner: "UserA" }], cleanup: cleanup("queued") }),
      () => {},
    );
    const panel = node.querySelector(".bh-todo")!;
    const row = panel.lastElementChild!;
    expect(row.classList.contains("bh-c-queued")).toBe(true);
    expect(row.firstElementChild?.getAttribute("aria-hidden")).toBe("true");
    expect(row.firstElementChild?.tagName.toLowerCase()).toBe("svg");
    const cancel = row.lastElementChild as HTMLElement;
    expect(cancel.textContent).toBe("Cancel request");
    expect(cancel.getAttribute("data-hint")).toBe(BOX_HEALTH_HINTS.cancelRequest);
  });

  it("shows the request alone in the panel when no need is left", () => {
    const node = summaryNode(withActions({ cleanup: cleanup("queued") }), () => {});
    expect(node.querySelector(".bh-todo .bh-needs")).toBeNull();
    expect(node.querySelector(".bh-todo .bh-c-queued")).not.toBeNull();
  });

  it("done is green and lists what was skipped in plain words in the dialog", () => {
    const done = cleanup("done", {
      result: { removed: 200, bytes_freed: GIB, skipped: { live_session: 3, not_old: 0, changed_recently: 2 }, errors: [], partial: false },
    });
    const body = detailsBody(withActions({ cleanup: done }));
    expect(body.querySelector(".bh-c-done")).not.toBeNull();
    const items = [...body.querySelectorAll(".bh-skipped li")].map((li) => li.textContent);
    expect(items).toEqual(["live session: 3", "changed recently: 2"]);
    expect(summaryOfState(done).querySelector(".bh-skipped")).toBeNull();
  });

  it("failed, expired and lost offer Try again, which opens the confirm for the matching offer", () => {
    for (const state of ["failed", "expired", "lost"] as const) {
      document.body.replaceChildren();
      const node = summaryOfState(cleanup(state));
      document.body.appendChild(node);
      click(buttonsIn(node, "Try again")[0]);
      expect(document.querySelector(".bh-confirm-text")?.textContent).toBe(FOUR[0].confirm);
      click(document.querySelector(".ae-dialog-close"));
    }
    const none = summaryNode(view({ actions: [], cleanup: cleanup("failed") }), () => {});
    expect(buttonsIn(none, "Try again")).toHaveLength(0);
  });

  it("gives every cleanup button a short, plain hint and a title", () => {
    const nodes = [
      detailsBody(withActions({ cleanup: cleanup("queued") })),
      summaryNode(
        withActions({
          needs: [{ ...need(1), action: "claude_scratch", owner: "UserA" }],
          cleanup: cleanup("failed"),
        }),
        () => {},
      ),
    ];
    openDetails(withActions(), null, FOUR[0]);
    nodes.push(document.querySelector(".bh-confirm")! as HTMLElement);
    let seen = 0;
    for (const node of nodes) {
      for (const b of node.querySelectorAll<HTMLButtonElement>(".bh-act")) {
        seen++;
        const text = b.getAttribute("data-hint") ?? "";
        expect(text.length).toBeGreaterThan(0);
        expect(text.length).toBeLessThanOrEqual(HINT_MAX);
        expect(text).not.toContain("—");
        expect(text).not.toContain("--");
        expect(b.title.length).toBeGreaterThan(0);
      }
    }
    expect(seen).toBeGreaterThanOrEqual(7);
  });

  it("carries the classes the phone rules style (44 px buttons, wrapping confirm text)", () => {
    openDetails(withActions({ cleanup: cleanup("failed") }), null, FOUR[0]);
    expect(document.querySelectorAll(".bh-confirm .bh-act")).toHaveLength(2);
    expect(document.querySelector(".bh-confirm-text")).not.toBeNull();
    expect(document.querySelector(".bh-confirm-actions")).not.toBeNull();
    expect(detailsBody(withActions()).querySelectorAll(".bh-offer-btn.bh-act")).toHaveLength(4);
  });
});

describe("cleanup requests", () => {
  const POST = "/v1/ae/box-health/cleanup";

  async function openOnPage(replies: Record<string, Reply[]>) {
    const stub = stubContext(replies);
    const root = boxHealthSection();
    document.body.appendChild(root);
    activateBoxHealth(stub.context, root);
    await flush();
    click(root.querySelector(".bh-details"));
    return { ...stub, root };
  }

  it("sends exactly {action, owner, count}, then reads the box again and closes the confirm", async () => {
    const queued = withActions({ actions: [], cleanup: cleanup("queued") });
    const { request } = await openOnPage({
      "/v1/ae/box-health": [ok(withActions()), ok(queued)],
      [POST]: [ok({ state: "queued" })],
    });
    click(buttonsIn(document, "Remove 268 folders")[0]);
    click(document.querySelector(".bh-confirm .btn-primary"));
    await flush();
    const post = request.mock.calls.find((c) => c[0].method === "POST")![0] as unknown as { body: unknown };
    expect(post.body).toEqual({ action: "claude_scratch", owner: "UserA", count: 268 });
    expect(Object.keys(post.body as object).sort()).toEqual(["action", "count", "owner"]);
    expect(document.querySelector(".bh-confirm")).toBeNull();
    expect(document.querySelector(".ae-dialog .bh-c-queued")?.textContent).toContain("Message for queued.");
  });

  it("shows the server's words on a 409 box_health_changed, refreshes and closes the confirm", async () => {
    const { paths } = await openOnPage({
      "/v1/ae/box-health": [ok(withActions()), ok(withActions({ actions: [offer({ count: 270, label: "Remove 270 folders" })] }))],
      [POST]: [{ ok: false, status: 409, body: { error: { code: "box_health_changed", message: "The numbers changed. Look again." } } }],
    });
    click(buttonsIn(document, "Remove 268 folders")[0]);
    click(document.querySelector(".bh-confirm .btn-primary"));
    await flush();
    expect(document.querySelector(".bh-confirm")).toBeNull();
    expect(document.querySelector(".ae-dialog .notice-error")?.textContent).toBe("The numbers changed. Look again.");
    expect(buttonsIn(document, "Remove 270 folders")).toHaveLength(1);
    expect(paths().filter((p) => p === "/v1/ae/box-health")).toHaveLength(2);
  });

  it("keeps the confirm open on box_health_busy and says why", async () => {
    await openOnPage({
      "/v1/ae/box-health": [ok(withActions()), ok(withActions())],
      [POST]: [{ ok: false, status: 409, body: { error: { code: "box_health_busy", message: "Another cleanup is running." } } }],
    });
    click(buttonsIn(document, "Remove 268 folders")[0]);
    click(document.querySelector(".bh-confirm .btn-primary"));
    await flush();
    expect(document.querySelector(".bh-confirm")).not.toBeNull();
    expect(document.querySelector(".bh-confirm .notice-error")?.textContent).toBe("Another cleanup is running.");
  });

  it("cancels a queued request with an empty body", async () => {
    const { request } = await openOnPage({
      "/v1/ae/box-health": [ok(withActions({ cleanup: cleanup("queued") })), ok(withActions())],
      [`${POST}/cancel`]: [ok({})],
    });
    click(document.querySelector(".ae-dialog-close"));
    click(buttonsIn(document, "Cancel request")[0]);
    await flush();
    const post = request.mock.calls.find((c) => c[0].method === "POST")![0] as unknown as { path: string; body: unknown };
    expect(post.path).toBe(`${POST}/cancel`);
    expect(post.body).toEqual({});
    expect(buttonsIn(document, "Cancel request")).toHaveLength(0);
  });

  it("polls every 10 seconds while a request is queued, and every 60 after", async () => {
    vi.useFakeTimers();
    const { context, paths } = stubContext({
      "/v1/ae/box-health": [
        ok(withActions({ cleanup: cleanup("queued") })),
        ok(withActions({ cleanup: cleanup("running") })),
        ok(withActions({ cleanup: cleanup("done") })),
        ok(withActions({ cleanup: cleanup("done") })),
      ],
    });
    const root = boxHealthSection();
    document.body.appendChild(root);
    activateBoxHealth(context, root);
    await vi.advanceTimersByTimeAsync(0);
    expect(paths()).toHaveLength(1);
    await vi.advanceTimersByTimeAsync(FAST_POLL_MS);
    expect(paths()).toHaveLength(2);
    await vi.advanceTimersByTimeAsync(FAST_POLL_MS);
    expect(paths()).toHaveLength(3);
    await vi.advanceTimersByTimeAsync(FAST_POLL_MS);
    expect(paths()).toHaveLength(3);
    await vi.advanceTimersByTimeAsync(POLL_MS);
    expect(paths()).toHaveLength(4);
  });
});

describe("a container walk that skipped a folder", () => {
  it("says some folders were unreadable", () => {
    const base = view();
    const users = (base.details.users as BoxUser[]).map((user) =>
      user.kind === "container"
        ? { ...user, claude: [{ where: "container" as const, readable: true, sessions: 4, stale_sessions: 0, had_unreadable: true }] }
        : user,
    );
    const body = detailsBody(view({ details: { ...base.details, users } }));
    expect(body.textContent).toContain("some folders unreadable");
  });
});

describe("round 1 review of PR 395", () => {
  it("matches a Needs-you line to the offer of its own kind: a container line never opens a box one", () => {
    const v = withActions({
      actions: [offer({ owner: "UserB", where: "container", label: "Remove 12 folders", count: 12 })],
    });
    const line = (key: string) => ({ ...need(1), key, action: "claude_scratch" as const, owner: "UserB" });
    expect(offerForNeed(v, line("claude:UserB:tmp:box"))).toBeNull();
    expect(offerForNeed(v, line("claude:UserB:container:container"))?.label).toBe("Remove 12 folders");
  });

  it("says the box runs it within 5 minutes on the confirm button, never 'now'", () => {
    expect(BOX_HEALTH_HINTS.confirmRun).toContain("within 5 minutes");
    expect(BOX_HEALTH_HINTS.confirmRun).not.toContain(" now");
    expect(BOX_HEALTH_HINTS.confirmRun.length).toBeLessThanOrEqual(HINT_MAX);
  });

  it("keeps focus inside the dialog when a step change redraws it", () => {
    openDetails(withActions(), null);
    const dialog = document.querySelector<HTMLElement>("[role=dialog]")!;
    document.querySelector<HTMLButtonElement>(".bh-offer-btn")!.click();
    const back = [...document.querySelectorAll<HTMLButtonElement>(".bh-confirm button")].find(
      (b) => b.textContent === "Cancel",
    )!;
    back.focus();
    back.click();
    expect(document.activeElement).not.toBe(document.body);
    expect(dialog.contains(document.activeElement)).toBe(true);
  });
});
