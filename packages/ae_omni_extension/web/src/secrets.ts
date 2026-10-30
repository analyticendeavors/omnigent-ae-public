// The Admin Center's Secrets section (2026-10-20,
// https://github.com/analyticendeavors/omnigent-ae/issues/139): each key the
// server expects from deploy/pull-secrets.sh, with whether the running server
// has it. Names only: the server never sends a value, its length or a hash
// (`/v1/ae/secrets`, ae_omni_policies/secrets_health.py).
//
// Why: a key the server started without shows up as a feature failing
// somewhere else (Databricks asking for credentials, a Merge that cannot
// merge), and `pull-secrets.sh --check` says which only at a shell on the box.
//
// One card in the Fabric row's style (.card-fabric): a dot, "Server secrets"
// and a pill, the counts, then the Required and Optional keys, each with what
// it turns on. A missing required key turns the card red and says how to fix
// it; an optional one that is not set stays grey.
//
// No poll timer: the server reads its secrets once, at start, so the answer
// changes only across a restart. It is read on activate and on focus, as
// host.ts does.
//
// 2026-10-23, expiry list (the owner): the chips became one row per key, so each
// shows its Key Vault name, when the vault says it expires (amber inside 60
// days, red once past, "no expiry set" otherwise), when it last changed, and
// Edit and Rotate links (the Azure portal page, and the system that issues the
// value, naming the account it opens). The dates come from the box's daily
// reading (secrets_relay.py); this page still never sees a value. A card with
// no reading, or one older than 36 hours, says so and how to run it.
//
// 2026-10-20, the dashboard redesign: the row sits under the host's in the
// Box health card (board.ts), and the keys are small chips, each a dot and
// what the key turns on (the key's name and its state in the chip's title and
// for a screen reader), required keys first. A missing required key still
// turns the row red, its chip red, and says how to fix it. The card header's
// "Updated" time is the host's; this block keeps its own only when mounted
// alone. After every draw it hands the page its Secrets tile (tiles.ts):
// "11 of 11" keys present, red when a required one is missing.
//
// 2026-10-23, the Key Vault card concept (the owner approved): the block is
// "Secrets on this box" and starts collapsed to three lines, the counts, then
// a chip for each key that needs the owner (missing, expired, inside 60 days, or
// refused by its last call), or one green line when none does. "Show 17 keys"
// opens the rows; open or closed is remembered (sections.ts, id
// "secrets-keys"). The host's Claude token joins the rows once the box's relay
// reports it ("host" tag), and each server row says when the server's own
// calls last worked with it ("worked 2:07 PM", "failed: 401 at 1:50 PM"),
// counted since the server started. A refused required key turns the block
// red, a refused optional one or an expiry inside 60 days amber. "All vault
// secrets" jumps to the Key Vault card (vault.ts), where every secret is.

import type { ExtensionContext } from "@omnigent/extension-sdk";
import { describeError, fetchSecrets, ADMIN_RECHECK_MS, isAdminOnly, type SecretKey, type SecretsStatus } from "./api";
import { el, notice, replaceChildren } from "./dom";
import { chevronIcon } from "./icons";
import { isOpen, setOpen } from "./sections";
import { registerRefresh, setTile, staleTile, type Tile } from "./tiles";

/** The sections.ts id that remembers whether the rows are shown. */
export const KEYS_OPEN_ID = "secrets-keys";

/** A key unchanged this long gets an amber "changed" date, expiry or not. */
const UNTOUCHED_DAYS = 180;

// ── words ───────────────────────────────────────────────────────────────────

const rowsOf = (status: SecretsStatus) => [...status.required, ...status.optional];

/** Present keys whose vault expiry has passed. */
const expiredRows = (rows: SecretKey[]) =>
  rows.filter((row) => row.present && row.expiry_state === "expired");
const soonRows = (rows: SecretKey[]) =>
  rows.filter((row) => row.present && row.expiry_state === "soon");

/** Present keys whose newest server call was refused. */
const failedRows = (rows: SecretKey[]) =>
  rows.filter((row) => row.present && row.check === "failed");

export type SecretsTone = "on" | "warn" | "failed";

/**
 * Red for a missing, expired or refused required key (the host's included);
 * amber for an expiry inside 60 days or a refused optional key.
 */
