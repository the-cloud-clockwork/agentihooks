import { SLUG } from "./config.js";
import { $, age, clock, h, span } from "./dom.js";
import { readRouting, writeRouting, writeSwarm } from "./api.js";
import { renderStats } from "./render.js";
import { renderChatTo } from "./chat.js";
import { clearNoteError, renderControls, renderGates, showNote } from "./controls.js";
import { firstPage, moreButton, wanted } from "./pages.js";

const LIVE_LANES = [["eng", "max_eng"], ["ci", "max_ci"], ["plan", "max_plan"]];
const ROLES = ["master", "engineer", "planner", "qa", "cicd"];
const OVERLAY_CAP = 3;
const VERDICTS = ["false-positive", "early-real", "established", "insufficient-evidence", "resolved"];
const VERDICT_TEXT = { "false-positive": "FP", "early-real": "early real", established: "established", "insufficient-evidence": "insufficient", resolved: "resolved" };
export let swarm = null;
let swarmReadError = false;
export let pending = "";

export let capDraft = {};
export let overlayDraft = {};

export function receiveSwarm(sw) {
  if (!sw) return swarmLost("no swarm for this ledger");
  if (swarmReadError && !pending) $("swarm-note").textContent = "";
  swarmReadError = false;
  clearNoteError();
  renderSwarm(sw);
  renderChatTo(sw);
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
    if (resp.ok) {
      swarm = await resp.json();
      if (op === "apply") capDraft = {};
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

function label(text, tone) {
  return h("span", { class: `lbl ${tone || String(text).replace(/\s+/g, "-")}`, text });
}

function idCell(text) {
  return h("span", { class: "sw-id", title: text, text });
}

function cells(...values) {
  return h("tr", {}, ...values.map((v) => (v && v.nodeType ? h("td", {}, v) : h("td", { text: v == null || v === "" ? "—" : String(v) }))));
}

function moreRow(key, total, noun, columns, again) {
  const cell = moreButton(key, total, noun, again, "td");
  if (cell) cell.colSpan = columns;
  return cell && h("tr", { class: "page-more" }, cell);
}

function withId(row, id) {
  row.id = id;
  return row;
}

function emptyRow(columns, text) {
  return h("tr", { class: "sw-empty" }, h("td", { colspan: String(columns), text }));
}

function openFindings(sw) {
  return (sw.findings || []).filter((f) => !f.verdict);
}

function liveCaps(sw) {
  const live = (lane) => (sw.agents || []).filter((a) => a.lane === lane && a.state !== "finished").length;
  return LIVE_LANES.map(([lane, key]) => `${lane} ${live(lane)}/${sw.config[key] ?? 0}`);
}

function modelText(a) {
  const model = a.harness === "codex" ? a.model : (a.model || "").replace(/^claude-/, "").replace(/-[\d.-]+$/, "");
  return [model, a.effort].filter(Boolean).join(" ");
}

function agentRows(sw, now) {
  const agents = [...(sw.agents || [])].sort((x, y) => (y.lane === "master") - (x.lane === "master"));
  return agents.map((a) => {
    const master = a.lane === "master";
    return { name: a.name, master, gates: a.gates || [], lane: master ? "—" : a.lane, profile: a.profile || "—", overlays: (a.overlays || []).join(" · ") || "—", model: modelText(a) || "—", task: master ? "" : a.task || "",
      state: master && (a.status || "working") === "working" ? "live" : a.status || "working", promoted: !!a.promoted, age: a.started_at ? span(now - a.started_at) : "—",
      finished: a.status === "finished", since: a.state_since || 0 };
  });
}

function agentRow(a) {
  return withId(cells(idCell(a.name), a.lane, a.profile, a.overlays, a.model, a.task ? h("a", { href: `#item-tasks-${a.task}`, text: a.task }) : "—",
    a.promoted ? h("span", { class: "sw-promoted" }, label(a.state), label("promoted")) : label(a.state), a.age, a.finished ? h("span") : agentActions(a)), `agent-${a.name}`);
}

function agentActions(a) {
  return h("div", { class: "sw-acts" },
    ...a.gates.map((g) => g.lifted ? label(`${g.gate} lifted`, "on")
      : h("button", { class: "sw-btn", type: "button", "data-lift": g.gate, "data-agent": a.name, "aria-label": `Lift the ${g.gate} gate for ${a.name}`, disabled: !!pending, text: `lift ${g.gate}` })),
    h("button", { class: "sw-btn", type: "button", "data-message": a.master ? "" : a.name, "aria-label": `Message ${a.name}`, text: "message" }),
    h("button", { class: "sw-btn danger", type: "button", "data-terminate": a.name, "aria-label": `Terminate ${a.name}`, disabled: !!pending, text: "terminate" }));
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
  overlayDraft[role] = worn.includes(name) ? worn.filter((n) => n !== name) : worn.length < OVERLAY_CAP ? [...worn, name] : worn;
}

function overlayRow(sw, role) {
  const worn = wornOverlays(sw, role);
  const names = [...new Set([...(sw.overlays_available || []).filter((o) => (o.wears || []).includes(role)).map((o) => o.name), ...worn])];
  return h("div", { class: "sw-ovl-row", role: "group", "aria-label": `Overlays the ${role} role wears` }, h("span", { class: "sw-cap-name", text: role }),
    names.length ? h("div", { class: "sw-ctl" }, ...names.map((name) => h("button", { class: "sw-btn sw-mode", type: "button", "data-overlay-role": role, "data-overlay": name,
      "aria-pressed": String(worn.includes(name)), disabled: !!pending || (!worn.includes(name) && worn.length >= OVERLAY_CAP), text: name })))
      : h("span", { class: "sw-ovl-none", text: "no overlay in the bundle wears this role" }));
}

function renderOverlays(sw) {
  const offered = (sw && sw.overlays_available) || [];
  $("overlays-count").textContent = `${offered.length} offered · ${OVERLAY_CAP} max`;
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

function accountState(sw, r) {
  const seen = ((sw.quota_capacity || {}).accounts || []).find((a) => a.name === r.account && a.harness === r.agent);
  return seen ? { state: seen.state.toLowerCase(), routing: percent(seen.routing) } : { state: "—", routing: "—" };
}

function quotaRows(sw, now) {
  const quota = sw.quota || {}, master = (sw.agents || []).find((a) => a.lane === "master") || {};
  return (quota.rows || []).map((r) => ({ account: r.account, harness: r.agent, kind: r.kind || "subscription", ...accountState(sw, r), five: percent(r.five_hour_left), fiveReset: resetIn(r.five_hour_resets_at, now),
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

function renderQuota(sw, now, force) {
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

function capacityLine(cap, now) {
  if (!cap || !cap.effective) return null;
  return { lanes: cap.lanes.map((lane) => `${lane} ${cap.effective[lane]} of ${cap.configured[lane]}`).join(" · "),
    changed: `changed ${span(now - cap.at)} ago`, reason: `because ${cap.reason}` };
}

function sessionCell(q) {
  return h("span", { class: "sw-sessions-value", text: String(q.sessions) });
}

function quotaCount(sw, count, now) {
  const at = (sw.quota || {}).probed_at, accounts = `${count} ${count === 1 ? "account" : "accounts"}`;
  return at ? `${accounts} · probed ${age(now - at * 1000)} ago` : accounts;
}

function doctorFigures(doctor, now) {
  const on = !!(doctor && doctor.slug);
  return [["state", on ? doctor.state : "not running"], ["last check", on && doctor.last_check ? `${span(now - doctor.last_check)} ago` : "—"], ["findings", on ? String(doctor.findings) : "—"]];
}

function seatText(seat) {
  return seat.startsWith("master@") ? seat : seat.split("@")[0].replace(/-(\d+)$/, " $1");
}

function kv(id, pairs) {
  $(id).replaceChildren(...pairs.map(([key, value]) => h("div", { class: "kv" }, h("dt", { text: key }), h("dd", {}, value && value.nodeType ? value : String(value)))));
}

export function renderSwarm(sw) {
  swarm = sw;
  const state = sw ? sw.config.state : "unavailable";
  $("swarm-state").className = `sw-state ${state}`;
  $("swarm-state").textContent = state;
  $("swarm-name").textContent = (sw && (sw.config.name || sw.config.slug)) || SLUG;
  $("swarm-tick").textContent = !sw ? "" : sw.last_tick ? `tick ${age(Date.now() - sw.last_tick)} ago` : "tick not recorded";
  $("tab-swarm").replaceChildren(...["Swarm ", h("span", { class: $("swarm-state").className, text: state }),
    sw && (openFindings(sw).length || sw.tasks.blocked) ? h("span", { class: "attention-dot", "aria-label": "Needs attention" }) : null].filter(Boolean));
  renderGates(sw);
  renderControls();
  if ($("swarm").hidden) return renderStats();
  if (!sw) {
    $("swarm-agents").replaceChildren(emptyRow(9, "No agents running. Start the swarm to work the open tasks."));
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
  const capacity = capacityLine(sw.quota_capacity, now);
  $("quota-capacity").replaceChildren(...(capacity ? [h("span", { class: "sw-caplanes", text: capacity.lanes }), h("span", { text: capacity.changed }), h("span", { text: capacity.reason })] : []));
  const doctor = sw.doctor || {};
  $("doctor-state").replaceChildren(label(doctorOn(sw) ? "on" : "off", doctorOn(sw) ? "on" : "off"));
  const figures = doctorFigures(doctor, now);
  if (doctorOn(sw)) figures[0][1] = h("a", { href: "/" + doctor.slug, text: doctor.state });
  kv("swarm-doctor", figures);
  if (wanted.id || !$("health").contains(document.activeElement)) renderHealth(sw.findings, now);
  renderHandoffs(sw.handoffs || []);
  renderStats();
}

function renderAgents(sw, now = Date.now()) {
  const agents = agentRows(sw, now), live = agents.filter((a) => !a.finished), finished = agents.filter((a) => a.finished).sort((x, y) => y.since - x.since);
  $("agents-count").textContent = finished.length ? `${live.length} live · ${finished.length} finished` : `${live.length} live`;
  $("swarm-agents").replaceChildren(...(live.length ? live.map(agentRow) : [emptyRow(9, "No agents running. Start the swarm to work the open tasks.")]),
    ...firstPage("agents", finished, (a) => `agent-${a.name}`).map(agentRow), moreRow("agents", finished.length, "more finished agents", 9, () => renderAgents(swarm)) || "");
}

function renderHealth(findings, now = Date.now()) {
  const list = findings || [];
  $("health-count").textContent = `${list.length} open`;
  $("health").replaceChildren(...(list.length ? firstPage("health", list, (f) => `finding-${f.id}`).map((f) => withId(healthRow(f, now), `finding-${f.id}`))
    : [emptyRow(7, "No health findings. The swarm checks every minute.")]), moreRow("health", list.length, "more findings", 7, () => renderHealth(swarm && swarm.findings)) || "");
}

function healthRow(f, now) {
  const earlier = f.verdict || {};
  const pick = h("select", { class: "hl-pick", "aria-label": `Verdict on ${f.kind} ${f.subject}`, "data-verdict": f.id },
    h("option", { value: "", text: "?" }), ...VERDICTS.map((v) => h("option", { value: v, text: VERDICT_TEXT[v] })));
  pick.value = earlier.value || "";
  const note = h("input", { class: "hl-note", type: "text", maxlength: "500", placeholder: "—", "aria-label": `Verdict note on ${f.kind} ${f.subject}` });
  note.value = earlier.note || "";
  return cells(f.kind, idCell(f.subject), (f.evidence || []).join(" · ") || f.summary, f.threshold, f.seen_at ? `${span(now - f.seen_at)} ago` : "—", pick, note);
}

function renderHandoffs(rows) {
  $("handoff-count").textContent = `${rows.length} ${rows.length === 1 ? "seat" : "seats"}`;
  $("swarm-handoffs").replaceChildren(...(rows.length ? firstPage("seats", rows, (r) => `seat-${r.seat}`).map((r) => withId(cells(idCell(seatText(r.seat)), clock(r.at), r.reason,
    r.continuity ? label(r.continuity) : "—", r.binding ? h("span", {}, label(r.binding), r.bound_at && ` ${clock(r.bound_at)}`) : "—",
    r.awaiting ? h("span", { class: "sw-ctl" }, ...["resume", "fresh"].map((choice) => h("button", { class: "sw-btn", type: "button",
      "data-agent": r.awaiting, "data-restore-choice": choice, disabled: !!pending, text: choice }))) : r.successor ? idCell(r.successor) : "—"), `seat-${r.seat}`))
    : [emptyRow(6, "No seats yet. Seats appear when agents start.")]), moreRow("seats", rows.length, "more seats", 6, () => renderHandoffs(rows)) || "");
}
