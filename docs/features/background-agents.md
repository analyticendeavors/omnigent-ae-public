# Background agents

<sub>Agents and pull requests &middot; [All features](README.md)</sub>

Stock Omnigent lists a session's subagents in the Agents tab of the right panel. The fork lays that tab out like Claude Desktop's background tasks, keeps each helper's state honest while it runs, fails or is closed, and lets you choose how the lead works with other agents.

## An Agents pane like Claude Desktop's

<p align="center"><img src="../media/agents-pane.webp" alt="An Agents pane like Claude Desktop's"></p>

The lead sits on top, then Running helpers with a live timer and the last line each one printed, then a counted Finished group you can open or clear. You can see at a glance what is still working and for how long. The graph view lays out left to right so a tall pane stays readable.

**Where:** The Agents tab in the session's right panel.<br>
**Built from:** core patch [`0055`](../../patches/0055-ae-patch-Lay-the-Agents-pane-out-like-Claude-Desktop.patch)

## The model each agent ran on

<p align="center"><img src="../media/agents-overview.jpg" alt="The model each agent ran on"></p>

Every row has a Model column with the model the helper actually ran on, written as a name (Haiku 5.5, Sonnet 5.5), including Claude Code's own subagents and Codex collaborators.

**Where:** The Model column in the Agents tab.<br>
**Built from:** core patch [`0063`](../../patches/0063-ae-patch-Show-the-model-each-agent-ran-on-in-the-Age.patch)

## Running until the lead records its return

<p align="center"><img src="../media/agents-running.webp" alt="Running until the lead records its return"></p>

A Claude Code subagent goes quiet between steps, and stock Omnigent read each pause as finished, so rows flickered between Running and Finished. The fork keeps a subagent under Running until its parent records the return. A subagent that died before its first pause, or across a server restart, still settles instead of reading Running forever.

**Where:** The Agents tab, and the sidebar's waiting count.<br>
**Built from:** core patches [`0112`](../../patches/0112-ae-patch-Keep-a-Claude-Code-subagent-running-until-i.patch), [`0125`](../../patches/0125-ae-patch-Settle-a-Claude-subagent-that-died-before-i.patch)

## Failed helpers

<p align="center"><img src="../media/agents-failed.webp" alt="Failed helpers"></p>

A helper whose run failed moves under its own Failed heading, with a warning icon and its error line, instead of hiding among the finished ones. The state survives a server restart.

**Where:** The Failed group in the Agents tab.<br>
**Built from:** core patch [`0130`](../../patches/0130-ae-patch-Show-a-failed-subagent-under-its-own-Failed.patch)

## Waiting on agents

<p align="center"><img src="../media/agents-waiting.webp" alt="Waiting on agents"></p>

When the lead's own turn ends while helpers still run, the session reads as waiting: the sidebar row shows a pulsing hourglass with the number of agents, and a status line above the composer says "Waiting on 2 agents" with a chip per helper.

**Where:** The session's row in the sidebar, and the status line above the composer.<br>
**Built from:** core patch [`0074`](../../patches/0074-ae-patch-Show-when-a-session-is-working-quiet-waitin.patch)

## Closed helpers stop counting

<p align="center"><img src="../media/agents-closed.webp" alt="Closed helpers stop counting"></p>

A helper session that is closed drops out of its parent's count at once, so the hourglass only counts work that is still running. Before, a closed helper could keep its parent waiting for hours.

**Where:** The hourglass on the session's sidebar row.<br>
**Built from:** core patch [`0133`](../../patches/0133-ae-patch-Stop-counting-a-closed-subagent-toward-its-.patch)

## Who works on this

A control in the composer sets how the lead works with other agents. Auto brings in collaborators when useful, Solo sticks to the lead's own subagents, and Team lets you pick collaborators, such as Codex for an independent review, each with its own model. Change lead swaps the orchestrator itself.

**Where:** The Auto button in the composer, which opens the "Who works on this?" dialog.<br>
**Built from:** core patches [`0030`](../../patches/0030-ae-patch-Add-composer-teams-and-separate-model-conte.patch), [`0032`](../../patches/0032-ae-patch-Add-Auto-collaboration-and-hide-unconfigure.patch)

---

Previous: [Action dock](action-dock.md) &middot; Next: [Pull requests](pull-requests.md)
