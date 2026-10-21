import { h } from "./dom.js";

function agentCellTip(node) {
  if (node.nodeType === Node.TEXT_NODE) return node.textContent.trim();
  if (node.matches(".sw-transport, .sw-promoted")) return node.dataset.tip;
  return [...node.childNodes].map(agentCellTip).filter(Boolean).join(" ").trim();
}

export function agentCellTips(row) {
  for (const cell of row.cells) {
    if (cell.querySelector(".sw-acts")) continue;
    const tip = agentCellTip(cell);
    cell.dataset.tip = tip;
    for (const el of cell.querySelectorAll("[data-tip], [title]")) {
      el.dataset.tip = tip;
    }
  }
  return row;
}

export function joinCell(report) {
  if (!report || !report.grade) return "";
  const tone = { complete: "live", partial: "waiting", failed: "stalled" }[report.grade];
  return h("span", { class: "sw-sub" }, h("span", { class: `lbl ${tone}`, text: report.grade }), ...Object.entries(report.misses || {}).map(([name, miss]) =>
    h("span", { class: "sw-sub", "data-tip": `${name.replace(/_/g, " ")} missing (${miss.required ? "required" : "optional"})`, text: name.replace(/_/g, " ") })));
}
