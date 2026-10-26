// The Admin Center's Presets card (2026-10-24): KPIs, takeaways, the table, the
// row pop-up, the 7 d / 30 d toggle and the empty, warming and error states.
// Every call is mocked: the route is GET /v1/ae/presets?days=N.
import type { ExtensionContext } from "@omnigent/extension-sdk";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { PresetRow, PresetsAnswer, PresetSuggestion } from "../src/api";
import {
  activatePresets,
  COST_NOTE,
  costDelta,
  DAYS_KEY,
  deactivatePresets,
  duration,
  FALLBACK_NOTE,
  modelTone,
  POLL_MS,
  presetsSection,
  presetsTile,
  runHead,
  runStartText,
  footnote,
  shownTakeaways,
  suggestionActions,
  TOKENS_NOTE,
  tokensChange,
  tokensDelta,
  tokensText,
  visibleSuggestions,
  STALE_MS,
  timeDelta,
  updatedText,
  WARMING_POLL_MS,
} from "../src/presets";
import { getTile, resetTiles } from "../src/tiles";

const sdk = vi.hoisted(() => ({ defineExtension: vi.fn() }));
vi.mock("@omnigent/extension-sdk", () => sdk);

type Reply = { ok: boolean; status: number; body: unknown };
const NOW = Date.parse("2026-10-24T15:00:00Z");

function row(overrides: Partial<PresetRow> = {}): PresetRow {
  return {
    id: "deep",
    name: "Deep debug",
    color: "amber",
    custom: false,
    runs: 14,
    median_wall_s: 2460,
    median_cost: 9.8,
    cost_by_model: { "claude-opus-5-5": 85, "gpt-5-codex": 15 },
    raises: 2,
    step_downs: 0,
    hung_runs: 1,
    daily: [1, 2, 3, 2, 5],
    recent: [
      {
        at: "2026-10-23T14:02:00Z",
        session: "s1",
        title: "Fix ghcr pull denied",
        wall_s: 3120,
        active_s: null,
        cost: 11.4,
        agents: "planner, builder x2, Codex",
        model_changes: ["helper Sonnet to Haiku"],
        review: "2 reviewers, 6 findings, 5 fixed",
        flags: ["Codex reviewer hung"],
      },
      {
        at: "2026-10-22T09:40:00Z",
        session: null,
        title: "Admin card speed",
        wall_s: 2280,
        active_s: null,
        cost: 8.1,
        agents: "builder",
        model_changes: [],
        review: "none reported",
        flags: [],
      },
    ],
    ...overrides,
  };
}

function answer(extra: Partial<PresetsAnswer> = {}): PresetsAnswer {
  return {
    state: "ready",
    days: 30,
    mode: "log",
    asOf: new Date(NOW).toISOString(),
    kpis: {
      runs: 148,
      families: 31,
      cost: 412,
      cost_change_pct: 18,
      median_wall_s: 2280,
      wall_change_s: -360,
      raises: 9,
      step_downs: 22,
      hung_runs: 3,
    },
    takeaways: [
      {
        id: "cost:deep",
        level: "warn",
        text: "Deep debug costs 3.2x the rest and finishes no faster",
        why: "That is 14 runs, median $9.80 against $3.10.",
        preset: "deep",
      },
    ],
    presets: [
      row(),
      row({
        id: "custom",
        name: "Per session",
        custom: true,
        runs: 3,
        recent: [],
      }),
    ],
    ...extra,
  };
}

const ok = (body: unknown): Reply => ({ ok: true, status: 200, body });
const failed = (status: number, code: string, message: string): Reply => ({
  ok: false,
  status,
  body: { error: { code, message } },
});

function stubContext(replies: Reply[]) {
  const request = vi.fn(
    async ({ method, path }: { method: string; path: string }) => {
      if (method !== "GET" || !path.startsWith("/v1/ae/presets?days=")) {
        return failed(500, "x", `unexpected ${method} ${path}`);
      }
      return replies.shift() ?? failed(500, "x", "no reply queued");
    },
  );
  const openSession = vi.fn(async () => undefined);
  const context = {
    view: "fabric",
    // The suggestion side reads are not counted: these tests count the presets route.
    server: {
      request: async (args: { method: string; path: string }) =>
        args.path.startsWith("/v1/ae/preset-suggestions")
          ? failed(404, "x", "no suggestions here")
          : request(args),
    },
    navigation: { openSession },
  } as unknown as ExtensionContext;
  return { context, request, openSession };
}

