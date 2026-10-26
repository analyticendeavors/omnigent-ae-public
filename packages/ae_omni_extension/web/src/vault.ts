// The Admin Center's Key Vault card (2026-10-23, the concept Reid approved
// that day): every secret in both vaults, ae-secrets-vault and
// the website's vault, by name. Names and dates only: the server answers from
// the box's daily reading (`GET /v1/ae/vault`, ae_omni_policies/
// vault_inventory.py, admins only) and never sees a value.
//
// Why: the Box health card answers "does this box have what it needs" for the
// 17 keys the box pulls. The other 90-odd secrets had no view at all, two of
// them live in both vaults and must be rotated twice, and only 2 of 103 carry
// an expiry, so "not changed in 180 days" is the signal that matters today.
//
// Layout, top to bottom: a vault switch with each vault's count; four numbers
// (expired, expire in 60 days, no expiry set, untouched 180 days and more);
// "Needs you", the server's short list, worst first; then "Show all 103",
// which opens a search box, filter chips (All, On box, Both vaults, No
// expiry, 180d+) and the secrets grouped by the first word of their name.
// Groups start folded; a search opens every group with a match. Each row has
// its On box and Both vaults badges, its expiry (with "from Entra" or "from
// GitHub" when the issuer's date is the earlier), when it last changed, and
// an Edit link to its Key Vault page.
//
// The chosen vault and whether the list is open are remembered per user in
// the SDK's `storage.user` (key `vault.view`), the way sections.ts and
// boardLayout.ts remember theirs: the page runs in an opaque-origin frame,
// where localStorage throws. Every read and write is wrapped, so the card
// works the same without it.
//
// The card hands the page its Key Vault tile (tiles.ts): "103 + 10", amber
// when a secret has expired, expires inside 60 days or has two copies that
// changed apart. It reads on activate and on focus; the reading changes once
// a day, so it has no timer.
//
// Owners (2026-10-23, secret expiry alerts): every secret can have an owner,
// an OmniGent user, so the server's 30 day, 7 day and expired alerts reach the
// person who rotates it; a secret with no owner alerts Reid. Rows and Needs
// you lines carry the owner's initials (a dashed "?" for none), the chips gain
// one per owner plus "No owner", and a click on a row opens a strip under it
// with an owner select and the Edit link. A group header's "Set owner for all
// N" sets every secret in the group at once. People come from the server's
// admin-only users list, read the first time a select opens; when it fails or
// is empty the select still offers the owners already set, so nobody's
// address is ever typed into the page. The pickers hide when the server cannot
// save owners (`owners_editable` false). The tile adds when the next alarm
// rings and how many secrets have no owner, so the gaps close over time.

import type { ExtensionContext } from "@omnigent/extension-sdk";
import {
  AeRequestError,
  describeError,
  fetchVault,
  fetchVaultPeople,
  setVaultOwner,
  type VaultInventory,
  type VaultNeed,
  type VaultSecret,
  type VaultView,
} from "./api";
import { el, notice, replaceChildren } from "./dom";
import { fromText, shortDay } from "./secrets";
import { chevronIcon, pencilIcon, searchIcon, serverIcon } from "./icons";
import { registerRefresh, setTile, staleTile, type Tile } from "./tiles";

export const VIEW_KEY = "vault.view";

/** The chips; an owner's chip is "owner:<id>" and "nobody" is No owner (2026-10-23). */
export type Filter = "all" | "box" | "both" | "none" | "old" | "nobody" | `owner:${string}`;

export const FILTERS: ReadonlyArray<{ id: Filter; label: string }> = [
  { id: "all", label: "All" },
  { id: "box", label: "On box" },
  { id: "both", label: "Both vaults" },
  { id: "none", label: "No expiry" },
  { id: "old", label: "180d+" },
];

export const NO_OWNER = "No owner (alerts go to Reid)";

// ── words ───────────────────────────────────────────────────────────────────

/** An owner id's local part in words: "reid.havens@..." is "Reid Havens". */
export function ownerName(id: string): string {
  const words = (id.split("@")[0] ?? "").split(/[._+-]+/).filter(Boolean);
  if (!words.length) return id;
  return words.map((word) => word[0]!.toUpperCase() + word.slice(1)).join(" ");
}

/** The avatar's letters: "reid.havens" is "RH"; a one-word name gives one letter. */
export function ownerInitials(id: string): string {
  const words = ownerName(id).split(" ");
  const last = words.length > 1 ? (words[words.length - 1]![0] ?? "") : "";
  return `${words[0]?.[0] ?? "?"}${last}`.toUpperCase();
}

