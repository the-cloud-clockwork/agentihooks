import { CHAT_SEEN, SWARM } from "./config.js";
import { $, grow, h, newId, when } from "./dom.js";
import { doc, queue } from "./sync.js";
import { attachable, attachmentsView, refreshTray, withAttachments } from "./media.js";
import { entryBody, wireKeys } from "./threads.js";
import { lastPage, limit, moreButton } from "./pages.js";

let chatSeen = 0;

export function renderChat() {
  $("chat-title").textContent = `Chat · ${doc.orchestrator || "swarm"}`;
  const log = $("chat-log");
  const list = doc.chat.filter((e) => !e.deleted);
  $("chat-clear").hidden = !list.length;
  if ($("chat-panel").hidden) {
    delete log.dataset.sig;
    return log.replaceChildren();
  }
  const page = lastPage("chat", list, () => null);
  const sig = `${limit("chat")}:` + page.map((e) => `${e.id}${e.sending ? "~" : ""}`).join();
  if (log.dataset.sig === sig) return;
  const stick = log.dataset.sig === undefined || log.scrollHeight - log.scrollTop - log.clientHeight < 48;
  log.dataset.sig = sig;
  log.replaceChildren(moreButton("chat", list.length, "older messages", renderChat, "div") || "", ...page.map((e) => h("div", { class: "entry" + (e.by === "operator" ? " mine" : "") + (e.sending ? " sending" : "") },
    h("div", { class: "entry-head" }, h("span", { class: "who", text: chatWho(e) }), h("span", { text: when(e.at) })),
    entryBody(e.text, new Set(doc.tasks.map((t) => t.id))), attachmentsView(e.attachments))));
  if (!list.length) log.append(h("div", { class: "empty", text: "No messages yet." }));
  if (stick) {
    log.scrollTop = log.scrollHeight;
    requestAnimationFrame(() => { log.scrollTop = log.scrollHeight; });
  }
}

function unreadCount(chat, seenAt) {
  return chat.filter((e) => !e.deleted && e.by !== "operator" && (e.at || 0) > seenAt).length;
}

function lastChatAt() {
  return Math.max(0, ...doc.chat.map((e) => e.at || 0));
}

export function renderChatBadge() {
  if (!$("chat-panel").hidden && lastChatAt() > chatSeen) {
    chatSeen = lastChatAt();
    try { localStorage.setItem(CHAT_SEEN, String(chatSeen)); } catch (e) { /* storage blocked: count resets on reload */ }
  }
  const n = unreadCount(doc.chat, chatSeen);
  $("chat-badge").hidden = !n;
  $("chat-badge").textContent = String(n);
}

export function showChat(open) {
  $("chat-panel").hidden = !open;
  $("chat-fab").setAttribute("aria-expanded", String(open));
  if (!open) return renderChat();
  renderChat();
  renderChatBadge();
  const log = $("chat-log");
  log.scrollTop = log.scrollHeight;
  $("chat-input").focus();
}

export function renderChatTo(sw) {
  const menu = $("chat-to-menu");
  if (!menu.hidden) return;
  const live = ((sw && sw.agents) || []).filter((a) => a.lane !== "master" && a.state !== "finished").map((a) => a.name);
  const names = ["", SWARM, ...live];
  const keep = names.includes($("chat-to").value) ? $("chat-to").value : "";
  menu.replaceChildren(...names.map((name) => h("button", { type: "button", role: "option", "data-to": name, text: name || "master" })));
  setChatTo(keep);
}

export function setChatTo(name) {
  $("chat-to").value = name;
  $("chat-to-pick").textContent = name || "master";
  for (const row of $("chat-to-menu").children) row.setAttribute("aria-selected", String(row.dataset.to === name));
}

function showChatTo(open) {
  const menu = $("chat-to-menu");
  menu.hidden = !open;
  $("chat-to-pick").setAttribute("aria-expanded", String(open));
  if (open) (menu.querySelector('[aria-selected="true"]') || menu.firstElementChild).focus();
}

function chatWho(e) {
  if (e.by !== "operator") return e.by;
  return e.text.startsWith(`@${SWARM} `) ? "You to the whole swarm" : "You";
}

function chatText(text, to) {
  return to && !text.startsWith("@") ? `@${to} ${text}` : text;
}

function sendChat(text) {
  const box = $("chat-input");
  box.value = "";
  grow(box);
  queue(withAttachments({ op: "add", thread: "chat", id: newId("message"), text: chatText(text, $("chat-to").value) }, "chat"));
  refreshTray("chat");
}

export function wireChat() {
  try {
    chatSeen = Number(localStorage.getItem(CHAT_SEEN)) || lastChatAt();
    localStorage.setItem(CHAT_SEEN, String(chatSeen));
  } catch (e) { chatSeen = lastChatAt(); }
  $("chat-fab").addEventListener("click", () => showChat($("chat-panel").hidden));
  $("chat-close").addEventListener("click", () => showChat(false));
  $("chat-to-pick").addEventListener("click", () => showChatTo($("chat-to-menu").hidden));
  $("chat-to-menu").addEventListener("click", (ev) => {
    const row = ev.target.closest("button[data-to]");
    if (!row) return;
    setChatTo(row.dataset.to);
    showChatTo(false);
    $("chat-input").focus();
  });
  $("chat-to-menu").addEventListener("keydown", (ev) => {
    const rows = [...$("chat-to-menu").children];
    const at = rows.indexOf(document.activeElement);
    if (ev.key === "ArrowDown" || ev.key === "ArrowUp") {
      ev.preventDefault();
      rows[(at + (ev.key === "ArrowDown" ? 1 : rows.length - 1)) % rows.length].focus();
    } else if (ev.key === "Escape") {
      ev.preventDefault();
      showChatTo(false);
      $("chat-to-pick").focus();
    }
  });
  document.addEventListener("click", (ev) => {
    if (!$("chat-to-menu").hidden && !ev.target.closest(".chat-to")) showChatTo(false);
  });
  $("chat-size").addEventListener("click", () => {
    const big = $("chat-panel").classList.toggle("big");
    $("chat-size").textContent = big ? "Shrink" : "Expand";
    $("chat-log").scrollTop = $("chat-log").scrollHeight;
  });
}

export function wireChatInput() {
  const input = $("chat-input");
  $("chat-clear").addEventListener("click", () => {
    if (confirm("Clear the whole chat?")) queue({ op: "clear", thread: "chat", id: newId("clear") });
  });
  input.addEventListener("input", () => grow(input));
  wireKeys(input, sendChat, () => showChat(false), "chat");
  input.after(attachable("chat", input, input.closest(".chat-compose")));
}
