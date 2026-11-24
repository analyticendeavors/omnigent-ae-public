// The picture a Social post shares (2026-11-07). The page's frame can show only `data:` and
// `blob:` images and cannot fetch, so the server fetches the picture and hands it over in base64
// pieces (GET /social/visual/{slot}/{part}, each under the bridge's 32,768-character cap). This
// module joins them into one Blob, keeps it for the page's life, and builds the File the share
// sheet takes. The editor loads it when it opens, so the File exists before the tap: a fetch
// after the tap would outlast the tap's permission to open the share sheet.

import type { ExtensionContext } from "@omnigent/extension-sdk";
import { fetchSocialVisual, type SocialMadeVisual, type SocialSlot } from "./api";

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
  previews.clear();
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

/** The made picture a visual key names ("c2:sha"), so a fetch never races a new choice. */
const madeCandidate = (slot: SocialSlot): string | undefined =>
  slot.visual?.kind === "made" ? slot.visual.key?.split(":")[0] : undefined;

async function download(
  context: ExtensionContext,
  slotId: string,
  pick: { c?: string; preview?: boolean } = {},
): Promise<Blob> {
  const first = await fetchSocialVisual(context, slotId, 0, pick);
  const pieces: Uint8Array[] = new Array<Uint8Array>(first.parts);
  pieces[0] = decode(first.data);
  const rest = Array.from({ length: first.parts - 1 }, (_, i) => i + 1);
  const worker = async (): Promise<void> => {
    for (let part = rest.shift(); part !== undefined; part = rest.shift()) {
      pieces[part] = decode((await fetchSocialVisual(context, slotId, part, pick)).data);
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
  const task = download(context, slot.id, { c: madeCandidate(slot) }).then(
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

const previews = new Map<string, Promise<Blob | null>>();

/**
 * The small tile of one made picture (2026-11-15), loaded once per picture; null when it
 * could not load (a later call tries again).
 */
export function loadPreview(context: ExtensionContext, slotId: string, made: SocialMadeVisual): Promise<Blob | null> {
  const key = `${slotId}|${made.key}`;
  const have = previews.get(key);
  if (have) return have;
  const task = download(context, slotId, { c: made.id, preview: true }).catch(() => {
    previews.delete(key);
    return null;
  });
  previews.set(key, task);
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

/** Whether this browser can put an image on the clipboard (the Clipboard API's image write). */
export function canCopyPicture(): boolean {
  return typeof ClipboardItem !== "undefined" && typeof navigator !== "undefined" && typeof navigator.clipboard?.write === "function";
}

/** How long the canvas gets to make the PNG before the copy gives up. */
export const PNG_TIMEOUT_MS = 10_000;
const PNG_FAILED = "Could not prepare the picture for copying; use Download.";

/** Draw a picture that is not a PNG onto a canvas and read it back as a PNG (the clipboard takes only PNG in most browsers). */
async function asPng(blob: Blob): Promise<Blob> {
  if (blob.type === "image/png") return blob;
  const bitmap = await createImageBitmap(blob);
  try {
    const canvas = document.createElement("canvas");
    canvas.width = bitmap.width;
    canvas.height = bitmap.height;
    const context = canvas.getContext("2d");
    if (!context) throw new Error(PNG_FAILED);
    context.drawImage(bitmap, 0, 0);
    return await new Promise<Blob>((resolve, reject) => {
      const timer = setTimeout(() => reject(new Error(PNG_FAILED)), PNG_TIMEOUT_MS);
      canvas.toBlob((out) => {
        clearTimeout(timer);
        if (out) resolve(out);
        else reject(new Error(PNG_FAILED));
      }, "image/png");
    });
  } finally {
    // The decoded bitmap holds memory until it is closed, on success and on failure alike.
    bitmap.close();
  }
}

/**
 * Put the picture on the clipboard as image/png (the only image type the clipboard takes). Call it from
 * the click handler: `clipboard.write` starts at once, inside the tap, with the PNG still being made
 * (a ClipboardItem takes a promise), so a JPEG or WebP conversion never pushes the write past the
 * user gesture. Returns null when it went, else the reason in plain words.
 */
export async function copyPicture(blob: Blob): Promise<string | null> {
  if (!canCopyPicture()) return "this browser cannot copy pictures";
  try {
    await navigator.clipboard.write([new ClipboardItem({ "image/png": asPng(blob) })]);
    return null;
  } catch (error) {
    const reason = (error as { message?: unknown } | null)?.message;
    return typeof reason === "string" && reason ? reason : "the browser refused";
  }
}

/** How long Download's own URL lives after the click (the browser starts the save at the click). */
export const DOWNLOAD_URL_MS = 1_000;

/** Save the picture as a file named for the post (the name the share sheet uses). True when the click was made. */
export function downloadPicture(blob: Blob, name: string): boolean {
  // Its own short-lived URL, revoked soon after the click, so it never outlives the save.
  let url: string;
  try {
    if (typeof URL.createObjectURL !== "function") return false;
    url = URL.createObjectURL(blob);
  } catch {
    return false;
  }
  const file = visualFile(blob, name);
  const link = document.createElement("a");
  link.href = url;
  link.download = file.name;
  link.rel = "noopener";
  link.hidden = true;
  document.body.appendChild(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), DOWNLOAD_URL_MS);
  return true;
}

// The display URLs an opened Social row makes: one per slot, revoked when the row's picture is replaced and
// when the Admin Center page is left (an object URL holds its picture in memory until it is revoked).
const shown = new Map<string, string>();

/** A fresh `blob:` URL for *slotId*'s opened row, revoking the one that row showed before. Null where the page cannot make one. */
export function showPictureUrl(slotId: string, blob: Blob): string | null {
  try {
    if (typeof URL.createObjectURL !== "function") return null;
    const url = URL.createObjectURL(blob);
    const old = shown.get(slotId);
    if (old) URL.revokeObjectURL(old);
    shown.set(slotId, url);
    return url;
  } catch {
    return null;
  }
}

/** Revoke every display URL (the Admin Center page was left; the tests start clean). */
export function revokePictureUrls(): void {
  for (const url of shown.values()) URL.revokeObjectURL(url);
  shown.clear();
}
