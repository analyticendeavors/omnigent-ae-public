// The Admin Center's Accounts card (2026-10-21): every signed-in Claude login
// (the default, personal ones, shared ones) and the Codex login, each with its
// 5-hour and weekly use, from `GET /v1/ae/limits/accounts`
// (ae_omni_policies/limits.py, "Per account").
//
// Why: the owner runs several Claude logins and one Codex login, and usage was
// visible only per session (the rail's Usage card), so the account nearest its
// limit was found by opening sessions one by one. This card is the one place
// that says which account has room.
//
// Rows are grouped Claude then Codex. Each row is the label, the plan when
// the source carries one (the default login), a 5-hour and a weekly bar with
// the percent and the reset time, the live sessions on it, and the reading's
// age: an idle account shows its last reading ("as of 3h ago"), never a fresh
// probe, and a signed-in account the relay has not read yet says "no reading
// yet". Bars turn amber at 70% and red at 90%, the steps of `limitTone` in the
// chat's usage panel (upstream web/src/components/AePlanLimits.tsx).
//
// Read on activate, on focus and every minute while visible (the relay posts
// every five). After every draw it hands the page its Accounts tile
// (tiles.ts): the account count and the fullest window, which is also the
// phone's folded header line.

import type { ExtensionContext } from "@omnigent/extension-sdk";
import { type AccountRow, type AccountWindow, describeError, fetchAccounts } from "./api";
import { el, notice, replaceChildren } from "./dom";
import { tone } from "./limits";
import { type Health, registerRefresh, setTile, staleTile, type Tile } from "./tiles";

export const POLL_MS = 60_000;

const GROUPS: ReadonlyArray<{ harness: AccountRow["harness"]; title: string }> = [
  { harness: "claude", title: "Claude" },
  { harness: "codex", title: "Codex" },
];

// ── words ───────────────────────────────────────────────────────────────────

