// The Admin Center's Needs you panel (2026-11-03, Admin Center v1.5 phase 1).
//
// Why: what needs the owner was split over cards. Git's list was built in the browser, Box
// health's on the server, and nothing could add them up. The server now does (`GET
// /v1/ae/attention`, ae_omni_policies/attention.py); this panel lists every item once, at the
// top of the page, and each card keeps its numbers and a line that opens the panel filtered to
// it (attentionStore.ts).
//
// Two levels of folding keep the panel from growing to a screenful of rows: one line per
// source (Git repos, Box health), then one per kind inside it (Failing PRs, Alerts to fix,
// Storage, ...), with at most three rows under a kind (one on a phone) and a "Show N more"
// button. On open only a source with an urgent item opens, and inside it only the urgent
// kinds; on a phone only the first urgent source. Open or closed is remembered per line with
// fold.ts (browser storage, a convenience only), and a line that gains an urgent item it has
// not shown before opens again by itself, so a remembered "closed" never hides something new.
//
// 2026-11-14 (concept D2): the Admin Center page draws this list as a table (needsTable.ts) instead of
// the tree below. This file still loads, counts and acts (Review, Fix, Merge, Dismiss, Clean up and their
// dialogs); when `activateAttention` is given `table` hooks it hands the drawing to them and builds each
// row with the table's frame, so the buttons and the Merge confirm are the same ones. Called without
// hooks (the tests, and any older caller) it draws the tree as before. Hide for me no longer removes a
// hidden card's items from the list (attentionView.ts, buildBlocks): hiding hides only the card.
//
// The groups are a WAI-ARIA tree: role="tree", each source and kind a treeitem with
// aria-expanded, each kind's rows a group, one roving tab stop, and the arrow keys.
//
// Rows are the flat `.bh-need` rows of the Box health card inside one `.bh-todo` surface per
// kind. Review opens a chat briefed on the item (a 409 slots_full asks first), Fix (2026-11-05)
// opens a chat that changes what the item needs after a confirm that states the approval it
// will meet, a ready pull request gets a Merge button that opens the Your move confirm
// (prs.ts) under its row, Dismiss takes a reason and says when the item comes back, and a Box
// item keeps the card's own Clean up confirm.
//
// The panel has no timer. It reads when it opens, when the page gets focus, when the page's
// refresh button runs and once after the Git and Box cards finish a read; each changed answer
// redraws the tree. An open Merge confirm survives those redraws: its card is read again once
// it is 60 seconds old, never dropped, and a confirm that cannot stay open says so.

import type { ExtensionContext } from "@omnigent/extension-sdk";
import {
  type AttentionChatAction,
  type AttentionItem,
  type AttentionOverview,
  type AttentionSource,
  type AttentionBatchTarget,
  type ComplianceReport,
  type PrCard,
  type SlotsFull,
  describeError,
  dismissAttention,
  assignedElsewhere,
  heldByWorkingChat,
  fetchAttention,
  fetchCompliance,
  fetchPrCard,
  fetchYourMove,
  restoreAttention,
  slotsFullOf,
  startAttentionBatch,
  startAttentionChat,
  startAttentionReviewMany,
} from "./api";
import {
  getAttention,
  getFilter,
  onAttention,
  resetAttention,
  setAttention,
  setFilter,
  setRereader,
  SOURCE_NAMES,
  uncounted,
  viewerLogin,
} from "./attentionStore";
import { openCleanup } from "./boxHealth";
import { rowMenu } from "./cards";
import { openDialog } from "./dialog";
import { button, el, flash, notice, replaceChildren } from "./dom";
import { sayInRow } from "./rowMessages";
import { attentionView } from "./attentionView";
import { onCardState } from "./cardState";
import { rememberedFold, saveFold } from "./fold";
import {
  checkIcon,
  chevronIcon,
  chevronsDownIcon,
  chevronsUpIcon,
  dotsIcon,
  eyeOffIcon,
  gitMergeIcon,
  listCheckIcon,
  messageIcon,
  restoreIcon,
  skipIcon,
  terminalIcon,
  toolIcon,
} from "./icons";
import { forgetHistory } from "./historyCache";
import { changePlan, registerPlanButton, resetPosts } from "./needsPost";
import { copyText } from "./lot";
import { type MessageSink, openOrShow } from "./openLink";
import {
  type MergeHooks,
  confirmIsOpen,
  dropConfirm,
  keepConfirm,
  mergingNode,
  openConfirm,
  urlKey,
} from "./prs";
import { registerRefresh } from "./tiles";
import { counts as yourMoveCounts } from "./yourMove";

/** Most items one Review all takes (the server's BATCH_MAX in labels.py). */
export const BATCH_MAX = 20;
/** Rows shown under a kind before "Show N more": three, one on a phone. */
export const ROWS_SHOWN = 3;
export const ROWS_SHOWN_PHONE = 1;
/** How long the Undo toast stays after a dismissal. */
export const UNDO_MS = 8_000;

/** The Investigate button's note on a home security row: read-only, nothing is changed. */
export const INVESTIGATE_HINT =
  "Open a chat that checks this alert and posts a verdict. It archives, mutes and blocks nothing.";
/** The page's phone breakpoint (style.css `@media (pointer: coarse), (max-width: 520px)`). */
const PHONE_QUERY = "(max-width: 520px)";
const DAY = 86_400_000;
const SEEN_KEY = "attn.seen.";

// ── the groups ──────────────────────────────────────────────────────────────

export interface GroupSpec {
  key: string;
  name: string;
  kinds: string[];
}

/** The kinds inside each source, in the order they show when equally urgent. */
export const GROUPS: Record<AttentionSource, GroupSpec[]> = {
  git: [
    { key: "accounts", name: "Accounts and repos that could not be read", kinds: ["account", "repo-error"] },
    { key: "sweep", name: "Sweep", kinds: ["sweep"] },
    { key: "alerts-fix", name: "Alerts to fix", kinds: ["alerts-fix"] },
    { key: "pr-failing", name: "Failing PRs", kinds: ["pr-failing"] },
    { key: "pr-ready", name: "Ready to merge", kinds: ["pr-ready"] },
    { key: "pr-update", name: "Behind or blocked", kinds: ["pr-update"] },
    { key: "alerts-stuck", name: "Alerts open after fix", kinds: ["alerts-stuck"] },
    { key: "branch", name: "Old branches", kinds: ["branch"] },
  ],
  "git-mine": [
    { key: "accounts", name: "Accounts and repos that could not be read", kinds: ["account", "repo-error"] },
    { key: "sweep", name: "Sweep", kinds: ["sweep"] },
    { key: "alerts-fix", name: "Alerts to fix", kinds: ["alerts-fix"] },
    { key: "pr-failing", name: "Failing PRs", kinds: ["pr-failing"] },
    { key: "pr-ready", name: "Ready to merge", kinds: ["pr-ready"] },
    { key: "pr-update", name: "Behind or blocked", kinds: ["pr-update"] },
    { key: "alerts-stuck", name: "Alerts open after fix", kinds: ["alerts-stuck"] },
    { key: "branch", name: "Old branches", kinds: ["branch"] },
  ],
  box: [
    { key: "heat-cpu-memory", name: "Heat, CPU and memory", kinds: ["heat", "cpu", "memory"] },
    { key: "storage", name: "Storage", kinds: ["mount"] },
    { key: "temp", name: "Temp folders and leftovers", kinds: ["temp", "litter", "docker"] },
    { key: "scan", name: "Cleanup scan", kinds: ["scan"] },
    { key: "container", name: "Containers", kinds: ["container"] },
    { key: "units", name: "Failed units", kinds: ["units"] },
    { key: "readings", name: "Monitor and readings", kinds: ["guard", "unread", "stale"] },
  ],
  host: [{ key: "host", name: "Not connected", kinds: ["host-failed"] }],
  home: [
    { key: "home-alarms", name: "Firewalla alarms", kinds: ["home-alarm"] },
    { key: "home-alerts", name: "Sentinel alerts", kinds: ["home-alert"] },
  ],
  cost: [{ key: "cost-over", name: "Over target", kinds: ["cost-over"] }],
  databricks: [{ key: "credit", name: "Credit used up", kinds: ["credit-over"] }],
  secrets: [
    {
      key: "secrets-broken",
      name: "Missing, expired or refused keys",
      kinds: ["secret-missing", "secret-expired", "secret-refused"],
    },
    { key: "secrets-soon", name: "Keys expiring soon", kinds: ["secret-soon"] },
  ],
  vault: [
    { key: "vault-expiry", name: "Expired or expiring secrets", kinds: ["vault-expired", "vault-soon"] },
    { key: "vault-check", name: "Secrets to check", kinds: ["vault-disabled", "vault-apart"] },
  ],
  accounts: [{ key: "accounts-full", name: "Weekly window full", kinds: ["account-full"] }],
  family: [{ key: "family-over", name: "Over the runaway flag today", kinds: ["family-over"] }],
  social: [
    { key: "post-overdue", name: "Posts past their slot", kinds: ["post-overdue"] },
    { key: "post-today", name: "Posts due today", kinds: ["post-today"] },
  ],
};

