// Lucide (decided 2026-09-23, icon set (a)): status, quadrant and
// capacity icons rendered from the vanilla `lucide` package inside the
// sandboxed frame. No emoji anywhere in the page. Tabler icons are inlined
// at the end: the merge method's tick (2026-09-25), the Open on GitHub
// button's external link (2026-09-27), the pull request cards' state and
// chip icons and a session row's "..." menu button (2026-10-14). Fabric
// Admin's action icons are lucide: power, clock and the links menu's external
// link (2026-10-16, second pass; its state is a coloured dot since then, so
// the state icons of the first pass went). The Databricks section's Start,
// Stop and Refresh are Tabler, inlined (2026-10-19), and so is the Deploys
// section's pause on Hold (2026-10-19).

import {
  Archive,
  CalendarClock,
  ChevronDown,
  CircleDashed,
  CircleX,
  CircleCheck,
  CircleParking,
  CirclePause,
  CirclePlay,
  ClipboardList,
  Clock,
  createElement,
  ExternalLink,
  Flame,
  FolderGit2,
  Funnel,
  LayoutDashboard,
  MessageCircleQuestion,
  Play,
  Power,
  ScrollText,
  Search,
  Sparkles,
  Split,
  type IconNode,
} from "lucide";
import type { AeStatus, CiState, WaitKind } from "./api";

const STATUS_ICONS: Record<AeStatus, [IconNode, string]> = {
  working: [CirclePlay, "working"],
  blocked: [CirclePause, "blocked"],
  review: [CircleCheck, "review"],
  parked: [CircleParking, "parked"],
  reference: [Archive, "reference"],
};

const QUADRANT_ICONS: Record<string, [IconNode, string]> = {
  hotfix: [Flame, "hotfix"],
  feature: [Sparkles, "feature"],
  admin: [ClipboardList, "admin"],
};

function render(node: IconNode, label: string, extraClass: string): SVGElement {
  const svg = createElement(node, {
    class: `icon ${extraClass}`,
    width: 18,
    height: 18,
    "aria-label": label,
    role: "img",
  });
  svg.appendChild(
    Object.assign(document.createElementNS("http://www.w3.org/2000/svg", "title"), {
      textContent: label,
    }),
  );
  return svg;
}

export function statusIcon(status: AeStatus): SVGElement {
  const [node, label] = STATUS_ICONS[status];
  return render(node, label, `icon-${status}`);
}

/** The parked icon as a lot row's marker, named for its sessions (2026-10-14). */
export function parkedIcon(label: string): SVGElement {
  return render(CircleParking, label, "icon-parked parked-mark");
}

/** The quadrant's icon, or null for a value the vocabulary does not know. */
export function quadrantIcon(quadrant: string | null): SVGElement | null {
  if (!quadrant) return null;
  const entry = QUADRANT_ICONS[quadrant];
  return entry ? render(entry[0], entry[1], `icon-${quadrant}`) : null;
}

/** How a Fabric capacity's state reads (fabric.ts sorts each state into one). */
export type FabricTone = "on" | "off" | "busy" | "failed" | "unknown";

/**
 * The Admin Center's Fabric section buttons (2026-10-16, second pass): each
 * button carries its own name.
 */
export function powerIcon(): SVGElement {
  return decorative(Power, "icon-power", 16);
}

export function clockIcon(): SVGElement {
  return decorative(Clock, "icon-clock", 16);
}

export function linksIcon(): SVGElement {
  return decorative(ExternalLink, "icon-links", 16);
}

/** Decorative: the text next to it carries the meaning. */
function decorative(node: IconNode, extraClass: string, size = 18): SVGElement {
  return createElement(node, {
    class: `icon ${extraClass}`,
    width: size,
    height: size,
    "aria-hidden": "true",
  });
}

export function chevronIcon(): SVGElement {
  return decorative(ChevronDown, "icon-chevron");
}

export function playIcon(): SVGElement {
  return decorative(Play, "icon-play", 16);
}

