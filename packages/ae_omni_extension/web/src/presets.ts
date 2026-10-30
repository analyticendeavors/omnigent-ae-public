// The Admin Center's Presets card (2026-10-24, preset insights PR 2): how each
// role preset has been doing over the last 7 or 30 days, from the run log the
// server writes when a lead's run ends (preset_runs.py). Takeaways first (fixed
// rules on the server, never a model; the card prints their text), then four
// numbers and one row per preset. A row opens a pop-up with that preset's
// recent runs; a run's title opens its session.
//
// Why: the owner picks presets by feel and had no way to see that one costs three
// times another without finishing faster. The server (`GET /v1/ae/presets`,
// preset_stats.py, admins only) answers from a cache and computes in the
// background, so the first read after a restart says "Reading" and the next
// poll has the numbers. With the run log off the answer is "fallback": session
// totals only, no takeaways, and the card says so in one line.
//
// The 7 d / 30 d choice is remembered per viewer in localStorage (30 d first).
// Cost is an estimate at API rates; on a subscription it shows where usage
// goes, not what the invoice says.

import type { ExtensionContext } from "@omnigent/extension-sdk";
import {
  AeRequestError,
  applySuggestion,
  describeError,
  dismissSuggestion,
  fetchNewerModels,
  fetchPresets,
  fetchPresetSuggestions,
  openSuggestionIssue,
  type NewerModel,
  type NewerModelsAnswer,
  type PresetReview,
  type PresetRow,
  type PresetRun,
  type PresetTakeaway,
  type PresetsAnswer,
  type PresetSuggestion,
  type PresetSuggestionsAnswer,
  undoSuggestion,
} from "./api";
import { sparkline, money } from "./azure";
import { button, type Child, el, notice, replaceChildren } from "./dom";
import { closeIcon } from "./icons";
import { registerRefresh, setTile, staleTile, type Tile } from "./tiles";

export const POLL_MS = 60_000;
/** While the server computes its first answer, so the numbers show in seconds. */
export const WARMING_POLL_MS = 4_000;
export const DAYS_KEY = "presets.days";
export const DEFAULT_DAYS = 30;
export const WINDOWS = [7, 30] as const;
export const FALLBACK_NOTE =
  "Detailed run logging is off, so this uses session totals only.";
/** A server answer older than this means its refresh is failing (it recomputes every 5 min). */
export const STALE_MS = 15 * 60_000;
export const COST_NOTE =
  "Cost is estimated at API rates. On a subscription it shows where usage goes, not what the invoice says.";

export const TOKENS_NOTE =
  "Tokens are input and output tokens, summed over the lead and its helpers; cache reads are not counted.";

export const NEWER_KEY = "presets.newer.dismissed";
export const NEWER_HINT =
  "A newer model in the same family is in the catalog. Presets are never changed for you; open Settings > Roles to switch.";

/** Model ids whose notice this viewer dismissed. Storage may be blocked; the notice still shows. */
export function loadDismissed(): string[] {
  try {
    const parsed: unknown = JSON.parse(window.localStorage.getItem(NEWER_KEY) ?? "[]");
    return Array.isArray(parsed) ? parsed.filter((v): v is string => typeof v === "string") : [];
  } catch {
    return [];
  }
}

function saveDismissed(ids: string[]): void {
  try {
    window.localStorage.setItem(NEWER_KEY, JSON.stringify(ids.slice(-20)));
  } catch {
    // Not remembered; the notice returns on the next load.
  }
}

/** Every newer model the viewer has not dismissed. */
export function pendingNewer(
  data: NewerModelsAnswer | null,
  dismissed: readonly string[],
): NewerModel[] {
  return (data?.models ?? []).filter((m) => !dismissed.includes(m.model));
}

/** "New model: Opus 5.6. 2 of your presets use Opus 5.5." for one or several models. */
export function newerText(models: readonly NewerModel[], data?: NewerModelsAnswer | null): string {
  const names = models.map((m) => m.name).join(", ");
  const from = [...new Set(models.flatMap((m) => m.from))];
  const ids = new Set(models.map((m) => m.model));
  const rows = data?.presets?.filter((p) => p.rows.some((r) => ids.has(r.newer)));
  const count = rows ? rows.length : Math.max(...models.map((m) => m.presets));
  const verb = count === 1 ? "uses" : "use";
  const label = models.length === 1 ? "New model" : "New models";
  return `${label}: ${names}. ${count} of your presets ${verb} ${from.join(" or ")}.`;
}

