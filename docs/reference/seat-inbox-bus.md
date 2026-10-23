# Durable seat inbox on the bus

`SeatInbox` is the expand boundary for inbox delivery on the shared event bus.
Existing Redis inbox storage, Claude channels and Codex pumps keep their current
connections until their adoption change. Pass an opened `Bus` and the swarm name
to `SeatInbox`, then bind a consumer with the stable seat address and receiving
session identity. The bus owner closes the bus.

`send(address, Message(...))` persists the immutable envelope before publishing.
The message id belongs to the caller and remains stable through outbox retries.
Reusing an id with another address or payload is refused. Retries reuse the saved
envelope even after the stream deduplication window expires.

Each swarm and seat pair derives one exact subject and one bounded durable name.
Successor sessions reuse that consumer. `fetch` records the receiving session and
claim revision before returning a receipt. Fetch does not acknowledge delivery.
`confirm(receipt)` checks that session and revision, records confirmation, then
acknowledges. A stale receipt cannot confirm a successor's claim. A replay after
an acknowledgement failure is acknowledged without showing the work again.

Delivery retry timing and the dead letter limit come from the supplied bus queue
policy. Confirmed item and immutable send records remain in the bus state store.
This boundary does not resolve aliases, allocate seats, dispatch master pools,
settle action items or migrate existing open mail; its callers supply canonical
seat addresses and preserve their existing task settlement rules.
