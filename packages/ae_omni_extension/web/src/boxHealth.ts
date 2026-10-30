// The Admin Center's Box health monitor (2026-10-28): heat, CPU, memory,
// storage and bloat on the box, as the first block in the Box health card
// (above the host details and the secrets) and its own "Box health" tile.
//
// Why: on 2026-10-28 the box's /tmp (a 62 GB RAM tmpfs capped at 1,048,576
// inodes) ran out of inodes and every Claude Code Bash call failed for about 15
// minutes. The owner reaches the box only through OmniGent, so the page has to show
// it. A timer on the box measures every five minutes and posts to the server
// (`box_health_relay.py`); the server works out the colours and the Needs-you
// lines (`box_health.py`); this module draws them. It does no judging of its
// own, except the tile.
//
// The summary is built to fit a 200 px card (two columns on a 520 to 600 px
// page): a header ("This box", when it was read, and Details), up to three
// Needs-you lines, then five one-line rows (a dot, a 64 px label and a value cut
// with an ellipsis; the whole text is the row's title). Details opens a dialog
// (dialog.ts) with the full tables, a day of heat history and every setting
// with a one-line hint (hint.ts).
//
// Privacy: the reading holds no folder, project, session, branch, worktree or
// file name, for any user; rows are per user label with counts, sizes and ages.
//
// Polling: on activate, every 60 s, and on focus, as host.ts does; a scheduled
// read is skipped while the tab is hidden. After every draw it hands the page
// its tile (tiles.ts): green OK, amber when something needs a look or the
// reading is stale, red when something is failing, grey before the first reading.

import type { ExtensionContext } from "@omnigent/extension-sdk";
import {
  AeRequestError,
  type BoxActionOffer,
  type BoxArea,
  type BoxClaudeRoot,
  type BoxHealthView,
  type BoxNeed,
  type BoxTone,
  type BoxUser,
  cancelBoxCleanup,
  describeError,
  fetchBoxHealth,
  requestBoxCleanup,
} from "./api";
import { openDialog } from "./dialog";
import { el, notice, replaceChildren } from "./dom";
import { HINT_MAX, hint } from "./hint";
import { alertTriangleIcon, circleCheckIcon, circleDashedIcon, queuedClockIcon } from "./icons";
import { registerRefresh, setTile, staleTile, type Health, type Tile } from "./tiles";

export const POLL_MS = 60_000;
/** The poll while a cleanup is queued, untaken or running (2026-10-28). */
export const FAST_POLL_MS = 10_000;
/** How many Needs-you lines the summary shows before "+n more in Details". */
export const SUMMARY_NEEDS = 3;

/** The hint for each control of the monitor's own (the settings' come from the server). */
export const BOX_HEALTH_HINTS = {
  details:
    "Opens the full tables: sensors, mounts, Claude temp, /tmp, Docker, worktrees and a day of history.",
  close: "Closes the details and returns to the card. Escape does the same.",
  claude:
    "Claude Code's session temp folders. Stale: nothing in one changed for the stale age and no live session holds it.",
  tmp: "Leftovers at the top of /tmp, counted without opening any folder. Old means the entry itself is older than the stale age.",
  worktrees:
    "Counted and sized, never listed, never removed. A container's ages are approximate: they use each folder's own change time.",
  cleanup:
    "Cleanups the box offers now. Each one asks you to confirm first, and the box skips anything a live session still holds.",
  cleanAction:
    "Opens a confirm step with the exact numbers. Nothing is removed until you confirm there.",
  cleanUp: "Opens Details on the confirm step for this cleanup. Nothing is removed until you confirm.",
  confirmRun: "Asks the box to run this within 5 minutes. It skips anything a live session still holds.",
  confirmBack: "Goes back to the details without removing anything.",
  cancelRequest: "Withdraws the request before the box starts it. Too late once the box has taken it.",
  tryAgain: "Opens the confirm step again so you can send the request again.",
} as const;

const OPEN_STATES = ["queued", "untaken", "running"];

// ── words ───────────────────────────────────────────────────────────────────

const KB = 1024;

/** Bytes as short words: "482 MB", "13 GB". */
export function sizeText(bytes: number | null | undefined): string {
  if (bytes == null) return "n/a";
  for (const [unit, size] of [
    ["TB", KB ** 4],
    ["GB", KB ** 3],
    ["MB", KB ** 2],
    ["KB", KB],
  ] as const) {
    if (bytes >= size) {
      const amount = bytes / size;
      return `${amount >= 10 ? Math.round(amount) : amount.toFixed(1)} ${unit}`;
    }
  }
  return `${Math.round(bytes)} B`;
}

/** A count as short words: "1,204", "26k", "1.05M". */
export function countText(count: number | null | undefined): string {
  if (count == null) return "n/a";
  if (count >= 1_000_000) return `${(count / 1_000_000).toFixed(2)}M`;
  if (count >= 10_000) return `${Math.round(count / 1000)}k`;
  return count.toLocaleString("en-US");
}

/** "just now", "3 m ago", "2 h ago", "4 d ago". */
export function agoText(seconds: number | null | undefined): string {
  if (seconds == null) return "never";
  if (seconds < 90) return seconds < 30 ? "just now" : `${Math.round(seconds)} s ago`;
  if (seconds < 5400) return `${Math.round(seconds / 60)} m ago`;
  if (seconds < 172_800) return `${Math.round(seconds / 3600)} h ago`;
  return `${Math.round(seconds / 86_400)} d ago`;
}

