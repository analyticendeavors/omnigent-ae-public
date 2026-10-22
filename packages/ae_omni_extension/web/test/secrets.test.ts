// The Admin Center's Secrets section (2026-10-20): each key the server
// expects, with whether it has it, names only. Every call is mocked: the
// route is GET /v1/ae/secrets.
import type { ExtensionContext } from "@omnigent/extension-sdk";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { SecretsStatus } from "../src/api";
import {
  activateSecrets,
  card,
  deactivateSecrets,
  keyState,
  metaItems,
  noticeTexts,
  pillText,
  secretsSection,
  toneOf,
} from "../src/secrets";

const sdk = vi.hoisted(() => ({ defineExtension: vi.fn() }));
vi.mock("@omnigent/extension-sdk", () => sdk);

type Reply = { ok: boolean; status: number; body: unknown };

const GET = "GET /v1/ae/secrets";
const NOW = Date.parse("2026-10-20T18:05:48Z");

const REQUIRED: Array<[string, string]> = [
  ["POSTGRES_PASSWORD", "Database"],
  ["OMNIGENT_OIDC_COOKIE_SECRET", "Microsoft sign-in"],
];
const OPTIONAL: Array<[string, string]> = [
  ["AE_GITHUB_TOKEN", "PR cards and Merge"],
  ["AE_DATABRICKS_CLIENT_ID", "Databricks section"],
];

/** Every key present except those named in *off*. */
function status(off: string[] = []): SecretsStatus {
  const rows = (keys: Array<[string, string]>) =>
    keys.map(([key, use]) => ({ key, use, present: !off.includes(key) }));
  return {
    required: rows(REQUIRED),
    optional: rows(OPTIONAL),
    missing: REQUIRED.map(([key]) => key).filter((key) => off.includes(key)),
  };
}

const ok = (body: unknown): Reply => ({ ok: true, status: 200, body });
const failed = (code: number, message: string): Reply => ({
  ok: false,
  status: code,
  body: { error: { code: "x", message } },
});

type Call = { method: string; path: string };

function stubContext(replies: Reply[]) {
  const request = vi.fn(async ({ method, path }: Call) => {
    if (`${method} ${path}` !== GET) return failed(500, `unexpected ${method} ${path}`);
    return replies.shift() ?? failed(500, "no reply queued");
  });
  const context = { view: "fabric", server: { request } } as unknown as ExtensionContext;
  return { context, request };
}

const flush = () => vi.advanceTimersByTimeAsync(0);

async function mount(context: ExtensionContext): Promise<HTMLElement> {
  const node = secretsSection();
  document.getElementById("root")?.appendChild(node);
  activateSecrets(context, node);
  await flush();
  return node;
}

const pill = (node: ParentNode) => node.querySelector(".fabric-state")?.textContent;
const notices = (node: ParentNode) =>
  [...node.querySelectorAll(".notice")].map((item) => item.textContent);
const chipOf = (node: ParentNode, use: string) =>
  node.querySelector<HTMLElement>(`.secret-key[data-use="${use}"]`);

beforeEach(() => {
  document.body.replaceChildren();
  const root = document.createElement("div");
  root.id = "root";
  document.body.appendChild(root);
  vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout", "Date"] });
  vi.setSystemTime(NOW);
});

afterEach(() => {
  deactivateSecrets();
  vi.useRealTimers();
  vi.restoreAllMocks();
});

describe("the words", () => {
  it("turns red only for a missing required key", () => {
    expect(toneOf(status())).toBe("on");
    expect(toneOf(status(["AE_GITHUB_TOKEN"]))).toBe("on");
    expect(toneOf(status(["POSTGRES_PASSWORD"]))).toBe("failed");
    expect(pillText(status())).toBe("All required present");
    expect(pillText(status(["POSTGRES_PASSWORD", "OMNIGENT_OIDC_COOKIE_SECRET"]))).toBe(
      "2 required missing",
    );
  });

  it("counts each group", () => {
    expect(metaItems(status(["OMNIGENT_OIDC_COOKIE_SECRET", "AE_GITHUB_TOKEN"]))).toEqual([
      "1 of 2 required",
      "1 of 2 optional",
      "As of the server's last start",
    ]);
  });

  it("calls an absent optional key not set, not missing", () => {
    const row = { key: "K", use: "u", present: false };
    expect(keyState(row, true)).toBe("Missing");
    expect(keyState(row, false)).toBe("Not set");
    expect(keyState({ ...row, present: true }, true)).toBe("Present");
  });

  it("says how to fix a missing required key and where optional ones come from", () => {
    expect(noticeTexts(status())).toEqual([]);
    const both = noticeTexts(status(["OMNIGENT_OIDC_COOKIE_SECRET", "AE_GITHUB_TOKEN"]));
    expect(both.map((item) => item.kind)).toEqual(["error", "info"]);
    expect(both[0]?.text).toMatch(/^The server started without OMNIGENT_OIDC_COOKIE_SECRET\./);
    expect(both[0]?.text).toMatch(/deploy\/pull-secrets\.sh/);
    expect(both[1]?.text).toMatch(/deploy\/README\.md/);
  });
});

