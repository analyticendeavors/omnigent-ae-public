// The Social view's calendar (2026-11-06, part 2): a week of seven day columns or a month grid,
// every post a chip with its kind, title, time, draft state and channel.
//
// Days are plain "YYYY-MM-DD" keys, so date arithmetic never depends on the browser's zone; a
// post's key comes from the rules' zone (`dayKeyOf`), the same clock the server plans on. Arrow
// keys move between days (Left and Right a day, Up and Down a week in the month, and in the
// week view too, so the same keys do the same thing), Enter opens the day's posts, Home and End
// jump to the first and last day of the week. One day cell is in the tab order at a time.

import type { SocialSlot, SocialView } from "./api";
import { el } from "./dom";
import { linkedinIcon, pencilIcon, toolIcon, youtubeIcon } from "./icons";
import { CHANNEL_NAMES, slotTitle, zonedParts } from "./socialWords";

export type Filter = "all" | "linkedin" | "youtube";
export type DayKey = string;

const pad = (n: number) => String(n).padStart(2, "0");

/** The calendar date of *ms* on the wall clock of *tz* (the browser's zone when *tz* is unknown). */
export function dayKeyOf(ms: number, tz: string): DayKey {
  const part = (zone: string | undefined) => {
    const out: Record<string, string> = {};
    for (const p of new Intl.DateTimeFormat("en-US", {
      timeZone: zone,
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
    }).formatToParts(ms)) {
      out[p.type] = p.value;
    }
    return `${out.year}-${out.month}-${out.day}`;
  };
  try {
    return part(tz);
  } catch {
    return part(undefined);
  }
}

const toDate = (key: DayKey) => {
  const [y, m, d] = key.split("-").map(Number);
  return new Date(Date.UTC(y, m - 1, d));
};
const fromDate = (date: Date): DayKey =>
  `${date.getUTCFullYear()}-${pad(date.getUTCMonth() + 1)}-${pad(date.getUTCDate())}`;

export function addDays(key: DayKey, n: number): DayKey {
  const date = toDate(key);
  date.setUTCDate(date.getUTCDate() + n);
  return fromDate(date);
}

/** Monday of the week holding *key*. */
export function mondayOf(key: DayKey): DayKey {
  const weekday = (toDate(key).getUTCDay() + 6) % 7;
  return addDays(key, -weekday);
}

export const weekKeys = (key: DayKey): DayKey[] => {
  const monday = mondayOf(key);
  return Array.from({ length: 7 }, (_, n) => addDays(monday, n));
};

/** Whole weeks (Monday first) that cover the month holding *key*. */
export function monthWeeks(key: DayKey): DayKey[][] {
  const first = `${key.slice(0, 7)}-01`;
  const next = toDate(first);
  next.setUTCMonth(next.getUTCMonth() + 1);
  const last = addDays(fromDate(next), -1);
  const weeks: DayKey[][] = [];
  for (let monday = mondayOf(first); monday <= last; monday = addDays(monday, 7)) {
    weeks.push(weekKeys(monday));
  }
  return weeks;
}

export function addMonths(key: DayKey, n: number): DayKey {
  const date = toDate(`${key.slice(0, 7)}-01`);
  date.setUTCMonth(date.getUTCMonth() + n);
  return fromDate(date);
}

const MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"];
const SHORT_MONTHS = MONTHS.map((name) => name.slice(0, 3));
const WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];

export const monthName = (key: DayKey) => `${MONTHS[Number(key.slice(5, 7)) - 1]} ${key.slice(0, 4)}`;
export const shortDate = (key: DayKey) => `${SHORT_MONTHS[Number(key.slice(5, 7)) - 1]} ${Number(key.slice(8, 10))}`;

/** "Nov 9 to 15, 2026" or "Nov 28 to Dec 4, 2026". */
export function weekRange(key: DayKey): string {
  const days = weekKeys(key);
  const [a, b] = [days[0], days[6]];
  const end = a.slice(0, 7) === b.slice(0, 7) ? String(Number(b.slice(8, 10))) : shortDate(b);
  return `${shortDate(a)} to ${end}, ${b.slice(0, 4)}`;
}

