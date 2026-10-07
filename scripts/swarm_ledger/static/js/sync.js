import { OFFLINE, PAGE, SYNC_COOLDOWN_MS } from "./config.js";
import { $, banner, newId, span, status } from "./dom.js";
import { readLedger, writeLedger } from "./api.js";
import { applyChecks, applyOp, itemOf, lsRead, lsWrite, setState, withDefaults } from "./state.js";
import { attaching } from "./media.js";
import { composing, editing } from "./threads.js";
import { render } from "./render.js";

const sentAt = { sync: 0, stats_sync: 0 };
let statsSent = null;

export let doc;
export let meta = {};
let rev = -1;
export let ops = [];
export let checks = {};
let inflight = false;
let seedBroken = false;

function lastSync(kind) {
  return [...(meta.events || [])].reverse().find((e) => e.kind === kind);
}

function statsCheck(events, local, refresh) {
  if (local && (!refresh || local.id !== refresh.id) && !events.some((e) => e.id === local.id))
    return local.state === "failed" ? local : { state: "pending", at: local.at };
  if (refresh) return refresh;
  const sent = [...events].reverse().find((e) => e.kind === "stats sync requested");
  return sent ? { state: "pending", at: sent.at } : null;
}

function statsCheckText(check) {
  const age = Math.max(0, Math.floor((Date.now() - (check.completed_at || check.at)) / 1000));
  return `${check.state} ${age < 60 ? `${age}s` : span(age * 1000)} ago`;
}

function cooldown(op, kind) {
  const last = lastSync(kind);
  const wait = SYNC_COOLDOWN_MS - (Date.now() - Math.max(sentAt[op], last ? last.at : 0));
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
  const check = statsCheck(meta.events || [], statsSent, meta.stats_refresh);
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

function applyServer(server) {
  if ((server._meta || {}).rev < rev) return;
  meta = server._meta || {};
  rev = meta.rev;
  doc = withDefaults(server);
  for (const op of ops) applyOp(doc, op);
  applyChecks(doc);
  banner([seedBroken && "This page's embedded copy was unreadable; showing the server's copy.", meta.seed_error]);
  render();
  lsWrite();
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
    const server = await resp.json();
    ops = ops.slice(sentOps.length);
    for (const c of sentChecks) if (checks[c.path] && checks[c.path].value === c.value) delete checks[c.path];
    applyServer(server);
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

export async function poll() {
  if (inflight) return;
  try {
    const resp = await readLedger();
    if (!resp.ok) throw new Error(String(resp.status));
    const server = await resp.json();
    if (inflight) return;
    if (staleAndIdle(server._meta.page_version)) return location.reload();
    if (server._meta.rev > rev) applyServer(server);
    if (ops.length || Object.keys(checks).length) flush(false);
    else if ($("status").classList.contains("offline") || $("status").textContent === "loading") status("saved");
  } catch (e) {
    status(OFFLINE, "offline");
  }
}

export function loadSeed() {
  let seed = null;
  try { seed = JSON.parse(document.getElementById("ledger-data").textContent); } catch (e) { seedBroken = true; }
  const saved = lsRead();
  doc = withDefaults(seed || (saved && saved.doc));
  if (saved) {
    ops = Array.isArray(saved.ops) ? saved.ops : [];
    checks = saved.checks && typeof saved.checks === "object" ? saved.checks : {};
    for (const op of ops) applyOp(doc, op);
    applyChecks(doc);
  }
  if (seedBroken) banner(["This page's embedded copy was unreadable; loading the server's copy."]);
}
