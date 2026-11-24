// What the Admin Center's table, tabs and rail work out from the stores (2026-11-14, concept D2).
//
// A plain module with no drawing: the grouping of Needs you by area, the words a folded row says,
// the tab rule, and the area counts. needsTable.ts, rail.ts and adminShell.ts draw from these, and
// test/needsModel.test.ts covers them without a page. Counts come from attentionStore.ts (the one
// counting rule: open, not dismissed, not held by a chat that is still working), so the table,
// the tab badges, the rail and the verdict can never disagree.

import type { AttentionItem, AttentionOverview, AttentionSource } from "./api";
import { type Block, buildBlocks, GROUPS, held } from "./attention";
import { attentionCount, getAttention, SOURCE_NAMES, uncounted, urgentCount } from "./attentionStore";
import { CARD_TILES, cardGone, getCards } from "./cardState";
import { DOMAINS, type DomainId, getTile } from "./tiles";

/** The area each Needs you source belongs to (Reid's D2 plan, 2026-11-14). */
export const AREA_OF: Record<AttentionSource, DomainId> = {
  git: "ship",
  "git-mine": "ship",
  box: "box",
  host: "box",
  home: "box",
  secrets: "box",
  cost: "cloud",
  databricks: "cloud",
  vault: "cloud",
  accounts: "money",
  family: "money",
  social: "ship",
};

/** Rows shown under an open source before "Show N more" (2026-11-15: five, since the page has the room). */
export const ROWS_OPEN = 5;
/** A server with this many connected cards or fewer gets no area tabs. */
export const AREA_TAB_MIN_CARDS = 4;

export const areaLabel = (id: DomainId): string => DOMAINS.find((domain) => domain.id === id)?.label ?? id;

export const sourcesOfArea = (area: DomainId): AttentionSource[] =>
  (Object.keys(AREA_OF) as AttentionSource[]).filter((source) => AREA_OF[source] === area);

/** The fold id of an area row; a source row keeps attention.ts's own `attn.<source>`. */
export const areaFoldId = (area: DomainId): string => `nt.area.${area}`;

export interface SourceGroup {
  source: AttentionSource;
  block: Block;
}

export interface AreaGroup {
  id: DomainId;
  label: string;
  sources: SourceGroup[];
  items: AttentionItem[];
  urgent: number;
  review: number;
  working: number;
}

/** Items counted for the owner: not held by a chat that is still working and not assigned to another admin. */
const counted = (items: AttentionItem[]): AttentionItem[] => items.filter((item) => !uncounted(item));

/** Area groups in the page's order, each with the source blocks that have items. */
export function buildAreas(
  items: AttentionItem[],
  options: { only?: AttentionSource | null; area?: DomainId | null } = {},
): AreaGroup[] {
  const blocks = buildBlocks(items, options.only ?? null);
  const areas: AreaGroup[] = [];
  for (const domain of DOMAINS) {
    if (options.area && options.area !== domain.id) continue;
    const sources = blocks.filter((block) => AREA_OF[block.source] === domain.id).map((block) => ({ source: block.source, block }));
    if (!sources.length) continue;
    const all = sources.flatMap((entry) => entry.block.items);
    const live = counted(all);
    const urgent = live.filter((item) => item.tone === "urgent").length;
    areas.push({
      id: domain.id,
      label: domain.label,
      sources,
      items: all,
      urgent,
      review: live.length - urgent,
      working: all.filter(held).length,
    });
  }
  // One fixed order (2026-11-16): the page's areas, and each area's sources in the server's order.
  // Urgency shows through the badges, the rail and Expand urgent, never through the position.
  return areas;
}

/** "Git repos 4 · Box host 1": what a closed area row lists. */
export function areaPreview(area: AreaGroup): string {
  return area.sources
    .map((entry) => `${SOURCE_NAMES[entry.source]} ${entry.block.items.length}`)
    .join(" · ");
}

