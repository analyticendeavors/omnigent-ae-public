// The Social view on a phone (2026-11-06): the Week tab is one agenda list instead of seven day
// rows, and a post is the same card in the Week list and under a picked day of the Month.
//
// The list covers 14 days from the selected week's Monday ("this week and next"). Groups, in
// order: OVERDUE (every planned post whose time has passed, wherever it falls; only when the
// range holds today), TODAY, then one small heading per later day that has posts. Days with
// nothing are left out.

import type { SocialSlot, SocialView } from "./api";
import {
  addDays,
  type DayKey,
  dayKeyOf,
  dayLabel,
  type Filter,
  matches,
  mondayOf,
  shortDate,
} from "./calendar";
import { el } from "./dom";
import { checkIcon, linkedinIcon, youtubeIcon } from "./icons";
import { CHANNEL_NAMES, slotTitle, zonedParts } from "./socialWords";

/** Days the phone's Week list covers. */
export const AGENDA_DAYS = 14;

export interface AgendaGroup {
  kind: "overdue" | "today" | "day" | "earlier";
  /** The day, for "today" and "day"; null for overdue and earlier. */
  day: DayKey | null;
  label: string;
  slots: SocialSlot[];
}

/** "Nov 2 to 15" or "Nov 23 to Dec 6": the Week list's range label. */
export function agendaRange(day: DayKey): string {
  const first = mondayOf(day);
  const last = addDays(first, AGENDA_DAYS - 1);
  const end = first.slice(0, 7) === last.slice(0, 7) ? String(Number(last.slice(8, 10))) : shortDate(last);
  return `${shortDate(first)} to ${end}`;
}

/** "Mon, Nov 9" */
export const agendaDay = (key: DayKey): string => dayLabel(key).replace(" ", ", ");

const byDue = (a: SocialSlot, b: SocialSlot) => Date.parse(a.due) - Date.parse(b.due);

export function agendaGroups(slots: SocialSlot[], tz: string, filter: Filter, day: DayKey, now: number): AgendaGroup[] {
  const first = mondayOf(day);
  const last = addDays(first, AGENDA_DAYS - 1);
  const today = dayKeyOf(now, tz);
  const shown = slots.filter((slot) => matches(slot, filter)).sort(byDue);
  const groups: AgendaGroup[] = [];
  const hasToday = today >= first && today <= last;
  const late = (slot: SocialSlot) => slot.state === "planned" && Date.parse(slot.due) < now;
  if (hasToday) {
    const rows = shown.filter(late);
    if (rows.length) groups.push({ kind: "overdue", day: null, label: "Overdue", slots: rows });
  }
  const byDay = new Map<DayKey, SocialSlot[]>();
  for (const slot of shown) {
    const key = dayKeyOf(Date.parse(slot.due), tz);
    if (key < first || key > last) continue;
    if (hasToday && late(slot)) continue; // already under Overdue
    byDay.set(key, [...(byDay.get(key) ?? []), slot]);
  }
  // Today first, then the days to come; then, when the range holds today, the days already past
  // (posted or skipped posts) under one muted EARLIER heading, oldest first. A range wholly in
  // the past or future lists its days in order.
  const keys = [...byDay.keys()].sort();
  const earlier = hasToday ? keys.filter((k) => k < today) : [];
  const days = hasToday ? [...keys.filter((k) => k === today), ...keys.filter((k) => k > today)] : keys;
  for (const key of days) {
    const rows = byDay.get(key) ?? [];
    groups.push(
      key === today
        ? { kind: "today", day: key, label: `Today, ${dayLabel(key)}`, slots: rows }
        : { kind: "day", day: key, label: agendaDay(key), slots: rows },
    );
  }
  if (earlier.length) groups.push({ kind: "earlier", day: null, label: "Earlier", slots: earlier.flatMap((key) => byDay.get(key) ?? []) });
  return groups;
}

