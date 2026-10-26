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
  NEEDS_SHOWN,
  OLD_BRANCH_DAYS,
  pullPill,
  STUCK_HOURS,
  SWEEP_LATE_HOURS,
  summaryWords,
  whenShort,
} from "../src/git";
import { getTile, resetTiles } from "../src/tiles";

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
const kinds = (data: GitOverview) => analyze(data, NOW).needs.map((need) => need.kind);

beforeEach(() => {
  resetTiles();
  document.body.replaceChildren();
  const root = document.createElement("div");
  root.id = "root";
  document.body.appendChild(root);
  vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout", "Date"] });
  vi.setSystemTime(NOW);
});

afterEach(() => {
  deactivateGit();
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

describe("who moves next", () => {
  it("sorts err first, then the ready pull request, then warn, each in its rule order", () => {
    expect(kinds(busy())).toEqual([
      "account", // fabrikam has no token
      "repo-error", // northwind/broken; fabrikam/site is explained by the account row
      "alerts-fix",
      "pr-failing",
      "pr-ready",
      "pr-update", // #13 behind
      "pr-update", // #18 blocked
      "alerts-stuck",
      "branch",
    ]);
  });

  it("writes the rows the way the design shows them", () => {
    const rows = analyze(busy(), NOW).needs;
    const row = (kind: string, i = 0) => rows.filter((r) => r.kind === kind)[i]!;
    expect(row("account")).toMatchObject({ title: "fabrikam is not connected", go: "Repo", tone: "err" });
    expect(row("account").meta).toMatch(/AE_GIT_TOKEN_FABRIKAM/);
    expect(row("repo-error")).toMatchObject({ title: "northwind/broken could not be read", go: "Repo" });
    expect(row("alerts-fix")).toMatchObject({
      title: "1 alert to fix: lodash",
      meta: "web · worst is high",
      go: "Alerts",
      url: "https://github.com/contoso/web/security/dependabot",
    });
    expect(row("pr-failing")).toMatchObject({ title: "Fix #16 Bump thing 16", meta: "web · checks failing, conflicts", go: "Open" });
    expect(row("pr-ready")).toMatchObject({
      title: "Merge #12 Bump thing 12",
      meta: "web · checks green, no conflicts",
      tone: "accent",
      url: "https://github.com/contoso/web/pull/12",
    });
    expect(row("pr-update", 0)).toMatchObject({ title: "Update #13 Bump thing 13", meta: "web · behind main" });
    expect(row("pr-update", 1).title).toBe("#18 Bump thing 18 is blocked");
    expect(row("alerts-stuck")).toMatchObject({
      title: "2 alerts fixed on main, still open on GitHub",
      meta: "web · stuck 3 days · a lockfile push or GitHub Support clears them",
      go: "Alerts",
    });
    expect(row("branch")).toMatchObject({
      title: "feat/old has 61 commits and no PR",
      meta: "web · last commit 2 months ago",
      go: "Branch",
      url: "https://github.com/contoso/web/tree/feat/old",
    });
  });

  it("puts ready pull requests behind every err row and ahead of every warn row", () => {
    const data = calm({
      repos: [
        repo("contoso/web", { prs: [pr(1), pr(2, { merge_state: "BEHIND" }), pr(3, { checks: "FAILURE" })] }),
      ],
    });
    expect(kinds(data)).toEqual(["pr-failing", "pr-ready", "pr-update"]);
  });

  it("says a branch has no commit count when ahead is not set", () => {
    const data = calm({ repos: [repo("contoso/web", { branches: [branch("feat/x", 30 * DAY, { ahead: null })] })] });
    expect(analyze(data, NOW).needs[0]!.title).toBe("feat/x, no PR");
    const zero = calm({ repos: [repo("contoso/web", { branches: [branch("feat/x", 30 * DAY, { ahead: 0 })] })] });
    expect(analyze(zero, NOW).needs[0]!.title).toBe("feat/x, no PR");
  });

  it("sorts old branches oldest first across repos", () => {
    const data = calm({
      repos: [
        repo("contoso/web", { branches: [branch("b-mid", 40 * DAY), branch("b-old", 200 * DAY)] }),
        repo("contoso/api", { branches: [branch("b-older", 400 * DAY)] }),
      ],
    });
    expect(analyze(data, NOW).needs.map((n) => n.title.split(",")[0])).toEqual(["b-older", "b-old", "b-mid"]);
  });

  it("thresholds: stuck after 24 hours, old after 14 days, late after 8 hours", () => {
    expect(STUCK_HOURS).toBe(24);
    expect(OLD_BRANCH_DAYS).toBe(14);
    expect(SWEEP_LATE_HOURS).toBe(8);
    const waiting = (ageMs: number, fields: Partial<GitAlert> = {}) =>
      calm({ repos: [repo("contoso/web", { alerts: [alert(1, "undici", "waiting", { waiting_since: before(ageMs), created_at: before(ageMs), ...fields })] })] });
    expect(kinds(waiting(24 * HOUR))).toEqual([]);
    expect(kinds(waiting(24 * HOUR + 1))).toEqual(["alerts-stuck"]);
    // Without waiting_since the alert's own created_at is the clock.
    expect(kinds(waiting(30 * HOUR, { waiting_since: null }))).toEqual(["alerts-stuck"]);
    // An older server sends neither: nothing says it is stuck.
    expect(kinds(waiting(30 * HOUR, { waiting_since: undefined, created_at: undefined }))).toEqual([]);

    const branches = (ageMs: number | null) => calm({ repos: [repo("contoso/web", { branches: [branch("feat/x", ageMs)] })] });
    expect(kinds(branches(14 * DAY))).toEqual([]);
    expect(kinds(branches(14 * DAY + 1))).toEqual(["branch"]);
    expect(kinds(branches(null))).toEqual([]);

    const sweep = (ageMs: number, conclusion: string | null = "success") =>
      calm({ sweep: { url: "https://github.com/contoso/automation/actions/workflows/dependency-sweep.yml", runs: [run(1, ageMs, conclusion)], error: null } });
    expect(kinds(sweep(8 * HOUR))).toEqual([]);
    expect(kinds(sweep(8 * HOUR + 1))).toEqual(["sweep"]);
    expect(analyze(sweep(9 * HOUR), NOW).needs[0]).toMatchObject({ title: "No Dependency Sweep has started in 9 hours", go: "Run" });
    expect(analyze(sweep(HOUR, "failure"), NOW).needs[0]).toMatchObject({ title: "The latest Dependency Sweep failed", go: "Run" });
    expect(kinds(sweep(HOUR, null))).toEqual([]);
  });

  it("leaves the sweep alone when it is null or unreadable", () => {
    expect(kinds(calm({ sweep: null }))).toEqual([]);
    expect(kinds(calm({ sweep: { url: "https://github.com/contoso/automation", runs: [], error: "boom" } }))).toEqual([]);
  });

  it("does not count view-only repos, and groups a repo's fix alerts into one row", () => {
    const data = calm({
      repos: [
        repo("contoso/web", { alerts: [alert(1, "a", "fix"), alert(2, "b", "fix", { severity: "CRITICAL" })] }),
        repo("northwind/legacy", { view_only: true, alerts: [alert(9, "x", "fix")], prs: [pr(4, { checks: "FAILURE" })] }),
      ],
    });
    const rows = analyze(data, NOW).needs;
    expect(rows).toHaveLength(1);
    expect(rows[0]).toMatchObject({ title: "2 alerts to fix: a, b", meta: "web · worst is critical" });
  });

  it("files the rest as lines that are not Reid's to move", () => {
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

  it("makes a Needs-you row of a configured sweep that has no runs", () => {
    const url = "https://github.com/contoso/automation/actions/workflows/dependency-sweep.yml";
    const none = calm({ sweep: { url, runs: [], error: null } });
    expect(analyze(none, NOW).needs).toEqual([
      expect.objectContaining({ kind: "sweep", tone: "err", title: "No Dependency Sweep runs found", url }),
    ]);
    expect(gitTile(none, null, NOW).health).toBe("problem");
  });

  it("copes with a pull request whose author is null", () => {
    const data = calm({ repos: [repo("contoso/web", { prs: [pr(1, { author: null, merge_state: "BEHIND" })] })] });
    expect(kinds(data)).toEqual(["pr-update"]);
  });

  it("sends a Dependabot PR to the Sweep line only when it is BEHIND or UNKNOWN", () => {
    const bot = (n: number, state: string, checks = "SUCCESS") =>
      pr(n, { author: "dependabot[bot]", merge_state: state, checks });
    const sweepOnly = (state: string) =>
      calm({ repos: [repo("contoso/web", { prs: [bot(1, state)] })] });
    for (const state of ["BEHIND", "UNKNOWN"]) {
      expect(kinds(sweepOnly(state))).toEqual([]);
      expect(analyze(sweepOnly(state), NOW).quiet[0]!.who).toBe("Sweep");
    }
    expect(analyze(sweepOnly("BLOCKED"), NOW).needs[0]).toMatchObject({ kind: "pr-update", title: "#1 Bump thing 1 is blocked" });
    expect(kinds(calm({ repos: [repo("contoso/web", { prs: [bot(1, "BEHIND", "FAILURE")] })] }))).toEqual(["pr-failing"]);
    expect(kinds(calm({ repos: [repo("contoso/web", { prs: [bot(1, "DIRTY")] })] }))).toEqual(["pr-failing"]);
    const pending = calm({ repos: [repo("contoso/web", { prs: [bot(1, "BEHIND", "PENDING")] })] });
    expect(analyze(pending, NOW).quiet[0]!.who).toBe("CI");
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
    expect(kinds(data)).toEqual([]);
    expect(analyze(data, NOW).oldBranches).toBe(0);
  });
});

describe("the summary and the tile", () => {
  it("says how many need you and how many alerts to fix", () => {
    expect(summaryWords(busy(), NOW)).toBe("9 need you, 1 alert to fix".replace("10", String(analyze(busy(), NOW).needs.length)));
    expect(summaryWords(calm(), NOW)).toBe("Nothing needs you");
    const one = calm({ repos: [repo("contoso/web", { prs: [pr(1)] })] });
    expect(summaryWords(one, NOW)).toBe("1 needs you, 0 alerts to fix");
  });

  it("is a problem with an err row, attention with only other rows, ok when clear", () => {
    expect(gitTile(busy(), null, NOW)).toMatchObject({ health: "problem", note: "1 to fix, 3 waiting on GitHub" });
    const warn = calm({ repos: [repo("contoso/web", { prs: [pr(2, { merge_state: "BEHIND" })] })] });
    expect(gitTile(warn, null, NOW)).toMatchObject({ health: "attention", value: "1 need you" });
    const ready = calm({ repos: [repo("contoso/web", { prs: [pr(1)] })] });
    expect(gitTile(ready, null, NOW).health).toBe("attention");
    expect(gitTile(calm(), null, NOW)).toMatchObject({ health: "ok", value: "All clear" });
  });

  it("is at least attention when the answer carries an error or the sweep could not be read", () => {
    expect(gitTile(calm({ error: "The sweep config is not readable JSON." }), null, NOW)).toMatchObject({ health: "attention", value: "All clear" });
    const sweep = { url: "https://github.com/contoso/automation", runs: [], error: "boom" };
    expect(gitTile(calm({ sweep }), null, NOW).health).toBe("attention");
  });

  it("is idle when not set up, and handles no answer", () => {
    expect(gitTile({ ...calm(), configured: false }, null).health).toBe("idle");
    expect(gitTile(null, null)).toMatchObject({ value: "Reading...", health: "idle" });
    expect(gitTile(null, new Error("x"))).toMatchObject({ value: "Unknown", health: "attention" });
    expect(gitTile(null, new AeRequestError(403, { error: { code: "x", message: "Admins only." } }))).toMatchObject({ value: "Admins", health: "idle" });
  });
});

describe("the card", () => {
  it("draws the numbers, the first five rows and the quiet box", async () => {
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

    const rows = all(node, ".git-need");
    expect(rows).toHaveLength(NEEDS_SHOWN);
    expect(rows.map((r) => r.dataset.kind)).toEqual(["account", "repo-error", "alerts-fix", "pr-failing", "pr-ready"]);
    expect(rows[4]!.className).toContain("need-accent");
    expect(text(rows[0]!, ".git-go")).toBe("Repo");
    expect(q<HTMLAnchorElement>(rows[4]!, ".git-go")?.href).toBe("https://github.com/contoso/web/pull/12");

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

  it("expands the rest in place with the more button, and folds back", async () => {
    const { context } = stubContext([ok(busy())]);
    const node = await mount(context);
    const more = q(node, ".git-more")!;
    expect(more.textContent).toBe("4 more");
    more.click();
    expect(all(node, ".git-need")).toHaveLength(9);
    expect(text(node, ".git-more")).toBe("Show fewer");
    q(node, ".git-more")!.click();
    expect(all(node, ".git-need")).toHaveLength(NEEDS_SHOWN);
  });

  it("has no more button for five rows or fewer", async () => {
    const data = calm({ repos: [repo("contoso/web", { prs: [pr(1), pr(2), pr(3), pr(4), pr(5)] })] });
    const { context } = stubContext([ok(data)]);
    const node = await mount(context);
    expect(all(node, ".git-need")).toHaveLength(5);
    expect(q(node, ".git-more")).toBeNull();
  });

  it("says Nothing needs you and hides the quiet box when it is empty", async () => {
    const { context } = stubContext([ok(calm())]);
    const node = await mount(context);
    expect(text(node, ".git-summary")).toBe("Nothing needs you");
    expect(text(node, ".git-okline")).toBe("Nothing needs you.");
    expect(q(node, ".git-need")).toBeNull();
    expect(q<HTMLElement>(node, ".git-quiet")?.hidden).toBe(true);
    expect(q(node, ".git-summary")?.className).not.toMatch(/is-/);
    expect(getTile("git")?.health).toBe("ok");
  });

  it("colours the header warn when rows need Reid but no alert needs a fix", async () => {
    const data = calm({ repos: [repo("contoso/web", { prs: [pr(1)] })] });
    const { context } = stubContext([ok(data)]);
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

  it("draws a failed or late sweep as a needs row and its dot by conclusion", async () => {
    const url = "https://github.com/contoso/automation/actions/workflows/dependency-sweep.yml";
    const { context } = stubContext([ok(calm({ sweep: { url, runs: [run(7, HOUR, "failure")], error: null } }))]);
    const node = await mount(context);
    expect(q(node, ".git-need")?.dataset.kind).toBe("sweep");
    expect(text(node, ".git-need-title")).toBe("The latest Dependency Sweep failed");
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
    const link = q<HTMLAnchorElement>(node, '.git-need[data-kind="pr-ready"] .git-go')!;
    link.click();
    await flush();
    expect(openExternal).toHaveBeenCalledWith("https://github.com/contoso/web/pull/12");
    openExternal.mockRejectedValueOnce(new Error("PopupBlocked"));
    link.click();
    await flush();
    expect(text(node, ".link-copy")).toBe("https://github.com/contoso/web/pull/12");
  });

  it("keeps the last answer and says so when a later read fails", async () => {
    const { context } = stubContext([ok(busy()), failed(502, "bad gateway")]);
    const node = await mount(context);
    window.dispatchEvent(new Event("focus"));
    await flush();
    expect(text(node, ".git-status")).toMatch(/could not be read/);
    expect(q(node, ".git-need")).not.toBeNull();
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