/** A stable hue per owner, so one person keeps one colour on every row and every load. */
export function ownerHue(id: string): number {
  let hash = 0;
  for (const ch of id.toLowerCase()) hash = (hash * 31 + ch.charCodeAt(0)) >>> 0;
  return hash % 360;
}

/** "Mar 24" in the browser's clock. */
function monthDay(at: Date): string {
  return at.toLocaleDateString("en-US", { month: "short", day: "numeric" });
}

/** "alarm Mar 24, 9:00 AM" in the browser's clock; null without an alarm or with a bad date. */
export function alarmWords(iso: string | null | undefined): string | null {
  if (!iso) return null;
  const at = new Date(iso);
  if (Number.isNaN(at.getTime())) return null;
  // ICU puts a narrow no-break space before AM; a plain one reads and tests the same.
  const time = at.toLocaleTimeString("en-US", { hour: "numeric", minute: "2-digit" }).replace(/\s/g, " ");
  return `alarm ${monthDay(at)}, ${time}`;
}

/** Whether the server sends owners: a server older than them leaves `owner` off every row. */
export function hasOwners(view: VaultView): boolean {
  return view.secrets.some((row) => row.owner !== undefined);
}

/** One chip per owner present, most secrets first, then "No owner"; none from an older server. */
export function ownerFilters(view: VaultView): Array<{ id: Filter; label: string; owner: string | null }> {
  if (!hasOwners(view)) return [];
  const counts = new Map<string, number>();
  for (const row of view.secrets) {
    if (row.owner) counts.set(row.owner, (counts.get(row.owner) ?? 0) + 1);
  }
  const people = [...counts.keys()].sort(
    (a, b) => counts.get(b)! - counts.get(a)! || ownerName(a).localeCompare(ownerName(b)),
  );
  return [
    ...people.map((id) => ({ id: `owner:${id}` as Filter, label: ownerName(id).split(" ")[0]!, owner: id })),
    { id: "nobody", label: "No owner", owner: null },
  ];
}

export function matches(row: VaultSecret, filter: Filter, query: string): boolean {
  const q = query.trim().toLowerCase();
  if (q && !row.name.toLowerCase().includes(q) && !(row.box_key ?? "").toLowerCase().includes(q)) {
    return false;
  }
  switch (filter) {
    case "box":
      return row.on_box;
    case "both":
      return row.both_vaults;
    case "none":
      return row.expiry_state === "none";
    case "old":
      return row.untouched;
    case "nobody":
      return row.owner === null;
    default:
      return filter.startsWith("owner:") ? row.owner === filter.slice("owner:".length) : true;
  }
}

export function filterCount(view: VaultView, filter: Filter): number {
  return view.secrets.filter((row) => matches(row, filter, "")).length;
}

/** A row's expiry: "no expiry", "2028-10-23 · 730d", "expired 2026-10-01". */
export function expiryWords(row: VaultSecret): string {
  const date = (row.expires ?? "").slice(0, 10);
  switch (row.expiry_state) {
    case "none":
      return "no expiry";
    case "expired":
      return `expired ${date}`;
    case "ok":
    case "soon":
      return `${date} · ${row.days_left ?? 0}d`;
    default:
      return "expiry not read";
  }
}

/** "from Entra", "from Entra, latest of 2", "from GitHub" (the same words as the box rows). */
export function fromWords(row: VaultSecret): string | null {
  return fromText(row);
}

/** Whether a vault needs a look: an expiry passed or inside 60 days, or a two-vault copy out of step. */
export function vaultNeedsLook(view: VaultView): boolean {
  return (
    view.counts.expired > 0 ||
    view.counts.soon > 0 ||
    view.needs_you.some((need) => need.kind === "both" || need.kind === "disabled")
  );
}

/**
 * The Key Vault tile: each vault's count, the expiring and untouched totals.
 * Since 2026-10-23 it also says when the next expiry alarm rings and how many
 * secrets have no owner; those words take the untouched count's place, which
 * moves to the tooltip, so the note stays one line.
 */
