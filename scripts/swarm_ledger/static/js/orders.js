import { $, h } from "./dom.js";
import { readOrders } from "./api.js";

let loading = false;
let pending = false;

function orderRow(order) {
  return h("li", { class: "item" },
    h("div", { class: "item-title", text: order.title }),
    order.quote ? h("div", { class: "his-words" }, h("blockquote", { text: order.quote })) : null,
    h("div", { class: "task-meta" },
      h("span", { text: `${order.claimable} claimable` }),
      h("span", { text: `${order.busy} busy` }),
      h("span", { style: "color: var(--positive)", text: `${order.done.length} done` })));
}

export async function loadOrders() {
  if (loading) { pending = true; return; }
  loading = true;
  try {
    const resp = await readOrders();
    if (!resp.ok) throw new Error();
    const { data } = await resp.json();
    $("orders").replaceChildren(...(data.length ? data.map(orderRow) : [h("li", { class: "empty", text: "No open operator orders." })]));
    $("orders-count").textContent = `· ${data.length} open`;
    $("orders-status").textContent = "";
  } catch {
    $("orders-status").textContent = "Operator orders unavailable.";
  } finally {
    loading = false;
    if (pending) { pending = false; loadOrders(); }
  }
}
