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
import { AeRequestError, type AzureFlag, type AzureView, describeError, fetchAzureCost } from "./api";
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
  // Health stays as is: the totals are complete, but "no flags" would read as checked.
  if (view.subscriptions.some((sub) => sub.counted && sub.note)) {
    parts.push("spike check unavailable");
  }
  const delta = paceDelta(view.month.pace, view.month.lastMonth);
  return {
    id: "azure",
    value: money(view.month.spent, view.currency),
    note: parts.slice(1).join(" · ") || (delta ? `pace ${delta}` : ""),
    health: flags || failed ? "attention" : "ok",
    summary: parts.join(" · "),
  };
}

function sparkline(view: AzureView): SVGElement {
  const width = 300;
  const height = 46;
  const costs = view.daily.map((day) => day.cost);
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
  svg.setAttribute(
    "aria-label",
    `Daily cost for the last ${costs.length} days, highest ${money(top, view.currency)}`,
  );
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
  const share = counted && total > 0 ? Math.min(100, (sub.spent / total) * 100) : 0;
  return el(
    "li",
    { class: counted ? "azure-sub" : "azure-sub azure-sub-apart" },
    el("span", { class: "azure-sub-name" }, sub.name),
    el("span", { class: "azure-amt" }, money(sub.spent, own)),
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
    sub.note ? el("span", { class: "azure-sub-caveat meta" }, sub.note) : null,
  );
}

/** The card's body for *view*, a list of nodes. */
export function azureBody(view: AzureView): Child[] {
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
    month.pace !== null
      ? el(
          "p",
          { class: "azure-pace meta" },
          `Pace ${money(month.pace, currency)}`,
          month.lastMonth > 0 ? ` vs ${money(month.lastMonth, currency)} last month` : "",
          delta ? ` (${delta})` : "",
        )
      : null,
    sparkline(view),
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
    replaceChildren(parts.list, [...azureBody(state.view), costLink(parts, state)]);
  }
  parts.refreshed.textContent = state.asOf
    ? `Updated ${new Date(state.asOf).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })}`
    : "";
  const tile = azureTile(state.view, state.configured, state.reading, state.error);
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
  );
}

let timer: ReturnType<typeof setTimeout> | null = null;
let onFocus: (() => void) | null = null;
/** Bumped by every activate and deactivate, as in accounts.ts. */
let generation = 0;

function stop(): void {
  if (timer) clearTimeout(timer);
  timer = null;
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
  const state: SectionState = {
    view: null,
    configured: true,
    note: null,
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
