import { $, h } from "./dom.js";
import { itemState } from "./state.js";
import { closedLeft, doc, pageIn, pagedOut } from "./sync.js";
import { searchClosed } from "./api.js";
import { noticeTarget, showAlerts, showNotifications } from "./notices.js";
import { showArtifacts } from "./artifacts.js";
import { renderSwarm, swarm } from "./swarm.js";
import { collapsible, groupOpen, markToggles, rememberGroup } from "./folds.js";
import { selectTab } from "./layout.js";
import { render } from "./render.js";
import { closedMore, firstPage, lazy, moreButton, wanted } from "./pages.js";
import { revealFrozenTarget, viewCount, viewItems } from "./frozen_view.js";

let outlinePick = null;

let outlineFrame = 0;
let outlineObserver = null;
const outlineTargets = new Map();
const outlinePositions = new WeakMap();
let markedCurrent = null;
let markedSection = null;

function swarmRows() {
  selectTab("swarm", false);
  renderSwarm(swarm);
}

const PANEL_ROWS = { notice: () => showNotifications(true), alert: () => showAlerts(true), artifact: () => showArtifacts(true), finding: swarmRows, seat: swarmRows };

function fetchTarget(id) {
  const kind = id.split("-")[0];
  if (kind === "item") {
    wanted.ids = new Set(revealFrozenTarget(doc, id));
    const list = id.split("-")[1];
    const section = document.querySelector(`section[data-outline="${["plans", "slices"].includes(list) ? "phases" : list}"] > details.fold`);
    if (section) section.open = true;
  } else if (!Object.hasOwn(PANEL_ROWS, kind)) return;
  wanted.id = id;
  (kind === "item" ? render : PANEL_ROWS[kind])();
  setTimeout(() => { wanted.id = null; wanted.ids.clear(); }, 0);
}

export function revealTarget(id) {
  if (!document.getElementById(id)) fetchTarget(id);
  const el = document.getElementById(id);
  if (el) reveal(el);
  return el;
}

export function pageTo(id) {
  const [kind, list, ...rest] = id.split("-");
  if (kind !== "item" || document.getElementById(id) || !pagedOut(list, rest.join("-"))) return false;
  pageIn(list, rest.join("-")).then(() => land(revealTarget(id)));
  return true;
}

export function jumpTo(ev, item) {
  const id = `item-${item.replace("/", "-")}`;
  if (pageTo(id)) return ev.preventDefault();
  const el = revealTarget(id);
  if (!el) return;
  ev.preventDefault();
  land(el);
}

function land(el) {
  if (!el) return;
  el.scrollIntoView({ behavior: "smooth", block: "center" });
  el.classList.remove("flash");
  void el.offsetWidth;
  el.classList.add("flash");
}

function outlineEntry(list) {
  return (i, n) => typeof i === "string" ? { id: `item-${list}-${n}`, title: i }
    : { id: `item-${list}-${i.id}`, title: list === "tasks" ? `${i.id} ${i.title}` : i.title || i.text || i.item || i.id, state: itemState(list, i),
      ...(list === "tasks" ? { status: i.state || "open", phase: i.phase || "" } : {}) };
}

function outlineOf(doc, heads) {
  return heads.map((head) => {
    const all = (head.list ? doc[head.list] || [] : []).filter((i) => !i.deleted);
    const list = viewItems(head.list, all);
    const grouped = ["tasks", "followups"].includes(head.list);
    const hidden = head.list ? closedLeft(head.list) : 0;
    const counts = grouped ? [...new Set([...list.map((i) => i.state || (i.done ? "done" : "open")), ...(hidden ? ["done"] : [])])]
      .map((state) => `${list.filter((i) => (i.state || (i.done ? "done" : "open")) === state).length + (state === "done" ? hidden : 0)} ${state}`).join(" · ") : "";
    return { id: head.id, title: head.title + (counts ? ` · ${counts}` : "") + viewCount(head.list, all),
      items: head.list === "followups" ? [] : list.map(outlineEntry(head.list)) };
  });
}

function outlineLink(e) {
  return h("a", { href: `#${e.id}`, "data-target": e.id, title: e.title, text: e.title, class: e.state && `st-${e.state}` });
}

function outlineAgain() {
  $("outline").dataset.sig = "";
  renderOutline();
}

const SEARCHED = ["phases", "tasks", "questions"];
const filter = { text: "", state: "" };
const remote = {};
const remoteTotal = {};
let searchTurn = 0;
let searchTimer = 0;

function filtering() {
  return !!(filter.text || filter.state);
}

function statusOf(i) {
  return i.state === "out" ? "out" : i.status || i.state;
}

function matches(i) {
  return (!filter.text || i.title.toLowerCase().includes(filter.text)) && (!filter.state || statusOf(i) === filter.state);
}

