// The Admin Center's Home security card (2026-11-08): one line on the home network's health,
// the open alert count, the last scan, the firewall's state, and two links out.
//
// Why: the owner's home app shows his network's health and will soon show a firewall's status.
// He wanted that status beside the other cards, with a link to the home app's Security tab when
// something needs a person. The card reads `GET /v1/ae/home-security`
// (ae_omni_policies/home_security.py), which carries a few labels and one count: never an
// alert's text, a device name or an address. The server reads the home app; it never talks to
// the firewall.
//
// States, worst first: unreachable (the home app did not answer), Firewalla down, alerts, scan
// stale, OK. A stale summary says nothing current about the firewall, so a stale answer never
// reads "Firewalla down" (the row says "old"), and alerts on a stale scan read "(old scan)". A server with no home app address answers off, which api.ts turns into an
// admin_only error; that is the "Not set up" tile, and the card leaves the board.
//
// Polling: every 60 s and on focus; a scheduled read is skipped while the tab is hidden, and
// deactivate stops it. After an admin_only answer it looks again every ADMIN_RECHECK_MS only.

import type { ExtensionContext } from "@omnigent/extension-sdk";
import {
  ADMIN_RECHECK_MS,
  describeError,
  fetchHomeSecurity,
  type HomeSecurityStatus,
  isAdminOnly,
} from "./api";
import { el, replaceChildren } from "./dom";
import { hint } from "./hint";
import { type Health, registerRefresh, setTile, staleTile, type Tile } from "./tiles";

export const POLL_MS = 60_000;
export const SETTINGS_HINT =
  "AE_HOME_SECURITY_URL is where the home app answers. AE_HOME_SECURITY_LINK is the page people open for its Security tab.";

export type StateKind = "unreachable" | "firewall" | "stale" | "alerts" | "ok";

/** A link only when a browser and the server read it the same way (2026-11-08, review). */
function parsed(raw: unknown): URL | null {
  if (typeof raw !== "string" || !raw || raw.length > 500 || raw.includes("\\")) return null;
  for (const ch of raw) {
    const code = ch.charCodeAt(0);
    if (code <= 0x20 || code === 0x7f) return null;
  }
  try {
    const url = new URL(raw);
    return url.username || url.password || !url.hostname ? null : url;
  } catch {
    return null;
  }
}

/** An http or https link with no userinfo, as the browser reads it, else null. */
export function safeLink(raw: unknown): string | null {
  const url = parsed(raw);
  return url && (url.protocol === "https:" || url.protocol === "http:") ? url.href : null;
}

/** An https link to firewalla.net or a subdomain, as the browser reads it, else null. */
export function safePortal(raw: unknown): string | null {
  const url = parsed(raw);
  if (!url || url.protocol !== "https:") return null;
  const host = url.hostname.toLowerCase();
  return host === "firewalla.net" || host.endsWith(".firewalla.net") ? url.href : null;
}

/** The card's state from the server's answer; the worst thing wins. */
export function stateOf(data: HomeSecurityStatus): StateKind {
  if (!data.reachable) return "unreachable";
  const fw = data.firewalla;
  // A stale summary is not news about the firewall.
  if (fw && !data.stale && (fw.api === "down" || fw.boxOnline === false)) return "firewall";
  if (data.alerts > 0) return "alerts";
  return data.stale ? "stale" : "ok";
}

/** The folded line: "Home OK", "Home: 2 alerts", "Firewalla down", "Home unreachable". */
export function tileText(data: HomeSecurityStatus): string {
  switch (stateOf(data)) {
    case "unreachable":
      return "Home unreachable";
    case "firewall":
      return "Firewalla down";
    case "stale":
      return "Home: scan stale";
    case "alerts":
      return `Home: ${data.alerts} ${data.alerts === 1 ? "alert" : "alerts"}${data.stale ? " (old scan)" : ""}`;
    default:
      return "Home OK";
  }
}

