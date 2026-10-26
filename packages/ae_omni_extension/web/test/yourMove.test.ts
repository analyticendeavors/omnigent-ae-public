import type { ExtensionContext } from "@omnigent/extension-sdk";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { PrCard, YourMove } from "../src/api";
import { resetLotState } from "../src/lot";
import { resetPrsState } from "../src/prs";
import { resetSectionState } from "../src/sections";
import { counts, renderYourMoveSection, resetYourMoveState } from "../src/yourMove";

const DATA: YourMove = {
  now: "2026-09-24T09:00:00-07:00",
  today: "2026-09-24",
  timezone: "America/Los_Angeles",
  total: 5,
  cached: false,
  sessions: {
    ok: true,
    counts: { gate: 1, awaiting: 1, blocked: 0, review: 1 },
    items: [
      {
        id: "conv_gate",
        title: "Pricing padding",
        kind: "gate",
        kinds: ["gate"],
        status: "working",
        fork: "EUR formatting is broken",
        next: null,
        task: null,
        pending: 0,
        updated_at: 1,
        since: "12m",
      },
      {
        id: "conv_ask",
        title: "Renewal note",
        kind: "awaiting",
        kinds: ["awaiting"],
        status: "working",
        fork: null,
        next: "send it",
        task: null,
        pending: 2,
        updated_at: 2,
        since: "3m",
      },
      {
        id: "conv_review",
        title: "Split view",
        kind: "review",
        kinds: ["review"],
        status: "review",
        fork: null,
        next: null,
        task: null,
        pending: 0,
        updated_at: 3,
        since: "1h",
      },
    ],
  },
  lot: {
    ok: true,
    today: "2026-09-24",
    items: [
      {
        id: "T1",
        title: "contoso-vendor",
        quadrant: "q2",
        estimate: "15",
        status: "open",
        tags: ["ae"],
        created_at: null,
        resurfaces: "2026-09-20",
        next: "Ask Alex",
        links: [],
        url: "https://tasks.example/#/all?q=contoso",
        overdue: true,
      },
    ],
  },
  steward: {
    ok: true,
    agent: "steward",
    brief: {
      session_id: "conv_steward",
      title: "AE steward",
      created_at: 1_790_000_000,
      live_status: "idle",
      message_at: 1_790_000_100,
      text: "Two items resurface today.",
      truncated: false,
    },
  },
  pulls: {
    ok: true,
    configured: true,
    login: "ae-reid-havens",
    repos: ["analyticendeavors/omnigent-ae"],
    errors: [],
    items: [
      {
        repo: "analyticendeavors/omnigent-ae",
        number: 23,
        title: "Add the Your move panel",
        url: "https://github.com/analyticendeavors/omnigent-ae/pull/23",
        author: "ae-reid-havens",
        draft: false,
        updated_at: null,
        why: ["author"],
        ci: "failing",
      },
    ],
  },
};

const SHA = "a1b2c3d4e5f60718293a4b5c6d7e8f9012345678";

/** Pull request 23's full card, as `GET /v1/ae/prs/{owner}/{repo}/{number}` answers. */
function card23(extra: Partial<PrCard> = {}): PrCard {
  return {
    repo: "analyticendeavors/omnigent-ae",
    number: 23,
    url: "https://github.com/analyticendeavors/omnigent-ae/pull/23",
    title: "Add the Your move panel",
    state: "open",
    draft: false,
    author: "ae-reid-havens",
    head_ref: "claude/your-move",
    base_ref: "main",
    head_sha: SHA,
    updated_at: null,
    ci: { state: "passing", passing: 6, failing: 0, pending: 0, total: 6 },
    mergeable: { state: "clean", detail: "No conflicts with the base branch." },
    review: { state: "none", approvals: 0, changes_requested: 0, requested: [] },
    merge: { methods: ["squash", "merge"], default_method: "squash", allowed: true, reason: null },
    ...extra,
  };
}

type Reply = { ok: boolean; status: number; body: unknown };

/**
 * A stub host. A card read answers *cards* in turn (the last one repeats);
 * every other request answers *replies* in turn (a promise holds it in
 * flight), then a gate reply.
 */
