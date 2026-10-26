// The Session Dashboard header (header.ts, 2026-10-17): the flow and the
// brief, the toggle that is remembered per user, and the release row.

import type { ExtensionContext } from "@omnigent/extension-sdk";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { CapacitySession, LotTask, PrCard } from "../src/api";
import {
  dayLabel,
  fetchRelease,
  HEADER_VIEW_KEY,
  type HeaderData,
  headerView,
  LOT_PIPS,
  loadHeaderView,
  monthDay,
  nextBack,
  OLD_S,
  renderHeader,
  resetHeaderView,
  lotCounts,
} from "../src/header";
import type { Limits } from "../src/limits";

function session(
  id: string,
  title: string,
  elapsed_s: number,
): CapacitySession {
  return {
    id,
    title,
    status: "working",
    live_status: "running",
    blocked: false,
    started: 0,
    elapsed_s,
    elapsed: `${Math.floor(elapsed_s / 3600)}h`,
    next: null,
    gate: null,
    fork: null,
    overflow: false,
    task: null,
  };
}

function pr(number: number, ci: Partial<PrCard["ci"]>): PrCard {
  return {
    number,
    title: `PR ${number}`,
    ci: {
      state: "pending",
      passing: 0,
      failing: 0,
      pending: 0,
      total: 0,
      ...ci,
    },
  } as PrCard;
}

function task(id: string, title: string, resurfaces: string | null): LotTask {
  return { id, title, resurfaces } as LotTask;
}

const NOW = "2026-10-17T15:00:00Z";
const LIMITS: Limits = {
  now: NOW,
  harnesses: {
    claude: {
      harness: "claude",
      observed_at: NOW,
      plan: null,
      note: null,
      windows: [
        {
          id: "5h",
          label: "5h",
          used_percent: 0,
          resets_at: null,
          window_minutes: 300,
        },
        {
          id: "wk",
          label: "week",
          used_percent: 100,
          resets_at: null,
          window_minutes: 10_080,
        },
      ],
    },
    codex: {
      harness: "codex",
      observed_at: NOW,
      plan: null,
      note: null,
      windows: [
        {
          id: "wk",
          label: "week",
          used_percent: 74,
          resets_at: null,
          window_minutes: 10_080,
        },
      ],
    },
  },
};

function data(extra: Partial<HeaderData> = {}): HeaderData {
  return {
    used: 4,
    limit: 3,
    working: [
      session("s1", "Omnigent Login MFA Options", 3 * 86_400),
      session("s2", "Fabric Admin Menu", 16 * 3600),
      session("s3", "MSFT Decision Model", 1800),
      session("s4", "Session Rail", 1200),
    ],
    yourMove: 0,
    prs: [pr(109, { passing: 3, pending: 5, total: 8 })],
    lot: {
      count: 16,
      next: task("t1", "shutdown-note-ritual", "2026-10-19"),
      soon: 1,
      overdue: 0,
    },
    release: {
      running: "ae-v0.16.0.34",
      latest: "ae-v0.16.0.35",
      state: "waiting",
      mid_turn: 2,
      error: null,
    },
    needsNote: 1,
    reference: 1,
    limits: LIMITS,
    folded: new Set(),
    ready: true,
    ...extra,
  };
}

function stubContext(saved: unknown = null) {
  const set = vi.fn(async () => undefined);
  const context = {
    navigation: { openSession: vi.fn() },
    server: { request: vi.fn() },
    storage: { user: { get: vi.fn(async () => saved), set } },
  } as unknown as ExtensionContext;
  return { context, set };
}

let host: HTMLElement;

beforeEach(() => {
  resetHeaderView();
  host = document.createElement("header");
  document.body.replaceChildren(host);
});

afterEach(() => {
  resetHeaderView();
});

const draw = (context: ExtensionContext, d = data()) => {
  const redraw = () => renderHeader(host, context, d, redraw);
  redraw();
};