/** The time for a card's right edge: the clock time, or how long ago for an overdue post. */
export function cardWhen(slot: SocialSlot, now: number, tz: string): string {
  const ms = Date.parse(slot.due);
  const parts = zonedParts(ms, tz);
  if (slot.state === "planned" && ms < now) return now - ms < 6 * 86_400_000 ? parts.weekday : parts.date;
  return parts.time;
}

export interface CardOptions {
  view: SocialView;
  now: number;
  /** The post open in the editor, outlined. */
  openSlot: string | null;
  /** Name the weekday in the time (the EARLIER group mixes days). */
  withDay?: boolean;
  onOpen: (slot: SocialSlot) => void;
}

/** One post as a card: platform badge, title, draft state, time. Tapping it opens the editor. */
export function agendaCard(slot: SocialSlot, options: CardOptions): HTMLElement {
  const { view, now } = options;
  const late = slot.state === "planned" && Date.parse(slot.due) < now;
  const linkedin = slot.platform === "linkedin";
  const channel = slot.channel && slot.kind !== "tool" && slot.kind !== "expert" ? (CHANNEL_NAMES[slot.channel] ?? slot.channel) : "";
  const state =
    slot.state === "posted"
      ? el("span", { class: "sv-ac-ok" }, checkIcon(), "Posted")
      : slot.state === "skipped"
        ? el("span", {}, "Skipped")
        : el("span", { class: slot.has_draft ? "" : "sv-ac-nd" }, slot.has_draft ? "Draft ready" : "No draft");
  const card = el(
    "button",
    {
      type: "button",
      class: `sv-ac${late ? " is-late" : ""} st-${slot.state}${options.openSlot === slot.id ? " is-open" : ""}`,
      "data-slot": slot.id,
    },
    el(
      "span",
      { class: `sv-ac-badge ${linkedin ? "b-li" : "b-yt"}`, title: linkedin ? "LinkedIn" : "YouTube", role: "img", "aria-label": linkedin ? "LinkedIn" : "YouTube" },
      linkedin ? linkedinIcon(14) : youtubeIcon(14),
    ),
    el(
      "span",
      { class: "sv-ac-text" },
      el("span", { class: "sv-ac-title" }, slotTitle(slot)),
      el("span", { class: "sv-ac-sub" }, state, channel ? ` · ${channel}` : "", slot.skippable && slot.state === "planned" ? " · optional" : ""),
    ),
    el("span", { class: "sv-ac-time" }, options.withDay ? `${zonedParts(Date.parse(slot.due), view.rules.tz).weekday} ${cardWhen(slot, now, view.rules.tz)}` : cardWhen(slot, now, view.rules.tz)),
  );
  card.addEventListener("click", () => options.onOpen(slot));
  return card;
}

export interface AgendaOptions extends CardOptions {
  filter: Filter;
  day: DayKey;
}

/** The Week list: groups of cards, or one line when the range has nothing. */
export function drawAgenda(options: AgendaOptions): HTMLElement {
  const { view, filter, day, now } = options;
  const groups = agendaGroups(view.slots, view.rules.tz, filter, day, now);
  const root = el("div", { class: "sv-agenda", "aria-label": `Posts from ${agendaRange(day)}` });
  if (!groups.length) {
    root.appendChild(el("p", { class: "meta sv-agenda-empty" }, `Nothing planned from ${agendaRange(day)}.`));
    return root;
  }
  for (const group of groups) {
    const count = group.slots.length;
    root.appendChild(
      el(
        "div",
        { class: `sv-ag-head g-${group.kind}` },
        el("span", {}, group.kind === "overdue" || group.kind === "earlier" ? group.label.toUpperCase() : group.label),
        group.kind === "day" ? el("span", { class: "sv-ag-count" }, `${count} ${count === 1 ? "post" : "posts"}`) : null,
        group.kind === "overdue" || group.kind === "earlier" ? el("span", { class: "sv-ag-count" }, `${count}`) : null,
      ),
    );
    for (const slot of group.slots) root.appendChild(agendaCard(slot, group.kind === "earlier" ? { ...options, withDay: true } : options));
  }
  return root;
}
