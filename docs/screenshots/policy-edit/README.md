# Editing a global policy's parameters (2026-10-18)

Taken with Playwright and the box's Chromium (1223) from the real web app (vite dev,
patches 0094 and 0095 applied) against a throwaway local server on its own sqlite
database, holding only the five AE policies created through `POST /v1/policies` with
the parameters `config/server.yaml` gives them. Nothing here comes from the box's
server or a real session.

- `list.png`: Settings > Global Policies at 1280 px. The slot cap, fork suspect and
  rabbit hole cards carry the pencil beside the switch; the hurry modes and the
  observer fallback take no parameters, so they have none.
- `edit-dialog.png`: the rabbit hole's Edit dialog, prefilled with `hours: 4` and
  `turns: 60`, the name fixed.
- `refused.png`: Save with `hours: -1`. The schema allows a number, the factory refuses
  it, and the server's reason shows in the dialog; nothing is saved.
- `slot-cap.png`: the slot cap's dialog: `mode` and `lot_url` prefilled, `limit` blank
  with the `AE_SLOT_LIMIT` default as its placeholder, and left out on Save.
- `phone-dialog.png`: the same dialog at a 390 px viewport.
