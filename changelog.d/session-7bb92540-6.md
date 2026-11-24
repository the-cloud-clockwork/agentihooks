### Changed

- The focus unclaimed and order waiting health checks keep their state between tick passes through one helper; the stored values are unchanged. A Semgrep rule holds new health checks to the helper and a second one refuses any other module reading or writing a check's key. The tick-step skill and the tick pattern rule say such state belongs to the check's module alone, and that a value another module reads is a declared domain written through a port.
