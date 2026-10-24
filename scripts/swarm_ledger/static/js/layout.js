import { GRIP_STEPS, LAYOUT_KEY, LAYOUT_LIMITS, SLUG } from "./config.js";
import { $, store, stored } from "./dom.js";
import { readLayout, writeLayout } from "./api.js";
import { showArtifacts } from "./artifacts.js";
import { anchorPanel, showAlerts, showNotifications } from "./notices.js";
import { pageTo, revealTarget } from "./outline.js";
import { renderSwarm, swarm } from "./swarm.js";

const tabScroll = {};

export function selectTab(id, updateHash = true) {
  if (!["ledger", "swarm"].includes(id)) return;
  const previous = document.querySelector('[role="tab"][aria-selected="true"]').getAttribute("aria-controls");
  if (previous !== id) tabScroll[previous] = $("main-content").scrollTop;
  for (const name of ["ledger", "swarm"]) {
    const active = name === id;
    $(name).hidden = !active;
    $("tab-" + name).setAttribute("aria-selected", String(active));
    $("tab-" + name).tabIndex = active ? 0 : -1;
  }
  if (previous !== id && id === "swarm") renderSwarm(swarm);
  if (previous !== id) $("main-content").scrollTop = tabScroll[id] || 0;
  store(`plan-ledger:${SLUG}:tab`, id);
  if (updateHash) history.replaceState(null, "", "#" + id);
}

export function openHash() {
  const id = decodeURIComponent(location.hash.slice(1));
  if (["ledger", "swarm"].includes(id)) return selectTab(id, false);
  const target = id && revealTarget(id);
  if (target) {
    requestAnimationFrame(() => target.scrollIntoView({ block: "start" }));
    return;
  }
  selectTab(stored(`plan-ledger:${SLUG}:tab`, "ledger"), false);
  pageTo(id);
}

export function wireTabs() {
  const tabs = [...document.querySelectorAll('[role="tab"]')];
  for (const [index, tab] of tabs.entries()) {
    tab.addEventListener("click", () => selectTab(tab.getAttribute("aria-controls")));
    tab.addEventListener("keydown", (ev) => {
      const next = { ArrowRight: (index + 1) % 2, ArrowLeft: (index + 1) % 2, Home: 0, End: 1 }[ev.key];
      if (next === undefined) return;
      ev.preventDefault();
      tabs[next].click();
      tabs[next].focus({ preventScroll: true });
    });
  }
  window.addEventListener("hashchange", openHash);
  openHash();
}

let layout = {};
let layoutSave = 0;

function knownRows(next) {
  const rows = new Set([...document.querySelectorAll("#swarm-box [data-grip]")].map((grip) => grip.dataset.row));
  return Object.fromEntries(Object.entries(next).filter(([key]) => ["columns", "stats"].includes(key) || rows.has(key)));
}

function applyLayout(next) {
  layout = next && typeof next === "object" && !Array.isArray(next) ? knownRows(next) : {};
  for (const grip of document.querySelectorAll("#swarm-box [data-grip]")) {
    const row = $(grip.dataset.row), value = (layout[row.id] || {})[grip.dataset.grip];
    const [cls, prop, unit] = grip.dataset.grip === "height" ? ["sized", "--h", "px"] : ["split", "--split", "%"];
    row.classList.toggle(cls, typeof value === "number");
    if (typeof value === "number") row.style.setProperty(prop, value + unit);
    else row.style.removeProperty(prop);
    if (grip.dataset.grip === "split") {
      if (typeof value === "number") grip.setAttribute("aria-valuenow", String(value));
      else grip.removeAttribute("aria-valuenow");
    }
  }
  $("layout-reset").disabled = !Object.keys(layout).length;
  applyColumns();
  orderStats();
}

function orderStats() {
  const box = $("stats"), order = Array.isArray(layout.stats) ? layout.stats : [];
  if (!box) return;
  const rows = [...box.children], rank = (row) => {
    const at = order.indexOf(row.dataset.stat);
    return at < 0 ? order.length + rows.indexOf(row) : at;
  };
  const sorted = [...rows].sort((a, b) => rank(a) - rank(b));
  if (sorted.some((row, i) => row !== rows[i])) box.append(...sorted);
}

function saveStats() {
  setLayout({ ...layout, stats: [...$("stats").children].map((row) => row.dataset.stat).filter(Boolean) }, 0);
}

