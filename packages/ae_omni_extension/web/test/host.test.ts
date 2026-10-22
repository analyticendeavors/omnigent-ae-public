// The Admin Center's Box host section (2026-10-20): whether the box host is
// connected, what deploy/host-relink.sh last reported, and the button that
// asks it for a reconnect link. Every call is mocked: the routes are GET
// /v1/ae/host and POST /v1/ae/host/relink.
import type { ExtensionContext } from "@omnigent/extension-sdk";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { HostStatus } from "../src/api";
import {
  ASK_LABEL,
  activateHost,
  buttonLabel,
  countdownText,
  deactivateHost,
  failedText,
  hostSection,
  metaItems,
  nextDelay,
  POLL_CONNECTED_MS,
  POLL_DISCONNECTED_MS,
  stateText,
  toneOf,
} from "../src/host";

const sdk = vi.hoisted(() => ({ defineExtension: vi.fn() }));
vi.mock("@omnigent/extension-sdk", () => sdk);

type Reply = { ok: boolean; status: number; body: unknown };

const GET = "GET /v1/ae/host";
const POST = "POST /v1/ae/host/relink";
const NOW = Date.parse("2026-10-20T18:05:48Z");

const status = (extra: Partial<HostStatus> = {}): HostStatus => ({
  host: { id: "host_box", name: "ae-llm-box", status: "offline" },
  state: "offline",
  streak: 0,
  since: null,
  attempt: 0,
  attempts: 5,
  expires_at: null,
  expires_in_s: 0,
  message: null,
  reason: null,
  updated_at: null,
  requested: false,
  requested_at: null,
  request_untaken: false,
  can_request: true,
  error: null,
  ...extra,
});

const connected = () =>
  status({
    host: { id: "host_box", name: "ae-llm-box", status: "online" },
    state: "connected",
    can_request: false,
  });
const rejected = () =>
  status({ state: "rejected", streak: 41, since: "2026-10-20T18:02:00Z" });
const sent = () =>
  status({
    state: "sent",
    attempt: 1,
    expires_at: "2026-10-20T18:10:00Z",
    expires_in_s: 252,
    can_request: false,
  });

const ok = (body: unknown): Reply => ({ ok: true, status: 200, body });
const failed = (code: number, message: string): Reply => ({
  ok: false,
  status: code,
  body: { error: { code: "x", message } },
});

type Call = { method: string; path: string; body?: unknown };

function stubContext(replies: Record<string, Reply[]>) {
  const request = vi.fn(async ({ method, path }: Call) => {
    const queue = replies[`${method} ${path}`] ?? [];
    return queue.shift() ?? failed(500, `no reply queued for ${path}`);
  });
  const context = { view: "fabric", server: { request } } as unknown as ExtensionContext;
  return { context, request };
}

const flush = () => vi.advanceTimersByTimeAsync(0);

async function mount(context: ExtensionContext): Promise<HTMLElement> {
  const node = hostSection();
  document.getElementById("root")?.appendChild(node);
  activateHost(context, node);
  await flush();
  return node;
}

const buttons = (node: ParentNode) => [...node.querySelectorAll<HTMLButtonElement>("button")];
const pill = (node: ParentNode) => node.querySelector(".fabric-state")?.textContent;
const meta = (node: ParentNode) =>
  [...node.querySelectorAll(".fabric-meta-item")].map((item) => item.textContent);
const notices = (node: ParentNode) =>
  [...node.querySelectorAll(".notice")].map((item) => item.textContent);

beforeEach(() => {
  document.body.replaceChildren();
  const root = document.createElement("div");
  root.id = "root";
  document.body.appendChild(root);
  vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout", "Date"] });
  vi.setSystemTime(NOW);
});

afterEach(() => {
  deactivateHost();
  vi.useRealTimers();
  vi.restoreAllMocks();
});

