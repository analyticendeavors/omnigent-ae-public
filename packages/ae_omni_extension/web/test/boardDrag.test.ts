// The Admin Center's card arrangement (2026-10-22): the saved layout read
// defensively, the pure steps that move a card, the drag and its keyboard twin,
// and the Reset layout button. jsdom does no layout, so a drag is driven with
// stubbed rectangles.
import type { ExtensionContext } from "@omnigent/extension-sdk";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { adminCard, arrange, type CardId, LAYOUTS, mountBoard } from "../src/board";
import { currentLayout, layoutResetButton } from "../src/boardDrag";
import {
  arrangement,
  dropTarget,
  hasArrangement,
  LAYOUT_KEY,
  loadBoardLayout,
  moveCard,
  onLayoutChange,
  resetArrangement,
  resetBoardLayoutState,
  setArrangement,
} from "../src/boardLayout";

const sdk = vi.hoisted(() => ({ defineExtension: vi.fn() }));
vi.mock("@omnigent/extension-sdk", () => sdk);

const IDS: CardId[] = ["deploys", "accounts", "compute", "credit", "azure", "box"];
/** The default columns as the board draws them from IDS (the fixture has no Key Vault card). */
const shown = (n: 2 | 3) => LAYOUTS[n].map((col) => col.filter((id) => IDS.includes(id)));

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
  it("is the default until something is saved, and a phone is never arranged", () => {
    expect(arrangement(3, LAYOUTS, IDS)).toEqual(LAYOUTS[3]);
    setArrangement(3, [["box"], ["deploys", "accounts", "compute"], ["azure", "credit"]]);
    expect(arrangement(1, LAYOUTS, IDS)).toEqual(LAYOUTS[1]);
    // Saved for three columns only: two columns keep their default.
    expect(arrangement(2, LAYOUTS, IDS)).toEqual(LAYOUTS[2]);
    expect(arrangement(3, LAYOUTS, IDS)[0]).toEqual(["box"]);
  });

  it("drops a card the page no longer has, a repeat, and puts a missing card in its default column", () => {
    setArrangement(3, [
      ["deploys", "gone", "deploys"],
      ["accounts", "compute"],
      ["azure"],
    ] as unknown as CardId[][]);
    expect(arrangement(3, LAYOUTS, IDS)).toEqual([
      // box defaults to the first column, credit to the third.
      ["deploys", "box"],
      ["accounts", "compute"],
      ["azure", "credit"],
    ]);
  });

  it("ignores an arrangement of the wrong shape or with no known card", () => {
    setArrangement(3, [["deploys"], ["accounts"]]);
    expect(arrangement(3, LAYOUTS, IDS)).toEqual(LAYOUTS[3]);
    setArrangement(3, [["x"], ["y"], ["z"]] as unknown as CardId[][]);
    expect(arrangement(3, LAYOUTS, IDS)).toEqual(LAYOUTS[3]);
  });

  it("is read from storage, and a bad value is ignored", async () => {
    const good = { "3": [["box"], ["deploys"], ["azure"]] };
    const { context } = storageContext({ ...good, "2": "nope" });
    await loadBoardLayout(context);
    expect(hasArrangement()).toBe(true);
    expect(arrangement(3, LAYOUTS, IDS)[0]).toEqual(["box"]);
    resetBoardLayoutState();
    await loadBoardLayout(storageContext([1, 2]).context);
    expect(hasArrangement()).toBe(false);
    resetBoardLayoutState();
    const broken = { storage: { user: { get: () => Promise.reject(new Error("no")) } } };
    await loadBoardLayout(broken as unknown as ExtensionContext);
    expect(hasArrangement()).toBe(false);
  });

  it("saves under one key, per column count, and tells listeners", async () => {
    const { context, set } = storageContext(undefined);
    await loadBoardLayout(context);
    const heard = vi.fn();
    onLayoutChange(heard);
    setArrangement(2, [["box"], ["deploys"]]);
    expect(set).toHaveBeenCalledWith(LAYOUT_KEY, { "2": [["box"], ["deploys"]] });
    resetArrangement();
    expect(set).toHaveBeenLastCalledWith(LAYOUT_KEY, {});
    expect(heard).toHaveBeenCalledTimes(2);
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

function makeBoard(n: 2 | 3 = 3) {
  const board = document.createElement("div");
  board.className = "admin-board";
  document.body.appendChild(board);
  const cards = Object.fromEntries(
    IDS.map((id) => [
      id,
      adminCard(id, id, document.createElementNS("http://www.w3.org/2000/svg", "svg"), [], "body"),
    ]),
  ) as Record<CardId, HTMLElement>;
  const layout = mountBoardAt(board, cards, n);
  return { board, cards, layout };
}

function mountBoardAt(board: HTMLElement, cards: Record<CardId, HTMLElement>, n: 2 | 3) {
  const page = document.createElement("div");
  Object.defineProperty(page, "clientWidth", { value: n === 3 ? 1400 : 900 });
  return mountBoard(page, board, cards);
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

/** Give the current columns and cards fixed boxes: 300 wide columns, 100 high cards, 10 apart. */
function stubLayout(board: HTMLElement) {
  [...board.children].forEach((col, c) => {
    stubRect(col, c * 310, 0, 300, 600);
    [...col.children].forEach((card, r) => stubRect(card, c * 310, r * 110, 300, 100));
  });
}

const fire = (target: EventTarget, type: string, x = 0, y = 0, extra: object = {}) =>
  target.dispatchEvent(
    new MouseEvent(type, { bubbles: true, cancelable: true, clientX: x, clientY: y, ...extra }),
  );

describe("the grips", () => {
  it("sit in every card's header, are named for their card, and are hidden on one column by style", () => {
    const { cards } = makeBoard();
    for (const id of IDS) {
      const found = grip(cards[id]);
      expect(found.closest(".admin-card-top")).not.toBeNull();
      expect(found.getAttribute("aria-label")).toContain(`Move ${id}`);
    }
  });
});

describe("dragging", () => {
  it("lifts the card, moves a dashed slot with the pointer, and saves the drop", () => {
    const { board, cards } = makeBoard();
    stubLayout(board);
    const heard = vi.fn();
    onLayoutChange(heard);
    fire(grip(cards.deploys), "pointerdown", 20, 20, { button: 0 });
    expect(cards.deploys.classList.contains("is-dragging")).toBe(true);
    expect(board.classList.contains("is-sorting")).toBe(true);
    expect(board.querySelector(".admin-slot")).not.toBeNull();
    // Over the third column, above its first card: that is where the slot goes.
    fire(document, "pointermove", 700, 10);
    const slot = board.querySelector(".admin-slot") as HTMLElement;
    expect(slot.parentElement).toBe(board.children[2]);
    expect(slot.nextElementSibling).toBe(cards.azure);
    expect(cards.deploys.style.left).toBe("680px");
    fire(document, "pointerup", 700, 10);
    expect(currentLayout(board)).toEqual([
      ["box"],
      ["accounts", "compute"],
      ["deploys", "azure", "credit"],
    ]);
    expect(arrangement(3, LAYOUTS, IDS)[2]).toEqual(["deploys", "azure", "credit"]);
    expect(heard).toHaveBeenCalledTimes(1);
    expect(board.querySelector(".admin-slot")).toBeNull();
    expect(board.classList.contains("is-sorting")).toBe(false);
    expect(cards.deploys.classList.contains("is-dragging")).toBe(false);
    expect(cards.deploys.style.left).toBe("");
  });

  it("drops into the middle of a column by the cards' middles", () => {
    const { board, cards } = makeBoard();
    stubLayout(board);
    fire(grip(cards.box), "pointerdown", 20, 120, { button: 0 });
    // Column two holds accounts (0 to 100) and compute (110 to 210); y 150 is past accounts' middle only.
    fire(document, "pointermove", 400, 150);
    fire(document, "pointerup", 400, 150);
    expect(currentLayout(board)).toEqual([
      ["deploys"],
      ["accounts", "box", "compute"],
      ["azure", "credit"],
    ]);
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
    fire(grip(cards.deploys), "pointerdown", 20, 20, { button: 0 });
    fire(document, "pointermove", 700, 10);
    document.dispatchEvent(
      new KeyboardEvent("keydown", { key: "Escape", bubbles: true, cancelable: true }),
    );
    expect(currentLayout(board)).toEqual(shown(3));
    expect(hasArrangement()).toBe(false);
    expect(board.querySelector(".admin-slot")).toBeNull();
    expect(cards.deploys.classList.contains("is-dragging")).toBe(false);
    fire(grip(cards.deploys), "pointerdown", 20, 20, { button: 0 });
    fire(document, "pointercancel");
    expect(hasArrangement()).toBe(false);
    expect(board.querySelector(".admin-slot")).toBeNull();
    // After a finished drag the document no longer follows the pointer.
    fire(document, "pointermove", 700, 10);
    expect(cards.deploys.style.left).toBe("");
  });

  it("ignores a secondary button, a press off the grip, and a one column board", () => {
    const { board, cards } = makeBoard();
    stubLayout(board);
    fire(grip(cards.deploys), "pointerdown", 20, 20, { button: 2 });
    expect(board.querySelector(".admin-slot")).toBeNull();
    fire(cards.deploys.querySelector(".admin-card-toggle") as Element, "pointerdown", 20, 20, {
      button: 0,
    });
    expect(board.querySelector(".admin-slot")).toBeNull();
    arrange(board, cards, 1);
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

  it("moves a card with the arrow keys, keeps the focus on its grip, and saves", () => {
    const { board, cards } = makeBoard();
    const first = grip(cards.deploys);
    first.focus();
    press(first, "ArrowDown");
    expect(currentLayout(board)[0]).toEqual(["box", "deploys"]);
    expect(document.activeElement).toBe(grip(cards.deploys));
    press(grip(cards.deploys), "ArrowRight");
    expect(currentLayout(board)).toEqual([
      ["box"],
      ["accounts", "deploys", "compute"],
      ["azure", "credit"],
    ]);
    expect(arrangement(3, LAYOUTS, IDS)[0]).toEqual(["box"]);
    // A move that goes nowhere changes nothing and is not saved again.
    const heard = vi.fn();
    onLayoutChange(heard);
    press(grip(cards.azure), "ArrowRight");
    expect(heard).not.toHaveBeenCalled();
    press(grip(cards.azure), "a");
    expect(heard).not.toHaveBeenCalled();
  });
});

describe("a saved arrangement on the board", () => {
  it("is laid out when it loads after the page, and again after a reset", async () => {
    const { board } = makeBoard();
    expect(currentLayout(board)).toEqual(shown(3));
    const saved = { "3": [["box"], ["deploys", "accounts", "compute"], ["azure", "credit"]] };
    await loadBoardLayout(storageContext(saved).context);
    expect(currentLayout(board)[0]).toEqual(["box"]);
    resetArrangement();
    expect(currentLayout(board)).toEqual(shown(3));
  });

  it("keeps two and three columns apart", () => {
    const { board, cards } = makeBoard(3);
    setArrangement(3, [["box"], ["deploys", "accounts", "compute"], ["azure", "credit"]]);
    arrange(board, cards, 2);
    expect(currentLayout(board)).toEqual(shown(2));
  });
});

describe("Reset layout", () => {
  it("shows only while something is saved and the board has columns", () => {
    const { board, cards } = makeBoard();
    const node = layoutResetButton(board);
    document.body.appendChild(node);
    expect(node.hidden).toBe(true);
    setArrangement(3, [["box"], ["deploys", "accounts", "compute"], ["azure", "credit"]]);
    expect(node.hidden).toBe(false);
    arrange(board, cards, 1);
    expect(node.hidden).toBe(true);
    arrange(board, cards, 3);
    expect(node.hidden).toBe(false);
    node.click();
    expect(hasArrangement()).toBe(false);
    expect(node.hidden).toBe(true);
    expect(currentLayout(board)).toEqual(shown(3));
  });

  it("shows only for a column count that has its own arrangement", () => {
    const { board, cards } = makeBoard();
    const node = layoutResetButton(board);
    document.body.appendChild(node);
    setArrangement(3, [["box"], ["deploys", "accounts", "compute"], ["azure", "credit"]]);
    arrange(board, cards, 2);
    expect(node.hidden).toBe(true);
    arrange(board, cards, 3);
    expect(node.hidden).toBe(false);
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
    const { board, cards } = makeBoard();
    stubLayout(board);
    fire(grip(cards.deploys), "pointerdown", 20, 20, { button: 0 });
    fire(document, "pointermove", 700, 10);
    // A breakpoint: two columns replace three while the card is lifted.
    arrange(board, cards, 2);
    expect(board.querySelector(".admin-slot")).toBeNull();
    expect(board.classList.contains("is-sorting")).toBe(false);
    expect(cards.deploys.classList.contains("is-dragging")).toBe(false);
    expect(cards.deploys.style.left).toBe("");
    fire(document, "pointerup", 700, 10);
    expect(hasArrangement()).toBe(false);
    // Every card is on the board exactly once.
    expect(cardsIn(board)).toHaveLength(6);
    expect(new Set(cardsIn(board)).size).toBe(6);
    expect(currentLayout(board)).toEqual(shown(2));
  });

  it("goes on when the board only reflows at the same column count", () => {
    const { board, cards } = makeBoard();
    stubLayout(board);
    fire(grip(cards.deploys), "pointerdown", 20, 20, { button: 0 });
    fire(document, "pointermove", 700, 10);
    // A scrollbar narrowing the page: nothing is rebuilt, so no event fires.
    const heard = vi.fn();
    board.addEventListener("admin-arranged", heard);
    arrange(board, cards, 3);
    expect(heard).not.toHaveBeenCalled();
    expect(board.querySelector(".admin-slot")).not.toBeNull();
    expect(cards.deploys.classList.contains("is-dragging")).toBe(true);
    fire(document, "pointerup", 700, 10);
    expect(arrangement(3, LAYOUTS, IDS)[2]).toEqual(["deploys", "azure", "credit"]);
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
    const saved = { "3": [["box"], ["deploys", "accounts", "compute"], ["azure", "credit"]] };
    await loadBoardLayout(storageContext(saved).context);
    expect(board.querySelector(".admin-slot")).toBeNull();
    expect(cards.deploys.classList.contains("is-dragging")).toBe(false);
    fire(document, "pointerup", 700, 10);
    expect(currentLayout(board)[0]).toEqual(["box"]);
    expect(cardsIn(board)).toHaveLength(6);
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
    firePointer(grip(cards.deploys), "pointerdown", 1, 20, 20, { button: 0 });
    const slot = board.querySelector(".admin-slot") as HTMLElement;
    expect(slot.parentElement).toBe(board.children[0]);
    // A second finger moves over the third column, and presses another grip.
    firePointer(document, "pointermove", 2, 700, 10);
    expect(slot.parentElement).toBe(board.children[0]);
    firePointer(grip(cards.azure), "pointerdown", 2, 700, 20, { button: 0 });
    expect(board.querySelectorAll(".admin-slot")).toHaveLength(1);
    expect(cards.azure.classList.contains("is-dragging")).toBe(false);
    // It lifts and drops without ending the first finger's drag.
    firePointer(document, "pointerup", 2, 700, 10);
    firePointer(document, "pointercancel", 2);
    expect(cards.deploys.classList.contains("is-dragging")).toBe(true);
    expect(hasArrangement()).toBe(false);
    // The first finger still drives it.
    firePointer(document, "pointermove", 1, 700, 10);
    expect(slot.parentElement).toBe(board.children[2]);
    firePointer(document, "pointerup", 1, 700, 10);
    expect(currentLayout(board)[2]).toEqual(["deploys", "azure", "credit"]);
  });
});

describe("a keyboard move is said aloud", () => {
  it("writes where the card went into a status region after the board", () => {
    const { board, cards } = makeBoard();
    const live = board.nextElementSibling as HTMLElement;
    expect(live.getAttribute("role")).toBe("status");
    expect(live.classList.contains("sr-only")).toBe(true);
    grip(cards.deploys).dispatchEvent(
      new KeyboardEvent("keydown", { key: "ArrowDown", bubbles: true, cancelable: true }),
    );
    expect(live.textContent).toBe("deploys moved to column 1, position 2.");
    grip(cards.deploys).dispatchEvent(
      new KeyboardEvent("keydown", { key: "ArrowRight", bubbles: true, cancelable: true }),
    );
    expect(live.textContent).toBe("deploys moved to column 2, position 2.");
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
  const mine = [["box"], ["deploys", "accounts", "compute"], ["azure", "credit"]] as CardId[][];

  it("wins over what the read returns, for its own column count only", async () => {
    const { context, set, release } = slowStorage();
    const loading = loadBoardLayout(context);
    setArrangement(3, mine);
    release({
      "3": [["credit"], ["accounts", "compute"], ["box", "deploys", "azure"]],
      "2": [["box"], ["deploys"]],
    });
    await loading;
    expect(arrangement(3, LAYOUTS, IDS)[0]).toEqual(["box"]);
    // Two columns were not touched, so the stored arrangement applies there.
    expect(arrangement(2, LAYOUTS, IDS)[0][0]).toBe("box");
    expect(set).toHaveBeenLastCalledWith(LAYOUT_KEY, expect.objectContaining({ "3": mine }));
  });

  it("is not undone by the read when it was a reset", async () => {
    const { context, release } = slowStorage();
    const loading = loadBoardLayout(context);
    resetArrangement();
    release({ "3": mine, "2": [["box"], ["deploys"]] });
    await loading;
    expect(hasArrangement()).toBe(false);
  });
});
