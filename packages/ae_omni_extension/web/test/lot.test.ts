import type { ExtensionContext } from "@omnigent/extension-sdk";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Lot, LotTask, Session } from "../src/api";
import { EXPANDED_KEY, loadLotState, type LotView, renderLot, resetLotState } from "../src/lot";
import { resetLotFilter } from "../src/lotFilter";
import { setTagStyles } from "../src/tags";

const TASK: LotTask = {
  id: "T1",
  title: "contoso-vendor-category-check",
  quadrant: "q2",
  estimate: "15",
  status: "open",
  tags: ["finance", "ae", "parked"],
  created_at: "2026-09-20T10:00:00Z",
  resurfaces: "2026-09-28",
  next: "Ask Alex in Teams about the Vendors category.",
  links: [],
  url: "https://tasks.example/#/all?q=contoso",
};
const LOT: Lot = { list: { id: "L1", name: "Parking Lot" }, count: 1, tasks: [TASK] };
const BLOCK = "THREAD: contoso-vendor-category-check\nNEXT: Ask Alex";

type Answer = { ok: boolean; status: number; body: unknown };

function stubContext(answer: () => Answer, saved: unknown = null) {
  const request = vi.fn(async () => answer());
  const openSession = vi.fn(async () => undefined);
  const openNewSession = vi.fn(async () => undefined);
  const set = vi.fn(async () => undefined);
  const get = vi.fn(async () => saved);
  const listAll = vi.fn(async () => [] as unknown[]);
  const context = {
    server: { request },
    sessions: { listAll },
    navigation: { openSession, openNewSession },
    storage: { user: { get, set, delete: vi.fn() } },
  } as unknown as ExtensionContext;
  return { context, request, openSession, openNewSession, set, get, listAll };
}

function render(context: ExtensionContext, host: HTMLElement, extra: Partial<LotView> = {}): void {
  const view: LotView = { lot: LOT, lotError: null, sessions: [], ...extra };
  renderLot(host, view, { context, refresh: async () => undefined });
}

/** A parked session that hangs off T1. */
function parkedSession(id: string, title: string): Session {
  return {
    id,
    title,
    status: "idle",
    unread: false,
    titleProvisional: false,
    workspace: null,
    gitBranch: null,
    projectId: null,
    labels: { "ae.status": "parked", "ae.task": "T1" },
    archived: false,
    createdAt: 1,
    updatedAt: 1,
  };
}

const flush = () => new Promise((resolve) => setTimeout(resolve, 0));

function resumeButton(host: HTMLElement): HTMLButtonElement {
  return host.querySelector<HTMLButtonElement>(".btn-resume")!;
}

