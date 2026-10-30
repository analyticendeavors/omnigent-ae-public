// The Admin Center's Databricks section (2026-10-19): one row per SQL
// warehouse, with Start and Stop, and a spend strip that weighs what the
// workspace has used against its credit, when one is set.
//
// Why: the owner's Databricks workspace (AWS, serverless only) runs on a
// prepaid credit, and a SQL warehouse bills for every minute it is up. They wanted to see
// both on the page that already wakes the Fabric capacity, from the iPhone as
// much as the laptop, rather than signing in to the account console. The
// server reads the workspace with a service principal (`GET /v1/ae/databricks`,
// ae_omni_policies/databricks.py) and never hands the page a token.
//
// Each warehouse is a row in the Fabric section's style (fabric.ts reuses the
// same classes): a status dot, the name, a state pill and a muted meta line
// (size, auto stop, serverless). Start and Stop ask once in a step under the
// row, as Wake and Off do; Start's sentence says it bills until it stops on its
// own. The link buttons open the warehouse (on its row) and the workspace (in
// the heading) in Databricks; until the host lets a page open a databricks.com
// URL it refuses, and then the URL shows as text to copy, as in fabric.ts.
//
// The spend strip shows two bars, this month's spend and the spend since the
// credit started, each "$x of $credit". The owner believes the credit resets monthly,
// while the account console shows one credit for a year, so both are shown and
// the server's cadence only picks which leads. Billing data lags hours, so the
// strip says "as of" the newest usage row. Reading spend needs a running
// warehouse: a GET never wakes one (the server reads on its own only when the
// warehouse is already up), and only Refresh, labelled as waking it, does.
// A reading runs on the server in the background; the strip says "Reading
// spend..." until it lands.
//
// Polling: every 15 s while a warehouse is starting or stopping or spend is
// being read, else every 60 s, and on focus. A scheduled read is skipped while
// the tab is hidden. As in fabric.ts, a read that began before an action
// cannot overwrite the row (or the spend) that action answered.
//
// 2026-10-20, the dashboard redesign: the section is split over two cards
// (board.ts). The warehouse rows sit in the Compute card under the Fabric
// capacities, with the workspace link in that card's header; the spend strip
// is the Databricks credit card, a ring of the credit left by the headline
// bar beside the two bars, "as of" and Refresh (still labelled as waking the
// warehouse). activateDatabricks takes the strip's place as a second element
// for that. After every draw it hands the page its Databricks tile (the
// warehouse's state and the spend of the credit) and a credit health that
// unfolds the credit card on a phone once three quarters of it is spent. The
// header's refresh button runs the plain read, never Refresh.
//
// 2026-10-22, the breakdown: under the ring, the last 21 days' spend as the
// Azure card's sparkline (shared from azure.ts) and this month's top five
// products (system.billing.usage's billing_origin_product, with friendly
// names), from the same server reading as the totals, so it never wakes a
// warehouse either. With no spend in the rows nothing new is drawn; when the
// breakdown failed the server's note says so under the bars.

import type { ExtensionContext } from "@omnigent/extension-sdk";
import {
  type DatabricksOverview,
  type DatabricksSpend,
  type DatabricksWarehouse,
  databricksWarehouseAction,
  describeError,
  ADMIN_RECHECK_MS,
  isAdminOnly,
  fetchDatabricks,
  refreshDatabricksSpend,
} from "./api";
import { sparkline } from "./azure";
import { ring } from "./board";
import { announce, button, el, empty, notice, replaceChildren } from "./dom";
import { externalLinkIcon, type FabricTone, refreshIcon, startIcon, stopIcon } from "./icons";
import { copyText } from "./lot";
import { type Health, registerRefresh, setTile, staleTile, type Tile, worst } from "./tiles";

export const POLL_BUSY_MS = 15_000;
export const POLL_IDLE_MS = 60_000;
export const REFRESH_LABEL = "Refresh (wakes warehouse)";
/** States that change on their own within a minute or two: read them often. */
const BUSY_STATES = new Set(["STARTING", "STOPPING", "DELETING"]);

type Action = "start" | "stop";

/** How a state reads: on, off, on its way, or not known (fabric.ts's tones). */
export function toneOf(state: string): FabricTone {
  if (state === "RUNNING") return "on";
  if (state === "STOPPED" || state === "DELETED") return "off";
  if (BUSY_STATES.has(state)) return "busy";
  return "unknown";
}

/** "RUNNING" as "Running". */
export function stateText(state: string): string {
  const lower = state.replace(/_/g, " ").toLowerCase();
  return lower.charAt(0).toUpperCase() + lower.slice(1);
}

// ── words and numbers ───────────────────────────────────────────────────────

/** Dollars: `$500` when whole, else to the cent (`$12.40`). */
export function dollars(usd: number): string {
  const cents = Math.round(usd * 100);
  return cents % 100 === 0 ? `$${cents / 100}` : `$${(cents / 100).toFixed(2)}`;
}

/** A time of day in the viewer's own clock: `1:00 PM`. */
function clockTime(ms: number): string {
  return new Date(ms).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
}

/** A credit date (`2030-01-01`) as `Jan 1, 2030`, read as a calendar day. */
export function dayText(day: string): string {
  const ms = Date.parse(`${day}T00:00:00Z`);
  if (Number.isNaN(ms)) return day;
  return new Date(ms).toLocaleDateString([], {
    month: "short",
    day: "numeric",
    year: "numeric",
    timeZone: "UTC",
  });
}

