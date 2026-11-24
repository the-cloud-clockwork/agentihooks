import { closedMore, firstPage, limit, moreButton, wantedIn } from "./pages.js";
import { place } from "./render_cycle.js";
import { render } from "./render.js";
import { filtering, outlineAgain, outlineGroup, outlineRow, phaseRows, remoteMore } from "./outline.js";

const outlineRows = new Map();

function keptRows(s, rows) {
  const old = outlineRows.get(s.id) || new Map(), next = new Map();
  const els = rows.map((i) => {
    const key = i.group ? `${i.id}\n${i.group}` : JSON.stringify(i);
    const el = !next.has(key) && old.get(key) || outlineRow(i);
    next.set(key, el);
    return el;
  });
  outlineRows.set(s.id, next);
  return els;
}

function outlineItems(s) {
  const key = `ol-${s.id}`;
  return [...keptRows(s, phaseRows(firstPage(key, s.items, (i) => i.id))), remoteMore(s), moreButton(key, s.items.length, "more items", outlineAgain),
    s.items.length && !filtering() && closedMore(s.id.replace(/^sec-/, ""), "done", () => render())];
}

function refill(el, s) {
  el.querySelector("details").section = s;
  const link = el.querySelector("summary > a");
  if (link.title !== s.title) link.title = s.title;
  if (link.textContent !== s.title) link.textContent = s.title;
  const list = el.querySelector("ul.ol-items");
  if (list) place(list, outlineItems(s));
}

const outlineGroups = new Map();

export function keptGroup(s, sig, phases) {
  const key = `ol-${s.id}`;
  wantedIn(key, s.items, (i) => i.id);
  const inputs = [sig, limit(key), filtering(), s.id === "sec-tasks" ? phases : ""].join("\n");
  const shell = [s.complete, filtering()].join();
  const hit = outlineGroups.get(s.id);
  if (hit && hit.inputs === inputs) return hit.el;
  if (hit && hit.shell === shell) {
    refill(hit.el, s);
    outlineGroups.set(s.id, { inputs, shell, el: hit.el });
    return hit.el;
  }
  const el = outlineGroup(s);
  outlineGroups.set(s.id, { inputs, shell, el });
  return el;
}
