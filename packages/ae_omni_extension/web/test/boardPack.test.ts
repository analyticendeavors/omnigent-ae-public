// The Admin Center page on a phone and a desktop (2026-11-07): the two-up shelf, and the packed
// board (Needs you first in column 1, then each card into the shortest column by guessed height).
// jsdom does no layout, so heights are the guesses and the CSS is read as text (layout.test.ts).
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { adminCard, arrange, type CardId, columnsFor, pairShelf } from "../src/board";
import {
  guessHeight,
  NEEDS_YOU_HEIGHT_GUESS,
  packedArrangement,
  packLayout,
  resetBoardLayoutState,
  setArrangement,
} from "../src/boardLayout";

vi.mock("@omnigent/extension-sdk", () => ({ defineExtension: vi.fn() }));

const ALL: CardId[] = ["decision", "deploys", "accounts", "compute", "credit", "azure", "box", "home-security", "vault", "git", "presets", "social"];

function makeBoard(columns: 1 | 2 | 3 | 4, ids: CardId[] = ALL) {
  const main = document.createElement("main");
  const attention = document.createElement("section");
  attention.id = "needs-you";
  attention.innerHTML = '<button type="button" class="attn-review-source">Review</button>';
  const board = document.createElement("div");
  board.className = "admin-board";
  main.append(attention, board);
  document.body.appendChild(main);
  const cards = Object.fromEntries(
    ids.map((id) => [
      id,
      adminCard(id, id, document.createElementNS("http://www.w3.org/2000/svg", "svg"), [], "body"),
    ]),
  ) as Record<CardId, HTMLElement>;
  arrange(board, cards, columns, false, attention);
  return { board, cards, attention, main };
}

const fold = (card: HTMLElement, folded: boolean) => card.classList.toggle("is-folded", folded);
const halves = (board: HTMLElement) =>
  [...board.querySelectorAll<HTMLElement>(".admin-card.is-half")].map((c) => c.dataset.card);

beforeEach(() => {
  document.body.replaceChildren();
  resetBoardLayoutState();
});
afterEach(() => resetBoardLayoutState());

describe("the two-up shelf (phone)", () => {
  const stack = () => {
    const { board, cards } = makeBoard(1, ["compute", "credit", "azure", "vault"]);
    const cloud = board.querySelector(".admin-col") as HTMLElement;
    return { board, cards, cloud };
  };

  it("leaves a lone folded card at full width", () => {
    const { board, cards } = stack();
    for (const card of Object.values(cards)) fold(card, false);
    fold(cards.credit, true);
    pairShelf(board);
    expect(halves(board)).toEqual([]);
  });

  it("pairs a run of two", () => {
    const { board, cards } = stack();
    for (const card of Object.values(cards)) fold(card, false);
    fold(cards.compute, true);
    fold(cards.credit, true);
    pairShelf(board);
    expect(halves(board)).toEqual(["compute", "credit"]);
  });

  it("pairs the first two of a run of three and leaves the odd last card full width", () => {
    const { board, cards } = stack();
    for (const card of Object.values(cards)) fold(card, false);
    fold(cards.compute, true);
    fold(cards.credit, true);
    fold(cards.azure, true);
    pairShelf(board);
    expect(halves(board)).toEqual(["compute", "credit"]);
  });

  it("starts a new run after an open card, and clears the pairs when a card opens", () => {
    const { board, cards } = stack();
    for (const card of Object.values(cards)) fold(card, true);
    fold(cards.credit, false);
    pairShelf(board);
    // compute is alone before the open card; azure and vault pair after it.
    expect(halves(board)).toEqual(["azure", "vault"]);
    fold(cards.credit, true);
    pairShelf(board);
    expect(halves(board)).toEqual(["compute", "credit", "azure", "vault"]);
  });

  it("marks nothing on two or three columns", () => {
    const { board, cards } = makeBoard(2);
    for (const card of Object.values(cards)) fold(card, true);
    pairShelf(board);
    expect(halves(board)).toEqual([]);
  });

  it("pairs through arrange itself, from each card's remembered fold", () => {
    const { board } = makeBoard(1);
    // Every card but Deploys starts folded unless it needs attention: a phone opens as pairs.
    expect(halves(board).length).toBeGreaterThan(0);
    expect(halves(board).length % 2).toBe(0);
  });
});

const ids = (col: Element): string[] =>
  [...col.children].map((c) => (c as HTMLElement).id || (c as HTMLElement).dataset.card || "");
const colsOf = (board: HTMLElement) => [...board.querySelectorAll(":scope > .admin-col")].map(ids);

describe("columnsFor", () => {
  it("starts a fourth column at 1,720 px", () => {
    expect([519, 520, 1039, 1040, 1719, 1720].map(columnsFor)).toEqual([1, 2, 2, 3, 3, 4]);
  });
});

