// The shared modal dialog (2026-10-28, dialog.ts): the WAI-ARIA modal pattern.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { type DialogOptions, type OpenDialog, openDialog as open } from "../src/dialog";

// A dialog left open keeps its document listener, so each test closes what it opened.
const opened: OpenDialog[] = [];
function openDialog(options: DialogOptions): OpenDialog {
  const dialog = open(options);
  opened.push(dialog);
  return dialog;
}

const dialogCard = () => document.querySelector<HTMLElement>(".ae-dialog-card")!;

function setup() {
  document.body.replaceChildren();
  const opener = document.createElement("button");
  opener.textContent = "Details";
  document.body.appendChild(opener);
  const body = document.createElement("div");
  const first = document.createElement("button");
  first.textContent = "First";
  const last = document.createElement("button");
  last.textContent = "Last";
  body.append(first, last);
  opener.focus();
  return { opener, first, last, body };
}

beforeEach(() => document.body.replaceChildren());
afterEach(() => {
  for (const dialog of opened.splice(0)) dialog.close();
  document.body.replaceChildren();
});

describe("the dialog", () => {
  it("is a labelled modal and focuses Close", () => {
    const { opener, body } = setup();
    const dialog = openDialog({ title: "Box health details", body, opener, id: "t" });
    const card = document.querySelector('[role="dialog"]')!;
    expect(card.getAttribute("aria-modal")).toBe("true");
    const title = document.getElementById(card.getAttribute("aria-labelledby")!)!;
    expect(title.textContent).toBe("Box health details");
    expect(document.activeElement).toBe(dialog.closeButton);
  });

  it("closes on Escape in the capture phase and returns focus to the opener", () => {
    const { opener, body } = setup();
    const onClose = vi.fn();
    openDialog({ title: "T", body, opener, id: "t", onClose });
    let behind = 0;
    document.addEventListener("keydown", () => (behind += 1));
    dialogCard().dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
    expect(document.querySelector('[role="dialog"]')).toBeNull();
    expect(document.activeElement).toBe(opener);
    expect(behind).toBe(0);
    expect(onClose).toHaveBeenCalledOnce();
  });

  it("wraps Tab and Shift+Tab inside the card", () => {
    const { opener, first, last, body } = setup();
    const dialog = openDialog({ title: "T", body, opener, id: "t" });
    last.focus();
    const tab = new KeyboardEvent("keydown", { key: "Tab", bubbles: true, cancelable: true });
    document.dispatchEvent(tab);
    expect(tab.defaultPrevented).toBe(true);
    expect(document.activeElement).toBe(dialog.closeButton);
    const back = new KeyboardEvent("keydown", {
      key: "Tab",
      shiftKey: true,
      bubbles: true,
      cancelable: true,
    });
    document.dispatchEvent(back);
    expect(document.activeElement).toBe(last);
    first.focus();
    expect(document.activeElement).toBe(first);
  });

  it("closes on a backdrop click but not on a click inside the card", () => {
    const { opener, body } = setup();
    openDialog({ title: "T", body, opener, id: "t" });
    document.querySelector<HTMLElement>(".ae-dialog-card")!.click();
    expect(document.querySelector('[role="dialog"]')).not.toBeNull();
    document.querySelector<HTMLElement>(".ae-dialog")!.click();
    expect(document.querySelector('[role="dialog"]')).toBeNull();
    expect(document.activeElement).toBe(opener);
  });

  it("closes with its Close button", () => {
    const { opener, body } = setup();
    const dialog = openDialog({ title: "T", body, opener, id: "t" });
    dialog.closeButton.click();
    expect(document.querySelector('[role="dialog"]')).toBeNull();
  });
});
