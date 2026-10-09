import { ACTIVE_MS, PHASE_LABELS } from "./config.js";
import { $, closedText, grow, h, headCount, newId, span, when } from "./dom.js";
import { inScope, itemState, phaseLifecycle, taskRank, taskRanks } from "./state.js";
import { doc, meta, queue, renderSync, toggle } from "./sync.js";
import { renderArtifacts } from "./artifacts.js";
import { closedComments, commentsView, openComments, rememberComment, taskLink, threadView } from "./threads.js";
import { renderAlerts, renderNotifications, renderPriorities } from "./notices.js";
import { renderOutline } from "./outline.js";
import { collapsible, markToggles } from "./folds.js";
import { renderChat, renderChatBadge } from "./chat.js";
import { inboxPending, renderSwarm, swarm } from "./swarm.js";
import { readWorkspace } from "./api.js";
import { firstPage, lazy, moreButton, wanted } from "./pages.js";

function scopeDot(key, item) {
  if (item.done) return null;
  const out = !!item.out_of_scope;
  return h("button", { class: "scope", type: "button", "data-focus": `scope:${key}`, "aria-pressed": String(out),
    title: out ? "Bring back in scope" : "Mark out of scope", text: "out of scope", on: { click: () => {
      if (out || confirm("Mark this item out of scope?")) toggle(key, !out, "out_of_scope");
    } } });
}

export function verdictButton(item, verdict, cls, text) {
  return h("button", { class: `link ${cls}`, type: "button", text, on: { click: () => queue({ op: "verdict", id: newId("comment"), item, verdict }) } });
}

function itemActions(key, item) {
  if (item.done || item.out_of_scope) return h("div", { class: "item-actions" }, scopeDot(key, item));
  return h("div", { class: "item-actions" }, verdictButton(key, "approved", "approve", "Approve"), verdictButton(key, "denied", "deny", "Deny"), scopeDot(key, item));
}

function itemClass(item) {
  return "item" + (item.done ? " done" : "") + (item.out_of_scope ? " out" : "");
}

function checkRow(list, item, n, titleText, descText) {
  const key = `${list}/${item.id}`;
  const box = h("input", { type: "checkbox", "data-focus": `check:${key}`, "aria-label": titleText, disabled: item.out_of_scope ? "" : false });
  box.checked = !!item.done;
  box.addEventListener("change", () => toggle(key, box.checked, "done"));
  const body = h("div", {}, h("div", { class: "text", text: titleText }),
    descText ? h("p", { class: "desc", text: descText }) : null);
  const row = h("div", { class: "row" }, box, n ? h("span", { class: "num", text: `${n}.` }) : null, body, itemActions(key, item));
  return h("li", { class: itemClass(item), id: `item-${list}-${item.id}` }, row, commentsView(key, item.comments));
}

function phaseLabel(phase, doc) {
  const state = phaseLifecycle(phase, doc);
  if (state !== "waiting") return PHASE_LABELS[state];
  const byId = Object.fromEntries(doc.phases.map((p) => [p.id, p]));
  const waits = phase.depends_on.filter((id) => !(byId[id] || {}).done).map((id) => (byId[id] || {}).title || id);
  return `Waits for phase ${waits.join(", ")}`;
}

function phaseReview(phase) {
  const review = phase.review || {};
  if (phaseLifecycle(phase, doc) !== "in_review" || (review.state === "sent_back" && !review.escalated)) return null;
  const note = h("input", { class: "hl-note phase-note", type: "text", maxlength: "500", placeholder: "note for the planner", "aria-label": "Review note" });
  const decide = (state) => {
    const text = note.value.trim();
    if (state === "sent_back" && !text) return note.focus();
    queue({ op: "phase_review", id: newId("review"), by: "operator", item: `phases/${phase.id}`, state, ...(text ? { note: text } : {}) });
  };
  const button = (text, state) => {
    const btn = h("button", { class: "sw-btn", type: "button", text });
    btn.addEventListener("click", () => decide(state));
    return btn;
  };
  return h("div", { class: "hl-verdict phase-review" }, note, button("Approve plan", "approved"), button("Send back", "sent_back"));
}

function phaseRow(item, n) {
  const key = `phases/${item.id}`;
  const box = h("input", { type: "checkbox", "data-focus": `check:${key}`, "aria-label": item.title, disabled: item.out_of_scope ? "" : false });
  box.checked = !!item.done;
  box.addEventListener("change", () => toggle(key, box.checked, "done"));
  const fold = h("details", { class: "phase-fold" },
    h("summary", {}, h("span", { class: "item-title", text: item.title }), h("span", { class: "phase-state", text: phaseLabel(item, doc) })),
    item.description ? h("p", { class: "desc", text: item.description }) : null, phaseSlices(item), phaseReview(item));
  fold.open = !closedComments.has(`${key}/row`);
  fold.addEventListener("toggle", () => rememberComment(`${key}/row`, fold.open));
  const row = h("div", { class: "row" }, box, h("span", { class: "num", text: `${n}.` }), h("div", {}, fold), itemActions(key, item));
  return h("li", { class: itemClass(item), id: `item-phases-${item.id}` }, row, commentsView(key, item.comments));
}

