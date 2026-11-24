// The Activity tab and the rail's "Done today" (2026-11-16, D2 phase 2). Drawing only; the data
// and the day grouping are in activityStore.ts, read from the server's activity log.
//
// "Done" means "no longer reported": the list stopped showing the item on two clean reads in a
// row. It is not proof that someone fixed it, and the page says so beside every Done list. A log
// the server could not read is said so in words, never drawn as an empty day.

import { type ActivityEvent, type DoneItem } from "./api";
import { activityDays, dayLabel, doneToday, eventText, getActivity, onActivity, unreadable } from "./activityStore";
import { SOURCE_NAMES } from "./attentionStore";
import { el, replaceChildren } from "./dom";
import { checkIcon } from "./icons";
import { whenText } from "./needsMore";

export const DONE_MEANS = "Done means the list no longer reports it. It is not proof that someone fixed it.";
/** Most Done entries the rail lists before "See all in Activity". */
export const RAIL_DONE_MAX = 5;

const timeOf = (iso: string): string => {
  const at = new Date(iso);
  return Number.isNaN(at.getTime()) ? "" : at.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
};

function doneRow(item: DoneItem): HTMLElement {
  return el(
    "li",
    { class: "act-row", "data-id": item.id },
    el("span", { class: "act-what" }, item.title || SOURCE_NAMES[item.source] || item.source),
    el("span", { class: "act-src meta" }, SOURCE_NAMES[item.source] ?? item.source),
    el("span", { class: "act-when meta", title: whenText(item.done_at) }, `left ${timeOf(item.done_at)}`),
  );
}

function eventRow(event: ActivityEvent): HTMLElement {
  return el(
    "li",
    { class: "act-row act-event", "data-what": event.what },
    el("span", { class: "act-what" }, eventText(event, SOURCE_NAMES[event.source] ?? event.source)),
    el("span", { class: "act-src meta" }, SOURCE_NAMES[event.source] ?? event.source),
    el("span", { class: "act-when meta", title: whenText(event.at) }, timeOf(event.at)),
  );
}

/** What the Activity tab holds, from the activity store. */
export function drawActivity(host: HTMLElement, now = Date.now()): void {
  const { data, error } = getActivity();
  if (!data) {
    replaceChildren(host, [
      el(
        "p",
        { class: error ? "act-bad" : "meta" },
        error ? "Activity could not be read. Reload the page, or try again in a minute." : "Reading activity...",
      ),
    ]);
    return;
  }
  if (unreadable(data)) {
    // The server could not read its log: never an empty week.
    replaceChildren(host, [
      el("p", { class: "act-bad", role: "status" }, `${data.unknown.join(". ")}. This is not an empty week; the log file on the server needs fixing.`),
    ]);
    return;
  }
  const days = activityDays(data);
  replaceChildren(host, [
    el("p", { class: "act-note meta" }, `The last 7 days. ${DONE_MEANS}`),
    error ? el("p", { class: "act-bad" }, "The newest read failed; this is the last answer.") : null,
    days.length
      ? el(
          "div",
          { class: "act-days" },
          ...days.map((day) =>
            el(
              "section",
              { class: "act-day", "data-day": day.key, "aria-label": dayLabel(day.key, now) },
              el("h3", { class: "act-day-h" }, dayLabel(day.key, now)),
              day.done.length
                ? el("div", { class: "act-group" }, el("h4", { class: "act-group-h" }, `No longer reported (${day.done.length})`), el("ul", { class: "act-list" }, ...day.done.map(doneRow)))
                : null,
              day.events.length
                ? el("div", { class: "act-group" }, el("h4", { class: "act-group-h" }, `What was done (${day.events.length})`), el("ul", { class: "act-list" }, ...day.events.map(eventRow)))
                : null,
            ),
          ),
        )
      : el("p", { class: "act-empty" }, checkIcon(), "Nothing was dropped from the list or acted on in the last 7 days."),
    data.more_items ? el("p", { class: "act-more meta" }, `${data.more_items} more done items not shown (the oldest).`) : null,
    data.more_events ? el("p", { class: "act-more meta" }, `${data.more_events} more actions not shown (the oldest).`) : null,
  ]);
}

