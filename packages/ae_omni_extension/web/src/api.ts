// The page's only bridge to data: the host RPC patch P2 adds. `server.request`
// reaches the AE layer's `/v1/ae/*` routes and the two session writes the
// allowlist permits (an `ae.*` labels PATCH, a user message); sessions come
// from `sessions.listAll()` with their `ae.*` labels. task-store stays the
// system of record behind `/v1/ae/lot`; this page is a view over it.

import type { ExtensionContext, ExtensionSessionSummary } from "@omnigent/extension-sdk";
import type { LimitWindow } from "./limits";

export type Session = ExtensionSessionSummary;

/** The `ae.status` values, in the order the page lists them (labels.py STATUS_VALUES). */
export type AeStatus = "working" | "blocked" | "review" | "parked" | "reference";
export const AE_STATUS_VALUES: readonly AeStatus[] = [
  "working",
  "blocked",
  "review",
  "parked",
  "reference",
];
// Fallback only: the page counts against `limit` from GET /v1/ae/capacity, the
// server's AE_SLOT_LIMIT (the one setting since 2026-09-24).
export const SLOT_LIMIT = 3;

export type Quadrant = "hotfix" | "feature" | "admin";
export type Slot = "never opened" | "freed" | "swapped-back";
export type GateRoute = "A" | "B" | "C" | "close";

/** One row of `GET /v1/ae/capacity` (ae_omni_policies.sessions.SessionSummary). */
export interface CapacitySession {
  id: string;
  title: string;
  status: AeStatus | null;
  live_status: string | null;
  blocked: boolean;
  started: number;
  elapsed_s: number;
  elapsed: string;
  next: string | null;
  gate: string | null;
  fork: string | null;
  overflow: boolean;
  task: string | null;
}

export interface Capacity {
  limit: number;
  slots_used: number;
  working: CapacitySession[];
  blocked: CapacitySession[];
  review: CapacitySession[];
  parked: CapacitySession[];
  reference: CapacitySession[];
  unlabelled_running: CapacitySession[];
  /** Today in the lot's timezone (Phase 5); the lot's date filters use it. */
  today?: string;
}

/** One task as `GET /v1/ae/lot` presents it (the worker's `list_parked` fields). */
export interface LotTask {
  id: string;
  title: string | null;
  quadrant: string | null;
  estimate: string | null;
  status: string | null;
  tags: string[] | null;
  created_at: string | null;
  resurfaces: string | null;
  next: string | null;
  /** `owner/name` from the note's REPO: line or a GitHub link (2026-09-25). */
  repo?: string | null;
  links: string[];
  url: string;
}

/** One row of `GET /v1/ae/lot/tags`: the app's colour name and Tabler icon name. */
export interface LotTag {
  name: string;
  tasks: number | null;
  color: string | null;
  icon: string | null;
}

/** `GET /v1/ae/lot/tags?icons=true`: the rows plus each named icon's path data. */
export interface LotTags {
  count: number;
  tags: LotTag[];
  icons?: Record<string, string[]>;
  icons_note?: string;
}

export interface Lot {
  list: { id: string; name: string } | null;
  status?: string;
  count: number;
  tasks: LotTask[];
  note?: string;
}

/** `POST /v1/ae/lot/park`: the worker's `park_fork` field names, verbatim. */
export interface ParkInput {
  thread: string;
  state: string;
  next: string;
  slot: Slot;
  quadrant: Quadrant;
  est: string;
  from: string;
  summary?: string;
  repo?: string;
  tags?: string[];
  due_date?: string;
  session_id?: string;
}

export interface ParkOutcome {
  outcome: "filed";
  task_id: string;
  title: string | null;
  url: string;
  warnings?: string[];
  session?: { id: string; labels: Record<string, string> };
}

export interface ResumeResult {
  task_id: string;
  title: string | null;
  block: string;
  fenced: string;
}

/** `POST /v1/ae/lot/{id}/session`: the session Resume started, the block already posted. */
export interface ResumeSessionResult extends ResumeResult {
  session_id: string;
  host: { id: string; name: string | null };
  agent: { id: string; name: string | null };
  workspace: string;
}

