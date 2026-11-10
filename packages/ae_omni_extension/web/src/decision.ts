// The Admin Center's Decision model card (2026-11-07): whether the outside judge model is on,
// how many calls it has used today, and the mode of each decision point.
//
// Why: the judge model is optional and off unless several server settings line up. The owner
// wanted to see its state next to the other Cloud cards, and to know which setting is missing
// without reading the server's logs. The card reads `GET /v1/ae/decision`
// (ae_omni_policies/decision_status.py), which carries numbers and labels only: never the
// endpoint, the key, the user list or the denylist.
//
// States: On when the model would answer the caller; Off when AE_DECISION_ENABLED is not set
// (the rows are muted and a visible line names the switch); amber when it is enabled but
// something is missing, with the reason in plain words; amber "Error" while the circuit breaker
// is open. When nothing is configured and it is not enabled the tile is `notSetUp`, so the card
// leaves the board and the footer names "Decision model" (board.ts). The switch hint is a
// tooltip in every state (hint.ts) and also a visible line only when Off.
//
// Polling: every 60 s and on focus; a scheduled read is skipped while the tab is hidden.

import type { ExtensionContext } from "@omnigent/extension-sdk";
import { type DecisionReason, type DecisionStatus, describeError, fetchDecision } from "./api";
import { dayText } from "./databricks";
import { el, replaceChildren } from "./dom";
import { hint } from "./hint";
import { type Health, registerRefresh, setTile, staleTile, type Tile } from "./tiles";

export const POLL_MS = 60_000;
export const SWITCH_LINE = "Switch: AE_DECISION_ENABLED in server.env. Per point: AE_DECISION_POINTS.";
export const SWITCH_HINT =
  "AE_DECISION_ENABLED turns the judge on or off. AE_DECISION_POINTS sets each point to off, shadow or live.";

const POINT_LABELS: Record<string, string> = {
  step_down: "Lead step-down",
  review_stop: "Review loop stop",
  agent_ask: "Agent questions (ask_judge)",
  advisor: "Preset advisor (Lead picks)",
};

/** A point's plain label; a point the card does not know shows its raw name. */
export function pointLabel(name: string): string {
  return POINT_LABELS[name] ?? name;
}

const REASON_TEXT: Record<Exclude<DecisionReason, "not_enabled">, string> = {
  no_endpoint: "On, but no endpoint is set (AE_DECISION_ENDPOINT)",
  no_key: "On, but no key is set (AE_DECISION_API_KEY)",
  no_denylist:
    "On, but the denylist file is missing or empty, so it refuses every call (AE_DECISION_DENYLIST_FILE)",
  no_users:
    "On, but no users are allowed: set AE_DECISION_USERS (on a single-user server, local)",
  not_allowed: "On, but you are not on the allowed list (AE_DECISION_USERS)",
};

/** The reason in plain words, naming the setting to fix. */
export function reasonText(reason: DecisionReason | undefined): string {
  if (!reason || reason === "not_enabled") return "Off";
  return REASON_TEXT[reason] ?? "On, but not available";
}

/** A clock time in the viewer's own clock: `1:00 PM`. */
function clockTime(ms: number): string {
  return new Date(ms).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
}

export type StateKind = "on" | "off" | "setup" | "paused" | "unset";

/** The card's state from the server's answer. */
export function stateOf(data: DecisionStatus): StateKind {
  // Not enabled and nothing set: the card leaves the board. A partly set up server shows Off.
  if (!data.enabled && !(data.set_up ?? data.configured)) return "unset";
  if (!data.enabled) return "off";
  // The reason comes first; a pause is said under it.
  if (!data.available) return "setup";
  return data.paused_until ? "paused" : "on";
}

/** "Decision-1" from the deployment name, as the model row says it. */
export function modelText(model: string | null): string {
  if (!model) return "Unknown";
  return model
    .split("-")
    .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
    .join("-");
}

