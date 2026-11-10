// The Social view's Scorecard tab (2026-11-06, part 2): five numbers, an 8-week LinkedIn chart,
// uploads that went public without a post, the tool rotation and the upload pace.
//
// The week that holds the first YouTube read is "First week": it reads neither on pace nor
// behind and never counts toward the streak. Weeks before it are drawn muted as "before
// start", not as empty bars, because nothing was owed then.

import type { SocialScore, SocialView } from "./api";
import { el } from "./dom";
import { alertTriangleIcon, circleCheckIcon, flameIcon, linkedinIcon, youtubeIcon } from "./icons";
import { dayKeyOf, shortDate } from "./calendar";
import { CHANNEL_NAMES, DAY } from "./socialWords";

const SVG = "http://www.w3.org/2000/svg";

function svg<K extends keyof SVGElementTagNameMap>(
  tag: K,
  attrs: Record<string, string | number>,
  text?: string,
): SVGElementTagNameMap[K] {
  const node = document.createElementNS(SVG, tag);
  for (const [name, value] of Object.entries(attrs)) node.setAttribute(name, String(value));
  if (text !== undefined) node.textContent = text;
  return node;
}

const plural = (n: number, one: string, many = `${one}s`) => `${n} ${n === 1 ? one : many}`;

export interface Kpi {
  label: string;
  value: string;
  note: string;
  good: boolean;
  flame?: boolean;
}

/** The five numbers, in the order the Scorecard shows them. */
export function kpis(score: SocialScore): Kpi[] {
  const hit = score.week_posted >= score.week_target;
  return [
    {
      label: "LinkedIn this week",
      value: `${score.week_posted} of ${score.week_target}`,
      note: score.first_week ? "First week" : score.on_pace ? "On pace" : "Behind",
      good: score.first_week || score.on_pace,
    },
    {
      label: "Promoted, 30 d",
      value: `${score.promoted} of ${score.promoted_of}`,
      note: score.missed > 0 ? `${score.missed} missed` : "All done",
      good: score.missed === 0,
    },
    {
      label: "Streak",
      value: plural(score.streak, "week"),
      note: score.first_week ? `First week is not counted · best ${score.best_streak}` : `Best: ${plural(score.best_streak, "week")}${hit ? " · target hit" : ""}`,
      good: score.first_week || hit || score.streak > 0,
      flame: true,
    },
    {
      label: "On time, 30 d",
      value: score.on_time_pct === null ? "None yet" : `${score.on_time_pct}%`,
      note: score.on_time_pct === null ? "No posts in 30 d" : `${score.on_time_of} marked posted`,
      good: score.on_time_pct === null || score.on_time_pct >= 70,
    },
    {
      label: "Community, 30 d",
      value: `${score.community_30d.posted} of ${score.community_30d.total}`,
      note:
        score.community_30d.total === score.community_30d.posted
          ? score.community_30d.total === 0
            ? "None due"
            : "All posted"
          : `${score.community_30d.total - score.community_30d.posted} open`,
      good: score.community_30d.total === score.community_30d.posted,
    },
  ];
}

function kpiBox(kpi: Kpi): HTMLElement {
  return el(
    "div",
    { class: `social-stat ${kpi.good ? "stat-ok" : "stat-attention"}` },
    el("span", { class: "social-stat-l" }, kpi.label),
    el("span", { class: "social-stat-n" }, kpi.flame ? flameIcon() : "", kpi.value),
    el("span", { class: "social-stat-note" }, kpi.note),
  );
}

/** "Sep 21" for a week's Monday. */
const weekLabel = (start: string) => shortDate(start);

/** The 8-week bar chart as inline SVG: the target as a dashed line, this week outlined. */
export function chart(score: SocialScore): SVGElement {
  const weeks = score.weeks;
  const top = Math.max(score.week_target, ...weeks.map((w) => w.posted), 1);
  const ceiling = top + (top >= 4 ? 1 : 0);
  const W = 640;
  const H = 240;
  const left = 30;
  const bottom = 28;
  const topPad = 14;
  const plot = H - bottom - topPad;
  const step = (W - left - 8) / Math.max(1, weeks.length);
  const barW = step * 0.62;
  const y = (n: number) => topPad + plot - (plot * n) / ceiling;
  const root = svg("svg", {
    class: "sv-chart",
    viewBox: `0 0 ${W} ${H}`,
    role: "img",
    "aria-label": `LinkedIn posts per week, last ${weeks.length} weeks; target ${score.week_target} a week`,
  });
  for (let n = 0; n <= ceiling; n += ceiling > 6 ? 2 : 1) {
    root.appendChild(svg("line", { x1: left, x2: W - 4, y1: y(n), y2: y(n), class: "sv-grid" }));
    root.appendChild(svg("text", { x: left - 6, y: y(n) + 4, class: "sv-axis", "text-anchor": "end" }, String(n)));
  }
  weeks.forEach((week, at) => {
    const x = left + at * step + (step - barW) / 2;
    const last = at === weeks.length - 1;
    const before = week.state === "before_start";
    if (before) {
      root.appendChild(svg("rect", { x, y: y(0) - 3, width: barW, height: 3, rx: 1.5, class: "sv-bar-before" }));
      root.appendChild(svg("text", { x: x + barW / 2, y: y(0) - 9, class: "sv-bar-before-t", "text-anchor": "middle" }, "before start"));
    } else if (last) {
      const h = Math.max(plot * week.posted / ceiling, 0);
      root.appendChild(svg("rect", { x, y: y(week.posted), width: barW, height: h, rx: 3, class: "sv-bar sv-bar-now" }));
      const open = Math.max(plot * Math.max(score.week_target - week.posted, 0) / ceiling, 0);
      if (open > 0) {
        root.appendChild(svg("rect", { x, y: y(Math.max(score.week_target, week.posted)), width: barW, height: open, rx: 3, class: "sv-bar-open" }));
      }
      root.appendChild(svg("text", { x: x + barW / 2, y: y(week.posted) - 6, class: "sv-bar-note", "text-anchor": "middle" }, `${week.posted} so far`));

    } else {
      root.appendChild(svg("rect", { x, y: y(week.posted), width: barW, height: plot * week.posted / ceiling, rx: 3, class: `sv-bar${week.state === "first" ? " sv-bar-first" : ""}` }));
      if (week.state === "first") {
        root.appendChild(svg("text", { x: x + barW / 2, y: y(week.posted) - 6, class: "sv-bar-note", "text-anchor": "middle" }, "first week"));
      }
    }
    root.appendChild(svg("text", { x: x + barW / 2, y: H - 8, class: "sv-axis", "text-anchor": "middle" }, weekLabel(week.start)));
  });
  root.appendChild(svg("line", { x1: left, x2: W - 4, y1: y(score.week_target), y2: y(score.week_target), class: "sv-target" }));
  root.appendChild(svg("text", { x: W - 6, y: y(score.week_target) - 5, class: "sv-target-l", "text-anchor": "end" }, `Target ${score.week_target}`));
  return root;
}

