import type { ExtensionContext } from "@omnigent/extension-sdk";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { OpenPrs, PrCard, YourMove } from "../src/api";
import { ciText, prCount, renderPrsSection, resetPrsState, withoutCarded } from "../src/prs";
import { resetSectionState } from "../src/sections";

const SHA = "a1b2c3d4e5f60718293a4b5c6d7e8f9012345678";

function card(number: number, extra: Partial<PrCard> = {}): PrCard {
  return {
    repo: "analyticendeavors/omnigent-ae",
    number,
    url: `https://github.com/analyticendeavors/omnigent-ae/pull/${number}`,
    title: `PR ${number}`,
    state: "open",
    draft: false,
    author: "ae-reid-havens",
    head_ref: `claude/branch-${number}`,
    base_ref: "main",
    head_sha: SHA,
    updated_at: "2026-09-24T10:00:00Z",
    ci: { state: "passing", passing: 6, failing: 0, pending: 0, total: 6 },
    mergeable: { state: "clean", detail: "No conflicts with the base branch." },
    review: { state: "none", approvals: 0, changes_requested: 0, requested: [] },
    merge: { methods: ["squash", "merge"], default_method: "squash", allowed: true, reason: null },
    sessions: [{ id: "s1", title: "Merge button", status: "working" }],
    ...extra,
  };
}

function prs(items: PrCard[], extra: Partial<OpenPrs> = {}): OpenPrs {
  return { configured: true, items, errors: [], sessions_scanned: 3, ...extra };
}

type Reply = { ok: boolean; status: number; body: unknown };

function stubContext(replies: Reply[] = []) {
  const request = vi.fn(async () => replies.shift() ?? { ok: true, status: 200, body: {} });
  const openSession = vi.fn(async () => undefined);
  const openExternal = vi.fn(async (_url: string) => undefined);
  const context = {
    server: { request },
    navigation: { openSession, openExternal, openNewSession: vi.fn(), openPage: vi.fn() },
    storage: { user: { get: vi.fn(async () => null), set: vi.fn(async () => undefined) } },
  } as unknown as ExtensionContext;
  const refresh = vi.fn(async () => undefined);
  return { deps: { context, refresh }, request, openSession, openExternal, refresh };
}

const flush = () => new Promise((resolve) => setTimeout(resolve, 0));

function host(): HTMLElement {
  const node = document.createElement("div");
  document.body.appendChild(node);
  return node;
}

function buttonNamed(root: ParentNode, name: string): HTMLButtonElement {
  const found = [...root.querySelectorAll("button")].find(
    (node) => (node.getAttribute("aria-label") ?? node.textContent?.trim()) === name,
  );
  if (!found) throw new Error(`no button named ${name}`);
  return found;
}

beforeEach(() => {
  resetPrsState();
  resetSectionState();
  document.body.replaceChildren();
});

afterEach(() => {
  document.body.replaceChildren();
});

describe("ciText and prCount", () => {
  it("says the CI counts the way the chip shows them", () => {
    expect(ciText(card(1).ci)).toBe("6/6 checks");
    expect(ciText({ state: "failing", passing: 4, failing: 2, pending: 0, total: 6 })).toBe(
      "2 of 6 checks failing",
    );
    expect(ciText({ state: "pending", passing: 5, failing: 0, pending: 1, total: 6 })).toBe(
      "5/6 passed, 1 running",
    );
    expect(ciText({ state: "none", passing: 0, failing: 0, pending: 0, total: 0 })).toBe(
      "No checks",
    );
  });

  it("counts open cards, and says off without a token", () => {
    expect(prCount({ data: null, error: null })).toBeUndefined();
    expect(prCount({ data: prs([card(1), card(2)]), error: null })).toBe(2);
    expect(prCount({ data: { configured: false, items: [], errors: [] }, error: null })).toBe(
      "off",
    );
  });
});