export function vaultTile(inventory: VaultInventory | null, error: unknown): Tile {
  if (!inventory) {
    const forbidden = error instanceof AeRequestError && error.status === 403;
    return {
      id: "vault",
      value: forbidden ? "Admins" : error ? "Unknown" : "Reading...",
      note: forbidden ? "admins only" : error ? "could not be read" : "",
      health: error && !forbidden ? "attention" : "idle",
    };
  }
  if (!inventory.vaults.length) {
    return { id: "vault", value: "Not read", note: "the box has not reported", health: "attention" };
  }
  const expiring = inventory.vaults.reduce((n, v) => n + v.counts.soon + v.counts.expired, 0);
  const old = inventory.vaults.reduce((n, v) => n + v.counts.untouched, 0);
  const value = inventory.vaults.map((v) => String(v.count)).join(" + ");
  const look = inventory.vaults.some(vaultNeedsLook) || inventory.stale;
  const noOwner = inventory.vaults.reduce((n, v) => n + (v.counts.no_owner ?? 0), 0);
  const next = inventory.alerts?.next ? new Date(inventory.alerts.next.alarm_at) : null;
  const extras = [
    next && !Number.isNaN(next.getTime()) ? `alarm set ${monthDay(next)}` : null,
    noOwner > 0 ? `${noOwner} ${noOwner === 1 ? "has" : "have"} no owner` : null,
  ].filter((words): words is string => words !== null);
  const counts = inventory.vaults.map((v) => `${v.vault}: ${v.count}`).join(", ");
  return {
    id: "vault",
    value,
    summary: `${value} vault secrets`,
    note: extras.length
      ? [`${expiring} expiring`, ...extras].join(" · ")
      : `${expiring} expiring, ${old} untouched 180d+`,
    health: look ? "attention" : "ok",
    title: extras.length ? `${counts}; ${old} untouched 180d+` : counts,
  };
}

// ── remembered view ─────────────────────────────────────────────────────────

interface Remembered {
  vault: string | null;
  open: boolean;
}

let remembered: Remembered = { vault: null, open: false };
let persist: (() => void) | null = null;

/** Read the remembered vault and list state; the defaults without it. */
export async function loadVaultView(context: ExtensionContext): Promise<void> {
  persist = () => {
    try {
      context.storage.user.set(VIEW_KEY, remembered).catch(() => {
        // storage.user unavailable: kept in memory until the page closes.
      });
    } catch {
      // The same, refused before a promise was made.
    }
  };
  try {
    const found = await context.storage.user.get<unknown>(VIEW_KEY);
    if (found && typeof found === "object" && !Array.isArray(found)) {
      const { vault, open } = found as { vault?: unknown; open?: unknown };
      remembered = {
        vault: typeof vault === "string" ? vault : remembered.vault,
        open: typeof open === "boolean" ? open : remembered.open,
      };
    }
  } catch {
    // Same as a failed write: the defaults.
  }
}

function remember(next: Partial<Remembered>): void {
  remembered = { ...remembered, ...next };
  persist?.();
}

/** For tests: forget the remembered view. */
export function resetVaultView(): void {
  remembered = { vault: null, open: false };
  persist = null;
}

// ── the card ────────────────────────────────────────────────────────────────

interface State {
  inventory: VaultInventory | null;
  error: unknown;
  at: number;
  filter: Filter;
  query: string;
  /** Groups Reid opened, per vault ("vault/group"). */
  openGroups: Set<string>;
  /** Groups Reid folded while a search or filter opens every match; cleared when it changes. */
  closedGroups: Set<string>;
  /** The open owner strip: "row:vault/name" under a row, "group:vault/group" under a group header. */
  picker: string | null;
  /** Whether a save is in flight, and the last save's error for the open strip. */
  saving: boolean;
  saveError: string | null;
  /** OmniGent users, read the first time a strip opens; null until then or after a failure. */
  people: string[] | null;
  peopleState: "unread" | "reading" | "read" | "failed";
  /** What a strip calls: activateVault fills it, since only it holds the context. */
  act: { save: (vault: string, names: string[], owner: string | null) => void; people: () => void } | null;
}

interface Parts {
  root: HTMLElement;
  status: HTMLElement;
  tabs: HTMLElement;
  stats: HTMLElement;
  needs: HTMLElement;
  toggle: HTMLButtonElement;
  list: HTMLElement;
  search: HTMLInputElement;
  chips: HTMLElement;
  groups: HTMLElement;
  refreshed: HTMLElement;
}

function safeEdit(url: string, name: string): HTMLElement | null {
  if (!url.startsWith("https://")) return null;
  return el(
    "a",
    {
      class: "vault-edit",
      href: url,
      target: "_blank",
      rel: "noopener noreferrer",
      "aria-label": `Edit ${name} in Key Vault`,
      title: "Edit in Key Vault",
    },
    pencilIcon(),
  );
}

function chosen(inventory: VaultInventory): VaultView | null {
  return (
    inventory.vaults.find((v) => v.vault === remembered.vault) ??
    inventory.vaults.find((v) => v.vault === inventory.box_vault) ??
    inventory.vaults[0] ??
    null
  );
}

