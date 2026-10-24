### Added

- `agentihooks host-guard install` links the home guard script and config to this checkout, removes the hand written guard systemd timer and runs the guard every minute on the agentihooks scheduler; `agentihooks host-guard check` names every home guard file that is not a link to the repository copy, a leftover guard systemd unit and a missing schedule. `agentihooks init` brings an installed guard back in sync.
