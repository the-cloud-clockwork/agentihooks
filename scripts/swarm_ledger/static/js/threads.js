import { COMMENTS_KEY, NOUN } from "./config.js";
import { grow, h, newId, store, stored, when } from "./dom.js";
import { queue } from "./sync.js";
import { attachable, attaching, attachmentsView, withAttachments } from "./media.js";
import { render } from "./render.js";
import { jumpTo } from "./outline.js";
import { toggles } from "./folds.js";
import { lastPage, lazy, moreButton } from "./pages.js";

export const composing = {};
export const editing = {};
const savedComments = stored(COMMENTS_KEY, {});
export const openComments = new Set(savedComments.opened || []);
export const closedComments = new Set(savedComments.closed || []);

export function wireKeys(box, onSubmit, onCancel, key) {
  box.addEventListener("keydown", (ev) => {
    if (ev.key === "Enter" && (ev.ctrlKey || ev.metaKey)) {
      ev.preventDefault();
      box.setRangeText("\n", box.selectionStart, box.selectionEnd, "end");
      box.dispatchEvent(new Event("input", { bubbles: true }));
    } else if (ev.key === "Enter" && !ev.shiftKey && !ev.isComposing) {
      ev.preventDefault();
      if (box.value.trim() || (attaching[key] || []).length) onSubmit(box.value.trim());
    }
    else if (ev.key === "Escape") { ev.preventDefault(); onCancel(); }
  });
}

function lineBox(key, value, placeholder, onSubmit, onCancel) {
  const box = h("textarea", { class: "line", rows: "1", placeholder, "data-focus": key, "aria-label": placeholder });
  box.value = value;
  box.addEventListener("input", () => { grow(box); (key.startsWith("edit:") ? editing : composing)[key.slice(key.indexOf(":") + 1)] = box.value; });
  wireKeys(box, onSubmit, onCancel, key.slice(key.indexOf(":") + 1));
  return h("div", {}, box, h("div", { class: "hint", text: "Enter to send · Ctrl+Enter for a new line · Esc to cancel" }));
}

export function entryBody(text, ids = new Set()) {
  const body = h("div", { class: "entry-body" });
  let offset = 0;
  for (const match of text.matchAll(/\bhttps?:\/\/[^\s<>"']+/gi)) {
    let url = match[0].replace(/[.,;:!?]+$/, "");
    for (const [open, close] of [["(", ")"], ["[", "]"], ["{", "}"]]) {
      while (url.endsWith(close) && url.split(close).length > url.split(open).length) url = url.slice(0, -1);
    }
    body.append(...taskMentions(text.slice(offset, match.index), ids), h("a", { href: url, target: "_blank", rel: "noopener noreferrer", text: url }), match[0].slice(url.length));
    offset = match.index + match[0].length;
  }
  body.append(...taskMentions(text.slice(offset), ids));
  return body;
}

function relayMark(entry) {
  return entry.relayed_by ? h("span", { text: `relayed from the ${entry.relayed_from} by ${entry.relayed_by}` }) : null;
}

function hisWords(entry) {
  return entry.relayed_by && entry.quote ? h("div", { class: "his-words" }, h("span", { class: "label", text: "His words" }), h("blockquote", { text: entry.quote })) : null;
}

function entryView(path, entry, noun) {
  const mine = entry.by === "operator";
  if (entry.id in editing) {
    const done = () => { delete editing[entry.id]; render(); };
    return h("div", { class: "entry mine" }, lineBox(`edit:${entry.id}`, editing[entry.id], `Edit ${noun}`,
      (text) => { delete editing[entry.id]; if (text !== entry.text) queue({ op: "edit", thread: path, id: entry.id, text }); else render(); }, done));
  }
  const actions = h("span", { class: "entry-actions" },
    h("button", { class: "link", type: "button", text: "Edit", on: { click: () => { editing[entry.id] = entry.text; render(`edit:${entry.id}`); } } }),
    h("button", { class: "link danger", type: "button", text: "Delete", on: { click: () => {
      if (confirm(`Delete this ${noun}?`)) queue({ op: "delete", thread: path, id: entry.id });
    } } }));
  const head = h("div", { class: "entry-head" }, h("span", { class: "who", text: mine ? "You" : entry.by === "earlier" ? "earlier notes" : entry.by }),
    relayMark(entry), h("span", { text: when(entry.at) }), entry.edited_at ? h("span", { text: "edited" }) : null, entry.sending ? h("span", { text: "sending" }) : null, actions);
  return h("div", { class: "entry" + (mine ? " mine" : "") + (entry.sending ? " sending" : ""), id: path === "notes" ? `item-notes-${entry.id}` : false }, head, entryBody(entry.text), hisWords(entry), attachmentsView(entry.attachments),
    path === "notes" ? commentsView(`notes/${entry.id}`, entry.comments || []) : null);
}

export function threadView(path, list, cls) {
  const noun = NOUN[path.split("/").pop()];
  const box = h("div", { class: cls });
  const live = list.filter((e) => !e.deleted);
  const older = moreButton(path, live.length, `older ${noun}s`, render, "div");
  if (older) box.append(older);
  for (const entry of lastPage(path, live, (e) => `item-notes-${e.id}`)) box.append(entryView(path, entry, noun));
  if (path in composing) {
    const close = () => { delete composing[path]; render(); };
    const line = lineBox(`compose:${path}`, composing[path], `Write a ${noun}…`, (text) => {
      delete composing[path];
      queue(withAttachments({ op: "add", thread: path, id: newId(noun), text }, path));
    }, close);
    if (path.endsWith("/comments")) line.insertBefore(attachable(path, line.firstChild, line), line.lastChild);
    box.append(line);
  } else {
    box.append(h("button", { class: "add", type: "button", text: `Add ${noun}`, on: { click: () => { composing[path] = ""; render(`compose:${path}`); } } }));
  }
  return box;
}

export function commentsView(key, list) {
  const count = list.filter((e) => !e.deleted).length;
  const box = h("details", { "data-key": key }, h("summary", {}, `Comments`, h("span", { class: "count", text: count ? `· ${count}` : "" })));
  const shown = toggles[`sec-${key.split("/")[0]}`];
  box.open = openComments.has(key) || (!closedComments.has(key) && shown === true);
  box.addEventListener("toggle", () => rememberComment(key, box.open));
  return lazy(box, () => threadView(`${key}/comments`, list, "thread flat"));
}

export function rememberComment(key, open) {
  (open ? openComments : closedComments).add(key);
  (open ? closedComments : openComments).delete(key);
  store(COMMENTS_KEY, { opened: [...openComments], closed: [...closedComments] });
}

export function taskLink(id) {
  return h("a", { class: "task-id", href: `#item-tasks-${id}`, text: id, on: { click: (ev) => jumpTo(ev, `tasks/${id}`) } });
}

function taskMentions(text, ids) {
  const out = [];
  let offset = 0;
  for (const match of text.matchAll(/\w+/g)) {
    if (!ids.has(match[0])) continue;
    out.push(text.slice(offset, match.index), taskLink(match[0]));
    offset = match.index + match[0].length;
  }
  out.push(text.slice(offset));
  return out;
}