/**
 * What a failed Resume still carries in its error body: the block for a hand
 * paste and, when the session was created but the block did not post, its id.
 */
export interface ResumeFallback {
  message: string;
  block: string;
  fenced: string;
  sessionId: string | null;
}

export interface GateReplyResult {
  posted: boolean;
  route: GateRoute;
  text: string;
}

/** A non-2xx answer from the server, with upstream's `{"error": {"code", "message"}}` read out. */
export class AeRequestError extends Error {
  readonly status: number;
  readonly code: string;
  readonly body: unknown;

  constructor(status: number, body: unknown) {
    const error = errorOf(body);
    super(error?.message ?? `Request failed (${status})`);
    this.name = "AeRequestError";
    this.status = status;
    this.code = error?.code ?? "http_error";
    this.body = body;
  }
}

function errorOf(body: unknown): { code: string; message: string } | null {
  if (!body || typeof body !== "object") return null;
  const error = (body as { error?: unknown }).error;
  if (!error || typeof error !== "object") return null;
  const { code, message } = error as { code?: unknown; message?: unknown };
  return {
    code: typeof code === "string" ? code : "error",
    message: typeof message === "string" ? message : "Request failed",
  };
}

async function call<T>(
  context: ExtensionContext,
  method: "GET" | "POST" | "PATCH",
  path: string,
  body?: unknown,
): Promise<T> {
  const response = await context.server.request({ method, path, body });
  if (!response.ok) throw new AeRequestError(response.status, response.body);
  return response.body as T;
}

export const fetchCapacity = (context: ExtensionContext) =>
  call<Capacity>(context, "GET", "/v1/ae/capacity");

// 100 is the route's cap: search and filters run over the whole lot (2026-09-25).
export const fetchLot = (context: ExtensionContext) =>
  call<Lot>(context, "GET", "/v1/ae/lot?status=open&limit=100");

export const fetchLotTags = (context: ExtensionContext) =>
  call<LotTags>(context, "GET", "/v1/ae/lot/tags?icons=true");

export const park = (context: ExtensionContext, input: ParkInput) =>
  call<ParkOutcome>(context, "POST", "/v1/ae/lot/park", input);

export const resumeSession = (context: ExtensionContext, taskId: string) =>
  call<ResumeSessionResult>(context, "POST", `/v1/ae/lot/${encodeURIComponent(taskId)}/session`);

/** The block and message out of a failed `resumeSession`, or null when the body has none. */
export function resumeFallbackOf(error: unknown): ResumeFallback | null {
  if (!(error instanceof AeRequestError)) return null;
  const detail = (error.body as { error?: Record<string, unknown> } | null)?.error;
  if (!detail || typeof detail.block !== "string" || typeof detail.fenced !== "string") {
    return null;
  }
  return {
    message: error.message,
    block: detail.block,
    fenced: detail.fenced,
    sessionId: typeof detail.session_id === "string" ? detail.session_id : null,
  };
}

export const replyGate = (context: ExtensionContext, sessionId: string, route: GateRoute) =>
  call<GateReplyResult>(context, "POST", `/v1/ae/gate/${encodeURIComponent(sessionId)}/reply`, {
    route,
  });

/** Write `ae.status` through the labels PATCH; the host refreshes the sidebar afterwards. */
export const setStatus = (context: ExtensionContext, sessionId: string, status: AeStatus) =>
  call<unknown>(context, "PATCH", `/v1/sessions/${encodeURIComponent(sessionId)}`, {
    labels: { "ae.status": status },
  });

/** Every top-level, non-archived session with its `ae.*` labels. */
export const fetchSessions = (context: ExtensionContext) =>
  context.sessions.listAll({ pageLimit: 100 });

// ── labels, read the way labels.py reads them ────────────────────────────

/** One `ae.*` label, or null when missing, empty or blank. */
export function label(session: Pick<Session, "labels">, key: string): string | null {
  const value = session.labels[key];
  if (typeof value !== "string") return null;
  const trimmed = value.trim();
  return trimmed === "" ? null : trimmed;
}

