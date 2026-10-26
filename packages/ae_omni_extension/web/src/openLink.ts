// Open on GitHub (2026-09-27): the buttons on every pull request card (Open
// PRs, Your move) that open the pull request in the device's browser. They
// replaced Copy link, because Reid works from an iPhone and a Windows laptop
// and copy, switch app, paste into the address bar was the slowest step of a
// merge done on GitHub.
//
// The page runs in a frame with `sandbox="allow-scripts"`, so it cannot open a
// window itself; `context.navigation.openExternal(url)` asks the host. Until
// core patch P25 the host opened only URLs it had handed out; since P25 it
// also opens a github.com pull request or issue URL
// (upstream/web/src/lib/aeExternalUrls.ts). Where that lands: a browser tab
// opens a new tab of the same browser, the OmniGent desktop app hands the link
// to the OS default browser (upstream's Electron setWindowOpenHandler), and
// the iPhone Home Screen app hands it to Safari.
//
// Review of https://github.com/analyticendeavors/omnigent-ae/pull/58
// (2026-09-27): the host opened with `noopener`, so a tab the browser blocked
// looked opened and no fallback ran. Since core patch P27 the host rejects
// with PopupBlocked. On any rejection the card's status line shows the URL as
// selectable text ("Tap and hold to copy"), without a clipboard write: after
// the host round trip iOS no longer counts the tap, and the write fails. A
// small Copy button beside Open on GitHub stays as the path that always works.
//
// 2026-09-28: Reid asked why the URL printed on a Your move row was not a
// link. The URL text is now a link-styled button (`urlLinkButton`) that takes
// the same path as Open on GitHub, fallback included.
//
// 2026-10-14 (review of the calmer PR cards): on a 375 px phone the three
// buttons cut "Open on GitHub" or wrapped Copy onto a row of its own. On a
// phone-narrow card the label now reads "GitHub" (its name stays "Open on
// GitHub") and Copy is an icon, so Merge, GitHub and Copy share one row.

import type { ExtensionContext } from "@omnigent/extension-sdk";
import { describeError } from "./api";
import { button, el, notice } from "./dom";
import { copyIcon, externalLinkIcon } from "./icons";
import { copyText } from "./lot";

export const OPEN_LABEL = "Open on GitHub";
export const COPY_LABEL = "Copy link";

/** The card's status line with *lead* and the URL as selectable text. */
function showLink(card: HTMLElement, lead: string, url: string): void {
  card.querySelector(".flash")?.remove();
  const line = notice(`${lead} Tap and hold to copy: `, "info");
  line.classList.add("flash");
  line.appendChild(el("code", { class: "link-copy" }, url));
  card.appendChild(line);
}

function say(card: HTMLElement, text: string): void {
  card.querySelector(".flash")?.remove();
  const line = notice(text, "ok");
  line.classList.add("flash");
  card.appendChild(line);
}

/** Ask the host to open *url*; a refusal puts the URL in *card*'s status line. */
export async function openOrShow(context: ExtensionContext, url: string, card: HTMLElement) {
  card.querySelector(".flash")?.remove();
  try {
    await context.navigation.openExternal(url);
  } catch (error) {
    showLink(card, `Could not open GitHub from here (${describeError(error)}).`, url);
  }
}

/** Open on GitHub for *url*; a refusal puts the URL in *card*'s status line. */
export function openOnGithubButton(
  context: ExtensionContext,
  url: string,
  card: HTMLElement,
): HTMLButtonElement {
  const node = button(OPEN_LABEL, {
    title: "Open the pull request on GitHub in a new tab",
    onClick: () => openOrShow(context, url, card),
  });
  node.classList.add("btn-open");
  // "Open on " hides on a phone-narrow card; the name stays the whole label.
  node.setAttribute("aria-label", OPEN_LABEL);
  node.replaceChildren(
    externalLinkIcon(),
    el(
      "span",
      { class: "btn-label" },
      el("span", { class: "btn-label-lead" }, "Open on "),
      "GitHub",
    ),
  );
  return node;
}

/**
 * The URL itself as a link-styled button (monospace, the accent colour,
 * underlined on hover) that opens it like Open on GitHub (2026-09-28).
 */
export function urlLinkButton(
  context: ExtensionContext,
  url: string,
  card: HTMLElement,
): HTMLButtonElement {
  const node = el(
    "button",
    { type: "button", class: "linkish url-link", "aria-label": `Open ${url} on GitHub` },
    el("code", {}, url),
  );
  node.addEventListener("click", () => {
    if (node.disabled) return;
    node.disabled = true;
    void openOrShow(context, url, card).finally(() => {
      node.disabled = false;
    });
  });
  return node;
}

/** The Copy link icon button: copies inside the tap, else shows the URL to copy. */
export function copyLinkButton(url: string, card: HTMLElement): HTMLButtonElement {
  const node = button(COPY_LABEL, {
    title: COPY_LABEL,
    onClick: async () => {
      if (await copyText(url)) say(card, "Copied the link.");
      else showLink(card, "The clipboard is unavailable here.", url);
    },
  });
  node.classList.add("btn-copy");
  node.setAttribute("aria-label", COPY_LABEL);
  node.replaceChildren(copyIcon());
  return node;
}

/** Open on GitHub and Copy, in that order, for a card's action row. */
export function linkButtons(
  context: ExtensionContext,
  url: string,
  card: HTMLElement,
): HTMLButtonElement[] {
  return [openOnGithubButton(context, url, card), copyLinkButton(url, card)];
}
