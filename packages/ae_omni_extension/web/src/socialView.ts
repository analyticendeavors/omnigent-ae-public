// The Social card's full view (2026-11-06, part 2): the Pop out button opens it over the Admin
// Center (a full sheet on a phone). Three tabs, Week, Month and Scorecard, a queue of posts not
// posted yet (overdue first), an editor for one post and a Settings fold.
//
// It is kept live while open the way the box health dialog is: the card draws, then calls
// `pushSocialView` with the newest answer, and the calendar, queue and scorecard redraw from it.
// The editor and the Settings fold are never redrawn by a read, so typing is never lost.
//
// Phone redesign (2026-11-06): the bar is two rows (tabs with a filter menu and a gear, then the
// range), the Week tab is one agenda list, the Month tab is a grid of dots with the picked day's
// posts under it, and the editor is a screen of its own with a back link. "Phone" is the width
// in socialWords.ts `PHONE_MAX`, re-read when the width changes.

import type { ExtensionContext } from "@omnigent/extension-sdk";
import type { SocialSlot, SocialView } from "./api";
import {
  addDays,
  addMonths,
  type DayKey,
  dayKeyOf,
  dayLabel,
  drawCalendar,
  type Filter,
  matches,
  monthName,
  weekRange,
} from "./calendar";
import { openDialog, type OpenDialog } from "./dialog";
import { el, notice, replaceChildren } from "./dom";
import {
  alertTriangleIcon,
  checkIcon,
  chevronLeftIcon,
  chevronRightIcon,
  filterIcon,
  linkedinIcon,
  settingsIcon,
  youtubeIcon,
} from "./icons";
import { agendaCard, agendaDay, agendaRange, drawAgenda } from "./socialAgenda";
import { closeEditor, forgetDraft, guard, openEditor, reloadEditor } from "./socialEditor";
import { attachPopover, type Popover } from "./socialMenu";
import { probeShare } from "./socialShare";
import { activeBanner, announceBanner, bannerNode, onBannerChange } from "./socialPosted";
import { drawScorecard } from "./socialScore";
import { drawSettings } from "./socialSettings";
import { dueLabel, isPhone, open, slotTitle, warnings, watchPhone } from "./socialWords";

type Tab = "week" | "month" | "score";

interface Open {
  dialog: OpenDialog;
  context: ExtensionContext;
  refresh: () => Promise<void>;
  view: SocialView;
  error: unknown;
  tab: Tab;
  filter: Filter;
  day: DayKey;
  selected: DayKey | null;
  slotId: string | null;
  settingsOpen: boolean;
  /** The page is phone-wide now (see PHONE_MAX); re-read when the width changes. */
  phone: boolean;
  filterMenu: { pop: Popover; items: Array<[Filter, HTMLButtonElement]>; button: HTMLButtonElement; dot: HTMLElement };
  gear: HTMLButtonElement;
  stop: Array<() => void>;
  parts: {
    bar: HTMLElement;
    tabs: HTMLElement;
    tools: HTMLElement;
    nav: HTMLElement;
    warns: HTMLElement;
    banner: HTMLElement;
    left: HTMLElement;
    main: HTMLElement;
    queue: HTMLElement;
    dayList: HTMLElement;
    editor: HTMLElement;
    settings: HTMLElement;
    status: HTMLElement;
    layout: HTMLElement;
  };
}

let current: Open | null = null;

/** The queue: open posts, overdue first (oldest first), then the rest by due time. */
export function queueRows(slots: SocialSlot[], now: number, filter: Filter): SocialSlot[] {
  const rows = slots.filter((slot) => open(slot) && matches(slot, filter)).sort((a, b) => Date.parse(a.due) - Date.parse(b.due));
  return [...rows.filter((s) => Date.parse(s.due) < now), ...rows.filter((s) => Date.parse(s.due) >= now)];
}

const FILTERS: Array<[Filter, string]> = [
  ["all", "All posts"],
  ["linkedin", "LinkedIn only"],
  ["youtube", "YouTube only"],
];

