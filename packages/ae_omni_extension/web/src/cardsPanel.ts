// The Cards panel (2026-11-07, card setup; the approved mockup A): a drawer from the right,
// 440 px wide on a wide page and full screen under 640 px, listing every card under its group
// with a state chip, a "Hide for me" eye and the on/off switch. Tapping a row unfolds its Setup:
// each field's label, hint, whether the server's environment sets it, and the variable name to
// set (never a value). The switch is the admin's: it turns the card on or off for everyone on
// the server. The eye is the viewer's own: it hides the card for them only (cardPrefs.ts).
//
// fabric.ts opens it (the header's Cards button and each card's gear); `notSetUp` is the hook for
// the "Needs setup" chip (board.ts cardNotSetUp).

import type { ExtensionContext } from "@omnigent/extension-sdk";
import { describeError, isAdminOnly } from "./api";
import { setCardHidden } from "./cardPrefs";
import {
  type CardField,
  type CardRow,
  type CardsAnswer,
  cardHidden,
  getCards,
  setCards,
} from "./cardState";
import { conflictAnswer, fetchCards, setCardSettings, setCardState } from "./cardsApi";
import { el } from "./dom";
import { createGitSetup } from "./gitSetup";
import { closeIcon, copyIcon, eyeIcon, eyeOffIcon, infoIcon, settingsIcon } from "./icons";
import { closeOpenHint, hasOpenHint, hint } from "./hint";
import { copyText } from "./lot";
import { DOMAINS } from "./tiles";
import "./cardsPanel.css";

export const DOCS_PATH = "docs/features/admin-center.md";
// The public mirror: the private repo's link is dead for anyone outside it.
const DOCS_URL = `https://github.com/analyticendeavors/omnigent-ae-public/blob/main/${DOCS_PATH}`;
export const INTRO =
  "The switch turns a card on or off for everyone on this server. The eye hides it for you only.";

export interface CardsPanelOptions {
  /** Open with this card's row scrolled into view and its Setup unfolded. */
  focus?: string;
  /** True when the board shows the card as not set up on this server (the "Needs setup" chip). */
  notSetUp?: (id: string) => boolean;
  /** The control that opened the panel; focus returns here on close (default: the active element). */
  opener?: HTMLElement | null;
}

export interface OpenCardsPanel {
  close: () => void;
  node: HTMLElement;
}

const FOCUSABLE = "button, [href], input, select, textarea, [tabindex]";

function turnedOffLine(row: CardRow): string | null {
  if (row.on || !row.changed_by) return null;
  const when = row.changed_at ? new Date(row.changed_at) : null;
  const day =
    when && !Number.isNaN(when.getTime())
      ? when.toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" })
      : null;
  return day ? `Turned off by ${row.changed_by} on ${day}` : `Turned off by ${row.changed_by}`;
}

