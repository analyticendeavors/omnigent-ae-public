# Core patch series

The only changes ever made to OmniGent's own code live here, as `git format-patch`
output. `upstream/` is a submodule pointing at the fork branch
`analyticendeavors/omnigent@ae/vX.Y.Z`, which is the upstream release tag plus these
commits, one commit per patch. This directory is the reviewable form of that branch;
CI regenerates one from the other and fails on drift.

Conventions:

- File names come from `git format-patch`: `NNNN-<slug>.patch`, numbered in apply
  order. Never rename or hand-edit a `.patch`; change the commit on the fork branch and
  run `just patches-export`.
- One commit per patch. Commit subjects start with `[ae-patch]` so they stand out in
  the fork's history and in `git log upstream/`.
- Logic goes in new files; a patch touches an upstream file in as few lines as it can
  (the handoff's target is six or fewer for the churn-heavy files), so the series keeps
  applying across weekly upstream releases.
- `upstream.lock` records the pinned tag, the fork branch head and `PATCH_COUNT`;
  `scripts/check-lock.sh` refuses a mismatch.

Planned series (see `docs/omnigent-fork-handoff.md` in agents-and-automation, section 6):

1. `0001` web: `ae.status` icon on session rows and a "Slots n/3" pill.
2. `0002` extensions: a `server.request` capability plus `labels` on session summaries.
3. `0003` server: an item-observers registry so the AE observer can read assistant items.
4. `0004` web: a Status submenu on the sidebar session menu that sets `ae.status`
   (added 2026-09-24, after the first live deploy; not in the handoff's plan).
5. `0005` web: a split view that shows up to four sessions side by side (added
   2026-09-24; not in the handoff's plan).
6. `0006` web: a plan limits popover behind the composer's context ring (added
   2026-09-24; not in the handoff's plan).
7. `0007` web: the sidebar's slots row named Dashboard, and a slots chip in the
   session header (added 2026-09-24 after the dashboard review; not in the
   handoff's plan).
8. `0008` web: the slot count on the installed app's icon (added 2026-09-24; not in
   the handoff's plan).
9. `0009` web: the full usage panel in the plan limits popover (added 2026-09-24; not
   in the handoff's plan).
10. `0010` web: layout presets for the split view (added 2026-09-24; not in the
    handoff's plan).
11. `0011` web: a Run button on chat shell code blocks that pastes the block into the
    session's shell (added 2026-09-25; not in the handoff's plan).
12. `0012` web: a merge card per tracked pull request in the session's GitHub panel
    and under the chat message that names it (added 2026-09-24; not in the handoff's
    plan).
13. `0013` web: fixes to `0011` found in review after it merged (added 2026-09-25).
14. `0014` web: every slot count takes the limit from `GET /v1/ae/capacity` (the
    server's `AE_SLOT_LIMIT`) instead of a constant 3, so the cap is one setting in
    `config/server.env` (added 2026-09-24; AE files only, no upstream file touched).
15. `0015` web: project icons from Tabler Icons (tabler.io/icons) instead of emojis,
    stored as `tabler:<name>` (added 2026-09-24; not in the handoff's plan).
16. `0016` web: a Paste button on the web terminal for touch devices (added
    2026-09-24; not in the handoff's plan).
17. `0017` web: fixes to `0016` found in review (added 2026-09-24).
18. `0018` web: the context ring colored as the window fills, and a caption and
    colors on the popover's prompt cache bar (added 2026-09-24; not in the
    handoff's plan).
19. `0019` web: the Inbox card title truncates instead of overlapping on a phone
    (added 2026-09-25; a fix to upstream's own page, not in the handoff's plan).
20. `0020` web: a PR card offers Review after a merge only when no other tracked
    pull request of the session is still open (added 2026-09-25; a fix to `0012`).
21. `0021` web: the merge card restyled for phones, with the merge method as a
    segmented control (added 2026-09-25; a restyle of `0012`, no upstream file
    touched).
22. `0022` web: the merge confirm step on one row, and the review offer relabelled
    "Mark session done" (added 2026-09-26).
23. `0023` web: fixes to `0015`, `0021` and `0022` found in a post-merge review
    (added 2026-09-27).
24. `0024` web: fixes to `0023`'s icon loader from the review of its pull request
    (added 2026-09-27).
25. `0025` web: `navigation.openExternal` also opens a github.com pull request or
    issue URL, so the Dashboard's cards open GitHub instead of copying the link
    (added 2026-09-27; not in the handoff's plan).
26. `0026` web: a pull request card's chips, outcome and action on one wrapping row,
    without GitHub's merged echo (added 2026-09-27; a restyle of `0012`, no upstream
    file touched).
27. `0027` web: `navigation.openExternal` rejects with `PopupBlocked` when the browser
    blocked the new tab (added 2026-09-27; a fix to `0025` from the review of its pull
    request).
28. `0028` web: the merge confirm step opens on the method the server recommends and
    tags it, with the reason under the question (added 2026-09-28; a change to `0012`'s
    own files, no upstream file touched).

The series was empty through Phase 1; `0001` landed with Phase 2, `0002` with
Phase 3 and `0003` with Phase 4 (all 2026-09-23). Adding a patch: commit on `upstream/` (detached from the
tag, or on the fork branch), `just patches-export`, commit the new `.patch` and the
updated `upstream.lock` and submodule pointer together, and push the fork branch.
When another PR merges a patch first, `just patches-rebase` moves this branch's
commits onto main's series and renumbers its patches in the docs (2026-10-20).

`0002` touches upstream's extension seam in one line per file where it can
(`omnigent/extensions/api.py`, `web/src/extensions/services/registry.ts`) and keeps
the allowlist in a new file, `web/src/extensions/services/serverRequest.ts`; the
Parking Lot page (`packages/ae_omni_extension`) is the only caller. Two tests
upstream owns had to change with it: the exact-key assertion on the session
projection and the permission enumeration in `tests/extensions/test_registry.py`.

`0003` is one new file (`omnigent/server/item_observers.py`: `register_item_observer`,
`notify_items_persisted`, fire-and-forget with logged exceptions) and two lines in each
of its call sites, the import and the call: `orchestration.py` after the append in
`_persist_external_conversation_item` (native sessions) and `helpers.py` after the
assistant segment append in `_flush_relay_text` (relay harnesses). Its tests are
`tests/server/test_item_observers.py`, which `just upstream-smoke` runs. With no
observer registered the server behaves exactly as before, so the patch carries no
behaviour of its own; `packages/ae_omni_policies/src/ae_omni_policies/observer.py` is
the only registrant.

`git am` in `scripts/apply-patches.sh` drops the `[ae-patch]` subject prefix when it
re-applies a patch onto the pristine tag (it strips every leading bracket unless told
to keep them); CI compares trees, not commit ids, so this is cosmetic until a sync
rebuilds the fork branch from the series.

`0004` (2026-09-24) is one new file, `web/src/shell/AeStatusMenu.tsx`, and eight lines
in `web/src/shell/Sidebar.tsx`: the import and the owner-gated `<AeStatusMenu>` in
`ConversationMenuItems`, above the Stop / Archive / Delete group, so the kebab and the
right-click menu both carry it. The extension SDK offers only page and primary
navigation contributions, so a menu item cannot come from the Parking Lot extension.
Its tests are `web/src/shell/AeStatusMenu.test.tsx` and four cases added to `0001`'s
`Sidebar.aeStatus.test.tsx`; `just web-check` runs both.

`0005` (2026-09-24) is the split view: new files `web/src/lib/aeSplitLayout.ts` (the
layout, its URL and storage round trip), `web/src/pages/AeSplitPage.tsx` (the page)
and `web/src/shell/AeSplitMenuItem.tsx` (the menu item), and ten lines in three
upstream files: `App.tsx` (the import and a `/split` route outside `AppShell`, so the
page has no sidebar of its own), `Sidebar.tsx` (the import and the item after P4's
Status submenu) and `AppShell.tsx` (the import and one condition in
`initialSidebarOpen`, so a pane frame starts with its sidebar closed). Its tests are
`aeSplitLayout.test.ts`, `AeSplitPage.test.tsx` and three cases added to
`Sidebar.aeStatus.test.tsx`; `just web-check` runs all three. Upstream is building its
own split view (https://github.com/omnigent-ai/omnigent/pull/6758, a draft on
2026-09-24); when a tag ships it, drop this patch in that sync.

`0006` (2026-09-24) is one new file, `web/src/components/AePlanLimits.tsx` (with its
vitest suite, run by `just web-check`), and two lines in `web/src/pages/ChatPage.tsx`:
the import and `<AePlanLimitsPopover />` beside the composer's context ring. Through
v0.13.0 it was six lines wrapping the ring; v0.15.0 moved the ring into the workspace
bar as `ComposerContextRing`, its own button in a row whose layout upstream's tests
pin, so since the v0.15.0 sync (2026-09-24) the popover renders no element there: it
listens for clicks on `[data-testid="composer-context-ring"]` and anchors to the ring
clicked. Its suite renders upstream's real ring, so a renamed test id fails there. The popover reads `GET /v1/ae/limits`, which the
AE layer serves; the data path is out of tree (`docs/PLAN-LIMITS.md`). It is a patch
because the composer's status line has no extension contribution point. It keeps its
fetch in component state rather than react-query because upstream's ChatPage suites
render the composer without a QueryClient.

`0007` (2026-09-24) is one new file, `web/src/components/AeSessionChip.tsx` (with its
vitest suite), edits to P1's own files (`AeSlotsPill.tsx` becomes the "Dashboard n/3"
row, active on the Dashboard's routes; `aeLabels.ts` gains `AE_DASHBOARD_PREFIX`; P1's
two suites follow the new name), and two lines in `web/src/shell/ChatHeader.tsx`: the
import and `<AeSessionChip>` at the head of the header's right-hand cluster. The chip
reads the session's row and the slot count from the `["conversations"]` cache, like
the row. Why a patch: the extension SDK has no header slot and a nav contribution's
label is static, so neither the count on the nav row nor the chip can come from the
extension (`docs/design/dashboard-review.md`, section 5). P1's four-line icon edit to
`ExtensionPrimaryNavigation.tsx` (`circle-parking`, `gauge`) has no user left now that
the extension contributes no nav row; it was kept rather than rewrite P1 on a
published fork branch, and can be dropped at the next sync, which rebuilds the
branch anyway.

`0008` (2026-09-24) touches no upstream file: a new `web/src/lib/aeAppBadge.ts`
(`syncAeAppBadge`, feature-detected, never throws) and one effect in P1's
`AeSlotsPill.tsx` that calls it whenever the count changes, so a Home Screen install
shows the working-session count on its icon and clears it at zero. The sidebar is
mounted on every page, open or not, which is why the row carries it. On iOS the
Badging API needs notification permission; upstream's idle notifications already
request it on the first tap, and without it the call is refused and ignored.

`0009` (2026-09-24, second pass on the plan limits) touches only the two files `0006`
added, `web/src/components/AePlanLimits.tsx` and its suite: the context window row
(read from the chat store the ring reads, so `ChatPage.tsx` keeps its two lines), the
per-model weekly rows, usage credits, the plan beside the harness name, a one-line
hint, and a close on window blur for the split view's iframes. It is its own commit
rather than an amended `0006` because `ae/v0.15.0` is never force-pushed. It was
written in parallel with `0007` and `0008` and rebased onto them when they merged
first; the files do not overlap.

`0010` (2026-09-24) is layout presets for `0005`'s split view, and touches no upstream
file: it changes only `aeSplitLayout.ts`, `AeSplitPage.tsx`, `AeSplitMenuItem.tsx`
and their two suites, all created by `0005`. It is a patch of its own, not an amended
`0005`, so the commits after `0005` (`0006` to `0009`) keep their ids. When upstream
ships a split view and `0005` is dropped, this patch goes with it.

`0011` (2026-09-25) is Run on chat shell code blocks. Reid copied command blocks by
hand into a shell, often on the phone; now an assistant block tagged bash, sh, shell,
zsh, console or powershell (or an untagged block that reads as commands) carries a
Run button for the owner of a session whose agent declares shells. The logic is four
new files: `web/src/lib/aeRunCommand.ts` (which blocks, the exact text, the heredoc,
quote and continuation guard that withholds Enter, the sudo / rm -rf / curl | sh / dd
list, and hidden control and bidi characters stripped so the sheet shows exactly what
is sent), `web/src/lib/aeTerminalInput.ts` (a registry of writable terminal attaches and
the published run target), `web/src/shell/useAeRunInTerminal.ts` (pick or create the
shell, open it, wait for its socket, paste) and `web/src/components/AeRunCodeButton.tsx`
(the button and the confirm sheet). Upstream files change by a few lines each:
`TerminalSession.ts` gains `aeInputState` and `aePaste` (xterm's own `paste`, then
an Enter through `term.input`, so the bytes take the keyboard's `onData` path and
WebSocket), `TerminalView.tsx` registers writable attaches (three lines and the
import), `message.tsx` renders the button in `ChatCodeBlockPre` (the import and one
line), and `AppShell.tsx` calls the hook (the import and one call). No server route
is added: a missing shell is created through the existing `POST
.../resources/terminals`, the same call as "+ New shell". Its suites are
`aeRunCommand.test.ts`, `aeTerminalInput.test.ts` and `AeRunCodeButton.test.tsx`,
the last a page test through the real `TerminalView` and `TerminalSession` with only
the WebSocket faked; `just web-check` runs all three.

`0012` (2026-09-24) is three new files, `web/src/lib/aePrs.ts` (the card's types, the
reads and the merge over `/v1/ae/prs`), `web/src/components/AePrCards.tsx` and
`web/src/components/AeInlinePrCards.tsx` (each with its vitest suite), and two lines
in each of two upstream files: `web/src/shell/GithubPanel.tsx` (`<AePrCards>` between
the PR picker and the details) and `web/src/components/blocks/BlockRenderer.tsx`
(`<AeInlinePrCards>` under a finished assistant text block; added 2026-09-25 at
Reid's request, a compact card under the message that names a tracked PR). It builds on upstream's
per-session pull request tracking (https://github.com/omnigent-ai/omnigent/pull/6935)
rather than beside it: the cards are the panel's own `prs` list, and only CI counts,
mergeability, reviews and the Merge button come from the AE routes
(`packages/ae_omni_policies/src/ae_omni_policies/prs.py`). Why a patch: the GitHub
panel is upstream's and the extension SDK has no slot in a session page. If upstream
adds a merge action to the panel, this patch shrinks to the review offer or goes. It
was written in parallel with `0011` and rebased onto it when `0011` merged first; the
files do not overlap.
`0013` (2026-09-25) fixes six bugs in `0011`, found in review after it merged. A Run
pasted on the WebSocket's open, before xterm had parsed the shell's request for
bracketed paste, so each newline went as Enter (a block held back for an open heredoc
still ran its earlier lines; a password prompt could read the next line): a sink is
now ready only once the shell has drawn and gone quiet, and a multi-line block is never
pasted unbracketed. A powershell block went to bash; blocks now run only in a shell
of their kind, read from the terminal's spec name. A `$ ` line in a heredoc body made
a bash block a transcript, a prompted heredoc lost its body, and a console block
without prompts sent its output as commands. Run showed on a block still streaming
(read from Streamdown's own context, no upstream change). The terminal now gets the
focus when the sheet closes, and the sheet scrolls inside 85vh on a phone. The only
upstream file it touches is `TerminalSession.ts`, whose two Run methods become four
small hooks (`aeOnOutput`, `aeBracketedPaste`, `aeWrite`, with `aeInputState` kept).
It was rebased onto `0012`, which merged first; the files do not overlap.

`0015` (2026-09-24) swaps the project icon picker from emoji-mart to Tabler Icons,
because Reid wanted clean line icons for projects instead of emojis (he met the emoji
picker in the New project dialog on his iPhone). A pick is stored as `tabler:<name>`
(for example `tabler:rocket`) in the project's `config.icon`, which the server already
keeps as a free string (`SessionProjectSummary.icon`; nothing validates it), so no
server change. An emoji saved earlier still renders as text, no icon is still the
folder, and a name the set does not know falls back to the folder. The logic is three
new files: `web/src/lib/aeTablerIconData.ts` (two lazy loaders),
`web/src/lib/aeTablerIcon.tsx` (the value format, `AeTablerRowIcon` for
`ProjectRowIcon`, `AeProjectIconGlyph` for the icon tiles) and
`web/src/components/AeTablerIconPicker.tsx` (search over names, categories and tags;
an 8-column grid; arrow keys, Enter, Space, and Up from the top row back to the search
box; its height comes from the `--emoji-picker-height` the callers' popovers already
publish, so it fits a 390 px phone). Upstream files change by a few lines each:
`ProjectIconPicker.tsx` (`EmojiPicker` renders the Tabler picker, keeping its name and
`onSelect` contract so its callers need no change; the landing tile draws the glyph;
the landing popover gets the default card chrome the emoji-mart card used to supply),
`ProjectPicker.tsx` (one branch in `ProjectRowIcon`), `NewProjectButton.tsx` and
`Sidebar.tsx` (the glyph inside the icon tiles and the project folder header; the
import and one line each), `ProjectIconPicker.test.tsx` (the stubbed picker), and
`web/package.json` with `pnpm-lock.yaml` (`@tabler/icons` 3.46.0, the newest version
past the workspace's seven-day cooldown on 2026-09-24).

Lazy rather than curated: the whole outline set (5,130 icons) is one chunk of 1.2 MB
(231 KB gzipped), fetched the first time a `tabler:` icon renders, with a same-size
empty box until it arrives; the tags (1.1 MB, 183 KB gzipped) load only when the picker
opens. The main entry grew by 6.6 KB (2.2 KB gzipped) and emoji-mart's two lazy chunks
(506 KB, 110 KB gzipped) are no longer built. A curated list would have been smaller
but would have made search miss most of tabler.io. The data comes from `@tabler/icons`,
not `@tabler/icons-react`: the React package is one module per icon, its
dynamic-imports map points at `.ts` files missing from its `dist` in 3.46.0, and it
carries no tags. The two JSON files sit outside `@tabler/icons`' `exports` map, so
`aeTablerIconData.ts` reaches them with `import.meta.glob` on
`/node_modules/@tabler/icons/`; that also keeps `tsc` from typing 2 MB of JSON.
`aeTablerIconData.test.ts` loads the real files, so a package upgrade that moves or
renames them fails there instead of silently drawing folders. The emoji-mart
dependencies stay in `package.json`: `@lobehub/fluent-emoji` still depends on them.
Extensions see project icons through `extensions/services/projects.ts`, which cut the
string at 16 characters until `0023` raised the limit to 48, enough for every
`tabler:` value (41 characters at most in 3.46.0); no AE extension draws project icons
today. Suites:
`aeTablerIconData.test.ts`, `aeTablerIcon.test.tsx`, `AeTablerIconPicker.test.tsx`, and
upstream's `ProjectIconPicker`, `ProjectRowIcon` and `Sidebar` suites, all run by
`just web-check`. Screens: `docs/screenshots/tabler-icons/`.

`0016` (2026-09-24) adds a Paste button to the web terminal on touch devices, because
Reid could not paste commands into a shell from his iPhone: xterm.js draws on a canvas
and keeps its input in a hidden textarea, so a long press on the terminal offers no
Paste. The button sits in the terminal's top right corner while the primary pointer is
coarse (`useIsCoarsePointer`, upstream's own hook) and a writable attach is connected;
it is hidden on a fine pointer, on read-only attaches and while the bridge is
connecting or closed. A tap reads the clipboard inside the gesture and writes it
through `aeWrite` from `0011` (xterm's own paste, bracketed when the shell asked for
it, Enter never pressed), and focuses the terminal after (useful with a hardware
keyboard; iOS does not raise its on-screen keyboard from that call, see `0017`). When the Clipboard API is missing (plain HTTP) or refuses (iOS declined,
permission denied, an empty read), a sheet opens with a textarea where the native long
press Paste works; Send writes its text the same way and Cancel writes nothing. The
text is never logged or stored. The icon is Tabler `clipboard-text` from
`@tabler/icons` 3.46.0, inlined as four path strings in the new file rather than
loading the lazy chunk of every icon for one button, drawn through `AeTablerSvg` from
`0015`. The logic is the new `web/src/components/AeTerminalPaste.tsx`; the only
upstream file touched is `TerminalView.tsx` (four added lines: an import and one
render expression). Suite: `AeTerminalPaste.test.tsx`, run by `just web-check`.
Screens: the terminal-paste screenshot set (removed before publication).

`0017` (2026-09-24) fixes what the dual review of `0016` found in
https://github.com/analyticendeavors/omnigent-ae/pull/44, the way `0013` fixed `0011`.
A paste now goes through `pasteIntoAeTerminal`, the guards a Run gets: nothing is
written unless the socket is open, and multi-line text is refused unless the shell
turned on bracketed paste, because unbracketed every line would run as it arrived (a
heredoc body or a password prompt would take the next line). Exactly one trailing
newline, a common copy artifact, is dropped first. The terminal session is taken at
tap time, and after the clipboard read the text is written only if that same session
is still current and open, so a terminal that re-dialed during the read gets nothing;
a read that resolves after unmount is dropped. Every paste that does not go through
opens the sheet with the text kept and a message for its cause (clipboard unreadable,
clipboard empty, terminal not connected, terminal reconnected during the read, or
multi-line text the shell cannot take), and Send closes the sheet only on success.
`TerminalView.tsx` now keeps the control mounted whenever the attach is writable and
passes `connected`, which disables the button, so an iOS app switch that drops the
socket no longer destroys an open sheet and its draft (the diff there is still an
import and one render expression). The sheet's content renders unconditionally so it
animates closed like other dialogs. The focus call after a paste stays for hardware
keyboards; the docs no longer claim it brings back the phone's keyboard, since iOS
only raises it from a focus inside the user gesture, and the clipboard read is an
await. The multi-line message is the fork's own wording rather than the Run guard's,
whose advice ("copy the block into the terminal") is the thing a phone cannot do. The
suite grew to 16 tests, one of them mounting the real `TerminalView` to show the
control and its sheet outlive a dropped socket.

`0018` (2026-09-24) answers two things Reid asked from his phone. The context ring
under the composer now changes color as the session nears the end of its context
window, the cue Claude Code desktop gives: the used arc is grey below 60%, the theme's
warning color from 60% and its destructive color from 80%, decided on the rounded
percentage the tooltip prints. Those steps are earlier than the plan limit bars' 70
and 90 (`limitTone`) because a context fills inside one session and the harness
compacts before 100%, while a plan limit only stops work at 100%; `limitTone`'s own
comment already described the ring's steps as the earlier ones. The popover's Context
window bar uses the same function, so the ring and the bar always agree. Upstream draws
the ring grayscale on purpose ("a near-full context window is a neutral fact"), so the
AE build keeps the button and the track muted and colors only the arc; the ring has no
visible percentage label in v0.15.0 (upstream's tests keep text out of it), so the
tooltip and the aria label are unchanged. Second, Reid read the thin bar under the
Context window row (cached, cache write and new input in three greys) as a second
meter. Its parts now have their own chart colors (`chart-1` blue, `chart-3` green,
`chart-5` grey, never the warning or destructive colors that mean nearly full), the
legend keeps its dots, and a caption above the bar says what it is ("How those tokens
were billed (prompt cache)"). He expected a `/context`-style breakdown (system prompt,
tools, messages); the status line the relay reads carries only the three cache counts,
so that breakdown would need new data and is not attempted. The logic is the new
`web/src/lib/aeContextTone.ts` (`contextTone`, `aeContextRingClass`); the upstream
edits are `ComposerContextRing.tsx` (the import, one `className` on the used arc and
two doc comment lines) and the name and comment of one test in
`ComposerContextRing.test.tsx` whose assertions still hold (the button stays muted).
`AePlanLimits.tsx` and its suite belong to `0006` and `0009`. Suites:
`aeContextTone.test.tsx` (the steps, and upstream's real ring at 50, 65 and 85
percent), two new cases in `AePlanLimits.test.tsx`, and upstream's
`ComposerContextRing.test.tsx`, all run by `just web-check`.

`0019` (2026-09-25) fixes a layout bug in upstream's Inbox page that Reid hit on his
iPhone (375 px wide): an approval card titled "System information and personal memory"
ran over the timestamp and the "Open session" link, and the agent label ("Claude
Code") ran past the card's right edge. The title span in `web/src/pages/InboxPage.tsx`
had `min-w-0 shrink-0 truncate`; `shrink-0` kept it at full width, so `truncate` never
applied. The patch drops `shrink-0` (one line); the toggle button around it already
has `min-w-0 flex-1`, so the title now shrinks and ends in an ellipsis, with the agent
label inside the same span and cut first. The timestamp stays visible at every width:
`relativeTime` prints a short form ("2h", "3d"), so hiding it below `sm` would free
almost no room. One case in upstream's `InboxPage.test.tsx` pins the classes and fails
with `shrink-0` back; `just web-check` runs that suite. It is a candidate to offer
upstream, and to drop at the sync that ships a fix.

`0020` (2026-09-25) fixes when `0012`'s card offers "Set this session to review".
Review means the session is done and frees one of Reid's three slots, yet a session
that had opened four pull requests showed the offer after the first merge, while
three were still open: the card only asked the server whether the session was
already in review. The rule is now in a new file, `web/src/lib/aeReviewOffer.ts`:
after a merge, take the session's other tracked pull requests (upstream's
per-session list) and read their states from the same `["ae-pr", repo, number]`
queries the cards use; offer review only when none is open. While some are open the
card offers nothing and shows a muted line ("3 more open pull requests in this
session."); while a state is still unknown it shows nothing, so a slow or failed
read never offers review early (the Status submenu still sets review by hand). The
GitHub panel's cards pass the list they hold; a card under a chat message takes it
from the `github-info` cache or reads it once after the merge. The only files
touched are `0012`'s own: `AePrCards.tsx` (the import, the hook call, the muted
line, one condition and a `tracked` prop) and `AeInlinePrCards.tsx` (one line
passing that prop). Suites: `aeReviewOffer.test.ts` and new cases in
`AePrCards.test.tsx` and `AeInlinePrCards.test.tsx`, run by `just web-check`. The
Dashboard's Open PRs card gets the same rule out of tree, in
`packages/ae_omni_extension/web/src/reviewOffer.ts`.

`0021` (2026-09-25) restyles `0012`'s merge card after Reid's iPhone screenshots of
the chat's inline card. Collapsed, a small Merge button sat bottom left beside an
empty half row. On the confirm step the two methods ("Squash and merge", "Merge
commit") were boxed buttons with the same shape and weight as Confirm merge and
Cancel, so the step read as four actions in a 2x2 grid and the chosen method was
barely visible. Now the card is a container (`@container/aepr`), and in a card
under 30rem wide (a phone, or the narrow GitHub panel) Merge, Confirm merge and
Set this session to review span the card, and Cancel is a quiet text button under
Confirm; wider cards keep them side by side. The method is one segmented control:
a bordered track, the chosen segment filled and ticked with Tabler's `check`
(inlined as its one path through `AeTablerSvg`, so no icon set loads), still a
`radiogroup` of `radio` buttons named as before, and still hidden when the
repository allows one method. Behavior, test ids and accessible names are
unchanged. The only files touched are `0012`'s own: `AePrCards.tsx` and
`AePrCards.test.tsx` (a case that the chosen method is the checked radio of the
group, and one that a single method shows no group), run by `just web-check`. The
Dashboard's Open PRs and Your move cards get the same look out of tree
(`packages/ae_omni_extension/web/src/prs.ts` and `style.css`). Screenshots at
390x844 and 1440x900, dark and light: `docs/screenshots/merge-card-phone/`.

`0022` (2026-09-26) answers two screenshots Reid sent of a 1100 px card. The confirm
step put the method buttons on one row and Confirm merge and Cancel on a second; it is
now one wrapping row (`data-testid="ae-pr-confirm-row"`): the segmented toggle, only
when the repo allows both methods and with the short labels Squash and Merge commit
(the question line above keeps "Squash and merge #52 into main?"), then Confirm merge
and Cancel (outline again, replacing `0021`'s quiet text Cancel under Confirm). In a
card under 30rem the toggle takes the row's full width and the two buttons share the
next row. After a merge the offer read "Set this session to review" on a full black
primary button and Reid did not know what it meant;
it now reads "Mark session done", an outline button (it is optional) with a muted
"frees a slot" hint beside it, tied by `aria-describedby`, and a tooltip saying it
sets the status to Review; success reads "Session marked done. Its slot is free."
Merge logic, the head SHA pin, `0020`'s offer rule and every test id are unchanged.
The files touched are `0012`'s and `0020`'s own: `AePrCards.tsx` and its suite,
`AeInlinePrCards.test.tsx` (one assertion on the label) and the header comment of
`aeReviewOffer.ts`. The Dashboard's Open PRs and Your move cards get the same row and
label out of tree (`packages/ae_omni_extension/web/src/prs.ts` and `style.css`).

`0023` (2026-09-27) fixes the eight bugs a post-merge code review found in two merged
pull requests, https://github.com/analyticendeavors/omnigent-ae/pull/43 (`0015`, the
Tabler project icons) and https://github.com/analyticendeavors/omnigent-ae/pull/53
(`0021` and `0022`, the merge card's phone restyle). Upstream-side:

- Icon load recovery. `useAeTablerIcons` stopped at "error" for good: nothing told a
  row icon about a later successful load, so every row drew the folder until a reload
  even after the picker's Try again worked. `aeTablerIconData.ts` now keeps a small
  subscriber set (`subscribeAeTablerIcons`) and the hook subscribes while it has no
  set. The retry itself could never succeed on Safari, which caches a failed dynamic
  `import()` in its module map: the two JSON files are now fetched as assets (an eager
  `?url` glob puts only their URLs in the bundle, relative to `import.meta.url`) and a
  retry adds `retry=N` to the URL. The build emits them as
  `assets/tabler-nodes-outline-<hash>.json` (249 KB gzipped) and
  `assets/icons-<hash>.json` (204 KB gzipped).
- Prototype names. `set[name]` had no own-key check, so `tabler:constructor` found
  `Object.prototype.constructor` and `.map` threw, taking the Sidebar down. The lookup
  uses `Object.hasOwn` and such a name draws the folder.
- Truncated names to extensions. `PROJECT_ICON_MAX_LENGTH` in
  `extensions/services/projects.ts` goes from 16 to 48 (`tabler:` plus the longest
  outline name in `@tabler/icons` 3.46.0 is 41 characters); `tabler:brand-github`
  reached extensions as `tabler:brand-git`. A test reads the installed package and
  fails if an upgrade ships a longer name. This and its test are the only upstream
  files touched; the rest are `0015`'s and `0012`'s own.
- Enter on an empty search picked the first suggestion ("folder"), and the landing
  tile saves on select, so it overwrote the icon. Enter now picks only for a typed
  query.
- Focus in the rename popover. emoji-mart's `autoFocus` went with it, so Radix's open
  auto-focus landed on the Sidebar's "Remove icon" button and Space or Enter removed
  the icon. The picker focuses its own search box on mount, which runs before the
  popover's auto-focus, so Radix leaves it there; `Sidebar.tsx` is unchanged.
- Card shrinks in chat. The inline card is a size container, so its content does not
  size it, and the chat bubble (`MessageContent`, `w-fit`) shrank it to the message
  text; a one-line reply got a narrow card that switched to the phone layout on a
  desktop. `w-full` alone would not help (a percentage of a fit-content parent), so the
  card gets a set width, `w-xl max-w-full`: 36rem, capped at the bubble.
- Method changeable mid-merge. The segmented method buttons are disabled while the
  merge is pending.

Out of tree (`packages/ae_omni_extension/web`): the Dashboard's radios are disabled,
and a change is ignored, while that card's merge is in flight; `.card-pr` and
`.card-pull` are inline-size containers and the phone rules switch on
`@container (min-width: 30rem)` instead of a 480 px viewport, so in the two-column grid
(720 px up) a narrow card gets the phone layout. The extension's root font is 15 px,
so its 30rem is 450 px against the chat card's 480 px. Suites: the Tabler suites,
`AePrCards.test.tsx` and `projects.test.ts` (all in `just web-check`), and the
extension's `prs.test.ts` and `layout.test.ts`. Screens:
`docs/screenshots/review-fixes-43-53/`.

`0024` (2026-09-27) answers the review of `0023`'s own pull request,
https://github.com/analyticendeavors/omnigent-ae/pull/57. All changes are in files
`0015` and `0023` created, plus two lines at the end of upstream's `test-setup.ts`:

- The embed. Upstream's library-mode build (`vite.embed.config.ts`, `build:embed`)
  inlines every asset as a data: URL, so `0023`'s eager `?url` glob put both JSON files
  into `chunks/aeTablerIcon-*.js`, which the embed entry imports statically: 5.4 MB
  (694 KB gzipped). The glob now asks for `?url&no-inline`, which Vite honours before
  the library rule, so the embed emits `assets/tabler-nodes-outline.json` and
  `assets/icons.json` and the chunk is 4 KB; the entry's static graph went from
  12.2 MB (2.3 MB gzipped) to 6.9 MB (1.6 MB gzipped). This fork does not ship the
  embed (no Dockerfile, deploy script, workflow or recipe builds it; only upstream's
  `tests/e2e_ui/test_embed_service_worker.py` does), but the patch should not break
  it. A retry never adds its query to a data: or blob: URL (`aeTablerRetryUrl`).
- `Object.hasOwn` is missing on Safari and iOS 15.0 to 15.3, which upstream's
  `build.target` (`safari15`, `ios15`) still covers; the lookup uses
  `Object.prototype.hasOwnProperty.call`.
- Tests. The loader's one network call is `lib/aeTablerAssetFetch.ts`; `test-setup.ts`
  mocks it with `lib/aeTablerAssetFetch.disk.ts`, which reads the installed files, so
  the Sidebar, ProjectRowIcon and ProjectIconPicker suites draw icons as before and
  none of their fetch stubs or call counts sees the icon set. Test paths to
  `node_modules` resolve from the test file, not the working directory.
- Recovery. An icon that saw a failed load no longer waits for the picker: the loader
  retries on the window `online` event, and when a new icon subscribes after a
  failure once a backoff has passed (2 s, doubling to 30 s), so a hard failure does
  not loop. The picker's Try again loads at once.
- `useAeTablerIcons` is `useSyncExternalStore(subscribeAeTablerIcons,
  aeTablerIconsState)`; subscribing starts the load, and the hand-built store and its
  race check are gone.
- Listeners run outside the load's promise chain, each in a try/catch, so a
  throwing listener cannot leave the load rejected while the set is in.

Out of tree, from the same review: the Dashboard's Cancel is disabled while a merge
is in flight, and a card whose merge is in flight cannot open a second confirm step
(`confirmRow` returns early), so Cancel then Merge can no longer reopen live radios.

`0025` (2026-09-27) lets an extension open a GitHub pull request or issue page. Reid
works from an iPhone (Safari) and a Windows laptop (Firefox) and asked to stop copy
and paste: the Dashboard's Open PRs and Your move cards offered only Copy link,
because the page runs in an iframe with `sandbox="allow-scripts"`
(`ExtensionViewHost.tsx`) and cannot open a window itself, and the host's
`navigation.openExternal` (`web/src/extensions/services/useExtensionHostServices.ts`)
refused any URL it had not handed out ("URL was not provided by the host"); the
Dashboard's pull requests come from the AE routes, not from `sessions.pullRequest`.
The upstream edit is one condition in that handler (the import and the widened `if`,
six lines after prettier): a URL the host handed out still opens as before, and so
does one that `isAeGithubLinkUrl` accepts, from the new
`web/src/lib/aeExternalUrls.ts`. The rule is exactly
`https://github.com/<owner>/<repo>/pull/<n>` (optionally followed by `/files`,
`/commits` or `/checks`) or `https://github.com/<owner>/<repo>/issues/<n>`, with an
optional `#fragment` of `[A-Za-z0-9._~-]`; owner and repo from `[A-Za-z0-9._-]` and not
all dots; `<n>` a positive integer without a leading zero, at most ten digits; no
query, no userinfo, no port, lowercase host, and the string must equal the `href` the
URL parser gives it, so nothing encoded or normalised slips through. The three
suffixes are accepted on pull requests only; an issue has no such pages. Why this is
safe: the method still needs the `navigation` permission, the host still opens with
`window.open(url, "_blank", "noopener,noreferrer")`, and the worst a page holding the
permission can do with it is open a GitHub pull request or issue page in a new tab,
with no query string to carry parameters. Every other URL keeps upstream's rule.
Where the tab lands: a browser opens it in that browser; the desktop app's
`setWindowOpenHandler` (`web/electron/src/main.js`, unchanged) sends web links to
`shell.openExternal`, the OS default browser; the iPhone Home Screen app hands it to
Safari. One upstream test changed: the handed-out case in
`useExtensionHostServices.test.tsx` used a github.com URL, which is now always
allowed, so it uses `https://git.example.com/...` and still proves the handed-out
rule. Upstream's own docs (`docs/extending/browser_extensions.md`) still describe the
handed-out rule only; they are not patched. Suites: `aeExternalUrls.test.ts` (accept
and reject cases: `javascript:`, `http:`, other hosts, lookalikes such as
`github.com.evil.example`, userinfo, ports, queries, extra segments, encoded slashes)
and `useExtensionHostServices.aeGithub.test.tsx` (a GitHub pull request or issue URL
opens without being handed out, a non-GitHub URL is still refused, and an extension
without `navigation` gets no method), both run by `just web-check`. The Dashboard's
Open on GitHub button is out of tree (`packages/ae_omni_extension/web/src/openLink.ts`).

`0026` (2026-09-27) is Reid's second note that the pull request cards waste space. His
screenshot of `0012`'s compact card under a chat message, about 1200 px wide, showed
each piece on its own full-width row with the right two-thirds empty: title, chips,
"Merged #55 (squashed), b0413d6. Pull Request successfully merged", "1 more open pull
request in this session.", and on a conflicted card "Conflicts with the base branch."
above a tall disabled Merge. `AePrCards.tsx` now puts the chips, the outcome or the
reason, and the action on one status row (`data-testid="ae-pr-status-row"`,
`flex-wrap`): each part after the chips is `flex-auto` (1 1 auto), so it stays on the
chips' line when its content fits and takes a row of its own when it does not, and
Merge or Mark session done carries `ml-auto` to sit at the row's right end. The merged
line reads "Merged (squashed) b0413d6" (GitHub's `message` is dropped; it only
repeated the line, and the title row already carries the number) and the "N more open
pull requests" note is an inline muted span. Buttons use `h-8 pointer-coarse:h-10`,
32 px with a mouse and 40 px on a touch screen like the Dashboard's `.btn`, instead of
`h-11 md:h-8`; Merge no longer spans a phone card (`WIDE_ON_PHONE` is gone). The card
is `px-2 py-1.5` with `gap-1` (was `p-2`, `gap-1.5`). The confirm step, the merge
logic, the head SHA pin, `0020`'s offer rule and every existing test id are unchanged.
Only `0012`'s own files change: `AePrCards.tsx`, `AePrCards.test.tsx` (the status row
holds the chips, the reason and a compact Merge; the merged line has no echo and the
note sits on the row) and `AeInlinePrCards.test.tsx` (the merged text), run by
`just web-check`. The Dashboard's Open PRs and Your move cards get the same row out of
tree (`statusRow` in `packages/ae_omni_extension/web/src/prs.ts`, `.pr-status` in
`style.css`).

`0027` (2026-09-27) fixes the should-fix both reviewers (Claude and Codex) found in
https://github.com/analyticendeavors/omnigent-ae/pull/58, after it merged: a blocked
new tab looked like success. `0025` kept upstream's
`window.open(url, "_blank", "noopener,noreferrer")`, and with `noopener` the call
returns null even when a tab opened, so the handler could not tell; the open also
runs one MessagePort hop after the tap, where iOS Safari may block it, and the
Dashboard's fallback never ran. The new `aeOpenExternalWindow` in
`web/src/lib/aeExternalUrls.ts` opens with `window.open(url, "_blank")`, sets the new
window's `opener` to null at once (the protection `noopener` gave), and throws
`ExtensionHostServiceError("PopupBlocked", ...)` when the call returns null. In a
native shell (`isNativeShell()`) it keeps upstream's `noopener` call and throws
nothing, because those shells route the link to the system browser and report null
for every open (`followLinkWithPopupFallback` in `ChatMarkdown.tsx` says the same).
The handler's edit is the import and one line (`aeOpenExternalWindow(url)` in place of
the `window.open` call); the URL allowlist and `isAeGithubLinkUrl` are unchanged, and
the handed-out URLs take the same path. One consequence: without `noreferrer` the
browser sends GitHub the referrer its policy allows (by default only the OmniGent
server's origin). Tests: `useExtensionHostServices.aeGithub.test.tsx` (the GitHub URLs
get a window back and its `opener` is cleared, a null return rejects with
`PopupBlocked`, an Electron shell keeps the `noopener` call) and upstream's handed-out
case in `useExtensionHostServices.test.tsx` (a returned window, the two-argument
call), run by `just web-check`. The Dashboard's side (the Copy button and the URL in
the status line) is out of tree, in `packages/ae_omni_extension/web/src/openLink.ts`.

`0028` (2026-09-28) is Reid's ask, from a screenshot of the chat card's confirm step
("Squash and merge #60 into main?", Squash ticked), that the step say which method to
use, select it by default and keep the other one selectable. Squash was always
preselected, which is wrong when a pull request promotes one long-lived branch into
another: a squash puts a commit on the base that the head never gets, so the next
promotion of the same branch conflicts. The server decides
(`recommend_merge_method` in `packages/ae_omni_policies/src/ae_omni_policies/prs.py`)
and the card's `merge` block gains `recommended_method` and `recommended_reason`;
`default_method` now equals the recommendation. `ConfirmMerge` in `AePrCards.tsx`
opens on the recommended method; that segment carries a small muted "Recommended"
pill (`data-testid="ae-pr-recommended-tag"`) and the reason as its `title`, and the
reason is one muted line under the question (`ae-pr-recommended-why`), on every width.
The question still follows the selected method, picking the other method still
merges with it, and a card without the two fields (an older server) opens on
`default_method` as before. The merge logic, the head SHA pin and every existing test
id are unchanged. Only `0012`'s own files change: `AePrCards.tsx`, the card type in
`web/src/lib/aePrs.ts`, and `AePrCards.test.tsx` (the default, the tag, the reason
line, switching to the other method and merging with it, and a card without a
recommendation), run by `just web-check`. The Dashboard's Open PRs and Your move
cards do the same out of tree (`packages/ae_omni_extension/web/src/prs.ts`,
`.pr-confirm-why` and `.pr-method-tag` in `style.css`).

`0059` (2026-10-16) stops the sidebar's "Dashboard n/3" row lighting up on the Fabric
Admin page. The row was active for any path under the Dashboard extension's prefix
(`/extensions/analyticendeavors.parking-lot/`), and Fabric Admin is a third page of
that same extension. `isAeDashboardPath` in `web/src/lib/aeLabels.ts` now names the
Dashboard's two views (`lot` and `your-move`, the pages in `plugin.py`), and
`AeSlotsPill.tsx` calls it in place of the prefix test (two lines). Tests:
`aeLabels.test.ts` (the helper) and `AeSlotsPill.test.tsx` (no `aria-current` on
`/fabric`), run by `just web-check`. It sits on the fork branch
`analyticendeavors/omnigent@ae/fabric-admin-nav-3`, based on the commit `main` pinned
before it (`45dbee9`).

`0064` (2026-10-16) lets the Fabric Admin page open its two links. The page asks the
host to open "Open in Azure" and "Open in Fabric" through `navigation.openExternal`,
and `0025`'s allowlist (host-issued URLs and GitHub pull request or issue links)
refused both. The new `isAeFabricAdminLinkUrl` in `web/src/lib/aeExternalUrls.ts`
admits exactly two shapes: one capacity's overview in the Azure portal
(`https://portal.azure.com/#resource/subscriptions/<guid>/resourceGroups/<rg>/providers/Microsoft.Fabric/capacities/<name>/overview`,
a strict 8-4-4-4-12 hex guid, Azure's name characters and lengths for the group and
the capacity, the provider and type spelled exactly as written) and
`https://app.fabric.microsoft.com/admin-portal/capacities` with nothing after it. Both
are https with the exact host, no userinfo, port, query or extra path, and the URL
must round-trip through `new URL` unchanged. `useExtensionHostServices.ts` imports the
helper and adds one more `&&` term beside `isAeGithubLinkUrl`; the open path
(`aeOpenExternalWindow`, `PopupBlocked`) is unchanged. Tests: `aeExternalUrls.test.ts`
(both accepted; other hosts, http, userinfo, ports, queries, fragments, extra paths,
other blades and providers, bad guids and names, `javascript:` rejected) and
`useExtensionHostServices.aeGithub.test.tsx` (both open, a lookalike is refused), run
by `just web-check`. It sits on the fork branch
`analyticendeavors/omnigent@ae/fabric-admin-links-2`, based on the commit `main` pinned
before it (`b8bd78d`).

`0068` (2026-10-17) renders a pull request card's description as markdown. Review
details printed the PR body as plain pre-wrapped text, so GitHub markdown showed its
`##` headings, `**` bold and backticks literally. `AePrCards.tsx` now hands the body to
the chat's `MessageResponse` (GFM, the chat's sanitising rehype plugins and link
safety) in `mode="static"`, because a finished body needs no streaming repair of
unclosed marks, with `remark-breaks` added, because GitHub treats a single newline in
a PR body as a line break, and a class that keeps headings at body size inside the
card. The body renders only once Review details opens, so collapsed cards skip the
markdown pipeline, and a body past the chat's limits (50,000 characters or a 5,000
character unbroken run) falls back to the chat's plain text view; `ChatMarkdown.tsx`
exports `isPathologicalText` and `PlainTextFallback` for that, with no other change.
The workspace file-link layer the chat adds is left out: paths in a PR body name repo
files, not workspace files. Images stay blocked and relative links stay unresolved,
the chat's own hardening, left as is. An empty body still says "No description
provided." Tests: `AePrCards.test.tsx` (markdown rendered, nothing rendered while
collapsed, an unmatched `**` kept literally, a huge body shown as plain text), run by
`just web-check`. It sits on the fork branch
`analyticendeavors/omnigent@ae/pr-body-markdown-3`, based on the commit `main` pinned
before it (`84460f2`).

`0069` (2026-10-17) says how many checks passed while CI is still running. A pull
request card's CI chip read "1 of 8 checks running", which reads as one check done;
`aeCiText` in `web/src/lib/aePrs.ts` now says "7/8 passed, 1 running". The other
states keep their wording. The server's merge reason (`ae_omni_policies/prs.py`) and
the Dashboard's copy of the chip text (`ae_omni_extension/web/src/prs.ts`) change in
the same pull request, out of tree. Test: `AePrCards.test.tsx` (the running chip's
text and tone), run by `just web-check`. It sits on the fork branch
`analyticendeavors/omnigent@ae/pr-body-markdown-3`, after `0068`.

`0070` (2026-10-17) shows native Codex questions inline in Claude's question form.
`0062` had given Codex questions a modal dialog with its own buttons, unlike the inline
`AskUserQuestionForm` Claude's questions use, and `Transcript` kept every question
with no exact "Answer to Codex questions" reply pending, so a plain chat reply left
full-width "Answer Codex questions" buttons behind. `CodexQuestionCard.tsx` now
renders inline with `AskUserQuestionForm` (options as radios, a free-text row,
Prev, Next, Submit and Cancel), disabled through a `fieldset` while the answer
sends; Cancel folds the card to a "Show Codex's question" link that brings it back.
It still sends one normal user message, "Answer to Codex questions:" then a
"title: answer" line per question with line breaks folded to spaces, with the same
stable id and retry on failure. The new `web/src/components/chat/aeCodexQuestions.ts`
holds the mapping to Claude's question shape, the answer text, and
`aePendingCodexQuestions`: only the newest question set that no real user message
has followed is pending, so a plain reply or a newer question (with or without an
item id) retires older ones, and a system notice does not. `Transcript.tsx` calls it
from `pendingCodexQuestions`, treats a reply still sending as a reply, and drops the
auto-open flag. Tests: `CodexQuestionCard.test.tsx` (inline with no dialog, one
answer message that stays answered after reload, several questions with free text
and a line break, a failed send kept to retry, Cancel and the link back, disabled
while sending) and `Transcript.activeTurn.test.ts` (a plain reply retires, the newest
of several wins, a newer question with no item id supersedes, a system notice keeps
it), run by `just web-check`. It sits on the fork branch
`analyticendeavors/omnigent@ae/codex-question-form`, based on the commit `main`
pinned before it (`34e4fa1`).

`0071` (2026-10-17) adds the session rail: a right-hand column of folding sections
about the open session (Triage, Environment, Context, Agents, Scheduled, Usage,
Connectors, Sent files, Outputs, Changes) that reorder by drag, or by each section's
menu on a phone, with order, folds and the open state kept per device. The chat header
toggles it, and a phone opens it as a full-screen sheet from the session menu. The
usage popover keeps one context bar with its top categories, the agent lanes and the
session's own plan, and its "Show details in session rail" button replaces the detailed
breakdown toggle. The popover and the rail share one context snapshot cache; the runner
returns MCP server names and statuses with the snapshot, and a new route keeps a Codex
session's last MCP startup map. Tests: the rail store, container, header toggle and
every section, plus the popover, snapshot cache and runner suites, run by
`just web-check` and `just upstream-smoke`. It sits on the fork branch
`analyticendeavors/omnigent@ae/session-rail`, based on the commit `main` pinned before
it (`7ac022c`).

`0073` (2026-10-17) renames the sidebar's Dashboard row to Session Dashboard. The AE
Dashboard became the Session Dashboard that day, with a read-out header in place of
its section chips (`packages/ae_omni_extension`, both page titles); the primary
navigation row in `web/src/components/AeSlotsPill.tsx` follows and reads "Session
Dashboard n/3", and the session header's slots chip (`AeSessionChip.tsx`) tells a
screen reader "open the Session Dashboard", and the session rail's lot error
(`aeRail/AeRailTriage.tsx`, `0071`) names it too. The links, the count, the red at the
limit and the active state are unchanged. Tests: `AeSlotsPill.test.tsx`,
`Sidebar.aeStatus.test.tsx` and `AeSessionChip.test.tsx` (the accessible names), and
`Sidebar.rowActions.test.tsx`, whose session-row match (`/^Session/`) also caught the
renamed row and now asks for `/^Session [A-Z]\b/`; run by `just web-check`. It sits on the fork branch
`analyticendeavors/omnigent@ae/session-dashboard-4`, based on the commit `main` pinned
before it (`b198887`).

`0074` (2026-10-17) shows when a session is working, quiet, waiting or scheduled. Every
session list row and websocket update carries `background_task_count` and
`active_child_count`; the sidebar adds waiting (a pulsing hourglass with the count),
quiet (an amber spinner after five minutes without an update) and scheduled (an alarm
with the next check-in time) row states, and a status line above the composer shows the
elapsed time, current step and last activity while working, the agents and shells still
running while waiting, and when a check-in will wake the session. Tests:
`aeSessionActivity.test.ts`, `aeSessionActivityHooks.test.ts`,
`AeSessionActivity.test.tsx`, `aeLastEventAt.test.ts` and
`tests/server/routes/test_ae_session_activity.py`, run by `just web-check` and
`just upstream-smoke`. It sits on the fork branch
`analyticendeavors/omnigent@ae/session-activity`, based on the commit `main` pinned
before it (`4685063`).

`0077` (2026-10-17) shows a native Claude session's live progress line in the chat's
Working row. While Claude Code streams one long tool call (a Write of a long file,
minutes at a time) the terminal shows a spinner line, `✻ Crunching… (4m 42s · ↓ 24.5k
tokens)`, but the web chat showed only "Working": the forwarder relays the transcript,
the hooks and the message deltas, and Claude Code writes a tool call to the transcript
only once its input has finished streaming. And "last activity" counted any event,
usage and status posts included, so it read "just now" with nothing happening.
`harnesses/claude_native/ae_pane_progress.py` (new) parses the spinner line from the
pane capture the forwarder already takes every two seconds (`bridge.py`'s
`read_pane_signals` gains a `progress` field, and `_composer_row`'s input-box finding
moves into `_composer_opening_rule` so both use it; `forwarder.py` calls the relay),
with a strict pattern (a spinner glyph, a capitalized gerund with an ellipsis, a
parenthesized elapsed time, optional token count) so a reworded line yields nothing
rather than noise. An empty capture, a pane with no input box, or the `/btw` overlay
reads "unknown" and posts nothing. It posts `external_session_progress` when the step
or token count changes, otherwise at most every ten seconds (with `elapsed_s`, so the
web ticks the time itself), once more when the line goes, and a clear to the old
session on a `/clear` or `/fork` rotation. The type and field limits live in one
module, `util/ae_session_progress.py`, that the forwarder and the server share. The
server (`routes/_sessions/ae_session_progress.py`, new; the type in `common.py`,
`routes_events.py`, `schemas.py`, `openapi.json`) validates bounded fields and
publishes it as the transient SSE `session.progress`, never stored. The web
(`lib/aeSessionProgress.ts` and `lib/aeStreamObserver.ts`, new; small edits in
`events.ts`, `sse.ts`, `chatStore.ts`, `conversationState.ts`,
`aeSessionActivityHooks.ts`, `AeSessionActivity.tsx`) keeps the latest line per
conversation with when it arrived, drops it after 30 seconds without an update, clears
it when an item or tool row lands, when the session stops, is superseded or rebinds,
shows "Crunching · 4m 42s · 24.5k tokens" in the Working row, says "no output yet; a
long step may be writing" a minute after the later of the last content and the turn's
start, and stamps "last activity" from content events and the idle-to-running edge only. Tests: `tests/test_ae_pane_progress.py`,
`tests/server/integration/test_ae_session_progress.py`, and the web
`aeSessionProgress` (lib, store, component) and `aeLastEventAt` suites, run by `just
test` and `just web-check`. It sits on the fork branch
`analyticendeavors/omnigent@ae/live-progress-2`, based on the commit `main` pinned
before it (`e95ad6e`). A dual review before the PR found ten points, all fixed in it.

`0084` (2026-10-18) shows session names, never ids, in the split view's pane pickers.
Names came only from the first page of unarchived sessions, so a pane on any other
session (archived, older, shared, just created) showed its raw id in its picker. A pane
the list does not hold now fetches its own row (`fetchConversationById`, at most four,
refetched when the pane frame's title changes), and every picker label goes through
`aeSplitPaneName` in `lib/aeSplitLayout.ts`: the list title, the fetched title, the
frame's document title, "Loading..." while the fetch runs, then "Untitled session",
with the legacy status glyph dropped and archived sessions read "Name (archived)". AE
files only (`pages/AeSplitPage.tsx`, `lib/aeSplitLayout.ts`). Tests:
`aeSplitLayout.test.ts` and `AeSplitPage.test.tsx`, run by `just web-check`. It sits on
the fork branch `analyticendeavors/omnigent@ae/split-and-rail`, based on the commit
`main` pinned before it (`67b526b`).

`0085` (2026-10-18) gives the session rail toggle its own icon and opens the rail in
every session. The toggle and the phone menu's "Session rail" item draw Tabler
list-details for both states, so they no longer look like the workspace panel toggle
beside them. A session that restored its open workspace panel closed the rail on
windows up to about 1440 px; the rail store's new `openInEverySession` preference (on
by default, saved in `ae.sessionRail.v1`, older layouts load with it on) opens the
rail at every session switch on a desktop (`shell/AeSessionRail.tsx`); `0087` replaced
how it makes room. Settings >
Chat (`pages/settings/AeChatSettingsSection.tsx`, new) carries the switch. Upstream
hunks: `shell/settingsNav.tsx` (the `chat` id, one nav item, the nav icon type widened
to any component taking a class name) and `pages/SettingsPage.tsx` (one import, one
line), and `web/src/test-setup.ts` (the preference off by default in tests, since
upstream's shell tests do not mock the rail's sections; the rail's own tests turn it
on). Tests: `aeSessionRail.test.ts`, `AeSessionRail.test.tsx` and
`AeChatSettingsSection.test.tsx`, plus the existing `settingsNav` and `SettingsPage`
suites, run by `just web-check`. It sits on the fork branch
`analyticendeavors/omnigent@ae/split-and-rail`, after `0084`.

`0086` (2026-10-18) fixes the review findings on `0084`. A pane outside the list now
fetches its row once the list has loaded or failed, and reads "Loading..." only while
that fetch is pending, so a failed fetch or list falls back to the frame title or
"Untitled session". The frame-title watcher refetches only when a pane's name changes
from one name to another and forgets panes that left the layout. `aeFrameTitleName`
reuses `UNTITLED_CONVERSATION_LABEL` and `DEFAULT_APP_NAME`. AE files only. Tests:
`aeSplitLayout.test.ts` and `AeSplitPage.test.tsx`, run by `just web-check`. It sits on
the fork branch `analyticendeavors/omnigent@ae/split-and-rail`, after `0085`.

`0087` (2026-10-18) fixes the review findings on `0085`'s rail behavior by replacing its
claim on the room with a restore rule. The claim closed panels through the header
toggle until the user's first click, which blocked keyboard users, closed panels the
agent opened while the user typed, reopened the rail when a new chat's temp id became
real, and saved each automatic close as the user's choice and the app-wide default.
Now `aeRailRestoredPanelOpen` (`lib/aeSessionRail.ts`) decides at AppShell's two
workspace-panel restore points: with the setting on, on a desktop, with no file or
comment in the URL, a restored panel that would not fit beside the rail starts closed
for this view, and nothing is saved. The rail opens on a session switch but not on a
new chat's id swap, recognized by the pair `hydrateLocalConversation` records in
`lib/aeTempPromotions.ts` (new); every later panel open swaps with the rail as before. Settings >
Chat now sits in SettingsPage's own `Section` shell, which SettingsPage wraps around
the AE body (exporting `Section` to the AE file would have made an import cycle that
the lint rejects), and its nav icon moves to `pages/settings/AeChatSettingsIcon.tsx`.
Upstream hunks: `shell/AppShell.tsx` (one import, the two restore expressions and a
reset on the no-session route, 18 added and 2 removed lines), `store/chatStore.ts` (one
import and one line recording the temp id's server id), `pages/SettingsPage.tsx` (the section line becomes a
`Section` wrapper), `shell/settingsNav.tsx` (the icon's import path) and
`web/src/test-setup.ts` (the setting off by default in the store and in storage, for
upstream's shell tests, which do not mock the rail's sections). Tests:
`aeSessionRail.test.ts`, `AeSessionRail.test.tsx`, `AeChatSettingsSection.test.tsx` and
`AppShell.aeSessionRail.test.tsx` (a restored panel starts closed with nothing saved,
the header toggle opens it by mouse and by keyboard, the agent opening a panel swaps
with the rail, and the rail is back at the next switch), run by `just web-check`. It
sits on the fork branch `analyticendeavors/omnigent@ae/split-and-rail`, after `0086`.

`0088` (2026-10-18) shows every sidebar row's waiting state and agent count, not only
the open session's. A list row's `status` rolls up busy direct children
(`_session_status_with_child_rollup`), so a row whose own turn had ended with agents
still working read as running, and only the open chat's store could tell it was
waiting. `SessionListItem` gains `turn_active` (own status from the relay cache or
`live_status`, or a message still waiting on the runner) and `blocked_on` (the latest
status edge's dialog, cached in `common.py` by `_publish_status` and cleared by any
edge without one); `ae_session_activity.py` reads both, `orchestration.py`'s list
builder sets them, `openapi.json` is regenerated. The web (`useConversations.ts`,
`aeSessionActivityHooks.ts`) reads `turn_active === false` as the open chat's
turn-idle signal and the row's `blocked_on` when the chat store has none, for rows and
for a collapsed project's marker, which now reads waiting when every running row only
waits on agents. Tests: `tests/server/routes/test_ae_session_activity.py` and
`aeSessionActivityHooks.test.ts`, run by `just upstream-smoke` and `just web-check`.

`0089` (2026-10-18) fixes the review findings on `0088`. `blocked_on` is served only
while the session's cached status is running or waiting, and the paths that write that
status without `_publish_status` (handing the session to another replica, the
failed-to-idle reset, the runner status probe) clear it, so a row never reads parked on
a dialog that is gone. The list builder reads the session's own status and its
starting flag once and derives both `status` and `turn_active` from them
(`_session_status_with_child_rollup` and `_list_status_with_starting` take them as
optional keywords). The open row prefers the chat store's turn and dialog over the
list row's, which lag by a websocket tick, so a new turn no longer flashes the
hourglass. Known gap: `blocked_on` lives only in the replica that holds the runner
tunnel, so with several server replicas the others serve none; this deployment runs
one server.

`0090` (web) lights the turn rail's last tick when the chat is scrolled to its end. The
`activeTurnId` memo in `Transcript.tsx` picks the user turn at the viewport midpoint, so
at the bottom the turn before the last stayed lit. A two-line hunk there returns the last
user turn when `aeAtTranscriptEnd` (new `web/src/lib/aeActiveTurn.ts`, within 4 px of
`scrollHeight`) says the chat is at its end, and the memo now also depends on the scroll
offset. Tests: `aeActiveTurn` and `Transcript.activeTurn`, run by `just web-check`.

`0091` (2026-10-18) shows the session rail's Context and Connectors sections as loading
while Claude starts, and keeps Try again for a lost connection. A new session's first
`context_usage` request arrived 0.4 seconds before Claude's SessionStart hook; the
runner (`runner/ae_context_usage.py`) found no `context_raw.json`, no session id or no
live process, raised the same `ValueError` as a real failure, answered "Could not read
Claude context" and started its 30-second backoff, and the web cache
(`components/aeContextUsage.ts`) stored that answer as fresh for three minutes. The
runner now raises `ContextNotReady` for those three cases and answers `{"available":
false, "pending": true, "reason": "Claude is starting."}` with no failure recorded, and
it logs each real failure at info (class and a path-trimmed first line; requests inside
the backoff are not logged again). `routes_ae_context_usage.py` adds `"offline": true`
to its runner-offline or timeout answer; the route's response stays a free-form object,
so `openapi.json` is unchanged. In the web, only an available snapshot is fresh; a
pending answer, or an unavailable one that is not offline, schedules a quiet retry
(2, 4, 8, 16, then 30 seconds) for up to two minutes while the hook reports `pending`
and `loading`, a real snapshot already shown stays until a newer one replaces it, and
the retry timer stops with the last subscriber. An offline answer or an HTTP error shows
at once. The hook also reloads when the focused session's status goes from running or
waiting to idle or failed, and the refresh button starts a new retry window.
`AeRailContext.tsx` shows its loading line instead of Try again while pending;
`AeRailConnectors.tsx` shows "Checking connectors…" with no error. Tests: the runner's
`tests/runner/test_ae_context_usage.py` (three not-ready cases answer pending with no
backoff; a real failure answers the error, backs off and logs without the path), the
route's `tests/server/routes/test_ae_context_usage_routes.py` (no runner answer is
`offline`), and the web `aeContextUsage`, `AeRailContext` and `AeRailConnectors` suites
(retry, window end, offline, unmount, turn end), all in `just web-check`'s existing list.
It sits on the fork branch `analyticendeavors/omnigent@ae/context-probe-pending`, based
on the commit `main` pinned before it (`67b526b`).

`0092` (2026-10-18) shows a permanent context answer at once instead of retrying it.
With `0091` every unavailable answer that was not `offline` waited two minutes of quiet
retries, including ones retrying can never fix. The runner now adds `"final": true` to
the answer for a session whose runner reports another harness, and to a new
`ContextUnsupported` raised when the host has no `/proc` directory; a session whose
harness the runner has not loaded yet (no spec cached) answers `pending` instead of the
other-harness sentence. The web cache treats a final answer as settled (reused like a
snapshot, no retry), and `AeRailContext.tsx` shows its reason as a muted line with no Try
again. An answer without `final` from an older runner keeps `0091`'s rules. The other
unavailable paths stay as they were: not ready is `pending`, a lost runner is the
server's `offline`, and a real probe failure (unsupported Claude Code, malformed reply,
unsafe bridge root) is the plain error with backoff that the web retries within its
window. Tests: the runner suite (another harness is final, an unknown harness is
pending, a missing `/proc` raises `ContextUnsupported`) and the web `aeContextUsage`
and `AeRailContext` suites (a final answer shows at once and is never retried).

`0093` (2026-10-18) fixes the review findings on the rail's context loading. A turn-end
reload got the runner's cached pre-turn snapshot (its cache key does not change with a
turn) and stamped it fresh for three more minutes, so the route takes `?fresh=true`
(`openapi.json` regenerated), forwards `fresh` in the `context_usage` event, `app.py`'s
AE dispatch passes it to `handle_context_usage`, which then skips its cache; the web
sends it for a turn end, Refresh and Try again, and a fresh read asked for while another
is in flight runs right after it. The turn-end edge is handled once per shared entry
(the entry records the last status it acted on), so the Context and Connectors sections
together start one fresh probe per turn end, not two. Pending answers carry the real not-ready reason ("No
live Claude process for this session...", "The runner has not loaded this session
yet.") instead of a fixed "Claude is starting.", and the web shows it when the quiet
window ends. Once a window has run out, the three-minute reload no longer opens another;
a status change of the focused session, Refresh or Try again does. Only `pending`
answers retry quietly: a plain unavailable answer is a real failure inside the runner's
backoff, so it shows at once again. The last watcher leaving an entry clears `pending`
with the retry state, and an answer arriving with no watcher schedules no retry. Tests:
the runner suite (each pending reason, the unloaded-harness reason, a fresh read
skipping and replacing the cache), the route suite (`?fresh=true` reaches the runner),
and the web `aeContextUsage` and `AeRailContext` suites (fresh URLs, a queued fresh
read, no new window after it ran out, a refresh opening one, pending cleared on unwatch,
a plain failure shown at once, the runner's reason after the window).

`0094` (2026-10-18, server) lets `PATCH /v1/policies/{id}` change a global policy's
parameters, and checks parameters by building the factory. Reid asked to edit the
policies on Settings > Global Policies, and the route took only name, handler and
enabled. `UpdateDefaultPolicyRequest` gains `factory_params`: omitted leaves them, `null`
or `{}` clears them, anything else replaces them whole. The new
`omnigent/server/routes/ae_default_policy_params.py` checks them against the registry's
`params_schema` and then builds the factory once, because the schema covers key names
and JSON types only, and a stored policy whose factory raises breaks policy evaluation
for every session; a refusal answers 400 with the factory's own message and what to do.
The create route runs the same check, and a PATCH that changes only the handler checks
the stored parameters against the new one (both added after the dual review). Upstream
lines: 7 in `default_policies.py`, 3 in `schemas.py`, 7 and 8 in the policy store, and 13
in the regenerated `openapi.json`. A sync conflicts here only if upstream changes
`UpdateDefaultPolicyRequest`, `update_default`, the end of `update_policy` or the checks
in `create_policy`. Tests: `tests/server/routes/test_ae_default_policy_params.py` (replace
and read back, a value only the factory refuses, a wrong type, an unknown key, `{enabled}`
alone leaving the params, a missing id, a create the factory refuses, a handler change
the stored params do not fit, and one with the params cleared) and
`tests/stores/test_ae_update_default_factory_params.py`.

`0095` (2026-10-18, web) adds Edit to the Global Policies page. Each stored policy whose
registry entry is a factory with parameters gets a pencil (Tabler `pencil`, inlined)
beside its switch; it opens the add dialog in an edit mode, with no picker, the name
fixed, and every parameter prefilled from the stored `factory_params`, and Save sends
them through `0094`. The form's strings cannot carry every stored value back (an empty
list, numbers or objects in a list, an item with a comma; Codex's finding in the dual
review), so a field the admin did not change keeps its stored value exactly, as do stored
keys the form does not show; a changed field takes the dialog's value, and a cleared one
is left out, so the factory's default applies. The add, update and delete hooks throw
the server's reason through upstream's `apiErrorFromResponse`, so the dialog says why a
save or an add was refused. Policies from the server config's `policies:` block keep no
controls, as before. The logic is in `web/src/lib/aePolicyEdit.ts` (the form values, the
saved params and the editable rule) and `web/src/components/AePolicyEditButton.tsx`; the
page gains an optional `editing` prop on `AddDefaultPolicyDialog` (about 45 lines), and
`useUpdateDefaultPolicy` sends only the fields it is given, so the switch still sends
exactly `{enabled}`. Tests: `aePolicyEdit.test.ts`, `PoliciesPage.aeEdit.test.tsx` and
`useDefaultPolicies.aeEdit.test.ts`. Screenshots in `docs/screenshots/policy-edit/`.

`0096` (2026-10-18, web) slides the session rail in and out with the workspace panel, so
a swap under one pane at a time (`0083`) no longer shows both panes at full width for
300 ms. `AeSessionRailColumn` in `web/src/shell/AeSessionRail.tsx` takes the panel's
motion from upstream's `.workspace-panel-motion` in `index.css` (width, opacity and a
12 px translate over 300 ms, `ease-in-out`) as Tailwind classes: it grows from width 0
through `@starting-style`, and `useRailPresence` keeps it mounted for 300 ms after it
closes, with `data-state="closed"` and `aria-hidden`, its content held at 304 px so
nothing reflows. Making way for the workspace panel as it opens (`useOnePaneAtATime` now
returns whether the panel was closed the render before the clash), the rail stays docked
in its row; it unmounts at once when it floats even with no panel open, beside a files or
logs push panel (they snap to full width), and beside a panel already open. The closing
column gets the `inert` attribute (React 18 drops the prop), `RAIL_MOTION_MS` sets both
its `transition-duration` and the unmount, and `motion-reduce` drops the slide. Only that
AE file and its tests change, no upstream-owned file. If upstream changes the panel's
timing in `index.css`, `RAIL_MOTION_MS` must move with it.
Tests: the "AeSessionRail slide" block in `AeSessionRail.test.tsx`.

`0097` (2026-10-18) plays a token defrag in the composer tray while a conversation
compacts. The logic is in new files: `web/src/lib/aeTokenDefrag.ts` (the pill strip and
the cluster map as pure step functions, whether a compaction is on screen read from the
store's blocks, the finishes and failures the stream tap counts, the time estimate, the
per-device switch `ae.tokenDefrag`, the ring hold and pulse, and the before and after
counts) and `web/src/components/AeTokenDefrag.tsx` with
its `.css` (the strip, the Win98 window, the Settings > Chat switch, the marker's counts).
Upstream files change by a few lines each: `ChatPage.tsx` mounts the strip in the tray,
`ComposerContextRing.tsx` shows the held count and eases the arc, `StatusBlocks.tsx` and
`chatBubbleParts.tsx` pass the marker's item id and show its counts,
`settingsPortability.ts` lets the switch export, and `aeStreamObserver.ts` counts each
`compaction_completed` and `compaction_failed`, reading the ring's count on a finish
before the store replaces it. A run ends only on those events, because a reconnect swaps
the live blocks for history: a loading block that vanishes with neither stops the strip
quietly after 8 seconds, and a run that hears nothing for 15 minutes gives up and lets
the ring go. The completion and failure boxes in the open window wait for OK. A side
chat's markers show no counts, because its bubbles index a filtered copy of the child's
blocks. Tests: `aeTokenDefrag.test.ts`, `AeTokenDefrag.test.tsx` and a held-ring case
in `ComposerContextRing.test.tsx`, run by `just web-check`. The switch joins the AE
`AeChatSettingsSection.tsx` from `0085`. It sits on the fork branch
`analyticendeavors/omnigent@ae/token-defrag`, based on the commit `main` pinned before
it (`669ab44`). A dual review before the PR (Codex and Claude on the first version, then
Codex again on each round of fixes) raised 14 points: 12 are fixed here with tests, one is
accepted (a marker's counts last until a reload or reconnect brings it back from
history), and one is dismissed (the dated header comments match the fork's other AE
files).

`0098` (2026-10-18) puts the session rail's Absorbed and Unfiled counts side by side. The
Triage card gave each count a row of its own, which left the card taller than it needed
to be. The two counts now share one row (`AeTriageCounts` in
`components/aeRail/AeRailTriage.tsx`, a two-column grid); opening one shades it and lists
its lines full width below the row, one list at a time. AE files only. Tests:
`AeRailTriage.test.tsx`, run by `just web-check`.

`0099` (2026-10-18) picks the Claude account when forking a Claude session. Upstream
v0.17.0 retired the in-place agent switch that moved a running Claude session to another
of the host's Claude accounts, so the fork request takes `claude_account` (at most 64
characters, like the host's account names; an empty string is the default CLI login).
`routes/sessions/ae_fork_claude_account.py` (new) checks it before any row exists: a
Claude native target with no provider credentials bound to the agent or the session, the
source session's host owned by the caller, connected, not a sandbox and with no provider
credentials of its own, and the name among that host's detected Claude logins. The fork
then stamps `omnigent.claude_native.account` through its extra labels, which win over a
label copied from the source. An external fork binds its host after the fork request,
so the checks run against the source's host; binding elsewhere still needs the caller to
own that host, and a launch there fails when it lacks the account. In the web, the fork
dialog's run-config section shows a Claude account row (`shell/AeForkClaudeAccount.tsx`,
new) for a Claude native fork that stays on the source's host and not in a sandbox:
"Same as this session" (the default, nothing sent) or one of the host's logins. The row
hides on a host signed in with provider credentials, and a pick drops when its row hides
or the host stops listing it. Upstream hunks: `routes_core.py` (the check and the label
in the fork route), `schemas.py` (the field), `openapi.json` (regenerated),
`lib/sessionsApi.ts` (the request field) and `shell/ForkSessionDialog.tsx` (the row's
state and mount). Tests: `tests/server/routes/test_ae_fork_claude_account.py` (the
validator matrix, provider credentials, and the fork route with the real check: a
foreign host's 403, a switched target replacing the source's account, the name limit),
and the web `AeForkClaudeAccount`, `ForkSessionDialog` (a sandbox fork drops the row) and
`sessionsApi` suites, run by `just upstream-smoke` and `just web-check`.

`0100` (2026-10-19, web) lists the notes saved on the session in the rail's Triage card,
so an old Absorbed or Unfiled note stays readable. The card counted the lines from the
`ae.absorbed` and `ae.unfiled` labels but found their text only in the loaded messages,
so a note in an older message read "The lines are in older messages not loaded here."
The AE observer (`packages/ae_omni_policies/src/ae_omni_policies/observer.py`) now saves
each counted line as a label of its own, `ae.absorbed.<n>` or `ae.unfiled.<n>` (the
line's text after its token, clipped to 200 characters, the first 20 of each kind). The
card (`lib/aeRailTriage.ts` `aeTriageLinesWithNotes`, `components/aeRail/AeRailTriage.tsx`)
lists the saved notes in order, then each loaded line not saved yet, matching a loaded
line against the saved notes once, so two identical lines still list twice; it cuts a
loaded line the way Python's `labels.clip` does (code points, Python's whitespace) so a
saved note and its loaded line always match. AE files only. Tests: `aeRailTriage.test.ts` and `AeRailTriage.test.tsx`, run by `just
web-check`. It sits on the fork branch `analyticendeavors/omnigent@ae/absorbed-notes`,
based on the commit `main` pinned before it (`1843e4e`).

`0101` (2026-10-19, web) slides the session rail out only while the workspace panel slides
open. Since v0.18, upstream's `setRightPanelOpenAnimated` sets `rightPanelVisibilityAnimating`
(and with it `data-animate-visibility`, which carries `.workspace-panel-motion`'s transition)
for the header toggle, links and tabs, while a session restore goes through
`setRightPanelOpenImmediately` and snaps. `0096`'s rail slid out whenever the panel had been
closed the render before the clash, so a restore that snapped the panel open beside it showed
both panes and squeezed the chat (437 to 181 px for about 280 ms) when "Open the session rail
in every session" was off. AppShell now passes the flag as `workspace.animating` (one line in
the rail's props; the field is required on `AeRailWorkspace` in
`components/aeRail/aeRailActions.ts`, so a caller that builds the object without it fails to
compile), and `useOnePaneAtATime` records a make-way only while the panel is sliding open,
latched at the clash so the flag dropping at 350 ms does not cut the slide short. With no
`workspace` prop (stories) the rail leaves at once, which never squeezes. Tests: the
"AeSessionRail slide" block in `AeSessionRail.test.tsx` (a toggle open slides, a switch
restore leaves at once even when the switch render still carries the old motion flag, and a
slide finishes when the flag drops partway).

`0102` (2026-10-19) groups the sidebar's sessions by triage status and lets them be
ordered by hand. On 2026-10-18 Reid asked for sessions grouped by status with clear
separation, or sorted fully by hand. The patch builds on upstream's own view model
behind the `sidebar_grouping_ui` release flag (Grouping and Ordering in the Sessions
filter menu, kept per device), which the image now turns on (`ensure_features` in
`docker_entrypoint.py`). Grouping gains Triage: Blocked, Working, Review, No status,
Parked and Reference, each header with its status glyph, word and count
(`AeTriageTitle`, passed through a new optional `titleContent` on `SectionHeader`, so
every other header keeps its exact name), then a collapsed Archived group that loads
the newest archived sessions when opened, with Show more (`shell/AeArchivedGroup.tsx`,
fed by `useArchivedSessions`). Ordering gains Manual, which applies inside every list
(Sessions, Shared, each project folder, each group) and is off while searching and on
the Archived display. Ids the order does not hold come first in updated order, so new
sessions land on top, and a move changes only the moved row (`lib/aeSessionOrder.ts`).
A row moves by a drag onto a row of the same list or by the row menu's Move items
(`shell/AeSidebarRowOrder.tsx`; a submenu on desktop, flat items on a phone). Each row
is an `ae-order` drop target, and `aePointerWithin` drops targets from other lists, so
filing into a folder and pinning work as before and a drop across Triage groups does
nothing. `aeHandleOrderDrop` consumes every `ae-order` drop, because upstream's
`resolveSidebarDrop` turns an unknown target into "remove from project". The order is
one list per user from `GET` and `PUT /v1/ae/sidebar` (`hooks/useAeSidebarOrder.ts`):
refetched on focus, on reconnect and every 60 s while the page is visible; moves queue,
each saved on the latest order; a 409 replays the move once on the fresh order. A
zustand store (`shell/aeSidebarStore.ts`) holds it, and rows, folders and the drag
handlers read it through selectors. In Triage or Manual the list loads up to 100 rows,
the window the background refresh keeps current, so every session reaches its group.
Settings > Chat gains a Session list row with Reset manual order and Undo
(`pages/settings/AeSessionListSetting.tsx`, in `0085`'s section). Upstream hunks:
`lib/sidebarViewPreferences.ts` (the two unions and their arrays),
`pages/settings/AeChatSettingsSection.tsx` (the row), and about twenty small sites in
`shell/Sidebar.tsx`: the ProjectFolder memo, `groupOrder`, `allOrdered`, `groupTitles`,
the folder groups the session hotkeys walk, the hook calls, the drop check in
`handleDragEnd`, the collision filter, the group and Archived renders, `titleContent`
on `SectionHeader` and `ConversationSection`, the labels, the Move menu, and each row's
drop target and insertion line. Upstream main changed the same two unions after v0.18.0
(a Created grouping and ordering), so the next sync conflicts there. Tests:
`aeSessionOrder.test.ts`, `aeTriage.test.ts`, `useAeSidebarOrder.test.tsx`,
`aeSidebarStore.test.ts`, `Sidebar.aeSortModes.test.tsx` (a folder row dropped on a
sibling stays filed) and `AeSessionListSetting.test.tsx`, run by `just web-check`. A
dual review before the PR (Codex, and Claude `/code-review` at high effort) raised 10
points on this patch. Six are fixed here with tests: a just-created session's temporary
id no longer rides along with an order (the server refused every save until the id was
real), trimming past 500 ids keeps the moved row, Reset and Undo go through the
sidebar's save queue, a failed save's toast says every recent move was undone, and a
duplicated error helper and a dead branch are gone. Four are left: Move to bottom ranks
past the display window (scrolling reaches it), Triage counts cover every loaded row,
the dated header comments match the fork's other AE files, and a known limit: past 100
active sessions, an old session that loads late shows at the top of its list until it
is moved. The Archived header uses Tabler's `history` glyph, so it no longer looks like
Reference's, and the drop line (in the sidebar accent, with an end dot) is portaled one
layer above the drag preview, which sits on the pointer and covered it in the real app.

`0103` (2026-10-19) keeps the bulk-select bar inside a narrow sidebar. At the sidebar's
220 px minimum, "N selected" plus the four action icons ran past its edge. The pill is
now a container (`@container/bulk`). Below 13rem of content the label turns
screen-reader-only and a bare count shows in its place (`shell/AeBulkBarCount.tsx`), so
the label keeps its one text node, "N selected", for screen readers and tests. Measured
in the real app on 2026-10-19: nothing overflows from 220 px to 320 px, and the words
return at 250 px. Upstream hunk: two lines in `BulkActionBar` (`shell/Sidebar.tsx`).
Test: `Sidebar.aeBulkBar.test.tsx`, run by `just web-check`.

`0104` (2026-10-19) sizes the compact shortcut chips to their label. `CompactKbd` was a
fixed 16 px square (`size-4`), which fits a single glyph on macOS but not Ctrl or Alt on
Windows and Linux, so the New session row's hover chip read "CtrlAlt N". The chip now
keeps a 16 px minimum and grows with its label (`h-4 min-w-4`), which also fixes the
workspace menu's chips and tooltip shortcuts. At its real width the New session chip
covered the row's label in a narrow sidebar, so the row is a container
(`@container/new-session`) and the chip shows only from 13rem of content. Measured in the
real app on 2026-10-19: hidden from the 220 px minimum to 236 px, shown from 240 px with a
12 px gap to the label. Upstream hunks: one class in `components/ui/kbd.tsx`, two in
`shell/Sidebar.tsx`. Test: `Sidebar.test.tsx`, run by `just web-check`.

`0105` (2026-10-20) lets a drag or the row menu's Move items reorder sessions from any
Ordering, switching the sidebar to Manual. Right after `0102` went live, Reid picked
upstream's project sort ("Sort projects by: Manual order", in the Projects header's ⋯
menu) instead of the session ordering behind the filter icon, and session drag did
nothing, because rows took `ae-order` drops only in Manual. Rows now take them, and the
Move items show, wherever a list can hold an order: the `sidebar_grouping_ui` flag on, no
search, not the Archived display. Outside Manual, a move switches this device's Ordering
to Manual through upstream's `updateView` (passed to `useAeSidebarView` at its one call
site), reads the saved order fresh, then queues the move with the list as it was shown as
its seed, so only the moved row changes place; when another device already saved an order,
that order wins and the move lands on top of it. Moves made while the read is out chain
behind it and apply in the order they were made; leaving Manual first drops them, and a
failed read drops them with the usual error toast. A toast says where to switch back.
Sessions the order does not hold: one created after the newest ranked session goes on top,
as before; an older one goes below the ranked rows, so the older pages Manual loads after
the switch stay below what was arranged. That retires `0102`'s known limit (an old session
loading late showed at the top). The rule runs in both `aeApplyManualOrder` and the
materialize step of `aeMoveInOrder`, so a move still changes only the moved row. Past 500
loaded sessions, the saved order's cap, Move to bottom can leave older unranked rows below
the moved one; Manual loads 100 rows, so that takes a long scroll. A failed read also
drops every move queued behind it. Upstream hunk: the `useAeSidebarView` call's arguments
in `shell/Sidebar.tsx`. Tests: `aeSessionOrder.test.ts`, `useAeSidebarOrder.test.tsx`,
`aeSidebarStore.test.ts` and `Sidebar.aeSortModes.test.tsx` (flag off and the Archived
display show no reorder; a search hides it in the hook; a second move during a slow read
applies after the first; leaving Manual first fires nothing later; a failed read saves
nothing; older rows loading after the switch land below), run by `just web-check`.

`0106` (2026-10-19) lets an extension open a Databricks link through
`navigation.openExternal`. The Admin Center's Databricks section (PR
https://github.com/analyticendeavors/omnigent-ae/pull/138) offers "Open in Databricks" for
the workspace and each SQL warehouse, which the allowlist refused, so the page fell back to
Copy. `isAeDatabricksLinkUrl` in `lib/aeExternalUrls.ts` admits exactly two shapes on any
one-label `<workspace>.cloud.databricks.com` host (Databricks on AWS), https with no
userinfo, port, query or fragment: the workspace root, with or without its slash (the
server sends it without), and `/sql/warehouses/<16 lowercase hex>`. Any workspace means any
extension holding the navigation permission can open another tenant's workspace or the
account console, the same reach the GitHub allowlist (P25) has over any repository; pinning
it to the configured host would need the host to hand the URL out. A punycode label
(`xn--`) is refused so a lookalike name cannot show as Unicode, and Azure Databricks hosts
are not admitted. Upstream hunk: the allowlist test in
`navigation.openExternal` (`extensions/services/useExtensionHostServices.ts`). Tests:
`aeExternalUrls.test.ts` and `useExtensionHostServices.aeGithub.test.tsx`, run by
`just web-check`. Their fixtures use the placeholder host
`dbc-00000000-0000.cloud.databricks.com`. They first carried AE's real workspace host,
which a follow-up patch replaced on 2026-10-21 (pre-publication audit findings A4-codex-01
and A6-claude-18); since the old host still sat in this patch's text, the series was
rewritten the same day to fold that follow-up into this patch (finding RS-codex-01), and
every later patch moved down one number.

`0107` (2026-10-19) shows a bullet beside each note in the session rail's Triage card,
under Absorbed and Unfiled. Each saved note was already its own `<li>` (patches `0098` and
`0100`), but Tailwind's preflight strips list markers, so two notes in a row read as one
paragraph. `AeTriageLines` in `components/aeRail/AeRailTriage.tsx` adds `list-disc` with a
muted marker; the "more in older messages not loaded here" line is `list-none`, since it
describes the list rather than being a note. Test: `AeRailTriage.test.tsx`, run by
`just web-check`.

`0108` (2026-10-19) shows Claude model versions in both model pickers and lists the older
versions the subscription still serves under "Previous models". The host's catalog names
each family bare ("Opus", "Sonnet", "Haiku", "Fable"), so neither the New chat dialog nor an
open chat's model menu said which version a pick runs on, and nothing older than the latest
could be picked. `lib/aeClaudeModels.ts` holds the known ids (Opus 5.5, 5, 4.8, 4.7; Sonnet
5.5, 5, 4.6; Haiku 5.5, 4.5; Fable 5.1, 5; each checked with `claude -p --model <id>` on
2026-10-19). Per subscription row, it relabels a bare family label with its version (a `[1m]`
row reads "Fable 5.1 (1M context)"; a label that already says more is kept) and adds the
older ids of each family the subscription serves. Only the two pickers apply it: the shared
host catalog query in `useHosts.ts` still returns the raw rows, so harness defaults, project
settings, fork, scheduled tasks and the team control are unchanged. The New chat dialog (when
the Claude harness is selected) and `ChatPage` put the older rows in a "Previous models"
submenu (`components/composer/AePreviousModelsSub.tsx`, with an optional `trailing` slot on a
composer section in `ComposerConfigSections.tsx`), and both decide what is previous with
`aeSplitPreviousBy`. An open chat's model chip reads the versioned rows too ("Opus 5.5").
`ChatPage`'s `claudeVersion` now uses the shared parser, which also fixes a dated id such as
`claude-opus-4-20250514` reading its date as the minor version. When a new model ships, add
its id to the list. Tests: `aeClaudeModels.test.ts`, `useHosts.test.tsx`,
`ChatPage.composer.test.tsx` and `NewChatDialog.test.tsx`, run by `just web-check`.

`0109` (2026-10-20) sorts sessions by their status icon under Ordering: Status. Reid
picked Ordering: Status and found a session he had checked as Review between two Working
ones: upstream's Status ordering sorts by live activity (`statusBucket` in
`shell/sidebarNav.ts`: an approval waiting or a failure, unread, running, draft, idle), so
every idle session counted as done and fell by last update. The `ae.applyOrder` call now
wraps both branches of `allOrdered` in `shell/Sidebar.tsx`. Under Status it sorts the
upstream order by the shown status (`aeSortByTriage` in `lib/aeTriage.ts`, stable, in the
Triage groups' order), so the order is the icon first, then live activity, then newest
first, and rows without an icon keep upstream's order. Project folders take the icon order
through `useAeFolderOrder`, newest first within one, since the folder has no live-activity
buckets. The icon is held while the pointer is in the list, sharing Triage's frozen map,
which the store now publishes with the Status flag. Like Triage, Status ordering loads the
list's further pages up to the 100 rows the background refresh keeps current, so a blocked
session on a later page sorts to the top at once. Search results and the Archived display
keep upstream's order, as for Manual. Upstream hunk: the `allOrdered` expression and its
comment in `shell/Sidebar.tsx`. Tests: `aeTriage.test.ts` and
`Sidebar.aeSortModes.test.tsx` (Sessions by icon, then live activity, then newest; a
folder by icon; Updated unchanged), run by `just web-check`; upstream's own Status
ordering test still passes, since its sessions carry no icon.

`0110` (2026-10-19) reads a dead permission hook's approval-wait marker as stale before the
TTL. A deploy SIGTERMs the runner while a PreToolUse permission hook is parked (here the
policy relay answered 502 during the deploy and the hook failed closed to "ask"), and the
hook dies before its cleanup clears its marker. The next runner read that marker as a
pending Claude prompt for the whole `APPROVAL_WAIT_MARKER_TTL_S` (420 s) and refused every
send with "Answer the pending Claude question or permission request before sending a
message", then delivered the buffered messages when the TTL ran out (seen 2026-10-20
03:20Z). A live hook refreshes its marker every 60 s, so once a marker is older than
`APPROVAL_WAIT_ORPHAN_GRACE_S` (90 s), `approval_wait_is_fresh` in
`harnesses/claude_native/bridge.py` probes the pid in its `<digest>.<pid>.wait` name with
`omnigent.inner._proc.process_alive` (Windows-safe; a zombie counts as gone) and treats a
gone process as stale. Younger markers are never probed, so a hook whose pid reads
differently from the runner (a launcher's pid namespace) is never misjudged. The TTL stays
the backstop for a reused pid or a name with no pid. Tests:
`test_approval_wait_marker_of_a_dead_hook_reads_stale_before_the_ttl` and
`test_approval_wait_marker_without_a_pid_falls_back_to_the_ttl` in
`tests/harnesses/claude_native/test_claude_native_bridge.py`.

`0111` (2026-10-20) lists Claude Code's own subagents in the rail's Agents section, running
and finished, where it used to show one running count that dropped a subagent the moment it
finished. Reid asked to see running agents with a short note on what each is doing, and a
separate Finished group confirming that earlier ones ran and the list loaded. A running row
shows the name the Agents tab uses (`childPrimaryLabel`), its elapsed time and one line:
"Waiting on you" while it holds a prompt, else its newest tool call's own description, which
`components/aeRail/aeRailSubagentSteps.ts` reads from `GET /v1/sessions/{id}/items` every 15 s
for at most six running rows (a subagent's newest message is only its prompt, since its work
lands as tool calls). Finished rows sit under a Finished toggle, closed until opened and
remembered in localStorage `ae.rail.agents.finishedOpen`, newest first, eight shown. Rows
the Agents tab's Clear hid stay hidden (`readAeAgentsClearedAt`, now exported from
`shell/AeAgentsSections.tsx`), and the times tick every 10 s between polls.
`fetchAeSessionContexts` now also returns every subagent row (`subagentRows`). Tests:
`AeRailAgents.test.tsx` and `aeRailSubagentSteps.test.ts`, run by `just web-check`.

`0112` (2026-10-20) keeps a Claude Code subagent's child session running until its parent
records its return. The claude-native forwarder posts `subagent.status` idle after any 5 s
lull in a subagent's transcript (`_SUBAGENT_IDLE_THRESHOLD_S`), and the server's child
summary reads idle as `completed`, so a live subagent flipped between busy and finished every
few seconds (sampled 2026-10-20: about 50 of 60 seconds read finished during a 2.5-hour
build). The sidebar, the Agents tab and the rail's Agents section (0111) all misread it; Reid
saw the rail show no agents, then "1 running" a minute later. `omnigent/server/
ae_subagent_liveness.py` holds the logic. The `subagent.status` route holds a lull idle for a
`claude-code-native-ui-subagent` child that carries a native id label and whose parent has no
completion marker yet (`_recorded_completion_status`), republishing its busy status with the
lull's `blocked_on`, background count and response id so those still reach the indicator.
`record_subagent_activity` publishes the child's end on the "returned" edge (idle when
completed or stopped, failed only when it failed, since the forwarder never resends idle); a
replayed completion publishes only when the child has no cached status, so a resumed subagent
is not flipped. Each held lull (re)arms a per-child timer for `CLAUDE_SUBAGENT_STALE_AFTER_S`
(60 minutes) that publishes idle through `_publish_status` if the child is still running with
no completion and no transcript update, so the cache, the parent's rollup and open panels all
settle. Known limits (2026-10-20 dual review): a subagent resumed after its first return
flickers as before; one quiet for over an hour inside a single long command reads finished
until its next step; timers live in memory, so a host death with no held lull leaves the
child running until the next server restart (a deploy restarts the server). Tests:
`tests/server/routes/test_ae_subagent_liveness.py` and
`test_subagent_idle_waits_for_claude_completion` in `test_subagent_status.py`.

`0113` (2026-10-20) folds the session rail's context category rows (used categories, the
autocompact buffer and free space) under their own Categories toggle inside the Context
section, with a count at its right end like the tool, memory, skill and agent lists. Reid
asked for it shut by default in a new chat: the fold state lives in
`AeContextCategoryRows`' open set and `AeRailContext` keys `SnapshotBody` by session id, so
a session always opens folded and stays as set until it is left or reloaded. The
deferred tool rows and the lists stay outside the fold. The toggle markup is now one
`FoldToggle` shared with the lists. Tests: `AeContextCategories.test.tsx` and
`aeRail/AeRailContext.test.tsx`, run by `just web-check`.

`0114` (2026-10-20) finishes 0113 on Reid's review: no detail rows show until Categories is
opened. The deferred tool rows (MCP tools and System tools, muted with a dash since they cost
nothing in the window until Claude loads them) move inside the fold after free space, and
the fold's count includes them. The Compact session button is now "Compact" and sits on the
countdown's line; the countdown takes `basis-0` so the row truncates the text instead of
wrapping the button. Tests: `AeContextCategories.test.tsx` and `aeRail/AeRailContext.test.tsx`.

`0115` (2026-10-20) tidies 0113 and 0114 after Reid's look and the dual review. The Categories
fold adds its bottom margin only while open and with lists below it, so a shut fold sits in
the lists' column with no extra gap. The countdown carries its text as a `title`, since a
narrow rail can truncate it beside Compact. `AeRailContext.test.tsx` gains a test that moves
the rail to a second session and back to the first (cached, so it draws without a reload):
the fold opened in the second comes back shut, which fails without the `SnapshotBody` key.

`0116` (2026-10-20) adds a status block to the sidebar's session hover card, so a session's
current activity shows without opening it. `SessionTooltipContent` in `shell/Sidebar.tsx`
takes the row's `sessionState` (the one its icon shows) and renders
`components/AeSessionHoverStatus.tsx` under the title. The block mounts only while the card
is open and reads one page of the session's newest items through `lib/aeSessionPeek.ts`,
again every 5 s and at once when the row's state or update time changes: the current turn's
newest step, labelled as the rail's Agents section labels it (0111), shown as "Now" until
its result arrives and "Last step" after; the question of an AskUserQuestion only while it
is the newest call and unanswered, else "Approve:" with the step a permission prompt holds;
the turn's start (the user's own message, by `itemsToBlocks`' rule, so harness and agent
notices never restart it); and the last reply and last error as separate plain lines, the
error shown only for a failed row. Waiting rows instead list up to two running agents with
their newest steps; a scheduled row only reuses its existing "Paused; ... wakes it" text and
fetches nothing. When the turn's start lies past the 30 items read, the head reads
"Working" without a time (2026-10-20 dual review: accepted, since a deeper read costs every
hover). Tests: `AeSessionHoverStatus.test.tsx`, `aeSessionPeek.test.ts` and one hover
case in `Sidebar.test.tsx`, run by `just web-check`.

`0117` (2026-10-20) is the host and picker half of shared Claude accounts, so a second
AE user's host can use company-paid Claude accounts that the admin manages, without
copying tokens between hosts. The server half is out of tree
(`ae_omni_policies/shared_accounts.py` and `shared_accounts_routes.py`). A host knows a
shared account only by its id, `shared:<name>`; host account names cannot hold a
colon, so a host's own "Team" and the shared "Team" never clash. The logic is the new
`omnigent/onboarding/ae_shared_claude_accounts.py`: `is_shared`, the host's descriptor
for a selected shared id, and `shared_account_env`, which at launch asks
`GET /v1/ae/claude-accounts/shared/<name>/token` with the runner's own server
credential (`_make_auth_token_factory`, the bearer its host gave it, and
`RUNNER_SERVER_URL`) and returns the launch environment, the token held nowhere else.
Upstream files change by a few lines each: `claude_accounts.py` (selecting a shared id
stores only the id as the active account; `_read` accepts it; `account_descriptors`
reports it; `account_env` and `account_label` hand shared ids to the new file; add and
rename refuse them with "Shared accounts are managed in Admin Center."), `ambient.py`
(Claude login detection counts a shared pick without fetching its token), and `0099`'s
`ae_fork_claude_account.py` (a fork may pick a shared account the source host does not
list, checked through `known_on_server`, a resolver the AE package registers). Web: the
new `web/src/lib/aeSharedClaudeAccounts.tsx` fetches `GET /v1/ae/claude-accounts/shared`
(names and labels only) and merges it after the host's own rows;
`useDetectedCredentials` in `useHosts.ts` calls it (four lines), `ClaudeAccounts.tsx`
shows the Shared tag (Tabler `users-group`, inlined) and no Rename on a shared row, and
`0099`'s `AeForkClaudeAccount.tsx` shows the tag in the fork dialog. Tests:
`tests/onboarding/test_ae_shared_claude_accounts.py` and a case in
`test_ae_fork_claude_account.py` (both in `just upstream-smoke`), and
`aeSharedClaudeAccounts.test.tsx` with new cases in `ClaudeAccounts.test.tsx` and
`AeForkClaudeAccount.test.tsx` (in `just web-check`). It sits on the fork branch
`analyticendeavors/omnigent@ae/shared-claude-accounts`, based on the commit `main` pinned
before it (`87e867b`). The SDK executor reads the account env on a worker thread
(`claude_sdk_executor.py`, one line), because a shared account's token is a network fetch
and would otherwise stall the event loop each turn (2026-10-20 dual review).

`0118` (2026-10-20) previews a pull request when the pointer rests on a chat link to it.
`WorkspaceFileLink` in `components/blocks/ChatMarkdown.tsx` wraps a link that
`githubPullRequest` recognises in `components/AePrLinkHoverCard.tsx`, a Radix hover card
(300 ms open delay). Its body mounts only while open and reads `fetchAePrCard` under the
merge card's query key (`["ae-pr", repo, number]`) with the card's 30 s poll as its
`staleTime`, so a pull request whose card is on screen opens with no fetch. It shows the
state pill in the merge card's colors (an unknown state falls back to a plain pill), repo
and number, title, author, additions and deletions, files changed, and `aeCiText` while
open. A failed read gets one muted line naming the cause: no GitHub token, 404 (no such
pull request or the token cannot see it), 401 or 403, else GitHub not answering. The link
is matched on origin and path, so a comment anchor still previews. The state colors are
copied rather than imported from `AePrCards.tsx`, which imports `ChatMarkdown.tsx` itself.
The link keeps upstream's URL `title` (dual review, 2026-10-20: the native tooltip can show
beside the card, kept because upstream's test and links with anchors rely on it). Tests:
`AePrLinkHoverCard.test.tsx`.

`0119` (2026-10-20) shows the effort a Claude session runs at when none was picked.
`_auto_create_claude_terminal` in `runner/native/orchestration.py` calls
`runner/ae_launch_effort.py` inside the trust-seeding `try`, so a cancelled or failed call
still releases the launch's routers. Unless the launch args carry `--effort`, it reads
`effortLevel` in Claude's precedence (`/etc/claude-code/managed-settings.json`, the
workspace's `.claude/settings.local.json` and `.claude/settings.json`, then
`settings.json` in the pane env's `CLAUDE_CONFIG_DIR`, else `$HOME/.claude`) and PATCHes
the label `ae.claude_effort_default` (`none` when no file names a level); a failed PATCH
only logs. The dual review (Codex and Claude, 2026-10-20) moved it from `reasoning_effort`
to a label: writing the pick field pinned the default as `--effort` on every relaunch,
could overwrite a pick made during launch, and ignored workspace settings and pass-through
`--effort`. Two upstream tests in `test_app_sessions_native_terminals_autocreate.py` that
count the launch's session PATCHes now skip this one. In the web, `lib/aeEffortDefault.ts`
reads the label from the `["conversations"]` cache (as the session chip reads `ae.status`;
null with no query client), and `SessionHarnessPicker` in `pages/ChatPage.tsx` checks the
pick or else that level and labels the row "Effort: High (default)", or "Effort: Claude
default" with no level, for a Claude session outside smart routing. Claude Code's built-in
per-model default is not known to OmniGent and is not guessed. Tests:
`tests/runner/test_ae_launch_effort.py` and `aeEffortDefault.test.ts`.

`0120` (2026-10-20) puts the check before the model name in the composer menu's
two-column model grid (`CompactConfigChoice` in `pages/ChatPage.tsx`, from 0062). The
checkbox item draws its check `absolute right-2`, which in a grid cell lands beside the
next column's model. The grid's items take `pl-7!` and `pr-2!` and move the indicator to
`left-1.5`; the `!` is needed because `.composer-agent-menu` in `index.css` sets item
padding outside Tailwind's layers. Verified in the real app against the live server (a
session on Opus 5.5 shows the check before "Opus 5.5"). No unit test: jsdom has no layout.

