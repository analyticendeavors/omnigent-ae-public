// The Social view's Settings fold (2026-11-06, part 2): every rule (POST /social/rules) and every
// tool (POST /social/tools). Every setting carries a hint written for Reid, not for a developer
// (the info control from hint.ts), because a number with no explanation is a number nobody changes.
//
// The fold draws once when it opens and again after a save, from the server's answer; a live
// re-read of the card never redraws it, so a half-typed value is not lost.

import type { ExtensionContext } from "@omnigent/extension-sdk";
import { describeError, saveSocialRules, saveSocialTools, type SocialRules, type SocialTool, type SocialView } from "./api";
import { button, el, notice, replaceChildren } from "./dom";
import { hint } from "./hint";

type NumberKey =
  | "short_li_days"
  | "tool_every_days"
  | "expert_per_week"
  | "expert_weekday"
  | "li_weekly_target"
  | "gap_days";
type BoolKey = "video_li" | "video_community" | "short_li" | "live_li";

interface BoolField {
  key: BoolKey;
  label: string;
  hint: string;
}
interface NumberField {
  key: NumberKey;
  label: string;
  min: number;
  max: number;
  hint: string;
}

export const BOOL_FIELDS: BoolField[] = [
  {
    key: "video_li",
    label: "Long video: LinkedIn post on publish day",
    hint: "Plans one LinkedIn post for every long video, due the day it goes public.",
  },
  {
    key: "video_community",
    label: "Long video: YouTube Community post",
    hint: "Plans one YouTube Community post for every long video, due the day it goes public.",
  },
  {
    key: "short_li",
    label: "Shorts: LinkedIn post",
    hint: "Plans a LinkedIn post for each Short. They are optional: skipping one never counts against you.",
  },
  {
    key: "live_li",
    label: "Livestream: announce the day before",
    hint: "Plans a LinkedIn post the day before each livestream, at the alarm time.",
  },
];

export const NUMBER_FIELDS: NumberField[] = [
  {
    key: "short_li_days",
    label: "Short: LinkedIn window (days)",
    min: 0,
    max: 30,
    hint: "How many days after a Short goes public you can still post it and have it count as promoted. 0 means the same day only.",
  },
  {
    key: "tool_every_days",
    label: "Tool promo every (days)",
    min: 1,
    max: 365,
    hint: "How often one of your tools gets a promo post. Each tool takes its turn in order, the one promoted longest ago first.",
  },
  {
    key: "expert_per_week",
    label: "Expert posts per week",
    min: 0,
    max: 7,
    hint: "How many posts a week are your own ideas, not a video or a tool. The weekly run drafts a topic for each.",
  },
  {
    key: "li_weekly_target",
    label: "Weekly LinkedIn target",
    min: 0,
    max: 14,
    hint: "How many LinkedIn posts you want to publish each week. A week that hits this counts toward your streak. Extra posts are added until the plan reaches it.",
  },
  {
    key: "gap_days",
    label: "Warn when a channel has no upload for (days)",
    min: 1,
    max: 60,
    hint: "Shows a warning on the card when a YouTube channel has nothing public or scheduled within that many days.",
  },
];

export const WEEKDAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"];
export const WEEKDAY_LABEL = "Expert post day";
export const WEEKDAY_HINT = "Which day the expert post is due. Extra expert posts that fill a short week land on other days.";

const TIME_HINT = "The time of day your posts are due, and when the phone alarm rings on a day with posts.";
const TZ_HINT = "The clock that decides what day it is and when the alarm rings, written as a zone name such as America/Los_Angeles.";
const ACTIVE_HINT = "Turn off to drop this tool from the rotation of promo posts. Posts already planned for it stay.";
const IMAGE_HINT = "A link to the picture that goes in the phone's share sheet with a post about this tool. It must be https and on a host the server allows (a GitHub social preview, or one named in AE_SOCIAL_IMAGE_HOSTS). Leave it empty to share the text only.";
const NOTE_HINT = "A line the drafter reads before writing about this tool, such as how to frame it (a preview, not a release). Up to 200 characters.";

function row(label: string, control: HTMLElement, tip: string, id: string): HTMLElement {
  return el("div", { class: "sv-set-row" }, el("label", { for: id }, label), control, hint(tip, label));
}

