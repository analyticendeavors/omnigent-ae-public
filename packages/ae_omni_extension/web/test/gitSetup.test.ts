// The Git entry of the Cards panel (2026-11-08, card setup part 3): company tokens per account,
// other admins' counts, "My GitHub accounts", the token input wipe, and the no-key text.
// Every call is mocked; names are example-org, octo-user and owner@example.com.
import type { ExtensionContext } from "@omnigent/extension-sdk";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { type CardsAnswer, resetCardState } from "../src/cardState";
import { type OpenCardsPanel, openCardsPanel } from "../src/cardsPanel";
import { EXPIRY_WARN_DAYS, KEY_NAME, NO_KEY_TEXT, TOKEN_HINT, expirySoon } from "../src/gitSetup";
import type { CompanyTokenOwner, MineAnswer, UsersAnswer } from "../src/gitSetupApi";

const TOKEN = `github_pat_${"A1b2C3d4".repeat(6)}`;
const soon = new Date(Date.now() + 5 * 86_400_000).toISOString();
const later = new Date(Date.now() + 90 * 86_400_000).toISOString();

function cards(owners: CompanyTokenOwner[]): CardsAnswer {
  return {
    revision: 3,
    state_ok: true,
    cards: [
      {
        id: "git",
        name: "Git repos",
        group: "ship",
        description: "Repos across your GitHub accounts",
        sources: ["git", "git-mine"],
        on: true,
        default_on: true,
        changed_by: null,
        changed_at: null,
        fields: [
          {
            label: "Token for one account",
            env: "AE_GIT_TOKEN_",
            kind: "secret",
            hint: "One token per GitHub account.",
            env_only: false,
            status: "set",
            owners,
          } as CardsAnswer["cards"][number]["fields"][number],
        ],
      },
    ],
  };
}

const OWNERS: CompanyTokenOwner[] = [
  { owner: "example-org", env_name: "AE_GIT_TOKEN_EXAMPLE_ORG", source: "env" },
  {
    owner: "fabrikam-labs",
    env_name: "AE_GIT_TOKEN_FABRIKAM_LABS",
    source: "saved",
    saved_by: "owner@example.com",
    saved_at: "2026-11-07T10:00:00Z",
    github_login: "octo-user",
    expires_at: soon,
  },
  { owner: "contoso", env_name: "AE_GIT_TOKEN_CONTOSO", source: "missing" },
];

const MINE = (over: Partial<MineAnswer> = {}): MineAnswer => ({
  key_configured: true,
  revision: 7,
  accounts: [
    { account: "octo-user", github_login: "octo-user", saved_at: "2026-11-07T10:00:00Z", expires_at: later, repos: 6 },
  ],
  repos: [],
  ...over,
});
const USERS: UsersAnswer = { users: [{ login: "other@example.com", accounts: 1, repos: 1 }] };

interface Call {
  method: string;
  path: string;
  body?: unknown;
}
type Reply = { ok: boolean; status: number; body: unknown };
const ok = (body: unknown): Reply => ({ ok: true, status: 200, body });

function setup(routes: Record<string, Reply | ((call: Call) => Reply)>, owners = OWNERS) {
  const calls: Call[] = [];
  const base: Record<string, Reply | ((call: Call) => Reply)> = {
    "GET /v1/ae/cards": ok(cards(owners)),
    "GET /v1/ae/git/mine": ok(MINE()),
    "GET /v1/ae/git/users": ok(USERS),
    ...routes,
  };
  const request = vi.fn(async (call: Call) => {
    calls.push(call);
    const route = base[`${call.method} ${call.path}`];
    if (!route) return { ok: false, status: 500, body: { error: { code: "x", message: `unexpected ${call.method} ${call.path}` } } };
    return typeof route === "function" ? route(call) : route;
  });
  const set = vi.fn(async () => undefined);
  const openExternal = vi.fn(async () => undefined);
  const context = {
    server: { request },
    storage: { user: { get: vi.fn(async () => undefined), set } },
    navigation: { openExternal },
  } as unknown as ExtensionContext;
  return { context, calls, set, openExternal };
}