`0121` (2026-10-20) folds the phone session menu's Status and Move items under one row each.
On a phone the menu listed every Status value and every Move item inline under headings,
about 20 rows, and ran off the bottom of the screen. The new `shell/AeMenuFold.tsx` row
shows the label, the current value (Status shows the stored one with its icon) and a
chevron; tapping it shows or hides the items in a tinted block under it and keeps the menu
open (`onSelect` calls `preventDefault`). `AeStatusMenu.tsx` and `AeSidebarRowOrder.tsx`
use it in their mobile branches only; desktop keeps its side flyouts. Tests: the mobile
cases in `AeStatusMenu.test.tsx`, `Sidebar.aeStatus.test.tsx` and
`Sidebar.aeSortModes.test.tsx`, run by `just web-check`.

`0122` (2026-10-20) is the dual review of 0121. Status and Move each kept their own open
state, so opening both brought back the 20-row menu; one module-wide value in
`AeMenuFold.tsx` now holds the open fold, so opening one shuts the other, and a closed menu
resets it. The opened group scrolls into view (the menu scrolls once it runs out of room),
and the row names its group with `aria-controls`. The phone Move test checks Move's items
alone, then that opening Status shuts Move. Left as is: the value reads word then icon, as
in the mockup Reid picked; arrow keys do not open a fold (Enter and Space do), since the
fold is phone only. Both sit on the fork branch
`analyticendeavors/omnigent@ae/v0.18.0-mobile-menu-fold`, based on the commit `main` pinned
before them (`4875f69`).