/** What a closed source row says: its first item's title, "and N more" when there are others. */
export function sourcePreview(block: Pick<Block, "items">): string {
  const first = block.items[0];
  if (!first) return "";
  const rest = block.items.length - 1;
  return rest > 0 ? `${first.title} and ${rest} more` : first.title;
}

/** The rows an open source shows, and how many wait behind "Show N more". */
export function rowsShown(
  items: AttentionItem[],
  expanded: boolean,
  limit = ROWS_OPEN,
): { shown: AttentionItem[]; rest: number } {
  const shown = expanded ? items : items.slice(0, limit);
  return { shown, rest: items.length - shown.length };
}

/** The name of the group (kind) an item is in, for the muted prefix. */
export function kindName(item: Pick<AttentionItem, "source" | "kind">): string {
  return GROUPS[item.source]?.find((spec) => spec.kinds.includes(item.kind))?.name ?? "";
}

/** True when a source's items span more than one kind, so each row names its own. */
export function hasSeveralKinds(items: Pick<AttentionItem, "kind">[]): boolean {
  return new Set(items.map((item) => item.kind)).size > 1;
}

/** Rough heights, in px, of the table's parts: what the open-or-fold plan spends. */
export const HEIGHTS = { head: 46, row: 58, more: 34 };

/** Narrowest page (container px) that splits the Overview into columns, and the width each column needs. */
export const SPLIT_MIN = 1800;
export const COL_MIN = 900;
export const MAX_COLS = 3;

/** The column count the width before this one had; a drop from it waits for this many px below its threshold. */
export const COL_HYSTERESIS = 40;

/**
 * How many area columns a table of *width* px holds: clamp(floor(width / 900), 1, 3), one under 1,800.
 * *from* is the count already drawn: it is kept until the width falls COL_HYSTERESIS px below that
 * count's threshold, so a scrollbar appearing at the edge does not flip the layout back and forth.
 */
export function columnCount(width: number, from = 1): number {
  const at = (w: number) => (w >= SPLIT_MIN ? Math.min(MAX_COLS, Math.max(1, Math.floor(w / COL_MIN))) : 1);
  const n = at(width);
  if (from > n && from > 1 && width >= from * COL_MIN - COL_HYSTERESIS) return from;
  return n;
}

/** The lines a source takes in the placement: its heading, up to five rows and a "Show N more" line. */
export function sourcePlaceLines(count: number): number {
  return 1 + Math.min(count, ROWS_OPEN) + (count > ROWS_OPEN ? 1 : 0);
}

/**
 * The lines an area takes when it is placed in a column (2026-11-15): its heading and every source as if
 * all were open. Folds, "Show N more" and an opened row never change it, so they never move an area.
 */
export function placeLines(area: AreaGroup): number {
  return 1 + area.sources.reduce((sum, entry) => sum + sourcePlaceLines(entry.block.items.length), 0);
}

/**
 * Which column each area goes in (2026-11-15, option B): areas in today's order, each in the column with
 * the fewest lines so far (the lowest column on a tie). It takes the fold-independent counts of
 * placeLines, so only a change in the areas, their rows or the column count re-plans.
 */
export function assignColumns(lines: number[], columns: number): number[] {
  const total = Array.from({ length: Math.max(1, columns) }, () => 0);
  return lines.map((count) => {
    const col = total.indexOf(Math.min(...total));
    total[col] = (total[col] ?? 0) + count;
    return col;
  });
}

/**
 * What is open for groups the person has not toggled (2026-11-15): every area and source is open, up to
 * five rows each; the last groups fold only when the list would run past *budget* px (the visible table
 * height). A path holding an urgent item stays open whatever the budget says. *touched* gives a person's
 * own choice for a line, which always wins (null: none). With *columns* above one the budget is each
 * column's height: areas go in the column assignColumns' rule picks as the plan goes (or the one
 * *columnOf* names, when the page keeps its placement), and an area folds only when its own column
 * would run past the budget.
 *
 * *collapsed* (the Overview, 2026-11-17): the owner asked for the top-level items first, so a line the
 * person never toggled opens every area and closes every source, urgent or not; the rows show only when a
 * source is opened. The budget and columns do not enter into it. An area tab (one area) keeps the plan above.
 */
