import type { ExtensionContext } from "@omnigent/extension-sdk";
import { describe, expect, it, vi } from "vitest";
import { fetchLimits, type Limits, shortLabel, shownLimits, tone } from "../src/limits";

const NOW = Date.parse("2026-09-24T16:00:00Z");

const LIMITS: Limits = {
  now: "2026-09-24T16:00:00Z",
  harnesses: {
    claude: {
      harness: "claude",
      observed_at: "2026-09-24T15:56:00Z",
      plan: null,
      note: null,
      windows: [
        {
          id: "five_hour",
          label: "5-hour limit",
          used_percent: 37.5,
          resets_at: "2026-09-24T18:00:00Z",
          window_minutes: 300,
        },
        {
          id: "seven_day",
          label: "Weekly, all models",
          used_percent: 92,
          resets_at: "2026-09-27T16:00:00Z",
          window_minutes: 10080,
        },
      ],
    },
    codex: {
      harness: "codex",
      observed_at: "2026-09-24T15:58:00Z",
      plan: "pro",
      note: null,
      windows: [
        {
          id: "codex.primary",
          label: "5-hour limit",
          used_percent: 71,
          resets_at: null,
          window_minutes: 300,
        },
        {
          id: "codex.secondary",
          label: "Weekly limit",
          used_percent: 44,
          resets_at: null,
          window_minutes: 10080,
        },
        {
          id: "codex_spark.primary",
          label: "Spark: 5-hour limit",
          used_percent: 3,
          resets_at: null,
          window_minutes: 300,
        },
      ],
    },
  },
};

describe("limits in the strip", () => {
  it("shortens window names and tones percentages", () => {
    expect(shortLabel(LIMITS.harnesses.claude!.windows[0])).toBe("5h");
    expect(shortLabel(LIMITS.harnesses.claude!.windows[1])).toBe("wk");
    expect(
      shortLabel({
        id: "s",
        label: "Spend limit",
        used_percent: 1,
        resets_at: null,
        window_minutes: null,
      }),
    ).toBe("spend");
    expect([tone(10), tone(70), tone(90)]).toEqual(["ok", "warn", "err"]);
  });

  it("names two windows per harness, every window and the reading's time in the title", () => {
    const shown = shownLimits(LIMITS, NOW);
    expect(shown.map((row) => row.harness)).toEqual(["claude", "codex"]);
    expect(shown.map((row) => row.windows.map((w) => shortLabel(w)))).toEqual([
      ["5h", "wk"],
      ["5h", "wk"],
    ]);
    expect(shown[1].title).toContain("Codex (pro)");
    expect(shown[1].title).toContain("Spark: 5-hour limit: 3% used");
    expect(shown[0].title).toMatch(/Weekly, all models: 92% used, resets /);
  });

  it("names nothing without a reading", () => {
    expect(shownLimits(null)).toEqual([]);
    expect(shownLimits({ now: LIMITS.now, harnesses: { claude: null, codex: null } })).toEqual([]);
  });

  it("fetches through server.request and throws on a failure", async () => {
    const request = vi.fn(async (): Promise<{ ok: boolean; status: number; body: unknown }> => ({
      ok: true,
      status: 200,
      body: LIMITS,
    }));
    const context = { server: { request } } as unknown as ExtensionContext;
    await expect(fetchLimits(context)).resolves.toEqual(LIMITS);
    expect(request).toHaveBeenCalledWith({ method: "GET", path: "/v1/ae/limits" });
    request.mockResolvedValueOnce({ ok: false, status: 404, body: null });
    await expect(fetchLimits(context)).rejects.toThrow("404");
  });
});