/** The slim notice at the top of the card; the Settings pointer is text, not a link. */
function newerNotice(state: State): HTMLElement | null {
  const models = pendingNewer(state.newer, loadDismissed());
  if (!models.length) return null;
  const names = models.map((m) => m.name).join(", ");
  const dismiss = el("button", {
    type: "button",
    class: "presets-nudge-x",
    "aria-label": `Dismiss the notice for ${names}`,
    title: "Dismiss until the next new model",
  });
  dismiss.appendChild(closeIcon());
  dismiss.addEventListener("click", () => {
    saveDismissed([...loadDismissed(), ...models.map((m) => m.model)]);
    paintNotice(state);
  });
  return el(
    "div",
    { class: "presets-nudge", role: "note", title: NEWER_HINT },
    el(
      "span",
      { class: "presets-nudge-text" },
      `${newerText(models, state.newer)} Review in Settings > Roles.`,
    ),
    dismiss,
  );
}

/** Put the notice at the top of the card body, or take it away; the rest is left alone. */
function paintNotice(state: State): void {
  const body = state.body;
  if (!body || state.adminOnly) return;
  body.querySelector(".presets-nudge")?.remove();
  const node = newerNotice(state);
  if (node) body.prepend(node);
}

/** What the card shows for this viewer: the server sends their own setting. */
export type Show = "cost" | "tokens" | "both";

export function showOf(view: PresetsAnswer | null | undefined): Show {
  const show = view?.show;
  return show === "cost" || show === "tokens" ? show : "both";
}

// ── words and numbers ───────────────────────────────────────────────────────

/** "52 m", "2 h 10 m", "<1 m"; a dash for nothing. */
export function duration(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined) return "-";
  const minutes = Math.round(seconds / 60);
  if (minutes < 1) return "<1 m";
  if (minutes < 60) return `${minutes} m`;
  return `${Math.floor(minutes / 60)} h ${String(minutes % 60).padStart(2, "0")} m`;
}

/** "38 min" for the big number. */
export function minutesText(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined) return "-";
  const minutes = Math.round(seconds / 60);
  return minutes < 1
    ? "<1 min"
    : minutes < 120
      ? `${minutes} min`
      : duration(seconds);
}

export function costText(value: number | null | undefined): string {
  return value === null || value === undefined ? "-" : money(value);
}

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

/** "up 12%", "down 8%" or "flat"; null when there is no prior window to compare. */
export function tokensChange(pct: number | null | undefined): string | null {
  if (pct === null || pct === undefined) return null;
  if (pct === 0) return "flat";
  return `${pct > 0 ? "up" : "down"} ${Math.abs(pct)}%`;
}

/** "up 12% vs prior week" (7 d) / "vs prior 30 days"; "no prior period" without one. */
export function tokensDelta(
  pct: number | null | undefined,
  days: number,
): string {
  const change = tokensChange(pct);
  if (change === null) return "no prior period";
  return `${change} vs prior ${days === 7 ? "week" : `${days} days`}`;
}

/** "Oct 23 14:02" in the viewer's zone. */
export function startedText(iso: string | null): string {
  if (!iso) return "-";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return "-";
  const day = date.toLocaleDateString("en-US", {
    month: "short",
    day: "numeric",
  });
  const time = date.toLocaleTimeString("en-US", {
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  });
  return `${day} ${time}`;
}

const plural = (n: number, one: string, many = `${one}s`) =>
  `${n} ${n === 1 ? one : many}`;

/** "+18% vs prior", "-4% vs prior", or nothing to compare. */
export function costDelta(pct: number | null | undefined): string {
  if (pct === null || pct === undefined) return "no prior period";
  return `${pct > 0 ? "+" : ""}${pct}% vs prior`;
}

/** "-6 min" / "+2 min" against the prior window, or nothing to compare. */
export function timeDelta(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined) return "no prior period";
  const minutes = Math.round(seconds / 60);
  return `${minutes > 0 ? "+" : ""}${minutes} min vs prior`;
}

/** Which colour a model's slice of the bar takes: its family, else grey. */
export function modelTone(model: string): string {
  const name = model.toLowerCase();
  for (const family of ["opus", "sonnet", "haiku", "fable"])
    if (name.includes(family)) return family;
  if (name.includes("gpt") || name.includes("codex") || name.includes("o3"))
    return "codex";
  return "other";
}

/** Whether the answer's own time is old enough to say its refresh is failing. */
export function answerIsStale(
  view: PresetsAnswer | null | undefined,
  now = Date.now(),
): boolean {
  const asOf = answerTime(view);
  return asOf > 0 && now - asOf > STALE_MS;
}

/** The server's `asOf` as epoch ms; 0 when there is none (never the client clock). */
export function answerTime(view: PresetsAnswer | null | undefined): number {
  const parsed = view?.asOf ? Date.parse(view.asOf) : NaN;
  return Number.isNaN(parsed) ? 0 : parsed;
}

