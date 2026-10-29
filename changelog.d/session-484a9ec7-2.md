### Fixed

- The swarm controller NATS user now publishes to the `scheduler` key value bucket (`$KV.scheduler.>`), matching the antoncore `swarm-controller` user, so the lint step that holds the two equal passes again.
