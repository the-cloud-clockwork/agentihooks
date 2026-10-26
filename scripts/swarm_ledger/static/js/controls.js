import { $, h } from "./dom.js";
import { readModels, writeSwarm } from "./api.js";
import { setChatTo, showChat } from "./chat.js";
import { capDraft, doctorOn, overlayChanges, pending, renderSwarm, saveRouting, swarm, swarmControl, toggleOverlay } from "./swarm.js";
import { wireHives } from "./hives.js";
import { lifecycleBody, sendLifecycle, unresolved } from "./lifecycle.js";
import { meta } from "./sync.js";

const GATE_MODES = ["enforce", "observe", "off"];
const GATE_LABELS = { enforce: "deny", observe: "log only", off: "skip" };
const STEPS = { eng: ["max_eng", 1], ci: ["max_ci", 1], plan: ["max_plan", 1], masters: ["masters_step", 1, "masters step"], compact: ["compact_limit", 50, "compact limit"], focus: ["focus_weight", 1, "focus weight"] };
const EFFORT_CAPS = { effort_min: "effort floor", effort_max: "effort ceiling" };
const MASTER_AGENTS = ["claude", "codex"];

let noteError = false;
let models = {};
let applied = null;

async function loadModels() {
  try {
    const response = await readModels();
    models = (response.ok && (await response.json()).data) || {};
  } catch {
    models = {};
  }
  renderControls();
}

function renderModels(values) {
  for (const harness of ["claude", "codex"]) {
    const key = `${harness}_model`, current = values[key] || "";
    const options = [...(models[harness] || [])];
    if (current && !options.length) options.unshift({ model: current, label: current });
    const select = $(`cap-${key}`), stamp = JSON.stringify(options);
    if (select.dataset.options !== stamp) {
      select.replaceChildren(...options.map((entry) => h("option", { value: entry.model, text: entry.label || entry.model })));
      select.dataset.options = stamp;
    }
    select.value = current;
  }
}

export function clearNoteError() {
  noteError = false;
}

function swarmControls(state) {
  return { start: state === "running", pause: state !== "running", stop: !["running", "paused", "drained"].includes(state), stop_now: state === "stopped" };
}

function affinityValue(sw) {
  const found = sw.master_affinity || {};
  return [found.desired, found.live].find((agent) => MASTER_AGENTS.includes(agent)) || MASTER_AGENTS[0];
}

function affinityNote(sw) {
  const order = sw && sw.master_affinity && sw.master_affinity.order;
  if (!order) return { text: "", cls: "" };
  if (order.state === "failed") return { text: `switch to ${order.to} failed: ${order.reason}`, cls: "bad" };
  return { text: `master hands off to ${order.to}`, cls: "" };
}

function capValues(sw) {
  return { ...sw.config, focus_weight: sw.config.focus_weight ?? 50, compact_limit: sw.compact_limit, master_agent: affinityValue(sw) };
}

function pageLimits() {
  const limits = meta.limits || {};
  return { capacity: limits.capacity || {}, efforts: limits.efforts || [] };
}

function capStep(values, lane, up, limits) {
  const efforts = limits.efforts;
  if (lane in EFFORT_CAPS) {
    return { [lane]: efforts[Math.min(efforts.length - 1, Math.max(0, efforts.indexOf(values[lane]) + (up ? 1 : -1)))] };
  }
  const [key, step] = STEPS[lane], [low, high] = limits.capacity[key] || [];
  return { [key]: Math.min(high, Math.max(low, (Number(values[key]) || 0) + (up ? step : -step))) };
}

function capChanges(values, draft, limits, catalog = {}) {
  const body = { action: "set" }, bad = [], efforts = limits.efforts;
  for (const [lane, [key, , name]] of Object.entries(STEPS)) {
    if (!(key in draft)) continue;
    const [low, high] = limits.capacity[key] || [];
    const text = String(draft[key]).trim();
    const value = /^\d+$/.test(text) ? Number(text) : NaN;
    if (!(value >= low && value <= high)) bad.push(`${name || lane} must be a whole number from ${low} to ${high}`);
    else if (value !== Number(values[key])) body[key] = value;
  }
  for (const [key, name] of Object.entries(EFFORT_CAPS)) {
    if (!(key in draft)) continue;
    const value = String(draft[key]).trim();
    if (!efforts.includes(value)) bad.push(`${name} must be one of ${efforts.join(", ")}`);
    else if (value !== values[key]) body[key] = value;
  }
  for (const harness of ["claude", "codex"]) {
    const key = `${harness}_model`;
    if (!(key in draft) || draft[key] === values[key]) continue;
    if (!(catalog[harness] || []).some((entry) => entry.model === draft[key])) bad.push(`${harness} model must be listed in the catalog`);
    else body[key] = draft[key];
  }
  if ("master_agent" in draft) {
    if (!MASTER_AGENTS.includes(draft.master_agent)) bad.push("master affinity must be Claude or Codex");
    else if (draft.master_agent !== values.master_agent) body.master_agent = draft.master_agent;
  }
  return { body, bad };
}

