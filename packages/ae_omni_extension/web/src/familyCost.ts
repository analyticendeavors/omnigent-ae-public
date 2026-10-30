// The Admin Center's Family cost card (2026-10-29): this week's (or month's) spend per session
// family, a lead plus every session it started and their native subagents, so a runaway family
// shows at a glance.
//
// Why: spend is easy to see per account and per preset but not per piece of work, and one lead
// that keeps restarting and spawning reviewers can cost more than a day of everything else. The
// server (`GET /v1/ae/family-cost`, family_cost.py) sums each family from the sessions' own
// usage every 15 minutes and answers from that, so the first read after a restart says
// "Reading" and the next poll has the numbers. An admin sees every family on every host (each
// row has a host chip); anyone else sees only their own.
//
// The number shown first is the viewer's own setting, "Show cost as" (tokens or dollars), with
// the other on hover; "Runaway family" is the spend today that raises the Needs you line. Both
// are saved per user on the server (`POST /v1/ae/family-cost/settings`). Dollars are an
// estimate at API rates; on a subscription they show where usage goes, not the invoice.

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
import { el, notice, replaceChildren } from "./dom";
import { hint } from "./hint";
import { registerRefresh, setTile, staleTile, type Tile } from "./tiles";

export const POLL_MS = 60_000;
/** While the server has no sweep yet, so the numbers show in seconds. */
export const WARMING_POLL_MS = 4_000;
export const DAYS_KEY = "familycost.days";
export const DEFAULT_DAYS = 7;
export const WINDOWS = [7, 30] as const;
/** The server sweeps every 15 minutes; an answer much older means the sweep is failing. */
export const STALE_MS = 45 * 60_000;
/** One-line helper texts (140 characters at most; familyCost.test.ts holds them to it). */
export const UNIT_HINT =
  "Which number the family cost card shows first; the other appears on hover.";
export const RUNAWAY_HINT =
  "Flags a family whose spend today passes this amount. Counts the lead, its children, subagents and Codex reviewers.";
export const COST_NOTE =
  "Dollars are estimated at API rates. On a subscription they show where usage goes, not what the invoice says.";
export const TODAY_NOTE = "Today means Pacific time (the day the Runaway family amount is checked against).";
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

