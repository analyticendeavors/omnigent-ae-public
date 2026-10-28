// The Admin Center's Key Vault card (2026-10-23): every secret in both vaults
// by name, from GET /v1/ae/vault. Every call is mocked.
import type { ExtensionContext } from "@omnigent/extension-sdk";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { VaultInventory, VaultSecret, VaultView } from "../src/api";
import { getTile, resetTiles } from "../src/tiles";
import {
  activateVault,
  alarmWords,
  deactivateVault,
  expiryWords,
  filterCount,
  loadVaultView,
  matches,
  NO_OWNER,
  ownerFilters,
  ownerHue,
  ownerInitials,
  ownerName,
  resetVaultView,
  vaultSection,
  vaultTile,
  VIEW_KEY,
} from "../src/vault";

const sdk = vi.hoisted(() => ({ defineExtension: vi.fn() }));
vi.mock("@omnigent/extension-sdk", () => sdk);

type Reply = { ok: boolean; status: number; body: unknown };

const NOW = Date.parse("2026-10-23T21:00:00Z");
const EDIT = "https://portal.azure.com/#@/asset/Microsoft_Azure_KeyVault/Secret/https://v/secrets/";

function secret(name: string, fields: Partial<VaultSecret> = {}): VaultSecret {
  return {
    name,
    group: name.split("-")[0] ?? name,
    enabled: true,
    updated: "2026-10-20T00:00:00Z",
    updated_days: 3,
    expires: null,
    vault_expires: null,
    source_expires: null,
    expiry_from: null,
    days_left: null,
    expiry_state: "none",
    untouched: false,
    on_box: false,
    box_key: null,
    both_vaults: false,
    edit_url: `${EDIT}${name}`,
    ...fields,
  };
}

function view(vault: string, secrets: VaultSecret[], extra: Partial<VaultView> = {}): VaultView {
  const groups = [...new Set(secrets.map((row) => row.group))].map((id) => ({
    id,
    label: id === "omnigent" ? "OmniGent" : id[0]!.toUpperCase() + id.slice(1),
    count: secrets.filter((row) => row.group === id).length,
    oldest_days: Math.max(...secrets.filter((row) => row.group === id).map((row) => row.updated_days ?? 0)),
  }));
  return {
    vault,
    count: secrets.length,
    counts: {
      expired: secrets.filter((r) => r.expiry_state === "expired").length,
      soon: secrets.filter((r) => r.expiry_state === "soon").length,
      no_expiry: secrets.filter((r) => r.expiry_state === "none").length,
      untouched: secrets.filter((r) => r.untouched).length,
    },
    needs_you: [],
    needs_you_total: 0,
    groups,
    secrets,
    ...extra,
  };
}

function inventory(): VaultInventory {
  const box = view(
    "kv-example-box",
    [
      secret("omnigent-ae-github-token", { on_box: true, box_key: "AE_GITHUB_TOKEN" }),
      secret("hubspot-access-token", { both_vaults: true, untouched: true, updated_days: 183 }),
      secret("databricks-cli-client-secret", {
        expiry_state: "ok",
        expires: "2028-10-23T00:00:00Z",
        days_left: 730,
      }),
      secret("azure-cost-client-secret", {
        on_box: true,
        expiry_state: "soon",
        expires: "2026-11-20T00:00:00Z",
        days_left: 28,
        expiry_from: "entra",
      }),
    ],
    {
      needs_you: [
        { name: "azure-cost-client-secret", kind: "soon", text: "Expires in 28 days, from Entra" },
        { name: "hubspot-access-token", kind: "both", text: "Rotate in both vaults" },
      ],
      needs_you_total: 2,
    },
  );
  const web = view("kv-example-web", [secret("hubspot-access-token", { both_vaults: true })]);
  return { read_at: "2026-10-23T14:07:00Z", stale: false, box_vault: "kv-example-box", vaults: [box, web] };
}

const ok = (body: unknown): Reply => ({ ok: true, status: 200, body });
const failed = (code: number, message: string): Reply => ({
  ok: false,
  status: code,
  body: { error: { code: "x", message } },
});

type Route = (body: unknown) => Reply;

