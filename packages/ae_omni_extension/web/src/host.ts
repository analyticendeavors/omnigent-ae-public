// The Admin Center's Box host section (2026-10-20): whether the box host is
// connected to the server, and a button that sends Reid a reconnect link when
// it is not.
//
// Why: when the server rejects the host's credential (the 2026-10-20
// Microsoft sign-in cutover did), the host retries forever and no session
// can start. The fix is `omnigent login` on the box, a sign-in link opened
// within five minutes, then a host restart. deploy/host-relink.sh does that
// by itself after 30 straight rejections or a failed host unit; this card
// shows how far it got and lets Reid ask for a link sooner. The server keeps the state
// (`/v1/ae/host`, ae_omni_policies/host_relink.py).
//
// One card in the Fabric row's style (.card-fabric): a dot, the host's name
// and a state pill, what the helper last reported, and the button. The
// button is hidden while the host is connected: a relink restarts the host,
// which ends every session on it, and the server refuses it then too. It is
// hidden while a login loop runs as well, since a second one would race it;
// while a link waits for the sign-in the card only counts its five minutes
// down.
//
// Polling: on activate, every 15 s while the host is not connected and every
// 60 s while it is, and on focus, as deploys.ts does; a scheduled read is
// skipped while the tab is hidden.
//
// 2026-10-20, the dashboard redesign: the row is the first in the Box health
// card (board.ts), above the server's secrets, and its "Updated" time sits in
// that card's header. The button's rules are unchanged. After every draw it
// hands the page its Box host tile (tiles.ts): a dot and the state, green when
// connected, amber while a relink is under way, red when rejected or not
// connected; a red or amber tile unfolds the card on a phone.

import type { ExtensionContext } from "@omnigent/extension-sdk";
import {
  AeRequestError,
  describeError,
  fetchHostStatus,
  type HostStatus,
  requestHostRelink,
} from "./api";
import { whenText } from "./databricks";
import { button, el, notice, replaceChildren } from "./dom";
import type { FabricTone } from "./icons";
import { registerRefresh, setTile, staleTile, type Tile } from "./tiles";

export const POLL_CONNECTED_MS = 60_000;
export const POLL_DISCONNECTED_MS = 15_000;
export const ASK_LABEL = "Send me a reconnect link";

type State = HostStatus["state"];

// ── words ───────────────────────────────────────────────────────────────────

export function toneOf(state: State): FabricTone {
  if (state === "connected") return "on";
  if (state === "rejected" || state === "failed") return "failed";
  if (state === "sending" || state === "sent" || state === "restarting") return "busy";
  return "unknown";
}

const LABELS: Record<State, string> = {
  connected: "Connected",
  rejected: "Rejected",
  sending: "Getting a link",
  sent: "Waiting for sign-in",
  restarting: "Restarting",
  failed: "Not reconnected",
  offline: "Not connected",
  unknown: "Unknown",
};

export function stateText(state: State): string {
  return LABELS[state];
}

/** "4:12 left", counted down from when the answer arrived (`at`). */
export function countdownText(status: HostStatus, at: number, now: number): string {
  const left = Math.max(0, Math.round(status.expires_in_s - (now - at) / 1000));
  const minutes = Math.floor(left / 60);
  const seconds = String(left % 60).padStart(2, "0");
  return `${minutes}:${seconds} left`;
}

/** The muted facts under the name. */
export function metaItems(status: HostStatus, at: number, now: number): string[] {
  const of = status.attempts > 1 ? ` ${status.attempt} of ${status.attempts}` : "";
  switch (status.state) {
    case "rejected": {
      const times = `Rejected ${status.streak} ${status.streak === 1 ? "time" : "times"} in a row`;
      const since = status.since ? ` since ${whenText(Date.parse(status.since), now)}` : "";
      return [`${times}${since}`];
    }
    case "sending":
      return [`Getting sign-in link${of}`];
    case "sent": {
      const expires = status.expires_at ? Date.parse(status.expires_at) : Number.NaN;
      const items = [`Link${of} sent to your phone`];
      if (!Number.isNaN(expires)) {
        items.push(`Expires ${whenText(expires, now)} (${countdownText(status, at, now)})`);
      }
      return items;
    }
    case "restarting":
      return ["Signed in, restarting the host"];
    case "failed":
      return [failedText(status)];
    case "offline":
      return ["The host is not connected to the server"];
    default:
      return [];
  }
}

/** Why the last loop failed, as the helper reported it. */
export function failedText(status: HostStatus): string {
  const links = `${status.attempts} ${status.attempts === 1 ? "link" : "links"}`;
  if (status.reason === "restart") return "Signed in, but the host did not restart";
  if (status.reason === "push") return `${links} could not be sent to your phone`;
  return `${links} lapsed without a sign-in`;
}

