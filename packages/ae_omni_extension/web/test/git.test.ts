// The Admin Center's Git repos card (2026-10-24), "Needs you first": the rules
// that sort everything by who moves next, the card that draws them, Show all,
// and the tile. Every call is mocked; owners are made up (contoso, fabrikam,
// northwind) and no person's name appears.
import type { ExtensionContext } from "@omnigent/extension-sdk";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { GitAlert, GitBranch, GitOverview, GitPull, GitRepo, GitRun } from "../src/api";
import { AeRequestError } from "../src/api";
import {
  activateGit,
  ageText,
  analyze,
  checksText,
  deactivateGit,
  gitSection,
  gitTile,
  OLD_BRANCH_DAYS,
  pullPill,
  STUCK_HOURS,
  SWEEP_LATE_HOURS,
  summaryWords,
  whenShort,
} from "../src/git";
import { getFilter, resetAttention, setAttention } from "../src/attentionStore";
import { getTile, resetTiles } from "../src/tiles";
import { item, many, overview } from "./attentionFixtures";

const sdk = vi.hoisted(() => ({ defineExtension: vi.fn() }));
vi.mock("@omnigent/extension-sdk", () => sdk);

type Reply = { ok: boolean; status: number; body: unknown };

const GET = "GET /v1/ae/git";
const NOW = Date.parse("2026-10-24T20:00:00Z");
const HOUR = 3_600_000;
const DAY = 24 * HOUR;
const before = (ms: number) => new Date(NOW - ms).toISOString();

const pr = (n: number, fields: Partial<GitPull> = {}): GitPull => ({
  number: n,
  title: `Bump thing ${n}`,
  url: `https://github.com/contoso/web/pull/${n}`,
  draft: false,
  merge_state: "CLEAN",
  checks: "SUCCESS",
  author: "contoso-dev",
  updated_at: before(2 * DAY),
  ...fields,
});

const alert = (
  n: number,
  pkg: string,
  bucket: "fix" | "waiting",
  fields: Partial<GitAlert> = {},
): GitAlert => ({
  number: n,
  url: `https://github.com/contoso/web/security/dependabot/${n}`,
  severity: "HIGH",
  package: pkg,
  ecosystem: "NPM",
  manifest: "package-lock.json",
  range: "< 2.0.0",
  patched: "2.0.0",
  installed: bucket === "fix" ? ["1.4.0"] : ["2.0.0"],
  bucket,
  created_at: before(5 * DAY),
  waiting_since: bucket === "waiting" ? before(3 * DAY) : null,
  ...fields,
});

const branch = (name: string, ageMs: number | null, fields: Partial<GitBranch> = {}): GitBranch => ({
  name,
  url: `https://github.com/contoso/web/tree/${name}`,
  last_commit_at: ageMs === null ? null : before(ageMs),
  ahead: null,
  kept: false,
  ...fields,
});

function repo(name: string, fields: Partial<GitRepo> = {}): GitRepo {
  return {
    repo: name,
    url: `https://github.com/${name}`,
    default_branch: "main",
    view_only: false,
    pushed_at: null,
    error: null,
    prs: [],
    alerts: [],
    branches: [],
    ...fields,
  };
}

const run = (id: number, ageMs: number, conclusion: string | null = "success"): GitRun => ({
  id,
  url: `https://github.com/contoso/automation/actions/runs/${id}`,
  created_at: before(ageMs),
  status: conclusion ? "completed" : "in_progress",
  conclusion,
});

const TOTALS = { repos: 0, prs: 0, prs_ready: 0, alerts_fix: 0, alerts_waiting: 0, branches: 0, repo_errors: 0 };

/** A quiet, connected answer; each test adds what it is about. */
function calm(fields: Partial<GitOverview> = {}): GitOverview {
  return {
    configured: true,
    setup: null,
    updated_at: before(10 * 60_000),
    error: null,
    accounts: [{ owner: "contoso", repos: 1, connected: true, note: null }],
    totals: { ...TOTALS, repos: 1 },
    repos: [repo("contoso/web")],
    sweep: {
      url: "https://github.com/contoso/automation/actions/workflows/dependency-sweep.yml",
      runs: [run(3, 2 * HOUR)],
      error: null,
    },
    ...fields,
  };
}

