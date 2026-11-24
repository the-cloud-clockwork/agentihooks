# ruleid: hive-member-review-keys
WORKER_KEYS = (
    "swarm:*:agents",
    "swarm:*:transfers",
)

# ruleid: hive-member-review-keys
WORKER_KEYS = (
    "swarm:*:agents",
    "swarm:*:review:*",
    "swarm:*:transfers",
)

# ruleid: hive-member-review-keys
WORKER_KEYS = (
    "swarm:*:agents",
    "swarm:*:review-tasks",
    "swarm:*:transfers",
)

# ok: hive-member-review-keys
WORKER_KEYS = (
    "swarm:*:agents",
    "swarm:*:review:*",
    "swarm:*:review-tasks",
    "swarm:*:transfers",
)

# ok: hive-member-review-keys
OTHER_KEYS = ("swarm:*:agents",)
