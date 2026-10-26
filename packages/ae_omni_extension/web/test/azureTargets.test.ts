// The Targets panel on the Azure card (2026-10-25): limits on spend and on a running
// Fabric capacity. Every call is mocked: GET /v1/ae/azure-cost/targets, and POSTs to
// /targets/{id} (save) and /targets/{id}/delete, because the extension host sends no
// PUT or DELETE.
import type { ExtensionContext } from "@omnigent/extension-sdk";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { CostTarget, CostTargetsAnswer } from "../src/api";
import {
  ALLOWED_KINDS,
  chipsOf,
  countsOf,
  defaultName,
  draftOf,
  EMPTY_TEXT,
  figureLine,
  limitText,
  meterOf,
  mountTargets,
  newDraft,
  newTargetId,
  ruleLine,
  scopeChoices,
  scopeText,
  sharedReason,
  statusLabel,
  sortedRules,
  TARGETS_OPEN_ID,
  targetsSection,
  toInput,
  valueText,
  withKind,
  withMeasure,
} from "../src/azureTargets";
import { isOpen, resetSectionState } from "../src/sections";

const sdk = vi.hoisted(() => ({ defineExtension: vi.fn() }));
vi.mock("@omnigent/extension-sdk", () => sdk);

type Reply = { ok: boolean; status: number; body: unknown };
const ok = (body: unknown): Reply => ({ ok: true, status: 200, body });
const failed = (status: number, code: string, message: string): Reply => ({
  ok: false,
  status,
  body: { error: { code, message } },
});

function rule(overrides: Partial<CostTarget> = {}): CostTarget {
  return {
    id: "t-mvp",
    name: "MVP 2025 month",
    enabled: true,
    scope: { kind: "subscription", name: "MVP 2025" },
    measure: "month_to_date",
    limit: 600,
    status: "over",
    reason: null,
    value: 640,
    dataDate: "2026-10-20",
    over: ["MVP 2025"],
    alert: null,
    ...overrides,
  };
}

function answer(rules: CostTarget[], extra: Partial<CostTargetsAnswer> = {}): CostTargetsAnswer {
  return {
    max: 50,
    rules,
    scopes: {
      subscriptions: ["MVP SC", "MVP 2025"],
      services: ["SQL Database"],
      resources: ["sql-prod-01"],
      capacities: ["cap1"],
    },
    alerts: {
      on: true,
      offReason: null,
      list: "AE Work",
      time: "09:00",
      tz: "America/Los_Angeles",
      open: 0,
      lastRun: null,
      lastError: null,
    },
    ...extra,
  };
}

type Call = { method: string; path: string; body?: unknown };

function stub(handler: (call: Call) => Reply) {
  const request = vi.fn(async (call: Call) => handler(call));
  const context = { server: { request } } as unknown as ExtensionContext;
  return { context, request };
}

const flush = async () => {
  for (let i = 0; i < 6; i++) await Promise.resolve();
  await new Promise((done) => setTimeout(done, 0));
};

async function mount(context: ExtensionContext) {
  const root = targetsSection();
  document.body.appendChild(root);
  const targets = mountTargets(context, root);
  await targets.refresh();
  await flush();
  return { root, targets };
}

beforeEach(() => {
  resetSectionState();
  document.body.replaceChildren();
});

/** Open a row's detail (rows start folded); the one named, else the first. */
function expand(root: ParentNode, name?: string): void {
  const rows = [...root.querySelectorAll<HTMLButtonElement>(".azure-target-main")];
  const hit = name ? rows.find((item) => item.textContent?.includes(name)) : rows[0];
  if (!hit) throw new Error(`no row ${name ?? ""}`);
  hit.click();
}

const text = (node: ParentNode, selector: string) =>
  [...node.querySelectorAll(selector)].map((item) => item.textContent);

function click(node: ParentNode, label: string): void {
  const hit = [...node.querySelectorAll<HTMLButtonElement>("button")].find(
    (item) => item.textContent === label,
  );
  if (!hit) throw new Error(`no button ${label}`);
  hit.click();
}

