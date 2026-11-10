// The Cards panel's two calls (2026-11-07, card setup), through the same `server.request`
// bridge api.ts uses. api.ts is not edited here (another change is rewriting the board around
// it), so the small `call` helper is repeated for these two routes.

import type { ExtensionContext } from "@omnigent/extension-sdk";
import { AeRequestError } from "./api";
import type { CardsAnswer } from "./cardState";

/**
 * A 403 from the browser-only gate is FastAPI's `{"detail": "..."}`, not the AE
 * `{"error": {...}}` shape AeRequestError reads, so its message would be lost (2026-11-07).
 */
function withMessage(body: unknown): unknown {
  const detail = (body as { detail?: unknown } | null)?.detail;
  return typeof detail === "string" && detail
    ? { error: { code: "refused", message: detail } }
    : body;
}

async function call(
  context: ExtensionContext,
  method: "GET" | "POST",
  path: string,
  body?: unknown,
): Promise<CardsAnswer> {
  const response = await context.server.request({ method, path, body });
  if (!response.ok) throw new AeRequestError(response.status, withMessage(response.body));
  return response.body as CardsAnswer;
}

/** Every card this server shows with its state and setup status. Admins only (403 otherwise). */
export const fetchCards = (context: ExtensionContext): Promise<CardsAnswer> =>
  call(context, "GET", "/v1/ae/cards");

/** Turn a card on or off for everyone; a stale `revision` answers 409 (see conflictAnswer). */
export const setCardState = (
  context: ExtensionContext,
  body: { id: string; on: boolean; revision: number },
): Promise<CardsAnswer> => call(context, "POST", "/v1/ae/cards/state", body);

/**
 * Save or clear settings typed for one card (2026-11-07): `values` maps a variable name to its
 * text, or null to clear the saved value. A 422 names the field; a 409 is a stale revision (see
 * conflictAnswer) or a field the server's environment sets.
 */
export const setCardSettings = (
  context: ExtensionContext,
  body: { id: string; values: Record<string, string | null>; revision: number },
): Promise<CardsAnswer> => call(context, "POST", "/v1/ae/cards/settings", body);

/** The state the server holds now, from a 409 answer; null for any other error. */
export function conflictAnswer(error: unknown): CardsAnswer | null {
  if (!(error instanceof AeRequestError) || error.status !== 409) return null;
  const detail = (error.body as { error?: { current?: unknown } } | null)?.error?.current;
  if (!detail || typeof detail !== "object") return null;
  const current = detail as Partial<CardsAnswer>;
  return Array.isArray(current.cards) && typeof current.revision === "number"
    ? (current as CardsAnswer)
    : null;
}