function stubContext(replies: Reply[], saved: unknown = undefined, routes: Record<string, Route> = {}) {
  let served: VaultInventory | null = null;
  const request = vi.fn(async ({ method, path, body }: { method: string; path: string; body?: unknown }) => {
    const route = routes[`${method} ${path}`];
    if (route) return route(body);
    // The card reads the summary, then each vault's rows in pages (2026-10-24): the
    // stub answers the pages from the last summary it handed out, two rows a page.
    const page = /^\/v1\/ae\/vault\/rows\?vault=([^&]+)&start=(\d+)$/.exec(path);
    if (method === "GET" && page) {
      const vault = decodeURIComponent(page[1]!);
      const start = Number(page[2]);
      const rows = served?.vaults.find((v) => v.vault === vault)?.secrets ?? [];
      const end = Math.min(start + 2, rows.length);
      return ok({ vault, secrets: rows.slice(start, end), next: end < rows.length ? end : null });
    }
    if (`${method} ${path}` !== "GET /v1/ae/vault?rows=false") return failed(500, `unexpected ${path}`);
    const reply = replies.shift() ?? failed(500, "no reply queued");
    if (!reply.ok) return reply;
    served = structuredClone(reply.body as VaultInventory);
    const summary = structuredClone(served);
    for (const v of summary.vaults) v.secrets = [];
    return { ...reply, body: summary };
  });
  const set = vi.fn(async () => undefined);
  const get = vi.fn(async () => saved);
  const context = {
    view: "fabric",
    server: { request },
    storage: { user: { get, set } },
  } as unknown as ExtensionContext;
  return { context, request, set };
}

const flush = () => vi.advanceTimersByTimeAsync(0);

async function mount(context: ExtensionContext): Promise<HTMLElement> {
  await loadVaultView(context);
  const node = vaultSection();
  document.getElementById("root")?.appendChild(node);
  activateVault(context, node);
  await flush();
  return node;
}

const q = <T extends HTMLElement = HTMLElement>(node: ParentNode, sel: string) =>
  node.querySelector<T>(sel);
const all = (node: ParentNode, sel: string) => [...node.querySelectorAll<HTMLElement>(sel)];
const names = (node: ParentNode, sel = ".vault-row:not([hidden] *)") =>
  all(node, sel)
    .filter((row) => !row.closest("[hidden]"))
    .map((row) => row.dataset.name);

beforeEach(() => {
  resetVaultView();
  resetTiles();
  document.body.replaceChildren();
  const root = document.createElement("div");
  root.id = "root";
  document.body.appendChild(root);
  vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout", "Date"] });
  vi.setSystemTime(NOW);
});

afterEach(() => {
  deactivateVault();
  vi.useRealTimers();
  vi.restoreAllMocks();
});

describe("the words", () => {
  it("filters by badge, expiry and age, and searches names and box keys", () => {
    const rows = inventory().vaults[0]!;
    expect(filterCount(rows, "all")).toBe(4);
    expect(filterCount(rows, "box")).toBe(2);
    expect(filterCount(rows, "both")).toBe(1);
    expect(filterCount(rows, "none")).toBe(2);
    expect(filterCount(rows, "old")).toBe(1);
    const github = rows.secrets[0]!;
    expect(matches(github, "all", "GITHUB")).toBe(true);
    expect(matches(github, "all", "ae_github_token")).toBe(true);
    expect(matches(github, "both", "github")).toBe(false);
  });

  it("words an expiry", () => {
    expect(expiryWords(secret("a"))).toBe("no expiry");
    expect(expiryWords(secret("a", { expiry_state: "ok", expires: "2028-10-23T00:00:00Z", days_left: 730 }))).toBe(
      "2028-10-23 · 730d",
    );
    expect(expiryWords(secret("a", { expiry_state: "expired", expires: "2026-10-01T00:00:00Z" }))).toBe(
      "expired 2026-10-01",
    );
  });

  it("builds the tile: both counts, amber when something needs a look", () => {
    expect(vaultTile(inventory(), null)).toMatchObject({
      value: "4 + 1",
      note: "1 expiring, 1 untouched 180d+",
      health: "attention",
    });
    const calm = inventory();
    calm.vaults = calm.vaults.map((v) => ({ ...v, counts: { ...v.counts, soon: 0 }, needs_you: [] }));
    expect(vaultTile(calm, null).health).toBe("ok");
    expect(vaultTile({ ...calm, vaults: [] }, null).value).toBe("Not read");
    const unset = { ...calm, vaults: [], configured: false };
    expect(vaultTile(unset, null)).toMatchObject({
      value: "Not read",
      note: "vault not configured (AE_VAULT_NAME)",
      health: "attention",
    });
    const stale = vaultTile({ ...calm, stale: true }, null);
    expect(stale.value).toBe("4 + 1");
    expect(stale.health).toBe("attention");
  });
});

