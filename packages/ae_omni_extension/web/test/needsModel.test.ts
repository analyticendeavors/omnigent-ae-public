// What the Admin Center's table, tabs and rail work out (2026-11-14, concept D2; needsModel.ts):
// the grouping by area then source, the words a folded row says, the counts, the tab rule and the
// opened row's facts. No page is drawn here.
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { resetAttention, setAttention } from "../src/attentionStore";
import { resetCardState, setCards, setHidden } from "../src/cardState";
import {
  AREA_OF,
  AREA_TAB_MIN_CARDS,
  agoText,
  areaCounts,
  areaFoldId,
  areaPreview,
  buildAreas,
  connectedCards,
  defaultOpenIds,
  evidenceText,
  factsOf,
  hasSeveralKinds,
  kindName,
  rowsShown,
  sinceText,
  sourcePreview,
  tabsFor,
  unreadSources,
} from "../src/needsModel";
import { resetRowOrder, trackRowOrder } from "../src/attention";
import { resetTiles, setTile, type DomainId } from "../src/tiles";
import { item, many, overview } from "./attentionFixtures";

beforeEach(() => {
  resetTiles();
  resetAttention();
  resetCardState();
});
afterEach(() => {
  resetTiles();
  resetAttention();
  resetCardState();
});

const sample = () => [
  ...many(4, "pr-failing"),
  item("box-1", { source: "box", kind: "heat", url: "", title: "CPU at 98C" }),
  item("host-1", { source: "host", kind: "host-failed", tone: "review", url: "", title: "Host not connected" }),
  item("v-1", { source: "vault", kind: "vault-soon", tone: "review", url: "", title: "Key expires" }),
  item("acc-1", { source: "accounts", kind: "account-full", tone: "review", url: "", title: "Window full" }),
];

describe("the area of each source", () => {
  it("puts git and git-mine in Ship, box, host, home and secrets in Box, cost, databricks and vault in Cloud, accounts and family in Accounts", () => {
    const area = (ids: string[]) => ids.map((id) => AREA_OF[id as keyof typeof AREA_OF]);
    expect(area(["git", "git-mine"])).toEqual(["ship", "ship"]);
    expect(area(["box", "host", "home", "secrets"])).toEqual(["box", "box", "box", "box"]);
    expect(area(["cost", "databricks", "vault"])).toEqual(["cloud", "cloud", "cloud"]);
    expect(area(["accounts", "family"])).toEqual(["money", "money"]);
  });
});

describe("grouping", () => {
  it("groups by area in the page's order, then by source, urgent rows first", () => {
    const areas = buildAreas(sample());
    expect(areas.map((a) => a.id)).toEqual(["ship", "box", "cloud", "money"]);
    expect(areas[1]!.sources.map((s) => s.source)).toEqual(["box", "host"]);
    expect(areas[0]!.sources[0]!.block.items).toHaveLength(4);
    expect(areas[0]!.label).toBe("Ship");
  });

  it("counts an area by the one rule: a source's items not held by a working chat", () => {
    const held = item("g-held", {
      kind: "pr-failing",
      tone: "review",
      working: { session_id: "s", title: "Review", path: "/x", status: "working" },
    });
    const areas = buildAreas([...many(2, "pr-failing"), held]);
    expect([areas[0]!.urgent, areas[0]!.review, areas[0]!.working]).toEqual([2, 0, 1]);
  });

  it("limits to one area, or to one source, as an area tab and a click on the bar do", () => {
    expect(buildAreas(sample(), { area: "box" }).map((a) => a.id)).toEqual(["box"]);
    expect(buildAreas(sample(), { only: "vault" }).map((a) => a.id)).toEqual(["cloud"]);
  });

  it("keeps the items of a source whose card this viewer hid (2026-11-14)", () => {
    setCards({
      revision: 1,
      state_ok: true,
      cards: [
        {
          id: "git", name: "Git repos", group: "ship", description: "", sources: ["git"], on: true,
          default_on: true, changed_by: null, changed_at: null, fields: [],
        },
      ],
    });
    setHidden(["git"]);
    expect(buildAreas(sample()).map((a) => a.id)).toContain("ship");
  });
});

