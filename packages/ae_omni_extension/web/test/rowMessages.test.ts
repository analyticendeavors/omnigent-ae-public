// sayInRow (rowMessages.ts): the table holds a row's result as state; the tree has no message line, so it
// flashes the result into the node it was given.
import { describe, expect, it, vi } from "vitest";
import type { Panel } from "../src/attention";
import { sayInRow } from "../src/rowMessages";

describe("sayInRow", () => {
  it("flashes into the fallback node when the panel has no table (the tree)", () => {
    const host = document.createElement("div");
    sayInRow({} as Panel, "row-1", "Could not snooze (down).", "error", host);
    expect(host.querySelector(".flash")?.textContent).toContain("Could not snooze (down).");
  });

  it("hands a flash-styled line to the table, and leaves the fallback node alone", () => {
    const host = document.createElement("div");
    const message = vi.fn();
    sayInRow({ table: { message } } as unknown as Panel, "row-1", "Assigned.", "ok", host);
    expect(host.childElementCount).toBe(0);
    const [, id, line] = message.mock.calls[0] as [unknown, string, HTMLElement];
    expect(id).toBe("row-1");
    expect(line.textContent).toBe("Assigned.");
    expect(line.classList.contains("flash")).toBe(true);
  });
});
