// The Overview wall (2026-11-17, option 3 "list and wall"; wall.ts, adminShell.ts, activityPanel.ts):
// the span rule, the order, what leaves the wall, the pills, the cards moving between the Overview and
// their tabs, the three width bands, the card title as a control, and the Done list. The compact style
// is CSS; the last block pins the card parts that CSS names, so a card whose markup changes cannot
// quietly show an extra part on the wall.
import type { ExtensionContext } from "@omnigent/extension-sdk";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { bandFor, BAND_HYSTERESIS, createShell, type Shell, STACK_PX, WIDE_PX } from "../src/adminShell";
import { loadActivity, resetActivity } from "../src/activityStore";
import { mountDoneList } from "../src/activityPanel";
import { deactivateAttention, markNewUrgent, resetAttentionSeen } from "../src/attention";
import { resetAttention, setAttention } from "../src/attentionStore";
import { adminCard, type CardId, type CardSet, WALL_OPEN_EVENT } from "../src/board";
import { type CardRow, resetCardState, setCards, setHidden } from "../src/cardState";
import { cardCounts, sourceCounts } from "../src/needsModel";
import { resetTiles, setTile } from "../src/tiles";
import { mountWall, spansFor } from "../src/wall";
import { gitTile } from "../src/git";
import { calm } from "./gitFixtures";
import css from "../src/style.css?raw";
import { item, overview } from "./attentionFixtures";

vi.mock("@omnigent/extension-sdk", () => ({ defineExtension: vi.fn() }));
// The Box heat line reads the box monitor's last answer; a test sets the readings here.
const heat = vi.hoisted(() => ({ history: null as Array<number | null> | null }));
vi.mock("../src/boxHealth", async (original) => ({
  ...(await original<typeof import("../src/boxHealth")>()),
  lastCpuHistory: () => heat.history,
}));

const ALL: CardId[] = ["deploys", "git", "social", "box", "home-security", "compute", "credit", "azure", "vault", "decision", "accounts", "presets"];
const SVG = () => document.createElementNS("http://www.w3.org/2000/svg", "svg");
const cardsFor = (ids: CardId[]): CardSet =>
  Object.fromEntries(ids.map((id) => [id, adminCard(id, `Title ${id}`, SVG(), [], "body")])) as CardSet;

const row = (id: string, sources: string[] = [], on = true): CardRow => ({
  id, name: id, group: "ship", description: "d", sources, on, default_on: true, changed_by: null, changed_at: null, fields: [],
});

function context() {
  const store: Record<string, unknown> = {};
  return {
    view: "fabric",
    server: { request: vi.fn(async () => ({ ok: false, status: 500, body: { error: { code: "x", message: "none" } } })) },
    storage: { user: { get: vi.fn(async (key: string) => store[key]), set: vi.fn(async (key: string, value: unknown) => void (store[key] = value)) } },
    navigation: { openExternal: vi.fn(), openPage: vi.fn(), openSession: vi.fn() },
  } as unknown as ExtensionContext;
}

let shell: Shell | null = null;
async function build(ids: CardId[] = ALL, width = 0) {
  shell = createShell({ context: context(), mode: "full", cards: cardsFor(ids), settleMs: 0 });
  document.getElementById("root")!.replaceChildren(shell.main);
  await shell.loadPrefs();
  if (width) resize(width);
  shell.layout();
  return shell;
}
/** The admin container's width: jsdom has no layout, so the page reads this and a window resize tells it. */
function resize(width: number) {
  Object.defineProperty(shell?.main ?? document.body, "clientWidth", { configurable: true, get: () => width });
  window.dispatchEvent(new Event("resize"));
}
const q = <T extends HTMLElement = HTMLElement>(sel: string) => document.querySelector<T>(sel);
const qa = (sel: string) => [...document.querySelectorAll<HTMLElement>(sel)];
const ov = () => q(".as-ov[data-wall]")!;
const onWall = () => qa(".as-wall .admin-card").map((card) => card.dataset.card);
const onBoard = (area: string) => qa(`#as-panel-${area} .admin-board .admin-card`).map((card) => card.dataset.card);

beforeEach(() => {
  resetTiles();
  resetAttention();
  resetCardState();
  resetActivity();
  document.body.innerHTML = '<div id="root"></div>';
  delete (window as { matchMedia?: unknown }).matchMedia;
});
afterEach(() => {
  shell?.stop();
  shell = null;
  deactivateAttention();
  resetCardState();
  resetActivity();
});

