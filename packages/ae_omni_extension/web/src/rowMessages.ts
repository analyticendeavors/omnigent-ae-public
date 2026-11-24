// One message about a Needs you row (Open on GitHub's refusal with its copyable link). The table keeps it
// as state so a redraw of the same row shows it again; it goes at the next action on a row or after 30 seconds
// (2026-11-16: appended to the row itself it became a grid item in the checkbox column and each 20 second
// redraw wiped it).

import type { Panel } from "./attention";
import { flash, notice } from "./dom";

export const ROW_MESSAGE_MS = 30_000;

export interface RowMessage {
  id: string;
  line: HTMLElement;
}

export interface RowMessages {
  /** Show *line* for row *id* (null: the row's action started, so its old message goes). */
  set: (p: Panel, id: string, line: HTMLElement | null) => void;
  /** Forget the message without painting (its row left the list, so it must not move to the page line). */
  drop: () => void;
  /** The message to show now, if any. */
  current: () => RowMessage | null;
  stop: () => void;
}

/** *paint* draws the current message (and removes the one that left); *left* is the row id it was about. */
export function createRowMessages(
  paint: (p: Panel, now: RowMessage | null, left: string | null) => void,
  ms: number = ROW_MESSAGE_MS,
): RowMessages {
  let held: RowMessage | null = null;
  let timer: ReturnType<typeof setTimeout> | null = null;
  const stop = (): void => {
    if (timer) clearTimeout(timer);
    timer = null;
  };
  return {
    set: (p, id, line) => {
      const left = held ? held.id : null;
      stop();
      held = line ? { id, line } : null;
      if (held) {
        timer = setTimeout(() => {
          const gone = held?.id ?? null;
          held = null;
          timer = null;
          paint(p, null, gone);
        }, ms);
      }
      paint(p, held, left);
    },
    drop: () => {
      stop();
      held = null;
    },
    current: () => held,
    stop: () => {
      stop();
      held = null;
    },
  };
}

/**
 * A row action's result line (success or failure): the table shows it in the opened row's message line (or the
 * page's when the row is closed) and holds it 30 seconds; the tree, which has no message line, flashes it into
 * *fallback*.
 */
export function sayInRow(p: Panel, id: string, text: string, kind: "error" | "info" | "ok", fallback: HTMLElement): void {
  if (!p.table) {
    flash(fallback, text, kind);
    return;
  }
  const line = notice(text, kind);
  line.classList.add("flash");
  p.table.message(p, id, line);
}
