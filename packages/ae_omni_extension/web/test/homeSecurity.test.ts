// The Home security card (2026-11-08): the folded line for every state, the hint, the links, the
// board's footer when it is off, the poller and a failed read.
import type { ExtensionContext } from "@omnigent/extension-sdk";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ADMIN_RECHECK_MS, AeRequestError, type HomeSecurityStatus } from "../src/api";
import { cardNotSetUp } from "../src/board";
import { HINT_MAX } from "../src/hint";
import {
  activateHomeSecurity,
  agoText,
  deactivateHomeSecurity,
  firewallText,
  homeSecurityPill,
  homeSecuritySection,
  homeSecurityTile,
  POLL_MS,
  safeLink,
  safePortal,
  renderHomeSecurity,
  SETTINGS_HINT,
  stateOf,
  tileText,
} from "../src/homeSecurity";
import { getTile, resetTiles } from "../src/tiles";

const sdk = vi.hoisted(() => ({ defineExtension: vi.fn() }));
vi.mock("@omnigent/extension-sdk", () => sdk);

const status = (extra: Partial<HomeSecurityStatus> = {}): HomeSecurityStatus => ({
  reachable: true,
  stale: false,
  lastScan: "2026-11-08T09:00:00Z",
  alerts: 0,
  firewalla: { api: "up", boxOnline: true, checkedAt: "2026-11-08T10:00:00Z" },
  portalUrl: "https://fw.firewalla.net/box/abc",
  link: "https://home.example.com/security",
  readAt: "2026-11-08T10:00:05Z",
  ...extra,
});

describe("the folded line", () => {
  it("says the short text for each state, worst first", () => {
    expect(tileText(status())).toBe("Home OK");
    expect(tileText(status({ alerts: 1 }))).toBe("Home: 1 alert");
    expect(tileText(status({ alerts: 2 }))).toBe("Home: 2 alerts");
    expect(tileText(status({ firewalla: { api: "down", boxOnline: null, checkedAt: null } }))).toBe("Firewalla down");
    expect(tileText(status({ firewalla: { api: "up", boxOnline: false, checkedAt: null } }))).toBe("Firewalla down");
    expect(tileText(status({ stale: true }))).toBe("Home: scan stale");
    // Alerts outrank a stale scan, and say the scan is old.
    expect(tileText(status({ alerts: 3, stale: true }))).toBe("Home: 3 alerts (old scan)");
    expect(stateOf(status({ alerts: 3, stale: true }))).toBe("alerts");
    // A stale summary never reads "Firewalla down": the row says old, the tile is not red.
    const down = { api: "down", boxOnline: false, checkedAt: null } as const;
    expect(tileText(status({ stale: true, firewalla: down }))).toBe("Home: scan stale");
    expect(homeSecurityTile(status({ stale: true, firewalla: down }), null).health).toBe("attention");
    expect(firewallText(status({ stale: true, firewalla: down }))).toBe("Cannot be reached (old)");
    expect(firewallText(status({ stale: true }))).toBe("Up, box online (old)");
    expect(tileText(status({ reachable: false, alerts: 4 }))).toBe("Home unreachable");
    // Firewalla down outranks alerts; an unreachable home app outranks the firewall.
    expect(stateOf(status({ alerts: 3, firewalla: { api: "down", boxOnline: null, checkedAt: null } }))).toBe("firewall");
    expect(stateOf(status({ reachable: false, firewalla: { api: "down", boxOnline: null, checkedAt: null } }))).toBe(
      "unreachable",
    );
  });

  it("is green only when all is well, and an absent firewall does not count against it", () => {
    expect(homeSecurityTile(status(), null).health).toBe("ok");
    expect(homeSecurityTile(status({ firewalla: null }), null).health).toBe("ok");
    expect(homeSecurityTile(status({ alerts: 2 }), null).health).toBe("attention");
    expect(homeSecurityTile(status({ reachable: false }), null).health).toBe("problem");
    expect(homeSecurityTile(status(), null).brief).toBe("Home OK");
  });

  it("is Not set up for an off card, Unknown for a failed read and Reading before one", () => {
    const off = new AeRequestError(403, { error: { code: "admin_only", message: "off" } });
    expect(homeSecurityTile(null, off)).toMatchObject({ value: "Not set up", notSetUp: true, health: "idle" });
    expect(homeSecurityTile(null, new Error("boom"))).toMatchObject({ value: "Unknown", health: "attention" });
    expect(homeSecurityTile(null, null).value).toBe("Reading...");
    expect(homeSecurityTile(null, new Error("boom")).notSetUp).toBeUndefined();
  });
});