describe("the span rule", () => {
  it("gives one card 6, two cards 3 and 3, three cards 2 each", () => {
    expect(spansFor(["a"])).toEqual([6]);
    expect(spansFor(["a", "b"])).toEqual([3, 3]);
    expect(spansFor(["a", "b", "c"])).toEqual([2, 2, 2]);
  });
  it("gives four cards 3 and 3 on two rows, five 2, 2, 2 then 3, 3, six or more rows of three", () => {
    expect(spansFor(["a", "b", "c", "d"])).toEqual([3, 3, 3, 3]);
    expect(spansFor(["a", "b", "c", "d", "e"])).toEqual([2, 2, 2, 3, 3]);
    expect(spansFor(["a", "b", "c", "d", "e", "f"])).toEqual([2, 2, 2, 2, 2, 2]);
    expect(spansFor(["a", "b", "c", "d", "e", "f", "g"])).toEqual([2, 2, 2, 2, 2, 2, 6]);
    expect(spansFor(["a", "b", "c", "d", "e", "f", "g", "h"])).toEqual([2, 2, 2, 2, 2, 2, 3, 3]);
  });
  it("lets Box health take 4 beside one other card, and nothing special otherwise", () => {
    expect(spansFor(["box", "home-security"])).toEqual([4, 2]);
    expect(spansFor(["home-security", "box"])).toEqual([2, 4]);
    expect(spansFor(["box"])).toEqual([6]);
    expect(spansFor(["box", "b", "c"])).toEqual([2, 2, 2]);
  });
});

describe("what the wall holds", () => {
  it("draws areas and cards in DOMAINS order, each tile with its span", async () => {
    await build(ALL, 1500);
    expect(qa(".wall-area").map((node) => node.dataset.area)).toEqual(["ship", "box", "cloud", "money"]);
    expect(onWall()).toEqual(["deploys", "git", "social", "box", "home-security", "compute", "credit", "azure", "vault", "decision", "accounts", "presets"]);
    const spans = (area: string) => qa(`.wall-area[data-area="${area}"] .wall-tile`).map((tile) => tile.dataset.span);
    expect(spans("ship")).toEqual(["2", "2", "2"]);
    expect(spans("box")).toEqual(["4", "2"]);
    expect(spans("cloud")).toEqual(["2", "2", "2", "3", "3"]);
    expect(spans("money")).toEqual(["3", "3"]);
    expect(qa(".wall-area-name").map((node) => node.textContent)).toEqual(["Ship", "Box", "Cloud", "Accounts"]);
  });

  it("leaves out a card that is hidden for this viewer or turned off, and an area left with none loses its label", async () => {
    setCards({ revision: 1, state_ok: true, cards: ALL.map((id) => row(id, [], id !== "vault")) });
    setHidden(["box", "home-security"]);
    await build(ALL, 1500);
    expect(onWall()).not.toContain("vault");
    expect(onWall()).not.toContain("box");
    expect(onWall()).not.toContain("home-security");
    expect(qa(".wall-area").map((node) => node.dataset.area)).toEqual(["ship", "cloud", "money"]);
    // Four cards left in Cloud: 3 and 3 on two rows.
    expect(qa('.wall-area[data-area="cloud"] .wall-tile').map((tile) => tile.dataset.span)).toEqual(["3", "3", "3", "3"]);
  });

  it("leaves out a card its integration says is not set up, and brings it back when it is", async () => {
    await build(ALL, 1500);
    expect(onWall()).toContain("azure");
    setTile({ id: "azure", health: "idle", value: "x", note: "", notSetUp: true });
    expect(onWall()).not.toContain("azure");
    expect(qa('.wall-area[data-area="cloud"] .wall-tile').map((tile) => tile.dataset.span)).toEqual(["3", "3", "3", "3"]);
    setTile({ id: "azure", health: "ok", value: "x", note: "" });
    expect(onWall()).toContain("azure");
  });

  it("does not move a card that is already in its place when something redraws", async () => {
    await build(ALL, 1500);
    const git = q('.admin-card[data-card="git"]')!;
    const tile = git.parentElement!;
    setTile({ id: "git", health: "attention", value: "x", note: "" });
    setAttention(overview([item("a")]));
    await Promise.resolve();
    expect(git.parentElement).toBe(tile);
    expect(qa(".wall-tile").length).toBe(12);
  });
});

