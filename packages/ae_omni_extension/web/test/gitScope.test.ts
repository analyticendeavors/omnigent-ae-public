// The Git card's Company, Mine, All switch and the Mine tag (2026-11-08, card setup part 3).
// Every call is mocked; owners are made up.
import type { ExtensionContext } from "@omnigent/extension-sdk";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { GitOverview } from "../src/api";
import { resetAttention } from "../src/attentionStore";
import { activateGit, deactivateGit, gitSection } from "../src/git";
import { resetTiles } from "../src/tiles";
import { calm, NOW, pr, repo, TOTALS } from "./gitFixtures";

const sdk = vi.hoisted(() => ({ defineExtension: vi.fn() }));
vi.mock("@omnigent/extension-sdk", () => sdk);

const answer = (scope: string, repos: GitOverview["repos"]): GitOverview =>
  calm({ scope: scope as GitOverview["scope"], repos, totals: { ...TOTALS, repos: repos.length } });

function stub(stored?: { view?: unknown; scope?: unknown }) {
  const paths: string[] = [];
  const request = vi.fn(async ({ path }: { method: string; path: string }) => {
    paths.push(path);
    if (path.endsWith("scope=mine")) {
      return { ok: true, status: 200, body: answer("mine", [{ ...repo("octo-user/notes", { prs: [pr(5)] }), scope: "mine" }]) };
    }
    if (path.endsWith("scope=company")) {
      return { ok: true, status: 200, body: answer("company", [{ ...repo("example-org/web"), scope: "company" }]) };
    }
    return {
      ok: true,
      status: 200,
      body: answer("all", [
        { ...repo("example-org/web"), scope: "company" },
        { ...repo("octo-user/notes", { prs: [pr(5)] }), scope: "mine" },
      ]),
    };
  });
  const get = vi.fn(async (key: string) => (key === "git.scope" ? stored?.scope : stored?.view));
  const set = vi.fn(async () => undefined);
  const context = {
    view: "fabric",
    server: { request },
    navigation: { openExternal: vi.fn(async () => undefined) },
    storage: { user: { get, set } },
  } as unknown as ExtensionContext;
  return { context, paths, set };
}

const flush = () => vi.advanceTimersByTimeAsync(0);
async function mount(context: ExtensionContext): Promise<HTMLElement> {
  const node = gitSection();
  document.getElementById("root")?.appendChild(node);
  activateGit(context, node);
  await flush();
  return node;
}
const scopeBtn = (node: ParentNode, id: string) => node.querySelector<HTMLButtonElement>(`[data-scope="${id}"]`)!;

beforeEach(() => {
  resetTiles();
  resetAttention();
  document.body.replaceChildren();
  const root = document.createElement("div");
  root.id = "root";
  document.body.appendChild(root);
  vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout", "Date"] });
  vi.setSystemTime(NOW);
});
afterEach(() => {
  deactivateGit();
  resetAttention();
  vi.useRealTimers();
});

describe("the Company, Mine, All switch", () => {
  it("starts on All, with no query, and says which is on", async () => {
    const { context, paths } = stub();
    const node = await mount(context);
    expect(paths).toEqual(["/v1/ae/git"]);
    expect(["company", "mine", "all"].map((id) => scopeBtn(node, id).getAttribute("aria-pressed"))).toEqual([
      "false",
      "false",
      "true",
    ]);
  });

  it("reads the other list on a click, and remembers the choice per viewer", async () => {
    const { context, paths, set } = stub();
    const node = await mount(context);
    scopeBtn(node, "mine").click();
    await flush();
    expect(paths).toEqual(["/v1/ae/git", "/v1/ae/git?scope=mine"]);
    expect(set).toHaveBeenCalledWith("git.scope", "mine");
    expect(scopeBtn(node, "mine").getAttribute("aria-pressed")).toBe("true");
    scopeBtn(node, "company").click();
    await flush();
    expect(paths.at(-1)).toBe("/v1/ae/git?scope=company");
    expect(set).toHaveBeenLastCalledWith("git.scope", "company");
  });

  it("asks for the remembered scope on the first read, and ignores a stored value it does not know", async () => {
    const a = stub({ scope: "company" });
    await mount(a.context);
    expect(a.paths).toEqual(["/v1/ae/git?scope=company"]);
    deactivateGit();
    document.body.replaceChildren();
    document.body.appendChild(Object.assign(document.createElement("div"), { id: "root" }));
    const b = stub({ scope: "everyone" });
    await mount(b.context);
    expect(b.paths).toEqual(["/v1/ae/git"]);
  });
});

