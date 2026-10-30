// Collapsible sections (2026-09-24, after the owner's design feedback): every
// section header on the Dashboard is a toggle, and which sections are open is
// remembered per section in the SDK's `storage.user` (key `sections.open`,
// an object of section id to boolean), in memory when that is unavailable.
// Reference and Unsorted start closed; everything else starts open. A
// header link opens a section before scrolling to it (header.ts).
//
// 2026-10-20: the Admin Center's cards fold on a phone the same way
// (board.ts), under ids that start with "admin-" so they never meet a
// Dashboard section's. Their default comes from the caller (open when the
// card needs attention), so isOpen takes one. A write the SDK refuses at once
// (no storage on the page) is remembered in memory like a failed one.

import type { ExtensionContext } from "@omnigent/extension-sdk";

export const SECTIONS_KEY = "sections.open";

/** Sections that start closed until the owner opens them once. */
const CLOSED_BY_DEFAULT = new Set(["reference", "unsorted"]);

const remembered = new Map<string, boolean>();
let persist: (() => void) | null = null;

/** Read the remembered sections; the page works the same without them. */
export async function loadSectionState(context: ExtensionContext): Promise<void> {
  persist = () => {
    try {
      context.storage.user.set(SECTIONS_KEY, Object.fromEntries(remembered)).catch(() => {
        // storage.user unavailable: remembered in memory until the page closes.
      });
    } catch {
      // The same, refused before a promise was made.
    }
  };
  try {
    const saved = await context.storage.user.get<unknown>(SECTIONS_KEY);
    if (saved && typeof saved === "object" && !Array.isArray(saved)) {
      for (const [id, open] of Object.entries(saved)) {
        if (typeof open === "boolean") remembered.set(id, open);
      }
    }
  } catch {
    // Same as a failed write: defaults, remembered in memory.
  }
}

/** Whether *id* is open: as remembered, else *fallback*, else the Dashboard's default. */
export function isOpen(id: string, fallback?: boolean): boolean {
  return remembered.get(id) ?? fallback ?? !CLOSED_BY_DEFAULT.has(id);
}

export function setOpen(id: string, open: boolean): void {
  remembered.set(id, open);
  persist?.();
}

/** For tests: forget what the module remembers. */
export function resetSectionState(): void {
  remembered.clear();
  persist = null;
}
