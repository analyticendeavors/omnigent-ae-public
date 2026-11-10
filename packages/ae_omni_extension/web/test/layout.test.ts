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

// 2026-11-03: a real browser showed Needs you never folding. `.attn-rows` and `.attn-children`
// say `display: flex`, which beats the `hidden` attribute folding sets, and jsdom applies no CSS,
// so no behaviour test could see it. Every .attn rule that sets a display must be beaten by a
// [hidden] rule: its own, or the panel's scoped `.attn [hidden]`.
describe("Needs you folding survives the stylesheet", () => {
  const plain = css.replace(/\/\*[\s\S]*?\*\//g, "");
  const rules = [...plain.matchAll(/([^{}]+)\{([^{}]*)\}/g)].map((m) => ({
    selector: m[1]!.trim(),
    body: m[2]!,
  }));
  const displaying = rules.filter(
    (r) => /(^|,)\s*\.attn[\w-]*\b/.test(r.selector) && /display:\s*(?!none)[\w-]+/.test(r.body),
  );

  it("finds the rules this guards", () => {
    const selectors = displaying.map((r) => r.selector);
    expect(selectors.some((s) => s.includes(".attn-rows"))).toBe(true);
    expect(selectors.some((s) => s.includes(".attn-children"))).toBe(true);
  });

  it("has a [hidden] rule that wins over every .attn display rule", () => {
    const scoped = rules.find((r) => r.selector === ".attn [hidden]");
    expect(scoped, "the .attn [hidden] rule").toBeDefined();
    expect(scoped!.body).toMatch(/display:\s*none\s*!important/);
  });

  it("never makes an .attn display rule !important (it would beat the [hidden] rule)", () => {
    for (const rule of displaying) expect(rule.body, rule.selector).not.toMatch(/display:[^;]*!important/);
  });

  it("hides the worst-item words on a phone instead of clipping them to a letter", () => {
    const at = css.indexOf("@media (max-width: 520px) {\n  .attn-line .attn-worst");
    expect(at).toBeGreaterThanOrEqual(0);
    expect(css.slice(at, css.indexOf("}\n}", at))).toContain("display: none");
    expect(block(".attn-worst")).toContain("min-width: 8ch");
  });
});

// 2026-11-06: four status pills replaced the ten tiles.
describe("status pills", () => {
  it("lays four pills in one row, and two by two under 760 px of the page", () => {
    expect(block(".admin-tiles")).toContain("repeat(4, minmax(0, 1fr))");
    const at = css.indexOf("@container admin (max-width: 760px) {\n  .admin-tiles {");
    expect(at).toBeGreaterThanOrEqual(0);
    expect(css.slice(at, css.indexOf("}\n}", at))).toContain("repeat(2, minmax(0, 1fr))");
  });

  it("keeps the strip on a phone: nothing hides .admin-tiles any more", () => {
    expect(css).not.toMatch(/@container admin \(width < 520px\) \{\s*\.admin-tiles \{\s*display: none/);
  });

  it("drops the rules that only served the old tiles, and keeps the Deploy hold bar", () => {
    for (const gone of [".admin-tiles > .tile", ".tile-value", ".tile-label", ".tile-note", ".tile-dot"]) {
      expect(css, gone).not.toContain(`${gone} {`);
    }
    expect(css).toContain(".tile-bar {");
  });

  it("tints a problem pill from --err, with no hard-coded colour", () => {
    expect(block(".pill-problem")).toContain("color-mix(in srgb, var(--err)");
    expect(block(".pill-attention")).toContain("var(--warn-bg)");
  });

  it("opens the popover on hover and keyboard focus only, and never clips it", () => {
    expect(block(".pill-pop")).toContain("display: none");
    expect(css).toContain(".pill:focus-visible + .pill-pop {");
    const at = css.indexOf("@media (hover: hover) {\n  .pill-wrap:hover .pill-pop {");
    expect(at, "the hover rule sits inside @media (hover: hover)").toBeGreaterThanOrEqual(0);
    expect(css.indexOf(".pill-wrap:hover")).toBeGreaterThan(css.indexOf("@media (hover: hover)"));
    expect(block(".pill-wrap")).not.toMatch(/overflow/);
    expect(block(".pill")).not.toMatch(/overflow/);
  });

  it("wraps the pill line to two lines instead of one ellipsized line", () => {
    const rule = block(".pill-line");
    expect(rule).toContain("-webkit-line-clamp: 2");
    expect(rule).toContain("line-clamp: 2");
    expect(rule).not.toContain("white-space: nowrap");
    // Under 520 px the brief is one line (2026-11-07).
    const at = css.indexOf("@container admin (width < 520px) {\n  .pill-line {");
    expect(at).toBeGreaterThanOrEqual(0);
    expect(css.slice(at, css.indexOf("\n}\n", at))).toContain("-webkit-line-clamp: 1");
  });

  it("makes the status boxes one height and leaves the host to reserve the phone's top band", () => {
    expect(block(".pill")).toContain("height: 100%");
    expect(css).not.toContain("padding-top: 52px");
  });

  it("puts the verdict on its own line under the title on a narrow page", () => {
    const at = css.indexOf("@container admin (max-width: 519px) {\n  .fabric.admin .fabric-top {");
    expect(at).toBeGreaterThanOrEqual(0);
    const rules = css.slice(at, css.indexOf("\n}\n\n", at));
    expect(rules).toContain("flex-wrap: wrap");
    expect(rules).toContain("flex: 1 0 100%");
  });

  // 2026-11-06 (phase 4): the cards sit under group headings.
  it("lays the board out as groups, each with the column grid, and wraps the Not set up line", () => {
    const board = block(".admin-board");
    // 2026-11-07 (D1): a grid of the page's columns that packs groups first-fit.
    expect(board).toContain("display: grid");
    expect(board).toContain("grid-auto-flow: row dense");
    expect(board).toContain("gap: 22px 14px");
    expect(css).not.toContain(".admin-col > .admin-card:last-child {\n  flex: 1;");
    // The group's stacks and the box-admin board keep today's grid.
    const start = css.indexOf(".admin-group-cols,");
    expect(start).toBeGreaterThanOrEqual(0);
    const grid = css.slice(start, css.indexOf("}", start));
    expect(grid).toContain(".admin-board[data-single]");
    expect(grid).toContain("grid-template-columns: repeat(var(--cols), minmax(0, 1fr))");
    expect(grid).toContain("gap: 14px");
    expect(grid).toContain("align-items: start");
    const heading = block(".admin-group-h");
    expect(heading).toContain("letter-spacing: 0.07em");
    expect(heading).toContain("text-transform: uppercase");
    expect(heading).toContain("var(--muted)");
    expect(block(".admin-group-line")).toContain("var(--line)");
    const notset = block(".admin-notset");
    expect(notset).toContain("overflow-wrap: anywhere");
    expect(notset).toContain("1px dashed var(--line)");
  });

  // 2026-11-07 (packed board): four columns, the group tag, Needs you as a tile, the flash ring.
  it("has a fourth column, a tag shown only when packed, and no 360 px Needs you grid", () => {
    expect(block('.admin-board[data-columns="4"]')).toContain("--cols: 4");
    expect(block(".admin-card-tag")).toContain("display: none");
    const tag = block(".admin-board[data-packed] .admin-card-tag");
    expect(tag).toContain("font-size: 11px");
    expect(tag).toContain("font-weight: 650");
    expect(tag).toContain("letter-spacing: 0.08em");
    expect(tag).toContain("var(--muted)");
    expect(css).not.toContain("minmax(360px, 1fr)");
    expect(block(".admin-board[data-packed] .attn-tree")).toContain("flex-direction: column");
    expect(block(".admin-board[data-packed] .attn-review-source")).toContain("margin-left: auto");
    expect(block(".admin-card.is-flash")).toContain("var(--accent)");
  });
});