function set(node: HTMLInputElement | HTMLSelectElement, value: string): void {
  node.value = value;
  node.dispatchEvent(new Event(node instanceof HTMLSelectElement ? "change" : "input", { bubbles: true }));
}

const posts = (request: ReturnType<typeof stub>["request"]) =>
  request.mock.calls.map(([call]) => call).filter((call) => call.method === "POST");

beforeEach(() => {
  document.body.replaceChildren();
});

describe("the words", () => {
  it("writes limits and figures in the measure's own unit", () => {
    expect(limitText("month_to_date", 600)).toBe("$600");
    expect(limitText("latest_finished_day", 25.5)).toBe("$25.50");
    expect(limitText("fabric_active_hours", 6)).toBe("6 hours");
    expect(limitText("fabric_active_hours", 1)).toBe("1 hour");
    expect(valueText({ measure: "fabric_active_hours", value: 7.04 })).toBe("7 h");
    expect(valueText({ measure: "month_to_date", value: 640 })).toBe("$640");
    expect(valueText({ measure: "month_to_date", value: null })).toBeNull();
  });

  it("names what a scope judges", () => {
    expect(scopeText({ kind: "all", name: null })).toBe("All of Azure");
    expect(scopeText({ kind: "subscription", name: "MVP SC" })).toBe("MVP SC");
    expect(scopeText({ kind: "service", name: "SQL Database" })).toBe("SQL Database (service)");
    expect(scopeText({ kind: "resource", name: null })).toBe("Each resource");
    expect(scopeText({ kind: "capacity", name: null })).toBe("Each Fabric capacity");
  });

  it("reads a row as one line and a figure line", () => {
    expect(ruleLine(rule())).toBe("MVP 2025 · Month to date over $600");
    expect(ruleLine(rule({ measure: "latest_finished_day", scope: { kind: "all", name: null }, limit: 25 }))).toBe(
      "All of Azure · Latest finished day over $25 a day",
    );
    expect(figureLine(rule())).toBe("Now $640, through 2026-10-20, over: MVP 2025");
    expect(figureLine(rule({ status: "cannot-evaluate", reason: "Azure is rate limiting." }))).toBe(
      "Azure is rate limiting.",
    );
    expect(figureLine(rule({ status: "off", enabled: false }))).toMatch(/^Turned off/);
  });

  it("makes ids the server accepts", () => {
    expect(newTargetId(() => 0.5)).toMatch(/^t-[a-z0-9]{6}$/);
    expect(newTargetId(() => 0)).toBe("t-000000");
  });
});

describe("the draft", () => {
  it("offers only the scopes a measure can use, as the server does", () => {
    expect(ALLOWED_KINDS.fabric_active_hours).toEqual(["capacity"]);
    expect(ALLOWED_KINDS.month_end_forecast).not.toContain("resource");
    const moved = withMeasure({ ...newDraft(), kind: "subscription", scopeName: "MVP SC" }, "fabric_active_hours");
    expect(moved.kind).toBe("capacity");
    expect(moved.scopeName).toBe("");
    const kept = withMeasure({ ...newDraft(), kind: "subscription", scopeName: "MVP SC" }, "month_end_forecast");
    expect(kept.kind).toBe("subscription");
    expect(kept.scopeName).toBe("MVP SC");
    expect(withKind(kept, "all").scopeName).toBe("");
  });

  it("takes the pick lists from the server", () => {
    const scopes = answer([]).scopes;
    expect(scopeChoices("subscription", scopes)).toEqual(["MVP SC", "MVP 2025"]);
    expect(scopeChoices("capacity", scopes)).toEqual(["cap1"]);
    expect(scopeChoices("all", scopes)).toEqual([]);
  });

  it("checks the limit and the required name, and names an unnamed target", () => {
    expect(toInput({ ...newDraft(), limit: "" })).toMatch(/limit above 0/);
    expect(toInput({ ...newDraft(), limit: "-3" })).toMatch(/limit above 0/);
    expect(toInput({ ...newDraft(), limit: "abc" })).toMatch(/limit above 0/);
    expect(toInput({ ...newDraft(), kind: "subscription", limit: "5" })).toMatch(/Pick a subscription/);
    expect(toInput({ ...newDraft(), name: "x".repeat(81), limit: "5" })).toMatch(/80 characters/);
    expect(toInput({ ...newDraft(), kind: "subscription", scopeName: "MVP SC", limit: "600" })).toEqual({
      name: "MVP SC month to date",
      enabled: true,
      scope: { kind: "subscription", name: "MVP SC" },
      measure: "month_to_date",
      limit: 600,
    });
    expect(defaultName({ ...newDraft(), measure: "fabric_active_hours", kind: "capacity" })).toBe(
      "Each Fabric capacity fabric hours active",
    );
    expect(draftOf(rule()).limit).toBe("600");
  });
});