function capCurrent() {
  return { ...capValues(swarm), ...capDraft };
}

function capBounds(values, limits) {
  const off = {}, efforts = limits.efforts;
  for (const [lane, [key]] of Object.entries(STEPS)) {
    const [low, high] = limits.capacity[key] || [];
    off[`${lane}_down`] = !(Number(values[key]) > low);
    off[`${lane}_up`] = !(Number(values[key]) < high);
  }
  for (const key of Object.keys(EFFORT_CAPS)) {
    const at = efforts.indexOf(values[key]);
    Object.assign(off, { [`${key}_down`]: !(at > 0), [`${key}_up`]: !(at >= 0 && at < efforts.length - 1) });
  }
  return off;
}

function opNote(phase, action, error) {
  const name = { start: "Start", pause: "Pause", stop: "Stop", stop_now: "Stop now", close: "Close ledger", terminate: "Terminate", reopen: "Reopen", doctor_start: "Start doctor", doctor_stop: "Stop doctor", autonomy: "Set autonomy", gate: "Set gate mode",
    eng_down: "Lower eng cap", eng_up: "Raise eng cap", ci_down: "Lower ci cap", ci_up: "Raise ci cap", plan_down: "Lower planner cap", plan_up: "Raise planner cap", masters_down: "Lower masters step", masters_up: "Raise masters step",
    compact_down: "Lower compact limit", compact_up: "Raise compact limit",
    effort_min_down: "Lower effort floor", effort_min_up: "Raise effort floor", effort_max_down: "Lower effort ceiling", effort_max_up: "Raise effort ceiling", apply: "Apply capacity", overlays: "Apply overlays", lift: "Lift the gate", quota_refresh: "Refresh quota", routing: "Set routing", hive: "Set hive", master_move: "Move master",
    attach: "Attach", drain: "Drain", cancel: "Cancel", force_stop: "Force stop", "restore-decision": "Recovery decision" }[action] || action;
  return { cls: "bad", text: `Could not ${name.toLowerCase()}${["start", "pause", "stop"].includes(action) ? " the swarm" : ""}: ${error}. Try again or ask the master.` };
}

export function showNote(phase, action, error) {
  noteError = phase === "error";
  const text = noteError ? opNote(phase, action, error).text : "";
  $("swarm-note").className = "sw-note bad";
  $("swarm-note").textContent = text;
  $("swarm-note").title = text;
}

function renderFocus(config) {
  const found = $("capacity-caps").querySelector("[data-focus-weight]");
  if (!("focus_weight" in config)) {
    if (found) found.remove();
    return;
  }
  if (found) return;
  const step = (up) => h("button", { class: "sw-btn sw-step", type: "button", "data-swarm": `focus_${up ? "up" : "down"}`, "aria-label": `${up ? "Raise" : "Lower"} focus weight`, text: up ? "+" : "−" });
  const row = h("div", { class: "sw-cap", "data-focus-weight": "" }, h("span", { class: "sw-cap-name", text: "focus weight" }), step(false),
    h("label", { class: "sw-field" }, h("input", { class: "sw-num", id: "cap-focus", type: "text", inputmode: "numeric", autocomplete: "off", "data-cap": "focus_weight", "aria-label": "Focus weight" }), h("span", { class: "sw-unit", text: "%" })), step(true));
  $("capacity-caps").querySelector(".sw-break").after(row);
}

