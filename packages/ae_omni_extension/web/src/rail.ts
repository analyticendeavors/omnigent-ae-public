// The health rail beside the Needs you table (2026-11-14, concept D2).
//
// One block per area that has a connected card (Ship, Box, Cloud, Accounts). Each lists its
// members from the tile store (tiles.ts): a dot in the member's health, its name and its short
// value. A block's header shows the area's Needs you counts by the same rule as the table
// (attentionStore.ts), or a check when it has none, and opens that area's tab. Where the data
// already exists a block adds one meter or chart: Ship the Deploy hold's bar, Box the CPU heat
// line when the box monitor holds the day's readings. It reads the stores the page already
// fills and adds no request.

import { doneTodayBlock } from "./activityPanel";
import { onActivity } from "./activityStore";
import { onAttention } from "./attentionStore";
import { lastCpuHistory, sparkline } from "./boxHealth";
import { onCardState, tileGone } from "./cardState";
import { el, replaceChildren } from "./dom";
import { checkIcon, chevronIcon } from "./icons";
import { areaCounts, areaLabel, connectedCards, sourceList, unreadIn } from "./needsModel";
import { DOMAINS, type Domain, getTile, memberText, onTiles, tileLabel, type Tile } from "./tiles";

export interface RailOptions {
  /** Open an area's tab (a block header). */
  openTab: (area: Domain["id"]) => void;
  /** True when *area* has a tab of its own (a block's header has nowhere to go otherwise). */
  tabs: (area: Domain["id"]) => boolean;
  /** Open the Activity tab ("See all in Activity"); null when this page has none. */
  activity?: (() => void) | null;
}

/** The members of an area that have reported and are on this page. */
export function railMembers(domain: Domain): Tile[] {
  return domain.members.flatMap((id) => {
    const tile = getTile(id);
    return tile && !tile.notSetUp && !tileGone(id) ? [tile] : [];
  });
}

function meter(domain: Domain, members: Tile[]): HTMLElement | null {
  if (domain.id === "ship") {
    const hold = members.find((tile) => tile.id === "hold" && tile.bar != null);
    if (!hold) return null;
    return el(
      "div",
      { class: "rail-meter", role: "img", "aria-label": `Deploy hold, ${Math.round(hold.bar ?? 0)} percent of the time used` },
      el("span", { class: "rail-meter-lab" }, "Deploy hold"),
      el("span", { class: "rail-bar" }, el("i", { style: `width:${Math.min(100, Math.max(0, hold.bar ?? 0))}%` })),
    );
  }
  if (domain.id === "box") return heatMeter();
  return null;
}

/** The Box CPU heat line (the rail's Box meter, and the wall's Box health tile); null before the box monitor has readings. */
export function heatMeter(): HTMLElement | null {
  const heat = lastCpuHistory();
  if (!heat) return null;
  return el("div", { class: "rail-meter rail-heat" }, el("span", { class: "rail-meter-lab" }, "CPU heat, 24 h"), sparkline(heat, "CPU temperature over 24 hours"));
}

/** The mark an area holding an unread source shows where a check would be: a question mark. */
function unreadMark(area: Domain["id"]): HTMLElement | null {
  const unread = unreadIn(area);
  if (!unread.length) return null;
  const words = `${sourceList(unread)} could not be read`;
  return el("span", { class: "rail-unread", title: words, role: "img", "aria-label": words }, "?");
}

/** The header button's name: the counts or the "could not be read" line, then where it goes. */
function headLabel(domain: Domain, counts: { urgent: number; review: number } | null): string {
  const unread = unreadIn(domain.id);
  const parts: string[] = [];
  if (counts?.urgent) parts.push(`${counts.urgent} urgent`);
  if (counts?.review) parts.push(`${counts.review} to review`);
  if (unread.length) parts.push(`${sourceList(unread)} could not be read`);
  if (!parts.length && counts) parts.push("nothing needs you");
  return `${areaLabel(domain.id)}: ${parts.length ? `${parts.join(", ")}, ` : ""}open the tab`;
}