function phaseSlices(phase) {
  const rows = doc.slices.filter((s) => s.phase === `phases/${phase.id}`);
  if (!rows.length) return null;
  const count = (s) => doc.tasks.filter((t) => !t.deleted && t.slice === `slices/${s.id}`).length;
  return h("div", { class: "phase-slices" }, h("span", { class: "phase-slices-label", text: "slices" }),
    ...rows.map((s) => h("span", { class: "slice", text: `${s.anchor} ${count(s)}` })));
}

function planGroups() {
  const known = new Set(doc.plans.map((p) => `plans/${p.id}`));
  const groups = doc.plans.map((plan) => ({ key: `plans/${plan.id}`, id: `item-plans-${plan.id}`, title: plan.title || plan.id,
    phases: doc.phases.filter((p) => p.plan === `plans/${plan.id}`) }));
  const loose = doc.phases.filter((p) => !known.has(p.plan));
  return loose.length ? [...groups, { key: "unplanned", id: "phases-unplanned", title: "Phases without a plan", phases: loose }] : groups;
}

function planRow({ key, id, title, phases }) {
  const idOf = (p) => `item-phases-${p.id}`;
  const fold = h("details", { class: "plan-fold" }, h("summary", {}, h("span", { class: "plan-title", text: title }),
    h("span", { class: "plan-count", text: `${phases.length} ${phases.length === 1 ? "phase" : "phases"}` })));
  fold.open = !closedComments.has(`${key}/row`) || phases.some((p) => idOf(p) === wanted.id);
  fold.addEventListener("toggle", () => rememberComment(`${key}/row`, fold.open));
  lazy(fold, () => h("ol", { class: "plan-phases" }, ...firstPage(key, phases, idOf).map((p, i) => phaseRow(p, i + 1)),
    moreButton(key, phases.length, "more phases", render), phases.length ? null : h("li", { class: "empty", text: "None." })));
  return h("li", { class: "plan", id }, fold);
}

function phaseList() {
  if (!doc.plans.length) return listInto("phases", doc.phases, (p, i) => phaseRow(p, i + 1));
  if (sectionOpen("phases")) $("phases").replaceChildren(...planGroups().map(planRow));
}

function questionRow(item, n) {
  const key = `questions/${item.id}`;
  const row = h("div", { class: "row" }, h("span", { class: "num", text: `${n}.` }), h("div", { class: "item-title", text: item.text }),
    itemActions(key, item));
  return h("li", { class: itemClass(item), id: `item-questions-${item.id}` }, row, threadView(`${key}/answers`, item.answers, "thread"),
    commentsView(key, item.comments));
}

function taskBlockers(task, tasks) {
  if ((task.state || "open") !== "open") return "";
  const byId = Object.fromEntries(tasks.map((t) => [t.id, t]));
  const title = (id) => (byId[id] ? byId[id].title : `task ${id}`);
  const unfinished = (ids) => (ids || []).filter((id) => (byId[id] || {}).state !== "done");
  const done = (ids) => ids.map((id) => `${title(id)} is done`).join(", and ");
  const parked = unfinished(task.parked_on);
  if (parked.length) return `Parked on ${task.branch ? `branch ${task.branch}` : "its branch"} until ${done(parked)}`;
  const open = unfinished(task.depends_on);
  const held = open.filter((id) => !(["claimed", "pr"].includes((byId[id] || {}).state) && byId[id].branch));
  if (held.length) return `Waiting until ${done(held)}`;
  return open.length ? `Ready to start on ${open.map((id) => `branch ${byId[id].branch} of ${title(id)}`).join(", and ")}` : "";
}

function proofRows(...sources) {
  return sources.flatMap((o) => Object.entries(o || {})).filter(([, v]) => String(v).trim());
}

function proofList(rows) {
  const label = (k) => k[0].toUpperCase() + k.slice(1).replace("_", " ");
  const value = (v) => /^https?:\/\//.test(v) ? h("dd", {}, h("a", { href: v, target: "_blank", rel: "noopener", text: v })) : h("dd", { text: v });
  return h("dl", {}, ...rows.flatMap(([k, v]) => [h("dt", { text: label(k) }), value(v)]));
}