/** The panel's parent order: the same as `SOURCES` in attention.py. */
export const SOURCE_ORDER: AttentionSource[] = [
  "git",
  "git-mine",
  "box",
  "host",
  "home",
  "cost",
  "databricks",
  "secrets",
  "vault",
  "accounts",
  "family",
  "social",
];

export interface Line {
  /** The fold id, and the roving key. */
  id: string;
  name: string;
  items: AttentionItem[];
  urgent: number;
  review: number;
  /** Items in the line that a working chat holds (listed, not counted); one assigned to another admin is neither. */
  working: number;
  /** The item to name when the line is closed: the worst one not held by a chat. */
  worst: AttentionItem | null;
}

export interface Group extends Line {
  key: string;
  source: AttentionSource;
}

export interface Block extends Line {
  source: AttentionSource;
  groups: Group[];
}

/** A chat that is still working holds the item: it stays listed but does not count. A waiting
 * chat (blocked or finished) does not hold it in this sense, because the owner has to act. */
export const held = (item: AttentionItem) => heldByWorkingChat(item);

/**
 * A personal Git item (source "git-mine") takes Dismiss and Open on GitHub only (2026-11-08).
 * Fix, Review and Merge act as the box's own GitHub login, so the page never offers them for a
 * personal item, whatever the answer lists; the server refuses them too (attention.py).
 */
export const isPersonal = (item: AttentionItem) => item.source === "git-mine";
export const actionsOf = (item: AttentionItem): AttentionItem["actions"] =>
  isPersonal(item) ? item.actions.filter((action) => action === "dismiss") : item.actions;

function lineOf(id: string, name: string, items: AttentionItem[]): Line {
  const counted = items.filter((item) => !uncounted(item));
  const urgent = counted.filter((item) => item.tone === "urgent");
  return {
    id,
    name,
    items,
    urgent: urgent.length,
    review: counted.length - urgent.length,
    working: items.filter(held).length,
    worst: urgent[0] ?? counted[0] ?? items[0] ?? null,
  };
}

/**
 * Rows keep their place while the page is open (2026-11-16, Reid's fixed-order pick). Each item id gets a
 * sequence number the first time it is seen, in the server's order, and a source lists its rows by it: a
 * row that stays keeps its spot through every redraw (a poll, an action, a tone change), a new row joins
 * at the end, and a reload starts fresh. Kept in memory per page, never in storage.
 */
const rowSeq = new Map<string, number>();
let nextSeq = 0;

/**
 * Track the whole answer: number new rows in the order given and forget the ones that left the list.
 * Only the table calls this, with the full answer, so the map is pruned wherever it is numbered. An item
 * that is dismissed or snoozed leaves the list, so one that is restored or woken joins the end of its source.
 */
export function trackRowOrder(items: AttentionItem[]): void {
  for (const item of items) {
    if (!rowSeq.has(item.id)) rowSeq.set(item.id, nextSeq++);
  }
  const live = new Set(items.map((item) => item.id));
  for (const id of [...rowSeq.keys()]) {
    if (!live.has(id)) rowSeq.delete(id);
  }
}

/** For tests: start the order fresh, as a reload does. */
export function resetRowOrder(): void {
  rowSeq.clear();
  nextSeq = 0;
}

/** Numbered rows by their number; a row not numbered yet follows them in the order given (the sort is stable). */
const inFixedOrder = (items: AttentionItem[]) =>
  [...items].sort((a, b) => (rowSeq.get(a.id) ?? Infinity) - (rowSeq.get(b.id) ?? Infinity));

export function buildBlocks(items: AttentionItem[], only: AttentionSource | null = null): Block[] {
  const blocks: Block[] = [];
  for (const source of SOURCE_ORDER) {
    if (only && only !== source) continue;
    // 2026-11-14: a source whose card this viewer hid stays in the list. Hiding hides the card
    // only (Reid's D2 pick); a card turned off for everyone is already absent from the answer.
    const mine = inFixedOrder(items.filter((item) => item.source === source));
    if (!mine.length) continue;
    const specs = GROUPS[source];
    const known = new Set(specs.flatMap((spec) => spec.kinds));
    const groups: Group[] = [];
    for (const spec of specs) {
      const inGroup = mine.filter((item) => spec.kinds.includes(item.kind));
      if (inGroup.length) {
        groups.push({ key: spec.key, source, ...lineOf(`attn.${source}.${spec.key}`, spec.name, inGroup) });
      }
    }
    const other = mine.filter((item) => !known.has(item.kind));
    if (other.length) groups.push({ key: "other", source, ...lineOf(`attn.${source}.other`, "Other", other) });
    // Kinds stay in the order GROUPS lists them: one fixed order, so "Review N" sends items the way the table shows them.
    blocks.push({ source, groups, ...lineOf(`attn.${source}`, SOURCE_NAMES[source], mine) });
  }
  return blocks;
}

export const plural = (n: number, one: string, many = `${one}s`) => `${n} ${n === 1 ? one : many}`;

export function countsText(urgent: number, review: number, working = 0): string {
  const parts = [urgent ? `${urgent} urgent` : "", review ? `${review} to review` : ""].filter(Boolean);
  if (parts.length) return parts.join(", ");
  // Nothing counts: say how many chats are on the rest instead of a bare "held by chats".
  return working ? `0 to review, ${plural(working, "chat")} working` : "";
}

// ── folding ─────────────────────────────────────────────────────────────────

export function isPhone(): boolean {
  return typeof window.matchMedia === "function" && window.matchMedia(PHONE_QUERY).matches;
}

/** What each line shows when the viewer has not opened or closed it. */
export function defaultOpen(blocks: Block[], phone: boolean): Map<string, boolean> {
  const open = new Map<string, boolean>();
  const first = blocks.find((block) => block.urgent > 0);
  for (const block of blocks) {
    open.set(block.id, block.urgent > 0 && (!phone || block === first));
    for (const group of block.groups) open.set(group.id, group.urgent > 0);
  }
  return open;
}

const seenMemory = new Map<string, string[]>();
const SEEDED_KEY = `${SEEN_KEY}__seeded`;
let seededMemory = false;

/** True once this browser has drawn any line: after that a line it never saw starts with nothing seen. */
function isSeeded(): boolean {
  if (seededMemory) return true;
  try {
    return window.localStorage.getItem(SEEDED_KEY) === "1";
  } catch {
    return false;
  }
}

function markSeeded(): void {
  seededMemory = true;
  try {
    window.localStorage.setItem(SEEDED_KEY, "1");
  } catch {
    // Kept in memory until the page closes.
  }
}

/** The urgent ids a line last showed open, or null when this browser never drew the line. */
function loadSeen(id: string): Set<string> | null {
  try {
    const raw = window.localStorage.getItem(SEEN_KEY + id);
    if (raw) {
      const parsed: unknown = JSON.parse(raw);
      if (Array.isArray(parsed)) return new Set(parsed.filter((v): v is string => typeof v === "string"));
    }
  } catch {
    // No storage in this frame: the list kept in memory.
  }
  const kept = seenMemory.get(id);
  return kept ? new Set(kept) : null;
}

function saveSeen(id: string, ids: string[]): void {
  seenMemory.set(id, ids);
  try {
    window.localStorage.setItem(SEEN_KEY + id, JSON.stringify(ids));
  } catch {
    // Kept in memory until the page closes.
  }
}

/**
 * A closed line that holds an urgent item it has not shown open since the person last opened it gets a
 * small "new" marker; it never reopens the line (2026-11-17, the owner wants the page to stay collapsed).
 * Each line remembers the urgent ids it last showed open. The first draw in a browser counts the current
 * ids as seen, so a first visit shows no markers. Returns the ids of the lines to mark.
 */
export function markNewUrgent(lines: Pick<Line, "id" | "items">[], isOpen: (id: string) => boolean): Set<string> {
  const marked = new Set<string>();
  // First draw in this browser (once per browser, not per line): every current id counts as seen. A line
  // that first appears later starts with nothing seen, so its first urgent item shows the dot.
  const seeded = isSeeded() || lines.some((line) => loadSeen(line.id) !== null);
  for (const line of lines) {
    const ids = line.items.filter((item) => item.tone === "urgent" && !uncounted(item)).map((item) => item.id);
    const stored = loadSeen(line.id);
    if (stored === null && seeded && !isOpen(line.id)) {
      if (ids.length) marked.add(line.id);
      saveSeen(line.id, []);
      continue;
    }
    const seen = stored;
    if (seen === null || isOpen(line.id)) {
      // First draw, or the line is open: the person sees every current id.
      saveSeen(line.id, ids);
      continue;
    }
    if (ids.some((id) => !seen.has(id))) marked.add(line.id);
    // Only ids still present are kept: one that goes away and returns is new again.
    const kept = ids.filter((id) => seen.has(id));
    if (kept.length !== seen.size) saveSeen(line.id, kept);
  }
  if (lines.length) markSeeded();
  return marked;
}

/** For tests: forget the urgent ids each line has shown. */
export function resetAttentionSeen(): void {
  seenMemory.clear();
  seededMemory = false;
  resetRowOrder();
  readCards.clear();
  mergedRows.clear();
}

// ── words ───────────────────────────────────────────────────────────────────

