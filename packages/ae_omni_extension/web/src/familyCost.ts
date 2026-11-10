// The Families tab of the Admin Center's Spend card (2026-10-29 as its own Family cost card):
// this week's (or month's) spend per session family, a lead plus every session it started and
// their native subagents, so a runaway family shows at a glance.
//
// Why: spend is easy to see per account and per preset but not per piece of work, and one lead
// that keeps restarting and spawning reviewers can cost more than a day of everything else. The
// server (`GET /v1/ae/family-cost`, family_cost.py) sums each family from the sessions' own
// usage every 15 minutes and answers from that, so the first read after a restart says
// "Reading" and the next poll has the numbers. An admin sees every family on every host (each
// row has a host chip); anyone else sees only their own.
//
// 2026-10-30: the card folded into the Presets card, now "Spend" (presets.ts). The 7 d / 30 d
// window is the card's one toggle (spend.ts), the footnotes sit in the card header's info icon,
// and a row is one line (title, host, share bar, amount) that opens to its roles and counts.
// The flag has two amounts, tokens and dollars, and a family is flagged when its spend today
// passes either (0 turns one off); the unit toggle only picks which number each row shows and
// the ranking. Why: the old card had one threshold at a time, picked by the unit, and its token
// box took millions, so typing 5000000 became 5e12 and the server refused it. The amounts are
// typed as 5M, 500K or 5,000,000 now (parseTokenAmount). Both are saved per user on the server
// (`POST /v1/ae/family-cost/settings`). Dollars are an estimate at API rates; on a subscription
// they show where usage goes, not the invoice.

import type { ExtensionContext } from "@omnigent/extension-sdk";
import {
  describeError,
  fetchFamilyCost,
  saveFamilyCostSettings,
  type CostUnit,
  type FamilyCostAnswer,
  type FamilyCostSettings,
  type FamilyRole,
  type FamilyRow,
} from "./api";
import { money } from "./azure";
import { liveNeedsLink } from "./attentionStore";
import { el, notice, replaceChildren } from "./dom";
import { fold, foldLine } from "./fold";
import { hint } from "./hint";
import { checkIcon } from "./icons";
import {
  DEFAULT_DAYS,
  onSpend,
  setFlaggedCount,
  setNotes,
  spendDays,
  syncDays,
  WINDOWS,
} from "./spend";
import { registerRefresh, setTile, staleTile, type Tile } from "./tiles";

export { DEFAULT_DAYS, WINDOWS };
export const POLL_MS = 60_000;
/** While the server has no sweep yet, so the numbers show in seconds. */
export const WARMING_POLL_MS = 4_000;
/** The server sweeps every 15 minutes; an answer much older means the sweep is failing. */
export const STALE_MS = 45 * 60_000;
/** How long the Saved tick stays after a limit is saved. */
export const SAVED_MS = 2_000;
/** What the server accepts (role_prefs.py MAX_RUNAWAY_TOKENS and MAX_RUNAWAY_USD). */
export const MAX_TOKENS = 1_000_000_000;
export const MAX_USD = 100_000;
/** The list box scrolls past this height. */
export const LIST_MAX_PX = 420;
/** One-line helper texts (140 characters at most; familyCost.test.ts holds them to it). */
export const UNIT_HINT =
  "Which number each family row shows, and how they rank; the other appears on hover.";
export const RUNAWAY_HINT =
  "Either amount flags a family whose spend today passes it. Tokens skip cache reads. Counts the lead and helpers. 0 or blank turns it off.";
export const COST_NOTE =
  "Dollars are estimated at API rates. On a subscription they show where usage goes, not what the invoice says.";
export const TOKENS_NOTE =
  "Family tokens count input (with cache writes), output and cache reads, so they run higher than preset tokens.";
export const TODAY_NOTE = "Today means Pacific time (the day the flag amounts are checked against).";
export const TRUNCATED_NOTE = "The list is cut to the biggest families.";
export const DEFAULT_SETTINGS: FamilyCostSettings = {
  unit: "tokens",
  runaway_tokens: 5_000_000,
  runaway_usd: 25,
};

const ROLE_NAMES: Record<string, string> = {
  lead: "lead incl. subagents",
  builder: "builder",
  reviewer: "reviewer",
  codex: "Codex",
  other: "other",
};
const ROLE_ORDER = ["lead", "builder", "reviewer", "codex", "other"];

// ── words and numbers ───────────────────────────────────────────────────────

const plural = (n: number, one: string, many = `${one}s`) => `${n} ${n === 1 ? one : many}`;

/** "950", "12.3K", "4.2M", "1.1B"; a dash for nothing. */
export function tokensText(value: number | null | undefined): string {
  if (value === null || value === undefined) return "-";
  for (const [limit, suffix] of [
    [1e9, "B"],
    [1e6, "M"],
    [1e3, "K"],
  ] as const) {
    if (value >= limit) {
      const figure = value / limit;
      return `${figure < 100 ? figure.toFixed(1) : Math.round(figure)}${suffix}`;
    }
  }
  return String(Math.round(value));
}