`0123` (2026-10-20) predicts the user's next prompt after each finished Claude turn and shows
it as ghost text in the composer (https://github.com/analyticendeavors/omnigent-ae/issues/45).
Claude Code's own prompt suggestions appear only in print/SDK mode
(`--prompt-suggestions`, behind `CLAUDE_CODE_ENABLE_PROMPT_SUGGESTION`); the interactive TUI
neither draws nor records one, so `harnesses/claude_native/ae_prompt_suggest.py` (new) makes
a side call. `forwarder.py` gains a few lines: on the parent's `Stop` hook, once its idle edge
is posted, `schedule_prompt_suggestion` starts a background task (the hook pass never waits
on it); a parent `UserPromptSubmit` and a `/clear` or `/fork` rotation cancel it, and a new
`Stop` cancels the previous one. The task skips sub-agent sessions (one
`GET /v1/sessions/{id}`, cached), hooks older than two minutes, and turns whose transcript
tail still holds a tool call without a result (an AskUserQuestion or permission prompt), then
runs `claude -p --model haiku --safe-mode --setting-sources "" --settings
'{"disableAllHooks": true}' --strict-mcp-config --tools "" --disable-slash-commands
--no-session-persistence --output-format text --system-prompt ...` with the prompt on stdin,
an environment without `OMNIGENT_*`, `RUNNER_*` or Claude Code session variables, and
a per-call temporary dir as cwd and Claude config dir (see the review fixes below), killed after 15 s. Each flag was checked in
`claude --help` (2.1.305), and a manual run on the box (2026-10-20) with a project settings
file whose `Stop`, `SessionStart` and `UserPromptSubmit` hooks touch marker files proved the
isolation: plain `claude -p` fired all three and wrote a transcript under
`~/.claude/projects`; the side call's flags fired none and wrote nothing (`--safe-mode` alone
also stops the empty auto-memory folder `--setting-sources ""` leaves). `NONE` or a failure
posts nothing; an answer is cut to one line of 120 characters and posted as
`external_session_suggestion`. The server (`routes/_sessions/ae_session_suggestion.py` and
`util/ae_session_suggestion.py`, new; `common.py`, `routes_events.py`, `schemas.py`,
`openapi.json`) validates one non-empty line and publishes the transient
`session.suggestion`, never stored. The web keeps the latest per conversation
(`lib/aeSuggestion.ts`, new; `events.ts`, `sse.ts`, `aeStreamObserver.ts`, `chatStore.ts`,
`conversationState.ts`), drops one that lands while a turn runs, and clears it when a turn
starts or the session rotates. `ChatComposer.tsx` takes an `aeSuggestion` prop and draws
`components/composer/AeSuggestionGhost.tsx` over the empty textarea (the placeholder hidden,
the chip's space kept by a flex row); plain Tab fills it before the caller's key handler
runs, and Tab with no ghost shown reaches the caller as before. `ChatPage.tsx` passes the
store's suggestion only while no turn runs and the box takes input. Off unless
`AE_PROMPT_SUGGESTIONS=1` is in the runner's environment. Tests:
`tests/harnesses/claude_native/forwarder/test_ae_prompt_suggest.py` (a fake `claude` on PATH:
isolation flags and environment, timeout and kill, `NONE`, the skip rules, cancel on a newer
turn, the switch, the forwarder hook-up, a hanging call not blocking the hook pass) and
`tests/server/integration/test_ae_session_suggestion.py`, run by `just upstream-smoke`;
`aeSuggestion.test.ts`, `store/aeSuggestion.test.ts` and `ChatComposer.aeSuggestion.test.tsx`,
run by `just web-check`. It sits on the fork branch
`analyticendeavors/omnigent@ae/v0.18.0-prompt-suggestions`, after `0122`.

Review fixes folded into `0123` (2026-10-20 dual review, ten items, all approved). A
measured run of the side call's exact flags rewrote `~/.claude.json` and left a backup in
`~/.claude/backups`, a write race with every live session; each call now gets a fresh
`mkdtemp` dir (removed afterwards) holding its cwd and a private `CLAUDE_CONFIG_DIR`, which
receives only the `claudeAiOauth` entry of the credentials file (mode 0600, and only while
the token stays valid for ten more minutes, so the side call never refreshes and rotates
the token the live sessions hold) or nothing when `CLAUDE_CODE_OAUTH_TOKEN` is set; a real
call that way authenticated and left `~/.claude.json` byte for byte the same. The reply now
comes from the `Stop` payload's `last_assistant_message` (a new `ClaudeHookRecord` field in
`bridge.py`, three lines), since the hook can fire before the transcript holds the turn's
last lines; the transcript tail supplies the prompt, and the pending tool call skip applies
only without it. Both forwarder `CancelledError` exits (teardown and supervisor restart)
cancel the call, a post never follows a closed client, and cancel or timeout kills the whole
process group. A typed `UserPromptSubmit` posts the null clear when a suggestion was shown.
The call runs only on the default provider: no gateway, Bedrock, Vertex or Foundry variable
in the runner's environment, no `apiKeyHelper` or such `env` in the session's
`claude-settings.json`, and no host launch config or managed gateway. On the web the grey
text lets taps through (only the chip fills), Tab is ignored mid-composition, and Esc
dismisses it until the next suggestion. `external_session_suggestion` joined the audit's
transient set. A session on a named Claude account (2026-10-20, Reid's call) gets no call:
the account's token reaches only the terminal's environment, so a side call from the
forwarder would bill the host's default login. The check reads the bridge dir's
`claude_account`, which the terminal's statusLine shim rewrites from its own
`OMNIGENT_CLAUDE_ACCOUNT` before every render (empty for the default login); a missing file
also skips. The forwarder's environment cannot tell, since the launch blanks the inherited
name. No account token is ever copied into the call. Skipped: suggesting only while someone views the session, since presence
lives in the server's memory and reaches clients only on the session stream; the forwarder
would need its own stream subscription.

`0124` (2026-10-20) reads Claude Code's per-model effort levels for 0119's default effort.
Checked live after ae-v0.18.0.22: every relaunched session got `none`, because Claude Code
2.1.304 writes effort under `modelSettings[<model id>].effortLevel` and the box's
`~/.claude/settings.json` has no top-level `effortLevel`. `claude_settings_efforts` in
`runner/ae_launch_effort.py` returns a map: the top-level level under `"*"` and each model's
own level under its id, each key from the first source in 0119's precedence that sets it
(a model's own level in a lower file beats a top-level level in a higher one; Claude's
exact rule here is not documented, and the most specific setting wins). The label holds
that map as compact JSON, or `none`. In the web, `aeEffortDefaultFor` in
`lib/aeEffortDefault.ts` picks the level for `effectiveModel` (by id, by id without a
`[1m]` style suffix, or through the picker option whose id it is), else `"*"`; a plain
level from 0119's format still reads as `"*"`. The hook still returns the raw label string,
so `useSyncExternalStore` gets a stable value. After the dual review (Codex and Claude,
2026-10-20): both sides of the match go through `comparableModelId` (now exported from
`lib/codexNativeModels.ts`, one line) after mapping a picker alias to its wire model, so
`opus[1m]` and a stored `claude-opus-5-5` agree; before the session reports a model, the
picker's default model stands in; values that are not Claude efforts are ignored; and the
runner drops per-model entries (keeping `"*"`) until the JSON fits the server's 256
character label limit, which would otherwise cut it mid-object. Kept as is: Claude's
precedence between a higher file's top-level level and a lower file's per-model level is
not documented, so the more specific per-model level wins; upstream's
`read_user_effort_level` (CLI-created sessions only) still reads the top-level key. Verified with vite dev against the live
server and the new label injected into the session list response: "Effort: High
(default)" with High checked. Tests: `tests/runner/test_ae_launch_effort.py` and
`aeEffortDefault.test.ts`.

`0125` (2026-10-20) closes 0112's in-memory timer gap: a subagent that died before its first
quiet spell, or across a server restart, stayed Running in the rail forever, and its parent
read "Waiting on 1 agent". Seen live: child `c34092b1f6d44843979636f3a708e33e`
(`general-purpose:a20c86b13faa11d46`) died about a second after launch when a deploy
restarted the host at 20:28Z, and `GET /v1/sessions/{id}` still answered `running` hours
later. 0112 armed the 60-minute staleness timer only inside `hold_premature_lull_idle`, so a
child with no lull never got one, and the module-level `_stale_timers` dict empties on every
restart while the row's persisted `live_status` keeps saying running. Two hooks, the logic in
`omnigent/server/ae_subagent_liveness.py`: `note_claude_subagent_status` runs after the
`external_session_status` route's `_publish_status` (`routes/sessions/routes_events.py`, one
line) and arms the same timer on a Claude child's first running edge, leaving a pending one
alone; `start_stale_sweep` runs from the server lifespan (`server/app.py`, beside the metrics
task) as a background task. The sweep reads the sub-agent ids whose `live_status` is running
in one query on `omnigent_conversation_metadata` (capped at 500), loads them in one batched
`get_conversations`, and hands each Claude child to `settle_stale_claude_subagent`: quiet past
the cap with no recorded completion settles idle, the rest are re-armed for the time
remaining, a child with a completion is left alone. That check now takes the row it was given
and, with no cached status, reads the row's `live_status`, which is what readers see after a
restart. Best-effort: any failure logs a warning and startup carries on. It sweeps the default
workspace only (the box runs one). The query reaches the SQLAlchemy store's private `_session`
maker; a store without one sweeps nothing and logs a warning. After the dual review (Codex and
Claude, 2026-10-20): a child the status cache already knows when the sweep reaches it (a host
reconnect restored it first) only gets a timer, never a settle, so live evidence beats the
startup snapshot; the 500-row cap logs a warning when reached. Kept as is: a `waiting` child is
not swept (0112 never settled one either), and timers are not cancelled at shutdown (one event
loop per server process). Tests: `tests/server/routes/test_ae_subagent_liveness.py`
(first running edge arms the cap and settles a child that never lulls; a restart's leftovers
are settled, re-armed or left alone; a restored child is only re-armed; both warnings) and `test_running_status_arms_claude_subagent_cap` in
`test_subagent_status.py`.

