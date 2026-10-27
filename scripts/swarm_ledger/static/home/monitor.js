import { readMonitor } from "../js/api.js";

const EVERY_MS = 10000;
const GIB = 1024;
const ROWS = [
  ["swarms", "Swarms"], ["agents", "Agents"], ["claude", "Claude", "sub"], ["codex", "Codex", "sub end"],
  ["hives", "Hives"], ["cpu", "CPU", "gauge"], ["memory", "Memory", "gauge"],
];

function h(tag, attrs, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) el.setAttribute(k, v);
  el.append(...kids);
  return el;
}

function share(used, total) {
  return total > 0 ? Math.min(used / total, 1) : 0;
}

function level(part) {
  return part >= 0.9 ? "high" : part >= 0.75 ? "warm" : "ok";
}

function gauge(row, detail, part) {
  row.querySelector("dd").textContent = `${Math.round(part * 100)}%`;
  row.querySelector(".detail").textContent = detail;
  row.style.setProperty("--part", part.toFixed(3));
  row.dataset.level = level(part);
  row.querySelector(".bar").setAttribute("aria-valuenow", String(Math.round(part * 100)));
}

function paint(box, d) {
  const cell = (key) => box.querySelector(`[data-key="${key}"]`);
  const set = (key, text) => { cell(key).querySelector("dd").textContent = text; };
  set("agents", String(d.agents.total));
  set("claude", String(d.agents.claude));
  set("codex", String(d.agents.codex));
  set("hives", String(d.hives.live));
  hiveRows(cell("hives"), d.hives.rows);
  const silent = d.agents.of - d.agents.reporting, where = d.agents.source === "herdr" ? "herdr machine" : "live hive";
  cell("agents").dataset.silent = silent ? "yes" : "no";
  cell("agents").title = silent
    ? `${silent} of ${d.agents.of} ${where}s did not answer; their agents are not counted`
    : d.agents.source === "herdr"
      ? `Agent panes on the main herdr server and its ${d.agents.of - 1} plugged machine${d.agents.of === 2 ? "" : "s"}`
      : `Agent processes on ${d.agents.of} live hive${d.agents.of === 1 ? "" : "s"}`;
  set("swarms", `${d.swarms.running} running`);
  cell("swarms").dataset.on = d.swarms.running ? "yes" : "no";
  gauge(cell("cpu"), `${d.cpu.used.toFixed(1)} of ${d.cpu.cores} cores`, share(d.cpu.used, d.cpu.cores));
  const gb = (mb) => (mb / GIB).toFixed(1);
  gauge(cell("memory"), `${gb(d.memory.used_mb)} of ${gb(d.memory.total_mb)} GB`, share(d.memory.used_mb, d.memory.total_mb));
  const partial = d.hives.measured < d.hives.live;
  cell("cpu").title = cell("memory").title = partial
    ? `${d.hives.measured} of ${d.hives.live} hives reported their load; the rest are not counted`
    : `Summed across ${d.hives.live} live hive${d.hives.live === 1 ? "" : "s"}, five minute load`;
}

function hiveRows(head, rows) {
  for (const old of head.parentElement.querySelectorAll(".fm[data-hive]")) old.remove();
  const made = rows.map((row, i) => {
    const el = h("div", { class: `fm sub${i === rows.length - 1 ? " end" : ""}`, "data-hive": row.name, "data-silent": row.agents === null ? "yes" : "no" },
      h("dt", {}, row.name), h("dd", {}, row.agents === null ? "—" : String(row.agents)));
    if (row.agents === null) el.title = `${row.name} did not answer; its agents are not counted`;
    return el;
  });
  head.after(...made);
}

export function monitor() {
  const list = h("dl", {});
  for (const [key, label, kind] of ROWS) {
    const row = h("div", { class: `fm${kind ? ` ${kind}` : ""}`, "data-key": key }, h("dt", {}, label), h("dd", {}, "—"));
    if (kind === "gauge") row.append(h("span", { class: "detail" }, "—"), h("span", { class: "bar", role: "meter", "aria-label": `${label} in use`, "aria-valuemin": "0", "aria-valuemax": "100" }));
    list.append(row);
  }
  const box = h("aside", { class: "monitor", "aria-labelledby": "monitor-title" },
    h("div", { class: "monitor-head" }, h("h2", { id: "monitor-title" }, "Resource Monitor")), list);
  const read = async () => {
    try {
      const resp = await readMonitor();
      if (!resp.ok) throw new Error(String(resp.status));
      paint(box, (await resp.json()).data);
      box.dataset.stale = "no";
      box.removeAttribute("title");
    } catch (error) {
      box.dataset.stale = "yes";
      box.title = `The resources could not be read (${error.message}); the figures are the last ones read`;
    }
  };
  read();
  setInterval(read, EVERY_MS);
  return box;
}
