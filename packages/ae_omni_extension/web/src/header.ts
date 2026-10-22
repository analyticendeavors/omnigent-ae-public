// The Session Dashboard header (2026-10-17): the section chips went, because the
// page already shows every section, and the header reads out the state instead.
// Two views, picked with the toggle at its right and remembered per user in
// `storage.user` (key `header.view`), the way sections.ts remembers sections:
//
//   flow   (default, concept 3) the title with a usage bar per harness, the
//          slots as blocks, one per working session (over the limit hatched
//          red, older than eight hours outlined, free slots dashed) over a
//          row naming each slot, and a pipeline of pips: each open pull
//          request's checks, the release's three steps (GET /v1/ae/release),
//          a pip per item on Your move, and three for the lot, blue for each
//          task coming back within a week.
//   brief  (concept 2) a dot and the title, one sentence, worst thing first,
//          and a muted footer: usage, the release, the counts.
//
// Both follow the two concepts in session-dashboard-header-concepts.html as
// drawn: Reid picked 3 with a toggle to 2 on 2026-10-17, and a first build
// that drifted from them (text rows, a lot strip, age bars) was redone to
// match. The pips are decoration for sight (aria-hidden); each row's text
// says the same thing in words. A row or a link that names a section opens
// and scrolls to it; a session block opens the session.

import type { ExtensionContext } from "@omnigent/extension-sdk";
import type { CapacitySession, LotTask, PrCard } from "./api";
import { el, replaceChildren, setSectionOpen } from "./dom";
import {
  type LimitWindow,
  type Limits,
  type ShownLimit,
  shortLabel,
  shownLimits,
  tone,
} from "./limits";
import { addDays } from "./lotFilter";
import { isReady } from "./prs";

export type HeaderView = "flow" | "brief";
export const HEADER_VIEW_KEY = "header.view";
/** A working session this old is outlined in the slots and named in the brief. */
export const OLD_S = 8 * 3600;
/** Open pull requests the pipeline lists; the Open PRs section has the rest. */
const PIPE_PRS = 3;
/** Your move pips drawn before the rest become "+n". */
const MOVE_PIPS = 8;
/** The lot's pips; one turns blue for each task coming back within SOON_DAYS. */
export const LOT_PIPS = 3;
export const SOON_DAYS = 7;

export interface Release {
  running: string | null;
  latest: string | null;
  state: "current" | "waiting" | "unknown";
  mid_turn: number;
  error: string | null;
  /** Keys config/server.env.example sets that the server lacks; absent on older servers. */
  env_missing?: string[] | null;
}

export async function fetchRelease(context: ExtensionContext): Promise<Release> {
  const response = await context.server.request({
    method: "GET",
    path: "/v1/ae/release",
  });
  if (!response.ok) throw new Error(`GET /v1/ae/release answered ${response.status}`);
  const body = response.body as Partial<Release> | null;
  if (!body || typeof body.state !== "string") throw new Error("GET /v1/ae/release: no state");
  return body as Release;
}

export interface HeaderData {
  used: number;
  limit: number;
  /** Working sessions, oldest first. */
  working: CapacitySession[];
  /** Your move's count as drawn; undefined until it answers. */
  yourMove: number | undefined;
  /** Open pull requests; null until the scan answers or while it is off. */
  prs: PrCard[] | null;
  /** `soon`: tasks coming back within SOON_DAYS; `overdue`: already past their date. */
  lot: { count: number; next: LotTask | null; soon: number; overdue: number } | null;
  release: Release | null;
  needsNote: number;
  reference: number;
  /** The plan limits (GET /v1/ae/limits), or null until they answer. */
  limits: Limits | null;
  /** Section ids folded away as empty: a link to one would go nowhere, so it is text. */
  folded: ReadonlySet<string>;
  /** False until the remembered view and the saved copy are read: the body waits. */
  ready: boolean;
}

let view: HeaderView = "flow";
let persist: (() => void) | null = null;

export async function loadHeaderView(context: ExtensionContext): Promise<void> {
  persist = () => {
    context.storage.user.set(HEADER_VIEW_KEY, view).catch(() => {
      // storage.user unavailable: remembered in memory until the page closes.
    });
  };
  try {
    const saved = await context.storage.user.get<unknown>(HEADER_VIEW_KEY);
    if (saved === "flow" || saved === "brief") view = saved;
  } catch {
    // Same as a failed write: the default, remembered in memory.
  }
}

export function headerView(): HeaderView {
  return view;
}

/** For tests: back to the default. */
export function resetHeaderView(): void {
  view = "flow";
  persist = null;
}

