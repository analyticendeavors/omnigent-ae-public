// The Parking Lot itself: task-store's open items through `GET /v1/ae/lot`,
// Resume, and the Park form. Park posts the worker's own field names to
// `POST /v1/ae/lot/park`; route C from a chat surface stays the agent's job
// through the task-store MCP, this form is for the human at the page.
//
// Cards (redesigned 2026-09-24 after the first box deploy): compact by
// default, one header row with the quadrant icon, the title, a meta line
// (resurface date and up to two tags) and Resume as the card's right-aligned
// action; NEXT, EST, every tag and the task-store link sit behind the
// expand toggle. Which cards are open is remembered per task in the SDK's
// `storage.user` (key `lot.expanded`), in memory when that is unavailable.
//
// Resume calls `POST /v1/ae/lot/{id}/session`, which starts a session with
// the task's Resume in Claude block as the first message, then opens it. It
// used to copy the block and open a blank composer, and the copy failed
// silently in the sandboxed frame. When no session can be started the error
// carries the block: the card shows it with a message that stays until
// dismissed or until a later Resume works, surviving the 20 s refresh.
//
// Dashboard merge (2026-09-24, docs/design/dashboard-review.md): a parked
// session hangs off its note's card ("Parked session: <title>", Open)
// instead of a second list called Parked sessions, so "parked" names one
// thing. `openParkForm` opens the form with a session chosen; Park on a
// working card and File a note in Needs a note call it.
//
// Tags, repo, search (2026-09-25,
// https://github.com/analyticendeavors/omnigent-ae/issues/28): every tag
// shows, wrapping under the header row, in task-store's own colour and icon
// (tags.ts), and a tag chip filters the lot by that tag; the repository (the
// note's REPO: line) sits in the meta line of the collapsed card; a search
// field and a filter sheet (lotFilter.ts) sit at the top of the section.
//
// task-store absent or unreachable is a state, not a failure of the page: the
// section renders the server's own message (503 not configured, 502
// unavailable) and everything else keeps working.
//
// One-line rows (2026-10-14, second pass): eight tasks took 1044 px on a
// phone. A collapsed task is now one line: the quadrant icon, the title cut
// with an ellipsis, then the resurface date ("today" in the warning colour
// when due), a parked marker when a parked session hangs off it, the
// chevron and Resume. The repo, the tags, NEXT, the task-store link and the
// parked session's own row with Open sit behind the chevron.

import type { ExtensionContext } from "@omnigent/extension-sdk";
import {
  AeRequestError,
  describeError,
  isLotAdminOnly,
  type Lot,
  type LotTask,
  type ParkInput,
  park,
  type Quadrant,
  type ResumeFallback,
  resumeFallbackOf,
  resumeSession,
  type Session,
  type Slot,
  estimateText,
  quadrantName,
  statusOf,
  titleOf,
} from "./api";
import {
  announce,
  body,
  button,
  el,
  empty,
  flash,
  inYourMoveNote,
  notice,
  replaceChildren,
  section,
  setSectionNote,
} from "./dom";
import { chevronIcon, parkedIcon, playIcon, quadrantIcon, repoIcon, statusIcon } from "./icons";
import { currentFilter, filterBar, filterTasks, isFiltering, localToday } from "./lotFilter";
import { tagChip, visibleTags } from "./tags";

export interface LotView {
  lot: Lot | null;
  lotError: unknown;
  sessions: Session[];
  /** Today in the lot's timezone, for the date filters; this device's date when absent. */
  today?: string | null;
  /** Tasks Your move shows as due (2026-10-14): listed there, not again here. */
  inYourMove?: ReadonlySet<string>;
}

interface Deps {
  context: ExtensionContext;
  refresh: () => Promise<void>;
}

const SLOTS: Slot[] = ["never opened", "freed", "swapped-back"];
const QUADRANTS: Quadrant[] = ["hotfix", "feature", "admin"];
export const EXPANDED_KEY = "lot.expanded";

