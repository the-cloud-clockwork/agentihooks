(() => {
  const DELAY = 1000;
  const CONTROLS = 'button, [role="tab"], [role="button"], [role="switch"], a.sync, a.fab, [data-gate-name], [data-tip]:not([data-tip=""])';
  const TIPS = [
    ["#title-edit", "Rename this ledger. The new title shows here and on HOME."],
    ['#title-form [type="submit"]', "Save the new ledger title."],
    ["#title-cancel", "Keep the current title and close the rename box."],
    ['[data-swarm="reopen"]', "Reopen this closed ledger so a fresh master picks it up and the swarm can run again."],
    ["#tab-ledger", "Show the ledger: priorities, notes, questions, phases, tasks and follow ups."],
    ["#tab-swarm", "Show the swarm: controls, caps, agents, quota, Doctor, health findings and seat handoffs."],
    ["#sections-all", "Open or fold every section of the ledger at once."],
    ["#comments-all", "Show or hide the comments under every item on the page."],
    ["[data-comments]", "Show or hide the comments under every item in this section."],
    ["#outline-all", "Open or fold every group in the outline."],
    ["#prio-clear-all", "Clear every priority from the list. The items they point at stay as they are."],
    ["#priorities .link.danger", "Clear this priority. The item it points at stays as it is."],
    ["#priorities .link.approve", "Approve this ask: posts your approval with the ask on the item and clears the priority."],
    [".link.approve", "Approve this item: posts your approval on it and clears any priority on it."],
    [".link.deny", "Deny this item: posts denied on it and clears any priority on it."],
    ['[data-swarm="start"]', "Start the swarm. Agents spawn to claim open tasks, up to each lane's cap."],
    ['[data-swarm="pause"]', "Pause the swarm. No new agents spawn; agents already running keep working."],
    ['[data-swarm="stop"]', "Stop the swarm gently. No new agents spawn, and running agents end when their work ends."],
    ['[data-swarm="stop_now"]', "Stop the swarm now. Every agent is closed at once and its unfinished task goes back to open."],
    ['[data-swarm="close"]', "Close this ledger. Its summary goes in the overview, every agent is removed, and the swarm is kept for later."],
    ['[data-autonomy="manual"]', "Manual autonomy. You approve every phase plan; the master only comments on it."],
    ['[data-autonomy="assist"]', "Assist autonomy. You approve every phase plan, and the master posts its recommendation first."],
    ['[data-autonomy="delegate"]', "Delegate autonomy. The master approves or sends back phase plans; you decide after three send backs."],
    ['[data-autonomy="full"]', "Full autonomy. The master approves phase plans and turns agents' follow ups into tasks without asking you."],
    ["#layout-reset", "Restore the default panel sizes on every ledger."],
    ['[data-gate-name="identity"]', "Checks the session identity on commands against the pinned agent identity."],
    ['[data-gate-name="subagents"]', "Counts subagent launches and continuations per task against its budget."],
    ['[data-gate-name="intent"]', "Checks changes against task, phase and project intent, including failed or pending tick verdicts."],
    ['[data-gate-name="claim-stop"]', "Checks unfinished work, checked waits, failing checks and merges due when an agent stops."],
    ['[data-gate-name="push-stop"]', "Pushes the task branch when an agent stops, and checks for uncommitted work or pushed work with no pull request or ledger line."],
    ['[data-gate-name="claims"]', "Counts agent lives claiming each task against the three claim limit."],
    ['[data-gate-name="talk"]', "Counts engineer and CI ledger talk writes since their last outcome against the talk budget."],
    ['[data-gate-name="watch"]', "Counts watch calls since the last action against the count and watch to action ratio limits."],
    ['[data-gate-name="reruns"]', "Counts CI reruns per pull request head against the two rerun limit; jobs cancelled before reaching a runner are exempt."],
    ['[data-gate-name="build"]', "Checks edits and commits against the traced plan, its validity and task territory."],
    ['[data-gate-name="one-push"]', "Holds a pull request until both reviews close and its work is on origin, and refuses a push while its checks run unless one is red."],
    ['[data-gate-name="quiet"]', "Checks claimed tasks for thirty minutes without progress; ledger commands remain available."],
    ['[data-gate-name="trace-plan"]', "Checks plan pieces against task, phase and project intent and the size of one pull request."],
    ["#quota-refresh", "Probe every Claude and Codex account now and redraw the quota table."],
    ['[data-gate-mode="enforce"]', "When the check fails, refuse the call and log the denial."],
    ['[data-gate-mode="observe"]', "When the check fails, let the call through and log a would be denial."],
    ['[data-gate-mode="off"]', "Skip the check. It does not run or log a decision."],
    ['[data-swarm="eng_down"]', "Lower the most engineer agents that may run at once."],
    ['[data-swarm="eng_up"]', "Raise the most engineer agents that may run at once."],
    ['[data-swarm="ci_down"]', "Lower the most CI agents that may run at once."],
    ['[data-swarm="ci_up"]', "Raise the most CI agents that may run at once."],
    ['[data-swarm="plan_down"]', "Lower the most planner agents that may run at once."],
    ['[data-swarm="plan_up"]', "Raise the most planner agents that may run at once."],
    ['[data-swarm="compact_down"]', "Lower the context size at which an agent writes its handoff."],
    ['[data-swarm="compact_up"]', "Raise the context size at which an agent writes its handoff."],
    ['[data-swarm="effort_min_down"]', "Lower the least effort a new agent may start with."],
    ['[data-swarm="effort_min_up"]', "Raise the least effort a new agent may start with."],
    ['[data-swarm="effort_max_down"]', "Lower the most effort a new agent may start with."],
    ['[data-swarm="effort_max_up"]', "Raise the most effort a new agent may start with."],
    ['[data-swarm="apply"]', "Send every changed capacity value to the swarm in one change."],
    ["#overlays-apply", "Send the changed overlay choices for each role to the swarm in one change."],
    ["[data-overlay]", "Add or remove this overlay for the role; a role wears at most three."],
    ['[data-swarm="doctor_start"]', "Start a Doctor that watches this swarm and rates each coordination failure."],
    ['[data-swarm="doctor_stop"]', "Stop the Doctor watching this swarm."],
    ["[data-message]", "Open the chat addressed to this agent."],
    ["[data-terminate]", "End this agent's session now."],
    ['[data-restore-choice="resume"]', "Resume this seat's earlier conversation after the restore."],
    ['[data-restore-choice="fresh"]', "Start this seat fresh from its handoff instead of its earlier conversation."],
    [".phase-review button:first-of-type", "Approve this phase plan so engineers can claim its tasks."],
    [".phase-review button:last-of-type", "Send this phase plan back to the planner with the note you wrote."],
    ["#stats-sync", "Ask the master to recheck the stats and answer you here."],
    ["#sync", "Ask every agent on this ledger to read it again now. The badge counts agents still to sync."],
    ["#to-top", "Scroll back to the top of the page."],
    ["#home", "Go to HOME, the list of every ledger."],
    ["#outline-toggle", "Show or hide the outline of every section and item."],
    ["#bell", "Open your notifications: comments, replies and answers from agents."],
    ["#chat-fab", "Open the chat with the master or any agent of this swarm."],
    ["#art-fab", "Open the artifacts agents published for you to review."],
    ["#art-close", "Close the artifacts panel."],
    ["#chat-clear", "Clear the chat history on this page."],
    ["#chat-size", "Make the chat panel larger or smaller."],
    ["#chat-to-pick", "Choose who gets your next message: the master, the whole swarm or one agent."],
    ['#chat-to-menu [data-to="swarm"]', "Send to every live agent of the swarm, the master included."],
    ["#chat-to-menu [data-to]", "Send your next message to this recipient."],
    ["#chat-close", "Close the chat panel."],
    ["#notif-clear-all", "Clear every notification."],
    ["#notif-close", "Close the notifications panel."],
    ["#notifs .link.danger", "Clear this notification."],
    ["#alert-fab", "Open the alerts: size limits and refused writes, each for the master or for you."],
    ["#alert-close", "Close the alerts panel."],
    ["#alerts .alert-claim", "Claim this alert so everyone sees you hold it."],
    ["#alerts .alert-done", "Close this alert as done. You write what was done first."],
    ['#alerts .alert-outcome [type="submit"]', "Save the outcome and close this alert as done."],
    [".art-title", "Open this artifact in the viewer."],
    ["#art-list .link.danger", "Move this artifact to the trash."],
    ["#art-trash .link", "Restore this artifact from the trash."],
    [".thumb", "Open this image in the viewer."],
    ['[aria-label="Previous image"]', "Show the previous image."],
    ['[aria-label="Next image"]', "Show the next image."],
    ['[aria-label="Close viewer"], [aria-label="Close artifact"]', "Close the viewer."],
    ['[aria-label="Remove this image"]', "Remove this image before you send."],
    [".attach-bar .link", "Attach images to this comment. You can also paste or drop them."],
    [".entry-actions .link.danger", "Delete this entry."],
    [".entry-actions .link", "Edit this entry."],
    [".add", "Write a new entry here. Enter sends it, Esc cancels."],
    ['.scope[aria-pressed="true"]', "Bring this item back in scope."],
    [".scope", "Mark this item out of scope. It stays on the ledger but no longer counts as open work."],
    [".act.del", "Move this ledger to the bin. Its swarm stops; the bin keeps it thirty days."],
    [".act.reopen", "Reopen this closed ledger so a fresh master picks it up."],
    [".act.restore", "Restore this ledger from the bin to HOME."],
    ["button.fold", "Show this ledger's full title and overview, or fold it back to one line."],
    ["#fold-all", "Open or fold every ledger row at once."],
    ["button.sort", "Sort the ledgers by this column. Click again to reverse the order."],
    ["#bin-fab", "Open the bin: deleted ledgers, kept thirty days."],
    ["#home-fab", "Back to HOME, the list of every ledger."],
  ];

  const box = document.createElement("div");
  box.className = "ledger-tip";
  box.popover = "manual";
  box.setAttribute("role", "tooltip");
  let timer = 0;
  let current = null;
  let muted = null;

  function control(el) {
    return el && el.closest ? el.closest(CONTROLS) : null;
  }

  function tip(el) {
    const target = control(el);
    if (!target) return null;
    if (target.dataset.tip) return target.dataset.tip;
    const found = TIPS.find(([selector]) => target.matches(selector));
    return found ? found[1] : null;
  }

  function hide() {
    clearTimeout(timer);
    timer = 0;
    current = null;
    if (box.isConnected && box.matches(":popover-open")) box.hidePopover();
  }

  function show(target, text) {
    if (!box.isConnected) document.body.append(box);
    box.textContent = text;
    box.showPopover();
    const at = target.getBoundingClientRect();
    const size = box.getBoundingClientRect();
    const left = Math.min(Math.max(8, at.left + at.width / 2 - size.width / 2), innerWidth - size.width - 8);
    const below = at.bottom + 8;
    box.style.left = `${left}px`;
    box.style.top = `${below + size.height > innerHeight - 8 ? at.top - size.height - 8 : below}px`;
  }

  document.addEventListener("pointerover", (ev) => {
    const target = control(ev.target);
    if (target === current || target === muted) return;
    hide();
    muted = null;
    const text = tip(target);
    if (!text) return;
    if (target.hasAttribute("title")) {
      if (!target.hasAttribute("aria-label")) target.setAttribute("aria-label", target.title);
      target.removeAttribute("title");
    }
    current = target;
    timer = setTimeout(() => show(target, text), DELAY);
  });
  document.addEventListener("pointerout", (ev) => {
    const target = current || muted;
    if (target && !target.contains(ev.relatedTarget)) {
      hide();
      muted = null;
    }
  });
  const dismiss = () => {
    muted = current || muted;
    hide();
  };
  document.addEventListener("pointerdown", dismiss, true);
  document.addEventListener("click", dismiss, true);
  window.addEventListener("scroll", dismiss, true);
  window.ledgerTip = tip;
})();
