### Fixed

- Keep telemetry exporters progressing through scheduler state conflicts and bounded timer setup, polling and cleanup; name the session in scheduler and terminal failure records.

### Added

- Doctor rates reports distinct telemetry stale incidents first observed per hour from the watched swarm's retained Doctor findings. Missing Doctor history reports unknown instead of zero.
