import { OFFLINE, PAGE } from "./config.js";
import { $, banner, newId, span, status } from "./dom.js";
import { readClosed, readMetadata, writeLedger } from "./api.js";
import { applyChecks, applyOp, hiddenDone, itemOf, lsRead, lsWrite, setState, withDefaults } from "./state.js";
import { attaching } from "./media.js";
import { composing, editing } from "./threads.js";
import { render } from "./render.js";

const sentAt = { sync: 0, stats_sync: 0 };
let statsSent = null;

export let doc;
export let meta = {};
let rev = -1;
let seenRev = -1;
export let ops = [];
export let checks = {};
let inflight = false;
let streamed = null;
let firstState;
export const loaded = new Promise((done) => { firstState = done; });

function lastSync(kind) {
  return [...(meta.events || [])].reverse().find((e) => e.kind === kind);
}

function statsCheck(events, local, refresh, ticked) {
  if (local && (!refresh || local.id !== refresh.id) && !events.some((e) => e.id === local.id))
    return local.state === "failed" ? local : { state: "pending", at: local.at };
  const at = (ticked && ticked.at) || 0;
  if (refresh) return refresh.state === "refreshed" && at > refresh.completed_at ? { ...refresh, completed_at: at, calculation: ticked.calculation } : refresh;
  const sent = [...events].reverse().find((e) => e.kind === "stats sync requested");
  if (sent) return { state: "pending", at: sent.at };
  return at ? { state: "refreshed", at, completed_at: at, calculation: ticked.calculation } : null;
}

function statsCheckText(check) {
  const age = Math.max(0, Math.floor((Date.now() - (check.completed_at || check.at)) / 1000));
  return `${check.state} ${age < 60 ? `${age}s` : span(age * 1000)} ago`;
}

function cooldown(op, kind) {
  const last = lastSync(kind);
  const wait = ((meta.limits || {}).sync_cooldown_ms || 0) - (Date.now() - Math.max(sentAt[op], last ? last.at : 0));
  return wait > 0 ? `again in ${Math.floor(wait / 60000)}m ${Math.ceil((wait % 60000) / 1000)}s` : "";
}

export function renderSync() {
  const last = lastSync("sync requested");
  const crew = meta.crew || [];
  const pending = last ? crew.filter((m) => (m.handled_rev || 0) < last.rev).length : 0;
  const wait = cooldown("sync", "sync requested");
  $("sync").disabled = !!wait;
  $("sync-badge").hidden = !pending;
  $("sync-badge").textContent = String(pending);
  const waiting = pending ? `${pending} of ${crew.length} agents have not synced yet` : "";
  $("sync").ariaLabel = [waiting, wait ? `Sync ${wait}` : "Sync the crew"].filter(Boolean).join(" · ");
  const statsWait = cooldown("stats_sync", "stats sync requested");
  const check = statsCheck(meta.events || [], statsSent, meta.stats_refresh, meta.time_left);
  const failed = check && check.state === "failed";
  $("stats-sync").disabled = !failed && !!statsWait;
  const checked = check ? `Stats check ${statsCheckText(check)}` : "";
  $("stats-sync").ariaLabel = [checked, failed ? "Retry stats refresh" : statsWait ? `Stats refresh ${statsWait}` : "Refresh stats"].filter(Boolean).join(" · ");
  $("stats-state").textContent = check ? statsCheckText(check) : "";
  $("stats-state").title = check ? check.error || (check.calculation && check.calculation.gap) || "" : "";
}

export function sendSync(op, buttonId) {
  const button = $(buttonId);
  if (button.disabled) return;
  sentAt[op] = Date.now();
  const retry = op === "stats_sync" && statsSent && statsSent.state === "failed" && ops.find((queued) => queued.id === statsSent.id);
  const id = retry ? retry.id : newId(op);
  if (op === "stats_sync") statsSent = { id, at: sentAt[op] };
  button.classList.add("sending");
  setTimeout(() => button.classList.remove("sending"), 1200);
  if (retry) flush(false);
  else queue({ op, id });
  renderSync();
}

