import { ACTIVE_MS } from "./config.js";
import { $, closedText, excerpt, h, headCount, newId, span, when } from "./dom.js";
import { renderCycle } from "./render_cycle.js";
import { hiddenDone, inScope, itemState, pendingOf, taskBlockers, taskRank, taskRanks } from "./state.js";
import { closedLeft, closedLeftIn, doc, meta, queue, renderSync, toggle } from "./sync.js";
import { renderArtifacts } from "./artifacts.js";
import { commentsView, taskLink, threadView } from "./threads.js";
import { bottleneckStat, renderAlerts, renderNotifications, renderPriorities, statGrip, statKey } from "./notices.js";
import { renderOutline } from "./outline.js";
import { markToggles } from "./folds.js";
import { renderChat, renderChatBadge } from "./chat.js";
import { inboxPending, renderSwarm, swarm } from "./swarm.js";
import { readWorkspace } from "./api.js";
import { closedMore, firstPage, moreButton } from "./pages.js";
import { frozen, held, ownFreeze, snowflake } from "./freezes.js";
import { prepareFrozenView, viewCount, viewItems, viewVisible } from "./frozen_view.js";
import { phaseReview, phaseStatus } from "./phase_review.js";

function scopeDot(key, item, off) {
  if (item.done) return null;
  const out = !!item.out_of_scope;
  return h("button", { class: "scope", type: "button", "data-focus": `scope:${key}`, "aria-pressed": String(out), disabled: off ? "" : false,
    title: out ? "Bring back in scope" : "Mark out of scope", text: "out of scope", on: { click: (ev) => {
      ev.preventDefault();
      if (out || confirm("Mark this item out of scope?")) toggle(key, !out, "out_of_scope");
    } } });
}

function freezeButton(key) {
  const own = ownFreeze(doc, key);
  return h("button", { class: "freeze", type: "button", "data-focus": `freeze:${key}`, "aria-pressed": String(!!own),
    title: own ? "Lift this freeze" : "Freeze this item and everything under it", text: own ? "unfreeze" : "freeze", on: { click: (ev) => {
      ev.preventDefault();
      queue(own ? { op: "freeze_clear", id: newId("freeze"), target: key } : { op: "freeze_set", id: newId("freeze"), verb: "freeze", target: key });
    } } });
}

export function verdictButton(item, verdict, cls, text, off) {
  return h("button", { class: `link ${cls}`, type: "button", text, disabled: off || pendingOf(doc, item) ? "" : false, on: { click: () => queue({ op: "verdict", id: newId("comment"), item, verdict }) } });
}

function itemActions(key, item) {
  const off = frozen(doc, key), freeze = ["phases", "tasks"].includes(key.split("/")[0]) ? freezeButton(key) : null;
  if (item.done || item.out_of_scope) return h("div", { class: "item-actions" }, freeze, scopeDot(key, item, off));
  return h("div", { class: "item-actions" }, verdictButton(key, "approved", "approve", "Approve", off), verdictButton(key, "denied", "deny", "Deny", off), freeze, scopeDot(key, item, off));
}

function holdClass(key) {
  return frozen(doc, key) ? " frozen" : held(doc, key) ? " held" : "";
}

function holdMark(key) {
  return frozen(doc, key) ? snowflake("Frozen") : held(doc, key) ? snowflake("Held by a focus") : null;
}

function itemClass(item, key) {
  return "item" + (item.done ? " done" : "") + (item.out_of_scope ? " out" : "") + (key ? holdClass(key) : "");
}

function checkRow(list, item, n, titleText, descText) {
  const key = `${list}/${item.id}`;
  const box = h("input", { type: "checkbox", "data-focus": `check:${key}`, "aria-label": titleText, disabled: item.out_of_scope || frozen(doc, key) ? "" : false });
  box.checked = !!item.done;
  box.addEventListener("change", () => toggle(key, box.checked, "done"));
  const body = h("div", {}, h("div", { class: "text", text: titleText }),
    descText ? h("p", { class: "desc", text: descText }) : null);
  const row = h("div", { class: "row" }, box, n ? h("span", { class: "num", text: `${n}.` }) : null, body, itemActions(key, item));
  return h("li", { class: itemClass(item, key), id: `item-${list}-${item.id}` }, holdMark(key), row, commentsView(key, item.comments));
}