describe("the pills in a card's header", () => {
  const withCard = () => setCards({ revision: 1, state_ok: true, cards: ALL.map((id) => row(id, id === "git" ? ["git"] : id === "social" ? ["social"] : [])) });
  const tile = (card: string) => q(`.wall-tile[data-card="${card}"]`)!;
  const pillText = (card: string) => [...tile(card).querySelectorAll(".wall-pills .nt-pill")].map((n) => n.textContent);

  it("always say their words ('3 urgent', '1 to review'), also with several pills (2026-11-18: the owner picked words over bare numbers)", async () => {
    withCard();
    await build(ALL, 1500);
    setAttention(overview([item("a"), item("b"), item("c")]));
    expect(pillText("git")).toEqual(["3 urgent"]);
    setAttention(overview([item("a"), item("b"), item("c"), item("r", { tone: "review", kind: "pr-ready" })]));
    expect(pillText("git")).toEqual(["3 urgent", "1 to review"]);
    const pills = tile("git").querySelector(".wall-pills")!;
    expect(pills.getAttribute("aria-label")).toBe("3 urgent, 1 to review");
    expect(pills.getAttribute("title")).toBe("3 urgent, 1 to review");
  });

  it("colour the tile's left edge red for an urgent item, amber for review only, none otherwise", async () => {
    withCard();
    await build(ALL, 1500);
    setAttention(overview([item("a"), item("s", { source: "social", tone: "review", kind: "post-late" })]));
    expect(tile("git").dataset.tone).toBe("urgent");
    expect(tile("social").dataset.tone).toBe("review");
    expect(tile("box").dataset.tone).toBe("none");
    expect(tile("box").querySelector(".wall-pills")).toBeNull();
  });

  it("count by the list's source rule: by_source from the server, not the rows the answer listed", async () => {
    withCard();
    await build(ALL, 1500);
    setAttention(overview([item("a")], { counts: { urgent: 9, review: 1, by_source: { git: { urgent: 9, review: 1 } } } }));
    expect(cardCounts("git")).toEqual({ urgent: 9, review: 1 });
    expect(pillText("git")).toEqual(["9 urgent", "1 to review"]);
    // The list's source line calls the same sourceCounts (needsTable.ts sourcePills), so both read 9 and 1.
    expect(sourceCounts("git")).toEqual({ urgent: 9, review: 1 });
  });
});

describe("the cards move between the Overview and their tabs", () => {
  it("takes the cards when the Overview is wide, hands them back when another tab opens, and takes them again", async () => {
    await build(ALL, 1500);
    expect(onWall().length).toBe(12);
    expect(onBoard("ship")).toEqual([]);
    shell!.show("ship");
    expect(onWall()).toEqual([]);
    expect(onBoard("ship")).toEqual(["deploys", "git", "social"]);
    expect(onBoard("cloud").length).toBe(5);
    expect(qa(".admin-card").every((card) => card.dataset.wall === undefined)).toBe(true);
    shell!.show("overview");
    expect(onWall().length).toBe(12);
    expect(onBoard("ship")).toEqual([]);
  });

  it("takes the cards back after a board rebuilds while the Overview shows", async () => {
    await build(ALL, 1500);
    // A card turned off or on makes every board lay itself out again, which pulls its cards back.
    setCards({ revision: 1, state_ok: true, cards: ALL.map((id) => row(id)) });
    setCards({ revision: 2, state_ok: true, cards: ALL.map((id) => row(id, [], id !== "presets")) });
    expect(onWall()).not.toContain("presets");
    expect(onWall().length).toBe(11);
    expect(qa(".wall-tile").every((tile) => tile.querySelector(".admin-card"))).toBe(true);
  });

  it("keeps the cards on their tabs under 760 px, and a width crossing back takes them", async () => {
    await build(ALL, 700);
    expect(ov().dataset.wall).toBe("off");
    expect(onWall()).toEqual([]);
    expect(onBoard("ship")).toEqual(["deploys", "git", "social"]);
    resize(1500);
    expect(onWall().length).toBe(12);
    resize(700);
    expect(onWall()).toEqual([]);
    expect(onBoard("cloud").length).toBe(5);
  });
});

