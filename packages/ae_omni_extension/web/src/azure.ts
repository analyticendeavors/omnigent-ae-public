// The Admin Center's Azure spend card (2026-10-22): what the Azure tenant has
// spent this month across its subscriptions and on what, with a flag for a
// resource whose latest day costs far more than its recent average.
//
// Why: a cost spike on one resource can go unseen until someone happens to
// open Cost Management. The card puts that glance on the page that already
// shows Fabric and Databricks cost. The server (`GET /v1/ae/azure-cost`, azure_cost.py) answers
// from a one hour cache and reads Azure in the background, so the first read
// after a restart says "Reading" and the next minute's poll has the numbers.
// Azure posts costs 8 to 24 hours late, so "through" names the newest day the
// month's figure covers and the caption says costs post late.
//
// The card's folded line (the "azure" tile, never drawn in the strip) reads
// "$412 this month" with the flag count; a flag makes the card amber and so
// opens it on a phone until Reid folds it. The Open Cost Management button at
// the foot asks the host to open the portal's cost analysis page (core patch
// 0151 admits that one URL); where the host refuses, the URL shows to copy.

import type { ExtensionContext } from "@omnigent/extension-sdk";
import {
  AeRequestError,
  type AzureCostAnswer,
  type AzureFlag,
  type AzureView,
  describeError,
  fetchAzureCost,
} from "./api";
import { mountTargets, type Targets, targetsSection } from "./azureTargets";
import { button, type Child, el, notice, replaceChildren } from "./dom";
import { externalLinkIcon } from "./icons";
import { copyText } from "./lot";
import { registerRefresh, setTile, staleTile, type Tile } from "./tiles";

export const COST_URL =
  "https://portal.azure.com/#view/Microsoft_Azure_CostManagement/Menu/~/costanalysis";
export const POLL_MS = 60_000;
/** While the server reads Azure for the first time, so the numbers show in seconds. */
export const READING_POLL_MS = 5_000;
const SVG_NS = "http://www.w3.org/2000/svg";

/** "$412" from $100 up, "$12.40" under it, in the answer's currency. */
export function money(amount: number, currency = "USD"): string {
  const whole = Math.abs(amount) >= 100;
  return new Intl.NumberFormat("en-US", {
    style: "currency",
    currency,
    minimumFractionDigits: whole ? 0 : 2,
    maximumFractionDigits: whole ? 0 : 2,
  }).format(amount);
}

/** "Oct 20" for an ISO day, without a timezone shifting it. */
export function dayText(iso: string): string {
  const [year, month, day] = iso.split("-").map(Number);
  return new Date(year, month - 1, day).toLocaleDateString("en-US", {
    month: "short",
    day: "numeric",
  });
}

/** "+9%" or "-4%" against last month, or null with no last month to compare. */
export function paceDelta(pace: number | null, lastMonth: number): string | null {
  if (pace === null || lastMonth <= 0) return null;
  const pct = Math.round(((pace - lastMonth) / lastMonth) * 100);
  return `${pct > 0 ? "+" : ""}${pct}%`;
}

export function flagText(flag: AzureFlag, currency: string): string {
  return (
    `${flag.resource} cost ${money(flag.cost, currency)} on ${dayText(flag.day)}, ` +
    `${flag.ratio}x its ${money(flag.average, currency)} a day average`
  );
}

export function azureTile(
  view: AzureView | null,
  configured: boolean,
  reading: boolean,
  error: unknown,
  rateLimit: AzureCostAnswer["rateLimit"] = null,
): Tile {
  if (!configured) {
    return {
      id: "azure",
      value: "Off",
      note: "not set up",
      health: "idle",
      summary: "Not set up",
    };
  }
  if (!view) {
    const text = error ? "Could not read" : reading ? "Reading" : "No data yet";
    return {
      id: "azure",
      value: text,
      note: "",
      health: error ? "attention" : "idle",
      summary: text,
    };
  }
  const flags = view.flags.length;
  const failed = view.subscriptions.filter((sub) => sub.error).length;
  const spent = `${money(view.month.spent, view.currency)} this month`;
  const parts = [spent];
  if (flags) parts.push(flags === 1 ? "1 flag" : `${flags} flags`);
  else if (failed) parts.push(failed === 1 ? "1 not read" : `${failed} not read`);
  // Unread subscriptions carry no error of their own while limited, so the tile says it.
  if (rateLimit) parts.push("rate limited");
  // Health stays as is: the totals are complete, but "no flags" would read as checked.
  if (view.subscriptions.some((sub) => sub.counted && sub.note)) {
    parts.push("spike check unavailable");
  }
  const delta = paceDelta(view.month.pace, view.month.lastMonth);
  return {
    id: "azure",
    value: money(view.month.spent, view.currency),
    note: parts.slice(1).join(" · ") || (delta ? `pace ${delta}` : ""),
    health: flags || failed || rateLimit ? "attention" : "ok",
    summary: parts.join(" · "),
  };
}