/** The owner's initials on their own colour, or a dashed "?" for no owner. */
function avatar(owner: string | null, attrs: Record<string, string> = {}, tag: "span" | "button" = "span"): HTMLElement {
  const who = owner ? ownerName(owner) : NO_OWNER;
  const node = el(
    tag,
    {
      role: tag === "span" ? "img" : undefined,
      "aria-label": `Owner: ${who}`,
      title: who,
      ...attrs,
      class: `vault-owner${owner ? "" : " is-none"}${attrs.class ? ` ${attrs.class}` : ""}`,
      "data-owner": owner ?? "",
    },
    owner ? ownerInitials(owner) : "?",
  );
  // A custom property through the CSSOM, the way header.ts sets its bar.
  if (owner) node.style.setProperty("--owner-hue", String(ownerHue(owner)));
  return node;
}

function needRow(need: VaultNeed, row: VaultSecret | undefined): HTMLElement {
  const tone = need.kind === "expired" ? "err" : need.kind === "untouched" ? "muted" : "warn";
  const owner = need.owner !== undefined ? need.owner : row?.owner;
  const alarm = alarmWords(need.alarm_at ?? row?.alert?.alarm_at);
  return el(
    "li",
    { class: `vault-need need-${tone}`, "data-name": need.name, "data-kind": need.kind },
    el("span", { class: "secret-dot", "aria-hidden": "true" }),
    el(
      "div",
      { class: "vault-need-text" },
      el("code", { class: "vault-name" }, need.name),
      el("span", { class: "vault-why" }, alarm ? `${need.text} · ${alarm}` : need.text),
    ),
    el(
      "span",
      { class: "vault-need-tail" },
      row ? safeEdit(row.edit_url, need.name) : null,
      owner !== undefined ? avatar(owner) : null,
    ),
  );
}

/** How a row's owner shows: read-only, or a button that opens its strip. */
interface RowOwner {
  editable: boolean;
  open: boolean;
  toggle: () => void;
}

function vaultRow(row: VaultSecret, owner?: RowOwner): HTMLElement {
  const from = fromWords(row);
  let badge: HTMLElement | null = null;
  if (row.owner !== undefined) {
    const who = row.owner ? ownerName(row.owner) : "no owner";
    badge = owner?.editable
      ? avatar(
          row.owner,
          {
            type: "button",
            class: "vault-owner-btn",
            "aria-expanded": String(owner.open),
            "aria-label": `Owner of ${row.name}: ${who}. Change owner`,
          },
          "button",
        )
      : avatar(row.owner);
  }
  const node = el(
    "li",
    {
      class:
        `vault-row expiry-${row.expiry_state}${row.enabled ? "" : " vault-disabled"}` +
        `${owner?.editable ? " is-pickable" : ""}${owner?.open ? " is-picking" : ""}`,
      "data-name": row.name,
    },
    el(
      "div",
      { class: "vault-row-main" },
      el("code", { class: "vault-name", title: row.box_key ? `On the box as ${row.box_key}` : undefined }, row.name),
      el(
        "span",
        { class: "vault-badges" },
        row.on_box ? el("span", { class: "vault-badge badge-box" }, serverIcon(), "On box") : null,
        row.both_vaults ? el("span", { class: "vault-badge badge-both" }, "Both vaults") : null,
        row.enabled ? null : el("span", { class: "vault-badge badge-off" }, "Disabled"),
      ),
    ),
    el(
      "div",
      { class: "vault-row-facts" },
      el(
        "span",
        { class: `vault-expiry state-${row.expiry_state}` },
        expiryWords(row),
        from ? el("span", { class: "secret-from" }, ` ${from}`) : null,
      ),
      row.updated_days != null
        ? el(
            "span",
            {
              class: `vault-changed${row.untouched ? " secret-old" : ""}`,
              title: row.updated ? `Changed ${row.updated.slice(0, 10)}` : undefined,
            },
            row.updated_days === 0 ? "changed today" : `${row.updated_days}d ago`,
          )
        : null,
      safeEdit(row.edit_url, row.name),
      badge,
    ),
  );
  if (owner?.editable) {
    // The whole row opens the strip for a mouse; the avatar button is the keyboard's way in.
    node.addEventListener("click", (event) => {
      const target = event.target as Element | null;
      if (target?.closest("a") || (target?.closest("button") && target.closest("button") !== badge)) return;
      owner.toggle();
    });
  }
  return node;
}

