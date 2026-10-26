# Roles presets

<sub>Roles presets &middot; [All features](README.md)</sub>

Stock Omnigent lets a lead session start subagents and other agent sessions, and each one runs on whatever model it is given. The fork adds roles presets: a named table that gives each role (lead, planner, reviewer, builder and helper) its own model and effort, a pill in the composer to pick one per chat, families that keep a lead's sessions together, and up to three independent reviewers whose findings merge into one verdict.

## Presets in Settings > Roles

<p align="center"><img src="../media/roles-settings.webp" alt="Presets in Settings &gt; Roles"></p>

Each preset sets the model and effort for five roles: lead, planner, reviewer, builder and helper. The lead can be fixed to a model or follow the session's own (Session model). Presets sit in one row, the default marked with a teal dot and pinned ones as icons, and All presets opens the full list, which you can reorder by drag or with the arrow keys. Each has a title, an icon and a color, a card description and a "when to use" note. The starters (Save tokens, Balanced, Solo, YOLO, Think it through, Deep debug and Quick fix) are ordinary presets you can edit, duplicate or delete, and Restore starters brings them back. A preset that runs the top model at max on every role shows a cost warning.

**Where:** Settings > Roles.<br>
**Built from:** core patch [`0163`](../../patches/0163-ae-patch-Add-Settings-Roles-where-presets-set-each-r.patch); [`packages/ae_omni_policies`](../../packages/ae_omni_policies)

## The lead reads its preset every turn

Every turn, the lead's notice names the model and effort each role gets and the preset's rules, so the lead starts a builder on a cheaper model instead of its own. A Claude lead is told the tier aliases for its Agent tool; a Codex or other lead gets the roles by model and effort. Sessions the lead starts as helpers are told so, and do not start helpers of their own; a handoff chat it opens is a lead of its own. When a preset fixes the lead's model, picking it switches the session's model too, with a "Lead switched to" toast and Undo.

**Where:** Every chat, on its own; the preset in use shows on the composer's preset pill.<br>
**Built from:** core patches [`0162`](../../patches/0162-ae-patch-Tell-the-lead-each-turn-which-model-and-eff.patch), [`0166`](../../patches/0166-ae-patch-Deliver-the-lead-s-notice-on-plain-text-nat.patch)

## Pick a preset per chat

<p align="center"><img src="../media/roles-preset-pill.webp" alt="Pick a preset per chat"></p>

The preset pill sits beside the model picker in the composer. Its menu lists Lead picks, where the lead chooses a preset from each one's "when to use" note, then your pinned presets, then More presets for the rest, and Alt 1 to 9 pick by list order. Choosing the default writes nothing on the session, so a later change of default still applies. On a phone the menu is a bottom sheet that opens on the list, not the search box.

**Where:** The preset pill left of the model picker in any chat's composer.<br>
**Built from:** core patches [`0164`](../../patches/0164-ae-patch-Pick-a-roles-preset-per-session-from-the-di.patch), [`0165`](../../patches/0165-ae-patch-Fold-the-composer-s-Auto-Solo-and-Team-butt.patch), [`0167`](../../patches/0167-ae-patch-Open-the-phone-preset-picker-on-the-list-no.patch), [`0170`](../../patches/0170-ae-patch-Show-pinned-presets-first-in-the-composer-m.patch)

## Start a new chat on a preset

<p align="center"><img src="../media/roles-new-chat-pill.jpg" alt="Start a new chat on a preset"></p>

The new-chat screen has the same pill, so a chat can start on the preset you want before its first message. The pick applies when you send the first message, before it is posted, so the first turn runs on it. A `START ON:` line in a chat's first message does the same in words: it sets the preset, or the model and effort, before the message is sent, and shows "Started on" with Undo. Settings > Roles has a switch to stop applying `START ON:` lines.

**Where:** The pill on the new-chat screen, or a `START ON:` line in the first message.<br>
**Built from:** core patches [`0164`](../../patches/0164-ae-patch-Pick-a-roles-preset-per-session-from-the-di.patch), [`0178`](../../patches/0178-ae-patch-Show-the-roles-preset-pill-on-the-new-chat-.patch)

## Who works on this

<p align="center"><img src="../media/roles-who-works.jpg" alt="Who works on this"></p>

