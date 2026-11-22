### Added

- A tick step keeps the seat of every running expert lineage: it launches one expert on an empty seat, the next expert after an accepted handoff, and finishes the expert of a lineage that was stopped or completed. An expert lost without a handoff leaves its successor one: the handoff it never confirmed, offered again, or a recovery handoff the swarm writes.
- `agentihooks swarm <id> expert start <mission file> --handoff <doc> [--from <session>]` starts a lineage from the Handoff v2 of a session that was not an expert; its first expert confirms that handoff like any successor.
- New live check `python -m scripts.feature_checks.expert_seats`: no lineage seat is held by two experts and every expert on a seat belongs to a lineage the swarm started.

### Changed

- The handoff an expert was given stays on its seat until that expert confirms it with `confirm-handoff`.
