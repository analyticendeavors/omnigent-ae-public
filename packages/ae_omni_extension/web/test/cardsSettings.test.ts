// Settings typed in the Cards panel (2026-11-07, card setup PR 2): an input per kind, one Save
// that posts only the changed fields with the revision, Clear, the 409 redraw, the 422 text.
import type { ExtensionContext } from "@omnigent/extension-sdk";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { type CardField, type CardsAnswer, getCards, resetCardState, setCards } from "../src/cardState";
import { type OpenCardsPanel, openCardsPanel } from "../src/cardsPanel";

const field = (env: string, kind: CardField["kind"], over: Partial<CardField> = {}): CardField => ({
  label: `Label of ${env}`,
  env,
  kind,
  hint: "A plain hint.",
  env_only: false,
  status: "missing",
  editable: true,
  source: "missing",
  host: false,
  default: null,
  saved_by: null,
  saved_at: null,
  value: null,
  count: null,
  unit: null,
  ...over,
});

function answer(fields: CardField[], over: Partial<CardsAnswer> = {}): CardsAnswer {
  return {
    revision: 4,
    state_ok: true,
    cards: [
      {
        id: "deploys",
        name: "Name deploys",
        group: "ship",
        description: "About deploys",
        sources: [],
        on: true,
        default_on: true,
        changed_by: null,
        changed_at: null,
        fields,
      },
    ],
    ...over,
  };
}

const FIELDS = (): CardField[] => [
  field("T_TEXT", "text"),
  field("T_LIST", "list"),
  field("T_NUM", "number", { default: "0.18", source: "default" }),
  field("T_DATE", "date"),
  field("T_BOOL", "bool"),
];

function context(request: ReturnType<typeof vi.fn>) {
  return {
    server: { request },
    storage: { user: { get: vi.fn().mockResolvedValue(undefined), set: vi.fn().mockResolvedValue(undefined) } },
    navigation: { openExternal: vi.fn().mockResolvedValue(undefined) },
  } as unknown as ExtensionContext;
}

const opened: OpenCardsPanel[] = [];
function open(request: ReturnType<typeof vi.fn> = vi.fn()) {
  const panel = openCardsPanel(context(request), { focus: "deploys" });
  opened.push(panel);
  return panel;
}
const flush = () => new Promise((resolve) => setTimeout(resolve, 0));
const input = (env: string) => document.querySelector<HTMLInputElement>(`[data-env="${env}"]`)!;
const save = () => document.querySelector<HTMLButtonElement>(".ae-card-save")!;
function type(env: string, value: string) {
  const node = input(env);
  node.value = value;
  node.dispatchEvent(new Event("input", { bubbles: true }));
}

beforeEach(() => {
  resetCardState();
  document.body.replaceChildren();
});
afterEach(() => {
  for (const panel of opened.splice(0)) panel.close();
  document.body.replaceChildren();
});