/** "Nov 6": the day *days* days from *now*, in the viewer's locale. */
export function comesBackOn(days: number, now = Date.now()): string {
  return new Date(now + days * DAY).toLocaleDateString([], { month: "short", day: "numeric" });
}

export const dateOf = (iso: string | undefined): string => {
  const ms = iso ? Date.parse(iso) : Number.NaN;
  return Number.isNaN(ms) ? "" : new Date(ms).toLocaleDateString([], { month: "short", day: "numeric" });
};

/** The reasons a Box item may carry; a Git item takes free text. */
export const BOX_REASONS = ["Known", "Fix planned", "Not a problem"];
export const REASON_MAX = 200;

// ── the panel ───────────────────────────────────────────────────────────────

export interface Worked {
  session_id: string;
  title: string;
  path: string;
  /** "waiting": the chat blocked or finished, so the owner acts. Missing means working. */
  status?: "working" | "waiting";
  /** Just started from this page (false: the server said a chat already holds it). */
  started: boolean;
}

/** The Session Dashboard's page id (plugin.py PAGE_ID), where Your move lives. */
export const DASHBOARD_PAGE_ID = "analyticendeavors.parking-lot.lot";

/** "2 chats waiting on you", from the Your move reader's own count of sessions that wait on the owner. */
export function waitingText(n: number): string {
  return `${n} ${n === 1 ? "chat" : "chats"} waiting on you`;
}

/** The empty card's line: "Nothing needs you. Checked 11:57 AM." (no time when the read had none). */
export function emptyText(readAt: string): string {
  const at = Date.parse(readAt);
  if (Number.isNaN(at)) return "Nothing needs you.";
  return `Nothing needs you. Checked ${new Date(at).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })}.`;
}

/** What the table view (needsTable.ts) plugs into the panel: its own drawing and its row frame. */
export interface TableHooks {
  /** Draws the whole panel for one read; the panel has already checked the signature. */
  draw: (p: Panel, input: { data: AttentionOverview | null; error: unknown; filter: AttentionSource | null; phone: boolean }) => void;
  /** A row-level message (null clears it): the table shows it in the opened row's own message line, not in the row grid. */
  message: (p: Panel, id: string, line: HTMLElement | null) => void;
  /** The row's frame in the table: the same three parts as `rowShell`, with the table's cells. */
  shell: (p: Panel, item: AttentionItem, meta: string) => { row: HTMLElement; text: HTMLElement; actions: HTMLElement };
  /** Called on every finished row (a rebuilt one too): the opened panel, the primary button. */
  finish: (p: Panel, row: HTMLElement, item: AttentionItem, data: AttentionOverview) => void;
}

export interface Panel {
  context: ExtensionContext;
  root: HTMLElement;
  /** Set when the panel draws the table (needsTable.ts) instead of the tree. */
  table: TableHooks | null;
  /** The id of the row opened in place, if any (table only). */
  openRow: string | null;
  /** Rows past the limit shown for these kinds. */
  more: Set<string>;
  /** Chats started from this page, until the server lists the item as held. */
  worked: Map<string, Worked>;
  dismissedOpen: boolean;
  /** The roving tab stop's key. */
  current: string | null;
  undo: ReturnType<typeof setTimeout> | null;
  drawn: string;
  lastFilter: AttentionSource | null;
  /** The newest Your move read; an older answer that arrives late is dropped. */
  waitSeq: number;
}

let panel: Panel | null = null;
let generation = 0;
let onFocus: (() => void) | null = null;
let stopStore: (() => void) | null = null;
let stopCards: (() => void) | null = null;
let phoneQuery: MediaQueryList | null = null;
let onPhone: (() => void) | null = null;

/** The panel's empty shell; activateAttention fills it. */
export function attentionSection(): HTMLElement {
  return el(
    "section",
    { class: "attn", id: "needs-you", "aria-labelledby": "needs-you-h" },
    el(
      "header",
      { class: "attn-top" },
      el("h2", { class: "attn-h", id: "needs-you-h" }, "Needs you"),
      el("span", { class: "attn-counts meta" }),
      el("div", { class: "attn-tools" }),
    ),
    el("p", { class: "attn-wait", hidden: true }),
    el("div", { class: "attn-notes" }),
    el("div", { class: "attn-body" }),
    el("div", { class: "attn-toast", "aria-live": "polite" }),
  );
}

export function part(p: Panel, selector: string): HTMLElement {
  const found = p.root.querySelector<HTMLElement>(selector);
  if (!found) throw new Error(`Needs you panel is missing ${selector}`);
  return found;
}

function refresh(p: Panel): Promise<void> {
  return fetchAttention(p.context).then(
    (data) => {
      if (panel === p) setAttention(data);
    },
    (error: unknown) => {
      if (panel === p) setAttention(null, error ?? new Error("Request failed"));
    },
  );
}

/**
 * A header tool: the words on a desktop, a 44 px icon on a phone (style.css swaps them). The
 * label also sits on aria-label, so a screen reader hears the same on both.
 */
export function toolButton(
  label: string,
  icon: SVGElement,
  options: { title: string; onClick: () => void },
): HTMLButtonElement {
  const node = button("", options);
  node.setAttribute("aria-label", label);
  node.append(el("span", { class: "attn-tool-icon" }, icon), el("span", { class: "attn-tool-text" }, label));
  return node;
}

/** Read Your move once and show the "N chats waiting on you" link, or nothing at 0 or on a failure. */
export function readWaiting(p: Panel): Promise<void> {
  const line = p.root.querySelector<HTMLElement>(".attn-wait");
  const seq = ++p.waitSeq;
  const hide = () => {
    if (!line || panel !== p || seq !== p.waitSeq) return;
    line.hidden = true;
    line.replaceChildren();
  };
  return fetchYourMove(p.context).then((data) => {
    if (!line || panel !== p || seq !== p.waitSeq) return;
    const n = yourMoveCounts(data).sessions;
    if (!n) return hide();
    const link = el(
      "button",
      { type: "button", class: "linkish attn-wait-link", title: "Open the Session Dashboard" },
      waitingText(n),
    );
    link.addEventListener("click", () => {
      Promise.resolve(p.context.navigation.openPage(DASHBOARD_PAGE_ID)).catch((error: unknown) =>
        flash(line, `Could not open the Dashboard (${describeError(error)}).`, "error"),
      );
    });
    line.replaceChildren(link);
    line.hidden = false;
  }).catch(hide);
}

export const slug = (text: string) => text.replace(/[^A-Za-z0-9_-]+/g, "-");

// ── rows ────────────────────────────────────────────────────────────────────

function iconButton(
  label: string,
  title: string,
  icon: SVGElement,
  focus: string,
  onClick: (self: HTMLButtonElement) => void | Promise<void>,
): HTMLButtonElement {
  const node = button("", { title, onClick });
  node.classList.add("btn-icon", "attn-btn");
  node.setAttribute("aria-label", label);
  node.dataset.focus = focus;
  node.replaceChildren(icon);
  return node;
}

/** Where a row's own messages go: the table's message line, or (the tree) the row itself. */
function rowSink(p: Panel, row: HTMLElement): MessageSink | undefined {
  const table = p.table;
  return table ? (line) => table.message(p, row.dataset.id ?? "", line) : undefined;
}

function titleNode(p: Panel, item: AttentionItem, row: HTMLElement): HTMLElement {
  if (!item.url.startsWith("https://github.com/")) {
    return el("span", { class: "attn-title" }, item.title);
  }
  const link = el(
    "a",
    { class: "attn-title git-link", href: item.url, target: "_blank", rel: "noopener noreferrer" },
    item.title,
  );
  link.addEventListener("click", (event) => {
    event.preventDefault();
    void openOrShow(p.context, item.url, row, rowSink(p, row));
  });
  return link;
}

function workingOf(p: Panel, item: AttentionItem): Worked | null {
  if (item.working) {
    return { ...item.working, started: false };
  }
  return p.worked.get(item.id) ?? null;
}

function workingNode(p: Panel, work: Worked, text: HTMLElement, id: string): HTMLElement {
  const open = el(
    "button",
    {
      type: "button",
      class: "linkish attn-open-chat",
      title: work.path ? `Open this chat (${work.path})` : "Open this chat",
    },
    "Open chat",
  );
  open.addEventListener("click", () => {
    Promise.resolve(p.context.navigation.openSession(work.session_id)).catch((error: unknown) =>
      sayInRow(p, id, `Could not open the chat (${describeError(error)}). It is at ${work.path}.`, "error", text),
    );
  });
  if (work.status === "waiting") {
    return el(
      "span",
      { class: "attn-working attn-waiting" },
      `${work.title} finished. Its answer, and any command to run, is in the chat.`,
      " ",
      open,
    );
  }
  return el(
    "span",
    { class: "attn-working" },
    work.started ? "Chat started. " : "",
    `Claude is on it: ${work.title}`,
    " ",
    open,
  );
}

/** The word on the right of a row: who has the next move. */
type RowStatus = "working" | "waiting";
const STATUS_WORDS: Record<RowStatus, string> = {
  working: "Chat working",
  waiting: "Waiting on you",
};

function statusChip(status: RowStatus): HTMLElement {
  return el("span", { class: `attn-status attn-status-${status}` }, STATUS_WORDS[status]);
}