/** "retires Feb 4, 2027" for a valid date, else null. */
export function retiresText(retires: string | null): string | null {
  return retires && /^\d{4}-\d{2}-\d{2}$/.test(retires) ? `retires ${dayText(retires)}` : null;
}

/** The line under the pill for the amber states, null when there is nothing to say. */
export function stateLines(data: DecisionStatus, now = Date.now()): string[] {
  const kind = stateOf(data);
  const lines: string[] = [];
  if (kind === "setup") {
    // No reason with an error: the server could not finish reading its own setup.
    lines.push(!data.reason && data.error ? "Status could not be read" : reasonText(data.reason));
  }
  if ((kind === "paused" || kind === "setup") && data.paused_until) {
    const until = Date.parse(data.paused_until);
    const when = Number.isNaN(until) ? "a few minutes" : clockTime(Math.max(until, now));
    lines.push(`Paused until ${when} after errors in a row; points use their normal rules.`);
  }
  return lines;
}

/** The first of the state lines, or null. */
export function stateLine(data: DecisionStatus, now = Date.now()): string | null {
  return stateLines(data, now)[0] ?? null;
}

export function decisionTile(data: DecisionStatus | null, error: unknown): Tile {
  if (!data) {
    return {
      id: "decision",
      value: error ? "Unknown" : "Reading...",
      note: error ? "could not be read" : "",
      health: error ? "attention" : "idle",
    };
  }
  const kind = stateOf(data);
  const calls = `${data.calls_today ?? 0} of ${data.daily_cap ?? 0} calls today`;
  if (kind === "unset") {
    return { id: "decision", value: "Not set up", note: "", health: "idle", notSetUp: true };
  }
  const health: Health = kind === "on" ? "ok" : kind === "off" ? "idle" : "attention";
  const value = { on: "On", off: "Off", setup: "Needs setup", paused: "Paused" }[kind];
  const brief = {
    on: "Decision model on",
    off: "Decision model off",
    setup: "Decision model needs setup",
    paused: "Decision model paused",
  }[kind];
  return {
    id: "decision",
    value,
    summary: brief,
    brief,
    note: kind === "off" ? "" : calls,
    health,
    title: stateLine(data) ?? undefined,
  };
}

// ── the card ────────────────────────────────────────────────────────────────

/** The state pill for the card header; activateDecision fills it. */
export function decisionPill(): HTMLElement {
  return el("span", { class: "admin-pill decision-pill", hidden: true });
}

/** The card's body; activateDecision fills it. */
export function decisionSection(): HTMLElement {
  return el(
    "div",
    { class: "admin-block admin-decision" },
    el("div", { class: "decision-body", "aria-live": "polite" }),
  );
}

function row(label: string, ...value: Array<Node | string>): HTMLElement {
  return el(
    "div",
    { class: "decision-row" },
    el("span", { class: "decision-k" }, label),
    el("span", { class: "decision-v" }, ...value),
  );
}

const small = (text: string) => el("small", {}, text);

function pointRows(data: DecisionStatus): HTMLElement {
  return el(
    "div",
    { class: "decision-row decision-points" },
    ...(data.points ?? []).map((point) =>
      el(
        "div",
        { class: "decision-pt", "data-point": point.name },
        el("span", { class: "decision-pt-name" }, pointLabel(point.name)),
        el("span", { class: `decision-chip decision-chip-${point.mode}` }, point.mode),
      ),
    ),
  );
}

/** The pill's text and class for a state. */
export function pillOf(kind: StateKind): { text: string; cls: string } | null {
  switch (kind) {
    case "on":
      return { text: "On", cls: "admin-pill-ok" };
    case "off":
      return { text: "Off", cls: "admin-pill-off" };
    case "setup":
      return { text: "Check setup", cls: "admin-pill-warn" };
    case "paused":
      return { text: "Error", cls: "admin-pill-warn" };
    default:
      return null;
  }
}