// Page-lifetime state the 20 s re-render must not wipe.
const expanded = new Set<string>();
const fallbacks = new Map<string, ResumeFallback>();
let parkState: ReturnType<typeof parkForm> | null = null;

/** Read the remembered open cards; the page works the same without them. */
export async function loadLotState(context: ExtensionContext): Promise<void> {
  try {
    const saved = await context.storage.user.get<unknown>(EXPANDED_KEY);
    if (Array.isArray(saved)) {
      for (const id of saved) if (typeof id === "string") expanded.add(id);
    }
  } catch {
    // storage.user unavailable: remember in memory for this visit only.
  }
}

function saveExpanded(context: ExtensionContext): void {
  context.storage.user.set(EXPANDED_KEY, [...expanded]).catch(() => {
    // Same as a failed read: the state lives in memory until the page closes.
  });
}

/** For tests: forget what the module remembers between renders. */
export function resetLotState(): void {
  expanded.clear();
  fallbacks.clear();
  parkState = null;
  drawn = null;
}

/** Copy through the clipboard API, else the selection fallback the sandbox still allows. */
export async function copyText(text: string): Promise<boolean> {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    // Not available in a sandboxed frame without a clipboard-write grant.
  }
  const area = el("textarea", { class: "clip", "aria-hidden": "true" });
  area.value = text;
  document.body.appendChild(area);
  area.select();
  let copied = false;
  try {
    copied = document.execCommand("copy");
  } catch {
    copied = false;
  }
  area.remove();
  return copied;
}

/** `2026-09-28` as `Sep 28`, read as a calendar date (no time zone shift). */
export function shortDate(value: string): string {
  const match = /^(\d{4})-(\d{2})-(\d{2})/.exec(value);
  if (!match) return value;
  const date = new Date(Number(match[1]), Number(match[2]) - 1, Number(match[3]));
  return date.toLocaleDateString("en-US", { month: "short", day: "numeric" });
}

/**
 * The date at the row's right end (2026-10-14): "today" when the task
 * resurfaces today, an earlier date still due, both in the warning colour.
 */
function dateMark(task: LotTask, today: string | null | undefined): HTMLElement | null {
  if (!task.resurfaces) return null;
  const day = task.resurfaces.slice(0, 10);
  const due = !!today && day <= today;
  return el(
    "span",
    { class: `resurface${due ? " is-due" : ""}`, title: `Resurfaces ${task.resurfaces}` },
    day === today ? "today" : shortDate(task.resurfaces),
  );
}

/** The parked icon on the one line when parked sessions hang off the task, named for them. */
function parkedMark(parked: Session[]): SVGElement | null {
  if (parked.length === 0) return null;
  return parkedIcon(
    parked.length === 1
      ? `Parked session: ${titleOf(parked[0])}`
      : `${parked.length} parked sessions`,
  );
}

/**
 * The repository, then every tag, on one wrapping line behind the chevron.
 * With *onTag* a chip filters the lot by its tag; without (Your move) it is a label.
 */
function tagRow(task: LotTask, onTag?: (tag: string) => void): HTMLElement | null {
  const tags = visibleTags(task.tags);
  if (tags.length === 0 && !task.repo) return null;
  const active = new Set(currentFilter().tags);
  return el(
    "div",
    { class: "card-tags" },
    task.repo
      ? el(
          "span",
          { class: "card-repo", title: `Repository ${task.repo}` },
          repoIcon(),
          el("span", { class: "card-repo-name" }, task.repo),
        )
      : null,
    ...tags.map((tag) =>
      tagChip(tag, onTag ? { onClick: onTag, pressed: active.has(tag.toLowerCase()) } : {}),
    ),
  );
}

/**
 * Parked sessions by the task their `ae.task` names: the lot's rows and Your
 * move's due rows (2026-10-14, review) mark them the same way.
 */