/** "Box health · Failed units": the source and group an item comes from. */
export function originOf(item: Pick<AttentionItem, "source" | "kind">): string {
  const group = GROUPS[item.source]?.find((spec) => spec.kinds.includes(item.kind));
  return [SOURCE_NAMES[item.source], group?.name].filter(Boolean).join(" · ");
}

/** The row's frame: dot, text column and the empty actions column. */
function rowShell(p: Panel, item: AttentionItem, meta: string) {
  if (p.table) {
    const made = p.table.shell(p, item, meta);
    made.text.append(titleNode(p, item, made.row));
    return made;
  }
  const tone = item.tone === "urgent" ? "red" : "amber";
  const text = el("div", { class: "attn-text" });
  const actions = el("div", { class: "attn-actions" });
  const key = `row:${item.id}`;
  const row = el(
    "div",
    {
      class: `bh-need attn-row bh-t-${tone}`,
      role: "treeitem",
      "aria-level": "3",
      "aria-label": `${item.title}${meta ? `, ${meta}` : ""}`,
      "data-key": key,
      "data-id": item.id,
      "data-kind": item.kind,
      "data-tone": item.tone,
      tabindex: "-1",
    },
    el("span", { class: `bh-dot bh-${tone}`, "aria-hidden": "true" }),
    text,
    actions,
  );
  text.append(titleNode(p, item, row));
  return { row, text, actions };
}

export function rowNode(p: Panel, item: AttentionItem, data: AttentionOverview): HTMLElement {
  const row = rowNodeBuilt(p, item, data);
  // The table fits the opened state and the primary button to the finished row, a rebuilt one included.
  if (p.table && row.classList.contains("nt-row")) p.table.finish(p, row, item, data);
  return row;
}

function rowNodeBuilt(p: Panel, item: AttentionItem, data: AttentionOverview): HTMLElement {
  // A merge in flight is not drawn again: its result lands on the node that asked for it.
  const live = item.url ? mergingNode(item.url) : undefined;
  if (live) return live;
  const merged = item.url ? mergedRows.get(urlKey(item.url)) : undefined;
  if (merged) return mergedRowNode(p, item, merged);
  // The table's group rows already name the source and the group, so its rows leave them out.
  const meta = [p.table ? "" : originOf(item), item.meta, item.back ? `back: ${item.back}` : ""]
    .filter(Boolean)
    .join(" · ");
  const { row, text, actions } = rowShell(p, item, meta);
  let keepMerge: (() => void) | null = null;
  text.append(el("span", { class: "attn-meta" }, meta));
  const work = workingOf(p, item);
  const waiting = !work || work.status === "waiting";
  // The table says "waiting on you" by being in the list; only a chat at work gets a chip there.
  if (!p.table || !waiting) actions.appendChild(statusChip(waiting ? "waiting" : "working"));
  if (work) text.append(workingNode(p, work, text, item.id));
  // A chat still working keeps the row; one that finished hands it back with every action.
  if (!waiting) return row;
  const offer = item.offer;
  if (offer && offer.can_run) {
    const clean = el(
      "button",
      {
        type: "button",
        class: "btn btn-quiet bh-act attn-clean",
        "aria-haspopup": "dialog",
        title: offer.hint || offer.label,
        "data-hint": offer.hint || offer.label,
        "data-focus": `${item.id}:cleanup`,
      },
      offer.label,
    );
    clean.addEventListener("click", () => {
      if (!openCleanup(offer, clean)) {
        sayInRow(p, item.id, "Box health has not listed that cleanup now. Read again and try once more.", "error", text);
      }
    });
    actions.appendChild(clean);
  }
  if (data.can_chat && actionsOf(item).includes("review")) {
    const investigate = item.source === "home";
    actions.appendChild(
      iconButton(
        investigate ? `Investigate ${item.title}` : `Review ${item.title}`,
        investigate ? INVESTIGATE_HINT : "Open a chat that reviews this item and reports back",
        investigate ? toolIcon() : messageIcon(),
        `${item.id}:review`,
        (self) => startChat(p, item, "review", self, text),
      ),
    );
  }
  if (data.can_chat && actionsOf(item).includes("fix")) {
    const command = item.fix_mode === "command";
    actions.appendChild(
      iconButton(
        `Fix ${item.title}`,
        item.fix_note || "Open a chat that fixes this item",
        command ? terminalIcon() : toolIcon(),
        `${item.id}:fix`,
        (self) => openFix(p, item, self, text),
      ),
    );
  }
  const pull = data.can_chat && actionsOf(item).includes("merge") ? pullOf(item) : null;
  if (pull) keepMerge = mergeControls(p, item, data, row, text, actions, pull);
  if (item.dismissible && item.actions.includes("dismiss")) {
    actions.appendChild(
      iconButton(
        `Dismiss ${item.title}`,
        "Hide this until it gets worse or the dismissal runs out",
        eyeOffIcon(),
        `${item.id}:dismiss`,
        (self) => openDismiss(p, item, self),
      ),
    );
  }
  if ((item.source === "git" || isPersonal(item)) && item.url.startsWith("https://github.com/")) {
    actions.appendChild(
      rowMenu(
        { title: item.title },
        [button("Open on GitHub", { onClick: () => openOrShow(p.context, item.url, row, rowSink(p, row)) })],
        {
          icon: dotsIcon(),
          label: `More actions for ${item.title}`,
          title: "More actions: Open on GitHub",
          className: "btn-icon attn-btn",
        },
      ),
    );
  }
  if (keepMerge) keepMerge();
  return row;
}

// ── Merge ──────────────────────────────────────────────────────────────────

/** How long a read card is trusted for the button's state (the server's card cache, 60 s). */
const CARD_TTL_MS = 60_000;

/** What the review record read says; shown under a Merge row, never a gate. */
export type Record_ = { report: ComplianceReport | null } | { error: string };

/** Pull request cards read by a Merge click, with their review record, until they age out. */
const readCards = new Map<string, { card: PrCard; at: number; record: Record_ }>();
/** Cards being read again behind an open confirm, so a redraw starts only one read. */
const rereading = new Set<string>();
/** Rows merged from this panel: the note that replaces the row until the list drops it. */
const mergedRows = new Map<string, string>();

/** The pull request card a Merge click last read for *item*, while it is fresh enough to show. */
export function readCardOf(item: AttentionItem): { card: PrCard; record: Record_ } | null {
  const read = item.url ? readCards.get(urlKey(item.url)) : undefined;
  return read && Date.now() - read.at < CARD_TTL_MS ? { card: read.card, record: read.record } : null;
}

/** The repo and number of a pull request item's GitHub link, or null. */
function pullOf(item: AttentionItem): { repo: string; number: number } | null {
  const found = /^https:\/\/github\.com\/([^/]+\/[^/]+)\/pull\/(\d+)/.exec(item.url);
  return found ? { repo: found[1]!, number: Number(found[2]) } : null;
}

const plural_ = (n: number, one: string) => `${n} ${one}${n === 1 ? "" : "s"}`;

/** What the merge gate rests on, as words under the row. Display only: the server decides. */
export function mergeFacts(card: PrCard): string {
  const parts = [
    `${card.ci.passing} of ${card.ci.total} checks passing`,
    plural_(card.review.approvals, "approval"),
  ];
  if (card.review.changes_requested) {
    parts.push(`${plural_(card.review.changes_requested, "review")} asking for changes`);
  }
  parts.push(card.mergeable.detail || card.mergeable.state);
  return parts.join(" · ");
}

/** Reads the fresh card and its review record; the record's failure never blocks the card. */
async function readPull(
  p: Panel,
  key: string,
  pull: { repo: string; number: number },
): Promise<PrCard> {
  const [card, record] = await Promise.all([
    fetchPrCard(p.context, pull),
    fetchCompliance(p.context, pull).then(
      (found): Record_ => ({ report: found.report }),
      (error: unknown): Record_ => ({ error: describeError(error) }),
    ),
  ]);
  readCards.set(key, { card, at: Date.now(), record });
  return card;
}

/** The review record as one line: its verdict and whether it covers the current head. */
export function recordText(record: Record_): string {
  if ("error" in record) return `Review record not read (${record.error})`;
  const report = record.report;
  if (!report) return "No review record on this PR";
  const covers =
    report.revision === "current"
      ? "current with the head commit"
      : report.revision === "stale"
        ? "stale: the head commit changed since"
        : "head commit not compared";
  return `Review record: ${report.verdict}, ${covers}`;
}

function mergedRowNode(p: Panel, item: AttentionItem, note: string): HTMLElement {
  const { row, text } = rowShell(p, item, note);
  text.append(el("span", { class: "attn-meta", role: "status" }, note));
  return row;
}

/**
 * The Merge button of a ready pull request, and the confirm under its row. A click reads the
 * fresh card and opens the Your move confirm (prs.ts) when the server's gate is open; if it is
 * shut the button stays off with the card's reason. The merge itself is prs.ts's `mergePr`,
 * which the server gates again (green, no conflicts, head pinned, admin); nothing here merges.
 * Returns what to run once the row is fully built: reopen a confirm a redraw closed.
 */
