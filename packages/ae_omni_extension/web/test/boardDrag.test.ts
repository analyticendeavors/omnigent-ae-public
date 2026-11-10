// The Admin Center's card arrangement (2026-10-22): the saved layout read defensively, the pure
// steps that move a card, the drag and its keyboard twin, and the Reset layout button. jsdom does
// no layout, so a drag is driven with stubbed rectangles. Since 2026-11-07 (packed board) the cards
// sit in n packed columns under Needs you, move anywhere on the board, and the save is per column
// count under `board.packed`.
import type { ExtensionContext } from "@omnigent/extension-sdk";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { adminCard, arrange, type CardId, mountBoard } from "../src/board";
import { currentLayout, layoutResetButton } from "../src/boardDrag";
import {
  dropTarget,
  hasArrangement,
  LAYOUT_KEY,
  loadBoardLayout,
  moveCard,
  onLayoutChange,
  packedArrangement,
  packLayout,
  resetArrangement,
  resetBoardLayoutState,
  setArrangement,
} from "../src/boardLayout";
import { DOMAINS } from "../src/tiles";

const sdk = vi.hoisted(() => ({ defineExtension: vi.fn() }));
vi.mock("@omnigent/extension-sdk", () => sdk);

/** Every card the board tests use, in board order (Ship, Box, Cloud, Accounts). */
const BOARD_IDS: CardId[] = [
  "deploys",
  "git",
  "social",
  "box",
  "home-security",
  "compute",
  "credit",
  "azure",
  "vault",
  "decision",
  "accounts",
  "presets",
];
/** What the board packs by default for *n* columns. */
const packed = (n: 2 | 3 | 4) => packLayout(n, BOARD_IDS);
const layoutOf = (board: HTMLElement) => currentLayout(board);

function storageContext(initial?: unknown) {
  const set = vi.fn(async () => undefined);
  const get = vi.fn(async () => initial);
  const context = { storage: { user: { get, set } } } as unknown as ExtensionContext;
  return { context, set, get };
}

beforeEach(() => {
  document.body.replaceChildren();
  resetBoardLayoutState();
});

afterEach(() => {
  resetBoardLayoutState();
});