/** Everyone the select offers: the users list, plus every owner already set, by name. */
function peopleFor(state: State, current: string | null): string[] {
  const ids = new Set(state.people ?? []);
  for (const v of state.inventory?.vaults ?? []) {
    for (const row of v.secrets) if (row.owner) ids.add(row.owner);
  }
  if (current) ids.add(current);
  return [...ids].sort((a, b) => ownerName(a).localeCompare(ownerName(b)) || a.localeCompare(b));
}

const MIXED = "mixed";

/**
 * The owner strip under a row, or under a group header for "Set owner for
 * all N": a select that saves on change, the row's Edit link, and one status
 * line (saving, the last save's error, or a missing people list).
 */
function ownerStrip(
  state: State,
  vault: string,
  names: string[],
  current: string | null | typeof MIXED,
  edit: VaultSecret | null,
): HTMLElement {
  const select = el(
    "select",
    { class: "vault-owner-select", "aria-label": names.length > 1 ? `Owner for all ${names.length}` : "Owner" },
    current === MIXED ? el("option", { value: MIXED, disabled: true, selected: true }, "Mixed owners") : null,
    el("option", { value: "", selected: current === null }, NO_OWNER),
    ...peopleFor(state, current === MIXED ? null : current).map((id) =>
      el("option", { value: id, title: id, selected: id === current }, ownerName(id)),
    ),
  );
  select.disabled = state.saving;
  select.addEventListener("change", () => {
    state.act?.save(vault, names, select.value === "" ? null : select.value);
  });
  const status = state.saving
    ? el("span", { class: "meta vault-owner-status" }, "Saving...")
    : state.saveError
      ? el("span", { class: "vault-owner-status vault-owner-error", role: "alert" }, state.saveError)
      : state.peopleState === "failed" || (state.peopleState === "read" && !state.people?.length)
        ? el("span", { class: "meta vault-owner-status" }, "No people list from the server; showing the owners already set.")
        : null;
  const link = (url: string | undefined, text: string, title?: string) =>
    url?.startsWith("https://")
      ? el(
          "a",
          {
            class: "vault-detail-link",
            href: url,
            target: "_blank",
            rel: "noopener noreferrer",
            title,
          },
          text === "Rotate" ? null : pencilIcon(),
          text,
        )
      : null;
  return el(
    "div",
    { class: "vault-detail" },
    el("label", { class: "vault-owner-pick" }, el("span", { class: "vault-owner-l" }, "Owner"), select),
    edit ? link(edit.edit_url, "Edit in Key Vault") : null,
    // Rotate shows only when it goes somewhere else: the issuer's page, not the vault's own.
    edit && edit.rotate_url && edit.rotate_url !== edit.edit_url
      ? link(edit.rotate_url, "Rotate", edit.rotate_hint ?? undefined)
      : null,
    status,
  );
}

function drawTabs(parts: Parts, state: State, inventory: VaultInventory, view: VaultView): void {
  replaceChildren(
    parts.tabs,
    inventory.vaults.map((v) => {
      const tab = el(
        "button",
        {
          type: "button",
          class: `vault-tab${v.vault === view.vault ? " is-on" : ""}`,
          "aria-pressed": String(v.vault === view.vault),
          "data-vault": v.vault,
        },
        el("span", { class: "vault-tab-name" }, v.vault),
        el("span", { class: "vault-tab-n" }, String(v.count)),
      );
      tab.addEventListener("click", () => {
        if (v.vault === view.vault) return;
        remember({ vault: v.vault });
        state.picker = null;
        draw(parts, state);
      });
      return tab;
    }),
  );
}

function drawStats(parts: Parts, view: VaultView): void {
  const stat = (n: number, label: string, tone: string) =>
    el(
      "div",
      { class: `vault-stat${n ? ` stat-${tone}` : ""}` },
      el("b", { class: "vault-stat-n" }, String(n)),
      el("span", { class: "vault-stat-l" }, label),
    );
  replaceChildren(parts.stats, [
    stat(view.counts.expired, "expired", "err"),
    stat(view.counts.soon, "expire in 60d", "warn"),
    stat(view.counts.no_expiry, "no expiry set", "muted"),
    stat(view.counts.untouched, "untouched 180d+", "warn"),
  ]);
}

function drawNeeds(parts: Parts, view: VaultView): void {
  const byName = new Map(view.secrets.map((row) => [row.name, row]));
  const more = view.needs_you_total - view.needs_you.length;
  replaceChildren(parts.needs, [
    el("h3", { class: "vault-h" }, "Needs you"),
    view.needs_you.length
      ? el(
          "ul",
          { class: "vault-needs", "aria-label": "Needs you" },
          ...view.needs_you.map((need) => needRow(need, byName.get(need.name))),
        )
      : el(
          "p",
          { class: "secret-okline" },
          el("span", { class: "secret-dot", "aria-hidden": "true" }),
          "Nothing in this vault needs you.",
        ),
    more > 0 ? el("p", { class: "meta vault-more" }, `and ${more} more in the full list`) : null,
  ]);
}