/** Everything at once: one row of every kind and a line of every quiet kind. */
function busy(): GitOverview {
  return calm({
    accounts: [
      { owner: "contoso", repos: 3, connected: true, note: null },
      { owner: "fabrikam", repos: 1, connected: false, note: "No token: set AE_GIT_TOKEN_FABRIKAM in the server's secrets file." },
    ],
    totals: { ...TOTALS, repos: 5, prs: 7, prs_ready: 1, alerts_fix: 1, alerts_waiting: 3, branches: 3, branches_kept: 1, repo_errors: 2 },
    repos: [
      repo("contoso/web", {
        prs: [
          pr(12),
          pr(13, { merge_state: "BEHIND" }),
          pr(14, { draft: true }),
          pr(15, { merge_state: "BEHIND", author: "dependabot[bot]" }),
          pr(16, { merge_state: "DIRTY", checks: "FAILURE" }),
          pr(17, { merge_state: "UNSTABLE", checks: "PENDING" }),
          pr(18, { merge_state: "BLOCKED" }),
        ],
        alerts: [
          alert(1, "lodash", "fix"),
          alert(2, "undici", "waiting"),
          alert(3, "@azure/msal-node", "waiting"),
        ],
        branches: [
          branch("feat/old", 60 * DAY, { ahead: 61 }),
          branch("feat/new", 2 * DAY),
          branch("feat/kept", 90 * DAY, { kept: true }),
        ],
      }),
      repo("contoso/api", { alerts: [alert(5, "express", "waiting", { waiting_since: before(2 * HOUR), created_at: before(2 * HOUR) })] }),
      repo("northwind/broken", { error: "GitHub answered 502 for northwind/broken; try again shortly." }),
      repo("northwind/legacy", {
        view_only: true,
        prs: [pr(1), pr(2), pr(3)],
        alerts: [alert(9, "x", "fix")],
      }),
      repo("fabrikam/site", { error: "No token: set AE_GIT_TOKEN_FABRIKAM in the server's secrets file." }),
    ],
    sweep: {
      url: "https://github.com/contoso/automation/actions/workflows/dependency-sweep.yml",
      runs: [run(3, 2 * HOUR), run(2, 8 * HOUR, "failure")],
      error: null,
    },
  });
}

const ok = (body: unknown): Reply => ({ ok: true, status: 200, body });
const failed = (code: number, message: string): Reply => ({
  ok: false,
  status: code,
  body: { error: { code: "x", message } },
});

function stubContext(replies: Reply[], stored?: unknown) {
  const request = vi.fn(async ({ method, path }: { method: string; path: string }) => {
    if (`${method} ${path}` !== GET) return failed(500, `unexpected ${path}`);
    return replies.shift() ?? failed(500, "no reply queued");
  });
  const openExternal = vi.fn(async () => undefined);
  const get = vi.fn(async () => stored);
  const set = vi.fn(async () => undefined);
  const context = {
    view: "fabric",
    server: { request },
    navigation: { openExternal },
    storage: { user: { get, set } },
  } as unknown as ExtensionContext;
  return { context, request, openExternal, get, set };
}

const flush = () => vi.advanceTimersByTimeAsync(0);

async function mount(context: ExtensionContext): Promise<HTMLElement> {
  const node = gitSection();
  document.getElementById("root")?.appendChild(node);
  activateGit(context, node);
  await flush();
  return node;
}

const q = <T extends HTMLElement = HTMLElement>(node: ParentNode, sel: string) =>
  node.querySelector<T>(sel);
const all = (node: ParentNode, sel: string) => [...node.querySelectorAll<HTMLElement>(sel)];
const text = (node: ParentNode, sel: string) => q(node, sel)?.textContent;

beforeEach(() => {
  resetTiles();
  resetAttention();
  document.body.replaceChildren();
  const root = document.createElement("div");
  root.id = "root";
  document.body.appendChild(root);
  vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout", "Date"] });
  vi.setSystemTime(NOW);
});

afterEach(() => {
  deactivateGit();
  resetAttention();
  vi.useRealTimers();
});