describe("the flow view", () => {
  it("is the default and draws a block per working session, over the limit hatched", () => {
    const { context } = stubContext();
    draw(context);
    expect(host.querySelector(".hd-title")?.textContent).toBe(
      "Session Dashboard",
    );
    expect(host.querySelector(".hd-flow")).not.toBeNull();
    const blocks = [...host.querySelectorAll<HTMLElement>(".hd-slot")];
    expect(blocks.map((b) => b.querySelector("b")?.textContent)).toEqual([
      "Omnigent Login MFA Options",
      "Fabric Admin Menu",
      "MSFT Decision Model",
      "Session Rail",
    ]);
    expect(blocks.map((b) => b.classList.contains("is-over"))).toEqual([
      false,
      false,
      false,
      true,
    ]);
    expect(blocks.map((b) => b.classList.contains("is-old"))).toEqual([
      true,
      true,
      false,
      false,
    ]);
    expect(host.querySelector(".hd-flow h2 b")?.textContent).toBe("4 of 3");
    expect(
      host.querySelector(".hd-flow h2 b")?.classList.contains("is-over"),
    ).toBe(true);
    // Concept 3: no dot, a name under each slot.
    expect(host.querySelector(".hd-dot")).toBeNull();
    expect(
      [...host.querySelectorAll(".hd-cap span")].map((cap) => cap.textContent),
    ).toEqual(["slot 1", "slot 2", "slot 3", "over"]);
    blocks[0].click();
    expect(context.navigation.openSession).toHaveBeenCalledWith("s1");
  });

  it("draws a session family as one slot, led by its lead, with the rest counted", () => {
    const { context } = stubContext();
    const lead = {
      ...session("lead", "Role routing build", 3600),
      family: "lead",
    };
    const kids = ["Families PR", "Roles PR"].map((title, i) => ({
      ...session(`k${i}`, title, 600),
      family: "lead",
    }));
    draw(
      context,
      data({
        used: 2,
        working: [kids[0], lead, kids[1], session("s9", "Solo", 60)],
      }),
    );
    const blocks = [
      ...host.querySelectorAll<HTMLElement>(".hd-slot:not(.is-free)"),
    ];
    expect(blocks.map((b) => b.querySelector("b")?.textContent)).toEqual([
      "Role routing build",
      "Solo",
    ]);
    expect(blocks[0].querySelector("span")?.textContent).toBe("1h +2");
    expect(blocks[0].title).toContain("with 2 more in its family");
    expect(host.querySelectorAll(".hd-slot.is-free")).toHaveLength(1);
    expect(host.querySelectorAll(".hd-slot.is-over")).toHaveLength(0);
    blocks[0].click();
    expect(context.navigation.openSession).toHaveBeenCalledWith("lead");
  });

  it("shows free slots dashed when fewer sessions work than the limit", () => {
    const { context } = stubContext();
    draw(context, data({ used: 1, working: [session("s1", "One", 60)] }));
    const free = host.querySelectorAll(".hd-slot.is-free");
    expect(free).toHaveLength(2);
    expect(host.querySelectorAll(".hd-cap span")).toHaveLength(3);
  });

  it("lists pull request checks as pips, then the release, Your move and the lot", () => {
    const { context } = stubContext();
    draw(context);
    const rows = [...host.querySelectorAll<HTMLElement>(".hd-pipe-row")];
    expect(rows.map((row) => row.textContent)).toEqual([
      "PR #1093/8",
      "Releaseheld, 2 mid-reply",
      "Your moveclear",
      `Lot16, next ${monthDay("2026-10-19")}`,
    ]);
    const pipClasses = (row: HTMLElement) =>
      [...row.querySelectorAll(".hd-pip")].map((pip) =>
        pip.className.replace("hd-pip", "").trim(),
      );
    expect(pipClasses(rows[0])).toEqual([
      "is-pass",
      "is-pass",
      "is-pass",
      "is-wait",
      "is-wait",
      "is-wait",
      "is-wait",
      "is-wait",
    ]);
    expect(pipClasses(rows[1])).toEqual(["is-pass", "is-wait", ""]);
    // Clear: one grey pip, as concept 3 draws it.
    expect(pipClasses(rows[2])).toEqual([""]);
    expect(pipClasses(rows[3])).toEqual(["is-soon", "", ""]);
    expect(rows[1].title).toBe("Newest tag ae-v0.16.0.35");
    for (const row of rows) {
      expect(row.querySelector(".hd-pips")?.getAttribute("aria-hidden")).toBe(
        "true",
      );
    }
  });

  it("draws overdue lot tasks red before the blue ones and names them", () => {
    const { context } = stubContext();
    draw(context, data({ lot: { count: 4, next: null, soon: 1, overdue: 2 } }));
    const lot = [...host.querySelectorAll<HTMLElement>(".hd-pipe-row")][3];
    expect(
      [...lot.querySelectorAll(".hd-pip")].map((pip) => pip.className),
    ).toEqual(["hd-pip is-fail", "hd-pip is-fail", "hd-pip is-soon"]);
    expect(lot.textContent).toBe("Lot4, 2 overdue");
  });

  it("says a failing PR's count in red, and the brief names how many are waiting on CI", async () => {
    const { context } = stubContext();
    draw(context, data({ prs: [pr(1, { passing: 1, failing: 1, total: 2 })] }));
    expect(
      host.querySelector(".hd-pipe-row")?.classList.contains("is-fail"),
    ).toBe(true);
    const brief = stubContext("brief");
    await loadHeaderView(brief.context);
    const ready = {
      ...pr(3, { state: "passing", passing: 2, total: 2 }),
      state: "open",
      merge: { allowed: true },
      mergeable: { state: "clean" },
    } as unknown as PrCard;
    draw(
      brief.context,
      data({
        prs: [pr(2, { passing: 1, pending: 1, total: 2 }), ready, ready],
      }),
    );
    expect(host.querySelector(".hd-brief p")?.textContent).toContain(
      "3 PRs: 1 waiting on CI.",
    );
  });

  it("never prints a missing version", () => {
    const { context } = stubContext();
    draw(
      context,
      data({
        release: {
          running: null,
          latest: null,
          state: "current",
          mid_turn: 0,
          error: null,
        },
      }),
    );
    expect(host.querySelectorAll(".hd-pipe-row")[1].textContent).toBe(
      "Releasecurrent",
    );
  });

  it("caps Your move at eight pips and the lot's blue pips at three", () => {
    const { context } = stubContext();
    draw(
      context,
      data({
        yourMove: 11,
        lot: { count: 9, next: null, soon: 5, overdue: 0 },
      }),
    );
    const rows = [...host.querySelectorAll<HTMLElement>(".hd-pipe-row")];
    expect(rows[2].querySelectorAll(".hd-pip.is-wait")).toHaveLength(8);
    expect(rows[2].querySelector(".hd-more")?.textContent).toBe("+3");
    expect(rows[2].textContent).toContain("11 waiting");
    expect(rows[3].querySelectorAll(".hd-pip")).toHaveLength(LOT_PIPS);
    expect(rows[3].querySelectorAll(".hd-pip.is-soon")).toHaveLength(LOT_PIPS);
    expect(rows[3].querySelector(".hd-more")?.textContent).toBe("+2");
  });

  it("draws hollow pips for an unknown release and three green once it is in", () => {
    const { context } = stubContext();
    draw(
      context,
      data({
        release: {
          running: "x",
          latest: null,
          state: "unknown",
          mid_turn: 0,
          error: "timeout",
        },
      }),
    );
    expect(
      host.querySelectorAll(".hd-pipe-row")[1].querySelectorAll(".is-unknown"),
    ).toHaveLength(3);
    draw(
      context,
      data({
        release: {
          running: "ae-v1.0.0.1",
          latest: "ae-v1.0.0.1",
          state: "current",
          mid_turn: 0,
          error: null,
        },
      }),
    );
    expect(
      host.querySelectorAll(".hd-pipe-row")[1].querySelectorAll(".is-pass"),
    ).toHaveLength(3);
  });

  it("puts a usage bar per harness beside the title, its week window", () => {
    const { context } = stubContext();
    draw(context);
    const meters = [...host.querySelectorAll<HTMLElement>(".hd-top .hd-meter")];
    expect(
      meters.map((meter) => meter.querySelector(".hd-meter-lbl")?.textContent),
    ).toEqual(["Claude week100%", "Codex week74%"]);
    expect(meters[0].title).toMatch(
      /^Claude, updated .+\n5h: 0% used\nweek: 100% used$/,
    );
    const fills = meters.map((meter) =>
      meter.querySelector<HTMLElement>(".hd-fill")!,
    );
    expect(fills.map((fill) => fill.style.getPropertyValue("--pct"))).toEqual([
      "100%",
      "74%",
    ]);
    expect(fills.map((fill) => fill.className)).toEqual([
      "hd-fill is-err",
      "hd-fill is-warn",
    ]);
  });

  it("says current for the running release and hides rows that have no data", () => {
    const { context } = stubContext();
    draw(
      context,
      data({
        prs: null,
        yourMove: undefined,
        lot: null,
        release: {
          running: "ae-v1.0.0.1",
          latest: "ae-v1.0.0.1",
          state: "current",
          mid_turn: 0,
          error: null,
        },
      }),
    );
    const text = [...host.querySelectorAll(".hd-pipe-row")].map(
      (row) => row.textContent,
    );
    expect(text).toEqual(["Releaseae-v1.0.0.1 current"]);
  });
});