describe("the saved arrangement", () => {
  it("is the packed default until something is saved, per column count", () => {
    expect(DOMAINS.flatMap((d) => d.cards)).toEqual(expect.arrayContaining(BOARD_IDS));
    expect(packedArrangement(3, BOARD_IDS)).toEqual(packed(3));
    const mine = [["azure"], ["compute", "credit"], BOARD_IDS.filter((id) => !["azure", "compute", "credit"].includes(id))];
    setArrangement(3, mine as CardId[][]);
    expect(packedArrangement(3, BOARD_IDS)).toEqual(mine);
    expect(packedArrangement(2, BOARD_IDS)).toEqual(packed(2));
    expect(packedArrangement(4, BOARD_IDS)).toEqual(packed(4));
  });

  it("drops an unknown card and a repeat, and places a missing card in the shortest column", () => {
    setArrangement(2, [["deploys", "gone", "deploys"], ["git"]] as unknown as CardId[][]);
    // box is missing: column 1 is 420 + 160, column 2 is 490, so it goes to column 2.
    expect(packedArrangement(2, ["deploys", "git", "box"])).toEqual([["deploys"], ["git", "box"]]);
  });

  it("ignores an arrangement with the wrong column count or no known card", () => {
    setArrangement(3, [["deploys"], ["git"]]);
    expect(packedArrangement(3, BOARD_IDS)).toEqual(packed(3));
    setArrangement(3, [["x"], ["y"], ["z"]] as unknown as CardId[][]);
    expect(packedArrangement(3, BOARD_IDS)).toEqual(packed(3));
  });

  it("is read from the new key per count; a bad count is ignored on its own", async () => {
    const mine = [["azure"], ["compute"], []];
    const { context, get } = storageContext({ "3": mine, "2": "nope", "4": [[1]] });
    await loadBoardLayout(context);
    expect(get).toHaveBeenCalledWith("board.packed");
    expect(hasArrangement(3)).toBe(true);
    expect(hasArrangement(2)).toBe(false);
    expect(hasArrangement(4)).toBe(false);
    resetBoardLayoutState();
    await loadBoardLayout(storageContext([1, 2]).context);
    expect(hasArrangement()).toBe(false);
    // A save with the wrong number of columns, or with no card, is not on the page: no Reset layout.
    resetBoardLayoutState();
    await loadBoardLayout(storageContext({ "2": [["deploys"]], "3": [[], [], []], "4": [] }).context);
    expect(hasArrangement()).toBe(false);
    resetBoardLayoutState();
    const broken = { storage: { user: { get: () => Promise.reject(new Error("no")) } } };
    await loadBoardLayout(broken as unknown as ExtensionContext);
    expect(hasArrangement()).toBe(false);
  });

  it("never reads the old per-group board.layout", async () => {
    const old = { "3": { ship: [["deploys"], [], []] } };
    const get = vi.fn(async (key: string) => (key === "board.layout" ? old : undefined));
    const set = vi.fn(async () => undefined);
    await loadBoardLayout({ storage: { user: { get, set } } } as unknown as ExtensionContext);
    expect(get).toHaveBeenCalledTimes(1);
    expect(get).not.toHaveBeenCalledWith("board.layout");
    expect(hasArrangement()).toBe(false);
    expect(set).not.toHaveBeenCalled();
  });

  it("saves under one key, per column count, and tells listeners", async () => {
    const { context, set } = storageContext(undefined);
    await loadBoardLayout(context);
    const heard = vi.fn();
    onLayoutChange(heard);
    setArrangement(2, [["box"], ["deploys"]]);
    expect(LAYOUT_KEY).toBe("board.packed");
    expect(set).toHaveBeenCalledWith(LAYOUT_KEY, { "2": [["box"], ["deploys"]] });
    setArrangement(4, [["box"], [], [], ["deploys"]]);
    expect(set).toHaveBeenLastCalledWith(LAYOUT_KEY, {
      "2": [["box"], ["deploys"]],
      "4": [["box"], [], [], ["deploys"]],
    });
    resetArrangement();
    expect(set).toHaveBeenLastCalledWith(LAYOUT_KEY, {});
    expect(heard).toHaveBeenCalledTimes(3);
    expect(hasArrangement()).toBe(false);
  });
});

describe("moving a card", () => {
  const layout: CardId[][] = [["deploys", "box"], ["accounts", "compute"], ["azure"]];

  it("steps up, down and across, and refuses to leave the board", () => {
    expect(moveCard(layout, "box", "up")).toEqual([
      ["box", "deploys"],
      ["accounts", "compute"],
      ["azure"],
    ]);
    expect(moveCard(layout, "deploys", "down")).toEqual([
      ["box", "deploys"],
      ["accounts", "compute"],
      ["azure"],
    ]);
    expect(moveCard(layout, "deploys", "up")).toBeNull();
    expect(moveCard(layout, "box", "down")).toBeNull();
    expect(moveCard(layout, "deploys", "left")).toBeNull();
    expect(moveCard(layout, "azure", "right")).toBeNull();
    expect(moveCard(layout, "box", "right")).toEqual([
      ["deploys"],
      ["accounts", "box", "compute"],
      ["azure"],
    ]);
    // To a shorter column: the end of it.
    expect(moveCard(layout, "compute", "right")).toEqual([
      ["deploys", "box"],
      ["accounts"],
      ["azure", "compute"],
    ]);
    expect(moveCard(layout, "credit", "up")).toBeNull();
  });

  it("picks the column under the pointer, else the nearest, and the place by card middles", () => {
    const columns = [
      {
        left: 0,
        right: 100,
        cards: [
          { top: 0, bottom: 100 },
          { top: 110, bottom: 210 },
        ],
      },
      { left: 110, right: 210, cards: [] },
    ];
    expect(dropTarget(columns, 50, 10)).toEqual({ column: 0, index: 0 });
    expect(dropTarget(columns, 50, 120)).toEqual({ column: 0, index: 1 });
    expect(dropTarget(columns, 50, 400)).toEqual({ column: 0, index: 2 });
    expect(dropTarget(columns, 150, 400)).toEqual({ column: 1, index: 0 });
    // In the gutter or past the edge: the nearest column.
    expect(dropTarget(columns, 105, 10).column).toBe(0);
    expect(dropTarget(columns, 900, 10).column).toBe(1);
    expect(dropTarget([], 5, 5)).toEqual({ column: 0, index: 0 });
  });
});


