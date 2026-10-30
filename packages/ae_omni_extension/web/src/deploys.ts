// The Admin Center's Deploys section (2026-10-19): the release the box runs,
// whether a newer one is waiting, and a hold that pauses the box's automatic
// deploys for 1, 2 or 3 hours.
//
// Why: a deploy restarts the server, and the owner kept stopping the auto-update
// timer by hand before a demo or a long run, then had to remember to start it
// again. A hold set here ends on its own. The server keeps it
// (`/v1/ae/deploy/hold`, ae_omni_policies/deploy_hold.py) and
// deploy/auto-update.sh reads it before each pass; a hold that cannot be read
// never blocks a release, and the page says so.
//
// The section is first under the page's heading, above Fabric capacity, so
// the phone shows it without a scroll. One card: the release line (from the
// header's `GET /v1/ae/release`), the hold line, then a button per choice the
// server offers ("Hold 1 h", "Hold 2 h", "Hold 3 h"; each sets the hold to
// that many hours from now, replacing any current one) and Resume while held.
// There is no confirm step: a hold is harmless and ends on its own. While a
// call is out every button is off and the pressed one says so; the outcome
// goes to the section's own status line. Resume is a POST, not a DELETE: the
// page's SDK sends only GET, POST and PATCH.
//
// Polling: both reads on activate, every 60 s (sooner when a hold is about to
// end) and on focus, as databricks.ts does; a scheduled read is skipped while
// the tab is hidden. After an action the card shows the hold the action
// answered and the release is read again. A hold read that began before an
// action cannot put the old hold back.
//
// 2026-10-20, the dashboard redesign: the section is the page's Deploys card
// (board.ts), first in the first column. A ring counts the hold's time down
// (the hold's own length when the server says when it was set, else the
// longest choice), beside the release line ("ae-v0.18.0.22 -> ae-v0.18.0.23
// queued", with the arrow drawn), "Held until 6:09 PM by pat.lee", the
// hold choices as one segmented control (1 h, 2 h, 3 h) and Resume. The card
// header carries an "On hold" pill. Calls, polling and the status line are
// unchanged. It also hands the page its Release and Deploy hold tiles
// (tiles.ts) after every draw, and its reads to the header's refresh button.

import type { ExtensionContext } from "@omnigent/extension-sdk";
import { type DeployHold, describeError, fetchDeployHold, holdDeploys, resumeDeploys } from "./api";
import { adminCard, ring } from "./board";
import { whenText } from "./databricks";
import { button, el, notice, replaceChildren } from "./dom";
import { fetchRelease, type Release } from "./header";
import { pauseIcon, rocketIcon, startIcon } from "./icons";
import { registerRefresh, setTile, shortLeft, type Tile } from "./tiles";

export const POLL_MS = 60_000;
/** The hold choices until the server's first answer names its own. */
export const DEFAULT_CHOICES: readonly number[] = [1, 2, 3];

type Pending = { kind: "hold"; hours: number } | { kind: "resume" };

/** One line of the card; a muted one is context, not news. */
export interface Line {
  text: string;
  muted: boolean;
  /** The release line: what runs and what is queued, drawn in mono with an arrow. */
  release?: { running: string; queued: string | null };
  /** The hold line's holder: *lead* is the text before the name, *full* the whole address for a tooltip. */
  by?: { lead: string; name: string; full: string };
}

/** "pat.lee" for "pat.lee@example.com"; a value with no "@" stays whole. */
export function holderName(setBy: string): string {
  const at = setBy.indexOf("@");
  return at > 0 ? setBy.slice(0, at) : setBy;
}

// ── words ───────────────────────────────────────────────────────────────────

/**
 * "ae-v0.18.0.6 is waiting to deploy for 2 busy sessions", or null when nothing
 * waits. While a hold is on, the hold is what it waits for, not the sessions.
 */
export function waitingText(release: Release, held = false): string | null {
  if (release.state !== "waiting") return null;
  const what = release.latest ?? "A newer release";
  if (held) return `${what} is waiting for the hold to end`;
  const busy = release.mid_turn;
  const why = busy > 0 ? ` for ${busy} busy ${busy === 1 ? "session" : "sessions"}` : "";
  return `${what} is waiting to deploy${why}`;
}

