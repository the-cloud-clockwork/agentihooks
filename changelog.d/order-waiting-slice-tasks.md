### Fixed

- The `order waiting` finding counts every task of an operator order. The tick holds a ledger view without slices, and the orders read missed each task filed under a slice there, so an order with a task in a pull request was reported as nobody's work and the counts ran low. A task now counts under an ordered phase when its row or the lead it is merged into names that phase, beside what the plan graph gives.
