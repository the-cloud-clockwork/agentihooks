const view = document.querySelector("main").className;
const list = document.getElementById("rows");
const FOLD_KEY = "home-fold", SORT_KEY = "home-sort";
const FIRST = { kind: 1, open: -1, done: -1, swarm: 1, at: -1 };
const SWARM_RANK = { running: 0, paused: 1, stopping: 1, drained: 2, stopped: 3, closed: 5 };
const ICONS = {
  trash: '<path d="M3 6h18"/><path d="M8 6V4h8v2"/><path d="M6 6l1 14h10l1-14"/><path d="M10 11v6M14 11v6"/>',
  restore: '<path d="M3 12a9 9 0 1 0 3-6.7L3 8"/><path d="M3 3v5h5"/>',
};

function h(tag, attrs, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (k === "text") el.textContent = v;
    else if (v !== undefined && v !== null) el.setAttribute(k, v);
  }
  for (const kid of kids) if (kid !== null && kid !== undefined) el.append(kid);
  return el;
}

function icon(name) {
  const box = document.createElement("template");
  box.innerHTML = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${ICONS[name]}</svg>`;
  return box.content.firstChild;
}

async function collection(path) {
  for (let attempt = 0; attempt < 3; attempt++) {
    const rows = [];
    let cursor = null, restart = false;
    do {
      const resp = await fetch(`/api/v1/${path}?limit=100${cursor ? `&cursor=${encodeURIComponent(cursor)}` : ""}`, { cache: "no-store" });
      if (resp.status === 409) { restart = true; break; }
      if (!resp.ok) throw new Error(`the server answered ${resp.status}`);
      const page = await resp.json();
      rows.push(...page.data);
      cursor = page.next_cursor;
    } while (cursor);
    if (!restart) return rows;
  }
  throw new Error("the list kept changing while it was read");
}

function ago(at, now) {
  const minutes = Math.floor((now - at) / 60000);
  for (const [unit, size] of [["d", 1440], ["h", 60], ["m", 1]]) if (minutes >= size) return `${Math.floor(minutes / size)}${unit} ago`;
  return "just now";
}

function stamp(at) {
  const d = new Date(at), pad = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
}

function activity(at, now) {
  if (!at) return h("span", { class: "when", text: "unknown" });
  const d = new Date(at), pad = (n) => String(n).padStart(2, "0");
  return h("time", { class: "when", datetime: d.toISOString().replace(/\.\d+Z$/, "Z"),
    "data-tip": `${stamp(at)} ${pad(d.getHours())}:${pad(d.getMinutes())}`, text: ago(at, now) });
}

function act(cls, act, slug, label, ...kids) {
  return h("button", { class: `act ${cls}`, type: "button", "data-act": act, "data-slug": slug, "aria-label": label }, ...kids);
}

function ledgerRow(s, cells, controls, lead, attrs) {
  return h("li", { class: "row", ...attrs }, lead,
    h("a", { class: "title", href: `/${s.slug}`, "data-tip": s.title, text: s.title }),
    h("span", { class: "kind", text: s.size }), h("span", { class: "ov", "data-tip": s.overview, text: s.overview }),
    ...cells, h("span", { class: "acts" }, ...controls));
}

function homeRow(s, now) {
  const state = s.closed_at ? "closed" : s.swarm;
  const cells = [h("span", { class: "num open" }, h("b", { text: String(s.open) }), " open"),
    h("span", { class: "num done" }, h("b", { text: String(s.done) }), " done"),
    h("span", { class: `state s-${state || "none"}`, text: state || "no swarm" }), activity(s.updated_at, now)];
  const controls = [s.closed_at ? act("reopen", "reopen", s.slug, `Reopen ${s.title}`, "Reopen") : null,
    act("del", "delete", s.slug, `Move ${s.title} to the bin`, icon("trash"))];
  const lead = h("button", { class: "fold", type: "button", "aria-expanded": "false", "aria-label": `Show all of ${s.title}`, text: "▸" });
  return ledgerRow(s, cells, controls, lead, { "data-slug": s.slug, "data-kind": s.size, "data-open": s.open, "data-done": s.done,
    "data-swarm": SWARM_RANK[state] ?? 4, "data-at": s.updated_at || 0 });
}

function binRow(s) {
  const cells = [h("span", { class: "deleted", text: stamp(s.deleted_at) }),
    h("span", { class: "left", text: `${s.days_left} day${s.days_left === 1 ? "" : "s"} left` })];
  return ledgerRow(s, cells, [act("restore", "restore", s.slug, `Restore ${s.title} to HOME`, icon("restore"), "Restore")], null, {});
}

async function reopenLedger(b) {
  const slug = encodeURIComponent(b.dataset.slug), page = await fetch("/" + slug);
  if (!page.ok) return page;
  const doc = new DOMParser().parseFromString(await page.text(), "text/html");
  const token = doc.querySelector("meta[name=ledger-token]").content;
  return fetch("/api/swarm/" + slug, { method: "PUT", headers: { "Content-Type": "application/json", "X-Ledger-Token": token },
    body: JSON.stringify({ action: "reopen" }) });
}

document.addEventListener("click", async (e) => {
  const b = e.target.closest("button[data-act]");
  if (!b) return;
  b.disabled = true;
  const r = b.dataset.act === "reopen" ? await reopenLedger(b)
    : await fetch("/api/bin", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ action: b.dataset.act, slug: b.dataset.slug }) });
  if (r.ok) return location.reload();
  b.disabled = false;
  alert(await r.text());
});

function folding() {
  const all = document.getElementById("fold-all");
  if (!all) return;
  const read = (k, f) => { try { return JSON.parse(localStorage.getItem(k) || "null") || f; } catch (e) { return f; } };
  const write = (k, v) => { try { localStorage.setItem(k, JSON.stringify(v)); } catch (e) { /* storage blocked: the choice lasts until reload */ } };
  const rows = () => [...list.querySelectorAll("li.row")];
  const isOpen = (r) => r.classList.contains("open");
  const fold = (r, open) => { r.classList.toggle("open", open); r.querySelector(".fold").setAttribute("aria-expanded", String(open)); };
  const label = () => { all.textContent = rows().length && rows().every(isOpen) ? "Collapse all" : "Expand all"; };
  const remember = () => { const s = read(FOLD_KEY, {}); for (const r of rows()) s[r.dataset.slug] = isOpen(r); write(FOLD_KEY, s); label(); };
  let sort = read(SORT_KEY, {});
  if (!Object.hasOwn(FIRST, sort.key) || ![1, -1].includes(sort.dir)) sort = { key: "at", dir: -1 };
  const value = (r, k) => (k === "kind" ? r.dataset.kind : Number(r.dataset[k]));
  const compare = (a, b) => {
    const x = value(a, sort.key), y = value(b, sort.key);
    return (x < y ? -1 : x > y ? 1 : 0) * sort.dir || Number(b.dataset.at) - Number(a.dataset.at);
  };
  const order = () => {
    list.append(...rows().sort(compare));
    for (const head of document.querySelectorAll(".sort")) {
      const on = head.dataset.sort === sort.key;
      if (on) head.dataset.dir = sort.dir > 0 ? "asc" : "desc";
      else delete head.dataset.dir;
      head.querySelector("i").textContent = on ? (sort.dir > 0 ? "▲" : "▼") : "↕";
    }
  };
  const saved = read(FOLD_KEY, {});
  for (const r of rows()) fold(r, saved[r.dataset.slug] === true);
  label();
  order();
  document.addEventListener("click", (e) => {
    const b = e.target.closest(".fold,#fold-all,.sort");
    if (!b) return;
    if (b.classList.contains("sort")) {
      const k = b.dataset.sort;
      sort = sort.key === k ? { key: k, dir: -sort.dir } : { key: k, dir: FIRST[k] };
      write(SORT_KEY, sort);
      return order();
    }
    if (b === all) {
      const open = !rows().every(isOpen);
      for (const r of rows()) fold(r, open);
    } else {
      const r = b.closest("li.row");
      fold(r, !isOpen(r));
    }
    remember();
  });
}

async function start() {
  const now = Date.now();
  try {
    if (view === "bin") {
      const rows = await collection("bin");
      list.replaceChildren(...rows.map(binRow));
      document.getElementById("total").textContent = `${rows.length} ledger${rows.length === 1 ? "" : "s"}`;
      if (!rows.length) list.append(h("li", { class: "empty", text: "The bin is empty." }));
    } else {
      const [rows, binned] = await Promise.all([collection("ledgers"), collection("bin")]);
      list.replaceChildren(...rows.map((s) => homeRow(s, now)));
      if (!rows.length) list.append(h("li", { class: "empty", text: "No ledgers yet." }));
      if (binned.length) document.getElementById("bin-fab").append(h("span", { class: "count", text: String(binned.length) }));
    }
  } catch (error) {
    list.replaceChildren(h("li", { class: "empty", text: `The ledger list could not be read: ${error.message}` }));
  }
  folding();
}

start();
