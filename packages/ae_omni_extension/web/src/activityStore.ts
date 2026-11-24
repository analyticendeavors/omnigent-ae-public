// The Needs you activity log as the page reads it (2026-11-16, D2 phase 2): the Done entries and
// the people's actions of the last seven days, from `GET /v1/ae/attention/done`.
//
// A plain module with no drawing. The Activity tab and the rail's "Done today" both read this one
// store, so they can never disagree, and adminShell.ts decides when to read again. "Done" means
// "no longer reported": the server cannot tell a fix from a card that went quiet, and the page
// says so wherever it lists Done entries. An unreadable log is `unreadable`, never an empty day.

import type { ExtensionContext } from "@omnigent/extension-sdk";
import { type ActivityEvent, type DoneAnswer, type DoneItem, fetchAttentionDone } from "./api";

/** How many days of Done entries the page reads (the route's largest). */
export const ACTIVITY_DAYS = 7;
/** A read this old or younger is not repeated by `loadActivity` unless forced. */
export const ACTIVITY_FRESH_MS = 30_000;

export interface ActivityState {
  /** The newest good answer, or null before any read. */
  data: DoneAnswer | null;
  /** True once the route answered (even with an unreadable log): the page offers the Activity tab. */
  loaded: boolean;
  /** The last read failed; the older answer (if any) stays. */
  error: unknown;
  at: number;
}

let state: ActivityState = { data: null, loaded: false, error: null, at: 0 };
const listeners = new Set<() => void>();
let inflight: Promise<void> | null = null;

const emit = (): void => {
  for (const listener of [...listeners]) listener();
};

export const getActivity = (): Readonly<ActivityState> => state;

export function onActivity(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

export function resetActivity(): void {
  state = { data: null, loaded: false, error: null, at: 0 };
  listeners.clear();
  inflight = null;
}

/** True when the log could not be read on the server: say so, never "nothing". */
export const unreadable = (data: DoneAnswer | null = state.data): boolean => !!data && data.unknown.length > 0;

/** Read the log (not again within ACTIVITY_FRESH_MS unless *force*); one read at a time. */
export function loadActivity(context: ExtensionContext, force = false, now = Date.now()): Promise<void> {
  if (inflight) return inflight;
  if (!force && state.data && now - state.at < ACTIVITY_FRESH_MS) return Promise.resolve();
  inflight = fetchAttentionDone(context, ACTIVITY_DAYS).then(
    (data) => {
      state = { data, loaded: true, error: null, at: now };
      inflight = null;
      emit();
    },
    (error: unknown) => {
      // A server without the route (or a viewer who may not read it) keeps `loaded` false.
      state = { ...state, error };
      inflight = null;
      emit();
    },
  );
  return inflight;
}

/** The local calendar day of an instant, as YYYY-MM-DD. */
export function dayKey(iso: string | number): string {
  const at = new Date(iso);
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${at.getFullYear()}-${pad(at.getMonth() + 1)}-${pad(at.getDate())}`;
}

/** The Done entries that left today (the viewer's local day), newest first. */
export function doneToday(data: DoneAnswer | null, now = Date.now()): DoneItem[] {
  const today = dayKey(now);
  return (data?.items ?? []).filter((item) => dayKey(item.done_at) === today);
}

export interface ActivityDay {
  /** YYYY-MM-DD, local. */
  key: string;
  done: DoneItem[];
  events: ActivityEvent[];
}

/** The last seven days that have anything, newest first, each with its Done entries and events. */
export function activityDays(data: DoneAnswer | null): ActivityDay[] {
  const days = new Map<string, ActivityDay>();
  const day = (iso: string): ActivityDay => {
    const key = dayKey(iso);
    let found = days.get(key);
    if (!found) {
      found = { key, done: [], events: [] };
      days.set(key, found);
    }
    return found;
  };
  for (const item of data?.items ?? []) day(item.done_at).done.push(item);
  for (const event of data?.events ?? []) day(event.at).events.push(event);
  return [...days.values()].sort((a, b) => (a.key < b.key ? 1 : -1));
}

/** "Today", "Yesterday" or "Mon, Nov 16" for a day key. */
export function dayLabel(key: string, now = Date.now()): string {
  if (key === dayKey(now)) return "Today";
  if (key === dayKey(now - 86_400_000)) return "Yesterday";
  const [y, m, d] = key.split("-").map(Number);
  return new Date(y!, (m ?? 1) - 1, d).toLocaleDateString([], { weekday: "short", month: "short", day: "numeric" });
}

const EVENT_WORDS: Record<ActivityEvent["what"], string> = {
  dismissed: "dismissed",
  snoozed: "snoozed",
  woke: "woke",
  restored: "restored",
  "chat-started": "started a chat on",
  assigned: "assigned",
  unassigned: "unassigned",
};

/** "alice dismissed Bump y": who did what to which item (the item's title when the log has one). */
export function eventText(event: ActivityEvent, sourceName: string): string {
  const who = event.by ? event.by.split("@")[0] : "Someone";
  const what = event.title || `an item in ${sourceName}`;
  if (event.what === "assigned") return `${who} assigned ${what}${event.to ? ` to ${event.to.split("@")[0]}` : ""}`;
  if (event.what === "unassigned" && event.by === "system") return `${what} was unassigned because it left the list`;
  return `${who} ${EVENT_WORDS[event.what]} ${what}`;
}
