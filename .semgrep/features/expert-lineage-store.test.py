def end(store, slug, name):
    # ruleid: feature-expert-lineage-store
    store.redis.hset(store.key(slug, "expert-lineages"), name, "{}")


def forget(store, slug, digest):
    # ruleid: feature-expert-lineage-store
    store.redis.hdel(store.key(slug, "expert-missions"), digest)


def revive(store, slug, name):
    # ruleid: feature-expert-lineage-store
    store.redis.sadd(store.key(slug, "expert-running"), name)


def rewrite(store, slug, name, raw):
    # ruleid: feature-expert-lineage-store
    store.redis.hset(store.key(slug, expert_lineage.LINEAGES), name, raw)


def swap(store, slug, digest, text):
    # ruleid: feature-expert-lineage-store
    store.redis.hset(store.key(slug, expert_lineage.MISSIONS), digest, text)


def retire(store, slug, name):
    # ruleid: feature-expert-lineage-store
    store.redis.srem(store.key(slug, expert_lineage.ACTIVE), name)


def prove(store, slug, name, text):
    # ruleid: feature-expert-lineage-store
    store.redis.hset(store.key(slug, "expert-proofs"), name, text)


def unprove(store, slug, name):
    # ruleid: feature-expert-lineage-store
    store.redis.hdel(store.key(slug, expert_lineage.PROOFS), name)


def stand(store, slug):
    # ok: feature-expert-lineage-store
    return expert_lineage.running(store, slug)


def end_by_code(store, slug, name, by, note, at):
    # ok: feature-expert-lineage-store
    return expert_lineage.stop(store, slug, name, by, note, at)


def read(store, slug, lineage):
    # ok: feature-expert-lineage-store
    return expert_lineage.mission(store, slug, lineage)


def shown(store, slug, name):
    # ok: feature-expert-lineage-store
    return expert_lineage.proof(store, slug, name)
