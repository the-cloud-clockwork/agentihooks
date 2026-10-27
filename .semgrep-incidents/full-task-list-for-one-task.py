import time


def own_task(ledger, slug, agent):
    # ruleid: full-task-list-for-one-task
    return next((row for row in ledger.tasks(slug) if row.get("id") == agent.task), {})


def own_task_by_key(ledger, slug, agent):
    # ruleid: full-task-list-for-one-task
    return next((t for t in ledger.tasks(slug) if t["id"] == agent.task), None)


def own_row(ledger, slug, agent):
    # ruleid: full-task-list-for-one-task
    return {task["id"]: task for task in ledger.tasks(slug)}.get(agent.task, {})


def wait_for_state(ledger, slug, task_id):
    while True:
        # ruleid: full-task-list-for-one-task
        rows = ledger.tasks(slug)
        if any(row["id"] == task_id and row["state"] == "done" for row in rows):
            return
        time.sleep(5)


def per_agent(ledger, slug, agents):
    for agent in agents:
        # ruleid: full-task-list-for-one-task
        rows = ledger.tasks(slug)
        print(agent, len(rows))


def poll_rows(ledger, slug, task_id):
    # ruleid: full-task-list-for-one-task
    while True:
        # ruleid: full-task-list-for-one-task
        for row in ledger.tasks(slug):
            if row["id"] == task_id and row["state"] == "done":
                return
        time.sleep(5)


def rows_per_agent(ledger, slug, agents):
    # ruleid: full-task-list-for-one-task
    for agent in agents:
        # ruleid: full-task-list-for-one-task
        for row in ledger.tasks(slug):
            print(agent, row)


def poll_every_ledger(ledger, slugs):
    while True:
        for slug in slugs:
            # ruleid: full-task-list-for-one-task
            print(slug, len(ledger.tasks(slug)))
        time.sleep(5)


def one_read(ledger, slug, agent):
    # ok: full-task-list-for-one-task
    return ledger.task(slug, agent.task) or {}


def whole_board(ledger, slug, members):
    # ok: full-task-list-for-one-task
    for task in ledger.tasks(slug):
        if task["id"] in members:
            print(task)


def read_once(ledger, slug, agents):
    # ok: full-task-list-for-one-task
    rows = {task["id"]: task for task in ledger.tasks(slug)}
    for agent in agents:
        print(rows.get(agent.task))


def every_ledger(ledger, slugs):
    for slug in slugs:
        # ok: full-task-list-for-one-task
        print(slug, len(ledger.tasks(slug)))
