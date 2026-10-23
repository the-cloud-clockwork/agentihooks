### Changed

- Document the session start dependency check: when the stamp is older than six hours it runs for up to one second, then its process group is killed and reaped before the hook returns, with the worker image proof evidence and the tests that pin it.