/** `2:00 PM` today, else `Oct 18, 2:00 PM`, in the viewer's clock. */
export function whenText(ms: number, now: number): string {
  if (new Date(ms).toDateString() === new Date(now).toDateString()) return clockTime(ms);
  const day = new Date(ms).toLocaleDateString([], { month: "short", day: "numeric" });
  return `${day}, ${clockTime(ms)}`;
}

function busyNote(state: string): string {
  if (state === "STARTING") return "Starting. Checking every 15 s.";
  if (state === "STOPPING") return "Stopping. Checking every 15 s.";
  return "Checking every 15 s.";
}

/** The auto stop as the meta line says it, or null when unknown. */
function autoStopText(minutes: number | null): string | null {
  if (minutes == null) return null;
  return minutes === 0 ? "no auto stop" : `auto stop ${minutes} min`;
}

/** The muted line under the name: size, auto stop, serverless. */
export function metaItems(warehouse: DatabricksWarehouse): string[] {
  if (toneOf(warehouse.state) === "busy") return [busyNote(warehouse.state)];
  const items: string[] = [];
  if (warehouse.size) items.push(warehouse.size);
  const stop = autoStopText(warehouse.autoStopMins);
  if (stop) items.push(stop);
  if (warehouse.serverless) items.push("serverless");
  return items;
}

/** The confirm step's sentence: what the tap costs, said once. */
export function question(action: Action, warehouse: DatabricksWarehouse): string {
  const { name, autoStopMins } = warehouse;
  if (action === "stop") return `Stop ${name}? A query still running on it is cancelled.`;
  if (autoStopMins == null) return `Start ${name}? It bills while it runs.`;
  if (autoStopMins === 0) {
    return `Start ${name}? It has no auto stop, so it bills until someone stops it.`;
  }
  return `Start ${name}? It bills until it auto stops after ${autoStopMins} min idle.`;
}

/** Which ask-once action a warehouse offers right now, if any. */
function offered(warehouse: DatabricksWarehouse): Action | null {
  if (warehouse.state === "STOPPED") return "start";
  if (warehouse.state === "RUNNING") return "stop";
  return null;
}

/** The wait before the next read: short while anything is on its way. */
export function nextDelay(data: DatabricksOverview | null): number {
  if (!data) return POLL_IDLE_MS;
  const busy =
    data.warehouses.some((warehouse) => BUSY_STATES.has(warehouse.state)) ||
    data.spend?.status === "reading";
  return busy ? POLL_BUSY_MS : POLL_IDLE_MS;
}

// ── the spend strip's pieces ────────────────────────────────────────────────

export interface SpendBar {
  key: "month" | "total";
  label: string;
  usd: number | null;
  /** The headline: first and larger. */
  head: boolean;
}

/** True when the server has a credit set; without one nothing is drawn against it. */
export function hasCredit(spend: DatabricksSpend): spend is DatabricksSpend & {
  creditUsd: number;
  creditStart: string;
  creditEnd: string;
} {
  return spend.creditUsd != null && spend.creditStart != null && spend.creditEnd != null;
}

/**
 * The two bars, the headline first: this month's when the credit is monthly,
 * the whole credit's when it is granted once.
 */
export function spendBars(spend: DatabricksSpend): SpendBar[] {
  const month: SpendBar = { key: "month", label: "This month", usd: spend.monthUsd, head: false };
  // No credit set on the server: this month's spend is all there is to show.
  if (!hasCredit(spend)) return [{ ...month, head: true }];
  const total: SpendBar = {
    key: "total",
    label: `Credit since ${dayText(spend.creditStart)}`,
    usd: spend.totalUsd,
    head: false,
  };
  const bars = spend.cadence === "monthly" ? [month, total] : [total, month];
  bars[0].head = true;
  return bars;
}

/** "$12.40 of $500", or "not read yet of $500" before any reading. */
export function barText(usd: number | null, credit: number): string {
  return `${usd == null ? "not read yet" : dollars(usd)} of ${dollars(credit)}`;
}

/** How far a bar is filled, 0 to 100. */
export function barPercent(usd: number | null, credit: number): number {
  if (usd == null || credit <= 0) return 0;
  return Math.min(100, Math.max(0, (usd / credit) * 100));
}

/** A reading this old (48 hours) turns amber: nobody has pressed Refresh or the warehouse stayed off. */
export const STALE_READING_MS = 48 * 60 * 60 * 1000;

