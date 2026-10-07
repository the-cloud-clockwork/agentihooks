import { GRIP_STEPS, LAYOUT_KEY, LAYOUT_LIMITS, SLUG } from "./config.js";
import { $, store, stored } from "./dom.js";
import { readLayout, writeLayout } from "./api.js";
import { showArtifacts } from "./artifacts.js";
import { anchorPanel, showAlerts, showNotifications } from "./notices.js";
import { revealTarget } from "./outline.js";
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
  } else selectTab(stored(`plan-ledger:${SLUG}:tab`, "ledger"), false);
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

function applyLayout(next) {
  layout = next && typeof next === "object" && !Array.isArray(next) ? next : {};
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
  $("layout-reset").addEventListener("click", () => setLayout({}, 0));
  readLayout()
    .then((resp) => (resp.ok ? resp.json() : null))
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
