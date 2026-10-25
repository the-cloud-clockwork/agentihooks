import { API_V1, EVENTS_API, HIVES_API, LAYOUT_API, ROOT_API, ROUTING_API, SLUG, TIPS_API, TOKEN } from "./config.js";

export function openEvents(cursor) {
  const headers = { Accept: "text/event-stream", "X-Ledger-Token": TOKEN };
  if (cursor) headers["Last-Event-ID"] = cursor;
  return fetch(EVENTS_API, { cache: "no-store", headers });
}

export function writeLedger(body, unloading) {
  return fetch(`${API_V1}/saves`, { method: "PUT", keepalive: unloading && body.length < 60000,
    headers: { "Content-Type": "application/json", "X-Ledger-Token": TOKEN }, body });
}

export function writeSwarm(body) {
  return fetch(`${API_V1}/swarm/controls`, { method: "PUT", headers: { "Content-Type": "application/json", "X-Ledger-Token": TOKEN }, body: JSON.stringify(body) });
}

export function readModels() {
  return fetch(`${API_V1}/swarm/models`, { cache: "no-store", headers: { "X-Ledger-Token": TOKEN } });
}

export function uploadImage(file) {
  return fetch(`${API_V1}/uploads/media`, { method: "POST", headers: { "Content-Type": "application/octet-stream", "X-Ledger-Token": TOKEN }, body: file });
}

export function readMetadata() {
  return fetch(API_V1, { cache: "no-store", headers: { "X-Ledger-Token": TOKEN } });
}

export function readWorkspace(taskId) {
  return fetch(`${API_V1}/tasks/${encodeURIComponent(taskId)}/workspace`, { cache: "no-store", headers: { "X-Ledger-Token": TOKEN } });
}

export function readClosed(name, offset) {
  return fetch(`${API_V1}/${name}?state=done&offset=${offset}&limit=10`, { cache: "no-store", headers: { "X-Ledger-Token": TOKEN } });
}

export function searchClosed(name, text, offset = 0) {
  return fetch(`${API_V1}/${name}?state=done&q=${encodeURIComponent(text)}&offset=${offset}&limit=50`, { cache: "no-store", headers: { "X-Ledger-Token": TOKEN } });
}

export function readArchitecture() {
  return fetch(`${API_V1}/architecture`, { cache: "no-store", headers: { "X-Ledger-Token": TOKEN } });
}

export function writeArchitecture(body) {
  return fetch(`${API_V1}/architecture/actions`, { method: "POST", headers: { "Content-Type": "application/json", "X-Ledger-Token": TOKEN }, body: JSON.stringify(body) });
}

export function readArtifact(id) {
  return fetch(artifactUrl(id));
}

export function readRouting() {
  return fetch(ROUTING_API, { cache: "no-store" });
}

export function writeRouting(changes) {
  return fetch(ROUTING_API, { method: "PATCH", headers: { "Content-Type": "application/json", "X-Ledger-Token": TOKEN, "X-Ledger-Slug": SLUG }, body: JSON.stringify(changes) });
}

export function readHives() {
  return fetch(HIVES_API, { cache: "no-store" });
}

export function writeHive(id, changes) {
  return fetch(`${HIVES_API}/${encodeURIComponent(id)}`, { method: "PATCH", headers: { "Content-Type": "application/json", "X-Ledger-Token": TOKEN, "X-Ledger-Slug": SLUG }, body: JSON.stringify(changes) });
}

export function readLayout() {
  return fetch(LAYOUT_API, { cache: "no-store" });
}

export function writeLayout(layout) {
  return fetch(LAYOUT_API, { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(layout) });
}

export function readTips() {
  return fetch(TIPS_API, { cache: "no-store" });
}

export async function loadTips() {
  try {
    const resp = await readTips();
    const values = resp.ok ? (await resp.json()).data : null;
    if (values && window.ledgerTipValues) window.ledgerTipValues(values);
    return values;
  } catch (e) { return null; }
}

export function readCollection(name, cursor) {
  return fetch(`${ROOT_API}/${name}?limit=100${cursor ? `&cursor=${encodeURIComponent(cursor)}` : ""}`, { cache: "no-store" });
}

export function writeBin(action, slug) {
  return fetch(`${ROOT_API}/bin/actions`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ action, slug }) });
}

export function readLedgerPage(slug) {
  return fetch(`/${encodeURIComponent(slug)}`);
}

export function writeLedgerSwarm(slug, token, body) {
  return fetch(`${ROOT_API}/ledgers/${encodeURIComponent(slug)}/swarm/controls`, { method: "PUT", headers: { "Content-Type": "application/json", "X-Ledger-Token": token }, body: JSON.stringify(body) });
}

export function mediaUrl(id) {
  return `${API_V1}/files/media/${id}`;
}

export function artifactUrl(id) {
  return `${API_V1}/files/artifacts/${id}`;
}