function upText(seconds: number | null | undefined): string {
  if (seconds == null) return "";
  const days = Math.floor(seconds / 86_400);
  return days >= 1 ? `up ${days} d` : `up ${Math.floor(seconds / 3600)} h`;
}

function clock(iso: string | null): string {
  return iso
    ? new Date(iso).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })
    : "never";
}

const HEALTH: Record<BoxTone, Health> = {
  green: "ok",
  amber: "attention",
  red: "problem",
  grey: "idle",
};

// ── what the summary shows ──────────────────────────────────────────────────

export interface Summary {
  /** "read 3 m ago", or "no reading yet". */
  age: string;
  needs: BoxNeed[];
  more: number;
  areas: BoxArea[];
  stale: boolean;
  missing: boolean;
}

/** The summary's content from a view: the worst few needs first, then five areas. */
export function summaryOf(view: BoxHealthView): Summary {
  const shown = view.needs.slice(0, SUMMARY_NEEDS);
  return {
    age: view.state === "missing" ? "no reading yet" : `read ${agoText(view.age_s)}`,
    needs: shown,
    more: Math.max(0, view.needs.length - shown.length),
    areas: view.areas,
    stale: view.state === "stale",
    missing: view.state === "missing",
  };
}

/** The Box health tile: "OK" or the worst need, and how fresh the reading is. */
export function tileOf(view: BoxHealthView | null, error: unknown): Tile {
  if (!view) {
    return {
      id: "boxhealth",
      value: error ? "Unknown" : "Reading...",
      note: error ? "could not be read" : "",
      health: error ? "attention" : "idle",
      dot: true,
    };
  }
  const worst = view.needs[0];
  const health = HEALTH[view.tone];
  const value = view.state === "missing" ? "No reading" : worst ? worst.text : "OK";
  const note =
    view.state === "missing"
      ? "the box timer has not run"
      : view.needs.length > 1
        ? `${view.needs.length} need you`
        : `Read ${agoText(view.age_s)}`;
  return {
    id: "boxhealth",
    value,
    summary: view.state === "missing" ? "Box not read" : worst ? worst.text : "Box OK",
    note,
    health,
    dot: true,
    title: worst?.detail || undefined,
  };
}

/** The status line's pill: the worst state in the tile's own words, in the server's tone. */
export function worstOf(view: BoxHealthView): { text: string; tone: BoxTone } {
  const text =
    view.state === "missing" ? "No reading" : view.needs.length ? `${view.needs.length} need you` : "OK";
  return { text, tone: view.tone };
}

// ── the summary section ─────────────────────────────────────────────────────

function dot(tone: BoxTone): HTMLElement {
  return el("span", { class: `bh-dot bh-${tone}`, "aria-hidden": "true" });
}

function tileRow(area: BoxArea): HTMLElement {
  return el(
    "li",
    {
      class: `bh-tile bh-t-${area.tone}`,
      title: area.title,
      "aria-label": `${area.label}: ${area.value}`,
    },
    el("span", { class: "bh-tile-l" }, dot(area.tone), el("span", { class: "bh-lbl" }, area.label)),
    el("span", { class: "bh-val" }, area.value),
  );
}

function needRow(need: BoxNeed, view: BoxHealthView): HTMLElement {
  const offer = offerForNeed(view, need);
  const row = el(
    "li",
    { class: `bh-need bh-t-${need.tone}`, title: need.detail || undefined },
    dot(need.tone),
    el("span", { class: "bh-need-text" }, need.text),
  );
  if (offer) {
    const button = tipped(
      el("button", { type: "button", class: "btn btn-quiet bh-act bh-need-act", "aria-haspopup": "dialog" }, "Clean up"),
      BOX_HEALTH_HINTS.cleanUp,
    );
    button.addEventListener("click", () => openDetails(view, button, offer));
    row.appendChild(button);
  }
  return row;
}

// ── cleanup (2026-10-28, Box health PR 2) ───────────────────────────────────
// The UI shows only what the server sends: counts, sizes, owner labels and the
// server's own sentences. It never builds or shows a folder, project or file name.

/** Put a hint on a button the way the card does: a tooltip and a `data-hint`, no added height. */
function tipped<T extends HTMLElement>(button: T, text: string, title?: string | null): T {
  button.setAttribute("data-hint", text);
  button.setAttribute("title", title || text);
  return button;
}

const plainHint = (text: string | null | undefined, fallback: string): string =>
  text && text.length <= HINT_MAX && !text.includes("—") && !text.includes("--") ? text : fallback;

const offersOf = (view: BoxHealthView): BoxActionOffer[] => view.actions ?? [];

/** Whether a line is about a box user's own folders or a mapped container's: its key ends in the kind. */
function kindOf(need: BoxNeed): "box" | "container" {
  return need.key.endsWith(":container") ? "container" : "box";
}

/** The offer a Needs-you line can be fixed with: same action and owner (any owner for Docker). */
export function offerForNeed(view: BoxHealthView, need: BoxNeed): BoxActionOffer | null {
  if (!need.action) return null;
  return (
    offersOf(view).find(
      (o) =>
        o.id === need.action &&
        (o.id === "docker_prune" || (o.owner === need.owner && (o.where ?? "box") === kindOf(need))),
    ) ?? null
  );
}

function offerForCleanup(view: BoxHealthView): BoxActionOffer | null {
  const c = view.cleanup;
  if (!c) return null;
  return offersOf(view).find((o) => o.id === c.action && o.owner === c.owner) ?? null;
}

