// The Git card's saved-token routes (2026-11-08, card setup part 3), through the same
// `server.request` bridge api.ts uses. One interface per route answer, all here, so a rename on
// the server is a one-line change. api.ts is not edited for these (another change is rewriting
// the board around it), so the small `call` helper is repeated, as in cardsApi.ts.
//
// A token typed on the screen travels only in the body of `checkToken`, `saveMine` and
// `saveCompanyToken`. Nothing here stores it, logs it, or puts it in a URL, and an error's text
// has the token cut out of it before it leaves this file (`scrub`).

import type { ExtensionContext } from "@omnigent/extension-sdk";
import { AeRequestError } from "./api";

/** One GitHub account's company token, as `GET /v1/ae/cards` lists it on the Git card. */
export interface CompanyTokenOwner {
  owner: string;
  /** The variable that would carry it, e.g. AE_GIT_TOKEN_EXAMPLE_ORG. */
  env_name: string;
  source: "env" | "saved" | "missing";
  saved_by?: string | null;
  saved_at?: string | null;
  github_login?: string | null;
  expires_at?: string | null;
}

/** One of the caller's saved accounts, from `GET /v1/ae/git/mine`. */
export interface MineAccount {
  account: string;
  github_login: string;
  saved_at: string | null;
  expires_at: string | null;
  unreadable?: boolean;
  /** How many of the caller's repos use this account. */
  repos: number;
}

export interface MineRepo {
  repo: string;
  account: string;
}

/** `GET /v1/ae/git/mine`. */
export interface MineAnswer {
  key_configured: boolean;
  revision: number;
  accounts: MineAccount[];
  repos: MineRepo[];
}

export interface VisibleRepo {
  repo: string;
  private?: boolean;
}

/** `POST /v1/ae/git/mine/check`: saves nothing. */
export interface CheckAnswer {
  github_login: string;
  expires_at: string | null;
  repos: VisibleRepo[];
  /** Repos already on the caller's list. */
  already?: string[];
}

/** `GET /v1/ae/git/users`: counts for other admins' lists, never names, accounts or tokens. */
export interface UsersAnswer {
  users: Array<{ login: string; accounts: number; repos: number }>;
}

type Method = "GET" | "POST";

/** The typed token cut out of any text that leaves this file. */
function scrub(error: unknown, secret: string | null): unknown {
  if (!secret || !(error instanceof AeRequestError)) return error;
  const clean = (text: string) => text.split(secret).join("[token]");
  const safe = new AeRequestError(error.status, {
    error: { code: error.code, message: clean(error.message) },
  });
  // The conflict answer of a stale revision is the one body the callers read back.
  const current = (error.body as { error?: { current?: unknown } } | null)?.error?.current;
  if (current) (safe.body as { error: Record<string, unknown> }).error.current = current;
  return safe;
}

async function call<T>(
  context: ExtensionContext,
  method: Method,
  path: string,
  body?: unknown,
  secret: string | null = null,
): Promise<T> {
  let response;
  try {
    response = await context.server.request({ method, path, body });
  } catch (error) {
    throw scrub(error, secret);
  }
  if (!response.ok) {
    // A browser-only refusal is FastAPI's {"detail": "..."}, not the AE error shape.
    const detail = (response.body as { detail?: unknown } | null)?.detail;
    const body =
      typeof detail === "string" && detail
        ? { error: { code: "refused", message: detail } }
        : response.body;
    throw scrub(new AeRequestError(response.status, body), secret);
  }
  return response.body as T;
}

export const fetchMine = (context: ExtensionContext): Promise<MineAnswer> =>
  call(context, "GET", "/v1/ae/git/mine");

export const checkToken = (context: ExtensionContext, token: string): Promise<CheckAnswer> =>
  call(context, "POST", "/v1/ae/git/mine/check", { token }, token);

export const saveMine = (
  context: ExtensionContext,
  body: { token: string; repos: string[]; revision: number },
): Promise<MineAnswer> => call(context, "POST", "/v1/ae/git/mine/save", body, body.token);

export const clearMine = (context: ExtensionContext, account: string): Promise<MineAnswer> =>
  call(context, "POST", "/v1/ae/git/mine/clear", { account });

export const fetchUsers = (context: ExtensionContext): Promise<UsersAnswer> =>
  call(context, "GET", "/v1/ae/git/users");

export const clearUser = (context: ExtensionContext, login: string): Promise<UsersAnswer> =>
  call(context, "POST", "/v1/ae/git/users/clear", { login });

export const saveCompanyToken = (
  context: ExtensionContext,
  body: { owner: string; token: string },
): Promise<unknown> => call(context, "POST", "/v1/ae/git/company/token", body, body.token);

export const clearCompanyToken = (context: ExtensionContext, owner: string): Promise<unknown> =>
  call(context, "POST", "/v1/ae/git/company/clear", { owner });

/** The 409 `current` state a stale `revision` answers with on the personal routes, or null. */
export function mineConflict(error: unknown): MineAnswer | null {
  if (!(error instanceof AeRequestError) || error.status !== 409) return null;
  const current = (error.body as { error?: { current?: unknown } } | null)?.error?.current;
  const found = current as Partial<MineAnswer> | null | undefined;
  return found && Array.isArray(found.accounts) && typeof found.revision === "number"
    ? (found as MineAnswer)
    : null;
}
