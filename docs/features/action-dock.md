# Action dock

<sub>Inside a session &middot; [All features](README.md)</sub>

Stock Omnigent shows each question and pull request where it landed in the chat, so what waits on you scrolls away as the session goes on. The fork gathers it into the action dock: one strip inside the composer's workspace bar that leads with the most urgent item and opens to a list of everything waiting in this chat.

## One strip in the composer bar

<p align="center"><img src="../media/dock-overview.webp" alt="One strip in the composer bar"></p>

Collapsed, the dock is one line after the folder and pull request chips: the most urgent item with its button, then a tinted badge for each other kind (a question, a usage limit, pull requests to merge, drafts, pull requests not ready). Nothing waiting draws nothing.

**Where:** The composer's workspace bar in any chat.<br>
**Built from:** core patches [`0137`](../../patches/0137-ae-patch-Add-an-action-dock-above-the-composer-for-q.patch), [`0141`](../../patches/0141-ae-patch-Put-the-action-dock-inside-the-composer-s-w.patch); [`packages/ae_omni_policies`](../../packages/ae_omni_policies)

## The opened list

<p align="center"><img src="../media/dock-open.jpg" alt="The opened list"></p>

The caret at the end of the strip opens every waiting item as a tray above the bar: questions first, then a usage limit, pull requests ready to merge, drafts and the rest, oldest first within each kind. A badge opens the list at its kind, and Escape closes it. While the tray is open, a chat scrolled to the bottom lifts above it, so the last lines stay in view.

**Where:** The caret at the end of the strip, or any badge.<br>
**Built from:** core patches [`0137`](../../patches/0137-ae-patch-Add-an-action-dock-above-the-composer-for-q.patch), [`0141`](../../patches/0141-ae-patch-Put-the-action-dock-inside-the-composer-s-w.patch), [`0159`](../../patches/0159-ae-patch-Lift-the-chat-above-the-open-action-dock-tr.patch)

## A waiting question

<p align="center"><img src="../media/dock-question.webp" alt="A waiting question"></p>

When an agent asks a question and you have scrolled away from its card, the question leads the strip with Jump to it, which scrolls the chat to the card. While the card is on screen the line steps aside, so the question never shows twice.

**Where:** The strip, whenever an agent's question is off screen.<br>
**Built from:** core patches [`0137`](../../patches/0137-ae-patch-Add-an-action-dock-above-the-composer-for-q.patch), [`0144`](../../patches/0144-ae-patch-Keep-merges-and-deploys-in-the-action-dock-.patch)

## Usage gate

<p align="center"><img src="../media/dock-usage-gate.webp" alt="Usage gate"></p>

When the account a chat runs on reaches 90% of its 5-hour or weekly limit, the dock says so ("Backup is at 93% of its 5-hour limit, resets 11:58 AM") and offers New chat, plus Switch model where the chat has a model menu and Switch account on a Claude chat that can fork to another login. It never blocks sending. A dismissal holds for that account until the window resets.

**Where:** The strip and the opened list, in a Claude Code or Codex chat near a plan limit.<br>
**Built from:** core patch [`0142`](../../patches/0142-ae-patch-Show-a-usage-limit-item-in-the-action-dock-.patch); [`packages/ae_omni_policies`](../../packages/ae_omni_policies)

## Every open pull request with its state

<p align="center"><img src="../media/dock-pr-states.webp" alt="Every open pull request with its state"></p>

Each open pull request the session tracks has a line with the same status word as its receipt in the chat: checks running with the passed count, a failed check, behind main, conflicts or changes requested. Lines stay in the dock wherever the chat is scrolled, even while the pull request's own receipt is on screen, so the dock always reads as what is pending now.

**Where:** The opened list (the caret at the end of the strip).<br>
**Built from:** core patches [`0144`](../../patches/0144-ae-patch-Keep-merges-and-deploys-in-the-action-dock-.patch), [`0152`](../../patches/0152-ae-patch-Show-every-open-pull-request-in-the-action-.patch); [`packages/ae_omni_policies`](../../packages/ae_omni_policies)

