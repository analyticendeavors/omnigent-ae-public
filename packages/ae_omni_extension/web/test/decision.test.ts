// The Decision model card (2026-11-07): every state, the labels, the hint, the board's footer
// when it is not set up, and a failed read.
import type { ExtensionContext } from "@omnigent/extension-sdk";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { DecisionStatus } from "../src/api";
import {
  activateDecision,
  deactivateDecision,
  decisionPill,
  decisionSection,
  decisionTile,
  modelText,
  pointLabel,
  reasonText,
  renderDecision,
  retiresText,
  stateLine,
  stateLines,
  stateOf,
  SWITCH_HINT,
  SWITCH_LINE,
} from "../src/decision";
import { HINT_MAX } from "../src/hint";
import { cardNotSetUp } from "../src/board";
import { getTile, resetTiles } from "../src/tiles";

const sdk = vi.hoisted(() => ({ defineExtension: vi.fn() }));
vi.mock("@omnigent/extension-sdk", () => sdk);

const status = (extra: Partial<DecisionStatus> = {}): DecisionStatus => ({
  configured: true,
  enabled: true,
  available: true,
  model: "decision-1",
  retires: "2027-02-04",
  calls_today: 37,
  daily_cap: 400,
  paused_until: null,
  points: [
    { name: "step_down", mode: "shadow" },
    { name: "review_stop", mode: "off" },
    { name: "agent_ask", mode: "live" },
    { name: "advisor", mode: "live" },
    { name: "future_point", mode: "off" },
  ],
  shadow: { would_act_7d: 142, lines_7d: 300 },
  ...extra,
});

function draw(data: DecisionStatus | null, error: unknown = null) {
  const card = document.createElement("div");
  const pill = decisionPill();
  const section = decisionSection();
  card.append(pill, section);
  renderDecision(section.querySelector<HTMLElement>(".decision-body")!, pill, data, error);
  return { card, pill, text: card.textContent ?? "" };
}

beforeEach(() => resetTiles());
afterEach(() => deactivateDecision());

describe("labels and words", () => {
  it("names the four points and shows any other raw", () => {
    expect(pointLabel("step_down")).toBe("Lead step-down");
    expect(pointLabel("review_stop")).toBe("Review loop stop");
    expect(pointLabel("agent_ask")).toBe("Agent questions (ask_judge)");
    expect(pointLabel("advisor")).toBe("Preset advisor (Lead picks)");
    expect(pointLabel("future_point")).toBe("future_point");
  });
  it("says the model and its retirement date", () => {
    expect(modelText("decision-1")).toBe("Decision-1");
    expect(modelText(null)).toBe("Unknown");
    expect(retiresText("2027-02-04")).toBe("retires Feb 4, 2027");
    expect(retiresText(null)).toBeNull();
    expect(retiresText("soon")).toBeNull();
  });
  it("explains each reason in plain words with the setting to fix", () => {
    expect(reasonText("no_users")).toBe(
      "On, but no users are allowed: set AE_DECISION_USERS (on a single-user server, local)",
    );
    expect(reasonText("no_denylist")).toContain("refuses every call");
    expect(reasonText("no_endpoint")).toContain("AE_DECISION_ENDPOINT");
    expect(reasonText("no_key")).toContain("AE_DECISION_API_KEY");
    expect(reasonText("not_allowed")).toContain("allowed list");
  });
  it("keeps the hint within the hint limit", () => {
    expect(SWITCH_HINT.length).toBeLessThanOrEqual(HINT_MAX);
  });
});

