import { QUOTA_REFRESH_MS } from "./config.js";
import { $ } from "./dom.js";
import { flush, loadMetadata, loadSaved, loaded, renderSync, sendSync } from "./sync.js";
import { showArtifacts } from "./artifacts.js";
import { editTitle, render, renderStats, saveTitle } from "./render.js";
import { clearNotification, clearPriority, showAlerts, showNotifications } from "./notices.js";
import { foldSections, markOutline, outlineBoxes, reveal, sectionBoxes, showOutline, wireOutline } from "./outline.js";
import { collapsible, commentBoxes, groupOpen, markToggles, rememberGroup, rememberUnanimous, setAllComments } from "./folds.js";
import { openHash, startLayout, wireRail, wireTabs } from "./layout.js";
import { showChat, wireChat, wireChatInput } from "./chat.js";
import { refreshQuota, wireSwarm } from "./controls.js";
import { connectEvents } from "./events.js";

function start() {
  loadSaved();
  startLayout();
  $("sync").addEventListener("click", () => sendSync("sync", "sync"));
  $("stats-sync").addEventListener("click", () => sendSync("stats_sync", "stats-sync"));
  $("title-edit").addEventListener("click", () => editTitle(true));
  $("title-cancel").addEventListener("click", () => editTitle(false));
  $("title-form").addEventListener("submit", (ev) => { ev.preventDefault(); saveTitle(); });
  $("title-input").addEventListener("keydown", (ev) => {
    if (ev.key === "Enter") { ev.preventDefault(); saveTitle(); }
    else if (ev.key === "Escape") { ev.preventDefault(); editTitle(false); }
  });
  $("title-input").addEventListener("input", () => { $("title-error").textContent = ""; });
  $("prio-clear-all").addEventListener("click", () => { if (confirm("Clear every priority?")) clearPriority("all"); });
  $("to-top").addEventListener("click", () => $("main-content").scrollTo({ top: 0, behavior: "smooth" }));
  $("bell").addEventListener("click", () => showNotifications($("notif").hidden));
  $("notif-close").addEventListener("click", () => showNotifications(false));
  $("alert-fab").addEventListener("click", () => showAlerts($("alert-panel").hidden));
  $("alert-close").addEventListener("click", () => showAlerts(false));
  $("art-fab").addEventListener("click", () => showArtifacts($("art-panel").hidden));
  $("art-close").addEventListener("click", () => showArtifacts(false));
  $("notif-clear-all").addEventListener("click", () => { if (confirm("Clear every notification?")) clearNotification("all"); });
  wireChat();
  document.addEventListener("keydown", (ev) => {
    if (ev.key !== "Escape" || ev.defaultPrevented) return;
    if ($("outline").classList.contains("open")) showOutline(false);
    else if (!$("notif").hidden) showNotifications(false);
    else if (!$("alert-panel").hidden) showAlerts(false);
    else if (!$("art-panel").hidden) showArtifacts(false);
    else if (!$("chat-panel").hidden) showChat(false);
  });
  wireOutline();
  wireChatInput();
  for (const box of document.querySelectorAll("details.fold")) collapsible(box);
  for (const btn of document.querySelectorAll("button[data-comments]")) {
    const section = btn.closest("section");
    btn.addEventListener("click", () => { setAllComments(section, !groupOpen(section.id)); markToggles(); });
    section.addEventListener("toggle", (ev) => {
      if (ev.target.matches("details[data-key]")) { rememberUnanimous(section.id, commentBoxes(section)); markToggles(); }
    }, true);
  }
  $("sections-all").addEventListener("click", foldSections);
  $("comments-all").addEventListener("click", () => {
    const open = !groupOpen("all-comments");
    for (const section of document.querySelectorAll(".col > section")) setAllComments(section, open);
    rememberGroup("all-comments", open);
    markToggles();
  });
  $("jump-to").addEventListener("change", (ev) => {
    const target = $(ev.target.value);
    if (target) { reveal(target); target.scrollIntoView({ block: "start" }); }
  });
  document.querySelector(".col").addEventListener("toggle", (ev) => {
    if (!sectionBoxes().includes(ev.target)) return;
    rememberUnanimous("sections", sectionBoxes());
    if (ev.target.open) render();
    else markToggles();
  }, true);
  $("outline").addEventListener("toggle", (ev) => {
    if (ev.target.matches("details.fold")) { rememberUnanimous("outline", outlineBoxes()); markToggles(); }
  }, true);
  render();
  wireTabs();
  wireRail();
  $("main-content").addEventListener("scroll", markOutline, { passive: true });
  loadMetadata();
  connectEvents();
  loaded.then(() => { if (location.hash.startsWith("#item-")) openHash(); });
  setInterval(refreshQuota, QUOTA_REFRESH_MS);
  setInterval(renderStats, 30000);
  setInterval(renderSync, 1000);
}

wireSwarm();
window.addEventListener("pagehide", () => flush(true));
document.addEventListener("visibilitychange", () => { if (document.hidden) flush(true); });
start();