export function toneOf(status: SecretsStatus): SecretsTone {
  if (
    status.missing.length ||
    status.host_missing?.length ||
    expiredRows(status.required).length ||
    failedRows(status.required).length
  ) {
    return "failed";
  }
  const all = rowsOf(status);
  return soonRows(all).length || failedRows(all).length ? "warn" : "on";
}

const TONE_CLASS: Record<SecretsTone, string> = {
  on: "fabric-on",
  warn: "fabric-on secrets-warn",
  failed: "fabric-failed",
};

export function pillText(status: SecretsStatus): string {
  const n = status.missing.length + (status.host_missing?.length ?? 0);
  if (n) return `${n} required missing`;
  const expired = expiredRows(status.required).length;
  if (expired) return `${expired} required expired`;
  const failing = failedRows(status.required).length;
  if (failing) return `${failing} required refused`;
  const soon = soonRows(rowsOf(status)).length;
  if (soon) return `${soon} expiring`;
  const refused = failedRows(rowsOf(status)).length;
  return refused ? `${refused} refused` : "All present";
}

const count = (rows: SecretKey[]) => rows.filter((row) => row.present).length;

const clock = (iso: string) =>
  new Date(iso).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });

const stamp = (iso: string) =>
  new Date(iso).toLocaleString([], {
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
  });

/** The muted facts under the name: "17 keys", "6 required", "0 expiring", when the vault was read. */
export function metaItems(status: SecretsStatus): string[] {
  const read = status.meta_read_at
    ? `vault read ${stamp(status.meta_read_at)}`
    : "vault not read yet";
  const all = rowsOf(status);
  const expiring = soonRows(all).length + expiredRows(all).length;
  return [
    `${all.length} ${all.length === 1 ? "key" : "keys"}`,
    `${status.required.length} required`,
    `${expiring} expiring`,
    read,
  ];
}

/** A key that needs the owner, as one chip: what it turns on, and why. */
export interface Problem {
  row: SecretKey;
  text: string;
  tone: "err" | "warn";
}

/**
 * Missing, expired, refused and inside-60-day keys, worst first. Unset optional
 * keys are not problems. Keys of one feature with the same trouble share a chip
 * (the three Azure spend keys refused together read as one "Azure card").
 */
export function problems(status: SecretsStatus): Problem[] {
  const required = new Set(status.required.map((row) => row.key));
  const out: Problem[] = [];
  const seen = new Set<string>();
  const push = (problem: Problem) => {
    const id = `${problem.row.use}\u0000${problem.text}`;
    if (seen.has(id)) return;
    seen.add(id);
    out.push(problem);
  };
  for (const row of rowsOf(status)) {
    const need = required.has(row.key);
    if (!row.present) {
      if (need) push({ row, text: "missing", tone: "err" });
      continue;
    }
    if (row.expiry_state === "expired") {
      push({ row, text: "expired", tone: need ? "err" : "warn" });
    } else if (row.check === "failed") {
      push({ row, text: `refused (${row.check_detail ?? "error"})`, tone: need ? "err" : "warn" });
    } else if (row.expiry_state === "soon") {
      push({ row, text: `expires in ${row.days_left ?? 0}d`, tone: "warn" });
    }
  }
  return out.sort((a, b) => (a.tone === b.tone ? 0 : a.tone === "err" ? -1 : 1));
}

/** The green line when nothing needs the owner: the nearest expiry, or that none is set. */
export function okLine(status: SecretsStatus): string {
  const next = rowsOf(status)
    .filter((row) => row.present && row.expires && (row.days_left ?? -1) >= 0)
    .sort((a, b) => (a.days_left ?? 0) - (b.days_left ?? 0))[0];
  return next
    ? `Nothing needs you. Next expiry: ${next.use}, ${day(next.expires ?? "")}.`
    : "Nothing needs you. Next expiry: none set.";
}

/** The Secrets tile: how many keys the server has, red when a required one is missing or expired. */
export function secretsTile(status: SecretsStatus | null, error: unknown): Tile {
  if (!status) {
    return {
      id: "secrets",
      value: error ? "Unknown" : "Reading...",
      note: error ? "could not be read" : "",
      health: error ? "attention" : "idle",
    };
  }
  const all = rowsOf(status);
  const n = status.missing.length + (status.host_missing?.length ?? 0);
  const expired = expiredRows(all).length;
  const soon = soonRows(all).length;
  const refused = failedRows(all).length;
  const problem = toneOf(status) === "failed";
  const note = n
    ? `${n} required missing`
    : expired
      ? `${expired} expired`
      : refused
        ? `${refused} refused`
        : soon
          ? `${soon} expiring soon`
          : count(all) === all.length
            ? "all present, none expiring"
            : "required keys present";
  return {
    id: "secrets",
    value: `${count(all)} of ${all.length}`,
    summary: `${count(all)} of ${all.length} secrets`,
    note,
    health: problem ? "problem" : expired || soon || refused ? "attention" : "ok",
  };
}

