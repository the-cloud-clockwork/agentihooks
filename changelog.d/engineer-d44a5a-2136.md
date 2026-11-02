### Fixed

- Skip the Doctor active read freshness and count requests for a binding whose current trace update time did not change since the last scan, so a pass of two dozen bindings fits the fifteen second budget.