/** What a row's number says on hover: the other unit, then the breakdown. */
export function hoverText(unit: CostUnit, row: FamilyRow): string {
  const other = amountText(unit === "dollars" ? "tokens" : "dollars", row.cost_usd, row.tokens.total);
  return (
    `${other}. Input ${tokensText(row.tokens.input)} (with cache writes), ` +
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

/** The threshold for *unit* from the viewer's settings. */
export function runawayLimit(settings: FamilyCostSettings, unit: CostUnit): number {
  return unit === "dollars" ? settings.runaway_usd : settings.runaway_tokens;
}

/** "$25" or "5M tokens": the threshold as the Needs you line says it. */
export function limitText(settings: FamilyCostSettings, unit: CostUnit): string {
  const limit = runawayLimit(settings, unit);
  return unit === "dollars" ? money(limit) : `${tokensText(limit)} tokens`;
}

/** The Needs you line, or null when no family passed the threshold today (or it is off). */
export function needsYou(
  answer: FamilyCostAnswer | null,
  settings: FamilyCostSettings,
  unit: CostUnit,
): string | null {
  if (!answer || answer.state !== "ready") return null;
  const limit = runawayLimit(settings, unit);
  if (limit <= 0) return null;
  const over = (answer.families ?? []).filter((row) =>
    (unit === "dollars" ? row.today.cost_usd : row.today.tokens_total) >= limit,
  );
  if (!over.length) return null;
  const names = over
    .slice(0, 3)
    .map((row) => row.title || row.lead_id.slice(0, 8))
    .join(", ");
  const more = over.length > 3 ? ` and ${over.length - 3} more` : "";
  return `${plural(over.length, "family", "families")} passed ${limitText(settings, unit)} today: ${names}${more}.`;
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

export function familyTile(
  answer: FamilyCostAnswer | null,
  error: unknown,
  unit: CostUnit,
  days = DEFAULT_DAYS,
  now = Date.now(),
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
  const flagged = totals.flagged > 0;
  const stale = answerIsStale(answer, now);
  return {
    id: "family",
    value: plural(totals.families, "family", "families"),
    summary: `${plural(totals.families, "family", "families")}, ${sum} in ${days} d`,
    note: flagged
      ? `${plural(totals.flagged, "runaway")} · ${sum} in ${days} d`
      : `${top ? `top ${amountText(unit, top.cost_usd, top.tokens.total)}` : sum} in ${days} d${stale ? " · refresh failing" : ""}`,
    health: flagged || stale ? "attention" : "ok",
  };
}

// ── persistence of the window ───────────────────────────────────────────────

export function loadDays(): number {
  try {
    const days = Number(window.localStorage.getItem(DAYS_KEY));
    if ((WINDOWS as readonly number[]).includes(days)) return days;
  } catch {
    // No storage in this frame: the default.
  }
  return DEFAULT_DAYS;
}

function saveDays(days: number): void {
  try {
    window.localStorage.setItem(DAYS_KEY, String(days));
  } catch {
    // Kept in memory until the page closes.
  }
}

// ── drawing ─────────────────────────────────────────────────────────────────

interface State {
  context: ExtensionContext;
  days: number;
  answer: FamilyCostAnswer | null;
  error: unknown;
  /** The unit and runaway values shown now: the saved ones until the viewer changes them. */
  settings: FamilyCostSettings;
  saveError: string | null;
}

interface Parts {
  status: HTMLElement;
  body: HTMLElement;
  toggle: HTMLElement;
  settings: HTMLElement;
  refreshed: HTMLElement;
}

/** The card body; activateFamilyCost fills it. */
export function familyCostSection(): HTMLElement {
  return el(
    "div",
    { class: "admin-block admin-family" },
    el(
      "div",
      { class: "family-top" },
      el("p", { class: "meta refreshed family-refreshed" }),
      el("div", { class: "family-toggle", role: "group", "aria-label": "Window" }),
    ),
    el("div", { class: "family-settings" }),
    el("div", { class: "page-status family-status" }),
    el("div", { class: "family-body" }),
  );
}

export function familyRow(row: FamilyRow, rows: readonly FamilyRow[], unit: CostUnit): HTMLElement {
  const main = el(
    "span",
    { class: "family-amount", title: hoverText(unit, row) },
    amountText(unit, row.cost_usd, row.tokens.total),
  );
  const fill = el("i", { class: row.flagged ? "is-hot" : "" });
  fill.style.width = `${sharePct(row, rows, unit)}%`;
  const facts: string[] = [];
  if (row.lead_context_tokens) facts.push(`context ${tokensText(row.lead_context_tokens)}`);
  facts.push(plural(row.restarts, "restart"));
  facts.push(plural(row.sessions, "session"));
  return el(
    "li",
    { class: `family-row${row.flagged ? " is-hot" : ""}`, "data-lead": row.lead_id },
    el(
      "div",
      { class: "family-line" },
      el("span", { class: "family-title", title: row.title }, row.title || row.lead_id.slice(0, 8)),
      row.host ? el("span", { class: "family-host", title: "Host" }, row.host) : null,
      main,
    ),
    el("span", { class: "family-bar", role: "img", "aria-label": `${sharePct(row, rows, unit)}% of the biggest family` }, fill),
    el(
      "div",
      { class: "family-roles" },
      ...sortedRoles(row.by_role).map((role) =>
        el("span", { class: `family-chip role-${role.role}` }, roleChipText(role, unit)),
      ),
    ),
    el("p", { class: "meta family-facts" }, facts.join(" · ")),
  );
}

function drawBody(parts: Parts, state: State): void {
  const answer = state.answer;
  const unit = state.settings.unit;
  if (!answer || answer.state === "warming") {
    replaceChildren(parts.body, [el("p", { class: "meta" }, "Reading the families...")]);
    return;
  }
  const rows = rankFamilies(answer.families ?? [], unit);
  const children: (HTMLElement | null)[] = [];
  const line = needsYou(answer, state.settings, unit);
  if (line) children.push(el("p", { class: "family-needs", role: "note" }, `Needs you: ${line}`));
  if (!rows.length) {
    children.push(el("p", { class: "meta" }, `No family spent anything in the last ${state.days} days.`));
  } else {
    children.push(
      el("ol", { class: "family-list" }, ...rows.map((row) => familyRow(row, rows, unit))),
    );
  }
  if (answer.truncated) {
    children.push(el("p", { class: "meta family-foot" }, "The list is cut to the biggest families."));
  }
  if (answer.partial && answer.history_since) {
    children.push(
      el(
        "p",
        { class: "meta family-foot" },
        `History starts ${answer.history_since}; older chats count from then.`,
      ),
    );
  }
  children.push(el("p", { class: "meta family-foot" }, TODAY_NOTE));
  children.push(el("p", { class: "meta family-foot" }, COST_NOTE));
  replaceChildren(parts.body, children);
}

function drawToggle(parts: Parts, state: State, redraw: () => void, reload: () => void): void {
  replaceChildren(
    parts.toggle,
    WINDOWS.map((days) => {
      const on = state.days === days;
      const choice = el(
        "button",
        { type: "button", class: `family-seg${on ? " is-on" : ""}`, "aria-pressed": String(on) },
        `${days} d`,
      );
      choice.addEventListener("click", () => {
        if (state.days === days) return;
        state.days = days;
        saveDays(days);
        state.answer = null;
        state.error = null;
        redraw();
        reload();
      });
      return choice;
    }),
  );
}

/** Tokens are typed in millions, dollars in dollars. */
export function toInputValue(settings: FamilyCostSettings, unit: CostUnit): string {
  const limit = runawayLimit(settings, unit);
  return String(unit === "dollars" ? limit : limit / 1e6);
}

export function fromInputValue(raw: string, unit: CostUnit): number | null {
  const value = Number(raw);
  if (raw.trim() === "" || !Number.isFinite(value) || value < 0) return null;
  return unit === "dollars" ? Math.round(value * 100) / 100 : Math.round(value * 1e6);
}

function drawSettings(parts: Parts, state: State, redraw: () => void): void {
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
        const before = state.settings;
        state.settings = { ...before, unit: choice };
        state.saveError = null;
        redraw();
        saveFamilyCostSettings(state.context, { family_cost_unit: choice }).then(
          (saved) => {
            state.settings = saved;
            redraw();
          },
          (error: unknown) => {
            state.settings = before;
            state.saveError = describeError(error);
            redraw();
          },
        );
      });
      return seg;
    }),
  );
  const input = el("input", {
    type: "number",
    class: "family-runaway",
    min: "0",
    step: unit === "dollars" ? "1" : "0.5",
    value: toInputValue(state.settings, unit),
    "aria-label": `Runaway family, in ${unit === "dollars" ? "dollars" : "millions of tokens"} per day`,
  });
  const commit = () => {
    const amount = fromInputValue(input.value, unit);
    if (amount === null || amount === runawayLimit(state.settings, unit)) {
      input.value = toInputValue(state.settings, unit);
      return;
    }
    const before = state.settings;
    const key = unit === "dollars" ? "runaway_usd" : "runaway_tokens";
    state.settings = { ...before, [key]: amount };
    state.saveError = null;
    redraw();
    saveFamilyCostSettings(state.context, {
      [unit === "dollars" ? "runaway_family_usd" : "runaway_family_tokens"]: amount,
    }).then(
      (saved) => {
        state.settings = saved;
        redraw();
      },
      (error: unknown) => {
        state.settings = before;
        state.saveError = describeError(error);
        redraw();
      },
    );
  };
  input.addEventListener("change", commit);
  input.addEventListener("keydown", (event) => {
    if ((event as KeyboardEvent).key === "Enter") commit();
  });
  replaceChildren(parts.settings, [
    el("span", { class: "family-label" }, "Show cost as"),
    hint(UNIT_HINT, "Show cost as"),
    units,
    el("span", { class: "family-label" }, "Runaway family"),
    hint(RUNAWAY_HINT, "Runaway family"),
    input,
    el("span", { class: "meta" }, unit === "dollars" ? "$ a day" : "M tokens a day"),
  ]);
}

