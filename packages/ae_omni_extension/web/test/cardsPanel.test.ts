// The Cards panel (2026-11-07, card setup): render, the switch, a 409, focus, labels, Hide for me.
import type { ExtensionContext } from "@omnigent/extension-sdk";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { AeRequestError } from "../src/api";
import { type CardsAnswer, cardHidden, getCards, resetCardState, setCards } from "../src/cardState";
import { INTRO, type OpenCardsPanel, openCardsPanel } from "../src/cardsPanel";

const field = (env: string, status: "set" | "missing") => ({
  label: `Label of ${env}`,
  env,
  kind: "text" as const,
  hint: "A plain hint.",
  env_only: false,
  status,
});
const card = (id: string, group: "ship" | "box" | "cloud" | "money", over: object = {}) => ({
  id,
  name: `Name ${id}`,
  group,
  description: `About ${id}`,
  sources: [] as string[],
  on: true,
  default_on: true,
  changed_by: null,
  changed_at: null,
  fields: [field(`ENV_${id.toUpperCase()}`, "missing")],
  ...over,
});
const answer = (over: Partial<CardsAnswer> = {}): CardsAnswer => ({
  revision: 4,
  state_ok: true,
  cards: [
    card("deploys", "ship"),
    card("git", "ship", { on: false, changed_by: "owner@example.com", changed_at: "2026-11-07T10:00:00Z" }),
    card("box", "box"),
    card("vault", "cloud", { fields: [field("AE_VAULT_NAME", "set")] }),
    card("accounts", "money"),
  ],
  ...over,
});

function context(request: ReturnType<typeof vi.fn>, stored?: unknown) {
  const set = vi.fn().mockResolvedValue(undefined);
  const ctx = {
    server: { request },
    storage: { user: { get: vi.fn().mockResolvedValue(stored), set } },
    navigation: { openExternal: vi.fn().mockResolvedValue(undefined) },
  } as unknown as ExtensionContext;
  return { ctx, set };
}

const opened: OpenCardsPanel[] = [];
function open(ctx: ExtensionContext, options = {}) {
  const panel = openCardsPanel(ctx, options);
  opened.push(panel);
  return panel;
}
const rowOf = (id: string) => document.querySelector<HTMLElement>(`[data-card="${id}"]`)!;
const flush = () => new Promise((resolve) => setTimeout(resolve, 0));

beforeEach(() => {
  resetCardState();
  document.body.replaceChildren();
});
afterEach(() => {
  for (const panel of opened.splice(0)) panel.close();
  document.body.replaceChildren();
});

