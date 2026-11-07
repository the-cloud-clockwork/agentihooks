### Changed

- The host tick runs each swarm once per scheduled pass. The extra passes that started while a slower swarm still ran, their cutoff and the first pass barrier are gone. The drained resume, the dev red and prerequisite reopens, the promotion and release watches, the fleet red notice, the quota notice, the lost runner sweep and the time left refresh now run as registered tick steps in their own groups, in the same order as before.
