# Admin Center

<sub>Accounts and admin &middot; [All features](README.md)</sub>

Stock Omnigent manages the server on its Settings pages: harnesses, AI accounts, members and global policies. The fork adds an Admin Center, a page of its own extension built on Omnigent's extension API, so the core app is unchanged. It puts the release, deploys, the host, every account's limits and cloud spend on one board, and Settings > Global Policies gains an Edit button.

## One board for the server

<p align="center"><img src="../media/admin-board.webp" alt="One board for the server"></p>

The Admin Center opens on a board of cards: Deploys, Box health, Accounts, Compute, Azure spend and Databricks credit. Each card reads its own source and draws its own empty or error state, so one missing integration never blanks the page. On a phone each card folds to one line that still shows its state.

**Where:** Admin Center in the sidebar, between Inbox and Session Dashboard.<br>
**Built from:** [`packages/ae_omni_extension`](../../packages/ae_omni_extension); [`packages/ae_omni_policies`](../../packages/ae_omni_policies)

## Status tiles

<p align="center"><img src="../media/admin-overview.jpg" alt="Status tiles"></p>

A strip of tiles above the cards gives one line per source: Release, Deploy hold, Box host, Fabric, Databricks, Secrets on box, Key Vault and Accounts, each colored by health. Amber or red tells you which card to read first.

**Where:** The top of the Admin Center.<br>
**Built from:** [`packages/ae_omni_extension`](../../packages/ae_omni_extension)

## Drag cards to rearrange

<p align="center"><img src="../media/admin-drag.webp" alt="Drag cards to rearrange"></p>

Each card header has a grip. Drag a card to another column or position, or focus the grip and use the arrow keys; the layout is saved per column count, and Reset layout brings the defaults back.

**Where:** The grip at the right of any card header, on two or three columns.<br>
**Built from:** [`packages/ae_omni_extension`](../../packages/ae_omni_extension)

## Hold deploys

<p align="center"><img src="../media/admin-deploy-hold.webp" alt="Hold deploys"></p>

A deploy restarts the server and ends every running turn. Hold 1 h, 2 h or 3 h pauses the box's automatic deploys; a ring counts down, the card says when the hold ends and who set it, and the hold ends on its own or with Resume. The card also shows the running release and any newer one queued, and how many busy sessions it waits on. The hold is admin only and needs no configuration (the server keeps it in its data volume, and `deploy/auto-update.sh` reads it); the newest release needs `AE_GITHUB_TOKEN`, and without it the card says the newest release is unknown.

**Where:** The Deploys card.<br>
**Built from:** [`packages/ae_omni_extension`](../../packages/ae_omni_extension); [`packages/ae_omni_policies`](../../packages/ae_omni_policies)

## Box health

<p align="center"><img src="../media/admin-host.webp" alt="Box health"></p>

The host card says whether the box's host is connected, and when it is not, how far the sign-in relink helper has got and lets an admin request a new link (the relink ends every live session, so it is admin only, and a non-admin never sees the button). Server secrets lists every key the server expects by what it turns on, never by value, and flags a missing required one. Each key is a row with its Key Vault name, when the vault says it expires (amber inside 60 days, red once past, "no expiry set" otherwise), when it last changed, and an Edit link (the Azure portal page) and a Rotate link (the system that issues the value, with the account it opens in a hint). The dates come from a daily timer on the box, because the server never reads the vault; the card says when that reading is missing or over 36 hours old (`deploy/README.md`, Secret expiry dates). The sign-in and task-store secrets count as required only when OIDC sign-in or the task store is set up. The relink status comes from `deploy/host-relink.sh`; without it the card shows only the connection.