describe("the three width bands", () => {
  it("are wide at 1,400 px and up, stack from 760 px, off under it", () => {
    expect(bandFor(2000)).toBe("wide");
    expect(bandFor(1400 + BAND_HYSTERESIS)).toBe("wide");
    expect(bandFor(1100)).toBe("stack");
    expect(bandFor(STACK_PX + BAND_HYSTERESIS)).toBe("stack");
    expect(bandFor(500)).toBe("off");
  });
  it("hold the band within a few px of a boundary, so a width resting there cannot flicker", () => {
    expect(bandFor(WIDE_PX, "stack")).toBe("stack");
    expect(bandFor(WIDE_PX, "wide")).toBe("wide");
    expect(bandFor(WIDE_PX - BAND_HYSTERESIS - 1, "wide")).toBe("stack");
    expect(bandFor(STACK_PX, "off")).toBe("off");
    expect(bandFor(STACK_PX, "stack")).toBe("stack");
    expect(bandFor(STACK_PX - BAND_HYSTERESIS - 1, "stack")).toBe("off");
  });
  it("set data-wall on the Overview as the page's width changes", async () => {
    await build(ALL, 1500);
    expect(ov().dataset.wall).toBe("wide");
    resize(1000);
    expect(ov().dataset.wall).toBe("stack");
    expect(onWall().length).toBe(12);
    resize(600);
    expect(ov().dataset.wall).toBe("off");
  });
  it("leaves the health rail off the Overview, and keeps the chips and the quiet day's tiles for the narrow bands", async () => {
    await build(ALL, 1500);
    expect(q(".as-rail")).toBeNull();
    expect(q(".as-chips")).not.toBeNull();
    expect(q(".as-tiles")).not.toBeNull();
  });
});

describe("the card title on the wall", () => {
  it("opens that card on its own tab from the wall", async () => {
    await build(ALL, 1500);
    const toggle = q('.wall-tile[data-card="git"] .admin-card-toggle')!;
    expect(toggle.title).toBe("Open the Title git card on the Ship tab");
    expect(toggle.hasAttribute("aria-expanded")).toBe(false);
    expect(toggle.tabIndex).not.toBe(-1);
    toggle.click();
    expect(q("#as-tab-ship")!.getAttribute("aria-selected")).toBe("true");
    expect(onBoard("ship")).toContain("git");
  });

  it("raises the event the shell listens for, and stays inert on a board of a wide page", async () => {
    await build(ALL, 1500);
    shell!.show("ship");
    const heard = vi.fn();
    shell!.main.addEventListener(WALL_OPEN_EVENT, heard);
    const toggle = q('#as-panel-ship .admin-card[data-card="git"] .admin-card-toggle')!;
    expect(toggle.hasAttribute("title")).toBe(false);
    expect(toggle.tabIndex).toBe(-1);
    toggle.click();
    expect(heard).not.toHaveBeenCalled();
    expect(q("#as-tab-ship")!.getAttribute("aria-selected")).toBe("true");
    shell!.show("overview");
    q('.wall-tile[data-card="social"] .admin-card-toggle')!.click();
    expect(heard).toHaveBeenCalledTimes(1);
    expect((heard.mock.calls[0]![0] as CustomEvent).detail).toBe("social");
  });
});

describe("pages that keep their layout", () => {
  it("gives a server with four cards or fewer no wall and no area tabs: the cards sit under the table", async () => {
    await build(["deploys", "git", "box", "vault"], 1500);
    expect(q("[data-wall]")).toBeNull();
    expect(q(".as-wall")).toBeNull();
    expect(q(".as-rail")).not.toBeNull();
    expect(qa("#as-panel-overview .admin-card").map((card) => card.dataset.card)).toEqual(["deploys", "git", "box", "vault"]);
  });

  it("gives the box admin's page no wall", async () => {
    shell = createShell({ context: context(), mode: "box", cards: cardsFor(["box"]), settleMs: 0 });
    document.getElementById("root")!.replaceChildren(shell.main);
    await shell.loadPrefs();
    shell.layout();
    expect(q(".as-wall")).toBeNull();
    expect(q(".as-ov-box")).not.toBeNull();
  });
});