export function planOpen(
  areas: AreaGroup[],
  budget: number,
  touched: (id: string) => boolean | null = () => null,
  columns = 1,
  columnOf?: (areaId: DomainId) => number,
  collapsed = false,
): Map<string, boolean> {
  const open = new Map<string, boolean>();
  if (collapsed) {
    for (const area of areas) {
      open.set(areaFoldId(area.id), touched(areaFoldId(area.id)) ?? true);
      for (const entry of area.sources) open.set(entry.block.id, touched(entry.block.id) ?? false);
    }
    return open;
  }
  const used = Array.from({ length: Math.max(1, columns) }, () => HEIGHTS.head); // each column's heading
  const lines = used.map(() => 0);
  const rowsCost = (n: number) => Math.min(n, ROWS_OPEN) * HEIGHTS.row + (n > ROWS_OPEN ? HEIGHTS.more : 0);
  for (const area of areas) {
    const col = columnOf ? Math.min(columnOf(area.id), used.length - 1) : lines.indexOf(Math.min(...lines));
    const areaId = areaFoldId(area.id);
    used[col] = (used[col] ?? 0) + HEIGHTS.head;
    const allCost = area.sources.reduce((sum, entry) => sum + HEIGHTS.head + rowsCost(entry.block.items.length), 0);
    const fits = (used[col] ?? 0) + allCost <= budget;
    const areaOpen = touched(areaId) ?? (fits || area.urgent > 0);
    open.set(areaId, areaOpen);
    for (const entry of area.sources) {
      const id = entry.block.id;
      const sourceOpen = touched(id) ?? (fits || entry.block.urgent > 0);
      open.set(id, sourceOpen);
      if (areaOpen) used[col] = (used[col] ?? 0) + HEIGHTS.head + (sourceOpen ? rowsCost(entry.block.items.length) : 0);
    }
    lines[col] = (lines[col] ?? 0) + placeLines(area);
  }
  return open;
}

/**
 * Rough height, in px, of the tallest column of a whole open list: what decides whether a longer list has
 * outgrown the screen. Areas go in the column *columnOf* names, or by planOpen's own rule (fewest lines so far).
 */
export function listCost(areas: AreaGroup[], columns = 1, columnOf?: (areaId: DomainId) => number): number {
  const rowsCost = (n: number) => Math.min(n, ROWS_OPEN) * HEIGHTS.row + (n > ROWS_OPEN ? HEIGHTS.more : 0);
  const used = Array.from({ length: Math.max(1, columns) }, () => HEIGHTS.head);
  const lines = used.map(() => 0);
  for (const area of areas) {
    const col = columnOf ? Math.min(columnOf(area.id), used.length - 1) : lines.indexOf(Math.min(...lines));
    used[col] =
      (used[col] ?? 0) +
      HEIGHTS.head +
      area.sources.reduce((sum, entry) => sum + HEIGHTS.head + rowsCost(entry.block.items.length), 0);
    lines[col] = (lines[col] ?? 0) + placeLines(area);
  }
  return Math.max(...used);
}

/** The ids of every area and source: the open plan is made again only when this changes. */
export function planKey(areas: AreaGroup[]): string {
  return areas.map((area) => `${areaFoldId(area.id)}:${area.sources.map((entry) => entry.block.id).join(",")}`).join("|");
}

/** What is open on first sight of the Overview: every area, no source. */
export function defaultOpenIds(areas: AreaGroup[]): Map<string, boolean> {
  return planOpen(areas, 0, () => null, 1, undefined, true);
}

/** The sources the newest answer says it could not read ("Git repos could not be read ..."). */
export function unreadSources(data: Pick<AttentionOverview, "unknown"> | null): Set<AttentionSource> {
  const out = new Set<AttentionSource>();
  for (const words of data?.unknown ?? []) {
    for (const source of Object.keys(SOURCE_NAMES) as AttentionSource[]) {
      if (words.startsWith(SOURCE_NAMES[source])) out.add(source);
    }
  }
  return out;
}

