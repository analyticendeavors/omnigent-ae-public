// The Git card's old "who moves next" rules, kept ONLY to pin the server's port
// (packages/ae_omni_policies/src/ae_omni_policies/git_center.py `needs()`), 2026-11-03.
// Production code no longer has these: the card draws the server's list. This copy is
// the TypeScript half of the parity test (gitParity.test.ts); the Python half is
// tests/test_git_center.py. Each row also carries the id the server gives it. Do not edit
// the rules here to follow a new behavior; change both ports and the fixture together.
import type { GitAlert, GitBranch, GitOverview, GitPull, GitRepo } from "../src/api";

/** A waiting alert older than this is stuck: GitHub's alerts normally lag a fix by hours, and
 *  on 2026-10-22 its dependency graph dropped a push and kept 22 fixed alerts open for days. */
const STUCK_HOURS = 24;
/** A branch with no PR, not kept, whose last commit is older than this is an old branch (2026-10-24). */
const OLD_BRANCH_DAYS = 14;
/** The Dependency Sweep runs every 6 hours; no run started for 8 is late (2026-10-24). */
const SWEEP_LATE_HOURS = 8;
/** Needs-you rows shown before the "N more" button. */
/** Names listed on a Not-yours line before "and N more". */
const QUIET_SHOWN = 3;

const HOUR = 3_600_000;
const DAY = 86_400_000;

const plural = (n: number, one: string, many = `${one}s`) => `${n} ${n === 1 ? one : many}`;

// ── words and numbers ───────────────────────────────────────────────────────


/** The head commit's checks as a muted phrase. */
function checksText(checks: string | null): string {
  if (checks === "SUCCESS") return "checks passed";
  if (checks === "FAILURE" || checks === "ERROR") return "checks failing";
  if (checks === "PENDING" || checks === "EXPECTED") return "checks running";
  return "no checks";
}

/** `1:54 PM` today, else `Oct 18`, in the viewer's clock. */
function whenShort(iso: string, now = Date.now()): string {
  const ms = Date.parse(iso);
  if (Number.isNaN(ms)) return "";
  if (new Date(ms).toDateString() === new Date(now).toDateString()) {
    return new Date(ms).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
  }
  return new Date(ms).toLocaleDateString([], { month: "short", day: "numeric" });
}

/** An age: `today`, `3 days`, `5 weeks`, `4 months`, `2 years`. */
function ageText(iso: string | null | undefined, now = Date.now()): string {
  const at = iso ? Date.parse(iso) : Number.NaN;
  if (Number.isNaN(at)) return "";
  const days = Math.max(0, Math.floor((now - at) / DAY));
  if (days === 0) return "today";
  if (days < 14) return plural(days, "day");
  if (days < 60) return plural(Math.floor(days / 7), "week");
  if (days < 730) return plural(Math.floor(days / 30), "month");
  return plural(Math.floor(days / 365), "year");
}

/** `2 days ago`, but `today` stays `today`. */
const ago = (iso: string | null | undefined, now: number) => {
  const age = ageText(iso, now);
  return age === "today" || age === "" ? age : `${age} ago`;
};

const shortName = (repo: string) => repo.slice(repo.indexOf("/") + 1);
const dependabotUrl = (repo: string) => `https://github.com/${repo}/security/dependabot`;
const isDependabot = (pr: GitPull) => (pr.author ?? "").toLowerCase().startsWith("dependabot");
const stamp = (iso: string | null | undefined) => (iso ? Date.parse(iso) : Number.NaN);

const SEVERITY = ["CRITICAL", "HIGH", "MODERATE", "LOW"];

/** "undici, @azure/msal-node" for a repo's alerts, three packages then a count. */
function packageList(alerts: GitAlert[]): string {
  const names = [...new Set(alerts.map((alert) => alert.package))];
  const shown = names.slice(0, 3).join(", ");
  return names.length > 3 ? `${shown} and ${names.length - 3} more` : shown;
}

/** Repos the card acts on: view-only ones are listed on their own and left out of totals. */
const live = (data: GitOverview): GitRepo[] => data.repos.filter((repo) => !repo.view_only);

// ── who moves next ──────────────────────────────────────────────────────────

type NeedTone = "err" | "accent" | "warn";

interface Need {
  id: string;
  kind:
    | "account"
    | "repo-error"
    | "sweep"
    | "alerts-fix"
    | "pr-failing"
    | "pr-ready"
    | "pr-update"
    | "alerts-stuck"
    | "branch";
  tone: NeedTone;
  title: string;
  meta: string;
  /** The link word: Open, Alerts, Branch, Run or Repo. */
  go: string;
  url: string;
}

interface QuietItem {
  text: string;
  url?: string;
}

interface QuietLine {
  who: "GitHub" | "CI" | "Sweep" | "Drafts" | "Recent branches" | "Kept" | "View only";
  items: QuietItem[];
  /** Items past QUIET_SHOWN, said as "and N more". */
  more: number;
  sep: string;
}

interface Analysis {
  /** Worst first: err, then the ready pull requests, then warn. */
  needs: Need[];
  ready: number;
  oldBranches: number;
  quiet: QuietLine[];
}

