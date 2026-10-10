import { h } from "./dom.js";

const FLAKE = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 2v20M3.3 7l17.4 10M3.3 17l17.4-10"/><path d="M9 4l3 2 3-2M9 20l3-2 3 2"/></svg>';

export function snowflake(title) {
  const el = h("span", { class: "snowflake", role: "img", title, "aria-label": title });
  el.innerHTML = FLAKE;
  return el;
}

export function isSelector(target) {
  return /^(lane|kind):/.test(target);
}

function byId(list, id) {
  return list.find((i) => i.id === id);
}

export function ancestors(d, key) {
  const [list, id] = key.split("/");
  if (list === "phases") {
    const phase = byId(d.phases, id);
    return [key, ...(phase && phase.plan ? [phase.plan] : [])];
  }
  if (list === "slices") {
    const row = byId(d.slices, id);
    return [key, ...(row && row.phase ? ancestors(d, row.phase) : [])];
  }
  if (list === "tasks") {
    const task = byId(d.tasks, id);
    if (!task) return [key];
    const up = [...(task.slice ? ancestors(d, task.slice) : []), ...(task.phase ? ancestors(d, `phases/${task.phase}`) : [])];
    return [key, ...new Set(up)];
  }
  return [key];
}

function matches(d, key, selector) {
  const [list, id] = key.split("/");
  const task = list === "tasks" && byId(d.tasks, id);
  if (!task) return false;
  const [field, value] = selector.split(":");
  return task[field] === value;
}

function covers(d, key, target) {
  return isSelector(target) ? matches(d, key, target) : ancestors(d, key).includes(target);
}

export function ownFreeze(d, key) {
  return d.freezes.find((r) => r.verb === "freeze" && r.target === key);
}

export function frozen(d, key) {
  return d.freezes.some((r) => r.verb === "freeze" && covers(d, key, r.target));
}

export function held(d, key) {
  const focus = d.freezes.filter((r) => r.verb === "focus");
  if (!focus.length || frozen(d, key)) return false;
  const [list, id] = key.split("/");
  if (list === "tasks" && (byId(d.tasks, id) || {}).rank === "urgent") return false;
  return !focus.some((r) => (isSelector(r.target) ? list !== "tasks" || matches(d, key, r.target)
    : covers(d, key, r.target) || ancestors(d, r.target).includes(key)));
}

export function setFreeze(d, op, at) {
  if (d.freezes.some((r) => r.verb === op.verb && r.target === op.target)) return;
  d.freezes.push({ id: op.id, verb: op.verb, target: op.target, by: "operator", at, reason: op.reason || "" });
}

export function clearFreeze(d, op) {
  d.freezes = d.freezes.filter((r) => r.target !== op.target && (isSelector(op.target) || isSelector(r.target) || !ancestors(d, r.target).includes(op.target)));
}

export function targetText(d, target) {
  if (isSelector(target)) return target.replace(":", " ");
  const [list, id] = target.split("/");
  const item = byId(d[list] || [], id) || {};
  const names = { plans: ["plan", item.title], phases: ["phase", item.title], slices: ["slice", item.anchor], tasks: ["task", item.title] };
  const [noun, name] = names[list] || [list, id];
  return `${noun} ${name || id}`;
}