describe("what a folded row says", () => {
  it("lists an area's sources with counts", () => {
    const [ship, box] = buildAreas([...sample(), ...many(1, "pr-ready", { source: "git-mine" })]);
    expect(areaPreview(ship!)).toBe("Git repos 4 · My Git repos 1");
    expect(areaPreview(box!)).toBe("Box health 1 · Box host 1");
  });

  it("names a source's first item and how many more", () => {
    const areas = buildAreas(sample());
    expect(sourcePreview(areas[0]!.sources[0]!.block)).toMatch(/ and 3 more$/);
    expect(sourcePreview(areas[1]!.sources[0]!.block)).toBe("CPU at 98C");
    expect(sourcePreview({ items: [] })).toBe("");
  });

  it("shows at most five rows of an open source, then Show N more", () => {
    const items = many(7, "pr-failing");
    expect(rowsShown(items, false)).toEqual({ shown: items.slice(0, 5), rest: 2 });
    expect(rowsShown(items, true)).toEqual({ shown: items, rest: 0 });
    expect(rowsShown(items.slice(0, 5), false).rest).toBe(0);
  });

  it("puts the kind before a title only when the source has more than one kind", () => {
    expect(hasSeveralKinds(many(3, "pr-failing"))).toBe(false);
    expect(hasSeveralKinds([...many(1, "pr-failing"), ...many(1, "pr-ready")])).toBe(true);
    expect(kindName(item("x", { kind: "alerts-fix" }))).toBe("Alerts to fix");
    expect(kindName(item("x", { kind: "nothing" }))).toBe("");
  });

  it("opens every area and closes every source on first sight, urgent or not", () => {
    const areas = buildAreas(sample());
    const open = defaultOpenIds(areas);
    for (const area of areas) {
      expect(open.get(areaFoldId(area.id))).toBe(true);
      for (const entry of area.sources) expect(open.get(entry.block.id)).toBe(false);
    }
    expect(open.get("attn.git")).toBe(false);
    expect(open.get("attn.box")).toBe(false);
  });
});

describe("counts and unread sources", () => {
  it("reads an area's counts from the attention store, the same rule as the table", () => {
    expect(areaCounts("ship")).toBeNull();
    setAttention(overview(sample()));
    expect(areaCounts("ship")).toEqual({ urgent: 4, review: 0 });
    expect(areaCounts("box")).toEqual({ urgent: 1, review: 1 });
    expect(areaCounts("cloud")).toEqual({ urgent: 0, review: 1 });
  });

  it("finds the sources the answer says it could not read", () => {
    expect([...unreadSources({ unknown: ["Git repos could not be read", "Key Vault could not be read; the count may be low"] })].sort()).toEqual(["git", "vault"]);
    expect(unreadSources(null).size).toBe(0);
  });
});

describe("the tab rule", () => {
  const connected = (counts: Record<DomainId, number>) =>
    Object.fromEntries(Object.entries(counts).map(([id, n]) => [id, Array.from({ length: n }, (_, i) => `${id}${i}`)])) as Record<DomainId, string[]>;

  it("gives a server with four connected cards or fewer no area tabs", () => {
    expect(AREA_TAB_MIN_CARDS).toBe(4);
    expect(tabsFor(connected({ ship: 2, box: 1, cloud: 1, money: 0 })).map((t) => t.id)).toEqual(["overview", "services"]);
    expect(tabsFor(connected({ ship: 0, box: 0, cloud: 0, money: 0 })).map((t) => t.id)).toEqual(["overview", "services"]);
  });

  it("gives five or more connected cards a tab for each area that has one, between Overview and Services", () => {
    expect(tabsFor(connected({ ship: 3, box: 2, cloud: 0, money: 1 })).map((t) => t.id)).toEqual([
      "overview", "ship", "box", "money", "services",
    ]);
    expect(tabsFor(connected({ ship: 3, box: 2, cloud: 5, money: 2 })).map((t) => t.label)).toEqual([
      "Overview", "Ship", "Box", "Cloud", "Accounts", "Services",
    ]);
  });

  it("counts a card as connected unless it is off, hidden or not set up", () => {
    expect(connectedCards("cloud")).toEqual(["compute", "credit", "azure", "vault", "decision"]);
    setTile({ id: "vault", value: "Not set up", note: "", health: "idle", notSetUp: true });
    expect(connectedCards("cloud")).not.toContain("vault");
    setHidden(["compute"]);
    expect(connectedCards("cloud")).not.toContain("compute");
    setTile({ id: "credit", value: "Not set up", note: "", health: "idle", notSetUp: true });
    expect(connectedCards("cloud")).not.toContain("credit");
    expect(connectedCards("cloud")).toEqual(["azure", "decision"]);
  });
});