export function statusOf(session: Pick<Session, "labels">): AeStatus | null {
  const value = label(session, "ae.status");
  return value !== null && (AE_STATUS_VALUES as readonly string[]).includes(value)
    ? (value as AeStatus)
    : null;
}

export function countOf(session: Pick<Session, "labels">, key: string): number {
  const value = label(session, key);
  if (value === null || !/^\d+$/.test(value)) return 0;
  return Number.parseInt(value, 10);
}

export function titleOf(session: Pick<Session, "id" | "title">): string {
  return session.title?.trim() || session.id.slice(0, 8);
}

/** Epoch seconds the slot was claimed (`ae.started`), else the row's creation time. */
export function startedAt(session: Pick<Session, "labels" | "createdAt">): number {
  const raw = label(session, "ae.started");
  if (raw !== null && /^\d+$/.test(raw)) return Number.parseInt(raw, 10);
  return Math.floor(session.createdAt);
}

/** `2h 10m` style, the same steps as ae_omni_policies.sessions.format_elapsed. */
export function formatElapsed(seconds: number): string {
  const total = Math.max(0, Math.floor(seconds));
  const days = Math.floor(total / 86_400);
  const hours = Math.floor((total % 86_400) / 3_600);
  const minutes = Math.floor((total % 3_600) / 60);
  if (days) return `${days}d ${hours}h`;
  if (hours) return `${hours}h ${minutes}m`;
  if (minutes) return `${minutes}m`;
  return "under a minute";
}

/**
 * The store derives its Eisenhower quadrant from the axes the note set
 * (ae_omni_policies.notes.QUADRANT_AXES, task-store's derive_quadrant): q1 is
 * hotfix (urgent and important), q2 admin (urgent only), q3 feature
 * (important only). q4 never comes from a note; it reads as itself.
 */
export function quadrantName(stored: string | null): Quadrant | null {
  switch (stored) {
    case "hotfix":
    case "q1":
      return "hotfix";
    case "feature":
    case "q3":
      return "feature";
    case "admin":
    case "q2":
      return "admin";
    default:
      return null;
  }
}

/** The store's two estimate buckets (notes.py estimate_for), in words. */
export function estimateText(stored: string | null): string | null {
  if (stored === "15") return "about 15 minutes";
  if (stored === "60plus") return "an hour or more";
  return stored;
}

export function describeError(error: unknown): string {
  if (error instanceof AeRequestError) return error.message;
  if (error instanceof Error) return error.message;
  return "Request failed";
}

// ── Your move (2026-09-24): `GET /v1/ae/your-move` ───────────────────────

/** A source that failed carries its own error; the others still render. */
export interface SourceError {
  ok: false;
  error: { code: string; message: string };
}

export type WaitKind = "gate" | "awaiting" | "blocked" | "review";

/** A session that waits on Reid (ae_omni_policies.your_move.waiting_sessions). */
export interface WaitingSession {
  id: string;
  title: string;
  kind: WaitKind;
  kinds: WaitKind[];
  status: AeStatus | null;
  fork: string | null;
  next: string | null;
  task: string | null;
  pending: number;
  updated_at: number;
  since: string;
}

export interface DueTask extends LotTask {
  overdue: boolean;
}

export interface StewardBrief {
  session_id: string;
  title: string;
  created_at: number;
  live_status: string | null;
  message_at: number | null;
  text: string | null;
  truncated: boolean;
}

export type CiState = "passing" | "failing" | "pending" | "none" | "unknown";

export interface WaitingPull {
  repo: string;
  number: number;
  title: string;
  url: string;
  author: string | null;
  draft: boolean;
  updated_at: string | null;
  why: ("author" | "review_requested")[];
  ci: CiState;
}

