// Cost targets on the Admin Center's Azure card (2026-10-25): limits the owner sets on
// spend or on a running Fabric capacity, each with a status, and a phone alarm
// from the server (cost_targets.py) when one is crossed.
//
// Why: the Azure card is only seen by someone who opens it, and a Fabric capacity
// left running bills $0.72 an hour. The server judges the targets against the
// reading it already holds and files a task-store task; this panel is where the
// limits are set, switched and removed. The extension host sends only GET, POST and
// PATCH, so saving and deleting are POSTs (api.ts).
//
// Layout (2026-10-26, the owner's concept A): the section starts folded to one line, a
// chevron, "Targets", the counts and, pushed right on the same row, a chip for each
// target that is over, one green chip when all is well, or one amber chip when targets
// cannot be checked. Only the first chip shows, then "+N more" for the rest, so the
// one shown stays readable on the counts' row. Open, the alerts note, then a bordered
// list with a line per target (status dot, name and what it measures, a meter, the
// figure) that opens in place to the detail and On/Edit/Delete, over first. The rows and
// the form are drawn apart so a poll never wipes what is being typed. Delete asks in the
// page (a second tap), never window.confirm. Open or closed is remembered per viewer
// (sections.ts, id "azure-targets").

import type { ExtensionContext } from "@omnigent/extension-sdk";
import {
  AeRequestError,
  type CostMeasure,
  type CostScopeKind,
  type CostTarget,
  type CostTargetInput,
  type CostTargetsAnswer,
  type CostTargetStatus,
  deleteCostTarget,
  describeError,
  fetchCostTargets,
  saveCostTarget,
} from "./api";
import { button, type Child, el, notice, replaceChildren } from "./dom";
import { chevronIcon } from "./icons";
import { isOpen, setOpen } from "./sections";

/** The sections.ts id that remembers whether the targets are shown; closed until opened once. */
export const TARGETS_OPEN_ID = "azure-targets";

export const MEASURES: Array<{ id: CostMeasure; label: string; hint: string }> = [
  { id: "month_to_date", label: "Month to date", hint: "Spend so far this month, in USD." },
  {
    id: "latest_finished_day",
    label: "Latest finished day",
    hint: "The newest day Azure has posted costs for, in USD.",
  },
  {
    id: "month_end_forecast",
    label: "Month-end forecast",
    hint: "The card's pace figure for the whole month, in USD. Needs 3 days of data.",
  },
  {
    id: "fabric_active_hours",
    label: "Fabric hours Active",
    hint: "How long a Fabric capacity has been running, in hours.",
  },
];

/** Which scopes each measure can use; the server enforces the same table. */
export const ALLOWED_KINDS: Record<CostMeasure, CostScopeKind[]> = {
  month_to_date: ["all", "subscription", "service"],
  latest_finished_day: ["all", "subscription", "resource"],
  month_end_forecast: ["all", "subscription"],
  fabric_active_hours: ["capacity"],
};

const KIND_LABELS: Record<CostScopeKind, string> = {
  all: "All of Azure",
  subscription: "One subscription",
  service: "One service",
  resource: "One resource",
  capacity: "Fabric capacity",
};

/** A name is picked for these; for resource and capacity an empty name means each one. */
const NAME_REQUIRED: CostScopeKind[] = ["subscription", "service"];
const NAME_OPTIONAL: CostScopeKind[] = ["resource", "capacity"];

export const EMPTY_TEXT =
  "A target is a limit you set on Azure spend or on a running Fabric capacity. When it is crossed, you get a task with a phone alarm.";

const STATUS_LABELS: Record<CostTargetStatus, string> = {
  ok: "Under",
  over: "Over",
  alerted: "Alerted",
  "cannot-evaluate": "Cannot check",
  off: "Off",
};

export const measureLabel = (measure: CostMeasure): string =>
  MEASURES.find((item) => item.id === measure)?.label ?? measure;

export const isHours = (measure: CostMeasure): boolean => measure === "fabric_active_hours";