// Your move (2026-09-24): why a session waits on Reid, and a pull request's CI.
const WAIT_ICONS: Record<WaitKind, [IconNode, string]> = {
  gate: [Split, "open gate"],
  awaiting: [MessageCircleQuestion, "waiting on an answer"],
  blocked: [CirclePause, "blocked"],
  review: [CircleCheck, "review"],
};

const CI_ICONS: Record<CiState, [IconNode, string]> = {
  passing: [CircleCheck, "CI passing"],
  failing: [CircleX, "CI failing"],
  pending: [CircleDashed, "CI running"],
  none: [CircleDashed, "no CI"],
  unknown: [CircleDashed, "CI state unknown"],
};

export function waitIcon(kind: WaitKind): SVGElement {
  const [node, label] = WAIT_ICONS[kind];
  return render(node, label, `icon-wait-${kind}`);
}

export function ciIcon(state: CiState): SVGElement {
  const [node, label] = CI_ICONS[state];
  return render(node, label, `icon-ci-${state}`);
}

export function dueIcon(): SVGElement {
  return decorative(CalendarClock, "icon-due");
}

export function briefIcon(): SVGElement {
  return decorative(ScrollText, "icon-brief");
}

export function yourMoveIcon(): SVGElement {
  return decorative(LayoutDashboard, "icon-your-move");
}

/** A session with no `ae.status` yet (Unsorted, 2026-09-24). */
export function unsortedIcon(): SVGElement {
  return render(CircleDashed, "no status", "icon-unsorted");
}

// The lot's search, filters and repository line (2026-09-25, issue 28).
export function searchIcon(): SVGElement {
  return decorative(Search, "icon-search", 16);
}

export function filterIcon(): SVGElement {
  return decorative(Funnel, "icon-filter", 16);
}

export function repoIcon(): SVGElement {
  return decorative(FolderGit2, "icon-repo", 13);
}

// Tabler's `check` outline icon (tabler.io/icons/icon/check), inlined as its one
// path: it ticks the chosen merge method exactly as the chat's PR card does
// (AePrCards.tsx draws the same path). 2026-09-25.
const TABLER_CHECK: IconNode = [["path", { d: "M5 12l5 5l10 -10" }]];

export function checkIcon(): SVGElement {
  return decorative(TABLER_CHECK, "pr-check", 14);
}

// Tabler's `external-link` outline icon (tabler.io/icons/icon/external-link,
// @tabler/icons 3.46.0), inlined as its three paths for the Open on GitHub
// button (openLink.ts, 2026-09-27).
const TABLER_EXTERNAL_LINK: IconNode = [
  ["path", { d: "M12 6h-6a2 2 0 0 0 -2 2v10a2 2 0 0 0 2 2h10a2 2 0 0 0 2 -2v-6" }],
  ["path", { d: "M11 13l9 -9" }],
  ["path", { d: "M15 4h5v5" }],
];

export function externalLinkIcon(): SVGElement {
  return decorative(TABLER_EXTERNAL_LINK, "icon-external", 16);
}

// Tabler's `copy` outline icon, @tabler/icons 3.46.0: the Copy link button is
// icon-only since 2026-10-14, so a phone card keeps its buttons on one row.
const TABLER_COPY: IconNode = [
  [
    "path",
    {
      d: "M7 9.667a2.667 2.667 0 0 1 2.667 -2.667h8.666a2.667 2.667 0 0 1 2.667 2.667v8.666a2.667 2.667 0 0 1 -2.667 2.667h-8.666a2.667 2.667 0 0 1 -2.667 -2.667l0 -8.666",
    },
  ],
  [
    "path",
    {
      d: "M4.012 16.737a2.005 2.005 0 0 1 -1.012 -1.737v-10c0 -1.1 .9 -2 2 -2h10c.75 0 1.158 .385 1.5 1",
    },
  ],
];

export function copyIcon(): SVGElement {
  return decorative(TABLER_COPY, "icon-copy", 16);
}

