def cmd_complete(store, args):
    agent = _occupant(store, args)
    lineage = expert_lineage.get(store, args.slug, agent.task)
    text = _document(args.proof, "mission proof")
    found = handoff_expert.proof_problems(text, resolves, _merged, args.slug, mission)
    if found:
        raise SwarmError(REFUSED + "\n".join(f"- {fault}" for fault in found))
    # ok: feature-expert-ends-on-proof
    ended = expert_lineage.complete(store, args.slug, lineage.id, agent.name, text, now_ms())
    print(json.dumps(asdict(ended)))


def cmd_complete(store, args):
    agent = _occupant(store, args)
    text = _document(args.proof, "mission proof")
    # ruleid: feature-expert-ends-on-proof
    ended = expert_lineage.complete(store, args.slug, agent.task, agent.name, text, now_ms())
    print(json.dumps(asdict(ended)))


def cmd_complete(store, args):
    agent = _occupant(store, args)
    text = _document(args.proof, "mission proof")
    found = handoff_expert.proof_problems(text, resolves, _merged, args.slug, mission)
    # ruleid: feature-expert-ends-on-proof
    ended = expert_lineage.complete(store, args.slug, agent.task, agent.name, text, now_ms())
    print(json.dumps({"ended": asdict(ended), "faults": found}))


def cmd_stop(store, args):
    # ruleid: feature-expert-ends-on-proof
    ended = expert_lineage.complete(store, args.slug, args.lineage, "operator", "", now_ms())
    print(json.dumps(asdict(ended)))


def run(slug, config, store, runtime, now_ms):
    for lineage in expert_lineage.running(store, slug):
        # ruleid: feature-expert-ends-on-proof
        expert_lineage.complete(store, slug, lineage.id, "swarm", "", now_ms)


def cmd_stop(store, args):
    # ok: feature-expert-ends-on-proof
    ended = expert_lineage.stop(store, args.slug, args.lineage, by, args.note, now_ms())
    print(json.dumps(asdict(ended)))