/** The number shown first for *unit*: "$12.40" or "4.2M tokens". */
export function amountText(unit: CostUnit, cost: number, tokens: number): string {
  return unit === "dollars" ? money(cost) : `${tokensText(tokens)} tokens`;
}

/** The row's number as a short figure ("895M", "$234"); the unit word lives in the hover title. */
export function figureText(unit: CostUnit, cost: number, tokens: number): string {
  return unit === "dollars" ? money(cost) : tokensText(tokens);
}

/** What a row's number says on hover: the full text, the other unit, then the breakdown. */
export function hoverText(unit: CostUnit, row: FamilyRow): string {
  const shown = amountText(unit, row.cost_usd, row.tokens.total);
  const other = amountText(unit === "dollars" ? "tokens" : "dollars", row.cost_usd, row.tokens.total);
  return (
    `${shown}. ${other}. Input ${tokensText(row.tokens.input)} (with cache writes), ` +
    `output ${tokensText(row.tokens.output)}, cache reads ${tokensText(row.tokens.cache_read)}.`
  );
}

/** A family's spend in *unit*, for ranking and shares. */
export function spendOf(row: FamilyRow, unit: CostUnit): number {
  return unit === "dollars" ? row.cost_usd : row.tokens.total;
}

/** The families biggest first in *unit* (the server ranks in the saved unit; a local switch re-ranks). */
export function rankFamilies(rows: readonly FamilyRow[], unit: CostUnit): FamilyRow[] {
  return [...rows].sort((a, b) => spendOf(b, unit) - spendOf(a, unit) || a.lead_id.localeCompare(b.lead_id));
}

/** A row's share of the biggest row, 0 to 100, for its bar. */
export function sharePct(row: FamilyRow, rows: readonly FamilyRow[], unit: CostUnit): number {
  const top = Math.max(0, ...rows.map((r) => spendOf(r, unit)));
  return top > 0 ? Math.round((spendOf(row, unit) / top) * 100) : 0;
}

/** The role chips in a fixed order, the lead first, skipping a role with nothing in it. */
export function sortedRoles(roles: readonly FamilyRole[]): FamilyRole[] {
  const rank = (role: string) => {
    const at = ROLE_ORDER.indexOf(role);
    return at < 0 ? ROLE_ORDER.length : at;
  };
  return [...roles].sort((a, b) => rank(a.role) - rank(b.role));
}

/** "lead incl. subagents 3.2M", "builder $4.10": one chip's text. */
export function roleChipText(role: FamilyRole, unit: CostUnit): string {
  const amount = unit === "dollars" ? money(role.cost_usd) : tokensText(role.tokens_total);
  return `${ROLE_NAMES[role.role] ?? role.role} ${amount}`;
}

/** Families whose title or host holds *query*, ignoring case; every family for a blank query. */
export function filterFamilies(rows: readonly FamilyRow[], query: string): FamilyRow[] {
  const needle = query.trim().toLowerCase();
  if (!needle) return [...rows];
  return rows.filter((row) =>
    `${row.title ?? ""}\n${row.host ?? ""}`.toLowerCase().includes(needle),
  );
}

// ── typed amounts ───────────────────────────────────────────────────────────

export type AmountRead = { ok: true; value: number } | { ok: false; error: string };

const MULTIPLIER: Record<string, number> = { k: 1e3, m: 1e6, b: 1e9 };
const NUMBER = /^(\d+(?:\.\d+)?|\.\d+)([kmb])?$/i;
// Commas are thousands groups only (2026-10-30): "5,5M" is not 55M, it is a typo.
const GROUPED = /^\d{1,3}(,\d{3})+(\.\d+)?[kmb]?$/i;
// Spaces, thin and no-break spaces and underscores are digit separators; commas are checked apart.
const SEPARATORS = /[\s\u00a0\u202f_]/g;
export const TOKEN_FORMAT_ERROR = "Type an amount like 5M, 500K or 5,000,000.";
export const TOKEN_MAX_ERROR = "The most is 1B tokens (1,000,000,000) a day.";
export const TOKEN_MIN_ERROR = "The least is 1 token. Type 0 to turn this flag off.";
export const USD_FORMAT_ERROR = "Type a dollar amount like 25 or 25.50.";
export const USD_MAX_ERROR = "The most is $100,000 a day.";
export const USD_MIN_ERROR = "The least is $0.01. Type 0 to turn this flag off.";

/** Drop the digit separators; null when a comma is not a thousands group. */
function plainNumber(text: string): string | null {
  const squeezed = text.replace(SEPARATORS, "");
  if (!squeezed.includes(",")) return squeezed;
  return GROUPED.test(squeezed) ? squeezed.replace(/,/g, "") : null;
}

/**
 * Read a token amount as typed: "5M", "5.5m", "500K", "1B", "5,000,000", "5 000 000" or
 * "5000000". A plain number is tokens. Blank is 0 (off). Anything else, a negative, a comma that
 * is not a thousands group, a nonzero amount under 1 token, or more than the server takes (1B) is
 * an error that says what to type.
 */
