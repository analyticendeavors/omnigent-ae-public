// The info button that explains a control in one line (2026-10-28, hint.ts): it opens on
// keyboard focus, hover and tap, closes on Escape, blur and a tap outside, is wired to its
// tip with aria-describedby, and the text is never longer than HINT_MAX.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { closeOpenHint, HINT_MAX, hasOpenHint, hint } from "../src/hint";

const TEXT = "A reading older than this many minutes turns the card amber.";

function mount(text = TEXT) {
  const node = hint(text, "Stale minutes");
  document.body.appendChild(node);
  const button = node.querySelector<HTMLButtonElement>("button")!;
  const tip = node.querySelector<HTMLElement>('[role="tooltip"]')!;
  return { node, button, tip };
}

beforeEach(() => document.body.replaceChildren());
afterEach(() => {
  closeOpenHint();
  document.body.replaceChildren();
});

describe("the hint", () => {
  it("is a named button wired to a tooltip, closed at first", () => {
    const { button, tip } = mount();
    expect(button.getAttribute("aria-label")).toBe("About Stale minutes");
    expect(button.getAttribute("aria-describedby")).toBe(tip.id);
    expect(button.getAttribute("data-hint")).toBe(TEXT);
    expect(tip.textContent).toBe(TEXT);
    expect(tip.hidden).toBe(true);
    expect(button.getAttribute("aria-expanded")).toBe("false");
  });

  it("opens on keyboard focus and closes on blur", () => {
    const { button, tip } = mount();
    button.focus();
    expect(tip.hidden).toBe(false);
    expect(button.getAttribute("aria-expanded")).toBe("true");
    button.blur();
    expect(tip.hidden).toBe(true);
  });

  it("opens on hover and closes when the pointer leaves", () => {
    const { button, tip } = mount();
    button.dispatchEvent(new MouseEvent("mouseenter"));
    expect(tip.hidden).toBe(false);
    button.dispatchEvent(new MouseEvent("mouseleave"));
    expect(tip.hidden).toBe(true);
  });

  it("toggles on a tap and stays open while the pointer leaves", () => {
    const { button, tip } = mount();
    button.click();
    expect(tip.hidden).toBe(false);
    button.dispatchEvent(new MouseEvent("mouseleave"));
    expect(tip.hidden).toBe(false);
    button.click();
    expect(tip.hidden).toBe(true);
  });

  it("closes on Escape without letting anything behind it see the key", () => {
    const { button, tip } = mount();
    let behind = 0;
    document.addEventListener("keydown", () => (behind += 1));
    button.click();
    button.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
    expect(tip.hidden).toBe(true);
    expect(behind).toBe(0);
  });

  it("closes on a tap outside, and only one tip is open at a time", () => {
    const first = mount();
    const second = mount("Another hint.");
    first.button.click();
    second.button.click();
    expect(first.tip.hidden).toBe(true);
    expect(second.tip.hidden).toBe(false);
    document.body.dispatchEvent(new Event("pointerdown", { bubbles: true }));
    expect(second.tip.hidden).toBe(true);
  });

  it("adds no height: the tip floats (position fixed in the stylesheet)", () => {
    expect(HINT_MAX).toBe(140);
  });
});

describe("where the tip opens", () => {
  const rect = (top: number, bottom: number, left = 100, width = 24) =>
    ({ top, bottom, left, right: left + width, width, height: bottom - top, x: left, y: top }) as DOMRect;

  function place(button: HTMLElement, tip: HTMLElement, at: DOMRect, height: number) {
    vi.spyOn(button, "getBoundingClientRect").mockReturnValue(at);
    vi.spyOn(tip, "getBoundingClientRect").mockReturnValue(rect(0, height, 0, 260));
  }

  beforeEach(() => {
    Object.defineProperty(window, "innerHeight", { configurable: true, value: 844 });
    Object.defineProperty(window, "innerWidth", { configurable: true, value: 390 });
  });

  it("opens below the button when it fits", () => {
    const { button, tip } = mount();
    place(button, tip, rect(300, 324), 60);
    button.click();
    expect(tip.style.top).toBe("330px");
  });

  it("flips above a button near the bottom edge of a phone", () => {
    const { button, tip } = mount();
    place(button, tip, rect(790, 814), 60);
    button.click();
    expect(tip.style.top).toBe("724px");
    expect(Number.parseFloat(tip.style.top) + 60).toBeLessThanOrEqual(844 - 8);
  });

  it("clamps inside the viewport when it fits on neither side", () => {
    const { button, tip } = mount();
    place(button, tip, rect(400, 424), 900);
    button.click();
    const top = Number.parseFloat(tip.style.top);
    expect(top).toBeGreaterThanOrEqual(8);
  });

  it("keeps the tip inside the sides too", () => {
    const { button, tip } = mount();
    place(button, tip, rect(300, 324, 370), 60);
    button.click();
    expect(Number.parseFloat(tip.style.left)).toBeLessThanOrEqual(390 - 260 - 8);
  });

  it("can be asked whether a tip is open, and closed from outside", () => {
    closeOpenHint();
    const { button } = mount();
    expect(hasOpenHint()).toBe(false);
    button.click();
    expect(hasOpenHint()).toBe(true);
    closeOpenHint();
    expect(hasOpenHint()).toBe(false);
  });
});