export const dayLabel = (key: DayKey) => `${WEEKDAYS[(toDate(key).getUTCDay() + 6) % 7]} ${shortDate(key)}`;

export function matches(slot: SocialSlot, filter: Filter): boolean {
  return filter === "all" || (filter === "linkedin" ? slot.platform === "linkedin" : slot.platform === "yt_community");
}

/** The posts by day, in due order, for the filter. */
export function slotsByDay(slots: SocialSlot[], tz: string, filter: Filter): Map<DayKey, SocialSlot[]> {
  const out = new Map<DayKey, SocialSlot[]>();
  const rows = slots.filter((slot) => matches(slot, filter)).sort((a, b) => Date.parse(a.due) - Date.parse(b.due));
  for (const slot of rows) {
    const key = dayKeyOf(Date.parse(slot.due), tz);
    out.set(key, [...(out.get(key) ?? []), slot]);
  }
  return out;
}

/** Where an arrow key moves the focus from *key*; null for a key the grid does not own. */
export function moveFocus(key: DayKey, pressed: string): DayKey | null {
  switch (pressed) {
    case "ArrowLeft":
      return addDays(key, -1);
    case "ArrowRight":
      return addDays(key, 1);
    case "ArrowUp":
      return addDays(key, -7);
    case "ArrowDown":
      return addDays(key, 7);
    case "Home":
      return mondayOf(key);
    case "End":
      return addDays(mondayOf(key), 6);
    default:
      return null;
  }
}

export interface CalendarOptions {
  mode: "week" | "month";
  /** A phone's month (2026-11-06): small squares with a dot per post instead of chips. */
  dots?: boolean;
  view: SocialView;
  filter: Filter;
  /** The day the grid is on (focus and the week or month shown). */
  day: DayKey;
  /** The open day, outlined. */
  selected: DayKey | null;
  /** The post open in the editor, outlined. */
  openSlot: string | null;
  now: number;
  onDay: (day: DayKey) => void;
  onOpenDay: (day: DayKey) => void;
  onOpenSlot: (slot: SocialSlot) => void;
}

function kindLabel(slot: SocialSlot): string {
  if (slot.platform === "yt_community") return "Community";
  if (slot.kind === "tool") return "Tool";
  if (slot.kind === "expert") return "Expert";
  return "LinkedIn";
}

const kindClass = (slot: SocialSlot) =>
  slot.platform === "yt_community" ? "k-community" : slot.kind === "tool" ? "k-tool" : slot.kind === "expert" ? "k-expert" : "k-linkedin";

function kindIcon(slot: SocialSlot): SVGElement {
  if (slot.platform === "yt_community") return youtubeIcon(12);
  if (slot.kind === "tool") return toolIcon(12);
  if (slot.kind === "expert") return pencilIcon();
  return linkedinIcon(12);
}

/** One post's chip: kind, title, time and draft state, channel for an upload's post. */
export function chip(slot: SocialSlot, options: CalendarOptions, compact: boolean): HTMLElement {
  const tz = options.view.rules.tz;
  const late = slot.state === "planned" && Date.parse(slot.due) < options.now;
  const draft =
    slot.state === "posted" ? "posted" : slot.state === "skipped" ? "skipped" : slot.has_draft ? "draft" : late ? "overdue" : "no draft";
  const channel = slot.channel && slot.kind !== "tool" && slot.kind !== "expert" ? (CHANNEL_NAMES[slot.channel] ?? slot.channel) : "";
  const time = zonedParts(Date.parse(slot.due), tz).time;
  const node = el(
    "button",
    {
      type: "button",
      class: `sv-chip ${kindClass(slot)} st-${slot.state}${slot.skippable ? " is-optional" : ""}${late ? " is-late" : ""}${options.openSlot === slot.id ? " is-open" : ""}`,
      tabindex: "-1",
      "data-slot": slot.id,
      title: `${slotTitle(slot)} (${kindLabel(slot)}, ${time}, ${draft}${channel ? `, ${channel}` : ""})`,
    },
    el("span", { class: "sv-chip-kind" }, kindIcon(slot), el("span", { class: "sv-chip-kind-t" }, kindLabel(slot))),
    el("span", { class: "sv-chip-title" }, slot.title ?? slot.tool ?? slot.topic ?? slotTitle(slot)),
    el("span", { class: "sv-chip-meta" }, [time, draft, channel].filter(Boolean).join(" · ")),
  );
  if (compact) node.classList.add("is-compact");
  node.addEventListener("click", (event) => {
    event.stopPropagation();
    options.onOpenSlot(slot);
  });
  return node;
}

