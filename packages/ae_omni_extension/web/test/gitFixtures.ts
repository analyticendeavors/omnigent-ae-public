// Git card fixtures shared by the parity test (gitParity.test.ts). Owners are made up
// (contoso, fabrikam, northwind) and no person's name appears.
import type { GitAlert, GitBranch, GitOverview, GitPull, GitRepo, GitRun } from "../src/api";

export const NOW = Date.parse("2026-10-24T20:00:00Z");
export const HOUR = 3_600_000;
export const DAY = 24 * HOUR;
export const before = (ms: number) => new Date(NOW - ms).toISOString();

export const pr = (n: number, fields: Partial<GitPull> = {}): GitPull => ({
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

export const alert = (
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

export const branch = (name: string, ageMs: number | null, fields: Partial<GitBranch> = {}): GitBranch => ({
  name,
  url: `https://github.com/contoso/web/tree/${name}`,
  last_commit_at: ageMs === null ? null : before(ageMs),
  ahead: null,
  kept: false,
  ...fields,
});

export function repo(name: string, fields: Partial<GitRepo> = {}): GitRepo {
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

export const run = (id: number, ageMs: number, conclusion: string | null = "success"): GitRun => ({
  id,
  url: `https://github.com/contoso/automation/actions/runs/${id}`,
  created_at: before(ageMs),
  status: conclusion ? "completed" : "in_progress",
  conclusion,
});

export const TOTALS = { repos: 0, prs: 0, prs_ready: 0, alerts_fix: 0, alerts_waiting: 0, branches: 0, repo_errors: 0 };

/** A quiet, connected answer; each test adds what it is about. */
export function calm(fields: Partial<GitOverview> = {}): GitOverview {
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
export function busy(): GitOverview {
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