const flush = () => vi.advanceTimersByTimeAsync(0);

async function mount(context: ExtensionContext): Promise<HTMLElement> {
  const node = presetsSection();
  document.getElementById("root")?.appendChild(node);
  activatePresets(context, node);
  await flush();
  return node;
}

beforeEach(() => {
  document.body.replaceChildren();
  const root = document.createElement("div");
  root.id = "root";
  document.body.appendChild(root);
  vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout", "Date"] });
  vi.setSystemTime(NOW);
  window.localStorage.clear();
  resetTiles();
});

afterEach(() => {
  deactivatePresets();
  resetTiles();
  vi.useRealTimers();
  vi.restoreAllMocks();
});

describe("the words", () => {
  it("writes durations and deltas", () => {
    expect(duration(3120)).toBe("52 m");
    expect(duration(7800)).toBe("2 h 10 m");
    expect(duration(20)).toBe("<1 m");
    expect(duration(null)).toBe("-");
    expect(costDelta(18)).toBe("+18% vs prior");
    expect(costDelta(-4)).toBe("-4% vs prior");
    expect(costDelta(null)).toBe("no prior period");
    expect(timeDelta(-360)).toBe("-6 min vs prior");
  });

  it("names a model's family for the bar", () => {
    expect(modelTone("claude-opus-5-5")).toBe("opus");
    expect(modelTone("gpt-5-codex")).toBe("codex");
    expect(modelTone("mystery")).toBe("other");
  });
});

describe("the card", () => {
  it("draws the four numbers, the takeaway, the table and the cost note", async () => {
    const { context, request } = stubContext([ok(answer())]);
    const node = await mount(context);
    expect(request).toHaveBeenCalledWith(
      expect.objectContaining({ path: "/v1/ae/presets?days=30" }),
    );
    const kpis = [...node.querySelectorAll(".presets-kpi")].map(
      (k) => k.textContent,
    );
    expect(kpis[0]).toContain("148");
    expect(kpis[0]).toContain("31 families");
    expect(kpis[1]).toContain("$412");
    expect(kpis[1]).toContain("+18% vs prior");
    expect(kpis[2]).toContain("38 min");
    expect(kpis[3]).toContain("9 / 22");
    expect(kpis[3]).toContain("3 hung runs");
    expect(node.querySelector(".presets-take b")?.textContent).toBe(
      "Deep debug costs 3.2x the rest and finishes no faster",
    );
    const rows = node.querySelectorAll(".presets-row");
    expect(rows).toHaveLength(2);
    expect(rows[0].textContent).toContain("Deep debug");
    expect(rows[0].textContent).toContain("$9.80");
    expect(rows[1].querySelector(".presets-pill")?.textContent).toBe("custom");
    expect(rows[0].querySelectorAll(".presets-bar i")).toHaveLength(2);
    expect(rows[0].querySelector("svg.azure-spark")).not.toBeNull();
    expect(node.textContent).toContain(COST_NOTE);
    expect(getTile("presets")?.value).toBe("148 runs");
  });

  it("says detailed logging is off in fallback mode and shows no takeaways", async () => {
    const { context } = stubContext([
      ok(answer({ mode: "fallback", takeaways: [] })),
    ]);
    const node = await mount(context);
    expect(node.querySelector(".presets-fallback")?.textContent).toBe(
      FALLBACK_NOTE,
    );
    expect(node.querySelector(".presets-take")).toBeNull();
  });

  it("says so when there are no runs, and while the server warms up", async () => {
    const { context, request } = stubContext([
      ok({ state: "warming", days: 30 }),
      ok(answer({ presets: [], takeaways: [] })),
    ]);
    const node = await mount(context);
    expect(node.textContent).toContain("Reading...");
    await vi.advanceTimersByTimeAsync(WARMING_POLL_MS);
    expect(request).toHaveBeenCalledTimes(2);
    expect(node.textContent).toContain("No runs in the last 30 days.");
  });

  it("shows a failure and keeps polling", async () => {
    const { context, request } = stubContext([
      failed(500, "boom", "server down"),
      ok(answer()),
    ]);
    const node = await mount(context);
    expect(node.querySelector(".notice")?.textContent).toContain("server down");
    await vi.advanceTimersByTimeAsync(POLL_MS);
    expect(request).toHaveBeenCalledTimes(2);
    expect(node.querySelector(".notice")).toBeNull();
    expect(node.querySelectorAll(".presets-row")).toHaveLength(2);
  });

  it("says admins only once and stops polling", async () => {
    const { context, request } = stubContext([
      failed(403, "admin_only", "Only an admin"),
    ]);
    const node = await mount(context);
    expect(node.textContent).toContain("for admins");
    await vi.advanceTimersByTimeAsync(POLL_MS * 2);
    expect(request).toHaveBeenCalledTimes(1);
    expect(presetsTile(null, new Error("x")).health).toBe("attention");
  });
});

