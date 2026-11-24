// The Admin Center pane (2026-11-18, PR 2 of the wall plan; needsPane.ts, needsTable.ts, adminShell.ts): at the wide
// band a click on a Needs you line fills a pane in the wall's place; below it the line opens in place. The pane
// shares the table's row code, one message store and one selection with the list, borrows the source's real card,
// and is never saved.
import type { ExtensionContext } from "@omnigent/extension-sdk";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createShell, type Shell } from "../src/adminShell";
import { activateAttention, deactivateAttention, resetAttentionSeen } from "../src/attention";
import { resetAttention, setAttention } from "../src/attentionStore";
import { adminCard, type CardId, type CardSet } from "../src/board";
import { type CardRow, resetCardState, setCards } from "../src/cardState";
import { FOLD_KEY, resetFolds, rememberedFold, saveFold } from "../src/fold";
import { mountPane } from "../src/needsPane";
import { isPicked, pickedCount, resetSelection } from "../src/needsSelect";
import { saveAssignedOnly, resetAssignedView } from "../src/needsAssign";
import { resetTiles } from "../src/tiles";
import css from "../src/style.css?raw";
import { item, overview } from "./attentionFixtures";
import type { AttentionOverview } from "../src/api";
import { notice } from "../src/dom";

vi.mock("@omnigent/extension-sdk", () => ({ defineExtension: vi.fn() }));
vi.mock("../src/boxHealth", async (original) => ({
  ...(await original<typeof import("../src/boxHealth")>()),
  lastCpuHistory: () => null,
  openCleanup: vi.fn(() => true),
}));

const IDS: CardId[] = ["deploys", "git", "social", "box", "home-security", "compute", "credit", "azure", "vault", "decision", "accounts", "presets"];
const SVG = () => document.createElementNS("http://www.w3.org/2000/svg", "svg");
const cardsFor = (ids: CardId[]): CardSet =>
  Object.fromEntries(ids.map((id) => [id, adminCard(id, `Title ${id}`, SVG(), [], "body")])) as CardSet;
const cardRow = (id: string, sources: string[] = []): CardRow => ({
  id, name: id, group: "ship", description: "d", sources, on: true, default_on: true, changed_by: null, changed_at: null, fields: [],
});
const flush = () => vi.advanceTimersByTimeAsync(0);
const q = <T extends HTMLElement = HTMLElement>(sel: string, from: ParentNode = document) => from.querySelector<T>(sel);
const qa = (sel: string, from: ParentNode = document) => [...from.querySelectorAll<HTMLElement>(sel)];

const ANSWER = (): AttentionOverview =>
  overview(
    [
      item("g1", { title: "Fix #1 first" }),
      item("g2", { title: "Fix #2 second" }),
      item("gm", { source: "git-mine", title: "Mine alone", actions: ["dismiss"] }),
      item("b1", { source: "box", kind: "heat", url: "", title: "CPU hot", actions: ["dismiss"] }),
    ],
    { sources: ["git", "git-mine", "box"] },
  );

let shell: Shell | null = null;
function context(answer: AttentionOverview) {
  return {
    view: "fabric",
    server: {
      request: vi.fn(async ({ method, path }: { method: string; path: string }) =>
        `${method} ${path}` === "GET /v1/ae/attention"
          ? { ok: true, status: 200, body: answer }
          : { ok: false, status: 500, body: { error: { code: "x", message: "none" } } },
      ),
    },
    storage: { user: { get: vi.fn(async () => undefined), set: vi.fn(async () => undefined) } },
    navigation: { openExternal: vi.fn(), openPage: vi.fn(), openSession: vi.fn() },
  } as unknown as ExtensionContext;
}

function resize(width: number) {
  Object.defineProperty(shell?.main ?? document.body, "clientWidth", { configurable: true, get: () => width });
  window.dispatchEvent(new Event("resize"));
}

async function build(width = 1500, answer: AttentionOverview = ANSWER()) {
  setCards({ revision: 1, state_ok: true, cards: IDS.map((id) => cardRow(id, id === "git" ? ["git", "git-mine"] : id === "box" ? ["box"] : [])) });
  const ctx = context(answer);
  shell = createShell({ context: ctx, mode: "full", cards: cardsFor(IDS), settleMs: 0 });
  document.getElementById("root")!.replaceChildren(shell.main);
  await shell.loadPrefs();
  resize(width);
  shell.layout();
  activateAttention(ctx, shell.attention, { waiting: false, table: shell.table.hooks });
  await flush();
  return shell;
}

const line = (source: string) => q<HTMLButtonElement>(`.nt-source[data-source="${source}"] .nt-grp-btn`)!;
const areaLine = (area: string) => q<HTMLButtonElement>(`.nt-area[data-area="${area}"] > .nt-grp-head .nt-grp-btn`)!;
const pane = () => q(".as-pane")!;
const paneRows = () => qa(".nt-pane-table .nt-row", pane()).map((row) => row.dataset.id);
const gitCards = () => qa('.admin-card[data-card="git"]');

beforeEach(() => {
  resetTiles();
  resetAttention();
  resetCardState();
  resetFolds();
  resetAttentionSeen();
  resetSelection();
  resetAssignedView();
  window.localStorage.clear();
  document.body.innerHTML = '<div id="root"></div>';
  delete (window as { matchMedia?: unknown }).matchMedia;
  vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout", "Date"] });
  vi.setSystemTime(new Date("2026-11-18T12:00:00Z"));
});
afterEach(() => {
  shell?.stop();
  shell = null;
  deactivateAttention();
  resetCardState();
  vi.useRealTimers();
});

