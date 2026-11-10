// The Cards wiring (2026-11-07): the gear, off and hidden cards leaving the board, the footer line,
// the status pill, Needs you, and the Cards button on the page.
import type { ExtensionContext } from "@omnigent/extension-sdk";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { buildBlocks } from "../src/attention";
import { attentionView } from "../src/attentionView";
import { resetAttention, setAttention } from "../src/attentionStore";
import { verdictParts } from "../src/verdict";
import { openCardsPanel } from "../src/cardsPanel";
import { mountTiles } from "../src/tiles";
import { adminCard, arrange, type CardId, CARD_SETTINGS_EVENT, mountBoard } from "../src/board";
import { resetBoardLayoutState } from "../src/boardLayout";
import { type CardRow, cardOff, resetCardState, setCards, setHidden } from "../src/cardState";
import { activateFabric, deactivateFabric } from "../src/fabric";
import { pillStates, resetTiles, setTile } from "../src/tiles";

vi.mock("@omnigent/extension-sdk", () => ({ defineExtension: vi.fn() }));

const ids: CardId[] = ["deploys", "git", "social", "box", "home-security", "compute", "credit", "azure", "vault", "decision", "accounts", "presets"];
const row = (id: string, over: Partial<CardRow> = {}): CardRow => ({
  id,
  name: id,
  group: "ship",
  description: "d",
  sources: id === "git" ? ["git"] : [],
  on: true,
  default_on: true,
  changed_by: null,
  changed_at: null,
  fields: [],
  ...over,
});
const answer = (rows: CardRow[]) => ({ revision: 1, state_ok: true, cards: rows });

function board(columns: 1 | 2 | 3 = 3) {
  const node = document.createElement("div");
  node.className = "admin-board";
  document.body.appendChild(node);
  const cards = Object.fromEntries(
    ids.map((id) => [id, adminCard(id, `Title ${id}`, document.createElementNS("http://www.w3.org/2000/svg", "svg"), [], "body")]),
  ) as Record<CardId, HTMLElement>;
  arrange(node, cards, columns, true);
  return { node, cards };
}
const shown = (node: HTMLElement) => [...node.querySelectorAll<HTMLElement>(".admin-card")].map((c) => c.dataset.card);

beforeEach(() => {
  document.body.replaceChildren();
  resetCardState();
  resetBoardLayoutState();
  resetTiles();
});
afterEach(() => {
  deactivateFabric();
  resetCardState();
});

describe("the gear", () => {
  it("is in every card header, named, and raises the settings event with the card id", () => {
    const { node } = board();
    const gear = node.querySelector<HTMLButtonElement>('[data-card="vault"] .admin-gear')!;
    expect(gear.title).toBe("Card settings");
    expect(gear.getAttribute("aria-label")).toBe("Settings for Title vault");
    const heard = vi.fn();
    node.addEventListener(CARD_SETTINGS_EVENT, (e) => heard((e as CustomEvent).detail));
    gear.click();
    expect(heard).toHaveBeenCalledWith("vault");
    const header = gear.parentElement!;
    expect(gear.nextElementSibling).toBe(header.querySelector(".admin-grip"));
  });
});

describe("off and hidden cards", () => {
  it("leave the board and are named in one footer line with a Cards settings button", () => {
    setCards(
      answer([
        ...ids.filter((id) => id !== "social" && id !== "decision" && id !== "presets").map((id) => row(id)),
        row("decision", { on: false, changed_by: "owner@example.com", changed_at: "2026-11-07T12:00:00Z" }),
        row("presets"),
      ]),
    );
    setHidden(["presets"]);
    const { node } = board();
    expect(shown(node)).not.toContain("social");
    expect(shown(node)).not.toContain("decision");
    expect(shown(node)).not.toContain("presets");
    expect(shown(node)).toContain("git");
    const note = node.querySelector<HTMLElement>(".admin-notset")!;
    expect(note.hidden).toBe(false);
    expect(note.textContent).toMatch(/Title decision \(by owner@example\.com, [^)]*\)/);
    expect(note.textContent).toContain("Hidden for you: Title presets.");
    expect(note.textContent).not.toContain("Title social");
    expect(note.querySelector("button")!.textContent).toBe("Cards settings");
    expect(note.querySelector("code")).toBeNull();
  });

  it("draw no footer when every card is on, and nothing is off before the server answers", () => {
    const { node } = board();
    expect(shown(node)).toContain("social");
    expect(node.querySelector<HTMLElement>(".admin-notset")!.hidden).toBe(true);
  });

  it("join or leave the board when the state changes under a mounted board", () => {
    const page = document.createElement("div");
    document.body.appendChild(page);
    const { node, cards } = board();
    page.appendChild(node);
    const mounted = mountBoard(page, node, cards);
    expect(shown(node)).toContain("vault");
    setHidden(["vault"]);
    expect(shown(node)).not.toContain("vault");
    setHidden([]);
    expect(shown(node)).toContain("vault");
    mounted.stop();
  });

  it("take their tiles out of the status pill", () => {
    setTile({ id: "vault", value: "2", note: "", health: "problem" });
    expect(pillStates().some((p) => p.members.some((t) => t.id === "vault"))).toBe(true);
    setCards(answer(ids.map((id) => row(id, { on: id !== "vault" }))));
    expect(pillStates().some((p) => p.members.some((t) => t.id === "vault"))).toBe(false);
  });
});

