import { $, h } from "./dom.js";
import { readArchitecture, writeArchitecture } from "./api.js";
import { label } from "./swarm.js";

const ARCH_VERB = { approve: "approve", rollback: "roll back" };

function recordCells(...values) {
  return h("tr", {}, ...values.map((v) => (v && v.nodeType ? h("td", {}, v) : h("td", { text: v || "—" }))));
}

function recordEmpty(text) {
  return h("tr", { class: "sw-empty" }, h("td", { colspan: "5", text }));
}

function proposalRow(p) {
  if (p.approved) return recordCells(p.name, "proposal", label("approved"), "", "");
  const reason = h("input", { class: "hl-note", type: "text", maxlength: "500", placeholder: "reason", "data-reason": p.id, "aria-label": `Reason to approve ${p.name}` });
  const approve = h("button", { class: "sw-btn ok", type: "button", "data-architecture": "approve", disabled: true, text: ARCH_VERB.approve,
    on: { click: () => sendArchitecture("approve", { action: "approve", proposal: p.id, reason: reason.value.trim() }) } });
  reason.addEventListener("input", () => { approve.disabled = !reason.value.trim(); });
  return recordCells(p.name, "proposal", label("awaiting approval"), reason, approve);
}

function revisionRow(r) {
  return recordCells(`revision ${r.revision}`, "revision", r.operation || "initial record", "",
    h("button", { class: "sw-btn danger", type: "button", "data-architecture": "rollback", text: ARCH_VERB.rollback,
      on: { click: () => sendArchitecture("rollback", { action: "rollback", to: r.revision }) } }));
}

function renderArchitecture(view) {
  $("architecture-count").textContent = `revision ${view.revision}`;
  const rows = [...view.proposals.map(proposalRow), ...view.revisions.map(revisionRow)];
  $("swarm-architecture").replaceChildren(...(rows.length ? rows : [recordEmpty("No proposals or earlier revisions.")]));
}

async function architectureRefusal(resp) {
  try {
    return (await resp.json()).error.message;
  } catch {
    return `server answered ${resp.status}`;
  }
}

async function sendArchitecture(action, body) {
  $("architecture-status").textContent = "";
  for (const btn of $("swarm-architecture").querySelectorAll("button")) btn.disabled = true;
  let why = "ledger server unreachable";
  try {
    const resp = await writeArchitecture(body);
    if (resp.ok) return renderArchitecture((await resp.json()).data);
    why = await architectureRefusal(resp);
  } catch {}
  $("architecture-status").textContent = `Could not ${ARCH_VERB[action]}: ${why}.`;
  loadArchitecture();
}

export async function loadArchitecture() {
  try {
    const resp = await readArchitecture();
    if (resp.ok) return renderArchitecture((await resp.json()).data);
  } catch {}
  $("swarm-architecture").replaceChildren(recordEmpty("Architecture record unavailable."));
}
