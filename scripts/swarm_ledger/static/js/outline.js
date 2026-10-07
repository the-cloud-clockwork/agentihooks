import { $, h } from "./dom.js";
import { itemState } from "./state.js";
import { doc } from "./sync.js";
import { noticeTarget } from "./notices.js";
import { collapsible, groupOpen, markToggles, rememberGroup } from "./folds.js";
import { selectTab } from "./layout.js";
import { render } from "./render.js";
import { firstPage, lazy, moreButton, wanted } from "./pages.js";

let outlinePick = null;

let outlineFrame = 0;

export function revealTarget(id) {
  let el = document.getElementById(id);
  if (!el && id.startsWith("item-")) {
    const section = document.querySelector(`section[data-outline="${id.split("-")[1]}"] > details.fold`);
    if (section) section.open = true;
    wanted.id = id;
    try { render(); } finally { wanted.id = null; }
    el = document.getElementById(id);
  }
  if (el) reveal(el);
  return el;
}

export function jumpTo(ev, item) {
  const el = revealTarget(`item-${item.replace("/", "-")}`);
  if (!el) return;
  ev.preventDefault();
  el.scrollIntoView({ behavior: "smooth", block: "center" });
  el.classList.remove("flash");
  void el.offsetWidth;
  el.classList.add("flash");
}

function outlineOf(doc, heads) {
  const entry = (list) => (i, n) => typeof i === "string" ? { id: `item-${list}-${n}`, title: i }
    : { id: `item-${list}-${i.id}`, title: list === "tasks" ? `${i.id} ${i.title}` : i.title || i.text || i.item || i.id, state: itemState(list, i) };
  return heads.map((head) => {
    const list = (head.list ? doc[head.list] || [] : []).filter((i) => !i.deleted);
    const grouped = ["tasks", "followups"].includes(head.list);
    const counts = grouped ? [...new Set(list.map((i) => i.state || (i.done ? "done" : "open")))]
      .map((state) => `${list.filter((i) => (i.state || (i.done ? "done" : "open")) === state).length} ${state}`).join(" · ") : "";
    return { id: head.id, title: head.title + (counts ? ` · ${counts}` : ""),
      items: head.list === "followups" ? [] : list.map(entry(head.list)) };
  });
}

function outlineLink(e) {
  return h("a", { href: `#${e.id}`, "data-target": e.id, title: e.title, text: e.title, class: e.state && `st-${e.state}` });
}

function outlineAgain() {
  $("outline").dataset.sig = "";
  renderOutline();
}

function outlineGroup(s) {
  const box = h("details", { class: "fold", id: `ol-${s.id}`, open: "" }, h("summary", {}, outlineLink(s)));
  collapsible(box);
  const key = `ol-${s.id}`;
  return h("li", { class: "ol-sec" }, lazy(box, () => h("ul", { class: "ol-items" },
    ...firstPage(key, s.items, (i) => i.id).map((i) => h("li", {}, outlineLink(i))), moreButton(key, s.items.length, "items", outlineAgain) || "")));
}

export function outlineBoxes() {
  return [...$("outline").querySelectorAll("details.fold")];
}

function foldAll() {
  const open = !groupOpen("outline");
  for (const box of outlineBoxes()) box.open = open;
  rememberGroup("outline", open);
  markToggles();
}

export function sectionBoxes() {
  return [...document.querySelectorAll(".col > section[data-outline]:not(#sec-overview) > details.fold")];
}

export function foldSections() {
  const open = !groupOpen("sections");
  for (const box of sectionBoxes()) box.open = open;
  rememberGroup("sections", open);
  markToggles();
}

function pageHeads() {
  return [...document.querySelectorAll(".col > section[data-outline]")].filter((s) => !s.hidden).map((s) => ({
    id: s.id, title: s.querySelector("summary").firstChild.textContent.trim(), list: s.dataset.outline }));
}

export function renderOutline() {
  const tree = outlineOf(doc, pageHeads());
  const nav = $("outline");
  const sig = JSON.stringify(tree);
  if (nav.dataset.sig !== sig) {
    nav.dataset.sig = sig;
    $("jump-to").replaceChildren(h("option", { value: "", text: "Choose a section" }), ...tree.map((s) => h("option", { value: s.id, text: s.title })));
    nav.replaceChildren(h("div", { class: "ol-tools" }, h("button", { class: "link", type: "button", id: "outline-all", on: { click: foldAll } })),
      h("ul", {}, ...tree.map(outlineGroup)));
  }
  markOutline();
  markPending();
}

function markPending() {
  const pending = new Set(doc.notifications.map((n) => noticeTarget(n.item)));
  for (const n of doc.notifications) {
    const list = n.item.split("/")[0];
    if (["tasks", "followups"].includes(list)) pending.add("sec-" + list);
  }
  for (const a of $("outline").querySelectorAll("a[data-target]")) a.classList.toggle("pending", pending.has(a.dataset.target));
}

export function markOutline() {
  const nav = $("outline");
  const links = [...nav.querySelectorAll("a[data-target]")];
  let current = outlinePick && links.find((a) => a.dataset.target === outlinePick);
  if (!current) for (const a of links) {
    const el = document.getElementById(a.dataset.target);
    if (el && el.getClientRects().length && el.getBoundingClientRect().top <= 120) current = a;
  }
  if (current && !current.getClientRects().length) current = current.closest(".ol-sec").querySelector("a");
  const section = current && current.closest(".ol-sec").querySelector("a");
  for (const a of links) {
    a.classList.toggle("on", a === current);
    a.classList.toggle("in", a === section && a !== current);
  }
  if (current && (current.offsetTop < nav.scrollTop || current.offsetTop > nav.scrollTop + nav.clientHeight - 24)) {
    nav.scrollTop = current.offsetTop - nav.clientHeight / 2;
  }
}

export function reveal(el) {
  const panel = el.closest('[role="tabpanel"]');
  if (panel) selectTab(panel.id, false);
  for (let box = el.closest("details"); box; box = box.parentElement.closest("details")) box.open = true;
}

export function showOutline(open) {
  $("outline").classList.toggle("open", open);
  $("outline-toggle").setAttribute("aria-expanded", String(open));
}

export function wireOutline() {
  $("outline-toggle").addEventListener("click", () => showOutline(!$("outline").classList.contains("open")));
  $("outline").addEventListener("click", (ev) => {
    const a = ev.target.closest("a[data-target]");
    const el = a && revealTarget(a.dataset.target);
    if (!el) return;
    ev.preventDefault();
    outlinePick = a.dataset.target;
    el.scrollIntoView({ behavior: "smooth", block: "start" });
    showOutline(false);
    markOutline();
  });
  const userScroll = () => { outlinePick = null; };
  for (const ev of ["wheel", "touchstart", "keydown"]) window.addEventListener(ev, userScroll, { passive: true });
  for (const ev of ["scroll", "resize"]) window.addEventListener(ev, () => {
    if (!outlineFrame) outlineFrame = requestAnimationFrame(() => { outlineFrame = 0; markOutline(); });
  }, { passive: true });
}
