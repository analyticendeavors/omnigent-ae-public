// The Admin Center's page shell (2026-11-14, concept D2; adminShell.ts): the tabs and how they follow
// the WAI-ARIA Tabs pattern, the rule that a server with four cards or fewer has no area tabs, the
// Needs you table moving into the showing tab, the remembered tab, the one-time note for a viewer who
// hid cards, the box admin's page and the Services tab.
import type { ExtensionContext } from "@omnigent/extension-sdk";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createShell, HIDDEN_NOTE, HIDDEN_NOTE_KEY, TAB_KEY, type Shell } from "../src/adminShell";
import { activateAttention, deactivateAttention } from "../src/attention";
import { resetAttention } from "../src/attentionStore";
import { adminCard, CARD_SETTINGS_EVENT, type CardId, type CardSet } from "../src/board";
import { REVEAL_CARD_EVENT } from "../src/cardNav";
import { type CardRow, resetCardState, setCards, setHidden } from "../src/cardState";
import { resetTiles, setTile } from "../src/tiles";

vi.mock("@omnigent/extension-sdk", () => ({ defineExtension: vi.fn() }));

const ALL: CardId[] = ["deploys", "git", "social", "box", "home-security", "compute", "credit", "azure", "vault", "decision", "accounts", "presets"];
const cardsFor = (ids: CardId[]): CardSet =>
  Object.fromEntries(
    ids.map((id) => [id, adminCard(id, `Title ${id}`, document.createElementNS("http://www.w3.org/2000/svg", "svg"), [], "body")]),
  ) as CardSet;

const row = (id: string): CardRow => ({
  id, name: id, group: "ship", description: "d", sources: [], on: true, default_on: true, changed_by: null, changed_at: null, fields: [],
});

function context(saved: Record<string, unknown> = {}) {
  const store = { ...saved };
  const set = vi.fn(async (key: string, value: unknown) => {
    store[key] = value;
  });
  const ctx = {
    view: "fabric",
    server: { request: vi.fn(async () => ({ ok: false, status: 500, body: { error: { code: "x", message: "none" } } })) },
    storage: { user: { get: vi.fn(async (key: string) => store[key]), set } },
    navigation: { openExternal: vi.fn(), openPage: vi.fn(), openSession: vi.fn() },
  } as unknown as ExtensionContext;
  return { ctx, set, store };
}

let shell: Shell | null = null;
async function build(ids: CardId[] = ALL, saved: Record<string, unknown> = {}, mode: "full" | "box" = "full") {
  const { ctx, set, store } = context(saved);
  shell = createShell({ context: ctx, mode, cards: cardsFor(ids), settleMs: 0 });
  document.getElementById("root")!.replaceChildren(shell.main);
  await shell.loadPrefs();
  shell.layout();
  return { shell, set, store, ctx };
}
const tab = (id: string) => document.querySelector<HTMLElement>(`#as-tab-${id}`)!;
const panel = (id: string) => document.querySelector<HTMLElement>(`#as-panel-${id}`)!;
const tabIds = () => [...document.querySelectorAll<HTMLElement>('[role="tab"]')].map((t) => t.id.replace("as-tab-", ""));
const selected = () => [...document.querySelectorAll<HTMLElement>('[role="tab"]')].filter((t) => t.getAttribute("aria-selected") === "true").map((t) => t.id.replace("as-tab-", ""));
const cardsIn = (node: Element) => [...node.querySelectorAll<HTMLElement>(".admin-card")].map((c) => c.dataset.card);

beforeEach(() => {
  resetTiles();
  resetAttention();
  resetCardState();
  document.body.innerHTML = '<div id="root"></div>';
  delete (window as { matchMedia?: unknown }).matchMedia;
});
afterEach(() => {
  shell?.stop();
  shell = null;
  deactivateAttention();
  resetCardState();
});