function segment<T extends string>(items: Array<[T, string]>, active: T, label: string, pick: (value: T) => void, className: string): HTMLElement {
  const group = el("div", { class: className, role: "group", "aria-label": label });
  for (const [value, text] of items) {
    const b = el("button", { type: "button", class: `sv-seg${value === active ? " is-on" : ""}`, "aria-pressed": value === active ? "true" : "false" }, text);
    b.addEventListener("click", () => pick(value));
    group.appendChild(b);
  }
  return group;
}

function iconButton(icon: SVGElement, label: string, onClick: () => void): HTMLButtonElement {
  const b = el("button", { type: "button", class: "btn btn-quiet btn-icon sv-arrow", "aria-label": label, title: label });
  b.appendChild(icon);
  b.addEventListener("click", onClick);
  return b;
}

/** The day the phone's Month list shows: the picked one, else today when this month holds it. */
function phoneDay(o: Open): DayKey | null {
  if (o.selected) return o.selected;
  const today = dayKeyOf(Date.now(), o.view.rules.tz);
  return today.slice(0, 7) === o.day.slice(0, 7) ? today : null;
}

function drawChrome(o: Open): void {
  const { parts } = o;
  replaceChildren(parts.tabs, [
    segment<Tab>(
      [
        ["week", "Week"],
        ["month", "Month"],
        ["score", o.phone ? "Score" : "Scorecard"],
      ],
      o.tab,
      "View",
      (tab) => {
        o.tab = tab;
        drawAll(o);
      },
      "sv-tabs",
    ),
  ]);
  const step = (n: number) => {
    o.day = o.tab === "month" ? addMonths(o.day, n) : addDays(o.day, 7 * n);
    if (o.phone && o.tab === "month") o.selected = null;
    drawAll(o);
  };
  const phoneWeek = o.phone && o.tab === "week";
  const today = el("button", { type: "button", class: "linkish sv-today", title: "Back to this week" }, "Today");
  today.addEventListener("click", () => {
    o.day = dayKeyOf(Date.now(), o.view.rules.tz);
    if (o.phone && o.tab === "month") o.selected = null;
    drawAll(o);
  });
  replaceChildren(
    parts.nav,
    o.tab === "score"
      ? []
      : [
          iconButton(chevronLeftIcon(), o.tab === "month" ? "Previous month" : "Previous week", () => step(-1)),
          el("span", { class: "sv-range" }, o.tab === "month" ? monthName(o.day) : phoneWeek ? agendaRange(o.day) : weekRange(o.day)),
          iconButton(chevronRightIcon(), o.tab === "month" ? "Next month" : "Next week", () => step(1)),
          today,
        ],
  );
  syncTools(o);
}

/** The filter button's dot and the menu's ticks, and the gear's state; the controls themselves live on. */
function syncTools(o: Open): void {
  const { button: filterButton, dot, items } = o.filterMenu;
  const on = o.filter !== "all";
  dot.hidden = !on;
  const label = FILTERS.find(([value]) => value === o.filter)?.[1] ?? "";
  filterButton.setAttribute("aria-label", on ? `Filter posts (showing: ${label})` : "Filter posts");
  for (const [value, item] of items) {
    item.setAttribute("aria-checked", value === o.filter ? "true" : "false");
    item.classList.toggle("is-on", value === o.filter);
  }
  o.gear.setAttribute("aria-expanded", o.settingsOpen ? "true" : "false");
}