/** "$600" or "6 hours", the limit as the rule's measure counts it. */
export function limitText(measure: CostMeasure, limit: number): string {
  if (isHours(measure)) return `${limit} ${limit === 1 ? "hour" : "hours"}`;
  const whole = Number.isInteger(limit);
  return `$${limit.toLocaleString("en-US", {
    minimumFractionDigits: whole ? 0 : 2,
    maximumFractionDigits: 2,
  })}`;
}

/** What a rule judges, in a phrase: "MVP 2025", "each resource", "all of Azure". */
export function scopeText(scope: CostTargetInput["scope"]): string {
  if (scope.kind === "all") return "All of Azure";
  if (scope.name) return scope.kind === "service" ? `${scope.name} (service)` : scope.name;
  return scope.kind === "resource" ? "Each resource" : "Each Fabric capacity";
}

/** The figure now, in the rule's own unit. */
export function valueText(rule: Pick<CostTarget, "measure" | "value">): string | null {
  if (rule.value === null) return null;
  if (isHours(rule.measure)) return `${Math.round(rule.value * 10) / 10} h`;
  return limitText(rule.measure, Math.round(rule.value * 100) / 100);
}

export function statusLabel(status: CostTargetStatus): string {
  return STATUS_LABELS[status];
}

/** The line under a row's title: what is judged, against what. */
export function ruleLine(rule: CostTarget): string {
  const verb = rule.measure === "latest_finished_day" ? "a day" : "";
  const over = `over ${limitText(rule.measure, rule.limit)}${verb ? ` ${verb}` : ""}`;
  return `${scopeText(rule.scope)} · ${measureLabel(rule.measure)} ${over}`;
}

/** The second line: the figure and what it covers, or why the target cannot be checked. */
export function figureLine(rule: CostTarget): string | null {
  if (rule.status === "off") return "Turned off. No alert will be filed.";
  const now = valueText(rule);
  if (rule.status === "cannot-evaluate") {
    return rule.reason ?? "This target cannot be checked right now.";
  }
  const parts: string[] = [];
  if (now) parts.push(`Now ${now}`);
  if (rule.dataDate) parts.push(`through ${rule.dataDate}`);
  if (rule.over?.length) parts.push(`over: ${rule.over.slice(0, 3).join(", ")}`);
  return parts.length ? parts.join(", ") : null;
}

// ── what the folded line and the rows say ───────────────────────────────────

/** Over its limit now, or already alerted. */
export const isOver = (rule: Pick<CostTarget, "status">): boolean =>
  rule.status === "over" || rule.status === "alerted";

export interface Counts {
  total: number;
  over: number;
  ok: number;
  cannot: number;
  off: number;
}

export function countsOf(rules: CostTarget[]): Counts {
  const counts: Counts = { total: rules.length, over: 0, ok: 0, cannot: 0, off: 0 };
  for (const rule of rules) {
    if (isOver(rule)) counts.over += 1;
    else if (rule.status === "ok") counts.ok += 1;
    else if (rule.status === "cannot-evaluate") counts.cannot += 1;
    else counts.off += 1;
  }
  return counts;
}

/** How full the limit is: value over limit, 0 when there is no value. */
const ratioOf = (rule: CostTarget): number =>
  rule.value === null || rule.limit <= 0 ? 0 : rule.value / rule.limit;

/** Over or alerted first, then cannot-check, then ok by how close to its limit, then off. */
export function sortedRules(rules: CostTarget[]): CostTarget[] {
  const rank = (rule: CostTarget): number =>
    isOver(rule) ? 0 : rule.status === "cannot-evaluate" ? 1 : rule.status === "ok" ? 2 : 3;
  return rules
    .map((rule, index) => ({ rule, index }))
    .sort((a, b) => {
      const by = rank(a.rule) - rank(b.rule);
      if (by) return by;
      if (rank(a.rule) <= 2 && rank(a.rule) !== 1) {
        const gap = ratioOf(b.rule) - ratioOf(a.rule);
        if (gap) return gap;
      }
      return a.index - b.index;
    })
    .map((item) => item.rule);
}

export type MeterTone = "over" | "near" | "ok" | "none";