export interface YourMove {
  now: string;
  today: string;
  timezone: string;
  total: number;
  cached: boolean;
  sessions: { ok: true; items: WaitingSession[]; counts: Record<WaitKind, number> } | SourceError;
  lot: { ok: true; today: string; items: DueTask[]; note?: string | null } | SourceError;
  steward: { ok: true; agent: string; brief: StewardBrief | null; note?: string } | SourceError;
  pulls:
    | {
        ok: true;
        configured: boolean;
        login: string;
        repos: string[];
        items: WaitingPull[];
        errors: { repo: string; message: string }[];
        note?: string;
        /** Why the section is off: no token, or a token but no AE_GITHUB_REPOS (2026-10-22). */
        missing?: "token" | "repos";
      }
    | SourceError;
}

export const fetchYourMove = (context: ExtensionContext, fresh = false) =>
  call<YourMove>(context, "GET", `/v1/ae/your-move${fresh ? "?fresh=true" : ""}`);

// ── PR cards (2026-09-24): `/v1/ae/prs` ───────────────────────────────────

export type PrCiState = "passing" | "failing" | "pending" | "none" | "unknown";
export type PrMergeableState =
  | "clean"
  | "unstable"
  | "conflicted"
  | "blocked"
  | "behind"
  | "draft"
  | "checking"
  | "merged"
  | "closed";
export type PrReviewState =
  | "approved"
  | "changes_requested"
  | "review_required"
  | "none"
  | "unknown";
export type MergeMethod = "squash" | "merge";

/** A session that tracks the pull request (upstream's per-session PR registry). */
export interface PrSessionRef {
  id: string;
  title: string;
  status: AeStatus | null;
}

/** One card, as ae_omni_policies.prs.read_card builds it; the gate is decided server-side. */
export interface PrCard {
  repo: string;
  number: number;
  url: string;
  title: string;
  state: "open" | "merged" | "closed";
  draft: boolean;
  author: string | null;
  head_ref: string | null;
  base_ref: string | null;
  head_sha: string | null;
  updated_at: string | null;
  ci: { state: PrCiState; passing: number; failing: number; pending: number; total: number };
  mergeable: { state: PrMergeableState; detail: string };
  review: {
    state: PrReviewState;
    approvals: number;
    changes_requested: number;
    requested: string[];
  };
  merge: {
    methods: MergeMethod[];
    default_method: MergeMethod | null;
    /** The method the confirm step preselects and tags, and why (2026-09-28).
     * Optional: a device's saved snapshot from before carries neither. */
    recommended_method?: MergeMethod | null;
    recommended_reason?: string | null;
    allowed: boolean;
    reason: string | null;
  };
  sessions?: PrSessionRef[];
}

export interface OpenPrs {
  configured: boolean;
  note?: string;
  items: PrCard[];
  errors: { session_id?: string; url?: string; message: string }[];
  sessions_scanned?: number;
}

export interface MergeResult {
  merged: true;
  repo: string;
  number: number;
  method: MergeMethod;
  sha: string | null;
  message: string;
  session: { id: string; status: AeStatus | null; offer_review: boolean } | null;
}

export const fetchPrs = (context: ExtensionContext, fresh = false) =>
  call<OpenPrs>(context, "GET", `/v1/ae/prs${fresh ? "?fresh=true" : ""}`);

/**
 * One pull request's card, tracked by a session or not (Your move's merge,
 * 2026-09-24). Not `fresh`: the server's minute-long card cache applies.
 */
export const fetchPrCard = (context: ExtensionContext, pull: Pick<PrCard, "repo" | "number">) =>
  call<PrCard>(context, "GET", `/v1/ae/prs/${pull.repo}/${pull.number}`);

/** Merge pinned to the head the card showed; a 409 carries the fresh card in `error.card`. */
export const mergePr = (
  context: ExtensionContext,
  card: Pick<PrCard, "repo" | "number" | "head_sha">,
  method: MergeMethod,
  sessionId?: string,
) =>
  call<MergeResult>(context, "POST", `/v1/ae/prs/${card.repo}/${card.number}/merge`, {
    sha: card.head_sha,
    method,
    ...(sessionId ? { session_id: sessionId } : {}),
  });

