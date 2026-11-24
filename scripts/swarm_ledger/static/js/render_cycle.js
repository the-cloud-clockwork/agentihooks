import { grow } from "./dom.js";

let renderVersion = 0;
let rendered = Promise.resolve();
let epoch = 0;
const scopes = new Map();

export function rebuildAll() {
  epoch++;
}

export function forget(scope) {
  for (const name of [...scopes.keys()]) if (name === scope || name.startsWith(`${scope}/`)) scopes.delete(name);
}

export function same(a, b) {
  return a.length === b.length && a.every((value, i) => value === b[i]);
}

export function kept(scope, inputs, make) {
  const old = scopes.get(scope) || new Map();
  const day = new Date().toDateString();
  let rows = null;
  return (item, extra) => {
    if (!rows) scopes.set(scope, (rows = new Map()));
    const key = [epoch, day, ...inputs(item, extra)];
    const hit = rows.has(item.id) ? null : old.get(item.id);
    const el = hit && same(hit.key, key) ? hit.el : make(item, extra);
    rows.set(item.id, { key, el });
    return el;
  };
}

function longestRun(order) {
  const tails = [], prev = order.map(() => -1);
  for (const [i, at] of order.entries()) {
    if (at < 0) continue;
    let lo = 0, hi = tails.length;
    while (lo < hi) {
      const mid = (lo + hi) >> 1;
      if (order[tails[mid]] < at) lo = mid + 1;
      else hi = mid;
    }
    if (lo > 0) prev[i] = tails[lo - 1];
    tails[lo] = i;
  }
  const stay = new Set();
  for (let i = tails.length ? tails[tails.length - 1] : -1; i >= 0; i = prev[i]) stay.add(i);
  return stay;
}

export function place(parent, nodes) {
  const wanted = nodes.filter(Boolean);
  const keep = new Set(wanted);
  for (const child of [...parent.childNodes]) if (!keep.has(child)) child.remove();
  const index = new Map([...parent.childNodes].map((child, i) => [child, i]));
  const stay = longestRun(wanted.map((node) => (index.has(node) ? index.get(node) : -1)));
  let next = null;
  for (let i = wanted.length - 1; i >= 0; i--) {
    if (!stay.has(i)) parent.insertBefore(wanted[i], next);
    next = wanted[i];
  }
}

export function renderCycle(focusKey, cooperative, build) {
  const version = ++renderVersion;
  const active = document.activeElement;
  let key = focusKey || (active && active.dataset ? active.dataset.focus : null);
  let selection = !focusKey && active && active.tagName === "TEXTAREA" ? active : null;
  const current = () => version === renderVersion;
  const sections = build(current);
  const paint = async () => {
    for (const section of sections) {
      if (cooperative) {
        await scheduler.postTask(() => {}, { priority: "background" });
        if (!current()) return rendered;
        const now = document.activeElement;
        if (!focusKey && now && now !== document.body) {
          key = now.dataset ? now.dataset.focus : null;
          selection = now.tagName === "TEXTAREA" ? now : null;
        }
      }
      try {
        const drawing = section();
        if (cooperative) await drawing;
      } catch (error) {
        console.error(error);
      }
    }
    if (cooperative && document.activeElement !== document.body) return;
    const again = key && document.querySelector(`[data-focus="${CSS.escape(key)}"]`);
    if (!again) return;
    again.focus();
    if (again.tagName === "TEXTAREA") {
      grow(again);
      const end = again.value.length;
      again.setSelectionRange(selection ? selection.selectionStart : end, selection ? selection.selectionEnd : end);
    }
  };
  rendered = paint();
  return rendered;
}
