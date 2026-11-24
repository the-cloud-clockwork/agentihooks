### Changed

- The orders read and the focus read of the ledger API build their reply through one helper: the data, its revision, the empty cursor of a read that lists and the reply limit with the read's own message. The replies are unchanged; a recorded run holds their text. A Semgrep rule refuses a reply measured to bound it by hand outside the resources module.