export function readTokenAmount(raw: string): AmountRead {
  const text = plainNumber(raw.trim().replace(/\s*tokens?$/i, ""));
  if (text === null) return { ok: false, error: TOKEN_FORMAT_ERROR };
  if (text === "") return { ok: true, value: 0 };
  const found = NUMBER.exec(text);
  if (!found) return { ok: false, error: TOKEN_FORMAT_ERROR };
  const typed = Number(found[1]) * (found[2] ? MULTIPLIER[found[2].toLowerCase()]! : 1);
  const value = Math.round(typed);
  if (!Number.isFinite(value) || value > MAX_TOKENS) return { ok: false, error: TOKEN_MAX_ERROR };
  // "0.4" tokens would round to 0, which means off; that is not what was typed.
  if (typed > 0 && value === 0) return { ok: false, error: TOKEN_MIN_ERROR };
  return { ok: true, value };
}

/** The same as a number, or null when it cannot be read. */
export function parseTokenAmount(raw: string): number | null {
  const read = readTokenAmount(raw);
  return read.ok ? read.value : null;
}

/** Read dollars as typed: "$25", "25", "25.50", "1,000". Blank is 0 (off). */
export function readDollarAmount(raw: string): AmountRead {
  const text = plainNumber(raw.trim().replace(/\$/g, ""));
  if (text === null) return { ok: false, error: USD_FORMAT_ERROR };
  if (text === "") return { ok: true, value: 0 };
  const found = NUMBER.exec(text);
  if (!found || found[2]) return { ok: false, error: USD_FORMAT_ERROR };
  const typed = Number(found[1]);
  const value = Math.round(typed * 100) / 100;
  if (!Number.isFinite(value) || value > MAX_USD) return { ok: false, error: USD_MAX_ERROR };
  if (typed > 0 && value === 0) return { ok: false, error: USD_MIN_ERROR };
  return { ok: true, value };
}

export function parseDollarAmount(raw: string): number | null {
  const read = readDollarAmount(raw);
  return read.ok ? read.value : null;
}

/** A saved token amount as the input shows it: "5M", "500K", "1.5B"; plain digits when no short form reads back the same. */
export function shortTokens(value: number): string {
  for (const [limit, suffix] of [
    [1e9, "B"],
    [1e6, "M"],
    [1e3, "K"],
  ] as const) {
    if (value >= limit) {
      const text = `${Number((value / limit).toFixed(2))}${suffix}`;
      if (parseTokenAmount(text) === value) return text;
      break;
    }
  }
  return String(value);
}

/** A saved dollar amount as the input shows it: "25", "25.5". */
export function shortDollars(value: number): string {
  return String(Number(value.toFixed(2)));
}

// ── the flag ────────────────────────────────────────────────────────────────

/** Which of the two amounts *row*'s spend today has passed (an amount of 0 is off). */
export function passed(row: FamilyRow, settings: FamilyCostSettings): { tokens: boolean; usd: boolean } {
  return {
    tokens:
      settings.runaway_tokens > 0 &&
      (row.today.flag_tokens ?? row.today.tokens_total) >= settings.runaway_tokens,
    usd: settings.runaway_usd > 0 && row.today.cost_usd >= settings.runaway_usd,
  };
}

/** Whether *row* is flagged: either amount passed today. */
export function isFlagged(row: FamilyRow, settings: FamilyCostSettings): boolean {
  const over = passed(row, settings);
  return over.tokens || over.usd;
}

/** The families flagged now, by the viewer's own amounts (not the server's last count, which an edit just made would outdate). */
export function flaggedFamilies(
  answer: FamilyCostAnswer | null,
  settings: FamilyCostSettings,
): FamilyRow[] {
  if (!answer || answer.state !== "ready") return [];
  return (answer.families ?? []).filter((row) => isFlagged(row, settings));
}

/**
 * How many families are flagged. The server counts over every family, but trims the list it sends
 * (`truncated`), so when it echoed the same amounts the viewer has now its count is the whole
 * truth. Right after a local edit the echo is older, so the rows we have are counted instead
 * until the next poll (2026-10-30).
 */
export function flaggedTotal(answer: FamilyCostAnswer | null, settings: FamilyCostSettings): number {
  const local = flaggedFamilies(answer, settings).length;
  const echoed = answer?.settings;
  const counted = answer?.totals?.flagged;
  if (
    answer?.state === "ready" &&
    echoed &&
    typeof counted === "number" &&
    echoed.runaway_tokens === settings.runaway_tokens &&
    echoed.runaway_usd === settings.runaway_usd
  ) {
    return Math.max(counted, local);
  }
  return local;
}

/** "5M tokens" or "$25": a flag amount as the Needs you line says it. */
export function tokenLimitText(settings: FamilyCostSettings): string {
  return `${shortTokens(settings.runaway_tokens)} tokens`;
}
export function dollarLimitText(settings: FamilyCostSettings): string {
  return `$${shortDollars(settings.runaway_usd)}`;
}

