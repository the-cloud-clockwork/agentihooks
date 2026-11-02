### Fixed

- Raise the state bucket revision inside the write transaction, so a write that loses its watch no longer leaves the execution copy unused and every other projected entry dropped by the next writer.