describe("a line at the wide band", () => {
  it("fills the pane with the source's rows and leaves the line's saved state alone", async () => {
    await build();
    expect(pane().hidden).toBe(true);
    expect(line("git").getAttribute("aria-expanded")).toBe("false");
    line("git").click();
    expect(pane().hidden).toBe(false);
    expect(q(".as-ov")!.dataset.pane).toBe("1");
    expect(q(".nt-pane-name")!.textContent).toBe("Git repos");
    expect(paneRows()).toEqual(["g1", "g2"]);
    // The pane's header counts like the list: 2 urgent.
    expect(qa(".nt-pane-pills .nt-pill", pane()).map((n) => n.textContent)).toEqual(["2 urgent"]);
    expect(line("git").getAttribute("aria-expanded")).toBe("false");
    expect(rememberedFold("attn.git")).toBeNull();
    expect(q(".nt-source[data-source='git']")!.classList.contains("is-selected")).toBe(true);
    expect(line("git").getAttribute("aria-current")).toBe("true");
    // Nothing opened under the line.
    expect(q(".nt-source[data-source='git'] .nt-rows")!.hidden).toBe(true);
  });

  it("ends a source line in an arrow and drops its chevron; an area line keeps its fold button (2026-11-18)", async () => {
    await build();
    const head = q(".nt-source[data-source='git'] > .nt-grp-head")!;
    expect(head.classList.contains("has-opens")).toBe(true);
    expect(q(".nt-opens", head)!.getAttribute("aria-hidden")).toBe("true");
    expect(q(".nt-chev", head)).toBeNull();
    expect(q(".nt-area[data-area='ship'] > .nt-grp-head .nt-fold")).not.toBeNull();
    // The area line ends in the same arrow too (2026-11-19, the owner's pick); the chevron stays beside its checkbox.
    expect(q(".nt-area[data-area='ship'] > .nt-grp-head .nt-opens")).not.toBeNull();
    expect(line("git").getAttribute("aria-current")).toBeNull();
    line("git").click();
    expect(line("git").getAttribute("aria-current")).toBe("true");
    expect(q(".nt-source[data-source='git']")!.classList.contains("is-selected")).toBe(true);
    expect(q(".nt-source[data-source='git'] > .nt-grp-head .nt-opens")).not.toBeNull();
  });

  it("gives every line one right column: an arrow on area lines too, a tab button with no icon, and one shared width rule (2026-11-19)", async () => {
    await build();
    const areaHead = q(".nt-area[data-area='ship'] > .nt-grp-head")!;
    expect(areaHead.classList.contains("has-opens")).toBe(true);
    expect(q(".nt-go", areaHead)!.querySelector("svg")).toBeNull();
    expect(q(".nt-go", areaHead)!.textContent).toBe("Ship tab");
    // The arrow is the line's last child, so it is the same column as a source line's.
    expect(areaHead.lastElementChild!.classList.contains("nt-opens")).toBe(true);
    expect(q(".nt-source[data-source='git'] > .nt-grp-head")!.lastElementChild!.classList.contains("nt-opens")).toBe(true);
    // Every wide line keeps a fixed 120 px action slot, empty when the line has no tab or Review button, and the
    // button fills the slot while the line button never grows into it.
    const heads = [...document.querySelectorAll<HTMLElement>(".nt-grp-head.has-opens")];
    expect(heads.length).toBeGreaterThan(2);
    for (const head of heads) expect(head.querySelectorAll(":scope > .nt-slot")).toHaveLength(1);
    expect(q(".nt-go", areaHead)!.parentElement!.classList.contains("nt-slot")).toBe(true);
    const empty = heads.filter((head) => q(".nt-slot", head)!.children.length === 0);
    expect(empty.length).toBeGreaterThan(0);
    expect(empty.every((head) => head.classList.contains("has-opens") && !q(".btn", q(".nt-slot", head)!))).toBe(true);
    expect(css).toMatch(/\.attn \.nt-grp-head\.has-opens > \.nt-slot \{[^}]*flex: none;[^}]*width: 120px;[^}]*margin-left: auto;/);
    expect(css).toMatch(/\.attn \.nt-slot > \.nt-go,\s*\.attn \.nt-slot > \.attn-review-source \{[^}]*width: 100%;/);
  });

  it("opens the area in the pane from a click on the area line's arrow", async () => {
    await build();
    expect(pane().hidden).toBe(true);
    q<HTMLElement>(".nt-area[data-area='ship'] > .nt-grp-head .nt-opens")!.click();
    expect(pane().hidden).toBe(false);
    expect(q(".nt-area[data-area='ship']")!.classList.contains("is-selected")).toBe(true);
  });

  it("keeps today's area line below the wide band: the tab button keeps its arrow icon and there is no shared column", async () => {
    await build(900);
    const areaHead = q(".nt-area[data-area='ship'] > .nt-grp-head")!;
    expect(areaHead.classList.contains("has-opens")).toBe(false);
    expect(q(".nt-opens", areaHead)).toBeNull();
    expect(q(".nt-go", areaHead)!.querySelector("svg")).not.toBeNull();
  });

  it("keeps the chevron and no arrow below the wide band", async () => {
    await build(900);
    const head = q(".nt-source[data-source='git'] > .nt-grp-head")!;
    expect(head.classList.contains("has-opens")).toBe(false);
    expect(q(".nt-opens", head)).toBeNull();
    expect(q(".nt-chev", head)).not.toBeNull();
  });

  it("opens the pane from a click on the arrow, like a click on the line", async () => {
    await build();
    expect(pane().hidden).toBe(true);
    q<HTMLElement>(".nt-source[data-source='git'] > .nt-grp-head .nt-opens")!.click();
    expect(pane().hidden).toBe(false);
    expect(q(".nt-pane-name")!.textContent).toBe("Git repos");
  });

  it("gives the card in the pane's foot the wall's tone, for urgent, review, none and a source with no counts, and no pills of its own", async () => {
    const footTone = async (answer: AttentionOverview, source: string): Promise<string | undefined> => {
      shell?.stop?.();
      document.body.replaceChildren();
      const root = document.createElement("div");
      root.id = "root";
      document.body.appendChild(root);
      deactivateAttention();
      resetAttention();
      resetCardState();
      resetTiles();
      await build(1500, answer);
      line(source).click();
      const foot = q(".nt-pane-card-in")!;
      expect(foot.classList.contains("wall-tile")).toBe(true);
      expect(qa(".wall-pills", foot)).toHaveLength(0); // the pane's own header shows the pills; the foot's card header is hidden
      return foot.dataset.tone;
    };
    const box = (extra: Parameters<typeof item>[1] = {}) => item("b1", { source: "box", kind: "heat", url: "", title: "CPU hot", actions: ["dismiss"], ...extra });
    expect(await footTone(ANSWER(), "git")).toBe("urgent");
    expect(await footTone(overview([box({ tone: "review" })], { sources: ["git", "box"] }), "box")).toBe("review");
    // None: the line is listed and the server counted zero for it.
    expect(await footTone(overview([box()], { sources: ["git", "box"], counts: { urgent: 0, review: 0, by_source: { box: { urgent: 0, review: 0 } } } }), "box")).toBe("none");
    // No counts for the source: the server's by_source has no entry for it, so the card gets no stripe.
    expect(await footTone(overview([box()], { sources: ["git", "box"], counts: { urgent: 0, review: 0, by_source: { git: { urgent: 0, review: 0 } } } }), "box")).toBe("none");
    line("box").click(); // closes the pane
    expect(q(".nt-pane-card-in")!.dataset.tone).toBeUndefined();
  });

  it("lets a long source name truncate before the pills, and keeps names where the chevron left them (the style sheet)", () => {
    const rule = (sel: string) => css.split("}").find((r) => r.includes("\n" + sel + " {") || r.trim().startsWith(sel + " {")) ?? "";
    expect(rule(".nt-source .nt-grp-name")).toMatch(/min-width: 0;[\s\S]*overflow: hidden;[\s\S]*text-overflow: ellipsis/);
    expect(rule(".nt-source > .nt-grp-head.has-opens > .nt-grp-btn")).toMatch(/padding-left: 36px/);
    expect(rule(".nt-pane-card-in.wall-tile")).not.toMatch(/box-shadow/); // the stripe shows in the foot
  });

  it("moves the focus to the pane's heading", async () => {
    await build();
    line("git").click();
    expect(document.activeElement).toBe(q(".nt-pane-name"));
  });

  it("fills the pane for an area with a heading per source, in the list's order", async () => {
    await build();
    areaLine("ship").click();
    expect(q(".nt-pane-name")!.textContent).toBe("Ship");
    expect(qa(".nt-pane-sec-h h4", pane()).map((n) => n.textContent)).toEqual(["Git repos", "My Git repos"]);
    expect(paneRows()).toEqual(["g1", "g2", "gm"]);
    expect(q(".nt-pane-card", pane())!.hidden).toBe(true);
    expect(q(".nt-area[data-area='ship']")!.classList.contains("is-selected")).toBe(true);
  });

  it("closes on a second click on the selected line", async () => {
    await build();
    line("git").click();
    line("git").click();
    expect(pane().hidden).toBe(true);
    expect(line("git").getAttribute("aria-current")).toBeNull();
  });

  it("builds the pane's rows with the table's own row code (checkbox, toggle, title, age, actions)", async () => {
    await build();
    line("git").click();
    const row = q(".nt-pane-table .nt-row", pane())!;
    expect(q("input[data-pick]", row)).not.toBeNull();
    expect(q(".nt-toggle", row)).not.toBeNull();
    expect(q(".attn-title", row)!.textContent).toBe("Fix #1 first");
    expect(q(".attn-actions", row)).not.toBeNull();
  });
});