export function renderControls() {
  const config = swarm ? swarm.config : {};
  renderFocus(config);
  const changes = swarm ? capChanges(capValues(swarm), capDraft, pageLimits(), models) : { body: {}, bad: [] };
  const off = swarm ? { ...swarmControls(config.state), ...capBounds(capCurrent(), pageLimits()), apply: !unsent(changes.body) && !changes.bad.length } : swarmControls("");
  for (const btn of [...$("swarm-box").querySelectorAll("button[data-swarm]"), ...$("closed-banner").querySelectorAll("button[data-swarm]")]) {
    const action = btn.dataset.swarm;
    btn.hidden = (action === "doctor_start" && doctorOn(swarm)) || (action === "doctor_stop" && !doctorOn(swarm));
    btn.disabled = !swarm || !!pending || !!off[action];
    btn.classList.toggle("sending", pending === action);
    btn.setAttribute("aria-busy", pending === action ? "true" : "false");
  }
  for (const btn of $("swarm-modes").querySelectorAll("button[data-autonomy]")) {
    btn.setAttribute("aria-pressed", String(btn.dataset.autonomy === (config.autonomy || "delegate")));
    btn.disabled = !swarm || !!pending;
  }
  for (const btn of $("swarm-gates").querySelectorAll("button[data-gate]")) btn.disabled = !swarm || !!pending;
  if (swarm) renderModels(capCurrent());
  for (const input of $("capacity-box").querySelectorAll("[data-cap]")) {
    if (swarm && !(input.dataset.cap in capDraft)) input.value = capValues(swarm)[input.dataset.cap] ?? "";
    input.setAttribute("aria-invalid", String(input.dataset.cap in capDraft && !!capChanges({}, { [input.dataset.cap]: capDraft[input.dataset.cap] }, pageLimits(), models).bad.length));
    input.disabled = !swarm || pending === "apply";
  }
  const command = swarm && (swarm.commands || []).at(-1);
  if (!pending && !noteError && command) {
    const phase = { pending: "queued", accepted: "accepted", acknowledged: "acknowledged", failed: "error" }[command.state];
    const action = commandAction(command);
    showNote(phase, action, command.error);
  }
  const note = affinityNote(swarm);
  $("affinity-state").textContent = note.text;
  $("affinity-state").className = `sw-unit sw-affinity-state ${note.cls}`;
}

function commandAction(command) {
  if (command.command === "quota") return "quota_refresh";
  if (command.command === "doctor") return `doctor_${command.argv[0]}`;
  const action = command.argv[0] === "--as" ? command.argv[2] : command.argv[0];
  if (action === "set" && command.argv.some((pair) => pair.startsWith("autonomy="))) return "autonomy";
  return action === "stop" && command.argv.includes("--now") ? "stop_now" : action;
}

export function renderGates(sw) {
  const gates = Object.entries((sw && sw.gate_modes) || {});
  $("gates-label").hidden = !gates.length;
  const modes = JSON.stringify(gates);
  if ($("swarm-gates").dataset.modes === modes) return;
  $("swarm-gates").dataset.modes = modes;
  $("swarm-gates").replaceChildren(...gates.map(([name, mode]) => h("div", { class: "sw-cap sw-gate", role: "group", "aria-label": `${name} gate mode` },
    h("span", { class: "sw-cap-name", "data-gate-name": name, text: name }),
    ...GATE_MODES.map((m) => h("button", { class: "sw-btn sw-mode", type: "button", "data-gate": name, "data-gate-mode": m, "aria-pressed": String(m === mode || GATE_LABELS[m] === mode), text: GATE_LABELS[m] })))));
}

export async function refreshQuota() {
  const btn = $("quota-refresh");
  if (!swarm || btn.classList.contains("sending")) return;
  showNote("pending", "quota_refresh");
  btn.classList.add("sending");
  btn.setAttribute("aria-busy", "true");
  try {
    const resp = await writeSwarm({ action: "quota_refresh" });
    if (resp.ok) renderSwarm(await resp.json());
    else showNote("error", "quota_refresh", (await resp.text()) || `server answered ${resp.status}`);
  } catch (e) {
    showNote("error", "quota_refresh", "ledger server unreachable");
  }
  btn.classList.remove("sending");
  btn.setAttribute("aria-busy", "false");
}

export function appliedCaps(body, sw) {
  for (const key of Object.keys(capDraft)) if (!(key in body)) delete capDraft[key];
  applied = { body, draft: { ...capDraft }, keys: Object.keys(body).filter((key) => key !== "action"), id: ((sw && sw.commands) || []).at(-1)?.id };
  settleCaps(sw);
}

export function settleCaps(sw) {
  if (!applied || !sw) return;
  const row = applied.id && (sw.commands || []).find((c) => c.id === applied.id), values = capValues(sw);
  const done = row && (row.state === "failed" || (row.state === "acknowledged" && sw.last_tick >= row.acknowledged_at));
  applied.keys = applied.keys.filter((key) => String(capDraft[key]).trim() === String(applied.draft[key]).trim());
  for (const key of applied.keys) if (done || String(values[key]) === String(applied.body[key])) delete capDraft[key];
  if (!applied.keys.some((key) => key in capDraft)) applied = null;
}

