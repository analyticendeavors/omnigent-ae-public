import type { ExtensionContext } from "@omnigent/extension-sdk";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Lot, LotTask } from "../src/api";
import { renderLot, resetLotState, taskCard } from "../src/lot";
import {
  addDays,
  emptyFilter,
  FILTER_KEY,
  filterTasks,
  type LotFilter,
  loadLotFilter,
  lotOptions,
  matchesTask,
  normalizeFilter,
  resetLotFilter,
} from "../src/lotFilter";
import { setTagStyles, tagChip, visibleTags } from "../src/tags";

const TODAY = "2026-09-25";

function task(id: string, extra: Partial<LotTask> = {}): LotTask {
  return {
    id,
    title: id,
    quadrant: "q3",
    estimate: null,
    status: "open",
    tags: ["parked"],
    created_at: null,
    resurfaces: null,
    next: null,
    repo: null,
    links: [],
    url: `https://tasks.example/#/all?q=${id}`,
    ...extra,
  };
}

const TASKS: LotTask[] = [
  task("eur-currency-formatting", {
    quadrant: "q1",
    tags: ["parked", "content", "ae"],
    resurfaces: "2026-09-22",
    next: "Open shared/js/money.js and add the EUR branch",
    repo: "analyticendeavors/website_content",
  }),
  task("graphiti-dedupe", {
    quadrant: "q3",
    tags: ["parked", "ae"],
    resurfaces: "2026-09-25",
    next: "Run the dedupe script against staging",
    repo: "analyticendeavors/omnigent-ae",
  }),
  task("renewal-reminder", {
    quadrant: "q2",
    tags: ["parked", "Admin"],
    resurfaces: "2026-10-01",
    next: "Email the renewal reminder",
  }),
  task("sidebar-search", { quadrant: "q3", resurfaces: "2026-10-02", tags: ["parked"] }),
];

function only(filter: Partial<LotFilter>): string[] {
  return filterTasks(TASKS, { ...emptyFilter(), ...filter }, TODAY).map((t) => t.id);
}

describe("the lot filter", () => {
  it("matches every word of the text in the title or NEXT, any case", () => {
    expect(only({ text: "EUR" })).toEqual(["eur-currency-formatting"]);
    expect(only({ text: "dedupe STAGING" })).toEqual(["graphiti-dedupe"]);
    expect(only({ text: "email reminder" })).toEqual(["renewal-reminder"]);
    expect(only({ text: "dedupe money" })).toEqual([]);
    expect(only({ text: "   " })).toHaveLength(4);
  });

  it("ORs inside a group and ANDs across groups", () => {
    expect(only({ quadrants: ["hotfix", "admin"] })).toEqual([
      "eur-currency-formatting",
      "renewal-reminder",
    ]);
    expect(only({ tags: ["ae"] })).toEqual(["eur-currency-formatting", "graphiti-dedupe"]);
    expect(only({ tags: ["ae"], quadrants: ["feature"] })).toEqual(["graphiti-dedupe"]);
    expect(only({ tags: ["admin"] })).toEqual(["renewal-reminder"]);
    expect(only({ repos: ["analyticendeavors/omnigent-ae"] })).toEqual(["graphiti-dedupe"]);
  });

  it("reads overdue and this week against the lot's today", () => {
    expect(only({ dates: ["overdue"] })).toEqual(["eur-currency-formatting"]);
    // Today through six days on: Sep 25 to Oct 1.
    expect(only({ dates: ["week"] })).toEqual(["graphiti-dedupe", "renewal-reminder"]);
    expect(only({ dates: ["overdue", "week"] })).toHaveLength(3);
    expect(matchesTask(task("x"), { ...emptyFilter(), dates: ["overdue"] }, TODAY)).toBe(false);
    expect(addDays("2026-12-29", 6)).toBe("2027-01-04");
  });

  it("offers the tags and repos the lot carries, most used first, parked left out", () => {
    expect(lotOptions(TASKS)).toEqual({
      tags: ["ae", "admin", "content"],
      repos: ["analyticendeavors/omnigent-ae", "analyticendeavors/website_content"],
    });
    expect(visibleTags(["Parked", "ae"])).toEqual(["ae"]);
  });

  it("reads a stored filter defensively", () => {
    expect(normalizeFilter(null)).toEqual(emptyFilter());
    expect(normalizeFilter(["x"])).toEqual(emptyFilter());
    expect(
      normalizeFilter({
        text: "eur",
        quadrants: ["hotfix", "q9", 3],
        dates: ["week", "someday"],
        tags: ["AE"],
        repos: ["a/b", null],
      }),
    ).toEqual({ text: "eur", quadrants: ["hotfix"], dates: ["week"], tags: ["ae"], repos: ["a/b"] });
  });
});

