### Changed

- The type gate passes a pull request whose files now have fewer type errors than the baseline records, as long as the baseline entry stays at or below the base's count. Fixed errors no longer need a baseline write, so pull requests stop going red and conflicting on the shared baseline file. A count above the measurement that exceeds the base, and any count below the measurement, still fail.
