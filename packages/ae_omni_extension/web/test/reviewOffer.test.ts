import type { ExtensionContext } from "@omnigent/extension-sdk";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { OpenPrs, PrCard, PrSessionRef } from "../src/api";
import { renderPrsSection, resetPrsState } from "../src/prs";
import { moreOpenText, splitReviewOffer } from "../src/reviewOffer";
import { resetSectionState } from "../src/sections";

// 2026-09-25: a session that had opened four pull requests was offered review
// after the first merge, while three were still open. Review now waits for the
// session's last open pull request.

const SHA = "a1b2c3d4e5f60718293a4b5c6d7e8f9012345678";
const S1: PrSessionRef = { id: "s1", title: "Merge button", status: "working" };
const S2: PrSessionRef = { id: "s2", title: "Lot search", status: "working" };

function card(
  number: number,
  sessions: PrSessionRef[] = [S1],
  extra: Partial<PrCard> = {},
): PrCard {
  return {
    repo: "analyticendeavors/omnigent-ae",
    number,
    url: `https://github.com/analyticendeavors/omnigent-ae/pull/${number}`,
    title: `PR ${number}`,
    state: "open",
    draft: false,
    author: "ae-owner",
    head_ref: `claude/branch-${number}`,
    base_ref: "main",
    head_sha: SHA,
    updated_at: "2026-09-25T10:00:00Z",
    ci: { state: "passing", passing: 6, failing: 0, pending: 0, total: 6 },
    mergeable: { state: "clean", detail: "No conflicts with the base branch." },
    review: { state: "none", approvals: 0, changes_requested: 0, requested: [] },
    merge: { methods: ["squash"], default_method: "squash", allowed: true, reason: null },
    sessions: sessions.map((ref) => ({ ...ref })),
    ...extra,
  };
}

const prs = (items: PrCard[]): OpenPrs => ({ configured: true, items, errors: [] });

function stubContext() {
  const request = vi.fn(async () => ({
    ok: true,
    status: 200,
    body: {
      merged: true,
      repo: "analyticendeavors/omnigent-ae",
      number: 1,
      method: "squash",
      sha: "m3rg3d0000",
      message: "Pull Request successfully merged",
      session: null,
    },
  }));
  const context = {
    server: { request },
    navigation: { openSession: vi.fn(), openNewSession: vi.fn(), openPage: vi.fn() },
    storage: { user: { get: vi.fn(async () => null), set: vi.fn(async () => undefined) } },
  } as unknown as ExtensionContext;
  return { context, refresh: vi.fn(async () => undefined) };
}

const flush = () => new Promise((resolve) => setTimeout(resolve, 0));

function buttons(root: ParentNode): string[] {
  return [...root.querySelectorAll("button")].map((node) => node.textContent?.trim() ?? "");
}

beforeEach(() => {
  resetPrsState();
  resetSectionState();
  document.body.replaceChildren();
});

afterEach(() => {
  document.body.replaceChildren();
});

describe("splitReviewOffer", () => {
  const url = (n: number) => card(n).url;

  it("offers review when the merged pull request was the session's only open one", () => {
    expect(splitReviewOffer([S1], url(1), [card(1)])).toEqual({ offer: [S1], waiting: [] });
    expect(splitReviewOffer([S1], url(1), [card(2, [S2])])).toEqual({ offer: [S1], waiting: [] });
  });

  it("holds a session back while its other pull requests are open", () => {
    expect(splitReviewOffer([S1], url(1), [card(1), card(2), card(3), card(4)])).toEqual({
      offer: [],
      waiting: [{ ref: S1, open: 3 }],
    });
  });

  it("decides per session on a card several sessions track", () => {
    expect(splitReviewOffer([S1, S2], url(1), [card(1, [S1, S2]), card(2, [S2])])).toEqual({
      offer: [S1],
      waiting: [{ ref: S2, open: 1 }],
    });
  });

  it("matches the merged URL whatever its case, and skips sessions already in review", () => {
    const upper = url(1).replace("analyticendeavors", "AnalyticEndeavors");
    expect(splitReviewOffer([S1], upper, [card(1)])).toEqual({ offer: [S1], waiting: [] });
    const done = { ...S1, status: "review" as const };
    expect(splitReviewOffer([done], url(1), [card(2)])).toEqual({ offer: [], waiting: [] });
  });

  it("says how many are left", () => {
    expect(moreOpenText({ ref: S1, open: 3 }, false)).toBe(
      "3 more open pull requests in this session.",
    );
    expect(moreOpenText({ ref: S2, open: 1 }, true)).toBe(
      '"Lot search" has 1 more open pull request.',
    );
  });
});

describe("the merged card's review offer", () => {
  it("waits for the session's last open pull request", async () => {
    const deps = stubContext();
    const target = document.createElement("div");
    document.body.appendChild(target);
    renderPrsSection(
      target,
      { data: prs([card(1), card(2), card(3), card(4)]), error: null },
      deps,
    );
    const first = target.querySelector<HTMLElement>('[data-pr$="/pull/1"]')!;
    [...first.querySelectorAll("button")].find((b) => b.textContent === "Merge")!.click();
    [...first.querySelectorAll("button")]
      .find((b) => b.textContent === "Squash into main")!
      .click();
    await flush();
    let done = target.querySelector<HTMLElement>(".card-pr-merged")!;
    expect(buttons(done)).not.toContain("Mark session done");
    expect(done.querySelector(".pr-more-open")?.textContent).toBe(
      "3 more open pull requests in this session.",
    );

    // The refresh after the merge drops #1; three are still open.
    renderPrsSection(target, { data: prs([card(2), card(3), card(4)]), error: null }, deps);
    done = target.querySelector<HTMLElement>(".card-pr-merged")!;
    expect(buttons(done)).not.toContain("Mark session done");

    // The other three merged elsewhere: now the session is done.
    renderPrsSection(target, { data: prs([]), error: null }, deps);
    done = target.querySelector<HTMLElement>(".card-pr-merged")!;
    expect(buttons(done)).toContain("Mark session done");
    expect(done.querySelector(".pr-more-open")).toBeNull();
  });
});

// 2026-09-25 (https://github.com/analyticendeavors/omnigent-ae/pull/50): a pull
// request merged from this page stays in the server's cached list for up to a
// minute; it must not count as the session's other open pull request.
describe("the review offer with a cached list", () => {
  it("does not count a pull request this page merged as still open", async () => {
    const deps = stubContext();
    const target = document.createElement("div");
    document.body.appendChild(target);
    const merge = async (n: number) => {
      const node = target.querySelector<HTMLElement>(`[data-pr$="/pull/${n}"]`)!;
      [...node.querySelectorAll("button")].find((b) => b.textContent === "Merge")!.click();
      [...node.querySelectorAll("button")]
        .find((b) => b.textContent === "Squash into main")!
        .click();
      await flush();
    };
    renderPrsSection(target, { data: prs([card(1), card(2)]), error: null }, deps);
    await merge(1);
    // The cached answer still lists both.
    renderPrsSection(target, { data: prs([card(1), card(2)]), error: null }, deps);
    await merge(2);
    renderPrsSection(target, { data: prs([card(1), card(2)]), error: null }, deps);
    const done = [...target.querySelectorAll<HTMLElement>(".card-pr-merged")];
    expect(done).toHaveLength(2);
    for (const node of done) {
      expect(buttons(node)).toContain("Mark session done");
      expect(node.querySelector(".pr-more-open")).toBeNull();
    }
  });
});