describe("words", () => {
  it("maps draft, merge state and checks to a pill", () => {
    const base = { draft: false, merge_state: "CLEAN", checks: "SUCCESS" };
    expect(pullPill({ ...base, draft: true })).toEqual({ label: "Draft", tone: "muted" });
    expect(pullPill(base)).toEqual({ label: "Ready", tone: "ok" });
    expect(pullPill({ ...base, merge_state: "BEHIND" })).toEqual({ label: "Behind", tone: "warn" });
    expect(pullPill({ ...base, merge_state: "DIRTY" })).toEqual({ label: "Conflicts", tone: "err" });
    expect(pullPill({ ...base, merge_state: "BLOCKED", checks: "FAILURE" })).toEqual({ label: "Checks failing", tone: "err" });
    expect(pullPill({ ...base, merge_state: "UNSTABLE" })).toEqual({ label: "Checks running", tone: "warn" });
    expect(pullPill({ ...base, merge_state: null, checks: null }).label).toBe("unknown");
  });

  it("words the checks, ages and times", () => {
    expect(checksText("SUCCESS")).toBe("checks passed");
    expect(checksText("ERROR")).toBe("checks failing");
    expect(checksText("PENDING")).toBe("checks running");
    expect(checksText(null)).toBe("no checks");
    expect(ageText(before(0), NOW)).toBe("today");
    expect(ageText(before(3 * DAY), NOW)).toBe("3 days");
    expect(ageText(before(35 * DAY), NOW)).toBe("5 weeks");
    expect(ageText(before(120 * DAY), NOW)).toBe("4 months");
    expect(ageText(null)).toBe("");
    expect(whenShort("not a date")).toBe("");
    expect(whenShort("2026-10-18T10:00:00Z", NOW)).toMatch(/Oct/);
  });
});

describe("the lines that are not the owner's to move", () => {
  // The rows for the owner come from the server now (git_center.py `needs()`, pinned by
  // gitParity.test.ts); what stays in the card is the other half of the old sort.
  const quietOf = (data: GitOverview) => analyze(data, NOW).quiet;

  it("files the rest as lines that are not the owner's to move", () => {
    const { quiet, ready, oldBranches } = analyze(busy(), NOW);
    const line = (who: string) => quiet.find((l) => l.who === who);
    expect(quiet.map((l) => l.who)).toEqual(["GitHub", "CI", "Sweep", "Drafts", "Recent branches", "Kept", "View only"]);
    expect(line("GitHub")!.items.map((i) => i.text)).toEqual(["1 alert in api, fixed on main today"]);
    expect(line("CI")!.items.map((i) => i.text)).toEqual(["web #17 checks running"]);
    expect(line("Sweep")!.items.map((i) => i.text)).toEqual(["1 Dependabot PR in 1 repo"]);
    expect(line("Drafts")!.items.map((i) => i.text)).toEqual(["web #14"]);
    expect(line("Recent branches")!.items[0]!.text).toBe("1 branch with no PR, last commit within 14 days");
    expect(line("Kept")!.items[0]!.text).toBe("1 branch kept on purpose");
    expect(line("View only")!.items).toEqual([
      { text: "legacy 3 PRs, 1 alert", url: "https://github.com/northwind/legacy" },
    ]);
    expect(ready).toBe(1);
    expect(oldBranches).toBe(1);
  });

  it("thresholds: an alert is stuck after 24 hours, a branch old after 14 days", () => {
    expect(STUCK_HOURS).toBe(24);
    expect(OLD_BRANCH_DAYS).toBe(14);
    expect(SWEEP_LATE_HOURS).toBe(8);
    const waiting = (ageMs: number, fields: Partial<GitAlert> = {}) =>
      calm({ repos: [repo("contoso/web", { alerts: [alert(1, "undici", "waiting", { waiting_since: before(ageMs), created_at: before(ageMs), ...fields })] })] });
    // Stuck alerts are a Needs you row; only the ones still inside the lag stay in the quiet box.
    expect(quietOf(waiting(24 * HOUR)).map((l) => l.who)).toEqual(["GitHub"]);
    expect(quietOf(waiting(24 * HOUR + 1))).toEqual([]);
    expect(quietOf(waiting(30 * HOUR, { waiting_since: null }))).toEqual([]);
    // An older server sends neither date: nothing says it is stuck.
    expect(quietOf(waiting(30 * HOUR, { waiting_since: undefined, created_at: undefined })).map((l) => l.who)).toEqual(["GitHub"]);

    const branches = (ageMs: number | null) => calm({ repos: [repo("contoso/web", { branches: [branch("feat/x", ageMs)] })] });
    expect(analyze(branches(14 * DAY), NOW).oldBranches).toBe(0);
    expect(analyze(branches(14 * DAY + 1), NOW).oldBranches).toBe(1);
    expect(analyze(branches(null), NOW).oldBranches).toBe(0);
  });

  it("does not count view-only repos in the quiet counts", () => {
    const data = calm({
      repos: [
        repo("contoso/web", { prs: [pr(1)] }),
        repo("northwind/legacy", { view_only: true, alerts: [alert(9, "x", "fix")], prs: [pr(4)] }),
      ],
    });
    const { ready, quiet } = analyze(data, NOW);
    expect(ready).toBe(1);
    expect(quiet.map((l) => l.who)).toEqual(["View only"]);
  });

  it("sends a Dependabot PR to the Sweep line only when it is BEHIND or UNKNOWN", () => {
    const bot = (n: number, state: string, checks = "SUCCESS") =>
      pr(n, { author: "dependabot[bot]", merge_state: state, checks });
    const sweepOnly = (state: string) =>
      calm({ repos: [repo("contoso/web", { prs: [bot(1, state)] })] });
    for (const state of ["BEHIND", "UNKNOWN"]) {
      expect(quietOf(sweepOnly(state))[0]!.who).toBe("Sweep");
    }
    // BLOCKED, failing and conflicting ones are rows for the owner, not quiet lines.
    expect(quietOf(sweepOnly("BLOCKED"))).toEqual([]);
    expect(quietOf(calm({ repos: [repo("contoso/web", { prs: [bot(1, "BEHIND", "FAILURE")] })] }))).toEqual([]);
    expect(quietOf(calm({ repos: [repo("contoso/web", { prs: [bot(1, "DIRTY")] })] }))).toEqual([]);
    const pending = calm({ repos: [repo("contoso/web", { prs: [bot(1, "BEHIND", "PENDING")] })] });
    expect(quietOf(pending)[0]!.who).toBe("CI");
  });

  it("copes with a pull request whose author is null", () => {
    const data = calm({ repos: [repo("contoso/web", { prs: [pr(1, { author: null, merge_state: "BEHIND" })] })] });
    expect(quietOf(data)).toEqual([]);
  });

  it("names three drafts, then how many more", () => {
    const drafts = [1, 2, 3, 4, 5].map((n) => pr(n, { draft: true }));
    const data = calm({ repos: [repo("contoso/web", { prs: drafts })] });
    const line = analyze(data, NOW).quiet[0]!;
    expect(line.items.map((i) => i.text)).toEqual(["web #1", "web #2", "web #3"]);
    expect(line.more).toBe(2);
  });

  it("does not count a kept branch as old, however old", () => {
    const data = calm({ repos: [repo("contoso/web", { branches: [branch("keep", 500 * DAY, { kept: true })] })] });
    expect(analyze(data, NOW).oldBranches).toBe(0);
    expect(quietOf(data)[0]!.who).toBe("Kept");
  });

  it("no longer builds Needs you rows (the server does)", () => {
    expect(Object.keys(analyze(busy(), NOW)).sort()).toEqual(["oldBranches", "quiet", "ready"]);
  });
});