/** The soonest lot task coming back on or after *today* (YYYY-MM-DD), else null. */
export function nextBack(tasks: LotTask[], today: string): LotTask | null {
  const dated = tasks.filter((task) => task.resurfaces && task.resurfaces >= today);
  dated.sort((a, b) =>
    a.resurfaces! < b.resurfaces! ? -1 : a.resurfaces! > b.resurfaces! ? 1 : 0,
  );
  return dated[0] ?? null;
}

/** Lot tasks coming back within SOON_DAYS of *today*, and those already overdue. */
export function lotCounts(tasks: LotTask[], today: string): { soon: number; overdue: number } {
  const last = addDays(today, SOON_DAYS - 1);
  const days = tasks
    .map((task) => task.resurfaces?.slice(0, 10))
    .filter((day) => day !== undefined);
  return {
    soon: days.filter((day) => day >= today && day <= last).length,
    overdue: days.filter((day) => day < today).length,
  };
}

function localDate(date: string): Date | null {
  const [y, m, d] = date.split("-").map(Number);
  return y && m && d ? new Date(y, m - 1, d) : null;
}

/** "Mon Oct 19" for a YYYY-MM-DD date, read as a local calendar day. */
export function dayLabel(date: string): string {
  const day = localDate(date);
  if (!day) return date;
  return `${day.toLocaleDateString(undefined, { weekday: "short" })} ${monthDay(date)}`;
}

/** "Oct 19" for a YYYY-MM-DD date. */
export function monthDay(date: string): string {
  const day = localDate(date);
  if (!day) return date;
  return `${day.toLocaleDateString(undefined, { month: "short" })} ${day.getDate()}`;
}

function windowName(shown: ShownLimit, window: LimitWindow): string {
  const label = shortLabel(window);
  return `${shown.name} ${label === "wk" ? "week" : label}`;
}

/**
 * Flow's usage bars: one per harness, the longest of the windows /usage leads
 * with (the week). A window with no length keeps its place, so with none known
 * the last shown one, /usage's longer one, wins.
 */
function usageMeters(limits: Limits | null): HTMLElement | null {
  const meters = shownLimits(limits).map((shown) => {
    const longest = shown.windows.reduce((best, w) =>
      (w.window_minutes ?? 0) >= (best.window_minutes ?? 0) ? w : best,
    );
    const pct = Math.round(longest.used_percent);
    const fill = el("i", { class: `hd-fill is-${tone(pct)}` });
    // CSSOM, not a style attribute: a strict style-src still allows it.
    fill.style.setProperty("--pct", `${Math.min(100, Math.max(0, pct))}%`);
    return el(
      "div",
      { class: "hd-meter", title: shown.title },
      el("div", { class: "hd-meter-lbl" }, windowName(shown, longest), el("b", {}, `${pct}%`)),
      el("div", { class: "hd-track", "aria-hidden": "true" }, fill),
    );
  });
  return meters.length
    ? el("div", { class: "hd-usage", role: "status", "aria-label": "Plan limits" }, ...meters)
    : null;
}

function jumpTo(id: string): void {
  const target = document.getElementById(id);
  if (!target) return;
  setSectionOpen(target, true);
  target.scrollIntoView({ block: "start", behavior: "smooth" });
}

let folded: ReadonlySet<string> = new Set();

function sectionLink(id: string, text: string, cls = ""): HTMLElement {
  if (folded.has(id)) return el("span", { class: cls || undefined }, text);
  const link = el("button", { type: "button", class: `hd-link ${cls}`.trim() }, text);
  link.addEventListener("click", () => jumpTo(id));
  return link;
}

function sessionLink(context: ExtensionContext, row: CapacitySession): HTMLElement {
  const link = el("button", { type: "button", class: "hd-link" }, row.title);
  link.addEventListener("click", () => void context.navigation.openSession(row.id));
  return link;
}

function toggle(onChange: () => void): HTMLElement {
  const group = el("div", {
    class: "hd-toggle",
    role: "group",
    "aria-label": "Header view",
  });
  for (const [id, text] of [
    ["flow", "Flow"],
    ["brief", "Brief"],
  ] as const) {
    const button = el(
      "button",
      { type: "button", "aria-pressed": String(view === id), "data-view": id },
      text,
    );
    button.addEventListener("click", () => {
      if (view === id) return;
      view = id;
      persist?.();
      onChange();
      // The redraw replaced this button: keep the focus on its successor.
      document.querySelector<HTMLElement>(`.hd-toggle [data-view="${id}"]`)?.focus();
    });
    group.appendChild(button);
  }
  return group;
}