function drawList(parts: Parts, state: State, view: VaultView): void {
  const owners = ownerFilters(view);
  // An owner's chip goes when their last secret moves to someone else, or on another vault.
  if (state.filter.startsWith("owner:") && !owners.some((chip) => chip.id === state.filter)) {
    state.filter = "all";
  }
  const editable = state.inventory?.owners_editable !== false && hasOwners(view);
  const chips: Array<{ id: Filter; label: string; owner?: string | null }> = [...FILTERS, ...owners];
  replaceChildren(
    parts.chips,
    chips.map(({ id, label, owner }) => {
      const chip = el(
        "button",
        {
          type: "button",
          class: `vault-chip${state.filter === id ? " is-on" : ""}${owner !== undefined ? " vault-chip-owner" : ""}`,
          "aria-pressed": String(state.filter === id),
          "data-filter": id,
        },
        owner !== undefined ? avatar(owner, { "aria-hidden": "true", role: "presentation" }) : null,
        label,
        el("span", { class: "vault-chip-n" }, String(filterCount(view, id))),
      );
      chip.addEventListener("click", () => {
        state.filter = id;
        state.closedGroups.clear();
        drawList(parts, state, view);
      });
      return chip;
    }),
  );
  const searching = state.query.trim() !== "" || state.filter !== "all";
  const groups = view.groups.flatMap((group) => {
    const rows = view.secrets.filter(
      (row) => row.group === group.id && matches(row, state.filter, state.query),
    );
    if (!rows.length) return [];
    const key = `${view.vault}/${group.id}`;
    const open = searching ? !state.closedGroups.has(key) : state.openGroups.has(key);
    const head = el(
      "button",
      {
        type: "button",
        class: "vault-group-head",
        "aria-expanded": String(open),
        "data-group": group.id,
      },
      chevronIcon(),
      el("b", { class: "vault-group-label" }, group.label),
      el("span", { class: "vault-group-n" }, String(rows.length)),
      group.oldest_days != null
        ? el("span", { class: "vault-group-old" }, `oldest change ${group.oldest_days}d`)
        : null,
    );
    head.addEventListener("click", () => {
      // Searching opens every group with a match; a click then folds or opens just this one.
      const set = searching ? state.closedGroups : state.openGroups;
      if (set.has(key)) set.delete(key);
      else set.add(key);
      drawList(parts, state, view);
    });
    const pick = (id: string) => () => {
      state.picker = state.picker === id ? null : id;
      state.saveError = null;
      if (state.picker) state.act?.people();
      drawList(parts, state, view);
    };
    const items = rows.flatMap((row) => {
      const id = `row:${view.vault}/${row.name}`;
      const node = vaultRow(row, { editable, open: state.picker === id, toggle: pick(id) });
      if (!editable || state.picker !== id) return [node];
      return [node, el("li", { class: "vault-detail-row" }, ownerStrip(state, view.vault, [row.name], row.owner ?? null, row))];
    });
    const list = el("ul", { class: "vault-rows", "aria-label": group.label }, ...items);
    list.hidden = !open;
    // "Set owner for all N" covers the whole group, whatever the search or chip shows.
    const everyone = view.secrets.filter((row) => row.group === group.id);
    const groupId = `group:${key}`;
    let setAll: HTMLElement | null = null;
    let strip: HTMLElement | null = null;
    if (editable && open) {
      setAll = el(
        "button",
        {
          type: "button",
          class: "linkish vault-set-all",
          "aria-expanded": String(state.picker === groupId),
          "data-group": group.id,
        },
        `Set owner for all ${everyone.length}`,
      );
      setAll.addEventListener("click", pick(groupId));
      if (state.picker === groupId) {
        const first = everyone[0]?.owner ?? null;
        const same = everyone.every((row) => (row.owner ?? null) === first);
        strip = ownerStrip(state, view.vault, everyone.map((row) => row.name), same ? first : MIXED, null);
      }
    }
    return [
      el(
        "li",
        { class: `vault-group${open ? " is-open" : ""}` },
        el("div", { class: "vault-group-bar" }, head, setAll),
        strip,
        list,
      ),
    ];
  });
  replaceChildren(parts.groups, [
    groups.length
      ? el("ul", { class: "vault-groups" }, ...groups)
      : el("p", { class: "meta vault-empty" }, "No secret matches."),
  ]);
}

