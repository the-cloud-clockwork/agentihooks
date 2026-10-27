def running(records, machine):
    # ruleid: admission-skips-workstation-pinned
    return len([row for row in records if row.get("hive") == machine and not row.get("workstation")])


def occupancy(records):
    # ruleid: admission-skips-workstation-pinned
    return sum(1 for row in records if not row["workstation"])


def held(agents):
    total = 0
    for agent in agents:
        # ruleid: admission-skips-workstation-pinned
        if agent.workstation:
            continue
        total += 1
    return total


def running_on_hive(records, machine):
    # ok: admission-skips-workstation-pinned
    return len([row for row in records if row.get("hive") == machine])


def waiting_for_workstation(rows, machine):
    # ok: admission-skips-workstation-pinned
    return [row for row in rows if row.get("workstation") == machine]
