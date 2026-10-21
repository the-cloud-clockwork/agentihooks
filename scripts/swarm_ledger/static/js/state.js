import { LS_KEY } from "./config.js";
import { checks, doc, ops } from "./sync.js";
import { clearFreeze, setFreeze } from "./freezes.js";

export const app = {

};

function entries(list) {
  return (Array.isArray(list) ? list : []).filter((e) => e && typeof e === "object" && typeof e.id === "string");
}

export function withDefaults(d) {
  d = d && typeof d === "object" ? d : {};
  const list = (name, fields, threads) => (Array.isArray(d[name]) ? d[name] : []).map((i) => {
    const item = { ...i };
    for (const f of fields) if (item[f] === undefined && !["depends_on", "planning", "release"].includes(f)) item[f] = f === "done" || f === "out_of_scope" ? false : "";
    for (const t of threads) item[t] = entries(item[t]);
    return item;
  });
  return {
    title: d.title || "", overview: d.overview || "", orchestrator: d.orchestrator || "", notes: entries(d.notes).map((n) => ({ ...n, comments: entries(n.comments) })), chat: entries(d.chat),
    sources: Array.isArray(d.sources) ? d.sources : [],
    time_left_minutes: Number.isInteger(d.time_left_minutes) && d.time_left_minutes >= 0 ? d.time_left_minutes : null,
    closed_at: Number.isInteger(d.closed_at) ? d.closed_at : null,
    priorities: (Array.isArray(d.priorities) ? d.priorities : []).filter((p) => p && typeof p.id === "string" && typeof p.item === "string"),
    notifications: (Array.isArray(d.notifications) ? d.notifications : []).filter((n) => n && typeof n.id === "string" && typeof n.item === "string"),
    alerts: (Array.isArray(d.alerts) ? d.alerts : []).filter((a) => a && typeof a.id === "string" && typeof a.text === "string"),
    artifacts: (Array.isArray(d.artifacts) ? d.artifacts : []).filter((a) => a && typeof a.id === "string" && a.file && typeof a.file.id === "string"),
    artifact_trash: (Array.isArray(d.artifact_trash) ? d.artifact_trash : []).filter((a) => a && typeof a.id === "string" && a.file && typeof a.file.id === "string"),
    plans: entries(d.plans), slices: entries(d.slices), closed: (d._meta && d._meta.closed) || d.closed || {},
    freezes: entries(d.freezes).filter((r) => ["freeze", "focus"].includes(r.verb) && typeof r.target === "string"),
    marks: Object.fromEntries(["frozen", "held"].map((k) => [k, (Array.isArray((d.marks || {})[k]) ? d.marks[k] : []).filter((key) => typeof key === "string")])),
    phases: list("phases", ["title", "description", "done", "out_of_scope", "depends_on", "planning", "release"], ["comments"]),
    questions: list("questions", ["text", "out_of_scope"], ["answers", "comments"]),
    followups: list("followups", ["text", "done", "out_of_scope"], ["comments"]),
    tasks: list("tasks", ["title", "description", "phase", "lane", "state", "claimed_by", "issue_url", "pr_url", "plan_url", "done", "out_of_scope", "rank"], ["comments"]),
  };
}

export function itemOf(d, list, id) { return d[list].find((i) => i.id === id); }

function threadOf(d, path) {
  if (path === "notes" || path === "chat") return d[path];
  const [list, id, name] = path.split("/");
  const item = itemOf(d, list, id);
  return item ? item[name] : null;
}