describe("the words", () => {
  it("reads the firewall row", () => {
    expect(firewallText(status())).toBe("Up, box online");
    expect(firewallText(status({ firewalla: null }))).toBe("Not reported");
    expect(firewallText(status({ firewalla: { api: "down", boxOnline: null, checkedAt: null } }))).toBe(
      "Cannot be reached",
    );
    expect(firewallText(status({ firewalla: { api: "unknown", boxOnline: null, checkedAt: null } }))).toBe("Unknown");
  });

  it("ages a scan and tolerates a bad time", () => {
    const now = Date.parse("2026-11-08T12:00:00Z");
    expect(agoText("2026-11-08T11:59:30Z", now)).toBe("just now");
    expect(agoText("2026-11-08T11:15:00Z", now)).toBe("45 min ago");
    expect(agoText("2026-11-08T09:00:00Z", now)).toBe("3 h ago");
    expect(agoText("2026-11-04T12:00:00Z", now)).toBe("4 d ago");
    expect(agoText(null, now)).toBe("");
    expect(agoText("nonsense", now)).toBe("");
  });

  it("carries a settings hint that is plain and short", () => {
    expect(SETTINGS_HINT.length).toBeGreaterThan(0);
    expect(SETTINGS_HINT.length).toBeLessThanOrEqual(HINT_MAX);
  });
});

describe("links", () => {
  it("keeps a portal link only on firewalla.net or a subdomain, https, as the browser reads it", () => {
    expect(safePortal("https://fw.firewalla.net/box/abc")).toBe("https://fw.firewalla.net/box/abc");
    expect(safePortal("https://firewalla.net/")).toBe("https://firewalla.net/");
    expect(safePortal("http://fw.firewalla.net/")).toBeNull();
    expect(safePortal("https://notfirewalla.net/")).toBeNull();
    expect(safePortal("https://firewalla.net.evil.example.com/")).toBeNull();
    expect(safePortal("https://name" + "@fw.firewalla.net/")).toBeNull();
    expect(safePortal("javascript:alert(1)")).toBeNull();
    expect(safePortal(null)).toBeNull();
  });

  it("refuses the parser-confusion case a browser and the server read differently", () => {
    const confused = "https://evil.example\\sub.firewalla.net/x";
    // The browser reads the backslash as a slash, so the host is evil.example.
    expect(new URL(confused).hostname).toBe("evil.example");
    expect(safePortal(confused)).toBeNull();
    expect(safeLink(confused)).toBeNull();
    expect(safePortal("https://fw.firewalla.net/a b")).toBeNull();
  });

  it("keeps a security link only when it is http or https", () => {
    expect(safeLink("https://home.example.com/security")).toBe("https://home.example.com/security");
    expect(safeLink("http://192.0.2.10:8080/")).toBe("http://192.0.2.10:8080/");
    expect(safeLink("javascript:alert(1)")).toBeNull();
    expect(safeLink("ftp://home.example.com/")).toBeNull();
  });

  it("draws no link for a confused address even if the server sent it", () => {
    const body = document.createElement("div");
    renderHomeSecurity(body, null, status({ portalUrl: "https://evil.example\\sub.firewalla.net/x" }), null);
    expect([...body.querySelectorAll("a")].map((a) => a.textContent)).toEqual(["Security tab"]);
  });
});

