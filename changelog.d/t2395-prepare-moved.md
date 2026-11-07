### Changed

- `_prepare`, which claims a task, picks its seat and primes its launch for the
  spawn pass, moves into `scripts.tick.steps.place` beside its one caller,
  `_spawn`, typed for the strict place module. It reaches tick names through
  the tick module, so patched tick seams still apply; the swarm tick module
  re-exports it and the tick pattern allowlist for the tick module drops its
  entry.
