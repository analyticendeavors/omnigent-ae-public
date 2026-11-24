// The "New models" fold inside the Spend card's Presets tab (2026-11-10): the Claude model ids the
// box's watch (model_watch_relay.py, a host timer every six hours) found inside Claude Code and
// asked a one-word question, with what answered, when it was first seen, its preset review and
// the release-notes link. The server decides what is stale (`report_stale`); this only draws it.
// Nothing here changes a preset: the review's suggestions live in the Suggestions fold, and a
// person applies them.

import type { ModelWatchAnswer, ModelWatchModel } from "./api";
import { el } from "./dom";
import { fold } from "./fold";

export const MODEL_WATCH_HELP =
  "Every 6 hours the box looks inside its Claude Code for model ids it has not seen and sends each one a one-word question. Only ids that answer on your plan are listed. A new one joins the model pickers, rings your phone and gets a preset review. Presets are never changed for you.";

export const MODEL_WATCH_STALE =
  "The box has not reported in over 13 hours. Check that ae-omnigent-model-watch.timer is running on the box (systemctl --user status ae-omnigent-model-watch.timer).";

/** The models that answer on the plan, newest first seen first. */
export function servedModels(data: ModelWatchAnswer | null): ModelWatchModel[] {
  return (data?.models ?? [])
    .filter((m) => m.probe === "served")
    .sort((a, b) => Date.parse(b.first_seen ?? "") - Date.parse(a.first_seen ?? "") || 0);
}

/** Ids seen in Claude Code that the plan does not serve (yet). */
export function unservedModels(data: ModelWatchAnswer | null): ModelWatchModel[] {
  return (data?.models ?? []).filter((m) => m.probe !== "served");
}

/** One plain line for the fold's head. */
export function modelWatchLine(data: ModelWatchAnswer): string {
  const served = servedModels(data).length;
  if (!served) return "New models: none yet";
  return `New models: ${served} on your plan`;
}

/** "Review done, 3 suggestions", "Review running", or why it did not run. */
export function reviewText(model: ModelWatchModel): string {
  const review = model.review;
  if (!review) return "No review yet";
  if (review.state === "started") return "Review running";
  if (review.state === "done") {
    const n = review.count ?? 0;
    return `Review done, ${n} suggestion${n === 1 ? "" : "s"}`;
  }
  return `Review did not run${review.why ? `: ${review.why}` : ""}`;
}

/** "Nov 10" in the viewer's zone, or "-". */
function day(iso: string | null): string {
  const when = iso ? new Date(iso) : null;
  if (!when || Number.isNaN(when.getTime())) return "-";
  return when.toLocaleDateString("en-US", { month: "short", day: "numeric" });
}

function notesLink(model: ModelWatchModel): HTMLElement {
  const notes = model.notes;
  if (!notes || !/^https:\/\//.test(notes.url)) return el("span", { class: "meta" }, "No release notes yet");
  const link = el(
    "a",
    { href: notes.url, target: "_blank", rel: "noopener noreferrer" },
    "Release notes",
  );
  return notes.status === "ok"
    ? link
    : el(
        "span",
        {},
        link,
        el(
          "span",
          { class: "meta" },
          notes.status === "not_mentioned" ? " (does not mention it yet)" : " (could not be read)",
        ),
      );
}

function row(model: ModelWatchModel, openSuggestions: () => void): HTMLElement {
  const count = model.review?.state === "done" ? (model.review.count ?? 0) : 0;
  const see = count
    ? el(
        "button",
        {
          type: "button",
          class: "btn btn-quiet",
          title: "Open the Suggestions fold below",
        },
        `See ${count} suggestion${count === 1 ? "" : "s"}`,
      )
    : null;
  see?.addEventListener("click", openSuggestions);
  return el(
    "div",
    { class: "presets-take modelwatch-row" },
    el("b", {}, `${model.label} `, el("code", {}, model.id)),
    el(
      "div",
      { class: "presets-why meta" },
      `First seen ${day(model.first_seen)}. Answers on your plan. ${reviewText(model)}.`,
    ),
    el("div", { class: "presets-take-act" }, see, notesLink(model)),
  );
}

/**
 * The fold, or null before the first read (or when the read failed, so the card is as it was).
 * `openSuggestions` opens and scrolls to the Suggestions fold.
 */
export function modelWatchFold(
  data: ModelWatchAnswer | null,
  openSuggestions: () => void,
): HTMLElement | null {
  if (!data) return null;
  const served = servedModels(data);
  const unserved = unservedModels(data);
  const stale = data.report_stale === true;
  const content = el(
    "div",
    { class: "modelwatch" },
    el("p", { class: "meta modelwatch-help" }, MODEL_WATCH_HELP),
    stale
      ? el("p", { class: "meta presets-error", role: "alert" }, MODEL_WATCH_STALE)
      : null,
    served.length
      ? el("div", { class: "presets-takes" }, ...served.map((m) => row(m, openSuggestions)))
      : el("p", { class: "meta" }, "No new model has answered on your plan yet."),
    unserved.length
      ? el(
          "p",
          { class: "meta modelwatch-unserved" },
          `Seen in Claude Code, not served on your plan yet: ${unserved.map((m) => m.label).join(", ")}.`,
        )
      : null,
  );
  return fold(
    "models",
    "presets-models",
    [
      modelWatchLine(data),
      stale ? el("span", { class: "presets-fold-warn" }, ", timer silent") : null,
    ],
    content,
  );
}
