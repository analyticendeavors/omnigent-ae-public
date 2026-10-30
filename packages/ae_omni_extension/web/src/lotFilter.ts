// Search and filters over the Parking Lot (2026-09-25,
// https://github.com/analyticendeavors/omnigent-ae/issues/28). The owner mostly
// reads the lot on their phone, so the bar is one search
// field and a Filters button; the button opens a sheet of chips (quadrant,
// resurface date, tag, repo) inline under the bar, never a modal.
//
// Semantics: the text matches the title and NEXT, every word somewhere,
// case-insensitive. Inside a group the chips are OR (hotfix or admin);
// across groups AND (a hotfix tagged ae). "Overdue" is a resurface date
// before today, "This week" today through six days on, both reckoned in the
// lot's timezone (`/v1/ae/capacity`'s `today`) when the server said it.
//
// The last filter is remembered in the SDK's `storage.user` (key
// `lot.filter`), which the host keeps in the browser's IndexedDB, so it is
// per device: the phone and the desk each keep their own. In memory when
// that is unavailable.
//
// The bar is built once and kept across the 20 s refresh (like the Park
// form), so a half-typed search never loses focus or text.

import type { ExtensionContext } from "@omnigent/extension-sdk";
import { type LotTask, type Quadrant, quadrantName } from "./api";
import { el } from "./dom";
import { filterIcon, quadrantIcon, repoIcon, searchIcon } from "./icons";
import { tagChip, visibleTags } from "./tags";

export type DateFilter = "overdue" | "week";

export interface LotFilter {
  text: string;
  quadrants: Quadrant[];
  dates: DateFilter[];
  tags: string[];
  repos: string[];
}

export const FILTER_KEY = "lot.filter";

const QUADRANTS: readonly Quadrant[] = ["hotfix", "feature", "admin"];
const DATES: readonly [DateFilter, string][] = [
  ["overdue", "Overdue"],
  ["week", "This week"],
];

export function emptyFilter(): LotFilter {
  return { text: "", quadrants: [], dates: [], tags: [], repos: [] };
}

function strings(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((item): item is string => typeof item === "string") : [];
}

/** A stored filter read back defensively: unknown values are dropped, not trusted. */
export function normalizeFilter(raw: unknown): LotFilter {
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) return emptyFilter();
  const value = raw as Record<string, unknown>;
  return {
    text: typeof value.text === "string" ? value.text : "",
    quadrants: strings(value.quadrants).filter((q): q is Quadrant =>
      QUADRANTS.includes(q as Quadrant),
    ),
    dates: strings(value.dates).filter((d): d is DateFilter => d === "overdue" || d === "week"),
    tags: strings(value.tags).map((tag) => tag.toLowerCase()),
    repos: strings(value.repos),
  };
}

/** How many chips are on (the text is counted apart: it shows in its own field). */
export function chipCount(filter: LotFilter): number {
  return filter.quadrants.length + filter.dates.length + filter.tags.length + filter.repos.length;
}

export function isFiltering(filter: LotFilter): boolean {
  return filter.text.trim() !== "" || chipCount(filter) > 0;
}