describe("the brief view", () => {
  it("says the worst thing first and names the oldest session, the PR and the next lot item", () => {
    const { context } = stubContext("brief");
    return loadHeaderView(context).then(() => {
      draw(context);
      expect(host.querySelector(".hd-dot")?.classList.contains("is-over")).toBe(
        true,
      );
      expect(host.querySelector(".hd-usage")).toBeNull();
      const sentence = host.querySelector(".hd-brief p")?.textContent ?? "";
      expect(sentence).toBe(
        "Over capacity: 4 working, limit 3. Oldest is Omnigent Login MFA Options at 72h; park it or finish it. " +
          "1 PR is waiting on CI. Nothing is on your move. " +
          `Next from the lot: shutdown-note-ritual, back ${dayLabel("2026-10-19")}.`,
      );
      // Concept 2's footer: usage, then the release, then the counts.
      const foot = [
        ...host.querySelectorAll(".hd-foot > span:not(.hd-sep)"),
      ].map((part) => part.textContent);
      expect(foot).toEqual([
        "Claude 5h 0%",
        "Claude week 100%",
        "Codex week 74%",
        "Release ae-v0.16.0.35 held: 2 sessions mid-reply",
        "16 parked, 1 needs a note, 1 reference",
      ]);
      expect(
        [...host.querySelectorAll(".hd-foot .hd-sep")].map(
          (sep) => sep.textContent,
        ),
      ).toEqual(["/", "/", "|", "|"]);
      expect(host.querySelector(".hd-foot .hd-use.is-err")?.textContent).toBe(
        "Claude week 100%",
      );
      const oldest = [
        ...host.querySelectorAll<HTMLElement>(".hd-brief .hd-link"),
      ][0];
      oldest.click();
      expect(context.navigation.openSession).toHaveBeenCalledWith("s1");
    });
  });

  it("counts failing checks and things on your move", () => {
    const { context } = stubContext("brief");
    return loadHeaderView(context).then(() => {
      draw(
        context,
        data({
          used: 2,
          working: [session("s1", "A", OLD_S - 1), session("s2", "B", 60)],
          prs: [
            pr(1, { failing: 1, total: 2, passing: 1 }),
            pr(2, { passing: 2, total: 2 }),
          ],
          yourMove: 2,
          lot: { count: 0, next: null, soon: 0, overdue: 0 },
        }),
      );
      expect(host.querySelector(".hd-brief p")?.textContent).toBe(
        "2 of 3 slots in use. 2 PRs: 1 with failing checks. 2 things are on your move.",
      );
    });
  });
});

