import { ACTIVE_MS } from "./config.js";
import { $, h, span, when } from "./dom.js";
import { place, same } from "./render_cycle.js";
import { inScope, sourceOf } from "./state.js";
import { closedLeft, doc, meta } from "./sync.js";
import { bottleneckStat, statGrip, statKey } from "./notices.js";
import { inboxPending, swarm } from "./swarm.js";

const rowSeen = new WeakMap();

function noteSeen(seen, name, at) {
  if (name && at && !["operator", "agent", "earlier"].includes(name)) seen[name] = Math.max(seen[name] || 0, at);
}

function seenIn(item) {
  const source = sourceOf(item);
  const keep = !!source && typeof source === "object";
  if (keep && rowSeen.has(source)) return rowSeen.get(source);
  const seen = {};
  for (const e of [...item.comments, ...(item.answers || [])]) noteSeen(seen, e.by, e.at);
  const pairs = Object.entries(seen);
  const row = { latest: Math.max(0, ...pairs.map(([, at]) => at)), pairs };
  if (keep) rowSeen.set(source, row);
  return row;
}

function lastSeen(since) {
  const seen = {};
  for (const e of meta.events || []) noteSeen(seen, e.by, e.at);
  for (const m of meta.crew || []) noteSeen(seen, m.name, m.last_seen);
  for (const t of [doc.notes, doc.chat, ...doc.notes.map((n) => n.comments)]) for (const e of t) noteSeen(seen, e.by, e.at);
  for (const list of ["phases", "questions", "followups", "tasks"]) for (const i of doc[list]) {
    const row = seenIn(i);
    if (row.latest >= since) for (const [name, at] of row.pairs) noteSeen(seen, name, at);
  }
  return seen;
}

let activeCount = { inputs: [], count: 0 };

function activeAgents() {
  const second = Math.floor(Date.now() / 1000);
  if (same([doc, meta, swarm, second], activeCount.inputs)) return activeCount.count;
  const seen = lastSeen(Date.now() - ACTIVE_MS);
  for (const agent of (swarm && swarm.agents) || []) seen[agent.name] = Math.max(seen[agent.name] || 0, agent.started_at || 0);
  const count = Object.values(seen).filter((at) => Date.now() - at < ACTIVE_MS).length;
  activeCount = { inputs: [doc, meta, swarm, second], count };
  return count;
}

export function timeLeftInputs(calc) {
  if (!calc || !calc.tiers) return undefined;
  const tiers = Object.entries(calc.tiers).map(([name, tier]) => `${name} ${tier.minutes}m from ${tier.samples} samples`).join(", ");
  const slots = calc.slots === null ? "unobserved slots" : `${calc.slots} slots`;
  const ci = calc.ci_minutes === null ? "no CI median yet" : `CI ${calc.ci_minutes}m a task`;
  const held = calc.frozen ? ` · ${calc.frozen} frozen tasks with ${calc.frozen_work}m of work left out` : "";
  return `Larger of a ${calc.chain}m chain and ${calc.work}m of work over ${slots} · ${calc.remaining} tasks left · ${tiers} · ${ci}${held}`;
}

function withRefusals(top, counts) {
  const shown = Object.entries(counts || {}).map(([reason, count]) => `${reason} ${count}`).join(" · ");
  return shown ? { ...(top || {}), waits: [...((top && top.waits) || []), `Admission refusals ${shown}`] } : top;
}

function labLine(entry) {
  const wait = entry.predicted_wait_s === null ? "no free slot" : `${span(entry.predicted_wait_s * 1000)} wait`;
  const ratio = entry.verdict_cache_hit_ratio;
  const hits = typeof ratio === "number" ? `${Math.round(ratio * 100)}% cache hits` : "no cache lookups";
  const parts = [entry.class.replace(/_/g, " "), `${entry.pending} pending`, wait, hits].map((part, i, all) => (i < all.length - 1 ? `${part} ·` : part));
  return h("li", { title: parts.join(" ") }, ...parts.flatMap((part, i) => [i ? " " : "", h("span", { class: "lab-part", text: part })]));
}

