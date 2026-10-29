### Fixed

- The spawn stall health check no longer raises while the local agent cap holds the lane: when the latest spawn refusal since the previous observation was the local cap and that machine's local slots are still at the recorded maximum, the stall clock resets. With one local slot free the finding still raises.
