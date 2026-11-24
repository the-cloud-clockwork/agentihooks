import { readOpen, readSummary, readThread } from "./api.js";
import { closedRow, receiveLedger, resume } from "./sync.js";

const FIRST = { tasks: 20, phases: 40, followups: 50, alerts: 50, slices: 100, freezes: 100 };
const NEWEST = { notifications: 50 };
const COMPLETE = ["slices", "freezes"];
const OPEN_PAGE = 50;
const OPEN = "open";
const OUT = "out";
let held = null;
let heldRev = -1;
let newest = -1;
let seeding = null;
let sections = {};
let summaryTimer = null;
const openRead = {};
const positions = {};
const loadedStates = {};
const remote = {};

async function answer(resp) {
  if (!resp.ok) throw new Error(String(resp.status));
  return resp.json();
}

function noteThreads(name, rows) {
  for (const row of rows) if (row.comments_count && !Array.isArray(row.comments)) remote[`${name}/${row.id}`] = row.comments_count;
}

async function openPage(name, offset, limit) {
  const reply = await answer(await readOpen(name, offset, limit));
  noteThreads(name, reply.data);
  return reply;
}

function same(older, newer) {
  if (!Array.isArray(older) || !Array.isArray(newer)) return newer;
  const before = new Map(older.map((row) => [row && row.id, row]));
  return newer.map((row) => {
    const kept = row && before.get(row.id);
    return kept && JSON.stringify(kept) === JSON.stringify(row) ? kept : row;
  });
}

function countLoaded(name, row) {
  const state = openState(name, row);
  if (!state) return;
  const bucket = loadedStates[name] || (loadedStates[name] = {});
  bucket[state] = (bucket[state] || 0) + 1;
}

async function seedOnce() {
  const summary = readSummary().then(answer);
  const pages = Object.entries(FIRST).map(([name, limit]) => openPage(name, 0, limit).then((reply) => [name, reply.data]));
  try {
    const { data } = await summary;
    const newestPages = Object.entries(NEWEST).map(([name, limit]) => {
      const offset = Math.max(0, ((data.sections || {})[name] || { open: 0 }).open - limit);
      return openPage(name, offset, limit).then((reply) => [name, reply.data]);
    });
    const server = { ...data };
    delete server.sections;
    for (const [name, rows] of [...(await Promise.all(pages)), ...(await Promise.all(newestPages))]) {
      positions[name] = new Map();
      loadedStates[name] = {};
      server[name] = same(held && held[name], placed(name, [], rows));
      openRead[name] = rows.length;
    }
    sections = data.sections || {};
    held = server;
    heldRev = server._meta.rev;
    newest = Math.max(newest, heldRev);
    receiveLedger(held);
    for (const name of COMPLETE) completeOpen(name).catch(() => null);
  } catch (error) {
    await Promise.allSettled(pages);
    throw error;
  }
}

export function seed() {
  if (!seeding) seeding = seedOnce().finally(() => {
    seeding = null;
    if (newest > heldRev) seed();
  });
  return seeding;
}

async function completeOpen(name) {
  while (held && openLeft(name)) {
    const before = openRead[name] || 0;
    await loadOpen(name, true);
    if ((openRead[name] || 0) === before) return;
  }
  if (held) receiveLedger(held);
}

export async function loadOpen(name, quiet = false) {
  const reply = await openPage(name, openRead[name] || 0, OPEN_PAGE);
  openRead[name] = (openRead[name] || 0) + reply.data.length;
  const known = new Set((held[name] || []).map((row) => row.id));
  held = { ...held, [name]: placed(name, held[name] || [], reply.data.filter((row) => !known.has(row.id))) };
  if (!quiet) receiveLedger(held);
}

function placed(name, kept, read) {
  const at = positions[name] || (positions[name] = new Map());
  const rows = read.map(({ position, ...row }) => {
    if (Number.isInteger(position)) at.set(row.id, position);
    countLoaded(name, row);
    return row;
  });
  const order = (row) => (at.has(row.id) ? at.get(row.id) : Infinity);
  return [...kept, ...rows].sort((a, b) => order(a) - order(b));
}

function openState(name, row) {
  if (row.deleted || closedRow(name, row)) return null;
  if (row.out_of_scope) return OUT;
  return name === "tasks" ? row.state || OPEN : OPEN;
}

export function openStates(name) {
  const section = sections[name];
  if (!section) return {};
  const loaded = loadedStates[name] || {};
  return Object.fromEntries(Object.entries(section.states).map(([state, n]) => [state, Math.max(0, n - (loaded[state] || 0))]));
}

export function openLeft(name) {
  return Object.values(openStates(name)).reduce((sum, n) => sum + n, 0);
}

function refreshSummary() {
  clearTimeout(summaryTimer);
  summaryTimer = setTimeout(async () => {
    try {
      const { data } = await answer(await readSummary());
      if (!held || seeding) return;
      sections = data.sections || sections;
      const next = { ...held };
      for (const [key, value] of Object.entries(data)) if (key !== "sections") next[key] = same(held[key], value);
      next._meta = { ...data._meta, rev: heldRev };
      held = next;
      receiveLedger(held);
    } catch (e) { return; }
  }, 300);
}

export function receiveOpen(at) {
  if (at === null || at === undefined) return;
  newest = Math.max(newest, at);
  if (held && !seeding && at > heldRev) seed();
}

export function receiveSnapshot(ledger) {
  if (!ledger || !ledger._meta) return;
  sections = {};
  held = ledger;
  heldRev = ledger._meta.rev;
  newest = Math.max(newest, heldRev);
  receiveLedger(held);
  resume();
}

export function receiveChange(change) {
  newest = Math.max(newest, change.rev);
  if (!held || seeding || heldRev >= change.rev) return;
  if (change.from === undefined || change.from > heldRev) {
    seed();
    return;
  }
  const parts = (change.patch && change.patch.o) || {};
  const rows = change.rows || {};
  const next = { ...held };
  for (const [name, ids] of Object.entries(rows)) {
    const part = parts[name] || {};
    const bodies = new Map((part.v || [...(part.u || []), ...(part.add || [])]).map((row) => [row.id, row]));
    const gone = new Set(ids.gone);
    const list = (held[name] || []).filter((row) => !gone.has(row.id)).map((row) => bodies.get(row.id) || row);
    const known = new Set(list.map((row) => row.id));
    const added = ids.u.filter((id) => !known.has(id) && bodies.has(id)).map((id) => bodies.get(id));
    for (const row of added) countLoaded(name, row);
    next[name] = [...list, ...added];
  }
  next._meta = { ...held._meta, rev: change.rev };
  held = next;
  heldRev = change.rev;
  receiveLedger(held);
  resume();
  if (Object.keys(parts).some((key) => !(key in rows))) refreshSummary();
}

export function remoteComments(key) {
  return remote[key] || 0;
}

export async function loadThread(key) {
  if (!remote[key] || !held) return;
  const [name, id] = key.split("/");
  const comments = [];
  let cursor = null;
  do {
    const reply = await answer(await readThread(key, cursor));
    comments.push(...reply.data);
    cursor = reply.next_cursor;
  } while (cursor);
  delete remote[key];
  held = { ...held, [name]: (held[name] || []).map((row) => (row.id === id ? { ...row, comments } : row)) };
  receiveLedger(held);
}
