import { grow } from "./dom.js";

let renderVersion = 0;
let rendered = Promise.resolve();

export function renderCycle(focusKey, cooperative, build) {
  const version = ++renderVersion;
  const active = document.activeElement;
  const key = focusKey || (active && active.dataset ? active.dataset.focus : null);
  const selection = !focusKey && active && active.tagName === "TEXTAREA" ? active : null;
  const current = () => version === renderVersion;
  const sections = build(current);
  const paint = async () => {
    for (const section of sections) {
      if (cooperative) {
        await scheduler.postTask(() => {}, { priority: "background" });
        if (!current()) return rendered;
      }
      try {
        const drawing = section();
        if (cooperative) await drawing;
      } catch (error) {
        console.error(error);
      }
    }
    if (cooperative && document.activeElement !== document.body) return;
    const again = key && document.querySelector(`[data-focus="${CSS.escape(key)}"]`);
    if (!again) return;
    again.focus();
    if (again.tagName === "TEXTAREA") {
      grow(again);
      const end = again.value.length;
      again.setSelectionRange(selection ? selection.selectionStart : end, selection ? selection.selectionEnd : end);
    }
  };
  rendered = paint();
  return rendered;
}
