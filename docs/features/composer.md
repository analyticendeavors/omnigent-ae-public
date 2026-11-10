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

## Speak replies

A speaker button sits beside the mic in the composer action row, with a small caret right of it that opens its menu. Turn on the Speak replies switch in that menu and the browser reads each reply in that chat aloud as it streams in, starting at the first finished sentence. Code blocks are replaced by "Code block skipped", markdown marks and links are read as plain words, and the TLDR recap at the end of a reply is left out. Below the Voice mode switch (next section), the menu has six setting rows: Speak replies and Fast answers as switches (Fast answers makes the chat answer at low effort while replies are spoken, then restores the old effort), then Voice, Speed, Microphone and Speaker as sub-menus. Each row has its hint icon (the small "i") beside the label and its switch or value at the right edge, and the two actions, Read last reply and Stop speaking, sit side by side at the bottom. Microphone and Speaker list System default first and then every device the browser reports, with long device names shortened; before the mic has been used once the browser lists no devices, and the menu says "Use the mic once to list devices." Microphone sets the input for server dictation and the level meter, and a change made while the mic is listening applies the next time the mic starts. Speaker routes the natural cloud voices; device voices always play on the system default, because the browser gives no control over them. Both are saved per device, a saved device that is unplugged falls back to System default until it returns, and the rows hide where the browser cannot list devices (Speaker also needs output selection). Voice lists natural cloud voices first (Azure Speech, four voices, made by the server) and then this device's voices; if the server cannot make a sentence, the device voice reads it instead. Setting `AE_SPEECH_REGION` and putting the `AE_SPEECH_KEY` secret in the server's secrets file turns the cloud voices on; without them only device voices show. Voice and Speed are remembered per device too. Speech stops when you send a message, switch chat or turn Speak replies off, and a send that has produced nothing after six seconds says "Working on it." once. Device voices run in the browser, and the button is hidden where the browser has no speech support. Off by default; in a narrow split pane the button hides unless it is on. The mic itself works in the desktop app through the server's dictation once the models are installed (see [Dictation](../../deploy/README.md#dictation-the-composer-mic) in the deploy README).

**Where:** The composer action row, left of the mic: the speaker button, and the caret beside it for the menu.<br>
**Built from:** core patches [`0313`](../../patches/0313-ae-patch-Speak-replies-aloud-from-a-composer-toggle-.patch), [`0314`](../../patches/0314-ae-patch-Fix-the-review-findings-on-Speak-replies-03.patch), [`0315`](../../patches/0315-ae-patch-Fix-the-fix-check-findings-on-Speak-replies.patch), [`0316`](../../patches/0316-ae-patch-Speak-replies-menu-with-tooltips-and-natura.patch), [`0317`](../../patches/0317-ae-patch-Fix-the-review-findings-on-the-Speak-replie.patch), [`0325`](../../patches/0325-ae-patch-Add-a-microphone-and-speaker-picker-to-the-.patch), [`0326`](../../patches/0326-ae-patch-Tighten-the-Speak-replies-menu-layout-hint-.patch), [`0327`](../../patches/0327-ae-patch-Fix-the-picker-review-findings-devices-list.patch), [`0328`](../../patches/0328-ae-patch-Show-the-device-model-on-the-picker-rows-an.patch); [`packages/ae_omni_policies`](../../packages/ae_omni_policies) (`tts.py`)

## Voice mode

Voice mode is one switch for hands-free talking. Tap the speaker button to turn it on (it turns Speak replies on with it); tap it again to turn it off (while it is speaking, a tap stops the reply instead; see below). A small caret right of the speaker opens the menu above, which gains a Voice mode switch on top. Turning Speak replies off while Voice mode is on turns Voice mode off too. While it is on and idle the mic listens through the server's dictation: the server marks the end of a sentence after 1.6 seconds of silence, Voice mode waits 0.6 seconds more for a continuation (a new word cancels the wait), then sends the draft the way the Send button does, about 2.2 seconds after you stop. From the send until the reply has been spoken the mic is stopped, so the voice is not dictated back; listening resumes 0.3 seconds after the last sentence ends, or 0.3 seconds after you tap the speaker to stop a reply. That tap is a barge-in: it stops the current sentence and silences the rest of that reply (until your next message), and Voice mode stays on. The speaker shows the phase as a word and a ring: Listening (teal) only while the mic is really listening, Starting the mic, Mic stopped (you stopped it by hand), Mic unavailable (amber; two starts failed, and the next reply cycle tries again). The first start after the server restarts can take about 40 seconds while the speech model loads, and the speaker says Starting the mic until it listens; the other phases are Working on it (amber) and Speaking, tap to stop. The mic stays muted from your send until the turn is over and the voice has finished, however long the reply takes to begin, and sound heard in that time never reaches the draft. When a question card (AskUserQuestion) appears, the card is read aloud as `Question. <text> One, <label>. Two, <label>.` without the option descriptions, and the next sentence you say is matched to an option by number ("two", "the first one", "option 2"), by the label, or by a short run of the label's words; the match is submitted as the card's Submit would. Words that match nothing go in as your own answer after the 0.6 second wait (unless the card says it takes no answer of its own, then Voice mode keeps listening for an option), a multi-select card collects picks until you say "done", and silence never answers a card. A number only counts when the answer is nothing but number words, and a label said after "not", "don't" or "never" is not picked. The switch is remembered per chat in this browser, is compared with the session's `ae.voice` label when the chat opens, and writes the `ae.voice` label on the session (see `docs/LABELS.md`); while the label is `on` the `ae_voice_style` policy adds one line to each message you send: "Spoken reply: lead with the answer, short plain sentences, no tables, code, links or headers unless asked." The server allows two dictation streams at once, so run Voice mode in one chat per window. Headphones keep the speakers from feeding the mic. Off by default.

**Where:** Tap the speaker button in the composer action row, or the Voice mode switch at the top of its menu.<br>
**Built from:** core patches [`0320`](../../patches/0320-ae-patch-Add-Voice-mode-one-switch-for-hands-free-li.patch), [`0321`](../../patches/0321-ae-patch-Fix-the-Voice-mode-review-findings-chat-swi.patch), [`0322`](../../patches/0322-ae-patch-Fix-the-Voice-mode-fix-check-findings-stuck.patch), [`0323`](../../patches/0323-ae-patch-Fix-the-Voice-mode-second-fix-check-finding.patch); [`packages/ae_omni_policies`](../../packages/ae_omni_policies) (`voice.py`)

---

Previous: [Session rail](session-rail.md) &middot; Next: [Action dock](action-dock.md)
