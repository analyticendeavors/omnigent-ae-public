# Composer

<sub>Inside a session &middot; [All features](README.md)</sub>

Stock Omnigent's composer sends your prompt to the session and lets you pick its model and effort. The fork has it guess your next prompt, names every Claude model version, shows the effort a session really runs at, and makes the slash menu and compaction easier to read.

## Prompt suggestions

<p align="center"><img src="../media/composer-suggestions.webp" alt="Prompt suggestions"></p>

When a turn finishes, a short Haiku side call predicts what you will type next, and the composer shows it as grey ghost text over the empty box. Tab fills it in without sending, so you can edit it first; typing anything hides it. It works in Claude sessions on any account and in Codex sessions too. The suggestion stays until your next prompt, so a reload or a second tab still shows it.

**Where:** The composer of any idle session, after a turn ends. The chip on the right reads "Tab to use" ("Tap to use" on a phone).<br>
**Built from:** core patches [`0123`](../../patches/0123-ae-patch-Predict-the-next-prompt-after-each-Claude-t.patch), [`0128`](../../patches/0128-ae-patch-Predict-the-next-prompt-in-every-session-on.patch), [`0183`](../../patches/0183-ae-patch-Keep-the-composer-s-prompt-suggestion-until.patch)

## Ranked alternatives

<p align="center"><img src="../media/composer-alternatives.webp" alt="Ranked alternatives"></p>

The side call offers up to three clearly different guesses, best first. Alt+] and Alt+[ step through them in place, and the counter on the chip (1/3) shows which one you are on. Esc hides them until the next turn.

**Where:** The counter on the suggestion chip, or Alt+] and Alt+[. Settings > Chat lets you rebind the keys.<br>
**Built from:** core patch [`0132`](../../patches/0132-ae-patch-Offer-up-to-three-ranked-prompt-suggestions.patch)

## Model versions and Previous models

<p align="center"><img src="../media/composer-model-versions.webp" alt="Model versions and Previous models"></p>

The host names Claude models by family alone (Opus, Sonnet), so the menu never said which version a pick runs. The fork labels each row with its version (Opus 5.5) and lists the older versions your subscription still serves under Previous models, so you can pin a session to one.

**Where:** The model chip under the composer, then the Claude Code row. The New session dialog shows the same list.<br>
**Built from:** core patch [`0108`](../../patches/0108-ae-patch-Show-Claude-model-versions-and-a-Previous-m.patch)

## A one-column model menu

<p align="center"><img src="../media/composer-model-menu.jpg" alt="A one-column model menu"></p>

The menu is one list, as in the Claude desktop app: the check sits before the current model, each model has a digit you can press to pick it while the menu is open, and Account, Effort, Previous models and Fast mode sit below as rows.

**Where:** The model chip under the composer.<br>
**Built from:** core patches [`0120`](../../patches/0120-ae-patch-Put-the-model-grid-s-check-before-the-name-.patch), [`0134`](../../patches/0134-ae-patch-Show-the-effort-a-Claude-session-runs-at-an.patch)

## The effort a session runs at

<p align="center"><img src="../media/composer-effort.webp" alt="The effort a session runs at"></p>

With no effort picked, stock Omnigent said nothing about the level Claude Code would use. The fork reads it: first the per-model `effortLevel` in Claude Code's settings files, tagged "settings", else Claude Code's own default for that model, tagged "default". The chip under the composer carries the level beside the model, as in "Opus 5.5 High".

**Where:** Effort in the model menu, and the model chip under the composer.<br>
**Built from:** core patches [`0119`](../../patches/0119-ae-patch-Show-the-effort-a-Claude-session-runs-at-wh.patch), [`0124`](../../patches/0124-ae-patch-Read-Claude-Code-s-per-model-effort-levels-.patch), [`0134`](../../patches/0134-ae-patch-Show-the-effort-a-Claude-session-runs-at-an.patch)

## Fast mode

<p align="center"><img src="../media/composer-fast-mode.webp" alt="Fast mode"></p>

A switch for Claude Code's fast mode, which gives faster Opus output billed to usage credits. It is per session, is enabled only on Opus models, and reads the session's real state each time the menu opens, so the switch never shows off while credits are being spent.

**Where:** Fast mode in the model menu of a Claude session. The host needs per-session fast mode in its Claude settings.<br>
**Built from:** core patches [`0044`](../../patches/0044-ae-patch-Add-a-Fast-mode-switch-to-the-model-menu-fo.patch), [`0045`](../../patches/0045-ae-patch-Fix-the-review-findings-on-the-Fast-mode-sw.patch)

## Blurred command and file menus

<p align="center"><img src="../media/composer-menu-blur.webp" alt="Blurred command and file menus"></p>

The slash command menu and the @ file mention menu blur the chat behind them, so their rows stay readable over a busy transcript in the dark theme.

**Where:** Type / or @ in the composer.<br>
**Built from:** core patch [`0153`](../../patches/0153-ae-patch-Blur-what-is-behind-the-slash-command-and-f.patch)

## Token defrag while compacting

<p align="center"><img src="../media/composer-defrag.webp" alt="Token defrag while compacting"></p>

While a conversation compacts, a strip of colored token blocks sorts itself in the composer tray with a progress percentage, and the marker left behind shows the token count before and after. Click the strip for a full defragmenter window. It tells you compaction is still running and roughly how far along it is.

**Where:** The composer tray during /compact or an automatic compaction. Turn it off in Settings > Chat.<br>
**Built from:** core patch [`0097`](../../patches/0097-ae-patch-Play-a-token-defrag-in-the-composer-tray-wh.patch)

## Queued notes survive a reload

A message you send while the chat is busy waits in the queue above the composer until the turn ends. The fork keeps up to 50 of those notes in the tab's session storage (each split-view pane keeps its own), so a reload, a phone discarding the tab, the automatic reload after a failed page chunk, or the reload an update notice asks for no longer loses them; past 50, newer notes stay in memory only and a reload loses them. After the reload each note comes back in its order, ahead of anything queued since. A text note under an hour old, queued under your sign-in in this tab, sends on its own as before. Any other note comes back waiting for Retry, with one toast saying why: one with attachments (attachments are not kept), an older one, one that already needed Retry, every note in a tab copied from another (they may also be waiting in the tab it came from), and every note when the browser cannot tell whether another tab holds them. A note waiting for Retry holds back the notes behind it in that chat until you retry or remove it. Each note remembers who queued it: one queued by someone else signed in on that browser is dropped, and when your sign-in cannot be confirmed within 10 seconds your notes wait, with a toast that says to reload rather than retype them. A note queued before the page knew who was signed in has no owner, so after the reload it comes back, waiting for Retry, to whoever is signed in then. A closed tab that is never reopened loses its notes, and nothing crosses devices.

**Where:** The queued notes above the composer, after a reload.<br>
**Built from:** core patches [`0285`](../../patches/0285-ae-patch-Keep-queued-notes-through-a-page-reload.patch), [`0286`](../../patches/0286-ae-patch-Fix-the-review-findings-on-keeping-queued-n.patch), [`0287`](../../patches/0287-ae-patch-Tell-the-user-when-queued-notes-wait-for-si.patch)

---

Previous: [Session rail](session-rail.md) &middot; Next: [Action dock](action-dock.md)
