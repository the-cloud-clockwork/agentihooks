### Changed

- The hub works out the class of each ready offer once for a readiness pass: the order tasks are offered in and the waiting offers moved behind a higher class now read one decider, where two functions decided whether a task is under a focus. The offers and the moves are unchanged; a recorded run of generated ledgers holds them, and a test runs every focus selector through both.
