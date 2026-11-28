# ruleid: feature-work-folder-readers-and-launch-pointers
def _synced(store, slug, ledger, key):
    ran = latest(store.agents(slug))
    copies, found = {}, {}
    for task in ledger.get("tasks", []):
        if not task.get("workspace"):
            continue
        host = ports.hive().reports.host(ran[task["id"]]) if task["id"] in ran else ""
        if not host:
            found[task["id"]] = None
            continue
        if host not in copies:
            copies[host] = json.loads(store.redis.get(key(host)) or "{}")
        found[task["id"]] = copies[host].get(slug, {}).get(task["id"], {})
    return found


# ok: feature-work-folder-readers-and-launch-pointers
def _synced(store, slug, ledger, key):
    ran = latest(store.agents(slug))
    copies, found = {}, {}
    for task in ledger.get("tasks", []):
        if not task.get("workspace"):
            continue
        host = ports.hive().reports.workspace_host(ran[task["id"]]) if task["id"] in ran else ""
        if not host:
            found[task["id"]] = None
            continue
        if host not in copies:
            copies[host] = json.loads(store.redis.get(key(host)) or "{}")
        found[task["id"]] = copies[host].get(slug, {}).get(task["id"], {})
    return found


# ruleid: feature-work-folder-readers-and-launch-pointers
def launched_on(store, slug, task_id, hive):
    store.redis.set(store.key(slug, WORK_FOLDER, task_id), hive, ex=FILES_SECONDS)


# ok: feature-work-folder-readers-and-launch-pointers
def launched_on(store, slug, task_id, hive):
    store.redis.set(store.key(slug, WORK_FOLDER, task_id), hive)


# ruleid: feature-work-folder-readers-and-launch-pointers
def complete(self, order, ack):
    if not self.slots.acknowledge(agent.execution_id, account):
        return Outcome("spawn", Status.REFUSED, HIVE, detail=LATE)
    placed = Placed(agent.pane_id, agent.harness, agent.account)
    return Outcome("spawn", Status.OK, HIVE, placed)


# ok: feature-work-folder-readers-and-launch-pointers
def complete(self, order, ack):
    if not self.slots.acknowledge(agent.execution_id, account):
        return Outcome("spawn", Status.REFUSED, HIVE, detail=LATE)
    ledger_workspace.launched_on(self.store, self.slug, order.task, order.hive)
    placed = Placed(agent.pane_id, agent.harness, agent.account)
    return Outcome("spawn", Status.OK, HIVE, placed)


# ruleid: feature-work-folder-readers-and-launch-pointers
def admit_legacy(store, slug, agent, runtime, table=None):
    try:
        target = process.launch_target(pid, process.local_namespace(), (table or processes)())
        return store.start_execution(slug, replace(agent, hive=hive_id(), runtime_target=target), "")
    except Exception as exc:
        runtime.retire(agent)
        raise SpawnError(str(exc), pane_id=agent.pane_id) from exc


# ok: feature-work-folder-readers-and-launch-pointers
def admit_legacy(store, slug, agent, runtime, table=None):
    try:
        target = process.launch_target(pid, process.local_namespace(), (table or processes)())
        started = store.start_execution(slug, replace(agent, hive=hive_id(), runtime_target=target), "")
    except Exception as exc:
        runtime.retire(agent)
        raise SpawnError(str(exc), pane_id=agent.pane_id) from exc
    ledger_workspace.launched_on(store, slug, started.task, started.hive)
    return started


# ruleid: feature-work-folder-readers-and-launch-pointers
def admit(store, launch, deps):
    agent = replace(launch.agent, harness="codex", transport=chosen, hive=hive_id(), runtime_target=target)
    return store.start_execution(launch.slug, agent, launch.previous_execution_id)


# ok: feature-work-folder-readers-and-launch-pointers
def admit(store, launch, deps):
    if launch.execution_id:
        return adopt(store, launch, chosen, hive_id())
    agent = replace(launch.agent, harness="codex", transport=chosen, hive=hive_id(), runtime_target=target)
    started = store.start_execution(launch.slug, agent, launch.previous_execution_id)
    ledger_workspace.launched_on(store, launch.slug, started.task, started.hive)
    return started


class HiveLaunch:
    # ok: feature-work-folder-readers-and-launch-pointers
    def admit(self, request, selected):
        return self.store.start_execution(self.slug, request.agent, "")


def named(slug, agent, recorded=""):
    # ruleid: feature-work-folder-readers-and-launch-pointers
    if ports.hive().reports.host(agent):
        return portable(slug, agent.task)
    return recorded or str(folder(slug, agent.task))


def named(slug, agent, recorded=""):
    # ok: feature-work-folder-readers-and-launch-pointers
    if ports.hive().reports.workspace_host(agent):
        return portable(slug, agent.task)
    return recorded or str(folder(slug, agent.task))
