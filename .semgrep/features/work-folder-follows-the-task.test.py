# ruleid: feature-work-folder-follows-the-task
def sync_workspaces(self, ttl):
    hosted = [
        (slug, task)
        for slug in self.store.slugs()
        for task, agent in ledger_workspace.latest(self.store.agents(slug)).items()
        if agent.execution_id and agent.lane != MASTER and agent.runtime_target.get("host_id") == self.hive
    ]
    ledger_workspace.sync(self.store.redis, self.hive, hosted, ttl)


# ruleid: feature-work-folder-follows-the-task
def sync_workspaces(self, ttl):
    hosted = [
        (slug, task)
        for slug in self.store.slugs()
        for task, agent in ledger_workspace.latest(self.store.agents(slug)).items()
        if agent.execution_id
        and agent.lane != MASTER
        and (agent.runtime_target.get("host_id") if agent.runtime_target else agent.hive) == self.hive
    ]
    ledger_workspace.sync(self.store.redis, self.hive, hosted, ttl)


# ok: feature-work-folder-follows-the-task
def sync_workspaces(self, ttl):
    hosted = [
        (slug, task)
        for slug in self.store.slugs()
        for task, agent in ledger_workspace.latest(self.store.agents(slug)).items()
        if agent.execution_id
        and agent.lane != MASTER
        and (agent.runtime_target.get("host_id") or agent.hive) == self.hive
    ]
    ledger_workspace.sync(self.store.redis, self.hive, hosted, ttl)


# ruleid: feature-work-folder-follows-the-task
def sync(redis, hive, hosted, ttl):
    copy, files = {}, {}
    for slug, task_id in hosted:
        copy.setdefault(slug, {})[task_id] = tails(slug, task_id)
        files.setdefault(slug, {})[task_id] = contents(slug, task_id)
    redis.set(synced_key(hive), json.dumps(copy), ex=ttl)
    redis.set(files_key(hive), json.dumps(files), ex=FILES_SECONDS)


# ok: feature-work-folder-follows-the-task
def sync(redis, hive, hosted, ttl):
    copy, files = {}, {}
    for slug, task_id in hosted:
        copy.setdefault(slug, {})[task_id] = tails(slug, task_id)
        found = contents(slug, task_id)
        files.setdefault(slug, {})[task_id] = found
        if found:
            redis.set(task_files_key(hive, slug, task_id), json.dumps(found), ex=FILES_SECONDS)
    redis.set(synced_key(hive), json.dumps(copy), ex=ttl)
    redis.set(files_key(hive), json.dumps(files), ex=FILES_SECONDS)


# ok: feature-work-folder-follows-the-task
def sync(redis, hive, hosted, ttl):
    copy, files = {}, {}
    for slug, task_id in hosted:
        copy.setdefault(slug, {})[task_id] = tails(slug, task_id)
        found = contents(slug, task_id)
        files.setdefault(slug, {})[task_id] = found
        if found:
            redis.set(task_files_key(hive, slug, task_id), json.dumps(found))
    redis.set(synced_key(hive), json.dumps(copy), ex=ttl)
    redis.set(files_key(hive), json.dumps(files), ex=FILES_SECONDS)


# ruleid: feature-work-folder-follows-the-task
def launched(self, slug, request):
    if not self.store.execution_registry.set_launch(slug, occupant.execution_id, decision):
        return False
    cast(Any, self.store).redis.hdel(self.store.key(slug, "launch-assignments"), request["task"])
    return True


# ok: feature-work-folder-follows-the-task
def launched(self, slug, request):
    if not self.store.execution_registry.set_launch(slug, occupant.execution_id, decision):
        return False
    folder = self.store.key(slug, WORK_FOLDER, request["task"])
    cast(Any, self.store).redis.set(folder, request["hive"])
    cast(Any, self.store).redis.hdel(self.store.key(slug, "launch-assignments"), request["task"])
    return True


# ruleid: feature-work-folder-follows-the-task
def _seeds(self, slug, task, doc, name):
    earlier = [agent for agent in self.store.agents(slug) if agent.task == task["id"] and agent.name != name]
    previous = max(earlier, key=lambda agent: agent.started_at, default=None)
    held = self.store.redis.get(ledger_workspace.files_key(previous.hive)) if previous and previous.hive else None
    synced = json.loads(held) if held else {}
    return {**synced.get(slug, {}).get(task["id"], {}), "steering.md": ledger_workspace.steering(task, doc)}


# ok: feature-work-folder-follows-the-task
def _seeds(self, slug, task, doc, name):
    earlier = [agent for agent in self.store.agents(slug) if agent.task == task["id"] and agent.name != name]
    previous = max(earlier, key=lambda agent: agent.started_at, default=None)
    redis = cast(Any, self.store).redis
    hives = (redis.get(self.store.key(slug, WORK_FOLDER, task["id"])), previous and previous.hive)
    copies = (redis.get(ledger_workspace.task_files_key(hive, slug, task["id"])) for hive in hives if hive)
    held = next(filter(None, copies), None)
    synced = json.loads(held) if held else {}
    return {**synced, "steering.md": ledger_workspace.steering(task, doc)}
