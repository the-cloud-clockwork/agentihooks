import { $, h } from "./dom.js";
import { writeSwarm } from "./api.js";
import { setChatTo, showChat } from "./chat.js";
import { capDraft, doctorOn, overlayChanges, pending, renderSwarm, swarm, swarmControl, toggleOverlay } from "./swarm.js";

const GATE_MODES = ["enforce", "observe", "off"];
const GATE_LABELS = { enforce: "deny", observe: "log only", off: "skip" };
const STEPS = { eng: ["max_eng", 1, 0, 50], ci: ["max_ci", 1, 0, 50], plan: ["max_plan", 1, 0, 50], compact: ["compact_limit", 50, 100, 1000, "compact limit"] };
const EFFORTS = ["low", "medium", "high", "max"];
const EFFORT_CAPS = { effort_min: "effort floor", effort_max: "effort ceiling" };
const MASTER_AGENTS = ["claude", "codex"];

let noteTimer = 0;
let noteError = false;

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
  return { ...sw.config, compact_limit: sw.compact_limit, master_agent: affinityValue(sw) };
}

function capStep(values, lane, up) {
  if (lane in EFFORT_CAPS) {
    const [low, high] = lane === "effort_min" ? [0, EFFORTS.indexOf(values.effort_max)] : [EFFORTS.indexOf(values.effort_min), EFFORTS.length - 1];
    return { [lane]: EFFORTS[Math.min(high, Math.max(low, EFFORTS.indexOf(values[lane]) + (up ? 1 : -1)))] };
  }
  const [key, step, low, high] = STEPS[lane];
  return { [key]: Math.min(high, Math.max(low, (Number(values[key]) || 0) + (up ? step : -step))) };
}

function capChanges(values, draft) {
  const body = { action: "set" }, bad = [];
  for (const [lane, [key, , low, high, name]] of Object.entries(STEPS)) {
    if (!(key in draft)) continue;
    const text = String(draft[key]).trim();
    const value = /^\d+$/.test(text) ? Number(text) : NaN;
    if (!(value >= low && value <= high)) bad.push(`${name || lane} must be a whole number from ${low} to ${high}`);
    else if (value !== Number(values[key])) body[key] = value;
  }
  const range = { ...values, ...draft };
  for (const [key, name] of Object.entries(EFFORT_CAPS)) {
    if (!(key in draft)) continue;
    const value = String(draft[key]).trim();
    if (!EFFORTS.includes(value)) bad.push(`${name} must be one of ${EFFORTS.join(", ")}`);
    else if (value !== values[key]) body[key] = value;
  }
  if ("master_agent" in draft) {
    if (!MASTER_AGENTS.includes(draft.master_agent)) bad.push("master affinity must be Claude or Codex");
    else if (draft.master_agent !== values.master_agent) body.master_agent = draft.master_agent;
  }
  const [floor, ceiling] = [range.effort_min, range.effort_max].map((v) => EFFORTS.indexOf(String(v).trim()));
  if (floor >= 0 && ceiling >= 0 && floor > ceiling) bad.push("effort floor must not be above the effort ceiling");
  return { body, bad };
}

function capCurrent() {
  return { ...capValues(swarm), ...capDraft };
}

function capBounds(values) {
  const off = {};
  for (const [lane, [key, , low, high]] of Object.entries(STEPS)) {
    off[`${lane}_down`] = !(Number(values[key]) > low);
    off[`${lane}_up`] = !(Number(values[key]) < high);
  }
  const low = EFFORTS.indexOf(values.effort_min), high = EFFORTS.indexOf(values.effort_max);
  Object.assign(off, { effort_min_down: !(low > 0), effort_min_up: !(low >= 0 && low < high), effort_max_down: !(high > low && low >= 0), effort_max_up: !(high >= 0 && high < EFFORTS.length - 1) });
  return off;
}

