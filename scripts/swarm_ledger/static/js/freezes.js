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

const indexes = new WeakMap();

export function byId(list, id) {
  let index = indexes.get(list);
  let at = index && index.get(id);
  if (at === undefined || list[at]?.id !== id) {
    index = new Map();
    for (const [i, item] of list.entries()) if (!index.has(item.id)) index.set(item.id, i);
    indexes.set(list, index);
    at = index.get(id);
  }
  return at === undefined ? undefined : list[at];
}

export function ownFreeze(d, key) {
  return d.freezes.find((r) => r.verb === "freeze" && r.target === key);
}

const markSets = new WeakMap();

function marked(list, key) {
  let set = markSets.get(list);
  if (!set || set.size !== list.length) markSets.set(list, (set = new Set(list)));
  return set.has(key);
}

export function frozen(d, key) {
  return marked(d.marks.frozen, key);
}

export function held(d, key) {
  return marked(d.marks.held, key);
}

export function snowflakeState(d, key) {
  return frozen(d, key) ? "frozen" : held(d, key) ? "held" : "";
}

export function setFreeze(d, op, at) {
  if (d.freezes.some((r) => r.verb === op.verb && r.target === op.target)) return;
  d.freezes.push({ id: op.id, verb: op.verb, target: op.target, by: "operator", at, reason: op.reason || "" });
  if (op.verb === "freeze" && !d.marks.frozen.includes(op.target)) d.marks.frozen.push(op.target);
}

export function clearFreeze(d, op) {
  d.freezes = d.freezes.filter((r) => r.target !== op.target);
  d.marks.frozen = d.marks.frozen.filter((key) => key !== op.target);
}

export function targetText(d, target) {
  if (isSelector(target)) return target.replace(":", " ");
  const [list, id] = target.split("/");
  const item = byId(d[list] || [], id) || {};
  const names = { plans: ["plan", item.title], phases: ["phase", item.title], slices: ["slice", item.anchor], tasks: ["task", item.title] };
  const [noun, name] = names[list] || [list, id];
  return `${noun} ${name || id}`;
}
