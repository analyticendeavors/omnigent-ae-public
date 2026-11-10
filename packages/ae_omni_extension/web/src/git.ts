// The Admin Center's Git repos card (2026-10-24): every repo across the
// GitHub accounts the box can read, sorted by who moves next. Since 2026-11-03 the
// "who moves next" rows are listed once, in the Needs you panel (attention.ts, built
// on the server from the same rules); the card keeps one line, "N in Needs you", that
// opens the panel filtered to it. GitHub lag, CI, drafts, the
// sweep's own pull requests and view-only repos still fold into one quiet "Not
// yours to move" box here. Same shape as the Key Vault card (vault.ts): four
// numbers, Needs you, a Show all list with search and chips. Data comes from
// `GET /v1/ae/git` (ae_omni_policies/git.py, admins only).
//
// Why: the owner keeps repos under several accounts; the pull requests, stale
// branches and alerts GitHub has not cleared lived on four different pages,
// and a list of everything hid the two things that needed them. This card is
// read only: every row is a link to GitHub, and the only buttons are the
// card's own (more, Show all, the chips) and the page's refresh. Links open
// the way the pull request cards open theirs (openLink.ts): through the host,
// and when it refuses, the URL shows as text to copy.
//
// The card reads on activate and on focus, and when the page header's refresh
// button runs; the server caches the answer for five minutes. After every
// draw it hands the page its Git tile. Whether Show all is open and which
// chip is on are remembered in `storage.user` (key `git.view`), the way
// vault.ts remembers its own.

import type { ExtensionContext } from "@omnigent/extension-sdk";
import {
  AeRequestError,
  describeError,
  fetchGit,
  type GitAlert,
  type GitOverview,
  type GitPull,
  type GitRepo,
  type GitRun,
  type GitScope,
} from "./api";
import { attentionCount, needsLink, onAttention, sourceRead, urgentCount } from "./attentionStore";
import { el, notice, replaceChildren } from "./dom";
import { chevronIcon, searchIcon } from "./icons";
import { openOrShow } from "./openLink";
import { type Health, registerRefresh, setTile, staleTile, type Tile } from "./tiles";

/** A waiting alert older than this is stuck: GitHub's alerts normally lag a fix by hours, and
 *  on 2026-10-22 its dependency graph dropped a push and kept 22 fixed alerts open for days. */
export const STUCK_HOURS = 24;
/** A branch with no PR, not kept, whose last commit is older than this is an old branch (2026-10-24). */
export const OLD_BRANCH_DAYS = 14;
/** The Dependency Sweep runs every 6 hours; no run started for 8 is late (2026-10-24). */
export const SWEEP_LATE_HOURS = 8;
/** Names listed on a Not-yours line before "and N more". */
const QUIET_SHOWN = 3;

const HOUR = 3_600_000;
const DAY = 86_400_000;
const VIEW_KEY = "git.view";
/** The Company, Mine, All switch, remembered per viewer (2026-11-08). */
const SCOPE_KEY = "git.scope";

const plural = (n: number, one: string, many = `${one}s`) => `${n} ${n === 1 ? one : many}`;

// ── words and numbers ───────────────────────────────────────────────────────

export type Tone = "ok" | "warn" | "err" | "muted";

/** The state pill of a pull request: its words and colour. */
export function pullPill(pr: Pick<GitPull, "draft" | "merge_state" | "checks">): {
  label: string;
  tone: Tone;
} {
  if (pr.draft) return { label: "Draft", tone: "muted" };
  if (pr.merge_state === "CLEAN") return { label: "Ready", tone: "ok" };
  if (pr.merge_state === "BEHIND") return { label: "Behind", tone: "warn" };
  if (pr.merge_state === "DIRTY") return { label: "Conflicts", tone: "err" };
  if (pr.checks === "FAILURE" || pr.checks === "ERROR") {
    return { label: "Checks failing", tone: "err" };
  }
  if (pr.merge_state === "UNSTABLE" || pr.checks === "PENDING") {
    return { label: "Checks running", tone: "warn" };
  }
  if (pr.merge_state === "BLOCKED") return { label: "Blocked", tone: "warn" };
  return { label: (pr.merge_state ?? "UNKNOWN").toLowerCase(), tone: "muted" };
}

/** The head commit's checks as a muted phrase. */
export function checksText(checks: string | null): string {
  if (checks === "SUCCESS") return "checks passed";
  if (checks === "FAILURE" || checks === "ERROR") return "checks failing";
  if (checks === "PENDING" || checks === "EXPECTED") return "checks running";
  return "no checks";
}

