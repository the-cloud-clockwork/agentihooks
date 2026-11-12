import { $, h } from "./dom.js";

const DISMISSED = "swarm-note-dismissed";
const failures = new Map();
let seen = new Set();
let drawn = null;
let held = "";

function readDismissed() {
  try {
    return new Set(JSON.parse(localStorage.getItem(DISMISSED) || "[]"));
  } catch {
    return new Set();
  }
}

function writeDismissed(ids) {
  try {
    localStorage.setItem(DISMISSED, JSON.stringify([...ids].slice(-200)));
  } catch {}
}

export function noteFailure(action, text, id = "") {
  failures.delete(action);
  failures.set(action, { text, id });
  drawFailures();
}

export function noteSuccess(action) {
  if (failures.delete(action)) drawFailures();
}

export function replayCommands(commands, actionOf, textOf) {
  const dismissed = readDismissed(), current = new Set();
  for (const command of commands) {
    const key = JSON.stringify([command.id, command.command, command.argv, command.state, command.created_at]);
    current.add(key);
    if (command.state === "pending" || seen.has(key)) continue;
    if (command.state !== "failed") noteSuccess(actionOf(command));
    else if (!dismissed.has(command.id)) noteFailure(actionOf(command), textOf(command), command.id || "");
  }
  seen = current;
  drawFailures();
}

export function holdNote(text) {
  held = text;
  $("swarm-note").className = "sw-note bad";
  $("swarm-note").title = text;
  $("swarm-note").textContent = text;
}

export function releaseNote() {
  held = "";
  drawFailures(true);
}

export function drawFailures(force = false) {
  const stamp = JSON.stringify([...failures]);
  if (held || (stamp === drawn && !force)) return;
  drawn = stamp;
  const note = $("swarm-note");
  note.className = "sw-note bad";
  note.title = [...failures.values()].map((failure) => failure.text).join("\n");
  note.replaceChildren(...[...failures].map(([action, { text }]) => h("span", { class: "sw-fail" },
    h("span", { class: "sw-fail-text", text }),
    h("button", { class: "sw-btn sw-dismiss", type: "button", "data-dismiss": action, "aria-label": `Dismiss: ${text}` }))));
}

export function wireFailures() {
  $("swarm-note").addEventListener("click", (event) => {
    const btn = event.target.closest("button[data-dismiss]");
    const failure = btn && failures.get(btn.dataset.dismiss);
    if (!failure) return;
    if (failure.id) writeDismissed(new Set([...readDismissed(), failure.id]));
    failures.delete(btn.dataset.dismiss);
    drawFailures();
  });
}
