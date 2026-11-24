// What is selected in the Needs you table, and the words for what a bulk action did (2026-11-16,
// D2 phase 2). A plain module with no drawing: needsTable.ts and needsBulk.ts draw from it and
// test/needsSelect.test.ts covers it without a page.
//
// Selection survives a redraw (ids, not nodes) and drops ids that left the list (`keepOnly`),
// so a bulk action never reaches an item the person can no longer see.

import { type AttentionItem, type BulkAnswer, type BulkResult, assignedElsewhere } from "./api";
import { BATCH_MAX, actionsOf, isPersonal } from "./attention";
import { viewerLogin } from "./attentionStore";

/** Most ids one bulk request takes (the server's BULK_MAX); a longer selection goes in pieces. */
export const BULK_MAX = 50;

const picked = new Set<string>();
const listeners = new Set<() => void>();

const emit = (): void => {
  for (const listener of [...listeners]) listener();
};

export const isPicked = (id: string): boolean => picked.has(id);
export const pickedIds = (): string[] => [...picked];
export const pickedCount = (): number => picked.size;

export function onSelection(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

export function setPicked(ids: string[], on: boolean): void {
  let changed = false;
  for (const id of ids) {
    if (on ? !picked.has(id) : picked.has(id)) {
      if (on) picked.add(id);
      else picked.delete(id);
      changed = true;
    }
  }
  if (changed) emit();
}

export function togglePicked(id: string): void {
  setPicked([id], !picked.has(id));
}

export function clearPicked(): void {
  if (!picked.size) return;
  picked.clear();
  emit();
}

/** Drop every selected id that is not in *ids* (an item that left the list). */
export function keepOnly(ids: Iterable<string>): void {
  const keep = new Set(ids);
  const gone = [...picked].filter((id) => !keep.has(id));
  if (gone.length) setPicked(gone, false);
}

/** Forget everything (deactivate, and the tests). */
export function resetSelection(): void {
  picked.clear();
  listeners.clear();
}

export type GroupState = "none" | "some" | "all";

/** What a select-all checkbox over *ids* shows: nothing, some (indeterminate) or every one. */
export function groupState(ids: string[]): GroupState {
  if (!ids.length) return "none";
  const n = ids.filter((id) => picked.has(id)).length;
  return n === 0 ? "none" : n === ids.length ? "all" : "some";
}

/** Items Snooze can take: not a personal Git item (Dismiss only) and with a rule for "worse". */
export const snoozable = (item: AttentionItem): boolean =>
  !isPersonal(item) && item.dismissible && actionsOf(item).includes("dismiss");

/** Items Review can take (never one assigned to another admin: the server leaves those out). */
export const reviewable_ = (item: AttentionItem): boolean =>
  !isPersonal(item) && actionsOf(item).includes("review") && !assignedElsewhere(item, viewerLogin());

/** A Social post row: Posted and Skip post change the plan the Social card shows; Dismiss would only hide the row. */
export const isSocialPost = (item: Pick<AttentionItem, "source" | "slot">): boolean => item.source === "social" && Boolean(item.slot);

/** The result-line words when Dismiss meets Social posts. */
export const SOCIAL_BULK_NOTE = "Social posts: use Posted or Skip on the row";

export interface BulkPlan {
  /** The selected items that are still listed, in list order. */
  items: AttentionItem[];
  dismiss: string[];
  snooze: string[];
  review: string[];
  /** Selected items that only take Dismiss (personal Git). */
  dismissOnly: number;
  /** Selected Social posts: Dismiss leaves them out (it would only hide the row; the Social card keeps counting them). */
  social: number;
}

/** What each bulk button would act on, from the listed *items* (worst first, as drawn). */
export function planBulk(items: AttentionItem[]): BulkPlan {
  const chosen = items.filter((item) => picked.has(item.id));
  return {
    items: chosen,
    dismiss: chosen.filter((item) => !isSocialPost(item)).map((item) => item.id),
    snooze: chosen.filter(snoozable).map((item) => item.id),
    review: chosen.filter(reviewable_).map((item) => item.id),
    dismissOnly: chosen.filter(isPersonal).length,
    social: chosen.filter(isSocialPost).length,
  };
}

export const reviewCap = (n: number): number => Math.min(n, BATCH_MAX);

/** Split *ids* into requests of at most BULK_MAX. */
export function chunks(ids: string[]): string[][] {
  const out: string[][] = [];
  for (let i = 0; i < ids.length; i += BULK_MAX) out.push(ids.slice(i, i + BULK_MAX));
  return out;
}

/** Join the answers of several bulk requests into one. */
export function joinAnswers(answers: BulkAnswer[]): BulkAnswer {
  const results = answers.flatMap((answer) => answer.results);
  return {
    results,
    succeeded: results.filter((r) => r.ok).length,
    failed: results.filter((r) => !r.ok).length,
    until: answers.find((a) => a.until)?.until,
  };
}

export interface BulkWords {
  /** "Dismissed 7, 2 could not be dismissed": the line the bar says. */
  line: string;
  /** The ids that failed, which stay selected. */
  failed: string[];
  /** Why each failed, once per distinct reason, with how many. */
  reasons: Array<{ error: string; count: number }>;
}

const PAST: Record<"dismiss" | "snooze", [string, string]> = {
  dismiss: ["Dismissed", "dismissed"],
  snooze: ["Snoozed", "snoozed"],
};

/** The words for a bulk answer: the counts, and the reasons the failures give. */
export function bulkWords(action: "dismiss" | "snooze", answer: BulkAnswer): BulkWords {
  const [word, lower] = PAST[action];
  const failed = answer.results.filter((r: BulkResult) => !r.ok);
  const reasons = new Map<string, number>();
  for (const r of failed) reasons.set(r.error || `status ${r.status}`, (reasons.get(r.error || `status ${r.status}`) ?? 0) + 1);
  const bad = failed.length ? `, ${failed.length} could not be ${lower}` : "";
  return {
    line: `${word} ${answer.succeeded}${bad}`,
    failed: failed.map((r) => r.id),
    reasons: [...reasons].map(([error, count]) => ({ error, count })),
  };
}

const WEEKDAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];

/** "Mon 9:00": when a snooze ends, on the viewer's own clock (24-hour, no leading zero on the hour). */
export function untilText(iso: string | undefined): string {
  const at = iso ? new Date(iso) : null;
  if (!at || Number.isNaN(at.getTime())) return "";
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${WEEKDAYS[at.getDay()]} ${at.getHours()}:${pad(at.getMinutes())}`;
}

export const SNOOZE_CHOICES: Array<{ id: "tomorrow" | "monday" | "week"; label: string; title: string }> = [
  { id: "tomorrow", label: "Tomorrow", title: "Hide it until tomorrow at 9:00" },
  { id: "monday", label: "Monday", title: "Hide it until Monday at 9:00" },
  { id: "week", label: "In a week", title: "Hide it for seven days" },
];

/** "Snoozed until Mon 9:00; back sooner if it gets worse". */
export const snoozedLine = (until: string | undefined): string => {
  const when = untilText(until);
  return `Snoozed${when ? ` until ${when}` : ""}; back sooner if it gets worse`;
};