describe("the toggle", () => {
  it("asks for 7 days, remembers it, and starts there next time", async () => {
    const { context, request } = stubContext([
      ok(answer()),
      ok(answer({ days: 7 })),
    ]);
    const node = await mount(context);
    const seven = [
      ...node.querySelectorAll<HTMLButtonElement>(".presets-seg"),
    ].find((b) => b.textContent === "7 d");
    seven?.click();
    await flush();
    expect(request).toHaveBeenLastCalledWith(
      expect.objectContaining({ path: "/v1/ae/presets?days=7" }),
    );
    expect(window.localStorage.getItem(DAYS_KEY)).toBe("7");
    expect(node.querySelector(".presets-seg.is-on")?.textContent).toBe("7 d");
    deactivatePresets();
    const again = stubContext([ok(answer({ days: 7 }))]);
    await mount(again.context);
    expect(again.request).toHaveBeenCalledWith(
      expect.objectContaining({ path: "/v1/ae/presets?days=7" }),
    );
  });

  it("works when storage throws", async () => {
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new Error("blocked");
    });
    const { context, request } = stubContext([ok(answer())]);
    await mount(context);
    expect(request).toHaveBeenCalledWith(
      expect.objectContaining({ path: "/v1/ae/presets?days=30" }),
    );
  });
});

describe("the pop-up", () => {
  it("opens on a row click with the runs, opens a session, and Escape closes it", async () => {
    const { context, openSession } = stubContext([ok(answer())]);
    const node = await mount(context);
    const first = node.querySelector<HTMLElement>(".presets-row");
    first?.focus();
    first?.click();
    const pop = document.querySelector(".presets-pop");
    expect(pop).not.toBeNull();
    expect(pop?.querySelector("h3")?.textContent).toBe(
      "Deep debug: 14 runs in 30 days",
    );
    const cells = [
      ...(pop?.querySelectorAll("tbody tr:first-child td") ?? []),
    ].map((c) => c.textContent);
    expect(cells.slice(2)).toEqual([
      "52 m",
      "-",
      "$11.40",
      "-",
      "planner, builder x2, Codex",
      "helper Sonnet to Haiku",
      "2 reviewers, 6 findings, 5 fixed",
      "Codex reviewer hung",
    ]);
    expect(pop?.textContent).not.toContain("Edit preset");
    expect(
      pop?.querySelectorAll("tbody tr")[1].querySelector("button"),
    ).toBeNull();
    pop?.querySelector<HTMLButtonElement>(".presets-run-link")?.click();
    expect(openSession).toHaveBeenCalledWith("s1");
    document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape" }));
    expect(document.querySelector(".presets-pop")).toBeNull();
    expect(document.activeElement).toBe(first);
  });

  it("closes on a click outside the card, and a takeaway's Show runs opens it", async () => {
    const { context } = stubContext([ok(answer())]);
    const node = await mount(context);
    node.querySelector<HTMLButtonElement>(".presets-take-act button")?.click();
    const pop = document.querySelector<HTMLElement>(".presets-pop");
    expect(pop).not.toBeNull();
    pop?.querySelector<HTMLElement>(".presets-pop-card")?.click();
    expect(document.querySelector(".presets-pop")).not.toBeNull();
    pop?.click();
    expect(document.querySelector(".presets-pop")).toBeNull();
  });

  it("opens from the keyboard", async () => {
    const { context } = stubContext([ok(answer())]);
    const node = await mount(context);
    node
      .querySelector(".presets-row")
      ?.dispatchEvent(
        new KeyboardEvent("keydown", { key: "Enter", bubbles: true }),
      );
    expect(document.querySelector(".presets-pop")).not.toBeNull();
  });
});

