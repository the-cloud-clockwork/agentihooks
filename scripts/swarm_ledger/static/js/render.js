import { $, closedText, excerpt, h, headCount, newId } from "./dom.js";
import { forget, kept, place, rebuildAll, renderCycle } from "./render_cycle.js";
import { hiddenDone, itemState, pendingOf, sourceOf, taskBlockers, taskRank, taskRanks, workOrder } from "./state.js";
import { checks, closedLeft, closedLeftIn, doc, ops, queue, renderSync, toggle } from "./sync.js";
import { openStates } from "./seed.js";
import { renderArtifacts } from "./artifacts.js";
import { commentsView, taskLink, threadView } from "./threads.js";
import { renderAlerts, renderNotifications, renderPriorities } from "./notices.js";
import { renderOutline } from "./outline.js";
import { markToggles } from "./folds.js";
import { renderChat, renderChatBadge } from "./chat.js";
import { renderSwarm, swarm } from "./swarm.js";
import { renderStats } from "./stats.js";
import { readWorkspace } from "./api.js";
import { closedMore, firstPage, moreButton, openMore } from "./pages.js";
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
const slicesByPhase = new Map();
let pendingMarks = new Map();

function notePending() {
  const marks = new Map();
  const add = (key, mark) => { if (typeof key === "string") marks.set(key, `${marks.get(key) || ""}${mark};`); };
  for (const op of ops) {
    add(op.item, op.id);
    add(op.target, op.id);
    if (typeof op.thread === "string") add(op.thread.split("/").slice(0, 2).join("/"), op.id);
  }
  for (const [path, c] of Object.entries(checks)) add(path.split("/").slice(0, 2).join("/"), `${path}=${c.value}`);
  pendingMarks = marks;
}

const tails = new Map();

function steady(key, node) {
  const old = tails.get(key);
  if (!node) tails.delete(key);
  else if (old && old.className === node.className && old.textContent === node.textContent) return old;
  else tails.set(key, node);
  return node;
}

function setText(el, text) {
  if (el.textContent !== text) el.textContent = text;
}

function rowInputs(key, item) {
  return [sourceOf(item), pendingMarks.get(key) || "", holdClass(key), !!ownFreeze(doc, key)];
}

function sliceCount(s) {
  return (sliceTaskCounts.get(`slices/${s.id}`) || 0) + closedLeftIn("tasks", `slices/${s.id}`);
}

function sliceInputs(phase) {
  return (slicesByPhase.get(`phases/${phase.id}`) || []).flatMap((s) => [s, sliceCount(s), ...rowInputs(`slices/${s.id}`, s).slice(2)]);
}

function phaseSlices(phase) {
  const rows = slicesByPhase.get(`phases/${phase.id}`) || [];
  if (!rows.length) return null;
  return h("div", { class: "phase-slices" }, h("span", { class: "phase-slices-label", text: "slices" }),
    ...rows.map((s) => h("span", { class: "slice" + holdClass(`slices/${s.id}`), id: `item-slices-${s.id}` }, holdMark(`slices/${s.id}`),
      h("span", { class: "slice-name", text: `${s.anchor} ${sliceCount(s)}` }), freezeButton(`slices/${s.id}`))));
}

function notesThread() {
  return kept("notes", () => [sourceOf(doc.notes), ...[...pendingMarks].filter(([key]) => key.startsWith("notes")).flat()],
    () => threadView("notes", doc.notes, "thread flat"))({ id: "notes" });
}

const stubs = new WeakMap();

function hiddenStubs() {
  const ids = ((doc.closed || {}).tasks || {}).ids || [];
  if (!stubs.has(ids)) stubs.set(ids, [...hiddenDone(doc, "tasks")].map((id) => ({ id, state: "done" })));
  return stubs.get(ids);
}