const ACTION_NAMES: Record<string, string> = {
  claude_scratch: "Claude temp folders",
  tmp_litter: "/tmp leftovers",
  review_checkouts: "Review checkouts",
  docker_prune: "Docker",
};

const ownerText = (owner: string): string => (owner === "all" ? "Whole box" : owner);

/** Skip reasons that read badly as their fixed word. */
const REASON_WORDS: Record<string, string> = {
  over_confirmed: "Aged after you confirmed, kept for the next run",
};

/** "no_session_match" as "no session match". */
const reasonText = (reason: string): string =>
  REASON_WORDS[reason] ?? reason.replace(/[_-]+/g, " ");

const WHY_NOT_FALLBACK = "The box cannot run this one now.";

/**
 * The visible reason a locked button is locked (a disabled button gets no hover, and a
 * phone has none), or null when the offer can run. The button points at it.
 */
function whyNotNode(offer: BoxActionOffer, button: HTMLElement, scope: string): HTMLElement | null {
  if (offer.can_run) return null;
  const id = `bh-why-${scope}-${offer.id}-${offer.owner}`.replace(/[^A-Za-z0-9_-]+/g, "-");
  button.setAttribute("aria-describedby", id);
  return el("span", { class: "bh-line bh-dim bh-why", id }, offer.why_not || WHY_NOT_FALLBACK);
}

/**
 * The latest cleanup request: the server's sentence as is, then the buttons its state
 * allows. *full* (the dialog) also lists what was skipped.
 */
function cleanupStateNode(view: BoxHealthView, full: boolean): HTMLElement | null {
  const c = view.cleanup;
  if (!c) return null;
  const parts: Array<Node | null> = [queuedClockIcon(), el("span", { class: "bh-cleanup-msg" }, c.message)];
  if (c.state === "queued" && c.can_cancel) {
    const cancel = tipped(
      el("button", { type: "button", class: "btn btn-quiet bh-act" }, "Cancel request"),
      BOX_HEALTH_HINTS.cancelRequest,
    );
    cancel.addEventListener("click", () => void doCancel());
    parts.push(cancel);
  }
  if (["failed", "expired", "lost"].includes(c.state) && c.can_retry !== false) {
    const offer = offerForCleanup(view);
    if (offer) {
      const again = tipped(
        el("button", { type: "button", class: "btn btn-quiet bh-act", "aria-haspopup": "dialog" }, "Try again"),
        BOX_HEALTH_HINTS.tryAgain,
      );
      again.addEventListener("click", () => {
        if (full && dlg) {
          problem = null;
          dlg.step = offer;
          drawDialog();
        } else {
          openDetails(view, again, offer);
        }
      });
      parts.push(again);
    }
  }
  const skipped = Object.entries(c.result?.skipped ?? {}).filter(([, n]) => n > 0);
  if (full && c.state === "done" && skipped.length) {
    parts.push(
      el(
        "ul",
        { class: "bh-skipped" },
        ...skipped.map(([reason, n]) => el("li", {}, `${reasonText(reason)}: ${countText(n)}`)),
      ),
    );
  }
  return el("div", { class: `bh-cleanup bh-c-${c.state}`, role: "status" }, ...parts);
}

const problemNode = (): HTMLElement | null =>
  problem ? notice(problem, "error") : null;

function cleanupSection(view: BoxHealthView): HTMLElement {
  const enabled = view.details.cleanup?.enabled !== false;
  const offers = enabled ? offersOf(view) : [];
  const aged = (view.details.cleanup?.auto_aged ?? []).filter((a) => a.count > 0);
  const nodes: Array<Node | null> = [problemNode(), cleanupStateNode(view, true)];
  if (!enabled) {
    nodes.push(el("p", { class: "bh-line bh-dim" }, "Cleanup is switched off on the box"));
  } else if (!offers.length) {
    nodes.push(line("Nothing to clean up right now."));
  } else {
    nodes.push(
      el(
        "ul",
        { class: "bh-offers" },
        ...offers.map((offer) => {
          const run = tipped(
            el(
              "button",
              {
                type: "button",
                class: "btn bh-act bh-offer-btn",
                "aria-haspopup": "dialog",
                disabled: !offer.can_run,
              },
              offer.label,
            ),
            plainHint(offer.hint, BOX_HEALTH_HINTS.cleanAction),
            offer.can_run ? null : offer.why_not || WHY_NOT_FALLBACK,
          );
          run.addEventListener("click", () => {
            if (!dlg) return;
            problem = null;
            dlg.step = offer;
            drawDialog();
          });
          const why = whyNotNode(offer, run, "list");
          return el(
            "li",
            { class: "bh-offer" },
            el(
              "span",
              { class: "bh-offer-what" },
              el("span", { class: "bh-offer-name" }, ACTION_NAMES[offer.id] ?? offer.id),
              el(
                "span",
                { class: "bh-offer-meta" },
                [
                  ownerText(offer.owner),
                  offer.files ? `${countText(offer.files)} files` : null,
                  sizeText(offer.bytes),
                ]
                  .filter(Boolean)
                  .join(" · "),
              ),
              why,
            ),
            run,
          );
        }),
      ),
    );
  }
  if (aged.length) {
    nodes.push(
      el(
        "p",
        { class: "bh-line bh-dim" },
        "Aged automatically in the last hour: " +
          aged
            .map((a) => `${countText(a.count)} folder${a.count === 1 ? "" : "s"} in ${a.owner}'s container`)
            .join("; "),
      ),
    );
  }
  return section("Cleanup", BOX_HEALTH_HINTS.cleanup, ...nodes);
}