let timer: ReturnType<typeof setTimeout> | null = null;
let onFocus: (() => void) | null = null;
/** Bumped by every activate and deactivate, as in presets.ts. */
let generation = 0;

function stop(): void {
  if (timer) clearTimeout(timer);
  timer = null;
  if (!onFocus) return;
  window.removeEventListener("focus", onFocus);
  document.removeEventListener("visibilitychange", onFocus);
  onFocus = null;
}

/** Read and draw the section *root* (from familyCostSection), again on focus and every minute. */
export function activateFamilyCost(context: ExtensionContext, root: HTMLElement): void {
  const find = <T extends HTMLElement>(selector: string): T => {
    const found = root.querySelector<T>(selector);
    if (!found) throw new Error(`Family cost card is missing ${selector}`);
    return found;
  };
  const parts: Parts = {
    status: find(".family-status"),
    body: find(".family-body"),
    toggle: find(".family-toggle"),
    settings: find(".family-settings"),
    refreshed: root.querySelector<HTMLElement>(".family-refreshed") ?? el("p"),
  };
  const mine = ++generation;
  const live = () => generation === mine;
  stop();
  const state: State = {
    context,
    days: loadDays(),
    answer: null,
    error: null,
    settings: { ...DEFAULT_SETTINGS },
    saveError: null,
  };
  let said = "";
  const paintStatus = () => {
    parts.refreshed.textContent = updatedText(state.answer);
    const tile = familyTile(state.answer, state.error, state.settings.unit, state.days);
    setTile(
      state.answer?.state === "ready" ? staleTile(tile, state.error) : tile,
      answerTime(state.answer) || undefined,
    );
  };
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
    drawToggle(parts, state, draw, () => void refresh());
    // A number being typed is not replaced under the cursor by a poll.
    if (!parts.settings.contains(document.activeElement)) drawSettings(parts, state, draw);
    drawBody(parts, state);
    paintStatus();
  };
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
    reading = fetchFamilyCost(context, asked)
      .then(
        (answer) => {
          // A toggle pressed while this was out makes the answer the wrong window.
          if (!live() || asked !== state.days) return;
          state.answer = answer;
          state.error = null;
          state.settings = settingsOf(answer);
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