`0126` (2026-10-20) fixes the flaky "applies the move on top of an order saved on another
device" case in `Sidebar.aeSortModes.test.tsx`, which failed CI on
https://github.com/analyticendeavors/omnigent-ae/pull/162 and
https://github.com/analyticendeavors/omnigent-ae/pull/168 and passed on a rerun. Root cause, in the test harness: a drop leaves dnd-kit's capture-phase `click`
listener on `document`, which stops every click, for 50 ms (it is removed with
`setTimeout(..., 50)`), and the suite's `afterEach` waits only 20 ms. The case clicks Move
to bottom right after the previous case's drag; a click inside that window never reached
React, so Radix never ran the item's select, the move never ran and the case waited for a
PUT that never came. Proof: with dnd-kit's 50 ms timer stretched to 2 s the case failed 4 of
4 with the listener live at the click (four other click cases failed with it), and before
the fix 8 of 10 drags returned with it still live, 0 of 10 after. The `drag` helper now
waits 50 ms after the drop, a timer that starts after dnd-kit's and so fires after it; no
product code changes. `Sidebar.dragOverlayPortal` and `Sidebar.pinReorder` drag the same
way and have not flaked; they are left as they are.

`0127` (2026-10-20) moves managing shared Claude accounts into Settings, Claude accounts,
beside the host's own logins. The new `web/src/shell/AeSharedClaudeAccountsGroup.tsx` draws
a Shared group: each shared account with its id and who added it, selectable for new chats
through the host's own `claude-oauth-select`, plus an admin's Rename, Remove (asking once)
and + Add. The Add form opens only from + Add and closes on Cancel or a successful add; the
token field is a password input cleared on submit. `aeSharedClaudeAccounts.tsx` gains the
full list read and the add, relabel and remove calls (`GET` and `POST
/v1/ae/claude-accounts/shared`, `POST .../{name}/label` and `.../{name}/delete`), each
refusal showing the server's own message. Admin is `getCurrentIsAdmin()` (the `/v1/me`
probe) or the list's `can_manage`. `ClaudeAccounts.tsx` changes by a few lines: the full
view skips shared rows in the host's list and renders the group last, refetching the host's
accounts after a change; the compact pickers are unchanged. Tests: new cases in
`ClaudeAccounts.test.tsx` (run by `just web-check`). It sits on the fork branch
`analyticendeavors/omnigent@ae/settings-shared-accounts`, after `0126`.