/** The confirm step: the server's text as is, the offer's button, Cancel. */
function confirmNode(offer: BoxActionOffer): HTMLElement {
  const run = tipped(
    el(
      "button",
      { type: "button", class: "btn btn-primary bh-act", disabled: !offer.can_run || !!dlg?.busy },
      offer.label,
    ),
    BOX_HEALTH_HINTS.confirmRun,
    offer.can_run ? null : offer.why_not || WHY_NOT_FALLBACK,
  );
  const back = tipped(
    el("button", { type: "button", class: "btn btn-quiet bh-act", disabled: !!dlg?.busy }, "Cancel"),
    BOX_HEALTH_HINTS.confirmBack,
  );
  run.addEventListener("click", () => void doRun(offer));
  back.addEventListener("click", () => {
    if (!dlg) return;
    problem = null;
    dlg.step = null;
    drawDialog();
  });
  queueMicrotask(() => (back.isConnected ? back.focus() : undefined));
  return el(
    "div",
    { class: "bh-confirm" },
    el("h4", { class: "bh-sec-h" }, `${ACTION_NAMES[offer.id] ?? offer.id} · ${ownerText(offer.owner)}`),
    problemNode(),
    el("p", { class: "bh-confirm-text" }, offer.confirm),
    whyNotNode(offer, run, "confirm"),
    el("div", { class: "bh-confirm-actions" }, run, back),
  );
}

const WORST_HINT = "The worst state on this box right now";

/** Draw the pill's icon and words, and its tone class. */
function setWorst(pill: HTMLElement, worst: { text: string; tone: BoxTone }): void {
  pill.className = `bh-worst bh-worst-${worst.tone}`;
  const icon =
    worst.tone === "green" ? circleCheckIcon() : worst.tone === "grey" ? circleDashedIcon() : alertTriangleIcon();
  replaceChildren(pill, [icon, worst.text]);
}

/** The summary block's content. *onDetails* opens the dialog from the Details button. */
export function summaryNode(
  view: BoxHealthView,
  onDetails: (opener: HTMLElement) => void,
): HTMLElement {
  const summary = summaryOf(view);
  const details = el(
    "button",
    {
      type: "button",
      class: "btn btn-quiet bh-details",
      "aria-haspopup": "dialog",
    },
    "Details",
  );
  details.addEventListener("click", () => onDetails(details));
  const worst = worstOf(view);
  const pill = el("span", { class: `bh-worst bh-worst-${worst.tone}`, title: WORST_HINT });
  setWorst(pill, worst);
  const head = el(
    "div",
    { class: "bh-status" },
    pill,
    el(
      "span",
      { class: "bh-when" },
      el("span", { class: "bh-title" }, "This box"),
      el("span", { class: "bh-read" }, el("span", { class: "bh-age" }, summary.age), hint(BOX_HEALTH_HINTS.details, "Details")),
    ),
    details,
  );
  const root = el(
    "div",
    { class: `bh${summary.stale ? " is-stale" : ""}` },
    head,
    el("div", { class: "bh-rest" }, ...restNodes(view)),
  );
  return root;
}

/** The part of the summary under its header: the missing-reading line, the needs, the areas. */
function restNodes(view: BoxHealthView): Node[] {
  const summary = summaryOf(view);
  const nodes: Node[] = [];
  if (summary.missing) {
    nodes.push(el("p", { class: "bh-empty" }, "No reading from the box yet."));
    if (view.setup) nodes.push(el("code", { class: "bh-setup" }, view.setup));
  }
  const todo: Node[] = [];
  if (summary.needs.length) {
    todo.push(el("ul", { class: "bh-needs" }, ...summary.needs.map((n) => needRow(n, view))));
    if (summary.more) {
      todo.push(el("p", { class: "bh-more" }, `+${summary.more} more in Details`));
    }
  }
  const cleanup = cleanupStateNode(view, false);
  if (cleanup) {
    todo.push(cleanup);
    const failure = problemNode();
    if (failure) todo.push(failure);
  }
  nodes.push(el("ul", { class: "bh-tiles" }, ...summary.areas.map(tileRow)));
  if (todo.length) nodes.push(el("div", { class: "bh-todo" }, el("p", { class: "bh-todo-h" }, "Needs you"), ...todo));
  return nodes;
}

/**
 * Bring a drawn summary up to date in place. The header (the Details button that an
 * open dialog returns focus to, and its hint, which may be open) is kept; only the age
 * and the lines under it are replaced. A poll must not rebuild what has focus.
 */
export function updateSummary(root: HTMLElement, view: BoxHealthView): void {
  const summary = summaryOf(view);
  root.classList.toggle("is-stale", summary.stale);
  const age = root.querySelector(".bh-age");
  if (age) age.textContent = summary.age;
  const pill = root.querySelector<HTMLElement>(".bh-worst");
  if (pill) setWorst(pill, worstOf(view));
  const rest = root.querySelector(".bh-rest");
  if (rest) replaceChildren(rest, restNodes(view));
}

// ── the Details dialog ──────────────────────────────────────────────────────