describe("the card", () => {
  it("shows the rows and two links that open in a new tab", () => {
    const body = document.createElement("div");
    const pill = homeSecurityPill();
    renderHomeSecurity(body, pill, status({ alerts: 2 }), null, Date.parse("2026-11-08T12:00:00Z"));
    expect(body.textContent).toContain("Up, box online");
    expect(body.textContent).toContain("3 h ago");
    expect(pill.textContent).toBe("Alerts");
    const links = [...body.querySelectorAll<HTMLAnchorElement>("a.hs-link")];
    expect(links.map((a) => [a.textContent, a.href])).toEqual([
      ["Security tab", "https://home.example.com/security"],
      ["Firewalla", "https://fw.firewalla.net/box/abc"],
    ]);
    for (const a of links) {
      expect(a.target).toBe("_blank");
      expect(a.rel).toContain("noopener");
    }
  });

  it("leaves out a link the server did not send", () => {
    const body = document.createElement("div");
    renderHomeSecurity(body, null, status({ link: null, portalUrl: null }), null);
    expect(body.querySelectorAll("a")).toHaveLength(0);
    expect(body.querySelector(".ae-hint-btn")).not.toBeNull();
  });

  it("says so when the home app did not answer, and when a read failed", () => {
    const body = document.createElement("div");
    renderHomeSecurity(body, null, status({ reachable: false }), null);
    expect(body.textContent).toContain("did not answer");
    renderHomeSecurity(body, null, null, new Error("offline"));
    expect(body.textContent).toContain("Could not read home security");
  });
});

describe("the poller", () => {
  const ctx = (request: ReturnType<typeof answer>) => ({ server: { request } }) as unknown as ExtensionContext;
  const answer = (body: unknown) =>
    vi.fn(async (_req: { method: string; path: string }) => ({ ok: true, status: 200, body }));

  beforeEach(() => {
    vi.useFakeTimers();
    resetTiles();
  });
  afterEach(() => {
    deactivateHomeSecurity();
    vi.useRealTimers();
  });

  it("reads every 60 s, skips a hidden tab and stops on deactivate", async () => {
    const request = answer(status());
    const root = document.createElement("div");
    root.append(homeSecuritySection(), homeSecurityPill());
    activateHomeSecurity(ctx(request), root);
    await vi.advanceTimersByTimeAsync(0);
    expect(request).toHaveBeenCalledTimes(1);
    expect(request.mock.calls[0][0]).toMatchObject({ method: "GET", path: "/v1/ae/home-security" });
    expect(getTile("homesecurity")?.value).toBe("Home OK");

    await vi.advanceTimersByTimeAsync(POLL_MS);
    expect(request).toHaveBeenCalledTimes(2);

    const visibility = vi.spyOn(document, "visibilityState", "get").mockReturnValue("hidden");
    await vi.advanceTimersByTimeAsync(POLL_MS);
    expect(request).toHaveBeenCalledTimes(2);
    visibility.mockRestore();

    await vi.advanceTimersByTimeAsync(0);
    deactivateHomeSecurity();
    await vi.advanceTimersByTimeAsync(POLL_MS * 3);
    expect(request).toHaveBeenCalledTimes(2);
  });

  it("backs off to the admin recheck interval after an admin_only answer", async () => {
    const request = vi.fn(async (_req: { method: string; path: string }) => ({
      ok: false,
      status: 403,
      body: { error: { code: "admin_only", message: "no" } },
    }));
    const root = document.createElement("div");
    root.append(homeSecuritySection(), homeSecurityPill());
    activateHomeSecurity(ctx(request), root);
    await vi.advanceTimersByTimeAsync(0);
    expect(request).toHaveBeenCalledTimes(1);
    await vi.advanceTimersByTimeAsync(POLL_MS * 3);
    expect(request).toHaveBeenCalledTimes(1);
    // A focus inside the window does not read again either.
    window.dispatchEvent(new Event("focus"));
    await vi.advanceTimersByTimeAsync(0);
    expect(request).toHaveBeenCalledTimes(1);
    await vi.advanceTimersByTimeAsync(ADMIN_RECHECK_MS);
    expect(request).toHaveBeenCalledTimes(2);
  });

  it("takes an off answer as Not set up and leaves the board", async () => {
    const request = answer({ off: true, card: "home-security" });
    const root = document.createElement("div");
    root.append(homeSecuritySection(), homeSecurityPill());
    activateHomeSecurity(ctx(request), root);
    await vi.advanceTimersByTimeAsync(0);
    expect(getTile("homesecurity")?.notSetUp).toBe(true);
    expect(cardNotSetUp("home-security")).toBe(true);
  });
});
