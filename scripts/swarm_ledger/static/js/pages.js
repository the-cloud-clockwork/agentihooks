import { h } from "./dom.js";

export const PAGE_SIZE = 50;
const shown = {};
export const wanted = { id: null };

export function limit(key) {
  return shown[key] || PAGE_SIZE;
}

export function include(key, index) {
  if (index >= 0) shown[key] = Math.max(limit(key), (Math.floor(index / PAGE_SIZE) + 1) * PAGE_SIZE);
}

export function wantedIn(key, items, idOf) {
  if (wanted.id) include(key, items.findIndex((item) => idOf(item) === wanted.id));
}

export function firstPage(key, items, idOf) {
  wantedIn(key, items, idOf);
  return items.slice(0, limit(key));
}

export function lastPage(key, items, idOf) {
  wantedIn(key, [...items].reverse(), idOf);
  return items.slice(Math.max(0, items.length - limit(key)));
}

export function moreButton(key, total, label, again, tag = "li") {
  const left = total - limit(key);
  if (left <= 0) return null;
  return h(tag, { class: "page-more" }, h("button", { class: "link", type: "button", text: `Show ${Math.min(left, PAGE_SIZE)} ${label} · ${left} left`,
    on: { click: () => { shown[key] = limit(key) + PAGE_SIZE; again(); } } }));
}

export function lazy(box, fill) {
  let filled = false;
  const load = () => {
    if (!box.open || filled) return;
    filled = true;
    box.append(...[fill()].flat().filter(Boolean));
  };
  box.addEventListener("toggle", load);
  load();
  return box;
}