describe("below the wide band", () => {
  it("opens a line in place as before, with no pane", async () => {
    await build(1000);
    line("git").click();
    expect(pane().hidden).toBe(true);
    expect(line("git").getAttribute("aria-expanded")).toBe("true");
    expect(q(".nt-source[data-source='git'] .nt-rows")!.hidden).toBe(false);
    expect(rememberedFold("attn.git")).toBe(true);
  });

  it("clears the pane when the window crosses below wide and opens nothing in place", async () => {
    await build(1500);
    line("git").click();
    resize(1000);
    expect(pane().hidden).toBe(true);
    expect(q(".as-ov")!.dataset.pane).toBeUndefined();
    expect(line("git").getAttribute("aria-expanded")).toBe("false");
    expect(q(".nt-source[data-source='git'] .nt-rows")!.hidden).toBe(true);
  });
});

describe("closing", () => {
  it("clears on Escape and returns the focus to the line that opened it", async () => {
    await build();
    line("git").click();
    q(".nt-pane-name")!.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true, cancelable: true }));
    expect(pane().hidden).toBe(true);
    expect(document.activeElement).toBe(line("git"));
  });

  it("clears on Close and returns the focus to the line", async () => {
    await build();
    line("git").click();
    q<HTMLButtonElement>(".nt-pane-close")!.click();
    expect(pane().hidden).toBe(true);
    expect(document.activeElement).toBe(line("git"));
    expect(q(".as-ov")!.dataset.pane).toBeUndefined();
  });

  it("clears on a tab change", async () => {
    await build();
    line("git").click();
    q<HTMLButtonElement>("#as-tab-ship")!.click();
    expect(pane().hidden).toBe(true);
    q<HTMLButtonElement>("#as-tab-overview")!.click();
    expect(pane().hidden).toBe(true);
  });
});

describe("the source's own card", () => {
  it("moves into the pane's foot, back to the wall on clear and on another source, always one element", async () => {
    await build();
    const wallTile = gitCards()[0]!.parentElement!;
    expect(wallTile.classList.contains("wall-tile")).toBe(true);
    line("git").click();
    expect(gitCards().length).toBe(1);
    expect(gitCards()[0]!.closest(".nt-pane-card")).not.toBeNull();
    expect(q(".nt-pane-card-l", pane())!.textContent).toBe("Git repos card");
    // The wall redraws on a state change and leaves the lent card where it is.
    shell!.layout();
    expect(gitCards()[0]!.closest(".nt-pane-card")).not.toBeNull();
    // Another source: Git's card goes back, Box health's moves in.
    line("box").click();
    expect(gitCards().length).toBe(1);
    expect(gitCards()[0]!.parentElement).toBe(wallTile);
    expect(qa('.admin-card[data-card="box"]').length).toBe(1);
    expect(q('.admin-card[data-card="box"]')!.closest(".nt-pane-card")).not.toBeNull();
    // Clear: the card is back on the wall.
    q<HTMLButtonElement>(".nt-pane-close")!.click();
    expect(q('.admin-card[data-card="box"]')!.closest(".wall-tile")).not.toBeNull();
    expect(qa(".nt-pane-card .admin-card").length).toBe(0);
  });

  it("shows no foot for an area", async () => {
    await build();
    areaLine("ship").click();
    expect(q(".nt-pane-card", pane())!.hidden).toBe(true);
    expect(gitCards()[0]!.closest(".wall-tile")).not.toBeNull();
  });

  it("shows no foot for a source whose card is turned off, and leaves the card alone", async () => {
    await build();
    setCards({ revision: 2, state_ok: true, cards: IDS.map((id) => ({ ...cardRow(id, id === "git" ? ["git", "git-mine"] : id === "box" ? ["box"] : []), on: id !== "git" })) });
    line("git").click();
    expect(q(".nt-pane-card", pane())!.hidden).toBe(true);
    expect(gitCards().some((card) => card.closest(".nt-pane-card"))).toBe(false);
  });
});

