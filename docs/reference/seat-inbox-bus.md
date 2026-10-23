# Durable seat inbox on the bus

`SeatInbox` is the expand boundary for inbox delivery on the shared event bus.
Redis inbox storage stays the item source until the migration. Pass an opened `Bus` and the swarm name
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

## Claude channel and Codex pump

With `AGENTIHOOKS_BUS=nats` (and `AGENTIHOOKS_BUS_URL`), `InboxStore.send`
publishes every item addressed to a seat to that seat's bus inbox with its item
id; master pool items and items addressed to an agent name stay on Redis alone.
A failed publish keeps the Redis send, prints one warning and skips the bus for `REOPEN_S` seconds.

`SeatFeed` binds the receiver's current seat consumer. The Claude inbox channel
waits on it instead of the Redis notify channel, claims and pushes through the
Redis store as before, and confirms each bus receipt once its item leaves
pending. The Codex pump takes its wake from the feed instead of the Redis notify
channel, reserves through the dispatcher as before, and confirms a receipt once
the accepted delivery moves its item. Items with no bus delivery still arrive at
the channel's Redis recheck and the pump's reconcile. A bus failure raises
`FeedError` once, closes the feed and reopens it after `REOPEN_S` seconds; meanwhile both
transports keep delivering from Redis. Without the setting both run on Redis
notify as before.