describe("review round 1", () => {
  it("polls an error answer at the normal rate and shows its note", async () => {
    const { context, request } = stubContext([
      ok({ state: "error", days: 30, note: "Could not read the runs (boom)" }),
      ok(answer()),
    ]);
    const node = await mount(context);
    expect(node.querySelector(".presets-error")?.textContent).toBe(
      "Could not read the runs (boom)",
    );
    expect(getTile("presets")?.health).toBe("attention");
    await vi.advanceTimersByTimeAsync(WARMING_POLL_MS * 2);
    expect(request).toHaveBeenCalledTimes(1);
    await vi.advanceTimersByTimeAsync(POLL_MS);
    expect(request).toHaveBeenCalledTimes(2);
    expect(node.querySelectorAll(".presets-row")).toHaveLength(2);
  });

  it("shows Updated from the server asOf and warns when it is old", async () => {
    const asOf = new Date(NOW - 2 * 60_000).toISOString();
    expect(updatedText({ ...answer(), asOf }, NOW)).toMatch(/^Updated /);
    expect(updatedText({ ...answer(), asOf }, NOW)).not.toContain("failing");
    const old = new Date(NOW - STALE_MS - 60_000).toISOString();
    expect(updatedText({ ...answer(), asOf: old }, NOW)).toMatch(
      /, refresh failing$/,
    );
    expect(presetsTile({ ...answer(), asOf: old }, null, 30, NOW).health).toBe(
      "attention",
    );
    expect(presetsTile({ ...answer(), asOf }, null, 30, NOW).health).toBe("ok");
    // In the card: the old answer says so.
    const { context } = stubContext([ok(answer({ asOf: old }))]);
    const node = await mount(context);
    expect(node.querySelector(".presets-refreshed")?.textContent).toContain(
      "refresh failing",
    );
    expect(getTile("presets")?.health).toBe("attention");
  });

  it("shows no time at all without an asOf", async () => {
    expect(updatedText(answer({ asOf: null }), NOW)).toBe("");
    expect(updatedText(answer({ asOf: "garbage" }), NOW)).toBe("");
    const { context } = stubContext([ok(answer({ asOf: null }))]);
    const node = await mount(context);
    expect(node.querySelector(".presets-refreshed")?.textContent).toBe("");
  });

  it("writes Started from started_at, else Ended from the end time", () => {
    const base = row().recent[0];
    expect(
      runStartText({ ...base, started_at: "2026-10-23T13:10:00Z" }),
    ).not.toContain("Ended");
    expect(runStartText({ ...base, started_at: null })).toMatch(/^Ended /);
    expect(runStartText({ ...base, started_at: null, at: null })).toBe("-");
  });

  it("the pop-up's first column is Started from started_at", async () => {
    const withStart = row({
      recent: [
        { ...row().recent[0], started_at: "2026-10-23T13:10:00Z" },
        row().recent[1],
      ],
    });
    const { context } = stubContext([ok(answer({ presets: [withStart] }))]);
    const node = await mount(context);
    node.querySelector<HTMLElement>(".presets-row")?.click();
    const cells = document.querySelectorAll(
      ".presets-pop tbody tr td:first-child",
    );
    expect(cells[0].textContent).not.toContain("Ended");
    expect(cells[1].textContent).toMatch(/^Ended /);
    expect(cells[0].getAttribute("data-label")).toBe("Started");
  });

  it("Escape closes the pop-up and goes no further", async () => {
    const { context } = stubContext([ok(answer())]);
    const node = await mount(context);
    node.querySelector<HTMLElement>(".presets-row")?.click();
    const behind = vi.fn();
    document.body.addEventListener("keydown", behind);
    document.body.dispatchEvent(
      new KeyboardEvent("keydown", { key: "Escape", bubbles: true }),
    );
    expect(document.querySelector(".presets-pop")).toBeNull();
    expect(behind).not.toHaveBeenCalled();
    document.body.removeEventListener("keydown", behind);
  });

  it("the focus trap skips disabled buttons", async () => {
    const { context } = stubContext([ok(answer())]);
    const node = await mount(context);
    node.querySelector<HTMLElement>(".presets-row")?.click();
    const links = document.querySelectorAll<HTMLButtonElement>(
      ".presets-pop .presets-run-link",
    );
    links[links.length - 1].disabled = true;
    const lastEnabled = links[0];
    lastEnabled.focus();
    const event = new KeyboardEvent("keydown", {
      key: "Tab",
      bubbles: true,
      cancelable: true,
    });
    document.dispatchEvent(event);
    expect(event.defaultPrevented).toBe(true);
    expect(document.activeElement).toBe(
      document.querySelector(".presets-pop-close"),
    );
  });

  it("the card table has five header cells and no Raises column", async () => {
    const { context } = stubContext([ok(answer())]);
    const node = await mount(context);
    const heads = [...node.querySelectorAll(".presets-main th")].map(
      (th) => th.textContent,
    );
    expect(heads).toEqual([
      "Preset",
      "Runs",
      "Median time",
      "Median cost",
      "Trend",
    ]);
    expect(
      node.querySelectorAll(".presets-row")[0].querySelectorAll("td"),
    ).toHaveLength(5);
    expect(node.querySelector(".presets-preset .presets-bar")).not.toBeNull();
  });
});

