export function applyPatch(value, patch) {
  if ("v" in patch) return patch.v;
  if ("o" in patch) {
    const out = { ...value };
    for (const [key, part] of Object.entries(patch.o)) {
      if ("d" in part) delete out[key];
      else out[key] = applyPatch(out[key], part);
    }
    return out;
  }
  const changed = new Map(patch.u.map((item) => [item.id, item]));
  if ("ids" in patch) {
    const current = new Map(value.map((item) => [item.id, item]));
    return patch.ids.map((id) => (changed.has(id) ? changed.get(id) : current.get(id)));
  }
  return value.slice(patch.drop).map((item) => (changed.size && changed.has(item.id) ? changed.get(item.id) : item)).concat(patch.add);
}