/** The muted line when the newest release cannot be told, with the reason when there is one. */
export function unknownText(reason: string | null): string {
  return reason ? `The newest release is unknown (${reason}).` : "The newest release is unknown.";
}

/**
 * "The server lacks 1 setting config/server.env.example sets: AE_X.", or null
 * when none is missing (2026-10-20: a missed AE_LIMITS_STATE_FILE blanked the
 * Usage card after every deploy, and nothing said so).
 */
export function envText(missing: string[] | null | undefined): string | null {
  if (!missing?.length) return null;
  const what = missing.length === 1 ? "setting" : "settings";
  return `The server lacks ${missing.length} ${what} config/server.env.example sets: ${missing.join(", ")}.`;
}

/** Why a queued release waits, or null when nothing waits or nothing holds it back. */
export function waitReason(release: Release, held = false): string | null {
  if (release.state !== "waiting") return null;
  if (held) return "Waiting for the hold to end";
  const busy = release.mid_turn;
  return busy > 0 ? `Waiting for ${busy} busy ${busy === 1 ? "session" : "sessions"}` : null;
}

/**
 * The release lines: "ae-v0.18.0.5 -> ae-v0.18.0.6 queued" (or "Running
 * ae-v0.18.0.5" when nothing waits), why it waits, why the newest is unknown,
 * and env drift.
 */
export function releaseLines(release: Release | null, error: unknown, held = false): Line[] {
  if (error) return [{ text: unknownText(describeError(error)), muted: true }];
  if (!release) return [{ text: "Reading...", muted: true }];
  const lines: Line[] = [];
  const queued = release.state === "waiting" ? release.latest : null;
  if (release.running) {
    lines.push({
      text: queued ? `${release.running} \u2192 ${queued} queued` : `Running ${release.running}`,
      muted: false,
      release: { running: release.running, queued },
    });
  } else {
    const waiting = waitingText(release, held);
    if (waiting) lines.push({ text: waiting, muted: false });
  }
  const reason = release.running ? waitReason(release, held) : null;
  if (reason) lines.push({ text: reason, muted: true });
  if (release.error || release.state === "unknown") {
    lines.push({ text: unknownText(release.error), muted: true });
  }
  const drift = envText(release.env_missing);
  if (drift) lines.push({ text: drift, muted: false });
  return lines;
}

/** "1 h 12 min left", rounded up to the minute. */
export function leftText(seconds: number): string {
  const minutes = Math.max(1, Math.ceil(seconds / 60));
  const hours = Math.floor(minutes / 60);
  const rest = minutes % 60;
  if (hours === 0) return `${rest} min left`;
  return rest === 0 ? `${hours} h left` : `${hours} h ${rest} min left`;
}

/** What the page says when no hold is on. */
export function idleText(choices: readonly number[]): string {
  const most = Math.max(...choices);
  return (
    "Deploys run automatically. " +
    `A hold pauses them for up to ${most} ${most === 1 ? "hour" : "hours"} and ends on its own.`
  );
}

/** Seconds left on *hold*, counted down from when its answer arrived (`at`). */
export function secondsLeft(hold: DeployHold, at: number, now: number): number {
  return Math.max(0, hold.remaining_s - (now - at) / 1000);
}

/**
 * The hold lines. *error* is a failed read; *hold.error* is a hold the server
 * could not read, which never blocks a deploy.
 */
export function holdLines(
  hold: DeployHold | null,
  error: unknown,
  now: number,
): Line[] {
  // A failed read leaves the last good hold below it, so Resume stays while that hold is on.
  const failed: Line[] = error
    ? [{ text: `The hold could not be read (${describeError(error)}).`, muted: true }]
    : [];
  if (!hold) return failed;
  if (hold.error) {
    const text = `The hold could not be read (${hold.error}), so deploys go ahead.`;
    return [...failed, { text, muted: true }];
  }
  if (!hold.held) return [...failed, { text: idleText(choicesOf(hold)), muted: true }];
  const until = hold.until ? Date.parse(hold.until) : Number.NaN;
  const lead = Number.isNaN(until) ? "Held" : `Held until ${whenText(until, now)}`;
  // The time left is the ring's (holdRing).
  if (!hold.set_by) return [...failed, { text: lead, muted: false }];
  const name = holderName(hold.set_by);
  return [
    ...failed,
    {
      text: `${lead} by ${name}`,
      muted: false,
      by: { lead: `${lead} by `, name, full: hold.set_by },
    },
  ];
}