export function applyOp(d, op) {
  if (op.op === "sync" || op.op === "stats_sync") return;
  if (op.op === "priority_clear") {
    d.priorities = op.target === "all" ? [] : d.priorities.filter((p) => p.id !== op.target);
    return;
  }
  if (op.op === "verdict") {
    const [list, id] = op.item.split("/");
    const item = itemOf(d, list, id);
    if (item && !item.comments.some((e) => e.id === op.id)) item.comments.push({ id: op.id, by: "operator", at: Date.now(), text: op.verdict, sending: true });
    d.priorities = d.priorities.filter((p) => p.item !== op.item);
    return;
  }
  if (op.op === "phase_review") {
    const phase = itemOf(d, "phases", op.item.split("/")[1]);
    if (phase) phase.review = { ...(phase.review || {}), state: op.state, note: op.note || "", escalated: false };
    return;
  }
  if (op.op === "task_rank") {
    const task = itemOf(d, "tasks", op.item.split("/")[1]);
    if (task) task.rank = op.rank === "next" ? "urgent" : op.rank;
    return;
  }
  if (op.op === "freeze_set") return setFreeze(d, op, Date.now());
  if (op.op === "freeze_clear") return clearFreeze(d, op);
  if (op.op === "title_set") {
    d.title = op.text.trim();
    return;
  }
  if (op.op === "artifact_delete" || op.op === "artifact_restore") {
    const [from, to] = op.op === "artifact_delete" ? ["artifacts", "artifact_trash"] : ["artifact_trash", "artifacts"];
    const row = d[from].find((a) => a.id === op.target);
    if (!row) return;
    const { deleted_at, ...rest } = row;
    d[from] = d[from].filter((a) => a !== row);
    d[to].push(to === "artifact_trash" ? { ...rest, deleted_at: Date.now() } : rest);
    return;
  }
  if (op.op === "alert_claim" || op.op === "alert_close") {
    const row = d.alerts.find((a) => a.id === op.target);
    if (!row || row.state === "done") return;
    if (op.op === "alert_close") Object.assign(row, { state: "done", outcome: op.outcome, closed_by: "operator" });
    else if (row.state === "open") Object.assign(row, { state: "claimed", claimed_by: "operator" });
    return;
  }
  if (op.op === "notification_clear") {
    d.notifications = op.target === "all" ? [] : d.notifications.filter((n) => n.id !== op.target);
    return;
  }
  const thread = threadOf(d, op.thread);
  if (!thread) return;
  const entry = thread.find((e) => e.id === op.id);
  if (op.op === "add" && !entry) thread.push({ id: op.id, by: "operator", at: Date.now(), text: op.text, sending: true, ...(op.thread === "notes" ? { comments: [] } : {}), ...(op.attachments ? { attachments: op.attachments } : {}) });
  else if (op.op === "edit" && entry) Object.assign(entry, { text: op.text, edited_at: Date.now(), sending: true });
  else if (op.op === "delete" && entry) Object.assign(entry, { deleted: true, text: "" });
  else if (op.op === "clear") thread.length = 0;
}

export function applyChecks(d) {
  for (const [path, c] of Object.entries(checks)) {
    const [list, id, field] = path.split("/");
    const item = itemOf(d, list, id);
    if (item) setState(item, field, c.value);
  }
}

export function setState(item, field, value) {
  item[field] = value;
  const other = field === "done" ? "out_of_scope" : "done";
  if (value && other in item) item[other] = false;
}

let cacheWorker = null;
let cacheVersion = 0;

export function lsRead() {
  try {
    const saved = JSON.parse(localStorage.getItem(LS_KEY) || "null");
    const pending = JSON.parse(localStorage.getItem(`${LS_KEY}:pending`) || "null");
    return pending ? { ...saved, ...pending } : saved;
  } catch (e) { return null; }
}

function cacheFits(value) {
  const pending = [value];
  let chars = 0;
  while (pending.length) {
    const item = pending.pop();
    if (typeof item === "string") chars += item.length;
    else if (item && typeof item === "object") pending.push(...Object.values(item));
    if (chars > 2621440) return false;
  }
  return true;
}

export function lsWrite() {
  try {
    const at = Date.now();
    localStorage.setItem(`${LS_KEY}:pending`, JSON.stringify({ ops, checks, at }));
    cacheVersion++;
    if (!cacheFits({ doc, ops, checks })) return;
    if (!cacheWorker) {
      const main = document.querySelector("script[type=module]").src;
      cacheWorker = new Worker(new URL("cache_worker.js", main));
      cacheWorker.onmessage = ({ data }) => {
        if (data.version !== cacheVersion) return;
        try { localStorage.setItem(LS_KEY, data.value); } catch (e) { /* storage blocked */ }
      };
    }
    cacheWorker.postMessage({ version: cacheVersion, value: { doc, ops, checks, at } });
  } catch (e) { /* storage blocked */ }
}

export function phaseLifecycle(phase, doc) {
  if (phase.out_of_scope) return "out_of_scope";
  if (phase.done) return "done";
  const done = new Set([...doc.phases.filter((p) => p.done).map((p) => p.id), ...(((doc.closed || {}).phases || {}).ids || [])]);
  if ((phase.depends_on || []).some((id) => !done.has(id))) return "waiting";
  if (phase.planning !== "auto") return "building";
  const plan = doc.tasks.find((t) => t.phase === phase.id && t.kind === "plan");
  const planned = plan ? plan.state === "done" : (((doc.closed || {}).tasks || {}).plan_phases || []).includes(phase.id);
  if (!plan && !planned) return "to_plan";
  if (!planned) return "planning";
  return (phase.review || {}).state === "approved" ? "building" : "in_review";
}

export function hiddenDone(doc, list) {
  return new Set(((doc.closed || {})[list] || {}).ids || []);
}

export function taskRanks() {
  return ["urgent", "high", "normal", "low"];
}

export function taskRank(item) {
  return taskRanks().includes(item.rank) ? item.rank : "normal";
}

export function inScope(list) { return list.filter((i) => !i.out_of_scope); }

export function itemState(list, i) {
  if (!["phases", "tasks", "questions", "followups"].includes(list)) return undefined;
  if (i.out_of_scope) return "out";
  const done = list === "questions" ? (i.answers || []).some((a) => !a.deleted) : !!i.done;
  return done ? "done" : "open";
}

export function taskBlockers(task, tasks) {
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