function mergeControls(
  p: Panel,
  item: AttentionItem,
  data: AttentionOverview,
  row: HTMLElement,
  text: HTMLElement,
  actions: HTMLElement,
  pull: { repo: string; number: number },
): () => void {
  const key = urlKey(item.url);
  const read = readCards.get(key);
  // A card ages out after 60 s, except while its confirm is open: that one is read again (once)
  // and kept, so a redraw never closes a confirm the owner is looking at.
  const confirmOpen = confirmIsOpen(item.url);
  const aged = read !== undefined && Date.now() - read.at >= CARD_TTL_MS;
  const card = read && (!aged || confirmOpen) ? read.card : null;
  if (read && aged && confirmOpen && !rereading.has(key)) {
    rereading.add(key);
    void readPull(p, key, pull)
      .catch(() => undefined)
      .finally(() => {
        rereading.delete(key);
        draw(p, true);
      });
  } else if (read && aged && !confirmOpen) {
    readCards.delete(key);
  }
  // Only a confirm this panel opened (it holds a read card) is this panel's to drop: Your move
  // shares prs.ts's confirm map, and its confirm for the same pull request is left alone.
  const lostConfirm = confirmOpen && read !== undefined && (card === null || !card.merge.allowed);
  if (lostConfirm) dropConfirm(item.url);
  const deps = {
    context: p.context,
    refresh: async () => {
      await refresh(p);
    },
  };
  const hooks: MergeHooks = {
    origin: "needs-you",
    rebuild: (fresh) => {
      const kept = readCards.get(key);
      readCards.set(key, { card: fresh, at: Date.now(), record: kept?.record ?? { report: null } });
      return rowNode(p, item, data);
    },
    merged: (done, result) => {
      readCards.delete(key);
      const how = result.method === "squash" ? "squashed" : "merge commit";
      const note = `Merged #${done.number} (${how})${result.sha ? ` ${result.sha.slice(0, 7)}` : ""}`;
      mergedRows.set(key, note);
      return mergedRowNode(p, item, note);
    },
  };
  const shut = card !== null && !card.merge.allowed;
  const merge = iconButton(
    `Merge ${item.title}`,
    shut ? (card.merge.reason ?? "Merge is off") : "Merge (asks to confirm first)",
    gitMergeIcon(),
    `${item.id}:merge`,
    async (self) => {
      self.disabled = true;
      try {
        const fresh = await readPull(p, key, pull);
        if (fresh.merge.allowed) openConfirm(fresh);
      } catch (error) {
        self.disabled = false;
        sayInRow(p, item.id, `Could not check whether it can merge (${describeError(error)}).`, "error", text);
        return;
      }
      draw(p, true);
    },
  );
  merge.disabled = shut;
  actions.appendChild(merge);
  if (card && read) {
    text.append(el("span", { class: "attn-meta attn-merge-facts" }, mergeFacts(card)));
    text.append(el("span", { class: "attn-meta attn-merge-record" }, recordText(read.record)));
    if (shut && card.merge.reason) text.append(el("span", { class: "pr-why" }, card.merge.reason));
  }
  return () => {
    if (card) keepConfirm(deps, card, row, actions, hooks);
    if (lostConfirm) sayInRow(p, item.id, "The merge confirm closed; press Merge again.", "info", text);
  };
}

// ── Fix ─────────────────────────────────────────────────────────────────────

/** One line on what a Fix chat does, per kind (the approval note comes from the server). */
export const FIX_JOBS: Record<string, string> = {
  "pr-failing": "Works on a fresh copy of the PR branch, fixes the failing checks and pushes the fix.",
  "pr-update": "Merges the base branch into the PR branch, or names who has to approve it.",
  "alerts-fix": "Bumps the fixed versions on a new branch and opens a draft PR.",
  "alerts-stuck": "Finds the commit that fixed each alert, then asks you before dismissing it.",
  branch: "Lists the branch's own commits, then asks you before deleting it.",
  sweep: "Finds the root cause in the latest run's log and fixes it on a branch with a draft PR.",
  account: "Names the token and the Key Vault secret behind it, and writes you the prompt that rotates it.",
  "repo-error": "Names the token and the Key Vault secret behind it, and writes you the prompt that rotates it.",
  mount: "Lists the biggest folders under the mount and asks before deleting anything.",
  units: "Reads the unit's journal and ends with one command for you to run in PowerShell.",
};

/** The confirm before a Fix chat: what it does, the approval it will meet, then Start or Cancel. */
function openFix(p: Panel, item: AttentionItem, opener: HTMLElement, text: HTMLElement): void {
  let close: () => void = () => undefined;
  const start = button("Start Fix chat", {
    kind: "primary",
    title: "Open the Fix chat now. It still asks you before every write that needs approval.",
    onClick: () => {
      close();
      void startChat(p, item, "fix", opener, text);
    },
  });
  const cancel = button("Cancel", { title: "Start nothing", onClick: () => close() });
  const body = el(
    "div",
    { class: "attn-dlg" },
    el("p", { class: "attn-dlg-item" }, item.title),
    el("p", { class: "attn-fix-job" }, FIX_JOBS[item.kind] ?? "Opens a chat that fixes this item."),
    el(
      "p",
      { class: "attn-fix-note" },
      el("strong", {}, "Approval: "),
      item.fix_note ?? "The chat asks you before any write that needs approval.",
    ),
    el("div", { class: "attn-dlg-actions" }, start, cancel),
  );
  const dialog = openDialog({
    title: "Start a Fix chat",
    body,
    opener,
    id: "attn-fix",
    closeLabel: "Close",
    className: "attn-dialog-card",
  });
  close = dialog.close;
}

// ── Review ──────────────────────────────────────────────────────────────────

/** Starts a chat (the argument says to ride over the slot limit) or throws; marks what it took. */
type Starter = (rideUnparked: boolean) => Promise<string | void>;

function chatStarter(p: Panel, item: AttentionItem, action: AttentionChatAction): Starter {
  return async (rideUnparked) => {
    const made = await startAttentionChat(p.context, item.id, action, rideUnparked);
    p.worked.set(item.id, {
      session_id: made.session_id,
      title: item.title,
      path: made.path,
      started: !made.existing,
    });
    // The item was already held by the other kind of chat: say so, never show a started Fix.
    const holder = made.holder_action;
    if (made.existing && holder && holder !== action) {
      const [has, want] = holder === "review" ? ["A Review", "Fix"] : ["A Fix", "Review"];
      return `${has} chat already holds this item; open it, or wait for it to finish before ${want}.`;
    }
    return undefined;
  };
}

function batchStarter(
  p: Panel,
  target: AttentionBatchTarget,
  name: string,
  items: AttentionItem[],
): Starter {
  return async (rideUnparked) => {
    const made = await startAttentionBatch(
      p.context,
      target,
      items.map((item) => item.id),
      rideUnparked,
    );
    // The server leaves out items another chat holds; those show as held on the refresh.
    const took = made.item_ids ?? items.map((item) => item.id);
    for (const item of items) {
      if (!took.includes(item.id)) continue;
      p.worked.set(item.id, {
        session_id: made.session_id,
        title: made.title || `Review ${plural(took.length, "item")}: ${name}`,
        path: made.path,
        started: !made.existing,
      });
    }
  };
}

/**
 * Review N (2026-11-16): one chat on the selected items from any sources. The server leaves out
 * what is gone, dismissed, snoozed, personal or held by another chat and names them; the note
 * says how many.
 */
function manyStarter(p: Panel, items: AttentionItem[]): Starter {
  return async (rideUnparked) => {
    const made = await startAttentionReviewMany(
      p.context,
      items.map((item) => item.id),
      rideUnparked,
    );
    const took = made.item_ids ?? items.map((item) => item.id);
    for (const item of items) {
      if (!took.includes(item.id)) continue;
      p.worked.set(item.id, {
        session_id: made.session_id,
        title: made.title || `Review ${plural(took.length, "item")}`,
        path: made.path,
        started: !made.existing,
      });
    }
    const skipped = made.skipped ?? [];
    if (!skipped.length) return undefined;
    // Say which were left out and why (a home item or a failed unit is kept apart on purpose).
    const titles = new Map(items.map((item) => [item.id, item.title]));
    const named = skipped.slice(0, 3).map((s) => `${titles.get(s.id) ?? "An item"} (${s.why})`);
    const more = skipped.length > 3 ? ` and ${skipped.length - 3} more` : "";
    return `Left out of the review: ${named.join("; ")}${more}.`;
  };
}

/** Start a Review N over *items* (already filtered to the ones Review can take). */
export function reviewMany(p: Panel, items: AttentionItem[], opener: HTMLElement, host: HTMLElement): Promise<void> {
  return runStart(p, manyStarter(p, items.slice(0, BATCH_MAX)), opener, host);
}

/** Read the list again and draw it (the table's actions call this after a change). */
export const reloadPanel = (p: Panel): Promise<void> => refresh(p);

