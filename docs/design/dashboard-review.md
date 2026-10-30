# Dashboard design review: slots, Your move and the Parking Lot

Written 2026-09-24, against `main` at `d0c05c0` (upstream pinned at `v0.13.0`, patches
P1 to P6), with upstream `v0.15.0` read from the sync branch
(https://github.com/analyticendeavors/omnigent-ae/pull/27, open on this date).

Why: on 2026-09-24 the owner said the AE layer's navigation feels incongruent (Your move,
Parking Lot and Slots are three separate entries for one idea), the slot count should
be visible everywhere including the phone, the difference between "Parked sessions"
and the "Parking Lot" is unclear, and the lot page is one long scroll with Reference
at the bottom. They asked for a holistic review before more features land. This page is
that review and the plan it leads to.

Screenshots: the dashboard-review screenshot set (removed before publication), taken from a real server
built from this tree (upstream's SPA built, `config/server.yaml`, single-user local
mode, the extension bundle through `OMNIGENT_EXTENSION_DEV_BUNDLES`), with 13 seeded
sessions (three working, one of them with an open gate and one with an unfiled note;
one blocked; two in review; two parked, one of them linked to a lot task and one not;
four reference; one imported with no status) and a mock task-store serving six open
lot tasks (two overdue, one due today, three later). Playwright Chromium at device
scale factor 2, 1440x900 and 375x667.

## 1. Inventory: every AE surface today

| # | Surface | What it shows | Where it lives | Owner |
|---|---|---|---|---|
| 1 | Status icon on each sidebar row | `ae.status` as a lucide icon (working, blocked derived from an outstanding approval, review, parked, reference); legacy title glyph stripped | sidebar, every page, desktop and the phone drawer | patch P1 (`AeStatusIcon.tsx`, 4 lines in `Sidebar.tsx`, 3 in `sidebarNav.ts`) |
| 2 | "Slots n/3" nav row | working count from the sidebar's own cache, red at 3; links to the Parking Lot page | sidebar primary nav | patch P1 (`AeSlotsPill.tsx`) |
| 3 | "Your move" nav row | link to the Your move page, `dashboard` icon, order 50 | sidebar primary nav | extension manifest (`plugin.py`) |
| 4 | "Parking Lot" nav row | link to the lot page, `circle-parking` icon, order 100 | sidebar primary nav | extension manifest; the icon name needs P1's 4-line edit to `ExtensionPrimaryNavigation.tsx` |
| 5 | Your move page | Sessions waiting (gate, approval, blocked, review), Due from the lot, Pull requests, Steward's brief | `/extensions/analyticendeavors.parking-lot/your-move` | extension (`yourMove.ts`) over `GET /v1/ae/your-move` |
| 6 | Parking Lot page, sticky strip | Slots n/3, a chip per working and blocked session, plan limits | top of the lot page | extension (`capacity.ts`, `limits.ts`) over `/v1/ae/capacity`, `/v1/ae/limits` |
| 7 | Parking Lot page, Your move block | the Your move counts and a button to that page | under the strip | extension (`renderYourMoveBlock`) |
| 8 | Parking Lot page, sections | Working (working and blocked cards with Park, Done, Blocked), Open gates (A, B, C, Close), the Parking Lot (task-store items, Resume, Park a fork form), Review, Parked sessions, Unfiled, Reference | `/extensions/analyticendeavors.parking-lot/lot` | extension (`capacity.ts`, `gate.ts`, `lot.ts`, `sessions.ts`) |
| 9 | Status submenu | Working, Blocked, Review, Parked, Reference on a row's kebab and right-click | sidebar | patch P4 (`AeStatusMenu.tsx`, 8 lines in `Sidebar.tsx`) |
| 10 | Split view | up to four sessions side by side | `/split` | patch P5 |
| 11 | Plan limits popover | Claude Code and Codex windows behind the composer's context ring | session page | patch P6 |
| 12 | Upstream Inbox | every approval prompt across sessions, answerable in place; a count badge in the nav | sidebar primary nav, `/inbox` | upstream |