export function parkedByTask(sessions: Session[]): Map<string, Session[]> {
  const byTask = new Map<string, Session[]>();
  for (const session of sessions) {
    const task = session.labels["ae.task"];
    if (statusOf(session) !== "parked" || !task) continue;
    byTask.set(task, [...(byTask.get(task) ?? []), session]);
  }
  return byTask;
}

/** A parked session's own row behind the chevron, with Open. */
function parkedRow(deps: Deps, session: Session): HTMLElement {
  return el(
    "div",
    { class: "task-session" },
    statusIcon("parked"),
    el("span", { class: "meta" }, "Parked session: "),
    el("span", { class: "task-session-title" }, titleOf(session)),
    button("Open", {
      title: "Open the parked session",
      onClick: () => deps.context.navigation.openSession(session.id),
    }),
  );
}

/** What the chevron shows: repo and tags, NEXT, the facts, the link, parked sessions. */
function detailsOf(
  deps: Deps,
  task: LotTask,
  quadrant: Quadrant | null,
  id: string,
  open: boolean,
  options: TaskCardOptions,
): HTMLElement {
  const details = el("div", { class: "card-details", id, hidden: !open });
  const tags = tagRow(task, options.onTag);
  if (tags) details.appendChild(tags);
  if (task.next) {
    details.appendChild(el("p", { class: "next" }, el("span", { class: "k" }, "NEXT "), task.next));
  }
  const facts = el("dl", { class: "facts" });
  const fact = (name: string, value: string | null | undefined) => {
    if (!value) return;
    facts.appendChild(el("dt", {}, name));
    facts.appendChild(el("dd", {}, value));
  };
  fact("Quadrant", quadrant ?? task.quadrant);
  fact("EST", estimateText(task.estimate));
  fact("Resurfaces", task.resurfaces);
  if (facts.childElementCount > 0) details.appendChild(facts);
  const url = el("p", { class: "meta url" }, "task-store: ");
  url.appendChild(el("code", {}, task.url));
  details.appendChild(url);
  for (const session of options.parked ?? []) details.appendChild(parkedRow(deps, session));
  return details;
}

const SLOW_START_POLLS = 8;
const SLOW_START_POLL_MS = 3_000;

/**
 * The session a slow Resume started, found by its `ae.task` label. The SDK
 * gives a host call ten seconds, and a create that waits on the host to
 * launch its runner can take longer; the server finishes the job regardless,
 * so the page looks for the session instead of reporting a failure.
 */
async function findStarted(
  context: ExtensionContext,
  taskId: string,
  since: number,
): Promise<string | null> {
  for (let attempt = 0; attempt < SLOW_START_POLLS; attempt += 1) {
    await new Promise((resolve) => setTimeout(resolve, SLOW_START_POLL_MS));
    try {
      const sessions = await context.sessions.listAll({ pageLimit: 100 });
      const match = sessions
        .filter((session) => session.labels["ae.task"] === taskId && session.createdAt >= since)
        .sort((left, right) => right.createdAt - left.createdAt)[0];
      if (match) return match.id;
    } catch {
      // Try again on the next tick; the budget above bounds the wait.
    }
  }
  return null;
}

function isTimeout(error: unknown): boolean {
  return (
    error instanceof Error &&
    error.name === "ExtensionApiError" &&
    (error as Error & { code?: string }).code === "Timeout"
  );
}

