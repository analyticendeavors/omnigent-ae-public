// Dragging an Admin Center card to another place (2026-10-22, boardLayout.ts
// keeps the result).
//
// The grip in a card's header starts a drag on two or more columns. The card
// lifts out of the flow (position: fixed, under the pointer) and a dashed slot
// the card's size takes its place; the pointer picks the column and the place
// in it, and the slot moves there, so the other cards slide to make room. A
// drop saves the arrangement; Escape or a cancelled pointer puts nothing
// anywhere. The coordinates are the pointer's, never `elementsFromPoint`, so
// the lifted card does not need to be out of the way of anything.
//
// One pointer drives a drag: moves, drops and cancels from any other finger
// are ignored, and a second grip pressed meanwhile starts nothing. A drag that
// the board outlives is cancelled, never committed: a breakpoint or a saved
// layout arriving mid-drag rebuilds the columns around the cards, which would
// otherwise drop the card into a detached column and save a layout under the
// wrong column count. A board that only reflows (a scrollbar narrowing the
// page) keeps its columns and fires no "admin-arranged" event, so the drag goes
// on, and the lifted card takes the width of its slot. Stopping the board
// cancels it too.
//
// 2026-11-07 (packed board): a card moves anywhere on the board (board.ts), so the drop columns
// are `.admin-board[data-packed] > .admin-col` and the layout a drop saves is the whole board's
// for this column count. Needs you has no grip, never moves, and nothing drops above it.
//
// The grip also takes the arrow keys (up and down in the column, left and
// right to the next column), so the page can be arranged without a mouse; each
// move is said in a visually hidden live region after the board.
// Events are delegated from the board, which stays put while its columns are
// rebuilt around the cards, and the drag listens on the document with the
// pointer captured by the grip, so a fast drag out of the frame still ends.

import type { CardId, CardSet } from "./board";
import {
  type Direction,
  dropTarget,
  hasArrangement,
  type Layout,
  moveCard,
  type Packed,
  resetArrangement,
  setArrangement,
} from "./boardLayout";
import { button } from "./dom";

const KEYS: Record<string, Direction> = {
  ArrowUp: "up",
  ArrowDown: "down",
  ArrowLeft: "left",
  ArrowRight: "right",
};

const isPacked = (board: HTMLElement): boolean => board.dataset.packed !== undefined;

/** The board's columns: the `.admin-col` stacks of a packed board. */
const columnsOf = (board: HTMLElement): HTMLElement[] =>
  [...board.children].filter((col): col is HTMLElement => col.classList.contains("admin-col"));

/** The cards in each column of the packed *board* as they sit in the page now (Needs you is no card). */
export function currentLayout(board: HTMLElement): Layout {
  return columnsOf(board).map((col) =>
    [...col.children].flatMap((card) => {
      const id = (card as HTMLElement).dataset.card;
      return id ? [id as CardId] : [];
    }),
  );
}

/** Whether *child* is the Needs you tile, which no card drops above. */
const isNeedsYou = (child: Element): boolean => child.id === "needs-you";

const arranged = (board: HTMLElement): Packed | null => {
  const n = Number(board.dataset.columns);
  return isPacked(board) && (n === 2 || n === 3 || n === 4) ? n : null;
};