describe("the tabs follow the WAI-ARIA Tabs pattern", () => {
  it("has a tablist, tabs that name their panels, and panels that name their tabs", async () => {
    await build();
    const list = document.querySelector('[role="tablist"]')!;
    expect(list.getAttribute("aria-label")).toBeTruthy();
    expect(tabIds()).toEqual(["overview", "ship", "box", "cloud", "money", "services"]);
    for (const id of tabIds()) {
      expect(tab(id).getAttribute("aria-controls")).toBe(`as-panel-${id}`);
      expect(panel(id).getAttribute("role")).toBe("tabpanel");
      expect(panel(id).getAttribute("aria-labelledby")).toBe(`as-tab-${id}`);
    }
  });

  it("selects one tab, shows only its panel, and gives only that tab a tab stop", async () => {
    await build();
    expect(selected()).toEqual(["overview"]);
    expect(tabIds().map((id) => tab(id).tabIndex)).toEqual([0, -1, -1, -1, -1, -1]);
    expect(tabIds().map((id) => panel(id).hidden)).toEqual([false, true, true, true, true, true]);
    tab("cloud").click();
    expect(selected()).toEqual(["cloud"]);
    expect(tab("cloud").tabIndex).toBe(0);
    expect(tab("overview").tabIndex).toBe(-1);
    expect(panel("cloud").hidden).toBe(false);
    expect(panel("overview").hidden).toBe(true);
  });

  it("moves with the arrow keys, wraps, and goes to the ends with Home and End", async () => {
    await build();
    const press = (key: string) => {
      (document.activeElement as HTMLElement | null ?? tab("overview")).dispatchEvent(new KeyboardEvent("keydown", { key, bubbles: true }));
    };
    tab("overview").focus();
    press("ArrowRight");
    expect(selected()).toEqual(["ship"]);
    expect(document.activeElement).toBe(tab("ship"));
    press("End");
    expect(selected()).toEqual(["services"]);
    press("ArrowRight");
    expect(selected()).toEqual(["overview"]);
    press("ArrowLeft");
    expect(selected()).toEqual(["services"]);
    press("Home");
    expect(selected()).toEqual(["overview"]);
  });
});

describe("the tab rule", () => {
  it("gives a server with four connected cards or fewer no area tabs, and puts the cards under the table", async () => {
    await build(["deploys", "box", "compute", "accounts"]);
    expect(tabIds()).toEqual(["overview", "services"]);
    expect(cardsIn(panel("overview"))).toEqual(["deploys", "box", "compute", "accounts"]);
    expect(panel("overview").querySelector("#needs-you")).not.toBeNull();
  });

  it("gives five cards a tab per area that has one, with that area's cards in its panel", async () => {
    await build(["deploys", "git", "box", "compute", "vault"]);
    expect(tabIds()).toEqual(["overview", "ship", "box", "cloud", "services"]);
    expect(cardsIn(panel("ship"))).toEqual(["deploys", "git"]);
    expect(cardsIn(panel("cloud"))).toEqual(["compute", "vault"]);
    // The Overview holds the table and the rail only.
    expect(cardsIn(panel("overview"))).toEqual([]);
  });

  it("follows the cards as they are turned off, hidden or not set up", async () => {
    await build(["deploys", "git", "box", "compute", "vault", "accounts"]);
    setCards({ revision: 1, state_ok: true, cards: ["deploys", "git", "box", "compute", "vault", "accounts"].map(row) });
    expect(tabIds()).toContain("cloud");
    setHidden(["vault", "compute"]);
    expect(tabIds()).toEqual(["overview", "services"]);
    expect(cardsIn(panel("overview"))).toEqual(["deploys", "git", "box", "accounts"]);
    setHidden([]);
    expect(tabIds()).toEqual(["overview", "ship", "box", "cloud", "money", "services"]);
    setTile({ id: "vault", value: "Not set up", note: "", health: "idle", notSetUp: true });
    expect(cardsIn(panel("cloud"))).toEqual(["compute"]);
  });
});