describe("withoutCarded", () => {
  it("drops Your move pull requests that Open PRs shows as cards", () => {
    const pull = (number: number) => ({
      repo: "analyticendeavors/omnigent-ae",
      number,
      title: `PR ${number}`,
      url: `https://github.com/analyticendeavors/omnigent-ae/pull/${number}`,
      author: "ae-reid-havens",
      draft: false,
      updated_at: null,
      why: ["author" as const],
      ci: "passing" as const,
    });
    const yourMove = {
      total: 3,
      pulls: {
        ok: true,
        configured: true,
        login: "ae-reid-havens",
        repos: [],
        items: [pull(1), pull(2), pull(3)],
        errors: [],
      },
    } as unknown as YourMove;
    const left = withoutCarded(yourMove, prs([card(2)]));
    expect(left.pulls.ok && left.pulls.items.map((row) => row.number)).toEqual([1, 3]);
    expect(left.total).toBe(2);
    expect(withoutCarded(yourMove, null)).toBe(yourMove);
  });
});

describe("renderPrsSection", () => {
  it("says the section is off without a token", () => {
    const { deps } = stubContext();
    const target = host();
    renderPrsSection(
      target,
      { data: { configured: false, items: [], errors: [] }, error: null },
      deps,
    );
    expect(target.querySelector(".section-toggle .count")?.textContent).toBe("off");
    expect(target.textContent).toContain("no GitHub token");
  });

  // 2026-10-14: a card whose Merge is live and clean says so in one line; the
  // chips come back only when something needs a look.
  it("says Ready to merge in one line on a clean card, with the branch and the sessions", () => {
    const { deps, openSession } = stubContext();
    const target = host();
    renderPrsSection(
      target,
      {
        data: prs([
          card(7, {
            review: { state: "approved", approvals: 1, changes_requested: 0, requested: [] },
          }),
        ]),
        error: null,
      },
      deps,
    );
    const node = target.querySelector<HTMLElement>(".card-pr")!;
    expect(node.querySelector(".repo")?.textContent).toBe("analyticendeavors/omnigent-ae#7");
    expect(node.querySelector(".pr-branch")?.textContent).toBe("claude/branch-7");
    expect(node.querySelector(".pr-ready")?.textContent).toBe(
      "Ready to merge · 6/6 checks · Approved",
    );
    expect(node.querySelector(".pr-chip")).toBeNull();
    const row = node.querySelector<HTMLElement>(".pr-status")!;
    expect([...row.children].map((part) => part.className)).toEqual(["pr-ready", "actions"]);
    // The head names the state; the Ready line already says CI, so no CI icon.
    const head = node.querySelector<HTMLElement>(".card-head")!;
    expect(head.querySelector(".icon-pull-open")?.getAttribute("aria-label")).toBe(
      "open pull request",
    );
    expect(head.querySelector('[class*="icon-ci-"]')).toBeNull();
    expect(buttonNamed(node, "Merge").disabled).toBe(false);
    buttonNamed(node, "Merge button").click();
    expect(openSession).toHaveBeenCalledWith("s1");
  });

  it("shows quiet chips when something is off, tinting only what blocks the merge", () => {
    const { deps } = stubContext();
    const target = host();
    const blocked = card(17, {
      ci: { state: "failing", passing: 5, failing: 1, pending: 0, total: 6 },
      mergeable: { state: "conflicted", detail: "Conflicts with the base branch." },
      review: { state: "changes_requested", approvals: 0, changes_requested: 1, requested: [] },
      merge: { ...card(17).merge, allowed: false, reason: "CI is failing: 1 of 6 checks." },
    });
    const waiting = card(18, {
      ci: { state: "pending", passing: 5, failing: 0, pending: 1, total: 6 },
      review: { state: "review_required", approvals: 0, changes_requested: 0, requested: ["x"] },
      merge: { ...card(18).merge, allowed: false, reason: "CI is still running: 5 of 6 checks passed, 1 to go." },
    });
    renderPrsSection(target, { data: prs([blocked, waiting]), error: null }, deps);
    const [first, second] = [...target.querySelectorAll<HTMLElement>(".card-pr")];
    const chips = (node: HTMLElement) => [...node.querySelectorAll<HTMLElement>(".pr-chip")];
    expect(first.querySelector(".pr-ready")).toBeNull();
    expect(chips(first).map((chip) => chip.textContent)).toEqual([
      "1 of 6 checks failing",
      "Conflicts",
      "Changes requested",
    ]);
    expect(chips(first).map((chip) => chip.className)).toEqual([
      "pr-chip pr-err",
      "pr-chip pr-err",
      "pr-chip pr-err",
    ]);
    // Each chip leads with its tone's icon; mergeability carries GitHub's detail.
    expect(chips(first).every((chip) => chip.firstElementChild?.matches("svg.pr-tone"))).toBe(true);
    expect(chips(first)[1].title).toBe("Conflicts with the base branch.");
    expect(chips(second).map((chip) => chip.textContent)).toEqual([
      "5/6 passed, 1 running",
      "No conflicts",
      "Review requested",
    ]);
    expect(chips(second).map((chip) => chip.className)).toEqual([
      "pr-chip pr-wait",
      "pr-chip pr-ok",
      "pr-chip pr-warn",
    ]);
  });

  it("draws the head's icon for the pull request's state", () => {
    const { deps } = stubContext();
    const target = host();
    renderPrsSection(target, { data: prs([card(19, { draft: true })]), error: null }, deps);
    const icon = target.querySelector<SVGElement>(".card-pr .card-head .icon-pull")!;
    expect(icon.classList.contains("icon-pull-draft")).toBe(true);
    expect(icon.getAttribute("aria-label")).toBe("draft pull request");
  });

  // A refused merge's fresh card can be merged or closed already: it says so
  // in its state's colour and offers no Merge.
  it("says Merged or Closed without chips or a Merge button on a card that is not open", () => {
    const { deps } = stubContext();
    const target = host();
    const shut = (state: "merged" | "closed", number: number, reason: string) =>
      card(number, {
        state,
        mergeable: { state, detail: `The pull request is ${state}.` },
        merge: { ...card(number).merge, allowed: false, reason },
      });
    renderPrsSection(
      target,
      {
        data: prs([
          shut("merged", 20, "Already merged."),
          shut("closed", 21, "Closed without merging."),
          card(22, { ci: { state: "failing", passing: 5, failing: 1, pending: 0, total: 6 } }),
        ]),
        error: null,
      },
      deps,
    );
    const [merged, closed, open] = [...target.querySelectorAll<HTMLElement>(".card-pr")];
    // The left border follows the state (review, 2026-10-14): purple once
    // merged, the plain border when closed, the CI colour only while open.
    expect(merged.className).toBe("card card-pr card-pr-merged");
    expect(closed.className).toBe("card card-pr");
    expect(open.className).toBe("card card-pr card-ci-failing");
    expect(merged.querySelector(".pr-merged-text")?.textContent).toBe("Merged");
    expect(merged.querySelector(".icon-pull-merged")).not.toBeNull();
    expect(closed.querySelector(".pr-closed")?.textContent).toBe("Closed without merging");
    expect(closed.querySelector(".icon-pull-closed")).not.toBeNull();
    for (const node of [merged, closed]) {
      expect(node.querySelector(".pr-chip")).toBeNull();
      expect(node.querySelector(".pr-why")).toBeNull();
      expect([...node.querySelectorAll("button")].map((b) => b.textContent?.trim())).not.toContain(
        "Merge",
      );
      expect(buttonNamed(node, "Open on GitHub")).toBeTruthy();
    }
  });

  it("opens the pull request on GitHub instead of copying its link", async () => {
    const { deps, openExternal } = stubContext();
    const target = host();
    renderPrsSection(target, { data: prs([card(7)]), error: null }, deps);
    const node = target.querySelector<HTMLElement>(".card-pr")!;
    expect(buttonNamed(node, "Open on GitHub").classList.contains("btn-open")).toBe(true);
    buttonNamed(node, "Open on GitHub").click();
    await flush();
    expect(openExternal).toHaveBeenCalledWith(
      "https://github.com/analyticendeavors/omnigent-ae/pull/7",
    );
    expect(node.querySelector(".flash")).toBeNull();
  });

  it("shows the URL to copy, without a clipboard write, when the host will not open it", async () => {
    const { deps, openExternal } = stubContext();
    openExternal.mockRejectedValueOnce(
      Object.assign(new Error("The browser blocked the new tab"), { code: "PopupBlocked" }),
    );
    const writeText = vi.fn(async () => undefined);
    Object.defineProperty(navigator, "clipboard", { value: { writeText }, configurable: true });
    const target = host();
    renderPrsSection(target, { data: prs([card(7)]), error: null }, deps);
    const node = target.querySelector<HTMLElement>(".card-pr")!;
    buttonNamed(node, "Open on GitHub").click();
    await flush();
    await flush();
    // Review of #58: after the host round trip iOS refuses the write, so none is tried.
    expect(writeText).not.toHaveBeenCalled();
    const line = node.querySelector<HTMLElement>(".flash")!;
    expect(line.textContent).toBe(
      "Could not open GitHub from here (The browser blocked the new tab). " +
        "Tap and hold to copy: https://github.com/analyticendeavors/omnigent-ae/pull/7",
    );
    expect(line.querySelector(".link-copy")?.textContent).toBe(
      "https://github.com/analyticendeavors/omnigent-ae/pull/7",
    );
    Reflect.deleteProperty(navigator, "clipboard");
  });

  it("keeps a Copy link icon beside Open on GitHub that copies in the tap", async () => {
    const { deps } = stubContext();
    const writeText = vi.fn(async () => undefined);
    Object.defineProperty(navigator, "clipboard", { value: { writeText }, configurable: true });
    const target = host();
    renderPrsSection(target, { data: prs([card(7)]), error: null }, deps);
    const node = target.querySelector<HTMLElement>(".card-pr")!;
    const open = buttonNamed(node, "Open on GitHub");
    const copy = buttonNamed(node, "Copy link");
    expect(open.nextElementSibling).toBe(copy);
    expect(copy.className).toBe("btn btn-quiet btn-copy");
    expect(copy.textContent).toBe("");
    copy.click();
    await flush();
    expect(writeText).toHaveBeenCalledWith(
      "https://github.com/analyticendeavors/omnigent-ae/pull/7",
    );
    expect(node.querySelector(".flash")?.textContent).toBe("Copied the link.");
    Reflect.deleteProperty(navigator, "clipboard");
  });

  it("disables Merge and shows why when the gate is shut", () => {
    const { deps, request } = stubContext();
    const target = host();
    const shut = card(8, {
      ci: { state: "failing", passing: 5, failing: 1, pending: 0, total: 6 },
      merge: {
        methods: ["squash"],
        default_method: "squash",
        allowed: false,
        reason: "CI is failing: 1 of 6 checks.",
      },
    });
    renderPrsSection(target, { data: prs([shut]), error: null }, deps);
    const merge = buttonNamed(target, "Merge");
    expect(merge.disabled).toBe(true);
    expect(merge.title).toBe("CI is failing: 1 of 6 checks.");
    expect(target.querySelector(".pr-why")?.textContent).toBe("CI is failing: 1 of 6 checks.");
    // 2026-09-27: chips, reason and buttons share one status row, in that order.
    const row = target.querySelector<HTMLElement>(".card-pr .pr-status")!;
    expect([...row.children].map((part) => part.className)).toEqual([
      "pr-chips",
      "pr-why",
      "actions",
    ]);
    expect(row.querySelector(".actions")?.contains(merge)).toBe(true);
    merge.click();
    expect(target.querySelector(".pr-confirm")).toBeNull();
    expect(request).not.toHaveBeenCalled();
  });

  it("confirms, merges with the chosen method and offers review for the session", async () => {
    const result = {
      merged: true,
      repo: "analyticendeavors/omnigent-ae",
      number: 9,
      method: "merge",
      sha: "m3rg3d0000",
      message: "Pull Request successfully merged",
      session: null,
    };
    const { deps, request, refresh } = stubContext([
      { ok: true, status: 200, body: result },
      { ok: true, status: 200, body: {} },
    ]);
    const target = host();
    renderPrsSection(target, { data: prs([card(9)]), error: null }, deps);
    buttonNamed(target, "Merge").click();
    const confirm = target.querySelector<HTMLElement>(".pr-confirm")!;
    // 2026-10-14: no question line; the confirm button names the method and
    // the branch, and follows the toggle.
    expect(confirm.querySelector(".pr-confirm-q")).toBeNull();
    expect(buttonNamed(confirm, "Squash into main").title).toBe("Squash into main");
    // 2026-09-26: the toggle, Cancel and the confirm button share one row,
    // the buttons at its right end (2026-10-14).
    const row = confirm.querySelector<HTMLElement>(".pr-confirm-row")!;
    expect([...row.children].map((child) => child.className)).toEqual([
      "pr-methods",
      "btn btn-quiet",
      "btn btn-primary",
    ]);
    expect([...row.querySelectorAll(".pr-method")].map((label) => label.textContent)).toEqual([
      "Squash",
      "Merge commit",
    ]);
    const mergeCommit = confirm.querySelector<HTMLInputElement>('input[value="merge"]')!;
    mergeCommit.checked = true;
    mergeCommit.dispatchEvent(new Event("change"));
    expect(buttonNamed(confirm, "Merge into main").title).toBe("Merge into main");
    buttonNamed(confirm, "Merge into main").click();
    await flush();
    expect(request).toHaveBeenCalledWith({
      method: "POST",
      path: "/v1/ae/prs/analyticendeavors/omnigent-ae/9/merge",
      body: { sha: SHA, method: "merge" },
    });
    expect(refresh).toHaveBeenCalledWith(true);
    const done = target.querySelector<HTMLElement>(".card-pr-merged")!;
    // 2026-09-27: short, without GitHub's echo, on one row with the buttons.
    expect(done.querySelector(".pr-merged-text")?.textContent).toBe(
      "Merged #9 (merge commit) m3rg3d0",
    );
    // 2026-10-14: the head turns to the purple merge icon, and no chips stay.
    expect(done.querySelector(".card-head .icon-pull-merged")).not.toBeNull();
    expect(done.querySelector(".pr-chip, .pr-ready")).toBeNull();
    expect(done.textContent).not.toContain("successfully merged");
    const status = done.querySelector<HTMLElement>(".pr-status")!;
    expect(status.contains(done.querySelector(".pr-merged-text"))).toBe(true);
    // Review of #58: Mark session done, then its hint, in one group. Dismiss
    // is an icon in the head's corner since 2026-10-15, off the status row.
    expect([...status.children].map((part) => part.className)).toEqual([
      "pr-merged-text",
      "actions pr-done",
    ]);
    const offer = status.querySelector<HTMLElement>(".pr-done")!;
    expect([...offer.children].map((part) => part.textContent)).toEqual([
      "Mark session done",
      "frees a slot",
    ]);
    const dismiss = buttonNamed(done.querySelector(".card-head")!, "Dismiss");
    expect(dismiss.classList.contains("btn-dismiss")).toBe(true);
    expect(dismiss.title).toBe("Dismiss");
    expect(dismiss.querySelector("svg.icon-close")).not.toBeNull();
    expect(dismiss.textContent).toBe("");
    const markDone = buttonNamed(done, "Mark session done");
    // Optional, so quiet (outline), not primary; the hint shows on a phone.
    expect(markDone.className).toBe("btn btn-quiet");
    expect(markDone.title).toBe(
      'Set "Merge button" to Review (done), which frees one of your slots',
    );
    expect(done.querySelector(".pr-done-hint")?.textContent).toBe("frees a slot");
    markDone.click();
    await flush();
    expect(request).toHaveBeenLastCalledWith({
      method: "PATCH",
      path: "/v1/sessions/s1",
      body: { labels: { "ae.status": "review" } },
    });
    expect(done.textContent).toContain('"Merge button" marked done. Its slot is free.');

    // The next refresh no longer lists it as open; the merged card stays.
    renderPrsSection(target, { data: prs([]), error: null }, deps);
    expect(target.querySelector(".card-pr-merged")).not.toBeNull();
    buttonNamed(target, "Dismiss").click();
    renderPrsSection(target, { data: prs([]), error: null }, deps);
    expect(target.querySelector(".card-pr-merged")).toBeNull();
  });

  // 2026-09-27, review of https://github.com/analyticendeavors/omnigent-ae/pull/53:
  // the radios rewrote the method and the question while the merge was in flight.
  it("locks the method while the merge is in flight", async () => {
    const { deps, request } = stubContext();
    let finish: (reply: Reply) => void = () => undefined;
    request.mockImplementationOnce(
      () =>
        new Promise<Reply>((resolve) => {
          finish = resolve;
        }),
    );
    const target = host();
    renderPrsSection(target, { data: prs([card(11)]), error: null }, deps);
    buttonNamed(target, "Merge").click();
    const confirm = target.querySelector<HTMLElement>(".pr-confirm")!;
    const radios = [...confirm.querySelectorAll<HTMLInputElement>('input[type="radio"]')];
    expect(radios.map((radio) => radio.disabled)).toEqual([false, false]);
    const go = buttonNamed(confirm, "Squash into main");
    go.click();
    expect(radios.map((radio) => radio.disabled)).toEqual([true, true]);
    radios[1].checked = true;
    radios[1].dispatchEvent(new Event("change"));
    expect(go.textContent).toBe("Squash into main");
    // Cancel is off too, and nothing reopens a second confirm step meanwhile:
    // not a tap on the (hidden) Merge, not a refresh.
    const cancel = buttonNamed(confirm, "Cancel");
    expect(cancel.disabled).toBe(true);
    cancel.click();
    buttonNamed(target, "Merge").click();
    renderPrsSection(target, { data: prs([card(11)]), error: null }, deps);
    expect(target.querySelectorAll(".pr-confirm")).toHaveLength(1);
    expect(target.querySelector<HTMLElement>(".card-pr .actions")?.hidden).toBe(true);
    expect(
      [...target.querySelectorAll<HTMLInputElement>('.pr-confirm input[type="radio"]')].every(
        (radio) => radio.disabled,
      ),
    ).toBe(true);
    finish({
      ok: true,
      status: 200,
      body: {
        merged: true,
        repo: "analyticendeavors/omnigent-ae",
        number: 11,
        method: "squash",
        sha: "m3rg3d0000",
        message: "Pull Request successfully merged",
        session: null,
      },
    });
    await flush();
    expect(request).toHaveBeenCalledWith({
      method: "POST",
      path: "/v1/ae/prs/analyticendeavors/omnigent-ae/11/merge",
      body: { sha: SHA, method: "squash" },
    });
    expect(target.querySelector(".card-pr-merged")?.textContent).toContain("(squashed)");
  });

  it("cancel puts the buttons back without a request", () => {
    const { deps, request } = stubContext();
    const target = host();
    renderPrsSection(
      target,
      {
        data: prs([
          card(3, {
            merge: { methods: ["squash"], default_method: "squash", allowed: true, reason: null },
          }),
        ]),
        error: null,
      },
      deps,
    );
    buttonNamed(target, "Merge").click();
    expect(target.querySelector('.pr-confirm input[type="radio"]')).toBeNull();
    const row = target.querySelector<HTMLElement>(".pr-confirm-row")!;
    expect([...row.children].map((child) => child.textContent)).toEqual([
      "Cancel",
      "Squash into main",
    ]);
    buttonNamed(target, "Cancel").click();
    expect(target.querySelector(".pr-confirm")).toBeNull();
    expect((target.querySelector(".card-pr .actions") as HTMLElement).hidden).toBe(false);
    expect(request).not.toHaveBeenCalled();
  });

  it("a refused merge shows the fresh card and the reason", async () => {
    const moved = card(4, {
      head_sha: "ffffffffffffffffffffffffffffffffffffffff",
      ci: { state: "pending", passing: 5, failing: 0, pending: 1, total: 6 },
      merge: {
        methods: ["squash", "merge"],
        default_method: "squash",
        allowed: false,
        reason: "CI is still running: 5 of 6 checks passed, 1 to go.",
      },
    });
    const { deps } = stubContext([
      {
        ok: false,
        status: 409,
        body: {
          error: {
            code: "merge_blocked",
            message: "CI is still running: 5 of 6 checks passed, 1 to go.",
            card: moved,
          },
        },
      },
    ]);
    const target = host();
    renderPrsSection(target, { data: prs([card(4)]), error: null }, deps);
    buttonNamed(target, "Merge").click();
    buttonNamed(target, "Squash into main").click();
    await flush();
    const node = target.querySelector<HTMLElement>(".card-pr")!;
    expect(buttonNamed(node, "Merge").disabled).toBe(true);
    expect(node.querySelector(".flash")?.textContent).toBe(
      "Not merged: CI is still running: 5 of 6 checks passed, 1 to go.",
    );
    // The session links survive the swap.
    expect(node.querySelector(".pr-sessions")?.textContent).toBe("from Merge button");
  });

  it("lists a pull request GitHub would not show, but not a sleeping session", () => {
    const { deps } = stubContext();
    const target = host();
    renderPrsSection(
      target,
      {
        data: prs([], {
          errors: [
            { session_id: "s9", message: "session not read: 503" },
            { url: "https://github.com/o/private/pull/1", message: "GET ... answered 404" },
          ],
        }),
        error: null,
      },
      deps,
    );
    const notices = [...target.querySelectorAll(".notice-error")].map((node) => node.textContent);
    expect(notices).toEqual(["https://github.com/o/private/pull/1: GET ... answered 404"]);
  });
});