/** `1:54 PM` today, else `Oct 18`, in the viewer's clock. */
export function whenShort(iso: string, now = Date.now()): string {
  const ms = Date.parse(iso);
  if (Number.isNaN(ms)) return "";
  if (new Date(ms).toDateString() === new Date(now).toDateString()) {
    return new Date(ms).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
  }
  return new Date(ms).toLocaleDateString([], { month: "short", day: "numeric" });
}

/** An age: `today`, `3 days`, `5 weeks`, `4 months`, `2 years`. */
export function ageText(iso: string | null | undefined, now = Date.now()): string {
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

/** A run's outcome in words: its conclusion, else where it is. */
function runWords(run: GitRun): string {
  return (run.conclusion ?? run.status).replace(/_/g, " ");
}

function runTone(run: GitRun): Tone {
  if (run.conclusion === "success") return "ok";
  if (run.conclusion === "failure" || run.conclusion === "timed_out") return "err";
  return "muted";
}

/** Repos the card acts on: view-only ones are listed on their own and left out of totals. */
const live = (data: GitOverview): GitRepo[] => data.repos.filter((repo) => !repo.view_only);

// ── the quiet lines ─────────────────────────────────────────────────────────
// The rows for the owner moved to the server (git_center.py `needs()`) and the Needs you
// panel. What stays here is the other half of the old sort: the lines that are not the
// owner's to move, and the two numbers the stats row needs.

export interface QuietItem {
  text: string;
  url?: string;
}

export interface QuietLine {
  who: "GitHub" | "CI" | "Sweep" | "Drafts" | "Recent branches" | "Kept" | "View only";
  items: QuietItem[];
  /** Items past QUIET_SHOWN, said as "and N more". */
  more: number;
  sep: string;
}

export interface Analysis {
  ready: number;
  oldBranches: number;
  quiet: QuietLine[];
}

function quietLine(who: QuietLine["who"], items: QuietItem[], sep = ", "): QuietLine[] {
  if (!items.length) return [];
  return [{ who, items: items.slice(0, QUIET_SHOWN), more: Math.max(0, items.length - QUIET_SHOWN), sep }];
}

/** Sort what is not the owner's to move into lines, and count the ready pull requests and old branches. */
export function analyze(data: GitOverview, now = Date.now()): Analysis {
  const github: QuietItem[] = [];
  const ci: QuietItem[] = [];
  const drafts: QuietItem[] = [];
  let recent = 0;
  let old = 0;
  let kept = 0;
  let sweepPrs = 0;
  const sweepRepos = new Set<string>();
  let ready = 0;

  for (const repo of live(data)) {
    const name = shortName(repo.repo);
    const lagging: GitAlert[] = [];
    for (const alert of repo.alerts.filter((a) => a.bucket === "waiting")) {
      const since = stamp(alert.waiting_since ?? alert.created_at);
      if (!(!Number.isNaN(since) && now - since > STUCK_HOURS * HOUR)) lagging.push(alert);
    }
    if (lagging.length) {
      const oldest = lagging
        .map((alert) => alert.waiting_since ?? alert.created_at ?? null)
        .sort((a, b) => stamp(a) - stamp(b))[0];
      const when = ago(oldest, now);
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
      if (failing || conflicts || pr.merge_state === "CLEAN") continue;
      if (pr.merge_state === "UNSTABLE" || pr.checks === "PENDING") {
        ci.push({ text: `${label} ${checksText("PENDING")}`, url: pr.url });
      } else if (isDependabot(pr) && (pr.merge_state === "BEHIND" || pr.merge_state === "UNKNOWN")) {
        // The sweep rebases and re-reads these on its own; BLOCKED is a review it will not give.
        sweepPrs += 1;
        sweepRepos.add(repo.repo);
      } else if (pr.merge_state === "BEHIND" || pr.merge_state === "BLOCKED") {
        // A row for the owner: the Needs you panel lists it.
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
      if (!Number.isNaN(at) && now - at > OLD_BRANCH_DAYS * DAY) old += 1;
      else recent += 1;
    }
  }

  const quiet: QuietLine[] = [
    ...quietLine("GitHub", github, "; "),
    ...quietLine("CI", ci),
    ...(sweepPrs
      ? quietLine("Sweep", [{ text: `${plural(sweepPrs, "Dependabot PR")} in ${plural(sweepRepos.size, "repo")}` }])
      : []),
    ...quietLine("Drafts", drafts),
    ...(recent
      ? quietLine("Recent branches", [
          { text: `${plural(recent, "branch", "branches")} with no PR, last commit within ${OLD_BRANCH_DAYS} days` },
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
  return { ready, oldBranches: old, quiet };
}

/** "8 need you, 0 alerts to fix": the header line; "Nothing needs you" when it is quiet. */
export function summaryWords(data: GitOverview): string {
  const n = attentionCount("git");
  const fix = plural(data.totals.alerts_fix, "alert");
  if (n === null) return `${fix} to fix`;
  if (n === 0) return "Nothing needs you";
  return `${n === 1 ? "1 needs you" : `${n} need you`}, ${fix} to fix`;
}

export function gitTile(data: GitOverview | null, error: unknown): Tile {
  if (!data) {
    const forbidden = error instanceof AeRequestError && error.status === 403;
    return {
      id: "git",
      value: forbidden ? "Admins" : error ? "Unknown" : "Reading...",
      note: forbidden ? "admins only" : error ? "could not be read" : "",
      health: error && !forbidden ? "attention" : "idle",
    };
  }
  if (!data.configured) {
    return { id: "git", value: "Not set up", note: "no repos listed", health: "idle", notSetUp: true };
  }
  // The count is the panel's (the server's rules); before its first read the tile waits for it.
  const n = attentionCount("git");
  const urgent = urgentCount("git") ?? 0;
  const health: Health =
    urgent > 0
      ? "problem"
      : (n ?? 0) > 0 || data.error || data.sweep?.error
        ? "attention"
        : n === null
          ? "idle"
          : "ok";
  return {
    id: "git",
    value: n === null ? "Reading..." : n ? `${n} need you` : "All clear",
    summary: summaryWords(data),
    brief: n === null ? undefined : n ? (n === 1 ? "1 needs you" : `${n} need you`) : "Git clear",
    note: `${data.totals.alerts_fix} to fix, ${data.totals.alerts_waiting} waiting on GitHub`,
    health,
  };
}

// ── remembered view ─────────────────────────────────────────────────────────

export type Chip = "active" | "prs" | "alerts" | "branches" | "viewonly" | "all";

const CHIPS: Array<{ id: Chip; label: string }> = [
  { id: "active", label: "Active" },
  { id: "prs", label: "PRs" },
  { id: "alerts", label: "Alerts" },
  { id: "branches", label: "Branches" },
  { id: "viewonly", label: "View only" },
  { id: "all", label: "All" },
];

interface Remembered {
  open: boolean;
  chip: Chip;
  scope: GitScope;
}

const SCOPES: Array<{ id: GitScope; label: string; title: string }> = [
  { id: "company", label: "Company", title: "The company's repos, read with the company's tokens" },
  { id: "mine", label: "Mine", title: "Your own repos, read with your own GitHub tokens" },
  { id: "all", label: "All", title: "Both lists; a repo in both shows once, under Company" },
];

const isScope = (value: unknown): value is GitScope => SCOPES.some((scope) => scope.id === value);

const isChip = (value: unknown): value is Chip => CHIPS.some((chip) => chip.id === value);

/** Read the remembered view; the defaults without storage or with something odd in it. */
async function loadView(context: ExtensionContext): Promise<Remembered> {
  const view: Remembered = { open: false, chip: "active", scope: "all" };
  try {
    const found = await context.storage.user.get<unknown>(VIEW_KEY);
    if (found && typeof found === "object" && !Array.isArray(found)) {
      const { open, chip } = found as { open?: unknown; chip?: unknown };
      if (typeof open === "boolean") view.open = open;
      if (isChip(chip)) view.chip = chip;
    }
  } catch {
    // No storage: the defaults, kept in memory until the page closes.
  }
  try {
    const scope = await context.storage.user.get<unknown>(SCOPE_KEY);
    if (isScope(scope)) view.scope = scope;
  } catch {
    // The same: All, kept in memory until the page closes.
  }
  return view;
}

function saveScope(context: ExtensionContext, scope: GitScope): void {
  try {
    context.storage.user.set(SCOPE_KEY, scope).catch(() => {
      // storage.user unavailable: kept in memory until the page closes.
    });
  } catch {
    // Refused before a promise was made.
  }
}

function saveView(context: ExtensionContext, view: Remembered): void {
  try {
    context.storage.user.set(VIEW_KEY, { open: view.open, chip: view.chip }).catch(() => {
      // storage.user unavailable: kept in memory until the page closes.
    });

  } catch {
    // The same, refused before a promise was made.
  }
}

// ── drawing ─────────────────────────────────────────────────────────────────

interface State {
  data: GitOverview | null;
  error: unknown;
  at: number;
  open: boolean;
  chip: Chip;
  /** Which list the card shows (Company, Mine, All). */
  scope: GitScope;
  /** Called by the switch; set by activateGit. */
  switchScope: (scope: GitScope) => void;
  query: string;
  openGroups: Set<string>;
  closedGroups: Set<string>;
}

interface Parts {
  root: HTMLElement;
  scope: HTMLElement;
  summary: HTMLElement;
  status: HTMLElement;
  refreshed: HTMLElement;
  notes: HTMLElement;
  stats: HTMLElement;
  needs: HTMLElement;
  quiet: HTMLElement;
  sweep: HTMLElement;
  footer: HTMLElement;
  list: HTMLElement;
  search: HTMLInputElement;
  chips: HTMLElement;
  groups: HTMLElement;
}

/** A link to GitHub that opens through the host and falls back to showing the URL. */
function ghLink(
  context: ExtensionContext,
  root: HTMLElement,
  url: string,
  className: string,
  ...children: Array<Node | string>
): HTMLElement {
  if (!url.startsWith("https://github.com/")) return el("span", { class: className }, ...children);
  const link = el(
    "a",
    { class: className, href: url, target: "_blank", rel: "noopener noreferrer" },
    ...children,
  );
  link.addEventListener("click", (event) => {
    event.preventDefault();
    void openOrShow(context, url, root);
  });
  return link;
}

type Linker = (url: string, className: string, ...children: Array<Node | string>) => HTMLElement;

function dot(tone: Tone): HTMLElement {
  return el("span", { class: `git-dot git-dot-${tone}`, "aria-hidden": "true" });
}

function drawStats(parts: Parts, data: GitOverview, analysis: Analysis): void {
  const stat = (n: number, label: string, tone: string, key: string) =>
    el(
      "div",
      { class: `git-stat stat-${tone}`, "data-stat": key },
      el("b", { class: "git-stat-n" }, String(n)),
      el("span", { class: "git-stat-l" }, label),
    );
  const fix = data.totals.alerts_fix;
  replaceChildren(parts.stats, [
    stat(data.totals.prs_ready, "Ready to merge", "accent", "ready"),
    stat(fix, "Alerts to fix", fix > 0 ? "err" : "ok", "fix"),
    stat(analysis.oldBranches, "Old branches", analysis.oldBranches > 0 ? "warn" : "muted", "old"),
    stat(data.totals.alerts_waiting, "Waiting on GitHub", "muted", "waiting"),
  ]);
}

function drawNeeds(parts: Parts, state: State): void {
  // Personal items are their own source (git-mine): only their owner is ever sent them, and they
  // take Dismiss and Open on GitHub only, so they get their own line here.
  const mine = state.scope === "company" ? 0 : (attentionCount("git-mine") ?? 0);
  replaceChildren(parts.needs, [
    el("h3", { class: "git-h" }, "Needs you"),
    state.scope === "mine" ? null : needsLink("git"),
    mine > 0 || state.scope === "mine"
      ? el("div", { class: "git-needs-mine" }, el("span", { class: "meta" }, "My Git repos: "), needsLink("git-mine"))
      : null,
  ]);
}

function drawScope(parts: Parts, state: State): void {
  replaceChildren(parts.scope, [
    ...SCOPES.map(({ id, label, title }) => {
      const on = state.scope === id;
      const node = el(
        "button",
        {
          type: "button",
          class: `git-scope-btn${on ? " is-on" : ""}`,
          "aria-pressed": String(on),
          "data-scope": id,
          title,
        },
        label,
      );
      node.addEventListener("click", () => {
        if (state.scope !== id) state.switchScope(id);
      });
      return node;
    }),
  ]);
}

function drawQuiet(parts: Parts, analysis: Analysis, link: Linker): void {
  if (!analysis.quiet.length) {
    replaceChildren(parts.quiet, []);
    parts.quiet.hidden = true;
    return;
  }
  parts.quiet.hidden = false;
  replaceChildren(parts.quiet, [
    el("h3", { class: "git-h" }, "Not yours to move"),
    ...analysis.quiet.map((line) => {
      const what: Array<Node | string> = [];
      line.items.forEach((item, i) => {
        if (i) what.push(line.sep);
        what.push(item.url ? link(item.url, "git-link", item.text) : item.text);
      });
      if (line.more) what.push(`${line.sep === "; " ? "; " : ", "}and ${line.more} more`);
      return el(
        "div",
        { class: "git-quiet-line", "data-who": line.who },
        el("span", { class: "git-quiet-who" }, line.who),
        el("span", { class: "git-quiet-what" }, ...what),
      );
    }),
  ]);
}

function drawSweep(parts: Parts, data: GitOverview, now: number, link: Linker): void {
  const sweep = data.sweep;
  parts.sweep.hidden = !sweep;
  if (!sweep) {
    replaceChildren(parts.sweep, []);
    return;
  }
  const [latest] = sweep.runs;
  if (sweep.error) {
    replaceChildren(parts.sweep, [`Dependency Sweep could not be read: ${sweep.error}`]);
    parts.sweep.className = "git-sweep git-warn";
  } else if (!latest) {
    replaceChildren(parts.sweep, [link(sweep.url, "git-link", "Dependency Sweep"), " has no runs yet."]);
    parts.sweep.className = "git-sweep git-meta";
  } else {
    parts.sweep.className = "git-sweep";
    parts.sweep.setAttribute("data-conclusion", latest.conclusion ?? latest.status);
    replaceChildren(parts.sweep, [
      dot(runTone(latest)),
      link(latest.url, "git-link", `Sweep ran ${whenShort(latest.created_at, now)}, ${runWords(latest)}`),
    ]);
  }
}

function drawFooter(parts: Parts, state: State, data: GitOverview, onToggle: () => void): void {
  const off = data.accounts.filter((account) => !account.connected).length;
  const read = data.accounts.length - off;
  const toggle = el(
    "button",
    {
      type: "button",
      class: "btn btn-quiet git-toggle",
      "aria-expanded": String(state.open),
      "aria-controls": "git-list",
    },
    state.open ? "Hide the full list" : `Show all ${plural(data.repos.length, "repo")}`,
  );
  toggle.addEventListener("click", onToggle);
  replaceChildren(parts.footer, [
    toggle,
    el(
      "span",
      { class: `git-accounts${off ? " git-warn" : ""}` },
      off ? `${read} of ${plural(data.accounts.length, "account")} read` : `${plural(read, "account")} read`,
    ),
  ]);
}

// ── Show all ────────────────────────────────────────────────────────────────

/** "1 PR, 22 alerts, 2 branches": what a repo's group holds. */
function groupWords(prs: number, alerts: number, branches: number): string {
  return [
    prs ? plural(prs, "PR") : "",
    alerts ? plural(alerts, "alert") : "",
    branches ? plural(branches, "branch", "branches") : "",
  ]
    .filter(Boolean)
    .join(", ");
}

function chipCount(data: GitOverview, chip: Chip): number {
  const repos = live(data);
  if (chip === "active") return repos.filter((r) => r.prs.length || r.alerts.length || r.branches.length).length;
  if (chip === "prs") return repos.reduce((sum, r) => sum + r.prs.length, 0);
  if (chip === "alerts") return repos.reduce((sum, r) => sum + r.alerts.length, 0);
  if (chip === "branches") return repos.reduce((sum, r) => sum + r.branches.length, 0);
  if (chip === "viewonly") return data.repos.filter((r) => r.view_only).length;
  return data.repos.length;
}

function itemRow(main: HTMLElement | string, meta: string, lead?: Node | null): HTMLElement {
  return el(
    "li",
    { class: "git-item" },
    el("div", { class: "git-item-top" }, lead ?? null, main),
    meta ? el("span", { class: "git-meta" }, meta) : null,
  );
}

function repoItems(repo: GitRepo, kinds: Array<"prs" | "alerts" | "branches">, query: string, now: number, link: Linker): HTMLElement[] {
  const q = query.trim().toLowerCase();
  const whole = q === "" || repo.repo.toLowerCase().includes(q);
  const hit = (text: string) => whole || text.toLowerCase().includes(q);
  const rows: HTMLElement[] = [];
  if (kinds.includes("prs")) {
    for (const pr of repo.prs) {
      if (!hit(`#${pr.number} ${pr.title}`)) continue;
      const pill = pullPill(pr);
      const updated = ago(pr.updated_at, now);
      rows.push(
        itemRow(
          link(pr.url, "git-link", `#${pr.number} ${pr.title}`),
          [checksText(pr.checks), updated ? `updated ${updated}` : ""].filter(Boolean).join(" · "),
          el("span", { class: `git-state git-state-${pill.tone}` }, pill.label),
        ),
      );
    }
  }
  if (kinds.includes("alerts")) {
    // One line per package and bucket: 22 alerts of one package are one line, not 22.
    const groups = new Map<string, GitAlert[]>();
    for (const alert of repo.alerts) {
      if (!hit(alert.package)) continue;
      const key = `${alert.bucket}:${alert.package}`;
      groups.set(key, [...(groups.get(key) ?? []), alert]);
    }
    for (const alerts of groups.values()) {
      const first = alerts[0]!;
      const waiting = first.bucket === "waiting";
      const since = alerts
        .map((alert) => alert.waiting_since ?? alert.created_at ?? null)
        .sort((a, b) => stamp(a) - stamp(b))[0];
      const age = ageText(since, now);
      const meta = waiting
        ? `fixed on ${repo.default_branch}, waiting on GitHub${age ? ` ${age}` : ""}`
        : `vulnerable: ${first.installed.length ? first.installed.join(", ") : "version not found"} installed${first.patched ? `, fixed in ${first.patched}` : ""}`;
      rows.push(
        itemRow(
          link(
            alerts.length === 1 ? first.url : dependabotUrl(repo.repo),
            "git-link",
            `${plural(alerts.length, "alert")}, ${first.package}`,
          ),
          meta,
        ),
      );
    }
  }
  if (kinds.includes("branches")) {
    for (const branch of repo.branches) {
      if (!hit(branch.name)) continue;
      const age = ageText(branch.last_commit_at, now);
      const words = branch.ahead ? `${plural(branch.ahead, "commit")}, no PR` : "no PR";
      rows.push(
        itemRow(
          link(branch.url, "git-link", el("code", {}, branch.name)),
          [words, age].filter(Boolean).join(" · "),
          branch.kept ? el("span", { class: "git-tag" }, "kept") : null,
        ),
      );
    }
  }
  return rows;
}

function drawList(parts: Parts, state: State, data: GitOverview, now: number, link: Linker, redraw: () => void, remember: () => void): void {
  parts.list.hidden = !state.open;
  if (!state.open) return;
  replaceChildren(
    parts.chips,
    CHIPS.map(({ id, label }) => {
      const chip = el(
        "button",
        {
          type: "button",
          class: `git-chip${state.chip === id ? " is-on" : ""}`,
          "aria-pressed": String(state.chip === id),
          "data-chip": id,
        },
        label,
        el("span", { class: "git-chip-n" }, String(chipCount(data, id))),
      );
      chip.addEventListener("click", () => {
        state.chip = id;
        state.closedGroups.clear();
        remember();
        redraw();
      });
      return chip;
    }),
  );
  const searching = state.query.trim() !== "";
  const all: Array<"prs" | "alerts" | "branches"> = ["prs", "alerts", "branches"];
  const kinds = state.chip === "prs" || state.chip === "alerts" || state.chip === "branches" ? [state.chip] : all;
  const repos = data.repos.filter((repo) =>
    state.chip === "viewonly" ? repo.view_only : state.chip === "all" ? true : !repo.view_only,
  );
  const groups = repos.flatMap((repo) => {
    const rows = repoItems(repo, kinds, state.query, now, link);
    const nameHit = !searching || repo.repo.toLowerCase().includes(state.query.trim().toLowerCase());
    // Quiet repos and view-only repos with nothing open show under their own chips only.
    if (!rows.length && !((state.chip === "all" || state.chip === "viewonly") && nameHit)) return [];
    const key = repo.repo;
    const open = searching ? !state.closedGroups.has(key) : state.openGroups.has(key);
    const counts = {
      prs: kinds.includes("prs") ? repo.prs.length : 0,
      alerts: kinds.includes("alerts") ? repo.alerts.length : 0,
      branches: kinds.includes("branches") ? repo.branches.length : 0,
    };
    const words = groupWords(counts.prs, counts.alerts, counts.branches) || (repo.error ? "could not be read" : "quiet");
    const head = el(
      "button",
      { type: "button", class: "git-group-head", "aria-expanded": String(open), "data-repo": repo.repo },
      chevronIcon(),
      el("span", { class: "git-group-name", title: repo.repo }, shortName(repo.repo)),
      repo.scope === "mine" ? el("span", { class: "git-tag git-tag-mine", title: "From your own list" }, "Mine") : null,
      el("span", { class: "git-group-sum" }, words),
    );
    head.addEventListener("click", () => {
      // Searching opens every group with a match; a click then folds or opens just this one.
      const set = searching ? state.closedGroups : state.openGroups;
      if (set.has(key)) set.delete(key);
      else set.add(key);
      redraw();
    });
    const body = el(
      "ul",
      { class: "git-items", "aria-label": repo.repo },
      ...(repo.error ? [el("li", { class: "git-warn" }, repo.error)] : []),
      ...rows,
      !rows.length && !repo.error ? el("li", { class: "git-meta" }, "Nothing open.") : null,
    );
    body.hidden = !open;
    return [el("li", { class: `git-group${open ? " is-open" : ""}`, "data-group": repo.repo }, head, body)];
  });
  replaceChildren(parts.groups, [
    groups.length
      ? el("ul", { class: "git-groups" }, ...groups)
      : el("p", { class: "meta git-empty" }, "No repo matches."),
  ]);
}

function draw(parts: Parts, state: State, context: ExtensionContext, remember: () => void): void {
  const now = Date.now();
  const said = state.error ? describeError(state.error) : "";
  replaceChildren(parts.status, [said ? notice(`The Git repos could not be read (${said}).`, "error") : null]);
  parts.refreshed.textContent = state.at
    ? `Updated ${new Date(state.at).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })}`
    : "";
  const data = state.data;
  const link: Linker = (url, className, ...children) => ghLink(context, parts.root, url, className, ...children);
  const redraw = () => draw(parts, state, context, remember);
  drawScope(parts, state);
  if (!data || !data.configured) {
    parts.summary.textContent = "";
    parts.summary.className = "git-summary";
    for (const part of [parts.stats, parts.needs, parts.quiet, parts.sweep, parts.footer, parts.groups, parts.chips]) {
      replaceChildren(part, []);
    }
    parts.quiet.hidden = true;
    parts.sweep.hidden = true;
    parts.list.hidden = true;
    replaceChildren(parts.notes, [
      !data
        ? state.error
          ? null
          : el("p", { class: "meta" }, "Reading...")
        : el(
            "p",
            { class: "meta git-setup" },
            data.setup ??
              (state.scope === "mine"
                ? "You have no personal repos yet. Add a GitHub account in Cards, under Git."
                : "Git repos are not set up on this box."),
          ),
    ]);
  } else {
    const analysis = analyze(data, now);
    const err = data.totals.alerts_fix > 0;
    parts.summary.textContent = summaryWords(data);
    parts.summary.className = `git-summary${err ? " is-err" : (attentionCount("git") ?? 0) > 0 ? " is-warn" : ""}`;
    replaceChildren(parts.notes, [
      // The sweep config failing to read leaves only the extra repos listed.
      data.error ? el("p", { class: "git-warn" }, data.error) : null,
      data.list_cut ? el("p", { class: "meta" }, "Lists shortened to fit; the counts include everything.") : null,
    ]);
    drawStats(parts, data, analysis);
    drawNeeds(parts, state);
    drawQuiet(parts, analysis, link);
    drawSweep(parts, data, now, link);
    drawFooter(parts, state, data, () => {
      state.open = !state.open;
      remember();
      redraw();
    });
    drawList(parts, state, data, now, link, redraw, remember);
  }
  const tile = gitTile(state.data, state.error);
  setTile(state.data ? staleTile(tile, state.error) : tile, state.at || undefined);
}

/** The card's body; activateGit fills it. */
export function gitSection(): HTMLElement {
  const search = el("input", {
    type: "search",
    class: "git-search-input",
    placeholder: "Search repos, branches, PRs, packages",
    "aria-label": "Search repos, branches, pull requests and packages",
    autocomplete: "off",
    spellcheck: "false",
  });
  const list = el(
    "div",
    { class: "git-list", id: "git-list" },
    el("label", { class: "git-search" }, searchIcon(), search),
    el("div", { class: "git-chips", role: "group", "aria-label": "Filter" }),
    el("div", { class: "git-groups-wrap" }),
  );
  list.hidden = true;
  return el(
    "div",
    { class: "admin-block admin-git" },
    el("div", { class: "git-scope", role: "group", "aria-label": "Which repos to show" }),
    el("p", { class: "meta refreshed git-refreshed" }),
    el("div", { class: "page-status git-status" }),
    el("p", { class: "git-summary" }),
    el(
      "div",
      { class: "git-body" },
      el("div", { class: "git-notes" }),
      el("div", { class: "git-stats" }),
      el("div", { class: "git-needs-wrap" }),
      el("div", { class: "git-quiet" }),
      el("p", { class: "git-sweep" }),
      el("div", { class: "git-footer" }),
      list,
    ),
  );
}

let onFocus: (() => void) | null = null;
/** The unsubscribe from the Needs you store: the card's count follows the panel's reads. */
let stopAttention: (() => void) | null = null;
/** Bumped by every activate and deactivate, as in accounts.ts. */
let generation = 0;

function stopListening(): void {
  stopAttention?.();
  stopAttention = null;
  if (!onFocus) return;
  window.removeEventListener("focus", onFocus);
  document.removeEventListener("visibilitychange", onFocus);
  onFocus = null;
}

/** Read and draw the card *root* (from gitSection, or a card holding it), again on every focus. */
export function activateGit(context: ExtensionContext, root: HTMLElement): void {
  const find = <T extends HTMLElement>(selector: string): T => {
    const found = root.querySelector<T>(selector);
    if (!found) throw new Error(`Git repos card is missing ${selector}`);
    return found;
  };
  // In the card the header carries the time, so this one may have moved.
  const parts: Parts = {
    root: root.matches(".admin-git") ? root : find(".admin-git"),
    scope: find(".git-scope"),
    summary: find(".git-summary"),
    status: find(".git-status"),
    refreshed: root.querySelector<HTMLElement>(".git-refreshed") ?? el("p"),
    notes: find(".git-notes"),
    stats: find(".git-stats"),
    needs: find(".git-needs-wrap"),
    quiet: find(".git-quiet"),
    sweep: find(".git-sweep"),
    footer: find(".git-footer"),
    list: find(".git-list"),
    search: find(".git-search-input"),
    chips: find(".git-chips"),
    groups: find(".git-groups-wrap"),
  };
  const mine = ++generation;
  const current = () => generation === mine;
  stopListening();
  const state: State = {
    data: null,
    error: null,
    at: 0,
    open: false,
    chip: "active",
    scope: "all",
    switchScope: (scope) => {
      if (!current()) return;
      // A read of the other list still on its way must not land on this one.
      readSeq++;
      reading = null;
      state.scope = scope;
      state.data = null;
      state.error = null;
      state.at = 0;
      drawn = "";
      state.openGroups.clear();
      state.closedGroups.clear();
      saveScope(context, scope);
      draw(parts, state, context, remember);
      void refresh();
    },
    query: "",
    openGroups: new Set(),
    closedGroups: new Set(),
  };
  const remember = () => saveView(context, state);
  parts.search.value = "";
  parts.search.oninput = () => {
    state.query = parts.search.value;
    state.closedGroups.clear();
    draw(parts, state, context, remember);
  };
  let reading: Promise<void> | null = null;
  let readSeq = 0;
  let drawn = "";
  const refresh = (): Promise<void> => {
    if (reading) return reading;
    const seq = ++readSeq;
    const live = () => current() && seq === readSeq;
    reading = fetchGit(context, state.scope)
      .then(
        (answer) => {
          if (!live()) return;
          state.data = answer;
          state.at = Date.now();
          state.error = null;
        },
        (error: unknown) => {
          if (live()) state.error = error ?? new Error("Request failed");
        },
      )
      .then(() => {
        if (!live()) return;
        // The Needs you list is built from this answer: read it again now (debounced).
        sourceRead();
        // An unchanged answer is not redrawn, so a focused link stays put. The quarter hour is
        // in the key because the stuck, old and late rules move with the clock.
        const next = JSON.stringify([
          state.data,
          state.error ? describeError(state.error) : null,
          Math.floor(Date.now() / (15 * 60_000)),
        ]);
        if (next !== drawn) {
          drawn = next;
          draw(parts, state, context, remember);
        } else {
          // The same answer is not redrawn, but it is fresh: say so.
          parts.refreshed.textContent = `Updated ${new Date(state.at).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })}`;
          setTile(state.data ? staleTile(gitTile(state.data, state.error), state.error) : gitTile(state.data, state.error), state.at);
        }
      })
      .finally(() => {
        if (seq === readSeq) reading = null;
      });
    return reading;
  };
  draw(parts, state, context, remember);
  stopAttention = onAttention(() => {
    if (current()) draw(parts, state, context, remember);
  });
  // The remembered scope decides what the first read asks for, so the view loads first.
  void loadView(context).then((view) => {
    if (!current()) return;
    state.open = view.open;
    state.chip = view.chip;
    if (view.scope !== state.scope) {
      // A read of the default scope may already be on its way (a focus event, a refresh
      // button); it must not land under the remembered one, as in switchScope.
      readSeq++;
      reading = null;
      state.scope = view.scope;
      state.data = null;
      state.error = null;
      state.at = 0;
      drawn = "";
    }
    draw(parts, state, context, remember);
    void refresh();
  });
  registerRefresh("git", () => (current() ? refresh() : undefined));
  onFocus = () => {
    if (document.visibilityState === "visible") void refresh();
  };
  window.addEventListener("focus", onFocus);
  document.addEventListener("visibilitychange", onFocus);
}

export function deactivateGit(): void {
  generation++;
  stopListening();
}
