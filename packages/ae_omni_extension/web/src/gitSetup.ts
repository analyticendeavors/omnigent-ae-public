// The Git entry of the Cards panel (2026-11-08, card setup part 3): the company tokens per
// GitHub account, the count of other admins' personal accounts, and "My GitHub accounts".
//
// One section object lives as long as the panel: the panel redraws its rows from scratch, and a
// token typed here must survive that, so the panel moves this node into each new Git row instead
// of building it again. The token lives only in a password-type input and in `pending` until the
// save (or the cancel) answers; then both are wiped. It is never put in storage, a URL, a log, a
// toast or an error line (gitSetupApi.ts also cuts it out of any server text).

import type { ExtensionContext } from "@omnigent/extension-sdk";
import { describeError } from "./api";
import type { CardRow } from "./cardState";
import { openDialog } from "./dialog";
import { el } from "./dom";
import {
  type CheckAnswer,
  type CompanyTokenOwner,
  type MineAccount,
  type MineAnswer,
  type UsersAnswer,
  checkToken,
  clearCompanyToken,
  clearMine,
  clearUser,
  fetchMine,
  fetchUsers,
  mineConflict,
  saveCompanyToken,
  saveMine,
} from "./gitSetupApi";

export const KEY_NAME = "AE_CARD_SECRETS_KEY";
export const NO_KEY_TEXT = `Set ${KEY_NAME} in the server's environment to save tokens here.`;
export const TOKEN_HINT =
  "A fine-grained token, read only: Metadata, Contents, Pull requests, Dependabot alerts, Actions.";
export const NEW_TOKEN_URL = "https://github.com/settings/personal-access-tokens/new";
export const MINE_RULE = "Only you see these. Read with your own token; never with the company's.";
export const COMPANY_RULE = "Every admin sees these. Admins edit them.";
/** An expiry this close shows as a warning. */
export const EXPIRY_WARN_DAYS = 14;

const DAY_MS = 86_400_000;

export interface GitSetup {
  node: HTMLElement;
  /** Draw the company rows from the Git card's row in the latest cards answer. */
  update: (row: CardRow, frozen: boolean) => void;
  /** Read the personal accounts and the other admins' counts (once; again after a change). */
  load: () => void;
  /** The panel closed: wipe anything typed. */
  dispose: () => void;
}

export interface GitSetupOptions {
  say: (text: string | null) => void;
  /** Read the cards again after a company token changed (the panel redraws from it). */
  refreshCards: () => Promise<void>;
}

/** The owners the Git card lists, from its token field (`owners`) or the row itself. */
export function companyOwners(row: CardRow): CompanyTokenOwner[] {
  const holder = row as CardRow & { owners?: CompanyTokenOwner[] };
  const field = row.fields.find((item) => item.env === "AE_GIT_TOKEN_") as
    | (CardRow["fields"][number] & { owners?: CompanyTokenOwner[] })
    | undefined;
  const found = field?.owners ?? holder.owners;
  return Array.isArray(found) ? found : [];
}

export function dayOf(iso: string | null | undefined): string | null {
  const when = iso ? new Date(iso) : null;
  return when && !Number.isNaN(when.getTime())
    ? when.toLocaleDateString(undefined, { month: "short", day: "numeric" })
    : null;
}

/** True when the expiry is within the warning window or already past. */
export function expirySoon(iso: string | null | undefined, now = Date.now()): boolean {
  const when = iso ? new Date(iso).getTime() : Number.NaN;
  return !Number.isNaN(when) && when - now <= EXPIRY_WARN_DAYS * DAY_MS;
}

const plural = (n: number, one: string) => `${n} ${one}${n === 1 ? "" : "s"}`;

function savedWords(by: string | null | undefined, at: string | null | undefined): string {
  const day = dayOf(at);
  return `Saved${by ? ` by ${by}` : ""}${day ? ` on ${day}` : ""}`;
}

function expiryNode(iso: string | null | undefined): HTMLElement | null {
  const day = dayOf(iso);
  if (!day) return null;
  const soon = expirySoon(iso);
  return el(
    "span",
    { class: "ae-git-expiry", "data-tone": soon ? "warn" : "ok" },
    `${soon && new Date(iso as string).getTime() < Date.now() ? "expired" : "expires"} ${day}`,
  );
}