/** The filter button with its menu, and the gear: built once, so a live read never closes the menu. */
function buildTools(o: Open): void {
  const wrap = el("div", { class: "sv-menu-wrap" });
  const dot = el("span", { class: "sv-filter-dot", "aria-hidden": "true" });
  dot.hidden = true;
  const trigger = el("button", {
    type: "button",
    class: "btn btn-quiet btn-icon sv-tool sv-filter-btn",
    "aria-label": "Filter posts",
    "aria-haspopup": "menu",
    title: "Filter posts: all, LinkedIn only or YouTube only",
  });
  trigger.append(filterIcon(), dot);
  const menu = el("div", { class: "sv-pop sv-filter-menu", role: "menu", "aria-label": "Show posts" });
  wrap.append(trigger, menu);
  const items: Array<[Filter, HTMLButtonElement]> = FILTERS.map(([value, text]) => {
    const item = el("button", { type: "button", class: "sv-menu-item", role: "menuitemradio", "aria-checked": "false" }, el("span", { class: "sv-menu-tick" }, checkIcon()), text);
    item.addEventListener("click", () => {
      o.filter = value;
      o.filterMenu.pop.close(true);
      drawAll(o);
    });
    menu.appendChild(item);
    return [value, item];
  });
  const pop = attachPopover(trigger, menu, () => {
    const checked = items.find(([value]) => value === o.filter)?.[1] ?? items[0][1];
    checked.focus();
  });
  menu.addEventListener("keydown", (event) => {
    const list = items.map(([, item]) => item);
    const at = list.indexOf(document.activeElement as HTMLButtonElement);
    const move = (to: number) => {
      event.preventDefault();
      list[(to + list.length) % list.length].focus();
    };
    if (event.key === "ArrowDown") move(at + 1);
    else if (event.key === "ArrowUp") move(at - 1);
    else if (event.key === "Home") move(0);
    else if (event.key === "End") move(list.length - 1);
  });
  o.filterMenu = { pop, items, button: trigger, dot };
  o.gear = el("button", {
    type: "button",
    class: "btn btn-quiet btn-icon sv-tool sv-settings-btn",
    "aria-label": "Settings",
    "aria-expanded": "false",
    title: "Rules and tools: when posts are planned and which tools are promoted",
  });
  o.gear.appendChild(settingsIcon());
  o.gear.addEventListener("click", () => toggleSettings(o));
  replaceChildren(o.parts.tools, [wrap, o.gear]);
}

function postRow(o: Open, slot: SocialSlot, now: number): HTMLElement {
  const late = Date.parse(slot.due) < now && slot.state === "planned";
  const b = el(
    "button",
    { type: "button", class: `sv-qrow${late ? " is-late" : ""}${o.slotId === slot.id ? " is-open" : ""}`, "data-slot": slot.id },
    el("span", { class: "social-platform" }, slot.platform === "linkedin" ? linkedinIcon(14) : youtubeIcon(14)),
    el("span", { class: "sv-qrow-title" }, slotTitle(slot)),
    el("span", { class: `social-due${late ? " is-late" : ""}` }, dueLabel(slot.due, now, o.view.rules.tz)),
    el("span", { class: `social-chip ${slot.has_draft ? "chip-ok" : "chip-warn"}` }, slot.state === "planned" ? (slot.has_draft ? "Draft" : "No draft") : slot.state),
  );
  b.addEventListener("click", () => select(o, slot));
  return b;
}