/** How much of the hold is left, 0 to 1: of its own length when known, else of the longest choice. */
export function holdFraction(hold: DeployHold, at: number, now: number): number {
  const set = hold.set_at ? Date.parse(hold.set_at) : Number.NaN;
  const until = hold.until ? Date.parse(hold.until) : Number.NaN;
  const whole =
    !Number.isNaN(set) && !Number.isNaN(until) && until > set
      ? (until - set) / 1000
      : Math.max(...choicesOf(hold)) * 3600;
  return Math.min(1, secondsLeft(hold, at, now) / whole);
}

/** "1:50" for 1 h 50 min left, rounded up to the minute. */
export function clockLeft(seconds: number): string {
  const minutes = Math.max(1, Math.ceil(seconds / 60));
  return `${Math.floor(minutes / 60)}:${String(minutes % 60).padStart(2, "0")}`;
}

/** The Release tile: what runs, and a newer release waiting. */
export function releaseTile(release: Release | null, error: unknown, held = false): Tile {
  if (error || !release || !release.running) {
    return {
      id: "release",
      value: release?.running ?? "Unknown",
      note: error ? "could not be read" : release ? "newest unknown" : "Reading...",
      health: error ? "attention" : "idle",
    };
  }
  const running = release.running.replace(/^ae-/, "");
  if (release.state === "waiting") {
    const latest = release.latest?.replace(/^ae-/, "") ?? "a newer release";
    return {
      id: "release",
      value: running,
      note: `${latest} waiting${held ? " for the hold" : ""}`,
      health: "attention",
    };
  }
  if (envText(release.env_missing)) {
    return { id: "release", value: running, note: "server lacks a setting", health: "attention" };
  }
  const note = release.state === "unknown" ? "newest unknown" : "up to date";
  return { id: "release", value: running, note, health: "ok" };
}

/** The Deploy hold tile: the time left and a bar, or Off. */
export function holdTile(hold: DeployHold | null, error: unknown, at: number, now: number): Tile {
  if (!hold || hold.error) {
    return {
      id: "hold",
      value: hold || error ? "Unknown" : "Reading...",
      note: hold?.error || error ? "deploys go ahead" : "",
      health: hold?.error || error ? "attention" : "idle",
    };
  }
  if (!hold.held) return { id: "hold", value: "Off", note: "deploys run automatically", health: "idle" };
  const left = secondsLeft(hold, at, now);
  return {
    id: "hold",
    value: shortLeft(left),
    note: hold.set_by ? `held by ${holderName(hold.set_by)}` : "on hold",
    health: "attention",
    title: hold.set_by && holderName(hold.set_by) !== hold.set_by ? `Held by ${hold.set_by}` : undefined,
    bar: Math.round(holdFraction(hold, at, now) * 100),
  };
}

function choicesOf(hold: DeployHold | null): readonly number[] {
  return hold?.choices?.length ? hold.choices : DEFAULT_CHOICES;
}

/** The status line after an action answered. */
export function doneText(pending: Pending, hold: DeployHold, now: number): string {
  if (pending.kind === "resume") return "Deploys resumed. They run automatically again.";
  const until = hold.until ? Date.parse(hold.until) : Number.NaN;
  if (!hold.held || Number.isNaN(until)) return `Deploys are on hold for ${pending.hours} h.`;
  return `Deploys are on hold until ${whenText(until, now)}.`;
}

/** The status line after an action failed. */
export function failedText(pending: Pending, error: unknown): string {
  const what = pending.kind === "hold" ? "hold deploys" : "resume deploys";
  return `Could not ${what}: ${describeError(error)}`;
}

/** The wait before the next read: 60 s, or just past the end of a hold that ends sooner. */
export function nextDelay(hold: DeployHold | null, at: number, now: number): number {
  if (!hold?.held) return POLL_MS;
  const ending = secondsLeft(hold, at, now) * 1000 + 1000;
  return Math.max(1000, Math.min(POLL_MS, ending));
}