describe("the card", () => {
  it("shows the box vault's counts and needs-you list, with the list folded", async () => {
    const { context } = stubContext([ok(inventory())]);
    const node = await mount(context);
    expect(all(node, ".vault-tab").map((tab) => tab.textContent)).toEqual([
      "kv-example-box4",
      "kv-example-web1",
    ]);
    expect(q(node, '.vault-tab[aria-pressed="true"]')?.dataset.vault).toBe("kv-example-box");
    expect(all(node, ".vault-stat-n").map((n) => n.textContent)).toEqual(["0", "1", "2", "1"]);
    expect(all(node, ".vault-need").map((row) => [row.dataset.name, row.dataset.kind])).toEqual([
      ["azure-cost-client-secret", "soon"],
      ["hubspot-access-token", "both"],
    ]);
    const edit = q<HTMLAnchorElement>(node, ".vault-need .vault-edit");
    expect(edit?.getAttribute("href")).toBe(`${EDIT}azure-cost-client-secret`);
    expect(edit?.getAttribute("target")).toBe("_blank");
    expect(q(node, ".vault-list")?.hidden).toBe(true);
    expect(q(node, ".vault-toggle")?.textContent).toBe("Show all 4");
    expect(q(node, ".vault-refreshed")?.textContent).toMatch(/^Read /);
    expect(getTile("vault")?.value).toBe("4 + 1");
  });

  it("switches vault and remembers it", async () => {
    const { context, set } = stubContext([ok(inventory())]);
    const node = await mount(context);
    q<HTMLButtonElement>(node, '.vault-tab[data-vault="kv-example-web"]')?.click();
    expect(q(node, '.vault-tab[aria-pressed="true"]')?.dataset.vault).toBe("kv-example-web");
    expect(all(node, ".vault-stat-n")[2]?.textContent).toBe("1");
    expect(set).toHaveBeenLastCalledWith(VIEW_KEY, { vault: "kv-example-web", open: false });
  });

  it("starts on the remembered vault with the list open", async () => {
    const { context } = stubContext([ok(inventory())], { vault: "kv-example-web", open: true });
    const node = await mount(context);
    expect(q(node, '.vault-tab[aria-pressed="true"]')?.dataset.vault).toBe("kv-example-web");
    expect(q(node, ".vault-list")?.hidden).toBe(false);
  });

  it("opens a grouped list with groups folded, and a group on click", async () => {
    const { context, set } = stubContext([ok(inventory())]);
    const node = await mount(context);
    q<HTMLButtonElement>(node, ".vault-toggle")?.click();
    expect(q(node, ".vault-list")?.hidden).toBe(false);
    expect(set).toHaveBeenLastCalledWith(VIEW_KEY, { vault: null, open: true });
    expect(all(node, ".vault-group-head").map((h) => h.dataset.group)).toEqual([
      "omnigent",
      "hubspot",
      "databricks",
      "azure",
    ]);
    expect(names(node)).toEqual([]);
    q<HTMLButtonElement>(node, '.vault-group-head[data-group="azure"]')?.click();
    expect(names(node)).toEqual(["azure-cost-client-secret"]);
    const row = q(node, '.vault-row[data-name="azure-cost-client-secret"]');
    expect(row?.querySelector(".badge-box")?.textContent).toBe("On box");
    expect(row?.querySelector(".secret-from")?.textContent).toBe(" from Entra");
    expect(row?.querySelector(".vault-expiry")?.textContent).toBe("2026-11-20 · 28d from Entra");
  });

  it("searches and filters, opening every group with a match", async () => {
    const { context } = stubContext([ok(inventory())], { vault: null, open: true });
    const node = await mount(context);
    const search = q<HTMLInputElement>(node, ".vault-search-input")!;
    search.value = "token";
    search.dispatchEvent(new Event("input"));
    expect(names(node)).toEqual(["omnigent-ae-github-token", "hubspot-access-token"]);
    // The input survives the redraw, so typing goes on.
    expect(q(node, ".vault-search-input")).toBe(search);
    // A group folds while the search is on, and opens again.
    q<HTMLButtonElement>(node, '.vault-group-head[data-group="omnigent"]')?.click();
    expect(names(node)).toEqual(["hubspot-access-token"]);
    q<HTMLButtonElement>(node, '.vault-group-head[data-group="omnigent"]')?.click();
    expect(names(node)).toEqual(["omnigent-ae-github-token", "hubspot-access-token"]);
    q<HTMLButtonElement>(node, '.vault-chip[data-filter="both"]')?.click();
    expect(names(node)).toEqual(["hubspot-access-token"]);
    expect(q(node, '.vault-row[data-name="hubspot-access-token"] .badge-both')?.textContent).toBe(
      "Both vaults",
    );
    expect(q(node, '.vault-chip[data-filter="box"] .vault-chip-n')?.textContent).toBe("2");
    search.value = "nothing-like-this";
    search.dispatchEvent(new Event("input"));
    expect(q(node, ".vault-empty")?.textContent).toBe("No secret matches.");
  });

  it("says admins only to anyone else, and the tile stays grey", async () => {
    const { context } = stubContext([failed(403, "Only an OmniGent admin can see it")]);
    const node = await mount(context);
    expect(q(node, ".notice")?.textContent).toBe(
      "Only an OmniGent admin can see the Key Vault inventory.",
    );
    expect(q(node, ".vault-body")?.hidden).toBe(true);
    expect(getTile("vault")).toMatchObject({ value: "Admins", health: "idle" });
  });

  it("says how to start the relay when there is no reading, and fails soft on an old server", async () => {
    const { context } = stubContext([ok({ read_at: null, stale: true, box_vault: "v", vaults: [] })]);
    const node = await mount(context);
    expect(q(node, ".notice")?.textContent).toMatch(/^No Key Vault reading yet/);
    deactivateVault();
    document.getElementById("root")?.replaceChildren();
    const older = stubContext([failed(404, "Not Found")]);
    const again = await mount(older.context);
    expect(q(again, ".notice")?.textContent).toMatch(/^The Key Vault inventory could not be read/);
    expect(getTile("vault")?.health).toBe("attention");
  });

  it("says when a vault's list is carried from an earlier run", async () => {
    const answer = inventory();
    answer.vaults[1] = { ...answer.vaults[1]!, read_from: "2026-10-21T06:15:00Z" };
    const { context } = stubContext([ok(answer)], { vault: "kv-example-web", open: false });
    const node = await mount(context);
    expect(q(node, ".vault-carried")?.textContent).toMatch(
      /^kv-example-web could not be read on the last run; showing its list from Oct 2[01]\.$/,
    );
    q<HTMLButtonElement>(node, '.vault-tab[data-vault="kv-example-box"]')?.click();
    expect(q(node, ".vault-carried")).toBeNull();
  });

  it("works when storage is unavailable", async () => {
    const { context } = stubContext([ok(inventory())]);
    (context.storage.user.get as ReturnType<typeof vi.fn>).mockRejectedValue(new Error("no storage"));
    (context.storage.user.set as ReturnType<typeof vi.fn>).mockImplementation(() => {
      throw new Error("no storage");
    });
    const node = await mount(context);
    q<HTMLButtonElement>(node, ".vault-toggle")?.click();
    expect(q(node, ".vault-list")?.hidden).toBe(false);
  });
});