/**
 * A daily cost line, `azure-spark` (shared with the Databricks credit card,
 * 2026-10-22): *label* names it for a screen reader from its highest point.
 */
export function sparkline(costs: number[], label: (top: number) => string): SVGElement {
  const width = 300;
  const height = 46;
  const top = Math.max(...costs, 0.01);
  const step = costs.length > 1 ? width / (costs.length - 1) : 0;
  const points = costs
    .map(
      (cost, i) =>
        `${(i * step).toFixed(1)},${(height - 4 - (cost / top) * (height - 8)).toFixed(1)}`,
    )
    .join(" ");
  const svg = document.createElementNS(SVG_NS, "svg");
  svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
  svg.setAttribute("preserveAspectRatio", "none");
  svg.setAttribute("class", "azure-spark");
  svg.setAttribute("role", "img");
  svg.setAttribute("aria-label", label(top));
  const line = document.createElementNS(SVG_NS, "polyline");
  line.setAttribute("points", points);
  svg.appendChild(line);
  return svg;
}

function subscriptionRow(
  sub: AzureView["subscriptions"][number],
  total: number,
  currency: string,
): HTMLElement {
  // A subscription billed in another currency is shown in it, outside the total and its bars.
  // An older server sends neither field: such a row is counted, in the total's currency.
  const counted = sub.counted !== false;
  const own = sub.currency ?? currency;
  const unread = sub.read === false;
  const share = counted && total > 0 ? Math.min(100, (sub.spent / total) * 100) : 0;
  return el(
    "li",
    { class: counted ? "azure-sub" : "azure-sub azure-sub-apart" },
    el("span", { class: "azure-sub-name" }, sub.name),
    el("span", { class: "azure-amt" }, unread ? "not read yet" : money(sub.spent, own)),
    counted
      ? el(
          "span",
          { class: "azure-bar", "aria-hidden": "true" },
          el("i", { style: `width: ${share.toFixed(1)}%` }),
        )
      : el(
          "span",
          { class: "azure-sub-note meta" },
          `Billed in ${own}, so it is not in the ${currency} total.`,
        ),
    sub.error ? el("span", { class: "azure-sub-err meta" }, sub.error) : null,
    sub.billedIn
      ? el(
          "span",
          { class: "azure-sub-caveat meta" },
          `billed in ${sub.billedIn}; USD as Azure reports it`,
        )
      : null,
    sub.note ? el("span", { class: "azure-sub-caveat meta" }, sub.note) : null,
  );
}

/** "3:42 PM" today, else "Oct 24, 3:42 PM", in the viewer's clock. */
function clockText(ms: number, now: number): string {
  const time = new Date(ms).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
  if (new Date(ms).toDateString() === new Date(now).toDateString()) return time;
  return `${new Date(ms).toLocaleDateString([], { month: "short", day: "numeric" })}, ${time}`;
}

/**
 * The one sentence for a rate limit (2026-10-23), shown once under the month total instead of
 * under every subscription. With a next try still ahead it says when; otherwise it says the
 * numbers are from the last good reading and how old that is.
 */
export function rateLimitText(limit: RateLimit, now: number): string {
  const retry = limit.retryAt ? Date.parse(limit.retryAt) : Number.NaN;
  if (!Number.isNaN(retry) && retry > now) {
    return `Cost Management is rate limiting. Next try ${clockText(retry, now)}.`;
  }
  const good = limit.goodAt ? Date.parse(limit.goodAt) : Number.NaN;
  if (Number.isNaN(good)) {
    return "Cost Management is rate limiting. There is no reading yet.";
  }
  return `Cost Management is rate limiting. These numbers are from the last good reading, ${ageText(good, now)}.`;
}

function ageText(ms: number, now: number): string {
  const minutes = Math.floor(Math.max(0, now - ms) / 60_000);
  if (minutes < 1) return "just now";
  if (minutes < 60) return `${minutes} min ago`;
  const hours = Math.floor(minutes / 60);
  return hours < 48 ? `${hours} h ago` : `${Math.floor(hours / 24)} d ago`;
}

type RateLimit = NonNullable<AzureCostAnswer["rateLimit"]>;