describe("the Needs you table", () => {
  it("sits in the showing tab and limits itself to that tab's area", async () => {
    await build();
    expect(panel("overview").contains(shell!.attention)).toBe(true);
    tab("ship").click();
    expect(panel("ship").contains(shell!.attention)).toBe(true);
    expect(panel("overview").contains(shell!.attention)).toBe(false);
    tab("box").click();
    expect(panel("box").contains(shell!.attention)).toBe(true);
    tab("overview").click();
    expect(panel("overview").contains(shell!.attention)).toBe(true);
  });

  it("starts every tab from the whole list: a source filter does not follow to another tab", async () => {
    const store = await import("../src/attentionStore");
    await build();
    store.setFilter("git");
    tab("cloud").click();
    expect(store.getFilter()).toBeNull();
  });
});

describe("the remembered tab", () => {
  it("saves the tab a viewer opens, and opens it again next time", async () => {
    const { set } = await build();
    tab("box").click();
    expect(set).toHaveBeenCalledWith(TAB_KEY, "box");
    shell!.stop();
    document.getElementById("root")!.replaceChildren();
    await build(ALL, { [TAB_KEY]: "cloud" });
    expect(selected()).toEqual(["cloud"]);
  });

  it("falls back to the Overview for a tab this page does not have", async () => {
    await build(["deploys", "box"], { [TAB_KEY]: "cloud" });
    expect(selected()).toEqual(["overview"]);
  });

  it("does not overwrite the saved tab before the viewer picks one", async () => {
    const { set } = await build(ALL, { [TAB_KEY]: "box" });
    expect(set).not.toHaveBeenCalled();
  });
});

describe("hidden cards and the one-time note", () => {
  const note = () => document.querySelector<HTMLElement>(".as-note")!;

  it("shows a viewer who has hidden cards a note that hiding now hides only the card", async () => {
    await build();
    expect(note().hidden).toBe(true);
    setHidden(["vault"]);
    expect(note().hidden).toBe(false);
    expect(HIDDEN_NOTE).toBe("Hidden cards now hide only the card; their items stay in Needs you.");
    expect(note().textContent).toContain(HIDDEN_NOTE);
  });

  it("remembers once it was seen, and does not show it again", async () => {
    const { set } = await build();
    setHidden(["vault"]);
    note().querySelector<HTMLButtonElement>("button")!.click();
    expect(set).toHaveBeenCalledWith(HIDDEN_NOTE_KEY, true);
    expect(note().hidden).toBe(true);
    shell!.stop();
    document.getElementById("root")!.replaceChildren();
    await build(ALL, { [HIDDEN_NOTE_KEY]: true });
    setHidden(["git"]);
    expect(note().hidden).toBe(true);
  });
});

describe("Services and card navigation", () => {
  it("opens the Services tab at a card from that card's gear", async () => {
    const { ctx } = await build();
    (ctx.server.request as ReturnType<typeof vi.fn>).mockResolvedValue({ ok: true, status: 200, body: { revision: 1, state_ok: true, cards: ALL.map(row) } });
    document.querySelector<HTMLElement>('[data-card="vault"] .admin-gear')!.dispatchEvent(new Event("click", { bubbles: true }));
    expect(selected()).toEqual(["services"]);
    expect(panel("services").querySelector(".ae-cards-page")).not.toBeNull();
    expect(document.querySelector(".ae-cards-scrim")).toBeNull();
    await new Promise((resolve) => setTimeout(resolve, 10));
    expect(panel("services").querySelector('[data-card="vault"] .ae-card-setup')?.hasAttribute("hidden")).toBe(false);
  });

  it("shows the tab that holds a card when something asks to go to it", async () => {
    await build();
    document.querySelector('[data-card="vault"]')!.dispatchEvent(new CustomEvent(REVEAL_CARD_EVENT, { bubbles: true, detail: "vault" }));
    expect(selected()).toEqual(["cloud"]);
  });

  it("raises the settings event from a gear with the card id", async () => {
    await build();
    const heard = vi.fn();
    shell!.main.addEventListener(CARD_SETTINGS_EVENT, (event) => heard((event as CustomEvent).detail));
    document.querySelector<HTMLButtonElement>('[data-card="git"] .admin-gear')!.click();
    expect(heard).toHaveBeenCalledWith("git");
  });
});