/** The block a failed Resume left behind, with the ways to use it by hand. */
function fallbackPanel(deps: Deps, task: LotTask, fallback: ResumeFallback): HTMLElement {
  const panel = el("div", { class: "resume-fallback" });
  panel.appendChild(notice(fallback.message, "error"));
  panel.appendChild(el("pre", { class: "resume" }, fallback.block));
  const actions = el("div", { class: "actions" });
  actions.appendChild(
    button("Copy block", {
      onClick: async () => {
        const copied = await copyText(fallback.fenced);
        flash(
          panel,
          copied
            ? "Copied. Paste it as the first message."
            : "Clipboard unavailable here; select the block above and copy it.",
          copied ? "ok" : "info",
        );
      },
    }),
  );
  const sessionId = fallback.sessionId;
  actions.appendChild(
    sessionId
      ? button("Open its session", {
          title: "The session was started; paste the block there",
          onClick: () => deps.context.navigation.openSession(sessionId),
        })
      : button("New session", {
          title: "Open the composer to paste the block into",
          onClick: () => deps.context.navigation.openNewSession(),
        }),
  );
  actions.appendChild(
    button("Dismiss", {
      onClick: () => {
        fallbacks.delete(task.id);
        panel.remove();
      },
    }),
  );
  panel.appendChild(actions);
  return panel;
}

export interface TaskCardOptions {
  /** Parked sessions whose `ae.task` is this task: a marker on the line, a row behind the chevron. */
  parked?: Session[];
  /** A tag chip filters the lot by its tag; without it (Your move) the chips are labels. */
  onTag?: (tag: string) => void;
  /** Today in the lot's timezone, so a due date reads "today"; none: dates only. */
  today?: string | null;
}

/**
 * One lot row: one line (icon, title, date, parked marker, chevron, Resume),
 * everything else behind the chevron (2026-10-14). Your move reuses it.
 */
export function taskCard(deps: Deps, task: LotTask, options: TaskCardOptions = {}): HTMLElement {
  const quadrant = quadrantName(task.quadrant);
  const title = task.title ?? task.id;
  const open = expanded.has(task.id);
  const detailsId = `task-${task.id.replace(/[^A-Za-z0-9_-]/g, "_")}-details`;
  const card = el("article", {
    class: `card card-task card-q-${quadrant ?? "none"}${open ? " is-open" : ""}`,
    "data-task": task.id,
  });

  const toggle = el(
    "button",
    {
      type: "button",
      class: "card-toggle",
      "aria-expanded": String(open),
      "aria-controls": detailsId,
      title: "Show or hide the repo, tags, NEXT and the task-store link",
    },
    quadrantIcon(quadrant),
    el("span", { class: "card-title", title }, title),
    dateMark(task, options.today),
    parkedMark(options.parked ?? []),
    chevronIcon(),
  );

  const details = detailsOf(deps, task, quadrant, detailsId, open, options);
  toggle.addEventListener("click", () => {
    const now = !expanded.has(task.id);
    if (now) expanded.add(task.id);
    else expanded.delete(task.id);
    toggle.setAttribute("aria-expanded", String(now));
    card.classList.toggle("is-open", now);
    details.hidden = !now;
    saveExpanded(deps.context);
  });

  const resume = button("Resume", {
    kind: "primary",
    title: "Start a session with this task's Resume in Claude block and open it",
    onClick: async (node) => {
      const label = node.lastChild;
      if (label) label.textContent = "Starting";
      const clicked = Math.floor(Date.now() / 1000) - 5;
      try {
        const started = await resumeSession(deps.context, task.id);
        fallbacks.delete(task.id);
        card.querySelector(".resume-fallback")?.remove();
        announce(`Started a session for ${title}; opening it.`);
        await deps.context.navigation.openSession(started.session_id);
      } catch (error) {
        if (isTimeout(error)) {
          const found = await findStarted(deps.context, task.id, clicked);
          if (found) {
            announce(`Started a session for ${title}; opening it.`);
            await deps.context.navigation.openSession(found);
            return;
          }
          flash(
            card,
            `The session is taking a while to start. It will appear in the sidebar with this task's label (ae.task ${task.id}); Resume again only if it does not.`,
            "info",
          );
          return;
        }
        const fallback = resumeFallbackOf(error);
        if (fallback) {
          fallbacks.set(task.id, fallback);
          card.querySelector(".resume-fallback")?.remove();
          card.appendChild(fallbackPanel(deps, task, fallback));
          return;
        }
        flash(card, describeError(error), "error");
      } finally {
        if (label) label.textContent = "Resume";
      }
    },
  });
  // The label is a span so the phone layout can show the icon alone and give
  // the title the width; aria-label keeps the name for assistive tech.
  resume.replaceChildren(playIcon(), el("span", { class: "btn-label" }, "Resume"));
  resume.classList.add("btn-resume");
  resume.setAttribute("aria-label", `Resume ${title}`);

  card.appendChild(el("div", { class: "card-head task-head" }, toggle, resume));
  card.appendChild(details);
  const fallback = fallbacks.get(task.id);
  if (fallback) card.appendChild(fallbackPanel(deps, task, fallback));
  return card;
}

