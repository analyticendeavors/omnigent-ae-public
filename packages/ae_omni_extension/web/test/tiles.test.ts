// The Admin Center dashboard (2026-10-20): the status tile strip (tiles.ts),
// each section's tile builder, and the cards' columns and phone folds
// (board.ts). The builders are pure; the strip and the board are plain DOM.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { accountsTile } from "../src/accounts";
import type {
  AccountRow,
  DatabricksOverview,
  DeployHold,
  HostStatus,
  SecretsStatus,
} from "../src/api";
import {
  adminCard,
  arrange,
  type CardId,
  CARD_TILES,
  columnsFor,
  cardNotSetUp,
  GROUP_LAYOUTS,
  groupOf,
  openByDefault,
} from "../src/board";
import { packedArrangement, packLayout } from "../src/boardLayout";
import { FLASH_MS, goToGroup } from "../src/cardNav";
import { creditHealth, creditTile, databricksTile } from "../src/databricks";
import { holdTile, releaseTile } from "../src/deploys";
import { fabricTile } from "../src/fabric";
import type { Release } from "../src/header";
import { hostTile } from "../src/host";
import { secretsTile } from "../src/secrets";
import { resetSectionState, setOpen } from "../src/sections";
import {
  DOMAINS,
  getTile,
  type Health,
  mountTiles,
  refreshAll,
  registerRefresh,
  resetTiles,
  setTile,
  shortLeft,
  staleTile,
  type Tile,
  type TileId,
  worst,
} from "../src/tiles";

const sdk = vi.hoisted(() => ({ defineExtension: vi.fn() }));
vi.mock("@omnigent/extension-sdk", () => sdk);

const NOW = Date.parse("2026-10-20T18:00:00Z");

/** An account row with a 5-hour and a weekly reading, both resetting later. */
function account(label: string, five: number, week: number): AccountRow {
  const later = new Date(NOW + 3_600_000).toISOString();
  const window = (id: string, used: number, minutes: number) => ({
    id,
    label: id,
    used_percent: used,
    resets_at: later,
    window_minutes: minutes,
  });
  return {
    harness: "claude",
    account: label,
    label,
    plan: null,
    five_hour: window("five_hour", five, 300),
    weekly: window("seven_day", week, 10080),
    observed_at: new Date(NOW - 60_000).toISOString(),
    live_sessions: 0,
  };
}

const release = (over: Partial<Release> = {}): Release => ({
  running: "ae-v0.18.0.22",
  latest: "ae-v0.18.0.22",
  state: "current",
  mid_turn: 0,
  error: null,
  ...over,
});

const hold = (over: Partial<DeployHold> = {}): DeployHold => ({
  held: true,
  until: new Date(NOW + 110 * 60_000).toISOString(),
  set_at: new Date(NOW - 70 * 60_000).toISOString(),
  set_by: "Owner",
  remaining_s: 110 * 60,
  choices: [1, 2, 3],
  error: null,
  ...over,
});

const host = (state: HostStatus["state"]): HostStatus =>
  ({
    host: { id: "h", name: "example-box", status: "online" },
    state,
  }) as HostStatus;

const secrets = (missing: string[] = []): SecretsStatus => ({
  required: [
    { key: "A", use: "Database", present: !missing.includes("A") },
    { key: "B", use: "Sign-in", present: true },
  ],
  optional: [{ key: "C", use: "GitHub", present: true }],
  missing,
});

const databricks = (
  state: string,
  monthUsd: number | null,
): DatabricksOverview => ({
  configured: true,
  can_act: true,
  note: null,
  host: null,
  error: null,
  warehouses: [
    {
      id: "w",
      name: "Starter",
      state,
      size: "2X-Small",
      autoStopMins: 5,
      serverless: true,
      url: "",
    },
  ],
  spend: {
    status: "ok",
    message: null,
    readAt: null,
    asOf: null,
    monthUsd,
    totalUsd: monthUsd,
    creditUsd: 500,
    cadence: "monthly",
    creditStart: "2030-01-01",
    creditEnd: "2030-12-31",
    warehouseId: "w",
  },
});

beforeEach(() => {
  document.body.replaceChildren();
  resetTiles();
  resetSectionState();
});

afterEach(() => {
  resetTiles();
});