describe("the opened row", () => {
  it("opens its panel under the rows, not inline, one at a time", async () => {
    await build();
    line("git").click();
    q<HTMLButtonElement>(".nt-pane-table .nt-row[data-id='g1'] .nt-toggle")!.click();
    expect(qa(".nt-pane-detail .nt-panel", pane()).length).toBe(1);
    expect(q(".nt-pane-table .nt-row[data-id='g1'] .nt-panel")).toBeNull();
    expect(q(".nt-pane-detail .attn-title", pane())!.textContent).toBe("Fix #1 first");
    q<HTMLButtonElement>(".nt-pane-table .nt-row[data-id='g2'] .nt-toggle")!.click();
    expect(qa(".nt-pane-detail", pane()).length).toBe(1);
    expect(q(".nt-pane-detail .attn-title", pane())!.textContent).toBe("Fix #2 second");
    // The list's rows stay shut: the pane's open row is the pane's own.
    expect(qa(".nt-source[data-source='git'] .nt-row.is-open").length).toBe(0);
    // Close under the rows closes it.
    q<HTMLButtonElement>(".nt-detail-close")!.click();
    expect(q(".nt-pane-detail", pane())).toBeNull();
  });

  it("opens the source that holds a row with that row open", async () => {
    await build();
    expect(shell!.table.openInPane("gm")).toBe(true);
    expect(q(".nt-pane-name")!.textContent).toBe("My Git repos");
    expect(q(".nt-pane-detail .attn-title", pane())!.textContent).toBe("Mine alone");
    expect(shell!.table.openInPane("nope")).toBe(false);
  });
});

describe("one message store and one selection", () => {
  it("shows a held row message in the pane's opened row, and in the list's row once the list draws it", async () => {
    await build();
    line("git").click();
    q<HTMLButtonElement>(".nt-pane-table .nt-row[data-id='g1'] .nt-toggle")!.click();
    shell!.table.hooks.message({ root: shell!.attention } as never, "g1", notice("Could not open it. Copy the link instead.", "error"));
    expect(q(".nt-pane-detail .nt-panel-msg", pane())!.textContent).toContain("Could not open it");
    // A redraw keeps it.
    shell!.table.redraw();
    expect(q(".nt-pane-detail .nt-panel-msg", pane())!.textContent).toContain("Could not open it");
    // Below the wide band the pane is gone and the list draws the same row in place: the same store shows it there.
    resize(1000);
    line("git").click();
    expect(q(".nt-page-msg:not(.nt-pane-msg)", shell!.attention)!.textContent).toContain("Could not open it");
  });

  it("shows a held message for a closed pane row in the pane's own line", async () => {
    await build();
    line("git").click();
    shell!.table.hooks.message({ root: shell!.attention } as never, "g2", notice("Nothing opened.", "error"));
    expect(q(".nt-pane-msg", pane())!.textContent).toContain("Nothing opened");
  });

  it("makes a pane checkbox change the shared selection and the bulk bar", async () => {
    await build();
    line("git").click();
    const box = q<HTMLInputElement>(".nt-pane-table .nt-row[data-id='g1'] input[data-pick]")!;
    expect(q(".nt-bulk")!.hidden).toBe(true);
    box.click();
    expect(isPicked("g1")).toBe(true);
    expect(q(".nt-bulk")!.hidden).toBe(false);
    // The bulk bar's Clear unchecks the pane's box too.
    [...q(".nt-bulk-acts")!.querySelectorAll<HTMLButtonElement>("button")].find((b) => b.textContent === "Clear")!.click();
    expect(isPicked("g1")).toBe(false);
    expect(q<HTMLInputElement>(".nt-pane-table .nt-row[data-id='g1'] input[data-pick]")!.checked).toBe(false);
  });
});

describe("never saved", () => {
  it("starts closed after a reload", async () => {
    await build();
    line("git").click();
    expect(pane().hidden).toBe(false);
    shell!.stop();
    deactivateAttention();
    document.body.innerHTML = '<div id="root"></div>';
    await build();
    expect(pane().hidden).toBe(true);
    expect(q(".as-ov")!.dataset.pane).toBeUndefined();
  });
});

