# Merge card on the phone (2026-09-25)

These shots predate patch `0022` (2026-09-26): the confirm step is now one row (the
method toggle with the short labels Squash and Merge commit, then Confirm merge and
Cancel, both boxed), wrapping to two on a phone with Confirm merge and Cancel side by
side rather than Cancel as a text button under Confirm; the Dashboard's buttons are
32 px tall with a mouse (40 px on a touch screen); and the offer after a merge reads
"Mark session done" with a "frees a slot" hint instead of "Set this session to
review". They were not retaken: the box has no headless browser.

Why: the owner's iPhone screenshots of the chat's inline pull request card. Collapsed, a
small Merge button sat bottom left beside an empty half row. On the confirm step the
two merge methods were boxed buttons with the same shape and weight as Confirm merge
and Cancel, so the step read as four actions in a 2x2 grid and the chosen method was
barely visible.

What changed (patch `0021` for the chat card, `packages/ae_omni_extension` for the
Dashboard):

- Collapsed: Merge spans the card on a phone (a card under 30rem on the chat side,
  a viewport under 480 px on the Dashboard, where Merge and Copy link already shared
  the row); desktop keeps its inline size.
- Confirm step: the method is a segmented control, one bordered track with the
  chosen segment filled and ticked with Tabler's `check` (`role="radiogroup"`),
  hidden when the repository allows one method. Confirm merge is the full-width
  primary button and Cancel a quiet text button under it on a phone; side by side
  on desktop.

How they were taken: Playwright Chromium, the real components from this branch
served by Vite with `/v1/ae/prs/*` answered by one fixed card (open, CI 8/8 passing,
no conflicts, no review, squash and merge commit allowed). The chat card is
`AePrCardView` with `variant="inline"` under a sample assistant message; the
Dashboard card is `renderPrsSection` from the extension's `prs.ts` with its own
stylesheet. Each shot is taken, then Merge is tapped and the confirm step is shot.

Sizes: `phone` is 390x844 with `hasTouch` and `isMobile` at device scale factor 2;
`desktop` is 1440x900 at scale 1. Both themes, `dark` and `light`.

| File | Shows |
|---|---|
| `chat-collapsed-{phone,desktop}-{dark,light}.png` | chat inline card, before Merge is tapped |
| `chat-confirm-{phone,desktop}-{dark,light}.png` | chat inline card, confirm step |
| `dashboard-collapsed-{phone,desktop}-{dark,light}.png` | Dashboard Open PRs card, before Merge |
| `dashboard-confirm-{phone,desktop}-{dark,light}.png` | Dashboard Open PRs card, confirm step |

Your move's pull request cards use the same Merge button and confirm step as Open
PRs (`mergeButton` and `confirmRow` in `prs.ts`), so they are not shot separately.
