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
  groupOf,
  openByDefault,
} from "../src/board";
import { mountRail } from "../src/rail";
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
  mountUpdated,
  pillStates,
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

describe("the tile store, the header time and the rail", () => {
  const tile = (id: TileId, health: Health, value: string = id, note = "", extra: Partial<Tile> = {}): Tile => ({
    id,
    value,
    note,
    health,
    ...extra,
  });
  const rail = () => {
    const host = document.createElement("div");
    document.body.append(host);
    const stop = mountRail(host, { openTab: () => undefined, tabs: () => true });
    return { host, stop };
  };
  const block = (host: HTMLElement, id: string) => host.querySelector<HTMLElement>(`.rail-block[data-area="${id}"]`);

  it("keeps the newest answer's time for the header, and the credit tile for its card's fold", () => {
    const time = document.createElement("p");
    mountUpdated(time);
    setTile(secretsTile(secrets(["A"]), null), NOW);
    setTile(releaseTile(release(), null), NOW - 60_000);
    // The credit tile belongs to no area; it carries a card's fold.
    setTile(creditTile(databricks("STOPPED", 12.4)));
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

  it("leaves out an area whose members are all missing or idle, and takes the worst health of the rest", () => {
    setTile(tile("accounts", "idle"));
    setTile(tile("presets", "idle"));
    expect(pillStates()).toEqual([]);
    setTile(tile("family", "ok", "Fine"));
    expect(pillStates().map((state) => state.domain.id)).toEqual(["money"]);
    setTile(tile("fabric", "ok"));
    setTile(tile("databricks", "attention", "Down", "check"));
    expect(pillStates().find((state) => state.domain.id === "cloud")!.health).toBe("attention");
    setTile(tile("vault", "problem", "Bad", "broken"));
    expect(pillStates().find((state) => state.domain.id === "cloud")!.health).toBe("problem");
  });

  it("draws a rail block per area with a card, each member as a dot, its name and its short value", () => {
    const { host, stop } = rail();
    setTile(tile("release", "ok", "v1", "up to date", { summary: "Release v1" }));
    setTile(tile("hold", "attention", "2 h", "held by someone", { brief: "Held 2 h", bar: 40 }));
    const ship = block(host, "ship")!;
    expect([...ship.querySelectorAll(".rail-mname")].map((n) => n.textContent)).toEqual(["Release", "Deploy hold"]);
    // The short value is the brief, else the summary, else the value: never the note.
    expect([...ship.querySelectorAll(".rail-mval")].map((n) => n.textContent)).toEqual(["Release v1", "Held 2 h"]);
    expect(ship.textContent).not.toContain("held by someone");
    expect(ship.querySelector(".tile-attention .rail-dot")).not.toBeNull();
    // The Deploy hold's bar is the Ship block's one meter.
    expect(ship.querySelector<HTMLElement>(".rail-bar > i")!.style.width).toBe("40%");
    expect(block(host, "cloud")).toBeNull();
    stop();
  });

  it("leaves a not-set-up member out of the rail, and the block out when every member is out", () => {
    const { host, stop } = rail();
    setTile(tile("fabric", "ok", "Active"));
    setTile({ ...tile("databricks", "idle", "Not set up"), notSetUp: true });
    setTile({ ...tile("vault", "idle", "Not set up"), notSetUp: true });
    expect([...block(host, "cloud")!.querySelectorAll(".rail-mname")].map((n) => n.textContent)).toEqual(["Fabric"]);
    setTile({ ...tile("fabric", "idle", "Not set up"), notSetUp: true });
    expect(block(host, "cloud")).toBeNull();
    stop();
  });

  it("keeps focus on a block's header across a tile change", () => {
    const { host, stop } = rail();
    setTile(tile("host", "ok", "Connected"));
    block(host, "box")!.querySelector<HTMLElement>(".rail-h")!.focus();
    setTile(tile("host", "attention", "Down", "check"));
    expect(document.activeElement).toBe(block(host, "box")!.querySelector(".rail-h"));
    stop();
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

  /** The card ids on the board, in order. */
  const shownIds = (board: HTMLElement) =>
    [...board.querySelectorAll<HTMLElement>(".admin-card")].map((card) => card.dataset.card);
  const order = DOMAINS.flatMap((d) => d.cards as CardId[]);

  it("picks four, three, two or one column by the page's width", () => {
    expect(columnsFor(2265)).toBe(4);
    expect(columnsFor(1720)).toBe(4);
    expect(columnsFor(1719)).toBe(3);
    expect(columnsFor(1440)).toBe(3);
    expect(columnsFor(900)).toBe(2);
    expect(columnsFor(390)).toBe(1);
  });

  it("puts every card in the group order in one grid, with no group headings, the drag grip or the packed board", () => {
    const board = document.createElement("div");
    board.className = "admin-board";
    document.body.appendChild(board);
    const all = cards();
    arrange(board, all, 3);
    expect(shownIds(board)).toEqual(order);
    expect(board.querySelector(".admin-group-h")).toBeNull();
    expect(board.querySelector(".admin-grip")).toBeNull();
    expect(board.dataset.packed).toBeUndefined();
    // Card headings are still h3s.
    expect(board.querySelectorAll("h3.admin-card-h")).toHaveLength(12);
    // The width only tells a phone from the rest: the same order at any column count.
    arrange(board, all, 2);
    expect(shownIds(board)).toEqual(order);
    arrange(board, all, 1);
    expect(shownIds(board)).toEqual(order);
    arrange(board, all, 3);
    // Every card is open on a wide page, and its header is not a toggle.
    expect(all.box.classList.contains("is-folded")).toBe(false);
    expect(all.box.querySelector(".admin-card-toggle")?.hasAttribute("aria-expanded")).toBe(false);
    // No server said anything is missing: no footer line.
    expect(board.querySelector<HTMLElement>(".admin-notset")?.hidden).toBe(true);
    expect(board.lastElementChild?.className).toBe("admin-notset");
  });

  it("deals an area tab's cards into 620 px lanes in turn, kept in page order for the keyboard, one lane on a phone", () => {
    const board = document.createElement("div");
    board.className = "admin-board as-board";
    document.body.appendChild(board);
    const all = cards();
    const lanesOf = () =>
      [...board.querySelectorAll<HTMLElement>(":scope > .admin-col > .admin-card")].map((card) =>
        card.style.getPropertyValue("--lane"),
      );
    // 1,300 px holds two lanes: the first card left, the second right, the third left again.
    arrange(board, all, 3, true, undefined, 1300);
    expect(board.dataset.lanes).toBe("2");
    // One column element in page order, so Tab and a screen reader go left to right.
    expect(board.querySelectorAll(":scope > .admin-col")).toHaveLength(1);
    expect(shownIds(board)).toEqual(order);
    expect(board.querySelector<HTMLElement>(".admin-col")!.style.getPropertyValue("--lanes")).toBe("2");
    expect(lanesOf()).toEqual(order.map((_, i) => String(i % 2)));
    // A wider page adds a lane without a forced rebuild.
    arrange(board, all, 4, false, undefined, 1920);
    expect(board.dataset.lanes).toBe("3");
    expect(lanesOf()).toEqual(order.map((_, i) => String(i % 3)));
    // A phone keeps one plain column in page order, with no lane left on any card.
    arrange(board, all, 1, false, undefined, 390);
    expect(board.dataset.lanes).toBe("1");
    expect(shownIds(board)).toEqual(order);
    expect(lanesOf().every((lane) => lane === "")).toBe(true);
    expect(all.box.style.top).toBe("");
    // A board that is not an area tab's keeps one column at any width.
    const plain = document.createElement("div");
    plain.className = "admin-board";
    document.body.appendChild(plain);
    arrange(plain, cards(), 3, true, undefined, 1920);
    expect(plain.dataset.lanes).toBe("1");
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