/** The Needs you line, or null when no family passed either amount today (or both are off). */
export function needsYou(
  answer: FamilyCostAnswer | null,
  settings: FamilyCostSettings,
): string | null {
  const over = flaggedFamilies(answer, settings);
  const total = flaggedTotal(answer, settings);
  if (!total) return null;
  // Name the amounts that were actually passed, so "5M tokens or $25" says which one tripped.
  // When the flagged families were all cut from the list, name the amounts that are on.
  const amounts: string[] = [];
  if (over.length ? over.some((row) => passed(row, settings).tokens) : settings.runaway_tokens > 0) {
    amounts.push(tokenLimitText(settings));
  }
  if (over.length ? over.some((row) => passed(row, settings).usd) : settings.runaway_usd > 0) {
    amounts.push(dollarLimitText(settings));
  }
  const head = `${plural(total, "family", "families")} passed ${amounts.join(" or ")} today`;
  if (!over.length) return `${head}.`;
  const names = over
    .slice(0, 3)
    .map((row) => row.title || row.lead_id.slice(0, 8))
    .join(", ");
  const more = total > over.slice(0, 3).length ? ` and ${total - over.slice(0, 3).length} more` : "";
  return `${head}: ${names}${more}.`;
}

/** The viewer's settings: what the server echoed, else the defaults. */
export function settingsOf(answer: FamilyCostAnswer | null): FamilyCostSettings {
  return answer?.settings ?? { ...DEFAULT_SETTINGS, unit: answer?.unit_default ?? "tokens" };
}

/** The server's `computed_at` as epoch ms; 0 when there is none (never the client clock). */
export function answerTime(answer: FamilyCostAnswer | null | undefined): number {
  const parsed = answer?.computed_at ? Date.parse(answer.computed_at) : NaN;
  return Number.isNaN(parsed) ? 0 : parsed;
}

export function answerIsStale(answer: FamilyCostAnswer | null | undefined, now = Date.now()): boolean {
  const asOf = answerTime(answer);
  return asOf > 0 && now - asOf > STALE_MS;
}

export function updatedText(answer: FamilyCostAnswer | null | undefined, now = Date.now()): string {
  const asOf = answerTime(answer);
  if (!asOf) return "";
  const time = new Date(asOf).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
  return `Updated ${time}${answerIsStale(answer, now) ? ", refresh failing" : ""}`;
}

/** The footnotes of this panel, for the card's info icon. */
export function familyNotes(answer: FamilyCostAnswer | null): string[] {
  const notes = [TOKENS_NOTE, COST_NOTE, TODAY_NOTE];
  if (answer?.state === "ready") {
    if (answer.partial && answer.history_since) {
      notes.push(`History starts ${answer.history_since}; older chats count from then.`);
    }
    if (answer.truncated) notes.push(TRUNCATED_NOTE);
  }
  return notes;
}

export function familyTile(
  answer: FamilyCostAnswer | null,
  error: unknown,
  unit: CostUnit,
  days = DEFAULT_DAYS,
  now = Date.now(),
  settings: FamilyCostSettings = settingsOf(answer),
): Tile {
  if (!answer || answer.state !== "ready" || !answer.totals) {
    const bad = Boolean(error);
    return {
      id: "family",
      value: bad ? "Unknown" : "Reading...",
      note: bad ? "could not be read" : "",
      health: bad ? "attention" : "idle",
    };
  }
  const { totals } = answer;
  const rows = answer.families ?? [];
  const top = rankFamilies(rows, unit)[0];
  const sum = amountText(unit, totals.cost_usd, totals.tokens_total);
  const over = flaggedTotal(answer, settings);
  const stale = answerIsStale(answer, now);
  return {
    id: "family",
    value: plural(totals.families, "family", "families"),
    summary: `${plural(totals.families, "family", "families")}, ${sum} in ${days} d`,
    brief: over > 0 ? `${plural(over, "runaway")} today` : plural(totals.families, "family", "families"),
    note:
      over > 0
        ? `${plural(over, "runaway")} · ${sum} in ${days} d`
        : `${top ? `top ${amountText(unit, top.cost_usd, top.tokens.total)}` : sum} in ${days} d${stale ? " · refresh failing" : ""}`,
    health: over > 0 || stale ? "attention" : "ok",
  };
}

// ── drawing ─────────────────────────────────────────────────────────────────

interface State {
  context: ExtensionContext;
  days: number;
  answer: FamilyCostAnswer | null;
  error: unknown;
  /** The unit and flag amounts shown now: the saved ones until the viewer changes them. */
  settings: FamilyCostSettings;
  saveError: string | null;
  /** The text in the filter box. */
  filter: string;
  /** The family whose detail is open (one at a time). */
  open: string | null;
  /** Bumped when a settings save starts and when it ends; a poll only adopts the server's settings if it did not move. */
  mutation: number;
  /** Saves still out. */
  inflight: number;
  /** The latest save per settings key; an older answer for the same key is dropped. */
  seq: Partial<Record<keyof FamilyCostSettings, number>>;
}

interface Parts {
  status: HTMLElement;
  needs: HTMLElement;
  filterBox: HTMLElement;
  filter: HTMLInputElement;
  body: HTMLElement;
  toggle: HTMLElement;
  settings: HTMLElement;
  refreshed: HTMLElement;
}