/** The card's body for *view*, a list of nodes. */
export function azureBody(
  view: AzureView,
  rateLimit: AzureCostAnswer["rateLimit"] = null,
  now = Date.now(),
): Child[] {
  const { month, currency } = view;
  const delta = paceDelta(month.pace, month.lastMonth);
  return [
    el(
      "p",
      { class: "azure-big" },
      money(month.spent, currency),
      " ",
      el(
        "small",
        {},
        month.through ? `this month, through ${dayText(month.through)}` : "this month",
      ),
    ),
    rateLimit ? el("p", { class: "azure-limit meta", role: "status" }, rateLimitText(rateLimit, now)) : null,
    month.pace !== null
      ? el(
          "p",
          { class: "azure-pace meta" },
          `Pace ${money(month.pace, currency)}`,
          month.lastMonth > 0 ? ` vs ${money(month.lastMonth, currency)} last month` : "",
          delta ? ` (${delta})` : "",
        )
      : null,
    sparkline(
      view.daily.map((day) => day.cost),
      (top) => `Daily cost for the last ${view.daily.length} days, highest ${money(top, currency)}`,
    ),
    el(
      "p",
      { class: "azure-caption meta" },
      `Daily cost, last ${view.daily.length} days. Costs post up to a day late.`,
    ),
    view.flags.length
      ? el(
          "ul",
          { class: "azure-flags" },
          ...view.flags.map((flag) =>
            el(
              "li",
              { class: "azure-flag" },
              el("b", {}, "Check this: "),
              flagText(flag, currency),
              el("span", { class: "meta" }, ` ${flag.subscription}`),
            ),
          ),
        )
      : null,
    el(
      "ul",
      { class: "azure-subs" },
      ...[...view.subscriptions]
        // Counted rows first, so the dashed "apart" rows sit together at the foot.
        .sort((a, b) => Number(b.counted !== false) - Number(a.counted !== false))
        .map((sub) => subscriptionRow(sub, month.spent, currency)),
    ),
    view.services.length
      ? el(
          "div",
          { class: "azure-services" },
          el("h3", { class: "azure-h" }, "Biggest by service"),
          el(
            "ul",
            {},
            ...view.services.map((service) =>
              el(
                "li",
                {},
                el("span", {}, service.name),
                el("span", { class: "azure-amt" }, money(service.cost, currency)),
              ),
            ),
          ),
        )
      : null,
  ];
}

interface Parts {
  context: ExtensionContext;
  status: HTMLElement;
  list: HTMLElement;
  refreshed: HTMLElement;
}

interface SectionState {
  view: AzureView | null;
  configured: boolean;
  note: string | null;
  rateLimit: AzureCostAnswer["rateLimit"];
  reading: boolean;
  asOf: number;
  error: unknown;
  said: string;
  /** The failure of the last Open Cost Management, to show the URL to copy. */
  linkNote: string | null;
  /** An Open Cost Management is out, so a second tap cannot open a second tab. */
  linking: boolean;
  /** The server answered admin_only (2026-10-22): one quiet line, and no more polls. */
  adminOnly: boolean;
}

/** Open Cost Management: a button, and under it the URL to copy when the host would not open it. */
function costLink(parts: Parts, state: SectionState): HTMLElement {
  const open = button("Open Cost Management", {
    title: "Open the Azure portal's cost analysis",
    onClick: async () => {
      if (state.linking) return;
      state.linking = true;
      state.linkNote = null;
      try {
        await parts.context.navigation.openExternal(COST_URL);
      } catch (error) {
        state.linkNote = `Could not open Azure from here (${describeError(error)}). Tap and hold to copy:`;
      } finally {
        state.linking = false;
      }
      render(parts, state);
    },
  });
  open.classList.add("azure-open");
  open.prepend(externalLinkIcon(), " ");
  const note = state.linkNote ? notice(`${state.linkNote} `, "info") : null;
  if (note) {
    note.appendChild(el("code", { class: "link-copy" }, COST_URL));
    note.append(
      " ",
      button("Copy", {
        title: "Copy the link",
        onClick: async (self) => {
          self.textContent = (await copyText(COST_URL)) ? "Copied" : "Copy failed";
        },
      }),
    );
  }
  return el("div", { class: "azure-link" }, open, note);
}

function render(parts: Parts, state: SectionState): void {
  const said = state.error ? describeError(state.error) : "";
  if (said !== state.said) {
    state.said = said;
    replaceChildren(parts.status, [
      said ? notice(`Azure spend could not be read (${said}).`, "error") : null,
    ]);
  }
  if (!state.configured && state.note) {
    replaceChildren(parts.list, [el("p", { class: "empty" }, state.note)]);
  } else if (!state.view) {
    const text = state.error ? null : "Reading...";
    replaceChildren(parts.list, [text ? el("p", { class: "meta" }, text) : null]);
  } else {
    replaceChildren(parts.list, [...azureBody(state.view, state.rateLimit), costLink(parts, state)]);
  }
  parts.refreshed.textContent = state.asOf
    ? `Updated ${new Date(state.asOf).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })}`
    : "";
  const tile = azureTile(state.view, state.configured, state.reading, state.error, state.rateLimit);
  setTile(state.view ? staleTile(tile, state.error) : tile, state.asOf || undefined);
}

