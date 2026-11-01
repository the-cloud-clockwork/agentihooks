### Fixed

- A reloaded ledger server no longer dies before it publishes its pid. Two of its threads could write the loaded code record at once under one temporary name, so one rename failed; each thread now writes its own temporary file.
