// The Admin Center's page shell (2026-11-14, concept D2; replaces the tile strip and the packed board).
//
// Why: the page was one board of every card with Needs you as its first tile. Reid picked D2: a
// header (title, verdict, refresh), tabs, and on the Overview one grouped Needs you table with a
// health rail beside it. Each area (Ship, Box, Cloud, Accounts) that has a connected card gets a tab
// with that area's Needs you rows on top and its cards in a grid below; Services is the Cards panel
// as a page. A server with four or fewer connected cards gets no area tabs, and its cards sit on
// the Overview under the table.
//
// The Needs you table is one element (attention.ts keeps loading and acting, needsTable.ts draws).
// It moves into the tab that is showing and limits itself to that tab's area, so there is one
// read, one set of counts and one open row. The tabs follow the WAI-ARIA Tabs pattern: a tablist,
// tabs with aria-selected and a roving tabindex, arrow keys, Home and End.
//
// The open tab is remembered per viewer in the SDK's `storage.user` (key `admin.tab`), in memory
// when storage is unavailable, the same pattern as sections.ts and cardPrefs.ts.

import type { ExtensionContext } from "@omnigent/extension-sdk";
import { loadActivity, getActivity, onActivity } from "./activityStore";
import { mountActivity, mountDoneList } from "./activityPanel";
import { attentionSection } from "./attention";
import { attentionView } from "./attentionView";
import { getAttention, onAttention, setFilter } from "./attentionStore";
import { CARD_SETTINGS_EVENT, type CardId, type CardSet, groupOf, mountBoard, summaryText, WALL_OPEN_EVENT } from "./board";
import { REVEAL_CARD_EVENT, goToCard } from "./cardNav";
import { CARD_TILES, cardGone, getHidden, onCardState } from "./cardState";
import { openCardsPanel, type OpenCardsPanel } from "./cardsPanel";
import { button, el, replaceChildren } from "./dom";
import { refreshIcon } from "./icons";
import { areaCounts, currentConnected, tabsFor, type TabSpec, unreadSources, sourcesOfArea } from "./needsModel";
import { mountPane, type Pane } from "./needsPane";
import { createNeedsTable, type NeedsTable } from "./needsTable";
import { mountChips, mountRail } from "./rail";
import { mountWall, type Wall } from "./wall";
import { DOMAINS, getTile, mountUpdated, onTiles, refreshAll, type DomainId } from "./tiles";
import { mountVerdict } from "./verdict";
import { cardNotSetUp } from "./board";

export const TAB_KEY = "admin.tab";
export const HIDDEN_NOTE_KEY = "admin.hiddenNote";
export const HIDDEN_NOTE =
  "Hidden cards now hide only the card; their items stay in Needs you.";

export interface Shell {
  main: HTMLElement;
  /** The Needs you panel's root: pass it to activateAttention with `table.hooks`. */
  attention: HTMLElement;
  table: NeedsTable;
  /** Read what this viewer left open last time (the tab, whether the hidden note was seen). */
  loadPrefs: () => Promise<void>;
  /** Draw the tabs and panels once the prefs, the card state and the first tiles are in. */
  layout: () => void;
  /** Show a tab by id (false when this page has no such tab). */
  show: (id: TabSpec["id"]) => boolean;
  stop: () => void;
}

export interface ShellOptions {
  context: ExtensionContext;
  /** "box": a box admin who is not an OmniGent admin: no tabs, the table and the Box health card. */
  mode: "full" | "box";
  cards: CardSet;
  /**
   * How long the first layout waits for the cards' first tile reads (ms) so a server near the
   * four-card line does not flip its tabs on load. 0 lays out at once (tests).
   */
  settleMs?: number;
}

/** The Overview's three width bands (2026-11-17): the wall beside the list, the wall below it, no wall. */
export type WallBand = "wide" | "stack" | "off";
export const WIDE_PX = 1400;
export const STACK_PX = 760;
/** A boundary is crossed only by this many px past it, so a width resting on one cannot flicker. */
export const BAND_HYSTERESIS = 8;
/** The workspace is at least this tall; a shorter window scrolls the page as it does today. */
export const WORKSPACE_MIN_PX = 560;