describe("Needs you", () => {
  const item = (source: string, id: string) =>
    ({ id, source, kind: "x", tone: "urgent", label: id, working: null }) as never;
  it("drops the lines of a source whose card this viewer hid", () => {
    setCards(answer([row("git")]));
    const items = [item("git", "git:1"), item("box", "box:1")];
    expect(buildBlocks(items).map((b) => b.source)).toContain("git");
    setHidden(["git"]);
    expect(buildBlocks(items).map((b) => b.source)).not.toContain("git");
  });
});

describe("hiding a card and the counts", () => {
  const it1 = (id: string, source: string, tone: "urgent" | "review") =>
    ({ id, source, kind: "x", tone, label: id, working: null }) as never;
  const data = () =>
    ({
      items: [it1("git:1", "git", "urgent"), it1("git:2", "git", "urgent")],
      dismissed: [],
      counts: { urgent: 2, review: 0 },
      unknown: [],
      read_at: "2026-11-07T12:00:00Z",
      more: 0,
      sources: ["git"],
    }) as never;

  it("recounts without the hidden source, so the verdict and the empty test agree with the list", () => {
    setCards(answer([row("git")]));
    expect(attentionView(data()).urgent).toBe(2);
    resetAttention();
    setAttention(data());
    expect(verdictParts()!.urgent).toBe(2);
    setHidden(["git"]);
    const view = attentionView(data());
    expect(view.empty).toBe(true);
    expect([view.urgent, view.review]).toEqual([0, 0]);
    expect(verdictParts()!.rest).toContain("Nothing needs you");
    expect(verdictParts()!.urgent).toBe(0);
  });
});

describe("answers that arrive out of order", () => {
  it("ignores an older revision, so a slow read cannot undo a switch", () => {
    setCards({ ...answer([row("git", { on: true })]), revision: 5 });
    setCards({ ...answer([row("git", { on: false })]), revision: 4 });
    expect(cardOff("git")).toBe(false);
    setCards({ ...answer([row("git", { on: false })]), revision: 6 });
    expect(cardOff("git")).toBe(true);
  });
});

describe("the footer and pills in the DOM", () => {
  it("mentions no Social card on a server that never named it", () => {
    setCards(answer(ids.filter((id) => id !== "social").map((id) => row(id))));
    const { node } = board();
    expect(shown(node)).not.toContain("social");
    expect(node.querySelector<HTMLElement>(".admin-notset")!.hidden).toBe(true);
  });

  it("redraws a mounted pill when the only live card of its group is hidden", () => {
    const strip = document.createElement("div");
    const time = document.createElement("span");
    document.body.append(strip, time);
    mountTiles(strip, time);
    setTile({ id: "vault", value: "2", note: "", health: "problem" });
    expect(strip.children.length).toBe(1);
    setCards(answer(ids.map((id) => row(id))));
    expect(strip.children.length).toBe(1);
    setHidden(["vault"]);
    expect(strip.children.length).toBe(0);
    setHidden([]);
    expect(strip.children.length).toBe(1);
  });
});

