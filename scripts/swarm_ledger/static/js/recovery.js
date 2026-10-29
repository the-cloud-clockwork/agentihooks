import { $, clock, h, span } from "./dom.js";
import { firstPage } from "./pages.js";
import { cells, emptyRow, idCell, label, moreRow, pending, seatText, withId } from "./swarm.js";

export function recoveryCards(sw) {
  return (sw && sw.recovery && sw.recovery.cards) || [];
}

function lines(texts) {
  return h("div", { class: "sw-lines" }, ...texts.map((text) => h("span", { text, title: text })));
}

function choiceButton(card, c) {
  return h("button", { class: "sw-btn", type: "button", "data-recovery-choice": c.send, "data-agent": card.agent,
    "aria-label": `${c.label} for ${card.agent}`, title: c.unavailable || c.label,
    disabled: !!(c.unavailable || pending || !c.send), text: c.label });
}

function decision(card) {
  if (card.accepted) return h("span", { class: "sw-acts" }, label(card.accepted.text, "confirmed"), card.accepted.replacement ? idCell(card.accepted.replacement) : null);
  if (card.requested) return label(card.requested_text, "pending");
  return h("div", { class: "sw-ctl" }, ...card.choices.map((c) => choiceButton(card, c)));
}

export function renderRecovery(sw) {
  const cards = recoveryCards(sw);
  $("recovery-count").textContent = cards.length ? `${cards.length} ${cards.length === 1 ? "seat" : "seats"}` : "none waiting";
  $("swarm-recovery").replaceChildren(...(cards.length
    ? cards.map((card) => withId(cells(idCell(seatText(card.seat)), card.reason, label(card.badge), lines(card.material),
      lines(card.diagnosis), span(card.wait_seconds * 1000), decision(card)), `recovery-${card.seat}`))
    : [emptyRow(7, "No seat is waiting on a recovery decision.")]));
}

export function renderHandoffs(rows, sw) {
  const recovering = new Set(recoveryCards(sw).map((card) => card.seat));
  $("handoff-count").textContent = `${rows.length} ${rows.length === 1 ? "seat" : "seats"}`;
  $("swarm-handoffs").replaceChildren(...(rows.length ? firstPage("seats", rows, (r) => `seat-${r.seat}`).map((r) => withId(cells(idCell(seatText(r.seat)), clock(r.at), r.reason,
    r.continuity ? label(r.continuity) : "—", r.binding ? h("span", {}, label(r.binding), r.bound_at && ` ${clock(r.bound_at)}`) : "—",
    r.awaiting && recovering.has(r.seat) ? h("a", { href: `#recovery-${r.seat}`, text: "recovery" })
    : r.awaiting ? h("span", { class: "sw-ctl" }, ...(r.resume_available === false ? ["fresh"] : ["resume", "fresh"]).map((choice) => h("button", { class: "sw-btn", type: "button",
      "data-agent": r.awaiting, "data-restore-choice": choice, disabled: !!pending, text: choice === "resume" ? r.resume_label || choice : choice }))) : r.successor ? idCell(r.successor) : "—"), `seat-${r.seat}`))
    : [emptyRow(6, "No seats yet. Seats appear when agents start.")]), moreRow("seats", rows.length, "more seats", 6, () => renderHandoffs(rows, sw)) || "");
}