/** The meter: fill is value over limit, capped at 100%; red when over, amber from 75%. */
export function meterOf(rule: CostTarget): { pct: number; tone: MeterTone } {
  if (rule.status === "off" || rule.status === "cannot-evaluate") return { pct: 0, tone: "none" };
  if (rule.value === null) {
    return isOver(rule) ? { pct: 100, tone: "over" } : { pct: 0, tone: "none" };
  }
  const ratio = ratioOf(rule);
  const pct = Math.max(0, Math.min(100, Math.round(ratio * 100)));
  if (isOver(rule)) return { pct, tone: "over" };
  return { pct, tone: ratio >= 0.75 ? "near" : "ok" };
}

const MEASURE_WORDS: Record<CostMeasure, string> = {
  month_to_date: "month to date",
  latest_finished_day: "one day",
  month_end_forecast: "forecast",
  fabric_active_hours: "any capacity",
};

/** The small muted words after a row's name: what it measures. */
export function measureWords(rule: CostTarget): string {
  let words = MEASURE_WORDS[rule.measure];
  if (rule.measure === "fabric_active_hours" && rule.scope.name) words = rule.scope.name;
  if (rule.measure === "latest_finished_day" && rule.over?.length) {
    words += ` \u00b7 top: ${rule.over[0]}`;
  }
  return rule.status === "off" ? `${words} \u00b7 off` : words;
}

/** "$61" and "$50" (or "0 h" and "6 h") for a row's figure; the first is null without a value. */
export function figureParts(rule: CostTarget): { now: string | null; limit: string } {
  const limit = isHours(rule.measure) ? `${rule.limit} h` : limitText(rule.measure, rule.limit);
  return { now: valueText(rule), limit };
}