export function completeList(list) {
  if (!["phases", "tasks", "questions", "followups"].includes(list)) return false;
  const rows = (doc[list] || []).filter((i) => !i.deleted);
  return (rows.length > 0 || closedLeft(list) > 0) && rows.every((i) => itemState(list, i) === "done");
}

function byPhase(items) {
  if (!items.some((i) => i.phase)) return items;
  const order = doc.phases.map((p) => p.id);
  const rank = (i) => (order.includes(i.phase) ? order.indexOf(i.phase) : order.length);
  return [...items].sort((a, b) => rank(a) - rank(b) || (rank(a) === order.length ? a.phase.localeCompare(b.phase) : 0));
}

function phaseRows(sorted) {
  if (!sorted.some((i) => i.phase)) return sorted;
  const titles = Object.fromEntries(doc.phases.map((p) => [p.id, p.title || p.id]));
  const grouped = [];
  for (const [n, i] of sorted.entries()) {
    if (!n || sorted[n - 1].phase !== i.phase) grouped.push({ id: `ol-group-${i.phase || "none"}`, group: i.phase ? titles[i.phase] || i.phase : "No phase" });
    grouped.push(i);
  }
  return grouped;
}

function shapeOutline(tree) {
  return tree.map((s) => {
    const list = s.id.replace(/^sec-/, "");
    const known = new Set(s.items.map((i) => i.id));
    const found = (remote[list] || []).map(outlineEntry(list)).filter((i) => !known.has(i.id));
    const items = [...s.items, ...found].filter((i) => !filtering() || matches(i));
    return { ...s, items: list === "tasks" ? byPhase(items) : items, hits: items.length, complete: completeList(list) };
  }).filter((s) => !filtering() || s.hits);
}

async function searchRemote() {
  const turn = ++searchTurn;
  for (const list of SEARCHED) {
    delete remote[list];
    delete remoteTotal[list];
  }
  if (filter.text && ["", "done"].includes(filter.state)) {
    for (const list of SEARCHED.filter((name) => closedLeft(name))) {
      try {
        const resp = await searchClosed(list, filter.text);
        if (turn !== searchTurn) return;
        if (resp.ok) {
          const { data, total } = await resp.json();
          remote[list] = data;
          remoteTotal[list] = total;
        }
      } catch (e) { /* offline: the loaded rows still filter */ }
    }
  }
  if (turn === searchTurn) renderOutline();
}

function setFilter(text, state) {
  filter.text = text.trim().toLowerCase();
  filter.state = state;
  renderOutline();
  clearTimeout(searchTimer);
  searchTimer = setTimeout(searchRemote, 200);
}

const STATES = [["", "Any state"], ["open", "Open"], ["claimed", "Claimed"], ["pr", "In PR"], ["blocked", "Blocked"], ["done", "Done"], ["out", "Out of scope"]];

function outlineTools() {
  const input = h("input", { class: "ol-filter", id: "outline-filter", type: "search", placeholder: "Filter", autocomplete: "off",
    "aria-label": "Filter the outline by title or id" });
  const pick = h("select", { class: "ol-state", id: "outline-state", "aria-label": "Filter the outline by state" },
    ...STATES.map(([value, text]) => h("option", { value, text })));
  const again = () => setFilter(input.value, pick.value);
  input.addEventListener("input", again);
  pick.addEventListener("change", again);
  return h("div", { class: "ol-tools" }, input, pick, h("button", { class: "link", type: "button", id: "outline-all", on: { click: foldAll } }));
}

function remoteMore(s) {
  const list = s.id.replace(/^sec-/, "");
  const left = filtering() ? (remoteTotal[list] || 0) - (remote[list] || []).length : 0;
  return left > 0 ? h("li", { class: "ol-group ol-more", text: `${left} older matches` }) : "";
}

function outlineRow(i) {
  return i.group ? h("li", { class: "ol-group", text: i.group }) : h("li", {}, outlineLink(i));
}