// Owners and expiry alerts (2026-10-23). Every id is an example.com address.
const ANN = "ann.lee@example.com";
const BO = "bo@example.com";

function owned(): VaultInventory {
  const answer = inventory();
  const box = answer.vaults[0]!;
  const owners: Record<string, string | null> = {
    "omnigent-ae-github-token": ANN,
    "hubspot-access-token": null,
    "databricks-cli-client-secret": ANN,
    "azure-cost-client-secret": BO,
  };
  box.secrets = box.secrets.map((row) => ({
    ...row,
    owner: owners[row.name] ?? null,
    alert: null,
    rotate_url: row.edit_url,
    rotate_hint: "Make a new version in Key Vault.",
  }));
  box.secrets[3] = {
    ...box.secrets[3]!,
    alert: {
      stage: "30",
      alarm_at: "2026-11-20T17:00:00Z",
      sent_at: "2026-10-23T16:00:00Z",
      list: "AE",
      expires: "2026-11-20",
    },
  };
  box.counts = { ...box.counts, no_owner: 1 };
  box.needs_you = [
    { name: "azure-cost-client-secret", kind: "soon", text: "Expires in 28 days, from Entra", owner: BO, alarm_at: "2026-11-20T17:00:00Z" },
    { name: "hubspot-access-token", kind: "both", text: "Rotate in both vaults", owner: null, alarm_at: null },
  ];
  answer.vaults[1]!.secrets = answer.vaults[1]!.secrets.map((row) => ({ ...row, owner: null }));
  answer.vaults[1]!.counts = { ...answer.vaults[1]!.counts, no_owner: 1 };
  answer.owners_editable = true;
  answer.alerts = {
    on: true,
    off_reason: null,
    list: "AE",
    time: "09:00",
    tz: "America/Los_Angeles",
    next: { vault: "kv-example-box", name: "azure-cost-client-secret", alarm_at: "2026-11-20T17:00:00Z" },
    open: 1,
  };
  return answer;
}