describe("the panel", () => {
  it("explains a target in one sentence when there are none", async () => {
    const { context } = stub(() => ok(answer([])));
    const { root } = await mount(context);
    expect(text(root, ".azure-targets-empty")).toEqual([EMPTY_TEXT]);
    expect(EMPTY_TEXT.split(". ").length).toBeLessThanOrEqual(2);
  });

  it("lists a line per target, over first, with the figure, and opens one in place", async () => {
    const rules = [
      rule({ id: "t-sc", name: "MVP SC month", status: "ok", value: 200, over: undefined }),
      rule({ id: "t-off", name: "Old", enabled: false, status: "off" }),
      rule({
        id: "t-fab",
        name: "Fabric",
        measure: "fabric_active_hours",
        scope: { kind: "capacity", name: null },
        limit: 6,
        status: "cannot-evaluate",
        reason: "Fabric is not configured on this server.",
        value: null,
      }),
      rule({
        id: "t-al",
        name: "Alerted",
        status: "alerted",
        alert: { sentAt: "2026-10-21T12:00:00Z", alarmAt: null, list: "AE Work" },
      }),
    ];
    const { context } = stub(() => ok(answer(rules)));
    const { root } = await mount(context);
    expect(root.querySelectorAll(".azure-target")).toHaveLength(4);
    expect(text(root, ".azure-target-name")[0]).toContain("Alerted");
    expect(text(root, ".azure-target-fig")).toEqual(["$640 / $600", "n/a", "$200 / $600", "off"]);
    expect(text(root, ".azure-target-name small")[3]).toBe("month to date \u00b7 off");
    // Folded rows hold no detail; opening one shows it, and a second click folds it.
    expect(root.querySelector(".azure-target-detail")).toBeNull();
    expect(root.textContent).toContain("Alerts go to AE Work at 09:00");
    expand(root, "Alerted");
    expect(root.querySelector(".azure-target-main[aria-expanded='true']")).not.toBeNull();
    expect(text(root, ".azure-target-figure")).toEqual(["Now $640, through 2026-10-20, over: MVP 2025"]);
    expect(root.textContent).toContain("Task filed in AE Work.");
    expand(root, "Alerted");
    expect(root.querySelector(".azure-target-detail")).toBeNull();
    expect(root.textContent).not.toMatch(/[\u{1F300}-\u{1FAFF}]/u);
  });

  it("says alerts are off when the server has no task store, and still shows targets", async () => {
    const off = answer([rule()], {
      alerts: { ...answer([]).alerts, on: false, offReason: "No task store is configured." },
    });
    const { context } = stub(() => ok(off));
    const { root } = await mount(context);
    expect(text(root, ".notice")).toEqual([
      "Alerts are off (No task store is configured.). Targets still show their status.",
    ]);
    expect(root.querySelectorAll(".azure-target")).toHaveLength(1);
  });

  it("adds a target with a POST to its own id and reads the list again", async () => {
    let list: CostTarget[] = [];
    const { context, request } = stub((call) => {
      if (call.method === "GET") return ok(answer(list));
      list = [rule({ id: call.path.split("/").pop() ?? "" })];
      return ok({ rule: list[0] });
    });
    const { root } = await mount(context);
    click(root, "+ Add target");
    await flush();
    const selects = root.querySelectorAll<HTMLSelectElement>("form select");
    // Measure, Applies to; the measure's first scope is all of Azure, which needs no name.
    expect(selects).toHaveLength(2);
    set(selects[1], "subscription");
    await flush();
    const form = root.querySelector("form") as HTMLFormElement;
    set(form.querySelectorAll<HTMLSelectElement>("select")[2], "MVP 2025");
    set(form.querySelector<HTMLInputElement>('input[type="number"]') as HTMLInputElement, "600");
    click(form, "Add target");
    await flush();
    const sent = posts(request);
    expect(sent).toHaveLength(1);
    expect(sent[0].path).toMatch(/^\/v1\/ae\/azure-cost\/targets\/t-[a-z0-9]{6}$/);
    expect(sent[0].body).toEqual({
      name: "MVP 2025 month to date",
      enabled: true,
      scope: { kind: "subscription", name: "MVP 2025" },
      measure: "month_to_date",
      limit: 600,
    });
    expect(root.querySelector("form")).toBeNull();
    expect(root.querySelectorAll(".azure-target")).toHaveLength(1);
  });

  it("shows the server's message when a save is refused, and keeps the form", async () => {
    const { context } = stub((call) =>
      call.method === "GET"
        ? ok(answer([]))
        : failed(422, "invalid_input", 'The subscription "Nope" is not one this server reads. Pick one from the list.'),
    );
    const { root } = await mount(context);
    click(root, "+ Add target");
    await flush();
    set(root.querySelector('input[type="number"]') as HTMLInputElement, "10");
    click(root.querySelector("form") as HTMLFormElement, "Add target");
    await flush();
    expect(text(root, "form .notice")[0]).toContain("Pick one from the list");
    expect(root.querySelector("form")).not.toBeNull();
  });

  it("refuses a bad limit before calling the server", async () => {
    const { context, request } = stub(() => ok(answer([])));
    const { root } = await mount(context);
    click(root, "+ Add target");
    await flush();
    click(root.querySelector("form") as HTMLFormElement, "Add target");
    await flush();
    expect(text(root, "form .notice")[0]).toMatch(/limit above 0/);
    expect(posts(request)).toHaveLength(0);
  });

  it("edits a target in place under its own id", async () => {
    const { context, request } = stub((call) =>
      call.method === "GET" ? ok(answer([rule()])) : ok({ rule: rule({ limit: 900 }) }),
    );
    const { root } = await mount(context);
    expand(root);
    click(root, "Edit");
    await flush();
    const limit = root.querySelector<HTMLInputElement>('input[type="number"]') as HTMLInputElement;
    expect(limit.value).toBe("600");
    set(limit, "900");
    click(root.querySelector("form") as HTMLFormElement, "Save target");
    await flush();
    const [sent] = posts(request);
    expect(sent.path).toBe("/v1/ae/azure-cost/targets/t-mvp");
    expect((sent.body as { limit: number }).limit).toBe(900);
  });

  it("switches a target off with the same rule, enabled flipped", async () => {
    const { context, request } = stub((call) =>
      call.method === "GET" ? ok(answer([rule()])) : ok({ rule: rule({ enabled: false }) }),
    );
    const { root } = await mount(context);
    expand(root);
    click(root, "On");
    await flush();
    const [sent] = posts(request);
    expect(sent.path).toBe("/v1/ae/azure-cost/targets/t-mvp");
    expect(sent.body).toEqual({
      name: "MVP 2025 month",
      enabled: false,
      scope: { kind: "subscription", name: "MVP 2025" },
      measure: "month_to_date",
      limit: 600,
    });
  });

  it("asks in the page before it deletes, and never uses window.confirm", async () => {
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(true);
    const { context, request } = stub((call) =>
      call.method === "GET" ? ok(answer([rule()])) : ok({ ok: true, deleted: "t-mvp" }),
    );
    const { root } = await mount(context);
    expand(root);
    click(root, "Delete");
    await flush();
    expect(posts(request)).toHaveLength(0);
    expect(root.textContent).toContain("Delete this target?");
    click(root, "Keep");
    await flush();
    expect(root.textContent).not.toContain("Delete this target?");
    click(root, "Delete");
    await flush();
    const buttons = [...root.querySelectorAll<HTMLButtonElement>(".azure-confirm button")];
    buttons.find((item) => item.textContent === "Delete")?.click();
    await flush();
    const [sent] = posts(request);
    expect(sent.path).toBe("/v1/ae/azure-cost/targets/t-mvp/delete");
    expect(confirm).not.toHaveBeenCalled();
  });

  it("keeps what is typed when a poll lands", async () => {
    const { context } = stub(() => ok(answer([rule()])));
    const { root, targets } = await mount(context);
    expand(root);
    click(root, "Edit");
    await flush();
    const limit = root.querySelector<HTMLInputElement>('input[type="number"]') as HTMLInputElement;
    set(limit, "777");
    await targets.refresh();
    await flush();
    expect(root.querySelector<HTMLInputElement>('input[type="number"]')?.value).toBe("777");
  });

  it("offers only the scopes the measure allows and rebuilds the picker on a change", async () => {
    const { context } = stub(() => ok(answer([])));
    const { root } = await mount(context);
    click(root, "+ Add target");
    await flush();
    const measure = root.querySelectorAll<HTMLSelectElement>("form select")[0];
    set(measure, "fabric_active_hours");
    await flush();
    const selects = root.querySelectorAll<HTMLSelectElement>("form select");
    expect([...selects[1].options].map((item) => item.textContent)).toEqual(["Fabric capacity"]);
    expect([...selects[2].options].map((item) => item.textContent)).toEqual(["Each capacity", "cap1"]);
    expect(root.textContent).toContain("Limit (hours)");
  });

  it("says why when the targets cannot be read, and keeps quiet for a non-admin", async () => {
    const broken = stub(() => failed(503, "cost_targets_unreadable", "The targets file cannot be read."));
    const first = await mount(broken.context);
    expect(text(first.root, ".notice")[0]).toBe("Targets could not be read (The targets file cannot be read.).");
    document.body.replaceChildren();
    const denied = stub(() => failed(403, "admin_only", "Only an OmniGent admin can see cost targets."));
    const second = await mount(denied.context);
    expect(second.root.hidden).toBe(true);
    expect(text(second.root, ".notice")).toEqual([]);
    await second.targets.refresh();
    expect(denied.request).toHaveBeenCalledTimes(1);
  });
});