const opened: OpenCardsPanel[] = [];
async function open(context: ExtensionContext) {
  opened.push(openCardsPanel(context, { focus: "git" }));
  await flush();
}
const flush = async () => {
  for (let n = 0; n < 6; n += 1) await new Promise((resolve) => setTimeout(resolve, 0));
};
const root = () => document.querySelector<HTMLElement>("[data-git-setup]")!;
const byText = (label: string) =>
  [...root().querySelectorAll<HTMLButtonElement>("button")].find((b) => b.textContent === label)!;
const owner = (name: string) => root().querySelector<HTMLElement>(`[data-owner="${name}"]`)!;
const tokenInput = () => root().querySelector<HTMLInputElement>("input.ae-git-token")!;
function type(input: HTMLInputElement, value: string) {
  input.value = value;
  input.dispatchEvent(new Event("input", { bubbles: true }));
}

beforeEach(() => {
  resetCardState();
  document.body.replaceChildren();
});
afterEach(() => {
  for (const panel of opened.splice(0)) panel.close();
  document.body.replaceChildren();
});

describe("company tokens", () => {
  it("lists each owner: environment (read only), saved (Replace, Clear) and not set (Add)", async () => {
    const { context } = setup({});
    await open(context);
    expect(owner("example-org").textContent).toContain("Token from the server's environment, AE_GIT_TOKEN_EXAMPLE_ORG");
    expect(owner("example-org").querySelector("button")).toBeNull();
    const saved = owner("fabrikam-labs");
    expect(saved.textContent).toContain("Saved by owner@example.com on");
    expect(saved.textContent).toContain("expires");
    expect([...saved.querySelectorAll("button")].map((b) => b.textContent)).toEqual(["Replace", "Clear"]);
    expect(saved.querySelector('.ae-git-expiry[data-tone="warn"]')).not.toBeNull();
    expect(owner("contoso").textContent).toContain("Not set");
    expect([...owner("contoso").querySelectorAll("button")].map((b) => b.textContent)).toEqual(["Add"]);
  });

  it("warns on an expiry within 14 days, and not beyond", () => {
    expect(EXPIRY_WARN_DAYS).toBe(14);
    expect(expirySoon(soon)).toBe(true);
    expect(expirySoon(later)).toBe(false);
    expect(expirySoon(null)).toBe(false);
  });

  it("adds a token in a password input, posts it, and wipes the input even when the answer is an error", async () => {
    const { context, calls, set } = setup({
      "POST /v1/ae/git/company/token": () => ({
        ok: false,
        status: 422,
        // A server that echoed the token back must not get it onto the screen.
        body: { error: { code: "x", message: `GitHub refused ${TOKEN}` } },
      }),
    });
    await open(context);
    byText("Add").click();
    const input = tokenInput();
    expect(input.type).toBe("password");
    expect(input.getAttribute("autocomplete")).toBe("off");
    expect(input.getAttribute("spellcheck")).toBe("false");
    type(input, TOKEN);
    byText("Save token").click();
    await flush();
    const post = calls.find((c) => c.path === "/v1/ae/git/company/token")!;
    expect(post.method).toBe("POST");
    expect(post.body).toEqual({ owner: "contoso", token: TOKEN });
    expect(document.querySelector("input.ae-git-token")).toBeNull();
    expect(document.body.innerHTML).not.toContain(TOKEN);
    expect(document.body.textContent).toContain("[token]");
    expect(JSON.stringify(set.mock.calls)).not.toContain(TOKEN);
  });

  it("reads the cards again after a company token is saved", async () => {
    const { context, calls } = setup({ "POST /v1/ae/git/company/token": ok({}) });
    await open(context);
    const before = calls.filter((c) => c.path === "/v1/ae/cards").length;
    byText("Add").click();
    type(tokenInput(), TOKEN);
    byText("Save token").click();
    await flush();
    expect(calls.filter((c) => c.path === "/v1/ae/cards").length).toBe(before + 1);
    expect(document.body.innerHTML).not.toContain(TOKEN);
  });

  it("Clear asks first, then posts the owner", async () => {
    const { context, calls } = setup({ "POST /v1/ae/git/company/clear": ok({}) });
    await open(context);
    owner("fabrikam-labs").querySelectorAll("button")[1]!.click();
    expect(calls.some((c) => c.path === "/v1/ae/git/company/clear")).toBe(false);
    const confirm = [...document.querySelectorAll<HTMLButtonElement>(".ae-dialog button")].find((b) => b.textContent === "Clear token")!;
    confirm.click();
    await flush();
    expect(calls.find((c) => c.path === "/v1/ae/git/company/clear")!.body).toEqual({ owner: "fabrikam-labs" });
  });
});