describe("the Done list", () => {
  const DAY = 86_400_000;
  const answer = (items: Array<[string, number]>, fields: object = {}) => ({
    items: items.map(([title, ago], i) => ({
      id: `d${i}`, source: "git" as const, kind: "pr-ready", title, done_at: new Date(Date.now() - ago).toISOString(), first_seen: null,
    })),
    events: [],
    unknown: [],
    read_at: new Date().toISOString(),
    ...fields,
  });
  async function mount(data: unknown, rows: number | "fit" = 6, activity: (() => void) | null = vi.fn()) {
    const request = vi.fn(async () => ({ ok: true, status: 200, body: data }));
    await loadActivity({ server: { request } } as unknown as ExtensionContext, true);
    const host = document.createElement("section");
    document.body.append(host);
    return { host, handle: mountDoneList(host, { openActivity: activity, rows }), activity };
  }
  const rowsOf = (host: HTMLElement) => [...host.querySelectorAll<HTMLElement>(".done-row")].filter((node) => !node.hidden);

  it("lists today first, then earlier days, each row with what it was, its source and when it left", async () => {
    const { host } = await mount(answer([["Merge A", 1000], ["Merge B", 2000], ["Old one", 2 * DAY]]));
    expect([...host.querySelectorAll(".done-day")].map((n) => n.textContent)[0]).toBe("Today");
    expect(rowsOf(host).map((n) => n.querySelector(".act-what")!.textContent)).toEqual(["Merge A", "Merge B", "Old one"]);
    const first = rowsOf(host)[0]!;
    expect(first.querySelector(".act-src")!.textContent).toBe("Git repos");
    expect(first.querySelector(".act-when")!.textContent).toMatch(/^left /);
    expect(host.querySelector("h3")!.textContent).toBe("Done");
    expect(host.textContent).toContain("no longer reported");
  });

  it("opens the Activity tab from its button", async () => {
    const { host, activity } = await mount(answer([["Merge A", 1000]]));
    host.querySelector<HTMLElement>(".done-all")!.click();
    expect(activity).toHaveBeenCalled();
  });

  it("shows 6 rows when stacked", async () => {
    const { host } = await mount(answer(Array.from({ length: 10 }, (_, i) => [`Merge ${i}`, 1000 + i] as [string, number])), 6);
    expect(rowsOf(host).length).toBe(6);
  });

  it("shows only whole rows in a measured block: it hides trailing rows until nothing is cut, and an orphan day label", async () => {
    const { host, handle } = await mount(answer([["A", 1000], ["B", 2000], ["C", 3000], ["Old", 2 * DAY]]), "fit");
    const list = host.querySelector<HTMLElement>(".done-list")!;
    Object.defineProperty(list, "clientHeight", { configurable: true, get: () => 100 });
    // Each visible child (a day label or a row) is 30 px tall.
    Object.defineProperty(list, "scrollHeight", { configurable: true, get: () => [...list.children].filter((n) => !(n as HTMLElement).hidden).length * 30 });
    handle.fit();
    expect(rowsOf(host).map((n) => n.querySelector(".act-what")!.textContent)).toEqual(["A", "B"]);
    // The earlier day has no row left, so its label goes with it.
    const labels = [...list.querySelectorAll<HTMLElement>(".done-day")].filter((n) => !n.hidden);
    expect(labels.map((n) => n.textContent)).toEqual(["Today"]);
    Object.defineProperty(list, "clientHeight", { configurable: true, get: () => 400 });
    handle.fit();
    expect(rowsOf(host).length).toBe(4);
  });

  it("is one short line, not an empty box, when nothing was done in 7 days", async () => {
    const { host } = await mount(answer([]));
    expect(host.classList.contains("is-empty")).toBe(true);
    expect(host.querySelector<HTMLElement>(".done-list")!.hidden).toBe(true);
    expect(host.querySelector<HTMLElement>(".done-h")!.hidden).toBe(true);
    const line = host.querySelector<HTMLElement>(".done-none")!;
    expect(line.hidden).toBe(false);
    expect(line.textContent).toBe("Nothing was done in the last 7 days.");
  });

  it("says the log could not be read rather than showing an empty week", async () => {
    const { host } = await mount(answer([], { unknown: ["The activity log could not be read"] }));
    expect(host.querySelector('[role="status"]')!.textContent).toBe("The activity log could not be read.");
  });

  it("sits in the Overview's left column under the list, shown when the wall is", async () => {
    const request = vi.fn(async () => ({ ok: true, status: 200, body: answer([["Merge A", 1000]]) }));
    shell = createShell({ context: { ...context(), server: { request } } as unknown as ExtensionContext, mode: "full", cards: cardsFor(ALL), settleMs: 0 });
    document.getElementById("root")!.replaceChildren(shell.main);
    await shell.loadPrefs();
    await loadActivity({ server: { request } } as unknown as ExtensionContext, true);
    resize(1500);
    shell.layout();
    const done = q(".as-ov-main > .as-done")!;
    expect(done.hidden).toBe(false);
    expect(done.querySelectorAll(".done-row").length).toBe(1);
  });
});

