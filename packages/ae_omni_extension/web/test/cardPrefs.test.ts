// "Hide for me" (2026-11-07, card setup): remembered in storage.user under cards.hidden.
import type { ExtensionContext } from "@omnigent/extension-sdk";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { HIDDEN_KEY, loadCardPrefs, setCardHidden } from "../src/cardPrefs";
import { cardHidden, resetCardState } from "../src/cardState";

function context(initial?: unknown) {
  const get = vi.fn().mockResolvedValue(initial);
  const set = vi.fn().mockResolvedValue(undefined);
  return { ctx: { storage: { user: { get, set } } } as unknown as ExtensionContext, get, set };
}

beforeEach(() => resetCardState());

describe("card prefs", () => {
  it("uses the documented key", () => expect(HIDDEN_KEY).toBe("cards.hidden"));

  it("reads the remembered ids and ignores anything that is not a string", async () => {
    const { ctx, get } = context(["git", 5, "vault"]);
    await loadCardPrefs(ctx);
    expect(get).toHaveBeenCalledWith("cards.hidden");
    expect(cardHidden("git")).toBe(true);
    expect(cardHidden("vault")).toBe(true);
    expect(cardHidden("5")).toBe(false);
  });

  it("starts empty when the value is not a list or storage fails", async () => {
    await loadCardPrefs(context({ git: true }).ctx);
    expect(cardHidden("git")).toBe(false);
    const broken = { storage: { user: { get: () => Promise.reject(new Error("no")) } } };
    await loadCardPrefs(broken as unknown as ExtensionContext);
    expect(cardHidden("git")).toBe(false);
  });

  it("writes the array when a card is hidden and again when it is shown", () => {
    const { ctx, set } = context();
    setCardHidden(ctx, "git", true);
    setCardHidden(ctx, "vault", true);
    expect(set).toHaveBeenLastCalledWith("cards.hidden", ["git", "vault"]);
    setCardHidden(ctx, "git", false);
    expect(set).toHaveBeenLastCalledWith("cards.hidden", ["vault"]);
    expect(cardHidden("git")).toBe(false);
  });

  it("keeps the choice in memory when storage refuses", () => {
    const set = vi.fn(() => {
      throw new Error("no storage");
    });
    const ctx = { storage: { user: { set } } } as unknown as ExtensionContext;
    setCardHidden(ctx, "git", true);
    expect(cardHidden("git")).toBe(true);
  });
});
