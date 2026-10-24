### Fixed

- Plan tracing keeps its passing verdict when a cut piece follow up write is refused or its answer is lost, and files each cut piece exactly once on a later run. Verdicts that marked a cut filed before the ledger accepted it are checked against the ledger and the missing follow ups are filed.
