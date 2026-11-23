# ruleid: feature-hive-role-release-floor
def hosts(role, hive, aliases=()):
    return hive.get("draining") != YES and not {role, *aliases} & set(hive["excluded"])


# ok: feature-hive-role-release-floor
def hosts(role, hive, aliases=()):
    """Whether the hive may be given the role."""
    return (
        hive.get("draining") != YES
        and not {role, *aliases} & set(hive["excluded"])
        and not outdated(role, hive, aliases)
    )


# ruleid: feature-hive-role-release-floor
def hosts(role, hive, aliases=()):
    floor = outdated(role, hive, aliases)
    return hive.get("draining") != YES


# ok: feature-hive-role-release-floor
def unhosted(role, hives, now, aliases=()):
    return None
