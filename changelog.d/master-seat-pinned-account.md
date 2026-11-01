### Fixed

- A swarm whose live master sits on a warned account that is also the declared Claude master account no longer holds a worker seat for its successor, because that successor respawns on the same declared account. A seat is still held when the master can move to another account.
