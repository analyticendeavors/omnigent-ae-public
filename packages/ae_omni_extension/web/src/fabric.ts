// The Admin Center's Fabric section (2026-10-16, renamed 2026-10-18): one card
// per Fabric capacity, with Wake, Off and Keep awake.
//
// Why: Reid wanted the aetraining wake button inside omnigent-ae, next to the
// Dashboard, with room for more capacity buttons; Off was added at his
// request, guarded by a separate suspend key so holders of the shared wake
// link can only wake. The page is the extension's `fabric` view, reached from
// its one nav row (plugin.py); the server reads each capacity through its
// Logic App (`GET /v1/ae/fabric`, ae_omni_policies/fabric.py) and never hands
// the page a URL or a key.
//
// Second pass the same day (Reid approved the mockup): one compact row per
// capacity. A status dot, the name and a small state pill; a muted meta line
// (SKU, region, price an hour, how long it has been up and what that cost,
// when it sleeps if idle); the actions at the right as 32 px icon buttons
// (40 px on a touch screen) and Wake as a small labelled button. The links
// (Azure portal, Fabric's capacity settings) sit behind one icon button that
// opens a small menu. Every detail is optional: the live Logic App does not
// send them yet, and the row draws without them.
//
// Start and Pause ask once in a step that opens under the row, like the pull
// request card's Merge (prs.ts): the sentence says what it costs, then Cancel
// and the button that acts. Keep awake opens a chooser there instead (2, 4 or
// 8 hours) whose sentence says how long that keeps it up and what it adds; it
// never claims to shorten anything. A failed call keeps its sentence on that
// card until the next try.
//
// Polling: every 15 s while any capacity is in a busy state (Waking, Resuming,
// Suspending, Pausing and ARM's own in-progress words), else every 60 s, and on
// focus. A scheduled read is skipped while the tab is hidden; coming back
// reads at once. A read that began before a Wake, Off or Keep awake cannot
// overwrite that card: its row is dropped and the card keeps the answer the
// action gave. After a wake or a suspend the card shows the state the Logic
// App answered (Waking, Suspending) and the next read comes 15 s later, not at
// once: ARM can still report Paused for a few seconds after a resume starts,
// which would put Wake back under Reid's thumb.
//
// Third pass (2026-10-16, review fixes): the page holds no copy of the
// runbook's rule. The server sends each keep awake choice's new sleep time
// and whether it adds any (keepAwakeOptions) and what a wake bills
// (wakeBilledHours); the page only formats them. Every action answers the
// capacity's full row, so the card redraws from it at once; when a read is
// still out as the action answers, its row for that card is dropped and one
// more read follows it. The links menu is cards.ts rowMenu (its viewport flip
// and redraw handling), and the open card is lifted over the next one. On a
// touch screen every action is 44 px with 8 px between neighbours.
//
// 2026-10-19: the page holds a second section under this one, Databricks
// (databricks.ts), mounted and started here and stopped with this one. It
// borrows these card classes and polls on its own clock.
//
// 2026-10-19, later: a third section, Deploys (deploys.ts), goes first under
// the page's heading, above this one: the release the box runs and a hold
// that pauses its automatic deploys for 1, 2 or 3 hours. Mounted, started and
// stopped here like Databricks; it polls on its own clock too. The page now
// reads Deploys, Fabric capacity, Databricks.
//
// 2026-10-20: a fourth, Box host (host.ts), between Deploys and Fabric
// capacity: whether the box host is connected, and a button that sends a
// reconnect link when the server rejects it. Mounted the same way. The page
// now reads Deploys, Box host, Fabric capacity, Databricks.
//
// 2026-10-20, later: a fifth, Shared Claude accounts (sharedAccounts.ts),
// after Box host: the company Claude logins every user's picker lists, which
// an admin adds, relabels and removes here. Mounted the same way.
//
// 2026-10-20, later: a sixth, Secrets (secrets.ts), last: each key the server
// expects from deploy/pull-secrets.sh with whether it has it, names only.
// Mounted the same way; it reads on activate and focus, with no timer. The
// page now reads Deploys, Box host, Shared Claude accounts, Fabric capacity,
// Databricks, Secrets.
//
// 2026-10-20, the dashboard redesign (Reid approved concept C): the page
// drops its 720 px column and uses the pane's width. Under a header (the
// title, when the newest answer arrived, and one refresh button that runs
// every section's normal read) sits a strip of status tiles (tiles.ts), then
// four cards in fixed column stacks (board.ts): Deploys, Compute (these
// capacity rows, then the Databricks warehouses), Databricks credit (the
// spend strip) and Box health (the host, then the secrets). The capacity rows
// keep every action, confirm step, chooser and menu; an Active capacity whose
// resume and sleep times are known gets a thin bar of its up time toward its
// sleep. The Shared Claude accounts section is gone: their management moves
// to OmniGent's Settings, and the page keeps only their count, as a tile.
// This module builds the page, mounts and stops every section, and hands the
// page its Fabric tile after every draw.
//
// 2026-10-21: a fifth card, Accounts (accounts.ts), at the head of the middle
// column: every signed-in Claude login and the Codex login with its 5-hour
// and weekly use. Its tile replaces the shared-accounts count, so this module
// no longer reads that list.
//
// 2026-10-21 (the capacity fleet): with the shared capacities Logic App set up,
// the server lists every Fabric capacity Reid has, not only the ones with a
// wake Logic App, so the Compute card draws one row per capacity it returns,
// never a fixed count. Each row's SKU in the meta line becomes a size picker
// while the capacity is Active or Paused; picking a size opens the same ask
// once step as Start and Pause, naming both sizes and both prices an hour
// ("Resize x from F4 ($0.72/h) to F8 ($1.44/h)? Billing changes at once."),
// and only its Resize button sends the call. Start and Pause go through the fleet
// on the server for every row; keep awake, the sleep time and the links stay
// on the rows that have them. A list the server could only partly read says so
// above the rows. Reid asked for Pause and Start, so the buttons, their
// confirm sentences and their toasts say so (the routes keep wake and suspend).