function stubContext(
  cards: Reply[] = [{ ok: true, status: 200, body: card23() }],
  replies: (Reply | Promise<Reply>)[] = [],
) {
  const request = vi.fn(async ({ method, path }: { method: string; path: string }) => {
    if (method === "GET" && path.startsWith("/v1/ae/prs/")) {
      return cards.length > 1 ? cards.shift()! : cards[0];
    }
    return (
      replies.shift() ?? { ok: true, status: 200, body: { posted: true, route: "A", text: "A" } }
    );
  });
  const openSession = vi.fn(async () => undefined);
  const openPage = vi.fn(async () => undefined);
  const openExternal = vi.fn(async (_url: string) => undefined);
  const context = {
    server: { request },
    sessions: { listAll: vi.fn(async () => []) },
    navigation: { openSession, openPage, openExternal, openNewSession: vi.fn() },
    storage: { user: { get: vi.fn(async () => null), set: vi.fn(async () => undefined) } },
  } as unknown as ExtensionContext;
  return { context, request, openSession, openPage, openExternal };
}

const flush = () => new Promise((resolve) => setTimeout(resolve, 0));

function hosts(): HTMLElement {
  const host = document.createElement("div");
  document.body.append(host);
  return host;
}

function buttonNamed(root: ParentNode, name: string): HTMLButtonElement {
  const found = [...root.querySelectorAll<HTMLButtonElement>("button")].find(
    (node) => (node.getAttribute("aria-label") ?? node.textContent?.trim()) === name,
  );
  if (!found) throw new Error(`no button ${name}`);
  return found;
}