function wireStats() {
  const box = $("stats");
  box.addEventListener("pointerdown", (ev) => {
    const grip = ev.target.closest(".stat-grip");
    if (!grip || ev.button !== 0) return;
    ev.preventDefault();
    const row = grip.closest(".stat");
    row.classList.add("dragging");
    document.body.classList.add("reordering");
    const move = (e) => {
      if (e.pointerId !== ev.pointerId || !row.isConnected) return;
      const target = [...box.children].find((other) => {
        const at = other.getBoundingClientRect();
        return other !== row && e.clientY >= at.top && e.clientY < at.bottom;
      });
      if (!target) return;
      const at = target.getBoundingClientRect();
      if (e.clientY < at.top + at.height / 2) target.before(row);
      else target.after(row);
    };
    const end = (e) => {
      if (e.pointerId !== ev.pointerId) return;
      document.removeEventListener("pointermove", move);
      document.removeEventListener("pointerup", end);
      document.removeEventListener("pointercancel", end);
      row.classList.remove("dragging");
      document.body.classList.remove("reordering");
      saveStats();
    };
    document.addEventListener("pointermove", move);
    document.addEventListener("pointerup", end);
    document.addEventListener("pointercancel", end);
  });
  box.addEventListener("keydown", (ev) => {
    const grip = ev.target.closest(".stat-grip"), row = grip && grip.closest(".stat");
    const next = row && { ArrowUp: row.previousElementSibling, ArrowDown: row.nextElementSibling }[ev.key];
    if (!next) return;
    ev.preventDefault();
    if (ev.key === "ArrowUp") next.before(row);
    else next.after(row);
    grip.focus();
    saveStats();
  });
  document.addEventListener("stats", orderStats);
}

const COLUMN_MIN = 40;
const COLUMN_STEP = 16;

function columnKey(table) {
  return `${table.id}-${table.tHead.rows[0].cells.length}`;
}

function columnHeads(table) {
  return [...table.tHead.rows[0].cells];
}

export function applyColumns() {
  const saved = layout.columns || {};
  for (const table of document.querySelectorAll("table.sw-table[id]")) {
    const heads = columnHeads(table), widths = saved[columnKey(table)];
    heads.forEach((th, i) => {
      if (i < heads.length - 1 && !th.querySelector(":scope > .col-grip")) th.append(columnGrip(th));
    });
    const sized = Array.isArray(widths) && widths.length === heads.length;
    table.classList.toggle("sized-cols", sized);
    heads.forEach((th, i) => { th.style.width = sized ? `${widths[i]}px` : ""; });
    table.style.width = sized ? `${widths.reduce((a, b) => a + b, 0)}px` : "";
  }
}

function columnGrip(th) {
  const grip = document.createElement("span");
  grip.className = "col-grip";
  grip.tabIndex = 0;
  grip.setAttribute("role", "separator");
  grip.setAttribute("aria-orientation", "vertical");
  grip.setAttribute("aria-label", `Resize the ${th.textContent.trim() || "column"} column`);
  return grip;
}

function setColumns(table, widths) {
  setLayout({ ...layout, columns: { ...(layout.columns || {}), [columnKey(table)]: widths.map((w) => Math.round(w)) } });
}

function measured(table) {
  return columnHeads(table).map((th) => th.getBoundingClientRect().width);
}

function wireColumns() {
  document.addEventListener("pointerdown", (ev) => {
    const grip = ev.target.closest(".col-grip");
    if (!grip || ev.button !== 0) return;
    ev.preventDefault();
    const th = grip.parentElement, table = th.closest("table"), index = th.cellIndex, start = measured(table), x0 = ev.clientX;
    grip.setPointerCapture(ev.pointerId);
    grip.classList.add("dragging");
    document.body.classList.add("resizing-x");
    const move = (e) => setColumns(table, start.map((w, i) => (i === index ? Math.max(COLUMN_MIN, w + e.clientX - x0) : w)));
    grip.addEventListener("pointermove", move);
    grip.addEventListener("lostpointercapture", () => {
      grip.removeEventListener("pointermove", move);
      grip.classList.remove("dragging");
      document.body.classList.remove("resizing-x");
    }, { once: true });
  });
  document.addEventListener("keydown", (ev) => {
    const grip = ev.target.closest && ev.target.closest(".col-grip");
    const step = grip && { ArrowLeft: -COLUMN_STEP, ArrowRight: COLUMN_STEP }[ev.key];
    if (!step) return;
    ev.preventDefault();
    const table = grip.closest("table"), index = grip.parentElement.cellIndex;
    setColumns(table, measured(table).map((w, i) => (i === index ? Math.max(COLUMN_MIN, w + step * (ev.shiftKey ? 4 : 1)) : w)));
  });
  document.addEventListener("columns", applyColumns);
}

function mirrorLayout() {
  try {
    if (Object.keys(layout).length) localStorage.setItem(LAYOUT_KEY, JSON.stringify(layout));
    else localStorage.removeItem(LAYOUT_KEY);
  } catch (e) { /* storage blocked: the server copy paints after its read */ }
}

function saveLayout(delay) {
  clearTimeout(layoutSave);
  layoutSave = setTimeout(() => {
    layoutSave = 0;
    writeLayout(layout).catch(() => {});
  }, delay);
}

function setLayout(next, delay = 300) {
  applyLayout(next);
  mirrorLayout();
  saveLayout(delay);
}

function gripValue(row, kind) {
  const box = row.getBoundingClientRect();
  return kind === "height" ? box.height : 100 * row.firstElementChild.getBoundingClientRect().width / box.width;
}

function resize(row, kind, value) {
  const [low, high, scale] = LAYOUT_LIMITS[kind];
  const size = Math.round(Math.min(high, Math.max(low, value)) * scale) / scale;
  setLayout({ ...layout, [row.id]: { ...layout[row.id], [kind]: size } });
}

