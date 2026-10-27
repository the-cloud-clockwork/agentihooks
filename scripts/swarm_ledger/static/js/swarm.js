import { SLUG } from "./config.js";
import { $, age, clock, h, newId, span } from "./dom.js";
import { readRouting, writeRouting, writeSwarm } from "./api.js";
import { renderStats } from "./render.js";
import { renderChatTo } from "./chat.js";
import { appliedCaps, clearNoteError, renderControls, renderGates, settleCaps, showNote } from "./controls.js";
import { firstPage, moreButton } from "./pages.js";
import { doc, queue } from "./sync.js";
import { tips } from "./state.js";
import { snowflake, targetText } from "./freezes.js";
import { agentActions, reconcile, settle, titlePause } from "./lifecycle.js";
import { quotaCount, renderHives } from "./hives.js";
import { agentCellTips, joinCell } from "./agent_cells.js";
import { capacityShown, liveAgents, renderRoles } from "./roles.js";
import { renderHandoffs, renderRecovery } from "./recovery.js";
import { renderContext } from "./context.js";

const LIVE_LANES = [["eng", "max_eng"], ["ci", "max_ci"], ["plan", "max_plan"]];
const ROLES = ["master", "engineer", "planner", "qa", "cicd", "documentation"];
export let swarm = null;
let swarmReadError = false;
export let pending = "";

export const capDraft = {};
export let overlayDraft = {};

export function receiveSwarm(sw) {
  if (!sw) return swarmLost("no swarm for this ledger");
  if (swarmReadError && !pending) $("swarm-note").textContent = "";
  swarmReadError = false;
  clearNoteError();
  const resend = reconcile(sw);
  settleCaps(sw);
  renderSwarm(sw);
  renderChatTo(sw);
  if (resend && !pending) swarmControl(resend, resend.control);
}

export function swarmLost(reason) {
  swarmReadError = true;
  renderSwarm(swarm);
  renderControls();
  $("swarm-note").className = "sw-note bad";
  $("swarm-note").textContent = `Could not read swarm status: ${reason}. Retrying. ${swarm ? "Last observed state shown." : "State is unavailable."}`;
}

export function doctorOn(sw) {
  return !!(sw && sw.doctor && sw.doctor.slug);
}

export async function swarmControl(body, op = body.action) {
  pending = op;
  showNote("pending", op);
  renderControls();
  try {
    const resp = await writeSwarm(body);
    settle(body);
    if (resp.ok) {
      swarm = await resp.json();
      if (op === "apply") appliedCaps(body, swarm);
      if (op === "overlays") overlayDraft = {};
      showNote("queued", op);
    } else {
      showNote("error", op, (await resp.text()) || `server answered ${resp.status}`);
    }
  } catch (e) {
    showNote("error", op, "ledger server unreachable");
  }
  pending = "";
  renderSwarm(swarm);
}

export function label(text, tone) {
  return h("span", { class: `lbl ${tone || String(text).replace(/\s+/g, "-")}`, text });
}

export function idCell(text) {
  return h("span", { class: "sw-id", title: text, text });
}

export function cells(...values) {
  return h("tr", {}, ...values.map((v) => (v && v.nodeType ? h("td", {}, v) : h("td", { text: v == null || v === "" ? "—" : String(v) }))));
}

export function moreRow(key, total, noun, columns, again) {
  const cell = moreButton(key, total, noun, again, "td");
  if (cell) cell.colSpan = columns;
  return cell && h("tr", { class: "page-more" }, cell);
}

export function withId(row, id) {
  row.id = id;
  return row;
}

export function emptyRow(columns, text) {
  return h("tr", { class: "sw-empty" }, h("td", { colspan: String(columns), text }));
}

function openFindings(sw) {
  return (sw.findings || []).filter((f) => !f.verdict);
}

function liveCaps(sw) {
  const live = (lane) => liveAgents(sw).filter((a) => a.lane === lane).length;
  return LIVE_LANES.map(([lane, key]) => `${lane} ${live(lane)}/${sw.config[key] ?? 0}`);
}

function modelText(a) {
  const model = a.harness === "codex" ? a.model : (a.model || "").replace(/^claude-/, "").replace(/-[\d.-]+$/, "");
  return [model, a.effort].filter(Boolean).join(" ");
}