// ── Admin Center, Fabric section (2026-10-16): `GET /v1/ae/fabric` and its actions ──

/**
 * One capacity as the server reads it from its Logic App; `error` when that
 * read failed. The details after `can_suspend` came with the second pass
 * (2026-10-16): each is null or missing until the Logic App sends it, and the
 * page draws without them. Times are ISO UTC.
 */
export interface FabricCapacity {
  name: string;
  state: string;
  can_suspend: boolean;
  /** Keep awake needs the suspend key too. */
  can_keep_awake?: boolean;
  /** "F4". */
  sku?: string | null;
  /** As ARM writes it: "North Central US" or "northcentralus". */
  region?: string | null;
  /** The ARM resource id, for the Azure portal link. */
  capacityId?: string | null;
  hourlyUsd?: number | null;
  /** The last successful resume. */
  resumedAt?: string | null;
  /** Hours the runbook leaves it up after a resume or a keep awake. */
  maxHours?: number | null;
  /** When the runbook is expected to suspend it if idle (server-side rule). */
  sleepsAt?: string | null;
  /** Third pass (2026-10-16): the runbook does nothing before this. */
  keepAwakeUntil?: string | null;
  /** What each keep awake choice would do, worked out by the server (fabric.py). */
  keepAwakeOptions?: FabricKeepAwakeOption[];
  /** Hours a wake bills at least: the runbook's window and its second quiet run. */
  wakeBilledHours?: number | null;
  /** The capacity fleet (2026-10-21): the Azure subscription's display name. */
  subscriptionName?: string | null;
  resourceGroup?: string | null;
  /** True when the server can resize it (the fleet knows its ARM id). */
  canScale?: boolean;
  /** The sizes a resize may pick ("F2", "F4", ...), the server's allowlist. */
  skuChoices?: string[];
  error?: string;
}

/** One keep awake choice as the server projects it. */
export interface FabricKeepAwakeOption {
  hours: number;
  /** The sleep time after it, if idle; null when the server cannot tell. */
  sleepsAt: string | null;
  /** False when it would not move the sleep time later. */
  addsTime: boolean;
}

export interface FabricOverview {
  configured: boolean;
  capacities: FabricCapacity[];
  /** With none configured: how to turn the page on. */
  note?: string;
  /** True when the rows come from the shared capacities Logic App (2026-10-21). */
  fleet?: boolean;
  /** Why the fleet's list is missing or partial; the rows shown still stand. */
  fleetError?: string;
}

/**
 * What a wake, a suspend or a keep awake answered: since the third pass
 * (2026-10-16) the capacity's full row, as `GET` gives it, with the state the
 * Logic App reported back, plus what the action did.
 */
export interface FabricActionResult extends FabricCapacity {
  action: "wake" | "suspend" | "keepawake" | "scale";
  changed: boolean;
}

export const fetchFabric = (context: ExtensionContext) =>
  call<FabricOverview>(context, "GET", "/v1/ae/fabric");

export const fabricAction = (
  context: ExtensionContext,
  name: string,
  action: "wake" | "suspend",
) =>
  call<FabricActionResult>(
    context,
    "POST",
    `/v1/ae/fabric/${encodeURIComponent(name)}/${action}`,
    {},
  );

/** Keep *name* up for *hours* more (1 to 12; the server checks). */
export const fabricKeepAwake = (context: ExtensionContext, name: string, hours: number) =>
  call<FabricActionResult>(
    context,
    "POST",
    `/v1/ae/fabric/${encodeURIComponent(name)}/keepawake`,
    { hours },
  );

/** Resize *name* to *sku* (one of its skuChoices; the server checks). Billing changes at once. */
export const fabricScale = (context: ExtensionContext, name: string, sku: string) =>
  call<FabricActionResult>(
    context,
    "POST",
    `/v1/ae/fabric/${encodeURIComponent(name)}/scale`,
    { sku },
  );

// ── Admin Center, Databricks section (2026-10-19): `GET /v1/ae/databricks` and its actions ──