`0128` (2026-10-20) makes 0123's prompt suggestions work in every session: a Claude session
on a named account, and Codex sessions (Reid's call after
https://github.com/analyticendeavors/omnigent-ae/pull/164;
https://github.com/analyticendeavors/omnigent-ae/issues/45). The side call, its isolation,
login, provider check, sub-agent check, one-line trim, cancel, clear and event post moved
from `claude_native/ae_prompt_suggest.py` to `harnesses/ae_prompt_suggest_core.py` (new),
which both harnesses import: the call runs Claude whichever harness asked, so it belongs to
neither, and the Codex forwarder importing Claude's module would make the Claude file the
owner of shared code. The Claude module keeps the transcript reading, the bridge dir
account file and the `Stop` scheduling. Named accounts: 0123 skipped a session whose
`claude_account` named an account, since the forwarder's environment only has the default
login. Now `account_login_env` resolves the name with `claude_accounts.account_env`, the
function the terminal launch reaches through `account_env_for_launch` (`main.py`), and
passes the result (`CLAUDE_CODE_OAUTH_TOKEN`, with `ANTHROPIC_API_KEY` and
`ANTHROPIC_AUTH_TOKEN` blanked as the launch does) to `run_side_call` as `login_env`,
layered over the process environment; `copy_login` then sees the token and writes nothing.
An empty file keeps the default login; a missing file (no render yet) or a name this host
cannot resolve skips with a debug line that names the account and the error type, never
the token. Proven on the box: `account_login_env("AE Backup")` and one real
`claude -p --model haiku` with `CLAUDE_CODE_OAUTH_TOKEN` removed and `CLAUDE_CONFIG_DIR`
pointed at an empty dir answered; the same call without the account found no login. Codex:
`codex_native/ae_prompt_suggest.py` (new) notes each completed `userMessage` and
`agentMessage` per session and turn, and on a parent thread's live `turn/completed` whose
status edge is `idle` (posted by `_handle_terminal_turn_boundary_inner`, the same edge the
web reads as the turn's end) starts the shared call on the host's active Claude account,
resolved with `account_env_for_launch(account_name=None)` as a terminal started without a
choice would be. Codex sends a turn's `item/completed` events before its `turn/completed` on
one ordered stream, and the forwarder handles them one at a time, so the turn's text is
complete by then; a `turn.items` list in the notification fills in a missed item (the
app-server schema, generated from codex-cli 0.156.1, marks `items` as what the payload
currently holds). Skipped: child threads (their events reach the hook without forwarder
state), sub-agent sessions, interrupted or failed turns, a reply carrying `questions`, a
Plan turn whose implementation prompt is about to ask (checked before that prompt blocks
on the answer), a `completedAt` older than two minutes, and hosts whose Claude runs on a
gateway, Bedrock or Vertex. A `turn/started` cancels a call in flight (its `running` edge
already clears the composer, so no extra null post); a `/clear` rotation cancels it and
forgets the noted turns, and the forwarder's exit cancels it.
`forwarder.py` gains 29 lines: the import, one state field, and those five hook points.
Both forwarders run inside the runner, so `AE_PROMPT_SUGGESTIONS` reaches Codex through the
same `OMNIGENT_RUNNER_ENV_PASSTHROUGH` entry as Claude. Tests:
`tests/harnesses/claude_native/forwarder/test_ae_prompt_suggest.py` (a named account's
token reaches the fake `claude` by env with no login file in its config dir and never in the
logs, the default account keeps the default login, an unresolvable or unknown account
skips) and `tests/harnesses/codex_native/forwarder/test_ae_prompt_suggest.py` (new: one call
and one event per turn end, the `turn.items` fallback, each skip, a new prompt cancelling
the call and killing its process, the clear, the forwarder's exit, the switch), run by
`just upstream-smoke`. It sits on the fork branch
`analyticendeavors/omnigent@ae/v0.18.0-prompt-suggestions-all`, after `0127`.

Review fixes folded into `0128` (2026-10-21 dual review, Codex found nothing, eight Claude
items, all approved). Codex calls run on the host's active account rather than always the
CLI's credentials file. `ANTHROPIC_API_KEY` joined the provider keys, so a raw API key
skips the call. `state.shown` is set before the suggestion's POST, so a cancel mid-POST
still sends the null clear. Named accounts resolve through `account_env_for_launch`, which
keeps the launch's provider-conflict check (the duplicate `OMNIGENT_*` strip went, since
`side_call_env` drops those). The Codex prompt reader reuses the forwarder's
`_user_message_text` and `_is_codex_skill_wrapper`, so a skill block with typed text keeps
the text, and the hook takes the posted edge's `turn_id`. The Claude side reads the account
name and the provider verdict once per forwarder, again only when `claude_account` changes;
the token is still resolved per call and never cached. The Codex notes skip child
sessions, a rotation drops them, and only a reply's last 2,000 characters (a prompt's
first 600) are kept. Skipped: retiring the `__all__` re-exports in the Claude module (out
of scope).

`0129` (2026-10-20) folds merged and closed pull requests under a message. Reid's
screenshot of a finished session showed three full Merged cards (about 550 px) below a
TLDR that already linked all three. `AeInlinePrCards` now reads each card through the
cards' own `["ae-pr", repo, number]` query (`useQueries`) and splits the list: open, draft
and not-yet-read pull requests render `AePrCardView` as before; merged and closed ones go
to the new `AeMergedPrFold.tsx`, one row with the count and numbers (the repo too when the
fold spans two), opening to one titled line per pull request, each of which opens its full
`AePrCardView`. The card still does the fetching; the fold's observer is enabled only for a
folded closed pull request, polling it every 30 s so a reopened one comes back as a card.
A pull request merged from a card on this page stays a card, with its after-merge note:
`ConfirmMerge`'s mutation now has the key `["ae-pr-merge", repo, number]` and
`gcTime: Infinity`, and the chat reads the successful ones with `useMutationState`, which
survives the transcript's virtualized remounts. One merged anywhere else folds on the next
poll. The GitHub panel's cards are otherwise unchanged. Tests: six cases in
`AeInlinePrCards.test.tsx`.

`0130` (2026-10-20) shows a failed Claude Code subagent as Failed instead of finished, and
makes the rail's Agents section and the Agents tab read the same rows. Seen live in session
`1b35117685d64c6794969f1d9db317de`: child `a1e6cf550ae04561a823282fc1b587dc`
(`general-purpose:aaaf60903a50f23e5`) stopped at 23:49:07Z with three image reads that never
returned, the parent recorded `session.subagent.returned` with status `failed` at 23:50:54Z,
and after the deploy restarted the server at 00:15:26Z the rail read "3 finished". Three
causes. Server: `_child_session_summary_from_conversation` (`routes/_sessions/helpers.py`)
read only `_session_status_cache`, which a restart empties, so the row's persisted
`live_status` of `failed` was lost and the child read idle; a cache miss now keeps a
persisted `failed` (two lines). Web: `splitAgentSections` (`shell/aeAgentsPaneModel.ts`)
filed failed rows under Finished; it now returns them as `failed` (`isChildFailed`), and
`AeAgentsSections.tsx` and `AeRailAgents.tsx` show them in an always-open Failed group with
Tabler's alert-triangle icon. The rail's summary is `aeRailAgentsSummary`, "1 running · 1
failed · 2 finished" with zero parts left out. The tab's Clear also clears failed rows. Web:
the rail read subagents from its own 60 s `fetchAeSessionContexts` poll while the tab reads
`useChildSessions`, which the session stream patches live, so after a resume the tab showed
Running a minute before the rail; the rail now reads `useChildSessions(sessionId)` filtered
by `isNativeSubagent` (now exported from `aeSessionContext.ts`). Left as is: elapsed time
still counts from `created_at`, since a SendMessage resume is not mirrored into the child's
items and the summary carries no run start; showing run time needs a new summary field.
Tests: `test_child_summary_keeps_a_persisted_failure_after_a_restart` in
`tests/server/routes/test_ae_subagent_liveness.py`, and failed-row cases in
`AeRailAgents.test.tsx`, `AeAgentsSections.test.tsx` and `aeAgentsPaneModel.test.ts`, run by
`just web-check`.

`0131` (2026-10-20) makes the merged pull request row from `0129` easier to read. Reid's
screenshots: the row's "#173 #172 #175" were plain text, and the opened list's lines did not
look like they opened. Each number is now a link (`target="_blank"`, like the line's
open-external icon) wrapped in `AePrLinkHoverCard` from `0118`, so hover or focus shows the
same preview as the chat's inline pull request links and reads the same `["ae-pr", repo,
number]` query. A link cannot sit inside a button, so `AeMergedPrFold`'s header is now a div:
the toggle button holds the caret, icon and count, and its `::after` covers the row, so a
click anywhere but a number still opens the fold; the numbers sit above it and stop
propagation. Each opened line's button now starts with the header's `Chevron` (Tabler
chevron-right at `size-4`, turned a quarter while that line's card is open). Tests: two cases
in `AeInlinePrCards.test.tsx`, with the existing row text checks now read from the fold.

`0132` (2026-10-20) lets the prompt suggestions from `0123` and `0128` offer alternatives. Reid
asked how to pick between two or three good guesses and chose, from three mockups, cycling in
place with Copilot's Alt+] and Alt+[, plus a Settings field to rebind them. The side call's
system prompt now asks for up to three clearly different guesses, best first, one per line, in
the same single Haiku call. `clean_suggestions` (replacing `clean_suggestion`) strips list
markers and quotes, stops at a `NONE` line, drops bracketed asides and near duplicates
(difflib ratio 0.85 or more, unless the differing words carry a digit or an identifier, so
"Merge PR 121" and "Merge PR 122" both stay), and keeps three at most. The event carries
`data.suggestions` and still sets `data.suggestion` to the top guess, so a page loaded before
the deploy keeps working; the route accepts either, validates 1 to 3 lines of 120 characters,
and `SessionSuggestionEvent` gains `suggestions`. On the web the store holds the list
(`aeSuggestion: readonly string[] | null`), `useAeSuggestionCycle` holds the shown index, and
the composer's key handler steps it with the chord from `lib/aeSuggestionKeys.ts`, matched on
`event.code` because Option on a Mac turns the brackets into quote characters; the step calls
`stopPropagation` so no window hotkey on the same keys fires too. The chip reads
"Tab to use | 1/3 >", a tap on the counter steps forward (the phone path), and the grey text
wraps instead of truncating, with the textarea held at least as tall as the wrapped text and
never below its own `min-height`. Settings > Chat gains Suggestion keys: Change records the
next combo from a window capture listener (Safari does not focus a clicked button), refuses a
bare key, Tab, Esc and Enter, the other action's combo and every app hotkey in
`hooks/use*Hotkey.ts` plus Cmd/Ctrl+A, C, V, X, Y and Z, and saves per browser in
`omnigent:ae-suggestion-keys`; Reset puts the other action back too when it holds this one's
default. Review fixes folded in (2026-10-20 dual review: Codex two, Claude ten, all ten
applied): the `NONE` stop and asides, the identifier rule for duplicates, the textarea floor,
the window listener for recording, the reserved list (Ctrl+Alt+V had toggled dictation), the
Reset collision, one shared storage listener, and an in-memory hold when storage is blocked.
Tests: new cases in `test_ae_prompt_suggest.py`, `test_ae_session_suggestion.py`,
`ChatComposer.aeSuggestion.test.tsx`, `aeSuggestion.test.ts` and `store/aeSuggestion.test.ts`,
and new `aeSuggestionKeys.test.ts` and `AeSuggestionKeysSetting.test.tsx`.

`0133` (2026-10-20) stops a closed subagent from counting toward its parent's hourglass badge
(`active_child_count`, `0074`). Seen live: parent `a2e393e9f4ad4f6caac2e2b3f7ae3b1e` read
`active_child_count: 2` for hours; its children `2cfcded225104966a7fffa1e915b385b` and
`ee95b92ff2444b1eba7a458485d05d58` had been closed with `sys_session_close`, whose REST path
PATCHes `archived: true`, while `_session_status_cache` still read running, and with their
runners gone no idle edge ever came. A deploy emptied the cache and the count, but the stored
`live_status` still read running. Root cause: archiving a session left its busy mark in
place, and `session_active_child_count` did not skip archived children (the child ids come
from `list_child_conversation_ids_by_parent`, which includes them). Two parts. The session
list and the updates stream (`routes_core.py`, three call sites) now pass
`include_archived=False` to that query, a new keyword (default `True`, so child recovery and
the stop sweep still see every child) on the store and its interface; the `archived` flag is
on the `conversations` row the query already reads, so the badge needs no extra query and is
right on every replica and after a restart. A first version kept an in-memory set of
archived ids instead; the dual review (Codex and Claude both) showed it drifts across
replicas, misses a cross-replica Undo and only grows, so the stored flag won. The status
roll-up reads the same filtered ids, so a closed child no longer makes its parent's row read
running either. `settle_archived_session` in `routes/_sessions/ae_session_activity.py`, one
call in `_archive_stop` (`orchestration.py`, after the best-effort stop, past the Undo
grace), publishes idle for a session still marked running or waiting, reading the stored
`live_status` when the cache has no entry; `_publish_status` also persists the stored
status, and the helper cancels `0125`'s staleness timer. `0125`'s settle helpers are
Claude-subagent-only (they wait for a completion marker), so this reuses their publish and
timer pieces rather than them. `failed` is left alone. Left as is: a scheduled run cut off
by an archive is recorded succeeded, as upstream's own intentional stop records it; a
restart inside the 8 s Undo grace loses the teardown, so that child's stored status keeps
reading running (it no longer counts); the two rows seen live keep their stored `running`
for the same reason. Tests: five cases in `tests/server/routes/test_ae_session_activity.py`
(a closed child leaves the count while an open running sibling stays, and Undo restores it;
an archived child with an approval does not count; the query keeps archived children unless
asked; the settle only moves running and waiting; `_archive_stop` settles a closed running
child).

`0134` (2026-10-20) shows the effort a Claude session actually runs at and lays the chat
composer's model menu out as one column. Reid: the menu said "Effort: Claude default" and he
needed the level; the chip under the composer should carry it beside the model as desktop
Claude Code's "Opus 5.5 Extra" does; and the whole popover felt clunky next to desktop's
list. He picked the desktop-list layout from three mockups. `lib/aeEffortDefault.ts` gains
`CLAUDE_CODE_DEFAULT_EFFORT`, Claude Code's own per-model default read from the model
catalog inside its binary (2.1.306; Opus 5.5 medium, Fable 5.1 high, Sonnet 5.5 medium,
Haiku 5.5 medium, Opus 5 and Sonnet 5 high, Opus 4.7 xhigh), `aeEffortResolved` (the
runner's settings-file level from `0119`/`0124` first, tagged `settings`, else the table,
tagged `claude`), `aeClaudeEffortLabel` (Claude Code's words: Low, Medium, High, Extra, Max;
Pi and Codex keep `normalizeEffortLabel`'s xHigh) and `aeEffortSourceTag`;
`aeEffortDefaultSuffix` and "Claude default" are gone. `ChatPage.tsx`: the Effort row reads
"Effort" with the level right-aligned, the submenu checks the resolved level and tags it
"default" or "settings", the chip's effort slot takes the resolved level when nothing is
pinned, and the hover summary's Effort row shows it with its source. `CompactSessionChoices`
drops the Models header and the two-column grid: primary models are rows with the check on
the left and a muted digit on the right, the digit picks the row while the menu is open
(a keydown handler on the list; no row's text starts with a digit, so Radix typeahead never
competes), and Previous models sits under Effort. New `shell/AeClaudeAccountSubmenu.tsx`
replaces the compact `ClaudeAccounts` grid in the menu: one "Account  AE Default ›" row whose
submenu lists the host's logins and the shared ones (Shared tag), selecting through the same
`claude-oauth-select` write; `ClaudeAccounts`' compact branch stays for `NewChatDialog`.
`AeFastMode.tsx`: the "Choose a supported Opus model" sentence becomes the disabled row's
tooltip so the row is one line; pending, failed and unknown states keep their second line.
Out of tree, `scripts/claude-effort-defaults.py` prints the table from the installed Claude
Code binary, so a Claude Code bump is one command and a diff. Review fixes folded in
(2026-10-20 dual review: Codex two, Claude ten, eight applied): the digit handler sits on
the whole menu (`lib/aeModelShortcut.ts`), skips portaled submenus and the row already
checked, and so works from the Account or Effort row too; the Account row says it sets the
login for new chats (tooltip and a first line in its submenu); a host whose read returns no
Claude login reads "Unavailable" with the alert, not "Loading…"; the hover summary words a
pinned effort the same way as the chip; the Fast mode row's reason stays in its
screen-reader text; the row class is shared from `lib/aeMenuRow.ts`; and the built-in
default reuses the settings path's alias resolution. Left as is: the Models header and
separator (the approved desktop-list design), null `env_var` keys (upstream's own keying),
and the `CompactSessionChoices` rewrite staying in `ChatPage.tsx` (it is the layout change).
Tests: `aeEffortDefault.test.ts` (table, resolution order, words, tags), new
`AeClaudeAccountSubmenu.test.tsx`, new cases in `ChatPage.composer.test.tsx` (resolved
level and tag, settings tag, pinned pick winning, digit pick from the Effort row and not
from a submenu or the current row, numbered rows) and `AeFastMode.test.tsx` (tooltip). It
sits on the fork branch `analyticendeavors/omnigent@ae/picker-effort`, after `0132`.

`0135` (2026-10-20) removes a personal Claude login from Settings. Reid could rename his
extra "AE Backup" login but not delete it: upstream had no delete at any layer. A new
credential kind, `claude-oauth-delete`, runs through the same four seams as rename, one
line each: the server's kind allowlist (`routes/hosts.py`), the host's frame handler
(`host/connect.py`), the web's `StoreCredentialInput["kind"]` (`hooks/useHosts.ts`), and a
branch in `_change_account` (`onboarding/claude_accounts.py`) that refuses `""` ("The
default CLI login cannot be removed."; shared ids are already refused by `0117`'s guard),
deletes the token (`secrets.delete_secret`, a no-op when absent), drops the name and its
label, and resets `active` to `""` when the removed login was the one new chats used; the
active-pointer write skips delete as it skips rename. The state file is written to its
temp first and the token deleted after; the record is replaced only once `load_secret`
confirms the token is gone (a locked keyring makes `delete_secret` fall back to the file
and leave it), else the temp goes and the login stays with "Could not remove the token on
this host" (2026-10-20 dual review: Codex one, Claude eight, five applied). The web side is
the new `shell/AeClaudeAccountRemove.tsx` (the Remove button and the one-line confirm, which
names the host, says running chats keep their token until they restart, and, for the
selected login, that new chats use the Default CLI account); `shell/ClaudeAccounts.tsx`
changes by the import, one state and two JSX calls, shows it on every personal login in the
full view (not the default CLI login, not shared rows, not the compact picker), and closes
the confirm when Rename opens. Left as is: the `elif` chain and the repeated message
handlers (upstream's shape). Tests: `tests/onboarding/test_claude_accounts.py` (removes the
name, label and token and keeps the rest; the active login falls back to the default; `""`,
unknown and shared refused; the login stays when the token cannot be removed; the frame
round-trip parametrized over rename and delete), now in `just upstream-smoke`, and new cases
in `ClaudeAccounts.test.tsx` (Remove shown only where it should be, one confirm then the
delete kind, Cancel sends nothing, a refusal keeps the confirm open, Rename closes it and the
selected login's confirm names the fallback). It sits on the fork branch
`analyticendeavors/omnigent@ae/account-delete`, after `0134`.

`0136` (2026-10-20) makes assistant text fill the chat column so replies, user bubbles,
merged PR rows, inline PR cards and the composer share one right edge. `index.css` sets
`--chat-prose-width: 100%` on `.is-assistant` (was `80ch`; the 120ch rule at the 64rem
container tier stays), `AePrCards.tsx` and `AeMergedPrFold.tsx` use `w-full` instead of
`w-xl max-w-full`, and `chatLayout.ts` drops the `min-[1921px]` tier so the column holds
48rem until the 2561px tier. Checked by eye at 2020px and 1440px on a real session. No test
asserted the old values: the composer test builds its classes from `CHAT_COLUMN_WIDTH`.

`0137` (2026-10-20) adds the action dock: one line above the composer's workspace bar that
gathers what waits on Reid in a chat (designs E1 collapsed, D3b opened, D4 deploy tracking,
approved by Reid the same day). Questions come first, then pull requests ready to merge, then
omnigent-ae merges not yet on the box; oldest first within a kind. Collapsed, the most urgent
item has its button ("Jump to it", Merge, or the Admin Center link and a dismiss) and every
other kind gets a tinted badge that opens the list at that kind; on a phone the badges are an
icon and a count. Opened, it is a "N waiting" header and a 30 px line per item, four before it
scrolls; open or closed is kept per session in localStorage. Merge opens the PR card's own
`ConfirmMerge` (method choice, then Confirm) inside the dock, never a one-click merge. An item
hides while its own card is on screen: the inline PR card and the merged row's numbers carry
`data-ae-pr="owner/repo#n"`, the floated question cards `data-ae-question`, and an
IntersectionObserver (re-scanned by a MutationObserver, since the feed is virtualized) reports
which are visible. Nothing waiting draws nothing.

The logic is in new files: `lib/aeActionDock.ts` (the pure model: items, order, badges, deploy
lifecycle and text), `lib/aePrDeploy.ts` (the deploy query both readers share),
`hooks/useAeActionDockItems.ts` (data and the on-screen hook), `components/AeActionDock.tsx`
and `components/AePrDeployStatus.tsx`, plus `lib/aeActionDockIcons.ts` (Tabler paths). Pull
requests come from every finished reply in the transcript (`aePrUrlsIn`, cached per block),
only those the session tracks (unlike the inline cards, an unknown tracked list gives no
items, so a link quoted from another session never offers Merge), one per pull request
however its URL is cased, and read through the cards' own `["ae-pr", repo, n]` query and
fetcher; the dock polls a card only while no feed card
observes it. Pending questions are the ones `Transcript.tsx` floats to the bottom
(`collectPendingElicitations` and the Codex path), published per session by
`useAePublishDockQuestions`. A deploy item needs this page to have seen the pull request
waiting (kept per session in localStorage with the time, dropped after four hours, so a
reload mid-deploy keeps it and an old one is not announced as new); it then shows "live in
ae-vX" for 60 s and clears. The on-screen scan reruns only when a mutation adds or removes a
card, not on every streamed token. Outside a query client (the composer's isolated tests)
the dock renders nothing. Deploy state polls every 60 s only while waiting.
Upstream files change by little: `ChatPage.tsx` the import and the mount, `Transcript.tsx`
the import, the publish call and two data attributes, `AePrCards.tsx` the `ConfirmMerge`
export and the inline card's attribute, and `AeMergedPrFold.tsx` the deploy status before the
repo name ("waiting for deploy" or "live in ae-vX", omnigent-ae only) and the chips'
attribute. The server side is out of tree: `GET /v1/ae/prs/{owner}/{repo}/{n}/deploy`
(`ae_omni_policies/pr_deploy.py`, `github.compare_status`): a compare cache and lock per
(tag, merge commit), the release read skipped for another repo or no token, and `unknown`
for a pull request merged into a branch other than the default (stacked), whose merge commit
never reaches a tag. Review before the first push (2026-10-20, Claude code review, ten
findings, eight applied; left as is: sharing the tracked-list filter with
`AeInlinePrCards.tsx`, since the dock now deliberately differs, and the docs pointer, which
this entry answers). Tests: `aeActionDock.test.ts`,
`AeActionDock.test.tsx` (collapsed and opened rendering, badges, the merge confirm and
Cancel, the deploy line and dismiss, hiding while a card is on screen),
`AePrDeployStatus.test.tsx`, and `tests/test_pr_deploy.py` in the policies package. It sits
on the fork branch `analyticendeavors/omnigent@ae/action-dock`, after `0135`.

`0138` (2026-10-21) shows the action dock's merge confirm whole when it opens from a line in
the opened list. The list keeps four 30 px lines in a 120 px scroll area, and `ConfirmMerge`
opened inside it was clipped: the method choice and the confirm button sat below the fold of
that small area (the screenshots of the built dock, 2026-10-20). In
`components/AeActionDock.tsx` the list now drops `max-h-[120px]` and its inner scroll while a
confirm is open for one of its items, and a layout effect scrolls the confirm into view
(`block: "nearest"`); Cancel or a finished merge restores the cap. The collapsed lead line was
never affected. Test: a new case in `AeActionDock.test.tsx` (cap lifted while the confirm is
open, back after Cancel). It sits on the fork branch
`analyticendeavors/omnigent@ae/dock-confirm-clip`, after `0137`.

`0139` (2026-10-21) shows the session title in the chat header of the iOS and Android
apps. Reid's phone screenshot: the header held only the sidebar button, the slots chip and
the menu. Upstream's `index.css` hid every breadcrumb segment but the parent-session link in
`[data-ios-native]` and `[data-android-native]`, on the view that the native chrome names the
session; it only switches servers. The plain title span in `ConversationBreadcrumb.tsx` and
the click-to-rename button in `HeaderTitle.tsx` (used at md and wider, so iPads) now carry
`breadcrumb-title`, the hide rule skips that class, and the rule that hid the empty nav is
gone. Folder, separators and the sub-agent segment stay hidden; a sub-agent still shows only
its Back link. The `min-w-0` chain from the header row to the span lets a long title
truncate instead of pushing the slots chip and menu off a 390 px screen (checked in review).
Unchecked: whether Android still floats a server pill over the header band (`index.css`
mentions one); look once on an Android phone. Tests: new
`ConversationBreadcrumb.aeNativeTitle.test.tsx`, which loads the native rules from
`index.css` (jsdom applies no app stylesheet) and fails on the old rule.

`0140` (2026-10-21) sizes the session menu and its submenus to their rows and gives New
Chat the same menu. Reid's screenshot: the chat's session menu (fixed `w-[19rem]` from
`0062`'s compact controls) left a gap between each model name and its shortcut digit, the
harness row menu above it kept upstream's fixed `w-[17.5rem]`, and the Account submenu took
its 334 px width from the scope hint. `AE_MENU_WIDTH_CLASS` in `web/src/lib/aeMenuRow.ts`
(`w-max`, a `min-w-40` floor, the viewport cap) now goes on all five; `AE_MENU_NOTE_CLASS`
(`w-0 min-w-full whitespace-normal`) keeps the hint and the error lines out of the
submenu's max-content width so they wrap under the rows. The Previous models chevron takes
`ml-auto!` because upstream's `.composer-agent-menu [role="menuitem"] > svg:last-child`
rule zeroes the margin. Reid then asked for all associated menus to match: New Chat's
Claude Code and Codex picker still drew upstream's sectioned list (account chips, inline
effort, checks on the right). The chat's compact list moves out of `ChatPage.tsx` into
`web/src/components/composer/AeCompactSessionChoices.tsx` (a `testIdPrefix` keeps each
page's test ids), and `NewChatDialog.tsx` renders it with `AeClaudeAccountSubmenu` and the
digit shortcuts for codex-native, and for claude-native when every model row comes from the
subscription; a mixed-source Claude catalog keeps the sectioned list, since the compact list
merges rows of one version and splits Previous models across sources. Pi keeps its model
search, and Devin and sandbox catalogs keep the sectioned list. The Account submenu's scope
line is a prop (New Chat says "For new chats."), digits work from any row of the config
menu, and model status lines wrap like the scope line.
`HarnessPickerEntry` gains a `configClassName` prop so New Chat's config submenu takes the
same width rule. Tests: class assertions in `AeClaudeAccountSubmenu.test.tsx`; the New Chat
suites open the Effort and Previous models submenus before picking. Checked with before and
after screenshots of the real app against the live server. It sits on the fork branch
`analyticendeavors/omnigent@ae/picker-menu-width`, after `0139`.

`0141` (2026-10-21) moves the action dock into the composer's workspace bar, one strip in
place of two. Before it the dock (`0137`) was a row of its own above the bar, 69 px together
with anything waiting; Reid asked for one strip, approved the mockups and said build. The dock
root is now `display: contents` and is mounted inside `ComposerWorkspaceBar`, after the token
defrag control, so each part is one of the bar's flex items: a hairline, the lead item (flex
1, so it takes the free space and truncates last, with its full text as a tooltip), the
badges, then the bar's own right cluster, and the caret (`order-last`) at the end. Opened, the
strip shows "N waiting" and the list is a tray positioned above the bar (`bottom-full`), the
bar squaring its top corners while it is open (`has-[[data-ae-dock-open]]`, top-left and
top-right). Merge pressed in
the strip opens the list with the confirm in that line, since the strip has no room for it,
and while a confirm is open the list may grow to half the screen (`0138` had removed the
cap outright) and scrolls the confirm into view. A live deploy
alone is a green chip with its dismiss and no caret. Badges drop to icon and count before the
lead truncates: `useAeBadgeCompaction` watches the lead with a ResizeObserver, compacts when
the lead's text overflows, and restores the labels once the lead's unused width covers what
they need. The dock root carries `data-ae-dock-items`, which the bar reads with
`group-has`: `COMPOSER_WORKSPACE_COLLAPSED_LABEL_CLASS` (`ChatComposer.tsx`) hides the
directory and branch labels, and `ComposerPrLink.tsx` hides its label and shows the count as
the button's `::after` (`data-ae-count`), so the chip reads "7", never "7 P…" or "#194", and
its text content and accessible name do not change. Upstream files change by one class
string each (`ChatComposer.tsx`, `ComposerControls.tsx`), two classes and an attribute
(`ComposerPrLink.tsx`), and a moved mount line (`ChatPage.tsx`). Tests: `AeActionDock.test.tsx`
now renders the dock inside a real `ComposerWorkspaceBar` with a `ComposerPrLink`, with new
cases for the mount inside the bar, the chip's count, the bar unchanged when nothing waits,
badge compaction (measured widths stubbed), Merge from the strip opening the list, and the
live chip, plus Cancel after a Merge from the strip returning to the strip without saving
the list open. Review before the first push (2026-10-21, Claude code review, ten findings):
applied the open state that a Merge from the strip saved (now a transient open, closed again
when the confirm ends), compaction going stale (it restarts from full labels when the lead or
badge set changes, and re-measures when the lead's text changes), and the dated history in
the file header. Left as is: the tray overlays what sits above the bar (the queued-messages
tray, the newest lines) while open, as a popover would, since an in-flow tray needs a second
mount in `ChatPage.tsx`; the caret's `order-last` puts it after the right cluster visually
but before it in tab order; a single PR reads "1", not "#180", as Reid's brief asked; and a
phone narrower than 390 px can run the strip's fixed items past the bar's edge. The dual
review of the PR (2026-10-21, Reid approved six fixes) added: the dock its own React key
(`ae-dock:<id>`, it had shared the token defrag's), the caret dropping any open confirm and a
Merge on the strip always opening its confirm, a confirm whose item leaves the dock (merged
elsewhere, or its card on screen) closing with the tray opened for it, the bar raised
(`has-[[data-ae-dock-open]]:z-20`) so feed controls cannot paint over the tray, the
half-screen cap above, and the tray starting closed on every load (the saved open state is
gone) and closing on Escape. It sits on
the fork branch `analyticendeavors/omnigent@ae/dock-single-strip`, after `0140`.

`0142` (2026-10-21) adds a usage gate to the action dock (plan "Accounts card and usage
gate", PR 2, decisions by Reid the same day). When the account a chat runs on reaches
`AE_LIMIT_GATE_PERCENT` (90) of its 5-hour or weekly window, the dock shows a `limit` item
ranked after questions and before merges: "AE Default is at 92% of its weekly limit, resets
Thu 9:00 AM" (on a phone "92% weekly", the buttons icon only and the dismiss only in the
opened list), with three buttons and a dismiss. The strip labels them Model, Account and New
chat so the line keeps its room; the opened list and the accessible names say Switch model
and Switch account.
Switch model bumps the composer's picker nonce, the same path as bare `/model` and
Ctrl+Shift+M, so the model menu opens on its config page; it is absent under the hotkey's own
conditions (no model rows, read-only, runner unreachable), where the nonce would do nothing. Switch account opens the fork
dialog as a full clone (history included) for this session, where `0099`'s Claude account
row picks the login; it is hidden on Codex, when the session cannot be forked or carries a
fork restriction (the opener returns early then), and when the
host offers no Claude login other than the session's own (none on provider credentials,
as the fork row decides). New chat is `navigate("/")`. The item never blocks sending.
A dismissal is the account's, not the chat's: one localStorage list for every chat (eight
entries at most), each entry a window (harness, account, window id) and the reset it was
dismissed for. A reading whose reset is within an hour of that one is the same window
(readings differ by seconds; the next window resets at least five hours later), so the item
returns in the next window. Each window keeps its own dismissal, and a dismissed fuller window
never hides the other one past 90%.

The logic is in new files: `lib/aeLimitGate.ts` (pure: the headline windows picked as the
server's `headline_windows` picks them, so a per-model weekly row never gates; the fullest
window not dismissed wins and a tie goes to the later reset; a window whose reset has passed
waits for a new reading; the percent rounded as the popover rounds it; the text and the
dismissals) and `hooks/useAeLimitGate.ts` (the session's
`GET /v1/ae/limits?session_id=` answer through `useAeLimits`, one shared query per session
refetched each minute, read only for a Claude or Codex session; the host's detected logins
and the session snapshot read only while a Claude gate shows). AE files of `0137` change by
little: `lib/aeActionDock.ts` the kind and its badge ("92% weekly"),
`hooks/useAeActionDockItems.ts` the item and its dismiss, `components/AeActionDock.tsx` the
line, the buttons and an `onSwitchModel` prop, and `lib/aeActionDockIcons.ts` four Tabler
icons. The one upstream file, `ChatPage.tsx`, passes `onSwitchModel` to the dock's mount (nine
lines).
Tests: `aeLimitGate.test.ts` (below, at and above 90 for each window, the fullest window,
no windows, Codex windows by length, dismiss until the reset changes, one window's dismissal
never hiding or undoing the other's) and new cases in
`AeActionDock.test.tsx` (the item and its three buttons, nothing below 90, each button's
handler, Switch account hidden with one login, on Codex, without fork access and with a fork
restriction, Switch model absent without a menu to open, a dismissal holding across a remount
until the window resets). Review before the first push (2026-10-21, Codex and a Claude code
review, eleven findings): applied the dismissal fixes (one entry per window, the reset
compared within an hour instead of rounded, the dismissed window skipped before the fullest
is picked, kept per account across chats), Switch account hidden under a fork restriction,
Switch model gated like the hotkey, and the popover's rounding. Left as is: New chat and Switch
model stay on the same account, as Reid's three buttons decided; the minute refetch per open
Claude or Codex chat, which `useAeLimits` already does for the popover; the reset text kept
apart from `formatResetShort` and the 90 apart from `limitTone`, since a lib file cannot
import from a component file and the gate is its own decision. It sits on the fork branch
`analyticendeavors/omnigent@ae/limit-gate`, after `0141`.

`0143` (2026-10-21) infers bracketed paste for a shell prompt on tmux older than 3.7, so
the Run button on a chat code block pastes a multi-line block again. Reid's screenshot: Run
on an eight-line bash block showed "This shell has not turned on bracketed paste ... Nothing
was sent". bash had enabled bracketed paste when it drew its prompt, before the browser
attached, and the attach seed replays that mode from `#{bracket_paste_flag}`, which tmux
only has from 3.7. The box runs tmux 3.6a (Ubuntu 26.04 ships no 3.7), so the flag expanded
empty, the seed never sent `?2004h`, and the Run guard in `web/src/lib/aeTerminalInput.ts`
refused the block. `_capture_pane_metadata` in `omnigent/terminals/control_bridge.py` now
also reads `#{pane_tty}` and `#{pane_current_command}`, and
`omnigent/terminals/_ae_bracket_paste.py` decides: a reported flag wins; an empty one means on
when bash, zsh or fish is the foreground command on the primary screen and the pane's tty is
out of canonical mode, which a line editor at its prompt sets and a script or the `read`
builtin does not (both still report `bash`; Codex reproduced the `read` case in review). The
command name is decoded with replacement so an odd name cannot cost the cursor and
alt-screen seed. On tmux 3.7 the inference goes dormant. Left as is: a shell whose user
turned bracketed paste off still gets it inferred until its next prompt resets the mode.
Tests: `tests/terminals/test_ae_bracket_paste.py` (the decision table and the tty check on a
real pty) and `test_ae_seed_infers_bracketed_paste_at_a_bash_prompt_only` in
`test_control_bridge.py` (a real tmux 3.6 bash pane seeds `?2004h` at its prompt and not
inside `read`); checked against the live bash pane on the box. It sits on the fork branch
`analyticendeavors/omnigent@ae/run-bracket-paste`, after `0142`.

