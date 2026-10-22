# The Fast mode switch (2026-10-14, patch 0044)

Component renders, not a live server: the session harness picker's model menu and
the `AeFastModeSection` row from `web/src/components/AeFastMode.tsx`, rendered to
static HTML with the web app's built Tailwind CSS and captured with headless
Chromium, at desktop width and 375 px, light and dark.

- `fast-desktop-light.png` and `fast-desktop-dark.png`: the Claude Code model menu
  with fast mode on (Opus 5.5), and the row's other states: before the session has
  reported its state ("Unknown until the session replies"), while Claude Code
  confirms ("Turning fast mode on..."), after a failed change (reverted, with the
  reason), on a model without fast mode ("Opus only"), and since the review fixes
  (2026-10-14) the pending note ("Applies when the current reply finishes") and a host
  without the per-session setting ("Needs per-session fast mode on this host").
- `fast-375-light.png` and `fast-375-dark.png`: the same on a phone.