/** A SQL warehouse's state as the Databricks API names it. */
export type DatabricksWarehouseState =
  | "RUNNING"
  | "STARTING"
  | "STOPPED"
  | "STOPPING"
  | "DELETING"
  | "DELETED";

/** One SQL warehouse as the server lists it; `url` opens it in the workspace. */
export interface DatabricksWarehouse {
  /** Sixteen hex characters. */
  id: string;
  name: string;
  state: DatabricksWarehouseState | string;
  /** "2X-Small". */
  size: string;
  /** Minutes idle before it stops on its own; 0 means never, null unknown. */
  autoStopMins: number | null;
  serverless: boolean;
  url: string;
}

export type DatabricksSpendStatus = "ok" | "reading" | "never" | "no_access" | "error";

/**
 * The last spend reading (system.billing.usage at list price) and the credit it
 * draws on. A reading runs on the server in the background; `reading` while it
 * is out, `never` until the first one, `no_access` when the service principal
 * lacks the billing grants (`message` names them). Times are ISO UTC.
 */
export interface DatabricksSpend {
  status: DatabricksSpendStatus;
  message: string | null;
  /** When the last good reading finished. */
  readAt: string | null;
  /** The newest usage row in the data: billing tables lag hours behind. */
  asOf: string | null;
  monthUsd: number | null;
  /** Since `creditStart`. */
  totalUsd: number | null;
  creditUsd: number;
  /** Which bar leads: this month's (`monthly`) or the whole credit's (`once`). */
  cadence: "monthly" | "once";
  /** YYYY-MM-DD. */
  creditStart: string;
  creditEnd: string;
  /** The warehouse a reading runs on, if any. */
  warehouseId: string | null;
}

export interface DatabricksOverview {
  configured: boolean;
  /** With nothing configured: how to turn the section on. */
  note: string | null;
  /** The workspace URL. */
  host: string | null;
  /** The listing failed as a whole (auth, timeout); spend still rides along. */
  error: { code: string; message: string } | null;
  warehouses: DatabricksWarehouse[];
  spend: DatabricksSpend | null;
}

export const fetchDatabricks = (context: ExtensionContext) =>
  call<DatabricksOverview>(context, "GET", "/v1/ae/databricks");

/** Start or stop warehouse *id*; the answer is its row after the call. */
export const databricksWarehouseAction = (
  context: ExtensionContext,
  id: string,
  action: "start" | "stop",
) =>
  call<DatabricksWarehouse>(
    context,
    "POST",
    `/v1/ae/databricks/warehouses/${encodeURIComponent(id)}/${action}`,
    {},
  );

/** Start a spend reading (it wakes its warehouse); the answer is the `reading` spend. */
export const refreshDatabricksSpend = (context: ExtensionContext) =>
  call<DatabricksSpend>(context, "POST", "/v1/ae/databricks/spend/refresh", {});

// ── Admin Center, Deploys section (2026-10-19): `/v1/ae/deploy/hold` and resume ──

/**
 * The deploy hold as the server reads it (ae_omni_policies/deploy_hold.py).
 * Times are ISO UTC. `error` is set when the stored hold could not be read;
 * `held` is false then, so deploys go ahead.
 */
export interface DeployHold {
  held: boolean;
  until: string | null;
  set_at: string | null;
  set_by: string | null;
  remaining_s: number;
  /** The hours a hold can be set for: 1, 2 and 3. */
  choices: number[];
  error: string | null;
}

export const fetchDeployHold = (context: ExtensionContext) =>
  call<DeployHold>(context, "GET", "/v1/ae/deploy/hold");

/** Hold deploys for *hours* from now, replacing any current hold (the server checks 1 to 3). */
export const holdDeploys = (context: ExtensionContext, hours: number) =>
  call<DeployHold>(context, "POST", "/v1/ae/deploy/hold", { hours });

/** End the hold. A POST: the page's SDK sends no DELETE. */
export const resumeDeploys = (context: ExtensionContext) =>
  call<DeployHold>(context, "POST", "/v1/ae/deploy/resume", {});

