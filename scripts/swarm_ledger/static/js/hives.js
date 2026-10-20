import { $, age, h } from "./dom.js";
import { readHives, writeHive } from "./api.js";
import { showNote } from "./controls.js";
import { label, pending, renderQuota, swarm, swarmControl } from "./swarm.js";

const HIVES_READ_MS = 15000;
let hives = null, hivesFailed = false, hivesRead = null, hivesAt = -Infinity;

function loadHives() {
  if (hivesRead || Date.now() - hivesAt < HIVES_READ_MS) return;
  hivesAt = Date.now();
  hivesRead = readHives().then((resp) => (resp.ok ? resp.json() : Promise.reject(resp.status)))
    .then((reply) => { hives = reply.data; hivesFailed = false; renderHives(true); renderQuota(swarm, Date.now()); })
    .catch(() => { hivesFailed = true; renderHives(true); })
    .finally(() => { hivesRead = null; });
}

export function quotaCount(sw, count, now) {
  const at = (sw.quota || {}).probed_at, accounts = `${count} ${count === 1 ? "account" : "accounts"}`;
  const counts = (hives || []).map((hive) => `${hive.name || hive.id} ${hive.agents}/${hive.max_agents}`).join(" · ");
  return [accounts, at ? `probed ${age(now - at * 1000)} ago` : "", counts].filter(Boolean).join(" · ");
}

function flag(hive, field) {
  const pick = h("select", { class: "hl-pick", "data-hive": hive.id, "data-hive-field": field, "aria-label": `${hive.id} ${field}` },
    ...["yes", "no"].map((value) => h("option", { value, text: value })));
  pick.value = hive[field];
  return pick;
}

function text(hive, field, value, width) {
  const input = h("input", { class: width ? "sw-num" : "hl-note", type: "text", "data-hive": hive.id, "data-hive-field": field,
    "aria-label": `${hive.id} ${field.replace("_", " ")}`, ...(width ? { inputmode: "numeric" } : {}) });
  input.value = value;
  return input;
}

function preferText(prefer) {
  return Object.entries(prefer || {}).map(([role, rank]) => `${role}:${rank}`).join(",");
}

function slots(hive) {
  return h("div", { class: "hv-slots" }, ...(hive.slots || []).map((slot) =>
    h("div", { text: `${slot.harness} ${slot.account} ${slot.agents}` })));
}

function moveButton(hive) {
  const here = hive.master && swarm && hive.master.slug === swarm.config.slug;
  return h("button", { class: "sw-btn", type: "button", "data-move-master": hive.id, disabled: !swarm || !!pending || !hive.live || !!here,
    "aria-label": `Move master to ${hive.id}`, text: "move master" });
}

function hiveRow(hive) {
  const values = [h("span", { class: "sw-id", title: hive.id, text: hive.name || hive.id }), label(hive.live ? "live" : "quiet", hive.live ? "live" : "stalled"),
    flag(hive, "ui"), flag(hive, "ephemeral"), text(hive, "excluded", (hive.excluded || []).join(",")), text(hive, "prefer", preferText(hive.prefer)),
    text(hive, "max_agents", String(hive.max_agents), true), `${hive.agents}/${hive.max_agents}`, slots(hive),
    hive.master ? h("span", { class: "sw-id", title: hive.master.name, text: hive.master.name }) : "—", moveButton(hive)];
  return h("tr", { id: `hive-${hive.id}` }, ...values.map((v) => (v.nodeType ? h("td", {}, v) : h("td", { text: v }))));
}

export function renderHives(force) {
  loadHives();
  if ($("swarm").hidden || (!force && $("swarm-hives").contains(document.activeElement))) return;
  const list = hives || [];
  $("hives-count").textContent = hivesFailed ? "—" : `${list.filter((hive) => hive.live).length} live · ${list.length} ${list.length === 1 ? "hive" : "hives"}`;
  $("swarm-hives").replaceChildren(...(list.length ? list.map(hiveRow)
    : [h("tr", { class: "sw-empty" }, h("td", { colspan: "11", text: hivesFailed ? "Could not read hives." : hives ? "No hives registered." : "Reading hives…" }))]));
}

function value(input) {
  const raw = input.value.trim(), field = input.dataset.hiveField;
  if (field === "excluded") return raw.split(",").map((name) => name.trim()).filter(Boolean);
  if (field === "prefer") return Object.fromEntries(raw.split(",").filter(Boolean).map((pair) => pair.split(":")).map(([role, rank]) => [role.trim(), Number(rank)]));
  if (field === "max_agents") return /^\d+$/.test(raw) ? Number(raw) : raw;
  return raw;
}

async function saveHive(input) {
  showNote("pending", "hive");
  try {
    const resp = await writeHive(input.dataset.hive, { [input.dataset.hiveField]: value(input) });
    const reply = await resp.json().catch(() => ({}));
    if (!resp.ok) throw new Error((reply.error && reply.error.message) || `server answered ${resp.status}`);
    hives = (hives || []).map((hive) => (hive.id === reply.data.id ? reply.data : hive));
    showNote("done", "hive");
  } catch (e) {
    showNote("error", "hive", e instanceof TypeError ? "ledger server unreachable" : e.message);
  }
  renderHives(true);
  renderQuota(swarm, Date.now(), true);
}

export function wireHives() {
  $("swarm-hives").addEventListener("change", (event) => {
    const input = event.target.closest("[data-hive-field]");
    if (input) saveHive(input);
  });
  $("swarm-hives").addEventListener("click", (event) => {
    const button = event.target.closest("button[data-move-master]");
    if (button) swarmControl({ action: "master_move", hive: button.dataset.moveMaster });
  });
}
