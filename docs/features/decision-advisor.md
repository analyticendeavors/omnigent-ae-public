# Decision-1 preset advisor

<sub>Decision-1 preset advisor &middot; [All features](README.md)</sub>

In a lead-picks chat the lead chooses a preset from each one's "when to use" note. The advisor gives it a second opinion from a judge model (Microsoft Decision-1) before it picks. The lead sends a one-line neutral summary of the task and a few counts; the server asks the judge which of the owner's first eight presets fits and how big the task is, and answers `use` with a preset or `decide_yourself`. The advisor never blocks a turn: any failure, refusal or low confidence is `decide_yourself`, and the lead picks as it always did. Added 2026-11-06.

## What the lead is told

When the advisor is on for the owner, the first line of the lead-picks notice (above the preset list) reads: "Step 1, for every task including small ones: before you pick a preset or start work, call advise_preset with a one-line neutral summary of the task that describes the kind of work, not its subject (no client, person or product names, no quoted text) and rough counts. In Claude Code it is a deferred tool: load it first with ToolSearch query "select:mcp__ae__advise_preset" (Codex has no ToolSearch; call it directly). If it answers use, take that preset and say "Decision-1 picked X"; if decide_yourself, or the tool is missing or fails, pick as below and say so in one line." The server decides whether the line renders (the `advisor` flag in the roles); a fixed preset never gets it. Made a required first step 2026-11-06, after a smoke test where 3 of 3 leads skipped the old "Before picking" line. It is still a prompt: a lead can skip it, which the `advisor_on` field below makes countable.

## What leaves the server

Only the lead's one-line summary, the counts (files, repos), the kind and two yes/no flags, and the preset "when to use" texts. Raw user messages never go out, and neither does a repository name. The key stays on the server: the lead calls the `advise_preset` tool, which posts to `POST /v1/ae/sessions/{id}/advise` as the calling session.

The guard refuses with a reason code and never repeats the text that matched, in a reply, a log or a run line. It refuses a summary that:

- matches a term in the private denylist file (`AE_DECISION_DENYLIST_FILE`, one term per line, whole words, any case). With no file, or an empty one, nothing is sent;
- has a capitalized word (any script, and a capital after a hyphen) that is not on the allow list (`AE_DECISION_TERM_ALLOW`, plus a few built-in tech terms), which catches names. The first word is checked too: only a common leading verb (Fix, Add, Update and similar) or an allowed term may start the summary. A multi-word allow entry such as `Power BI` matches as a whole phrase only. Runs of spaces and tabs are collapsed before the denylist match;
- appears inside any user message in the session, so the lead cannot paste the user's words through. Messages the harness has not taken yet are read too. If the transcript cannot be read to the end, or a lead has no user text at all, nothing is sent (`no_transcript`);
- holds a newline, a link or any domain-shaped word (`name.co.uk`; versions such as `3.11` pass), an email, a path, a quote, a backtick, or more than three digits in a run or in groups split by spaces, dashes or dots (small counts such as "4 files" pass).

Everything sent for a preset (its id and its "when to use" or description text) goes through the denylist and the name check; a preset that fails is left out of the choices and the call still goes ahead with the rest. Owner-written preset text may start a sentence with any word. Helpers (child sessions) get `decide_yourself` (`helper`), and a session is cut off after 10 calls an hour (`rate_limited`). The guard cannot catch a lower-case paraphrase of a client's facts or a name that is neither capitalized nor on the denylist, so the rule still rests on the lead; the guard catches slips.

## The verdict

`use` only when the top choice is a real preset (not `abstain`), at least `AE_DECISION_MIN_CONFIDENCE` (0.6) and at least 0.15 ahead of the next choice. The reply names the preset, its title and the confidence, and carries the size for information only (the label of the most likely level, left out when no level holds half the weight); size never changes a preset's review rules. The lead says "Decision-1 picked X" when it takes the advice. The call has a 3 second timeout and no retry.

## Turning it on

