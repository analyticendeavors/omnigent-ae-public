import type { ExtensionContext } from "@omnigent/extension-sdk";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  COPY_LABEL,
  linkButtons,
  OPEN_LABEL,
  openOnGithubButton,
  urlLinkButton,
} from "../src/openLink";

const LINK = "https://github.com/analyticendeavors/omnigent-ae/pull/58";

function contextWith(openExternal: (url: string) => Promise<void>): ExtensionContext {
  return { navigation: { openExternal } } as unknown as ExtensionContext;
}

function stubClipboard(writeText: (text: string) => Promise<void>): void {
  Object.defineProperty(navigator, "clipboard", { value: { writeText }, configurable: true });
}

const flush = () => new Promise((resolve) => setTimeout(resolve, 0));

function mount(openExternal: (url: string) => Promise<void>) {
  const card = document.createElement("article");
  document.body.append(card);
  const [open, copy] = linkButtons(contextWith(openExternal), LINK, card);
  card.append(open, copy);
  return { card, open, copy };
}

describe("the pull request link buttons", () => {
  beforeEach(() => document.body.replaceChildren());

  afterEach(() => {
    Reflect.deleteProperty(navigator, "clipboard");
    Reflect.deleteProperty(document, "execCommand");
    vi.restoreAllMocks();
  });

  it("is Open on GitHub with a decorative Tabler icon, then a Copy link icon", () => {
    const { open, copy } = mount(vi.fn(async () => undefined));
    expect(open.textContent?.trim()).toBe(OPEN_LABEL);
    expect(open.className).toBe("btn btn-quiet btn-open");
    const icon = open.querySelector("svg.icon-external")!;
    expect(icon.getAttribute("aria-hidden")).toBe("true");
    expect(icon.querySelectorAll("path")).toHaveLength(3);
    // On a phone-narrow card "Open on " hides (style.css), so the button
    // carries its whole label as its name (2026-10-14).
    expect(open.getAttribute("aria-label")).toBe(OPEN_LABEL);
    expect(open.querySelector(".btn-label-lead")?.textContent).toBe("Open on ");
    // Copy is the Tabler copy icon alone, named and titled "Copy link".
    expect(COPY_LABEL).toBe("Copy link");
    expect(copy.textContent).toBe("");
    expect(copy.getAttribute("aria-label")).toBe(COPY_LABEL);
    expect(copy.title).toBe(COPY_LABEL);
    const copyIcon = copy.querySelector("svg.icon-copy")!;
    expect(copyIcon.getAttribute("aria-hidden")).toBe("true");
    expect(copyIcon.querySelectorAll("path")).toHaveLength(2);
    expect(copy.className).toBe("btn btn-quiet btn-copy");
  });

  it("asks the host to open the URL and says nothing when it opens", async () => {
    const openExternal = vi.fn(async () => undefined);
    const writeText = vi.fn(async () => undefined);
    stubClipboard(writeText);
    const { card, open } = mount(openExternal);
    open.click();
    await flush();
    expect(openExternal).toHaveBeenCalledWith(LINK);
    expect(writeText).not.toHaveBeenCalled();
    expect(card.querySelector(".flash")).toBeNull();
  });

  it("shows the URL as selectable text when the browser blocked the tab", async () => {
    const openExternal = vi.fn(async () => {
      throw Object.assign(new Error("The browser blocked the new tab"), { code: "PopupBlocked" });
    });
    const writeText = vi.fn(async () => undefined);
    stubClipboard(writeText);
    const { card, open } = mount(openExternal);
    open.click();
    await flush();
    await flush();
    // No clipboard write after the round trip: iOS refuses it there.
    expect(writeText).not.toHaveBeenCalled();
    const line = card.querySelector<HTMLElement>(".flash")!;
    expect(line.textContent).toBe(
      `Could not open GitHub from here (The browser blocked the new tab). Tap and hold to copy: ${LINK}`,
    );
    expect(line.querySelector("code.link-copy")?.textContent).toBe(LINK);
  });

  it("copies with Copy inside the tap and says so", async () => {
    const writeText = vi.fn(async () => undefined);
    stubClipboard(writeText);
    const { card, copy } = mount(vi.fn(async () => undefined));
    copy.click();
    await flush();
    expect(writeText).toHaveBeenCalledWith(LINK);
    expect(card.querySelector(".flash")?.textContent).toBe("Copied the link.");
  });

  it("shows the URL to copy by hand when Copy cannot reach the clipboard", async () => {
    stubClipboard(async () => {
      throw new Error("denied");
    });
    Object.defineProperty(document, "execCommand", { value: () => false, configurable: true });
    const { card, copy } = mount(vi.fn(async () => undefined));
    copy.click();
    await flush();
    await flush();
    expect(card.querySelector(".flash")?.textContent).toBe(
      `The clipboard is unavailable here. Tap and hold to copy: ${LINK}`,
    );
  });

  it("makes the URL text a button that opens it (2026-09-28)", async () => {
    const openExternal = vi.fn(async () => undefined);
    const card = document.createElement("article");
    const link = urlLinkButton(contextWith(openExternal), LINK, card);
    card.append(link);
    expect(link.tagName).toBe("BUTTON");
    expect(link.type).toBe("button");
    expect(link.getAttribute("aria-label")).toBe(`Open ${LINK} on GitHub`);
    expect(link.className).toBe("linkish url-link");
    expect(link.querySelector("code")?.textContent).toBe(LINK);
    link.click();
    await flush();
    expect(openExternal).toHaveBeenCalledWith(LINK);
    expect(card.querySelector(".flash")).toBeNull();
  });

  it("gives the URL button the same blocked-tab fallback", async () => {
    const openExternal = vi.fn(async () => {
      throw Object.assign(new Error("The browser blocked the new tab"), { code: "PopupBlocked" });
    });
    const writeText = vi.fn(async () => undefined);
    stubClipboard(writeText);
    const card = document.createElement("article");
    const link = urlLinkButton(contextWith(openExternal), LINK, card);
    card.append(link);
    link.click();
    await flush();
    await flush();
    expect(writeText).not.toHaveBeenCalled();
    expect(card.querySelector(".flash")?.textContent).toBe(
      `Could not open GitHub from here (The browser blocked the new tab). Tap and hold to copy: ${LINK}`,
    );
    expect(link.disabled).toBe(false);
  });

  it("clears the last message when a later tap opens", async () => {
    let refuse = true;
    const openExternal = vi.fn(async () => {
      if (refuse) throw new Error("refused");
    });
    const card = document.createElement("article");
    const open = openOnGithubButton(contextWith(openExternal), LINK, card);
    card.append(open);
    open.click();
    await flush();
    await flush();
    expect(card.querySelector(".flash")).not.toBeNull();
    refuse = false;
    open.click();
    await flush();
    expect(card.querySelector(".flash")).toBeNull();
  });
});