describe("without AE_CARD_SECRETS_KEY", () => {
  it("replaces the inputs with the sentence naming the variable", async () => {
    const { context } = setup({ "GET /v1/ae/git/mine": ok(MINE({ key_configured: false, accounts: [] })) });
    await open(context);
    expect(NO_KEY_TEXT).toBe(`Set ${KEY_NAME} in the server's environment to save tokens here.`);
    expect(root().textContent).toContain(NO_KEY_TEXT);
    expect(byText("Connect another account")).toBeUndefined();
    expect(byText("Add").disabled).toBe(true);
    expect(root().querySelector("input.ae-git-token")).toBeNull();
  });
});

describe("other users", () => {
  it("shows counts only and clears a user's tokens behind a confirm", async () => {
    const { context, calls } = setup({ "POST /v1/ae/git/users/clear": ok({ users: [] }) });
    await open(context);
    expect(root().textContent).toContain("Other users: 1 has 1 personal account connected.");
    expect(root().textContent).not.toMatch(/example-org\/|octo-user\/notes/);
    byText("Clear their tokens").click();
    expect(calls.some((c) => c.path === "/v1/ae/git/users/clear")).toBe(false);
    const confirm = [...document.querySelectorAll<HTMLButtonElement>(".ae-dialog button")].find((b) => b.textContent === "Clear their tokens")!;
    confirm.click();
    await flush();
    expect(calls.find((c) => c.path === "/v1/ae/git/users/clear")!.body).toEqual({ login: "other@example.com" });
    expect(root().textContent).toContain("nobody else has a personal account connected");
  });
});