describe("focus after the opener goes away", () => {
  it("falls back to the header Cards button", () => {
    setCards(answer(ids.map((id) => row(id))));
    const cardsBtn = document.createElement("button");
    cardsBtn.className = "admin-cards-btn";
    const gear = document.createElement("button");
    document.body.append(cardsBtn, gear);
    const ctx = {
      server: { request: vi.fn(async () => ({ ok: true, status: 200, body: answer(ids.map((id) => row(id))) })) },
      storage: { user: { get: vi.fn(), set: vi.fn().mockResolvedValue(undefined) } },
      navigation: {},
    } as unknown as ExtensionContext;
    const panel = openCardsPanel(ctx, { opener: gear });
    gear.remove();
    panel.close();
    expect(document.activeElement).toBe(cardsBtn);
  });
});

describe("the page", () => {
  function context(opts: { admin?: boolean } = {}) {
    const request = vi.fn(async ({ path }: { method: string; path: string }) => {
      if (path === "/v1/ae/access") return { ok: true, status: 200, body: { admin: opts.admin !== false, box_admin: true } };
      if (path === "/v1/ae/cards")
        return { ok: true, status: 200, body: answer(ids.map((id) => row(id, { on: id !== "social" }))) };
      return { ok: false, status: 500, body: { error: { code: "x", message: "none" } } };
    });
    const ctx = {
      view: "fabric",
      server: { request },
      storage: { user: { get: vi.fn().mockResolvedValue(["presets"]), set: vi.fn().mockResolvedValue(undefined) } },
      navigation: { openExternal: vi.fn() },
      theme: { getCurrent: vi.fn(async () => ({ theme: "light" })), subscribe: vi.fn(async () => ({ dispose: vi.fn() })) },
    } as unknown as ExtensionContext;
    return { ctx, request };
  }
  const flush = () => new Promise((resolve) => setTimeout(resolve, 20));

  it("loads the card state for an admin, hides what is off or hidden, and opens the panel from Cards and from a gear", async () => {
    document.body.innerHTML = '<div id="root"></div>';
    const { ctx, request } = context();
    await activateFabric(ctx);
    await flush();
    expect(request.mock.calls.some(([c]) => c.path === "/v1/ae/cards")).toBe(true);
    const cardsOnBoard = shown(document.querySelector<HTMLElement>(".admin-board")!);
    expect(cardsOnBoard).not.toContain("social");
    expect(cardsOnBoard).not.toContain("presets");
    const button = document.querySelector<HTMLButtonElement>(".admin-cards-btn")!;
    expect(button.textContent).toContain("Cards");
    expect(button.title.length).toBeGreaterThan(10);
    expect(button.title.length).toBeLessThanOrEqual(200);
    button.click();
    expect(document.querySelector(".ae-cards-drawer")).not.toBeNull();
    document.querySelector<HTMLButtonElement>('[aria-label="Close the Cards panel"]')!.click();
    document.querySelector<HTMLButtonElement>('[data-card="vault"] .admin-gear')!.click();
    await flush();
    const open = document.querySelector<HTMLElement>('.ae-cards-drawer [data-card="vault"] .ae-card-setup')!;
    expect(open.hidden).toBe(false);
  });

  it("tells a box admin that Box health is off instead of drawing the card", async () => {
    document.body.innerHTML = '<div id="root"></div>';
    const request = vi.fn(async ({ path }: { path: string }) =>
      path === "/v1/ae/access"
        ? { ok: true, status: 200, body: { admin: false, box_admin: true, box_on: false } }
        : { ok: false, status: 500, body: {} },
    );
    const ctx = {
      view: "fabric",
      server: { request },
      theme: { getCurrent: vi.fn(async () => ({ theme: "light" })), subscribe: vi.fn(async () => ({ dispose: vi.fn() })) },
    } as unknown as ExtensionContext;
    await activateFabric(ctx);
    expect(document.body.textContent).toContain("Box health is turned off on this server. Ask an admin to turn it back on.");
    expect(document.querySelector(".admin-card")).toBeNull();
    expect(request.mock.calls.every(([c]) => c.path === "/v1/ae/access")).toBe(true);
  });

  it("never calls /cards for a box admin", async () => {
    document.body.innerHTML = '<div id="root"></div>';
    const { ctx, request } = context({ admin: false });
    await activateFabric(ctx);
    await flush();
    expect(request.mock.calls.some(([c]) => c.path === "/v1/ae/cards")).toBe(false);
    expect(document.querySelector(".admin-cards-btn")).toBeNull();
  });
});
