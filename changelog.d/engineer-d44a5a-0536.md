### Fixed

- Deadline bound tests keep their deadlines valid under mutation tracing, so
  mutation statistics finish their clean baseline instead of losing a test to
  instrumentation overhead. Every Playwright page and context default timeout
  and the sixteen concurrent ledger task add writers' client deadline take the
  shared mutation trace factor only while a mutation run is active.
