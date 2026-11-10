// Parity between the Git card's old TypeScript rules (kept in legacyGitRules.ts) and the server's
// port (git_center.needs). Both halves check one golden file, test/fixtures/git-parity.json:
// this test fails if the old rules stop giving the file's ids, or if the file stops matching the
// scenarios built here; tests/test_git_center.py fails if the Python port stops giving the same
// ids. To change a rule, change both ports and replace the file with the JSON of `golden()` below
// (print it with console.log(JSON.stringify(golden(), null, 2)) in a scratch run) (2026-11-03).
import { describe, expect, it } from "vitest";
import type { GitOverview } from "../src/api";
import goldenText from "./fixtures/git-parity.json?raw";
import { alert, before, branch, busy, calm, DAY, HOUR, NOW, pr, repo, run, TOTALS } from "./gitFixtures";
import { analyze } from "./legacyGitRules";


const SCENARIOS: Record<string, GitOverview> = {
  busy: busy(),
  calm: calm(),
  thresholds: calm({
    repos: [
      repo("contoso/web", {
        alerts: [
          alert(1, "a", "waiting", { waiting_since: before(25 * HOUR) }),
          alert(2, "b", "waiting", { waiting_since: before(23 * HOUR) }),
        ],
        branches: [
          branch("old-one", 15 * DAY),
          branch("older", 40 * DAY, { ahead: 2 }),
          branch("fresh", 13 * DAY),
          branch("undated", null),
        ],
      }),
    ],
    sweep: {
      url: "https://github.com/contoso/automation/actions/workflows/dependency-sweep.yml",
      runs: [run(9, 9 * HOUR)],
      error: null,
    },
  }),
  "view-only and drafts": calm({
    totals: { ...TOTALS, repos: 1 },
    repos: [
      repo("contoso/web", {
        prs: [pr(1, { draft: true }), pr(2, { author: null as unknown as string })],
        alerts: [
          { ...alert(1, "x", "fix"), severity: "LOW" },
          { ...alert(2, "y", "fix"), severity: "CRITICAL" },
        ],
      }),
      repo("northwind/legacy", { view_only: true, prs: [pr(1)], alerts: [alert(9, "x", "fix")] }),
    ],
  }),
  "dependabot sweep rules": calm({
    repos: [
      repo("contoso/web", {
        prs: [
          pr(1, { merge_state: "BEHIND", author: "dependabot[bot]" }),
          pr(2, { merge_state: "UNKNOWN", author: "dependabot[bot]" }),
          pr(3, { merge_state: "BLOCKED", author: "dependabot[bot]" }),
          pr(4, { merge_state: "UNKNOWN" }),
          pr(5, { merge_state: "HAS_HOOKS", checks: "PENDING" }),
          pr(6, { merge_state: "DIRTY", checks: "SUCCESS" }),
          pr(7, { merge_state: "BLOCKED", checks: "FAILURE" }),
        ],
      }),
    ],
  }),
  "sweep failed, none, and broken": calm({
    sweep: {
      url: "https://github.com/contoso/automation/actions/workflows/dependency-sweep.yml",
      runs: [run(5, HOUR, "failure")],
      error: null,
    },
  }),
  "sweep never ran": calm({
    sweep: {
      url: "https://github.com/contoso/automation/actions/workflows/dependency-sweep.yml",
      runs: [],
      error: null,
    },
  }),
  "sweep unreadable": calm({
    sweep: {
      url: "https://github.com/contoso/automation/actions/workflows/dependency-sweep.yml",
      runs: [],
      error: "The Actions read was refused.",
    },
  }),
  "no sweep": calm({ sweep: null }),
};

const toneOf = (tone: string) => (tone === "err" ? "urgent" : "review");

function expected() {
  return Object.fromEntries(
    Object.entries(SCENARIOS).map(([name, overview]) => [
      name,
      analyze(overview, NOW).needs.map((need) => ({ id: need.id, tone: toneOf(need.tone) })),
    ]),
  );
}

const golden = () => ({
  now: NOW,
  scenarios: Object.fromEntries(
    Object.entries(SCENARIOS).map(([name, overview]) => [name, { overview }]),
  ),
  expected: expected(),
});

describe("git rules parity", () => {
  it("the golden file holds the scenarios and the old rules' ids", () => {
    const onDisk = JSON.parse(goldenText);
    expect(onDisk).toEqual(JSON.parse(JSON.stringify(golden())));
  });

  it("covers every kind of row the old rules can make", () => {
    const kinds = new Set(
      Object.values(SCENARIOS).flatMap((overview) => analyze(overview, NOW).needs.map((n) => n.kind)),
    );
    expect([...kinds].sort()).toEqual(
      ["account", "alerts-fix", "alerts-stuck", "branch", "pr-failing", "pr-ready", "pr-update", "repo-error", "sweep"].sort(),
    );
  });
});