const people = (): Reply => ok({ users: [{ id: ANN, is_admin: true }, { id: "cy.park@example.com", is_admin: false }] });

describe("owner words", () => {
  it("names, initials and a stable colour from the id", () => {
    expect(ownerName(ANN)).toBe("Ann Lee");
    expect(ownerInitials(ANN)).toBe("AL");
    expect(ownerInitials(BO)).toBe("B");
    expect(ownerHue(ANN)).toBe(ownerHue("ANN.LEE@example.com"));
    expect(ownerHue(ANN)).toBeGreaterThanOrEqual(0);
    expect(ownerHue(ANN)).toBeLessThan(360);
  });

  it("words an alarm in the browser's clock", () => {
    expect(alarmWords("2026-11-20T17:00:00Z")).toMatch(/^alarm Nov 2[01], \d{1,2}:00 [AP]M$/);
    expect(alarmWords(null)).toBeNull();
    expect(alarmWords("not a date")).toBeNull();
  });

  it("makes a chip per owner, most first, then No owner; none from an older server", () => {
    const box = owned().vaults[0]!;
    expect(ownerFilters(box).map((chip) => [chip.id, chip.label])).toEqual([
      [`owner:${ANN}`, "Ann"],
      [`owner:${BO}`, "Bo"],
      ["nobody", "No owner"],
    ]);
    expect(filterCount(box, `owner:${ANN}`)).toBe(2);
    expect(filterCount(box, "nobody")).toBe(1);
    expect(matches(box.secrets[0]!, `owner:${BO}`, "")).toBe(false);
    expect(ownerFilters(inventory().vaults[0]!)).toEqual([]);
  });

  it("puts the next alarm and the no-owner count on the tile", () => {
    const tile = vaultTile(owned(), null);
    expect(tile.note).toMatch(/^1 expiring · alarm set Nov 2[01] · 2 have no owner$/);
    expect(tile.title).toBe("kv-example-box: 4, kv-example-web: 1; 1 untouched 180d+");
    const one = owned();
    one.alerts = { ...one.alerts!, next: null };
    one.vaults[1]!.counts.no_owner = 0;
    expect(vaultTile(one, null).note).toBe("1 expiring · 1 has no owner");
  });
});