const TERMINAL_TONES = { live: "live", cached: "stalled", unverified: "unknown" };

function terminalView(t, now) {
  if (!t) return null;
  if (t.state === "live" && t.live_at === t.checked_at && Math.abs(now - t.checked_at) <= t.fresh_ms) return { state: "live", detail: "" };
  const reason = (t.reason || (t.live_at ? "stale" : "never live")).replace(/_/g, " ");
  const detail = [t.live_at ? `live ${span(now - t.live_at)} ago` : "", reason].filter(Boolean).join(" · ");
  return { state: t.live_at ? "cached" : "unverified", detail };
}

const WAIT_TEXT = { scheduling: "scheduling wait", quota: "quota wait", input: "input wait" };
const OBSERVED_TONES = { working: "working", starting: "pending", pending: "pending", waiting_quota: "waiting", lost: "stalled" };

function executionView(c, now) {
  if (!c) return null;
  const where = [c.backend, c.generation ? `gen ${c.generation}` : "", c.pool].filter(Boolean).join(" · ");
  const seen = c.seen_at ? `seen ${span(now - c.seen_at)} ago` : "not seen";
  const shown = c.wait ? WAIT_TEXT[c.wait] : c.observed.replace(/_/g, " ");
  return { shown, tone: c.wait ? "waiting" : OBSERVED_TONES[c.observed] || "unknown", seen, where, behind: !!c.terminal_behind };
}

function remoteSwarm(sw) {
  return Object.keys(sw.terminals || {}).length > 0 || Object.values(sw.executions || {}).some((c) => c.backend !== "local");
}

function agentRows(sw, now) {
  const agents = [...(sw.agents || [])].sort((x, y) => (y.lane === "master") - (x.lane === "master"));
  return agents.map((a) => {
    const master = a.lane === "master", offer = sw.lifecycle && sw.lifecycle.agents && sw.lifecycle.agents[a.name];
    return { name: a.name, master, hiveJoin: a.hive_join || null, gates: a.gates || [], lane: master ? "—" : a.lane, profile: a.profile || "—", overlays: (a.overlays || []).join(" · ") || "—", model: modelText(a) || "—", transport: a.capabilities || null, task: master ? "" : a.task || "", account: master ? a.account || "" : "", hive: a.hive || "", remoteControl: master && a.remote_control && a.remote_control.state ? a.remote_control : null,
      state: master && (a.status || "working") === "working" ? "live" : a.status || "working", promoted: !!a.promoted, age: a.started_at ? span(now - a.started_at) : "—",
      finished: a.status === "finished", since: a.state_since || 0, terminal: terminalView((sw.terminals || {})[a.name], now),
      execution: executionView((sw.executions || {})[a.name], now), lifecycle: offer ? { offer, controls: sw.lifecycle.controls.filter((c) => c.action in offer.disabled), terminate: !!offer.terminate } : null };
  });
}

function agentRow(a, remote) {
  return agentCellTips(withId(cells(((id) => a.hive ? h("span", {}, id, h("span", { class: "sw-sub", title: `hive ${a.hive}`, text: a.hive })) : id)(a.account ? h("span", {}, idCell(a.name), h("span", { class: "sw-sub", title: `account ${a.account}`, text: a.account })) : idCell(a.name)), a.lane, a.profile, a.overlays, a.model, a.remoteControl ? h("span", { "data-tip": `Remote Control: ${a.remoteControl.state} (${a.remoteControl.detail})` }, label(`remote control ${a.remoteControl.state}`, a.remoteControl.state), h("span", { class: "sw-sub", text: a.remoteControl.detail }), ...(a.transport ? [transportCell(a.transport)] : [])) : transportCell(a.transport), a.task ? h("a", { href: `#item-tasks-${a.task}`, text: a.task }) : "—",
    h("span", {}, a.promoted ? h("span", { class: "sw-promoted", "data-tip": `${a.state} · promoted` }, label(a.state), " ", label("promoted")) : label(a.state), joinCell(a.hiveJoin)),
    ...(remote ? [executionCell(a.execution), terminalCell(a.terminal, a.execution)] : []), a.age, a.finished ? h("span") : agentActions(a, pending, swarmReadError)), `agent-${a.name}`));
}