async function runStart(
  p: Panel,
  start: Starter,
  opener: HTMLElement,
  host: HTMLElement,
  what = "review",
  rowId: string | null = null,
): Promise<void> {
  try {
    const note = await start(false);
    draw(p, true);
    void refresh(p);
    if (note) {
      const row = [...p.root.querySelectorAll<HTMLElement>(".attn-row")].find(
        (r) => r.dataset.id === (rowId ?? host.closest<HTMLElement>(".attn-row")?.dataset.id),
      );
      const into = row?.querySelector<HTMLElement>(".attn-text") ?? p.root;
      if (rowId) sayInRow(p, rowId, note, "info", into);
      else flash(into, note, "info");
    }
  } catch (error) {
    const full = slotsFullOf(error);
    if (full) openSlotsFull(p, full, opener, start, what);
    else if (rowId) sayInRow(p, rowId, `Could not start the ${what} (${describeError(error)}).`, "error", host);
    else flash(host, `Could not start the ${what} (${describeError(error)}).`, "error");
  }
}

function startChat(
  p: Panel,
  item: AttentionItem,
  action: AttentionChatAction,
  opener: HTMLElement,
  text: HTMLElement,
) {
  return runStart(p, chatStarter(p, item, action), opener, text, action === "fix" ? "Fix chat" : "review", item.id);
}

/** The items a line's Review all would take: Review offered, not held, not just started. */
export function reviewable(p: { worked: Map<string, unknown> }, items: AttentionItem[]): AttentionItem[] {
  return items.filter(
    (item) =>
      actionsOf(item).includes("review") && !held(item) && !assignedElsewhere(item, viewerLogin()) && !p.worked.has(item.id),
  );
}

/**
 * A Review all button over `pool` (a line's items, or a source's items across its lines). The
 * source form posts `{source}` and reads "Review N" (the line form says "Review all N").
 */
export function reviewAllNode(
  p: Panel,
  line: Line,
  target: AttentionBatchTarget,
  pool: AttentionItem[],
  data: AttentionOverview,
  scope: "line" | "source" = "line",
): HTMLButtonElement | null {
  if (!data.can_chat) return null;
  const all = reviewable(p, pool);
  if (all.length < 2) return null;
  const take = all.slice(0, BATCH_MAX);
  const noun = scope === "source" ? "Review" : "Review all";
  const words =
    all.length > BATCH_MAX ? `Review ${BATCH_MAX} of ${all.length}` : `${noun} ${all.length}`;
  const node = el("button", {
    type: "button",
    class: scope === "source" ? "btn btn-secondary attn-review-all attn-review-source" : "btn btn-secondary attn-review-all",
    title: `Opens one chat that reads every item in this ${scope === "source" ? "source" : "line"} and reports one root cause per item. It changes nothing.`,
    "aria-label": `${words} in ${line.name}`,
    "data-focus": `${line.id}:review-all`,
  });
  node.append(listCheckIcon(), el("span", {}, words));
  node.addEventListener("click", (event) => {
    // The line folds on a click; this button is not the fold.
    event.stopPropagation();
    if (node.disabled) return;
    node.disabled = true;
    const host = node.closest<HTMLElement>("[data-line]") ?? node;
    void runStart(p, batchStarter(p, target, line.name, take), node, host).finally(() => {
      node.disabled = false;
    });
  });
  return node;
}

function openSlotsFull(
  p: Panel,
  full: SlotsFull,
  opener: HTMLElement,
  start: Starter,
  what = "review",
): void {
  const status = el("div", { class: "attn-dlg-status", "aria-live": "polite" });
  let close: () => void = () => undefined;
  const copy = button("Copy brief", {
    title: "Copy the brief to start the chat yourself once a slot is free",
    onClick: async () => {
      replaceChildren(status, []);
      if (full.brief && (await copyText(full.brief))) {
        replaceChildren(status, [notice("Copied the brief.", "ok")]);
      } else {
        replaceChildren(status, [
          notice("The clipboard is unavailable here. Select the brief below and copy it.", "info"),
          el("pre", { class: "attn-brief" }, full.brief || "The server sent no brief."),
        ]);
      }
    },
  });
  const anyway = button("Start anyway (over the slot limit)", {
    kind: "primary",
    title: "Start the chat now, over the slot limit. Nothing is started until you press this.",
    onClick: async () => {
      replaceChildren(status, []);
      try {
        await start(true);
        close();
        draw(p, true);
        void refresh(p);
      } catch (error) {
        replaceChildren(status, [notice(`Could not start the ${what} (${describeError(error)}).`, "error")]);
      }
    },
  });
  const holders = full.working.map((holder) => el("li", {}, holder.title));
  const body = el(
    "div",
    { class: "attn-dlg" },
    el(
      "p",
      {},
      full.limit
        ? `All ${plural(full.limit, "slot")} are taken, so a new chat would go over the limit.`
        : "All slots are taken, so a new chat would go over the limit.",
    ),
    holders.length ? el("p", { class: "meta" }, "Chats holding them:") : null,
    holders.length ? el("ul", { class: "attn-holders" }, ...holders) : null,
    el("div", { class: "attn-dlg-actions" }, copy, anyway),
    status,
  );
  const dialog = openDialog({
    title: `No free slot for a ${what === "review" ? "review" : "Fix"} chat`,
    body,
    opener,
    id: "attn-slots",
    closeLabel: "Close",
    className: "attn-dialog-card",
  });
  close = dialog.close;
}

// ── Dismiss ─────────────────────────────────────────────────────────────────

function openDismiss(p: Panel, item: AttentionItem, opener: HTMLElement): void {
  const box = item.source === "box";
  const id = `attn-reason-${slug(item.id)}`;
  const field: HTMLInputElement | HTMLSelectElement = box
    ? el(
        "select",
        { id, class: "attn-reason" },
        el("option", { value: "" }, "No reason"),
        ...BOX_REASONS.map((reason) => el("option", { value: reason }, reason)),
      )
    : el("input", {
        id,
        class: "attn-reason",
        type: "text",
        maxlength: String(REASON_MAX),
        placeholder: "Why (optional)",
        autocomplete: "off",
      });
  const status = el("div", { class: "attn-dlg-status", "aria-live": "polite" });
  let close: () => void = () => undefined;
  const dismiss = button("Dismiss", {
    kind: "primary",
    title: "Hide this item until it gets worse or the dismissal runs out",
    onClick: async () => {
      replaceChildren(status, []);
      try {
        await dismissAttention(p.context, item.id, field.value.trim().slice(0, REASON_MAX));
        forgetHistory(item.id);
      } catch (error) {
        replaceChildren(status, [notice(`Could not dismiss (${describeError(error)}).`, "error")]);
        return;
      }
      close();
      showUndo(p, item);
      await refresh(p);
    },
  });
  const cancel = button("Cancel", { title: "Leave the item as it is", onClick: () => close() });
  const body = el(
    "div",
    { class: "attn-dlg" },
    el("p", { class: "attn-dlg-item" }, item.title),
    item.meta ? el("p", { class: "meta" }, item.meta) : null,
    el("label", { class: "attn-dlg-label", for: id }, box ? "Reason" : "Reason (optional)"),
    field,
    el(
      "p",
      { class: "attn-comes-back" },
      `Comes back if ${item.back_if}, or on ${comesBackOn(item.expires_days)}`,
    ),
    el("div", { class: "attn-dlg-actions" }, dismiss, cancel),
    status,
  );
  const dialog = openDialog({
    title: "Dismiss this item",
    body,
    opener,
    id: "attn-dismiss",
    closeLabel: "Close",
    className: "attn-dialog-card",
  });
  close = dialog.close;
}

function showUndo(p: Panel, item: AttentionItem): void {
  if (p.undo) clearTimeout(p.undo);
  const host = part(p, ".attn-toast");
  const clear = () => {
    host.replaceChildren();
    p.undo = null;
  };
  const undo = button("Undo", {
    title: "Bring this item back",
    onClick: async () => {
      if (p.undo) clearTimeout(p.undo);
      try {
        await restoreAttention(p.context, item.id);
        forgetHistory(item.id);
        clear();
        await refresh(p);
      } catch (error) {
        replaceChildren(host, [notice(`Could not undo (${describeError(error)}).`, "error")]);
      }
    },
  });
  replaceChildren(host, [
    el("div", { class: "attn-undo", role: "status" }, el("span", { class: "attn-undo-text" }, `Dismissed: ${item.title}`), undo),
  ]);
  p.undo = setTimeout(clear, UNDO_MS);
}

function skipFromFold(p: Panel, item: AttentionItem): HTMLButtonElement {
  const skip = el("button", {
    type: "button",
    class: "attn-review-all attn-restore attn-skip",
    title: "Skip this post: it leaves the Social plan and stops counting toward the week",
    "aria-label": `Skip post ${item.title}`,
  });
  skip.append(skipIcon(), el("span", {}, "Skip post"));
  registerPlanButton(item.slot as string, skip);
  skip.addEventListener("click", () => {
    if (skip.disabled) return;
    void changePlan(p, item, "skip");
  });
  return skip;
}