/** The Families panel's body; activateFamilyCost fills it. */
export function familyCostSection(): HTMLElement {
  return el(
    "div",
    { class: "admin-block admin-family" },
    el(
      "div",
      { class: "family-top" },
      el("p", { class: "meta refreshed family-refreshed" }),
      el("div", { class: "family-unit" }),
    ),
    el("div", { class: "family-settings" }),
    el("div", { class: "page-status family-status" }),
    el("div", { class: "family-needs-slot" }),
    el(
      "div",
      { class: "family-filter", hidden: true },
      el("input", {
        type: "text",
        class: "family-filter-input",
        placeholder: "Filter families",
        "aria-label": "Filter families",
        autocomplete: "off",
      }),
    ),
    el("div", { class: "family-body" }),
  );
}

let detailCounter = 0;

export interface RowOptions {
  /** Whether the row's detail is open. */
  open?: boolean;
  /** Whether the row is over a flag amount today. */
  hot?: boolean;
  /** Whether the host pill sits on the line; it does only when the answer has more than one host. */
  showHost?: boolean;
  onToggle?: () => void;
}

/** One family: a line (title, host, share bar, amount) that opens to its roles and counts. */
export function familyRow(
  row: FamilyRow,
  rows: readonly FamilyRow[],
  unit: CostUnit,
  options: RowOptions = {},
): HTMLElement {
  const open = Boolean(options.open);
  const hot = options.hot ?? row.flagged;
  const id = `family-detail-${++detailCounter}`;
  const fill = el("i", { class: hot ? "is-hot" : "" });
  fill.style.width = `${sharePct(row, rows, unit)}%`;
  const line = el(
    "button",
    {
      type: "button",
      class: "family-line",
      "aria-expanded": String(open),
      "aria-controls": id,
    },
    el("span", { class: "family-title", title: row.title }, row.title || row.lead_id.slice(0, 8)),
    options.showHost && row.host ? el("span", { class: "family-host", title: "Host" }, row.host) : null,
    el("span", { class: "family-bar", "aria-hidden": "true" }, fill),
    el(
      "span",
      { class: "family-amount", title: hoverText(unit, row) },
      figureText(unit, row.cost_usd, row.tokens.total),
    ),
  );
  line.addEventListener("click", () => options.onToggle?.());
  const facts: string[] = [];
  if (!options.showHost && row.host) facts.push(`host ${row.host}`);
  if (row.lead_context_tokens) facts.push(`context ${tokensText(row.lead_context_tokens)}`);
  facts.push(plural(row.restarts, "restart"));
  facts.push(plural(row.sessions, "session"));
  facts.push(`today ${amountText(unit, row.today.cost_usd, row.today.tokens_total)}`);
  const detail = el(
    "div",
    { class: "family-detail", id, hidden: !open },
    el(
      "div",
      { class: "family-roles" },
      ...sortedRoles(row.by_role).map((role) =>
        el("span", { class: `family-chip role-${role.role}` }, roleChipText(role, unit)),
      ),
    ),
    el("p", { class: "meta family-facts" }, facts.join(" · ")),
  );
  return el(
    "li",
    { class: `family-row${hot ? " is-hot" : ""}${open ? " is-open" : ""}`, "data-lead": row.lead_id },
    line,
    detail,
  );
}

/** "52 families, biggest: Release Prep 895M"; "3 matches" while a filter is typed. */
export function familiesLine(
  all: readonly FamilyRow[],
  shownCount: number,
  total: number,
  filtering: boolean,
  unit: CostUnit,
): HTMLElement | string {
  if (filtering) return plural(shownCount, "match", "matches");
  const top = rankFamilies(all, unit)[0];
  const count = plural(total, "family", "families");
  if (!top) return count;
  return foldLine(
    `${count}, biggest:`,
    top.title || top.lead_id.slice(0, 8),
    figureText(unit, top.cost_usd, top.tokens.total),
  );
}

