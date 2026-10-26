import type { ExtensionContext } from "@omnigent/extension-sdk";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Capacity, CapacitySession, Lot, Session } from "../src/api";
import { renderCapacity } from "../src/capacity";
import { holdMenus, resetMenuState } from "../src/cards";
import { renderLot, resetLotState } from "../src/lot";
import { loadSectionState, resetSectionState, SECTIONS_KEY } from "../src/sections";
import { needsNote, renderNeedsNote, renderReference, renderUnsorted } from "../src/sessions";

function session(id: string, labels: Record<string, string>, title = id): Session {
  return {
    id,
    title,
    status: "idle",
    unread: false,
    titleProvisional: false,
    workspace: null,
    gitBranch: null,
    projectId: null,
    labels,
    archived: false,
    createdAt: 1,
    updatedAt: 1,
  };
}

function row(id: string, extra: Partial<CapacitySession> = {}): CapacitySession {
  return {
    id,
    title: id,
    status: "working",
    live_status: null,
    blocked: false,
    started: 1,
    elapsed_s: 60,
    elapsed: "1m",
    next: null,
    gate: null,
    fork: null,
    overflow: false,
    task: null,
    ...extra,
  };
}

function stubContext(saved: unknown = null) {
  const set = vi.fn(async () => undefined);
  const openSession = vi.fn(async () => undefined);
  const context = {
    server: { request: vi.fn() },
    sessions: { listAll: vi.fn(async () => []) },
    navigation: { openSession, openNewSession: vi.fn(), openPage: vi.fn() },
    storage: { user: { get: vi.fn(async () => saved), set, delete: vi.fn() } },
  } as unknown as ExtensionContext;
  return { context, set, openSession };
}

const flush = () => new Promise((resolve) => setTimeout(resolve, 0));

function host(): HTMLElement {
  const node = document.createElement("div");
  document.body.appendChild(node);
  return node;
}

function key(target: Element, name: string): void {
  target.dispatchEvent(new KeyboardEvent("keydown", { key: name, bubbles: true }));
}

/** One working row with its "..." menu, drawn again by *draw* as the 20 s refresh does. */
function workingRow() {
  const { context } = stubContext();
  const request = vi.fn(async () => ({ ok: true, status: 200, body: {} }));
  (context.server as unknown as { request: typeof request }).request = request;
  const parkSession = vi.fn();
  const refresh = vi.fn(async () => undefined);
  const working = host();
  const view = {
    capacity: null,
    capacityError: null,
    sessions: [session("a", { "ae.status": "working" }, "Pricing fix")],
  };
  // As main.ts render redraws: hold the menus, draw, put them back.
  const draw = () => {
    const keep = holdMenus();
    renderCapacity({ working, blocked: host() }, view, { context, refresh, parkSession });
    keep();
  };
  draw();
  const parts = () => {
    const trigger = working.querySelector<HTMLButtonElement>(".btn-menu")!;
    const menu = document.getElementById(trigger.getAttribute("aria-controls")!)!;
    const items = [...menu.querySelectorAll<HTMLButtonElement>('[role="menuitem"]')];
    return { trigger, menu, items };
  };
  return { draw, parts, request, parkSession, refresh };
}