function proofBody(item) {
  const list = proofList(proofRows(item.contract, item.proof));
  if (item.workspace) readWorkspace(item.id).then((resp) => (resp.ok ? resp.json() : null)).then((tails) => {
    if (tails) list.replaceWith(proofList(proofRows(item.contract, item.proof, tails.data)));
  }).catch(() => null);
  return list;
}

function taskProof(key, item) {
  if (!item.workspace && !proofRows(item.contract, item.proof).length) return null;
  const box = h("details", { class: "task-proof" }, h("summary", {}, "Contract and proof"));
  box.open = openComments.has(key);
  box.addEventListener("toggle", () => rememberComment(key, box.open));
  return lazy(box, () => proofBody(item));
}

function rankPick(key, item) {
  const pick = h("select", { class: `rank-pick rank-${taskRank(item)}`, "aria-label": `Queue rank of ${item.title}`, "data-focus": `rank:${key}`,
    disabled: item.state === "done" ? "" : false, on: { change: () => queue({ op: "task_rank", id: newId("rank"), item: key, rank: pick.value }) } },
    ...taskRanks().map((r) => h("option", { value: r, text: r })));
  pick.value = taskRank(item);
  return pick;
}

function difficultyLabel(item) {
  if (!["S", "M", "L"].includes(item.difficulty)) return null;
  const confidence = typeof item.difficulty_confidence === "number" ? `, confidence ${Math.round(item.difficulty_confidence * 100)}%` : "";
  return h("span", { class: `difficulty difficulty-${item.difficulty}`, text: `size ${item.difficulty}`, title: `set by ${item.difficulty_source || "operator"}${confidence}` });
}

function taskRow(item, tasks) {
  const key = `tasks/${item.id}`;
  const waits = taskBlockers(item, tasks || []);
  const link = (url, label) => /^https?:\/\//.test(url || "") ? h("a", { href: url, target: "_blank", rel: "noopener", text: label }) : null;
  const meta = h("div", { class: "task-meta" }, rankPick(key, item), difficultyLabel(item), h("span", { text: item.lane }), item.kind ? h("span", { class: "kind", text: item.kind }) : null,
    h("span", { text: item.state }),
    item.phase ? h("span", { text: item.phase }) : null,
    item.claimed_by ? h("span", { class: "claimed", text: `claimed by ${item.claimed_by}` }) : null,
    link(item.plan_url, "plan"), link(item.issue_url, "issue"), link(item.pr_url, "pull request"));
  const body = h("div", {}, h("div", { class: "item-title" }, taskLink(item.id), h("span", { text: item.title })),
    item.description ? h("p", { class: "desc", text: item.description }) : null, meta,
    waits ? h("p", { class: "desc", text: waits }) : null);
  const row = h("div", { class: "row" }, body, itemActions(key, item));
  return h("li", { class: itemClass(item), id: `item-tasks-${item.id}` }, row, taskProof(`${key}/proof`, item), commentsView(key, item.comments));
}

function taskCounts(tasks) {
  const order = ["open", "claimed", "pr", "blocked", "done"];
  const n = {};
  for (const t of tasks) n[t.state || "open"] = (n[t.state || "open"] || 0) + 1;
  const states = [...order, ...Object.keys(n).filter((st) => !order.includes(st))];
  return headCount(states.map((st) => [n[st], st]));
}

function stateCounts(list, labels) {
  const n = {};
  for (const i of doc[list].filter((i) => !i.deleted)) {
    const st = itemState(list, i);
    n[st] = (n[st] || 0) + 1;
  }
  return headCount(Object.entries(labels).map(([st, label]) => [n[st], label]));
}

function sectionOpen(id) {
  const box = $(id).closest("details");
  if (!box || box.open) return true;
  $(id).replaceChildren();
  return false;
}

function listInto(id, items, make, idOf = (i) => `item-${id}-${i.id}`) {
  const el = $(id);
  if (!sectionOpen(id)) return;
  el.replaceChildren(...firstPage(id, items, idOf).map(make), moreButton(id, items.length, `more ${id}`, render) || "");
  if (!items.length) el.append(h("li", { class: "empty", text: "None." }));
}

