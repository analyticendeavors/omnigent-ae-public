// The Social card's words and numbers, shared by the compact card (social.ts) and the full view
// (socialView.ts, calendar.ts) so neither imports the other (2026-11-06, part 2).

import type { SocialSlot, SocialView } from "./api";

export const HOUR = 3_600_000;
export const DAY = 86_400_000;
/** The rows under "Up next". */
export const UP_NEXT = 3;
export const CHANNEL_NAMES: Record<string, string> = { ae: "AE", hc: "HC" };
/** The channel's name in a post's own words (starter text) until the server says it (`channel_names`). */
export const CHANNEL_FULL_NAMES: Record<string, string> = { ae: "Analytic Endeavors", hc: "HC" };

/**
 * A phone is a full view at most this wide (px). style.css repeats the number in its
 * `@media (max-width: 560px)` blocks; a test reads the file and fails if the two drift.
 */
export const PHONE_MAX = 560;
export const PHONE_QUERY = `(max-width: ${PHONE_MAX}px)`;
/** The server cuts a draft's preview here; a shorter preview is the whole text. */
export const PREVIEW_CHARS = 160;

/** Whether the page is phone-wide now (false where `matchMedia` is missing). */
export function isPhone(): boolean {
  try {
    return typeof window.matchMedia === "function" && window.matchMedia(PHONE_QUERY).matches;
  } catch {
    return false;
  }
}

/** Whether the main pointer is a finger (a phone or tablet): the share sheet is the way to post. */
export function isTouch(): boolean {
  try {
    return typeof window.matchMedia === "function" && window.matchMedia("(pointer: coarse)").matches;
  } catch {
    return false;
  }
}

/** Call *onChange* whenever the page crosses the phone breakpoint; returns the stop function. */
export function watchPhone(onChange: (phone: boolean) => void): () => void {
  if (typeof window.matchMedia !== "function") return () => undefined;
  const query = window.matchMedia(PHONE_QUERY);
  const listener = (event: { matches: boolean }) => onChange(event.matches);
  query.addEventListener("change", listener);
  return () => query.removeEventListener("change", listener);
}

// ── words and numbers ───────────────────────────────────────────────────────


/** The wall-clock parts of *ms* in *tz*; the browser's zone when *tz* is not a zone name. */
export function zonedParts(ms: number, tz: string): { day: string; weekday: string; time: string; date: string } {
  const at = (options: Intl.DateTimeFormatOptions) => {
    try {
      return new Intl.DateTimeFormat("en-US", { timeZone: tz, ...options }).format(ms);
    } catch {
      return new Intl.DateTimeFormat("en-US", options).format(ms);
    }
  };
  return {
    day: at({ year: "numeric", month: "2-digit", day: "2-digit" }),
    weekday: at({ weekday: "short" }),
    time: at({ hour: "numeric", minute: "2-digit" }),
    date: at({ month: "short", day: "numeric" }),
  };
}

/** "Today 9:00 AM", "Tomorrow 9:00 AM", "Tue 6:00 AM", "Nov 20 9:00 AM", "Overdue since Mon". */
export function dueLabel(due: string, now: number, tz: string): string {
  const ms = Date.parse(due);
  if (Number.isNaN(ms)) return "";
  const then = zonedParts(ms, tz);
  if (ms < now) {
    const age = now - ms;
    return `Overdue since ${age < 6 * DAY ? then.weekday : then.date}`;
  }
  const today = zonedParts(now, tz);
  if (then.day === today.day) return `Today ${then.time}`;
  if (zonedParts(now + DAY, tz).day === then.day) return `Tomorrow ${then.time}`;
  return ms - now < 6 * DAY ? `${then.weekday} ${then.time}` : `${then.date} ${then.time}`;
}

/** The row's title: "Video: ...", "Short: ...", "Livestream: ...", "Community post: ...", "Tool: ...". */
export function slotTitle(slot: SocialSlot): string {
  const name = slot.title ?? "";
  const join = (lead: string, rest: string) => (rest ? `${lead}: ${rest}` : lead);
  if (slot.kind === "tool") return join("Tool", slot.tool ?? "");
  if (slot.kind === "expert") return join("Expert post", slot.topic ?? "");
  if (slot.platform === "yt_community") return join("Community post", name);
  if (slot.kind === "short") return join("Short", name);
  if (slot.kind === "live") return join("Livestream", name);
  return join("Video", name);
}