describe("the words", () => {
  it("ranks healths and shortens a time left", () => {
    expect(worst(["ok", "idle"])).toBe("ok");
    expect(worst(["ok", "problem", "attention"])).toBe("problem");
    expect(worst([])).toBe("idle");
    expect(shortLeft(110 * 60)).toBe("1 h 50 m");
    expect(shortLeft(7200)).toBe("2 h");
    expect(shortLeft(30)).toBe("1 m");
  });

  it("marks a tile built from the last answer when the newest read failed", () => {
    const ok = {
      id: "fabric" as const,
      value: "Active",
      note: "F2",
      health: "ok" as const,
    };
    expect(staleTile(ok, null)).toBe(ok);
    expect(staleTile(ok, new Error("x"))).toMatchObject({
      note: "F2 \u00b7 last answer",
      health: "attention",
    });
    expect(staleTile({ ...ok, brief: "Held 2 h" }, new Error("x")).brief).toBe("Held 2 h, last answer");
    expect(
      staleTile({ ...ok, note: "", health: "problem" }, new Error("x")),
    ).toMatchObject({
      note: "last answer",
      health: "problem",
    });
  });

  it("builds the Release and Deploy hold tiles", () => {
    expect(releaseTile(release(), null)).toMatchObject({
      value: "v0.18.0.22",
      health: "ok",
    });
    const waiting = releaseTile(
      release({ latest: "ae-v0.18.0.23", state: "waiting" }),
      null,
      true,
    );
    expect(waiting).toMatchObject({
      note: "v0.18.0.23 waiting for the hold",
      health: "attention",
    });
    expect(releaseTile(null, new Error("x")).health).toBe("attention");
    const held = holdTile(hold(), null, NOW, NOW);
    expect(held).toMatchObject({ value: "1 h 50 m", health: "attention" });
    // 110 of the hold's 180 minutes are left.
    expect(held.bar).toBe(61);
    expect(held.brief).toBe("Held 1 h 50 m");
    expect(holdTile(hold({ held: false }), null, NOW, NOW)).toMatchObject({
      value: "Off",
      health: "idle",
    });
  });

  it("builds the host, secrets and accounts tiles", () => {
    expect(hostTile(host("connected"), null)).toMatchObject({
      value: "Connected",
      health: "ok",
    });
    expect(hostTile(host("rejected"), null).health).toBe("problem");
    expect(hostTile(host("sent"), null).health).toBe("attention");
    expect(secretsTile(secrets(), null)).toMatchObject({
      value: "3 of 3",
      health: "ok",
    });
    expect(secretsTile(secrets(["A"]), null)).toMatchObject({
      value: "2 of 3",
      note: "1 required missing",
      health: "problem",
    });
    expect(
      accountsTile([account("A", 10, 20), account("B", 72, 5)], NOW),
    ).toMatchObject({
      value: "2",
      note: "fullest 72% \u00b7 Manage in Settings",
      health: "attention",
      summary: "B 72% 5-hour",
    });
    expect(accountsTile([account("A", 10, 91)], NOW)).toMatchObject({
      health: "problem",
      summary: "A 91% weekly",
    });
    expect(accountsTile([account("A", 10, 20)], NOW).health).toBe("ok");
    expect(accountsTile([], NOW)).toMatchObject({
      value: "0",
      note: "Manage in Settings",
      health: "idle",
    });
    expect(accountsTile(null, NOW, new Error("x"))).toMatchObject({
      value: "Unknown",
      health: "attention",
    });
    expect(accountsTile(null, NOW).note).toBe("Manage in Settings");
  });

  it("builds the Fabric, Databricks and credit tiles", () => {
    const active = fabricTile(
      {
        configured: true,
        can_act: true,
        capacities: [
          {
            name: "aetraining",
            state: "Active",
            can_suspend: true,
            hourlyUsd: 0.72,
            resumedAt: new Date(NOW - 2 * 3_600_000).toISOString(),
          },
        ],
      },
      null,
      NOW,
    );
    expect(active).toMatchObject({
      value: "Active",
      note: "aetraining · $1.44 so far",
    });
    expect(active.health).toBe("ok");
    const stopped = databricksTile(databricks("STOPPED", 12.4), null);
    expect(stopped).toMatchObject({
      value: "Stopped",
      note: "$12.40 of $500 used",
      health: "idle",
    });
    expect(creditTile(databricks("STOPPED", 12.4)).value).toBe("$487.60 left");
    expect(creditTile(databricks("STOPPED", 12.4)).brief).toBe("$12.40 of $500 credit");
    expect(databricksTile(databricks("STOPPED", 12.4), null).brief).toBe("Databricks stopped");
    expect(databricksTile(databricks("RUNNING", 400), null).brief).toBe("$400 of $500 credit");
    expect(creditHealth(databricks("RUNNING", 400).spend)).toBe("attention");
    expect(creditHealth(databricks("RUNNING", 510).spend)).toBe("problem");
    expect(databricksTile(databricks("STOPPED", 510), null).health).toBe(
      "problem",
    );
    const failed = {
      ...databricks("STOPPED", 12.4),
      warehouses: [],
      error: { code: "timeout", message: "The listing timed out." },
    };
    expect(databricksTile(failed, null)).toMatchObject({
      value: "Unknown",
      health: "attention",
    });
  });
});