/** "5 min ago", "3 h ago", "2 d ago"; an empty string for a missing or unreadable time. */
export function agoText(iso: string | null | undefined, now = Date.now()): string {
  const at = iso ? Date.parse(iso) : Number.NaN;
  if (Number.isNaN(at)) return "";
  const minutes = Math.floor(Math.max(0, now - at) / 60_000);
  if (minutes < 1) return "just now";
  if (minutes < 60) return `${minutes} min ago`;
  const hours = Math.floor(minutes / 60);
  return hours < 48 ? `${hours} h ago` : `${Math.floor(hours / 24)} d ago`;
}

/** The firewall row's words. */
export function firewallText(data: HomeSecurityStatus): string {
  const fw = data.firewalla;
  if (!fw) return "Not reported";
  let text = "Unknown";
  if (fw.api === "down") text = "Cannot be reached";
  else if (fw.boxOnline === false) text = "Box offline";
  else if (fw.api === "up") text = fw.boxOnline === true ? "Up, box online" : "Up";
  // From a stale summary: say it is old rather than current.
  return data.stale && text !== "Unknown" ? `${text} (old)` : text;
}

export function homeSecurityTile(data: HomeSecurityStatus | null, error: unknown): Tile {
  if (!data) {
    // An off card or a non-admin answers admin_only: nothing to show, so the card leaves the board.
    if (error && isAdminOnly(error)) {
      return { id: "homesecurity", value: "Not set up", note: "", health: "idle", notSetUp: true };
    }
    return {
      id: "homesecurity",
      value: error ? "Unknown" : "Reading...",
      note: error ? "could not be read" : "",
      health: error ? "attention" : "idle",
    };
  }
  const kind = stateOf(data);
  const health: Health = kind === "ok" ? "ok" : kind === "unreachable" || kind === "firewall" ? "problem" : "attention";
  const text = tileText(data);
  const scan = agoText(data.lastScan);
  return {
    id: "homesecurity",
    value: text,
    summary: text,
    brief: text,
    note: scan ? `scan ${scan}` : "",
    health,
    dot: true,
  };
}

// ── the card ────────────────────────────────────────────────────────────────

/** The state pill for the card header; activateHomeSecurity fills it. */
export function homeSecurityPill(): HTMLElement {
  return el("span", { class: "admin-pill hs-pill", hidden: true });
}

/** The card's body; activateHomeSecurity fills it. */
export function homeSecuritySection(): HTMLElement {
  return el("div", { class: "admin-block admin-hs" }, el("div", { class: "hs-body", "aria-live": "polite" }));
}

function row(label: string, ...value: Array<Node | string>): HTMLElement {
  return el("div", { class: "hs-row" }, el("span", { class: "hs-k" }, label), el("span", { class: "hs-v" }, ...value));
}

const outLink = (text: string, href: string): HTMLElement =>
  el("a", { class: "hs-link", href, target: "_blank", rel: "noopener noreferrer" }, text);

/** The pill's text and class for a state. */
export function pillOf(kind: StateKind): { text: string; cls: string } {
  switch (kind) {
    case "ok":
      return { text: "OK", cls: "admin-pill-ok" };
    case "alerts":
      return { text: "Alerts", cls: "admin-pill-warn" };
    case "stale":
      return { text: "Stale", cls: "admin-pill-warn" };
    case "firewall":
      return { text: "Firewalla down", cls: "admin-pill-warn" };
    default:
      return { text: "Unreachable", cls: "admin-pill-warn" };
  }
}