describe("the card parts the wall's compact style names", () => {
  const NAMED = [".git-scope", ".git-list", ".social-actions", ".bh-status", ".admin-host", ".fabric-meta", ".dbx-refresh", ".azure-caption", ".vault-list", ".decision-row", ".acct-meta", ".presets-fold", ".deploy-acts", ".hs-foot"];
  it("lists them in style.css under .wall-tile", () => {
    for (const part of NAMED) expect(css, part).toContain(part);
    expect(css).toContain('.wall-tile[data-card="git"] .git-scope');
  });
  it("hides the wall only while the pane shows (PR 2 replaced the interim rule that keyed on an open source)", () => {
    expect(css).toContain('.as-ov[data-wall="wide"][data-pane] > .as-wall');
    expect(css).not.toContain('.as-ov[data-wall="wide"]:has(.nt-source .nt-grp-btn[aria-expanded="true"]) {');
  });
});

describe("fix round 1", () => {
  const four: CardId[] = ["deploys", "git", "box", "vault"];
  const loadedActivity = async () => {
    const request = vi.fn(async () => ({ ok: true, status: 200, body: { items: [], events: [], unknown: [], read_at: new Date().toISOString() } }));
    await loadActivity({ server: { request } } as unknown as ExtensionContext, true);
  };

  it("H1: four cards with the Activity tab loaded keep their page at every width: cards under the table, the rail, no wall, no Done column", async () => {
    for (const width of [1500, 1000, 600]) {
      shell?.stop();
      resetActivity();
      document.body.innerHTML = '<div id="root"></div>';
      await loadedActivity();
      await build(four, width);
      expect(qa('[role="tab"]').map((t) => t.id)).toContain("as-tab-activity");
      expect(qa('[role="tab"]').map((t) => t.id)).not.toContain("as-tab-ship");
      expect(qa("#as-panel-overview .admin-card").map((c) => c.dataset.card), `width ${width}`).toEqual(four);
      expect(q(".as-rail")).not.toBeNull();
      expect(q(".as-wall")).toBeNull();
      expect(q(".as-done")).toBeNull();
      expect(q("[data-wall]")).toBeNull();
    }
  });

  it("M1: sizes the workspace only while the Overview is open, and again when it opens", async () => {
    await build(ALL, 1500);
    shell!.show("ship");
    Object.defineProperty(window, "innerHeight", { configurable: true, value: 1000 });
    window.dispatchEvent(new Event("resize"));
    const before = ov().style.getPropertyValue("--as-ov-h");
    expect(before).not.toBe("972px");
    shell!.show("overview");
    expect(ov().style.getPropertyValue("--as-ov-h")).toBe("972px");
  });

  it("M3: classifies the first measurement at the exact thresholds, hysteresis only after", async () => {
    expect(bandFor(1400)).toBe("wide");
    expect(bandFor(1399)).toBe("stack");
    expect(bandFor(760)).toBe("stack");
    expect(bandFor(759)).toBe("off");
    await build(ALL, 1400);
    expect(ov().dataset.wall).toBe("wide");
    shell?.stop();
    document.body.innerHTML = '<div id="root"></div>';
    await build(ALL, 760);
    expect(ov().dataset.wall).toBe("stack");
    resize(755);
    expect(ov().dataset.wall).toBe("stack");
  });

  it("M2: a board rebuild keeps the focus on a control inside a wall card, and the card turned off leaves", async () => {
    setCards({ revision: 1, state_ok: true, cards: ALL.map((id) => row(id)) });
    await build(ALL, 1500);
    const toggle = q('.wall-tile[data-card="git"] .admin-card-toggle')!;
    toggle.focus();
    expect(document.activeElement).toBe(toggle);
    setCards({ revision: 2, state_ok: true, cards: ALL.map((id) => row(id, [], id !== "presets")) });
    expect(document.activeElement).toBe(toggle);
    expect(onWall()).not.toContain("presets");
    expect(onWall()).toContain("git");
  });

  it("L10: a column-count change on the page does not pull the wall's cards off or drop the focus", async () => {
    await build(ALL, 1100);
    const toggle = q('.wall-tile[data-card="social"] .admin-card-toggle')!;
    toggle.focus();
    resize(1800);
    expect(document.activeElement).toBe(toggle);
    expect(onWall().length).toBe(12);
    resize(1100);
    expect(onWall().length).toBe(12);
  });

  it("M4: the wall's card title is named by what it does and drops aria-controls; both come back off the wall", async () => {
    await build(ALL, 1500);
    const toggle = q('.wall-tile[data-card="git"] .admin-card-toggle')!;
    expect(toggle.getAttribute("aria-label")).toBe("Open the Title git card on the Ship tab");
    expect(toggle.hasAttribute("aria-controls")).toBe(false);
    shell!.show("ship");
    expect(toggle.hasAttribute("aria-label")).toBe(false);
    expect(toggle.getAttribute("aria-controls")).toBe("admin-git-body");
  });

  it("M7: two draws with the same heat readings leave the heat line's node in place, and new readings replace it", async () => {
    heat.history = [50, 60, 70];
    await build(ALL, 1500);
    const node = q(".wall-tile[data-card='box'] .wall-heat")!;
    expect(node).not.toBeNull();
    expect(q(".as-wall")!.dataset.heat).toBe("1");
    setTile({ id: "git", health: "attention", value: "x", note: "" });
    setAttention(overview([item("a")]));
    expect(q(".wall-tile[data-card='box'] .wall-heat")).toBe(node);
    heat.history = [50, 60, 80];
    setTile({ id: "git", health: "ok", value: "y", note: "" });
    expect(q(".wall-tile[data-card='box'] .wall-heat")).not.toBe(node);
    heat.history = null;
    setTile({ id: "git", health: "ok", value: "z", note: "" });
    expect(q(".wall-heat")).toBeNull();
    expect(q(".as-wall")!.dataset.heat).toBeUndefined();
  });

  it("L10: stop releases the width observer, the window listeners and the store subscriptions", async () => {
    const disconnect = vi.fn();
    vi.stubGlobal("ResizeObserver", class { observe() {} disconnect = disconnect; });
    const removed = vi.spyOn(window, "removeEventListener");
    setCards({ revision: 1, state_ok: true, cards: ALL.map((id) => row(id, id === "git" ? ["git"] : [])) });
    await build(ALL, 1500);
    const tone = () => q(".wall-tile[data-card='git']")!.dataset.tone;
    shell!.stop();
    expect(disconnect).toHaveBeenCalled();
    expect(removed.mock.calls.filter((c) => c[0] === "resize").length).toBeGreaterThan(0);
    const before = tone();
    setAttention(overview([item("a")]));
    expect(tone()).toBe(before);
    removed.mockRestore();
    vi.unstubAllGlobals();
    shell = null;
  });

  it("L5: the table's column header and previews return when a source is open (full width)", () => {
    expect(css).toContain('.as-ov[data-wall="wide"]:not(:has(.nt-source .nt-grp-btn[aria-expanded="true"])) .attn .nt-head');
  });

  it("H2 and M5: a coarse pointer gives the wall and the Done list 44 px; the phone sheet's Snooze is quiet and Close has a hover", () => {
    expect(css).toContain(".done-h .done-all,\n  .wall-tile .btn,");
    expect(css).toContain("height: var(--ctl-h, 44px)");
    expect(css).toContain(".nt-sheet .btn.nt-snooze-btn,\n.nt-sheet .btn.nt-dismiss,\n.nt-sheet .btn.nt-close {");
    expect(css).toContain(".nt-sheet .btn.nt-close:hover {");
  });

  it("L9: the Box health tile keeps its read-failure note", () => {
    expect(css).not.toContain('.wall-tile[data-card="box"] .bh-note');
  });
});