function wireGrip(grip) {
  const row = $(grip.dataset.row), kind = grip.dataset.grip;
  grip.addEventListener("pointerdown", (ev) => {
    if (ev.button !== 0) return;
    ev.preventDefault();
    grip.setPointerCapture(ev.pointerId);
    const box = row.getBoundingClientRect(), y0 = ev.clientY, mode = kind === "height" ? "resizing-y" : "resizing-x";
    const move = (e) => resize(row, kind, kind === "height" ? box.height + e.clientY - y0 : 100 * (e.clientX - box.left) / box.width);
    grip.classList.add("dragging");
    document.body.classList.add(mode);
    grip.addEventListener("pointermove", move);
    grip.addEventListener("lostpointercapture", () => {
      grip.removeEventListener("pointermove", move);
      grip.classList.remove("dragging");
      document.body.classList.remove(mode);
    }, { once: true });
  });
  grip.addEventListener("keydown", (ev) => {
    const step = GRIP_STEPS[kind][ev.key];
    if (!step) return;
    ev.preventDefault();
    resize(row, kind, gripValue(row, kind) + step * (ev.shiftKey ? 4 : 1));
  });
}

export function startLayout() {
  applyLayout(stored(LAYOUT_KEY, {}));
  for (const grip of document.querySelectorAll("#swarm-box [data-grip]")) wireGrip(grip);
  wireColumns();
  wireStats();
  $("layout-reset").addEventListener("click", () => setLayout({}, 0));
  readLayout()
    .then((resp) => (resp.ok ? resp.json().then((reply) => reply.data) : null))
    .then((saved) => {
      if (saved && !layoutSave) { applyLayout(saved); mirrorLayout(); }
    })
    .catch(() => {})
    .finally(() => { document.documentElement.dataset.layout = "ready"; });
}

export function wireRail() {
  const rail = $("icon-strip");
  const railButtons = ["home", "art-fab", "bell", "alert-fab", "to-top", "sync"].map($);
  const placeRail = () => {
    for (const btn of railButtons) rail.append(btn);
  };
  placeRail();
  window.addEventListener("resize", placeRail);
  const stripPanels = [[$("notif"), $("bell"), showNotifications], [$("alert-panel"), $("alert-fab"), showAlerts], [$("art-panel"), $("art-fab"), showArtifacts]];
  const anchorOpen = () => { for (const [panel, icon] of stripPanels) if (!panel.hidden) anchorPanel(panel, icon); };
  new ResizeObserver(anchorOpen).observe(rail);
  document.addEventListener("scroll", anchorOpen, { capture: true, passive: true });
  document.addEventListener("pointerdown", (ev) => {
    const path = ev.composedPath();
    if (path.some((el) => el instanceof HTMLDialogElement)) return;
    for (const [panel, icon, show] of stripPanels) if (!panel.hidden && !path.includes(panel) && !path.includes(icon)) show(false);
  });
}

export function wireStatsDrawer() {
  const panel = $("stats-column");
  const narrow = window.matchMedia("(max-width: 1023px)");
  const toggle = document.createElement("button");
  toggle.className = "stats-toggle";
  toggle.id = "stats-toggle";
  toggle.type = "button";
  toggle.setAttribute("aria-controls", "stats-column");
  toggle.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M15 18l-6-6 6-6"/></svg>';
  document.body.append(toggle);
  const isOpen = () => document.body.classList.contains("stats-open");
  const show = (open) => {
    document.body.classList.toggle("stats-open", open);
    toggle.setAttribute("aria-expanded", String(open));
    toggle.setAttribute("aria-label", open ? "Hide stats" : "Show stats");
  };
  show(false);
  toggle.addEventListener("click", () => show(!isOpen()));
  let start = null;
  document.addEventListener("touchstart", (ev) => {
    const touch = ev.touches[0];
    const skip = ev.touches.length !== 1 || ev.target.closest(".sw-scroll, .chat-panel, .notif, input, textarea, select");
    start = narrow.matches && !skip ? { x: touch.clientX, y: touch.clientY } : null;
  }, { passive: true });
  document.addEventListener("touchend", (ev) => {
    if (!start) return;
    const touch = ev.changedTouches[0];
    const dx = touch.clientX - start.x;
    const dy = touch.clientY - start.y;
    const from = start.x;
    start = null;
    if (Math.abs(dx) < 60 || Math.abs(dy) > Math.abs(dx) / 2) return;
    if (dx > 0 && isOpen()) show(false);
    else if (dx < 0 && !isOpen() && from > window.innerWidth * 0.5) show(true);
  }, { passive: true });
  document.addEventListener("pointerdown", (ev) => {
    const path = ev.composedPath();
    if (isOpen() && !path.includes(panel) && !path.includes(toggle)) show(false);
  });
  document.addEventListener("keydown", (ev) => { if (ev.key === "Escape" && isOpen()) show(false); });
  narrow.addEventListener("change", () => show(false));
}