function opNote(phase, action, error) {
  const name = { start: "Start", pause: "Pause", stop: "Stop", stop_now: "Stop now", close: "Close ledger", terminate: "Terminate", reopen: "Reopen", doctor_start: "Start doctor", doctor_stop: "Stop doctor", autonomy: "Set autonomy", gate: "Set gate mode",
    eng_down: "Lower eng cap", eng_up: "Raise eng cap", ci_down: "Lower ci cap", ci_up: "Raise ci cap", plan_down: "Lower planner cap", plan_up: "Raise planner cap",
    compact_down: "Lower compact limit", compact_up: "Raise compact limit",
    effort_min_down: "Lower effort floor", effort_min_up: "Raise effort floor", effort_max_down: "Lower effort ceiling", effort_max_up: "Raise effort ceiling", apply: "Apply capacity", overlays: "Apply overlays", verdict: "Verdict", lift: "Lift the gate", quota_refresh: "Refresh quota" }[action] || action;
  if (phase === "pending") return { cls: "pending", text: `${name}: sending` };
  if (phase === "queued") return { cls: "pending", text: `${name}: pending, waiting for the hive tick` };
  if (phase === "accepted") return { cls: "pending", text: `${name}: accepted by the hive tick` };
  if (phase === "done" || phase === "acknowledged") return { cls: "ok", text: `${name}: acknowledged` };
  return { cls: "bad", text: `Could not ${name.toLowerCase()}${["start", "pause", "stop"].includes(action) ? " the swarm" : ""}: ${error}. Try again or ask the master.` };
}

export function showNote(phase, action, error) {
  const note = opNote(phase, action, error);
  noteError = phase === "error";
  clearTimeout(noteTimer);
  $("swarm-note").className = `sw-note ${note.cls}`;
  $("swarm-note").textContent = note.text;
  $("swarm-note").title = note.text;
  if (phase === "done") noteTimer = setTimeout(() => { $("swarm-note").textContent = ""; }, 4000);
}

export function renderControls() {
  const config = swarm ? swarm.config : {};
  const changes = swarm ? capChanges(capValues(swarm), capDraft) : { body: {}, bad: [] };
  const off = swarm ? { ...swarmControls(config.state), ...capBounds(capCurrent()), apply: Object.keys(changes.body).length < 2 && !changes.bad.length } : swarmControls("");
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
  for (const input of $("capacity-box").querySelectorAll("[data-cap]")) {
    if (swarm && !(input.dataset.cap in capDraft)) input.value = capValues(swarm)[input.dataset.cap] ?? "";
    input.setAttribute("aria-invalid", String(input.dataset.cap in capDraft && !!capChanges({}, { [input.dataset.cap]: capDraft[input.dataset.cap] }).bad.length));
    input.disabled = !swarm || pending === "apply";
  }
  renderCommands((swarm && swarm.commands) || []);
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
  return action === "stop" && command.argv.includes("--now") ? "stop_now" : action;
}

function renderCommands(commands) {
  $("command-log").hidden = !commands.length;
  const counts = ["pending", "accepted", "failed"].map((state) => [state, commands.filter((command) => command.state === state).length]);
  $("command-count").textContent = counts.filter(([, count]) => count).map(([state, count]) => `${count} ${state}`).join(" · ");
  $("command-rows").replaceChildren(...[...commands].reverse().map((command) => h("p", { class: "sw-command" },
    h("span", { text: opNote("pending", commandAction(command)).text.split(":")[0] }),
    h("span", { text: command.state }),
    command.error && h("span", { text: command.error }))));
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

function applyCaps() {
  const { body, bad } = capChanges(capValues(swarm), capDraft);
  if (bad.length) return showNote("error", "apply", bad.join("; "));
  if (Object.keys(body).length > 1) swarmControl(body, "apply");
}

export function wireSwarm() {
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
    const lift = event.target.closest("button[data-lift]");
    if (lift && !lift.disabled) return swarmControl({ action: "lift", agent: lift.dataset.agent, gate: lift.dataset.lift });
    const overlay = event.target.closest("button[data-overlay]");
    if (overlay && !overlay.disabled) {
      toggleOverlay(swarm, overlay.dataset.overlayRole, overlay.dataset.overlay);
      return renderSwarm(swarm);
    }
    if (event.target.closest("#overlays-apply:not(:disabled)")) return swarmControl({ action: "set", overlays: overlayChanges(swarm) }, "overlays");
    const restore = event.target.closest("button[data-restore-choice]");
    if (restore && !restore.disabled) return swarmControl({ action: "restore-decision", agent: restore.dataset.agent, choice: restore.dataset.restoreChoice });
    const btn = event.target.closest("button[data-swarm]");
    if (!btn || btn.disabled) return;
    const action = btn.dataset.swarm;
    const step = action.match(/^(eng|ci|plan|compact|effort_min|effort_max)_(up|down)$/);
    if (step) {
      const change = capStep(capCurrent(), step[1], step[2] === "up");
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

  $("health").addEventListener("change", (event) => {
    const pick = event.target.closest("select.hl-pick");
    if (!pick || !pick.value) return;
    const row = pick.closest("tr");
    swarmControl({ action: "verdict", id: pick.dataset.verdict, verdict: pick.value, note: row.querySelector(".hl-note").value.trim() });
  });
}
