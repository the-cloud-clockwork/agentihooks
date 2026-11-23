# ruleid: feature-expert-stop-gate
GATES = {
    gate.name: gate
    for gate in (
        PinnedIdentity(),
        WatchBudget(),
        ClaimStop(),
        PushStop(),
        QuietClaim(),
    )
}

# ok: feature-expert-stop-gate
GATES = {
    gate.name: gate
    for gate in (
        PinnedIdentity(),
        WatchBudget(),
        ClaimStop(),
        ExpertStop(),
        PushStop(),
        QuietClaim(),
    )
}

# ruleid: feature-expert-stop-gate
GATES = {gate.name: gate for gate in (PinnedIdentity(), ClaimStop())}

# ok: feature-expert-stop-gate
GATES = {gate.name: gate for gate in (ExpertStop(), PinnedIdentity())}

# ok: feature-expert-stop-gate
HOST_GATES = {gate.name: gate for gate in (PlacementGate(), PromptGuard())}