const WIDTH: Record<2 | 3 | 4, number> = { 2: 900, 3: 1400, 4: 1800 };

function makeBoard(n: 2 | 3 | 4 = 3) {
  const main = document.createElement("main");
  const attention = document.createElement("section");
  attention.id = "needs-you";
  const board = document.createElement("div");
  board.className = "admin-board";
  main.append(attention, board);
  document.body.appendChild(main);
  const cards = Object.fromEntries(
    BOARD_IDS.map((id) => [
      id,
      adminCard(id, id, document.createElementNS("http://www.w3.org/2000/svg", "svg"), [], "body"),
    ]),
  ) as Record<CardId, HTMLElement>;
  const page = document.createElement("div");
  Object.defineProperty(page, "clientWidth", { value: WIDTH[n] });
  const layout = mountBoard(page, board, cards, attention);
  return { board, cards, layout, attention };
}

const grip = (card: HTMLElement) =>
  card.querySelector<HTMLButtonElement>(".admin-grip") as HTMLButtonElement;

function stubRect(node: Element, left: number, top: number, width: number, height: number) {
  (node as HTMLElement).getBoundingClientRect = () =>
    ({
      left,
      top,
      width,
      height,
      right: left + width,
      bottom: top + height,
      x: left,
      y: top,
      toJSON() {},
    }) as DOMRect;
}

/** Fixed boxes: 300 wide columns 310 apart, 100 high children (Needs you included), 110 apart. */
function stubLayout(board: HTMLElement) {
  [...board.querySelectorAll(":scope > .admin-col")].forEach((col, c) => {
    stubRect(col, c * 310, 0, 300, 2000);
    [...col.children].forEach((child, r) => stubRect(child, c * 310, r * 110, 300, 100));
  });
}

const fire = (target: EventTarget, type: string, x = 0, y = 0, extra: object = {}) =>
  target.dispatchEvent(
    new MouseEvent(type, { bubbles: true, cancelable: true, clientX: x, clientY: y, ...extra }),
  );

const needsFirst = (board: HTMLElement) =>
  expect(board.querySelector(":scope > .admin-col")?.firstElementChild?.id).toBe("needs-you");

describe("the grips", () => {
  it("sit in every card's header and are named for their card; Needs you has none", () => {
    const { board, cards, attention } = makeBoard();
    for (const id of BOARD_IDS) {
      const found = grip(cards[id]);
      expect(found.closest(".admin-card-top")).not.toBeNull();
      expect(found.getAttribute("aria-label")).toContain(`Move ${id}`);
    }
    expect(attention.querySelector(".admin-grip")).toBeNull();
    needsFirst(board);
  });
});