const transportCell = (c) => c ? h("span", { class: "sw-transport", "data-tip": `${c.push_inbox ? "push" : "no push"} · ${c.native_terminal_attach ? "native terminal" : "no native terminal"}` },
  label(c.push_inbox ? "push" : "no push", c.push_inbox ? "on" : "off"), " ", label(c.native_terminal_attach ? "native terminal" : "no native terminal", c.native_terminal_attach ? "on" : "off")) : "—";

function executionCell(e) {
  return e ? h("span", {}, label(e.shown, e.tone), ` ${e.seen}`, h("span", { class: "sw-sub", text: e.where })) : "—";
}

function terminalCell(t, e) {
  if (!t) return "—";
  const detail = [t.detail, e && e.behind ? "behind heartbeat" : ""].filter(Boolean).join(" · ");
  return h("span", {}, label(t.state, TERMINAL_TONES[t.state]), detail && ` ${detail}`);
}

export function wornOverlays(sw, role) {
  return overlayDraft[role] || (sw.config.overlays || {})[role] || [];
}

export function overlayChanges(sw) {
  const changed = {};
  for (const role of ROLES) if (role in overlayDraft && overlayDraft[role].join() !== ((sw.config.overlays || {})[role] || []).join()) changed[role] = overlayDraft[role];
  return changed;
}

export function toggleOverlay(sw, role, name) {
  const worn = wornOverlays(sw, role);
  overlayDraft[role] = worn.includes(name) ? worn.filter((n) => n !== name) : worn.length >= tips.overlays ? worn : [...worn, name];
}

function overlayRow(sw, role) {
  const worn = wornOverlays(sw, role);
  const names = [...new Set([...(sw.overlays_available || []).filter((o) => (o.wears || []).includes(role)).map((o) => o.name), ...worn])];
  return h("div", { class: "sw-ovl-row", role: "group", "aria-label": `Overlays the ${role} role wears` }, h("span", { class: "sw-cap-name", text: role }),
    names.length ? h("div", { class: "sw-ctl" }, ...names.map((name) => h("button", { class: "sw-btn sw-mode", type: "button", "data-overlay-role": role, "data-overlay": name,
      "aria-pressed": String(worn.includes(name)), disabled: !!pending || (!worn.includes(name) && worn.length >= tips.overlays), text: name })))
      : h("span", { class: "sw-ovl-none", text: "no overlay in the bundle wears this role" }));
}

function renderOverlays(sw) {
  const offered = (sw && sw.overlays_available) || [];
  $("overlays-count").textContent = `${offered.length} offered` + (tips.overlays ? ` · ${tips.overlays} max` : "");
  $("swarm-overlays").replaceChildren(...(sw ? ROLES.map((role) => overlayRow(sw, role)) : []));
  $("overlays-apply").disabled = !sw || !!pending || !Object.keys(overlayChanges(sw)).length;
}

export function inboxPending(sw) {
  return (sw.agents || []).reduce((n, a) => n + (a.inbox || []).length, 0);
}

function percent(v) {
  return v == null ? "—" : `${Math.round(v)}%`;
}

function resetIn(at, now) {
  if (at == null) return "—";
  const s = Math.max(0, at - Math.floor(now / 1000)), d = Math.floor(s / 86400), hr = Math.floor((s % 86400) / 3600), m = Math.floor((s % 3600) / 60);
  const pad = (n) => String(n).padStart(2, "0");
  return s < 3600 ? `${m}m` : s < 86400 ? `${hr}h${pad(m)}m` : `${d}d${pad(hr)}h`;
}

function accountState(r) {
  return r.state ? { state: r.state.toLowerCase().replace(/_/g, " "), routing: percent(r.routing_left) } : { state: "—", routing: "—" };
}

function quotaRows(sw, now) {
  const quota = sw.quota || {}, master = (sw.agents || []).find((a) => a.lane === "master") || {};
  return (quota.rows || []).map((r) => ({ account: r.account, harness: r.agent, kind: r.kind || "subscription", ...accountState(r), five: percent(r.five_hour_left), fiveReset: resetIn(r.five_hour_resets_at, now),
    seven: percent(r.seven_day_left), sevenReset: resetIn(r.seven_day_resets_at, now), sessions: r.sessions, cap: r.cap, weight: r.weight,
    master: !!master.account && r.account === master.account && r.agent === (master.harness || "claude") }));
}