/**
 * The band for the admin container's width. The first measurement (*prev* null) is classified at the
 * exact thresholds; later ones keep *prev* while the width is within the hysteresis of a boundary.
 */
export function bandFor(width: number, prev: WallBand | null = null): WallBand {
  if (prev === null) return width >= WIDE_PX ? "wide" : width >= STACK_PX ? "stack" : "off";
  const wide = prev === "wide" ? width >= WIDE_PX - BAND_HYSTERESIS : width >= WIDE_PX + BAND_HYSTERESIS;
  if (wide) return "wide";
  const stack = prev === "off" ? width >= STACK_PX + BAND_HYSTERESIS : width >= STACK_PX - BAND_HYSTERESIS;
  return stack ? "stack" : "off";
}

const PANEL = (id: string): string => `as-panel-${id}`;
const TAB = (id: string): string => `as-tab-${id}`;

export function createShell(options: ShellOptions): Shell {
  const { context, mode, cards } = options;
  const settleMs = options.settleMs ?? 4000;
  const full = mode === "full";
  const attention = attentionSection();
  const verdict = el("p", { class: "admin-verdict", "aria-live": "polite" });
  const updated = el("p", { class: "meta admin-updated" });
  const again = button("", { title: "Read everything again", onClick: () => refreshAll() });
  again.classList.add("btn-icon", "admin-refresh");
  again.setAttribute("aria-label", "Read everything again");
  again.replaceChildren(refreshIcon());
  const note = el("div", { class: "as-note", hidden: true });
  const tabList = el("div", { class: "as-tabs", role: "tablist", "aria-label": "Admin Center sections", hidden: !full });
  const body = el("div", { class: "as-body" });
  const main = el(
    "main",
    { class: "fabric admin as" },
    el("header", { class: "fabric-top" }, el("h1", { class: "fabric-h" }, "Admin Center"), verdict, updated, again),
    note,
    tabList,
    body,
  );

  let active: TabSpec["id"] = "overview";
  let tabs: TabSpec[] = [];
  let signature = "";
  let slots = new Map<string, HTMLElement>();
  let stopBoards: Array<() => void> = [];
  let stopRail: Array<() => void> = [];
  // Each board's `layout` hands its cards back; the wall holds them while the Overview shows (wall.ts).
  let boardLayouts: Array<(force?: boolean) => void> = [];
  let wall: Wall | null = null;
  // The pane that takes the wall's place when a line is opened at the wide band (needsPane.ts).
  let pane: Pane | null = null;
  let done: ReturnType<typeof mountDoneList> | null = null;
  // Null until the admin container has been measured (a hidden or unattached page reads 0 wide).
  let band: WallBand | null = null;
  let stopBand: Array<() => void> = [];
  let services: OpenCardsPanel | null = null;
  // The Services panel does not depend on the tab list: one node for the page's life, so a token being
  // typed, open setup folds and focus survive a rebuild of the tabs (it is never detached).
  const servicesPanel = el(
    "section",
    { class: "as-panel as-panel-services", role: "tabpanel", id: PANEL("services"), "aria-labelledby": TAB("services"), hidden: true },
    el("div", { class: "as-services" }),
  );
  let pending = false;
  let settleTimer: ReturnType<typeof setTimeout> | null = null;
  let tabMemory: string | null = null;
  let noteSeen = false;
  let stopped = false;
  const stops: Array<() => void> = [];

  // The table goes where it is needed; an area tab limits it to that area.
  const table = createNeedsTable({
    openTab: (area) => show(area),
    hasTab: (area) => tabs.some((tab) => tab.id === area),
    openCard: (card, area) => {
      if (!show(area)) show("overview");
      goToCard(card);
    },
    // A line fills the pane only while the Overview shows at the wide band; anywhere else it opens in place.
    paneWide: () => active === "overview" && band === "wide",
    // The source's own card, when the wall would show it (on, visible, set up): the pane borrows the real one.
    cardNode: (id) => {
      const card = cards[id as CardId];
      return card && !cardGone(id) && !cardNotSetUp(id as CardId) ? card : null;
    },
  });


  // ── prefs ─────────────────────────────────────────────────────────────────

  const loadPrefs = async (): Promise<void> => {
    try {
      const saved = await context.storage.user.get<unknown>(TAB_KEY);
      if (typeof saved === "string") tabMemory = saved;
      noteSeen = (await context.storage.user.get<unknown>(HIDDEN_NOTE_KEY)) === true;
    } catch {
      // Storage unavailable: the Overview, and the note shows until this visit's dismissal.
    }
    // The activity log decides whether the Activity tab exists. It is started here and never waited
    // for: a slow or missing route must not hold the page up, and the tab appears when it answers.
    if (full) void loadActivity(context, true);
  };

  const remember = (key: string, value: unknown): void => {
    try {
      context.storage.user.set(key, value).catch(() => {
        // Refused: remembered in memory until the page closes.
      });
    } catch {
      // The same, refused before a promise was made.
    }
  };

  // ── the one-time note for a viewer who has hidden cards ───────────────────

  const drawNote = (): void => {
    const show = full && getHidden().size > 0 && !noteSeen;
    note.hidden = !show;
    if (!show) {
      replaceChildren(note, []);
      return;
    }
    if (note.childElementCount) return;
    const gotIt = button("Got it", { title: "Do not show this note again", onClick: () => {
      noteSeen = true;
      remember(HIDDEN_NOTE_KEY, true);
      drawNote();
    } });
    note.append(el("span", {}, HIDDEN_NOTE), " ", gotIt);
  };

  // ── tabs ──────────────────────────────────────────────────────────────────

  const badgeOf = (id: TabSpec["id"]): { text: string; tone: "urgent" | "review" | "ok" } | null => {
    const { data } = getAttention();
    if (!data || id === "services" || id === "activity") return null;
    const unread = unreadSources(data);
    let urgent: number;
    let review: number;
    let questioned: boolean;
    if (id === "overview") {
      const view = attentionView(data);
      urgent = view.urgent;
      review = view.review;
      questioned = unread.size > 0;
    } else {
      const counts = areaCounts(id);
      if (!counts) return null;
      urgent = counts.urgent;
      review = counts.review;
      questioned = sourcesOfArea(id).some((source) => unread.has(source));
    }
    const total = urgent + review;
    if (!total && !questioned) return null;
    return { text: `${total || ""}${questioned ? "?" : ""}`, tone: urgent ? "urgent" : "review" };
  };

  const drawQuiet = (): void => {
    const { data } = getAttention();
    const quiet = !!data && attentionView(data).empty && unreadSources(data).size === 0;
    for (const node of body.querySelectorAll<HTMLElement>(".as-ov:not(.as-ov-box)")) node.classList.toggle("is-quiet", quiet);
  };

  const drawBadges = (): void => {
    drawQuiet();
    for (const tab of tabs) {
      const node = tabList.querySelector<HTMLElement>(`#${TAB(tab.id)} .as-badge`);
      if (!node) continue;
      const badge = badgeOf(tab.id);
      node.hidden = !badge;
      node.textContent = badge?.text ?? "";
      node.className = `as-badge as-badge-${badge?.tone ?? "ok"}`;
      node.setAttribute("aria-label", badge ? `${badge.text.replace("?", " (some could not be read)")} in Needs you` : "");
    }
  };

  const select = (id: TabSpec["id"], focus = false, save = true): void => {
    // A source filter belongs to the tab it was set on; another tab starts from the whole list.
    if (id !== active) {
      setFilter(null);
      // A tab change closes the pane (the focus stays where the tab change put it).
      pane?.clear(false);
    }
    active = id;
    for (const tab of tabs) {
      const node = tabList.querySelector<HTMLElement>(`#${TAB(tab.id)}`);
      const on = tab.id === id;
      node?.setAttribute("aria-selected", String(on));
      if (node) node.tabIndex = on ? 0 : -1;
      const panel = body.querySelector<HTMLElement>(`#${PANEL(tab.id)}`);
      if (panel) panel.hidden = !on;
    }
    if (focus) tabList.querySelector<HTMLElement>(`#${TAB(id)}`)?.focus();
    // The table sits in the showing tab and is limited to that tab's area.
    const slot = slots.get(id);
    if (slot) {
      if (attention.parentElement !== slot) slot.prepend(attention);
      table.setArea(id === "overview" || id === "services" || id === "activity" ? null : id);
    }
    if (id === "services" && !services) {
      const host = servicesPanel.querySelector<HTMLElement>(".as-services");
      if (host) services = openCardsPanel(context, {
          into: host,
          notSetUp: (card) => cardNotSetUp(card as CardId),
          doing: (card) => summaryText(card as CardId),
        });
    }
    if (id === "overview") {
      takeWall();
      if (band === "wide") sizeWorkspace();
    } else giveBack();
    if (save) remember(TAB_KEY, id);
  };

  // ── the wall: the real cards, on the Overview while it is showing ─────────

  const takeWall = (): void => {
    if (stopped || !wall) return;
    if (active === "overview" && band && band !== "off") {
      wall.take();
      // A board's rebuild may have pulled the card the pane holds; it goes back to the pane's foot.
      pane?.retake();
    } else giveBack();
  };
  const giveBack = (): void => {
    // Only when the wall held cards: a board's own rebuild raises admin-arranged, which calls takeWall.
    if (!wall?.release()) return;
    for (const layoutBoard of boardLayouts) layoutBoard(true);
  };
  const ovNode = (): HTMLElement | null => body.querySelector<HTMLElement>(".as-ov[data-wall]");
  /** The workspace is as tall as the window below the tabs (at least 560 px). */
  const sizeWorkspace = (): void => {
    const ov = ovNode();
    // A hidden panel reports a top of 0: size it only while the Overview is the open tab.
    if (!ov || active !== "overview") return;
    const top = ov.getBoundingClientRect().top + window.scrollY;
    ov.style.setProperty("--as-ov-h", `${Math.max(WORKSPACE_MIN_PX, Math.round(window.innerHeight - top - 28))}px`);
  };
  const applyBand = (): void => {
    const ov = ovNode();
    if (!ov) return;
    const width = main.clientWidth || 0;
    if (!width) return;
    const next = bandFor(width, band);
    const changed = next !== band;
    band = next;
    ov.dataset.wall = band;
    // Under the wide band there is no pane: it closes and nothing opens in place in its stead.
    if (band !== "wide") pane?.clear(false);
    if (changed) {
      done?.setRows(band === "wide" ? "fit" : 6);
      takeWall();
      // A source's line draws closed at the wide band and as saved below it: the list draws again.
      table.redraw();
    }
    if (band === "wide") sizeWorkspace();
    done?.fit();
  };

  const show = (id: TabSpec["id"]): boolean => {
    if (!tabs.some((tab) => tab.id === id)) return false;
    select(id);
    return true;
  };

  const onTabKey = (event: KeyboardEvent): void => {
    const at = tabs.findIndex((tab) => tab.id === active);
    const move = (to: number): void => {
      event.preventDefault();
      const next = tabs[(to + tabs.length) % tabs.length];
      if (next) select(next.id, true);
    };
    if (event.key === "ArrowRight") move(at + 1);
    else if (event.key === "ArrowLeft") move(at - 1);
    else if (event.key === "Home") move(0);
    else if (event.key === "End") move(tabs.length - 1);
  };

  // ── the panels ────────────────────────────────────────────────────────────

  const board = (ids: CardId[], single = false): { node: HTMLElement; stop: () => void } => {
    const subset: CardSet = {};
    for (const id of ids) if (cards[id]) subset[id] = cards[id];
    const node = el("div", { class: "admin-board as-board" });
    // One card and no panel to open: the gear stays out (style.css).
    if (single) node.dataset.single = "";
    const mounted = mountBoard(main, node, subset);
    mounted.refold();
    boardLayouts.push(mounted.layout);
    // A board that rebuilds pulls its cards back from the wall; the wall takes them again.
    node.addEventListener("admin-arranged", () => takeWall());
    return { node, stop: mounted.stop };
  };

  const rebuild = (): void => {
    // The wall's cards go back to their boards before the boards are replaced.
    // The pane's target and open row survive a rebuild of the tabs (shown again below); no redraw of the table here,
    // which is about to be rebuilt.
    const kept = pane?.current() ?? null;
    pane?.stop();
    wall?.release();
    for (const stop of stopBoards) stop();
    for (const stop of stopRail) stop();
    for (const stop of stopBand) stop();
    wall?.stop();
    table.attachPane(null);
    pane = null;
    done?.stop();
    stopBoards = [];
    stopRail = [];
    stopBand = [];
    boardLayouts = [];
    wall = null;
    done = null;
    band = null;
    slots = new Map();
    const panels: HTMLElement[] = [];
    const tabNodes: HTMLElement[] = [];
    const cardIds = (area: DomainId): CardId[] =>
      (Object.keys(cards) as CardId[]).filter((id) => groupOf(id) === area);
    // Area tabs are the four area tabs only: the Activity tab does not make a four-card server an area server.
    const areaTabs = tabs.some((tab) => DOMAINS.some((domain) => domain.id === tab.id));
    const tabsHooks = {
      openTab: (area: DomainId) => void show(area),
      tabs: (area: DomainId) => tabs.some((tab) => tab.id === area),
      activity: tabs.some((tab) => tab.id === "activity") ? () => void show("activity") : null,
    };

    for (const tab of tabs) {
      tabNodes.push(
        el(
          "button",
          {
            type: "button",
            class: "as-tab",
            role: "tab",
            id: TAB(tab.id),
            "aria-controls": PANEL(tab.id),
            "aria-selected": "false",
            tabindex: "-1",
          },
          el("span", { class: "as-tab-name" }, tab.label),
          el("span", { class: "as-badge", hidden: true }),
        ),
      );
      if (tab.id === "services") continue;
      if (tab.id === "activity") {
        const host = el("div", { class: "as-activity" });
        panels.push(
          el(
            "section",
            { class: "as-panel as-panel-activity", role: "tabpanel", id: PANEL("activity"), "aria-labelledby": TAB("activity"), hidden: true },
            host,
          ),
        );
        stopRail.push(mountActivity(host));
        continue;
      }
      const panel = el("section", { class: `as-panel as-panel-${tab.id}`, role: "tabpanel", id: PANEL(tab.id), "aria-labelledby": TAB(tab.id), hidden: true });
      if (tab.id === "overview") {
        const slot = el("div", { class: "as-slot" });
        slots.set("overview", slot);
        const main_ = el("div", { class: "as-ov-main" }, el("div", { class: "as-chips" }), slot);
        // Four or fewer connected cards: no area tabs, and the cards sit under the table.
        if (!areaTabs) {
          const all = board((Object.keys(cards) as CardId[]));
          stopBoards.push(all.stop);
          main_.append(all.node);
        }
        const rail = el("aside", { class: "as-rail", "aria-label": "Health by area" });
        // The Done list and the wall (2026-11-17) take the rail's place on a server with area tabs.
        // A quiet day (nothing needs you, every source read): the same blocks as large tiles under the line.
        const tiles = el("div", { class: "as-tiles", "aria-label": "Health by area", role: "group" });
        let ov: HTMLElement;
        if (areaTabs) {
          const doneHost = el("section", { class: "as-done", "aria-label": "Done", hidden: true });
          done = mountDoneList(doneHost, { openActivity: tabsHooks.activity, rows: 6 });
          const wallHost = el("div", { class: "as-wall", "aria-label": "Every card, by area" });
          wall = mountWall(wallHost, cards);
          main_.append(doneHost, tiles);
          // The pane takes the wall's grid area while a source or an area is open (style.css, data-pane).
          const paneHost = el("section", { class: "as-pane" });
          ov = el("div", { class: "as-ov", "data-wall": "off" }, main_, wallHost, paneHost);
          pane = mountPane(paneHost, {
            ...table.paneDeps,
            lend: (card) => wall?.lend(card),
            onChange: (open) => {
              if (open) ov.dataset.pane = "1";
              else delete ov.dataset.pane;
            },
          });
          table.attachPane(pane);
        } else {
          main_.append(tiles);
          ov = el("div", { class: "as-ov" }, main_, rail);
          stopRail.push(mountRail(rail, tabsHooks));
        }
        panel.append(ov);
        stopRail.push(mountRail(tiles, tabsHooks));
        const chips = main_.querySelector<HTMLElement>(".as-chips")!;
        stopRail.push(mountChips(chips, tabsHooks));
      } else {
        const slot = el("div", { class: "as-slot" });
        slots.set(tab.id, slot);
        const area = board(cardIds(tab.id));
        stopBoards.push(area.stop);
        panel.append(slot, area.node);
      }
      panels.push(panel);
    }
    replaceChildren(tabList, tabNodes);
    // Every panel but Services is replaced; Services stays where it is, last.
    if (servicesPanel.parentElement !== body) body.append(servicesPanel);
    for (const child of [...body.children]) if (child !== servicesPanel) child.remove();
    for (const panel of panels) body.insertBefore(panel, servicesPanel);
    for (const node of tabNodes) {
      node.addEventListener("click", () => select(node.id.replace("as-tab-", "") as TabSpec["id"]));
      node.addEventListener("keydown", onTabKey);
    }
    if (wall) {
      // The Overview's band follows the admin container's width (a ResizeObserver, the window's resize
      // where there is none); the workspace height follows the window.
      applyBand();
      const onWindow = (): void => {
        if (band === "wide") sizeWorkspace();
      };
      window.addEventListener("resize", onWindow);
      stopBand.push(() => window.removeEventListener("resize", onWindow));
      if (typeof ResizeObserver !== "undefined") {
        const observer = new ResizeObserver(() => applyBand());
        observer.observe(main);
        stopBand.push(() => observer.disconnect());
      } else {
        window.addEventListener("resize", applyBand);
        stopBand.push(() => window.removeEventListener("resize", applyBand));
      }
    }
    // The pane the rebuild closed opens again on the same target when it can (the Overview at the wide band).
    if (kept && pane && band === "wide" && active === "overview") pane.show(kept, false);
  };

  let ready = false;
  const relayout = (): void => {
    if (stopped) return;
    tabList.hidden = !full || pending;
    if (!full) {
      // A box admin: no tabs; the table, and the Box health card beside it.
      tabs = [{ id: "overview", label: "Overview" }];
      const slot = el("div", { class: "as-slot" });
      const here = board(Object.keys(cards) as CardId[], true);
      stopBoards.push(here.stop);
      body.replaceChildren(el("div", { class: "as-ov as-ov-box" }, el("div", { class: "as-ov-main" }, slot), here.node));
      slots.set("overview", slot);
      slot.prepend(attention);
      table.setArea(null);
      return;
    }
    const next = tabsFor(currentConnected(new Set(Object.keys(cards))), getActivity().loaded);
    const nextSignature = next.map((tab) => tab.id).join(",");
    if (nextSignature !== signature) {
      const previous = active;
      // The tabs and boards are drawn again; a control that had the focus keeps it (the cards move,
      // they are not rebuilt), and a note or board that goes away hands it to the showing board.
      const focused = document.activeElement as HTMLElement | null;
      const hadFocus = !!focused && body.contains(focused);
      tabs = next;
      signature = nextSignature;
      rebuild();
      const wanted = tabs.some((tab) => tab.id === previous && previous !== "overview")
        ? previous
        : tabs.some((tab) => tab.id === tabMemory)
          ? (tabMemory as TabSpec["id"])
          : "overview";
      tabMemory = null;
      select(wanted, false, false);
      table.redraw();
      if (hadFocus && focused) {
        if (focused.isConnected) focused.focus({ preventScroll: true });
        else {
          const target = body.querySelector<HTMLElement>(`#${PANEL(active)} .admin-board`) ?? tabList.querySelector<HTMLElement>(`#${TAB(active)}`);
          if (target) {
            if (!target.matches("button")) target.tabIndex = -1;
            target.focus({ preventScroll: true });
          }
        }
      }
    }
    drawBadges();
    drawNote();
  };

  /** True once every card on the page has had a tile read (or has none to give). */
  const settled = (): boolean =>
    (Object.keys(cards) as CardId[]).every(
      (id) => cardGone(id) || CARD_TILES[id].length === 0 || CARD_TILES[id].some((tile) => getTile(tile) !== undefined),
    );
  const finishSettle = (): void => {
    if (!pending) return;
    pending = false;
    if (settleTimer) clearTimeout(settleTimer);
    settleTimer = null;
    relayout();
  };

  /**
   * The first call (after the prefs and the card state are in) starts the tabs. A full page waits for
   * the cards' first tile reads before it decides which tabs to draw, so a server near the four-card
   * line does not flip them on load; until then only the table shows. A timer caps the wait.
   */
  const layout = (): void => {
    ready = true;
    if (!full || settleMs <= 0 || settled()) {
      relayout();
      return;
    }
    pending = true;
    tabList.hidden = true;
    const slot = el("div", { class: "as-slot" });
    slots = new Map([["overview", slot]]);
    body.replaceChildren(el("div", { class: "as-ov" }, el("div", { class: "as-ov-main" }, slot)), servicesPanel);
    slot.prepend(attention);
    table.setArea(null);
    settleTimer = setTimeout(finishSettle, settleMs);
  };
  const follow = (): void => {
    if (!ready) return;
    if (pending) {
      if (settled()) finishSettle();
      return;
    }
    relayout();
  };

  // ── wiring ────────────────────────────────────────────────────────────────

  mountUpdated(updated);
  mountVerdict(verdict, { fine: full });
  main.addEventListener(CARD_SETTINGS_EVENT, (event) => {
    if (!full) return;
    const detail = (event as CustomEvent<CardId | null>).detail;
    if (!show("services")) return;
    if (detail) services?.show(detail);
  });
  main.addEventListener(WALL_OPEN_EVENT, (event) => {
    const id = (event as CustomEvent<CardId>).detail;
    // The card's title on the wall: show the card's own tab (the cards go back to their boards), then go to it.
    if (!show(groupOf(id))) return;
    goToCard(id);
  });
  main.addEventListener(REVEAL_CARD_EVENT, (event) => {
    const id = (event as CustomEvent<CardId>).detail;
    const area = groupOf(id);
    if (!show(area)) show("overview");
  });
  if (full) {
    stops.push(onTiles(follow), onCardState(follow));
  }
  stops.push(onAttention(drawBadges));
  // A card turned on, off or hidden changes what the open pane's foot may hold: it draws again, keeping its target,
  // its open row, the focus and the scroll.
  stops.push(onCardState(() => pane?.refresh()));
  // The log is read again (at most every 30 s) whenever Needs you is, and the tabs follow when the
  // route first answers or stops answering.
  stops.push(
    onAttention(() => {
      if (full) void loadActivity(context);
    }),
    onActivity(() => {
      if (full) follow();
    }),
  );

  return {
    main,
    attention,
    table,
    loadPrefs,
    layout,
    show,
    stop: () => {
      stopped = true;
      if (settleTimer) clearTimeout(settleTimer);
      table.dispose();
      for (const stop of stops) stop();
      for (const stop of stopBoards) stop();
      for (const stop of stopRail) stop();
      for (const stop of stopBand) stop();
      wall?.stop();
      done?.stop();
      services?.close();
      services = null;
    },
  };
}