describe("dragging", () => {
  it("lifts the card, moves a dashed slot with the pointer, and saves the whole board's columns", () => {
    const { board, cards } = makeBoard(3);
    stubLayout(board);
    const heard = vi.fn();
    onLayoutChange(heard);
    const homeColumn = layoutOf(board).findIndex((col) => col.includes("azure"));
    fire(grip(cards.azure), "pointerdown", 330, 120, { button: 0 });
    expect(cards.azure.classList.contains("is-dragging")).toBe(true);
    expect(board.classList.contains("is-sorting")).toBe(true);
    // Over the third column, above its first card: the slot goes first there.
    fire(document, "pointermove", 700, 10);
    const slot = board.querySelector(".admin-slot") as HTMLElement;
    expect(slot.parentElement).toBe(board.children[2]);
    expect(cards.azure.style.left).toBe(`${700 - 330 + homeColumn * 310}px`);
    fire(document, "pointerup", 700, 10);
    const now = layoutOf(board);
    expect(now[2][0]).toBe("azure");
    expect(now.flat().sort()).toEqual([...BOARD_IDS].sort());
    expect(packedArrangement(3, BOARD_IDS)).toEqual(now);
    expect(heard).toHaveBeenCalledTimes(1);
    expect(board.querySelector(".admin-slot")).toBeNull();
    expect(board.classList.contains("is-sorting")).toBe(false);
    expect(cards.azure.classList.contains("is-dragging")).toBe(false);
    expect(cards.azure.style.left).toBe("");
    needsFirst(board);
  });

  it("moves a card to any column, whatever its group", () => {
    const { board, cards } = makeBoard(3);
    const before = layoutOf(board);
    const home = before.findIndex((col) => col.includes("deploys"));
    const target = home === 0 ? 1 : 0;
    stubLayout(board);
    fire(grip(cards.deploys), "pointerdown", 20, 20, { button: 0 });
    fire(document, "pointermove", target * 310 + 50, 1500);
    fire(document, "pointerup", target * 310 + 50, 1500);
    const after = layoutOf(board);
    expect(after[target].at(-1)).toBe("deploys");
    expect(after.some((col, i) => i !== target && col.includes("deploys"))).toBe(false);
  });

  it("never drops above Needs you: index 0 of column 1 is just under it", () => {
    const { board, cards } = makeBoard(3);
    stubLayout(board);
    fire(grip(cards.azure), "pointerdown", 330, 120, { button: 0 });
    fire(document, "pointermove", 50, 5);
    fire(document, "pointerup", 50, 5);
    needsFirst(board);
    expect(layoutOf(board)[0][0]).toBe("azure");
    expect((board.children[0].children[1] as HTMLElement).dataset.card).toBe("azure");
  });

  it("drops into the middle of a column by the cards' middles", () => {
    const { board, cards } = makeBoard(3);
    const column = layoutOf(board)[1];
    const mover = column.at(-1) as CardId;
    stubLayout(board);
    fire(grip(cards[mover]), "pointerdown", 330, 120, { button: 0 });
    // Column two's first card spans 0 to 100 (middle 50); y 70 is past it.
    fire(document, "pointermove", 330 + 50, 70);
    fire(document, "pointerup", 380, 70);
    expect(layoutOf(board)[1].indexOf(mover)).toBe(1);
  });

  it("takes the keyboard into the frame, so Escape reaches a drag the page started", () => {
    const { board, cards } = makeBoard();
    stubLayout(board);
    expect(document.activeElement).not.toBe(grip(cards.deploys));
    fire(grip(cards.deploys), "pointerdown", 20, 20, { button: 0 });
    expect(document.activeElement).toBe(grip(cards.deploys));
    fire(document, "pointerup", 20, 20);
  });

  it("puts nothing anywhere on Escape or a cancelled pointer", () => {
    const { board, cards } = makeBoard();
    stubLayout(board);
    const before = layoutOf(board);
    fire(grip(cards.deploys), "pointerdown", 20, 20, { button: 0 });
    fire(document, "pointermove", 700, 10);
    document.dispatchEvent(
      new KeyboardEvent("keydown", { key: "Escape", bubbles: true, cancelable: true }),
    );
    expect(layoutOf(board)).toEqual(before);
    expect(hasArrangement()).toBe(false);
    expect(board.querySelector(".admin-slot")).toBeNull();
    expect(cards.deploys.classList.contains("is-dragging")).toBe(false);
    fire(grip(cards.deploys), "pointerdown", 20, 20, { button: 0 });
    fire(document, "pointercancel");
    expect(hasArrangement()).toBe(false);
    expect(board.querySelector(".admin-slot")).toBeNull();
    fire(document, "pointermove", 700, 10);
    expect(cards.deploys.style.left).toBe("");
  });

  it("ignores a secondary button, a press off the grip, and a one column board", () => {
    const { board, cards, attention } = makeBoard();
    stubLayout(board);
    fire(grip(cards.deploys), "pointerdown", 20, 20, { button: 2 });
    expect(board.querySelector(".admin-slot")).toBeNull();
    fire(cards.deploys.querySelector(".admin-card-toggle") as Element, "pointerdown", 20, 20, {
      button: 0,
    });
    expect(board.querySelector(".admin-slot")).toBeNull();
    arrange(board, cards, 1, false, attention);
    fire(grip(cards.deploys), "pointerdown", 20, 20, { button: 0 });
    expect(board.querySelector(".admin-slot")).toBeNull();
  });

  it("stops listening when the board is stopped", () => {
    const { board, cards, layout } = makeBoard();
    stubLayout(board);
    layout.stop();
    fire(grip(cards.deploys), "pointerdown", 20, 20, { button: 0 });
    expect(board.querySelector(".admin-slot")).toBeNull();
  });
});