/** A sparkline: an inline SVG polyline (120 by 24); a missing point breaks the line. */
export function sparkline(values: Array<number | null>, label: string): SVGElement {
  const ns = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(ns, "svg");
  svg.setAttribute("viewBox", "0 0 120 24");
  svg.setAttribute("width", "120");
  svg.setAttribute("height", "24");
  svg.setAttribute("role", "img");
  svg.setAttribute("class", "bh-spark");
  const present = values.filter((v): v is number => v != null);
  const high = present.length ? Math.max(...present) : 0;
  const low = present.length ? Math.min(...present) : 0;
  const span = high - low || 1;
  svg.setAttribute(
    "aria-label",
    present.length ? `${label}, highest ${Math.round(high)}` : `${label}, no history yet`,
  );
  const step = values.length > 1 ? 120 / (values.length - 1) : 0;
  let run: string[] = [];
  const flush = () => {
    if (run.length > 1) {
      const line = document.createElementNS(ns, "polyline");
      line.setAttribute("points", run.join(" "));
      svg.appendChild(line);
    } else if (run.length === 1) {
      const [x, y] = run[0].split(",");
      const dotNode = document.createElementNS(ns, "circle");
      dotNode.setAttribute("cx", x);
      dotNode.setAttribute("cy", y);
      dotNode.setAttribute("r", "1");
      svg.appendChild(dotNode);
    }
    run = [];
  };
  values.forEach((value, index) => {
    if (value == null) return flush();
    const x = (index * step).toFixed(1);
    const y = (22 - ((value - low) / span) * 20).toFixed(1);
    run.push(`${x},${y}`);
  });
  flush();
  return svg;
}

type Cell = string | Node | null | undefined | false;

function table(headers: string[], rows: Cell[][]): HTMLElement {
  return el(
    "div",
    { class: "bh-scroll" },
    el(
      "table",
      { class: "bh-table" },
      el("thead", {}, el("tr", {}, ...headers.map((h) => el("th", { scope: "col" }, h)))),
      el(
        "tbody",
        {},
        ...rows.map((row) => el("tr", {}, ...row.map((cell) => el("td", {}, cell)))),
      ),
    ),
  );
}

function section(title: string, tip: string | null, ...body: Array<Node | null>): HTMLElement {
  return el(
    "section",
    { class: "bh-sec" },
    el("h4", { class: "bh-sec-h" }, title, tip ? hint(tip, title) : null),
    ...body,
  );
}

const line = (text: string): HTMLElement => el("p", { class: "bh-line" }, text);

function whereText(root: BoxClaudeRoot): string {
  return { tmp: "/tmp", cache: "cache", container: "container" }[root.where];
}

function claudeRows(users: BoxUser[]): Cell[][] {
  const rows: Cell[][] = [];
  for (const user of users) {
    for (const root of user.claude) {
      if (!root.readable) {
        const age = root.top_age_days == null ? "" : `, changed ${root.top_age_days.toFixed(1)} d ago`;
        rows.push([
          user.label,
          whereText(root),
          `not readable (aged by the box rule${age})`,
          "",
          "",
          "",
        ]);
      } else {
        rows.push([
          user.label,
          whereText(root),
          root.had_unreadable
            ? `${countText(root.sessions)} (some folders unreadable)`
            : countText(root.sessions),
          countText(root.stale_sessions),
          `${countText(root.files)}${root.partial ? "+" : ""}`,
          `${sizeText(root.bytes)}${root.partial ? "+" : ""}`,
        ]);
      }
    }
  }
  return rows;
}

function litterRows(users: BoxUser[]): Cell[][] {
  return users.flatMap((user) =>
    user.litter.map((group) => [
      user.label,
      group.group,
      countText(group.entries),
      countText(group.stale),
      group.bytes == null ? "n/a" : sizeText(group.bytes),
    ]),
  );
}

function worktreeText(user: BoxUser): string | null {
  const w = user.worktrees;
  if (!w) return null;
  const parts = [
    `${w.count} worktree${w.count === 1 ? "" : "s"}`,
    w.bytes == null ? null : sizeText(w.bytes),
    `${w.older_14d} older than 14 days`,
    w.with_changes == null ? null : `${w.with_changes}${w.partial ? "+" : ""} with changes`,
  ].filter(Boolean);
  return parts.join(", ");
}

