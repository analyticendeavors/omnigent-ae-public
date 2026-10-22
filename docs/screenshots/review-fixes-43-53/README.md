# Review fixes for the merge card (2026-09-27)

Why: a post-merge code review of
https://github.com/analyticendeavors/omnigent-ae/pull/53 found two layout bugs,
fixed in patch `0023` (the chat card) and out of tree (the Dashboard card):

- In chat, the card is a size container, so the `w-fit` chat bubble shrank it to the
  width of the message text; a one-line reply got a narrow card that switched to the
  phone layout on a desktop. The card now has a set width, 36rem capped at the bubble.
- On the Dashboard, the phone layout switched on a 480 px viewport, so in the
  two-column grid (720 px up) a card narrower than a phone still got the desktop
  layout. The card is now a container and the switch reads its own width (30rem).

How they were taken: Playwright Chromium, dark theme, the real components served by
Vite. The chat card is `AeInlinePrCards` inside the real `Message` and
`MessageContent` (the `w-fit` bubble) under the one-line reply "Opened
https://github.com/analyticendeavors/omnigent-ae/pull/55", with `/v1/ae/prs/*`
answered by one fixed card (open, CI 8/8 passing, no conflicts, no review, squash and
merge commit allowed). The Dashboard card is `renderPrsSection` from the extension's
`prs.ts` in the page's own grid (`.grid`, `.col-main`, `.col-side`) with its
stylesheet. The `before-` shots reproduce the old code in the same page: the chat card
with the old class list (no `w-xl`), the Dashboard with the stylesheet from `main` at
916d17b.

Measured in the page: the chat card is 576 px wide at 1440x900 (it was 413 px, the
width of the reply text) and fills the bubble at 358 px on the phone. The Dashboard
card is 421 px wide at 800 px, under the 450 px that 30rem is on the page's 15 px root
font, so it takes the phone layout.

| File | Shows |
|---|---|
| `chat-desktop-1440-collapsed.png` | chat card at 1440x900, before Merge is tapped: full width under a one-line reply |
| `chat-desktop-1440-confirm.png` | the same, confirm step: toggle, Confirm merge and Cancel on one row |
| `before-chat-desktop-1440-confirm.png` | the old code: the card shrunk to the reply text and flipped to the phone layout |
| `chat-phone-390-collapsed.png` | chat card at 390x844 (`hasTouch`, `isMobile`, scale 2), collapsed |
| `chat-phone-390-confirm.png` | the same, confirm step: toggle on its own row, the two buttons sharing the next |
| `dashboard-800-confirm.png` | Dashboard Open PRs card in the two-column grid at 800 px, confirm step: phone layout |
| `before-dashboard-800-confirm.png` | the old stylesheet: the desktop layout in a phone-narrow card, wrapping unevenly |