describe("the section", () => {
  it("lists every key with its state and use", async () => {
    const { context } = stubContext([ok(status())]);
    const node = await mount(context);
    expect(pill(node)).toBe("All required present");
    expect(node.querySelector(".card-fabric")?.classList.contains("fabric-on")).toBe(true);
    const keys = [...node.querySelectorAll(".secret-name")].map((item) => item.textContent);
    expect(keys).toEqual([...REQUIRED, ...OPTIONAL].map(([key]) => key));
    expect(chipOf(node, "PR cards and Merge")?.querySelector(".secret-use")?.textContent).toBe(
      "PR cards and Merge",
    );
    expect(notices(node)).toEqual([]);
    expect(node.querySelector(".secrets-refreshed")?.textContent).toMatch(/^Updated /);
  });

  it("marks a missing required key red and an unset optional one grey", async () => {
    const { context } = stubContext([
      ok(status(["OMNIGENT_OIDC_COOKIE_SECRET", "AE_DATABRICKS_CLIENT_ID"])),
    ]);
    const node = await mount(context);
    expect(pill(node)).toBe("1 required missing");
    expect(node.querySelector(".card-fabric")?.classList.contains("fabric-failed")).toBe(true);
    const missing = chipOf(node, "Microsoft sign-in");
    expect(missing?.classList.contains("secret-missing")).toBe(true);
    expect(missing?.querySelector(".secret-state")?.textContent).toBe("Missing");
    const absent = chipOf(node, "Databricks section");
    expect(absent?.classList.contains("secret-absent")).toBe(true);
    expect(absent?.querySelector(".secret-state")?.textContent).toBe("Not set");
    expect(node.querySelector('.notice[role="alert"]')?.textContent).toMatch(
      /OMNIGENT_OIDC_COOKIE_SECRET/,
    );
  });

  it("draws one chip for a feature that needs two keys", async () => {
    const two = status();
    two.required.push({ key: "OMNIGENT_OIDC_CLIENT_ID", use: "Microsoft sign-in", present: true });
    const { context } = stubContext([ok(two)]);
    const node = await mount(context);
    const chips = [...node.querySelectorAll('[aria-label="Required"] .secret-use')];
    expect(chips.map((item) => item.textContent)).toEqual(["Database", "Microsoft sign-in"]);
    expect(chipOf(node, "Microsoft sign-in")?.title).toBe(
      "OMNIGENT_OIDC_COOKIE_SECRET: Present\nOMNIGENT_OIDC_CLIENT_ID: Present",
    );
  });

  it("turns a two-key chip red or grey when either key is missing", () => {
    const rows = (use: string, present: boolean[]) =>
      present.map((p, i) => ({ key: `K${i}`, use, present: p }));
    const two = (required: boolean, present: boolean[]) => {
      const list = rows("Feature", present);
      return card({
        required: required ? list : [],
        optional: required ? [] : list,
        missing: [],
      }).querySelectorAll(".secret-key");
    };
    expect(two(true, [true, false])).toHaveLength(1);
    expect(two(true, [true, false])[0]?.classList.contains("secret-missing")).toBe(true);
    expect((two(true, [true, false])[0] as HTMLElement).title).toBe("K0: Present\nK1: Missing");
    expect(two(false, [false, true])[0]?.classList.contains("secret-absent")).toBe(true);
    expect(two(true, [true, true])[0]?.className).toBe("secret-key");
  });

  it("reads again on focus, with no timer of its own", async () => {
    const { context, request } = stubContext([ok(status()), ok(status(["POSTGRES_PASSWORD"]))]);
    const node = await mount(context);
    await vi.advanceTimersByTimeAsync(10 * 60_000);
    expect(request).toHaveBeenCalledTimes(1);
    window.dispatchEvent(new Event("focus"));
    await flush();
    expect(request).toHaveBeenCalledTimes(2);
    expect(pill(node)).toBe("1 required missing");
  });

  it("leaves the card alone when a focus read brings the same answer", async () => {
    const { context } = stubContext([
      ok(status(["POSTGRES_PASSWORD"])),
      ok(status(["POSTGRES_PASSWORD"])),
    ]);
    const node = await mount(context);
    const before = node.querySelector(".secrets-card");
    window.dispatchEvent(new Event("focus"));
    await flush();
    // A redraw would make a screen reader announce the alert again.
    expect(node.querySelector(".secrets-card")).toBe(before);
  });

  it("keeps the last good card when a read fails", async () => {
    const { context } = stubContext([ok(status()), failed(502, "upstream down")]);
    const node = await mount(context);
    window.dispatchEvent(new Event("focus"));
    await flush();
    expect(pill(node)).toBe("All required present");
    expect(notices(node).at(-1)).toMatch(/^The secrets could not be read/);
  });

  it("shows only the failure when the first read fails", async () => {
    const { context } = stubContext([failed(500, "boom")]);
    const node = await mount(context);
    expect(node.querySelector(".card-fabric")).toBeNull();
    expect(notices(node)[0]).toMatch(/^The secrets could not be read/);
  });

  it("stops reading once deactivated", async () => {
    const { context, request } = stubContext([ok(status()), ok(status())]);
    await mount(context);
    deactivateSecrets();
    window.dispatchEvent(new Event("focus"));
    await flush();
    expect(request).toHaveBeenCalledTimes(1);
  });
});
