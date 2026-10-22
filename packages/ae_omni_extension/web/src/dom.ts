// A few element builders. The page is plain DOM (no framework inside the
// sandboxed frame keeps the bundle small); everything user-facing is built
// through these so text is always set as text, never as markup.

import { chevronIcon } from "./icons";
import { isOpen, setOpen } from "./sections";

export type Child = Node | string | null | undefined | false;

export function el<K extends keyof HTMLElementTagNameMap>(
  tag: K,
  attrs: Record<string, string | boolean | undefined> = {},
  ...children: Child[]
): HTMLElementTagNameMap[K] {
  const node = document.createElement(tag);
  for (const [name, value] of Object.entries(attrs)) {
    if (value === undefined || value === false) continue;
    if (value === true) node.setAttribute(name, "");
    else node.setAttribute(name, value);
  }
  append(node, children);
  return node;
}

export function append(parent: Node, children: Child[]): void {
  for (const child of children) {
    if (child === null || child === undefined || child === false) continue;
    parent.appendChild(typeof child === "string" ? document.createTextNode(child) : child);
  }
}

export function replaceChildren(parent: Element, children: Child[]): void {
  parent.replaceChildren();
  append(parent, children);
}

export interface ButtonOptions {
  kind?: "primary" | "quiet" | "danger";
  title?: string;
  disabled?: boolean;
  onClick: (button: HTMLButtonElement) => void | Promise<void>;
}

/**
 * A button (32 px tall with a mouse, 40 px on a touch screen, style.css .btn)
 * that disables itself while its handler runs, so a double
 * tap on the phone never posts twice.
 */
export function button(label: string, options: ButtonOptions): HTMLButtonElement {
  const node = el(
    "button",
    {
      type: "button",
      class: `btn btn-${options.kind ?? "quiet"}`,
      title: options.title,
      disabled: options.disabled,
    },
    label,
  );
  node.addEventListener("click", () => {
    if (node.disabled) return;
    node.disabled = true;
    Promise.resolve(options.onClick(node)).finally(() => {
      node.disabled = false;
    });
  });
  return node;
}

/**
 * A section with a heading, a count pill and a body the caller fills. The
 * heading is a toggle (2026-09-24): open or closed is remembered per section
 * id (sections.ts). A section whose count is zero shows its heading alone,
 * dimmed and not expandable, so an empty day fits on one phone screen.
 * *note* is a muted word after the count ("1 in Your move", 2026-10-14).
 */
export function section(
  id: string,
  heading: string,
  count?: number | string,
  note?: string,
): HTMLElement {
  const isEmpty = count === 0;
  const open = !isEmpty && isOpen(id);
  const node = el("section", {
    class: `section${isEmpty ? " is-empty" : ""}${open ? "" : " is-closed"}`,
    id,
    "aria-labelledby": `${id}-h`,
  });
  const toggle = el(
    "button",
    {
      type: "button",
      class: "section-toggle",
      "aria-expanded": String(open),
      "aria-controls": `${id}-body`,
      disabled: isEmpty,
    },
    chevronIcon(),
    el("span", { class: "section-name" }, heading),
    count === undefined ? null : el("span", { class: "count" }, String(count)),
    note ? el("span", { class: "section-note" }, note) : null,
  );
  toggle.addEventListener("click", () =>
    setSectionOpen(node, node.classList.contains("is-closed")),
  );
  node.appendChild(el("h2", { id: `${id}-h`, class: "section-h" }, toggle));
  node.appendChild(el("div", { class: "section-body", id: `${id}-body`, hidden: !open }));
  return node;
}

/**
 * The heading note of a section that leaves *skipped* of its *total* items to
 * Your move (2026-10-14): "2 in Your move", or "In Your move" when that is
 * all of them (no empty state then); none when it left nothing there.
 */
export function inYourMoveNote(skipped: number, total: number): string | undefined {
  if (skipped === 0) return undefined;
  return skipped === total ? "In Your move" : `${skipped} in Your move`;
}

/**
 * Set or clear the note of a section already drawn: the lot's follows its
 * search and filters, which redraw the rows alone (2026-10-14, review).
 */
export function setSectionNote(node: HTMLElement, note: string | undefined): void {
  const toggle = node.querySelector(":scope > .section-h > .section-toggle");
  if (!toggle) return;
  const span = toggle.querySelector(".section-note");
  if (!note) span?.remove();
  else if (span) span.textContent = note;
  else toggle.appendChild(el("span", { class: "section-note" }, note));
}

/** Open or close a rendered section and remember it. */
export function setSectionOpen(node: HTMLElement, open: boolean): void {
  if (node.classList.contains("is-empty")) return;
  node.classList.toggle("is-closed", !open);
  node.querySelector(".section-toggle")?.setAttribute("aria-expanded", String(open));
  const content = node.querySelector<HTMLElement>(".section-body");
  if (content) content.hidden = !open;
  setOpen(node.id, open);
}

export function body(sectionNode: HTMLElement): HTMLElement {
  return sectionNode.querySelector<HTMLElement>(".section-body")!;
}

export function empty(text: string): HTMLElement {
  return el("p", { class: "empty" }, text);
}

export function notice(text: string, kind: "error" | "info" | "ok" = "info"): HTMLElement {
  return el(
    "p",
    { class: `notice notice-${kind}`, role: kind === "error" ? "alert" : "status" },
    text,
  );
}

let toastTimer: ReturnType<typeof setTimeout> | null = null;

/**
 * A page-level confirmation that survives the re-render an action triggers
 * (a card that just moved sections takes its own flash with it). Lives in
 * the element with id `toast`, which the layout creates once; clears itself
 * after a few seconds.
 */
export function announce(text: string, kind: "error" | "info" | "ok" = "ok"): void {
  const host = document.getElementById("toast");
  if (!host) return;
  replaceChildren(host, [notice(text, kind)]);
  if (toastTimer) clearTimeout(toastTimer);
  toastTimer = setTimeout(() => host.replaceChildren(), 6_000);
}

/** Show a transient message under an action; cleared by the next one. */
export function flash(target: HTMLElement, text: string, kind: "error" | "info" | "ok"): void {
  target.querySelector(".flash")?.remove();
  const node = notice(text, kind);
  node.classList.add("flash");
  target.appendChild(node);
}