/** "Updated 2:05 PM", plus ", refresh failing" when old; empty with no `asOf`. */
export function updatedText(
  view: PresetsAnswer | null | undefined,
  now = Date.now(),
): string {
  const asOf = answerTime(view);
  if (!asOf) return "";
  const time = new Date(asOf).toLocaleTimeString([], {
    hour: "numeric",
    minute: "2-digit",
  });
  return `Updated ${time}${answerIsStale(view, now) ? ", refresh failing" : ""}`;
}

export function presetsTile(
  view: PresetsAnswer | null,
  error: unknown,
  days = DEFAULT_DAYS,
  now = Date.now(),
): Tile {
  if (!view || view.state !== "ready" || !view.kpis) {
    const forbidden = error instanceof AeRequestError && error.status === 403;
    const bad = error || view?.state === "error";
    return {
      id: "presets",
      value: forbidden ? "Admins" : bad ? "Unknown" : "Reading...",
      note: forbidden ? "admins only" : bad ? "could not be read" : "",
      health: bad && !forbidden ? "attention" : "idle",
    };
  }
  const { runs, cost } = view.kpis;
  const stale = answerIsStale(view, now);
  return {
    id: "presets",
    value: plural(runs, "run"),
    summary: `${plural(runs, "run")}, ${money(cost)} in ${days} d`,
    note: `${money(cost)} in ${days} days${stale ? " \u00b7 refresh failing" : ""}`,
    health: stale ? "attention" : "ok",
  };
}

// ── drawing ─────────────────────────────────────────────────────────────────

interface State {
  context: ExtensionContext;
  days: number;
  answer: PresetsAnswer | null;
  error: unknown;
  adminOnly: boolean;
  /** Newer models in the catalog (null until read, or when the read failed). */
  newer: NewerModelsAnswer | null;
  /** Every user's suggestions (null until read, or when the read failed: the card still draws). */
  suggestions: PresetSuggestionsAnswer | null;
  /** Ids of the viewer's own suggestions, so "For <owner>" shows only on someone else's. */
  mine: Set<string>;
  /** Suggestion ids with a button press in flight. */
  busy: Set<string>;
  /** The last failure per suggestion id, shown on its row. */
  rowErrors: Map<string, string>;
  redraw: () => void;
  /** The card body, once drawn, for inserting the notice without a redraw. */
  body: HTMLElement | null;
  /** Where the pop-up was opened from, so focus can go back to it. */
  opener: HTMLElement | null;
  popup: { preset: string; node: HTMLElement; off: () => void } | null;
}

interface Parts {
  status: HTMLElement;
  body: HTMLElement;
  refreshed: HTMLElement;
  toggle: HTMLElement;
}

function kpi(
  label: string,
  value: string,
  sub: string,
  tone = "",
): HTMLElement {
  return el(
    "div",
    { class: "presets-kpi" },
    el("div", { class: "presets-kpi-l" }, label),
    el("div", { class: "presets-kpi-v" }, value),
    el("div", { class: `presets-kpi-d ${tone}`.trim() }, sub),
  );
}

function kpis(view: PresetsAnswer, days: number): HTMLElement | null {
  const k = view.kpis;
  if (!k) return null;
  const show = showOf(view);
  const up = (k.cost_change_pct ?? 0) > 0 ? "is-up" : "";
  const tokensUp = (k.tokens_change_pct ?? 0) > 0 ? "is-up" : "";
  const tokens = k.tokens ?? 0;
  let usage: HTMLElement;
  if (show === "tokens") {
    usage = kpi(
      "Tokens",
      tokensText(tokens),
      tokensDelta(k.tokens_change_pct, days),
      tokensUp,
    );
  } else {
    usage = kpi("Est. API cost", money(k.cost), costDelta(k.cost_change_pct), up);
    if (show === "both") {
      const change = tokensChange(k.tokens_change_pct);
      usage.appendChild(
        el(
          "div",
          { class: `presets-kpi-d presets-kpi-2 ${tokensUp}`.trim() },
          `${tokensText(tokens)} tokens${change ? `, ${change}` : ""}`,
        ),
      );
    }
  }
  return el(
    "div",
    { class: "presets-kpis" },
    kpi("Runs", String(k.runs), plural(k.families, "family", "families")),
    usage,
    kpi(
      "Median time to done",
      minutesText(k.median_wall_s),
      timeDelta(k.wall_change_s),
    ),
    kpi(
      "Raises / step-downs",
      `${k.raises} / ${k.step_downs}`,
      k.hung_runs === null
        ? "hung runs not recorded"
        : plural(k.hung_runs, "hung run"),
    ),
  );
}

/** The takeaways for the viewer's pick: the other metric's are hidden; at most three. */
export function shownTakeaways(view: PresetsAnswer): PresetTakeaway[] {
  const show = showOf(view);
  const hide = show === "tokens" ? "cost" : show === "cost" ? "tokens" : null;
  return (view.takeaways ?? [])
    .filter((take) => take.metric !== hide)
    .slice(0, 3);
}

