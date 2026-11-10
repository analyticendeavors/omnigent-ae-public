// Every Needs you request the page makes must pass the host bridge's real request parser
// (upstream web/src/extensions/services/serverRequest.ts, patch 0002). A path outside its
// AE_ROUTE pattern is refused before any network call, which is how every Dismiss, Restore and
// Review failed when the item id sat in the path (review round 1, 2026-11-03). The parser is
// imported from upstream/ itself, not copied.
import type { ExtensionContext } from "@omnigent/extension-sdk";
import { describe, expect, it, vi } from "vitest";

vi.mock("@omnigent/extension-sdk", () => ({ defineExtension: vi.fn() }));

import { dismissAttention, fetchAttention, restoreAttention, startAttentionReview } from "../src/api";
import { parseServerRequest } from "../../../../upstream/web/src/extensions/services/serverRequest";

const IDS = [
  "git:pr-failing:contoso/web#12",
  "git:branch:contoso/web:feat/a b?c&d=e%20f",
  "box:mount:/:space",
  "git:sweep:dependency-sweep",
];

function recorder() {
  const seen: Array<{ method: string; path: string; body: unknown }> = [];
  const request = vi.fn(async (call: { method: string; path: string; body?: unknown }) => {
    seen.push({ method: call.method, path: call.path, body: call.body ?? null });
    return { ok: true, status: 200, body: {} };
  });
  return { seen, context: { server: { request } } as unknown as ExtensionContext };
}

describe("the Needs you requests pass the host bridge's parser", () => {
  it("accepts the list, dismiss, restore and review requests for awkward ids", async () => {
    const { seen, context } = recorder();
    await fetchAttention(context).catch(() => undefined);
    for (const id of IDS) {
      await dismissAttention(context, id, "Known").catch(() => undefined);
      await dismissAttention(context, id, "").catch(() => undefined);
      await restoreAttention(context, id).catch(() => undefined);
      await startAttentionReview(context, id).catch(() => undefined);
      await startAttentionReview(context, id, true).catch(() => undefined);
    }
    expect(seen.length).toBe(1 + IDS.length * 5);
    for (const call of seen) {
      const parsed = parseServerRequest({ method: call.method, path: call.path, body: call.body });
      expect(parsed.kind).toBe("ae");
      expect(parsed.path).toBe(call.path);
    }
    // The id travels in the body, never in the path.
    for (const call of seen.filter((c) => c.method === "POST")) {
      expect(call.path).toMatch(/^\/v1\/ae\/attention\/(dismiss|restore|session)$/);
      expect(IDS).toContain((call.body as { id: string }).id);
    }
  });

  it("would have refused the old path form (the parser is the real one)", () => {
    expect(() =>
      parseServerRequest({
        method: "POST",
        path: `/v1/ae/attention/${encodeURIComponent(IDS[0]!)}/dismiss`,
        body: {},
      }),
    ).toThrow();
  });
});