describe("the confirm step across a refresh", () => {
  it("stays open with the chosen method when the section re-renders", () => {
    const { deps } = stubContext();
    const target = host();
    renderPrsSection(target, { data: prs([card(5)]), error: null }, deps);
    buttonNamed(target, "Merge").click();
    const mergeCommit = target.querySelector<HTMLInputElement>('input[value="merge"]')!;
    mergeCommit.checked = true;
    mergeCommit.dispatchEvent(new Event("change"));
    renderPrsSection(target, { data: prs([card(5)]), error: null }, deps);
    expect(buttonNamed(target, "Merge into main")).toBeTruthy();
    expect(target.querySelector<HTMLInputElement>('input[value="merge"]')!.checked).toBe(true);
    // A gate that shut in the meantime closes it.
    const shut = card(5, { merge: { ...card(5).merge, allowed: false, reason: "CI is failing." } });
    renderPrsSection(target, { data: prs([shut]), error: null }, deps);
    expect(target.querySelector(".pr-confirm")).toBeNull();
  });
});

describe("the method picker", () => {
  it("exposes the chosen method as the checked radio of one radiogroup", () => {
    const { deps } = stubContext();
    const target = host();
    renderPrsSection(target, { data: prs([card(6)]), error: null }, deps);
    buttonNamed(target, "Merge").click();
    const group = target.querySelector<HTMLElement>('.pr-confirm [role="radiogroup"]')!;
    const radios = [...group.querySelectorAll<HTMLInputElement>('input[type="radio"]')];
    // Short labels on the toggle (2026-09-26); the confirm button names the method.
    expect(radios.map((radio) => radio.closest("label")?.textContent)).toEqual([
      "Squash",
      "Merge commit",
    ]);
    const checked = () =>
      radios.filter((radio) => radio.checked).map((radio) => radio.closest("label")?.textContent);
    expect(checked()).toEqual(["Squash"]);
    // Each segment carries the tick; the stylesheet shows the checked one's.
    expect(group.querySelectorAll(".pr-method svg.pr-check")).toHaveLength(2);
    radios[1].closest("label")!.click();
    expect(checked()).toEqual(["Merge commit"]);
    // Cancel is an outline button beside the confirm button in the one row
    // (2026-09-26; #53 had a text button under Confirm).
    expect(buttonNamed(target, "Cancel").className).toBe("btn btn-quiet");
  });
});