function field(
  name: keyof ParkInput | "tags_text",
  labelText: string,
  input: HTMLInputElement | HTMLSelectElement | HTMLTextAreaElement,
  hint?: string,
): HTMLElement {
  input.id = `park-${name}`;
  input.name = name;
  return el(
    "div",
    { class: "field" },
    el("label", { for: input.id }, labelText),
    input,
    hint ? el("span", { class: "hint" }, hint) : null,
  );
}

function textInput(placeholder: string, required = false): HTMLInputElement {
  return el("input", { type: "text", placeholder, required, autocomplete: "off" });
}

function select(values: readonly string[], selected?: string): HTMLSelectElement {
  const node = el("select");
  for (const value of values) {
    node.appendChild(el("option", { value, selected: value === selected }, value || "(none)"));
  }
  return node;
}

/**
 * The Park form: the worker's fields, the session optional. Built once per
 * page and moved into each render (2026-09-24): rebuilding it every 20 s
 * refresh, or on the refresh the frame's first focus triggers, closed it and
 * dropped whatever was typed.
 */
function parkForm(
  deps: Deps,
  sessions: Session[],
): { node: HTMLElement; fillSessions: (sessions: Session[]) => void } {
  const details = el("details", { class: "park", id: "park" });
  details.appendChild(el("summary", {}, chevronIcon(), "Park a fork"));
  const form = el("form", { class: "park-form", novalidate: true });
  const thread = textInput("short-name", true);
  const state = el("textarea", {
    rows: "2",
    required: true,
    placeholder: "Where it stands in one line: what exists, what is decided",
  });
  const next = el("textarea", {
    rows: "2",
    required: true,
    placeholder: "One physical action that resumes it",
  });
  const slot = select(SLOTS, "never opened");
  const quadrant = select(QUADRANTS, "feature");
  const est = textInput("minutes | about an hour | a block | multi-block", true);
  const from = textInput("this thread's task, surface, date", true);
  const summary = textInput("optional: one line for the task list");
  const repo = textInput("optional: owner/repo");
  const tags = textInput("optional: comma-separated");
  const due = el("input", { type: "date" });
  const sessionSelect = el("select");
  let current = sessions;
  // Refilled on every render; the chosen session survives while it is listed.
  const fillSessions = (next: Session[]) => {
    current = next;
    const chosen = sessionSelect.value;
    sessionSelect.replaceChildren(el("option", { value: "" }, "(no session)"));
    for (const session of next) {
      const status = statusOf(session);
      const parkable =
        status === "working" ||
        status === "blocked" ||
        status === null ||
        (status === "parked" && !session.labels["ae.task"]);
      if (parkable)
        sessionSelect.appendChild(el("option", { value: session.id }, titleOf(session)));
    }
    if ([...sessionSelect.options].some((option) => option.value === chosen)) {
      sessionSelect.value = chosen;
    }
  };
  fillSessions(sessions);
  sessionSelect.addEventListener("change", () => {
    if (sessionSelect.value && !thread.value) {
      const chosen = current.find((session) => session.id === sessionSelect.value);
      if (chosen)
        thread.value = titleOf(chosen)
          .toLowerCase()
          .replace(/[^a-z0-9]+/g, "-")
          .replace(/^-|-$/g, "");
    }
  });
  form.append(
    field("thread", "THREAD", thread),
    field("state", "STATE", state),
    field("next", "NEXT", next),
    el(
      "div",
      { class: "row" },
      field("slot", "SLOT", slot),
      field("quadrant", "QUADRANT", quadrant),
    ),
    el("div", { class: "row" }, field("est", "EST", est), field("from", "FROM", from)),
    field("summary", "Summary", summary),
    el("div", { class: "row" }, field("repo", "Repo", repo), field("tags_text", "Tags", tags)),
    el(
      "div",
      { class: "row" },
      field("due_date", "Due", due, "blank: the quadrant's resurface date"),
      field("session_id", "Session", sessionSelect, "marks it parked and points it at the task"),
    ),
  );
  const submit = button("File to the Parking Lot", {
    kind: "primary",
    onClick: async () => {
      const missing = [thread, state, next, est, from].filter((input) => !input.value.trim());
      if (missing.length > 0) {
        missing[0].focus();
        flash(form, "THREAD, STATE, NEXT, EST and FROM are required.", "error");
        return;
      }
      const input: ParkInput = {
        thread: thread.value.trim(),
        state: state.value.trim(),
        next: next.value.trim(),
        slot: slot.value as Slot,
        quadrant: quadrant.value as Quadrant,
        est: est.value.trim(),
        from: from.value.trim(),
      };
      if (summary.value.trim()) input.summary = summary.value.trim();
      if (repo.value.trim()) input.repo = repo.value.trim();
      const tagList = tags.value
        .split(",")
        .map((tag) => tag.trim())
        .filter(Boolean);
      if (tagList.length > 0) input.tags = tagList;
      if (due.value) input.due_date = due.value;
      if (sessionSelect.value) input.session_id = sessionSelect.value;
      try {
        const outcome = await park(deps.context, input);
        const lines = [`Filed ${outcome.title ?? input.thread} as task ${outcome.task_id}.`];
        if (outcome.session) lines.push(`Session marked parked.`);
        for (const warning of outcome.warnings ?? []) lines.push(warning);
        announce(`${lines.join(" ")} task-store: ${outcome.url}`);
        form.reset();
        await deps.refresh();
      } catch (error) {
        if (error instanceof AeRequestError && error.code === "task_store_unfiled") {
          const detail = error.body as { error?: { note?: string; instruction?: string } };
          const note = detail.error?.note;
          flash(
            form,
            `UNFILED (target unreachable), file this yourself: ${error.message}`,
            "error",
          );
          if (note) form.appendChild(el("pre", { class: "resume flash-keep" }, note));
          return;
        }
        flash(form, describeError(error), "error");
      }
    },
  });
  submit.type = "button";
  form.appendChild(el("div", { class: "actions" }, submit));
  form.addEventListener("submit", (event) => event.preventDefault());
  details.appendChild(form);
  return { node: details, fillSessions };
}