/** Today as `YYYY-MM-DD` on this device, when the server did not say. */
export function localToday(now = new Date()): string {
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())}`;
}

/** `YYYY-MM-DD` plus *days*, as calendar dates (no time zone in play). */
export function addDays(day: string, days: number): string {
  const [year, month, date] = day.split("-").map(Number);
  const next = new Date(Date.UTC(year, month - 1, date + days));
  return next.toISOString().slice(0, 10);
}

function dateMatches(resurfaces: string | null, today: string, which: DateFilter): boolean {
  if (!resurfaces) return false;
  const day = resurfaces.slice(0, 10);
  if (which === "overdue") return day < today;
  return day >= today && day <= addDays(today, 6);
}

export function matchesTask(task: LotTask, filter: LotFilter, today: string): boolean {
  const words = filter.text.toLowerCase().split(/\s+/).filter(Boolean);
  if (words.length > 0) {
    const haystack = `${task.title ?? task.id} ${task.next ?? ""}`.toLowerCase();
    if (!words.every((word) => haystack.includes(word))) return false;
  }
  if (filter.quadrants.length > 0) {
    const quadrant = quadrantName(task.quadrant);
    if (!quadrant || !filter.quadrants.includes(quadrant)) return false;
  }
  if (filter.dates.length > 0) {
    if (!filter.dates.some((which) => dateMatches(task.resurfaces, today, which))) return false;
  }
  if (filter.tags.length > 0) {
    const tags = new Set((task.tags ?? []).map((tag) => tag.toLowerCase()));
    if (!filter.tags.some((tag) => tags.has(tag))) return false;
  }
  if (filter.repos.length > 0) {
    if (!task.repo || !filter.repos.includes(task.repo)) return false;
  }
  return true;
}

export function filterTasks(tasks: LotTask[], filter: LotFilter, today: string): LotTask[] {
  return tasks.filter((task) => matchesTask(task, filter, today));
}

/** Most used first, then by name; what the lot actually carries, not the whole vocabulary. */
function byUse(values: string[]): string[] {
  const counts = new Map<string, number>();
  for (const value of values) counts.set(value, (counts.get(value) ?? 0) + 1);
  return [...counts.keys()].sort(
    (left, right) => counts.get(right)! - counts.get(left)! || left.localeCompare(right),
  );
}

/** The tag and repo chips the sheet offers for this lot. */
export function lotOptions(tasks: LotTask[]): { tags: string[]; repos: string[] } {
  return {
    tags: byUse(tasks.flatMap((task) => visibleTags(task.tags).map((tag) => tag.toLowerCase()))),
    repos: byUse(tasks.map((task) => task.repo).filter((repo): repo is string => Boolean(repo))),
  };
}

// ── page state ─────────────────────────────────────────────────────────

let current: LotFilter = emptyFilter();
let persist: (() => void) | null = null;
let persistTimer: ReturnType<typeof setTimeout> | null = null;

/** Read the remembered filter; the page works the same without it. */
export async function loadLotFilter(context: ExtensionContext): Promise<void> {
  persist = () => {
    context.storage.user.set(FILTER_KEY, current).catch(() => {
      // storage.user unavailable: remembered in memory until the page closes.
    });
  };
  try {
    current = normalizeFilter(await context.storage.user.get<unknown>(FILTER_KEY));
  } catch {
    current = emptyFilter();
  }
}

export function currentFilter(): LotFilter {
  return current;
}

/** For tests: forget the filter and the bar. */
export function resetLotFilter(): void {
  current = emptyFilter();
  persist = null;
  if (persistTimer) clearTimeout(persistTimer);
  persistTimer = null;
  bar = null;
}

function save(immediate = false): void {
  if (persistTimer) clearTimeout(persistTimer);
  persistTimer = null;
  if (immediate) persist?.();
  // Typing writes once it pauses; the host rate-limits storage writes.
  else persistTimer = setTimeout(() => persist?.(), 400);
}

function toggled<T>(list: T[], value: T): T[] {
  return list.includes(value) ? list.filter((item) => item !== value) : [...list, value];
}

// ── the bar ────────────────────────────────────────────────────────────

export interface FilterBar {
  node: HTMLElement;
  /**
   * Refill the sheet's chips from *tasks*, every task in the lot, and the
   * "n of m" line for the *shown* of them.
   */
  update: (tasks: LotTask[], shown: number) => void;
  /** Toggle one tag (a tag chip on a card calls this). */
  toggleTag: (tag: string) => void;
  /**
   * Call before the section is rebuilt; the returned function puts the focus
   * and caret back in the search field if they were there (moving a focused
   * input to a new parent blurs it, and on the phone that drops the keyboard).
   */
  holdFocus: () => () => void;
}

let bar: FilterBar | null = null;

function option(
  label: Node[],
  pressed: boolean,
  title: string,
  onClick: () => void,
  extraClass = "",
): HTMLButtonElement {
  const node = el("button", {
    type: "button",
    class: `filter-opt${extraClass ? ` ${extraClass}` : ""}`,
    "aria-pressed": String(pressed),
    title,
  });
  node.append(...label);
  node.addEventListener("click", onClick);
  return node;
}

function group(title: string, children: HTMLElement[]): HTMLElement | null {
  if (children.length === 0) return null;
  return el(
    "div",
    { class: "filter-group", role: "group", "aria-label": title },
    el("span", { class: "filter-group-h" }, title),
    el("div", { class: "filter-opts" }, ...children),
  );
}

/** The bar, built once per page; *onChange* re-renders the cards. */
export function filterBar(onChange: () => void): FilterBar {
  if (bar) {
    barChanged = onChange;
    return bar;
  }
  barChanged = onChange;
  const input = el("input", {
    type: "search",
    class: "lot-search-input",
    placeholder: "Search the lot",
    "aria-label": "Search the Parking Lot by title and NEXT",
    autocomplete: "off",
    enterkeyhint: "search",
  });
  input.value = current.text;
  const toggle = el("button", {
    type: "button",
    class: "btn lot-filter-toggle",
    "aria-expanded": "false",
    "aria-controls": "lot-filter-sheet",
    title: "Filter by quadrant, resurface date, tag and repo",
  });
  const badge = el("span", { class: "count" });
  toggle.append(filterIcon(), el("span", { class: "btn-label" }, "Filters"), badge);
  const sheet = el("div", { class: "lot-filter-sheet", id: "lot-filter-sheet", hidden: true });
  const summary = el("p", { class: "lot-filter-summary meta", "aria-live": "polite" });
  const node = el(
    "div",
    { class: "lot-filter" },
    el(
      "div",
      { class: "lot-filter-bar" },
      el("label", { class: "lot-search" }, searchIcon(), input),
      toggle,
    ),
    sheet,
    summary,
  );

  let lastOptions = "";

  const changed = (immediate: boolean) => {
    save(immediate);
    barChanged();
  };
  const set = (next: Partial<LotFilter>) => {
    current = { ...current, ...next };
    changed(true);
  };

  input.addEventListener("input", () => {
    current = { ...current, text: input.value };
    changed(false);
  });
  toggle.addEventListener("click", () => {
    const open = Boolean(sheet.hidden);
    sheet.hidden = !open;
    toggle.setAttribute("aria-expanded", String(open));
    toggle.classList.toggle("is-open", open);
  });

  const clearButton = (text: string) => {
    const clear = el("button", { type: "button", class: "link-btn" }, text);
    clear.addEventListener("click", () => {
      current = emptyFilter();
      input.value = "";
      changed(true);
    });
    return clear;
  };

  const fillSheet = (tasks: LotTask[]) => {
    const options = lotOptions(tasks);
    const key = JSON.stringify([options, current]);
    if (key === lastOptions) return;
    lastOptions = key;
    // Keep chips that are on but no longer in the lot, so they can be turned off.
    const tags = [...options.tags, ...current.tags.filter((t) => !options.tags.includes(t))];
    const repos = [...options.repos, ...current.repos.filter((r) => !options.repos.includes(r))];
    const groups = [
      group(
        "Quadrant",
        QUADRANTS.map((quadrant) =>
          option(
            [quadrantIcon(quadrant), el("span", {}, quadrant)].filter(Boolean) as Node[],
            current.quadrants.includes(quadrant),
            `Only ${quadrant}`,
            () => set({ quadrants: toggled(current.quadrants, quadrant) }),
            `filter-q-${quadrant}`,
          ),
        ),
      ),
      group(
        "Resurfaces",
        DATES.map(([which, text]) =>
          option(
            [el("span", {}, text)],
            current.dates.includes(which),
            which === "overdue" ? "Resurface date before today" : "Resurfaces in the next 7 days",
            () => set({ dates: toggled(current.dates, which) }),
          ),
        ),
      ),
      group(
        "Tags",
        tags.map((tag) =>
          tagChip(tag, {
            pressed: current.tags.includes(tag),
            onClick: () => set({ tags: toggled(current.tags, tag) }),
          }),
        ),
      ),
      group(
        "Repo",
        repos.map((repo) =>
          option(
            [repoIcon(), el("span", { class: "filter-repo" }, repo)],
            current.repos.includes(repo),
            `Only ${repo}`,
            () => set({ repos: toggled(current.repos, repo) }),
          ),
        ),
      ),
    ].filter((node): node is HTMLElement => node !== null);
    const actions = el("div", { class: "filter-actions" }, clearButton("Clear filters"));
    sheet.replaceChildren(...groups, actions);
  };

  const update = (tasks: LotTask[], shown: number) => {
    fillSheet(tasks);
    const chips = chipCount(current);
    badge.textContent = chips > 0 ? String(chips) : "";
    badge.hidden = chips === 0;
    toggle.classList.toggle("is-active", chips > 0);
    if (input.value !== current.text && document.activeElement !== input) {
      input.value = current.text;
    }
    if (isFiltering(current)) {
      summary.replaceChildren(`Showing ${shown} of ${tasks.length}. `, clearButton("Clear"));
      summary.hidden = false;
    } else {
      summary.replaceChildren();
      summary.hidden = true;
    }
  };

  const toggleTag = (tag: string) => {
    set({ tags: toggled(current.tags, tag.toLowerCase()) });
  };

  const holdFocus = () => {
    if (document.activeElement !== input) return () => undefined;
    const start = input.selectionStart;
    const end = input.selectionEnd;
    return () => {
      if (document.activeElement === input) return;
      input.focus({ preventScroll: true });
      if (start !== null && end !== null) input.setSelectionRange(start, end);
    };
  };

  bar = { node, update, toggleTag, holdFocus };
  return bar;
}

// The bar outlives a render; the latest render's callback is the one to call.
let barChanged: () => void = () => undefined;