describe("the Dashboard", () => {
  beforeEach(() => {
    resetLotState();
    resetSectionState();
    resetMenuState();
    document.body.replaceChildren();
  });

  afterEach(() => vi.restoreAllMocks());

  it("counts Working as the slots and gives Blocked its own section", () => {
    const { context } = stubContext();
    const capacity = {
      limit: 3,
      slots_used: 2,
      working: [row("a", { gate: "open" }), row("b")],
      blocked: [row("c", { status: "blocked", blocked: true })],
    } as unknown as Capacity;
    const working = host();
    const blocked = host();
    renderCapacity(
      { working, blocked },
      { capacity, capacityError: null, sessions: [session("b", { "ae.unfiled": "2" })] },
      { context, refresh: vi.fn() },
    );
    expect(working.querySelector(".section-h .count")?.textContent).toBe("2/3");
    expect(working.querySelectorAll("[data-session]")).toHaveLength(2);
    expect(working.querySelector('[data-session="a"] .tag-gate')?.textContent).toBe("gate open");
    expect(working.querySelector('[data-session="b"] .tag-unfiled')?.textContent).toBe("2 unfiled");
    expect(blocked.querySelector(".section-h .count")?.textContent).toBe("1");
    expect(blocked.querySelector('[data-session="c"]')).not.toBeNull();
  });

  it("parks from a working card through the park form", async () => {
    const { context } = stubContext();
    const parkSession = vi.fn();
    const working = host();
    renderCapacity(
      { working, blocked: host() },
      { capacity: null, capacityError: null, sessions: [session("a", { "ae.status": "working" })] },
      { context, refresh: vi.fn(), parkSession },
    );
    const park = [...working.querySelectorAll<HTMLButtonElement>("button")].find(
      (node) => node.textContent === "Park",
    )!;
    park.click();
    await flush();
    expect(parkSession).toHaveBeenCalledWith("a");
  });

  it("shows a parked session on its note's card, not in a list of its own", () => {
    const { context } = stubContext();
    const lot: Lot = {
      list: { id: "L", name: "Parking Lot" },
      count: 1,
      tasks: [
        {
          id: "T-4",
          title: "sidebar-search-shortcut",
          quadrant: "q3",
          estimate: "15",
          status: "open",
          tags: [],
          created_at: null,
          resurfaces: "2026-09-27",
          next: null,
          links: [],
          url: "https://tasks.example/",
        },
      ],
    };
    const target = host();
    const parked = session("p", { "ae.status": "parked", "ae.task": "T-4" }, "Sidebar search");
    renderLot(target, { lot, lotError: null, sessions: [parked] }, { context, refresh: vi.fn() });
    // 2026-10-14: a marker on the one line, the session's row behind the chevron.
    expect(target.querySelector('[data-task="T-4"] .task-head .parked-mark title')?.textContent).toBe(
      "Parked session: Sidebar search",
    );
    const line = target.querySelector('[data-task="T-4"] .card-details .task-session');
    expect(line?.textContent).toContain("Parked session: Sidebar search");
  });

  it("keeps the park form, open and typed in, across a refresh", () => {
    const { context } = stubContext();
    const target = host();
    const lot: Lot = { list: { id: "L", name: "Parking Lot" }, count: 0, tasks: [] };
    const working = session("w", { "ae.status": "working" }, "Pricing fix");
    const view = { lot, lotError: null, sessions: [working] };
    renderLot(target, view, { context, refresh: vi.fn() });
    const form = target.querySelector<HTMLDetailsElement>("#park")!;
    form.open = true;
    target.querySelector<HTMLSelectElement>("#park-session_id")!.value = "w";
    target.querySelector<HTMLTextAreaElement>("#park-state")!.value = "half typed";
    renderLot(target, view, { context, refresh: vi.fn() });
    expect(target.querySelector("#park")).toBe(form);
    expect(form.open).toBe(true);
    expect(target.querySelector<HTMLSelectElement>("#park-session_id")!.value).toBe("w");
    expect(target.querySelector<HTMLTextAreaElement>("#park-state")!.value).toBe("half typed");
  });

  // Second pass (2026-10-14): the title opens the session, the Open button
  // went, and Park and the status moves sit in the row's "..." menu.
  it("draws one row shape: the title opens it, Park and the moves in a menu", async () => {
    const { context, openSession } = stubContext();
    const working = host();
    const blocked = host();
    renderCapacity(
      { working, blocked },
      {
        capacity: null,
        capacityError: null,
        sessions: [
          session("a", { "ae.status": "working" }),
          session("c", { "ae.status": "blocked" }),
        ],
      },
      { context, refresh: vi.fn(), parkSession: vi.fn() },
    );
    const unsorted = host();
    renderUnsorted(unsorted, [session("u", {})], { context, refresh: vi.fn() });
    for (const [root, items] of [
      [working, ["Park", "Move to Review", "Move to Blocked"]],
      [blocked, ["Move to Working"]],
      [unsorted, ["Move to Working", "Move to Review", "Move to Reference"]],
    ] as const) {
      const card = root.querySelector<HTMLElement>("article.card-session")!;
      const names = [...card.querySelectorAll("button")].map((node) => node.textContent);
      expect(names).not.toContain("Open");
      const trigger = card.querySelector<HTMLButtonElement>(".card-head .btn-menu")!;
      expect(trigger.getAttribute("aria-haspopup")).toBe("menu");
      const menu = document.getElementById(trigger.getAttribute("aria-controls")!)!;
      expect(card.contains(menu)).toBe(true);
      expect(menu.getAttribute("role")).toBe("menu");
      expect(menu.hidden).toBe(true);
      expect(
        [...menu.querySelectorAll('[role="menuitem"]')].map((node) => node.textContent),
      ).toEqual(items);
    }
    const title = working.querySelector<HTMLButtonElement>("h3.card-title > button.card-open")!;
    expect(title.textContent).toBe("a");
    title.click();
    await flush();
    expect(openSession).toHaveBeenCalledWith("a");
  });

  it("opens the ... menu on its first item, moves with the arrow keys, closes on Escape", () => {
    const { parts } = workingRow();
    const { trigger, menu, items } = parts();
    expect(trigger.getAttribute("aria-label")).toBe("More actions for Pricing fix");
    expect(trigger.getAttribute("aria-expanded")).toBe("false");
    trigger.click();
    expect(trigger.getAttribute("aria-expanded")).toBe("true");
    expect(menu.hidden).toBe(false);
    expect(document.activeElement).toBe(items[0]);
    key(items[0], "ArrowDown");
    expect(document.activeElement).toBe(items[1]);
    key(items[1], "ArrowUp");
    key(items[0], "ArrowUp");
    // Wraps to the last item.
    expect(document.activeElement).toBe(items[2]);
    key(items[2], "Home");
    expect(document.activeElement).toBe(items[0]);
    key(items[0], "End");
    expect(document.activeElement).toBe(items[2]);
    key(items[2], "Escape");
    expect(menu.hidden).toBe(true);
    expect(trigger.getAttribute("aria-expanded")).toBe("false");
    expect(document.activeElement).toBe(trigger);
    // ArrowUp on the button opens the menu on its last item.
    key(trigger, "ArrowUp");
    expect(menu.hidden).toBe(false);
    expect(document.activeElement).toBe(items[2]);
  });

  it("runs the chosen item: Park opens the park form, a move writes ae.status", async () => {
    const { parts, parkSession, request, refresh } = workingRow();
    const { trigger, menu, items } = parts();
    trigger.click();
    items[0].click();
    await flush();
    expect(parkSession).toHaveBeenCalledWith("a");
    // Closed before the item acted, focus back on the button.
    expect(menu.hidden).toBe(true);
    expect(document.activeElement).toBe(trigger);
    trigger.click();
    items[1].click();
    await flush();
    expect(request).toHaveBeenCalledWith({
      method: "PATCH",
      path: "/v1/sessions/a",
      body: { labels: { "ae.status": "review" } },
    });
    expect(refresh).toHaveBeenCalled();
  });

  it("closes the ... menu on a press outside it, leaving focus where the press put it", () => {
    const { parts } = workingRow();
    const { trigger, menu } = parts();
    trigger.click();
    trigger.dispatchEvent(new Event("pointerdown", { bubbles: true }));
    expect(menu.hidden).toBe(false);
    document.body.dispatchEvent(new Event("pointerdown", { bubbles: true }));
    expect(menu.hidden).toBe(true);
    expect(trigger.getAttribute("aria-expanded")).toBe("false");
    expect(document.activeElement).not.toBe(trigger);
  });

  it("keeps an open ... menu open across the 20 s redraw, on the same item", () => {
    const { draw, parts } = workingRow();
    const before = parts();
    before.trigger.click();
    key(before.items[0], "ArrowDown");
    const focus = vi.spyOn(HTMLElement.prototype, "focus");
    // Review (2026-10-14): one explicit step after the redraw, no microtask.
    draw();
    const after = parts();
    expect(before.trigger.isConnected).toBe(false);
    expect(after.trigger).not.toBe(before.trigger);
    expect(after.trigger.getAttribute("aria-expanded")).toBe("true");
    expect(after.menu.hidden).toBe(false);
    expect(document.activeElement).toBe(after.items[1]);
    // Put back without scrolling the page.
    expect(focus).toHaveBeenCalledTimes(1);
    expect(focus).toHaveBeenCalledWith({ preventScroll: true });
    // Closed, a redraw leaves it closed and the focus on its button.
    key(after.items[1], "Escape");
    draw();
    expect(parts().menu.hidden).toBe(true);
    expect(document.activeElement).toBe(parts().trigger);
  });

  // Review (2026-10-14): a menu left open while the focus is elsewhere stays
  // open on the redrawn row, and the redraw neither takes the focus nor
  // scrolls; a frame without the focus is never handed it back.
  it("leaves the focus alone when a redraw finds the menu open and the focus elsewhere", () => {
    const { draw, parts } = workingRow();
    parts().trigger.click();
    const elsewhere = document.createElement("input");
    document.body.appendChild(elsewhere);
    elsewhere.focus();
    const focus = vi.spyOn(HTMLElement.prototype, "focus");
    draw();
    expect(document.activeElement).toBe(elsewhere);
    expect(focus).not.toHaveBeenCalled();
    expect(parts().menu.hidden).toBe(false);
    expect(parts().trigger.getAttribute("aria-expanded")).toBe("true");
    // Focus in the menu, but the frame itself without the focus.
    parts().items[0].focus();
    vi.spyOn(document, "hasFocus").mockReturnValue(false);
    focus.mockClear();
    draw();
    expect(focus).not.toHaveBeenCalled();
    expect(parts().menu.hidden).toBe(false);
  });

  it("collects what breaks the note rule under Needs a note", async () => {
    const { context } = stubContext();
    const parkSession = vi.fn();
    const noNote = session("n", { "ae.status": "parked" });
    const gone = session("g", { "ae.status": "parked", "ae.task": "T-9" });
    const unfiled = session("u", { "ae.status": "working", "ae.unfiled": "1" });
    const fine = session("f", { "ae.status": "parked", "ae.task": "T-4" });
    const open = new Set(["T-4"]);
    expect(needsNote(noNote, open)).toBe("parked without a note");
    expect(needsNote(gone, open)).toBe("task T-9 is not open in the lot");
    expect(needsNote(unfiled, open)).toBe("1 unfiled note");
    expect(needsNote(fine, open)).toBeNull();
    const target = host();
    renderNeedsNote(target, [noNote, gone, unfiled, fine], open, {
      context,
      refresh: vi.fn(),
      parkSession,
    });
    expect(target.querySelector(".section-h .count")?.textContent).toBe("3");
    const file = [...target.querySelectorAll<HTMLButtonElement>("button")].filter(
      (node) => node.textContent === "File a note",
    );
    // Only the sessions without a task get the button.
    expect(file).toHaveLength(2);
    file[0].click();
    await flush();
    expect(parkSession).toHaveBeenCalledWith("n");
  });

  it("starts Reference and Unsorted closed and remembers a toggle", async () => {
    const { context, set } = stubContext();
    await loadSectionState(context);
    const reference = host();
    const unsorted = host();
    const sessions = [session("r", { "ae.status": "reference" }), session("x", {})];
    renderReference(reference, sessions, { context, refresh: vi.fn() });
    renderUnsorted(unsorted, sessions, { context, refresh: vi.fn() });
    const node = reference.querySelector<HTMLElement>("#reference")!;
    expect(node.classList.contains("is-closed")).toBe(true);
    expect(unsorted.querySelector("#unsorted")?.classList.contains("is-closed")).toBe(true);
    expect(unsorted.querySelector('[data-session="x"]')).not.toBeNull();
    node.querySelector<HTMLButtonElement>(".section-toggle")!.click();
    expect(node.classList.contains("is-closed")).toBe(false);
    expect(node.querySelector<HTMLElement>(".section-body")!.hidden).toBe(false);
    expect(set).toHaveBeenCalledWith(SECTIONS_KEY, { reference: true });
  });

  it("restores remembered sections on load", async () => {
    const { context } = stubContext({ reference: true, working: false });
    await loadSectionState(context);
    const target = host();
    renderReference(target, [session("r", { "ae.status": "reference" })], {
      context,
      refresh: vi.fn(),
    });
    expect(target.querySelector("#reference")?.classList.contains("is-closed")).toBe(false);
  });
});

describe("Reference reopen", () => {
  it("gives each Reference line a menu that moves it back to Working or Review", () => {
    const target = document.createElement("div");
    document.body.appendChild(target);
    renderReference(target, [session("r", { "ae.status": "reference" })], {
      context: {} as never,
      refresh: vi.fn(),
    });
    const items = [...target.querySelectorAll(".menu-item")].map((n) => n.textContent);
    expect(items).toEqual(["Move to Working", "Move to Review"]);
  });
});

describe("Reference reopen failure", () => {
  it("shows a failed move on its row", async () => {
    const target = document.createElement("div");
    document.body.appendChild(target);
    const context = {
      server: { request: vi.fn(async () => ({ ok: false, status: 500, body: {} })) },
    } as never;
    renderReference(target, [session("r", { "ae.status": "reference" })], {
      context,
      refresh: vi.fn(),
    });
    (target.querySelector(".menu-item") as HTMLButtonElement).click();
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(target.querySelector("li .flash")).not.toBeNull();
  });
});
