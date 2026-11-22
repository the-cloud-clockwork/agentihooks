### Added

- A phase or a task can carry the operator's order. `agentihooks api order ITEM --quote WORDS` or `--request ENTRY` marks `phases/<id>` or `tasks/<id>` with his recorded words, or with the id of an entry he wrote, and `phase add` and `task add` take `--quote WORDS` to file and mark in one call. Only the operator, or the ledger orchestrator with his recorded words, writes or clears the mark (`order ITEM --clear --quote WORDS`); phase and task updates keep it.