Three nav rows (2, 3, 4) lead to two pages that each embed part of the other (7 on
the lot page; the lot's due items on Your move). Nothing AE-specific appears on a
session page, and nothing at all on a session page on the phone
(`phone-375x667-session.png`).

## 2. Problems

### The owner's four

1. **Three nav entries for one feature.** "Your move", "Parking Lot" and "Slots 3/3"
   sit next to each other (`desktop-1440x900-nav.png`); two of them open the same page
   (Slots links to the Parking Lot), and the third opens a page whose summary is also
   a block on the second. The feature is the WIP system as a whole: what is running,
   what waits on the owner, what is parked, what is closed. It has one home, so it gets one
   entry.
2. **Slots are not visible where work happens.** The count lives in the sidebar and on
   the lot page. On the phone the sidebar is a drawer, so on a session (where the
   fourth prompt is typed) there is no count and no status at all
   (`phone-375x667-session.png`). On desktop with the sidebar collapsed, the same.
3. **"Parked sessions" versus "Parking Lot" is unclear, because they are two halves of
   one thing shown as two unrelated lists.** A parked fork is a task-store note
   (the durable record, resumable with no session). A parked session is a session
   row with `ae.status=parked`, which may or may not have a note (`ae.task`). In the
   seed, "Sidebar search shortcut" is a parked session linked to task `T-4`, and
   `sidebar-search-shortcut` is that task in the lot; the page shows them as two cards
   in two columns with nothing joining them (`desktop-1440x900-lot.png`,
   `desktop-1440x900-lot-reference.png`). "Box backup rotation" is parked with no
   note at all, which the three-slot system does not allow (everything parked has a
   resumable note), and the page does not flag it.
4. **One long scroll.** Reference starts 1,483 px down the lot page at 1440x900 and
   2,625 px down on the phone (page heights 1,937 and 3,079 px). No section collapses.
   Each reference card is 130 px for a title and a lone Open button.

### Found on the way

5. **Counts disagree.** The strip says Slots 3/3; the Working section under it says 4,
   because it lists the blocked session too. Blocked holds no slot
   (`docs/LABELS.md`), so the section header contradicts the strip one inch above it.
6. **The same session appears up to three times.** "Graphiti ingest retry" (working,
   gate open) is a Working card, an Open gates card and a count in the Your move block.
   "LinkedIn post draft" (working, one unfiled note) is a Working card and an Unfiled
   card. The steward's brief is a Reference card and the Your move brief.
7. **Your move re-lists what upstream's Inbox already answers.** An approval
   outstanding is a Your move card with an Open button; upstream's Inbox (present in
   `v0.13.0`, extended in `v0.15.0` by https://github.com/omnigent-ai/omnigent/pull/6926
   with auto mode on Claude permission prompts) renders the same prompt as an
   `ApprovalCard` you can answer in place, with its own nav badge. Ours is the weaker
   copy.