// ── Admin Center, Box host section (2026-10-20): `/v1/ae/host` and the relink request ──

/**
 * The box host as the server sees it (ae_omni_policies/host_relink.py): its
 * connection, and what deploy/host-relink.sh last reported while it is not
 * connected. Times are ISO UTC. `can_request` is false while the host is
 * connected: a relink restarts the host, which ends every session on it.
 */
export interface HostStatus {
  host: { id: string; name: string; status: "online" | "offline" } | null;
  state:
    | "connected"
    | "rejected"
    | "sending"
    | "sent"
    | "restarting"
    | "failed"
    | "offline"
    | "unknown";
  streak: number;
  since: string | null;
  attempt: number;
  attempts: number;
  expires_at: string | null;
  expires_in_s: number;
  message: string | null;
  /** Why a loop ended in `failed`: every link lapsed, the restart failed, or no reminder went out. */
  reason: "lapsed" | "restart" | "push" | null;
  updated_at: string | null;
  requested: boolean;
  requested_at: string | null;
  request_untaken: boolean;
  can_request: boolean;
  error: string | null;
}

export const fetchHostStatus = (context: ExtensionContext) =>
  call<HostStatus>(context, "GET", "/v1/ae/host");

/** Ask the box's helper for a sign-in link; the server answers 409 while the host is connected. */
export const requestHostRelink = (context: ExtensionContext) =>
  call<HostStatus>(context, "POST", "/v1/ae/host/relink", {});

// ── Admin Center, Secrets section (2026-10-20): `/v1/ae/secrets` ──

/** One key the server expects, with what it turns on. Never a value. */
export interface SecretKey {
  key: string;
  present: boolean;
  use: string;
}

/**
 * Each key deploy/pull-secrets.sh writes for the server, with whether the
 * running server has it (ae_omni_policies/secrets_health.py). `missing` names
 * the required keys it lacks.
 */
export interface SecretsStatus {
  required: SecretKey[];
  optional: SecretKey[];
  missing: string[];
}

export const fetchSecrets = (context: ExtensionContext) =>
  call<SecretsStatus>(context, "GET", "/v1/ae/secrets");

// ── Admin Center, Accounts card (2026-10-21): `/v1/ae/limits/accounts` ──

/** One window of an account's reading: the same shape as `GET /v1/ae/limits` windows. */
export type AccountWindow = LimitWindow;

/**
 * One signed-in account's newest reading: labels, plan, percents and reset
 * times only, never an email or a token. `account` is null for the harness's
 * default login; `observed_at` is null for an account with no reading yet;
 * `live_sessions` is null where nothing counts them (Codex).
 */
export interface AccountRow {
  harness: "claude" | "codex";
  account: string | null;
  label: string;
  plan: string | null;
  five_hour: AccountWindow | null;
  weekly: AccountWindow | null;
  observed_at: string | null;
  live_sessions: number | null;
}

export interface AccountsAnswer {
  now: string;
  accounts: AccountRow[];
}

export const fetchAccounts = (context: ExtensionContext) =>
  call<AccountsAnswer>(context, "GET", "/v1/ae/limits/accounts");

export interface AzureFlag {
  resource: string;
  subscription: string;
  day: string;
  cost: number;
  average: number;
  ratio: number;
}

export interface AzureView {
  currency: string;
  month: { spent: number; pace: number | null; lastMonth: number; through: string | null };
  daily: Array<{ date: string; cost: number }>;
  subscriptions: Array<{
    id: string;
    name: string;
    spent: number;
    /** Its own billing currency; a row not counted is never converted into the total's. */
    currency: string;
    counted: boolean;
    error: string | null;
  }>;
  services: Array<{ name: string; cost: number }>;
  flags: AzureFlag[];
}

export interface AzureCostAnswer {
  configured: boolean;
  note: string | null;
  reading?: boolean;
  asOf?: string | null;
  view?: AzureView | null;
}

export const fetchAzureCost = (context: ExtensionContext) =>
  call<AzureCostAnswer>(context, "GET", "/v1/ae/azure-cost");