describe("the summary and the tile", () => {
  const withGit = (n: number, urgent = n) =>
    setAttention(
      overview([
        ...many(urgent, "pr-failing"),
        ...many(n - urgent, "pr-update", { tone: "review" }),
        item("box-x", { source: "box", kind: "heat" }),
      ]),
    );

  it("says how many need you and how many alerts to fix, from the Needs you count", () => {
    expect(summaryWords(busy())).toBe("1 alert to fix");
    withGit(9);
    expect(summaryWords(busy())).toBe("9 need you, 1 alert to fix");
    withGit(1);
    expect(summaryWords(calm())).toBe("1 needs you, 0 alerts to fix");
    setAttention(overview([item("box-x", { source: "box", kind: "heat" })]));
    expect(summaryWords(calm())).toBe("Nothing needs you");
  });

  it("is a problem with an urgent item, attention with only review items, ok when clear", () => {
    withGit(9);
    expect(gitTile(busy(), null)).toMatchObject({ health: "problem", note: "1 to fix, 3 waiting on GitHub", value: "9 need you", brief: "9 need you" });
    withGit(1, 0);
    expect(gitTile(calm(), null)).toMatchObject({ health: "attention", value: "1 need you" });
    setAttention(overview([]));
    expect(gitTile(calm(), null)).toMatchObject({ health: "ok", value: "All clear", brief: "Git clear" });
  });

  it("does not count an item a working chat is on, but counts one a finished chat holds", () => {
    const hold = { session_id: "s1", title: "Chat", path: "/s/s1" };
    setAttention(overview([item("a", { working: hold })]));
    expect(gitTile(calm(), null)).toMatchObject({ health: "ok", value: "All clear" });
    setAttention(overview([item("a", { working: { ...hold, status: "waiting" } })]));
    expect(gitTile(calm(), null)).toMatchObject({ health: "problem", value: "1 need you" });
  });

  it("waits for the Needs you read before it says anything about the count", () => {
    expect(gitTile(calm(), null)).toMatchObject({ health: "idle", value: "Reading..." });
  });

  it("is at least attention when the answer carries an error or the sweep could not be read", () => {
    setAttention(overview([]));
    expect(gitTile(calm({ error: "The sweep config is not readable JSON." }), null)).toMatchObject({ health: "attention", value: "All clear" });
    const sweep = { url: "https://github.com/contoso/automation", runs: [], error: "boom" };
    expect(gitTile(calm({ sweep }), null).health).toBe("attention");
  });

  it("is idle when not set up, and handles no answer", () => {
    expect(gitTile({ ...calm(), configured: false }, null).health).toBe("idle");
    expect(gitTile(null, null)).toMatchObject({ value: "Reading...", health: "idle" });
    expect(gitTile(null, new Error("x"))).toMatchObject({ value: "Unknown", health: "attention" });
    expect(gitTile(null, new AeRequestError(403, { error: { code: "x", message: "Admins only." } }))).toMatchObject({ value: "Admins", health: "idle" });
  });
});

