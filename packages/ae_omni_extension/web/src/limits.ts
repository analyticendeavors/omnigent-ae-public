// Plan limits in the capacity strip (2026-09-24): the compact copy of the
// context ring's popover (patch P6), one line per harness, "Claude 5h 38% ·
// wk 61%", with the full labels and reset times in the tooltip. The numbers
// are `GET /v1/ae/limits`, filled by the box host's relay (docs/PLAN-LIMITS.md).
// Nothing renders until a reading exists, so a server without the relay keeps
// the strip as it was.

import type { ExtensionContext } from "@omnigent/extension-sdk";

export type Harness = "claude" | "codex";

export interface LimitWindow {
  id: string;
  label: string;
  used_percent: number;
  resets_at: string | null;
  window_minutes: number | null;
}

export interface LimitSnapshot {
  harness: Harness;
  observed_at: string;
  plan: string | null;
  windows: LimitWindow[];
  note: string | null;
}

export interface Limits {
  now: string;
  harnesses: Partial<Record<Harness, LimitSnapshot | null>>;
}

const NAMES: Record<Harness, string> = { claude: "Claude", codex: "Codex" };
// The two windows per harness /usage leads with; a Codex model bucket beyond
// them lives in the popover, not here or in the Session Dashboard header.
const SHOWN = 2;

export async function fetchLimits(context: ExtensionContext): Promise<Limits> {
  const response = await context.server.request({ method: "GET", path: "/v1/ae/limits" });
  if (!response.ok) throw new Error(`GET /v1/ae/limits answered ${response.status}`);
  return response.body as Limits;
}

/** "5h", "wk", "2d": the window's length in as few letters as reads unambiguously. */
export function shortLabel(window: LimitWindow): string {
  const minutes = window.window_minutes;
  if (minutes === 7 * 24 * 60) return "wk";
  if (minutes && minutes % (24 * 60) === 0) return `${minutes / (24 * 60)}d`;
  if (minutes && minutes % 60 === 0) return `${minutes / 60}h`;
  if (minutes) return `${minutes}m`;
  return window.label.split(/[\s,]/)[0].toLowerCase();
}

export function tone(pct: number): "ok" | "warn" | "err" {
  if (pct >= 90) return "err";
  if (pct >= 70) return "warn";
  return "ok";
}

function resetText(iso: string | null, now: number): string {
  if (!iso) return "";
  const at = Date.parse(iso);
  if (!Number.isFinite(at)) return "";
  if (at <= now) return ", reset since this reading";
  const date = new Date(at);
  const when = date.toLocaleString(undefined, {
    weekday: "short",
    hour: "numeric",
    minute: "2-digit",
  });
  return `, resets ${when}`;
}

/** One harness as the Session Dashboard header names it (header.ts). */
export interface ShownLimit {
  harness: Harness;
  name: string;
  /** The windows /usage leads with (SHOWN of them), in its order. */
  windows: LimitWindow[];
  /** Every window, the plan and when it was read, with reset times: the hover text. */
  title: string;
}

/** The harnesses with a reading and at least one window to show, Claude first. */
export function shownLimits(limits: Limits | null, now = Date.now()): ShownLimit[] {
  if (!limits) return [];
  const out: ShownLimit[] = [];
  for (const harness of ["claude", "codex"] as const) {
    const snapshot = limits.harnesses[harness];
    const windows = snapshot?.windows.slice(0, SHOWN) ?? [];
    if (!snapshot || windows.length === 0) continue;
    const updated = new Date(snapshot.observed_at).toLocaleTimeString(undefined, {
      hour: "numeric",
      minute: "2-digit",
    });
    const title = [
      `${NAMES[harness]}${snapshot.plan ? ` (${snapshot.plan})` : ""}, updated ${updated}`,
      ...snapshot.windows.map(
        (w) => `${w.label}: ${Math.round(w.used_percent)}% used${resetText(w.resets_at, now)}`,
      ),
    ].join("\n");
    out.push({ harness, name: NAMES[harness], windows, title });
  }
  return out;
}