function keptPhases() {
  return kept("phases", (p, i) => [...rowInputs(`phases/${p.id}`, p), i, doc.phase_status[p.id], ...sliceInputs(p)], (p, i) => phaseRow(p, i + 1));
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

function planShell({ key, id, title, plan }, total) {
  const head = h("div", { class: "plan-head" }, plan ? holdMark(key) : null, h("span", { class: "plan-title", text: title }),
    h("span", { class: "plan-count", text: `${total} ${total === 1 ? "phase" : "phases"}` }), planActions(key, plan));
  return h("li", { class: "plan" + (plan && plan.out_of_scope ? " out" : "") + (plan ? holdClass(key) : ""), id }, head, h("ol", { class: "plan-phases" }));
}

function planRow(group, shells, phases) {
  const idOf = (p) => `item-phases-${p.id}`;
  const total = group.phases.length + closedLeftIn("phases", group.key);
  const shell = shells(group, total);
  place(shell.lastChild, [...firstPage(group.key, group.phases, idOf).map(phases), steady(`more ${group.key}`, moreButton(group.key, group.phases.length, "more phases", render)),
    steady(`empty ${group.key}`, group.phases.length ? null : h("li", { class: "empty", text: "None." }))]);
  return shell;
}

function phaseList(cooperative = false, current = () => true) {
  sliceTaskCounts.clear();
  for (const task of doc.tasks) if (!task.deleted && task.slice) sliceTaskCounts.set(task.slice, (sliceTaskCounts.get(task.slice) || 0) + 1);
  slicesByPhase.clear();
  for (const s of viewItems("slices", doc.slices)) slicesByPhase.set(s.phase, [...(slicesByPhase.get(s.phase) || []), s]);
  if (!doc.plans.length) return listInto("phases", viewItems("phases", doc.phases), keptPhases(), undefined, closedMore("phases", "done phases", render));
  if (!sectionOpen("phases")) return;
  const groups = planGroups();
  const target = $("phases");
  const empty = steady("empty phases", groups.length ? null : h("li", { class: "empty", text: "All phases are hidden." }));
  const phases = keptPhases();
  const shells = kept("phases/plans", (g, total) => [g.plan, g.title, total, ...rowInputs(g.key, g.plan || {}).slice(1)], planShell);
  if (!cooperative) return place(target, [...groups.map((g) => planRow(g, shells, phases)), empty, steady("closed phases", closedMore("phases", "done phases", render))]);
  const paint = async () => {
    const rows = [];
    for (const group of groups) {
      await scheduler.postTask(() => {}, { priority: "background" });
      if (!current()) return;
      rows.push(planRow(group, shells, phases));
    }
    if (current()) place(target, [...rows, empty, steady("closed phases", closedMore("phases", "done phases", render))]);
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
  const body = h("div", {}, h("div", { class: "item-title" }, taskLink(item.id, `task-id:${key}`), h("span", { text: item.title })),
    item.description ? h("p", { class: "desc", text: item.description }) : null, meta,
    waits ? h("p", { class: "desc", text: waits }) : null);
  const row = h("div", { class: "row" }, body, itemActions(key, item));
  return h("li", { class: itemClass(item, key), id: `item-tasks-${item.id}` }, holdMark(key), row, taskProof(item), commentsView(key, item.comments));
}

function taskCounts(tasks) {
  const order = ["open", "claimed", "pr", "blocked", "done"];
  const n = {};
  for (const t of tasks) n[t.state || "open"] = (n[t.state || "open"] || 0) + 1;
  for (const [st, left] of Object.entries(openStates("tasks"))) if (left) n[st] = (n[st] || 0) + left;
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
  for (const [st, left] of Object.entries(openStates(list))) if (left) n[st] = (n[st] || 0) + left;
  if (closedLeft(list)) n.done = (n.done || 0) + closedLeft(list);
  return headCount(Object.entries(labels).map(([st, label]) => [n[st], label]));
}

function sectionOpen(id) {
  const box = $(id).closest("details");
  if (!box || box.open) return true;
  $(id).replaceChildren();
  forget(id);
  return false;
}

function listInto(id, items, make, idOf = (i) => `item-${id}-${i.id}`, tail = null) {
  const el = $(id);
  if (!sectionOpen(id)) return;
  place(el, [...firstPage(id, items, idOf).map(make), steady(`more ${id}`, moreButton(id, items.length, `more ${id}`, render)), steady(`tail ${id}`, tail),
    steady(`empty ${id}`, items.length ? null : h("li", { class: "empty", text: doc[id].some((i) => !i.deleted) ? "All items are hidden." : "None." }))]);
}

export function render(focusKey, cooperative = false, live = false, first = "") {
  if (!live) rebuildAll();
  return renderCycle(focusKey, cooperative, (current) => {
    notePending();
    prepareFrozenView(doc);
    if (document.title !== doc.title) document.title = doc.title;
    setText($("title"), doc.title);
    setText($("overview"), excerpt(doc.overview, 200));
    setText($("closed-label"), closedText(doc.closed_at));
    if ($("closed-banner").hidden !== !doc.closed_at) $("closed-banner").hidden = !doc.closed_at;
    const sections = [
      ["sources", () => {
        setText($("sources-count"), headCount([[doc.sources.length]]));
        listInto("sources", doc.sources.map((s, n) => ({ id: n, text: s })), kept("sources", (s) => [s.text], (s) => h("li", { id: `item-sources-${s.id}`, text: s.text })));
      }],
      ["phases", () => {
        setText($("phases-count"), stateCounts("phases", { open: "open", done: "done" }) + viewCount("phases", doc.phases));
        return phaseList(cooperative, current);
      }],
      ["tasks", () => {
        setText($("tasks-count"), taskCounts(doc.tasks) + viewCount("tasks", doc.tasks));
        const known = [...doc.tasks, ...hiddenStubs()];
        groupedWork("tasks", viewItems("tasks", doc.tasks), kept("tasks", (t) => [...rowInputs(`tasks/${t.id}`, t), taskBlockers(t, known)], (t) => taskRow(t, known)));
      }],
      ["questions", () => {
        setText($("questions-count"), stateCounts("questions", { open: "open", done: "answered" }));
        listInto("questions", [...doc.questions].sort((a, b) => (a.answers || []).length - (b.answers || []).length),
          kept("questions", (q, i) => [...rowInputs(`questions/${q.id}`, q), i], (q, i) => questionRow(q, i + 1)), undefined, closedMore("questions", "answered questions", render));
      }],
      ["notes", () => {
        setText($("notes-count"), headCount([[doc.notes.filter((e) => !e.deleted).length]]));
        if (sectionOpen("notes")) place($("notes"), [notesThread()]);
      }],
      ["followups", () => {
        setText($("followups-count"), stateCounts("followups", { open: "open", done: "done" }) + viewCount("followups", doc.followups));
        groupedWork("followups", viewItems("followups", doc.followups), kept("followups", (f) => rowInputs(`followups/${f.id}`, f), (f) => checkRow("followups", f, 0, f.text, "")));
      }],
      ["sync", () => { renderSync(); }], ["priorities", () => { renderPriorities(); }], ["notifications", () => { renderNotifications(); }], ["alerts", () => { renderAlerts(); }],
      ["artifacts", () => { renderArtifacts(); }], ["chat", () => { renderChat(); }], ["chat badge", () => { renderChatBadge(); }], ["stats", () => { renderStats(); }],
      ["swarm", () => { renderSwarm(swarm); }], ["outline", () => { renderOutline(); }], ["toggles", () => { markToggles(); }],
    ];
    return [...sections.filter(([name]) => name === first), ...sections.filter(([name]) => name !== first)].map(([, section]) => section);
  });
}

const NOUNS = { tasks: "tasks", followups: "follow-ups" };

function doneGroup(key, text) {
  const group = document.getElementById(key);
  if (!group) return h("li", { class: "done-group", id: key }, h("div", { class: "done-head", text }), h("ol", {}));
  if (group.firstChild.textContent !== text) group.firstChild.textContent = text;
  return group;
}

function groupedWork(name, items, row) {
  if (!sectionOpen(name)) return;
  const idOf = (i) => `item-${name}-${i.id}`;
  const { live, done } = workOrder(items);
  const nodes = [...firstPage(name, live, idOf).map(row), steady(`more ${name}`, moreButton(name, live.length, `more ${NOUNS[name]}`, render) || openMore(name, `open ${NOUNS[name]}`, render))];
  nodes.push(steady(`empty ${name}`, !items.length && doc[name].some((i) => !i.deleted) ? h("li", { class: "empty", text: "All items are hidden." }) : null));
  if (done.length || closedLeft(name)) {
    const key = `${name}-done`;
    const group = doneGroup(key, `${done.length + closedLeft(name)} done`);
    place(group.lastChild, [...firstPage(key, done, idOf).map(row), steady(`more ${key}`, moreButton(key, done.length, `more done ${NOUNS[name]}`, render)),
      steady(`closed ${name}`, closedMore(name, `done ${NOUNS[name]}`, render))]);
    nodes.push(group);
  }
  place($(name), nodes);
}