8. **Unplaced sessions are invisible.** "Imported: old Claude Code chat" has no
   `ae.status`; it appears in the sidebar with no icon and nowhere on either page
   (`/v1/ae/capacity` reports only unplaced sessions that are running). An import of
   25 Claude Code sessions (Analytic Endeavors' daily-use runbook, in a private repo) lands 25 rows in that dead zone
   unless the observer finds a glyph.
9. **Naming.** "Done" writes `review`; "Park" writes `parked` without a note (the
   tooltip says to file it with the form below, which is collapsed); "Unfiled" is
   internal vocabulary for "a note the agent could not file"; the nav's Parking Lot
   icon is the parked status icon, so the page and one of its statuses look the same.
10. **Phone layout.** Before the first section, the strip, the Your move block and the
    refresh line take 470 of 1,334 device pixels (35 percent of the screen). The
    strip's chips run off the right edge with no affordance to scroll. Working cards
    wrap their four buttons to two rows. The refresh line ("Refreshed 7:36:57 PM;
    again in 20 s or on focus.") is always there and never actionable.
11. **Empty states** are right per section ("Nothing waits for a read.") but every
    empty section still takes a full card's height, so an empty day is still a scroll.

## 3. How the pieces relate (the model the UI should show)

```
            session row (OmniGent)                          task (task-store Parking Lot)
            ae.status = ...                                  the resumable note
  ┌──────────────────────────────────────────┐          ┌──────────────────────────────┐
  │ working   holds a slot (max 3)           │          │ open, resurfaces on a date   │
  │ blocked   waiting on a person, no slot   │          │ NEXT, EST, tags              │
  │ review    done, read when convenient     │          │ Resume starts a new session  │
  │ parked ───┼── ae.task ─────────────────────────────▶│  (and stamps ae.task on it)  │
  │ reference closed, lookup only            │          └──────────────────────────────┘
  │ (none)    imported or never prompted     │             a task may have no session
  └──────────────────────────────────────────┘             (filed from a chat surface)
```

The Parking Lot is the list of notes; a parked session is a session that is shelved
behind one of those notes. So the page should show one list, the lot, and hang the
shelved session off its note ("session: Sidebar search shortcut, Open"), not show a
second list with the same word in its title. A parked session with no note is a
problem to fix, and it belongs with the other "a note is missing" problem, the
unfiled count, under one heading.

Proposed words, used everywhere (page, buttons, docs, LABELS.md prose; the label
values themselves stay as they are):

| Today | Proposed | Why |
|---|---|---|
| Your move, Parking Lot, Slots (three nav rows) | **Dashboard** (one row, with `n/3` on it) | one home for the system |
| Parked sessions | gone; the session shows on its lot card as "session" | one list for parked work |
| Unfiled | **Needs a note**: parked sessions with no `ae.task`, and sessions with `ae.unfiled > 0` | names the fix, not the token |
| Done (button) | **Review** | it writes `review` |
| Park (button) | **Park**, opening the park form with the session chosen | a park from the Dashboard always leaves a note |
| Open gates (section) | inside **Your move** | it is the most urgent kind of "waiting on you" |
| Working 4 | **Working 3/3**, then **Blocked 1** as its own section | the header matches the slot count |
| (nothing) | **Unsorted**: sessions with no status, collapsed | imports have a home |

## 4. Proposed information architecture

One nav row, **Dashboard**, trailing the slot count (`3/3`, red at the limit). One
page, top to bottom:

1. **Strip** (sticky): `3/3` slots, one chip per working session; on the phone the
   chips scroll horizontally with a fade at the edge. Plan limits stay in the strip.
2. **Jump bar** (sticky under the strip): a chip per section with its count
   (`Your move 4 · Working 3 · Blocked 1 · Review 2 · Lot 6 · Needs a note 2 ·
   Reference 4`). Tapping one opens that section and scrolls to it. This is the fix
   for "scroll far to find Reference" that survives any number of sections.
3. **Your move** (open by default): open gates with A, B, C, Close; approvals
   outstanding, with Open (answered in the session, or in upstream's Inbox); lot
   items due today or overdue, with Resume; pull requests; the steward's brief
   (collapsed). Blocked and review sessions are not repeated here: they have their
   own sections, and the jump bar counts them.
4. **Working** `n/3` (open): cards with Open, Park, Review, Blocked, and chips for
   "gate open" and "1 unfiled" instead of second cards elsewhere. Park opens the
   park form with the session chosen.
5. **Blocked** (open).
6. **Parking Lot** (open): task-store items in the store's order; a card whose task
   has a parked session shows it inline with Open; Resume as today; Park a fork at
   the end.
7. **Review** (open): Open, Working, Reference.
8. **Needs a note** (open): parked with no note (File a note opens the park form),
   parked with a task that is no longer open, unfiled counts.
9. **Unsorted** (closed by default): sessions with no `ae.status`, with Working,
   Review and Reference buttons.
10. **Reference** (closed by default): one line per session, no button row; the
    title opens it.

On a wide screen, 3 to 6 are the main column and 7 to 10 the side column; on the
phone it is one list in that order.

Every section header is a button with `aria-expanded`; the open or closed state is
remembered per section in the extension's `storage.user` (key `sections.open`), in
memory when that is unavailable. An empty section renders as its header alone, with
a zero count and no body, so an empty day fits on one phone screen.

(Corrected 2026-09-24 while building: the first draft sent approvals to `/inbox`,
but the extension SDK can navigate only to extension pages, sessions and the
composer, not to a host route. So an approval keeps its card and Open.)

**Always-visible slots.** Three places, one count:

- the sidebar row (desktop, the phone drawer): "Dashboard 3/3" (patch P1, the row it
  already owns);
- the session header (desktop and phone): a small chip `● Working · 3/3` with the
  session's own status and the count, tap opens the Dashboard. On the phone this is
  the only AE signal on a session today, so it is the most valuable piece of the
  patch work (a new file and 2 lines in upstream's `ChatHeader.tsx`);
- the Dashboard's strip.

A fourth: the installed web app's icon badge (`navigator.setAppBadge(slots)`,
supported by Safari on iOS 16.4 and later for Home Screen apps, which is how the owner
opens it on the phone). It needs notification permission on iOS. First proposed as a
later decision; the owner asked for it the same day, and it shipped as patch P8.

### Desktop mockup (1440 wide, sidebar open)

```
┌───────────────┬──────────────────────────────────────────────────────────────────────┐
│ Omnigent      │ (3/3) ▶Pricing table… 1h31 ▶Graphiti ingest… 26m ▶LinkedIn post… 11m │ strip
│               ├──────────────────────────────────────────────────────────────────────┤
│ New session   │ Your move 4 · Working 3 · Blocked 1 · Review 2 · Lot 6 · Needs a     │ jump bar
│ Automations   │ note 2 · Unsorted 1 · Reference 4                                    │
│ Inbox       1 ├──────────────────────────────────────────────────────────────────────┤
│ Dashboard 3/3 │ ▾ Your move 4                                                        │
│               │   ⑂ Graphiti ingest retry  FORK: Dedupe nodes…   [A][B][C][Close]    │
│ Sessions      │   ⏲ eur-currency-formatting   overdue since Sep 22        [Resume]   │
│ ▶ Pricing…    │   ⏲ renewal-reminder-email    overdue since Sep 23        [Resume]   │
│ ▶ Graphiti…   │   ⏲ graphiti-dedupe           due today                   [Resume]   │
│ ⏸ Client…     │   ▸ Steward's brief, Thu 08:00                                       │
│ ✓ OmniGent…   │ ▾ Working 3/3                        ▾ Review 2                      │
│ ✓ Plan lim…   │   ▶ Pricing table padding fix 1h31    ✓ OmniGent split view          │
│ Ⓟ Sidebar…    │     NEXT Rerun the EUR case…            [Open][Working][Reference]   │
│ Ⓟ Box back…   │     [Open][Park] [Review][Blocked]    ✓ Plan limits popover          │
│ ▦ Tailscale…  │   ▶ Graphiti ingest retry  ⑂ gate open                               │
│ …             │   ▶ LinkedIn post draft  1 unfiled   ▾ Needs a note 2                │
│               │ ▾ Blocked 1                            Ⓟ Box backup rotation         │
│               │   ⏸ Client renewal note                   parked, no note  [Park]    │
│               │ ▾ Parking Lot 6                        ▶ LinkedIn post draft 1 unf.  │
│               │   ⚑ sidebar-search-shortcut Sep 27     ▸ Unsorted 1                  │
│               │     session: Sidebar search shortcut [Open]    [Resume]              │
│               │   …                                    ▸ Reference 4                 │
└───────────────┴──────────────────────────────────────────────────────────────────────┘
```

### Phone mockup (375 wide)

```
┌───────────────────────────────────┐     Session page (patch)
│ ☰                                 │     ┌───────────────────────────────────┐
│ (3/3) ▶Pricing t… ▶Graphiti… ▶Li›│     │ ☰ Pricing table padding… [▶ 3/3] ⋯│
├───────────────────────────────────┤     │                                   │
│ Your move 4 · Working 3 · Block…› │     │  …transcript…                     │
├───────────────────────────────────┤     │                                   │
│ ▾ Your move 4                     │     │ [ composer                     ↑ ]│
│  ⑂ Graphiti ingest retry          │     └───────────────────────────────────┘
│    FORK: Dedupe nodes in the…     │      chip = this session's status and
│    [A] [B] [C] [Close]            │      the slot count; tap → Dashboard
│  ⏲ eur-currency-formatting        │
│    overdue since Sep 22 [Resume]  │
│ ▾ Working 3/3                     │
│  ▶ Pricing table padding… 1h31    │
│    [Open][Park] [Review][Blocked] │  ← one row at 375 (smaller label set)
│ ▸ Blocked 1                       │
│ ▸ Parking Lot 6                   │
│ ▸ Review 2                        │
│ ▸ Needs a note 2                  │
│ ▸ Unsorted 1                      │
│ ▸ Reference 4                     │
└───────────────────────────────────┘
```

## 5. Thin-fork cost, piece by piece

Upstream line counts are lines added or changed in upstream-owned files; new AE files
inside the patch are free by rule 1 of `CLAUDE.md`.

| Piece | Where | Upstream lines | Notes |
|---|---|---|---|
| One Dashboard page (merge Your move and the lot page, sections by status, jump bar, collapsible remembered sections, Needs a note, Unsorted, parked sessions on their lot cards, counts that agree) | extension | 0 | `main.ts`, `capacity.ts`, `sessions.ts`, `lot.ts`, `yourMove.ts`, `dom.ts`; the `lot` route stays (P1's link target), the `your-move` route renders the same page for old bookmarks |
| One nav entry (drop the extension's two nav rows; P1's row becomes "Dashboard n/3") | extension + P7 | 0 now, **-4** at the next sync | the extension contributes no nav row; P7 relabels `AeSlotsPill.tsx` (an AE file). P1's 4-line icon edit to `ExtensionPrimaryNavigation.tsx` loses its user and is dropped when the next sync rebuilds the fork branch (built 2026-09-24 as P7 rather than an amendment to P1, so the published branch was not rewritten) |
| Session header chip (status plus `n/3`, tap opens the Dashboard) | patch P1 | +2 in `ChatHeader.tsx` (the import and the element in the right cluster, before `PresenceAvatars`) | new file `AeSessionChip.tsx` reusing `useAeSlotCount` and `aeDisplayStatus`; v0.15.0 moved mobile viewer avatars into the session menu (https://github.com/omnigent-ai/omnigent/pull/7987), which frees exactly that space on the phone |
| Approvals: no second copy of upstream's Inbox | extension | 0 | not possible yet: the SDK has no navigation to a host route such as `/inbox`, so an approval keeps its Your move card with Open; if upstream adds route navigation, the card becomes one line to the Inbox |
| App icon badge | P8 | 0 | built 2026-09-24 at the owner's request: `lib/aeAppBadge.ts` and one effect in `AeSlotsPill.tsx` |

Net: the patch series grows by 2 upstream lines now and shrinks by 4 at the next sync; nothing new needs a
patch except the header chip. No prompt rule is added (rule 4); "Park" always leaving
a note makes the "park means a note" rule something the UI enforces instead of
something the agent is told.

### What upstream `v0.15.0` already has, and what we build on

- **Inbox** (approvals across sessions, answerable in place; https://github.com/omnigent-ai/omnigent/pull/6926
  adds auto mode on Claude permission prompts). Build on it where the SDK allows:
  the Dashboard adds nothing to approvals beyond the Your move card that opens the
  session, and hands off to `/inbox` once an extension can navigate to it.
- **Fuzzy session finder** (Cmd+Option+S, https://github.com/omnigent-ai/omnigent/pull/7226).
  Nothing to duplicate: the Dashboard never grows a search box; Reference collapsed
  plus the finder is how an old session is found.
- **Sidebar error icon** (https://github.com/omnigent-ai/omnigent/pull/7251) sits in
  the row's trailing badge; P1's icon leads the row. No conflict.
- **Extension seams**: `v0.15.0` still offers pages, primary navigation (id, label,
  page, icon, order, `when`) and reserved commands. There is no badge on a nav
  contribution and no header slot, which is why the count on the nav row and the
  session chip stay in P1. If upstream adds a nav badge, the count moves to the
  extension and P1 loses `AeSlotsPill.tsx` entirely; worth proposing upstream.
- **No dashboard or WIP concept upstream**: nothing to merge with.

## 6. Order of work

1. Now, extension only (no conflict with the sync in
   https://github.com/analyticendeavors/omnigent-ae/pull/27 beyond a bundle rebuild):
   the Dashboard page, one extension nav row, collapsible remembered sections, the jump
   bar, Needs a note, Unsorted, counts that agree, and Park opening the park form.
2. After the sync merged (the same day), on `ae/v0.15.0`: a new patch, P7, renames
   P1's row to "Dashboard n/3" (active on the Dashboard's routes) and adds the
   session header chip; the extension drops its nav row. Built as a seventh patch
   rather than an amendment to P1, because amending P1 would rewrite the published
   fork branch; P1's four-line icon edit to `ExtensionPrimaryNavigation.tsx` stays
   until the next sync rebuilds the branch, and is dropped then (it has no user).
3. The app icon badge, asked for by the owner the same day: patch P8, no upstream file
   touched (`navigator.setAppBadge` from the sidebar row, cleared at zero; on iOS it
   needs the notification permission upstream already requests).

## 7. Addendum, 2026-09-25: the lot and the header after a day of use

The owner's feedback (https://github.com/analyticendeavors/omnigent-ae/issues/28), mostly
from their phone. The lot card showed two grey tags and "+1" with room to spare; it now
shows every tag in task-store's own colour and icon, and the repository on the
collapsed card. The lot gains a search field and a filter sheet (quadrant, overdue or
this week, tag, repo), remembered per device. This revises one line of section 5:
"the Dashboard never grows a search box" was about finding a session, which
upstream's finder does; finding a parked note among 14 is a different job and no
upstream surface does it.

The strip in section 4 (item 1) is gone. Its Slots pill and chip per working session
repeated what the sidebar shows ("Dashboard n/3" and every session with its status)
and what the Working section shows one inch lower. The count moved to the Working chip
of the jump bar (red at the limit), and the jump bar and the plan limits share one row
that wraps across the width on a wide screen. On the phone the sticky top is one row
shorter.