import type { Disposable, ExtensionContext } from "@omnigent/extension-sdk";
import "./style.css";
import {
  describeError,
  type FabricActionResult,
  type FabricCapacity,
  type FabricOverview,
  fabricAction,
  fabricKeepAwake,
  fabricScale,
  fetchFabric,
} from "./api";
import { accountsSection, activateAccounts, deactivateAccounts } from "./accounts";
import { adminCard, mountBoard } from "./board";
import { holdMenus, rowMenu } from "./cards";
import {
  activateDatabricks,
  databricksSection,
  databricksStrip,
  deactivateDatabricks,
} from "./databricks";
import { activateDeploys, deactivateDeploys, deploysSection } from "./deploys";
import { announce, button, el, empty, notice, replaceChildren } from "./dom";
import { activateHost, deactivateHost, hostSection } from "./host";
import {
  boltIcon,
  clockIcon,
  coinIcon,
  type FabricTone,
  linksIcon,
  powerIcon,
  refreshIcon,
  sharedIcon,
  shieldCheckIcon,
} from "./icons";
import { copyText } from "./lot";
import { activateSecrets, deactivateSecrets, secretsSection } from "./secrets";
import { loadSectionState } from "./sections";
import {
  type Health,
  mountTiles,
  refreshAll,
  registerRefresh,
  resetTiles,
  setTile,
  staleTile,
  type Tile,
  worst,
} from "./tiles";

export const POLL_BUSY_MS = 15_000;
export const POLL_IDLE_MS = 60_000;
export const FABRIC_CAPACITIES_URL = "https://app.fabric.microsoft.com/admin-portal/capacities";
const HOUR_MS = 3_600_000;
/** States that change on their own within minutes: read them often. */
const BUSY_STATES = new Set([
  "Waking",
  "Resuming",
  "Suspending",
  "Pausing",
  // ARM's in-progress provisioning states, shown the same way (2026-10-16).
  "Provisioning",
  "Updating",
  "Scaling",
  "Preparing",
  "Deleting",
]);
const FAILED_STATES = new Set(["WakeFailed", "SuspendFailed", "Failed"]);

type Action = "wake" | "off";

/** How a state reads: on, off, on its way, failed, or not known. */
export function toneOf(state: string): FabricTone {
  if (state === "Active") return "on";
  if (state === "Paused") return "off";
  if (BUSY_STATES.has(state)) return "busy";
  if (FAILED_STATES.has(state)) return "failed";
  return "unknown";
}

/** "WakeFailed" as "Wake failed": ARM's and the Logic App's words, spaced. */
export function stateText(state: string): string {
  const spaced = state.replace(/([a-z])([A-Z])/g, "$1 $2");
  return spaced.charAt(0) + spaced.slice(1).toLowerCase();
}

// ── the meta line ───────────────────────────────────────────────────────────

/** Dollars to the cent: `$1.60`. */
export function money(usd: number): string {
  return `$${(Math.round(usd * 100) / 100).toFixed(2)}`;
}

/** An ISO time as epoch milliseconds, or null. */
function instant(value: string | null | undefined): number | null {
  if (!value) return null;
  const ms = Date.parse(value);
  return Number.isNaN(ms) ? null : ms;
}

/** A time of day in the viewer's own clock: `1:00 AM`. */
export function clockTime(ms: number): string {
  return new Date(ms).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
}

/** `2 h 14 m`, or `14 m` under an hour. */
export function duration(ms: number): string {
  const minutes = Math.max(0, Math.floor(ms / 60_000));
  const hours = Math.floor(minutes / 60);
  return hours ? `${hours} h ${minutes % 60} m` : `${minutes} m`;
}

// Region words ARM runs together in its lowercase form, longest first so
// "southeast" wins over "south". An unknown piece leaves the name as given.
const REGION_WORDS = [
  "switzerland",
  "netherlands",
  "australia",
  "indonesia",
  "southeast",
  "northeast",
  "southwest",
  "northwest",
  "malaysia",
  "zealand",
  "germany",
  "belgium",
  "central",
  "denmark",
  "austria",
  "pacific",
  "europe",
  "brazil",
  "canada",
  "france",
  "israel",
  "mexico",
  "norway",
  "poland",
  "sweden",
  "taiwan",
  "africa",
  "japan",
  "india",
  "italy",
  "korea",
  "north",
  "qatar",
  "south",
  "spain",
  "chile",
  "asia",
  "east",
  "west",
  "new",
  "jio",
  "uae",
  "uk",
  "us",
];
const REGION_CAPS = new Set(["us", "uk", "uae"]);

/** "northcentralus" as "North Central US"; anything else as ARM wrote it. */
export function regionText(region: string | null | undefined): string | null {
  if (!region) return null;
  if (!/^[a-z0-9]+$/.test(region)) return region;
  const words: string[] = [];
  let at = 0;
  while (at < region.length) {
    const digits = /^\d+/.exec(region.slice(at));
    if (digits) {
      words.push(digits[0]);
      at += digits[0].length;
      continue;
    }
    const word = REGION_WORDS.find((item) => region.startsWith(item, at));
    if (!word) return region;
    words.push(REGION_CAPS.has(word) ? word.toUpperCase() : word[0].toUpperCase() + word.slice(1));
    at += word.length;
  }
  return words.join(" ");
}

/** What a wake costs at least, or null when the price or the hours are unknown. */
function wakeCost(capacity: FabricCapacity): string | null {
  const hours = capacity.wakeBilledHours;
  if (hours == null) return null;
  if (capacity.hourlyUsd != null) return money(hours * capacity.hourlyUsd);
  // The first pass's figure for aetraining's F4, kept until the Logic App sends a price.
  return hours === 7 ? "$5" : null;
}