function labStat(lab) {
  const age = lab.at ? ` · ${span(Date.now() - lab.at)} ago` : " · never";
  const lines = lab.classes.map(labLine);
  const failed = lab.error ? [h("li", { class: "lab-error", text: `Read failed ${span(Date.now() - lab.error_at)} ago: ${lab.error}` })] : [];
  return h("div", { class: "stat lab-capacity", "data-stat": statKey("Lab") },
    h("dt", {}, statGrip("Lab"), `Lab${age}`),
    h("dd", {}, h("ul", { class: "lab-list" }, ...lines, ...failed)));
}

export function renderStats() {
  const phases = inScope(doc.phases), ups = inScope(doc.followups), tasks = inScope(doc.tasks);
  const total = phases.length + closedLeft("phases"), done = phases.filter((p) => p.done).length + closedLeft("phases");
  const upsDone = ups.filter((f) => f.done).length + closedLeft("followups"), tasksDone = tasks.filter((t) => t.done || t.state === "done").length + closedLeft("tasks");
  const out = ["phases", "questions", "followups"].reduce((n, l) => n + doc[l].length - inScope(doc[l]).length, 0);
  const started = meta.created_at, elapsed = started ? Date.now() - started : null;
  const left = doc.time_left_minutes;
  const refresh = meta.stats_refresh;
  const calc = (meta.time_left && meta.time_left.calculation) || (refresh && refresh.calculation);
  const gap = calc && calc.gap;
  const leftText = gap ? `unknown · ${gap}${left === null ? "" : ` · prior ${span(left * 60000)}`}` : left === null ? "not set" : span(left * 60000);
  const last = (meta.events || []).reduce((m, e) => Math.max(m, e.at || 0), 0);
  const pct = total ? Math.round(100 * done / total) : 0;
  const next = phases.find((p) => !p.done), counts = (swarm && swarm.tasks) || {}, figure = (value) => (swarm ? String(value || 0) : "—");
  const row = (label, value, extra) => h("div", { class: "stat", title: label === "Time left" ? timeLeftInputs(calc) : undefined, style: gap && label === "Time left" ? "grid-template-columns:75px minmax(0,1fr)" : "", "data-stat": statKey(label) }, h("dt", {}, statGrip(label), label), h("dd", {}, value, extra));
  const box = $("stats"), shown = new Map([...box.children].map((el) => [el.outerHTML, el]));
  place(box, [row("Started", started ? when(started) : "—"),
    row("Elapsed", elapsed === null ? "—" : span(elapsed)),
    row("Time left", leftText),
    row("Phases", `${done} / ${total} · ${pct}%`, h("div", { class: "bar" }, h("i", { style: `width:${pct}%` }))),
    row("Tasks", `${tasksDone} / ${tasks.length + closedLeft("tasks")}`),
    row("Follow-ups", `${upsDone} / ${ups.length + closedLeft("followups")}`),
    row("Frozen", calc && calc.frozen !== undefined ? `${calc.frozen} · ${span(calc.frozen_work * 60000)}` : "—"),
    row("Unfrozen", calc && calc.frozen !== undefined ? String(calc.remaining) : "—"),
    row("Open", figure(counts.open)),
    row("Claimed", figure(counts.claimed)),
    row("Blocked", figure(counts.blocked)),
    row("In PR", figure(counts.pr)),
    row("Done today", figure(swarm && swarm.done_today)),
    row("Next phase", next ? next.id : "—"),
    ...(out ? [row("Out of scope", String(out))] : []),
    row("Inbox pending", figure(swarm && inboxPending(swarm))),
    row("Agents · 2 h", String(activeAgents())),
    row("Last activity", last ? `${span(Date.now() - last)} ago` : "—"),
    ...(calc && calc.lab ? [labStat(calc.lab)] : []),
    bottleneckStat(withRefusals(swarm && swarm.top_bottleneck, swarm && swarm.admission))].map((el) => shown.get(el.outerHTML) || el));
  document.dispatchEvent(new Event("stats"));
}
