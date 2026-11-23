### Changed

- A waiting offer now moves behind a later offer the hub would offer before it, where before it moved only behind one of a higher class: a task raised to high passes the normal offers queued before it, a task lowered to low falls behind them, and an ordered offer falls behind one whose order is older. Inside the urgent or focused class a waiting offer keeps its place. An offer still moves at most once per dedupe window.