/** "up 2 h 14 m, about $1.60" for an Active capacity whose resume time is known. */
export function uptimeText(capacity: FabricCapacity, now: number): string | null {
  const resumed = instant(capacity.resumedAt);
  if (capacity.state !== "Active" || resumed === null || resumed > now + 60_000) return null;
  const up = Math.max(0, now - resumed);
  const cost =
    capacity.hourlyUsd != null ? `, about ${money((up / HOUR_MS) * capacity.hourlyUsd)}` : "";
  return `up ${duration(up)}${cost}`;
}

/** What an Active capacity has cost since it woke, or null when that is not known. */
export function costSoFar(capacity: FabricCapacity, now: number): number | null {
  const resumed = instant(capacity.resumedAt);
  if (capacity.state !== "Active" || resumed === null || capacity.hourlyUsd == null) return null;
  return (Math.max(0, now - resumed) / HOUR_MS) * capacity.hourlyUsd;
}

/** How far an Active capacity is from its wake to its sleep, 0 to 100, or null. */
export function upPercent(capacity: FabricCapacity, now: number): number | null {
  const resumed = instant(capacity.resumedAt);
  const sleeps = instant(capacity.sleepsAt);
  if (capacity.state !== "Active" || resumed === null || sleeps === null || sleeps <= resumed) {
    return null;
  }
  return Math.min(100, Math.max(0, ((now - resumed) / (sleeps - resumed)) * 100));
}

const TONE_HEALTH: Record<FabricTone, Health> = {
  on: "ok",
  busy: "attention",
  failed: "problem",
  off: "idle",
  unknown: "idle",
};

/** The Fabric tile: the capacity's state and what it has cost so far (or a count of several). */
export function fabricTile(data: FabricOverview | null, error: unknown, now: number): Tile {
  if (!data) {
    return {
      id: "fabric",
      value: error ? "Unknown" : "Reading...",
      note: error ? "could not be read" : "",
      health: error ? "attention" : "idle",
      dot: true,
    };
  }
  const rows = data.capacities;
  if (rows.length === 0) return { id: "fabric", value: "None", note: "", health: "idle", dot: true };
  const health = worst(rows.map((row) => TONE_HEALTH[toneOf(row.state)]));
  if (rows.length > 1) {
    const on = rows.filter((row) => toneOf(row.state) === "on").length;
    const value = `${on} of ${rows.length} on`;
    return { id: "fabric", value, note: "", health, dot: true, summary: `Fabric ${value}` };
  }
  const [row] = rows;
  const cost = costSoFar(row, now);
  return {
    id: "fabric",
    value: stateText(row.state),
    summary: `Fabric ${stateText(row.state).toLowerCase()}`,
    note: cost === null ? row.name : `${row.name} \u00b7 ${money(cost)} so far`,
    health,
    dot: true,
  };
}

/** "sleeps around 1:00 AM if idle", from the server's sleepsAt. */
export function sleepText(capacity: FabricCapacity, now: number): string | null {
  const sleeps = instant(capacity.sleepsAt);
  if (capacity.state !== "Active" || sleeps === null || sleeps < now - 60_000) return null;
  return `sleeps around ${clockTime(sleeps)} if idle`;
}

function busyNote(state: string): string {
  if (state === "Waking" || state === "Resuming") {
    return "Ready in about 2 minutes. Checking every 15 s.";
  }
  if (state === "Suspending" || state === "Pausing") return "Pausing. Checking every 15 s.";
  return "Checking every 15 s.";
}

/**
 * The muted line under the name, item by item; whatever the server knows.
 * Without *withSku* the SKU is left out (the size picker stands in for it).
 */
export function metaItems(capacity: FabricCapacity, now: number, withSku = true): string[] {
  if (toneOf(capacity.state) === "busy") return [busyNote(capacity.state)];
  const items: string[] = [];
  if (capacity.sku && withSku) items.push(capacity.sku);
  const region = regionText(capacity.region);
  if (region) items.push(region);
  if (capacity.subscriptionName) items.push(capacity.subscriptionName);
  if (capacity.hourlyUsd != null) items.push(`${money(capacity.hourlyUsd)}/h`);
  const up = uptimeText(capacity, now);
  if (up) items.push(up);
  const sleeps = sleepText(capacity, now);
  if (sleeps) items.push(sleeps);
  if (capacity.state === "Paused" && capacity.wakeBilledHours != null) {
    const cost = wakeCost(capacity);
    items.push(`Start bills at least ${capacity.wakeBilledHours} h${cost ? `, about ${cost}` : ""}`);
  }
  return items;
}

// ── the sentences that ask ──────────────────────────────────────────────────

/** The confirm step's sentence: what the tap costs, said once. */
export function question(action: Action, capacity: FabricCapacity): string {
  const { name } = capacity;
  if (action === "off") return `Pause ${name}? Anyone using it is cut off.`;
  const hours = capacity.wakeBilledHours;
  const cost = wakeCost(capacity);
  const bills = hours == null ? "It bills by the hour" : `It bills at least ${hours} hours`;
  return `Start ${name}? ${bills}${cost ? ` (about ${cost})` : ""} and is ready in about 2 minutes.`;
}

/** An F SKU's capacity units ("F8" is 8), or null for anything else. */
export function skuUnits(sku: string | null | undefined): number | null {
  const found = /^F(\d{1,4})$/.exec(sku ?? "");
  return found ? Number(found[1]) : null;
}

/**
 * What *sku* costs an hour, at the rate the server priced the current SKU at
 * (its hourlyUsd over its capacity units), or null when that is not known.
 */
export function skuHourly(capacity: FabricCapacity, sku: string): number | null {
  const units = skuUnits(capacity.sku);
  const target = skuUnits(sku);
  if (capacity.hourlyUsd == null || !units || target === null) return null;
  return (capacity.hourlyUsd / units) * target;
}

/** The resize step's sentence: both sizes, both prices an hour, and that it bills at once. */
export function resizeQuestion(capacity: FabricCapacity, sku: string): string {
  const price = (value: number | null) => (value === null ? "" : ` (${money(value)}/h)`);
  const from = `${capacity.sku ?? "its size"}${price(capacity.hourlyUsd ?? null)}`;
  const to = `${sku}${price(skuHourly(capacity, sku))}`;
  return `Resize ${capacity.name} from ${from} to ${to}? Billing changes at once.`;
}

