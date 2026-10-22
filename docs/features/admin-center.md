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

A strip of tiles above the cards gives one line per source: Release, Deploy hold, Box host, Fabric, Databricks, Secrets and Accounts, each colored by health. Amber or red tells you which card to read first.

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

The host card says whether the box's host is connected, and when it is not, how far the sign-in relink helper has got and lets you request a new link. Server secrets lists every key the server expects by what it turns on, never by value, and flags a missing required one. The relink status comes from `deploy/host-relink.sh`; without it the card shows only the connection.

**Where:** The Box health card.<br>
**Built from:** [`packages/ae_omni_extension`](../../packages/ae_omni_extension); [`packages/ae_omni_policies`](../../packages/ae_omni_policies)

## Every account's limits

<p align="center"><img src="../media/admin-accounts.webp" alt="Every account's limits"></p>

The Accounts card lists every signed-in Claude and Codex account, shared ones included, with its 5-hour and weekly use, reset times and live sessions, fullest first. Readings come from the box host's limits relay (`AE_LIMITS_*` in the host's environment); an account it has not read yet says no reading yet.

**Where:** The Accounts card; Manage in Settings on its tile opens Settings > AI accounts.<br>
**Built from:** [`packages/ae_omni_extension`](../../packages/ae_omni_extension); [`packages/ae_omni_policies`](../../packages/ae_omni_policies)

## Compute and spend

<p align="center"><img src="../media/admin-spend.webp" alt="Compute and spend"></p>

Compute lists Fabric capacities, with Start, Pause, keep awake and resize, and Databricks SQL warehouses with Start and Stop. Azure spend shows the month so far, its pace against last month, cost by subscription and service, and a flag on any day well above its average. Databricks credit shows spend against a credit. Each card is off until configured and says how to add it: Fabric needs `AE_FABRIC_WAKE_URL_<NAME>` (with `AE_FABRIC_SUSPEND_KEY_<NAME>` for Pause, and `AE_FABRIC_CAPACITIES_URL` and `AE_FABRIC_CAPACITIES_KEY` to list every capacity and resize), Databricks needs `AE_DATABRICKS_HOST`, `AE_DATABRICKS_CLIENT_ID` and `AE_DATABRICKS_CLIENT_SECRET`, and its credit card reads `AE_DATABRICKS_CREDIT_USD`, `AE_DATABRICKS_CREDIT_CADENCE` (`monthly` or `once`), `AE_DATABRICKS_CREDIT_START` and `AE_DATABRICKS_CREDIT_END` (without them it assumes a one-off USD 200 credit from 2026-08-19 to 2027-08-19). Azure needs `AE_AZURE_COST_SUBSCRIPTIONS`, `AE_AZURE_COST_TENANT_ID`, `AE_AZURE_COST_CLIENT_ID` and `AE_AZURE_COST_CLIENT_SECRET`.

Azure spend is for admins only, and its Open Cost Management button opens the portal's cost analysis page through the host.

**Where:** The Compute, Azure spend and Databricks credit cards.<br>
**Built from:** core patch [`0151`](../../patches/0151-ae-patch-Let-an-extension-open-the-Azure-cost-analys.patch); [`packages/ae_omni_extension`](../../packages/ae_omni_extension); [`packages/ae_omni_policies`](../../packages/ae_omni_policies)

## Edit a global policy's parameters

<p align="center"><img src="../media/policies-edit.webp" alt="Edit a global policy's parameters"></p>

Stock Omnigent lets an admin switch a global policy on or off. The fork adds Edit beside the switch: it opens the policy's parameters prefilled, such as the slot cap's limit or the rabbit-hole hours, and the server checks the new values by building the policy before it saves. A slot cap limit set here also sets every slot count in the UI, in place of `AE_SLOT_LIMIT`.

**Where:** Settings > Global Policies: the pencil on a policy with parameters.<br>
**Built from:** core patches [`0094`](../../patches/0094-ae-patch-Check-a-global-policy-s-factory_params-by-b.patch), [`0095`](../../patches/0095-ae-patch-Edit-a-global-policy-s-parameters-on-the-Gl.patch); [`packages/ae_omni_policies`](../../packages/ae_omni_policies)

---

Previous: [Usage and accounts](usage-and-accounts.md) &middot; Next: [Phone](phone.md)