describe("the Done list notes and header", () => {
  const items = [{ id: "d0", source: "git", kind: "pr-ready", title: "Merge A", done_at: new Date(Date.now() - 1000).toISOString(), first_seen: null }];
  const serve = (body: unknown) => ({ server: { request: vi.fn(async () => ({ ok: true, status: 200, body })) } }) as unknown as ExtensionContext;

  it("L1: says the newest read failed, and how many done items the answer cut", async () => {
    await loadActivity(serve({ items, events: [], unknown: [], read_at: "x", more_items: 3 }), true);
    const host = document.createElement("section");
    document.body.append(host);
    mountDoneList(host, { openActivity: null, rows: 6 });
    expect(host.querySelector(".done-notes")!.textContent).toContain("3 more done items not shown.");
    const failing = { server: { request: vi.fn(async () => ({ ok: false, status: 500, body: { error: { code: "x", message: "no" } } })) } } as unknown as ExtensionContext;
    await loadActivity(failing, true);
    expect(host.querySelector(".done-notes")!.textContent).toContain("The newest read failed; this is the last answer.");
  });

  it("L2: builds the header once, so the Activity button keeps its focus through a tick", async () => {
    await loadActivity(serve({ items, events: [], unknown: [], read_at: "x" }), true);
    const host = document.createElement("section");
    document.body.append(host);
    mountDoneList(host, { openActivity: () => {}, rows: 6 });
    const button = host.querySelector<HTMLElement>(".done-all")!;
    button.focus();
    await loadActivity(serve({ items, events: [], unknown: [], read_at: "y" }), true);
    expect(host.querySelector(".done-all")).toBe(button);
    expect(document.activeElement).toBe(button);
  });

  it("L3: always keeps the first whole row, however little room there is", async () => {
    await loadActivity(serve({ items: [...items, { ...items[0]!, id: "d1", title: "Merge B" }], events: [], unknown: [], read_at: "x" }), true);
    const host = document.createElement("section");
    document.body.append(host);
    const handle = mountDoneList(host, { openActivity: null, rows: "fit" });
    const list = host.querySelector<HTMLElement>(".done-list")!;
    Object.defineProperty(list, "clientHeight", { configurable: true, get: () => 10 });
    Object.defineProperty(list, "scrollHeight", { configurable: true, get: () => 500 });
    handle.fit();
    const shown = [...list.querySelectorAll<HTMLElement>(".done-row")].filter((n) => !n.hidden);
    expect(shown.length).toBe(1);
  });
});

