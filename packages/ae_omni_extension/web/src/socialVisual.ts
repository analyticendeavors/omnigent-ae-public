// The picture a Social post shares (2026-11-07). The page's frame can show only `data:` and
// `blob:` images and cannot fetch, so the server fetches the picture and hands it over in base64
// pieces (GET /social/visual/{slot}/{part}, each under the bridge's 32,768-character cap). This
// module joins them into one Blob, keeps it for the page's life, and builds the File the share
// sheet takes. The editor loads it when it opens, so the File exists before the tap: a fetch
// after the tap would outlast the tap's permission to open the share sheet.

import type { ExtensionContext } from "@omnigent/extension-sdk";
import { fetchSocialVisual, type SocialSlot } from "./api";

/** Pieces asked for at once; a 3 MB picture is about 175 of them. */
const PARALLEL = 4;

const loaded = new Map<string, Blob>();
const loading = new Map<string, Promise<Blob | null>>();
const failed = new Set<string>();

/**
 * What a cached picture belongs to: the slot and the picture's identity (the video id, or the
 * tool's image link). A tool whose image link changed is a new key, so the old picture is never
 * shared for it (2026-11-07 review).
 */
const keyOf = (slot: SocialSlot): string => `${slot.id}|${slot.visual?.key ?? ""}`;

/** Forget every picture (tests start clean). */
export function resetVisuals(): void {
  loaded.clear();
  loading.clear();
  failed.clear();
}

/** The picture if it has loaded; never waits. */
export const loadedVisual = (slot: SocialSlot): Blob | null => loaded.get(keyOf(slot)) ?? null;

/** Where the picture stands: "none" (the post has none), "loading", "ready" or "failed". */
export function visualState(slot: SocialSlot): "none" | "loading" | "ready" | "failed" {
  if (!slot.visual) return "none";
  const key = keyOf(slot);
  if (loaded.has(key)) return "ready";
  return failed.has(key) ? "failed" : "loading";
}

function decode(b64: string): Uint8Array {
  const binary = atob(b64);
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i += 1) bytes[i] = binary.charCodeAt(i);
  return bytes;
}

async function download(context: ExtensionContext, slotId: string): Promise<Blob> {
  const first = await fetchSocialVisual(context, slotId, 0);
  const pieces: Uint8Array[] = new Array<Uint8Array>(first.parts);
  pieces[0] = decode(first.data);
  const rest = Array.from({ length: first.parts - 1 }, (_, i) => i + 1);
  const worker = async (): Promise<void> => {
    for (let part = rest.shift(); part !== undefined; part = rest.shift()) {
      pieces[part] = decode((await fetchSocialVisual(context, slotId, part)).data);
    }
  };
  await Promise.all(Array.from({ length: Math.min(PARALLEL, rest.length) }, worker));
  return new Blob(pieces as BlobPart[], { type: first.type });
}

/**
 * Start loading *slot*'s picture (once); resolves to it, or null when the post has none or the
 * server could not fetch it. A failure is not remembered, so opening the post again retries.
 */
export function loadVisual(context: ExtensionContext, slot: SocialSlot): Promise<Blob | null> {
  if (!slot.visual) return Promise.resolve(null);
  const key = keyOf(slot);
  const have = loaded.get(key);
  if (have) return Promise.resolve(have);
  const pending = loading.get(key);
  if (pending) return pending;
  failed.delete(key);
  const task = download(context, slot.id).then(
    (blob) => {
      loaded.set(key, blob);
      loading.delete(key);
      return blob;
    },
    () => {
      loading.delete(key);
      failed.add(key);
      return null;
    },
  );
  loading.set(key, task);
  return task;
}

/** The File the share sheet takes, named for the post ("my-title.jpg"). */
export function visualFile(blob: Blob, name: string): File {
  const slug =
    name
      .toLowerCase()
      .replace(/[^a-z0-9]+/g, "-")
      .replace(/^-+|-+$/g, "")
      .slice(0, 60) || "post";
  return new File([blob], `${slug}.${blob.type === "image/png" ? "png" : "jpg"}`, { type: blob.type });
}

const urls = new WeakMap<Blob, string>();

/** A `blob:` URL for the picture (one per picture), or null where the page cannot make one. */
export function visualUrl(blob: Blob): string | null {
  const have = urls.get(blob);
  if (have) return have;
  try {
    if (typeof URL.createObjectURL !== "function") return null;
    const url = URL.createObjectURL(blob);
    urls.set(blob, url);
    return url;
  } catch {
    return null;
  }
}