describe("the folded section", () => {
  const mixed = () => [
    rule({ id: "a", name: "AnalyticEndeavors", limit: 50, value: 61, alert: { sentAt: "x", alarmAt: null, list: "AE Work" } }),
    rule({ id: "b", name: "Second", limit: 100, value: 150 }),
    rule({ id: "c", name: "Third", limit: 100, value: 120 }),
    rule({ id: "d", name: "Fine", status: "ok", value: 10, limit: 100 }),
    rule({ id: "e", name: "Old", status: "off", value: null }),
  ];

  it("starts closed, says the counts, and remembers being opened", async () => {
    const { context } = stub(() => ok(answer(mixed())));
    const { root } = await mount(context);
    const head = root.querySelector<HTMLButtonElement>(".azure-targets-head") as HTMLButtonElement;
    expect(head.getAttribute("aria-expanded")).toBe("false");
    expect((root.querySelector(".azure-targets-body") as HTMLElement).hidden).toBe(true);
    expect(text(root, ".azure-targets-sum")[0]).toBe("5 \u00b7 3 over \u00b7 1 ok \u00b7 1 off");
    head.click();
    expect(head.getAttribute("aria-expanded")).toBe("true");
    expect((root.querySelector(".azure-targets-body") as HTMLElement).hidden).toBe(false);
    expect(isOpen(TARGETS_OPEN_ID, false)).toBe(true);
    // A new mount reads the remembered state.
    document.body.replaceChildren();
    const again = await mount(context);
    expect(again.root.querySelector(".azure-targets-head")?.getAttribute("aria-expanded")).toBe("true");
    again.root.querySelector<HTMLButtonElement>(".azure-targets-head")?.click();
    expect(isOpen(TARGETS_OPEN_ID, true)).toBe(false);
  });

  it("leaves out a zero count and shows the cannot-check count in amber", () => {
    const rules = [
      rule({ id: "a", status: "cannot-evaluate", value: null, reason: "No reading." }),
      rule({ id: "b", status: "ok", value: 1 }),
    ];
    expect(countsOf(rules)).toEqual({ total: 2, over: 0, ok: 1, cannot: 1, off: 0 });
  });

  it("shows the first red chip and +N more for the rest, in the header beside the counts", async () => {
    const { context } = stub(() => ok(answer(mixed())));
    const { root } = await mount(context);
    const chips = text(root, ".azure-targets-head .azure-chip");
    expect(chips).toEqual(["Second $150 of $100", "+2 more"]);
    expect(root.querySelectorAll(".azure-chip-err")).toHaveLength(2);
  });

  it("counts an amber chip into +N more, amber when nothing else is over", () => {
    const why = "Azure has not been read since 2026-10-26 10:12 UTC, too old to judge a target.";
    const over = rule({ id: "a", name: "A", status: "over", value: 650 });
    const cannot = rule({ id: "b", status: "cannot-evaluate", value: null, reason: why });
    expect(chipsOf([over, cannot]).map((chip) => [chip.tone, chip.text])).toEqual([
      ["err", "A $650 of $600"],
      ["warn", "+1 more"],
    ]);
  });

  it("puts the full chip text in a title", async () => {
    const { context } = stub(() => ok(answer(mixed())));
    const { root } = await mount(context);
    const chip = root.querySelector<HTMLElement>(".azure-targets-head .azure-chip");
    expect(chip?.title).toBe(chip?.textContent);
  });

  it("names each row's status to a screen reader, not only by its dot", async () => {
    const { context } = stub(() => ok(answer(mixed())));
    const { root } = await mount(context);
    for (const row of root.querySelectorAll<HTMLElement>(".azure-target-main")) {
      const id = row.getAttribute("data-rule-id");
      const found = mixed().find((item) => item.id === id);
      expect(found).toBeDefined();
      expect(row.textContent).toContain(statusLabel(found!.status));
    }
    expect(root.querySelector(".azure-target-main .azure-dot")?.getAttribute("aria-hidden")).toBe("true");
  });

  it("keeps keyboard focus on the row after a redraw, and the switch has no aria-pressed", async () => {
    const { context } = stub(() => ok(answer(mixed())));
    const { root, targets } = await mount(context);
    root.querySelector<HTMLButtonElement>(".azure-targets-head")?.click();
    const row = root.querySelector<HTMLButtonElement>(".azure-target-main");
    const id = row?.getAttribute("data-rule-id");
    row?.focus();
    expect(document.activeElement).toBe(row);
    await targets.refresh();
    await flush();
    const after = document.activeElement as HTMLElement;
    expect(after.classList.contains("azure-target-main")).toBe(true);
    expect(after.getAttribute("data-rule-id")).toBe(id);
    row?.click();
    const again = document.activeElement as HTMLElement;
    expect(again.getAttribute("data-rule-id")).toBe(id);
    const add = root.querySelector<HTMLButtonElement>(".azure-add");
    add?.focus();
    await targets.refresh();
    await flush();
    expect(document.activeElement?.classList.contains("azure-add")).toBe(true);
    expect(root.querySelector(".azure-target-toggle")?.hasAttribute("aria-pressed")).toBe(false);
  });

  it("shows one green chip when every target is under its limit", () => {
    const rules = [rule({ status: "ok", value: 1 }), rule({ id: "o", status: "off", value: null })];
    expect(chipsOf(rules)).toEqual([{ tone: "ok", text: "Every target is under its limit" }]);
    expect(chipsOf([rule({ status: "off", value: null })])).toEqual([]);
  });

  it("shows one amber chip, and the shared reason once when every target has it", async () => {
    const why = "Azure has not been read since 2026-10-26 10:12 UTC, too old to judge a target.";
    const rules = [
      rule({ id: "a", status: "cannot-evaluate", value: null, reason: why }),
      rule({ id: "b", status: "cannot-evaluate", value: null, reason: why }),
      rule({ id: "c", status: "off", value: null }),
    ];
    expect(sharedReason(rules)).toBe(why);
    expect(chipsOf(rules)).toEqual([{ tone: "warn", text: "Azure has not been read since 2026-10-26 10:12 UTC" }]);
    const { context } = stub(() => ok(answer(rules, { note: "Azure is rate limiting; figures as of 10:12 UTC" })));
    const { root } = await mount(context);
    expect(text(root, ".azure-targets-shared")).toEqual([why]);
    expect(text(root, ".azure-target-figure")).toEqual([]);
    expand(root);
    expect(text(root, ".azure-target-figure")).toEqual([]);
    expect(root.textContent).toContain("Azure is rate limiting; figures as of 10:12 UTC.");
    // Different reasons are said on each row, with a count in the chip.
    const split = [rules[0], { ...rules[1], reason: "Other." }];
    expect(sharedReason(split)).toBeNull();
    expect(chipsOf(split)[0].text).toBe("2 cannot be checked");
  });
});

