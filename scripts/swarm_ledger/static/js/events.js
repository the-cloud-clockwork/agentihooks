import { RETRY_MS } from "./config.js";
import { openEvents } from "./api.js";
import { applyPatch } from "./patch.js";
import { disconnected, receiveLedger, resume } from "./sync.js";
import { receiveSwarm, swarmLost } from "./swarm.js";

let eventCursor = null;
let snapshot = null;
const latest = { ledger: null, swarm: null };

function onStreamEvent(name, data) {
  if (name === "snapshot") {
    Object.assign(latest, data);
    receiveSwarm(latest.swarm);
    if (latest.ledger) receiveLedger(latest.ledger);
  } else if (name === "ledger") {
    latest.ledger = applyPatch(latest.ledger, data.patch);
    receiveLedger(latest.ledger);
  } else if (name === "swarm") {
    latest.swarm = applyPatch(latest.swarm, data.patch);
    receiveSwarm(latest.swarm);
  } else if (name === "heartbeat") {
    resume();
  }
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
    const raw = data.join("\n");
    if (name === "snapshot" && raw === snapshot) resume();
    else {
      await scheduler.postTask(() => {}, { priority: "background" });
      const parsed = JSON.parse(raw);
      await scheduler.postTask(() => {}, { priority: "background" });
      onStreamEvent(name, parsed);
      if (name === "snapshot") snapshot = raw;
      else if (name === "ledger" || name === "swarm") snapshot = null;
    }
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