/** What the latest render drew the cards from, so a filter change redraws only them. */
let drawn: { node: HTMLElement; cards: HTMLElement; view: LotView; deps: Deps } | null = null;

function drawCards(): void {
  if (!drawn || !drawn.view.lot) return;
  const { node, cards, view, deps } = drawn;
  const lot = view.lot!;
  const parked = parkedByTask(view.sessions);
  const filter = currentFilter();
  const today = view.today || localToday();
  // A search or filter looks at every task, Your move's included, so it always finds one.
  const listed = isFiltering(filter) ? lot.tasks : notInYourMove(lot.tasks, view);
  // The heading's "1 in Your move" comes from the same list, so it goes while
  // a search or filter is on and comes back with it (2026-10-14, review).
  setSectionNote(node, inYourMoveNote(lot.tasks.length - listed.length, lot.tasks.length));
  const shown = filterTasks(listed, filter, today);
  const bar = filterBar(drawCards);
  const nodes = shown.map((task) =>
    taskCard(deps, task, { parked: parked.get(task.id), onTag: bar.toggleTag, today }),
  );
  if (nodes.length === 0 && isFiltering(filter)) {
    nodes.push(empty("Nothing in the lot matches. Clear the search or a filter to see more."));
  }
  cards.replaceChildren(...nodes);
  // The sheet offers every tag and repo in the lot, Your move's due tasks
  // included, though the list leaves those out (2026-10-14, review).
  bar.update(lot.tasks, shown.length);
}