export function queue(op) {
  ops.push(op);
  applyOp(doc, op);
  render();
  lsWrite();
  flush(false);
}

export function toggle(key, value, field) {
  const [list, id] = key.split("/");
  const item = itemOf(doc, list, id);
  const path = `${key}/${field}`;
  checks[path] = { value, base: path in checks ? checks[path].base : !!item[field] };
  setState(item, field, value);
  render();
  lsWrite();
  flush(false);
}

const older = {};
const pulled = {};

export function closedLeft(name) {
  const closed = (meta.closed || {})[name];
  return closed ? Math.max(0, closed.hidden - (pulled[name] || 0)) : 0;
}

export function closedLeftIn(name, key) {
  const closed = (meta.closed || {})[name];
  const field = { phases: "plan", tasks: "slice" }[name];
  if (!closed || !closed.groups || !field) return 0;
  const plans = new Set(doc.plans.map((p) => `plans/${p.id}`));
  const group = (i) => (name === "phases" && !plans.has(i.plan) ? "unplanned" : i[field]);
  return Math.max(0, (closed.groups[key] || 0) - (older[name] || []).filter((i) => group(i) === key).length);
}

function keepDropped(previous, next) {
  for (const name of Object.keys(pulled)) {
    const gone = hiddenDone(next, name);
    const have = new Set([...next[name], ...(older[name] || [])].map((i) => i.id));
    const dropped = previous[name].filter((i) => gone.has(i.id) && !have.has(i.id)).reverse();
    older[name] = [...dropped, ...older[name]].filter((i) => gone.has(i.id));
    pulled[name] = older[name].length;
  }
  for (const name of new Set([...touchedPaged(seenRev), ...unsettled])) refreshClosed(name).catch(() => null);
  seenRev = meta.rev;
}

function mergeOlder(d) {
  for (const [name, rows] of Object.entries(older)) {
    const known = new Set(d[name].map((i) => i.id));
    d[name] = [...rows.filter((i) => !known.has(i.id)).reverse(), ...d[name]];
  }
}

export function pagedOut(name, id) {
  return hiddenDone(doc, name).has(id) && !(doc[name] || []).some((i) => i.id === id);
}

export async function pageIn(name, id) {
  while (pagedOut(name, id) && closedLeft(name)) {
    const before = pulled[name] || 0;
    await loadClosed(name);
    if ((pulled[name] || 0) === before) return;
  }
}

const refreshing = {};
const unsettled = new Set();

async function refreshClosed(name) {
  const want = (older[name] || []).length;
  const turn = (refreshing[name] = (refreshing[name] || 0) + 1);
  unsettled.add(name);
  const rows = [];
  while (rows.length < want) {
    const closed = (meta.closed || {})[name];
    if (!closed) break;
    const resp = await readClosed(name, closed.kept + rows.length);
    if (turn !== refreshing[name] || !resp.ok) return;
    const { data } = await resp.json();
    if (turn !== refreshing[name]) return;
    if (!data.length) break;
    rows.push(...withDefaults({ [name]: data })[name]);
  }
  unsettled.delete(name);
  const stale = new Set(older[name].map((i) => i.id));
  older[name] = rows.slice(0, want);
  pulled[name] = older[name].length;
  doc[name] = doc[name].filter((i) => !stale.has(i.id));
  mergeOlder(doc);
  render();
}

function touchedPaged(since) {
  const names = new Set();
  for (const e of meta.events || []) {
    if (e.rev <= since || typeof e.target !== "string") continue;
    const [name, id] = e.target.split("/");
    if ((older[name] || []).some((i) => i.id === id)) names.add(name);
  }
  return names;
}