describe("the pills and the header", () => {
  const mount = () => {
    const strip = document.createElement("div");
    const time = document.createElement("p");
    mountTiles(strip, time);
    return { strip, time };
  };
  const tile = (id: TileId, health: Health, value: string = id, note = "", extra: Partial<Tile> = {}): Tile => ({
    id,
    value,
    note,
    health,
    ...extra,
  });
  const pill = (strip: HTMLElement, id: string) => strip.querySelector<HTMLElement>(`[data-pill="${id}"]`);

  it("draws one pill per group in order, the worst member's health, and the newest time", () => {
    const { strip, time } = mount();
    setTile(secretsTile(secrets(["A"]), null), NOW);
    setTile(releaseTile(release(), null), NOW - 60_000);
    // The credit tile belongs to no pill; it carries a card's fold.
    setTile(creditTile(databricks("STOPPED", 12.4)));
    const pills = [...strip.querySelectorAll<HTMLElement>(".pill")];
    expect(pills.map((node) => node.dataset.pill)).toEqual(["ship", "box"]);
    expect(pills[0].classList.contains("pill-ok")).toBe(true);
    expect(pills[1].classList.contains("pill-problem")).toBe(true);
    expect(pills[1].querySelector(".pill-h svg")?.getAttribute("aria-hidden")).toBe("true");
    expect(time.textContent).toMatch(/^Updated /);
    expect(getTile("credit")?.value).toBe("$487.60 left");
  });

  it("holds the four groups the owner approved", () => {
    expect(DOMAINS.map((d) => [d.id, d.label])).toEqual([
      ["ship", "Ship"],
      ["box", "Box"],
      ["cloud", "Cloud"],
      ["money", "Accounts"],
    ]);
    expect(Object.fromEntries(DOMAINS.map((d) => [d.id, d.members]))).toEqual({
      ship: ["release", "hold", "git", "social"],
      box: ["boxhealth", "host", "secrets", "homesecurity"],
      cloud: ["fabric", "databricks", "azure", "vault", "decision"],
      money: ["accounts", "presets", "family"],
    });
  });

  it("names, for each group, cards that hold its member tiles", () => {
    for (const domain of DOMAINS) {
      const held = domain.cards.flatMap((card) => CARD_TILES[card as CardId]);
      for (const member of domain.members) expect(held, `${domain.id} ${member}`).toContain(member);
    }
  });

  it("leaves out a pill whose members are all missing or idle", () => {
    const { strip } = mount();
    setTile(tile("accounts", "idle"));
    setTile(tile("presets", "idle"));
    expect(strip.querySelector(".pill")).toBeNull();
    setTile(tile("family", "ok", "Fine"));
    expect(pill(strip, "money")).not.toBeNull();
  });

  it("takes the worst health of the members", () => {
    const { strip } = mount();
    setTile(tile("fabric", "ok"));
    setTile(tile("databricks", "attention", "Down", "check"));
    expect(pill(strip, "cloud")!.classList.contains("pill-attention")).toBe(true);
    setTile(tile("vault", "problem", "Bad", "broken"));
    expect(pill(strip, "cloud")!.classList.contains("pill-problem")).toBe(true);
  });

  it("says brief first, never the note, problems before attention before ok, and skips idle ones", () => {
    const { strip } = mount();
    setTile(tile("release", "ok", "v1", "up to date", { summary: "Release v1" }));
    setTile(tile("hold", "attention", "2 h", "held by someone", { brief: "Held 2 h", summary: "Hold on" }));
    setTile(tile("git", "problem", "9 need you", "1 to fix, 3 waiting", { brief: "9 need you" }));
    const line = pill(strip, "ship")!.querySelector(".pill-line")!;
    expect(line.textContent).toBe("9 need you \u00b7 Held 2 h \u00b7 Release v1");
    expect([...line.querySelectorAll("b")].map((b) => b.textContent)).toEqual(["9 need you", "Held 2 h"]);
    expect(line.textContent).not.toContain("held by someone");
    setTile(tile("git", "idle", "Not set up", "no repos", { brief: "9 need you" }));
    expect(line.textContent).toBe("Held 2 h \u00b7 Release v1");
  });

  it("drops a part whose text repeats an earlier one", () => {
    const { strip } = mount();
    setTile(tile("accounts", "attention", "6.7B tokens", "", { brief: "6.7B tokens in 30 d" }));
    setTile(tile("presets", "ok", "x", "", { brief: "6.7B tokens in 30 d" }));
    setTile(tile("family", "ok", "x", "", { brief: "249 families" }));
    const line = pill(strip, "money")!.querySelector(".pill-line")!;
    expect(line.textContent).toBe("6.7B tokens in 30 d \u00b7 249 families");
  });

  it("hands focus to the strip when the focused pill goes idle and is removed", () => {
    const { strip } = mount();
    document.body.append(strip);
    setTile(tile("host", "ok", "Connected"));
    pill(strip, "box")!.focus();
    setTile(tile("host", "idle", "Admins only"));
    expect(pill(strip, "box")).toBeNull();
    expect(document.activeElement).toBe(strip);
    strip.remove();
  });

  it("shows a compact number in the header, and the full count in the accessible name", () => {
    const { strip } = mount();
    const number = () => pill(strip, "cloud")!.querySelector<HTMLElement>(".pill-count")!;
    const name = () => pill(strip, "cloud")!.getAttribute("aria-label")!;
    setTile(tile("fabric", "attention"));
    expect(number().textContent).toBe("1");
    expect(name()).toMatch(/^Cloud, 1 to check: /);
    setTile(tile("databricks", "attention"));
    expect(number().textContent).toBe("2");
    expect(name()).toMatch(/^Cloud, 2 to check: /);
    setTile(tile("vault", "problem"));
    expect(number().textContent).toBe("1");
    expect(name()).toMatch(/^Cloud, 1 problem: /);
    setTile(tile("azure", "problem"));
    expect(name()).toMatch(/^Cloud, 2 problems: /);
    setTile(tile("host", "ok"));
    expect(pill(strip, "box")!.querySelector<HTMLElement>(".pill-count")!.hidden).toBe(true);
    expect(pill(strip, "box")!.getAttribute("aria-label")).toMatch(/^Box: /);
  });

  it("keeps focus and the same nodes across a setTile, and gives the button no title", () => {
    const { strip } = mount();
    setTile(tile("host", "ok", "Connected"));
    const before = pill(strip, "box")!;
    document.body.append(strip);
    before.focus();
    expect(document.activeElement).toBe(before);
    setTile(tile("host", "attention", "Down", "check"));
    setTile(tile("secrets", "ok", "8 of 8"));
    expect(pill(strip, "box")).toBe(before);
    expect(document.activeElement).toBe(before);
    expect(before.classList.contains("pill-attention")).toBe(true);
    expect(before.hasAttribute("title")).toBe(false);
    strip.remove();
  });

  it("lists every member in the label, the title and the popover", () => {
    const { strip } = mount();
    setTile(releaseTile(release(), null));
    setTile(tile("hold", "ok", "Off", "deploys go ahead"));
    const node = pill(strip, "ship")!;
    expect(node.getAttribute("aria-label")).toMatch(/^Ship: Release: .*; Deploy hold: /);
    const rows = [...strip.querySelectorAll(".pill-pop .pill-pop-row")];
    expect(rows).toHaveLength(2);
    expect(rows[0].textContent).toContain("Release");
    expect(strip.querySelector(".pill-pop")?.getAttribute("aria-hidden")).toBe("true");
  });

  it("shows the Deploy hold's bar inside the Ship pill", () => {
    const { strip } = mount();
    setTile(tile("hold", "attention", "On", "1 h left", { bar: 40 }));
    const bar = pill(strip, "ship")!.querySelector<HTMLElement>(".tile-bar > i");
    expect(bar?.style.width).toBe("40%");
  });

  it("scrolls to its group's heading and focuses the first card, opening it on a phone", () => {
    const { strip } = mount();
    const group = document.createElement("section");
    group.className = "admin-group";
    group.dataset.group = "box";
    const box = document.createElement("article");
    box.className = "admin-card is-folded";
    box.dataset.card = "box";
    const toggle = document.createElement("button");
    toggle.className = "admin-card-toggle";
    toggle.addEventListener("click", () => box.classList.remove("is-folded"));
    box.append(toggle);
    group.append(box);
    const scroll = vi.fn();
    group.scrollIntoView = scroll;
    document.body.append(group);
    setTile(tile("host", "ok", "Connected"));
    pill(strip, "box")!.click();
    expect(box.classList.contains("is-folded")).toBe(false);
    expect(scroll).toHaveBeenCalled();
    expect(document.activeElement).toBe(toggle);
    group.remove();
  });

  it("on the packed board scrolls to the group's first card, focuses its header and rings every card of the group", () => {
    vi.useFakeTimers();
    try {
      const { strip } = mount();
      const board = document.createElement("div");
      board.className = "admin-board";
      board.dataset.packed = "";
      const make = (id: string, group: string) => {
        const card = document.createElement("section");
        card.className = "admin-card";
        card.dataset.card = id;
        card.dataset.group = group;
        card.append(Object.assign(document.createElement("button"), { className: "admin-card-toggle" }));
        card.scrollIntoView = vi.fn();
        return card;
      };
      const compute = make("compute", "cloud");
      const azure = make("azure", "cloud");
      const deploys = make("deploys", "ship");
      board.append(deploys, compute, azure);
      document.body.append(board);
      setTile(tile("fabric", "ok", "Active"));
      pill(strip, "cloud")!.click();
      expect(compute.scrollIntoView).toHaveBeenCalled();
      expect(deploys.scrollIntoView).not.toHaveBeenCalled();
      expect(document.activeElement).toBe(compute.querySelector(".admin-card-toggle"));
      expect([compute, azure].map((c) => c.classList.contains("is-flash"))).toEqual([true, true]);
      expect(deploys.classList.contains("is-flash")).toBe(false);
      vi.advanceTimersByTime(FLASH_MS - 1);
      expect(compute.classList.contains("is-flash")).toBe(true);
      vi.advanceTimersByTime(1);
      expect([compute, azure].map((c) => c.classList.contains("is-flash"))).toEqual([false, false]);
      // A second click while the ring shows restarts it for the full time.
      pill(strip, "cloud")!.click();
      vi.advanceTimersByTime(1000);
      pill(strip, "cloud")!.click();
      vi.advanceTimersByTime(FLASH_MS - 1);
      expect([compute, azure].map((c) => c.classList.contains("is-flash"))).toEqual([true, true]);
      vi.advanceTimersByTime(1);
      expect([compute, azure].map((c) => c.classList.contains("is-flash"))).toEqual([false, false]);
      board.remove();
    } finally {
      vi.useRealTimers();
    }
  });

  it("goToGroup says false when no card of the group is on the page", () => {
    expect(goToGroup("cloud")).toBe(false);
    const board = document.createElement("div");
    board.className = "admin-board";
    board.dataset.packed = "";
    document.body.append(board);
    expect(goToGroup("cloud")).toBe(false);
    board.remove();
  });

  it("falls back to the first card when the board has no groups (the box-only view)", () => {
    const { strip } = mount();
    const box = document.createElement("article");
    box.className = "admin-card";
    box.dataset.card = "box";
    const toggle = document.createElement("button");
    toggle.className = "admin-card-toggle";
    box.append(toggle);
    const scroll = vi.fn();
    box.scrollIntoView = scroll;
    document.body.append(box);
    setTile(tile("host", "ok", "Connected"));
    pill(strip, "box")!.click();
    expect(scroll).toHaveBeenCalled();
    expect(document.activeElement).toBe(toggle);
    box.remove();
  });

  it("leaves a not-set-up member out of its pill, and hides the pill when every member is out", () => {
    const { strip } = mount();
    setTile(tile("fabric", "ok", "Active"));
    setTile({ ...tile("databricks", "idle", "Not set up"), notSetUp: true });
    setTile({ ...tile("vault", "idle", "Not set up"), notSetUp: true });
    const rows = [...strip.querySelectorAll('[data-pill="cloud"] ~ .pill-pop .pill-pop-name')].map(
      (row) => row.textContent,
    );
    expect(rows).toEqual(["Fabric"]);
    setTile({ ...tile("fabric", "idle", "Not set up"), notSetUp: true });
    expect(pill(strip, "cloud")).toBeNull();
  });

  it("runs every registered read from the refresh button", () => {
    const one = vi.fn();
    const two = vi.fn();
    registerRefresh("one", one);
    registerRefresh("two", two);
    refreshAll();
    expect(one).toHaveBeenCalledOnce();
    expect(two).toHaveBeenCalledOnce();
  });
});