describe("owners on the card", () => {
  async function openGroup(node: HTMLElement, group: string): Promise<void> {
    if (q(node, ".vault-list")?.hidden) q<HTMLButtonElement>(node, ".vault-toggle")?.click();
    q<HTMLButtonElement>(node, `.vault-group-head[data-group="${group}"]`)?.click();
    await flush();
  }

  it("shows avatars on Needs you with the alarm, and filters by owner", async () => {
    const { context } = stubContext([ok(owned())]);
    const node = await mount(context);
    const needs = all(node, ".vault-need");
    expect(needs[0]?.querySelector(".vault-why")?.textContent).toMatch(
      /^Expires in 28 days, from Entra · alarm Nov 2[01], \d{1,2}:00 [AP]M$/,
    );
    expect(needs[0]?.querySelector(".vault-owner")?.textContent).toBe("B");
    expect(needs[1]?.querySelector(".vault-why")?.textContent).toBe("Rotate in both vaults");
    expect(needs[1]?.querySelector(".vault-owner.is-none")?.getAttribute("title")).toBe(NO_OWNER);
    q<HTMLButtonElement>(node, ".vault-toggle")?.click();
    expect(all(node, ".vault-chip").map((chip) => chip.dataset.filter)).toEqual([
      "all", "box", "both", "none", "old", `owner:${ANN}`, `owner:${BO}`, "nobody",
    ]);
    expect(q(node, '.vault-chip[data-filter="nobody"] .vault-chip-n')?.textContent).toBe("1");
    q<HTMLButtonElement>(node, `.vault-chip[data-filter="owner:${ANN}"]`)?.click();
    expect(names(node)).toEqual(["omnigent-ae-github-token", "databricks-cli-client-secret"]);
    expect(
      all(node, ".vault-row .vault-owner").map((avatar) => avatar.textContent),
    ).toEqual(["AL", "AL"]);
  });

  it("opens a row's strip with people and saves the owner, then reads again", async () => {
    const saves: unknown[] = [];
    const after = owned();
    after.vaults[0]!.secrets[1] = { ...after.vaults[0]!.secrets[1]!, owner: ANN };
    const { context, request } = stubContext([ok(owned()), ok(after)], undefined, {
      "GET /v1/ae/vault/people": people,
      "POST /v1/ae/vault/owner": (body) => {
        saves.push(body);
        return ok({ updated: 1, owners: { "hubspot-access-token": ANN } });
      },
    });
    const node = await mount(context);
    await openGroup(node, "hubspot");
    q(node, '.vault-row[data-name="hubspot-access-token"] .vault-name')?.click();
    await flush();
    const select = q<HTMLSelectElement>(node, ".vault-detail .vault-owner-select")!;
    expect([...select.options].map((o) => [o.value, o.textContent])).toEqual([
      ["", NO_OWNER],
      [ANN, "Ann Lee"],
      [BO, "Bo"],
      ["cy.park@example.com", "Cy Park"],
    ]);
    expect(select.value).toBe("");
    // Rotate goes to the same page as Edit here, so only Edit shows.
    expect(all(node, ".vault-detail-link").map((a) => [a.textContent, a.getAttribute("href")])).toEqual([
      ["Edit in Key Vault", `${EDIT}hubspot-access-token`],
    ]);
    select.value = ANN;
    select.dispatchEvent(new Event("change"));
    expect(q(node, ".vault-owner-status")?.textContent).toBe("Saving...");
    await flush();
    await flush();
    expect(saves).toEqual([{ vault: "kv-example-box", names: ["hubspot-access-token"], owner: ANN }]);
    expect(request.mock.calls.filter(([r]) => r.path === "/v1/ae/vault/people")).toHaveLength(1);
    expect(q(node, ".vault-detail")).toBeNull();
    expect(q(node, '.vault-row[data-name="hubspot-access-token"] .vault-owner')?.textContent).toBe("AL");
  });

  it("sets every secret in a group, and says why a save failed", async () => {
    const answer = owned();
    answer.vaults[0]!.secrets.push(
      secret("azure-other-secret", { owner: null }),
    );
    answer.vaults[0]!.groups = answer.vaults[0]!.groups.map((g) => (g.id === "azure" ? { ...g, count: 2 } : g));
    const saves: unknown[] = [];
    const { context } = stubContext([ok(answer)], undefined, {
      "GET /v1/ae/vault/people": () => failed(403, "admin only"),
      "POST /v1/ae/vault/owner": (body) => {
        saves.push(body);
        return failed(500, "The owner file could not be saved; check /data on the box.");
      },
    });
    const node = await mount(context);
    await openGroup(node, "azure");
    const setAll = q<HTMLButtonElement>(node, '.vault-set-all[data-group="azure"]')!;
    expect(setAll.textContent).toBe("Set owner for all 2");
    setAll.click();
    await flush();
    const select = q<HTMLSelectElement>(node, ".vault-group > .vault-detail .vault-owner-select")!;
    expect(select.value).toBe("mixed");
    // The people list failed: the owners already set are still offered.
    expect([...select.options].map((o) => o.value)).toEqual(["mixed", "", ANN, BO]);
    expect(q(node, ".vault-owner-status")?.textContent).toBe(
      "No people list from the server; showing the owners already set.",
    );
    select.value = BO;
    select.dispatchEvent(new Event("change"));
    await flush();
    expect(saves).toEqual([
      { vault: "kv-example-box", names: ["azure-cost-client-secret", "azure-other-secret"], owner: BO },
    ]);
    expect(q(node, ".vault-owner-error")?.textContent).toBe(
      "Not saved: The owner file could not be saved; check /data on the box.",
    );
  });

  it("links Rotate to the issuer's page with its hint", async () => {
    const answer = owned();
    answer.vaults[0]!.secrets[3] = {
      ...answer.vaults[0]!.secrets[3]!,
      rotate_url: "https://entra.example.com/app/credentials",
      rotate_hint: "Make a new client secret in Entra.",
    };
    const { context } = stubContext([ok(answer)], undefined, { "GET /v1/ae/vault/people": people });
    const node = await mount(context);
    await openGroup(node, "azure");
    q(node, '.vault-row[data-name="azure-cost-client-secret"] .vault-name')?.click();
    await flush();
    const links = all(node, ".vault-detail-link");
    expect(links.map((a) => a.textContent)).toEqual(["Edit in Key Vault", "Rotate"]);
    expect(links[1]?.getAttribute("href")).toBe("https://entra.example.com/app/credentials");
    expect(links[1]?.getAttribute("title")).toBe("Make a new client secret in Entra.");
  });

  it("says when the alert pass failed, and nothing when alerts are off", async () => {
    const answer = owned();
    answer.alerts = { ...answer.alerts!, last_run: "2026-10-23T13:15:00Z", last_error: "the task app did not answer" };
    const { context } = stubContext([ok(answer)]);
    const node = await mount(context);
    expect(q(node, ".vault-alerts-error")?.textContent).toBe("Expiry alerts: the task app did not answer");
    deactivateVault();
    document.getElementById("root")?.replaceChildren();
    const off = owned();
    off.alerts = { ...off.alerts!, on: false, off_reason: "not configured", last_error: "old failure" };
    const again = await mount(stubContext([ok(off)]).context);
    expect(q(again, ".vault-alerts-error")).toBeNull();
  });

  it("hides the pickers when the server cannot save owners", async () => {
    const answer = owned();
    answer.owners_editable = false;
    const { context, request } = stubContext([ok(answer)]);
    const node = await mount(context);
    await openGroup(node, "hubspot");
    expect(q(node, ".vault-set-all")).toBeNull();
    expect(q(node, ".vault-owner-btn")).toBeNull();
    q(node, '.vault-row[data-name="hubspot-access-token"] .vault-name')?.click();
    expect(q(node, ".vault-detail")).toBeNull();
    expect(q(node, '.vault-row[data-name="hubspot-access-token"] .vault-owner.is-none')).not.toBeNull();
    expect(request.mock.calls.some(([r]) => r.path === "/v1/ae/vault/people")).toBe(false);
  });

  it("opens the strip from the avatar button for a keyboard", async () => {
    const { context } = stubContext([ok(owned())], undefined, { "GET /v1/ae/vault/people": people });
    const node = await mount(context);
    await openGroup(node, "databricks");
    const button = q<HTMLButtonElement>(node, '.vault-row[data-name="databricks-cli-client-secret"] .vault-owner-btn')!;
    expect(button.getAttribute("aria-expanded")).toBe("false");
    button.click();
    await flush();
    const again = () => q<HTMLButtonElement>(node, '.vault-row[data-name="databricks-cli-client-secret"] .vault-owner-btn');
    expect(again()?.getAttribute("aria-expanded")).toBe("true");
    expect(q<HTMLSelectElement>(node, ".vault-owner-select")?.value).toBe(ANN);
    again()?.click();
    expect(q(node, ".vault-detail")).toBeNull();
  });
});
