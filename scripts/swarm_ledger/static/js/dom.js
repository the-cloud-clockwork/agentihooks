export const $ = (id) => document.getElementById(id);

export function h(tag, attrs, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (k === "text") el.textContent = v;
    else if (k === "on") for (const [ev, fn] of Object.entries(v)) el.addEventListener(ev, fn);
    else if (v !== false && v !== undefined) el.setAttribute(k, v);
  }
  for (const kid of kids) if (kid) el.append(kid);
  return el;
}

export function status(text, cls) {
  const el = $("status");
  el.textContent = text;
  el.className = "status" + (cls ? " " + cls : "");
}

export function banner(messages) {
  const el = $("banner");
  el.textContent = messages.filter(Boolean).join(" · ");
  el.classList.toggle("on", !!el.textContent);
}

export function closedText(closedAt) {
  if (!Number.isInteger(closedAt)) return "";
  return `Closed ${new Date(closedAt).toISOString().slice(0, 16).replace("T", " ")} UTC`;
}

export function grow(el) {
  el.style.height = "auto";
  el.style.height = el.scrollHeight + 2 + "px";
}

export function when(at) {
  if (!at) return "";
  const d = new Date(at);
  const time = d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  return d.toDateString() === new Date().toDateString() ? time : `${d.toLocaleDateString([], { month: "short", day: "numeric" })} ${time}`;
}

export function newId(noun) {
  const rand = (crypto.randomUUID ? crypto.randomUUID() : String(Math.random()).slice(2)).replace(/-/g, "").slice(0, 10);
  return `${noun[0]}-${rand}`;
}

export function headCount(parts) {
  const shown = parts.filter(([n]) => n).map(([n, label]) => (label ? `${n} ${label}` : String(n)));
  return shown.length ? `· ${shown.join(" · ")}` : "";
}

export function age(ms) {
  return ms < 60000 ? `${Math.max(0, Math.floor(ms / 1000))}s` : span(ms);
}

export function span(ms) {
  const m = Math.max(0, Math.round(ms / 60000));
  const d = Math.floor(m / 1440), hr = Math.floor((m % 1440) / 60);
  return d ? `${d}d ${hr}h` : hr ? `${hr}h ${m % 60}m` : `${m}m`;
}

export function noticeTime(at) {
  const t = new Date(at);
  const time = t.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  return t.toDateString() === new Date().toDateString() ? time : `${t.toLocaleDateString([], { month: "short", day: "numeric" })} ${time}`;
}

export function clock(at) {
  return at ? new Date(at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false }) : "";
}

export function stored(key, fallback) {
  try { return JSON.parse(localStorage.getItem(key) || "null") || fallback; } catch (e) { return fallback; }
}

export function store(key, value) {
  try { localStorage.setItem(key, JSON.stringify(value)); } catch (e) { /* storage blocked: the state lasts until reload */ }
}
