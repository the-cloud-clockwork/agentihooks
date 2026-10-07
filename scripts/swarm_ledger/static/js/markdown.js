import { h } from "./dom.js";

function inline(text) {
  const out = [];
  const re = /(`[^`]+`)|\*\*([^*]+)\*\*|\*([^*\s][^*]*)\*|\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g;
  let last = 0;
  for (let m = re.exec(text); m; m = re.exec(text)) {
    out.push(text.slice(last, m.index));
    if (m[1]) out.push(h("code", { text: m[1].slice(1, -1) }));
    else if (m[2]) out.push(h("strong", {}, ...inline(m[2])));
    else if (m[3]) out.push(h("em", {}, ...inline(m[3])));
    else out.push(h("a", { href: m[5], target: "_blank", rel: "noopener noreferrer", text: m[4] }));
    last = re.lastIndex;
  }
  out.push(text.slice(last));
  return out;
}

const MD_LIST = /^\s*([-*+]|\d+[.)])\s+/;
const MD_BREAK = /^(#{1,6}\s|\s*```|\s*~~~|>)/;

export function markdown(text) {
  const lines = text.replace(/\r\n?/g, "\n").split("\n");
  const cells = (row) => row.trim().replace(/^\||\|$/g, "").split("|").map((c) => c.trim());
  const blocks = [];
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    const fence = line.match(/^\s*(```|~~~)/);
    if (fence) {
      const body = [];
      for (i++; i < lines.length && !lines[i].trim().startsWith(fence[1]); i++) body.push(lines[i]);
      i++;
      blocks.push(h("pre", {}, h("code", { text: body.join("\n") })));
    } else if (/^#{1,6}\s/.test(line)) {
      blocks.push(h(`h${line.match(/^#+/)[0].length}`, {}, ...inline(line.replace(/^#+\s+/, "").replace(/\s+#+\s*$/, ""))));
      i++;
    } else if (line.includes("|") && /^\s*\|?\s*:?-{3,}/.test(lines[i + 1] || "")) {
      const head = cells(line);
      const rows = [];
      for (i += 2; i < lines.length && lines[i].includes("|"); i++) rows.push(cells(lines[i]));
      blocks.push(h("table", {}, h("thead", {}, h("tr", {}, ...head.map((c) => h("th", {}, ...inline(c))))),
        h("tbody", {}, ...rows.map((r) => h("tr", {}, ...r.map((c) => h("td", {}, ...inline(c))))))));
    } else if (MD_LIST.test(line)) {
      const ordered = /^\s*\d/.test(line);
      const items = [];
      for (; i < lines.length && MD_LIST.test(lines[i]); i++) items.push(h("li", {}, ...inline(lines[i].replace(MD_LIST, ""))));
      blocks.push(h(ordered ? "ol" : "ul", {}, ...items));
    } else if (/^>/.test(line)) {
      const quote = [];
      for (; i < lines.length && /^>/.test(lines[i]); i++) quote.push(lines[i].replace(/^>\s?/, ""));
      blocks.push(h("blockquote", {}, ...markdown(quote.join("\n"))));
    } else if (/^\s*([-*_])(\s*\1){2,}\s*$/.test(line)) {
      blocks.push(h("hr", {}));
      i++;
    } else if (!line.trim()) {
      i++;
    } else {
      const para = [line.trim()];
      for (i++; i < lines.length && lines[i].trim() && !MD_BREAK.test(lines[i]) && !MD_LIST.test(lines[i]); i++) para.push(lines[i].trim());
      blocks.push(h("p", {}, ...inline(para.join(" "))));
    }
  }
  return blocks;
}

export function jsonTree(value, key) {
  const label = key === undefined ? "" : `${JSON.stringify(key)}: `;
  const keys = value && typeof value === "object" ? Object.keys(value) : [];
  if (!keys.length) {
    const kind = value === null ? "null" : Array.isArray(value) ? "array" : typeof value;
    return h("div", { class: "json-leaf" }, label, h("span", { class: `json-${kind}`, text: JSON.stringify(value) }));
  }
  const list = Array.isArray(value);
  return h("details", { open: "" },
    h("summary", { text: label + (list ? "[" : "{"), "data-close": list ? "]" : "}" }),
    ...keys.map((k) => jsonTree(value[k], list ? undefined : k)),
    h("div", { class: "json-close", text: list ? "]" : "}" }));
}