/** The Activity tab's content, kept current while it is on the page; returns its stop. */
export function mountActivity(host: HTMLElement): () => void {
  const draw = (): void => drawActivity(host);
  draw();
  return onActivity(draw);
}

/**
 * The rail's Done today block (and the quiet day's list): a count, up to five entries and "See all
 * in Activity". Null before the route answered (an older server has none). An unreadable log
 * says so instead of "0".
 */
export function doneTodayBlock(openActivity: (() => void) | null, now = Date.now()): HTMLElement | null {
  const { data, loaded, error } = getActivity();
  if (!loaded || !data) return null;
  if (unreadable(data)) {
    return el(
      "section",
      { class: "rail-block rail-done", "data-area": "done" },
      el("div", { class: "rail-h" }, el("span", { class: "rail-name" }, "Done today")),
      el("p", { class: "rail-done-bad meta", role: "status" }, "The activity log could not be read."),
    );
  }
  const today = doneToday(data, now);
  const head = el(
    "div",
    { class: "rail-h" },
    el("span", { class: "rail-name" }, "Done today"),
    el("span", { class: "rail-counts" }, el("span", { class: "nt-pill nt-pill-done", title: `${today.length} no longer reported today` }, String(today.length))),
  );
  const list = today.length
    ? el(
        "ul",
        { class: "rail-members rail-done-list" },
        ...today.slice(0, RAIL_DONE_MAX).map((item) =>
          el(
            "li",
            { class: "rail-member", title: `${SOURCE_NAMES[item.source] ?? item.source}: left the list at ${timeOf(item.done_at)}. ${DONE_MEANS}` },
            el("span", { class: "rail-dot", "aria-hidden": "true" }),
            el("span", { class: "rail-mname" }, item.title || SOURCE_NAMES[item.source] || item.source),
            el("span", { class: "rail-mval" }, timeOf(item.done_at)),
          ),
        ),
      )
    : el("p", { class: "rail-done-none meta" }, "Nothing has dropped off the list yet today.");
  // A later read failed: the list is the last answer, and the rail says so as the Activity tab does.
  const stale = error ? el("p", { class: "rail-done-bad meta", role: "status" }, "The newest read failed; this is the last answer.") : null;
  const more =
    openActivity && (today.length > RAIL_DONE_MAX || today.length > 0)
      ? el("button", { type: "button", class: "linkish rail-done-all", title: "Open the Activity tab" }, "See all in Activity")
      : null;
  more?.addEventListener("click", () => openActivity?.());
  const cut = data.more_items ? el("p", { class: "rail-done-none meta" }, `${data.more_items} more done items not shown.`) : null;
  return el("section", { class: "rail-block rail-done", "data-area": "done" }, head, list, stale, cut, more);
}

/** Done entries the wall's Done list draws at most (the block shows as many whole rows as its space holds). */
export const DONE_LIST_MAX = 40;

/**
 * The Done list for the Overview's left column (2026-11-17): a header ("Done", "no longer reported", an
 * Activity button), then rows from the activity store, today first and then earlier days, each with what
 * it was, its source and when it left. Whole rows only: with `fit` the block measures its list and hides
 * trailing rows (and a day label with no row under it) until nothing is cut, but never the first row;
 * without it (the stacked layout) it shows `rows` rows. Nothing done in 7 days is one short line, not an
 * empty box. The header is built once (the Activity button keeps its focus through a tick); only the list,
 * the notes and the line under it change. It follows `onActivity` and returns its stop. The block stays
 * hidden before the route answered (an older server has none).
 */