/** The lot's tasks less the ones Your move shows as due (each item once, 2026-10-14). */
function notInYourMove(tasks: LotTask[], view: LotView): LotTask[] {
  const skip = view.inYourMove;
  return skip ? tasks.filter((task) => !skip.has(task.id)) : tasks;
}

export function renderLot(host: HTMLElement, view: LotView, deps: Deps): void {
  const count = view.lot?.count;
  // An empty lot keeps its body: the Park form lives there ("0" is not empty).
  // drawCards sets the heading's note.
  const node = section("lot", "Parking Lot", count === 0 ? "0" : count);
  const target = body(node);
  const restoreFocus = filterBar(drawCards).holdFocus();
  drawn = null;
  if (view.lot === null) {
    const error = view.lotError;
    if (isLotAdminOnly(error)) {
      // A signed-in non-admin (2026-10-28): quiet and final, with no park form to file into.
      target.appendChild(empty("The Parking Lot is for admins."));
      parkState = null;
      replaceChildren(host, [node]);
      restoreFocus();
      return;
    }
    if (error instanceof AeRequestError && error.status === 503) {
      target.appendChild(
        notice(`task-store is not configured on this server: ${error.message}`, "info"),
      );
    } else if (error instanceof AeRequestError) {
      target.appendChild(
        notice(`task-store did not answer (${error.status}): ${error.message}`, "error"),
      );
    } else if (error) {
      target.appendChild(notice(`The lot could not be read: ${describeError(error)}`, "error"));
    } else {
      target.appendChild(empty("Loading the lot."));
    }
  } else if (view.lot.list === null) {
    target.appendChild(empty(view.lot.note ?? "No Parking Lot list exists yet."));
  } else if (view.lot.tasks.length === 0) {
    target.appendChild(empty("The lot is empty."));
  } else {
    const present = new Set(view.lot.tasks.map((task) => task.id));
    const stale = [...expanded].filter((id) => !present.has(id));
    for (const id of stale) expanded.delete(id);
    if (stale.length > 0) saveExpanded(deps.context);
    const cards = el("div", { class: "cards" });
    drawn = { node, cards, view, deps };
    target.appendChild(filterBar(drawCards).node);
    target.appendChild(cards);
    drawCards();
  }
  if (parkState) parkState.fillSessions(view.sessions);
  else parkState = parkForm(deps, view.sessions);
  target.appendChild(parkState.node);
  replaceChildren(host, [node]);
  restoreFocus();
}

/**
 * Open the park form with a session chosen, its THREAD filled from the title,
 * and bring it into view (Park on a working card, File a note). Opens the
 * Parking Lot section first when it is closed.
 */
export function openParkForm(sessionId: string): void {
  const lotSection = document.getElementById("lot");
  if (lotSection?.classList.contains("is-closed")) {
    lotSection.querySelector<HTMLButtonElement>(".section-toggle")?.click();
  }
  const form = document.getElementById("park") as HTMLDetailsElement | null;
  const select = document.getElementById("park-session_id") as HTMLSelectElement | null;
  if (!form || !select) return;
  form.open = true;
  if ([...select.options].some((option) => option.value === sessionId)) {
    select.value = sessionId;
    select.dispatchEvent(new Event("change"));
  }
  form.scrollIntoView({ block: "start", behavior: "smooth" });
  document.getElementById("park-state")?.focus({ preventScroll: true });
}
