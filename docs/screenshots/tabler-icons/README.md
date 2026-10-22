# Tabler project icons (patch P15, 2026-09-24)

Taken the Phase 3 way: a real OmniGent server from the pinned submodule (`v0.15.0`
plus patches P1 to P15) with its web SPA built from the patched tree,
`../config/server.yaml`, a fresh SQLite database and single-user local mode. Five
projects were created through `POST /v1/projects`: three with Tabler icons
(`tabler:briefcase`, `tabler:chart-bar`, `tabler:rocket`), one with an emoji saved the
old way (`🔥`) and one with no icon. Playwright Chromium at device scale factor 2,
1440x900 and 390x844 (the phone at the width of Reid's iPhone, touch on).

- `*-sidebar.png`: the Projects group. Tabler icons draw in the folder's box and color,
  the emoji project keeps its emoji, and the project without an icon keeps the folder.
- `*-picker.png`: New project, then the icon tile: the Tabler picker with the
  suggested project icons first. On the phone the popover fits the screen width and
  scrolls inside.
- `*-search.png`: "chart" typed in the search box: 57 matches by name and by tag.
- `*-picked.png`: after tapping `chart-pie` and typing a name: the tile shows the
  chosen icon before Create.