Since 2026-10-23 the secrets block is "Secrets on this box" and starts collapsed: the counts, then a chip for each key that needs you (missing, expired, expiring inside 60 days, or refused on the server's last call), or one green line naming the next expiry. Show keys opens the rows, and the page remembers it. The host's Claude token is a row too, tagged host, once the box's daily check has reported it. Each server key also says when the server's own calls last worked with it ("worked 2:07 PM") or were refused ("failed: 401 at 1:50 PM"); that is kept in memory, so it counts from the last server start. A refused required key turns the block red, a refused optional one amber.

**Where:** The Box health card.<br>
**Built from:** [`packages/ae_omni_extension`](../../packages/ae_omni_extension); [`packages/ae_omni_policies`](../../packages/ae_omni_policies)

## Key Vault

Every secret in both Key Vaults (the main vault and the website's `kv-example-web`), by name. A switch picks the vault; four numbers count the expired ones, those expiring inside 60 days, those with no expiry set and those unchanged for 180 days or more; Needs you lists the few worth a look, worst first, including any secret whose two copies were changed apart and so needs rotating in both vaults. Show all opens a search box, filters (On box, Both vaults, No expiry, 180d+) and the secrets grouped by the first word of their name, with groups folded until you open one or search. App secrets tagged to their Entra app, and the server's GitHub token, show the issuer's end date when it comes first ("from Entra", "from GitHub"). Admins only: the names say which services we use. Values never leave Key Vault; the daily check on the box reads names and dates (`deploy/README.md`, Secret expiry dates).

Since 2026-10-23 every secret can have an owner, an OmniGent member picked from the member list: open a row (or a group's "Set owner for all") to set it. Rows and Needs you lines show the owner's initials, the filters count each owner and "No owner", and the counts say how many have none. The server also files a task in the task app, with a 09:00 alarm that rings on the phone, when a secret's expiry is 30 days away, moves that alarm at 7 days and when it expires, and closes the task once the secret is rotated; Needs you says when an alarm is set and the tile says when the next one rings (`deploy/README.md`, Expiry alerts).

**Where:** The Key Vault card, under Box health; its tile says both vaults' counts.<br>
**Built from:** [`packages/ae_omni_extension`](../../packages/ae_omni_extension); [`packages/ae_omni_policies`](../../packages/ae_omni_policies)

## Git repos

One card for every repository across several GitHub accounts, so a backlog cleanup reads from one place instead of dozens of `gh` queries. It sorts everything by who moves next. Four numbers come first (ready to merge, alerts to fix, old branches, waiting on GitHub), then Needs you, the few things only you can decide, worst first: an account or repo that cannot be read, a late or failed Dependency Sweep, alerts still vulnerable on the default branch, a failing or conflicting pull request, one ready to merge, alerts GitHub has left open more than a day after the fix landed, and branches with no pull request whose last commit is over two weeks old. Everything else (alerts GitHub has not cleared yet, checks still running, the sweep's own Dependabot pull requests, drafts, recent branches, branches marked as kept, view-only repos) is one line each under Not yours to move. Show all opens every repo, folded, with a search box and chips for PRs, alerts, branches and view-only repos.

Alerts are split by what the default branch really installs: the server reads each alert's lockfile (npm `package-lock.json`, pinned pip `requirements.txt`) and compares the installed version to the vulnerable range. An alert whose lockfile cannot be read counts as needing a fix, so nothing hides. Read only: every row links to GitHub. Repos named view only are shown and left out of every count. Admins only.

The repo list comes from a Dependency Sweep config (`AE_GIT_SWEEP_CONFIG`, `owner/repo:path`) plus `AE_GIT_REPOS` and `AE_GIT_VIEW_ONLY`; `AE_GIT_KEEP_BRANCHES` names branches kept on purpose. Each account reads with its own read-only token, `AE_GIT_TOKEN_<OWNER>`, falling back to `AE_GITHUB_TOKEN`; an account with neither shows as not connected (`deploy/README.md`, The Git repos card's tokens).

**Where:** The Git repos card; its tile says how many things need you and how many alerts need a fix.<br>
**Built from:** [`packages/ae_omni_extension`](../../packages/ae_omni_extension); [`packages/ae_omni_policies`](../../packages/ae_omni_policies)

## Every account's limits

<p align="center"><img src="../media/admin-accounts.webp" alt="Every account's limits"></p>

The Accounts card lists every signed-in Claude and Codex account, shared ones included, with its 5-hour and weekly use, reset times and live sessions, fullest first. Readings come from the box host's limits relay (`AE_LIMITS_*` in the host's environment); an account it has not read yet says no reading yet.

**Where:** The Accounts card; Manage in Settings on its tile opens Settings > AI accounts.<br>
**Built from:** [`packages/ae_omni_extension`](../../packages/ae_omni_extension); [`packages/ae_omni_policies`](../../packages/ae_omni_policies)

## Compute and spend

<p align="center"><img src="../media/admin-spend.webp" alt="Compute and spend"></p>

Compute lists Fabric capacities, with Start, Pause, keep awake and resize, and Databricks SQL warehouses with Start and Stop. Every Compute action changes the bill, so each one, and the Databricks spend refresh, is admin only; anyone signed in can read the cards, and a non-admin sees them without the action buttons. Azure spend uses Azure's own US dollar figures when Azure supplies them, so a subscription billed in another currency still counts in the totals and its targets; without them, such a subscription gets its own line outside the totals. It shows the month so far, its pace against last month, cost by subscription and service, and a flag on any day well above its average. Databricks credit shows spend against a credit when one is set, and only this month's spend otherwise. Each card is off until configured and says how to add it: Fabric needs `AE_FABRIC_WAKE_URL_<NAME>` (with `AE_FABRIC_SUSPEND_KEY_<NAME>` for Pause, and `AE_FABRIC_CAPACITIES_URL` and `AE_FABRIC_CAPACITIES_KEY` to list every capacity and resize), Databricks needs `AE_DATABRICKS_HOST`, `AE_DATABRICKS_CLIENT_ID` and `AE_DATABRICKS_CLIENT_SECRET`, and its credit card reads `AE_DATABRICKS_CREDIT_USD`, `AE_DATABRICKS_CREDIT_CADENCE` (`monthly` or `once`), `AE_DATABRICKS_CREDIT_START` and `AE_DATABRICKS_CREDIT_END` (the credit shows only when the amount and both dates are set; there is no built-in credit, and a value that does not read leaves it off). Azure needs `AE_AZURE_COST_SUBSCRIPTIONS`, `AE_AZURE_COST_TENANT_ID`, `AE_AZURE_COST_CLIENT_ID` and `AE_AZURE_COST_CLIENT_SECRET`.

Azure spend is for admins only, and its Open Cost Management button opens the portal's cost analysis page through the host.

The Azure card's Targets section, folded at first to one line of counts, holds cost targets an admin can add, edit, switch off or delete: a subscription's month-to-date spend, one resource's spend on the latest finished day, or a Fabric capacity's running hours. A target that is crossed files a task with a phone alarm, and the task closes itself when the value falls back (`AE_COST_TARGETS_FILE`, `AE_COST_ALERT_LIST`).

**Where:** The Compute, Azure spend and Databricks credit cards.<br>
**Built from:** core patch [`0151`](../../patches/0151-ae-patch-Let-an-extension-open-the-Azure-cost-analys.patch); [`packages/ae_omni_extension`](../../packages/ae_omni_extension); [`packages/ae_omni_policies`](../../packages/ae_omni_policies)

## Presets card

One card for how roles presets are used over 7 or 30 days: plain-word takeaways and headline numbers first (including raises, step-downs and runs that hung), then a row per preset with its runs and median time and spend, each with a bar of its spend by model, and a pop-up of a preset's recent runs. Spend shows as cost, tokens or both, as Preset insights' "Presets card shows" sets it; tokens are input and output, summed over the lead and its helpers, without cache reads. It reads the run log that Preset insights turns on (see [Roles presets](roles-presets.md)). When the catalog has a newer model than a preset row uses, a note on the card names the model and how many presets use an older one, points to Settings > Roles, and can be dismissed until the next new model; it never changes a preset. Admins only.

**Where:** The Presets card in the Admin Center.<br>
**Built from:** [`packages/ae_omni_extension`](../../packages/ae_omni_extension); [`packages/ae_omni_policies`](../../packages/ae_omni_policies)

## Edit a global policy's parameters

<p align="center"><img src="../media/policies-edit.webp" alt="Edit a global policy's parameters"></p>

Stock Omnigent lets an admin switch a global policy on or off. The fork adds Edit beside the switch: it opens the policy's parameters prefilled, such as the slot cap's limit or the rabbit-hole hours, and the server checks the new values by building the policy before it saves. A slot cap limit set here also sets every slot count in the UI, in place of `AE_SLOT_LIMIT`.

**Where:** Settings > Global Policies: the pencil on a policy with parameters.<br>
**Built from:** core patches [`0094`](../../patches/0094-ae-patch-Check-a-global-policy-s-factory_params-by-b.patch), [`0095`](../../patches/0095-ae-patch-Edit-a-global-policy-s-parameters-on-the-Gl.patch); [`packages/ae_omni_policies`](../../packages/ae_omni_policies)

## Open tabs hear about an update

<p align="center"><img src="../media/reload-notice.jpg" alt="Open tabs hear about an update"></p>

Stock Omnigent keeps running the page a tab loaded, so a fix deployed to the server stays invisible until someone reloads. The fork checks the server's start page every two minutes and whenever the window comes back into view. When the server runs a newer build, a toast says Omnigent was updated, with Reload and Later. Later keeps that tab on the page it has until the next update.

**Where:** Any open tab, after the server is redeployed.<br>
**Built from:** core patches [`0214`](../../patches/0214-ae-patch-Tell-an-open-tab-that-the-server-was-update.patch), [`0215`](../../patches/0215-ae-patch-Spell-Omnigent-the-usual-way-in-the-Reload-.patch)

---

Previous: [Usage and accounts](usage-and-accounts.md) &middot; Next: [Phone](phone.md)
