import { $, newId } from "./dom.js";
import { doc, queue } from "./sync.js";

export function editTitle(on) {
  $("title-row").hidden = on;
  $("title-form").hidden = !on;
  $("title-error").textContent = "";
  if (!on) return $("title-edit").focus();
  const box = $("title-input");
  box.value = doc.title;
  box.focus();
  box.select();
}

export function editOverview(on) {
  $("overview").hidden = on;
  $("overview-form").hidden = !on;
  $("overview-edit").hidden = on;
  if (!on) return $("overview-edit").focus();
  const box = $("overview-input");
  box.value = doc.overview;
  box.focus();
}

export function saveOverview() {
  const text = $("overview-input").value.trim();
  editOverview(false);
  if (text !== doc.overview) queue({ op: "overview_set", id: newId("overview"), text });
}

export function saveTitle() {
  const text = $("title-input").value.trim();
  if (!text) {
    $("title-error").textContent = "The title cannot be empty";
    return $("title-input").focus();
  }
  editTitle(false);
  if (text !== doc.title) queue({ op: "title_set", id: newId("title"), text });
}
