# Split view (patch P5, 2026-09-24)

Taken the Phase 3 way: a real OmniGent server from the pinned submodule with its web
SPA built, `../config/server.yaml`, a fresh database, single-user local mode, and four
seeded sessions carrying `ae.status` labels and a short transcript each (posted as
`external_conversation_item` events, so no runner was needed). Playwright Chromium
at device scale factor 2, a fresh browser profile per viewport.

- `desktop-1440x900-menu.png`: right-click on a sidebar row; "Open in split view"
  sits under patch P4's Status submenu, above the lifecycle actions.
- `desktop-1440x900-two-panes.png`: the result of that click while viewing another
  session: the viewed session and the chosen one side by side (`/split?s=a,b`).
- `desktop-1440x900-three-panes.png`: after "Add pane" and picking a third session
  from the new pane's switcher. The yellow banner in the third pane is upstream's
  stream-budget warning: the local server speaks plain HTTP/1.1, where upstream caps
  live chat streams at three per origin. The box is served over HTTP/2 through
  Cloudflare, where the cap is thirty, so it does not appear there.
- `desktop-1009x719-fallback.png`: the same `/split?s=a,b` URL at 1009x719, below the
  1024 px floor, lands on the first pane's session on its own.

## Layout presets (patch P10, 2026-09-24)

Taken the same way, with the web SPA rebuilt from the patched tree and four seeded
sessions, at 1440x900, device scale factor 2. Each shot opens
`/split?s=...&l=<preset>` directly.

- `desktop-1440x900-preset-cols2.png`: 2 side by side (the URL carries no `l`).
- `desktop-1440x900-preset-rows2.png`: 2 stacked, one horizontal divider.
- `desktop-1440x900-preset-stack-left.png`: 2+1, two stacked with one beside them.
- `desktop-1440x900-preset-stack-right.png`: its mirror, one with two stacked beside it.
- `desktop-1440x900-preset-grid.png`: the 2x2 grid; one vertical divider and a
  horizontal one per column, each column's stack resizing on its own.
- `desktop-1440x900-preset-cols3.png` and `-cols4.png`: 3 and 4 side by side.
- `desktop-1440x900-picker.png` and `-picker-closeup.png`: the picker in the top bar,
  one icon per preset drawn from its shape, the current one (grid) pressed, the
  pointer on 2+1. The closeup is the top-left 720x80 of the same page.
- `desktop-1440x900-grid-after-swap.png`: the grid after dragging the first pane's
  grip onto the fourth pane; the two sessions changed places and neither frame
  reloaded.

The yellow banner in a pane of the four-pane shots is upstream's stream-budget
warning under the local server's HTTP/1.1, as in the P5 shots above; it does not
appear on the box, which is served over HTTP/2.
