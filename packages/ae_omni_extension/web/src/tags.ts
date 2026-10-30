// Tag chips in task-store's own colours and icons (2026-09-25,
// https://github.com/analyticendeavors/omnigent-ae/issues/28). The owner saw two
// grey chips and "+1" on each lot card; the app paints every tag with the
// colour and Tabler icon chosen in its tag manager. `GET /v1/ae/lot/tags?icons=true`
// carries both (the colour's palette name, the icon's path data), and this
// module keeps them for the page's lifetime so every card, the lot's and Your
// move's alike, draws the same face.
//
// The palette is the app's (webapp/static/style.css `.tc-*` in
// agents-and-automation home-server/task-store), copied into style.css. A tag
// with no colour of its own, or a store that did not answer, gets the neutral
// chip: the tags never disappear because their styling could not be read.

import type { LotTags } from "./api";
import { el } from "./dom";

export interface TagStyle {
  color: string | null;
  icon: string[] | null;
}

/** The app's tag palette (TAG_COLORS in the task-store webapp's server.py). */
export const TAG_COLORS: readonly string[] = [
  "teal",
  "blue",
  "green",
  "pink",
  "magenta",
  "lime",
  "brown",
  "slate",
  "khaki",
  "orange",
  "red",
];

/**
 * Tags a lot card never shows: every task filed to the lot carries `parked`
 * (normalize_tags puts it first), so on the Parking Lot it says nothing.
 */
export const HIDDEN_TAGS: ReadonlySet<string> = new Set(["parked"]);

const styles = new Map<string, TagStyle>();

/** Keep the styles from one `/lot/tags` answer, keyed casefolded like the store. */
export function setTagStyles(tags: LotTags | null): void {
  styles.clear();
  if (!tags) return;
  for (const row of tags.tags) {
    if (typeof row.name !== "string") continue;
    const color = row.color && TAG_COLORS.includes(row.color) ? row.color : null;
    const icon = row.icon ? (tags.icons?.[row.icon] ?? null) : null;
    styles.set(row.name.toLowerCase(), { color, icon });
  }
}

export function tagStyle(tag: string): TagStyle | null {
  return styles.get(tag.toLowerCase()) ?? null;
}

/** The tags a card shows, in the store's order, `parked` left out. */
export function visibleTags(tags: string[] | null | undefined): string[] {
  return (tags ?? []).filter((tag) => !HIDDEN_TAGS.has(tag.toLowerCase()));
}

const SVG_NS = "http://www.w3.org/2000/svg";

/** A Tabler outline icon from its path data, drawn the way the app's svgFromPaths does. */
export function tablerSvg(paths: string[], size = 12): SVGElement {
  const svg = document.createElementNS(SVG_NS, "svg");
  for (const [name, value] of Object.entries({
    viewBox: "0 0 24 24",
    width: String(size),
    height: String(size),
    fill: "none",
    stroke: "currentColor",
    "stroke-width": "2",
    "stroke-linecap": "round",
    "stroke-linejoin": "round",
    "aria-hidden": "true",
    class: "icon tag-icon",
  })) {
    svg.setAttribute(name, value);
  }
  for (const d of paths) {
    const path = document.createElementNS(SVG_NS, "path");
    path.setAttribute("d", d);
    svg.appendChild(path);
  }
  return svg;
}

/**
 * One tag chip: the app's colour and icon when it has them. A button when
 * *onClick* is given (the lot's cards filter by the tag), a plain label
 * otherwise (Your move's cards, the filter sheet's own options).
 */
export function tagChip(
  tag: string,
  options: { onClick?: (tag: string) => void; pressed?: boolean } = {},
): HTMLElement {
  const style = tagStyle(tag);
  const cls = `tag-chip${style?.color ? ` tc-${style.color}` : ""}`;
  const node = options.onClick
    ? el("button", {
        type: "button",
        class: `${cls} is-button`,
        title: options.pressed ? `Stop filtering by ${tag}` : `Show only ${tag}`,
        "aria-pressed": String(Boolean(options.pressed)),
        "data-tag": tag,
      })
    : el("span", { class: cls, "data-tag": tag });
  if (style?.icon && style.icon.length > 0) node.appendChild(tablerSvg(style.icon));
  node.appendChild(el("span", { class: "tag-name" }, tag));
  const onClick = options.onClick;
  if (onClick) {
    node.addEventListener("click", (event) => {
      event.stopPropagation();
      onClick(tag);
    });
  }
  return node;
}
