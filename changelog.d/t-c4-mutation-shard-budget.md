### Changed

- Pull request mutation in Tests runs the measured plan the push preflight already uses. A new refine job sizes the shards from measured test stats and splits mutants whose selected tests exceed one shard's budget into a head plus slices across shards. The shards follow that plan, and the required mutation job merges every shard report and grades split units. Hub functions with thousands of seconds of selected tests now grade inside the 1080 second shard budget instead of filling one shard round robin until its deadline.