export function render(focusKey) {
  const active = document.activeElement;
  const key = focusKey || (active && active.dataset ? active.dataset.focus : null);
  const caret = !focusKey && active && active.tagName === "TEXTAREA" ? [active.selectionStart, active.selectionEnd] : null;
  document.title = doc.title;
  $("title").textContent = doc.title;
  $("overview").textContent = doc.overview;
  $("overview-box").querySelector("summary").textContent = "Overview";
  $("closed-label").textContent = closedText(doc.closed_at);
  $("closed-banner").hidden = !doc.closed_at;
  $("sources-count").textContent = headCount([[doc.sources.length]]);
  listInto("sources", doc.sources.map((s, n) => ({ id: n, text: s })), (s) => h("li", { id: `item-sources-${s.id}`, text: s.text }));
  $("phases-count").textContent = stateCounts("phases", { open: "open", done: "done" });
  phaseList();
  $("tasks-count").textContent = taskCounts(doc.tasks);
  groupedWork("tasks", doc.tasks, (t) => taskRow(t, doc.tasks));
  $("questions-count").textContent = stateCounts("questions", { open: "open", done: "answered" });
  listInto("questions", [...doc.questions].sort((a, b) => (a.answers || []).length - (b.answers || []).length), (q, i) => questionRow(q, i + 1));
  $("notes-count").textContent = headCount([[doc.notes.filter((e) => !e.deleted).length]]);
  if (sectionOpen("notes")) $("notes").replaceChildren(threadView("notes", doc.notes, "thread flat"));
  $("followups-count").textContent = stateCounts("followups", { open: "open", done: "done" });
  groupedWork("followups", doc.followups, (f) => checkRow("followups", f, 0, f.text, ""));
  renderSync();
  renderPriorities();
  renderNotifications();
  renderAlerts();
  renderArtifacts();
  renderChat();
  renderChatBadge();
  renderStats();
  renderSwarm(swarm);
  renderOutline();
  markToggles();

  const again = key && document.querySelector(`[data-focus="${CSS.escape(key)}"]`);
  if (!again) return;
  again.focus();
  if (again.tagName === "TEXTAREA") {
    grow(again);
    const end = again.value.length;
    again.setSelectionRange(caret ? caret[0] : end, caret ? caret[1] : end);
  }
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
  if (!done.length) return;
  const box = h("details", { class: "fold", id: name + "-done" }, h("summary", { text: `${done.length} done` }));
  collapsible(box);
  if (done.some((i) => idOf(i) === wanted.id)) box.open = true;
  const key = `${name}-done`;
  lazy(box, () => h("ol", {}, ...firstPage(key, done, idOf).map(row), moreButton(key, done.length, `more done ${NOUNS[name]}`, render) || ""));
  $(name).append(h("li", {}, box));
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
  return `Larger of a ${calc.chain}m chain and ${calc.work}m of work over ${slots} · ${calc.remaining} tasks left · ${tiers} · ${ci}`;
}

export function renderStats() {
  const phases = inScope(doc.phases), ups = inScope(doc.followups), tasks = inScope(doc.tasks);
  const total = phases.length, done = phases.filter((p) => p.done).length, upsDone = ups.filter((f) => f.done).length;
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
  const row = (label, value, extra) => h("div", { class: "stat", title: label === "Time left" ? timeLeftInputs(calc) : undefined, style: gap && label === "Time left" ? "grid-template-columns:75px minmax(0,1fr)" : "" }, h("dt", { text: label }), h("dd", {}, value, extra));
  $("stats").replaceChildren(
    row("Started", started ? when(started) : "—"),
    row("Elapsed", elapsed === null ? "—" : span(elapsed)),
    row("Time left", leftText),
    row("Phases", `${done} / ${total} · ${pct}%`, h("div", { class: "bar" }, h("i", { style: `width:${pct}%` }))),
    row("Tasks", `${tasks.filter((t) => t.done || t.state === "done").length} / ${tasks.length}`),
    row("Open", figure(counts.open)),
    row("Claimed", figure(counts.claimed)),
    row("In PR", figure(counts.pr)),
    row("Blocked", figure(counts.blocked)),
    row("Done today", figure(swarm && swarm.done_today)),
    row("Next phase", next ? next.id : "—"),
    row("Inbox pending", figure(swarm && inboxPending(swarm))),
    row("Follow-ups", `${upsDone} / ${ups.length}`),
    ...(out ? [row("Out of scope", String(out))] : []),
    row("Agents · 2 h", String(activeAgents())),
    row("Last activity", last ? `${span(Date.now() - last)} ago` : "—"));
}

export function editTitle(on) {
  $("title-row").hidden = on;
  $("title-form").hidden = !on;
  $("title-error").textContent = "";
  if (!on) return $("title-edit").focus();
  const box = $("title-input");
  box.value = doc.title;
  box.focus();
  box.select();
}

export function saveTitle() {
  const text = $("title-input").value.trim();
  if (!text) {
    $("title-error").textContent = "The title cannot be empty";
    return $("title-input").focus();
  }
  editTitle(false);
  if (text !== doc.title) queue({ op: "title_set", id: newId("title"), text });
}
