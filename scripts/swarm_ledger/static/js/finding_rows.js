import { $, clock, h } from "./dom.js";

function findingRow(row) {
  const mark = { doctrine: "required by doctrine", task: "required by the task" }[row.required];
  const text = [clock(row.at), row.kind, row.target, row.text, mark].filter(Boolean).join(" · ");
  return h("li", { class: mark ? "sw-required" : false, text });
}

function findingRows(f) {
  const total = f.rows_total || 0, shown = (f.rows || []).length;
  const count = !total ? "no ledger rows" : total > shown ? `latest ${shown} of ${total} rows` : `${total} rows`;
  const head = [`${f.kind} ${f.subject}`, `last outcome ${f.last_outcome}`, count].join(" · ");
  return h("li", {}, h("span", { text: head }), h("ul", {}, ...(f.rows || []).map(findingRow)));
}

export function renderFindingRows(open) {
  const shown = document.getElementById("alert-findings"), attached = open.filter((f) => "last_outcome" in f);
  if (!attached.length) return shown && shown.remove();
  const list = shown || $("swarm-alert").appendChild(h("ul", { class: "sw-findings", id: "alert-findings" }));
  list.replaceChildren(...attached.map(findingRows));
}