function takeaways(state: State, view: PresetsAnswer): HTMLElement | null {
  const items = shownTakeaways(view);
  if (!items.length) return null;
  return el(
    "div",
    { class: "presets-takes" },
    ...items.map((take) => {
      const row = (view.presets ?? []).find((p) => p.id === take.preset);
      return el(
        "div",
        { class: `presets-take${take.level === "warn" ? " is-warn" : ""}` },
        el("b", {}, take.text),
        el("div", { class: "presets-why meta" }, take.why),
        row
          ? el(
              "div",
              { class: "presets-take-act" },
              button("Show runs", {
                title: `Open ${row.name}'s recent runs`,
                onClick: (self) => openPopup(state, row.id, self),
              }),
            )
          : null,
      );
    }),
  );
}

// ── the suggestions from the scheduled review (preset_suggestions.py) ──────

/**
 * Suggestions to list: the latest review's, plus any older one still open. Dismissed ones are
 * gone. A "no change" row stays "open" forever, so it shows only from its owner's newest review.
 */
export function visibleSuggestions(data: PresetSuggestionsAnswer | null): {
  at: string | null;
  items: PresetSuggestion[];
  open: number;
} {
  if (!data?.suggestions.length) return { at: null, items: [], open: 0 };
  const latest = [...data.reviews].sort(
    (a, b) => Date.parse(b.at) - Date.parse(a.at),
  )[0];
  const newestByOwner = new Map<string, PresetReview>();
  for (const r of data.reviews) {
    const held = newestByOwner.get(r.owner);
    if (!held || Date.parse(r.at) > Date.parse(held.at)) newestByOwner.set(r.owner, r);
  }
  const items = data.suggestions.filter((s) => {
    if (s.status === "dismissed") return false;
    if (s.kind === "no_change") {
      const newest = newestByOwner.get(s.owner);
      return !newest || s.review_id === newest.id;
    }
    return s.status === "open" || !latest || s.review_id === latest.id;
  });
  return {
    at: latest?.at ?? null,
    items,
    open: items.filter((s) => s.status === "open" && s.kind !== "no_change")
      .length,
  };
}

/** "Oct 20" for the heading, in the viewer's zone. */
function reviewDay(iso: string | null): string {
  const when = iso ? new Date(iso) : null;
  if (!when || Number.isNaN(when.getTime())) return "";
  return when.toLocaleDateString([], { month: "short", day: "numeric" });
}

/** Which buttons a suggestion shows, by kind and status (the viewer's rights come from the server). */
export function suggestionActions(
  s: PresetSuggestion,
): Array<"apply" | "undo" | "dismiss" | "issue"> {
  if (s.kind === "no_change") return [];
  if (s.kind === "own") {
    if (s.status === "open")
      return s.can_apply ? ["apply", "dismiss"] : ["dismiss"];
    if (s.status === "undone")
      return s.can_apply ? ["apply", "dismiss"] : ["dismiss"];
    if (s.status === "applying") return s.can_apply ? ["apply"] : [];
    if (s.status === "applied" || s.status === "undoing")
      return s.can_undo ? ["undo"] : [];
    return [];
  }
  return s.status === "open" ? ["issue", "dismiss"] : [];
}

const ACTIONS = {
  apply: { label: "Apply", run: applySuggestion, kind: "primary" },
  undo: { label: "Undo", run: undoSuggestion, kind: "quiet" },
  dismiss: { label: "Dismiss", run: dismissSuggestion, kind: "quiet" },
  issue: { label: "Open issue", run: openSuggestionIssue, kind: "primary" },
} as const;