describe("the board", () => {
  const cards = () =>
    Object.fromEntries(
      (
        ["deploys", "accounts", "compute", "credit", "azure", "box", "home-security", "vault", "decision", "git", "presets", "social"] as CardId[]
      ).map((id) => [
        id,
        adminCard(
          id,
          id,
          document.createElementNS("http://www.w3.org/2000/svg", "svg"),
          [],
          "body",
        ),
      ]),
    ) as Record<CardId, HTMLElement>;

  /** The board's columns of card ids (Needs you is no card), for a packed board. */
  const columns = (board: HTMLElement) =>
    [...board.querySelectorAll(":scope > .admin-col")].map((column) =>
      [...column.children].flatMap((card) => ((card as HTMLElement).dataset.card ? [(card as HTMLElement).dataset.card] : [])),
    );
  const order = DOMAINS.flatMap((d) => d.cards as CardId[]);

  it("picks four, three, two or one column by the page's width", () => {
    expect(columnsFor(2265)).toBe(4);
    expect(columnsFor(1720)).toBe(4);
    expect(columnsFor(1719)).toBe(3);
    expect(columnsFor(1440)).toBe(3);
    expect(columnsFor(900)).toBe(2);
    expect(columnsFor(390)).toBe(1);
  });

  it("packs the cards into the board's columns on a wide page and keeps four group headings on a phone", () => {
    const board = document.createElement("div");
    board.className = "admin-board";
    document.body.appendChild(board);
    const all = cards();
    arrange(board, all, 3);
    expect(columns(board)).toEqual(packLayout(3, order));
    expect(board.querySelector(".admin-group-h")).toBeNull();
    // Card headings are still h3s.
    expect(board.querySelectorAll("h3.admin-card-h")).toHaveLength(12);
    arrange(board, all, 2);
    expect(columns(board)).toEqual(packLayout(2, order));
    arrange(board, all, 4);
    expect(columns(board)).toEqual(packLayout(4, order));
    expect(packedArrangement(4, order)).toEqual(packLayout(4, order));
    arrange(board, all, 1);
    expect(board.dataset.packed).toBeUndefined();
    const groups = [...board.querySelectorAll<HTMLElement>(".admin-group")].map((g) => ({
      group: g.dataset.group,
      cards: [...g.querySelectorAll<HTMLElement>(".admin-card")].map((c) => c.dataset.card),
    }));
    expect(groups).toEqual([
      { group: "ship", cards: ["deploys", "git", "social"] },
      { group: "box", cards: ["box", "home-security"] },
      { group: "cloud", cards: ["compute", "credit", "azure", "vault", "decision"] },
      { group: "money", cards: ["accounts", "presets"] },
    ]);
    expect([...board.querySelectorAll(".admin-group-h")].map((h) => h.textContent)).toEqual([
      "Ship",
      "Box",
      "Cloud",
      "Accounts",
    ]);
    expect(board.querySelectorAll("h2.admin-group-h")).toHaveLength(4);
    arrange(board, all, 3);
    // Every card is open on a wide page, and its header is not a toggle.
    expect(all.box.classList.contains("is-folded")).toBe(false);
    expect(all.box.querySelector(".admin-card-toggle")?.hasAttribute("aria-expanded")).toBe(false);
    // No server said anything is missing: no footer line.
    expect(board.querySelector<HTMLElement>(".admin-notset")?.hidden).toBe(true);
    expect(board.lastElementChild?.className).toBe("admin-notset");
  });

  it("drops a card only when every tile behind it reported not set up", () => {
    setTile({ id: "fabric", value: "Not set up", note: "", health: "idle", notSetUp: true });
    expect(cardNotSetUp("compute")).toBe(false);
    setTile({ id: "databricks", value: "Not set up", note: "", health: "idle", notSetUp: true });
    expect(cardNotSetUp("compute")).toBe(true);
    // Databricks set up with no credit amount keeps the credit card.
    expect(creditTile({ configured: true, warehouses: [], spend: null } as unknown as DatabricksOverview).notSetUp).toBeUndefined();
    expect(creditTile({ configured: false, warehouses: [], spend: null } as unknown as DatabricksOverview).notSetUp).toBe(true);
    expect(cardNotSetUp("deploys")).toBe(false);
  });

  it("brings a card back when a later read fails after a not-set-up answer", () => {
    const gone: Tile = { id: "fabric", value: "Not set up", note: "", health: "idle", notSetUp: true };
    expect(staleTile(gone, null).notSetUp).toBe(true);
    expect(staleTile(gone, new Error("down")).notSetUp).toBeUndefined();
    setTile(staleTile(gone, new Error("down")));
    setTile({ id: "databricks", value: "Not set up", note: "", health: "idle", notSetUp: true });
    expect(cardNotSetUp("compute")).toBe(false);
    const off = { configured: false, warehouses: [], spend: null } as unknown as DatabricksOverview;
    expect(creditTile(off).notSetUp).toBe(true);
    expect(creditTile(off, new Error("down")).notSetUp).toBeUndefined();
  });

  it("has every card in exactly one group", () => {
    for (const id of Object.keys(cards()) as CardId[]) {
      expect(DOMAINS.filter((d) => d.cards.includes(id))).toHaveLength(1);
      expect(DOMAINS.find((d) => d.id === groupOf(id))?.cards).toContain(id);
    }
    // And the phone layout holds exactly the group's cards.
    for (const d of DOMAINS) {
      expect(GROUP_LAYOUTS[1][d.id].flat().sort()).toEqual([...d.cards].sort());
    }
  });

  it("folds on a phone: Deploys open, the rest folded unless they need attention", () => {
    const board = document.createElement("div");
    board.className = "admin-board";
    document.body.appendChild(board);
    const all = cards();
    setTile(hostTile(host("connected"), null));
    arrange(board, all, 1);
    expect(all.deploys.classList.contains("is-folded")).toBe(false);
    expect(all.box.classList.contains("is-folded")).toBe(true);
    expect(all.box.querySelector(".admin-card-sum")?.textContent).toBe(
      "Host connected",
    );
    const capacities = [{ name: "a", state: "Active", can_suspend: true }];
    setTile(fabricTile({ configured: true, capacities, can_act: true }, null, NOW));
    setTile(databricksTile(databricks("STOPPED", 12.4), null));
    arrange(board, all, 1);
    // Each part names what it is (2026-10-20 review).
    expect(all.compute.querySelector(".admin-card-sum")?.textContent).toBe(
      "Fabric active \u00b7 warehouse stopped",
    );
    expect(all.box.querySelector<HTMLElement>(".admin-card-body")?.hidden).toBe(
      true,
    );
    // The host drops: the card needs attention and opens.
    setTile(hostTile(host("rejected"), null));
    expect(openByDefault("box")).toBe(true);
    arrange(board, all, 1);
    expect(all.box.classList.contains("is-folded")).toBe(false);
    // A tap folds it, and that is remembered over the default.
    all.box.querySelector<HTMLButtonElement>(".admin-card-toggle")?.click();
    expect(all.box.classList.contains("is-folded")).toBe(true);
    expect(
      all.box
        .querySelector(".admin-card-toggle")
        ?.getAttribute("aria-expanded"),
    ).toBe("false");
    setOpen("admin-compute", true);
    arrange(board, all, 1);
    expect(all.compute.classList.contains("is-folded")).toBe(false);
  });

  it("folds Accounts to its fullest account, and opens it near a limit", () => {
    const board = document.createElement("div");
    board.className = "admin-board";
    document.body.appendChild(board);
    const all = cards();
    setTile(
      accountsTile(
        [account("AE Default", 40, 12), account("Backup", 5, 3)],
        NOW,
      ),
    );
    arrange(board, all, 1);
    expect(all.accounts.classList.contains("is-folded")).toBe(true);
    expect(all.accounts.querySelector(".admin-card-sum")?.textContent).toBe(
      "AE Default 40% 5-hour",
    );
    setTile(accountsTile([account("AE Default", 40, 93)], NOW));
    arrange(board, all, 1);
    expect(all.accounts.classList.contains("is-folded")).toBe(false);
    expect(all.accounts.dataset.health).toBe("problem");
  });
});
