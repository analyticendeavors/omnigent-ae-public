import { describe, expect, it } from "vitest";
import css from "../src/style.css?raw";

// jsdom does no layout, so the phone overflow bug (2026-09-25: the "no GitHub
// token" notice, a never-wrapping .meta, dragged the page sideways at 375px)
// is held by the stylesheet rules that prevent it.
function block(selector: string): string {
  const start = css.indexOf(`${selector} {`);
  expect(start, `${selector} rule`).toBeGreaterThanOrEqual(0);
  return css.slice(start, css.indexOf("}", start));
}

describe("phone layout guards", () => {
  it("lets the pull-requests-off notice wrap", () => {
    const rule = block(".ym-off");
    expect(rule).toContain("white-space: normal");
    expect(rule).toContain("overflow-wrap: anywhere");
  });

  it("clips horizontal overflow on the page root without breaking the sticky top", () => {
    const rule = block("#root");
    expect(rule).toContain("overflow-x: clip");
    expect(rule).not.toContain("overflow-x: hidden");
  });

  // 2026-09-25 (issue 28): the tag rows, the search bar and the filter sheet.
  it("wraps every tag row and filter row instead of widening the card", () => {
    for (const selector of [".card-tags", ".filter-opts", ".lot-filter-summary"]) {
      expect(block(selector)).toContain("flex-wrap: wrap");
    }
    for (const selector of [".tag-chip", ".filter-opt"]) {
      const rule = block(selector);
      expect(rule).toContain("max-width: 100%");
      expect(rule).toContain("min-width: 0");
    }
    for (const selector of [".tag-name", ".filter-repo", ".card-repo-name"]) {
      expect(block(selector)).toContain("text-overflow: ellipsis");
    }
  });

  it("lets the search field shrink beside the Filters button", () => {
    expect(block(".lot-filter-bar")).toContain("min-width: 0");
    expect(block(".lot-search")).toContain("min-width: 0");
    const input = block(".lot-search-input");
    expect(input).toContain("min-width: 0");
    // Safari on iOS zooms into any input under 16 px.
    expect(input).toContain("font-size: 16px");
  });

  // 2026-09-24: Your move's pull request cards got Merge; the confirm step
  // hides the card's own button row there too.
  it("hides a pull request card's buttons while its confirm step is open", () => {
    for (const selector of [".card-pr .actions[hidden]", ".card-pull .actions[hidden]"]) {
      expect(css).toContain(selector);
    }
    expect(block(".card-pull .actions[hidden]")).toContain("display: none");
  });

  // 2026-09-27: a pull request card's chips, reason and buttons share one
  // wrapping row, the buttons at its right end, in a tighter card.
  it("puts a pull request card's status on one wrapping row", () => {
    const row = block(".pr-status");
    expect(row).toContain("display: flex");
    expect(row).toContain("flex-wrap: wrap");
    const actions = block(".pr-status > .actions");
    expect(actions).toContain("flex: 1 1 auto");
    expect(actions).toContain("justify-content: flex-end");
    // Flat rows since 2026-10-14: no right padding, the row ends at the column.
    expect(css).toMatch(/\.card-pr,\n\.card-pull \{\n {2}padding: 8px 0 8px 12px;\n {2}gap: 6px;/);
  });

  // Review of #58 (2026-09-27): Copy keeps its own width on a phone-narrow
  // card. A merged card's Dismiss sits at the end of its head (2026-10-15).
  it("keeps Copy compact and puts Dismiss at the end of a merged card's head", () => {
    const dismiss = block(".card-head > .btn-dismiss");
    expect(dismiss).toContain("flex: 0 0 auto");
    expect(dismiss).toContain("margin-left: auto");
    expect(css).toMatch(/\.btn\.btn-menu,\n\.btn\.btn-dismiss \{[^}]*width: 32px;/);
    const narrow = css.split("@container not (min-width: 30rem) {").slice(1).join("");
    expect(narrow).toContain(".card-pr .actions .btn-copy");
    expect(narrow).not.toContain("pr-dismiss");
    expect(block(".link-copy")).toContain("user-select: all");
  });

  // Review, 2026-10-14: on a phone-narrow card "Open on GitHub" reads "GitHub"
  // and Copy is a square icon button, so Merge, GitHub and Copy share one row.
  it("shortens Open on GitHub and squares Copy on a phone-narrow card", () => {
    const narrow = css.split("@container not (min-width: 30rem) {").slice(1).join("");
    expect(narrow).toMatch(/\.btn-open \.btn-label-lead \{\s*display: none;/);
    expect(block(".btn-copy")).toContain("justify-content: center");
    expect(block(".pr-ready-text")).toContain("font-weight: 500");
  });

  // 2026-09-26: the confirm step is one wrapping row; on a phone the method
  // toggle takes its own row and Cancel and the confirm button sit side by side.
  it("keeps the merge confirm step on one wrapping row", () => {
    expect(block(".pr-confirm-row")).toContain("flex-wrap: wrap");
    expect(block(".pr-confirm .pr-methods")).toContain("flex: 1 1 100%");
    expect(css).not.toMatch(/\.pr-confirm \.actions \{\s*flex-direction: column/);
    expect(block(".pr-method input")).toContain("opacity: 0");
  });

  // 2026-09-27: in the two-column grid (720 px up) a card can be phone-narrow
  // while the viewport is not, so the switch reads the card's width.
  it("lays out a pull request card by its own width, not the viewport's", () => {
    expect(block(".card-pr,\n.card-pull")).toContain("container-type: inline-size");
    const wide = block("@container (min-width: 30rem)");
    expect(wide).toContain(".pr-method");
    const narrow = css.split("@container not (min-width: 30rem) {").slice(1).join("");
    expect(narrow).toContain(".pr-confirm .actions .btn");
    expect(narrow).toContain(".pr-confirm .pr-methods");
    for (const phone of css.split(/@media \((?:max|min)-width: 4[78]\dpx\) \{/).slice(1)) {
      const body = phone.slice(0, phone.indexOf("\n}"));
      expect(body).not.toMatch(/\.pr-|\.card-pr|\.card-pull/);
    }
  });

  // 2026-09-26: the owner found the Dashboard's buttons too tall at desktop width.
  // One shared rule: 32 px with a mouse, 40 px on a touch screen.
  it("keeps buttons compact with a mouse and thumb-sized on a touch screen", () => {
    expect(block("\n.btn")).toContain("min-height: 32px");
    // The first touch-screen block is the one right after the .btn rule.
    const coarse = block("@media (pointer: coarse)");
    expect(coarse).toContain(".btn");
    expect(coarse).toContain("min-height: 40px");
  });

  // 2026-10-14: from 720 px the Parking Lot heads the side column (main.ts);
  // the phone's one column keeps reading order through an order per slot.
  it("keeps the phone's reading order explicit, slot by slot", () => {
    const order = [
      "your-move",
      "prs",
      "working",
      "blocked",
      "lot",
      "review",
      "needs-note",
      "unsorted",
      "reference",
      "empties",
    ];
    order.forEach((slot, index) => {
      expect(block(`.slot-${slot}`)).toContain(`order: ${index + 1};`);
    });
  });

  // 2026-10-14: .menu sets display, which beats the hidden attribute.
  it("hides a closed row menu and keeps its button a .btn target", () => {
    expect(block(".menu[hidden]")).toContain("display: none");
    // A merged card's Dismiss shares the ghost icon button (2026-10-15).
    expect(block(".btn.btn-dismiss")).toContain("width: 32px");
    expect(css).toMatch(
      /@media \(pointer: coarse\) \{[^@]*\.btn\.btn-menu,\s*\.btn\.btn-dismiss \{\s*width: 40px;/,
    );
  });

  // Review (2026-10-14): the title that opens a session row is a target too.
  it("keeps a session row's title as tall as a .btn", () => {
    expect(block(".card-open")).toContain("min-height: 32px");
    expect(css).toMatch(/@media \(pointer: coarse\) \{\s*\.card-open \{\s*min-height: 40px;/);
  });

  // The Session Dashboard header (2026-10-17): a read-out that scrolls with
  // the page, so a phone keeps its screen. On the phone the usage bars take
  // their own row, the slots wrap two to a row and their names stay hidden.
  it("keeps the header out of the way on the phone", () => {
    expect(block("\n.top")).not.toContain("position: sticky");
    expect(block("\n.hd-top")).toContain('"usage usage"');
    expect(block("\n.hd-slots")).toContain("grid-template-columns: repeat(2, minmax(0, 1fr))");
    expect(block(".hd-slot b")).toContain("text-overflow: ellipsis");
    expect(block("\n.hd-cap")).toContain("display: none");
    // The status text wraps rather than pushing the pips off a phone (review).
    expect(block("\n.hd-pipe-row")).toContain(
      "74px minmax(min-content, 1fr) minmax(0, max-content)",
    );
    expect(block(".hd-pipe-right")).toContain("overflow-wrap: break-word");
  });

  it("puts the slots and the pipeline side by side from 720 px", () => {
    const wide = css.split("@media (min-width: 720px) {\n  .hd-top")[1].split("\n}\n")[0];
    expect(wide).toContain('"title usage toggle"');
    expect(wide).toContain("grid-template-columns: minmax(0, 1.4fr) minmax(0, 1fr)");
    expect(wide).toMatch(/\.hd-cap \{\s*display: flex;/);
  });
});