function suggestionRow(state: State, s: PresetSuggestion): HTMLElement {
  const busy = state.busy.has(s.id);
  const failure = state.rowErrors.get(s.id);
  const facts = [s.why, s.saving ? `Expected saving: ${s.saving}` : ""]
    .filter(Boolean)
    .join(" ");
  const buttons = suggestionActions(s).map((name) => {
    const action = ACTIONS[name];
    return button(action.label, {
      kind: action.kind,
      disabled: busy,
      onClick: async () => {
        state.busy.add(s.id);
        state.rowErrors.delete(s.id);
        state.redraw();
        try {
          await action.run(state.context, s.id);
        } catch (error) {
          state.rowErrors.set(s.id, describeError(error));
        }
        state.busy.delete(s.id);
        try {
          state.suggestions = await fetchPresetSuggestions(state.context, true);
        } catch {
          // The row keeps its old look until the next refresh.
        }
        state.redraw();
      },
    });
  });
  const open = s.status === "open" && s.kind !== "no_change";
  return el(
    "div",
    {
      class: `presets-take presets-suggestion${open ? " is-warn" : ""}`,
      "data-suggestion": s.id,
    },
    el(
      "b",
      {},
      `${s.preset_title ? `${s.preset_title}: ` : ""}${s.title}`,
      s.status === "applied"
        ? " (applied)"
        : s.status === "undone"
          ? " (undone)"
          : "",
    ),
    state.mine.has(s.id)
      ? null
      : el("span", { class: "meta presets-for" }, ` For ${s.owner}`),
    el("div", { class: "presets-why meta" }, facts),
    s.changes_text
      ? el(
          "div",
          { class: "presets-changes meta", "data-changes": s.id },
          ...s.changes_text
            .split("\n")
            .map((line) => el("div", {}, line)),
        )
      : null,
    failure
      ? el(
          "div",
          { class: "presets-why presets-error", role: "alert" },
          failure,
        )
      : null,
    buttons.length || s.issue_url
      ? el(
          "div",
          { class: "presets-take-act" },
          ...buttons,
          s.status === "issue_opened" && s.issue_url
            ? el(
                "a",
                {
                  href: s.issue_url,
                  target: "_blank",
                  rel: "noopener noreferrer",
                },
                "Open the issue",
              )
            : null,
        )
      : null,
  );
}

function suggestions(state: State): HTMLElement | null {
  const { at, items, open } = visibleSuggestions(state.suggestions);
  if (!items.length) return null;
  return el(
    "div",
    { class: "presets-suggestions" },
    el(
      "h3",
      { class: "presets-suggestions-h" },
      `Suggestions from ${reviewDay(at) || "the latest"} review, ${open} open`,
    ),
    el(
      "div",
      { class: "presets-takes" },
      ...items.map((s) => suggestionRow(state, s)),
    ),
  );
}

function bar(row: PresetRow, show: Show): HTMLElement {
  const tokens = show === "tokens";
  const by = (tokens ? row.tokens_by_model : row.cost_by_model) ?? {};
  const noun = tokens ? "tokens" : "cost";
  const total = Object.values(by).reduce((a, b) => a + b, 0);
  const wrap = el("span", { class: "presets-bar", role: "img" });
  if (total <= 0) {
    wrap.setAttribute("aria-label", `No ${noun} recorded`);
    return wrap;
  }
  const parts: string[] = [];
  for (const [model, amount] of Object.entries(by)) {
    const pct = (amount / total) * 100;
    parts.push(`${model} ${Math.round(pct)}%`);
    const slice = el("i", {
      class: `tone-${modelTone(model)}`,
      title: `${model}: ${tokens ? `${tokensText(amount)} tokens` : money(amount)}`,
    });
    slice.style.width = `${pct.toFixed(1)}%`;
    wrap.appendChild(slice);
  }
  wrap.setAttribute(
    "aria-label",
    `${tokens ? "Tokens" : "Cost"} by model: ${parts.join(", ")}`,
  );
  return wrap;
}

function presetRow(state: State, row: PresetRow, show: Show): HTMLElement {
  const median =
    show === "tokens"
      ? el("td", { class: "n" }, tokensText(row.median_tokens))
      : el(
          "td",
          { class: "n" },
          costText(row.median_cost),
          show === "both"
            ? el(
                "span",
                { class: "presets-sub meta" },
                `${tokensText(row.median_tokens)} tokens`,
              )
            : null,
        );
  const tr = el(
    "tr",
    {
      class: "presets-row",
      tabindex: "0",
      "data-preset": row.id,
      "aria-label": `${row.name}, open recent runs`,
    },
    el(
      "td",
      { class: "presets-preset" },
      el(
        "span",
        { class: "presets-name" },
        el("i", { class: `presets-sw sw-${row.color}`, "aria-hidden": "true" }),
        row.name,
        row.custom ? el("span", { class: "presets-pill" }, "custom") : null,
      ),
      bar(row, show),
    ),
    el("td", { class: "n" }, String(row.runs)),
    el("td", { class: "n" }, duration(row.median_wall_s)),
    median,
    el(
      "td",
      { class: "presets-trend" },
      show === "tokens"
        ? sparkline(
            row.daily_tokens ?? [],
            (top) =>
              `${row.name} daily tokens, highest ${tokensText(top)}`,
          )
        : sparkline(
            row.daily,
            (top) => `${row.name} daily cost, highest ${money(top)}`,
          ),
    ),
  );
  const open = () => openPopup(state, row.id, tr);
  tr.addEventListener("click", open);
  tr.addEventListener("keydown", (event) => {
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      open();
    }
  });
  return tr;
}