/** The words of each "could not be read" line, with the source each is about (null: none matched). */
export function unknownLines(
  data: Pick<AttentionOverview, "unknown"> | null,
): Array<{ words: string; source: AttentionSource | null }> {
  return (data?.unknown ?? []).map((words) => ({
    words,
    source: (Object.keys(SOURCE_NAMES) as AttentionSource[]).find((source) => words.startsWith(SOURCE_NAMES[source])) ?? null,
  }));
}

/** The areas that hold a source the newest answer could not read, from the attention store. */
export function unreadAreas(): Set<DomainId> {
  const out = new Set<DomainId>();
  for (const source of unreadSources(getAttention().data)) out.add(AREA_OF[source]);
  return out;
}

/** The sources of one area that could not be read. */
export function unreadIn(area: DomainId): AttentionSource[] {
  return [...unreadSources(getAttention().data)].filter((source) => AREA_OF[source] === area);
}

/** "Git repos and Box host": names for a sentence. */
export function sourceList(sources: AttentionSource[]): string {
  const names = sources.map((source) => SOURCE_NAMES[source]);
  return names.length > 1 ? `${names.slice(0, -1).join(", ")} and ${names[names.length - 1]}` : (names[0] ?? "");
}

/** Needs you counts for one area, by the store's one rule (null before the first read). */
export function areaCounts(area: DomainId): { urgent: number; review: number } | null {
  let urgent = 0;
  let total = 0;
  for (const source of sourcesOfArea(area)) {
    const all = attentionCount(source);
    if (all === null) return null;
    total += all;
    urgent += urgentCount(source) ?? 0;
  }
  return { urgent, review: total - urgent };
}

/**
 * One source's counts by the page's one rule (the server's by_source), or null before the first read.
 * The list's source line and the wall's card header both count with this (2026-11-17).
 */
export function sourceCounts(source: AttentionSource): { urgent: number; review: number } | null {
  const urgent = urgentCount(source);
  const all = attentionCount(source);
  return urgent !== null && all !== null ? { urgent, review: all - urgent } : null;
}

/** The Needs you sources a card owns (the server's answer), or none before it came. */
export function cardSources(card: string): AttentionSource[] {
  return (getCards()?.cards.find((row) => row.id === card)?.sources ?? []) as AttentionSource[];
}

/** The counts for the source or sources a card owns, summed by sourceCounts; null when it owns none or none has read. */
export function cardCounts(card: string): { urgent: number; review: number } | null {
  let urgent = 0;
  let review = 0;
  let any = false;
  for (const source of cardSources(card)) {
    const counts = sourceCounts(source);
    if (!counts) continue;
    any = true;
    urgent += counts.urgent;
    review += counts.review;
  }
  return any ? { urgent, review } : null;
}

/** How many of an area's counted items the answer left out to fit the page (by_source against the rows). */
export function areaCut(area: DomainId): number {
  const { data } = getAttention();
  const counts = data ? areaCounts(area) : null;
  if (!data || !counts) return 0;
  const listed = data.items.filter((item) => AREA_OF[item.source] === area && !uncounted(item)).length;
  return Math.max(0, counts.urgent + counts.review - listed);
}

/** How many of one source's counted items the answer left out (its by_source total against its listed rows). */
export function sourceCut(source: AttentionSource): number {
  const { data } = getAttention();
  const total = data ? attentionCount(source) : null;
  if (!data || total === null) return 0;
  const listed = data.items.filter((item) => item.source === source && !uncounted(item)).length;
  return Math.max(0, total - listed);
}

/**
 * The cards of an area that are connected: on, not hidden, and not "not set up". *present* names
 * the cards the page was built with (a page that never had a card has nothing to connect).
 */