/** Open posts: not posted, not skipped. */
export const open = (slot: SocialSlot) => slot.state === "planned";

/** The next *n* open posts: overdue first (oldest first), then by due time. */
export function upNext(slots: SocialSlot[], now: number, n = UP_NEXT): SocialSlot[] {
  const rows = slots.filter(open).sort((a, b) => Date.parse(a.due) - Date.parse(b.due));
  const late = rows.filter((slot) => Date.parse(slot.due) < now);
  return [...late, ...rows.filter((slot) => Date.parse(slot.due) >= now)].slice(0, n);
}

export const overdueCount = (view: SocialView, now: number) =>
  view.slots.filter((slot) => open(slot) && Date.parse(slot.due) < now).length;

/** Open posts due later today (in the rules' time zone) that are not yet overdue. */
export function dueTodayCount(view: SocialView, now: number): number {
  const today = zonedParts(now, view.rules.tz).day;
  return view.slots.filter(
    (slot) =>
      open(slot) && Date.parse(slot.due) >= now && zonedParts(Date.parse(slot.due), view.rules.tz).day === today,
  ).length;
}

export interface Pill {
  text: string;
  tone: "ok" | "warn" | "err";
}

/** The header pill: overdue, else due today, else on track or behind. */
export function headerPill(view: SocialView, now: number): Pill {
  const late = overdueCount(view, now);
  if (late > 0) return { text: `${late} overdue`, tone: "err" };
  const today = dueTodayCount(view, now);
  if (today > 0) return { text: `${today} due today`, tone: "warn" };
  if (view.score.first_week) return { text: "First week", tone: "ok" };
  return view.score.on_pace ? { text: "On track", tone: "ok" } : { text: "Behind", tone: "warn" };
}

export const ago = (iso: string, now: number): string => {
  const minutes = Math.max(0, Math.round((now - Date.parse(iso)) / 60_000));
  if (minutes < 90) return `${minutes} min ago`;
  return `${Math.round(minutes / 60)} h ago`;
};

export interface Warning {
  text: string;
  tone: "warn" | "err";
}

/** Gap lines: nothing scheduled, a failed read, a stale reading. */
export function warnings(view: SocialView, now: number): Warning[] {
  const out: Warning[] = [];
  const stale: string[] = [];
  for (const [channel, gap] of Object.entries(view.gaps)) {
    const name = CHANNEL_NAMES[channel] ?? channel.toUpperCase();
    if (gap.upload_gap) {
      out.push({
        text: `${name} channel: nothing scheduled in the next ${view.rules.gap_days} days.`,
        tone: "warn",
      });
    }
    if (gap.read_failed) out.push({ text: `${name} read failed: ${gap.error ?? "unknown error"}`, tone: "err" });
    const read = view.uploads_meta[channel]?.read_at;
    if (gap.stale && read) stale.push(read);
  }
  if (stale.length) {
    const oldest = stale.reduce((a, b) => (Date.parse(a) <= Date.parse(b) ? a : b));
    out.push({ text: `YouTube read ${Math.round((now - Date.parse(oldest)) / HOUR)} h ago`, tone: "warn" });
  }
  return out;
}

/** LinkedIn's share box with the draft in it, or the channel's Community tab. */
export function openUrl(slot: SocialSlot, view: SocialView, text: string): string | null {
  if (slot.platform === "linkedin") {
    const base = "https://www.linkedin.com/feed/?shareActive=true";
    if (!text) return base;
    try {
      // The host's allowlist wants the apostrophe written as %27 (patch 0295).
      return `${base}&text=${encodeURIComponent(text).replace(/'/g, "%27")}`;
    } catch {
      return base; // a lone surrogate cannot be encoded; the clipboard still has the draft
    }
  }
  const id = slot.channel ? view.channel_ids[slot.channel] : null;
  return id ? `https://www.youtube.com/channel/${id}/posts` : null;
}


/** "Post on LinkedIn" or, for a Community post, "Post on YouTube". */
export const postLabel = (slot: SocialSlot): string => (slot.platform === "linkedin" ? "Post on LinkedIn" : "Post on YouTube");

/** The site's name for the "Back from ..." banner. */
export const siteName = (platform: SocialSlot["platform"]): string => (platform === "linkedin" ? "LinkedIn" : "YouTube");