function table(state: State, view: PresetsAnswer): HTMLElement {
  const show = showOf(view);
  const head = [
    "Preset",
    "Runs",
    "Median time",
    show === "tokens" ? "Median tokens" : "Median cost",
    "Trend",
  ];
  return el(
    "div",
    { class: "presets-fit" },
    el(
      "table",
      { class: "presets-table presets-main" },
      el(
        "thead",
        {},
        el(
          "tr",
          {},
          ...head.map((name, i) =>
            el(
              "th",
              {
                scope: "col",
                class: [
                  i >= 1 && i <= 3 ? "n" : "",
                  i === 4 ? "presets-trend" : "",
                ]
                  .join(" ")
                  .trim(),
              },
              name,
            ),
          ),
        ),
      ),
      el(
        "tbody",
        {},
        ...(view.presets ?? []).map((row) => presetRow(state, row, show)),
      ),
    ),
  );
}

/** The one plain line under the table for the viewer's pick. */
export function footnote(show: Show): string {
  if (show === "tokens") return TOKENS_NOTE;
  if (show === "cost") return COST_NOTE;
  return `${COST_NOTE} ${TOKENS_NOTE}`;
}

function drawBody(parts: Parts, state: State): void {
  const view = state.answer;
  const lines: Child[] = [];
  if (!state.adminOnly) lines.push(newerNotice(state));
  if (state.adminOnly) {
    lines.push(el("p", { class: "empty" }, "Preset insights are for admins."));
  } else if (view?.state === "error") {
    lines.push(
      el(
        "p",
        { class: "meta presets-error" },
        view.note || "Preset insights could not be read.",
      ),
    );
  } else if (!view || view.state === "warming") {
    lines.push(state.error ? null : el("p", { class: "meta" }, "Reading..."));
  } else if (!view.presets?.length) {
    lines.push(
      el(
        "p",
        { class: "empty" },
        `No runs in the last ${plural(state.days, "day")}.`,
      ),
    );
    if (view.mode === "fallback")
      lines.push(el("p", { class: "meta" }, FALLBACK_NOTE));
  } else {
    lines.push(
      view.mode === "fallback"
        ? el("p", { class: "meta presets-fallback" }, FALLBACK_NOTE)
        : null,
      kpis(view, state.days),
      takeaways(state, view),
      suggestions(state),
      table(state, view),
      el("p", { class: "meta presets-foot" }, footnote(showOf(view))),
    );
  }
  replaceChildren(parts.body, lines);
}

function drawToggle(
  parts: Parts,
  state: State,
  redraw: () => void,
  reload: () => void,
): void {
  replaceChildren(
    parts.toggle,
    WINDOWS.map((days) => {
      const on = state.days === days;
      const choice = el(
        "button",
        {
          type: "button",
          class: `presets-seg${on ? " is-on" : ""}`,
          "aria-pressed": String(on),
        },
        `${days} d`,
      );
      choice.addEventListener("click", () => {
        if (state.days === days) return;
        state.days = days;
        saveDays(days);
        state.answer = null;
        state.error = null;
        closePopup(state, false);
        redraw();
        reload();
      });
      return choice;
    }),
  );
}

// ── the pop-up ──────────────────────────────────────────────────────────────

/** "Oct 23 13:10" when the start is known, else "Ended Oct 23 14:02". */
export function runStartText(run: PresetRun): string {
  if (run.started_at) return startedText(run.started_at);
  return run.at ? `Ended ${startedText(run.at)}` : "-";
}

/** The pop-up's column heads for the viewer's pick: Cost, Tokens or both. */
export function runHead(show: Show): string[] {
  return [
    "Started",
    "Session",
    "Wall",
    "Active",
    ...(show === "tokens" ? [] : ["Cost"]),
    ...(show === "cost" ? [] : ["Tokens"]),
    "Agents",
    "Model changes",
    "Review",
    "Flags",
  ];
}

function runRow(state: State, run: PresetRun, show: Show): HTMLElement {
  const head = runHead(show);
  const title = run.session
    ? button(run.title || run.session, {
        title: "Open this session",
        onClick: () => {
          const session = run.session;
          if (session) void state.context.navigation.openSession(session);
        },
      })
    : el("span", {}, run.title);
  if (title instanceof HTMLButtonElement)
    title.classList.add("presets-run-link");
  // data-label feeds the stacked layout in a narrow pop-up (CSS container query).
  const cell = (index: number, cls: string, ...kids: Child[]) =>
    el("td", { class: cls, "data-label": head[index] }, ...kids);
  let next = 4;
  const usage: HTMLElement[] = [];
  if (show !== "tokens") usage.push(cell(next++, "n", costText(run.cost)));
  if (show !== "cost") usage.push(cell(next++, "n", tokensText(run.tokens)));
  return el(
    "tr",
    {},
    cell(0, "", runStartText(run)),
    cell(1, "presets-run-title", title),
    cell(2, "n", duration(run.wall_s)),
    cell(3, "n", duration(run.active_s)),
    ...usage,
    cell(next, "", run.agents),
    cell(
      next + 1,
      "",
      run.model_changes.length ? run.model_changes.join("; ") : "none",
    ),
    cell(next + 2, "", run.review),
    cell(
      next + 3,
      "",
      ...run.flags.map((flag) =>
        el("span", { class: "presets-pill is-warn" }, flag),
      ),
    ),
  );
}