describe("the packed board (2 or more columns)", () => {
  it("draws no group sections: n columns, Needs you first in column 1, then the footer", () => {
    for (const n of [2, 3, 4] as const) {
      const { board } = makeBoard(n);
      expect(board.dataset.packed).toBe("");
      expect(board.querySelector(".admin-group")).toBeNull();
      expect([...board.children].map((c) => c.className)).toEqual([
        ...Array.from({ length: n }, () => "admin-col"),
        "admin-notset",
      ]);
      expect(ids(board.children[0])[0]).toBe("needs-you");
    }
  });

  it("puts each card, in Ship, Box, Cloud, Accounts order, into the shortest column by guess (ties left)", () => {
    const { board } = makeBoard(4);
    expect(colsOf(board)).toEqual([
      ["needs-you", "home-security", "azure"],
      ["deploys", "box", "vault"],
      ["git", "compute", "accounts"],
      ["social", "credit", "decision", "presets"],
    ]);
  });

  it("packs three columns the same way, and always draws every column even with few cards", () => {
    const three = makeBoard(3);
    expect(colsOf(three.board)[0][0]).toBe("needs-you");
    expect(colsOf(three.board).flat().filter((x) => x !== "needs-you").sort()).toEqual([...ALL].sort());
    const few = makeBoard(4, ["deploys"]);
    expect(colsOf(few.board)).toEqual([["needs-you"], ["deploys"], [], []]);
  });

  it("counts Needs you as its guess when it packs", () => {
    expect(NEEDS_YOU_HEIGHT_GUESS).toBeGreaterThan(0);
    expect(packLayout(2, ["deploys"])).toEqual([[], ["deploys"]]);
    expect(guessHeight("deploys")).toBeLessThan(NEEDS_YOU_HEIGHT_GUESS);
  });

  it("gives each card a group tag and a data-group", () => {
    const { board } = makeBoard(2);
    const tag = (id: string) =>
      board.querySelector(`.admin-card[data-card="${id}"] > .admin-card-tag`)?.textContent;
    expect([tag("deploys"), tag("box"), tag("azure"), tag("accounts")]).toEqual([
      "Ship",
      "Box",
      "Cloud",
      "Accounts",
    ]);
    expect((board.querySelector('[data-card="azure"]') as HTMLElement).dataset.group).toBe("cloud");
  });

  it("places the same cards in the same columns whatever their rendered heights", () => {
    const place = (height: number) => {
      const { board, cards, attention } = makeBoard(3);
      for (const card of Object.values(cards)) Object.defineProperty(card, "offsetHeight", { value: height, configurable: true });
      arrange(board, cards, 3, true, attention);
      return colsOf(board);
    };
    expect(place(40)).toEqual(place(900));
  });

  it("keeps a phone's group sections and puts Needs you right before the board", () => {
    const { board, attention, main } = makeBoard(1);
    expect(board.dataset.packed).toBeUndefined();
    expect([...board.querySelectorAll(".admin-group")].map((g) => (g as HTMLElement).dataset.group)).toEqual([
      "ship",
      "box",
      "cloud",
      "money",
    ]);
    expect(attention.nextElementSibling).toBe(board);
    expect(attention.parentElement).toBe(main);
  });

  it("moves Needs you in and out of column 1 across the phone breakpoint and keeps focus on its button", () => {
    const { board, cards, attention, main } = makeBoard(2);
    const review = attention.querySelector("button") as HTMLButtonElement;
    expect(attention.parentElement?.parentElement).toBe(board);
    review.focus();
    arrange(board, cards, 1, false, attention);
    expect(attention.parentElement).toBe(main);
    expect(attention.nextElementSibling).toBe(board);
    expect(document.activeElement).toBe(review);
    arrange(board, cards, 3, false, attention);
    expect(ids(board.children[0])[0]).toBe("needs-you");
    expect(document.activeElement).toBe(review);
    expect(board.dataset.packed).toBe("");
  });
});

describe("a saved arrangement per column count", () => {
  const known: CardId[] = ["deploys", "git", "social", "box"];

  it("is used when it has n columns, and mended: unknown ids dropped, missing cards placed by guess", () => {
    setArrangement(3, [["git", "gone"], ["deploys", "git"], []] as unknown as CardId[][]);
    // social and box are missing, placed in board order: social (430) to the empty column 3,
    // then box to column 2 (160 against 430 and 910).
    expect(packedArrangement(3, known)).toEqual([["git"], ["deploys", "box"], ["social"]]);
  });

  it("falls back to the pack when the saved count of columns is wrong or nothing in it is known", () => {
    setArrangement(2, [["a"], ["b"], ["c"]] as never);
    expect(packedArrangement(2, known)).toEqual(packLayout(2, known));
    setArrangement(4, [["nope"], [], [], []] as never);
    expect(packedArrangement(4, known)).toEqual(packLayout(4, known));
  });

  it("is kept per column count: a 3-column save does not change 2 or 4", () => {
    setArrangement(3, [["social"], ["git"], ["deploys", "box"]]);
    expect(packedArrangement(2, known)).toEqual(packLayout(2, known));
    expect(packedArrangement(4, known)).toEqual(packLayout(4, known));
    expect(packedArrangement(3, known)).toEqual([["social"], ["git"], ["deploys", "box"]]);
  });
});