// ── the section ─────────────────────────────────────────────────────────────

interface SectionState {
  release: Release | null;
  releaseError: unknown;
  hold: DeployHold | null;
  /** When the hold's answer arrived, for counting its time down. */
  holdAt: number;
  holdError: unknown;
  /** The action whose call is out, if any. */
  pending: Pending | null;
  /** The button to put the focus back on once the call answers. */
  focusKey: string | null;
  /**
   * The hold (held or not, and its `until`) the status notice describes, or
   * null when it describes none; a later read that differs in either clears it.
   */
  notice: { held: boolean; until: string | null } | null;
  /** When the latest action started or ended (see `clock`). */
  lastAction: number;
  /** A counter, not a time: orders reads against actions without clock skew. */
  clock: number;
}

interface Section {
  context: ExtensionContext;
  state: SectionState;
  root: HTMLElement;
  release: HTMLElement;
  hold: HTMLElement;
  acts: HTMLElement;
  status: HTMLElement;
  /** The ring's place, empty while no hold is on. */
  ring: HTMLElement;
  /** The header's "On hold" pill. */
  pill: HTMLElement;
  live: () => boolean;
  render: () => void;
  readRelease: () => Promise<void>;
  /** Both reads, after any read already out (which began before the latest action). */
  readAll: () => Promise<void>;
  schedule: () => void;
}

function keyOf(pending: Pending): string {
  return pending.kind === "hold" ? `deploy:hold:${pending.hours}` : "deploy:resume";
}

function lineNodes(lines: Line[]): HTMLElement[] {
  return lines.map((line) => {
    if (line.by) {
      // The name never breaks mid-word; the full address is its tooltip.
      return el(
        "p",
        { class: line.muted ? "deploy-line deploy-line-muted" : "deploy-line" },
        line.by.lead,
        el("span", { class: "deploy-holder", title: line.by.full }, line.by.name),
      );
    }
    if (!line.release) {
      return el(
        "p",
        { class: line.muted ? "deploy-line deploy-line-muted" : "deploy-line" },
        line.text,
      );
    }
    // The same words, drawn: the versions in mono, the arrow, "queued" muted.
    const { running, queued } = line.release;
    const node = el(
      "p",
      { class: "deploy-line deploy-ver", "aria-label": line.text },
      queued ? el("code", {}, running) : `Running ${running}`,
    );
    if (queued) {
      node.append(
        el("span", { class: "deploy-arrow", "aria-hidden": "true" }, " \u2192 "),
        el("code", { class: "deploy-queued" }, queued),
        el("span", { class: "deploy-dim" }, " queued"),
      );
    }
    return node;
  });
}

function actionButton(
  section: Section,
  pending: Pending,
  label: string,
  busyLabel: string,
  icon: SVGElement,
  title: string,
): HTMLButtonElement {
  const { state } = section;
  const pressed = state.pending !== null && keyOf(state.pending) === keyOf(pending);
  const node = button(pressed ? busyLabel : label, {
    title,
    disabled: state.pending !== null,
    onClick: () => act(section, pending),
  });
  node.classList.add("deploy-btn");
  node.dataset.focusKey = keyOf(pending);
  if (pending.kind === "hold") {
    // One segment of the hold control: "1 h", named in full for a screen reader.
    node.classList.add("deploy-seg-btn");
    node.setAttribute("aria-label", pressed ? busyLabel : `Hold ${pending.hours} h`);
    if (!pressed) node.textContent = `${pending.hours} h`;
  }
  node.prepend(icon);
  return node;
}

function actionButtons(section: Section): HTMLElement[] {
  const { hold } = section.state;
  const segments = choicesOf(hold).map((hours) =>
    actionButton(
      section,
      { kind: "hold", hours },
      `Hold ${hours} h`,
      "Holding...",
      pauseIcon(),
      `Pause deploys for ${hours} h from now`,
    ),
  );
  // Only the first segment carries the pause icon: it labels the control.
  for (const node of segments.slice(1)) node.querySelector("svg")?.remove();
  const nodes: HTMLElement[] = [
    el("div", { class: "deploy-seg", role: "group", "aria-label": "Hold deploys for" }, ...segments),
  ];
  if (hold?.held) {
    nodes.push(
      actionButton(
        section,
        { kind: "resume" },
        "Resume",
        "Resuming...",
        startIcon(),
        "End the hold so deploys run again",
      ),
    );
  }
  return nodes;
}