function drawBody(parts: Parts, state: State, redraw: () => void): void {
  const answer = state.answer;
  const unit = state.settings.unit;
  // A poll or a click rebuilds the rows; the scroll place and the focused row stay.
  const scroll = parts.body.querySelector<HTMLElement>(".family-box")?.scrollTop ?? 0;
  const active = document.activeElement;
  const focused =
    active instanceof HTMLElement && parts.body.contains(active)
      ? active.closest<HTMLElement>(".family-row")?.dataset.lead
      : undefined;
  const focusedFold =
    active instanceof HTMLElement && parts.body.contains(active)
      ? active.closest<HTMLElement>("[data-fold]")?.dataset.fold
      : undefined;
  const line = needsYou(answer, state.settings);
  replaceChildren(
    parts.needs,
    [
      line ? el("p", { class: "family-needs", role: "note" }, `Needs you: ${line}`) : null,
      liveNeedsLink("family"),
    ],
  );
  if (!answer || answer.state === "warming") {
    parts.filterBox.hidden = true;
    replaceChildren(parts.body, [el("p", { class: "meta" }, "Reading the families...")]);
    return;
  }
  const all = rankFamilies(answer.families ?? [], unit);
  const showHost = new Set(all.map((row) => row.host).filter(Boolean)).size > 1;
  parts.filterBox.hidden = all.length === 0;
  const shown = filterFamilies(all, state.filter);
  const children: (HTMLElement | null)[] = [];
  if (!all.length) {
    children.push(el("p", { class: "meta" }, `No family spent anything in the last ${state.days} days.`));
  } else if (!shown.length) {
    children.push(el("p", { class: "meta" }, `No family matches "${state.filter.trim()}".`));
  } else {
    const total = Math.max(answer.totals?.families ?? 0, all.length);
    const list = el(
      "div",
      {},
      el(
        "div",
        { class: "family-box" },
        el(
          "ol",
          { class: "family-list" },
          ...shown.map((row) =>
            familyRow(row, all, unit, {
              open: state.open === row.lead_id,
              hot: isFlagged(row, state.settings),
              showHost,
              onToggle: () => {
                state.open = state.open === row.lead_id ? null : row.lead_id;
                redraw();
              },
            }),
          ),
        ),
      ),
      shown.length < total
        ? el("p", { class: "meta family-count" }, `Showing ${shown.length} of ${total} families`)
        : null,
    );
    // Typing a filter opens the fold at once without saving that; clearing it returns to what the
    // viewer left (2026-10-30). The filter box sits above the fold, so focus never moves.
    const filtering = state.filter.trim() !== "";
    children.push(
      fold("families", "family-fold", [familiesLine(all, shown.length, total, filtering, unit)], list, {
        forceOpen: filtering,
      }),
    );
  }
  replaceChildren(parts.body, children);
  const box = parts.body.querySelector<HTMLElement>(".family-box");
  if (box) box.scrollTop = scroll;
  if (focused) {
    parts.body
      .querySelector<HTMLElement>(`.family-row[data-lead="${CSS.escape(focused)}"] .family-line`)
      ?.focus();
  } else if (focusedFold) {
    parts.body.querySelector<HTMLElement>(`[data-fold="${CSS.escape(focusedFold)}"]`)?.focus();
  }
}

interface SaveHooks {
  /** Runs once this save is settled and the settings are updated, before the card redraws. */
  settled?: (error: unknown | null) => void;
}

/**
 * Save part of the settings. The patch shows at once; when the server answers, only the keys of
 * this save take its values (a poll, or the other field's save, may have moved the rest), and a
 * failure puts back only these keys. An older answer for a key that a newer save has taken over
 * is dropped. (2026-10-30: a late poll or a second save used to overwrite the whole object.)
 */
function persist(
  state: State,
  redraw: () => void,
  patch: Partial<FamilyCostSettings>,
  body: Record<string, unknown>,
  hooks: SaveHooks = {},
): Promise<void> {
  const keys = Object.keys(patch) as (keyof FamilyCostSettings)[];
  const before: Partial<FamilyCostSettings> = {};
  const mine: Partial<Record<keyof FamilyCostSettings, number>> = {};
  for (const key of keys) {
    (before as Record<string, unknown>)[key] = state.settings[key];
    mine[key] = (state.seq[key] ?? 0) + 1;
    state.seq[key] = mine[key];
  }
  const current = (key: keyof FamilyCostSettings) => state.seq[key] === mine[key];
  state.settings = { ...state.settings, ...patch };
  state.saveError = null;
  state.mutation++;
  state.inflight++;
  redraw();
  return saveFamilyCostSettings(state.context, body).then(
    (result) => {
      state.inflight--;
      state.mutation++;
      for (const key of keys) {
        if (current(key)) state.settings = { ...state.settings, [key]: result[key] };
      }
      hooks.settled?.(null);
      redraw();
    },
    (error: unknown) => {
      state.inflight--;
      state.mutation++;
      for (const key of keys) {
        if (current(key)) state.settings = { ...state.settings, [key]: before[key] };
      }
      hooks.settled?.(error ?? new Error("Request failed"));
      redraw();
    },
  );
}

function drawUnit(parts: Parts, state: State, redraw: () => void): void {
  const unit = state.settings.unit;
  const units = el(
    "div",
    { class: "family-toggle", role: "group", "aria-label": "Show cost as" },
    ...(["tokens", "dollars"] as const).map((choice) => {
      const on = unit === choice;
      const seg = el(
        "button",
        { type: "button", class: `family-seg${on ? " is-on" : ""}`, "aria-pressed": String(on) },
        choice === "tokens" ? "Tokens" : "Dollars",
      );
      seg.addEventListener("click", () => {
        if (state.settings.unit === choice) return;
        void persist(state, redraw, { unit: choice }, { family_cost_unit: choice }, {
          settled: (error) => {
            if (error) state.saveError = describeError(error);
          },
        });
      });
      return seg;
    }),
  );
  replaceChildren(parts.toggle, [units, hint(UNIT_HINT, "Show cost as")]);
}

// ── the two flag amounts ────────────────────────────────────────────────────

interface Field {
  node: HTMLElement;
  /** Show the saved amount, unless the viewer is typing in it or it holds an unsaved one. */
  sync: () => void;
}