/** The Azure card's body; activateAzure fills it. */
export function azureSection(): HTMLElement {
  return el(
    "div",
    { class: "admin-block admin-azure" },
    el("p", { class: "meta refreshed azure-refreshed" }),
    el("div", { class: "page-status azure-status" }),
    el("section", { class: "azure-list", "aria-label": "Azure spend" }),
    targetsSection(),
  );
}

let timer: ReturnType<typeof setTimeout> | null = null;
let onFocus: (() => void) | null = null;
/** The Targets panel of the mounted card (azureTargets.ts); its reads ride on the card's own. */
let targets: Targets | null = null;
/** Bumped by every activate and deactivate, as in accounts.ts. */
let generation = 0;

function stop(): void {
  if (timer) clearTimeout(timer);
  timer = null;
  targets?.dispose();
  targets = null;
  if (!onFocus) return;
  window.removeEventListener("focus", onFocus);
  document.removeEventListener("visibilitychange", onFocus);
  onFocus = null;
}

/** Read and draw the section *root* (from azureSection), again on focus and every minute. */
export function activateAzure(context: ExtensionContext, root: HTMLElement): void {
  const list = root.querySelector<HTMLElement>(".azure-list");
  const status = root.querySelector<HTMLElement>(".azure-status");
  // In the card the header carries the time, so this one may have moved.
  const refreshed = root.querySelector<HTMLElement>(".azure-refreshed") ?? el("p");
  if (!list || !status) throw new Error("Azure section is missing");
  const parts: Parts = { context, status, list, refreshed };
  const mine = ++generation;
  const live = () => generation === mine;
  stop();
  const panel = root.querySelector<HTMLElement>(".azure-targets");
  const mounted = panel ? mountTargets(context, panel) : null;
  targets = mounted;
  const state: SectionState = {
    view: null,
    configured: true,
    note: null,
    rateLimit: null,
    reading: false,
    asOf: 0,
    error: null,
    said: "",
    linkNote: null,
    linking: false,
    adminOnly: false,
  };
  let reading: Promise<void> | null = null;
  const schedule = () => {
    if (timer) clearTimeout(timer);
    timer = setTimeout(
      () => {
        // A hidden tab does not poll; visibilitychange reads on return.
        if (document.visibilityState === "visible") void refresh();
        else schedule();
      },
      state.reading ? READING_POLL_MS : POLL_MS,
    );
  };
  const refresh = (): Promise<void> => {
    if (reading) return reading;
    reading = fetchAzureCost(context)
      .then(
        (answer) => {
          if (!live()) return;
          state.configured = answer.configured;
          state.note = answer.note;
          state.rateLimit = answer.rateLimit ?? null;
          state.reading = Boolean(answer.reading);
          // The server's own reading time: the numbers are as old as that, not as the poll.
          if (answer.view) state.view = answer.view;
          // Secrets taken away: the old numbers must go, or the card shows them under "Off".
          if (!answer.configured) state.view = null;
          if (answer.asOf) state.asOf = Date.parse(answer.asOf);
          state.error = null;
        },
        (error: unknown) => {
          if (!live()) return;
          if (error instanceof AeRequestError && error.code === "admin_only") {
            // Not an error to fix: Azure spend is for admins, so say so once and stop asking.
            state.adminOnly = true;
            state.configured = false;
            state.note = "Azure spend is for admins.";
            state.view = null;
            state.error = null;
            return;
          }
          state.error = error ?? new Error("Request failed");
        },
      )
      .then(() => {
        if (!live()) return;
        render(parts, state);
        // The targets are read on the card's own poll; they fail on their own, quietly.
        if (state.configured && !state.adminOnly) void mounted?.refresh();
        if (panel) panel.hidden = !state.configured || state.adminOnly;
        if (!state.adminOnly) schedule();
      })
      .finally(() => {
        reading = null;
      });
    return reading;
  };
  render(parts, state);
  void refresh();
  registerRefresh("azure", () => (live() ? refresh() : undefined));
  onFocus = () => {
    if (document.visibilityState === "visible") void refresh();
  };
  window.addEventListener("focus", onFocus);
  document.addEventListener("visibilitychange", onFocus);
}

export function deactivateAzure(): void {
  generation++;
  stop();
}