/** The fields that differ from *rules*, ready for POST /social/rules; null when nothing changed. */
export function rulesPatch(form: HTMLElement, rules: SocialRules): Partial<SocialRules> | null {
  const patch: Record<string, boolean | number | string> = {};
  const input = (key: string) => form.querySelector<HTMLInputElement>(`[data-rule="${key}"]`);
  for (const field of BOOL_FIELDS) {
    const node = input(field.key);
    if (node && node.checked !== rules[field.key]) patch[field.key] = node.checked;
  }
  for (const field of NUMBER_FIELDS) {
    const node = input(field.key);
    if (!node) continue;
    const value = Number(node.value);
    if (node.value.trim() === "" || !Number.isInteger(value) || value < field.min || value > field.max) {
      throw new Error(`${field.label}: enter a whole number from ${field.min} to ${field.max}.`);
    }
    if (value !== rules[field.key]) patch[field.key] = value;
  }
  const day = form.querySelector<HTMLSelectElement>('[data-rule="expert_weekday"]');
  if (day && Number(day.value) !== rules.expert_weekday) patch.expert_weekday = Number(day.value);
  const time = input("alarm_hhmm");
  if (time && time.value !== rules.alarm_hhmm) {
    if (!/^([01]\d|2[0-3]):[0-5]\d$/.test(time.value)) throw new Error("Phone alarm time: pick a time such as 09:00.");
    patch.alarm_hhmm = time.value;
  }
  const tz = input("tz");
  if (tz && tz.value.trim() !== rules.tz) {
    if (!tz.value.trim()) throw new Error("Time zone: enter a zone name such as America/Los_Angeles.");
    patch.tz = tz.value.trim();
  }
  return Object.keys(patch).length ? (patch as Partial<SocialRules>) : null;
}

/** Set every rule control to *rules* (after a save, from the server's answer). */
export function fillRules(form: HTMLElement, rules: SocialRules): void {
  for (const field of BOOL_FIELDS) {
    const node = form.querySelector<HTMLInputElement>(`[data-rule="${field.key}"]`);
    if (node) node.checked = rules[field.key];
  }
  for (const field of NUMBER_FIELDS) {
    const node = form.querySelector<HTMLInputElement>(`[data-rule="${field.key}"]`);
    if (node) node.value = String(rules[field.key]);
  }
  const set = (key: string, value: string) => {
    const node = form.querySelector<HTMLInputElement | HTMLSelectElement>(`[data-rule="${key}"]`);
    if (node) node.value = value;
  };
  set("expert_weekday", String(rules.expert_weekday));
  set("alarm_hhmm", rules.alarm_hhmm);
  set("tz", rules.tz);
}

/** The tool list with the form's edits applied. */
export function toolsFrom(form: HTMLElement, tools: SocialTool[]): SocialTool[] {
  return tools.map((tool) => {
    const active = form.querySelector<HTMLInputElement>(`[data-tool="${tool.id}"][data-field="active"]`);
    const note = form.querySelector<HTMLInputElement>(`[data-tool="${tool.id}"][data-field="note"]`);
    const image = form.querySelector<HTMLInputElement>(`[data-tool="${tool.id}"][data-field="image"]`);
    return {
      ...tool,
      active: active ? active.checked : tool.active,
      note: note ? note.value.trim() || null : tool.note,
      // An older server sends no `image`; an empty box on such a tool adds nothing to the save.
      ...(image && (image.value.trim() || tool.image !== undefined) ? { image: image.value.trim() || null } : {}),
    };
  });
}

export interface SettingsHooks {
  context: ExtensionContext;
  /** Read the card again after a save. */
  changed: () => Promise<void>;
}

