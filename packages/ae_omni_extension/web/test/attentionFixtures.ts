// Needs you fixtures (2026-11-03): items and answers shaped like `GET /v1/ae/attention`
// (ae_omni_policies/attention.py), shared by the panel's, the Git card's and the Box card's tests.
import { heldByWorkingChat, type AttentionItem, type AttentionOverview, type AttentionSource } from "../src/api";

let counter = 0;

/** An urgent failing-PR item of the Git source; each test passes what it is about. */
export function item(id: string, fields: Partial<AttentionItem> = {}): AttentionItem {
  counter += 1;
  return {
    id,
    source: "git",
    kind: "pr-failing",
    tone: "urgent",
    title: `Fix #${counter} Bump thing`,
    meta: "web · checks failing",
    url: `https://github.com/contoso/web/pull/${counter}`,
    fingerprint: `fp-${id}`,
    dismissible: true,
    actions: ["review", "dismiss"],
    back_if: "a new commit still fails",
    expires_days: 3,
    offer: null,
    ...fields,
  };
}

export function overview(
  items: AttentionItem[],
  fields: Partial<AttentionOverview> = {},
): AttentionOverview {
  const counted = items.filter((entry) => !heldByWorkingChat(entry));
  const by_source: Record<string, { urgent: number; review: number }> = {};
  for (const entry of counted) {
    const row = (by_source[entry.source] ??= { urgent: 0, review: 0 });
    if (entry.tone === "urgent") row.urgent += 1;
    else row.review += 1;
  }
  const sources: AttentionSource[] = ["git", "box"];
  return {
    sources,
    can_chat: true,
    items,
    dismissed: [],
    counts: {
      urgent: counted.filter((entry) => entry.tone === "urgent").length,
      review: counted.filter((entry) => entry.tone === "review").length,
      by_source,
    },
    unknown: [],
    read_at: "2026-11-03T20:00:00Z",
    ...fields,
  };
}

/** `n` items of one kind, ids `<kind>-1` .. `<kind>-n`. */
export function many(
  n: number,
  kind: string,
  fields: Partial<AttentionItem> = {},
): AttentionItem[] {
  return Array.from({ length: n }, (_, i) => item(`${kind}-${i + 1}`, { kind, ...fields }));
}