/** A reason cut to a chip: its first sentence or clause, 70 characters at most. */
export function shortReason(reason: string): string {
  const first = reason.split(/\.\s|\s\(|;\s|,\s/)[0].replace(/\.$/, "");
  return first.length > 70 ? `${first.slice(0, 69)}\u2026` : first;
}

/** The one reason every enabled target shares for being unchecked, else null. */
export function sharedReason(rules: CostTarget[]): string | null {
  const live = rules.filter((rule) => rule.status !== "off");
  if (!live.length || !live.every((rule) => rule.status === "cannot-evaluate")) return null;
  const first = live[0].reason ?? "";
  return first && live.every((rule) => (rule.reason ?? "") === first) ? first : null;
}

export interface Chip {
  tone: "err" | "warn" | "ok";
  text: string;
}

/** The chips beside the counts: shown folded and open. */
export function chipsOf(rules: CostTarget[]): Chip[] {
  const counts = countsOf(rules);
  const chips: Chip[] = [];
  for (const rule of sortedRules(rules).filter(isOver)) {
    const { now, limit } = figureParts(rule);
    const sum = now ? `${rule.name} ${now} of ${limit}` : `${rule.name} over ${limit}`;
    chips.push({ tone: "err", text: rule.alert ? `${sum} \u00b7 task filed` : sum });
  }
  if (counts.cannot) {
    const shared = sharedReason(rules.filter((rule) => rule.status === "cannot-evaluate"));
    chips.push({
      tone: "warn",
      text: shared ? shortReason(shared) : `${counts.cannot} cannot be checked`,
    });
  } else if (!counts.over && counts.ok) {
    chips.push({ tone: "ok", text: "Every target is under its limit" });
  }
  // One readable chip and a count beat three cut to "Sec..." beside the counts.
  if (chips.length <= 1) return chips;
  const rest = chips.slice(1);
  const tone = rest.some((chip) => chip.tone === "err") ? "err" : "warn";
  return [chips[0], { tone, text: `+${rest.length} more` }];
}

export interface Draft {
  /** Null for a new target. */
  id: string | null;
  name: string;
  measure: CostMeasure;
  kind: CostScopeKind;
  scopeName: string;
  limit: string;
  enabled: boolean;
}

export function newDraft(): Draft {
  return {
    id: null,
    name: "",
    measure: "month_to_date",
    kind: "all",
    scopeName: "",
    limit: "",
    enabled: true,
  };
}

/** The part of a listed target that the server takes back on a save. */
export function inputOf(rule: CostTarget): CostTargetInput {
  return {
    name: rule.name,
    enabled: rule.enabled,
    scope: { kind: rule.scope.kind, name: rule.scope.name },
    measure: rule.measure,
    limit: rule.limit,
  };
}

export function draftOf(rule: CostTarget): Draft {
  return {
    id: rule.id,
    name: rule.name,
    measure: rule.measure,
    kind: rule.scope.kind,
    scopeName: rule.scope.name ?? "",
    limit: String(rule.limit),
    enabled: rule.enabled,
  };
}

/** A fresh id the server accepts: lowercase letters and digits. */
export function newTargetId(random: () => number = Math.random): string {
  return `t-${Math.floor(random() * 36 ** 6)
    .toString(36)
    .padStart(6, "0")}`;
}

/** Change the measure; a scope it cannot use falls back to the first one it can. */
export function withMeasure(draft: Draft, measure: CostMeasure): Draft {
  const kinds = ALLOWED_KINDS[measure];
  const keep = kinds.includes(draft.kind);
  return {
    ...draft,
    measure,
    kind: keep ? draft.kind : kinds[0],
    scopeName: keep ? draft.scopeName : "",
  };
}

export function withKind(draft: Draft, kind: CostScopeKind): Draft {
  return { ...draft, kind, scopeName: "" };
}

/** The names the scope picker offers for *kind*, from the server's lists. */
export function scopeChoices(kind: CostScopeKind, scopes: CostTargetsAnswer["scopes"]): string[] {
  if (kind === "subscription") return scopes.subscriptions;
  if (kind === "service") return scopes.services;
  if (kind === "resource") return scopes.resources;
  if (kind === "capacity") return scopes.capacities;
  return [];
}

/** The default name for a target left unnamed: the line it would read as. */
export function defaultName(draft: Draft): string {
  const scope = scopeText({ kind: draft.kind, name: draft.scopeName.trim() || null });
  return `${scope} ${measureLabel(draft.measure).toLowerCase()}`.slice(0, 80);
}

/** What to fix before saving, or the payload. The server checks the same things again. */
export function toInput(draft: Draft): CostTargetInput | string {
  const limit = Number(draft.limit);
  if (draft.limit.trim() === "" || !Number.isFinite(limit) || limit <= 0) {
    return `Enter a limit above 0 (${isHours(draft.measure) ? "hours" : "USD"}).`;
  }
  const name = draft.scopeName.trim();
  if (NAME_REQUIRED.includes(draft.kind) && !name) {
    return `Pick a ${draft.kind} for this target.`;
  }
  const label = draft.name.trim() || defaultName(draft);
  if (label.length > 80) return "Shorten the name to 80 characters or fewer.";
  return {
    name: label,
    enabled: draft.enabled,
    scope: { kind: draft.kind, name: name || null },
    measure: draft.measure,
    limit,
  };
}

interface State {
  answer: CostTargetsAnswer | null;
  error: unknown;
  /** The server answered admin_only: say nothing and stop polling. */
  adminOnly: boolean;
  draft: Draft | null;
  formError: string | null;
  saving: boolean;
  /** The id whose delete awaits a second tap. */
  confirming: string | null;
  rowError: string | null;
  /** The ids whose detail is shown. */
  expanded: Set<string>;
}

export interface Targets {
  /** Read the targets again (the card calls this on its own poll). */
  refresh: () => Promise<void>;
  dispose: () => void;
}

/** The panel's empty shell; *mountTargets* fills it. */
export function targetsSection(): HTMLElement {
  return el(
    "section",
    { class: "azure-targets", "aria-label": "Cost targets" },
    el("button", { class: "azure-targets-head", type: "button", "aria-expanded": "false" }),
    el(
      "div",
      { class: "azure-targets-body", hidden: true },
      el("div", { class: "azure-targets-status" }),
      el("ul", { class: "azure-targets-rows" }),
      el("div", { class: "azure-targets-foot" }),
      el("div", { class: "azure-targets-form" }),
    ),
  );
}

function dot(status: CostTargetStatus): HTMLElement {
  const kind = status === "cannot-evaluate" ? "cannot" : status === "alerted" ? "over" : status;
  return el("span", { class: `azure-dot azure-dot-${kind}`, "aria-hidden": "true" });
}

function field(label: string, control: HTMLElement, hint?: string): HTMLElement {
  const id = `azt-${Math.random().toString(36).slice(2, 8)}`;
  control.id = id;
  return el(
    "div",
    { class: "field" },
    el("label", { for: id }, label),
    control,
    hint ? el("span", { class: "hint" }, hint) : null,
  );
}

/** Draw the panel in *root* (from targetsSection) and keep it fresh. */
export function mountTargets(context: ExtensionContext, root: HTMLElement): Targets {
  const head = root.querySelector<HTMLElement>(".azure-targets-head");
  const body = root.querySelector<HTMLElement>(".azure-targets-body");
  const status = root.querySelector<HTMLElement>(".azure-targets-status");
  const rows = root.querySelector<HTMLElement>(".azure-targets-rows");
  const foot = root.querySelector<HTMLElement>(".azure-targets-foot");
  const formBox = root.querySelector<HTMLElement>(".azure-targets-form");
  if (!head || !body || !status || !rows || !foot || !formBox) {
    throw new Error("Targets section is missing");
  }
  const state: State = {
    answer: null,
    error: null,
    adminOnly: false,
    draft: null,
    formError: null,
    saving: false,
    confirming: null,
    rowError: null,
    expanded: new Set(),
  };
  let alive = true;
  let loading: Promise<void> | null = null;

  const startEdit = (draft: Draft) => {
    state.draft = draft;
    state.formError = null;
    drawForm();
    drawHead();
    formBox.querySelector<HTMLElement>("input, select")?.focus();
  };
  const stopEdit = () => {
    state.draft = null;
    state.formError = null;
    drawForm();
    drawHead();
  };

  const act = async (work: () => Promise<unknown>): Promise<string | null> => {
    try {
      await work();
      await load();
      return null;
    } catch (error) {
      return describeError(error);
    }
  };

  const setFolded = (open: boolean) => {
    head.setAttribute("aria-expanded", String(open));
    body.hidden = !open;
  };
  setFolded(isOpen(TARGETS_OPEN_ID, false));
  head.addEventListener("click", () => {
    const open = head.getAttribute("aria-expanded") !== "true";
    setFolded(open);
    setOpen(TARGETS_OPEN_ID, open);
  });

  function drawHead(): void {
    const answer = state.answer;
    const counts = answer ? countsOf(answer.rules) : null;
    const parts: Child[] = [];
    const add = (part: Child) => {
      if (parts.length) parts.push(" \u00b7 ");
      parts.push(part);
    };
    if (counts) {
      add(counts.total ? el("b", {}, String(counts.total)) : "No targets");
      if (counts.over) add(el("span", { class: "azure-sum-over" }, `${counts.over} over`));
      if (counts.ok) add(`${counts.ok} ok`);
      if (counts.cannot) {
        add(el("span", { class: "azure-sum-warn" }, `${counts.cannot} cannot check`));
      }
      if (counts.off) add(`${counts.off} off`);
    }
    const chips: Chip[] = answer ? chipsOf(answer.rules) : [];
    if (!answer && state.error) chips.push({ tone: "warn", text: "Targets could not be read" });
    replaceChildren(head as HTMLElement, [
      chevronIcon(),
      el("span", { class: "azure-targets-label" }, "Targets"),
      parts.length ? el("span", { class: "azure-targets-sum" }, ...parts) : null,
      chips.length
        ? el(
            "span",
            { class: "azure-targets-chips" },
            ...chips.map((chip) =>
              el(
                "span",
                {
                  class: `azure-chip azure-chip-${chip.tone}${chip.text.startsWith("+") ? " azure-chip-more" : ""}`,
                  title: chip.text,
                },
                chip.text,
              ),
            ),
          )
        : null,
    ]);
    drawFoot();
  }

  // Rows and the foot are rebuilt on every toggle and poll: put keyboard focus back on the
  // element that had it (a row, a row's switch, or "+ Add target").
  function keepFocus(draw: () => void): void {
    const doc = (head as HTMLElement).ownerDocument;
    const active = doc.activeElement as HTMLElement | null;
    let find: string | null = null;
    if (active && (head as HTMLElement).closest(".azure-targets")?.contains(active)) {
      const id = active.getAttribute("data-rule-id");
      if (active.classList.contains("azure-target-main") && id) {
        find = `.azure-target-main[data-rule-id="${CSS.escape(id)}"]`;
      } else if (active.classList.contains("azure-target-toggle") && id) {
        find = `.azure-target-toggle[data-rule-id="${CSS.escape(id)}"]`;
      } else if (active.classList.contains("azure-add")) {
        find = ".azure-add";
      }
    }
    draw();
    if (find) {
      (head as HTMLElement).closest(".azure-targets")?.querySelector<HTMLElement>(find)?.focus();
    }
  }

  function drawFoot(): void {
    keepFocus(drawFootNow);
  }

  function drawFootNow(): void {
    const max = state.answer?.max ?? 50;
    const full = (state.answer?.rules.length ?? 0) >= max;
    replaceChildren(foot as HTMLElement, [
      state.answer && !state.draft
        ? el(
            "button",
            {
              class: "azure-add",
              type: "button",
              title: full ? `Up to ${max} targets` : "Set a new limit",
              disabled: full,
            },
            "+ Add target",
          )
        : null,
      state.answer && state.answer.rules.length > 1
        ? el("span", { class: "meta azure-targets-sorted" }, "Sorted: over first, then closest to its limit")
        : null,
    ]);
    (foot as HTMLElement).querySelector<HTMLButtonElement>(".azure-add")?.addEventListener("click", () => {
      startEdit(newDraft());
    });
  }

  function drawStatus(): void {
    const answer = state.answer;
    const children: Child[] = [];
    if (state.error) {
      children.push(notice(`Targets could not be read (${describeError(state.error)}).`, "error"));
    }
    if (state.rowError) children.push(notice(state.rowError, "error"));
    if (answer && !answer.alerts.on) {
      children.push(
        notice(
          `Alerts are off (${answer.alerts.offReason ?? "no task store"}). Targets still show their status.`,
          "info",
        ),
      );
    } else if (answer) {
      const open = answer.alerts.open;
      children.push(
        el(
          "p",
          { class: "meta azure-targets-meta" },
          `Alerts go to ${answer.alerts.list} at ${answer.alerts.time} (${answer.alerts.tz}); a running Fabric capacity rings within minutes.`,
          open ? ` ${open} open.` : "",
          answer.alerts.lastError ? ` Last problem: ${answer.alerts.lastError}` : "",
        ),
      );
    }
    if (answer?.note) children.push(el("p", { class: "meta azure-targets-meta" }, `${answer.note}.`));
    const shared = answer ? sharedReason(answer.rules) : null;
    if (shared) children.push(el("p", { class: "azure-targets-shared", role: "note" }, shared));
    replaceChildren(status as HTMLElement, children);
  }

  function rowOf(rule: CostTarget, shared: string | null): HTMLElement {
    const open = state.expanded.has(rule.id);
    const line = shared && rule.status === "cannot-evaluate" ? null : figureLine(rule);
    const confirming = state.confirming === rule.id;
    const meter = meterOf(rule);
    const { now, limit } = figureParts(rule);
    const toggle = el(
      "button",
      {
        class: "azure-switch azure-target-toggle",
        "data-rule-id": rule.id,
        type: "button",
        role: "switch",
        "aria-checked": String(rule.enabled),
        title: rule.enabled ? "Turn this target off" : "Turn this target on",
      },
      el("i", { "aria-hidden": "true" }),
      rule.enabled ? "On" : "Off",
    );
    toggle.addEventListener("click", async () => {
      if (toggle.disabled) return;
      toggle.disabled = true;
      state.rowError = await act(() =>
        saveCostTarget(context, rule.id, { ...inputOf(rule), enabled: !rule.enabled }),
      );
      drawStatus();
      drawRows();
    });
    const main = el(
      "button",
      {
        class: "azure-target-main",
        type: "button",
        "aria-expanded": String(open),
        "data-rule-id": rule.id,
      },
      dot(rule.status),
      el("span", { class: "sr-only" }, `${statusLabel(rule.status)}. `),
      el("span", { class: "azure-target-name" }, rule.name, el("small", {}, measureWords(rule))),
      el(
        "span",
        { class: `azure-meter azure-meter-${meter.tone}`, "data-tone": meter.tone },
        el("i", { style: `width:${meter.pct}%` }),
      ),
      el(
        "span",
        { class: `azure-target-fig${isOver(rule) ? " azure-fig-over" : ""}` },
        ...(rule.status === "off" || !now
          ? [el("span", {}, rule.status === "off" ? "off" : "n/a")]
          : [now, el("span", {}, ` / ${limit}`)]),
      ),
      chevronIcon(),
    );
    main.addEventListener("click", () => {
      if (state.expanded.has(rule.id)) state.expanded.delete(rule.id);
      else state.expanded.add(rule.id);
      drawRows();
    });
    const detail = el(
      "div",
      { class: "azure-target-detail" },
      el("p", { class: "azure-target-line meta" }, ruleLine(rule)),
      line ? el("p", { class: "azure-target-figure" }, line) : null,
      rule.alert
        ? el("p", { class: "azure-target-alert meta" }, `Task filed in ${rule.alert.list ?? "the task app"}.`)
        : null,
      el(
        "div",
        { class: "azure-target-actions" },
        toggle,
        button("Edit", { onClick: () => startEdit(draftOf(rule)) }),
        confirming
          ? el(
              "span",
              { class: "azure-confirm", role: "group", "aria-label": `Delete ${rule.name}` },
              el("span", { class: "meta" }, "Delete this target?"),
              button("Delete", {
                kind: "danger",
                onClick: async () => {
                  state.confirming = null;
                  state.rowError = await act(() => deleteCostTarget(context, rule.id));
                  drawStatus();
                },
              }),
              button("Keep", {
                onClick: () => {
                  state.confirming = null;
                  drawRows();
                },
              }),
            )
          : button("Delete", {
              title: "Remove this target",
              onClick: () => {
                state.confirming = rule.id;
                drawRows();
              },
            }),
      ),
    );
    return el(
      "li",
      {
        class: `azure-target azure-target-${rule.status === "cannot-evaluate" ? "cannot" : rule.status}${open ? " is-open" : ""}`,
      },
      main,
      open ? detail : null,
    );
  }

  function drawRows(): void {
    keepFocus(drawRowsNow);
  }

  function drawRowsNow(): void {
    const answer = state.answer;
    if (!answer) {
      replaceChildren(rows as HTMLElement, []);
      return;
    }
    if (!answer.rules.length) {
      replaceChildren(rows as HTMLElement, [el("li", { class: "empty azure-targets-empty" }, EMPTY_TEXT)]);
      return;
    }
    const shared = sharedReason(answer.rules);
    replaceChildren(
      rows as HTMLElement,
      sortedRules(answer.rules).map((rule) => rowOf(rule, shared)),
    );
  }

  function drawForm(): void {
    const draft = state.draft;
    const scopes = state.answer?.scopes;
    if (!draft || !scopes) {
      replaceChildren(formBox as HTMLElement, []);
      return;
    }
    const name = el("input", {
      type: "text",
      maxlength: "80",
      autocomplete: "off",
      placeholder: "Left blank, the target names itself",
      value: draft.name,
    });
    name.addEventListener("input", () => {
      draft.name = name.value;
    });
    const measure = el("select");
    for (const item of MEASURES) {
      const option = el("option", { value: item.id }, item.label);
      option.selected = item.id === draft.measure;
      measure.appendChild(option);
    }
    measure.addEventListener("change", () => {
      state.draft = withMeasure(draft, measure.value as CostMeasure);
      drawForm();
    });
    const kind = el("select");
    for (const id of ALLOWED_KINDS[draft.measure]) {
      const option = el("option", { value: id }, KIND_LABELS[id]);
      option.selected = id === draft.kind;
      kind.appendChild(option);
    }
    kind.addEventListener("change", () => {
      state.draft = withKind(draft, kind.value as CostScopeKind);
      drawForm();
    });
    const choices = scopeChoices(draft.kind, scopes);
    let pick: HTMLElement | null = null;
    if (NAME_REQUIRED.includes(draft.kind) || NAME_OPTIONAL.includes(draft.kind)) {
      const select = el("select");
      const optional = NAME_OPTIONAL.includes(draft.kind);
      const names = choices.includes(draft.scopeName) || !draft.scopeName ? choices : [draft.scopeName, ...choices];
      if (optional) {
        const each = el("option", { value: "" }, draft.kind === "resource" ? "Each resource" : "Each capacity");
        each.selected = !draft.scopeName;
        select.appendChild(each);
      } else if (!draft.scopeName) {
        const none = el("option", { value: "" }, `Pick a ${draft.kind}`);
        none.selected = true;
        select.appendChild(none);
      }
      for (const item of names) {
        const option = el("option", { value: item }, item);
        option.selected = item === draft.scopeName;
        select.appendChild(option);
      }
      select.addEventListener("change", () => {
        draft.scopeName = select.value;
      });
      pick = field(
        draft.kind === "capacity" ? "Capacity" : draft.kind === "service" ? "Service" : draft.kind === "resource" ? "Resource" : "Subscription",
        select,
        names.length || optional ? undefined : "Nothing to pick yet. The list fills after the first reading.",
      );
    }
    const limit = el("input", {
      type: "number",
      inputmode: "decimal",
      min: "0",
      step: "any",
      value: draft.limit,
    });
    limit.addEventListener("input", () => {
      draft.limit = limit.value;
    });
    const enabled = el("input", { type: "checkbox", checked: draft.enabled });
    enabled.checked = draft.enabled;
    enabled.addEventListener("change", () => {
      draft.enabled = enabled.checked;
    });
    const hint = MEASURES.find((item) => item.id === draft.measure)?.hint;
    const save = button(draft.id ? "Save target" : "Add target", {
      kind: "primary",
      onClick: async () => {
        const input = toInput(draft);
        if (typeof input === "string") {
          state.formError = input;
          drawForm();
          return;
        }
        state.saving = true;
        const failure = await act(() => saveCostTarget(context, draft.id ?? newTargetId(), input));
        state.saving = false;
        if (failure) {
          state.formError = failure;
          drawForm();
          return;
        }
        stopEdit();
      },
    });
    replaceChildren(formBox as HTMLElement, [
      el(
        "form",
        { class: "azure-target-form", novalidate: true },
        el("h3", { class: "azure-h" }, draft.id ? "Edit target" : "New target"),
        field("Name", name),
        field("Measure", measure, hint),
        field("Applies to", kind),
        pick,
        field(isHours(draft.measure) ? "Limit (hours)" : "Limit (USD)", limit),
        el("label", { class: "azure-target-check" }, enabled, " On"),
        state.formError ? notice(state.formError, "error") : null,
        el("div", { class: "azure-target-actions" }, save, button("Cancel", { onClick: stopEdit })),
      ),
    ]);
    const form = (formBox as HTMLElement).querySelector("form");
    form?.addEventListener("submit", (event) => event.preventDefault());
  }

  const redraw = () => {
    drawHead();
    drawStatus();
    drawRows();
  };

  function load(): Promise<void> {
    if (loading) return loading;
    loading = fetchCostTargets(context)
      .then(
        (answer) => {
          if (!alive) return;
          state.answer = answer;
          state.error = null;
        },
        (error: unknown) => {
          if (!alive) return;
          if (error instanceof AeRequestError && error.code === "admin_only") {
            state.adminOnly = true;
            state.answer = null;
            state.error = null;
            return;
          }
          state.error = error ?? new Error("Request failed");
        },
      )
      .then(() => {
        if (!alive) return;
        if (state.adminOnly) root.hidden = true;
        redraw();
        // Names to pick from arrive with the first answer; a form opened before it waits.
        if (state.draft && !(formBox as HTMLElement).firstChild) drawForm();
      })
      .finally(() => {
        loading = null;
      });
    return loading;
  }

  redraw();
  return {
    refresh: () => (state.adminOnly ? Promise.resolve() : load()),
    dispose: () => {
      alive = false;
    },
  };
}