// ── flow ────────────────────────────────────────────────────────────────

function slotBlocks(context: ExtensionContext, data: HeaderData): HTMLElement[] {
  const blocks: HTMLElement[] = [];
  const caps: HTMLElement[] = [];
  const total = Math.max(data.working.length, data.limit);
  for (let index = 0; index < total; index++) {
    const row = data.working[index];
    const over = index >= data.limit;
    caps.push(el("span", {}, over ? "over" : `slot ${index + 1}`));
    if (!row) {
      blocks.push(el("div", { class: "hd-slot is-free" }, el("span", {}, "free")));
      continue;
    }
    const old = row.elapsed_s >= OLD_S;
    const block = el(
      "button",
      {
        type: "button",
        class: `hd-slot${over ? " is-over" : ""}${old ? " is-old" : ""}`,
        title: `${row.title}, ${row.elapsed}${over ? ", over the limit" : ""}`,
      },
      el("b", {}, row.title),
      el("span", {}, over ? `${row.elapsed}, over` : row.elapsed),
    );
    block.addEventListener("click", () => void context.navigation.openSession(row.id));
    blocks.push(block);
  }
  return [
    el("div", { class: "hd-slots" }, ...blocks),
    el("div", { class: "hd-cap", "aria-hidden": "true" }, ...caps),
  ];
}

/** A row of pips, each `i.hd-pip` with its tone class (empty for grey). */
function pipRow(classes: string[], extra: HTMLElement | null = null): HTMLElement {
  return el(
    "span",
    { class: "hd-pips", "aria-hidden": "true" },
    ...classes.map((cls) => el("i", { class: `hd-pip ${cls}`.trim() })),
    extra,
  );
}

function pips(prCard: PrCard): HTMLElement {
  const { passing, failing, pending, total } = prCard.ci;
  const marks: HTMLElement[] = [];
  const add = (count: number, cls: string) => {
    for (let i = 0; i < count; i++) marks.push(el("i", { class: `hd-pip ${cls}` }));
  };
  add(passing, "is-pass");
  add(failing, "is-fail");
  add(pending, "is-wait");
  if (total === 0) add(1, "");
  return el("span", { class: "hd-pips is-wrap", "aria-hidden": "true" }, ...marks);
}

function pipeRow(
  name: HTMLElement | string,
  middle: HTMLElement | null,
  right: string,
): HTMLElement {
  return el(
    "div",
    { class: "hd-pipe-row" },
    typeof name === "string" ? el("span", { class: "hd-pipe-name" }, name) : name,
    middle ?? el("span", {}),
    el("span", { class: "hd-pipe-right" }, right),
  );
}

/** The pipeline's short release text. */
function releaseShort(release: Release): string {
  if (release.state === "waiting") {
    // Only that a newer tag exists: a pin, rollback or hold on the box is out of view.
    return release.mid_turn > 0 ? `held, ${release.mid_turn} mid-reply` : "not installed yet";
  }
  if (release.state === "current")
    return release.running ? `${release.running} current` : "current";
  return release.error ? `unknown: ${release.error}` : "status unknown";
}

/** The brief footer's release sentence. */
function releaseLong(release: Release): string {
  if (release.state === "waiting") {
    const tag = release.latest ? ` ${release.latest}` : "";
    const held =
      release.mid_turn > 0
        ? ` held: ${release.mid_turn} ${release.mid_turn === 1 ? "session" : "sessions"} mid-reply`
        : " next, not installed yet";
    return `Release${tag}${held}`;
  }
  if (release.state === "current")
    return release.running ? `Release ${release.running} current` : "Release current";
  return `Release status unknown${release.error ? `: ${release.error}` : ""}`;
}

/**
 * Three steps: a newer tag exists, no session is mid-reply, installed. The
 * middle pip says only what the server knows: a pin, a rollback or a hold on
 * the box is out of view, so it never claims the install is clear. An unknown
 * status draws hollow pips, not the grey of a step not reached.
 */
function releaseSteps(release: Release): HTMLElement {
  if (release.state === "current") return pipRow(["is-pass", "is-pass", "is-pass"]);
  if (release.state === "waiting")
    return pipRow(["is-pass", release.mid_turn > 0 ? "is-wait" : "is-pass", ""]);
  return pipRow(["is-unknown", "is-unknown", "is-unknown"]);
}