describe("order and meters", () => {
  it("sorts over first, then cannot check, then ok by how full, then off", () => {
    const rules = [
      rule({ id: "off", status: "off", value: null }),
      rule({ id: "low", status: "ok", value: 10, limit: 100 }),
      rule({ id: "cannot", status: "cannot-evaluate", value: null }),
      rule({ id: "high", status: "ok", value: 70, limit: 100 }),
      rule({ id: "over", status: "over", value: 120, limit: 100 }),
      rule({ id: "alerted", status: "alerted", value: 300, limit: 100 }),
    ];
    expect(sortedRules(rules).map((item) => item.id)).toEqual([
      "alerted",
      "over",
      "cannot",
      "high",
      "low",
      "off",
    ]);
  });

  it("fills value over limit, capped, red when over, amber from 75%, else green", () => {
    const at = (value: number | null, status: CostTarget["status"] = "ok") =>
      meterOf(rule({ value, limit: 100, status }));
    expect(at(74)).toEqual({ pct: 74, tone: "ok" });
    expect(at(75)).toEqual({ pct: 75, tone: "near" });
    expect(at(99)).toEqual({ pct: 99, tone: "near" });
    expect(at(250, "over")).toEqual({ pct: 100, tone: "over" });
    expect(at(null)).toEqual({ pct: 0, tone: "none" });
    expect(at(50, "off")).toEqual({ pct: 0, tone: "none" });
    expect(at(50, "cannot-evaluate")).toEqual({ pct: 0, tone: "none" });
  });

  it("draws the meter with its tone and width", async () => {
    const { context } = stub(() =>
      ok(answer([rule({ id: "n", status: "ok", value: 80, limit: 100 }), rule({ id: "g", status: "ok", value: 5, limit: 100 })])),
    );
    const { root } = await mount(context);
    const meters = [...root.querySelectorAll<HTMLElement>(".azure-meter")];
    expect(meters.map((item) => item.dataset.tone)).toEqual(["near", "ok"]);
    expect(meters[0].querySelector("i")?.getAttribute("style")).toBe("width:80%");
  });
});
