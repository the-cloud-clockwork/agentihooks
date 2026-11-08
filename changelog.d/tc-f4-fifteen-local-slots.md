### Changed

- The workstation carries fifteen agents: `local-max-agents` defaults to 15 instead of 8 and a headed hive (`ui=yes`) defaults to `max-agents=15` instead of 10. `swarm <id> autoscale` reports the local slots. Stored explicit values are kept, headless hives stay at 20, and the host room guard still refuses spawns when load or available memory leaves no room.