/** Draw the card from a good answer, or from a failed read. */
export function renderDecision(
  body: HTMLElement,
  pill: HTMLElement | null,
  data: DecisionStatus | null,
  error: unknown,
  now = Date.now(),
): void {
  if (pill) {
    const state = data ? pillOf(stateOf(data)) : null;
    pill.hidden = !state && !(error && !data);
    pill.className = `admin-pill decision-pill ${state ? state.cls : "admin-pill-warn"}`;
    pill.textContent = state ? state.text : error ? "Unknown" : "";
  }
  if (!data) {
    replaceChildren(body, [
      el(
        "p",
        { class: error ? "decision-note" : "meta" },
        error ? `Could not read the Decision model: ${describeError(error)}` : "Reading...",
      ),
    ]);
    return;
  }
  const kind = stateOf(data);
  const muted = kind === "off";
  const lines = stateLines(data, now);
  const retires = retiresText(data.retires);
  const shadow = data.shadow?.would_act_7d ?? 0;
  replaceChildren(body, [
    ...lines.map((line) => el("p", { class: "decision-note", role: "status" }, line)),
    error ? el("p", { class: "decision-note" }, `Showing the last answer: ${describeError(error)}`) : null,
    data.error ? el("p", { class: "meta" }, `Part of the status could not be read (${data.error}).`) : null,
    el(
      "div",
      { class: `decision-rows${muted ? " is-muted" : ""}` },
      row("Model", modelText(data.model), retires ? " " : "", retires ? small(retires) : ""),
      row("Calls today", String(data.calls_today ?? 0), " ", small(`of ${data.daily_cap ?? 0}`)),
      pointRows(data),
      row("Shadow results", small(`${shadow} would-act ${shadow === 1 ? "call" : "calls"} in 7 days`)),
    ),
    el(
      "div",
      { class: "decision-foot" },
      muted ? el("p", { class: "meta decision-switch" }, SWITCH_LINE) : el("span"),
      hint(SWITCH_HINT, "Decision model"),
    ),
  ]);
}

let timer: ReturnType<typeof setTimeout> | null = null;
let onFocus: (() => void) | null = null;
let generation = 0;

/** Read and draw the card *root* (holding decisionSection and decisionPill) and keep it fresh. */
export function activateDecision(context: ExtensionContext, root: HTMLElement): void {
  const body = root.querySelector<HTMLElement>(".decision-body");
  const pill = root.querySelector<HTMLElement>(".decision-pill");
  if (!body) throw new Error("Decision model section is missing");
  const mine = ++generation;
  const live = () => generation === mine;
  if (onFocus) {
    window.removeEventListener("focus", onFocus);
    document.removeEventListener("visibilitychange", onFocus);
  }
  let data: DecisionStatus | null = null;
  let error: unknown = null;
  let reading: Promise<void> | null = null;
  const draw = () => {
    if (!live()) return;
    renderDecision(body, pill, data, error);
    const tile = decisionTile(data, error);
    setTile(error && data ? staleTile(tile, error) : tile, Date.now());
  };
  const refresh = (): Promise<void> => {
    if (reading) return reading;
    reading = fetchDecision(context)
      .then(
        (next) => {
          data = next;
          error = null;
        },
        (failure: unknown) => {
          error = failure;
        },
      )
      .then(() => {
        if (!live()) return;
        draw();
        if (timer) clearTimeout(timer);
        timer = setTimeout(() => {
          if (live() && document.visibilityState === "visible") void refresh();
        }, POLL_MS);
      })
      .finally(() => {
        reading = null;
      });
    return reading;
  };
  draw();
  void refresh();
  registerRefresh("decision", () => (live() ? refresh() : undefined));
  onFocus = () => {
    if (document.visibilityState === "visible") void refresh();
  };
  window.addEventListener("focus", onFocus);
  document.addEventListener("visibilitychange", onFocus);
}

export function deactivateDecision(): void {
  generation++;
  if (timer) clearTimeout(timer);
  timer = null;
  if (onFocus) {
    window.removeEventListener("focus", onFocus);
    document.removeEventListener("visibilitychange", onFocus);
  }
  onFocus = null;
}
