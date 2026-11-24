// The health rail and the narrow-page chips (2026-11-14, concept D2; rail.ts): the area counts by the
// page's one rule, the case where an area holds a source that could not be read, and which headers
// link to a tab. The tile store's own tests are in tiles.test.ts.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { resetAttention, setAttention } from "../src/attentionStore";
import { type CardRow, resetCardState, setCards } from "../src/cardState";
import { mountChips, mountRail } from "../src/rail";
import { resetTiles, setTile, type Health, type TileId } from "../src/tiles";
import { item, many, overview } from "./attentionFixtures";

const tile = (id: TileId, health: Health, value: string = id) => setTile({ id, health, value, note: "" });
const q = (node: ParentNode, sel: string) => node.querySelector<HTMLElement>(sel);
const all = (node: ParentNode, sel: string) => [...node.querySelectorAll<HTMLElement>(sel)];

beforeEach(() => {
  resetTiles();
  resetAttention();
  resetCardState();
  document.body.replaceChildren();
});
afterEach(() => {
  resetTiles();
  resetAttention();
  resetCardState();
});

function rail(tabs: (area: string) => boolean = () => true) {
  const host = document.createElement("div");
  document.body.append(host);
  const open = vi.fn();
  const stop = mountRail(host, { openTab: open, tabs });
  return { host, open, stop };
}
function chips(tabs: (area: string) => boolean = () => true) {
  const host = document.createElement("div");
  document.body.append(host);
  const open = vi.fn();
  const stop = mountChips(host, { openTab: open, tabs });
  return { host, open, stop };
}
const block = (host: HTMLElement, area: string) => q(host, `.rail-block[data-area="${area}"]`)!;

describe("an area's counts", () => {
  it("come from by_source, the table's rule, in the block header and the chip", () => {
    tile("git", "attention", "x");
    tile("release", "ok", "v1");
    const { host, stop } = rail();
    const c = chips();
    // The rows are three, but the server counted nine urgent and one to review (it cut the rest).
    setAttention(overview(many(3, "pr-failing"), { counts: { urgent: 9, review: 1, by_source: { git: { urgent: 9, review: 1 } } }, more: 7 }));
    expect(all(block(host, "ship"), ".rail-counts .nt-pill").map((n) => n.textContent)).toEqual(["9", "1"]);
    expect(q(c.host, ".as-chip .nt-pill")!.textContent).toBe("10");
    expect(q(block(host, "ship"), ".rail-ok")).toBeNull();
    stop();
    c.stop();
  });

  it("show a check when the area has nothing and every source was read", () => {
    tile("host", "ok", "Connected");
    const { host, stop } = rail();
    const c = chips();
    setAttention(overview([]));
    expect(q(block(host, "box"), ".rail-ok")).not.toBeNull();
    expect(q(block(host, "box"), ".rail-unread")).toBeNull();
    expect(q(c.host, ".as-chip .rail-ok")).not.toBeNull();
    stop();
    c.stop();
  });
});

describe("an area with a source that could not be read", () => {
  it("shows a question mark and the source's name, never the green check", () => {
    tile("git", "ok", "x");
    tile("host", "ok", "Connected");
    const { host, stop } = rail();
    const c = chips();
    setAttention(overview([], { unknown: ["Git repos could not be read"] }));
    const mark = q(block(host, "ship"), ".rail-unread")!;
    expect(mark.textContent).toBe("?");
    expect(mark.getAttribute("aria-label")).toBe("Git repos could not be read");
    expect(q(block(host, "ship"), ".rail-ok")).toBeNull();
    // The other areas read fine and keep their check.
    expect(q(block(host, "box"), ".rail-ok")).not.toBeNull();
    const chip = q(c.host, '.as-chip[data-area="ship"]')!;
    expect(q(chip, ".rail-unread")).not.toBeNull();
    expect(q(chip, ".rail-ok")).toBeNull();
    expect(chip.classList.contains("as-chip-unread")).toBe(true);
    stop();
    c.stop();
  });

  it("keeps the counts beside the question mark when the area also has rows", () => {
    tile("git", "problem", "x");
    const { host, stop } = rail();
    setAttention(overview([item("g1", { source: "git", kind: "pr-failing" })], { unknown: ["Git repos could not be read"] }));
    expect(all(block(host, "ship"), ".rail-counts > *").map((n) => n.className.split(" ").pop())).toEqual(["nt-pill-urgent", "rail-unread"]);
    stop();
  });
});

describe("tab links", () => {
  it("make a block header and a chip a button only for an area that has a tab", () => {
    tile("git", "ok", "x");
    tile("host", "ok", "Connected");
    const { host, open, stop } = rail((area) => area === "ship");
    const c = chips((area) => area === "ship");
    expect(block(host, "ship").querySelector(".rail-h")!.tagName).toBe("BUTTON");
    expect(block(host, "box").querySelector(".rail-h")!.tagName).toBe("DIV");
    expect(q(c.host, '[data-area="ship"]')!.tagName).toBe("BUTTON");
    expect(q(c.host, '[data-area="box"]')!.tagName).toBe("DIV");
    block(host, "ship").querySelector<HTMLElement>(".rail-h")!.click();
    expect(open).toHaveBeenCalledWith("ship");
    q(c.host, '[data-area="ship"]')!.click();
    expect(c.open).toHaveBeenCalledWith("ship");
    stop();
    c.stop();
  });
});

describe("a block header's name", () => {
  it("carries the counts, or the unread line, and where it goes", () => {
    tile("git", "attention", "x");
    tile("host", "ok", "Connected");
    tile("fabric", "ok", "Active");
    const { host, stop } = rail();
    setAttention(overview([item("g1", { source: "git", kind: "pr-failing" }), item("g2", { source: "git", kind: "pr-ready", tone: "review" })], { unknown: ["Box host could not be read"] }));
    const label = (area: string) => block(host, area).querySelector(".rail-h")!.getAttribute("aria-label");
    expect(label("ship")).toBe("Ship: 1 urgent, 1 to review, open the tab");
    expect(label("box")).toBe("Box: Box host could not be read, open the tab");
    expect(label("cloud")).toBe("Cloud: nothing needs you, open the tab");
    stop();
  });
});

describe("the blocks", () => {
  it("leave out an area whose card is off or whose tile is not set up", () => {
    const row = (id: string): CardRow => ({ id, name: id, group: "cloud", description: "", sources: [], on: id !== "vault", default_on: true, changed_by: null, changed_at: null, fields: [] });
    tile("vault", "ok", "x");
    const { host, stop } = rail();
    expect(block(host, "cloud")).not.toBeNull();
    setCards({ revision: 1, state_ok: true, cards: [row("vault")] });
    expect(host.querySelector('.rail-block[data-area="cloud"]')).toBeNull();
    stop();
  });
});