const FLAG_FIELDS = {
  tokens: {
    prefix: "",
    suffix: "tokens",
    label: "Flag a family over this many tokens a day",
    read: readTokenAmount,
    show: shortTokens,
    current: (s: FamilyCostSettings) => s.runaway_tokens,
    settingsKey: "runaway_tokens",
    postKey: "runaway_family_tokens",
    inputmode: "decimal",
  },
  usd: {
    prefix: "$",
    suffix: "",
    label: "Flag a family over this many dollars a day",
    read: readDollarAmount,
    show: shortDollars,
    current: (s: FamilyCostSettings) => s.runaway_usd,
    settingsKey: "runaway_usd",
    postKey: "runaway_family_usd",
    inputmode: "decimal",
  },
} as const;

/** Timers for the Saved ticks, cleared when the card goes. */
let tickTimers: ReturnType<typeof setTimeout>[] = [];

function thresholdField(state: State, kind: keyof typeof FLAG_FIELDS, redraw: () => void): Field {
  const cfg = FLAG_FIELDS[kind];
  const input = el("input", {
    type: "text",
    class: `family-runaway family-runaway-${kind}`,
    inputmode: cfg.inputmode,
    autocomplete: "off",
    spellcheck: "false",
    "aria-label": cfg.label,
    value: cfg.show(cfg.current(state.settings)),
  });
  const note = el("span", { class: "family-note", "aria-live": "polite" });
  const box = el(
    "span",
    { class: "family-input" },
    cfg.prefix ? el("span", { class: "family-affix" }, cfg.prefix) : null,
    input,
    cfg.suffix ? el("span", { class: "family-affix" }, cfg.suffix) : null,
  );
  const node = el("span", { class: `family-limit family-limit-${kind}` }, box, note);
  let pending = false;
  let invalid = false;
  /** The text a pending save carries, so Enter's save and the change event after it are one save. */
  let sentText = "";
  /** Bumped by every keystroke; an answer never touches text typed after its request went out. */
  let revision = 0;
  /** The revision at which the input last showed a saved amount; a newer one is the viewer's own text. */
  let cleanRevision = 0;
  let tick: ReturnType<typeof setTimeout> | null = null;
  const show = (text: string) => {
    input.value = text;
    cleanRevision = revision;
  };
  const clearNote = () => {
    if (tick) clearTimeout(tick);
    tick = null;
    note.replaceChildren();
    input.removeAttribute("aria-invalid");
    invalid = false;
  };
  const fail = (message: string) => {
    clearNote();
    invalid = true;
    input.setAttribute("aria-invalid", "true");
    note.appendChild(el("span", { class: "family-field-error", role: "alert" }, message));
  };
  const saved = () => {
    clearNote();
    note.appendChild(el("span", { class: "family-saved" }, checkIcon(), "Saved"));
    tick = setTimeout(clearNote, SAVED_MS);
    tickTimers.push(tick);
  };
  const commit = () => {
    if (pending && input.value === sentText) return;
    const read = cfg.read(input.value);
    if (!read.ok) {
      fail(read.error);
      return;
    }
    const amount = read.value;
    // The same amount again is still a save the viewer asked for: it says Saved, it does not go quiet.
    if (amount === cfg.current(state.settings)) {
      show(cfg.show(amount));
      saved();
      return;
    }
    const sent = revision;
    sentText = input.value;
    pending = true;
    clearNote();
    void persist(
      state,
      redraw,
      { [cfg.settingsKey]: amount },
      { [cfg.postKey]: amount },
      {
        settled: (error) => {
          pending = false;
          // Text typed since the request went out is the viewer's; the answer leaves it alone.
          if (revision !== sent) return;
          if (error) {
            // The typed text stays so it can be fixed.
            fail(describeError(error));
          } else {
            show(cfg.show(cfg.current(state.settings)));
            saved();
          }
        },
      },
    );
  };
  input.addEventListener("input", () => {
    revision++;
    if (invalid) clearNote();
  });
  input.addEventListener("change", commit);
  input.addEventListener("keydown", (event) => {
    if ((event as KeyboardEvent).key === "Enter") commit();
  });
  return {
    node,
    sync: () => {
      if (pending || invalid || document.activeElement === input || revision !== cleanRevision) return;
      input.value = cfg.show(cfg.current(state.settings));
    },
  };
}

function buildSettings(parts: Parts, state: State, redraw: () => void): Field[] {
  const fields = [thresholdField(state, "tokens", redraw), thresholdField(state, "usd", redraw)];
  // Line 1 is the label; line 2 is the two boxes with "or" between and "a day" and the hint after,
  // which wrap together when the card is narrow.
  replaceChildren(parts.settings, [
    el("span", { class: "family-label" }, "Flag a family over"),
    el(
      "div",
      { class: "family-limits" },
      fields[0]!.node,
      el("span", { class: "family-label" }, "or"),
      fields[1]!.node,
      el("span", { class: "family-label" }, "a day"),
      hint(RUNAWAY_HINT, "the flag amounts"),
    ),
  ]);
  return fields;
}