/**
 * The text a post starts with when it has no draft (2026-11-06): a plain line and the link, so the
 * Post button is never stuck for lack of text. An expert post has none (the composer opens empty),
 * and a post whose title or link is missing has none either (an older server sends no link).
 */
export function starterText(slot: SocialSlot, names: Record<string, string> = CHANNEL_FULL_NAMES): string {
  const url = slot.url ?? "";
  if (slot.kind === "tool") return slot.tool && url ? `${slot.tool}: ${url}` : "";
  const title = (slot.title ?? "").trim();
  if (slot.kind === "expert" || !title || !url) return "";
  // A title that ends in a full stop or a question mark does not get a second one.
  const sentence = /[.!?]$/.test(title) ? title : `${title}.`;
  const channel = slot.channel ? (names[slot.channel] ?? CHANNEL_FULL_NAMES[slot.channel] ?? slot.channel) : "";
  if (slot.platform === "yt_community") return `New video: ${title}\n\n${url}`;
  if (slot.kind === "short") return `New Short on the ${channel} channel: ${sentence}\n\n${url}`;
  if (slot.kind === "live") return `Live on YouTube soon: ${sentence}\n\n${url}`;
  return `New video on the ${channel} channel: ${sentence}\n\n${url}`;
}

// ── bold first line (2026-11-07) ────────────────────────────────────────────
// LinkedIn shows plain text only. Letters from Unicode's Mathematical Sans-Serif Bold block read
// as bold there, which is what shows in the feed's preview of a post's first line. Only line 1 is
// changed, and a link, hashtag or @mention on it never is (a bold-lettered one does not link).

const BOLD_UPPER = 0x1d5d4;
const BOLD_LOWER = 0x1d5ee;
const BOLD_DIGIT = 0x1d7ec;
// A link, a #hashtag or an @mention: LinkedIn will not link one written in bold letters.
const LINK = /https?:\/\/\S+|(?<!\S)[#@]\S+/g;

const firstLineEnd = (text: string): number => {
  const end = text.indexOf("\n");
  return end < 0 ? text.length : end;
};

/** *line* with *map* applied to every part that is not a link. */
function mapOutsideLinks(line: string, map: (part: string) => string): string {
  let out = "";
  let last = 0;
  for (const match of line.matchAll(LINK)) {
    out += map(line.slice(last, match.index)) + match[0];
    last = match.index + match[0].length;
  }
  return out + map(line.slice(last));
}

const toBold = (part: string): string =>
  part.replace(/[A-Za-z0-9]/g, (ch) => {
    const code = ch.charCodeAt(0);
    if (code >= 65 && code <= 90) return String.fromCodePoint(BOLD_UPPER + code - 65);
    if (code >= 97 && code <= 122) return String.fromCodePoint(BOLD_LOWER + code - 97);
    return String.fromCodePoint(BOLD_DIGIT + code - 48);
  });

const fromBold = (part: string): string =>
  [...part]
    .map((ch) => {
      const code = ch.codePointAt(0) ?? 0;
      if (code >= BOLD_UPPER && code < BOLD_UPPER + 26) return String.fromCharCode(65 + code - BOLD_UPPER);
      if (code >= BOLD_LOWER && code < BOLD_LOWER + 26) return String.fromCharCode(97 + code - BOLD_LOWER);
      if (code >= BOLD_DIGIT && code < BOLD_DIGIT + 10) return String.fromCharCode(48 + code - BOLD_DIGIT);
      return ch;
    })
    .join("");

/** Line 1 with A-Z, a-z and 0-9 set in Mathematical Sans-Serif Bold; links and other lines stay. */
export function boldFirstLine(text: string): string {
  const end = firstLineEnd(text);
  return mapOutsideLinks(text.slice(0, end), toBold) + text.slice(end);
}

/** Line 1 back to plain A-Z, a-z and 0-9. */
export function unboldFirstLine(text: string): string {
  const end = firstLineEnd(text);
  return mapOutsideLinks(text.slice(0, end), fromBold) + text.slice(end);
}

/** Whether line 1 is bold: it has a bold letter and no plain letter or digit outside a link. */
export function isFirstLineBold(text: string): boolean {
  const line = text.slice(0, firstLineEnd(text));
  let bold = false;
  let plain = false;
  mapOutsideLinks(line, (part) => {
    if (fromBold(part) !== part) bold = true;
    if (/[A-Za-z0-9]/.test(part)) plain = true;
    return part;
  });
  return bold && !plain;
}