/** A pip per item waiting on Reid, "+n" past MOVE_PIPS; one grey pip when clear. */
function movePips(count: number): HTMLElement {
  if (count === 0) return pipRow([""]);
  return pipRow(
    Array.from({ length: Math.min(count, MOVE_PIPS) }, () => "is-wait"),
    count > MOVE_PIPS ? el("span", { class: "hd-more" }, `+${count - MOVE_PIPS}`) : null,
  );
}

/**
 * LOT_PIPS pips: one red for each task past its date, then one blue for each
 * coming back within SOON_DAYS, "+n" for those beyond the three.
 */
function lotPips(soon: number, overdue: number): HTMLElement {
  const marks = [
    ...Array.from({ length: overdue }, () => "is-fail"),
    ...Array.from({ length: soon }, () => "is-soon"),
  ];
  const shown = marks.slice(0, LOT_PIPS);
  while (shown.length < LOT_PIPS) shown.push("");
  const more = marks.length - LOT_PIPS;
  return pipRow(shown, more > 0 ? el("span", { class: "hd-more" }, `+${more}`) : null);
}

function pipeline(data: HeaderData): HTMLElement {
  const rows: HTMLElement[] = [];
  for (const prCard of (data.prs ?? []).slice(0, PIPE_PRS)) {
    const { passing, failing, total } = prCard.ci;
    const name = sectionLink("prs", `PR #${prCard.number}`, "hd-pipe-name");
    name.title = prCard.title;
    const row = pipeRow(name, pips(prCard), total ? `${passing}/${total}` : "no checks");
    // Muted as concept 3, but a failing check is said in red, not only by a pip.
    if (failing > 0) row.classList.add("is-fail");
    rows.push(row);
  }
  const more = (data.prs?.length ?? 0) - PIPE_PRS;
  if (more > 0) rows.push(pipeRow(sectionLink("prs", `+${more} PRs`, "hd-pipe-name"), null, ""));
  if (data.release) {
    const row = pipeRow("Release", releaseSteps(data.release), releaseShort(data.release));
    if (data.release.latest) row.title = `Newest tag ${data.release.latest}`;
    rows.push(row);
  }
  if (data.yourMove !== undefined) {
    rows.push(
      pipeRow(
        sectionLink("your-move", "Your move", "hd-pipe-name"),
        movePips(data.yourMove),
        data.yourMove ? `${data.yourMove} waiting` : "clear",
      ),
    );
  }
  if (data.lot) {
    const next = data.lot.next;
    rows.push(
      pipeRow(
        sectionLink("lot", "Lot", "hd-pipe-name"),
        lotPips(data.lot.soon, data.lot.overdue),
        [
          `${data.lot.count}`,
          data.lot.overdue ? `${data.lot.overdue} overdue` : null,
          next?.resurfaces ? `next ${monthDay(next.resurfaces.slice(0, 10))}` : null,
        ]
          .filter(Boolean)
          .join(", "),
      ),
    );
  }
  return el("div", { class: "hd-pipe" }, el("h2", {}, "Pipeline"), ...rows);
}

function flow(context: ExtensionContext, data: HeaderData): HTMLElement {
  const over = data.used > data.limit;
  return el(
    "div",
    { class: "hd-flow" },
    el(
      "div",
      { class: "hd-slots-wrap" },
      el(
        "h2",
        {},
        "Slots ",
        el(
          "b",
          {
            class: over ? "is-over" : data.used === data.limit ? "is-full" : "",
          },
          `${data.used} of ${data.limit}`,
        ),
      ),
      ...slotBlocks(context, data),
    ),
    pipeline(data),
  );
}

// ── brief ───────────────────────────────────────────────────────────────

function span(text: string, tone: string): HTMLElement {
  return el("span", { class: `hd-${tone}` }, text);
}

