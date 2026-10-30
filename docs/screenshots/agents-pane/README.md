# The Agents pane (2026-10-15)

Taken with the box's headless Chromium (`chrome-headless-shell` 1223) from a small
Vite page that mounts upstream's `SubagentsPanel` on seeded query data: one Claude
Code root, nine finished Claude Code subagents, two running ones and one running
agent OmniGent launched (the only row with a Stop square), plus a four-item task
list. The names and times are illustrative, not the owner's. The pane is 620x1100, the
shape of the right rail on the owner's screen, and 375x812 for the phone shots.

- `before-graph-dark.png`, `before-list-dark.png`: upstream before patch 0054.
  The graph is one top-down row of ten nodes shrunk to dots; the list is in spawn
  order with no times.
- `after-graph-dark.png`: automatic direction in the tall pane, so left to right at
  100%.
- `after-graph-tb-dark.png`: the toggle set to top-down. The fit stops at 70% and
  starts at the root; the rest is a pan away.
- `after-graph-phone-light.png`: left to right at 375 px.
- `after-list-dark.png`: Running with the Task, Model and Time header, Finished folded
  to "Finished 9", and the Plan section.
- `model-before-codex-dark.png`, `model-after-codex-dark.png` (2026-10-16, patch
  0063): a Codex session's finished collaborators at 450x760, the shape of the owner's
  report. Before, every Model cell is empty and the dots float in that gap; after, each
  row shows its model and the dot sits right of Time.
- `model-before-list-dark.png`, `model-after-list-dark.png`, `model-after-list-light.png`,
  `model-after-list-phone-dark.png`: the Claude Code list with each subagent's reported
  model ("Opus 5.5", "Sonnet 5.5", "Haiku 5.5"). The first running row has no report
  yet, so its cell stays blank. One running row needs a response and one finished row
  failed: their words sit left of Model, so Model and Time stay in line.
- `after-list-open-light.png`, `after-list-phone-dark.png`: Finished unfolded, in
  light at full width and dark at 375 px with no sideways scroll.