describe("style.css", () => {
  it("no longer holds the PR 1 interim rule that hides the wall when a source opens in the list", () => {
    expect(css).not.toContain('.as-ov[data-wall="wide"]:has(.nt-source .nt-grp-btn[aria-expanded="true"]) {');
    expect(css).not.toContain("Until the pane (PR 2)");
    expect(css).toContain('.as-ov[data-wall="wide"][data-pane] > .as-wall');
  });
  it("keeps the Done list under the list while the pane shows", () => {
    expect(css).not.toMatch(/\[data-pane\][^{]*\.as-done\s*\{\s*display:\s*none/);
  });
});

describe("review round 1", () => {
  const ids = () => qa("[id]").map((n) => n.id).filter(Boolean);

  it("M1: a source saved open draws closed at wide, its state survives a band change, and no id is duplicated", async () => {
    saveFold("attn.git", true);
    await build(1000);
    expect(q(".nt-source[data-source='git'] .nt-rows")!.hidden).toBe(false);
    resize(1500);
    expect(q(".nt-source[data-source='git'] .nt-rows")!.hidden).toBe(true);
    expect(line("git").getAttribute("aria-expanded")).toBe("false");
    expect(rememberedFold("attn.git")).toBe(true);
    line("git").click();
    q<HTMLButtonElement>(".nt-pane-table .nt-row[data-id='g1'] .nt-toggle")!.click();
    const all = ids();
    expect(new Set(all).size).toBe(all.length);
    expect(q(".nt-pane-detail .nt-panel")!.id.startsWith("pane-nt-p-")).toBe(true);
    expect(q(".nt-pane-table .nt-row[data-id='g1'] .nt-toggle")!.getAttribute("aria-controls")!.startsWith("pane-")).toBe(true);
    resize(1000);
    expect(q(".nt-source[data-source='git'] .nt-rows")!.hidden).toBe(false);
  });

  it("M1: Expand urgent at wide opens the urgent areas and fills the pane with the first urgent source; Collapse all closes the areas and the pane", async () => {
    await build();
    q<HTMLButtonElement>("[data-focus='tool:collapse']")!.click();
    expect(pane().hidden).toBe(true);
    expect(q(".nt-area[data-area='ship'] .nt-grp-body")!.hidden).toBe(true);
    q<HTMLButtonElement>("[data-focus='tool:expand']")!.click();
    expect(q(".nt-area[data-area='ship'] .nt-grp-body")!.hidden).toBe(false);
    expect(pane().hidden).toBe(false);
    expect(q(".nt-pane-name")!.textContent).toBe("Git repos");
    // The sources' saved states were not touched.
    expect(rememberedFold("attn.git")).toBeNull();
    q<HTMLButtonElement>("[data-focus='tool:collapse']")!.click();
    expect(pane().hidden).toBe(true);
    expect(q(".nt-area[data-area='ship'] .nt-grp-body")!.hidden).toBe(true);
  });

  it("M3: Escape on a closed menu trigger clears the pane; with the menu open it closes only the menu", async () => {
    await build();
    line("git").click();
    q<HTMLButtonElement>(".nt-pane-table .nt-row[data-id='g1'] .nt-toggle")!.click();
    const trigger = q<HTMLButtonElement>(".nt-pane-detail .nt-snooze-drop .btn-menu")!;
    trigger.focus();
    trigger.click();
    expect(trigger.getAttribute("aria-expanded")).toBe("true");
    trigger.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true, cancelable: true }));
    expect(trigger.getAttribute("aria-expanded")).toBe("false");
    expect(pane().hidden).toBe(false);
    trigger.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true, cancelable: true }));
    expect(pane().hidden).toBe(true);
  });

  it("M4: a card turned off while the pane shows it leaves the foot and keeps the target and the open row", async () => {
    await build();
    line("git").click();
    q<HTMLButtonElement>(".nt-pane-table .nt-row[data-id='g1'] .nt-toggle")!.click();
    expect(q(".nt-pane-card .admin-card", pane())).not.toBeNull();
    setCards({ revision: 3, state_ok: true, cards: IDS.map((id) => ({ ...cardRow(id, id === "git" ? ["git", "git-mine"] : id === "box" ? ["box"] : []), on: id !== "git" })) });
    expect(pane().hidden).toBe(false);
    expect(q(".nt-pane-name")!.textContent).toBe("Git repos");
    expect(q(".nt-pane-card", pane())!.hidden).toBe(true);
    expect(q(".nt-pane-card .admin-card", pane())).toBeNull();
    expect(q(".nt-pane-detail .attn-title", pane())!.textContent).toBe("Fix #1 first");
  });

  it("M4: a board that rebuilds and pulls the lent card away gets it back in the foot", async () => {
    await build();
    line("git").click();
    const card = gitCards()[0]!;
    const board = q(".as-panel-ship .as-board")!;
    board.append(card);
    board.dispatchEvent(new CustomEvent("admin-arranged", { bubbles: true }));
    expect(gitCards().length).toBe(1);
    expect(gitCards()[0]!.closest(".nt-pane-card")).not.toBeNull();
  });

  it("M4: a rebuild of the tabs keeps the pane's target and open row", async () => {
    await build();
    line("git").click();
    q<HTMLButtonElement>(".nt-pane-table .nt-row[data-id='g1'] .nt-toggle")!.click();
    // The Cloud area gains a card: the tab list changes and the shell rebuilds.
    setCards({ revision: 4, state_ok: true, cards: IDS.filter((id) => id !== "vault").map((id) => cardRow(id, id === "git" ? ["git", "git-mine"] : id === "box" ? ["box"] : [])) });
    shell!.layout();
    expect(pane().hidden).toBe(false);
    expect(q(".nt-pane-name")!.textContent).toBe("Git repos");
    expect(q(".nt-pane-detail .attn-title", pane())!.textContent).toBe("Fix #1 first");
  });

  it("M5: a data tick keeps the focus on a pane checkbox and the pane's scroll", async () => {
    await build();
    line("git").click();
    const body = q(".nt-pane-body", pane())!;
    let top = 0;
    Object.defineProperty(body, "scrollTop", { configurable: true, get: () => top, set: (v: number) => void (top = v) });
    top = 120;
    const box = q<HTMLInputElement>(".nt-pane-table .nt-row[data-id='g2'] input[data-pick]")!;
    box.focus();
    setAttribute_(overview([item("g1", { title: "Fix #1 first" }), item("g2", { title: "Fix #2 second" }), item("g3", { title: "Third" })], { sources: ["git"] }));
    expect(document.activeElement).toBe(q(".nt-pane-table .nt-row[data-id='g2'] input[data-pick]"));
    expect(top).toBe(120);
  });

  it("M5: a data tick keeps the focus inside the opened row's panel", async () => {
    await build();
    line("git").click();
    q<HTMLButtonElement>(".nt-pane-table .nt-row[data-id='g1'] .nt-toggle")!.click();
    const review = [...qa(".nt-pane-detail .attn-actions button")].find((b) => b.dataset.focus?.endsWith(":review"))!;
    review.focus();
    const key = review.dataset.focus;
    setAttribute_(overview([item("g1", { title: "Fix #1 first" }), item("g2", { title: "Fix #2 second" }), item("g3", { title: "Third" })], { sources: ["git"] }));
    expect((document.activeElement as HTMLElement).dataset.focus).toBe(key);
    expect(pane().contains(document.activeElement)).toBe(true);
  });

  it("M6: Escape after the area was folded away focuses the area line, never the body", async () => {
    await build();
    line("git").click();
    saveFold("nt.area.ship", false);
    shell!.table.redraw();
    q(".nt-pane-name")!.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true, cancelable: true }));
    expect(document.activeElement).not.toBe(document.body);
    expect(document.activeElement).toBe(areaLine("ship"));
  });

  it("M6: crossing below wide with the focus inside the pane moves it to the line", async () => {
    await build();
    line("git").click();
    expect(pane().contains(document.activeElement)).toBe(true);
    resize(1000);
    expect(pane().hidden).toBe(true);
    expect(document.activeElement).toBe(line("git"));
  });

  it("L1: a row open when the pane clears is not reopened by the next source or by a draw", async () => {
    await build();
    line("git").click();
    q<HTMLButtonElement>(".nt-pane-table .nt-row[data-id='g1'] .nt-toggle")!.click();
    q<HTMLButtonElement>(".nt-pane-close")!.click();
    line("git").click();
    expect(q(".nt-pane-detail", pane())).toBeNull();
    shell!.table.redraw();
    expect(q(".nt-pane-detail", pane())).toBeNull();
  });

  it("L2: a source whose items all go away shows a one-line empty state, keeps Close and the card foot", async () => {
    await build();
    line("git").click();
    setAttribute_(overview([item("b1", { source: "box", kind: "heat", url: "", title: "CPU hot", actions: ["dismiss"] })], { sources: ["git", "box"] }));
    expect(pane().hidden).toBe(false);
    expect(q(".nt-pane-table .attn-empty", pane())!.textContent).toBe("Nothing needs you in Git repos now");
    expect(q(".nt-pane-close")).not.toBeNull();
    expect(q(".nt-pane-card .admin-card", pane())).not.toBeNull();
  });

  it("L3: opening the pane and a row writes nothing to the fold state or under a pane key", async () => {
    await build();
    const keys: string[] = [];
    const original = Storage.prototype.setItem;
    Storage.prototype.setItem = function (key: string, value: string) {
      keys.push(key);
      return original.call(this, key, value);
    };
    try {
      line("git").click();
      q<HTMLButtonElement>(".nt-pane-table .nt-row[data-id='g1'] .nt-toggle")!.click();
    } finally {
      Storage.prototype.setItem = original;
    }
    expect(keys.filter((k) => k.startsWith(FOLD_KEY) || /pane/i.test(k))).toEqual([]);
  });

  it("L4: the wall hides and the Done list stays while the pane shows, and both return on clear", async () => {
    await build();
    const tag = document.createElement("style");
    const start = css.indexOf("/* The pane (2026-11-18, needsPane.ts)");
    tag.textContent = css.slice(start, css.indexOf(".nt-pane-attn {", start)) + '.as-ov:not([data-wall="off"]) > .as-wall { display: flex; } .as-ov:not([data-wall="off"]) .as-done:not([hidden]) { display: flex; }';
    document.head.append(tag);
    const done = q<HTMLElement>(".as-done")!;
    done.hidden = false;
    const wall = q<HTMLElement>(".as-wall")!;
    expect(getComputedStyle(wall).display).not.toBe("none");
    line("git").click();
    expect(getComputedStyle(wall).display).toBe("none");
    expect(getComputedStyle(done).display).not.toBe("none");
    q<HTMLButtonElement>(".nt-pane-close")!.click();
    expect(getComputedStyle(wall).display).not.toBe("none");
    tag.remove();
  });
});