/** A key's word: an optional key the server lacks is off, not broken. */
export function keyState(row: SecretKey, required: boolean): string {
  if (row.present) return "Present";
  return required ? "Missing" : "Not set";
}

const day = (iso: string) => iso.slice(0, 10);

/** "29d ago" for an ISO time, from now. */
export function agoText(iso: string, now = Date.now()): string {
  const days = Math.max(0, Math.floor((now - Date.parse(iso)) / 86_400_000));
  return days === 0 ? "today" : `${days}d ago`;
}

const ageDays = (iso: string, now = Date.now()) =>
  Math.max(0, Math.floor((now - Date.parse(iso)) / 86_400_000));

/**
 * Where an effective expiry came from, when it is not the vault's own date:
 * "from Entra", "from Entra, latest of 2", "from Entra, read Oct 21" (an
 * Entra reading carried from an earlier run), "from GitHub".
 */
export function fromText(
  row: Pick<SecretKey, "expiry_from" | "expiry_state" | "source_count" | "source_from">,
): string | null {
  if (row.expiry_state === "none" || row.expiry_state === "unknown") return null;
  if (row.expiry_from === "github") return "from GitHub";
  if (row.expiry_from !== "entra") return null;
  const notes = ["from Entra"];
  if ((row.source_count ?? 0) > 1) notes.push(`latest of ${row.source_count}`);
  if (row.source_from) notes.push(`read ${shortDay(row.source_from)}`);
  return notes.join(", ");
}

/** "Oct 21" for an ISO time. */
export const shortDay = (iso: string) =>
  new Date(iso).toLocaleDateString([], { month: "short", day: "numeric" });

/** How the server's newest call with the key went: "worked 2:07 PM" or "failed: 401 at 1:50 PM". */
export function checkText(row: SecretKey): string | null {
  if (!row.check || !row.checked_at) return null;
  if (row.check === "ok") return `worked ${clock(row.checked_at)}`;
  return `failed: ${row.check_detail ?? "error"} at ${clock(row.checked_at)}`;
}

/** What the vault says about when a key expires. */
export function expiryText(row: SecretKey): string {
  const days = row.days_left ?? 0;
  switch (row.expiry_state) {
    case "ok":
    case "soon":
      return `expires ${day(row.expires ?? "")}, ${days} ${days === 1 ? "day" : "days"}`;
    case "expired":
      return `expired ${day(row.expires ?? "")}`;
    case "none":
      return "no expiry set";
    default:
      return "expiry not read";
  }
}

/** The notices under the keys: how to fix a missing required key, where optional ones come from. */
export function noticeTexts(status: SecretsStatus): Array<{ text: string; kind: "error" | "info" }> {
  const items: Array<{ text: string; kind: "error" | "info" }> = [];
  if (status.host_missing?.length) {
    items.push({
      kind: "error",
      text:
        `The host's env file has no ${status.host_missing.join(", ")}. Run ` +
        "deploy/pull-secrets.sh on the box, then restart the host (ae-omnigent-host).",
    });
  }
  const refused = failedRows(status.required);
  if (refused.length) {
    items.push({
      kind: "error",
      text:
        `Refused on the server's last call: ${refused.map((row) => row.key).join(", ")}. ` +
        "Rotate each where it is issued, save it in Key Vault, then run " +
        "deploy/pull-secrets.sh and restart the server.",
    });
  }
  if (status.missing.length) {
    items.push({
      kind: "error",
      text:
        `The server started without ${status.missing.join(", ")}. Run deploy/pull-secrets.sh ` +
        "on the box, then restart the server with deploy/compose.sh up -d.",
    });
  }
  const expired = expiredRows(rowsOf(status));
  if (expired.length) {
    items.push({
      kind: "error",
      text:
        `Expired in Key Vault: ${expired.map((row) => row.key).join(", ")}. Rotate each, ` +
        "then run deploy/pull-secrets.sh and restart the server.",
    });
  }
  if (status.meta_stale !== false) {
    items.push({
      kind: "info",
      text: status.meta_read_at
        ? "The expiry dates are over 36 hours old: the box's daily check has not reported. " +
          "Run python -m ae_omni_policies.secrets_relay on the box, or look at " +
          "systemctl --user status ae-omnigent-secrets-meta.timer."
        : "No expiry dates yet: the box has not reported. Install " +
          "ae-omnigent-secrets-meta.timer (deploy/README.md, Secret expiry dates).",
    });
  }
  if (status.optional.some((row) => !row.present)) {
    items.push({
      kind: "info",
      text: "Optional keys turn one feature on each. deploy/README.md says how to add them.",
    });
  }
  return items;
}