describe("the card", () => {
  /** The panel's answer: *n* Git items (urgent unless *review*), plus one Box item the card ignores. */
  const gitItems = (n: number, review = 0) =>
    setAttention(
      overview([
        ...many(n - review, "pr-failing"),
        ...many(review, "pr-update", { tone: "review" }),
        item("box-x", { source: "box", kind: "heat" }),
      ]),
    );

  it("draws the numbers, one Needs you link and the quiet box", async () => {
    gitItems(9);
    const { context } = stubContext([ok(busy())]);
    const node = await mount(context);

    expect(text(node, ".git-summary")).toBe("9 need you, 1 alert to fix");
    expect(q(node, ".git-summary")?.className).toContain("is-err");
    expect(all(node, ".git-stat").map((s) => s.textContent)).toEqual([
      "1Ready to merge",
      "1Alerts to fix",
      "1Old branches",
      "3Waiting on GitHub",
    ]);
    expect(q(node, '[data-stat="fix"]')?.className).toContain("stat-err");
    expect(q(node, '[data-stat="old"]')?.className).toContain("stat-warn");

    // The rows moved to the Needs you panel; the card says how many and links there.
    expect(q(node, ".git-need")).toBeNull();
    expect(text(node, ".git-needs-wrap .git-h")).toBe("Needs you");
    expect(text(node, ".needs-link-btn")).toBe("9 in Needs you");

    const quiet = all(node, ".git-quiet-line");
    expect(quiet.map((l) => text(l, ".git-quiet-who"))).toEqual(["GitHub", "CI", "Sweep", "Drafts", "Recent branches", "Kept", "View only"]);
    expect(text(quiet[0]!, ".git-quiet-what")).toBe("1 alert in api, fixed on main today");
    expect(text(quiet[6]!, ".git-quiet-what")).toBe("legacy 3 PRs, 1 alert");

    const sweep = q(node, ".git-sweep")!;
    expect(sweep.textContent).toMatch(/^Sweep ran \d{1,2}:\d{2}\s?[AP]M, success$/);
    expect(q<HTMLAnchorElement>(sweep, ".git-link")?.href).toBe("https://github.com/contoso/automation/actions/runs/3");
    expect(q(sweep, ".git-dot")?.className).toContain("git-dot-ok");
    expect(sweep.textContent).not.toMatch(/next/);

    expect(text(node, ".git-toggle")).toBe("Show all 5 repos");
    expect(text(node, ".git-accounts")).toBe("1 of 2 accounts read");
    expect(q(node, ".git-accounts")?.className).toContain("git-warn");
    // The round-1 sections are gone.
    expect(q(node, ".git-acct")).toBeNull();
    expect(q(node, '[data-block="prs"]')).toBeNull();
    expect(getTile("git")?.health).toBe("problem");
    expect(getTile("git")?.value).toBe("9 need you");
  });

  it("opens the Needs you panel filtered to Git from the link", async () => {
    gitItems(2);
    const panel = document.createElement("section");
    panel.id = "needs-you";
    const scroll = vi.fn();
    panel.scrollIntoView = scroll;
    document.body.appendChild(panel);
    const { context } = stubContext([ok(busy())]);
    const node = await mount(context);
    expect(getFilter()).toBeNull();
    q(node, ".needs-link-btn")!.click();
    expect(getFilter()).toBe("git");
    expect(scroll).toHaveBeenCalled();
  });

  it("follows the panel's read: a count arriving later redraws the line, the summary and the tile", async () => {
    const { context } = stubContext([ok(busy())]);
    const node = await mount(context);
    expect(text(node, ".needs-link")).toBe("Reading Needs you...");
    expect(text(node, ".git-summary")).toBe("1 alert to fix");
    gitItems(3);
    expect(text(node, ".needs-link-btn")).toBe("3 in Needs you");
    expect(text(node, ".git-summary")).toBe("3 need you, 1 alert to fix");
    expect(getTile("git")?.value).toBe("3 need you");
  });

  it("says Needs you could not be read when the panel's read failed and nothing came before", async () => {
    setAttention(null, new Error("down"));
    const { context } = stubContext([ok(busy())]);
    const node = await mount(context);
    expect(text(node, ".needs-link")).toBe("Needs you could not be read.");
  });

  it("says Nothing needs you and hides the quiet box when it is empty", async () => {
    setAttention(overview([]));
    const { context } = stubContext([ok(calm())]);
    const node = await mount(context);
    expect(text(node, ".git-summary")).toBe("Nothing needs you");
    expect(text(node, ".needs-link-none")).toBe("Nothing needs you.");
    expect(q(node, ".needs-link-btn")).toBeNull();
    expect(q<HTMLElement>(node, ".git-quiet")?.hidden).toBe(true);
    expect(q(node, ".git-summary")?.className).not.toMatch(/is-/);
    expect(getTile("git")?.health).toBe("ok");
  });

  it("colours the header warn when items need the owner but no alert needs a fix", async () => {
    gitItems(1, 1);
    const { context } = stubContext([ok(calm({ repos: [repo("contoso/web", { prs: [pr(1)] })] }))]);
    const node = await mount(context);
    expect(q(node, ".git-summary")?.className).toContain("is-warn");
  });

  it("shows the setup sentence and an idle tile when not configured", async () => {
    const data = { ...calm(), configured: false, setup: "Set AE_GIT_SWEEP_CONFIG or AE_GIT_REPOS.", repos: [], accounts: [] };
    const { context } = stubContext([ok(data)]);
    const node = await mount(context);
    expect(text(node, ".git-setup")).toMatch(/AE_GIT_SWEEP_CONFIG/);
    expect(q(node, ".git-stat")).toBeNull();
    expect(getTile("git")?.health).toBe("idle");
  });

  it("leaves the sweep line out when it is null and shows its error", async () => {
    const none = stubContext([ok(calm({ sweep: null }))]);
    const a = await mount(none.context);
    expect(q<HTMLElement>(a, ".git-sweep")?.hidden).toBe(true);
    deactivateGit();
    document.body.replaceChildren();
    document.body.appendChild(Object.assign(document.createElement("div"), { id: "root" }));
    const bad = stubContext([ok(calm({ sweep: { url: "https://github.com/contoso/automation", runs: [], error: "Not Found" } }))]);
    const b = await mount(bad.context);
    expect(text(b, ".git-sweep")).toBe("Dependency Sweep could not be read: Not Found");
  });

  it("draws the sweep's dot by its conclusion", async () => {
    const url = "https://github.com/contoso/automation/actions/workflows/dependency-sweep.yml";
    const { context } = stubContext([ok(calm({ sweep: { url, runs: [run(7, HOUR, "failure")], error: null } }))]);
    const node = await mount(context);
    expect(q(node, ".git-sweep .git-dot")?.className).toContain("git-dot-err");
  });

  it("says when the repo list could not be read and when the lists were shortened", async () => {
    const { context } = stubContext([ok(calm({ error: "Sweep config could not be read.", list_cut: true }))]);
    const node = await mount(context);
    expect(text(node, ".git-notes")).toContain("Sweep config could not be read.");
    expect(text(node, ".git-notes")).toContain("Lists shortened to fit");
  });

  it("opens a link through the host, and shows the URL when the host refuses", async () => {
    const { context, openExternal } = stubContext([ok(busy())]);
    const node = await mount(context);
    const link = q<HTMLAnchorElement>(node, '.git-quiet-line[data-who="Drafts"] .git-link')!;
    link.click();
    await flush();
    expect(openExternal).toHaveBeenCalledWith("https://github.com/contoso/web/pull/14");
    openExternal.mockRejectedValueOnce(new Error("PopupBlocked"));
    link.click();
    await flush();
    expect(text(node, ".link-copy")).toBe("https://github.com/contoso/web/pull/14");
  });

  it("keeps the last answer and says so when a later read fails", async () => {
    const { context } = stubContext([ok(busy()), failed(502, "bad gateway")]);
    const node = await mount(context);
    window.dispatchEvent(new Event("focus"));
    await flush();
    expect(text(node, ".git-status")).toMatch(/could not be read/);
    expect(q(node, ".git-stat")).not.toBeNull();
    expect(getTile("git")?.note).toMatch(/last answer/);
  });

  it("takes Ready to merge from the totals, and shows the shortened-lists note", async () => {
    const data = calm({ list_cut: true, totals: { ...TOTALS, repos: 1, prs: 60, prs_ready: 42 } });
    const { context } = stubContext([ok(data)]);
    const node = await mount(context);
    expect(q(node, '[data-stat="ready"]')?.textContent).toBe("42Ready to merge");
    expect(node.textContent).toMatch(/Lists shortened/);
  });

  it("keeps the last-answer mark when the unchanged failure repeats", async () => {
    const { context } = stubContext([ok(busy()), failed(502, "bad gateway"), failed(502, "bad gateway")]);
    await mount(context);
    window.dispatchEvent(new Event("focus"));
    await flush();
    expect(getTile("git")?.note).toMatch(/last answer/);
    window.dispatchEvent(new Event("focus"));
    await flush();
    expect(getTile("git")?.note).toMatch(/last answer/);
  });

  it("says when it cannot read at all", async () => {
    const { context } = stubContext([failed(403, "Admins only.")]);
    const node = await mount(context);
    expect(text(node, ".git-status")).toMatch(/Admins only/);
    expect(getTile("git")?.value).toBe("Admins");
  });
});

