// A small popover for the Social view (2026-11-06, phone redesign): the filter menu and the
// editor's More panel. The trigger and the panel share a wrapper that is `position: relative`.
//
// It closes on Escape (focus goes back to the trigger) and on a click or tap outside. While it
// is open the wrapper carries `data-menu-open`, which the dialog reads so the same Escape closes
// the popover first and the dialog on the next press.

export interface Popover {
  open: () => void;
  close: (restoreFocus?: boolean) => void;
  isOpen: () => boolean;
}

export function attachPopover(trigger: HTMLButtonElement, panel: HTMLElement, onOpen?: () => void): Popover {
  const wrap = trigger.parentElement as HTMLElement;
  panel.hidden = true;
  trigger.setAttribute("aria-expanded", "false");
  let isOpen = false;

  const onClick = (event: Event) => {
    if (!wrap.contains(event.target as Node)) close(false);
  };
  const onKey = (event: KeyboardEvent) => {
    if (event.key !== "Escape") return;
    event.preventDefault();
    event.stopPropagation();
    close(true);
  };

  function open(): void {
    if (isOpen) return;
    isOpen = true;
    panel.hidden = false;
    trigger.setAttribute("aria-expanded", "true");
    wrap.dataset.menuOpen = "1";
    document.addEventListener("click", onClick, true);
    document.addEventListener("keydown", onKey, true);
    onOpen?.();
  }

  function close(restoreFocus = false): void {
    if (!isOpen) return;
    isOpen = false;
    panel.hidden = true;
    trigger.setAttribute("aria-expanded", "false");
    delete wrap.dataset.menuOpen;
    document.removeEventListener("click", onClick, true);
    document.removeEventListener("keydown", onKey, true);
    if (restoreFocus) trigger.focus();
  }

  trigger.addEventListener("click", () => (isOpen ? close(true) : open()));
  return { open, close, isOpen: () => isOpen };
}
