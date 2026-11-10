// The card state module (2026-11-07, card setup): no DOM, shared by the board and the panel.
import { beforeEach, describe, expect, it, vi } from "vitest";
import {
  type CardsAnswer,
  cardGone,
  cardHidden,
  cardOff,
  onCardState,
  resetCardState,
  setCards,
  setHidden,
} from "../src/cardState";

const row = (id: string, on: boolean) => ({
  id,
  name: id,
  group: "ship" as const,
  description: "d",
  sources: [] as string[],
  on,
  default_on: true,
  changed_by: null,
  changed_at: null,
  fields: [],
});
const answer = (): CardsAnswer => ({
  revision: 2,
  state_ok: true,
  cards: [row("git", false), row("vault", true)],
});

beforeEach(() => resetCardState());

describe("card state", () => {
  it("shows everything until the server has answered", () => {
    expect(cardOff("git")).toBe(false);
    expect(cardGone("git")).toBe(false);
  });

  it("treats an off card, and a card the server does not list, as off", () => {
    setCards(answer());
    expect(cardOff("git")).toBe(true);
    expect(cardOff("vault")).toBe(false);
    expect(cardOff("social")).toBe(true);
  });

  it("hides for this viewer only and gone means off or hidden", () => {
    setCards(answer());
    setHidden(["vault"]);
    expect(cardHidden("vault")).toBe(true);
    expect(cardOff("vault")).toBe(false);
    expect(cardGone("vault")).toBe(true);
    expect(cardGone("git")).toBe(true);
  });

  it("tells listeners after every change and stops after the returned function", () => {
    const listener = vi.fn();
    const stop = onCardState(listener);
    setCards(answer());
    setHidden(["git"]);
    expect(listener).toHaveBeenCalledTimes(2);
    stop();
    setHidden([]);
    expect(listener).toHaveBeenCalledTimes(2);
  });
});

describe("the off body", () => {
  it("is read as off only with off true and a card id", async () => {
    const { fetchSocial } = await import("../src/api");
    const ctx = (body: unknown) =>
      ({ server: { request: async () => ({ ok: true, status: 200, body }) } }) as never;
    await expect(fetchSocial(ctx({ off: true, card: "social" }))).rejects.toMatchObject({ code: "admin_only" });
    // A route's own `off` field, with no card id, is data, not the off state.
    await expect(fetchSocial(ctx({ off: true }))).resolves.toEqual({ off: true });
  });
});