const ROUTING_READ_MS = 60000;
let routing = null, routingRead = null, routingAt = -Infinity;

function loadRouting() {
  if (routingRead || Date.now() - routingAt < ROUTING_READ_MS) return;
  routingAt = Date.now();
  routingRead = readRouting().then((resp) => (resp.ok ? resp.json() : Promise.reject(resp.status)))
    .then((reply) => { routing = reply.data; renderQuota(swarm, Date.now()); })
    .catch(() => {})
    .finally(() => { routingRead = null; });
}

function routingKey(q, field) {
  return `${q.harness}-api-${field === "cap" ? "max-sessions" : "weight"}`;
}

function routingInput(q, field) {
  const input = h("input", { class: "sw-num", type: "text", inputmode: "numeric", "data-routing": routingKey(q, field),
    "aria-label": `${q.harness} api ${field}`, placeholder: field === "cap" ? "none" : "0", disabled: !routing });
  input.value = routing ? String(routing[routingKey(q, field)] ?? "") : "";
  return field === "weight" ? h("span", { class: "sw-field" }, input, h("span", { class: "sw-unit", text: "%" })) : input;
}

function quotaRow(q) {
  const api = q.kind === "api";
  return cells(idCell(q.account), q.harness, label(q.kind), q.state === "—" ? q.state : label(q.state), q.five, q.fiveReset, q.seven, q.sevenReset, q.routing, sessionCell(q),
    api ? routingInput(q, "weight") : q.weight == null ? "—" : `${q.weight}%`, api ? routingInput(q, "cap") : q.cap,
    q.master ? label("master", "master") : h("span"));
}

export function renderQuota(sw, now, force) {
  if (!sw || (!force && $("swarm-quota").contains(document.activeElement))) return;
  loadRouting();
  const quota = quotaRows(sw, now);
  $("quota-count").textContent = quotaCount(sw, quota.length, now);
  $("swarm-quota").replaceChildren(...(quota.length ? quota.map(quotaRow) : [emptyRow(13, "No quota observed yet.")]));
}

export async function saveRouting(input) {
  const text = input.value.trim();
  const value = text === "" ? null : /^-?\d+$/.test(text) && Number.isSafeInteger(Number(text)) ? Number(text) : text;
  showNote("pending", "routing");
  try {
    const resp = await writeRouting({ [input.dataset.routing]: value });
    const reply = await resp.json().catch(() => ({}));
    if (!resp.ok) throw new Error((reply.error && reply.error.message) || `server answered ${resp.status}`);
    routing = reply.data;
    showNote("done", "routing");
  } catch (e) {
    showNote("error", "routing", e instanceof TypeError ? "ledger server unreachable" : e.message);
  }
  renderQuota(swarm, Date.now(), true);
}

function sessionCell(q) {
  return h("span", { class: "sw-sessions-value", text: String(q.sessions) });
}

function renderDoctor(sw, now) {
  const doctor = sw.doctor || {}, on = doctorOn(sw);
  $("doctor-dot").replaceChildren(label(on ? "on" : "off", on ? "on" : "off"));
  $("doctor-state").replaceChildren(on ? h("a", { href: "/" + doctor.slug, text: doctor.state }) : "not running");
  $("doctor-check").textContent = `last check ${on && doctor.last_check ? `${span(now - doctor.last_check)} ago` : "—"}`;
  $("doctor-findings").textContent = `findings ${on ? doctor.findings : "—"}`;
}

export function seatText(seat) {
  return seat.startsWith("master@") ? seat : seat.split("@")[0].replace(/-(\d+)$/, " $1");
}

function tickText(sw, now) {
  if (!sw) return "";
  const completed = sw.last_tick || 0, activity = sw.last_tick_activity || 0;
  if (activity > completed) {
    const previous = completed ? `completed ${age(now - completed)} ago` : "no completed tick";
    return `tick activity ${age(now - activity)} ago · ${previous}`;
  }
  return completed ? `tick ${age(now - completed)} ago` : "tick not recorded";
}