describe("the words", () => {
  it("gives each state a tone and a pill", () => {
    expect(toneOf("connected")).toBe("on");
    expect(toneOf("rejected")).toBe("failed");
    expect(toneOf("failed")).toBe("failed");
    expect(toneOf("sent")).toBe("busy");
    expect(toneOf("offline")).toBe("unknown");
    expect(stateText("sent")).toBe("Waiting for sign-in");
    expect(stateText("rejected")).toBe("Rejected");
  });

  it("counts a link down from when its answer arrived", () => {
    expect(countdownText(sent(), NOW, NOW)).toBe("4:12 left");
    expect(countdownText(sent(), NOW, NOW + 13_000)).toBe("3:59 left");
    expect(countdownText(sent(), NOW, NOW + 600_000)).toBe("0:00 left");
  });

  it("says how far the helper got", () => {
    expect(metaItems(rejected(), NOW, NOW)[0]).toMatch(/^Rejected 41 times in a row since /);
    expect(metaItems(sent(), NOW, NOW)).toEqual([
      "Link 1 of 5 sent to your phone",
      expect.stringMatching(/^Expires .+ \(4:12 left\)$/),
    ]);
    expect(metaItems(status({ state: "failed", reason: "lapsed" }), NOW, NOW)).toEqual([
      "5 links lapsed without a sign-in",
    ]);
    expect(metaItems(connected(), NOW, NOW)).toEqual([]);
  });

  it("offers the button only when a link can be asked for", () => {
    expect(buttonLabel(connected())).toBeNull();
    expect(buttonLabel(rejected())).toBe(ASK_LABEL);
    // A link is out: the loop is running, so no second one.
    expect(buttonLabel(sent())).toBeNull();
    expect(buttonLabel(status({ requested: true, can_request: false }))).toBe("Asking the box...");
    expect(buttonLabel(status({ state: "sending", can_request: false }))).toBeNull();
  });

  it("reads often while disconnected and redraws every second during a countdown", () => {
    expect(nextDelay(connected())).toBe(POLL_CONNECTED_MS);
    expect(nextDelay(rejected())).toBe(POLL_DISCONNECTED_MS);
    expect(nextDelay(sent())).toBe(1000);
    expect(nextDelay(null)).toBe(POLL_DISCONNECTED_MS);
  });
});

describe("a failed loop says why", () => {
  it("tells lapsed links from a restart that failed and reminders that never went out", () => {
    expect(failedText(status({ state: "failed", reason: "lapsed" }))).toBe(
      "5 links lapsed without a sign-in",
    );
    expect(failedText(status({ state: "failed", reason: "restart" }))).toBe(
      "Signed in, but the host did not restart",
    );
    expect(failedText(status({ state: "failed", reason: "push", attempts: 1 }))).toBe(
      "1 link could not be sent to your phone",
    );
  });

  it("puts the restart case in its notice", async () => {
    const { context } = stubContext({ [GET]: [ok(status({ state: "failed", reason: "restart" }))] });
    const node = await mount(context);
    expect(pill(node)).toBe("Not reconnected");
    expect(meta(node)).toEqual(["Signed in, but the host did not restart"]);
    expect(notices(node)[0]).toMatch(/did not restart/);
    expect(buttons(node)[0]?.textContent).toBe(ASK_LABEL);
  });
});