describe("the box admin's page", () => {
  it("has no tabs: the table, and the Box health card beside it", async () => {
    await build(["box"], {}, "box");
    expect(document.querySelector<HTMLElement>('[role="tablist"]')!.hidden).toBe(true);
    expect(document.querySelector('[role="tab"]')).toBeNull();
    expect(document.querySelector(".as-ov-box #needs-you")).not.toBeNull();
    expect(cardsIn(document.querySelector(".as-ov-box")!)).toEqual(["box"]);
    expect(document.querySelector(".as-rail")).toBeNull();
    expect(document.querySelector<HTMLElement>(".admin-board")!.hasAttribute("data-single")).toBe(true);
  });
});

describe("the header", () => {
  it("holds the title, the verdict, the time and one refresh button with a name and a title", async () => {
    await build();
    const header = document.querySelector("header.fabric-top")!;
    expect(header.querySelector("h1")!.textContent).toBe("Admin Center");
    expect(header.querySelector(".admin-verdict")).not.toBeNull();
    const refresh = header.querySelector<HTMLButtonElement>(".admin-refresh")!;
    expect(refresh.getAttribute("aria-label")).toBe("Read everything again");
    expect(refresh.title).toBe("Read everything again");
    expect(document.querySelector(".admin-tiles")).toBeNull();
    expect(document.querySelector(".pill")).toBeNull();
  });
});

// ── 2026-11-15 review round ──────────────────────────────────────────────────

import { setAttention } from "../src/attentionStore";
import { overview, item as attnItem, many } from "./attentionFixtures";

describe("the Services panel across a rebuild of the tabs", () => {
  it("is the same node, not closed, with its typed value and open setup fold kept", async () => {
    const { ctx } = await build(["deploys", "git", "box", "compute", "vault", "accounts"]);
    (ctx.server.request as ReturnType<typeof vi.fn>).mockResolvedValue({
      ok: true,
      status: 200,
      body: { revision: 1, state_ok: true, cards: ["deploys", "git", "box", "compute", "vault", "accounts"].map((id) => ({
        ...row(id),
        fields: id === "vault" ? [{ label: "Vault name", env: "AE_VAULT_NAME", kind: "text", hint: "h", env_only: false, status: "missing", editable: true }] : [],
      })) },
    });
    tab("services").click();
    await new Promise((resolve) => setTimeout(resolve, 10));
    const before = panel("services");
    const input = before.querySelector<HTMLInputElement>('[data-card="vault"] input.ae-card-input');
    // Open the vault's Setup, and type into its field.
    before.querySelector<HTMLElement>('[data-card="vault"] .ae-card-row-head')!.click();
    const typed = before.querySelector<HTMLInputElement>('[data-card="vault"] input.ae-card-input')!;
    typed.value = "kv-test";
    typed.dispatchEvent(new Event("input", { bubbles: true }));
    expect(input).not.toBeNull();
    typed.focus();
    // The tab list changes (two cards go): the tabs are drawn again.
    setHidden(["vault", "compute"]);
    expect(tabIds()).toEqual(["overview", "services"]);
    expect(panel("services")).toBe(before);
    expect(before.isConnected).toBe(true);
    expect(before.querySelector<HTMLInputElement>('[data-card="vault"] input.ae-card-input')!.value).toBe("kv-test");
    expect(before.querySelector<HTMLElement>('[data-card="vault"] .ae-card-setup')!.hidden).toBe(false);
    expect(selected()).toEqual(["services"]);
  });
});