/** The brief's sentence parts, worst first; strings and links. */
export function briefParts(context: ExtensionContext, data: HeaderData): (HTMLElement | string)[] {
  const parts: (HTMLElement | string)[] = [];
  if (data.used > data.limit) {
    parts.push(span(`Over capacity: ${data.used} working, limit ${data.limit}.`, "bad"), " ");
  } else if (data.used === data.limit) {
    parts.push(span(`All ${data.limit} slots in use.`, "warn"), " ");
  } else {
    parts.push(`${data.used} of ${data.limit} slots in use. `);
  }
  const oldest = data.working[0];
  if (oldest && oldest.elapsed_s >= OLD_S) {
    parts.push(
      "Oldest is ",
      sessionLink(context, oldest),
      ` at ${oldest.elapsed}; park it or finish it. `,
    );
  }
  const prs = data.prs ?? [];
  if (prs.length > 0) {
    const failing = prs.filter((p) => p.ci.failing > 0).length;
    const pending = prs.filter((p) => p.ci.failing === 0 && p.ci.pending > 0).length;
    const ready = prs.filter(isReady).length;
    const one = prs.length === 1;
    const is = one ? " is " : " are ";
    parts.push(sectionLink("prs", one ? "1 PR" : `${prs.length} PRs`));
    if (failing > 0)
      parts.push(one ? " has " : `: ${failing} with `, span("failing checks.", "bad"), " ");
    else if (pending > 0 && pending < prs.length)
      parts.push(": ", span(`${pending} waiting on CI.`, "warn"), " ");
    else if (pending > 0) parts.push(is, span("waiting on CI.", "warn"), " ");
    else if (ready === prs.length) parts.push(is, span("ready to merge.", "good"), " ");
    else if (ready > 0)
      parts.push(": ", span(`${ready} ready to merge, the rest need a look.`, "warn"), " ");
    else parts.push(" ", span(one ? "needs a look." : "need a look.", "warn"), " ");
  }
  if (data.yourMove !== undefined) {
    if (data.yourMove === 0) parts.push(span("Nothing is on your move.", "good"), " ");
    else {
      parts.push(
        sectionLink("your-move", data.yourMove === 1 ? "1 thing" : `${data.yourMove} things`),
        " ",
        span(data.yourMove === 1 ? "is on your move." : "are on your move.", "warn"),
        " ",
      );
    }
  }
  const next = data.lot?.next;
  if (next?.resurfaces) {
    parts.push(
      "Next from the lot: ",
      sectionLink("lot", next.title ?? "untitled"),
      `, back ${dayLabel(next.resurfaces.slice(0, 10))}.`,
    );
  }
  while (parts.at(-1) === " ") parts.pop();
  return parts;
}

/** The brief's footer: usage, then the release, then the counts, as concept 2. */
function briefFoot(data: HeaderData): HTMLElement | null {
  const groups: HTMLElement[][] = [];
  const usage = shownLimits(data.limits).flatMap((shown) =>
    shown.windows.map((window) => {
      const pct = Math.round(window.used_percent);
      return el(
        "span",
        { class: `hd-use is-${tone(pct)}`, title: shown.title },
        `${windowName(shown, window)} ${pct}%`,
      );
    }),
  );
  if (usage.length) groups.push(usage);
  if (data.release) groups.push([el("span", {}, releaseLong(data.release))]);
  const counts = [
    data.lot ? `${data.lot.count} parked` : null,
    data.needsNote ? `${data.needsNote} needs a note` : null,
    data.reference ? `${data.reference} reference` : null,
  ].filter((part): part is string => part !== null);
  if (counts.length) groups.push([el("span", {}, counts.join(", "))]);
  if (!groups.length) return null;
  const sep = (text: string) => el("span", { class: "hd-sep", "aria-hidden": "true" }, text);
  const parts: HTMLElement[] = [];
  groups.forEach((group, g) => {
    if (g > 0) parts.push(sep("|"));
    group.forEach((item, i) => {
      if (i > 0) parts.push(sep("/"));
      parts.push(item);
    });
  });
  return el("div", { class: "hd-foot" }, ...parts);
}

function brief(context: ExtensionContext, data: HeaderData): HTMLElement {
  return el(
    "div",
    { class: "hd-brief" },
    el("p", {}, ...briefParts(context, data)),
    briefFoot(data),
  );
}

// ── the header ──────────────────────────────────────────────────────────

export function renderHeader(
  host: HTMLElement,
  context: ExtensionContext,
  data: HeaderData,
  onChange: () => void,
): void {
  folded = data.folded;
  const title = el("h1", { class: "hd-title" }, "Session Dashboard");
  // Until the remembered view is read, the title alone: no flash of the other view.
  if (!data.ready) {
    replaceChildren(host, [el("div", { class: "hd-top" }, title)]);
    return;
  }
  const over = data.used > data.limit;
  host.classList.toggle("is-brief", view === "brief");
  if (view === "brief") {
    const tone = over ? " is-over" : data.used === data.limit ? " is-full" : "";
    replaceChildren(host, [
      el(
        "div",
        { class: "hd-top" },
        el("span", { class: `hd-dot${tone}`, "aria-hidden": "true" }),
        title,
        toggle(onChange),
      ),
      brief(context, data),
    ]);
    return;
  }
  replaceChildren(host, [
    el("div", { class: "hd-top" }, title, usageMeters(data.limits), toggle(onChange)),
    flow(context, data),
  ]);
}