describe("the Cards panel", () => {
  it("lists the cards under their group headings in the board's order, with the intro line", () => {
    setCards(answer());
    open(context(vi.fn()).ctx);
    expect(document.body.textContent).toContain(INTRO);
    expect(document.querySelector("h3")!.textContent).toBe("Cards");
    const headings = [...document.querySelectorAll(".ae-cards-group")].map((n) => n.textContent);
    expect(headings).toEqual(["Ship", "Box", "Cloud", "Accounts"]);
    const ids = [...document.querySelectorAll<HTMLElement>("[data-card]")].map((n) => n.dataset.card);
    expect(ids).toEqual(["deploys", "git", "box", "vault", "accounts"]);
    expect(rowOf("deploys").textContent).toContain("About deploys");
  });

  it("shows a state chip and says who turned an off card off", () => {
    setCards(answer());
    open(context(vi.fn()).ctx, { notSetUp: (id: string) => id === "box" });
    const chip = (id: string) => rowOf(id).querySelector(".ae-card-chip")!.textContent;
    expect(chip("deploys")).toBe("On");
    expect(chip("git")).toBe("Off");
    expect(chip("box")).toBe("Needs setup");
    expect(rowOf("git").textContent).toMatch(/Turned off by owner@example\.com on .*2026/);
  });

  it("unfolds Setup on a tap: label, hint, source, variable name, copy", () => {
    setCards(answer());
    open(context(vi.fn()).ctx);
    const setup = rowOf("vault").querySelector<HTMLElement>(".ae-card-setup")!;
    expect(setup.hidden).toBe(true);
    rowOf("vault").querySelector<HTMLButtonElement>(".ae-card-row-head")!.click();
    const unfolded = rowOf("vault").querySelector<HTMLElement>(".ae-card-setup")!;
    expect(unfolded.hidden).toBe(false);
    expect(unfolded.textContent).toContain("Label of AE_VAULT_NAME");
    expect(unfolded.textContent).toContain("Set in the server's environment");
    expect(unfolded.querySelector("code")!.textContent).toBe("AE_VAULT_NAME");
    expect(unfolded.querySelector("[data-hint]")!.getAttribute("data-hint")).toBe("A plain hint.");
    expect(unfolded.querySelector('[aria-label^="Copy the variable name"]')).not.toBeNull();
    expect(unfolded.textContent).toContain("docs/features/admin-center.md");
    rowOf("box").querySelector<HTMLButtonElement>(".ae-card-row-head")!.click();
    expect(rowOf("box").textContent).toContain("Not set");
  });

  it("opens on the focus card with its Setup unfolded", () => {
    setCards(answer());
    open(context(vi.fn()).ctx, { focus: "vault" });
    expect(rowOf("vault").querySelector<HTMLElement>(".ae-card-setup")!.hidden).toBe(false);
    expect(rowOf("git").querySelector<HTMLElement>(".ae-card-setup")!.hidden).toBe(true);
    expect(rowOf("vault").hasAttribute("data-focus")).toBe(true);
  });

  it("posts the switch with the revision and redraws from the answer", async () => {
    setCards(answer());
    const next = answer({ revision: 5, cards: answer().cards.map((c) => (c.id === "vault" ? { ...c, on: false } : c)) });
    const request = vi.fn().mockResolvedValue({ ok: true, status: 200, body: next });
    open(context(request).ctx);
    rowOf("vault").querySelector<HTMLButtonElement>(".ae-card-switch")!.click();
    await flush();
    expect(request).toHaveBeenCalledWith({
      method: "POST",
      path: "/v1/ae/cards/state",
      body: { id: "vault", on: false, revision: 4 },
    });
    expect(getCards()!.revision).toBe(5);
    expect(rowOf("vault").querySelector(".ae-card-chip")!.textContent).toBe("Off");
    expect(rowOf("vault").querySelector(".ae-card-switch")!.getAttribute("aria-checked")).toBe("false");
  });

  it("redraws from the 409 answer and says the state changed elsewhere", async () => {
    setCards(answer());
    const current = answer({ revision: 9, cards: answer().cards.map((c) => (c.id === "deploys" ? { ...c, on: false } : c)) });
    const request = vi
      .fn()
      .mockResolvedValueOnce({ ok: true, status: 200, body: answer() })
      .mockResolvedValue({ ok: false, status: 409, body: { error: { code: "cards_changed_elsewhere", message: "m", current } } });
    open(context(request).ctx);
    await flush();
    rowOf("vault").querySelector<HTMLButtonElement>(".ae-card-switch")!.click();
    await flush();
    expect(getCards()!.revision).toBe(9);
    expect(rowOf("deploys").querySelector(".ae-card-chip")!.textContent).toBe("Off");
    const note = document.querySelector<HTMLElement>(".ae-cards-note")!;
    expect(note.hidden).toBe(false);
    expect(note.textContent).toContain("changed elsewhere");
  });

  it("shows the browser-only gate's FastAPI detail text, not a bare status", async () => {
    setCards(answer());
    const request = vi
      .fn()
      .mockResolvedValueOnce({ ok: true, status: 200, body: answer() })
      .mockResolvedValue({ ok: false, status: 403, body: { detail: "Use the Admin Center page." } });
    open(context(request).ctx);
    await flush();
    rowOf("vault").querySelector<HTMLButtonElement>(".ae-card-switch")!.click();
    await flush();
    expect(document.querySelector(".ae-cards-note")!.textContent).toBe("Use the Admin Center page.");
  });

  it("reads again on open while the cached rows stay, and uses the public docs link", async () => {
    setCards(answer());
    const fresh = answer({ revision: 8, cards: answer().cards.map((c) => (c.id === "deploys" ? { ...c, on: false } : c)) });
    const request = vi.fn().mockResolvedValue({ ok: true, status: 200, body: fresh });
    const { ctx } = context(request);
    open(ctx);
    expect(rowOf("deploys").querySelector(".ae-card-chip")!.textContent).toBe("On");
    await flush();
    expect(request).toHaveBeenCalledWith({ method: "GET", path: "/v1/ae/cards", body: undefined });
    expect(getCards()!.revision).toBe(8);
    expect(rowOf("deploys").querySelector(".ae-card-chip")!.textContent).toBe("Off");
    rowOf("vault").querySelector<HTMLButtonElement>(".ae-card-docs")!.click();
    expect(ctx.navigation.openExternal).toHaveBeenCalledWith(
      "https://github.com/analyticendeavors/omnigent-ae-public/blob/main/docs/features/admin-center.md",
    );
  });

  it("shows another error's message and keeps the state", async () => {
    setCards(answer());
    const request = vi
      .fn()
      .mockResolvedValueOnce({ ok: true, status: 200, body: answer() })
      .mockRejectedValue(new AeRequestError(500, { error: { code: "x", message: "Could not save." } }));
    open(context(request).ctx);
    await flush();
    rowOf("vault").querySelector<HTMLButtonElement>(".ae-card-switch")!.click();
    await flush();
    expect(document.querySelector(".ae-cards-note")!.textContent).toBe("Could not save.");
    expect(getCards()!.revision).toBe(4);
  });

  it("shows the word On or Off in the switch, matching aria-checked", () => {
    setCards(answer());
    open(context(vi.fn()).ctx);
    for (const [id, word, checked] of [["vault", "On", "true"], ["git", "Off", "false"]]) {
      const toggle = rowOf(id).querySelector<HTMLButtonElement>(".ae-card-switch")!;
      expect(toggle.querySelector(".ae-card-switch-word")!.textContent).toBe(word);
      expect(toggle.getAttribute("aria-checked")).toBe(checked);
      expect(toggle.querySelector(".ae-card-switch-track .ae-card-switch-knob")).not.toBeNull();
    }
  });

  it("flips the card when the word inside the switch is clicked", async () => {
    setCards(answer());
    const next = answer({ revision: 5, cards: answer().cards.map((c) => (c.id === "vault" ? { ...c, on: false } : c)) });
    const request = vi.fn().mockResolvedValue({ ok: true, status: 200, body: next });
    open(context(request).ctx);
    rowOf("vault").querySelector<HTMLElement>(".ae-card-switch-word")!.click();
    await flush();
    expect(request).toHaveBeenCalledWith({
      method: "POST",
      path: "/v1/ae/cards/state",
      body: { id: "vault", on: false, revision: 4 },
    });
  });

  it("disables every switch with a hint when the state file is unreadable", () => {
    setCards(answer({ state_ok: false }));
    open(context(vi.fn()).ctx);
    for (const toggle of document.querySelectorAll<HTMLButtonElement>(".ae-card-switch")) {
      expect(toggle.disabled).toBe(true);
      expect(toggle.title).toContain("cannot be read");
    }
  });

  it("hides a card for the viewer, writes storage.user and flips the eye", () => {
    setCards(answer());
    const { ctx, set } = context(vi.fn());
    open(ctx);
    rowOf("box").querySelector<HTMLButtonElement>(".ae-card-eye")!.click();
    expect(set).toHaveBeenCalledWith("cards.hidden", ["box"]);
    expect(cardHidden("box")).toBe(true);
    expect(rowOf("box").querySelector(".ae-card-chip")!.textContent).toBe("Hidden for you");
    expect(rowOf("box").querySelector(".ae-card-eye")!.getAttribute("aria-pressed")).toBe("true");
  });

  it("gives every control a name or a hint", () => {
    setCards(answer());
    open(context(vi.fn()).ctx, { focus: "vault" });
    const controls = document.querySelectorAll<HTMLElement>("button, [role=switch]");
    expect(controls.length).toBeGreaterThan(10);
    for (const control of controls) {
      const named = control.getAttribute("aria-label") || control.getAttribute("title") || control.textContent;
      expect(named?.trim(), control.outerHTML).toBeTruthy();
    }
  });

  it("closes on Escape and on the close button, and returns focus to the opener", () => {
    setCards(answer());
    const opener = document.createElement("button");
    document.body.appendChild(opener);
    opener.focus();
    open(context(vi.fn()).ctx, { opener });
    expect(document.activeElement).toBe(document.querySelector('[aria-label="Close the Cards panel"]'));
    document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
    expect(document.querySelector(".ae-cards-drawer")).toBeNull();
    expect(document.activeElement).toBe(opener);
    open(context(vi.fn()).ctx, { opener });
    document.querySelector<HTMLButtonElement>('[aria-label="Close the Cards panel"]')!.click();
    expect(document.querySelector(".ae-cards-drawer")).toBeNull();
  });

  it("traps Tab inside the drawer", () => {
    setCards(answer());
    open(context(vi.fn()).ctx);
    const drawer = document.querySelector<HTMLElement>(".ae-cards-drawer")!;
    const items = [...drawer.querySelectorAll<HTMLButtonElement>("button")].filter((b) => !b.disabled && !b.closest("[hidden]"));
    items[items.length - 1].focus();
    const tab = new KeyboardEvent("keydown", { key: "Tab", bubbles: true, cancelable: true });
    document.dispatchEvent(tab);
    expect(tab.defaultPrevented).toBe(true);
    expect(document.activeElement).toBe(items[0]);
  });

  it("is a labelled modal dialog", () => {
    setCards(answer());
    open(context(vi.fn()).ctx);
    const drawer = document.querySelector('[role="dialog"]')!;
    expect(drawer.getAttribute("aria-modal")).toBe("true");
    expect(document.getElementById(drawer.getAttribute("aria-labelledby")!)!.textContent).toBe("Cards");
  });

  it("reads the cards itself when none are held, and says so for a non-admin", async () => {
    const good = vi.fn().mockResolvedValue({ ok: true, status: 200, body: answer() });
    open(context(good).ctx);
    await flush();
    expect(good).toHaveBeenCalledWith({ method: "GET", path: "/v1/ae/cards", body: undefined });
    expect(rowOf("vault")).not.toBeNull();
    for (const panel of opened.splice(0)) panel.close();
    resetCardState();
    const refused = vi
      .fn()
      .mockResolvedValue({ ok: false, status: 403, body: { error: { code: "admin_only", message: "no" } } });
    open(context(refused).ctx);
    await flush();
    expect(document.body.textContent).toContain("Only an OmniGent admin");
  });
});