// ── the section ─────────────────────────────────────────────────────────────

/** A link only to a page on the web; the server builds these, but a page never trusts a string. */
function safeLink(
  url: string | null | undefined,
  text: string,
  label: string,
  hint?: string | null,
) {
  if (!url || !url.startsWith("https://")) return null;
  return el(
    "a",
    {
      class: "secret-link",
      href: url,
      target: "_blank",
      rel: "noopener noreferrer",
      "aria-label": label,
      title: hint ?? undefined,
    },
    text,
  );
}

/** One key: its dot, what it turns on, state, expiry, last change and the two links. */
function keyRow(row: SecretKey, required: boolean): HTMLElement {
  const state = row.expiry_state ?? "unknown";
  const off = !row.present;
  const failed = row.present && row.check === "failed";
  const needsAction = state === "expired" || state === "soon" || failed;
  const tone = off
    ? required
      ? "missing"
      : "absent"
    : state === "expired" || state === "soon"
      ? state
      : failed
        ? required
          ? "expired"
          : "soon"
        : "";
  const from = row.vault_name ? fromText(row) : null;
  const check = checkText(row);
  const old = row.updated ? ageDays(row.updated) >= UNTOUCHED_DAYS : false;
  return el(
    "li",
    {
      class: `secret-row${tone ? ` secret-${tone}` : ""}`,
      "data-key": row.key,
      "data-expiry": state,
    },
    el("span", { class: "secret-dot", "aria-hidden": "true" }),
    el(
      "div",
      { class: "secret-main" },
      el(
        "span",
        { class: "secret-use" },
        row.use,
        row.where === "host" ? el("span", { class: "secret-tag" }, "host") : null,
      ),
      el(
        "span",
        { class: "secret-ids" },
        el("code", { class: "secret-name" }, row.key),
        row.vault_name && row.vault_name !== row.key
          ? el("span", { class: "secret-vault" }, `Key Vault: ${row.vault_name}`)
          : null,
      ),
    ),
    el(
      "div",
      { class: "secret-facts" },
      el("span", { class: "secret-state" }, keyState(row, required)),
      row.vault_name
        ? el(
            "span",
            { class: `secret-expiry expiry-${state}` },
            expiryText(row),
            from ? el("span", { class: "secret-from" }, ` ${from}`) : null,
          )
        : null,
      row.updated
        ? el(
            "span",
            {
              class: `secret-changed${old ? " secret-old" : ""}`,
              title: `Changed in Key Vault ${day(row.updated)}`,
            },
            `changed ${agoText(row.updated)}`,
          )
        : null,
      check
        ? el(
            "span",
            {
              class: `secret-check check-${row.check}`,
              title: "Since the server started; it resets on a restart.",
            },
            check,
          )
        : null,
      row.enabled === false ? el("span", { class: "secret-disabled" }, "disabled in vault") : null,
    ),
    el(
      "div",
      { class: "secret-links" },
      safeLink(row.edit_url, "Edit", `Edit ${row.key} in Key Vault`),
      safeLink(row.rotate_url, "Rotate", `Rotate ${row.key}`, row.rotate_hint),
    ),
    needsAction && row.rotate_hint ? el("p", { class: "secret-hint" }, row.rotate_hint) : null,
  );
}

function keyList(title: string, rows: SecretKey[], required: boolean): HTMLElement {
  return el(
    "div",
    { class: "secret-group" },
    el("h4", { class: "secret-group-title" }, title),
    el(
      "ul",
      { class: "secret-keys", "aria-label": title },
      ...rows.map((row) => keyRow(row, required)),
    ),
  );
}