describe("personal rows", () => {
  it("carry a Mine tag, and the card offers links to GitHub only: no Merge, Fix or Review", async () => {
    const { context } = stub({ view: { open: true, chip: "all" } });
    const node = await mount(context);
    const tags = [...node.querySelectorAll<HTMLElement>(".git-tag-mine")];
    expect(tags.map((tag) => tag.textContent)).toEqual(["Mine"]);
    expect(tags[0]!.closest("[data-group]")?.getAttribute("data-group")).toBe("octo-user/notes");
    const buttons = [...node.querySelectorAll<HTMLButtonElement>("button")].map(
      (b) => `${b.textContent} ${b.getAttribute("aria-label") ?? ""}`,
    );
    expect(buttons.filter((label) => /Merge|Fix|Review/.test(label))).toEqual([]);
    expect(node.querySelector('[data-group="example-org/web"] .git-tag-mine')).toBeNull();
  });

  it("shows a plain sentence when the Mine list is empty", async () => {
    const request = vi.fn(async () => ({ ok: true, status: 200, body: calm({ configured: false, setup: null, repos: [] }) }));
    const context = {
      view: "fabric",
      server: { request },
      navigation: { openExternal: vi.fn() },
      storage: { user: { get: vi.fn(async (key: string) => (key === "git.scope" ? "mine" : undefined)), set: vi.fn(async () => undefined) } },
    } as unknown as ExtensionContext;
    const node = await mount(context);
    expect(node.textContent).toContain("You have no personal repos yet");
  });
});

describe("a read that starts before the remembered scope is known", () => {
  it("never draws All data under the remembered Mine tab", async () => {
    let releaseScope: (value: string) => void = () => undefined;
    const scopeKnown = new Promise<string>((resolve) => (releaseScope = resolve));
    let releaseAll: () => void = () => undefined;
    const allDone = new Promise<void>((resolve) => (releaseAll = resolve));
    const paths: string[] = [];
    const request = vi.fn(async ({ path }: { method: string; path: string }) => {
      paths.push(path);
      if (path.endsWith("scope=mine")) {
        return { ok: true, status: 200, body: answer("mine", [{ ...repo("octo-user/notes", { prs: [pr(5)] }), scope: "mine" }]) };
      }
      await allDone;
      return {
        ok: true,
        status: 200,
        body: answer("all", [
          { ...repo("example-org/web"), scope: "company" },
          { ...repo("octo-user/notes", { prs: [pr(5)] }), scope: "mine" },
        ]),
      };
    });
    const context = {
      view: "fabric",
      server: { request },
      navigation: { openExternal: vi.fn() },
      storage: {
        user: {
          get: vi.fn(async (key: string) => (key === "git.scope" ? scopeKnown : { open: true, chip: "all" })),
          set: vi.fn(async () => undefined),
        },
      },
    } as unknown as ExtensionContext;
    const node = gitSection();
    document.getElementById("root")?.appendChild(node);
    activateGit(context, node);
    await flush();
    // A focus read starts on the default scope while the stored one is still loading.
    window.dispatchEvent(new Event("focus"));
    await flush();
    expect(paths).toEqual(["/v1/ae/git"]);
    releaseScope("mine");
    await flush();
    releaseAll();
    await flush();
    await flush();
    expect(paths).toEqual(["/v1/ae/git", "/v1/ae/git?scope=mine"]);
    expect(scopeBtn(node, "mine").getAttribute("aria-pressed")).toBe("true");
    expect(node.querySelector('[data-group="example-org/web"]')).toBeNull();
    expect(node.querySelector('[data-group="octo-user/notes"]')).not.toBeNull();
  });
});