// Tabler's `dots` outline icon, @tabler/icons 3.46.0: a session row's "..."
// menu button (cards.ts, 2026-10-14).
const TABLER_DOTS: IconNode = [
  ["path", { d: "M4 12a1 1 0 1 0 2 0a1 1 0 1 0 -2 0" }],
  ["path", { d: "M11 12a1 1 0 1 0 2 0a1 1 0 1 0 -2 0" }],
  ["path", { d: "M18 12a1 1 0 1 0 2 0a1 1 0 1 0 -2 0" }],
];

export function dotsIcon(): SVGElement {
  return decorative(TABLER_DOTS, "icon-dots");
}

// Tabler's `x` outline icon, @tabler/icons 3.46.0: a merged card's Dismiss
// (prs.ts, 2026-10-15).
const TABLER_X: IconNode = [
  ["path", { d: "M18 6l-12 12" }],
  ["path", { d: "M6 6l12 12" }],
];

export function closeIcon(): SVGElement {
  return decorative(TABLER_X, "icon-close", 16);
}

// The pull request cards (2026-10-14, Reid: calmer cards): the head's icon
// names the state in GitHub's colours (open green, draft grey, merged purple,
// closed red), and each chip shows its tone as a shape, so colour is never the
// only signal. Tabler outline icons, @tabler/icons 3.46.0.
const PULL_ENDS: IconNode = [
  ["path", { d: "M4 18a2 2 0 1 0 4 0a2 2 0 1 0 -4 0" }],
  ["path", { d: "M4 6a2 2 0 1 0 4 0a2 2 0 1 0 -4 0" }],
  ["path", { d: "M16 18a2 2 0 1 0 4 0a2 2 0 1 0 -4 0" }],
];

const TABLER_PULL: IconNode = [
  ...PULL_ENDS,
  ["path", { d: "M6 8l0 8" }],
  ["path", { d: "M11 6h5a2 2 0 0 1 2 2v8" }],
  ["path", { d: "M14 9l-3 -3l3 -3" }],
];

const TABLER_PULL_DRAFT: IconNode = [
  ...PULL_ENDS,
  ["path", { d: "M6 8v8" }],
  ["path", { d: "M18 11h.01" }],
  ["path", { d: "M18 6h.01" }],
];

const TABLER_PULL_CLOSED: IconNode = [
  ...PULL_ENDS,
  ["path", { d: "M6 8v8" }],
  ["path", { d: "M18 11v5" }],
  ["path", { d: "M16 4l4 4m0 -4l-4 4" }],
];

const TABLER_MERGE: IconNode = [
  ["path", { d: "M5 18a2 2 0 1 0 4 0a2 2 0 1 0 -4 0" }],
  ["path", { d: "M5 6a2 2 0 1 0 4 0a2 2 0 1 0 -4 0" }],
  ["path", { d: "M15 12a2 2 0 1 0 4 0a2 2 0 1 0 -4 0" }],
  ["path", { d: "M7 8l0 8" }],
  ["path", { d: "M7 8a4 4 0 0 0 4 4h4" }],
];

export type PullState = "open" | "merged" | "closed";

const PULL_ICONS: Record<PullState | "draft", [IconNode, string]> = {
  open: [TABLER_PULL, "open pull request"],
  draft: [TABLER_PULL_DRAFT, "draft pull request"],
  merged: [TABLER_MERGE, "merged pull request"],
  closed: [TABLER_PULL_CLOSED, "closed pull request"],
};

/** A pull request's state icon; Your move's rows are open, so *state* defaults to it. */
export function pullIcon(draft: boolean, state: PullState = "open"): SVGElement {
  const key = state === "open" && draft ? "draft" : state;
  const [node, label] = PULL_ICONS[key];
  return render(node, label, `icon-pull icon-pull-${key}`);
}

const CIRCLE: IconNode = [["path", { d: "M3 12a9 9 0 1 0 18 0a9 9 0 1 0 -18 0" }]];

/** A chip's tone: fine, waiting, worth a look, nothing to say, or blocking. */
export type ChipTone = "ok" | "wait" | "warn" | "muted" | "err";

