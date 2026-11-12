def discarded(reading: object) -> object:
    # ruleid: codex-expired-reading-discarded
    return publish(reading.observed_at) if reading and not reading.expired else unknown()


def retained(reading: object) -> object:
    # ok: codex-expired-reading-discarded
    return publish(reading.observed_at) if reading else unknown()
