### Fixed

- An expert launch that fails keeps its reason. The expert seats pass stores the error, redacted and on one line, on the binding of the failed transfer and on the lineage record as its last failed launch, and `expert status` shows it; before, only a line the tick dropped with an abandoned step held it.