function phaseRow(item, n) {
  const key = `phases/${item.id}`;
  const box = h("input", { type: "checkbox", "data-focus": `check:${key}`, "aria-label": item.title, disabled: item.out_of_scope || frozen(doc, key) ? "" : false });
  box.checked = !!item.done;
  box.addEventListener("change", () => toggle(key, box.checked, "done"));
  const body = h("div", { class: "phase-body" },
    h("div", { class: "phase-head" }, h("span", { class: "item-title", text: item.title }), h("span", { class: "phase-state", text: phaseStatus(item).label })),
    item.description ? h("p", { class: "desc", text: item.description }) : null, phaseSlices(item), phaseReview(item));
  const row = h("div", { class: "row" }, box, h("span", { class: "num", text: `${n}.` }), h("div", {}, body), itemActions(key, item));
  return h("li", { class: itemClass(item, key), id: `item-phases-${item.id}` }, holdMark(key), row, commentsView(key, item.comments));
}

const sliceTaskCounts = new Map();

function phaseSlices(phase) {
  const rows = viewItems("slices", doc.slices).filter((s) => s.phase === `phases/${phase.id}`);
  if (!rows.length) return null;
  const count = (s) => (sliceTaskCounts.get(`slices/${s.id}`) || 0) + closedLeftIn("tasks", `slices/${s.id}`);
  return h("div", { class: "phase-slices" }, h("span", { class: "phase-slices-label", text: "slices" }),
    ...rows.map((s) => h("span", { class: "slice" + holdClass(`slices/${s.id}`), id: `item-slices-${s.id}` }, holdMark(`slices/${s.id}`),
      h("span", { class: "slice-name", text: `${s.anchor} ${count(s)}` }), freezeButton(`slices/${s.id}`))));
}

function planGroups() {
  const known = new Set(doc.plans.map((p) => `plans/${p.id}`));
  const groups = doc.plans.map((plan) => ({ key: `plans/${plan.id}`, id: `item-plans-${plan.id}`, title: plan.title || plan.id, plan,
    phases: viewItems("phases", doc.phases).filter((p) => p.plan === `plans/${plan.id}`) })).filter((g) => viewVisible(g.key));
  const loose = viewItems("phases", doc.phases).filter((p) => !known.has(p.plan));
  return loose.length ? [...groups, { key: "unplanned", id: "phases-unplanned", title: "Phases without a plan", phases: loose }] : groups;
}

function planActions(key, plan) {
  if (!plan) return null;
  return h("span", { class: "item-actions plan-actions" }, freezeButton(key), scopeDot(key, plan, frozen(doc, key)));
}

function planRow({ key, id, title, phases, plan }) {
  const idOf = (p) => `item-phases-${p.id}`;
  const total = phases.length + closedLeftIn("phases", key);
  const head = h("div", { class: "plan-head" }, plan ? holdMark(key) : null, h("span", { class: "plan-title", text: title }),
    h("span", { class: "plan-count", text: `${total} ${total === 1 ? "phase" : "phases"}` }), planActions(key, plan));
  const list = h("ol", { class: "plan-phases" }, ...firstPage(key, phases, idOf).map((p, i) => phaseRow(p, i + 1)),
    moreButton(key, phases.length, "more phases", render), phases.length ? null : h("li", { class: "empty", text: "None." }));
  return h("li", { class: "plan" + (plan && plan.out_of_scope ? " out" : "") + (plan ? holdClass(key) : ""), id }, head, list);
}

