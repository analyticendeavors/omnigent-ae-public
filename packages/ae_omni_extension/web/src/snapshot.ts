// The last good answer of each slow call, per device (2026-09-26).
//
// Why: the owner waited 3 to 5 seconds on every open of the Dashboard on their
// iPhone, looking at an empty page. The page now paints what this device saw
// last as soon as it opens, marked as refreshing with its actions off, and
// replaces each section as its call lands (main.ts).
//
// Stored in the SDK's `storage.user` (IndexedDB on this device, the store
// sections.ts and lot.ts already use), one key per part so no value nears the
// host's 32 KB cap; a part over PART_MAX_BYTES is not kept (and its old copy
// is dropped), so the snapshots never crowd out the remembered sections and
// filters in the extension's 256 KB. Sessions are not kept: the host lists
// them itself, fast, and they are the largest payload.

import type { ExtensionContext } from "@omnigent/extension-sdk";
import type { Capacity, Lot, LotTags, OpenPrs, YourMove } from "./api";
import type { Limits } from "./limits";

/** Bump when a stored shape changes, so an old copy is never drawn by new code. */
export const SNAPSHOT_VERSION = 1;
/** Under the host's 32 KB per value, with room for the envelope. */
export const PART_MAX_BYTES = 24 * 1024;

export interface Parts {
  yourMove: YourMove;
  prs: OpenPrs;
  lot: Lot;
  tags: LotTags;
  capacity: Capacity;
  limits: Limits;
}

export type Part = keyof Parts;

export const PARTS: readonly Part[] = ["yourMove", "prs", "lot", "tags", "capacity", "limits"];

export const snapshotKey = (part: Part) => `snapshot.${part}`;

interface Envelope {
  v: number;
  at: number;
  data: unknown;
}

const isObject = (value: unknown): value is Record<string, unknown> =>
  typeof value === "object" && value !== null && !Array.isArray(value);

/** Enough of each shape that a render of it cannot throw. */
const VALID: Record<Part, (data: Record<string, unknown>) => boolean> = {
  yourMove: (data) => isObject(data.sessions) && isObject(data.lot) && isObject(data.pulls),
  prs: (data) =>
    typeof data.configured === "boolean" && Array.isArray(data.items) && Array.isArray(data.errors),
  lot: (data) => Array.isArray(data.tasks) && typeof data.count === "number",
  tags: (data) => Array.isArray(data.tags),
  capacity: (data) => Array.isArray(data.working) && Array.isArray(data.blocked),
  limits: () => true,
};

/** What each key last held as written, so an unchanged answer is not written again. */
const written = new Map<Part, string>();

/** Every part this device kept, read in parallel; a missing or unreadable one is left out. */
export async function loadSnapshot(context: ExtensionContext): Promise<Partial<Parts>> {
  const read = await Promise.all(
    PARTS.map(async (part) => {
      try {
        const saved = await context.storage.user.get<unknown>(snapshotKey(part));
        if (!isObject(saved) || saved.v !== SNAPSHOT_VERSION || !isObject(saved.data)) {
          return null;
        }
        if (!VALID[part](saved.data)) return null;
        written.set(part, JSON.stringify(saved.data));
        return [part, saved.data] as const;
      } catch {
        return null;
      }
    }),
  );
  const out: Record<string, unknown> = {};
  for (const entry of read) if (entry) out[entry[0]] = entry[1];
  return out as Partial<Parts>;
}

/** Keep a fresh answer for the next open; unchanged or oversized answers are not written. */
export function saveSnapshot<P extends Part>(
  context: ExtensionContext,
  part: P,
  data: Parts[P],
): void {
  const json = JSON.stringify(data);
  if (written.get(part) === json) return;
  written.set(part, json);
  const envelope: Envelope = { v: SNAPSHOT_VERSION, at: Date.now(), data };
  const key = snapshotKey(part);
  if (new TextEncoder().encode(json).byteLength > PART_MAX_BYTES) {
    context.storage.user.delete(key).catch(() => {
      // storage.user unavailable: nothing was kept to drop.
    });
    return;
  }
  context.storage.user.set(key, envelope).catch(() => {
    // storage.user unavailable or full: the next open starts empty, as before.
    written.delete(part);
  });
}

/** For tests: forget what was written. */
export function resetSnapshotState(): void {
  written.clear();
}