function unsent(body) {
  return Object.keys(body).some((key) => key !== "action" && !(applied && applied.keys.includes(key) && String(applied.body[key]) === String(body[key])));
}

function applyCaps() {
  const { body, bad } = capChanges(capValues(swarm), capDraft, pageLimits(), models);
  if (bad.length) return showNote("error", "apply", bad.join("; "));
  if (unsent(body)) swarmControl(body, "apply");
}

export function wireSwarm() {
  loadModels();
  $("quota-refresh").addEventListener("click", refreshQuota);

  $("closed-banner").addEventListener("click", (event) => {
    const btn = event.target.closest("button[data-swarm]");
    if (btn && !btn.disabled) swarmControl({ action: "reopen" });
  });

  $("swarm-box").addEventListener("click", (event) => {
    const mode = event.target.closest("button[data-autonomy]");
    if (mode && !mode.disabled) return swarmControl({ action: "set", autonomy: mode.dataset.autonomy }, "autonomy");
    const gate = event.target.closest("button[data-gate]");
    if (gate && !gate.disabled) return swarmControl({ action: "set", gates: { [gate.dataset.gate]: gate.dataset.gateMode } }, "gate");
    const message = event.target.closest("button[data-message]");
    if (message) {
      showChat(true);
      setChatTo(message.dataset.message);
      return;
    }
    const terminate = event.target.closest("button[data-terminate]");
    if (terminate && !terminate.disabled) {
      const name = terminate.dataset.terminate;
      if (confirm(`Terminate ${name}? Work in progress stays in its worktree.`)) swarmControl({ action: "terminate", name }, "terminate");
      return;
    }
    const life = event.target.closest("button[data-lifecycle]");
    if (life && !life.disabled) {
      const agent = life.dataset.agent, control = swarm.lifecycle.controls.find((c) => c.action === life.dataset.lifecycle);
      if (pending || unresolved()[agent] || !swarm.lifecycle.agents[agent]) return;
      if (control.destructive && !confirm(`${control.label} ${agent}? ${control.consequence}`)) return;
      return sendLifecycle(lifecycleBody(swarm, agent, control.action));
    }
    const lift = event.target.closest("button[data-lift]");
    if (lift && !lift.disabled) return swarmControl({ action: "lift", agent: lift.dataset.agent, gate: lift.dataset.lift });
    const overlay = event.target.closest("button[data-overlay]");
    if (overlay && !overlay.disabled) {
      toggleOverlay(swarm, overlay.dataset.overlayRole, overlay.dataset.overlay);
      return renderSwarm(swarm);
    }
    if (event.target.closest("#overlays-apply:not(:disabled)")) return swarmControl({ action: "set", overlays: overlayChanges(swarm) }, "overlays");
    const recovery = event.target.closest("button[data-recovery-choice]");
    if (recovery && !recovery.disabled) return swarmControl({ action: "restore-decision", agent: recovery.dataset.agent, choice: recovery.dataset.recoveryChoice });
    const restore = event.target.closest("button[data-restore-choice]");
    if (restore && !restore.disabled) return swarmControl({ action: "restore-decision", agent: restore.dataset.agent, choice: restore.dataset.restoreChoice });
    const btn = event.target.closest("button[data-swarm]");
    if (!btn || btn.disabled) return;
    const action = btn.dataset.swarm;
    const step = action.match(/^(eng|ci|plan|masters|compact|effort_min|effort_max)_(up|down)$/);
    if (step) {
      const change = capStep(capCurrent(), step[1], step[2] === "up", pageLimits());
      Object.assign(capDraft, change);
      $("cap-" + step[1]).value = Object.values(change)[0];
      return renderControls();
    }
    if (action === "apply") return applyCaps();
    const consequence = {
      stop_now: "Retire every agent now. Work in progress stays in its worktree.",
      close: "Write the summary, retire every agent and close this ledger. Reopen brings it back.",
      doctor_stop: "Stop the Doctor crew and close its linked ledger."
    }[action];
    if (consequence && !confirm(consequence)) return;
    swarmControl({ action });
  });

  $("capacity-box").addEventListener("input", (event) => {
    const input = event.target.closest("[data-cap]");
    if (!input) return;
    capDraft[input.dataset.cap] = input.value;
    renderControls();
  });

  $("capacity-box").addEventListener("keydown", (event) => {
    if (event.key === "Enter" && event.target.closest("input[data-cap]") && swarm && !pending) applyCaps();
  });

  $("swarm-quota").addEventListener("change", (event) => {
    const input = event.target.closest("input[data-routing]");
    if (input) saveRouting(input);
  });

  wireHives();
}
