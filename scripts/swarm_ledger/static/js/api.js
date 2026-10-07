import { API, EVENTS_API, LAYOUT_API, MEDIA_API, SLUG, TOKEN } from "./config.js";

export function openEvents(cursor) {
  const headers = { Accept: "text/event-stream", "X-Ledger-Token": TOKEN };
  if (cursor) headers["Last-Event-ID"] = cursor;
  return fetch(EVENTS_API, { cache: "no-store", headers });
}

export function writeLedger(body, unloading) {
  return fetch(API, { method: "PUT", keepalive: unloading && body.length < 60000,
    headers: { "Content-Type": "application/json", "X-Ledger-Token": TOKEN }, body });
}

export function writeSwarm(body) {
  return fetch(API.replace(`/api/${SLUG}`, `/api/swarm/${SLUG}`), { method: "PUT", headers: { "Content-Type": "application/json", "X-Ledger-Token": TOKEN }, body: JSON.stringify(body) });
}

export function uploadImage(file) {
  return fetch(MEDIA_API, { method: "POST", headers: { "X-Ledger-Token": TOKEN }, body: file });
}

export function readArtifact(id) {
  return fetch(artifactUrl(id));
}

export function readLayout() {
  return fetch(LAYOUT_API, { cache: "no-store" });
}

export function writeLayout(layout) {
  return fetch(LAYOUT_API, { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(layout) });
}

export function mediaUrl(id) {
  return `${API.replace(`/api/${SLUG}`, `/media/${SLUG}`)}/${id}`;
}

export function artifactUrl(id) {
  return `${API.replace(`/api/${SLUG}`, `/artifacts/${SLUG}`)}/${id}`;
}