function begin(
  board: HTMLElement,
  n: Packed,
  card: HTMLElement,
  grip: HTMLElement,
  start: PointerEvent,
  onDone: () => void,
): () => void {
  const doc = board.ownerDocument;
  const box = card.getBoundingClientRect();
  const offsetX = start.clientX - box.left;
  const offsetY = start.clientY - box.top;
  const slot = doc.createElement("div");
  slot.className = "admin-slot";
  slot.setAttribute("aria-hidden", "true");
  slot.style.height = `${box.height}px`;
  card.before(slot);
  card.classList.add("is-dragging");
  card.style.width = `${box.width}px`;
  card.style.left = `${box.left}px`;
  card.style.top = `${box.top}px`;
  board.classList.add("is-sorting");
  try {
    grip.setPointerCapture(start.pointerId);
  } catch {
    // No capture (a test, or a pointer that already ended): the document still hears the drag.
  }
  // Pressing a grip is not a focus change (pointerdown is prevented), and in the page's
  // frame the keyboard stays with the parent window, so Escape would never reach the drag.
  // Focusing the grip moves the keyboard into this frame for the length of the drag.
  grip.focus({ preventScroll: true });

  const move = (event: PointerEvent) => {
    if (event.pointerId !== start.pointerId) return;
    card.style.left = `${event.clientX - offsetX}px`;
    card.style.top = `${event.clientY - offsetY}px`;
    // The slot sits in a column, so its width follows a reflow the drag outlived.
    const slotWidth = slot.getBoundingClientRect().width;
    if (slotWidth > 0) card.style.width = `${slotWidth}px`;
    const columns = columnsOf(board);
    const boxes = columns.map((col) => {
      const rect = col.getBoundingClientRect();
      return {
        left: rect.left,
        right: rect.right,
        cards: [...col.children]
          .filter((child) => child !== card && child !== slot && !isNeedsYou(child))
          .map((child) => {
            const r = child.getBoundingClientRect();
            return { top: r.top, bottom: r.bottom };
          }),
      };
    });
    const { column, index } = dropTarget(boxes, event.clientX, event.clientY);
    // Index 0 of column 1 is just under Needs you, never above it.
    const others = [...columns[column].children].filter(
      (child) => child !== card && child !== slot && !isNeedsYou(child),
    );
    const before = others[index] ?? null;
    const target = columns[column];
    const placed =
      slot.parentElement === target &&
      (before
        ? slot.nextElementSibling === before
        : [...target.children].filter((child) => child !== card).pop() === slot);
    if (!placed) target.insertBefore(slot, before);
  };

  let done = false;
  const finish = (commit: boolean) => {
    if (done) return;
    done = true;
    // First, so the layout the drop saves cannot cancel this drag again.
    onDone();
    doc.removeEventListener("pointermove", move);
    doc.removeEventListener("pointerup", up);
    doc.removeEventListener("pointercancel", cancel);
    doc.removeEventListener("keydown", escape, true);
    try {
      grip.releasePointerCapture(start.pointerId);
    } catch {
      // Already released.
    }
    // The columns the slot sat in may have been replaced mid-drag (see the header).
    const save = commit && slot.isConnected && board.contains(slot) && board.dataset.columns === String(n);
    if (save) slot.replaceWith(card);
    else slot.remove();
    card.classList.remove("is-dragging");
    card.style.width = "";
    card.style.left = "";
    card.style.top = "";
    board.classList.remove("is-sorting");
    if (save) setArrangement(n, currentLayout(board));
  };
  const up = (event: PointerEvent) => {
    if (event.pointerId === start.pointerId) finish(true);
  };
  const cancel = (event: PointerEvent) => {
    if (event.pointerId === start.pointerId) finish(false);
  };
  const escape = (event: KeyboardEvent) => {
    if (event.key !== "Escape") return;
    event.preventDefault();
    finish(false);
  };
  doc.addEventListener("pointermove", move);
  doc.addEventListener("pointerup", up);
  doc.addEventListener("pointercancel", cancel);
  doc.addEventListener("keydown", escape, true);
  return () => finish(false);
}

/** Make the cards of *board* draggable by their grips; the returned function undoes it. */
export function enableDrag(board: HTMLElement, cards: CardSet): () => void {
  let cancelActive: (() => void) | null = null;
  const live = board.ownerDocument.createElement("div");
  live.className = "sr-only";
  live.setAttribute("role", "status");
  board.after(live);
  const onDown = (event: Event) => {
    const down = event as PointerEvent;
    const n = arranged(board);
    if (cancelActive) return;
    const grip = (down.target as Element | null)?.closest?.(".admin-grip");
    if (!n || !grip || down.button > 0) return;
    const card = grip.closest<HTMLElement>(".admin-card");
    if (!card || !board.contains(card)) return;
    down.preventDefault();
    cancelActive = begin(board, n, card, grip as HTMLElement, down, () => {
      cancelActive = null;
    });
  };
  const onKey = (event: Event) => {
    // A pointer drag owns the board; a keyboard move would save the pre-drag layout.
    if (cancelActive) return;
    const key = event as KeyboardEvent;
    const n = arranged(board);
    const grip = (key.target as Element | null)?.closest?.(".admin-grip");
    const direction = KEYS[key.key];
    if (!n || !grip || !direction) return;
    key.preventDefault();
    const id = grip.closest<HTMLElement>(".admin-card")?.dataset.card as CardId | undefined;
    if (!id) return;
    const next = moveCard(currentLayout(board), id, direction);
    if (!next) return;
    setArrangement(n, next);
    // The board was laid out again around the same cards; the grip lost its focus.
    cards[id]?.querySelector<HTMLElement>(".admin-grip")?.focus({ preventScroll: true });
    const placed = currentLayout(board);
    const column = placed.findIndex((ids) => ids.includes(id));
    const title = cards[id]?.querySelector(".admin-card-title")?.textContent ?? id;
    live.textContent = `${title} moved to column ${column + 1}, position ${placed[column].indexOf(id) + 1}.`;
  };
  const onArranged = () => cancelActive?.();
  board.addEventListener("pointerdown", onDown);
  board.addEventListener("keydown", onKey);
  board.addEventListener("admin-arranged", onArranged);
  return () => {
    cancelActive?.();
    board.removeEventListener("pointerdown", onDown);
    board.removeEventListener("keydown", onKey);
    board.removeEventListener("admin-arranged", onArranged);
    live.remove();
  };
}

/**
 * The page header's Reset layout button: shown only while the current column
 * count has a saved arrangement (one saved at another width is not on screen).
 * Pressing it clears every width, as its title says.
 */
export function layoutResetButton(board: HTMLElement): HTMLButtonElement {
  const node = button("Reset layout", {
    title: "Put the cards back in their default places, at every width",
    onClick: () => resetArrangement(),
  });
  node.classList.add("admin-reset");
  const update = () => {
    const n = arranged(board);
    node.hidden = n === null || !hasArrangement(n);
  };
  update();
  board.addEventListener("admin-arranged", update);
  return node;
}
