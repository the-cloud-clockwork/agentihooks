def resolution(held, rows, inbox):
    # ruleid: wait-reads-one-inbox-world
    item = inbox.get(held["target"])
    return item.state


def reply_resolution(target, tick_inbox):
    # ruleid: wait-reads-one-inbox-world
    return tick_inbox.get(target).state


def located_resolution(held, rows, inbox):
    # ok: wait-reads-one-inbox-world
    item = inbox.locate(held["target"])
    return rows.get(held["task"]), item.state


def wake(inbox, item_id):
    # ok: wait-reads-one-inbox-world
    return inbox.get(item_id)
