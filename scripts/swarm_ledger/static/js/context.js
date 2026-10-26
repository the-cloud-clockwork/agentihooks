import { $, h } from "./dom.js";
import { readContextEvidence } from "./api.js";
import { cells, emptyRow, idCell, label, withId } from "./swarm.js";

const opened = new Map();

export function contextCards(sw) {
  return (sw && sw.context && sw.context.cards) || [];
}

function evidenceLines(e) {
  if (e.records) return [`records ${e.first} to ${e.last} of session ${e.session}`, ...e.records];
  return [`revision ${e.revision}`, e.text];
}

function evidence(key) {
  const found = opened.get(key);
  if (!found) return null;
  const texts = found.error ? [`unavailable: ${found.error}`] : evidenceLines(found.data);
  return h("div", { class: "sw-lines ctx-evidence" }, ...texts.map((text) => h("span", { text, title: text })));
}

async function expand(card, item, sw) {
  const key = `${card.task}|${item.ref}`;
  const reply = await readContextEvidence(card.task, item.ref);
  const body = await reply.json().catch(() => ({}));
  opened.set(key, reply.ok ? { data: body.data } : { error: (body.error && body.error.message) || `HTTP ${reply.status}` });
  renderContext(sw);
}

function itemLine(card, item, sw) {
  const key = `${card.task}|${item.ref}`;
  if (item.unavailable) opened.delete(key);
  const tags = item.labels.map((text) => label(text));
  const text = item.unavailable ? `unavailable: ${item.unavailable}` : item.summary;
  const open = item.ref && !item.unavailable && h("button", { class: "sw-btn", type: "button", "data-context-ref": item.ref,
    "aria-label": `Show the evidence behind ${item.ref}`, text: opened.has(key) ? "refresh" : "evidence", on: { click: () => expand(card, item, sw) } });
  return h("div", { class: "ctx-item" }, h("span", { class: "sw-acts" }, label(item.kind, "kind"), ...tags, open),
    h("span", { class: item.unavailable ? "ctx-text unavailable" : "ctx-text", text, title: text }), evidence(key));
}

function receipt(card) {
  return h("span", { class: "sw-acts", title: card.receipt || card.receipt_state }, label(card.receipt_state));
}

export function renderContext(sw) {
  const cards = contextCards(sw);
  $("context-count").textContent = cards.length ? `${cards.length} ${cards.length === 1 ? "task" : "tasks"}` : "none delivered";
  $("swarm-context").replaceChildren(...(cards.length
    ? cards.map((card) => withId(cells(idCell(card.task), card.brain, card.purpose, receipt(card),
      h("div", { class: "sw-lines" }, ...card.delivery.map((text) => h("span", { text }))),
      h("div", { class: "ctx-items" }, ...card.items.map((item) => itemLine(card, item, sw)),
        card.omitted.length ? h("span", { class: "ctx-text", text: `${card.omitted.length} omitted: ${card.omitted.join(", ")}` }) : null)),
    `context-${card.task}`))
    : [emptyRow(6, "No task has been given brain context yet.")]));
}