function setAttribute_(answer: AttentionOverview) {
  setAttention(answer);
}

// ── gap fixes after the owner's desktop test (2026-11-18) ───────────────────────────────────────────────────────
const ME = "owner@example.com";
const MINE_ANSWER = (): AttentionOverview =>
  overview(
    [
      item("g1", { title: "Fix #1 first", assigned: { to: ME, by: ME, at: "2026-11-18T10:00:00Z" } }),
      item("g2", { title: "Fix #2 second" }),
      item("gm", { source: "git-mine", title: "Mine alone", actions: ["dismiss"] }),
      item("b1", { source: "box", kind: "heat", tone: "review", url: "", title: "CPU hot", actions: ["dismiss"] }),
    ],
    { sources: ["git", "git-mine", "box"], me: ME },
  );
const groupBox = (root: ParentNode) => q<HTMLInputElement>("input[data-pick-group]", root)!;
const chev = (area: string) => q<HTMLButtonElement>(`.nt-area[data-area="${area}"] > .nt-grp-head .nt-fold`)!;

describe("line checkboxes at the wide band", () => {
  it("select every listed row of a source though its line draws closed, with the bulk bar and mixed state", async () => {
    await build();
    const src = q(".nt-source[data-source='git']")!;
    expect(groupBox(src).disabled).toBe(false);
    groupBox(src).click();
    await flush();
    expect(pickedCount()).toBe(2);
    expect(q(".nt-bulk")!.hidden).toBe(false);
    expect(isPicked("g1") && isPicked("g2")).toBe(true);
    q<HTMLInputElement>("input[data-pick='g1']")?.click();
    // The area line over Git repos and My Git repos shows the mixed state (2 of 3).
    const area = groupBox(q(".nt-area[data-area='ship']")!.querySelector(":scope > .nt-grp-head")!);
    expect(area.indeterminate).toBe(true);
    area.click();
    await flush();
    expect(pickedCount()).toBe(3);
  });

  it("follows Assigned to me: only the assigned rows are selected", async () => {
    saveAssignedOnly(ME, true);
    await build(1500, MINE_ANSWER());
    groupBox(q(".nt-source[data-source='git']")!).click();
    await flush();
    expect(pickedCount()).toBe(1);
    expect(isPicked("g1")).toBe(true);
  });

  it("select a closed line's rows below wide too", async () => {
    await build(1000);
    expect(line("git").getAttribute("aria-expanded")).toBe("false");
    const box = groupBox(q(".nt-source[data-source='git']")!);
    expect(box.disabled).toBe(false);
    box.click();
    await flush();
    expect(pickedCount()).toBe(2);
  });
});