/** The Details dialog's content. */
export function detailsBody(view: BoxHealthView): HTMLElement {
  const d = view.details;
  const users = d.users ?? [];
  const body = el("div", { class: "bh-details-body" });
  body.appendChild(
    line(
      [
        `Read ${clock(view.read_at)} (${agoText(view.age_s)})`,
        view.slow_read_at ? `slow scans ${clock(view.slow_read_at)}` : null,
        upText(d.system?.uptime_s),
        view.host,
      ]
        .filter(Boolean)
        .join(" · "),
    ),
  );

  body.appendChild(
    section(
      "Needs you",
      null,
      view.needs.length
        ? el(
            "ul",
            { class: "bh-needs bh-needs-full" },
            ...view.needs.map((need) =>
              el(
                "li",
                { class: `bh-need bh-t-${need.tone}` },
                dot(need.tone),
                el(
                  "span",
                  { class: "bh-need-text" },
                  need.text,
                  need.detail ? el("span", { class: "bh-need-detail" }, need.detail) : null,
                ),
              ),
            ),
          )
        : line("Nothing needs you."),
    ),
  );

  body.appendChild(cleanupSection(view));

  const heat = d.heat;
  const sensors = [
    heat?.cpu_c != null ? `CPU ${Math.round(heat.cpu_c)}C` : null,
    heat?.chassis_c != null ? `chassis ${Math.round(heat.chassis_c)}C` : null,
    heat?.nvme_c != null ? `SSD ${Math.round(heat.nvme_c)}C` : null,
    heat?.gpu_c != null
      ? `GPU ${Math.round(heat.gpu_c)}C${heat.gpu_power_w ? ` at ${Math.round(heat.gpu_power_w)} W` : ""}`
      : null,
  ].filter(Boolean);
  const top = (values: Array<number | null>) => {
    const present = values.filter((v): v is number => v != null);
    return present.length ? `highest ${Math.round(Math.max(...present))}` : "no history";
  };
  body.appendChild(
    section(
      "Heat",
      null,
      line(sensors.join(", ") || "No sensors read."),
      el(
        "div",
        { class: "bh-sparks" },
        el(
          "span",
          { class: "bh-spark-item" },
          el("span", { class: "bh-spark-name" }, "CPU 24 h"),
          sparkline(view.history.cpu_c, "CPU temperature over 24 hours"),
          el("span", { class: "bh-spark-top" }, top(view.history.cpu_c)),
        ),
        el(
          "span",
          { class: "bh-spark-item" },
          el("span", { class: "bh-spark-name" }, "SSD 24 h"),
          sparkline(view.history.nvme_c, "SSD temperature over 24 hours"),
          el("span", { class: "bh-spark-top" }, top(view.history.nvme_c)),
        ),
      ),
    ),
  );

  const cpu = d.cpu;
  body.appendChild(
    section(
      "CPU",
      null,
      line(
        cpu
          ? `Load ${cpu.load1.toFixed(1)}, ${cpu.load5.toFixed(1)}, ${cpu.load15.toFixed(1)} on ${cpu.threads} threads` +
              (cpu.busy_pct != null ? ` · busy ${Math.round(cpu.busy_pct)}%` : "") +
              (cpu.psi_some_avg60 != null ? ` · pressure ${Math.round(cpu.psi_some_avg60)}%` : "")
          : "Not read.",
      ),
      el(
        "div",
        { class: "bh-sparks" },
        el(
          "span",
          { class: "bh-spark-item" },
          el("span", { class: "bh-spark-name" }, "Load 24 h"),
          sparkline(view.history.load1, "Load over 24 hours"),
          el("span", { class: "bh-spark-top" }, top(view.history.load1)),
        ),
      ),
    ),
  );

  const mem = d.memory;
  body.appendChild(
    section(
      "Memory",
      null,
      line(
        mem
          ? `${sizeText(mem.used_b)} of ${sizeText(mem.total_b)} in use (${Math.round(mem.used_pct ?? 0)}%)` +
              ` · swap ${sizeText(mem.swap_total_b - mem.swap_free_b)} of ${sizeText(mem.swap_total_b)}`
          : "Not read.",
      ),
      mem?.tmpfs.length
        ? line(
            "tmpfs, counted in the use above: " +
              mem.tmpfs.map((t) => `${t.path} ${sizeText(t.used_b)}`).join(", "),
          )
        : null,
    ),
  );

  body.appendChild(
    section(
      "Storage",
      null,
      table(
        ["Mount", "Type", "Space", "Inodes"],
        (d.mounts ?? []).map((m) => [
          m.path,
          m.fstype,
          `${m.space_pct == null ? "n/a" : `${Math.round(m.space_pct)}%`} ${sizeText(m.used_b)}/${sizeText(m.size_b)}`,
          m.inodes ? `${Math.round(m.inodes_pct ?? 0)}% ${countText(m.inodes_used)}/${countText(m.inodes)}` : "n/a",
        ]),
      ),
    ),
  );

  const guard = d.guard;
  body.appendChild(
    section(
      "Claude temp",
      BOX_HEALTH_HINTS.claude,
      table(["User", "Where", "Sessions", "Stale", "Items", "Size"], claudeRows(users)),
      guard
        ? line(
            guard.running
              ? `${guard.locked} live session${guard.locked === 1 ? "" : "s"} protected` +
                  (guard.claude_remote ? ` · ${guard.claude_remote} Remote Control` : "") +
                  (guard.claude_unmatched
                    ? ` · ${guard.claude_unmatched} not matched to a folder`
                    : "")
              : "The temp guard is not running, so a long session can lose temp files.",
          )
        : null,
    ),
  );

  body.appendChild(
    section(
      "/tmp",
      BOX_HEALTH_HINTS.tmp,
      table(["User", "Group", "Entries", "Old", "Files size"], litterRows(users)),
    ),
  );

  const wt = users.flatMap((u) => {
    const text = worktreeText(u);
    return text ? [[u.label, u.kind === "container" ? "container" : "box", text]] : [];
  });
  body.appendChild(
    section(
      "Worktrees",
      BOX_HEALTH_HINTS.worktrees,
      wt.length ? table(["User", "Where", "Counts"], wt) : line("None counted."),
    ),
  );

  const containers = users.filter((u) => u.kind === "container");
  if (containers.length) {
    body.appendChild(
      section(
        "Containers",
        null,
        table(
          ["User", "State", "Writable layer", "Home volume"],
          containers.map((u) => [
            u.label,
            u.running === false ? "not running" : "running",
            sizeText(u.writable_b),
            sizeText(u.home_b),
          ]),
        ),
      ),
    );
  }

  const docker = d.docker;
  body.appendChild(
    section(
      "Other",
      null,
      docker
        ? line(
            `Docker: ${docker.images} images ${sizeText(docker.images_b)} ` +
              `(${sizeText(docker.images_reclaim_b)} can be freed), ${docker.volumes} volumes ` +
              `${sizeText(docker.volumes_b)}, build cache ${sizeText(docker.build_cache_b)}`,
          )
        : line("Docker: not read."),
      line(
        `journald ${sizeText(d.journal_b)}` +
          (d.dirs?.length
            ? ` · ${d.dirs.map((x) => `${x.label} ${sizeText(x.bytes)}${x.partial ? "+" : ""}`).join(" · ")} (Claude temp not counted)`
            : ""),
      ),
    ),
  );

  const failedUnits = [...(d.system?.failed_units ?? []), ...(d.system?.failed_user_units ?? [])];
  const otherUnits = (d.system?.failed_units_other ?? 0) + (d.system?.failed_user_units_other ?? 0);
  body.appendChild(
    section(
      "System",
      null,
      line(
        failedUnits.length || otherUnits
          ? `Failed units: ${[...failedUnits, ...(otherUnits ? [`${otherUnits} other (not named)`] : [])].join(", ")}`
          : "No failed units.",
      ),
    ),
  );

  body.appendChild(
    section(
      "Settings",
      null,
      el(
        "ul",
        { class: "bh-settings" },
        ...view.settings.map((s) =>
          el(
            "li",
            { class: "bh-setting" },
            el("span", { class: "bh-setting-name" }, s.name),
            el("span", { class: "bh-setting-value" }, s.value),
            hint(s.hint, s.name),
          ),
        ),
      ),
    ),
  );
  return body;
}

