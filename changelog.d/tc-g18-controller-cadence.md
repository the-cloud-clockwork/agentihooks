### Changed

- The swarm controller starts each tick pass one tick after the previous pass started, not one tick after it ended. A pass that outlasts the tick is followed by the next at once. On the live swarm the gap after each pass was a constant 63 seconds, so passes started about 109 seconds apart.
- The simulated tick pass budget drops from 384 to 153 seconds, the measured simulated pass of 152.5 seconds.