function stubContext(saved: unknown = null) {
  const set = vi.fn(async () => undefined);
  const context = {
    server: { request: vi.fn() },
    sessions: { listAll: vi.fn(async () => []) },
    navigation: { openSession: vi.fn(), openNewSession: vi.fn() },
    storage: { user: { get: vi.fn(async () => saved), set, delete: vi.fn() } },
  } as unknown as ExtensionContext;
  return { context, set };
}

const LOT: Lot = { list: { id: "L", name: "Parking Lot" }, count: TASKS.length, tasks: TASKS };

function shownIds(host: HTMLElement): string[] {
  return [...host.querySelectorAll<HTMLElement>("[data-task]")].map((node) => node.dataset.task!);
}

describe("the lot's search bar and cards", () => {
  let host: HTMLElement;

  beforeEach(() => {
    resetLotState();
    resetLotFilter();
    setTagStyles(null);
    document.body.replaceChildren();
    host = document.createElement("div");
    document.body.appendChild(host);
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.restoreAllMocks();
  });

  function render(context: ExtensionContext): void {
    renderLot(
      host,
      { lot: LOT, lotError: null, sessions: [], today: TODAY },
      { context, refresh: async () => undefined },
    );
  }

  it("filters as Reid types, remembers it per device, and keeps the field across a refresh", async () => {
    vi.useFakeTimers();
    const { context, set } = stubContext();
    await loadLotFilter(context);
    render(context);
    const input = host.querySelector<HTMLInputElement>(".lot-search-input")!;
    input.focus();
    input.value = "dedupe";
    input.dispatchEvent(new Event("input"));
    expect(shownIds(host)).toEqual(["graphiti-dedupe"]);
    expect(host.querySelector(".lot-filter-summary")?.textContent).toContain("Showing 1 of 4");
    // One write once typing pauses, not one per keystroke.
    expect(set).not.toHaveBeenCalled();
    await vi.advanceTimersByTimeAsync(500);
    expect(set).toHaveBeenCalledTimes(1);
    expect(set).toHaveBeenCalledWith(FILTER_KEY, { ...emptyFilter(), text: "dedupe" });

    // The 20 s refresh rebuilds the section; the field, its text and focus stay.
    render(context);
    const again = host.querySelector<HTMLInputElement>(".lot-search-input")!;
    expect(again).toBe(input);
    expect(again.value).toBe("dedupe");
    expect(document.activeElement).toBe(again);
    expect(shownIds(host)).toEqual(["graphiti-dedupe"]);
  });

  it("restores the remembered filter on the next visit", async () => {
    const { context } = stubContext({ ...emptyFilter(), quadrants: ["admin"] });
    await loadLotFilter(context);
    render(context);
    expect(shownIds(host)).toEqual(["renewal-reminder"]);
    const toggle = host.querySelector<HTMLButtonElement>(".lot-filter-toggle")!;
    expect(toggle.querySelector(".count")?.textContent).toBe("1");
    toggle.click();
    expect(host.querySelector<HTMLElement>(".lot-filter-sheet")!.hidden).toBe(false);
    const admin = host.querySelector<HTMLButtonElement>(".filter-q-admin")!;
    expect(admin.getAttribute("aria-pressed")).toBe("true");
    admin.click();
    expect(shownIds(host)).toHaveLength(4);
  });

  it("filters by a tag when its chip on a card is tapped, and clears", async () => {
    const { context, set } = stubContext();
    await loadLotFilter(context);
    render(context);
    const chip = host.querySelector<HTMLButtonElement>(
      '[data-task="renewal-reminder"] .tag-chip[data-tag="Admin"]',
    )!;
    chip.click();
    expect(shownIds(host)).toEqual(["renewal-reminder"]);
    expect(set).toHaveBeenLastCalledWith(FILTER_KEY, { ...emptyFilter(), tags: ["admin"] });
    expect(
      host.querySelector('[data-task="renewal-reminder"] .tag-chip')?.getAttribute("aria-pressed"),
    ).toBe("true");
    [...host.querySelectorAll<HTMLButtonElement>(".lot-filter-summary .link-btn")][0].click();
    expect(shownIds(host)).toHaveLength(4);
  });

  it("says so when nothing matches, and the sheet still offers the way back", async () => {
    const { context } = stubContext({ ...emptyFilter(), text: "no such words" });
    await loadLotFilter(context);
    render(context);
    expect(shownIds(host)).toEqual([]);
    expect(host.querySelector(".cards .empty")?.textContent).toContain("Nothing in the lot matches");
  });

  // Review (2026-10-14): the list leaves Your move's due tasks out, the
  // Filters sheet does not.
  it("offers a tag and a repo that only a task in Your move carries", () => {
    const { context } = stubContext();
    renderLot(
      host,
      {
        lot: LOT,
        lotError: null,
        sessions: [],
        today: TODAY,
        inYourMove: new Set(["eur-currency-formatting"]),
      },
      { context, refresh: async () => undefined },
    );
    expect(shownIds(host)).not.toContain("eur-currency-formatting");
    host.querySelector<HTMLButtonElement>(".lot-filter-toggle")!.click();
    const sheet = host.querySelector<HTMLElement>(".lot-filter-sheet")!;
    expect(sheet.querySelector('.tag-chip[data-tag="content"]')).not.toBeNull();
    expect(sheet.textContent).toContain("analyticendeavors/website_content");
  });

  // 2026-10-14: the row is one line; the repo sits behind the chevron.
  it("shows the repo behind the chevron", () => {
    const { context } = stubContext();
    render(context);
    const card = host.querySelector('[data-task="graphiti-dedupe"]')!;
    expect(card.querySelector(".card-toggle .card-repo")).toBeNull();
    const repo = card.querySelector(".card-details .card-repo")!;
    expect(repo.textContent).toBe("analyticendeavors/omnigent-ae");
    expect(card.querySelector<HTMLElement>(".card-details")!.hidden).toBe(true);
    expect(host.querySelector('[data-task="sidebar-search"] .card-repo')).toBeNull();
  });
});