function drawLists(o: Open): void {
  const now = Date.now();
  if (o.phone) {
    // The agenda replaces the queue; the Month shows the picked day's posts as cards. The cut
    // note stays, since the list can be missing the posts furthest away.
    replaceChildren(o.parts.queue, [
      o.tab !== "score" && o.view.truncated ? el("p", { class: "meta" }, "The list is cut to fit; the posts furthest away are not shown.") : null,
    ]);
    const day = o.tab === "month" ? phoneDay(o) : null;
    if (!day) {
      replaceChildren(o.parts.dayList, o.tab === "month" ? [el("p", { class: "meta" }, "Tap a day to see its posts.")] : []);
      return;
    }
    const posts = o.view.slots
      .filter((slot) => matches(slot, o.filter) && dayKeyOf(Date.parse(slot.due), o.view.rules.tz) === day)
      .sort((a, b) => Date.parse(a.due) - Date.parse(b.due));
    const cardOptions = { view: o.view, now, openSlot: o.slotId, onOpen: (slot: SocialSlot) => select(o, slot) };
    replaceChildren(o.parts.dayList, [
      el("div", { class: "sv-ag-head g-day" }, el("span", {}, agendaDay(day)), el("span", { class: "sv-ag-count" }, `${posts.length} ${posts.length === 1 ? "post" : "posts"}`)),
      posts.length ? el("div", { class: "sv-agenda" }, ...posts.map((slot) => agendaCard(slot, cardOptions))) : el("p", { class: "meta" }, "No posts that day."),
    ]);
    return;
  }
  const rows = queueRows(o.view.slots, now, o.filter);
  replaceChildren(o.parts.queue, [
    el("h4", { class: "social-h" }, `Not posted yet (${rows.length})`),
    rows.length
      ? el("div", { class: "sv-qlist" }, ...rows.map((slot) => postRow(o, slot, now)))
      : el("p", { class: "meta" }, "Nothing left to post right now."),
    o.view.truncated ? el("p", { class: "meta" }, "The list is cut to fit; the posts furthest away are not shown.") : null,
  ]);
  if (o.selected && o.tab !== "score") {
    const day = o.selected;
    const posts = o.view.slots
      .filter((slot) => matches(slot, o.filter) && dayKeyOf(Date.parse(slot.due), o.view.rules.tz) === day)
      .sort((a, b) => Date.parse(a.due) - Date.parse(b.due));
    replaceChildren(o.parts.dayList, [
      el("h4", { class: "social-h" }, `Posts on ${dayLabel(day)} (${posts.length})`),
      posts.length ? el("div", { class: "sv-qlist" }, ...posts.map((slot) => postRow(o, slot, now))) : el("p", { class: "meta" }, "No posts that day."),
    ]);
  } else {
    replaceChildren(o.parts.dayList, []);
  }
}

function drawMain(o: Open): void {
  const now = Date.now();
  if (o.tab === "score") {
    replaceChildren(o.parts.main, [drawScorecard(o.view, now, { postLate: (id) => postLate(o, id) })]);
    return;
  }
  if (o.phone && o.tab === "week") {
    replaceChildren(o.parts.main, [
      drawAgenda({ view: o.view, filter: o.filter, day: o.day, now, openSlot: o.slotId, onOpen: (slot) => select(o, slot) }),
    ]);
    return;
  }
  const grid = drawCalendar({
    mode: o.tab,
    dots: o.phone && o.tab === "month",
    view: o.view,
    filter: o.filter,
    day: o.day,
    selected: o.phone && o.tab === "month" ? phoneDay(o) : o.selected,
    openSlot: o.slotId,
    now,
    onDay: (day) => {
      const shown = o.tab === "month" ? day.slice(0, 7) === o.day.slice(0, 7) : true;
      const sameWeek = o.tab === "week" && weekRange(day) === weekRange(o.day);
      o.day = day;
      if (o.tab === "month" ? !shown : !sameWeek) {
        drawAll(o);
        o.parts.main.querySelector<HTMLElement>(`[data-day="${day}"]`)?.focus();
      }
    },
    onOpenDay: (day) => {
      if (o.phone && o.tab === "month") {
        // A phone's Month lists the day's posts under the grid; it never guesses one to open.
        o.selected = day;
        if (day.slice(0, 7) === o.day.slice(0, 7)) o.day = day;
        drawAll(o);
        o.parts.main.querySelector<HTMLElement>(`[data-day="${day}"]`)?.focus();
        return;
      }
      o.day = day;
      o.selected = day;
      const posts = o.view.slots.filter((slot) => matches(slot, o.filter) && dayKeyOf(Date.parse(slot.due), o.view.rules.tz) === day);
      drawAll(o);
      if (posts.length === 1) select(o, posts[0]);
      else o.parts.dayList.scrollIntoView?.({ block: "nearest" });
      o.parts.main.querySelector<HTMLElement>(`[data-day="${day}"]`)?.focus();
    },
    onOpenSlot: (slot) => select(o, slot),
  });
  replaceChildren(o.parts.main, [grid]);
}

function drawWarns(o: Open): void {
  const lines = warnings(o.view, Date.now());
  replaceChildren(o.parts.warns, [
    o.error ? notice("The newest read failed; this is the last answer. It will try again.", "error") : null,
    ...lines.map((line) => el("p", { class: `social-warn warn-${line.tone}` }, alertTriangleIcon(), line.text)),
  ]);
}

