import { $, h } from "./dom.js";

const LANE_ROLES = [["eng", ["eng"], ["engineer", "frontend", "qa", "documentation"]], ["ci", ["ci"], ["cicd"]], ["plan", ["plan"], ["planner"]], ["seats", ["dispatch", "master"], ["dispatcher", "master"]]];
const LANE_PROFILES = { eng: "engineer", ci: "cicd", plan: "planner", dispatch: "dispatcher", master: "master" };

export function liveAgents(sw) {
  return (sw.agents || []).filter((a) => a.state !== "finished");
}

function laneRoles(sw) {
  const live = liveAgents(sw), role = (a) => a.profile || LANE_PROFILES[a.lane];
  return LANE_ROLES.map(([lane, lanes, known]) => {
    const agents = live.filter((a) => lanes.includes(a.lane)), names = [...new Set([...known, ...agents.map(role).filter(Boolean)])];
    return [lane, names.map((name) => [name, agents.filter((a) => role(a) === name).length])];
  });
}

export function capacityShown(sw, readError) {
  $("capacity-unavailable").hidden = !readError;
  $("capacity-unavailable").textContent = sw ? "Swarm status could not be read. Showing the last observed state." : "Swarm status could not be read.";
  for (const id of ["capacity-caps", "roles-label", "capacity-roles"]) $(id).hidden = !sw;
}

export function renderRoles(sw) {
  $("capacity-roles").replaceChildren(...(sw ? laneRoles(sw) : []).map(([lane, roles]) => h("div", { class: "sw-role-lane", role: "group", "aria-label": `${lane} roles`, "data-lane": lane },
    h("span", { class: "sw-cap-name", text: lane }),
    ...roles.map(([name, count]) => h("span", { class: "sw-role", "data-role": name }, h("span", { class: "sw-role-name", text: name }), h("span", { class: "sw-role-count", "data-role-count": "", text: String(count) }))))));
}
