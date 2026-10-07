import { SLUG } from "./config.js";
import { $, age, clock, h, span } from "./dom.js";
import { readSwarm, writeSwarm } from "./api.js";
import { inScope } from "./state.js";
import { doc } from "./sync.js";
import { renderStats } from "./render.js";
import { renderChatTo } from "./chat.js";
import { renderControls, renderGates, showNote } from "./controls.js";

const LIVE_LANES = [["eng", "max_eng"], ["ci", "max_ci"], ["plan", "max_plan"]];
const VERDICTS = ["false-positive", "early-real", "established", "insufficient-evidence", "resolved"];
const VERDICT_TEXT = { "false-positive": "FP", "early-real": "early real", established: "established", "insufficient-evidence": "insufficient", resolved: "resolved" };
export let swarm = null;
let swarmReadError = false;
export let pending = "";

export let capDraft = {};

export async function pollSwarm() {
  try {
    const resp = await readSwarm();
    if (!resp.ok) throw new Error(`server answered ${resp.status}`);
    const sw = await resp.json();
    if (swarmReadError && !pending) $("swarm-note").textContent = "";
    swarmReadError = false;
    renderSwarm(sw);
    renderChatTo(sw);
  } catch (error) {
    swarmReadError = true;
    renderSwarm(swarm);
    $("swarm-note").className = "sw-note bad";
    $("swarm-note").textContent = `Could not read swarm status: ${error.message}. Retrying. ${swarm ? "Last observed state shown." : "State is unavailable."}`;
    renderControls();
  }
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
      showNote("done", op);
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
    return { name: a.name, master, gates: a.gates || [], lane: master ? "—" : a.lane, profile: a.profile || "—", model: modelText(a) || "—", task: master ? "" : a.task || "",
      state: master && (a.status || "working") === "working" ? "live" : a.status || "working", age: a.started_at ? span(now - a.started_at) : "—" };
  });
}

function agentActions(a) {
  return h("div", { class: "sw-acts" },
    ...a.gates.map((g) => g.lifted ? label(`${g.gate} lifted`, "on")
      : h("button", { class: "sw-btn", type: "button", "data-lift": g.gate, "data-agent": a.name, "aria-label": `Lift the ${g.gate} gate for ${a.name}`, disabled: !!pending, text: `lift ${g.gate}` })),
    h("button", { class: "sw-btn", type: "button", "data-message": a.master ? "" : a.name, "aria-label": `Message ${a.name}`, text: "message" }),
    h("button", { class: "sw-btn danger", type: "button", "data-terminate": a.name, "aria-label": `Terminate ${a.name}`, disabled: !!pending, text: "terminate" }));
}

function taskFigures(sw, d) {
  const t = sw.tasks || {}, phases = inScope(d.phases), next = phases.find((p) => !p.done);
  const inbox = (sw.agents || []).reduce((n, a) => n + (a.inbox || []).length, 0);
  return [["open", t.open || 0], ["claimed", t.claimed || 0], ["in pr", t.pr || 0], ["blocked", t.blocked || 0], ["done today", sw.done_today || 0],
    ["phases", `${phases.filter((p) => p.done).length} / ${phases.length}`], ["next phase", next ? next.id : "—"], ["inbox pending", inbox]];
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

function quotaRows(sw, now) {
  const quota = sw.quota || {}, master = (sw.agents || []).find((a) => a.lane === "master") || {};
  return (quota.rows || []).map((r) => ({ account: r.account, harness: r.agent, five: percent(r.five_hour_left), fiveReset: resetIn(r.five_hour_resets_at, now),
    seven: percent(r.seven_day_left), sevenReset: resetIn(r.seven_day_resets_at, now), sessions: `${r.sessions}/${quota.cap ?? "—"}`,
    master: !!master.account && r.account === master.account && r.agent === (master.harness || "claude") }));
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
  if (!sw) {
    $("swarm-agents").replaceChildren(emptyRow(8, "No agents running. Start the swarm to work the open tasks."));
    return;
  }
  const now = Date.now(), open = openFindings(sw);
  $("swarm-alert").hidden = !open.length;
  $("alert-count").textContent = `${open.length} ${open.length === 1 ? "finding" : "findings"} open`;
  $("alert-kinds").textContent = [...new Set(open.map((f) => f.kind))].join(" · ");
  $("alert-live").replaceChildren(...liveCaps(sw).map((text) => h("span", { text })));
  const agents = agentRows(sw, now);
  $("agents-count").textContent = `${agents.length} live`;
  $("swarm-agents").replaceChildren(...(agents.length ? agents.map((a) => cells(idCell(a.name), a.lane, a.profile, a.model,
    a.task ? h("a", { href: `#item-tasks-${a.task}`, text: a.task }) : "—", label(a.state), a.age, agentActions(a))) : [emptyRow(8, "No agents running. Start the swarm to work the open tasks.")]));
  $("tasks-open").textContent = `${(sw.tasks || {}).open || 0} open`;
  kv("swarm-tasks", taskFigures(sw, doc));
  const quota = quotaRows(sw, now);
  $("quota-count").textContent = quotaCount(sw, quota.length, now);
  $("swarm-quota").replaceChildren(...(quota.length ? quota.map((q) => cells(idCell(q.account), q.harness, q.five, q.fiveReset, q.seven, q.sevenReset, q.sessions, q.master ? label("master", "master") : h("span")))
    : [emptyRow(8, "No quota observed yet. Run agentihooks balance.")]));
  const doctor = sw.doctor || {};
  $("doctor-state").replaceChildren(label(doctorOn(sw) ? "on" : "off", doctorOn(sw) ? "on" : "off"));
  const figures = doctorFigures(doctor, now);
  if (doctorOn(sw)) figures[0][1] = h("a", { href: "/" + doctor.slug, text: doctor.state });
  kv("swarm-doctor", figures);
  if (!$("health").contains(document.activeElement)) renderHealth(sw.findings, now);
  renderHandoffs(sw.handoffs || []);
  renderStats();
}

function renderHealth(findings, now = Date.now()) {
  const list = findings || [];
  $("health-count").textContent = `${list.length} open`;
  $("health").replaceChildren(...(list.length ? list.map((f) => healthRow(f, now)) : [emptyRow(7, "No health findings. The swarm checks every minute.")]));
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
  $("swarm-handoffs").replaceChildren(...(rows.length ? rows.map((r) => cells(idCell(seatText(r.seat)), clock(r.at), r.reason,
    r.continuity ? label(r.continuity) : "—", r.binding ? h("span", {}, label(r.binding), r.bound_at && ` ${clock(r.bound_at)}`) : "—",
    r.awaiting ? h("span", { class: "sw-ctl" }, ...["resume", "fresh"].map((choice) => h("button", { class: "sw-btn", type: "button",
      "data-agent": r.awaiting, "data-restore-choice": choice, disabled: !!pending, text: choice }))) : r.successor ? idCell(r.successor) : "—"))
    : [emptyRow(6, "No seats yet. Seats appear when agents start.")]));
}