describe("the first layout waits for the first tile reads", () => {
  it("shows only the table until the cards have reported, then draws the tabs once", async () => {
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
    try {
      const { ctx } = context();
      shell = createShell({ context: ctx, mode: "full", cards: cardsFor(["deploys", "git", "box", "compute", "vault"]), settleMs: 4000 });
      document.getElementById("root")!.replaceChildren(shell.main);
      await shell.loadPrefs();
      shell.layout();
      expect(document.querySelector<HTMLElement>('[role="tablist"]')!.hidden).toBe(true);
      expect(document.querySelector('[role="tab"]')).toBeNull();
      expect(shell.main.querySelector("#needs-you")).not.toBeNull();
      // Two of five cards say they are not set up: the line (four cards) is crossed before any tab shows.
      setTile({ id: "hold", value: "Off", note: "", health: "ok" });
      setTile({ id: "git", value: "x", note: "", health: "ok" });
      setTile({ id: "boxhealth", value: "x", note: "", health: "ok" });
      setTile({ id: "fabric", value: "x", note: "", health: "idle", notSetUp: true });
      setTile({ id: "databricks", value: "x", note: "", health: "idle", notSetUp: true });
      expect(document.querySelector('[role="tab"]')).toBeNull();
      setTile({ id: "vault", value: "Not set up", note: "", health: "idle", notSetUp: true });
      expect(tabIds()).toEqual(["overview", "services"]);
      expect(document.querySelector<HTMLElement>('[role="tablist"]')!.hidden).toBe(false);
    } finally {
      vi.useRealTimers();
    }
  });

  it("stops waiting after the cap, so a card that never reports cannot hold the page", async () => {
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
    try {
      const { ctx } = context();
      shell = createShell({ context: ctx, mode: "full", cards: cardsFor(ALL), settleMs: 4000 });
      document.getElementById("root")!.replaceChildren(shell.main);
      await shell.loadPrefs();
      shell.layout();
      expect(document.querySelector('[role="tab"]')).toBeNull();
      vi.advanceTimersByTime(4000);
      expect(tabIds()).toEqual(["overview", "ship", "box", "cloud", "money", "services"]);
    } finally {
      vi.useRealTimers();
    }
  });
});

describe("a quiet day", () => {
  it("marks the Overview quiet only when nothing needs you and every source was read, and keeps the line on top", async () => {
    await build();
    const ov = () => document.querySelector<HTMLElement>("#as-panel-overview .as-ov")!;
    expect(ov().classList.contains("is-quiet")).toBe(false);
    setAttention(overview([]));
    expect(ov().classList.contains("is-quiet")).toBe(true);
    expect(ov().querySelector(".as-tiles")).not.toBeNull();
    expect(ov().querySelector("#needs-you")).not.toBeNull();
    // An unread source is not a quiet day.
    setAttention(overview([], { unknown: ["Git repos could not be read"] }));
    expect(ov().classList.contains("is-quiet")).toBe(false);
    setAttention(overview(many(1, "pr-failing")));
    expect(ov().classList.contains("is-quiet")).toBe(false);
    void attnItem;
  });
});

describe("the area links in the table", () => {
  async function linksFor(ids: CardId[]): Promise<number> {
    document.getElementById("root")!.replaceChildren();
    resetTiles();
    const { ctx } = await build(ids);
    (ctx.server.request as ReturnType<typeof vi.fn>).mockResolvedValue({ ok: true, status: 200, body: overview(many(2, "pr-failing")) });
    activateAttention(ctx, shell!.attention, { waiting: false, table: shell!.table.hooks });
    await new Promise((resolve) => setTimeout(resolve, 10));
    return document.querySelectorAll(".nt-go").length;
  }

  it("draw only for an area whose tab exists", async () => {
    // Four connected cards: no area tabs, so no area link either.
    expect(await linksFor(["deploys", "box", "compute", "accounts"])).toBe(0);
    shell!.stop();
    deactivateAttention();
    resetAttention();
    expect(await linksFor(ALL)).toBe(1);
  });
});