Off unless `AE_DECISION_ENABLED=1`, `AE_DECISION_ENDPOINT` and `AE_DECISION_API_KEY` are set, the session's owner is in `AE_DECISION_USERS`, and the denylist file has a term. `deploy/pull-secrets.sh` reads the key and the endpoint from Key Vault (`foundry-decision-api-key`, `foundry-decision-endpoint`); the other settings are in `config/server.env.example`. When it is off the lead's notice has no extra line and the tool answers `decide_yourself`.

## Turn it on or off (2026-11-07)

It is off unless every line is set. In `server.env`:

```
AE_DECISION_ENABLED=1
AE_DECISION_ENDPOINT=...           # from the secrets file
AE_DECISION_API_KEY=...            # from the secrets file
AE_DECISION_USERS=you@example.com
AE_DECISION_DENYLIST_FILE=/data/ae-decision-denylist.txt
AE_DECISION_POINTS=agent_ask:off      # optional, per point
AE_DECISION_RETIRES=2027-02-04        # optional, shown on the card
```

To switch it off, remove `AE_DECISION_ENABLED` (or set it to 0) and restart; `AE_DECISION_POINTS` turns single points off while the rest stay on.

`GET /v1/ae/decision` answers `configured`, `set_up` (any setting is present or it is enabled; the card shows Not set up only when this is false), `enabled`, `available` (for the caller), `model`, `retires`, `calls_today`, `daily_cap`, `paused_until`, `points` (name and mode) and `shadow` (`would_act_7d`, `lines_7d`). When `available` is false it adds one `reason`, the first that applies in this order:

- `not_enabled`: `AE_DECISION_ENABLED` is not set.
- `no_endpoint`: `AE_DECISION_ENDPOINT` is empty.
- `no_key`: `AE_DECISION_API_KEY` is empty.
- `no_denylist`: the denylist file is unset, missing or empty.
- `no_users`: `AE_DECISION_USERS` is empty. Set it to the allowed user ids; on a single-user server the one user is `local`.
- `not_allowed`: the caller is not in `AE_DECISION_USERS`.

The denylist is required on purpose. The guard fails closed: with no terms it cannot tell a client name from a tech word, so it refuses every call rather than risk sending one out. Nothing secret is in the answer, and it is cached for 60 seconds per caller.

When it is not available, agents never see `advise_preset` or `ask_judge`: the MCP server hides them until the route says `available`, and the Lead picks "Step 1" line (patch 0295) already depends on the same check. A chat that is already open keeps the tool list it started with until it restarts; a hidden tool that is called anyway answers `decide_yourself` or an error, as before. The Admin Center's Decision model card shows this state; see [Admin Center](admin-center.md#decision-model-2026-11-07).

## The run log

Each call adds an entry to the preset run's `advisor` list (at most 10 a run): time, a hash of the summary and its length, the counts, the preset and size probabilities, the verdict, the latency and an error class or refusal code. The summary text is kept only with `AE_DECISION_LOG_SUMMARY=1`, and only for a call that was sent. The write is best effort: it never delays the reply, and an entry is skipped if the run log is busy. Nothing is kept while the owner's preset insights are off. Read the probabilities after a week to tune the thresholds.

A lead-picks run line also carries `advisor_on`: true when the advisor was available for the owner when the line was written, false when it was off or the check failed. `advisor: []` with `advisor_on: true` is a lead that skipped the call. The field is absent for any other preset.

## Removing it

## Judge interface, decision points and the shadow log (2026-11-06)

**Why.** The judge model retires 2027-02-04, so nothing new names it. A decision point asks a `Judge` (`judge.py`): `yes_no`, `choice` or `score` in, a `Verdict` (answer, probabilities, latency, error class) out. `Decision1Judge` is the one adapter today; the endpoint has no yes/no type, so `yes_no` is a two-option choice over yes and no. `RuleJudge` wraps a plain function for the baseline and never does I/O. Moving to a new model is one new adapter.