/** Whether the row offers a size picker now: the server can resize it and ARM allows it. */
export function canResize(capacity: FabricCapacity): boolean {
  return (
    capacity.canScale === true &&
    !!capacity.sku &&
    (capacity.skuChoices?.length ?? 0) > 0 &&
    (capacity.state === "Active" || capacity.state === "Paused")
  );
}

/** The keep awake choices the server offers for *capacity*, in hours. */
export function keepAwakeChoices(capacity: FabricCapacity): number[] {
  return (capacity.keepAwakeOptions ?? []).map((option) => option.hours);
}

/**
 * Keep awake's sentence for *hours*: until when that keeps it up if idle and
 * what it adds, from the server's projection (keepAwakeOptions; fabric.py
 * keep_awake_options works it out with the same rule as sleepsAt). A choice
 * the server says adds nothing says so; it never reads as shortening.
 */
export function keepAwakeText(
  capacity: FabricCapacity,
  hours: number,
  now: number,
): { text: string; adds: boolean } {
  const hourly = capacity.hourlyUsd;
  const option = capacity.keepAwakeOptions?.find((item) => item.hours === hours);
  const until = instant(option?.sleepsAt);
  const sleeps = instant(capacity.sleepsAt);
  if (option && !option.addsTime) {
    const already = sleeps ?? until;
    return {
      text:
        already === null
          ? `It already stays up longer than that if idle; ${hours} h adds nothing.`
          : `It already stays up until about ${clockTime(already)} if idle; ${hours} h adds nothing.`,
      adds: false,
    };
  }
  if (until === null) {
    const cost = hourly != null ? ` (about ${money(hours * hourly)})` : "";
    return { text: `Keeps it up at least ${hours} more hours${cost}.`, adds: true };
  }
  const extra = ((until - Math.max(sleeps ?? now, now)) / HOUR_MS) * (hourly ?? 0);
  const cost = hourly != null ? ` (about ${money(Math.max(0, extra))} more)` : "";
  return { text: `Keeps it up until about ${clockTime(until)}${cost}.`, adds: true };
}

/** The chooser's first pick: the shortest choice that keeps it up longer, else the longest. */
export function defaultHours(capacity: FabricCapacity): number | null {
  const options = capacity.keepAwakeOptions ?? [];
  return (options.find((option) => option.addsTime) ?? options.at(-1))?.hours ?? null;
}

/** The wait before the next read: short while anything is on its way. */
export function nextDelay(data: FabricOverview | null): number {
  const busy = data?.capacities.some((capacity) => BUSY_STATES.has(capacity.state)) ?? false;
  return busy ? POLL_BUSY_MS : POLL_IDLE_MS;
}

/** Which ask-once action a capacity offers right now, if any. */
function offered(capacity: FabricCapacity): Action | null {
  if (capacity.state === "Paused") return "wake";
  if (capacity.state === "Active" && capacity.can_suspend) return "off";
  return null;
}

function canKeepAwake(capacity: FabricCapacity): boolean {
  return (
    capacity.state === "Active" &&
    capacity.can_keep_awake === true &&
    keepAwakeChoices(capacity).length > 0
  );
}

/** The menu's links: the capacity in the Azure portal, and Fabric's capacity settings. */
export function capacityLinks(capacity: FabricCapacity): { label: string; url: string }[] {
  const id = capacity.capacityId;
  if (!id || !id.startsWith("/subscriptions/")) return [];
  return [
    { label: "Open in Azure", url: `https://portal.azure.com/#resource${id}/overview` },
    { label: "Open in Fabric", url: FABRIC_CAPACITIES_URL },
  ];
}

// ── the page ────────────────────────────────────────────────────────────────

interface PageState {
  data: FabricOverview | null;
  error: unknown;
  refreshedAt: number | null;
  /** The confirm step that is open, per capacity; it survives a re-render. */
  confirming: Map<string, Action>;
  /** The keep awake chooser that is open, per capacity, with its chosen hours. */
  keeping: Map<string, number>;
  /** The resize step that is open, per capacity, with the size picked. */
  resizing: Map<string, string>;
  /** The last failed action's sentence, per capacity, until the next try. */
  failed: Map<string, string>;
  /** A link the host would not open, per capacity, shown to copy. */
  linkNote: Map<string, { lead: string; url: string }>;
  /**
   * Capacities with a call on its way. The card is kept as it is until the call
   * answers; the value is null when the button was not inside a card.
   */
  inFlight: Map<string, HTMLElement | null>;
  /** Per capacity, when its latest action started or ended (see `clock`). */
  lastAction: Map<string, number>;
  /** A counter, not a time: orders reads against actions without clock skew. */
  clock: number;
}

/**
 * The rows of a read that began at tick `begin`, minus any capacity whose
 * action started or ended after that: such a row predates the action and would
 * put the old state (and the idle poll) back. Those cards keep their row.
 */
export function freshRows(
  fresh: FabricOverview,
  current: FabricOverview | null,
  begin: number,
  lastAction: ReadonlyMap<string, number>,
): FabricOverview {
  const stale = (name: string) => (lastAction.get(name) ?? 0) > begin;
  if (![...lastAction.keys()].some(stale)) return fresh;
  const capacities = fresh.capacities.flatMap((row) => {
    if (!stale(row.name)) return [row];
    const kept = current?.capacities.find((item) => item.name === row.name);
    return kept ? [kept] : [];
  });
  return { ...fresh, capacities };
}