describe("the words in a row", () => {
  it("says how long ago, in the shortest words", () => {
    const now = Date.parse("2026-11-14T12:00:00Z");
    expect(sinceText(now - 20_000, now)).toBe("now");
    expect(sinceText(now - 5 * 60_000, now)).toBe("5 min");
    expect(sinceText(now - 3 * 3_600_000, now)).toBe("3 h");
    expect(sinceText(now - 72 * 3_600_000, now)).toBe("3 d");
    expect(agoText("2026-11-14T11:58:00Z", now)).toBe("2 min ago");
    expect(agoText("2026-11-14T11:59:50Z", now)).toBe("just now");
    expect(agoText(undefined, now)).toBe("");
  });

  it("builds the opened row's facts from what the item carries, and nothing it does not", () => {
    const backOn = (days: number) => `in ${days}`;
    const facts = factsOf(item("a", { kind: "pr-failing", back_if: "a new commit still fails", expires_days: 3 }), [], backOn);
    expect(facts.map((f) => [f.label, f.value])).toEqual([
      ["Source", "Git repos"],
      ["Group", "Failing PRs"],
      ["Comes back if", "a new commit still fails"],
      ["Dismiss lasts", "3 days (to in 3)"],
    ]);
    const fixed = factsOf(item("b", { dismissible: false, kind: "nothing", back: "3 days passed" }), [{ label: "Checks", value: "14 of 14 passing" }], backOn);
    expect(fixed.map((f) => f.label)).toEqual(["Source", "Checks", "Came back"]);
  });

  it("says what the source reports and when it was read", () => {
    expect(evidenceText(item("a", { meta: "web · checks failing" }), "9:50 AM", null)).toBe("Git repos reports: web · checks failing. Read at 9:50 AM.");
    expect(evidenceText(item("a"), "", "14 of 14 checks passing")).toBe("Git repos reports: 14 of 14 checks passing.");
    // 2026-11-18: text that already ends a sentence gets no second period.
    expect(evidenceText(item("a", { meta: "systemd reports these units failed." }), "1:29 PM", null)).toBe("Git repos reports: systemd reports these units failed. Read at 1:29 PM.");
    expect(evidenceText(item("a", { meta: "Is it hot?" }), "", null)).toBe("Git repos reports: Is it hot?");
    expect(evidenceText(item("a", { meta: "Disk full!" }), "", null)).toBe("Git repos reports: Disk full!");
  });
});

import { areaCut, assignColumns, columnCount, listCost, placeLines, planOpen, sourceList, unknownLines } from "../src/needsModel";

describe("the open plan (2026-11-15)", () => {
  const areas = () => buildAreas([...many(7, "pr-failing"), item("v1", { source: "vault", kind: "vault-soon", tone: "review", url: "" }), item("a1", { source: "accounts", kind: "account-full", tone: "review", url: "" })]);

  it("opens everything when it fits", () => {
    const open = planOpen(areas(), 5000);
    expect([...open.values()].every(Boolean)).toBe(true);
    expect(buildAreas(sample()).map((a) => a.id)[0]).toBe("ship");
  });

  it("folds the least urgent areas when the list would overflow, and never one holding an urgent item", () => {
    const open = planOpen(areas(), 500);
    expect(open.get(areaFoldId("ship"))).toBe(true);
    expect(open.get("attn.git")).toBe(true);
    expect(open.get(areaFoldId("cloud"))).toBe(false);
    expect(open.get(areaFoldId("money"))).toBe(false);
  });

  it("collapsed (the Overview): every area open, every source closed, whatever the budget or urgency, a saved choice wins", () => {
    const open = planOpen(areas(), 0, () => null, 1, undefined, true);
    expect(open.get(areaFoldId("ship"))).toBe(true);
    expect(open.get("attn.git")).toBe(false); // urgent, still closed
    expect(open.get(areaFoldId("cloud"))).toBe(true);
    expect(open.get("attn.vault")).toBe(false);
    const touched = (id: string) => (id === "attn.vault" ? true : id === areaFoldId("ship") ? false : null);
    const mine = planOpen(areas(), 0, touched, 1, undefined, true);
    expect(mine.get("attn.vault")).toBe(true);
    expect(mine.get(areaFoldId("ship"))).toBe(false);
    expect(mine.get("attn.git")).toBe(false);
  });

  it("an area tab keeps the budgeted plan: the same call without collapsed folds the least urgent when it overflows", () => {
    const open = planOpen(areas(), 500);
    expect(open.get("attn.git")).toBe(true); // urgent stays open in the budgeted plan
    expect(open.get(areaFoldId("money"))).toBe(false);
  });

  it("lets a person's own choice win over the plan, open or closed", () => {
    const touched = (id: string) => (id === areaFoldId("money") ? true : id === areaFoldId("ship") ? false : null);
    const open = planOpen(areas(), 500, touched);
    expect(open.get(areaFoldId("money"))).toBe(true);
    expect(open.get(areaFoldId("ship"))).toBe(false);
  });

  it("keeps the page's order whatever is urgent: an urgent Cloud item does not move Cloud above Ship", () => {
    const listed = buildAreas([
      item("c1", { source: "cost", kind: "cost-over", tone: "urgent", url: "" }),
      item("g1", { kind: "pr-failing", tone: "review" }),
    ]);
    expect(listed.map((a) => a.id)).toEqual(["ship", "cloud"]);
  });

  it("keeps an area's sources in their fixed order when a later one is urgent", () => {
    const [box] = buildAreas([
      item("h1", { source: "host", kind: "host-failed", tone: "urgent", url: "" }),
      item("b1", { source: "box", kind: "heat", tone: "review", url: "" }),
    ]);
    expect(box!.sources.map((s) => s.source)).toEqual(["box", "host"]);
  });
});