The pill's "Who works on this?" row opens a dialog with the lead (Change lead swaps it), the Roles preset row with Customize for this chat's own role table, and the sessions added to this chat, such as Codex for an independent review, each with its own model. Each preset has a Helpers rule: Any agent or session, or Own subagents only, which keeps the work in the lead's own subagents and greys the sessions list. A Reviews line says how this preset reports reviews, such as one merged verdict.

**Where:** The preset pill, then Who works on this?<br>
**Built from:** core patches [`0030`](../../patches/0030-ae-patch-Add-composer-teams-and-separate-model-conte.patch), [`0032`](../../patches/0032-ae-patch-Add-Auto-collaboration-and-hide-unconfigure.patch), [`0165`](../../patches/0165-ae-patch-Fold-the-composer-s-Auto-Solo-and-Team-butt.patch)

## Independent reviewers, one verdict

<p align="center"><img src="../media/roles-reviewers.jpg" alt="Independent reviewers, one verdict"></p>

A preset's Reviewer role can be a stack of up to three reviewers: a Claude model and effort, run as a native subagent, or another agent such as Codex, started as a fresh session each review so it carries nothing from the last round. With two or more, the lead runs them all at the final review on the same diff, never shows one reviewer's findings to another, and merges them into one verdict; fix checks stay with Reviewer 1. Preset rules choose whether you see the merged verdict alone or each list and then the verdict, and what happens when a reviewer cannot start. A Codex reviewer joins the lead's family, and the lead reports each final review to the preset's run log. Balanced, YOLO, Think it through and Deep debug start with Codex as Reviewer 2.

**Where:** The Reviewer rows of a preset in Settings > Roles (Add reviewer), Customize, and Who works on this?<br>
**Built from:** core patches [`0169`](../../patches/0169-ae-patch-Let-a-roles-preset-name-up-to-three-reviewe.patch), [`0180`](../../patches/0180-ae-patch-Tell-the-lead-to-start-a-Codex-reviewer-wit.patch), [`0181`](../../patches/0181-ae-patch-Tell-the-lead-to-report-its-final-review-an.patch); [`packages/ae_omni_policies`](../../packages/ae_omni_policies)

## Rules for model changes and helpers

<p align="center"><img src="../media/roles-model-rules.jpg" alt="Rules for model changes and helpers"></p>

Each preset carries its own rules. "Raise a stuck child, or lower the lead" is Off, Tell me after or Ask first, and applies both ways; "Lead steps down for mechanical work" moves the lead to the Builder's model for work such as watching CI; and "Automatic raises per child" sets how many raises happen before every raise asks you. Helpers decides whether the lead may hand work to other agents and sessions, or only its own subagents.

**Where:** Model changes in this preset, in Settings > Roles.<br>
**Built from:** core patches [`0163`](../../patches/0163-ae-patch-Add-Settings-Roles-where-presets-set-each-r.patch), [`0165`](../../patches/0165-ae-patch-Fold-the-composer-s-Auto-Solo-and-Team-butt.patch), [`0174`](../../patches/0174-ae-patch-Show-the-lead-s-model-changes-as-chat-rows-.patch)

## Model changes you can undo

<p align="center"><img src="../media/roles-model-band.jpg" alt="Model changes you can undo"></p>

The lead changes a helper's model, or its own, with the `set_session_model` tool, under those rules. The latest change of the lead and of each of its helpers, from the last 24 hours, shows in a band under the composer, newest first: what changed ("Raised the builder from Sonnet medium to Opus high"), the reason and how long ago, with Undo, Open helper and a close box to dismiss it. A helper running above the model it started on carries an amber raise arrow in Grouping > Family and on the Agents card; its tooltip says from which model and how many of its automatic raises are used.

**Where:** The band under the composer, in both the chat and the terminal view.<br>
**Built from:** core patches [`0174`](../../patches/0174-ae-patch-Show-the-lead-s-model-changes-as-chat-rows-.patch), [`0177`](../../patches/0177-ae-patch-Show-model-changes-in-a-band-above-every-ch.patch), [`0186`](../../patches/0186-ae-patch-Make-the-model-change-band-s-actions-look-c.patch); [`packages/ae_omni_policies`](../../packages/ae_omni_policies)

## Families of sessions