function button(label: string, title: string, onClick: () => void, quiet = true): HTMLButtonElement {
  const node = el(
    "button",
    { type: "button", class: quiet ? "btn btn-quiet ae-git-btn" : "btn ae-git-btn", title },
    label,
  );
  node.addEventListener("click", onClick);
  return node;
}

/** A password-type input: no autofill, no spellcheck, never named, so nothing remembers it. */
function tokenInput(label: string): HTMLInputElement {
  return el("input", {
    type: "password",
    class: "ae-card-input ae-git-token",
    autocomplete: "off",
    spellcheck: "false",
    autocapitalize: "off",
    "aria-label": label,
    placeholder: "github_pat_...",
  });
}

function hintLine(context: ExtensionContext, text = TOKEN_HINT): HTMLElement {
  const link = el(
    "a",
    { href: NEW_TOKEN_URL, target: "_blank", rel: "noopener noreferrer", class: "ae-git-link" },
    "Make one on GitHub",
  );
  link.addEventListener("click", (event) => {
    const open = context.navigation?.openExternal;
    if (!open) return;
    event.preventDefault();
    Promise.resolve(open(NEW_TOKEN_URL)).catch(() => undefined);
  });
  return el("p", { class: "ae-card-field-status ae-git-hint" }, text, " ", link);
}

function confirmDialog(
  opener: HTMLElement,
  title: string,
  words: string,
  action: string,
  onConfirm: () => void,
): void {
  const cancel = button("Cancel", "Close without changing anything", () => dialog.close());
  const ok = button(action, words, () => {
    dialog.close();
    onConfirm();
  }, false);
  const dialog = openDialog({
    title,
    id: "ae-git-confirm",
    opener,
    body: el("div", {}, el("p", {}, words), el("div", { class: "ae-card-save-row" }, ok, cancel)),
  });
}