describe("rows keep their place (2026-11-16)", () => {
  const ids = (list: ReturnType<typeof buildAreas>) => list[0]!.sources[0]!.block.items.map((i) => i.id);
  const git = (id: string, tone: "urgent" | "review" = "review") => item(id, { kind: "pr-failing", tone });
  // The table numbers the whole answer first, then groups what it shows.
  const draw = (items: ReturnType<typeof git>[]) => {
    trackRowOrder(items);
    return buildAreas(items);
  };
  beforeEach(() => resetRowOrder());

  it("lists the first draw in the server's order, urgent or not", () => {
    expect(ids(draw([git("a"), git("b", "urgent"), git("c")]))).toEqual(["a", "b", "c"]);
  });

  it("keeps a row's place when its tone changes, in either direction", () => {
    draw([git("a"), git("b"), git("c")]);
    expect(ids(draw([git("a"), git("b", "urgent"), git("c")]))).toEqual(["a", "b", "c"]);
    expect(ids(draw([git("c", "urgent"), git("b"), git("a")]))).toEqual(["a", "b", "c"]);
  });

  it("puts a new row at the end of its source", () => {
    draw([git("a"), git("b")]);
    expect(ids(draw([git("n", "urgent"), git("a"), git("b")]))).toEqual(["a", "b", "n"]);
  });

  it("lets a vanished row leave without moving the others", () => {
    draw([git("a"), git("b"), git("c")]);
    expect(ids(draw([git("a"), git("c")]))).toEqual(["a", "c"]);
  });

  it("puts a restored or woken item at the end of its source: it left the list, so it is new again", () => {
    draw([git("a"), git("b"), git("c")]);
    draw([git("a"), git("c")]);
    expect(ids(draw([git("a"), git("b"), git("c")]))).toEqual(["a", "c", "b"]);
  });

  it("prunes the sequence numbers of rows that left, so the map does not grow", () => {
    draw([git("a"), git("b"), git("c")]);
    draw([git("c")]);
    // Had "a" and "b" been kept, they would still sort before "c" when they return.
    expect(ids(draw([git("a"), git("b"), git("c")]))).toEqual(["c", "a", "b"]);
  });

  it("does not number rows when only grouped (a caller that never prunes): the given order stands", () => {
    expect(ids(buildAreas([git("b"), git("a")]))).toEqual(["b", "a"]);
    expect(ids(draw([git("a"), git("b")]))).toEqual(["a", "b"]);
    expect(ids(buildAreas([git("b"), git("a")]))).toEqual(["a", "b"]);
  });

  it("starts fresh on a reload: the server's order again", () => {
    draw([git("a"), git("b")]);
    resetRowOrder();
    expect(ids(draw([git("b"), git("a")]))).toEqual(["b", "a"]);
  });

  it("does not move Box above Ship, and dismissing the urgent Box item moves nothing", () => {
    const ship = [git("g1"), git("g2")];
    const box = item("b1", { source: "box", kind: "heat", tone: "urgent", url: "" });
    expect(buildAreas([...ship, box]).map((a) => a.id)).toEqual(["ship", "box"]);
    expect(buildAreas(ship.concat(item("b2", { source: "box", kind: "heat", tone: "review", url: "" }))).map((a) => a.id)).toEqual(["ship", "box"]);
    expect(buildAreas(ship).map((a) => a.id)).toEqual(["ship"]);
  });
});