**Data rule.** A judge sees only numbers, booleans, fixed enum labels and a guarded summary (a string only `guard_summary` can make, after the advisor's guard passes). Never raw user text, paths, URLs, names or hashes of text. Adapters check it again and answer `bad_state` without sending anything.

**Modes.** Each point (`decision_shadow.py`, `register_point`) declares its feature keys and types. Anything undeclared is dropped and counted. `AE_DECISION_POINTS` (for example `agent_ask:off`) is each point's ceiling: `off`, `shadow` (ask the judge, log it, the rule still acts) or `live` (the judge acts when it passes the point's threshold). A missing point is off. Without a promotion a point runs in shadow at most. A promotions file (`AE_DECISION_POINTS_FILE`, JSON of point to mode, default `ae-decision-points.json` next to the run log) raises a point up to its ceiling, read on each call with an mtime cache; a broken file caps every point at shadow. `AE_DECISION_ENABLED` off turns every point off. A point goes live only with the owner's one-tap approval.

**Safety.** `ask` never raises. It times out at 3 s (`AE_DECISION_POINT_TIMEOUT_S`), allows 400 calls a day and 30 a minute (`AE_DECISION_DAILY_CAP`, `AE_DECISION_MINUTE_CAP`), runs 4 at a time, and a breaker opens for 10 minutes after 5 errors in a row. Off, capped or failed means the baseline, `acted` false. `fire` runs `ask` in the background for hook paths that must not wait. No point is wired into a hook yet; they arrive in a later change.

**Shadow log.** `ae-decision-shadow.jsonl` in the data dir (`AE_DECISION_SHADOW_FILE`), one line per ask: `at` (UTC), `point`, `session`, `judge`, `judge_ver`, `mode`, `features` (declared only), `answer`, `probs`, `threshold`, `would_act`, `acted`, `baseline`, `latency_ms`, `error_class`, `dropped_features` (a count). No text, no hashes, no paths. For a point whose option names an agent writes (`agent_ask`), `answer` is the 0-based index of the chosen option or level (yes is 0, no is 1) or null, and `probs` is a list of probabilities rounded to 3 places in the order the agent gave them; the names themselves are never logged. The daily cap is shared by all points. Rotated to `.1` past 5 MB; lines older than 30 days are dropped at rotation and at most once a day otherwise.

## Lead step-down: dropped after its replay (2026-11-07)

**What it was.** The plan's first decision point: when an Opus lead's recent work turns mechanical (PR watching, CI waits, applying pinned fixes), the server would switch the lead to Sonnet. A live test on 2026-11-06 showed a mid-session switch applies on the next turn, and that it rewrites the prompt cache once (about 45k tokens).

**What the replay measured.** Stage 0 of the replay (no judge calls) took the 78 Opus-lead runs of the run log with a transcript (wall time 3 minutes or more, about 13.5 days up to 2026-11-07). It found every mechanical stretch, meaning 12 or more calls with no file edit and no planner, and priced each stretch's real tokens on Sonnet against Opus, minus the cache re-warm.

| Variant (saving as a share of lead-only Opus spend) | Perfect judge | Plain rule (every stretch) |
|---|---|---|
| Two-way: the lead returns to Opus after the stretch | 6.3% | -19% |
| Upper bound: no re-warm charged for the return to Opus | 21% | 20% |

Allowing small edits inside a stretch changes these by under 2 points. Two-way, a stretch breaks even only at about 68 calls, and only stretches of 80 calls or more save money.

**Decision.** Dropped (owner, 2026-11-07). The point needed 10% of lead-only Opus spend with a perfect judge, and the realistic two-way case reaches 6%, so no judge can make it pay. The upper-bound row ignores the cost of switching back, so it is not a real option; keeping a lead on Sonnet for good is a choice of lead model, which the Lead picks advisor already makes at the start. Stage 1 (asking Decision-1 on about 330 windows) was not run. The stage 0 code is kept unmerged on the branch `decision-replay`, for a successor model or a later look at stretches of 80 calls or more.

## The ask_judge tool (2026-11-06)