describe("Your move", () => {
  beforeEach(() => {
    resetLotState();
    resetSectionState();
    resetPrsState();
    resetYourMoveState();
    document.body.replaceChildren();
  });

  afterEach(() => vi.restoreAllMocks());

  it("counts gates, approvals, due items and pull requests, not review", () => {
    const { context } = stubContext();
    const target = hosts();
    renderYourMoveSection(target, { data: DATA, error: null }, { context, refresh: vi.fn() });
    expect(counts(DATA)).toEqual({ sessions: 2, due: 1, pulls: 1, total: 4 });
    expect(target.querySelector(".section-h .count")?.textContent).toBe("4");
    // Review has its own Dashboard section; it is not repeated here.
    expect(target.querySelector('[data-session="conv_review"]')).toBeNull();
    expect([...target.querySelectorAll(".ym-group-h")].map((node) => node.textContent)).toEqual([
      "Due from the lot (1)",
      "Pull requests (1)",
    ]);
  });

  it("answers a gate in place and refreshes past the cache", async () => {
    const { context, request } = stubContext();
    const refresh = vi.fn(async () => undefined);
    const target = hosts();
    renderYourMoveSection(target, { data: DATA, error: null }, { context, refresh });
    const gate = target.querySelector<HTMLElement>('[data-session="conv_gate"]')!;
    expect(gate.textContent).toContain("EUR formatting is broken");
    expect([...gate.querySelectorAll("button")].map((node) => node.textContent?.trim())).toEqual([
      "Do it now",
      "Write a note",
      "Save for later",
      "Drop it",
      "Open",
    ]);
    buttonNamed(gate, "Write a note").click();
    await flush();
    expect(request).toHaveBeenCalledWith({
      method: "POST",
      path: "/v1/ae/gate/conv_gate/reply",
      body: { route: "B" },
    });
    expect(refresh).toHaveBeenCalledWith(true);
  });

  // 2026-09-27: the labels say what each answer does; what they post is unchanged.
  it("posts A, B, C and close from the plain-action labels", async () => {
    const expected: [string, string][] = [
      ["Do it now", "A"],
      ["Write a note", "B"],
      ["Save for later", "C"],
      ["Drop it", "close"],
    ];
    for (const [label, route] of expected) {
      const { context, request } = stubContext();
      const target = hosts();
      renderYourMoveSection(target, { data: DATA, error: null }, { context, refresh: vi.fn() });
      const gate = target.querySelector<HTMLElement>('[data-session="conv_gate"]')!;
      const node = buttonNamed(gate, label);
      expect(node.title.startsWith(route === "close" ? "Close:" : `${route},`)).toBe(true);
      node.click();
      await flush();
      expect(request).toHaveBeenCalledWith({
        method: "POST",
        path: "/v1/ae/gate/conv_gate/reply",
        body: { route },
      });
      target.remove();
    }
  });

  it("opens a session that waits on an answer", async () => {
    const { context, openSession } = stubContext();
    const target = hosts();
    renderYourMoveSection(target, { data: DATA, error: null }, { context, refresh: vi.fn() });
    const ask = target.querySelector<HTMLElement>('[data-session="conv_ask"]')!;
    expect(ask.textContent).toContain("Waiting on your answer (2 outstanding)");
    expect(ask.textContent).toContain("send it");
    buttonNamed(ask, "Open").click();
    await flush();
    expect(openSession).toHaveBeenCalledWith("conv_ask");
  });

  it("shows a due lot item with Resume and marks it overdue", () => {
    const { context } = stubContext();
    const target = hosts();
    renderYourMoveSection(target, { data: DATA, error: null }, { context, refresh: vi.fn() });
    const card = target.querySelector<HTMLElement>('[data-task="T1"]')!;
    expect(card.classList.contains("is-overdue")).toBe(true);
    expect(card.querySelector(".resurface")?.textContent).toBe("overdue since Sep 20");
    expect(card.querySelector(".btn-resume")).not.toBeNull();
  });

  it("gives a pull request its CI state and Open on GitHub", async () => {
    const { context, openExternal } = stubContext();
    const target = hosts();
    renderYourMoveSection(target, { data: DATA, error: null }, { context, refresh: vi.fn() });
    const pull = target.querySelector<HTMLElement>(".card-pull")!;
    expect(pull.classList.contains("card-ci-failing")).toBe(true);
    expect(pull.textContent).toContain("analyticendeavors/omnigent-ae#23");
    expect(pull.textContent).toContain("CI failing");
    expect(pull.querySelector("code")?.textContent).toBe(
      "https://github.com/analyticendeavors/omnigent-ae/pull/23",
    );
    buttonNamed(pull, "Open on GitHub").click();
    await flush();
    expect(openExternal).toHaveBeenCalledWith(
      "https://github.com/analyticendeavors/omnigent-ae/pull/23",
    );
    // 2026-09-28: the printed URL opens it too.
    const link = pull.querySelector<HTMLButtonElement>(
      'button[aria-label="Open https://github.com/analyticendeavors/omnigent-ae/pull/23 on GitHub"]',
    )!;
    expect(link.closest(".meta.url")).not.toBeNull();
    link.click();
    await flush();
    expect(openExternal).toHaveBeenCalledTimes(2);
    expect(openExternal).toHaveBeenLastCalledWith(
      "https://github.com/analyticendeavors/omnigent-ae/pull/23",
    );
  });

  it("shows the URL to copy, without a clipboard write, when the host will not open it", async () => {
    const { context, openExternal } = stubContext();
    openExternal.mockRejectedValueOnce(
      Object.assign(new Error("The browser blocked the new tab"), { code: "PopupBlocked" }),
    );
    const writeText = vi.fn(async () => undefined);
    Object.defineProperty(navigator, "clipboard", { value: { writeText }, configurable: true });
    const target = hosts();
    renderYourMoveSection(target, { data: DATA, error: null }, { context, refresh: vi.fn() });
    const node = target.querySelector<HTMLElement>(".card-pull")!;
    buttonNamed(node, "Open on GitHub").click();
    await flush();
    await flush();
    // Review of #58: after the host round trip iOS refuses the write, so none is tried.
    expect(writeText).not.toHaveBeenCalled();
    const line = node.querySelector<HTMLElement>(".flash")!;
    expect(line.textContent).toBe(
      "Could not open GitHub from here (The browser blocked the new tab). " +
        "Tap and hold to copy: https://github.com/analyticendeavors/omnigent-ae/pull/23",
    );
    expect(line.querySelector(".link-copy")?.textContent).toBe(
      "https://github.com/analyticendeavors/omnigent-ae/pull/23",
    );
    Reflect.deleteProperty(navigator, "clipboard");
  });

  it("keeps a Copy link icon beside Open on GitHub that copies in the tap", async () => {
    const { context } = stubContext();
    const writeText = vi.fn(async () => undefined);
    Object.defineProperty(navigator, "clipboard", { value: { writeText }, configurable: true });
    const target = hosts();
    renderYourMoveSection(target, { data: DATA, error: null }, { context, refresh: vi.fn() });
    const node = target.querySelector<HTMLElement>(".card-pull")!;
    const open = buttonNamed(node, "Open on GitHub");
    const copy = buttonNamed(node, "Copy link");
    expect(open.nextElementSibling).toBe(copy);
    expect(copy.className).toBe("btn btn-quiet btn-copy");
    copy.click();
    await flush();
    expect(writeText).toHaveBeenCalledWith(
      "https://github.com/analyticendeavors/omnigent-ae/pull/23",
    );
    expect(node.querySelector(".flash")?.textContent).toBe("Copied the link.");
    Reflect.deleteProperty(navigator, "clipboard");
  });

  it("keeps the brief collapsed until asked", () => {
    const { context } = stubContext();
    const target = hosts();
    renderYourMoveSection(target, { data: DATA, error: null }, { context, refresh: vi.fn() });
    const details = target.querySelector<HTMLDetailsElement>("details.brief")!;
    expect(details.open).toBe(false);
    expect(details.textContent).toContain("Two items resurface today.");
  });

  it("renders a failing source inline and the rest as usual", () => {
    const { context } = stubContext();
    const target = hosts();
    const broken: YourMove = {
      ...DATA,
      lot: { ok: false, error: { code: "task_store_unavailable", message: "502 from the edge" } },
      pulls: {
        ok: true,
        configured: false,
        login: "ae-reid-havens",
        repos: [],
        items: [],
        errors: [],
        note: "Pull requests are off: the server has no GitHub token.",
      },
    };
    renderYourMoveSection(target, { data: broken, error: null }, { context, refresh: vi.fn() });
    expect(target.querySelector(".notice-error")?.textContent).toContain("502 from the edge");
    expect(target.querySelectorAll("[data-session]")).toHaveLength(2);
    expect(target.textContent).toContain("the server has no GitHub token");
  });

  it("asks for AE_GITHUB_REPOS when the token is there but no repos are", () => {
    const { context } = stubContext();
    const target = document.createElement("div");
    const noRepos: YourMove = {
      ...DATA,
      pulls: {
        ok: true,
        configured: false,
        login: "",
        repos: [],
        items: [],
        errors: [],
        note: "Pull requests are off: no repos to read.",
        missing: "repos",
      },
    };
    renderYourMoveSection(target, { data: noRepos, error: null }, { context, refresh: vi.fn() });
    expect(target.textContent).toContain("Set AE_GITHUB_REPOS");
    expect(target.textContent).not.toContain("no GitHub token");
  });

  it("collapses to its heading when nothing waits", () => {
    const { context } = stubContext();
    const target = hosts();
    const quiet: YourMove = {
      ...DATA,
      sessions: { ok: true, counts: { gate: 0, awaiting: 0, blocked: 0, review: 0 }, items: [] },
      lot: { ok: true, today: "2026-09-24", items: [] },
      pulls: {
        ...DATA.pulls,
        ok: true,
        configured: true,
        items: [],
        errors: [],
      } as YourMove["pulls"],
      steward: { ok: true, agent: "steward", brief: null },
    };
    renderYourMoveSection(target, { data: quiet, error: null }, { context, refresh: vi.fn() });
    const node = target.querySelector<HTMLElement>("#your-move")!;
    expect(node.classList.contains("is-empty")).toBe(true);
    expect(node.querySelector<HTMLElement>(".section-body")!.hidden).toBe(true);
  });

  it("keeps the brief reachable when nothing else waits", () => {
    const { context } = stubContext();
    const target = hosts();
    const briefOnly: YourMove = {
      ...DATA,
      sessions: { ok: true, counts: { gate: 0, awaiting: 0, blocked: 0, review: 0 }, items: [] },
      lot: { ok: true, today: "2026-09-24", items: [] },
      pulls: {
        ...DATA.pulls,
        ok: true,
        configured: true,
        items: [],
        errors: [],
      } as YourMove["pulls"],
    };
    renderYourMoveSection(target, { data: briefOnly, error: null }, { context, refresh: vi.fn() });
    const node = target.querySelector<HTMLElement>("#your-move")!;
    expect(node.classList.contains("is-empty")).toBe(false);
    expect(node.querySelector(".section-h .count")?.textContent).toBe("0");
    expect(node.querySelector("details.brief")).not.toBeNull();
  });
});

