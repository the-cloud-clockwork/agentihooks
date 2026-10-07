import { ARTIFACT_KEEP_DAYS, DAY } from "./config.js";
import { $, h, newId, when } from "./dom.js";
import { artifactUrl, readArtifact } from "./api.js";
import { doc, queue } from "./sync.js";
import { jsonTree, markdown } from "./markdown.js";
import { anchorPanel } from "./notices.js";

async function artifactBody(file) {
  const url = artifactUrl(file.id);
  if (file.type.startsWith("image/")) return [h("img", { src: url, alt: "Artifact image", width: file.width, height: file.height })];
  const resp = await readArtifact(file.id);
  if (!resp.ok) throw new Error(`the server answered ${resp.status}`);
  const text = await resp.text();
  if (file.type === "application/json") return [h("div", { class: "json-tree" }, jsonTree(JSON.parse(text)))];
  return [h("div", { class: "art-doc" }, ...markdown(text))];
}

function artifactMeta(a) {
  return [a.by, a.task ? `#${a.task}` : "", when(a.at)].filter(Boolean).join(" · ");
}

function openArtifact(a) {
  const previousFocus = document.activeElement;
  const body = h("div", { class: "image-scroll art-body" }, h("p", { class: "hint", text: "Loading…" }));
  const viewer = h("dialog", { class: "image-viewer", "aria-label": "Artifact viewer" },
    h("div", { class: "image-toolbar" },
      h("div", { class: "art-head" }, h("strong", { text: a.title }), h("span", { class: "notif-meta", text: artifactMeta(a) })),
      h("button", { class: "link image-close", type: "button", text: "Close", "aria-label": "Close artifact", on: { click: () => viewer.close() } })),
    body);
  viewer.addEventListener("click", (ev) => { if (ev.target === viewer) viewer.close(); });
  viewer.addEventListener("keydown", (ev) => { if (ev.key === "Escape") ev.stopPropagation(); });
  viewer.addEventListener("close", () => { viewer.remove(); previousFocus.focus(); });
  document.body.append(viewer);
  viewer.showModal();
  artifactBody(a.file).then((nodes) => body.replaceChildren(...nodes),
    (err) => body.replaceChildren(h("p", { class: "attach-note", text: `This artifact could not be opened: ${err.message}` })));
}

function daysLeft(deletedAt) {
  return Math.max(0, Math.ceil((ARTIFACT_KEEP_DAYS * DAY - (Date.now() - deletedAt)) / DAY));
}

export function renderArtifacts() {
  const list = [...doc.artifacts].sort((a, b) => (b.at || 0) - (a.at || 0));
  $("art-badge").hidden = !list.length;
  $("art-badge").textContent = String(list.length);
  if ($("art-panel").hidden) {
    $("art-trash").replaceChildren();
    return $("art-list").replaceChildren();
  }
  $("art-list").replaceChildren(...list.map((a) => h("li", { class: "notif-row art-row" },
    h("div", { class: "notif-meta", text: artifactMeta(a) }),
    h("button", { class: "art-title", type: "button", text: a.title, on: { click: () => openArtifact(a) } }),
    h("button", { class: "link danger", type: "button", text: "Delete", "aria-label": `Delete ${a.title}`,
      on: { click: () => queue({ op: "artifact_delete", id: newId("artdel"), target: a.id }) } }))));
  if (!list.length) $("art-list").append(h("li", { class: "empty", text: "No artifacts yet." }));
  const trash = [...doc.artifact_trash].sort((a, b) => (b.deleted_at || 0) - (a.deleted_at || 0));
  $("art-trash-head").hidden = !trash.length;
  $("art-trash").replaceChildren(...trash.map((a) => h("li", { class: "notif-row art-row" },
    h("div", { class: "notif-meta", text: `${a.title} · ${daysLeft(a.deleted_at)} days left` }),
    h("button", { class: "link", type: "button", text: "Restore", "aria-label": `Restore ${a.title}`,
      on: { click: () => queue({ op: "artifact_restore", id: newId("artback"), target: a.id }) } }))));
}

export function showArtifacts(open) {
  $("art-panel").hidden = !open;
  $("art-fab").setAttribute("aria-expanded", String(open));
  renderArtifacts();
  if (open) anchorPanel($("art-panel"), $("art-fab"));
}