function phaseList(cooperative = false, current = () => true) {
  sliceTaskCounts.clear();
  for (const task of doc.tasks) if (!task.deleted && task.slice) sliceTaskCounts.set(task.slice, (sliceTaskCounts.get(task.slice) || 0) + 1);
  if (!doc.plans.length) return listInto("phases", viewItems("phases", doc.phases), (p, i) => phaseRow(p, i + 1), undefined, closedMore("phases", "done phases", render));
  if (!sectionOpen("phases")) return;
  const groups = planGroups();
  const target = $("phases");
  const empty = groups.length ? "" : h("li", { class: "empty", text: "All phases are hidden." });
  if (!cooperative) return target.replaceChildren(...groups.map(planRow), empty, closedMore("phases", "done phases", render) || "");
  const paint = async () => {
    const rows = document.createDocumentFragment();
    for (const group of groups) {
      await scheduler.postTask(() => {}, { priority: "background" });
      if (!current()) return;
      rows.append(planRow(group));
    }
    if (current()) target.replaceChildren(rows, empty, closedMore("phases", "done phases", render) || "");
  };
  return paint();
}

function questionRow(item, n) {
  const key = `questions/${item.id}`;
  const row = h("div", { class: "row" }, h("span", { class: "num", text: `${n}.` }), h("div", { class: "item-title", text: item.text }),
    itemActions(key, item));
  return h("li", { class: itemClass(item), id: `item-questions-${item.id}` }, row, threadView(`${key}/answers`, item.answers, "thread"),
    commentsView(key, item.comments));
}

function proofRows(...sources) {
  return sources.flatMap((o) => Object.entries(o || {})).filter(([, v]) => String(v).trim());
}

function proofList(rows) {
  const label = (k) => k[0].toUpperCase() + k.slice(1).replace("_", " ");
  const value = (v) => /^https?:\/\//.test(v) ? h("dd", {}, h("a", { href: v, target: "_blank", rel: "noopener", text: v })) : h("dd", { text: v });
  return h("dl", {}, ...rows.flatMap(([k, v]) => [h("dt", { text: label(k) }), value(v)]));
}

const workTails = new Map();

function workButton(item) {
  if (!item.workspace || workTails.has(item.id)) return null;
  return h("button", { class: "link", type: "button", text: "Show work folder", on: { click: () => readWorkspace(item.id)
    .then((resp) => (resp.ok ? resp.json() : null)).then((tails) => { if (tails) { workTails.set(item.id, tails.data); render(); } }).catch(() => null) } });
}

function taskProof(item) {
  if (!item.workspace && !proofRows(item.contract, item.proof).length) return null;
  return h("div", { class: "task-proof" }, h("div", { class: "task-proof-head", text: "Contract and proof" }),
    proofList(proofRows(item.contract, item.proof, workTails.get(item.id))), workButton(item));
}

function rankPick(key, item) {
  const sending = pendingOf(doc, key), rank = sending && sending.op === "task_rank" ? sending.rank : taskRank(item);
  const pick = h("select", { class: `rank-pick rank-${rank}${sending ? " pending" : ""}`, "aria-label": `Queue rank of ${item.title}`, "data-focus": `rank:${key}`,
    disabled: sending || item.state === "done" || frozen(doc, key) ? "" : false, on: { change: () => queue({ op: "task_rank", id: newId("rank"), item: key, rank: pick.value }) } },
    ...taskRanks().map((r) => h("option", { value: r, text: r })));
  pick.value = rank;
  return pick;
}

function difficultyLabel(item) {
  if (!["S", "M", "L"].includes(item.difficulty)) return null;
  const confidence = typeof item.difficulty_confidence === "number" ? `, confidence ${Math.round(item.difficulty_confidence * 100)}%` : "";
  return h("span", { class: `difficulty difficulty-${item.difficulty}`, text: `size ${item.difficulty}`, title: `set by ${item.difficulty_source || "operator"}${confidence}` });
}

function awaitingLabel(item) {
  const awaiting = item.state === "done" ? "" : (item.awaiting || "").replace(/[_-]+/g, " ");
  return awaiting ? h("span", { class: "awaiting", text: awaiting }) : null;
}

