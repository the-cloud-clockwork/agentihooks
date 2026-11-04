### Changed

- The execution registry answers whether an agent name is managed from an archived name index kept by the archive move and the records migration, and built once for swarms migrated earlier, instead of loading the whole execution archive. Putting an agent runs that check outside the swarm write lock, so parallel reap no longer queues behind it; one live name read inside the legacy projection's watch still refuses a racing admission.
