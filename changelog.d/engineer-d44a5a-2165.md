### Fixed

- Fix traced plan and intent verdict reuse after a task title or description changes. Rejudge earlier plan pieces against the rewritten task while retaining reuse for unchanged task text and plans. Existing traced plans need one `trace-plan` run after upgrade, and stored intent plan verdicts are judged once more.