export async function loadClosed(name) {
  const closed = (meta.closed || {})[name];
  if (!closed) return;
  refreshing[name] = (refreshing[name] || 0) + 1;
  const resp = await readClosed(name, closed.kept + (pulled[name] || 0));
  if (resp.ok) {
    const { data } = await resp.json();
    const have = new Set((older[name] || []).map((i) => i.id));
    const fresh = withDefaults({ [name]: data })[name].filter((i) => !have.has(i.id));
    older[name] = [...(older[name] || []), ...fresh];
    pulled[name] = fresh.length ? older[name].length : closed.hidden;
    mergeOlder(doc);
  }
  if (unsettled.has(name)) refreshClosed(name).catch(() => null);
}

function applyServer(server) {
  if ((server._meta || {}).rev < rev) return;
  meta = server._meta || {};
  rev = meta.rev;
  const previous = doc;
  doc = withDefaults(server);
  keepDropped(previous, doc);
  mergeOlder(doc);
  for (const op of ops) applyOp(doc, op);
  applyChecks(doc);
  banner([meta.seed_error]);
  const appliedRev = rev;
  Promise.resolve(render(null, true)).then(() => {
    if (rev !== appliedRev) return;
    if (!performance.getEntriesByName("ledger-rendered").length) performance.mark("ledger-rendered");
    lsWrite();
    firstState();
  });
}

export async function flush(unloading) {
  if (inflight || (!ops.length && !Object.keys(checks).length)) return;
  const sentOps = ops.slice();
  const sentChecks = Object.entries(checks).map(([path, c]) => ({ path, value: c.value, base: c.base }));
  const body = JSON.stringify({ changes: sentChecks, ops: sentOps });
  inflight = true;
  status("saving", "dirty");
  try {
    const resp = await writeLedger(body, unloading);
    if (!resp.ok) throw new Error(String(resp.status));
    rev = Math.max(rev, ((await resp.json())._meta || {}).rev || rev);
    ops = ops.slice(sentOps.length);
    for (const c of sentChecks) if (checks[c.path] && checks[c.path].value === c.value) delete checks[c.path];
    lsWrite();
    status("saved " + new Date().toLocaleTimeString());
  } catch (e) {
    if (statsSent && sentOps.some((op) => op.id === statsSent.id)) {
      statsSent = { ...statsSent, state: "failed", completed_at: Date.now(), error: "Stats request failed; retry refresh" };
      renderSync();
    }
    status(OFFLINE, "offline");
  } finally {
    inflight = false;
  }
  if (streamed && streamed._meta.rev >= rev) applyServer(streamed);
  if (!unloading && (ops.length || Object.keys(checks).length) && $("status").className === "status") flush(false);
}

function staleAndIdle(version) {
  if (!PAGE || !version || version === PAGE) return false;
  const draft = Object.keys(composing).length || Object.keys(editing).length || $("chat-input").value || Object.keys(attaching).length;
  if (ops.length || Object.keys(checks).length || draft) return false;
  try {
    if (sessionStorage.getItem("ledger-reloaded") === version) return false;
    sessionStorage.setItem("ledger-reloaded", version);
  } catch (e) { /* storage blocked: reload once per load below */ }
  return true;
}

export function resume() {
  if (ops.length || Object.keys(checks).length) flush(false);
  else if ($("status").classList.contains("offline") || $("status").textContent === "loading") status("saved");
}

export function disconnected() {
  status(OFFLINE, "offline");
}

export function receiveLedger(state) {
  streamed = state;
  if (inflight) return;
  if (staleAndIdle(state._meta.page_version)) return location.reload();
  applyServer(state);
  resume();
}

export function loadSaved() {
  const saved = lsRead();
  doc = withDefaults(saved && saved.doc);
  if (saved) {
    ops = Array.isArray(saved.ops) ? saved.ops : [];
    checks = saved.checks && typeof saved.checks === "object" ? saved.checks : {};
    for (const op of ops) applyOp(doc, op);
    applyChecks(doc);
  }
}

export async function loadMetadata() {
  try {
    const resp = await readMetadata();
    if (!resp.ok || rev >= 0) return;
    const { data } = await resp.json();
    if (rev >= 0) return;
    Object.assign(doc, { title: data.title || doc.title, overview: data.overview || doc.overview });
    render();
  } catch (e) { return; }
}