function panel(title: string, ...children: Array<HTMLElement | null>): HTMLElement {
  return el("section", { class: "sv-panel" }, el("h4", { class: "social-h" }, title), ...children);
}

const platformIcon = (platform: string) => (platform === "linkedin" ? linkedinIcon(14) : youtubeIcon(14));

export interface ScoreHooks {
  /** Post late: open this post in the editor. */
  postLate: (id: string) => void;
}

/** The whole tab for *view*. */
export function drawScorecard(view: SocialView, now: number, hooks: ScoreHooks): HTMLElement {
  const score = view.score;
  // Dates read on the rules' clock, as everywhere else in the view, not as the UTC date of the ISO string.
  const onDay = (iso: string) => shortDate(dayKeyOf(Date.parse(iso), view.rules.tz));
  const rootNode = el("div", { class: "sv-score" });
  rootNode.appendChild(el("div", { class: "social-stats sv-kpis" }, ...kpis(score).map(kpiBox)));
  rootNode.appendChild(
    panel(
      "LinkedIn posts per week",
      el(
        "p",
        { class: "meta" },
        `Last ${score.weeks.length} weeks. The dashed line is your target of ${score.week_target}; the outlined bar is this week so far.${score.first_week ? " This is your first week, so it is not counted yet." : ""}`,
      ),
      el("div", { class: "sv-chart-wrap" }, chart(score)),
    ),
  );
  const late = score.unposted;
  rootNode.appendChild(
    panel(
      `Went public without a post (${score.unposted_total})`,
      late.length
        ? el(
            "div",
            { class: "sv-list" },
            ...late.map((row) => {
              const b = el("button", { type: "button", class: "btn btn-quiet", title: "Open this post in the editor" }, "Post late");
              b.addEventListener("click", () => hooks.postLate(row.id));
              return el(
                "div",
                { class: "sv-line" },
                el("span", { class: "social-platform" }, platformIcon(row.platform)),
                el("span", { class: "sv-line-text" }, row.title ?? row.id, el("span", { class: "meta" }, ` · public ${onDay(row.due)}${row.channel ? ` · ${CHANNEL_NAMES[row.channel] ?? row.channel}` : ""}`)),
                b,
              );
            }),
            score.unposted_total > late.length ? el("p", { class: "meta" }, `Plus ${score.unposted_total - late.length} older.`) : null,
          )
        : el("p", { class: "meta" }, "Nothing missed in the last 30 days."),
    ),
  );
  rootNode.appendChild(
    panel(
      "Tool rotation",
      score.rotation.length
        ? el(
            "div",
            { class: "sv-list" },
            ...score.rotation.map((tool) =>
              el(
                "div",
                { class: "sv-line" },
                el("span", { class: "sv-line-text" }, tool.name, el("span", { class: "meta" }, tool.last ? ` · last promoted ${daysAgo(tool.last, now)}` : " · not promoted yet")),
                el("span", { class: "meta" }, tool.next ? `next ${onDay(tool.next)}` : "no slot yet"),
              ),
            ),
          )
        : el("p", { class: "meta" }, "No tool is active. Turn one on in Settings."),
    ),
  );
  rootNode.appendChild(
    panel(
      "Upload pace, next 14 days",
      el(
        "div",
        { class: "sv-list" },
        ...Object.entries(score.pace).map(([channel, counts]) => {
          const total = counts.long + counts.short + counts.live;
          const parts = [
            counts.long ? plural(counts.long, "video") : "",
            counts.short ? plural(counts.short, "Short") : "",
            counts.live ? plural(counts.live, "livestream") : "",
          ].filter(Boolean);
          return el(
            "div",
            { class: "sv-line" },
            el("span", { class: "social-platform" }, youtubeIcon(14)),
            el("span", { class: "sv-line-text" }, CHANNEL_NAMES[channel] ?? channel.toUpperCase(), el("span", { class: "meta" }, ` · ${parts.join(", ") || "nothing scheduled"}`)),
            el(
              "span",
              { class: `social-chip ${total ? "chip-ok" : "chip-warn"}` },
              total ? circleCheckIcon() : alertTriangleIcon(),
              total ? "OK" : "Gap",
            ),
          );
        }),
      ),
    ),
  );
  return rootNode;
}

/** "8 d ago" for a mark more than a day old, "today" for a newer one. */
export function daysAgo(iso: string, now: number): string {
  const days = Math.floor((now - Date.parse(iso)) / DAY);
  return days < 1 ? "today" : `${days} d ago`;
}