`0144` (2026-10-21) keeps merges, deploys and the usage item in the action dock while their
card is on screen. `0137` hid any item while part of its own card was visible, so a chat with
three pull requests ready showed one at a time and the lead changed as Reid scrolled; he
reads the dock as what is pending now. `aeDockArrange` (`web/src/lib/aeActionDock.ts`) now
drops only questions whose card is on screen (`questionsOnScreen`), since a pending question
card floats just above the dock and would otherwise show twice; Reid picked that from
mockups. The observer hook becomes `useAeQuestionsOnScreen` and watches `[data-ae-question]`
only, and `data-ae-pr` goes from the inline card and the merged row's chips, since nothing
reads it now. A ready pull request whose card is in view shows Merge in both places, so
`ConfirmMerge` (`AePrCards.tsx`) disables Confirm while any merge of that pull request is in
flight (`useIsMutating` on the shared `ae-pr-merge` key). Tests: the arrange suite checks a
visible question hides and a visible pull request does not; the dock suite stubs an
IntersectionObserver that reports the question card visible and expects the merge to lead,
and the confirm-closing case now merges the pull request through its query instead of
scrolling its card into view. Review before the first push (2026-10-21, Codex and Claude
code review): applied the merge guard, a test that fails without the change, the dead
attribute and two comments. It sits on the fork branch
`analyticendeavors/omnigent@ae/dock-show-all`, after `0143`.