/** "just now", "35 min ago", "2 h ago", "3 d ago" for a reading *ms* old. */
export function readAgeText(ms: number, now: number): string {
  const minutes = Math.floor(Math.max(0, now - ms) / 60_000);
  if (minutes < 1) return "just now";
  if (minutes < 60) return `${minutes} min ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 48) return `${hours} h ago`;
  return `${Math.floor(hours / 24)} d ago`;
}

/** When the last good reading finished, in ms, or null without one. */
function readAtMs(spend: DatabricksSpend): number | null {
  const ms = spend.readAt ? Date.parse(spend.readAt) : Number.NaN;
  return Number.isNaN(ms) ? null : ms;
}

/** Whether the numbers were read 48 hours ago or more. */
export function readingIsStale(spend: DatabricksSpend | null, now: number): boolean {
  const ms = spend ? readAtMs(spend) : null;
  return ms !== null && now - ms >= STALE_READING_MS;
}

/**
 * "as of 2 h ago (billing data lags a few hours)", or null without data. The age is the
 * reading's (when it was taken); a server that sends no `readAt` gets the newest usage row's
 * time instead.
 */
export function asOfText(spend: DatabricksSpend, now: number): string | null {
  const read = readAtMs(spend);
  if (read !== null) return `as of ${readAgeText(read, now)} (billing data lags a few hours)`;
  const ms = spend.asOf ? Date.parse(spend.asOf) : Number.NaN;
  if (Number.isNaN(ms)) return null;
  return `as of ${whenText(ms, now)} (billing data lags a few hours)`;
}

/** "as of 2 h ago" for the tile note, or null before any reading. */
function asOfAge(spend: DatabricksSpend, now = Date.now()): string | null {
  const ms = readAtMs(spend);
  return ms === null ? null : `as of ${readAgeText(ms, now)}`;
}

/** The strip's one status line for a state other than a good reading, if any. */
export function spendStatusText(spend: DatabricksSpend, canAct = true): string | null {
  if (spend.status === "reading") return "Reading spend...";
  if (spend.status === "never") {
    // A non-admin has no Refresh (2026-10-22), so the line does not point at one.
    return canAct
      ? "Spend has not been read yet. Refresh wakes the warehouse to read it."
      : "Spend has not been read yet.";
  }
  if (spend.status === "no_access") {
    return (
      spend.message ??
      "The service principal cannot read system.billing: it needs USE CATALOG on system, " +
        "USE SCHEMA on system.billing and SELECT on its usage and list_prices tables."
    );
  }
  if (spend.status === "error") {
    const message = spend.message ?? "the reading failed";
    // The server's message is a sentence already; no second period.
    return `Spend could not be read: ${message}${/[.!?]$/.test(message) ? "" : "."}`;
  }
  return null;
}

/**
 * Friendly names for system.billing.usage's billing_origin_product codes (the
 * Databricks docs list them); any other code is title cased.
 */
const PRODUCT_NAMES: Record<string, string> = {
  SQL: "SQL",
  JOBS: "Jobs",
  INTERACTIVE: "Notebooks",
  NOTEBOOKS: "Notebooks",
  DLT: "Lakeflow pipelines",
  MODEL_SERVING: "Model serving",
  ALL_PURPOSE: "All-purpose compute",
  VECTOR_SEARCH: "AI Search",
  AI_FUNCTIONS: "AI Functions",
  AI_GATEWAY: "AI Gateway",
  AI_RUNTIME: "AI Runtime",
};

/** `SQL` -> "SQL", `DEFAULT_STORAGE` -> "Default Storage". */
export function productName(code: string): string {
  const known = PRODUCT_NAMES[code.toUpperCase()];
  if (known) return known;
  return code
    .toLowerCase()
    .split(/[_\s]+/)
    .filter(Boolean)
    .map((word) => word[0].toUpperCase() + word.slice(1))
    .join(" ");
}

export const TOP_PRODUCTS = 5;

/**
 * This month's biggest products with spend, at most TOP_PRODUCTS, largest
 * first; codes sharing a name (INTERACTIVE, NOTEBOOKS) are one row.
 */
export function topProducts(spend: DatabricksSpend): Array<{ name: string; usd: number }> {
  const byName = new Map<string, number>();
  for (const row of spend.products ?? []) {
    if (row.usd <= 0) continue;
    const name = productName(row.product);
    byName.set(name, (byName.get(name) ?? 0) + row.usd);
  }
  return [...byName]
    .map(([name, usd]) => ({ name, usd }))
    .sort((a, b) => b.usd - a.usd)
    .slice(0, TOP_PRODUCTS);
}

/**
 * The days to draw as the sparkline, or null when none had spend (nothing to
 * draw). A day of net credit draws at zero, inside the line's box.
 */
export function spendDays(spend: DatabricksSpend): Array<{ date: string; usd: number }> | null {
  const days = spend.days ?? [];
  if (!days.some((day) => day.usd > 0)) return null;
  return days.map((day) => ({ date: day.date, usd: Math.max(0, day.usd) }));
}

/** The headline bar's spend (spendBars), or null before any reading. */
function headlineUsd(spend: DatabricksSpend): number | null {
  return spendBars(spend)[0]?.usd ?? null;
}

/** The credit's health: a problem once spent, attention past three quarters. */
export function creditHealth(spend: DatabricksSpend | null, now = Date.now()): Health {
  if (!spend) return "idle";
  if (spend.status === "error" || spend.status === "no_access") return "attention";
  const used = headlineUsd(spend);
  if (!hasCredit(spend) || used == null || spend.creditUsd <= 0) return "idle";
  if (used >= spend.creditUsd) return "problem";
  // Amber also when the numbers are two days old, however much credit is left.
  return used >= spend.creditUsd * 0.75 || readingIsStale(spend, now) ? "attention" : "ok";
}

/** The Databricks tile: the first warehouse's state, the headline spend of the credit. */
export function databricksTile(data: DatabricksOverview | null, error: unknown): Tile {
  if (!data) {
    return {
      id: "databricks",
      value: error ? "Unknown" : "Reading...",
      note: error ? "could not be read" : "",
      health: error ? "attention" : "idle",
      dot: true,
    };
  }
  if (!data.configured) {
    return { id: "databricks", value: "Not set up", note: "", health: "idle", dot: true };
  }
  const first = data.warehouses[0];
  const spend = data.spend;
  const used = spend ? headlineUsd(spend) : null;
  const age = spend ? asOfAge(spend) : null;
  const spent = (usd: number | null) => (usd == null ? "spend not read" : dollars(usd));
  const note = spend
    ? (hasCredit(spend)
        ? `${spent(used)} of ${dollars(spend.creditUsd)} used`
        : `${spent(used)} this month`) + (age ? ` · ${age}` : "")
    : "";
  const credit = creditHealth(spend);
  // A listing that failed (auth, timeout) comes back as a 200 with no
  // warehouses, so it is not "No warehouse": say so and ask for a look.
  if (data.error) {
    return {
      id: "databricks",
      value: "Unknown",
      summary: "warehouses not read",
      note: "warehouses could not be read",
      health: worst(["attention", credit]),
      dot: true,
    };
  }
  if (!first) {
    return { id: "databricks", value: "No warehouse", note, health: credit, dot: true };
  }
  const tone = toneOf(first.state);
  const warehouse: Health = tone === "on" ? "ok" : tone === "busy" ? "attention" : "idle";
  return {
    id: "databricks",
    value:
      data.warehouses.length > 1 ? `${first.name}: ${stateText(first.state)}` : stateText(first.state),
    summary: `warehouse ${stateText(first.state).toLowerCase()}`,
    note,
    // A credit that is merely fine does not turn a stopped warehouse green.
    health: worst([warehouse, credit === "ok" ? "idle" : credit]),
    dot: true,
  };
}

/** The credit card's folded line and health ("$100 left"), as a tile the strip does not show. */
export function creditTile(data: DatabricksOverview | null): Tile {
  const spend = data?.spend ?? null;
  const used = spend ? headlineUsd(spend) : null;
  return {
    id: "credit",
    value: spend && hasCredit(spend) && used != null ? `${dollars(Math.max(0, spend.creditUsd - used))} left` : "",
    note: "",
    health: creditHealth(spend),
  };
}

// ── the section ─────────────────────────────────────────────────────────────

interface SectionState {
  data: DatabricksOverview | null;
  error: unknown;
  /** The server answered admin_only (2026-10-28): one quiet line, and no more polls. */
  adminOnly: boolean;
  /** When admin_only was last answered, for the 5 minute look-again. */
  adminSince: number;
  refreshedAt: number | null;
  /** The confirm step that is open, per warehouse id; it survives a re-render. */
  confirming: Map<string, Action>;
  /** The last failed action's sentence, per warehouse id, until the next try. */
  failed: Map<string, string>;
  /** A link the host would not open, per warehouse id ("host" for the workspace), to copy. */
  linkNote: Map<string, { lead: string; url: string }>;
  /** Warehouses with a call on its way; the card is kept as it is until it answers. */
  inFlight: Map<string, HTMLElement | null>;
  /** Per warehouse id, when its latest action started or ended (see `clock`). */
  lastAction: Map<string, number>;
  /** When the latest Refresh started or ended (see `clock`). */
  spendAction: number;
  /** A Refresh call on its way. */
  spendInFlight: boolean;
  /** The last failed Refresh's sentence, until the next try. */
  spendFailed: string | null;
  /** A counter, not a time: orders reads against actions without clock skew. */
  clock: number;
}

/**
 * The answer of a read that began at tick `begin`, keeping the current row of
 * any warehouse (and the current spend) whose action started or ended after
 * that: the read predates the action and would put the old state back.
 */
export function freshOverview(
  fresh: DatabricksOverview,
  current: DatabricksOverview | null,
  begin: number,
  lastAction: ReadonlyMap<string, number>,
  spendAction: number,
): DatabricksOverview {
  const stale = (id: string) => (lastAction.get(id) ?? 0) > begin;
  const warehouses = fresh.warehouses.flatMap((row) => {
    if (!stale(row.id)) return [row];
    const kept = current?.warehouses.find((item) => item.id === row.id);
    return kept ? [kept] : [];
  });
  const spend = spendAction > begin && current?.spend ? current.spend : fresh.spend;
  return { ...fresh, warehouses, spend };
}

interface Section {
  context: ExtensionContext;
  state: SectionState;
  status: HTMLElement;
  list: HTMLElement;
  strip: HTMLElement;
  /** The header's slot for the workspace link. */
  host: HTMLElement;
  refreshed: HTMLElement;
  render: () => void;
  schedule: (delay: number) => void;
  /** After an action answers: the next read in *delay*, or right after the one that is out. */
  afterAction: (delay: number) => void;
}

/** Tag a control so a redraw can put the focus back on its successor. */
function keyed<T extends HTMLElement>(node: T, key: string): T {
  node.dataset.focusKey = key;
  return node;
}

/** A 32 px icon-only button (44 px on a touch screen) named by *label*. */
function iconButton(
  label: string,
  icon: SVGElement,
  onClick: (self: HTMLButtonElement) => void | Promise<void>,
  extraClass = "",
): HTMLButtonElement {
  const node = button("", { title: label, onClick });
  node.classList.add("btn-icon");
  if (extraClass) node.classList.add(extraClass);
  node.setAttribute("aria-label", label);
  node.replaceChildren(icon);
  return node;
}

/** Ask the host to open *url*; a refusal shows the URL under *key*'s card to copy. */
async function openLink(section: Section, key: string, url: string): Promise<void> {
  section.state.linkNote.delete(key);
  try {
    await section.context.navigation.openExternal(url);
  } catch (error) {
    section.state.linkNote.set(key, {
      lead: `Could not open Databricks from here (${describeError(error)}). Tap and hold to copy:`,
      url,
    });
  }
  section.render();
}

function linkNotice(text: { lead: string; url: string }): HTMLElement {
  const line = notice(`${text.lead} `, "info");
  line.classList.add("fabric-link-note");
  line.appendChild(el("code", { class: "link-copy" }, text.url));
  const copy = button("Copy", {
    title: "Copy the link",
    onClick: async (self) => {
      self.textContent = (await copyText(text.url)) ? "Copied" : "Copy failed";
    },
  });
  line.append(" ", copy);
  return line;
}

/** Put an action's answer (the warehouse's row) on its card. */
function setRow(section: Section, row: DatabricksWarehouse): void {
  const warehouses = section.state.data?.warehouses;
  if (!warehouses) return;
  const at = warehouses.findIndex((item) => item.id === row.id);
  if (at >= 0) warehouses[at] = row;
}

function confirmPanel(
  section: Section,
  warehouse: DatabricksWarehouse,
  action: Action,
): HTMLElement {
  const { id, name } = warehouse;
  const { state } = section;
  const box = el("div", {
    class: "fabric-panel fabric-confirm",
    role: "group",
    "aria-label": action === "start" ? `Confirm starting ${name}` : `Confirm stopping ${name}`,
  });
  const busyLabel = action === "start" ? "Starting..." : "Stopping...";
  const idleLabel = action === "start" ? "Start now" : "Stop now";
  // A re-render while the call is out builds this row again: born busy.
  const pending = state.inFlight.has(id);
  const cancel = keyed(
    button("Cancel", {
      disabled: pending,
      onClick: () => {
        state.confirming.delete(id);
        section.render();
      },
    }),
    `dbx:${id}:cancel`,
  );
  const go = keyed(
    button(pending ? busyLabel : idleLabel, {
      kind: action === "start" ? "primary" : "danger",
      disabled: pending,
      onClick: async (self) => {
        if (state.inFlight.has(id)) return;
        state.inFlight.set(id, box.closest<HTMLElement>("article"));
        state.lastAction.set(id, ++state.clock);
        cancel.disabled = true;
        self.textContent = busyLabel;
        state.failed.delete(id);
        try {
          const row = await databricksWarehouseAction(section.context, id, action);
          state.lastAction.set(id, ++state.clock);
          state.inFlight.delete(id);
          state.confirming.delete(id);
          setRow(section, row);
          announce(`${action === "start" ? "Starting" : "Stopping"} ${name}.`, "ok");
          section.render();
          section.afterAction(POLL_BUSY_MS);
        } catch (error) {
          state.lastAction.set(id, ++state.clock);
          state.inFlight.delete(id);
          state.confirming.delete(id);
          state.failed.set(
            id,
            `${action === "start" ? "Start" : "Stop"} failed: ${describeError(error)}`,
          );
          section.render();
        }
      },
    }),
    `dbx:${id}:go`,
  );
  go.classList.add("btn-tint");
  box.appendChild(
    el(
      "div",
      { class: "fabric-confirm-row" },
      el("p", { class: "fabric-confirm-text" }, question(action, warehouse)),
      cancel,
      go,
    ),
  );
  return box;
}

function actionButtons(section: Section, warehouse: DatabricksWarehouse): HTMLElement[] {
  const { id, name } = warehouse;
  const { state } = section;
  const nodes: HTMLElement[] = [];
  if (warehouse.url) {
    nodes.push(
      keyed(
        iconButton(`Open ${name} in Databricks`, externalLinkIcon(), () =>
          openLink(section, id, warehouse.url),
        ),
        `dbx:${id}:open`,
      ),
    );
  }
  // A non-admin sees the state and the link, never Start or Stop (2026-10-22).
  if (!state.data?.can_act) return nodes;
  const action = offered(warehouse);
  const openStep = state.confirming.get(id);
  const toggle = (which: Action) => () => {
    if (state.confirming.get(id) === which) state.confirming.delete(id);
    else state.confirming.set(id, which);
    section.render();
  };
  if (action === "stop") {
    const stop = iconButton(`Stop ${name}`, stopIcon(), toggle("stop"), "fabric-off-btn");
    stop.setAttribute("aria-expanded", String(openStep === "stop"));
    nodes.push(keyed(stop, `dbx:${id}:stop`));
  } else if (action === "start") {
    const start = button("Start", {
      title: `Start ${name} (asks to confirm first)`,
      onClick: toggle("start"),
    });
    start.classList.add("btn-tint", "fabric-wake");
    start.prepend(startIcon());
    start.setAttribute("aria-expanded", String(openStep === "start"));
    nodes.push(keyed(start, `dbx:${id}:start`));
  }
  return nodes;
}

function warehouseCard(section: Section, warehouse: DatabricksWarehouse): HTMLElement {
  const { state } = section;
  const held = state.inFlight.get(warehouse.id);
  if (held) return held;
  const { id, name } = warehouse;
  // A step the state no longer offers, or a caller who may not act, is dropped.
  if (state.confirming.get(id) !== offered(warehouse) || !state.data?.can_act) {
    state.confirming.delete(id);
  }
  const meta = metaItems(warehouse);
  const node = el(
    "article",
    {
      class: `card card-fabric card-dbx fabric-${toneOf(warehouse.state)}`,
      "data-warehouse": id,
    },
    el(
      "div",
      { class: meta.length ? "fabric-row" : "fabric-row fabric-row-solo" },
      el("span", { class: "fabric-dot", "aria-hidden": "true" }),
      el(
        "div",
        { class: "fabric-title" },
        el("span", { class: "fabric-name" }, name),
        el("span", { class: "fabric-state" }, stateText(warehouse.state)),
      ),
      el("div", { class: "fabric-acts" }, ...actionButtons(section, warehouse)),
      meta.length
        ? el(
            "p",
            { class: "fabric-meta" },
            ...meta.map((item) => el("span", { class: "fabric-meta-item" }, item)),
          )
        : null,
    ),
  );
  const failed = state.failed.get(id);
  if (failed) node.appendChild(notice(failed, "error"));
  const note = state.linkNote.get(id);
  if (note) node.appendChild(linkNotice(note));
  const open = state.confirming.get(id);
  if (open) node.appendChild(confirmPanel(section, warehouse, open));
  return node;
}

function barNode(bar: SpendBar, credit: number): HTMLElement {
  const text = barText(bar.usd, credit);
  const percent = barPercent(bar.usd, credit);
  const fill = el("div", { class: "dbx-fill" });
  fill.style.width = `${percent}%`;
  const tone = bar.usd != null && bar.usd >= credit ? " dbx-bar-over" : "";
  return el(
    "div",
    { class: `dbx-bar${bar.head ? " dbx-bar-head" : ""}${tone}`, "data-bar": bar.key },
    el(
      "div",
      { class: "dbx-bar-top" },
      el("span", { class: "dbx-bar-label" }, bar.label),
      el("span", { class: "dbx-bar-value" }, text),
    ),
    el(
      "div",
      {
        class: "dbx-track",
        role: "meter",
        "aria-label": `${bar.label}: ${text}`,
        "aria-valuemin": "0",
        "aria-valuemax": String(credit),
        "aria-valuenow": String(bar.usd ?? 0),
      },
      fill,
    ),
  );
}

function refreshButton(section: Section, spend: DatabricksSpend): HTMLButtonElement {
  const { state } = section;
  const runsOn = state.data?.warehouses.find((item) => item.id === spend.warehouseId);
  const node = button(REFRESH_LABEL, {
    title: runsOn
      ? `Read spend now on ${runsOn.name}; starts it if it is stopped`
      : "Read spend now; starts the warehouse if it is stopped",
    disabled: state.spendInFlight || spend.status === "reading",
    onClick: async () => {
      if (state.spendInFlight) return;
      state.spendInFlight = true;
      state.spendAction = ++state.clock;
      state.spendFailed = null;
      try {
        const next = await refreshDatabricksSpend(section.context);
        state.spendAction = ++state.clock;
        state.spendInFlight = false;
        if (state.data) state.data.spend = next;
        section.render();
        section.afterAction(POLL_BUSY_MS);
      } catch (error) {
        state.spendAction = ++state.clock;
        state.spendInFlight = false;
        state.spendFailed = `Refresh failed: ${describeError(error)}`;
        section.render();
      }
    },
  });
  node.classList.add("btn-tint", "dbx-refresh");
  node.prepend(refreshIcon());
  return keyed(node, "dbx:refresh");
}

/** This month's spend as plain text, for a server with no credit set. */
function monthLine(spend: DatabricksSpend): HTMLElement {
  const usd = spend.monthUsd;
  return el("p", { class: "dbx-asof" }, `This month: ${usd == null ? "not read yet" : dollars(usd)}`);
}

/** The ring of the credit left by the headline bar, or null before any reading. */
function creditRing(spend: DatabricksSpend): HTMLElement | null {
  const used = headlineUsd(spend);
  if (!hasCredit(spend) || used == null || spend.creditUsd <= 0) return null;
  const left = Math.max(0, spend.creditUsd - used);
  const health = creditHealth(spend);
  const tone = health === "problem" ? "ring-err" : health === "attention" ? "ring-warn" : "ring-ok";
  return ring(
    left / spend.creditUsd,
    dollars(left),
    "left",
    tone,
    `${dollars(left)} of the ${dollars(spend.creditUsd)} credit left`,
  );
}

/** The sparkline and the top products under the ring, or null with no spend in them. */
function breakdownBlock(spend: DatabricksSpend): HTMLElement | null {
  const days = spendDays(spend);
  const products = topProducts(spend);
  if (!days && products.length === 0) return null;
  return el(
    "div",
    { class: "dbx-breakdown" },
    days
      ? sparkline(
          days.map((day) => day.usd),
          (top) => `Daily spend for the last ${days.length} days, highest ${dollars(top)}`,
        )
      : null,
    days
      ? el(
          "p",
          { class: "azure-caption meta" },
          `Daily spend, last ${days.length} days. Billing data lags a few hours.`,
        )
      : null,
    products.length
      ? el(
          "div",
          { class: "azure-services dbx-products" },
          el("h3", { class: "azure-h" }, "Biggest by product this month"),
          el(
            "ul",
            {},
            ...products.map((row) =>
              el(
                "li",
                {},
                el("span", {}, row.name),
                el("span", { class: "azure-amt" }, dollars(row.usd)),
              ),
            ),
          ),
        )
      : null,
  );
}

function spendStrip(section: Section, spend: DatabricksSpend, now: number): HTMLElement {
  const { state } = section;
  const quiet = spend.status === "reading";
  const status = spendStatusText(spend, state.data?.can_act === true);
  const hasNumbers = spend.monthUsd != null || spend.totalUsd != null;
  const asOf = asOfText(spend, now);
  const asOfClass = readingIsStale(spend, now) ? "dbx-asof dbx-asof-stale" : "dbx-asof";
  const node = el(
    "div",
    { class: "card card-fabric dbx-spend", role: "group", "aria-labelledby": "dbx-spend-h" },
    el(
      "div",
      { class: "dbx-spend-top" },
      el("span", { class: "fabric-name", id: "dbx-spend-h" }, "Spend"),
      state.data?.can_act ? refreshButton(section, spend) : null,
    ),
    hasNumbers || spend.status === "ok"
      ? el(
          "div",
          { class: "dbx-spend-main" },
          creditRing(spend),
          el(
            "div",
            { class: "dbx-bars" },
            ...(hasCredit(spend)
              ? spendBars(spend).map((bar) => barNode(bar, spend.creditUsd))
              : [monthLine(spend)]),
            asOf ? el("p", { class: asOfClass }, asOf) : null,
          ),
        )
      : asOf
        ? el("p", { class: asOfClass }, asOf)
        : null,
    hasNumbers ? breakdownBlock(spend) : null,
    spend.note ? el("p", { class: "dbx-asof dbx-note" }, spend.note) : null,
  );
  if (status) {
    if (quiet) node.appendChild(el("p", { class: "dbx-asof dbx-reading" }, status));
    else if (spend.status === "never") node.appendChild(el("p", { class: "dbx-asof" }, status));
    else node.appendChild(notice(status, spend.status === "error" ? "error" : "info"));
  }
  if (state.spendFailed) node.appendChild(notice(state.spendFailed, "error"));
  if (hasCredit(spend)) {
    const start = dayText(spend.creditStart);
    const end = dayText(spend.creditEnd);
    node.title = `The ${dollars(spend.creditUsd)} credit runs ${start} to ${end}.`;
  }
  return node;
}

function render(section: Section): void {
  const { state } = section;
  // The rows and the strip live in two cards: the focus is looked for in both.
  const wraps = [
    section.list.closest<HTMLElement>(".admin-block, .admin-card") ?? section.list,
    section.strip,
  ];
  const active = document.activeElement as HTMLElement | null;
  const inside = (node: Element | null) => wraps.some((wrap) => wrap.contains(node));
  const focusKey = inside(active) ? active?.dataset.focusKey : undefined;
  const data = state.data;
  const now = Date.now();
  const hostNote = state.linkNote.get("host");
  if (state.adminOnly) {
    replaceChildren(section.status, []);
    replaceChildren(section.list, [el("p", { class: "meta" }, "Databricks is for admins.")]);
    replaceChildren(section.strip, []);
    return;
  }
  replaceChildren(section.status, [
    hostNote ? linkNotice(hostNote) : null,
    state.error
      ? notice(
          data
            ? `Showing the last answer; the refresh failed: ${describeError(state.error)}`
            : `Databricks could not be read: ${describeError(state.error)}`,
          "error",
        )
      : null,
    data?.error ? notice(`Warehouses could not be read: ${data.error.message}`, "error") : null,
  ]);
  if (!data) {
    replaceChildren(section.list, [state.error ? null : el("p", { class: "meta" }, "Reading...")]);
    replaceChildren(section.strip, []);
  } else if (!data.configured) {
    replaceChildren(section.list, [empty(data.note ?? "Databricks is not configured.")]);
    replaceChildren(section.strip, []);
  } else {
    replaceChildren(
      section.list,
      data.warehouses.length === 0
        ? [data.error ? null : empty("No SQL warehouse in this workspace.")]
        : data.warehouses.map((warehouse) => warehouseCard(section, warehouse)),
    );
    replaceChildren(section.strip, [data.spend ? spendStrip(section, data.spend, now) : null]);
  }
  if (focusKey && !inside(document.activeElement)) {
    const next = wraps
      .flatMap((wrap) => [...wrap.querySelectorAll<HTMLElement>("[data-focus-key]")])
      .find((node) => node.dataset.focusKey === focusKey);
    next?.focus({ preventScroll: true });
  }
  replaceChildren(section.host, [
    data?.host
      ? keyed(
          iconButton("Open the workspace in Databricks", externalLinkIcon(), () =>
            openLink(section, "host", data.host ?? ""),
          ),
          "dbx:host",
        )
      : null,
  ]);
  if (state.refreshedAt === null) {
    section.refreshed.textContent = "";
    section.refreshed.removeAttribute("title");
  } else {
    section.refreshed.textContent = `Updated ${clockTime(state.refreshedAt)}`;
    section.refreshed.title = `Again in ${nextDelay(data) / 1000} s or on focus.`;
  }
  setTile(creditTile(data));
  const tile = databricksTile(data, state.error);
  setTile(
    data ? staleTile(tile, state.error) : tile,
    state.error ? undefined : (state.refreshedAt ?? undefined),
  );
}

/**
 * The warehouses' block, for activateFabric to put in the Compute card under
 * the Fabric capacities, under its own "Databricks" heading that carries the
 * workspace link; activateDatabricks fills it.
 */
export function databricksSection(): HTMLElement {
  return el(
    "div",
    { class: "admin-block admin-databricks" },
    // Its own heading, so the workspace link sits with Databricks, not on the Compute card.
    el("div", { class: "dbx-head" }, el("span", { class: "dbx-name" }, "Databricks"), el("span", { class: "dbx-host" })),
    el("p", { class: "meta refreshed dbx-refreshed" }),
    el("div", { class: "page-status dbx-status", "aria-live": "polite" }),
    // An id, as on the Fabric list, so a redraw can find a card.
    el("section", {
      class: "cards fabric-list",
      id: "databricks-warehouses",
      "aria-label": "Databricks warehouses",
    }),
  );
}

/** The spend strip's place, the Databricks credit card's body. */
export function databricksStrip(): HTMLElement {
  return el("div", { class: "dbx-strip" });
}

let timer: ReturnType<typeof setTimeout> | null = null;
let onFocus: (() => void) | null = null;
/**
 * Bumped by every activate and deactivate (review fix, 2026-10-19): a read or
 * an action still out from an earlier activation finds its number stale, so it
 * neither draws into that activation's detached section nor touches the timer,
 * which belongs to the current one.
 */
let generation = 0;

/**
 * Read and draw the section *root* (from databricksSection, or the card
 * holding it) and the spend strip *stripPlace* (from databricksStrip) and keep
 * them fresh.
 */
export function activateDatabricks(
  context: ExtensionContext,
  root: HTMLElement,
  stripPlace?: HTMLElement,
): void {
  const status = root.querySelector<HTMLElement>(".dbx-status");
  const list = root.querySelector<HTMLElement>("#databricks-warehouses");
  const strip = stripPlace ?? root.querySelector<HTMLElement>(".dbx-strip");
  const host = root.querySelector<HTMLElement>(".dbx-host");
  // In the Compute card the header's time is Fabric's, so this one may be gone.
  const refreshed = root.querySelector<HTMLElement>(".dbx-refreshed") ?? el("p");
  if (!status || !list || !strip || !host || !refreshed) {
    throw new Error("Databricks section is missing");
  }
  const mine = ++generation;
  const live = () => generation === mine;
  if (onFocus) {
    window.removeEventListener("focus", onFocus);
    document.removeEventListener("visibilitychange", onFocus);
  }
  const state: SectionState = {
    data: null,
    error: null,
    adminOnly: false,
    adminSince: 0,
    refreshedAt: null,
    confirming: new Map(),
    failed: new Map(),
    linkNote: new Map(),
    inFlight: new Map(),
    lastAction: new Map(),
    spendAction: 0,
    spendInFlight: false,
    spendFailed: null,
    clock: 0,
  };
  let reading: Promise<void> | null = null;
  /** One more read queued behind the one that is out. */
  let followUp: number | null = null;
  const refresh = (): Promise<void> => {
    if (reading) return reading;
    if (state.adminOnly && Date.now() - state.adminSince < ADMIN_RECHECK_MS) return Promise.resolve();
    const begin = ++state.clock;
    reading = fetchDatabricks(context)
      .then(
        (data) => {
          state.data = freshOverview(
            data,
            state.data,
            begin,
            state.lastAction,
            state.spendAction,
          );
          state.error = null;
          state.adminOnly = false;
        },
        (error: unknown) => {
          if (isAdminOnly(error)) {
            state.adminOnly = true;
            state.adminSince = Date.now();
            state.data = null;
            state.error = null;
            return;
          }
          // The last good rows stay, with the failure said once above them.
          state.error = error;
        },
      )
      .then(() => {
        state.refreshedAt = Date.now();
        const queued = followUp;
        followUp = null;
        if (!live()) return;
        section.render();
        section.schedule(queued ?? nextDelay(state.data));
      })
      .finally(() => {
        reading = null;
      });
    return reading;
  };
  const section: Section = {
    context,
    state,
    status,
    list,
    strip,
    host,
    refreshed,
    render: () => {
      if (live()) render(section);
    },
    schedule: (delay) => {
      if (!live()) return;
      if (timer) clearTimeout(timer);
      timer = setTimeout(() => {
        // A hidden tab does not poll; visibilitychange reads on return.
        if (live() && document.visibilityState === "visible") void refresh();
      }, state.adminOnly ? ADMIN_RECHECK_MS : delay);
    },
    afterAction: (delay) => {
      if (reading) followUp = Math.min(followUp ?? delay, delay);
      else section.schedule(delay);
    },
  };
  section.render();
  void refresh();
  registerRefresh("databricks", () => (live() ? refresh() : undefined));
  onFocus = () => {
    if (document.visibilityState === "visible") void refresh();
  };
  window.addEventListener("focus", onFocus);
  document.addEventListener("visibilitychange", onFocus);
}

export function deactivateDatabricks(): void {
  generation++;
  if (timer) clearTimeout(timer);
  timer = null;
  if (onFocus) {
    window.removeEventListener("focus", onFocus);
    document.removeEventListener("visibilitychange", onFocus);
  }
  onFocus = null;
}