/** The week or month grid: roving focus, arrows move, Enter opens the day. */
export function drawCalendar(options: CalendarOptions): HTMLElement {
  const { view, filter } = options;
  const by = slotsByDay(view.slots, view.rules.tz, filter);
  const today = dayKeyOf(options.now, view.rules.tz);
  const weeks = options.mode === "week" ? [weekKeys(options.day)] : monthWeeks(options.day);
  const grid = el("div", {
    class: `sv-cal sv-cal-${options.mode}${options.dots ? " sv-cal-dots" : ""}`,
    role: "grid",
    "aria-label": options.mode === "week" ? `Week of ${weekRange(options.day)}` : monthName(options.day),
  });
  const cells = new Map<DayKey, HTMLElement>();
  if (options.mode === "month") {
    grid.appendChild(
      el("div", { class: "sv-cal-head", role: "row" }, ...WEEKDAYS.map((name) => el("span", { role: "columnheader", title: name }, options.dots ? name[0] : name))),
    );
  }
  for (const days of weeks) {
    const row = el("div", { class: "sv-cal-row", role: "row" });
    for (const key of days) {
      const posts = by.get(key) ?? [];
      const outside = options.mode === "month" && key.slice(0, 7) !== options.day.slice(0, 7);
      const open = posts.filter((slot) => slot.state === "planned").length;
      const cell = el(
        "div",
        {
          class: `sv-day${key === today ? " is-today" : ""}${key === options.selected ? " is-selected" : ""}${outside ? " is-outside" : ""}`,
          role: "gridcell",
          tabindex: key === options.day ? "0" : "-1",
          "data-day": key,
          "aria-selected": key === options.selected ? "true" : "false",
          "aria-label": `${dayLabel(key)}, ${posts.length === 0 ? "no posts" : `${posts.length} ${posts.length === 1 ? "post" : "posts"}, ${open} not posted`}`,
        },
        el(
          "div",
          { class: "sv-day-top" },
          el("span", { class: "sv-day-name" }, options.mode === "week" ? WEEKDAYS[days.indexOf(key)] : ""),
          el("span", { class: "sv-day-num" }, String(Number(key.slice(8, 10)))),
        ),
      );
      if (options.dots) {
        // One dot per post: blue for LinkedIn, red for YouTube; a closed post is a hollow dot.
        cell.appendChild(
          el(
            "span",
            { class: "sv-dots" },
            ...posts.slice(0, 4).map((slot) =>
              el("span", { class: `sv-dot ${slot.platform === "linkedin" ? "d-li" : "d-yt"}${slot.state === "planned" ? "" : " is-done"}` }),
            ),
          ),
        );
      } else {
        const shown = options.mode === "month" ? posts.slice(0, 2) : posts;
        for (const slot of shown) cell.appendChild(chip(slot, options, options.mode === "month"));
        if (posts.length > shown.length) cell.appendChild(el("span", { class: "sv-more" }, `+${posts.length - shown.length} more`));
      }
      cell.addEventListener("click", () => options.onOpenDay(key));
      cells.set(key, cell);
      row.appendChild(cell);
    }
    grid.appendChild(row);
  }
  grid.addEventListener("keydown", (event) => {
    const from = (event.target as HTMLElement).closest<HTMLElement>(".sv-day")?.dataset.day;
    if (!from) return;
    if (event.key === "Enter" && event.target === cells.get(from)) {
      event.preventDefault();
      options.onOpenDay(from);
      return;
    }
    const to = moveFocus(from, event.key);
    if (!to) return;
    event.preventDefault();
    // One cell is in the tab order: the one the keyboard is on.
    const next = cells.get(to);
    if (next) {
      for (const cell of cells.values()) cell.tabIndex = -1;
      next.tabIndex = 0;
    }
    options.onDay(to);
    // The grid redraws when the day leaves the shown week or month; otherwise just move focus.
    next?.focus();
  });
  return grid;
}
