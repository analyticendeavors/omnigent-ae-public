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
// 2026-10-20, the dashboard redesign: the row sits under the host's in the
// Box health card (board.ts), and the keys are small chips, each a dot and
// what the key turns on (the key's name and its state in the chip's title and
// for a screen reader), required keys first. A missing required key still
// turns the row red, its chip red, and says how to fix it. The card header's
// "Updated" time is the host's; this block keeps its own only when mounted
// alone. After every draw it hands the page its Secrets tile (tiles.ts):
// "11 of 11" keys present, red when a required one is missing.

import type { ExtensionContext } from "@omnigent/extension-sdk";
import { describeError, fetchSecrets, type SecretKey, type SecretsStatus } from "./api";
import { el, notice, replaceChildren } from "./dom";
import type { FabricTone } from "./icons";
import { registerRefresh, setTile, staleTile, type Tile } from "./tiles";

// ── words ───────────────────────────────────────────────────────────────────

export function toneOf(status: SecretsStatus): FabricTone {
  return status.missing.length ? "failed" : "on";
}

export function pillText(status: SecretsStatus): string {
  const n = status.missing.length;
  return n ? `${n} required missing` : "All required present";
}

const count = (rows: SecretKey[]) => rows.filter((row) => row.present).length;

/** The muted facts under the name. */
export function metaItems(status: SecretsStatus): string[] {
  return [
    `${count(status.required)} of ${status.required.length} required`,
    `${count(status.optional)} of ${status.optional.length} optional`,
    "As of the server's last start",
  ];
}

/** The Secrets tile: how many keys the server has, red when a required one is missing. */
export function secretsTile(status: SecretsStatus | null, error: unknown): Tile {
  if (!status) {
    return {
      id: "secrets",
      value: error ? "Unknown" : "Reading...",
      note: error ? "could not be read" : "",
      health: error ? "attention" : "idle",
    };
  }
  const all = [...status.required, ...status.optional];
  const n = status.missing.length;
  return {
    id: "secrets",
    value: `${count(all)} of ${all.length}`,
    summary: `${count(all)} of ${all.length} secrets`,
    note: n ? `${n} required missing` : count(all) === all.length ? "all keys loaded" : "required keys loaded",
    health: n ? "problem" : "ok",
  };
}

/** A key's word: an optional key the server lacks is off, not broken. */
export function keyState(row: SecretKey, required: boolean): string {
  if (row.present) return "Present";
  return required ? "Missing" : "Not set";
}

/** The notices under the keys: how to fix a missing required key, where optional ones come from. */
export function noticeTexts(status: SecretsStatus): Array<{ text: string; kind: "error" | "info" }> {
  const items: Array<{ text: string; kind: "error" | "info" }> = [];
  if (status.missing.length) {
    items.push({
      kind: "error",
      text:
        `The server started without ${status.missing.join(", ")}. Run deploy/pull-secrets.sh ` +
        "on the box, then restart the server with deploy/compose.sh up -d.",
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

/** The keys behind one use: features that need two keys get one chip. */
interface Use {
  use: string;
  rows: SecretKey[];
}

/** Group keys by their use, in first-seen order. */
export function usesOf(rows: SecretKey[]): Use[] {
  const uses = new Map<string, Use>();
  for (const row of rows) {
    const found = uses.get(row.use);
    if (found) found.rows.push(row);
    else uses.set(row.use, { use: row.use, rows: [row] });
  }
  return [...uses.values()];
}

/** One chip per use: a dot and its use; the keys behind it, each with its state, in the title and for a screen reader. */
function keyChips(title: string, rows: SecretKey[], required: boolean): HTMLElement {
  return el(
    "ul",
    { class: "secret-keys", "aria-label": title },
    ...usesOf(rows).map(({ use, rows: keys }) => {
      const off = keys.some((row) => !row.present);
      return el(
        "li",
        {
          class: `secret-key${off ? (required ? " secret-missing" : " secret-absent") : ""}`,
          "data-use": use,
          title: keys
            .map((row) => `${row.key}: ${keyState(row, required)}${required ? "" : " (optional)"}`)
            .join("\n"),
        },
        el("span", { class: "secret-dot", "aria-hidden": "true" }),
        el("span", { class: "secret-use" }, use),
        el(
          "span",
          { class: "sr-only" },
          ...keys.flatMap((row) => [
            ", ",
            el("code", { class: "secret-name" }, row.key),
            ", ",
            el("span", { class: "secret-state" }, keyState(row, required)),
          ]),
        ),
      );
    }),
  );
}

export function card(status: SecretsStatus): HTMLElement {
  const node = el(
    "article",
    { class: `card card-fabric secrets-card fabric-${toneOf(status)}` },
    el(
      "div",
      { class: "fabric-row" },
      el("span", { class: "fabric-dot", "aria-hidden": "true" }),
      el(
        "div",
        { class: "fabric-title" },
        el("span", { class: "fabric-name" }, "Server secrets"),
        el("span", { class: "fabric-state" }, pillText(status)),
      ),
      el("div", { class: "fabric-acts" }),
      el(
        "p",
        { class: "fabric-meta" },
        ...metaItems(status).map((item) => el("span", { class: "fabric-meta-item" }, item)),
      ),
    ),
    el(
      "div",
      { class: "secret-groups" },
      keyChips("Required", status.required, true),
      keyChips("Optional", status.optional, false),
    ),
  );
  for (const item of noticeTexts(status)) node.appendChild(notice(item.text, item.kind));
  return node;
}

interface SectionState {
  status: SecretsStatus | null;
  at: number;
  error: unknown;
  /** What the list last drew, so an unchanged answer is not redrawn. */
  drawn: string;
}

function render(list: HTMLElement, refreshed: HTMLElement, state: SectionState): void {
  // Every focus reads again; redrawing an unchanged card would make a screen
  // reader announce its role="alert" notice each time (dual review, 2026-10-20).
  const drawn = JSON.stringify([state.status, state.error ? describeError(state.error) : null]);
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
    el("section", { class: "cards fabric-list secrets-list", "aria-label": "Server secrets" }),
  );
}

let onFocus: (() => void) | null = null;
/** Bumped by every activate and deactivate, as in host.ts. */
let generation = 0;

function stopListening(): void {
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
  const state: SectionState = { status: null, at: 0, error: null, drawn: "" };
  let reading: Promise<void> | null = null;
  const refresh = (): Promise<void> => {
    if (reading) return reading;
    reading = fetchSecrets(context)
      .then(
        (answer) => {
          if (!live()) return;
          state.status = answer;
          state.at = Date.now();
          state.error = null;
        },
        (error: unknown) => {
          if (live()) state.error = error ?? new Error("Request failed");
        },
      )
      .then(() => {
        if (live()) render(list, refreshed, state);
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