export function renderSwarm(sw) {
  swarm = sw;
  const state = sw ? sw.config.state : "unavailable";
  $("swarm-state").className = `sw-state ${state}`;
  $("swarm-state").textContent = state;
  $("swarm-name").textContent = (sw && (sw.config.name || sw.config.slug)) || SLUG;
  $("swarm-tick").textContent = tickText(sw, Date.now());
  $("tab-swarm").replaceChildren(...["Swarm ", h("span", { class: $("swarm-state").className, text: state }),
    sw && (openFindings(sw).length || sw.tasks.blocked) ? h("span", { class: "attention-dot", "aria-label": "Needs attention" }) : null].filter(Boolean));
  renderGates(sw);
  renderControls();
  capacityShown(sw, swarmReadError);
  renderRoles(sw);
  titlePause(sw);
  if ($("swarm").hidden) return renderStats();
  renderFreezes();
  if (!sw) {
    terminalHead(false);
    $("swarm-agents").replaceChildren(emptyRow(10, "No agents running. Start the swarm to work the open tasks."));
    renderOverlays(sw);
    return;
  }
  const now = Date.now(), open = openFindings(sw);
  $("swarm-alert").hidden = !open.length;
  $("alert-count").textContent = `${open.length} ${open.length === 1 ? "finding" : "findings"} open`;
  $("alert-kinds").textContent = [...new Set(open.map((f) => f.kind))].join(" · ");
  $("alert-live").replaceChildren(...liveCaps(sw).map((text) => h("span", { text })));
  renderAgents(sw, now);
  renderOverlays(sw);
  renderQuota(sw, now);
  renderHives();
  renderDoctor(sw, now);
  renderHandoffs(sw.handoffs || [], sw);
  renderRecovery(sw);
  renderContext(sw);
  renderStats();
}

function terminalHead(remote) {
  const head = $("agents-table").querySelector("thead tr"), found = head.querySelector("[data-terminal]");
  if (remote && !found) head.querySelector(".ag-state").after(h("th", { "data-execution": "", text: "execution" }), h("th", { "data-terminal": "", text: "terminal" }));
  if (!remote && found) head.querySelectorAll("[data-execution], [data-terminal]").forEach((th) => th.remove());
  if (remote !== !!found) document.dispatchEvent(new Event("columns"));
}

function renderAgents(sw, now = Date.now()) {
  const agents = agentRows(sw, now), live = agents.filter((a) => !a.finished), finished = agents.filter((a) => a.finished).sort((x, y) => y.since - x.since);
  $("agents-count").textContent = finished.length ? `${live.length} live · ${finished.length} finished` : `${live.length} live`;
  const remote = remoteSwarm(sw), columns = remote ? 12 : 10, row = (a) => agentRow(a, remote);
  terminalHead(remote);
  $("swarm-agents").replaceChildren(...(live.length ? live.map(row) : [emptyRow(columns, "No agents running. Start the swarm to work the open tasks.")]),
    ...firstPage("agents", finished, (a) => `agent-${a.name}`).map(row), moreRow("agents", finished.length, "more finished agents", columns, () => renderAgents(swarm)) || "");
}

function freezeRow(r) {
  const end = r.verb === "focus" ? "end focus" : "unfreeze", target = targetText(doc, r.target);
  const row = cells(snowflake(r.verb === "focus" ? "Focus" : "Frozen"), r.verb, target, r.by, r.at ? clock(r.at) : "—",
    h("button", { class: "sw-btn", type: "button", "aria-label": `${end} ${target}`, text: end,
      on: { click: () => queue({ op: "freeze_clear", id: newId("freeze"), target: r.target }) } }));
  row.cells[2].title = target;
  return row;
}

function renderFreezes() {
  const rows = doc.freezes, count = (verb) => rows.filter((r) => r.verb === verb).length;
  const plural = (n, one, many) => `${n} ${n === 1 ? one : many}`;
  $("freeze-count").textContent = `${plural(count("freeze"), "freeze", "freezes")} · ${plural(count("focus"), "focus", "focuses")}`;
  $("swarm-freezes").replaceChildren(...(rows.length ? rows.map(freezeRow) : [emptyRow(6, "Nothing is frozen.")]));
}