<p align="center"><img src="../media/sidebar-family-tree.webp" alt="Families of sessions"></p>

A session that starts other sessions through the orchestrator's `create_session` leads a family: it and every session it started carry the same colored dot before the title, and a fork of any of them joins too. Hover the dot to see who started a row; click it to show only that family, and clear the bar above the list to see everything again. Grouping: Family lists the families first as trees, each lead as the parent row with its sessions on a thin line in the family's color, which replaces the dot, and a letter badge for each one's role, then the Standalone sessions in the current sort. The chevron at the right edge of the lead's row folds its family. Closed sessions fold into one "closed" row under their lead, archived ones included, and archiving a lead from its row or the chat header archives its family with it, after a confirm when some are still open. A family holds one slot between all its sessions, unless the slot cap counts helpers (see [Triage and policies](triage-and-policies.md)), and one lead may have three of them working at once. With Settings > Chat > "Pin a lead's family" set to Yes (it is No by default), pinning a lead also pins its open sessions, lead first, and unpinning the lead unpins them all, with one Undo. `create_session` takes a `role`, which starts the session on the model and effort your preset sets for it, plus optional `model`, `effort` and `preset` that override it; the session carries `ae.role` and `ae.preset` labels (see `docs/LABELS.md`).

**Where:** The filter icon on the Sessions header, then Grouping > Family; the dot before a session's title, or Show > Family dots to turn the dots off; Settings > Chat > Pin a lead's family.<br>
**Built from:** core patches [`0161`](../../patches/0161-ae-patch-Mark-session-families-in-the-sidebar-and-co.patch), [`0168`](../../patches/0168-ae-patch-Show-Grouping-Family-as-tree-rows-families-.patch), [`0171`](../../patches/0171-ae-patch-Call-the-Grouping-Family-section-for-sessio.patch), [`0175`](../../patches/0175-ae-patch-Fold-a-family-s-closed-sessions-into-one-ro.patch), [`0184`](../../patches/0184-ae-patch-Show-only-the-name-on-a-family-s-lead-row-a.patch), [`0185`](../../patches/0185-ae-patch-Archive-a-family-s-sessions-with-its-lead-f.patch), [`0187`](../../patches/0187-ae-patch-Replace-a-family-child-s-role-chip-and-rais.patch), [`0188`](../../patches/0188-ae-patch-Pin-a-family-s-open-sessions-with-its-lead-.patch), [`0191`](../../patches/0191-ae-patch-Give-a-family-lead-row-fixed-right-hand-slo.patch); [`packages/ae_omni_policies`](../../packages/ae_omni_policies)

## A family at a glance

<p align="center"><img src="../media/family-hover.jpg" alt="A family at a glance"></p>

Hover a lead row and the card opens with a Family block: the preset, the sessions it leads and how many are still open, and the family's total spend as Spend shows sets it. Hover a helper to see its role, model, effort and spend, and the session that started it.

**Where:** The hover card on any row in Grouping > Family.<br>
**Built from:** core patches [`0172`](../../patches/0172-ae-patch-Show-each-agent-s-role-model-effort-and-tok.patch), [`0184`](../../patches/0184-ae-patch-Show-only-the-name-on-a-family-s-lead-row-a.patch), [`0189`](../../patches/0189-ae-patch-Show-a-lead-s-family-as-labelled-rows-at-th.patch)

## Preset insights

<p align="center"><img src="../media/roles-insights.jpg" alt="Preset insights"></p>

Settings > Roles ends with Preset insights. Its switch, "Collect run logs and review presets", logs each run (time, cost, raises and the final review) and has a reviewer suggest preset changes on a schedule. The block shows the last and next review with Run now, and your suggestions with Apply, Undo and Dismiss; an admin chooses whether the review reads everyone's runs or only their own. The Admin Center's Presets card shows the same runs as numbers.

**Where:** Preset insights, the last block of Settings > Roles.<br>
**Built from:** core patches [`0181`](../../patches/0181-ae-patch-Tell-the-lead-to-report-its-final-review-an.patch), [`0182`](../../patches/0182-ae-patch-Add-preset-insights-block-to-Settings-Roles.patch); [`packages/ae_omni_policies`](../../packages/ae_omni_policies)

---

Next: [Triage and policies](triage-and-policies.md)