export function mountDoneList(
  host: HTMLElement,
  options: { openActivity: (() => void) | null; rows: number | "fit" },
): { stop: () => void; fit: () => void; setRows: (rows: number | "fit") => void } {
  let rows = options.rows;
  const more = options.openActivity
    ? el("button", { type: "button", class: "btn btn-secondary done-all", title: "Open the Activity tab" }, "Activity")
    : null;
  more?.addEventListener("click", () => options.openActivity?.());
  const head = el("div", { class: "done-h" }, el("h3", {}, "Done"), el("span", { class: "meta" }, "no longer reported"), el("span", { class: "done-sp" }), more);
  const list = el("div", { class: "done-list" });
  const notes = el("div", { class: "done-notes" });
  const line = el("p", { class: "meta done-none" });
  host.append(head, list, notes, line);
  const fit = (): void => {
    const nodes = [...list.children] as HTMLElement[];
    for (const node of nodes) node.hidden = false;
    const firstRow = nodes.findIndex((node) => !node.classList.contains("done-day"));
    if (rows !== "fit") {
      let shown = 0;
      for (const node of nodes) {
        if (node.classList.contains("done-day")) continue;
        if (shown >= rows) node.hidden = true;
        else shown += 1;
      }
    } else if (list.clientHeight > 0) {
      // Trailing rows go until nothing is cut, but the first whole row always stays.
      for (let i = nodes.length - 1; i > firstRow && list.scrollHeight > list.clientHeight + 1; i--) nodes[i]!.hidden = true;
    }
    // A day label whose rows are all hidden goes too.
    nodes.forEach((node, i) => {
      if (!node.classList.contains("done-day")) return;
      const next = nodes[i + 1];
      if (!next || next.classList.contains("done-day") || next.hidden) node.hidden = true;
    });
  };
  const show = (empty: boolean, text: string | null, icon = false): void => {
    host.className = empty ? "as-done is-empty" : "as-done";
    head.hidden = empty;
    list.hidden = empty;
    line.hidden = !text;
    replaceChildren(line, text ? [icon ? checkIcon() : null, text] : []);
    if (empty) {
      replaceChildren(list, []);
      replaceChildren(notes, []);
    }
  };
  const draw = (now = Date.now()): void => {
    const { data, loaded, error } = getActivity();
    host.hidden = !loaded || !data;
    if (host.hidden) return;
    if (unreadable(data)) {
      show(true, "The activity log could not be read.");
      line.setAttribute("role", "status");
      return;
    }
    line.removeAttribute("role");
    const days = activityDays(data).filter((day) => day.done.length);
    if (!days.length) {
      show(true, "Nothing was done in the last 7 days.", true);
      return;
    }
    show(false, null);
    const nodes: HTMLElement[] = [];
    let count = 0;
    for (const day of days) {
      if (count >= DONE_LIST_MAX) break;
      nodes.push(el("div", { class: "done-day" }, dayLabel(day.key, now)));
      for (const item of day.done) {
        if (count >= DONE_LIST_MAX) break;
        count += 1;
        nodes.push(
          el(
            "div",
            { class: "act-row done-row", "data-id": item.id, title: `${SOURCE_NAMES[item.source] ?? item.source}: ${DONE_MEANS}` },
            el("span", { class: "act-what" }, item.title || SOURCE_NAMES[item.source] || item.source),
            el("span", { class: "act-src meta" }, SOURCE_NAMES[item.source] ?? item.source),
            el("span", { class: "act-when meta", title: whenText(item.done_at) }, `left ${timeOf(item.done_at)}`),
          ),
        );
      }
    }
    replaceChildren(list, nodes);
    // The rail's two lines, kept here: a later read failed (this is the last answer), and rows the answer cut.
    replaceChildren(notes, [
      error ? el("p", { class: "meta done-note", role: "status" }, "The newest read failed; this is the last answer.") : null,
      data!.more_items ? el("p", { class: "meta done-note" }, `${data!.more_items} more done items not shown.`) : null,
    ]);
    fit();
  };
  draw();
  const stop = onActivity(() => draw());
  const observer = typeof ResizeObserver !== "undefined" ? new ResizeObserver(() => fit()) : null;
  observer?.observe(list);
  return {
    stop: () => {
      stop();
      observer?.disconnect();
    },
    fit,
    setRows: (next) => {
      rows = next;
      draw();
    },
  };
}