describe("the keyboard", () => {
  const press = (target: Element, key: string) =>
    target.dispatchEvent(new KeyboardEvent("keydown", { key, bubbles: true, cancelable: true }));

  it("moves a card left and right across the whole board, keeps the focus on its grip, and saves", () => {
    const { board, cards } = makeBoard(3);
    const start = layoutOf(board);
    const id = start[1][0];
    const g = grip(cards[id]);
    g.focus();
    press(g, "ArrowLeft");
    expect(layoutOf(board)[0]).toContain(id);
    expect(layoutOf(board)[0][0]).toBe(id);
    expect(document.activeElement).toBe(grip(cards[id]));
    needsFirst(board);
    press(grip(cards[id]), "ArrowRight");
    press(grip(cards[id]), "ArrowRight");
    expect(layoutOf(board)[2]).toContain(id);
    expect(packedArrangement(3, BOARD_IDS)).toEqual(layoutOf(board));
  });

  it("moves a card up and down inside its column", () => {
    const { board, cards } = makeBoard(3);
    const col = layoutOf(board)[1];
    press(grip(cards[col[0]]), "ArrowDown");
    expect(layoutOf(board)[1].slice(0, 2)).toEqual([col[1], col[0]]);
  });

  it("does nothing at the edges: nothing goes above Needs you, left of column 1 or right of the last", () => {
    const { board, cards } = makeBoard(3);
    const heard = vi.fn();
    onLayoutChange(heard);
    const first = layoutOf(board)[0][0];
    press(grip(cards[first]), "ArrowUp");
    press(grip(cards[first]), "ArrowLeft");
    const last = layoutOf(board)[2].at(-1) as CardId;
    press(grip(cards[last]), "ArrowRight");
    press(grip(cards[last]), "ArrowDown");
    press(grip(cards[last]), "a");
    expect(heard).not.toHaveBeenCalled();
    needsFirst(board);
  });
});

describe("a saved arrangement on the board", () => {
  it("is laid out when it loads after the page, and again after a reset", async () => {
    const { board } = makeBoard();
    expect(layoutOf(board)).toEqual(packed(3));
    const rest = BOARD_IDS.filter((id) => id !== "azure");
    await loadBoardLayout(storageContext({ "3": [["azure"], rest, []] }).context);
    expect(layoutOf(board)).toEqual([["azure"], rest, []]);
    resetArrangement();
    expect(layoutOf(board)).toEqual(packed(3));
  });

  it("keeps each column count apart", () => {
    const { board, cards, attention } = makeBoard(3);
    setArrangement(3, [["azure"], ["compute", "credit"], []]);
    arrange(board, cards, 2, false, attention);
    expect(layoutOf(board)).toEqual(packed(2));
    arrange(board, cards, 4, false, attention);
    expect(layoutOf(board)).toEqual(packed(4));
  });
});

describe("Reset layout", () => {
  it("shows only while something is saved for the column count, and clears every count", () => {
    const { board, cards, attention } = makeBoard();
    const node = layoutResetButton(board);
    document.body.appendChild(node);
    expect(node.hidden).toBe(true);
    setArrangement(3, [["azure"], ["compute", "credit"], []]);
    expect(node.hidden).toBe(false);
    arrange(board, cards, 1, false, attention);
    expect(node.hidden).toBe(true);
    arrange(board, cards, 2, false, attention);
    expect(node.hidden).toBe(true);
    arrange(board, cards, 3, false, attention);
    expect(node.hidden).toBe(false);
    node.click();
    expect(hasArrangement()).toBe(false);
    expect(node.hidden).toBe(true);
    expect(layoutOf(board)).toEqual(packed(3));
  });
});