/** The notices under the row: what happens next, or what went wrong. */
export function noticeTexts(status: HostStatus): Array<{ text: string; kind: "error" | "info" }> {
  const items: Array<{ text: string; kind: "error" | "info" }> = [];
  if (status.state === "rejected") {
    items.push({
      kind: "error",
      text:
        "The server is turning the host away (HTTP 403), so sessions cannot start. " +
        "The box sends a sign-in link on its own after 30 rejections; the button sends one now.",
    });
  } else if (status.state === "sent") {
    items.push({
      kind: "info",
      text:
        "Reconnect link sent as a task reminder. Open it and sign in with Microsoft before it " +
        "expires; the host restarts by itself once the sign-in lands.",
    });
  } else if (status.state === "failed") {
    items.push({
      kind: "error",
      text:
        status.reason === "restart"
          ? "The sign-in worked, but ae-omnigent-host.service did not restart. The button " +
            "signs in again and retries the restart."
          : "No sign-in arrived. The button sends a fresh link.",
    });
  }
  if (status.message) items.push({ kind: "info", text: status.message });
  if (status.request_untaken) {
    items.push({
      kind: "error",
      text:
        "The box has not picked the request up. Check that ae-omnigent-host-relink.service " +
        "is running on the box.",
    });
  }
  if (status.error) items.push({ kind: "info", text: `Partly unknown: ${status.error}` });
  return items;
}

/** The button's label, or null when the card shows none (connected, or nothing to ask). */
export function buttonLabel(status: HostStatus): string | null {
  if (status.requested) return "Asking the box...";
  return status.can_request ? ASK_LABEL : null;
}

/** The Box host tile: a dot and the state, the host's name under it. */
export function hostTile(status: HostStatus | null, error: unknown): Tile {
  if (!status) {
    return {
      id: "host",
      value: error ? "Unknown" : "Reading...",
      note: error ? "could not be read" : "",
      health: error ? "attention" : "idle",
      dot: true,
    };
  }
  const tone = toneOf(status.state);
  const health = tone === "on" ? "ok" : tone === "busy" ? "attention" : "problem";
  return {
    id: "host",
    value: stateText(status.state),
    summary: `Host ${stateText(status.state).toLowerCase()}`,
    note: status.host?.name || "Box host",
    health: status.state === "unknown" ? "idle" : health,
    dot: true,
  };
}

/** The wait before the next read; a link counting down redraws every second. */
export function nextDelay(status: HostStatus | null): number {
  if (status?.state === "sent") return 1000;
  return status?.state === "connected" ? POLL_CONNECTED_MS : POLL_DISCONNECTED_MS;
}

// ── the section ─────────────────────────────────────────────────────────────

interface SectionState {
  status: HostStatus | null;
  /** When the status arrived, for counting a link down. */
  at: number;
  error: unknown;
  asking: boolean;
  /** When the latest ask started or ended (see `clock`). */
  lastAction: number;
  /** A counter, not a time: orders reads against asks without clock skew. */
  clock: number;
}

interface Section {
  context: ExtensionContext;
  state: SectionState;
  list: HTMLElement;
  status: HTMLElement;
  refreshed: HTMLElement;
  live: () => boolean;
  render: () => void;
  refresh: () => Promise<void>;
  /** A read that begins after any read already out (which began before the latest ask). */
  readAll: () => Promise<void>;
  schedule: () => void;
}

function card(section: Section, status: HostStatus, now: number): HTMLElement {
  const { state } = section;
  const name = status.host?.name || "Box host";
  const meta = metaItems(status, state.at, now);
  const label = buttonLabel(status);
  const acts: HTMLElement[] = [];
  if (label) {
    const node = button(state.asking ? "Asking the box..." : label, {
      title: "Send a sign-in link to your phone as a task reminder",
      disabled: state.asking || status.requested,
      onClick: () => ask(section),
    });
    node.classList.add("btn-tint");
    acts.push(node);
  }
  const node = el(
    "article",
    { class: `card card-fabric fabric-${toneOf(status.state)}`, "data-host": name },
    el(
      "div",
      { class: meta.length ? "fabric-row" : "fabric-row fabric-row-solo" },
      el("span", { class: "fabric-dot", "aria-hidden": "true" }),
      el(
        "div",
        { class: "fabric-title" },
        el("span", { class: "fabric-name" }, name),
        el("span", { class: "fabric-state" }, stateText(status.state)),
      ),
      el("div", { class: "fabric-acts" }, ...acts),
      meta.length
        ? el(
            "p",
            { class: "fabric-meta" },
            ...meta.map((item) => el("span", { class: "fabric-meta-item" }, item)),
          )
        : null,
    ),
  );
  for (const item of noticeTexts(status)) node.appendChild(notice(item.text, item.kind));
  return node;
}

function render(section: Section): void {
  const { state } = section;
  const now = Date.now();
  if (!state.status) {
    replaceChildren(section.list, [
      state.error
        ? notice(`The host status could not be read (${describeError(state.error)}).`, "error")
        : el("p", { class: "meta" }, "Reading..."),
    ]);
  } else {
    replaceChildren(section.list, [
      card(section, state.status, now),
      state.error
        ? notice(`The host status could not be read (${describeError(state.error)}).`, "error")
        : null,
    ]);
  }
  section.refreshed.textContent = state.at
    ? `Updated ${new Date(state.at).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })}`
    : "";
  const tile = hostTile(state.status, state.error);
  setTile(state.status ? staleTile(tile, state.error) : tile, state.at);
}