`0145` (2026-10-21) previews a pull request when the pointer rests on its number in the
action dock. Reid asked for the chat links' hover preview (`AePrLinkHoverCard`, `0118`) on
the dock's `#n` too, before the click that opens GitHub. `AeActionDock.tsx` wraps the number's
link in `AePrLinkHoverCard`, in the strip and the opened list alike; the preview reads the
`["ae-pr", repo, number]` query the dock already holds, so it opens with no fetch. The link's
`title` becomes `aria-label`, since a native tooltip would show over the preview. Test: the
dock suite focuses the number and expects the preview with the pull request's title (it fails
without the wrap). It sits on the fork branch `analyticendeavors/omnigent@ae/dock-pr-hover`,
after `0144`.

`0146` (2026-10-21) keeps the merged pull request row's numbers on one line on a phone. In
`AeMergedPrFold.tsx` the chip row was `flex-wrap`, so at 390 px three numbers beside "3
merged" and "waiting for deploy" took two lines. The row is now `overflow-x-auto` with the
scrollbar hidden (`[scrollbar-width:none]`), and each chip `shrink-0 whitespace-nowrap`, so
the numbers stay on one line and scroll sideways when they do not fit; the label's count and
the opened fold still list every one. Test: `AeInlinePrCards.test.tsx` asserts the row's and
chips' classes. Checked with a before and after rendering of the row's classes at 390 px. It
sits on the fork branch `analyticendeavors/omnigent@ae/fold-chips-one-line`, after `0145`.

