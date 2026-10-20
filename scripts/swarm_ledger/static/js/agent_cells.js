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