/** A pointer event carrying a pointer id, which jsdom's MouseEvent does not take. */
function firePointer(
  target: EventTarget,
  type: string,
  id: number,
  x = 0,
  y = 0,
  extra: object = {},
) {
  const event = new MouseEvent(type, {
    bubbles: true,
    cancelable: true,
    clientX: x,
    clientY: y,
    ...extra,
  });
  Object.defineProperty(event, "pointerId", { value: id });
  target.dispatchEvent(event);
}

const cardsIn = (board: HTMLElement) => [...board.querySelectorAll<HTMLElement>(".admin-card")];

describe("a drag that the board outlives", () => {
  it("is cancelled, not committed, when the columns are rebuilt under it", () => {
    const { board, cards, attention } = makeBoard();
    stubLayout(board);
    fire(grip(cards.deploys), "pointerdown", 20, 20, { button: 0 });
    fire(document, "pointermove", 700, 10);
    // A breakpoint: two columns replace three while the card is lifted.
    arrange(board, cards, 2, false, attention);
    expect(board.querySelector(".admin-slot")).toBeNull();
    expect(board.classList.contains("is-sorting")).toBe(false);
    expect(cards.deploys.classList.contains("is-dragging")).toBe(false);
    expect(cards.deploys.style.left).toBe("");
    fire(document, "pointerup", 700, 10);
    expect(hasArrangement()).toBe(false);
    expect(cardsIn(board)).toHaveLength(BOARD_IDS.length);
    expect(new Set(cardsIn(board)).size).toBe(BOARD_IDS.length);
    expect(layoutOf(board)).toEqual(packed(2));
  });

  it("goes on when the board only reflows at the same column count", () => {
    const { board, cards, attention } = makeBoard();
    stubLayout(board);
    fire(grip(cards.deploys), "pointerdown", 20, 20, { button: 0 });
    fire(document, "pointermove", 700, 10);
    const heard = vi.fn();
    board.addEventListener("admin-arranged", heard);
    arrange(board, cards, 3, false, attention);
    expect(heard).not.toHaveBeenCalled();
    expect(board.querySelector(".admin-slot")).not.toBeNull();
    expect(cards.deploys.classList.contains("is-dragging")).toBe(true);
    fire(document, "pointerup", 700, 10);
    expect(layoutOf(board)[2][0]).toBe("deploys");
    expect(hasArrangement(3)).toBe(true);
  });

  it("ignores an arrow key pressed during a pointer drag", () => {
    const { board, cards } = makeBoard();
    stubLayout(board);
    fire(grip(cards.deploys), "pointerdown", 20, 20, { button: 0 });
    grip(cards.deploys).dispatchEvent(
      new KeyboardEvent("keydown", { key: "ArrowRight", bubbles: true, cancelable: true }),
    );
    expect(hasArrangement()).toBe(false);
    expect(cards.deploys.classList.contains("is-dragging")).toBe(true);
    fire(document, "pointerup", 20, 20);
  });

  it("is cancelled when a saved arrangement arrives while the card is lifted", async () => {
    const { board, cards } = makeBoard();
    stubLayout(board);
    fire(grip(cards.deploys), "pointerdown", 20, 20, { button: 0 });
    fire(document, "pointermove", 700, 10);
    const rest = BOARD_IDS.filter((id) => id !== "azure");
    await loadBoardLayout(storageContext({ "3": [["azure"], rest, []] }).context);
    expect(board.querySelector(".admin-slot")).toBeNull();
    expect(cards.deploys.classList.contains("is-dragging")).toBe(false);
    fire(document, "pointerup", 700, 10);
    expect(layoutOf(board)[0]).toEqual(["azure"]);
    expect(cardsIn(board)).toHaveLength(BOARD_IDS.length);
  });

  it("is cancelled when the board is stopped", () => {
    const { board, cards, layout } = makeBoard();
    stubLayout(board);
    fire(grip(cards.deploys), "pointerdown", 20, 20, { button: 0 });
    layout.stop();
    expect(board.querySelector(".admin-slot")).toBeNull();
    expect(cards.deploys.classList.contains("is-dragging")).toBe(false);
    expect(board.classList.contains("is-sorting")).toBe(false);
    fire(document, "pointerup", 700, 10);
    expect(hasArrangement()).toBe(false);
  });
});