export function createGitSetup(context: ExtensionContext, options: GitSetupOptions): GitSetup {
  const { say } = options;
  let row: CardRow | null = null;
  let frozen = false;
  let mine: MineAnswer | null = null;
  let users: UsersAnswer | null = null;
  let mineError: string | null = null;
  let usersError: string | null = null;
  let loading = false;
  let loaded = false;
  let busy = false;
  let disposed = false;
  /** The company owner whose token form is open. */
  let companyOpen: string | null = null;
  /** The connect form: closed, token typed (checking), or checked with repos to pick. */
  let connect: { open: boolean; checked: CheckAnswer | null; picks: Set<string>; problem: string | null } = {
    open: false,
    checked: null,
    picks: new Set(),
    problem: null,
  };
  /** The token between Check and Save. Wiped with the input. */
  let pending: string | null = null;
  let companyProblem: string | null = null;

  const node = el("div", { class: "ae-git-setup", "data-git-setup": "" });
  // One input per form, kept across redraws so a typed token is not lost to a refresh; the
  // forms are rebuilt around them.
  const connectInput = tokenInput("GitHub token for a new account");
  const companyInput = tokenInput("GitHub token for this account");

  function wipeConnect(): void {
    connectInput.value = "";
    pending = null;
    connect = { open: false, checked: null, picks: new Set(), problem: null };
  }
  function wipeCompany(): void {
    companyInput.value = "";
    companyOpen = null;
  }

  const keyKnown = (): boolean => mine !== null;
  const keyOk = (): boolean => mine?.key_configured === true;

  async function loadAll(): Promise<void> {
    if (loading || disposed) return;
    loading = true;
    const [m, u] = await Promise.allSettled([fetchMine(context), fetchUsers(context)]);
    loading = false;
    if (disposed) return;
    loaded = true;
    mine = m.status === "fulfilled" ? m.value : mine;
    mineError = m.status === "rejected" ? describeError(m.reason) : null;
    users = u.status === "fulfilled" ? u.value : users;
    usersError = u.status === "rejected" ? describeError(u.reason) : null;
    draw();
  }

  // ── company tokens ─────────────────────────────────────────────────────────

  async function submitCompany(owner: string): Promise<void> {
    const token = companyInput.value;
    if (!token.trim() || busy) return;
    busy = true;
    companyProblem = null;
    draw();
    try {
      await saveCompanyToken(context, { owner, token });
      say(`Saved the token for ${owner}.`);
    } catch (error) {
      companyProblem = describeError(error);
    } finally {
      // Wiped on any answer.
      wipeCompany();
      busy = false;
    }
    await options.refreshCards().catch(() => undefined);
    draw();
  }

  async function removeCompany(owner: string): Promise<void> {
    if (busy) return;
    busy = true;
    companyProblem = null;
    draw();
    try {
      await clearCompanyToken(context, owner);
      say(`Removed the saved token for ${owner}.`);
    } catch (error) {
      companyProblem = describeError(error);
    } finally {
      busy = false;
    }
    await options.refreshCards().catch(() => undefined);
    draw();
  }

  function tokenForm(
    input: HTMLInputElement,
    save: { label: string; title: string; run: () => void },
    cancel: () => void,
    disabled: boolean,
  ): HTMLElement {
    input.disabled = busy || disabled;
    const go = button(save.label, save.title, save.run, false);
    go.disabled = busy || disabled;
    const stop = button("Cancel", "Close this without saving", cancel);
    stop.disabled = busy;
    input.onkeydown = (event) => {
      if (event.key === "Enter") {
        event.preventDefault();
        save.run();
      }
    };
    return el("div", { class: "ae-git-form" }, input, el("div", { class: "ae-card-save-row" }, go, stop), hintLine(context));
  }

  function companyRow(owner: CompanyTokenOwner): HTMLElement {
    const editing = companyOpen === owner.owner;
    const lead = el("strong", {}, owner.owner);
    let status: Array<Node | string>;
    const actions: HTMLElement[] = [];
    const canType = keyOk() && !frozen;
    if (owner.source === "env") {
      status = [`Token from the server's environment, `, el("code", {}, owner.env_name), "."];
    } else if (owner.source === "saved") {
      status = [savedWords(owner.saved_by, owner.saved_at), owner.github_login ? ` (${owner.github_login})` : "", ", ", expiryNode(owner.expires_at) ?? "no expiry known", "."];
      const replace = button("Replace", `Replace the saved token for ${owner.owner}`, () => {
        companyOpen = owner.owner;
        companyProblem = null;
        draw();
        companyInput.focus();
      });
      replace.disabled = busy || !canType;
      const clear = button("Clear", `Remove the saved token for ${owner.owner}`, () =>
        confirmDialog(
          clear,
          `Clear the token for ${owner.owner}?`,
          `The saved token for ${owner.owner} is deleted. Company reads for it stop until a token is set again.`,
          "Clear token",
          () => void removeCompany(owner.owner),
        ),
      );
      clear.disabled = busy || frozen;
      actions.push(replace, clear);
    } else {
      status = [`Not set. Set `, el("code", {}, owner.env_name), " in the server's environment, or add a token here."];
      const add = button("Add", `Add a token for ${owner.owner}`, () => {
        companyOpen = owner.owner;
        companyProblem = null;
        draw();
        companyInput.focus();
      });
      add.disabled = busy || !canType;
      actions.push(add);
    }
    return el(
      "li",
      { class: "ae-git-item", "data-owner": owner.owner, "data-source": owner.source },
      el("div", { class: "ae-git-line" }, lead, ". ", ...status, " ", ...actions),
      editing && canType
        ? tokenForm(
            companyInput,
            {
              label: "Save token",
              title: `Check the token with GitHub and save it for ${owner.owner}`,
              run: () => void submitCompany(owner.owner),
            },
            () => {
              wipeCompany();
              draw();
            },
            false,
          )
        : null,
    );
  }

  function companyBlock(): HTMLElement {
    const owners = row ? companyOwners(row) : [];
    return el(
      "section",
      { class: "ae-git-block", "data-part": "company" },
      el("h5", {}, "Company repos"),
      el("p", { class: "ae-card-field-status" }, COMPANY_RULE),
      mine && !mine.key_configured ? el("p", { class: "ae-card-field-status ae-git-nokey", role: "status" }, NO_KEY_TEXT) : null,
      owners.length
        ? el("ul", { class: "ae-git-list" }, ...owners.map(companyRow))
        : el("p", { class: "ae-card-field-status" }, "No GitHub account is listed for the company repos yet."),
      companyProblem ? el("p", { class: "ae-card-error", role: "alert" }, companyProblem) : null,
    );
  }

  // ── other admins ───────────────────────────────────────────────────────────

  async function removeUser(login: string): Promise<void> {
    if (busy) return;
    busy = true;
    draw();
    try {
      users = await clearUser(context, login);
      say(`Cleared the saved tokens of ${login}.`);
    } catch (error) {
      usersError = describeError(error);
    } finally {
      busy = false;
    }
    draw();
  }

  function othersBlock(): HTMLElement {
    const list = users?.users ?? [];
    const accounts = list.reduce((sum, user) => sum + user.accounts, 0);
    const summary = users
      ? list.length
        ? `Other users: ${list.length === 1 ? "1 has" : `${list.length} have`} ${plural(accounts, "personal account")} connected.`
        : "Other users: nobody else has a personal account connected."
      : "Other users: reading...";
    return el(
      "section",
      { class: "ae-git-block", "data-part": "others" },
      el("p", { class: "ae-git-line" }, summary),
      el("p", { class: "ae-card-field-status" }, "You see counts only, never another admin's repos, accounts or tokens. Clearing is for when someone leaves."),
      list.length
        ? el(
            "ul",
            { class: "ae-git-list" },
            ...list.map((user) => {
              const clear = button("Clear their tokens", `Delete every token ${user.login} saved here`, () =>
                confirmDialog(
                  clear,
                  `Clear the tokens of ${user.login}?`,
                  `Every GitHub token ${user.login} saved here, and their repo list, is deleted. They can connect again.`,
                  "Clear their tokens",
                  () => void removeUser(user.login),
                ),
              );
              clear.disabled = busy || frozen;
              return el(
                "li",
                { class: "ae-git-item", "data-user": user.login },
                el("div", { class: "ae-git-line" }, el("strong", {}, user.login), `. ${plural(user.accounts, "account")}, ${plural(user.repos, "repo")}. `, clear),
              );
            }),
          )
        : null,
      usersError ? el("p", { class: "ae-card-error", role: "alert" }, usersError) : null,
    );
  }

  // ── my accounts ────────────────────────────────────────────────────────────

  async function removeMine(account: string): Promise<void> {
    if (busy) return;
    busy = true;
    mineError = null;
    draw();
    try {
      mine = await clearMine(context, account);
      say(`Removed ${account}.`);
    } catch (error) {
      mineError = describeError(error);
    } finally {
      busy = false;
    }
    draw();
  }

  async function runCheck(): Promise<void> {
    const token = connectInput.value;
    if (!token.trim() || busy) return;
    busy = true;
    connect.problem = null;
    draw();
    try {
      const found = await checkToken(context, token);
      pending = token;
      connect.checked = found;
      // Repos already on the list start ticked; a new account starts with none.
      connect.picks = new Set(found.already ?? []);
    } catch (error) {
      connect.problem = describeError(error);
    } finally {
      busy = false;
    }
    draw();
  }

  async function runSave(): Promise<void> {
    const token = pending;
    if (!token || busy || !connect.checked || !mine) return;
    busy = true;
    connect.problem = null;
    draw();
    let problem: string | null = null;
    try {
      const answer = await saveMine(context, {
        token,
        repos: [...connect.picks],
        revision: mine.revision,
      });
      mine = answer;
      say(`Saved ${connect.checked.github_login}.`);
    } catch (error) {
      const current = mineConflict(error);
      if (current) {
        mine = current;
        problem = "Your list changed elsewhere. It is up to date now; paste the token again to save.";
      } else {
        problem = describeError(error);
      }
    } finally {
      // The save answered: the token is gone from the input and from memory.
      wipeConnect();
      busy = false;
    }
    mineError = problem;
    draw();
  }

  function accountRow(account: MineAccount): HTMLElement {
    const replace = button("Replace", `Paste a new token for ${account.account}`, () => {
      connect = { open: true, checked: null, picks: new Set(), problem: null };
      draw();
      connectInput.focus();
    });
    replace.disabled = busy || !keyOk();
    const remove = button("Remove", `Delete the saved token for ${account.account} and its repos`, () =>
      confirmDialog(
        remove,
        `Remove ${account.account}?`,
        `Your saved token for ${account.account} and its ${plural(account.repos, "repo")} are deleted from your list.`,
        "Remove",
        () => void removeMine(account.account),
      ),
    );
    remove.disabled = busy;
    const day = dayOf(account.saved_at);
    return el(
      "li",
      { class: "ae-git-item", "data-account": account.account },
      el(
        "div",
        { class: "ae-git-line" },
        el("strong", {}, account.github_login || account.account),
        ". ",
        account.unreadable
          ? "The server cannot read this token with its current key. Replace it. "
          : `${day ? `Saved ${day}` : "Saved"}, `,
        account.unreadable ? null : (expiryNode(account.expires_at) ?? "no expiry known"),
        account.unreadable ? null : ". ",
        `${plural(account.repos, "repo")}. `,
        replace,
        " ",
        remove,
      ),
    );
  }

  function pickList(checked: CheckAnswer): HTMLElement {
    const picks = connect.picks;
    const save = button(
      `Save ${plural(picks.size, "repo")}`,
      "Save this token and these repos to your own list",
      () => void runSave(),
      false,
    );
    save.disabled = busy || !keyOk();
    const cancel = button("Cancel", "Throw the token away and change nothing", () => {
      wipeConnect();
      draw();
    });
    cancel.disabled = busy;
    return el(
      "div",
      { class: "ae-git-picks" },
      el(
        "p",
        { class: "ae-git-line", role: "status" },
        `Signed in as ${checked.github_login}. ${plural(checked.repos.length, "repo")} visible.`,
        " ",
        expiryNode(checked.expires_at),
      ),
      el(
        "ul",
        { class: "ae-git-pick-list" },
        ...checked.repos.map((repo) => {
          const box = el("input", { type: "checkbox", class: "ae-card-check ae-git-pick", "data-repo": repo.repo });
          box.checked = picks.has(repo.repo);
          box.disabled = busy;
          box.addEventListener("change", () => {
            if (box.checked) picks.add(repo.repo);
            else picks.delete(repo.repo);
            save.textContent = `Save ${plural(picks.size, "repo")}`;
          });
          return el("li", {}, el("label", { class: "ae-git-pick-label" }, box, el("span", {}, repo.repo, repo.private ? " (private)" : "")));
        }),
      ),
      el("div", { class: "ae-card-save-row" }, save, cancel),
    );
  }

  function connectBlock(): HTMLElement {
    if (!keyKnown()) return el("p", { class: "ae-card-field-status" }, "Reading...");
    if (!keyOk()) return el("p", { class: "ae-card-field-status ae-git-nokey", role: "status" }, NO_KEY_TEXT);
    if (!connect.open) {
      return button("Connect another account", "Paste a GitHub token to read your own repos", () => {
        connect.open = true;
        draw();
        connectInput.focus();
      });
    }
    if (connect.checked) {
      // The token stays in the input, read only, until the save or the cancel answers.
      connectInput.disabled = true;
      return el("div", { class: "ae-git-form" }, connectInput, pickList(connect.checked), connect.problem ? el("p", { class: "ae-card-error", role: "alert" }, connect.problem) : null);
    }
    connectInput.disabled = busy || frozen;
    const check = button("Check", "Ask GitHub who this token belongs to and which repos it can read", () => void runCheck(), false);
    check.disabled = busy || frozen;
    const cancel = button("Cancel", "Close this without saving", () => {
      wipeConnect();
      draw();
    });
    cancel.disabled = busy;
    connectInput.onkeydown = (event) => {
      if (event.key === "Enter") {
        event.preventDefault();
        void runCheck();
      }
    };
    return el(
      "div",
      { class: "ae-git-form" },
      connectInput,
      el("div", { class: "ae-card-save-row" }, check, cancel),
      hintLine(context),
      connect.problem ? el("p", { class: "ae-card-error", role: "alert" }, connect.problem) : null,
    );
  }

  function mineBlock(): HTMLElement {
    const accounts = mine?.accounts ?? [];
    return el(
      "section",
      { class: "ae-git-block", "data-part": "mine" },
      el("h5", {}, "My GitHub accounts"),
      el("p", { class: "ae-card-field-status" }, MINE_RULE),
      accounts.length ? el("ul", { class: "ae-git-list" }, ...accounts.map(accountRow)) : null,
      mineError ? el("p", { class: "ae-card-error", role: "alert" }, mineError) : null,
      connectBlock(),
    );
  }

  function draw(): void {
    if (disposed) return;
    // Keep the token inputs alive across a redraw: detach, rebuild around them, reattach.
    node.replaceChildren(companyBlock(), othersBlock(), mineBlock());
  }

  draw();

  return {
    node,
    update(next, isFrozen) {
      row = next;
      frozen = isFrozen;
      draw();
    },
    load() {
      if (!loaded && !loading) void loadAll();
    },
    dispose() {
      disposed = true;
      wipeConnect();
      wipeCompany();
    },
  };
}
