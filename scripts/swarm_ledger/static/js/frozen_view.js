import { SLUG } from "./config.js";
import { $, h, store, stored } from "./dom.js";
import { byId, snowflake, snowflakeState } from "./freezes.js";

const STORAGE_KEY = `plan-ledger:${SLUG}:hide-frozen`;
const MARKED = ["plans", "phases", "slices", "tasks", "followups"];
const STRUCTURE = ["plans", "phases", "slices"];
let hiding = stored(STORAGE_KEY, false);
let visible = new Set();
const revealed = new Set();
const UP = { phases: "plan", slices: "phase", tasks: "slice" };

function ancestors(d, key, seen = new Set()) {
  if (seen.has(key)) return [];
  seen.add(key);
  const [list, id] = key.split("/");
  const item = UP[list] && byId(d[list] || [], id);
  if (!item) return [key];
  const task = list === "tasks";
  const up = [item[UP[list]] || (task && item.phase ? `phases/${item.phase}` : ""), task && item.merged_into ? `tasks/${item.merged_into}` : ""];
  return [key, ...new Set(up.filter(Boolean).flatMap((parent) => ancestors(d, parent, seen)))];
}

export function prepareFrozenView(d) {
  visible = new Set(revealed);
  const parents = new Set();
  for (const list of MARKED) for (const item of d[list]) {
    if (!item.deleted) for (const parent of ancestors(d, `${list}/${item.id}`).slice(1)) parents.add(parent);
  }
  for (const list of MARKED) for (const item of d[list]) {
    const key = `${list}/${item.id}`;
    if (!item.deleted && !snowflakeState(d, key) && (!STRUCTURE.includes(list) || !parents.has(key))) visible.add(key);
  }
  for (const key of [...visible]) for (const parent of ancestors(d, key)) {
    if (!snowflakeState(d, parent)) visible.add(parent);
  }
  markFrozenToggle();
}

export function viewVisible(key) {
  return !hiding || !MARKED.includes(key.split("/")[0]) || visible.has(key);
}

export function viewItems(list, items) {
  return items.filter((i) => !i.deleted && viewVisible(list === "priorities" ? i.item : `${list}/${i.id}`));
}

export function viewCount(list, items) {
  const all = items.filter((i) => !i.deleted);
  const hidden = all.length - viewItems(list, all).length;
  return hiding && [...MARKED, "priorities"].includes(list) ? ` · ${all.length - hidden} visible · ${hidden} hidden` : "";
}

export function revealFrozenTarget(d, id) {
  const match = /^item-(plans|phases|slices|tasks|followups)-(.+)$/.exec(id);
  if (!match) return [];
  const keys = ancestors(d, `${match[1]}/${match[2]}`);
  for (const key of keys) revealed.add(key);
  return keys.map((key) => `item-${key.replace("/", "-")}`);
}

function markFrozenToggle() {
  const button = $("frozen-view");
  button.setAttribute("aria-pressed", String(hiding));
  const icon = snowflake("");
  icon.removeAttribute("role");
  icon.removeAttribute("title");
  icon.removeAttribute("aria-label");
  icon.setAttribute("aria-hidden", "true");
  button.replaceChildren(icon, h("span", { text: hiding ? "Show all" : "Hide frozen" }));
}

export function toggleFrozenView() {
  hiding = !hiding;
  revealed.clear();
  store(STORAGE_KEY, hiding);
}
