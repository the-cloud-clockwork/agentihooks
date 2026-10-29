import { h, newId } from "./dom.js";
import { pendingOf } from "./state.js";
import { doc, queue } from "./sync.js";

export function phaseStatus(phase) {
  return doc.phase_status[phase.id] || { state: "", label: "" };
}

export function phaseReview(phase) {
  const review = phase.review || {};
  if (phaseStatus(phase).state !== "in_review" || (review.state === "sent_back" && !review.escalated)) return null;
  if (pendingOf(doc, `phases/${phase.id}`)) return h("div", { class: "hl-verdict phase-review", text: "review sent" });
  const note = h("input", { class: "hl-note phase-note", type: "text", maxlength: "500", placeholder: "note for the planner", "aria-label": "Review note" });
  const decide = (state) => {
    const text = note.value.trim();
    if (state === "sent_back" && !text) return note.focus();
    queue({ op: "phase_review", id: newId("review"), by: "operator", item: `phases/${phase.id}`, state, ...(text ? { note: text } : {}) });
  };
  const button = (text, state) => {
    const btn = h("button", { class: "sw-btn", type: "button", text });
    btn.addEventListener("click", () => decide(state));
    return btn;
  };
  return h("div", { class: "hl-verdict phase-review" }, note, button("Approve plan", "approved"), button("Send back", "sent_back"));
}