// ── suggestions from the scheduled review ───────────────────────────────────

function suggestion(
  overrides: Partial<PresetSuggestion> = {},
): PresetSuggestion {
  return {
    id: "sg_1",
    review_id: "rv_1",
    review_session: "s9",
    owner: "reid",
    kind: "own",
    preset_id: "deep",
    preset_title: "Deep debug",
    title: "Builder Opus high to Sonnet high",
    why: "There were 14 runs and the finding counts matched.",
    saving: "about $5 per run",
    changes: [{ path: "roles.builder", value: "x" }],
    status: "open",
    issue_url: null,
    created_at: "2026-10-20T07:00:00Z",
    updated_at: "2026-10-20T07:00:00Z",
    can_apply: true,
    can_undo: false,
    changes_text: "Builder: claude-opus-5-5 high to claude-sonnet-5-5 medium",
    ...overrides,
  };
}

const review = {
  id: "rv_1",
  session: "s9",
  owner: "reid",
  at: "2026-10-20T07:30:00Z",
  summary: "",
  count: 3,
};

/** A context that answers the presets route, the suggestion lists and the action POSTs. */
function suggestionContext(
  everyone: PresetSuggestion[],
  actions: Record<string, Reply> = {},
  own: PresetSuggestion[] = everyone.filter((s) => s.owner === "reid"),
) {
  const posted: string[] = [];
  const request = vi.fn(
    async ({ method, path }: { method: string; path: string }) => {
      if (path.startsWith("/v1/ae/presets?days=")) return ok(answer());
      if (method === "GET" && path === "/v1/ae/preset-suggestions?all=true") {
        return ok({ suggestions: everyone, reviews: [review] });
      }
      if (method === "GET" && path === "/v1/ae/preset-suggestions?all=false") {
        return ok({ suggestions: own, reviews: [review] });
      }
      if (method === "POST") {
        posted.push(path);
        return actions[path] ?? ok({ ok: true, suggestion: suggestion() });
      }
      return failed(500, "x", `unexpected ${method} ${path}`);
    },
  );
  const context = {
    view: "fabric",
    server: { request },
    navigation: { openSession: vi.fn() },
  } as unknown as ExtensionContext;
  return { context, request, posted };
}

const labels = (node: Element) =>
  [...node.querySelectorAll("button")].map((b) => b.textContent);

function withTokens(show: PresetsAnswer["show"]): PresetsAnswer {
  const base = answer({ show });
  base.kpis = {
    ...base.kpis!,
    tokens: 4_200_000,
    tokens_change_pct: 12,
  };
  base.takeaways = [
    ...base.takeaways!,
    {
      id: "tokens:deep",
      metric: "tokens",
      level: "warn",
      text: "Deep debug uses 3.0x the tokens of the rest",
      why: "That is 14 runs, median 9.0M tokens against 3.0M.",
      preset: "deep",
    },
    {
      id: "hang:Codex",
      metric: "hang",
      level: "warn",
      text: "Codex hung 3 times",
      why: "No update.",
      preset: "deep",
    },
  ];
  base.takeaways[0] = { ...base.takeaways[0], metric: "cost" };
  const first = base.presets![0];
  first.median_tokens = 1_200_000;
  first.total_tokens = 16_800_000;
  first.tokens_by_model = { "claude-opus-5-5": 90, "gpt-5-codex": 10 };
  first.daily_tokens = [5, 6, 7, 8, 9];
  first.recent[0].tokens = 2_400_000;
  first.recent[1].tokens = 800;
  return base;
}

