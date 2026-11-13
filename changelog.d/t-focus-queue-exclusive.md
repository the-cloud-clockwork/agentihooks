### Changed

- Focus is an exclusive ordered queue. While any focused task is not done, the tick claims no task outside the focus, urgent ones included, and focused tasks take every free lane seat in queue order instead of a focus weight share.
- `agentihooks swarm <id> focus TARGET` takes `--only`, which drops every other focus, and `--top` or `--bottom` for the target's place in the queue. A focus unfreezes its target and everything under it, a freeze ends the focus on its own target, and `unfreeze` leaves a task free to run after the focus queue empties.