/** Draw the fold's body from *view*'s rules and tools. */
export function drawSettings(host: HTMLElement, view: SocialView, hooks: SettingsHooks): void {
  const status = el("div", { class: "sv-set-status", "aria-live": "polite" });
  const say = (text: string, tone: "error" | "info" | "ok") => replaceChildren(status, [notice(text, tone)]);
  // The last saved values: a save takes the server's answer as the new baseline, so a second save
  // that changes a value back is still seen as a change.
  let rules = view.rules;
  let tools = view.tools;
  let ruleCount = 0;
  const fieldId = (key: string) => `sv-rule-${key}-${++ruleCount}`;

  /** The save worked; a card refresh that fails after it is reported on its own. */
  const refreshed = async (lead: string): Promise<void> => {
    try {
      await hooks.changed();
    } catch (error) {
      say(`${lead}; the card could not refresh: ${describeError(error)}. Reload the page to see the newest numbers.`, "info");
    }
  };

  const rulesForm = el("div", { class: "sv-set-group", "data-group": "rules" });
  rulesForm.appendChild(el("h4", { class: "social-h" }, "Rules"));
  for (const field of BOOL_FIELDS) {
    const id = fieldId(field.key);
    const box = el("input", { type: "checkbox", id, "data-rule": field.key });
    box.checked = rules[field.key];
    rulesForm.appendChild(row(field.label, box, field.hint, id));
  }
  for (const field of NUMBER_FIELDS) {
    const id = fieldId(field.key);
    const num = el("input", { type: "number", id, "data-rule": field.key, min: String(field.min), max: String(field.max), step: "1" });
    num.value = String(rules[field.key]);
    rulesForm.appendChild(row(field.label, num, field.hint, id));
  }
  const dayId = fieldId("expert_weekday");
  const day = el("select", { id: dayId, "data-rule": "expert_weekday" });
  WEEKDAY_NAMES.forEach((name, n) => day.appendChild(el("option", { value: String(n) }, name)));
  day.value = String(rules.expert_weekday);
  rulesForm.appendChild(row(WEEKDAY_LABEL, day, WEEKDAY_HINT, dayId));
  const timeId = fieldId("alarm_hhmm");
  const time = el("input", { type: "time", id: timeId, "data-rule": "alarm_hhmm" });
  time.value = rules.alarm_hhmm;
  rulesForm.appendChild(row("Phone alarm time", time, TIME_HINT, timeId));
  const tzId = fieldId("tz");
  const tz = el("input", { type: "text", id: tzId, "data-rule": "tz", maxlength: "64", spellcheck: "false" });
  tz.value = rules.tz;
  rulesForm.appendChild(row("Time zone", tz, TZ_HINT, tzId));
  rulesForm.appendChild(
    button("Save rules", {
      kind: "primary",
      title: "Save the rules above; the plan is redrawn from them",
      onClick: async () => {
        replaceChildren(status, []);
        try {
          const patch = rulesPatch(rulesForm, rules);
          if (!patch) {
            say("No rule changed.", "info");
            return;
          }
          const answer = await saveSocialRules(hooks.context, patch);
          // The server answers with the rules it now holds; that is the new baseline.
          rules = answer.rules ?? { ...rules, ...patch };
          fillRules(rulesForm, rules);
          say("Rules saved.", "ok");
          await refreshed("Saved");
        } catch (error) {
          say(error instanceof Error && !("status" in error) ? error.message : `The rules did not save (${describeError(error)}). Nothing changed; check the values and try again.`, "error");
        }
      },
    }),
  );

  const toolsForm = el("div", { class: "sv-set-group", "data-group": "tools" });
  toolsForm.appendChild(el("h4", { class: "social-h" }, "Tools to promote"));
  for (const tool of tools) {
    const activeId = fieldId(`${tool.id}-active`);
    const noteId = fieldId(`${tool.id}-note`);
    const active = el("input", { type: "checkbox", id: activeId, "data-tool": tool.id, "data-field": "active" });
    active.checked = tool.active;
    const note = el("input", { type: "text", id: noteId, maxlength: "200", "data-tool": tool.id, "data-field": "note", placeholder: "How to frame it" });
    note.value = tool.note ?? "";
    const imageId = fieldId(`${tool.id}-image`);
    const image = el("input", { type: "url", id: imageId, maxlength: "290", "data-tool": tool.id, "data-field": "image", placeholder: "https://..." });
    image.value = tool.image ?? "";
    toolsForm.appendChild(
      el(
        "div",
        { class: "sv-set-tool" },
        row(`${tool.name}: in the rotation`, active, ACTIVE_HINT, activeId),
        row(`${tool.name}: preview framing`, note, NOTE_HINT, noteId),
        row(`${tool.name}: image link`, image, IMAGE_HINT, imageId),
      ),
    );
  }
  toolsForm.appendChild(
    button("Save tools", {
      kind: "primary",
      title: "Save the tool switches, framing lines and image links",
      onClick: async () => {
        replaceChildren(status, []);
        try {
          const next = toolsFrom(toolsForm, tools);
          await saveSocialTools(hooks.context, next);
          tools = next;
          say("Tools saved.", "ok");
          await refreshed("Saved");
        } catch (error) {
          say(`The tools did not save (${describeError(error)}). Nothing changed; try again.`, "error");
        }
      },
    }),
  );
  replaceChildren(host, [rulesForm, toolsForm, status]);
}