/** Draw the card from a good answer, or from a failed read. */
export function renderHomeSecurity(
  body: HTMLElement,
  pill: HTMLElement | null,
  data: HomeSecurityStatus | null,
  error: unknown,
  now = Date.now(),
): void {
  if (pill) {
    const state = data ? pillOf(stateOf(data)) : null;
    pill.hidden = !state && !(error && !data);
    pill.className = `admin-pill hs-pill ${state ? state.cls : "admin-pill-warn"}`;
    pill.textContent = state ? state.text : error ? "Unknown" : "";
  }
  if (!data) {
    replaceChildren(body, [
      el(
        "p",
        { class: error ? "hs-note" : "meta" },
        error ? `Could not read home security: ${describeError(error)}` : "Reading...",
      ),
    ]);
    return;
  }
  const scan = agoText(data.lastScan, now);
  const links = [
    safeLink(data.link) ? outLink("Security tab", safeLink(data.link) as string) : null,
    safePortal(data.portalUrl) ? outLink("Firewalla", safePortal(data.portalUrl) as string) : null,
  ].filter((node): node is HTMLElement => node !== null);
  replaceChildren(body, [
    data.reachable ? null : el("p", { class: "hs-note", role: "status" }, "The home app did not answer."),
    error ? el("p", { class: "hs-note" }, `Showing the last answer: ${describeError(error)}`) : null,
    el(
      "div",
      { class: `hs-rows${data.reachable ? "" : " is-muted"}` },
      row("Firewalla", firewallText(data)),
      row("Open alerts", String(data.alerts)),
      row("Last scan", scan || "No scan yet", data.stale && scan ? el("small", {}, " (stale)") : ""),
    ),
    el(
      "div",
      { class: "hs-foot" },
      links.length ? el("span", { class: "hs-links" }, ...links) : el("span"),
      hint(SETTINGS_HINT, "Home security"),
    ),
  ]);
}

let timer: ReturnType<typeof setTimeout> | null = null;
let onFocus: (() => void) | null = null;
let generation = 0;

/** Read and draw the card *root* (holding the section and pill) and keep it fresh. */
export function activateHomeSecurity(context: ExtensionContext, root: HTMLElement): void {
  const body = root.querySelector<HTMLElement>(".hs-body");
  const pill = root.querySelector<HTMLElement>(".hs-pill");
  if (!body) throw new Error("Home security section is missing");
  const mine = ++generation;
  const live = () => generation === mine;
  if (onFocus) {
    window.removeEventListener("focus", onFocus);
    document.removeEventListener("visibilitychange", onFocus);
  }
  let data: HomeSecurityStatus | null = null;
  let error: unknown = null;
  let reading: Promise<void> | null = null;
  let adminSince = 0;
  const draw = () => {
    if (!live()) return;
    renderHomeSecurity(body, pill, data, error);
    const tile = homeSecurityTile(data, error);
    setTile(error && data ? staleTile(tile, error) : tile, Date.now());
  };
  const refresh = (): Promise<void> => {
    if (reading) return reading;
    // After admin_only (card off, or not an admin) look again only every ADMIN_RECHECK_MS.
    if (error && isAdminOnly(error) && Date.now() - adminSince < ADMIN_RECHECK_MS) {
      return Promise.resolve();
    }
    reading = fetchHomeSecurity(context)
      .then(
        (next) => {
          data = next;
          error = null;
        },
        (failure: unknown) => {
          error = failure;
          if (isAdminOnly(failure)) adminSince = Date.now();
        },
      )
      .then(() => {
        if (!live()) return;
        draw();
        if (timer) clearTimeout(timer);
        timer = setTimeout(
          () => {
            if (live() && document.visibilityState === "visible") void refresh();
          },
          error && isAdminOnly(error) ? ADMIN_RECHECK_MS : POLL_MS,
        );
      })
      .finally(() => {
        reading = null;
      });
    return reading;
  };
  draw();
  void refresh();
  registerRefresh("homesecurity", () => (live() ? refresh() : undefined));
  onFocus = () => {
    if (document.visibilityState === "visible") void refresh();
  };
  window.addEventListener("focus", onFocus);
  document.addEventListener("visibilitychange", onFocus);
}

export function deactivateHomeSecurity(): void {
  generation++;
  if (timer) clearTimeout(timer);
  timer = null;
  if (onFocus) {
    window.removeEventListener("focus", onFocus);
    document.removeEventListener("visibilitychange", onFocus);
  }
  onFocus = null;
}