function block(domain: Domain, options: RailOptions): HTMLElement | null {
  const members = railMembers(domain);
  if (!connectedCards(domain.id).length || !members.length) return null;
  const counts = areaCounts(domain.id);
  const linked = options.tabs(domain.id);
  const unread = unreadMark(domain.id);
  const head = el(
    linked ? "button" : "div",
    {
      ...(linked
        ? { type: "button", title: `Open the ${areaLabel(domain.id)} tab`, "aria-label": headLabel(domain, counts) }
        : {}),
      class: "rail-h",
    },
    el("span", { class: "rail-icon", "aria-hidden": "true" }, domain.icon()),
    el("span", { class: "rail-name" }, domain.label),
    counts && (counts.urgent || counts.review)
      ? el(
          "span",
          { class: "rail-counts" },
          counts.urgent ? el("span", { class: "nt-pill nt-pill-urgent", title: `${counts.urgent} urgent` }, String(counts.urgent)) : null,
          counts.review ? el("span", { class: "nt-pill nt-pill-review", title: `${counts.review} to review` }, String(counts.review)) : null,
          unread,
        )
      : unread
        ? el("span", { class: "rail-counts" }, unread)
        : counts
          ? el("span", { class: "rail-ok", title: "Nothing needs you here", role: "img", "aria-label": "Nothing needs you here" }, checkIcon())
          : null,
    linked ? el("span", { class: "rail-go", "aria-hidden": "true" }, chevronIcon()) : null,
  );
  if (linked) head.addEventListener("click", () => options.openTab(domain.id));
  return el(
    "section",
    { class: "rail-block", "data-area": domain.id },
    head,
    el(
      "ul",
      { class: "rail-members" },
      ...members.map((tile) =>
        el(
          "li",
          { class: `rail-member tile-${tile.health}`, title: tile.title || undefined },
          el("span", { class: "rail-dot", "aria-hidden": "true" }),
          el("span", { class: "rail-mname" }, tileLabel(tile.id)),
          el("span", { class: "rail-mval" }, memberText(tile)),
        ),
      ),
    ),
    meter(domain, members),
  );
}

/** Draw the rail into *host* and keep it current; returns the function that stops it. */
export function mountRail(host: HTMLElement, options: RailOptions): () => void {
  const draw = (): void => {
    // A redraw replaces the nodes; a focused block header keeps its focus.
    const focused = host.contains(document.activeElement)
      ? (document.activeElement as HTMLElement).closest<HTMLElement>(".rail-block")?.dataset.area
      : undefined;
    replaceChildren(host, [...DOMAINS.map((domain) => block(domain, options)), doneTodayBlock(options.activity ?? null)]);
    if (focused) host.querySelector<HTMLElement>(`.rail-block[data-area="${focused}"] .rail-h`)?.focus({ preventScroll: true });
  };
  draw();
  const stops = [onTiles(draw), onAttention(draw), onCardState(draw), onActivity(draw)];
  return () => {
    for (const stop of stops) stop();
  };
}

/**
 * Four small area chips (2026-11-14): what the rail says in one line each, for a page too narrow
 * for the rail (under 1,700 px one row, a phone two by two). A chip opens its area's tab.
 */
export function mountChips(host: HTMLElement, options: RailOptions): () => void {
  const draw = (): void => {
    replaceChildren(
      host,
      DOMAINS.map((domain) => {
        const members = railMembers(domain);
        if (!connectedCards(domain.id).length || !members.length) return null;
        const counts = areaCounts(domain.id);
        const total = counts ? counts.urgent + counts.review : 0;
        const unread = unreadMark(domain.id);
        const tone = counts?.urgent ? "urgent" : total ? "review" : unread ? "unread" : "ok";
        const worstTile = members.find((tile) => tile.health === "problem") ?? members.find((tile) => tile.health === "attention");
        const line = worstTile ? memberText(worstTile) : memberText(members[0]!);
        const linked = options.tabs(domain.id);
        const chip = el(
          linked ? "button" : "div",
          {
            ...(linked ? { type: "button", title: `Open the ${areaLabel(domain.id)} tab` } : {}),
            class: `as-chip as-chip-${tone}`,
            "data-area": domain.id,
          },
          el(
            "span",
            { class: "as-chip-h" },
            el("span", { class: "rail-icon", "aria-hidden": "true" }, domain.icon()),
            el("span", { class: "rail-name" }, domain.label),
            total ? el("span", { class: `nt-pill nt-pill-${tone === "urgent" ? "urgent" : "review"}` }, String(total)) : null,
            unread,
            total || unread ? null : el("span", { class: "rail-ok", role: "img", "aria-label": "Nothing needs you here" }, checkIcon()),
          ),
          el("span", { class: "as-chip-line" }, line),
        );
        if (linked) chip.addEventListener("click", () => options.openTab(domain.id));
        return chip;
      }),
    );
  };
  draw();
  const stops = [onTiles(draw), onAttention(draw), onCardState(draw)];
  return () => {
    for (const stop of stops) stop();
  };
}