describe("the section", () => {
  it("hides the button while the host is connected", async () => {
    const { context } = stubContext({ [GET]: [ok(connected())] });
    const node = await mount(context);
    expect(pill(node)).toBe("Connected");
    expect(node.querySelector(".card-fabric")?.classList.contains("fabric-on")).toBe(true);
    expect(node.querySelector(".fabric-row-solo")).not.toBeNull();
    expect(buttons(node)).toEqual([]);
    expect(notices(node)).toEqual([]);
    expect(node.querySelector(".host-refreshed")?.textContent).toMatch(/^Updated /);
  });

  it("shows the button and the 403 notice while the host is rejected", async () => {
    const { context } = stubContext({ [GET]: [ok(rejected())] });
    const node = await mount(context);
    expect(pill(node)).toBe("Rejected");
    expect(node.querySelector(".fabric-name")?.textContent).toBe("ae-llm-box");
    const [ask] = buttons(node);
    expect(ask?.textContent).toBe(ASK_LABEL);
    expect(ask?.classList.contains("btn-tint")).toBe(true);
    expect(notices(node)[0]).toMatch(/HTTP 403/);
  });

  it("counts a sent link down every second", async () => {
    const { context, request } = stubContext({ [GET]: [ok(sent()), ok(sent())] });
    const node = await mount(context);
    expect(pill(node)).toBe("Waiting for sign-in");
    expect(meta(node)[1]).toMatch(/\(4:12 left\)$/);
    // The countdown only: no button while the loop runs.
    expect(buttons(node)).toEqual([]);
    await vi.advanceTimersByTimeAsync(3000);
    expect(meta(node)[1]).toMatch(/\(4:09 left\)$/);
    // The countdown redraws without reading; a read follows after 15 s.
    expect(request.mock.calls.length).toBe(1);
    await vi.advanceTimersByTimeAsync(13_000);
    expect(request.mock.calls.length).toBe(2);
  });

  it("asks the server for a link and says what comes next", async () => {
    const asked = status({ state: "rejected", streak: 41, requested: true, can_request: false });
    const { context, request } = stubContext({
      [GET]: [ok(rejected()), ok(asked)],
      [POST]: [ok(asked)],
    });
    const node = await mount(context);
    buttons(node)[0]?.click();
    await flush();
    const posts = request.mock.calls.filter(([call]) => (call as Call).method === "POST");
    expect(posts).toEqual([[{ method: "POST", path: "/v1/ae/host/relink", body: {} }]]);
    expect(node.querySelector(".host-status")?.textContent).toMatch(/task reminder within a minute/);
    const [waiting] = buttons(node);
    expect(waiting?.textContent).toBe("Asking the box...");
    expect(waiting?.disabled).toBe(true);
  });

  it("says so when the host reconnected before the tap landed", async () => {
    const { context } = stubContext({
      [GET]: [ok(rejected()), ok(connected())],
      [POST]: [failed(409, "The box host is connected, so it needs no reconnect link.")],
    });
    const node = await mount(context);
    buttons(node)[0]?.click();
    await flush();
    expect(node.querySelector(".host-status")?.textContent).toBe(
      "The host is connected again, so it needs no link.",
    );
    expect(pill(node)).toBe("Connected");
    expect(buttons(node)).toEqual([]);
  });

  it("warns when the box has not picked a request up", async () => {
    const late = status({ requested: true, can_request: false, request_untaken: true });
    const { context } = stubContext({ [GET]: [ok(late)] });
    const node = await mount(context);
    expect(notices(node).some((text) => text?.includes("ae-omnigent-host-relink.service"))).toBe(
      true,
    );
  });

  it("says a failed read and keeps the last good card", async () => {
    const { context } = stubContext({ [GET]: [ok(rejected()), failed(502, "upstream down")] });
    const node = await mount(context);
    await vi.advanceTimersByTimeAsync(POLL_DISCONNECTED_MS);
    expect(pill(node)).toBe("Rejected");
    expect(notices(node).at(-1)).toMatch(/could not be read \(upstream down\)/);
  });

  it("shows only the failure when the first read fails", async () => {
    const { context } = stubContext({ [GET]: [failed(500, "boom")] });
    const node = await mount(context);
    expect(node.querySelector(".card-fabric")).toBeNull();
    expect(notices(node)[0]).toMatch(/could not be read \(boom\)/);
  });
});

describe("an ask and a read already out", () => {
  it("reads again after the ask instead of trusting a read that began before it", async () => {
    const waiting: Record<string, Array<(reply: Reply) => void>> = {};
    const request = vi.fn(
      ({ method, path }: Call) =>
        new Promise<Reply>((resolve) => {
          (waiting[`${method} ${path}`] ??= []).push(resolve);
        }),
    );
    const context = { view: "fabric", server: { request } } as unknown as ExtensionContext;
    const answer = (key: string, reply: Reply) => waiting[key]?.shift()?.(reply);
    const node = hostSection();
    document.getElementById("root")?.appendChild(node);
    activateHost(context, node);
    answer(GET, ok(rejected()));
    await flush();
    // A focus read goes out, then the tap, before that read answers.
    window.dispatchEvent(new Event("focus"));
    await flush();
    buttons(node)[0]?.click();
    await flush();
    const asked = status({ state: "rejected", streak: 41, requested: true, can_request: false });
    answer(POST, ok(asked));
    await flush();
    // The stale read answers now, from before the tap; it must not put the button back.
    answer(GET, ok(rejected()));
    await flush();
    expect(buttons(node)[0]?.disabled).toBe(true);
    // A fresh read began after the ask; its answer is what the card shows.
    const gets = request.mock.calls.filter(([call]) => (call as Call).method === "GET");
    expect(gets.length).toBe(3);
    answer(GET, ok(asked));
    await flush();
    expect(buttons(node)[0]?.textContent).toBe("Asking the box...");
  });
});