interface Page {
  context: ExtensionContext;
  state: PageState;
  status: HTMLElement;
  list: HTMLElement;
  refreshed: HTMLElement;
  render: () => void;
  schedule: (delay: number) => void;
  /**
   * After an action answers: the next read in *delay*, or, when a read is
   * still out, one more read *whenReading* after that one lands (its row for
   * the card is stale and dropped by freshRows).
   */
  afterAction: (delay: number, whenReading: number) => void;
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

/** Put an action's answer (the capacity's full row since the third pass) on its card. */
function setRow(page: Page, name: string, result: FabricActionResult): void {
  const row = page.state.data?.capacities.find((item) => item.name === name);
  if (!row) return;
  const { action: _action, changed: _changed, ...fresh } = result;
  Object.assign(row, fresh);
  if (!("error" in fresh)) delete row.error;
}

function confirmPanel(page: Page, capacity: FabricCapacity, action: Action): HTMLElement {
  const { name } = capacity;
  const box = el("div", {
    class: "fabric-panel fabric-confirm",
    role: "group",
    "aria-label": action === "wake" ? `Confirm starting ${name}` : `Confirm pausing ${name}`,
  });
  const busyLabel = action === "wake" ? "Starting..." : "Pausing...";
  const idleLabel = action === "wake" ? "Start now" : "Pause";
  // A re-render while the call is out builds this row again: born busy.
  const pending = page.state.inFlight.has(name);
  const cancel = keyed(
    button("Cancel", {
      disabled: pending,
      onClick: () => {
        page.state.confirming.delete(name);
        page.render();
      },
    }),
    `${name}:cancel`,
  );
  const go = keyed(
    button(pending ? busyLabel : idleLabel, {
      kind: action === "wake" ? "primary" : "danger",
      disabled: pending,
      onClick: async (self) => {
        if (page.state.inFlight.has(name)) return;
        // Held even when this row is not inside a card, so the guard never
        // depends on finding one.
        page.state.inFlight.set(name, box.closest<HTMLElement>("article"));
        page.state.lastAction.set(name, ++page.state.clock);
        cancel.disabled = true;
        self.textContent = busyLabel;
        page.state.failed.delete(name);
        try {
          const result = await fabricAction(
            page.context,
            name,
            action === "wake" ? "wake" : "suspend",
          );
          page.state.lastAction.set(name, ++page.state.clock);
          page.state.inFlight.delete(name);
          page.state.confirming.delete(name);
          setRow(page, name, result);
          announce(
            result.changed
              ? `${action === "wake" ? "Starting" : "Pausing"} ${name}.`
              : `${name} is ${stateText(result.state).toLowerCase()}; nothing changed.`,
            result.changed ? "ok" : "info",
          );
          page.render();
          page.afterAction(POLL_BUSY_MS, POLL_BUSY_MS);
        } catch (error) {
          page.state.lastAction.set(name, ++page.state.clock);
          page.state.inFlight.delete(name);
          page.state.confirming.delete(name);
          page.state.failed.set(
            name,
            `${action === "wake" ? "Start" : "Pause"} failed: ${describeError(error)}`,
          );
          page.render();
        }
      },
    }),
    `${name}:go`,
  );
  go.classList.add("btn-tint");
  box.appendChild(
    el(
      "div",
      { class: "fabric-confirm-row" },
      el("p", { class: "fabric-confirm-text" }, question(action, capacity)),
      cancel,
      go,
    ),
  );
  return box;
}

function keepPanel(page: Page, capacity: FabricCapacity): HTMLElement {
  const { name } = capacity;
  const pending = page.state.inFlight.has(name);
  const offered = keepAwakeChoices(capacity);
  const kept = page.state.keeping.get(name);
  let hours = kept !== undefined && offered.includes(kept) ? kept : (defaultHours(capacity) ?? 0);
  const box = el("div", {
    class: "fabric-panel fabric-keep",
    role: "group",
    "aria-label": `Keep ${name} awake`,
  });
  const sentence = el("p", { class: "fabric-confirm-text", "aria-live": "polite" });
  const cancel = keyed(
    button("Cancel", {
      disabled: pending,
      onClick: () => {
        page.state.keeping.delete(name);
        page.render();
      },
    }),
    `${name}:cancel`,
  );
  const go = keyed(
    button(pending ? "Keeping awake..." : "Keep awake", {
      kind: "primary",
      disabled: pending,
      onClick: async (self) => {
        if (page.state.inFlight.has(name)) return;
        page.state.inFlight.set(name, box.closest<HTMLElement>("article"));
        page.state.lastAction.set(name, ++page.state.clock);
        cancel.disabled = true;
        for (const choice of choices) choice.disabled = true;
        self.textContent = "Keeping awake...";
        page.state.failed.delete(name);
        const chosen = hours;
        try {
          const result = await fabricKeepAwake(page.context, name, chosen);
          page.state.lastAction.set(name, ++page.state.clock);
          page.state.inFlight.delete(name);
          page.state.keeping.delete(name);
          setRow(page, name, result);
          announce(
            result.changed
              ? `Keeping ${name} up ${chosen} more hours.`
              : `${name} is ${stateText(result.state).toLowerCase()}; nothing changed.`,
            result.changed ? "ok" : "info",
          );
          page.render();
          // The answer is the new row; a read that was out meanwhile is
          // followed by a fresh one at once (no ARM lag to wait out).
          page.afterAction(nextDelay(page.state.data), 0);
        } catch (error) {
          page.state.lastAction.set(name, ++page.state.clock);
          page.state.inFlight.delete(name);
          page.state.keeping.delete(name);
          page.state.failed.set(name, `Keep awake failed: ${describeError(error)}`);
          page.render();
        }
      },
    }),
    `${name}:go`,
  );
  go.classList.add("btn-tint");
  const choices = offered.map((value) =>
    keyed(
      el(
        "button",
        {
          type: "button",
          class: "btn fabric-choice",
          "aria-pressed": String(value === hours),
          disabled: pending,
        },
        `${value} h`,
      ),
      `${name}:keep-${value}`,
    ),
  );
  const show = () => {
    const said = keepAwakeText(capacity, hours, Date.now());
    sentence.textContent = said.text;
    if (!page.state.inFlight.has(name)) go.disabled = !said.adds;
    choices.forEach((choice, index) => {
      choice.setAttribute("aria-pressed", String(offered[index] === hours));
    });
  };
  choices.forEach((choice, index) => {
    choice.addEventListener("click", () => {
      if (choice.disabled) return;
      hours = offered[index];
      page.state.keeping.set(name, hours);
      show();
    });
  });
  show();
  box.append(
    el("div", { class: "fabric-choices", role: "group", "aria-label": "Hours" }, ...choices),
    el("div", { class: "fabric-confirm-row" }, sentence, cancel, go),
  );
  return box;
}

/** The resize step, the same ask once shape as Start and Pause: the sentence, Cancel, Resize. */
function resizePanel(page: Page, capacity: FabricCapacity, sku: string): HTMLElement {
  const { name } = capacity;
  const box = el("div", {
    class: "fabric-panel fabric-confirm fabric-resize",
    role: "group",
    "aria-label": `Confirm resizing ${name}`,
  });
  const pending = page.state.inFlight.has(name);
  const cancel = keyed(
    button("Cancel", {
      disabled: pending,
      onClick: () => {
        page.state.resizing.delete(name);
        page.render();
      },
    }),
    `${name}:cancel`,
  );
  const go = keyed(
    button(pending ? "Resizing..." : "Resize", {
      kind: "primary",
      disabled: pending,
      onClick: async (self) => {
        if (page.state.inFlight.has(name)) return;
        page.state.inFlight.set(name, box.closest<HTMLElement>("article"));
        page.state.lastAction.set(name, ++page.state.clock);
        cancel.disabled = true;
        self.textContent = "Resizing...";
        page.state.failed.delete(name);
        try {
          const result = await fabricScale(page.context, name, sku);
          page.state.lastAction.set(name, ++page.state.clock);
          page.state.inFlight.delete(name);
          page.state.resizing.delete(name);
          setRow(page, name, result);
          announce(
            result.changed
              ? `Resizing ${name} to ${sku}.`
              : `${name} is ${result.sku ?? sku} already; nothing changed.`,
            result.changed ? "ok" : "info",
          );
          page.render();
          page.afterAction(POLL_BUSY_MS, POLL_BUSY_MS);
        } catch (error) {
          page.state.lastAction.set(name, ++page.state.clock);
          page.state.inFlight.delete(name);
          page.state.resizing.delete(name);
          page.state.failed.set(name, `Resize failed: ${describeError(error)}`);
          page.render();
        }
      },
    }),
    `${name}:go`,
  );
  go.classList.add("btn-tint");
  box.appendChild(
    el(
      "div",
      { class: "fabric-confirm-row" },
      el("p", { class: "fabric-confirm-text" }, resizeQuestion(capacity, sku)),
      cancel,
      go,
    ),
  );
  return box;
}

/**
 * The size picker that stands in for the SKU in the meta line. Picking a size
 * only opens the resize step; the call waits for its Resize button.
 */
function sizePicker(page: Page, capacity: FabricCapacity): HTMLSelectElement {
  const { name } = capacity;
  const current = capacity.sku ?? "";
  const choices = capacity.skuChoices ?? [];
  const sizes = choices.includes(current) ? choices : [current, ...choices];
  const picked = page.state.resizing.get(name) ?? current;
  const node = keyed(
    el("select", {
      class: "fabric-size",
      "aria-label": `Size of ${name}`,
      title: "Resize (asks to confirm first)",
      disabled: page.state.inFlight.has(name),
    }),
    `${name}:size`,
  );
  for (const sku of sizes) {
    // The attribute too, so the markup alone carries the choice.
    const option = el("option", { value: sku, selected: sku === picked }, sku);
    option.selected = sku === picked;
    node.appendChild(option);
  }
  node.addEventListener("change", () => {
    page.state.confirming.delete(name);
    page.state.keeping.delete(name);
    if (node.value === current) page.state.resizing.delete(name);
    else page.state.resizing.set(name, node.value);
    page.render();
  });
  return node;
}

/** Ask the host to open *url*; a refusal shows the URL on the card to copy. */
async function openLink(page: Page, name: string, label: string, url: string): Promise<void> {
  page.state.linkNote.delete(name);
  try {
    await page.context.navigation.openExternal(url);
  } catch (error) {
    const where = label.replace(/^Open in /, "");
    page.state.linkNote.set(name, {
      lead: `Could not open ${where} from here (${describeError(error)}). Tap and hold to copy:`,
      url,
    });
  }
  page.render();
}

/**
 * The links behind one icon button: cards.ts rowMenu (a menu button with
 * arrow keys, Home, End, Escape and Tab, flipping up or right at the frame's
 * edge), with the links icon instead of "...".
 */
function linksMenu(page: Page, capacity: FabricCapacity): HTMLElement | null {
  const links = capacityLinks(capacity);
  if (links.length === 0) return null;
  const { name } = capacity;
  const items = links.map(({ label, url }) =>
    button(label, { onClick: () => openLink(page, name, label, url) }),
  );
  return rowMenu({ title: name }, items, {
    icon: linksIcon(),
    label: `Links for ${name}`,
    title: "Open in Azure or Fabric",
    className: "btn-icon fabric-links",
  });
}

function actionButtons(page: Page, capacity: FabricCapacity): HTMLElement[] {
  const { name } = capacity;
  const nodes: HTMLElement[] = [];
  const menu = linksMenu(page, capacity);
  if (menu) nodes.push(menu);
  const openStep = page.state.confirming.get(name);
  if (canKeepAwake(capacity)) {
    const keep = iconButton(
      `Keep ${name} awake longer`,
      clockIcon(),
      () => {
        page.state.confirming.delete(name);
        page.state.resizing.delete(name);
        if (page.state.keeping.has(name)) page.state.keeping.delete(name);
        else page.state.keeping.set(name, defaultHours(capacity) ?? 0);
        page.render();
      },
      "fabric-keep-btn",
    );
    keep.setAttribute("aria-expanded", String(page.state.keeping.has(name)));
    nodes.push(keyed(keep, `${name}:keep`));
  }
  const action = offered(capacity);
  const toggle = (which: Action) => () => {
    page.state.keeping.delete(name);
    page.state.resizing.delete(name);
    if (page.state.confirming.get(name) === which) page.state.confirming.delete(name);
    else page.state.confirming.set(name, which);
    page.render();
  };
  if (action === "off") {
    const off = iconButton(`Pause ${name}`, powerIcon(), toggle("off"), "fabric-off-btn");
    off.setAttribute("aria-expanded", String(openStep === "off"));
    nodes.push(keyed(off, `${name}:off`));
  } else if (action === "wake") {
    const wake = button("Start", {
      title: `Start ${name} (asks to confirm first)`,
      onClick: toggle("wake"),
    });
    wake.classList.add("btn-tint", "fabric-wake");
    wake.prepend(powerIcon());
    wake.setAttribute("aria-expanded", String(openStep === "wake"));
    nodes.push(keyed(wake, `${name}:wake`));
  }
  return nodes;
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

function capacityCard(page: Page, capacity: FabricCapacity, now: number): HTMLElement {
  const held = page.state.inFlight.get(capacity.name);
  if (held) return held;
  const { name } = capacity;
  const tone = toneOf(capacity.state);
  const label = stateText(capacity.state);
  // A step the state no longer offers is dropped.
  if (page.state.confirming.get(name) !== offered(capacity) || !offered(capacity)) {
    page.state.confirming.delete(name);
  }
  if (!canKeepAwake(capacity)) page.state.keeping.delete(name);
  const resizable = canResize(capacity);
  if (!resizable || page.state.resizing.get(name) === capacity.sku) {
    page.state.resizing.delete(name);
  }
  const meta = metaItems(capacity, now, !resizable);
  const picker = resizable ? sizePicker(page, capacity) : null;
  const up = upPercent(capacity, now);
  const upBar = up === null ? null : el("div", { class: "fabric-upbar", "aria-hidden": "true" }, el("i"));
  if (upBar) (upBar.firstElementChild as HTMLElement).style.width = `${up}%`;
  const node = el(
    "article",
    { class: `card card-fabric fabric-${tone}`, "data-capacity": name },
    el(
      "div",
      { class: meta.length || picker ? "fabric-row" : "fabric-row fabric-row-solo" },
      el("span", { class: "fabric-dot", "aria-hidden": "true" }),
      el(
        "div",
        { class: "fabric-title" },
        el("span", { class: "fabric-name" }, name),
        el("span", { class: "fabric-state" }, label),
      ),
      el("div", { class: "fabric-acts" }, ...actionButtons(page, capacity)),
      meta.length || picker
        ? el(
            "p",
            { class: "fabric-meta" },
            picker,
            ...meta.map((item) => el("span", { class: "fabric-meta-item" }, item)),
          )
        : null,
      upBar,
    ),
  );
  if (capacity.error) node.appendChild(notice(capacity.error, "error"));
  const failed = page.state.failed.get(name);
  if (failed) node.appendChild(notice(failed, "error"));
  const note = page.state.linkNote.get(name);
  if (note) node.appendChild(linkNotice(note));
  const open = page.state.confirming.get(name);
  const resizeTo = page.state.resizing.get(name);
  if (open) node.appendChild(confirmPanel(page, capacity, open));
  else if (resizeTo) node.appendChild(resizePanel(page, capacity, resizeTo));
  else if (page.state.keeping.has(name)) node.appendChild(keepPanel(page, capacity));
  return node;
}

function render(page: Page): void {
  const { state } = page;
  // The focus survives a redraw: the same control on the new card takes it.
  const active = document.activeElement as HTMLElement | null;
  const focusKey = page.list.contains(active) ? active?.dataset.focusKey : undefined;
  // An open links menu comes back on the redrawn card (cards.ts holdMenus).
  const restoreMenus = holdMenus();
  replaceChildren(page.status, [
    state.error
      ? notice(
          state.data
            ? `Showing the last answer; the refresh failed: ${describeError(state.error)}`
            : `Fabric capacities could not be read: ${describeError(state.error)}`,
          "error",
        )
      : null,
    state.data?.fleetError ? notice(state.data.fleetError, "info") : null,
  ]);
  const data = state.data;
  const now = Date.now();
  if (!data) {
    replaceChildren(page.list, [state.error ? null : el("p", { class: "meta" }, "Reading...")]);
  } else if (data.capacities.length === 0) {
    replaceChildren(page.list, [empty(data.note ?? "No Fabric capacity is configured.")]);
  } else {
    replaceChildren(
      page.list,
      data.capacities.map((capacity) => capacityCard(page, capacity, now)),
    );
  }
  if (focusKey && !page.list.contains(document.activeElement)) {
    const next = [...page.list.querySelectorAll<HTMLElement>("[data-focus-key]")].find(
      (node) => node.dataset.focusKey === focusKey,
    );
    next?.focus({ preventScroll: true });
  }
  restoreMenus();
  if (state.refreshedAt === null) {
    page.refreshed.textContent = "";
    page.refreshed.removeAttribute("title");
  } else {
    page.refreshed.textContent = `Updated ${clockTime(state.refreshedAt)}`;
    page.refreshed.title = `Again in ${nextDelay(data) / 1000} s or on focus.`;
  }
  // A failed read keeps the last answer on the tile, marked, and does not move "Updated".
  const tile = fabricTile(data, state.error, now);
  setTile(data ? staleTile(tile, state.error) : tile, state.error ? undefined : (state.refreshedAt ?? undefined));
}

let themeSubscription: Disposable | null = null;
let stopBoard: (() => void) | null = null;
let timer: ReturnType<typeof setTimeout> | null = null;
let onFocus: (() => void) | null = null;
let active = false;
/** Bumped by every activate, so one that was overtaken during its awaits stops. */
let activations = 0;

export async function activateFabric(context: ExtensionContext): Promise<void> {
  const root = document.getElementById("root");
  if (!root) throw new Error("Extension root is missing");
  stopBoard?.();
  resetTiles();
  const status = el("div", { class: "page-status", "aria-live": "polite" });
  const toast = el("div", { class: "page-status", id: "toast", "aria-live": "assertive" });
  // A section with an id, so a redraw finds an open menu's card (cards.ts whereOf).
  const list = el("section", {
    class: "cards fabric-list",
    id: "fabric-capacities",
    "aria-label": "Fabric capacities",
  });
  const refreshed = el("p", { class: "meta refreshed" });
  const updated = el("p", { class: "meta admin-updated" });
  const tiles = el("div", { class: "admin-tiles" });
  const board = el("div", { class: "admin-board" });
  const deploys = deploysSection();
  const databricks = databricksSection();
  const strip = databricksStrip();
  const host = hostSection();
  const secrets = secretsSection();
  const accountsBody = accountsSection();
  // The card headers carry one "Updated" each: Fabric's on Compute, the host's on Box health.
  databricks.querySelector(".dbx-refreshed")?.remove();
  secrets.querySelector(".secrets-refreshed")?.remove();
  const compute = adminCard(
    "compute",
    "Compute",
    boltIcon(16),
    [databricks.querySelector(".dbx-host"), refreshed],
    el("div", { class: "admin-block admin-fabric" }, status, list, toast),
    databricks,
  );
  const credit = adminCard("credit", "Databricks credit", coinIcon(), [], strip);
  const accounts = adminCard(
    "accounts",
    "Accounts",
    sharedIcon(16),
    [accountsBody.querySelector(".accounts-refreshed")],
    accountsBody,
  );
  const box = adminCard(
    "box",
    "Box health",
    shieldCheckIcon(),
    [host.querySelector(".host-refreshed")],
    host,
    secrets,
  );
  const again = button("", { title: "Read everything again", onClick: () => refreshAll() });
  again.classList.add("btn-icon", "admin-refresh");
  again.setAttribute("aria-label", "Read everything again");
  again.replaceChildren(refreshIcon());
  const main = el(
    "main",
    { class: "fabric admin" },
    el(
      "header",
      { class: "fabric-top" },
      el("h1", { class: "fabric-h" }, "Admin Center"),
      updated,
      again,
    ),
    tiles,
    board,
  );
  root.replaceChildren(main);
  mountTiles(tiles, updated);
  const layout = mountBoard(main, board, { deploys, accounts, compute, credit, box });
  stopBoard = layout.stop;
  active = true;
  const mine = ++activations;
  const state: PageState = {
    data: null,
    error: null,
    refreshedAt: null,
    confirming: new Map(),
    keeping: new Map(),
    resizing: new Map(),
    failed: new Map(),
    linkNote: new Map(),
    inFlight: new Map(),
    lastAction: new Map(),
    clock: 0,
  };
  let reading: Promise<void> | null = null;
  /** The delay of one more read queued behind the one that is out, if any. */
  let followUp: number | null = null;
  const refresh = (): Promise<void> => {
    if (reading) return reading;
    const begin = ++state.clock;
    reading = fetchFabric(context)
      .then(
        (data) => {
          state.data = freshRows(data, state.data, begin, state.lastAction);
          state.error = null;
        },
        (error: unknown) => {
          // The last good cards stay, with the failure said once above them.
          state.error = error;
        },
      )
      .then(() => {
        state.refreshedAt = Date.now();
        const queued = followUp;
        followUp = null;
        // A newer activation owns the page, the tiles and the timer now.
        if (!active || mine !== activations) return;
        page.render();
        page.schedule(queued ?? nextDelay(state.data));
      })
      .finally(() => {
        reading = null;
      });
    return reading;
  };
  const page: Page = {
    context,
    state,
    status,
    list,
    refreshed,
    render: () => render(page),
    schedule: (delay) => {
      if (timer) clearTimeout(timer);
      timer = active
        ? setTimeout(() => {
            // A hidden tab does not poll; visibilitychange reads on return.
            if (document.visibilityState === "visible") void refresh();
          }, delay)
        : null;
    },
    afterAction: (delay, whenReading) => {
      if (reading) followUp = Math.min(followUp ?? whenReading, whenReading);
      else page.schedule(delay);
    },
  };
  page.render();

  // The theme, and which cards Reid folded on his phone (a local read; the
  // defaults without it), asked for together.
  const [theme] = await Promise.all([context.theme.getCurrent(), loadSectionState(context)]);
  layout.refold();
  document.documentElement.dataset.theme = theme.theme;
  const subscription = await context.theme.subscribe((next) => {
    document.documentElement.dataset.theme = next.theme;
  });
  // A deactivate (or a newer activate) during the awaits above has already
  // stopped the sections; starting them now would leave their focus listeners
  // behind with nothing to remove them (dual review, 2026-10-20).
  if (!active || mine !== activations) {
    subscription.dispose();
    return;
  }
  themeSubscription = subscription;
  // Ready first, data second, as on the Dashboard: the host allows ten
  // seconds for activation.
  void refresh();
  activateDeploys(context, deploys);
  activateHost(context, box);
  activateDatabricks(context, compute, strip);
  activateSecrets(context, secrets);
  activateAccounts(context, accounts);
  const live = () => active && mine === activations;
  registerRefresh("fabric", () => (live() ? refresh() : undefined));
  onFocus = () => {
    if (document.visibilityState !== "visible") return;
    void refresh();
  };
  window.addEventListener("focus", onFocus);
  document.addEventListener("visibilitychange", onFocus);
}

export function deactivateFabric(): void {
  active = false;
  deactivateDeploys();
  deactivateHost();
  deactivateDatabricks();
  deactivateSecrets();
  deactivateAccounts();
  stopBoard?.();
  stopBoard = null;
  resetTiles();
  themeSubscription?.dispose();
  themeSubscription = null;
  if (timer) clearTimeout(timer);
  timer = null;
  if (onFocus) {
    window.removeEventListener("focus", onFocus);
    document.removeEventListener("visibilitychange", onFocus);
  }
  onFocus = null;
}