let timer: ReturnType<typeof setTimeout> | null = null;
let onFocus: (() => void) | null = null;
let unsubscribe: (() => void)[] = [];
/** Bumped by every activate and deactivate, as in presets.ts. */
let generation = 0;

function stop(): void {
  if (timer) clearTimeout(timer);
  timer = null;
  for (const t of tickTimers) clearTimeout(t);
  tickTimers = [];
  for (const off of unsubscribe) off();
  unsubscribe = [];
  if (!onFocus) return;
  window.removeEventListener("focus", onFocus);
  document.removeEventListener("visibilitychange", onFocus);
  onFocus = null;
}

/** Read and draw the Families panel inside *root* (from familyCostSection), again on focus and every minute. */
export function activateFamilyCost(context: ExtensionContext, root: HTMLElement): void {
  const find = <T extends HTMLElement>(selector: string): T => {
    const found = root.querySelector<T>(selector);
    if (!found) throw new Error(`Spend card is missing ${selector}`);
    return found;
  };
  const filterBox = find(".family-filter");
  const parts: Parts = {
    status: find(".family-status"),
    needs: find(".family-needs-slot"),
    filterBox,
    filter: find<HTMLInputElement>(".family-filter-input"),
    body: find(".family-body"),
    toggle: find(".family-unit"),
    settings: find(".family-settings"),
    refreshed: root.querySelector<HTMLElement>(".family-refreshed") ?? el("p"),
  };
  const mine = ++generation;
  const live = () => generation === mine;
  stop();
  const state: State = {
    context,
    days: syncDays(),
    answer: null,
    error: null,
    settings: { ...DEFAULT_SETTINGS },
    saveError: null,
    filter: parts.filter.value,
    open: null,
    mutation: 0,
    inflight: 0,
    seq: {},
  };
  let said = "";
  const paintStatus = () => {
    parts.refreshed.textContent = updatedText(state.answer);
    const tile = familyTile(state.answer, state.error, state.settings.unit, state.days, Date.now(), state.settings);
    setTile(
      state.answer?.state === "ready" ? staleTile(tile, state.error) : tile,
      answerTime(state.answer) || undefined,
    );
    setFlaggedCount(flaggedTotal(state.answer, state.settings));
    setNotes("families", familyNotes(state.answer));
  };
  // The flag fields keep what is typed through a poll; only the rows and the toggle are rebuilt.
  let fields: Field[] = [];
  const draw = () => {
    const message = state.error ? describeError(state.error) : (state.saveError ?? "");
    if (message !== said) {
      said = message;
      replaceChildren(parts.status, [
        message
          ? notice(
              state.error
                ? `Family cost could not be read (${message}).`
                : `The setting was not saved (${message}).`,
              "error",
            )
          : null,
      ]);
    }
    drawUnit(parts, state, draw);
    for (const field of fields) field.sync();
    drawBody(parts, state, draw);
    paintStatus();
  };
  fields = buildSettings(parts, state, draw);
  parts.filter.addEventListener("input", () => {
    state.filter = parts.filter.value;
    drawBody(parts, state, draw);
  });
  const schedule = () => {
    if (timer) clearTimeout(timer);
    timer = setTimeout(
      () => {
        // A hidden tab does not poll; visibilitychange reads on return.
        if (document.visibilityState === "visible") void refresh();
        else schedule();
      },
      state.answer?.state === "warming" ? WARMING_POLL_MS : POLL_MS,
    );
  };
  let reading: Promise<void> | null = null;
  const refresh = (): Promise<void> => {
    if (reading) return reading;
    const asked = state.days;
    // A save that starts, ends or is out while this reads makes the server's settings in this
    // answer older than ours; only the rows and totals are taken then.
    const startedAt = state.mutation;
    const startedOut = state.inflight;
    reading = fetchFamilyCost(context, asked)
      .then(
        (answer) => {
          // A toggle pressed while this was out makes the answer the wrong window.
          if (!live() || asked !== state.days) return;
          state.answer = answer;
          state.error = null;
          if (state.mutation === startedAt && startedOut === 0 && state.inflight === 0) {
            state.settings = settingsOf(answer);
          }
        },
        (error: unknown) => {
          if (!live() || asked !== state.days) return;
          state.error = error ?? new Error("Request failed");
        },
      )
      .then(() => {
        if (!live()) return;
        draw();
        schedule();
      })
      .finally(() => {
        reading = null;
        if (live() && asked !== state.days) void refresh();
      });
    return reading;
  };
  // The card's one window toggle (presets.ts) changes both panels.
  unsubscribe = [
    onSpend("days", () => {
      if (!live() || spendDays() === state.days) return;
      state.days = spendDays();
      state.answer = null;
      state.error = null;
      state.open = null;
      draw();
      void refresh();
    }),
  ];
  draw();
  void refresh();
  registerRefresh("family", () => (live() ? refresh() : undefined));
  onFocus = () => {
    if (document.visibilityState === "visible") void refresh();
  };
  window.addEventListener("focus", onFocus);
  document.addEventListener("visibilitychange", onFocus);
}

export function deactivateFamilyCost(): void {
  generation++;
  stop();
}
