import { $, h, headCount, newId, noticeTime } from "./dom.js";
import { doc, queue } from "./sync.js";
import { verdictButton } from "./render.js";
import { jumpTo } from "./outline.js";
import { foldSaved } from "./folds.js";
import { showChat } from "./chat.js";
import { firstPage, moreButton, wanted } from "./pages.js";

export function anchorPanel(panel, icon) {
  panel.style.setProperty("--anchor-top", `${icon.getBoundingClientRect().top}px`);
  panel.style.setProperty("--anchor-left", `${$("icon-strip").getBoundingClientRect().right}px`);
}

export function clearPriority(target) {
  queue({ op: "priority_clear", id: newId("clear"), target });
}

const PRIORITY_KINDS = { tasks: ["title", "task"], phases: ["title", "phase"], questions: ["text", "question"], followups: ["text", "follow up"] };

function prioritySubject(item) {
  const [list, id] = item.split("/");
  const [field, noun] = PRIORITY_KINDS[list];
  const found = doc[list].find((i) => i.id === id && !i.deleted);
  const label = field === "title" ? id : noun;
  return found ? { label, title: found[field] } : { label, title: `This ${noun} was removed`, gone: true };
}

export function renderPriorities() {
  const list = doc.priorities;
  $("prio-count").textContent = headCount([[list.length]]);
  $("tab-ledger").textContent = list.length ? `Ledger · ${list.length}` : "Ledger";
  if (list.length && typeof foldSaved()["prio-box"] !== "boolean") $("prio-box").open = true;
  document.querySelector(".prio-head").hidden = !list.length;
  if (!$("prio-box").open) return $("priorities").replaceChildren();
  $("priorities").replaceChildren(...firstPage("priorities", list, (p) => `item-priorities-${p.id}`).map((p) => {
    const { label, title, gone } = prioritySubject(p.item);
    const link = (cls, text) => h("a", { class: cls, href: `#item-${p.item.replace("/", "-")}`, text, on: { click: (ev) => jumpTo(ev, p.item) } });
    return h("li", { class: "prio", id: `item-priorities-${p.id}` },
      h("div", { class: "prio-body" },
        h("div", { class: "prio-head-line" }, link("prio-link", label), link(gone ? "prio-title gone" : "prio-title", title)),
        h("div", { class: "prio-text", text: p.text })),
      verdictButton(p.item, "approved", "approve", "Approve"),
      h("button", { class: "link danger", type: "button", text: "Clear", on: { click: () => clearPriority(p.id) } }));
  }), moreButton("priorities", list.length, "more priorities", renderPriorities) || "");
  if (!list.length) $("priorities").append(h("li", { class: "empty", text: "Nothing waits on you." }));
}

function jumpToNotice(ev, item) {
  if (item !== "chat") return jumpTo(ev, item);
  ev.preventDefault();
  showChat(true);
}

export function noticeTarget(item) {
  return item === "chat" ? "chat-log" : `item-${item.replace("/", "-")}`;
}

export function clearNotification(target) {
  queue({ op: "notification_clear", id: newId("notice"), target });
}

export function renderNotifications() {
  const list = [...doc.notifications].reverse();
  $("bell-badge").hidden = !list.length;
  $("bell-badge").textContent = String(list.length);
  $("notif-clear-all").hidden = !list.length;
  if ($("notif").hidden) return $("notifs").replaceChildren();
  $("notifs").replaceChildren(...firstPage("notifs", list, (n) => `notice-${n.id}`).map((n) => h("li", { class: "notif-row", id: `notice-${n.id}` },
    h("div", { class: "notif-meta" },
      h("span", { text: noticeTime(n.at) }),
      n.item ? h("a", { class: "prio-link", href: `#${noticeTarget(n.item)}`,
        text: n.item === "chat" ? "#chat" : `#${n.item.split("/")[1]}`, on: { click: (ev) => jumpToNotice(ev, n.item) } }) : "",
      h("span", { text: n.label === "Reply" ? `Reply from ${n.by}` : n.label }),
      h("button", { class: "link danger", type: "button", text: "Clear", on: { click: () => clearNotification(n.id) } })),
    h("div", { class: "notif-text", text: n.text, title: n.item ? "Go to it" : "",
      on: { click: (ev) => { if (n.item && !getSelection().toString()) jumpToNotice(ev, n.item); } } }))),
    moreButton("notifs", list.length, "more notifications", renderNotifications) || "");
  if (!list.length) $("notifs").append(h("li", { class: "empty", text: "No notifications." }));
}

const ALERT_SOURCES = { size: "Size limit", sync: "Refused write" };
const alertDraft = { id: "", text: "" };

function alertRow(a) {
  const outcome = h("input", { class: "line", type: "text", maxlength: "2000", placeholder: "Outcome…", "aria-label": "Outcome", value: alertDraft.id === a.id ? alertDraft.text : "",
    on: { input: (ev) => { alertDraft.text = ev.target.value; } } });
  const form = h("form", { class: "alert-outcome", hidden: alertDraft.id !== a.id && "",
    on: { submit: (ev) => {
      ev.preventDefault();
      if (!outcome.value.trim()) return outcome.focus();
      Object.assign(alertDraft, { id: "", text: "" });
      queue({ op: "alert_close", id: newId("alert"), target: a.id, outcome: outcome.value.trim() });
    } } },
    outcome, h("button", { class: "link", type: "submit", text: "Save" }));
  const openForm = () => { Object.assign(alertDraft, { id: a.id, text: "" }); form.hidden = false; outcome.value = ""; outcome.focus(); };
  return h("li", { class: "notif-row", id: `alert-${a.id}` },
    h("div", { class: "notif-meta" },
      h("span", { text: noticeTime(a.at) }),
      h("span", { text: ALERT_SOURCES[a.source] || a.source }),
      h("span", { text: `For the ${a.target}` }),
      h("span", { class: "alert-state", text: a.state === "claimed" ? `Claimed by ${a.claimed_by}` : "Open" }),
      h("span", { class: "alert-acts" },
        a.state === "open" && h("button", { class: "link alert-claim", type: "button", text: "Claim", on: { click: () => queue({ op: "alert_claim", id: newId("alert"), target: a.id }) } }),
        h("button", { class: "link alert-done", type: "button", text: "Mark done", on: { click: openForm } }))),
    h("div", { class: "notif-text", text: a.text }), form);
}

export function renderAlerts() {
  const list = doc.alerts.filter((a) => a.state !== "done").reverse();
  $("alert-badge").hidden = !list.length;
  $("alert-badge").textContent = String(list.length);
  if ($("alert-panel").hidden) return $("alerts").replaceChildren();
  if (!wanted.id && document.activeElement.matches("#alerts input")) return;
  $("alerts").replaceChildren(...firstPage("alerts", list, (a) => `alert-${a.id}`).map(alertRow), moreButton("alerts", list.length, "more alerts", renderAlerts) || "");
  if (!list.length) $("alerts").append(h("li", { class: "empty", text: "No open alerts." }));
}

export function showAlerts(open) {
  $("alert-panel").hidden = !open;
  $("alert-fab").setAttribute("aria-expanded", String(open));
  renderAlerts();
  if (open) anchorPanel($("alert-panel"), $("alert-fab"));
}

export function showNotifications(open) {
  $("notif").hidden = !open;
  $("bell").setAttribute("aria-expanded", String(open));
  renderNotifications();
  if (open) anchorPanel($("notif"), $("bell"));
}