function outlineGroup(s) {
  const box = h("details", { class: "fold", id: `ol-${s.id}`, open: filtering() || !s.complete ? "" : false }, h("summary", {}, outlineLink(s)));
  if (box.open) box.dataset.fresh = "";
  if (!filtering()) collapsible(box);
  const key = `ol-${s.id}`;
  return h("li", { class: "ol-sec" }, lazy(box, () => h("ul", { class: "ol-items" },
    ...phaseRows(firstPage(key, s.items, (i) => i.id)).map(outlineRow), remoteMore(s), moreButton(key, s.items.length, "more items", outlineAgain) || "",
    (s.items.length && !filtering() && closedMore(s.id.replace(/^sec-/, ""), "done", () => render())) || "")));
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
  const shaped = shapeOutline(tree);
  const nav = $("outline");
  const sig = JSON.stringify(shaped);
  if (nav.dataset.sig !== sig) {
    nav.dataset.sig = sig;
    $("jump-to").replaceChildren(h("option", { value: "", text: "Choose a section" }), ...tree.map((s) => h("option", { value: s.id, text: s.title })));
    if (!$("outline-filter")) nav.replaceChildren(outlineTools());
    const list = h("ul", { id: "outline-list" }, ...shaped.map(outlineGroup));
    const old = $("outline-list");
    if (old) old.replaceWith(list);
    else nav.append(list);
  }
  scheduleOutline();
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

function observeOutline(links) {
  if (!outlineObserver) outlineObserver = new IntersectionObserver((entries) => {
    for (const entry of entries) outlinePositions.set(entry.target, {
      passed: entry.boundingClientRect.top <= 120,
      visible: !!(entry.boundingClientRect.width || entry.boundingClientRect.height),
    });
    markOutline();
  });
  const targets = new Map();
  for (const link of links) {
    const id = link.dataset.target;
    const target = document.getElementById(id);
    if (target) targets.set(id, target);
  }
  for (const [id, target] of outlineTargets) if (targets.get(id) !== target) {
    outlineObserver.unobserve(target);
    outlineTargets.delete(id);
  }
  for (const [id, target] of targets) if (outlineTargets.get(id) !== target) {
    outlineTargets.set(id, target);
    outlineObserver.observe(target);
  }
}

function scheduleOutline() {
  if (outlineFrame) return;
  outlineFrame = requestAnimationFrame(() => setTimeout(() => {
    outlineFrame = 0;
    if (outlineObserver) outlineObserver.disconnect();
    outlineTargets.clear();
    markOutline();
  }, 0));
}

export function markOutline() {
  const nav = $("outline");
  const links = [...nav.querySelectorAll("a[data-target]")];
  observeOutline(links);
  let current = outlinePick && links.find((a) => a.dataset.target === outlinePick);
  if (!current) for (const a of links) {
    const position = outlinePositions.get(outlineTargets.get(a.dataset.target));
    if (position && position.visible && position.passed) current = a;
  }
  if (current && !current.getClientRects().length) current = current.closest(".ol-sec").querySelector("a");
  const section = current && current.closest(".ol-sec").querySelector("a");
  if (current === markedCurrent && section === markedSection) return;
  if (current) current.scrollIntoView({ block: "nearest" });
  for (const a of [markedCurrent, markedSection]) if (a) a.classList.remove("on", "in");
  if (current) current.classList.add("on");
  if (section && section !== current) section.classList.add("in");
  markedCurrent = current;
  markedSection = section;
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

const WALKS = ["#outline a[data-target]", "#tasks .item-title > a.task-id"];
const STEPS = { ArrowDown: 1, ArrowUp: -1, j: 1, k: -1 };

function typing(el) {
  return !!el && (el.isContentEditable || ["INPUT", "TEXTAREA", "SELECT"].includes(el.tagName));
}

function walkKeys(ev) {
  const step = STEPS[ev.key];
  if (!step || ev.defaultPrevented || ev.altKey || ev.ctrlKey || ev.metaKey || ev.shiftKey || typing(ev.target)) return;
  const at = document.activeElement;
  const walk = WALKS.find((selector) => at && at.matches(selector));
  if (!walk && ev.key.startsWith("Arrow")) return;
  const links = [...document.querySelectorAll(walk || WALKS[0])].filter((a) => a.getClientRects().length);
  if (!links.length) return;
  ev.preventDefault();
  const n = links.indexOf(at);
  const next = n < 0 ? links.find((a) => a.classList.contains("on")) || links[0] : links[Math.min(links.length - 1, Math.max(0, n + step))];
  next.focus();
  next.scrollIntoView({ block: "nearest" });
}

export function wireOutline() {
  $("outline-toggle").addEventListener("click", () => showOutline(!$("outline").classList.contains("open")));
  document.querySelector(".col").addEventListener("toggle", scheduleOutline, true);
  document.addEventListener("scroll", (ev) => { if (ev.target !== $("outline")) scheduleOutline(); }, { capture: true, passive: true });
  $("outline").addEventListener("click", (ev) => {
    const a = ev.target.closest("a[data-target]");
    if (a && pageTo(a.dataset.target)) {
      ev.preventDefault();
      showOutline(false);
      return;
    }
    const el = a && revealTarget(a.dataset.target);
    if (!el) return;
    ev.preventDefault();
    outlinePick = a.dataset.target;
    el.scrollIntoView({ behavior: "smooth", block: "start" });
    showOutline(false);
    markOutline();
  });
  document.addEventListener("keydown", walkKeys);
  const userScroll = () => { outlinePick = null; };
  for (const ev of ["wheel", "touchstart", "keydown"]) window.addEventListener(ev, userScroll, { passive: true });
  for (const ev of ["scroll", "resize"]) window.addEventListener(ev, scheduleOutline, { passive: true });
}