const TONE_ICONS: Record<ChipTone, IconNode> = {
  ok: [...CIRCLE, ["path", { d: "M9 12l2 2l4 -4" }]],
  wait: [
    ["path", { d: "M3 12a9 9 0 1 0 18 0a9 9 0 0 0 -18 0" }],
    ["path", { d: "M12 7v5l3 3" }],
  ],
  warn: [
    ["path", { d: "M3 12a9 9 0 1 0 18 0a9 9 0 0 0 -18 0" }],
    ["path", { d: "M12 8v4" }],
    ["path", { d: "M12 16h.01" }],
  ],
  muted: [
    ["path", { d: "M8.56 3.69a9 9 0 0 0 -2.92 1.95" }],
    ["path", { d: "M3.69 8.56a9 9 0 0 0 -.69 3.44" }],
    ["path", { d: "M3.69 15.44a9 9 0 0 0 1.95 2.92" }],
    ["path", { d: "M8.56 20.31a9 9 0 0 0 3.44 .69" }],
    ["path", { d: "M15.44 20.31a9 9 0 0 0 2.92 -1.95" }],
    ["path", { d: "M20.31 15.44a9 9 0 0 0 .69 -3.44" }],
    ["path", { d: "M20.31 8.56a9 9 0 0 0 -1.95 -2.92" }],
    ["path", { d: "M15.44 3.69a9 9 0 0 0 -3.44 -.69" }],
  ],
  err: [...CIRCLE, ["path", { d: "M10 10l4 4m0 -4l-4 4" }]],
};

/** Decorative: the chip's text says what the icon shows. */
export function toneIcon(tone: ChipTone): SVGElement {
  return decorative(TONE_ICONS[tone], "pr-tone", 14);
}

// The Admin Center's Databricks section (2026-10-19, databricks.ts): a
// warehouse's Start and Stop, and the spend strip's Refresh. Tabler outline
// icons, @tabler/icons 3.46.0 (player-play, player-stop, refresh).
const TABLER_PLAYER_PLAY: IconNode = [["path", { d: "M7 4v16l13 -8l-13 -8" }]];

const TABLER_PLAYER_STOP: IconNode = [
  [
    "path",
    {
      d: "M5 7a2 2 0 0 1 2 -2h10a2 2 0 0 1 2 2v10a2 2 0 0 1 -2 2h-10a2 2 0 0 1 -2 -2l0 -10",
    },
  ],
];

const TABLER_REFRESH: IconNode = [
  ["path", { d: "M20 11a8.1 8.1 0 0 0 -15.5 -2m-.5 -4v4h4" }],
  ["path", { d: "M4 13a8.1 8.1 0 0 0 15.5 2m.5 4v-4h-4" }],
];

export function startIcon(): SVGElement {
  return decorative(TABLER_PLAYER_PLAY, "icon-start", 16);
}

export function stopIcon(): SVGElement {
  return decorative(TABLER_PLAYER_STOP, "icon-stop", 16);
}

export function refreshIcon(): SVGElement {
  return decorative(TABLER_REFRESH, "icon-refresh", 16);
}

// The Admin Center's Deploys section (2026-10-19, deploys.ts): the Hold
// buttons. Tabler outline icon, @tabler/icons 3.46.0 (player-pause); Resume
// reuses player-play (startIcon).
const TABLER_PLAYER_PAUSE: IconNode = [
  ["path", { d: "M6 6a1 1 0 0 1 1 -1h2a1 1 0 0 1 1 1v12a1 1 0 0 1 -1 1h-2a1 1 0 0 1 -1 -1l0 -12" }],
  [
    "path",
    { d: "M14 6a1 1 0 0 1 1 -1h2a1 1 0 0 1 1 1v12a1 1 0 0 1 -1 1h-2a1 1 0 0 1 -1 -1l0 -12" },
  ],
];

export function pauseIcon(): SVGElement {
  return decorative(TABLER_PLAYER_PAUSE, "icon-pause", 16);
}

