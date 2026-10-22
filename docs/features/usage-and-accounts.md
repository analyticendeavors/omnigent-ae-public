# Usage and accounts

<sub>Accounts and admin &middot; [All features](README.md)</sub>

Stock Omnigent draws a context ring under the composer and lets a host sign in to Claude Code with one account. The fork turns the ring into a usage readout for the session and its plan, keeps several Claude logins per host, adds company accounts an admin shares with every user, and lets you choose the account when you fork a session.

## A context ring that changes color

<p align="center"><img src="../media/usage-ring.webp" alt="A context ring that changes color"></p>

The ring under the composer turns amber when the session's context window is sixty percent full and red at eighty, the same cue Claude Code desktop gives. You see which sessions are close to compacting without opening any of them.

**Where:** The context ring at the right of the composer's workspace bar.<br>
**Built from:** core patch [`0018`](../../patches/0018-ae-patch-Color-the-context-ring-near-a-full-window-a.patch)

## The usage popover

<p align="center"><img src="../media/usage-popover.webp" alt="The usage popover"></p>

Click the ring to open the usage popover. A bar shows what fills the context window by category (messages, tools, memory files and the rest), with a color legend under it, and below that the plan's 5-hour and weekly windows with their reset times and the usage credits spent. A Codex session shows the Codex plan instead.

**Where:** Click the context ring under the composer. Show details in session rail opens the full breakdown.<br>
**Built from:** core patches [`0006`](../../patches/0006-ae-patch-Open-a-plan-limits-popover-from-the-compose.patch), [`0009`](../../patches/0009-ae-patch-Show-the-session-s-context-and-every-usage-.patch), [`0040`](../../patches/0040-ae-patch-Shorten-the-Usage-popover-in-Claude-Desktop.patch), [`0064`](../../patches/0064-ae-patch-Label-context-window-colors-directly-beneat.patch), [`0065`](../../patches/0065-ae-patch-Match-context-legend-colors-to-detailed-cat.patch), [`0071`](../../patches/0071-ae-patch-Add-a-session-rail-beside-the-chat-and-slim.patch); [`packages/ae_omni_policies`](../../packages/ae_omni_policies)

## Limits for the session's own account

<p align="center"><img src="../media/usage-popover-account.jpg" alt="Limits for the session's own account"></p>

Each Claude session records which account it runs on, and the popover reads the limits for that account, so a session on a backup login shows the backup's windows and not the default login's. The account's label appears beside the plan name.

**Where:** The usage popover, on the line that names Claude Code, the plan and the account.<br>
**Built from:** core patch [`0078`](../../patches/0078-ae-patch-Record-each-Claude-session-s-account-beside.patch); [`packages/ae_omni_policies`](../../packages/ae_omni_policies)

## Several Claude logins per host

<p align="center"><img src="../media/accounts-settings-page.jpg" alt="Several Claude logins per host"></p>

A host keeps more than one Claude subscription login, each with a label you can rename, and you pick which one new chats use. Add Claude account signs a new login in through the browser, so no token is ever pasted; pasting an existing OAuth token stays available under Advanced.

**Where:** Settings > AI accounts, under Claude subscription account.<br>
**Built from:** core patches [`0046`](../../patches/0046-ae-patch-Add-Claude-backup-accounts-and-repair-chat-.patch), [`0048`](../../patches/0048-ae-patch-Make-Claude-account-setup-readable-and-avai.patch), [`0049`](../../patches/0049-ae-patch-Add-browser-Claude-sign-in-and-provider-set.patch), [`0060`](../../patches/0060-ae-patch-Add-editable-account-labels-and-responsive-.patch)

## Remove a personal login

<p align="center"><img src="../media/accounts-remove.webp" alt="Remove a personal login"></p>

Remove deletes a login and its token from the host after one confirm, which names the host and says running chats keep their token until they restart. The default CLI login cannot be removed, and removing the selected login hands new chats back to the default.

**Where:** Settings > AI accounts: Remove beside a login.<br>
**Built from:** core patch [`0135`](../../patches/0135-ae-patch-Remove-a-personal-Claude-login-from-Setting.patch)

## Shared Claude accounts

<p align="center"><img src="../media/accounts-shared.webp" alt="Shared Claude accounts"></p>

An admin adds company Claude accounts once on the server, and every user's host can select them without copying a token; the host fetches the token at launch. The Shared group lists each account with who added it and when, and an admin can add, rename or remove one. They also appear in every account picker with a Shared tag. The server needs `AE_SHARED_ACCOUNTS_KEY`; without it the group says shared accounts cannot be added or used.

**Where:** Settings > AI accounts, in the Shared group below the host's own logins.<br>
**Built from:** core patches [`0117`](../../patches/0117-ae-patch-List-the-admin-s-shared-Claude-accounts-in-.patch), [`0127`](../../patches/0127-ae-patch-Manage-shared-Claude-accounts-from-Settings.patch); [`packages/ae_omni_policies`](../../packages/ae_omni_policies)

## Pick the account when forking

<p align="center"><img src="../media/accounts-fork-picker.webp" alt="Pick the account when forking"></p>

Omnigent switches a running session to another harness or model by forking it. The fork dialog adds a Claude account row, so the copy can run on another login or a shared account while the original keeps its own. It defaults to Same as this session and names that account.

**Where:** The composer's model menu > Claude Code > Switch harness, then Claude account.<br>
**Built from:** core patch [`0099`](../../patches/0099-ae-patch-Pick-the-Claude-account-when-forking-a-Clau.patch)

---

Previous: [Pull requests](pull-requests.md) &middot; Next: [Admin Center](admin-center.md)