describe("settings typed in the Cards panel", () => {
  it("shows an input for each kind", () => {
    setCards(answer(FIELDS()));
    open();
    expect(input("T_TEXT").tagName).toBe("INPUT");
    expect(input("T_TEXT").type).toBe("text");
    expect(input("T_LIST").tagName).toBe("TEXTAREA");
    expect(input("T_NUM").type).toBe("number");
    expect(input("T_DATE").type).toBe("date");
    expect(input("T_BOOL").type).toBe("checkbox");
    expect(input("T_NUM").placeholder).toBe("0.18");
  });

  it("saves only the changed fields, with the revision, and clears the draft", async () => {
    setCards(answer(FIELDS()));
    const next = answer(
      [field("T_TEXT", "text", { source: "saved", value: "abc", status: "set", saved_by: "owner@example.com", saved_at: "2026-11-07T10:00:00Z" }), ...FIELDS().slice(1)],
      { revision: 5 },
    );
    const request = vi
      .fn()
      .mockResolvedValueOnce({ ok: true, status: 200, body: answer(FIELDS()) })
      .mockResolvedValue({ ok: true, status: 200, body: next });
    open(request);
    await flush();
    expect(save().disabled).toBe(true);
    type("T_TEXT", "abc");
    type("T_BOOL", "x");
    input("T_BOOL").checked = true;
    input("T_BOOL").dispatchEvent(new Event("change", { bubbles: true }));
    expect(save().disabled).toBe(false);
    save().click();
    await flush();
    expect(request).toHaveBeenLastCalledWith({
      method: "POST",
      path: "/v1/ae/cards/settings",
      body: { id: "deploys", values: { T_TEXT: "abc", T_BOOL: "1" }, revision: 4 },
    });
    expect(getCards()!.revision).toBe(5);
    expect(document.querySelector('[data-field="T_TEXT"]')!.textContent).toContain(
      "Saved by owner@example.com on",
    );
  });

  it("sends null for a field emptied, and Clear sends null for a saved value", async () => {
    const saved = [field("T_TEXT", "text", { source: "saved", value: "abc", status: "set", saved_by: "a@example.com" })];
    setCards(answer(saved));
    const request = vi
      .fn()
      .mockResolvedValueOnce({ ok: true, status: 200, body: answer(saved) })
      .mockResolvedValue({ ok: true, status: 200, body: answer([field("T_TEXT", "text")], { revision: 5 }) });
    open(request);
    await flush();
    document.querySelector<HTMLButtonElement>(".ae-card-clear")!.click();
    await flush();
    expect(request).toHaveBeenLastCalledWith({
      method: "POST",
      path: "/v1/ae/cards/settings",
      body: { id: "deploys", values: { T_TEXT: null }, revision: 4 },
    });
    expect(input("T_TEXT").value).toBe("");
    expect(document.querySelector(".ae-card-clear")).toBeNull();
  });

  it("shows a 422 next to the field it names and keeps what was typed", async () => {
    setCards(answer(FIELDS()));
    const request = vi
      .fn()
      .mockResolvedValueOnce({ ok: true, status: 200, body: answer(FIELDS()) })
      .mockResolvedValue({
        ok: false,
        status: 422,
        body: { error: { code: "invalid_setting", message: "T_TEXT must be a plain number" } },
      });
    open(request);
    await flush();
    type("T_TEXT", "abc");
    save().click();
    await flush();
    const error = document.querySelector<HTMLElement>('[data-field="T_TEXT"] .ae-card-error')!;
    expect(error.hidden).toBe(false);
    expect(error.textContent).toContain("must be a plain number");
    expect(input("T_TEXT").value).toBe("abc");
    type("T_TEXT", "abcd");
    expect(error.hidden).toBe(true);
  });

  it("redraws from a 409 conflict and keeps what was typed", async () => {
    setCards(answer(FIELDS()));
    const current = answer([field("T_TEXT", "text", { source: "saved", value: "elsewhere", status: "set" }), ...FIELDS().slice(1)], { revision: 9 });
    const request = vi
      .fn()
      .mockResolvedValueOnce({ ok: true, status: 200, body: answer(FIELDS()) })
      .mockResolvedValue({ ok: false, status: 409, body: { error: { code: "cards_changed_elsewhere", message: "m", current } } });
    open(request);
    await flush();
    type("T_DATE", "2026-01-02");
    save().click();
    await flush();
    expect(getCards()!.revision).toBe(9);
    expect(document.querySelector(".ae-cards-note")!.textContent).toContain("changed elsewhere");
    expect(input("T_TEXT").value).toBe("elsewhere");
    expect(input("T_DATE").value).toBe("2026-01-02");
  });

  it("shows an environment-set field read only, a count for a prefix and the host note", () => {
    setCards(
      answer([
        field("T_ENV", "text", { source: "env", status: "set" }),
        field("T_TOKEN_", "secret", { editable: false, env_only: true, source: "env", status: "set", count: 3, unit: "account" }),
        field("T_ONE_", "secret", { editable: false, env_only: true, source: "env", status: "set", count: 1, unit: "capacity" }),
        field("T_HOST", "text", { editable: false, host: true }),
        field("T_PRICE", "text", { editable: false, source: "default", default: "https://api.example.test" }),
      ]),
    );
    open();
    expect(document.querySelector('[data-env="T_ENV"]')).toBeNull();
    const text = (env: string) => document.querySelector(`[data-field="${env}"]`)!.textContent;
    expect(text("T_ENV")).toContain("Set in the server's environment. Change it there.");
    expect(text("T_TOKEN_")).toContain("Set for 3 accounts");
    expect(text("T_ONE_")).toContain("Set for 1 capacity");
    expect(text("T_HOST")).toContain("Set on the host, in config/host.env");
    expect(text("T_PRICE")).toContain("Default: https://api.example.test");
    expect(document.querySelector(".ae-card-save")).toBeNull();
  });

  it("disables every input and Save when the saved card file cannot be read", () => {
    setCards(answer(FIELDS(), { state_ok: false }));
    open();
    for (const env of ["T_TEXT", "T_LIST", "T_NUM", "T_DATE", "T_BOOL"]) expect(input(env).disabled).toBe(true);
    expect(save().disabled).toBe(true);
    expect(document.body.textContent).toContain("Saving is paused");
  });

  it("gives every control and every field a label or hint", () => {
    setCards(answer(FIELDS()));
    open();
    for (const env of ["T_TEXT", "T_LIST", "T_NUM", "T_DATE", "T_BOOL"]) {
      expect(input(env).getAttribute("aria-label")).toBe(`Label of ${env}`);
      expect(document.querySelector(`[data-field="${env}"] .ae-hint-btn`)).not.toBeNull();
    }
    expect(save().textContent).toBe("Save");
    expect(save().title).not.toBe("");
  });
});