describe("one pointer at a time", () => {
  it("ignores another finger's move, drop and cancel, and a second grip pressed meanwhile", () => {
    const { board, cards } = makeBoard();
    stubLayout(board);
    const home = layoutOf(board).findIndex((col) => col.includes("deploys"));
    firePointer(grip(cards.deploys), "pointerdown", 1, 20, 20, { button: 0 });
    const slot = board.querySelector(".admin-slot") as HTMLElement;
    expect(slot.parentElement).toBe(board.children[home]);
    // A second finger moves over another column, and presses another grip.
    const other = home === 2 ? 1 : 2;
    firePointer(document, "pointermove", 2, other * 310 + 50, 10);
    expect(slot.parentElement).toBe(board.children[home]);
    firePointer(grip(cards.azure), "pointerdown", 2, 700, 20, { button: 0 });
    expect(board.querySelectorAll(".admin-slot")).toHaveLength(1);
    expect(cards.azure.classList.contains("is-dragging")).toBe(false);
    firePointer(document, "pointerup", 2, 700, 10);
    firePointer(document, "pointercancel", 2);
    expect(cards.deploys.classList.contains("is-dragging")).toBe(true);
    expect(hasArrangement()).toBe(false);
    // The first finger still drives it.
    firePointer(document, "pointermove", 1, other * 310 + 50, 10);
    expect(slot.parentElement).toBe(board.children[other]);
    firePointer(document, "pointerup", 1, other * 310 + 50, 10);
    expect(layoutOf(board)[other]).toContain("deploys");
  });
});

describe("a keyboard move is said aloud", () => {
  it("writes where the card went into a status region after the board", () => {
    const { board, cards } = makeBoard();
    const live = board.nextElementSibling as HTMLElement;
    expect(live.getAttribute("role")).toBe("status");
    expect(live.classList.contains("sr-only")).toBe(true);
    const id = layoutOf(board)[1][0];
    grip(cards[id]).dispatchEvent(
      new KeyboardEvent("keydown", { key: "ArrowLeft", bubbles: true, cancelable: true }),
    );
    expect(live.textContent).toBe(`${id} moved to column 1, position 1.`);
  });
});

describe("a change made before the stored arrangement arrives", () => {
  function slowStorage() {
    let release: (value: unknown) => void = () => {};
    const get = vi.fn(() => new Promise((done) => (release = done)));
    const set = vi.fn(async () => undefined);
    const context = { storage: { user: { get, set } } } as unknown as ExtensionContext;
    return { context, set, release: (value: unknown) => release(value) };
  }
  const mine = [["azure"], ["compute", "credit"], []] as CardId[][];

  it("wins over what the read returns, for its own column count only", async () => {
    const { context, set, release } = slowStorage();
    const loading = loadBoardLayout(context);
    setArrangement(3, mine);
    release({ "3": [["credit"], ["azure"], ["compute"]], "2": [["azure", "credit"], ["compute"]] });
    await loading;
    expect(packedArrangement(3, ["azure", "compute", "credit"])[0]).toEqual(["azure"]);
    expect(packedArrangement(2, ["azure", "compute", "credit"])[0]).toEqual(["azure", "credit"]);
    expect(set).toHaveBeenLastCalledWith(LAYOUT_KEY, expect.objectContaining({ "3": mine }));
  });

  it("is not undone by the read when it was a reset", async () => {
    const { context, release } = slowStorage();
    const loading = loadBoardLayout(context);
    resetArrangement();
    release({ "3": mine, "2": [["azure", "credit"], ["compute"]] });
    await loading;
    expect(hasArrangement()).toBe(false);
  });
});