/** The dismissed items as a list with their Restore actions (the old fold and the Needs you table share it). */
export function dismissedList(p: Panel, items: AttentionItem[], listId: string, hidden: boolean): HTMLElement {
  return el(
    "ul",
    { class: "attn-dismissed-list", id: listId, hidden },
    ...items.map((item) => {
      const reason = item.dismissed?.reason ?? "";
      const until = dateOf(item.dismissed?.expires_at);
      // Quiet rows (2026-11-08): no "Dismissed" pill (the fold says it), a compact Restore styled
      // like Review all and level with the title, one detail line, and the reason on one line that
      // opens to its full text on a click or tap.
      const restore = el("button", {
        type: "button",
        class: "attn-review-all attn-restore",
        title: "Put this item back in Needs you",
        "aria-label": `Restore ${item.title}`,
      });
      restore.append(restoreIcon(), el("span", {}, "Restore"));
      restore.addEventListener("click", async () => {
        if (restore.disabled) return;
        restore.disabled = true;
        try {
          await restoreAttention(p.context, item.id);
          forgetHistory(item.id);
          await refresh(p);
        } catch (error) {
          sayInRow(p, item.id, `Could not restore (${describeError(error)}).`, "error", row);
        } finally {
          restore.disabled = false;
        }
      });
      // A Social post hidden before Skip existed: Skip post turns the old hide into a real skip (the plan changes).
      const skip: HTMLElement | null = item.source === "social" && item.slot ? skipFromFold(p, item) : null;
      const why: HTMLElement | null = reason
        ? el(
            "button",
            { type: "button", class: "attn-dismissed-reason", title: reason, "aria-expanded": "false" },
            reason,
          )
        : null;
      why?.addEventListener("click", () => {
        const open = why.getAttribute("aria-expanded") !== "true";
        why.setAttribute("aria-expanded", String(open));
      });
      const row: HTMLElement = el(
        "li",
        { class: "attn-dismissed-row", "data-id": item.id },
        el("span", { class: "attn-title" }, item.title),
        // Skip post and Restore side by side in the button column (a row with no Skip keeps Restore alone).
        skip ? el("span", { class: "attn-fold-acts" }, skip, restore) : restore,
        el(
          "span",
          { class: "attn-meta" },
          [originOf(item), until ? `back ${until}` : ""].filter(Boolean).join(" · "),
        ),
        why,
      );
      return row;
    }),
  );
}

export function dismissedNode(p: Panel, items: AttentionItem[]): HTMLElement | null {
  if (!items.length) return null;
  const listId = "attn-dismissed-list";
  const head = el(
    "button",
    {
      type: "button",
      class: "attn-dismissed-head",
      "aria-expanded": String(p.dismissedOpen),
      "aria-controls": listId,
      title: "Items you hid. Restore brings one back.",
    },
    chevronIcon(),
    `Dismissed (${items.length})`,
  );
  const list = dismissedList(p, items, listId, !p.dismissedOpen);
  head.addEventListener("click", () => {
    p.dismissedOpen = !p.dismissedOpen;
    head.setAttribute("aria-expanded", String(p.dismissedOpen));
    list.hidden = !p.dismissedOpen;
  });
  return el("div", { class: "attn-dismissed" }, head, list);
}

// ── lines ───────────────────────────────────────────────────────────────────

function lineHead(
  p: Panel,
  line: Line,
  open: boolean,
  className: string,
  labelId: string,
  redraw: () => void,
  extra: HTMLElement | null = null,
): HTMLElement {
  const worst = !open && line.worst ? line.worst.title : "";
  const head = el(
    "div",
    { class: `attn-line ${className}` },
    el("span", { class: "attn-chev", "aria-hidden": "true" }, chevronIcon()),
    el(
      "span",
      { class: "attn-label", id: labelId },
      el("span", { class: "attn-name" }, line.name),
      " ",
      el(
        "span",
        {
          class: line.urgent ? "attn-n attn-n-urgent" : "attn-n",
          // A phone shows this as a number badge (style.css); the words stay for a screen reader.
          role: "img",
          "aria-label": countsText(line.urgent, line.review, line.working),
          "data-total": String(line.urgent + line.review),
        },
        countsText(line.urgent, line.review, line.working),
      ),
      worst ? " " : null,
      worst ? el("span", { class: "attn-worst" }, worst) : null,
    ),
    extra,
  );
  head.addEventListener("click", () => {
    saveFold(line.id, !open);
    p.current = `line:${line.id}`;
    redraw();
  });
  return head;
}

function groupNode(
  p: Panel,
  group: Group,
  open: boolean,
  data: AttentionOverview,
  phone: boolean,
  redraw: () => void,
  single = false,
): HTMLElement {
  const limit = phone ? ROWS_SHOWN_PHONE : ROWS_SHOWN;
  const expanded = p.more.has(group.id);
  const shown = expanded ? group.items : group.items.slice(0, limit);
  const rest = group.items.length - shown.length;
  const labelId = `attn-l-${slug(group.id)}`;
  const rows = el(
    "div",
    {
      class: "attn-rows",
      role: "group",
      "aria-label": `${group.name}, ${plural(group.items.length, "item")}`,
      hidden: !open,
    },
    ...shown.map((item) => rowNode(p, item, data)),
  );
  if (group.items.length > limit) {
    const more = el(
      "button",
      {
        type: "button",
        class: "linkish attn-more",
        "aria-expanded": String(expanded),
        title: expanded ? "Show only the first rows again" : "Show every row in this group",
      },
      expanded ? "Show fewer" : `Show ${rest} more`,
    );
    more.addEventListener("click", () => {
      if (p.more.has(group.id)) p.more.delete(group.id);
      else p.more.add(group.id);
      redraw();
    });
    rows.appendChild(more);
  }
  return el(
    "li",
    {
      class: "attn-child bh-todo",
      role: "treeitem",
      "aria-level": "2",
      "aria-expanded": String(open),
      "aria-labelledby": labelId,
      "data-key": `line:${group.id}`,
      "data-line": group.id,
      tabindex: "-1",
    },
    lineHead(
      p,
      group,
      open,
      "bh-todo-h",
      labelId,
      redraw,
      // A source with one group shows only the source button; this one would repeat it.
      group.key === "other" || single
        ? null
        : reviewAllNode(p, group, { line: group.key }, group.items, data),
    ),
    rows,
  );
}

function blockNode(
  p: Panel,
  block: Block,
  open: boolean,
  openOf: (id: string) => boolean,
  data: AttentionOverview,
  phone: boolean,
  redraw: () => void,
): HTMLElement {
  const labelId = `attn-l-${slug(block.id)}`;
  const real = block.groups.filter((group) => group.key !== "other");
  const sourceButton = reviewAllNode(
    p,
    block,
    { source: block.source },
    real.flatMap((group) => group.items),
    data,
    "source",
  );
  return el(
    "li",
    {
      class: "attn-parent",
      role: "treeitem",
      "aria-level": "1",
      "aria-expanded": String(open),
      "aria-labelledby": labelId,
      "data-key": `line:${block.id}`,
      "data-line": block.id,
      tabindex: "-1",
    },
    lineHead(p, block, open, "attn-parent-line", labelId, redraw, sourceButton),
    el(
      "ul",
      { class: "attn-children", role: "group", "aria-label": `${block.name} groups`, hidden: !open },
      ...block.groups.map((group) =>
        groupNode(p, group, openOf(group.id), data, phone, redraw, real.length === 1),
      ),
    ),
  );
}

// ── the tree's keyboard ─────────────────────────────────────────────────────

function visibleItems(tree: HTMLElement): HTMLElement[] {
  return [...tree.querySelectorAll<HTMLElement>('[role="treeitem"]')].filter(
    (item) => !item.parentElement?.closest("[hidden]"),
  );
}

function setCurrent(p: Panel, tree: HTMLElement, key: string | null): void {
  p.current = key;
  for (const item of tree.querySelectorAll<HTMLElement>('[role="treeitem"]')) {
    item.tabIndex = item.dataset.key === key ? 0 : -1;
  }
}

function onTreeKey(p: Panel, tree: HTMLElement, event: KeyboardEvent): void {
  const item = event.target as HTMLElement;
  if (item.getAttribute("role") !== "treeitem") return;
  const items = visibleItems(tree);
  const at = items.indexOf(item);
  const expandable = item.hasAttribute("aria-expanded");
  const open = item.getAttribute("aria-expanded") === "true";
  const go = (target: HTMLElement | undefined) => {
    if (!target) return;
    event.preventDefault();
    setCurrent(p, tree, target.dataset.key ?? null);
    target.focus();
  };
  const toggle = (next: boolean) => {
    event.preventDefault();
    const id = item.dataset.line;
    if (!id) return;
    saveFold(id, next);
    p.current = item.dataset.key ?? null;
    draw(p, true);
  };
  switch (event.key) {
    case "ArrowDown":
      return go(items[at + 1]);
    case "ArrowUp":
      return go(items[at - 1]);
    case "Home":
      return go(items[0]);
    case "End":
      return go(items[items.length - 1]);
    case "ArrowRight":
      if (expandable && !open) return toggle(true);
      if (expandable && open) {
        return go(items[at + 1] && item.contains(items[at + 1]) ? items[at + 1] : undefined);
      }
      return;
    case "ArrowLeft":
      if (expandable && open) return toggle(false);
      return go(item.parentElement?.closest<HTMLElement>('[role="treeitem"]') ?? undefined);
    case "Enter":
    case " ":
      if (expandable) return toggle(!open);
      return;
    default:
  }
}

// ── drawing ─────────────────────────────────────────────────────────────────