function applyOpen(parts: Parts, view: VaultView | null): void {
  const open = remembered.open && view !== null;
  parts.list.hidden = !open;
  parts.toggle.hidden = view === null;
  parts.toggle.setAttribute("aria-expanded", String(open));
  parts.root.classList.toggle("is-open", open);
  const label = parts.toggle.querySelector(".vault-toggle-label");
  if (label && view) label.textContent = open ? "Hide the list" : `Show all ${view.count}`;
}

function draw(parts: Parts, state: State): void {
  const inventory = state.inventory;
  const failure = state.error
    ? state.error instanceof AeRequestError && state.error.status === 403
      ? notice("Only an OmniGent admin can see the Key Vault inventory.", "info")
      : notice(`The Key Vault inventory could not be read (${describeError(state.error)}).`, "error")
    : null;
  const view = inventory ? chosen(inventory) : null;
  const body = parts.root.querySelector<HTMLElement>(".vault-body");
  if (body) body.hidden = view === null;
  const notes: Array<HTMLElement | null> = [failure];
  if (inventory && !view) {
    notes.push(
      notice(
        "No Key Vault reading yet: the box has not reported. Run python -m " +
          "ae_omni_policies.secrets_relay on the box (deploy/README.md, Secret expiry dates).",
        "info",
      ),
    );
  } else if (inventory?.stale) {
    notes.push(
      notice(
        "This reading is over 36 hours old: the box's daily check has not reported. " +
          "Look at systemctl --user status ae-omnigent-secrets-meta.timer on the box.",
        "info",
      ),
    );
  }
  if (!inventory && !state.error) notes.push(el("p", { class: "meta" }, "Reading..."));
  // The alert pass's last failure (2026-10-23); with alerts off the tile already says nothing new.
  if (inventory?.alerts?.on && inventory.alerts.last_error) {
    notes.push(el("p", { class: "meta vault-alerts-error" }, `Expiry alerts: ${inventory.alerts.last_error}`));
  }
  if (view?.read_from) {
    notes.push(
      el(
        "p",
        { class: "meta vault-carried" },
        `${view.vault} could not be read on the last run; showing its list from ${shortDay(view.read_from)}.`,
      ),
    );
  }
  replaceChildren(parts.status, notes);
  if (inventory && view) {
    drawTabs(parts, state, inventory, view);
    drawStats(parts, view);
    drawNeeds(parts, view);
    drawList(parts, state, view);
  }
  applyOpen(parts, view);
  parts.refreshed.textContent = inventory?.read_at
    ? `Read ${new Date(inventory.read_at).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })}`
    : "";
  const tile = vaultTile(inventory, state.error);
  setTile(inventory ? staleTile(tile, state.error) : tile, state.at || undefined);
}

/** The card's body, for activateFabric to put in an adminCard; activateVault fills it. */
export function vaultSection(): HTMLElement {
  const search = el("input", {
    type: "search",
    class: "vault-search-input",
    placeholder: "Search names",
    "aria-label": "Search secret names",
    autocomplete: "off",
    spellcheck: "false",
  });
  const toggle = el(
    "button",
    { type: "button", class: "btn btn-quiet vault-toggle", "aria-controls": "vault-list" },
    chevronIcon(),
    el("span", { class: "vault-toggle-label" }),
  );
  toggle.hidden = true;
  const list = el(
    "div",
    { class: "vault-list", id: "vault-list" },
    el("label", { class: "vault-search" }, searchIcon(), search),
    el("div", { class: "vault-chips", role: "group", "aria-label": "Filter" }),
    el("div", { class: "vault-groups-wrap" }),
  );
  list.hidden = true;
  return el(
    "div",
    { class: "admin-block admin-vault" },
    el("p", { class: "meta refreshed vault-refreshed" }),
    el("div", { class: "vault-status" }),
    el(
      "div",
      { class: "vault-body" },
      el("div", { class: "vault-tabs", role: "group", "aria-label": "Vault" }),
      el("div", { class: "vault-stats" }),
      el("div", { class: "vault-needs-wrap" }),
      el("div", { class: "vault-bar" }, toggle),
      list,
    ),
  );
}