describe("Cost, Tokens or Both", () => {
  it("writes compact tokens and the change words", () => {
    expect(tokensText(950)).toBe("950");
    expect(tokensText(12_340)).toBe("12.3K");
    expect(tokensText(4_200_000)).toBe("4.2M");
    expect(tokensText(1_100_000_000)).toBe("1.1B");
    expect(tokensText(null)).toBe("-");
    expect(tokensChange(12)).toBe("up 12%");
    expect(tokensChange(-8)).toBe("down 8%");
    expect(tokensChange(0)).toBe("flat");
    expect(tokensChange(null)).toBeNull();
    expect(tokensDelta(12, 7)).toBe("up 12% vs prior week");
    expect(tokensDelta(-8, 30)).toBe("down 8% vs prior 30 days");
    expect(tokensDelta(null, 7)).toBe("no prior period");
  });

  it("tokens: Tokens tile, Median tokens, tokens bar and series, no cost takeaways", async () => {
    const { context } = stubContext([ok(withTokens("tokens"))]);
    const node = await mount(context);
    const tiles = [...node.querySelectorAll(".presets-kpi")].map(
      (k) => k.textContent ?? "",
    );
    expect(tiles[1]).toContain("Tokens");
    expect(tiles[1]).toContain("4.2M");
    expect(tiles[1]).toContain("up 12% vs prior 30 days");
    expect(tiles[1]).not.toContain("$412");
    const heads = [...node.querySelectorAll(".presets-main th")].map(
      (h) => h.textContent,
    );
    expect(heads).toEqual([
      "Preset",
      "Runs",
      "Median time",
      "Median tokens",
      "Trend",
    ]);
    const first = node.querySelector(".presets-row");
    expect(first?.textContent).toContain("1.2M");
    expect(first?.textContent).not.toContain("$9.80");
    expect(
      first?.querySelector(".presets-bar")?.getAttribute("aria-label"),
    ).toContain("Tokens by model");
    expect(
      first?.querySelector("svg.azure-spark")?.getAttribute("aria-label"),
    ).toContain("daily tokens");
    const takes = [...node.querySelectorAll(".presets-take b")].map(
      (b) => b.textContent,
    );
    expect(takes).toEqual([
      "Deep debug uses 3.0x the tokens of the rest",
      "Codex hung 3 times",
    ]);
    expect(node.querySelector(".presets-foot")?.textContent).toBe(
      TOKENS_NOTE,
    );
    first?.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    const pop = document.querySelector(".presets-pop");
    const popHeads = [...(pop?.querySelectorAll("th") ?? [])].map(
      (h) => h.textContent,
    );
    expect(popHeads).toContain("Tokens");
    expect(popHeads).not.toContain("Cost");
    const cells = [...(pop?.querySelectorAll("tbody tr:first-child td") ?? [])];
    expect(cells.map((c) => c.textContent)).toContain("2.4M");
    expect(cells.map((c) => c.textContent)).not.toContain("$11.40");
  });

  it("cost: as before, tokens takeaways hidden, cost note only", async () => {
    const { context } = stubContext([ok(withTokens("cost"))]);
    const node = await mount(context);
    const tiles = [...node.querySelectorAll(".presets-kpi")].map(
      (k) => k.textContent ?? "",
    );
    expect(tiles[1]).toContain("Est. API cost");
    expect(tiles[1]).toContain("$412");
    expect(tiles[1]).not.toContain("tokens");
    expect(
      [...node.querySelectorAll(".presets-main th")].map((h) => h.textContent),
    ).toContain("Median cost");
    expect(node.querySelector(".presets-row")?.textContent).not.toContain(
      "1.2M",
    );
    const takes = [...node.querySelectorAll(".presets-take b")].map(
      (b) => b.textContent,
    );
    expect(takes).toEqual([
      "Deep debug costs 3.2x the rest and finishes no faster",
      "Codex hung 3 times",
    ]);
    expect(node.querySelector(".presets-foot")?.textContent).toBe(COST_NOTE);
    node
      .querySelector(".presets-row")
      ?.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    const popHeads = [
      ...document.querySelectorAll(".presets-pop th"),
    ].map((h) => h.textContent);
    expect(popHeads).toContain("Cost");
    expect(popHeads).not.toContain("Tokens");
  });

  it("both: second tile line, tokens under the median, both pop-up columns, all takeaways", async () => {
    const { context } = stubContext([ok(withTokens("both"))]);
    const node = await mount(context);
    const tile = node.querySelectorAll(".presets-kpi")[1];
    expect(tile.textContent).toContain("$412");
    expect(tile.querySelector(".presets-kpi-2")?.textContent).toBe(
      "4.2M tokens, up 12%",
    );
    const first = node.querySelector(".presets-row");
    expect(first?.textContent).toContain("$9.80");
    expect(first?.querySelector(".presets-sub")?.textContent).toBe(
      "1.2M tokens",
    );
    expect(
      first?.querySelector(".presets-bar")?.getAttribute("aria-label"),
    ).toContain("Cost by model");
    expect(
      first?.querySelector("svg.azure-spark")?.getAttribute("aria-label"),
    ).toContain("daily cost");
    expect(node.querySelectorAll(".presets-take")).toHaveLength(3);
    expect(node.querySelector(".presets-foot")?.textContent).toBe(
      footnote("both"),
    );
    first?.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    const popHeads = [
      ...document.querySelectorAll(".presets-pop th"),
    ].map((h) => h.textContent);
    expect(popHeads).toContain("Cost");
    expect(popHeads).toContain("Tokens");
    expect(runHead("both").indexOf("Tokens")).toBe(
      runHead("both").indexOf("Cost") + 1,
    );
  });

  it("treats a missing show as both, and caps takeaways at three", () => {
    const view = answer();
    view.takeaways = Array.from({ length: 5 }, (_, i) => ({
      id: `t${i}`,
      level: "info" as const,
      text: `t${i}`,
      why: "",
      preset: "deep",
    }));
    expect(shownTakeaways(view)).toHaveLength(3);
    expect(footnote("both")).toContain(COST_NOTE);
  });
});