function render(section: Section): void {
  const { state } = section;
  const now = Date.now();
  const active = document.activeElement as HTMLElement | null;
  const focusKey =
    state.focusKey ?? (section.root.contains(active) ? active?.dataset.focusKey : undefined);
  replaceChildren(
    section.release,
    lineNodes(releaseLines(state.release, state.releaseError, state.hold?.held === true)),
  );
  replaceChildren(
    section.hold,
    lineNodes(holdLines(state.hold, state.holdError, now)),
  );
  replaceChildren(section.acts, actionButtons(section));
  const held = state.hold?.held === true && !state.hold.error ? state.hold : null;
  replaceChildren(section.ring, [
    held
      ? ring(
          holdFraction(held, state.holdAt, now),
          clockLeft(secondsLeft(held, state.holdAt, now)),
          "left",
          "ring-warn",
          leftText(secondsLeft(held, state.holdAt, now)),
        )
      : null,
  ]);
  section.pill.hidden = !held;
  setTile(releaseTile(state.release, state.releaseError, held !== null));
  setTile(holdTile(state.hold, state.holdError, state.holdAt, now), state.holdAt);
  // Only a focus the redraw dropped (body, nothing, or the removed button) is put back;
  // a control the user moved to meanwhile keeps it.
  const here = document.activeElement;
  const dropped = !here || here === document.body || !here.isConnected;
  if (focusKey && !state.pending && dropped) {
    const keyed = [...section.acts.querySelectorAll<HTMLElement>("[data-focus-key]")];
    // Resume is gone after a resume: the first Hold button takes the focus.
    const next =
      keyed.find((node) => node.dataset.focusKey === focusKey) ??
      keyed.find((node) => node.dataset.focusKey?.startsWith("deploy:hold:"));
    next?.focus({ preventScroll: true });
  }
}

/** Hold or resume, then show the hold the server answered and read the release again. */
async function act(section: Section, pending: Pending): Promise<void> {
  const { state, context } = section;
  if (state.pending) return;
  state.pending = pending;
  state.focusKey = keyOf(pending);
  state.notice = null;
  state.lastAction = ++state.clock;
  replaceChildren(section.status, []);
  section.render();
  let outcome: HTMLElement;
  let failedAction = false;
  try {
    const hold =
      pending.kind === "hold"
        ? await holdDeploys(context, pending.hours)
        : await resumeDeploys(context);
    // A late answer after the page closed touches nothing.
    if (!section.live()) return;
    state.hold = hold;
    state.holdAt = Date.now();
    state.holdError = null;
    outcome = notice(doneText(pending, hold, state.holdAt), "ok");
    state.notice = { held: hold.held, until: hold.until };
  } catch (error) {
    if (!section.live()) return;
    outcome = notice(failedText(pending, error), "error");
    failedAction = true;
    // A read that shows another hold (the server applied it after all) clears this.
    state.notice = state.hold ? { held: state.hold.held, until: state.hold.until } : null;
  }
  state.pending = null;
  state.lastAction = ++state.clock;
  section.render();
  state.focusKey = null;
  replaceChildren(section.status, [outcome]);
  section.schedule();
  // A failed call may still have been applied, so read the hold too.
  await (failedAction ? section.readAll() : section.readRelease());
}

/**
 * The Deploys card (board.ts), for activateFabric to put first in the first
 * column; activateDeploys fills it.
 */
export function deploysSection(): HTMLElement {
  return adminCard(
    "deploys",
    "Deploys",
    rocketIcon(),
    [el("span", { class: "admin-pill admin-pill-warn deploy-pill", hidden: true }, "On hold")],
    el(
      "div",
      { class: "card card-fabric deploy-card" },
      el("div", { class: "deploy-ring" }),
      el(
        "div",
        { class: "deploy-info" },
        el("div", { class: "deploy-release" }),
        el("div", { class: "deploy-hold" }),
        el("div", { class: "deploy-acts", role: "group", "aria-label": "Deploy hold" }),
      ),
    ),
    el("div", { class: "page-status deploy-status", "aria-live": "polite" }),
  );
}

