### Fixed

- On Windows a session end skips closing the broadcast session, as session start already skips registering it; the step imported the session registry and failed with `No module named resource`.
