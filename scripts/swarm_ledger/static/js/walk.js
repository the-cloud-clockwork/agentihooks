const WALKS = ["#outline a[data-target]", "#tasks .item-title > a.task-id"];
const WALK_KEYS = { ArrowDown: 1, ArrowUp: -1, j: 1, k: -1 };

function typing(el) {
  return !!el && (el.isContentEditable || ["INPUT", "TEXTAREA", "SELECT"].includes(el.tagName));
}

export function walkKeys(ev) {
  const step = WALK_KEYS[ev.key];
  if (!step || ev.defaultPrevented || ev.altKey || ev.ctrlKey || ev.metaKey || ev.shiftKey || typing(ev.target)) return;
  const at = document.activeElement;
  const walk = WALKS.find((selector) => at && at.matches(selector));
  if (!walk && ev.key.startsWith("Arrow")) return;
  const links = [...document.querySelectorAll(walk || WALKS[0])].filter((a) => a.getClientRects().length);
  if (!links.length) return;
  ev.preventDefault();
  const n = links.indexOf(at);
  const next = n < 0 ? links.find((a) => a.classList.contains("on")) || links[0] : links[Math.min(links.length - 1, Math.max(0, n + step))];
  next.focus();
  next.scrollIntoView({ block: "nearest" });
}