describe("an area's chevron", () => {
  it("is its own button that folds and unfolds the area in place at wide, leaving the pane alone", async () => {
    await build();
    const c = chev("ship");
    expect(c.getAttribute("aria-label")).toBe("Fold Ship");
    expect(c.getAttribute("aria-expanded")).toBe("true");
    expect(areaLine("ship").contains(c)).toBe(false);
    c.click();
    expect(pane().hidden).toBe(true);
    expect(chev("ship").getAttribute("aria-label")).toBe("Unfold Ship");
    expect(chev("ship").getAttribute("aria-expanded")).toBe("false");
    expect(q(".nt-area[data-area='ship'] > .nt-grp-body")!.hidden).toBe(true);
    chev("ship").click();
    expect(q(".nt-area[data-area='ship'] > .nt-grp-body")!.hidden).toBe(false);
    // The rest of the line still fills the pane.
    areaLine("ship").click();
    expect(pane().hidden).toBe(false);
  });

  it("folds in place below wide too, and the line then folds with it", async () => {
    await build(1000);
    const body = () => q(".nt-area[data-area='ship'] > .nt-grp-body")!;
    expect(chev("ship").getAttribute("aria-expanded")).toBe("true");
    expect(body().hidden).toBe(false);
    chev("ship").click();
    expect(chev("ship").getAttribute("aria-expanded")).toBe("false");
    expect(areaLine("ship").getAttribute("aria-expanded")).toBe("false");
    expect(body().hidden).toBe(true);
    expect(rememberedFold("nt.area.ship")).toBe(false);
    areaLine("ship").click();
    expect(chev("ship").getAttribute("aria-expanded")).toBe("true");
    expect(body().hidden).toBe(false);
  });

  it("is the only control that announces the fold state at wide; below wide the line announces it too", async () => {
    await build(1500);
    const bodyId = q(".nt-area[data-area='ship'] > .nt-grp-body")!.id;
    expect(areaLine("ship").hasAttribute("aria-expanded")).toBe(false);
    expect(areaLine("ship").hasAttribute("aria-controls")).toBe(false);
    expect(chev("ship").getAttribute("aria-controls")).toBe(bodyId);
    shell?.stop();
    deactivateAttention();
    document.body.innerHTML = '<div id="root"></div>';
    await build(1000);
    expect(areaLine("ship").getAttribute("aria-expanded")).toBe("true");
    expect(areaLine("ship").getAttribute("aria-controls")).toBe(q(".nt-area[data-area='ship'] > .nt-grp-body")!.id);
    expect(chev("ship").getAttribute("aria-controls")).toBe(areaLine("ship").getAttribute("aria-controls"));
  });
});

describe("Expand urgent at wide", () => {
  const expand = () => qa(".attn-tools .btn").find((b) => b.textContent!.includes("Expand urgent"))!;
  it("opens each area with an urgent item and leaves the others as they were", async () => {
    await build();
    saveFold("nt.area.box", true);
    saveFold("nt.area.ship", false);
    shell!.table.redraw();
    await flush();
    expect(chev("box").getAttribute("aria-expanded")).toBe("true");
    expect(chev("ship").getAttribute("aria-expanded")).toBe("false");
    expand().click();
    await flush();
    // Ship holds urgent items and opens; Box (no urgent item) stays open as it was; the pane shows the first urgent source.
    expect(chev("ship").getAttribute("aria-expanded")).toBe("true");
    expect(chev("box").getAttribute("aria-expanded")).toBe("true");
    expect(q(".nt-pane-name")!.textContent).toBe("Git repos");
  });
});

describe("the toolbar with Assigned to me", () => {
  const tools = () => q(".attn-tools")!;
  const labels = () => qa(".attn-tools .btn, .attn-tools .nt-view-chip").map((b) => (b.textContent ?? "").replace(/\d+$/, "").trim());

  it("keeps the same buttons in both modes, even with nothing assigned, and a padded empty line", async () => {
    await build(1500, ANSWER_WITH_ME());
    const before = labels();
    expect(before).toEqual(expect.arrayContaining(["Assigned to me", "Expand urgent", "Collapse all"]));
    q(".nt-view-chip")!.click();
    await flush();
    expect(labels()).toEqual(before);
    expect(qa(".attn-tools .btn").every((b) => (b as HTMLButtonElement).disabled)).toBe(true);
    expect(tools().querySelector(".nt-view-chip")).not.toBeNull();
    expect(q(".attn-empty")!.textContent).toBe("Nothing is assigned to you.");
  });

  it("makes the pane show only the assigned rows, with its own empty line", async () => {
    await build(1500, MINE_ANSWER());
    line("git").click();
    expect(paneRows()).toEqual(["g1", "g2"]);
    q(".nt-view-chip")!.click();
    await flush();
    expect(paneRows()).toEqual(["g1"]);
    line("git").click();
    line("git").click();
  });

  it("says the pane is empty when nothing is assigned in the source", async () => {
    await build(1500, MINE_ANSWER());
    line("git").click();
    saveAssignedOnly(ME, true);
    const other = MINE_ANSWER();
    other.items = other.items.filter((i) => i.id !== "g1");
    setAttention({ ...other });
    await flush();
    // Already on (saved above): the chip is a toggle now, so a click here would turn it off.
    expect(q(".nt-view-chip")!.getAttribute("aria-pressed")).toBe("true");
    expect(q(".nt-pane .attn-empty")?.textContent ?? "").toContain("is assigned to you");
  });
});

function ANSWER_WITH_ME(): AttentionOverview {
  const a = ANSWER();
  a.me = ME;
  return a;
}

describe("Escape closes the pane from the list and the page body", () => {
  const esc = (target: Element | Document) => target.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true, cancelable: true }));

  it("from the Needs you list, returning the focus to the line", async () => {
    await build();
    line("git").click();
    const other = line("box");
    other.focus();
    esc(other);
    expect(pane().hidden).toBe(true);
    expect(document.activeElement).toBe(line("git"));
  });

  it("from the page body", async () => {
    await build();
    line("git").click();
    (document.activeElement as HTMLElement).blur();
    esc(document.body);
    expect(pane().hidden).toBe(true);
  });

  it("not from a text field, a select, or a dialog", async () => {
    await build();
    line("git").click();
    const input = document.createElement("input");
    q("#needs-you")!.append(input);
    input.focus();
    esc(input);
    expect(pane().hidden).toBe(false);
    const dialog = document.createElement("div");
    dialog.setAttribute("role", "dialog");
    const inner = document.createElement("button");
    dialog.append(inner);
    q("#needs-you")!.append(dialog);
    esc(inner);
    expect(pane().hidden).toBe(false);
    const handled = new KeyboardEvent("keydown", { key: "Escape", bubbles: true, cancelable: true });
    line("box").addEventListener("keydown", (e) => e.preventDefault(), { once: true });
    line("box").dispatchEvent(handled);
    expect(pane().hidden).toBe(false);
  });
});