function taskRow(item, tasks) {
  const key = `tasks/${item.id}`;
  const waits = taskBlockers(item, tasks || []);
  const link = (url, label) => /^https?:\/\//.test(url || "") ? h("a", { href: url, target: "_blank", rel: "noopener", text: label }) : null;
  const meta = h("div", { class: "task-meta" }, rankPick(key, item), difficultyLabel(item), h("span", { text: item.lane }), item.kind ? h("span", { class: "kind", text: item.kind }) : null,
    h("span", { text: item.state }), awaitingLabel(item),
    item.phase ? h("span", { text: item.phase }) : null,
    item.claimed_by ? h("span", { class: "claimed", text: `claimed by ${item.claimed_by}` }) : null,
    link(item.plan_url, "plan"), link(item.issue_url, "issue"), link(item.pr_url, "pull request"));
  const body = h("div", {}, h("div", { class: "item-title" }, taskLink(item.id), h("span", { text: item.title })),
    item.description ? h("p", { class: "desc", text: item.description }) : null, meta,
    waits ? h("p", { class: "desc", text: waits }) : null);
  const row = h("div", { class: "row" }, body, itemActions(key, item));
  return h("li", { class: itemClass(item, key), id: `item-tasks-${item.id}` }, holdMark(key), row, taskProof(item), commentsView(key, item.comments));
}

function taskCounts(tasks) {
  const order = ["open", "claimed", "pr", "blocked", "done"];
  const n = {};
  for (const t of tasks) n[t.state || "open"] = (n[t.state || "open"] || 0) + 1;
  if (closedLeft("tasks")) n.done = (n.done || 0) + closedLeft("tasks");
  const states = [...order, ...Object.keys(n).filter((st) => !order.includes(st))];
  return headCount(states.map((st) => [n[st], st]));
}

function stateCounts(list, labels) {
  const n = {};
  for (const i of doc[list].filter((i) => !i.deleted)) {
    const st = itemState(list, i);
    n[st] = (n[st] || 0) + 1;
  }
  if (closedLeft(list)) n.done = (n.done || 0) + closedLeft(list);
  return headCount(Object.entries(labels).map(([st, label]) => [n[st], label]));
}

function sectionOpen(id) {
  const box = $(id).closest("details");
  if (!box || box.open) return true;
  $(id).replaceChildren();
  return false;
}

function listInto(id, items, make, idOf = (i) => `item-${id}-${i.id}`, tail = null) {
  const el = $(id);
  if (!sectionOpen(id)) return;
  el.replaceChildren(...firstPage(id, items, idOf).map(make), moreButton(id, items.length, `more ${id}`, render) || "", tail || "");
  if (!items.length) el.append(h("li", { class: "empty", text: doc[id].some((i) => !i.deleted) ? "All items are hidden." : "None." }));
}