/** The button: ask the server, show its answer, say what happens next. */
async function ask(section: Section): Promise<void> {
  const { state, context } = section;
  if (state.asking) return;
  state.asking = true;
  state.lastAction = ++state.clock;
  replaceChildren(section.status, []);
  section.render();
  let outcome: HTMLElement;
  try {
    const status = await requestHostRelink(context);
    if (!section.live()) return;
    state.status = status;
    state.at = Date.now();
    state.error = null;
    outcome = notice(
      "Asked the box for a reconnect link. It arrives as a task reminder within a minute.",
      "ok",
    );
  } catch (error) {
    if (!section.live()) return;
    const connected = error instanceof AeRequestError && error.status === 409;
    outcome = connected
      ? notice("The host is connected again, so it needs no link.", "ok")
      : notice(`Could not ask for a reconnect link: ${describeError(error)}`, "error");
  }
  state.asking = false;
  state.lastAction = ++state.clock;
  section.render();
  replaceChildren(section.status, [outcome]);
  // The answer, or a 409 after a reconnect, is worth reading again at once,
  // with a read that begins now: one already out began before the ask.
  await section.readAll();
}

/**
 * The host's block, for activateFabric to put first in the Box health card
 * (it moves the "Updated" time into that card's header); activateHost fills it.
 */
export function hostSection(): HTMLElement {
  return el(
    "div",
    { class: "admin-block admin-host" },
    el("p", { class: "meta refreshed host-refreshed" }),
    el("section", { class: "cards fabric-list host-list", "aria-label": "Box host" }),
    el("div", { class: "page-status host-status", "aria-live": "polite" }),
  );
}

let timer: ReturnType<typeof setTimeout> | null = null;
let onFocus: (() => void) | null = null;
/** Bumped by every activate and deactivate, as in deploys.ts. */
let generation = 0;

/** Read and draw the section *root* (from hostSection, or the card holding it) and keep it fresh. */
export function activateHost(context: ExtensionContext, root: HTMLElement): void {
  const list = root.querySelector<HTMLElement>(".host-list");
  const status = root.querySelector<HTMLElement>(".host-status");
  const refreshed = root.querySelector<HTMLElement>(".host-refreshed");
  if (!list || !status || !refreshed) throw new Error("Box host section is missing");
  const mine = ++generation;
  const live = () => generation === mine;
  if (onFocus) {
    window.removeEventListener("focus", onFocus);
    document.removeEventListener("visibilitychange", onFocus);
  }
  const state: SectionState = {
    status: null,
    at: 0,
    error: null,
    asking: false,
    lastAction: 0,
    clock: 0,
  };
  let reading: Promise<void> | null = null;
  let lastRead = 0;
  const section: Section = {
    context,
    state,
    list,
    status,
    refreshed,
    live,
    render: () => {
      if (live()) render(section);
    },
    refresh: () => {
      if (reading) return reading;
      lastRead = Date.now();
      const begin = ++state.clock;
      reading = fetchHostStatus(context)
        .then(
          (answer) => {
            // A read that began before an ask, or answers during one, would put the old card back.
            if (!live() || state.asking || state.lastAction > begin) return;
            state.status = answer;
            state.at = Date.now();
            state.error = null;
          },
          (error: unknown) => {
            // The last good card stays, with the failure said under it.
            if (live()) state.error = error ?? new Error("Request failed");
          },
        )
        .then(() => {
          section.render();
          section.schedule();
        })
        .finally(() => {
          reading = null;
        });
      return reading;
    },
    readAll: () => {
      const again = (): Promise<void> => (reading ? reading.then(again) : section.refresh());
      return again();
    },
    schedule: () => {
      if (!live()) return;
      if (timer) clearTimeout(timer);
      const delay = nextDelay(state.status);
      timer = setTimeout(() => {
        if (!live() || document.visibilityState !== "visible") return;
        // A countdown redraws every second but reads only every 15 s.
        if (Date.now() - lastRead < POLL_DISCONNECTED_MS && state.status?.state === "sent") {
          section.render();
          section.schedule();
          return;
        }
        void section.refresh();
      }, delay);
    },
  };
  section.render();
  void section.refresh();
  registerRefresh("host", () => (live() ? section.refresh() : undefined));
  onFocus = () => {
    if (document.visibilityState === "visible") void section.refresh();
  };
  window.addEventListener("focus", onFocus);
  document.addEventListener("visibilitychange", onFocus);
}

export function deactivateHost(): void {
  generation++;
  if (timer) clearTimeout(timer);
  timer = null;
  if (onFocus) {
    window.removeEventListener("focus", onFocus);
    document.removeEventListener("visibilitychange", onFocus);
  }
  onFocus = null;
}