/** "as of just now", "as of 12m ago", "as of 3h ago", "as of 2d ago"; "no reading yet" without. */
export function ageText(observedAt: string | null, now: number): string {
  if (!observedAt) return "no reading yet";
  const at = Date.parse(observedAt);
  if (!Number.isFinite(at)) return "no reading yet";
  const minutes = Math.floor(Math.max(0, now - at) / 60_000);
  if (minutes < 1) return "as of just now";
  if (minutes < 60) return `as of ${minutes}m ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 48) return `as of ${hours}h ago`;
  return `as of ${Math.floor(hours / 24)}d ago`;
}

/** Whether *window* has reset since its reading, so its percent no longer holds. */
export function hasReset(window: AccountWindow, now: number): boolean {
  if (!window.resets_at) return false;
  const at = Date.parse(window.resets_at);
  return Number.isFinite(at) && at <= now;
}

/** "resets Thu 9:00 AM", "reset since this reading", or nothing without a time. */
export function resetText(window: AccountWindow, now: number): string {
  if (!window.resets_at) return "";
  const at = Date.parse(window.resets_at);
  if (!Number.isFinite(at)) return "";
  if (at <= now) return "reset since this reading";
  const when = new Date(at).toLocaleString(undefined, {
    weekday: "short",
    hour: "numeric",
    minute: "2-digit",
  });
  return `resets ${when}`;
}

/** "2 live sessions", "1 live session", "No live session"; nothing where none are counted. */
export function liveText(row: AccountRow): string {
  if (row.live_sessions === null) return "";
  if (row.live_sessions === 0) return "No live session";
  return `${row.live_sessions} live session${row.live_sessions === 1 ? "" : "s"}`;
}

export interface Fullest {
  row: AccountRow;
  window: AccountWindow;
  /** "5-hour" or "weekly". */
  name: string;
  percent: number;
}

/** The fullest 5-hour or weekly window across *rows*, skipping one reset since; null when none. */
export function fullest(rows: AccountRow[], now: number): Fullest | null {
  let best: Fullest | null = null;
  for (const row of rows) {
    for (const [name, window] of [
      ["5-hour", row.five_hour],
      ["weekly", row.weekly],
    ] as const) {
      if (!window || hasReset(window, now)) continue;
      const percent = Math.round(window.used_percent);
      if (!best || percent > best.percent) best = { row, window, name, percent };
    }
  }
  return best;
}

const HEALTH: Record<ReturnType<typeof tone>, Health> = {
  ok: "ok",
  warn: "attention",
  err: "problem",
};

export const SETTINGS_TITLE = "Claude accounts are managed in OmniGent Settings > Claude accounts.";

/**
 * The Accounts tile: how many accounts, and the fullest window, with its
 * health from the same 70 and 90 steps as the bars. "Manage in Settings"
 * stays in the note: an extension page cannot open a Settings page (the
 * SDK's navigation.openPage opens only the extension's own pages).
 */
export function accountsTile(rows: AccountRow[] | null, now: number, error: unknown = null): Tile {
  if (!rows) {
    return {
      id: "accounts",
      value: error ? "Unknown" : "Reading...",
      note: error ? "could not be read" : "Manage in Settings",
      health: error ? "attention" : "idle",
      title: SETTINGS_TITLE,
    };
  }
  const top = fullest(rows, now);
  const count = `${rows.length} account${rows.length === 1 ? "" : "s"}`;
  if (!top) {
    return {
      id: "accounts",
      value: String(rows.length),
      note: "Manage in Settings",
      health: "idle",
      summary: `${count}, no reading yet`,
      title: SETTINGS_TITLE,
    };
  }
  return {
    id: "accounts",
    value: String(rows.length),
    note: `fullest ${top.percent}% \u00b7 Manage in Settings`,
    health: HEALTH[tone(top.percent)],
    summary: `${top.row.label} ${top.percent}% ${top.name}`,
    title: `Fullest: ${top.row.label}, ${top.name} ${top.percent}%. ${SETTINGS_TITLE}`,
  };
}

// ── the section ─────────────────────────────────────────────────────────────

function bar(name: string, window: AccountWindow | null, now: number): HTMLElement {
  if (!window) {
    return el(
      "div",
      { class: "acct-bar acct-bar-none" },
      el(
        "span",
        { class: "acct-bar-top" },
        el("span", { class: "acct-bar-name" }, name),
        "No reading",
      ),
    );
  }
  const passed = hasReset(window, now);
  // The tone from the percent shown, as the tile does, so "90%" is never amber.
  const percent = Math.round(window.used_percent);
  const fill = el("i", { class: "acct-fill" });
  fill.style.width = `${Math.min(100, Math.max(0, window.used_percent))}%`;
  const when = resetText(window, now);
  const label = `${name}: ${percent}% used${when ? `, ${when}` : ""}`;
  return el(
    "div",
    {
      class: `acct-bar acct-${passed ? "reset" : tone(percent)}`,
      role: "meter",
      "aria-label": label,
      "aria-valuemin": "0",
      "aria-valuemax": "100",
      "aria-valuenow": String(Math.min(100, percent)),
      title: window.label,
    },
    el(
      "span",
      { class: "acct-bar-top" },
      el("span", { class: "acct-bar-name" }, name),
      el("span", { class: "acct-bar-value" }, `${percent}%`),
      el("span", { class: "acct-bar-reset" }, when),
    ),
    el("span", { class: "acct-track", "aria-hidden": "true" }, fill),
  );
}

/**
 * The bars a read row draws. A plan may carry only one window (Codex on a
 * weekly-only plan has no 5-hour one), so a missing window beside a present
 * one is left out, not drawn as "No reading", which would look like a failed
 * read. A reading with neither window keeps both slots at "No reading".
 */
export function barsFor(row: AccountRow, now: number): HTMLElement[] {
  if (!row.five_hour && !row.weekly) {
    return [bar("5-hour", null, now), bar("Weekly", null, now)];
  }
  return [
    row.five_hour ? bar("5-hour", row.five_hour, now) : null,
    row.weekly ? bar("Weekly", row.weekly, now) : null,
  ].filter((node): node is HTMLElement => node !== null);
}

export function accountRow(row: AccountRow, now: number): HTMLElement {
  const facts = [liveText(row), ageText(row.observed_at, now)].filter(Boolean);
  return el(
    "li",
    {
      class: `acct-row${row.observed_at ? "" : " acct-unread"}`,
      "data-harness": row.harness,
      "data-account": row.account ?? "",
    },
    el(
      "div",
      { class: "acct-head" },
      el("span", { class: "acct-label" }, row.label),
      row.plan ? el("span", { class: "acct-plan" }, row.plan) : null,
    ),
    row.observed_at
      ? el(
          "div",
          { class: "acct-bars" },
          ...barsFor(row, now),
        )
      : null,
    el("p", { class: "acct-meta" }, facts.join(" · ")),
  );
}

/** The rows, grouped Claude then Codex; a group with no row is left out. */
export function accountsList(rows: AccountRow[], now: number): HTMLElement[] {
  return GROUPS.flatMap(({ harness, title }) => {
    const mine = rows.filter((row) => row.harness === harness);
    if (!mine.length) return [];
    return [
      el(
        "div",
        { class: "acct-group", "data-group": harness },
        el("h3", { class: "acct-group-h" }, title),
        el(
          "ul",
          { class: "acct-rows", "aria-label": `${title} accounts` },
          ...mine.map((row) => accountRow(row, now)),
        ),
      ),
    ];
  });
}

interface SectionState {
  rows: AccountRow[] | null;
  at: number;
  error: unknown;
  /** The failure the status line last said, so an unchanged one is not announced again. */
  said: string;
}

function render(root: Parts, state: SectionState): void {
  const now = Date.now();
  const said = state.error ? describeError(state.error) : "";
  // The rows redraw every read for their ages; the failure notice only when it
  // changes, or a screen reader would announce its role="alert" each minute.
  if (said !== state.said) {
    state.said = said;
    replaceChildren(root.status, [
      said ? notice(`The accounts could not be read (${said}).`, "error") : null,
    ]);
  }
  if (!state.rows) {
    replaceChildren(root.list, [state.error ? null : el("p", { class: "meta" }, "Reading...")]);
  } else if (!state.rows.length) {
    replaceChildren(root.list, [el("p", { class: "meta" }, "No account has signed in yet.")]);
  } else {
    // The last good rows stay, with the failure said above them.
    replaceChildren(root.list, accountsList(state.rows, now));
  }
  root.refreshed.textContent = state.at
    ? `Updated ${new Date(state.at).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })}`
    : "";
  const tile = accountsTile(state.rows, now, state.error);
  setTile(state.rows ? staleTile(tile, state.error) : tile, state.at || undefined);
}

interface Parts {
  status: HTMLElement;
  list: HTMLElement;
  refreshed: HTMLElement;
}

/** The Accounts card's body; activateAccounts fills it. */
export function accountsSection(): HTMLElement {
  return el(
    "div",
    { class: "admin-block admin-accounts" },
    el("p", { class: "meta refreshed accounts-refreshed" }),
    el("div", { class: "page-status accounts-status" }),
    el("section", { class: "accounts-list", "aria-label": "Accounts" }),
  );
}

let timer: ReturnType<typeof setTimeout> | null = null;
let onFocus: (() => void) | null = null;
/** Bumped by every activate and deactivate, as in host.ts. */
let generation = 0;

function stop(): void {
  if (timer) clearTimeout(timer);
  timer = null;
  if (!onFocus) return;
  window.removeEventListener("focus", onFocus);
  document.removeEventListener("visibilitychange", onFocus);
  onFocus = null;
}

/** Read and draw the section *root* (from accountsSection), again on focus and every minute. */
export function activateAccounts(context: ExtensionContext, root: HTMLElement): void {
  const list = root.querySelector<HTMLElement>(".accounts-list");
  const status = root.querySelector<HTMLElement>(".accounts-status");
  // In the card the header carries the time, so this one may have moved.
  const refreshed = root.querySelector<HTMLElement>(".accounts-refreshed") ?? el("p");
  if (!list || !status) throw new Error("Accounts section is missing");
  const parts: Parts = { status, list, refreshed };
  const mine = ++generation;
  const live = () => generation === mine;
  stop();
  const state: SectionState = { rows: null, at: 0, error: null, said: "" };
  let reading: Promise<void> | null = null;
  const schedule = () => {
    if (timer) clearTimeout(timer);
    timer = setTimeout(() => {
      // A hidden tab does not poll; visibilitychange reads on return.
      if (document.visibilityState === "visible") void refresh();
      else schedule();
    }, POLL_MS);
  };
  const refresh = (): Promise<void> => {
    if (reading) return reading;
    reading = fetchAccounts(context)
      .then(
        (answer) => {
          if (!live()) return;
          state.rows = answer.accounts;
          state.at = Date.now();
          state.error = null;
        },
        (error: unknown) => {
          if (live()) state.error = error ?? new Error("Request failed");
        },
      )
      .then(() => {
        if (!live()) return;
        render(parts, state);
        schedule();
      })
      .finally(() => {
        reading = null;
      });
    return reading;
  };
  render(parts, state);
  void refresh();
  registerRefresh("accounts", () => (live() ? refresh() : undefined));
  onFocus = () => {
    if (document.visibilityState === "visible") void refresh();
  };
  window.addEventListener("focus", onFocus);
  document.addEventListener("visibilitychange", onFocus);
}

export function deactivateAccounts(): void {
  generation++;
  stop();
}