let timer: ReturnType<typeof setTimeout> | null = null;
let onFocus: (() => void) | null = null;
/**
 * Bumped by every activate and deactivate, as in databricks.ts: a read or an
 * action still out from an earlier activation finds its number stale, so it
 * neither draws into a detached section nor touches the timer.
 */
let generation = 0;

/** Read and draw the section *root* (from deploysSection) and keep it fresh. */
export function activateDeploys(context: ExtensionContext, root: HTMLElement): void {
  const release = root.querySelector<HTMLElement>(".deploy-release");
  const hold = root.querySelector<HTMLElement>(".deploy-hold");
  const acts = root.querySelector<HTMLElement>(".deploy-acts");
  const status = root.querySelector<HTMLElement>(".deploy-status");
  const ringPlace = root.querySelector<HTMLElement>(".deploy-ring");
  const pill = root.querySelector<HTMLElement>(".deploy-pill");
  if (!release || !hold || !acts || !status || !ringPlace || !pill) {
    throw new Error("Deploys section is missing");
  }
  const mine = ++generation;
  const live = () => generation === mine;
  if (onFocus) {
    window.removeEventListener("focus", onFocus);
    document.removeEventListener("visibilitychange", onFocus);
  }
  const state: SectionState = {
    release: null,
    releaseError: null,
    hold: null,
    holdAt: 0,
    holdError: null,
    pending: null,
    focusKey: null,
    notice: null,
    lastAction: 0,
    clock: 0,
  };
  const takeRelease = (result: PromiseSettledResult<Release>) => {
    if (result.status === "fulfilled") {
      state.release = result.value;
      state.releaseError = null;
    } else {
      state.releaseError = result.reason ?? new Error("Request failed");
    }
  };
  let reading: Promise<void> | null = null;
  const refresh = (): Promise<void> => {
    if (reading) return reading;
    const begin = ++state.clock;
    reading = Promise.allSettled([fetchRelease(context), fetchDeployHold(context)])
      .then(([releaseResult, holdResult]) => {
        if (!live()) return;
        takeRelease(releaseResult);
        // A read that began before an action, or answers during one, would put the old hold back.
        if (!state.pending && state.lastAction <= begin) {
          if (holdResult.status === "fulfilled") {
            state.hold = holdResult.value;
            state.holdAt = Date.now();
            state.holdError = null;
            // The notice describes a state that no longer holds.
            const { notice } = state;
            if (notice && (notice.held !== state.hold.held || notice.until !== state.hold.until)) {
              state.notice = null;
              replaceChildren(status, []);
            }
          } else {
            state.holdError = holdResult.reason ?? new Error("Request failed");
          }
        }
        section.render();
        section.schedule();
      })
      .finally(() => {
        reading = null;
      });
    return reading;
  };
  const section: Section = {
    context,
    state,
    root,
    release,
    hold,
    acts,
    status,
    ring: ringPlace,
    pill,
    live,
    render: () => {
      if (live()) render(section);
    },
    readRelease: async () => {
      const [result] = await Promise.allSettled([fetchRelease(context)]);
      if (!live()) return;
      takeRelease(result);
      render(section);
    },
    readAll: () => {
      const again = (): Promise<void> => (reading ? reading.then(again) : refresh());
      return again();
    },
    schedule: () => {
      if (!live()) return;
      if (timer) clearTimeout(timer);
      timer = setTimeout(
        () => {
          // A hidden tab does not poll; visibilitychange reads on return.
          if (live() && document.visibilityState === "visible") void refresh();
        },
        nextDelay(state.hold, state.holdAt, Date.now()),
      );
    },
  };
  section.render();
  void refresh();
  registerRefresh("deploys", () => (live() ? refresh() : undefined));
  onFocus = () => {
    if (document.visibilityState === "visible") void refresh();
  };
  window.addEventListener("focus", onFocus);
  document.addEventListener("visibilitychange", onFocus);
}

export function deactivateDeploys(): void {
  generation++;
  if (timer) clearTimeout(timer);
  timer = null;
  if (onFocus) {
    window.removeEventListener("focus", onFocus);
    document.removeEventListener("visibilitychange", onFocus);
  }
  onFocus = null;
}