`0147` (2026-10-21) opens the action dock's pull request preview above the bar. With `0145`
it opened below the number, over the composer's text box. `AePrLinkHoverCard` takes an
optional `side` ("top" or "bottom", passed to `HoverCardContent`), and `AeActionDock.tsx`
passes "top", the direction the dock's tray opens; chat links pass nothing and keep Radix's
default. Radix still flips the preview when there is no room above. Test: the dock suite
expects the preview's `data-side="top"` (it fails without the prop). It sits on the fork
branch `analyticendeavors/omnigent@ae/dock-preview-up`, after `0146`.

`0148` (2026-10-21) caches the `gh auth status --json hosts` answer for 60 seconds across
the host's processes. On the box, every read of a session's GitHub resource ran it through
`_list_accounts` (`omnigent/runner/github_resource.py`), in the session's own runner or in
the host service, and the server's merge watcher reads every recent session each 120
seconds: 39 of 135 `gh` processes in ten minutes, four to seven in the same second at each
watcher burst. Runners are one process per session, so a per-process cache would rarely
hit; `omnigent/runner/_ae_gh_auth_cache.py` keeps one small file per user under
`$XDG_CACHE_HOME/omnigent` (default `~/.cache/omnigent`), keyed by the gh config dir, the
size and mtime of its `hosts.yml` (a login, logout or switch misses at once), the sandbox
flag and a hash of the `GH_*` token variables; the config dir comes from gh's own
platform-aware order (`omnigent/git_providers/github.py`). Only an authenticated answer is
cached. A `gh` call on the ambient login that fails with an authentication error marks the
entry invalidated, and a fill that started before the mark does not overwrite it; a call
run with a selected account's token is ignored. Concurrent misses share one `gh` run
through an advisory file lock that a waiter gives up on after 3 seconds or at the title
lookup's deadline, whichever is first, and a filesystem without `flock` runs unlocked at
once. A write removes other keys' entries and locks idle for an hour, since the broker's
`hosts.yml` refresh makes a new key every 30 minutes. The cached accounts carry login,
active, state and host, never a token.
`OMNIGENT_GH_AUTH_CACHE_SECONDS` sets the TTL (`0` turns it off), and an autouse fixture
in upstream's `tests/conftest.py` turns it off so stubbed `gh` answers never outlive a
test. The upstream edit is the import, a one-line `_list_accounts` wrapper and one
`note_gh_result` line in `_gh`. Tests: `tests/runner/test_ae_gh_auth_cache.py` (hit, TTL
expiry, failures never cached, login and token changes miss, auth failure invalidates,
a selected account's failure does not, an invalidation during a fill wins, one `gh` run for
concurrent misses, capped lock wait, pruning), in `just upstream-smoke`. Review before the
first push (2026-10-21, Codex and Claude code review): applied the Windows config dir,
`posix_only` on the lock tests, the deadline, the token guard, the fill race, pruning, the
`flock` error case and a shorter module docstring; left as is: other processes' keys are
not invalidated (a different env is a different identity), and the 3 second wait stays
below the 15 second `gh` timeout so a slow holder costs a waiter at most 3 seconds. It
sits on the fork branch `analyticendeavors/omnigent@ae/gh-auth-cache`, after `0147`.