export function openCardsPanel(
  context: ExtensionContext,
  options: CardsPanelOptions = {},
): OpenCardsPanel {
  const opener = options.opener ?? (document.activeElement as HTMLElement | null);
  const unfolded = new Set<string>(options.focus ? [options.focus] : []);
  let busy = false;
  let closed = false;
  // Typed but not yet saved, by card id then variable name (2026-11-07). A checkbox holds "1"/"0".
  const drafts = new Map<string, Map<string, string>>();
  // The server's refusal text, by card id then variable name; "" is the card as a whole.
  const problems = new Map<string, Map<string, string>>();

  const closeButton = el("button", {
    type: "button",
    class: "btn btn-quiet",
    "aria-label": "Close the Cards panel",
    title: "Close",
  });
  closeButton.appendChild(closeIcon());
  const note = el("p", { class: "ae-cards-note", role: "status", "aria-live": "polite", hidden: true });
  const list = el("div", { class: "ae-cards-list" });
  const title = el("h3", { id: "ae-cards-title" }, settingsIcon(16), "Cards");
  const drawer = el(
    "aside",
    { class: "ae-cards-drawer", role: "dialog", "aria-modal": "true", "aria-labelledby": "ae-cards-title" },
    el("div", { class: "ae-cards-top" }, title, closeButton),
    el(
      "div",
      { class: "ae-cards-body" },
      el("p", { class: "ae-cards-intro" }, infoIcon(14), INTRO),
      note,
      list,
    ),
  );
  const scrim = el("div", { class: "ae-cards-scrim" }, drawer);
  // The Git entry's tokens (gitSetup.ts). One object for the panel's life: render() rebuilds every
  // row, and a token typed there must survive that, so its node is moved into each new Git row.
  const gitSetup = createGitSetup(context, {
    say: (text) => say(text),
    refreshCards: async () => {
      setCards(await fetchCards(context));
      if (!closed) render();
    },
  });

  function say(text: string | null): void {
    note.textContent = text ?? "";
    note.hidden = !text;
  }

  function chipFor(row: CardRow): { text: string; key: string } {
    if (cardHidden(row.id)) return { text: "Hidden for you", key: "hidden" };
    if (!row.on) return { text: "Off", key: "off" };
    if (options.notSetUp?.(row.id)) return { text: "Needs setup", key: "setup" };
    return { text: "On", key: "on" };
  }

  const originalOf = (field: CardField): string =>
    field.kind === "bool" ? (field.value === "1" ? "1" : "0") : (field.value ?? "");

  function draftOf(row: CardRow, field: CardField): string {
    return drafts.get(row.id)?.get(field.env) ?? originalOf(field);
  }

  function changedEnvs(row: CardRow): string[] {
    return row.fields
      .filter((field) => field.editable && field.source !== "env")
      .filter((field) => draftOf(row, field) !== originalOf(field))
      .map((field) => field.env);
  }

  function refreshSave(row: CardRow): void {
    const button = list.querySelector<HTMLButtonElement>(`[data-card="${row.id}"] .ae-card-save`);
    if (button) button.disabled = busy || !getCards()?.state_ok || changedEnvs(row).length === 0;
  }

  function dayOf(iso: string | null | undefined): string | null {
    const when = iso ? new Date(iso) : null;
    return when && !Number.isNaN(when.getTime())
      ? when.toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" })
      : null;
  }

  function statusText(field: CardField): string {
    const source = field.source ?? (field.status === "set" ? "env" : "missing");
    if (source === "env") {
      if (typeof field.count === "number") {
        const unit = field.unit ?? "item";
        return `Set for ${field.count} ${unit}${field.count === 1 ? "" : "s"}`;
      }
      return field.editable
        ? "Set in the server's environment. Change it there."
        : "Set in the server's environment";
    }
    if (source === "saved") return "Saved in this panel";
    if (source === "default" && field.default != null) return `Default: ${field.default}`;
    return "Not set";
  }

  function inputFor(row: CardRow, field: CardField, frozen: boolean): HTMLElement {
    const id = `ae-card-in-${row.id}-${field.env}`;
    const common = {
      id,
      "data-env": field.env,
      "aria-label": field.label,
      placeholder: field.kind === "bool" ? undefined : (field.default ?? ""),
      disabled: frozen || busy,
    };
    const value = draftOf(row, field);
    let input: HTMLInputElement | HTMLTextAreaElement;
    if (field.kind === "list") {
      input = el("textarea", { ...common, rows: "2", class: "ae-card-input" }) as HTMLTextAreaElement;
      input.value = value;
    } else if (field.kind === "bool") {
      input = el("input", { ...common, type: "checkbox", class: "ae-card-check" }) as HTMLInputElement;
      (input as HTMLInputElement).checked = value === "1";
    } else {
      const type = field.kind === "number" ? "number" : field.kind === "date" ? "date" : "text";
      input = el("input", {
        ...common,
        type,
        ...(type === "number" ? { step: "any", inputmode: "decimal" } : {}),
        class: "ae-card-input",
      }) as HTMLInputElement;
      input.value = value;
    }
    input.addEventListener(field.kind === "bool" ? "change" : "input", () => {
      const next =
        field.kind === "bool" ? ((input as HTMLInputElement).checked ? "1" : "0") : input.value;
      const mine = drafts.get(row.id) ?? new Map<string, string>();
      mine.set(field.env, next);
      drafts.set(row.id, mine);
      problems.get(row.id)?.delete(field.env);
      list
        .querySelector(`[data-card="${row.id}"] [data-error-for="${field.env}"]`)
        ?.setAttribute("hidden", "");
      refreshSave(row);
    });
    return input;
  }

  function fieldBlock(row: CardRow, index: number, frozen: boolean): HTMLElement {
    const field = row.fields[index];
    const copy = el("button", {
      type: "button",
      class: "ae-card-copy",
      "aria-label": `Copy the variable name ${field.env}`,
      title: "Copy the variable name",
    });
    copy.appendChild(copyIcon());
    copy.addEventListener("click", () => {
      void copyText(field.env).then((ok) => say(ok ? `Copied ${field.env}.` : "Copy is not available here."));
    });
    const source = field.source ?? (field.status === "set" ? "env" : "missing");
    const typable = field.editable === true && source !== "env";
    const error = problems.get(row.id)?.get(field.env);
    const savedDay = dayOf(field.saved_at);
    const clear =
      typable && field.value != null
        ? el("button", { type: "button", class: "ae-card-clear", title: "Remove the saved value" }, "Clear")
        : null;
    clear?.addEventListener("click", () => void saveSettings(row, { [field.env]: null }));
    return el(
      "div",
      { class: "ae-card-field", "data-field": field.env },
      el("div", { class: "ae-card-field-label" }, field.label, hint(field.hint, field.label)),
      typable ? inputFor(row, field, frozen) : null,
      el(
        "div",
        { class: "ae-card-field-status", "data-status": field.status },
        statusText(field),
        field.value != null && typable && field.saved_by
          ? ` Saved by ${field.saved_by}${savedDay ? ` on ${savedDay}` : ""}.`
          : "",
        clear ? " " : null,
        clear,
      ),
      field.host && !typable
        ? el("div", { class: "ae-card-field-status" }, "Set on the host, in config/host.env")
        : null,
      el(
        "div",
        { class: "ae-card-error", role: "alert", "data-error-for": field.env, hidden: !error },
        error ?? "",
      ),
      typable ? null : el("div", { class: "ae-card-env" }, el("code", {}, field.env), copy),
    );
  }

  async function saveSettings(row: CardRow, only?: Record<string, string | null>): Promise<void> {
    const answer = getCards();
    if (!answer || busy) return;
    const values: Record<string, string | null> =
      only ??
      Object.fromEntries(
        changedEnvs(row).map((env) => {
          const field = row.fields.find((item) => item.env === env)!;
          const draft = draftOf(row, field);
          return [env, field.kind !== "bool" && draft.trim() === "" ? null : draft];
        }),
      );
    if (!Object.keys(values).length) return;
    busy = true;
    problems.delete(row.id);
    render();
    try {
      setCards(await setCardSettings(context, { id: row.id, values, revision: answer.revision }));
      const mine = drafts.get(row.id);
      for (const env of Object.keys(values)) mine?.delete(env);
      say("Saved.");
    } catch (error) {
      const current = conflictAnswer(error);
      if (current) {
        setCards(current);
        say("This changed elsewhere. The list now shows what is saved; your typing is kept.");
      } else {
        const message = describeError(error);
        const env = Object.keys(values).find((name) => message.startsWith(name));
        problems.set(row.id, new Map([[env ?? "", message]]));
        say(env ? null : message);
      }
    } finally {
      busy = false;
      render();
    }
  }

  function saveRow(row: CardRow, frozen: boolean): HTMLElement {
    const save = el(
      "button",
      {
        type: "button",
        class: "btn ae-card-save",
        title: frozen
          ? "Saving is paused until the server's card file is fixed"
          : `Save the changed settings for ${row.name}`,
        disabled: frozen || busy || changedEnvs(row).length === 0,
      },
      "Save",
    );
    save.addEventListener("click", () => void saveSettings(row));
    const general = problems.get(row.id)?.get("");
    return el(
      "div",
      { class: "ae-card-save-row" },
      save,
      general ? el("span", { class: "ae-card-error", role: "alert" }, general) : null,
    );
  }

  function rowNode(answer: CardsAnswer, row: CardRow): HTMLElement {
    const open = unfolded.has(row.id);
    const chip = chipFor(row);
    const setupId = `ae-card-setup-${row.id}`;
    const head = el(
      "button",
      { type: "button", class: "ae-card-row-head", "aria-expanded": String(open), "aria-controls": setupId },
      el("span", { class: "ae-card-row-name" }, row.name, el("span", { class: "ae-card-chip", "data-chip": chip.key }, chip.text)),
      el("span", { class: "ae-card-row-desc" }, row.description),
    );
    head.title = `Show how ${row.name} is set up`;
    head.addEventListener("click", () => {
      if (unfolded.has(row.id)) unfolded.delete(row.id);
      else unfolded.add(row.id);
      render();
      list.querySelector<HTMLElement>(`[data-card="${row.id}"] .ae-card-row-head`)?.focus();
    });

    const hiddenNow = cardHidden(row.id);
    const eye = el("button", {
      type: "button",
      class: "ae-card-eye",
      "aria-pressed": String(hiddenNow),
      "aria-label": hiddenNow ? `Show ${row.name} for me again` : `Hide ${row.name} for me`,
      title: hiddenNow ? "Show this card for me again" : "Hide this card for me only",
    });
    eye.appendChild(hiddenNow ? eyeOffIcon(16) : eyeIcon(16));
    eye.addEventListener("click", () => {
      setCardHidden(context, row.id, !cardHidden(row.id));
      render();
      list.querySelector<HTMLElement>(`[data-card="${row.id}"] .ae-card-eye`)?.focus();
    });

    const frozen = !answer.state_ok;
    if (row.id === "git") {
      gitSetup.update(row, frozen);
      if (open) gitSetup.load();
    }
    const toggle = el("button", {
      type: "button",
      class: "ae-card-switch",
      role: "switch",
      "aria-checked": String(row.on),
      "aria-label": `${row.name}: ${row.on ? "on" : "off"} for everyone`,
      title: frozen
        ? "The saved card state cannot be read, so switching is paused until the server's file is fixed"
        : `Turn ${row.name} ${row.on ? "off" : "on"} for everyone on this server`,
      disabled: frozen || busy,
    });
    toggle.append(
      el("span", { class: "ae-card-switch-track", "aria-hidden": "true" }, el("span", { class: "ae-card-switch-knob" })),
      el("span", { class: "ae-card-switch-word", "aria-hidden": "true" }, row.on ? "On" : "Off"),
    );
    toggle.addEventListener("click", () => void flip(row));

    const off = turnedOffLine(row);
    const docs = el("button", { type: "button", class: "btn btn-quiet ae-card-docs", title: "Open the Admin Center guide" }, "Read the guide");
    docs.addEventListener("click", () => {
      Promise.resolve(context.navigation?.openExternal?.(DOCS_URL)).catch(() => say(`Open ${DOCS_PATH} in the repository.`));
    });
    const setup = el(
      "div",
      { class: "ae-card-setup", id: setupId, hidden: !open },
      el("h4", {}, "Setup"),
      frozen && row.fields.some((field) => field.editable)
        ? el(
            "p",
            { class: "ae-card-field-status" },
            "Saving is paused: the server cannot read its saved card file.",
          )
        : null,
      // The Git card's token-per-account field is replaced by gitSetup's rows.
      ...row.fields.map((_f, index) =>
        row.id === "git" && row.fields[index].env === "AE_GIT_TOKEN_" ? null : fieldBlock(row, index, frozen),
      ),
      row.id === "git" ? gitSetup.node : null,
      row.fields.some((field) => field.editable && field.source !== "env") ? saveRow(row, frozen) : null,
      row.fields.length ? null : el("p", { class: "ae-card-field-status" }, "Nothing to set up."),
      el("div", {}, docs, " ", el("code", {}, DOCS_PATH)),
    );
    return el(
      "article",
      { class: "ae-card-row", "data-card": row.id, "data-focus": options.focus === row.id ? "" : undefined },
      el("div", { class: "ae-card-row-top" }, head, eye, toggle),
      off ? el("p", { class: "ae-card-off-by" }, off) : null,
      setup,
    );
  }

  function render(): void {
    const answer = getCards();
    if (!answer) return;
    list.replaceChildren();
    for (const domain of DOMAINS) {
      const rows = domain.cards
        .map((id) => answer.cards.find((row) => row.id === id))
        .filter((row): row is CardRow => row !== undefined);
      if (!rows.length) continue;
      list.appendChild(el("h4", { class: "ae-cards-group" }, domain.label));
      for (const row of rows) list.appendChild(rowNode(answer, row));
    }
  }

  async function flip(row: CardRow): Promise<void> {
    const answer = getCards();
    if (!answer || busy) return;
    busy = true;
    render();
    try {
      setCards(await setCardState(context, { id: row.id, on: !row.on, revision: answer.revision }));
      say(null);
    } catch (error) {
      const current = conflictAnswer(error);
      if (current) {
        setCards(current);
        say("This changed elsewhere. The list now shows what is saved.");
      } else {
        say(describeError(error));
      }
    } finally {
      busy = false;
      render();
    }
  }

  const onKey = (event: KeyboardEvent): void => {
    if (event.key === "Escape") {
      // An open hint takes this Escape first; the next one closes the panel.
      if (hasOpenHint()) return;
      event.preventDefault();
      event.stopPropagation();
      event.stopImmediatePropagation();
      close();
    } else if (event.key === "Tab") {
      const items = [...drawer.querySelectorAll<HTMLElement>(FOCUSABLE)].filter(
        (item) => !item.hasAttribute("disabled") && item.getAttribute("tabindex") !== "-1" && !item.closest("[hidden]"),
      );
      if (!items.length) return;
      const first = items[0];
      const last = items[items.length - 1];
      const active = document.activeElement;
      if (event.shiftKey && (active === first || !drawer.contains(active))) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && (active === last || !drawer.contains(active))) {
        event.preventDefault();
        first.focus();
      }
    }
  };

  function close(): void {
    if (closed) return;
    closed = true;
    gitSetup.dispose();
    closeOpenHint();
    document.removeEventListener("keydown", onKey, true);
    scrim.remove();
    // The opener can be gone (hiding its card removes the gear): fall back to the header's Cards
    // button, then the board, so focus never drops to the page body.
    if (opener?.isConnected) opener.focus();
    else {
      const fallback =
        document.querySelector<HTMLElement>(".admin-cards-btn") ??
        document.querySelector<HTMLElement>(".admin-board");
      if (fallback) {
        if (!fallback.matches("button")) fallback.tabIndex = -1;
        fallback.focus({ preventScroll: true });
      }
    }
  }

  document.addEventListener("keydown", onKey, true);
  scrim.addEventListener("click", (event) => {
    if (event.target === scrim) close();
  });
  closeButton.addEventListener("click", close);
  document.body.appendChild(scrim);
  closeButton.focus();

  const settle = (): void => {
    render();
    if (options.focus) {
      const row = list.querySelector<HTMLElement>(`[data-card="${options.focus}"]`);
      row?.scrollIntoView?.({ block: "center" });
    }
  };
  // Always read again on open; the cached rows stay on screen until the answer lands.
  const cached = getCards() !== null;
  if (cached) settle();
  else list.appendChild(el("p", { class: "ae-card-field-status" }, "Reading the cards..."));
  fetchCards(context)
    .then((answer) => {
      if (closed) return;
      setCards(answer);
      settle();
    })
    .catch((error: unknown) => {
      if (closed) return;
      if (cached) {
        say(`Could not refresh the cards: ${describeError(error)}`);
        return;
      }
      list.replaceChildren(
        el(
          "p",
          { class: "ae-cards-note" },
          isAdminOnly(error)
            ? "Only an OmniGent admin can open the card settings."
            : `Could not read the cards: ${describeError(error)}`,
        ),
      );
    });
  return { close, node: scrim };
}
