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

// 2026-11-14 (concept D2): the pill strip, the packed board and the drag are gone; the page is tabs,
// one Needs you table and a rail. These hold the rules that keep it readable.
describe("the Admin Center page", () => {
  it("puts the verdict on its own line under the title on a narrow page", () => {
    const at = css.indexOf("@container admin (max-width: 519px) {\n  .fabric.admin .fabric-top {");
    expect(at).toBeGreaterThanOrEqual(0);
    const rules = css.slice(at, css.indexOf("\n}\n\n", at));
    expect(rules).toContain("flex-wrap: wrap");
    expect(rules).toContain("flex: 1 0 100%");
  });

  it("drops the rules that only served the pills, the packed board and the drag", () => {
    for (const gone of [".admin-tiles {", ".pill {", ".pill-pop {", ".admin-grip {", ".admin-group-h {", ".tile-bar {"]) {
      expect(css, gone).not.toContain(gone);
    }
    expect(css).not.toContain("data-packed");
    expect(css).not.toContain("is-dragging");
  });

  it("wraps the Not set up line and hides the packed board's group tag", () => {
    const notset = block(".admin-notset");
    expect(notset).toContain("overflow-wrap: anywhere");
    expect(notset).toContain("1px dashed var(--line)");
    expect(block(".admin-card-tag")).toContain("display: none");
  });

  it("puts the rail beside the table from 1,700 px of page, at 500 px, and folds it to chips below", () => {
    const at = css.indexOf("@container admin (min-width: 1700px) {");
    expect(at).toBeGreaterThanOrEqual(0);
    const wide = css.slice(at, css.indexOf("\n}\n\n", at));
    expect(wide).toContain("minmax(0, 1fr) 500px");
    expect(wide).toContain(".as-chips");
    expect(block(".as-rail")).toContain("display: none");
  });

  it("folds the Age and Checked columns into the row's second line under 1,700 px", () => {
    const at = css.indexOf("@container admin (max-width: 1699px) {");
    expect(at).toBeGreaterThanOrEqual(0);
    const narrow = css.slice(at, css.indexOf("\n}\n\n", at));
    expect(narrow).toContain(".nt-row > .nt-age");
    expect(narrow).toContain(".nt-row > .nt-checked");
    expect(narrow).toContain("display: none");
    expect(narrow).toContain("attr(data-fold)");
  });

  it("gives the phone two-line rows, 44 px targets and an opened item as a body-level sheet with pinned actions", () => {
    const at = css.indexOf("@container admin (max-width: 520px) {\n  .nt-head,");
    expect(at).toBeGreaterThanOrEqual(0);
    const phone = css.slice(at, css.indexOf("\n}\n\n", at));
    expect(phone).toContain("min-height: 44px");
    // The sheet is a <dialog> in the top layer, outside the page container's containing block, not a
    // fixed row inside it (a container makes its descendants' fixed boxes relative to itself).
    expect(phone).not.toContain("position: fixed");
    const sheetRule = (() => {
      const at = css.indexOf(".nt-sheet {\n  position: fixed");
      expect(at).toBeGreaterThanOrEqual(0);
      return css.slice(at, css.indexOf("}", at));
    })();
    expect(sheetRule).toContain("position: fixed");
    expect(sheetRule).toContain("inset: 0");
    // Below the shared dialogs (.ae-dialog, 60) so a confirmation opened from the sheet stacks on top.
    const dialogZ = Number(/z-index: (\d+)/.exec(block(".ae-dialog"))![1]);
    expect(Number(/z-index: (\d+)/.exec(sheetRule)![1])).toBeLessThan(dialogZ);
    expect(block(".nt-sheet .nt-acts")).toContain("position: sticky");
    // The pinned bar is a step lighter than the sheet, and its buttons one more.
    expect(sheetRule).toContain("background: var(--s2)");
    expect(block(".nt-sheet .nt-acts")).toContain("background: var(--s3)");
    expect(block(".nt-sheet .nt-acts")).toContain("--btn-bg: var(--s4)");
    // The sheet lives on body, outside .fabric.admin, so it carries the ramp's variables itself.
    expect(css).toContain(".fabric.admin,\n.nt-sheet {");
    expect(css).toContain(':root[data-theme="dark"] .nt-sheet {');
  });

  it("drops the checkboxes, the selection bar and the composition bar on a phone page, and shows Snooze's word", () => {
    const at = css.indexOf("@container admin (max-width: 520px) {\n  .nt-head,");
    const phone = css.slice(at, css.indexOf("\n}\n\n", at));
    const hidden = phone.slice(0, phone.indexOf("}"));
    for (const selector of [".nt-pick", ".nt-pick-group", ".nt-bulk", ".nt-comp-slot"]) expect(hidden, selector).toContain(selector);
    expect(hidden).toContain("display: none");
    // The word is a real label in the DOM, shown here and hidden elsewhere.
    expect(phone).toContain(".nt-snooze-word");
    expect(css).toContain(".nt-snooze-menu .nt-snooze-word {\n  display: none;");
  });

  it("builds one button system: tokens, 13 px 600 labels, and 44 px / 14 px on a coarse pointer", () => {
    expect(css).toContain("--ctl-h: 32px;");
    expect(css).toContain("--ctl-fs: 13px;");
    expect(css).toContain("--ctl-fw: 600;");
    expect(css).toContain("--ctl-fs: 14px;");
    // The three complaints: Review N is no longer muted 12 px / 28 px text, the area link is no plain text.
    expect(block(".attn-review-all")).not.toContain("font-size: 12px");
    expect(block(".attn-review-all")).not.toContain("min-height: 28px");
    expect(block(".nt-go")).not.toContain("color: var(--muted)");
  });

  it("shows the toast above a phone's sheet, on phone widths only", () => {
    const at = css.indexOf("@media (max-width: 520px) {\n  body:has(.nt-sheet) #toast {");
    expect(at).toBeGreaterThanOrEqual(0);
    const rule = css.slice(at, css.indexOf("\n}\n", at));
    expect(rule).toContain("position: fixed");
    const sheetZ = Number(/z-index: (\d+)/.exec(css.slice(css.indexOf(".nt-sheet {\n  position: fixed")))![1]);
    expect(Number(/z-index: (\d+)/.exec(rule)![1])).toBeGreaterThan(sheetZ);
    // No rule moves the toast without a phone width and an open sheet.
    expect(css.match(/#toast/g)!.length).toBeLessThanOrEqual(2);
  });

  it("gives every coarse pointer 44 px targets on the tabs, rows, groups, bar, rail and chips", () => {
    const at = css.lastIndexOf("@media (pointer: coarse) {\n  .as-tab,");
    expect(at).toBeGreaterThanOrEqual(0);
    const coarse = css.slice(at, css.indexOf("\n}\n", at));
    for (const selector of [".as-tab", ".nt-toggle", ".nt-grp-btn", ".nt-seg", ".rail-h", ".as-chip"]) {
      expect(coarse, selector).toContain(selector);
    }
    expect(coarse).toContain("min-height: 44px");
    // Buttons (Close, the area tab button, the toolbar) take their 44 px from the button system's token.
    const tokens = css.slice(css.indexOf("@media (pointer: coarse) {\n  .fabric.admin,\n  .nt-sheet {"));
    expect(tokens.slice(0, 200)).toContain("--ctl-h: 44px");
  });

  it("fills the width: area cards in lanes, Services one wide row per card, a quiet day's tiles", () => {
    // Columns, not a grid row (2026-11-22): a short card sits straight under the card above it.
    // Lanes side by side, cards kept in page order (2026-11-22): a short card sits straight under the card above it.
    const laned = '.as-board[data-lanes]:not([data-lanes="1"]) > .admin-col';
    expect(block(laned)).toContain("position: relative");
    expect(block(`${laned} > .admin-card`)).toContain("position: absolute");
    expect(block(`${laned} > .admin-card`)).toContain("left: calc(var(--lane, 0) * (100% + 14px) / var(--lanes, 1))");
    expect(css).not.toContain(".as-board .admin-col {");
    expect(css).toContain(".ae-cards-page .ae-card-cells {");
    // Only where there is no wall (2026-11-17): under 760 px, and on a server with four cards or fewer.
    expect(block('.as-ov.is-quiet:not([data-wall="wide"], [data-wall="stack"]) .as-tiles')).toContain("display: grid");
    expect(block(".as-tiles")).toContain("display: none");
  });

  it("holds the light theme's pills and small labels at 4.5:1 or better (measured in the round 2 report)", () => {
    const lum = (hex: string): number => {
      const [r, g, b] = [1, 3, 5].map((i) => parseInt(hex.slice(i, i + 2), 16) / 255).map((c) => (c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4));
      return 0.2126 * r! + 0.7152 * g! + 0.0722 * b!;
    };
    const ratio = (a: string, b: string): number => {
      const [hi, lo] = [lum(a), lum(b)].sort((x, y) => y - x);
      return (hi! + 0.05) / (lo! + 0.05);
    };
    const light = css.slice(css.indexOf(".fabric.admin,\n.nt-sheet {\n  --s0"));
    const rule = light.slice(0, light.indexOf("}"));
    const muted = /--muted: (#[0-9a-f]{6})/.exec(rule)![1]!;
    const warn = /--warn: (#[0-9a-f]{6})/.exec(rule)![1]!;
    const steps = ["#ffffff", "#f6f7f9", "#eceff3", "#e2e6ec"];
    for (const step of steps) expect(ratio(muted, step), `muted on ${step}`).toBeGreaterThanOrEqual(4.5);
    for (const step of steps.slice(1)) expect(ratio(warn, step), `warn on ${step}`).toBeGreaterThanOrEqual(4.5);
    // The amber pill is white on the darker amber; the red one white on --err; the blue one white on --accent.
    expect(ratio("#ffffff", warn)).toBeGreaterThanOrEqual(4.5);
    expect(ratio("#ffffff", "#b91c1c")).toBeGreaterThanOrEqual(4.5);
    expect(ratio("#ffffff", "#2563eb")).toBeGreaterThanOrEqual(4.5);
    expect(block(".nt-pill-review")).toContain("color: #ffffff");
  });

  it("holds the surface ramp for the dark theme and a darker one for the light theme", () => {
    const dark = css.slice(css.indexOf(':root[data-theme="dark"] .fabric.admin,'));
    for (const step of ["#111827", "#1a2538", "#25344f", "#304262", "#2c3a54", "#3d4f72", "#4d6189"]) {
      expect(dark.slice(0, dark.indexOf("}")), step).toContain(step);
    }
    const light = css.slice(css.indexOf(".fabric.admin,\n.nt-sheet {\n  --s0"));
    for (const step of ["#f6f7f9", "#eceff3", "#e2e6ec"]) expect(light.slice(0, light.indexOf("}")), step).toContain(step);
    // The opened row's panel is one step lighter than the table card, its facts one more.
    expect(block(".nt-panel")).toContain("background: var(--s2)");
    expect(css).toContain("--inner: var(--s3);");
  });

  it("keeps a closed row's non-primary actions out of the row, and the panel's grid ready for a second column", () => {
    expect(css).toContain(".nt-row:not(.is-open) .attn-actions > button[data-focus]:not([data-primary])");
    expect(css).toContain(".nt-panel:has(> .nt-panel-side)");
  });
});

describe("the status chip beside the action buttons (2026-11-18)", () => {
  const ruleAt = (selector: string): string => {
    const at = css.indexOf(`${selector} {`);
    return css.slice(at, css.indexOf("}", at));
  };

  it("matches its neighbours' height, centre and radius in the Admin Center and the sheet, and never looks pressable", () => {
    const rule = ruleAt(".fabric.admin .attn-status,\n.nt-sheet .attn-status");
    expect(rule).toContain("align-self: stretch");
    expect(rule).toContain("align-items: center");
    expect(rule).toContain("border-radius: 8px");
    expect(rule).toContain("min-height: var(--ctl-h, 32px)");
    expect(rule).toContain("cursor: default");
    expect(css).toMatch(/\.fabric\.admin \.attn-status,\n\s+\.nt-sheet \.attn-status \{\n\s+min-height: 44px;/);
    expect(css).not.toMatch(/\.attn-status[^{]*:hover/);
  });

  it("leaves the chip's size alone on other surfaces", () => {
    const base = ruleAt(".attn-status");
    expect(base).toContain("padding: 2px 8px");
    expect(base).toContain("border-radius: 6px");
    expect(base).not.toContain("min-height");
  });

  it("insets the empty text like the header, banners and strip", () => {
    expect(ruleAt(".attn.nt > .attn-body > .attn-empty")).toContain("padding: 0 18px");
  });
});