let dialog: { close: () => void } | null = null;

/** The open dialog's own state: the view it shows, the confirm step if one is open. */
interface DialogState {
  view: BoxHealthView;
  step: BoxActionOffer | null;
  busy: boolean;
  wrap: HTMLElement;
  /** Changes when something the Cleanup section draws changes. */
  sig: string;
}

let dlg: DialogState | null = null;
/** The last cleanup failure, in the server's words; cleared when the next try starts. */
let problem: string | null = null;
/** What the section hands the buttons: the context, a forced re-read and a summary redraw. */
let controller: { context: ExtensionContext; refreshNow: () => Promise<void>; draw: () => void } | null = null;

const cleanupSig = (view: BoxHealthView): string =>
  JSON.stringify([view.actions ?? null, view.cleanup ?? null, view.details.cleanup ?? null]);

function drawDialog(): void {
  if (!dlg) return;
  const scroller = dlg.wrap.closest<HTMLElement>(".ae-dialog-body");
  const top = scroller?.scrollTop ?? 0;
  const focused = focusedControl(dlg.wrap);
  replaceChildren(dlg.wrap, [dlg.step ? confirmNode(dlg.step) : detailsBody(dlg.view)]);
  if (scroller) scroller.scrollTop = top;
  restoreFocus(dlg.wrap, focused);
}

interface FocusMark {
  tag: string;
  label: string;
  nth: number;
}

/** The control that has focus inside the dialog body, by what it says and which of its twins it is. */
function focusedControl(wrap: HTMLElement): FocusMark | null {
  const active = document.activeElement;
  if (!(active instanceof HTMLElement) || !wrap.contains(active)) return null;
  const label = (active.textContent ?? "").trim();
  const twins = [...wrap.querySelectorAll<HTMLElement>(active.tagName)].filter(
    (e) => (e.textContent ?? "").trim() === label,
  );
  return { tag: active.tagName, label, nth: Math.max(0, twins.indexOf(active)) };
}

/** After a redraw put focus back on the matching control, else on the dialog title, never on the body. */
function restoreFocus(wrap: HTMLElement, mark: FocusMark | null): void {
  if (!mark) return;
  const twins = [...wrap.querySelectorAll<HTMLElement>(mark.tag)].filter(
    (e) => (e.textContent ?? "").trim() === mark.label && !(e as HTMLButtonElement).disabled,
  );
  const target = twins[mark.nth] ?? twins[0];
  if (target) {
    target.focus();
    return;
  }
  const title = wrap.closest<HTMLElement>("[role=dialog]")?.querySelector<HTMLElement>("h2, h3");
  if (title) {
    title.tabIndex = -1;
    title.focus();
  }
}

/** A fresh view reached the open dialog: redraw only if the Cleanup part changed. */
function syncDialog(view: BoxHealthView): void {
  if (!dlg) return;
  dlg.view = view;
  const sig = cleanupSig(view);
  if (sig === dlg.sig) return;
  dlg.sig = sig;
  if (dlg.step) {
    const step = dlg.step;
    const now = offersOf(view).find((o) => o.id === step.id && o.owner === step.owner);
    if (!now || now.confirm !== step.confirm) {
      dlg.step = null;
      problem = "The numbers changed since you opened that. Check the new numbers and try again.";
    }
  }
  drawDialog();
}

async function doRun(offer: BoxActionOffer): Promise<void> {
  const mine = dlg;
  if (!mine || !controller || mine.busy) return;
  problem = null;
  mine.busy = true;
  drawDialog();
  try {
    await requestBoxCleanup(controller.context, {
      action: offer.id,
      owner: offer.owner,
      count: offer.count,
    });
    mine.step = null;
  } catch (error) {
    problem = describeError(error);
    // The numbers moved under the confirm: close it so the new ones are read first.
    if (error instanceof AeRequestError && error.code === "box_health_changed") mine.step = null;
  }
  mine.busy = false;
  await controller?.refreshNow();
  if (dlg === mine) drawDialog();
  controller?.draw();
}

async function doCancel(): Promise<void> {
  if (!controller) return;
  problem = null;
  try {
    await cancelBoxCleanup(controller.context);
  } catch (error) {
    problem = describeError(error);
  }
  await controller?.refreshNow();
  if (dlg) drawDialog();
  controller?.draw();
}