describe("My GitHub accounts", () => {
  const CHECK = {
    github_login: "octo-user",
    expires_at: later,
    repos: [{ repo: "octo-user/notes", private: true }, { repo: "octo-user/site" }, { repo: "example-org/tools" }],
    already: [],
  };

  it("lists each account with its date, expiry and repo count, and the helper text", async () => {
    const { context, openExternal } = setup({});
    await open(context);
    const account = root().querySelector<HTMLElement>('[data-account="octo-user"]')!;
    expect(account.textContent).toMatch(/Saved .*expires .*6 repos/);
    expect([...account.querySelectorAll("button")].map((b) => b.textContent)).toEqual(["Replace", "Remove"]);
    expect(root().textContent).toContain("Only you see these. Read with your own token; never with the company's.");
    byText("Connect another account").click();
    expect(root().textContent).toContain(TOKEN_HINT);
    const link = root().querySelector<HTMLAnchorElement>("a.ae-git-link")!;
    expect(link.textContent).toBe("Make one on GitHub");
    expect(link.getAttribute("href")).toBe("https://github.com/settings/personal-access-tokens/new");
    link.click();
    expect(openExternal).toHaveBeenCalledWith("https://github.com/settings/personal-access-tokens/new");
  });

  it("checks, lists the repos, saves the ticked ones, and wipes the token after the save answers", async () => {
    const saved = MINE({
      revision: 8,
      accounts: [
        { account: "octo-user", github_login: "octo-user", saved_at: "2026-11-08T10:00:00Z", expires_at: later, repos: 2 },
      ],
    });
    const { context, calls, set } = setup({
      "POST /v1/ae/git/mine/check": ok(CHECK),
      "POST /v1/ae/git/mine/save": ok(saved),
    });
    await open(context);
    byText("Connect another account").click();
    const input = tokenInput();
    expect(input.type).toBe("password");
    expect(input.getAttribute("autocomplete")).toBe("off");
    type(input, TOKEN);
    byText("Check").click();
    await flush();
    expect(calls.find((c) => c.path === "/v1/ae/git/mine/check")!.body).toEqual({ token: TOKEN });
    expect(root().textContent).toContain("Signed in as octo-user. 3 repos visible.");
    const boxes = [...root().querySelectorAll<HTMLInputElement>("input.ae-git-pick")];
    expect(boxes.map((b) => b.dataset.repo)).toEqual(["octo-user/notes", "octo-user/site", "example-org/tools"]);
    expect(byText("Save 0 repos")).toBeDefined();
    boxes[0]!.checked = true;
    boxes[0]!.dispatchEvent(new Event("change", { bubbles: true }));
    boxes[1]!.checked = true;
    boxes[1]!.dispatchEvent(new Event("change", { bubbles: true }));
    expect(byText("Save 2 repos")).toBeDefined();
    byText("Save 2 repos").click();
    await flush();
    const post = calls.find((c) => c.path === "/v1/ae/git/mine/save")!;
    expect(post.body).toEqual({ token: TOKEN, repos: ["octo-user/notes", "octo-user/site"], revision: 7 });
    expect(document.querySelector("input.ae-git-token")).toBeNull();
    expect(document.body.innerHTML).not.toContain(TOKEN);
    expect(JSON.stringify(set.mock.calls)).not.toContain(TOKEN);
    expect(root().querySelector('[data-account="octo-user"]')!.textContent).toContain("2 repos");
  });

  it("Cancel throws the token away without saving", async () => {
    const { context, calls } = setup({ "POST /v1/ae/git/mine/check": ok(CHECK) });
    await open(context);
    byText("Connect another account").click();
    type(tokenInput(), TOKEN);
    byText("Check").click();
    await flush();
    byText("Cancel").click();
    expect(document.querySelector("input.ae-git-token")).toBeNull();
    expect(document.body.innerHTML).not.toContain(TOKEN);
    expect(calls.some((c) => c.path === "/v1/ae/git/mine/save")).toBe(false);
  });

  it("a refused check keeps the form, names the problem, and leaves the token out of the text", async () => {
    const { context } = setup({
      "POST /v1/ae/git/mine/check": () => ({
        ok: false,
        status: 422,
        body: { error: { code: "token_refused", message: `GitHub refused this token ${TOKEN}` } },
      }),
    });
    await open(context);
    byText("Connect another account").click();
    type(tokenInput(), TOKEN);
    byText("Check").click();
    await flush();
    expect(root().querySelector('[role="alert"]')!.textContent).toContain("GitHub refused this token");
    expect(root().textContent).not.toContain(TOKEN);
  });

  it("a stale revision wipes the token, shows the current list and says to paste again", async () => {
    const current = MINE({ revision: 9, accounts: [] });
    const { context } = setup({
      "POST /v1/ae/git/mine/check": ok(CHECK),
      "POST /v1/ae/git/mine/save": () => ({
        ok: false,
        status: 409,
        body: { error: { code: "stale_revision", message: "changed", current } },
      }),
    });
    await open(context);
    byText("Connect another account").click();
    type(tokenInput(), TOKEN);
    byText("Check").click();
    await flush();
    byText("Save 0 repos").click();
    await flush();
    expect(document.body.innerHTML).not.toContain(TOKEN);
    expect(root().textContent).toContain("paste the token again");
    expect(root().querySelector('[data-account="octo-user"]')).toBeNull();
  });

  it("Remove asks first, then posts the account", async () => {
    const { context, calls } = setup({ "POST /v1/ae/git/mine/clear": ok(MINE({ accounts: [] })) });
    await open(context);
    byText("Remove").click();
    expect(calls.some((c) => c.path === "/v1/ae/git/mine/clear")).toBe(false);
    [...document.querySelectorAll<HTMLButtonElement>(".ae-dialog button")].find((b) => b.textContent === "Remove")!.click();
    await flush();
    expect(calls.find((c) => c.path === "/v1/ae/git/mine/clear")!.body).toEqual({ account: "octo-user" });
    expect(root().querySelector('[data-account="octo-user"]')).toBeNull();
  });
});

describe("the panel closing", () => {
  it("wipes a typed token", async () => {
    const { context } = setup({});
    opened.push(openCardsPanel(context, { focus: "git" }));
    await flush();
    byText("Connect another account").click();
    const input = tokenInput();
    type(input, TOKEN);
    opened.pop()!.close();
    expect(input.value).toBe("");
  });
});