describe("the lot card", () => {
  let host: HTMLElement;

  beforeEach(() => {
    resetLotState();
    resetLotFilter();
    setTagStyles(null);
    document.body.replaceChildren();
    host = document.createElement("div");
    document.body.appendChild(host);
  });

  afterEach(() => vi.restoreAllMocks());

  // Second pass (2026-10-14): one line, everything else behind the chevron.
  it("is one line: icon, title, date, chevron and Resume; the rest behind the chevron", () => {
    const { context } = stubContext(() => ({ ok: true, status: 200, body: {} }));
    render(context, host, { today: "2026-09-24" });
    const card = host.querySelector<HTMLElement>('[data-task="T1"]')!;
    // The line and the hidden details, nothing else on the card.
    expect([...card.children].map((node) => node.className)).toEqual([
      "card-head task-head",
      "card-details",
    ]);
    const head = card.querySelector(".task-head")!;
    const toggle = head.querySelector<HTMLButtonElement>(".card-toggle")!;
    expect(head.children).toHaveLength(2);
    expect(head.lastElementChild).toBe(resumeButton(host));
    expect(toggle.querySelector(".icon-admin")).not.toBeNull();
    expect(toggle.querySelector(".card-title")?.textContent).toBe(TASK.title);
    const date = toggle.querySelector(".resurface")!;
    expect(date.textContent).toBe("Sep 28");
    expect(date.classList.contains("is-due")).toBe(false);
    expect(toggle.querySelector(".icon-chevron")).not.toBeNull();
    expect(toggle.querySelector(".parked-mark")).toBeNull();
    expect(head.querySelector(".card-tags, .next, .url, .card-repo")).toBeNull();
    // Behind the chevron: every tag but `parked` (which every lot task
    // carries), NEXT and the task-store link.
    const details = card.querySelector<HTMLElement>(".card-details")!;
    expect(details.hidden).toBe(true);
    expect([...details.querySelectorAll(".card-tags .tag-chip")].map((t) => t.textContent)).toEqual(
      ["finance", "ae"],
    );
    expect(details.querySelector(".next")?.textContent).toBe(`NEXT ${TASK.next}`);
    expect(details.querySelector(".url code")?.textContent).toBe(TASK.url);
    toggle.click();
    expect(toggle.getAttribute("aria-expanded")).toBe("true");
    expect(details.hidden).toBe(false);
    expect(card.classList.contains("is-open")).toBe(true);
  });

  it("says today in the warning colour on the day it resurfaces", () => {
    const { context } = stubContext(() => ({ ok: true, status: 200, body: {} }));
    render(context, host, { today: "2026-09-28" });
    const date = host.querySelector(".task-head .resurface")!;
    expect(date.textContent).toBe("today");
    expect(date.classList.contains("is-due")).toBe(true);
    expect(date.getAttribute("title")).toBe("Resurfaces 2026-09-28");
    // Still due a day later: the date, in the same colour.
    render(context, host, { today: "2026-09-29" });
    const late = host.querySelector(".task-head .resurface")!;
    expect(late.textContent).toBe("Sep 28");
    expect(late.classList.contains("is-due")).toBe(true);
  });

  it("marks a parked session on the line and opens it from behind the chevron", async () => {
    const stub = stubContext(() => ({ ok: true, status: 200, body: {} }));
    render(stub.context, host, { sessions: [parkedSession("conv_p", "Vendors check")] });
    const mark = host.querySelector(".task-head .card-toggle .parked-mark")!;
    expect(mark.getAttribute("aria-label")).toBe("Parked session: Vendors check");
    expect(mark.querySelector("title")?.textContent).toBe("Parked session: Vendors check");
    const row = host.querySelector<HTMLElement>(".card-details .task-session")!;
    expect(row.textContent).toContain("Parked session: Vendors check");
    row.querySelector<HTMLButtonElement>("button")!.click();
    await flush();
    expect(stub.openSession).toHaveBeenCalledWith("conv_p");
    // Two of them: one marker that counts them.
    render(stub.context, host, {
      sessions: [parkedSession("conv_p", "Vendors check"), parkedSession("conv_q", "Second")],
    });
    expect(host.querySelectorAll(".task-head .parked-mark")).toHaveLength(1);
    expect(host.querySelector(".task-head .parked-mark")?.getAttribute("aria-label")).toBe(
      "2 parked sessions",
    );
    expect(host.querySelectorAll(".card-details .task-session")).toHaveLength(2);
  });

  it("expands, remembers it in storage.user, and restores it", async () => {
    const first = stubContext(() => ({ ok: true, status: 200, body: {} }));
    render(first.context, host);
    const toggle = host.querySelector<HTMLButtonElement>(".card-toggle")!;
    toggle.click();
    expect(toggle.getAttribute("aria-expanded")).toBe("true");
    expect(host.querySelector<HTMLElement>(".card-details")!.hidden).toBe(false);
    expect(first.set).toHaveBeenCalledWith(EXPANDED_KEY, ["T1"]);

    resetLotState();
    const second = stubContext(() => ({ ok: true, status: 200, body: {} }), ["T1", 7]);
    await loadLotState(second.context);
    render(second.context, host);
    expect(host.querySelector<HTMLElement>(".card-details")!.hidden).toBe(false);
  });

  it("starts the session and opens it", async () => {
    const stub = stubContext(() => ({
      ok: true,
      status: 200,
      body: { session_id: "conv_new", task_id: "T1", block: BLOCK, fenced: "x" },
    }));
    render(stub.context, host);
    resumeButton(host).click();
    await flush();
    expect(stub.request).toHaveBeenCalledWith({
      method: "POST",
      path: "/v1/ae/lot/T1/session",
      body: undefined,
    });
    expect(stub.openSession).toHaveBeenCalledWith("conv_new");
    expect(stub.openNewSession).not.toHaveBeenCalled();
    expect(host.querySelector(".resume-fallback")).toBeNull();
  });

  it("keeps the block on the card when no session can be started", async () => {
    const stub = stubContext(() => ({
      ok: false,
      status: 503,
      body: {
        error: {
          code: "resume_no_target",
          message: "no session was started: no host is online",
          block: BLOCK,
          fenced: `\`\`\`\n${BLOCK}\n\`\`\``,
        },
      },
    }));
    render(stub.context, host);
    resumeButton(host).click();
    await flush();
    expect(stub.openSession).not.toHaveBeenCalled();
    const panel = host.querySelector(".resume-fallback")!;
    expect(panel.querySelector(".notice-error")?.textContent).toContain("no host is online");
    expect(panel.querySelector("pre")?.textContent).toBe(BLOCK);

    // The 20 s refresh re-renders the lot; the message stays until dismissed.
    render(stub.context, host);
    const kept = host.querySelector(".resume-fallback")!;
    expect(kept.querySelector("pre")?.textContent).toBe(BLOCK);
    const buttons = [...kept.querySelectorAll("button")];
    buttons.find((node) => node.textContent === "New session")!.click();
    await flush();
    expect(stub.openNewSession).toHaveBeenCalled();
    buttons.find((node) => node.textContent === "Dismiss")!.click();
    await flush();
    expect(host.querySelector(".resume-fallback")).toBeNull();
    render(stub.context, host);
    expect(host.querySelector(".resume-fallback")).toBeNull();
  });

  it("offers the started session when only the block failed to post", async () => {
    const stub = stubContext(() => ({
      ok: false,
      status: 502,
      body: {
        error: {
          code: "resume_block_not_posted",
          message: "session conv_half started but the block was not posted: 409",
          session_id: "conv_half",
          block: BLOCK,
          fenced: "x",
        },
      },
    }));
    render(stub.context, host);
    resumeButton(host).click();
    await flush();
    const open = [...host.querySelectorAll(".resume-fallback button")].find(
      (node) => node.textContent === "Open its session",
    ) as HTMLButtonElement;
    open.click();
    await flush();
    expect(stub.openSession).toHaveBeenCalledWith("conv_half");
  });

  it("flashes a plain error without a block", async () => {
    const stub = stubContext(() => ({
      ok: false,
      status: 503,
      body: {
        error: { code: "task_store_not_configured", message: "task-store is not configured" },
      },
    }));
    render(stub.context, host);
    resumeButton(host).click();
    await flush();
    expect(host.querySelector(".resume-fallback")).toBeNull();
    expect(host.querySelector(".flash")?.textContent).toBe("task-store is not configured");
    expect(resumeButton(host).textContent).toBe("Resume");
  });

  it("finds a slow start by its ae.task label after the host call times out", async () => {
    vi.useFakeTimers();
    try {
      const stub = stubContext(() => {
        throw Object.assign(new Error("Host method server.request timed out"), {
          name: "ExtensionApiError",
          code: "Timeout",
        });
      });
      const now = Date.now() / 1000;
      stub.listAll.mockResolvedValueOnce([]).mockResolvedValueOnce([
        { id: "conv_old", labels: { "ae.task": "T1" }, createdAt: now - 3600 },
        { id: "conv_slow", labels: { "ae.task": "T1" }, createdAt: now + 1 },
        { id: "conv_other", labels: { "ae.task": "T2" }, createdAt: now + 2 },
      ]);
      render(stub.context, host);
      resumeButton(host).click();
      await vi.advanceTimersByTimeAsync(7_000);
      expect(stub.listAll).toHaveBeenCalledTimes(2);
      expect(stub.openSession).toHaveBeenCalledWith("conv_slow");
      expect(host.querySelector(".resume-fallback")).toBeNull();
    } finally {
      vi.useRealTimers();
    }
  });
});