/** A chip for a key that needs the owner; it opens the key's Rotate page when there is one. */
function problemChip(problem: Problem): HTMLElement {
  const { row } = problem;
  const parts = [
    el("span", { class: "secret-dot", "aria-hidden": "true" }),
    el("span", { class: "secret-chip-use" }, row.use),
    el("span", { class: "secret-chip-why" }, problem.text),
  ];
  const label = `${row.use} (${row.key}): ${problem.text}`;
  const url = row.rotate_url;
  const cls = `secret-chip chip-${problem.tone}`;
  if (row.present && url && url.startsWith("https://")) {
    return el(
      "a",
      {
        class: cls,
        href: url,
        target: "_blank",
        rel: "noopener noreferrer",
        "aria-label": `${label}. Rotate`,
        title: row.rotate_hint ?? undefined,
        "data-key": row.key,
      },
      ...parts,
    );
  }
  return el("span", { class: cls, "aria-label": label, "data-key": row.key }, ...parts);
}

/** Show or hide the rows, and say so on the toggle. */
function applyKeysOpen(node: HTMLElement, open: boolean, total: number): void {
  const groups = node.querySelector<HTMLElement>(".secret-groups");
  const toggle = node.querySelector<HTMLButtonElement>(".secret-toggle");
  if (groups) groups.hidden = !open;
  node.classList.toggle("is-open", open);
  if (toggle) {
    toggle.setAttribute("aria-expanded", String(open));
    const label = toggle.querySelector(".secret-toggle-label");
    if (label) label.textContent = open ? "Hide keys" : `Show ${total} ${total === 1 ? "key" : "keys"}`;
  }
}

/** Scroll to the Key Vault card, opening it on a phone (board.ts folds cards there). */
function goToVault(): void {
  const vault = document.querySelector<HTMLElement>('.admin-card[data-card="vault"]');
  if (!vault) return;
  if (vault.classList.contains("is-folded")) {
    vault.querySelector<HTMLButtonElement>(".admin-card-toggle")?.click();
  }
  vault.scrollIntoView({ block: "start", behavior: "smooth" });
  vault.querySelector<HTMLElement>(".admin-card-toggle")?.focus({ preventScroll: true });
}

export function card(status: SecretsStatus): HTMLElement {
  const total = rowsOf(status).length;
  const found = problems(status);
  const toggle = el(
    "button",
    { type: "button", class: "btn btn-quiet secret-toggle", "aria-controls": "secret-groups" },
    chevronIcon(),
    el("span", { class: "secret-toggle-label" }),
  );
  const vaultLink = el(
    "button",
    { type: "button", class: "secret-vault-link" },
    "All vault secrets \u2192",
  );
  vaultLink.addEventListener("click", goToVault);
  const node = el(
    "article",
    { class: `card card-fabric secrets-card ${TONE_CLASS[toneOf(status)]}` },
    el(
      "div",
      { class: "fabric-row" },
      el("span", { class: "fabric-dot", "aria-hidden": "true" }),
      el(
        "div",
        { class: "fabric-title" },
        el("span", { class: "fabric-name" }, "Secrets on this box"),
        el("span", { class: "fabric-state" }, pillText(status)),
      ),
      el("div", { class: "fabric-acts" }),
      el(
        "p",
        { class: "fabric-meta" },
        ...metaItems(status).map((item) => el("span", { class: "fabric-meta-item" }, item)),
      ),
    ),
    found.length
      ? el(
          "div",
          { class: "secret-chips", role: "list", "aria-label": "Keys that need you" },
          ...found.map((problem) => {
            const chip = problemChip(problem);
            chip.setAttribute("role", "listitem");
            return chip;
          }),
        )
      : el(
          "p",
          { class: "secret-okline" },
          el("span", { class: "secret-dot", "aria-hidden": "true" }),
          okLine(status),
        ),
    el("div", { class: "secret-bar" }, toggle, vaultLink),
    el(
      "div",
      { class: "secret-groups", id: "secret-groups" },
      keyList("Required", status.required, true),
      keyList("Optional", status.optional, false),
    ),
  );
  applyKeysOpen(node, isOpen(KEYS_OPEN_ID, false), total);
  toggle.addEventListener("click", () => {
    const open = !node.classList.contains("is-open");
    setOpen(KEYS_OPEN_ID, open);
    applyKeysOpen(node, open, total);
  });
  for (const item of noticeTexts(status)) node.appendChild(notice(item.text, item.kind));
  return node;
}

interface SectionState {
  status: SecretsStatus | null;
  at: number;
  error: unknown;
  /** The server answered admin_only (2026-10-28): one quiet line, and no more reads. */
  adminOnly: boolean;
  /** When admin_only was last answered, for the 5 minute look-again. */
  adminSince: number;
  /** What the list last drew, so an unchanged answer is not redrawn. */
  drawn: string;
}

