import { ACCEPT, MAX_ATTACH } from "./config.js";
import { h } from "./dom.js";
import { mediaUrl, uploadImage } from "./api.js";

export const attaching = {};
const attachNote = {};

function openImages(list, index) {
  const previousFocus = document.activeElement;
  const image = h("img", { alt: "Attached image" });
  const previous = h("button", { class: "link", type: "button", text: "Previous", "aria-label": "Previous image", on: { click: () => show(index - 1) } });
  const next = h("button", { class: "link", type: "button", text: "Next", "aria-label": "Next image", on: { click: () => show(index + 1) } });
  const counter = h("span", { "aria-live": "polite" });
  const scroll = h("div", { class: "image-scroll" }, image);
  const viewer = h("dialog", { class: "image-viewer", "aria-label": "Image viewer" },
    h("div", { class: "image-toolbar" }, previous, counter, next,
      h("button", { class: "link image-close", type: "button", text: "Close", "aria-label": "Close viewer", on: { click: () => viewer.close() } })), scroll);
  function show(value) {
    index = value;
    const att = list[index];
    image.src = mediaUrl(att.id);
    image.width = att.width;
    image.height = att.height;
    counter.textContent = `${index + 1} / ${list.length}`;
    previous.disabled = index === 0;
    next.disabled = index === list.length - 1;
    previous.hidden = next.hidden = list.length === 1;
    scroll.scrollTop = scroll.scrollLeft = 0;
  }
  viewer.addEventListener("click", (ev) => { if (!ev.target.closest("img, button")) viewer.close(); });
  viewer.addEventListener("keydown", (ev) => {
    if (ev.key === "Escape") ev.stopPropagation();
    if (ev.key === "ArrowRight" && !next.disabled) show(index + 1);
    if (ev.key === "ArrowLeft" && !previous.disabled) show(index - 1);
  });
  viewer.addEventListener("close", () => { viewer.remove(); previousFocus.focus(); });
  document.body.append(viewer);
  show(index);
  viewer.showModal();
}

function thumb(att, list = [att], index = 0) {
  return h("button", { class: "thumb", type: "button", "aria-label": "Open image viewer", on: { click: () => openImages(list, index) } },
    h("img", { src: mediaUrl(att.id), alt: "Screenshot", loading: "lazy", width: att.width, height: att.height }));
}

export function attachmentsView(list) {
  return list && list.length ? h("div", { class: "attach-row" }, ...list.map((att, index) => thumb(att, list, index))) : null;
}

export function withAttachments(op, key) {
  const list = attaching[key] || [];
  delete attaching[key];
  delete attachNote[key];
  return list.length ? { ...op, attachments: list } : op;
}

async function addImages(key, files) {
  for (const file of [...files]) {
    try {
      const list = attaching[key] || [];
      if (list.length >= MAX_ATTACH) throw new Error(`At most ${MAX_ATTACH} images per line`);
      const resp = await uploadImage(file);
      if (!resp.ok) throw new Error(await resp.text());
      const att = await resp.json();
      if (!list.some((a) => a.id === att.id)) attaching[key] = [...list, att];
      delete attachNote[key];
    } catch (e) {
      attachNote[key] = (e && e.message) || "Upload failed";
    }
    refreshTray(key);
  }
}

function trayView(key) {
  const list = attaching[key] || [];
  return h("div", { class: "attach-tray", "data-tray": key },
    ...list.map((att) => h("span", { class: "attach-pre" }, thumb(att),
      h("button", { class: "link danger", type: "button", text: "Remove", "aria-label": "Remove this image", on: { click: () => {
        attaching[key] = (attaching[key] || []).filter((a) => a.id !== att.id);
        refreshTray(key);
      } } }))),
    attachNote[key] ? h("span", { class: "attach-note", text: attachNote[key] }) : null);
}

export function refreshTray(key) {
  const old = [...document.querySelectorAll("[data-tray]")].find((el) => el.dataset.tray === key);
  if (old) old.replaceWith(trayView(key));
}

export function attachable(key, box, zone) {
  const pick = h("input", { type: "file", accept: ACCEPT, multiple: "", hidden: "", "aria-label": "Pick images", on: { change: () => {
    addImages(key, pick.files);
    pick.value = "";
  } } });
  box.addEventListener("paste", (ev) => {
    const files = ev.clipboardData ? [...ev.clipboardData.files] : [];
    if (!files.length) return;
    if (!ev.clipboardData.getData("text/plain")) ev.preventDefault();
    addImages(key, files);
  });
  zone.addEventListener("dragover", (ev) => {
    if (![...ev.dataTransfer.types].includes("Files")) return;
    ev.preventDefault();
    zone.classList.add("dropping");
  });
  zone.addEventListener("dragleave", (ev) => { if (!zone.contains(ev.relatedTarget)) zone.classList.remove("dropping"); });
  zone.addEventListener("drop", (ev) => {
    zone.classList.remove("dropping");
    if (!ev.dataTransfer.files.length) return;
    ev.preventDefault();
    addImages(key, ev.dataTransfer.files);
  });
  return h("div", { class: "attach-bar" }, pick,
    h("button", { class: "link", type: "button", text: "Add image", title: "Pick, paste or drop images", on: { click: () => pick.click() } }), trayView(key));
}