/**
 * Open the Details dialog for *view*, returning focus to *opener* when it closes. With
 * *offer*, it opens straight on that cleanup's confirm step.
 */
export function openDetails(
  view: BoxHealthView,
  opener: HTMLElement | null,
  offer?: BoxActionOffer | null,
): void {
  dialog?.close();
  problem = null;
  const wrap = el("div", { class: "bh-dlg" });
  const state: DialogState = { view, step: offer ?? null, busy: false, wrap, sig: cleanupSig(view) };
  dlg = state;
  const open = openDialog({
    title: "Box health details",
    body: wrap,
    opener,
    id: "bh-dialog",
    closeLabel: "Close box health details",
    closeTitle: BOX_HEALTH_HINTS.close,
    className: "bh-dialog-card",
    onClose: () => {
      dialog = null;
      if (dlg === state) dlg = null;
    },
  });
  dialog = open;
  drawDialog();
}

// ── the section ─────────────────────────────────────────────────────────────

interface SectionState {
  view: BoxHealthView | null;
  at: number;
  error: unknown;
}

let timer: ReturnType<typeof setTimeout> | null = null;
let onFocus: (() => void) | null = null;
/** Bumped by every activate and deactivate, as in host.ts. */
let generation = 0;

/** The monitor's block, for activateFabric to put first in the Box health card. */
export function boxHealthSection(): HTMLElement {
  return el(
    "div",
    { class: "admin-block admin-boxhealth" },
    el("p", { class: "meta refreshed bh-refreshed" }),
    el("div", { class: "bh-body", "aria-label": "Box health" }),
    el("div", { class: "page-status bh-status", "aria-live": "polite" }),
  );
}

function render(body: HTMLElement, refreshed: HTMLElement | null, state: SectionState): void {
  const failure = (): HTMLElement =>
    notice(`The box health could not be read (${describeError(state.error)}).`, "error");
  if (!state.view) {
    replaceChildren(body, [
      state.error ? failure() : el("p", { class: "meta" }, "Reading..."),
    ]);
  } else {
    const summary = body.querySelector<HTMLElement>(".bh");
    let note = body.querySelector<HTMLElement>(".bh-note");
    if (summary && note) {
      // Update in place: a poll must not rebuild the Details button (focus returns to
      // it when the dialog closes) or close an open hint.
      updateSummary(summary, state.view);
    } else {
      note = el("div", { class: "bh-note" });
      replaceChildren(body, [
        summaryNode(state.view, (opener) => {
          if (state.view) openDetails(state.view, opener);
        }),
        note,
      ]);
    }
    replaceChildren(note, [state.error ? failure() : null]);
  }
  if (refreshed) {
    refreshed.textContent = state.at
      ? `Updated ${new Date(state.at).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })}`
      : "";
  }
  const tile = tileOf(state.view, state.error);
  setTile(state.view ? staleTile(tile, state.error) : tile, state.at || undefined);
}

/** Read and draw the section inside *root* (from boxHealthSection, or the card holding it). */
export function activateBoxHealth(context: ExtensionContext, root: HTMLElement): void {
  const body = root.querySelector<HTMLElement>(".bh-body");
  const refreshed = root.querySelector<HTMLElement>(".bh-refreshed");
  if (!body) throw new Error("Box health section is missing");
  const mine = ++generation;
  const live = () => generation === mine;
  if (onFocus) {
    window.removeEventListener("focus", onFocus);
    document.removeEventListener("visibilitychange", onFocus);
  }
  const state: SectionState = { view: null, at: 0, error: null };
  let reading: Promise<void> | null = null;
  const draw = () => {
    if (!live()) return;
    render(body, refreshed, state);
    if (state.view) syncDialog(state.view);
  };
  const schedule = () => {
    if (!live()) return;
    if (timer) clearTimeout(timer);
    // While a cleanup is queued, untaken or running, look every 10 s to see it land.
    const fast = OPEN_STATES.includes(state.view?.cleanup?.state ?? "");
    timer = setTimeout(() => {
      if (live() && document.visibilityState === "visible") void refresh();
    }, fast ? FAST_POLL_MS : POLL_MS);
  };
  const refresh = (): Promise<void> => {
    if (reading) return reading;
    reading = fetchBoxHealth(context)
      .then(
        (answer) => {
          if (!live()) return;
          state.view = answer;
          state.at = Date.now();
          state.error = null;
        },
        (error: unknown) => {
          // The last good summary stays, with the failure said under it.
          if (live()) state.error = error ?? new Error("Request failed");
        },
      )
      .then(() => {
        draw();
        schedule();
      })
      .finally(() => {
        reading = null;
      });
    return reading;
  };
  controller = {
    context,
    draw,
    refreshNow: async () => {
      if (reading) await reading;
      if (live()) await refresh();
    },
  };
  draw();
  void refresh();
  registerRefresh("boxhealth", () => (live() ? refresh() : undefined));
  onFocus = () => {
    if (document.visibilityState === "visible") void refresh();
  };
  window.addEventListener("focus", onFocus);
  document.addEventListener("visibilitychange", onFocus);
}

export function deactivateBoxHealth(): void {
  generation++;
  if (timer) clearTimeout(timer);
  timer = null;
  dialog?.close();
  dialog = null;
  dlg = null;
  controller = null;
  problem = null;
  if (onFocus) {
    window.removeEventListener("focus", onFocus);
    document.removeEventListener("visibilitychange", onFocus);
  }
  onFocus = null;
}
