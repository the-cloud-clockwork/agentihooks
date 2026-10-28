### Fixed

- Swarm launches: execution history writes from one tick's parallel launches are serialized, so a batch of six to eight launches no longer exhausts the admission retries and reopens tasks with `execution history kept changing; admission was not committed`.