describe("two filters at once", () => {
  it("reads as one line with one Show all that clears both, and the strip keeps every source", async () => {
    saveAssignedOnly(ME, true);
    await build(1500, MINE_ANSWER());
    const strip = () => qa(".nt-seg").map((b) => b.textContent);
    const all = strip();
    expect(all.length).toBeGreaterThan(1);
    q<HTMLButtonElement>(".nt-seg[aria-label*='Box health']")?.click();
    await flush();
    const chip = q<HTMLButtonElement>(".attn-chip")!;
    expect(chip.textContent).toBe("Assigned to you in Box health: nothing · Show all");
    expect(qa(".attn-empty").map((n) => n.textContent)).not.toContain("Nothing is assigned to you.");
    expect(strip()).toEqual(all);
    chip.click();
    await flush();
    expect(q(".attn-chip")).toBeNull();
    expect(qa(".nt-view-chip.is-on")).toHaveLength(0);
  });
});

describe("Expand urgent and Collapse all act on what is listed (review round 1)", () => {
  const tool = (words: string) => qa(".attn-tools .btn").find((b) => b.textContent!.includes(words))!;

  it("fills the pane with the first urgent source of the filtered list, never a hidden one", async () => {
    await build();
    q<HTMLButtonElement>(".nt-seg[aria-label^='Box health']")!.click();
    await flush();
    tool("Expand urgent").click();
    await flush();
    expect(q(".nt-pane-name")!.textContent).toBe("Box health");
  });

  it("leaves the folds of a filtered-out area alone when it collapses all", async () => {
    await build();
    saveFold("nt.area.ship", true);
    q<HTMLButtonElement>(".nt-seg[aria-label^='Box health']")!.click();
    await flush();
    tool("Collapse all").click();
    await flush();
    expect(rememberedFold("nt.area.ship")).toBe(true);
    expect(rememberedFold("nt.area.box")).toBe(false);
  });

  it("below wide touches only the sources inside areas with an urgent item", async () => {
    await build(1000, MINE_ANSWER());
    // Seeded open: the old code wrote closed for every source whose area it walked, so it would fail this.
    saveFold("nt.area.box", true);
    saveFold("attn.box", true);
    shell!.table.redraw();
    await flush();
    tool("Expand urgent").click();
    await flush();
    expect(rememberedFold("nt.area.ship")).toBe(true);
    expect(rememberedFold("attn.git")).toBe(true);
    expect(rememberedFold("nt.area.box")).toBe(true);
    expect(rememberedFold("attn.box")).toBe(true);
  });
});

describe("Escape and what takes it for itself (review round 1)", () => {
  const esc = (target: Element | Document) => {
    const event = new KeyboardEvent("keydown", { key: "Escape", bubbles: true, cancelable: true });
    target.dispatchEvent(event);
    return event;
  };
  const inPane = (node: HTMLElement): HTMLElement => {
    q(".nt-pane-main", pane())!.append(node);
    node.focus();
    return node;
  };

  it("keeps the pane open from a select, a text field or an open menu mounted inside it", async () => {
    await build();
    line("git").click();
    const select = document.createElement("select");
    select.append(new Option("a"));
    esc(inPane(select));
    expect(pane().hidden).toBe(false);
    const input = document.createElement("input");
    esc(inPane(input));
    expect(pane().hidden).toBe(false);
    const open = document.createElement("button");
    open.setAttribute("aria-haspopup", "menu");
    open.setAttribute("aria-expanded", "true");
    esc(inPane(open));
    expect(pane().hidden).toBe(false);
    const area = document.createElement("textarea");
    esc(inPane(area));
    expect(pane().hidden).toBe(false);
    // A checkbox is not a text field: Escape still closes the pane from a ticked row checkbox.
    const text = document.createElement("input");
    text.type = "text";
    esc(inPane(text));
    expect(pane().hidden).toBe(false);
    // A plain control in the pane still closes it.
    const plain = document.createElement("button");
    esc(inPane(plain));
    expect(pane().hidden).toBe(true);
  });

  it("closes the pane from a row checkbox inside it", async () => {
    await build();
    line("git").click();
    const box = q<HTMLInputElement>("input[data-pick]", pane())!;
    box.click();
    box.focus();
    expect(box.checked).toBe(true);
    esc(box);
    expect(pane().hidden).toBe(true);
  });

  it("does nothing when the pane is closed", async () => {
    await build();
    expect(pane().hidden).toBe(true);
    expect(esc(document.body).defaultPrevented).toBe(false);
    expect(esc(line("git")).defaultPrevented).toBe(false);
    expect(pane().hidden).toBe(true);
  });

  describe("the document listener's lifecycle", () => {
    const deps = () => ({ view: vi.fn(), redraw: () => false, focusLine: vi.fn(), lend: vi.fn() }) as unknown as Parameters<typeof mountPane>[1];
    const keydownHandler = (spy: { mock: { calls: unknown[][] } }) => spy.mock.calls.filter((c) => c[0] === "keydown").map((c) => c[1]).at(-1);

    it("is removed by stop()", () => {
      const add = vi.spyOn(document, "addEventListener");
      const remove = vi.spyOn(document, "removeEventListener");
      const host = document.createElement("section");
      document.body.append(host);
      const mounted = mountPane(host, deps());
      const handler = keydownHandler(add);
      expect(handler).toBeTypeOf("function");
      mounted.stop();
      expect(remove).toHaveBeenCalledWith("keydown", handler);
      add.mockRestore();
      remove.mockRestore();
    });

    it("is dropped by the first Escape after the host left the page (a shell rebuild)", () => {
      const add = vi.spyOn(document, "addEventListener");
      const remove = vi.spyOn(document, "removeEventListener");
      const host = document.createElement("section");
      document.body.append(host);
      mountPane(host, deps());
      const handler = keydownHandler(add);
      host.remove();
      esc(document.body);
      expect(remove).toHaveBeenCalledWith("keydown", handler);
      add.mockRestore();
      remove.mockRestore();
    });
  });
});