/** "Did you post it?" above the list or the editor; the node is kept while it is the same post. */
function drawBanner(o: Open): void {
  const wanted = activeBanner(o.view);
  const { banner } = o.parts;
  if (!wanted) {
    delete banner.dataset.slot;
    replaceChildren(banner, []);
    return;
  }
  if (banner.dataset.slot === wanted.slotId && banner.firstChild) return;
  const slot = o.view.slots.find((s) => s.id === wanted.slotId);
  banner.dataset.slot = wanted.slotId;
  replaceChildren(banner, [
    bannerNode({
      context: o.context,
      slotId: wanted.slotId,
      platform: wanted.platform,
      title: slot ? slotTitle(slot) : "",
      refresh: o.refresh,
      posted: () => {
        if (current === o && o.slotId === wanted.slotId) void reloadEditor(o.parts.editor);
      },
    }),
  ]);
}

/** Redraw everything a read can change: not the editor, not the Settings fold. */
function drawAll(o: Open): void {
  // A read redraws everything it can change; give the keyboard back to what it was on: a chip or
  // a queue row by its post, a day cell by its day.
  const active = document.activeElement as HTMLElement | null;
  const post = active?.matches(".sv-chip, .sv-qrow, .sv-ac") ? active.dataset.slot : undefined;
  const region = active?.closest<HTMLElement>(".sv-main, .sv-queue, .sv-daylist") ?? null;
  const where = region ? [...Object.values(o.parts)].indexOf(region) : -1;
  const dayCell = active?.classList.contains("sv-day") ? active.dataset.day : undefined;
  drawChrome(o);
  drawWarns(o);
  drawBanner(o);
  drawMain(o);
  drawLists(o);
  // On a phone the open editor is a screen of its own: the bar and the list step aside.
  const editing = o.phone && o.slotId !== null;
  o.parts.bar.hidden = editing;
  o.parts.warns.hidden = editing;
  o.parts.left.hidden = editing;
  o.parts.settings.hidden = editing || !o.settingsOpen;
  o.parts.layout.classList.toggle("has-editor", o.slotId !== null && !o.phone);
  o.parts.layout.classList.toggle("is-editing", editing);
  if (post && where >= 0) {
    const root = Object.values(o.parts)[where] as HTMLElement;
    [...root.querySelectorAll<HTMLElement>("[data-slot]")].find((n) => n.dataset.slot === post)?.focus();
  } else if (dayCell) {
    o.parts.main.querySelector<HTMLElement>(`[data-day="${dayCell}"]`)?.focus();
  }
}

function select(o: Open, slot: { id: string; due: string }): void {
  // The post that is already open: nothing to do (a click must not reload it over your edits).
  if (o.slotId === slot.id && o.parts.editor.dataset.slot === slot.id) return;
  guard(o.parts.editor, () => {
    forgetDraft(o.parts.editor); // the user chose to discard it
    openSlot(o, slot);
  });
}

function openSlot(o: Open, slot: { id: string; due: string }): void {
  o.slotId = slot.id;
  o.parts.editor.hidden = false;
  openEditor(o.parts.editor, slot.id, {
    context: o.context,
    view: () => o.view,
    changed: o.refresh,
    backLabel: () => (o.tab === "month" ? "Month" : "Week"),
    closed: () => {
      const id = o.slotId;
      o.slotId = null;
      o.parts.editor.hidden = true;
      drawAll(o);
      // Back to the card the user came from, by its post.
      if (id) o.parts.left.querySelector<HTMLElement>(`[data-slot="${id}"]`)?.focus();
    },
  });
  drawAll(o);
  if (o.phone) o.dialog.node.querySelector<HTMLElement>(".ae-dialog-body")?.scrollTo?.({ top: 0 });
  else o.parts.editor.scrollIntoView?.({ block: "nearest" });
}