// The Admin Center's Shared Claude accounts (2026-10-20): the Shared tag on
// each account, and since the section gave way to a count tile the same day
// (tiles.ts), that tile's icon. Since 2026-10-21 also the Accounts card's
// badge (accounts.ts), at 16 px. Tabler outline icon, @tabler/icons 3.46.0
// (users-group), the one the chat's account picker shows beside a shared
// account too.
const TABLER_USERS_GROUP: IconNode = [
  ["path", { d: "M10 13a2 2 0 1 0 4 0a2 2 0 0 0 -4 0" }],
  ["path", { d: "M8 21v-1a2 2 0 0 1 2 -2h4a2 2 0 0 1 2 2v1" }],
  ["path", { d: "M15 5a2 2 0 1 0 4 0a2 2 0 0 0 -4 0" }],
  ["path", { d: "M17 10h2a2 2 0 0 1 2 2v1" }],
  ["path", { d: "M5 5a2 2 0 1 0 4 0a2 2 0 0 0 -4 0" }],
  ["path", { d: "M3 13v-1a2 2 0 0 1 2 -2h2" }],
];

export function sharedIcon(size = 14): SVGElement {
  return decorative(TABLER_USERS_GROUP, "icon-shared", size);
}

// The Admin Center dashboard (2026-10-20, tiles.ts and board.ts): the tile
// labels' and the card headers' icon badges. Tabler outline icons,
// @tabler/icons 3.46.0 (tag, bolt, database, key, server, rocket,
// shield-check, coin, chevron-down). The Shared accounts tile reuses
// users-group above, and the Deploy hold tile player-pause.
const TABLER_TAG: IconNode = [
  ["path", { d: "M6.5 7.5a1 1 0 1 0 2 0a1 1 0 1 0 -2 0" }],
  [
    "path",
    {
      d: "M3 6v5.172a2 2 0 0 0 .586 1.414l7.71 7.71a2.41 2.41 0 0 0 3.408 0l5.592 -5.592a2.41 2.41 0 0 0 0 -3.408l-7.71 -7.71a2 2 0 0 0 -1.414 -.586h-5.172a3 3 0 0 0 -3 3z",
    },
  ],
];
const TABLER_BOLT: IconNode = [["path", { d: "M13 3l0 7l6 0l-8 11l0 -7l-6 0l8 -11" }]];
const TABLER_DATABASE: IconNode = [
  ["path", { d: "M4 6a8 3 0 1 0 16 0a8 3 0 1 0 -16 0" }],
  ["path", { d: "M4 6v6a8 3 0 0 0 16 0v-6" }],
  ["path", { d: "M4 12v6a8 3 0 0 0 16 0v-6" }],
];
const TABLER_KEY: IconNode = [
  [
    "path",
    {
      d: "M16.555 3.843l3.602 3.602a2.877 2.877 0 0 1 0 4.069l-2.643 2.643a2.877 2.877 0 0 1 -4.069 0l-.301 -.301l-6.558 6.558a2 2 0 0 1 -1.239 .578l-.175 .008h-1.172a1 1 0 0 1 -.993 -.883l-.007 -.117v-1.172a2 2 0 0 1 .467 -1.284l.119 -.13l.414 -.414h2v-2h2v-2l2.144 -2.144l-.301 -.301a2.877 2.877 0 0 1 0 -4.069l2.643 -2.643a2.877 2.877 0 0 1 4.069 0z",
    },
  ],
  ["path", { d: "M15 9h.01" }],
];
const TABLER_SERVER: IconNode = [
  ["path", { d: "M3 7a3 3 0 0 1 3 -3h12a3 3 0 0 1 3 3v2a3 3 0 0 1 -3 3h-12a3 3 0 0 1 -3 -3z" }],
  [
    "path",
    { d: "M3 15a3 3 0 0 1 3 -3h12a3 3 0 0 1 3 3v2a3 3 0 0 1 -3 3h-12a3 3 0 0 1 -3 -3l0 -2" },
  ],
  ["path", { d: "M7 8l0 .01" }],
  ["path", { d: "M7 16l0 .01" }],
];
const TABLER_ROCKET: IconNode = [
  [
    "path",
    { d: "M4 13a8 8 0 0 1 7 7a6 6 0 0 0 3 -5a9 9 0 0 0 6 -8a3 3 0 0 0 -3 -3a9 9 0 0 0 -8 6a6 6 0 0 0 -5 3" },
  ],
  ["path", { d: "M7 14a6 6 0 0 0 -3 6a6 6 0 0 0 6 -3" }],
  ["path", { d: "M14 9a1 1 0 1 0 2 0a1 1 0 1 0 -2 0" }],
];
const TABLER_SHIELD_CHECK: IconNode = [
  [
    "path",
    { d: "M11.46 20.846a12 12 0 0 1 -7.96 -14.846a12 12 0 0 0 8.5 -3a12 12 0 0 0 8.5 3a12 12 0 0 1 -.09 7.06" },
  ],
  ["path", { d: "M15 19l2 2l4 -4" }],
];
const TABLER_COIN: IconNode = [
  ["path", { d: "M3 12a9 9 0 1 0 18 0a9 9 0 1 0 -18 0" }],
  [
    "path",
    { d: "M14.8 9a2 2 0 0 0 -1.8 -1h-2a2 2 0 1 0 0 4h2a2 2 0 1 1 0 4h-2a2 2 0 0 1 -1.8 -1" },
  ],
  ["path", { d: "M12 7v10" }],
];
const TABLER_CHEVRON_DOWN: IconNode = [["path", { d: "M6 9l6 6l6 -6" }]];