const RANK: Record<NeedTone, number> = { err: 0, accent: 1, warn: 2 };

function quietLine(who: QuietLine["who"], items: QuietItem[], sep = ", "): QuietLine[] {
  if (!items.length) return [];
  return [{ who, items: items.slice(0, QUIET_SHOWN), more: Math.max(0, items.length - QUIET_SHOWN), sep }];
}

/** Sort everything the card knows into rows for the owner and lines that are not theirs to move. */
function analyze(data: GitOverview, now = Date.now()): Analysis {
  const needs: Need[] = [];
  const github: QuietItem[] = [];
  const ci: QuietItem[] = [];
  const drafts: QuietItem[] = [];
  const recent: GitBranch[] = [];
  const old: Array<{ repo: GitRepo; branch: GitBranch; at: number }> = [];
  const stuckRows: Need[] = [];
  const prRows: Need[] = [];
  let kept = 0;
  let sweepPrs = 0;
  const sweepRepos = new Set<string>();
  let ready = 0;

  // An account without a token says so once; a repo whose error is that note adds nothing.
  const explained = new Set<string>();
  for (const account of data.accounts) {
    if (account.connected) continue;
    const note = account.note ?? "No token for this account.";
    explained.add(note);
    needs.push({
      id: `git:account:${account.owner}`,
      kind: "account",
      tone: "err",
      title: `${account.owner} is not connected`,
      meta: note,
      go: "Repo",
      url: `https://github.com/${account.owner}`,
    });
  }
  for (const repo of live(data)) {
    if (repo.error && !explained.has(repo.error)) {
      needs.push({
        id: `git:repo-error:${repo.repo}`,
        kind: "repo-error",
        tone: "err",
        title: `${repo.repo} could not be read`,
        meta: repo.error,
        go: "Repo",
        url: repo.url,
      });
    }
  }

  const latest = data.sweep?.runs[0];
  if (data.sweep && !latest && !data.sweep.error) {
    // A configured sweep that has never run is as wrong as one that stopped.
    needs.push({
      id: "git:sweep:dependency-sweep",
      kind: "sweep",
      tone: "err",
      title: "No Dependency Sweep runs found",
      meta: "The workflow is set up but has no runs yet",
      go: "Run",
      url: data.sweep.url,
    });
  }
  if (data.sweep && latest) {
    const started = stamp(latest.created_at);
    if (["failure", "timed_out", "startup_failure"].includes(latest.conclusion ?? "")) {
      needs.push({
        id: "git:sweep:dependency-sweep",
        kind: "sweep",
        tone: "err",
        title: "The latest Dependency Sweep failed",
        meta: `Started ${whenShort(latest.created_at, now)}`,
        go: "Run",
        url: latest.url,
      });
    } else if (now - started > SWEEP_LATE_HOURS * HOUR) {
      const hours = Math.floor((now - started) / HOUR);
      needs.push({
        id: "git:sweep:dependency-sweep",
        kind: "sweep",
        tone: "err",
        title: `No Dependency Sweep has started in ${plural(hours, "hour")}`,
        meta: `It runs every 6 hours; the last one started ${whenShort(latest.created_at, now)}`,
        go: "Run",
        url: latest.url,
      });
    }
  }

  for (const repo of live(data)) {
    const name = shortName(repo.repo);
    const fix = repo.alerts.filter((alert) => alert.bucket === "fix");
    if (fix.length) {
      const worst = SEVERITY.find((level) => fix.some((alert) => alert.severity === level));
      needs.push({
        id: `git:alerts-fix:${repo.repo}`,
        kind: "alerts-fix",
        tone: "err",
        title: `${plural(fix.length, "alert")} to fix: ${packageList(fix)}`,
        meta: worst ? `${name} · worst is ${worst.toLowerCase()}` : name,
        go: "Alerts",
        url: dependabotUrl(repo.repo),
      });
    }

    const stuck: GitAlert[] = [];
    const lagging: GitAlert[] = [];
    for (const alert of repo.alerts.filter((a) => a.bucket === "waiting")) {
      const since = stamp(alert.waiting_since ?? alert.created_at);
      (!Number.isNaN(since) && now - since > STUCK_HOURS * HOUR ? stuck : lagging).push(alert);
    }
    const oldestSince = (alerts: GitAlert[]) =>
      alerts
        .map((alert) => alert.waiting_since ?? alert.created_at ?? null)
        .sort((a, b) => stamp(a) - stamp(b))[0];
    if (stuck.length) {
      stuckRows.push({
        id: `git:alerts-stuck:${repo.repo}`,
        kind: "alerts-stuck",
        tone: "warn",
        title: `${plural(stuck.length, "alert")} fixed on ${repo.default_branch}, still open on GitHub`,
        meta: `${name} · stuck ${ageText(oldestSince(stuck), now)} · a lockfile push or GitHub Support clears them`,
        go: "Alerts",
        url: dependabotUrl(repo.repo),
      });
    }
    if (lagging.length) {
      const when = ago(oldestSince(lagging), now);
      github.push({
        text: `${plural(lagging.length, "alert")} in ${name}, fixed on ${repo.default_branch}${when ? ` ${when}` : ""}`,
        url: dependabotUrl(repo.repo),
      });
    }

    for (const pr of repo.prs) {
      const label = `${name} #${pr.number}`;
      if (pr.draft) {
        drafts.push({ text: label, url: pr.url });
        continue;
      }
      const failing = pr.checks === "FAILURE" || pr.checks === "ERROR";
      const conflicts = pr.merge_state === "DIRTY";
      if (pr.merge_state === "CLEAN") ready += 1;
      if (failing || conflicts) {
        prRows.push({
          id: `git:pr-failing:${repo.repo}#${pr.number}`,
          kind: "pr-failing",
          tone: "err",
          title: `Fix #${pr.number} ${pr.title}`,
          meta: `${name} · ${[failing ? "checks failing" : "", conflicts ? "conflicts" : ""].filter(Boolean).join(", ")}`,
          go: "Open",
          url: pr.url,
        });
      } else if (pr.merge_state === "CLEAN") {
        prRows.push({
          id: `git:pr-ready:${repo.repo}#${pr.number}`,
          kind: "pr-ready",
          tone: "accent",
          title: `Merge #${pr.number} ${pr.title}`,
          meta: `${name} · ${pr.checks === "SUCCESS" ? "checks green" : checksText(pr.checks)}, no conflicts`,
          go: "Open",
          url: pr.url,
        });
      } else if (pr.merge_state === "UNSTABLE" || pr.checks === "PENDING") {
        ci.push({ text: `${label} ${checksText("PENDING")}`, url: pr.url });
      } else if (isDependabot(pr) && (pr.merge_state === "BEHIND" || pr.merge_state === "UNKNOWN")) {
        // The sweep rebases and re-reads these on its own; BLOCKED is a review it will not give.
        sweepPrs += 1;
        sweepRepos.add(repo.repo);
      } else if (pr.merge_state === "BEHIND") {
        prRows.push({
          id: `git:pr-update:${repo.repo}#${pr.number}`,
          kind: "pr-update",
          tone: "warn",
          title: `Update #${pr.number} ${pr.title}`,
          meta: `${name} · behind ${repo.default_branch}`,
          go: "Open",
          url: pr.url,
        });
      } else if (pr.merge_state === "BLOCKED") {
        prRows.push({
          id: `git:pr-update:${repo.repo}#${pr.number}`,
          kind: "pr-update",
          tone: "warn",
          title: `#${pr.number} ${pr.title} is blocked`,
          meta: `${name} · waiting on a review or a required check`,
          go: "Open",
          url: pr.url,
        });
      } else {
        // UNKNOWN, HAS_HOOKS or no state yet: GitHub is still working it out.
        ci.push({ text: `${label} ${checksText(pr.checks)}`, url: pr.url });
      }
    }

    for (const branch of repo.branches) {
      if (branch.kept) {
        kept += 1;
        continue;
      }
      const at = stamp(branch.last_commit_at);
      // A branch with no readable date is not called old: nothing says it is.
      if (!Number.isNaN(at) && now - at > OLD_BRANCH_DAYS * DAY) old.push({ repo, branch, at });
      else recent.push(branch);
    }
  }

  old.sort((a, b) => a.at - b.at);
  const branchRows: Need[] = old.map(({ repo, branch }) => ({
    id: `git:branch:${repo.repo}:${branch.name}`,
    kind: "branch",
    tone: "warn",
    title: branch.ahead
      ? `${branch.name} has ${plural(branch.ahead, "commit")} and no PR`
      : `${branch.name}, no PR`,
    meta: `${shortName(repo.repo)} · last commit ${ago(branch.last_commit_at, now)}`,
    go: "Branch",
    url: branch.url,
  }));

  // Array.sort is stable: inside one rank the order built above stands.
  const all = [...needs, ...prRows, ...stuckRows, ...branchRows].sort((a, b) => RANK[a.tone] - RANK[b.tone]);

  const quiet: QuietLine[] = [
    ...quietLine("GitHub", github, "; "),
    ...quietLine("CI", ci),
    ...(sweepPrs
      ? quietLine("Sweep", [{ text: `${plural(sweepPrs, "Dependabot PR")} in ${plural(sweepRepos.size, "repo")}` }])
      : []),
    ...quietLine("Drafts", drafts),
    ...(recent.length
      ? quietLine("Recent branches", [
          { text: `${plural(recent.length, "branch", "branches")} with no PR, last commit within ${OLD_BRANCH_DAYS} days` },
        ])
      : []),
    ...(kept
      ? quietLine("Kept", [{ text: `${plural(data.totals.branches_kept ?? kept, "branch", "branches")} kept on purpose` }])
      : []),
    ...quietLine(
      "View only",
      data.repos
        .filter((repo) => repo.view_only)
        .map((repo) => ({
          text: `${shortName(repo.repo)} ${plural(repo.prs.length, "PR")}, ${plural(repo.alerts.length, "alert")}`,
          url: repo.url,
        })),
      "; ",
    ),
  ];
  return { needs: all, ready, oldBranches: old.length, quiet };
}

export { analyze };
