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
  columnsFor,
  LAYOUTS,
  openByDefault,
} from "../src/board";
import { creditHealth, creditTile, databricksTile } from "../src/databricks";
import { holdTile, releaseTile } from "../src/deploys";
import { fabricTile } from "../src/fabric";
import type { Release } from "../src/header";
import { hostTile } from "../src/host";
import { secretsTile } from "../src/secrets";
import { resetSectionState, setOpen } from "../src/sections";
import {
  getTile,
  mountTiles,
  refreshAll,
  registerRefresh,
  resetTiles,
  setTile,
  shortLeft,
  staleTile,
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
  set_by: "reid",
  remaining_s: 110 * 60,
  choices: [1, 2, 3],
  error: null,
  ...over,
});

const host = (state: HostStatus["state"]): HostStatus =>
  ({
    host: { id: "h", name: "ae-llm-box", status: "online" },
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
    creditUsd: 200,
    cadence: "monthly",
    creditStart: "2026-08-19",
    creditEnd: "2027-08-19",
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
      note: "$12.40 of $200 used",
      health: "idle",
    });
    expect(creditTile(databricks("STOPPED", 12.4)).value).toBe("$187.60 left");
    expect(creditHealth(databricks("RUNNING", 160).spend)).toBe("attention");
    expect(creditHealth(databricks("RUNNING", 210).spend)).toBe("problem");
    expect(databricksTile(databricks("STOPPED", 210), null).health).toBe(
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

describe("the strip and the header", () => {
  it("draws the tiles in order with their health, and the newest time", () => {
    const strip = document.createElement("div");
    const time = document.createElement("p");
    mountTiles(strip, time);
    setTile(secretsTile(secrets(["A"]), null), NOW);
    setTile(releaseTile(release(), null), NOW - 60_000);
    // The credit tile carries a card's fold, not a tile of the strip.
    setTile(creditTile(databricks("STOPPED", 12.4)));
    const tiles = [...strip.querySelectorAll<HTMLElement>(".tile")];
    expect(tiles.map((node) => node.dataset.tile)).toEqual([
      "release",
      "secrets",
    ]);
    expect(tiles[1].classList.contains("tile-problem")).toBe(true);
    expect(
      tiles[1].querySelector(".tile-label svg")?.getAttribute("aria-hidden"),
    ).toBe("true");
    expect(time.textContent).toMatch(/^Updated /);
    expect(getTile("credit")?.value).toBe("$187.60 left");
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
        ["deploys", "accounts", "compute", "credit", "azure", "box"] as CardId[]
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

  const columns = (board: HTMLElement) =>
    [...board.children].map((column) =>
      [...column.children].map((card) => (card as HTMLElement).dataset.card),
    );

  it("picks three, two or one column by the page's width", () => {
    expect(columnsFor(1440)).toBe(3);
    expect(columnsFor(900)).toBe(2);
    expect(columnsFor(390)).toBe(1);
  });

  it("stacks each column's cards without a shared grid", () => {
    const board = document.createElement("div");
    board.className = "admin-board";
    document.body.appendChild(board);
    const all = cards();
    arrange(board, all, 3);
    expect(columns(board)).toEqual(LAYOUTS[3]);
    // Box health sits under Deploys and Databricks credit under Azure spend (2026-10-22).
    expect(columns(board)).toEqual([
      ["deploys", "box"],
      ["accounts", "compute"],
      ["azure", "credit"],
    ]);
    arrange(board, all, 2);
    expect(columns(board)).toEqual([
      ["deploys", "box", "compute"],
      ["accounts", "azure", "credit"],
    ]);
    arrange(board, all, 1);
    expect(columns(board)).toEqual([
      ["deploys", "accounts", "compute", "credit", "azure", "box"],
    ]);
    arrange(board, all, 3);
    // Every card is open on a wide page, and its header is not a toggle.
    expect(all.box.classList.contains("is-folded")).toBe(false);
    expect(
      all.box
        .querySelector(".admin-card-toggle")
        ?.hasAttribute("aria-expanded"),
    ).toBe(false);
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