describe("Show all", () => {
  const open = async (data: GitOverview, stored?: unknown) => {
    const stub = stubContext([ok(data)], stored);
    const node = await mount(stub.context);
    return { node, ...stub };
  };

  it("is closed by default and opens with the footer button, remembering it", async () => {
    const { node, set } = await open(busy());
    expect(q<HTMLElement>(node, ".git-list")?.hidden).toBe(true);
    q(node, ".git-toggle")!.click();
    expect(q<HTMLElement>(node, ".git-list")?.hidden).toBe(false);
    expect(text(node, ".git-toggle")).toBe("Hide the full list");
    expect(set).toHaveBeenLastCalledWith("git.view", { open: true, chip: "active" });
  });

  it("restores the remembered view, and ignores a stored value it does not understand", async () => {
    const a = await open(busy(), { open: true, chip: "alerts" });
    expect(q<HTMLElement>(a.node, ".git-list")?.hidden).toBe(false);
    expect(q(a.node, ".git-chip.is-on")?.dataset.chip).toBe("alerts");
    expect(a.get).toHaveBeenCalledWith("git.view");
    deactivateGit();
    document.body.replaceChildren();
    document.body.appendChild(Object.assign(document.createElement("div"), { id: "root" }));
    const b = await open(busy(), { open: "yes", chip: "nope" });
    expect(q<HTMLElement>(b.node, ".git-list")?.hidden).toBe(true);
    expect(q(b.node, ".git-chip.is-on")).toBeNull();
  });

  it("works when storage is unavailable", async () => {
    const stub = stubContext([ok(busy())]);
    (stub.get as ReturnType<typeof vi.fn>).mockRejectedValue(new Error("no storage"));
    (stub.set as ReturnType<typeof vi.fn>).mockImplementation(() => {
      throw new Error("no storage");
    });
    const node = await mount(stub.context);
    q(node, ".git-toggle")!.click();
    expect(q<HTMLElement>(node, ".git-list")?.hidden).toBe(false);
  });

  it("counts its chips and shows only repos with something open under Active", async () => {
    const { node } = await open(busy(), { open: true, chip: "active" });
    expect(all(node, ".git-chip").map((c) => c.textContent)).toEqual([
      "Active2",
      "PRs7",
      "Alerts4",
      "Branches3",
      "View only1",
      "All5",
    ]);
    expect(all(node, ".git-group").map((g) => g.dataset.group)).toEqual(["contoso/web", "contoso/api"]);
    expect(text(q(node, '[data-group="contoso/web"]')!, ".git-group-sum")).toBe("7 PRs, 3 alerts, 3 branches");
  });

  it("keeps groups folded, opens one on click, and lists its PRs, packages and branches", async () => {
    const { node } = await open(busy(), { open: true, chip: "active" });
    const web = q(node, '[data-group="contoso/web"]')!;
    expect(web.className).not.toContain("is-open");
    expect(q<HTMLElement>(web, ".git-items")?.hidden).toBe(true);
    q(web, ".git-group-head")!.click();
    const group = q(node, '[data-group="contoso/web"]')!;
    expect(group.className).toContain("is-open");
    const items = all(group, ".git-item");
    expect(items.map((i) => text(i, ".git-link"))).toEqual([
      "#12 Bump thing 12",
      "#13 Bump thing 13",
      "#14 Bump thing 14",
      "#15 Bump thing 15",
      "#16 Bump thing 16",
      "#17 Bump thing 17",
      "#18 Bump thing 18",
      "1 alert, lodash",
      "1 alert, undici",
      "1 alert, @azure/msal-node",
      "feat/old",
      "feat/new",
      "feat/kept",
    ]);
    expect(text(items[0]!, ".git-state")).toBe("Ready");
    expect(text(items[0]!, ".git-meta")).toBe("checks passed · updated 2 days ago");
    expect(text(items[7]!, ".git-meta")).toBe("vulnerable: 1.4.0 installed, fixed in 2.0.0");
    expect(text(items[8]!, ".git-meta")).toBe("fixed on main, waiting on GitHub 3 days");
    expect(text(items[10]!, ".git-meta")).toBe("61 commits, no PR · 2 months");
    expect(text(items[11]!, ".git-meta")).toBe("no PR · 2 days");
    expect(text(items[12]!, ".git-tag")).toBe("kept");
    q(group, ".git-group-head")!.click();
    expect(q(node, '[data-group="contoso/web"]')!.className).not.toContain("is-open");
  });

  it("joins a package's alerts into one line linking the alerts page", async () => {
    const data = calm({
      repos: [repo("contoso/web", { alerts: [alert(1, "@vitest/mocker", "waiting"), alert(2, "@vitest/mocker", "waiting"), alert(3, "@vitest/mocker", "waiting")] })],
    });
    const { node } = await open(data, { open: true, chip: "alerts" });
    q(node, ".git-group-head")!.click();
    const link = q<HTMLAnchorElement>(node, ".git-item .git-link")!;
    expect(link.textContent).toBe("3 alerts, @vitest/mocker");
    expect(link.href).toBe("https://github.com/contoso/web/security/dependabot");
  });

  it("filters by kind and shows quiet and view-only repos only under their own chips", async () => {
    const { node } = await open(busy(), { open: true, chip: "prs" });
    expect(all(node, ".git-group").map((g) => g.dataset.group)).toEqual(["contoso/web"]);
    expect(text(q(node, ".git-group")!, ".git-group-sum")).toBe("7 PRs");
    q(node, '[data-chip="viewonly"]')!.click();
    expect(all(node, ".git-group").map((g) => g.dataset.group)).toEqual(["northwind/legacy"]);
    q(node, '[data-chip="all"]')!.click();
    expect(all(node, ".git-group")).toHaveLength(5);
    expect(text(q(node, '[data-group="fabrikam/site"]')!, ".git-group-sum")).toBe("could not be read");
  });

  it("calls a repo with nothing open quiet", async () => {
    const { node } = await open(calm(), { open: true, chip: "all" });
    expect(text(q(node, ".git-group")!, ".git-group-sum")).toBe("quiet");
  });

  it("searches repos, branches, PR titles and packages, opening every group with a match", async () => {
    const { node, set } = await open(busy(), { open: true, chip: "active" });
    const search = q<HTMLInputElement>(node, ".git-search-input")!;
    const type = (value: string) => {
      search.value = value;
      search.dispatchEvent(new Event("input"));
    };
    type("msal");
    expect(all(node, ".git-group").map((g) => g.dataset.group)).toEqual(["contoso/web"]);
    expect(q(node, ".git-group")!.className).toContain("is-open");
    expect(all(node, ".git-item").map((i) => text(i, ".git-link"))).toEqual(["1 alert, @azure/msal-node"]);
    type("feat/new");
    expect(all(node, ".git-item").map((i) => text(i, ".git-link"))).toEqual(["feat/new"]);
    type("thing 16");
    expect(all(node, ".git-item").map((i) => text(i, ".git-link"))).toEqual(["#16 Bump thing 16"]);
    // A repo name matches the whole repo.
    type("api");
    expect(all(node, ".git-group").map((g) => g.dataset.group)).toEqual(["contoso/api"]);
    expect(all(node, ".git-item")).toHaveLength(1);
    // A click folds just that group while searching.
    q(node, ".git-group-head")!.click();
    expect(q(node, ".git-group")!.className).not.toContain("is-open");
    type("zzz");
    expect(text(node, ".git-empty")).toBe("No repo matches.");
    // Typing is not a view worth remembering; the chip and the toggle are.
    expect(set).not.toHaveBeenCalled();
    q(node, '[data-chip="prs"]')!.click();
    expect(set).toHaveBeenLastCalledWith("git.view", { open: true, chip: "prs" });
  });

  it("keeps focus in the search box while typing", async () => {
    const { node } = await open(busy(), { open: true, chip: "active" });
    const search = q<HTMLInputElement>(node, ".git-search-input")!;
    const before = search;
    search.value = "web";
    search.dispatchEvent(new Event("input"));
    expect(q(node, ".git-search-input")).toBe(before);
  });
});
