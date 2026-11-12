import { $, h } from "./dom.js";

const failures = new Map();
const seen = new Set();
let drawn = "";

export function noteFailure(action, text) {
  failures.delete(action);
  failures.set(action, text);
  drawFailures();
}

export function noteSuccess(action) {
  if (failures.delete(action)) drawFailures();
}

export function replayCommands(commands, actionOf, textOf) {
  for (const command of commands) {
    const key = JSON.stringify([command.id, command.command, command.argv, command.state, command.created_at]);
    if (command.state === "pending" || seen.has(key)) continue;
    seen.add(key);
    if (command.state === "failed") noteFailure(actionOf(command), textOf(command));
    else noteSuccess(actionOf(command));
  }
}

export function drawFailures(force = false) {
  const stamp = JSON.stringify([...failures]);
  if (stamp === drawn && !force) return;
  drawn = stamp;
  const note = $("swarm-note");
  note.className = "sw-note bad";
  note.title = [...failures.values()].join("\n");
  note.replaceChildren(...[...failures].map(([action, text]) => h("span", { class: "sw-fail" },
    h("span", { class: "sw-fail-text", text }),
    h("button", { class: "sw-btn sw-dismiss", type: "button", "data-dismiss": action, "aria-label": `Dismiss: ${text}` }))));
}

export function wireFailures() {
  $("swarm-note").addEventListener("click", (event) => {
    const btn = event.target.closest("button[data-dismiss]");
    if (btn && failures.delete(btn.dataset.dismiss)) drawFailures();
  });
}