Any session (lead, helper, builder, reviewer, planner) can call `ask_judge` for a quick typed judgment: a `choice` between 2 to 6 named options, a `yes_no` gate, or a `score` on 2 to 8 ordered levels, with optional `facts` (up to 12 numbers, booleans or short words). It posts `POST /v1/ae/sessions/{id}/judge` as the calling session; the owner check is the advise route's, and no role is refused. Every text the agent writes (question, option keys and descriptions, levels, string facts) goes through the advisor's guard with the denylist, and compared with the session's user messages on lower-case words with punctuation, `_` and `-` removed: refused when a whole user message of two or more words sits inside the text, when a text of two or more words sits inside a user message, or when they share a run of six or more words (a one-word text is never refused for appearing in a message). Option keys and fact keys get the same guard, with `_` read as a space. The denylist also matches across `_` and `-` (so `rebill_contoso` hits `contoso`), for the advisor too. For a helper or other child, the user messages of its lead and every ancestor are checked too; if any cannot be read the call answers `no_transcript`. A session with more than 500 messages also answers `no_transcript` (fail closed). Levels and string facts are at most 40 characters. A refusal answers `error: refused_<code>` and never the matched text. 20 calls per session per hour.

The reply is `answer`, `probs` and `low_confidence` (the advisor's rule: top choice at least `AE_DECISION_MIN_CONFIDENCE` and 0.15 ahead). On any error the reply is `answer: null`, `low_confidence: true` and an `error` code, with HTTP 200. It returns probabilities, not reasons; the agent treats `low_confidence` as "decide yourself". The point is `agent_ask`, live by default when the advisor is on for the owner (the answer only goes back to the agent); `AE_DECISION_POINTS=agent_ask:off` is the switch. Its shadow line keeps only `kind`, `n_options`, `n_facts` and `role`; the question, options and facts are never written.

**What the copy check reads (2026-11-06).** Every user message in the session except stored items the harness stamped `is_meta` (or with a `subagent_return_id`): subagent hand-backs and task notifications. A lead holds many of these (one live lead had 52 user texts and 6 were the owner's), and they carry plain phrases such as "files changed", so the check refused ordinary questions (`refused_key` on `files_changed`). The stamp is trusted, never the text: a prefix match let an owner paste a note, type below it and escape the check. Queued inputs are always counted. The check also covers ancestors and direct children (the owner can type into a child chat; archived and sub-agent children (an owner can type into a Codex side chat) and every owner's rows count, matched with or without the `conv_` prefix; up to 100 children, and more, an unreadable child or a scan past 5,000 sessions means `no_transcript`). A child's brief is counted, so a false refusal is possible and safe. Each text the lead delivered to a child (an Agent prompt, a create_session brief, a SendMessage or post_message follow-up) cancels one equal child message (2026-11-06), so an owner message the lead echoes still counts once (a delivery call that never landed still cancels a copy; outputs are not read): it is the agent's own writing, and counting it made every lead that had started sub-agents refuse plain questions. Nothing is skipped by label, since labels are client-writable and the server does not stop a message posted into a sub-agent mirror; such a message is not a lead delivery, so it still counts. The check guards against careless copying; an agent set on evading it can rephrase, which no copy check stops. Sub-agent mirrors have their own cap of 400, other children 100. Still open: owner text that lives only in a sibling or an unrelated chat is not checked. A session left with no counted text fails closed as `no_transcript`.

The judge model retires 2027-02-04. To remove the feature, delete `decision_advisor.py`, `judge.py`, `decision_shadow.py`, `agent_judge.py`, their routes in `router.py`, the `advise_preset` tool in `mcp.py`, the `advisor` list in `preset_runs.py`, the secrets block in `deploy/pull-secrets.sh`, and revert the core patch below.

**Where:** A lead-picks chat; nothing shows in the UI.<br>
**Built from:** core patches [`0294`](../../patches/0294-ae-patch-Tell-a-lead-picks-lead-to-ask-the-preset-ad.patch), [`0295`](../../patches/0295-ae-patch-Decide-the-preset-advisor-on-the-server-so-.patch), [`0296`](../../patches/0296-ae-patch-Fix-the-line-length-in-the-advisor-availabi.patch), [`0298`](../../patches/0298-ae-patch-Make-the-Decision-1-advisor-call-a-required.patch)), [`0299`](../../patches/0299-ae-patch-Tell-the-lead-the-advisor-summary-describes.patch); [`packages/ae_omni_policies`](../../packages/ae_omni_policies)