describe("suggestions", () => {
  it("picks the buttons by kind and status", () => {
    expect(suggestionActions(suggestion())).toEqual(["apply", "dismiss"]);
    expect(suggestionActions(suggestion({ can_apply: false }))).toEqual([
      "dismiss",
    ]);
    expect(
      suggestionActions(
        suggestion({ status: "applied", can_apply: false, can_undo: true }),
      ),
    ).toEqual(["undo"]);
    expect(
      suggestionActions(suggestion({ status: "applied", can_apply: false })),
    ).toEqual([]);
    expect(suggestionActions(suggestion({ status: "undone" }))).toEqual([
      "apply",
      "dismiss",
    ]);
    expect(
      suggestionActions(suggestion({ status: "applying" })),
    ).toEqual(["apply"]);
    expect(
      suggestionActions(
        suggestion({ status: "undoing", can_apply: false, can_undo: true }),
      ),
    ).toEqual(["undo"]);
    expect(
      suggestionActions(suggestion({ kind: "starter", can_apply: false })),
    ).toEqual(["issue", "dismiss"]);
    expect(
      suggestionActions(
        suggestion({ kind: "starter", status: "issue_opened" }),
      ),
    ).toEqual([]);
    expect(
      suggestionActions(suggestion({ kind: "no_change", can_apply: false })),
    ).toEqual([]);
  });

  it("lists the latest review's rows and older open ones, never dismissed", () => {
    const view = visibleSuggestions({
      reviews: [review],
      suggestions: [
        suggestion(),
        suggestion({ id: "sg_2", status: "dismissed" }),
        suggestion({ id: "sg_3", review_id: "rv_0", status: "open" }),
        suggestion({
          id: "sg_4",
          review_id: "rv_0",
          status: "applied",
          can_apply: false,
        }),
        suggestion({ id: "sg_5", kind: "no_change", can_apply: false }),
      ],
    });
    expect(view.items.map((s) => s.id)).toEqual(["sg_1", "sg_3", "sg_5"]);
    expect(view.open).toBe(2);
  });

  it("shows a no-change row only from its owner's newest review", () => {
    const older = {
      ...review,
      id: "rv_0",
      at: "2026-10-06T07:30:00Z",
    };
    const view = visibleSuggestions({
      reviews: [older, review, { ...review, id: "rv_s", owner: "steve", at: "2026-10-01T00:00:00Z" }],
      suggestions: [
        suggestion({ id: "nc_old", kind: "no_change", review_id: "rv_0", can_apply: false }),
        suggestion({ id: "nc_new", kind: "no_change", review_id: "rv_1", can_apply: false }),
        // Steve's only review is older than reid's, yet it is his newest.
        suggestion({
          id: "nc_steve",
          kind: "no_change",
          owner: "steve",
          review_id: "rv_s",
          can_apply: false,
        }),
      ],
    });
    expect(view.items.map((s) => s.id)).toEqual(["nc_new", "nc_steve"]);
    expect(view.open).toBe(0);
  });

  it("draws the heading, the why line with the saving, and the buttons per kind", async () => {
    const { context } = suggestionContext([
      suggestion(),
      suggestion({
        id: "sg_2",
        kind: "starter",
        can_apply: false,
        title: "Add a reviewer timeout",
      }),
      suggestion({
        id: "sg_3",
        kind: "no_change",
        can_apply: false,
        title: "no change",
        saving: null,
      }),
      suggestion({
        id: "sg_4",
        kind: "starter",
        status: "issue_opened",
        can_apply: false,
        issue_url: "https://github.com/a/b/issues/1",
      }),
    ]);
    const node = await mount(context);
    expect(node.querySelector(".presets-suggestions-h")?.textContent).toBe(
      "Suggestions from Oct 20 review, 2 open",
    );
    const rows = node.querySelectorAll(".presets-suggestion");
    expect(rows).toHaveLength(4);
    expect(rows[0].textContent).toContain(
      "Deep debug: Builder Opus high to Sonnet high",
    );
    expect(rows[0].textContent).toContain("Expected saving: about $5 per run");
    expect(
      rows[0].querySelector(".presets-changes")?.textContent,
    ).toContain("Builder: claude-opus-5-5 high to claude-sonnet-5-5 medium");
    expect(labels(rows[0])).toEqual(["Apply", "Dismiss"]);
    expect(labels(rows[1])).toEqual(["Open issue", "Dismiss"]);
    expect(labels(rows[2])).toEqual([]);
    expect(labels(rows[3])).toEqual([]);
    expect(rows[3].querySelector("a")?.getAttribute("href")).toBe(
      "https://github.com/a/b/issues/1",
    );
  });

  it("says For <owner> only on someone else's row, and offers no Apply there", async () => {
    const { context } = suggestionContext([
      suggestion(),
      suggestion({ id: "sg_2", owner: "steve", can_apply: false }),
    ]);
    const node = await mount(context);
    const rows = node.querySelectorAll(".presets-suggestion");
    expect(rows[0].textContent).not.toContain("For ");
    expect(rows[1].textContent).toContain("For steve");
    expect(labels(rows[1])).toEqual(["Dismiss"]);
  });

  it("Apply posts to the apply route and reads the list again", async () => {
    const { context, posted, request } = suggestionContext([suggestion()]);
    const node = await mount(context);
    const before = request.mock.calls.length;
    node
      .querySelector<HTMLButtonElement>(".presets-suggestion button")
      ?.click();
    await flush();
    expect(posted).toEqual(["/v1/ae/preset-suggestions/sg_1/apply"]);
    expect(request.mock.calls.length).toBeGreaterThan(before + 1);
  });

  it("shows the 409 message on the row and keeps the buttons", async () => {
    const message =
      "This preset changed since the review on Oct 20. Dismiss it or edit the preset yourself.";
    const { context } = suggestionContext([suggestion()], {
      "/v1/ae/preset-suggestions/sg_1/apply": failed(
        409,
        "preset_changed",
        message,
      ),
    });
    const node = await mount(context);
    node
      .querySelector<HTMLButtonElement>(".presets-suggestion button")
      ?.click();
    await flush();
    const row = node.querySelector(".presets-suggestion");
    expect(row?.querySelector("[role=alert]")?.textContent).toBe(message);
    expect(labels(row as Element)).toEqual(["Apply", "Dismiss"]);
    expect((row?.querySelector("button") as HTMLButtonElement).disabled).toBe(
      false,
    );
  });

  it("the card still draws when the suggestion routes fail", async () => {
    const { context } = stubContext([ok(answer())]);
    const node = await mount(context);
    expect(node.querySelectorAll(".presets-row")).toHaveLength(2);
    expect(node.querySelector(".presets-suggestions")).toBeNull();
  });
});