describe("the new-urgent marker for a line that appears later (L4)", () => {
  const line = (id: string, ...urgent: string[]) => ({ id, items: urgent.map((u) => item(u)) });
  beforeEach(() => {
    window.localStorage.clear();
    resetAttentionSeen();
  });
  it("seeds the first draw once per browser: a first visit shows no dot, a line that appears afterwards does", () => {
    expect([...markNewUrgent([line("attn.git", "a")], () => false)]).toEqual([]);
    expect([...markNewUrgent([line("attn.git", "a"), line("attn.box", "b")], () => false)]).toEqual(["attn.box"]);
    // Still marked until it is opened; open clears it.
    expect([...markNewUrgent([line("attn.git", "a"), line("attn.box", "b")], () => false)]).toEqual(["attn.box"]);
    expect([...markNewUrgent([line("attn.git", "a"), line("attn.box", "b")], (id) => id === "attn.box")]).toEqual([]);
  });
});

describe("the wall keeps a drawn card through a failed read (2026-11-18, review round 1)", () => {
  const tile = (extra: Record<string, unknown> = {}) => ({ id: "git", value: "Not set up", note: "", health: "idle", notSetUp: true, ...extra }) as Parameters<typeof setTile>[0];
  const mount = () => {
    const host = document.createElement("div");
    document.body.append(host);
    const wall = mountWall(host, cardsFor(["git", "deploys"]));
    wall.take();
    return { host, wall };
  };
  const drawn = (host: HTMLElement) => !!host.querySelector('[data-card="git"]');
  beforeEach(() => resetTiles());

  it("keeps a card it has drawn when its tile turns not-set-up on a failed read, and drops it on a clean answer", () => {
    const { host, wall } = mount();
    setTile({ id: "git", value: "All clear", note: "", health: "ok" });
    expect(drawn(host)).toBe(true);
    setTile(tile({ failed: true }));
    expect(drawn(host)).toBe(true);
    setTile(tile());
    expect(drawn(host)).toBe(false);
    wall.release();
  });

  it("never draws a card whose first clean read says it is not set up", () => {
    setTile(tile());
    const { host, wall } = mount();
    expect(drawn(host)).toBe(false);
    // Even a later failed read does not bring a card that was never drawn.
    setTile(tile({ failed: true }));
    expect(drawn(host)).toBe(false);
    wall.release();
  });

  it("gets a failed flag from the Git tile when a not-set-up answer arrives with an error", () => {
    expect(gitTile(calm({ configured: false }), new Error("boom"))).toMatchObject({ notSetUp: true, failed: true });
    expect(gitTile(calm({ configured: false }), null).failed).toBeUndefined();
  });
});
