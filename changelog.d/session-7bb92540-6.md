### Changed

- The focus unclaimed and order waiting health checks keep their state between tick passes through one helper; the stored values are unchanged. A Semgrep rule holds new health checks to the helper, and the tick-step skill says such state is private to its step and declared as a write only once another step reads it.
