### Fixed

- CI runner minutes: the workflow reads Actions through the TCC Main CI App instead of the swarm App, keeps the jobs of completed runs in an Actions cache so later dispatches never read them again, and refuses to start when the job reads would leave fewer than 1000 calls of the hourly limit.