// Merge on Your move's pull request cards (2026-09-24): a pull request no
// session tracks is listed only here, so its card carries the Open PRs card's
// Merge button, confirm step and merged card. The full card is read only when
// Merge is tapped, so the 20-second poll costs no GitHub calls.
describe("Your move merge", () => {
  const CARD_PATH = "/v1/ae/prs/analyticendeavors/omnigent-ae/23";
  const MERGE_PATH = "/v1/ae/prs/analyticendeavors/omnigent-ae/23/merge";
  const RESULT = {
    merged: true,
    repo: "analyticendeavors/omnigent-ae",
    number: 23,
    method: "squash",
    sha: "m3rg3d0000",
    message: "Pull Request successfully merged",
    session: null,
  };
  const OK = (body: unknown): Reply => ({ ok: true, status: 200, body });
  const FAIL: Reply = {
    ok: false,
    status: 502,
    body: { error: { code: "github_failed", message: "GitHub 502" } },
  };

  beforeEach(() => {
    resetLotState();
    resetSectionState();
    resetPrsState();
    resetYourMoveState();
    document.body.replaceChildren();
  });

  afterEach(() => vi.restoreAllMocks());

  const pullOf = (target: HTMLElement) => target.querySelector<HTMLElement>(".card-pull")!;
  type Stub = ReturnType<typeof stubContext>["request"];
  const calls = (request: Stub, path: string) =>
    request.mock.calls.filter(([arg]) => arg.path === path).length;
  /** A reply the test releases by hand, to hold a request in flight. */
  function held(): { promise: Promise<Reply>; release: (reply: Reply) => void } {
    let release: (reply: Reply) => void = () => undefined;
    const promise = new Promise<Reply>((resolve) => {
      release = resolve;
    });
    return { promise, release };
  }
  function setup(cards?: Reply[], replies?: (Reply | Promise<Reply>)[]) {
    const { context, request } = stubContext(cards, replies);
    const refresh = vi.fn(async () => undefined);
    const deps = { context, refresh };
    const target = hosts();
    const render = (data: YourMove = DATA) =>
      renderYourMoveSection(target, { data, error: null }, deps);
    render();
    return { request, refresh, deps, target, render };
  }
  async function tapMerge(target: HTMLElement): Promise<void> {
    buttonNamed(pullOf(target), "Merge").click();
    await flush();
    await flush();
  }

  // Finding 1: the GitHub budget.
  it("reads no card on render or on any poll, and keeps Open on GitHub", async () => {
    const { request, target, render } = setup();
    render();
    render();
    await flush();
    expect(calls(request, CARD_PATH)).toBe(0);
    const pull = pullOf(target);
    expect(buttonNamed(pull, "Merge").disabled).toBe(false);
    expect(buttonNamed(pull, "Open on GitHub")).toBeTruthy();
  });

  it("reads the card on a Review details tap and then shows the details and a draft's Mark ready", async () => {
    const { request, target } = setup([{ ok: true, status: 200, body: card23({ draft: true }) }]);
    buttonNamed(pullOf(target), "Review details").click();
    await flush();
    expect(calls(request, CARD_PATH)).toBe(1);
    const pull = pullOf(target);
    expect(pull.querySelector<HTMLDetailsElement>("details.pr-details")?.open).toBe(true);
    expect(buttonNamed(pull, "Mark ready for review")).toBeTruthy();
    expect(pull.querySelector(".tag")?.textContent).toBe("draft");
  });

  it("reads the card on a tap and opens the confirm step when the gate is open", async () => {
    const { request, target } = setup();
    await tapMerge(target);
    expect(request).toHaveBeenCalledWith({ method: "GET", path: CARD_PATH, body: undefined });
    const confirm = target.querySelector<HTMLElement>(".pr-confirm")!;
    expect(buttonNamed(confirm, "Squash into main")).toBeTruthy();
  });

  it("opens the confirm step on the recommended method with its reason (2026-09-28)", async () => {
    const why =
      "Merge commit keeps release/1.0 and main in step; a squash would make the next promotion conflict.";
    const promotion = card23({
      head_ref: "release/1.0",
      merge: {
        methods: ["squash", "merge"],
        default_method: "merge",
        recommended_method: "merge",
        recommended_reason: why,
        allowed: true,
        reason: null,
      },
    });
    const { target } = setup([OK(promotion)]);
    await tapMerge(target);
    const confirm = target.querySelector<HTMLElement>(".pr-confirm")!;
    expect(buttonNamed(confirm, "Merge into main")).toBeTruthy();
    expect(confirm.querySelector<HTMLInputElement>('input[value="merge"]')!.checked).toBe(true);
    // 2026-10-14: the reason leads with "Recommended"; the segment keeps it as its title.
    expect(confirm.querySelector(".pr-confirm-why")?.textContent).toBe(`Recommended · ${why}`);
    expect(confirm.querySelector<HTMLLabelElement>(".pr-method[title]")?.title).toBe(why);
    confirm.querySelector<HTMLInputElement>('input[value="squash"]')!.closest("label")!.click();
    expect(buttonNamed(confirm, "Squash into main")).toBeTruthy();
  });

  it("shows the reason and turns Merge off when the gate is shut", async () => {
    const shut = card23({
      merge: {
        methods: ["squash"],
        default_method: "squash",
        allowed: false,
        reason: "CI is failing: 1 of 6 checks.",
      },
    });
    const { target } = setup([OK(shut)]);
    await tapMerge(target);
    const pull = pullOf(target);
    expect(pull.querySelector(".pr-why")?.textContent).toBe("CI is failing: 1 of 6 checks.");
    expect(buttonNamed(pull, "Merge").disabled).toBe(true);
    expect(pull.querySelector(".pr-confirm")).toBeNull();
    // 2026-09-27: the CI line, the reason and the buttons share one status row.
    const row = pull.querySelector<HTMLElement>(".pr-status")!;
    expect([...row.children].map((part) => part.className)).toEqual([
      "card-sub",
      "pr-why",
      "actions",
    ]);
  });

  it("reuses a read for a minute: Cancel, then Merge again, reads nothing", async () => {
    const { request, target } = setup();
    await tapMerge(target);
    buttonNamed(target, "Cancel").click();
    await tapMerge(target);
    expect(target.querySelector(".pr-confirm")).not.toBeNull();
    expect(calls(request, CARD_PATH)).toBe(1);
  });

  it("merges with the card's head SHA and the chosen method, then refreshes", async () => {
    const { request, refresh, target } = setup(undefined, [OK({ ...RESULT, method: "merge" })]);
    await tapMerge(target);
    const confirm = target.querySelector<HTMLElement>(".pr-confirm")!;
    const mergeCommit = confirm.querySelector<HTMLInputElement>('input[value="merge"]')!;
    mergeCommit.checked = true;
    mergeCommit.dispatchEvent(new Event("change"));
    buttonNamed(confirm, "Merge into main").click();
    await flush();
    expect(request).toHaveBeenCalledWith({
      method: "POST",
      path: MERGE_PATH,
      body: { sha: SHA, method: "merge" },
    });
    expect(refresh).toHaveBeenCalledWith(true);
    const done = target.querySelector<HTMLElement>(".card-pr-merged")!;
    expect(done.querySelector(".pr-merged-text")?.textContent).toBe(
      "Merged #23 (merge commit) m3rg3d0",
    );
    expect(done.textContent).not.toContain("successfully merged");
  });

  it("shows the server's refusal and the fresh card", async () => {
    const moved = card23({
      head_sha: "ffffffffffffffffffffffffffffffffffffffff",
      ci: { state: "pending", passing: 5, failing: 0, pending: 1, total: 6 },
      merge: {
        methods: ["squash", "merge"],
        default_method: "squash",
        allowed: false,
        reason: "CI is still running: 5 of 6 checks passed, 1 to go.",
      },
    });
    const { target } = setup(undefined, [
      {
        ok: false,
        status: 409,
        body: {
          error: {
            code: "head_moved",
            message: "New commits arrived since the card was read; look again, then merge.",
            card: moved,
          },
        },
      },
    ]);
    await tapMerge(target);
    buttonNamed(target, "Squash into main").click();
    await flush();
    const pull = pullOf(target);
    expect(pull.querySelector(".flash")?.textContent).toBe(
      "Not merged: New commits arrived since the card was read; look again, then merge.",
    );
    expect(pull.querySelector(".pr-why")?.textContent).toBe("CI is still running: 5 of 6 checks passed, 1 to go.");
    expect(buttonNamed(pull, "Merge").disabled).toBe(true);
  });

  // Finding 2: the count agrees with the list.
  it("leaves a pull request merged here out of the count", async () => {
    const { target, render } = setup(undefined, [OK(RESULT)]);
    await tapMerge(target);
    buttonNamed(target, "Squash into main").click();
    await flush();
    // The cached answer still lists it; the count and the list both drop it.
    render();
    expect(counts(DATA)).toEqual({ sessions: 2, due: 1, pulls: 0, total: 3 });
    expect(target.querySelector(".section-h .count")?.textContent).toBe("3");
    expect(target.textContent).toContain("Pull requests (0)");
  });

  // Finding 3: Dismiss does not resurrect it.
  it("keeps a dismissed merge out of the list for the cache window, then reads afresh", async () => {
    const { request, target, render } = setup(undefined, [OK(RESULT)]);
    await tapMerge(target);
    buttonNamed(target, "Squash into main").click();
    await flush();
    render();
    expect(target.querySelector(".card-pr-merged")).not.toBeNull();
    expect(target.querySelector(".card-pull")).toBeNull();
    buttonNamed(target, "Dismiss").click();
    render();
    expect(target.querySelector(".card-pr-merged")).toBeNull();
    expect(target.querySelector(".card-pull")).toBeNull();
    // Past the window a listing that still has it shows an open card with no
    // held state: no reason, no confirm, and a tap reads it again.
    const later = Date.now() + 81_000;
    vi.spyOn(Date, "now").mockReturnValue(later);
    render();
    const pull = pullOf(target);
    expect(pull.querySelector(".pr-why")).toBeNull();
    expect(pull.querySelector(".pr-confirm")).toBeNull();
    await tapMerge(target);
    expect(calls(request, CARD_PATH)).toBe(2);
  });

  // Finding 5: no redraw while the merge is in flight.
  it("keeps the card it merged from through a re-render, and the result lands on it", async () => {
    const post = held();
    const { target, render } = setup(undefined, [post.promise]);
    await tapMerge(target);
    const node = pullOf(target);
    buttonNamed(node, "Squash into main").click();
    render();
    render();
    expect(pullOf(target)).toBe(node);
    expect(node.isConnected).toBe(true);
    post.release(OK(RESULT));
    await flush();
    await flush();
    expect(target.querySelector(".card-pr-merged")?.isConnected).toBe(true);
    expect(target.querySelector(".card-pull")).toBeNull();
  });

  // Finding 6: one POST.
  it("turns Confirm off while the merge runs and sends one POST however often it is tapped", async () => {
    const post = held();
    const { request, target, render } = setup(undefined, [post.promise]);
    await tapMerge(target);
    const confirm = buttonNamed(target, "Squash into main");
    confirm.click();
    expect(confirm.disabled).toBe(true);
    confirm.click();
    render();
    buttonNamed(target, "Squash into main").click();
    post.release(OK(RESULT));
    await flush();
    await flush();
    expect(calls(request, MERGE_PATH)).toBe(1);
  });

  // Finding 7: pruning.
  it("forgets a card once the list no longer has its pull request", async () => {
    const { request, target, render } = setup();
    await tapMerge(target);
    buttonNamed(target, "Cancel").click();
    render({ ...DATA, pulls: { ...DATA.pulls, items: [] } as YourMove["pulls"] });
    render();
    // Within the minute, but the read was dropped with the card: a tap reads again.
    await tapMerge(target);
    expect(calls(request, CARD_PATH)).toBe(2);
  });

  // Findings 8 and 10: a failed read.
  it("keeps the last good card when a later read fails, and Merge stays on", async () => {
    const { request, target, render } = setup([OK(card23()), FAIL]);
    await tapMerge(target);
    buttonNamed(target, "Cancel").click();
    const later = Date.now() + 61_000;
    vi.spyOn(Date, "now").mockReturnValue(later);
    render();
    await tapMerge(target);
    expect(calls(request, CARD_PATH)).toBe(2);
    const pull = pullOf(target);
    expect(pull.querySelector(".pr-why")?.textContent).toBe(
      "Showing the last read; reading it again failed: GitHub 502. " +
        "Tap Merge to try again, or Open on GitHub to merge there.",
    );
    // The last good card's gate was open: the confirm step opens on it (the
    // server reads the card fresh again before it merges).
    expect(pull.querySelector(".pr-confirm")).not.toBeNull();
    expect(buttonNamed(pull, "Merge").disabled).toBe(false);
  });

  it("says what to do when the first read fails", async () => {
    const { target } = setup([FAIL]);
    await tapMerge(target);
    const pull = pullOf(target);
    expect(pull.querySelector(".pr-why")?.textContent).toBe(
      "Could not check whether it can merge: GitHub 502. " +
        "Tap Merge to try again, or Open on GitHub to merge there.",
    );
    expect(buttonNamed(pull, "Merge").disabled).toBe(false);
    expect(buttonNamed(pull, "Open on GitHub")).toBeTruthy();
  });
});