describe("the recommended method (2026-09-28)", () => {
  const PROMOTE =
    "Merge commit keeps dev and main in step; a squash would make the next promotion conflict.";
  const promotion = (number: number) =>
    card(number, {
      head_ref: "dev",
      merge: {
        methods: ["squash", "merge"],
        default_method: "merge",
        recommended_method: "merge",
        recommended_reason: PROMOTE,
        allowed: true,
        reason: null,
      },
    });

  it("opens on the recommended method and leads its reason with Recommended", () => {
    const { deps } = stubContext();
    const target = host();
    renderPrsSection(target, { data: prs([promotion(12)]), error: null }, deps);
    buttonNamed(target, "Merge").click();
    const confirm = target.querySelector<HTMLElement>(".pr-confirm")!;
    expect(confirm.querySelector<HTMLInputElement>('input[value="merge"]')!.checked).toBe(true);
    expect(buttonNamed(confirm, "Merge into main")).toBeTruthy();
    // 2026-10-14: the reason is the step's first line, led by "Recommended";
    // the pill on the segment is gone.
    const why = confirm.querySelector<HTMLElement>(".pr-confirm-why")!;
    expect(why.textContent).toBe(`Recommended · ${PROMOTE}`);
    expect(why.querySelector(".pr-confirm-rec")?.textContent).toBe("Recommended");
    expect(confirm.firstElementChild).toBe(why);
    expect(confirm.querySelector(".pr-method-tag")).toBeNull();
    // The recommended segment still carries the reason as its title.
    const titled = [...confirm.querySelectorAll<HTMLLabelElement>(".pr-method")].filter(
      (label) => label.title,
    );
    expect(titled.map((label) => label.querySelector("input")?.value)).toEqual(["merge"]);
    expect(titled[0].title).toBe(PROMOTE);
    expect(
      [...confirm.querySelectorAll(".pr-method-text")].map((text) => text.textContent),
    ).toEqual(["Squash", "Merge commit"]);
  });

  it("still switches to the other method and merges with it", async () => {
    const { deps, request } = stubContext([{ ok: true, status: 200, body: {} }]);
    const target = host();
    renderPrsSection(target, { data: prs([promotion(13)]), error: null }, deps);
    buttonNamed(target, "Merge").click();
    const confirm = target.querySelector<HTMLElement>(".pr-confirm")!;
    confirm.querySelector<HTMLInputElement>('input[value="squash"]')!.closest("label")!.click();
    expect(confirm.querySelector<HTMLInputElement>('input[value="squash"]')!.checked).toBe(true);
    // The reason stays on the recommendation; the button follows the choice.
    expect(confirm.querySelector(".pr-confirm-why")?.textContent).toBe(`Recommended · ${PROMOTE}`);
    buttonNamed(confirm, "Squash into main").click();
    await flush();
    expect(request).toHaveBeenCalledWith({
      method: "POST",
      path: "/v1/ae/prs/analyticendeavors/omnigent-ae/13/merge",
      body: { sha: SHA, method: "squash" },
    });
  });

  it("shows the reason without a toggle when the repo allows one method", () => {
    const { deps } = stubContext();
    const target = host();
    const only = card(14, {
      merge: {
        methods: ["merge"],
        default_method: "merge",
        recommended_method: "merge",
        recommended_reason: "The only method this repo allows.",
        allowed: true,
        reason: null,
      },
    });
    renderPrsSection(target, { data: prs([only]), error: null }, deps);
    buttonNamed(target, "Merge").click();
    expect(buttonNamed(target, "Merge into main")).toBeTruthy();
    // No choice to make, so no "Recommended" in front of the reason.
    expect(target.querySelector(".pr-confirm-why")?.textContent).toBe(
      "The only method this repo allows.",
    );
    expect(target.querySelector(".pr-confirm-rec")).toBeNull();
    expect(target.querySelector(".pr-methods")).toBeNull();
  });

  it("keeps the old default and draws no reason for a card without a recommendation", () => {
    const { deps } = stubContext();
    const target = host();
    renderPrsSection(target, { data: prs([card(15)]), error: null }, deps);
    buttonNamed(target, "Merge").click();
    expect(target.querySelector<HTMLInputElement>('input[value="squash"]')!.checked).toBe(true);
    expect(target.querySelector(".pr-confirm-why")).toBeNull();
    expect(target.querySelector(".pr-method[title]")).toBeNull();
  });

  // 2026-10-14: the button names the branch the change lands on; without a
  // base branch it falls back to GitHub's own words.
  it("names the base branch on the confirm button, or GitHub's words without one", () => {
    const { deps } = stubContext();
    const target = host();
    renderPrsSection(target, { data: prs([card(16, { base_ref: null })]), error: null }, deps);
    buttonNamed(target, "Merge").click();
    expect(buttonNamed(target, "Squash and merge")).toBeTruthy();
    target.querySelector<HTMLInputElement>('input[value="merge"]')!.closest("label")!.click();
    expect(buttonNamed(target, "Create merge commit").title).toBe("Create merge commit");
  });
});