describe("the plan's columns and cost (2026-11-16)", () => {
  const two = () => buildAreas([...many(7, "pr-failing"), item("v1", { source: "vault", kind: "vault-soon", tone: "review", url: "" }), item("a1", { source: "accounts", kind: "account-full", tone: "review", url: "" })]);

  it("costs the tallest column, not the sum of them all", () => {
    const areas = two();
    const one = listCost(areas, 1);
    const cols = listCost(areas, 2);
    expect(cols).toBeLessThan(one);
    expect(cols).toBeGreaterThan(0);
  });

  it("places an area where columnOf says, not where the row counts would put it", () => {
    const areas = two();
    const left = planOpen(areas, 400, () => null, 2, () => 0);
    const split = planOpen(areas, 400, () => null, 2);
    // Everything forced into column 0 runs out of room sooner than the greedy placement.
    expect([...left.values()].filter(Boolean).length).toBeLessThanOrEqual([...split.values()].filter(Boolean).length);
    expect(listCost(areas, 2, () => 0)).toBeGreaterThan(listCost(areas, 2));
  });
});

describe("unread lines and a cut list", () => {
  it("matches each line to its source, and lists names in a sentence", () => {
    expect(unknownLines({ unknown: ["Git repos could not be read", "Something else"] })).toEqual([
      { words: "Git repos could not be read", source: "git" },
      { words: "Something else", source: null },
    ]);
    expect(sourceList(["git", "box"])).toBe("Git repos and Box health");
    expect(sourceList(["git", "box", "vault"])).toBe("Git repos, Box health and Key Vault");
    expect(sourceList([])).toBe("");
  });

  it("counts the rows the server cut from an area (by_source against the listed rows)", () => {
    setAttention(overview(many(3, "pr-failing"), { counts: { urgent: 9, review: 1, by_source: { git: { urgent: 9, review: 1 } } } }));
    expect(areaCut("ship")).toBe(7);
    expect(areaCut("box")).toBe(0);
  });
});

describe("columns", () => {
  it("counts clamp(floor(width / 900), 1, 3) and stays one under 1,800", () => {
    expect([0, 1700, 1799, 1800, 2530, 2700, 5120, 9000].map((w) => columnCount(w))).toEqual([1, 1, 1, 2, 2, 3, 3, 3]);
  });

  it("keeps the column count it had until the width falls 40 px below its threshold", () => {
    expect(columnCount(1780, 2)).toBe(2);
    expect(columnCount(1760, 2)).toBe(2);
    expect(columnCount(1759, 2)).toBe(1);
    expect(columnCount(1780, 1)).toBe(1);
    expect(columnCount(2670, 3)).toBe(3);
    expect(columnCount(2659, 3)).toBe(2);
    expect(columnCount(1000, 3)).toBe(1);
  });

  it("assigns each area, in order, to the column with the fewest lines so far, lowest on a tie", () => {
    expect(assignColumns([10, 4, 4, 4], 2)).toEqual([0, 1, 1, 1]);
    expect(assignColumns([5, 5, 5, 5], 3)).toEqual([0, 1, 2, 0]);
    expect(assignColumns([3, 9], 1)).toEqual([0, 0]);
  });

  it("estimates the fold from the tallest column, not the sum of every area", () => {
    const areas = buildAreas(
      [
        ...many(5, "pr-failing"),
        ...[1, 2, 3].map((n) => item(`v-${n}`, { source: "vault", kind: "vault-soon", tone: "review", url: "", title: `k${n}`, actions: ["review"] })),
        ...[1, 2].map((n) => item(`a-${n}`, { source: "accounts", kind: "account-full", tone: "review", url: "", title: `w${n}`, actions: ["review"] })),
      ],
      {},
    );
    const nothing = () => null;
    const single = planOpen(areas, 700, nothing, 1);
    const two = planOpen(areas, 700, nothing, 2);
    const folded = (m: Map<string, boolean>) => areas.filter((a) => !m.get(areaFoldId(a.id))).map((a) => a.id);
    expect(folded(single).length).toBeGreaterThan(0);
    expect(folded(two).length).toBeLessThan(folded(single).length);
    // The placement count ignores folds: the same whether the area is open or closed.
    expect(placeLines(areas[0]!)).toBe(1 + 1 + 5);
  });
});
