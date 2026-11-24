import { RETRY_MS } from "./config.js";
import { openEvents, readSwarm } from "./api.js";
import { applyPatch } from "./patch.js";
import { disconnected, receiveChange, receiveOpen, receiveSnapshot, resume } from "./sync.js";
import { receiveSwarm, swarmLost } from "./swarm.js";

let eventCursor = null;
const latest = { swarm: null };

function onStreamEvent(name, data) {
  if (name === "open") {
    if ("swarm" in data) {
      latest.swarm = data.swarm;
      receiveSwarm(latest.swarm);
    }
    receiveOpen(data.rev);
  } else if (name === "snapshot") {
    if (data.swarm) {
      latest.swarm = data.swarm;
      receiveSwarm(latest.swarm);
    }
    if (data.ledger) receiveSnapshot(data.ledger);
  } else if (name === "ledger") {
    receiveChange(data);
  } else if (name === "swarm") {
    latest.swarm = applyPatch(latest.swarm, data.patch);
    receiveSwarm(latest.swarm);
  } else if (name === "heartbeat") {
    resume();
  }
}

export async function loadSwarm() {
  const resp = await readSwarm();
  if (!resp.ok) return;
  const reply = await resp.json();
  const status = { ...reply.data };
  for (const name of reply.collections || []) {
    status[name] = [];
    let cursor = null;
    do {
      const page = await readSwarm(name, cursor);
      if (!page.ok) return;
      const rows = await page.json();
      status[name].push(...rows.data);
      cursor = rows.next_cursor;
    } while (cursor);
  }
  if (latest.swarm === null) receiveSwarm(status);
}

async function takeFrame(block) {
  let name = "message", id = null;
  const data = [];
  for (const line of block.split("\n")) {
    const at = line.indexOf(":");
    const field = at < 0 ? line : line.slice(0, at);
    const value = at < 0 ? "" : line.slice(at + 1).replace(/^ /, "");
    if (field === "event") name = value;
    else if (field === "data") data.push(value);
    else if (field === "id") id = value;
  }
  if (!data.length) return;
  try {
    onStreamEvent(name, JSON.parse(data.join("\n")));
  } catch (error) {
    eventCursor = null;
    throw error;
  }
  if (id) eventCursor = id;
}

async function readStream(body) {
  const reader = body.pipeThrough(new TextDecoderStream()).getReader();
  let buffer = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) return;
    buffer = (buffer + value).replace(/\r\n?/g, "\n");
    let end;
    while ((end = buffer.indexOf("\n\n")) >= 0) {
      await takeFrame(buffer.slice(0, end));
      buffer = buffer.slice(end + 2);
    }
  }
}

export async function connectEvents() {
  let failures = 0;
  for (;;) {
    try {
      const resp = await openEvents(eventCursor);
      if (resp.status === 410) {
        eventCursor = null;
        continue;
      }
      if (!resp.ok || !resp.body) throw new Error(`server answered ${resp.status}`);
      failures = 0;
      resume();
      await readStream(resp.body);
    } catch (error) {
      failures++;
      disconnected();
      swarmLost(error.message || "ledger server unreachable");
    }
    await new Promise((done) => setTimeout(done, RETRY_MS[Math.min(failures, RETRY_MS.length - 1)]));
  }
}