describe("two cards with the same number in different repos", () => {
  it("each method picker checks only its own radio", () => {
    const { deps } = stubContext();
    const target = host();
    const other = card(7, {
      repo: "analyticendeavors/agents-and-automation",
      url: "https://github.com/analyticendeavors/agents-and-automation/pull/7",
    });
    renderPrsSection(target, { data: prs([card(7), other]), error: null }, deps);
    const [first, second] = [...target.querySelectorAll<HTMLElement>(".card-pr")];
    buttonNamed(first, "Merge").click();
    buttonNamed(second, "Merge").click();
    // The label wraps its input; no id, no `for` that another card could share.
    expect(target.querySelector(".pr-method[for]")).toBeNull();
    expect(target.querySelector(".pr-methods input[id]")).toBeNull();
    const label = [...second.querySelectorAll<HTMLLabelElement>(".pr-method")].find((node) =>
      node.textContent?.includes("Merge commit"),
    )!;
    label.click();
    expect(second.querySelector<HTMLInputElement>('input[value="merge"]')!.checked).toBe(true);
    expect(buttonNamed(second, "Merge into main")).toBeTruthy();
    expect(first.querySelector<HTMLInputElement>('input[value="squash"]')!.checked).toBe(true);
    expect(buttonNamed(first, "Squash into main")).toBeTruthy();
  });
});
