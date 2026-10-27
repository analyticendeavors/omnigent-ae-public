# omnigent-ae-extension

The Dashboard and the Admin Center, as an OmniGent V1 browser extension
(`upstream/docs/extending/extensions.md`). Extension id
`analyticendeavors.parking-lot`; the Dashboard at
`/extensions/analyticendeavors.parking-lot/lot` (the Parking Lot page from Phase 3,
2026-09-23, the Dashboard since 2026-09-24), also served at
`/extensions/analyticendeavors.parking-lot/your-move` so bookmarks from the old Your
move page land on it. The Dashboard has no nav row of its own: the sidebar's
"Dashboard n/3" row (patch P7, which renamed P1's Slots row) links to the `lot` route
and carries the slot count, which a contribution's static label cannot. The
extension's one nav row is the Admin Center's (2026-10-16 as Fabric Admin, below), at
`/extensions/analyticendeavors.parking-lot/fabric`. Why one Dashboard page: Reid found "Your move", "Parking Lot" and "Slots"
three entries for one feature (2026-09-24); the review and the plan are
`docs/design/dashboard-review.md`.

The page is an extension so upstream UI churn cannot break it (handoff decision 6):
it runs in upstream's sandboxed iframe and reaches data only through the host RPC
that patch P2 adds (`patches/0002-*`): `sessions.listAll()` with `ae.*` labels and
`server.request`, an allowlist over the host's authenticated fetch that admits `GET`
and `POST` under `/v1/ae/`, an `ae.*`-only labels `PATCH /v1/sessions/{id}`, and one
user message to `POST /v1/sessions/{id}/events`. task-store stays the system of
record; the page is a view over it.

## Sections

Every section heading is a toggle; which sections are open is remembered per
section in `storage.user` (key `sections.open`). Reference and Unsorted start
closed. A section with nothing in it shows its heading alone. The top is the Session
Dashboard header (2026-10-17, below), a read-out in place of the chip per section
it had until then (a chip opened its section and scrolled to it; the page already
shows every section, so the jump bar went). The strip above the chips
(a Slots pill and a chip per working or blocked session) was removed on 2026-09-25
(https://github.com/analyticendeavors/omnigent-ae/issues/28): the sidebar already
lists those sessions with their status and "Dashboard n/3", and the Working and
Blocked sections list them again.

| Section | Source | Actions |
|---|---|---|
| Header | `GET /v1/ae/capacity` for the slots; the pull requests, Your move and the lot as loaded below; `GET /v1/ae/release`; `GET /v1/ae/limits` | Flow or Brief (remembered); a slot block opens its session; a row or a link opens its section |
| Your move | `GET /v1/ae/your-move`: open gates, approvals outstanding, lot items due today or overdue, pull requests without a card below, the steward's brief (blocked and review have their own sections) | a gate: Do it now, Write a note, Save for later, Drop it in place (they post A, B, C, close; labelled A, B, C, Close until 2026-09-27, when Reid asked for labels that say what each does), then Open; an approval: Open; due: Resume; a pull request: Merge, the same button and confirm as Open PRs (2026-09-24, below), and Open on GitHub and a small Copy (Copy link alone until 2026-09-27) |
| Open PRs | `GET /v1/ae/prs`: every open pull request a session tracks (upstream's per-session tracking), as a card with CI counts, mergeability, review state and the sessions it came from (2026-09-24) | Merge (off with the server's reason; asks once on one row: the method toggle when the repo allows both, Confirm merge, Cancel (2026-09-26); afterwards Mark session done with a "frees a slot" hint, which sets the session to review, only when that merge was the session's last open pull request, else a muted "N more open pull requests in this session." line (2026-09-25, `web/src/reviewOffer.ts`; labelled Set session to review until 2026-09-26)); Open on GitHub and a small Copy (Copy link alone until 2026-09-27) |
| Working n/3 | `ae.status=working` (the slots); chips for "gate open" and "n unfiled" | Open; Park (opens the park form with the session chosen, so a park leaves a note); Review; Blocked |
| Blocked | working with an approval outstanding, or `ae.status=blocked`; holds no slot | Open, Working |
| Parking Lot | `GET /v1/ae/lot?status=open`; `GET /v1/ae/lot/tags?icons=true` for tag colours and icons; a parked session with `ae.task` shows on its task's card | Search and Filters (below); a tag chip filters by its tag; Resume: `POST /v1/ae/lot/{id}/session` starts a session with the task's block as its first message and opens it; the Park form posts the worker's fields to `POST /v1/ae/lot/park` |
| Review | `ae.status=review` | Open, Working, Reference |
| Needs a note | parked with no `ae.task`, parked with a task that is not open in the lot, or `ae.unfiled > 0` | Open; File a note (the park form, session chosen) |
| Unsorted | no `ae.status` (imports, never prompted) | Open, Working, Review, Reference |
| Reference | `ae.status=reference`, one line each | the title opens it |

Main column: Your move, Open PRs, Working, Blocked, Parking Lot. Side column: Review, Needs a
note, Unsorted, Reference. Under 720 px it is one list in that order.

## Session Dashboard header (2026-10-17)

The page is the Session Dashboard since 2026-10-17 (the Dashboard before; both page
titles and patch 0070's sidebar row). Its header (`web/src/header.ts`) reads out the
state in one of two views, picked with the Flow and Brief toggle at its right and
remembered per user in `storage.user` (key `header.view`):

- **Flow**, the default, drawn as concept 3: the title with a usage bar per harness
  (its longest window, the week: green, amber from 70%, red from 90%), the slots as
  blocks, one per working session with its title and elapsed time (over the limit
  hatched red, older than eight hours outlined, a free slot dashed; a block opens its
  session) over a row naming each slot on a wide window, and a pipeline of pips: up
  to three open pull requests with a pip per check (green passed, amber running, red
  failing), the release's three steps (a newer tag, no session mid-reply, installed;
  hollow when the status is unknown, and the middle pip never claims a pin or hold on
  the box is clear), a pip per item on Your move (one grey pip when clear), and three
  for the lot, blue for each task coming back within a week, overdue ones included.
- **Brief**, drawn as concept 2: a dot (red over the limit, amber at it) and the
  title, one sentence, worst thing first ("Over capacity: 4 working, limit 3. Oldest
  is ... at 3d 5h; park it or finish it. 1 PR is waiting on CI. Nothing is on your
  move. Next from the lot: ..., back Mon Oct 19."), and a muted footer: the usage
  windows, the release, then the parked, needs-a-note and reference counts.

The first build of both views (https://github.com/analyticendeavors/omnigent-ae/pull/111 and
https://github.com/analyticendeavors/omnigent-ae/pull/115) drifted from the concepts: text in
place of the usage bars, a seven-day lot strip, age bars, round dots. On 2026-10-17
Reid asked for them exactly as drawn, so this is that. One part of concept 3 is not
data the server has: the release's deadline ("held to 09:52"), which lives in the
box's auto-update state, so the row says how many sessions hold it instead. The pips
are hidden from screen readers; each row's text says the same.

The header is not sticky: it is taller
than the chip bar was and, pinned, would cover a third of a phone screen. The release
row reads `GET /v1/ae/release` (`ae_omni_policies/release.py`): the running tag
(`OMNIGENT_IMAGE_TAG`), the newest `ae-v*` tag on GitHub through the server's token
(GitHub's matching-refs call, so every tag in one answer; cached two minutes), and how
many sessions are mid-turn by auto-update's own rule (running or waiting, runner seen
within five minutes), the ones it waits for since
https://github.com/analyticendeavors/omnigent-ae/pull/109. "Next" says only that a
newer tag exists: a pin, a rolled-back tag or a hold on the box is out of view, so it
never claims an install. The brief calls a pull request ready only when its card would
(open, Merge allowed, no conflicts, checks passed or none), and the header leaves out
one just merged from the page. A section folded as empty is named as text, not a
link, and the header draws its title alone until the remembered view is read. Reid picked flow
with a toggle to the brief from three concepts on 2026-10-17.

## Admin Center (2026-10-16 as Fabric Admin; renamed 2026-10-18)

Why: Reid wanted the aetraining wake button, and room for more capacity buttons,
inside omnigent-ae next to the Dashboard. Off was added at his request, guarded by a
separate suspend key so holders of the shared wake link can only wake.

Renamed 2026-10-18: the page is the hub for admin sections still to come (Databricks
compute, spend, deploy hold, secrets health), so the Fabric capacity cards sit under
their own "Fabric capacity" heading (`section.admin-section`, with the "Updated" line
that belongs to the Fabric poll). The page id, route and view (`fabric`), the nav id,
the gauge icon, the `fabric-*` classes and the API paths stayed, so old links keep
working.

A page of its own (`web/src/fabric.ts`, the manifest's `fabric` view), reached from an
"Admin Center" row with lucide's gauge icon in upstream's extension slot between
Inbox and Usage (`primary_navigation` in `plugin.py`; no core patch). The same bundle
draws it: `main.ts` picks the page by the view the host names at activation.

| Part | Source | Actions |
|---|---|---|
| A card per capacity | `GET /v1/ae/fabric` (`ae_omni_policies/fabric.py`): each capacity the server has an `AE_FABRIC_WAKE_URL_<NAME>` for, its state read through that capacity's Logic App, and whether it holds a suspend key | Wake when Paused: `POST /v1/ae/fabric/{name}/wake`; Off when Active and the server holds the key: `POST /v1/ae/fabric/{name}/suspend` |

- **Asks once, on one row.** Like the pull request card's Merge: the card's divided
  confirm step holds one sentence, Cancel and the button that acts. Wake: "Wakes
  aetraining. Bills at least 7 hours (about $5)." Off: "Turns aetraining off now.
  Anyone using it is cut off; the next wake bills at least 7 hours." One call per tap,
  never retried.
- **States.** Active (a green tick), Paused (a muted pause), Waking, Resuming,
  Suspending and Pausing, plus ARM's Provisioning, Updating, Scaling, Preparing and
  Deleting (an hourglass, no motion), Wake failed and Suspend failed (a
  red alert), anything else Unknown (a dashed circle). After a wake or a suspend the
  card shows what the Logic App answered and the next read comes 15 s later, not at
  once: ARM can still say Paused for a few seconds after a resume starts, which would
  put Wake back under a thumb. A read that began before the action is dropped for that card, the busy poll stays at
  15 s, and a hidden tab skips scheduled reads and reads once on return.
- **Refresh.** Every 15 s while any capacity is Waking, Resuming, Suspending or
  Pausing, else every 60 s, and on focus.
- **Errors.** A capacity whose Logic App fails shows the server's sentence on its own
  card; a failed Wake or Off keeps its sentence on that card until the next try; a
  failed list read keeps the last cards with one line above them. With nothing
  configured the page shows the server's how-to sentence (Key Vault
  `aetraining-wake-url`, `deploy/pull-secrets.sh`).
- **Phone.** One column with 16 px gutters at 375 px.

**Second pass, 2026-10-16 (extension 0.3.0).** Reid approved a compact mockup: the
first pass's buttons were oversized for what they do.

- **One row per capacity.** A status dot, the name and a small state pill; a muted
  meta line (SKU, region with ARM's lowercase form written out, `$0.72/h`, "up 2 h
  14 m, about $1.60" from `resumedAt`, "sleeps around 1:00 AM if idle" from the
  server's `sleepsAt` in the viewer's local time); the actions at the right as 32 px
  icon buttons (40 px on a touch screen) and Start as a small labelled button. Start,
  Pause and Keep awake open their step under the row; the confirm sentences are
  "Start aetraining? It bills at least 7 hours (about $5.04) and is ready in about 2
  minutes." and "Pause aetraining? Anyone using it is cut off."
- **Links.** One icon button opens a small menu (a menu button with arrow keys,
  Escape and Tab): Open in Azure (`https://portal.azure.com/#resource{capacityId}/overview`)
  and Open in Fabric (Fabric's capacity settings page). Hidden without `capacityId`.
  The host admits exactly these two shapes (patch 0064). When a popup is blocked
  the card shows the URL to copy, with a Copy button.
- **Keep awake.** A clock button when Active and the server holds the suspend key
  (`can_keep_awake`): 2, 4 or 8 hours, each saying until when that keeps it up and
  what it adds ("Keeps it up until about 3:20 AM (about $2.88 more)."), worked out
  from `sleepsAt` with the runbook's hourly rule; a choice that adds nothing says so
  and cannot be sent, and the first pick is the shortest that adds something.
  `POST /v1/ae/fabric/{name}/keepawake` with `{"hours": N}`; the next read comes at
  once for the new sleep time.
- **Without the new fields** (the live Logic App does not send them yet) the row
  draws as before: no meta line on an Active row, "Wake bills at least 7 h, about $5"
  on a Paused one, no links, no keep awake.

**Third pass, 2026-10-16 (review fixes).** The page holds no copy of the runbook's
rule, which changed the same day (keep awake has its own `keepAwakeUntil`).

- **Projections from the server.** Each row carries `keepAwakeOptions` (2, 4 and 8
  hours, each with its new `sleepsAt` and `addsTime`, from `fabric.py`'s `sleeps_at`)
  and `wakeBilledHours`; the chooser and the Wake sentence only format them. Without
  `wakeBilledHours` the Wake sentence names no figure.
- **Actions answer the full row.** Wake, Off and Keep awake redraw the card from the
  answer at once. A read still out when the answer lands keeps its other rows, not
  that card's, and one more read follows it.
- **Links menu.** It is the Dashboard's `cards.ts` `rowMenu` with the links icon, so
  it flips up or right at the frame's edge and comes back after a redraw; the open
  card carries `menu-open`, which lifts it over the next card (each card is its own
  stacking context through `container-type`).
- **Touch.** Every action is 44 px under `pointer: coarse`, 8 px apart.

**Capacity fleet, 2026-10-21.** Reid wanted every Fabric capacity he has on the
Compute card, each with its size, state, Pause or Start, and a resize. With the
server's `AE_FABRIC_CAPACITIES_URL` and `AE_FABRIC_CAPACITIES_KEY` set, `GET
/v1/ae/fabric` returns one row per capacity the shared Logic App lists, and the
card draws one row per row returned, never a fixed count.

- **Size picker.** On a row the server can resize (`canScale`) that is Active or
  Paused, the SKU at the head of the meta line is a small select of `skuChoices`
  (F2 to F64), so it costs the actions no width on a phone (44 px tall under
  `pointer: coarse`). The subscription's name joins the meta line.
- **Resize asks once.** Picking a size only opens the confirm step, which names both
  sizes and both prices an hour: "Resize teamlab from F4 ($0.72/h) to F8
  ($1.44/h)? Billing changes at once." The new price is the current one per capacity
  unit times the new size's units. Only its Resize button sends `POST
  /v1/ae/fabric/{name}/scale` with `{"sku": "F8"}`; Cancel puts the picker back.
  The answer is the full row (usually Updating, a busy state, so the picker steps
  aside and the next read comes 15 s later).
- **Start and Pause** go through the fleet on the server for every row; keep awake, the
  sleep time and the minimum wake bill stay on the rows with a wake Logic App
  (aetraining). A list the server could only partly read says so above the rows.

The page never sees a Logic App URL or key: the server holds both and returns only
names, states and its own sentences.

### Deploys (2026-10-19)

Why: a deploy restarts the server and often the host, ending every turn in flight.
Until now, holding one meant stopping the box's auto-update timer by hand, which no
page showed. This was the Admin Center roadmap's deploy hold.

The first `section.admin-section`, above Fabric capacity, built by
`web/src/deploys.ts` and mounted from `activateFabric`. It shows the running release
from `GET /v1/ae/release`, the same route as the Dashboard header. A newer release
adds "ae-vX is waiting to deploy", plus "for N busy sessions" when sessions are
mid-turn. Below that, the hold from `GET /v1/ae/deploy/hold`: "On hold until 9:46
PM, set by reid" and the time left, or "Deploys run automatically. A hold pauses
them for up to 3 hours and ends on its own."

Hold 1 h, 2 h and 3 h (`POST /v1/ae/deploy/hold` with `{"hours": N}`) set a hold
that many hours from now, replacing any current one. Resume
(`POST /v1/ae/deploy/resume`) shows only while a hold is on. Neither asks first:
a hold is harmless and ends on its own. Resume is a POST because the extension
SDK sends only GET, POST and PATCH. The section re-reads every 60 s, on focus, and
1 s after a hold runs out. A read that began before an action is dropped, so an
old hold never comes back after Resume.

How the box obeys the hold is in `deploy/README.md`, "The deploy hold".

### Shared Claude accounts (2026-10-20)

After Box host: the company-paid Claude logins every user's account picker lists,
marked Shared, beside their host's own (patch 0117). Why: a second AE user's host can
use Claude accounts the admin manages, without anyone copying a token between hosts.
Each account is a Fabric-style row: its label, a Shared pill (Tabler `users-group`),
the id a host selects (`shared:<name>`) and who added it. An admin also gets Rename
(an inline label editor) and Remove (a second press, "Remove for everyone", confirms),
and an Add form under the list: name, optional label, and the OAuth token from
`claude setup-token` in a password field that clears as soon as the add answers. The
page never receives a token back. A missing `AE_SHARED_ACCOUNTS_KEY` shows as
an error naming it; a non-admin sees the list and a line saying only an admin can
change it. Reads on activate and on focus (`sharedAccounts.ts`; routes in
`ae_omni_policies/shared_accounts_routes.py`).

Retired the same day by the dashboard redesign: managing shared accounts moves to
OmniGent's Settings > Claude accounts, and the page keeps only their count, as the
Shared accounts tile ("Manage in Settings"). An extension page cannot open a Settings
page: the SDK's `navigation.openPage` opens only the extension's own pages.

### Dashboard layout (2026-10-20)

Reid approved concept C. The page uses the pane's width. Under the header (title,
"Updated", one refresh button that runs every section's normal read) sits a tile strip
(`tiles.ts`): Release, Deploy hold, Box host, Fabric, Databricks, Secrets and Shared
accounts, each with a coloured left edge by health. Each section builds its own tile from
the state it already has, so the strip makes no requests. The cards (`board.ts`) are
Deploys, Compute (Fabric rows, then Databricks warehouses), Databricks credit and Box
health (host, then secrets). They sit in fixed column stacks, and the last card in each
column stretches. At 1040 px and wider there are three columns: Deploys over credit,
then Compute, then Box health. From 520 px there are two: Deploys over Compute, and
credit over Box health. Below 520 px there is one column, with no tiles, and each card
folds to its header line. Deploys starts open; the other cards start folded unless
they need attention, and each card remembers its fold in `sections.open` under an
`admin-` id.

### Accounts card (2026-10-21)

A fifth card, at the head of the middle column: every signed-in Claude login (the
default, personal and shared ones) and the Codex login, grouped Claude then Codex. Each
row is the label, the plan when known, a 5-hour and a weekly bar with the percent and
the reset time (amber from 70%, red from 90%), the live sessions on it and the reading's
age ("as of 3h ago", or "no reading yet" for a signed-in account the relay has not read).
Why: Reid runs several accounts, and their usage was visible only one session at a time.
The Shared tile becomes the Accounts tile: the account count and the fullest window,
coloured by the same steps, still noting "Manage in Settings". On a phone the folded
card names the fullest account and its percent. Columns: three are Deploys over credit,
Accounts over Compute, then Box health; two are Deploys over Compute, then Accounts over
credit over Box health; one is Deploys, Accounts, Compute, credit, Box health. Reads
`GET /v1/ae/limits/accounts` on activate, on focus and every minute (`accounts.ts`;
the data path is in `docs/PLAN-LIMITS.md`, "Accounts summary").

### Admin Center polish (2026-10-20)

Two small fixes to the dashboard. The secrets row in Box health now draws one chip per
distinct use, in first-seen order, so a feature that needs two keys ("Microsoft sign-in",
"Databricks section") no longer shows twice. A chip is red when any of its keys is missing
and required, grey when one is missing and optional, green otherwise; its tooltip lists
each key behind it with its state. The counts (the "10 of 10" tile, the pill, the folded
summary) still count keys. The Deploys hold line and the Deploy hold tile now show only
the part of the holder's address before the "@" ("by reid.havens"), with the full address
in the tooltip; a holder with no "@" shows whole. The name in the hold line never wraps
mid-word.

### Databricks (2026-10-19)

Why: the Admin Center roadmap's Databricks compute and spend strip, built on the
Databricks on AWS account "Analytic Endeavors (MVP)", whose prepaid credit is a hard cap
(no payment method on file). Serverless only, because classic clusters on AWS also
bill EC2.

A second `section.admin-section` under Fabric capacity, built by `web/src/databricks.ts`
and mounted from `activateFabric`, with its own poll (15 s while a warehouse is
starting or stopping or spend is being read, else 60 s; skipped while hidden, read on
focus). Each SQL warehouse is a row in the Fabric style (dot, name, state pill, size,
auto stop, serverless) with Start or Stop, each asking once under the row; Start's
sentence says the warehouse bills until it auto stops. Below the rows, the spend strip
shows two bars against the credit, "This month" and "Credit since <start>", led by
whichever `AE_DATABRICKS_CREDIT_CADENCE` names, with the time the billing data runs
to (it lags hours). With no credit set (no built-in default; the server sends null),
it shows this month's spend alone.

Spend is a query on a warehouse, so it costs a wake: the server reads it on its own
only when the warehouse is already running and the last attempt is over 30 minutes
old, and otherwise only when someone taps "Refresh (wakes warehouse)". Nothing starts
a warehouse on a timer. The reading runs in the background (a cold start plus the
query can pass the host's ten seconds) and is kept in `AE_DATABRICKS_STATE_FILE`.
Without the `system.billing` grant the strip names the grants to ask for.

The "Open in Databricks" buttons open the workspace and each warehouse in a new tab
since patch 0106 (2026-10-19), which adds those two link shapes, on any
`*.cloud.databricks.com` workspace, to the host's `navigation.openExternal` allowlist. Before it the page could only show the URL with
Copy, because patch 0066 admitted only Azure portal and Fabric links. The page never
sees the client ID, the secret or a token.

One thing a contribution cannot do: the sidebar's "Dashboard n/3" row (patch P7)
lit for every route under this extension, so it showed as current on the Admin Center
too, beside the Admin Center's own row. That was a core change to `AeSlotsPill.tsx`'s
prefix match, and patch 0059 made it: the row now lights only on the Dashboard's own
pages.

## Your move (2026-09-24)

Why: Reid runs several sessions at once, merges from his phone,
and kept asking "what's left". The section answers "what is waiting on me right
now" from one route, `GET /v1/ae/your-move` (`ae_omni_policies/your_move.py`),
which reads signals that already exist and adds no prompt rule. It was a page of
its own until the Dashboard merge the same day; the route still reports blocked and
review sessions, and the Dashboard shows those in their own sections instead of
twice. Each source renders its own error: a task-store outage leaves the sessions
and pull requests in place. Without `AE_GITHUB_TOKEN` one line says pull requests
are off (`deploy/README.md`, "The GitHub token").

Two things the extension SDK does not offer at v0.13.0 (nor v0.15.0), so they are
not here: a count badge on the navigation link (a contribution's label is static;
the slot count on the nav lives in patches P1 and P7), and navigation to a host route
such as `/inbox` (only pages, sessions and the composer). A third, opening a pull
request in a new tab, came with core patch P25 (below).

### Open on GitHub (2026-09-27)

Why: Reid works from an iPhone (Safari) and a Windows laptop (Firefox) and hates copy
and paste; every pull request card offered only Copy link, because the frame is
`sandbox="allow-scripts"` and upstream's `navigation.openExternal` opened only URLs
the host handed out itself (the Dashboard's pull requests come from the AE routes).
Core patch P25 (`patches/0025`) lets the host also open a github.com pull request or
issue URL of an exact shape (`upstream/web/src/lib/aeExternalUrls.ts`), for any
extension holding the `navigation` permission, which this one already declares
(`plugin.py`).

`web/src/openLink.ts` builds the button: Tabler's `external-link` icon (inlined in
`icons.ts`, like the merge method's tick) and "Open on GitHub", in the compact `.btn`
style, on every Open PRs and Your move pull request card. A tap calls
`context.navigation.openExternal(url)`; the host opens a new tab, which a browser
opens in itself, the desktop app hands to the OS default browser, and the iPhone Home
Screen app hands to Safari. Lot cards carry a task-store link and no copy button, so
they are unchanged.

Review of https://github.com/analyticendeavors/omnigent-ae/pull/58 (2026-09-27, Claude
and Codex): the host opened with `noopener`, so `window.open` returned null even on
success and a tab the browser blocked (iOS Safari may, one MessagePort hop after the
tap) looked opened; nothing fell back. Since core patch P27 the host rejects with
`PopupBlocked`. On any rejection the card's status line shows the URL as selectable
text ("Tap and hold to copy"), without a clipboard write, because iOS refuses one
after the round trip. A small quiet Copy (`copyLinkButton`, `.btn-copy`) sits beside
Open on GitHub on every card that had Copy link, copying inside the tap, so there is
always a working path; on a phone-narrow card it keeps its own width. A merged card's
Dismiss is its own `.actions.pr-dismiss` group at the row's end, so the "frees a slot"
hint stays beside Mark session done.

2026-09-28: Reid asked why the URL printed on a Your move pull request row was not a
link. It is now a link-styled button (`urlLinkButton`: monospace, the accent colour,
underlined on hover, `aria-label` "Open <url> on GitHub") that opens the pull request
the same way Open on GitHub does, blocked-tab fallback included. It is the only bare
pull request URL the Dashboard prints: Open PRs cards show `repo#number`, lot cards
show a task-store link, and the fallback's URL stays selectable text for copying.

### One status row on pull request cards (2026-09-27)

Reid's second note that the cards waste space (with core patch P26 for the chat's
card): an Open PRs card put its chips, the reason Merge is off, the "from" line and
the buttons on separate full-width rows. `statusRow` in `web/src/prs.ts` now puts the
chips (Your move: its CI line), the reason or a merged card's result and notes, and
the `.actions` on one `.pr-status` row that wraps only when the card is too narrow,
the buttons filling the rest of the row from its right end. A merged card reads
"Merged #55 (squashed) b0413d6" without GitHub's "Pull Request successfully merged",
with Mark session done and Dismiss at the row's end. `.card-pr` and `.card-pull` are
tighter (8 px by 12 px padding, 6 px between rows).

### Merge on Your move's pull requests (2026-09-24)

Why: Open PRs lists only pull requests a session tracks through upstream's
per-session registry, and the ones a subagent or another session opened often are
not tracked. On 2026-09-24 Reid, on his iPhone, saw Open PRs at 0 while Your move
listed green, mergeable pull requests with nothing but Copy link.

Each Your move pull request card now carries the Open PRs card's Merge button,
its reason when the gate is shut, the method and Confirm step, and the merged card
(`mergeButton`, `mergeWhy`, `keepConfirm`, `mergedCards` in `web/src/prs.ts`), so
a pull request merges the same way in either section. Neither
`GET /v1/ae/prs/{owner}/{repo}/{number}` nor
`POST /v1/ae/prs/{owner}/{repo}/{number}/merge` asks whether a session tracks the
pull request, so no server change was needed.

The full card (merge gate, head SHA, methods) is read only when Merge is tapped
(`fetchPrCard` in `web/src/api.ts`), not when the card renders. The Your move list
comes from GitHub's list route, whose rows carry no mergeability and no reviews,
so putting the gate in the list, or reading every card on each poll, would cost
about five GitHub calls per pull request per minute: 30 pull requests is some 7,000
an hour, over the token's 5,000 (dual review of
https://github.com/analyticendeavors/omnigent-ae/pull/50, 2026-09-25). A tap costs
one read, and a second tap within the minute (the server's card cache) reuses it.
An open gate goes straight to the confirm step; a shut one shows its reason with
Merge off. A failed read keeps the last good card, says what failed, and leaves
Merge on: tap it to try again, or Open on GitHub to merge there.

A merged card stays in the section it was merged from until Dismiss, and the pull
request stays out of the list and the count for 80 seconds after the merge (the
server's minute-long caches plus one poll), so a cached answer cannot bring it
back as open. While a merge is in flight its card is not redrawn and Confirm
sends one POST. The Mark session done offer (`web/src/reviewOffer.ts`) is shared too,
but a pull request Your move lists has no tracking session (Open PRs would show
it otherwise), so it offers none. Open on GitHub and Copy stay (Copy link alone until 2026-09-27).

## Tags, repo and search (2026-09-25)

Reid's feedback on the lot (https://github.com/analyticendeavors/omnigent-ae/issues/28),
read mostly on an iPhone.

- **Tags.** Every tag a task carries shows, in a row under the card's header that
  wraps; no "+1". Each chip wears task-store's own colour and Tabler icon, the ones
  chosen in the app's tag manager (`src/tags.ts`, the palette copied from the app's
  stylesheet). `parked` is left off: every lot task carries it. A tag with no colour,
  or a store whose tags could not be read, gets the neutral chip. On the lot a chip
  is a button that filters by its tag; on Your move it is a label.
- **Repo.** The collapsed card's meta line carries the repository (`owner/name`, from
  the note's `REPO:` line or the first GitHub link on the task), cut with an
  ellipsis on the phone.
- **Search and filters** (`src/lotFilter.ts`). One search field over the title and
  NEXT (every word, any case) and a Filters button opening a sheet of chips under
  it: quadrant, resurfaces (overdue, this week: today through six days on, in the
  lot's timezone), tag and repo. OR inside a group, AND across groups. "Showing n
  of m" and Clear appear while a filter is on. The filter is remembered under the
  `storage.user` key `lot.filter`; the host keeps that in the browser's IndexedDB,
  so each device keeps its own. The bar survives the 20 s refresh with its text and
  focus.

## Lot cards and Resume (2026-09-24)

Redesigned after the first box deploy (Reid's feedback: Resume sat awkwardly on
each card, the cards left the right half empty, Reference sat alone in the lower
right, and Resume landed on an empty session). A card is compact by default: one
header row with the quadrant icon, the title, a meta line (resurface date, up to
two tags) and Resume as the right-aligned action. The row toggles the details:
NEXT, quadrant, EST, every tag, the task-store link. (2026-09-25: the meta line
carries the repository instead of tags, and every tag shows in a wrapping row under
the header; see "Tags, repo and search".) Open cards are remembered per
task under the `storage.user` key `lot.expanded` (in memory when storage fails).

Resume calls `POST /v1/ae/lot/{id}/session` (`ae_omni_policies/router.py`), which
does what the web composer does: the Claude Code agent (`claude-native-ui`) on
the one online host, in the host's home directory, labelled `ae.task=<id>`,
then the fenced block as the first user message. The page opens that session. If
the server cannot start one (no host online, two hosts and no `AE_RESUME_HOST`,
no agent), the card keeps the block with the reason and Copy / New session /
Dismiss until dismissed or a later Resume works; if the session started but the
block did not post, the card offers "Open its session". The SDK gives a host
call ten seconds; a create that waits longer on the host is found by its
`ae.task` label instead.

Refreshes on focus and every 20 seconds. Each section renders its own empty or
error state: without the Cloudflare Access token the lot shows the server's 503
text and everything else keeps working. No emoji anywhere; icons are lucide
(working CirclePlay, blocked CirclePause, review CircleCheck, parked CircleParking,
reference Archive; hotfix Flame, feature Sparkles, admin ClipboardList; capacity
Gauge). Single column at 375 to 430 px with 44 px targets; two columns from 720 px. Buttons
(`.btn`) are 32 px tall with a mouse and 40 px on a touch screen (2026-09-26: Reid
found Merge, Copy link, Open / Park / Review / Blocked and Resume too tall at desktop
width).

## Layout

```
pyproject.toml                    distribution omnigent-ae-extension, the entry point, ruff and pytest config
src/ae_omni_extension/plugin.py   get_manifest(): the declarative manifest
src/ae_omni_extension/dist/       the built bundle, committed (extension.js, extension.css)
web/                              the page's source (vite, TypeScript, the vanilla lucide package)
tests/test_manifest.py            check_extension_package over the manifest and the committed dist
web/test/                         vitest in jsdom: the lot, Resume, Your move, the Merge flow in both sections and the Admin Center (`pnpm run test`)
```

## Building

`just ext-build` runs `pnpm install --frozen-lockfile`, the type-check, the vitest
suite and `pnpm run build` in `web/`
and then fails if the committed `dist/` differs from the fresh build, so the bundle
in git is always the one the source produces. The SDK is a private workspace package
upstream; `web/vite.config.ts` aliases it to
`upstream/sdks/web-extension/src/index.ts`, the same way upstream's hello-page
example does. `just ext-doctor` runs `omni extensions doctor
analyticendeavors.parking-lot` in upstream's venv.

Dev loop: `just dev` exports `OMNIGENT_EXTENSION_DEV_BUNDLES` pointing at
`src/ae_omni_extension`, so a rebuild is picked up on the next server start
without reinstalling the package.

## Versioning

`pyproject.toml` `project.version`, `__version__` in `__init__.py`, and
`web/package.json` move together; upstream's conformance check compares the first
two with the manifest. `requires_omnigent` in `plugin.py` and the `omnigent` pin in
`pyproject.toml` move with the submodule (`docs/UPSTREAM-SYNC.md`, step 4).
