import { FOLD_KEY, TOGGLES_KEY } from "./config.js";
import { $, store, stored } from "./dom.js";
import { rememberComment } from "./threads.js";
import { doc } from "./sync.js";
import { outlineBoxes, sectionBoxes } from "./outline.js";

export const toggles = stored(TOGGLES_KEY, {});

export function groupOpen(id) {
  return !!toggles[id];
}

export function rememberGroup(id, open) {
  toggles[id] = open;
  store(TOGGLES_KEY, toggles);
}

function seedGroup(id, boxes) {
  if (typeof toggles[id] !== "boolean" && boxes.length) rememberGroup(id, boxes.every((b) => b.open));
}

export function rememberUnanimous(id, boxes) {
  const open = boxes.filter((b) => b.open).length;
  if (boxes.length && (!open || open === boxes.length)) rememberGroup(id, open > 0);
}

export function commentBoxes(section) {
  return [...section.querySelectorAll("details[data-key]")];
}

export function markToggles() {
  seedGroup("all-comments", [...document.querySelectorAll(".col details[data-key]")]);
  $("comments-all").textContent = groupOpen("all-comments") ? "Hide all comments" : "Show all comments";
  for (const btn of document.querySelectorAll("button[data-comments]")) {
    const section = btn.closest("section");
    seedGroup(section.id, commentBoxes(section));
    btn.textContent = groupOpen(section.id) ? "Hide all comments" : "Show all comments";
  }
  seedGroup("outline", outlineBoxes());
  $("outline-all").textContent = groupOpen("outline") ? "Collapse all" : "Expand all";
  seedGroup("sections", sectionBoxes());
  $("sections-all").textContent = groupOpen("sections") ? "Collapse all" : "Expand all";
}

export function foldSaved() {
  try { return JSON.parse(localStorage.getItem(FOLD_KEY) || "{}") || {}; } catch (e) { return {}; }
}

export function collapsible(box) {
  const saved = foldSaved();
  if (typeof saved[box.id] === "boolean") box.open = saved[box.id];
  box.querySelector(":scope > summary").addEventListener("click", (ev) => { if (ev.target.closest("button, a")) ev.preventDefault(); });
  box.addEventListener("toggle", () => {
    const state = foldSaved();
    state[box.id] = box.open;
    try { localStorage.setItem(FOLD_KEY, JSON.stringify(state)); } catch (e) { /* storage blocked: defaults return on reload */ }
  });
}

export function setAllComments(section, open) {
  if (open) section.querySelector("details.fold").open = true;
  if (section.id === "sec-phases") for (const p of doc.phases) rememberComment(`phases/${p.id}`, open);
  for (const box of commentBoxes(section)) {
    rememberComment(box.dataset.key, open);
    box.open = open;
  }
  rememberGroup(section.id, open);
}