describe("states", () => {
  it("On: green pill, calls, chips, shadow results, tooltip, no switch line", () => {
    const { pill, text, card } = draw(status());
    expect(pill.textContent).toBe("On");
    expect(pill.classList.contains("admin-pill-ok")).toBe(true);
    expect(text).toContain("Decision-1");
    expect(text).toContain("retires Feb 4, 2027");
    expect(text).toContain("37 of 400");
    expect(text).toContain("142 would-act calls in 7 days");
    expect(card.querySelector('[data-point="step_down"] .decision-chip')?.textContent).toBe("shadow");
    expect(card.querySelector('[data-point="advisor"] .decision-chip-live')).not.toBeNull();
    expect(text).not.toContain(SWITCH_LINE);
    expect(card.querySelector(".ae-hint-btn")?.getAttribute("data-hint")).toBe(SWITCH_HINT);
    expect(card.querySelector(".is-muted")).toBeNull();
    expect(stateOf(status())).toBe("on");
    expect(decisionTile(status(), null)).toMatchObject({ id: "decision", value: "On", health: "ok" });
  });

  it("Off: muted rows, the switch line is visible, the tooltip is there too", () => {
    const data = status({ enabled: false, available: false, reason: "not_enabled" });
    const { pill, text, card } = draw(data);
    expect(pill.textContent).toBe("Off");
    expect(card.querySelector(".decision-rows.is-muted")).not.toBeNull();
    expect(text).toContain(SWITCH_LINE);
    expect(card.querySelector(".ae-hint-btn")).not.toBeNull();
    expect(decisionTile(data, null)).toMatchObject({ value: "Off", health: "idle" });
    expect(decisionTile(data, null).notSetUp).toBeUndefined();
  });

  it("amber: enabled but not available says why", () => {
    const data = status({ available: false, reason: "no_users" });
    const { pill, text } = draw(data);
    expect(pill.classList.contains("admin-pill-warn")).toBe(true);
    expect(text).toContain("On, but no users are allowed: set AE_DECISION_USERS");
    expect(text).not.toContain(SWITCH_LINE);
    expect(stateLine(data)).toContain("set AE_DECISION_USERS");
    expect(decisionTile(data, null)).toMatchObject({ health: "attention", value: "Needs setup" });
    // Enabled with nothing configured is amber too, not a card that vanishes.
    const bare = status({ configured: false, available: false, reason: "no_endpoint" });
    expect(stateOf(bare)).toBe("setup");
    expect(decisionTile(bare, null).notSetUp).toBeUndefined();
  });

  it("Error: amber while the breaker is open, with the time", () => {
    const until = new Date(Date.UTC(2026, 10, 7, 18, 30)).toISOString();
    const data = status({ paused_until: until });
    const { pill, text } = draw(data);
    expect(pill.textContent).toBe("Error");
    expect(pill.classList.contains("admin-pill-warn")).toBe(true);
    expect(text).toMatch(/Paused until .*:\d\d.* after errors in a row; points use their normal rules\./);
    expect(decisionTile(data, null)).toMatchObject({ value: "Paused", health: "attention" });
  });

  it("not set up: the tile says so and the card leaves the board", () => {
    const data = status({
      configured: false,
      set_up: false,
      enabled: false,
      available: false,
      reason: "not_enabled",
    });
    expect(stateOf(data)).toBe("unset");
    const tile = decisionTile(data, null);
    expect(tile).toMatchObject({ notSetUp: true, health: "idle" });
  });

  it("partly set up but disabled shows Off, not Not set up", () => {
    const data = status({
      configured: false,
      set_up: true,
      enabled: false,
      available: false,
      reason: "not_enabled",
    });
    expect(stateOf(data)).toBe("off");
    expect(decisionTile(data, null).notSetUp).toBeUndefined();
    expect(draw(data).pill.textContent).toBe("Off");
  });

  it("not_allowed says the caller is not on the list", () => {
    const data = status({ available: false, reason: "not_allowed" });
    expect(draw(data).text).toContain("you are not on the allowed list");
  });

  it("the reason comes first and the pause is said under it", () => {
    const until = new Date(Date.UTC(2026, 10, 7, 18, 30)).toISOString();
    const data = status({ available: false, reason: "no_key", paused_until: until });
    expect(stateOf(data)).toBe("setup");
    const lines = stateLines(data);
    expect(lines[0]).toContain("AE_DECISION_API_KEY");
    expect(lines[1]).toMatch(/^Paused until /);
    const notes = draw(data).card.querySelectorAll(".decision-note");
    expect(notes).toHaveLength(2);
  });

  it("an error with no reason says the status could not be read", () => {
    const data = status({ available: false, reason: undefined, error: "RuntimeError" });
    expect(stateLines(data)[0]).toBe("Status could not be read");
  });

  it("the server's fallback answer still renders", () => {
    const blank = {
      configured: false,
      set_up: true,
      enabled: true,
      available: false,
      model: null,
      retires: null,
      calls_today: 0,
      daily_cap: 0,
      paused_until: null,
      points: [],
      shadow: { would_act_7d: 0, lines_7d: 0 },
      error: "OSError",
    } as DecisionStatus;
    expect(draw(blank).text).toContain("could not be read (OSError)");
    // An older or odd answer with no points or shadow does not throw either.
    const odd = { ...blank, points: undefined, shadow: undefined } as unknown as DecisionStatus;
    expect(() => draw(odd)).not.toThrow();
  });

  it("a failed first read: amber Unknown and the error, not a not-set-up card", () => {
    const { pill, text } = draw(null, new Error("Request failed"));
    expect(pill.textContent).toBe("Unknown");
    expect(text).toContain("Could not read the Decision model: Request failed");
    expect(decisionTile(null, new Error("x"))).toMatchObject({ value: "Unknown", health: "attention" });
    expect(decisionTile(null, new Error("x")).notSetUp).toBeUndefined();
  });
});

describe("the live card", () => {
  const context = (reply: () => Promise<unknown>) =>
    ({ server: { request: vi.fn(reply) } }) as unknown as ExtensionContext;
  const flush = async () => {
    for (let i = 0; i < 5; i++) await Promise.resolve();
  };

  it("reads GET /v1/ae/decision, draws the card and reports the tile", async () => {
    const request = vi.fn(async () => ({ ok: true, status: 200, body: status() }));
    const card = document.createElement("div");
    card.append(decisionPill(), decisionSection());
    activateDecision({ server: { request } } as unknown as ExtensionContext, card);
    await flush();
    expect(request).toHaveBeenCalledWith({ method: "GET", path: "/v1/ae/decision", body: undefined });
    expect(card.querySelector(".decision-pill")?.textContent).toBe("On");
    expect(getTile("decision")?.value).toBe("On");
  });

  it("a not-configured answer marks the tile not set up so the board drops the card", async () => {
    const body = status({ configured: false, enabled: false, available: false, reason: "not_enabled" });
    const card = document.createElement("div");
    card.append(decisionPill(), decisionSection());
    activateDecision(context(async () => ({ ok: true, status: 200, body })), card);
    await flush();
    expect(getTile("decision")?.notSetUp).toBe(true);
    expect(cardNotSetUp("decision")).toBe(true);
  });

  it("a failed read shows the error and keeps the card on the board", async () => {
    const card = document.createElement("div");
    card.append(decisionPill(), decisionSection());
    activateDecision(context(async () => ({ ok: false, status: 500, body: null })), card);
    await flush();
    expect(card.textContent).toContain("Could not read the Decision model");
    expect(getTile("decision")?.health).toBe("attention");
    expect(cardNotSetUp("decision")).toBe(false);
  });
});
