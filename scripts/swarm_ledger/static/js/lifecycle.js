import { SLUG } from "./config.js";
import { $, h, newId } from "./dom.js";
import { label, swarmControl } from "./swarm.js";

const LIFECYCLE_KEY = `lifecycle:${SLUG}`;
const RECONCILING = "waiting to reconcile an earlier control by its operation id";
const NOT_CURRENT = "the swarm view is not current; controls wait for a fresh read";

export function unresolved() {
  try {
    return JSON.parse(sessionStorage.getItem(LIFECYCLE_KEY) || "{}");
  } catch (e) {
    return {};
  }
}

function keep(map) {
  try {
    if (Object.keys(map).length) sessionStorage.setItem(LIFECYCLE_KEY, JSON.stringify(map));
    else sessionStorage.removeItem(LIFECYCLE_KEY);
  } catch (e) {}
}

export function settle(body) {
  const map = unresolved();
  if (body.action !== "lifecycle" || !map[body.agent] || map[body.agent].op !== body.op) return;
  delete map[body.agent];
  keep(map);
}

export function reconcile(sw) {
  const known = new Set(((sw.lifecycle && sw.lifecycle.operations) || []).map((o) => o.op)), map = unresolved();
  const open = Object.values(map).filter((body) => !known.has(body.op));
  if (open.length !== Object.keys(map).length) keep(Object.fromEntries(open.map((body) => [body.agent, body])));
  return open[0];
}

export function lifecycleBody(sw, name, control) {
  const seen = sw.lifecycle.agents[name];
  return { action: "lifecycle", control, agent: name, op: newId("lifecycle"), incarnation: seen.incarnation, generation: seen.generation, task_revision: seen.task_revision };
}

export function sendLifecycle(body) {
  keep({ ...unresolved(), [body.agent]: body });
  return swarmControl(body, body.control);
}

function lifecycleButton(a, c, pending, notCurrent) {
  const reason = (notCurrent ? NOT_CURRENT : "") || a.lifecycle.offer.disabled[c.action] || (unresolved()[a.name] ? RECONCILING : "");
  return h("button", { class: `sw-btn${c.destructive ? " danger" : ""}`, type: "button", "data-lifecycle": c.action, "data-agent": a.name, "aria-label": `${c.label} ${a.name}`,
    title: reason || `${c.scope}: ${c.consequence}`, disabled: !!(reason || pending), text: c.label });
}

export function agentActions(a, pending, notCurrent) {
  const life = a.lifecycle;
  return h("div", { class: "sw-acts" },
    ...a.gates.map((g) => g.lifted ? label(`${g.gate} lifted`, "on")
      : h("button", { class: "sw-btn", type: "button", "data-lift": g.gate, "data-agent": a.name, "aria-label": `Lift the ${g.gate} gate for ${a.name}`, disabled: !!pending, text: `lift ${g.gate}` })),
    h("button", { class: "sw-btn", type: "button", "data-message": a.master ? "" : a.name, "aria-label": `Message ${a.name}`, text: "message" }),
    ...(life ? life.controls.map((c) => lifecycleButton(a, c, pending, notCurrent)) : []),
    (!life || life.terminate) && h("button", { class: "sw-btn danger", type: "button", "data-terminate": a.name, "aria-label": `Terminate ${a.name}`, disabled: !!pending, text: "terminate" }));
}

export function titlePause(sw) {
  const pause = sw && sw.lifecycle && sw.lifecycle.controls.find((c) => c.action === "pause");
  if (pause) $("swarm-ctl").querySelector('[data-swarm="pause"]').title = `${pause.scope}: ${pause.consequence}`;
}