export function tagIcon(): SVGElement {
  return decorative(TABLER_TAG, "icon-tag", 14);
}

export function boltIcon(size = 14): SVGElement {
  return decorative(TABLER_BOLT, "icon-bolt", size);
}

export function databaseIcon(): SVGElement {
  return decorative(TABLER_DATABASE, "icon-database", 14);
}

export function keyIcon(): SVGElement {
  return decorative(TABLER_KEY, "icon-key", 14);
}

export function serverIcon(): SVGElement {
  return decorative(TABLER_SERVER, "icon-server", 14);
}

export function rocketIcon(): SVGElement {
  return decorative(TABLER_ROCKET, "icon-rocket", 16);
}

export function shieldCheckIcon(): SVGElement {
  return decorative(TABLER_SHIELD_CHECK, "icon-shield", 16);
}

const TABLER_CLOUD: IconNode = [
  [
    "path",
    {
      d: "M6.657 18c-2.572 0 -4.657 -2.007 -4.657 -4.483c0 -2.475 2.085 -4.482 4.657 -4.482c.393 -1.762 1.794 -3.2 3.675 -3.773c1.88 -.572 3.956 -.193 5.444 1c1.488 1.19 2.162 3.007 1.77 4.769h.99c1.913 0 3.464 1.56 3.464 3.486c0 1.927 -1.551 3.487 -3.465 3.487h-11.878",
    },
  ],
];

export function cloudIcon(): SVGElement {
  return decorative(TABLER_CLOUD, "icon-cloud", 16);
}

const TABLER_GRIP: IconNode = [
  ["path", { d: "M8 5a1 1 0 1 0 2 0a1 1 0 1 0 -2 0" }],
  ["path", { d: "M8 12a1 1 0 1 0 2 0a1 1 0 1 0 -2 0" }],
  ["path", { d: "M8 19a1 1 0 1 0 2 0a1 1 0 1 0 -2 0" }],
  ["path", { d: "M14 5a1 1 0 1 0 2 0a1 1 0 1 0 -2 0" }],
  ["path", { d: "M14 12a1 1 0 1 0 2 0a1 1 0 1 0 -2 0" }],
  ["path", { d: "M14 19a1 1 0 1 0 2 0a1 1 0 1 0 -2 0" }],
];

/** The handle a card is dragged by (board.ts). */
export function gripIcon(): SVGElement {
  return decorative(TABLER_GRIP, "icon-grip", 16);
}

export function coinIcon(): SVGElement {
  return decorative(TABLER_COIN, "icon-coin", 16);
}

export function foldIcon(): SVGElement {
  return decorative(TABLER_CHEVRON_DOWN, "icon-fold", 16);
}
