// The Admin Center's Secrets section (2026-10-20): each key the server
// expects, with whether it has it, names only; 2026-10-23, one row per key with
// its vault expiry and Edit and Rotate links. Every call is mocked: the route
// is GET /v1/ae/secrets.
import type { ExtensionContext } from "@omnigent/extension-sdk";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { SecretKey, SecretsStatus } from "../src/api";
import {
  activateSecrets,
  card,
  checkText,
  deactivateSecrets,
  expiryText,
  fromText,
  keyState,
  metaItems,
  noticeTexts,
  okLine,
  pillText,
  problems,
  secretsSection,
  secretsTile,
  toneOf,
} from "../src/secrets";
import { isOpen, resetSectionState } from "../src/sections";

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

const EDIT = "https://portal.azure.com/#@/asset/Microsoft_Azure_KeyVault/Secret/https://v.example/secrets/";

/** Every key present except those named in *off*; *rows* overrides a key's vault fields. */
function status(off: string[] = [], rows: Record<string, Partial<SecretKey>> = {}): SecretsStatus {
  const build = (keys: Array<[string, string]>) =>
    keys.map(([key, use]) => ({
      key,
      use,
      present: !off.includes(key),
      vault_name: `kv-${key.toLowerCase()}`,
      expiry_state: "none" as const,
      edit_url: `${EDIT}kv-${key.toLowerCase()}`,
      rotate_url: `${EDIT}kv-${key.toLowerCase()}`,
      rotate_hint: "Make the new value where it is issued.",
      ...rows[key],
    }));
  return {
    required: build(REQUIRED),
    optional: build(OPTIONAL),
    missing: REQUIRED.map(([key]) => key).filter((key) => off.includes(key)),
    meta_read_at: "2026-10-20T12:00:00Z",
    meta_stale: false,
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
const rowOf = (node: ParentNode, key: string) =>
  node.querySelector<HTMLElement>(`.secret-row[data-key="${key}"]`);

beforeEach(() => {
  resetSectionState();
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
    expect(pillText(status())).toBe("All present");
    expect(pillText(status(["POSTGRES_PASSWORD", "OMNIGENT_OIDC_COOKIE_SECRET"]))).toBe(
      "2 required missing",
    );
  });

  it("counts the keys and says when the vault was read", () => {
    const items = metaItems(
      status(["OMNIGENT_OIDC_COOKIE_SECRET"], { AE_GITHUB_TOKEN: { expiry_state: "soon" } }),
    );
    expect(items.slice(0, 3)).toEqual(["4 keys", "2 required", "1 expiring"]);
    expect(items[3]).toMatch(/^vault read /);
    expect(metaItems({ ...status(), meta_read_at: null })[3]).toBe("vault not read yet");
  });

  it("turns red for an expired required key but only the tile for an expired optional one", () => {
    const expired = { expiry_state: "expired" as const, expires: "2026-10-01T00:00:00Z" };
    const required = status([], { POSTGRES_PASSWORD: expired });
    expect(toneOf(required)).toBe("failed");
    expect(pillText(required)).toBe("1 required expired");
    expect(secretsTile(required, null).health).toBe("problem");
    const optional = status([], { AE_GITHUB_TOKEN: expired });
    expect(toneOf(optional)).toBe("on");
    expect(secretsTile(optional, null)).toMatchObject({ health: "attention", note: "1 expired" });
    const soon = status([], { AE_GITHUB_TOKEN: { expiry_state: "soon" } });
    expect(secretsTile(soon, null)).toMatchObject({ health: "attention", note: "1 expiring soon" });
    expect(secretsTile(status(), null)).toMatchObject({
      health: "ok",
      note: "all present, none expiring",
    });
  });

  it("words each expiry state", () => {
    const row = { key: "K", use: "u", present: true, expires: "2028-10-23T00:00:00+00:00" };
    expect(expiryText({ ...row, expiry_state: "ok", days_left: 730 })).toBe(
      "expires 2028-10-23, 730 days",
    );
    expect(expiryText({ ...row, expiry_state: "soon", days_left: 1 })).toBe(
      "expires 2028-10-23, 1 day",
    );
    expect(expiryText({ ...row, expiry_state: "expired", days_left: -3 })).toBe(
      "expired 2028-10-23",
    );
    expect(expiryText({ ...row, expiry_state: "none" })).toBe("no expiry set");
    expect(expiryText({ ...row, expiry_state: "unknown" })).toBe("expiry not read");
    expect(expiryText({ key: "K", use: "u", present: true })).toBe("expiry not read");
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

describe("the notices about dates", () => {
  it("names an expired key and how to recover", () => {
    const expired = status([], { AE_GITHUB_TOKEN: { expiry_state: "expired" } });
    const items = noticeTexts(expired);
    expect(items).toHaveLength(1);
    expect(items[0]?.kind).toBe("error");
    expect(items[0]?.text).toMatch(/^Expired in Key Vault: AE_GITHUB_TOKEN\./);
  });

  it("says when the box has not reported, or reported too long ago", () => {
    const never = noticeTexts({ ...status(), meta_read_at: null, meta_stale: true });
    expect(never[0]?.text).toMatch(/^No expiry dates yet/);
    const old = noticeTexts({ ...status(), meta_stale: true });
    expect(old[0]?.text).toMatch(/over 36 hours old/);
    expect(old[0]?.text).toMatch(/secrets_relay/);
  });
});

describe("the section", () => {
  it("lists every key, required first, with its state, use and vault name", async () => {
    const { context } = stubContext([ok(status())]);
    const node = await mount(context);
    expect(pill(node)).toBe("All present");
    expect(node.querySelector(".card-fabric")?.classList.contains("fabric-on")).toBe(true);
    const keys = [...node.querySelectorAll(".secret-name")].map((item) => item.textContent);
    expect(keys).toEqual([...REQUIRED, ...OPTIONAL].map(([key]) => key));
    const github = rowOf(node, "AE_GITHUB_TOKEN");
    expect(github?.querySelector(".secret-use")?.textContent).toBe("PR cards and Merge");
    expect(github?.querySelector(".secret-vault")?.textContent).toBe("Key Vault: kv-ae_github_token");
    expect(github?.querySelector(".secret-state")?.textContent).toBe("Present");
    expect(
      [...node.querySelectorAll(".secret-group-title")].map((item) => item.textContent),
    ).toEqual(["Required", "Optional"]);
    expect(notices(node)).toEqual([]);
    expect(node.querySelector(".secrets-refreshed")?.textContent).toMatch(/^Updated /);
  });

  it("shows each key's expiry, amber inside 60 days and red once past", async () => {
    const { context } = stubContext([
      ok(
        status([], {
          POSTGRES_PASSWORD: {
            expiry_state: "ok",
            expires: "2028-10-23T00:00:00+00:00",
            days_left: 730,
            updated: "2026-10-22T05:02:08+00:00",
          },
          AE_GITHUB_TOKEN: {
            expiry_state: "soon",
            expires: "2026-11-02T00:00:00+00:00",
            days_left: 10,
          },
          AE_DATABRICKS_CLIENT_ID: {
            expiry_state: "expired",
            expires: "2026-10-01T00:00:00+00:00",
            days_left: -20,
            enabled: false,
          },
        }),
      ),
    ]);
    const node = await mount(context);
    const db = rowOf(node, "POSTGRES_PASSWORD");
    expect(db?.querySelector(".secret-expiry")?.textContent).toBe("expires 2028-10-23, 730 days");
    expect(db?.querySelector(".secret-changed")?.textContent).toBe("changed today");
    expect(db?.querySelector(".secret-changed")?.getAttribute("title")).toBe(
      "Changed in Key Vault 2026-10-22",
    );
    expect(db?.classList.contains("secret-soon")).toBe(false);
    const soon = rowOf(node, "AE_GITHUB_TOKEN");
    expect(soon?.classList.contains("secret-soon")).toBe(true);
    expect(soon?.querySelector(".expiry-soon")?.textContent).toBe("expires 2026-11-02, 10 days");
    expect(soon?.querySelector(".secret-hint")?.textContent).toBe(
      "Make the new value where it is issued.",
    );
    const expired = rowOf(node, "AE_DATABRICKS_CLIENT_ID");
    expect(expired?.classList.contains("secret-expired")).toBe(true);
    expect(expired?.querySelector(".secret-disabled")?.textContent).toBe("disabled in vault");
    expect(rowOf(node, "OMNIGENT_OIDC_COOKIE_SECRET")?.querySelector(".secret-expiry")?.textContent).toBe(
      "no expiry set",
    );
    expect(rowOf(node, "OMNIGENT_OIDC_COOKIE_SECRET")?.querySelector(".secret-hint")).toBeNull();
    expect(notices(node).some((text) => /^Expired in Key Vault: AE_DATABRICKS_CLIENT_ID/.test(text ?? ""))).toBe(true);
  });

  it("gives each row an Edit and a Rotate link that name the key and open safely", async () => {
    const { context } = stubContext([ok(status())]);
    const node = await mount(context);
    const links = [...(rowOf(node, "POSTGRES_PASSWORD")?.querySelectorAll("a") ?? [])];
    expect(links.map((link) => link.textContent)).toEqual(["Edit", "Rotate"]);
    expect(links.map((link) => link.getAttribute("aria-label"))).toEqual([
      "Edit POSTGRES_PASSWORD in Key Vault",
      "Rotate POSTGRES_PASSWORD",
    ]);
    for (const link of links) {
      expect(link.getAttribute("target")).toBe("_blank");
      expect(link.getAttribute("rel")).toBe("noopener noreferrer");
      expect(link.getAttribute("href")).toMatch(/^https:\/\//);
    }
    expect(links[1]?.title).toBe("Make the new value where it is issued.");
  });

  it("draws no link for a key without a vault name or for an address that is not https", async () => {
    const { context } = stubContext([
      ok(
        status([], {
          POSTGRES_PASSWORD: { vault_name: null, edit_url: null, rotate_url: null },
          AE_GITHUB_TOKEN: { edit_url: "javascript:alert(1)", rotate_url: "http://plain.example" },
        }),
      ),
    ]);
    const node = await mount(context);
    expect(rowOf(node, "POSTGRES_PASSWORD")?.querySelectorAll("a")).toHaveLength(0);
    expect(rowOf(node, "POSTGRES_PASSWORD")?.querySelector(".secret-expiry")).toBeNull();
    expect(rowOf(node, "AE_GITHUB_TOKEN")?.querySelectorAll("a")).toHaveLength(0);
  });

  it("says when the vault dates are stale", async () => {
    const { context } = stubContext([ok({ ...status(), meta_stale: true })]);
    const node = await mount(context);
    expect(notices(node).join(" ")).toMatch(/over 36 hours old/);
  });

  it("marks a missing required key red and an unset optional one grey", async () => {
    const { context } = stubContext([
      ok(status(["OMNIGENT_OIDC_COOKIE_SECRET", "AE_DATABRICKS_CLIENT_ID"])),
    ]);
    const node = await mount(context);
    expect(pill(node)).toBe("1 required missing");
    expect(node.querySelector(".card-fabric")?.classList.contains("fabric-failed")).toBe(true);
    const missing = rowOf(node, "OMNIGENT_OIDC_COOKIE_SECRET");
    expect(missing?.classList.contains("secret-missing")).toBe(true);
    expect(missing?.querySelector(".secret-state")?.textContent).toBe("Missing");
    const absent = rowOf(node, "AE_DATABRICKS_CLIENT_ID");
    expect(absent?.classList.contains("secret-absent")).toBe(true);
    expect(absent?.querySelector(".secret-state")?.textContent).toBe("Not set");
    expect(node.querySelector('.notice[role="alert"]')?.textContent).toMatch(
      /OMNIGENT_OIDC_COOKIE_SECRET/,
    );
  });

  it("draws one row per key even when a feature needs two", () => {
    const two = status();
    two.required.push({ key: "OMNIGENT_OIDC_CLIENT_ID", use: "Microsoft sign-in", present: true });
    two.required.push({ key: "OMNIGENT_OIDC_CLIENT_SECRET", use: "Microsoft sign-in", present: false });
    const rows = card(two).querySelectorAll('[aria-label="Required"] .secret-row');
    expect([...rows].map((row) => row.getAttribute("data-key"))).toEqual([
      "POSTGRES_PASSWORD",
      "OMNIGENT_OIDC_COOKIE_SECRET",
      "OMNIGENT_OIDC_CLIENT_ID",
      "OMNIGENT_OIDC_CLIENT_SECRET",
    ]);
    expect(keyState(two.required[3] as SecretKey, true)).toBe("Missing");
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
    expect(pill(node)).toBe("All present");
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

// ── 2026-10-23: collapsed by default, chips, the host key, last worked ──────

const groupsOf = (node: ParentNode) => node.querySelector<HTMLElement>(".secret-groups");
const toggleOf = (node: ParentNode) => node.querySelector<HTMLButtonElement>(".secret-toggle");
const chipsOf = (node: ParentNode) =>
  [...node.querySelectorAll<HTMLElement>(".secret-chip")].map((chip) => chip.dataset.key);

describe("collapsed and open", () => {
  it("starts collapsed with one green line when nothing needs the owner", () => {
    const node = card(status());
    expect(groupsOf(node)?.hidden).toBe(true);
    expect(toggleOf(node)?.getAttribute("aria-expanded")).toBe("false");
    expect(toggleOf(node)?.textContent).toBe("Show 4 keys");
    expect(node.querySelector(".secret-okline")?.textContent).toBe(
      "Nothing needs you. Next expiry: none set.",
    );
    expect(chipsOf(node)).toEqual([]);
    expect(node.querySelector(".fabric-name")?.textContent).toBe("Secrets on this box");
  });

  it("opens and closes the rows and remembers it", () => {
    const node = card(status());
    toggleOf(node)?.click();
    expect(groupsOf(node)?.hidden).toBe(false);
    expect(toggleOf(node)?.textContent).toBe("Hide keys");
    expect(isOpen("secrets-keys", false)).toBe(true);
    expect(groupsOf(card(status()))?.hidden).toBe(false);
    toggleOf(node)?.click();
    expect(groupsOf(node)?.hidden).toBe(true);
    expect(isOpen("secrets-keys", true)).toBe(false);
  });

  it("chips only the keys that need the owner, worst first, and links each to Rotate", () => {
    const answer = status(["POSTGRES_PASSWORD", "AE_DATABRICKS_CLIENT_ID"], {
      AE_GITHUB_TOKEN: { expiry_state: "soon", days_left: 21 },
    });
    const node = card(answer);
    // An unset optional key is not a problem; a missing required one is.
    expect(chipsOf(node)).toEqual(["POSTGRES_PASSWORD", "AE_GITHUB_TOKEN"]);
    const soon = node.querySelector<HTMLAnchorElement>('.secret-chip[data-key="AE_GITHUB_TOKEN"]');
    expect(soon?.tagName).toBe("A");
    expect(soon?.textContent).toBe("PR cards and Mergeexpires in 21d");
    expect(soon?.getAttribute("rel")).toBe("noopener noreferrer");
    const missing = node.querySelector('.secret-chip[data-key="POSTGRES_PASSWORD"]');
    expect(missing?.tagName).toBe("SPAN");
    expect(missing?.classList.contains("chip-err")).toBe(true);
    expect(node.querySelector(".secret-okline")).toBeNull();
  });

  it("names the nearest expiry when nothing needs the owner", () => {
    const answer = status([], {
      AE_GITHUB_TOKEN: { expiry_state: "ok", expires: "2027-05-15T00:00:00Z", days_left: 203 },
    });
    expect(okLine(answer)).toBe("Nothing needs you. Next expiry: PR cards and Merge, 2027-05-15.");
  });
});

describe("the host key and last worked", () => {
  const host = (present: boolean): SecretKey => ({
    key: "CLAUDE_CODE_OAUTH_TOKEN",
    use: "Claude on the host",
    present,
    where: "host",
    vault_name: "claude-code-oauth-token",
    expiry_state: "none",
  });

  it("tags the host row and turns red when the host lacks its key", () => {
    const answer = status();
    answer.required.push(host(false));
    answer.host_missing = ["CLAUDE_CODE_OAUTH_TOKEN"];
    expect(toneOf(answer)).toBe("failed");
    expect(pillText(answer)).toBe("1 required missing");
    expect(secretsTile(answer, null).health).toBe("problem");
    const node = card(answer);
    expect(rowOf(node, "CLAUDE_CODE_OAUTH_TOKEN")?.querySelector(".secret-tag")?.textContent).toBe(
      "host",
    );
    expect(noticeTexts(answer)[0]?.text).toMatch(/^The host's env file has no CLAUDE_CODE_OAUTH_TOKEN/);
  });

  it("says when a key last worked or was refused, red for a required key", () => {
    const at = "2026-10-20T14:07:00Z";
    expect(checkText({ ...host(true), check: "ok", checked_at: at })).toMatch(/^worked /);
    expect(
      checkText({ ...host(true), check: "failed", checked_at: at, check_detail: "401" }),
    ).toMatch(/^failed: 401 at /);
    const answer = status([], {
      POSTGRES_PASSWORD: { check: "failed", checked_at: at, check_detail: "401" },
      AE_GITHUB_TOKEN: { check: "ok", checked_at: at },
    });
    answer.failing = ["POSTGRES_PASSWORD"];
    expect(toneOf(answer)).toBe("failed");
    expect(pillText(answer)).toBe("1 required refused");
    expect(problems(answer).map((p) => p.text)).toEqual(["refused (401)"]);
    const node = card(answer);
    expect(rowOf(node, "POSTGRES_PASSWORD")?.querySelector(".check-failed")?.textContent).toMatch(
      /^failed: 401 at /,
    );
    expect(rowOf(node, "AE_GITHUB_TOKEN")?.querySelector(".check-ok")?.getAttribute("title")).toBe(
      "Since the server started; it resets on a restart.",
    );
  });

  it("goes amber, not red, for a refused optional key", () => {
    const answer = status([], {
      AE_GITHUB_TOKEN: { check: "failed", checked_at: "2026-10-20T14:07:00Z", check_detail: "401" },
    });
    expect(toneOf(answer)).toBe("warn");
    expect(card(answer).classList.contains("secrets-warn")).toBe(true);
    expect(secretsTile(answer, null).health).toBe("attention");
  });

  it("marks a date from Entra or GitHub and an old change", () => {
    const node = card(
      status([], {
        AE_GITHUB_TOKEN: {
          expiry_state: "soon",
          expires: "2026-11-02T00:00:00Z",
          days_left: 12,
          expiry_from: "github",
          updated: "2026-01-01T00:00:00Z",
        },
      }),
    );
    const row = rowOf(node, "AE_GITHUB_TOKEN");
    expect(row?.querySelector(".secret-from")?.textContent).toBe(" from GitHub");
    expect(row?.querySelector(".secret-changed")?.classList.contains("secret-old")).toBe(true);
  });

  it("draws an older server's answer, without the new fields", () => {
    const old = status();
    delete old.meta_stale;
    const node = card(old);
    expect(node.querySelector(".secret-check")).toBeNull();
    expect(node.querySelector(".secret-tag")).toBeNull();
    expect(toneOf(old)).toBe("on");
    expect(pill(node)).toBe("All present");
  });
});

describe("issuer dates", () => {
  it("says latest of N and when a carried Entra reading was made", () => {
    const row = { expiry_state: "ok" as const, expiry_from: "entra" as const };
    expect(fromText(row)).toBe("from Entra");
    expect(fromText({ ...row, source_count: 2 })).toBe("from Entra, latest of 2");
    expect(fromText({ ...row, source_from: "2026-10-21T06:15:00Z" })).toMatch(
      /^from Entra, read Oct 2[01]$/,
    );
    expect(fromText({ ...row, expiry_from: "github" })).toBe("from GitHub");
    expect(fromText({ ...row, expiry_from: "vault" })).toBeNull();
    expect(fromText({ ...row, expiry_state: "none" })).toBeNull();
  });
});

describe("a signed-in non-admin (2026-10-28)", () => {
  it("gets one quiet line and no more reads", async () => {
    const denied: Reply = { ok: false, status: 403, body: { error: { code: "admin_only", message: "Only an OmniGent admin can see this; ask an admin to do it." } } };
    const { context, request } = stubContext([denied, denied]);
    const node = await mount(context);
    expect(node.textContent).toContain("The box's secrets are for admins.");
    expect(notices(node)).toEqual([]);
    window.dispatchEvent(new Event("focus"));
    await flush();
    expect(request).toHaveBeenCalledTimes(1);
  });
});

describe("a false admin_only answer (2026-10-28)", () => {
  it("is looked at again after five minutes and the card comes back on a 200", async () => {
    const denied: Reply = { ok: false, status: 403, body: { error: { code: "admin_only", message: "Only an OmniGent admin can see this; ask an admin to do it." } } };
    const { context, request } = stubContext([denied, ok(status())]);
    const node = await mount(context);
    expect(node.textContent).toContain("for admins");
    vi.setSystemTime(NOW + 4 * 60_000);
    window.dispatchEvent(new Event("focus"));
    await flush();
    expect(request).toHaveBeenCalledTimes(1);
    vi.setSystemTime(NOW + 5 * 60_000 + 1000);
    window.dispatchEvent(new Event("focus"));
    await flush();
    expect(request).toHaveBeenCalledTimes(2);
    expect(node.textContent).not.toContain("for admins");
  });
});

describe("the secrets look-again timer (2026-10-28)", () => {
  it("reads once more five minutes after admin_only with no focus event", async () => {
    const denied: Reply = { ok: false, status: 403, body: { error: { code: "admin_only", message: "no" } } };
    const { context, request } = stubContext([denied, ok(status())]);
    const node = await mount(context);
    expect(node.textContent).toContain("for admins");
    await vi.advanceTimersByTimeAsync(4 * 60_000);
    expect(request).toHaveBeenCalledTimes(1);
    await vi.advanceTimersByTimeAsync(61_000);
    expect(request).toHaveBeenCalledTimes(2);
    expect(node.textContent).not.toContain("for admins");
  });
});