function render(list: HTMLElement, refreshed: HTMLElement, state: SectionState): void {
  // Every focus reads again; redrawing an unchanged card would make a screen
  // reader announce its role="alert" notice each time (dual review, 2026-10-20).
  const drawn = JSON.stringify([
    state.status,
    state.error ? describeError(state.error) : null,
    state.adminOnly,
  ]);
  if (drawn !== state.drawn) {
    state.drawn = drawn;
    draw(list, state);
  }
  refreshed.textContent = state.at
    ? `Updated ${new Date(state.at).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })}`
    : "";
  const tile = secretsTile(state.status, state.error);
  setTile(state.status ? staleTile(tile, state.error) : tile, state.at);
}

function draw(list: HTMLElement, state: SectionState): void {
  if (state.adminOnly) {
    replaceChildren(list, [el("p", { class: "meta" }, "The box's secrets are for admins.")]);
    return;
  }
  const failure = state.error
    ? notice(`The secrets could not be read (${describeError(state.error)}).`, "error")
    : null;
  if (!state.status) {
    replaceChildren(list, [failure ?? el("p", { class: "meta" }, "Reading...")]);
  } else {
    // The last good card stays, with the failure said under it.
    replaceChildren(list, [card(state.status), failure]);
  }
}

/** The secrets' block, for activateFabric to put under the host's in the Box health card; activateSecrets fills it. */
export function secretsSection(): HTMLElement {
  return el(
    "div",
    { class: "admin-block admin-secrets" },
    el("p", { class: "meta refreshed secrets-refreshed" }),
    el("section", { class: "cards fabric-list secrets-list", "aria-label": "Secrets on this box" }),
  );
}

let onFocus: (() => void) | null = null;
/** The look-again after an admin_only answer (2026-10-28). */
let recheck: ReturnType<typeof setTimeout> | null = null;
/** Bumped by every activate and deactivate, as in host.ts. */
let generation = 0;

function stopListening(): void {
  if (recheck) clearTimeout(recheck);
  recheck = null;
  if (!onFocus) return;
  window.removeEventListener("focus", onFocus);
  document.removeEventListener("visibilitychange", onFocus);
  onFocus = null;
}

/** Read and draw the section *root* (from secretsSection), again on every focus. */
export function activateSecrets(context: ExtensionContext, root: HTMLElement): void {
  const list = root.querySelector<HTMLElement>(".secrets-list");
  // In the Box health card the header's time is the host's, so this one may be gone.
  const refreshed = root.querySelector<HTMLElement>(".secrets-refreshed") ?? el("p");
  if (!list) throw new Error("Secrets section is missing");
  const mine = ++generation;
  const live = () => generation === mine;
  stopListening();
  const state: SectionState = { status: null, at: 0, error: null, adminOnly: false, adminSince: 0, drawn: "" };
  let reading: Promise<void> | null = null;
  const refresh = (): Promise<void> => {
    if (reading) return reading;
    if (state.adminOnly && Date.now() - state.adminSince < ADMIN_RECHECK_MS) return Promise.resolve();
    reading = fetchSecrets(context)
      .then(
        (answer) => {
          if (!live()) return;
          state.status = answer;
          state.at = Date.now();
          state.error = null;
          state.adminOnly = false;
        },
        (error: unknown) => {
          if (live() && isAdminOnly(error)) {
            state.adminOnly = true;
            state.adminSince = Date.now();
            state.status = null;
            state.error = null;
            return;
          }
          if (live()) state.error = error ?? new Error("Request failed");
        },
      )
      .then(() => {
        if (!live()) return;
        render(list, refreshed, state);
        if (recheck) clearTimeout(recheck);
        recheck = state.adminOnly
          ? setTimeout(() => {
              if (live()) void refresh();
            }, ADMIN_RECHECK_MS)
          : null;
      })
      .finally(() => {
        reading = null;
      });
    return reading;
  };
  render(list, refreshed, state);
  void refresh();
  registerRefresh("secrets", () => (live() ? refresh() : undefined));
  onFocus = () => {
    if (document.visibilityState === "visible") void refresh();
  };
  window.addEventListener("focus", onFocus);
  document.addEventListener("visibilitychange", onFocus);
}

export function deactivateSecrets(): void {
  generation++;
  stopListening();
}
