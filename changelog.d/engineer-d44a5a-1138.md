### Fixed

- Record the acknowledged process identity of a hive worker that finished before the tick collected its launch acknowledgement, so its retire can end and stops failing every tick.