describe("after review (2026-10-17)", () => {
  it("draws the title alone until the remembered view is read", () => {
    const { context } = stubContext();
    draw(context, data({ ready: false }));
    expect(host.querySelector(".hd-title")?.textContent).toBe(
      "Session Dashboard",
    );
    expect(host.querySelector(".hd-toggle")).toBeNull();
    expect(host.querySelector(".hd-flow, .hd-brief")).toBeNull();
  });

  it("names a folded section as text, not a link that goes nowhere", () => {
    const { context } = stubContext();
    draw(context, data({ folded: new Set(["your-move"]) }));
    const rows = [...host.querySelectorAll<HTMLElement>(".hd-pipe-row")];
    const yourMove = rows.find((row) =>
      row.textContent?.startsWith("Your move"),
    )!;
    expect(yourMove.querySelector("button")).toBeNull();
    const lot = rows.find((row) => row.textContent?.startsWith("Lot"))!;
    expect(lot.querySelector("button.hd-link")).not.toBeNull();
  });

  it("calls a PR ready only when its card would, and says when the release is not in", async () => {
    const { context } = stubContext("brief");
    await loadHeaderView(context);
    const draft = {
      ...pr(7, { state: "passing", passing: 2, total: 2 }),
      state: "open",
      draft: true,
      merge: { allowed: false },
      mergeable: { state: "clean" },
    } as unknown as PrCard;
    draw(
      context,
      data({
        used: 1,
        working: [session("s1", "A", 60)],
        prs: [draft],
        release: {
          running: "ae-v1.0.0.1",
          latest: "ae-v1.0.0.2",
          state: "waiting",
          mid_turn: 0,
          error: null,
        },
      }),
    );
    expect(host.querySelector(".hd-brief p")?.textContent).toContain(
      "1 PR needs a look.",
    );
    expect(host.querySelector(".hd-foot")?.textContent).toContain(
      "Release ae-v1.0.0.2 next, not installed yet",
    );
  });

  it("says why the release is unknown", () => {
    const { context } = stubContext();
    draw(
      context,
      data({
        release: {
          running: "x",
          latest: null,
          state: "unknown",
          mid_turn: 0,
          error: "no token",
        },
      }),
    );
    const text = [...host.querySelectorAll(".hd-pipe-row")].map(
      (row) => row.textContent,
    );
    expect(text).toContain("Releaseunknown: no token");
  });

  it("keeps the focus on the toggle after switching", async () => {
    const { context } = stubContext();
    await loadHeaderView(context);
    draw(context);
    host
      .querySelector<HTMLButtonElement>('.hd-toggle [data-view="brief"]')!
      .click();
    expect(document.activeElement?.getAttribute("data-view")).toBe("brief");
  });
});