function closePopup(state: State, restoreFocus: boolean): void {
  const popup = state.popup;
  if (!popup) return;
  popup.off();
  popup.node.remove();
  state.popup = null;
  const opener = state.opener;
  state.opener = null;
  if (restoreFocus && opener?.isConnected) opener.focus();
}

function openPopup(state: State, presetId: string, opener: HTMLElement): void {
  const row = state.answer?.presets?.find((p) => p.id === presetId);
  if (!row) return;
  closePopup(state, false);
  state.opener = opener;
  const show = showOf(state.answer);
  const head = runHead(show);
  const close = el(
    "button",
    {
      type: "button",
      class: "presets-pop-close btn btn-quiet",
      "aria-label": "Close recent runs",
    },
    closeIcon(),
  );
  const title = `${row.name}: ${plural(row.runs, "run")} in ${state.days} days`;
  const card = el(
    "div",
    {
      class: "presets-pop-card",
      role: "dialog",
      "aria-modal": "true",
      "aria-labelledby": "presets-pop-h",
    },
    el(
      "div",
      { class: "presets-pop-top" },
      el("h3", { id: "presets-pop-h" }, title),
      close,
    ),
    el(
      "div",
      { class: "presets-fit" },
      el(
        "table",
        { class: "presets-table presets-runs" },
        el(
          "thead",
          {},
          el("tr", {}, ...head.map((name) => el("th", { scope: "col" }, name))),
        ),
        el("tbody", {}, ...row.recent.map((run) => runRow(state, run, show))),
      ),
    ),
    el(
      "p",
      { class: "meta" },
      row.runs > row.recent.length
        ? `Showing the newest ${row.recent.length} of ${row.runs}. `
        : "",
      "Wall runs from the first message to the last turn.",
    ),
  );
  const backdrop = el("div", { class: "presets-pop" }, card);
  const onKey = (event: KeyboardEvent) => {
    if (event.key === "Escape") {
      // This Escape closes the pop-up and nothing behind it (a capture listener runs first).
      event.preventDefault();
      event.stopPropagation();
      event.stopImmediatePropagation();
      closePopup(state, true);
    } else if (event.key === "Tab") {
      const items = [
        ...card.querySelectorAll<HTMLElement>("button, [href], [tabindex]"),
      ].filter(
        (item) =>
          !item.hasAttribute("disabled") &&
          item.getAttribute("tabindex") !== "-1",
      );
      if (!items.length) return;
      const first = items[0];
      const last = items[items.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    }
  };
  document.addEventListener("keydown", onKey, true);
  backdrop.addEventListener("click", (event) => {
    if (event.target === backdrop) closePopup(state, true);
  });
  close.addEventListener("click", () => closePopup(state, true));
  document.body.appendChild(backdrop);
  state.popup = {
    preset: presetId,
    node: backdrop,
    off: () => document.removeEventListener("keydown", onKey, true),
  };
  close.focus();
}

// ── remembered choice ───────────────────────────────────────────────────────

export function loadDays(): number {
  try {
    const raw = window.localStorage.getItem(DAYS_KEY);
    const days = Number(raw);
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

// ── the card ────────────────────────────────────────────────────────────────

/** The Presets card's body; activatePresets fills it. */
export function presetsSection(): HTMLElement {
  return el(
    "div",
    { class: "admin-block admin-presets" },
    el(
      "div",
      { class: "presets-top" },
      el("p", { class: "meta refreshed presets-refreshed" }),
      el("div", {
        class: "presets-toggle",
        role: "group",
        "aria-label": "Window",
      }),
    ),
    el("div", { class: "page-status presets-status" }),
    el("div", { class: "presets-body" }),
  );
}

let timer: ReturnType<typeof setTimeout> | null = null;
let onFocus: (() => void) | null = null;
let closeActive: (() => void) | null = null;
/** Bumped by every activate and deactivate, as in accounts.ts. */
let generation = 0;

function stop(): void {
  if (timer) clearTimeout(timer);
  timer = null;
  closeActive?.();
  closeActive = null;
  if (!onFocus) return;
  window.removeEventListener("focus", onFocus);
  document.removeEventListener("visibilitychange", onFocus);
  onFocus = null;
}

/** Read and draw the section *root* (from presetsSection), again on focus and every minute. */
export function activatePresets(
  context: ExtensionContext,
  root: HTMLElement,
): void {
  const find = <T extends HTMLElement>(selector: string): T => {
    const found = root.querySelector<T>(selector);
    if (!found) throw new Error(`Presets card is missing ${selector}`);
    return found;
  };
  const parts: Parts = {
    status: find(".presets-status"),
    body: find(".presets-body"),
    toggle: find(".presets-toggle"),
    // In the card the header carries the time, so this one may have moved.
    refreshed: root.querySelector<HTMLElement>(".presets-refreshed") ?? el("p"),
  };
  const mine = ++generation;
  const live = () => generation === mine;
  stop();
  const state: State = {
    context,
    days: loadDays(),
    answer: null,
    error: null,
    adminOnly: false,
    newer: null,
    suggestions: null,
    mine: new Set(),
    busy: new Set(),
    rowErrors: new Map(),
    redraw: () => undefined,
    body: parts.body,
    opener: null,
    popup: null,
  };
  closeActive = () => closePopup(state, false);
  let said = "";
  const draw = () => {
    const message = state.error ? describeError(state.error) : "";
    if (message !== said) {
      said = message;
      replaceChildren(parts.status, [
        message
          ? notice(`Preset insights could not be read (${message}).`, "error")
          : null,
      ]);
    }
    drawToggle(parts, state, draw, () => void refresh());
    // A pop-up open over a redraw keeps its own copy of the rows; only the table is rebuilt.
    drawBody(parts, state);
    paintStatus();
  };
  // The time shown is the server's `asOf`, never this browser's clock.
  const paintStatus = () => {
    parts.refreshed.textContent = updatedText(state.answer);
    const tile = presetsTile(state.answer, state.error, state.days);
    setTile(
      state.answer?.state === "ready" ? staleTile(tile, state.error) : tile,
      answerTime(state.answer) || undefined,
    );
  };
  state.redraw = () => {
    if (live()) draw();
  };
  let reading: Promise<void> | null = null;
  let drawn = "";
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
  const refresh = (): Promise<void> => {
    if (reading) return reading;
    const asked = state.days;
    // The notice is a side read after the card paints: it never delays the card, and a failure
    // or a slow answer leaves the card as it was.
    void fetchNewerModels(context).then(
      (answer) => {
        if (!live()) return;
        const same = JSON.stringify(answer) === JSON.stringify(state.newer);
        state.newer = answer;
        if (!same) paintNotice(state);
      },
      () => undefined,
    );
    // The suggestions are a side read: a failure leaves the card as it was.
    const side = Promise.all([
      fetchPresetSuggestions(context, true),
      fetchPresetSuggestions(context, false),
    ]).then(
      ([everyone, own]) => {
        if (!live()) return;
        state.suggestions = everyone;
        state.mine = new Set(own.suggestions.map((s) => s.id));
      },
      () => undefined,
    );
    reading = Promise.all([fetchPresets(context, asked), side])
      .then(([answer]) => answer)
      .then(
        (answer) => {
          // A toggle pressed while this was out makes the answer the wrong window.
          if (!live() || asked !== state.days) return;
          state.answer = answer;
          state.error = null;
        },
        (error: unknown) => {
          if (!live() || asked !== state.days) return;
          if (error instanceof AeRequestError && error.code === "admin_only") {
            state.adminOnly = true;
            state.error = null;
            return;
          }
          state.error = error ?? new Error("Request failed");
        },
      )
      .then(() => {
        if (!live()) return;
        const open = state.popup?.preset;
        const opener = state.opener;
        // An unchanged answer is not redrawn, so a focused row stays put.
        const key = JSON.stringify([
          state.days,
          state.answer,
          state.error ? describeError(state.error) : null,
          state.adminOnly,
          state.suggestions,
          [...state.mine],
        ]);
        if (key !== drawn) {
          drawn = key;
          draw();
        } else {
          paintStatus();
        }
        // The rows were rebuilt, so the opener is a stale node: point it at the new row.
        if (open && state.popup) {
          state.opener =
            parts.body.querySelector<HTMLElement>(
              `[data-preset="${CSS.escape(open)}"]`,
            ) ?? opener;
        }
        if (!state.adminOnly) schedule();
      })
      .finally(() => {
        reading = null;
        // The window changed while a read was out: read the one now chosen.
        if (live() && asked !== state.days && !state.adminOnly) void refresh();
      });
    return reading;
  };
  draw();
  void refresh();
  registerRefresh("presets", () => (live() ? refresh() : undefined));
  onFocus = () => {
    if (document.visibilityState === "visible") void refresh();
  };
  window.addEventListener("focus", onFocus);
  document.addEventListener("visibilitychange", onFocus);
}

export function deactivatePresets(): void {
  generation++;
  stop();
}