function partsOf(root: HTMLElement): Parts {
  const find = <T extends HTMLElement>(selector: string): T => {
    const found = root.querySelector<T>(selector);
    if (!found) throw new Error(`Key Vault card is missing ${selector}`);
    return found;
  };
  return {
    root,
    status: find(".vault-status"),
    tabs: find(".vault-tabs"),
    stats: find(".vault-stats"),
    needs: find(".vault-needs-wrap"),
    toggle: find<HTMLButtonElement>(".vault-toggle"),
    list: find(".vault-list"),
    search: find<HTMLInputElement>(".vault-search-input"),
    chips: find(".vault-chips"),
    groups: find(".vault-groups-wrap"),
    // The card header carries the time when activateFabric moved it there.
    refreshed:
      root.querySelector<HTMLElement>(".vault-refreshed") ??
      root.closest(".admin-card")?.querySelector<HTMLElement>(".vault-refreshed") ??
      el("p"),
  };
}

let onFocus: (() => void) | null = null;
let generation = 0;

function stopListening(): void {
  if (!onFocus) return;
  window.removeEventListener("focus", onFocus);
  document.removeEventListener("visibilitychange", onFocus);
  onFocus = null;
}

/** Read and draw the card *root* (from vaultSection, or a card holding it), again on every focus. */
export function activateVault(context: ExtensionContext, root: HTMLElement): void {
  const parts = partsOf(root);
  const mine = ++generation;
  const live = () => generation === mine;
  stopListening();
  const state: State = {
    inventory: null,
    error: null,
    at: 0,
    filter: "all",
    query: "",
    openGroups: new Set(),
    closedGroups: new Set(),
    picker: null,
    saving: false,
    saveError: null,
    people: null,
    peopleState: "unread",
    act: null,
  };
  const redrawList = () => {
    const view = state.inventory ? chosen(state.inventory) : null;
    if (view) drawList(parts, state, view);
  };
  parts.toggle.addEventListener("click", () => {
    remember({ open: !remembered.open });
    const view = state.inventory ? chosen(state.inventory) : null;
    applyOpen(parts, view);
    if (remembered.open) parts.search.focus({ preventScroll: true });
  });
  parts.search.addEventListener("input", () => {
    state.query = parts.search.value;
    state.closedGroups.clear();
    const view = state.inventory ? chosen(state.inventory) : null;
    if (view) drawList(parts, state, view);
  });
  let reading: Promise<void> | null = null;
  let drawn = "";
  const refresh = (): Promise<void> => {
    if (reading) return reading;
    reading = fetchVault(context)
      .then(
        (answer) => {
          if (!live()) return;
          state.inventory = answer;
          state.at = Date.now();
          state.error = null;
        },
        (error: unknown) => {
          if (live()) state.error = error ?? new Error("Request failed");
        },
      )
      .then(() => {
        if (!live()) return;
        // An unchanged answer is not redrawn, so an open group or a focused row stays put.
        const next = JSON.stringify([state.inventory, state.error ? describeError(state.error) : null]);
        if (next !== drawn) {
          drawn = next;
          draw(parts, state);
        }
      })
      .finally(() => {
        reading = null;
      });
    return reading;
  };
  state.act = {
    // The users list is read once per card, the first time a strip opens; a
    // failure leaves the owners already set to choose from.
    people: () => {
      if (state.peopleState !== "unread") return;
      state.peopleState = "reading";
      void fetchVaultPeople(context)
        .then(
          (answer) => {
            if (!live()) return;
            const users = Array.isArray(answer?.users) ? answer.users : [];
            state.people = users
              .map((user) => user?.id)
              .filter((id): id is string => typeof id === "string" && id !== "");
            state.peopleState = "read";
          },
          () => {
            if (live()) state.peopleState = "failed";
          },
        )
        .then(() => {
          if (live() && state.picker) redrawList();
        });
    },
    save: (vault, names, owner) => {
      if (state.saving) return;
      state.saving = true;
      state.saveError = null;
      redrawList();
      void setVaultOwner(context, vault, names, owner).then(
        () => {
          if (!live()) return;
          state.saving = false;
          state.picker = null;
          redrawList();
          // A reading already in flight predates the save, so read again after it.
          void (reading ?? Promise.resolve()).then(() => refresh());
        },
        (error: unknown) => {
          if (!live()) return;
          state.saving = false;
          state.saveError = `Not saved: ${describeError(error)}`;
          redrawList();
        },
      );
    },
  };
  draw(parts, state);
  void refresh();
  registerRefresh("vault", () => (live() ? refresh() : undefined));
  onFocus = () => {
    if (document.visibilityState === "visible") void refresh();
  };
  window.addEventListener("focus", onFocus);
  document.addEventListener("visibilitychange", onFocus);
}

export function deactivateVault(): void {
  generation++;
  stopListening();
}