describe("tag chips", () => {
  beforeEach(() => {
    resetLotState();
    resetLotFilter();
    document.body.replaceChildren();
  });

  it("wear the app's colour and icon, and a plain face when the tag has none", () => {
    setTagStyles({
      count: 3,
      tags: [
        { name: "ae", tasks: 2, color: "teal", icon: "briefcase" },
        { name: "content", tasks: 1, color: "not-a-palette-name", icon: "missing-icon" },
        { name: "Admin", tasks: 1, color: "orange", icon: null },
      ],
      icons: { briefcase: ["M3 9h18", "M8 7v-2"] },
    });
    const ae = tagChip("AE");
    expect(ae.className).toBe("tag-chip tc-teal");
    expect([...ae.querySelectorAll("svg path")].map((p) => p.getAttribute("d"))).toEqual([
      "M3 9h18",
      "M8 7v-2",
    ]);
    expect(ae.tagName).toBe("SPAN");
    const content = tagChip("content");
    expect(content.className).toBe("tag-chip");
    expect(content.querySelector("svg")).toBeNull();
    expect(tagChip("admin").className).toBe("tag-chip tc-orange");
    expect(tagChip("unknown").className).toBe("tag-chip");
  });

  it("are labels, not filters, on a card outside the lot (Your move)", () => {
    const { context } = stubContext();
    const card = taskCard({ context, refresh: async () => undefined }, TASKS[0]);
    const chips = [...card.querySelectorAll(".card-tags .tag-chip")];
    expect(chips.map((chip) => chip.textContent)).toEqual(["content", "ae"]);
    expect(chips.every((chip) => chip.tagName === "SPAN")).toBe(true);
  });
});