/** Post late (Scorecard): the editor opens on the Week tab at that post's week. */
function postLate(o: Open, id: string): void {
  const slot = o.view.slots.find((s) => s.id === id);
  o.tab = "week";
  if (slot) o.day = dayKeyOf(Date.parse(slot.due), o.view.rules.tz);
  select(o, { id, due: slot?.due ?? "" });
}

function toggleSettings(o: Open): void {
  o.settingsOpen = !o.settingsOpen;
  o.parts.settings.hidden = !o.settingsOpen;
  o.gear.setAttribute("aria-expanded", o.settingsOpen ? "true" : "false");
  if (o.settingsOpen) {
    drawSettings(o.parts.settings, o.view, { context: o.context, changed: o.refresh });
  }
}

/**
 * Open the full view over the Admin Center, returning focus to *opener* when it closes. Opening
 * it again while it is open just brings the open one back and reads the card. *slotId* opens the
 * editor on that post (the compact card's Open on a post with no draft).
 */
export function openSocialView(
  context: ExtensionContext,
  view: SocialView,
  opener: HTMLElement | null,
  refresh: () => Promise<void>,
  slotId?: string,
): void {
  void probeShare(context); // does the browser have a share sheet? cached for the Share taps
  if (current) {
    void refresh();
    if (slotId) select(current, { id: slotId, due: "" });
    return;
  }
  const parts = {
    bar: el("div", { class: "sv-bar" }),
    tabs: el("div", { class: "sv-tabs-wrap" }),
    tools: el("div", { class: "sv-tools" }),
    nav: el("div", { class: "sv-nav" }),
    warns: el("div", { class: "social-warns sv-warns" }),
    banner: el("div", { class: "sv-banner-wrap", "aria-live": "polite" }),
    main: el("div", { class: "sv-main" }),
    queue: el("div", { class: "sv-queue" }),
    dayList: el("div", { class: "sv-daylist" }),
    editor: el("div", { class: "sv-editor", "aria-label": "Post editor" }),
    settings: el("div", { class: "sv-settings" }),
    status: el("div", { class: "sv-status" }),
  };
  parts.editor.hidden = true;
  parts.settings.hidden = true;
  parts.bar.append(el("div", { class: "sv-bar-top" }, parts.tabs, parts.tools), parts.nav);
  const left = el("div", { class: "sv-left" }, parts.main, parts.dayList, parts.queue);
  const layout = el("div", { class: "sv-layout" }, left, parts.editor);
  const body = el("div", { class: "sv-body" }, parts.bar, parts.settings, parts.warns, parts.banner, layout);
  const dialog = openDialog({
    title: "Social",
    body,
    opener,
    id: "social-view",
    closeLabel: "Close the Social view",
    className: "sv-dialog-card",
    onClose: () => {
      const o = current;
      current = null;
      for (const stop of o?.stop ?? []) stop();
      // The compact card shows "Did you post it?" while the view is closed.
      announceBanner();
    },
  });
  const o = {
    dialog,
    context,
    refresh,
    view,
    error: null,
    tab: "week",
    filter: "all",
    day: dayKeyOf(Date.now(), view.rules.tz),
    selected: null,
    slotId: null,
    settingsOpen: false,
    phone: isPhone(),
    stop: [],
    parts: { ...parts, left, layout },
  } as unknown as Open;
  buildTools(o);
  o.stop.push(
    watchPhone((phone) => {
      o.phone = phone;
      drawAll(o);
    }),
    onBannerChange(() => drawBanner(o)),
  );
  current = o;
  drawAll(o);
  if (slotId) select(o, { id: slotId, due: "" });
}

/** The card read again: redraw the open view from it (not the editor or the Settings fold). */
export function pushSocialView(view: SocialView | null, error: unknown): void {
  if (!current || !view) return;
  current.view = view;
  current.error = error;
  drawAll(current);
}

/** Close the view, if open (the card going away closes it). */
export function closeSocialView(): void {
  const o = current;
  if (!o) return;
  closeEditor(o.parts.editor);
  o.dialog.close();
}

export const socialViewOpen = (): boolean => current !== null;