/** Expand the urgent lines and close the rest, or close every line (the header's buttons). */
function setAll(p: Panel, blocks: Block[], mode: "urgent" | "none"): void {
  for (const block of blocks) {
    saveFold(block.id, mode === "urgent" && block.urgent > 0);
    for (const group of block.groups) saveFold(group.id, mode === "urgent" && group.urgent > 0);
  }
  draw(p, true);
}

function focusMark(p: Panel): string | null {
  const active = document.activeElement;
  if (!(active instanceof HTMLElement) || !p.root.contains(active)) return null;
  const control = active.closest<HTMLElement>("[data-focus]");
  if (control?.dataset.focus) return `focus:${control.dataset.focus}`;
  const keyed = active.closest<HTMLElement>("[data-key]");
  return keyed?.dataset.key ? `key:${keyed.dataset.key}` : null;
}

function restoreMark(p: Panel, mark: string | null): void {
  if (!mark) return;
  const [kind, ...rest] = mark.split(":");
  const value = rest.join(":");
  const target =
    kind === "focus"
      ? [...p.root.querySelectorAll<HTMLElement>("[data-focus]")].find((n) => n.dataset.focus === value)
      : [...p.root.querySelectorAll<HTMLElement>("[data-key]")].find((n) => n.dataset.key === value);
  target?.focus({ preventScroll: true });
}

/** A confirm for a pull request the list no longer has must not wait for Your move to open it. */
function forgetUnlisted(data: AttentionOverview): void {
  const listed = new Set(data.items.map((entry) => urlKey(entry.url)));
  for (const key of [...readCards.keys()]) {
    if (!listed.has(key) && !mergingNode(key) && !mergedRows.has(key)) {
      dropConfirm(key);
      readCards.delete(key);
    }
  }
}

/** Draw the whole panel from the store; *force* redraws even when the data is unchanged. */
export function draw(p: Panel, force = false): void {
  const { data, error } = getAttention();
  const filter = getFilter();
  const phone = isPhone();
  const signature = JSON.stringify([data, error ? describeError(error) : null, filter, phone]);
  if (!force && signature === p.drawn) return;
  p.drawn = signature;
  const mark = focusMark(p);
  if (p.table) {
    // A confirm for a pull request the list no longer has must not wait for Your move to open it.
    if (data) forgetUnlisted(data);
    p.table.draw(p, { data, error, filter, phone });
    restoreMark(p, mark);
    return;
  }
  const counts = part(p, ".attn-counts");
  const tools = part(p, ".attn-tools");
  const notes = part(p, ".attn-notes");
  const body = part(p, ".attn-body");

  const notesList: Array<Node | null> = [];
  if (error) {
    notesList.push(
      notice(
        data
          ? `Needs you shows the last answer; the refresh failed (${describeError(error)}).`
          : `Needs you could not be read (${describeError(error)}).`,
        "error",
      ),
    );
  }
  if (!data) {
    counts.textContent = "";
    replaceChildren(tools, []);
    replaceChildren(notes, notesList);
    replaceChildren(body, [error ? null : el("p", { class: "meta" }, "Reading...")]);
    return;
  }
  for (const words of data.unknown) notesList.push(el("p", { class: "attn-unknown meta" }, words));
  if (filter) {
    const listed = attentionView(data).items;
    const shown = listed.filter((item) => item.source === filter).length;
    const words = `Showing ${shown} of ${listed.length}`;
    const clear = el(
      "button",
      {
        type: "button",
        class: "attn-chip",
        title: "Show every source again",
        "aria-label": `${words}, only ${SOURCE_NAMES[filter]}. Show all`,
      },
      `${words} · ${SOURCE_NAMES[filter]} · Show all`,
    );
    clear.addEventListener("click", () => setFilter(null));
    notesList.push(el("div", { class: "attn-filter" }, clear));
  }
  replaceChildren(notes, notesList);

  forgetUnlisted(data);
  const all = buildBlocks(data.items);
  const blocks = filter ? all.filter((block) => block.source === filter) : all;
  const defaults = defaultOpen(blocks, phone);
  const openOf = (id: string) => rememberedFold(id) ?? defaults.get(id) ?? false;
  // This older list view shows no marker; it only keeps the seen ids and never reopens a line.
  markNewUrgent(all.flatMap((block) => [block, ...block.groups]), openOf);
  const view = attentionView(data);
  const dismissed = view.dismissed.filter((item) => !filter || item.source === filter);
  const redraw = () => draw(p, true);

  counts.textContent = !view.empty
    ? countsText(view.urgent, view.review, view.working)
    : "";
  replaceChildren(tools, [
    blocks.length
      ? toolButton("Expand urgent", chevronsDownIcon(), {
          title: "Open the sources and groups that have an urgent item, and close the rest",
          onClick: () => setAll(p, all, "urgent"),
        })
      : null,
    blocks.length
      ? toolButton("Collapse all", chevronsUpIcon(), {
          title: "Close every source and group",
          onClick: () => setAll(p, all, "none"),
        })
      : null,
  ]);

  let tree: HTMLElement | null = null;
  if (blocks.length) {
    tree = el(
      "ul",
      { class: "attn-tree", role: "tree", "aria-label": "Needs you" },
      ...blocks.map((block) => blockNode(p, block, openOf(block.id), openOf, data, phone, redraw)),
    );
    const treeNode = tree;
    treeNode.addEventListener("keydown", (event) => onTreeKey(p, treeNode, event));
    treeNode.addEventListener("focusin", (event) => {
      const item = (event.target as HTMLElement).closest<HTMLElement>('[role="treeitem"]');
      if (item && event.target === item) setCurrent(p, treeNode, item.dataset.key ?? null);
    });
    const items = visibleItems(treeNode);
    const stop = items.find((item) => item.dataset.key === p.current) ?? items[0];
    setCurrent(p, treeNode, stop?.dataset.key ?? null);
  }
  replaceChildren(body, [
    tree ??
      (view.empty || filter
        ? el(
            "p",
            { class: "attn-empty" },
            ...(view.empty && !filter && data.unknown.length === 0
              ? [checkIcon(), emptyText(data.read_at)]
              : ["Nothing needs you."]),
          )
        : null),
    data.more ? el("p", { class: "attn-more meta" }, `${data.more} more not shown`) : null,
    dismissedNode(p, dismissed),
  ]);
  restoreMark(p, mark);
}

// ── activate ────────────────────────────────────────────────────────────────

function stopListening(): void {
  stopStore?.();
  stopStore = null;
  stopCards?.();
  stopCards = null;
  if (onFocus) {
    window.removeEventListener("focus", onFocus);
    document.removeEventListener("visibilitychange", onFocus);
    onFocus = null;
  }
  if (phoneQuery && onPhone) phoneQuery.removeEventListener?.("change", onPhone);
  phoneQuery = null;
  onPhone = null;
}

/** Read and draw the panel *root* (from attentionSection), again on every focus and refresh; *options.waiting* false skips the Dashboard link's read. */
export function activateAttention(
  context: ExtensionContext,
  root: HTMLElement,
  options: { waiting?: boolean; table?: TableHooks } = {},
): void {
  // A box admin who is not an OmniGent admin is not sent to the Dashboard's Your move read.
  const waiting = options.waiting !== false;
  stopListening();
  const mine = ++generation;
  const p: Panel = {
    context,
    root,
    table: options.table ?? null,
    openRow: null,
    more: new Set(),
    worked: new Map(),
    dismissedOpen: false,
    current: null,
    undo: null,
    drawn: "",
    lastFilter: getFilter(),
    waitSeq: 0,
  };
  panel = p;
  stopStore = onAttention(() => {
    if (panel !== p) return;
    const filter = getFilter();
    // Filtering to a source opens its line, so the card's link lands on something visible.
    if (filter && filter !== p.lastFilter) saveFold(`attn.${filter}`, true);
    const filtered = filter !== p.lastFilter;
    p.lastFilter = filter;
    // A chat started from here stays marked until the server lists the item as held or gone.
    const items = getAttention().data?.items;
    if (items) {
      for (const id of [...p.worked.keys()]) {
        const found = items.find((item) => item.id === id);
        if (!found || found.working) p.worked.delete(id);
      }
    }
    draw(p, filtered);
  });
  stopCards = onCardState(() => {
    if (panel === p) draw(p, true);
  });
  draw(p, true);
  void refresh(p);
  if (waiting) void readWaiting(p);
  setRereader(() => {
    if (panel !== p) return;
    void refresh(p);
    if (waiting) void readWaiting(p);
  });
  registerRefresh("attention", () => {
    if (generation !== mine) return undefined;
    if (waiting) void readWaiting(p);
    return refresh(p);
  });
  onFocus = () => {
    if (document.visibilityState !== "visible") return;
    void refresh(p);
    if (waiting) void readWaiting(p);
  };
  window.addEventListener("focus", onFocus);
  document.addEventListener("visibilitychange", onFocus);
  if (typeof window.matchMedia === "function") {
    phoneQuery = window.matchMedia(PHONE_QUERY);
    onPhone = () => draw(p);
    phoneQuery.addEventListener?.("change", onPhone);
  }
}

export function deactivateAttention(): void {
  generation++;
  setRereader(null);
  stopListening();
  if (panel?.undo) clearTimeout(panel.undo);
  panel = null;
  resetAttention();
  // A Social row's loaded posts and picture URLs go with the page.
  resetPosts();
}