export function render(focusKey, cooperative = false) {
  return renderCycle(focusKey, cooperative, (current) => {
    prepareFrozenView(doc);
    document.title = doc.title;
    $("title").textContent = doc.title;
    $("overview").textContent = excerpt(doc.overview, 200);
    $("closed-label").textContent = closedText(doc.closed_at);
    $("closed-banner").hidden = !doc.closed_at;
    const sections = [
      () => {
        $("sources-count").textContent = headCount([[doc.sources.length]]);
        listInto("sources", doc.sources.map((s, n) => ({ id: n, text: s })), (s) => h("li", { id: `item-sources-${s.id}`, text: s.text }));
      },
      () => {
        $("phases-count").textContent = stateCounts("phases", { open: "open", done: "done" }) + viewCount("phases", doc.phases);
        return phaseList(cooperative, current);
      },
      () => {
        $("tasks-count").textContent = taskCounts(doc.tasks) + viewCount("tasks", doc.tasks);
        const known = [...doc.tasks, ...[...hiddenDone(doc, "tasks")].map((id) => ({ id, state: "done" }))];
        groupedWork("tasks", viewItems("tasks", doc.tasks), (t) => taskRow(t, known));
      },
      () => {
        $("questions-count").textContent = stateCounts("questions", { open: "open", done: "answered" });
        listInto("questions", [...doc.questions].sort((a, b) => (a.answers || []).length - (b.answers || []).length), (q, i) => questionRow(q, i + 1), undefined, closedMore("questions", "answered questions", render));
      },
      () => {
        $("notes-count").textContent = headCount([[doc.notes.filter((e) => !e.deleted).length]]);
        if (sectionOpen("notes")) $("notes").replaceChildren(threadView("notes", doc.notes, "thread flat"));
      },
      () => {
        $("followups-count").textContent = stateCounts("followups", { open: "open", done: "done" }) + viewCount("followups", doc.followups);
        groupedWork("followups", viewItems("followups", doc.followups), (f) => checkRow("followups", f, 0, f.text, ""));
      },
      () => { renderSync(); }, () => { renderPriorities(); }, () => { renderNotifications(); }, () => { renderAlerts(); },
      () => { renderArtifacts(); }, () => { renderChat(); }, () => { renderChatBadge(); }, () => { renderStats(); },
      () => { renderSwarm(swarm); }, () => { renderOutline(); }, () => { markToggles(); },
    ];
    return sections;
  });
}

const NOUNS = { tasks: "tasks", followups: "follow-ups" };

function groupedWork(name, items, row) {
  if (!sectionOpen(name)) return;
  const idOf = (i) => `item-${name}-${i.id}`;
  const live = items.filter((i) => !i.deleted && !i.done && i.state !== "done");
  const done = items.filter((i) => !i.deleted && (i.done || i.state === "done"));
  const rank = { pr: 0, claimed: 1, open: 2, blocked: 3 };
  live.sort((a, b) => (rank[a.state || "open"] ?? 4) - (rank[b.state || "open"] ?? 4) || taskRanks().indexOf(taskRank(a)) - taskRanks().indexOf(taskRank(b)));
  $(name).replaceChildren(...firstPage(name, live, idOf).map(row), moreButton(name, live.length, `more ${NOUNS[name]}`, render) || "");
  if (!items.length && doc[name].some((i) => !i.deleted)) $(name).append(h("li", { class: "empty", text: "All items are hidden." }));
  if (!done.length && !closedLeft(name)) return;
  done.reverse();
  const key = `${name}-done`;
  $(name).append(h("li", { class: "done-group", id: key }, h("div", { class: "done-head", text: `${done.length + closedLeft(name)} done` }),
    h("ol", {}, ...done.map(row), closedMore(name, `done ${NOUNS[name]}`, render) || "")));
}

function lastSeen() {
  const seen = {};
  const note = (name, at) => {
    if (name && at && !["operator", "agent", "earlier"].includes(name)) seen[name] = Math.max(seen[name] || 0, at);
  };
  for (const e of meta.events || []) note(e.by, e.at);
  for (const m of meta.crew || []) note(m.name, m.last_seen);
  const threads = [doc.notes, doc.chat, ...doc.notes.map((n) => n.comments)];
  for (const list of ["phases", "questions", "followups", "tasks"]) for (const i of doc[list]) threads.push(i.comments, i.answers || []);
  for (const t of threads) for (const e of t) note(e.by, e.at);
  return seen;
}

function activeAgents() {
  const seen = lastSeen();
  for (const agent of (swarm && swarm.agents) || []) seen[agent.name] = Math.max(seen[agent.name] || 0, agent.started_at || 0);
  return Object.values(seen).filter((at) => Date.now() - at < ACTIVE_MS).length;
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
  $("stats").replaceChildren(row("Started", started ? when(started) : "—"),
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
    bottleneckStat(withRefusals(swarm && swarm.top_bottleneck, swarm && swarm.admission)));
  document.dispatchEvent(new Event("stats"));
}