describe("the toggle", () => {
  it("switches views and remembers the choice per user", async () => {
    const { context, set } = stubContext();
    await loadHeaderView(context);
    draw(context);
    const brief = host.querySelector<HTMLButtonElement>(
      '.hd-toggle [data-view="brief"]',
    )!;
    expect(brief.getAttribute("aria-pressed")).toBe("false");
    brief.click();
    expect(headerView()).toBe("brief");
    expect(set).toHaveBeenCalledWith(HEADER_VIEW_KEY, "brief");
    expect(host.querySelector(".hd-brief")).not.toBeNull();
    expect(host.querySelector(".hd-flow")).toBeNull();
    expect(
      host
        .querySelector('.hd-toggle [data-view="brief"]')
        ?.getAttribute("aria-pressed"),
    ).toBe("true");
  });

  it("ignores a saved value that is not a view", async () => {
    const { context } = stubContext("tiles");
    await loadHeaderView(context);
    expect(headerView()).toBe("flow");
  });
});

describe("helpers", () => {
  it("counts lot tasks back within a week, overdue ones too, across a month end", () => {
    const tasks = [
      task("a", "A", "2026-10-30"),
      task("b", "B", "2026-11-05"),
      task("c", "C", "2026-11-06"),
      task("d", "D", "2026-10-29"),
      task("e", "E", "2026-10-31T09:00:00"),
      task("f", "F", null),
    ];
    // Soon: a, b (the seventh day) and e (a date with a time); d is overdue; c, f neither.
    expect(lotCounts(tasks, "2026-10-30")).toEqual({ soon: 3, overdue: 1 });
  });

  it("writes days as Reid's concepts do, with no comma", () => {
    expect(monthDay("2026-10-19")).toMatch(/^\S+ 19$/);
    expect(dayLabel("2026-10-19")).toMatch(/^\S+ \S+ 19$/);
  });

  it("picks the soonest lot task on or after today", () => {
    const tasks = [
      task("a", "old", "2026-10-01"),
      task("b", "later", "2026-11-02"),
      task("c", "soon", "2026-10-19"),
      task("d", "undated", null),
    ];
    expect(nextBack(tasks, "2026-10-17")?.id).toBe("c");
    expect(nextBack(tasks, "2026-12-01")).toBeNull();
  });

  it("reads the release route and refuses an answer without a state", async () => {
    const request = vi
      .fn()
      .mockResolvedValueOnce({
        ok: true,
        status: 200,
        body: { state: "current", running: "x" },
      })
      .mockResolvedValueOnce({ ok: true, status: 200, body: {} })
      .mockResolvedValueOnce({ ok: false, status: 502, body: null });
    const context = { server: { request } } as unknown as ExtensionContext;
    await expect(fetchRelease(context)).resolves.toMatchObject({
      state: "current",
    });
    await expect(fetchRelease(context)).rejects.toThrow("no state");
    await expect(fetchRelease(context)).rejects.toThrow("502");
    expect(request).toHaveBeenCalledWith({
      method: "GET",
      path: "/v1/ae/release",
    });
  });
});