export function connectedCards(area: DomainId, present?: ReadonlySet<string>): string[] {
  const domain = DOMAINS.find((d) => d.id === area);
  if (!domain) return [];
  return domain.cards.filter((id) => {
    if (present && !present.has(id)) return false;
    const tiles = CARD_TILES[id as keyof typeof CARD_TILES] ?? [];
    const notSetUp = tiles.length > 0 && tiles.every((tile) => getTile(tile)?.notSetUp === true);
    return !cardGone(id) && !notSetUp;
  });
}

export interface TabSpec {
  id: "overview" | DomainId | "activity" | "services";
  label: string;
}

/**
 * The tabs: Overview, one per area that has a connected card, Activity (when the server has the
 * Done route), Services. A server with four or
 * fewer connected cards gets no area tabs (its cards sit on the Overview under the table).
 */
export function tabsFor(connected: Record<DomainId, string[]>, withActivity = false): TabSpec[] {
  const total = DOMAINS.reduce((sum, domain) => sum + connected[domain.id].length, 0);
  const tabs: TabSpec[] = [{ id: "overview", label: "Overview" }];
  if (total > AREA_TAB_MIN_CARDS) {
    for (const domain of DOMAINS) {
      if (connected[domain.id].length) tabs.push({ id: domain.id, label: domain.label });
    }
  }
  // Activity (D2 phase 2) sits after the area tabs and only when the server's Done route answered.
  if (withActivity) tabs.push({ id: "activity", label: "Activity" });
  tabs.push({ id: "services", label: "Services" });
  return tabs;
}

export function currentConnected(present?: ReadonlySet<string>): Record<DomainId, string[]> {
  return Object.fromEntries(DOMAINS.map((domain) => [domain.id, connectedCards(domain.id, present)])) as Record<DomainId, string[]>;
}

// ── age and checked ─────────────────────────────────────────────────────────

/** "12 min", "5 h", "3 d": how long ago, in the shortest words. */
export function sinceText(ms: number, now = Date.now()): string {
  const gap = Math.max(0, now - ms);
  const minutes = Math.floor(gap / 60_000);
  if (minutes < 1) return "now";
  if (minutes < 60) return `${minutes} min`;
  const hours = Math.floor(minutes / 60);
  if (hours < 48) return `${hours} h`;
  return `${Math.floor(hours / 24)} d`;
}

/** "2 min ago", or "just now". */
export function agoText(iso: string | undefined, now = Date.now()): string {
  const at = iso ? Date.parse(iso) : Number.NaN;
  if (Number.isNaN(at)) return "";
  const text = sinceText(at, now);
  return text === "now" ? "just now" : `${text} ago`;
}

// ── the opened row's facts ──────────────────────────────────────────────────

export interface Fact {
  label: string;
  value: string;
}

/** Only facts the item carries today, in one row of equal boxes. */
export function factsOf(
  item: AttentionItem,
  extra: Fact[] = [],
  backOn: (days: number) => string,
): Fact[] {
  const facts: Fact[] = [{ label: "Source", value: SOURCE_NAMES[item.source] }];
  const kind = kindName(item);
  if (kind) facts.push({ label: "Group", value: kind });
  facts.push(...extra);
  if (item.dismissible && item.back_if) facts.push({ label: "Comes back if", value: item.back_if });
  if (item.dismissible && item.expires_days > 0) {
    facts.push({ label: "Dismiss lasts", value: `${item.expires_days} ${item.expires_days === 1 ? "day" : "days"} (to ${backOn(item.expires_days)})` });
  }
  if (item.back) facts.push({ label: "Came back", value: item.back });
  return facts;
}

/**
 * The opened row's evidence line: what the source reports and when it was read. *mergeLine* is
 * what a Merge click last read from GitHub for a pull request, when there is one.
 */
export function evidenceText(item: AttentionItem, readClock: string, mergeLine: string | null): string {
  const said = mergeLine ?? (item.meta || item.title);
  // The period goes on only when the text does not already end a sentence (2026-11-18: "failed.." came out twice).
  const stop = /[.?!]$/.test(said.trimEnd()) ? "" : ".";
  return `${SOURCE_NAMES[item.source]} reports: ${said}${stop}${readClock ? ` Read at ${readClock}.` : ""}`;
}