## Update branch

<p align="center"><img src="../media/dock-update-branch.webp" alt="Update branch"></p>

A pull request that is behind its base gets Update branch on its line, which merges the base into the branch without leaving the chat. The dock and the receipt share the request, so both read Updating until GitHub reports the new head and the checks start over.

**Where:** A behind line in the opened list.<br>
**Built from:** core patch [`0152`](../../patches/0152-ae-patch-Show-every-open-pull-request-in-the-action-.patch); [`packages/ae_omni_policies`](../../packages/ae_omni_policies)

## Drafts with Mark ready

<p align="center"><img src="../media/dock-mark-ready.webp" alt="Drafts with Mark ready"></p>

An open draft gets a line with its checks and Mark ready, which marks it ready for review without merging it. Once GitHub calls it clean, the line turns into a Merge line.

**Where:** A draft line in the strip or the opened list.<br>
**Built from:** core patch [`0149`](../../patches/0149-ae-patch-Show-draft-pull-requests-in-the-action-dock.patch); [`packages/ae_omni_policies`](../../packages/ae_omni_policies)

## Hover preview

<p align="center"><img src="../media/dock-hover-preview.webp" alt="Hover preview"></p>

Resting the pointer on a pull request number in the dock shows a preview: its state, repository, title, author, size and checks. It opens above the bar so it never covers the text box, and it reads data the dock already holds, so it appears with no extra request.

**Where:** Any `#n` in the strip or the opened list.<br>
**Built from:** core patches [`0145`](../../patches/0145-ae-patch-Preview-a-pull-request-on-hover-over-its-nu.patch), [`0147`](../../patches/0147-ae-patch-Open-the-action-dock-s-pull-request-preview.patch)

## Merge from the dock

<p align="center"><img src="../media/dock-merge.webp" alt="Merge from the dock"></p>

A pull request that is ready gets Merge. It asks once, inside the dock: the method the server recommends, preselected with the reason, the other method one click away, and a button that names the method and the base ("Squash into main"). The list grows while the confirm is open so it always shows whole. After the merge the line leaves and the receipt in the chat records it.

**Where:** A ready line in the strip or the opened list.<br>
**Built from:** core patches [`0137`](../../patches/0137-ae-patch-Add-an-action-dock-above-the-composer-for-q.patch), [`0138`](../../patches/0138-ae-patch-Lift-the-action-dock-list-s-height-cap-whil.patch), [`0141`](../../patches/0141-ae-patch-Put-the-action-dock-inside-the-composer-s-w.patch), [`0144`](../../patches/0144-ae-patch-Keep-merges-and-deploys-in-the-action-dock-.patch); [`packages/ae_omni_policies`](../../packages/ae_omni_policies)

## The confirm, shown whole

<p align="center"><img src="../media/dock-merge-confirm.jpg" alt="The confirm, shown whole"></p>

The confirm step sits in the pull request's own line and scrolls into view, with the recommended method, the reason, Cancel and the merge button all visible at once. Escape or Cancel closes it without merging.

**Where:** Merge on any ready line.<br>
**Built from:** core patches [`0138`](../../patches/0138-ae-patch-Lift-the-action-dock-list-s-height-cap-whil.patch), [`0141`](../../patches/0141-ae-patch-Put-the-action-dock-inside-the-composer-s-w.patch)

## Deploy tracking

For the repository the server deploys from, a merge stays in the dock until it runs: the line reads how long ago it merged and what the deploy waits on (busy sessions, or a hold set in the Admin Center), then shows a green "live in" chip with the release for a minute. Pull requests in other repositories end at the merge.

**Where:** The strip after a merge in the repository the server deploys from.<br>
**Built from:** core patches [`0137`](../../patches/0137-ae-patch-Add-an-action-dock-above-the-composer-for-q.patch), [`0144`](../../patches/0144-ae-patch-Keep-merges-and-deploys-in-the-action-dock-.patch); [`packages/ae_omni_policies`](../../packages/ae_omni_policies)

---

Previous: [Composer](composer.md) &middot; Next: [Background agents](background-agents.md)
