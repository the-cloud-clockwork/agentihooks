import { h } from "./dom.js";
import { closedLeft, loadClosed } from "./sync.js";

export const PAGE_SIZE = 50;
export const FULL_PAGE = { first: PAGE_SIZE, more: PAGE_SIZE };
export const COMMENT_PAGE = { first: 8, more: 10, below: true };
const shown = {};
export const wanted = { id: null, ids: new Set() };

export function limit(key, page = FULL_PAGE) {
  return shown[key] || page.first;
}

export function include(key, index, page = FULL_PAGE) {
  if (index < page.first) return;
  shown[key] = Math.max(limit(key, page), page.first + Math.ceil((index + 1 - page.first) / page.more) * page.more);
}

export function wantedIn(key, items, idOf, page = FULL_PAGE) {
  if (wanted.id) for (const id of [wanted.id, ...wanted.ids]) include(key, items.findIndex((item) => idOf(item) === id), page);
}

export function firstPage(key, items, idOf, page = FULL_PAGE) {
  wantedIn(key, items, idOf, page);
  return items.slice(0, limit(key, page));
}

export function lastPage(key, items, idOf, page = FULL_PAGE) {
  wantedIn(key, [...items].reverse(), idOf, page);
  return items.slice(Math.max(0, items.length - limit(key, page)));
}

export function moreButton(key, total, label, again, tag = "li", page = FULL_PAGE) {
  const left = total - limit(key, page);
  if (left <= 0) return null;
  return h(tag, { class: "page-more" }, h("button", { class: "link", type: "button", text: `Show ${Math.min(left, page.more)} ${label} · ${left} left`,
    on: { click: () => { shown[key] = limit(key, page) + page.more; again(); } } }));
}

export function threadPage(key, items, { idOf, label, again, make }, page = FULL_PAGE) {
  const more = moreButton(key, items.length, label, again, "div", page);
  const rows = lastPage(key, items, idOf, page).map(make);
  return (page.below ? [...rows, more] : [more, ...rows]).filter(Boolean);
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

export function closedMore(name, label, again) {
  const left = closedLeft(name);
  if (!left) return null;
  const button = h("button", { class: "link", type: "button", "data-closed": name, text: `Show ${Math.min(left, 10)} more ${label} · ${left} left` });
  button.addEventListener("click", () => {
    button.disabled = true;
    loadClosed(name).catch(() => null).finally(() => { button.disabled = false; again(); });
  });
  return h("li", { class: "page-more" }, button);
}
